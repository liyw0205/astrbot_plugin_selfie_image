"""Persistent web generation task lifecycle mixin."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any, Dict, Optional

from ..core.utils import (
    load_json_file,
    redact_sensitive_data,
    redact_sensitive_text,
    save_json_file,
)


class WebTaskMixin:
    def _web_task_timestamp(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    @staticmethod
    def _task_progress_defaults(requested_count: Any = 1) -> Dict[str, Any]:
        """Return stable progress fields for newly persisted generation tasks."""
        try:
            requested = max(1, int(requested_count or 1))
        except (TypeError, ValueError):
            requested = 1
        return {
            "requested_count": requested,
            "completed_count": 0,
            "succeeded_count": 0,
            "failed_count": 0,
            "progress_percent": 0,
            "current_index": 0,
        }

    @staticmethod
    def _task_media_type(task: Mapping[str, Any]) -> str:
        request = task.get("request_data") if isinstance(task.get("request_data"), Mapping) else {}
        value = str(task.get("media_type") or request.get("media_type") or "").strip().lower()
        if value in {"image", "video"}:
            return value
        kind = str(request.get("kind") or "").strip().lower()
        source = str(task.get("source") or "").strip().lower()
        return "video" if kind == "video" or "视频" in kind or "video" in source else "image"

    def _task_runtime_defaults(self) -> Dict[str, Any]:
        return {
            "queue_waiting": False,
            "queue_position": 0,
            "queue_wait_started_ts": None,
            "queue_wait_seconds": 0.0,
            "generation_started_ts": None,
            "active_slots": 0,
            "max_concurrent_tasks": 0,
            "timeout_warning": "",
        }

    def _load_web_tasks(self) -> Dict[str, Dict[str, Any]]:
        data = load_json_file(self.tasks_path)
        if not isinstance(data, dict):
            return {}
        raw_tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
        tasks: Dict[str, Dict[str, Any]] = {}
        expired_on_start = False
        for task_id, raw in raw_tasks.items():
            if not isinstance(raw, dict):
                continue
            task = copy.deepcopy(raw)
            task["task_id"] = str(task.get("task_id") or task_id)
            for key, value in self._task_runtime_defaults().items():
                task.setdefault(key, value)
            if task.get("status") in {"queued", "running"}:
                task["status"] = "expired"
                task["success"] = False
                task["error"] = "插件重启后未恢复该任务，请重新提交"
                task["finished_ts"] = time.time()
                task["finished_at"] = self._web_task_timestamp()
                expired_on_start = True
            tasks[task["task_id"]] = task
        if expired_on_start:
            save_json_file(self.tasks_path, {"tasks": tasks})
        return tasks

    def _persist_web_tasks_locked(self) -> None:
        path = str(getattr(self, "tasks_path", "") or "").strip()
        if path:
            save_json_file(path, {"tasks": self._web_tasks})

    def _request_fingerprint(
        self, payload: Mapping[str, Any], owner_session: str = ""
    ) -> str:
        """Build a short-lived dedupe key without persisting request contents."""
        image_values = list(payload.get("images") or [])
        if payload.get("image"):
            image_values.append(payload.get("image"))
        image_hashes = [
            hashlib.sha256(str(value or "").encode("utf-8", "ignore")).hexdigest()[:24]
            for value in image_values
        ]
        fields = {
            "owner_session": str(owner_session or ""),
            "media_type": str(payload.get("media_type") or "image").strip().lower(),
            "prompt": str(payload.get("prompt") or payload.get("original_prompt") or "").strip(),
            "channel": str(payload.get("channel") or "").strip(),
            "model": str(payload.get("model") or "").strip(),
            "aspect_ratio": str(payload.get("aspect_ratio") or "").strip(),
            "resolution": str(payload.get("resolution") or "").strip(),
            "duration": str(payload.get("duration") or "").strip(),
            "count": str(payload.get("count") or 1).strip(),
            "prompt_enhance": str(payload.get("prompt_enhance") or "").strip().lower(),
            "image_hashes": image_hashes,
        }
        encoded = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8", "ignore")).hexdigest()[:24]

    def _find_recent_duplicate_task_locked(
        self, fingerprint: str, *, now: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        if not fingerprint:
            return None
        current = float(now or time.time())
        for task in self._web_tasks.values():
            if not isinstance(task, dict) or task.get("request_fingerprint") != fingerprint:
                continue
            if task.get("status") not in {"queued", "running"}:
                continue
            if current - float(task.get("created_ts") or 0) <= 120:
                return copy.deepcopy(task)
        return None

    def _summarize_web_test_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_images = list(payload.get("images") or [])
        if payload.get("image"):
            raw_images.append(payload.get("image"))
        media_type = str(payload.get("media_type") or "image").strip().lower()
        if media_type == "video":
            summary = {
                "media_type": "video",
                "original_prompt": str(payload.get("prompt") or "").strip()
                or "一段自然流畅的短视频",
                "channel": str(payload.get("channel") or "").strip(),
                "model": str(payload.get("model") or "").strip(),
                "aspect_ratio": str(payload.get("aspect_ratio") or "16:9"),
                "duration": int(
                    payload.get("duration") or self.config.video_default_duration or 5
                ),
                "raw_reference_image_count": len(raw_images),
            }
            retry_id = str(payload.get("_retry_record_id") or payload.get("retry_record_id") or "").strip()
            if retry_id:
                summary["retry_record_id"] = retry_id[:128]
            return summary
        prompt_enhance_raw = payload.get("prompt_enhance", True)
        prompt_enhance = not (
            prompt_enhance_raw is False
            or str(prompt_enhance_raw).strip().lower()
            in {"false", "0", "no", "off", "关闭", "否"}
        )
        summary = {
            "original_prompt": str(payload.get("prompt") or "").strip()
            or "看着镜头自然自拍",
            "channel": str(payload.get("channel") or "").strip(),
            "model": str(payload.get("model") or "").strip(),
            "aspect_ratio": str(
                payload.get("aspect_ratio")
                or self.config.image_default_aspect_ratio
                or "9:16"
            ),
            "resolution": str(
                payload.get("resolution") or self.config.image_default_resolution or "1K"
            ),
            "prompt_enhance": prompt_enhance,
            "use_selfie_reference": bool(payload.get("use_selfie_reference")),
            "raw_reference_image_count": len(raw_images),
        }
        try:
            summary["count"] = max(1, int(payload.get("count") or 1))
        except (TypeError, ValueError):
            summary["count"] = 1
        retry_id = str(payload.get("_retry_record_id") or payload.get("retry_record_id") or "").strip()
        if retry_id:
            summary["retry_record_id"] = retry_id[:128]
        return summary

    def _prune_web_tasks_locked(self) -> None:
        if len(self._web_tasks) <= 50:
            return
        finished = [
            (float(task.get("updated_ts") or 0), task_id)
            for task_id, task in self._web_tasks.items()
            if task.get("status")
            in {"succeeded", "partial_success", "failed", "cancelled", "expired"}
        ]
        finished.sort(key=lambda item: item[0])
        while len(self._web_tasks) > 50 and finished:
            _, task_id = finished.pop(0)
            self._web_tasks.pop(task_id, None)

    def _set_web_image_task(self, task_id: str, **fields: Any) -> None:
        with self._web_task_lock:
            task = self._web_tasks.get(task_id)
            if not task:
                return
            now = time.time()
            if "current_index" in fields:
                try:
                    fields["current_index"] = max(
                        int(task.get("current_index") or 0), int(fields["current_index"] or 0)
                    )
                except (TypeError, ValueError):
                    pass
            task.update(fields)
            task["updated_ts"] = now
            task["updated_at"] = self._web_task_timestamp()
            self._prune_web_tasks_locked()
            self._persist_web_tasks_locked()

    def get_web_image_task(self, task_id: str) -> Dict[str, Any]:
        with self._web_task_lock:
            task = self._web_tasks.get(str(task_id or "").strip())
            if not task:
                raise ValueError("任务不存在或已清理")
            data = copy.deepcopy(task)
        now = time.time()
        if data.get("status") in {"queued", "running"}:
            started = float(data.get("started_ts") or data.get("created_ts") or now)
            data["running_seconds"] = round(max(0.0, now - started), 2)
            generation_started = data.get("generation_started_ts")
            if generation_started:
                try:
                    data["queue_wait_seconds"] = round(
                        max(0.0, float(generation_started) - float(data.get("created_ts") or generation_started)),
                        2,
                    )
                except (TypeError, ValueError):
                    pass
            elif data.get("queue_waiting"):
                try:
                    data["queue_wait_seconds"] = round(
                        max(0.0, now - float(data.get("queue_wait_started_ts") or data.get("created_ts") or now)),
                        2,
                    )
                except (TypeError, ValueError):
                    pass
            request = data.get("request_data") if isinstance(data.get("request_data"), Mapping) else {}
            media_type = self._task_media_type(data)
            config = getattr(self, "config", None)
            timeout = (
                getattr(config, "video_global_timeout", 300)
                if media_type == "video"
                else getattr(config, "image_global_timeout", 180)
            )
            try:
                timeout = max(10, int(timeout or 180))
            except (TypeError, ValueError):
                timeout = 180
            queue_wait = float(data.get("queue_wait_seconds") or 0)
            running = float(data.get("running_seconds") or 0)
            warning = ""
            warning_code = ""
            if data.get("queue_waiting") and queue_wait >= min(30, max(10, timeout * 0.2)):
                warning = "排队时间较长，正在等待并发槽位"
                warning_code = "queue_wait"
            elif data.get("generation_started_ts") and running >= timeout * 0.8:
                warning = f"已接近全局超时（{timeout}s）"
                warning_code = "timeout_near"
            data["timeout_warning"] = warning
            data["timeout_warning_code"] = warning_code
        return redact_sensitive_data(data)

    def list_web_tasks(
        self,
        *,
        include_finished: bool = False,
        limit: int = 50,
        media_type: str = "",
    ) -> Dict[str, Any]:
        """Return an admin-facing task snapshot without request bodies or secrets."""
        wanted = str(media_type or "").strip().lower()
        if wanted not in {"", "image", "video"}:
            raise ValueError("media_type 必须是 image 或 video")
        with self._web_task_lock:
            raw_tasks = sorted(
                (copy.deepcopy(item) for item in self._web_tasks.values() if isinstance(item, dict)),
                key=lambda item: float(item.get("created_ts") or 0),
                reverse=True,
            )
        active = {"queued", "running"}
        rows = []
        for raw in raw_tasks:
            if not include_finished and raw.get("status") not in active:
                continue
            if wanted and self._task_media_type(raw) != wanted:
                continue
            try:
                row = self.get_web_image_task(str(raw.get("task_id") or ""))
            except Exception:
                row = redact_sensitive_data(raw)
            # The queue view only needs lifecycle telemetry. Keep prompts,
            # result payloads, fingerprints, and session ownership in the
            # task-detail endpoint instead of repeating them in every poll.
            row["media_type"] = self._task_media_type(row)
            for private_key in ("request_data", "result", "request_fingerprint", "owner_session"):
                row.pop(private_key, None)
            rows.append(row)
            if len(rows) >= max(1, min(200, int(limit or 50))):
                break

        all_active = [item for item in raw_tasks if item.get("status") in active]
        status_counts: Dict[str, int] = {}
        media_counts: Dict[str, Dict[str, int]] = {}
        for item in all_active:
            status = str(item.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            media = self._task_media_type(item)
            bucket = media_counts.setdefault(media, {"queued": 0, "running": 0})
            if status in bucket:
                bucket[status] += 1

        image_gate = getattr(self, "_image_batch_gate", None)
        video_gate = getattr(self, "_video_semaphore", None)
        image_max = max(1, int(getattr(getattr(self, "config", None), "image_max_concurrent_tasks", 1) or 1))
        video_max = max(1, int(getattr(getattr(self, "config", None), "video_max_concurrent_tasks", 1) or 1))
        image_value = getattr(image_gate, "_value", image_max) if image_gate is not None else image_max
        video_value = getattr(video_gate, "_value", video_max) if video_gate is not None else video_max
        image_active = max(0, min(image_max, image_max - int(image_value)))
        video_active = max(0, min(video_max, video_max - int(video_value)))
        return {
            "tasks": rows,
            "summary": {
                "total_active": len(all_active),
                "queued": status_counts.get("queued", 0),
                "running": status_counts.get("running", 0),
                "by_media_type": media_counts,
                "image_active_slots": image_active,
                "image_max_concurrent_tasks": image_max,
                "video_active_slots": video_active,
                "video_max_concurrent_tasks": video_max,
            },
        }

    def start_web_image_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise RuntimeError("请求体必须是 JSON 对象")
        loop = getattr(self, "loop", None)
        if loop is None or not loop.is_running():
            raise RuntimeError("AstrBot 事件循环未就绪，无法启动后台生图任务")
        payload_copy = copy.deepcopy(payload)
        media_type = str(payload_copy.get("media_type") or "image").strip().lower()
        if media_type not in {"image", "video"}:
            raise RuntimeError("media_type 必须是 image 或 video")
        self._validate_web_test_selection(payload_copy)
        force_regenerate = bool(
            payload_copy.get("force_regenerate") or payload_copy.get("force")
        )
        fingerprint = self._request_fingerprint(payload_copy, "web")
        with self._web_task_lock:
            if not force_regenerate:
                duplicate = self._find_recent_duplicate_task_locked(fingerprint)
                if duplicate:
                    duplicate["deduplicated"] = True
                    return redact_sensitive_data(duplicate)
            self._web_task_seq += 1
            task_id = f"web-{int(time.time() * 1000)}-{self._web_task_seq}"
            now = time.time()
            requested_count = 1
            if media_type == "image":
                try:
                    requested_count = max(1, int(payload_copy.get("count") or 1))
                except (TypeError, ValueError):
                    requested_count = 1
            self._web_tasks[task_id] = {
                "task_id": task_id,
                "status": "queued",
                "success": None,
                "error": "",
                "created_ts": now,
                "updated_ts": now,
                "created_at": self._web_task_timestamp(),
                "updated_at": self._web_task_timestamp(),
                "request_data": self._summarize_web_test_payload(payload_copy),
                "result": None,
                "source": "web-video-test" if media_type == "video" else "web-test",
                "owner_session": "web",
                "cancel_requested": False,
                "request_fingerprint": fingerprint,
                "deduplicated": False,
                **self._task_runtime_defaults(),
                **self._task_progress_defaults(requested_count),
            }
            self._prune_web_tasks_locked()
            self._persist_web_tasks_locked()
        runtime_future = asyncio.run_coroutine_threadsafe(
            self._run_web_image_task(task_id, payload_copy), loop
        )
        runtime_tasks = getattr(self, "_runtime_generation_tasks", None)
        if runtime_tasks is None:
            runtime_tasks = {}
            self._runtime_generation_tasks = runtime_tasks
        runtime_tasks[task_id] = runtime_future
        runtime_future.add_done_callback(
            lambda _task, tid=task_id: getattr(self, "_runtime_generation_tasks", {}).pop(tid, None)
        )
        return self.get_web_image_task(task_id)

    async def _run_web_image_task(self, task_id: str, payload: Dict[str, Any]) -> None:
        media_type = str(payload.get("media_type") or "image").strip().lower()
        generation_started = time.time()
        runtime_fields = {
            "queue_waiting": False,
            "queue_position": 0,
        }
        if media_type != "video":
            runtime_fields["generation_started_ts"] = generation_started
        self._set_web_image_task(
            task_id,
            status="running",
            started_ts=time.time(),
            started_at=self._web_task_timestamp(),
            **runtime_fields,
        )
        try:
            if self._task_cancel_requested(task_id):
                raise RuntimeError("任务已取消")
            result = await (
                self.web_test_video(payload, task_id=task_id)
                if media_type == "video"
                else self.web_test_image(payload)
            )
            result = self._normalize_generation_result(result, payload.get("count") or 1)
            result = redact_sensitive_data(result)
            if self._task_cancel_requested(task_id):
                self._set_web_image_task(
                    task_id,
                    status="cancelled",
                    success=False,
                    error="任务已取消",
                    result={"success": False, "error": "任务已取消"},
                    finished_ts=time.time(),
                    finished_at=self._web_task_timestamp(),
                )
                return
            success = bool(result.get("success"))
            requested_count = int(result.get("requested_count") or payload.get("count") or 1)
            succeeded_count = int(result.get("succeeded_count") or 0)
            failed_count = int(result.get("failed_count") or 0)
            completed_count = min(requested_count, max(0, succeeded_count + failed_count))
            response_data = result.get("response_data") if isinstance(result.get("response_data"), dict) else {}
            result_error = result.get("error") or response_data.get("error") or ""
            error = "" if success else redact_sensitive_text(str(result_error or "这次没顺好"))
            if not success and not result.get("error"):
                result["error"] = error
            self._set_web_image_task(
                task_id,
                status=str(result.get("status") or ("succeeded" if success else "failed")),
                success=success,
                error=error,
                requested_count=result.get("requested_count", 1),
                completed_count=completed_count,
                succeeded_count=succeeded_count,
                failed_count=failed_count,
                progress_percent=int(round(completed_count * 100 / max(1, requested_count))),
                current_index=completed_count,
                result=result,
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
        except Exception as exc:
            error = redact_sensitive_text(str(exc))
            cancelled = "取消" in error
            self._set_web_image_task(
                task_id,
                status="cancelled" if cancelled else "failed",
                success=False,
                error=error,
                completed_count=0,
                progress_percent=0,
                result={"success": False, "error": error},
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
