"""Regression checks on the assembled prompts without calling an image model."""

import asyncio
import random
import re
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from astrbot_plugin_selfie_image.cos.cos_looks import build_cos_look_action
from astrbot_plugin_selfie_image.cos.leg_focus import (
    LEGFOCUS_HOSIERY_STYLES,
    LEGFOCUS_FRAMING,
    LEGFOCUS_CAMERA_FRAMINGS,
    LEGFOCUS_REFERENCE_RULES,
    LEGFOCUS_SCENE_RULE,
    LEGFOCUS_SCENE_BY_ID,
    compatible_leg_compositions,
    LEGFOCUS_VIEW_BY_ID,
    LEGFOCUS_POSE_BY_ID,
    LegFocusDataError,
    build_leg_focus_action,
)
from astrbot_plugin_selfie_image.core.providers import ImageReference
from astrbot_plugin_selfie_image.core.models import AICatConfig
from astrbot_plugin_selfie_image.features.persona import PersonaManager
from astrbot_plugin_selfie_image.main import SelfieImagePlugin
from astrbot_plugin_selfie_image.prompts.prompt_templates import (
    build_selfie_builtin_prompt,
    extract_generated_action_contract,
    extract_user_prompt,
)
from astrbot_plugin_selfie_image.prompts.selfie_actions import (
    build_group_selfie_action,
    build_selfie_look_action,
    build_third_person_look_action,
)


def plugin_for_prompts(tmp_path, english=False):
    plugin = object.__new__(SelfieImagePlugin)
    plugin.persona = PersonaManager(tmp_path)
    plugin.config = SimpleNamespace(image_max_batch_count=10)
    plugin._ensure_daily_selfie_schedule = AsyncMock()
    plugin._persona_identity_reference = lambda **_kw: ImageReference(data=b"identity", mime_type="image/png")
    plugin._persona_auxiliary_references = lambda action="": []
    plugin._prompt_en_needed = lambda *_a, **_kw: english
    # Exercise the supported translation-failure fallback without a network LLM.
    plugin._translate_prompt_to_english = AsyncMock(side_effect=lambda text, **_kw: (text, {"applied": False}))
    return plugin


@pytest.mark.parametrize("english,translation_applied", [(False, False), (True, False), (True, True)])
def test_all_look_modes_keep_the_selected_contract_in_final_request(tmp_path, english, translation_applied):
    plugin = plugin_for_prompts(tmp_path, english)
    if translation_applied:
        plugin._translate_prompt_to_english = AsyncMock(
            side_effect=lambda text, **_kw: (text, {"applied": True})
        )
    random.seed(5)
    actions = [
        build_selfie_look_action("侧身"),
        build_selfie_look_action("穿黑丝"),
        build_selfie_look_action("测试角色 COS"),
        build_selfie_look_action("露腰", has_refs=True),
        build_third_person_look_action("背对镜头"),
        build_leg_focus_action("不要第三人称，第一人称 不要沙发 不要脚踝交叠 白色短袜"),
        build_group_selfie_action("三人合影，穿黑丝"),
        build_cos_look_action("侧身"),
    ]
    for action in actions:
        prompt, refs, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, []))
        contract = extract_generated_action_contract(action)
        assert prompt.count(contract) == 1
        assert len(refs) == 1
        supplement = re.search(r"(?:用户补充要求优先|用户补充要求|用户指定内容)[:：]", action)
        if supplement:
            assert extract_user_prompt(action) in prompt
            assert prompt.count("User request:") <= 1
        if "【legs:outfit】" not in action:
            assert "【legs:outfit】" not in prompt


