"""Task snapshot byte budgets, diagnostics, and restart compatibility."""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_selfie_image.core.utils import collect_record_cache_paths, load_json_file, save_json_file
from astrbot_plugin_selfie_image.tasks import task_manager
from astrbot_plugin_selfie_image.tasks.task_manager import WebTaskMixin
from astrbot_plugin_selfie_image.webui.services import build_health_payload


def snapshot_plugin(root: Path, *, max_bytes: int = 4096) -> WebTaskMixin:
    plugin = WebTaskMixin()
    plugin.tasks_path = str(root / "tasks.json")
    plugin._web_task_lock = threading.RLock()
    plugin._web_tasks = {}
    plugin.WEB_TASK_SNAPSHOT_MAX_BYTES = max_bytes
    plugin.config = SimpleNamespace()
    plugin._cache_stats = lambda: (0, 0)
    return plugin


def read_snapshot(plugin: WebTaskMixin) -> tuple[dict, bytes]:
    raw = Path(plugin.tasks_path).read_bytes()
    stored = json.loads(raw)
    assert stored["snapshot"]["size_bytes"] == len(raw)
    assert plugin.get_task_snapshot_health()["size_bytes"] == len(raw)
    return stored, raw


def test_budget_uses_real_utf8_bytes_and_keeps_every_large_active_task(tmp_path, monkeypatch):
    plugin = snapshot_plugin(tmp_path, max_bytes=4096)
    warnings = []
    monkeypatch.setattr(task_manager.logger, "warning", lambda *args: warnings.append(args))
    for task_id, status in (("queued", "queued"), ("running", "running")):
        plugin._web_tasks[task_id] = {
            "task_id": task_id,
            "status": status,
            "trace_id": "gen-trace",
            "owner_session": "group:1",
            "owner_user_id": "user-1",
            "quota_reserved": 2,
            "request_fingerprint": "fingerprint",
            "request_data": {"original_prompt": "\u4eba" * 1000},
            "request_image_paths": ["reference.png"],
        }
    original = copy.deepcopy(plugin._web_tasks)
    plugin._persist_web_tasks_locked()
    stored, raw = read_snapshot(plugin)
    assert len(raw.decode("utf-8")) < plugin.WEB_TASK_SNAPSHOT_MAX_BYTES < len(raw)
    assert stored["tasks"] == original
    assert plugin._web_tasks == original
    health = plugin.get_task_snapshot_health()
    assert health["over_budget"] is True
    assert health["warning"]
    assert health["evicted_task_count"] == health["compacted_task_count"] == 0
    aggregate = build_health_payload(plugin)
    assert aggregate["status"] == "degraded"
    assert aggregate["task_snapshot_health"]["size_bytes"] == len(raw)
    assert len(warnings) == 1

    # Status changes must flush even while active payloads exceed the budget.
    plugin._set_web_image_task("running", status="running", completed_count=1)
    again, _ = read_snapshot(plugin)
    assert again["snapshot"]["write_count"] == 2
    assert again["tasks"]["running"]["completed_count"] == 1
    assert set(again["tasks"]) == {"queued", "running"}
    assert len(warnings) == 1


def test_budget_evicts_oldest_terminal_before_newer_terminal_or_active_work(tmp_path):
    plugin = snapshot_plugin(tmp_path, max_bytes=100_000)
    plugin._web_tasks = {
        "active": {"task_id": "active", "status": "running", "request_data": {"prompt": "a" * 1400}},
        "new": {"task_id": "new", "status": "succeeded", "updated_ts": 20, "result": {"prompt": "b" * 1400}},
        "old": {"task_id": "old", "status": "failed", "updated_ts": 10, "error": "c" * 1400},
    }
    plugin._persist_web_tasks_locked()
    _, initial = read_snapshot(plugin)
    old_bytes = len(json.dumps({"old": plugin._web_tasks["old"]}, separators=(",", ":")).encode())
    plugin.WEB_TASK_SNAPSHOT_MAX_BYTES = len(initial) - old_bytes + 64
    plugin._persist_web_tasks_locked()
    stored, raw = read_snapshot(plugin)
    assert set(stored["tasks"]) == {"active", "new"}
    assert set(plugin._web_tasks) == {"active", "new", "old"}
    assert len(raw) <= plugin.WEB_TASK_SNAPSHOT_MAX_BYTES
    assert stored["snapshot"]["evicted_task_count"] == 1
    assert stored["snapshot"]["over_budget"] is False
    assert build_health_payload(plugin)["status"] == "ok"


