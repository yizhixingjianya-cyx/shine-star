"""FTV 插件共享基础设施（由 cyxbot ftv 插件移植）。"""

from __future__ import annotations

import logging

from astrbot.api import AstrBotConfig

from . import image_host, static_server
from .api import FtvAPI, FtvError
from .config import FtvSettings
from .render import set_download_headers_provider

logger = logging.getLogger("astrbot")

__all__ = ["FtvService", "FtvError"]


class FtvService:
    """聚合配置、API 客户端与图床，供指令与 AI 工具共用。"""

    def __init__(self, config: AstrBotConfig | dict | None = None) -> None:
        self.settings = FtvSettings(dict(config or {}))
        self.api = FtvAPI(
            base_url=self.settings.base_url,
            app_id=self.settings.app_id,
            client_secret=self.settings.client_secret,
        )

    def start(self) -> None:
        """初始化图床并（可选）启动本地静态服务。"""
        if self.settings.image_host_url:
            image_host.configure(
                base_url=self.settings.image_host_url,
                static_dir_path=None,
                ttl_hours=self.settings.image_ttl_hours,
            )
            image_host.init_hosting()
            static_server.start_server(
                self.settings.image_host_bind, self.settings.image_host_port
            )
        else:
            logger.info(
                "FTV 图床未启用（未配置 image_host_url），"
                "AI 工具与指令将回落为直接发送图片组件"
            )
        set_download_headers_provider(self.api.download_headers)

    def stop(self) -> None:
        """停止静态服务。"""
        static_server.stop_server()
