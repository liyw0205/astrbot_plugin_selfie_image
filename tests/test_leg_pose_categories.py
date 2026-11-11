"""Pose categories must constrain both selection and persisted action contracts."""

import asyncio
import re
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from astrbot_plugin_selfie_image.cos.leg_focus import (
    LEGFOCUS_POSE_BY_ID,
    LegFocusDataError,
    build_leg_focus_action,
    ensure_leg_focus_action,
    has_leg_focus_contract,
    parse_requested_leg_pose,
)


@pytest.mark.parametrize("query,postures", [
    ("站姿", {"standing"}),
    ("站着", {"standing"}),
    ("坐姿", {"seated"}),
    ("坐着", {"seated"}),
    ("跪姿", {"kneeling"}),
    ("跪坐", {"kneeling"}),
    ("躺姿", {"lying", "side_lying", "prone", "reclined"}),
    ("躺着", {"lying", "side_lying", "prone", "reclined"}),
    ("仰卧", {"lying"}),
    ("平躺", {"lying"}),
    ("侧躺", {"side_lying"}),
    ("侧卧", {"side_lying"}),
    ("俯卧", {"prone"}),
    ("趴着", {"prone"}),
    ("半躺", {"reclined"}),
    ("半靠", {"reclined"}),
    ("躺姿 侧躺", {"side_lying"}),
    ("躺姿 不要侧躺 不要俯卧", {"lying", "reclined"}),
    ("不要站姿 不要坐姿 不要跪姿", {"lying", "side_lying", "prone", "reclined"}),
])
def test_category_selects_all_and_only_compatible_postures(query, postures):
    with patch(
        "astrbot_plugin_selfie_image.cos.leg_focus._weighted_pose_choice",
        side_effect=lambda pool: pool[0],
    ) as choose:
        action = build_leg_focus_action(query)
    candidates = choose.call_args.args[0]
    assert {pose["posture"] for pose in candidates} == postures
    assert {pose["id"] for pose in candidates} == {
        pose["id"] for pose in LEGFOCUS_POSE_BY_ID.values() if pose["posture"] in postures
    }
    assert has_leg_focus_contract(action)
    assert has_leg_focus_contract(ensure_leg_focus_action(action))


@pytest.mark.parametrize("query", ["站姿 躺姿", "坐姿 跪姿", "侧躺 俯卧", "站姿 不要站姿"])
def test_conflicting_categories_are_rejected(query):
    with pytest.raises(LegFocusDataError, match="姿势类别.*冲突"):
        build_leg_focus_action(query)


@pytest.mark.parametrize("query,options", [
    ("站姿 自拍", {}),
    ("俯卧 自拍", {}),
    ("站姿 床面", {}),
    ("侧躺", {"force_pose": "standing_weight_shift"}),
    ("站姿", {"force_pose": "bed_supine_knees_together"}),
])
def test_category_does_not_fall_back_to_another_posture(query, options):
    with pytest.raises(LegFocusDataError, match="不兼容"):
        build_leg_focus_action(query, **options)


@pytest.mark.parametrize("query", ["不要并膝自然跪坐", "不要并膝坐姿轻捏大腿"])
def test_negated_specific_pose_does_not_request_its_category(query):
    with patch(
        "astrbot_plugin_selfie_image.cos.leg_focus._weighted_pose_choice",
        side_effect=lambda pool: pool[0],
    ) as choose:
        build_leg_focus_action(query)
    candidates = choose.call_args.args[0]
    assert {pose["posture"] for pose in candidates} == {
        "standing", "seated", "kneeling", "lying", "side_lying", "prone", "reclined",
    }
    assert all(pose["title"] not in query for pose in candidates)


def test_generic_kneeling_does_not_resolve_to_one_fixed_pose():
    assert parse_requested_leg_pose("跪姿") == ""
    assert parse_requested_leg_pose("跪坐") == ""
    assert parse_requested_leg_pose("并膝自然跪坐") == "kneeling_sit_back"


@pytest.mark.parametrize("pose_id", list(LEGFOCUS_POSE_BY_ID))
def test_exact_pose_name_still_selects_the_requested_pose(pose_id):
    pose = LEGFOCUS_POSE_BY_ID[pose_id]
    action = build_leg_focus_action(pose["title"])
    assert re.search(r"【pose:([a-z_]+)】", action).group(1) == pose_id


def test_expansion_has_the_accepted_posture_distribution():
    assert Counter(pose["posture"] for pose in LEGFOCUS_POSE_BY_ID.values()) == {
        "standing": 7, "seated": 11, "lying": 5, "side_lying": 3,
        "prone": 3, "reclined": 4, "kneeling": 6,
    }


