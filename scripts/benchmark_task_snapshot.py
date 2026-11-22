"""Benchmark task-snapshot write amplification in temporary fixtures.

The benchmark exercises the current JSON snapshot implementation without
reading or modifying the repository data directory.  It has two scenarios:
progress updates for active tasks (which should merge into one timer flush)
and simultaneous terminal updates (which must flush synchronously).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import runpy
import statistics
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
# The plugin normally imports AstrBot at process start.  The benchmark is an
# offline utility, so reuse the test compatibility stubs instead.
runpy.run_path(str(ROOT / "tests" / "conftest.py"))

from astrbot_plugin_selfie_image.tasks.task_manager import WebTaskMixin


class _BenchmarkTask(WebTaskMixin):
    """Small task mixin host that records each completed snapshot duration."""

    def __init__(self, tasks_path: Path, *, delay: float) -> None:
        self.tasks_path = str(tasks_path)
        self._web_task_lock = threading.RLock()
        self._web_task_terminal_metrics_lock = threading.Lock()
        self._web_tasks: dict[str, dict[str, Any]] = {}
        self.WEB_TASK_SNAPSHOT_DELAY = max(0.001, float(delay))
        self.config = SimpleNamespace()
        self._cache_stats = lambda: (0, 0)
        self.durations_ms: list[float] = []

    def _record_snapshot_duration_locked(self, duration_ms: float) -> None:
        self.durations_ms.append(float(duration_ms))
        super()._record_snapshot_duration_locked(duration_ms)


def _percentile(values: Iterable[float], quantile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return round(ordered[index], 3)


def _latency_summary(values: Iterable[float]) -> dict[str, float | int]:
    samples = [float(value) for value in values]
    if not samples:
        return {"samples": 0, "average": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    return {
        "samples": len(samples),
        "average": round(statistics.fmean(samples), 3),
        "p50": _percentile(samples, 0.50),
        "p95": _percentile(samples, 0.95),
        "p99": _percentile(samples, 0.99),
        "max": round(max(samples), 3),
    }


def _size_summary(values: Iterable[int]) -> dict[str, float | int]:
    samples = [int(value) for value in values]
    if not samples:
        return {"samples": 0, "average": 0.0, "p95": 0, "min": 0, "max": 0, "last": 0}
    return {
        "samples": len(samples),
        "average": round(statistics.fmean(samples), 3),
        "p95": int(_percentile(samples, 0.95)),
        "min": min(samples),
        "max": max(samples),
        "last": samples[-1],
    }


def _task_fixture(count: int) -> dict[str, dict[str, Any]]:
    return {
        f"task-{index:04d}": {
            "task_id": f"task-{index:04d}",
            "status": "running",
            "trace_id": f"trace-{index:04d}",
            "source": "benchmark",
            "request_data": {"prompt": f"benchmark prompt {index}"},
        }
        for index in range(count)
    }


def _wait_for_timer(plugin: _BenchmarkTask, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while plugin.get_task_snapshot_health()["pending"] and time.monotonic() < deadline:
        time.sleep(0.001)
    if plugin.get_task_snapshot_health()["pending"]:
        raise TimeoutError("task snapshot timer did not flush before the deadline")


def _validate_reload(plugin: _BenchmarkTask, expected_count: int, *, terminal: bool) -> tuple[bool, bool]:
    path = Path(plugin.tasks_path)
    if not path.is_file():
        return False, False
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(tasks, dict) or len(tasks) != expected_count:
            return False, False
        persisted_complete = all(
            isinstance(row, dict)
            and str(row.get("status") or "").strip().lower() == ("succeeded" if terminal else "running")
            for row in tasks.values()
        )
        # A real restart intentionally expires active work and writes that
        # decision back. Replay a copy so restart recovery cannot alter the
        # final-file-size measurement for the benchmarked write sequence.
        with tempfile.TemporaryDirectory(prefix="selfie-task-snapshot-replay-") as replay_dir:
            replay_path = Path(replay_dir) / "tasks.json"
            replay_path.write_bytes(raw)
            reloaded = _BenchmarkTask(replay_path, delay=plugin.WEB_TASK_SNAPSHOT_DELAY)
            restored = reloaded._load_web_tasks()
            reloadable = bool(len(restored) == expected_count)
            # ``persisted_complete`` is the meaningful terminal state for
            # active tasks; on restart those tasks are expected to expire.
            restored_complete = all(
                str(row.get("status") or "").strip().lower() == "succeeded"
                for row in restored.values()
            ) if terminal else persisted_complete
            return reloadable, restored_complete
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False, False


def _run_sample(task_count: int, *, terminal: bool, delay: float, timeout: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="selfie-task-snapshot-bench-") as temporary:
        plugin = _BenchmarkTask(Path(temporary) / "tasks.json", delay=delay)
        # Keep every fixture row so retention pruning does not hide a
        # concurrent terminal transition in the completeness check.
        plugin.WEB_TASK_KEEP_LIMIT = max(plugin.WEB_TASK_KEEP_LIMIT, task_count)
        plugin._web_tasks = _task_fixture(task_count)
        errors: list[BaseException] = []
        caller_durations_ms: list[float] = []
        batch_started_at = [0.0]
        batch_duration_ms: float | None = None
        if terminal:
            barrier = threading.Barrier(
                task_count,
                action=lambda: batch_started_at.__setitem__(0, time.perf_counter()),
            )

            def finish(task_id: str) -> None:
                try:
                    barrier.wait(timeout=timeout)
                    caller_started_at = time.perf_counter()
                    plugin._set_web_image_task(
                        task_id,
                        status="succeeded",
                        success=True,
                        result={"success": True, "task_id": task_id},
                    )
                    caller_durations_ms.append(
                        max(0.0, time.perf_counter() - caller_started_at) * 1000.0
                    )
                except BaseException as exc:  # pragma: no cover - reported below
                    errors.append(exc)

            threads = [threading.Thread(target=finish, args=(task_id,)) for task_id in plugin._web_tasks]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=timeout)
            if errors or any(thread.is_alive() for thread in threads):
                raise RuntimeError(f"terminal snapshot benchmark failed: {errors!r}")
            batch_duration_ms = max(
                0.0, time.perf_counter() - batch_started_at[0]
            ) * 1000.0
        else:
            for index, task_id in enumerate(plugin._web_tasks):
                plugin._set_web_image_task(
                    task_id,
                    generation_stage="provider",
                    generation_stage_label=f"provider-{index % 3}",
                )
            _wait_for_timer(plugin, timeout)

        health = plugin.get_task_snapshot_health()
        reloadable, complete = _validate_reload(plugin, task_count, terminal=terminal)
        path = Path(plugin.tasks_path)
        return {
            "actual_writes": int(health["write_count"]),
            "write_bytes_total": int(health["write_bytes_total"]),
            "final_file_size_bytes": path.stat().st_size if path.exists() else 0,
            "write_latency_ms": list(plugin.durations_ms),
            "scheduled_flush_count": int(health["scheduled_flush_count"]),
            "coalesced_update_count": int(health["coalesced_update_count"]),
            "timer_flush_count": int(health["timer_flush_count"]),
            "forced_flush_count": int(health["forced_flush_count"]),
            "skipped_write_count": int(health["skipped_write_count"]),
            "caller_completion_latency_ms": caller_durations_ms,
            "batch_completion_ms": batch_duration_ms,
            "terminal_update_max_concurrency": int(
                health["terminal_update_concurrency"]["max_concurrency"]
            ),
            "reloadable": reloadable,
            "complete_status": complete,
        }


def _aggregate(samples: list[dict[str, Any]], task_count: int, scenario: str) -> dict[str, Any]:
    durations = [duration for sample in samples for duration in sample["write_latency_ms"]]
    caller_durations = [
        duration
        for sample in samples
        for duration in sample["caller_completion_latency_ms"]
    ]
    batch_durations = [
        sample["batch_completion_ms"]
        for sample in samples
        if sample["batch_completion_ms"] is not None
    ]
    writes = [sample["actual_writes"] for sample in samples]
    merge_attempts = [
        sample["scheduled_flush_count"] + sample["coalesced_update_count"]
        for sample in samples
    ]
    merged = [sample["coalesced_update_count"] for sample in samples]
    return {
        "scenario": scenario,
        "tasks": task_count,
        "samples": len(samples),
        "actual_writes": sum(writes),
        "write_bytes_total": sum(sample["write_bytes_total"] for sample in samples),
        "final_file_size_bytes": _size_summary(sample["final_file_size_bytes"] for sample in samples),
        "write_latency_ms": _latency_summary(durations),
        "caller_completion_latency_ms": _latency_summary(caller_durations),
        "batch_completion_ms": _latency_summary(batch_durations),
        "terminal_update_max_concurrency": max(
            (sample["terminal_update_max_concurrency"] for sample in samples), default=0
        ),
        "scheduled_flush_count": sum(sample["scheduled_flush_count"] for sample in samples),
        "coalesced_update_count": sum(merged),
        "timer_flush_count": sum(sample["timer_flush_count"] for sample in samples),
        "forced_flush_count": sum(sample["forced_flush_count"] for sample in samples),
        "skipped_write_count": sum(sample["skipped_write_count"] for sample in samples),
        "merge_rate": round(sum(merged) / max(1, sum(merge_attempts)), 6),
        "reloadable_samples": sum(bool(sample["reloadable"]) for sample in samples),
        "complete_status_samples": sum(bool(sample["complete_status"]) for sample in samples),
        "sample_results": samples,
    }


def run_benchmark(
    task_counts: Iterable[int] = (1, 10, 50, 100),
    *,
    samples: int = 10,
    terminal_concurrency: bool = False,
    delay: float = 0.02,
    timeout: float = 5.0,
) -> dict[str, Any]:
    counts = [int(count) for count in task_counts]
    if not counts or any(count < 1 for count in counts):
        raise ValueError("task_counts must contain positive integers")
    if samples < 1:
        raise ValueError("samples must be positive")
    cases = []
    for count in counts:
        active_samples = [
            _run_sample(count, terminal=False, delay=delay, timeout=timeout)
            for _ in range(samples)
        ]
        cases.append(_aggregate(active_samples, count, "active_timer_merge"))
        if terminal_concurrency:
            terminal_samples = [
                _run_sample(count, terminal=True, delay=delay, timeout=timeout)
                for _ in range(samples)
            ]
            cases.append(_aggregate(terminal_samples, count, "terminal_concurrency"))
    return {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
        },
        "fixture": {
            "task_counts": counts,
            "samples": samples,
            "timer_delay_seconds": delay,
            "terminal_concurrency": terminal_concurrency,
            "temporary_directory_only": True,
        },
        "methodology": {
            "active_timer_merge": "one durable progress update per active task, then wait for the coalescing timer",
            "terminal_concurrency": "one barrier-synchronized succeeded transition per task; terminal writes remain synchronous",
            "retention_window": "fixture keep limit is raised to task count to isolate write amplification from production history pruning",
            "merge_rate": "coalesced_update_count / (scheduled_flush_count + coalesced_update_count)",
            "percentiles": "nearest-rank; small sample counts are descriptive rather than tail-latency guarantees",
        },
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", default="1,10,50,100", help="comma-separated active-task counts")
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--terminal-concurrency", action="store_true")
    parser.add_argument("--delay", type=float, default=0.02, help="timer delay in seconds")
    parser.add_argument("--output", default="", help="optional JSON report path")
    args = parser.parse_args()
    try:
        counts = [int(value.strip()) for value in str(args.tasks).split(",") if value.strip()]
        report = run_benchmark(
            counts,
            samples=args.samples,
            terminal_concurrency=args.terminal_concurrency,
            delay=args.delay,
        )
    except (TypeError, ValueError, RuntimeError, TimeoutError) as exc:
        parser.error(str(exc))
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded, encoding="utf-8")
        print(f"benchmark report written: {destination}")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
