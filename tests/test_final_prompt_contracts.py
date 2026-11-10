"""Regression checks on the assembled prompts without calling an image model."""

import asyncio
import random
import re
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from astrbot_plugin_selfie_image.cos.cos_looks import (
    COS_FRAMING_CLASSES,
    COS_RANDOM_POSE_CLASSES,
    build_cos_look_action,
    get_cos_look_by_id,
)
from astrbot_plugin_selfie_image.cos.leg_focus import (
    LEGFOCUS_HOSIERY_STYLES,
    LEGFOCUS_OUTFIT_POOL,
    LEGFOCUS_POOL_REFERENCE_PROMPTS,
    LEGFOCUS_ANATOMY_RULE,
    LEGFOCUS_APPEARANCE_PROMPTS,
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
    COS_HAIR_OVERRIDE_CONTRACT_EN,
    COS_HAIR_OVERRIDE_CONTRACT_ZH,
    build_selfie_builtin_prompt,
    extract_generated_action_contract,
    extract_user_prompt,
)
from astrbot_plugin_selfie_image.prompts.command_parser import parse_prompt_options
from astrbot_plugin_selfie_image.prompts.selfie_actions import (
    build_group_selfie_action,
    build_selfie_look_action,
    build_third_person_look_action,
)


COMPLEX_THIRD_POSES = {
    "kneeling_upright",
    "bed_supine_knees_together",
}

DIRECT_SELF_SEATED_POSES = {"seated_thigh_gentle_pinch", "seated_self_knees_offset"}


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


@pytest.mark.parametrize("english,translation_applied", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("pose_id,query,forbidden_views", [
    ("half_turn", "灯会 自然侧身", {"two_thirds", "side_profile", "rear_three_quarter"}),
    ("side_profile", "摄影棚 纯侧身", {"three_quarter_front", "two_thirds", "rear_three_quarter"}),
    ("wall_lean_curve", "摄影棚 扶墙侧身", {"three_quarter_front", "two_thirds", "side_profile"}),
    ("rear_edge_curve", "摄影棚 背身侧边前倾", {"three_quarter_front", "two_thirds", "side_profile"}),
    ("rear_hip_shift", "摄影棚 背身偏侧展示身材", {"three_quarter_front", "two_thirds", "side_profile"}),
])
def test_cos_direction_families_do_not_mix_in_final_request(
    tmp_path, english, translation_applied, pose_id, query, forbidden_views,
):
    plugin = plugin_for_prompts(tmp_path, english)
    if translation_applied:
        plugin._translate_prompt_to_english = AsyncMock(
            side_effect=lambda text, **_kw: (text, {"applied": True})
        )

    def prefer_conflicting_view(values):
        for view_id in ("side_profile", "two_thirds", "three_quarter_front", "rear_three_quarter"):
            if view_id in values:
                return view_id
        return values[0]

    with patch("astrbot_plugin_selfie_image.cos.cos_looks.random.choice", side_effect=prefer_conflicting_view):
        action = build_cos_look_action(
            query, force_id="ancient_white_blue_backless_gauze", camera="third",
        )
    prompt, refs, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, []))

    assert len(refs) == 1
    assert f"【cos_pose:{pose_id}】" in prompt
    framing_id = re.search(r"【cos_framing:([a-z_]+)】", prompt).group(1)
    assert framing_id not in forbidden_views
    assert prompt.count(COS_RANDOM_POSE_CLASSES[pose_id]["prompt"]) == 1
    assert prompt.count(COS_FRAMING_CLASSES[framing_id]["prompt"]) == 1
    for view_id in forbidden_views:
        assert COS_FRAMING_CLASSES[view_id]["prompt"] not in prompt
    assert "侧面或侧后方拍摄" not in prompt
    if pose_id == "half_turn":
        assert "胸口斜向镜头" in prompt
        assert "头部与胸口同向" in prompt
    elif pose_id == "side_profile":
        assert "仅呈现自然可见的一侧五官" in prompt


@pytest.mark.parametrize("english,translation_applied", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("external_reference", [False, True])
@pytest.mark.parametrize("look_id,forbidden", [
    ("yao_deer_theme", ("浅金或浅粉色",)),
    ("hok_huamulan_moqi_jiaofeng", ()),
    ("jinhsi_dragon_lord", ()),
    ("rem_blue_lolita", ()),
    ("ancient_mint_floral_short_ruqun", ("后垂黑色长发",)),
    ("ancient_pink_blue_floral_collar", ("深色长发", "低髻或发髻")),
    ("ancient_blue_floral_halter_doudou", ("低髻或丸子发髻",)),
    ("xishi_xuxiangsi", ("粉白或淡粉色", "发髻或团子", "白色或粉色辫子")),
    ("shaosiyuan_red_gold_sleeves", ("长发或双马尾",)),
    ("xiao_qiao_white_bear", ("长卷或中长假发",)),
    ("xiaoqiao_line_dog", ("青蓝或湖蓝色", "熊耳或狗耳",)),
    ("shorekeeper_stellar", ("短长发", "短长裙")),
    ("hok_diaochan_fumeng_fanhua", ("双人画面", "另一人物", "绿色长发")),
    ("hok_wangzhaojun_changye_huansheng", ("假发与头部固定饰件：星夜纹上衣",)),
    ("douluo3_ya_li_healer", ("浅色长发假发",)),
    ("douluo2_zhang_lexuan_moon", ("深色长发假发",)),
    ("wuthering_lumi_polar", ("浅色短假发",)),
    ("roxy_cream", ("可搭浅薰衣草",)),
    ("white_hair_black_bow_apron", ()),
    ("camellya_red_rose", ()),
    ("wuthering_roccia_stage", ()),
])
def test_cos_hair_rules_cover_costumes_and_reference_sources_in_final_request(
    tmp_path, english, translation_applied, external_reference, look_id, forbidden,
):
    plugin = plugin_for_prompts(tmp_path, english)
    if translation_applied:
        plugin._translate_prompt_to_english = AsyncMock(
            side_effect=lambda text, **_kw: (text, {"applied": True})
        )
    action = build_cos_look_action(
        "摄影棚 自然站姿", force_id=look_id, camera="third", has_refs=external_reference,
    )
    references = [ImageReference(data=b"external", mime_type="image/png")] if external_reference else []
    prompt, refs, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, references))

    assert len(refs) == 1
    assert refs[0].data == (b"external" if external_reference else b"identity")
    outfit_body = get_cos_look_by_id(look_id)["prompt"].split("不要额外人物", 1)[0]
    assert prompt.count(outfit_body) == 1
    for fragment in forbidden:
        assert fragment not in prompt
    if look_id == "yao_deer_theme":
        assert "整顶浅粉色双辫假发" in prompt
        assert "发际线处发丝、短齐刘海、两侧鬓发与粗辫均为同一浅粉色" in prompt
    elif look_id in {"hok_huamulan_moqi_jiaofeng", "jinhsi_dragon_lord", "rem_blue_lolita"}:
        assert "假发" not in outfit_body
    elif look_id == "white_hair_black_bow_apron":
        assert "刘海中央一缕深色挑染" in prompt
    elif look_id == "camellya_red_rose":
        assert "黑色挑染和红色发尾" in prompt
    elif look_id == "wuthering_roccia_stage":
        assert "蓝紫渐变长发" in prompt
        assert "两侧卷发带粉色挑染" in prompt
    elif look_id == "wuthering_lumi_polar":
        assert "局部发束带蓝绿色挑染" in prompt
    assert "发型发色来自参考图一" not in prompt
    hair_contract = COS_HAIR_OVERRIDE_CONTRACT_EN if english else COS_HAIR_OVERRIDE_CONTRACT_ZH
    assert prompt.count(hair_contract) == 1


