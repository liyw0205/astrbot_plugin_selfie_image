"""Batch generation must retain delivery evidence and commit ordering."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_selfie_image.core.models import AICatConfig
from astrbot_plugin_selfie_image.features.reference_media import ImageDeliveryOutcome
from astrbot_plugin_selfie_image.main import SelfieImagePlugin


@pytest.fixture
def plugin():
    instance = object.__new__(SelfieImagePlugin)
    instance.config = AICatConfig.from_dict({})
    instance._image_scheduler = object()
    instance._web_task_lock = threading.RLock()
    instance._web_tasks = {"task": {"requested_count": 1}}
    instance._task_cancel_requested = Mock(return_value=False)
    instance._set_web_image_task = Mock()
    instance._record_generated_images_async = AsyncMock()
    instance._send_generated_images = AsyncMock(return_value=None)
    instance._set_task_notification_status = Mock()
    instance._wait_for_record_commits = AsyncMock()
    instance._update_generation_records_delivery = Mock()
    instance._update_generation_records_delivery_timing = Mock()
    instance._build_success_text = Mock(return_value="")
    instance._batch_success_text = Mock(return_value="")
    return instance


def run_batch(plugin, run_one, total=1):
    return asyncio.run(plugin._run_counted_generation_shots(
        task_id="task", event=object(), total=total, fail_label="failed",
        run_one=run_one, log_prefix="regression",
    ))


@pytest.mark.parametrize("receipt,expected", [
    (None, "sent"),
    (2, "sent"),
    (1, "failed"),
    (ImageDeliveryOutcome(0, failed=2), "failed"),
    (ImageDeliveryOutcome(0, unknown=2), "unknown"),
])
def test_delivery_outcome_keeps_generation_and_receipt_separate(plugin, receipt, expected):
    plugin._send_generated_images.return_value = receipt
    run_one = AsyncMock(return_value={"success": True, "files": ["a.png", "b.png"]})
    result = run_batch(plugin, run_one)

    assert result["files"] == ["a.png", "b.png"]
    assert result["generation_success"] is True
    assert result["succeeded_count"] == 1
    assert plugin._set_task_notification_status.call_args.args[1] == expected
    plugin._record_generated_images_async.assert_awaited_once()
    if expected == "unknown":
        assert result["delivery_success"] is None
        assert result["delivery_unknown"] is True
        assert result["delivery_unknown_count"] == 2
        plugin._update_generation_records_delivery.assert_not_called()
        kwargs = plugin._update_generation_records_delivery_timing.call_args.kwargs
        assert kwargs["paths"] == ["a.png", "b.png"]
        assert kwargs["elapsed_seconds"] >= 0
    else:
        plugin._update_generation_records_delivery_timing.assert_not_called()
        assert result["delivery_success"] is (expected == "sent")
        assert plugin._update_generation_records_delivery.call_args.kwargs["delivered"] is (expected == "sent")
        assert plugin._update_generation_records_delivery.call_args.kwargs["paths"] == ["a.png", "b.png"]
        assert plugin._update_generation_records_delivery.call_args.kwargs["delivery_elapsed_seconds"] >= 0
        if expected == "failed":
            assert result["status"] == "delivery_failed"
            assert result["success"] is False


@pytest.mark.parametrize("unknown", [False, True])
def test_delivery_latency_excludes_commit_wait(plugin, monkeypatch, unknown):
    from astrbot_plugin_selfie_image.generation import counted_workflow

    clock = [0.0]
    monkeypatch.setattr(counted_workflow, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def commit(_task_id):
        clock[0] += 20

    async def send(_event, _files):
        clock[0] += 2.5
        return ImageDeliveryOutcome(0, unknown=1) if unknown else 1

    plugin._wait_for_record_commits.side_effect = commit
    plugin._send_generated_images.side_effect = send
    run_batch(plugin, AsyncMock(return_value={"success": True, "files": ["a.png"]}))

    if unknown:
        assert plugin._update_generation_records_delivery_timing.call_args.kwargs["elapsed_seconds"] == 2.5
        plugin._update_generation_records_delivery.assert_not_called()
    else:
        assert plugin._update_generation_records_delivery.call_args.kwargs["delivery_elapsed_seconds"] == 2.5


def test_record_commit_precedes_progress_and_delivery_update(plugin):
    events = []

    async def commit(_task_id):
        events.append("commit")

    async def send(_event, _files):
        events.append("send")

    plugin._wait_for_record_commits.side_effect = commit
    plugin._send_generated_images.side_effect = send
    plugin._set_web_image_task.side_effect = lambda _tid, **kw: events.append(
        "completed" if kw.get("completed_count") == 1 else "queued"
    )
    plugin._update_generation_records_delivery.side_effect = lambda *_a, **_kw: events.append("delivery")
    result = run_batch(plugin, AsyncMock(return_value={"success": True, "files": ["a.png"]}))

    assert result["success"] is True
    assert events == ["queued", "commit", "send", "commit", "delivery", "completed"]


def test_legacy_generation_slot_is_released_on_exception(plugin):
    plugin._image_scheduler = None
    gate = asyncio.Semaphore(1)
    plugin._ensure_image_batch_gate = lambda: gate
    with pytest.raises(RuntimeError, match="upstream exception"):
        run_batch(plugin, AsyncMock(side_effect=RuntimeError("upstream exception")))
    assert gate._value == 1


@pytest.mark.parametrize("error", [asyncio.CancelledError(), RuntimeError("cooldown exception")])
def test_legacy_slot_is_released_when_cooldown_is_interrupted(plugin, error):
    plugin._image_scheduler = None
    gate = asyncio.Semaphore(1)
    plugin._ensure_image_batch_gate = lambda: gate
    plugin._wait_for_image_batch_cooldown = AsyncMock(side_effect=error)
    run_one = AsyncMock()
    with pytest.raises(type(error)):
        run_batch(plugin, run_one, total=2)
    assert gate._value == 1
    run_one.assert_not_awaited()
