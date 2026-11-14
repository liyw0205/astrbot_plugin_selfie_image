from __future__ import annotations

import json
import asyncio
import threading
from types import SimpleNamespace

from astrbot_plugin_selfie_image import main as main_module
from astrbot_plugin_selfie_image.tasks import task_manager
from astrbot_plugin_selfie_image.tasks.task_manager import WebTaskMixin


def test_task_creation_and_status_changes_emit_safe_json_events(monkeypatch):
    plugin = WebTaskMixin()
    plugin.loop = SimpleNamespace(is_running=lambda: True)
    plugin.config = SimpleNamespace(
        image_max_batch_count=10,
        image_default_aspect_ratio="9:16",
        image_default_resolution="1K",
    )
    plugin.tasks_path = ""
    plugin._web_task_lock = threading.RLock()
    plugin._web_tasks = {}
    plugin._web_task_seq = 0
    plugin._validate_web_test_selection = lambda _payload: None
    plugin._request_fingerprint = lambda *_args: "fingerprint"
    plugin._start_runtime_generation = lambda _task_id, coroutine: coroutine.close()
    plugin.get_web_image_task = lambda task_id: plugin._web_tasks[task_id]
    messages = []
    monkeypatch.setattr(task_manager.logger, "info", lambda message: messages.append(message))

    created = plugin.start_web_image_task({"prompt": "private prompt text"})
    task_id = created["task_id"]
    trace_id = created["trace_id"]
    plugin._set_web_image_task(task_id, generation_stage="provider", progress_percent=10)
    plugin._set_web_image_task(task_id, status="running")
    plugin._set_web_image_task(
        task_id,
        status="failed",
        error="api_key=private-provider-key prompt=private-error-text",
    )

    events = [json.loads(message.removeprefix("[SelfieImage] ")) for message in messages]
    assert [(event["from_status"], event["to_status"]) for event in events] == [
        ("unknown", "queued"),
        ("queued", "running"),
        ("running", "failed"),
    ]
    assert all(event["event"] == "task.lifecycle" for event in events)
    assert all(event["task_id"] == task_id and event["trace_id"] == trace_id for event in events)
    serialized = json.dumps(events)
    assert "private prompt text" not in serialized
    assert "private-provider-key" not in serialized
    assert "private-error-text" not in serialized


def test_lifecycle_logging_failure_does_not_raise(monkeypatch):
    plugin = WebTaskMixin()

    def fail_logging(*_args):
        raise RuntimeError("log unavailable")

    monkeypatch.setattr(task_manager.logger, "info", fail_logging)

    plugin._log_task_lifecycle_event("web-12345678-1", "gen-12345678", "image", "queued", "running")


def test_notification_logging_records_only_real_enum_transitions(monkeypatch):
    plugin = WebTaskMixin()
    plugin._web_task_lock = threading.RLock()
    plugin._web_tasks = {
        "cmd-12345678-1": {
            "task_id": "cmd-12345678-1",
            "trace_id": "gen-12345678",
            "status": "running",
            "user_notification_status": "pending",
        }
    }
    plugin._web_task_timestamp = lambda: "now"
    plugin._persist_web_tasks_locked = lambda: None
    messages = []
    monkeypatch.setattr(task_manager.logger, "info", lambda message: messages.append(message))

    plugin._set_task_notification_status(
        "cmd-12345678-1", "failed", "api_key=private-notification-key prompt=private-message"
    )
    plugin._set_task_notification_status("cmd-12345678-1", "unknown", "late receipt")

    events = [json.loads(message.removeprefix("[SelfieImage] ")) for message in messages]
    assert len(events) == 1
    assert events[0] == {
        "channel": "message",
        "event": "task.notification",
        "from_status": "pending",
        "media_type": "image",
        "task_id": "cmd-12345678-1",
        "timestamp": "now",
        "to_status": "failed",
        "trace_id": "gen-12345678",
    }
    assert "private-notification-key" not in json.dumps(events)
    assert "private-message" not in json.dumps(events)
    assert plugin._web_tasks["cmd-12345678-1"]["user_notification_status"] == "failed"


def test_image_delivery_reconciliation_logs_task_and_delivery_transitions(monkeypatch):
    plugin = WebTaskMixin()
    plugin._web_task_lock = threading.RLock()
    plugin._records_lock = threading.RLock()
    plugin._web_tasks = {
        "web-12345678-1": {
            "task_id": "web-12345678-1",
            "trace_id": "gen-12345678",
            "status": "delivery_failed",
            "delivery_failed": True,
            "error": "private provider failure",
            "record_ids": ["record-1"],
            "succeeded_count": 1,
            "result": {"files": ["image.png"], "requested_count": 1},
            "user_notification_status": "failed",
        }
    }
    plugin._records = [
        {
            "id": "record-1",
            "task_id": "web-12345678-1",
            "generated_image_paths": ["image.png"],
            "image_delivery": {"image.png": "sent"},
            "delivery_success": True,
        }
    ]
    plugin._web_task_timestamp = lambda: "now"
    plugin._persist_web_tasks_locked = lambda: None
    messages = []
    monkeypatch.setattr(task_manager.logger, "info", lambda message: messages.append(message))

    plugin._reconcile_task_image_delivery("web-12345678-1")

    events = [json.loads(message.removeprefix("[SelfieImage] ")) for message in messages]
    assert [(event["event"], event["from_status"], event["to_status"]) for event in events] == [
        ("task.lifecycle", "delivery_failed", "succeeded"),
        ("task.notification", "failed", "sent"),
    ]
    assert all(event["task_id"] == "web-12345678-1" for event in events)
    assert all(event["trace_id"] == "gen-12345678" for event in events)
    assert events[1]["channel"] == "image_delivery"
    assert "private provider failure" not in json.dumps(events)


def test_immediate_cancel_path_emits_terminal_lifecycle_event(monkeypatch):
    plugin = object.__new__(main_module.SelfieImagePlugin)
    plugin._web_task_lock = threading.RLock()
    plugin._web_tasks = {
        "cmd-12345678-1": {
            "task_id": "cmd-12345678-1",
            "trace_id": "gen-12345678",
            "status": "running",
            "cancel_requested": False,
        }
    }
    plugin._runtime_generation_tasks = {}
    plugin._image_scheduler = None
    plugin._web_task_timestamp = lambda: "now"
    plugin._persist_web_tasks_locked = lambda: None
    messages = []
    monkeypatch.setattr(task_manager.logger, "info", lambda message: messages.append(message))

    plugin.cancel_image_task("cmd-12345678-1", is_admin=True)

    events = [json.loads(message.removeprefix("[SelfieImage] ")) for message in messages]
    assert len(events) == 1
    assert events[0]["event"] == "task.lifecycle"
    assert events[0]["from_status"] == "running"
    assert events[0]["to_status"] == "cancelled"


def test_notification_failure_warning_uses_redacted_error(monkeypatch):
    plugin = WebTaskMixin()
    plugin._set_task_notification_status = lambda *_args, **_kwargs: None
    warnings = []
    monkeypatch.setattr(main_module.logger, "warning", lambda message: warnings.append(message))

    class Event:
        def plain_result(self, text):
            return text

        async def send(self, _message):
            raise RuntimeError("api_key=private-transport-key")

    assert asyncio.run(
        main_module.SelfieImagePlugin._send_task_notification(plugin, "cmd-12345678-1", Event(), "private result")
    ) is False
    assert len(warnings) == 1
    assert "private-transport-key" not in warnings[0]
