"""隐形水印模块（由 cyxbot 的 ftv/watermark.py 移植）。

仅提供基于 PIL 的低透明度平铺隐形水印。
"""

from __future__ import annotations

import logging

from PIL import Image, ImageFont

from .fonts import Fonts, fallback_draw

logger = logging.getLogger("astrbot")


class WatermarkGenerator:
    """水印生成器（仅隐形水印）。"""

    @classmethod
    def add_invisible_watermark(
        cls,
        image: Image.Image,
        watermark_text: str = "CYX-bot-mark",
        opacity: float = 0.05,
    ) -> Image.Image:
        """添加隐形水印（低透明度全屏平铺）。

        Args:
            image: PIL 图片对象。
            watermark_text: 水印文字。
            opacity: 透明度 (0-1)。

        Returns:
            添加水印后的图片。
        """
        try:
            image = image.convert("RGBA")
            width, height = image.size
            overlay = Image.new("RGBA", image.size, (255, 255, 255, 0))
            draw = fallback_draw(overlay)

            try:
                font = Fonts.cjk(40)
            except Exception:  # noqa: BLE001
                font = ImageFont.load_default()

            alpha = int(255 * opacity)
            watermark_color = (255, 255, 255, alpha)

            spacing = 200
            for y in range(0, height, spacing):
                for x in range(0, width, spacing):
                    offset_x = (x + y // 2) % spacing
                    draw.text(
                        (x + offset_x, y),
                        watermark_text,
                        font=font,
                        fill=watermark_color,
                    )

            return Image.alpha_composite(image, overlay)
        except Exception as e:  # noqa: BLE001
            logger.error(f"添加隐形水印失败：{e}")
            return image


def apply_all_watermarks(
    image: Image.Image,
    watermark_text: str = "CYX-bot-mark",
    blind_wm_text: str = "CYX-bot",
) -> tuple[Image.Image, int]:
    """应用水印（当前仅隐形水印；盲水印已移除）。

    Args:
        image: PIL 图片对象。
        watermark_text: 隐形水印文字。
        blind_wm_text: 兼容参数（已废弃）。

    Returns:
        ``(添加水印后的图片, 0)``。
    """
    final_image = WatermarkGenerator.add_invisible_watermark(
        image, watermark_text=watermark_text, opacity=0.03
    )
    return final_image, 0