@pytest.mark.parametrize("query", [
    "不要第三人称，第一人称", "非自拍", "notselfie", "不要沙发",
    "不要脚踝交叠", "不要袜子", "不穿鞋和袜子", "不要黑丝", "白色短袜", "白色堆堆袜",
    "双手撑床", "单手撑地", "第三人称 低机位平拍", "第三人称 斜侧拍",
    "晒腿", "主要看腿形", "看腿形",
])
def test_leg_constraints_survive_final_persona_and_reference_selection(tmp_path, query):
    plugin = plugin_for_prompts(tmp_path)
    for seed in range(12):
        random.seed(seed)
        action = build_leg_focus_action(query)
        prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, []))
        assert query in prompt
        assert prompt.count(f"用户补充要求优先：{query}。") == 1
        if query.startswith("不要第三人称"):
            assert "【cam:selfie】" in prompt
        if query in {"非自拍", "notselfie"}:
            assert "【cam:third】" in prompt
        if query == "不要沙发":
            assert "【leg_scene:sofa】" not in prompt
        if query == "不要脚踝交叠":
            assert "【pose:seated_ankle_cross】" not in prompt
        if query in {"不要袜子", "不穿鞋和袜子", "不要黑丝"}:
            assert "【wear:black】" not in prompt
        if query in {"不要袜子", "不穿鞋和袜子"}:
            assert "【wear:white】" not in prompt
        if query == "双手撑床":
            assert "【leg_scene:bed_surface】" in prompt
        if query == "单手撑地":
            assert re.search(r"【leg_scene:(?:bedroom_rug|living_room_rug)】", prompt)
        if query == "白色堆堆袜":
            assert not any(style["prompt"] in prompt for style in LEGFOCUS_HOSIERY_STYLES)
            assert "不额外随机其他袜装或赤足" in prompt
        if query == "第三人称 斜侧拍":
            view_id = re.search(r"【leg_view:([a-z_]+)】", prompt).group(1)
            assert LEGFOCUS_VIEW_BY_ID[view_id]["direction"] == "oblique"


@pytest.mark.parametrize("query", ["赤足", "白色短袜", "白色堆堆袜", "不要袜子", "不要鞋子"])
def test_leg_reference_detail_override_does_not_also_preserve_old_socks(tmp_path, query):
    plugin = plugin_for_prompts(tmp_path)
    action = build_leg_focus_action(query, has_refs=True)
    prompt, refs, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(
        None, action, [ImageReference(data=b"external", mime_type="image/png")]
    ))
    assert refs[0].data == b"external"
    assert "袜鞋与配饰，只调整姿势和构图" not in prompt
    assert "其余服饰沿用原图" in prompt
    assert prompt.count(query) >= 1


@pytest.mark.parametrize("query", ["第三人称 第一人称", "侧拍 正前方", "第一人称 全身"])
def test_incompatible_leg_requests_are_rejected_before_prompt_generation(query):
    with pytest.raises(LegFocusDataError):
        build_leg_focus_action(query)


@pytest.mark.parametrize("external_reference", [False, True])
@pytest.mark.parametrize("english,translation_applied", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("pose_id,view_id", [
    ("edge_knees_together_dangle", "third_front_low"),
    ("edge_staggered_dangle", "self_forward_down"),
    ("edge_one_leg_raised", "self_left_down"),
    ("edge_crossed_knee_dangle", "self_right_down"),
    ("edge_parallel_forward", "third_front_low"),
    ("seated_forward_leg", "third_surface_oblique_low"),
    ("edge_knees_together_ankles_apart", "third_front_low"),
    ("edge_single_foot_flex", "third_oblique_low"),
    ("edge_both_feet_flex", "third_front_low"),
    ("edge_feet_overlap", "third_oblique_low"),
    ("surface_extended_foot_flex", "third_ground_oblique_low"),
    ("surface_parallel_feet_overlap", "third_surface_side_low"),
    ("reclined_offset_legs", "self_forward_down"),
    ("supported_straight_legs", "third_side_level"),
    ("supported_straight_legs", "third_oblique_high"),
    ("seated_knees_together", "self_forward_down"),
    ("edge_knees_together_dangle", "self_left_down"),
    ("one_knee_side_support", "self_steep_down"),
    ("supine_offset_legs", "self_reclined_forward"),
    ("side_lying_offset_legs", "self_reclined_left"),
    ("kneeling_sit_back", "self_forward_down"),
    ("kneeling_upright", "third_oblique_low"),
])
def test_reference_leg_silhouettes_keep_their_camera_specific_crop_in_final_request(
    tmp_path, external_reference, english, translation_applied, pose_id, view_id,
):
    plugin = plugin_for_prompts(tmp_path, english)
    if translation_applied:
        plugin._translate_prompt_to_english = AsyncMock(
            side_effect=lambda text, **_kw: (text, {"applied": True})
        )
    action = build_leg_focus_action(
        has_refs=external_reference, force_pose=pose_id, force_view=view_id,
    )
    refs = [ImageReference(data=b"external", mime_type="image/png")] if external_reference else []
    prompt, selected_refs, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, refs))
    pose = LEGFOCUS_POSE_BY_ID[pose_id]
    camera = LEGFOCUS_VIEW_BY_ID[view_id]["camera"]
    assert prompt.count(pose["prompt"]) == 1
    assert prompt.count(LEGFOCUS_FRAMING) == 1
    assert prompt.count(LEGFOCUS_SCENE_RULE) == 1
    assert prompt.count(pose["crop_prompts"][camera]) == 1
    assert prompt.count(LEGFOCUS_CAMERA_FRAMINGS[camera]) == 1
    assert sum(prompt.count(rule) for rule in LEGFOCUS_REFERENCE_RULES.values()) == 1
    assert "严格遵循以上已锁定的看看腿动作合同" not in prompt
    for other_camera, crop in pose["crop_prompts"].items():
        if other_camera != camera:
            assert crop not in prompt
    if camera == "selfie":
        assert "第一人称腰线至足部照片" not in prompt
        assert "从胸下或腰线附近裁" not in prompt
    assert "脚端四周留出少量余量" not in prompt
    assert "腰头至双脚完整" not in prompt
    if "dangle" in pose_id or pose_id in {"edge_one_leg_raised", "edge_parallel_forward"}:
        assert "【leg_support:raised_edge】" in prompt
        assert "双脚落在地面" not in prompt
        assert "不贴近脚端" not in prompt
        assert "不贴近脚底" not in prompt
    if external_reference:
        assert selected_refs[0].data == b"external"
        assert "【outfit:reference】" in prompt


