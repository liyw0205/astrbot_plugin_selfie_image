"""Shared runtime coordination for image generation jobs."""

from __future__ import annotations

import asyncio
import contextvars
import time
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from ..core.models import ImageModelTarget
from ..core.providers import ImageReference


IMAGE_BATCH_REQUEST_COOLDOWN_SECONDS = 2.0
_IMAGE_JOB_ALREADY_SCHEDULED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "image_job_already_scheduled", default=False
)


class GenerationRuntimeService:
    """Coordinate scheduling, legacy batch slots and runtime shutdown."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def set_generation_stage(
        self,
        record_context: Optional[Mapping[str, Any]],
        stage: str,
        label: str,
    ) -> None:
        plugin = self.plugin
        if not isinstance(record_context, Mapping):
            return
        task_id = str(record_context.get("task_id") or "").strip()
        setter = getattr(plugin, "_set_web_image_task", None)
        if not task_id or not callable(setter):
            return
        try:
            setter(
                task_id,
                generation_stage=str(stage or "").strip(),
                generation_stage_label=str(label or "").strip(),
            )
        except Exception:
            return

    async def run_image_generation(
        self,
        prompt: str,
        aspect_ratio: str,
        resolution: str,
        refs: List[ImageReference],
        targets: Optional[List[ImageModelTarget]] = None,
        source: str = "command",
        audit_user_id: str = "",
        event: Any = None,
        original_prompt: str = "",
        max_attempts: Optional[int] = None,
        allow_compat_retry: bool = True,
        prompt_en_meta: Optional[Dict[str, Any]] = None,
        record_context: Optional[Mapping[str, Any]] = None,
        reference_selection: Optional[Mapping[str, Any]] = None,
        reference_fidelity: str = "",
    ) -> Dict[str, Any]:
        plugin = self.plugin
        if getattr(plugin, "_terminating", False):
            raise asyncio.CancelledError()
        context = dict(record_context) if isinstance(record_context, Mapping) else {}
        task_id = str(context.get("task_id") or "").strip()
        if task_id and not str(context.get("trace_id") or "").strip():
            task_getter = getattr(plugin, "get_web_image_task", None)
            if callable(task_getter):
                try:
                    task_snapshot = task_getter(task_id)
                except Exception:
                    task_snapshot = None
                if isinstance(task_snapshot, Mapping):
                    trace_id = str(task_snapshot.get("trace_id") or "").strip()
                    if trace_id:
                        context["trace_id"] = trace_id
        record_context = context or record_context
        scheduler = getattr(plugin, "_image_scheduler", None)
        if scheduler is None or _IMAGE_JOB_ALREADY_SCHEDULED.get():
            return await plugin._run_image_generation_unscheduled(
                prompt, aspect_ratio, resolution, refs, targets, source, audit_user_id,
                event, original_prompt, max_attempts, allow_compat_retry, prompt_en_meta,
                record_context, reference_selection, reference_fidelity,
            )
        context = record_context if isinstance(record_context, Mapping) else {}
        task_id = str(context.get("task_id") or "").strip()
        if not task_id:
            task_id = f"direct-{id(asyncio.current_task())}"

        plugin._set_generation_stage(
            record_context,
            "queue",
            "等待画布并发槽位" if source == "studio-run" else "等待图片并发槽位",
        )
        submitted_at = time.monotonic()

        async def run() -> Dict[str, Any]:
            slot_token = _IMAGE_JOB_ALREADY_SCHEDULED.set(True)
            try:
                plugin._set_generation_stage(
                    record_context,
                    "generating",
                    "生成画布结果" if source == "studio-run" else "生成图片结果",
                )
                return await plugin._run_image_generation_unscheduled(
                    prompt, aspect_ratio, resolution, refs, targets, source, audit_user_id,
                    event, original_prompt, max_attempts, allow_compat_retry, prompt_en_meta,
                    record_context, reference_selection, reference_fidelity,
                    queue_wait_seconds=max(0.0, time.monotonic() - submitted_at),
                )
            finally:
                _IMAGE_JOB_ALREADY_SCHEDULED.reset(slot_token)

        return await scheduler.submit(task_id, run)

    def image_inflight_limit(self) -> int:
        plugin = self.plugin
        scheduler = getattr(plugin, "_image_scheduler", None)
        if scheduler is None:
            try:
                configured = int(getattr(plugin.config, "image_max_concurrent_tasks", 5) or 5)
            except (TypeError, ValueError):
                configured = 5
            return max(1, min(5, configured))
        return max(1, min(5, int((scheduler._limit if hasattr(scheduler, "_limit") else 1) or 1)))

    def image_scheduler_snapshot(self) -> Dict[str, int]:
        plugin = self.plugin
        scheduler = getattr(plugin, "_image_scheduler", None)
        if scheduler is None:
            return {
                "limit": plugin._image_inflight_limit(),
                "active": 0,
                "pending": 0,
                "queued_tasks": 0,
            }
        return {
            "limit": int(getattr(scheduler, "_limit", plugin._image_inflight_limit()) or 1),
            "active": int(getattr(scheduler, "_active", 0) or 0),
            "pending": sum(len(queue) for queue in getattr(scheduler, "_pending", {}).values()),
            "queued_tasks": len(getattr(scheduler, "_round_robin", ())),
        }

    def image_batch_queue_expected(self, total: int = 1) -> bool:
        plugin = self.plugin
        scheduler = getattr(plugin, "_image_scheduler", None)
        if scheduler is not None:
            snapshot = plugin._image_scheduler_snapshot()
            if snapshot["active"] >= snapshot["limit"]:
                return True
        gate = getattr(plugin, "_image_batch_gate", None)
        if gate is not None:
            return int(getattr(gate, "_value", 0)) <= 0
        return False

    def ensure_image_batch_gate(self) -> asyncio.Semaphore:
        plugin = self.plugin
        gate = getattr(plugin, "_image_batch_gate", None)
        if gate is None or not isinstance(gate, asyncio.Semaphore):
            gate = asyncio.Semaphore(plugin._image_inflight_limit())
            plugin._image_batch_gate = gate
        plugin._selfie_batch_gate = gate
        return gate

    async def acquire_image_slot(self, task_id: str) -> Optional[asyncio.Semaphore]:
        plugin = self.plugin
        gate = plugin._ensure_image_batch_gate()
        while not plugin._task_cancel_requested(task_id):
            try:
                await asyncio.wait_for(gate.acquire(), timeout=1.0)
                return gate
            except asyncio.TimeoutError:
                continue
        return None

    def ensure_image_batch_cooldown_lock(self) -> asyncio.Lock:
        plugin = self.plugin
        lock = getattr(plugin, "_image_batch_cooldown_lock", None)
        if lock is None or not isinstance(lock, asyncio.Lock):
            plugin._image_batch_cooldown_lock = asyncio.Lock()
            lock = plugin._image_batch_cooldown_lock
        return lock

    async def wait_for_image_batch_cooldown(
        self, task_id: str, cooldown_seconds: float = IMAGE_BATCH_REQUEST_COOLDOWN_SECONDS
    ) -> bool:
        plugin = self.plugin
        lock = plugin._ensure_image_batch_cooldown_lock()
        async with lock:
            if plugin._task_cancel_requested(task_id):
                return False
            await asyncio.sleep(cooldown_seconds)
            return not plugin._task_cancel_requested(task_id)

    async def close_scheduler(self) -> None:
        scheduler = getattr(self.plugin, "_image_scheduler", None)
        if scheduler is not None:
            await scheduler.aclose()
