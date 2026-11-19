"""Wrap outgoing images in APNG with a separate blurred static fallback."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageEnhance, ImageOps

from ..core.utils import DEFAULT_MAX_ANIMATION_FRAMES, DEFAULT_MAX_IMAGE_DIMENSION, DEFAULT_MAX_IMAGE_PIXELS
from .help_poster import _fallback_font


def _preview_cover(image: Image.Image) -> Image.Image:
    background = Image.new("RGBA", image.size, (32, 32, 32, 255))
    background.alpha_composite(image)
    small = background.convert("RGB")
    small.thumbnail((16, 16), Image.Resampling.BOX)
    cover = ImageEnhance.Brightness(small.resize(image.size, Image.Resampling.BILINEAR)).enhance(0.45)
    draw = ImageDraw.Draw(cover)
    text = "点击查看原图"
    size = min(72, max(10, image.width // 16))
    while size >= 10:
        font = _fallback_font(size, bold=True)
        try:
            left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        except UnicodeEncodeError:
            text = "View original"
            left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        if right - left <= image.width * 0.85 and bottom - top <= image.height * 0.7:
            draw.text(((image.width - (right - left)) / 2 - left,
                       (image.height - (bottom - top)) / 2 - top),
                      text, font=font, fill="white")
            break
        size -= 1
    return cover.convert("RGBA")


def build_hidden_preview(
    path: str,
    *,
    max_dimension: int = DEFAULT_MAX_IMAGE_DIMENSION,
    max_pixels: int = DEFAULT_MAX_IMAGE_PIXELS,
    max_frames: int = DEFAULT_MAX_ANIMATION_FRAMES,
) -> bytes:
    """Keep the full decoded image while changing only its static preview."""
    with Image.open(path) as opened:
        width, height = opened.size
        if max(width, height) > max_dimension or width * height > max_pixels:
            raise ValueError("预览隐藏封装失败：图片尺寸超出限制")
        frame_count = getattr(opened, "n_frames", 1)
        if frame_count > max_frames:
            raise ValueError("预览隐藏封装失败：动画帧数超出限制")
        start = int(bool(opened.info.get("default_image")))
        loop = int(opened.info.get("loop", 0))
        if opened.format == "GIF" and ("loop" not in opened.info or loop > 0):
            # GIF counts repetitions; APNG counts total plays.
            loop += 1
        frames = []
        durations = []
        for index in range(start, frame_count):
            opened.seek(index)
            frames.append(ImageOps.exif_transpose(opened).convert("RGBA"))
            duration = opened.info.get("duration", 100)
            durations.append(100 if duration is None else max(0, float(duration)))
    if not frames:
        raise ValueError("预览隐藏封装失败：没有可显示的图片")
    cover = _preview_cover(frames[0])
    blends = [0] * len(frames)
    if len(frames) == 1:
        # The transparent OVER frame keeps the reveal visible without flicker.
        frames.append(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
        durations = [100, 100]
        blends.append(1)
        loop = 0
    output = BytesIO()
    cover.save(output, "PNG", save_all=True, default_image=True,
               append_images=frames, duration=durations, disposal=[0] * len(frames),
               blend=blends, loop=loop, compress_level=6)
    return output.getvalue()