@pytest.mark.parametrize("pose_id", list(LEGFOCUS_POSE_BY_ID))
def test_every_leg_pose_view_reaches_final_request_without_duplicate_instructions(tmp_path, pose_id):
    plugin = plugin_for_prompts(tmp_path)
    pose = LEGFOCUS_POSE_BY_ID[pose_id]
    for view_id in pose["view_ids"]:
        action = build_leg_focus_action(has_refs=True, force_pose=pose_id, force_view=view_id)
        prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(
            None, action, [ImageReference(data=b"external", mime_type="image/png")]
        ))
        view = LEGFOCUS_VIEW_BY_ID[view_id]
        if pose_id in {
            "reclined_offset_legs", "edge_fold_and_drop", "edge_staggered_dangle",
            "edge_one_leg_raised", "edge_crossed_knee_dangle",
        }:
            assert view["camera"] == "selfie"
            assert "第三人称" not in prompt
        if pose_id in {
            "seated_diagonal_legs", "edge_fold_and_drop", "supported_bent_knees",
            "armrest_offset_legs", "seated_forward_leg", "backed_staggered_legs",
            "armrest_side_knees", "seated_crossed_knee", "one_knee_side_support",
            "reclined_one_knee", "edge_staggered_dangle", "edge_one_leg_raised",
            "edge_crossed_knee_dangle", "edge_parallel_forward",
            "supine_offset_legs", "side_lying_offset_legs",
        } and view["camera"] == "third":
            assert view["elevation"] == "up", (pose_id, view_id)
            assert "仰拍" in view["prompt"]
        scene_id = re.search(r"【leg_scene:([a-z_]+)】", action).group(1)
        support_id = re.search(r"【leg_support:([a-z_]+)】", action).group(1)
        scene = LEGFOCUS_SCENE_BY_ID[scene_id]
        if view["camera"] == "third" and pose_id in {
            "seated_diagonal_legs", "supported_bent_knees", "armrest_offset_legs",
            "seated_forward_leg", "backed_staggered_legs", "seated_crossed_knee",
            "one_knee_side_support", "surface_extended_foot_flex", "surface_parallel_feet_overlap",
        }:
            assert support_id in {"raised_soft_surface", "ground_surface"}
            assert scene_id in {
                "bed_surface", "sofa", "floor_cushion", "bedroom_rug", "living_room_rug", "floor_window",
            }
            assert "脚掌自然落地" not in prompt
            assert "脚掌稳放地面" not in prompt
        for selected in (
            action, LEGFOCUS_FRAMING, LEGFOCUS_CAMERA_FRAMINGS[view["camera"]],
            pose["crop_prompts"][view["camera"]], view["prompt"], pose["prompt"],
            LEGFOCUS_SCENE_RULE, scene["prompt"], scene["supports"][support_id],
        ):
            assert prompt.count(selected) == 1, (pose_id, view_id, selected)
        sentences = [part.strip() for part in re.split(r"[。\n]", prompt) if part.strip()]
        duplicates = [part for part, count in Counter(sentences).items() if count > 1]
        assert not duplicates, (pose_id, view_id, duplicates)
        assert "约与坐姿膝部同高" not in prompt
        assert "腰头至双脚完整" not in prompt
        assert "第一人称腰线至" not in prompt