@pytest.mark.parametrize("query", [
    "不要第三人称，第一人称", "非自拍", "notselfie", "不要沙发",
    "不要脚踝交叠", "不要袜子", "不穿鞋和袜子", "不要黑丝", "白色短袜", "白色堆堆袜",
    "双手撑床", "单手撑地", "第三人称 低机位仰拍", "第三人称 斜侧拍",
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


@pytest.mark.parametrize("english", [False, True])
def test_random_leg_outfit_replaces_reference_clothing_but_reference_outfit_does_not(
    tmp_path, english,
):
    outfit = next(item for item in LEGFOCUS_OUTFIT_POOL if item["id"] == "lace_blouse_skirt")
    plugin = plugin_for_prompts(tmp_path, english)
    random_action = build_leg_focus_action(has_refs=False, force_outfit=outfit["id"])
    assert f"【outfit:{outfit['id']}】" in random_action

    random_prompt, refs, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(
        None, random_action, []
    ))
    assert refs[0].data == b"identity"
    assert outfit["prompt"] in random_prompt
    assert LEGFOCUS_POOL_REFERENCE_PROMPTS["en" if english else "zh"] in random_prompt

    reference_action = build_leg_focus_action(has_refs=True)
    assert "【outfit:reference】" in reference_action
    reference_prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(
        None, reference_action, []
    ))
    assert "保留原有上装、下装、连体结构、袜鞋与配饰。" in reference_prompt
    assert LEGFOCUS_POOL_REFERENCE_PROMPTS["zh"] not in reference_prompt


@pytest.mark.parametrize("english", [False, True])
@pytest.mark.parametrize("pose_id,outfit_id,scene_id,support_id", [
    ("edge_single_foot_flex", "blouse_fitted_mini", "bed_edge", "raised_edge"),
    ("seated_knees_offset", "tweed_short_skirt_set", "gaming_chair", "seat_edge"),
])
def test_leg_skirt_camera_and_coverage_survive_final_prompt_assembly(
    tmp_path, english, pose_id, outfit_id, scene_id, support_id,
):
    from astrbot_plugin_selfie_image.cos.leg_focus import (
        LEGFOCUS_OUTFIT_CAPTURE_PROFILES, get_leg_focus_outfit, has_leg_focus_contract,
    )

    outfit = get_leg_focus_outfit(outfit_id)
    assert outfit["capture_profile"] == "skirt"
    action = build_leg_focus_action(
        force_pose=pose_id, force_outfit=outfit_id, preferred_scene=scene_id,
    )
    plugin = plugin_for_prompts(tmp_path, english)
    prompt, refs, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, []))
    view_id = re.search(r"【leg_view:([a-z_]+)】", prompt).group(1)
    view = LEGFOCUS_VIEW_BY_ID[view_id]
    assert view["camera"] == "third"
    assert view["direction"] in {"oblique", "side"}
    assert view["elevation"] == "up"
    assert f"【leg_scene:{scene_id}】" in prompt
    assert f"【leg_support:{support_id}】" in prompt
    assert f"【outfit:{outfit_id}】" in prompt
    assert has_leg_focus_contract(prompt)
    assert refs[0].data == b"identity"
    assert prompt.count(outfit["prompt"]) == 1
    assert prompt.count(LEGFOCUS_OUTFIT_CAPTURE_PROFILES["skirt"]["coverage_prompt"]) == 1
    assert prompt.count(LEGFOCUS_POSE_BY_ID[pose_id]["prompt"]) == 1
    assert prompt.count(view["prompt"]) == 1
    assert sum(item["prompt"] in prompt for item in LEGFOCUS_OUTFIT_POOL) == 1
    assert LEGFOCUS_VIEW_BY_ID["third_front_low"]["prompt"] not in prompt
    assert "俯拍" not in prompt
    for language in ("zh", "en"):
        builtin = build_selfie_builtin_prompt(action, language=language, has_reference_image=True)
        assert builtin.count(outfit["prompt"]) == 1
        assert builtin.count(view["prompt"]) == 1
        assert builtin.count(LEGFOCUS_OUTFIT_CAPTURE_PROFILES["skirt"]["coverage_prompt"]) == 1


