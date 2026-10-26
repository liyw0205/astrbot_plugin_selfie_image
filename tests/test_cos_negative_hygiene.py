"""Regression tests for the new COS batch and negative-prompt hygiene.

Three guarantees:
1. The 20 new outfits are present with unique ids, sane length, and the
   shared negative contract appearing exactly once after normalization.
2. Generic exclusions live in ONE place (_COS_COMMON_NEGATIVE_PROMPT) and are
   re-appended by normalize_cos_prompt_negatives(), so source records must not
   duplicate them.
3. Outfit-specific exclusions stay on outfits that actually risk them.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astrbot_plugin_selfie_image.cos.cos_looks import (
    _COS_COMMON_NEGATIVE_PROMPT,
    COS_LOOK_SETS,
    get_cos_look_by_id,
    normalize_cos_prompt_negatives,
)

NEW_IDS = (
    "furina_white_summer_dress",
    "nahida_green_strappy_dress",
    "sparkle_red_strappy_dress",
    "black_swan_velvet_halter",
    "changli_summer_swim",
    "cartethyia_white_lace_swim",
    "blue_archive_hoshino_white_shirt_dress",
    "shiroko_sport_vest_shorts",
    "blue_archive_yuuka_home_camisole",
    "blue_archive_izuna_yukata",
    "azur_lane_agir_black_monokini",
    "azur_lane_cheshire_white_lace_swim",
    "azur_lane_sirius_navy_halter",
    "original_white_shirt_loose_tie",
    "original_knit_cardigan_camisole",
    "original_wa_lift_yukata_short",
    "original_tennis_pleated_skirt",
    "original_satin_slip_dress",
    "original_swim_with_shirt",
    "original_lace_slip_nightie",
)


class NewBatchTest(unittest.TestCase):
    def test_all_twenty_are_registered(self) -> None:
        for look_id in NEW_IDS:
            self.assertIsNotNone(get_cos_look_by_id(look_id), look_id)

    def test_new_ids_and_titles_are_unique(self) -> None:
        ids = [item["id"] for item in COS_LOOK_SETS]
        titles = [item["title"] for item in COS_LOOK_SETS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(titles), len(set(titles)))

    def test_new_prompts_are_well_formed(self) -> None:
        for look_id in NEW_IDS:
            item = get_cos_look_by_id(look_id)
            prompt = str(item["prompt"])
            self.assertGreaterEqual(len(prompt), 120, look_id)
            self.assertTrue(item["cos_type"], look_id)
            self.assertIn("真人", prompt, look_id)

    def test_new_prompts_carry_contract_exactly_once(self) -> None:
        for look_id in NEW_IDS:
            final = normalize_cos_prompt_negatives(get_cos_look_by_id(look_id)["prompt"])
            self.assertEqual(final.count(_COS_COMMON_NEGATIVE_PROMPT), 1, look_id)


class GenericNegativeNotDuplicatedTest(unittest.TestCase):
    """The shared contract is appended at runtime, so source must not repeat it."""

    def _source_records(self):
        """Parse prompts straight from the module source, before runtime normalization."""
        import json
        import re

        text = (ROOT / "cos" / "cos_looks.py").read_text(encoding="utf-8")
        records = []
        for match in re.finditer(r'\{\s*"id":\s*"([^"]+)",.*?"prompt":\s*("[^"]*")', text, re.S):
            records.append({"id": match.group(1), "prompt": json.loads(match.group(2))})
        return records

    def test_source_records_do_not_repeat_shared_contract(self) -> None:
        offenders = [
            rec["id"]
            for rec in self._source_records()
            if _COS_COMMON_NEGATIVE_PROMPT in rec["prompt"]
        ]
        self.assertEqual(offenders, [], f"shared contract hard-coded in source: {offenders}")

    def test_normalization_still_appends_contract_once(self) -> None:
        for item in COS_LOOK_SETS:
            final = normalize_cos_prompt_negatives(item["prompt"])
            self.assertEqual(final.count(_COS_COMMON_NEGATIVE_PROMPT), 1, item["id"])

    def test_normalization_is_idempotent(self) -> None:
        for item in COS_LOOK_SETS:
            once = normalize_cos_prompt_negatives(item["prompt"])
            twice = normalize_cos_prompt_negatives(once)
            self.assertEqual(once, twice, item["id"])


class OutfitSpecificNegativeTest(unittest.TestCase):
    """Exclusions must stay where the risk actually is."""

    def test_no_duplicate_negative_terms_inside_one_prompt(self) -> None:
        import re

        offenders = {}
        for item in COS_LOOK_SETS:
            prompt = str(item.get("prompt") or "")
            for neg in re.findall(r"[^。；，]*(?:不要|禁止|不得|不出现)[^。；]*", prompt):
                for term in re.findall(r"[\u4e00-\u9fa5A-Za-z]{2,}", neg):
                    pass
            neg_text = "".join(
                re.findall(r"[^。；]*(?:不要|禁止|不得|不出现|不携带)[^。；]*", prompt)
            )
            seen = {}
            for term in re.findall(r"[\u4e00-\u9fa5]{2,6}", neg_text):
                seen[term] = seen.get(term, 0) + 1
            dup = {k: v for k, v in seen.items() if v > 1}
            if dup:
                offenders[item["id"]] = dup
        self.assertEqual(offenders, {}, f"duplicated negative terms: {offenders}")

    def test_swimwear_outfits_do_not_ban_the_only_thing_they_are(self) -> None:
        for look_id in (
            "azur_lane_agir_black_monokini",
            "azur_lane_cheshire_white_lace_swim",
            "azur_lane_sirius_navy_halter",
            "changli_summer_swim",
            "cartethyia_white_lace_swim",
            "original_swim_with_shirt",
        ):
            prompt = str(get_cos_look_by_id(look_id)["prompt"])
            self.assertNotIn("不要泳装", prompt, look_id)
            self.assertNotIn("不要比基尼", prompt, look_id)


if __name__ == "__main__":
    unittest.main()
