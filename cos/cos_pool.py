"""Persistent favorites and administrator-managed COS look pools."""

from __future__ import annotations

import copy
import os
from typing import Any, Dict, Iterable, List, Mapping, Optional

from ..core.utils import load_json_file, save_json_file


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
        self._data: Dict[str, Any] = {"favorites": [], "custom": []}
        self.load()

    def load(self) -> None:
        raw = load_json_file(self.file_path)
        if not isinstance(raw, Mapping):
            raw = {}
        favorites = raw.get("favorites")
        custom = raw.get("custom")
        self._data = {
            "favorites": list(dict.fromkeys(str(item).strip() for item in (favorites or []) if str(item).strip()))[:1000]
            if isinstance(favorites, (list, tuple, set))
            else [],
            "custom": self._normalize_custom(custom if isinstance(custom, (list, tuple)) else []),
        }

    @staticmethod
    def _normalize_item(item: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        item_id = str(item.get("id") or item.get("name") or "").strip()
        title = str(item.get("title") or item.get("name") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if not item_id or not title or not prompt:
            return None
        return {
            "id": item_id[:120],
            "title": title[:200],
            "prompt": prompt[:30000],
            "source": "custom",
            "tags": [str(tag).strip()[:40] for tag in (item.get("tags") or []) if str(tag).strip()][:20]
            if isinstance(item.get("tags"), (list, tuple, set))
            else [],
            "compatibility": copy.deepcopy(item.get("compatibility") or {}),
        }

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
        save_json_file(self.file_path, copy.deepcopy(self._data))

    def list_favorites(self) -> List[str]:
        return list(self._data["favorites"])

    def is_favorite(self, look_id: str) -> bool:
        return str(look_id or "").strip() in set(self._data["favorites"])

    def set_favorite(self, look_id: str, enabled: bool) -> Dict[str, Any]:
        value = str(look_id or "").strip()
        if not value:
            raise ValueError("COS 套装 ID 不能为空")
        favorites = list(self._data["favorites"])
        if enabled and value not in favorites:
            favorites.append(value)
        elif not enabled:
            favorites = [item for item in favorites if item != value]
        self._data["favorites"] = favorites[:1000]
        self.save()
        return {"id": value, "favorite": value in self._data["favorites"]}

    def list_custom(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._data["custom"])

    def save_custom(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_item(payload)
        if normalized is None:
            raise ValueError("自定义 COS 必须包含 id、title 和 prompt")
        custom = [item for item in self._data["custom"] if item["id"] != normalized["id"]]
        custom.insert(0, normalized)
        self._data["custom"] = custom[: self.MAX_CUSTOM]
        self.save()
        return copy.deepcopy(normalized)

    def delete_custom(self, look_id: str) -> Dict[str, Any]:
        value = str(look_id or "").strip()
        before = len(self._data["custom"])
        self._data["custom"] = [item for item in self._data["custom"] if item["id"] != value]
        if len(self._data["custom"]) == before:
            raise ValueError("自定义 COS 不存在")
        self.save()
        return {"id": value, "deleted": True}

    def export_data(self) -> Dict[str, Any]:
        return {"version": 1, "favorites": self.list_favorites(), "custom": self.list_custom()}

    def import_data(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("COS 池导入内容必须是对象")
        favorites = payload.get("favorites")
        if isinstance(favorites, (list, tuple, set)):
            self._data["favorites"] = list(dict.fromkeys(str(item).strip() for item in favorites if str(item).strip()))[:1000]
        custom = payload.get("custom")
        if isinstance(custom, (list, tuple)):
            self._data["custom"] = self._normalize_custom(custom)
        self.save()
        return self.export_data()

    def filter_ids(self, ids: Iterable[str], *, favorites_only: bool = False) -> List[str]:
        values = [str(item).strip() for item in ids if str(item).strip()]
        if favorites_only:
            favorites = set(self._data["favorites"])
            values = [item for item in values if item in favorites]
        return list(dict.fromkeys(values))
