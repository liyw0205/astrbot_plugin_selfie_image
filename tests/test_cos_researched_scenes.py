"""Exercise screenshot-derived scenes through the COS action boundary."""

import re

import pytest

from astrbot_plugin_selfie_image.cos import cos_looks as cos
from astrbot_plugin_selfie_image.cos.cos_looks_loader import (
    COS_JSON_LIGHTINGS,
    COS_JSON_POSES,
    COS_JSON_SCENES,
    COS_JSON_STYLES,
    validate_json_catalog,
)
from astrbot_plugin_selfie_image.features.persona import PersonaManager


SCENE_STYLES = {
    "blue_violet_poolside": "water_digital_portrait",
    "cool_washbasin": "cool_indoor_digital",
    "stone_bathroom": "warm_gray_snapshot",
    "daylight_car_backseat": "soft_gray_backlit",
}
CAMERAS = ("third", "first", "selfie")


def selected_id(prompt, key):
    return re.search(rf"【cos_{key}:([a-z0-9_]+)】", prompt).group(1)


def assert_compatible_selection(action, scene_id):
    scene = COS_JSON_SCENES[scene_id]
    assert selected_id(action, "scene") == scene_id
    pose_id = selected_id(action, "pose")
    assert pose_id in scene["pose_ids"]
    frame_id = selected_id(action, "framing")
    assert frame_id in COS_JSON_POSES[pose_id]["framing_ids"]
    assert scene_id in cos.COS_FRAMING_SCENE_COMPATIBILITY.get(frame_id, (scene_id,))
    assert selected_id(action, "lighting") in scene["lighting_ids"]
    assert selected_id(action, "style") in scene["style_ids"]
    assert "可对镜取景" not in action
    assert "按明确要求对镜取景" not in action
    assert "握持唯一一部普通手机" not in action


@pytest.mark.parametrize("scene_id", SCENE_STYLES)
@pytest.mark.parametrize("camera", CAMERAS)
def test_researched_scene_defaults_remain_compatible_with_exhausted_rotation(scene_id, camera):
    action = cos.build_cos_look_action(
        COS_JSON_SCENES[scene_id]["aliases"][0],
        force_id="roxy_cream", camera=camera,
        avoid_scenes=list(cos.COS_RANDOM_SCENE_CLASSES),
        avoid_poses=list(cos.COS_RANDOM_POSE_CLASSES),
        avoid_framings=list(cos.COS_FRAMING_CLASSES),
    )
    assert_compatible_selection(action, scene_id)


@pytest.mark.parametrize("scene_id", SCENE_STYLES)
@pytest.mark.parametrize("camera", CAMERAS)
def test_style_only_request_selects_its_environment_even_with_combination_picker(monkeypatch, scene_id, camera):
    monkeypatch.setattr(cos.random, "random", lambda: 0.0)
    style_id = SCENE_STYLES[scene_id]
    action = cos.build_cos_look_action(
        COS_JSON_STYLES[style_id]["aliases"][0], force_id="roxy_cream", camera=camera,
    )
    assert_compatible_selection(action, scene_id)
    assert selected_id(action, "style") == style_id


@pytest.mark.parametrize("scene_id,pose_id", [
    (scene_id, pose_id)
    for scene_id in SCENE_STYLES
    for pose_id in COS_JSON_SCENES[scene_id]["pose_ids"]
])
@pytest.mark.parametrize("camera", CAMERAS)
def test_each_researched_pose_and_crop_keeps_one_light_and_the_original_costume(tmp_path, scene_id, pose_id, camera):
    for framing_id in COS_JSON_POSES[pose_id]["framing_ids"]:
        action = cos.build_cos_look_action(
            f"{COS_JSON_SCENES[scene_id]['aliases'][0]} {cos.COS_POSE_REQUEST_ALIASES[pose_id][0]}",
            force_id="ninghongye_clear_eye", camera=camera, has_refs=True,
            avoid_framings=[key for key in cos.COS_FRAMING_CLASSES if key != framing_id],
        )
        final = PersonaManager(str(tmp_path)).build_selfie_prompt(
            action, bot_name="", personality="", has_reference_image=True,
            has_external_reference=False,
        )
        assert_compatible_selection(final, scene_id)
        assert selected_id(final, "pose") == pose_id
        assert selected_id(final, "framing") == framing_id
        assert final.count(cos.COS_FULL_EYE_COVER_PROMPT) == 1
        lighting_id = selected_id(final, "lighting")
        assert final.count(COS_JSON_LIGHTINGS[lighting_id]["prompt"]) == 1
        style_id = selected_id(final, "style")
        assert final.count(COS_JSON_STYLES[style_id]["prompt"]) == 1