@pytest.mark.parametrize("pose_id", [
    "seated_diagonal_legs", "armrest_offset_legs", "seated_forward_leg",
    "backed_staggered_legs", "seated_crossed_knee", "supported_bent_knees",
    "one_knee_side_support",
])
def test_third_person_crossed_legs_keep_both_legs_on_a_single_surface(tmp_path, pose_id):
    plugin = plugin_for_prompts(tmp_path)
    for scene_query, scene_ids, support_id, view_id in (
        ("床上", {"bed_surface"}, "raised_soft_surface", "third_surface_oblique_low"),
        ("地毯", {"bedroom_rug", "living_room_rug"}, "ground_surface", "third_ground_side_low"),
        ("沙发", {"sofa"}, "raised_soft_surface", "third_surface_side_low"),
    ):
        action = build_leg_focus_action(
            f"第三人称 {scene_query} 仰拍", has_refs=True, force_pose=pose_id, force_view=view_id,
        )
        prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(
            None, action, [ImageReference(data=b"external", mime_type="image/png")]
        ))
        scene_id = re.search(r"【leg_scene:([a-z_]+)】", prompt).group(1)
        assert scene_id in scene_ids
        assert f"【leg_support:{support_id}】" in prompt
        assert prompt.count(LEGFOCUS_POSE_BY_ID[pose_id]["prompt"]) == 1
        support_prompt = LEGFOCUS_SCENE_BY_ID[scene_id]["supports"][support_id]
        if support_id == "ground_surface":
            assert "同一地面" in support_prompt
            assert "承托臀部与脚部" in support_prompt
        else:
            assert "臀部与双腿" in support_prompt
            assert re.search(r"不越过(?:床沿|座沿)悬垂", support_prompt)
        assert "脚掌落地" not in prompt
        assert "脚掌稳放地面" not in prompt
        assert "越过座沿自然垂下" not in prompt
        assert "【outfit:reference】" in prompt
    for scene_query in ("床沿", "电竞椅", "书桌椅", "卧室软凳", "客厅单椅"):
        for angle in ("", "仰拍", "俯拍", "侧拍", "平拍"):
            with pytest.raises(LegFocusDataError, match="不兼容"):
                build_leg_focus_action(f"第三人称 {scene_query} {angle}", force_pose=pose_id)


@pytest.mark.parametrize("query,pose_id", [
    ("小腿倒V", "edge_knees_together_ankles_apart"),
    ("倒V单脚轻勾", "edge_ankles_apart_single_flex"),
    ("倒V双脚轻勾", "edge_ankles_apart_both_flex"),
    ("单脚轻勾", "edge_single_foot_flex"),
    ("双脚轻勾", "edge_both_feet_flex"),
    ("脚掌相叠", "edge_feet_overlap"),
])
@pytest.mark.parametrize("external_reference", [False, True])
def test_dangling_foot_variants_keep_their_pose_camera_and_clothing(tmp_path, query, pose_id, external_reference):
    plugin = plugin_for_prompts(tmp_path)
    action = build_leg_focus_action(
        f"第三人称 床沿 {query}", has_refs=external_reference, force_view="third_oblique_low",
    )
    refs = [ImageReference(data=b"external", mime_type="image/png")] if external_reference else []
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, refs))
    assert f"【pose:{pose_id}】" in prompt
    assert "【leg_support:raised_edge】" in prompt
    assert "【cam:third】" in prompt
    assert prompt.count(LEGFOCUS_POSE_BY_ID[pose_id]["prompt"]) == 1
    assert "双脚离地" in prompt
    assert "脚掌落地" not in prompt
    assert "必须赤足" not in prompt
    if "flex" in pose_id or "overlap" in pose_id:
        assert "脚尖自然下垂" not in prompt
    if external_reference:
        assert "保留原有上装、下装、连体结构、袜鞋与配饰" in prompt
    if pose_id == "edge_feet_overlap":
        assert "膝部与小腿不交叉" in prompt
    if "ankles_apart" in pose_id:
        assert "脚踝间距略大于膝间距" in prompt
        with pytest.raises(LegFocusDataError, match="不兼容"):
            build_leg_focus_action("第三人称 侧拍", force_pose=pose_id)


