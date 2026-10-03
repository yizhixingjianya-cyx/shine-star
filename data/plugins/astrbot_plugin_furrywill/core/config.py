"""每日鉴毛插件配置。"""

from __future__ import annotations

import os

__all__ = ["FurryWillSettings"]


def _clean(value) -> str:
    """剥掉手写配置里常见的反引号 / 引号 / 空格。"""
    text = str(value or "").strip()
    for ch in ("`", "'", '"'):
        text = text.strip(ch)
    return text.strip()


class FurryWillSettings:
    """每日鉴毛（Furry Will / Furry Gallery）配置聚合。"""

    def __init__(self, config: dict | None = None) -> None:
        cfg = config or {}
        self.base_url = _clean(
            cfg.get("furry_will_base_url")
            or os.getenv("FURRY_WILL_BASE_URL")
            or "https://mrjm.fur-bot.com"
        ).rstrip("/")
        self.jwt_secret = _clean(
            cfg.get("furry_will_jwt_secret") or os.getenv("FURRY_WILL_JWT_SECRET")
        )
        self.jwt_qq = _clean(
            cfg.get("furry_will_jwt_qq") or os.getenv("FURRY_WILL_JWT_QQ")
        )
        # 本地图床（与 FTV 插件同款，独立目录）
        self.image_host_url = _clean(
            cfg.get("image_host_url") or os.getenv("FURRY_WILL_IMAGE_HOST_URL")
        ).rstrip("/")
        try:
            self.image_host_port = int(
                cfg.get("image_host_port")
                or os.getenv("FURRY_WILL_IMAGE_HOST_PORT")
                or 11452
            )
        except (TypeError, ValueError):
            self.image_host_port = 11452
        self.image_host_bind = _clean(
            cfg.get("image_host_bind")
            or os.getenv("FURRY_WILL_IMAGE_HOST_BIND")
            or "0.0.0.0"
        )

    def configured(self) -> bool:
        """凭据是否齐全。"""
        return bool(self.base_url and self.jwt_secret and self.jwt_qq)
