"""Exercise the retained compound staging entries through real COS selection."""

import re

import pytest

from astrbot_plugin_selfie_image.cos import cos_looks as cos
from astrbot_plugin_selfie_image.cos.cos_looks_loader import validate_json_catalog
from astrbot_plugin_selfie_image.features.persona import PersonaManager


BUNDLES = (
    ("standing_support_leg_other_knee_high_bent", "pose_reference_room", "ground_oblique_raised_foot_foreground"),
    ("kneeling_upright_front_hem_level_lift", "pose_reference_room", "kneeling_front_level_hem_oblique_high"),
    ("standing_staggered_front_hem_side_lift", "pose_reference_room", "staggered_hem_side_lift_full_oblique"),
    ("front_oblique_chest_waist_upper_thigh_crop", "pose_reference_room", "front_oblique_neck_to_upper_thigh"),
    ("standing_support_other_knee_up_foot_down", "pose_reference_room", "low_oblique_knee_up_foot_down_diagonal"),
    ("ancient_room_table_supine_head_over_edge_legs_up", "ancient_wood_table_room", "table_inverted_head_raised_legs"),
    ("ancient_style_face_close_high_angle_portrait", "ancient_soft_portrait_room", "ancient_face_close_high_angle"),
    ("hallway_wall_lean_palm_support_tiptoe_stance", "light_wood_hallway", "hallway_wall_palm_tiptoe_full"),
    ("home_gaming_chair_side_sit_crossed_leg_feet_suspended", "cartoon_gaming_chair_room", "gaming_chair_crossed_leg_feet_suspended"),
    ("blue_pool_upper_body_float_ancient_style_close_portrait", "clear_blue_portrait_pool", "blue_pool_chest_waterline_close"),
    ("shallow_water_standing_both_hands_hem_lift_low_waist_leg_crop", "golden_shallow_water", "shallow_water_low_waist_to_legs"),
    ("ancient_hot_spring_seated_lean_forward_overlapped_hands", "misty_petals_hot_spring", "hot_spring_chest_waterline_overlapped_hands"),
)


def selected_id(prompt, key):
    match = re.search(rf"【cos_{key}:([a-z0-9_]+)】", prompt)
    assert match, f"missing {key} in {prompt}"
    return match.group(1)


def assert_bundle(action, pose_id, scene_id, framing_id):
    assert selected_id(action, "pose") == pose_id
    assert selected_id(action, "scene") == scene_id
    assert selected_id(action, "framing") == framing_id
    assert "【cos_composition:" not in action
    assert cos.COS_RANDOM_POSE_CLASSES[pose_id]["prompt"] in action
    assert cos.COS_RANDOM_SCENE_CLASSES[scene_id]["prompt"] in action
    assert cos.COS_FRAMING_CLASSES[framing_id]["prompt"] in action
    assert "9:16" in action


def test_catalog_has_no_unreachable_pose_camera_framing_paths():
    assert validate_json_catalog() == []


@pytest.mark.parametrize("pose_id,scene_id,framing_id", BUNDLES)
@pytest.mark.parametrize("request_kind", ["alias", "id"])
def test_named_compound_survives_exhausted_rotation_and_final_prompt(tmp_path, pose_id, scene_id, framing_id, request_kind):
    request = cos.COS_POSE_REQUEST_ALIASES[pose_id][0] if request_kind == "alias" else pose_id
    action = cos.build_cos_look_action(
        request,
        force_id="roxy_off_shoulder_sleep_dress", camera="third", has_refs=True,
        avoid_poses=list(cos.COS_RANDOM_POSE_CLASSES),
        avoid_scenes=list(cos.COS_RANDOM_SCENE_CLASSES),
        avoid_framings=list(cos.COS_FRAMING_CLASSES),
    )
    final = PersonaManager(str(tmp_path)).build_selfie_prompt(
        action, bot_name="", personality="", has_reference_image=True,
        has_external_reference=False,
    )
    assert_bundle(final, pose_id, scene_id, framing_id)
    assert cos.get_cos_look_by_id("roxy_off_shoulder_sleep_dress")["prompt"] in final