def test_budget_compacts_only_optional_diagnostics_after_terminal_eviction(tmp_path):
    plugin = snapshot_plugin(tmp_path)
    plugin._web_tasks = {
        "old": {"task_id": "old", "status": "succeeded", "updated_ts": 1},
        "active": {
            "task_id": "active", "status": "running", "owner_session": "group:1",
            "quota_reserved": 2, "request_data": {"prompt": "portrait"},
            "record_ids": ["record-1"], "result": {
                "success": True, "generation_success": True, "delivery_success": False,
                "files": ["generated.png"], "request_image_paths": ["reference.png"],
                "request_data": {"original_prompt": "portrait"},
                "response_data": {
                    "raw_response": "x" * 16_000, "error": "delivery unavailable",
                    "success": True, "trace_id": "gen-trace", "delivery_success": False,
                },
                "attempts": [{"debug": "y" * 16_000}],
            },
        },
    }
    original = copy.deepcopy(plugin._web_tasks)
    plugin._persist_web_tasks_locked()
    stored, raw = read_snapshot(plugin)
    assert set(stored["tasks"]) == {"active"}
    task = stored["tasks"]["active"]
    result = task["result"]
    assert result["files"] == ["generated.png"]
    assert result["request_image_paths"] == ["reference.png"]
    assert result["request_data"] == {"original_prompt": "portrait"}
    assert result["response_data"] == {
        "error": "delivery unavailable", "success": True,
        "trace_id": "gen-trace", "delivery_success": False,
    }
    assert result["generation_success"] is True
    assert result["delivery_success"] is False
    assert task["owner_session"] == "group:1"
    assert task["quota_reserved"] == 2
    assert task["record_ids"] == ["record-1"]
    assert set(task["snapshot_compacted_fields"]) == {"result.response_data", "result.attempts"}
    assert "attempts" not in result
    assert plugin._web_tasks == original
    assert len(raw) <= plugin.WEB_TASK_SNAPSHOT_MAX_BYTES
    assert stored["snapshot"]["compacted_task_count"] == 1
    assert stored["snapshot"]["evicted_task_count"] == 1
    assert stored["snapshot"]["over_budget"] is False


@pytest.mark.parametrize("container", ["response_data", "attempts"])
@pytest.mark.parametrize("media_field", [
    "generated_image_paths", "request_image_paths", "image_paths", "files", "images",
])
def test_diagnostic_compaction_preserves_nested_media_evidence(tmp_path, container, media_field):
    plugin = snapshot_plugin(tmp_path, max_bytes=2048)
    evidence = {"provider": {"payload": {media_field: ["only-in-diagnostics.png"]}}}
    response = {"raw_response": "x" * 8000}
    attempts = [{"debug": "y" * 8000}]
    if container == "response_data":
        response.update(evidence)
    else:
        attempts[0].update(evidence)
    plugin._web_tasks = {"active": {
        "task_id": "active", "status": "running",
        "result": {"response_data": response, "attempts": attempts},
    }}
    original = copy.deepcopy(plugin._web_tasks)
    original_paths = set(collect_record_cache_paths(original))
    plugin._persist_web_tasks_locked()
    stored, _ = read_snapshot(plugin)
    task = stored["tasks"]["active"]
    assert task["result"][container] == original["active"]["result"][container]
    assert set(collect_record_cache_paths(stored["tasks"])) == original_paths
    assert plugin._web_tasks == original
    assert stored["snapshot"]["over_budget"] is True
    assert stored["snapshot"]["compacted_task_count"] == 1
    other_container = "attempts" if container == "response_data" else "response_data"
    assert task["snapshot_compacted_fields"] == ["result." + other_container]


def test_diagnostics_with_unique_media_paths_in_both_containers_are_not_compacted(tmp_path):
    plugin = snapshot_plugin(tmp_path, max_bytes=2048)
    plugin._web_tasks = {"active": {
        "task_id": "active", "status": "running", "result": {
            "response_data": {"generated_image_paths": ["response-only.png"], "debug": "x" * 8000},
            "attempts": [{"response_data": {"request_image_paths": ["attempt-only.png"]}, "debug": "y" * 8000}],
        },
    }}
    original = copy.deepcopy(plugin._web_tasks)
    plugin._persist_web_tasks_locked()
    stored, _ = read_snapshot(plugin)
    assert stored["tasks"] == original
    assert set(collect_record_cache_paths(stored["tasks"])) == {"response-only.png", "attempt-only.png"}
    assert stored["snapshot"]["compacted_task_count"] == 0
    assert stored["snapshot"]["over_budget"] is True


