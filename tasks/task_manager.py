"""Persistent web generation task lifecycle mixin."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import shutil
import stat
import threading
import time
from contextlib import suppress
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional
from astrbot.api import logger

from ..core.async_compat import mark_task_cancelling
from ..core.utils import (
    collect_record_cache_paths,
    new_trace_id,
    redact_sensitive_data,
    redact_sensitive_text,
    save_json_file,
    save_json_file_compact,
)
from ..generation.generation_results import build_task_terminal_state
from .task_views import task_media_type, task_source_label


class WebTaskMixin:
    # Task snapshots are operational history, not the durable generation
    # record archive. Keep a bounded recent window while never pruning active
    # tasks; terminal records remain available through generation records.
    WEB_TASK_KEEP_LIMIT = 50
    WEB_TASK_SNAPSHOT_DELAY = 0.2
    WEB_TASK_SNAPSHOT_MAX_BYTES = 10 * 1024 * 1024
    TASK_SNAPSHOT_DURATION_SAMPLE_LIMIT = 128

    # These values are useful while a task is being polled, but are not
    # needed to recover a task after restart.  Keeping them in memory avoids
    # rewriting the JSON snapshot for every scheduler tick.
    _TASK_VOLATILE_FIELDS = frozenset(
        {
            "queue_position",
            "queue_wait_seconds",
            "running_seconds",
            "active_slots",
            "max_concurrent_tasks",
            "timeout_warning",
            "timeout_warning_code",
        }
    )
    TASK_SNAPSHOT_METADATA_VERSION = 1

    _TASK_TERMINAL_STATUSES = {
        "succeeded",
        "partial_success",
        "failed",
        "delivery_failed",
        "cancelled",
        "expired",
    }

    _TASK_NOTIFICATION_STATUSES = {
        "pending",
        "sent",
        "failed",
        "unknown",
        "not_sent",
    }

    def _normalize_web_image_count(self, value: Any = 1) -> int:
        """Clamp Web image batches to the same configured limit as commands."""
        try:
            requested = int(value or 1)
        except (TypeError, ValueError):
            requested = 1
        try:
            limit = int(getattr(getattr(self, "config", None), "image_max_batch_count", 100) or 100)
        except (TypeError, ValueError):
            limit = 100
        return max(1, min(100, limit, requested))

    def _web_task_timestamp(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    def _snapshot_load_failure(
        self,
        reason: str,
        *,
        blocked: bool,
        recovery_path: str = "",
    ) -> None:
        """Keep task snapshot read failures visible without retaining raw input."""
        self._web_task_snapshot_read_failed = True
        self._web_task_snapshot_load_error = redact_sensitive_text(str(reason or "任务快照读取失败"))[:320]
        self._web_task_snapshot_load_blocked = bool(blocked)
        self._web_task_snapshot_recovery_file = os.path.basename(str(recovery_path or ""))[:160]

    @staticmethod
    def _quarantine_task_snapshot(path: str) -> str:
        """Copy malformed JSON aside while keeping the original for manual recovery."""
        source = str(path or "").strip()
        if not source:
            return ""
        for _ in range(3):
            target = f"{source}.corrupt-{time.time_ns()}"
            try:
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as output, open(source, "rb") as incoming:
                    shutil.copyfileobj(incoming, output)
                    output.flush()
                    os.fsync(output.fileno())
                return target
            except FileNotFoundError:
                with suppress(OSError):
                    os.unlink(target)
                return ""
            except FileExistsError:
                continue
            except OSError:
                with suppress(OSError):
                    os.unlink(target)
                return ""
        return ""

    def _read_web_task_snapshot(self) -> Dict[str, Any]:
        """Read a task snapshot while preserving corrupt or unsafe source files."""
        path = str(getattr(self, "tasks_path", "") or "").strip()
        self._web_task_snapshot_load_error = ""
        self._web_task_snapshot_load_blocked = False
        self._web_task_snapshot_recovery_file = ""
        self._web_task_snapshot_file_present = False
        self._web_task_snapshot_read_failed = False
        if not path:
            return {}
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return {}
        except OSError:
            self._snapshot_load_failure("任务快照读取失败，拒绝覆盖原文件", blocked=True)
            return {}
        self._web_task_snapshot_file_present = True
        self._web_task_snapshot_size_bytes = int(getattr(info, "st_size", 0) or 0)
        if stat.S_ISLNK(info.st_mode):
            self._snapshot_load_failure("任务快照是符号链接，拒绝读取和覆盖", blocked=True)
            return {}
        if not stat.S_ISREG(info.st_mode):
            self._snapshot_load_failure("任务快照不是普通文件，拒绝读取和覆盖", blocked=True)
            return {}
        try:
            with open(path, "r", encoding="utf-8-sig") as incoming:
                data = json.load(incoming)
        except (json.JSONDecodeError, UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
            recovery_path = self._quarantine_task_snapshot(path)
            if recovery_path:
                self._snapshot_load_failure("任务快照 JSON 损坏，原文件已隔离；请人工恢复", blocked=True, recovery_path=recovery_path)
            else:
                self._snapshot_load_failure("任务快照 JSON 损坏，原文件无法隔离", blocked=True)
            return {}
        except OSError:
            self._snapshot_load_failure("任务快照读取失败，拒绝覆盖原文件", blocked=True)
            return {}
        if not isinstance(data, dict):
            recovery_path = self._quarantine_task_snapshot(path)
            if recovery_path:
                self._snapshot_load_failure("任务快照结构无效，原文件已隔离；请人工恢复", blocked=True, recovery_path=recovery_path)
            else:
                self._snapshot_load_failure("任务快照结构无效，原文件无法隔离", blocked=True)
            return {}
        return data

    def _reject_loaded_task_snapshot(self, reason: str) -> Dict[str, Any]:
        """Quarantine a structurally invalid object before any write can prune it."""
        path = str(getattr(self, "tasks_path", "") or "").strip()
        recovery_path = self._quarantine_task_snapshot(path)
        if recovery_path:
            self._snapshot_load_failure(
                f"{reason}，原文件已隔离；请人工恢复", blocked=True, recovery_path=recovery_path,
            )
        else:
            self._snapshot_load_failure(f"{reason}，原文件无法隔离", blocked=True)
        return {}

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

    _task_media_type = staticmethod(task_media_type)

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
        data = self._read_web_task_snapshot()
        if not isinstance(data, dict):
            return {}
        if getattr(self, "_web_task_snapshot_read_failed", False):
            return {}
        if getattr(self, "_web_task_snapshot_file_present", False) and "tasks" not in data:
            return self._reject_loaded_task_snapshot("任务快照缺少 tasks 字段")
        if "tasks" in data and not isinstance(data.get("tasks"), dict):
            return self._reject_loaded_task_snapshot("任务快照 tasks 字段结构无效")
        if "snapshot" in data and not isinstance(data.get("snapshot"), dict):
            return self._reject_loaded_task_snapshot("任务快照 snapshot 字段结构无效")
        snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else {}
        # Snapshot metadata was added after the original ``{"tasks": ...}``
        # format.  All fields are optional so old files remain readable.
        try:
            self._web_task_snapshot_version = max(0, int(snapshot.get("snapshot_version") or 0))
        except (TypeError, ValueError):
            self._web_task_snapshot_version = 0
        try:
            self._web_task_snapshot_write_count = max(0, int(snapshot.get("write_count") or 0))
        except (TypeError, ValueError):
            self._web_task_snapshot_write_count = 0
        try:
            self._web_task_snapshot_failure_count = max(0, int(snapshot.get("failure_count") or 0))
        except (TypeError, ValueError):
            self._web_task_snapshot_failure_count = 0
        for field in (
            "write_bytes_total",
            "scheduled_flush_count",
            "coalesced_update_count",
            "timer_flush_count",
            "forced_flush_count",
            "skipped_write_count",
        ):
            try:
                value = max(0, int(snapshot.get(field) or 0))
            except (TypeError, ValueError, OverflowError):
                value = 0
            setattr(self, "_web_task_snapshot_" + field, value)
        self._web_task_snapshot_last_write_duration_ms = self._coerce_snapshot_duration(
            snapshot.get("last_write_duration_ms")
        )
        self._web_task_snapshot_last_write_at = str(snapshot.get("last_write_at") or "")[:64]
        self._web_task_snapshot_last_failure_reason = redact_sensitive_text(
            str(snapshot.get("last_failure_reason") or "")
        )[:320]
        self._web_task_snapshot_last_failure_at = str(snapshot.get("last_failure_at") or "")[:64]
        self._web_task_last_failure_reason = redact_sensitive_text(
            str(snapshot.get("recent_task_failure_reason") or "")
        )[:320]
        self._web_task_last_failure_at = str(snapshot.get("recent_task_failure_at") or "")[:64]
        if not self._web_task_snapshot_load_error:
            self._web_task_snapshot_load_error = redact_sensitive_text(
                str(snapshot.get("load_error") or "")
            )[:320]
        self._web_task_snapshot_recovery_file = os.path.basename(
            str(snapshot.get("load_recovery_file") or getattr(self, "_web_task_snapshot_recovery_file", ""))
        )[:160]
        self._web_task_snapshot_load_blocked = bool(
            getattr(self, "_web_task_snapshot_load_blocked", False)
        )
        try:
            self._web_task_snapshot_size_bytes = os.path.getsize(self.tasks_path)
        except OSError:
            self._web_task_snapshot_size_bytes = 0
        for field in ("evicted_task_count", "compacted_task_count"):
            try:
                value = max(0, int(snapshot.get(field) or 0))
            except (TypeError, ValueError, OverflowError):
                value = 0
            setattr(self, "_web_task_snapshot_" + field, value)
        self._web_task_snapshot_timer = None
        raw_tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
        tasks: Dict[str, Dict[str, Any]] = {}
        lifecycle_events = []
        changed_on_start = False
        for task_id, raw in raw_tasks.items():
            if not isinstance(raw, dict):
                return self._reject_loaded_task_snapshot("任务快照任务条目结构无效")
            task = copy.deepcopy(raw)
            task["task_id"] = str(task.get("task_id") or task_id)
            for key, value in self._task_runtime_defaults().items():
                task.setdefault(key, value)
            try:
                quota_reserved = max(0, int(task.get("quota_reserved") or 0))
            except (TypeError, ValueError):
                quota_reserved = 0
                task["quota_reserved"] = 0
                task["quota_released"] = True
                task["quota_released_ts"] = time.time()
                changed_on_start = True
            if task.get("status") in {"queued", "running"}:
                previous_status = str(task.get("status") or "").strip().lower()
                task["status"] = "expired"
                task["success"] = False
                task["error"] = "插件重启后未恢复该任务，请重新提交"
                task["finished_ts"] = time.time()
                task["finished_at"] = self._web_task_timestamp()
                # A quota reservation lives only in the running process.  A
                # restart cannot resume the worker, so release the persisted
                # marker together with the expired task snapshot.
                if quota_reserved > 0:
                    task["quota_reserved"] = 0
                task["quota_released"] = True
                task["quota_released_ts"] = task["finished_ts"]
                changed_on_start = True
                lifecycle_events.append((task["task_id"], task.get("trace_id") or "", self._task_media_type(task), previous_status, "expired"))
            elif quota_reserved > 0:
                # Older versions could leave a reservation marker on a
                # terminal task after a process crash.  It must not block a
                # user's next request after restart.
                task["quota_reserved"] = 0
                task["quota_released"] = True
                task["quota_released_ts"] = time.time()
                changed_on_start = True
            tasks[task["task_id"]] = task
        if changed_on_start:
            self._persist_web_tasks_locked(tasks=tasks)
        for event in lifecycle_events:
            self._log_task_lifecycle_event(*event)
        return tasks

    @staticmethod
    def _coerce_snapshot_duration(value: Any) -> float:
        try:
            return round(max(0.0, float(value or 0.0)), 3)
        except (TypeError, ValueError):
            return 0.0

    def _increment_snapshot_metric_locked(self, name: str, amount: int = 1) -> int:
        """Increment a bounded integer counter used by snapshot health."""
        attribute = "_web_task_snapshot_" + str(name or "")
        try:
            current = max(0, int(getattr(self, attribute, 0) or 0))
        except (TypeError, ValueError, OverflowError):
            current = 0
        try:
            increment = max(0, int(amount or 0))
        except (TypeError, ValueError, OverflowError):
            increment = 0
        value = current + increment
        setattr(self, attribute, value)
        return value

    def _mark_snapshot_write_skipped_locked(self) -> None:
        self._increment_snapshot_metric_locked("skipped_write_count")

    def _record_snapshot_duration_locked(self, duration_ms: float) -> None:
        duration = self._coerce_snapshot_duration(duration_ms)
        if not math.isfinite(duration):
            return
        samples = list(getattr(self, "_web_task_snapshot_duration_samples_ms", ()) or ())
        samples.append(duration)
        try:
            limit = max(1, int(self.TASK_SNAPSHOT_DURATION_SAMPLE_LIMIT))
        except (TypeError, ValueError, OverflowError):
            limit = 128
        self._web_task_snapshot_duration_samples_ms = samples[-limit:]

    def _snapshot_duration_health_locked(self) -> Dict[str, Any]:
        samples = []
        for value in list(getattr(self, "_web_task_snapshot_duration_samples_ms", ()) or ()):
            try:
                duration = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(duration) and duration >= 0:
                samples.append(duration)
        if not samples:
            return {"samples": 0, "p50": 0.0, "p95": 0.0, "max": 0.0}
        ordered = sorted(samples)
        p50 = ordered[max(0, math.ceil(len(ordered) * 0.50) - 1)]
        p95 = ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]
        return {
            "samples": len(ordered),
            "p50": round(p50, 3),
            "p95": round(p95, 3),
            "max": round(ordered[-1], 3),
        }

    def _snapshot_health_locked(self) -> Dict[str, Any]:
        """Return bounded operational metadata for the health endpoint."""
        load_error = redact_sensitive_text(
            str(getattr(self, "_web_task_snapshot_load_error", "") or "")
        )[:320]
        write_count = int(getattr(self, "_web_task_snapshot_write_count", 0) or 0)
        failure_count = int(getattr(self, "_web_task_snapshot_failure_count", 0) or 0)
        attempts = write_count + failure_count
        return {
            "status": "error" if load_error or getattr(self, "_web_task_snapshot_load_blocked", False) else "ok",
            "version": int(getattr(self, "_web_task_snapshot_version", 0) or 0),
            "snapshot_version": int(getattr(self, "_web_task_snapshot_version", 0) or 0),
            "metadata_version": self.TASK_SNAPSHOT_METADATA_VERSION,
            "write_count": write_count,
            "failure_count": failure_count,
            "write_failure_rate": round(failure_count / attempts, 6) if attempts else None,
            "write_bytes_total": int(getattr(self, "_web_task_snapshot_write_bytes_total", 0) or 0),
            "scheduled_flush_count": int(getattr(self, "_web_task_snapshot_scheduled_flush_count", 0) or 0),
            "coalesced_update_count": int(getattr(self, "_web_task_snapshot_coalesced_update_count", 0) or 0),
            "timer_flush_count": int(getattr(self, "_web_task_snapshot_timer_flush_count", 0) or 0),
            "forced_flush_count": int(getattr(self, "_web_task_snapshot_forced_flush_count", 0) or 0),
            "skipped_write_count": int(getattr(self, "_web_task_snapshot_skipped_write_count", 0) or 0),
            "last_write_duration_ms": self._coerce_snapshot_duration(
                getattr(self, "_web_task_snapshot_last_write_duration_ms", 0.0)
            ),
            "write_duration_ms": self._snapshot_duration_health_locked(),
            "last_write_at": str(getattr(self, "_web_task_snapshot_last_write_at", "") or "")[:64],
            "last_failure_reason": redact_sensitive_text(
                str(getattr(self, "_web_task_snapshot_last_failure_reason", "") or "")
            )[:320],
            "last_failure_at": str(getattr(self, "_web_task_snapshot_last_failure_at", "") or "")[:64],
            "load_error": load_error,
            "load_recovery_file": os.path.basename(
                str(getattr(self, "_web_task_snapshot_recovery_file", "") or "")
            )[:160],
            "load_blocked": bool(getattr(self, "_web_task_snapshot_load_blocked", False)),
            "recent_task_failure_reason": redact_sensitive_text(
                str(getattr(self, "_web_task_last_failure_reason", "") or "")
            )[:320],
            "recent_task_failure_at": str(getattr(self, "_web_task_last_failure_at", "") or "")[:64],
            "pending": getattr(self, "_web_task_snapshot_timer", None) is not None,
            "volatile_fields": sorted(self._TASK_VOLATILE_FIELDS),
            **self._task_snapshot_budget_health(),
        }

    def _task_snapshot_byte_limit(self) -> int:
        try:
            return max(1, int(self.WEB_TASK_SNAPSHOT_MAX_BYTES))
        except (TypeError, ValueError, OverflowError):
            return 10 * 1024 * 1024

    def _task_snapshot_budget_health(self) -> Dict[str, Any]:
        size_bytes = int(getattr(self, "_web_task_snapshot_size_bytes", 0) or 0)
        max_bytes = self._task_snapshot_byte_limit()
        over_budget = size_bytes > max_bytes
        load_error = redact_sensitive_text(
            str(getattr(self, "_web_task_snapshot_load_error", "") or "")
        )[:320]
        warning = self._task_snapshot_budget_warning() if over_budget else ""
        if load_error:
            warning = f"{warning}；{load_error}" if warning else load_error
        return {
            "size_bytes": size_bytes,
            "max_bytes": max_bytes,
            "over_budget": over_budget,
            "warning": warning,
            "evicted_task_count": int(getattr(self, "_web_task_snapshot_evicted_task_count", 0) or 0),
            "compacted_task_count": int(getattr(self, "_web_task_snapshot_compacted_task_count", 0) or 0),
        }

    @staticmethod
    def _task_snapshot_budget_warning() -> str:
        return "任务快照超过字节预算；已保留任务必要信息，请检查并发量或迁移任务存储"

    def get_task_snapshot_health(self) -> Dict[str, Any]:
        """Expose task snapshot write health without returning task contents."""
        lock = getattr(self, "_web_task_lock", None)
        if lock is None:
            return self._snapshot_health_locked()
        with lock:
            return self._snapshot_health_locked()

    def _persistent_web_tasks_locked(
        self, tasks: Optional[Mapping[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Copy tasks while omitting volatile fields from active snapshots."""
        persisted: Dict[str, Dict[str, Any]] = {}
        for task_id, task in (self._web_tasks if tasks is None else tasks).items():
            if not isinstance(task, dict):
                continue
            row = dict(task)
            if str(row.get("status") or "").strip().lower() in {"queued", "running"}:
                for key in self._TASK_VOLATILE_FIELDS:
                    row.pop(key, None)
            persisted[str(task_id)] = row
        return persisted

    @staticmethod
    def _compact_task_snapshot_diagnostics(task: Dict[str, Any]) -> Dict[str, Any]:
        """Drop provider diagnostics while preserving task decisions and media."""
        def has_media_evidence(value: Any) -> bool:
            if collect_record_cache_paths(value):
                return True

            # The cache collector follows three path fields. Older provider
            # results also use files/images and singular media or URL fields;
            # keep their entire container when its semantics are uncertain.
            def has_other_media_fields(node: Any) -> bool:
                if isinstance(node, dict):
                    for key, child in node.items():
                        if child and key in {
                            "files", "images", "image", "image_path", "image_url", "image_urls",
                            "media_path", "media_paths", "video_path", "video_paths",
                            "thumbnail_path", "thumbnail_paths", "generated_image_sources",
                            "request_image_sources", "path", "paths", "url", "urls",
                        }:
                            return True
                        if has_other_media_fields(child):
                            return True
                elif isinstance(node, (list, tuple)):
                    return any(has_other_media_fields(child) for child in node)
                return False

            return has_other_media_fields(value)

        result = task.get("result")
        if not isinstance(result, dict):
            return task
        reduced = dict(result)
        fields = []
        if "response_data" in reduced:
            response = reduced["response_data"]
            # These values can be used to explain a task's outcome. Provider
            # echoes and detailed attempts are archived in generation records.
            if isinstance(response, dict) and not has_media_evidence(response):
                reduced["response_data"] = {
                    key: value
                    for key, value in response.items()
                    if key in {
                        "error", "success", "status", "generation_success", "delivery_success",
                        "delivery_failed", "delivery_unknown", "cancelled", "trace_id",
                    }
                }
                fields.append("result.response_data")
        if isinstance(reduced.get("attempts"), (list, tuple)) and not has_media_evidence(reduced["attempts"]):
            reduced.pop("attempts")
            fields.append("result.attempts")
        if not fields:
            return task
        row = dict(task)
        row["result"] = reduced
        row["snapshot_compacted_fields"] = fields
        return row

    def _prepare_task_snapshot(
        self, tasks: Dict[str, Dict[str, Any]], metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Apply the disk budget without changing live tasks or dropping active work."""
        def encoded_size(value: Any) -> int:
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

        # Measure each entry once, so evicting a terminal window does not
        # repeatedly serialize every large active task.
        entry_sizes = {key: encoded_size({key: task}) - 2 for key, task in tasks.items()}
        entries_bytes = sum(entry_sizes.values())
        metadata.update({
            "size_bytes": 0,
            "max_bytes": self._task_snapshot_byte_limit(),
            "over_budget": False,
            "warning": "",
            "evicted_task_count": 0,
            "compacted_task_count": 0,
        })

        def measure() -> int:
            # size_bytes contributes its own decimal width to the JSON file.
            # Iterate only the small envelope until that width is stable.
            while True:
                size_bytes = (
                    encoded_size({"tasks": {}, "snapshot": metadata})
                    + entries_bytes + max(0, len(tasks) - 1)
                )
                if metadata["size_bytes"] == size_bytes:
                    return size_bytes
                metadata["size_bytes"] = size_bytes

        max_bytes = metadata["max_bytes"]
        size_bytes = measure()
        terminal_ids = []
        for key, task in tasks.items():
            status = str(task.get("status") or "").strip().lower()
            # Startup first marks abandoned work expired, then reconciles it
            # into generation records. Keep that evidence until reconciliation.
            if status in self._TASK_TERMINAL_STATUSES and (status != "expired" or task.get("restart_reconciled")):
                terminal_ids.append(key)

        def terminal_age(task_id: str) -> float:
            task = tasks[task_id]
            try:
                return float(task.get("updated_ts") or task.get("created_ts") or 0)
            except (TypeError, ValueError, OverflowError):
                return 0.0

        for task_id in sorted(terminal_ids, key=terminal_age):
            if size_bytes <= max_bytes:
                break
            tasks.pop(task_id)
            entries_bytes -= entry_sizes.pop(task_id)
            metadata["evicted_task_count"] += 1
            size_bytes = measure()
        if size_bytes > max_bytes:
            for task_id in sorted(tasks, key=entry_sizes.get, reverse=True):
                row = self._compact_task_snapshot_diagnostics(tasks[task_id])
                new_size = encoded_size({task_id: row}) - 2
                if new_size >= entry_sizes[task_id]:
                    continue
                entries_bytes += new_size - entry_sizes[task_id]
                entry_sizes[task_id] = new_size
                tasks[task_id] = row
                metadata["compacted_task_count"] += 1
                size_bytes = measure()
                if size_bytes <= max_bytes:
                    break
        if size_bytes > max_bytes:
            # A byte budget cannot justify losing ownership, quota, prompts,
            # paths, or entire active tasks. Persist them and surface the breach.
            metadata["over_budget"] = True
            metadata["warning"] = self._task_snapshot_budget_warning()
            measure()
        return {"tasks": tasks, "snapshot": metadata}

    def _persist_web_tasks_locked(
        self, *, tasks: Optional[Mapping[str, Dict[str, Any]]] = None,
    ) -> None:
        timer = getattr(self, "_web_task_snapshot_timer", None)
        if timer is not None:
            timer.cancel()
            self._web_task_snapshot_timer = None
        if getattr(self, "_web_task_snapshot_load_blocked", False):
            raise OSError("任务快照读取失败，拒绝覆盖原文件")
        path = str(getattr(self, "tasks_path", "") or "").strip()
        if not path:
            return
        next_version = int(getattr(self, "_web_task_snapshot_version", 0) or 0) + 1
        current_write_count = int(getattr(self, "_web_task_snapshot_write_count", 0) or 0)
        current_failure_count = int(getattr(self, "_web_task_snapshot_failure_count", 0) or 0)
        current_bytes_total = int(getattr(self, "_web_task_snapshot_write_bytes_total", 0) or 0)
        flush_mode = "timer" if getattr(self, "_web_task_snapshot_flush_mode", "") == "timer" else "forced"
        next_timer_flush_count = int(getattr(self, "_web_task_snapshot_timer_flush_count", 0) or 0)
        next_forced_flush_count = int(getattr(self, "_web_task_snapshot_forced_flush_count", 0) or 0)
        if flush_mode == "timer":
            next_timer_flush_count += 1
        else:
            next_forced_flush_count += 1
        metadata = {
            "metadata_version": self.TASK_SNAPSHOT_METADATA_VERSION,
            "snapshot_version": next_version,
            "write_count": current_write_count + 1,
            "failure_count": current_failure_count,
            "write_bytes_total": current_bytes_total,
            "scheduled_flush_count": int(getattr(self, "_web_task_snapshot_scheduled_flush_count", 0) or 0),
            "coalesced_update_count": int(getattr(self, "_web_task_snapshot_coalesced_update_count", 0) or 0),
            "timer_flush_count": next_timer_flush_count,
            "forced_flush_count": next_forced_flush_count,
            "skipped_write_count": int(getattr(self, "_web_task_snapshot_skipped_write_count", 0) or 0),
            # The current write duration is recorded in memory immediately
            # after save; this value represents the previous successful write
            # in the file and is refreshed on the next lifecycle flush.
            "last_write_duration_ms": self._coerce_snapshot_duration(
                getattr(self, "_web_task_snapshot_last_write_duration_ms", 0.0)
            ),
            "last_write_at": self._web_task_timestamp(),
            "last_failure_reason": redact_sensitive_text(
                str(getattr(self, "_web_task_snapshot_last_failure_reason", "") or "")
            )[:320],
            "last_failure_at": str(getattr(self, "_web_task_snapshot_last_failure_at", "") or "")[:64],
            "load_error": redact_sensitive_text(
                str(getattr(self, "_web_task_snapshot_load_error", "") or "")
            )[:320],
            "load_recovery_file": os.path.basename(
                str(getattr(self, "_web_task_snapshot_recovery_file", "") or "")
            )[:160],
            "recent_task_failure_reason": redact_sensitive_text(
                str(getattr(self, "_web_task_last_failure_reason", "") or "")
            )[:320],
            "recent_task_failure_at": str(getattr(self, "_web_task_last_failure_at", "") or "")[:64],
        }
        started = time.perf_counter()
        try:
            # ``size_bytes`` and the cumulative byte counter are part of the
            # JSON envelope itself. Iterate until both values describe the
            # exact UTF-8 payload that will be atomically replaced.
            encoded_size = -1
            payload = None
            for _ in range(12):
                metadata["write_bytes_total"] = current_bytes_total + max(0, encoded_size)
                payload = self._prepare_task_snapshot(
                    self._persistent_web_tasks_locked(tasks), metadata,
                )
                encoded = json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                actual_size = len(encoded)
                if (
                    actual_size == encoded_size
                    and int(payload["snapshot"].get("size_bytes") or 0) == actual_size
                    and int(metadata.get("write_bytes_total") or 0) == current_bytes_total + actual_size
                ):
                    break
                encoded_size = actual_size
            if payload is None or encoded_size < 0:
                raise RuntimeError("任务快照序列化失败")
            save_json_file_compact(path, payload)
        except Exception as exc:
            self._web_task_snapshot_failure_count = int(
                getattr(self, "_web_task_snapshot_failure_count", 0) or 0
            ) + 1
            self._web_task_snapshot_last_failure_reason = redact_sensitive_text(str(exc))[:320]
            self._web_task_snapshot_last_failure_at = self._web_task_timestamp()
            raise
        duration_ms = round(max(0.0, time.perf_counter() - started) * 1000.0, 3)
        self._web_task_snapshot_version = next_version
        self._web_task_snapshot_write_count = metadata["write_count"]
        self._web_task_snapshot_write_bytes_total = metadata["write_bytes_total"]
        self._web_task_snapshot_timer_flush_count = next_timer_flush_count
        self._web_task_snapshot_forced_flush_count = next_forced_flush_count
        self._web_task_snapshot_last_write_duration_ms = duration_ms
        self._web_task_snapshot_last_write_at = metadata["last_write_at"]
        self._web_task_snapshot_last_write_bytes = encoded_size
        self._record_snapshot_duration_locked(duration_ms)
        was_over_budget = self._task_snapshot_budget_health()["over_budget"]
        for field in ("size_bytes", "evicted_task_count", "compacted_task_count"):
            setattr(self, "_web_task_snapshot_" + field, metadata[field])
        if metadata["over_budget"] and not was_over_budget:
            logger.warning(
                "[SelfieImage] 任务快照超过字节预算: %s > %s；保留全部活动任务",
                metadata["size_bytes"], metadata["max_bytes"],
            )

    def _schedule_web_task_snapshot_locked(self) -> None:
        """Coalesce live progress writes; lifecycle boundaries flush immediately."""
        if not str(getattr(self, "tasks_path", "") or "").strip():
            return
        if getattr(self, "_web_task_snapshot_timer", None) is not None:
            self._increment_snapshot_metric_locked("coalesced_update_count")
            return
        self._increment_snapshot_metric_locked("scheduled_flush_count")
        version = getattr(self, "_web_task_snapshot_version", 0)

        def flush() -> None:
            with self._web_task_lock:
                try:
                    if version != getattr(self, "_web_task_snapshot_version", 0):
                        return
                    self._web_task_snapshot_flush_mode = "timer"
                    self._persist_web_tasks_locked()
                except Exception as exc:
                    logger.error("[SelfieImage] 任务进度快照写入失败: %s", exc)
                finally:
                    self._web_task_snapshot_flush_mode = ""
                    if self._web_task_snapshot_timer is timer:
                        self._web_task_snapshot_timer = None

        timer = threading.Timer(self.WEB_TASK_SNAPSHOT_DELAY, flush)
        timer.daemon = True
        self._web_task_snapshot_timer = timer
        timer.start()

    def _start_runtime_generation(self, task_id: str, coro: Any) -> Any:
        """Keep real worker tasks available until their cancellation cleanup ends."""
        started = False

        async def tracked() -> None:
            nonlocal started
            started = True
            current = asyncio.current_task()
            with self._web_task_lock:
                workers = getattr(self, "_runtime_generation_workers", None)
                if workers is None:
                    workers = {}
                    self._runtime_generation_workers = workers
                workers[task_id] = current
            try:
                if getattr(self, "_terminating", False):
                    coro.close()
                    return
                await coro
            finally:
                with self._web_task_lock:
                    workers.pop(task_id, None)

        with self._web_task_lock:
            if getattr(self, "_terminating", False):
                coro.close()
                raise RuntimeError("插件正在卸载，无法提交新任务")
            loop = getattr(self, "loop", None)
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is not None and (loop is None or current_loop is loop):
                handle = current_loop.create_task(tracked())
            else:
                handle = asyncio.run_coroutine_threadsafe(tracked(), loop)
            runtime_tasks = getattr(self, "_runtime_generation_tasks", None)
            if runtime_tasks is None:
                runtime_tasks = {}
                self._runtime_generation_tasks = runtime_tasks
            runtime_tasks[task_id] = handle

            def remove_done(future: Any) -> None:
                with self._web_task_lock:
                    runtime_tasks.pop(task_id, None)
                    if future.cancelled():
                        worker = getattr(self, "_runtime_generation_workers", {}).get(task_id)
                        if worker is not None:
                            mark_task_cancelling(worker)
                if not started:
                    coro.close()
                if not future.cancelled():
                    exc = future.exception()
                    if exc is not None:
                        logger.error("[SelfieImage] 图片任务 %s 异常退出: %s", task_id, exc)

            handle.add_done_callback(remove_done)
            return handle

    def reconcile_expired_tasks_after_restart(self) -> int:
        """Create one inspectable record for each task abandoned by a restart.

        Queued workers cannot be resumed safely because provider requests and
        outbound delivery receipts are not durable.  The task is therefore
        marked ``expired`` on load; this method links that decision to the
        generation record store exactly once so task history and record
        history do not disagree about what happened.
        """
        records = getattr(self, "_records", None)
        records_lock = getattr(self, "_records_lock", None)
        record_task = getattr(self, "_record_task", None)
        if not isinstance(records, list) or records_lock is None or not callable(record_task):
            return 0
        with records_lock:
            existing_ids = {
                str(row.get("task_id") or "").strip()
                for row in records
                if isinstance(row, Mapping) and str(row.get("task_id") or "").strip()
            }
        with self._web_task_lock:
            candidates = [
                copy.deepcopy(task)
                for task in self._web_tasks.values()
                if isinstance(task, dict)
                and str(task.get("status") or "").strip().lower() == "expired"
                and not bool(task.get("restart_reconciled"))
            ]
        reconciled = 0
        for task in candidates:
            task_id = str(task.get("task_id") or "").strip()
            if not task_id:
                continue
            if task_id not in existing_ids:
                request = task.get("request_data") if isinstance(task.get("request_data"), Mapping) else {}
                request = dict(request)
                for key in ("images", "image", "_task_id", "api_key", "api_keys"):
                    request.pop(key, None)
                media_type = self._task_media_type(task)
                source = str(task.get("source") or "restart-recovery").strip()
                prompt = str(
                    request.get("original_prompt")
                    or request.get("prompt")
                    or request.get("action")
                    or ""
                ).strip()
                try:
                    reference_images = max(0, int(request.get("raw_reference_image_count") or 0))
                except (TypeError, ValueError):
                    reference_images = 0
                error = str(task.get("error") or "插件重启后未恢复该任务，请重新提交")
                record = {
                    "source": source,
                    "source_label": task_source_label(task),
                    "media_type": media_type,
                    "success": False,
                    "generation_success": False,
                    "delivery_success": None,
                    "status": "expired",
                    "cancelled": False,
                    "error": error,
                    "prompt": prompt,
                    "original_prompt": prompt,
                    "request_prompt": prompt,
                    "final_prompt": prompt,
                    "used_model": str(request.get("model") or ""),
                    "elapsed_seconds": 0,
                    "reference_images": reference_images,
                    "request_data": redact_sensitive_data({**request, "stage": "restart_recovery", "task_id": task_id}),
                    "response_data": redact_sensitive_data(
                        {
                            "success": False,
                            "status": "expired",
                            "stage": "restart_recovery",
                            "error": error,
                            "notification_status": "not_sent",
                        }
                    ),
                    "request_image_paths": [],
                    "generated_image_paths": [],
                    "attempts": [],
                    "retry_count": 0,
                    "retry_exhausted": False,
                    "task_id": task_id,
                }
                try:
                    record_task(record)
                    existing_ids.add(task_id)
                except Exception as exc:
                    logger.warning("[SelfieImage] 重启任务 %s 的过期记录落库失败: %s", task_id, exc)
                    continue
            self._set_web_image_task(
                task_id,
                restart_reconciled=True,
                user_notification_status="not_sent",
                user_notification_reason="插件重启时无法发送历史任务通知",
            )
            reconciled += 1
        return reconciled

    def _request_fingerprint(
        self,
        payload: Mapping[str, Any],
        owner_session: str = "",
        reference_images: Optional[Iterable[Any]] = None,
    ) -> str:
        """Build a short-lived dedupe key without persisting request contents."""

        def stable_value(value: Any) -> Any:
            if isinstance(value, Mapping):
                return {
                    str(key): stable_value(value[key])
                    for key in sorted(value, key=lambda item: str(item))
                }
            if isinstance(value, (list, tuple)):
                return [stable_value(item) for item in value]
            if isinstance(value, set):
                return sorted((stable_value(item) for item in value), key=str)
            if value is None or isinstance(value, (str, int, float, bool)):
                return value
            return str(value)

        image_values = list(payload.get("images") or [])
        if payload.get("image"):
            image_values.append(payload.get("image"))
        # Some callers keep references under a semantic name instead of the
        # transport ``images`` field. Include both without storing the media.
        image_values.extend(list(payload.get("reference_images") or []))
        image_hashes = []
        for value in image_values + list(reference_images or []):
            raw = getattr(value, "data", None)
            if raw is None and isinstance(value, Mapping):
                raw = value.get("data") or value.get("value")
            if isinstance(raw, (bytes, bytearray, memoryview)):
                encoded_image = bytes(raw)
            elif isinstance(raw, str):
                encoded_image = raw.encode("utf-8", "ignore")
            else:
                encoded_image = json.dumps(
                    stable_value(value),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8", "ignore")
            image_hashes.append(hashlib.sha256(encoded_image).hexdigest()[:24])
        image_hashes.extend(str(value)[:64] for value in (payload.get("reference_image_hashes") or []))
        try:
            requested_count = max(
                1,
                int(payload.get("requested_count") or payload.get("count") or 1),
            )
        except (TypeError, ValueError):
            requested_count = 1
        fields = {
            "owner_session": str(owner_session or ""),
            "media_type": str(payload.get("media_type") or "image").strip().lower(),
            "prompt": str(
                payload.get("prompt")
                or payload.get("original_prompt")
                or payload.get("action")
                or ""
            ).strip(),
            "channel": str(payload.get("channel") or "").strip(),
            "model": str(payload.get("model") or "").strip(),
            "requested_model": str(payload.get("requested_model") or "").strip(),
            "aspect_ratio": str(payload.get("aspect_ratio") or "").strip(),
            "resolution": str(payload.get("resolution") or "").strip(),
            "count": requested_count,
            "reference_image_count": payload.get(
                "raw_reference_image_count",
                payload.get("reference_image_count", len(image_values)),
            ),
            "use_selfie_reference": bool(payload.get("use_selfie_reference")),
            "prompt_enhance": stable_value(payload.get("prompt_enhance")),
            "template_values": stable_value(payload.get("template_values")),
            "template_randomize": stable_value(payload.get("template_randomize")),
            "variation_enabled": stable_value(payload.get("variation_enabled")),
            "variation_fields": stable_value(payload.get("variation_fields") or payload.get("vary")),
            "preset_name": str(payload.get("preset_name") or "").strip(),
            "unresolved_template_values": stable_value(payload.get("template_unresolved")),
            "template_pools": stable_value(payload.get("template_pools")),
            "template_seed": stable_value(payload.get("template_seed")),
            "special_preset_variants": stable_value(payload.get("special_preset_variants")),
            "source": str(payload.get("source") or payload.get("kind") or "").strip(),
            "owner_user_id": str(payload.get("owner_user_id") or "").strip(),
            "owner_origin": str(payload.get("owner_origin") or "").strip(),
            "retry_record_id": str(payload.get("_retry_record_id") or payload.get("retry_record_id") or "").strip(),
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

    def _log_task_lifecycle_event(
        self,
        task_id: str,
        trace_id: str,
        media_type: str,
        from_status: str,
        to_status: str,
    ) -> None:
        """Emit a bounded lifecycle event without user or provider payloads."""
        valid_statuses = {
            "queued", "running", "succeeded", "partial_success", "failed",
            "delivery_failed", "cancelled", "expired",
        }
        try:
            def safe_text(value: str, limit: int) -> str:
                return "".join(
                    char for char in str(value or "")
                    if char.isascii() and (char.isalnum() or char in "-_")
                )[:limit]

            event = {
                "event": "task.lifecycle",
                "timestamp": self._web_task_timestamp(),
                "task_id": safe_text(task_id, 64),
                "trace_id": safe_text(trace_id, 80),
                "media_type": "image" if str(media_type or "").lower() == "image" else "unknown",
                "from_status": from_status if from_status in valid_statuses else "unknown",
                "to_status": to_status if to_status in valid_statuses else "unknown",
            }
            logger.info("[SelfieImage] " + json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        except Exception:
            # Observability must not change the task state transition outcome.
            return

    def _log_task_notification_event(
        self,
        task_id: str,
        trace_id: str,
        media_type: str,
        channel: str,
        from_status: str,
        to_status: str,
    ) -> None:
        """Emit a bounded delivery-state event without message or error text."""
        try:
            def safe_text(value: str, limit: int) -> str:
                return "".join(
                    char for char in str(value or "")
                    if char.isascii() and (char.isalnum() or char in "-_")
                )[:limit]

            statuses = self._TASK_NOTIFICATION_STATUSES
            event = {
                "event": "task.notification",
                "timestamp": self._web_task_timestamp(),
                "task_id": safe_text(task_id, 64),
                "trace_id": safe_text(trace_id, 80),
                "media_type": "image" if str(media_type or "").lower() == "image" else "unknown",
                "channel": channel if channel in {"message", "image_delivery"} else "unknown",
                "from_status": from_status if from_status in statuses else "unknown",
                "to_status": to_status if to_status in statuses else "unknown",
            }
            logger.info("[SelfieImage] " + json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        except Exception:
            return

    def _set_web_image_task(self, task_id: str, **fields: Any) -> None:
        lifecycle_event = None
        notification_event = None
        try:
            with self._web_task_lock:
                task = self._web_tasks.get(task_id)
                if not task:
                    return
                previous_status = str(task.get("status") or "").strip().lower()
                previous_notification_status = str(task.get("user_notification_status") or "").strip().lower()
                now = time.time()
                if "current_index" in fields:
                    try:
                        fields["current_index"] = max(
                            int(task.get("current_index") or 0), int(fields["current_index"] or 0)
                        )
                    except (TypeError, ValueError):
                        pass
                changed_fields = {
                    key for key, value in fields.items()
                    if key not in task or task.get(key) != value
                }
                if not changed_fields:
                    task_count_before_prune = len(self._web_tasks)
                    self._prune_web_tasks_locked()
                    if len(self._web_tasks) != task_count_before_prune:
                        self._persist_web_tasks_locked()
                        return
                    self._mark_snapshot_write_skipped_locked()
                    return
                task.update(fields)
                status = str(task.get("status") or "").strip().lower()
                if previous_status != status:
                    lifecycle_event = (
                        str(task.get("task_id") or task_id),
                        str(task.get("trace_id") or ""),
                        self._task_media_type(task),
                        previous_status,
                        status,
                    )
                current_notification_status = str(task.get("user_notification_status") or "").strip().lower()
                if "user_notification_status" in fields and previous_notification_status != current_notification_status:
                    notification_event = (
                        str(task.get("task_id") or task_id),
                        str(task.get("trace_id") or ""),
                        self._task_media_type(task),
                        "message",
                        previous_notification_status,
                        current_notification_status,
                    )
                if status in {"failed", "delivery_failed", "expired"}:
                    reason = str(fields.get("error") or task.get("error") or "").strip()
                    if reason:
                        sanitized_reason = redact_sensitive_text(reason)[:320]
                        task["last_failure_reason"] = sanitized_reason
                        self._web_task_last_failure_reason = sanitized_reason
                        self._web_task_last_failure_at = self._web_task_timestamp()
                task["updated_ts"] = now
                task["updated_at"] = self._web_task_timestamp()
                self._prune_web_tasks_locked()
                # Preserve the lifecycle-boundary contract: an explicit
                # ``status`` field still forces a write when another field
                # changed, even if the status value itself is unchanged.
                durable_fields = set(fields) - self._TASK_VOLATILE_FIELDS
                if "status" in durable_fields or task.get("status") in self._TASK_TERMINAL_STATUSES or getattr(self, "_terminating", False):
                    self._persist_web_tasks_locked()
                elif durable_fields:
                    self._schedule_web_task_snapshot_locked()
        finally:
            if lifecycle_event:
                self._log_task_lifecycle_event(*lifecycle_event)
            if notification_event:
                self._log_task_notification_event(*notification_event)

    def _set_task_notification_status(
        self,
        task_id: str,
        status: str,
        reason: str = "",
    ) -> None:
        """Persist the user-facing delivery outcome for a command task.

        Generation and message delivery are separate outcomes. Keeping this
        marker on the task snapshot lets the task center explain a successful
        generation whose notification failed (or whose receipt is unknown)
        without inferring it from a terminal generation status.
        """
        normalized = str(status or "").strip().lower()
        if normalized not in self._TASK_NOTIFICATION_STATUSES:
            normalized = "unknown"
        tid = str(task_id or "").strip()
        if not tid:
            return
        notification_event = None
        with self._web_task_lock:
            task = self._web_tasks.get(tid)
            if not isinstance(task, dict):
                return
            previous = str(task.get("user_notification_status") or "").strip().lower()
            # A late fallback message must not turn an already recorded
            # transport failure into an indeterminate state.
            if normalized == "unknown" and previous in {"failed", "not_sent"}:
                self._mark_snapshot_write_skipped_locked()
                return
            reason_text = redact_sensitive_text(str(reason or ""))[:320]
            if previous == normalized and str(task.get("user_notification_reason") or "") == reason_text:
                self._mark_snapshot_write_skipped_locked()
                return
            task["user_notification_status"] = normalized
            task["user_notification_reason"] = reason_text
            if previous != normalized:
                notification_event = (
                    str(task.get("task_id") or tid),
                    str(task.get("trace_id") or ""),
                    self._task_media_type(task),
                    "message",
                    previous,
                    normalized,
                )
            now = time.time()
            task["updated_ts"] = now
            task["updated_at"] = self._web_task_timestamp()
            self._persist_web_tasks_locked()
        if notification_event:
            self._log_task_notification_event(*notification_event)

    def _task_notification_status(self, task_id: str) -> str:
        """Read the persisted notification marker without exposing task data."""
        tid = str(task_id or "").strip()
        if not tid:
            return ""
        with self._web_task_lock:
            task = self._web_tasks.get(tid)
            return str(task.get("user_notification_status") or "").strip().lower() if isinstance(task, dict) else ""

    def _reconcile_task_image_delivery(self, task_id: str) -> None:
        """Refresh a terminal task from its per-image redelivery evidence."""
        tid = str(task_id or "").strip()
        if not tid:
            return
        lifecycle_event = None
        notification_event = None
        # Freeze record evidence until the task snapshot is persisted.
        with self._records_lock, self._web_task_lock:
            task = self._web_tasks.get(tid)
            if not isinstance(task, dict) or task.get("status") not in {"delivery_failed", "succeeded", "partial_success"}:
                return
            previous_status = str(task.get("status") or "").strip().lower()
            previous_notification_status = str(task.get("user_notification_status") or "").strip().lower()
            records = [r for r in self._records if str(r.get("task_id") or "") == tid]
            result = copy.deepcopy(task.get("result") or {})
            image_states = {}
            for record in records:
                fallback = "failed" if record.get("delivery_failed") else "sent" if record.get("delivery_success") else "unknown"
                for path in record.get("generated_image_paths") or []:
                    image_states[path] = (record.get("image_delivery") or {}).get(path, fallback)
            expected = {
                self._cache_relative_path(path) if os.path.isabs(path) else path
                for path in (result.get("files") or result.get("image_paths") or result.get("generated_image_paths") or [])
            }
            known_ids = {str(r.get("id") or "") for r in records}
            linked_ids = {str(rid) for rid in task.get("record_ids") or []}
            if not image_states or not expected.issubset(image_states) or not linked_ids.issubset(known_ids):
                return
            if not expected and len(image_states) < int(task.get("succeeded_count") or result.get("succeeded_count") or 1):
                return
            failed = sum(state == "failed" for state in image_states.values())
            unknown = sum(state == "unknown" for state in image_states.values())
            previous_error = str(task.get("delivery_error") or task.get("error") or "")
            if (task.get("delivery_failed") or task.get("status") == "delivery_failed") and not failed:
                history = list(task.get("delivery_history") or [])
                history.append({"time": self._web_task_timestamp(), "status": task.get("status"), "error": previous_error[:800]})
                task["delivery_history"] = history[-32:]
                for data in (task, result):
                    data.pop("delivery_error", None)
                    if not data.get("failed_count") or data.get("error") == previous_error:
                        data["error"] = ""
                result.pop("status", None)
                if result.get("failed_count"):
                    result["error"] = next((str(record.get("error") or "") for record in records
                                            if record.get("generation_success") is False and record.get("error")), result.get("error", ""))
            result.update({
                "generation_success": True, "delivery_success": False if failed else None if unknown else True,
                "delivery_failed": bool(failed), "delivery_unknown": bool(unknown and not failed),
                "delivery_failed_count": failed, "delivery_unknown_count": unknown,
            })
            terminal = build_task_terminal_state(
                result, requested_count=task.get("requested_count") or result.get("requested_count") or 1,
            )
            task.update({
                "result": terminal["result"], "status": terminal["terminal_status"], "success": terminal["success"],
                "generation_success": terminal["generation_success"], "delivery_success": terminal["result"]["delivery_success"],
                "delivery_failed": terminal["delivery_failed"], "delivery_unknown": terminal["delivery_unknown"],
                "delivery_failed_count": failed, "delivery_unknown_count": unknown,
                "error": terminal["error"], "generation_stage": terminal["terminal_stage"],
                "generation_stage_label": "部分完成" if terminal["partial_success"] and not failed else "已完成",
                "user_notification_status": "failed" if failed else "unknown" if unknown else "sent",
                "user_notification_reason": previous_error[:320] if failed else "消息投递结果未知" if unknown else "",
                "updated_ts": time.time(), "updated_at": self._web_task_timestamp(),
            })
            self._persist_web_tasks_locked()
            current_status = str(task.get("status") or "").strip().lower()
            current_notification_status = str(task.get("user_notification_status") or "").strip().lower()
            task_id_value = str(task.get("task_id") or tid)
            trace_id = str(task.get("trace_id") or "")
            media_type = self._task_media_type(task)
            if previous_status != current_status:
                lifecycle_event = (task_id_value, trace_id, media_type, previous_status, current_status)
            if previous_notification_status != current_notification_status:
                notification_event = (
                    task_id_value, trace_id, media_type, "image_delivery",
                    previous_notification_status, current_notification_status,
                )
        if lifecycle_event:
            self._log_task_lifecycle_event(*lifecycle_event)
        if notification_event:
            self._log_task_notification_event(*notification_event)

    def _reconcile_task_terminal_evidence_locked(self, task: Dict[str, Any]) -> bool:
        """Promote a stale active snapshot when its result is already final.

        The runner writes the result before publishing the outer task status.
        A status poll or cancel request can therefore briefly observe
        ``running`` together with a completed result. Treat that result as the
        source of truth so the same task cannot be shown as active and then
        cancelled after it has finished.
        """
        if str(task.get("status") or "").strip().lower() not in {"queued", "running"}:
            return False
        result = task.get("result") if isinstance(task.get("result"), Mapping) else None
        if not isinstance(result, Mapping):
            return False
        requested_count = task.get("requested_count") or result.get("requested_count") or 1
        terminal = build_task_terminal_state(
            dict(result),
            requested_count=requested_count,
            cancel_requested=bool(task.get("cancel_requested")),
        )
        if not terminal["terminal_stage"] in {"complete"} and not terminal["delivery_failed"] and not terminal["partial_success"]:
            return False
        now = time.time()
        previous_status = str(task.get("status") or "").strip().lower()
        task.update(
            {
                "status": terminal["terminal_status"],
                "success": terminal["success"],
                "generation_success": terminal["generation_success"],
                "delivery_success": (
                    False
                    if terminal["delivery_failed"]
                    else terminal["result"].get("delivery_success")
                ),
                "delivery_failed": terminal["delivery_failed"],
                "delivery_unknown": terminal["delivery_unknown"],
                "cancel_requested": False,
                "error": terminal["error"],
                "result": terminal["result"],
                "generation_stage": terminal["terminal_stage"],
                "generation_stage_label": "已完成" if terminal["terminal_stage"] == "complete" else "部分完成" if terminal["partial_success"] else "发送失败",
                "finished_ts": task.get("finished_ts") or now,
                "finished_at": task.get("finished_at") or self._web_task_timestamp(),
                "updated_ts": now,
                "updated_at": self._web_task_timestamp(),
            }
        )
        self._persist_web_tasks_locked()
        current_status = str(task.get("status") or "").strip().lower()
        if previous_status != current_status:
            self._log_task_lifecycle_event(
                str(task.get("task_id") or ""),
                str(task.get("trace_id") or ""),
                self._task_media_type(task),
                previous_status,
                current_status,
            )
        return True

    def get_web_image_task(self, task_id: str) -> Dict[str, Any]:
        with self._web_task_lock:
            task = self._web_tasks.get(str(task_id or "").strip())
            if not task:
                raise ValueError("任务不存在或已清理")
            self._reconcile_task_terminal_evidence_locked(task)
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
            timeout = getattr(config, "image_global_timeout", 180)
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

    @staticmethod
    def _task_request_image_paths(task: Mapping[str, Any]) -> list[str]:
        """Expose only bounded, relative cache paths for image-to-image previews."""
        paths: list[str] = []
        seen: set[str] = set()
        containers = [
            task.get("result") if isinstance(task.get("result"), Mapping) else {},
            task,
            task.get("request_data") if isinstance(task.get("request_data"), Mapping) else {},
        ]
        result = task.get("result") if isinstance(task.get("result"), Mapping) else {}
        if isinstance(result.get("request_data"), Mapping):
            containers.insert(1, result["request_data"])
        for container in containers:
            raw_paths = container.get("request_image_paths")
            if not isinstance(raw_paths, (list, tuple)):
                continue
            for raw_path in raw_paths:
                path = str(raw_path or "").strip()
                normalized = path.replace("\\", "/")
                if (
                    not normalized
                    or len(normalized) > 512
                    or normalized.startswith("/")
                    or normalized.startswith("../")
                    or "/../" in normalized
                    or normalized == ".."
                    or (len(normalized) > 1 and normalized[1] == ":")
                    or normalized in seen
                ):
                    continue
                seen.add(normalized)
                paths.append(normalized)
                if len(paths) >= 12:
                    return paths
        return paths

    @staticmethod
    def _task_image_stage(record: Mapping[str, Any]) -> tuple[str, str]:
        """Normalize the generation stage shown for one retained image."""
        response = record.get("response_data") if isinstance(record.get("response_data"), Mapping) else {}
        stage = str(record.get("generation_stage") or response.get("stage") or "").strip().lower()[:80]
        labels = {
            "preflight": "准备图片请求",
            "prompt_translation": "处理图片提示词",
            "image_to_text": "图转文",
            "prompt_audit": "审核提示词",
            "select_model": "选择模型",
            "queue": "等待并发槽位",
            "generating": "生成图片",
            "generate": "生成图片",
            "output_audit": "审核图片结果",
            "delivery": "发送图片",
            "complete": "生成完成",
            "failed": "生成失败",
            "cancelled": "已取消",
            "unknown": "未记录阶段",
        }
        generation_success = record.get("generation_success")
        if generation_success is None and "success" in record:
            generation_success = bool(record.get("success"))
        if generation_success not in (True, False, None):
            generation_success = None
        delivery_success = record.get("delivery_success")
        if delivery_success not in (True, False, None):
            delivery_success = None
        delivery_failed = bool(record.get("delivery_failed")) or delivery_success is False
        delivery_unknown = bool(record.get("delivery_unknown")) or delivery_success is None and bool(
            record.get("generation_success")
        )
        if delivery_failed or delivery_unknown:
            stage = "delivery"
        elif not stage:
            stage = "complete" if generation_success is True else "failed" if generation_success is False else ""
        elif stage not in labels:
            stage = "unknown"
        return stage, labels.get(stage, stage or "等待结果")

    def _task_image_summaries(self, task: Mapping[str, Any]) -> list[Dict[str, Any]]:
        """Build a bounded per-image view from records linked to a task.

        Records are already split to one image during persistence. The
        fallback expansion below keeps legacy multi-image rows readable while
        deliberately omitting cache paths and other media-bearing fields.
        """
        result = task.get("result") if isinstance(task.get("result"), Mapping) else {}
        linked_ids = task.get("record_ids")
        if not isinstance(linked_ids, list):
            linked_ids = result.get("record_ids") if isinstance(result.get("record_ids"), list) else []
        linked_ids = list(dict.fromkeys(str(item).strip() for item in linked_ids if str(item).strip()))
        records = getattr(self, "_records", None)
        records_lock = getattr(self, "_records_lock", None)
        raw_records: list[Mapping[str, Any]] = []
        if isinstance(records, list):
            if records_lock is None:
                snapshot = list(records)
            else:
                with records_lock:
                    snapshot = list(records)
            by_id = {
                str(item.get("id") or "").strip(): item
                for item in snapshot
                if isinstance(item, Mapping) and str(item.get("id") or "").strip()
            }
            seen: set[str] = set()
            for record_id in linked_ids:
                record = by_id.get(record_id)
                if record is not None:
                    raw_records.append(record)
                    seen.add(record_id)
            task_id = str(task.get("task_id") or "").strip()
            for record in snapshot:
                if not isinstance(record, Mapping):
                    continue
                record_id = str(record.get("id") or "").strip()
                if record_id in seen:
                    continue
                if task_id and str(record.get("task_id") or "").strip() == task_id:
                    raw_records.append(record)
                    seen.add(record_id)

        rows: list[Dict[str, Any]] = []

        def clean_paths(value: Any) -> list[str]:
            if not isinstance(value, (list, tuple)):
                return []
            return [str(path).strip() for path in value if str(path).strip()]

        def add_record(record: Mapping[str, Any], paths: list[str]) -> None:
            response = record.get("response_data") if isinstance(record.get("response_data"), Mapping) else {}
            used_model = str(
                record.get("used_model")
                or response.get("used_model")
                or (record.get("request_data", {}).get("model") if isinstance(record.get("request_data"), Mapping) else "")
                or ""
            ).strip()
            generation_success = record.get("generation_success")
            if generation_success is None and "success" in record:
                generation_success = bool(record.get("success"))
            if generation_success not in (True, False, None):
                generation_success = None
            delivery_success = record.get("delivery_success")
            if delivery_success not in (True, False, None):
                delivery_success = None
            delivery_failed = bool(record.get("delivery_failed")) or delivery_success is False
            delivery_unknown = bool(record.get("delivery_unknown")) or delivery_success is None and bool(
                generation_success
            )
            image_states = record.get("image_delivery") if isinstance(record.get("image_delivery"), Mapping) else {}
            stage, stage_label = self._task_image_stage(record)
            error = str(
                record.get("delivery_error")
                or record.get("error")
                or response.get("delivery_error")
                or response.get("error")
                or ""
            ).strip()
            try:
                elapsed = float(record.get("elapsed_seconds"))
                elapsed = round(max(0.0, min(86400.0, elapsed)), 3)
            except (TypeError, ValueError, OverflowError):
                elapsed = None
            for path in paths or [""]:
                state = str(image_states.get(path) or "").strip().lower()
                if state not in {"sent", "failed", "unknown"}:
                    state = "failed" if delivery_failed else "unknown" if delivery_unknown else "sent" if delivery_success is True else "pending"
                rows.append(
                    {
                        "index": len(rows) + 1,
                        "record_id": str(record.get("id") or "")[:128],
                        "generation_stage": stage,
                        "generation_stage_label": stage_label,
                        "used_model": redact_sensitive_text(used_model)[:180],
                        "generation_success": generation_success,
                        "delivery_state": state,
                        "delivery_success": delivery_success,
                        "delivery_unknown": delivery_unknown,
                        "delivery_failed": delivery_failed,
                        "elapsed_seconds": elapsed,
                        "error": redact_sensitive_text(error)[:320],
                    }
                )

        for record in raw_records:
            paths = clean_paths(record.get("generated_image_paths"))
            add_record(record, paths)
            if len(rows) >= 200:
                return rows[:200]

        # A task can be observed between generation completion and record
        # commit. Use terminal result files as a short-lived, path-free view.
        if not rows:
            fallback_paths = clean_paths(
                result.get("files")
                or result.get("image_paths")
                or result.get("generated_image_paths")
            )
            if fallback_paths:
                add_record(result, fallback_paths[:200])
        return rows[:200]

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

    def _task_list_row(
        self,
        task: Mapping[str, Any],
        *,
        include_request_images: bool = False,
    ) -> Dict[str, Any]:
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
            "trace_id",
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
            "quota_reserved",
            "quota_released",
            "quota_released_ts",
            "restart_reconciled",
            "user_notification_status",
            "user_notification_reason",
            "last_failure_reason",
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
        if include_request_images:
            row["request_image_paths"] = self._task_request_image_paths(task)
        return redact_sensitive_data(row)

    def get_web_task_detail(self, task_id: str) -> Dict[str, Any]:
        """Return a redacted detail view with bounded request-image paths."""
        task = self.get_web_image_task(task_id)
        request = task.get("request_data") if isinstance(task.get("request_data"), Mapping) else {}
        result = task.get("result") if isinstance(task.get("result"), Mapping) else {}
        result_request = result.get("request_data") if isinstance(result.get("request_data"), Mapping) else {}
        detail = self._task_list_row(task, include_request_images=True)
        request_details = dict(result_request)
        request_details.update({key: value for key, value in request.items() if value not in (None, "")})
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
                    key: redact_sensitive_data(request_details.get(key))
                    for key in (
                        "kind",
                        "mode",
                        "session_id",
                        "studio_template",
                        "channel",
                        "model",
                        "aspect_ratio",
                        "resolution",
                        "timeout_seconds",
                        "size",
                        "count",
                        "requested_count",
                        "prompt_enhance",
                        "use_selfie_reference",
                        "reference_image_count",
                        "persona_reference_count",
                        "raw_reference_image_count",
                        "deduplicated_reference_image_count",
                        "duplicate_reference_image_count",
                        "identity_reference_source",
                        "identity_reference_count",
                        "extra_reference_image_count",
                        "raw_reference_image_count_total",
                        "deduplicated_reference_image_count_total",
                        "duplicate_reference_image_count_total",
                        "reference_selection",
                        "used_slots",
                        "source_asset_ids",
                        "studio_source_asset_ids",
                        "composition",
                        "cos",
                        "cos_pose",
                        "cos_scene",
                        "cos_framing",
                        "cos_composition",
                        "cos_view",
                        "request_prompt_en",
                    )
                    if request_details.get(key) not in (None, "")
                },
                "record_ids": list(detail.get("record_ids") or []),
                "attempts": self._task_attempt_summaries(task),
                "result_summary": {
                    "file_count": len(
                        result.get("files")
                        or result.get("image_paths")
                        or result.get("generated_image_paths")
                        or []
                    ),
                    "used_model": redact_sensitive_text(str(result.get("used_model") or ""))[:180],
                    "elapsed_seconds": result.get("elapsed_seconds"),
                    "generation_success": result.get("generation_success"),
                    "delivery_success": result.get("delivery_success"),
                    "delivery_unknown": result.get("delivery_unknown"),
                },
                "image_results": self._task_image_summaries(task),
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
        if wanted not in {"", "image"}:
            raise ValueError("当前仅支持图片任务")
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
            for task in self._web_tasks.values():
                if isinstance(task, dict):
                    self._reconcile_task_terminal_evidence_locked(task)
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

        image_scheduler = getattr(self, "_image_scheduler", None)
        try:
            image_max = int(getattr(getattr(self, "config", None), "image_max_concurrent_tasks", 5) or 5)
        except (TypeError, ValueError):
            image_max = 5
        image_max = max(1, min(5, image_max))
        image_active = max(0, min(image_max, int(getattr(image_scheduler, "_active", 0) or 0)))
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
        scope: str = "all",
    ) -> Dict[str, Any]:
        """Submit retries for records linked to selected terminal tasks.

        Task payloads intentionally omit request bodies, so a retry is only
        possible when the task has at least one retained generation record.
        Each linked record is retried independently and receives the optional
        feedback string. ``scope=failed`` limits the operation to records whose
        generation or delivery state is known to be unsuccessful.
        """
        ids = self._normalize_task_ids(task_ids)
        if not ids:
            raise ValueError("至少选择一条任务")
        if len(ids) > 200:
            raise ValueError("单次最多重试 200 条任务")
        normalized_scope = str(scope or "all").strip().lower().replace("-", "_")
        if normalized_scope in {"failed_only", "only_failed", "失败", "仅失败"}:
            normalized_scope = "failed"
        if normalized_scope not in {"all", "failed"}:
            raise ValueError("不支持的重试范围，可选：all、failed")
        retry = getattr(self, "start_record_retry_task", None)
        if not callable(retry):
            raise RuntimeError("当前版本不支持任务重试")
        record_getter = getattr(self, "get_record_for_web", None)
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
                if normalized_scope == "failed":
                    if not callable(record_getter):
                        skipped.append({"task_id": task_id, "record_id": record_id, "reason": "无法读取生成记录状态"})
                        continue
                    try:
                        record = record_getter(record_id)
                    except Exception:
                        skipped.append({"task_id": task_id, "record_id": record_id, "reason": "生成记录不存在或已清理"})
                        continue
                    response = record.get("response_data") if isinstance(record, Mapping) else {}
                    if not isinstance(response, Mapping):
                        response = {}
                    status = str(record.get("status") or response.get("status") or "").strip().lower() if isinstance(record, Mapping) else ""
                    generation_failed = (
                        isinstance(record, Mapping)
                        and (record.get("success") is False or record.get("generation_success") is False)
                    ) or response.get("success") is False
                    delivery_failed = (
                        isinstance(record, Mapping)
                        and (
                            record.get("delivery_success") is False
                            or bool(record.get("delivery_failed"))
                            or bool(record.get("delivery_unknown"))
                        )
                    ) or status in {"failed", "delivery_failed", "partial_success"}
                    if not generation_failed and not delivery_failed:
                        skipped.append({"task_id": task_id, "record_id": record_id, "reason": "生成记录未失败"})
                        continue
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
            "scope": normalized_scope,
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
            rows.append(self._task_list_row(detail, include_request_images=True))
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
        if getattr(self, "_terminating", False):
            raise RuntimeError("插件正在卸载，无法提交新任务")
        if not isinstance(payload, dict):
            raise RuntimeError("请求体必须是 JSON 对象")
        loop = getattr(self, "loop", None)
        if loop is None or not loop.is_running():
            raise RuntimeError("AstrBot 事件循环未就绪，无法启动后台生图任务")
        payload_copy = copy.deepcopy(payload)
        media_type = str(payload_copy.get("media_type") or "image").strip().lower()
        if media_type != "image":
            raise RuntimeError("当前仅支持图片任务")
        self._validate_web_test_selection(payload_copy)
        requested_count = self._normalize_web_image_count(payload_copy.get("count"))
        payload_copy["count"] = requested_count
        payload_copy["requested_count"] = requested_count
        force_regenerate = bool(
            payload_copy.get("force_regenerate") or payload_copy.get("force")
        )
        payload_copy.setdefault("source", "record-retry" if payload_copy.get("_retry_record_id") or payload_copy.get("retry_record_id") else "web-test")
        selfie_refs = []
        if payload_copy.get("use_selfie_reference"):
            identity_getter = getattr(self, "_persona_identity_reference", None)
            auxiliary_getter = getattr(self, "_persona_auxiliary_references", None)
            identity = identity_getter() if callable(identity_getter) else None
            if identity:
                selfie_refs.append(identity)
            if callable(auxiliary_getter) and self._summarize_web_test_payload(payload_copy)["prompt_enhance"]:
                selfie_refs.extend(auxiliary_getter(str(payload_copy.get("prompt") or "")))
        fingerprint = self._request_fingerprint(
            payload_copy, str(payload_copy.get("owner_session") or "web"), selfie_refs,
        )
        with self._web_task_lock:
            if getattr(self, "_terminating", False):
                raise RuntimeError("插件正在卸载，无法提交新任务")
            if not force_regenerate:
                duplicate = self._find_recent_duplicate_task_locked(fingerprint)
                if duplicate:
                    duplicate["deduplicated"] = True
                    return redact_sensitive_data(duplicate)
            self._web_task_seq += 1
            task_id = f"web-{int(time.time() * 1000)}-{self._web_task_seq}"
            trace_id = new_trace_id("gen")
            now = time.time()
            self._web_tasks[task_id] = {
                "task_id": task_id,
                "trace_id": trace_id,
                "status": "queued",
                "success": None,
                "error": "",
                "created_ts": now,
                "updated_ts": now,
                "created_at": self._web_task_timestamp(),
                "updated_at": self._web_task_timestamp(),
                "request_data": self._summarize_web_test_payload(payload_copy),
                "result": None,
                "source": "record-retry" if payload_copy.get("_retry_record_id") or payload_copy.get("retry_record_id") else "web-test",
                "owner_session": str(payload_copy.get("owner_session") or "web").strip()[:200] or "web",
                "cancel_requested": False,
                "request_fingerprint": fingerprint,
                "deduplicated": False,
                **self._task_runtime_defaults(),
                **self._task_progress_defaults(requested_count),
                "generation_stage": "preflight",
                "generation_stage_label": "准备图片任务",
            }
            self._prune_web_tasks_locked()
            self._persist_web_tasks_locked()
        self._log_task_lifecycle_event(task_id, trace_id, media_type, "", "queued")
        payload_copy["_trace_id"] = trace_id
        self._start_runtime_generation(task_id, self._run_web_image_task(task_id, payload_copy))
        return self.get_web_image_task(task_id)

    async def _run_web_image_task(self, task_id: str, payload: Dict[str, Any]) -> None:
        media_type = str(payload.get("media_type") or "image").strip().lower()
        generation_started = time.time()
        runtime_fields = {
            "queue_waiting": False,
            "queue_position": 0,
        }
        runtime_fields["generation_started_ts"] = generation_started
        self._set_web_image_task(
            task_id,
            status="running",
            started_ts=time.time(),
            started_at=self._web_task_timestamp(),
            generation_stage="preflight",
            generation_stage_label="准备图片任务",
            **runtime_fields,
        )
        try:
            if self._task_cancel_requested(task_id):
                raise RuntimeError("任务已取消")
            # Keep the queue ID in-memory only; generation handlers copy it to
            # their record metadata and the compact request summary omits it.
            run_payload = copy.deepcopy(payload)
            run_payload["_task_id"] = task_id
            result = await self.web_test_image(run_payload)
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
                        source=str(payload.get("source") or "web-test"),
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
                    source=str(payload.get("source") or "web-test"),
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
                    source=str(payload.get("source") or "web-test"),
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
