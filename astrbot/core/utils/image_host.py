"""可复用的本地图床模块。

多个插件可以共用同一个图床：各自把图片目录注册到一个 URL 前缀下，由本模块
统一启动一个只读 HTTP 服务对外提供，并给出公网 URL、图片尺寸与 markdown
图片语法。图片必须能被公网访问，QQ 官方这类平台才会去下载转存。

典型用法::

    from astrbot.core.utils import image_host

    image_host.configure(base_url="http://1.2.3.4:11453", bind="0.0.0.0", port=11453)
    image_host.register_dir("emoticons", emoticons_dir)
    image_host.start()

    url = image_host.url_for("emoticons", "cat.png")
    dw, dh = image_host.md_display_size(w, h, 400, 400)
    md = image_host.image_markdown(url, "猫猫", dw, dh)

设计要点：

- 只读：仅响应 ``GET`` / ``HEAD``，其余方法 405；
- 安全：不列目录、拒绝路径穿越、不跟随符号链接，最终路径必须落在注册目录内；
- 多目录：按 URL 首段前缀分发到各自目录，插件之间互不干扰；
- 零额外依赖：静态服务与图片尺寸解析只用标准库（下载才用到 httpx）。
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

logger = logging.getLogger("astrbot")

__all__ = [
    "configure",
    "register_dir",
    "is_enabled",
    "is_running",
    "base_url",
    "url_for",
    "host_bytes",
    "download_and_host",
    "image_size",
    "image_size_bytes",
    "md_display_size",
    "image_markdown",
    "start",
    "stop",
]

# 单文件上限：超出按不存在处理，避免误发超大文件
_MAX_BYTES = 32 * 1024 * 1024

_state: dict[str, Any] = {
    "base_url": "",
    "bind": "0.0.0.0",
    "port": 0,
    "roots": {},  # URL 前缀 -> 本机目录
}
_server: dict[str, Any] | None = None
_lock = threading.Lock()


# ---------------------------------------------------------------- 配置
def configure(
    base_url: str | None = None,
    bind: str | None = None,
    port: int | None = None,
) -> None:
    """设置图床对外地址与监听参数。

    只更新传入的字段，传 ``None`` 表示保持原值不变，方便多个插件分别配置。

    Args:
        base_url: 公网可访问的基地址，例如 ``http://1.2.3.4:11453``；
            传空串表示停用图床。
        bind: 监听地址，默认 ``0.0.0.0``。
        port: 监听端口。
    """
    if base_url is not None:
        raw = str(base_url).strip().rstrip("/")
        if raw and "://" not in raw:
            raw = f"http://{raw}"
        _state["base_url"] = raw
    if bind is not None:
        _state["bind"] = str(bind).strip() or "0.0.0.0"
    if port is not None:
        try:
            _state["port"] = int(port or 0)
        except (TypeError, ValueError):
            _state["port"] = 0


def register_dir(prefix: str, root: str | Path) -> None:
    """把一个本机目录注册到 URL 前缀下。

    Args:
        prefix: URL 前缀（如 ``emoticons``），会拼在 base_url 之后。
        root: 要对外提供的本机目录。
    """
    key = str(prefix or "").strip("/")
    if not key:
        logger.warning("图床注册目录失败：前缀不能为空")
        return
    _state["roots"][key] = Path(root)


def is_enabled() -> bool:
    """图床是否可用（配了对外地址且至少注册了一个目录）。"""
    return bool(_state["base_url"]) and bool(_state["roots"])


def base_url() -> str:
    """当前配置的图床对外基地址（可能为空）。"""
    return str(_state["base_url"])


def _root_for(prefix: str) -> Path | None:
    return _state["roots"].get(str(prefix or "").strip("/"))


# ---------------------------------------------------------------- URL
def url_for(prefix: str, filename: str, subdir: str = "") -> str | None:
    """拼出某个文件的公网 URL；未启用或前缀未注册返回 None。

    Args:
        prefix: 注册时的 URL 前缀。
        filename: 文件名（会被 URL 编码）。
        subdir: 目录内的可选子目录，会一并出现在 URL 路径里。

    Returns:
        公网 URL；不可用时返回 None。
    """
    if not is_enabled() or not filename or _root_for(prefix) is None:
        return None
    parts = [str(prefix).strip("/")]
    sub = str(subdir or "").strip("/")
    if sub:
        parts.append(sub)
    parts.append(quote(str(filename), safe=""))
    return f"{_state['base_url']}/{'/'.join(parts)}"


def host_bytes(
    data: bytes,
    prefix: str,
    ext: str = ".png",
    subdir: str = "",
) -> str | None:
    """把图片字节写入注册目录（按内容 md5 命名）并返回公网 URL。

    Args:
        data: 图片字节。
        prefix: 注册时的 URL 前缀。
        ext: 文件后缀（含点），默认 ``.png``。
        subdir: 目录内的子目录（会一并出现在 URL 路径里）。

    Returns:
        公网 URL；未启用、目录不可写或数据为空时返回 None。
    """
    root = _root_for(prefix)
    if not is_enabled() or root is None or not data:
        return None
    directory = root / subdir if subdir else root
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(f"图床目录不可写（{directory}）：{e}")
        return None
    name = hashlib.md5(data).hexdigest() + (ext or ".png")
    path = directory / name
    if path.exists():
        try:
            os.utime(path, None)
        except OSError:
            pass
    else:
        try:
            tmp = directory / f".{name}.tmp"
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(f"图床写入失败（{path}）：{e}")
            return None
    return url_for(prefix, name, subdir)


async def download_and_host(
    image_url: str,
    prefix: str,
    default_ext: str = ".png",
    subdir: str = "",
) -> tuple[str, int, int] | None:
    """下载远端图片并原样转存到图床，返回 ``(公网 URL, 宽, 高)``。

    图床不可用（未启用 / 未注册 / 写入失败）时，若已成功下载并解析出尺寸，
    则回落返回**源站链接 + 尺寸**，这样 markdown 图片依旧可用
    （平台在发送时会即时下载转存该资源）。

    Args:
        image_url: 源图地址。
        prefix: 注册时的 URL 前缀。
        default_ext: 无法从 URL 推断后缀时使用的默认后缀。
        subdir: 目录内的子目录。

    Returns:
        成功返回三元组；下载失败或无法解析尺寸时返回 None。
    """
    if not image_url:
        return None
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx 是核心依赖，缺失时直接回落
        logger.warning("图床转存需要 httpx，当前环境不可用")
        return None

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(image_url)
            if resp.status_code != 200:
                logger.warning(
                    f"图床下载源图失败 HTTP {resp.status_code} | {image_url}"
                )
                return None
            data = resp.content
    except Exception as e:  # noqa: BLE001
        logger.warning(f"图床下载源图出错 {e} | {image_url}")
        return None
    if not data:
        return None

    width, height = image_size_bytes(data)
    ext = Path(urlparse(image_url).path).suffix.lower() or default_ext
    if not ext.startswith(".") or len(ext) > 6:
        ext = default_ext
    url = host_bytes(data, prefix, ext, subdir)
    if not url:
        return (image_url, width, height) if width and height else None
    return url, width, height


# ---------------------------------------------------------------- 图片尺寸
def image_size_bytes(data: bytes) -> tuple[int, int]:
    """从图片字节里解析宽高（PNG / JPEG / GIF / WEBP）；失败返回 ``(0, 0)``。"""
    try:
        with BytesIO(data) as f:
            head = f.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                f.seek(16)
                w, h = struct.unpack(">II", f.read(8))
                return int(w), int(h)
            if head[:6] in (b"GIF87a", b"GIF89a"):
                w, h = struct.unpack("<HH", head[6:10])
                return int(w), int(h)
            if head[:2] == b"\xff\xd8":
                return _jpeg_size(f)
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                return _webp_size(f, head)
    except (OSError, struct.error):
        pass
    return 0, 0


def image_size(path: str | Path) -> tuple[int, int]:
    """读图片文件的宽高；无法解析时返回 ``(0, 0)``。"""
    try:
        with Path(path).open("rb") as f:
            data = f.read(65536)
    except OSError:
        return 0, 0
    return image_size_bytes(data)


def _jpeg_size(f) -> tuple[int, int]:
    """解析 JPEG 的 SOF 段取宽高。"""
    f.seek(2)
    while True:
        marker = f.read(2)
        if len(marker) < 2 or marker[0] != 0xFF:
            return 0, 0
        m = marker[1]
        if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
            continue
        seg_len_bytes = f.read(2)
        if len(seg_len_bytes) < 2:
            return 0, 0
        seg_len = struct.unpack(">H", seg_len_bytes)[0]
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            f.read(1)  # 精度
            h, w = struct.unpack(">HH", f.read(4))
            return int(w), int(h)
        f.seek(seg_len - 2, 1)


def _webp_size(f, head: bytes) -> tuple[int, int]:
    """解析 WebP（VP8 / VP8L / VP8X）宽高。"""
    f.seek(12)
    chunk = f.read(4)
    if chunk == b"VP8X":
        f.read(4)
        w = int.from_bytes(f.read(3), "little") + 1
        h = int.from_bytes(f.read(3), "little") + 1
        return w, h
    if chunk == b"VP8 ":
        f.read(3)
        if f.read(3) != b"\x9d\x01\x2a":
            return 0, 0
        w, h = struct.unpack("<HH", f.read(4))
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L":
        f.read(1)
        b = f.read(4)
        if len(b) < 4:
            return 0, 0
        bits = int.from_bytes(b, "little")
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
        return w, h
    return 0, 0


def md_display_size(
    width: int,
    height: int,
    max_width: int,
    max_height: int,
    scale: float = 1.0,
) -> tuple[int, int]:
    """算出 markdown 该回填的显示尺寸。

    先等比缩进 ``max_width`` x ``max_height`` 的框（只缩小、不放大），
    再整体乘 ``scale``。

    Args:
        width: 原图宽。
        height: 原图高。
        max_width: 显示宽上限（<=0 表示不限）。
        max_height: 显示高上限（<=0 表示不限）。
        scale: 最终缩放系数（<=0 视为 1.0）。

    Returns:
        ``(显示宽, 显示高)``；宽高不合法时返回 ``(0, 0)``。
    """
    if width <= 0 or height <= 0:
        return 0, 0
    ratio = 1.0
    if max_width > 0 and max_height > 0:
        ratio = min(1.0, max_width / width, max_height / height)
    try:
        factor = float(scale)
    except (TypeError, ValueError):
        factor = 1.0
    if factor <= 0:
        factor = 1.0
    return max(1, int(width * ratio * factor)), max(1, int(height * ratio * factor))


def image_markdown(url: str, alt: str = "图片", width: int = 0, height: int = 0) -> str:
    """拼 markdown 图片语法 ``![说明 #宽px #高px](url)``（宽高为 0 时省略尺寸）。

    注意：QQ 官方要求尺寸必须带，否则客户端只画一个 ``[文本]`` 方块。
    """
    safe_alt = (alt or "图片").replace("[", "（").replace("]", "）").replace("\n", " ")
    size = f" #{int(width)}px #{int(height)}px" if width and height else ""
    return f"![{safe_alt}{size}]({url})"


# ---------------------------------------------------------------- 静态服务
def _safe_target(root: Path, rel: str) -> Path | None:
    """把 URL 相对路径映射成目录内的真实文件；越界或非法返回 None。"""
    if not rel or rel.startswith(".") or "\x00" in rel:
        return None
    try:
        base = root.resolve()
        target = (base / rel).resolve()
    except OSError:
        return None
    if target == base or base not in target.parents:
        return None
    return target


class _Handler(BaseHTTPRequestHandler):
    """只读静态处理器：按 URL 首段前缀分发到对应目录。"""

    server_version = "astrbot-image-host/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A002
        pass

    def log_error(self, fmt, *args):  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802
        self._serve(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head_only=True)

    def do_POST(self) -> None:  # noqa: N802
        self._send(
            405,
            b"method not allowed",
            "text/plain; charset=utf-8",
            head_only=False,
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
                500,
                b"internal error",
                "text/plain; charset=utf-8",
                head_only=head_only,
            )

    def _serve_inner(self, *, head_only: bool) -> None:
        rel = unquote(urlparse(self.path).path).lstrip("/")
        if rel in ("", "health"):
            payload = f"astrbot image host ok\n{_state['base_url']}\n".encode()
            self._send(200, payload, "text/plain; charset=utf-8", head_only=head_only)
            return
        prefix, _, rest = rel.partition("/")
        root = _state["roots"].get(prefix)
        if root is None or not rest:
            self._send(
                404, b"not found", "text/plain; charset=utf-8", head_only=head_only
            )
            return
        target = _safe_target(root, rest)
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
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(
            200,
            data,
            ctype,
            head_only=head_only,
            extra={"Cache-Control": "public, max-age=604800, immutable"},
        )


def is_running() -> bool:
    """静态服务线程是否在跑。"""
    handle = _server
    thread = handle.get("thread") if handle else None
    return bool(thread is not None and thread.is_alive())


def start() -> None:
    """在后台线程里启动只读静态服务；失败只打日志，不影响调用方。"""
    global _server
    with _lock:
        if not is_enabled():
            logger.info("图床未启用（未配置对外地址或未注册目录），跳过启动")
            return
        if is_running():
            return
        host, port = _state["bind"], _state["port"]
        if not port:
            logger.warning("图床未配置监听端口，跳过启动")
            return
        try:
            httpd = ThreadingHTTPServer((host, port), _Handler)
        except OSError as e:
            logger.error(f"图床启动失败（{host}:{port}）：{e!r}")
            return
        httpd.daemon_threads = True
        thread = threading.Thread(
            target=httpd.serve_forever, name="astrbot-image-host", daemon=True
        )
        thread.start()
        _server = {"server": httpd, "thread": thread, "host": host, "port": port}
        logger.info(
            f"图床已启动：{_state['base_url']} → {host}:{port}"
            f"（目录前缀 {list(_state['roots'])}）"
        )


def stop(timeout: float = 3.0) -> None:
    """停止静态服务。"""
    global _server
    with _lock:
        handle = _server
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
            logger.warning(f"图床停止异常：{e!r}")
        finally:
            _server = None