@pytest.mark.parametrize("container", ["response_data", "attempts"])
def test_unrecognized_diagnostic_shapes_are_preserved(tmp_path, container):
    plugin = snapshot_plugin(tmp_path, max_bytes=2048)
    plugin._web_tasks = {"active": {
        "task_id": "active", "status": "running", "result": {container: "legacy-data" * 1000},
    }}
    plugin._persist_web_tasks_locked()
    stored, _ = read_snapshot(plugin)
    assert stored["tasks"] == plugin._web_tasks
    assert stored["snapshot"]["compacted_task_count"] == 0
    assert stored["snapshot"]["over_budget"] is True


@pytest.mark.parametrize("status", ["expired", "EXPIRED", " Expired ", "\texpired\n"])
def test_unreconciled_expired_protection_normalizes_legacy_status(tmp_path, status):
    plugin = snapshot_plugin(tmp_path, max_bytes=2048)
    plugin._web_tasks = {"abandoned": {
        "task_id": "abandoned", "status": status, "restart_reconciled": False,
        "request_data": {"prompt": "x" * 8000},
    }}
    plugin._persist_web_tasks_locked()
    stored, _ = read_snapshot(plugin)
    assert stored["tasks"] == plugin._web_tasks
    assert stored["snapshot"]["over_budget"] is True
    plugin._web_tasks["abandoned"]["restart_reconciled"] = True
    plugin._persist_web_tasks_locked()
    final, _ = read_snapshot(plugin)
    assert final["tasks"] == {}
    assert final["snapshot"]["over_budget"] is False


def test_budget_protects_unknown_status_instead_of_dropping_it(tmp_path):
    plugin = snapshot_plugin(tmp_path, max_bytes=1024)
    plugin._web_tasks = {"future": {"task_id": "future", "status": "awaiting_receipt", "error": "x" * 4000}}
    plugin._persist_web_tasks_locked()
    stored, _ = read_snapshot(plugin)
    assert stored["tasks"] == plugin._web_tasks
    assert stored["snapshot"]["over_budget"] is True


def test_failed_budget_write_keeps_actual_file_metrics_and_persists_failure_on_retry(tmp_path, monkeypatch):
    plugin = snapshot_plugin(tmp_path, max_bytes=2048)
    plugin._web_tasks = {"active": {"task_id": "active", "status": "running"}}
    plugin._persist_web_tasks_locked()
    _, original = read_snapshot(plugin)
    save = task_manager.save_json_file_compact
    plugin._web_tasks["active"]["request_data"] = {"prompt": "x" * 8000}

    def fail(_path, _data):
        raise OSError("disk full token=secret-token-123")

    monkeypatch.setattr(task_manager, "save_json_file_compact", fail)
    with pytest.raises(OSError, match="disk full"):
        plugin._persist_web_tasks_locked()
    assert Path(plugin.tasks_path).read_bytes() == original
    health = plugin.get_task_snapshot_health()
    assert health["size_bytes"] == len(original)
    assert health["over_budget"] is False
    assert health["write_count"] == health["failure_count"] == 1
    assert health["last_failure_reason"] == "disk full token=[REDACTED]"

    monkeypatch.setattr(task_manager, "save_json_file_compact", save)
    plugin._persist_web_tasks_locked()
    stored, _ = read_snapshot(plugin)
    assert stored["tasks"]["active"] == plugin._web_tasks["active"]
    assert stored["snapshot"]["over_budget"] is True
    assert stored["snapshot"]["write_count"] == 2
    assert stored["snapshot"]["failure_count"] == 1
    assert stored["snapshot"]["last_failure_reason"] == "disk full token=[REDACTED]"


