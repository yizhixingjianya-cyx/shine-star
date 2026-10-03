"""FTv 渲染层（由 cyxbot ftv 插件的 image_generator.py + render_v2.py 合并移植）。

对外提供：
- ``generate_user_info_image`` 单张档案卡片（640×896）
- ``generate_users_collage``   多用户拼图（3 列 × N 行）
- ``generate_profile_image``   档案详情大图（1000×1500）
- ``generate_species_stats_image`` 物种统计图（1000 宽，分页）
- ``generate_help_image``      帮助图（800 宽，高度随内容）

图片下载、铺满裁切、文本换行、磁盘缓存等公共能力集中在本模块底部。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

from .fonts import Fonts, fallback_draw
from .watermark import apply_all_watermarks

logger = logging.getLogger("astrbot")

__all__ = [
    "STATS_PER_PAGE",
    "generate_user_info_image",
    "generate_users_collage",
    "generate_profile_image",
    "generate_species_stats_image",
    "generate_help_image",
    "set_download_headers_provider",
]

# ------------------------------------------------------------------ 路径与缓存
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_IMAGE_CACHE_DIR = _PLUGIN_DIR / "cache" / "images"
_IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_NOPIC_PATH = _PLUGIN_DIR / "assets" / "nopic.png"
_NOPIC_CACHE: Image.Image | None = None

_CACHE_EXPIRY = 3600  # 秒
FONT_CACHE: dict[str, Any] = {}

# 下载展示图时附带认证头（由插件注入，取 accessToken）
_download_headers_provider = None


def set_download_headers_provider(provider) -> None:
    """注入「下载展示图请求头」的提供者（一般为 API 客户端的方法）。"""
    global _download_headers_provider
    _download_headers_provider = provider


def _init_font_cache() -> None:
    """初始化字体缓存。"""
    try:
        FONT_CACHE["title"] = Fonts.cjk(20)
        FONT_CACHE["content"] = Fonts.cjk(12)
        FONT_CACHE["small"] = Fonts.cjk(10)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"字体缓存初始化失败：{e}")
        FONT_CACHE["title"] = ImageFont.load_default()
        FONT_CACHE["content"] = ImageFont.load_default()
        FONT_CACHE["small"] = ImageFont.load_default()


_init_font_cache()


def _get_cache_key(prefix: str, urls: list) -> str:
    """生成缓存文件的唯一键。"""
    return f"{prefix}_{hashlib.md5('|'.join(urls).encode()).hexdigest()}"


def _load_cached_image(cache_key: str) -> Image.Image | None:
    """从缓存加载图片。"""
    cache_file = _IMAGE_CACHE_DIR / f"{cache_key}.png"
    if cache_file.exists():
        if time.time() - cache_file.stat().st_mtime < _CACHE_EXPIRY:
            try:
                return Image.open(cache_file)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"读取缓存图片失败：{e}")
                cache_file.unlink(missing_ok=True)
        else:
            cache_file.unlink(missing_ok=True)
    return None


def _save_to_cache(img: Image.Image, cache_key: str) -> None:
    """保存图片到缓存。"""
    try:
        img.save(_IMAGE_CACHE_DIR / f"{cache_key}.png", format="PNG")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"保存缓存图片失败：{e}")


# ------------------------------------------------------------------ 下载与裁切
def _load_nopic_image() -> Image.Image | None:
    """加载本地兜底图 nopic.png（带内存缓存，返回副本）。"""
    global _NOPIC_CACHE
    if _NOPIC_CACHE is None:
        try:
            img = Image.open(_NOPIC_PATH)
            img.load()
            _NOPIC_CACHE = img.convert("RGBA")
        except Exception as e:  # noqa: BLE001
            logger.error(f"加载兜底图 nopic.png 失败：{e}")
            return None
    return _NOPIC_CACHE.copy()


def _fit_cover(
    img: Image.Image,
    size: tuple[int, int],
    resample: Image.Resampling = Image.Resampling.LANCZOS,
) -> Image.Image:
    """等比缩放并居中裁剪到目标尺寸（铺满裁切，不拉伸变形）。"""
    target_w, target_h = size
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return img.resize(size, resample)
    ratio = max(target_w / src_w, target_h / src_h)
    scaled = img.resize(
        (max(target_w, round(src_w * ratio)), max(target_h, round(src_h * ratio))),
        resample,
    )
    left = (scaled.width - target_w) // 2
    top = (scaled.height - target_h) // 2
    return scaled.crop((left, top, left + target_w, top + target_h))


async def _download_and_cache_image(url: str, img_type: str = "图片") -> BytesIO | None:
    """下载并缓存图片（异步），返回校验过的 PNG BytesIO，失败 None。"""
    try:
        url = (url or "").strip("`").strip().replace(" ", "")
        if not url.startswith("http://") and not url.startswith("https://"):
            logger.error(f"下载{img_type}失败：URL 格式无效：{url}")
            return None

        filename = hashlib.md5(url.encode()).hexdigest() + ".png"
        cache_path = _IMAGE_CACHE_DIR / filename
        if cache_path.exists():
            try:
                data = cache_path.read_bytes()
                buf = BytesIO(data)
                img = Image.open(buf)
                img.load()
                out = BytesIO()
                img.save(out, format="PNG")
                out.seek(0)
                return out
            except Exception as e:  # noqa: BLE001
                logger.warning(f"读取{img_type}缓存失败：{e}")
                cache_path.unlink(missing_ok=True)

        headers = {}
        if _download_headers_provider is not None:
            try:
                headers = _download_headers_provider() or {}
            except Exception as e:  # noqa: BLE001
                logger.warning(f"获取下载请求头失败：{e}")

        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                content = resp.content
                if resp.status_code not in (200, 304):
                    logger.error(f"下载{img_type}失败：HTTP {resp.status_code} | {url}")
                    return None
        except Exception as e:  # noqa: BLE001
            logger.error(f"下载{img_type}失败：{e}")
            return None

        if not content or len(content) < 100:
            logger.error(f"下载{img_type}失败：内容为空或过短")
            return None

        try:
            img = Image.open(BytesIO(content))
            img.load()
        except Exception as e:  # noqa: BLE001
            logger.error(f"下载{img_type}失败：无效图片数据 {e}")
            return None

        try:
            img.save(cache_path, format="PNG")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"保存{img_type}缓存失败：{e}")

        out = BytesIO()
        img.save(out, format="PNG")
        out.seek(0)
        return out
    except Exception as e:  # noqa: BLE001
        logger.error(f"下载{img_type}失败：{e}")
        return None


async def _load_valid_image(
    img_url: str, img_type: str, profile_id: str
) -> Image.Image | None:
    """加载并验证图片，失败时统一返回兜底图 nopic.png。"""
    cached = await _download_and_cache_image(img_url, img_type)
    if cached:
        try:
            cached.seek(0)
            img = Image.open(cached)
            img.load()
            return img
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{img_type}缓存损坏，改用兜底图：{e}")
    logger.warning(f"{img_type}加载失败（档案 ID: {profile_id}），改用兜底图 nopic.png")
    return _load_nopic_image()


def _wrap_text_to_lines(draw, paragraph: str, font, max_width: int) -> list:
    """将单段文本按宽度自动换行（中文逐字断行）。"""
    lines: list = []
    current_line = ""
    for word in str(paragraph).split():
        if draw.textlength(word, font=font) <= max_width:
            test_line = f"{current_line} {word}".strip() if current_line else word
            if draw.textlength(test_line, font=font) <= max_width:
                current_line = test_line
            else:
                lines.append(current_line)
                current_line = word
        else:
            if current_line:
                lines.append(current_line)
                current_line = ""
            for ch in word:
                test_line = current_line + ch
                if draw.textlength(test_line, font=font) <= max_width:
                    current_line = test_line
                else:
                    lines.append(current_line)
                    current_line = ch
    if current_line:
        lines.append(current_line)
    return lines


def _draw_wrapped_text(
    draw,
    text: str,
    x: int,
    y: int,
    font,
    max_width: int,
    line_spacing: int,
    max_lines=None,
) -> int:
    """自动换行绘制文本，支持中文逐字断行、最大行数限制。返回结束 y。"""
    paragraphs = str(text).split("\n")
    all_lines: list = []
    for paragraph in paragraphs:
        if not paragraph.strip():
            continue
        all_lines.extend(_wrap_text_to_lines(draw, paragraph, font, max_width))

    if max_lines is not None and len(all_lines) > max_lines:
        all_lines = all_lines[:max_lines]
        last_line = all_lines[-1]
        while last_line and draw.textlength(f"{last_line}...", font=font) > max_width:
            last_line = last_line[:-1]
        all_lines[-1] = f"{last_line}..."

    current_y = y
    for line in all_lines:
        draw.text((x, current_y), line, font=font, fill="#333333")
        current_y += font.size + line_spacing
    return current_y


def _card_avatar_url(item: dict) -> str | None:
    """取卡片头像地址：优先 avatar_url，其次角色的 images[0]。"""
    url = item.get("avatar_url")
    if not url:
        images = item.get("images") or []
        if images:
            url = images[0]
    if isinstance(url, str):
        url = url.strip().strip("`")
    return url or None


# ====================================================================
# 调色板（v2）
# ====================================================================
PAGE_BG = "#F4F2EF"
SURFACE = "#FFFFFF"
INK = "#22201D"
INK2 = "#55504A"
INK3 = "#8B847C"
INK4 = "#ADA69E"
LINE = "#E2DED7"
MUTED = "#EDEAE5"
ACCENT = "#E2603F"
ACCENT_SOFT = "#FBEDE8"
BRAND_TEXT = "FURSUIT.TV DEV"

_FONT_CACHE: dict[int, Any] = {}


def _font(size: int):
    """按字号取统一字体链，带缓存。"""
    cached = _FONT_CACHE.get(size)
    if cached is None:
        try:
            cached = Fonts.cjk(size)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"加载 {size}px 字体失败：{e}")
            cached = ImageFont.load_default()
        _FONT_CACHE[size] = cached
    return cached


def _rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    r, g, b = ImageColor.getrgb(color)[:3]
    return (r, g, b, alpha)


def _tl(draw, text: Any, font) -> int:
    return int(draw.textlength(str(text), font=font))


def _vcenter(draw, font, text: Any, top: int, height: int) -> int:
    """让文本在 [top, top+height) 内竖直居中，返回 text() 应使用的 y。"""
    try:
        bbox = draw.textbbox((0, 0), str(text) or "汉", font=font)
        glyph_h = bbox[3] - bbox[1]
        return int(top + (height - glyph_h) / 2 - bbox[1])
    except Exception:  # noqa: BLE001
        size = int(getattr(font, "size", 20) or 20)
        return int(top + (height - size) / 2)


def _ellipsize(draw, text: Any, font, max_width: int) -> str:
    """按像素宽单行截断并补省略号。"""
    text = str(text)
    if max_width <= 0:
        return ""
    if _tl(draw, text, font) <= max_width:
        return text
    suffix = "…"
    while text and _tl(draw, text + suffix, font) > max_width:
        text = text[:-1]
    return f"{text}{suffix}" if text else ""


def _draw_right(draw, right: int, y: int, text: Any, font, fill) -> None:
    text = str(text)
    draw.text((right - _tl(draw, text, font), y), text, font=font, fill=fill)


def _hline(draw, y: int, x0: int, x1: int, color: str = LINE, width: int = 1) -> None:
    draw.rectangle([x0, y, x1 - 1, y + width - 1], fill=color)


def _paste_circle(
    canvas: Image.Image, img: Image.Image, x: int, y: int, size: int
) -> None:
    """铺满裁切后按圆形蒙版粘贴。"""
    fitted = _fit_cover(img, (size, size), Image.Resampling.LANCZOS).convert("RGB")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([(0, 0), (size - 1, size - 1)], fill=255)
    canvas.paste(fitted, (x, y), mask)


def _draw_eye(draw, x: int, y: int, color: str, w: int = 36, h: int = 24) -> None:
    """极简「眼睛」图形。"""
    draw.ellipse([x, y, x + w - 1, y + h - 1], outline=color, width=2)
    r = 4
    cx, cy = x + w // 2, y + h // 2
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)


EYE_W, EYE_H, EYE_GAP = 36, 24, 12


def _brief_date(value: Any) -> str:
    """日期字段裁剪为 YYYY-MM-DD。"""
    text = str(value or "").strip()
    if len(text) > 10 and "-" in text:
        return text[:10]
    return text


def _draw_paragraph(
    draw,
    x: int,
    y: int,
    text: Any,
    font,
    fill,
    max_width: int,
    line_height: int,
    max_lines: int,
) -> int:
    """按宽自动换行绘制段落，限制行数并补省略号。返回结束 y。"""
    content = str(text or "").strip()
    if not content:
        return y
    lines = _wrap_text_to_lines(draw, content, font, max_width)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and _tl(draw, last + "…", font) > max_width:
            last = last[:-1]
        lines[-1] = f"{last}…"
    cy = y
    for line in lines:
        draw.text((x, cy), line, font=font, fill=fill)
        cy += line_height
    return cy


# ====================================================================
# 一、档案卡片（设计 320×448，2 倍图输出 640×896）
# ====================================================================
CARD_W, CARD_H = 640, 896
CARD_RADIUS = 32
CARD_PAD = 40
CARD_GAP = 24

_TOPBAR_H = 80
_AVATAR_SIZE = 176
_AVATAR_X, _AVATAR_Y = 40, 104
_INFO_TOP, _INFO_ROW_H = 336, 52
_INFO_DIVIDER_TOP = 312
_INFO_DIVIDER_BOTTOM = 600
_BIO_TOP, _BIO_H = 624, 168
_BIO_BODY_TOP = 58
_BIO_LINE_H = 34
_BIO_MAX_LINES = max(1, (_BIO_H - _BIO_BODY_TOP) // _BIO_LINE_H)
_FOOTER_LINE_Y = 808

_TEXT_X = _AVATAR_X + _AVATAR_SIZE + 32
_TEXT_W = CARD_W - _TEXT_X - CARD_PAD


def _norm_card_item(item: dict) -> dict:
    """把「用户档案」与「角色(崽崽)」两种数据源归一化为 v2 卡片字段。"""
    item = item if isinstance(item, dict) else {}
    name = (
        item.get("nickname") or item.get("name") or item.get("username") or "未知昵称"
    )
    username = str(item.get("username") or "").strip()
    species = item.get("fursuit_species") or item.get("species") or ""
    gender = item.get("gender") or ""
    location = item.get("location") or ""
    birthday = item.get("birthday") or item.get("fursuit_birthday") or ""
    maker = item.get("fursuit_maker") or ""
    introduction = item.get("introduction") or ""
    worldview = item.get("worldview") or ""

    if worldview and not introduction:
        bio, bio_label = worldview, "设定"
    else:
        bio, bio_label = introduction, "简介"

    raw_id = item.get("id")
    ident = f"#{raw_id}" if raw_id not in (None, "") else "#—"
    quick = " · ".join(str(v) for v in (species, gender, location) if v)

    return {
        "ident": ident,
        "name": str(name),
        "username": username,
        "quick": quick,
        "species": str(species) if species else "未填写",
        "gender": str(gender) if gender else "未公开",
        "location": str(location) if location else "未知",
        "birthday": _brief_date(birthday) or "无",
        "maker": str(maker) if maker else "未知",
        "view_count": item.get("view_count"),
        "bio": str(bio).strip(),
        "bio_label": bio_label,
    }


def _render_card(item: dict, avatar: Image.Image | None) -> Image.Image:
    """绘制单张 v2 档案卡片（640×896，四角透明便于拼图留白）。"""
    norm = _norm_card_item(item)
    card = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    draw = fallback_draw(card)

    draw.rounded_rectangle(
        [(0, 0), (CARD_W - 1, CARD_H - 1)],
        radius=CARD_RADIUS,
        fill=SURFACE,
        outline=LINE,
        width=2,
    )

    # 顶栏
    f_brand = _font(24)
    dot = 16
    dot_y = (_TOPBAR_H - dot) // 2
    draw.ellipse([CARD_PAD, dot_y, CARD_PAD + dot, dot_y + dot], fill=ACCENT)
    draw.text(
        (CARD_PAD + dot + 16, _vcenter(draw, f_brand, BRAND_TEXT, 0, _TOPBAR_H)),
        BRAND_TEXT,
        font=f_brand,
        fill=INK2,
    )
    f_id = _font(24)
    _draw_right(
        draw,
        CARD_W - CARD_PAD,
        _vcenter(draw, f_id, norm["ident"], 0, _TOPBAR_H),
        norm["ident"],
        f_id,
        INK3,
    )
    _hline(draw, _TOPBAR_H, 0, CARD_W, width=2)

    # 头像
    draw.ellipse(
        [
            _AVATAR_X - 4,
            _AVATAR_Y - 4,
            _AVATAR_X + _AVATAR_SIZE + 4,
            _AVATAR_Y + _AVATAR_SIZE + 4,
        ],
        fill=ACCENT_SOFT,
    )
    if avatar is not None:
        try:
            _paste_circle(card, avatar, _AVATAR_X, _AVATAR_Y, _AVATAR_SIZE)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"头像粘贴失败，保留占位圆：{e}")

    # 昵称 / 用户名 / 速览
    f_nick = _font(40)
    draw.text(
        (_TEXT_X, _vcenter(draw, f_nick, norm["name"], 104, 68)),
        _ellipsize(draw, norm["name"], f_nick, _TEXT_W),
        font=f_nick,
        fill=INK,
    )
    if norm["username"]:
        f_user = _font(26)
        draw.text(
            (_TEXT_X, _vcenter(draw, f_user, norm["username"], 172, 36)),
            _ellipsize(draw, f"@{norm['username']}", f_user, _TEXT_W),
            font=f_user,
            fill=INK3,
        )
    if norm["quick"]:
        f_quick = _font(24)
        draw.text(
            (_TEXT_X, _vcenter(draw, f_quick, norm["quick"], 208, 36)),
            _ellipsize(draw, norm["quick"], f_quick, _TEXT_W),
            font=f_quick,
            fill=INK2,
        )

    # 信息区
    _hline(draw, _INFO_DIVIDER_TOP, 0, CARD_W, width=2)
    _hline(draw, _INFO_DIVIDER_BOTTOM, 0, CARD_W, width=2)
    f_label = _font(24)
    f_value = _font(28)
    rows = [
        ("物种", norm["species"]),
        ("性别", norm["gender"]),
        ("地区", norm["location"]),
        ("生日", norm["birthday"]),
        ("制作", norm["maker"]),
    ]
    for index, (label, value) in enumerate(rows):
        row_y = _INFO_TOP + index * _INFO_ROW_H
        baseline = _vcenter(draw, f_value, "汉", row_y, _INFO_ROW_H)
        draw.text((CARD_PAD, baseline), label, font=f_label, fill=INK3)
        _draw_right(
            draw,
            CARD_W - CARD_PAD,
            baseline,
            _ellipsize(draw, value, f_value, _TEXT_W + 100),
            f_value,
            INK,
        )
        if index < len(rows) - 1:
            _hline(draw, row_y + _INFO_ROW_H, CARD_PAD, CARD_W - CARD_PAD, width=2)

    # 简介块
    if norm["bio"]:
        bio_x0, bio_x1 = CARD_PAD, CARD_W - CARD_PAD
        draw.rounded_rectangle(
            [(bio_x0, _BIO_TOP), (bio_x1, _BIO_TOP + _BIO_H)], radius=16, fill=MUTED
        )
        draw.text(
            (bio_x0 + 24, _BIO_TOP + 22), norm["bio_label"], font=_font(24), fill=ACCENT
        )
        _draw_paragraph(
            draw,
            bio_x0 + 24,
            _BIO_TOP + _BIO_BODY_TOP,
            norm["bio"],
            _font(24),
            INK2,
            max_width=(bio_x1 - bio_x0) - 48,
            line_height=_BIO_LINE_H,
            max_lines=_BIO_MAX_LINES,
        )

    # 页脚
    _hline(draw, _FOOTER_LINE_Y, 0, CARD_W)
    f_src = _font(22)
    draw.text(
        (
            CARD_PAD,
            _vcenter(
                draw,
                f_src,
                "VDS API · starfure",
                _FOOTER_LINE_Y,
                CARD_H - _FOOTER_LINE_Y,
            ),
        ),
        "VDS API · starfure",
        font=f_src,
        fill=INK4,
    )
    if norm["view_count"] not in (None, ""):
        f_num = _font(24)
        num_text = str(norm["view_count"])
        num_y = _vcenter(draw, f_num, num_text, _FOOTER_LINE_Y, CARD_H - _FOOTER_LINE_Y)
        _draw_right(draw, CARD_W - CARD_PAD, num_y, num_text, f_num, ACCENT)
        num_bbox = draw.textbbox((0, 0), num_text, font=f_num)
        eye_center = num_y + (num_bbox[1] + num_bbox[3]) / 2
        _draw_eye(
            draw,
            CARD_W - CARD_PAD - _tl(draw, num_text, f_num) - EYE_W - EYE_GAP,
            int(eye_center - EYE_H / 2),
            INK4,
            w=EYE_W,
            h=EYE_H,
        )

    return card


async def generate_user_info_image(item: dict) -> Image.Image | None:
    """生成单张 v2 档案卡片（640×896）。失败返回 None。"""
    try:
        label = (
            item.get("username")
            or item.get("name")
            or item.get("nickname")
            or "unknown"
        )
        avatar = await _load_valid_image(
            img_url=_card_avatar_url(item) or "",
            img_type="头像",
            profile_id=str(item.get("id") or label),
        )
        card = _render_card(item, avatar)
        watermarked, _ = apply_all_watermarks(
            card, watermark_text=f"FTV-{label}", blind_wm_text=f"FTV-Copyright-{label}"
        )
        return watermarked
    except Exception as e:  # noqa: BLE001
        logger.error(f"生成档案卡片失败：{e}", exc_info=True)
        return None


async def generate_users_collage(items: list, columns: int = 3) -> Image.Image | None:
    """生成多用户拼图（列数默认 3，卡片间距 24）。"""
    try:
        items = [it for it in (items or []) if isinstance(it, dict)]
        if not items:
            return None

        num = len(items)
        columns = max(1, min(int(columns), num))
        rows = (num + columns - 1) // columns
        width = columns * CARD_W + (columns - 1) * CARD_GAP
        height = rows * CARD_H + (rows - 1) * CARD_GAP

        async def _fetch(item: dict) -> Image.Image | None:
            try:
                return await _load_valid_image(
                    img_url=_card_avatar_url(item) or "",
                    img_type="头像",
                    profile_id=str(item.get("id") or item.get("name") or "unknown"),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"拼图头像加载失败：{e}")
                return None

        avatars = await asyncio.gather(*[_fetch(it) for it in items])

        collage = Image.new("RGBA", (width, height), _rgba(PAGE_BG))
        for index, (item, avatar) in enumerate(zip(items, avatars)):
            card = _render_card(item, avatar)
            row, col = divmod(index, columns)
            collage.paste(
                card, (col * (CARD_W + CARD_GAP), row * (CARD_H + CARD_GAP)), card
            )
        return collage
    except Exception as e:  # noqa: BLE001
        logger.error(f"生成拼图失败：{e}", exc_info=True)
        return None


# ====================================================================
# 二、档案详情大图（1000×1500）
# ====================================================================
PROFILE_W, PROFILE_H = 1000, 1500
_PANEL_X, _PANEL_Y = 60, 60
_PANEL_W, _PANEL_H = 880, 1380
_PANEL_PAD = 48
_PROFILE_AVATAR = 200
_BLUR_RADIUS = 12
_MASK_ALPHA = 229


async def generate_profile_image(
    vertical_img_url: str,
    avatar_url: str,
    horizontal_img_url: str,
    showcase_other_url: str,
    profile_data: dict,
    title_text: str = "兽频道档案",
) -> Image.Image | None:
    """生成 v2 档案详情大图（1000×1500）。失败返回 None。"""
    profile_data = profile_data if isinstance(profile_data, dict) else {}
    pid = profile_data.get("id", "unknown")
    try:
        cache_key = _get_cache_key(
            "profile_v2",
            [
                vertical_img_url,
                avatar_url,
                horizontal_img_url,
                showcase_other_url,
                str(pid),
            ],
        )
        cached = _load_cached_image(cache_key)
        if cached:
            return cached

        img_v = await _load_valid_image(vertical_img_url, "竖版背景图", str(pid))
        if img_v is None:
            logger.error(f"档案 {pid} 背景图缺失，终止")
            return None
        base = _fit_cover(img_v, (int(PROFILE_W * 1.08), int(PROFILE_H * 1.08)))
        base = base.filter(ImageFilter.GaussianBlur(_BLUR_RADIUS)).resize(
            (PROFILE_W, PROFILE_H), Image.Resampling.LANCZOS
        )
        canvas = base.convert("RGBA")
        canvas = Image.alpha_composite(
            canvas,
            Image.new("RGBA", (PROFILE_W, PROFILE_H), _rgba(PAGE_BG, _MASK_ALPHA)),
        )

        panel = Image.new("RGBA", (_PANEL_W, _PANEL_H), (0, 0, 0, 0))
        pd = fallback_draw(panel)
        pd.rounded_rectangle(
            [(0, 0), (_PANEL_W - 1, _PANEL_H - 1)],
            radius=20,
            fill=SURFACE,
            outline=LINE,
            width=1,
        )

        left = _PANEL_PAD
        right = _PANEL_W - _PANEL_PAD
        inner_w = right - left

        # 顶栏
        brand = str(title_text).strip() or BRAND_TEXT
        f_brand = _font(20)
        dot, row_h, row_top = 12, 40, 48
        draw_y = row_top + (row_h - dot) // 2
        pd.ellipse([left, draw_y, left + dot, draw_y + dot], fill=ACCENT)
        pd.text(
            (left + dot + 10, _vcenter(pd, f_brand, brand, row_top, row_h)),
            _ellipsize(pd, brand, f_brand, inner_w - 200),
            font=f_brand,
            fill=INK2,
        )
        f_ident = _font(22)
        _draw_right(
            pd,
            right,
            _vcenter(pd, f_ident, str(pid), row_top, row_h),
            f"#{pid}",
            f_ident,
            INK3,
        )
        _hline(pd, 104, left, right)

        # 头像 + 昵称
        av_x, av_y = left, 132
        pd.ellipse(
            [
                av_x - 4,
                av_y - 4,
                av_x + _PROFILE_AVATAR + 4,
                av_y + _PROFILE_AVATAR + 4,
            ],
            fill=ACCENT_SOFT,
        )
        img_a = await _load_valid_image(avatar_url, "头像", str(pid))
        if img_a is not None:
            try:
                _paste_circle(panel, img_a, av_x, av_y, _PROFILE_AVATAR)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"档案头像粘贴失败：{e}")

        name = str(
            profile_data.get("nickname") or profile_data.get("name") or "未知昵称"
        )
        f_name = _font(40)
        pd.text(
            (left + _PROFILE_AVATAR + 40, 150),
            _ellipsize(pd, name, f_name, inner_w - 280),
            font=f_name,
            fill=INK,
        )

        username = str(profile_data.get("username") or "")
        if username:
            f_user = _font(24)
            pd.text(
                (left + _PROFILE_AVATAR + 40, 205),
                _ellipsize(pd, f"@{username}", f_user, inner_w - 280),
                font=f_user,
                fill=INK3,
            )

        intro = str(profile_data.get("introduction") or "无")
        _draw_paragraph(
            pd,
            left + _PROFILE_AVATAR + 40,
            250,
            intro,
            _font(22),
            INK2,
            inner_w - 280,
            32,
            4,
        )

        # 信息格 3×2
        grid_top = 372
        cell_w = inner_w // 3
        cell_h = 90
        fields = [
            ("物种", profile_data.get("fursuit_species") or "未填写"),
            ("地区", profile_data.get("location") or "未知"),
            ("制作", profile_data.get("fursuit_maker") or "未知"),
            ("生日", _brief_date(profile_data.get("fursuit_birthday")) or "无"),
            (
                "浏览",
                profile_data.get("view_count")
                if profile_data.get("view_count") is not None
                else "—",
            ),
            ("ID", pid),
        ]
        for idx, (label, value) in enumerate(fields):
            r, c = divmod(idx, 3)
            cx = left + c * cell_w
            cy = grid_top + r * cell_h
            pd.text((cx, cy), label, font=_font(20), fill=INK3)
            pd.text(
                (cx, cy + 30),
                _ellipsize(pd, value, _font(26), cell_w - 20),
                font=_font(26),
                fill=INK,
            )

        # 展示图（横版 + 方形）
        show_top = grid_top + 2 * cell_h + 20
        img_h = await _load_valid_image(horizontal_img_url, "横版展示图", str(pid))
        if img_h is not None:
            try:
                fitted = _fit_cover(img_h, (inner_w, 320))
                panel.paste(fitted, (left, show_top))
                show_top += 340
            except Exception as e:  # noqa: BLE001
                logger.warning(f"横版展示图粘贴失败：{e}")

        img_o = await _load_valid_image(showcase_other_url, "方形展示图", str(pid))
        if img_o is not None and show_top < _PANEL_H - 240:
            try:
                fitted = _fit_cover(img_o, (240, 240))
                panel.paste(fitted, (left, show_top))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"方形展示图粘贴失败：{e}")

        canvas.alpha_composite(panel, (_PANEL_X, _PANEL_Y))

        # 页脚
        fd = fallback_draw(canvas)
        fd.text(
            (_PANEL_X + _PANEL_PAD, PROFILE_H - 42),
            "VDS API · starfure",
            font=_font(20),
            fill=INK4,
        )

        watermarked, _ = apply_all_watermarks(
            canvas, watermark_text=f"FTV-{pid}", blind_wm_text=f"FTV-Copyright-{pid}"
        )
        _save_to_cache(watermarked, cache_key)
        return watermarked
    except Exception as e:  # noqa: BLE001
        logger.error(f"生成档案大图失败：{e}", exc_info=True)
        return None


# ====================================================================
# 三、物种统计图（1000 宽，分页）
# ====================================================================
STATS_PER_PAGE = 2000


async def generate_species_stats_image(
    species_list: list,
    total: int,
    title: str = "兽频道 物种统计",
    page: int | None = None,
) -> list[Image.Image] | None:
    """生成统计图片（四列布局，带竖向分割线）。

    Args:
        species_list: ``[{"species": ..., "count": ...}, ...]``。
        total: 档案总数（展示用）。
        title: 标题。
        page: 为 None 时生成全部分页；指定页（从 1 开始）只生成该页。

    Returns:
        图片列表，失败返回 None。
    """
    try:
        total_species = len(species_list)
        all_starts = list(range(0, total_species, STATS_PER_PAGE))
        if not all_starts:
            return []
        total_pages = len(all_starts)
        if page is not None:
            if not 1 <= page <= total_pages:
                return None
            all_starts = all_starts[page - 1 : page]

        images = []
        for page_idx, page_start in enumerate(all_starts, 1):
            page_no = page if page is not None else page_idx
            page_data = species_list[page_start : page_start + STATS_PER_PAGE]
            page_len = len(page_data)

            width = 1000
            row_height = 25
            header_height = 80
            footer_height = 50
            columns = 4
            column_width = width // columns
            column_padding = 15

            rows_needed = (page_len + columns - 1) // columns
            actual_height = header_height + rows_needed * row_height + footer_height

            image = Image.new("RGBA", (width, actual_height), (255, 255, 255, 255))
            draw = fallback_draw(image)
            title_font = FONT_CACHE.get("title", ImageFont.load_default())
            content_font = FONT_CACHE.get("small", ImageFont.load_default())

            title_width = draw.textlength(title, font=title_font)
            draw.text(
                ((width - title_width) // 2, 20), title, font=title_font, fill="#333333"
            )
            page_text = f"第 {page_no}/{total_pages} 页"
            page_w = draw.textlength(page_text, font=content_font)
            draw.text(
                (width - page_w - 15, 25), page_text, font=content_font, fill="#999999"
            )

            draw.line(
                [(0, header_height - 10), (width, header_height - 10)],
                fill="#e9ecef",
                width=1,
            )
            for col in range(1, columns):
                x = col * column_width
                draw.line(
                    [(x, header_height), (x, actual_height - footer_height)],
                    fill="#e9ecef",
                    width=1,
                )

            start_y = header_height
            for row in range(rows_needed):
                row_y = start_y + row * row_height
                for col in range(columns):
                    idx_in_page = row * columns + col
                    if idx_in_page >= page_len:
                        break
                    real_num = page_start + idx_in_page + 1
                    col_s = col * column_width + column_padding
                    col_e = (col + 1) * column_width - column_padding
                    sp = page_data[idx_in_page]
                    name = sp.get("species", "未知")
                    cnt = sp.get("count", 0)
                    show_text = f"{real_num}. {name}"
                    if len(show_text) > 20:
                        show_text = show_text[:17] + "..."
                    draw.text(
                        (col_s, row_y), show_text, font=content_font, fill="#333333"
                    )
                    cnt_str = str(cnt)
                    cnt_w = draw.textlength(cnt_str, font=content_font)
                    draw.text(
                        (col_e - cnt_w, row_y),
                        cnt_str,
                        font=content_font,
                        fill="#333333",
                    )

            footer_text = "VDS API - starfure"
            foot_w = draw.textlength(footer_text, font=content_font)
            draw.text(
                ((width - foot_w) // 2, actual_height - 30),
                footer_text,
                font=content_font,
                fill="#999999",
            )
            images.append(image)

        return images
    except Exception as e:  # noqa: BLE001
        logger.error(f"生成物种统计图片失败：{e}", exc_info=True)
        return None


# ====================================================================
# 四、帮助图（800 宽，高度随内容增长）
# ====================================================================
HELP_W = 800
_HELP_MIN_H = 1000
_HELP_TITLE = "兽频道插件帮助"
_HELP_FOOTER = "—— furtv API from vds，plugin by starfure ——"


def generate_help_image(help_content: str) -> str | None:
    """生成帮助图片，返回图片路径；失败返回 None。"""
    try:
        title_font = Fonts.cjk(36)
        content_font = Fonts.cjk(24)

        margin = 60
        max_width = HELP_W - 2 * margin
        line_h = 34

        # 先按宽换行，算出高度
        probe = Image.new("RGBA", (HELP_W, 10))
        pdraw = fallback_draw(probe)
        wrapped: list[str] = []
        for paragraph in help_content.split("\n"):
            if not paragraph.strip():
                wrapped.append("")
                continue
            wrapped.extend(
                _wrap_text_to_lines(pdraw, paragraph, content_font, max_width)
            )
        content_h = len(wrapped) * (line_h + 8)
        height = max(_HELP_MIN_H, 140 + content_h + 80)

        background = Image.new("RGBA", (HELP_W, height), (245, 245, 245, 255))
        draw = fallback_draw(background)

        title_bbox = draw.textbbox((0, 0), _HELP_TITLE, font=title_font)
        title_x = (HELP_W - (title_bbox[2] - title_bbox[0])) // 2
        draw.text(
            (title_x, 20),
            _HELP_TITLE,
            font=title_font,
            fill=(255, 50, 50),
            align="center",
        )

        cy = 100
        for line in wrapped:
            if line:
                draw.text((margin, cy), line, font=content_font, fill=(30, 30, 30))
            cy += line_h + 8

        footer_bbox = draw.textbbox((0, 0), _HELP_FOOTER, font=content_font)
        footer_x = (HELP_W - (footer_bbox[2] - footer_bbox[0])) // 2
        draw.text(
            (footer_x, height - (footer_bbox[3] - footer_bbox[1]) - 20),
            _HELP_FOOTER,
            font=content_font,
            fill=(100, 100, 100),
        )

        output_path = _PLUGIN_DIR / "cache" / "help_output.png"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        background.convert("RGB").save(output_path)
        return str(output_path)
    except Exception as e:  # noqa: BLE001
        logger.error(f"生成帮助图片失败：{e}", exc_info=True)
        return None