@pytest.mark.parametrize("pose_id,support,scenes", [
    ("standing_chair_single_leg_back", "standing_chair", {"gaming_chair", "desk_chair", "living_room_chair"}),
    ("standing_one_foot_on_stool", "standing_stool", {"chair_footstool"}),
    ("reclined_feet_on_stool", "reclined_footstool", {"sofa_footstool"}),
    ("kneeling_single_knee_grounded", "half_kneeling_surface", {"floor_cushion", "bedroom_rug", "living_room_rug"}),
])
def test_special_supports_cannot_fall_back_to_an_incompatible_surface(pose_id, support, scenes):
    from astrbot_plugin_selfie_image.cos.leg_focus import compatible_leg_compositions

    combinations = compatible_leg_compositions(LEGFOCUS_POSE_BY_ID[pose_id])
    assert {scene["id"] for scene, _, _ in combinations} == scenes
    assert {selected for _, selected, _ in combinations} == {support}
    with pytest.raises(LegFocusDataError, match="不兼容"):
        build_leg_focus_action("床面", force_pose=pose_id)


@pytest.mark.parametrize("pose_id", [
    "prone_legs_extended", "prone_single_knee_bent", "prone_both_knees_bent",
])
def test_prone_uses_only_external_side_view(pose_id):
    from astrbot_plugin_selfie_image.cos.leg_focus import compatible_leg_compositions

    combinations = compatible_leg_compositions(LEGFOCUS_POSE_BY_ID[pose_id])
    assert combinations
    assert {(view["camera"], view["direction"], view["elevation"]) for _, _, view in combinations} == {
        ("third", "side", "level"),
    }
    with pytest.raises(LegFocusDataError, match="不兼容"):
        build_leg_focus_action(force_pose=pose_id, force_camera="selfie")


@pytest.mark.parametrize("query", [
    "站姿 扶椅后屈单腿", "仰卧 仰卧双膝轻错位", "侧躺 上腿屈膝前放",
    "俯卧 双腿屈膝错落", "半躺 双脚搭脚凳", "跪姿 单膝跪地",
])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_expanded_pose_contract_survives_persona_and_bilingual_templates(tmp_path, query, language):
    from astrbot_plugin_selfie_image.features.persona import PersonaManager
    from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt

    manager = PersonaManager(tmp_path)
    action = build_leg_focus_action(query, has_refs=True, force_camera="third")
    persona_prompt = manager.build_selfie_prompt(action, "心酱", "", True)
    prompt = build_selfie_builtin_prompt(
        action, action_content=persona_prompt, language=language, has_reference_image=True,
    )
    pose_id = re.search(r"【pose:([a-z_]+)】", action).group(1)
    assert prompt.count(action) == 1
    assert prompt.count(LEGFOCUS_POSE_BY_ID[pose_id]["prompt"]) == 1
    assert "本人看向自己双腿" not in prompt
    assert has_leg_focus_contract(prompt)


@pytest.mark.parametrize("query,postures", [
    ("站姿", {"standing"}), ("坐姿", {"seated"}), ("跪姿", {"kneeling"}),
    ("躺姿", {"lying", "side_lying", "prone", "reclined"}),
    ("侧躺", {"side_lying"}), ("俯卧", {"prone"}), ("半躺", {"reclined"}),
    ("不要站姿 躺姿", {"lying", "side_lying", "prone", "reclined"}),
])
def test_batch_rebuild_keeps_category_without_locking_one_pose(tmp_path, query, postures):
    from astrbot_plugin_selfie_image.features.persona import PersonaManager
    from astrbot_plugin_selfie_image.main import SelfieImagePlugin

    plugin = object.__new__(SelfieImagePlugin)
    plugin.persona = PersonaManager(tmp_path)
    plugin.config = SimpleNamespace(image_max_batch_count=10)
    plugin._ensure_daily_selfie_schedule = AsyncMock()
    plugin._persona_identity_reference = lambda **_kw: None
    plugin._persona_auxiliary_references = lambda action="": []
    plugin._prompt_en_needed = lambda *_a, **_kw: False
    plugin._run_image_generation = AsyncMock(return_value={"success": True})

    async def run_shots(**kwargs):
        return [await kwargs["run_one"](index) for index in range(kwargs["total"])]

    plugin._run_counted_generation_shots = run_shots
    action = build_leg_focus_action(query)
    asyncio.run(plugin._run_selfie_batches_unlocked(
        "", None, action, [], "command-look-legs", 3, "9:16", "1K", "失败",
        rebuild_extra_request=query,
    ))
    poses = [
        re.search(r"【pose:([a-z_]+)】", call.args[0]).group(1)
        for call in plugin._run_image_generation.call_args_list
    ]
    assert len(poses) == 3
    assert all(LEGFOCUS_POSE_BY_ID[pose_id]["posture"] in postures for pose_id in poses)
    assert len(set(poses)) > 1
