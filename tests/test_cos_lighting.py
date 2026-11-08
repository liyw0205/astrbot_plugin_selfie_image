"""Check lighting at the final COS prompt boundary without image requests."""

import re

import pytest

from astrbot_plugin_selfie_image.cos import cos_looks as cos
from astrbot_plugin_selfie_image.cos.cos_looks_loader import (
    COS_JSON_CONTACT_SHADOW_PROMPTS,
    COS_JSON_LIGHTINGS,
    COS_JSON_LIGHTING_RULES,
    COS_JSON_POSE_REQUEST_ALIASES,
    COS_JSON_SCENES,
    COS_JSON_STYLES,
    validate_json_catalog,
)
from astrbot_plugin_selfie_image.features.persona import PersonaManager
from astrbot_plugin_selfie_image.core.utils import compact_generation_record
from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt


def selected_id(prompt, key):
    return re.search(rf"【cos_{key}:([a-z0-9_]+)】", prompt).group(1)


@pytest.mark.parametrize("scene_id", COS_JSON_SCENES)
def test_each_scene_emits_one_compatible_light_and_one_natural_style(tmp_path, scene_id):
    assert validate_json_catalog() == []
    scene = COS_JSON_SCENES[scene_id]
    pose_id = scene["pose_ids"][0]
    action = cos.build_cos_look_action(
        f"{scene['aliases'][0]} {COS_JSON_POSE_REQUEST_ALIASES[pose_id][0]} 自然手机快照",
        force_id="roxy_cream", camera="third", has_refs=True,
    )
    final = PersonaManager(str(tmp_path)).build_selfie_prompt(
        action, bot_name="", personality="", has_reference_image=True, extra_reference_count=1,
    )
    lighting_id = selected_id(final, "lighting")
    assert lighting_id in scene["lighting_ids"]
    assert final.count(COS_JSON_LIGHTINGS[lighting_id]["prompt"]) == 1
    assert final.count(COS_JSON_LIGHTING_RULES["source_consistency"]) == 1
    assert final.count(COS_JSON_STYLES["natural_snapshot"]["prompt"]) == 1
    assert selected_id(final, "style") == "natural_snapshot"
    assert "高光泛白" not in final
    assert "胶片颗粒" not in final
    assert scene["style_weights"]["natural_snapshot"] > 1


@pytest.mark.parametrize("pose_id,alias,shadow_id", [
    ("wall_raised_elbow_lean", "抬臂靠墙侧背前倾", "wall_elbow"),
    ("kneeling_back_support", "跪坐后撑仰身", "kneeling_back"),
    ("side_edge_bend", "侧边支撑前倾", "edge_hands"),
    ("rear_wall_curve", "背身扶墙微弯", "wall_hands"),
    ("side_profile", "纯侧身", "default"),
    ("back_to_camera", "背对镜头", "default"),
])
def test_occluded_faces_keep_pose_shadows_without_face_fill(tmp_path, pose_id, alias, shadow_id):
    action = cos.build_cos_look_action(
        f"卧室地毯 {alias} 自然手机快照",
        force_id="roxy_off_shoulder_sleep_dress", camera="third", has_refs=True,
    )
    final = PersonaManager(str(tmp_path)).build_selfie_prompt(
        action, bot_name="", personality="", has_reference_image=True, extra_reference_count=1,
    )
    assert selected_id(final, "pose") == pose_id
    assert final.count(COS_JSON_CONTACT_SHADOW_PROMPTS[shadow_id]) == 1
    assert COS_JSON_LIGHTING_RULES["face_optional"] in final
    assert COS_JSON_LIGHTING_RULES["face_visible"] not in final
    assert cos.COS_FACE_RISK_PROMPTS["hair_shadow"] not in final
    assert cos.COS_FACE_RISK_PROMPTS["hat_shadow"] not in final
    assert "双眼、眉毛和鼻梁保持完整可见" not in final
    assert "面部必须得到镜头侧前方填光" not in final


