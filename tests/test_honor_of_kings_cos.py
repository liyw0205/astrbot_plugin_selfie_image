from __future__ import annotations

import unittest

from astrbot_plugin_selfie_image.cos.cos_looks_loader import (
    COS_JSON_LEGACY_GROUPS,
    COS_JSON_LOOKS_BY_ID,
    get_json_legacy_group,
    validate_json_catalog,
)


class TestHonorOfKingsCosCatalog(unittest.TestCase):
    def test_catalog_is_json_owned_and_references_valid_looks(self) -> None:
        look_ids = COS_JSON_LEGACY_GROUPS["honor_of_kings"]
        looks = get_json_legacy_group("honor_of_kings")

        self.assertGreater(len(looks), 100)
        self.assertEqual(len(look_ids), len(set(look_ids)))
        self.assertEqual({look["id"] for look in looks}, set(look_ids))
        self.assertTrue(all(look_id in COS_JSON_LOOKS_BY_ID for look_id in look_ids))
        self.assertEqual(validate_json_catalog(), [])

    def test_look_records_keep_costume_data_separate_from_staging(self) -> None:
        staging_fields = {
            "pose_id", "scene_id", "framing_id", "pose_ids", "scene_ids",
            "style_ids", "expression_ids", "framing_ids", "makeup_ids",
            "compositions", "camera", "orientation", "angle",
        }
        for look in get_json_legacy_group("honor_of_kings"):
            self.assertTrue(look["title"])
            self.assertTrue(look["prompt"])
            self.assertEqual(look["cos_type"], "王者荣耀")
            self.assertFalse(staging_fields.intersection(look), look["id"])

    def test_catalog_look_ids_are_unique_across_all_groups(self) -> None:
        all_ids = [look_id for group in COS_JSON_LEGACY_GROUPS.values() for look_id in group]
        self.assertEqual(len(all_ids), len(set(all_ids)))


if __name__ == "__main__":
    unittest.main()