@pytest.mark.parametrize("pose_id", ["supported_bent_knees", "one_knee_side_support"])
@pytest.mark.parametrize("query,view_id,support_id", [
    ("第一人称 地毯", "self_steep_down", "wide_surface"),
    ("第三人称 床上 仰拍", "third_surface_oblique_low", "raised_soft_surface"),
    ("第三人称 沙发 侧拍 仰拍", "third_surface_side_low", "raised_soft_surface"),
    ("第三人称 地毯 仰拍", "third_ground_oblique_low", "ground_surface"),
    ("第三人称 地毯 侧拍 仰拍", "third_ground_side_low", "ground_surface"),
])
def test_bent_leg_final_camera_keeps_a_compatible_support(tmp_path, pose_id, query, view_id, support_id):
    plugin = plugin_for_prompts(tmp_path)
    action = build_leg_focus_action(query, has_refs=True, force_pose=pose_id, force_view=view_id)
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(
        None, action, [ImageReference(data=b"external", mime_type="image/png")]
    ))
    assert f"【leg_support:{support_id}】" in prompt
    assert prompt.count(LEGFOCUS_VIEW_BY_ID[view_id]["prompt"]) == 1
    if support_id == "raised_soft_surface":
        assert "高于地面" in prompt
        assert "略高于软面边缘" in prompt
        assert "地毯" not in prompt
    elif support_id == "ground_surface":
        assert re.search(r"【leg_scene:(?:bedroom_rug|living_room_rug)】", prompt)
        assert "高于地垫" in prompt
        assert "第三人称" in prompt
    else:
        assert re.search(r"【leg_scene:(?:bedroom_rug|living_room_rug)】", prompt)
        assert "第三人称" not in prompt


@pytest.mark.parametrize("external_reference", [False, True])
@pytest.mark.parametrize("query,pose_id,view_id,support_id", [
    ("落地镜", "seated_knees_together", "self_forward_down", "seat_edge"),
    ("穿衣镜", "supported_straight_legs", "self_left_down", "long_surface"),
    ("全身镜", "one_knee_side_support", "self_right_down", "wide_surface"),
    ("镜前", "kneeling_sit_back", "self_steep_down", "kneeling_surface"),
])
def test_floor_mirror_scene_keeps_first_person_crop_in_final_prompt(
    tmp_path, external_reference, query, pose_id, view_id, support_id,
):
    plugin = plugin_for_prompts(tmp_path)
    action = build_leg_focus_action(
        query, has_refs=external_reference, force_pose=pose_id, force_view=view_id,
    )
    refs = [ImageReference(data=b"external", mime_type="image/png")] if external_reference else []
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, refs))
    assert "【leg_scene:floor_mirror】" in prompt
    assert "【cam:selfie】" in prompt
    assert f"【leg_support:{support_id}】" in prompt
    assert prompt.count(LEGFOCUS_CAMERA_FRAMINGS["selfie"]) == 1
    assert prompt.count(LEGFOCUS_POSE_BY_ID[pose_id]["crop_prompts"]["selfie"]) == 1
    assert prompt.count(LEGFOCUS_SCENE_BY_ID["floor_mirror"]["prompt"]) == 1
    assert "第三人称" not in prompt
    assert "头部到足部完整入镜" not in prompt
    if external_reference:
        assert "【outfit:reference】" in prompt


def test_floor_mirror_is_excluded_from_all_third_person_selections():
    for pose in LEGFOCUS_POSE_BY_ID.values():
        combinations = compatible_leg_compositions(pose, scene_ids="floor_mirror")
        assert all(view["camera"] == "selfie" for _, _, view in combinations)
        assert not compatible_leg_compositions(pose, camera="third", scene_ids="floor_mirror")
    for query, options in (
        ("第三人称 落地镜", {}),
        ("落地镜", {"force_camera": "third"}),
        ("落地镜", {"force_view": "third_front_low"}),
        ("落地镜 全身", {}),
    ):
        with pytest.raises(LegFocusDataError, match="不兼容"):
            build_leg_focus_action(query, **options)


