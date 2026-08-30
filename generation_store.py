"""Generation record persistence, cache management, and channel health."""

from __future__ import annotations

import asyncio
import copy
import os
import time
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .generation_records import build_generation_metrics, composition_metadata
from .providers import ImageReference
from .utils import (
    collect_cache_cleanup_candidates,
    collect_record_cache_paths,
    collect_unreferenced_record_cache_paths,
    compact_generation_record,
    detect_mime_by_bytes,
    load_json_file,
    looks_like_image_bytes,
    redact_sensitive_data,
    safe_delete_relative_files,
    save_image_bytes,
    save_json_file,
    split_generation_record_images,
    summarize_record_for_list,
)

RECORD_KEEP_LIMIT = 300


class GenerationStoreMixin:
    def _load_records(self) -> List[Dict[str, Any]]:
        data = load_json_file(self.records_path)
        items = data.get("records") if isinstance(data.get("records"), list) else []
        records = [item for item in items if isinstance(item, dict)]
        compacted = [compact_generation_record(redact_sensitive_data(item)) for item in records[:RECORD_KEEP_LIMIT]]
        return compacted

    def _persist_records(self) -> None:
        with self._records_lock:
            self._records = [
                compact_generation_record(redact_sensitive_data(item))
                for item in self._records[:RECORD_KEEP_LIMIT]
                if isinstance(item, dict)
            ]
            save_json_file(self.records_path, {"records": self._records})


    def _record_task(self, record: Dict[str, Any]) -> None:
        payload = copy.deepcopy(record)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._commit_generation_records(payload)
            return
        loop.create_task(asyncio.to_thread(self._commit_generation_records, payload))

    def _commit_generation_records(self, record: Dict[str, Any]) -> None:
        # One generated image per monitor row. Batch/concurrency must not pile shots together.
        for piece in split_generation_record_images(record):
            self._commit_generation_record(piece)

    def _commit_generation_record(self, record: Dict[str, Any]) -> None:
        stale_cache_paths: List[str] = []
        response_data = record.get("response_data")
        if "attempts" not in record and isinstance(response_data, Mapping):
            record["attempts"] = list(response_data.get("attempts") or [])
        # Enrich failure fields for monitor list/detail (also backfills empty used_model).
        try:
            from .error_classify import summarize_generation_failures

            attempts = list(record.get("attempts") or [])
            if not attempts and isinstance(response_data, Mapping):
                attempts = list(response_data.get("attempts") or [])
            summary = summarize_generation_failures(
                attempts,
                fallback_error=str(record.get("error") or ""),
            )
            # Intermediate failures are useful even when final attempt succeeded.
            if summary.get("failure_reasons"):
                record["failure_reasons"] = summary["failure_reasons"]
            if record.get("success") is False:
                if summary.get("failure_reason"):
                    record["failure_reason"] = summary["failure_reason"]
                if not str(record.get("used_model") or "").strip() and summary.get("last_failed_model"):
                    record["used_model"] = summary["last_failed_model"]
                if not str(record.get("error") or "").strip() and summary.get("failure_reason"):
                    record["error"] = summary["failure_reason"]
            # success path: never promote intermediate failures into top-level failure_reason/error
            elif "failure_reason" in record and record.get("success") is True:
                record.pop("failure_reason", None)
        except Exception:
            pass
        with self._records_lock:
            record = compact_generation_record(redact_sensitive_data(dict(record)))
            self._record_seq += 1
            record.setdefault("id", f"{int(time.time() * 1000)}-{self._record_seq}")
            record["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            self._records.insert(0, record)
            evicted_records = self._records[RECORD_KEEP_LIMIT:]
            if evicted_records:
                del self._records[RECORD_KEEP_LIMIT:]
                stale_cache_paths = collect_unreferenced_record_cache_paths(evicted_records, self._records)
            self._persist_records()
        if stale_cache_paths:
            safe_delete_relative_files(self.generated_dir, stale_cache_paths)

    def get_recent_records(self, *, summary: bool = False) -> List[Dict[str, Any]]:
        with self._records_lock:
            # Avoid deepcopy of fat nested blobs on every list poll.
            records = [dict(item) if isinstance(item, dict) else item for item in self._records[:RECORD_KEEP_LIMIT]]
        if summary:
            return redact_sensitive_data(
                [summarize_record_for_list(self._enrich_record_for_web(item)) for item in records if isinstance(item, dict)]
            )
        return redact_sensitive_data([self._enrich_record_for_web(item) for item in records if isinstance(item, dict)])

    def get_generation_metrics(self) -> Dict[str, Any]:
        """Return redacted aggregate metrics from retained generation records."""
        with self._records_lock:
            records = [dict(item) for item in self._records if isinstance(item, dict)]
        return build_generation_metrics(records)

    def _composition_metadata(self, prompt: str, source: str, aspect_ratio: str, resolution: str, reference_count: int) -> Dict[str, Any]:
        return composition_metadata(
            prompt,
            source,
            aspect_ratio,
            resolution,
            reference_count,
        )

    def get_record_for_web(self, record_id: str) -> Dict[str, Any]:
        target_id = str(record_id or "").strip()
        with self._records_lock:
            for record in self._records:
                if str(record.get("id") or "") == target_id:
                    return redact_sensitive_data(self._enrich_record_for_web(copy.deepcopy(record)))
        raise ValueError("记录不存在或已清理")

    def _enrich_record_for_web(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Backfill failure fields for monitor without rewriting disk."""
        if not isinstance(record, dict):
            return record
        try:
            from .error_classify import summarize_generation_failures

            attempts = list(record.get("attempts") or [])
            response_data = record.get("response_data")
            if not attempts and isinstance(response_data, Mapping):
                attempts = list(response_data.get("attempts") or [])
            if not attempts:
                return record
            summary = summarize_generation_failures(
                attempts,
                fallback_error=str(record.get("error") or record.get("failure_reason") or ""),
            )
            # Always surface intermediate failed attempts (including final-success retries).
            if not record.get("failure_reasons") and summary.get("failure_reasons"):
                record["failure_reasons"] = summary["failure_reasons"]
            if record.get("success") is False:
                if not str(record.get("failure_reason") or "").strip() and summary.get("failure_reason"):
                    record["failure_reason"] = summary["failure_reason"]
                if not str(record.get("used_model") or "").strip() and summary.get("last_failed_model"):
                    record["used_model"] = summary["last_failed_model"]
            elif record.get("success") is True:
                # Final success should not look like a terminal failure in the detail header.
                record.pop("failure_reason", None)
        except Exception:
            pass
        return record

    def clear_recent_records(self) -> int:
        with self._records_lock:
            count = len(self._records)
            records = copy.deepcopy(self._records)
            self._records.clear()
            self._persist_records()
        safe_delete_relative_files(self.generated_dir, collect_record_cache_paths(records))
        return count

    def _cache_relative_path(self, path: str) -> str:
        try:
            return os.path.relpath(os.path.abspath(path), os.path.abspath(self.generated_dir))
        except Exception:
            return str(path or "")

    def _cache_absolute_path(self, rel_path: str) -> str:
        base = os.path.abspath(self.generated_dir)
        raw_path = str(rel_path or "").strip()
        if not raw_path:
            raise ValueError("图片路径不能为空")
        path = os.path.abspath(os.path.join(base, raw_path))
        if path == base or not path.startswith(base + os.sep):
            raise ValueError("非法图片路径")
        return path

    def get_cached_image_info(self, rel_path: str) -> Dict[str, Any]:
        abs_path = self._cache_absolute_path(rel_path)
        exists = os.path.exists(abs_path) and os.path.isfile(abs_path)
        mime = "image/png"
        is_image = False
        is_video = False
        if exists:
            with open(abs_path, "rb") as handle:
                head = handle.read(512)
            is_image = looks_like_image_bytes(head)
            is_video = head[4:12].startswith(b"ftyp") or abs_path.lower().endswith((".mp4", ".webm", ".mov"))
            if is_image:
                mime = detect_mime_by_bytes(head)
            elif is_video:
                mime = "video/webm" if abs_path.lower().endswith(".webm") else "video/quicktime" if abs_path.lower().endswith(".mov") else "video/mp4"
        return {
            "path": rel_path,
            "absolute_path": abs_path,
            "name": os.path.basename(abs_path),
            "exists": exists,
            "is_image": is_image,
            "is_video": is_video,
            "mime_type": mime,
        }

    def _save_cache_image(self, data: bytes, prefix: str, mime: str = "") -> str:
        path = save_image_bytes(data, self.generated_dir, prefix=prefix, mime=mime or detect_mime_by_bytes(data))
        return self._cache_relative_path(path)

    def _load_cache_image_bytes(self, rel_path: str) -> Optional[Tuple[bytes, str]]:
        try:
            info = self.get_cached_image_info(rel_path)
        except Exception:
            return None
        if not info.get("exists") or info.get("is_image") is False:
            return None
        try:
            with open(info["absolute_path"], "rb") as handle:
                data = handle.read()
        except OSError:
            return None
        if not data:
            return None
        mime = str(info.get("mime_type") or detect_mime_by_bytes(data) or "image/png")
        return data, mime

    def _save_reference_images_to_cache(self, refs: List[ImageReference]) -> List[str]:
        paths: List[str] = []
        for ref in refs:
            if ref.data:
                paths.append(self._save_cache_image(ref.data, "request", ref.mime_type))
        return paths

    def _cache_size_bytes(self) -> int:
        total = 0
        for root, _, files in os.walk(self.generated_dir):
            for name in files:
                path = os.path.join(root, name)
                try:
                    total += os.path.getsize(path)
                except OSError:
                    pass
        return total

    def _cleanup_image_cache_if_needed(self, protected_paths: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        limit = max(10, int(self.config.image_cache_limit_mb or 100)) * 1024 * 1024
        total = self._cache_size_bytes()
        deleted: List[str] = []
        if total <= limit:
            return {"limit_bytes": limit, "total_bytes": total, "deleted": deleted}
        with self._records_lock:
            referenced_paths = collect_record_cache_paths(self._records)
        candidates = collect_cache_cleanup_candidates(self.generated_dir, protected_paths, referenced_paths)
        for path in candidates:
            try:
                size = os.path.getsize(path)
                os.remove(path)
                deleted.append(self._cache_relative_path(path))
                total = max(0, total - size)
            except OSError:
                pass
            if total <= limit:
                break
        return {"limit_bytes": limit, "total_bytes": total, "deleted": deleted}

    def get_cache_cleanup_preview(self, protected_paths: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        """Return a dry-run cache cleanup plan without deleting files."""
        limit = max(10, int(self.config.image_cache_limit_mb or 100)) * 1024 * 1024
        total = self._cache_size_bytes()
        with self._records_lock:
            referenced_paths = collect_record_cache_paths(self._records)
        candidates = collect_cache_cleanup_candidates(self.generated_dir, protected_paths, referenced_paths)
        planned: List[Dict[str, Any]] = []
        remaining = total
        if total > limit:
            for path in candidates:
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                planned.append({"path": self._cache_relative_path(path), "size_bytes": size})
                remaining = max(0, remaining - size)
                if remaining <= limit:
                    break
        return {
            "limit_bytes": limit,
            "total_bytes": total,
            "would_delete_bytes": total - remaining,
            "remaining_bytes": remaining,
            "would_delete": planned,
        }

    def clear_channel_health(self, channel: str = "") -> Dict[str, Any]:
        with self._channel_health_lock:
            if channel:
                self._channel_health.pop(str(channel).strip(), None)
            else:
                self._channel_health.clear()
            return self.get_channel_health()

    def get_channel_health(self) -> Dict[str, Any]:
        now = time.time()
        with self._channel_health_lock:
            return {
                name: {**dict(state), "cooldown_remaining": round(max(0.0, float(state.get("cooldown_until") or 0) - now), 2)}
                for name, state in self._channel_health.items()
            }

    def _channel_is_healthy(self, channel: str) -> bool:
        with self._channel_health_lock:
            state = self._channel_health.get(str(channel).strip()) or {}
            return float(state.get("cooldown_until") or 0) <= time.time()

    def _record_channel_health(self, attempts: Iterable[Mapping[str, Any]]) -> None:
        now = time.time()
        for attempt in attempts:
            channel = str(attempt.get("channel") or "").strip()
            if not channel:
                continue
            category = str(attempt.get("error_category") or "").strip()
            success = bool(attempt.get("success"))
            if not success and category not in {"network", "server", "timeout_create", "timeout_poll"}:
                continue
            with self._channel_health_lock:
                state = self._channel_health.setdefault(channel, {"consecutive_failures": 0, "last_error_category": ""})
                if success:
                    state["consecutive_failures"] = 0
                    state["cooldown_until"] = 0
                    state["last_success_ts"] = now
                    continue
                if category not in {"network", "server", "timeout_create", "timeout_poll"}:
                    continue
                state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
                state["last_error_category"] = category
                state["last_error_ts"] = now
                if state["consecutive_failures"] >= 3:
                    state["cooldown_until"] = now + 60