@pytest.mark.parametrize("pose_id,scene_id,framing_id", BUNDLES)
def test_compound_is_reachable_without_an_explicit_pose_request(monkeypatch, pose_id, scene_id, framing_id):
    monkeypatch.setattr(cos.random, "random", lambda: 1.0)
    action = cos.build_cos_look_action(
        force_id="roxy_off_shoulder_sleep_dress", camera="third",
        avoid_poses=[key for key in cos.COS_RANDOM_POSE_CLASSES if key != pose_id],
        avoid_scenes=[key for key in cos.COS_RANDOM_SCENE_CLASSES if key != scene_id],
    )
    assert_bundle(action, pose_id, scene_id, framing_id)


@pytest.mark.parametrize("pose_id,scene_id,framing_id", [
    bundle for bundle in BUNDLES
    if "selfie" in cos.COS_RANDOM_POSE_CLASSES[bundle[0]]["random_camera_ids"]
])
def test_selfie_uses_fixed_camera_without_taking_a_hand_from_the_pose(pose_id, scene_id, framing_id):
    action = cos.build_cos_look_action(
        cos.COS_POSE_REQUEST_ALIASES[pose_id][0], force_id="roxy_off_shoulder_sleep_dress", camera="selfie",
    )
    assert_bundle(action, pose_id, scene_id, framing_id)
    assert "采用固定或定时自拍" in action
    assert "握持唯一一部普通手机" not in action


def test_raised_knee_and_foot_down_remain_different_without_left_right_labels():
    knee_high = cos.COS_RANDOM_POSE_CLASSES[BUNDLES[0][0]]["prompt"]
    foot_down = cos.COS_RANDOM_POSE_CLASSES[BUNDLES[4][0]]["prompt"]
    assert "跨过身体中线" in knee_high
    assert "小腿向前" in knee_high
    assert "悬空脚贴近镜头" in knee_high
    assert "斜向下垂" in foot_down and "脚尖向下" in foot_down
    for text in (knee_high, foot_down):
        assert not any(term in text for term in ("左腿", "右腿", "画面左", "画面右"))


def test_inverted_table_portrait_keeps_closed_eyes_in_expression_layer():
    pose_id = BUNDLES[5][0]
    expression_id, prompt = cos._pick_scene_expression(pose_id)
    assert prompt == cos.COS_JSON_EXPRESSIONS[expression_id]["closed_eyes_prompt"]
    assert "双眼轻闭" in cos.COS_RANDOM_POSE_CLASSES[pose_id]["prompt"]
    assert "视线稳定" not in prompt


@pytest.mark.parametrize("pose_id", [BUNDLES[1][0], BUNDLES[2][0], BUNDLES[10][0]])
def test_hem_actions_do_not_randomize_for_a_trouser_only_costume(monkeypatch, pose_id):
    original = cos.get_cos_look_by_id

    def get_look(look_id):
        if look_id == "trouser_fixture":
            return {"id": look_id, "prompt": "贴身背心，深灰色长裤，短靴。"}
        return original(look_id)

    monkeypatch.setattr(cos, "get_cos_look_by_id", get_look)
    profile = cos.cos_look_compatibility("trouser_fixture")
    assert pose_id not in profile["recommended_pose_ids"]
    assert "golden_shallow_water" not in profile["recommended_scene_ids"]
    variation = cos.pick_cos_variation(
        "trouser_fixture", camera="third",
        extra_request=cos.COS_POSE_REQUEST_ALIASES[pose_id][0],
    )
    assert variation["pose_id"] != pose_id
    assert variation["selection_notice"]


@pytest.mark.parametrize("index", [6, 9, 10])
def test_compound_does_not_replace_cos_hair_or_costume(index):
    pose = cos.COS_RANDOM_POSE_CLASSES[BUNDLES[index][0]]["prompt"]
    assert "当前 COS" in pose
    assert not any(term in pose for term in ("黑色蓬松古风发型", "碎钻额饰", "白花发饰", "浅灰古风薄纱长裙"))
