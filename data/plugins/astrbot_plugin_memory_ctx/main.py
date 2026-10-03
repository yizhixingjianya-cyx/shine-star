"""Memory compression and context management plugin for AstrBot."""

from __future__ import annotations

import json
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.api.web import request

from .memory import MemoryEngine
from .storage import MemoryStore

PLUGIN_NAME = "astrbot_plugin_memory_ctx"
ROUTE_PREFIX = f"/{PLUGIN_NAME}"


class MemoryCtxPlugin(Star):
    """Scheduled memory compression with a hard context token ceiling."""

    def __init__(self, context: Context, config: Any = None) -> None:
        """Initialize the plugin.

        Args:
            context: The AstrBot plugin context.
            config: Plugin configuration mapping from ``_conf_schema.json``.
        """
        super().__init__(context)
        self.config = config if config is not None else {}
        self.store = MemoryStore(self.context.get_db())
        self.engine = MemoryEngine(self.context, self.config, self.store, self.logger)
        self._jobs: list[Any] = []

    async def initialize(self) -> None:
        """Create tables, register web APIs and schedule cron jobs."""
        try:
            missing = await self.store.ensure_tables()
        except Exception as exc:
            self.logger.error(
                "failed to create memory tables; long-term memory injection "
                f"will be skipped until this is fixed: {exc}",
                exc_info=True,
            )
        else:
            if missing:
                self.logger.error(
                    "memory tables are still missing after creation attempt: "
                    f"{missing}; long-term memory injection will be skipped."
                )
            else:
                self.logger.info("memory tables are ready.")
        self._register_web_apis()
        if not bool(self.config.get("enable", True)):
            self.logger.info("memory_ctx compression disabled by configuration.")
            return
        await self._clear_stale_crons()
        await self._register_crons()

    async def _clear_stale_crons(self) -> None:
        """Delete cron records left behind by previous plugin loads.

        The scheduler persists basic jobs even when ``persistent=False``, so a
        reload would otherwise accumulate duplicate jobs and double-fire.
        """
        try:
            jobs = await self.context.cron_manager.list_jobs()
        except Exception as exc:
            self.logger.warning(f"failed to list cron jobs for cleanup: {exc}")
            return
        for job in jobs:
            if job.name.startswith(f"{PLUGIN_NAME}_"):
                try:
                    await self.context.cron_manager.delete_job(job.job_id)
                except Exception:
                    pass

    async def terminate(self) -> None:
        """Remove the cron jobs registered by this plugin."""
        for job in self._jobs:
            try:
                await self.context.cron_manager.delete_job(job.job_id)
            except Exception:
                pass
        self._jobs.clear()

    def _register_web_apis(self) -> None:
        routes = [
            ("/memories", self._api_list_memories, ["GET"], "List memories"),
            ("/memory/stats", self._api_stats, ["GET"], "Memory statistics"),
            ("/memory/compress", self._api_compress, ["POST"], "Compress now"),
            ("/memory/recover", self._api_recover, ["POST"], "Recover archive"),
            ("/sessions", self._api_sessions, ["GET"], "List memory sessions"),
            (
                "/memory/delete",
                self._api_delete_memory,
                ["POST"],
                "Delete session memory",
            ),
            (
                "/memory/delete-all",
                self._api_delete_all,
                ["POST"],
                "Delete all memory",
            ),
            ("/providers", self._api_providers, ["GET"], "List providers"),
            ("/config", self._api_get_config, ["GET"], "Get plugin config"),
            ("/config", self._api_set_config, ["POST"], "Update plugin config"),
            ("/database", self._api_database, ["GET"], "Database status"),
            ("/database/test", self._api_database_test, ["POST"], "Test database"),
        ]
        for path, handler, methods, desc in routes:
            try:
                self.context.register_web_api(
                    f"{ROUTE_PREFIX}{path}", handler, methods, desc
                )
            except Exception as exc:
                self.logger.error(f"failed to register {path}: {exc}", exc_info=True)

    async def _register_crons(self) -> None:
        cron_manager = self.context.cron_manager
        timezone = str(self.config.get("timezone", "Asia/Shanghai"))
        jobs = [
            (
                "incremental",
                str(self.config.get("incremental_cron", "0 */2 * * *")),
                self._job_incremental,
            ),
            (
                "global",
                str(self.config.get("global_cron", "30 0 * * *")),
                self._job_global,
            ),
            (
                "weekly",
                str(self.config.get("weekly_cron", "0 1 * * 1")),
                self._job_weekly,
            ),
        ]
        for name, expression, handler in jobs:
            try:
                job = await cron_manager.add_basic_job(
                    name=f"{PLUGIN_NAME}_{name}",
                    cron_expression=expression,
                    handler=handler,
                    description=f"{PLUGIN_NAME} {name} task",
                    timezone=timezone,
                    persistent=False,
                )
                self._jobs.append(job)
            except Exception as exc:
                self.logger.error(
                    f"failed to register cron {name}: {exc}", exc_info=True
                )

    async def _job_incremental(self) -> None:
        await self.engine.run_incremental()

    async def _job_global(self) -> None:
        await self.engine.run_global()

    async def _job_weekly(self) -> None:
        await self.engine.run_weekly()

    @filter.on_llm_request()
    async def guard_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Inject long-term memory and enforce the token ceiling.

        Args:
            event: The message event.
            req: The outgoing provider request.
        """
        try:
            await self.engine.inject_long_term(event, req)
        except Exception as exc:
            self.logger.error(f"long-term injection failed: {exc}", exc_info=True)
        try:
            await self.engine.guard_request(event, req)
        except Exception as exc:
            self.logger.error(f"context guard failed: {exc}", exc_info=True)

    @filter.llm_tool(name="search_memory")
    async def search_memory(
        self, event: AstrMessageEvent, query: str, scope: str = "session"
    ) -> str:
        """Search the assistant's compressed long-term memories.

        Args:
            query(string): Natural language keywords describing what to recall.
            scope(string): Search scope, "session" for the current session only, "all" for every session.
        """
        if not bool(self.config.get("memory_tool_enable", True)):
            return "Memory search is disabled."
        umo = None if str(scope).strip().lower() == "all" else event.unified_msg_origin
        limit = int(self.config.get("tool_result_limit", 5) or 5)
        try:
            records = await self.engine.search(umo, query, limit)
        except Exception as exc:
            self.logger.error(f"memory search failed: {exc}", exc_info=True)
            return "Memory search failed."
        if not records:
            return "No matching memory found."
        lines = []
        for record in records:
            period = record.period_key or "unknown"
            tier_name = "long-term" if record.tier == 2 else "daily"
            lines.append(f"[{period} | {tier_name}] {record.summary}")
        return "\n\n".join(lines)

    @filter.llm_tool(name="search_daily_memory")
    async def search_daily_memory(
        self, event: AstrMessageEvent, query: str, date: str = ""
    ) -> str:
        """Search the daily memories of the current chat, which are not loaded automatically.

        Args:
            query(string): Natural language keywords describing what to recall.
            date(string): Optional day filter in YYYY-MM-DD format, e.g. 2026-10-03.
        """
        if not bool(self.config.get("daily_memory_tool_enable", True)):
            return "Daily memory search is disabled."
        limit = int(self.config.get("tool_result_limit", 5) or 5)
        try:
            records = await self.engine.search_daily(
                event.unified_msg_origin, query, date=date.strip(), limit=limit
            )
        except Exception as exc:
            self.logger.error(f"daily memory search failed: {exc}", exc_info=True)
            return "Daily memory search failed."
        if not records:
            return "No matching daily memory found."
        lines = [f"[{row.period_key}] {row.summary}" for row in records]
        return "\n\n".join(lines)

    @filter.command("记忆状态")
    async def memory_status(self, event: AstrMessageEvent):
        """Show the compression status of the current session."""
        umo = event.unified_msg_origin
        stats = await self.store.stats(umo)
        lines = [
            f"当前会话 umo：{umo}",
            f"当前会话记忆：每日记忆 {stats.get('tier1', 0)} 条，"
            f"长期记忆 {stats.get('tier2', 0)} 条。",
        ]
        other = [
            row
            for row in await self.store.list_umo_summaries()
            if row.get("umo") != umo and (row.get("daily") or row.get("long_term"))
        ]
        if other:
            lines.append("其它已有记忆的会话（umo / 每日 / 长期）：")
            lines.extend(
                f"- {row['umo']} / {row.get('daily', 0)} / {row.get('long_term', 0)}"
                for row in other[:5]
            )
        else:
            lines.append("其它会话也没有记忆。")
        yield event.plain_result("\n".join(lines))

    @filter.command("记忆压缩")
    async def memory_compress(self, event: AstrMessageEvent):
        """Trigger compression for the current session immediately."""
        umo = event.unified_msg_origin
        try:
            result = await self.engine.compress_session(
                umo, reason="manual", force=True
            )
        except Exception as exc:
            yield event.plain_result(f"压缩失败：{exc}")
            return
        yield event.plain_result(f"压缩结果（{umo}）：{result}")

    @filter.command("记忆清理")
    async def memory_purge(self, event: AstrMessageEvent):
        """Purge expired memory records."""
        try:
            removed = await self.store.purge_expired()
        except Exception as exc:
            yield event.plain_result(f"清理失败：{exc}")
            return
        yield event.plain_result(f"已清理 {removed} 条过期记忆。")

    # ------------------------------------------------------------------
    # Web APIs
    # ------------------------------------------------------------------

    async def _api_list_memories(self) -> dict:
        tier_raw = request.query.get("tier")
        tier = None
        if tier_raw not in (None, "", "all"):
            try:
                tier = int(tier_raw)
            except (TypeError, ValueError):
                tier = None
        umo = request.query.get("umo") or None
        page = self._int_query("page", 1)
        page_size = min(max(self._int_query("page_size", 20), 1), 100)
        rows, total = await self.store.list_segments(
            tier=tier, umo=umo, page=page, page_size=page_size
        )
        items = [
            {
                "id": row.id,
                "umo": row.umo,
                "session_type": row.session_type,
                "tier": row.tier,
                "period_key": row.period_key,
                "summary": row.summary,
                "summary_tokens": row.summary_tokens,
                "msg_count": row.msg_count,
                "model_used": row.model_used,
                "updated_at": row.updated_at,
                "expires_at": row.expires_at,
            }
            for row in rows
        ]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def _int_query(self, key: str, default: int) -> int:
        try:
            return int(request.query.get(key) or default)
        except (TypeError, ValueError):
            return default

    async def _api_stats(self) -> dict:
        umo = request.query.get("umo") or None
        stats = await self.store.stats(umo)
        return {"stats": stats}

    async def _api_compress(self) -> dict:
        body = await request.json(default={}) or {}
        umo = str(body.get("umo") or "").strip()
        if umo:
            result = await self.engine.compress_session(
                umo, reason="manual", force=True
            )
            return {"result": result}
        result = await self.engine.run_incremental(force=True)
        return {"result": result}

    async def _api_recover(self) -> dict:
        body = await request.json(default={}) or {}
        umo = str(body.get("umo") or "").strip()
        if not umo:
            return {"restored": 0, "error": "umo is required"}
        restored = await self.engine.recover(umo)
        return {"restored": restored}

    async def _api_sessions(self) -> dict:
        """List every known conversation together with its memory counters.

        The previous implementation only returned sessions that already had
        memory rows, so a session that had never been compressed was invisible
        in the panel.
        """
        stats = {row["umo"]: row for row in await self.store.list_umo_summaries()}
        try:
            cursors = await self.store.list_cursors()
        except Exception as exc:
            self.logger.warning(f"list cursors failed: {exc}")
            cursors = {}
        try:
            conversations = await self.context.conversation_manager.get_conversations()
        except Exception as exc:
            self.logger.warning(f"list conversations failed: {exc}")
            conversations = []
        items: list[dict] = []
        seen: set[str] = set()
        for conv in conversations:
            umo = str(getattr(conv, "user_id", "") or "")
            if not umo or umo in seen:
                continue
            seen.add(umo)
            try:
                content = json.loads(getattr(conv, "content", "") or "[]")
            except Exception:
                content = []
            stat = stats.get(umo, {})
            cursor = cursors.get(umo, {})
            items.append(
                {
                    "umo": umo,
                    "platform_id": str(getattr(conv, "platform_id", "") or ""),
                    "session_type": stat.get("session_type")
                    or self.engine.session_type(umo),
                    "messages": len(content) if isinstance(content, list) else 0,
                    "daily": int(stat.get("daily", 0) or 0),
                    "long_term": int(stat.get("long_term", 0) or 0),
                    "latest": int(stat.get("latest", 0) or 0),
                    "last_compress_at": int(cursor.get("last_compress_at", 0) or 0),
                    "last_error": cursor.get("last_error") or "",
                }
            )
        for umo, stat in stats.items():
            if umo in seen:
                continue
            cursor = cursors.get(umo, {})
            items.append(
                {
                    "umo": umo,
                    "platform_id": "",
                    "session_type": stat.get("session_type") or "",
                    "messages": 0,
                    "daily": int(stat.get("daily", 0) or 0),
                    "long_term": int(stat.get("long_term", 0) or 0),
                    "latest": int(stat.get("latest", 0) or 0),
                    "last_compress_at": int(cursor.get("last_compress_at", 0) or 0),
                    "last_error": cursor.get("last_error") or "",
                }
            )
        items.sort(
            key=lambda row: max(
                int(row.get("latest", 0) or 0),
                int(row.get("last_compress_at", 0) or 0),
            ),
            reverse=True,
        )
        return {"items": items}

    async def _api_delete_memory(self) -> dict:
        body = await request.json(default={}) or {}
        umo = str(body.get("umo") or "").strip()
        if not umo:
            return {"error": "umo is required"}
        scope = str(body.get("scope") or "all").strip().lower()
        deleted = {"daily": 0, "long_term": 0, "archive": 0, "cursor": 0}
        if scope in ("all", "daily"):
            deleted["daily"] = await self.store.delete_segments(umo=umo, tier=1)
        if scope in ("all", "long_term"):
            deleted["long_term"] = await self.store.delete_segments(umo=umo, tier=2)
        if scope in ("all", "archive"):
            deleted["archive"] = await self.store.delete_archives(umo=umo)
        if scope == "all":
            deleted["cursor"] = await self.store.reset_cursors(umo=umo)
        return {"deleted": deleted}

    async def _api_delete_all(self) -> dict:
        body = await request.json(default={}) or {}
        scope = str(body.get("scope") or "all").strip().lower()
        deleted = {"daily": 0, "long_term": 0, "archive": 0, "cursor": 0}
        if scope in ("all", "daily"):
            deleted["daily"] = await self.store.delete_segments(tier=1)
        if scope in ("all", "long_term"):
            deleted["long_term"] = await self.store.delete_segments(tier=2)
        if scope in ("all", "archive"):
            deleted["archive"] = await self.store.delete_archives()
        if scope == "all":
            deleted["cursor"] = await self.store.reset_cursors()
        return {"deleted": deleted}

    def _provider_options(self, providers: list[Any]) -> list[dict]:
        options = []
        for provider in providers or []:
            try:
                meta = provider.meta()
                pid = getattr(meta, "id", "") or ""
                model = getattr(meta, "model", "") or ""
            except Exception:
                continue
            if not pid:
                continue
            options.append({"id": pid, "model": model, "label": f"{pid} ({model})"})
        return options

    async def _api_providers(self) -> dict:
        chat: list[dict] = []
        embedding: list[dict] = []
        rerank: list[dict] = []
        try:
            chat = self._provider_options(self.context.get_all_providers())
        except Exception as exc:
            self.logger.warning(f"list chat providers failed: {exc}")
        try:
            embedding = self._provider_options(
                self.context.get_all_embedding_providers()
            )
        except Exception as exc:
            self.logger.warning(f"list embedding providers failed: {exc}")
        try:
            rerank = self._provider_options(
                self.context.provider_manager.rerank_provider_insts
            )
        except Exception as exc:
            self.logger.warning(f"list rerank providers failed: {exc}")
        return {"chat": chat, "embedding": embedding, "rerank": rerank}

    async def _api_get_config(self) -> dict:
        keys = [
            "enable",
            "compress_provider_id",
            "embedding_provider_id",
            "rerank_provider_id",
            "session_scope",
            "scope_list",
            "keep_recent_turns",
            "summary_ttl_days",
            "long_term_ttl_days",
            "max_context_tokens",
            "emergency_threshold_ratio",
            "offpeak_start",
            "offpeak_end",
            "defer_if_offpeak_within_minutes",
            "incremental_cron",
            "global_cron",
            "weekly_cron",
            "timezone",
            "memory_tool_enable",
            "daily_memory_tool_enable",
            "long_term_enable",
            "long_term_inject_enable",
            "long_term_inject_tokens",
            "long_term_max_chars",
            "group_compress_enable",
            "private_compress_enable",
            "archive_enable",
            "db_type",
            "mysql_host",
            "mysql_port",
            "mysql_user",
            "mysql_database",
            "mysql_charset",
        ]
        return {"config": {key: self.config.get(key) for key in keys}}

    async def _api_set_config(self) -> dict:
        body = await request.json(default={}) or {}
        allowed = {
            "compress_provider_id",
            "embedding_provider_id",
            "rerank_provider_id",
            "keep_recent_turns",
            "summary_ttl_days",
            "long_term_ttl_days",
            "max_context_tokens",
            "emergency_threshold_ratio",
            "memory_tool_enable",
            "daily_memory_tool_enable",
            "long_term_enable",
            "long_term_inject_enable",
            "long_term_inject_tokens",
            "long_term_max_chars",
        }
        int_keys = (
            "keep_recent_turns",
            "summary_ttl_days",
            "long_term_ttl_days",
            "max_context_tokens",
            "long_term_inject_tokens",
            "long_term_max_chars",
        )
        bool_keys = (
            "memory_tool_enable",
            "daily_memory_tool_enable",
            "long_term_enable",
            "long_term_inject_enable",
        )
        changed: dict[str, Any] = {}
        for key, value in body.items():
            if key not in allowed:
                continue
            if key in int_keys:
                value = max(0, int(value))
            elif key == "emergency_threshold_ratio":
                value = min(max(float(value), 0.1), 1.0)
            elif key in bool_keys:
                value = bool(value)
            else:
                value = str(value or "")
            self.config[key] = value
            changed[key] = value
        if not changed:
            return {"updated": 0, "error": "no writable field provided"}
        try:
            save = getattr(self.config, "save_config_async", None)
            if callable(save):
                await save()
            else:
                self.config.save_config()
        except Exception as exc:
            self.logger.warning(f"persist plugin config failed: {exc}")
            return {"updated": len(changed), "changed": changed, "persisted": False}
        return {"updated": len(changed), "changed": changed, "persisted": True}

    async def _api_database(self) -> dict:
        db = self.context.get_db()
        url = str(getattr(db, "DATABASE_URL", "") or "")
        redacted = self._redact_db_url(url)
        counts = {}
        try:
            counts = await self.store.stats()
        except Exception as exc:
            self.logger.warning(f"db stats failed: {exc}")
        return {
            "dialect": "mysql" if url.startswith("mysql") else "sqlite",
            "database_url": redacted,
            "table_prefix": "mem_",
            "tables": ["mem_segment", "mem_raw_archive", "mem_cursor"],
            "counts": counts,
            "configured_type": str(self.config.get("db_type", "follow_core")),
        }

    async def _api_database_test(self) -> dict:
        body = await request.json(default={}) or {}
        host = str(
            body.get("mysql_host") or self.config.get("mysql_host") or ""
        ).strip()
        port = int(body.get("mysql_port") or self.config.get("mysql_port") or 3306)
        user = str(
            body.get("mysql_user") or self.config.get("mysql_user") or ""
        ).strip()
        password = str(
            body.get("mysql_password") or self.config.get("mysql_password") or ""
        )
        database = str(
            body.get("mysql_database") or self.config.get("mysql_database") or ""
        ).strip()
        if not host or not user or not database:
            return {"ok": False, "error": "host / user / database are required"}
        try:
            import asyncio

            import pymysql

            def _probe() -> None:
                conn = pymysql.connect(
                    host=host,
                    port=port,
                    user=user,
                    password=password,
                    database=database,
                    connect_timeout=8,
                )
                try:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT 1")
                        cursor.fetchone()
                finally:
                    conn.close()

            await asyncio.to_thread(_probe)
            return {"ok": True, "message": f"connected to {host}:{port}/{database}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _redact_db_url(self, url: str) -> str:
        if "@" not in url:
            return url
        scheme, _, rest = url.partition("://")
        if not rest:
            return url
        credentials, _, host = rest.rpartition("@")
        user = credentials.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
