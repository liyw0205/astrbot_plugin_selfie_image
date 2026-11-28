"""Regression coverage for the shared soft-surface reclining COS pool."""

import re

import pytest

from astrbot_plugin_selfie_image.cos import cos_looks as cos
from astrbot_plugin_selfie_image.cos.cos_looks_loader import (
    COS_JSON_LOOKS_BY_ID,
    COS_JSON_POSES,
    COS_JSON_SCENES,
)


SCENES = (
    "bedroom_carpet",
    "living_room_rug",
    "gray_rug_recline",
    "gray_bed_recline",
)
CAMERAS = ("third", "first", "selfie")
RECLINE_POSES = {
    "soft_surface_recline_support",
    "soft_surface_recline_hem_hold",
}
HEM_LOOKS = (
    "original_skyblue_lace_cami",
    "original_blue_anime_print_cami",
)


def tag(action, key):
    match = re.search(rf"【cos_{key}:([a-z0-9_]+)】", action)
    assert match, f"missing COS {key} tag: {action}"
    return match.group(1)


@pytest.mark.parametrize("look_id", HEM_LOOKS)
def test_new_camis_are_searchable_and_hem_pose_is_explicit(look_id):
    look = COS_JSON_LOOKS_BY_ID[look_id]
    assert cos.pick_cos_look_set(query=look["title"], exclude_special=True)["id"] == look_id

    action = cos.build_cos_look_action(
        "灰色地毯角落 半躺轻捏衣摆",
        force_id=look_id,
        camera="third",
    )
    assert tag(action, "pose") == "soft_surface_recline_hem_hold"
    assert tag(action, "scene") == "gray_rug_recline"
    assert tag(action, "framing") == "recline_high_close"


@pytest.mark.parametrize("scene_id", SCENES)
@pytest.mark.parametrize("camera", CAMERAS)
@pytest.mark.parametrize("pose_id", sorted(RECLINE_POSES))
def test_recline_pose_uses_compatible_scene_crop_and_camera(scene_id, camera, pose_id):
    scene_alias = COS_JSON_SCENES[scene_id]["aliases"][0]
    action = cos.build_cos_look_action(
        f"{scene_alias} {cos.COS_POSE_REQUEST_ALIASES[pose_id][0]}",
        force_id=HEM_LOOKS[0],
        camera=camera,
        avoid_scenes=list(cos.COS_RANDOM_SCENE_CLASSES),
        avoid_poses=list(cos.COS_RANDOM_POSE_CLASSES),
        avoid_framings=list(cos.COS_FRAMING_CLASSES),
    )

    assert tag(action, "scene") == scene_id
    assert tag(action, "pose") == pose_id
    assert tag(action, "framing") == "recline_high_close"
    assert f"【cam:{camera}】" in action
    assert "闭眼" not in action
    assert "床上自然熟睡" not in action


@pytest.mark.parametrize("scene_id", ["gray_rug_recline", "gray_bed_recline"])
@pytest.mark.parametrize("camera", CAMERAS)
def test_new_scene_alone_defaults_to_base_pose_with_exhausted_rotation(scene_id, camera):
    action = cos.build_cos_look_action(
        COS_JSON_SCENES[scene_id]["aliases"][0],
        force_id="roxy_cream",
        camera=camera,
        avoid_scenes=list(cos.COS_RANDOM_SCENE_CLASSES),
        avoid_poses=list(cos.COS_RANDOM_POSE_CLASSES),
        avoid_framings=list(cos.COS_FRAMING_CLASSES),
    )
    assert tag(action, "scene") == scene_id
    assert tag(action, "pose") == "soft_surface_recline_support"
    assert tag(action, "framing") == "recline_high_close"
    assert "用户补充要求优先" not in action


def test_shared_base_pose_is_available_to_other_cos_looks():
    variation = cos.pick_cos_variation(
        "roxy_cream",
        extra_request="深灰床面 半躺后撑",
        camera="third",
    )
    assert variation["pose_id"] == "soft_surface_recline_support"
    assert variation["scene_id"] == "gray_bed_recline"


