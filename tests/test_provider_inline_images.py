"""Keep large inline image responses from blocking the bot's event loop."""

import asyncio
import base64
import json
from pathlib import Path
import subprocess
import sys
import textwrap
import time

import pytest

from astrbot_plugin_selfie_image.core.provider_parser import extract_image_urls_from_text


@pytest.mark.parametrize("wrapper", [
    "![image]({inline})",
    '<img src="{inline}">',
    '<img srcset="{inline} 1x, https://cdn.test/preview.png 2x">',
    "generated: {inline}",
])
@pytest.mark.parametrize("prefix", [
    "data:image/png;base64,",
    "data:image/png;name=result.png;charset=utf-8;base64,",
    "BASE64://",
])
def test_inline_images_do_not_hide_other_image_links(wrapper, prefix):
    encoded = base64.b64encode(bytes(range(256))).decode("ascii")
    inline = prefix + encoded
    content = wrapper.format(inline=inline)
    content += " [download](https://cdn.test/result.png) ![preview](outputs/preview.webp)"

    result = extract_image_urls_from_text(content)

    assert result["b64"] == [inline]
    assert "https://cdn.test/result.png" in result["urls"]
    assert "outputs/preview.webp" in result["others"]
    assert not any(encoded in value for value in result["urls"] + result["others"])


