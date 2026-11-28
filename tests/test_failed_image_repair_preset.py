"""Keep anatomy repair available in persisted stores and command prompts."""

import json

import pytest

from astrbot_plugin_selfie_image.prompts.command_parser import expand_user_text_with_preset
from astrbot_plugin_selfie_image.prompts.preset import (
    ImagePresetManager,
    LEGACY_BUILTIN_PROMPT_REPLACEMENTS,
)
from astrbot_plugin_selfie_image.studio.studio import (
    BUILTIN_PROMPTS,
    global_prompt_presets,
    prompt_owns_staging,
)


REPAIR = next(item for item in BUILTIN_PROMPTS if item["id"] == "i2i_failed_image_repair")


@pytest.mark.parametrize("customized", [False, True])
def test_legacy_repair_migrates_only_the_unmodified_prompt(tmp_path, customized):
    prompt = LEGACY_BUILTIN_PROMPT_REPLACEMENTS["废图修复"]
    if customized:
        prompt += "\n用户追加的修复要求"
    original = {
        "prompt": prompt,
        "aspect_ratio": "3:4",
        "resolution": "2K",
        "description": "保留的说明",
        "extra_prompt": "保留原发型",
    }
    path = tmp_path / ImagePresetManager.PRESET_FILENAME
    path.write_text(json.dumps({"废图修复": original}), encoding="utf-8")

    manager = ImagePresetManager(str(tmp_path))
    expected = dict(original, prompt=prompt if customized else REPAIR["prompt"])
    assert json.loads(path.read_text(encoding="utf-8"))["废图修复"] == expected
    assert manager.presets["废图修复"].prompt == expected["prompt"]
    assert expected["prompt"] != LEGACY_BUILTIN_PROMPT_REPLACEMENTS["废图修复"]

    # Reopening the migrated store must not rewrite it or bump its revision.
    contents = path.read_bytes()
    revision_path = path.with_name(path.name + ".revision.json")
    revision = revision_path.read_bytes()
    ImagePresetManager(str(tmp_path))
    assert path.read_bytes() == contents
    assert revision_path.read_bytes() == revision

    expanded, aspect, resolution, name = expand_user_text_with_preset(
        "废图修复 校正抬起的脚",
        presets=manager,
        default_aspect_ratio="9:16",
        default_resolution="1K",
    )
    assert expanded == expected["prompt"] + " 保留原发型 校正抬起的脚"
    assert (aspect, resolution, name) == ("3:4", "2K", "废图修复")


def test_deleted_repair_preset_is_not_restored_on_upgrade(tmp_path):
    path = tmp_path / ImagePresetManager.PRESET_FILENAME
    path.write_text(json.dumps({
        "__deleted_builtin_presets__": {"names": ["废图修复"]},
    }), encoding="utf-8")
    manager = ImagePresetManager(str(tmp_path))
    assert not manager.has_preset("废图修复")
    assert not ImagePresetManager(str(tmp_path)).has_preset("废图修复")
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert "废图修复" not in stored
    assert stored["__deleted_builtin_presets__"]["names"] == ["废图修复"]


def test_fresh_repair_is_global_and_keeps_control_of_staging(tmp_path):
    manager = ImagePresetManager(str(tmp_path))
    expanded, _, _, name = expand_user_text_with_preset(
        "废图修复", presets=manager,
        default_aspect_ratio="9:16", default_resolution="1K",
    )
    assert name == REPAIR["title"]
    assert expanded == REPAIR["prompt"]
    assert prompt_owns_staging(expanded)
    assert any(item["id"] == REPAIR["id"] for item in global_prompt_presets())