@pytest.mark.parametrize("scene_id", SCENE_STYLES)
def test_small_scene_pose_pools_fall_back_from_collage_without_other_environments(scene_id):
    action = cos.build_cos_look_action(
        f"{COS_JSON_SCENES[scene_id]['aliases'][0]} 多视角拼贴",
        force_id="roxy_cream", camera="third", force_multi_view_collage=True,
    )
    assert_compatible_selection(action, scene_id)
    assert "【cos_view:multi_view_collage】" not in action
    assert "兼容姿势不足" in action


@pytest.mark.parametrize("constraint", ["胸像近景", "低机位", "光线：正面闪光灯"])
def test_parsed_backlight_scene_alias_preserves_independent_user_constraints(constraint):
    action = cos.build_cos_look_action(
        f"车内后排逆光 {constraint}", force_id="roxy_cream", camera="third",
    )
    assert selected_id(action, "scene") == "daylight_car_backseat"
    assert constraint in action
    if constraint.startswith("光线"):
        assert selected_id(action, "lighting") == "user"
        assert selected_id(action, "framing") != "user"
    else:
        assert selected_id(action, "framing") == "user"
        assert selected_id(action, "lighting") == "car_window_backlight"


@pytest.mark.parametrize("framing_id,scene_id", [
    ("poolside_medium", "cool_washbasin"),
    ("washbasin_rear_medium", "daylight_car_backseat"),
    ("washbasin_front_medium", "blue_violet_poolside"),
    ("car_window_close", "stone_bathroom"),
])
def test_environment_specific_crops_cannot_be_forced_into_another_scene(framing_id, scene_id):
    pose_id = COS_JSON_SCENES[scene_id]["pose_ids"][0]
    selected = cos.pick_cos_framing(
        pose_id=pose_id, scene_id=scene_id, camera="third",
        forced_framing_id=framing_id,
    )
    assert selected["view_id"] != framing_id


def test_custom_environment_does_not_invent_a_pool_car_or_washbasin_support():
    constrained_poses = {
        pose_id for pose_id, pose in COS_JSON_POSES.items() if pose.get("scene_ids")
    }
    variation = cos.pick_cos_variation(
        "roxy_cream", extra_request="场景：红色摄影幕布", camera="third",
        avoid_poses=[pose_id for pose_id in COS_JSON_POSES if pose_id not in constrained_poses],
    )
    assert variation["scene_id"] == "user"
    assert variation["pose_id"] not in constrained_poses


@pytest.mark.parametrize("scene_ids", [42, "blue_violet_poolside", [], ["unknown_scene"]])
def test_pose_scene_validation_reports_invalid_restrictions(monkeypatch, scene_ids):
    monkeypatch.setitem(COS_JSON_POSES["poolside_seated_support"], "scene_ids", scene_ids)
    assert "pose poolside_seated_support: invalid scene_ids" in validate_json_catalog()


def test_pose_scene_validation_reports_incompatible_scene_pairs(monkeypatch):
    monkeypatch.setitem(COS_JSON_POSES["poolside_seated_support"], "scene_ids", ["stone_bathroom"])
    assert (
        "scene blue_violet_poolside: poses incompatible poolside_seated_support"
        in validate_json_catalog()
    )
