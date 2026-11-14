"""Record submission telemetry must preserve persistence and wait semantics."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from astrbot_plugin_selfie_image.generation import generation_store
from astrbot_plugin_selfie_image.generation.generation_store import GenerationStoreMixin
from astrbot_plugin_selfie_image.generation.record_metrics import RecordCommitMetrics
from astrbot_plugin_selfie_image.webui.services import build_health_payload


def test_empty_commit_health_has_no_samples_or_errors():
    health = GenerationStoreMixin().get_record_commit_health()
    assert health["scope"] == "process"
    assert health["status"] == "idle"
    assert health["completed_count"] == health["success_count"] == health["failure_count"] == 0
    for key in ("success_latency_ms", "failure_latency_ms"):
        assert health[key] == {
            stage: {"samples": 0, "p50": 0.0, "p95": 0.0, "max": 0.0}
            for stage in ("queue_wait", "write", "total")
        }


@pytest.mark.parametrize("failed", [False, True])
def test_sync_submission_reports_end_to_end_latency_and_preserves_exception(monkeypatch, failed):
    clock = iter((1.0, 1.0, 1.125))
    monkeypatch.setattr(generation_store, "time", SimpleNamespace(perf_counter=lambda: next(clock)))
    plugin = GenerationStoreMixin()
    captured = []
    failure = OSError("disk full api_key=private-value")

    def commit(record):
        captured.append(record)
        record["nested"]["value"] = "changed"
        if failed:
            raise failure

    plugin._commit_generation_records = commit
    original = {"nested": {"value": "original"}}
    if failed:
        with pytest.raises(OSError) as raised:
            plugin._record_task(original)
        assert raised.value is failure
    else:
        plugin._record_task(original)
    assert len(captured) == 1
    assert original == {"nested": {"value": "original"}}
    health = plugin.get_record_commit_health()
    assert health["success_count"] == int(not failed)
    assert health["failure_count"] == int(failed)
    observed = health["failure_latency_ms" if failed else "success_latency_ms"]
    assert observed["queue_wait"] == {"samples": 1, "p50": 0.0, "p95": 0.0, "max": 0.0}
    assert observed["write"] == observed["total"] == {"samples": 1, "p50": 125.0, "p95": 125.0, "max": 125.0}
    assert health["status"] == ("error" if failed else "ok")
    if failed:
        assert health["last_failure_type"] == "OSError"
        assert health["last_failure_at"]
        assert "private-value" not in str(health)


def test_failed_attempts_are_separate_and_sample_retention_is_bounded():
    metrics = RecordCommitMetrics()
    metrics.observe(0, 2, 5, OSError("failed"))
    for index in range(metrics.SAMPLE_LIMIT - 1):
        metrics.observe(index, index + 0.25, index + 1)
    health = metrics.snapshot()
    assert health["completed_count"] == metrics.SAMPLE_LIMIT
    assert health["success_latency_ms"]["total"]["samples"] == metrics.SAMPLE_LIMIT - 1
    assert health["failure_latency_ms"]["total"]["p95"] == 5000.0
    metrics.observe(1, 1, 1.5)
    health = metrics.snapshot()
    assert health["status"] == "ok"
    assert health["completed_count"] == metrics.SAMPLE_LIMIT + 1
    assert health["failure_count"] == 1
    assert health["failure_latency_ms"]["total"]["samples"] == 0
    assert health["success_latency_ms"]["total"]["samples"] == metrics.SAMPLE_LIMIT
    assert health["last_failure_reason"] == "failed"
    health["success_latency_ms"]["total"]["samples"] = -1
    assert metrics.snapshot()["success_latency_ms"]["total"]["samples"] == metrics.SAMPLE_LIMIT


def test_concurrent_completions_keep_exact_counts_and_consistent_snapshots():
    metrics = RecordCommitMetrics()

    def observe(index):
        metrics.observe(0, 0.01, 0.02, OSError("failed") if index % 2 else None)
        health = metrics.snapshot()
        assert health["completed_count"] == health["success_count"] + health["failure_count"]
        assert health["success_latency_ms"]["total"]["samples"] + health["failure_latency_ms"]["total"]["samples"] <= metrics.SAMPLE_LIMIT

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(observe, range(300)))
    health = metrics.snapshot()
    assert health["success_count"] == health["failure_count"] == 150
    assert health["completed_count"] == 300


def test_executor_queue_wait_is_measured_before_writer_starts(monkeypatch):
    async def exercise():
        clock = [1.0]
        monkeypatch.setattr(generation_store, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        occupied = threading.Event()
        release = threading.Event()

        def occupy():
            occupied.set()
            assert release.wait(3)

        blocker = loop.run_in_executor(None, occupy)
        plugin = GenerationStoreMixin()
        written = []

        def commit(record):
            written.append(record)
            clock[0] = 8.0

        plugin._commit_generation_records = commit
        try:
            while not occupied.is_set():
                await asyncio.sleep(0)
            plugin._record_task({"task_id": "task", "generated_image_paths": ["output.png"]})
            await asyncio.sleep(0)
            assert not written
            assert plugin.get_record_commit_health()["completed_count"] == 0
            clock[0] = 3.0
            release.set()
            await blocker
            await plugin._wait_for_record_commits("task")
            health = plugin.get_record_commit_health()
            durations = health["success_latency_ms"]
            assert durations["queue_wait"]["p95"] == 2000.0
            assert durations["write"]["p95"] == 5000.0
            assert durations["total"]["p95"] == 7000.0
            assert health["completed_count"] == 1
            assert not plugin._record_commit_tasks
            assert not plugin._pending_record_commits
            assert not plugin._pending_image_record_commits
        finally:
            release.set()
            await blocker

    asyncio.run(exercise())


@pytest.mark.parametrize("wait_kind", ["timeout", "cancel"])
def test_wait_interruption_keeps_writer_running_and_counts_completion_once(wait_kind):
    async def exercise():
        plugin = GenerationStoreMixin()
        started = threading.Event()
        release = threading.Event()

        def commit(_record):
            started.set()
            assert release.wait(3)

        plugin._commit_generation_records = commit
        plugin._record_task({"task_id": "task"})
        try:
            assert await asyncio.to_thread(started.wait, 1)
            if wait_kind == "timeout":
                await plugin._wait_for_record_commits("task", timeout=0.01)
            else:
                waiter = asyncio.create_task(plugin._wait_for_record_commits("task"))
                await asyncio.sleep(0)
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
            assert plugin.get_record_commit_health()["completed_count"] == 0
            assert all(not task.cancelled() for task in plugin._record_commit_tasks)
        finally:
            release.set()
            await plugin._wait_for_record_commits("task")
        assert plugin.get_record_commit_health()["success_count"] == 1

    asyncio.run(exercise())


def test_failed_async_submission_is_health_visible_without_escaping_wait():
    async def exercise():
        plugin = GenerationStoreMixin()

        def commit(_record):
            raise OSError("token=private-value")

        plugin._commit_generation_records = commit
        plugin._record_task({"task_id": "task"})
        await plugin._wait_for_record_commits("task")
        health = plugin.get_record_commit_health()
        assert health["failure_count"] == 1
        assert health["success_latency_ms"]["total"]["samples"] == 0
        assert health["failure_latency_ms"]["total"]["samples"] == 1
        assert "private-value" not in str(health)

    asyncio.run(exercise())


@pytest.mark.parametrize("status,expected", [("idle", "ok"), ("ok", "ok"), ("error", "degraded")])
def test_health_endpoint_includes_commit_metrics_and_degrades_failed_writes(status, expected):
    health = {"status": status, "success_count": 2, "failure_count": 1, "token": "private-value"}
    plugin = SimpleNamespace(
        config=SimpleNamespace(), _cache_stats=lambda: (0, 0), get_record_commit_health=lambda: health,
    )
    payload = build_health_payload(plugin)
    assert payload["record_commit_health"]["status"] == status
    assert payload["record_commit_health"]["success_count"] == 2
    assert payload["status"] == expected
    assert "private-value" not in str(payload)
    assert health["token"] == "private-value"


def test_faulty_commit_health_hook_keeps_diagnostics_available():
    def fail():
        raise RuntimeError("token=private-value")

    plugin = SimpleNamespace(config=SimpleNamespace(), _cache_stats=lambda: (0, 0), get_record_commit_health=fail)
    payload = build_health_payload(plugin)
    assert payload["status"] == "degraded"
    assert payload["record_commit_health"]["status"] == "error"
    assert "private-value" not in str(payload)
