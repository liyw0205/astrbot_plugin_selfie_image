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


def test_nai_multi_megabyte_markdown_response_finishes_in_bounded_process():
    # A subprocess timeout still works if a regression holds the GIL inside re.
    script = textwrap.dedent("""
        import asyncio
        import base64
        import json
        import runpy
        runpy.run_path('tests/conftest.py')
        from astrbot_plugin_selfie_image.core.models import ImageModelTarget
        from astrbot_plugin_selfie_image.core.providers import ImageGenerateRequest, NovelAIImageAdapter

        image = b'\\x89PNG\\r\\n\\x1a\\n' + bytes(range(256)) * 8192
        inline = 'data:image/png;base64,' + base64.b64encode(image).decode('ascii')
        content = '![image](' + inline + ')'
        response = {'choices': [{'message': {'content': content}}]}

        class Adapter(NovelAIImageAdapter):
            async def post_json_data_or_error(self, *_args, **_kwargs):
                return response, ''

        async def main():
            target = ImageModelTarget('relay', 'novelai', 'https://relay.test/v1', 'test', 'nai-diffusion-4-5-full', 5)
            result = await Adapter(target, None).generate(ImageGenerateRequest(prompt='a blue flower'))
            assert not result.error, result.error
            assert result.images == [image]
            assert result.source_media == [{'type': 'base64', 'value': inline}]
            print(json.dumps({'image_count': len(result.images), 'image_bytes': len(result.images[0])}))

        asyncio.run(main())
    """)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(completed.stdout) == {"image_count": 1, "image_bytes": 2_097_160}
