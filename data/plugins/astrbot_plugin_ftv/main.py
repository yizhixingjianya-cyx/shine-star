"""FTV 兽频道插件（指令 + AI 工具）—— 由 cyxbot 的 ftv 插件移植。

提供两类能力：
1. 指令：热门 / 随机 / 物种 / 搜索 / 兽档案 / 崽崽 / 学校 / 聚会 / 动态流 等，
   保留原指令名、别名与回复文案；图片走本机渲染，尽量复刻原发送格式。
2. AI 工具：把同样的平台能力封装成 function-calling 工具，供大模型在对话中调用；
   其中图片类工具会把渲染结果发布到本地图床并返回 markdown，AI 可直接把图片
   嵌入自己的回复；另有 ``ftv_send_image`` 工具可直接把图片发到聊天。

配置（WebUI 插件管理 → FTV 兽频道 → 配置）：
  ftv_base_url       API 网关地址
  ftv_app_id         应用 ID
  ftv_client_secret  应用密钥
  image_host_url     本地图床对外地址（形如 http://1.2.3.4:11451；不填则不发图床）
  image_host_port    本地图床监听端口（默认 11451）
  image_host_bind    本地图床监听地址（默认 0.0.0.0）
  image_ttl_hours    临时图保留小时数（默认 24，0 表示不回收）
"""

from __future__ import annotations

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star

from .commands import FtvCommandMixin
from .core import FtvService
from .tools import build_tools


class FtvPlugin(FtvCommandMixin, Star):
    """FTV 兽频道插件：注册指令与 LLM 工具。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.service = FtvService(dict(config or {}))
        # 注册 AI 工具（>= v4.5.1）
        self.context.add_llm_tools(*build_tools(self.service))
        logger.info(
            "FTV 兽频道插件已加载 | 网关 %s | 图床 %s",
            self.service.settings.base_url,
            self.service.settings.image_host_url or "未启用",
        )

    async def initialize(self) -> None:
        """插件激活：初始化图床并（可选）启动本地静态服务。"""
        try:
            self.service.start()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"FTV 图床初始化异常（不影响插件加载）：{e}")

    async def terminate(self) -> None:
        """插件禁用/重载：停止本地静态服务。"""
        try:
            self.service.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"FTV 图床服务停止异常：{e}")