@pytest.mark.parametrize("transport", ["relay", "native", "native_brotli"])
def test_nai_multi_megabyte_markdown_response_finishes_in_bounded_process(transport):
    # A subprocess timeout still works if a regression holds the GIL inside re.
    script = textwrap.dedent("""
        import asyncio
        import base64
        import brotli
        import json
        import runpy
        import sys
        runpy.run_path('tests/conftest.py')
        from astrbot_plugin_selfie_image.core.models import AICatConfig
        from astrbot_plugin_selfie_image.core.providers import ImageGenerateRequest, NovelAIImageAdapter

        image = b'\\x89PNG\\r\\n\\x1a\\n' + bytes(range(256)) * 8192
        inline = 'data:image/png;base64,' + base64.b64encode(image).decode('ascii')
        content = '![image](' + inline + ')'
        response = {'choices': [{'message': {'content': content}}]}
        transport = sys.argv[1]

        class Response:
            status = 200
            headers = {'Content-Type': 'application/json'}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def read(self):
                body = json.dumps({'images': [{'image': inline}]}).encode()
                return brotli.compress(body) if transport == 'native_brotli' else body

        class Session:
            def post(self, *_args, **_kwargs):
                return Response()

        class Adapter(NovelAIImageAdapter):
            async def post_json_data_or_error(self, *_args, **_kwargs):
                return response, ''

        async def main():
            config = AICatConfig.from_dict({'image_channels': [{
                'name': 'nai', 'provider_type': 'openai' if transport == 'relay' else 'novelai',
                'base_url': 'https://relay.test/v1' if transport == 'relay' else 'https://native.test/image/chami',
                'api_key': 'test', 'enabled_models': ['nai-diffusion-4-5-full'],
            }]})
            target = config.get_enabled_image_targets()[0]
            result = await Adapter(target, Session()).generate(ImageGenerateRequest(prompt='a blue flower'))
            assert not result.error, result.error
            assert result.images == [image]
            assert result.source_media == [{'type': 'base64', 'value': inline}]
            print(json.dumps({'image_count': len(result.images), 'image_bytes': len(result.images[0])}))

        asyncio.run(main())
    """)
    completed = subprocess.run(
        [sys.executable, "-c", script, transport],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(completed.stdout) == {"image_count": 1, "image_bytes": 2_097_160}


def test_response_preview_bounds_large_structured_payload_before_redaction():
    script = textwrap.dedent("""
        import json
        import runpy
        runpy.run_path('tests/conftest.py')
        from astrbot_plugin_selfie_image.core.provider_parser import response_preview

        preview = response_preview({
            'api_key': 'sk-sensitive-preview-secret',
            'error': {'message': 'upstream rejected request'},
            'payload': 'x' * 100_000,
        }, 1000)
        assert len(preview) <= 1000
        assert 'sk-sensitive-preview-secret' not in preview
        assert 'upstream rejected request' in preview
        print(json.dumps({'length': len(preview)}))
    """)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert json.loads(completed.stdout) == {"length": 1000}


def test_base64_collection_and_decode_do_not_block_event_loop(monkeypatch):
    from astrbot_plugin_selfie_image.core import provider_parser

    image = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    encoded = base64.b64encode(image).decode("ascii")
    payload = {"data": [{"b64_json": encoded}]}
    original_collect = provider_parser.collect_images_from_unknown
    original_decode = provider_parser.b64_to_bytes

    def slow_collect(value):
        time.sleep(0.04)
        return original_collect(value)

    def slow_decode(value):
        time.sleep(0.04)
        return original_decode(value)

    monkeypatch.setattr(provider_parser, "collect_images_from_unknown", slow_collect)
    monkeypatch.setattr(provider_parser, "b64_to_bytes", slow_decode)

    async def run():
        task = asyncio.create_task(
            provider_parser.images_from_response_unknown(object(), payload, timeout=1)
        )
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while not task.done():
                ticks += 1
                await asyncio.sleep(0.001)

        pulse = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        images = await asyncio.wait_for(task, timeout=2)
        await pulse

        assert images == [image]
        assert ticks >= 10

    asyncio.run(run())


def test_openai_b64_fast_path_runs_off_event_loop(monkeypatch):
    from astrbot_plugin_selfie_image.core import providers
    from astrbot_plugin_selfie_image.core.models import ImageModelTarget
    from astrbot_plugin_selfie_image.core.providers import ImageGenerateRequest, OpenAIImageAdapter

    image = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    encoded = base64.b64encode(image).decode("ascii")
    response = {"data": [{"b64_json": encoded}]}
    original_extract = providers.extract_openai_images_data

    def slow_extract(data, max_bytes):
        time.sleep(0.08)
        return original_extract(data, max_bytes)

    monkeypatch.setattr(providers, "extract_openai_images_data", slow_extract)

    class Adapter(OpenAIImageAdapter):
        async def post_json_data_or_error(self, *_args, **_kwargs):
            return response, ""

    target = ImageModelTarget(
        channel_name="test", provider_type="openai", base_url="https://relay.test",
        api_key="test", model="dall-e-3", timeout=30,
    )
    adapter = Adapter(target, object())
    async def run():
        task = asyncio.create_task(adapter.generate(ImageGenerateRequest(prompt="a blue flower")))
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while not task.done():
                ticks += 1
                await asyncio.sleep(0)

        pulse = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        result = await asyncio.wait_for(task, timeout=2)
        await pulse

        assert result.images == [image]
        assert result.source_media == [{"type": "base64", "value": encoded}]
        assert ticks >= 10

    asyncio.run(run())


def test_novelai_large_error_preview_runs_off_event_loop(monkeypatch):
    from astrbot_plugin_selfie_image.core import providers
    from astrbot_plugin_selfie_image.core.models import AICatConfig
    from astrbot_plugin_selfie_image.core.providers import ImageGenerateRequest, NovelAIImageAdapter

    body = json.dumps({"error": {"message": "upstream rejected request"}, "payload": "x" * 100_000}).encode()
    original_preview = providers.response_preview

    def slow_preview(value, limit=1000):
        time.sleep(0.08)
        return original_preview(value, limit)

    monkeypatch.setattr(providers, "response_preview", slow_preview)

    class Response:
        status = 400
        headers = {"Content-Type": "application/json"}
        charset = "utf-8"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def read(self):
            return body

    class Session:
        def post(self, *_args, **_kwargs):
            return Response()

    config = AICatConfig.from_dict({"image_channels": [{
        "name": "nai", "provider_type": "novelai", "base_url": "https://api.novelai.net",
        "api_key": "test", "enabled_models": ["nai-diffusion-4-5-full"],
    }]})
    adapter = NovelAIImageAdapter(config.get_enabled_image_targets()[0], Session())

    async def run():
        task = asyncio.create_task(adapter.generate(ImageGenerateRequest(prompt="a blue flower")))
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while not task.done():
                ticks += 1
                await asyncio.sleep(0.001)

        pulse = asyncio.create_task(heartbeat())
        result = await asyncio.wait_for(task, timeout=2)
        await pulse

        assert result.error.startswith("HTTP 400:")
        assert ticks >= 10

    asyncio.run(run())
