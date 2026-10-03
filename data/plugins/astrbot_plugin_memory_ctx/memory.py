"""Compression engine for the memory-context plugin.

The engine implements the "scheduled compression + discard" strategy:

* an incremental pass folds older turns into a rolling summary;
* a daily off-peak pass merges and purges expired records;
* a weekly pass aggregates daily memories into long-term memory;
* a request guard keeps every session under the token ceiling and triggers
  an emergency compression when the ceiling is about to be exceeded.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.core.agent.context.round_utils import rounds_to_text, split_into_rounds
from astrbot.core.utils.session_lock import session_lock_manager

from .storage import MemoryStore, now_ts

SUMMARY_USER_PREFIX = "[MemoryCtx] Previous conversation summary:\n"
SUMMARY_ACK = "Acknowledged the memory summary."

DEFAULT_SUMMARY_INSTRUCTION = (
    "You are a long-term memory engine. Compress the transcript into a concise, "
    "structured memory. Keep facts, entities, user preferences, decisions, "
    "unresolved tasks and emotional tone. Merge with the existing summary when "
    "provided. Never invent facts. Write in the transcript's language."
)
DEFAULT_LONG_TERM_MERGE_INSTRUCTION = (
    "You maintain a single rolling long-term memory for one chat session. "
    "Merge the existing long-term memory with the new daily memory into one "
    "stable, compact profile: durable facts, user preferences, relationships, "
    "recurring topics and open threads. Drop one-off chatter and duplicated "
    "content. Never invent facts. Keep it under roughly 800 words. "
    "Write in the source language."
)
DEFAULT_WEEKLY_INSTRUCTION = (
    "You are a long-term memory engine. Merge the following daily memories into a "
    "single stable long-term profile: durable facts, preferences, relationships, "
    "recurring topics and open threads. Drop one-off chatter. Never invent facts. "
    "Write in the source language."
)

SUMMARY_CHUNK_CHARS = 24000


class MemoryEngine:
    """Orchestrates compression, persistence and context guarding."""

    def __init__(
        self,
        context: Any,
        config: dict,
        store: MemoryStore,
        logger: Any,
    ) -> None:
        """Initialize the engine.

        Args:
            context: The plugin ``Context`` instance.
            config: Plugin configuration mapping.
            store: The memory store.
            logger: Plugin logger.
        """
        self.context = context
        self.config = config or {}
        self.store = store
        self.logger = logger

    def cfg(self, key: str, default: Any = None) -> Any:
        """Read a configuration value with a fallback default.

        Args:
            key: Configuration key.
            default: Value returned when the key is missing or None.

        Returns:
            The configuration value or the default.
        """
        value = self.config.get(key, default)
        return default if value is None else value

    @property
    def max_tokens(self) -> int:
        """Return the configured per-session token ceiling."""
        try:
            return int(self.cfg("max_context_tokens", 200000))
        except (TypeError, ValueError):
            return 200000

    def session_type(self, umo: str) -> str:
        """Classify a unified message origin.

        Args:
            umo: Unified message origin.

        Returns:
            ``group``, ``friend`` or ``other``.
        """
        if ":GroupMessage:" in umo:
            return "group"
        if ":FriendMessage:" in umo:
            return "friend"
        return "other"

    def in_scope(self, umo: str) -> bool:
        """Return whether a session is covered by the current configuration.

        Args:
            umo: Unified message origin.

        Returns:
            True when the session should be compressed.
        """
        scope = str(self.cfg("session_scope", "all")).strip().lower()
        scope_list = self.cfg("scope_list", []) or []
        if scope == "whitelist" and umo not in scope_list:
            return False
        if scope == "blacklist" and umo in scope_list:
            return False
        session_type = self.session_type(umo)
        if session_type == "group" and not bool(
            self.cfg("group_compress_enable", True)
        ):
            return False
        if session_type == "friend" and not bool(
            self.cfg("private_compress_enable", True)
        ):
            return False
        return True

    def _estimate(self, value: Any) -> int:
        """Estimate the token count of a single content value.

        Args:
            value: A string, list or dict content payload.

        Returns:
            The estimated token count.
        """
        if value is None:
            return 0
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                text = str(value)
        chinese = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
        other = len(text) - chinese
        return max(1, int(chinese * 0.6 + other * 0.3))

    def count_tokens(self, history: list) -> int:
        """Estimate the token count of a stored conversation history.

        Checkpoint entries are ignored. The estimate mirrors AstrBot's own
        character-based counter but avoids building pydantic models on every
        request, which keeps the request guard cheap.

        Args:
            history: OpenAI-format history messages.

        Returns:
            The estimated token count.
        """
        total = 0
        for message in history:
            if not isinstance(message, dict) or message.get("role") == "_checkpoint":
                continue
            total += self._estimate(message.get("content"))
            if message.get("tool_calls"):
                total += self._estimate(message.get("tool_calls"))
        return total

    def seconds_until_offpeak(self, now: datetime | None = None) -> float:
        """Return seconds until the next off-peak window.

        Args:
            now: Optional current time override (used for testing).

        Returns:
            0 when currently inside the window, otherwise seconds until its start.
        """
        tz = ZoneInfo(str(self.cfg("timezone", "Asia/Shanghai")))
        current = (now or datetime.now(tz)).astimezone(tz)
        start_h, start_m = self._parse_hm(self.cfg("offpeak_start", "00:30"), (0, 30))
        end_h, end_m = self._parse_hm(self.cfg("offpeak_end", "08:30"), (8, 30))
        start = current.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        end = current.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        if end <= start:
            return 0.0
        if start <= current < end:
            return 0.0
        if current < start:
            return (start - current).total_seconds()
        return (start + timedelta(days=1) - current).total_seconds()

    def _parse_hm(self, value: Any, default: tuple[int, int]) -> tuple[int, int]:
        try:
            hour, minute = str(value).split(":")
            return int(hour), int(minute)
        except (ValueError, AttributeError):
            return default

    def _day_key(self) -> str:
        tz = ZoneInfo(str(self.cfg("timezone", "Asia/Shanghai")))
        return datetime.now(tz).strftime("%Y-%m-%d")

    def _week_key(self) -> str:
        tz = ZoneInfo(str(self.cfg("timezone", "Asia/Shanghai")))
        return datetime.now(tz).strftime("%G-W%V")

    def _is_summary_user(self, message: Any) -> bool:
        return (
            isinstance(message, dict)
            and message.get("role") == "user"
            and isinstance(message.get("content"), str)
            and message["content"].startswith(SUMMARY_USER_PREFIX)
        )

    def _is_ack(self, message: Any) -> bool:
        return (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and message.get("content") == SUMMARY_ACK
        )

    def _split_leading_system(self, history: list) -> tuple[list, list]:
        index = 0
        while (
            index < len(history)
            and isinstance(history[index], dict)
            and history[index].get("role") == "system"
        ):
            index += 1
        return history[:index], history[index:]

    def _strip_summary_pairs(self, body: list) -> tuple[list[str], list]:
        summaries: list[str] = []
        result: list = []
        index = 0
        while index < len(body):
            message = body[index]
            if self._is_summary_user(message):
                summaries.append(
                    str(message.get("content", ""))[len(SUMMARY_USER_PREFIX) :]
                )
                if index + 1 < len(body) and self._is_ack(body[index + 1]):
                    index += 2
                else:
                    index += 1
                continue
            result.append(message)
            index += 1
        return summaries, result

    def _split_index(self, body: list, keep_turns: int) -> int:
        starts = [
            i
            for i, message in enumerate(body)
            if isinstance(message, dict) and message.get("role") == "user"
        ]
        if keep_turns <= 0:
            return len(body)
        if len(starts) <= keep_turns:
            return 0
        return starts[len(starts) - keep_turns]

    async def _provider_id(self, umo: str) -> str:
        configured = str(self.cfg("compress_provider_id", "") or "").strip()
        if configured:
            try:
                if self.context.get_provider_by_id(configured) is not None:
                    return configured
            except Exception:
                pass
        try:
            return await self.context.get_current_chat_provider_id(umo)
        except Exception:
            return ""

    async def _embed(self, text: str) -> list[float] | None:
        provider_id = str(self.cfg("embedding_provider_id", "") or "").strip()
        if not provider_id or not text:
            return None
        try:
            provider = self.context.get_provider_by_id(provider_id)
        except Exception:
            provider = None
        if provider is None or not hasattr(provider, "get_embedding"):
            return None
        try:
            vector = await provider.get_embedding(text[:2000])
        except Exception as exc:
            self.logger.warning(f"embedding failed: {exc}")
            return None
        if isinstance(vector, list) and vector:
            try:
                return [float(item) for item in vector]
            except (TypeError, ValueError):
                return None
        return None

    def _cosine(self, left: list[float], right: list[float]) -> float | None:
        if not left or not right or len(left) != len(right):
            return None
        dot = 0.0
        norm_left = 0.0
        norm_right = 0.0
        for a, b in zip(left, right):
            dot += a * b
            norm_left += a * a
            norm_right += b * b
        if norm_left <= 0 or norm_right <= 0:
            return None
        return dot / ((norm_left**0.5) * (norm_right**0.5))

    async def _rerank(self, query: str, documents: list[str]) -> list[int] | None:
        """Rerank documents with the configured rerank provider.

        Args:
            query: The user query.
            documents: Candidate documents to rerank.

        Returns:
            A list of document indices ordered by relevance, or None when the
            rerank provider is unavailable.
        """
        provider_id = str(self.cfg("rerank_provider_id", "") or "").strip()
        if not provider_id or not documents:
            return None
        try:
            provider = self.context.get_provider_by_id(provider_id)
        except Exception:
            provider = None
        if provider is None or not hasattr(provider, "rerank"):
            return None
        try:
            result = await provider.rerank(query, documents)
        except Exception as exc:
            self.logger.warning(f"rerank failed: {exc}")
            return None
        return self._parse_rerank_result(result, len(documents))

    def _parse_rerank_result(self, result: Any, count: int) -> list[int] | None:
        """Normalize a provider rerank response into ordered document indices.

        Args:
            result: Raw provider response.
            count: Number of candidate documents.

        Returns:
            Ordered document indices, or None when parsing fails.
        """
        if not result:
            return None
        entries: Any = result
        if isinstance(result, dict):
            entries = result.get("results") or result.get("data") or result
        if not isinstance(entries, list):
            return None
        orders: list[tuple[float, int]] = []
        for position, entry in enumerate(entries):
            index = position
            score = float(-position)
            if isinstance(entry, dict):
                raw_index = entry.get("index", entry.get("corpus_id", position))
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    index = position
                raw_score = entry.get("relevance_score", entry.get("score"))
                try:
                    score = float(raw_score)
                except (TypeError, ValueError):
                    score = float(-position)
            elif isinstance(entry, int):
                index = entry
                score = float(count - position)
            if 0 <= index < count:
                orders.append((score, index))
        if not orders:
            return None
        orders.sort(key=lambda item: item[0], reverse=True)
        seen: set[int] = set()
        ordered: list[int] = []
        for _, index in orders:
            if index not in seen:
                seen.add(index)
                ordered.append(index)
        return ordered or None

    async def _llm_call(self, provider_id: str, system_prompt: str, text: str) -> str:
        if not text.strip():
            return ""
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt=system_prompt,
                prompt=text,
            )
            return response.completion_text or ""
        except Exception as exc:
            self.logger.error(f"summary LLM call failed: {exc}")
            return ""

    def _chunk_text(self, text: str, max_chars: int = SUMMARY_CHUNK_CHARS) -> list[str]:
        if len(text) <= max_chars:
            return [text]
        chunks: list[str] = []
        current: list[str] = []
        size = 0
        for line in text.split("\n"):
            if size + len(line) + 1 > max_chars and current:
                chunks.append("\n".join(current))
                current = []
                size = 0
            current.append(line)
            size += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        return chunks

    async def _summarize_text(
        self, provider_id: str, system_prompt: str, text: str
    ) -> str:
        chunks = self._chunk_text(text)
        if len(chunks) <= 1:
            return (await self._llm_call(provider_id, system_prompt, text)).strip()
        partials = []
        for chunk in chunks:
            piece = (await self._llm_call(provider_id, system_prompt, chunk)).strip()
            if piece:
                partials.append(piece)
        if not partials:
            return ""
        merged = "\n".join(partials)
        if len(merged) > SUMMARY_CHUNK_CHARS:
            merged = (await self._llm_call(provider_id, system_prompt, merged)).strip()
        return merged

    async def _summarize(
        self, umo: str, raw_messages: list, prior_summary: str
    ) -> tuple[str, str | None]:
        provider_id = await self._provider_id(umo)
        if not provider_id:
            return "", None
        system_prompt = str(
            self.cfg("summary_instruction", DEFAULT_SUMMARY_INSTRUCTION)
            or DEFAULT_SUMMARY_INSTRUCTION
        )
        transcript = ""
        if raw_messages:
            try:
                transcript = rounds_to_text(split_into_rounds(raw_messages))
            except Exception:
                transcript = json.dumps(raw_messages, ensure_ascii=False)
        parts: list[str] = []
        if prior_summary:
            parts.append("[Existing memory summary]\n" + prior_summary)
        if transcript:
            parts.append("[New transcript]\n" + transcript)
        payload = "\n\n".join(parts)
        if not payload.strip():
            return "", None
        return await self._summarize_text(
            provider_id, system_prompt, payload
        ), provider_id

    async def enforce_limit(
        self, umo: str, conversation_id: str, history: list
    ) -> tuple[list, list]:
        """Drop the oldest turns until the history fits the token ceiling.

        Args:
            umo: Unified message origin.
            conversation_id: Source conversation ID.
            history: The history to trim.

        Returns:
            A tuple of (trimmed_history, dropped_messages).
        """
        max_tokens = self.max_tokens
        if max_tokens <= 0:
            return history, []
        head, body = self._split_leading_system(history)
        kept_prefix: list = []
        rest = body
        if rest and self._is_summary_user(rest[0]):
            prefix_len = 2 if len(rest) > 1 and self._is_ack(rest[1]) else 1
            kept_prefix = rest[:prefix_len]
            rest = rest[prefix_len:]
        dropped: list = []
        while rest and self.count_tokens(head + kept_prefix + rest) > max_tokens:
            boundary = 1
            while boundary < len(rest) and not (
                isinstance(rest[boundary], dict)
                and rest[boundary].get("role") == "user"
            ):
                boundary += 1
            dropped.extend(rest[:boundary])
            rest = rest[boundary:]
        if kept_prefix and self.count_tokens(head + kept_prefix + rest) > max_tokens:
            dropped.extend(kept_prefix)
            kept_prefix = []
        result = head + kept_prefix + rest
        if dropped and bool(self.cfg("archive_enable", True)):
            try:
                await self.store.archive_messages(umo, conversation_id, dropped)
            except Exception as exc:
                self.logger.warning(f"archive on truncate failed: {exc}")
        return result, dropped

    async def compress_session(
        self, umo: str, reason: str = "incremental", force: bool = False
    ) -> dict:
        """Compress one session: fold older turns into the rolling summary.

        Args:
            umo: Unified message origin.
            reason: Trigger source, ``incremental`` / ``global`` / ``emergency`` / ``manual``.
            force: Re-summarize even when only a previous summary exists.

        Returns:
            A result dictionary describing what happened.
        """
        if not bool(self.cfg("enable", True)):
            return {"skipped": "disabled"}
        if reason != "manual" and not self.in_scope(umo):
            return {"skipped": "out_of_scope"}
        conv_mgr = self.context.conversation_manager
        conversation_id = await conv_mgr.get_curr_conversation_id(umo)
        if not conversation_id:
            return {"skipped": "no_conversation"}
        async with session_lock_manager.acquire_lock(umo):
            conversation = await conv_mgr.get_conversation(umo, conversation_id)
            if not conversation:
                return {"skipped": "no_conversation"}
            history = json.loads(conversation.history or "[]")
            head, body = self._split_leading_system(history)
            prior_summaries, body = self._strip_summary_pairs(body)
            split = self._split_index(body, int(self.cfg("keep_recent_turns", 6)))
            old = body[:split]
            recent = body[split:]
            raw_old = [
                message
                for message in old
                if isinstance(message, dict) and message.get("role") != "_checkpoint"
            ]
            prior_text = "\n".join(item for item in prior_summaries if item).strip()
            if not raw_old and not (force and prior_text):
                return {"skipped": "nothing_to_compress"}
            summary, model_used = await self._summarize(umo, raw_old, prior_text)
            if not summary:
                return {"skipped": "summary_failed"}
            if bool(self.cfg("archive_enable", True)) and raw_old:
                try:
                    await self.store.archive_messages(umo, conversation_id, raw_old)
                except Exception as exc:
                    self.logger.warning(f"archive failed: {exc}")
            timestamp = now_ts()
            new_history = list(head)
            summary_tokens = 0
            if summary:
                # Daily memory lives only in the database and is retrieved on
                # demand; it is deliberately NOT written back into the context.
                summary_tokens = self.count_tokens(
                    [{"role": "user", "content": summary}]
                )
                try:
                    embedding = await self._embed(summary)
                    await self.store.upsert_segment(
                        umo=umo,
                        session_type=self.session_type(umo),
                        tier=1,
                        period_key=self._day_key(),
                        seg_start_ts=0,
                        seg_end_ts=timestamp,
                        summary=summary,
                        summary_tokens=summary_tokens,
                        msg_count=len(raw_old),
                        model_used=model_used,
                        ttl_days=int(self.cfg("summary_ttl_days", 3)),
                        embedding=embedding,
                    )
                except Exception as exc:
                    self.logger.error(f"persist segment failed: {exc}", exc_info=True)
                # The rolling long-term memory is what stays in the context.
                await self._refresh_long_term(umo, summary, model_used)
            new_history.extend(recent)
            trimmed, _ = await self.enforce_limit(umo, conversation_id, new_history)
            await conv_mgr.update_conversation(
                umo,
                conversation_id,
                history=trimmed,
                token_usage=self.count_tokens(trimmed),
            )
            await self.store.upsert_cursor(umo, last_compress_at=timestamp)
            return {
                "compressed": bool(summary),
                "messages": len(raw_old),
                "summary_tokens": summary_tokens,
            }

    async def _refresh_long_term(
        self, umo: str, daily_summary: str, model_used: str | None
    ) -> None:
        """Merge a fresh daily summary into the session's long-term memory.

        The long-term row is a single rolling record per session that keeps
        accumulating durable facts; it is what gets injected into the context.

        Args:
            umo: Unified message origin.
            daily_summary: The daily summary just produced.
            model_used: Provider ID used for the merge.
        """
        if not bool(self.cfg("long_term_enable", True)):
            return
        existing = await self._get_long_term_summary(umo)
        provider_id = model_used or await self._provider_id(umo)
        combined = daily_summary
        if provider_id and existing:
            merged = await self._summarize_text(
                provider_id,
                DEFAULT_LONG_TERM_MERGE_INSTRUCTION,
                f"[Existing long-term memory]\n{existing}\n\n"
                f"[New daily memory]\n{daily_summary}",
            )
            if merged:
                combined = merged
        elif existing:
            combined = f"{existing}\n{daily_summary}"
        max_chars = int(self.cfg("long_term_max_chars", 4000) or 4000)
        if len(combined) > max_chars:
            combined = combined[-max_chars:]
        try:
            await self.store.upsert_segment(
                umo=umo,
                session_type=self.session_type(umo),
                tier=2,
                period_key="__long_term__",
                seg_start_ts=0,
                seg_end_ts=now_ts(),
                summary=combined,
                summary_tokens=self.count_tokens(
                    [{"role": "user", "content": combined}]
                ),
                msg_count=0,
                model_used=provider_id,
                ttl_days=int(self.cfg("long_term_ttl_days", 30)),
                embedding=await self._embed(combined),
            )
        except Exception as exc:
            self.logger.error(f"persist long-term failed: {exc}", exc_info=True)

    async def _get_long_term_summary(self, umo: str) -> str:
        """Return the rolling long-term summary for a session.

        Args:
            umo: Unified message origin.

        Returns:
            The long-term summary text, or an empty string.
        """
        try:
            rows, _ = await self.store.list_segments(
                tier=2, umo=umo, page=1, page_size=10
            )
        except Exception:
            return ""
        for row in rows:
            if row.summary:
                return row.summary
        return ""

    async def inject_long_term(self, event: Any, req: Any) -> None:
        """Inject the session's long-term memory into the system prompt.

        Args:
            event: The message event.
            req: The ``ProviderRequest`` about to be sent.
        """
        if not bool(self.cfg("enable", True)) or not bool(
            self.cfg("long_term_inject_enable", True)
        ):
            return
        umo = event.unified_msg_origin
        if not self.in_scope(umo):
            return
        try:
            summary = await self._get_long_term_summary(umo)
        except Exception:
            return
        if not summary:
            return
        budget = int(self.cfg("long_term_inject_tokens", 1500) or 1500)
        if budget > 0:
            summary = self._truncate_to_tokens(summary, budget)
        block = (
            "<long_term_memory>\n"
            "以下内容是你对该会话的长期记忆，请自然地作为背景知识使用，不要逐条复述。\n"
            f"{summary}\n"
            "</long_term_memory>"
        )
        existing = getattr(req, "system_prompt", None) or ""
        if "<long_term_memory>" in existing:
            return
        req.system_prompt = f"{existing}\n\n{block}" if existing else block

    def _truncate_to_tokens(self, text: str, budget: int) -> str:
        """Truncate text so its estimated token count stays within a budget.

        Args:
            text: The source text.
            budget: Maximum estimated tokens.

        Returns:
            The possibly truncated text.
        """
        if (
            budget <= 0
            or self.count_tokens([{"role": "user", "content": text}]) <= budget
        ):
            return text
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if self.count_tokens([{"role": "user", "content": text[:mid]}]) <= budget:
                low = mid
            else:
                high = mid - 1
        return text[:low]

    async def search_daily(
        self, umo: str | None, query: str, date: str = "", limit: int = 5
    ) -> list:
        """Search only daily (tier-1) memories.

        Args:
            umo: Optional session filter; None searches every session.
            query: Natural language query.
            date: Optional ``YYYY-MM-DD`` period filter.
            limit: Maximum number of records to return.

        Returns:
            Matching daily memory segments.
        """
        records = await self.search(umo, query, limit=max(limit * 3, limit))
        daily = [row for row in records if row.tier == 1]
        if date:
            filtered = [row for row in daily if row.period_key == date]
            if filtered:
                daily = filtered
        return daily[:limit]

    async def guard_request(self, event: Any, req: Any) -> None:
        """Guard an outgoing LLM request against the token ceiling.

        Args:
            event: The message event.
            req: The ``ProviderRequest`` about to be sent.
        """
        if not bool(self.cfg("enable", True)):
            return
        umo = event.unified_msg_origin
        if not self.in_scope(umo):
            return
        max_tokens = self.max_tokens
        if max_tokens <= 0:
            return
        history = list(req.contexts or [])
        if not history:
            return
        threshold = max_tokens * float(self.cfg("emergency_threshold_ratio", 0.9))
        if self.count_tokens(history) < threshold:
            return
        window = int(self.cfg("defer_if_offpeak_within_minutes", 30)) * 60
        remaining = self.seconds_until_offpeak()
        if 0 < remaining <= window:
            self.logger.info(
                f"context near limit for {umo}, deferring compression to off-peak."
            )
            await self._reload_and_enforce(umo, req)
            return
        self.logger.info(f"emergency compression triggered for {umo}")
        await self.compress_session(umo, reason="emergency")
        await self._reload_and_enforce(umo, req)

    async def _reload_and_enforce(self, umo: str, req: Any) -> None:
        conv_mgr = self.context.conversation_manager
        conversation_id = await conv_mgr.get_curr_conversation_id(umo)
        if not conversation_id:
            return
        conversation = await conv_mgr.get_conversation(umo, conversation_id)
        if not conversation:
            return
        history = json.loads(conversation.history or "[]")
        trimmed, dropped = await self.enforce_limit(umo, conversation_id, history)
        if dropped:
            await conv_mgr.update_conversation(
                umo,
                conversation_id,
                history=trimmed,
                token_usage=self.count_tokens(trimmed),
            )
        req.contexts = trimmed

    async def run_incremental(self, force: bool = False) -> dict:
        """Run one incremental compression pass across every in-scope session.

        Args:
            force: Re-summarize sessions even without new messages.

        Returns:
            A summary dictionary with processed/compressed counts.
        """
        if not bool(self.cfg("enable", True)):
            return {"processed": 0, "compressed": 0, "total": 0}
        umos = await self.store.list_session_umos()
        processed = 0
        compressed = 0
        for umo in umos:
            if not self.in_scope(umo):
                continue
            try:
                result = await self.compress_session(
                    umo, reason="incremental", force=force
                )
                if result.get("compressed"):
                    compressed += 1
                    processed += 1
                elif force:
                    processed += 1
            except Exception as exc:
                self.logger.error(f"compress {umo} failed: {exc}", exc_info=True)
                try:
                    await self.store.upsert_cursor(umo, last_error=str(exc))
                except Exception:
                    pass
        return {"processed": processed, "compressed": compressed, "total": len(umos)}

    async def run_global(self) -> dict:
        """Run the daily off-peak pass: global compression plus purging.

        Returns:
            A summary dictionary.
        """
        result = await self.run_incremental(force=True)
        purged = await self.store.purge_expired()
        timestamp = now_ts()
        for umo in await self.store.list_session_umos():
            try:
                await self.store.upsert_cursor(umo, last_global_at=timestamp)
            except Exception:
                pass
        return {"incremental": result, "purged": purged}

    async def run_weekly(self) -> dict:
        """Rebuild each session's rolling long-term memory from its dailies.

        Returns:
            A summary dictionary with the number of sessions updated.
        """
        if not bool(self.cfg("enable", True)):
            return {"weekly": 0}
        ttl = int(self.cfg("long_term_ttl_days", 30))
        timestamp = now_ts()
        since = timestamp - 7 * 86400
        updated = 0
        for umo in await self.store.list_session_umos():
            if not self.in_scope(umo):
                continue
            try:
                rows, _ = await self.store.list_segments(
                    tier=1, umo=umo, page=1, page_size=500
                )
                actual = [
                    row
                    for row in rows
                    if row.summary and (row.updated_at or 0) >= since
                ]
                if not actual:
                    continue
                provider_id = await self._provider_id(umo)
                if not provider_id:
                    continue
                existing = await self._get_long_term_summary(umo)
                parts = []
                if existing:
                    parts.append(f"[Existing long-term memory]\n{existing}")
                parts.append(
                    "[Recent daily memories]\n"
                    + "\n\n".join(f"[{row.period_key}] {row.summary}" for row in actual)
                )
                weekly = await self._summarize_text(
                    provider_id,
                    DEFAULT_WEEKLY_INSTRUCTION,
                    "\n\n".join(parts),
                )
                if not weekly:
                    continue
                max_chars = int(self.cfg("long_term_max_chars", 4000) or 4000)
                if len(weekly) > max_chars:
                    weekly = weekly[-max_chars:]
                embedding = await self._embed(weekly)
                await self.store.upsert_segment(
                    umo=umo,
                    session_type=self.session_type(umo),
                    tier=2,
                    period_key="__long_term__",
                    seg_start_ts=since,
                    seg_end_ts=timestamp,
                    summary=weekly,
                    summary_tokens=self.count_tokens(
                        [{"role": "user", "content": weekly}]
                    ),
                    msg_count=len(actual),
                    model_used=provider_id,
                    ttl_days=ttl,
                    embedding=embedding,
                )
                updated += 1
            except Exception as exc:
                self.logger.error(f"weekly memory {umo} failed: {exc}", exc_info=True)
        return {"weekly": updated}

    async def search(self, umo: str | None, query: str, limit: int = 5) -> list:
        """Search compressed memories by keyword with optional vector reranking.

        Args:
            umo: Optional session filter; None searches every session.
            query: Natural language query.
            limit: Maximum number of records to return.

        Returns:
            The most relevant memory segments.
        """
        text = (query or "").strip()
        if not text:
            return []
        if umo:
            rows, _ = await self.store.list_segments(umo=umo, page=1, page_size=300)
        else:
            rows, _ = await self.store.list_segments(page=1, page_size=300)
        if not rows:
            return []
        keyword = text.lower()
        matched = [row for row in rows if keyword in (row.summary or "").lower()]
        candidates = matched or rows

        query_vector = await self._embed(text)
        if query_vector:
            scored = []
            for row in candidates:
                if row.embedding:
                    score = self._cosine(query_vector, row.embedding)
                    if score is not None:
                        scored.append((score, row))
            if scored:
                scored.sort(key=lambda item: item[0], reverse=True)
                candidates = [row for _, row in scored]

        pool = candidates[: max(limit * 4, limit)]
        order = await self._rerank(text, [row.summary or "" for row in pool])
        if order:
            pool = [pool[index] for index in order if index < len(pool)]
        return pool[:limit]

    async def recover(self, umo: str) -> int:
        """Restore archived raw messages back into a session.

        Args:
            umo: Unified message origin.

        Returns:
            The number of restored messages.
        """
        conv_mgr = self.context.conversation_manager
        conversation_id = await conv_mgr.get_curr_conversation_id(umo)
        if not conversation_id:
            return 0
        rows = await self.store.pop_archive(umo)
        payloads = [row.payload for row in rows if isinstance(row.payload, dict)]
        if not payloads:
            return 0
        async with session_lock_manager.acquire_lock(umo):
            conversation = await conv_mgr.get_conversation(umo, conversation_id)
            if not conversation:
                return 0
            history = json.loads(conversation.history or "[]")
            head, body = self._split_leading_system(history)
            marker = {
                "role": "user",
                "content": "[MemoryCtx] Restored raw messages from the cold archive.",
            }
            restored = head + [marker] + payloads + body
            trimmed, _ = await self.enforce_limit(umo, conversation_id, restored)
            await conv_mgr.update_conversation(umo, conversation_id, history=trimmed)
        return len(payloads)