def test_legacy_restart_keeps_abandoned_task_evidence_until_record_reconciliation(tmp_path):
    plugin = snapshot_plugin(tmp_path, max_bytes=2048)
    save_json_file(plugin.tasks_path, {"tasks": {
        "abandoned": {
            "task_id": "abandoned", "status": "running", "quota_reserved": 2,
            "owner_session": "group:1", "request_data": {"prompt": "\u4eba" * 4000},
        },
        "old": {"task_id": "old", "status": "succeeded", "updated_ts": 1},
    }})
    plugin._web_tasks = plugin._load_web_tasks()
    stored, _ = read_snapshot(plugin)
    assert set(stored["tasks"]) == {"abandoned"}
    task = stored["tasks"]["abandoned"]
    assert task["status"] == "expired"
    assert task["quota_reserved"] == 0
    assert task["quota_released"] is True
    assert task["request_data"]["prompt"] == "\u4eba" * 4000
    assert stored["snapshot"]["over_budget"] is True

    reloaded = snapshot_plugin(tmp_path, max_bytes=2048)
    reloaded._web_tasks = reloaded._load_web_tasks()
    assert reloaded._web_tasks["abandoned"]["status"] == "expired"
    assert reloaded.get_task_snapshot_health()["over_budget"] is True
    assert reloaded.get_task_snapshot_health()["evicted_task_count"] == 1

    # After the restart record is linked, ordinary terminal eviction is safe.
    reloaded._web_tasks["abandoned"]["restart_reconciled"] = True
    reloaded._persist_web_tasks_locked()
    final, raw = read_snapshot(reloaded)
    assert final["tasks"] == {}
    assert len(raw) <= reloaded.WEB_TASK_SNAPSHOT_MAX_BYTES


def test_legacy_terminal_snapshot_health_measures_file_including_whitespace(tmp_path):
    plugin = snapshot_plugin(tmp_path, max_bytes=20_000)
    save_json_file(plugin.tasks_path, {"tasks": {"done": {"task_id": "done", "status": "succeeded"}}})
    raw = Path(plugin.tasks_path).read_bytes()
    plugin._web_tasks = plugin._load_web_tasks()
    health = plugin.get_task_snapshot_health()
    assert health["size_bytes"] == len(raw)
    assert health["over_budget"] is False
    assert health["evicted_task_count"] == health["compacted_task_count"] == 0
    assert health["write_count"] == 0
    assert "snapshot" not in load_json_file(plugin.tasks_path)


