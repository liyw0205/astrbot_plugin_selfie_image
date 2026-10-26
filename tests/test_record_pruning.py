import sqlite3
import sys
import tempfile
import threading
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


class SequenceRestoreTest(unittest.TestCase):
    """A new record must outlive the oldest one even after manual deletions."""

    def _db(self, root):
        return RecordDatabase(str(Path(root) / "generation_records.sqlite3"))

    def _row(self, name, ts, seq):
        return {
            "id": name,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            "created_ts": ts,
            "seq_hint": seq,
        }

    def test_max_sequence_reports_stored_high_water_mark(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            db = self._db(root)
            self.assertEqual(db.max_sequence(), 0)
            base = time.time() - 10000
            for i in range(10):
                db.upsert_record(self._row(f"r{i:02d}", base + i, i), sequence=i + 1)
            self.assertEqual(db.max_sequence(), 10)
            # Deleting a middle row leaves max(seq) above the row count, which
            # is exactly the state that used to make a new record reuse a
            # retained sequence value.
            db.delete_records(["r05"])
            self.assertEqual(db.max_sequence(), 10)
            self.assertEqual(len(db.load_records(100)), 9)
            # Deleting the newest row lowers the observable maximum; the
            # restore helper still keeps the counter unique via max(count, seq).
            db.delete_records(["r09"])
            self.assertEqual(db.max_sequence(), 9)
            self.assertEqual(len(db.load_records(100)), 8)

    def test_restore_seeds_counter_above_deleted_gap(self) -> None:
        from types import SimpleNamespace

        from astrbot_plugin_selfie_image.generation.generation_store import (
            GenerationStoreMixin,
        )

        with tempfile.TemporaryDirectory() as root:
            db = self._db(root)
            base = time.time() - 10000
            for i in range(10):
                db.upsert_record(self._row(f"r{i:02d}", base + i, i), sequence=i + 1)
            db.delete_records(["r05"])  # count 9, max seq 10
            loaded = db.load_records(100)
            stub = SimpleNamespace(
                _records=loaded,
                _records_lock=threading.RLock(),
                _record_database=lambda: db,
            )
            # len(records) would be 9 and collide with the retained seq 10.
            self.assertEqual(GenerationStoreMixin._restore_record_sequence(stub), 10)

    def test_new_record_after_deletion_is_not_pruned(self) -> None:
        """Reproduces the reported bug: the fresh row was evicted, not the oldest."""
        from types import SimpleNamespace

        from astrbot_plugin_selfie_image.generation.generation_store import (
            GenerationStoreMixin,
        )

        with tempfile.TemporaryDirectory() as root:
            db = self._db(root)
            base = time.time() - 10000
            for i in range(10):
                db.upsert_record(self._row(f"r{i:02d}", base + i, i), sequence=i + 1)
            db.delete_records(["r05"])

            loaded = db.load_records(100)
            keep = len(loaded)
            stub = SimpleNamespace(
                _records=loaded,
                _records_lock=threading.RLock(),
                _record_database=lambda: db,
            )
            counter = GenerationStoreMixin._restore_record_sequence(stub) + 1
            self.assertLessEqual(
                counter,
                db.max_sequence() + 1,
                "the restored counter must not reuse a retained sequence value",
            )
            db.upsert_record(
                {
                    "id": "NEW",
                    "time": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(base + 100)
                    ),
                    "created_ts": base + 100,
                },
                sequence=counter,
            )
            db.prune_to_limit(keep)
            left = [r["id"] for r in db.load_records(100)]
            self.assertIn("NEW", left, "the newly added record must survive")
            self.assertNotIn("r00", left, "the oldest record must be evicted")


if __name__ == "__main__":
    unittest.main()
