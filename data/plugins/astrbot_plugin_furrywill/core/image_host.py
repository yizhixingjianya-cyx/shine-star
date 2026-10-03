"""每日鉴毛图床适配层 —— 复用 AstrBot 通用图床模块。

真正的实现在 ``astrbot.core.utils.image_host``：多个插件共用一个只读静态服务，
各自把目录注册到一个 URL 前缀下（本插件用 ``furrywill``，持久图放在 ``keep/``）。
这里保留原函数签名，供 commands / tools / service 调用。
"""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path

from astrbot.core.utils import image_host as _host

logger = logging.getLogger("astrbot")

__all__ = [
    "configure",
    "hosting_enabled",
    "image_base_url",
    "url_prefix",
    "static_dir",
    "keep_dir",
    "init_hosting",
    "host_bytes",
    "host_pil",
    "image_markdown",
    "md_display_size",
    "download_and_host",
    "MD_MAX_WIDTH",
    "MD_MAX_HEIGHT",
]

KEEP_DIRNAME = "keep"
_DEFAULT_PREFIX = "furrywill"

MD_MAX_WIDTH = 720
MD_MAX_HEIGHT = 1080

_prefix = _DEFAULT_PREFIX


def configure(base_url: str = "", prefix: str = _DEFAULT_PREFIX) -> None:
    """把图床目录注册到通用图床。

    对外地址 / 监听 / 端口由 AstrBot 全局配置 ``image_host`` 统一决定，
    ``base_url`` 仅为兼容旧签名保留，不再生效。

    Args:
        base_url: 已废弃，忽略。
        prefix: URL 前缀，默认 ``furrywill``。
    """
    del base_url
    global _prefix
    _prefix = (prefix or _DEFAULT_PREFIX).strip("/") or _DEFAULT_PREFIX
    _host.register_dir(_prefix, static_dir())


def hosting_enabled() -> bool:
    """图片托管是否启用。"""
    return _host.is_enabled()


def image_base_url() -> str:
    """对外基地址。"""
    return _host.base_url()


def url_prefix() -> str:
    """URL 路径前缀。"""
    return _prefix


def static_dir() -> Path:
    """本机静态目录。"""
    return Path(__file__).resolve().parent / "image_host"


def keep_dir() -> Path:
    """持久图目录（永不回收）。"""
    return static_dir() / KEEP_DIRNAME


def init_hosting() -> None:
    """启动时建目录。"""
    if not hosting_enabled():
        logger.info("每日鉴毛图床未启用（未配置 image_host_url）")
        return
    try:
        keep_dir().mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f"每日鉴毛图床目录创建失败（{keep_dir()}）：{e}")


def host_bytes(data: bytes, ext: str = ".png") -> str | None:
    """把图片字节写入 ``keep/`` 并返回公网 URL；失败返回 None。"""
    return _host.host_bytes(data, _prefix, ext, subdir=KEEP_DIRNAME)


def host_pil(img) -> str | None:
    """把 PIL 图片存成 PNG 后发布。"""
    buf = BytesIO()
    img.save(buf, format="PNG")
    return host_bytes(buf.getvalue(), ".png")


def image_markdown(url: str, alt: str = "图片", width: int = 0, height: int = 0) -> str:
    """拼 markdown 图片语法 ``![说明 #宽px #高px](url)``。"""
    return _host.image_markdown(url, alt, width, height)


def md_display_size(
    width: int,
    height: int,
    max_width: int = MD_MAX_WIDTH,
    max_height: int = MD_MAX_HEIGHT,
) -> tuple[int, int]:
    """算出 markdown 该回填的显示尺寸（等比，不放大）。"""
    return _host.md_display_size(width, height, max_width, max_height)


async def download_and_host(image_url: str) -> tuple[str, int, int] | None:
    """下载源图并原样转存到 ``keep/``，返回 ``(公网 URL, 宽, 高)``。

    图床未启用/不可用时回落源站链接 + 尺寸（平台发送时会即时下载转存）。

    Args:
        image_url: 源站图片地址。

    Returns:
        转存成功返回三元组，失败返回 None。
    """
    return await _host.download_and_host(image_url, _prefix, subdir=KEEP_DIRNAME)
