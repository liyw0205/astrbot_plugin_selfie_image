from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from astrbot_plugin_selfie_image.core.models import AICatConfig
from astrbot_plugin_selfie_image.core.utils import (
    data_url_to_bytes,
    image_geometry_error,
    image_validation_limits,
)


def encoded_image(image: Image.Image, fmt: str = "PNG", **kwargs) -> bytes:
    output = BytesIO()
    image.save(output, format=fmt, **kwargs)
    return output.getvalue()


def test_image_geometry_accepts_normal_png() -> None:
    data = encoded_image(Image.new("RGB", (120, 80), "red"))
    assert image_geometry_error(data) == ""
    decoded, mime = data_url_to_bytes(
        "data:image/png;base64," + __import__("base64").b64encode(data).decode("ascii")
    )
    assert decoded == data
    assert mime == "image/png"


def test_image_geometry_rejects_dimension_and_pixel_limits() -> None:
    wide = encoded_image(Image.new("RGB", (2048, 2), "blue"))
    assert image_geometry_error(wide, max_dimension=1024, max_pixels=10_000_000) == "too_large_dimensions"

    dense = encoded_image(Image.new("RGB", (512, 512), "green"))
    assert image_geometry_error(dense, max_dimension=1024, max_pixels=100_000) == "too_many_pixels"


def test_image_geometry_rejects_animated_frame_limit() -> None:
    frames = [Image.new("RGBA", (2, 2), (index, 0, 0, 255)) for index in range(9)]
    data = encoded_image(frames[0], "GIF", save_all=True, append_images=frames[1:], loop=0, duration=1)
    assert image_geometry_error(data, max_frames=8) == "too_many_frames"


def test_image_limits_use_config_overrides_and_model_defaults() -> None:
    limits = image_validation_limits(
        SimpleNamespace(
            image_max_image_dimension=2048,
            image_max_image_pixels=2_000_000,
            image_max_animation_frames=3,
        )
    )
    assert limits == (2048, 2_000_000, 3)

    config = AICatConfig.from_dict(
        {
            "image": {
                "max_image_dimension": 2048,
                "max_image_pixels": 2_000_000,
                "max_animation_frames": 3,
            }
        }
    )
    assert image_validation_limits(config) == (2048, 2_000_000, 3)
