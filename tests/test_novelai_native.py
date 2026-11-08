"""Verify native NovelAI URL handling, model discovery and image responses."""

import asyncio
import base64
import json
from io import BytesIO
from zipfile import ZipFile

import brotli
import pytest
from PIL import Image

from astrbot_plugin_selfie_image.core.models import AICatConfig
from astrbot_plugin_selfie_image.core.provider_parser import build_model_list_urls
from astrbot_plugin_selfie_image.core.providers import ImageGenerateRequest, NovelAIImageAdapter
from astrbot_plugin_selfie_image.main import SelfieImagePlugin


_png_buffer = BytesIO()
Image.new("RGB", (32, 32), (60, 120, 255)).save(_png_buffer, format="PNG")
PNG = _png_buffer.getvalue()


def channel_config(base_url, provider="novelai"):
    return AICatConfig.from_dict({"image_channels": [{
        "name": "nai", "provider_type": provider, "base_url": base_url,
        "api_key": "account-key-and-internal-key", "enabled_models": ["nai-diffusion-4-5-full"],
    }]})


class Response:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status
        self.headers = {"Content-Type": "application/zip" if isinstance(data, bytes) else "application/json"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def read(self):
        return self.data if isinstance(self.data, bytes) else json.dumps(self.data).encode()

    async def text(self):
        return json.dumps(self.data)

    async def json(self, **_kwargs):
        return self.data


class Session:
    def __init__(self, data, status=200):
        self.response = Response(data, status)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, url, **kwargs):
        self.requests.append({"method": "GET", "url": url, **kwargs})
        return self.response

    def post(self, url, **kwargs):
        self.requests.append({"method": "POST", "url": url, **kwargs})
        return self.response


@pytest.mark.parametrize("base,endpoint", [
    ("https://api.novelai.net", "https://api.novelai.net/ai/generate-image"),
    ("https://image.novelai.net/", "https://image.novelai.net/ai/generate-image"),
    ("https://api.novelai.net/ai/", "https://api.novelai.net/ai/generate-image"),
    ("https://native.test/proxy/ai", "https://native.test/proxy/ai/generate-image"),
    ("https://native.test", "https://native.test/ai/generate-image"),
    ("http://native.test/image/chami", "http://native.test/image/chami"),
    ("https://native.test/ai/generate-image/", "https://native.test/ai/generate-image"),
    ("https://nai2api.test/ai/generate-image", "https://nai2api.test/ai/generate-image"),
    ("https://native.test/proxy/v1/custom-image/", "https://native.test/proxy/v1/custom-image"),
])
@pytest.mark.parametrize("response_format", ["png", "zip"])
def test_native_channel_posts_official_body_to_exact_endpoint(base, endpoint, response_format):
    config = channel_config(base)
    target = config.get_enabled_image_targets()[0]
    body = PNG
    if response_format == "zip":
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr("image.png", PNG)
        body = buffer.getvalue()
    session = Session(body)
    adapter = NovelAIImageAdapter(target, session)
    assert adapter._mode() == "official"
    result = asyncio.run(adapter.generate(ImageGenerateRequest(prompt="a blue flower")))

    assert not result.error
    assert result.images == [PNG]
    assert len(session.requests) == 1
    request = session.requests[0]
    assert request["method"] == "POST"
    assert request["url"] == endpoint
    assert request["headers"]["Authorization"] == "Bearer account-key-and-internal-key"
    assert request["json"]["input"] == "a blue flower"
    assert request["json"]["action"] == "generate"
    assert request["json"]["model"] == "nai-diffusion-4-5-full"
    assert request["json"]["parameters"]["v4_prompt"]["caption"]["base_caption"] == "a blue flower"
    assert "messages" not in request["json"]


@pytest.mark.parametrize("provider", ["novelai", "NAI", "ppnai"])
def test_native_models_never_add_v1_or_repeat_models_suffix(provider):
    assert build_model_list_urls("http://native.test/image/chami/", provider) == ["http://native.test/image/chami/models"]
    assert build_model_list_urls("https://native.test/models", provider) == ["https://native.test/models"]
    assert build_model_list_urls("https://native.test/proxy/v1/native", provider) == ["https://native.test/proxy/v1/native/models"]
    assert build_model_list_urls("", provider) == []


def test_nai_targets_from_openai_channels_still_use_relay_protocol():
    config = channel_config("https://relay.test/v1", provider="openai")
    target = config.get_enabled_image_targets()[0]
    adapter = NovelAIImageAdapter(target, None)
    assert config.image_channels[0].extra == {}
    assert target.provider_type == "novelai"
    assert adapter._mode() == "relay"
    assert adapter._endpoint(adapter._mode()) == "https://relay.test/v1/chat/completions"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 502])
def test_native_generation_error_never_retries_as_openai_chat(status):
    target = channel_config("http://native.test/image/chami").get_enabled_image_targets()[0]
    session = Session({"error": {"message": "native request failed"}}, status)
    result = asyncio.run(NovelAIImageAdapter(target, session).generate(ImageGenerateRequest(prompt="a blue flower")))
    assert f"HTTP {status}" in result.error
    assert "native request failed" in result.error
    assert len(session.requests) == 1
    assert session.requests[0]["url"] == "http://native.test/image/chami"


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("already_decoded", [False, True])
def test_native_json_images_with_missing_or_already_decoded_brotli_headers(compressed, already_decoded):
    encoded = base64.b64encode(PNG).decode("ascii")
    payload = {"images": [{"image": encoded, "index": 0, "seed": 123}]}
    body = json.dumps(payload).encode()
    if compressed and not already_decoded:
        body = brotli.compress(body)
    target = channel_config("http://native.test/image/chami").get_enabled_image_targets()[0]
    session = Session(body)
    session.response.headers = {"Content-Type": "application/json"}
    if already_decoded:
        session.response.headers["Content-Encoding"] = "br"

    result = asyncio.run(NovelAIImageAdapter(target, session).generate(ImageGenerateRequest(prompt="a blue flower")))

    assert not result.error
    assert result.images == [PNG]
    assert result.source_media == [{"type": "base64", "value": encoded}]
    assert len(session.requests) == 1


