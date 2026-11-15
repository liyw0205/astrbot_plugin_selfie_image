"""Named collections of generation record IDs.

Collections are an index only: deleting a collection never deletes records or
media.  The store is intentionally independent from the generation SQLite
schema so it can be removed or rolled back without a data migration.
"""

from __future__ import annotations

import copy
import hashlib
import os
import secrets
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List

from ..core.utils import load_json_file, save_json_file

MAX_COLLECTIONS = 100
MAX_ITEMS = 500
COLLECTION_UNDO_TTL_SECONDS = 10 * 60
MAX_COLLECTION_UNDO_SNAPSHOTS = 32
MIN_COLLECTION_UNDO_TOKEN_LENGTH = 32


class AssetCollectionStore:
    def __init__(self, data_dir: str) -> None:
        self.path = os.path.join(str(data_dir), "asset_collections.json")
        self._lock = threading.RLock()
        self._collections: Dict[str, Dict[str, Any]] = {}
        # Only the digest is persisted.  The clear token returned by delete()
        # is therefore both short-lived and useless if copied from disk.
        self._undo_snapshots: Dict[str, Dict[str, Any]] = {}
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
            normalized = self._normalize_collection(item)
            if normalized is not None:
                self._collections[normalized["id"]] = normalized

        undo_rows = raw.get("deleted_snapshots") if isinstance(raw, Mapping) else []
        now = time.time()
        for row in undo_rows if isinstance(undo_rows, list) else []:
            if not isinstance(row, Mapping):
                continue
            token_hash = str(row.get("token_hash") or "").strip().lower()
            snapshot = row.get("collection")
            try:
                expires_at = float(row.get("expires_at"))
            except (TypeError, ValueError):
                continue
            if (
                len(token_hash) != 64
                or not all(char in "0123456789abcdef" for char in token_hash)
                or expires_at <= now
            ):
                continue
            normalized = self._normalize_collection(snapshot)
            if normalized is None:
                continue
            self._undo_snapshots[token_hash] = {
                "collection": normalized,
                "expires_at": expires_at,
                "created_at": str(row.get("created_at") or ""),
            }
        self._undo_snapshots = dict(
            sorted(
                self._undo_snapshots.items(),
                key=lambda item: float(item[1].get("expires_at") or 0),
                reverse=True,
            )[:MAX_COLLECTION_UNDO_SNAPSHOTS]
        )

    def _persist(
        self,
        collections: Mapping[str, Mapping[str, Any]] | None = None,
        undo_snapshots: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        active = self._collections if collections is None else collections
        undo = self._undo_snapshots if undo_snapshots is None else undo_snapshots
        rows = sorted(
            (copy.deepcopy(dict(item)) for item in active.values()),
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )[:MAX_COLLECTIONS]
        undo_rows = []
        for token_hash, row in sorted(
            undo.items(), key=lambda item: float(item[1].get("expires_at") or 0), reverse=True
        )[:MAX_COLLECTION_UNDO_SNAPSHOTS]:
            snapshot = self._normalize_collection(row.get("collection"))
            if snapshot is None:
                continue
            undo_rows.append(
                {
                    "token_hash": str(token_hash),
                    "collection": snapshot,
                    "expires_at": float(row.get("expires_at") or 0),
                    "created_at": str(row.get("created_at") or ""),
                }
            )
        # save_json_file writes a temporary file and os.replace()s it, so the
        # collection removal and its undo snapshot share one durable commit.
        save_json_file(
            self.path,
            {
                "version": 2,
                "collections": rows,
                "deleted_snapshots": undo_rows,
                "updated_at": time.time(),
            },
        )

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
            item = self._collections.get(cid)
            if item is None:
                raise ValueError("资产集合不存在")
            token = secrets.token_urlsafe(32)
            token_hash = self._token_digest(token)
            now = time.time()
            expires_at = now + COLLECTION_UNDO_TTL_SECONDS
            next_collections = copy.deepcopy(self._collections)
            next_collections.pop(cid, None)
            next_undo = copy.deepcopy(self._undo_snapshots)
            next_undo[token_hash] = {
                "collection": copy.deepcopy(item),
                "expires_at": expires_at,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            }
            self._prune_undo(next_undo, now=now)
            self._persist(next_collections, next_undo)
            self._collections = next_collections
            self._undo_snapshots = next_undo
            return {
                "deleted": True,
                "id": cid,
                "undo": {
                    "token": token,
                    "expires_at": expires_at,
                    "ttl_seconds": COLLECTION_UNDO_TTL_SECONDS,
                },
            }

    def restore(self, token: str) -> Dict[str, Any]:
        """Restore one deleted collection and consume its token atomically."""
        raw_token = str(token or "").strip()
        if len(raw_token) < MIN_COLLECTION_UNDO_TOKEN_LENGTH or len(raw_token) > 256:
            raise ValueError("撤销令牌无效或已过期")
        token_hash = self._token_digest(raw_token)
        with self._lock:
            snapshot = self._undo_snapshots.get(token_hash)
            now = time.time()
            if snapshot is None or float(snapshot.get("expires_at") or 0) <= now:
                if snapshot is not None:
                    next_undo = copy.deepcopy(self._undo_snapshots)
                    next_undo.pop(token_hash, None)
                    self._persist(self._collections, next_undo)
                    self._undo_snapshots = next_undo
                raise ValueError("撤销令牌无效或已过期")
            item = self._normalize_collection(snapshot.get("collection"))
            if item is None:
                raise ValueError("撤销令牌无效或已过期")
            if item["id"] in self._collections:
                raise ValueError("资产集合已存在，无法恢复")
            if len(self._collections) >= MAX_COLLECTIONS:
                raise ValueError(f"资产集合最多 {MAX_COLLECTIONS} 个，无法恢复")
            if any(
                str(row.get("name") or "").casefold() == item["name"].casefold()
                for row in self._collections.values()
            ):
                raise ValueError("集合名称已存在，无法恢复")
            next_collections = copy.deepcopy(self._collections)
            next_collections[item["id"]] = item
            next_undo = copy.deepcopy(self._undo_snapshots)
            next_undo.pop(token_hash, None)
            self._persist(next_collections, next_undo)
            self._collections = next_collections
            self._undo_snapshots = next_undo
            return {"restored": True, "collection": copy.deepcopy(item)}

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

    @staticmethod
    def _token_digest(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_collection(item: Any) -> Dict[str, Any] | None:
        if not isinstance(item, Mapping):
            return None
        cid = str(item.get("id") or "").strip()[:128]
        name = str(item.get("name") or "").strip()[:80]
        if not cid or not name:
            return None
        return {
            "id": cid,
            "name": name,
            "note": str(item.get("note") or "")[:500],
            "record_ids": list(
                dict.fromkeys(
                    str(x).strip()
                    for x in (item.get("record_ids") or [])
                    if str(x).strip()
                )
            )[:MAX_ITEMS],
            "created_at": str(item.get("created_at") or ""),
            "updated_at": str(item.get("updated_at") or ""),
        }

    @staticmethod
    def _prune_undo(
        snapshots: Dict[str, Dict[str, Any]], *, now: float | None = None
    ) -> None:
        current = time.time() if now is None else float(now)
        for key in list(snapshots):
            try:
                expired = float(snapshots[key].get("expires_at") or 0) <= current
            except (TypeError, ValueError):
                expired = True
            if expired:
                snapshots.pop(key, None)
        if len(snapshots) > MAX_COLLECTION_UNDO_SNAPSHOTS:
            keep = sorted(
                snapshots.items(),
                key=lambda item: float(item[1].get("expires_at") or 0),
                reverse=True,
            )[:MAX_COLLECTION_UNDO_SNAPSHOTS]
            snapshots.clear()
            snapshots.update(keep)

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
