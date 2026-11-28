"""COS composition effects are explicitly selected ordinary built-in presets."""

import json
import re
from unittest.mock import patch

import pytest

from astrbot_plugin_selfie_image.cos import cos_looks as cos
from astrbot_plugin_selfie_image.cos.cos_looks_loader import COS_DATA_DIR
from astrbot_plugin_selfie_image.prompts.command_parser import extract_prompt_aspect_ratio
from astrbot_plugin_selfie_image.prompts.preset import ImagePresetManager
from astrbot_plugin_selfie_image.studio.studio import (
    BUILTIN_PROMPTS,
    global_prompt_presets,
    prompt_preset_group,
)


MIGRATED_IDS = (
    "photo_frame_top_bottom_break",
    "photo_frame_bottom_left_break",
    "multi_view_collage",
)
PRESETS = [item for item in BUILTIN_PROMPTS if item["id"] in MIGRATED_IDS]


def test_compositions_are_no_longer_in_any_cos_pool():
    assert not set(MIGRATED_IDS) & cos.COS_COMBINATION_PRESETS.keys()
    assert not any(key.startswith("photo_frame") for key in cos.COS_RANDOM_POSE_CLASSES)
    assert "photo_frame_studio" not in cos.COS_RANDOM_SCENE_CLASSES
    assert "photo_frame_front_medium" not in cos.COS_FRAMING_CLASSES
    assert not any(key.startswith("photo_frame") for key in cos.COS_POSE_REQUEST_ALIASES)
    with (COS_DATA_DIR / "_compositions.json").open(encoding="utf-8") as handle:
        catalog = json.load(handle)
    assert "multi_view" not in catalog
    assert "multi_view_request_terms" not in catalog


@pytest.mark.parametrize("existing", [False, True])
def test_migrated_presets_are_ordinary_globals_and_seed_existing_stores(tmp_path, existing):
    if existing:
        (tmp_path / ImagePresetManager.PRESET_FILENAME).write_text(
            json.dumps({"custom": {"prompt": "keep my custom prompt"}}), encoding="utf-8",
        )
    manager = ImagePresetManager(str(tmp_path))
    visible = {row["id"]: row for row in global_prompt_presets()}
    assert {item["id"] for item in PRESETS} == set(MIGRATED_IDS)
    for item in PRESETS:
        assert prompt_preset_group(item) == ""
        assert visible[item["id"]]["source"] == "builtin"
        assert visible[item["id"]]["preset_group"] == ""
        resolved = manager.resolve(item["title"])
        assert resolved["preset_name"] == item["title"]
        assert resolved["prompt"] == item["prompt"]
    if existing:
        assert manager.resolve("custom")["prompt"] == "keep my custom prompt"


@pytest.mark.parametrize("preset", PRESETS, ids=lambda item: item["id"])
@pytest.mark.parametrize("has_refs", [False, True])
def test_cos_preset_owns_staging_and_keeps_the_selected_outfit(preset, has_refs):
    look = {
        "id": "test_outfit", "title": "fixture costume", "cos_type": "fixture series",
        "prompt": "蓝色外套，银色纽扣，白色发带。",
    }
    with (
        patch.object(cos, "pick_cos_variation", side_effect=AssertionError("random staging")),
        patch.object(cos, "pick_cos_framing", side_effect=AssertionError("random crop")),
    ):
        action = cos.build_cos_look_action(
            preset["prompt"] + "，保留原配饰", has_refs,
            picker=lambda **_: look,
        )
        assert cos.build_cos_third_person_prompt(action) == action
        web = cos.build_cos_third_person_prompt("COS 换装：" + look["prompt"] + preset["prompt"])
        assert cos.build_cos_third_person_prompt(web) == web
    for prompt in (action, web):
        assert prompt.count(preset["prompt"]) == 1
        assert look["prompt"] in prompt
        assert "【cam:third】" in prompt
        assert "【cos_staging:preset】" in prompt
        assert not re.search(r"【cos_(?:pose|scene|framing|combo|view):", prompt)
    assert "保留原配饰" in action
    assert ("引用图片作为本次主角身份来源" in action) is has_refs


def test_ordinary_cos_does_not_randomly_produce_frames_or_collages():
    def choose(values):
        return True if isinstance(values[0], bool) else values[0]

    with patch.object(cos.random, "choice", side_effect=choose):
        for camera in ("third", "selfie", "first"):
            action = cos.build_cos_look_action("", force_id="roxy_cream", camera=camera)
            assert "拼贴" not in action
            assert "白边照片" not in action
            assert "【cos_staging:preset】" not in action
            assert f"【cam:{camera}】" in action


def test_collage_preset_retains_seven_panels_and_portrait_ratio():
    preset = next(item for item in PRESETS if item["id"] == "multi_view_collage")
    assert extract_prompt_aspect_ratio(preset["prompt"]) == "9:16"
    assert "严格为七张独立照片" in preset["prompt"]
    assert "中央竖幅主画格（大图）" in preset["prompt"]
    assert "同一场景光源" in preset["prompt"]
