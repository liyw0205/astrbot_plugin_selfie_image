"""Persistent web generation task lifecycle mixin."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional

from ..core.utils import (
    load_json_file,
    redact_sensitive_data,
    redact_sensitive_text,
    save_json_file,
)
from ..generation.generation_results import build_task_terminal_state
from .task_views import task_source_label


class WebTaskMixin:
    # Task snapshots are operational history, not the durable generation
    # record archive. Keep a bounded recent window while never pruning active
    # tasks; terminal records remain available through generation records.
    WEB_TASK_KEEP_LIMIT = 50

    _TASK_TERMINAL_STATUSES = {
        "succeeded",
        "partial_success",
        "failed",
        "delivery_failed",
        "cancelled",
        "expired",
    }

    def _normalize_web_image_count(self, value: Any = 1) -> int:
        """Clamp Web image batches to the same configured limit as commands."""
        try:
            requested = int(value or 1)
        except (TypeError, ValueError):
            requested = 1
        try:
            limit = int(getattr(getattr(self, "config", None), "image_max_batch_count", 20) or 20)
        except (TypeError, ValueError):
            limit = 20
        return max(1, min(20, limit, requested))

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
            "generation_stage": "",
            "generation_stage_label": "",
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
                "requested_count": 1,
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
        summary["count"] = self._normalize_web_image_count(payload.get("count"))
        summary["requested_count"] = summary["count"]
        retry_id = str(payload.get("_retry_record_id") or payload.get("retry_record_id") or "").strip()
        if retry_id:
            summary["retry_record_id"] = retry_id[:128]
        return summary

    def _prune_web_tasks_locked(self) -> None:
        keep_limit = max(1, int(getattr(self, "WEB_TASK_KEEP_LIMIT", 50) or 50))
        finished = [
            (float(task.get("updated_ts") or 0), task_id)
            for task_id, task in self._web_tasks.items()
            if task.get("status")
            in {"succeeded", "partial_success", "failed", "delivery_failed", "cancelled", "expired"}
        ]
        finished.sort(key=lambda item: item[0])
        # Active tasks are operational state and must never consume the
        # terminal-history allowance. Keep the newest ``keep_limit`` terminal
        # snapshots while leaving every queued/running task untouched.
        while len(finished) > keep_limit:
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

    @staticmethod
    def _task_prompt_summary(task: Mapping[str, Any], *, limit: int = 120) -> str:
        request = task.get("request_data") if isinstance(task.get("request_data"), Mapping) else {}
        result = task.get("result") if isinstance(task.get("result"), Mapping) else {}
        text = str(
            request.get("original_prompt")
            or request.get("prompt")
            or result.get("original_prompt")
            or result.get("prompt")
            or ""
        )
        compact = " ".join(text.split())
        return compact[:limit] + ("..." if len(compact) > limit else "")

    @staticmethod
    def _task_attempt_summaries(task: Mapping[str, Any]) -> list[Dict[str, Any]]:
        result = task.get("result") if isinstance(task.get("result"), Mapping) else {}
        response = result.get("response_data") if isinstance(result.get("response_data"), Mapping) else {}
        raw_attempts = result.get("attempts") or response.get("attempts") or []
        rows: list[Dict[str, Any]] = []
        for index, raw in enumerate(raw_attempts if isinstance(raw_attempts, list) else [], 1):
            if not isinstance(raw, Mapping):
                continue
            error = redact_sensitive_text(str(raw.get("error_user_message") or raw.get("error") or ""))
            rows.append(
                {
                    "attempt": int(raw.get("attempt") or index),
                    "channel": redact_sensitive_text(str(raw.get("channel") or ""))[:120],
                    "model": redact_sensitive_text(str(raw.get("label") or raw.get("model") or ""))[:160],
                    "success": bool(raw.get("success")),
                    "elapsed_seconds": raw.get("elapsed_seconds"),
                    "error_category": str(raw.get("error_category") or "")[:64],
                    "error": error[:240],
                }
            )
        return rows

    @classmethod
    def task_operation_capabilities(cls, task: Mapping[str, Any]) -> Dict[str, bool]:
        """Single state matrix shared by task list/detail and cancel guards."""
        status = str(task.get("status") or "").strip().lower()
        active = status in {"queued", "running"}
        terminal = status in cls._TASK_TERMINAL_STATUSES
        retry_id = str(task.get("retry_record_id") or task.get("record_id") or "").strip()
        return {
            "can_cancel": active and not bool(task.get("cancel_requested")),
            "can_delete": terminal,
            "can_retry": terminal and bool(retry_id or task.get("record_ids")),
            "is_terminal": terminal,
        }

    def _task_list_row(self, task: Mapping[str, Any]) -> Dict[str, Any]:
        """Create the small, poll-safe task shape used by the task center."""
        request = task.get("request_data") if isinstance(task.get("request_data"), Mapping) else {}
        result = task.get("result") if isinstance(task.get("result"), Mapping) else {}
        used_model = str(
            result.get("used_model")
            or task.get("used_model")
            or request.get("model")
            or ""
        ).strip()
        retry_record_id = str(
            request.get("retry_record_id")
            or task.get("retry_record_id")
            or result.get("retry_record_id")
            or ""
        ).strip()
        linked_record_ids = task.get("record_ids")
        if not isinstance(linked_record_ids, list):
            linked_record_ids = result.get("record_ids") if isinstance(result.get("record_ids"), list) else []
        linked_record_ids = [str(item).strip() for item in linked_record_ids if str(item).strip()][:200]
        prompt_text = redact_sensitive_text(
            str(
                request.get("original_prompt")
                or request.get("prompt")
                or result.get("original_prompt")
                or result.get("prompt")
                or ""
            ).strip()
        )[:50000]
        public_keys = (
            "task_id",
            "status",
            "success",
            "created_ts",
            "updated_ts",
            "created_at",
            "updated_at",
            "started_ts",
            "started_at",
            "finished_ts",
            "finished_at",
            "cancel_requested",
            "requested_count",
            "completed_count",
            "succeeded_count",
            "failed_count",
            "progress_percent",
            "current_index",
            "queue_waiting",
            "queue_position",
            "queue_wait_seconds",
            "running_seconds",
            "generation_started_ts",
            "active_slots",
            "max_concurrent_tasks",
            "timeout_warning",
            "timeout_warning_code",
            "generation_stage",
            "generation_stage_label",
            "generation_success",
            "delivery_success",
            "delivery_unknown",
            "delivery_failed",
            "delivery_error",
        )
        row = {key: task.get(key) for key in public_keys if key in task}
        row.update(
            {
                "media_type": self._task_media_type(task),
                "source": str(task.get("source") or ""),
                "source_label": task_source_label(task),
                "prompt_summary": self._task_prompt_summary(task),
                "prompt": prompt_text,
                "used_model": redact_sensitive_text(used_model)[:180],
                "error": redact_sensitive_text(str(task.get("error") or result.get("error") or ""))[:320],
                # ``record_id`` is kept for the source record of retry tasks;
                # ``record_ids`` contains records produced by this task.
                "record_id": retry_record_id[:128],
                "retry_record_id": retry_record_id[:128],
                "record_ids": linked_record_ids,
                **self.task_operation_capabilities({**task, "retry_record_id": retry_record_id, "record_ids": linked_record_ids}),
            }
        )
        return redact_sensitive_data(row)

    def get_web_task_detail(self, task_id: str) -> Dict[str, Any]:
        """Return a redacted detail view without request bodies or local file paths."""
        task = self.get_web_image_task(task_id)
        request = task.get("request_data") if isinstance(task.get("request_data"), Mapping) else {}
        result = task.get("result") if isinstance(task.get("result"), Mapping) else {}
        detail = self._task_list_row(task)
        original_prompt = str(
            request.get("original_prompt")
            or request.get("prompt")
            or result.get("original_prompt")
            or ""
        ).strip()
        final_prompt = str(
            result.get("final_prompt")
            or result.get("request_prompt")
            or request.get("request_prompt_en")
            or request.get("request_prompt")
            or ""
        ).strip()
        detail.update(
            {
                "original_prompt": redact_sensitive_text(original_prompt)[:50000],
                "final_prompt": redact_sensitive_text(final_prompt)[:50000],
                "parameters": {
                    key: redact_sensitive_data(request.get(key))
                    for key in (
                        "kind",
                        "mode",
                        "session_id",
                        "studio_template",
                        "channel",
                        "model",
                        "aspect_ratio",
                        "resolution",
                        "duration",
                        "requested_duration",
                        "timeout_seconds",
                        "size",
                        "count",
                        "requested_count",
                        "prompt_enhance",
                        "reference_image_count",
                        "raw_reference_image_count",
                        "used_slots",
                        "source_asset_ids",
                        "studio_source_asset_ids",
                        "composition",
                        "cos",
                        "cos_pose",
                        "cos_scene",
                        "cos_view",
                        "request_prompt_en",
                    )
                    if request.get(key) not in (None, "")
                },
                "record_ids": list(detail.get("record_ids") or []),
                "attempts": self._task_attempt_summaries(task),
                "result_summary": {
                    "file_count": len(
                        result.get("files")
                        or result.get("image_paths")
                        or result.get("generated_image_paths")
                        or result.get("generated_video_paths")
                        or []
                    ),
                    "used_model": redact_sensitive_text(str(result.get("used_model") or ""))[:180],
                    "elapsed_seconds": result.get("elapsed_seconds"),
                    "generation_success": result.get("generation_success"),
                    "delivery_success": result.get("delivery_success"),
                    "delivery_unknown": result.get("delivery_unknown"),
                },
            }
        )
        return redact_sensitive_data(detail)

    def list_web_tasks(
        self,
        *,
        include_finished: bool = False,
        limit: int = 50,
        offset: int = 0,
        media_type: str = "",
        source: str = "",
        status: str = "",
        model: str = "",
        keyword: str = "",
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Return an admin-facing task snapshot without request bodies or secrets.

        Filters are applied to the persisted task metadata before ``limit`` so
        the dashboard can browse recent history without downloading every task
        row.  ``source``, ``model`` and ``keyword`` are case-insensitive
        substring filters; ``status`` accepts a comma-separated set of
        internal status values.
        """
        try:
            page_offset = int(offset or 0)
        except (TypeError, ValueError):
            raise ValueError("offset 必须是整数") from None
        if page_offset < 0:
            raise ValueError("offset 不能小于 0")
        try:
            page_limit = max(1, min(200, int(limit or 50)))
        except (TypeError, ValueError):
            raise ValueError("limit 必须是整数") from None
        wanted = str(media_type or "").strip().lower()
        if wanted not in {"", "image", "video"}:
            raise ValueError("media_type 必须是 image 或 video")
        source_query = str(source or "").strip().lower()
        model_query = str(model or "").strip().lower()
        keyword_query = str(keyword or "").strip().lower()
        status_values = {
            item.strip().lower()
            for item in str(status or "").split(",")
            if item and item.strip()
        }
        valid_statuses = {
            "queued",
            "running",
            "succeeded",
            "partial_success",
            "failed",
            "delivery_failed",
            "cancelled",
            "expired",
        }
        unknown_statuses = status_values - valid_statuses
        if unknown_statuses:
            raise ValueError("status 包含不支持的任务状态")
        try:
            start_value = float(start_ts) if start_ts is not None else None
        except (TypeError, ValueError):
            raise ValueError("start_ts 必须是时间戳") from None
        try:
            end_value = float(end_ts) if end_ts is not None else None
        except (TypeError, ValueError):
            raise ValueError("end_ts 必须是时间戳") from None
        if start_value is not None and end_value is not None and start_value > end_value:
            raise ValueError("开始时间不能晚于结束时间")
        with self._web_task_lock:
            raw_tasks = sorted(
                (copy.deepcopy(item) for item in self._web_tasks.values() if isinstance(item, dict)),
                key=lambda item: float(item.get("created_ts") or 0),
                reverse=True,
            )
        active = {"queued", "running"}
        filtered_tasks = []
        for raw in raw_tasks:
            if not include_finished and raw.get("status") not in active:
                continue
            if wanted and self._task_media_type(raw) != wanted:
                continue
            raw_status = str(raw.get("status") or "").strip().lower()
            if status_values and raw_status not in status_values:
                continue
            if source_query:
                source_text = " ".join(
                    (
                        str(raw.get("source") or ""),
                        task_source_label(raw),
                    )
                ).lower()
                if source_query not in source_text:
                    continue
            if model_query:
                request = raw.get("request_data") if isinstance(raw.get("request_data"), Mapping) else {}
                result = raw.get("result") if isinstance(raw.get("result"), Mapping) else {}
                model_text = " ".join(
                    (
                        str(raw.get("used_model") or ""),
                        str(request.get("model") or ""),
                        str(result.get("used_model") or ""),
                    )
                ).lower()
                if model_query not in model_text:
                    continue
            if keyword_query:
                request = raw.get("request_data") if isinstance(raw.get("request_data"), Mapping) else {}
                result = raw.get("result") if isinstance(raw.get("result"), Mapping) else {}
                keyword_text = " ".join(
                    (
                        self._task_prompt_summary(raw, limit=10000),
                        str(raw.get("error") or ""),
                        str(request.get("original_prompt") or request.get("prompt") or ""),
                        str(result.get("error") or ""),
                    )
                ).lower()
                if keyword_query not in keyword_text:
                    continue
            try:
                created_ts = float(raw.get("created_ts") or 0)
            except (TypeError, ValueError):
                created_ts = 0.0
            if start_value is not None and created_ts < start_value:
                continue
            if end_value is not None and created_ts > end_value:
                continue
            filtered_tasks.append(raw)

        rows = []
        for raw in filtered_tasks[page_offset : page_offset + page_limit]:
            try:
                row = self.get_web_image_task(str(raw.get("task_id") or ""))
            except Exception:
                row = redact_sensitive_data(raw)
            rows.append(self._task_list_row(row))

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
                "filtered_total": len(filtered_tasks),
                "queued": status_counts.get("queued", 0),
                "running": status_counts.get("running", 0),
                "by_media_type": media_counts,
                "image_active_slots": image_active,
                "image_max_concurrent_tasks": image_max,
                "video_active_slots": video_active,
                "video_max_concurrent_tasks": video_max,
            },
            "offset": page_offset,
            "limit": page_limit,
            "total": len(raw_tasks),
            "filtered_total": len(filtered_tasks),
            "filters": {
                "include_finished": bool(include_finished),
                "media_type": wanted,
                "source": source_query,
                "status": sorted(status_values),
                "model": model_query,
                "keyword": keyword_query,
                "start_ts": start_value,
                "end_ts": end_value,
            },
        }

    @staticmethod
    def _normalize_task_ids(task_ids: Iterable[Any]) -> List[str]:
        if isinstance(task_ids, (str, bytes)):
            values = [task_ids]
        else:
            values = list(task_ids or [])
        return list(dict.fromkeys(str(item or "").strip() for item in values if str(item or "").strip()))

    def delete_web_tasks(self, task_ids: Iterable[Any]) -> Dict[str, Any]:
        """Delete terminal task metadata while leaving generation records intact."""
        ids = self._normalize_task_ids(task_ids)
        if not ids:
            raise ValueError("至少选择一条任务")
        if len(ids) > 200:
            raise ValueError("单次最多删除 200 条任务")
        deleted: List[str] = []
        missing: List[str] = []
        skipped: List[Dict[str, str]] = []
        with self._web_task_lock:
            for task_id in ids:
                task = self._web_tasks.get(task_id)
                if not isinstance(task, dict):
                    missing.append(task_id)
                    continue
                status = str(task.get("status") or "").strip().lower()
                if status not in self._TASK_TERMINAL_STATUSES:
                    skipped.append({"task_id": task_id, "reason": "活动任务不能删除"})
                    continue
                self._web_tasks.pop(task_id, None)
                deleted.append(task_id)
            if deleted:
                self._persist_web_tasks_locked()
        return {
            "deleted": deleted,
            "missing": missing,
            "skipped": skipped,
            "deleted_count": len(deleted),
        }

    def retry_web_tasks(
        self,
        task_ids: Iterable[Any],
        feedback: str = "",
        strategy: str = "full",
    ) -> Dict[str, Any]:
        """Submit retries for records linked to selected terminal tasks.

        Task payloads intentionally omit request bodies, so a retry is only
        possible when the task has at least one retained generation record.
        Each linked record is retried independently and receives the optional
        feedback string.
        """
        ids = self._normalize_task_ids(task_ids)
        if not ids:
            raise ValueError("至少选择一条任务")
        if len(ids) > 200:
            raise ValueError("单次最多重试 200 条任务")
        retry = getattr(self, "start_record_retry_task", None)
        if not callable(retry):
            raise RuntimeError("当前版本不支持任务重试")
        submitted: List[Dict[str, Any]] = []
        missing: List[str] = []
        skipped: List[Dict[str, str]] = []
        errors: List[Dict[str, str]] = []
        seen_records: set[str] = set()
        with self._web_task_lock:
            selected = {task_id: copy.deepcopy(self._web_tasks.get(task_id)) for task_id in ids}
        for task_id in ids:
            task = selected.get(task_id)
            if not isinstance(task, dict):
                missing.append(task_id)
                continue
            status = str(task.get("status") or "").strip().lower()
            if status not in self._TASK_TERMINAL_STATUSES:
                skipped.append({"task_id": task_id, "reason": "活动任务不能重试"})
                continue
            record_ids = task.get("record_ids")
            if not isinstance(record_ids, list):
                record_ids = []
            retry_id = str(task.get("retry_record_id") or task.get("record_id") or "").strip()
            record_ids = self._normalize_task_ids([*record_ids, retry_id])
            record_ids = [record_id for record_id in record_ids if record_id not in seen_records]
            if not record_ids:
                skipped.append({"task_id": task_id, "reason": "没有可重试的生成记录"})
                continue
            for record_id in record_ids:
                seen_records.add(record_id)
                try:
                    try:
                        retry_task = retry(record_id, str(feedback or "").strip()[:2000], strategy)
                    except TypeError:
                        # Keep compatibility with older plugin/test shims.
                        retry_task = retry(record_id, str(feedback or "").strip()[:2000])
                    submitted.append(
                        {
                            "task_id": task_id,
                            "record_id": record_id,
                            "retry_task_id": str((retry_task or {}).get("task_id") or ""),
                        }
                    )
                except Exception as exc:
                    errors.append(
                        {
                            "task_id": task_id,
                            "record_id": record_id,
                            "error": redact_sensitive_text(str(exc))[:320],
                        }
                    )
        return {
            "submitted": submitted,
            "missing": missing,
            "skipped": skipped,
            "errors": errors,
            "submitted_count": len(submitted),
        }

    def export_web_tasks(self, task_ids: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
        """Export redacted task rows for offline troubleshooting."""
        ids = self._normalize_task_ids(task_ids or []) if task_ids is not None else []
        if len(ids) > 200:
            raise ValueError("单次最多导出 200 条任务")
        selected = set(ids)
        with self._web_task_lock:
            raw_tasks = sorted(
                (copy.deepcopy(item) for item in self._web_tasks.values() if isinstance(item, dict)),
                key=lambda item: float(item.get("created_ts") or 0),
                reverse=True,
            )
        rows: List[Dict[str, Any]] = []
        for raw in raw_tasks:
            task_id = str(raw.get("task_id") or "").strip()
            if selected and task_id not in selected:
                continue
            try:
                detail = self.get_web_image_task(task_id)
            except Exception:
                detail = raw
            rows.append(self._task_list_row(detail))
            if len(rows) >= 200:
                break
        return {
            "format": "selfie-image-task-export",
            "version": 1,
            "exported_at": self._web_task_timestamp(),
            "count": len(rows),
            "tasks": rows,
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
        requested_count = (
            self._normalize_web_image_count(payload_copy.get("count"))
            if media_type == "image"
            else 1
        )
        if media_type == "image":
            # Pass the normalized value to both the task summary and the
            # runner so quota reservation, progress, and execution agree.
            payload_copy["count"] = requested_count
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
                "source": (
                    "record-video-retry"
                    if media_type == "video" and (payload_copy.get("_retry_record_id") or payload_copy.get("retry_record_id"))
                    else "record-retry"
                    if payload_copy.get("_retry_record_id") or payload_copy.get("retry_record_id")
                    else "web-video-test"
                    if media_type == "video"
                    else "web-test"
                ),
                "owner_session": "web",
                "cancel_requested": False,
                "request_fingerprint": fingerprint,
                "deduplicated": False,
                **self._task_runtime_defaults(),
                **self._task_progress_defaults(requested_count),
                "generation_stage": "preflight",
                "generation_stage_label": "准备视频任务" if media_type == "video" else "准备图片任务",
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
            generation_stage="preflight",
            generation_stage_label="准备视频任务" if media_type == "video" else "准备图片任务",
            **runtime_fields,
        )
        try:
            if self._task_cancel_requested(task_id):
                raise RuntimeError("任务已取消")
            # Keep the queue ID in-memory only; generation handlers copy it to
            # their record metadata and the compact request summary omits it.
            run_payload = copy.deepcopy(payload)
            run_payload["_task_id"] = task_id
            result = await (
                self.web_test_video(run_payload, task_id=task_id)
                if media_type == "video"
                else self.web_test_image(run_payload)
            )
            result = redact_sensitive_data(result)
            terminal = build_task_terminal_state(
                result,
                requested_count=payload.get("count") or 1,
                cancel_requested=self._task_cancel_requested(task_id),
            )
            result = terminal["result"]
            cancelled_result = terminal["cancelled_result"]
            if cancelled_result:
                ensure_failure = getattr(self, "_ensure_task_failure_record", None)
                if callable(ensure_failure):
                    await ensure_failure(
                        task_id,
                        error="任务已取消",
                        cancelled=True,
                        stage="cancelled",
                        media_type=media_type,
                        source=str(payload.get("source") or ("web-video-test" if media_type == "video" else "web-test")),
                        prompt=str(payload.get("prompt") or ""),
                        request_data=payload,
                    )
                self._set_web_image_task(
                    task_id,
                    status="cancelled",
                    success=False,
                    generation_stage="cancelled",
                    generation_stage_label="已取消",
                    error="任务已取消",
                    result=result,
                    finished_ts=time.time(),
                    finished_at=self._web_task_timestamp(),
                )
                return
            # A completion result wins a racing cancel request; clear the
            # request marker so the terminal snapshot remains self-consistent.
            if self._task_cancel_requested(task_id):
                self._set_web_image_task(task_id, cancel_requested=False)
            wait_commits = getattr(self, "_wait_for_record_commits", None)
            if callable(wait_commits):
                await wait_commits(task_id)
            success = terminal["success"]
            generation_success = terminal["generation_success"]
            delivery_unknown = terminal["delivery_unknown"]
            delivery_failed = terminal["delivery_failed"]
            requested_count = int(result.get("requested_count") or payload.get("count") or 1)
            succeeded_count = int(result.get("succeeded_count") or 0)
            failed_count = int(result.get("failed_count") or 0)
            completed_count = min(requested_count, max(0, succeeded_count + failed_count))
            response_data = result.get("response_data") if isinstance(result.get("response_data"), dict) else {}
            result_error = result.get("error") or response_data.get("error") or ""
            error = "" if success else redact_sensitive_text(str(result_error or "这次没顺好"))
            if not success and not result.get("error"):
                result["error"] = error
            mark_delivery = getattr(self, "_mark_task_records_delivery", None)
            record_paths = (
                result.get("image_paths")
                or result.get("generated_image_paths")
                or result.get("generated_video_paths")
                or []
            )
            # Partial batches still return their successful files to the Web
            # client.  Mark those records as delivered instead of leaving
            # their transport state indefinitely unknown.
            if callable(mark_delivery) and not delivery_unknown and (success or delivery_failed or record_paths):
                await mark_delivery(
                    task_id,
                    delivered=bool(not delivery_failed),
                    error=error if delivery_failed else "",
                    paths=record_paths,
                )
            terminal_status = terminal["terminal_status"]
            partial_success = terminal["partial_success"]
            terminal_stage = terminal["terminal_stage"]
            terminal_stage_label = (
                "部分完成"
                if partial_success
                else "已完成"
                if success or delivery_failed
                else "已失败"
            )
            self._set_web_image_task(
                task_id,
                status=terminal_status,
                success=success,
                generation_stage=terminal_stage,
                generation_stage_label=terminal_stage_label,
                error=error,
                generation_success=generation_success or success,
                delivery_success=(
                    False
                    if delivery_failed
                    else None
                    if delivery_unknown
                    else True
                    if success or record_paths
                    else result.get("delivery_success")
                ),
                delivery_unknown=delivery_unknown,
                delivery_failed=delivery_failed,
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
        except asyncio.CancelledError:
            ensure_failure = getattr(self, "_ensure_task_failure_record", None)
            if callable(ensure_failure):
                await ensure_failure(
                    task_id,
                    error="任务已取消",
                    cancelled=True,
                    stage="cancelled",
                    media_type=media_type,
                    source=str(payload.get("source") or ("web-video-test" if media_type == "video" else "web-test")),
                    prompt=str(payload.get("prompt") or ""),
                    request_data=payload,
                )
            self._set_web_image_task(
                task_id,
                status="cancelled",
                success=False,
                generation_stage="cancelled",
                generation_stage_label="已取消",
                error="任务已取消",
                result={"success": False, "error": "任务已取消", "cancelled": True},
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
            return
        except Exception as exc:
            error = redact_sensitive_text(str(exc))
            cancelled = "取消" in error
            ensure_failure = getattr(self, "_ensure_task_failure_record", None)
            if callable(ensure_failure):
                await ensure_failure(
                    task_id,
                    error=error,
                    cancelled=cancelled,
                    stage="cancelled" if cancelled else "task_exception",
                    media_type=media_type,
                    source=str(payload.get("source") or ("web-video-test" if media_type == "video" else "web-test")),
                    prompt=str(payload.get("prompt") or ""),
                    request_data=payload,
                )
            self._set_web_image_task(
                task_id,
                status="cancelled" if cancelled else "failed",
                success=False,
                generation_stage="cancelled" if cancelled else "failed",
                generation_stage_label="已取消" if cancelled else "已失败",
                error=error,
                completed_count=0,
                progress_percent=0,
                result={"success": False, "error": error},
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