@pytest.mark.parametrize("english", [False, True])
def test_leg_legacy_front_skirt_retry_preserves_clothes_and_changes_camera(tmp_path, english):
    from astrbot_plugin_selfie_image.cos.leg_focus import (
        LEGFOCUS_OUTFIT_CAPTURE_PROFILES, get_leg_focus_outfit, has_leg_focus_contract,
    )

    outfit_id = "blouse_fitted_mini"
    outfit = get_leg_focus_outfit(outfit_id)
    legacy = (
        LEGFOCUS_VIEW_BY_ID["third_front_low"]["prompt"] + outfit["prompt"]
        + " 【legs:outfit】 【frame:leg_detail】 【pose:edge_single_foot_flex】"
        " 【leg_view:third_front_low】 【leg_scene:bed_edge】 【leg_support:raised_edge】"
        f" 【cam:third】 【wear:white】 【outfit:{outfit_id}】"
    )
    assert not has_leg_focus_contract(legacy)
    plugin = plugin_for_prompts(tmp_path, english)
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, legacy, []))
    assert has_leg_focus_contract(prompt)
    assert "【pose:edge_single_foot_flex】" in prompt
    assert "【leg_scene:bed_edge】" in prompt
    assert "【outfit:blouse_fitted_mini】" in prompt
    assert "【leg_view:third_oblique_low】" in prompt
    assert "【wear:white】" in prompt
    assert LEGFOCUS_VIEW_BY_ID["third_front_low"]["prompt"] not in prompt
    assert prompt.count(outfit["prompt"]) == 1
    assert prompt.count(LEGFOCUS_OUTFIT_CAPTURE_PROFILES["skirt"]["coverage_prompt"]) == 1
    assert prompt.count(LEGFOCUS_FRAMING) == 1


@pytest.mark.parametrize("query", ["第三人称 第一人称", "侧拍 正前方", "第一人称 全身"])
def test_incompatible_leg_requests_are_rejected_before_prompt_generation(query):
    with pytest.raises(LegFocusDataError):
        build_leg_focus_action(query)