@pytest.mark.parametrize("look_id", ["roxy_cream", "original_ccd_sleepwear_overknees"])
def test_other_outfits_keep_the_requested_hem_pose(look_id):
    variation = cos.pick_cos_variation(
        look_id,
        extra_request="灰色地毯角落 半躺轻捏衣摆",
        camera="third",
    )
    assert variation["pose_id"] == "soft_surface_recline_hem_hold"
    assert not variation["selection_notice"]


@pytest.mark.parametrize("look_id", HEM_LOOKS + ("roxy_cream", "original_ccd_sleepwear_overknees"))
@pytest.mark.parametrize("camera", CAMERAS)
def test_hem_pose_is_randomly_reachable_across_outfits(look_id, camera):
    pose_id = "soft_surface_recline_hem_hold"
    scene_id = "gray_rug_recline"
    profile = cos.cos_look_compatibility(look_id)
    variation = cos.pick_cos_variation(
        look_id,
        camera=camera,
        avoid_poses=[item for item in cos.COS_RANDOM_POSE_CLASSES if item != pose_id],
        avoid_scenes=[item for item in cos.COS_RANDOM_SCENE_CLASSES if item != scene_id],
    )

    assert pose_id in profile["recommended_pose_ids"]
    assert variation["pose_id"] == pose_id
    assert variation["scene_id"] == scene_id


def test_hem_pose_is_reachable_in_the_final_action_for_other_outfits():
    pose_id = "soft_surface_recline_hem_hold"
    action = cos.build_cos_look_action(
        force_id="roxy_cream",
        camera="third",
        avoid_poses=[item for item in cos.COS_RANDOM_POSE_CLASSES if item != pose_id],
        avoid_scenes=[item for item in cos.COS_RANDOM_SCENE_CLASSES if item != "gray_rug_recline"],
    )
    assert tag(action, "pose") == pose_id
    assert tag(action, "framing") == "recline_high_close"
    assert "若服装没有合适的自由下摆" in action


def test_pose_only_request_stays_on_compatible_surfaces_after_rotation_is_exhausted():
    variation = cos.pick_cos_variation(
        "roxy_cream", extra_request="半躺后撑", camera="third",
        avoid_scenes=list(cos.COS_RANDOM_SCENE_CLASSES),
    )
    assert variation["scene_id"] in SCENES
    assert variation["pose_id"] == "soft_surface_recline_support"


def test_conflicting_style_scene_keeps_its_environment():
    style = cos.COS_JSON_STYLES["water_digital_portrait"]["aliases"][0]
    variation = cos.pick_cos_variation(
        HEM_LOOKS[0], extra_request=f"{style} 半躺后撑", camera="third",
    )
    assert variation["scene_id"] == "blue_violet_poolside"
    assert variation["pose_id"] in COS_JSON_SCENES["blue_violet_poolside"]["pose_ids"]
    assert variation["pose_id"] not in RECLINE_POSES


@pytest.mark.parametrize("scene_id", [
    "daylight_car_backseat", "cool_washbasin", "stone_bathroom", "blue_violet_poolside",
])
@pytest.mark.parametrize("camera", CAMERAS)
@pytest.mark.parametrize("pose_id", sorted(RECLINE_POSES))
def test_conflicting_scene_is_preserved_and_rejected_pose_alias_is_removed(scene_id, camera, pose_id):
    request = f"{COS_JSON_SCENES[scene_id]['aliases'][0]} {cos.COS_POSE_REQUEST_ALIASES[pose_id][0]}"
    variation = cos.pick_cos_variation(HEM_LOOKS[0], extra_request=request, camera=camera)
    action = cos.build_cos_look_action(
        request, force_id=HEM_LOOKS[0], camera=camera,
        avoid_scenes=list(cos.COS_RANDOM_SCENE_CLASSES),
        avoid_poses=list(cos.COS_RANDOM_POSE_CLASSES),
        avoid_framings=list(cos.COS_FRAMING_CLASSES),
    )

    assert variation["scene_id"] == scene_id
    assert variation["pose_id"] not in RECLINE_POSES
    assert variation["selection_notice"]
    assert tag(action, "scene") == scene_id
    assert tag(action, "pose") in COS_JSON_SCENES[scene_id]["pose_ids"]
    assert tag(action, "pose") not in RECLINE_POSES
    assert "半躺后撑" not in action
    assert "半躺轻捏" not in action


