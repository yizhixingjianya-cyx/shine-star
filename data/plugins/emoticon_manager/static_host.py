"""表情包图床适配层 —— 复用 AstrBot 通用图床模块。

真正的实现在 ``astrbot.core.utils.image_host``：多个插件共用一个只读静态服务，
各自把目录注册到一个 URL 前缀下。**对外地址 / 监听 / 端口统一由 AstrBot 全局
配置 ``image_host`` 决定**，插件不再各自设置。这里只保留本插件原有的函数签名，
调用方（main.py）无需改动。
"""

from __future__ import annotations

from pathlib import Path

from astrbot.core.utils import image_host as _host

__all__ = [
    "configure",
    "is_enabled",
    "url_for",
    "image_size",
    "md_display_size",
    "image_markdown",
    "start",
    "stop",
    "is_running",
]

# 本插件在通用图床里使用的 URL 前缀
_PREFIX = "emoticons"


def configure(
    base_url: str,
    bind: str,
    port: int,
    root: Path,
    prefix: str = _PREFIX,
) -> None:
    """把表情目录注册到通用图床。

    对外地址 / 监听 / 端口由全局配置 ``image_host`` 统一决定，
    这里的 ``base_url`` / ``bind`` / ``port`` 仅为兼容旧签名保留，不再生效。

    Args:
        base_url: 已废弃，忽略。
        bind: 已废弃，忽略。
        port: 已废弃，忽略。
        root: 要对外提供的表情目录。
        prefix: URL 前缀，默认 ``emoticons``。
    """
    del base_url, bind, port
    global _PREFIX
    _PREFIX = str(prefix or _PREFIX).strip("/") or _PREFIX
    _host.register_dir(_PREFIX, root)


def is_enabled() -> bool:
    """图床是否可用（由全局配置决定）。"""
    return _host.is_enabled()


def url_for(filename: str) -> str | None:
    """拼出表情图的公网 URL。"""
    return _host.url_for(_PREFIX, filename)


def image_size(path) -> tuple[int, int]:
    """读图片宽高。"""
    return _host.image_size(path)


def md_display_size(width, height, max_width, max_height, scale=1.0):
    """算 markdown 回填的显示尺寸。"""
    return _host.md_display_size(width, height, max_width, max_height, scale)


def image_markdown(url: str, alt: str, width: int = 0, height: int = 0) -> str:
    """拼 markdown 图片语法。"""
    return _host.image_markdown(url, alt, width, height)


def start() -> None:
    """启动共享图床服务。"""
    _host.start()


def stop(timeout: float = 3.0) -> None:
    """停止共享图床服务。"""
    _host.stop(timeout)


def is_running() -> bool:
    """静态服务是否在跑。"""
    return _host.is_running()
