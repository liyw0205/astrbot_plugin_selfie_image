"""Keep large inline image responses from blocking the bot's event loop."""

import base64
import json
from pathlib import Path
import subprocess
import sys
import textwrap

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
