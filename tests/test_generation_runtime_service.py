from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from astrbot_plugin_selfie_image.generation.runtime_service import (
    _IMAGE_JOB_ALREADY_SCHEDULED,
)
from astrbot_plugin_selfie_image.main import SelfieImagePlugin


def test_runtime_service_keeps_plugin_generation_hooks_and_scheduler_context():
    plugin = object.__new__(SelfieImagePlugin)
    plugin._terminating = False
    stages = []
    dispatched = []

    class Scheduler:
        async def submit(self, task_id, run):
            self.task_id = task_id
            return await run()

    async def run_unscheduled(*args, **kwargs):
        dispatched.append(
            (
                args[12],
                kwargs["queue_wait_seconds"],
                _IMAGE_JOB_ALREADY_SCHEDULED.get(),
            )
        )
        return {"success": True}

    plugin._image_scheduler = Scheduler()
    plugin.get_web_image_task = lambda _task_id: {"trace_id": "trace-runtime"}
    plugin._set_web_image_task = lambda task_id, **fields: stages.append(
        (task_id, fields["generation_stage"], fields["generation_stage_label"])
    )
    plugin._run_image_generation_unscheduled = run_unscheduled

    result = asyncio.run(
        plugin._run_image_generation(
            "runtime test",
            "1:1",
            "1K",
            [],
            record_context={"task_id": "task-runtime"},
        )
    )

    assert result == {"success": True}
    assert plugin._image_scheduler.task_id == "task-runtime"
    assert stages == [
        ("task-runtime", "queue", "等待图片并发槽位"),
        ("task-runtime", "generating", "生成图片结果"),
    ]
    assert dispatched[0][0]["trace_id"] == "trace-runtime"
    assert dispatched[0][1] >= 0
    assert dispatched[0][2] is True
    assert _IMAGE_JOB_ALREADY_SCHEDULED.get() is False


def test_runtime_helpers_use_overridable_plugin_hooks_and_legacy_slots():
    plugin = object.__new__(SelfieImagePlugin)
    plugin.config = SimpleNamespace(image_max_concurrent_tasks=2)
    plugin._image_scheduler = object()
    plugin._image_scheduler_snapshot = lambda: {
        "limit": 1,
        "active": 1,
        "pending": 0,
        "queued_tasks": 0,
    }

    assert plugin._image_batch_queue_expected(10)

    plugin._image_scheduler = None
    assert plugin._image_inflight_limit() == 2
    gate = plugin._ensure_image_batch_gate()
    assert gate._value == 2
    plugin._web_task_lock = threading.RLock()
    plugin._web_tasks = {"cancelled": {"cancel_requested": True}}
    assert asyncio.run(plugin._acquire_image_slot("cancelled")) is None
