"""独立静态图床服务（在后台线程里跑）。

由 cyxbot 的 ``util/static_server.py`` 移植，去掉 nonebot 依赖：
用标准库 ``ThreadingHTTPServer`` 起一个**只读**静态文件服务，把
``core/image_host.py`` 落盘的图片对外提供::

    http://<地址>:<端口>/ftv/tmp/<md5>.png    （临时图，TTL 回收）
    http://<地址>:<端口>/ftv/keep/<md5>.png   （每日鉴毛的图，永久保留）

只服务 ``GET`` / ``HEAD``；不列目录、不跟随符号链接、路径穿越会被拒。
"""

from __future__ import annotations

import logging
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import image_host

logger = logging.getLogger("astrbot")

__all__ = ["is_running", "start_server", "stop_server", "build_handler"]

_MAX_BYTES = 32 * 1024 * 1024
_handle: dict[str, Any] | None = None
_lock = threading.Lock()


def _safe_target(rel: str) -> Path | None:
    """把 URL 相对路径映射成图床目录内的真实文件；越界或非法返回 None。"""
    if not rel or rel.startswith(".") or "\x00" in rel:
        return None
    try:
        root = image_host.static_dir().resolve()
        target = (root / rel).resolve()
    except OSError:
        return None
    if target == root or root not in target.parents:
        return None
    return target


class _ImageHandler(BaseHTTPRequestHandler):
    """只读静态处理器：命中 ``/<prefix>/<文件>`` 就回图，其余一律 404。"""

    server_version = "astrbot-ftv-image-host/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802
        self._serve(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head_only=True)

    def do_POST(self) -> None:  # noqa: N802
        self._send(
            405, b"method not allowed", "text/plain; charset=utf-8", head_only=False
        )

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST

    def _send(self, status, body, ctype, *, head_only, extra=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if head_only or not body:
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _serve(self, *, head_only: bool) -> None:
        try:
            self._serve_inner(head_only=head_only)
        except Exception:  # noqa: BLE001 — 单个请求出错不能带走服务线程
            self._send(
                500, b"internal error", "text/plain; charset=utf-8", head_only=head_only
            )

    def _serve_inner(self, *, head_only: bool) -> None:
        prefix = image_host.url_prefix()
        rel = unquote(urlparse(self.path).path).lstrip("/")

        if rel in ("", "health", prefix):
            self._send(
                200,
                f"ftv image host ok\n{image_host.image_base_url()}\n".encode(),
                "text/plain; charset=utf-8",
                head_only=head_only,
            )
            return

        if not rel.startswith(prefix + "/"):
            self._send(
                404, b"not found", "text/plain; charset=utf-8", head_only=head_only
            )
            return

        target = _safe_target(rel[len(prefix) + 1 :])
        if target is None or not target.is_file():
            self._send(
                404, b"not found", "text/plain; charset=utf-8", head_only=head_only
            )
            return

        try:
            if target.stat().st_size > _MAX_BYTES:
                self._send(
                    404, b"not found", "text/plain; charset=utf-8", head_only=head_only
                )
                return
            data = target.read_bytes()
        except OSError:
            self._send(
                500, b"read error", "text/plain; charset=utf-8", head_only=head_only
            )
            return

        # 有人来取 = 这张图「刚被用过」，刷新 mtime，临时图回收据此判断
        try:
            import os

            os.utime(target, None)
        except OSError:
            pass

        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(
            200,
            data,
            ctype,
            head_only=head_only,
            extra={"Cache-Control": "public, max-age=604800, immutable"},
        )


def build_handler():
    """返回静态处理器类（便于测试）。"""
    return _ImageHandler


def is_running() -> bool:
    """图床服务线程是否在跑。"""
    handle = _handle
    thread = handle.get("thread") if handle else None
    return bool(thread is not None and thread.is_alive())


def start_server(host: str, port: int) -> None:
    """在后台线程里跑静态服务；失败只打日志，不影响机器人本体。

    Args:
        host: 监听地址。
        port: 监听端口。
    """
    global _handle
    with _lock:
        if is_running():
            return
        try:
            httpd = ThreadingHTTPServer((host, port), _ImageHandler)
        except OSError as e:
            logger.error(f"ftv 图床静态服务启动失败（{host}:{port}）：{e!r}")
            return

        httpd.daemon_threads = True
        thread = threading.Thread(
            target=httpd.serve_forever, name="ftv-image-host", daemon=True
        )
        thread.start()
        _handle = {"server": httpd, "thread": thread, "host": host, "port": port}
        logger.info(
            f"ftv 图床静态服务已启动：{image_host.image_base_url() or '（未配置对外地址）'} "
            f"→ {host}:{port}（本地目录 {image_host.static_dir()}）"
        )


def stop_server(timeout: float = 3.0) -> None:
    """停止图床服务。"""
    global _handle
    with _lock:
        handle = _handle
        if not handle:
            return
        try:
            httpd = handle.get("server")
            if httpd is not None:
                httpd.shutdown()
                httpd.server_close()
            thread = handle.get("thread")
            if thread is not None and thread.is_alive():
                thread.join(timeout=timeout)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ftv 图床静态服务停止异常：{e!r}")
        finally:
            _handle = None
