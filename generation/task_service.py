"""Command-task creation and lifecycle orchestration."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterable, Mapping
from typing import Any, Dict, Optional

from astrbot.api import logger

from ..core.providers import ImageReference
from ..core.utils import event_user_id, new_trace_id, redact_sensitive_data, redact_sensitive_text
from ..features.access_policy import quota_error_message
from .generation_results import build_task_terminal_state


class GenerationTaskService:
    """Own command-task lifecycle decisions while delegating storage to the plugin."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def quota_error_message(self, event: Any, requested_count: int = 1) -> str:
        plugin = self.plugin
        permission_error = plugin._permission_denied_message(event)
        if permission_error:
            return permission_error
        status = plugin._access_status(event)
        status["allowed"] = True
        user_id = event_user_id(event)
        lock = getattr(plugin, "_quota_reservation_lock", None)
        if lock is None:
            lock = threading.RLock()
            plugin._quota_reservation_lock = lock
        reservations = getattr(plugin, "_quota_reservations", {})
        with lock:
            reserved = sum(
                int(item.get("count") or 0)
                for item in reservations.values()
                if str(item.get("user_id") or "") == user_id
            )
        return quota_error_message(
            status,
            plugin._current_usage_stats(),
            enabled=plugin.config.image_enable_daily_limit,
            limit=plugin.config.image_daily_limit_count,
            requested_count=max(1, int(requested_count or 1)) + reserved,
        )

    def reserve_quota_for_task(
        self,
        task_id: str,
        event: Any,
        requested_count: int,
        *,
        media_type: str = "image",
    ) -> str:
        plugin = self.plugin
        count = max(1, int(requested_count or 1))
        if not bool(getattr(plugin.config, "image_enable_daily_limit", False)):
            return ""
        status = plugin._access_status(event)
        if status.get("unlimited") or not status.get("allowed"):
            return ""
        user_id = event_user_id(event)
        reservation_lock = getattr(plugin, "_quota_reservation_lock", None)
        if reservation_lock is None:
            reservation_lock = threading.RLock()
            plugin._quota_reservation_lock = reservation_lock
        reservations = getattr(plugin, "_quota_reservations", None)
        if reservations is None:
            reservations = {}
            plugin._quota_reservations = reservations
        usage_lock = getattr(plugin, "_usage_lock", None)
        if usage_lock is None:
            usage_lock = threading.RLock()
            plugin._usage_lock = usage_lock
        with reservation_lock, usage_lock:
            stats = plugin._current_usage_stats()
            users = stats.get("users") if isinstance(stats.get("users"), dict) else {}
            row = users.get(user_id) if isinstance(users.get(user_id), dict) else {}
            used = int(row.get("count") or 0)
            reserved = sum(
                int(item.get("count") or 0)
                for item in reservations.values()
                if str(item.get("user_id") or "") == user_id
            )
            limit = int(getattr(plugin.config, "image_daily_limit_count", 0) or 0)
            if used + reserved + count > limit:
                return f"今日生图次数已用完：{used + reserved}/{limit}。"
            reservations[str(task_id)] = {
                "user_id": user_id,
                "count": count,
                "created_ts": time.time(),
            }
        return ""

    def release_quota_reservation(self, task_id: str) -> None:
        plugin = self.plugin
        lock = getattr(plugin, "_quota_reservation_lock", None)
        reservations = getattr(plugin, "_quota_reservations", None)
        released = None
        if lock is not None and reservations is not None:
            with lock:
                released = reservations.pop(str(task_id or ""), None)
        task_lock = getattr(plugin, "_web_task_lock", None)
        tasks = getattr(plugin, "_web_tasks", None)
        if task_lock is None or not isinstance(tasks, dict):
            return
        tid = str(task_id or "").strip()
        if not tid:
            return
        with task_lock:
            task = tasks.get(tid)
            if not isinstance(task, dict):
                return
            if released is None and not task.get("quota_reserved") and task.get("quota_released"):
                return
            task["quota_reserved"] = 0
            task["quota_released"] = True
            task["quota_released_ts"] = time.time()
            task["updated_ts"] = time.time()
            task["updated_at"] = plugin._web_task_timestamp()
            plugin._persist_web_tasks_locked()

    def cancel_image_task(
        self,
        task_id: str,
        *,
        session_key: str = "",
        is_admin: bool = False,
    ) -> str:
        plugin = self.plugin
        tid = str(task_id or "").strip()
        if not tid:
            raise ValueError("请提供任务ID")
        with plugin._web_task_lock:
            task = plugin._web_tasks.get(tid)
            if not task:
                raise ValueError("任务不存在或已清理")
            reconcile = getattr(plugin, "_reconcile_task_terminal_evidence_locked", None)
            if callable(reconcile):
                reconcile(task)
            owner = str(task.get("owner_session") or "")
            if owner and session_key and owner != session_key and not is_admin:
                raise PermissionError("不能取消其他会话的生图任务")
            status = str(task.get("status") or "")
            capabilities = plugin.task_operation_capabilities(task)
            if capabilities["is_terminal"]:
                return f"这单已经结束了（{status}），不用再取消"
            if not capabilities["can_cancel"] and task.get("cancel_requested"):
                return f"已请求取消 {tid}，任务将在安全边界停止"
            task["cancel_requested"] = True
            scheduler = getattr(plugin, "_image_scheduler", None)
            if scheduler is not None:
                loop = getattr(plugin, "loop", None)
                if loop is not None and loop.is_running():
                    def schedule_cancel() -> None:
                        cancel = loop.create_task(scheduler.cancel_task(tid))
                        cancel.add_done_callback(plugin._log_scheduler_cancel_error)
                    loop.call_soon_threadsafe(schedule_cancel)
            now = time.time()
            runtime_task = getattr(plugin, "_runtime_generation_tasks", {}).get(tid)
            if runtime_task is None:
                task["status"] = "cancelled"
                task["success"] = False
                task["error"] = "任务已取消"
                task["finished_ts"] = now
                task["finished_at"] = plugin._web_task_timestamp()
                plugin._log_task_lifecycle_event(
                    str(task.get("task_id") or tid),
                    str(task.get("trace_id") or ""),
                    plugin._task_media_type(task),
                    status.strip().lower(),
                    "cancelled",
                )
            task["updated_ts"] = now
            task["updated_at"] = plugin._web_task_timestamp()
            plugin._persist_web_tasks_locked()
            return "已立即取消 " + tid if runtime_task is None else f"已请求取消 {tid}，任务将在安全边界停止"

    @staticmethod
    def log_scheduler_cancel_error(future: asyncio.Future) -> None:
        if not future.cancelled():
            exc = future.exception()
            if exc is not None:
                logger.error("[SelfieImage] 取消图片请求失败: %s", exc)

    def task_cancel_requested(self, task_id: str) -> bool:
        plugin = self.plugin
        with plugin._web_task_lock:
            task = plugin._web_tasks.get(task_id)
            return bool(task and task.get("cancel_requested"))

    def mark_task_delivery_failed(self, task_id: str, error: str) -> None:
        plugin = self.plugin
        tid = str(task_id or "").strip()
        if not tid:
            return
        message = redact_sensitive_text(str(error or "生成成功但发送失败"))[:800]
        lifecycle_event = None
        with plugin._web_task_lock:
            task = plugin._web_tasks.get(tid)
            if not isinstance(task, dict):
                return
            previous_status = str(task.get("status") or "").strip().lower()
            result = task.get("result") if isinstance(task.get("result"), dict) else {}
            result = dict(result)
            result.update(
                {
                    "success": False,
                    "status": "delivery_failed",
                    "generation_success": True,
                    "delivery_success": False,
                    "delivery_failed": True,
                    "error": message,
                }
            )
            task.update(
                {
                    "status": "delivery_failed",
                    "success": False,
                    "generation_success": True,
                    "delivery_success": False,
                    "delivery_failed": True,
                    "error": message,
                    "result": result,
                    "finished_ts": time.time(),
                    "finished_at": plugin._web_task_timestamp(),
                }
            )
            task["updated_ts"] = task["finished_ts"]
            task["updated_at"] = task["finished_at"]
            plugin._persist_web_tasks_locked()
            current_status = str(task.get("status") or "").strip().lower()
            if previous_status != current_status:
                lifecycle_event = (
                    str(task.get("task_id") or tid),
                    str(task.get("trace_id") or ""),
                    plugin._task_media_type(task),
                    previous_status,
                    current_status,
                )
        if lifecycle_event:
            plugin._log_task_lifecycle_event(*lifecycle_event)

    def start_command_image_task(
        self,
        event: Any,
        *,
        source: str,
        summary: Dict[str, Any],
        runner: Any,
        reference_images: Optional[Iterable[ImageReference]] = None,
    ) -> Dict[str, Any]:
        plugin = self.plugin
        session_key = plugin._session_key(event)
        request_summary = dict(summary or {})
        request_summary.setdefault("source", str(source or "").strip())
        request_summary["owner_user_id"] = event_user_id(event)
        if not request_summary.get("requested_model"):
            request_summary["requested_model"] = plugin._get_session_model_override(event)
        request_summary["owner_origin"] = str(getattr(event, "unified_msg_origin", "") or "")
        force_regenerate = bool(request_summary.get("force_regenerate") or request_summary.get("force"))
        fingerprint = plugin._request_fingerprint(request_summary, session_key, reference_images)
        with plugin._web_task_lock:
            checker = getattr(plugin, "_ensure_generation_available", None)
            if callable(checker):
                checker()
            elif getattr(plugin, "_terminating", False):
                raise RuntimeError("插件正在卸载，无法提交新任务")
            if not force_regenerate:
                duplicate = plugin._find_recent_duplicate_task_locked(fingerprint)
                if duplicate and duplicate.get("owner_session") == session_key:
                    duplicate["deduplicated"] = True
                    return redact_sensitive_data(duplicate)
            plugin._web_task_seq += 1
            task_id = f"cmd-{int(time.time() * 1000)}-{plugin._web_task_seq}"
            trace_id = new_trace_id("gen")
            now = time.time()
            media_type = "image"
            requested_count = max(1, int(request_summary.get("requested_count") or request_summary.get("count") or 1))
            quota_error = plugin._reserve_quota_for_task(
                task_id,
                event,
                requested_count,
                media_type=media_type,
            )
            reservation_lock = getattr(plugin, "_quota_reservation_lock", None)
            if reservation_lock is None:
                reservation_lock = threading.RLock()
                plugin._quota_reservation_lock = reservation_lock
            reservations = getattr(plugin, "_quota_reservations", {})
            with reservation_lock:
                reserved_count = int((reservations.get(task_id) or {}).get("count") or 0)
            plugin._web_tasks[task_id] = {
                "task_id": task_id,
                "trace_id": trace_id,
                "status": "failed" if quota_error else "queued",
                "success": False if quota_error else None,
                "error": quota_error,
                "created_ts": now,
                "updated_ts": now,
                "created_at": plugin._web_task_timestamp(),
                "updated_at": plugin._web_task_timestamp(),
                "request_data": redact_sensitive_data(request_summary),
                "result": None,
                "source": source,
                "owner_session": session_key,
                "owner_user_id": event_user_id(event),
                "cancel_requested": False,
                "request_fingerprint": fingerprint,
                "deduplicated": False,
                "quota_reserved": reserved_count,
                "quota_released": not bool(reserved_count),
                "quota_released_ts": now if not reserved_count else None,
                "quota_error": quota_error,
                "user_notification_status": "pending",
                "user_notification_reason": "",
                **plugin._task_runtime_defaults(),
                **plugin._task_progress_defaults(requested_count),
                "generation_stage": "preflight",
                "generation_stage_label": "准备图片任务",
            }
            plugin._prune_web_tasks_locked()
            plugin._persist_web_tasks_locked()
        initial_status = "failed" if quota_error else "queued"
        plugin._log_task_lifecycle_event(task_id, trace_id, media_type, "", initial_status)
        plugin._log_task_notification_event(task_id, trace_id, media_type, "message", "", "pending")
        if quota_error:
            return plugin.get_web_image_task(task_id)
        plugin._start_runtime_generation(task_id, plugin._run_command_image_task(task_id, event, runner))
        return plugin.get_web_image_task(task_id)

    async def await_command_image_task(self, task: Mapping[str, Any]) -> Dict[str, Any]:
        plugin = self.plugin
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            return {"success": False, "error": "任务创建失败"}
        runtime_task = getattr(plugin, "_runtime_generation_tasks", {}).get(task_id)
        if runtime_task is not None:
            try:
                await asyncio.shield(runtime_task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[SelfieImage] 等待任务 {task_id} 失败: {exc}")
        try:
            completed = plugin.get_web_image_task(task_id)
        except Exception as exc:
            return {"success": False, "error": redact_sensitive_text(str(exc))}
        result = completed.get("result")
        if isinstance(result, dict):
            return dict(result)
        return {
            "success": bool(completed.get("success")),
            "error": str(completed.get("error") or "任务没有返回结果"),
        }

    async def run_command_image_task(self, task_id: str, event: Any, runner: Any) -> None:
        plugin = self.plugin
        if plugin._task_cancel_requested(task_id):
            await plugin._ensure_task_failure_record(
                task_id,
                error="任务已取消",
                cancelled=True,
                stage="cancelled",
            )
            plugin._release_quota_reservation(task_id)
            plugin._set_task_notification_status(task_id, "not_sent", "任务在开始生成前已取消")
            plugin._set_web_image_task(
                task_id,
                status="cancelled",
                success=False,
                generation_stage="cancelled",
                generation_stage_label="已取消",
                error="任务已取消",
                finished_ts=time.time(),
                finished_at=plugin._web_task_timestamp(),
            )
            return
        with plugin._web_task_lock:
            task_meta = dict(plugin._web_tasks.get(task_id) or {})
        plugin._set_web_image_task(
            task_id,
            status="running",
            started_ts=time.time(),
            started_at=plugin._web_task_timestamp(),
            generation_stage="preflight",
            generation_stage_label="准备图片任务",
        )
        try:
            if plugin._task_cancel_requested(task_id):
                raise RuntimeError("任务已取消")
            result = await runner(task_id)
            with plugin._web_task_lock:
                requested_count = int((plugin._web_tasks.get(task_id) or {}).get("requested_count") or 1)
            result = redact_sensitive_data(result)
            terminal = build_task_terminal_state(
                result,
                requested_count=requested_count,
                cancel_requested=plugin._task_cancel_requested(task_id),
            )
            result = terminal["result"]
            success = terminal["success"]
            generation_success = terminal["generation_success"]
            delivery_unknown = terminal["delivery_unknown"]
            delivery_failed = terminal["delivery_failed"]
            error = redact_sensitive_text(terminal["error"])
            cancelled_result = terminal["cancelled_result"]
            notification_status = plugin._task_notification_status(task_id)
            if delivery_unknown:
                plugin._set_task_notification_status(task_id, "unknown", "消息投递结果未知")
            elif cancelled_result:
                plugin._set_task_notification_status(task_id, "not_sent", "任务已取消，未发送生成结果")
            elif notification_status in {"", "pending"}:
                plugin._set_task_notification_status(task_id, "unknown", "任务完成但未收到消息投递回执")
            wait_commits = getattr(plugin, "_wait_for_record_commits", None)
            if callable(wait_commits):
                await wait_commits(task_id)
            partial_success = terminal["partial_success"]
            terminal_stage = terminal["terminal_stage"]
            terminal_stage_label = (
                "已取消"
                if terminal_stage == "cancelled"
                else "部分完成"
                if partial_success
                else "已完成"
                if terminal_stage == "complete"
                else "已失败"
            )
            plugin._set_web_image_task(
                task_id,
                status=terminal["terminal_status"],
                success=success,
                generation_stage=terminal_stage,
                generation_stage_label=terminal_stage_label,
                generation_success=generation_success,
                delivery_success=(
                    False
                    if delivery_failed
                    else None
                    if delivery_unknown
                    else True
                    if success
                    else result.get("delivery_success")
                ),
                delivery_failed=delivery_failed,
                delivery_unknown=delivery_unknown,
                error=error,
                requested_count=result.get("requested_count", 1),
                succeeded_count=result.get("succeeded_count", 0),
                failed_count=result.get("failed_count", 0),
                result=result,
                cancel_requested=False if not cancelled_result else True,
                finished_ts=time.time(),
                finished_at=plugin._web_task_timestamp(),
            )
        except asyncio.CancelledError:
            await plugin._ensure_task_failure_record(
                task_id,
                error="任务已取消",
                cancelled=True,
                stage="cancelled",
            )
            plugin._set_web_image_task(
                task_id,
                status="cancelled",
                success=False,
                generation_stage="cancelled",
                generation_stage_label="已取消",
                error="任务已取消",
                result={"success": False, "error": "任务已取消", "cancelled": True},
                finished_ts=time.time(),
                finished_at=plugin._web_task_timestamp(),
            )
            plugin._set_task_notification_status(task_id, "not_sent", "任务执行被取消")
            return
        except Exception as exc:
            error = redact_sensitive_text(str(exc))
            cancelled = "取消" in error
            try:
                with plugin._web_task_lock:
                    task_meta = dict((plugin._web_tasks or {}).get(task_id) or {})
            except Exception:
                task_meta = {}
            source = str(task_meta.get("source") or "command-task")
            summary = task_meta.get("request_data") or task_meta.get("summary") or {}
            summary = summary if isinstance(summary, dict) else {}
            prompt = str(
                summary.get("prompt")
                or summary.get("action")
                or summary.get("original_prompt")
                or ""
            )
            await plugin._ensure_task_failure_record(
                task_id,
                error=error,
                cancelled=cancelled,
                stage="cancelled" if cancelled else "task_exception",
                media_type="image",
                source=source,
                prompt=str(summary.get("raw_original_prompt") or prompt),
                request_data=summary,
            )
            plugin._set_web_image_task(
                task_id,
                status="cancelled" if cancelled else "failed",
                success=False,
                generation_stage="cancelled" if cancelled else "failed",
                generation_stage_label="已取消" if cancelled else "已失败",
                error=error,
                result={"success": False, "error": error},
                finished_ts=time.time(),
                finished_at=plugin._web_task_timestamp(),
            )
            await plugin._send_task_notification(
                task_id,
                event,
                plugin._friendly_user_error_message(error, "生图没有完成"),
            )
        finally:
            plugin._release_quota_reservation(task_id)
