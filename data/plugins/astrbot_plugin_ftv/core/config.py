"""插件配置读取。

配置项在 ``_conf_schema.json`` 中声明，由 AstrBot 持久化到
``data/config/astrbot_plugin_ftv_config.json``。
"""

from __future__ import annotations

import os
from typing import Any

__all__ = ["FtvSettings"]


def _clean(value: Any) -> str:
    """剥掉手写配置里常见的反引号 / 引号 / 空格。"""
    text = str(value or "").strip()
    for ch in ("`", "'", '"'):
        text = text.strip(ch)
    return text.strip()


class FtvSettings:
    """插件配置聚合对象，同时保留环境变量作为回退。"""

    def __init__(self, config: dict | None = None) -> None:
        cfg = config or {}
        self.base_url = _clean(
            cfg.get("ftv_base_url")
            or os.getenv("FTV_BASE_URL")
            or "https://open-cn1.vdsentnet.com"
        ).rstrip("/")
        self.app_id = _clean(cfg.get("ftv_app_id") or os.getenv("FTV_APP_ID"))
        self.client_secret = _clean(
            cfg.get("ftv_client_secret") or os.getenv("FTV_CLIENT_SECRET")
        )
        # 本地图床
        self.image_host_url = _clean(
            cfg.get("image_host_url") or os.getenv("FTV_IMAGE_HOST_URL")
        ).rstrip("/")
        try:
            self.image_host_port = int(
                cfg.get("image_host_port") or os.getenv("FTV_IMAGE_HOST_PORT") or 11451
            )
        except (TypeError, ValueError):
            self.image_host_port = 11451
        self.image_host_bind = _clean(
            cfg.get("image_host_bind") or os.getenv("FTV_IMAGE_HOST_BIND") or "0.0.0.0"
        )
        try:
            self.image_ttl_hours = float(
                cfg.get("image_ttl_hours") or os.getenv("FTV_IMAGE_TTL_HOURS") or 24
            )
        except (TypeError, ValueError):
            self.image_ttl_hours = 24.0
        self.help_text = str(cfg.get("help_text") or "").strip()
