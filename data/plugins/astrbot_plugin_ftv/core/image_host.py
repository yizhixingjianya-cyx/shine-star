"""本地图床：把本机生成的图片落盘，换成公网可访问 URL。

由 cyxbot 的 ``util/image_host.py`` 精简移植，去掉了对 nonebot 的依赖：

- ``host_bytes`` / ``host_image`` 把图片写入静态目录，按内容 md5 命名，同图复用 URL；
- ``image_markdown`` 拼出 markdown 图片语法 ``![说明 #宽 #宽](url)``；
- ``md_display_size`` 等比算出回填的显示尺寸（只改显示，不重编码）。

对外基地址通过插件配置 ``image_host_url`` 指定（形如 ``http://1.2.3.4:11451``）。
未配置时 ``host_*`` 返回 ``None``，调用方应回落为「直接发图片组件」的普通消息。
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from io import BytesIO
from pathlib import Path

logger = logging.getLogger("astrbot")

__all__ = [
    "configure",
    "hosting_enabled",
    "image_base_url",
    "url_prefix",
    "static_dir",
    "temp_dir",
    "keep_dir",
    "host_bytes",
    "host_image",
    "image_markdown",
    "md_display_size",
    "sniff_ext",
    "MD_MAX_WIDTH",
    "MD_MAX_HEIGHT",
]

TEMP_DIRNAME = "tmp"
KEEP_DIRNAME = "keep"
_DEFAULT_PREFIX = "ftv"
_KEEP_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")

# 运行时配置（由插件 __init__ 调 configure 注入）
_base_url = ""
_prefix = _DEFAULT_PREFIX
_static_dir: Path | None = None
_ttl_hours = 24.0

_host_warned = False
_host_logged = False
_last_cleanup = 0.0
_CLEANUP_INTERVAL = 3600


def configure(
    base_url: str = "",
    prefix: str = _DEFAULT_PREFIX,
    static_dir_path: str | Path | None = None,
    ttl_hours: float = 24.0,
) -> None:
    """注入图床配置。

    Args:
        base_url: 对外基地址，例如 ``http://1.2.3.4:11451``；空则关闭图片托管。
        prefix: URL 路径前缀，默认 ``ftv``。
        static_dir_path: 本机静态目录；为空时用插件目录下的 ``image_host``。
        ttl_hours: 临时图保留小时数，``0`` 表示不回收。
    """
    global _base_url, _prefix, _static_dir, _ttl_hours
    raw = (base_url or "").strip().rstrip("/")
    if raw and "://" not in raw:
        raw = f"http://{raw}"
    _base_url = raw
    _prefix = (prefix or _DEFAULT_PREFIX).strip("/") or _DEFAULT_PREFIX
    if static_dir_path:
        _static_dir = Path(static_dir_path).expanduser()
    else:
        _static_dir = Path(__file__).resolve().parent.parent / "image_host"
    try:
        _ttl_hours = float(ttl_hours)
    except (TypeError, ValueError):
        _ttl_hours = 24.0


def hosting_enabled() -> bool:
    """图片托管是否启用：需显式配置对外地址。"""
    return bool(_base_url)


def image_base_url() -> str:
    """对外基地址。"""
    return _base_url


def url_prefix() -> str:
    """URL 路径前缀。"""
    return _prefix


def static_dir() -> Path:
    """本机静态目录。"""
    return _static_dir or (Path(__file__).resolve().parent.parent / "image_host")


def temp_dir() -> Path:
    """临时图目录（会被 TTL 回收）。"""
    return static_dir() / TEMP_DIRNAME


def keep_dir() -> Path:
    """持久图目录（永不回收）。"""
    return static_dir() / KEEP_DIRNAME


def temp_ttl() -> float:
    """临时图保留秒数；``0`` 表示不回收。"""
    return _ttl_hours * 3600.0 if _ttl_hours > 0 else 0.0


def sniff_ext(data: bytes) -> str:
    """按文件头判断图片类型，决定落盘后缀。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def purge_expired() -> int:
    """回收超过 TTL 没被用过的临时图，返回删除张数。"""
    root = static_dir()
    ttl = temp_ttl()
    if not root.is_dir() or ttl <= 0:
        return 0
    now = time.time()
    removed = 0
    keep = root / KEEP_DIRNAME
    try:
        candidates = list(root.rglob("*"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if not path.is_file() or path.is_symlink():
                continue
            if keep == path or keep in path.parents:
                continue
            if path.name.startswith("."):
                continue
            if path.suffix.lower() not in _KEEP_SUFFIXES:
                continue
            if now - path.stat().st_mtime > ttl:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _cleanup() -> None:
    """发布图片时顺手回收过期图（节流：最多每小时扫一次）。"""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < _CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    removed = purge_expired()
    if removed:
        logger.info(f"图床回收了 {removed} 张过期的临时图")


def init_hosting() -> None:
    """启动时初始化图床：建目录 + 清一遍过期图 + 打一行状态日志。"""
    if not hosting_enabled():
        logger.info("图床未启用（未配置 image_host_url），跳过初始化")
        return
    root = static_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        temp_dir().mkdir(parents=True, exist_ok=True)
        keep_dir().mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f"图床目录创建失败（{root}）：{e}")
        return
    removed = purge_expired()
    logger.info(
        f"图床初始化完成 | 对外地址 {image_base_url()} | 本地目录 {root} | "
        f"本次清理过期图 {removed} 张"
    )


def host_bytes(data: bytes, ext: str = "", persistent: bool = False) -> str | None:
    """把图片字节写入静态目录并返回公网 URL；不可用/失败返回 None。

    Args:
        data: 图片字节。
        ext: 后缀，留空按文件头自动判断。
        persistent: ``True`` 落在 ``keep/`` 永久保留，否则 ``tmp/`` 走 TTL 回收。

    Returns:
        公网 URL，失败返回 None。
    """
    global _host_warned, _host_logged
    if not hosting_enabled():
        return None

    directory = keep_dir() if persistent else temp_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        if not _host_warned:
            _host_warned = True
            logger.warning(f"图床目录不可写（{directory}）：{e}")
        return None

    name = hashlib.md5(data).hexdigest() + (ext or sniff_ext(data))
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
            if not _host_warned:
                _host_warned = True
                logger.warning(f"图床写入失败（{path}）：{e}")
            return None

    _cleanup()
    parts = [p for p in (url_prefix(), directory.name, name) if p]
    url = f"{image_base_url()}/{'/'.join(parts)}"
    if not _host_logged:
        _host_logged = True
        logger.info(f"图床已启用：对外地址 {image_base_url()}，本地目录 {static_dir()}")
    return url


def host_image(img, persistent: bool = False) -> str | None:
    """把 PIL 图片存成 PNG 后发布，返回公网 URL；失败返回 None。"""
    try:
        buf = BytesIO()
        img.save(buf, format="PNG")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"图床图片编码 PNG 失败：{e}")
        return None
    return host_bytes(buf.getvalue(), ".png", persistent=persistent)