@pytest.mark.parametrize("external_reference", [False, True])
@pytest.mark.parametrize("english,translation_applied", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("pose_id,view_id", [
    ("edge_knees_together_dangle", "third_front_low"),
    ("edge_knees_together_dangle", "third_side_low"),
    ("edge_knees_together_ankles_apart", "third_front_low"),
    ("edge_ankles_apart_both_flex", "third_oblique_low"),
    ("kneeling_sit_back", "self_forward_down"),
    ("kneeling_upright", "self_steep_down"),
    ("kneeling_upright", "third_oblique_low"),
    ("kneeling_sit_back", "self_mirror_front_down"),
    ("kneeling_upright", "self_mirror_left_down"),
    ("seated_thigh_gentle_pinch", "self_forward_down"),
    ("seated_self_knees_offset", "self_right_down"),
    ("kneeling_relaxed_apart", "third_kneeling_oblique_down"),
    ("chair_thigh_cross_slant", "third_oblique_low"),
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
    framing_key = "mirror" if LEGFOCUS_VIEW_BY_ID[view_id].get("capture_mode") == "mirror" else camera
    assert prompt.count(pose["prompt"]) == 1
    assert prompt.count(LEGFOCUS_FRAMING) == 1
    assert prompt.count(LEGFOCUS_SCENE_RULE) == 1
    assert prompt.count(pose["crop_prompts"][framing_key]) == 1
    assert prompt.count(LEGFOCUS_CAMERA_FRAMINGS[framing_key]) == 1
    assert sum(prompt.count(rule) for rule in LEGFOCUS_REFERENCE_RULES.values()) == 1
    assert "严格遵循以上已锁定的看看腿动作合同" not in prompt
    if pose_id in {"edge_knees_together_dangle", "edge_knees_together_ankles_apart", "edge_ankles_apart_both_flex", "chair_thigh_cross_slant", "kneeling_upright"} and camera == "third":
        assert camera == "third"
        assert "第一人称" not in prompt
        assert "俯拍" not in prompt
        assert "仰拍" in prompt
    for other_camera, crop in pose["crop_prompts"].items():
        if other_camera != framing_key:
            assert crop not in prompt
    if camera == "selfie":
        assert "第一人称腰线至足部照片" not in prompt
        assert "从胸下或腰线附近裁" not in prompt
        assert prompt.count("上缘裁过肩部") == 1
        assert "胸部可随姿势自然保留" in prompt
    assert "脚端四周留出少量余量" not in prompt
    assert "腰头至双脚完整" not in prompt
    if pose_id.startswith("edge_"):
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
    for scene, support_id, view in compatible_leg_compositions(pose):
        view_id = view["id"]
        action = build_leg_focus_action(scene["id"], has_refs=True, force_pose=pose_id, force_view=view_id)
        prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(
            None, action, [ImageReference(data=b"external", mime_type="image/png")]
        ))
        if pose_id == "reclined_offset_legs":
            assert view["camera"] == "selfie"
            assert "第三人称" not in prompt
        if view["camera"] == "third":
            assert "第一人称" not in prompt
            assert "本人看向自己双腿" not in prompt
        else:
            assert "第三人称" not in prompt
            assert view["elevation"] in {"down", "level"}
            assert "人物外部" not in prompt
        if support_id in {"seat_edge", "raised_edge", "chair_seat", "armrest", "backrest"}:
            if pose_id in DIRECT_SELF_SEATED_POSES:
                assert view["camera"] == "selfie", (pose_id, view_id)
                assert view["elevation"] == "down", (pose_id, view_id)
            else:
                assert view["camera"] == "third", (pose_id, view_id)
                assert view["elevation"] == "up", (pose_id, view_id)
                assert "仰拍" in view["prompt"]
        if support_id in {"ground_surface", "raised_soft_surface", "reclined_surface", "lying_surface"}:
            if pose_id not in COMPLEX_THIRD_POSES and pose_id != "supported_straight_legs":
                assert view["camera"] == "selfie", (pose_id, support_id, view_id)
        assert f"【leg_scene:{scene['id']}】" in prompt
        assert f"【leg_support:{support_id}】" in prompt
        assert prompt.count("左右均指人物自身") == 1
        assert prompt.count(LEGFOCUS_ANATOMY_RULE) == 1
        assert "实际可见部位为主" in prompt
        assert "可见脚与同侧小腿自然衔接" in prompt
        assert not re.search(r"腿部近照|取至小腿或脚端|脚端随(?:画幅|构图)|取至屈膝与部分小腿", prompt)
        framing_key = "mirror" if view.get("capture_mode") == "mirror" else view["camera"]
        for selected in (
            action, LEGFOCUS_FRAMING, LEGFOCUS_CAMERA_FRAMINGS[framing_key],
            pose["crop_prompts"][framing_key], view["prompt"], pose["prompt"],
            LEGFOCUS_SCENE_RULE, scene["prompt"], scene["supports"][support_id],
        ):
            assert prompt.count(selected) == 1, (pose_id, view_id, selected)
        sentences = [part.strip() for part in re.split(r"[。\n]", prompt) if part.strip()]
        duplicates = [part for part, count in Counter(sentences).items() if count > 1]
        assert not duplicates, (pose_id, view_id, duplicates)
        assert prompt.count("上缘裁过肩部") == 1
        assert "胸部可随姿势自然保留" in prompt
        assert "胸部留在画外" not in prompt
        assert "从胸下或腰线附近裁" not in prompt
        if pose_id == "side_seated_front_support":
            assert "双手与前臂保持连续" in prompt
            assert "不因裁切而截断手腕" in prompt
        if pose_id == "side_folded_legs":
            assert "前后腿自然错层但不堆叠" in prompt
            assert "不把两条腿融合成一团" in prompt
        if pose_id == "one_knee_side_support":
            assert "相机退开保持普通焦段" in prompt
            assert "近端腿不占满画面" in prompt
        if pose_id == "side_lying_offset_legs":
            assert "近侧大腿和膝盖不贴近镜头" in prompt
            assert "避免夸张近大远小" in prompt
        assert "约与坐姿膝部同高" not in prompt
        assert "腰头至双脚完整" not in prompt
        assert "第一人称腰线至" not in prompt


@pytest.mark.parametrize("pose_id", [
    "edge_ankles_apart_both_flex", "chair_thigh_cross_slant",
    "edge_single_foot_flex", "edge_knees_together_slant", "seated_knees_offset",
])
def test_supported_third_person_poses_use_low_views_only(pose_id):
    pose = LEGFOCUS_POSE_BY_ID[pose_id]
    combinations = compatible_leg_compositions(pose)
    assert combinations
    assert all(
        view["camera"] == "third"
        and view["elevation"] == "up"
        and view["direction"] in {"front", "oblique", "side"}
        for _, _, view in combinations
    )
    with pytest.raises(LegFocusDataError, match="不兼容"):
        build_leg_focus_action(force_pose=pose_id, force_camera="selfie")


@pytest.mark.parametrize("pose_id,scenes,supports,camera", [
    ("edge_single_foot_flex", {"bed_edge", "bedroom_bench", "gaming_chair", "desk_chair", "indoor_cabinet"}, {"raised_edge"}, "third"),
    ("edge_knees_together_slant", {"bed_edge", "bedroom_bench", "gaming_chair", "desk_chair", "indoor_cabinet"}, {"raised_edge"}, "third"),
    ("seated_knees_offset", {"bed_edge", "bedroom_bench", "sofa", "gaming_chair", "desk_chair", "living_room_chair", "indoor_desk", "indoor_cabinet"}, {"seat_edge", "chair_seat"}, "third"),
    ("reclined_same_side_knees", {"bed_surface", "sofa", "floor_window", "bedroom_chaise"}, {"reclined_surface"}, "selfie"),
    ("standing_weight_shift", {"indoor_desk", "indoor_cabinet"}, {"standing_edge"}, "third"),
])
def test_added_leg_poses_keep_their_required_support_and_camera(pose_id, scenes, supports, camera):
    pose = LEGFOCUS_POSE_BY_ID[pose_id]
    combinations = compatible_leg_compositions(pose)
    assert {scene["id"] for scene, _, _ in combinations} == scenes
    assert {support for _, support, _ in combinations} <= supports
    assert {view["camera"] for _, _, view in combinations} == {camera}
    if camera == "third":
        assert all(view["elevation"] == "up" for _, _, view in combinations)
    else:
        assert {view["id"] for _, _, view in combinations} == {"self_left_down", "self_right_down"}
    for query in ("卧室地毯", "第三人称 俯拍", "落地镜"):
        with pytest.raises(LegFocusDataError, match="不兼容"):
            build_leg_focus_action(query, force_pose=pose_id)


@pytest.mark.parametrize("appearance_type", ["auto", "real", "anime"])
@pytest.mark.parametrize("english,translation_applied", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("external_reference", [False, True])
def test_leg_appearance_details_reach_the_final_request_once(
    tmp_path, appearance_type, english, translation_applied, external_reference,
):
    plugin = plugin_for_prompts(tmp_path, english)
    plugin.persona.set_appearance_type(appearance_type)
    if translation_applied:
        plugin._translate_prompt_to_english = AsyncMock(
            side_effect=lambda text, **_kw: (text, {"applied": True})
        )
    action = build_leg_focus_action(force_pose="reclined_same_side_knees", has_refs=external_reference)
    refs = [ImageReference(data=b"external", mime_type="image/png")] if external_reference else []
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, refs))
    language = "en" if english else "zh"
    appearance = LEGFOCUS_APPEARANCE_PROMPTS[appearance_type][language]
    assert prompt.count(appearance) == 1
    assert prompt.count(action) == 1
    assert "【cam:selfie】" in prompt
    assert "第三人称" not in prompt
    for other_type, translations in LEGFOCUS_APPEARANCE_PROMPTS.items():
        for other_language, line in translations.items():
            if (other_type, other_language) != (appearance_type, language):
                assert line not in prompt


@pytest.mark.parametrize("appearance_type", ["auto", "real", "anime"])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_leg_builtin_templates_append_appearance_before_return_without_repeating_it(appearance_type, language):
    action = build_leg_focus_action(force_pose="edge_single_foot_flex", has_refs=True)
    kwargs = dict(language=language, appearance_type=appearance_type, has_reference_image=True)
    prompt = build_selfie_builtin_prompt(action, **kwargs)
    appearance = LEGFOCUS_APPEARANCE_PROMPTS[appearance_type][language]
    assert prompt.count(appearance) == 1
    assert prompt.count(action) == 1
    rebuilt = build_selfie_builtin_prompt(action, action_content=prompt, **kwargs)
    assert rebuilt.count(appearance) == 1
    assert rebuilt.count(action) == 1
    assert "【cam:third】" in rebuilt
    assert "本人看向自己双腿" not in rebuilt


@pytest.mark.parametrize("scene_query,scene_id,view_id", [
    ("床上", "bed_surface", "self_forward_down"),
    ("地毯", "bedroom_rug", "self_left_down"),
    ("沙发", "sofa", "self_right_down"),
])
def test_kneeling_leg_actions_keep_both_knees_on_a_supported_surface(tmp_path, scene_query, scene_id, view_id):
    plugin = plugin_for_prompts(tmp_path)
    action = build_leg_focus_action(
        f"第一人称 {scene_query} 俯拍", has_refs=True,
        force_pose="kneeling_sit_back", force_view=view_id,
    )
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(
        None, action, [ImageReference(data=b"external", mime_type="image/png")]
    ))
    assert f"【leg_scene:{scene_id}】" in prompt
    assert "【leg_support:kneeling_surface】" in prompt
    assert prompt.count(LEGFOCUS_POSE_BY_ID["kneeling_sit_back"]["prompt"]) == 1
    assert "双膝自然靠近落在柔软平面上" in prompt
    assert "第三人称" not in prompt
    with pytest.raises(LegFocusDataError, match="不兼容"):
        build_leg_focus_action(f"第三人称 {scene_query}", force_pose="kneeling_sit_back")


