"""每日鉴毛指令（由 cyxbot pyfurbot 的 furry_will.py 移植）。

保留指令名 ``每日鉴毛``（别名 ``鉴毛`` / ``furrywill`` / ``fw``）与回复文案，
尽量复刻原发送格式：markdown 平台走「markdown 图片 + 卡片正文」一条消息，
其它平台回落「文本 + 图片」。

注意：``@filter.command`` 处理器会被 AstrBot 绑定到插件实例，因此这里做成
**mixin**：``class FurryWillPlugin(FurryWillCommandMixin, Star)``。
"""

from __future__ import annotations

import logging
import re

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain

from ..core import image_host
from ..core.card import (
    build_card_keyboard,
    build_card_markdown,
    build_card_text,
    help_text,
    parse_args,
)

logger = logging.getLogger("astrbot")

__all__ = ["FurryWillCommandMixin"]

CARD_MD_MAX_WIDTH = 540
CARD_MD_MAX_HEIGHT = 810

# 匹配回复里嵌入的 markdown 图片（即卡片图）
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*https?://[^)\s]+\)")


def _arg(event: AstrMessageEvent) -> str:
    """取指令后的原始参数文本（去掉指令名）。"""
    msg = event.get_message_str().strip()
    parts = msg.split(" ", 1)
    return parts[1].strip() if len(parts) > 1 else ""


class FurryWillCommandMixin:
    """每日鉴毛指令处理器（mixin，期望宿主提供 ``self.service``）。"""

    service = None  # 由宿主 Star 类注入

    @filter.command("每日鉴毛", alias={"鉴毛", "furrywill", "fw"})
    async def furry_will(self, event: AstrMessageEvent):
        """每日鉴毛：随机抽卡 / 期数 / 名称 / 地区 / 工作室查询。"""
        raw = _arg(event)

        # 帮助
        if raw.lower() in ("help", "帮助", "?", "？"):
            await event.send(event.plain_result(help_text()))
            return

        if not self.service.settings.configured():
            await event.send(
                event.plain_result(
                    "尚未配置每日鉴毛 API 凭证，请先填写 furry_will_base_url / "
                    "furry_will_jwt_secret / furry_will_jwt_qq"
                )
            )
            return

        action, keyword = parse_args(raw)
        if action is None:
            await event.send(
                event.plain_result("无法识别的参数，发送「每日鉴毛 帮助」查看用法")
            )
            return

        logger.info(f"每日鉴毛：action={action} keyword={keyword}")
        try:
            if action == "random":
                body = await self.service.api.random()
            elif action == "qishu":
                body = await self.service.api.by_qishu(keyword)
            elif action == "name":
                body = await self.service.api.by_name(keyword)
            elif action == "city":
                body = await self.service.api.by_city(keyword)
            elif action == "studio":
                body = await self.service.api.by_studio(keyword)
            else:  # pragma: no cover
                await event.send(event.plain_result("未知操作"))
                return
            await self._send_card(event, body)
        except Exception as e:  # noqa: BLE001
            await event.send(event.plain_result(str(e)))

    async def _send_card(self, event: AstrMessageEvent, body: dict) -> None:
        """把卡片（取第一条）带图片发给用户，图和文落在同一条消息里。"""
        data = body.get("data") or []
        if isinstance(data, dict):
            data = [data]
        if not data:
            await event.send(event.plain_result("没有获取到卡片数据，请换关键词再试"))
            return

        producer = body.get("producer", "")
        card = data[0]
        text = build_card_text(card, producer)
        md_body = build_card_markdown(card, producer)
        image_url = (card.get("url") or "").strip()

        # 转存到本机图床（源站链接会过期），转存不了就退回源站链接
        hosted = await image_host.download_and_host(image_url) if image_url else None
        if hosted:
            final_url, width, height = hosted
        else:
            final_url, width, height = image_url, 0, 0

        # 首选 markdown：图 + 文一条发出（尺寸齐了才发，否则回落普通消息）
        if final_url and width and height:
            disp_w, disp_h = image_host.md_display_size(
                width, height, CARD_MD_MAX_WIDTH, CARD_MD_MAX_HEIGHT
            )
            content = image_host.image_markdown(
                final_url, card.get("name") or "每日鉴毛", disp_w, disp_h
            )
            if md_body:
                content = f"{content}\n\n{md_body}"
            chain = event.make_result().message(content)
            chain.use_markdown(True)
            chain.use_keyboard(build_card_keyboard())
            await event.send(chain)
            return

        # 回落：普通消息「纯文本 + 图片 URL」
        if final_url:
            comps = []
            if text:
                comps.append(Plain(f"{text}\n"))
            comps.append(Image.fromURL(final_url))
            await event.send(event.chain_result(comps))
            return

        await event.send(
            event.plain_result(text or "没有获取到卡片数据，请换关键词再试")
        )

    @filter.on_decorating_result()
    async def _force_markdown_when_card(self, event: AstrMessageEvent) -> None:
        """发送前兜底：只要 AI 的回复里含卡片（markdown 图片语法），整条强制走 markdown。

        AI 常会把「每日鉴毛」卡片直接以 ``![说明 #宽px #高px](url)`` 写进回复；
        一旦这条消息被当成纯文本发送，图片语法就会原样显示成文字。
        这里在发送前统一纠正：检测到 markdown 图片就强制 markdown 发送，
        并关闭文转图（否则整条 markdown 会被渲染成一张图片，图里的图片语法不生效）。
        """
        result = event.get_result()
        if result is None or not result.chain:
            return
        text = "".join(c.text for c in result.chain if isinstance(c, Plain))
        if _MD_IMAGE_RE.search(text):
            result.use_markdown(True)
            result.use_t2i(False)
