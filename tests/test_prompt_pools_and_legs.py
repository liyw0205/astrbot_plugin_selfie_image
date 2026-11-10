from __future__ import annotations

import asyncio
import copy
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from astrbot.api import logger as _astrbot_logger  # noqa: F401
except ImportError:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
    )
    astrbot.api = api
    sys.modules.setdefault("astrbot", astrbot)
    sys.modules.setdefault("astrbot.api", api)


class PromptPoolJsonTests(unittest.TestCase):
    def test_leg_pose_support_matches_the_selected_scene(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import LEGFOCUS_POSE_BY_ID, build_leg_focus_action, compatible_leg_compositions

        self.assertEqual(set(LEGFOCUS_POSE_BY_ID), {
            "chair_thigh_cross_slant", "edge_ankles_apart_both_flex",
            "edge_knees_together_dangle", "edge_knees_together_ankles_apart",
            "kneeling_sit_back", "kneeling_upright", "chair_parallel_legs_slant",
            "bed_supine_knees_together", "chair_one_foot_on_stool",
            "edge_single_foot_flex", "edge_knees_together_slant", "seated_knees_offset",
            "reclined_same_side_knees", "standing_weight_shift",
            "seated_thigh_gentle_pinch", "seated_self_knees_offset", "kneeling_relaxed_apart",
        })
        for pose in LEGFOCUS_POSE_BY_ID.values():
            combinations = compatible_leg_compositions(pose)
            self.assertTrue(combinations, pose["id"])
            crop_keys = set(pose["camera_views"])
            if any(view.get("capture_mode") == "mirror" for _, _, view in combinations):
                crop_keys.add("mirror")
            self.assertEqual(crop_keys, set(pose["crop_prompts"]))
            for scene, support, view in combinations:
                self.assertIn(view["camera"], pose["camera_views"])
                self.assertIn(view["id"], pose["camera_views"][view["camera"]])
                self.assertIn(support, scene["supports"])
        window = compatible_leg_compositions(
            LEGFOCUS_POSE_BY_ID["kneeling_sit_back"], scene_ids="floor_window",
        )
        self.assertTrue(window)
        self.assertTrue(all(view["camera"] == "selfie" for _, _, view in window))
        action = build_leg_focus_action(force_pose="kneeling_sit_back", force_view="self_forward_down")
        self.assertIn("双手轻搭大腿外侧", action)
        self.assertNotIn("手持", action)

    def test_daily_and_builtin_pools_load_complete_versioned_records(self) -> None:
        from astrbot_plugin_selfie_image.features.daily_prompt_pools import (
            DAILY_FALLBACK_SEASONAL_LAYERS,
            DAILY_EXTRA_OUTFIT_POOL,
            DAILY_SCHEDULE_ROUTES,
            SELFIE_SNAPSHOT_PRESETS,
        )
        from astrbot_plugin_selfie_image.studio.studio import BUILTIN_PROMPTS
        from astrbot_plugin_selfie_image.prompts.group_prompt_pool import GROUP_COMPOSITIONS

        self.assertEqual(set(DAILY_FALLBACK_SEASONAL_LAYERS), set(range(1, 13)))
        self.assertGreaterEqual(len(DAILY_SCHEDULE_ROUTES), 8)
        self.assertTrue(SELFIE_SNAPSHOT_PRESETS)
        self.assertTrue(any("JK" in item for item in DAILY_EXTRA_OUTFIT_POOL))
        self.assertTrue(any("洛丽塔" in item for item in DAILY_EXTRA_OUTFIT_POOL))
        self.assertTrue(any("运动短裤" in item or "运动短裙" in item or "网球短裙" in item for item in DAILY_EXTRA_OUTFIT_POOL))
        self.assertEqual(len({item["id"] for item in BUILTIN_PROMPTS}), len(BUILTIN_PROMPTS))
        self.assertEqual(len(BUILTIN_PROMPTS), 67)
        self.assertTrue(all(item["title"] and item["prompt"] for item in BUILTIN_PROMPTS))
        self.assertTrue(GROUP_COMPOSITIONS)
        self.assertTrue(all(item["title"] and item["prompt"] for item in GROUP_COMPOSITIONS))

    def test_group_action_uses_the_json_composition_pool(self) -> None:
        from astrbot_plugin_selfie_image.prompts.selfie_actions import build_group_selfie_action

        action = build_group_selfie_action("背面合影")

        self.assertIn("【shot:back_view】", action)
        self.assertIn("不回头、不回眸、不扭颈", action)

    def test_group_pool_has_multi_person_cos_pose_variants_without_gaze_conflicts(self) -> None:
        from astrbot_plugin_selfie_image.prompts.group_prompt_pool import GROUP_COMPOSITIONS_BY_ID
        from astrbot_plugin_selfie_image.prompts.selfie_actions import build_group_selfie_action

        expected_ids = {
            "rooftop_side_support",
            "side_edge_support_group",
            "rear_three_quarter_group",
            "stair_sit_group",
            "side_hip_shift_group",
        }
        self.assertTrue(expected_ids <= GROUP_COMPOSITIONS_BY_ID.keys())
        for item_id in expected_ids:
            item = GROUP_COMPOSITIONS_BY_ID[item_id]
            self.assertIn("2-n", item["prompt"])
            self.assertIn(item["orientation"], {"side", "back", "mixed"})

        with patch(
            "astrbot_plugin_selfie_image.prompts.selfie_actions.random.choices",
            return_value=["rooftop_side_support"],
        ):
            action = build_group_selfie_action("多人合影")

        self.assertIn("【shot:rooftop_side_support】", action)
        self.assertIn("双手同时搭扶在同一段栏杆顶面", action)
        self.assertIn("旧城区楼群", action)
        self.assertIn("略低机位仰拍", action)
        self.assertIn("不强求正脸", action)
        self.assertNotIn("AI 若自然面向镜头", action)
        self.assertNotIn("默认多数人看向镜头", action)

    def test_current_event_outfit_resolves_the_complete_schedule_look(self) -> None:
        from astrbot_plugin_selfie_image.features.daily_schedule import (
            DailySchedule,
            ScheduleEvent,
        )
        from astrbot_plugin_selfie_image.features.persona import PersonaManager, daily_schedule_date_key

        outfit = "青白色连体 COS 服，配套长袜与短靴"
        with tempfile.TemporaryDirectory() as tmp:
            manager = PersonaManager(tmp)
            manager.daily_schedule_store.set_current(
                DailySchedule(
                    date=daily_schedule_date_key(),
                    route_id="test",
                    theme="test",
                    mood="",
                    seed="",
                    events=[
                        ScheduleEvent(
                            id="all_day",
                            start="00:00",
                            end="23:59",
                            title="全天",
                            scene="室内",
                            action="站立",
                            outfit_ref="main",
                        )
                    ],
                    outfits={"main": outfit},
                )
            )

            self.assertEqual(manager.get_current_daily_outfit(), outfit)

    def test_reference_look_locks_clothes_across_pose_variants(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import build_leg_focus_action

        with patch(
            "astrbot_plugin_selfie_image.cos.leg_focus.pick_leg_focus_outfit",
            side_effect=AssertionError("reference outfit must bypass random legwear"),
        ):
            first = build_leg_focus_action(
                has_refs=True,
                force_pose="seated_self_knees_offset",
                force_camera="selfie",
            )
            second = build_leg_focus_action(
                has_refs=True,
                force_pose="chair_thigh_cross_slant",
                force_camera="third",
            )

        for action in (first, second):
            self.assertIn("保留原有上装、下装、连体结构、袜鞋与配饰", action)
            self.assertIn("【outfit:reference】", action)
            self.assertNotIn("本次随机腿部细节", action)
            self.assertNotIn("白色丝袜", action)
        self.assertNotEqual(first.split("【pose:", 1)[1], second.split("【pose:", 1)[1])

    def test_explicit_user_outfit_bypasses_pool_without_random_replacement(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import build_leg_focus_action

        with patch(
            "astrbot_plugin_selfie_image.cos.leg_focus.pick_leg_focus_outfit",
            side_effect=AssertionError("explicit outfit must bypass random legwear"),
        ):
            action = build_leg_focus_action(
                "换成蓝白连体COS服和长靴",
                force_camera="third",
            )

        self.assertIn("换成蓝白连体COS服和长靴", action)
        self.assertIn("服饰按用户本次具体要求呈现", action)
        self.assertIn("【outfit:custom】", action)

    def test_leg_pool_selects_only_compatible_pose_scene_and_view_combinations(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_POSE_POOL,
            LEGFOCUS_VIEW_BY_ID,
            build_leg_focus_action,
            compatible_leg_compositions,
        )

        self.assertEqual(len(LEGFOCUS_POSE_POOL), 17)
        for pose in LEGFOCUS_POSE_POOL:
            self.assertNotIn("scene_ids", pose)
            self.assertIn("camera_views", pose)
            scene_ids = {scene["id"] for scene, _, _ in compatible_leg_compositions(pose)}
            if pose["id"] == "chair_one_foot_on_stool":
                self.assertEqual(scene_ids, {"chair_footstool"})
            else:
                self.assertGreater(len(scene_ids), 1)
            for camera, view_ids in pose["camera_views"].items():
                self.assertTrue(view_ids)
                for view_id in view_ids:
                    action = build_leg_focus_action(
                        force_pose=pose["id"], force_camera=camera, force_view=view_id,
                    )
                    self.assertIn(f"【cam:{camera}】", action)
                    self.assertIn("【frame:leg_detail】", action)
                    self.assertNotIn("看向镜头", action)
                    selected_scene = action.split("本张室内场景：", 1)[1].split("。", 1)[0]
                    self.assertNotIn("户外", selected_scene)

    def test_new_leg_poses_keep_scene_support_and_camera_compatibility(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_POSE_BY_ID, LEGFOCUS_VIEW_BY_ID, build_leg_focus_action,
            compatible_leg_compositions,
        )

        chair_pose = LEGFOCUS_POSE_BY_ID["chair_parallel_legs_slant"]
        chair_combos = compatible_leg_compositions(chair_pose)
        self.assertTrue(chair_combos)
        self.assertTrue(all(scene["id"] in {
            "gaming_chair", "desk_chair", "living_room_chair", "indoor_desk", "indoor_cabinet",
        } for scene, _, _ in chair_combos))
        self.assertEqual(set(chair_pose["camera_views"]["third"]), {"third_oblique_low", "third_side_low"})

        bed_pose = LEGFOCUS_POSE_BY_ID["bed_supine_knees_together"]
        bed_combos = compatible_leg_compositions(bed_pose)
        self.assertTrue(bed_combos)
        self.assertTrue(all(support == "lying_surface" for _, support, _ in bed_combos))
        self.assertTrue({"bed_surface", "bedroom_chaise"} <= {scene["id"] for scene, _, _ in bed_combos})
        self.assertEqual(bed_pose["camera_views"]["third"], ["third_oblique_level"])
        self.assertEqual(LEGFOCUS_VIEW_BY_ID["third_oblique_level"]["elevation"], "level")

        stool_pose = LEGFOCUS_POSE_BY_ID["chair_one_foot_on_stool"]
        stool_combos = compatible_leg_compositions(stool_pose)
        self.assertTrue(stool_combos)
        self.assertTrue(all(scene["id"] == "chair_footstool" and support == "chair_footstool" for scene, support, _ in stool_combos))
        for scene, support, view in stool_combos:
            action = build_leg_focus_action(
                force_pose=stool_pose["id"], force_camera="third", force_view=view["id"],
            )
            self.assertIn("椅前低矮软脚凳", action)
            self.assertIn(f"【leg_support:{support}】", action)

    def test_leg_camera_contracts_keep_self_view_separate_from_external_crop(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_CAMERA_FRAMINGS, LEGFOCUS_POSE_POOL, LEGFOCUS_VIEW_BY_ID, build_leg_focus_action,
        )

        for pose in LEGFOCUS_POSE_POOL:
            for view_id in (view_id for values in pose["camera_views"].values() for view_id in values):
                view = LEGFOCUS_VIEW_BY_ID[view_id]
                camera = view["camera"]
                other = "third" if camera == "selfie" else "selfie"
                framing_key = "mirror" if view.get("capture_mode") == "mirror" else camera
                with self.subTest(pose=pose["id"], view=view_id):
                    action = build_leg_focus_action(
                        has_refs=True, force_pose=pose["id"], force_view=view_id,
                    )
                    self.assertEqual(action.count(LEGFOCUS_CAMERA_FRAMINGS[framing_key]), 1)
                    self.assertNotIn(LEGFOCUS_CAMERA_FRAMINGS[other], action)
                    self.assertEqual(action.count(view["prompt"]), 1)
                    if camera == "selfie":
                        if framing_key == "mirror":
                            self.assertIn("镜面中同一人的穿搭倒影", action)
                            self.assertNotIn("本人看向自己双腿", action)
                        else:
                            self.assertIn("本人看向自己双腿", action)
                        self.assertIn("上缘裁过肩部", action)
                        self.assertIn("胸部可随姿势自然保留", action)
                        self.assertNotRegex(action, r"胸下(?:起裁|裁切|取景|开始)|三分之四侧向|椅背只")
                        self.assertNotIn("手持", action)
                    else:
                        self.assertIn("第三人称画外相机从人物外部拍摄", action)
                        self.assertNotIn("本人看向自己双腿", action)

    def test_leg_camera_contract_survives_persona_and_bilingual_templates(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_CAMERA_FRAMINGS, LEGFOCUS_REFERENCE_RULES, LEGFOCUS_POSE_BY_ID,
            LEGFOCUS_VIEW_BY_ID, build_leg_focus_action,
        )
        from astrbot_plugin_selfie_image.features.persona import PersonaManager
        from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt

        with tempfile.TemporaryDirectory() as tmp:
            manager = PersonaManager(tmp)
            for pose_id in ("kneeling_sit_back", "kneeling_upright", "chair_thigh_cross_slant"):
                cameras = set(LEGFOCUS_POSE_BY_ID[pose_id]["camera_views"])
                for camera in cameras:
                    for has_refs in (True, False):
                        action = build_leg_focus_action(
                            has_refs=has_refs, force_pose=pose_id, force_camera=camera,
                        )
                        view_id = re.search(r"【leg_view:([a-z_]+)】", action).group(1)
                        framing_key = "mirror" if LEGFOCUS_VIEW_BY_ID[view_id].get("capture_mode") == "mirror" else camera
                        prompts = {
                            "persona": manager.build_selfie_prompt(action, "心酱", "", has_refs),
                            "zh": build_selfie_builtin_prompt(action, has_reference_image=has_refs),
                            "en": build_selfie_builtin_prompt(action, language="en", has_reference_image=has_refs),
                        }
                        for path, prompt in prompts.items():
                            with self.subTest(pose=pose_id, camera=camera, refs=has_refs, path=path):
                                self.assertEqual(prompt.count(action), 1)
                                self.assertEqual(prompt.count(LEGFOCUS_CAMERA_FRAMINGS[framing_key]), 1)
                                rule = LEGFOCUS_REFERENCE_RULES["en" if path == "en" else "zh"]
                                self.assertEqual(prompt.count(rule), int(has_refs))
                                self.assertNotIn("正对镜头时自然看向镜头", prompt)

    def test_leg_full_outfit_uses_external_camera_without_self_view_or_face_crop(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_CAMERA_FRAMINGS, LEGFOCUS_POSE_POOL, LEGFOCUS_VIEW_BY_ID, LegFocusDataError, build_leg_focus_action,
        )

        for pose in LEGFOCUS_POSE_POOL:
            with self.subTest(pose=pose["id"]):
                if "third" not in pose["camera_views"]:
                    with self.assertRaises(LegFocusDataError):
                        build_leg_focus_action(force_pose=pose["id"], force_framing="full_outfit")
                    continue
                action = build_leg_focus_action(
                    has_refs=True, force_pose=pose["id"], force_camera="third",
                    force_framing="full_outfit",
                )
                self.assertIn("【frame:full_outfit】", action)
                self.assertIn("【cam:third】", action)
                self.assertIn("头部到足部完整入镜", action)
                self.assertNotIn(LEGFOCUS_CAMERA_FRAMINGS["selfie"], action)
                self.assertNotIn(LEGFOCUS_CAMERA_FRAMINGS["third"], action)
                for crop in pose["crop_prompts"].values():
                    self.assertNotIn(crop, action)
                self.assertNotRegex(action, r"不露脸|不拍头脸|头部、脸部和肩(?:膀|部)不入镜|镜面自拍")

        with self.assertRaisesRegex(LegFocusDataError, "第一人称自视"):
            build_leg_focus_action("看看腿 全身 第一人称")

    def test_leg_reference_wrapper_does_not_restore_the_reference_pose_or_crop(self) -> None:
        from astrbot_plugin_selfie_image.core.providers import ImageReference
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_REFERENCE_RULES, build_leg_focus_action,
        )
        from astrbot_plugin_selfie_image.prompts.prompt_composition import build_prompt_with_reference_instruction
        from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt

        reference = ImageReference(data=b"reference")
        action = build_leg_focus_action(has_refs=True, force_camera="selfie")
        for language in ("zh", "en"):
            for existing_language in ("", "zh", "en"):
                with self.subTest(language=language, existing_language=existing_language):
                    raw = action + (LEGFOCUS_REFERENCE_RULES[existing_language] if existing_language else "")
                    prompt = build_prompt_with_reference_instruction(raw, [reference], language=language)
                    self.assertEqual(prompt.count(action), 1)
                    self.assertEqual(sum(prompt.count(rule) for rule in LEGFOCUS_REFERENCE_RULES.values()), 1)
                    self.assertEqual(build_prompt_with_reference_instruction(prompt, [reference], language=language), prompt)
                    built = build_selfie_builtin_prompt(action, language=language, action_content=prompt)
                    self.assertEqual(sum(built.count(rule) for rule in LEGFOCUS_REFERENCE_RULES.values()), 1)
                    self.assertEqual(built.count(action), 1)
                    self.assertNotIn("尽量保持相关人物身份、服装、姿势、场景与构图一致", prompt)
                    self.assertNotIn("while preserving relevant identity, outfit, pose, scene, and composition", prompt)

    def test_leg_pool_rejects_missing_or_invalid_camera_and_reference_contracts(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        for field in ("camera_framings", "reference_rules", "appearance_prompts"):
            for invalid in (None, {}, {"selfie": "", "third": "external"}, {"zh": [], "en": "reference"}):
                with self.subTest(field=field, value=invalid):
                    payload = dict(leg_focus._POOL)
                    payload[field] = invalid
                    with patch.object(leg_focus, "read_versioned_json", side_effect=lambda path, _label: payload if path == leg_focus.DATA_PATH else {"views": leg_focus.LEGFOCUS_VIEW_POOL}):
                        with self.assertRaisesRegex(leg_focus.LegFocusDataError, field):
                            leg_focus._load_pool()

    def test_leg_angle_requests_select_actual_camera_geometry_without_selecting_a_pose(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_VIEW_BY_ID, build_leg_focus_action, parse_requested_leg_pose,
        )

        for query, camera, direction, elevations in (
            ("第三人称 侧拍 仰拍", "third", "side", {"up"}),
            ("第三人称 斜前方 仰拍", "third", "oblique", {"up"}),
            ("第三人称 正前方 仰拍", "third", "front", {"up"}),
            ("第一人称 偏左 俯拍", "selfie", "left", {"down"}),
        ):
            with self.subTest(query=query):
                self.assertEqual(parse_requested_leg_pose(query), "")
                action = build_leg_focus_action(query)
                view = LEGFOCUS_VIEW_BY_ID[re.search(r"【leg_view:([a-z_]+)】", action).group(1)]
                self.assertEqual((view["camera"], view["direction"]), (camera, direction))
                self.assertIn(view["elevation"], elevations)

        for _ in range(12):
            action = build_leg_focus_action("第三人称 不要侧拍")
            view = LEGFOCUS_VIEW_BY_ID[re.search(r"【leg_view:([a-z_]+)】", action).group(1)]
            self.assertNotEqual(view["elevation"], "top")
            if view["elevation"] == "down":
                self.assertIn("【pose:kneeling_relaxed_apart】", action)
                self.assertIn("【leg_support:kneeling_surface】", action)
            self.assertNotEqual(view["direction"], "side")

    def test_leg_incompatible_requests_do_not_silently_change_the_scene_pose_or_person(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import LegFocusDataError, build_leg_focus_action

        for query, options in (
            ("第三人称", {"force_pose": "kneeling_sit_back"}),
            ("第一人称", {"force_pose": "chair_thigh_cross_slant"}),
            ("沙发", {"force_pose": "edge_knees_together_dangle"}),
            ("地毯", {"force_pose": "edge_knees_together_dangle"}),
            ("电竞椅", {"force_pose": "kneeling_upright"}),
            ("第一人称 仰拍", {"force_pose": "kneeling_sit_back"}),
            ("第一人称 侧拍", {}),
            ("第一人称 斜侧拍", {}),
            ("床沿", {"force_pose": "kneeling_sit_back"}),
            ("卧室地毯", {"force_pose": "chair_thigh_cross_slant"}),
            ("电竞椅", {"force_pose": "kneeling_sit_back"}),
            ("第三人称 俯拍", {"force_pose": "edge_knees_together_dangle"}),
            ("第三人称 平拍", {"force_pose": "chair_thigh_cross_slant"}),
            ("第三人称 俯拍", {"force_pose": "kneeling_upright"}),
            ("第三人称 顶拍", {}),
            ("第三人称 背身", {}),
            ("第三人称 仰拍 俯拍", {}),
            ("地毯", {"force_view": "third_side_low"}),
        ):
            with self.subTest(query=query, options=options), self.assertRaises(LegFocusDataError):
                build_leg_focus_action(query, **options)

    def test_leg_pose_can_change_scene_and_view_without_changing_the_action(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_POSE_BY_ID, build_leg_focus_action, compatible_leg_compositions,
        )

        for pose_id in ("chair_thigh_cross_slant", "kneeling_sit_back", "kneeling_upright"):
            pose = LEGFOCUS_POSE_BY_ID[pose_id]
            for scene, support, view in compatible_leg_compositions(pose):
                with self.subTest(pose=pose_id, scene=scene["id"], view=view["id"]):
                    action = build_leg_focus_action(
                        scene["id"], has_refs=True, force_pose=pose_id, force_view=view["id"],
                    )
                    self.assertIn(f"【leg_scene:{scene['id']}】", action)
                    self.assertIn(f"【leg_support:{support}】", action)
                    self.assertIn(pose["prompt"], action)
                    self.assertIn(scene["supports"][support], action)
                    self.assertEqual(sum(other["prompt"] in action for other in LEGFOCUS_POSE_BY_ID.values()), 1)

    def test_leg_fixed_pose_uses_each_compatible_view_before_repeating(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_POSE_BY_ID, build_leg_focus_action, compatible_leg_compositions,
        )

        pose = LEGFOCUS_POSE_BY_ID["chair_thigh_cross_slant"]
        view_ids = {view["id"] for _, _, view in compatible_leg_compositions(pose, scene_ids="gaming_chair")}
        used = set()
        for _ in view_ids:
            action = build_leg_focus_action("电竞椅", force_pose=pose["id"], avoid_combinations=used)
            view_id = re.search(r"【leg_view:([a-z_]+)】", action).group(1)
            self.assertNotIn((pose["id"], view_id), used)
            used.add((pose["id"], view_id))
        self.assertEqual({view_id for _, view_id in used}, view_ids)
        self.assertIn("【pose:chair_thigh_cross_slant】", build_leg_focus_action("电竞椅", force_pose=pose["id"], avoid_combinations=used))

    def test_leg_schema_rejects_unknown_references_and_rear_views(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        for error in ("missing_view", "missing_support", "rear_view", "incompatible_support", "missing_crop", "wrong_crop_camera", "self_side", "self_up", "invalid_scene_camera", "empty_scene_cameras", "invalid_capture_mode", "third_mirror", "empty_capture_modes", "missing_mirror_crop", "missing_mirror_hand"):
            with self.subTest(error=error):
                payload = copy.deepcopy(leg_focus._POOL)
                view_payload = {"views": payload.pop("views")}
                if error == "missing_view":
                    payload["poses"][0]["camera_views"]["third"] = ["does_not_exist"]
                elif error == "missing_support":
                    payload["scenes"][0]["supports"] = {}
                elif error == "rear_view":
                    view_payload["views"][0]["direction"] = "back"
                elif error == "incompatible_support":
                    payload["poses"][0]["support_types"] = ["wide_surface"]
                    payload["poses"][0]["camera_views"]["third"] = ["third_side_low"]
                elif error == "missing_crop":
                    payload["poses"][0].pop("crop_prompts")
                elif error == "wrong_crop_camera":
                    payload["poses"][0]["crop_prompts"] = {"selfie": "waist to feet"}
                elif error == "self_side":
                    view_payload["views"][0]["direction"] = "side"
                elif error == "invalid_scene_camera":
                    payload["scenes"][0]["cameras"] = ["unknown"]
                elif error == "empty_scene_cameras":
                    payload["scenes"][0]["cameras"] = []
                elif error == "invalid_capture_mode":
                    view_payload["views"][0]["capture_mode"] = "unknown"
                elif error == "third_mirror":
                    view = next(view for view in view_payload["views"] if view["camera"] == "third")
                    view["capture_mode"] = "mirror"
                elif error == "empty_capture_modes":
                    payload["scenes"][0]["capture_modes"] = []
                elif error == "missing_mirror_crop":
                    next(pose for pose in payload["poses"] if pose["id"] == "kneeling_upright")["crop_prompts"].pop("mirror")
                elif error == "missing_mirror_hand":
                    next(pose for pose in payload["poses"] if pose["id"] == "kneeling_upright")["hand_prompts"].pop("mirror")
                else:
                    view_payload["views"][0]["elevation"] = "up"
                with patch.object(leg_focus, "read_versioned_json", side_effect=lambda path, _label: payload if path == leg_focus.DATA_PATH else view_payload):
                    with self.assertRaises(leg_focus.LegFocusDataError):
                        leg_focus._load_pool()

    def test_pose_camera_groups_keep_random_selection_inside_valid_combinations(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        expected = set(leg_focus.LEGFOCUS_POSE_BY_ID)
        self.assertFalse(leg_focus.LEGFOCUS_EXPLICIT_ONLY_POSE_IDS)
        for pose_id in expected:
            pose = leg_focus.LEGFOCUS_POSE_BY_ID[pose_id]
            for camera, view_ids in pose["camera_views"].items():
                self.assertTrue(leg_focus.compatible_leg_compositions(pose, camera=camera))
                for view_id in view_ids:
                    action = leg_focus.build_leg_focus_action(
                        force_pose=pose_id, force_camera=camera, force_view=view_id,
                    )
                    self.assertIn(f"【pose:{pose_id}】", action)
                    self.assertIn(f"【cam:{camera}】", action)

    def test_reference_leg_additions_are_in_default_random_candidates(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        for pose_id in ("seated_thigh_gentle_pinch", "seated_self_knees_offset", "kneeling_relaxed_apart"):
            def select_requested(pool):
                self.assertIn(pose_id, {pose["id"] for pose in pool})
                return dict(next(pose for pose in pool if pose["id"] == pose_id))

            with self.subTest(pose=pose_id), patch.object(leg_focus, "_weighted_pose_choice", side_effect=select_requested):
                action = leg_focus.build_leg_focus_action()
                self.assertIn(f"【pose:{pose_id}】", action)
                self.assertTrue(leg_focus.has_leg_focus_contract(action))

    def test_leg_batch_keeps_requested_pose_scene_and_varies_the_concrete_camera(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_POSE_BY_ID, build_leg_focus_action, compatible_leg_compositions,
        )
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin

        plugin = object.__new__(SelfieImagePlugin)
        plugin._normalize_count = int
        plugin._build_selfie_prompt_and_refs_for_event = AsyncMock(side_effect=lambda _event, action, _refs: (action, [], {}))
        plugin._run_image_generation = AsyncMock(return_value={"success": True, "files": []})
        plugin._is_cos_action = lambda _action: False
        plugin._looks_like_group_selfie_intent = lambda _action: False

        async def run_shots(*, total, run_one, **_kwargs):
            for index in range(total):
                await run_one(index)
            return {"success": True}

        plugin._run_counted_generation_shots = run_shots
        request = "椅上叠腿斜伸 电竞椅"
        action = build_leg_focus_action(request, force_pose="chair_thigh_cross_slant")
        count = len({
            view["id"] for _, _, view in compatible_leg_compositions(
                LEGFOCUS_POSE_BY_ID["chair_thigh_cross_slant"], scene_ids="gaming_chair",
            )
        })
        result = asyncio.run(plugin._run_selfie_batches_unlocked(
            task_id="legs-view-test", event=None, action=action, extra_refs=[],
            source="command-look-legs", requested_count=count, aspect="9:16",
            resolution="1K", fail_label="失败", rebuild_extra_request=request,
        ))
        self.assertTrue(result["success"])
        requests = [call.kwargs["original_prompt"] for call in plugin._run_image_generation.await_args_list]
        self.assertEqual(len(requests), count)
        self.assertEqual(len({re.search(r"【leg_view:([a-z_]+)】", prompt).group(1) for prompt in requests}), count)
        for prompt in requests:
            self.assertIn("【pose:chair_thigh_cross_slant】", prompt)
            self.assertIn("【leg_scene:gaming_chair】", prompt)
        for call in plugin._run_image_generation.await_args_list:
            self.assertEqual(call.args[0], call.kwargs["original_prompt"])

    def test_leg_legacy_contract_is_rebuilt_without_repeating_its_old_furniture_or_camera(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_FRAMING, build_leg_focus_action, ensure_leg_focus_action,
        )

        action = build_leg_focus_action(has_refs=True, force_pose="kneeling_sit_back", force_camera="selfie")
        legacy = re.sub(r"【leg_(?:view|scene|support):[a-z_]+】", "", action)
        rebuilt = ensure_leg_focus_action(legacy, True)
        self.assertEqual(rebuilt.count(LEGFOCUS_FRAMING), 1)
        self.assertIn("【leg_view:", rebuilt)
        self.assertIn("【pose:kneeling_sit_back】", rebuilt)
        self.assertIn("【cam:selfie】", rebuilt)
        self.assertIn("【outfit:reference】", rebuilt)

    def test_leg_user_supplement_does_not_include_random_pose_scene_or_view_tags(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import build_leg_focus_action
        from astrbot_plugin_selfie_image.features.persona import extract_user_extra_text
        from astrbot_plugin_selfie_image.prompts.prompt_templates import extract_user_prompt

        action = build_leg_focus_action("第三人称 侧拍 仰拍 卧室软凳")
        for extract in (extract_user_extra_text, extract_user_prompt):
            extra = extract(action)
            self.assertEqual(extra, "第三人称 侧拍 仰拍 卧室软凳")
            self.assertNotIn("【", extra)

    def test_leg_command_explains_conflicting_constraints_before_starting_generation(self) -> None:
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin

        plugin = object.__new__(SelfieImagePlugin)
        plugin._extract_command_count = lambda text, **_kwargs: (text, 1)
        plugin._expand_dynamic_preset_variants = lambda *_args: ([], "", "", "")
        plugin._expand_user_text_with_preset = lambda text: (text, "", "", "")
        plugin._handle_selfie_command = AsyncMock(side_effect=AssertionError("must not start generation"))
        event = types.SimpleNamespace(plain_result=lambda text: text)

        async def invoke():
            return [item async for item in plugin.cmd_look_legs(event)]

        with patch("astrbot_plugin_selfie_image.main.extract_command_message", return_value="第一人称 仰拍"), patch(
            "astrbot_plugin_selfie_image.main.extract_image_sources_from_event", return_value=[],
        ):
            output = asyncio.run(invoke())
        self.assertEqual(len(output), 1)
        self.assertIn("不兼容", output[0])
        plugin._handle_selfie_command.assert_not_called()

    def test_bare_leg_detail_is_written_once_with_one_dedicated_outfit(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import LEGFOCUS_OUTFIT_POOL, build_leg_focus_action

        detail = {
            "id": "bare_legs",
            "title": "自然光腿",
            "prompt": "自然肤色光腿，皮肤保留自然纹理，不增加袜装或鞋履。",
            "weight": 2,
        }
        with patch(
            "astrbot_plugin_selfie_image.cos.leg_focus._weighted_detail_choice",
            return_value=detail,
        ):
            action = build_leg_focus_action(force_pose="kneeling_sit_back", force_camera="selfie")

        self.assertEqual(action.count(detail["prompt"]), 1)
        self.assertEqual(sum(item["prompt"] in action for item in LEGFOCUS_OUTFIT_POOL), 1)
        self.assertIn("【wear:bare】", action)
        self.assertNotIn("日程", action)
        self.assertNotIn("来源优先级", action)

    def test_hosiery_styles_follow_outfit_compatibility_and_explicit_requests(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_HOSIERY_STYLES, LEGFOCUS_OUTFIT_POOL, build_leg_focus_action,
        )

        for outfit in LEGFOCUS_OUTFIT_POOL:
            for wear in ("黑丝", "白丝", "连裤袜", "赤足"):
                with self.subTest(outfit=outfit["id"], wear=wear), patch(
                    "astrbot_plugin_selfie_image.cos.leg_focus.pick_leg_focus_outfit", return_value=outfit,
                ):
                    action = build_leg_focus_action(wear, force_pose="kneeling_sit_back", force_camera="selfie")
                    styles = [style for style in LEGFOCUS_HOSIERY_STYLES if style["prompt"] in action]
                    self.assertEqual(action.count(outfit["prompt"]), 1)
                    self.assertIn(f"【outfit:{outfit['id']}】", action)
                    self.assertEqual(len(styles), 0 if wear == "赤足" else 1)
                    for style in styles:
                        self.assertIn(style["family"], outfit["hosiery_families"])
                        if wear == "连裤袜":
                            self.assertEqual(style["kind"], "tights")
                        else:
                            self.assertEqual(style["wear"], wear)
        for style in LEGFOCUS_HOSIERY_STYLES:
            for alias in style["aliases"]:
                with self.subTest(style=style["id"], alias=alias):
                    action = build_leg_focus_action(alias)
                    self.assertEqual(action.count(style["prompt"]), 1)
                    selected = [outfit for outfit in LEGFOCUS_OUTFIT_POOL if outfit["prompt"] in action]
                    self.assertEqual(len(selected), 1)
                    self.assertIn(style["family"], selected[0]["hosiery_families"])

    def test_outfit_pool_is_json_owned_and_contains_no_source_policy_prompt(self) -> None:
        from astrbot_plugin_selfie_image.core.prompt_data import read_versioned_json
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            OUTFIT_DATA_PATH, LEGFOCUS_OUTFIT_POOL, LEGFOCUS_HOSIERY_STYLES,
        )

        payload = read_versioned_json(OUTFIT_DATA_PATH, "test")
        self.assertNotIn("source_rule", payload)
        self.assertEqual(payload["outfits"], LEGFOCUS_OUTFIT_POOL)
        self.assertEqual(payload["hosiery_styles"], LEGFOCUS_HOSIERY_STYLES)

    def test_leg_outfit_capture_rules_keep_skirts_off_front_cameras(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        for outfit in leg_focus.LEGFOCUS_OUTFIT_POOL:
            with self.subTest(outfit=outfit["id"]):
                action = leg_focus.build_leg_focus_action(
                    force_pose="edge_knees_together_dangle", force_outfit=outfit["id"],
                )
                view_id = re.search(r"【leg_view:([a-z_]+)】", action).group(1)
                view = leg_focus.LEGFOCUS_VIEW_BY_ID[view_id]
                self.assertEqual(view["elevation"], "up")
                self.assertEqual(action.count(outfit["prompt"]), 1)
                self.assertTrue(leg_focus.has_leg_focus_contract(action))
                if outfit["capture_profile"] == "skirt":
                    self.assertIn(view["direction"], {"oblique", "side"})
                    with self.assertRaisesRegex(leg_focus.LegFocusDataError, "不兼容"):
                        leg_focus.build_leg_focus_action(
                            force_pose="edge_knees_together_dangle", force_outfit=outfit["id"],
                            force_view="third_front_low",
                        )
                else:
                    front = leg_focus.build_leg_focus_action(
                        force_pose="edge_knees_together_dangle", force_outfit=outfit["id"],
                        force_view="third_front_low",
                    )
                    self.assertIn(f"【outfit:{outfit['id']}】", front)
                    self.assertIn("【leg_view:third_front_low】", front)
                self_view = leg_focus.build_leg_focus_action(
                    force_pose="kneeling_sit_back", force_camera="selfie", force_outfit=outfit["id"],
                )
                self.assertIn(f"【outfit:{outfit['id']}】", self_view)
                self.assertIn("【cam:selfie】", self_view)

    def test_leg_outfit_filter_does_not_fall_back_to_an_incompatible_skirt(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        compositions = leg_focus.compatible_leg_compositions(
            leg_focus.LEGFOCUS_POSE_BY_ID["edge_knees_together_ankles_apart"],
        )
        skirts = [outfit for outfit in leg_focus.LEGFOCUS_OUTFIT_POOL if outfit["capture_profile"] == "skirt"]
        with patch.object(leg_focus, "LEGFOCUS_OUTFIT_POOL", skirts):
            with self.assertRaisesRegex(leg_focus.LegFocusDataError, "没有兼容"):
                leg_focus.pick_leg_focus_outfit(compositions=compositions, avoid_id=skirts[0]["id"])
        action = leg_focus.build_leg_focus_action(force_pose="edge_knees_together_ankles_apart")
        outfit_id = re.search(r"【outfit:([a-z_]+)】", action).group(1)
        self.assertEqual(leg_focus.get_leg_focus_outfit(outfit_id)["capture_profile"], "shorts")

    def test_leg_outfit_schema_rejects_invalid_capture_rules(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        for error in ("missing_profile", "unknown_profile", "missing_coverage", "invalid_direction", "invalid_elevation"):
            with self.subTest(error=error):
                payload = copy.deepcopy(leg_focus._OUTFITS)
                if error == "missing_profile":
                    payload["outfits"][0].pop("capture_profile")
                elif error == "unknown_profile":
                    payload["outfits"][0]["capture_profile"] = "unknown"
                elif error == "missing_coverage":
                    payload["capture_profiles"]["skirt"].pop("coverage_prompt")
                elif error == "invalid_direction":
                    payload["capture_profiles"]["skirt"]["third_directions"] = ["rear"]
                else:
                    payload["capture_profiles"]["skirt"]["third_elevations"] = ["unknown"]
                with patch.object(leg_focus, "read_versioned_json", return_value=payload):
                    with self.assertRaises(leg_focus.LegFocusDataError):
                        leg_focus._load_outfit_pool()

    def test_canvas_leg_outfits_distinguish_persona_from_uploaded_or_parent_images(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import LEGFOCUS_OUTFIT_POOL, build_leg_focus_action
        from astrbot_plugin_selfie_image.features.persona import PersonaManager
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin
        from astrbot_plugin_selfie_image.studio.studio_adapter import StudioMixin

        for source in ("persona", "uploaded", "parent"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                session = {
                    "id": "legs-canvas", "template": "selfie",
                    "graph": {"mode": "selfie", "prompt": "看看腿", "count": 1},
                    "slots": [{"id": "identity", "role": "identity", "image_path": "upload.png" if source == "uploaded" else ""}],
                    "last_run": {"summary": {"reference_node_id": "parent" if source == "parent" else ""}},
                    "canvas": {"nodes": [{"id": "parent", "result": {"media_path": "parent.png"}}]},
                }
                state = {"status": "queued", "record_ids": []}
                stub = object.__new__(StudioMixin)
                stub.config = types.SimpleNamespace(
                    image_default_aspect_ratio="9:16", image_default_resolution="1K", image_enable_image_prompt_en=False,
                )
                stub.studio = types.SimpleNamespace(
                    get=lambda _session_id: session, attach_run_finish=lambda *_args, **_kwargs: None,
                )
                stub.persona = PersonaManager(tmp)
                stub.persona.get_reference_image = lambda: {"data": b"persona", "mime_type": "image/png"}
                stub.persona.ensure_daily_selfie_profile = AsyncMock(side_effect=AssertionError("legs must not load the schedule"))
                stub._build_leg_focus_action = build_leg_focus_action
                stub._normalize_selfie_action = SelfieImagePlugin._normalize_selfie_action.__get__(stub)
                stub._task_cancel_requested = lambda _task_id: False
                stub._web_task_timestamp = lambda: "now"
                stub._set_web_image_task = lambda _task_id, **fields: state.update(fields)
                stub.get_web_image_task = lambda _task_id: dict(state)
                stub._wait_for_record_commits = AsyncMock()
                stub._load_cache_image_bytes = lambda _path: (b"uploaded", "image/png")
                stub._prompt_en_needed = lambda *_args, **_kwargs: False
                stub._record_task = lambda _record: None
                stub._run_image_generation = AsyncMock(return_value={"success": True, "files": ["out.png"], "image_paths": ["out.png"]})

                asyncio.run(stub._run_studio_task("web-studio-legs", session["id"]))

                self.assertEqual(state["status"], "succeeded", state)
                action = state["request_data"]["original_prompt"]
                if source == "persona":
                    self.assertEqual(sum(item["prompt"] in action for item in LEGFOCUS_OUTFIT_POOL), 1)
                else:
                    self.assertIn("【outfit:reference】", action)
                    self.assertFalse(any(item["prompt"] in action for item in LEGFOCUS_OUTFIT_POOL))
                stub.persona.ensure_daily_selfie_profile.assert_not_awaited()

    def test_leg_prompt_templates_defer_to_the_generated_action_contract(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import build_leg_focus_action
        from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt

        action = build_leg_focus_action(
            force_pose="kneeling_sit_back",
            force_camera="selfie",
            has_refs=True,
        )
        prompt_zh = build_selfie_builtin_prompt(action, has_reference_image=True)
        prompt_en = build_selfie_builtin_prompt(
            action,
            language="en",
            has_reference_image=True,
            action_content="Close indoor lower-body outfit photo. Keep the selected pose, camera, scene, and clothing unchanged.",
        )

        self.assertIn(action, prompt_zh)
        self.assertIn("头部、脸部和肩部不入镜", prompt_zh)
        self.assertIn("胸部可随姿势自然保留", prompt_zh)
        self.assertNotIn("全身镜前自拍", prompt_zh)
        self.assertNotIn("日常长裤搭配", prompt_zh)
        self.assertIn("Close indoor lower-body outfit photo", prompt_en)
        self.assertNotIn("full-length everyday trousers", prompt_en)
        self.assertNotIn("look at the camera", prompt_en.lower())

    def test_full_outfit_leg_contract_does_not_inherit_generic_gaze_or_crop(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import build_leg_focus_action
        from astrbot_plugin_selfie_image.features.persona import PersonaManager
        from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt

        action = build_leg_focus_action(
            force_pose="chair_thigh_cross_slant",
            force_camera="third",
            force_framing="full_outfit",
        )
        with tempfile.TemporaryDirectory() as tmp:
            prompt_zh = PersonaManager(tmp).build_selfie_prompt(
                action,
                "心酱",
                "",
                has_reference_image=True,
            )
        prompt_en = build_selfie_builtin_prompt(
            action,
            language="en",
            has_reference_image=True,
            action_content="Full-length indoor outfit contract.",
        )

        self.assertIn("【frame:full_outfit】", action)
        self.assertIn("头部到足部完整入镜", prompt_zh)
        self.assertEqual(prompt_zh.count(action), 1)
        self.assertNotIn("默认使用近景下半身裁切", prompt_zh)
        self.assertNotIn("正对镜头时自然看向镜头", prompt_zh)
        self.assertNotIn("腰线至鞋履", prompt_zh)
        self.assertIn("Full-length indoor outfit contract.", prompt_en)
        self.assertNotIn("waist to shoes", prompt_en.lower())

    def test_leg_contract_selfie_does_not_reintroduce_legacy_foot_framing(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import build_leg_focus_action
        from astrbot_plugin_selfie_image.features.persona import PersonaManager

        action = build_leg_focus_action(
            force_pose="kneeling_sit_back",
            force_view="self_forward_down",
            force_legwear="赤足",
        )
        with tempfile.TemporaryDirectory() as tmp:
            manager = PersonaManager(tmp)
            prompt = manager.build_selfie_prompt(
                action,
                bot_name="心酱",
                personality="",
                has_reference_image=True,
            )

        self.assertEqual(prompt.count("第一人称主观视角"), 1)
        self.assertNotIn("腰线至足部照片", prompt)
        self.assertEqual(prompt.count(action), 1)

    def test_english_leg_prompt_uses_one_action_contract_without_legacy_pose_or_outfit(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import build_leg_focus_action
        from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt

        action = build_leg_focus_action(
            force_pose="kneeling_sit_back",
            force_camera="selfie",
            force_legwear="赤足",
        )
        translated_contract = "Translated indoor leg action contract."
        prompt = build_selfie_builtin_prompt(
            action,
            language="en",
            has_reference_image=True,
            action_content=translated_contract,
        )

        self.assertEqual(prompt.count(translated_contract), 1)
        self.assertNotIn("full-length everyday trousers", prompt)
        self.assertNotIn("opaque skin-tone full-length legwear", prompt)
        self.assertNotIn("waist to feet", prompt)

    def test_english_group_prompt_keeps_the_selected_composition_contract(self) -> None:
        from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt
        from astrbot_plugin_selfie_image.prompts.selfie_actions import build_group_selfie_action

        action = build_group_selfie_action("背面合影")
        translated_contract = "Translated back-facing group composition."
        prompt = build_selfie_builtin_prompt(
            action,
            language="en",
            action_content=translated_contract,
        )

        self.assertEqual(prompt.count(translated_contract), 1)
        self.assertNotIn("vertical half-body scene", prompt)
        self.assertNotIn("smartphone selfie composition", prompt)

    def test_english_cos_prompt_does_not_append_generic_half_body_or_eye_contact(self) -> None:
        from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt

        contract = "Rear-facing COS portrait with the head aligned to the shoulders."
        prompt = build_selfie_builtin_prompt(
            "看看COS【cam:third】【cos_pose:back_to_camera】",
            language="en",
            has_reference_image=True,
            appearance_type="real",
            action_content=contract,
        )

        self.assertEqual(prompt.count(contract), 1)
        self.assertNotIn("vertical close half-body everyday photo", prompt)
        self.assertNotIn("keep natural focused eye contact", prompt.lower())

    def test_batch_failure_remaining_uses_completed_images_not_failed_index(self) -> None:
        from astrbot_plugin_selfie_image.generation.generation_results import batch_failure_text

        message = batch_failure_text(
            index=4,
            total=10,
            done_files=9,
            error="模型超时（180s）",
            mode="stop",
            skipped=1,
            skip_max=2,
            will_continue=False,
        )

        self.assertIn("第 4/10 张没出成", message)
        self.assertIn("已出 9 张", message)
        self.assertIn("后面 1 张先不跑了", message)
        self.assertNotIn("后面 6 张", message)

    def test_automatic_aspect_defaults_to_portrait_provider_dimensions(self) -> None:
        from astrbot_plugin_selfie_image.core.providers import (
            ImageGenerateRequest,
            _aspect_orientation,
            map_aspect_ratio_to_gpt_image_size,
            map_aspect_ratio_to_nai_size,
            map_aspect_ratio_to_openai_size,
        )

        self.assertEqual(ImageGenerateRequest(prompt="portrait").aspect_ratio, "9:16")
        self.assertEqual(_aspect_orientation("自动"), "portrait")
        self.assertEqual(_aspect_orientation(""), "portrait")
        self.assertEqual(_aspect_orientation("auto"), "portrait")
        self.assertEqual(map_aspect_ratio_to_openai_size("自动"), "1024x1792")
        self.assertEqual(map_aspect_ratio_to_gpt_image_size("自动"), "1024x1536")
        self.assertEqual(map_aspect_ratio_to_gpt_image_size("auto"), "1024x1536")
        self.assertEqual(map_aspect_ratio_to_nai_size("自动"), (832, 1216))

    def test_daily_outdoor_scene_does_not_leak_into_leg_prompt(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import build_leg_focus_action
        from astrbot_plugin_selfie_image.features.persona import PersonaManager

        action = build_leg_focus_action(
            has_refs=True,
            preferred_scene="城市天台步道",
            force_pose="kneeling_sit_back",
            force_camera="selfie",
        )
        with tempfile.TemporaryDirectory() as tmp:
            prompt = PersonaManager(tmp).build_selfie_prompt(
                action,
                "小助",
                "温柔",
                True,
                1,
                has_external_reference=True,
            )

        self.assertIn("保留原有上装、下装、连体结构、袜鞋与配饰", prompt)
        self.assertIn("【cam:selfie】", prompt)
        self.assertIn("【leg_support:kneeling_surface】", prompt)
        self.assertNotIn("城市天台步道", prompt)
        self.assertNotIn("当前日程场景", prompt)


if __name__ == "__main__":
    unittest.main()
