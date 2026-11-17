"""Persistent favorites and administrator-managed COS look pools."""

from __future__ import annotations

import copy
import os
import threading
from typing import Any, Dict, Iterable, List, Mapping, Optional

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
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"favorites": [], "custom": []}
        self._storage_status: Dict[str, Any] = {"ok": True, "status": "missing", "source": self.file_path}
        self._storage_blocked = False
        self.load()

    def load(self) -> None:
        with self._lock:
            result = load_json_file_result(self.file_path)
            self._storage_status = {
                "ok": result.ok,
                "status": result.status,
                "error": result.error,
                "source": self.file_path,
            }
            self._storage_blocked = not result.ok
            if not result.ok:
                return
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
                self.save()

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

    def save(self) -> None:
        with self._lock:
            if self._storage_blocked:
                raise RuntimeError("COS 池存储读取失败，已停止覆盖原文件")
            save_json_file(self.file_path, copy.deepcopy(self._data))
            self._storage_status = {"ok": True, "status": "ok", "error": "", "source": self.file_path}

    def _commit(self, data: Dict[str, Any]) -> None:
        with self._lock:
            previous = self._data
            self._data = copy.deepcopy(data)
            try:
                self.save()
            except Exception:
                self._data = previous
                raise

    def list_favorites(self) -> List[str]:
        return list(self._data["favorites"])

    def is_favorite(self, look_id: str) -> bool:
        return str(look_id or "").strip() in set(self._data["favorites"])

    def set_favorite(self, look_id: str, enabled: bool) -> Dict[str, Any]:
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
            self._commit(candidate)
            return {"id": value, "favorite": value in candidate["favorites"]}

    def list_custom(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._data["custom"])

    def save_custom(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
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
            self._commit(candidate)
            return copy.deepcopy(normalized)

    def delete_custom(self, look_id: str) -> Dict[str, Any]:
        value = str(look_id or "").strip()
        with self._lock:
            before = len(self._data["custom"])
            custom = [item for item in self._data["custom"] if item["id"] != value]
            if len(custom) == before:
                raise ValueError("自定义 COS 不存在")
            candidate = copy.deepcopy(self._data)
            candidate["custom"] = custom
            self._commit(candidate)
            return {"id": value, "deleted": True}

    def export_data(self) -> Dict[str, Any]:
        return {"version": 1, "favorites": self.list_favorites(), "custom": self.list_custom()}

    def import_data(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
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
            self._commit(candidate)
            return {"version": 1, "favorites": list(candidate["favorites"]), "custom": copy.deepcopy(candidate["custom"])}

    def filter_ids(self, ids: Iterable[str], *, favorites_only: bool = False) -> List[str]:
        values = [str(item).strip() for item in ids if str(item).strip()]
        if favorites_only:
            favorites = set(self._data["favorites"])
            values = [item for item in values if item in favorites]
        return list(dict.fromkeys(values))
