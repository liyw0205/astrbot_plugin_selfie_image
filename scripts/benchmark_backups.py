"""Measure online backups on disposable, synthetic data; never use host data."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import types


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def manager_class(baseline_ref: str | None):
    if not baseline_ref:
        from astrbot_plugin_selfie_image.features.backup_manager import BackupManager
        return BackupManager
    source = subprocess.check_output(
        ["git", "show", f"{baseline_ref}:features/backup_manager.py"], cwd=ROOT, text=True,
    )
    module = types.ModuleType("astrbot_plugin_selfie_image.features._backup_baseline")
    module.__package__ = "astrbot_plugin_selfie_image.features"
    exec(compile(source, "<backup-baseline>", "exec"), module.__dict__)
    return module.BackupManager


def benchmark(size_mib: int, samples: int, baseline_ref: str | None) -> dict:
    cls = manager_class(baseline_ref)
    with tempfile.TemporaryDirectory(prefix="selfie-backup-benchmark-") as temporary:
        root = Path(temporary)
        (root / "selfie_image_config.json").write_text("{}", encoding="utf-8")
        (root / "image_cache").mkdir()
        (root / "image_cache" / "synthetic.bin").write_bytes(random.Random(2026).randbytes(size_mib * 1024 * 1024))
        manager = cls(root)
        try:
            tracemalloc.start()
            started = time.perf_counter()
            task = manager.start_backup(["config", "cache"])
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                finished = manager.task(task["task_id"])
                if finished["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.005)
            else:
                raise RuntimeError("synthetic backup timed out")
            if finished["status"] != "completed":
                raise RuntimeError(finished.get("error") or "synthetic backup failed")
            create_seconds = time.perf_counter() - started
            _current, create_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            manager.list_backups()
            tracemalloc.start()
            timings = []
            for _ in range(samples):
                started = time.perf_counter()
                listing = manager.list_backups()
                timings.append((time.perf_counter() - started) * 1000)
                if listing["count"] != 1:
                    raise RuntimeError("synthetic archive disappeared")
            _current, list_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            return {
                "synthetic": True, "baseline_ref": baseline_ref, "size_mib": size_mib, "samples": samples,
                "create_seconds": round(create_seconds, 4), "create_python_peak_bytes": create_peak,
                "list_p50_ms": round(statistics.median(timings), 4),
                "list_p95_ms": round(sorted(timings)[math.ceil(samples * 0.95) - 1], 4),
                "list_python_peak_bytes": list_peak,
                "limits": "Device-local synthetic data and Python allocations only; not host latency or RSS.",
            }
        finally:
            close = getattr(manager, "close", None)
            if close is not None:
                close()
            else:
                manager._executor.shutdown(wait=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=int, default=16)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--baseline-ref", help="Optional Git revision for a comparison in a separate process")
    args = parser.parse_args()
    if not 1 <= args.size_mib <= 64 or not 1 <= args.samples <= 100:
        parser.error("size-mib must be 1..64 and samples must be 1..100")
    print(json.dumps(benchmark(args.size_mib, args.samples, args.baseline_ref), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