def test_full_body_request_is_still_distinct_from_full_length_mirror():
    action = build_leg_focus_action("不要全身镜 全身")
    assert "【frame:full_outfit】" in action
    assert "【cam:third】" in action
    assert "【leg_scene:floor_mirror】" not in action


def test_elevated_seats_keep_external_low_views_and_natural_self_view():
    seat_supports = {"seat_edge", "raised_edge", "wide_edge", "armrest", "backrest"}
    raised_views = set()
    for pose in LEGFOCUS_POSE_BY_ID.values():
        for _, support, view in compatible_leg_compositions(pose):
            if support not in seat_supports:
                continue
            if view["camera"] == "third":
                assert view["elevation"] in {"level", "up"}, (pose["id"], support, view["id"])
                if support == "raised_edge":
                    assert view["elevation"] == "up", (pose["id"], view["id"])
                    raised_views.add(view["direction"])
            else:
                assert view["elevation"] == "down"
    assert raised_views == {"front", "oblique", "side"}


@pytest.mark.parametrize("query,pose_id,view_id,scene_id", [
    ("第一人称 床上 仰躺", "supine_offset_legs", "self_reclined_forward", "bed_surface"),
    ("第一人称 床上 侧躺", "side_lying_offset_legs", "self_reclined_right", "bed_surface"),
    ("第三人称 床上 侧躺 侧拍 仰拍", "side_lying_offset_legs", "third_surface_side_low", "bed_surface"),
    ("第一人称 落地窗前 跪坐", "kneeling_sit_back", "self_forward_down", "floor_window"),
    ("第三人称 落地窗前 直身跪姿 仰拍", "kneeling_upright", "third_oblique_low", "floor_window"),
])
def test_lying_and_kneeling_keep_support_projection_and_crop_in_final_prompt(
    tmp_path, query, pose_id, view_id, scene_id,
):
    plugin = plugin_for_prompts(tmp_path)
    action = build_leg_focus_action(query, has_refs=True, force_view=view_id)
    prompt, refs, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(
        None, action, [ImageReference(data=b"external", mime_type="image/png")]
    ))
    assert refs[0].data == b"external"
    assert f"【pose:{pose_id}】" in prompt
    assert f"【leg_scene:{scene_id}】" in prompt
    assert prompt.count(LEGFOCUS_POSE_BY_ID[pose_id]["prompt"]) == 1
    assert prompt.count(LEGFOCUS_VIEW_BY_ID[view_id]["prompt"]) == 1
    assert prompt.count(f"用户补充要求优先：{query}。") == 1
    assert "【outfit:reference】" in prompt
    if view_id.startswith("self_reclined_"):
        assert "【leg_support:lying_surface】" in prompt
        assert "机位随卧姿放低" in prompt
        assert "低头" not in prompt
        assert "俯拍" not in prompt
        assert "第三人称" not in prompt
        assert "侧拍" not in prompt
    if pose_id.startswith("kneeling_"):
        assert "【leg_support:kneeling_surface】" in prompt
    if pose_id == "kneeling_sit_back":
        assert "小腿与脚处在身后画外" in prompt


@pytest.mark.parametrize("pose_id", [
    "edge_fold_and_drop", "supported_bent_knees", "side_folded_legs",
    "reclined_one_knee", "edge_one_leg_raised", "edge_parallel_forward",
])
def test_folded_and_forward_legs_do_not_randomly_use_head_on_level_camera(pose_id):
    with pytest.raises(LegFocusDataError, match="不兼容"):
        build_leg_focus_action(force_pose=pose_id, force_view="third_front_level")


def test_random_third_person_selection_keeps_front_views_and_side_variation():
    counts = Counter()
    with patch("astrbot_plugin_selfie_image.cos.leg_focus.random", random.Random(20261108)):
        for _ in range(400):
            action = build_leg_focus_action("第三人称")
            view_id = re.search(r"【leg_view:([a-z_]+)】", action).group(1)
            counts[LEGFOCUS_VIEW_BY_ID[view_id]["direction"]] += 1
    assert 60 <= counts["front"] <= 200, counts
    assert counts["oblique"] > counts["front"], counts
    assert counts["side"] > 5, counts