def test_changing_camera_angle_keeps_the_requested_scene_light():
    for framing_id in ("side_low_medium", "side_back_low_medium"):
        action = cos.build_cos_look_action(
            "卧室地毯 抬臂靠墙侧背前倾 室内柔和侧光 自然手机快照",
            force_id="roxy_cream", camera="third",
            avoid_framings=[key for key in cos.COS_FRAMING_CLASSES if key != framing_id],
        )
        assert selected_id(action, "framing") == framing_id
        assert selected_id(action, "lighting") == "room_side_soft"
        assert action.count(COS_JSON_LIGHTINGS["room_side_soft"]["prompt"]) == 1


@pytest.mark.parametrize("style_request", ["暖调日常快照", "复古 CCD"])
def test_selected_film_effect_is_applied_once(tmp_path, style_request):
    action = cos.build_cos_look_action(
        f"卧室地毯 跪坐后撑仰身 {style_request}", force_id="roxy_cream", camera="third",
    )
    final = PersonaManager(str(tmp_path)).build_selfie_prompt(
        action, bot_name="", personality="", has_reference_image=True,
    )
    assert selected_id(final, "style") != "natural_snapshot"
    assert final.count(COS_JSON_STYLES[selected_id(final, "style")]["prompt"]) == 1
    assert final.count("泛白") == 1
    assert "柔和正面光" not in final
    assert "柔和棚拍面光" not in final


@pytest.mark.parametrize("lighting_request", ["光线：正面闪光灯", "不要窗光，不要补光", "不要室内柔和侧光"])
def test_explicit_light_constraints_do_not_get_a_random_light(tmp_path, lighting_request):
    action = cos.build_cos_look_action(
        f"卧室地毯 抬臂靠墙侧背前倾 {lighting_request}", force_id="roxy_cream", camera="third",
    )
    final = PersonaManager(str(tmp_path)).build_selfie_prompt(
        action, bot_name="", personality="", has_reference_image=True,
    )
    assert selected_id(final, "lighting") == "user"
    assert lighting_request in final
    assert COS_JSON_LIGHTING_RULES["user_face"] in final
    assert all(light["prompt"] not in final for light in COS_JSON_LIGHTINGS.values())


def test_web_cos_and_collage_keep_one_light_contract():
    web = cos.build_cos_third_person_prompt("COS 换装：蓝色礼服，摄影棚，抬臂靠墙侧背前倾")
    assert selected_id(web, "lighting") == "studio_side"
    assert cos.build_cos_third_person_prompt(web) == web
    assert web.count(COS_JSON_LIGHTINGS["studio_side"]["prompt"]) == 1

    collage = cos.build_cos_look_action(
        "摄影棚 多视角拼贴 自然手机快照", force_id="roxy_cream", camera="third",
        force_multi_view_collage=True,
    )
    assert "【cos_view:multi_view_collage】" in collage
    assert collage.count(COS_JSON_LIGHTINGS["studio_side"]["prompt"]) == 1
    assert collage.count(COS_JSON_LIGHTING_RULES["collage"]) == 1
    assert cos.build_cos_third_person_prompt(collage) == collage


def test_builtin_cos_fallback_keeps_outdoor_lighting():
    action = cos.build_cos_look_action(
        "日落天台 自然站姿 自然手机快照", force_id="roxy_cream", camera="third",
    )
    chinese = build_selfie_builtin_prompt(action, appearance_type="real")
    english = build_selfie_builtin_prompt(action, language="en", appearance_type="real")
    assert "使用窗光或房间环境光" not in chinese
    assert english.count(COS_JSON_LIGHTINGS[selected_id(action, "lighting")]["prompt"]) == 1


def test_generation_record_retains_selected_light_across_compaction():
    compact = compact_generation_record({
        "original_prompt": "COS 【cos_lighting:studio_side】",
        "request_data": {"cos_lighting": "studio_side"},
    })
    assert compact["cos_lighting"] == "studio_side"
    assert compact_generation_record(compact)["request_data"]["cos_lighting"] == "studio_side"


def test_first_person_side_and_back_views_do_not_force_visible_faces():
    for alias in ("背身扶墙微弯", "纯侧身"):
        action = cos.build_cos_look_action(
            f"卧室地毯 {alias}", force_id="ninghongye_clear_eye", camera="first",
        )
        assert "保持面部、发型、服装和配饰清晰可见" not in action
        assert "双眼自然可见" not in action
        assert COS_JSON_LIGHTING_RULES["face_optional"] in action