def image_markdown(url: str, alt: str = "图片", width: int = 0, height: int = 0) -> str:
    """拼 markdown 图片语法 ``![说明 #宽px #高px](url)``（宽高为 0 时省略尺寸）。"""
    safe_alt = (alt or "图片").replace("[", "（").replace("]", "）").replace("\n", " ")
    size = f" #{int(width)}px #{int(height)}px" if width and height else ""
    return f"![{safe_alt}{size}]({url})"


MD_MAX_WIDTH = 720
MD_MAX_HEIGHT = 1080


def md_display_size(
    width: int,
    height: int,
    max_width: int = MD_MAX_WIDTH,
    max_height: int = MD_MAX_HEIGHT,
) -> tuple[int, int]:
    """算出 markdown 该回填的显示尺寸（等比，不放大）。

    Args:
        width: 原图宽。
        height: 原图高。
        max_width: 显示宽上限。
        max_height: 显示高上限。

    Returns:
        ``(显示宽, 显示高)``；宽高不合法时返回 ``(0, 0)``。
    """
    if width <= 0 or height <= 0:
        return 0, 0
    if width <= max_width and height <= max_height:
        return int(width), int(height)
    ratio = min(max_width / width, max_height / height)
    return max(1, int(width * ratio)), max(1, int(height * ratio))
