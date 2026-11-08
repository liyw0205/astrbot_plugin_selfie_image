"""NAI reference recovery, edit resolution and text-only request boundaries."""

import asyncio
import json
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from PIL import Image, PngImagePlugin

from astrbot_plugin_selfie_image.core.providers import ImageReference
from astrbot_plugin_selfie_image.nai.reference import (
    DATA_PATH, parse_reference_response, read_reference_metadata,
    resolve_reference_prompt, strip_reference_controls,
)


def png_bytes(**info):
    metadata = PngImagePlugin.PngInfo()
    for key, value in info.items():
        metadata.add_text(key, json.dumps(value) if isinstance(value, dict) else value)
    stream = BytesIO()
    Image.new("RGB", (2, 2)).save(stream, format="PNG", pnginfo=metadata)
    return stream.getvalue()


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
@pytest.mark.parametrize("encoding", ["ascii", "unicode"])
def test_exif_generation_comment(format, encoding):
    payload = json.dumps({"prompt": "1girl, blue dress", "seed": 123, "steps": 28})
    comment = b"ASCII\0\0\0" + payload.encode() if encoding == "ascii" else b"UNICODE\0" + payload.encode("utf-16")
    exif = Image.Exif()
    exif[37510] = comment
    stream = BytesIO()
    Image.new("RGB", (2, 2)).save(stream, format=format, exif=exif)
    result = read_reference_metadata(stream.getvalue())
    assert result["source"] == "exif_comment"
    assert result["prompt"] == "1girl, blue dress"


@pytest.mark.parametrize("info", [
    {"Description": "an ordinary photo caption"},
    {"Comment": {"prompt": "not verified generation data"}},
    {"Comment": "not valid json"},
    {"Comment": {"prompt": "", "seed": 1, "steps": 28}},
])
def test_arbitrary_metadata_is_not_mistaken_for_generation_prompt(info):
    assert read_reference_metadata(png_bytes(**info)) == {}
    assert read_reference_metadata(b"not an image") == {}


def test_novelai_png_comment_and_marked_description():
    result = read_reference_metadata(png_bytes(
        Software="NovelAI", Comment={"prompt": "1girl, red dress", "uc": "bad hands"},
        Description="outdated description",
    ))
    assert result["prompt"] == "1girl, red dress"
    assert result["source"] == "png_comment"
    assert "bad hands" not in str(result)
    assert read_reference_metadata(png_bytes(Software="NovelAI", Description="1girl"))["prompt"] == "1girl"


def test_v4_character_captions_are_available_to_the_resolver():
    result = read_reference_metadata(png_bytes(Comment={
        "seed": 1, "steps": 28,
        "v4_prompt": {"caption": {"base_caption": "2girls, garden", "char_captions": [
            {"char_caption": "red hair, red dress"}, {"char_caption": "blue hair, blue dress"},
        ]}},
        "v4_negative_prompt": {"caption": {"base_caption": "bad anatomy"}},
    }))
    assert result["prompt"] == "2girls, garden"
    assert result["characters"] == ["red hair, red dress", "blue hair, blue dress"]
    assert "bad anatomy" not in str(result)


def test_sd_metadata_excludes_negative_prompt_and_sampling_settings():
    result = read_reference_metadata(png_bytes(parameters=(
        "1girl, (blue dress:1.2)\nNegative prompt: bad hands\nSteps: 28, Sampler: Euler a, CFG scale: 7"
    )))
    assert result["source"] == "sd_parameters"
    assert result["prompt"] == "1girl, (blue dress:1.2)"
    assert result["normalize"] is True


def test_recovered_controls_removed_without_destroying_content_weights():
    controls = json.loads(DATA_PATH.read_text())["control_tags"]
    prompt = (
        "artist:old, 1.2::artist:a, {artist:b}, best quality::, "
        "{{artist:c, 1girl}}, 1.1::blue dress, long hair::, "
        "[amazing_quality], rating:safe, absurdres, 1girl"
    )
    assert strip_reference_controls(prompt, controls) == "{{1girl}}, 1.1::blue dress, long hair::, 1girl"


@pytest.mark.parametrize("response,expected", [
    ('```json\n{"prompt":"1girl, blue dress"}\n```', "1girl, blue dress"),
    ('{"prompt":"1girl"}', "1girl"),
    ('{"prompt":"1girl","ok":false}', ""),
    ('{"prompt":1}', ""),
    ('{"prompt":["1girl"]}', ""),
    ('{"error":"not supported"}', ""),
    ("I cannot see the image", ""),
])
def test_reference_response_requires_a_structured_prompt(response, expected):
    assert parse_reference_response(response) == expected


def test_metadata_without_edits_needs_no_vision_or_llm():
    ref = ImageReference(data=png_bytes(Comment={
        "prompt": "artist:old, 1girl, blue dress, best quality", "steps": 28, "seed": 1,
    }), mime_type="image/png")
    call_llm = AsyncMock()
    prompt, metadata = asyncio.run(resolve_reference_prompt(call_llm, None, [ref], ""))
    call_llm.assert_not_awaited()
    assert prompt == "1girl, blue dress"
    assert metadata["visual_image_count"] == 0
    assert metadata["references"][0]["source"] == "png_comment"


def test_mixed_references_keep_indices_and_only_missing_metadata_uses_vision():
    original = png_bytes(Comment={"prompt": "1girl, red dress", "seed": 1, "steps": 28})
    screenshot = png_bytes(Description="ordinary screenshot")
    refs = [ImageReference(data=data, mime_type="image/png") for data in (original, screenshot)]
    call_llm = AsyncMock(return_value='{"prompt":"1girl, blue dress, standing"}')
    prompt, metadata = asyncio.run(resolve_reference_prompt(call_llm, None, refs, "站姿蓝裙"))
    assert prompt == "1girl, blue dress, standing"
    assert call_llm.call_args.kwargs["images"] == [screenshot]
    assert call_llm.call_args.kwargs["use_persona"] is False
    assert metadata["references"] == [
        {"index": 1, "source": "png_comment", "prompt": "1girl, red dress", "characters": []},
        {"index": 2, "source": "vision", "image_index": 1},
    ]


def test_character_captions_are_normalized_instead_of_silently_dropped():
    ref = ImageReference(data=png_bytes(Comment={
        "seed": 1, "steps": 28, "v4_prompt": {"caption": {
            "base_caption": "2girls", "char_captions": [
                {"char_caption": "red hair, red dress"}, {"char_caption": "blue hair, blue dress"},
            ],
        }},
    }), mime_type="image/png")
    call_llm = AsyncMock(return_value='{"prompt":"2girls, girl with red hair and red dress, girl with blue hair and blue dress"}')
    prompt, _ = asyncio.run(resolve_reference_prompt(call_llm, None, [ref], ""))
    call_llm.assert_awaited_once()
    assert '"characters": ["red hair, red dress", "blue hair, blue dress"]' in call_llm.call_args.args[1]
    assert call_llm.call_args.kwargs["images"] == []
    assert "red hair and red dress" in prompt