@pytest.mark.parametrize("payload", [
    "{\"tasks\":",
    "[]",
    '{}',
    '{"unexpected": true}',
    '{"tasks": []}',
    '{"tasks": {"broken": []}}',
    '{"tasks": {}, "snapshot": []}',
])
def test_corrupt_task_snapshot_is_quarantined_and_health_stays_degraded(tmp_path, payload):
    plugin = snapshot_plugin(tmp_path)
    original = payload.encode("utf-8")
    Path(plugin.tasks_path).write_bytes(original)

    assert plugin._load_web_tasks() == {}
    health = plugin.get_task_snapshot_health()
    assert health["status"] == "error"
    assert health["load_blocked"] is True
    assert health["size_bytes"] == len(original)
    assert any(marker in health["load_error"] for marker in ("损坏", "结构无效", "缺少"))
    aggregate = build_health_payload(plugin)
    assert aggregate["status"] == "degraded"
    assert aggregate["task_snapshot_health"]["warning"] == health["load_error"]
    quarantined = list(tmp_path.glob("tasks.json.corrupt-*"))
    assert len(quarantined) == 1
    assert Path(plugin.tasks_path).read_bytes() == original
    assert quarantined[0].read_bytes() == original

    # The original remains untouched until an operator restores or removes it;
    # automatic lifecycle writes must not replace the only recovery artifact.
    with pytest.raises(OSError, match="拒绝覆盖"):
        plugin._persist_web_tasks_locked()
    assert Path(plugin.tasks_path).read_bytes() == original

    plugin._schedule_web_task_snapshot_locked()
    deadline = time.monotonic() + 1
    while plugin.get_task_snapshot_health()["pending"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert plugin.get_task_snapshot_health()["pending"] is False


def test_unsafe_task_snapshot_is_never_read_or_overwritten(tmp_path):
    plugin = snapshot_plugin(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text('{"tasks":{"keep":{"status":"succeeded"}}}', encoding="utf-8")
    Path(plugin.tasks_path).symlink_to(outside)

    assert plugin._load_web_tasks() == {}
    health = plugin.get_task_snapshot_health()
    assert health["status"] == "error"
    assert health["load_blocked"] is True
    assert "符号链接" in health["load_error"]
    with pytest.raises(OSError, match="拒绝覆盖"):
        plugin._persist_web_tasks_locked()
    assert Path(plugin.tasks_path).is_symlink()
    assert outside.read_text(encoding="utf-8") == '{"tasks":{"keep":{"status":"succeeded"}}}'


def test_blocked_snapshot_does_not_abort_final_shutdown_flush(tmp_path, monkeypatch):
    from astrbot_plugin_selfie_image import main as plugin_main

    plugin = snapshot_plugin(tmp_path)
    original = b'{"tasks":'
    Path(plugin.tasks_path).write_bytes(original)
    plugin._load_web_tasks()
    errors = []
    monkeypatch.setattr(plugin_main.logger, "error", lambda *args: errors.append(args))

    plugin_main.SelfieImagePlugin._flush_web_task_snapshots(plugin)

    assert Path(plugin.tasks_path).read_bytes() == original
    assert errors


def _wait_for_snapshot_timer(plugin: WebTaskMixin, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while plugin.get_task_snapshot_health()["pending"] and time.monotonic() < deadline:
        time.sleep(0.005)
    assert plugin.get_task_snapshot_health()["pending"] is False


def test_snapshot_health_tracks_merge_bytes_and_flush_duration(tmp_path):
    plugin = snapshot_plugin(tmp_path)
    plugin.WEB_TASK_SNAPSHOT_DELAY = 0.02
    plugin._web_tasks = {"task": {"task_id": "task", "status": "running"}}

    plugin._set_web_image_task("task", generation_stage="provider")
    plugin._set_web_image_task("task", generation_stage_label="调用模型")
    _wait_for_snapshot_timer(plugin)

    health = plugin.get_task_snapshot_health()
    assert health["write_count"] == 1
    assert health["scheduled_flush_count"] == 1
    assert health["coalesced_update_count"] == 1
    assert health["timer_flush_count"] == 1
    assert health["forced_flush_count"] == 0
    assert health["skipped_write_count"] == 0
    assert health["write_bytes_total"] == health["size_bytes"]
    assert health["write_duration_ms"]["samples"] == 1
    assert health["write_duration_ms"]["p95"] >= health["write_duration_ms"]["p50"]

    plugin._set_web_image_task("task", status="succeeded", success=True)
    health = plugin.get_task_snapshot_health()
    assert health["write_count"] == 2
    assert health["forced_flush_count"] == 1
    assert health["write_bytes_total"] > health["size_bytes"]
    assert health["write_duration_ms"]["samples"] == 2
    persisted = load_json_file(plugin.tasks_path)
    assert persisted["snapshot"]["write_bytes_total"] == health["write_bytes_total"]
    assert persisted["snapshot"]["timer_flush_count"] == 1
    assert persisted["snapshot"]["forced_flush_count"] == 1


def test_duplicate_generation_record_link_is_skipped_without_rewrite(tmp_path):
    from astrbot_plugin_selfie_image.generation.generation_store import GenerationStoreMixin

    plugin = snapshot_plugin(tmp_path)
    plugin._web_tasks = {"task": {"task_id": "task", "status": "running", "result": {}}}
    GenerationStoreMixin._link_generation_record_to_task(plugin, "task", "record-1")
    first = load_json_file(plugin.tasks_path)
    GenerationStoreMixin._link_generation_record_to_task(plugin, "task", "record-1")
    second = load_json_file(plugin.tasks_path)

    assert second["tasks"] == first["tasks"]
    assert plugin.get_task_snapshot_health()["write_count"] == 1
    assert plugin.get_task_snapshot_health()["skipped_write_count"] == 1


def test_concurrent_terminal_flushes_are_immediately_reloadable(tmp_path, monkeypatch):
    from astrbot_plugin_selfie_image.tasks import task_manager

    count = 12
    plugin = snapshot_plugin(tmp_path)
    plugin._web_tasks = {
        f"task-{index}": {
            "task_id": f"task-{index}",
            "status": "running",
            "request_data": {"prompt": f"prompt-{index}"},
        }
        for index in range(count)
    }
    writes = []
    original_save = task_manager.save_json_file_compact

    def save(path, data):
        original_save(path, data)
        writes.append(Path(path).stat().st_size)

    monkeypatch.setattr(task_manager, "save_json_file_compact", save)
    barrier = threading.Barrier(count)
    errors = []

    def finish(task_id):
        try:
            barrier.wait(timeout=2)
            plugin._set_web_image_task(
                task_id,
                status="succeeded",
                success=True,
                result={"success": True, "task_id": task_id},
            )
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=finish, args=(f"task-{index}",)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    persisted = load_json_file(plugin.tasks_path)
    assert set(persisted["tasks"]) == {f"task-{index}" for index in range(count)}
    assert all(row["status"] == "succeeded" for row in persisted["tasks"].values())
    health = plugin.get_task_snapshot_health()
    assert len(writes) == count
    assert health["write_count"] == count
    assert health["forced_flush_count"] == count
    assert health["write_duration_ms"]["samples"] == count
    assert health["write_bytes_total"] == sum(writes)
    assert health["size_bytes"] == len(Path(plugin.tasks_path).read_bytes())
