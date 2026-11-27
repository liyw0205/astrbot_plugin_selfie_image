from __future__ import annotations

import threading
from types import SimpleNamespace

from astrbot_plugin_selfie_image.main import SelfieImagePlugin
from astrbot_plugin_selfie_image.webui.services import build_health_payload


def _startup_probe() -> SelfieImagePlugin:
    probe = object.__new__(SelfieImagePlugin)
    probe._persistent_state_lock = threading.RLock()
    probe._records_lock = threading.RLock()
    probe._web_task_lock = threading.RLock()
    probe._records = [{"id": "early-record", "success": True}]
    probe._record_seq = 0
    probe._web_tasks = {"early-task": {"task_id": "early-task", "seq": 9}}
    probe._web_task_seq = 0
    probe._load_records = lambda: [{"id": "saved-record", "success": True}]
    probe._restore_record_sequence = lambda: 11
    probe._load_web_tasks = lambda: {"saved-task": {"task_id": "saved-task", "seq": 3}}
    probe.reconcile_expired_tasks_after_restart = lambda: None
    probe._startup_state_lock = threading.RLock()
    probe._startup_state = {
        "status": "loading",
        "phase": "constructing",
        "records_loaded": False,
        "tasks_loaded": False,
    }
    return probe


def test_startup_state_installation_preserves_early_live_writes():
    probe = _startup_probe()
    probe._web_tasks["early-task"]["status"] = "running"
    canvas_recovery_calls = []
    probe.studio = SimpleNamespace(reconcile_interrupted_runs=canvas_recovery_calls.append)

    probe._load_persistent_state()

    assert [row["id"] for row in probe._records] == ["early-record", "saved-record"]
    assert set(probe._web_tasks) == {"early-task", "saved-task"}
    assert probe._web_task_seq == 9
    assert canvas_recovery_calls == [{"early-task"}]
    assert probe.get_startup_readiness()["records_loaded"] is True


def test_health_payload_exposes_starting_without_marking_provider_failure():
    plugin = SimpleNamespace(
        config=SimpleNamespace(image_cache_limit_count=10, image_record_keep_limit=10, image_cache_limit_mb=10),
        _cache_stats=lambda: (0, 0),
        get_startup_readiness=lambda: {"status": "loading", "phase": "records"},
    )

    payload = build_health_payload(plugin)

    assert payload["status"] == "starting"
    assert payload["startup"]["phase"] == "records"
