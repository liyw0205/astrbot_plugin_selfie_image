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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astrbot_plugin_selfie_image.cos.cos_looks import COS_LOOK_SETS, get_cos_look_by_id

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
        # Kayoko keeps its own necktie description ahead of the shared spec.
        kayoko = by_id["blue_archive_kayoko"]
        own = kayoko.split(CRAFT_MARKER, 1)[0]
        self.assertIn("领带", own)
        # Shaoluo keeps its own bunny-ear hair accessory intent.
        shaoluo = by_id["shaoluo_pink_white_rabbit"]
        self.assertIn("兔耳", shaoluo.split(CRAFT_MARKER, 1)[0])

    def test_negative_contract_still_once_per_prompt(self) -> None:
        for item in self.items:
            prompt = str(item.get("prompt") or "")
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


if __name__ == "__main__":
    unittest.main()
