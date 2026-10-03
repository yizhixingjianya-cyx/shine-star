"""指令上传器的持久化存储。

把用户在面板里填写的 QQ 开放平台凭据（AppID / AppSecret）与勾选的指令清单
保存为 JSON，落到 AstrBot 的 data 目录下（插件目录会被更新/重装覆盖，不能放那）。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("astrbot")

__all__ = ["UploaderStore"]


def _data_dir() -> Path:
    """取 AstrBot 的 data 目录；失败时回退到插件目录的 .state。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return Path(get_astrbot_data_path())
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parent.parent / ".state"


def _empty_doc() -> dict[str, Any]:
    """默认空文档：凭据 + 全局开关 + 指令清单。"""
    return {
        "version": 1,
        "updated_at": "",
        "app_id": "",
        "app_secret": "",
        "bot_qq": "",
        "sandbox": False,  # 是否走沙箱环境
        "scopes": ["group", "c2c"],
        "commands": [],  # [{name, panel_name, aliases, plugin, desc, scope, block}]
        "last_upload": {},  # 最近一次上传结果
    }


class UploaderStore:
    """指令上传器的 JSON 存储（读写加锁，原子替换）。"""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        if path is None:
            directory = _data_dir() / "cmd_uploader"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "state.json"
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        """读取文档；不存在或损坏时返回空文档。"""
        with self._lock:
            return self._load_locked()

    def _load_locked(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_doc()
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                return _empty_doc()
            base = _empty_doc()
            base.update(doc)
            if not isinstance(base.get("commands"), list):
                base["commands"] = []
            return base
        except Exception as e:  # noqa: BLE001
            logger.warning(f"指令上传器状态解析失败，已重置：{e!r}")
            return _empty_doc()

    def save(self, doc: dict[str, Any]) -> dict[str, Any]:
        """写回文档（更新 updated_at，原子替换）。"""
        with self._lock:
            doc = dict(doc or {})
            doc.setdefault("version", 1)
            doc["updated_at"] = (
                datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
            return doc

    def update(self, **fields: Any) -> dict[str, Any]:
        """只更新部分字段并写回。"""
        doc = self.load()
        doc.update(fields)
        return self.save(doc)
