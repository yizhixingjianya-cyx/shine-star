"""指令上传器共享基础设施。"""

from __future__ import annotations

import logging

from .qqapi import QQOfficialClient
from .scanner import collect_commands, merge_commands
from .store import UploaderStore

logger = logging.getLogger("astrbot")

__all__ = ["UploaderService", "collect_commands", "merge_commands"]


class UploaderService:
    """聚合存储、指令扫描与 QQ OpenAPI 客户端。"""

    def __init__(self, config: dict | None = None) -> None:
        cfg = dict(config or {})
        self.store = UploaderStore()
        # 配置里的默认凭据（若面板未填，则回退到插件配置）
        self.default_app_id = str(cfg.get("app_id") or "").strip()
        self.default_app_secret = str(cfg.get("app_secret") or "").strip()

    def snapshot(self) -> dict:
        """读取当前状态：凭据（脱敏）+ 合并后的指令清单。"""
        doc = self.store.load()
        commands = merge_commands(doc.get("commands"))
        doc["commands"] = commands
        if not doc.get("app_id"):
            doc["app_id"] = self.default_app_id
        return doc

    def save_selection(self, commands: list[dict]) -> dict:
        """保存用户勾选/编辑后的指令清单。"""
        return self.store.update(commands=commands)

    def save_credentials(
        self, app_id: str, app_secret: str, bot_qq: str = "", sandbox: bool = False
    ) -> dict:
        """保存 QQ 开放平台凭据（不触发任何上传）。"""
        return self.store.update(
            app_id=str(app_id or "").strip(),
            app_secret=str(app_secret or "").strip(),
            bot_qq=str(bot_qq or "").strip(),
            sandbox=bool(sandbox),
        )

    def build_client(self) -> QQOfficialClient:
        """用已保存（或配置默认）的凭据构造客户端。"""
        doc = self.store.load()
        app_id = str(doc.get("app_id") or self.default_app_id)
        app_secret = str(doc.get("app_secret") or self.default_app_secret)
        return QQOfficialClient(app_id, app_secret, sandbox=bool(doc.get("sandbox")))
