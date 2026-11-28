"""Exercise animated references through transport, quote collection and group commands."""

import asyncio
import base64
from contextlib import asynccontextmanager
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image as PILImage
import pytest

from astrbot_plugin_selfie_image.core.models import AICatConfig
from astrbot_plugin_selfie_image.core.providers import (
    AgnesImageAdapter,
    ImageGenerateRequest,
    ImageModelTarget,
    ImageReference,
)
from astrbot_plugin_selfie_image.core.utils import fetch_image_source
from astrbot_plugin_selfie_image.features.persona import PersonaManager
from astrbot_plugin_selfie_image.features.reference_collector import (
    ReferenceCollector,
    download_sources_as_references,
)
from astrbot_plugin_selfie_image.main import SelfieImagePlugin
from astrbot_plugin_selfie_image.generation.preview_hidden import build_hidden_preview
from astrbot_plugin_selfie_image.prompts.preset import ImagePresetManager


def gif_bytes(frame_count=25):
    frames = [PILImage.new("RGBA", (12, 9), (index * 9, 80, 20, 255)) for index in range(frame_count)]
    frames[0].putpixel((0, 0), (0, 0, 0, 0))
    output = BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=40)
    return output.getvalue()


def apng_bytes(frame_count=25, default_image=False):
    frames = [PILImage.new("RGBA", (12, 9), (index * 9, 80, 20, 255)) for index in range(frame_count)]
    frames[1].putpixel((0, 0), (0, 0, 0, 0))
    output = BytesIO()
    frames[0].save(
        output, format="PNG", save_all=True, append_images=frames[1:],
        duration=40, default_image=default_image,
    )
    return output.getvalue()


def assert_second_frame(data, original):
    with PILImage.open(BytesIO(original)) as source, PILImage.open(BytesIO(data)) as result:
        assert source.n_frames > 8
        source.seek(1)
        assert result.format == "PNG"
        assert result.n_frames == 1
        assert result.size == source.size
        assert result.convert("RGBA").tobytes() == source.convert("RGBA").tobytes()


def assert_first_frame(data, original):
    with PILImage.open(BytesIO(original)) as source, PILImage.open(BytesIO(data)) as result:
        assert source.n_frames == 25
        assert result.format == "PNG"
        assert result.n_frames == 1
        assert result.size == source.size
        assert result.convert("RGBA").tobytes() == source.convert("RGBA").tobytes()


@pytest.mark.parametrize("format", ["gif", "apng", "apng-with-cover"])
@pytest.mark.parametrize("transport", ["file", "uri", "data", "base64", "http"])
def test_animation_reference_uses_selected_frame_across_sources(tmp_path, transport, format):
    original = gif_bytes() if format == "gif" else apng_bytes(default_image=format == "apng-with-cover")
    path = tmp_path / "quoted animation.bin"
    path.write_bytes(original)
    encoded = base64.b64encode(original).decode("ascii")
    sources = {
        "file": str(path), "uri": path.as_uri(),
        "data": "data:image/jpeg;base64," + encoded,
        "base64": "base64://" + encoded,
        "http": "https://cdn.example/animation",
    }

    async def chunks(_size):
        yield original

    @asynccontextmanager
    async def response():
        yield SimpleNamespace(
            status=200, headers={"content-type": "application/octet-stream"},
            content=SimpleNamespace(iter_chunked=chunks),
        )

    session = SimpleNamespace(get=lambda *_args, **_kwargs: response())
    reasons = []
    refs, failed = asyncio.run(download_sources_as_references(
        [sources[transport]], session, max_bytes=1024 * 1024, max_frames=1,
        failure_reasons=reasons,
    ))

    assert failed == 0
    assert len(refs) == 1
    assert refs[0].mime_type == "image/png"
    assert refs[0].source_url == ""
    (assert_first_frame if format == "gif" else assert_second_frame)(refs[0].data, original)
    assert reasons == []
    adapter = AgnesImageAdapter(
        ImageModelTarget("test", "agnes", "https://example.test", "test", "agnes-image-2.1-flash", 30),
        None,
    )
    payload = adapter.build_payload(ImageGenerateRequest(prompt="group photo", images=refs))
    assert payload["extra_body"]["image"][0].startswith("data:image/png;base64,")


@pytest.mark.parametrize("format", ["gif", "apng"])
@pytest.mark.parametrize("limits", [
    {"max_bytes": 16}, {"max_dimension": 8}, {"max_pixels": 50},
])
def test_selected_frame_keeps_reference_byte_and_geometry_limits(tmp_path, limits, format):
    path = tmp_path / "animation.gif"
    path.write_bytes(gif_bytes() if format == "gif" else apng_bytes())
    reasons = []
    options = {"max_bytes": 1024 * 1024, **limits}

    assert asyncio.run(fetch_image_source(
        str(path), None, **options, allow_animation_reference_frame=True, failure_reasons=reasons,
    )) is None
    assert reasons == ["too_large"]


def test_unreadable_gif_is_not_forwarded_as_a_reference(tmp_path):
    path = tmp_path / "broken.gif"
    path.write_bytes(b"GIF89a")
    reasons = []
    refs, failed = asyncio.run(download_sources_as_references(
        [str(path)], None, max_bytes=1024, failure_reasons=reasons,
    ))
    assert not refs
    assert failed == 1
    assert reasons == ["invalid_or_unreadable"]


