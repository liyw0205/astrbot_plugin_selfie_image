"""Generation record persistence, cache management, and channel health."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import threading
import time
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .generation_records import _record_timestamp, build_generation_metrics, composition_metadata
from .record_database import RecordDatabase
from ..core.providers import ImageReference
from ..core.utils import (
    collect_cache_cleanup_candidates,
    collect_record_cache_paths,
    collect_unreferenced_record_cache_paths,
    compact_generation_record,
    detect_mime_by_bytes,
    load_json_file,
    looks_like_image_bytes,
    redact_generation_record,
    redact_sensitive_text,
    redact_sensitive_data,
    safe_delete_relative_files,
    save_image_bytes,
    save_json_file,
    split_generation_record_images,
    summarize_record_for_list,
    bytes_to_data_url,
)

RECORD_KEEP_LIMIT = 1000
ASSET_QUERY_SCAN_LIMIT = RECORD_KEEP_LIMIT
CHANNEL_COOLDOWN_FAILURE_THRESHOLD = 3
CHANNEL_COOLDOWN_SECONDS = 60
CHANNEL_TRANSIENT_ERROR_CATEGORIES = {
    "network",
    "server",
    "timeout",
    "timeout_create",
    "timeout_poll",
}


class GenerationStoreMixin:
    @staticmethod
    def _coerce_optional_bool(value: Any) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"", "none", "null"}:
                return None
            if lowered in {"1", "true", "yes", "on", "是", "开启"}:
                return True
            if lowered in {"0", "false", "no", "off", "否", "关闭"}:
                return False
        return bool(value)

    def _record_database(self) -> Optional[RecordDatabase]:
        path = str(getattr(self, "records_db_path", "") or "").strip()
        if not path:
            return None
        database = getattr(self, "_record_db", None)
        if database is not None:
            return database
        try:
            database = RecordDatabase(path)
        except Exception:
            return None
        self._record_db = database
        return database

    def _media_sources_root(self) -> str:
        root = str(getattr(self, "media_sources_dir", "") or "").strip()
        if not root:
            data_dir = str(getattr(self, "data_dir", "") or "").strip()
            root = os.path.join(data_dir, "media_sources") if data_dir else ""
        if root:
            os.makedirs(root, exist_ok=True)
        return root

    @staticmethod
    def _record_media_sources(record: Mapping[str, Any]) -> Dict[str, Any]:
        response = record.get("response_data")
        response = response if isinstance(response, Mapping) else {}
        image_sources = record.get("generated_image_sources")
        if not isinstance(image_sources, list) or not image_sources:
            image_sources = response.get("generated_image_sources")
        if not isinstance(image_sources, list):
            image_sources = []
        video_source = (
            record.get("video_source")
            or record.get("video_url")
            or response.get("video_source")
            or response.get("video_url")
            or ""
        )
        return {
            "generated_image_sources": copy.deepcopy(image_sources),
            "video_source": copy.deepcopy(video_source),
        }

    def _record_cache_media_paths(self, record: Mapping[str, Any]) -> List[str]:
        """Return generated media paths in the same order as source entries."""
        response = record.get("response_data")
        response = response if isinstance(response, Mapping) else {}
        paths = record.get("generated_image_paths")
        if not isinstance(paths, list) or not paths:
            paths = response.get("generated_image_paths")
        if not isinstance(paths, list):
            paths = []
        return [str(path).strip() for path in paths if str(path or "").strip()]

    def _compact_media_sources(self, record: Mapping[str, Any], sources: Mapping[str, Any]) -> Dict[str, Any]:
        """Replace large inline image sources with paths to the saved cache files."""
        compact = copy.deepcopy(dict(sources))
        image_sources = compact.get("generated_image_sources")
        paths = self._record_cache_media_paths(record)
        if not isinstance(image_sources, list) or not paths:
            return compact
        converted: List[Any] = []
        for index, source in enumerate(image_sources):
            path = paths[index] if index < len(paths) else ""
            if isinstance(source, Mapping):
                source_type = str(source.get("type") or "").strip().lower()
                value = str(source.get("value") or "").strip()
                if source_type == "base64" and value and path:
                    try:
                        absolute = self._cache_absolute_path(path)
                        if os.path.isfile(absolute):
                            converted.append({"type": "cache_path", "value": path})
                            continue
                    except (OSError, ValueError):
                        pass
            elif isinstance(source, str) and source.strip() and path:
                try:
                    absolute = self._cache_absolute_path(path)
                    if os.path.isfile(absolute):
                        converted.append({"type": "cache_path", "value": path})
                        continue
                except (OSError, ValueError):
                    pass
            converted.append(source)
        compact["generated_image_sources"] = converted
        return compact

    def _media_sources_limit_bytes(self) -> int:
        try:
            limit_mb = max(10, int(getattr(self.config, "image_cache_limit_mb", 200) or 200))
        except (AttributeError, TypeError, ValueError):
            limit_mb = 200
        return limit_mb * 1024 * 1024

    def _media_sources_size_bytes(self) -> int:
        root = self._media_sources_root()
        total = 0
        if not root or not os.path.isdir(root):
            return total
        for current, _, names in os.walk(root):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(current, name))
                except OSError:
                    continue
        return total

    def _schedule_media_sources_prune(self, records: Iterable[Mapping[str, Any]]) -> None:
        """Run the potentially large sidecar sweep without holding record locks."""
        try:
            if self._media_sources_size_bytes() <= self._media_sources_limit_bytes():
                return
        except OSError:
            return
        cleanup_lock = getattr(self, "_media_sources_cleanup_lock", None)
        if cleanup_lock is None:
            cleanup_lock = threading.Lock()
            self._media_sources_cleanup_lock = cleanup_lock
        if not cleanup_lock.acquire(False):
            return
        try:
            snapshot = [copy.deepcopy(item) for item in (records or ()) if isinstance(item, Mapping)]
        except Exception:
            cleanup_lock.release()
            return

        def worker() -> None:
            try:
                self._prune_orphan_media_sidecars(snapshot)
            finally:
                cleanup_lock.release()

        threading.Thread(
            target=worker,
            name="selfie-media-source-cleanup",
            daemon=True,
        ).start()

    @staticmethod
    def _remove_media_sources(record: Dict[str, Any]) -> None:
        for key in ("generated_image_sources", "video_source", "video_url"):
            record.pop(key, None)
        response = record.get("response_data")
        if isinstance(response, dict):
            for key in ("generated_image_sources", "video_source", "video_url"):
                response.pop(key, None)

    def _sidecar_path(self, record_id: str) -> str:
        root = self._media_sources_root()
        if not root:
            return ""
        text = str(record_id or "").strip()
        safe = text if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", text) else hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
        return os.path.join(root, safe + ".json")

    def _write_media_sidecar(self, record_id: str, record: Mapping[str, Any]) -> str:
        sources = self._compact_media_sources(record, self._record_media_sources(record))
        if not sources["generated_image_sources"] and not sources["video_source"]:
            return ""
        path = self._sidecar_path(record_id)
        if not path:
            return ""
        try:
            save_json_file(path, {"record_id": str(record_id), **sources})
        except Exception:
            return ""
        return os.path.relpath(path, self._media_sources_root())

    def _read_media_sidecar(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        record_id = str(record.get("id") or "").strip()
        filename = str(record.get("media_sources_file") or "").strip()
        root = self._media_sources_root()
        if not root or not record_id:
            return {"generated_image_sources": [], "video_source": ""}
        path = os.path.abspath(os.path.join(root, filename)) if filename else self._sidecar_path(record_id)
        if path == root or not path.startswith(os.path.abspath(root) + os.sep):
            return {"generated_image_sources": [], "video_source": ""}
        data = load_json_file(path)
        if not isinstance(data, Mapping):
            return {"generated_image_sources": [], "video_source": ""}
        sources = self._record_media_sources(data)
        image_sources = sources.get("generated_image_sources")
        if not isinstance(image_sources, list):
            return sources
        materialized: List[Any] = []
        for source in image_sources:
            if not isinstance(source, Mapping) or str(source.get("type") or "").strip().lower() != "cache_path":
                materialized.append(source)
                continue
            try:
                absolute = self._cache_absolute_path(str(source.get("value") or ""))
                with open(absolute, "rb") as handle:
                    blob = handle.read()
                if not blob:
                    materialized.append(source)
                    continue
                materialized.append({"type": "base64", "value": bytes_to_data_url(blob)})
            except (OSError, ValueError):
                materialized.append(source)
        sources["generated_image_sources"] = materialized
        return sources

    def _attach_media_sidecar(self, record: Dict[str, Any]) -> Dict[str, Any]:
        sources = self._read_media_sidecar(record)
        if sources["generated_image_sources"]:
            record["generated_image_sources"] = sources["generated_image_sources"]
        if sources["video_source"]:
            record["video_source"] = sources["video_source"]
        response = record.get("response_data")
        if isinstance(response, dict):
            if sources["generated_image_sources"]:
                response["generated_image_sources"] = copy.deepcopy(sources["generated_image_sources"])
            if sources["video_source"]:
                response["video_source"] = sources["video_source"]
        return record

    def _delete_media_sidecars(self, records: Iterable[Mapping[str, Any]]) -> None:
        for record in records:
            record_id = str(record.get("id") or "").strip() if isinstance(record, Mapping) else ""
            path = self._sidecar_path(record_id) if record_id else ""
            if path:
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass

    def _prune_orphan_media_sidecars(self, records: Iterable[Mapping[str, Any]]) -> int:
        """Remove media-source sidecars that no retained record can load."""
        root = self._media_sources_root()
        if not root or not os.path.isdir(root):
            return 0
        started_at = time.time()
        expected = set()
        for record in records or ():
            if not isinstance(record, Mapping):
                continue
            record_id = str(record.get("id") or "").strip()
            if record_id:
                expected.add(os.path.basename(self._sidecar_path(record_id)))
            declared = str(record.get("media_sources_file") or "").strip()
            if declared and os.path.basename(declared) == declared and declared.endswith(".json"):
                expected.add(declared)
        deleted = 0
        try:
            names = os.listdir(root)
        except OSError:
            return 0
        record_by_sidecar: Dict[str, Mapping[str, Any]] = {}
        for record in records or ():
            if not isinstance(record, Mapping):
                continue
            record_id = str(record.get("id") or "").strip()
            if not record_id:
                continue
            record_by_sidecar[os.path.basename(self._sidecar_path(record_id))] = record
            path = self._sidecar_path(record_id)
            if not path or not os.path.isfile(path):
                continue
            data = load_json_file(path)
            if not isinstance(data, Mapping):
                continue
            sources = self._record_media_sources(data)
            compact_sources = self._compact_media_sources(record, sources)
            if compact_sources == sources:
                continue
            try:
                save_json_file(path, {"record_id": record_id, **compact_sources})
            except OSError:
                continue
        sidecars: List[Tuple[float, int, str, bool, bool]] = []
        for name in names:
            if not name.endswith(".json") or name in expected:
                continue
            path = os.path.join(root, name)
            try:
                # A concurrent record commit may create its sidecar after the
                # retained-record snapshot was taken. Let the next sweep
                # inspect it instead of deleting a just-written sidecar.
                if os.path.isfile(path) and os.path.getmtime(path) < started_at:
                    os.remove(path)
                    deleted += 1
            except OSError:
                continue
        limit_bytes = self._media_sources_limit_bytes()
        total_bytes = 0
        for name in names:
            if not name.endswith(".json") or name not in expected:
                continue
            path = os.path.join(root, name)
            try:
                size = os.path.getsize(path)
                data = load_json_file(path)
                sources = self._record_media_sources(data) if isinstance(data, Mapping) else {}
                image_sources = sources.get("generated_image_sources") if isinstance(sources, Mapping) else []
                has_inline = any(
                    isinstance(item, Mapping) and str(item.get("type") or "").strip().lower() == "base64"
                    for item in (image_sources or [])
                )
                record = record_by_sidecar.get(name) or {}
                protected = bool(record.get("favorite") or record.get("pinned"))
                sidecars.append((os.path.getmtime(path), size, path, has_inline, protected))
                total_bytes += size
            except OSError:
                continue
        # Old records whose generated cache has already disappeared cannot be
        # displayed from the path anymore; bound their retained source blobs so
        # the sidecar directory cannot grow without limit.
        for _, size, path, has_inline, protected in sorted(sidecars):
            if total_bytes <= limit_bytes:
                break
            if not has_inline or protected:
                continue
            try:
                os.remove(path)
                total_bytes -= size
                deleted += 1
            except OSError:
                continue
        return deleted

    def _backup_legacy_records(self, data: Any) -> None:
        path = str(getattr(self, "records_path", "") or "").strip()
        if not path or not os.path.isfile(path):
            return
        backup = path + ".bak"
        if os.path.exists(backup):
            return
        try:
            shutil.copy2(path, backup)
            save_json_file(
                path,
                {
                    "migrated_to": os.path.basename(str(getattr(self, "records_db_path", "generation_records.sqlite3"))),
                    "backup": os.path.basename(backup),
                    "migrated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                },
            )
        except OSError:
            pass

    def _load_records(self) -> List[Dict[str, Any]]:
        data = load_json_file(self.records_path)
        items = data.get("records") if isinstance(data.get("records"), list) else []
        records = [item for item in items if isinstance(item, dict)]
        retained = records[:RECORD_KEEP_LIMIT]
        evicted = records[RECORD_KEEP_LIMIT:]
        database = self._record_database()
        if database is not None and database.has_records():
            loaded = database.load_records(RECORD_KEEP_LIMIT)
            self._prune_orphan_media_sidecars(loaded)
            return loaded

        compacted: List[Dict[str, Any]] = []
        for index, item in enumerate(retained):
            prepared = copy.deepcopy(item)
            prepared.setdefault("id", f"legacy-{index + 1}")
            sidecar = self._write_media_sidecar(str(prepared["id"]), prepared)
            self._remove_media_sources(prepared)
            if sidecar:
                prepared["media_sources_file"] = sidecar
            compacted.append(compact_generation_record(redact_generation_record(prepared)))
        if database is not None and compacted:
            database.replace_records(compacted)
            self._backup_legacy_records(data)
        if evicted:
            # Loading used to trim only the in-memory list, leaving stale rows
            # in generation_records.json until a later write happened.
            if database is None:
                save_json_file(self.records_path, {"records": compacted})
            generated_dir = str(getattr(self, "generated_dir", "") or "")
            if generated_dir:
                safe_delete_relative_files(
                    generated_dir,
                    collect_unreferenced_record_cache_paths(evicted, compacted),
                )
        self._prune_orphan_media_sidecars(compacted)
        return compacted

    def _persist_records(self) -> None:
        with self._records_lock:
            evicted_records = self._records[RECORD_KEEP_LIMIT:]
            self._records = [
                compact_generation_record(redact_generation_record(item))
                for item in self._records[:RECORD_KEEP_LIMIT]
                if isinstance(item, dict)
            ]
            retained_records = list(self._records)
            database = self._record_database()
            if database is not None:
                database.replace_records(self._records)
            else:
                save_json_file(self.records_path, {"records": self._records})
        if evicted_records:
            safe_delete_relative_files(
                self.generated_dir,
                collect_unreferenced_record_cache_paths(evicted_records, retained_records),
            )
        self._schedule_media_sources_prune(retained_records)


    def _record_task(self, record: Dict[str, Any]) -> None:
        payload = copy.deepcopy(record)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._commit_generation_records(payload)
            return
        commit_task = loop.create_task(asyncio.to_thread(self._commit_generation_records, payload))
        # Keep a short-lived handle for queue tasks.  The web task runner can
        # await these commits before publishing its terminal status, avoiding a
        # visible window where a completed task has no linked record yet.
        task_id = str(payload.get("task_id") or "").strip()
        if task_id:
            pending = getattr(self, "_pending_record_commits", None)
            if pending is None:
                pending = {}
                self._pending_record_commits = pending
            pending.setdefault(task_id, set()).add(commit_task)

            def remove_done(_future: Any, *, tid: str = task_id, handle: Any = commit_task) -> None:
                bucket = pending.get(tid)
                if not bucket:
                    return
                bucket.discard(handle)
                if not bucket:
                    pending.pop(tid, None)

            commit_task.add_done_callback(remove_done)

    async def _wait_for_record_commits(self, task_id: str, timeout: float = 2.0) -> None:
        """Wait briefly for records produced by one queue task to be persisted."""
        tid = str(task_id or "").strip()
        if not tid:
            return
        pending = getattr(self, "_pending_record_commits", None)
        if not isinstance(pending, dict):
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout or 0.0))
        while True:
            futures = list(pending.get(tid) or ())
            if not futures:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(
                    asyncio.gather(*futures, return_exceptions=True),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                return

    async def _mark_task_records_delivery(
        self,
        task_id: str,
        *,
        delivered: bool,
        error: str = "",
        paths: Optional[Iterable[str]] = None,
    ) -> int:
        """Wait for a task's record writes, then persist transport outcome."""
        await self._wait_for_record_commits(task_id)
        return self._update_generation_records_delivery(
            task_id,
            delivered=delivered,
            error=error,
            paths=paths,
        )

    def _commit_generation_records(self, record: Dict[str, Any]) -> None:
        # One generated image per monitor row. Batch/concurrency must not pile shots together.
        for piece in split_generation_record_images(record):
            self._commit_generation_record(piece)

    def _commit_generation_record(self, record: Dict[str, Any]) -> None:
        stale_cache_paths: List[str] = []
        # A generation task can produce more than one retained record (batch
        # images are split below).  Keep the task link on every resulting row
        # and backfill the task with the concrete record IDs after persistence.
        task_id = str(
            record.get("task_id")
            or (
                record.get("request_data", {}).get("task_id")
                if isinstance(record.get("request_data"), Mapping)
                else ""
            )
            or ""
        ).strip()
        committed_record_id = ""
        response_data = record.get("response_data")
        if "attempts" not in record and isinstance(response_data, Mapping):
            record["attempts"] = list(response_data.get("attempts") or [])
        # Enrich failure fields for monitor list/detail (also backfills empty used_model).
        try:
            from ..core.error_classify import summarize_generation_failures

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
            self._record_seq += 1
            record.setdefault("id", f"{int(time.time() * 1000)}-{self._record_seq}")
            record["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            sidecar = self._write_media_sidecar(str(record["id"]), record)
            stored_record = copy.deepcopy(record)
            self._remove_media_sources(stored_record)
            if sidecar:
                stored_record["media_sources_file"] = sidecar
            stored_record = compact_generation_record(redact_generation_record(stored_record))
            self._records.insert(0, stored_record)
            evicted_records = self._records[RECORD_KEEP_LIMIT:]
            if evicted_records:
                del self._records[RECORD_KEEP_LIMIT:]
                stale_cache_paths = collect_unreferenced_record_cache_paths(evicted_records, self._records)
            self._persist_records()
            self._delete_media_sidecars(evicted_records)
            committed_record_id = str(record.get("id") or "").strip()
        if stale_cache_paths:
            safe_delete_relative_files(self.generated_dir, stale_cache_paths)
        if task_id and committed_record_id:
            self._link_generation_record_to_task(task_id, committed_record_id)

    def _link_generation_record_to_task(self, task_id: str, record_id: str) -> None:
        """Backfill task -> record links without making records depend on tasks."""
        tid = str(task_id or "").strip()
        rid = str(record_id or "").strip()
        if not tid or not rid:
            return
        task_lock = getattr(self, "_web_task_lock", None)
        tasks = getattr(self, "_web_tasks", None)
        if task_lock is None or not isinstance(tasks, dict):
            return
        try:
            with task_lock:
                task = tasks.get(tid)
                if not isinstance(task, dict):
                    return
                record_ids = task.get("record_ids")
                if not isinstance(record_ids, list):
                    record_ids = []
                record_ids = [str(item).strip() for item in record_ids if str(item).strip()]
                if rid not in record_ids:
                    record_ids.append(rid)
                task["record_ids"] = record_ids[:200]
                task.setdefault("record_id", record_ids[0] if record_ids else rid)
                result = task.get("result")
                if isinstance(result, dict):
                    result_ids = result.get("record_ids")
                    if not isinstance(result_ids, list):
                        result_ids = []
                    result_ids = [str(item).strip() for item in result_ids if str(item).strip()]
                    if rid not in result_ids:
                        result_ids.append(rid)
                    result["record_ids"] = result_ids[:200]
                    result.setdefault("record_id", result_ids[0] if result_ids else rid)
                now = time.time()
                task["updated_ts"] = now
                task["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
                persist = getattr(self, "_persist_web_tasks_locked", None)
                if callable(persist):
                    persist()
        except Exception:
            # A missing/retired task must never make record persistence fail.
            return

    def _update_generation_records_delivery(
        self,
        task_id: str,
        *,
        delivered: bool,
        error: str = "",
        paths: Optional[Iterable[str]] = None,
    ) -> int:
        """Mark records produced by a task after the outbound send finishes.

        Generation and transport are separate lifecycle stages.  Records are
        written as soon as the provider returns, while chat delivery happens
        afterwards; this small update keeps the monitor honest when delivery
        succeeds or fails without rewriting media sidecars.
        """
        tid = str(task_id or "").strip()
        if not tid:
            return 0
        safe_error = redact_sensitive_text(str(error or ""))[:800]
        path_filter = set()
        for raw_path in paths or ():
            text = str(raw_path or "").strip()
            if not text:
                continue
            try:
                path_filter.add(
                    self._cache_relative_path(text)
                    if os.path.isabs(text)
                    else os.path.normpath(text)
                )
            except Exception:
                path_filter.add(text)
        changed = 0
        records_lock = getattr(self, "_records_lock", None)
        records = getattr(self, "_records", None)
        if records_lock is None or not isinstance(records, list):
            return 0
        with records_lock:
            matches = [
                record
                for record in self._records
                if isinstance(record, dict)
                and str(record.get("task_id") or "").strip() == tid
                and (
                    not path_filter
                    or bool(
                        path_filter.intersection(
                            {
                                str(item).strip()
                                for key in ("generated_image_paths", "generated_video_paths")
                                for item in (record.get(key) if isinstance(record.get(key), list) else [])
                                if str(item).strip()
                            }
                        )
                    )
                )
            ]
            if not matches:
                return 0
            for record in matches:
                previous_delivery_error = str(record.get("delivery_error") or "")
                record["generation_success"] = True
                record["delivery_success"] = bool(delivered)
                record["delivery_failed"] = not delivered
                if delivered:
                    if record.get("status") == "delivery_failed":
                        record["status"] = "succeeded" if record.get("success") else "failed"
                    record.pop("delivery_error", None)
                    if previous_delivery_error and record.get("error") == previous_delivery_error:
                        record["error"] = ""
                else:
                    record["status"] = "delivery_failed"
                    record["success"] = False
                    if safe_error:
                        record["delivery_error"] = safe_error
                        # Surface transport failures in list views as well as
                        # the detail-only delivery_error field.
                        record["error"] = safe_error
                response = record.get("response_data")
                if isinstance(response, dict):
                    previous_response_error = str(response.get("delivery_error") or "")
                    response["generation_success"] = True
                    response["delivery_success"] = bool(delivered)
                    response["delivery_failed"] = not delivered
                    if safe_error and not delivered:
                        response["delivery_error"] = safe_error
                        response["error"] = safe_error
                    elif delivered and previous_response_error and response.get("error") == previous_response_error:
                        response["error"] = ""
                    response["status"] = record.get("status")
                changed += 1
            self._persist_records()
        return changed

    def get_recent_records(self, *, summary: bool = False) -> List[Dict[str, Any]]:
        with self._records_lock:
            # Avoid deepcopy of fat nested blobs on every list poll.
            records = [dict(item) if isinstance(item, dict) else item for item in self._records[:RECORD_KEEP_LIMIT]]
        if summary:
            return [
                redact_generation_record(summarize_record_for_list(self._enrich_record_for_web(item)))
                for item in records
                if isinstance(item, dict)
            ]
        return [
            redact_generation_record(self._enrich_record_for_web(item))
            for item in records
            if isinstance(item, dict)
        ]

    def get_generation_metrics(self, *, window_seconds: Optional[int] = None) -> Dict[str, Any]:
        """Return redacted aggregate metrics from retained generation records."""
        with self._records_lock:
            records = [dict(item) for item in self._records if isinstance(item, dict)]
        return build_generation_metrics(records, window_seconds=window_seconds)

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
                    # Web handlers redact the response according to its use. In
                    # particular, detail responses must remove inline Base64
                    # before JSON serialization, while copy actions need the
                    # original media source on demand.
                    detail = self._enrich_record_for_web(self._attach_media_sidecar(copy.deepcopy(record)))
                    detail["task_summary"] = self._record_task_summary(detail)
                    return detail
        raise ValueError("记录不存在或已清理")

    def _record_task_summary(self, record: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Return a small, redacted snapshot of the task linked to a record."""
        task_id = str(record.get("task_id") or "").strip()
        if not task_id:
            return None
        task_lock = getattr(self, "_web_task_lock", None)
        tasks = getattr(self, "_web_tasks", None)
        if task_lock is None or not isinstance(tasks, dict):
            return None
        try:
            with task_lock:
                task = copy.deepcopy(tasks.get(task_id))
        except Exception:
            return None
        if not isinstance(task, Mapping):
            return None
        request = task.get("request_data") if isinstance(task.get("request_data"), Mapping) else {}
        result = task.get("result") if isinstance(task.get("result"), Mapping) else {}
        media_type = str(task.get("media_type") or request.get("media_type") or "").strip().lower()
        if media_type not in {"image", "video"}:
            kind = str(request.get("kind") or "").strip().lower()
            source = str(task.get("source") or "").strip().lower()
            media_type = "video" if kind == "video" or "视频" in kind or "video" in source else "image"
        linked = task.get("record_ids")
        if not isinstance(linked, list):
            linked = result.get("record_ids") if isinstance(result.get("record_ids"), list) else []
        linked_ids = list(dict.fromkeys(str(item).strip() for item in linked if str(item).strip()))[:200]
        if not linked_ids and task_id == str(record.get("task_id") or "").strip():
            linked_ids = [str(record.get("id") or "").strip()] if str(record.get("id") or "").strip() else []
        try:
            from ..tasks.task_views import task_source_label

            source_label = task_source_label(task)
        except Exception:
            source_label = str(task.get("source") or "未知来源")
        requested = task.get("requested_count") or request.get("requested_count") or request.get("count") or 1
        completed = task.get("completed_count")
        if completed is None:
            completed = result.get("completed_count") or 0
        succeeded = task.get("succeeded_count")
        if succeeded is None:
            succeeded = result.get("succeeded_count") or 0
        failed = task.get("failed_count")
        if failed is None:
            failed = result.get("failed_count") or 0
        return redact_sensitive_data(
            {
                "task_id": task_id,
                "status": str(task.get("status") or "unknown"),
                "source": redact_sensitive_text(str(task.get("source") or ""))[:120],
                "source_label": redact_sensitive_text(str(source_label or "未知来源"))[:120],
                "media_type": media_type,
                "generation_stage": redact_sensitive_text(str(task.get("generation_stage") or ""))[:80],
                "generation_stage_label": redact_sensitive_text(str(task.get("generation_stage_label") or ""))[:120],
                "requested_count": requested,
                "completed_count": completed,
                "succeeded_count": succeeded,
                "failed_count": failed,
                "record_ids": linked_ids,
            }
        )

    def _enrich_record_for_web(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Backfill failure fields for monitor without rewriting disk."""
        if not isinstance(record, dict):
            return record
        # Historical rows may predate the explicit route fields. Infer them
        # for web consumers without rewriting the stored record.
        request_data = record.get("request_data") if isinstance(record.get("request_data"), Mapping) else {}
        channel = str(record.get("channel") or request_data.get("channel") or "").strip()
        model = str(record.get("model") or request_data.get("model") or "").strip()
        attempts_for_route = record.get("attempts") if isinstance(record.get("attempts"), list) else []
        response_for_route = record.get("response_data")
        if not attempts_for_route and isinstance(response_for_route, Mapping):
            attempts_for_route = response_for_route.get("attempts") if isinstance(response_for_route.get("attempts"), list) else []
        for attempt in reversed(attempts_for_route):
            if not isinstance(attempt, Mapping):
                continue
            if attempt.get("success") or not channel or not model:
                channel = channel or str(attempt.get("channel") or "").strip()
                model = model or str(attempt.get("model") or "").strip()
            if channel and model and attempt.get("success"):
                break
        if (not channel or not model) and record.get("used_model"):
            label_channel, separator, label_model = str(record.get("used_model") or "").partition("/")
            if separator:
                channel = channel or label_channel.strip()
                model = model or label_model.strip()
            else:
                model = model or str(record.get("used_model") or "").strip()
        if channel:
            record["channel"] = channel
        if model:
            record["model"] = model
        if isinstance(request_data, Mapping) and (channel or model):
            request_copy = dict(request_data)
            if channel:
                request_copy.setdefault("channel", channel)
            if model:
                request_copy.setdefault("model", model)
            record["request_data"] = request_copy
        try:
            from ..core.error_classify import summarize_generation_failures

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
            self._delete_media_sidecars(records)
        safe_delete_relative_files(self.generated_dir, collect_record_cache_paths(records))
        return count

    def update_record_asset_metadata(self, record_id: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Update favorite/pin/tags/note without rewriting media sidecars."""
        target_id = str(record_id or "").strip()
        if not target_id:
            raise ValueError("记录 ID 不能为空")
        patch = dict(payload) if isinstance(payload, Mapping) else {}
        with self._records_lock:
            target = next((item for item in self._records if str(item.get("id") or "") == target_id), None)
            if target is None:
                raise ValueError("记录不存在或已清理")
            database = self._record_database()
            if database is not None:
                result = database.update_metadata(
                    target_id,
                    favorite=self._coerce_optional_bool(patch.get("favorite")) if "favorite" in patch else self._coerce_optional_bool(patch.get("is_favorite")),
                    pinned=self._coerce_optional_bool(patch.get("pinned")) if "pinned" in patch else self._coerce_optional_bool(patch.get("is_pinned")),
                    tags=patch.get("tags") if "tags" in patch else None,
                    note=patch.get("note") if "note" in patch else patch.get("asset_note") if "asset_note" in patch else None,
                )
                target.update(result)
            else:
                if "favorite" in patch or "is_favorite" in patch:
                    target["favorite"] = bool(self._coerce_optional_bool(patch.get("favorite", patch.get("is_favorite"))))
                if "pinned" in patch or "is_pinned" in patch:
                    target["pinned"] = bool(self._coerce_optional_bool(patch.get("pinned", patch.get("is_pinned"))))
                if "tags" in patch:
                    raw_tags = patch.get("tags")
                    if isinstance(raw_tags, str):
                        raw_tags = raw_tags.replace("，", ",").split(",")
                    target["tags"] = list(dict.fromkeys(str(item).strip() for item in (raw_tags or []) if str(item).strip()))[:30]
                if "note" in patch or "asset_note" in patch:
                    target["note"] = str(patch.get("note", patch.get("asset_note")) or "").strip()[:2000]
                self._persist_records()
                result = {
                    "id": target_id,
                    "favorite": bool(target.get("favorite")),
                    "pinned": bool(target.get("pinned")),
                    "tags": list(target.get("tags") or []),
                    "note": str(target.get("note") or ""),
                }
            return result

    def get_record_reuse_payload(self, record_id: str) -> Dict[str, Any]:
        record = self.get_record_for_web(record_id)
        request_data = record.get("request_data") if isinstance(record.get("request_data"), dict) else {}
        prompt = str(record.get("original_prompt") or record.get("request_prompt") or record.get("prompt") or "")
        paths = record.get("generated_image_paths") if isinstance(record.get("generated_image_paths"), list) else []
        return {
            "record_id": str(record.get("id") or record_id),
            "media_type": str(record.get("media_type") or "image"),
            "prompt": prompt,
            "original_prompt": prompt,
            "request_data": {
                "aspect_ratio": request_data.get("aspect_ratio") or "",
                "resolution": request_data.get("resolution") or "",
                "reference_image_count": request_data.get("reference_image_count") or 0,
            },
            "generated_image_paths": [str(path) for path in paths if str(path).strip()],
            "favorite": bool(record.get("favorite")),
            "pinned": bool(record.get("pinned")),
            "tags": list(record.get("tags") or []),
            "note": str(record.get("note") or ""),
        }

    def query_asset_records(
        self,
        *,
        favorite: Optional[bool] = None,
        pinned: Optional[bool] = None,
        tag: str = "",
        source: str = "",
        model: str = "",
        media_type: str = "",
        success: Optional[bool] = None,
        keyword: str = "",
        start_time: str = "",
        end_time: str = "",
        offset: int = 0,
        limit: int = RECORD_KEEP_LIMIT,
        sort: str = "recent",
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """Filter the lightweight record index for the asset library."""
        # The durable record index currently retains at most 1000 rows; keep
        # the query contract explicit instead of implying an unbounded scan.
        records = self.get_recent_records(summary=True)[:ASSET_QUERY_SCAN_LIMIT]
        source_text = str(source or "").strip().lower()
        model_text = str(model or "").strip().lower()
        tag_text = str(tag or "").strip().lower()
        media_text = str(media_type or "").strip().lower()
        keyword_text = str(keyword or "").strip().lower()
        start_text = str(start_time or "").strip()
        end_text = str(end_time or "").strip()
        if len(end_text) == 10:
            end_text += " 23:59:59"

        filtered: List[Dict[str, Any]] = []
        for record in records:
            if favorite is not None and bool(record.get("favorite")) is not bool(favorite):
                continue
            if pinned is not None and bool(record.get("pinned")) is not bool(pinned):
                continue
            record_media_type = str(record.get("media_type") or "").strip().lower()
            if not record_media_type:
                image_paths = record.get("generated_image_paths") if isinstance(record.get("generated_image_paths"), list) else []
                video_paths = record.get("generated_video_paths") if isinstance(record.get("generated_video_paths"), list) else []
                record_media_type = "video" if video_paths and not image_paths else "image"
            if media_text and record_media_type != media_text:
                continue
            if success is not None and bool(record.get("success")) is not bool(success):
                continue
            if tag_text and tag_text not in {str(item).strip().lower() for item in (record.get("tags") or [])}:
                continue
            if source_text:
                haystack = " ".join(
                    str(record.get(key) or "")
                    for key in ("source", "source_label", "group_id", "user_id")
                ).lower()
                if source_text not in haystack:
                    continue
            if model_text and model_text not in str(record.get("used_model") or "").lower():
                continue
            if keyword_text:
                haystack = " ".join(
                    str(record.get(key) or "")
                    for key in (
                        "source", "source_label", "used_model", "error", "failure_reason",
                        "original_prompt", "request_prompt", "group_id", "user_id", "note",
                    )
                ).lower()
                if keyword_text not in haystack:
                    continue
            record_time = str(record.get("time") or "")
            if start_text and record_time < start_text:
                continue
            if end_text and record_time > end_text:
                continue
            filtered.append(record)

        sort_text = str(sort or "recent").strip().lower()
        if sort_text in {"priority", "pinned", "favorite"}:
            filtered.sort(
                key=lambda item: (
                    bool(item.get("pinned")),
                    bool(item.get("favorite")),
                    str(item.get("time") or ""),
                ),
                reverse=True,
            )
        elif sort_text in {"oldest", "asc"}:
            filtered.reverse()

        total = len(records)
        filtered_count = len(filtered)
        safe_offset = max(0, int(offset or 0))
        safe_limit = max(1, min(int(limit or RECORD_KEEP_LIMIT), RECORD_KEEP_LIMIT))
        return filtered[safe_offset : safe_offset + safe_limit], {
            "total": total,
            "filtered": filtered_count,
            "offset": safe_offset,
            "limit": safe_limit,
        }

    def get_asset_records(
        self,
        *,
        favorite: Optional[bool] = None,
        pinned: Optional[bool] = None,
        tag: str = "",
    ) -> List[Dict[str, Any]]:
        records, _ = self.query_asset_records(
            favorite=favorite,
            pinned=pinned,
            tag=tag,
            limit=RECORD_KEEP_LIMIT,
        )
        return records

    def list_asset_tags(self) -> List[Dict[str, Any]]:
        """Return tag suggestions with counts for the asset library."""
        counts: Dict[str, int] = {}
        for record in self.get_recent_records(summary=True):
            for raw_tag in record.get("tags") or []:
                tag = str(raw_tag or "").strip()
                if tag:
                    counts[tag] = counts.get(tag, 0) + 1
        return [
            {"tag": tag, "count": count}
            for tag, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
        ]

    def export_asset_metadata(self, record_ids: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
        """Export redacted asset rows; media origins and secrets stay excluded."""
        rows, _ = self.query_asset_records(limit=RECORD_KEEP_LIMIT, sort="recent")
        ids = self._normalize_record_ids(record_ids) if record_ids is not None else []
        if ids:
            by_id = {str(row.get("id") or ""): row for row in rows}
            rows = [by_id[item] for item in ids if item in by_id]
        return {
            "format": "selfie-image-asset-metadata",
            "version": 1,
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "count": len(rows),
            "records": rows,
        }

    def import_asset_metadata(self, payload: Any) -> Dict[str, Any]:
        """Restore metadata onto existing records without creating new rows."""
        if isinstance(payload, Mapping):
            records = payload.get("records")
        else:
            records = payload
        if not isinstance(records, list):
            raise ValueError("records 必须是数组")
        if not records:
            raise ValueError("导入文件没有资产记录")
        if len(records) > RECORD_KEEP_LIMIT:
            raise ValueError(f"单次最多导入 {RECORD_KEEP_LIMIT} 条资产")
        updated: List[str] = []
        missing: List[str] = []
        invalid: List[str] = []
        for item in records:
            if not isinstance(item, Mapping):
                invalid.append("")
                continue
            record_id = str(item.get("id") or "").strip()
            if not record_id:
                invalid.append("")
                continue
            patch = {
                key: item[key]
                for key in ("favorite", "is_favorite", "pinned", "is_pinned", "tags", "note", "asset_note")
                if key in item
            }
            if not patch:
                invalid.append(record_id)
                continue
            try:
                self.update_record_asset_metadata(record_id, patch)
            except ValueError:
                missing.append(record_id)
            else:
                updated.append(record_id)
        if not updated and (missing or invalid):
            raise ValueError("导入记录均不存在或没有可恢复的元数据")
        return {
            "updated": updated,
            "missing": missing,
            "invalid": invalid,
            "updated_count": len(updated),
        }

    @staticmethod
    def _normalize_record_ids(record_ids: Iterable[Any]) -> List[str]:
        if isinstance(record_ids, (str, bytes)):
            values = [record_ids]
        else:
            values = list(record_ids or [])
        return list(dict.fromkeys(str(item or "").strip() for item in values if str(item or "").strip()))

    def update_records_asset_metadata(self, record_ids: Iterable[Any], payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Apply one metadata patch to several records without touching media sidecars."""
        ids = self._normalize_record_ids(record_ids)
        if not ids:
            raise ValueError("至少选择一条资产")
        if len(ids) > RECORD_KEEP_LIMIT:
            raise ValueError(f"单次最多操作 {RECORD_KEEP_LIMIT} 条资产")
        patch = dict(payload) if isinstance(payload, Mapping) else {}
        updated: List[str] = []
        missing: List[str] = []
        for record_id in ids:
            try:
                self.update_record_asset_metadata(record_id, patch)
            except ValueError:
                missing.append(record_id)
            else:
                updated.append(record_id)
        if not updated and missing:
            raise ValueError("所选资产不存在或已清理")
        return {"updated": updated, "missing": missing, "updated_count": len(updated)}

    def delete_records(self, record_ids: Iterable[Any]) -> Dict[str, Any]:
        """Delete selected records and only their now-unreferenced cache files."""
        ids = self._normalize_record_ids(record_ids)
        if not ids:
            raise ValueError("至少选择一条资产")
        if len(ids) > RECORD_KEEP_LIMIT:
            raise ValueError(f"单次最多删除 {RECORD_KEEP_LIMIT} 条资产")
        selected: List[Dict[str, Any]] = []
        missing: List[str] = []
        with self._records_lock:
            selected_ids = set(ids)
            for record_id in ids:
                match = next((item for item in self._records if str(item.get("id") or "") == record_id), None)
                if match is None:
                    missing.append(record_id)
                else:
                    selected.append(copy.deepcopy(match))
            if not selected:
                raise ValueError("所选资产不存在或已清理")
            self._records = [item for item in self._records if str(item.get("id") or "") not in selected_ids]
            database = self._record_database()
            if database is not None:
                database.delete_records([str(item.get("id") or "") for item in selected])
            else:
                self._persist_records()
            self._delete_media_sidecars(selected)
            stale_paths = collect_unreferenced_record_cache_paths(selected, self._records)
        safe_delete_relative_files(self.generated_dir, stale_paths)
        return {"deleted": [str(item.get("id") or "") for item in selected], "missing": missing, "deleted_count": len(selected)}

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
        total, _ = self._cache_stats()
        return total

    def _cache_stats(self) -> Tuple[int, int]:
        """Return total bytes and regular-file count for the unified media cache."""
        total = 0
        count = 0
        for root, _, files in os.walk(self.generated_dir):
            for name in files:
                path = os.path.join(root, name)
                try:
                    total += os.path.getsize(path)
                    count += 1
                except OSError:
                    pass
        return total, count

    def _asset_protected_cache_paths(self) -> List[str]:
        """Keep media referenced by pinned/favorite assets during cache GC."""
        with self._records_lock:
            protected_records = [
                item
                for item in self._records
                if isinstance(item, Mapping) and (item.get("favorite") or item.get("pinned"))
            ]
        return collect_record_cache_paths(protected_records)

    def _cache_cleanup_plan(self, protected_paths: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        """Build the shared count-and-size cleanup plan without deleting files."""
        limit_bytes = max(10, int(getattr(self.config, "image_cache_limit_mb", 200) or 200)) * 1024 * 1024
        limit_count = max(10, int(getattr(self.config, "image_cache_limit_count", 100) or 100))
        total_bytes, total_count = self._cache_stats()
        with self._records_lock:
            records = [dict(item) for item in self._records if isinstance(item, Mapping)]
        referenced_paths = collect_record_cache_paths(records)
        record_timestamps: Dict[str, float] = {}
        for record in records:
            timestamp = _record_timestamp(record)
            if timestamp <= 0:
                continue
            for path in collect_record_cache_paths([record]):
                record_timestamps[path] = max(timestamp, record_timestamps.get(path, 0.0))
        # Orphans are still reclaimed first. Referenced media is ordered by
        # its record time, not file mtime, so an old request file cannot jump
        # ahead of newer records merely because it was written earlier.
        protected = [
            *list(protected_paths or []),
            *self._asset_protected_cache_paths(),
        ]
        candidates = collect_cache_cleanup_candidates(
            self.generated_dir,
            protected,
            referenced_paths,
            record_timestamps,
        )
        planned: List[Dict[str, Any]] = []
        remaining_bytes = total_bytes
        remaining_count = total_count
        for path in candidates:
            if remaining_bytes <= limit_bytes and remaining_count <= limit_count:
                break
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            planned.append({"path": self._cache_relative_path(path), "size_bytes": size})
            remaining_bytes = max(0, remaining_bytes - size)
            remaining_count = max(0, remaining_count - 1)
        # The preview token lets the UI confirm the exact plan it showed to
        # the user.  It contains only relative paths, sizes and limits; no
        # prompt or other sensitive record data is exposed.
        token_payload = {
            "limit_bytes": limit_bytes,
            "limit_count": limit_count,
            "total_bytes": total_bytes,
            "total_count": total_count,
            "would_delete": planned,
        }
        plan_token = hashlib.sha256(
            json.dumps(token_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:32]
        return {
            "limit_bytes": limit_bytes,
            "limit_count": limit_count,
            "total_bytes": total_bytes,
            "total_count": total_count,
            "remaining_bytes": remaining_bytes,
            "remaining_count": remaining_count,
            "would_delete_bytes": total_bytes - remaining_bytes,
            "would_delete_count": total_count - remaining_count,
            "would_delete": planned,
            "plan_token": plan_token,
        }

    def _cleanup_image_cache_if_needed(
        self,
        protected_paths: Optional[Iterable[str]] = None,
        *,
        plan: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a cleanup plan, or build one for automatic garbage collection.

        Web confirmation passes the already validated preview plan so the
        execution cannot silently switch to a newly computed candidate set.
        Automatic callers omit ``plan`` and retain the existing behavior.
        """
        cleanup_plan = dict(plan) if isinstance(plan, Mapping) else self._cache_cleanup_plan(protected_paths)
        planned_items = cleanup_plan.get("would_delete") or []
        if isinstance(plan, Mapping):
            # A token authenticates the preview, but it cannot prevent an
            # external writer from replacing a file between validation and
            # deletion. Preflight every confirmed item so execution only
            # starts when the signed path/size snapshot is still present.
            for item in planned_items:
                if not isinstance(item, Mapping):
                    raise ValueError("缓存清理计划无效，请重新预览")
                rel_path = str(item.get("path") or "")
                try:
                    absolute = self._cache_absolute_path(rel_path)
                    current_size = os.path.getsize(absolute)
                except (OSError, ValueError):
                    raise ValueError("缓存内容已变化，请重新预览后再确认清理") from None
                try:
                    expected_size = int(item.get("size_bytes"))
                except (TypeError, ValueError):
                    raise ValueError("缓存清理计划无效，请重新预览") from None
                if current_size != expected_size:
                    raise ValueError("缓存内容已变化，请重新预览后再确认清理")
        deleted: List[str] = []
        deleted_bytes = 0
        for item in planned_items:
            rel_path = str(item.get("path") or "")
            try:
                item_size = max(0, int(item.get("size_bytes") or 0))
            except (TypeError, ValueError):
                item_size = 0
            try:
                os.remove(self._cache_absolute_path(rel_path))
                deleted.append(rel_path)
                deleted_bytes += item_size
            except (OSError, ValueError):
                if isinstance(plan, Mapping):
                    raise ValueError("缓存内容已变化，请重新预览后再确认清理") from None
                continue
        total_bytes, total_count = self._cache_stats()
        return {
            "limit_bytes": cleanup_plan.get("limit_bytes", 0),
            "limit_count": cleanup_plan.get("limit_count", 0),
            "initial_total_bytes": cleanup_plan.get("total_bytes", total_bytes),
            "initial_total_count": cleanup_plan.get("total_count", total_count),
            "total_bytes": total_bytes,
            "total_count": total_count,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "deleted_bytes": deleted_bytes,
            "would_delete_count": cleanup_plan.get("would_delete_count", len(planned_items)),
            "would_delete_bytes": cleanup_plan.get(
                "would_delete_bytes",
                sum(
                    max(0, int(item.get("size_bytes") or 0))
                    for item in planned_items
                    if isinstance(item, Mapping)
                ),
            ),
        }

    def get_cache_cleanup_preview(self, protected_paths: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        """Return a dry-run cache cleanup plan without deleting files."""
        return self._cache_cleanup_plan(protected_paths)

    def cleanup_image_cache_from_web(self, *, confirm: bool = False, plan_token: str = "") -> Dict[str, Any]:
        """Preview or execute the current orphan-cache cleanup plan.

        Manual cleanup is deliberately limited to the same protected,
        count/size-aware candidates used by automatic GC.  A confirmation
        token prevents a stale UI preview from silently deleting a different
        set of files after the cache changed.
        """
        plan = self._cache_cleanup_plan()
        if not confirm:
            return {"requires_confirmation": bool(plan.get("would_delete")), "preview": plan}
        token = str(plan_token or "").strip()
        # An empty token is accepted only when the current plan is empty. A
        # stale non-empty token must still be rejected if the cache changed
        # enough that no deletion is currently needed.
        current_token = str(plan.get("plan_token") or "")
        if token and token != current_token:
            raise ValueError("缓存内容已变化，请重新预览后再确认清理")
        if plan.get("would_delete") and token != current_token:
            raise ValueError("缓存内容已变化，请重新预览后再确认清理")
        # Execute the exact plan whose token was validated above. Do not
        # recalculate candidates after confirmation, otherwise a concurrent
        # cache change could delete files that were not shown in the preview.
        result = self._cleanup_image_cache_if_needed(plan=plan)
        return {"confirmed": True, "preview": plan, "result": result}

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
            result: Dict[str, Any] = {}
            for name, raw_state in self._channel_health.items():
                state = dict(raw_state)
                attempts = max(0, int(state.get("attempts") or 0))
                successes = max(0, int(state.get("successes") or 0))
                elapsed = max(0.0, float(state.get("total_elapsed_seconds") or 0))
                cooldown_until = max(0.0, float(state.get("cooldown_until") or 0))
                remaining = max(0, int(round(cooldown_until - now)))
                state["success_rate"] = round(successes / attempts, 4) if attempts else None
                state["average_elapsed_seconds"] = round(elapsed / attempts, 2) if attempts else 0.0
                state["cooling_down"] = remaining > 0
                state["cooldown_remaining_seconds"] = remaining
                result[name] = state
            return result

    def _select_healthy_generation_targets(self, targets: Iterable[Any]) -> Tuple[List[Any], List[Dict[str, Any]]]:
        """Skip temporarily unhealthy channels while preserving a no-dead-end fallback."""
        items = list(targets or [])
        if not items:
            return [], []
        now = time.time()
        lock = getattr(self, "_channel_health_lock", None)
        health = getattr(self, "_channel_health", {})
        if lock is None or not isinstance(health, dict):
            states: Dict[str, Dict[str, Any]] = {}
        else:
            with lock:
                states = {name: dict(state) for name, state in health.items()}

        available: List[Any] = []
        cooling: List[Tuple[int, Any, float]] = []
        for index, target in enumerate(items):
            channel = str(getattr(target, "channel_name", "") or "").strip()
            cooldown_until = float((states.get(channel) or {}).get("cooldown_until") or 0)
            if channel and cooldown_until > now:
                cooling.append((index, target, cooldown_until))
            else:
                available.append(target)

        selected = available
        if not selected and cooling:
            # All channels are cooling down. Keep the channel that recovers
            # first so a transient failure never makes generation unavailable.
            _, earliest_target, _ = min(cooling, key=lambda item: (item[2], item[0]))
            earliest_channel = str(getattr(earliest_target, "channel_name", "") or "").strip()
            selected = [
                target
                for _, target, _ in cooling
                if str(getattr(target, "channel_name", "") or "").strip() == earliest_channel
            ]

        selected_ids = {id(target) for target in selected}
        skipped: List[Dict[str, Any]] = []
        for _, target, cooldown_until in cooling:
            if id(target) in selected_ids:
                continue
            remaining = max(1, int(round(cooldown_until - now)))
            label = redact_sensitive_text(str(getattr(target, "label", "") or ""))
            skipped.append(
                {
                    "channel": redact_sensitive_text(str(getattr(target, "channel_name", "") or "")),
                    "model": redact_sensitive_text(str(getattr(target, "model", "") or "")),
                    "label": label,
                    "success": False,
                    "error": f"渠道冷却中，已跳过（约 {remaining}s 后恢复）",
                    "error_user_message": f"渠道冷却中，已跳过（约 {remaining}s 后恢复）",
                    "error_category": "cooldown",
                    "retryable": True,
                    "retry_action": "next_model",
                    "cooldown_remaining_seconds": remaining,
                }
            )
        return selected, skipped

    def _record_channel_health(self, attempts: Iterable[Mapping[str, Any]]) -> None:
        now = time.time()
        for attempt in attempts:
            channel = str(attempt.get("channel") or "").strip()
            if not channel:
                continue
            category = str(attempt.get("error_category") or "").strip()
            success = bool(attempt.get("success"))
            with self._channel_health_lock:
                state = self._channel_health.setdefault(
                    channel,
                    {
                        "consecutive_failures": 0,
                        "last_error_category": "",
                        "attempts": 0,
                        "successes": 0,
                        "failures": 0,
                        "total_elapsed_seconds": 0.0,
                    },
                )
                state["attempts"] = int(state.get("attempts") or 0) + 1
                try:
                    state["total_elapsed_seconds"] = float(state.get("total_elapsed_seconds") or 0) + max(
                        0.0, float(attempt.get("elapsed_seconds") or 0)
                    )
                except (TypeError, ValueError):
                    pass
                if success:
                    state["successes"] = int(state.get("successes") or 0) + 1
                    state["consecutive_failures"] = 0
                    state["last_success_ts"] = now
                    state.pop("cooldown_until", None)
                    continue
                state["failures"] = int(state.get("failures") or 0) + 1
                state["last_error_category"] = category
                state["last_error"] = redact_sensitive_text(
                    str(attempt.get("error_user_message") or attempt.get("error") or "")
                )[:240]
                state["last_error_ts"] = now
                if category not in CHANNEL_TRANSIENT_ERROR_CATEGORIES:
                    state["consecutive_failures"] = 0
                    continue
                state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
                if state["consecutive_failures"] >= CHANNEL_COOLDOWN_FAILURE_THRESHOLD:
                    state["cooldown_until"] = now + CHANNEL_COOLDOWN_SECONDS
