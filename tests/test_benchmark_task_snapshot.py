from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


_SPEC = spec_from_file_location(
    "benchmark_task_snapshot",
    Path(__file__).resolve().parents[1] / "scripts" / "benchmark_task_snapshot.py",
)
_BENCHMARK = module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_BENCHMARK)


def test_task_snapshot_benchmark_reports_merge_and_terminal_matrix():
    report = _BENCHMARK.run_benchmark(
        (1, 3), samples=1, terminal_concurrency=True, delay=0.05, timeout=2.0
    )

    assert report["fixture"]["temporary_directory_only"] is True
    assert report["fixture"]["task_counts"] == [1, 3]
    assert len(report["cases"]) == 4
    for case in report["cases"]:
        assert case["actual_writes"] >= 1
        assert case["write_bytes_total"] >= case["final_file_size_bytes"]["last"]
        assert case["write_latency_ms"]["samples"] == case["actual_writes"]
        assert case["reloadable_samples"] == 1
        assert case["final_file_size_bytes"]["last"] > 0

    active_one, terminal_one, active_three, terminal_three = report["cases"]
    assert active_one["scenario"] == "active_timer_merge"
    assert active_one["actual_writes"] == 1
    assert active_one["merge_rate"] == 0.0
    assert active_one["caller_completion_latency_ms"]["samples"] == 0
    assert active_one["batch_completion_ms"]["samples"] == 0
    assert active_one["terminal_update_max_concurrency"] == 0
    assert terminal_one["scenario"] == "terminal_concurrency"
    assert terminal_one["actual_writes"] == 1
    assert terminal_one["complete_status_samples"] == 1
    assert terminal_one["caller_completion_latency_ms"]["samples"] == 1
    assert terminal_one["batch_completion_ms"]["samples"] == 1
    assert terminal_one["terminal_update_max_concurrency"] == 1
    assert active_three["actual_writes"] == 1
    assert active_three["coalesced_update_count"] == 2
    assert active_three["merge_rate"] == pytest.approx(2 / 3, abs=1e-6)
    assert active_three["caller_completion_latency_ms"]["samples"] == 0
    assert active_three["batch_completion_ms"]["samples"] == 0
    assert active_three["terminal_update_max_concurrency"] == 0
    assert terminal_three["actual_writes"] == 3
    assert terminal_three["forced_flush_count"] == 3
    assert terminal_three["complete_status_samples"] == 1
    assert terminal_three["caller_completion_latency_ms"]["samples"] == 3
    assert terminal_three["batch_completion_ms"]["samples"] == 1
    assert terminal_three["terminal_update_max_concurrency"] >= 1
    assert terminal_three["batch_completion_ms"]["max"] >= terminal_three["caller_completion_latency_ms"]["max"]
