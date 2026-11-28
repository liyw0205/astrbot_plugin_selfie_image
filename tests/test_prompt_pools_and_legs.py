from __future__ import annotations

import asyncio
import copy
import json
import random
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


def _leg_pool_payload_for_path(module, path, payload, view_payload=None):
    split = {
        module.POSE_DATA_PATH: "poses",
        module.SCENE_DATA_PATH: "scenes",
        module.SCENE_STATE_DATA_PATH: "scene_states",
        module.CAPTURE_STYLE_DATA_PATH: "capture_styles",
    }
    if path == module.DATA_PATH:
        return payload
    if path == module.VIEW_DATA_PATH:
        return view_payload or {"views": payload.get("views", [])}
    if path == module.SCENE_STATE_DATA_PATH:
        return {
            "scene_state_probability": payload.get("scene_state_probability", 0.3),
            "scene_states": payload.get("scene_states", []),
        }
    if path in split:
        key = split[path]
        return {key: payload.get(key, [])}
    return {}


def _leg_outfit_payload_for_path(module, path, payload):
    rule_paths = {
        module.PRESERVE_PROMPTS_DATA_PATH: "preserve_prompts",
        module.POOL_REFERENCE_PROMPTS_DATA_PATH: "pool_reference_prompts",
        module.WEAR_PROMPTS_DATA_PATH: "wear_prompts",
        module.WEAR_LABELS_DATA_PATH: "wear_labels",
    }
    if path in rule_paths:
        key = rule_paths[path]
        return {"version": 1, key: payload.get(key)}
    categories = {
        module.OUTFIT_DATA_PATH: "outfits",
        module.CAPTURE_PROFILE_DATA_PATH: "capture_profiles",
        module.HOSIERY_STYLE_DATA_PATH: "hosiery_styles",
        module.LEG_ACCESSORY_DATA_PATH: "leg_accessories",
        module.RANDOM_LEG_DETAIL_DATA_PATH: "random_leg_details",
    }
    if path in categories:
        key = categories[path]
        return {"version": 1, key: payload.get(key)}
    return {}


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
    def test_leg_json_files_have_one_category_each(self) -> None:
        data_dir = Path(__file__).resolve().parents[1] / "data" / "legs"
        expected = {
            "prompt_rules.json": {"version", "proportion_guard", "framing", "anatomy_rule", "hosiery_rule", "camera_framings", "full_framing", "crop_modes", "reference_rules", "scene_rule", "appearance_prompts"},
            "poses.json": {"version", "poses"},
            "scenes.json": {"version", "scenes"},
            "scene_states.json": {"version", "scene_state_probability", "scene_states"},
            "capture_styles.json": {"version", "capture_styles"},
            "views.json": {"version", "views"},
            "outfits.json": {"version", "outfits"},
            "capture_profiles.json": {"version", "capture_profiles"},
            "preserve_prompts.json": {"version", "preserve_prompts"},
            "pool_reference_prompts.json": {"version", "pool_reference_prompts"},
            "wear_prompts.json": {"version", "wear_prompts"},
            "wear_labels.json": {"version", "wear_labels"},
            "hosiery_styles.json": {"version", "hosiery_styles"},
            "leg_accessories.json": {"version", "leg_accessories"},
            "random_leg_details.json": {"version", "random_leg_details"},
            "combo_presets.json": {"version", "combos"},
        }
        self.assertEqual({path.name for path in data_dir.glob("*.json")}, set(expected))
        for filename, fields in expected.items():
            with self.subTest(filename=filename):
                self.assertEqual(set(json.loads((data_dir / filename).read_text(encoding="utf-8"))), fields)

    def test_leg_pose_support_matches_the_selected_scene(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import LEGFOCUS_POSE_BY_ID, build_leg_focus_action, compatible_leg_compositions

        self.assertEqual(set(LEGFOCUS_POSE_BY_ID), {
            "sofa_side_sit_knees_together",
            "chair_thigh_cross_slant",
            "edge_knees_together_dangle", "edge_knees_together_ankles_apart",
            "kneeling_sit_back", "kneeling_upright",
            "bed_supine_knees_together",
            "edge_knees_together_slant", "seated_knees_offset", "seated_ankles_softly_crossed",
            "reclined_same_side_knees", "standing_weight_shift",
            "seated_thigh_gentle_pinch", "kneeling_relaxed_apart",
            "standing_ankles_cross", "standing_front_toe_touch", "standing_side_staggered",
            "standing_chair_single_leg_back", "standing_one_foot_on_stool", "standing_supported_tiptoe",
            "supine_parallel_extended", "supine_ankles_cross", "supine_one_knee_up", "supine_knees_offset",
            "side_lying_knees_bent", "side_lying_top_knee_forward", "side_lying_legs_staggered",
            "prone_legs_extended", "prone_single_knee_bent", "prone_both_knees_bent",
            "reclined_parallel_extended", "reclined_one_knee_up", "reclined_feet_on_stool",
            "kneeling_side_sit", "kneeling_single_knee_grounded", "kneeling_sit_forward_lean",
            "mirror_side_sit_shins_overlap", "mirror_seated_knees_apart_feet_together",
            "chair_self_parallel_knees_offset",
        })
        for pose in LEGFOCUS_POSE_BY_ID.values():
            combinations = compatible_leg_compositions(pose)
            self.assertTrue(combinations, pose["id"])
            if pose["id"] == "kneeling_single_knee_grounded":
                self.assertEqual(set(pose["scene_ids"]), {"indoor_desk", "indoor_flat_ground"})
            else:
                self.assertNotIn("scene_ids", pose)
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
        self.assertIn("一手在上身旁持机向下拍摄并留在画外", action)
        self.assertIn("第一人称自拍机位由人物手持手机拍摄", action)
        for pose_id in ("edge_knees_together_dangle", "supine_parallel_extended", "side_lying_knees_bent"):
            with self.subTest(support_pose=pose_id):
                supported = build_leg_focus_action(force_pose=pose_id)
                self.assertIn("承托位置硬约束：", supported)
                self.assertIn("关节链硬约束：", supported)
                self.assertIn("重力方向硬约束：", supported)

    def test_leg_pose_risk_gating_and_positive_language(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        high_risk_ids = {
            "chair_thigh_cross_slant",
            "seated_ankles_softly_crossed",
            "mirror_side_sit_shins_overlap",
            "mirror_seated_knees_apart_feet_together",
            "standing_ankles_cross",
            "supine_ankles_cross",
            "side_lying_top_knee_forward",
            "kneeling_side_sit",
            "kneeling_single_knee_grounded",
            "side_lying_knees_bent",
            "prone_both_knees_bent",
        }
        self.assertEqual(
            {pose["id"] for pose in leg_focus.LEGFOCUS_POSE_POOL if pose.get("risk") == "high"},
            high_risk_ids,
        )
        for pose_id in high_risk_ids:
            action = leg_focus.build_leg_focus_action(force_pose=pose_id)
            self.assertIn(f"【pose:{pose_id}】", action)

        pose_choices = [
            {"id": "ordinary", "weight": 2},
            {"id": "risky", "weight": 1, "risk": "high"},
        ]
        with patch.object(leg_focus.random, "choices", return_value=[pose_choices[0]]) as choose:
            leg_focus._weighted_pose_choice(pose_choices)
        self.assertEqual(choose.call_args.kwargs["weights"], [2, 0.15])

        pose = leg_focus.LEGFOCUS_POSE_BY_ID["edge_knees_together_dangle"]
        with patch.object(leg_focus.random, "choices", return_value=[pose["detail_variants"][0]]) as choose:
            leg_focus._weighted_pose_detail_choice(pose, camera="third", capture_mode="direct")
        self.assertEqual(choose.call_args.kwargs["weights"], [2])

        forbidden_pose_negations = re.compile(
            r"(?:不|别|避免|不要|不得|禁止)[^，。；]{0,8}"
            r"(?:扭转|翻折|翻转|交叉|交叠|交缠|缠绕|悬空|绷直|勾起|劈叉|反弓)"
        )
        for pose in leg_focus.LEGFOCUS_POSE_POOL:
            text = " ".join((
                pose["prompt"], *pose["crop_prompts"].values(),
                *(variant["prompt"] for variant in pose.get("detail_variants", [])),
            ))
            self.assertIsNone(forbidden_pose_negations.search(text), pose["id"])

        white_action = leg_focus.build_leg_focus_action("白丝 全身", force_pose="seated_knees_offset")
        self.assertIn("双腿各自沿同侧髋、膝、踝连续", white_action)
        self.assertLess(white_action.index(leg_focus.LEGFOCUS_ANATOMY_RULE), white_action.index(leg_focus.LEGFOCUS_HOSIERY_RULE))
        self.assertIn("脸部、手部、腿部肤色一致", white_action)
        self.assertIn("白色只属于袜料纤维，不覆盖皮肤", white_action)
        self.assertNotRegex(leg_focus.LEGFOCUS_WEAR_PROMPTS["白丝"], r"漂白|提白")
        white_sheer = next(item for item in leg_focus.LEGFOCUS_HOSIERY_STYLES if item["id"] == "white_sheer")
        self.assertNotRegex(white_sheer["prompt"], r"漂白|提白")
        for pose in leg_focus.LEGFOCUS_POSE_POOL:
            for prompt in pose.get("hand_prompts", {}).values():
                self.assertNotRegex(prompt, r"颜色|肤色|白色|黑色|红色|灰色|冷调")

    def test_grounded_kneeling_keeps_crop_support_outfit_and_proportions_aligned(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        pose_id = "kneeling_single_knee_grounded"
        knee_outfits = {"sport_shorts_set", "tee_cargo_shorts", "knit_camisole_shorts_set"}
        self.assertEqual(
            {item["id"] for item in leg_focus._compatible_leg_outfits(posture="kneeling")},
            knee_outfits,
        )
        for _ in range(20):
            action = leg_focus.build_leg_focus_action(force_pose=pose_id)
            face_mode = re.search(r"【face_mode:(none|half|full)】", action).group(1)
            if face_mode == "none":
                self.assertIn("【crop_mode:shoulder_crop】", action)
            else:
                self.assertIn(f"【crop_mode:{face_mode}_face】", action)
                self.assertIn("本次神态按 COS 表情模板执行", action)
            self.assertNotIn("【crop_mode:leg_full】", action)
            self.assertIn("【leg_support:flat_ground】", action)
            self.assertIn("即便画面只出现大腿到脚", action)
            self.assertIn("左膝与右脚掌直接落在同一块平整硬质地面", action)
            self.assertIn("【outfit:", action)
            self.assertRegex(action, r"【outfit:(?:sport_shorts_set|tee_cargo_shorts|knit_camisole_shorts_set)】")
            self.assertTrue(leg_focus.has_leg_focus_contract(action))
            self.assertFalse(leg_focus.has_leg_focus_contract(
                re.sub(r"【crop_mode:[a-z_]+】", "【crop_mode:leg_full】", action)
            ))

        forced_crop = leg_focus.build_leg_focus_action(
            force_pose=pose_id, force_view="third_kneeling_oblique_down", force_crop_mode="leg_full",
        )
        self.assertIn("【crop_mode:shoulder_crop】", forced_crop)
        self.assertIn("【angle:pitch=30,yaw=45】", forced_crop)
        with self.assertRaisesRegex(leg_focus.LegFocusDataError, "不兼容"):
            leg_focus.build_leg_focus_action(
                force_pose="standing_weight_shift", force_outfit="sport_shorts_set",
            )

        shorts = leg_focus.LEGFOCUS_OUTFIT_BY_ID["tee_cargo_shorts"]
        ankle_style = next(style for style in leg_focus.LEGFOCUS_HOSIERY_STYLES if style["id"] == "black_ankle_socks")
        with patch.object(leg_focus.random, "choices", return_value=[ankle_style]) as choose:
            leg_focus._pick_hosiery_style(
                "黑丝", shorts, pose=leg_focus.LEGFOCUS_POSE_BY_ID[pose_id],
            )
        weights = choose.call_args.kwargs["weights"]
        self.assertIn(ankle_style["weight"] * 0.15, weights)

    def test_accessory_constraints_are_added_and_bulk_and_stacking_are_enforced(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        outfit = leg_focus.LEGFOCUS_OUTFIT_POOL[0]
        sheer = next(style for style in leg_focus.LEGFOCUS_HOSIERY_STYLES if style["id"] == "white_sheer")
        opaque = next(style for style in leg_focus.LEGFOCUS_HOSIERY_STYLES if style["id"] == "white_opaque")
        pose = leg_focus.LEGFOCUS_POSE_BY_ID["standing_weight_shift"]
        ring = leg_focus.LEGFOCUS_ACCESSORY_BY_ID["thigh_double_strap_ring"]
        self.assertEqual(
            [item["id"] for item in leg_focus._pick_leg_accessory("白丝", outfit, sheer, pose, "加金属腿环")],
            ["thigh_double_strap_ring"],
        )
        self.assertEqual(
            [item["id"] for item in leg_focus._pick_leg_accessory("白丝", outfit, opaque, pose, "加金属腿环")],
            ["thigh_double_strap_ring"],
        )
        prompt = leg_focus._leg_accessory_prompt(ring, "白丝", opaque)
        for phrase in ("紧贴袜料", "宽度不超过约两指", "保留该款指定的环带配色", "不勒入皮肤", ring["title"]):
            self.assertIn(phrase, prompt)
        for style in leg_focus.LEGFOCUS_HOSIERY_STYLES:
            self.assertNotRegex(style["prompt"], r"(?:不加|不增加|不要|不出现)[^。；]*腿环")

        with patch.object(leg_focus.random, "random", return_value=0.9), patch.object(
            leg_focus.random, "choices", side_effect=lambda pool, **_kwargs: [pool[0]],
        ) as choose:
            default = leg_focus._pick_leg_accessory("光腿", outfit, {}, pose)
        self.assertEqual(len(default), 1)
        self.assertTrue(all(item["risk"] != "high" for item in choose.call_args.args[0]))
        combo_action = leg_focus.build_leg_focus_action("combo_06")
        self.assertIn("【legwear_category:ankle_sock】", combo_action)
        self.assertNotIn("【leg_accessory:garter_over_thighhigh", combo_action)
        self.assertTrue(leg_focus.has_leg_focus_contract(combo_action))

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
        self.assertGreaterEqual(len(BUILTIN_PROMPTS), 88)
        from astrbot_plugin_selfie_image.studio.studio import ACTION_PROMPT_PRESET_IDS
        for preset_id in (
            "preset_social_hotel_breakfast",
            "preset_social_elevator_lipstick",
            "preset_social_movie_night",
        ):
            self.assertIn(preset_id, ACTION_PROMPT_PRESET_IDS)
            self.assertTrue(any(item["id"] == preset_id for item in BUILTIN_PROMPTS))
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
            LEGFOCUS_POSE_POOL, LEGFOCUS_SCENE_STATE_POOL,
            LEGFOCUS_VIEW_BY_ID,
            build_leg_focus_action,
            compatible_leg_compositions,
        )

        self.assertEqual(len(LEGFOCUS_POSE_POOL), 39)
        for pose in LEGFOCUS_POSE_POOL:
            if pose["id"] == "kneeling_single_knee_grounded":
                self.assertEqual(set(pose["scene_ids"]), {"indoor_desk", "indoor_flat_ground"})
            else:
                self.assertNotIn("scene_ids", pose)
            self.assertIn("camera_views", pose)
            scene_ids = {scene["id"] for scene, _, _ in compatible_leg_compositions(pose)}
            if pose["id"] == "standing_one_foot_on_stool":
                self.assertEqual(scene_ids, {"chair_footstool"})
            elif pose["id"] == "reclined_feet_on_stool":
                self.assertEqual(scene_ids, {"sofa_footstool"})
            elif pose["id"] in {"mirror_side_sit_shins_overlap", "mirror_seated_knees_apart_feet_together"}:
                self.assertEqual(scene_ids, {"floor_mirror"})
            elif pose["id"] == "sofa_side_sit_knees_together":
                self.assertIn("sofa", scene_ids)
                self.assertGreater(len(scene_ids), 1)
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
                    selected_scene = re.search(r"本张(室内|室外)场景：([^\n]+)", action)
                    self.assertIsNotNone(selected_scene)
                    if selected_scene.group(1) == "室内":
                        self.assertNotIn("户外", selected_scene.group(2))
                    else:
                        selected_state = re.search(r"【leg_state:([a-z_]+)】", action)
                        prompt_states = [state for state in LEGFOCUS_SCENE_STATE_POOL if state["prompt"] in action]
                        self.assertLessEqual(len(prompt_states), 1)
                        self.assertEqual(bool(selected_state), bool(prompt_states))
                        if not selected_state:
                            for phrase in ("干爽", "水珠", "水痕", "汗湿"):
                                self.assertNotIn(phrase, action)

    def test_removed_awkward_pose_is_not_available(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_POSE_BY_ID, build_leg_focus_action, compatible_leg_compositions,
            parse_requested_leg_pose,
        )

        self.assertNotIn("chair_parallel_legs_slant", LEGFOCUS_POSE_BY_ID)
        self.assertEqual(parse_requested_leg_pose("椅上并膝同向斜放"), "")
        replacement = build_leg_focus_action("椅上并膝同向斜放")
        self.assertNotIn("【pose:chair_parallel_legs_slant】", replacement)
        self.assertNotIn("chair_one_foot_on_stool", LEGFOCUS_POSE_BY_ID)
        self.assertEqual(parse_requested_leg_pose("椅上单脚搭低脚凳"), "")
        replacement = build_leg_focus_action("椅上单脚搭低脚凳")
        self.assertNotIn("【pose:chair_one_foot_on_stool】", replacement)

        bed_pose = LEGFOCUS_POSE_BY_ID["bed_supine_knees_together"]
        bed_combos = compatible_leg_compositions(bed_pose)
        self.assertTrue(bed_combos)
        self.assertTrue(all(support == "lying_surface" for _, support, _ in bed_combos))
        self.assertTrue({"bed_surface", "bedroom_chaise"} <= {scene["id"] for scene, _, _ in bed_combos})
        self.assertEqual(bed_pose["camera_views"]["third"], ["third_oblique_level"])

    def test_leg_focus_random_outfits_exclude_full_length_trousers(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_OUTFIT_POOL, LEGFOCUS_OUTFIT_BY_ID, LEGFOCUS_OUTFIT_CAPTURE_PROFILES,
            _compatible_leg_outfits,
            build_leg_focus_action,
        )

        self.assertFalse(any(item["capture_profile"] == "trousers" for item in LEGFOCUS_OUTFIT_POOL))
        self.assertEqual(set(LEGFOCUS_OUTFIT_CAPTURE_PROFILES), {"skirt", "shorts"})
        self.assertFalse(any(
            re.search(r"trouser|legging|长裤|紧身裤", json.dumps(item, ensure_ascii=False), re.IGNORECASE)
            for item in LEGFOCUS_OUTFIT_POOL
        ))
        knee_outfits = {
            item["id"] for item in _compatible_leg_outfits(posture="kneeling")
        }
        self.assertEqual(knee_outfits, {"sport_shorts_set", "tee_cargo_shorts", "knit_camisole_shorts_set"})
        self.assertTrue(all(
            LEGFOCUS_OUTFIT_BY_ID[item_id]["capture_profile"] == "shorts"
            and "thigh_high" in LEGFOCUS_OUTFIT_BY_ID[item_id]["category"]
            for item_id in knee_outfits
        ))

    def test_leg_accessory_prompt_blends_with_surface_instead_of_looking_pasted_on(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import _leg_accessory_prompt

        prompt = _leg_accessory_prompt(
            {"title": "黑色星星爱心金属腿环", "prompt": "窄幅黑色皮革环带贴合大腿。"}, "光腿", {},
        )
        self.assertIn("接触阴影", prompt)
        self.assertIn("不悬浮、不像贴片", prompt)

    def test_distinctive_thigh_ring_names_and_aliases_select_the_requested_variant(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        outfit = leg_focus.LEGFOCUS_OUTFIT_BY_ID["lace_blouse_skirt"]
        pose = leg_focus.LEGFOCUS_POSE_BY_ID["standing_weight_shift"]
        for item in leg_focus.LEGFOCUS_ACCESSORIES:
            for term in (item["id"], item["title"], *item["aliases"]):
                with self.subTest(accessory=item["id"], term=term):
                    selected = leg_focus._pick_leg_accessory("光腿", outfit, {}, pose, f"请加{term}")
                    self.assertEqual([choice["id"] for choice in selected], [item["id"]])
        self.assertEqual(
            leg_focus._requested_leg_accessory("不要黑色猫耳蝴蝶结腿环，请加白色猫耳蝴蝶结腿环")["id"],
            "thigh_white_cat_bow_cross",
        )
        self.assertEqual(leg_focus._requested_leg_accessory("不要白色猫耳蝴蝶结腿环"), {})

    def test_distinctive_thigh_ring_details_survive_final_prompts_and_match_the_leg_surface(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus
        from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt

        cases = (
            ("standing_weight_shift", "bare_legs", "皮肤", "buckle_wrap_mini"),
            ("standing_weight_shift", "sheer", "袜料", "lace_blouse_skirt"),
            ("sofa_side_sit_knees_together", "thigh_high", "皮肤", "resort_open_shirt_shorts"),
            ("supine_parallel_extended", "bare_legs", "皮肤", "resort_open_shirt_shorts"),
        )
        for item in leg_focus.LEGFOCUS_ACCESSORIES:
            for pose_id, category, surface, outfit_id in cases:
                with self.subTest(accessory=item["id"], pose=pose_id, category=category):
                    action = leg_focus.build_leg_focus_action(
                        f"加{item['title']}", force_pose=pose_id, force_camera="third",
                        force_outfit=outfit_id, force_legwear_category=category,
                        force_face_mode="none",
                    )
                    self.assertIn(f"【leg_accessory:{item['id']}|thin|贴合】", action)
                    self.assertTrue(leg_focus.has_leg_focus_contract(action))
                    prompt = build_selfie_builtin_prompt(action)
                    self.assertIn(item["prompt"], prompt)
                    self.assertIn(f"紧贴{surface}", prompt)
                    self.assertIn("保留该款指定的环带配色、金属色和材质", prompt)
                    self.assertIn("随当前姿势受重力", prompt)
                    self.assertIn("不跨接双腿", prompt)
                    self.assertNotIn("厚度低且不凸起", prompt)
                    self.assertNotRegex(prompt, r"颜色与(?:肤色|袜色)同色系")
                    self.assertNotIn("不增加腿环", prompt)
                    if category == "thigh_high":
                        self.assertIn("不与袜口合并", prompt)

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
                    self.assertNotIn("允许上身、肩颈和头脸入画", action)
                    face_mode = re.search(r"【face_mode:(none|half|full)】", action).group(1)
                    self.assertIn(face_mode, view.get("face_modes", ["none"]))
                    if face_mode == "none":
                        self.assertTrue(
                            "头部与面部保持画外" in action
                            or "头部与面部完全不进入画面" in action
                        )
                    else:
                        self.assertIn(f"【crop_mode:{face_mode}_face】", action)
                        self.assertIn("本次神态按 COS 表情模板执行", action)
                    self.assertIn("不额外添加衣物或物件遮挡", action)
                    self.assertNotIn("头部、脸部和肩部不入镜", action)
                    if camera == "selfie":
                        if framing_key == "mirror":
                            self.assertIn("镜中同一人的腿部穿搭与姿势是视觉主体", action)
                            self.assertIn("竖屏近中景", action)
                            self.assertIn("不为补全头脚拉远镜头", action)
                            self.assertNotIn("本人看向自己双腿", action)
                        else:
                            self.assertIn("本人看向自己双腿", action)
                        self.assertNotRegex(action, r"胸下(?:起裁|裁切|取景|开始)|三分之四侧向|椅背只")
                        self.assertIn("第一人称自拍机位由人物手持手机拍摄", action)
                    else:
                        self.assertIn("第三人称画外相机从人物外部拍摄", action)
                        self.assertIn("视觉重点落在腿部穿搭", action)
                        self.assertIn("画面上缘严格服从本次 crop mode", action)
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
            LEGFOCUS_CAMERA_FRAMINGS, LEGFOCUS_POSE_POOL, LEGFOCUS_VIEW_BY_ID,
            LegFocusDataError, build_leg_focus_action, compatible_leg_compositions,
        )

        for pose in LEGFOCUS_POSE_POOL:
            with self.subTest(pose=pose["id"]):
                face_combinations = [
                    item for item in compatible_leg_compositions(
                        pose, camera="third", framing="full_outfit", allow_explicit=True,
                    )
                    if "full" in item[2].get("face_modes", ["none"])
                ]
                if not face_combinations:
                    with self.assertRaisesRegex(LegFocusDataError, "不支持指定的露脸方式"):
                        build_leg_focus_action(
                            has_refs=True, force_pose=pose["id"], force_camera="third",
                            force_framing="full_outfit", force_face_mode="full",
                        )
                    continue
                force_view = face_combinations[0][2]["id"] if face_combinations else ""
                action = build_leg_focus_action(
                    has_refs=True, force_pose=pose["id"], force_camera="third",
                    force_framing="full_outfit", force_face_mode="full", force_view=force_view,
                )
                self.assertIn("【frame:full_outfit】", action)
                self.assertIn("【face_mode:full】", action)
                self.assertIn("【legs_face:context】", action)
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

        for field in (
            "camera_framings", "reference_rules", "appearance_prompts", "capture_styles", "scene_states",
        ):
            for invalid in (None, {}, {"selfie": "", "third": "external"}, {"zh": [], "en": "reference"}):
                with self.subTest(field=field, value=invalid):
                    payload = dict(leg_focus._POOL)
                    payload[field] = invalid
                    with patch.object(
                        leg_focus, "read_versioned_json",
                        side_effect=lambda path, _label: _leg_pool_payload_for_path(leg_focus, path, payload),
                    ):
                        with self.assertRaisesRegex(leg_focus.LegFocusDataError, field):
                            leg_focus._load_pool()

        for field, value, message in (
            ("hosiery_rule", "", "hosiery_rule"),
            ("anatomy_rule", "x" * 61, "60 字"),
        ):
            payload = dict(leg_focus._POOL)
            payload[field] = value
            with patch.object(
                leg_focus, "read_versioned_json",
                side_effect=lambda path, _label: _leg_pool_payload_for_path(leg_focus, path, payload),
            ):
                with self.assertRaisesRegex(leg_focus.LegFocusDataError, message):
                    leg_focus._load_pool()

        payload = copy.deepcopy(leg_focus._POOL)
        payload["poses"][0]["risk"] = []
        with patch.object(
            leg_focus, "read_versioned_json",
            side_effect=lambda path, _label: _leg_pool_payload_for_path(leg_focus, path, payload),
        ):
            with self.assertRaisesRegex(leg_focus.LegFocusDataError, "字段不完整或引用无效"):
                leg_focus._load_pool()

        for probability in (None, True, -0.1, 1.1):
            with self.subTest(scene_state_probability=probability):
                payload = dict(leg_focus._POOL)
                payload["scene_state_probability"] = probability
                with patch.object(
                    leg_focus, "read_versioned_json",
                    side_effect=lambda path, _label: _leg_pool_payload_for_path(leg_focus, path, payload),
                ):
                    with self.assertRaisesRegex(leg_focus.LegFocusDataError, "scene_state_probability"):
                        leg_focus._load_pool()

    def test_leg_capture_styles_match_scene_and_camera_mode(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        styles = leg_focus.LEGFOCUS_CAPTURE_STYLE_POOL
        ids = {style["id"] for style in styles}
        self.assertEqual(len(ids), len(styles))
        workout = next(style for style in styles if style["id"] == "post_workout_mirror_flash")
        self.assertEqual(workout["modes"], ["mirror"])
        self.assertEqual(workout["scene_ids"], ["floor_mirror"])

        mirror_pose = leg_focus.LEGFOCUS_POSE_BY_ID["mirror_side_sit_shins_overlap"]
        with patch.object(leg_focus, "_weighted_capture_style_choice", return_value=workout) as choose:
            action = leg_focus.build_leg_focus_action(
                "落地镜前", force_pose=mirror_pose["id"], force_camera="selfie",
            )
        choose.assert_called_once_with("floor_mirror", "mirror", "indoor", "")
        self.assertIn("闷热让额角、手臂和腿部有细密汗珠与汗湿反光", action)
        self.assertIn("【leg_capture_style:post_workout_mirror_flash】", action)
        self.assertNotIn("【leg_state:", action)

        candidates = [
            style for style in styles
            if "third" in style["modes"]
            and ("*" in style["scene_ids"] or "floor_window" in style["scene_ids"])
        ]
        self.assertTrue(candidates)
        self.assertTrue(any(style["id"] == "rain_window_contrast" for style in candidates))

    def test_outdoor_scenes_have_reachable_states_and_matching_capture_styles(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        outdoor_scenes = [
            scene for scene in leg_focus.LEGFOCUS_SCENE_POOL
            if scene.get("environment", "indoor") == "outdoor"
        ]
        self.assertEqual(
            {scene["id"] for scene in outdoor_scenes},
            {"outdoor_pool_edge", "pool_lounger", "beach_mat", "beach_canvas_chair"},
        )
        self.assertTrue(all(scene.get("capture_modes") == ["direct"] for scene in outdoor_scenes))
        self.assertEqual(leg_focus.LEGFOCUS_SCENE_STATE_PROBABILITY, 0.3)
        self.assertEqual(
            {state["id"] for state in leg_focus.LEGFOCUS_SCENE_STATE_POOL},
            {"pool_water_wet", "beach_sweat_damp"},
        )

        def build_for_scene(scene: dict, state: dict | None = None) -> str:
            pose = next(
                pose for pose in leg_focus.LEGFOCUS_POSE_POOL
                if leg_focus.compatible_leg_compositions(
                    pose, camera="selfie", scene_ids=scene["id"],
                )
            )
            with patch.object(leg_focus, "_weighted_scene_state_choice", return_value=state or {}):
                return leg_focus.build_leg_focus_action(
                    scene["title"], force_pose=pose["id"], force_camera="selfie", has_refs=True,
                )

        for scene in outdoor_scenes:
            with self.subTest(scene=scene["id"]):
                for camera_mode in ("selfie", "third"):
                    reachable = [
                        (pose, combo)
                        for pose in leg_focus.LEGFOCUS_POSE_POOL
                        for combo in leg_focus.compatible_leg_compositions(
                            pose, camera=camera_mode, scene_ids=scene["id"],
                        )
                    ]
                    self.assertTrue(reachable)
                action = build_for_scene(scene)
                self.assertIn(f"【leg_scene:{scene['id']}】", action)
                self.assertNotIn("【leg_state:", action)
                self.assertNotIn("干爽", action)
                self.assertNotIn("水珠", action)
                self.assertNotIn("水痕", action)
                self.assertFalse(any(item["prompt"] in action for item in leg_focus.LEGFOCUS_SCENE_STATE_POOL))
                style_id = re.search(r"【leg_capture_style:([a-z_]+)】", action).group(1)
                matching_styles = leg_focus._compatible_capture_styles(
                    scene["id"], "selfie", "outdoor",
                )
                self.assertIn(style_id, {item["id"] for item in matching_styles})
                for allowed_state in leg_focus.LEGFOCUS_SCENE_STATE_POOL:
                    if "outdoor" not in allowed_state["environments"] or (
                        "*" not in allowed_state["scene_ids"]
                        and scene["id"] not in allowed_state["scene_ids"]
                    ):
                        continue
                    for camera_mode in ("selfie", "third"):
                        self.assertTrue(leg_focus._compatible_capture_styles(
                            scene["id"], camera_mode, "outdoor", allowed_state["id"],
                        ))

        for state in leg_focus.LEGFOCUS_SCENE_STATE_POOL:
            scene = next(
                scene for scene in outdoor_scenes
                if "*" in state["scene_ids"] or scene["id"] in state["scene_ids"]
            )
            with self.subTest(optional_state=state["id"]):
                action = build_for_scene(scene, state)
                self.assertIn(f"【leg_state:{state['id']}】", action)
                self.assertEqual(action.count(state["prompt"]), 1)
                self.assertEqual(
                    len([item for item in leg_focus.LEGFOCUS_SCENE_STATE_POOL if item["prompt"] in action]),
                    1,
                )
                scene_id = re.search(r"【leg_scene:([a-z_]+)】", action).group(1)
                style_id = re.search(r"【leg_capture_style:([a-z_]+)】", action).group(1)
                matching_styles = leg_focus._compatible_capture_styles(
                    scene_id, "selfie", "outdoor", state["id"],
                )
                self.assertIn(style_id, {item["id"] for item in matching_styles})
                self.assertNotIn("水珠", state["prompt"])
                self.assertNotIn("水痕", state["prompt"])

    def test_leg_angle_requests_select_actual_camera_geometry_without_selecting_a_pose(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_POSE_BY_ID, LEGFOCUS_VIEW_BY_ID, build_leg_focus_action, parse_requested_leg_pose,
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
                pose_id = re.search(r"【pose:([a-z_]+)】", action).group(1)
                posture = LEGFOCUS_POSE_BY_ID[pose_id]["posture"]
                if posture == "kneeling":
                    self.assertRegex(action, r"【leg_support:(?:flat_ground|flat_ground)】")
                else:
                    self.assertEqual(posture, "seated")
                    self.assertIn("【leg_support:", action)
            self.assertNotEqual(view["direction"], "side")

    def test_leg_incompatible_requests_do_not_silently_change_the_scene_pose_or_person(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LegFocusDataError, build_leg_focus_action, has_leg_focus_contract,
        )

        for query, options in (
            ("沙发", {"force_pose": "edge_knees_together_dangle"}),
            ("地毯", {"force_pose": "edge_knees_together_dangle"}),
            ("电竞椅", {"force_pose": "kneeling_upright"}),
            ("第一人称 侧拍", {}),
            ("第一人称 斜侧拍", {}),
            ("床沿", {"force_pose": "kneeling_sit_back"}),
            ("卧室地毯", {"force_pose": "chair_thigh_cross_slant"}),
            ("电竞椅", {"force_pose": "kneeling_sit_back"}),
            ("第三人称 顶拍", {}),
            ("第三人称 背身", {}),
            ("第三人称 仰拍 俯拍", {}),
            ("地毯", {"force_view": "third_side_low"}),
        ):
            with self.subTest(query=query, options=options), self.assertRaises(LegFocusDataError):
                build_leg_focus_action(query, **options)

        for query, pose_id in (
            ("第三人称", "kneeling_sit_back"),
            ("第三人称 俯拍", "edge_knees_together_dangle"),
            ("第三人称 平拍", "chair_thigh_cross_slant"),
        ):
            with self.subTest(query=query, pose_id=pose_id):
                action = build_leg_focus_action(query, force_pose=pose_id)
                self.assertIn(f"【pose:{pose_id}】", action)
                self.assertTrue(has_leg_focus_contract(action))

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
                with patch.object(
                    leg_focus, "read_versioned_json",
                    side_effect=lambda path, _label: _leg_pool_payload_for_path(
                        leg_focus, path, payload, view_payload,
                    ),
                ):
                    with self.assertRaises(leg_focus.LegFocusDataError):
                        leg_focus._load_pool()

    def test_pose_camera_groups_keep_random_selection_inside_valid_combinations(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        expected = set(leg_focus.LEGFOCUS_POSE_BY_ID)
        self.assertEqual(
            leg_focus.LEGFOCUS_EXPLICIT_ONLY_POSE_IDS,
            set(),
        )
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

        for pose_id in (
            "seated_thigh_gentle_pinch", "seated_knees_offset", "kneeling_relaxed_apart",
            "chair_self_parallel_knees_offset",
        ):
            def select_requested(pool):
                self.assertIn(pose_id, {pose["id"] for pose in pool})
                return dict(next(pose for pose in pool if pose["id"] == pose_id))

            with self.subTest(pose=pose_id), patch.object(leg_focus.random, "random", return_value=1), patch.object(leg_focus, "_weighted_pose_choice", side_effect=select_requested):
                action = leg_focus.build_leg_focus_action()
                expected_id = "seated_knees_offset" if pose_id == "seated_knees_offset" else pose_id
                self.assertIn(f"【pose:{expected_id}】", action)
                self.assertTrue(leg_focus.has_leg_focus_contract(action))

        for pose_id in ("mirror_side_sit_shins_overlap", "mirror_seated_knees_apart_feet_together"):
            def select_weighted_pose(pool):
                self.assertNotIn(pose_id, {pose["id"] for pose in pool})
                return dict(pool[0])

            with self.subTest(pose=pose_id, random_pool="high_risk_excluded"), patch.object(
                leg_focus.random, "random", return_value=1,
            ), patch.object(leg_focus, "_weighted_pose_choice", side_effect=select_weighted_pose):
                leg_focus.build_leg_focus_action()
            self.assertIn(f"【pose:{pose_id}】", leg_focus.build_leg_focus_action(force_pose=pose_id))

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
        with patch(
            "astrbot_plugin_selfie_image.generation.selfie_batch_workflow._plan_legwear_categories",
            return_value=["bare_legs"] * count,
        ):
            result = asyncio.run(plugin._run_selfie_batches_unlocked(
                task_id="legs-view-test", event=None, action=action, extra_refs=[],
                source="command-look-legs", requested_count=count, aspect="9:16",
                resolution="1K", fail_label="失败", rebuild_extra_request=request,
            ))
        self.assertTrue(result["success"])
        requests = [call.args[0] for call in plugin._run_image_generation.await_args_list]
        self.assertEqual(len(requests), count)
        self.assertEqual(len({re.search(r"【leg_view:([a-z_]+)】", prompt).group(1) for prompt in requests}), count)
        for prompt in requests:
            self.assertIn("【pose:chair_thigh_cross_slant】", prompt)
            self.assertIn("【leg_scene:gaming_chair】", prompt)
        for call in plugin._run_image_generation.await_args_list:
            self.assertEqual(call.kwargs["original_prompt"], call.args[0])

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
            LEGFOCUS_HOSIERY_STYLES, LEGFOCUS_OUTFIT_POOL, LegFocusDataError, build_leg_focus_action,
        )

        for outfit in LEGFOCUS_OUTFIT_POOL:
            for wear in ("黑丝", "白丝", "连裤袜", "赤足"):
                kneeling_only = "kneeling" in outfit.get("compatible_postures", [])
                pose_id = "kneeling_single_knee_grounded" if kneeling_only else "standing_weight_shift"
                if outfit.get("capture_profile") != "skirt" and wear == "连裤袜":
                    with self.subTest(outfit=outfit["id"], wear=wear), self.assertRaises(LegFocusDataError):
                        build_leg_focus_action(
                            wear, force_pose=pose_id,
                            force_camera="third" if kneeling_only else "selfie",
                            force_outfit=outfit["id"],
                        )
                    continue
                with self.subTest(outfit=outfit["id"], wear=wear), patch(
                    "astrbot_plugin_selfie_image.cos.leg_focus.pick_leg_focus_outfit", return_value=outfit,
                ):
                    action = build_leg_focus_action(
                        wear, force_pose=pose_id,
                        force_camera="third" if kneeling_only else "selfie",
                    )
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
        white_sheer = next(style for style in LEGFOCUS_HOSIERY_STYLES if style["id"] == "white_sheer")
        black_sheer = next(style for style in LEGFOCUS_HOSIERY_STYLES if style["id"] == "black_sheer")
        self.assertIn("灰白/冷象牙色纱线色差", white_sheer["prompt"])
        self.assertIn("肤色柔和透出", white_sheer["prompt"])
        self.assertIn("皮肤保持自然肤色对比", white_sheer["prompt"])
        self.assertNotRegex(white_sheer["prompt"], r"漂白|提白")
        self.assertIn("细密黑色纤维", black_sheer["prompt"])
        self.assertIn("局部柔和缎光", black_sheer["prompt"])
        for style in LEGFOCUS_HOSIERY_STYLES:
            for alias in style["aliases"]:
                with self.subTest(style=style["id"], alias=alias):
                    action = build_leg_focus_action(alias)
                    self.assertIn(f"【hosiery:{style['id']}】", action)
                    visual_core = re.split(
                        r"(?:不加|不增加|不要|不出现|禁止|不需要|不带)",
                        style["prompt"], maxsplit=1,
                    )[0].rstrip("；，,")
                    self.assertIn(visual_core, action)
                    selected = [outfit for outfit in LEGFOCUS_OUTFIT_POOL if outfit["prompt"] in action]
                    self.assertEqual(len(selected), 1)
                    self.assertIn(style["family"], selected[0]["hosiery_families"])

    def test_random_legwear_uses_full_coverage_defaults_and_keeps_special_requests_explicit(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_HOSIERY_STYLES, LEGFOCUS_RANDOM_LEG_DETAILS, LEGFOCUS_OUTFIT_POOL,
            _pick_hosiery_style, _weighted_detail_choice, build_leg_focus_action,
        )

        self.assertTrue(next(item for item in LEGFOCUS_RANDOM_LEG_DETAILS if item["id"] == "barefoot")["explicit_only"])
        self.assertTrue(all(style.get("category") for style in LEGFOCUS_HOSIERY_STYLES))
        self.assertTrue(all(outfit.get("risk") and outfit.get("category") for outfit in LEGFOCUS_OUTFIT_POOL))
        for outfit in LEGFOCUS_OUTFIT_POOL:
            for wear in ("黑丝", "白丝"):
                with self.subTest(outfit=outfit["id"], wear=wear):
                    style = _pick_hosiery_style(wear, outfit, category="sheer")
                    if outfit.get("capture_profile") == "skirt":
                        self.assertTrue(style)
                        self.assertEqual(style["category"], "sheer")
                    else:
                        self.assertEqual(style, {})
                        ankle_style = _pick_hosiery_style(wear, outfit, category="ankle_sock")
                        self.assertEqual(ankle_style["kind"], "ankle")

        for _ in range(30):
            self.assertNotEqual(_weighted_detail_choice()["wear"], "赤足")

        stirrup = build_leg_focus_action(
            "踩脚黑丝", force_pose="standing_weight_shift", force_camera="selfie",
            force_outfit="jk_cardigan_pleats",
        )
        self.assertIn("黑色过膝踩脚长袜", stirrup)
        barefoot = build_leg_focus_action("赤足", force_pose="kneeling_sit_back", force_camera="selfie")
        self.assertIn("脚趾数量正确", barefoot)
        self.assertIn("【wear:barefoot】", barefoot)

    def test_leg_prompt_pool_deep_compatibility_lint_is_clean(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import validate_leg_focus_catalog

        self.assertEqual(validate_leg_focus_catalog(), [])

    def test_outfit_pool_is_json_owned_and_contains_no_source_policy_prompt(self) -> None:
        from astrbot_plugin_selfie_image.core.prompt_data import read_versioned_json
        from astrbot_plugin_selfie_image.cos.leg_focus import OUTFIT_DATA_PATH, LEGFOCUS_OUTFIT_POOL

        payload = read_versioned_json(OUTFIT_DATA_PATH, "test")
        self.assertNotIn("source_rule", payload)
        self.assertEqual(set(payload), {"version", "outfits"})
        self.assertEqual(payload["outfits"], LEGFOCUS_OUTFIT_POOL)

    def test_leg_outfit_capture_rules_keep_skirts_off_front_cameras(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        for outfit in leg_focus.LEGFOCUS_OUTFIT_POOL:
            with self.subTest(outfit=outfit["id"]):
                kneeling_only = "kneeling" in outfit.get("compatible_postures", [])
                pose_id = "kneeling_single_knee_grounded" if kneeling_only else "edge_knees_together_dangle"
                action = leg_focus.build_leg_focus_action(
                    force_pose=pose_id, force_camera="third", force_outfit=outfit["id"],
                )
                view_id = re.search(r"【leg_view:([a-z_]+)】", action).group(1)
                view = leg_focus.LEGFOCUS_VIEW_BY_ID[view_id]
                self.assertIn(
                    view["elevation"],
                    leg_focus.LEGFOCUS_OUTFIT_CAPTURE_PROFILES[outfit["capture_profile"]]["third_elevations"],
                )
                self.assertEqual(action.count(outfit["prompt"]), 1)
                self.assertTrue(leg_focus.has_leg_focus_contract(action))
                if kneeling_only:
                    self.assertIn(view["id"], {"third_kneeling_oblique_down", "third_oblique_level"})
                    face_mode = re.search(r"【face_mode:(none|half|full)】", action).group(1)
                    self.assertIn(face_mode, view.get("face_modes", ["none"]))
                    crop = "shoulder_crop" if face_mode == "none" else f"{face_mode}_face"
                    self.assertIn(f"【crop_mode:{crop}】", action)
                    with self.assertRaisesRegex(leg_focus.LegFocusDataError, "不兼容"):
                        leg_focus.build_leg_focus_action(
                            force_pose="edge_knees_together_dangle", force_outfit=outfit["id"],
                        )
                    continue
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
                    force_pose="seated_knees_offset", force_camera="selfie", force_outfit=outfit["id"],
                )
                self.assertIn(f"【outfit:{outfit['id']}】", self_view)
                self.assertIn("【cam:selfie】", self_view)

    def test_leg_outfit_filter_only_returns_compatible_skirts(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        compositions = leg_focus.compatible_leg_compositions(
            leg_focus.LEGFOCUS_POSE_BY_ID["edge_knees_together_ankles_apart"],
        )
        skirts = [outfit for outfit in leg_focus.LEGFOCUS_OUTFIT_POOL if outfit["capture_profile"] == "skirt"]
        with patch.object(leg_focus, "LEGFOCUS_OUTFIT_POOL", skirts):
            selected = leg_focus.pick_leg_focus_outfit(compositions=compositions, avoid_id=skirts[0]["id"])
            self.assertIn(selected, leg_focus._compatible_leg_outfits(compositions=compositions))
        action = leg_focus.build_leg_focus_action(force_pose="edge_knees_together_ankles_apart")
        outfit_id = re.search(r"【outfit:([a-z_]+)】", action).group(1)
        outfit = leg_focus.get_leg_focus_outfit(outfit_id)
        self.assertIn(outfit, leg_focus._compatible_leg_outfits(
            posture="seated", compositions=compositions,
        ))

    def test_outfit_environment_rules_keep_sleepwear_indoors_and_resort_clothes_outdoors(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        outfits = {item["id"]: item for item in leg_focus.LEGFOCUS_OUTFIT_POOL}
        indoor_only_ids = {
            *(outfit_id for outfit_id in outfits if outfit_id.startswith("sleep_")),
        }
        for outfit_id in indoor_only_ids:
            with self.subTest(outfit=outfit_id):
                self.assertEqual(outfits[outfit_id]["environments"], ["indoor"])

        resort_ids = {
            "resort_halter_wrap_mini", "resort_fitted_crop_shorts", "resort_open_shirt_shorts",
        }
        for outfit_id in resort_ids:
            with self.subTest(outfit=outfit_id):
                outfit = outfits[outfit_id]
                self.assertTrue(leg_focus._leg_outfit_allows_environment(outfit, "indoor"))
                self.assertTrue(leg_focus._leg_outfit_allows_environment(outfit, "outdoor"))

        general_outfit = outfits["jk_cardigan_pleats"]
        self.assertTrue(leg_focus._leg_outfit_allows_environment(general_outfit, "indoor"))
        self.assertTrue(leg_focus._leg_outfit_allows_environment(general_outfit, "outdoor"))

        pose = leg_focus.LEGFOCUS_POSE_BY_ID["seated_knees_offset"]
        beach_compositions = leg_focus.compatible_leg_compositions(
            pose, scene_ids="beach_canvas_chair",
        )
        beach_candidates = leg_focus._compatible_leg_outfits(compositions=beach_compositions)
        self.assertTrue(beach_compositions)
        self.assertFalse(indoor_only_ids & {item["id"] for item in beach_candidates})
        self.assertTrue(resort_ids & {item["id"] for item in beach_candidates})

        for _ in range(40):
            action = leg_focus.build_leg_focus_action(
                "海边帆布椅", force_pose=pose["id"],
            )
            outfit_id = re.search(r"【outfit:([a-z_]+)】", action).group(1)
            self.assertTrue(leg_focus._leg_outfit_allows_environment(outfits[outfit_id], "outdoor"))

        indoor_action = leg_focus.build_leg_focus_action(
            "客厅单椅", force_pose=pose["id"], force_outfit="sleep_drop_shoulder_shorts",
        )
        self.assertIn("【outfit:sleep_drop_shoulder_shorts】", indoor_action)
        with self.assertRaisesRegex(leg_focus.LegFocusDataError, "不兼容"):
            leg_focus.build_leg_focus_action(
                "海边帆布椅", force_pose=pose["id"], force_outfit="sleep_drop_shoulder_shorts",
            )

    def test_white_tights_and_supine_pose_prompts_keep_fabric_and_relaxed_shape(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        white_tights = next(item for item in leg_focus.LEGFOCUS_HOSIERY_STYLES if item["id"] == "white_sheer")
        supine = leg_focus.LEGFOCUS_POSE_BY_ID["supine_parallel_extended"]
        self.assertIn("皮肤保持自然肤色对比", white_tights["prompt"])
        self.assertIn("细密的针织微纹", white_tights["prompt"])
        self.assertIn("双膝保持放松", supine["prompt"])
        self.assertIn("轻微前后错位", supine["prompt"])
        self.assertIn("适中距离", supine["crop_prompts"]["selfie"])
        for view_id in ("self_forward_down", "self_left_down", "self_right_down"):
            with self.subTest(view=view_id):
                self.assertIn("普通手机焦段", leg_focus.LEGFOCUS_VIEW_BY_ID[view_id]["prompt"])
                self.assertIn("避免头顶垂直俯拍", leg_focus.LEGFOCUS_VIEW_BY_ID[view_id]["prompt"])

    def test_leg_outfit_schema_rejects_invalid_capture_rules(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        for error in (
            "missing_profile", "unknown_profile", "missing_coverage", "invalid_direction",
            "invalid_elevation", "non_tights_random", "empty_environments", "unknown_environment",
            "duplicate_environments", "unhashable_environment",
        ):
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
                elif error == "invalid_elevation":
                    payload["capture_profiles"]["skirt"]["third_elevations"] = ["unknown"]
                elif error in {"empty_environments", "unknown_environment", "duplicate_environments", "unhashable_environment"}:
                    payload["outfits"][0]["environments"] = {
                        "empty_environments": [],
                        "unknown_environment": ["pool"],
                        "duplicate_environments": ["indoor", "indoor"],
                        "unhashable_environment": [[]],
                    }[error]
                else:
                    payload["hosiery_styles"][1].pop("category", None)
                with patch.object(
                    leg_focus, "read_versioned_json",
                    side_effect=lambda path, _label: _leg_outfit_payload_for_path(leg_focus, path, payload),
                ):
                    with self.assertRaises(leg_focus.LegFocusDataError):
                        leg_focus._load_outfit_pool()

    def test_legwear_categories_bare_contract_and_accessory_compatibility(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        expected_categories = {
            "bare_legs", "sheer", "opaque", "fishnet", "knee_high", "ankle_sock", "thigh_high",
        }
        self.assertTrue(expected_categories <= {item["category"] for item in leg_focus.LEGFOCUS_RANDOM_LEG_DETAILS})
        bare = leg_focus.build_leg_focus_action(
            force_legwear_category="bare_legs", force_pose="kneeling_sit_back", force_camera="selfie",
        )
        self.assertIn("【legwear_category:bare_legs】", bare)
        self.assertIn("【hosiery:none】", bare)
        self.assertNotIn("【hosiery:white_sheer】", bare)
        self.assertFalse(any(style["prompt"] in bare for style in leg_focus.LEGFOCUS_HOSIERY_STYLES))
        bare_detail = next(item for item in leg_focus.LEGFOCUS_RANDOM_LEG_DETAILS if item["id"] == "bare_legs")
        self.assertNotRegex(bare_detail["prompt"], r"袜|织物|纤维|白丝|冷调|丝袜")

        explicit_black = leg_focus.build_leg_focus_action(
            "黑丝", force_legwear_category="bare_legs", force_pose="standing_weight_shift", force_camera="selfie",
        )
        self.assertIn("【wear:black】", explicit_black)
        outfit_id = re.search(r"【outfit:([a-z_]+)】", explicit_black).group(1)
        expected_category = (
            "thigh_high" if leg_focus.LEGFOCUS_OUTFIT_BY_ID[outfit_id]["capture_profile"] == "shorts" else "sheer"
        )
        self.assertIn(f"【legwear_category:{expected_category}】", explicit_black)

        outfit = leg_focus.LEGFOCUS_OUTFIT_BY_ID["lace_blouse_skirt"]
        thigh_high = next(item for item in leg_focus.LEGFOCUS_HOSIERY_STYLES if item["id"] == "black_lace_top")
        pose = leg_focus.LEGFOCUS_POSE_BY_ID["standing_weight_shift"]
        self.assertEqual(
            [item["id"] for item in leg_focus._pick_leg_accessory("黑丝", outfit, thigh_high, pose, "加吊袜带")],
            [],
        )
        bare_outfit = leg_focus.LEGFOCUS_OUTFIT_POOL[0]
        thigh_ring = leg_focus._pick_leg_accessory(
            "赤足", bare_outfit, {}, pose, "请加金属腿环",
        )
        self.assertEqual([item["id"] for item in thigh_ring], ["thigh_double_strap_ring"])
        self.assertEqual(leg_focus._pick_leg_accessory("黑丝", bare_outfit, {}, pose, "请加脚趾戒"), [])

    def test_leg_combo_presets_are_selectable_random_and_keep_contracts(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        self.assertEqual(len(leg_focus.LEGFOCUS_COMBO_POOL), 10)
        for combo in leg_focus.LEGFOCUS_COMBO_POOL:
            with self.subTest(combo=combo["id"]):
                action = leg_focus.build_leg_focus_action(combo["id"])
                self.assertIn(f"【combo:{combo['id']}】", action)
                self.assertIn(f"【pose:{combo['pose']}】", action)
                self.assertIn(f"【leg_view:{combo['view']}】", action)
                self.assertIn(f"【crop_mode:{combo['crop_mode']}】", action)
                self.assertIn(f"【angle:pitch={combo['angle']['pitch']},yaw={combo['angle']['yaw']}】", action)
                self.assertTrue(leg_focus.has_leg_focus_contract(action))
                tail = action.rsplit("【combo:", 1)[1]
                self.assertRegex(tail, r"^combo_[0-9]{2}】 【leg_view:[a-z_]+】 【pose:[a-z_]+】 【crop_mode:[a-z_]+】 【angle:pitch=-?[0-9]+,yaw=-?[0-9]+】 【cam:(?:selfie|third)】$")

        state = random.getstate()
        try:
            random.seed(117)
            counts = {}
            for index in range(1000):
                action = leg_focus.build_leg_focus_action("")
                match = re.search(r"【combo:(combo_[0-9]{2})】", action)
                combo_id = match.group(1) if match else "none"
                counts[combo_id] = counts.get(combo_id, 0) + 1
        finally:
            random.setstate(state)
        hit_counts = {key: value for key, value in counts.items() if key != "none"}
        self.assertEqual(set(hit_counts), {
            combo["id"] for combo in leg_focus.LEGFOCUS_COMBO_POOL
            if combo["risk"] != "high"
            and leg_focus.LEGFOCUS_POSE_BY_ID[combo["pose"]].get("risk") != "high"
        })
        self.assertGreaterEqual(sum(hit_counts.values()) / 1000, 0.3)
        self.assertLessEqual(max(hit_counts.values()) / 1000, 0.25)
        for _ in range(100):
            action = leg_focus.build_leg_focus_action("")
            mode = re.search(r"【face_mode:(none|half|full)】", action).group(1)
            view_id = re.search(r"【leg_view:([a-z_]+)】", action).group(1)
            self.assertIn(mode, leg_focus.LEGFOCUS_VIEW_BY_ID[view_id].get("face_modes", ["none"]))
        self.assertIn("【combo:combo_03】", leg_focus.build_leg_focus_action(
            "完整露脸", prefer_combo=True,
        ))
        used = set()
        for _ in range(8):
            action = leg_focus.build_leg_focus_action("", avoid_combos=used, prefer_combo=True)
            used.add(re.search(r"【combo:(combo_[0-9]{2})】", action).group(1))
        self.assertEqual(len(used), 8)
        risky_poses = {
            pose_id for pose_id, pose in leg_focus.LEGFOCUS_POSE_BY_ID.items()
            if pose.get("risk") == "high"
        }
        default_face_combo_poses = {
            combo["pose"] for combo in leg_focus.LEGFOCUS_COMBO_POOL
            if combo["face_mode"] in {"half", "full"}
        }
        self.assertTrue(risky_poses.isdisjoint(default_face_combo_poses))

    def test_named_leg_combos_keep_pose_and_view_when_wear_changes(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        default = leg_focus.build_leg_focus_action("combo_08")
        bare = leg_focus.build_leg_focus_action("combo_08 光腿")
        white = leg_focus.build_leg_focus_action("combo_01 白丝")
        kneeling = leg_focus.build_leg_focus_action("combo_06")

        self.assertIn("【pose:sofa_side_sit_knees_together】", default)
        self.assertIn("【leg_view:third_seated_oblique_down】", default)
        self.assertIn("【pose:sofa_side_sit_knees_together】", bare)
        self.assertIn("【leg_view:third_seated_oblique_down】", bare)
        self.assertIn("【wear:bare】", bare)
        self.assertNotIn("sock_top_bow", bare)
        self.assertRegex(bare, r"【leg_accessory:(?!none)[^】]+】")
        self.assertIn("【wear:white】", white)
        self.assertIn("【leg_accessory:", white)
        self.assertIn("【leg_support:flat_ground】", kneeling)
        self.assertIn("【angle:pitch=30,yaw=45】", kneeling)

    def test_leg_angle_aliases_and_accessory_stacking_rules(self) -> None:
        from astrbot_plugin_selfie_image.cos import leg_focus

        self.assertEqual(
            leg_focus.parse_leg_angle_tokens("右前斜俯"),
            {
                "pitch_range": (30, 40), "pitch": 35,
                "yaw_range": (30, 60), "yaw": 45,
            },
        )
        self.assertEqual(
            leg_focus.parse_leg_angle_tokens("正侧")["yaw_ranges"],
            ((-100, -80), (80, 100)),
        )
        self.assertEqual(leg_focus.parse_leg_angle_tokens("轻俯")["pitch"], 15)
        self.assertEqual(leg_focus.parse_leg_angle_tokens("陡俯")["pitch"], 55)
        self.assertEqual(leg_focus.parse_leg_angle_tokens("平视")["pitch"], 0)
        self.assertEqual(leg_focus.parse_leg_angle_tokens("仰拍")["pitch"], -20)
        self.assertEqual(leg_focus.parse_leg_angle_tokens("左前")["yaw"], -45)
        self.assertEqual(leg_focus.parse_leg_angle_tokens("正侧")["yaw_values"], (-90, 90))
        self.assertEqual(leg_focus.parse_leg_angle_tokens("左正侧")["yaw_range"], (-100, -80))
        self.assertEqual(leg_focus.parse_requested_leg_angles("右前")["yaw_range"], (40, 50))
        self.assertEqual(leg_focus.parse_requested_leg_angles("陡俯")["pitch_range"], (50, 60))
        self.assertEqual(
            leg_focus.parse_requested_leg_angles("正侧")["yaw_ranges"],
            ((-95, -85), (85, 95)),
        )
        view_prompts = {
            view["id"]: leg_focus._view_angle_prompt(view)
            for view in leg_focus.LEGFOCUS_VIEW_POOL if view["camera"] == "third"
        }
        self.assertIn("机位与人物肩线同高", view_prompts["third_oblique_level"])
        self.assertIn("俯视约35度", view_prompts["third_seated_oblique_down"])
        self.assertIn("轻仰约15度", view_prompts["third_front_low"])
        self.assertIn("普通手机焦段、竖幅近距离拍摄", view_prompts["third_oblique_level"])
        cleaned = leg_focus._clean_leg_view_prompt(
            "相机正面平拍；与膝部基本等高；全身入镜。",
            leg_focus.LEGFOCUS_VIEW_BY_ID["third_seated_oblique_down"],
            "shoulder_crop",
        )
        self.assertEqual(cleaned, "")
        with patch.object(leg_focus.random, "random", return_value=1):
            third = leg_focus.build_leg_focus_action("第三人称 不露脸")
        self.assertIn("第三人称画外相机", third)
        self.assertRegex(third, r"第三人称画外相机位于.+偏航角[+-]?\d+度")
        self.assertRegex(third, r"【angle:pitch=-?\d+,yaw=-?\d+】")
        self.assertIn("肩线下方", third)
        accessories = leg_focus.LEGFOCUS_ACCESSORY_BY_ID
        self.assertTrue(set(accessories).isdisjoint({
            "thigh_metal_band", "thigh_lace_band", "thigh_satin_band", "thigh_double_chain",
            "thigh_pearl_band", "thigh_leather_band", "thigh_bow_band", "thigh_crystal_band",
        }))
        self.assertTrue(all(item["accessory_type"] == "thigh_band" for item in accessories.values()))
        self.assertTrue(all(
            "皮肤保持本人自然肤色" in leg_focus._leg_accessory_prompt(item, "光腿", {})
            for item in accessories.values()
        ))
        self.assertEqual(
            {item["id"] for item in leg_focus._pick_leg_accessory(
                "光腿", leg_focus.LEGFOCUS_OUTFIT_POOL[0], {},
                leg_focus.LEGFOCUS_POSE_BY_ID["standing_weight_shift"],
                "大腿环", force_ids=["thigh_cat_bat_paw", "thigh_white_cat_chain"],
            )},
            {"thigh_cat_bat_paw"},
        )

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
            force_pose="seated_knees_offset",
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
        self.assertNotIn("允许上身、肩颈和头脸入画", prompt_zh)
        face_mode = re.search(r"【face_mode:(none|half|full)】", action).group(1)
        if face_mode == "none":
            self.assertIn("头部与面部保持画外", prompt_zh)
        else:
            self.assertIn(f"【face_mode:{face_mode}】", prompt_zh)
        self.assertIn("所选服装原有的露肩、锁骨和腰腹自然保留", prompt_zh)
        self.assertNotIn("头部、脸部和肩部不入镜", prompt_zh)
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
        self.assertIn("【leg_support:flat_ground】", prompt)
        self.assertNotIn("城市天台步道", prompt)
        self.assertNotIn("当前日程场景", prompt)


if __name__ == "__main__":
    unittest.main()
