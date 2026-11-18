"""Batch generation must retain delivery evidence and commit ordering."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_selfie_image.core.models import AICatConfig
from astrbot_plugin_selfie_image.features.reference_media import ImageDeliveryOutcome
from astrbot_plugin_selfie_image.generation.counted_workflow import CountedGenerationWorkflow
from astrbot_plugin_selfie_image.generation.image_scheduler import ImageJobScheduler
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
    instance._batch_failure_policy = lambda: ("stop", 0)
    instance._friendly_user_error_message = lambda error, _label: error
    instance._batch_shot_fail_text = Mock(return_value="failed")
    instance._send_task_notification = AsyncMock()
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


@pytest.mark.parametrize("same_shot", [False, True])
def test_mixed_receipts_keep_confirmed_failure_and_unknown_counts_on_stop(plugin, same_shot):
    plugin._image_inflight_limit = lambda: 1
    plugin._send_generated_images.side_effect = (
        [ImageDeliveryOutcome(0, failed=1, unknown=1)] if same_shot else
        [ImageDeliveryOutcome(0, failed=1), ImageDeliveryOutcome(0, unknown=1)]
    )
    results = (
        [{"success": True, "files": ["a.png", "b.png"]}] if same_shot else
        [{"success": True, "files": ["a.png"]}, {"success": True, "files": ["b.png"]}]
    )
    results.append({"success": False, "error": "provider stopped"})
    result = run_batch(plugin, AsyncMock(side_effect=results), total=len(results))

    assert result["status"] == "delivery_failed"
    assert result["files"] == ["a.png", "b.png"]
    assert result["delivery_success"] is False
    assert result["delivery_failed"] is True
    assert result["delivery_unknown"] is False
    assert result["delivery_failed_count"] == result["delivery_unknown_count"] == 1
    assert result["batch_failed_at"] == len(results)
    assert result["error"] == "provider stopped"


@pytest.mark.parametrize("cancelled", [False, True])
def test_batch_without_generated_images_does_not_report_delivery(plugin, cancelled):
    plugin._task_cancel_requested.return_value = cancelled
    result = run_batch(plugin, AsyncMock(return_value={"success": False, "error": "provider failed"}))
    assert result["files"] == []
    assert result["generation_success"] is False
    assert result["delivery_success"] is None
    assert result["delivery_failed"] is result["delivery_unknown"] is False
    assert result["status"] == ("cancelled" if cancelled else "failed")
    plugin._send_generated_images.assert_not_awaited()


def test_three_hundred_shots_drain_through_shared_scheduler(plugin):
    scheduler = ImageJobScheduler(limit=5)
    plugin._image_scheduler = scheduler
    plugin._image_inflight_limit = lambda: 5
    plugin._web_tasks = {f"task-{batch}": {"requested_count": 100} for batch in range(3)}
    plugin._set_web_image_task = Mock()
    plugin._batch_success_text = Mock(return_value="")
    plugin._normalize_generation_result = lambda result, _total: result

    active = 0
    peak = 0
    completed = 0

    async def run_batch(batch):
        task_id = f"task-{batch}"

        async def run_one(index):
            async def upstream_request():
                nonlocal active, peak, completed
                active += 1
                peak = max(peak, active)
                try:
                    await asyncio.sleep(0)
                    completed += 1
                    return True
                finally:
                    active -= 1

            await scheduler.submit(task_id, upstream_request)
            return {"success": True, "files": [f"{task_id}-{index}.png"]}

        workflow = CountedGenerationWorkflow(
            plugin,
            task_id=task_id,
            event=object(),
            total=100,
            fail_label="failed",
            run_one=run_one,
            log_prefix="scheduler pressure test",
            log_info=lambda _message: None,
        )
        return await workflow.run()

    async def run():
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(run_batch(batch) for batch in range(3))),
                timeout=15,
            )
            snapshot = await scheduler.snapshot()
        finally:
            await scheduler.aclose()

        assert all(result["completed_count"] == 100 for result in results)
        assert completed == 300
        assert peak == 5
        assert snapshot == {"limit": 5, "active": 0, "pending": 0, "queued_tasks": 0}

    asyncio.run(run())


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
