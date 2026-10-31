from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astrbot_plugin_selfie_image.cos.cos_looks import get_cos_look_by_id


class ExpansionPromptReviewTest(unittest.TestCase):
    def test_project_sekai_group_name_is_spelled_correctly(self) -> None:
        look = get_cos_look_by_id("prsk_ichika_leoneed")
        self.assertEqual(look["title"], "星乃一歌·Leo/need")

    def test_ram_prompt_describes_her_own_outfit_without_rem_comparison(self) -> None:
        prompt = get_cos_look_by_id("rezero_ram_asym_maid")["prompt"]
        self.assertNotIn("Rem", prompt)
        self.assertNotIn("蓝色长裙", prompt)
        self.assertIn("单侧袖口饰粉色滚边", prompt)

    def test_medusa_blindfold_is_not_mislabeled_as_hair_accessory(self) -> None:
        prompt = get_cos_look_by_id("fate_medusa_black_violet")["prompt"]
        self.assertIn("佩戴黑色窄眼罩", prompt)
        self.assertNotIn("眼罩式发饰", prompt)

    def test_ceobe_prompt_excludes_weapons_unambiguously(self) -> None:
        prompt = get_cos_look_by_id("ark_ceobe_wilderness")["prompt"]
        self.assertIn("不带武器", prompt)
        self.assertNotIn("多把武器", prompt)

    def test_saria_uniform_has_a_clear_trouser_silhouette(self) -> None:
        prompt = get_cos_look_by_id("ark_saria_structured_uniform")["prompt"]
        self.assertIn("深灰色直筒长裤", prompt)
        self.assertNotIn("长裤裙", prompt)


if __name__ == "__main__":
    unittest.main()