@pytest.mark.parametrize("response_format", ["png", "zip"])
def test_native_brotli_binary_response(response_format):
    body = PNG
    if response_format == "zip":
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr("image.png", PNG)
        body = buffer.getvalue()
    target = channel_config("http://native.test/image/chami").get_enabled_image_targets()[0]
    session = Session(brotli.compress(body))
    result = asyncio.run(NovelAIImageAdapter(target, session).generate(ImageGenerateRequest(prompt="a blue flower")))

    assert not result.error
    assert result.images == [PNG]
    assert len(session.requests) == 1


@pytest.mark.parametrize("status", [200, 400, 401, 429, 502])
def test_native_brotli_json_error_keeps_upstream_reason(status):
    payload = {"error": {"message": "internal NAI key unavailable"}}
    target = channel_config("http://native.test/image/chami").get_enabled_image_targets()[0]
    session = Session(brotli.compress(json.dumps(payload).encode()), status)
    session.response.headers = {"Content-Type": "application/json"}
    result = asyncio.run(NovelAIImageAdapter(target, session).generate(ImageGenerateRequest(prompt="a blue flower")))

    assert result.images == []
    assert "internal NAI key unavailable" in result.error
    if status >= 400:
        assert f"HTTP {status}" in result.error
    assert len(session.requests) == 1


@pytest.mark.parametrize("compressed", [False, True])
def test_native_response_size_limit_applies_before_and_after_decompression(compressed):
    body = json.dumps({"images": [{"image": "a" * 5000}]}).encode()
    target = channel_config("http://native.test/image/chami").get_enabled_image_targets()[0]
    target.extra["max_response_bytes"] = 1024
    session = Session(brotli.compress(body) if compressed else body)
    result = asyncio.run(NovelAIImageAdapter(target, session).generate(ImageGenerateRequest(prompt="a blue flower")))

    assert result.images == []
    assert "超过 1024 bytes" in result.error
    assert len(session.requests) == 1


def test_native_binary_image_limit():
    target = channel_config("https://image.novelai.net").get_enabled_image_targets()[0]
    result = asyncio.run(NovelAIImageAdapter(target, Session(PNG)).generate(
        ImageGenerateRequest(prompt="a blue flower", max_image_bytes=4)
    ))
    assert result.images == []
    assert "图片超过 4 bytes" in result.error


@pytest.mark.parametrize("body", [b"<html>Bad gateway</html>", b"", b"not an image", b"\xff\x00\x01"])
def test_native_invalid_body_is_a_single_parse_failure(body):
    target = channel_config("http://native.test/image/chami").get_enabled_image_targets()[0]
    session = Session(body)
    result = asyncio.run(NovelAIImageAdapter(target, session).generate(ImageGenerateRequest(prompt="a blue flower")))

    assert result.images == []
    assert "未返回可解析图片" in result.error
    assert len(session.requests) == 1


def test_native_model_refresh_sends_bearer_key_and_keeps_upstream_order(monkeypatch):
    session = Session({"data": [{"id": "nai-diffusion-5-full"}, {"id": "nai-diffusion-4-5-full"}]})
    monkeypatch.setattr("astrbot_plugin_selfie_image.main.aiohttp.ClientSession", lambda **_kwargs: session)
    plugin = object.__new__(SelfieImagePlugin)
    plugin.config = channel_config("https://native.test/api")
    models = asyncio.run(plugin.web_refresh_image_models({"channel": {
        "name": "nai", "provider_type": "novelai", "base_url": "https://native.test/api", "api_key": "******",
    }}))
    assert models == ["nai-diffusion-5-full", "nai-diffusion-4-5-full"]
    assert len(session.requests) == 1
    assert session.requests[0]["url"] == "https://native.test/api/models"
    assert session.requests[0]["headers"]["Authorization"] == "Bearer account-key-and-internal-key"


@pytest.mark.parametrize("status", [401, 403, 404, 405, 429, 500, 501])
def test_native_model_refresh_distinguishes_missing_route_from_upstream_errors(monkeypatch, status):
    session = Session({"error": "upstream error"}, status)
    monkeypatch.setattr("astrbot_plugin_selfie_image.main.aiohttp.ClientSession", lambda **_kwargs: session)
    plugin = object.__new__(SelfieImagePlugin)
    plugin.config = channel_config("http://native.test/image/chami")
    with pytest.raises(RuntimeError) as error:
        asyncio.run(plugin.web_refresh_image_models({"channel": {
            "name": "nai", "provider_type": "novelai", "base_url": "http://native.test/image/chami", "api_key": "******",
        }}))
    assert f"HTTP {status}" in str(error.value)
    assert ("请手动添加" in str(error.value)) == (status in {404, 405, 501})
    assert len(session.requests) == 1
    assert session.requests[0]["method"] == "GET"
    assert session.requests[0]["url"] == "http://native.test/image/chami/models"
