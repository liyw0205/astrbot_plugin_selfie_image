"""Named collections of generation record IDs.

Collections are an index only: deleting a collection never deletes records or
media.  The store is intentionally independent from the generation SQLite
schema so it can be removed or rolled back without a data migration.
"""

from __future__ import annotations

import copy
import os
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List

from ..core.utils import load_json_file, save_json_file

MAX_COLLECTIONS = 100
MAX_ITEMS = 500


class AssetCollectionStore:
    def __init__(self, data_dir: str) -> None:
        self.path = os.path.join(str(data_dir), "asset_collections.json")
        self._lock = threading.RLock()
        self._collections: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        raw = load_json_file(self.path)
        rows = raw.get("collections") if isinstance(raw, Mapping) else []
        if isinstance(rows, Mapping):
            rows = list(rows.values())
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, Mapping):
                continue
            cid = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            if not cid or not name:
                continue
            self._collections[cid] = {
                "id": cid[:128],
                "name": name[:80],
                "note": str(item.get("note") or "")[:500],
                "record_ids": list(dict.fromkeys(str(x).strip() for x in (item.get("record_ids") or []) if str(x).strip()))[:MAX_ITEMS],
                "created_at": str(item.get("created_at") or ""),
                "updated_at": str(item.get("updated_at") or ""),
            }

    def _persist(self) -> None:
        rows = sorted(self._collections.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True)[:MAX_COLLECTIONS]
        save_json_file(self.path, {"version": 1, "collections": rows, "updated_at": time.time()})

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(sorted(self._collections.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True))

    def get(self, collection_id: str) -> Dict[str, Any]:
        with self._lock:
            item = self._collections.get(str(collection_id or "").strip())
            if not item:
                raise ValueError("资产集合不存在")
            return copy.deepcopy(item)

    def create(self, name: str, *, note: str = "", record_ids: Iterable[Any] = ()) -> Dict[str, Any]:
        text = str(name or "").strip()[:80]
        if not text:
            raise ValueError("集合名称不能为空")
        with self._lock:
            if len(self._collections) >= MAX_COLLECTIONS:
                raise ValueError(f"资产集合最多 {MAX_COLLECTIONS} 个")
            if any(str(item.get("name") or "").casefold() == text.casefold() for item in self._collections.values()):
                raise ValueError("集合名称已存在")
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            item = {"id": f"collection-{uuid.uuid4().hex[:16]}", "name": text, "note": str(note or "")[:500], "record_ids": self._normalize_ids(record_ids), "created_at": now, "updated_at": now}
            self._collections[item["id"]] = item
            self._persist()
            return copy.deepcopy(item)

    def update(self, collection_id: str, *, name: Any = None, note: Any = None) -> Dict[str, Any]:
        with self._lock:
            item = self._collections.get(str(collection_id or "").strip())
            if not item:
                raise ValueError("资产集合不存在")
            if name is not None:
                text = str(name or "").strip()[:80]
                if not text:
                    raise ValueError("集合名称不能为空")
                item["name"] = text
            if note is not None:
                item["note"] = str(note or "")[:500]
            item["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._persist()
            return copy.deepcopy(item)

    def delete(self, collection_id: str) -> Dict[str, Any]:
        with self._lock:
            cid = str(collection_id or "").strip()
            if cid not in self._collections:
                raise ValueError("资产集合不存在")
            self._collections.pop(cid, None)
            self._persist()
            return {"deleted": True, "id": cid}

    @staticmethod
    def _normalize_ids(record_ids: Iterable[Any]) -> List[str]:
        return list(dict.fromkeys(str(item).strip() for item in (record_ids or ()) if str(item).strip()))[:MAX_ITEMS]

    def add(self, collection_id: str, record_ids: Iterable[Any]) -> Dict[str, Any]:
        with self._lock:
            item = self._collections.get(str(collection_id or "").strip())
            if not item:
                raise ValueError("资产集合不存在")
            item["record_ids"] = self._normalize_ids([*(item.get("record_ids") or []), *self._normalize_ids(record_ids)])
            item["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._persist()
            return copy.deepcopy(item)

    def remove(self, collection_id: str, record_ids: Iterable[Any]) -> Dict[str, Any]:
        with self._lock:
            item = self._collections.get(str(collection_id or "").strip())
            if not item:
                raise ValueError("资产集合不存在")
            removed = {str(x).strip() for x in (record_ids or ()) if str(x).strip()}
            item["record_ids"] = [x for x in item.get("record_ids") or [] if x not in removed]
            item["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._persist()
            return copy.deepcopy(item)

    def export(self) -> Dict[str, Any]:
        return {"format": "selfie-image-asset-collections", "version": 1, "collections": self.list()}

    def import_data(self, payload: Any, *, preview: bool = False) -> Dict[str, Any]:
        rows = payload.get("collections") if isinstance(payload, Mapping) else []
        if not isinstance(rows, list):
            raise ValueError("collections 必须是数组")
        normalized = []
        for row in rows[:MAX_COLLECTIONS]:
            if not isinstance(row, Mapping) or not str(row.get("name") or "").strip():
                continue
            normalized.append({"name": str(row.get("name") or "").strip()[:80], "note": str(row.get("note") or "")[:500], "record_ids": self._normalize_ids(row.get("record_ids") or [])})
        if preview:
            return {"preview": True, "count": len(normalized), "collections": normalized}
        imported = 0
        for row in normalized:
            try:
                self.create(row["name"], note=row["note"], record_ids=row["record_ids"])
                imported += 1
            except ValueError:
                continue
        return {"preview": False, "imported": imported, "skipped": len(normalized) - imported}