@pytest.mark.parametrize("pose_id", [
    "chair_thigh_cross_slant",
])
def test_crossed_leg_final_requests_keep_each_foot_with_its_own_leg(tmp_path, pose_id):
    plugin = plugin_for_prompts(tmp_path)
    for view_id in (
        view_id for refs in LEGFOCUS_POSE_BY_ID[pose_id]["camera_views"].values()
        for view_id in refs
    ):
        action = build_leg_focus_action(has_refs=True, force_pose=pose_id, force_view=view_id)
        prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(
            None, action, [ImageReference(data=b"external", mime_type="image/png")]
        ))
        assert prompt.count("左右均指人物自身") == 1
        assert "右脚" in prompt and "左脚" in prompt
        assert not re.search(r"右(?:膝|踝|小腿)[^，；。]*连到[^，；。]*左脚", prompt)
        assert not re.search(r"左(?:膝|踝|小腿)[^，；。]*连到[^，；。]*右脚", prompt)
        if pose_id.startswith("chair_"):
            assert "悬空的右脚" in prompt
            assert re.search(r"左脚(?:着地|稳放地面)", prompt)
            assert "两脚均放在同一支撑平面" not in prompt
        elif pose_id == "edge_feet_overlap":
            assert "双脚离地" in prompt
            assert "膝部与小腿不交叉" in prompt
        else:
            support_id = re.search(r"【leg_support:([a-z_]+)】", prompt).group(1)
            assert support_id in {"raised_soft_surface", "ground_surface"}
            assert "双脚离地" not in prompt


@pytest.mark.parametrize("query,pose_id", [
    ("小腿倒V", "edge_knees_together_ankles_apart"),
    ("倒V双脚轻勾", "edge_ankles_apart_both_flex"),
    ("双脚悬垂", "edge_knees_together_dangle"),
])
@pytest.mark.parametrize("external_reference", [False, True])
def test_dangling_foot_variants_keep_their_pose_camera_and_clothing(tmp_path, query, pose_id, external_reference):
    plugin = plugin_for_prompts(tmp_path)
    view_id = "third_front_low" if pose_id in {"edge_knees_together_ankles_apart", "edge_knees_together_dangle"} else "third_oblique_low"
    action = build_leg_focus_action(
        f"第三人称 床沿 {query}", has_refs=external_reference, force_view=view_id,
    )
    refs = [ImageReference(data=b"external", mime_type="image/png")] if external_reference else []
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, refs))
    assert f"【pose:{pose_id}】" in prompt
    assert "【leg_support:raised_edge】" in prompt
    assert "【cam:third】" in prompt
    assert f"【leg_view:{view_id}】" in prompt
    assert prompt.count(LEGFOCUS_POSE_BY_ID[pose_id]["prompt"]) == 1
    assert "双脚离地" in prompt
    assert "脚掌落地" not in prompt
    assert "必须赤足" not in prompt
    if "flex" in pose_id or "overlap" in pose_id:
        assert "脚尖自然下垂" not in prompt
    if external_reference:
        assert "保留原有上装、下装、连体结构、袜鞋与配饰" in prompt
    if "ankles_apart" in pose_id:
        assert "脚踝间距略大于膝间距" in prompt
        with pytest.raises(LegFocusDataError, match="不兼容"):
            build_leg_focus_action("第三人称 侧拍", force_pose=pose_id)


