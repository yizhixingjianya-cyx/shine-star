"""每日鉴毛图床的静态服务适配层 —— 复用 AstrBot 通用图床模块。

真正的实现已抽到 ``astrbot.core.utils.image_host``：所有插件共用一个只读
静态服务，按 URL 前缀分发到各自目录。这里只保留原函数签名。
"""

from __future__ import annotations

from astrbot.core.utils import image_host as _host

__all__ = ["is_running", "start_server", "stop_server"]


def is_running() -> bool:
    """图床服务线程是否在跑。"""
    return _host.is_running()


def start_server(host: str, port: int) -> None:
    """启动共享图床服务（监听地址 / 端口由全局配置决定，参数不再生效）。"""
    del host, port
    _host.start()


def stop_server(timeout: float = 3.0) -> None:
    """停止图床服务。"""
    _host.stop(timeout)
