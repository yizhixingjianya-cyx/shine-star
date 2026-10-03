"""每日鉴毛插件共享基础设施。"""

from __future__ import annotations

import logging

from astrbot.api import AstrBotConfig

from . import image_host, static_server
from .api import FurryWillAPI, FurryWillError
from .config import FurryWillSettings

logger = logging.getLogger("astrbot")

__all__ = ["FurryWillService", "FurryWillError"]


class FurryWillService:
    """聚合配置、API 客户端与图床，供指令与 AI 工具共用。"""

    def __init__(self, config: AstrBotConfig | dict | None = None) -> None:
        self.settings = FurryWillSettings(dict(config or {}))
        self.api = FurryWillAPI(self.settings)

    def start(self) -> None:
        """初始化图床并（可选）启动本地静态服务。"""
        if self.settings.image_host_url:
            image_host.configure(base_url=self.settings.image_host_url)
            image_host.init_hosting()
            static_server.start_server(
                self.settings.image_host_bind, self.settings.image_host_port
            )
        else:
            logger.info(
                "每日鉴毛图床未启用（未配置 image_host_url），"
                "将回落为直接发送图片 URL 组件"
            )

    def stop(self) -> None:
        """停止静态服务。"""
        static_server.stop_server()