@pytest.mark.parametrize("query,scene_id,view_id", [
    ("第一人称 床上 俯拍", "bed_surface", "self_forward_down"),
    ("第一人称 沙发 偏左 俯拍", "sofa", "self_left_down"),
    ("第一人称 卧室地毯 俯拍", "bedroom_rug", "self_forward_down"),
    ("第一人称 客厅地毯 偏右 俯拍", "living_room_rug", "self_right_down"),
])
def test_kneeling_self_view_uses_a_compatible_surface(tmp_path, query, scene_id, view_id):
    plugin = plugin_for_prompts(tmp_path)
    action = build_leg_focus_action(query, has_refs=True, force_pose="kneeling_sit_back", force_view=view_id)
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(
        None, action, [ImageReference(data=b"external", mime_type="image/png")]
    ))
    assert "【leg_scene:" + scene_id + "】" in prompt
    assert "【leg_support:kneeling_surface】" in prompt
    assert prompt.count(LEGFOCUS_VIEW_BY_ID[view_id]["prompt"]) == 1
    assert "第三人称" not in prompt


@pytest.mark.parametrize("external_reference", [False, True])
@pytest.mark.parametrize("query,pose_id,view_id,support_id", [
    ("落地镜", "kneeling_upright", "self_mirror_front_down", "kneeling_surface"),
    ("穿衣镜", "kneeling_sit_back", "self_mirror_left_down", "kneeling_surface"),
    ("全身镜", "kneeling_sit_back", "self_mirror_right_down", "kneeling_surface"),
    ("镜前", "kneeling_upright", "self_mirror_left_down", "kneeling_surface"),
])
@pytest.mark.parametrize("english", [False, True])
def test_floor_mirror_scene_uses_visible_mirror_and_consistent_selfie_in_final_prompt(
    tmp_path, external_reference, query, pose_id, view_id, support_id, english,
):
    plugin = plugin_for_prompts(tmp_path, english)
    action = build_leg_focus_action(
        query, has_refs=external_reference, force_pose=pose_id, force_view=view_id,
    )
    refs = [ImageReference(data=b"external", mime_type="image/png")] if external_reference else []
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, refs))
    assert "【leg_scene:floor_mirror】" in prompt
    assert "【cam:selfie】" in prompt
    assert f"【leg_support:{support_id}】" in prompt
    assert prompt.count(LEGFOCUS_CAMERA_FRAMINGS["mirror"]) == 1
    assert prompt.count(LEGFOCUS_POSE_BY_ID[pose_id]["crop_prompts"]["mirror"]) == 1
    assert prompt.count(LEGFOCUS_POSE_BY_ID[pose_id]["hand_prompts"]["mirror"]) == 1
    assert LEGFOCUS_POSE_BY_ID[pose_id]["hand_prompts"]["direct"] not in prompt
    assert LEGFOCUS_CAMERA_FRAMINGS["selfie"] not in prompt
    assert "镜面占据主要画幅" in prompt
    assert "画幅边缘自然入画" not in prompt
    assert "本人看向自己双腿" not in prompt
    assert "设备留在画外" not in prompt
    assert prompt.count(LEGFOCUS_SCENE_BY_ID["floor_mirror"]["prompt"]) == 1
    assert "第三人称" not in prompt
    assert "头部到足部完整入镜" not in prompt
    if external_reference:
        assert "【outfit:reference】" in prompt


def test_floor_mirror_is_excluded_from_all_third_person_selections():
    for pose in LEGFOCUS_POSE_BY_ID.values():
        combinations = compatible_leg_compositions(pose, scene_ids="floor_mirror")
        assert all(view["camera"] == "selfie" for _, _, view in combinations)
        assert all(view.get("capture_mode") == "mirror" for _, _, view in combinations)
        assert not compatible_leg_compositions(pose, camera="third", scene_ids="floor_mirror")
    for query, options in (
        ("第三人称 落地镜", {}),
        ("落地镜", {"force_camera": "third"}),
        ("落地镜", {"force_view": "third_front_low"}),
        ("落地镜 全身", {}),
    ):
        with pytest.raises(LegFocusDataError, match="不兼容"):
            build_leg_focus_action(query, **options)


def test_leg_mirror_views_require_a_mirror_and_rebuild_obsolete_edge_only_record(tmp_path):
    from astrbot_plugin_selfie_image.cos.leg_focus import has_leg_focus_contract

    for view in LEGFOCUS_VIEW_BY_ID.values():
        if view.get("capture_mode") != "mirror":
            continue
        combinations = compatible_leg_compositions(LEGFOCUS_POSE_BY_ID["kneeling_upright"], view_id=view["id"])
        assert combinations
        assert {scene["id"] for scene, _, _ in combinations} == {"floor_mirror"}
        with pytest.raises(LegFocusDataError, match="不兼容"):
            build_leg_focus_action("床上", force_pose="kneeling_upright", force_view=view["id"])
    legacy = (
        "镜框与局部镜面在腿部远端的画幅边缘自然入画。本人看向自己双腿。"
        " 【legs:outfit】 【frame:leg_detail】 【pose:kneeling_upright】"
        " 【leg_view:self_forward_down】 【leg_scene:floor_mirror】 【leg_support:kneeling_surface】"
        " 【cam:selfie】 【wear:black】 【outfit:reference】"
    )
    assert not has_leg_focus_contract(legacy)
    plugin = plugin_for_prompts(tmp_path)
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(
        None, legacy, [ImageReference(data=b"external", mime_type="image/png")],
    ))
    assert has_leg_focus_contract(prompt)
    assert "【leg_scene:floor_mirror】" in prompt
    assert "【pose:kneeling_upright】" in prompt
    assert "【outfit:reference】" in prompt
    assert prompt.count(LEGFOCUS_CAMERA_FRAMINGS["mirror"]) == 1
    assert "画幅边缘自然入画" not in prompt
    assert "本人看向自己双腿" not in prompt


