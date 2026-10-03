"""每日鉴毛插件（指令 + AI 工具）—— 由 cyxbot 的 pyfurbot(furry_will) 移植。

提供两类能力：
1. 指令 ``每日鉴毛``（别名 鉴毛 / furrywill / fw）：随机抽卡、期数/名称/地区/工作室查询，
   保留原指令名与回复文案；卡片图走本机图床，markdown 平台走「图 + 文」一条消息。
2. AI 工具：抽卡查询，以及把卡片图嵌入回复 / 直接发送的工具。

配置（WebUI 插件管理 → 每日鉴毛 → 配置）：
  furry_will_base_url    API 地址（默认 https://mrjm.fur-bot.com）
  furry_will_jwt_secret  JWT 密钥
  furry_will_jwt_qq      绑定 QQ（用于生成 JWT）
  image_host_url         本地图床对外地址（不填则不发图床）
  image_host_port        本地图床监听端口（默认 11452）
  image_host_bind        本地图床监听地址（默认 0.0.0.0）
"""

from __future__ import annotations

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star

from .commands import FurryWillCommandMixin
from .core import FurryWillService
from .tools import build_tools


class FurryWillPlugin(FurryWillCommandMixin, Star):
    """每日鉴毛插件：注册指令与 LLM 工具。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.service = FurryWillService(dict(config or {}))
        self.context.add_llm_tools(*build_tools(self.service))
        logger.info(
            "每日鉴毛插件已加载 | API %s | 图床 %s | 凭证 %s",
            self.service.settings.base_url,
            self.service.settings.image_host_url or "未启用",
            "已配置" if self.service.settings.configured() else "未配置",
        )

    async def initialize(self) -> None:
        """插件激活：初始化图床并（可选）启动本地静态服务。"""
        try:
            self.service.start()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"每日鉴毛图床初始化异常（不影响插件加载）：{e}")

    async def terminate(self) -> None:
        """插件禁用/重载：停止本地静态服务。"""
        try:
            self.service.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"每日鉴毛图床服务停止异常：{e}")