def test_compact_first_person_crop_does_not_restore_a_waist_to_feet_header(tmp_path):
    plugin = plugin_for_prompts(tmp_path)
    action = build_leg_focus_action(force_pose="seated_knees_together", force_view="self_forward_down")
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, []))
    assert prompt.count("第一人称主观视角") == 1
    assert "小腿和脚留在画外" in prompt
    assert "腰线至足部照片" not in prompt
    assert "双脚完整入镜" not in prompt


def test_group_leg_description_is_not_routed_into_leg_mode(tmp_path):
    plugin = plugin_for_prompts(tmp_path)
    random.seed(5)
    action = build_group_selfie_action("多人合影 黑丝")
    intent = plugin.persona.analyze_selfie_intent(action)
    assert intent.is_group_photo and not intent.is_legs_only
    for language in ("zh", "en"):
        prompt = build_selfie_builtin_prompt(action, language=language)
        assert "【legs:outfit】" not in prompt


def test_close_selfie_has_one_framing_and_crop_waist_keeps_reference_context(tmp_path):
    plugin = plugin_for_prompts(tmp_path)
    with patch("astrbot_plugin_selfie_image.prompts.selfie_actions.random.choices", return_value=["close_portrait"]):
        action = build_selfie_look_action()
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, []))
    assert "近景胸像" in prompt
    assert "画面至少到腰线" not in prompt
    cropped = build_selfie_look_action("露腰", has_refs=True)
    assert "不另加沙发或床" in cropped
    assert "放松地坐着或靠在深色沙发/床边" not in cropped


def test_reference_description_uses_locked_contract_scope(tmp_path):
    plugin = plugin_for_prompts(tmp_path)
    plugin.config = SimpleNamespace(image_ocr_model="")
    plugin._call_text_llm = AsyncMock(return_value="相同人物，长发。")
    contract = build_leg_focus_action("赤足", has_refs=True)
    _, meta = asyncio.run(plugin._describe_reference_images_for_generation(
        object(), [b"external"], prompt_contract=contract
    ))
    instruction = plugin._call_text_llm.call_args.args[1]
    assert contract in instruction
    assert "不要描述原图的姿势" in instruction
    assert "未被本次要求替换的衣物" in instruction
    assert meta["scope"] == "contract_identity"


def test_batch_keeps_user_supplement_without_locking_random_legwear(tmp_path):
    plugin = plugin_for_prompts(tmp_path)
    plugin._run_image_generation = AsyncMock(return_value={"success": True})

    async def run_shots(**kwargs):
        return [await kwargs["run_one"](index) for index in range(kwargs["total"])]

    plugin._run_counted_generation_shots = run_shots
    for source, action in (
        ("command-look-legs", build_leg_focus_action()),
        ("command-group-selfie", build_group_selfie_action("三人合影，手持同一束花")),
    ):
        with patch.object(plugin, "_build_leg_focus_action", wraps=plugin._build_leg_focus_action) as build_legs:
            asyncio.run(plugin._run_selfie_batches_unlocked(
                "", None, action, [], source, 2, "9:16", "1K", "失败"
            ))
            if source == "command-look-legs":
                assert all(not call.kwargs["force_legwear"] for call in build_legs.call_args_list)
            else:
                for call in plugin._run_image_generation.call_args_list[-2:]:
                    assert "三人合影，手持同一束花" in call.args[0]