@pytest.mark.parametrize("pose_id,query", [
    ("seated_thigh_gentle_pinch", "室内桌边 并膝轻捏大腿"),
    ("seated_self_knees_offset", "电竞椅 自视并膝错位"),
])
def test_new_leg_self_view_seated_variants_keep_subjective_crop_and_compatible_seat(tmp_path, pose_id, query):
    plugin = plugin_for_prompts(tmp_path)
    action = build_leg_focus_action(query)
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, []))
    assert f"【pose:{pose_id}】" in prompt
    assert "【cam:selfie】" in prompt
    assert "【leg_support:chair_seat】" in prompt
    assert prompt.count(LEGFOCUS_CAMERA_FRAMINGS["selfie"]) == 1
    assert prompt.count(LEGFOCUS_POSE_BY_ID[pose_id]["prompt"]) == 1
    assert "第三人称" not in prompt
    assert "镜面自拍" not in prompt
    with pytest.raises(LegFocusDataError, match="不兼容"):
        build_leg_focus_action("第三人称", force_pose=pose_id)


def test_leg_high_kneeling_camera_stays_off_seats_and_skirts(tmp_path):
    pose_id, view_id = "kneeling_relaxed_apart", "third_kneeling_oblique_down"
    for scene, support, view in compatible_leg_compositions(LEGFOCUS_POSE_BY_ID[pose_id], view_id=view_id):
        assert support == "kneeling_surface"
        assert scene["id"] not in {"bed_edge", "gaming_chair", "desk_chair"}
        assert view["camera"] == "third" and view["elevation"] == "down"
    action = build_leg_focus_action("床面分膝跪姿 俯拍", force_view=view_id)
    plugin = plugin_for_prompts(tmp_path)
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, []))
    assert "【leg_scene:bed_surface】" in prompt
    assert f"【pose:{pose_id}】" in prompt
    assert prompt.count(LEGFOCUS_VIEW_BY_ID[view_id]["prompt"]) == 1
    assert "第一人称" not in prompt
    outfit = next(outfit for outfit in LEGFOCUS_OUTFIT_POOL if outfit["prompt"] in prompt)
    assert outfit["capture_profile"] == "shorts"
    with pytest.raises(LegFocusDataError, match="不兼容"):
        build_leg_focus_action(force_pose=pose_id, force_view=view_id, force_outfit="blouse_fitted_mini")


def test_full_body_request_is_still_distinct_from_full_length_mirror():
    action = build_leg_focus_action("不要全身镜 全身")
    assert "【frame:full_outfit】" in action
    assert "【cam:third】" in action
    assert "【leg_scene:floor_mirror】" not in action


def test_elevated_seats_keep_external_low_views_and_explicit_self_view_variants():
    seat_supports = {"seat_edge", "raised_edge", "wide_edge", "armrest", "backrest", "chair_seat"}
    raised_views = set()
    for pose in LEGFOCUS_POSE_BY_ID.values():
        for _, support, view in compatible_leg_compositions(pose):
            if support not in seat_supports:
                continue
            if pose["id"] in DIRECT_SELF_SEATED_POSES:
                assert support in {"seat_edge", "chair_seat"}
                assert view["camera"] == "selfie"
                assert view["elevation"] == "down"
            else:
                assert view["camera"] == "third", (pose["id"], support, view["id"])
                assert view["elevation"] == "up", (pose["id"], support, view["id"])
            if support == "raised_edge":
                assert view["elevation"] == "up", (pose["id"], view["id"])
                raised_views.add(view["direction"])
    assert raised_views == {"front", "oblique", "side"}


@pytest.mark.parametrize("pose_id", [
    "chair_thigh_cross_slant", "edge_ankles_apart_both_flex",
    "edge_knees_together_dangle", "edge_knees_together_ankles_apart",
])
@pytest.mark.parametrize("query", ["第一人称", "俯拍", "顶拍"])
def test_seat_edge_poses_reject_self_view_and_downward_camera_requests(pose_id, query):
    with pytest.raises(LegFocusDataError, match="不兼容"):
        build_leg_focus_action(query, force_pose=pose_id)


@pytest.mark.parametrize("pose_id,scene_id,view_id,support_id,camera,new_camera", [
    ("edge_knees_together_dangle", "bed_edge", "self_left_down", "raised_edge", "selfie", "third"),
    ("chair_thigh_cross_slant", "living_room_chair", "self_right_down", "chair_seat", "selfie", "third"),
    ("kneeling_sit_back", "floor_window", "third_oblique_low", "kneeling_surface", "third", "selfie"),
    ("kneeling_upright", "floor_window", "third_oblique_low", "kneeling_surface", "third", "selfie"),
])
@pytest.mark.parametrize("english,translation_applied", [(False, False), (True, False), (True, True)])
def test_obsolete_leg_requests_are_rebuilt_with_current_support_and_camera(
    tmp_path, pose_id, scene_id, view_id, support_id, camera, new_camera, english, translation_applied,
):
    from astrbot_plugin_selfie_image.cos.leg_focus import has_leg_focus_contract

    legacy = (
        "本人看向自己双腿的第一人称主观视角。沿自身右侧腿线偏右斜俯拍。"
        f" 【legs:outfit】 【frame:leg_detail】 【pose:{pose_id}】"
        f" 【leg_view:{view_id}】 【leg_scene:{scene_id}】 【leg_support:{support_id}】"
        f" 【cam:{camera}】 【wear:black】 【outfit:reference】"
    )
    assert not has_leg_focus_contract(legacy)
    plugin = plugin_for_prompts(tmp_path, english)
    if translation_applied:
        plugin._translate_prompt_to_english = AsyncMock(
            side_effect=lambda text, **_kw: (text, {"applied": True})
        )
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(
        None, legacy, [ImageReference(data=b"external", mime_type="image/png")]
    ))
    assert has_leg_focus_contract(prompt)
    assert f"【cam:{new_camera}】" in prompt
    assert f"【leg_view:{view_id}】" not in prompt
    assert prompt.count("左右均指人物自身") == 1
    if new_camera == "selfie":
        assert "第三人称" not in prompt
        assert "【leg_support:kneeling_surface】" in prompt
    else:
        assert "第一人称" not in prompt
        assert "俯拍" not in prompt
        assert re.search(r"【leg_support:(?:raised_edge|chair_seat)】", prompt)


