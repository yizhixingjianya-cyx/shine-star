"""每日鉴毛 -> AstrBot LLM 工具（含图片嵌入）。

把「每日鉴毛」的抽卡/查询能力封装成 function-calling 工具：
- 查询类工具返回卡片的文字信息；
- ``furrywill_render_card`` 会转存卡片图到本地图床并返回 markdown 图片语法，
  AI 可把它直接写进回复，图片就显示在聊天里；
- ``furrywill_send_card`` 直接把卡片以 markdown「图 + 文」一条消息发出（与 cyxbot 格式一致）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api.message_components import Image, Plain
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..core import FurryWillError, FurryWillService, image_host
from ..core.card import build_card_keyboard, build_card_markdown, build_card_text

logger = logging.getLogger("astrbot")

__all__ = ["build_tools"]

CARD_MD_MAX_WIDTH = 540
CARD_MD_MAX_HEIGHT = 810

CARD_MD_SCALE = 0.75


def _prop(
    ptype: str, description: str, enum: list[str] | None = None
) -> dict[str, Any]:
    """构造一个 JSON schema 属性。"""
    p: dict[str, Any] = {"type": ptype, "description": description}
    if enum:
        p["enum"] = enum
    return p


def _make_tool(
    name: str,
    description: str,
    props: dict[str, dict],
    required: list[str] | None,
    handler: Callable[..., Awaitable[str]],
    *,
    with_context: bool = False,
):
    """动态构造一个 FunctionTool 子类。"""
    schema = {"type": "object", "properties": props, "required": required or []}

    async def _call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:  # noqa: ANN001
        try:
            if with_context:
                return await handler(context, kwargs)
            return await handler(kwargs)
        except FurryWillError as e:
            return f"[每日鉴毛错误] {e}"
        except Exception as e:  # noqa: BLE001
            logger.exception("[furrywill tools] 工具调用异常")
            return f"[每日鉴毛错误] {type(e).__name__}: {e}"

    cls = type(
        "_Concrete",
        (FunctionTool,),
        {
            "__module__": __name__,
            "__annotations__": {"name": str, "description": str, "parameters": dict},
            "name": name,
            "description": description,
            "parameters": Field(default_factory=lambda: schema),
            "call": _call,
        },
    )
    return dataclass(cls)()


def build_tools(service: FurryWillService) -> list[FunctionTool]:
    """构建每日鉴毛的 LLM 工具。"""
    api = service.api
    tools: list[FunctionTool] = []

    async def _fetch(kwargs: dict) -> dict:
        """根据 keyword 走不同查询分支，返回 API body。"""
        action = kwargs.get("action") or "random"
        keyword = (kwargs.get("keyword") or "").strip()
        if action == "qishu":
            return await api.by_qishu(keyword)
        if action == "name":
            return await api.by_name(keyword)
        if action == "city":
            return await api.by_city(keyword)
        if action == "studio":
            return await api.by_studio(keyword)
        return await api.random()

    async def _text_handler(kwargs: dict) -> str:
        body = await _fetch(kwargs)
        data = body.get("data") or []
        if isinstance(data, dict):
            data = [data]
        if not data:
            return "[每日鉴毛] 没有获取到卡片数据，请换关键词再试"
        producer = body.get("producer", "")
        card = data[0]
        return build_card_text(card, producer)

    tools.append(
        _make_tool(
            "furrywill_lookup",
            "查询『每日鉴毛』(Furry Will / Furry Gallery) 卡片信息（纯文本）。"
            "【使用范围】用户想了解/检索鉴毛卡片资料、且**不需要图片**时调用；"
            "本工具只返回文字、不产生图片——用户要看图请改用 furrywill_render_card，"
            "用户要求直接发到聊天请用 furrywill_send_card。"
            "action 可选 random(随机抽卡)/qishu(按期刊号)/name(按名称)/city(按地区)/studio(按工作室)；"
            "random 时 keyword 可留空。",
            {
                "action": _prop(
                    "string",
                    "查询方式",
                    enum=["random", "qishu", "name", "city", "studio"],
                ),
                "keyword": _prop(
                    "string", "查询关键词：期号/名称/地区/工作室；random 可留空"
                ),
            },
            ["action"],
            _text_handler,
        )
    )
    tools.append(
        _make_tool(
            "furrywill_render_card",
            "抽一张『每日鉴毛』卡片并把图片嵌入回复。"
            "【使用范围】用户想要**看图**（要卡面图片、收藏/保存图片）时优先用它；"
            "返回的 markdown 必须被你**原样**写进最终回复，图片才会显示。"
            "只想拿文字资料时用 furrywill_lookup；用户明确要求『直接发/别写文字』时用 furrywill_send_card。"
            "会把卡片图转存到本地图床并返回 markdown 图片语法"
            "（形如 `![每日鉴毛 #宽 #高](http://.../furrywill/keep/xxx.png)`）。"
            "设置 send_to_chat=true 时会同时把图片作为一条消息直接发出，"
            "并把**实际发送的那张卡片**信息一并返回，请以返回信息为准配文。",
            {
                "action": _prop(
                    "string",
                    "查询方式",
                    enum=["random", "qishu", "name", "city", "studio"],
                ),
                "keyword": _prop(
                    "string", "查询关键词：期号/名称/地区/工作室；random 可留空"
                ),
                "send_to_chat": _prop(
                    "boolean", "是否同时直接把图片发到聊天，默认 false"
                ),
            },
            ["action"],
            partial(_render_card_tool, service),
            with_context=True,
        )
    )
    tools.append(
        _make_tool(
            "furrywill_send_card",
            "抽一张『每日鉴毛』卡片并**直接发送**到当前聊天（无需你把 markdown 写进回复）。"
            "【使用范围】仅当用户明确要求『直接发一张/不用我贴文字』时使用；"
            "普通看图请求请用 furrywill_render_card，同一次请求不要与它同时调用。"
            "注意：每次调用都会**重新随机抽一张**卡片；本工具会把**实际发送的那张卡片**的"
            "名字/期号/地区/种族/工作室/出品方信息一并返回，请一律以返回的这张卡片信息为准"
            "来写配文，不要另行编造或使用其它卡片的信息。",
            {
                "action": _prop(
                    "string",
                    "查询方式",
                    enum=["random", "qishu", "name", "city", "studio"],
                ),
                "keyword": _prop(
                    "string", "查询关键词：期号/名称/地区/工作室；random 可留空"
                ),
            },
            ["action"],
            partial(_send_card_tool, service),
            with_context=True,
        )
    )

    logger.info(
        "[furrywill tools] 已注册 %d 个工具：%s",
        len(tools),
        ", ".join(t.name for t in tools),
    )
    return tools


async def _fetch_card(service: FurryWillService, action: str, keyword: str) -> dict:
    """按 action 抽卡，返回 (card, producer) 的 body。"""
    action = action or "random"
    if action == "qishu":
        body = await service.api.by_qishu(keyword)
    elif action == "name":
        body = await service.api.by_name(keyword)
    elif action == "city":
        body = await service.api.by_city(keyword)
    elif action == "studio":
        body = await service.api.by_studio(keyword)
    else:
        body = await service.api.random()
    return body


async def _hosted_card(service: FurryWillService, body: dict):
    """取卡片并转存图片，返回 ``(card, producer, url, width, height)``。"""
    data = body.get("data") or []
    if isinstance(data, dict):
        data = [data]
    if not data:
        return None
    producer = body.get("producer", "")
    card = data[0]
    image_url = (card.get("url") or "").strip()
    hosted = await image_host.download_and_host(image_url) if image_url else None
    if hosted:
        url, w, h = hosted
    else:
        url, w, h = image_url, 0, 0
    return card, producer, url, w, h


async def _markdown_for_card(
    card: dict, producer: str, url: str, w: int, h: int
) -> str:
    """拼出 markdown（图 + 卡片正文）。"""
    if not url:
        return ""
    if w and h:
        disp = image_host.md_display_size(w, h, CARD_MD_MAX_WIDTH, CARD_MD_MAX_HEIGHT)
    else:
        disp = (0, 0)
    img_md = image_host.image_markdown(url, card.get("name") or "每日鉴毛", *disp)
    body = build_card_markdown(card, producer)
    return f"{img_md}\n\n{body}" if body else img_md


def _sent_card_reply(card_text: str) -> str:
    """拼出「已发送 + 所发卡片信息」的返回文案给模型。

    直接发送类工具会把**实际抽到并发送**的那张卡片信息一并回传；模型据此撰写配文
    即可，避免它另外描述一张卡而出现图文不一致。

    Args:
        card_text: ``build_card_text`` 生成的卡片正文。

    Returns:
        返回给 LLM 的工具结果文本。
    """
    return (
        "已把下面这张卡片直接发送到聊天。请以下面这张卡片的信息为准来描述它，"
        "不要另行编造或使用其它卡片的信息：\n\n"
        f"{card_text}"
    )


async def _render_card_tool(service: FurryWillService, context, kw: dict) -> str:
    """抽卡并返回 markdown / 直接发送。"""
    body = await _fetch_card(
        service, (kw.get("action") or "random"), (kw.get("keyword") or "").strip()
    )
    fetched = await _hosted_card(service, body)
    if not fetched:
        return "[每日鉴毛] 没有获取到卡片数据，请换关键词再试"
    card, producer, url, w, h = fetched

    if kw.get("send_to_chat") and url:
        event = context.context.event
        md = await _markdown_for_card(card, producer, url, w, h) if (w and h) else ""
        if md:
            chain = event.make_result().message(md)
            chain.use_markdown(True)
            chain.use_keyboard(build_card_keyboard())
            await event.send(chain)
        else:
            comps = [Plain(f"{build_card_text(card, producer)}\n"), Image.fromURL(url)]
            await event.send(event.chain_result(comps))
        return _sent_card_reply(build_card_text(card, producer))

    md = await _markdown_for_card(card, producer, url, w, h)
    text = build_card_text(card, producer)
    if md:
        return f"{text}\n\n可直接把下面这行原样放进回复来嵌入图片：\n{md}"
    return f"{text}\n\n（未配置 image_host_url，无法嵌入图片）"


async def _send_card_tool(service: FurryWillService, context, kw: dict) -> str:
    """抽卡并直接发送到当前聊天。"""
    body = await _fetch_card(
        service, (kw.get("action") or "random"), (kw.get("keyword") or "").strip()
    )
    fetched = await _hosted_card(service, body)
    if not fetched:
        return "[每日鉴毛] 没有获取到卡片数据，请换关键词再试"
    card, producer, url, w, h = fetched
    card_text = build_card_text(card, producer)

    event = context.context.event

    # 首选 markdown：图 + 文一条发出（与 cyxbot / 指令路径同一套排版与尺寸规则）
    if url and w and h:
        md = await _markdown_for_card(card, producer, url, w, h)
        if md:
            chain = event.make_result().message(md)
            chain.use_markdown(True)
            chain.use_keyboard(build_card_keyboard())
            await event.send(chain)
            return _sent_card_reply(card_text)

    # 回落：普通消息「纯文本 + 图片 URL」
    if url:
        comps = [Plain(f"{card_text}\n"), Image.fromURL(url)]
        await event.send(event.chain_result(comps))
        return _sent_card_reply(card_text)

    await event.send(event.plain_result(card_text))
    return _sent_card_reply(card_text)
