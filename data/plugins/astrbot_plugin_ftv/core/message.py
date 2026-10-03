"""消息构造助手：尽量复刻原插件「图 + 文 / 图 + 按钮」的发送格式。

原插件在 goqbot（QQ 官方机器人）下走 markdown 富文本：一条消息里同时放
markdown 图片与正文，必要时挂键盘按钮；其它平台回落为「文本段 + 图片段」。

AstrBot 侧：
- 支持 markdown 的平台（如 QQ Official）：用 ``MessageChain.use_markdown(True)``
  把 markdown 文本（含 ``![alt #宽 #高](url)``）作为一条消息发出；
- 其它平台：回落「Plain 文本 + Image 组件」，图片可用公网 URL 或本地文件。
"""

from __future__ import annotations

import logging
from io import BytesIO

from astrbot.api.message_components import Image, Plain

from . import image_host

logger = logging.getLogger("astrbot")

__all__ = ["markdown_result", "image_text_result", "plain_result", "image_component"]


def image_component(image, url: str | None = None):
    """把 PIL 图片或 URL 变成一个 Image 消息组件。

    Args:
        image: PIL 图片对象或本地路径字符串。
        url: 若给出则优先用公网 URL 构造。

    Returns:
        Image 组件。
    """
    if url:
        return Image.fromURL(url)
    if isinstance(image, (str, bytes)):
        if isinstance(image, bytes):
            return Image.fromBytes(image)
        return Image.fromFileSystem(image)
    buf = BytesIO()
    image.save(buf, format="PNG")
    return Image.fromBytes(buf.getvalue())


def markdown_result(text: str, images: list | None = None):
    """构造 markdown 富文本消息（一条消息里图 + 文）。

    Args:
        text: markdown 正文。
        images: 可选，``[(url, alt, width, height), ...]``，会拼在正文前面。

    Returns:
        ``(chain, use_markdown)``：组件列表与是否走 markdown。
    """
    blocks = []
    for item in images or []:
        url, alt, width, height = item
        blocks.append(image_host.image_markdown(url, alt, width, height))
    body = "\n\n".join([b for b in blocks if b] + [text]) if blocks else text
    return [Plain(body)]


def image_text_result(image, text: str = "", url: str | None = None):
    """构造「文本 + 图片」普通消息（一个气泡）。

    文本段放在图片段之前，与走 goqbot「单图快路径」的观感一致。

    Args:
        image: PIL 图片对象或本地路径。
        text: 附在图上的文本。
        url: 若给出则用公网 URL 发图。

    Returns:
        消息组件列表。
    """
    comps = []
    if text:
        comps.append(Plain(text))
    if image is not None or url:
        comps.append(image_component(image, url))
    return comps


def plain_result(text: str):
    """构造纯文本消息组件列表。"""
    return [Plain(text)]
