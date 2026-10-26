import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astrbot_plugin_selfie_image.generation.record_database import RecordDatabase


class PruneOrderTest(unittest.TestCase):
    def _db(self, root):
        return RecordDatabase(str(Path(root) / "generation_records.sqlite3"))

    def _row(self, name, ts):
        return {
            "id": name,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            "created_ts": ts,
        }

    def test_prune_removes_oldest_not_middle(self):
        with tempfile.TemporaryDirectory() as root:
            db = self._db(root)
            base = time.time() - 10000
            records = [self._row(f"r{i:03d}", base + i) for i in range(20)]
            db.replace_records(list(reversed(records)))
            db.prune_to_limit(5)
            left = [r["id"] for r in db.load_records(100)]
            self.assertEqual(left, [f"r{i:03d}" for i in range(19, 14, -1)])

    def test_pinned_and_favourite_survive_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            db = self._db(root)
            base = time.time() - 10000
            for i in range(6):
                db.upsert_record(
                    {
                        "id": f"r{i}",
                        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(base + i)),
                        "created_ts": base + i,
                        "pinned": i == 0,
                    },
                    sequence=i + 1,
                )
            db.prune_to_limit(2)
            left = {r["id"] for r in db.load_records(100)}
            self.assertIn("r0", left, "pinned record must never be pruned")
            self.assertIn("r5", left)
            self.assertIn("r4", left)

    def test_mixed_sign_seq_does_not_delete_middle_band(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            db = self._db(root)
            base = time.time() - 10000
            # Legacy rows kept positive enumerate() seq; upserts used -seq.
            for i in range(10):
                db.upsert_record(
                    {
                        "id": f"legacy{i}",
                        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(base + i)),
                        "created_ts": base + i,
                    },
                    sequence=i + 1,
                )
            for i in range(10, 20):
                db.upsert_record(
                    {
                        "id": f"r{i}",
                        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(base + i)),
                        "created_ts": base + i,
                    },
                    sequence=i + 1,
                )
            db.prune_to_limit(5)
            left = {r["id"] for r in db.load_records(100)}
            # Newest five must be exactly what survives.
            self.assertEqual(left, {f"r{i}" for i in range(15, 20)})


if __name__ == "__main__":
    unittest.main()
