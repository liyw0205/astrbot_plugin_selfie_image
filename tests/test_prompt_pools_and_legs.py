from __future__ import annotations

import asyncio
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
        from astrbot_plugin_selfie_image.cos.leg_focus import LEGFOCUS_POSE_BY_ID, build_leg_focus_action

        expected_supports = {
            "bed_side_recline": {"bed_surface"},
            "bed_one_knee_up": {"bed_surface", "bed_edge"},
            "sofa_side_fold": {"sofa"},
            "sofa_armrest_asymmetric": {"sofa"},
            "sofa_forward_leg": {"sofa"},
            "sofa_ankle_cross": {"sofa"},
        }
        for pose_id, scene_ids in expected_supports.items():
            self.assertEqual(set(LEGFOCUS_POSE_BY_ID[pose_id]["scene_ids"]), scene_ids)
        action = build_leg_focus_action(force_pose="bed_edge_diagonal", force_camera="selfie")
        self.assertIn("双手仍按姿势支撑床面", action)
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
                force_pose="sofa_ankle_cross",
                force_camera="third",
            )
            second = build_leg_focus_action(
                has_refs=True,
                force_pose="gaming_chair_side_fold",
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

    def test_leg_pool_has_pose_specific_first_and_third_person_indoor_views(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import (
            LEGFOCUS_POSE_POOL,
            LEGFOCUS_SCENE_BY_ID,
            build_leg_focus_action,
        )

        self.assertGreaterEqual(len(LEGFOCUS_POSE_POOL), 14)
        for pose in LEGFOCUS_POSE_POOL:
            self.assertEqual(set(pose["camera_views"]), {"selfie", "third"})
            self.assertTrue(pose["scene_ids"])
            self.assertTrue(all(scene_id in LEGFOCUS_SCENE_BY_ID for scene_id in pose["scene_ids"]))
            for camera in ("selfie", "third"):
                action = build_leg_focus_action(
                    force_pose=pose["id"],
                    force_camera=camera,
                )
                self.assertIn(f"【cam:{camera}】", action)
                self.assertIn("【frame:leg_detail】", action)
                self.assertNotIn("看向镜头", action)
                selected_scene = action.split("本张室内场景：", 1)[1].split("。", 1)[0]
                self.assertNotIn("户外", selected_scene)

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
            action = build_leg_focus_action(force_pose="bed_edge_diagonal", force_camera="third")

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
                    action = build_leg_focus_action(wear, force_pose="sofa_ankle_cross", force_camera="third")
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
            force_pose="sofa_armrest_asymmetric",
            force_camera="third",
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
        self.assertIn("头部、脸部和肩膀不入镜", prompt_zh)
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
            force_pose="bed_edge_diagonal",
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
        self.assertIn("服装按本次锁定搭配执行", prompt_zh)
        self.assertNotIn("默认使用近景下半身裁切", prompt_zh)
        self.assertNotIn("正对镜头时自然看向镜头", prompt_zh)
        self.assertNotIn("腰线至鞋履", prompt_zh)
        self.assertIn("Full-length indoor outfit contract.", prompt_en)
        self.assertNotIn("waist to shoes", prompt_en.lower())

    def test_leg_contract_selfie_defines_foot_target_before_the_legacy_branch(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import build_leg_focus_action
        from astrbot_plugin_selfie_image.features.persona import PersonaManager

        action = build_leg_focus_action(
            force_pose="bed_edge_diagonal",
            force_camera="selfie",
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

        self.assertIn("第一人称腰线至足部照片", prompt)
        self.assertIn(action, prompt)

    def test_english_leg_prompt_uses_one_action_contract_without_legacy_pose_or_outfit(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import build_leg_focus_action
        from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt

        action = build_leg_focus_action(
            force_pose="bed_edge_diagonal",
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
            force_pose="bed_edge_side_dangle",
            force_camera="third",
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
        self.assertTrue(any(f"【场景池·{title}】" in prompt for title in ("床沿", "卧室床边座面")))
        self.assertNotIn("城市天台步道", prompt)
        self.assertNotIn("当前日程场景", prompt)


if __name__ == "__main__":
    unittest.main()
