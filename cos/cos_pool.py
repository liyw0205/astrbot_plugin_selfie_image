"""Persistent favorites and administrator-managed COS look pools."""

from __future__ import annotations

import copy
import os
import threading
from typing import Any, Dict, Iterable, List, Mapping, Optional

from ..core.persistent_store import (
    PersistentStoreConflictError,
    cross_process_file_lock,
    file_fingerprint,
    read_revision_state,
    write_revision_state,
)
from ..core.utils import load_json_file_result, save_json_file
from .cos_looks import get_cos_look_by_id


class CosPoolStore:
    """Small JSON store for favorites and custom COS outfits.

    Built-in outfits remain immutable.  Custom entries are validated to the
    same public shape as ``cos_looks.list_cos_look_sets`` and can be exported
    without exposing filesystem paths or credentials.
    """

    FILENAME = "cos_pools.json"
    MAX_CUSTOM = 200

    def __init__(self, data_dir: str):
        self.file_path = os.path.join(str(data_dir), self.FILENAME)
        self._lock_path = f"{self.file_path}.lock"
        self._revision_path = f"{self.file_path}.revision.json"
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"favorites": [], "custom": []}
        self._storage_status: Dict[str, Any] = {"ok": True, "status": "missing", "source": self.file_path}
        self._storage_blocked = False
        self._storage_revision = 1
        self._storage_fingerprint: Optional[str] = None
        self.load()

    def load(self) -> None:
        with self._lock:
            with cross_process_file_lock(self._lock_path):
                self._load_locked()

    def _load_locked(self) -> None:
        result = None
        revision = 1
        fingerprint = ""
        changed = False
        for _ in range(2):
            revision, fingerprint, changed = read_revision_state(
                self.file_path,
                self._revision_path,
                current_revision=self._storage_revision,
                current_fingerprint=self._storage_fingerprint,
            )
            result = load_json_file_result(self.file_path)
            if file_fingerprint(self.file_path) == fingerprint:
                break
        if result is None or file_fingerprint(self.file_path) != fingerprint:
            self._storage_blocked = True
            self._storage_status = {
                "ok": False,
                "status": "changed_during_read",
                "error": "COS 池文件读取期间发生变化，已保留当前内存数据",
                "source": self.file_path,
            }
            return
        self._storage_status = {
            "ok": result.ok,
            "status": result.status,
            "error": result.error,
            "source": self.file_path,
        }
        self._storage_blocked = not result.ok
        if not result.ok:
            return
        self._storage_revision = revision
        self._storage_fingerprint = fingerprint
        if changed:
            try:
                write_revision_state(self._revision_path, revision, fingerprint)
            except Exception:
                # The JSON remains readable when the optional sidecar is
                # unavailable; a later successful save reconciles it.
                pass
        raw = result.data
        favorites = raw.get("favorites")
        custom = raw.get("custom")
        raw_custom = custom if isinstance(custom, (list, tuple)) else []
        self._data = {
            "favorites": list(dict.fromkeys(str(item).strip() for item in (favorites or []) if str(item).strip()))[:1000]
            if isinstance(favorites, (list, tuple, set))
            else [],
            # Older versions allowed a custom item to reuse a built-in ID.
            # Drop those records during load so the dashboard cannot render a
            # partial custom row beside the complete built-in row.
            "custom": self._normalize_custom(raw_custom),
        }
        if len(self._data["custom"]) != len(raw_custom):
            self._save_locked()

    def storage_status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._storage_status)

    @staticmethod
    def _is_builtin_id(item_id: str) -> bool:
        return bool(get_cos_look_by_id(item_id))

    @staticmethod
    def _normalize_item(item: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        item_id = str(item.get("id") or item.get("name") or "").strip()
        title = str(item.get("title") or item.get("name") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if not item_id or not title or not prompt or CosPoolStore._is_builtin_id(item_id):
            return None
        mode = str(
            item.get("mode")
            or item.get("cos_mode")
            or item.get("staging_mode")
            or ""
        ).strip().lower()
        special = bool(item.get("special") is True or mode in {"special", "fixed", "standalone"})
        normalized = {
            "id": item_id[:120],
            "title": title[:200],
            "prompt": prompt[:30000],
            "cos_type": str(item.get("cos_type") or item.get("series") or "").strip()[:80],
            "source": "custom",
            "tags": [str(tag).strip()[:40] for tag in (item.get("tags") or []) if str(tag).strip()][:20]
            if isinstance(item.get("tags"), (list, tuple, set))
            else [],
            "compatibility": copy.deepcopy(item.get("compatibility") or {}),
        }
        if special:
            normalized["mode"] = "special"
            normalized["special_camera"] = str(
                item.get("special_camera") or item.get("fixed_camera") or "third"
            ).strip()[:20]
            special_prompt = str(item.get("special_prompt") or "").strip()
            if special_prompt:
                normalized["special_prompt"] = special_prompt[:30000]
        return normalized

    def _normalize_custom(self, items: Iterable[Any]) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for raw in items:
            if not isinstance(raw, Mapping):
                continue
            normalized = self._normalize_item(raw)
            if normalized is None:
                continue
            if any(item["id"] == normalized["id"] for item in result):
                continue
            result.append(normalized)
            if len(result) >= self.MAX_CUSTOM:
                break
        return result

    def save(self, *, expected_revision: Optional[int] = None) -> None:
        with self._lock:
            with cross_process_file_lock(self._lock_path):
                self._save_locked(expected_revision=expected_revision)

    def _save_locked(self, *, expected_revision: Optional[int] = None) -> None:
        if self._storage_blocked:
            raise RuntimeError("COS 池存储读取失败，已停止覆盖原文件")
        disk_revision, disk_fingerprint, changed = read_revision_state(
            self.file_path,
            self._revision_path,
            current_revision=self._storage_revision,
            current_fingerprint=self._storage_fingerprint,
        )
        if changed:
            try:
                write_revision_state(self._revision_path, disk_revision, disk_fingerprint)
            except Exception:
                pass
        if expected_revision is not None and int(expected_revision) != self._storage_revision:
            raise PersistentStoreConflictError(expected_revision, self._storage_revision, "COS 池")
        if self._storage_fingerprint is not None and disk_fingerprint != self._storage_fingerprint:
            raise PersistentStoreConflictError(self._storage_revision, disk_revision, "COS 池")
        if self._storage_fingerprint is not None and self._storage_revision != disk_revision:
            raise PersistentStoreConflictError(self._storage_revision, disk_revision, "COS 池")
        save_json_file(self.file_path, copy.deepcopy(self._data))
        new_fingerprint = file_fingerprint(self.file_path)
        new_revision = max(self._storage_revision, disk_revision, 1) + 1
        try:
            write_revision_state(self._revision_path, new_revision, new_fingerprint)
        except Exception:
            pass
        self._storage_revision = new_revision
        self._storage_fingerprint = new_fingerprint
        self._storage_status = {"ok": True, "status": "ok", "error": "", "source": self.file_path}

    def get_revision(self) -> int:
        with self._lock:
            with cross_process_file_lock(self._lock_path):
                revision, fingerprint, changed = read_revision_state(
                    self.file_path,
                    self._revision_path,
                    current_revision=self._storage_revision,
                    current_fingerprint=self._storage_fingerprint,
                )
                if changed:
                    try:
                        write_revision_state(self._revision_path, revision, fingerprint)
                    except Exception:
                        pass
                return revision

    def _commit(self, data: Dict[str, Any], *, expected_revision: Optional[int] = None) -> None:
        with self._lock:
            previous = self._data
            self._data = copy.deepcopy(data)
            try:
                self.save(expected_revision=expected_revision)
            except Exception:
                self._data = previous
                raise

    def list_favorites(self) -> List[str]:
        with self._lock:
            return list(self._data["favorites"])

    def is_favorite(self, look_id: str) -> bool:
        with self._lock:
            return str(look_id or "").strip() in set(self._data["favorites"])

    def set_favorite(
        self,
        look_id: str,
        enabled: bool,
        *,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        value = str(look_id or "").strip()
        if not value:
            raise ValueError("COS 套装 ID 不能为空")
        with self._lock:
            favorites = list(self._data["favorites"])
            if enabled and value not in favorites:
                favorites.append(value)
            elif not enabled:
                favorites = [item for item in favorites if item != value]
            candidate = copy.deepcopy(self._data)
            candidate["favorites"] = favorites[:1000]
            self._commit(candidate, expected_revision=expected_revision)
            return {"id": value, "favorite": value in candidate["favorites"]}

    def list_custom(self) -> List[Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._data["custom"])

    def save_custom(
        self,
        payload: Mapping[str, Any],
        *,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        item_id = str(payload.get("id") or payload.get("name") or "").strip()
        if self._is_builtin_id(item_id):
            raise ValueError("自定义 COS 不能使用内置套装 ID")
        normalized = self._normalize_item(payload)
        if normalized is None:
            raise ValueError("自定义 COS 必须包含 id、title 和 prompt")
        with self._lock:
            custom = [item for item in self._data["custom"] if item["id"] != normalized["id"]]
            custom.insert(0, normalized)
            candidate = copy.deepcopy(self._data)
            candidate["custom"] = custom[: self.MAX_CUSTOM]
            self._commit(candidate, expected_revision=expected_revision)
            return copy.deepcopy(normalized)

    def delete_custom(
        self,
        look_id: str,
        *,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        value = str(look_id or "").strip()
        with self._lock:
            before = len(self._data["custom"])
            custom = [item for item in self._data["custom"] if item["id"] != value]
            if len(custom) == before:
                raise ValueError("自定义 COS 不存在")
            candidate = copy.deepcopy(self._data)
            candidate["custom"] = custom
            self._commit(candidate, expected_revision=expected_revision)
            return {"id": value, "deleted": True}

    def export_data(self) -> Dict[str, Any]:
        return {"version": 1, "favorites": self.list_favorites(), "custom": self.list_custom()}

    def import_data(
        self,
        payload: Mapping[str, Any],
        *,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("COS 池导入内容必须是对象")
        with self._lock:
            favorites = payload.get("favorites")
            candidate = copy.deepcopy(self._data)
            if isinstance(favorites, (list, tuple, set)):
                candidate["favorites"] = list(dict.fromkeys(str(item).strip() for item in favorites if str(item).strip()))[:1000]
            custom = payload.get("custom")
            if isinstance(custom, (list, tuple)):
                candidate["custom"] = self._normalize_custom(custom)
            self._commit(candidate, expected_revision=expected_revision)
            return {"version": 1, "favorites": list(candidate["favorites"]), "custom": copy.deepcopy(candidate["custom"])}

    def filter_ids(self, ids: Iterable[str], *, favorites_only: bool = False) -> List[str]:
        with self._lock:
            values = [str(item).strip() for item in ids if str(item).strip()]
            if favorites_only:
                favorites = set(self._data["favorites"])
                values = [item for item in values if item in favorites]
            return list(dict.fromkeys(values))