@pytest.mark.parametrize("namespace", ["studio", "creative"])
@pytest.mark.parametrize("english,translation_applied", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("mode,query", [
    ("group", "三人侧身合影 黑丝"),
    ("selfie", "背对镜头"),
    ("selfie", "看看腿 第一人称 白色短袜"),
])
def test_canvas_final_requests_use_the_same_mode_contracts(tmp_path, namespace, english, translation_applied, mode, query):
    plugin = plugin_for_prompts(tmp_path, english)
    if translation_applied:
        plugin._translate_prompt_to_english = AsyncMock(side_effect=lambda text, **_kw: (text, {"applied": True}))
    plugin.config = AICatConfig.from_dict({})
    plugin.persona.set_appearance_type("anime")
    plugin.persona.ensure_daily_selfie_profile = AsyncMock()
    session = {
        "id": "canvas", "template": "group" if mode == "group" else "selfie",
        "graph": {"mode": mode, "prompt": query, "count": 1},
        "slots": [{"id": "ref", "image_path": "reference.png"}],
    }
    store = SimpleNamespace(get=lambda _: session, attach_run_finish=lambda *_a, **_kw: None)
    plugin._canvas_store = lambda selected: store if selected == namespace else None
    plugin._resolve_studio_refs = lambda *_a, **_kw: ([(b"external", "image/png")], ["ref"])
    plugin._task_cancel_requested = lambda _: False
    plugin._web_task_timestamp = lambda: "now"
    state = {}
    plugin._set_web_image_task = lambda _, **fields: state.update(fields)
    plugin.get_web_image_task = lambda _: state
    plugin._wait_for_record_commits = AsyncMock()
    plugin._record_task = lambda _: None
    plugin._run_image_generation = AsyncMock(return_value={"success": True, "image_paths": ["generated.png"]})
    asyncio.run(plugin._run_studio_task("task", "canvas", canvas_namespace=namespace))
    assert state["status"] == "succeeded", state.get("error")
    request = plugin._run_image_generation.call_args.kwargs
    action = request["original_prompt"]
    assert request["prompt"].count(extract_generated_action_contract(action)) == 1
    if mode == "group":
        assert plugin.persona.analyze_selfie_intent(action).is_group_photo
        assert "【legs:outfit】" not in request["prompt"]
        assert "柔光京阿尼" in request["prompt"]
    elif "看看腿" in query:
        assert LEGFOCUS_CAMERA_FRAMINGS["selfie"] in request["prompt"]
        assert "白色短袜" in request["prompt"]
    else:
        assert "主角背对镜头" in request["prompt"]


def test_llm_group_contract_is_not_wrapped_again(tmp_path):
    plugin = plugin_for_prompts(tmp_path)
    action = build_group_selfie_action("背对镜头合影")
    prepared, _, _ = plugin._prepare_llm_selfie_action(action, False)
    assert prepared == action


@pytest.mark.parametrize("builder", [build_selfie_look_action, build_third_person_look_action, build_group_selfie_action])
def test_explicit_gaze_is_not_overridden_by_camera_defaults(tmp_path, builder):
    plugin = plugin_for_prompts(tmp_path)
    for seed in range(12):
        random.seed(seed)
        action = builder("不看镜头，目光看向侧边")
        prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, []))
        assert "不看镜头，目光看向侧边" in prompt
        for forced in ("必须看向镜头", "眼神对焦镜头", "优先与镜头有眼神交流", "正对镜头时自然看向镜头", "对镜头轻轻点头笑"):
            assert forced not in prompt


def test_quick_draw_variants_are_composed_before_sending(tmp_path):
    plugin = plugin_for_prompts(tmp_path)
    plugin.config = AICatConfig.from_dict({})
    plugin._validate_web_test_selection = lambda _: None
    plugin._find_web_retry_target = lambda *_a, **_kw: SimpleNamespace(label="test")
    plugin._record_task = lambda _: None
    plugin._run_image_generation = AsyncMock(return_value={"success": True, "image_paths": ["generated.png"]})
    plugin.build_creative_variations = lambda *_a, **_kw: [
        {"prompt": "自然自拍，窗边"}, {"prompt": "自然自拍，书桌边"},
    ]
    result = asyncio.run(plugin.web_test_image({
        "prompt": "自然自拍", "use_selfie_reference": True,
        "variation_enabled": True, "count": 2,
    }))
    assert result["success"], result.get("error")
    for call, scene in zip(plugin._run_image_generation.call_args_list, ("窗边", "书桌边")):
        assert scene in call.kwargs["prompt"]
        assert "参考图一是主角身份锚点" in call.kwargs["prompt"]
        assert call.kwargs["refs"][0].data == b"identity"


@pytest.mark.parametrize("refs", [[], [ImageReference(data=b"external", mime_type="image/png")]])
def test_plain_drawing_requests_preserve_the_user_prompt(tmp_path, refs):
    plugin = plugin_for_prompts(tmp_path)
    plugin._run_image_generation = AsyncMock(return_value={"success": True})
    raw = "正面站姿，摄影者留在画外；只按参考服装，不额外换装。"
    asyncio.run(plugin._draw_passthrough_once(None, raw, "9:16", "1K", refs, "command-draw"))
    call = plugin._run_image_generation.call_args
    assert call.args[0] == raw
    assert call.args[3] == refs