def test_custom_scene_does_not_invent_a_soft_support_surface():
    request = "场景：自建摄影幕布 半躺后撑"
    variation = cos.pick_cos_variation("roxy_cream", extra_request=request, camera="third")
    action = cos.build_cos_look_action(request, force_id="roxy_cream", camera="third")

    assert variation["scene_id"] == "user"
    assert variation["pose_id"] not in RECLINE_POSES
    assert not COS_JSON_POSES[variation["pose_id"]].get("scene_ids")
    assert tag(action, "scene") == "user"
    assert tag(action, "pose") not in RECLINE_POSES
    assert "半躺后撑" not in action


@pytest.mark.parametrize("capture", ["手持手机自拍", "对镜自拍"])
def test_handheld_selfie_releases_the_two_hand_recline_pose(capture):
    request = f"卧室地毯 半躺后撑 {capture}"
    variation = cos.pick_cos_variation("roxy_cream", extra_request=request, camera="selfie")
    action = cos.build_cos_look_action(request, force_id="roxy_cream", camera="selfie")

    assert variation["scene_id"] == "bedroom_carpet"
    assert variation["pose_id"] not in RECLINE_POSES
    assert variation["selection_notice"]
    assert tag(action, "scene") == "bedroom_carpet"
    assert tag(action, "pose") not in RECLINE_POSES
    assert "【cam:selfie】" in action
    assert "由主角原有的一只手自然持唯一一部普通手机" in action
    assert "半躺后撑" not in action


@pytest.mark.parametrize("pose", ["", "半躺后撑", "半躺轻捏衣摆"])
def test_phone_in_new_scene_keeps_scene_without_adding_a_support_hand(pose):
    action = cos.build_cos_look_action(
        f"深灰床面 {pose} 手持手机自拍", force_id=HEM_LOOKS[0], camera="selfie",
    )
    assert tag(action, "scene") == "gray_bed_recline"
    assert tag(action, "pose") not in RECLINE_POSES
    assert "由主角原有的一只手自然持唯一一部普通手机" in action
    assert "固定或定时自拍" not in action
    assert "不添加手持手机" not in action


def test_third_person_phone_face_cover_does_not_keep_an_occupied_hem_hand():
    action = cos.build_cos_look_action(
        "卧室地毯 半躺轻捏衣摆 手机遮脸", force_id=HEM_LOOKS[0], camera="third",
        avoid_poses=list(cos.COS_RANDOM_POSE_CLASSES),
    )
    assert tag(action, "scene") == "bedroom_carpet"
    assert tag(action, "pose") not in RECLINE_POSES
    assert "握持唯一一部普通手机" in action
    assert "半躺轻捏" not in action


@pytest.mark.parametrize("negative", ["不要手机遮脸", "手机不要遮脸"])
def test_negated_phone_request_keeps_recline_pose(negative):
    action = cos.build_cos_look_action(
        f"深灰床面 半躺后撑 {negative}", force_id=HEM_LOOKS[0], camera="selfie",
    )
    assert tag(action, "pose") == "soft_surface_recline_support"
    assert "握持唯一一部普通手机" not in action


def test_sleep_scene_keeps_its_existing_pose_and_excludes_new_recline_poses():
    action = cos.build_cos_look_action(
        "卧室床铺 熟睡姿势",
        force_id="roxy_cream",
        camera="third",
    )
    assert tag(action, "pose") == "sleeping_on_bed"
    assert not (set(COS_JSON_SCENES["bedroom_sleep"]["pose_ids"]) & RECLINE_POSES)
    assert all("sleeping_on_bed" not in COS_JSON_SCENES[scene_id]["pose_ids"] for scene_id in SCENES)
