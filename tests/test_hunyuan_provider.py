from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_selfie_image.core.models import (
    AICatConfig,
    ImageModelTarget,
    infer_provider_type_from_model,
    normalize_provider_type,
    resolve_model_provider_type,
)
from astrbot_plugin_selfie_image.core.providers import (
    HunyuanImageAdapter,
    ImageGenerateRequest,
    ImageReference,
    create_adapter,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\nreference"


class FakeResponse:
    status = 200
    charset = "utf-8"
    headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def read(self):
        return b'{"data": []}'


class FakeSession:
    def __init__(self):
        self.requests = []

    async def post(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        return FakeResponse()


def make_target(model: str = "hy-image-3.5", base_url: str = "") -> ImageModelTarget:
    return ImageModelTarget(
        channel_name="hunyuan",
        provider_type="hunyuan",
        base_url=base_url,
        api_key="test-key",
        model=model,
        timeout=30,
    )


def test_hunyuan_model_prefixes_resolve_even_with_openai_protocol_lock():
    assert normalize_provider_type("hunyuan") == "hunyuan"
    assert normalize_provider_type("hy-image") == "hunyuan"
    assert normalize_provider_type("hunyuan-image") == "hunyuan"

    for model in ("hy-image-3.5", "hunyuan-image-3.5"):
        assert infer_provider_type_from_model(model) == "hunyuan"
        assert resolve_model_provider_type(model, "openai", protocol_lock=True) == "hunyuan"
        assert resolve_model_provider_type(model, "openai", "openai", protocol_lock=True) == "hunyuan"

    config = AICatConfig.from_dict(
        {
            "image_channels": [
                {
                    "name": "test-hunyuan",
                    "provider_type": "hunyuan",
                    "base_url": "https://your-company.copilot.qq.com",
                    "api_key": "test-key",
                    "enabled_models": ["hy-image-3.5"],
                }
            ]
        }
    )
    assert config.get_prioritized_targets()[0].provider_type == "hunyuan"


def test_hunyuan_text_to_image_uses_default_endpoint_and_openai_payload():
    session = FakeSession()
    adapter = create_adapter(make_target(), session)

    assert isinstance(adapter, HunyuanImageAdapter)
    asyncio.run(adapter.generate(ImageGenerateRequest(prompt="a red umbrella")))

    request = session.requests[0]
    assert request["url"] == "https://your-company.copilot.qq.com/v1/images/generations"
    assert request["json"] == {
        "model": "hy-image-3.5",
        "prompt": "a red umbrella",
        "response_format": "b64_json",
    }


def test_hunyuan_image_to_image_uses_json_image_url_array():
    session = FakeSession()
    adapter = HunyuanImageAdapter(make_target("hunyuan-image-3.5"), session)
    request = ImageGenerateRequest(
        prompt="preserve the subject",
        images=[ImageReference(data=PNG_BYTES, mime_type="image/png")],
    )

    asyncio.run(adapter.generate(request))

    posted = session.requests[0]
    assert posted["url"] == "https://your-company.copilot.qq.com/v1/images/edits"
    image_url = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
    assert posted["json"] == {
        "model": "hunyuan-image-3.5",
        "prompt": "preserve the subject",
        "images": [{"image_url": image_url}],
    }


def test_dashboard_exposes_hunyuan_provider_and_default_base_url():
    dashboard = Path(__file__).resolve().parents[1] / "pages" / "dashboard" / "index.html"
    html = dashboard.read_text(encoding="utf-8")

    assert "'hunyuan'" in html
    assert "hunyuan: 'https://your-company.copilot.qq.com'" in html
    assert "compact.startsWith('hy-image')" in html
    assert "compact.startsWith('hunyuan-image')" in html