@pytest.mark.parametrize("query,pose_id,view_id,scene_id", [
    ("第一人称 落地窗前 跪坐", "kneeling_sit_back", "self_forward_down", "floor_window"),
    ("第一人称 落地窗前 直身跪姿 偏左俯拍", "kneeling_upright", "self_left_down", "floor_window"),
    ("第三人称 床上 直身跪姿 仰拍", "kneeling_upright", "third_oblique_low", "bed_surface"),
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
    if pose_id.startswith("kneeling_"):
        assert "【leg_support:kneeling_surface】" in prompt
    if pose_id == "kneeling_sit_back":
        assert "小腿与脚处在身后画外" in prompt


@pytest.mark.parametrize("pose_id", [
    "edge_knees_together_dangle", "edge_ankles_apart_both_flex",
    "edge_knees_together_ankles_apart", "chair_thigh_cross_slant",
])
def test_folded_and_forward_legs_do_not_randomly_use_head_on_level_camera(pose_id):
    with pytest.raises(LegFocusDataError, match="不兼容"):
        build_leg_focus_action(force_pose=pose_id, force_view="self_forward_down")


def test_random_third_person_selection_keeps_front_views_and_side_variation():
    counts = Counter()
    with patch("astrbot_plugin_selfie_image.cos.leg_focus.random", random.Random(20261108)):
        for _ in range(400):
            action = build_leg_focus_action("第三人称")
            view_id = re.search(r"【leg_view:([a-z_]+)】", action).group(1)
            counts[LEGFOCUS_VIEW_BY_ID[view_id]["direction"]] += 1
    assert set(counts) == {"front", "oblique", "side"}, counts


def test_wider_first_person_framing_keeps_the_subjects_actual_visible_range(tmp_path):
    plugin = plugin_for_prompts(tmp_path)
    action = build_leg_focus_action(force_pose="kneeling_sit_back", force_view="self_forward_down")
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, []))
    assert prompt.count("第一人称主观视角") == 1
    assert "普通手机焦段和正常拍摄距离" in prompt
    assert "脚端依实际视线与遮挡自然入镜" in prompt
    assert "近侧大腿和膝盖不贴近镜头" in prompt
    assert "腰线至足部照片" not in prompt
    assert "双脚完整入镜" not in prompt


def test_suspended_knees_use_front_view_and_avoid_random_stirrup_socks():
    from astrbot_plugin_selfie_image.cos.leg_focus import LEGFOCUS_HOSIERY_STYLES

    pose_id = "edge_knees_together_ankles_apart"
    pose = LEGFOCUS_POSE_BY_ID[pose_id]
    assert pose["camera_views"] == {"third": ["third_front_low"]}
    assert "stirrup" in pose["disallowed_hosiery_kinds"]

    action = build_leg_focus_action(
        "黑丝", force_pose=pose_id, force_view="third_front_low",
    )
    assert "彼此分开，不交叉、不扭转" in action
    assert not any(
        style["kind"] == "stirrup" and style["prompt"] in action
        for style in LEGFOCUS_HOSIERY_STYLES
    )
    with pytest.raises(LegFocusDataError, match="袜型不兼容"):
        build_leg_focus_action(
            "白色踩脚袜", force_pose=pose_id, force_view="third_front_low",
        )


@pytest.mark.parametrize("pose_id", ["edge_knees_together_dangle", "edge_ankles_apart_both_flex"])
def test_edge_leg_poses_keep_third_person_views_and_avoid_stirrup(pose_id):
    from astrbot_plugin_selfie_image.cos.leg_focus import LEGFOCUS_HOSIERY_STYLES

    pose = LEGFOCUS_POSE_BY_ID[pose_id]
    assert set(pose["camera_views"]) == {"third"}
    assert "stirrup" in pose["disallowed_hosiery_kinds"]
    action = build_leg_focus_action(
        "黑丝", force_pose=pose_id, force_view="third_oblique_low",
    )
    assert "【leg_view:third_oblique_low】" in action
    assert not any(
        style["kind"] == "stirrup" and style["prompt"] in action
        for style in LEGFOCUS_HOSIERY_STYLES
    )


@pytest.mark.parametrize("query,expected_aspect", [("", "9:16"), ("方图", "1:1"), ("--ar 3:4", "3:4")])
def test_leg_batch_initial_square_neck_outfit_keeps_requested_aspect(tmp_path, query, expected_aspect):
    plugin = plugin_for_prompts(tmp_path)
    plugin.config.image_default_aspect_ratio = "自动"
    plugin.config.image_default_resolution = "1K"
    plugin.presets = SimpleNamespace(resolve=lambda prompt: {"prompt": prompt})
    outfit = next(item for item in LEGFOCUS_OUTFIT_POOL if item["id"] == "sleep_square_neck_slip")
    with patch("astrbot_plugin_selfie_image.cos.leg_focus.pick_leg_focus_outfit", return_value=outfit):
        action = build_leg_focus_action(query, force_pose="chair_thigh_cross_slant", force_view="third_oblique_low")
    assert "【outfit:sleep_square_neck_slip】" in action
    _, aspect, _, _, _ = plugin._resolve_image_preset(action)
    assert aspect == expected_aspect
    assert parse_prompt_options(action, default_aspect_ratio="自动")[1] == expected_aspect


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
    ("selfie", "看看腿 第一人称 电竞椅 白色短袜"),
    ("selfie", "看看腿 第一人称 落地镜 白色短袜"),
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
        framing = "mirror" if "落地镜" in query else "selfie"
        other_framing = "selfie" if framing == "mirror" else "mirror"
        assert LEGFOCUS_CAMERA_FRAMINGS[framing] in request["prompt"]
        assert LEGFOCUS_CAMERA_FRAMINGS[other_framing] not in request["prompt"]
        assert "【cam:selfie】" in action
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
