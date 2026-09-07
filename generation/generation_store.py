"""Generation record persistence, cache management, and channel health."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import re
import shutil
import time
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .generation_records import build_generation_metrics, composition_metadata
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
    safe_delete_relative_files,
    save_image_bytes,
    save_json_file,
    split_generation_record_images,
    summarize_record_for_list,
)

RECORD_KEEP_LIMIT = 1000


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
        sources = self._record_media_sources(record)
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
        return self._record_media_sources(data) if isinstance(data, Mapping) else {"generated_image_sources": [], "video_source": ""}

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
            return database.load_records(RECORD_KEEP_LIMIT)

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
        if stale_cache_paths:
            safe_delete_relative_files(self.generated_dir, stale_cache_paths)

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
                    return self._enrich_record_for_web(self._attach_media_sidecar(copy.deepcopy(record)))
        raise ValueError("记录不存在或已清理")

    def _enrich_record_for_web(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Backfill failure fields for monitor without rewriting disk."""
        if not isinstance(record, dict):
            return record
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
        records = self.get_recent_records(summary=True)
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
        total = 0
        for root, _, files in os.walk(self.generated_dir):
            for name in files:
                path = os.path.join(root, name)
                try:
                    total += os.path.getsize(path)
                except OSError:
                    pass
        return total

    def _asset_protected_cache_paths(self) -> List[str]:
        """Keep media referenced by pinned/favorite assets during cache GC."""
        with self._records_lock:
            protected_records = [
                item
                for item in self._records
                if isinstance(item, Mapping) and (item.get("favorite") or item.get("pinned"))
            ]
        return collect_record_cache_paths(protected_records)

    def _cleanup_image_cache_if_needed(self, protected_paths: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        limit = max(10, int(self.config.image_cache_limit_mb or 100)) * 1024 * 1024
        total = self._cache_size_bytes()
        deleted: List[str] = []
        if total <= limit:
            return {"limit_bytes": limit, "total_bytes": total, "deleted": deleted}
        with self._records_lock:
            referenced_paths = collect_record_cache_paths(self._records)
        protected = list(protected_paths or []) + self._asset_protected_cache_paths()
        candidates = collect_cache_cleanup_candidates(self.generated_dir, protected, referenced_paths)
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
        protected = list(protected_paths or []) + self._asset_protected_cache_paths()
        candidates = collect_cache_cleanup_candidates(self.generated_dir, protected, referenced_paths)
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
        with self._channel_health_lock:
            return {
                name: dict(state)
                for name, state in self._channel_health.items()
            }

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
                    state["last_success_ts"] = now
                    continue
                if category not in {"network", "server", "timeout_create", "timeout_poll"}:
                    continue
                state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
                state["last_error_category"] = category
                state["last_error_ts"] = now
