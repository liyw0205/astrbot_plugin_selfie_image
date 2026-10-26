"""Regression tests for COS prompt-pool template hygiene.

Covers two fixed problems:
1. The shared craft sentence listed 制服/兔耳/领带 as guaranteed physical
   details, leaking bunny ears and neckties into outfits that have neither.
   It was rewritten to 服装、发饰和道具 / 纽扣、袜鞋.
2. The C.C. 白虎礼服 prompt asked for a full-length mirror selfie while the
   shared negative contract forbids a second person, producing unstable frames.

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
        self.assertEqual(len(self.items), 270)
        ids = [str(item.get("id") or "") for item in self.items]
        self.assertEqual(len(ids), len(set(ids)))
        self.carriers = [
            item
            for item in self.items
            if CRAFT_SENTENCE in str(item.get("prompt") or "")
        ]

    def test_rewritten_craft_sentence_count_is_stable(self) -> None:
        self.assertEqual(len(self.carriers), 33)

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


class MirrorSelfieContradictionTest(unittest.TestCase):
    def test_cc_white_tiger_prompt_uses_direct_selfie(self) -> None:
        item = get_cos_look_by_id("cc_white_tiger_pink_gold_dress")
        self.assertTrue(item)
        prompt = str(item.get("prompt") or "")
        self.assertNotIn("全身镜中自拍", prompt)
        self.assertNotIn("镜子", prompt)
        self.assertNotIn("镜中", prompt)
        self.assertNotIn("全身镜", prompt)
        self.assertIn("自拍", prompt)
        self.assertIn("手持自拍视角", prompt)
        # Pose wording survives the rewrite.
        self.assertIn("身体微侧", prompt)


if __name__ == "__main__":
    unittest.main()
