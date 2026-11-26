"""Hidden-preview encoding and outgoing-image delivery contracts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

from astrbot_plugin_selfie_image.core.models import AICatConfig
from astrbot_plugin_selfie_image.features.config_manager import ConfigurationMixin
from astrbot_plugin_selfie_image.features.reference_media import ReferenceMediaMixin
from astrbot_plugin_selfie_image.generation.preview_hidden import build_hidden_preview
from astrbot_plugin_selfie_image.generation.prompt_lookup import image_fingerprint


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
def test_static_fallback_differs_but_reveal_keeps_full_decoded_pixels(tmp_path, format):
    path = tmp_path / ("generated." + format.lower())
    image = Image.new("RGB", (180, 300), "#aeddcc")
    image.paste("#8e4272", (10, 20, 100, 200))
    image.save(path, format)
    before = path.read_bytes()
    with Image.open(path) as original:
        expected = ImageOps.exif_transpose(original).convert("RGBA").tobytes()
    encoded = build_hidden_preview(str(path))
    with Image.open(BytesIO(encoded)) as result:
        assert result.size == (180, 300)
        assert result.info["default_image"] is True
        assert result.n_frames == 3
        cover = result.convert("RGBA").tobytes()
        assert cover != expected
        result.seek(1)
        assert result.convert("RGBA").tobytes() == expected
        result.seek(2)
        assert result.convert("RGBA").tobytes() == expected
    assert path.read_bytes() == before
    assert image_fingerprint(encoded) == image_fingerprint(before)


def test_transparency_and_exif_orientation_are_preserved(tmp_path):
    path = tmp_path / "transparent.png"
    original = Image.new("RGBA", (60, 100), (50, 90, 180, 75))
    exif = Image.Exif()
    exif[274] = 6
    original.save(path, exif=exif)
    with Image.open(path) as opened:
        expected = ImageOps.exif_transpose(opened).convert("RGBA")
    with Image.open(BytesIO(build_hidden_preview(str(path)))) as result:
        assert result.size == expected.size
        result.seek(1)
        assert result.convert("RGBA").tobytes() == expected.tobytes()


def test_animation_frames_durations_and_loop_are_preserved(tmp_path):
    path = tmp_path / "animated.png"
    first = Image.new("RGBA", (60, 100), "red")
    second = Image.new("RGBA", (60, 100), "blue")
    first.save(path, save_all=True, append_images=[second], duration=[70, 230], loop=2)
    with Image.open(BytesIO(build_hidden_preview(str(path)))) as result:
        assert result.info["default_image"] is True
        assert result.info["loop"] == 2
        for index, frame, duration in [(1, first, 70), (2, second, 230)]:
            result.seek(index)
            assert result.info["duration"] == duration
            assert result.convert("RGBA").tobytes() == frame.tobytes()


def test_existing_default_image_is_replaced_without_revealing_the_old_cover(tmp_path):
    path = tmp_path / "wrapped.png"
    cover = Image.new("RGBA", (60, 100), "black")
    reveal = Image.new("RGBA", cover.size, "red")
    cover.save(path, save_all=True, default_image=True, append_images=[reveal], duration=100)
    with Image.open(BytesIO(build_hidden_preview(str(path)))) as result:
        result.seek(1)
        assert result.convert("RGBA").tobytes() == reveal.tobytes()


def test_cover_remains_available_without_unicode_font_support(tmp_path):
    path = tmp_path / "generated.png"
    Image.new("RGB", (180, 300), "red").save(path)
    font = getattr(ImageFont, "load_default_imagefont", ImageFont.load_default)()
    with patch("astrbot_plugin_selfie_image.generation.preview_hidden._font", return_value=font):
        encoded = build_hidden_preview(str(path))
    with Image.open(BytesIO(encoded)) as result:
        assert result.info["default_image"] is True
        result.seek(1)
        assert result.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


@pytest.mark.parametrize("font_size", [None, 48], ids=["bitmap-fallback", "missing-chinese-glyphs"])
def test_missing_chinese_glyphs_use_readable_centered_english(tmp_path, monkeypatch, font_size):
    path = tmp_path / "generated.png"
    Image.new("RGB", (1024, 1536), "red").save(path)
    font = (getattr(ImageFont, "load_default_imagefont", ImageFont.load_default)()
            if font_size is None else ImageFont.load_default(size=font_size))
    drawn = []
    draw_text = ImageDraw.ImageDraw.text

    def capture(draw, xy, text, *args, **kwargs):
        drawn.append(text)
        return draw_text(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    encoded = build_hidden_preview(str(path), font_loader=lambda *_args, **_kwargs: font)
    with Image.open(BytesIO(encoded)) as result:
        cover = result.convert("RGB")
        bounds = ImageChops.difference(cover, Image.new("RGB", cover.size, cover.getpixel((0, 0)))).getbbox()
        assert drawn == ["View original"]
        assert bounds is not None
        left, top, right, bottom = bounds
        assert right - left > 150
        assert bottom - top >= 40
        assert abs((left + right) / 2 - cover.width / 2) <= 2
        assert abs((top + bottom) / 2 - cover.height / 2) <= 2
        result.seek(1)
        assert result.convert("RGB").tobytes() == Image.new("RGB", result.size, "red").tobytes()


@pytest.mark.parametrize("gif_loop,plays", [(None, 1), (0, 0), (2, 3)])
def test_gif_repeat_count_is_converted_to_apng_play_count(tmp_path, gif_loop, plays):
    path = tmp_path / "animated.gif"
    first = Image.new("RGB", (60, 100), "red")
    options = {} if gif_loop is None else {"loop": gif_loop}
    first.save(path, save_all=True, append_images=[Image.new("RGB", first.size, "blue")],
               duration=[70, 230], **options)
    with Image.open(BytesIO(build_hidden_preview(str(path)))) as result:
        assert result.info["loop"] == plays


@pytest.mark.parametrize("limits", [{"max_dimension": 100}, {"max_pixels": 100}])
def test_geometry_limits_are_checked_before_full_decode(tmp_path, limits):
    path = tmp_path / "generated.png"
    Image.new("RGB", (180, 300)).save(path)
    with pytest.raises(ValueError, match="尺寸超出限制"):
        build_hidden_preview(str(path), **limits)


def test_animation_frame_limit_is_enforced(tmp_path):
    path = tmp_path / "animated.gif"
    first = Image.new("RGB", (10, 10), "red")
    first.save(path, save_all=True, append_images=[Image.new("RGB", (10, 10), "blue")])
    with pytest.raises(ValueError, match="帧数超出限制"):
        build_hidden_preview(str(path), max_frames=1)


@pytest.mark.parametrize("enabled", [False, True])
def test_preview_toggle_config_persists_and_reloads(tmp_path, enabled):
    plugin = ConfigurationMixin()
    plugin.config_path = str(tmp_path / "config.json")
    plugin.key_config = {"web": {"enable": False, "host": "127.0.0.1", "port": 14514, "token": "test"}}
    plugin._config_lock = threading.RLock()
    assert AICatConfig.from_dict({}).image_enable_preview_hidden is False
    plugin._apply_raw_config({"image": {"enable_preview_hidden": not enabled}})
    plugin.update_config_from_web({"image": {"enable_preview_hidden": enabled}})
    assert plugin.config.image_enable_preview_hidden is enabled
    assert plugin.get_config_for_web()["image"]["enable_preview_hidden"] is enabled
    persisted = json.loads((tmp_path / "config.json").read_text())
    assert AICatConfig.from_dict(persisted).image_enable_preview_hidden is enabled


class _Event:
    def __init__(self):
        self.sent = []
        self.group_id = "100"

    def chain_result(self, components):
        return components

    async def send(self, message):
        self.sent.append(message)
        return {"message_id": "preview-message"}


def _delivery_plugin(enabled):
    plugin = ReferenceMediaMixin()
    plugin.config = SimpleNamespace(image_enable_preview_hidden=enabled, group_chat_configs={})
    plugin._create_image_component = lambda path: ("original", path)
    plugin._send_failures = {}
    plugin._send_failures_lock = threading.RLock()
    plugin._session_key = lambda event: "group:test"
    plugin._cache_relative_path = lambda path: path
    return plugin


@pytest.mark.parametrize(
    ("global_enabled", "mode", "expected"),
    [(False, "default", False), (True, "default", True), (False, "on", True), (True, "off", False)],
)
def test_group_preview_mode_overrides_or_inherits_global_setting(global_enabled, mode, expected):
    plugin = _delivery_plugin(global_enabled)
    plugin.config.group_chat_configs = {"100": {"preview_hidden_mode": mode}}
    assert plugin._preview_hidden_enabled_for_event(_Event()) is expected


def test_disabled_toggle_does_not_encode_images():
    plugin = _delivery_plugin(False)
    event = _Event()
    with patch("astrbot_plugin_selfie_image.features.reference_media.build_hidden_preview", side_effect=AssertionError):
        result = asyncio.run(plugin._send_generated_images(event, ["original.png"]))
    assert int(result) == 1
    assert event.sent == [[("original", "original.png")]]


def test_group_preview_off_bypasses_global_hidden_preview(tmp_path):
    path = tmp_path / "generated.png"
    Image.new("RGB", (60, 100), "red").save(path)
    plugin = _delivery_plugin(True)
    plugin.config.group_chat_configs = {"100": {"preview_hidden_mode": "off"}}
    event = _Event()
    with patch("astrbot_plugin_selfie_image.features.reference_media.build_hidden_preview", side_effect=AssertionError):
        result = asyncio.run(plugin._send_generated_images(event, [str(path)]))
    assert int(result) == 1
    assert event.sent == [[("original", str(path))]]


@pytest.mark.parametrize("remember_failures", [True, False])
def test_hidden_preview_bytes_reach_delivery_and_redelivery(tmp_path, remember_failures):
    path = tmp_path / "generated.png"
    Image.new("RGB", (60, 100), "red").save(path)
    plugin = _delivery_plugin(True)
    font_requests = []

    def font_loader(size, *, bold=False):
        font_requests.append((size, bold))
        return ImageFont.load_default(size=size)

    plugin._status_report_font = font_loader
    event = _Event()
    record = {"id": "record-1", "generated_image_paths": [str(path)]}
    deliveries = []

    async def wait(_path):
        pass

    plugin._wait_for_image_record = wait
    plugin._prompt_record_for_file = lambda _path: dict(record)
    plugin._redelivery_record_for_file = lambda _event, _path: dict(record)
    plugin._remember_prompt_deliveries = lambda _event, rows: deliveries.extend(rows)
    with patch("astrbot_plugin_selfie_image.features.reference_media.Image", SimpleNamespace(fromBytes=lambda data: data)):
        result = asyncio.run(plugin._send_generated_images(event, [str(path)], remember_failures=remember_failures))
    assert int(result) == 1
    assert font_requests == [(10, True)]
    encoded = event.sent[0][0]
    with Image.open(BytesIO(encoded)) as opened:
        assert opened.info["default_image"] is True
        opened.seek(1)
        assert opened.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
    assert hashlib.md5(encoded).hexdigest() in deliveries[0][0]["image_alias_md5s"]


def test_legacy_component_constructor_preserves_encoded_bytes(tmp_path):
    path = tmp_path / "generated.jpg"
    Image.new("RGB", (10, 10), "red").save(path)
    plugin = _delivery_plugin(True)
    class LegacyImage:
        def __init__(self, **kwargs):
            self.file = kwargs["file"]
    with patch("astrbot_plugin_selfie_image.features.reference_media.Image", LegacyImage):
        component = asyncio.run(plugin._generated_image_component(str(path), None))
    assert component.file.startswith("base64://")
    with Image.open(BytesIO(base64.b64decode(component.file[9:]))) as opened:
        assert opened.info["default_image"] is True


def test_encoding_failure_is_retryable_without_sending_original(tmp_path):
    plugin = _delivery_plugin(True)
    event = _Event()
    result = asyncio.run(plugin._send_generated_images(event, [str(tmp_path / "missing.png")]))
    assert int(result) == 0
    assert result.failed_count == 1
    assert not event.sent
    assert plugin._send_failures == {"group:test": [str(tmp_path / "missing.png")]}