def test_hidden_preview_reference_uses_revealed_image_not_blurred_cover(tmp_path):
    path = tmp_path / "original.png"
    original = PILImage.new("RGBA", (64, 96), (10, 80, 200, 180))
    original.paste("red", (8, 12, 35, 55))
    original.save(path)
    wrapped = build_hidden_preview(str(path))
    path = tmp_path / "quoted.png"
    path.write_bytes(wrapped)

    refs, failed = asyncio.run(download_sources_as_references(
        [path.as_uri()], None, max_bytes=1024 * 1024, max_frames=1,
    ))

    assert failed == 0
    assert len(refs) == 1
    assert refs[0].source_url == ""
    with PILImage.open(BytesIO(wrapped)) as cover, PILImage.open(BytesIO(refs[0].data)) as result:
        assert cover.info["default_image"] is True
        assert cover.convert("RGBA").tobytes() != original.tobytes()
        assert result.n_frames == 1
        assert result.convert("RGBA").tobytes() == original.tobytes()


@pytest.mark.parametrize("lookup", [False, True])
@pytest.mark.parametrize("format", ["gif", "apng"])
def test_lookup_can_preserve_original_animation_bytes(tmp_path, lookup, format):
    original = gif_bytes(frame_count=2) if format == "gif" else apng_bytes(frame_count=2)
    path = tmp_path / "animation.gif"
    path.write_bytes(original)
    event = SimpleNamespace(message_obj=SimpleNamespace(message=[Image(str(path))]))
    collector = ReferenceCollector(max_bytes=1024 * 1024, include_image_alternates=lookup)

    collected = asyncio.run(collector.collect(event, None))

    assert collected.failed_count == 0
    assert len(collected.message) == 1
    ref = collected.message[0]
    assert ref.mime_type == ("image/gif" if lookup and format == "gif" else "image/png")
    assert (ref.data == original) is lookup


class Image:
    def __init__(self, path):
        self.file = path
        self.path = ""


class Reply:
    def __init__(self, image):
        self.chain = [image]


class Plain:
    text = "/合影"


class At:
    qq = "bot-qq"


@pytest.mark.parametrize("reference", ["quoted-gif", "attached-gif", "quoted-apng", "attached-apng", "quoted-png", "broken", "none"])
def test_group_command_keeps_quote_objects_and_reports_read_failures(tmp_path, reference):
    original = apng_bytes() if reference.endswith("apng") else gif_bytes()
    path = tmp_path / "object.gif"
    path.write_bytes(original if reference != "broken" else b"GIF89a")
    if reference == "quoted-png":
        path = tmp_path / "object.png"
        PILImage.new("RGB", (12, 9), "blue").save(path)
    image = Image(path.as_uri())
    components = [At(), Plain()]
    if reference != "none":
        components.append(image if reference.startswith("attached-") else Reply(image))
    event = SimpleNamespace(
        message_str="/合影", message_obj=SimpleNamespace(message=components, quote=None),
        plain_result=lambda text: text,
    )
    plugin = object.__new__(SelfieImagePlugin)
    plugin.context = SimpleNamespace()
    plugin.config = AICatConfig.from_dict({})
    plugin.persona = PersonaManager(tmp_path)
    plugin.persona.has_reference_image = lambda: True
    plugin.presets = ImagePresetManager(tmp_path)
    plugin._persona_identity_reference = lambda **_kwargs: ImageReference(b"identity", "image/png")
    plugin._quota_error_message = lambda *_args: ""
    plugin._rate_limit_error_message = lambda *_args: ""
    plugin._recent_context_image_sources_async = AsyncMock(return_value=[])
    plugin._build_contextual_progress_text = AsyncMock(return_value="progress")
    plugin._image_batch_queue_expected = lambda *_args: False
    plugin._background_selfie_batches = AsyncMock(return_value={})

    @asynccontextmanager
    async def reference_session():
        yield None

    plugin._reference_client_session = reference_session
    captured = {}
    plugin.start_command_image_task = lambda *_args, **kwargs: captured.update(kwargs)

    async def invoke():
        replies = [reply async for reply in plugin.cmd_group_selfie(event)]
        if captured:
            await captured["runner"]("group-gif-test")
        return replies

    replies = asyncio.run(invoke())
    if reference == "broken":
        assert replies == ["参考图片不够清晰或无法识别，请发送清晰的原图后重试。"]
        assert not captured
        plugin._background_selfie_batches.assert_not_awaited()
    elif reference == "none":
        assert "没有读取到合影对象参考图" in replies[0]
        assert captured["summary"]["reference_image_count"] == 0
    else:
        assert replies == ["progress"]
        assert captured["summary"]["reference_image_count"] == 1
        selection = plugin._background_selfie_batches.call_args.kwargs["cos_selection"]
        assert selection.identity_source == "persona"
        assert selection.extra_reference_image_count == 1
        identity, peer = captured["reference_images"]
        assert identity.data == b"identity"
        assert peer.source_url == (path.as_uri() if reference == "quoted-png" else "")
        assert peer.mime_type == "image/png"
        if reference.endswith("gif"):
            assert_first_frame(peer.data, original)
        elif reference.endswith("apng"):
            assert_second_frame(peer.data, original)
        else:
            assert peer.data == path.read_bytes()
