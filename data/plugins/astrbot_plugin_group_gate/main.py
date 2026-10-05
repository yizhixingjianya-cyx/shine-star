"""Group access gate: registration codes and a manual group whitelist.

New groups must either be whitelisted from the WebUI or redeem a registration
code before the bot will answer them. Every message from an unregistered group
still receives a reminder, and the event is stopped so the AI pipeline never
sees it.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import request

from .storage import GateStore

PLUGIN_NAME = "astrbot_plugin_group_gate"
ROUTE_PREFIX = f"/{PLUGIN_NAME}"

DEFAULT_REMINDER = (
    "⚠️ 本群尚未授权，我暂时不能参与对话。\n"
    "请发送注册码完成授权（格式：注册 <注册码>），或联系管理员在后台把本群加入白名单。"
)
DEFAULT_APPROVED = "✅ 注册码有效，本群已授权，我现在可以参与对话啦。"
DEFAULT_BAD_CODE = "❌ 注册码无效或已用尽，请检查后重新发送，或联系管理员。"
DEFAULT_KEYWORDS = "注册,授权,登记"


class GroupGatePlugin(Star):
    """Gate group chats behind a whitelist or a registration code."""

    def __init__(self, context: Context, config: Any = None) -> None:
        """Initialize the plugin.

        Args:
            context: The AstrBot plugin context.
            config: Plugin configuration mapping from ``_conf_schema.json``.
        """
        super().__init__(context)
        self.config = config if config is not None else {}
        self.store = GateStore(self.context.get_db())

    def cfg(self, key: str, default: Any = None) -> Any:
        """Read a configuration value with a fallback default.

        Args:
            key: Configuration key.
            default: Value returned when the key is missing or None.

        Returns:
            The configured value or the default.
        """
        value = self.config.get(key, default)
        return default if value is None else value

    def enabled(self) -> bool:
        """Return whether the gate is switched on.

        Returns:
            True when the gate should intercept group messages.
        """
        return bool(self.cfg("enable", False))

    async def initialize(self) -> None:
        """Create the plugin tables and register the Web APIs."""
        try:
            missing = await self.store.ensure_tables()
            if missing:
                self.logger.error(
                    f"group gate tables missing after init: {', '.join(missing)}"
                )
            else:
                self.logger.info("group gate tables are ready.")
        except Exception as exc:
            self.logger.error(f"failed to ensure group gate tables: {exc}")
        self._register_web_apis()

    # ------------------------------------------------------------------
    # The gate
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1)
    async def gate_group_message(self, event: AstrMessageEvent):
        """Intercept every message coming from a non-whitelisted group.

        Args:
            event: The incoming group message event.

        Yields:
            A reminder or an approval message for the group.
        """
        if not self.enabled():
            return
        umo = event.unified_msg_origin
        if not umo:
            return
        bot = umo.split(":", 1)[0]
        try:
            if await self.store.is_bot_open(bot):
                return
        except Exception as exc:
            self.logger.error(f"group gate bot lookup failed: {exc}")
        try:
            state = await self.store.is_allowed(umo)
        except Exception as exc:
            self.logger.error(f"group gate lookup failed: {exc}")
            return
        if state is True:
            return
        if state is None:
            # Remember unknown groups so the panel can list them with their
            # name for one-click whitelisting.
            try:
                await self.store.set_allowed(umo, False, source="pending")
            except Exception as exc:
                self.logger.warning(f"record pending group failed: {exc}")

        code = self._extract_code(event.message_str or "")
        if code:
            try:
                redeemed = await self.store.consume_code(code, umo)
            except Exception as exc:
                self.logger.error(f"redeem registration code failed: {exc}")
                redeemed = False
            if redeemed:
                await self.store.set_allowed(
                    umo, True, note=f"注册码 {code}", source="code"
                )
                self.logger.info(f"group approved by registration code: {umo}")
                event.stop_event()
                yield event.plain_result(
                    str(self.cfg("approved_text", DEFAULT_APPROVED))
                )
                return
            event.stop_event()
            yield event.plain_result(str(self.cfg("bad_code_text", DEFAULT_BAD_CODE)))
            return

        self.logger.info(f"blocked message from unregistered group: {umo}")
        event.stop_event()
        yield event.plain_result(str(self.cfg("reminder_text", DEFAULT_REMINDER)))

    def _extract_code(self, text: str) -> str:
        """Extract a candidate registration code from a message.

        Accepts both ``注册 <code>`` style commands and a bare code token.

        Args:
            text: The raw message text.

        Returns:
            The candidate code, or an empty string when none is found.
        """
        text = (text or "").strip()
        if not text:
            return ""
        keywords = [
            item.strip()
            for item in str(self.cfg("register_keywords", DEFAULT_KEYWORDS)).split(",")
            if item.strip()
        ]
        for keyword in keywords:
            for prefix in (f"/{keyword}", f"#{keyword}", f"／{keyword}", keyword):
                if text.startswith(prefix):
                    rest = text[len(prefix) :].strip().lstrip(":：,， ")
                    if rest:
                        return rest.split()[0]
        min_len = int(self.cfg("code_length", 6) or 6)
        if " " not in text and text.isalnum() and len(text) >= min_len:
            return text
        return ""

    # ------------------------------------------------------------------
    # Web APIs
    # ------------------------------------------------------------------
    def _register_web_apis(self) -> None:
        routes = [
            ("/groups", self._api_groups, ["GET"], "List group whitelist"),
            ("/whitelist", self._api_whitelist, ["POST"], "Set group whitelist"),
            (
                "/whitelist-bulk",
                self._api_whitelist_bulk,
                ["POST"],
                "Bulk set group whitelist",
            ),
            (
                "/open-all",
                self._api_open_all,
                ["POST"],
                "Open or gate a whole bot",
            ),
            ("/codes", self._api_list_codes, ["GET"], "List registration codes"),
            ("/codes/create", self._api_create_codes, ["POST"], "Create codes"),
            ("/codes/delete", self._api_delete_code, ["POST"], "Delete a code"),
            ("/config", self._api_get_config, ["GET"], "Get plugin config"),
            ("/config", self._api_set_config, ["POST"], "Update plugin config"),
            (
                "/seed-current",
                self._api_seed_current,
                ["POST"],
                "Whitelist known groups",
            ),
        ]
        for path, handler, methods, desc in routes:
            try:
                self.context.register_web_api(
                    f"{ROUTE_PREFIX}{path}", handler, methods, desc
                )
            except Exception as exc:
                self.logger.error(f"failed to register {path}: {exc}", exc_info=True)

    async def _known_groups(self) -> list[dict]:
        """Collect every known group conversation.

        Returns:
            A list of dicts with the origin, platform and message count.
        """
        try:
            conversations = await self.context.conversation_manager.get_conversations()
        except Exception as exc:
            self.logger.warning(f"list conversations failed: {exc}")
            return []
        items: list[dict] = []
        seen: set[str] = set()
        for conv in conversations:
            umo = str(getattr(conv, "user_id", "") or "")
            if not umo or ":GroupMessage:" not in umo or umo in seen:
                continue
            seen.add(umo)
            try:
                content = getattr(conv, "content", "") or "[]"
                messages = len(json.loads(content))
            except Exception:
                messages = 0
            items.append(
                {
                    "umo": umo,
                    "platform_id": str(getattr(conv, "platform_id", "") or ""),
                    "messages": messages,
                }
            )
        return items

    async def _api_groups(self) -> dict:
        """Return every known group together with its whitelist state."""
        rows = {row.umo: row for row in await self.store.list_groups()}
        known = await self._known_groups()
        platform_types: dict[str, str] = {}
        try:
            core_config = self.context.get_config()
            platforms = core_config.get("platform", []) if core_config else []
        except Exception as exc:
            self.logger.warning(f"read platform config failed: {exc}")
            platforms = []
        for platform in platforms or []:
            pid = str(platform.get("id") or "").strip()
            if pid:
                platform_types[pid] = str(platform.get("type") or "")
        names: dict[str, str] = {}
        umos = {group["umo"] for group in known} | set(rows)
        if umos:
            try:
                aliases = await self.context.get_db().get_umo_aliases(list(umos))
            except Exception as exc:
                self.logger.warning(f"resolve group names failed: {exc}")
            else:
                for alias in aliases:
                    name = (
                        str(alias.user_alias or "").strip()
                        or str(alias.auto_name or "").strip()
                    )
                    if name:
                        names[alias.umo] = name
        items = []
        for group in known:
            row = rows.get(group["umo"])
            items.append(
                {
                    **group,
                    "bot": str(group["umo"]).split(":", 1)[0],
                    "display_name": names.get(group["umo"], group["umo"]),
                    "allowed": bool(row.allowed) if row else False,
                    "known": True,
                    "note": row.note if row else "",
                    "source": row.source if row else "",
                }
            )
        for umo, row in rows.items():
            if any(item["umo"] == umo for item in items):
                continue
            items.append(
                {
                    "umo": umo,
                    "bot": str(umo).split(":", 1)[0],
                    "display_name": names.get(umo, umo),
                    "platform_id": "",
                    "messages": 0,
                    "allowed": bool(row.allowed),
                    "known": False,
                    "note": row.note,
                    "source": row.source,
                }
            )
        bot_ids = sorted(
            {str(item.get("bot") or "") for item in items if item.get("bot")}
            | set(platform_types)
        )
        try:
            open_bots = await self.store.open_bots()
        except Exception as exc:
            self.logger.warning(f"list open bots failed: {exc}")
            open_bots = set()
        bots = [
            {
                "id": pid,
                "label": pid,
                "type": platform_types.get(pid, ""),
                "open_all": pid in open_bots,
            }
            for pid in bot_ids
        ]
        return {"items": items, "bots": bots, "enabled": self.enabled()}

    async def _api_whitelist(self) -> dict:
        """Allow or deny a single group."""
        body = await request.json(default={}) or {}
        umo = str(body.get("umo") or "").strip()
        if not umo:
            return {"error": "umo is required"}
        allowed = bool(body.get("allowed", True))
        note = str(body.get("note") or "")
        await self.store.set_allowed(umo, allowed, note=note, source="manual")
        return {"umo": umo, "allowed": allowed}

    async def _api_whitelist_bulk(self) -> dict:
        """Allow or deny many groups at once (multi-select in the panel)."""
        body = await request.json(default={}) or {}
        umos = body.get("umos") or []
        if not isinstance(umos, list):
            return {"error": "umos must be a list"}
        allowed = bool(body.get("allowed", True))
        note = str(body.get("note") or "")
        updated = 0
        for umo in umos:
            umo = str(umo or "").strip()
            if not umo:
                continue
            await self.store.set_allowed(umo, allowed, note=note, source="manual")
            updated += 1
        return {"updated": updated, "allowed": allowed}

    async def _api_open_all(self) -> dict:
        """Fully open or re-gate a whole bot.

        A fully open bot bypasses the gate entirely: every group of that bot,
        including groups seen later, is allowed without a whitelist entry.
        """
        body = await request.json(default={}) or {}
        bot = str(body.get("bot") or "").strip()
        if not bot:
            return {"error": "bot is required"}
        open_all = bool(body.get("open", True))
        await self.store.set_bot_open(bot, open_all)
        self.logger.info(
            f"group gate {'opened' if open_all else 'closed'} for bot: {bot}"
        )
        return {"bot": bot, "open_all": open_all}

    async def _api_seed_current(self) -> dict:
        """Whitelist every group the bot already knows about."""
        groups = await self._known_groups()
        updated = 0
        for group in groups:
            await self.store.set_allowed(
                group["umo"], True, note="初始化导入", source="manual"
            )
            updated += 1
        return {"updated": updated}

    async def _api_list_codes(self) -> dict:
        """List every registration code."""
        rows = await self.store.list_codes()
        return {
            "items": [
                {
                    "code": row.code,
                    "note": row.note,
                    "max_uses": row.max_uses,
                    "uses": row.uses,
                    "enabled": bool(row.enabled),
                    "created_at": row.created_at,
                    "used_at": row.used_at,
                    "used_by": row.used_by,
                }
                for row in rows
            ]
        }

    async def _api_create_codes(self) -> dict:
        """Generate one or more registration codes."""
        body = await request.json(default={}) or {}
        try:
            count = min(max(int(body.get("count", 1) or 1), 1), 50)
        except (TypeError, ValueError):
            count = 1
        try:
            length = min(max(int(body.get("length", 6) or 6), 4), 32)
        except (TypeError, ValueError):
            length = 6
        try:
            max_uses = max(int(body.get("max_uses", 1) or 1), 0)
        except (TypeError, ValueError):
            max_uses = 1
        note = str(body.get("note") or "")
        created = []
        for _ in range(count):
            code = self._generate_code(length)
            await self.store.create_code(code, note=note, max_uses=max_uses)
            created.append(code)
        return {"created": created}

    def _generate_code(self, length: int) -> str:
        """Generate a human-friendly random code.

        Args:
            length: Number of characters.

        Returns:
            An upper-case alphanumeric code without ambiguous characters.
        """
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "".join(secrets.choice(alphabet) for _ in range(length))

    async def _api_delete_code(self) -> dict:
        """Delete a registration code."""
        body = await request.json(default={}) or {}
        code = str(body.get("code") or "").strip()
        if not code:
            return {"error": "code is required"}
        deleted = await self.store.delete_code(code)
        return {"deleted": deleted}

    async def _api_get_config(self) -> dict:
        """Return the writable plugin configuration."""
        return {
            "enable": self.enabled(),
            "reminder_text": self.cfg("reminder_text", DEFAULT_REMINDER),
            "approved_text": self.cfg("approved_text", DEFAULT_APPROVED),
            "bad_code_text": self.cfg("bad_code_text", DEFAULT_BAD_CODE),
            "register_keywords": self.cfg("register_keywords", DEFAULT_KEYWORDS),
            "code_length": int(self.cfg("code_length", 6) or 6),
        }

    async def _api_set_config(self) -> dict:
        """Persist the writable plugin configuration."""
        body = await request.json(default={}) or {}
        changed: dict[str, Any] = {}
        for key in (
            "reminder_text",
            "approved_text",
            "bad_code_text",
            "register_keywords",
        ):
            if key in body:
                self.config[key] = str(body[key] or "")
                changed[key] = self.config[key]
        if "enable" in body:
            self.config["enable"] = bool(body["enable"])
            changed["enable"] = self.config["enable"]
        if "code_length" in body:
            try:
                self.config["code_length"] = min(max(int(body["code_length"]), 4), 32)
            except (TypeError, ValueError):
                pass
            else:
                changed["code_length"] = self.config["code_length"]
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
