"""Regression tests for COS prompt-pool template hygiene.

Covers two fixed problems:
1. The shared craft sentence listed 制服/兔耳/领带 as guaranteed physical
   details, leaking bunny ears and neckties into outfits that have neither.
   It was rewritten to 服装、发饰和道具 / 纽扣、袜鞋.
2. The C.C. 白虎礼服 prompt used a fixed mirror-selfie template. It now keeps
   the outfit details and reference-person anchor without camera or pose rules.

Note: the Azur Lane rigging outfits carry a DIFFERENT craft sentence (ship-kit
wording); it is intentionally out of scope for the rewritten sentence.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astrbot_plugin_selfie_image.cos.cos_looks import (
    COS_LOOK_SETS,
    build_cos_look_action,
    get_cos_look_by_id,
    is_special_cos_look,
    normalize_cos_prompt_negatives,
)

CRAFT_MARKER = "真人COS摄影执行规范"
CRAFT_SENTENCE = (
    "服装、发饰和道具都像真实手工制作并穿戴在身上，"
    "保留布料厚度、缝线、纽扣、袜鞋和假发纤维"
)
OLD_LEAK_FRAGMENTS = (
    "制服、兔耳、发饰和道具",
    "纽扣、领带、袜鞋",
)


class CosCraftSpecHygieneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.items = list(COS_LOOK_SETS)
        # Pool grows over time; assert a sane lower bound plus uniqueness
        # instead of freezing an exact count that every new batch breaks.
        self.assertGreaterEqual(len(self.items), 270)
        ids = [str(item.get("id") or "") for item in self.items]
        self.assertEqual(len(ids), len(set(ids)))
        self.carriers = [
            item
            for item in self.items
            if CRAFT_SENTENCE in str(item.get("prompt") or "")
        ]

    def test_no_post_hoc_spec_sentences_remain(self) -> None:
        # The per-series 真人COS摄影执行规范 blocks were removed on purpose:
        # they duplicated the shared negative contract and leaked costume words
        # into outfits that do not have them. Records describe only themselves.
        self.assertEqual(len(self.carriers), 0)

    def test_old_leaky_fragments_are_gone_pool_wide(self) -> None:
        for item in self.items:
            if is_special_cos_look(item):
                continue
            prompt = str(item.get("prompt") or "")
            for fragment in OLD_LEAK_FRAGMENTS:
                self.assertNotIn(fragment, prompt, str(item.get("id")))

    def test_craft_sentence_body_has_no_bunny_or_tie(self) -> None:
        # Scope: between the shared marker and the next clause boundary.
        for item in self.carriers:
            prompt = str(item.get("prompt") or "")
            self.assertIn(CRAFT_MARKER, prompt, str(item.get("id")))
            segment = prompt.split(CRAFT_MARKER, 1)[1].split("；", 1)[0]
            self.assertIn(CRAFT_SENTENCE, segment, str(item.get("id")))
            self.assertNotIn("兔耳", segment, str(item.get("id")))
            self.assertNotIn("领带", segment, str(item.get("id")))
            self.assertNotIn("遵纪", segment, str(item.get("id")))

    def test_own_costume_details_are_preserved(self) -> None:
        by_id = {str(item.get("id")): str(item.get("prompt") or "") for item in self.items}
        # Kayoko keeps its own hoodie and red plaid skirt details.
        kayoko = by_id["blue_archive_kayoko"]
        own = kayoko.split(CRAFT_MARKER, 1)[0]
        self.assertIn("连帽卫衣", own)
        self.assertIn("深红格纹短裙", own)
        # Shaoluo keeps its own bunny-ear hair accessory intent.
        shaoluo = by_id["shaoluo_pink_white_rabbit"]
        self.assertIn("兔耳", shaoluo.split(CRAFT_MARKER, 1)[0])

    def test_negative_contract_still_once_per_prompt(self) -> None:
        for item in self.items:
            if is_special_cos_look(item):
                continue
            prompt = normalize_cos_prompt_negatives(str(item.get("prompt") or ""))
            self.assertEqual(prompt.count("不要额外人物"), 1, str(item.get("id")))


class CcWhiteTigerPromptTest(unittest.TestCase):
    def test_prompt_keeps_costume_details_without_a_camera_template(self) -> None:
        item = get_cos_look_by_id("cc_white_tiger_pink_gold_dress")
        self.assertTrue(item)
        prompt = str(item.get("prompt") or "")
        self.assertTrue(prompt.startswith("以形象图中的人为COS主体"))
        self.assertNotIn("全身镜中自拍", prompt)
        self.assertNotIn("镜子", prompt)
        self.assertNotIn("镜中", prompt)
        self.assertNotIn("全身镜", prompt)
        self.assertNotIn("自拍", prompt)
        self.assertIn("金色藤蔓花朵镂空", prompt)
        self.assertIn("粉色缎面高开叉长裙", prompt)


@pytest.mark.parametrize("look_id,required,forbidden", [
    ("roxy_cream", ("薄碎齐刘海", "鬓发", "耳后低位", "大腿上段", "辫尾"), ("未写明自然补全",)),
    ("fate_nero_opera_regalia", ("中分长刘海", "脑后盘成", "发尾收入髻内"), ("后发披散至腰际",)),
    ("mushoku_tensei_sylphiette", ("碎刘海", "耳周短发", "后颈剪短", "发尾细碎外翘"), ("后发披散至腰际",)),
    ("blue_archive_junko", ("红色长双马尾假发", "厚碎齐刘海", "耳后高位", "发尾渐浅橙色"), ("红色短发",)),
    ("zzz_sunna_private_sportswear", ("碎斜刘海", "耳侧短发", "停在颈侧"), ("束起的后发垂至腰际",)),
    ("artsee173_011_welrod_british", ("耳侧绕到后脑", "后颈小马尾", "尾端停在颈根"), ("束起的后发垂至腰际",)),
    ("rem_white_summer_dress", ("覆盖右眼", "露出左眼", "后颈发尾内扣"), ("覆盖左眼",)),
    ("ram_yellow_casual_dress", ("覆盖左眼", "露出右眼", "后颈发尾内扣"), ("覆盖右眼",)),
    ("hi3_rita_long_maid_coat", ("后脑发丝贴顺收至颈根", "发尾齐颈并微微内扣"), ("后发披散至腰际",)),
    ("azur_lane_kaga", ("齐刘海横向平齐并由细发束自然分开", "后区发丝齐至颈根、末端微向内扣"), ("后发披散至腰际",)),
    ("ark_kalstit_researcher", ("额前短碎刘海错落露眉", "后发轻薄分层停在肩头"), ("束起的后发垂至腰际",)),
    ("hi3_seele_asym_dress", ("侧分长刘海斜掠一侧眉眼", "后颈剪出轻薄短层次，发尾微微外翘"), ("后发披散至腰际",)),
    ("cc_white_tiger_pink_gold_dress", ("齐刘海横向修齐并留少量细碎边缘", "后发整体直落到大腿上段、末端收成轻尖"), ("后发披散至腰际",)),
    ("silver_wolf_blue_black_street", ("碎刘海斜向一侧形成不规则短束", "后颈收短并露出轻翘发尾"), ("束起的后发垂至腰际",)),
    ("silver_wolf_tech_vest", ("碎刘海斜向一侧形成不规则短束", "后颈收短并露出轻翘发尾"), ("束起的后发垂至腰际",)),
    ("blue_archive_hoshino_private_gym", ("蓬松厚刘海偏向一侧", "头顶保留一束弯曲翘发"), ("齐颈短发",)),
    ("ark_suzuran_ritual", ("齐刘海均匀落在眉上并留少量细碎发丝", "后发轻薄分层至肩头、尾端微微内弯"), ("束起的后发垂至腰际",)),
])
def test_pool_hair_details_reach_the_action_without_incompatible_lengths(look_id, required, forbidden):
    pool_prompt = get_cos_look_by_id(look_id)["prompt"]
    action = build_cos_look_action("自然站姿 摄影棚", force_id=look_id, camera="third")
    for phrase in required:
        assert phrase in pool_prompt
        assert action.count(phrase) == 1
    for phrase in forbidden:
        assert phrase not in pool_prompt
        assert phrase not in action


if __name__ == "__main__":
    unittest.main()
