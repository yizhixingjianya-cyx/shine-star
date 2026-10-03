"""字体管理模块（由 cyxbot 的 util/font_manager.py 移植）。

统一管理插件内置字体文件，提供按字符回退的字体链，覆盖常用/生僻汉字、符号与 emoji。
字体文件放在插件目录 ``fonts/`` 下，与原项目保持一致。
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Sequence
from pathlib import Path

from PIL import ImageDraw, ImageFont

logger = logging.getLogger("astrbot")

# 字体目录：插件根目录下的 fonts/
FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"

# 字体映射表：别名 -> 文件名
FONT_MAP = {
    "cjk": "CJKUnified.ttf",
    "noto_sans": "NotoSans.ttf",
    "noto_sans_kr": "NotoSansKR.ttf",
    "noto_sans_arabic": "NotoSansArabic.ttf",
    "noto_sans_hebrew": "NotoSansHebrew.ttf",
    "noto_sans_thai": "NotoSansThai.ttf",
    "noto_sans_devanagari": "NotoSansDevanagari.ttf",
    "cubic": "Cubic.ttf",
    "cubic_11": "Cubic_11.ttf",
    "hanyi": "hanyi.otf",
    "mi": "mi.ttf",
}

# 默认回退链：主字体未覆盖的字符依次向后找
FALLBACK_FONTS = (
    "cjk",
    "noto_sans",
    "noto_sans_kr",
    "noto_sans_arabic",
    "noto_sans_hebrew",
    "noto_sans_thai",
    "noto_sans_devanagari",
)


def get_font_path(font_name: str) -> Path | None:
    """获取字体文件路径，不存在返回 None。

    Args:
        font_name: 字体别名或文件名。

    Returns:
        存在的字体文件路径，否则 None。
    """
    if font_name in FONT_MAP.values():
        font_path = FONTS_DIR / font_name
    else:
        font_file = FONT_MAP.get(font_name.lower())
        font_path = FONTS_DIR / font_file if font_file else FONTS_DIR / font_name

    if font_path.exists():
        return font_path

    logger.warning(f"字体文件不存在: {font_path}")
    return None


def load_font(font_name: str, size: int = 24) -> ImageFont.FreeTypeFont:
    """加载字体，失败回退 Pillow 内置字体。"""
    font_path = get_font_path(font_name)
    if font_path:
        try:
            return ImageFont.truetype(str(font_path), size)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"加载字体失败 {font_path}: {e}")
    return ImageFont.load_default()


@functools.cache
def _coverage(font_file: str) -> frozenset:
    """读取字体 cmap 码位集合（按文件名缓存）。"""
    try:
        from fontTools.ttLib import TTFont

        tt = TTFont(str(FONTS_DIR / font_file), lazy=True)
        try:
            return frozenset(tt.getBestCmap().keys())
        finally:
            tt.close()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"读取字体码表失败 {font_file}: {e}")
        return frozenset()


class FontChain:
    """按字符自动回退的多字体集合。"""

    def __init__(self, size: int, names: Sequence[str] = FALLBACK_FONTS) -> None:
        self.size = size
        self._fonts: list[tuple[ImageFont.FreeTypeFont, frozenset]] = []
        for name in names:
            path = get_font_path(name)
            if not path:
                continue
            try:
                font = ImageFont.truetype(str(path), size)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"加载字体失败 {path}: {e}")
                continue
            self._fonts.append((font, _coverage(path.name)))
        if not self._fonts:
            logger.warning("回退链中没有任何可用字体，使用 Pillow 内置位图字体")
            self._fonts.append((ImageFont.load_default(), frozenset()))
        self.path = (
            str(self._fonts[0][0].path) if hasattr(self._fonts[0][0], "path") else ""
        )

    @property
    def fonts(self) -> list[tuple[ImageFont.FreeTypeFont, frozenset]]:
        """回退链中的 (字体, 覆盖码位) 列表。"""
        return self._fonts

    def font_for(self, ch: str):
        """取能显示该字符的第一个字体。"""
        cp = ord(ch)
        for font, covered in self._fonts:
            if cp in covered:
                return font
        return self._fonts[0][0]

    def runs(self, text: str) -> list[tuple[ImageFont.FreeTypeFont, str]]:
        """把文本切成 ``[(字体, 连续片段)]``，相邻同字体自动合并。"""
        runs: list[list] = []
        for ch in text:
            font = self.font_for(ch)
            if runs and runs[-1][0] is font:
                runs[-1][1].append(ch)
            else:
                runs.append([font, [ch]])
        return [(font, "".join(chars)) for font, chars in runs]


class FallbackImageDraw(ImageDraw.ImageDraw):
    """支持字符级字体回退的 ImageDraw。"""

    def text(self, xy, text, fill=None, font=None, *args, **kwargs):
        if not isinstance(font, FontChain):
            return super().text(xy, text, fill, font, *args, **kwargs)
        if kwargs.get("anchor") is not None or (len(args) > 0 and args[0] is not None):
            return super().text(xy, text, fill, font.fonts[0][0], *args, **kwargs)

        x, y = xy
        result = (x, y)
        for sub_font, chunk in font.runs(str(text)):
            result = super().text((x, y), chunk, fill, sub_font, *args, **kwargs)
            x += self.textlength(chunk, font=sub_font)
        return result

    def textlength(self, text, font=None, *args, **kwargs):
        if not isinstance(font, FontChain):
            return super().textlength(text, font, *args, **kwargs)
        return sum(
            self.textlength(chunk, font=sub_font, *args, **kwargs)
            for sub_font, chunk in font.runs(str(text))
        )

    def textbbox(self, xy, text, font=None, *args, **kwargs):
        if not isinstance(font, FontChain):
            return super().textbbox(xy, text, font, *args, **kwargs)
        x, y = xy
        _, top, _, bottom = super().textbbox((0, 0), "汉", font.fonts[0][0])
        return (x, y + top, x + self.textlength(text, font=font), y + bottom)

    def multiline_textlength(self, text, font=None, spacing=4, *args, **kwargs):
        if not isinstance(font, FontChain):
            return super().multiline_textlength(text, font, spacing, *args, **kwargs)
        widths = [self.textlength(line, font=font) for line in str(text).split("\n")]
        return max(widths) if widths else 0

    def multiline_textbbox(
        self, xy, text, font=None, spacing=4, align="left", *args, **kwargs
    ):
        if not isinstance(font, FontChain):
            return super().multiline_textbbox(
                xy, text, font, spacing, align, *args, **kwargs
            )
        x, y = xy
        lines = str(text).split("\n")
        _, top, _, bottom = super().textbbox((0, 0), "汉", font.fonts[0][0])
        height = len(lines) * (font.size + spacing) - spacing if lines else 0
        return (
            x,
            y + top,
            x + self.multiline_textlength(text, font=font, spacing=spacing),
            y + height + bottom,
        )

    def multiline_text(
        self,
        xy,
        text,
        fill=None,
        font=None,
        anchor=None,
        spacing=4,
        align="left",
        direction=None,
        features=None,
        language=None,
        stroke_width=0,
        stroke_fill=None,
        embedded_color=False,
    ):
        if not isinstance(font, FontChain):
            return super().multiline_text(
                xy,
                text,
                fill,
                font,
                anchor,
                spacing,
                align,
                direction,
                features,
                language,
                stroke_width,
                stroke_fill,
                embedded_color,
            )
        x, y = xy
        lines = str(text).split("\n")
        widths = [self.textlength(line, font=font) for line in lines]
        block_width = max(widths) if widths else 0
        for line, line_width in zip(lines, widths):
            if align == "center":
                line_x = x + (block_width - line_width) / 2
            elif align == "right":
                line_x = x + (block_width - line_width)
            else:
                line_x = x
            self.text(
                (line_x, y),
                line,
                fill=fill,
                font=font,
                anchor=anchor,
                direction=direction,
                features=features,
                language=language,
                stroke_width=stroke_width,
                stroke_fill=stroke_fill,
                embedded_color=embedded_color,
            )
            y += font.size + spacing
        return x, y


def fallback_draw(image) -> ImageDraw.ImageDraw:
    """创建支持字体回退的画布对象。"""
    return FallbackImageDraw(image)


class Fonts:
    """字体快捷访问类。"""

    @staticmethod
    def cjk(size: int = 24) -> FontChain:
        """统一字体链：覆盖常用/生僻汉字、符号、emoji 及多国外语。"""
        return FontChain(size)

    @staticmethod
    def cubic(size: int = 24) -> ImageFont.FreeTypeFont:
        """Cubic 字体。"""
        return load_font("cubic", size)

    @staticmethod
    def hanyi(size: int = 24) -> ImageFont.FreeTypeFont:
        """汉仪字体。"""
        return load_font("hanyi", size)

    @staticmethod
    def mi(size: int = 24) -> ImageFont.FreeTypeFont:
        """小米字体。"""
        return load_font("mi", size)
