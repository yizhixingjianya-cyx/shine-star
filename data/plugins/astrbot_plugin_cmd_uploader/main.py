"""AstrBot 指令上传面板插件。

参照 CYX BOT 的「指令上传面板」：

1. 读取整个 AstrBot 实例里**所有插件已注册的指令**（含别名、所属插件、描述）；
2. 在 WebUI 提供一块**面板页**，让用户填写 QQ 官方机器人的 AppID（Key）与
   AppSecret（Token），并勾选/编辑要上传的指令与描述；
3. **不主动上传**：只有用户点击面板上的「保存并上传」按钮，才会把选中的指令
   通过 QQ 官方开放平台 OpenAPI 上传为指令面板（快捷菜单指令）。

配置以 JSON 形式持久化在 ``data/cmd_uploader/state.json``，卸载/更新插件不丢。

面板页：WebUI → 插件 → 「指令上传面板」。
"""

from __future__ import annotations

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

from .core import UploaderService
from .core.qqapi import QQApiError

PLUGIN_NAME = "astrbot_plugin_cmd_uploader"
ROUTE_PREFIX = f"/{PLUGIN_NAME}"


class CmdUploaderPlugin(Star):
    """指令上传面板插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = dict(config or {})
        self.service = UploaderService(self.config)

    async def initialize(self) -> None:
        """注册面板页所需的 Web API。"""
        routes = [
            ("/state", self._api_state, ["GET"], "读取凭据与合并后的指令清单"),
            ("/rescan", self._api_rescan, ["POST"], "重新扫描全部指令"),
            ("/commands", self._api_save_commands, ["POST"], "保存勾选/编辑的指令"),
            (
                "/credentials",
                self._api_save_credentials,
                ["POST"],
                "保存 AppID/AppSecret",
            ),
            (
                "/upload",
                self._api_upload,
                ["POST"],
                "上传选中指令到 QQ 平台（手动触发）",
            ),
        ]
        for path, handler, methods, desc in routes:
            try:
                self.context.register_web_api(
                    f"{ROUTE_PREFIX}{path}", handler, methods, desc
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(f"注册 {path} 失败：{exc}", exc_info=True)

    async def terminate(self) -> None:
        """插件卸载时的收尾（无需主动清理）。"""

    # ------------------------------------------------------------ Web API
    async def _api_state(self, **_kwargs):
        """返回当前凭据（脱敏）与合并后的指令清单。"""
        try:
            doc = self.service.snapshot()
        except Exception as e:  # noqa: BLE001
            return error_response(f"读取状态失败：{e}", status_code=500)
        # 脱敏：不回传 AppSecret 明文
        secret = str(doc.get("app_secret") or "")
        return json_response(
            {
                "app_id": doc.get("app_id") or "",
                "has_secret": bool(secret),
                "secret_tail": secret[-4:] if secret else "",
                "bot_qq": doc.get("bot_qq") or "",
                "sandbox": bool(doc.get("sandbox")),
                "updated_at": doc.get("updated_at") or "",
                "last_upload": doc.get("last_upload") or {},
                "commands": doc.get("commands") or [],
                "summary": _summarize(doc.get("commands") or []),
            }
        )

    async def _api_rescan(self, **_kwargs):
        """重新扫描指令并与已保存的编辑合并后回写。"""
        try:
            doc = self.service.snapshot()
            self.service.save_selection(doc.get("commands") or [])
            return json_response(
                {
                    "count": len(doc.get("commands") or []),
                    "commands": doc.get("commands") or [],
                    "summary": _summarize(doc.get("commands") or []),
                }
            )
        except Exception as e:  # noqa: BLE001
            return error_response(f"扫描失败：{e}", status_code=500)

    async def _api_save_commands(self, **_kwargs):
        """保存用户在面板里勾选/编辑的指令清单。"""
        body = await request.json(default={}) or {}
        commands = body.get("commands")
        if not isinstance(commands, list):
            return error_response("缺少 commands 数组")
        try:
            self.service.save_selection(commands)
            return json_response({"ok": True, "count": len(commands)})
        except Exception as e:  # noqa: BLE001
            return error_response(f"保存失败：{e}", status_code=500)

    async def _api_save_credentials(self, **_kwargs):
        """保存 AppID / AppSecret / 机器人 QQ / 沙箱开关（不触发上传）。"""
        body = await request.json(default={}) or {}
        app_id = str(body.get("app_id") or "").strip()
        app_secret = str(body.get("app_secret") or "").strip()
        doc = self.service.store.load()
        # 允许「只改其它字段、不重填密钥」：app_secret 为空时沿用旧值
        if not app_secret:
            app_secret = str(doc.get("app_secret") or "")
        try:
            self.service.save_credentials(
                app_id,
                app_secret,
                str(body.get("bot_qq") or ""),
                bool(body.get("sandbox")),
            )
        except Exception as e:  # noqa: BLE001
            return error_response(f"保存失败：{e}", status_code=500)
        return json_response({"ok": True, "has_secret": bool(app_secret)})

    async def _api_upload(self, **_kwargs):
        """手动上传：把当前勾选的指令推送到 QQ 平台。"""
        body = await request.json(default={}) or {}

        # 允许本次携带新凭据（先保存再上传）
        if body.get("app_id") is not None or body.get("app_secret"):
            doc = self.service.store.load()
            self.service.save_credentials(
                str(body.get("app_id") or doc.get("app_id") or ""),
                str(body.get("app_secret") or doc.get("app_secret") or ""),
                str(body.get("bot_qq") or doc.get("bot_qq") or ""),
                bool(body.get("sandbox", doc.get("sandbox"))),
            )

        doc = self.service.snapshot()
        commands = doc.get("commands") or []
        scopes = doc.get("scopes") or ["group", "c2c"]

        client = self.service.build_client()
        if not client.configured():
            return error_response("请先填写 AppID 与 AppSecret 再上传")

        results: list[dict] = []
        errors: list[str] = []
        for scope in scopes:
            try:
                stat = await client.upload_scope(scope, commands)
                results.append(stat)
            except QQApiError as e:
                errors.append(f"{scope}: {e}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{scope}: {type(e).__name__}: {e}")

        record = {
            "ok": not errors,
            "results": results,
            "errors": errors,
            "scopes": scopes,
        }
        self.service.store.update(last_upload=record)

        if errors and not results:
            return error_response("；".join(errors), status_code=502, data=record)
        return json_response(record)


def _summarize(commands: list[dict]) -> dict:
    """统计指令清单：总数 / 屏蔽数 / 启用数 / 按插件分组。"""
    cmds = [c for c in commands if isinstance(c, dict) and c.get("name")]
    by_plugin: dict[str, int] = {}
    for c in cmds:
        key = str(c.get("plugin") or "未知插件")
        by_plugin[key] = by_plugin.get(key, 0) + 1
    blocked = sum(1 for c in cmds if c.get("block"))
    return {
        "total": len(cmds),
        "blocked": blocked,
        "enabled": len(cmds) - blocked,
        "with_aliases": sum(1 for c in cmds if c.get("aliases")),
        "by_plugin": dict(sorted(by_plugin.items(), key=lambda kv: -kv[1])),
    }
