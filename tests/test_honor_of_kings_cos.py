from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_selfie_image.cos.cos_looks import (
    COS_FRAMING_CLASSES,
    COS_LOOK_SETS,
    COS_RANDOM_POSE_CLASSES,
    COS_RANDOM_SCENE_CLASSES,
    build_cos_look_action,
    cos_look_compatibility,
    pick_cos_framing,
)
from astrbot_plugin_selfie_image.cos.cos_looks_honor_of_kings import (
    COS_LOOK_COMPATIBILITY_HONOR_OF_KINGS,
    COS_LOOK_SETS_HONOR_OF_KINGS,
    COS_HONOR_OF_KINGS_SCENE_PROFILES,
)


class HonorOfKingsCosCatalogTest(unittest.TestCase):
    def test_all_reviewed_candidates_are_registered_once(self) -> None:
        self.assertEqual(len(COS_LOOK_SETS_HONOR_OF_KINGS), 186)
        self.assertEqual(len(COS_LOOK_COMPATIBILITY_HONOR_OF_KINGS), 186)

        ids = [str(item["id"]) for item in COS_LOOK_SETS_HONOR_OF_KINGS]
        titles = [str(item["title"]) for item in COS_LOOK_SETS_HONOR_OF_KINGS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(titles), len(set(titles)))

        catalog = [item for item in COS_LOOK_SETS if item not in COS_LOOK_SETS_HONOR_OF_KINGS]
        catalog_ids = {str(item.get("id") or "") for item in catalog}
        catalog_titles = {str(item.get("title") or "") for item in catalog}
        self.assertFalse(catalog_ids.intersection(ids))
        self.assertFalse(catalog_titles.intersection(titles))

    def test_skin_titles_use_official_separators(self) -> None:
        by_id = {str(item["id"]): str(item["title"]) for item in COS_LOOK_SETS_HONOR_OF_KINGS}
        self.assertEqual(by_id["hok_zhenji_zhimei_huadequ"], "甄姬·至美·化雀舞")
        self.assertEqual(by_id["hok_wuzetian_shenqi_minghuiyi"], "武则天·神器·明辉仪")
        self.assertEqual(by_id["hok_nuwa_qianqiu_sheng"], "女娲·愿照·千秋盛")
        self.assertEqual(by_id["hok_shangguan_shenqi_wanxiangbi"], "上官婉儿·神器·万象笔")

    def test_costume_prompts_do_not_override_random_staging(self) -> None:
        conflicting_controls = re.compile(
            r"(?:自拍|镜头|机位|视角|构图|景别|仰拍|俯拍|姿势|动作|站立|坐姿|蹲下|回眸|背对镜头)"
        )
        for item in COS_LOOK_SETS_HONOR_OF_KINGS:
            prompt = str(item["prompt"])
            self.assertTrue(prompt.startswith("身份与参考图边界："), str(item["id"]))
            self.assertIn(f"「{item['title']}」", prompt, str(item["id"]))
            self.assertTrue(
                any(
                    section in prompt
                    for section in (
                        "配色与整体辨识：", "假发与头部固定饰件：", "上身衣装与层次：",
                        "腰线及上下装连接：", "下装轮廓与层次：", "袜履与鞋型：",
                        "面料、纹样与固定饰件材质：",
                    )
                ),
                str(item["id"]),
            )
            self.assertIn("还原边界：", prompt, str(item["id"]))
            self.assertIn("未明确或参考立绘中无法确认的部件不补造", prompt, str(item["id"]))
            self.assertNotIn("按该皮肤专属原设", prompt, str(item["id"]))
            self.assertNotIn("真人可穿戴COS", prompt, str(item["id"]))
            self.assertGreaterEqual(len(prompt), 250, str(item["id"]))
            self.assertIsNone(conflicting_controls.search(prompt), str(item["id"]))
            self.assertNotRegex(prompt, r"(?:背景|布景|场景|雪景|海滩背景|野外场景)")

    def test_anime_appearance_does_not_reintroduce_live_action_or_reference_medium(self) -> None:
        action = build_cos_look_action(
            "",
            has_refs=True,
            camera="third",
            appearance_type="anime",
            force_id="hok_shangguan_liangzhu",
        )
        self.assertIn("禁止真人照片质感", action)
        self.assertIn("视觉媒介遵循本次形象类型设置", action)
        self.assertNotIn("真人可穿戴COS", action)
        self.assertNotIn("保留参考图本人的性别、年龄感、脸部身份、五官比例和视觉媒介", action)

    def test_every_look_has_a_valid_nonempty_compatible_profile(self) -> None:
        for item in COS_LOOK_SETS_HONOR_OF_KINGS:
            look_id = str(item["id"])
            config = COS_LOOK_COMPATIBILITY_HONOR_OF_KINGS[look_id]
            self.assertTrue(config["pose_ids"], look_id)
            self.assertTrue(config["scene_ids"], look_id)
            self.assertTrue(set(config["pose_ids"]).issubset(COS_RANDOM_POSE_CLASSES), look_id)
            self.assertTrue(set(config["scene_ids"]).issubset(COS_RANDOM_SCENE_CLASSES), look_id)

            profile = cos_look_compatibility(look_id)
            self.assertTrue(profile["recommended_pose_ids"], look_id)
            self.assertTrue(profile["recommended_scene_ids"], look_id)
            self.assertTrue(
                all(profile["scene_pose_compatibility"].values()),
                look_id,
            )

    def test_theme_profiles_keep_distinct_scene_families(self) -> None:
        japanese = set(COS_LOOK_COMPATIBILITY_HONOR_OF_KINGS["hok_daqiao_yishi_wunv"]["scene_ids"])
        marine = set(COS_LOOK_COMPATIBILITY_HONOR_OF_KINGS["hok_duoliya_renyu_zhige"]["scene_ids"])
        winter = set(COS_LOOK_COMPATIBILITY_HONOR_OF_KINGS["hok_gongsunli_qixuelingzhu"]["scene_ids"])
        traditional = set(COS_LOOK_COMPATIBILITY_HONOR_OF_KINGS["hok_shangguan_liangzhu"]["scene_ids"])
        modern = set(COS_LOOK_COMPATIBILITY_HONOR_OF_KINGS["hok_sunshangxiang_yinni_shanyao"]["scene_ids"])
        ice_blue = set(COS_LOOK_COMPATIBILITY_HONOR_OF_KINGS["hok_garo_taihua"]["scene_ids"])
        snow_gown = set(COS_LOOK_COMPATIBILITY_HONOR_OF_KINGS["hok_wangzhaojun_bingxuezhihua"]["scene_ids"])

        self.assertIn("japanese_ink_room", japanese)
        self.assertNotIn("japanese_ink_room", marine)
        self.assertIn("aquarium_tunnel", marine)
        self.assertIn("snowy_courtyard", winter)
        self.assertNotIn("snowy_courtyard", traditional)
        self.assertNotIn("old_city_rooftop", traditional)
        self.assertIn("rainy_neon_street", modern)
        self.assertNotIn("aquarium_tunnel", modern)
        self.assertNotIn("snowy_courtyard", ice_blue)
        self.assertIn("snowy_courtyard", snow_gown)
        self.assertEqual(set(traditional), set(COS_HONOR_OF_KINGS_SCENE_PROFILES["traditional"]))

    def test_shangguan_costumes_have_explicit_identity_cues_and_face_favoring_frames(self) -> None:
        by_id = {str(item["id"]): str(item["prompt"]) for item in COS_LOOK_SETS_HONOR_OF_KINGS}
        cases = {
            "hok_shangguan_liangzhu": ("主色明确为冰蓝", "大型卷云形蓝色发饰", "深绿色玉叶状饰件"),
            "hok_shangguan_tianlang_huimeng": ("紫色短发", "浅灰白色宽松短款外套", "街头战队感"),
            "hok_shangguan_shenqi_wanxiangbi": ("银白色长发", "金色发冠", "不写成朱红主色"),
        }
        for look_id, cues in cases.items():
            prompt = by_id[look_id]
            self.assertTrue(all(cue in prompt for cue in cues), look_id)
            weights = COS_LOOK_COMPATIBILITY_HONOR_OF_KINGS[look_id]["framing_weights"]
            profile = cos_look_compatibility(look_id)
            self.assertEqual(profile["recommended_framing_weights"], weights)
            self.assertGreater(weights["front_half"], weights["environmental"])
            self.assertIn("eye_level_full", COS_FRAMING_CLASSES)

    def test_framing_weights_bias_close_shots_without_removing_full_body_options(self) -> None:
        with patch(
            "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
            side_effect=lambda choices: choices[0],
        ) as choose:
            result = pick_cos_framing(
                pose_id="standing_detail",
                scene_id="photo_studio",
                camera="third",
                allowed_framings=("front_half", "eye_level_full"),
                framing_weights={"front_half": 4},
            )
        self.assertIn(result["view_id"], {"front_half", "eye_level_full"})
        weighted_pool = choose.call_args_list[0].args[0]
        self.assertEqual(weighted_pool.count("front_half"), 4)
        self.assertEqual(weighted_pool.count("eye_level_full"), 1)


if __name__ == "__main__":
    unittest.main()
