"""Bounded batch generation with incremental progress and delivery receipts."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, List


class CountedGenerationWorkflow:
    """Keep per-batch state separate from the plugin and its shared scheduler."""

    def __init__(
        self,
        plugin: Any,
        *,
        task_id: str,
        event: Any,
        total: int,
        fail_label: str,
        run_one: Callable,
        log_prefix: str,
        log_info: Callable[[str], Any],
    ) -> None:
        self.plugin = plugin
        self.task_id = task_id
        self.event = event
        self.total = max(1, int(total))
        self.fail_label = fail_label
        self.run_one = run_one
        self.log_prefix = log_prefix
        self.log_info = log_info
        self.inflight = min(plugin._image_inflight_limit(), self.total)
        self.sem = asyncio.Semaphore(self.inflight)
        self.send_lock = asyncio.Lock()
        self.stop = False
        self.skipped_shots = 0
        self.all_files: List[str] = []
        self.used_model = ""
        self.last_elapsed = 0.0
        self.failed_at = 0
        self.last_failure_error = ""
        self.all_targets_cooling_down = False
        self.cancelled = False
        self.succeeded_shots = 0
        self.cancelled_shots = 0
        self.delivery_failed_shots = 0
        self.delivery_unknown_shots = 0
        self.delivery_error = ""
        self.all_attempts: List[Dict[str, Any]] = []
        self.final_prompt = ""
        self.original_prompt = ""
        self.last_request_data: Dict[str, Any] = {}

    async def run(self) -> Dict[str, Any]:
        await asyncio.gather(*(self._one(index) for index in range(self.total)))
        result = self._summarize_result()
        if (
            self.all_targets_cooling_down
            and not self.cancelled
            and not self.plugin._task_cancel_requested(self.task_id)
        ):
            await self.plugin._send_task_notification(
                self.task_id,
                self.event,
                "任务已结束：所有可用出图渠道均已进入冷却。\n"
                f"计划 {self.total} 张，成功 {self.succeeded_shots} 张，"
                f"失败 {self.skipped_shots} 张，未执行 {result['batch_unstarted']} 张。",
            )
        return result

    def _persist_progress(self, index: int = 0) -> None:
        completed = min(self.total, self.succeeded_shots + self.skipped_shots + self.cancelled_shots)
        self.plugin._set_web_image_task(
            self.task_id,
            requested_count=self.total,
            completed_count=completed,
            succeeded_count=self.succeeded_shots,
            failed_count=self.skipped_shots,
            progress_percent=int(round(completed * 100 / max(1, self.total))),
            current_index=max(0, int(index or 0)),
        )

    async def _one(self, index: int) -> None:
        plugin = self.plugin
        async with self.sem:
            if self.stop or plugin._task_cancel_requested(self.task_id):
                if plugin._task_cancel_requested(self.task_id):
                    async with self.send_lock:
                        self.cancelled = True
                        self.cancelled_shots += 1
                        self._persist_progress(index + 1)
                return
            self.log_info(
                f"[SelfieImage] {self.log_prefix} {index + 1}/{self.total} inflight={self.inflight} task={self.task_id}"
            )
            with plugin._web_task_lock:
                task_snapshot = dict(plugin._web_tasks.get(self.task_id) or {})
            if "requested_count" in task_snapshot:
                plugin._set_web_image_task(
                    self.task_id,
                    generation_stage="queue",
                    generation_stage_label="等待图片并发槽位",
                    requested_count=self.total,
                    completed_count=int(task_snapshot.get("completed_count") or 0),
                )
            legacy_slot = None
            try:
                if getattr(plugin, "_image_scheduler", None) is None:
                    legacy_slot = await plugin._acquire_image_slot(self.task_id)
                    if legacy_slot is None:
                        async with self.send_lock:
                            self.cancelled = True
                            self.cancelled_shots += 1
                            self._persist_progress(index + 1)
                        return
                    if self.total > 1 and not await plugin._wait_for_image_batch_cooldown(self.task_id):
                        legacy_slot.release()
                        legacy_slot = None
                        async with self.send_lock:
                            self.cancelled = True
                            self.cancelled_shots += 1
                            self._persist_progress(index + 1)
                        return
                result = await self.run_one(index)
                if result.get("all_targets_cooling_down"):
                    # Close admission before releasing the slot or waiting
                    # for record commits. Already-running shots still drain.
                    self.all_targets_cooling_down = True
                    self.stop = True
                    if not self.failed_at:
                        self.failed_at = index + 1
            finally:
                if legacy_slot is not None:
                    legacy_slot.release()
        # Await the worker's record commit before publishing progress, so a
        # terminal snapshot cannot temporarily expose an empty record_ids list.
        wait_commits = getattr(plugin, "_wait_for_record_commits", None)
        if callable(wait_commits):
            await wait_commits(self.task_id)
        if isinstance(result, dict):
            self.final_prompt = str(result.get("final_prompt") or self.final_prompt)
            self.original_prompt = str(result.get("original_prompt") or self.original_prompt)
            if isinstance(result.get("request_data"), dict):
                self.last_request_data = dict(result["request_data"])
        async with self.send_lock:
            raw_attempts = result.get("attempts") if isinstance(result, dict) else None
            if isinstance(raw_attempts, list):
                self.all_attempts.extend(item for item in raw_attempts if isinstance(item, dict))
            # Stop only prevents pending shots. Results already in flight must
            # still be handled after another shot reports a failure.
            if plugin._task_cancel_requested(self.task_id):
                self.cancelled = True
                self.stop = True
                self.cancelled_shots += 1
                self._persist_progress(index + 1)
                return
            if not result.get("success"):
                await self._handle_failure(index, result)
                return
            await self._handle_success(index, result)

    async def _handle_failure(self, index: int, result: Dict[str, Any]) -> None:
        plugin = self.plugin
        raw_error = str(result.get("failure_reason") or result.get("error") or "")
        if not raw_error:
            attempts = result.get("attempts") or []
            if isinstance(attempts, list) and attempts:
                last = attempts[-1] if isinstance(attempts[-1], dict) else {}
                raw_error = str(last.get("error_user_message") or last.get("error") or "")
        self.last_failure_error = raw_error
        error = plugin._friendly_user_error_message(raw_error, self.fail_label)
        self.skipped_shots += 1
        self._persist_progress(index + 1)
        if self.all_targets_cooling_down:
            return
        mode, skip_max = plugin._batch_failure_policy()
        # Completion order can differ from shot index. Count terminal shots to
        # decide whether another queued or in-flight shot remains.
        completed_shots = self.succeeded_shots + self.skipped_shots + self.cancelled_shots
        has_remaining = completed_shots < self.total
        will_continue = has_remaining and (
            mode == "skip" or (mode == "skip_max" and self.skipped_shots <= skip_max)
        )
        message = plugin._batch_shot_fail_text(
            index=index + 1,
            total=self.total,
            done_files=len(self.all_files),
            error=error,
            mode=mode,
            skipped=self.skipped_shots,
            skip_max=skip_max,
            will_continue=will_continue,
        )
        await plugin._send_task_notification(self.task_id, self.event, message)
        if not will_continue:
            self.stop = True
            self.failed_at = index + 1

    async def _handle_success(self, index: int, result: Dict[str, Any]) -> None:
        plugin = self.plugin
        files = list(result.get("files") or [])
        self.used_model = str(result.get("used_model") or self.used_model)
        self.last_elapsed = float(result.get("elapsed_seconds") or self.last_elapsed)
        if files:
            await plugin._record_generated_images_async(self.event, 1)
            failed_before = self.delivery_failed_shots
            unknown_before = self.delivery_unknown_shots
            await self._deliver_files(files)
            self.all_files.extend(files)
            self.succeeded_shots += 1
        self._persist_progress(index + 1)
        if files and self.delivery_failed_shots > failed_before:
            await plugin._send_task_notification(
                self.task_id,
                self.event,
                "图片已生成，但发送失败。可使用 /生图重发 重试发送。",
            )
        elif files and self.delivery_unknown_shots > unknown_before:
            await plugin._send_task_notification(
                self.task_id,
                self.event,
                "图片已生成，但消息投递结果未知。请先检查会话，避免重复发送。",
            )
        else:
            info = plugin._batch_success_text(
                plugin._build_success_text(self.last_elapsed, len(files), self.used_model, self.event),
                index + 1,
                self.total,
            )
            if info:
                await plugin._send_task_notification(self.task_id, self.event, info)

    async def _deliver_files(self, files: List[str]) -> None:
        plugin = self.plugin
        send_started = time.monotonic()
        sent = await plugin._send_generated_images(self.event, files)
        delivery_elapsed = max(0.0, time.monotonic() - send_started)
        sent_count = int(sent or 0)
        explicit_failed = max(0, int(getattr(sent, "failed_count", 0) or 0))
        unknown = max(0, int(getattr(sent, "unknown_count", 0) or 0))
        self.delivery_unknown_shots += unknown
        # A transport timeout can follow a successful send. Keep ambiguous
        # receipts separate so they are never promoted to confirmed failure.
        if sent is None:
            delivered = True
            delivery_unknown = False
        elif explicit_failed:
            delivered = False
            delivery_unknown = False
            self.delivery_failed_shots += explicit_failed
            self.delivery_error = "部分生成结果发送失败，请在记录或失败图片入口重试"
        elif unknown:
            delivered = False
            delivery_unknown = True
        elif sent_count >= len(files):
            delivered = True
            delivery_unknown = False
        else:
            delivered = False
            delivery_unknown = False
            self.delivery_failed_shots += len(files) - sent_count
            self.delivery_error = "部分生成结果发送失败，请在记录或失败图片入口重试"
        plugin._set_task_notification_status(
            self.task_id,
            "unknown" if delivery_unknown else "sent" if delivered else "failed",
            "消息投递结果未知" if delivery_unknown else self.delivery_error if not delivered else "",
        )
        wait_commits = getattr(plugin, "_wait_for_record_commits", None)
        update_delivery = getattr(plugin, "_update_generation_records_delivery", None)
        update_delivery_timing = getattr(plugin, "_update_generation_records_delivery_timing", None)
        if callable(wait_commits):
            await wait_commits(self.task_id)
        if callable(update_delivery) and not delivery_unknown:
            update_delivery(
                self.task_id,
                delivered=delivered,
                error="图片发送失败" if not delivered else "",
                paths=files,
                delivery_elapsed_seconds=delivery_elapsed,
            )
        elif delivery_unknown and callable(update_delivery_timing):
            update_delivery_timing(
                self.task_id,
                elapsed_seconds=delivery_elapsed,
                paths=files,
            )

    def _summarize_result(self) -> Dict[str, Any]:
        generation_success = self.succeeded_shots > 0
        delivery_failed = self.delivery_failed_shots > 0
        delivery_unknown = self.delivery_unknown_shots > 0 and not delivery_failed
        delivery_success = (
            False if delivery_failed else
            None if delivery_unknown or not generation_success else
            True
        )
        if self.cancelled:
            error = "任务已取消"
        elif self.all_targets_cooling_down:
            error = "所有可用出图渠道均已进入冷却，任务提前结束"
        elif self.failed_at:
            error = self.last_failure_error or self.fail_label or "生图没有完成"
        elif delivery_failed:
            error = self.delivery_error or "生成成功但发送失败"
        else:
            error = ""
        result = {
            "success": not self.cancelled and not self.failed_at and self.skipped_shots == 0 and not delivery_failed,
            "error": error,
            "cancelled": self.cancelled,
            "files": self.all_files,
            "original_prompt": self.original_prompt,
            "final_prompt": self.final_prompt,
            "request_data": self.last_request_data,
            "used_model": self.used_model,
            "elapsed_seconds": self.last_elapsed,
            "generation_success": generation_success,
            "delivery_success": delivery_success,
            "delivery_unknown": delivery_unknown,
            "delivery_failed": delivery_failed,
            "delivery_unknown_count": self.delivery_unknown_shots,
            "delivery_failed_count": self.delivery_failed_shots,
            "batch_total": self.total,
            "batch_skipped": self.skipped_shots,
            "completed_count": min(self.total, self.succeeded_shots + self.skipped_shots + self.cancelled_shots),
            "succeeded_count": self.succeeded_shots,
            "failed_count": self.skipped_shots,
            "attempts": self.all_attempts,
        }
        if self.cancelled:
            result["status"] = "cancelled"
        elif delivery_failed and generation_success:
            result["status"] = "delivery_failed"
        if self.failed_at:
            result["batch_failed_at"] = self.failed_at
        if self.all_targets_cooling_down:
            result["all_targets_cooling_down"] = True
            result["batch_unstarted"] = max(0, self.total - result["completed_count"])
        return self.plugin._normalize_generation_result(result, self.total)
