"""SQLite persistence for generation record indexes and asset metadata."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional


class RecordDatabase:
    """Small SQLite-backed index with one JSON payload per retained record.

    Media origins are deliberately not part of ``payload``. The generation
    store keeps those in a separate sidecar directory and joins them only for
    explicit detail/copy requests.
    """

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(str(path))
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS generation_records (
                    id TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL,
                    time TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    favorite INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    tags TEXT NOT NULL DEFAULT '[]',
                    note TEXT NOT NULL DEFAULT '',
                    created_ts REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_generation_records_seq
                    ON generation_records(seq DESC);
                CREATE INDEX IF NOT EXISTS idx_generation_records_asset
                    ON generation_records(pinned DESC, favorite DESC, seq DESC);
                """
            )

    @staticmethod
    def _metadata(record: Mapping[str, Any]) -> tuple[int, int, str, str]:
        def as_bool(value: Any) -> bool:
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on", "是", "开启"}
            return bool(value)

        favorite = int(as_bool(record.get("favorite") if "favorite" in record else record.get("is_favorite")))
        pinned = int(as_bool(record.get("pinned") if "pinned" in record else record.get("is_pinned")))
        raw_tags = record.get("tags")
        if isinstance(raw_tags, str):
            tags = [item.strip() for item in raw_tags.replace("，", ",").split(",") if item.strip()]
        elif isinstance(raw_tags, (list, tuple, set)):
            tags = [str(item).strip() for item in raw_tags if str(item).strip()]
        else:
            tags = []
        tags = list(dict.fromkeys(tags))[:30]
        note = str(record.get("note") or record.get("asset_note") or "").strip()[:2000]
        return favorite, pinned, json.dumps(tags, ensure_ascii=False), note

    @staticmethod
    def _decode_tags(value: Any) -> list[str]:
        try:
            decoded = json.loads(str(value or "[]"))
        except (TypeError, ValueError):
            decoded = []
        return [str(item).strip() for item in decoded if str(item).strip()][:30] if isinstance(decoded, list) else []

    def has_records(self) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT 1 FROM generation_records LIMIT 1").fetchone()
        return row is not None

    def load_records(self, limit: int = 1000) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 1000), 10000))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id, seq, time, payload, favorite, pinned, tags, note, created_ts "
                "FROM generation_records ORDER BY seq ASC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        records: List[Dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            payload.setdefault("id", row["id"])
            if row["time"]:
                payload.setdefault("time", row["time"])
            payload["favorite"] = bool(row["favorite"])
            payload["pinned"] = bool(row["pinned"])
            payload["tags"] = self._decode_tags(row["tags"])
            payload["note"] = str(row["note"] or "")
            if row["created_ts"]:
                payload.setdefault("created_ts", row["created_ts"])
            records.append(payload)
        return records

    def replace_records(self, records: Iterable[Mapping[str, Any]]) -> None:
        rows = []
        now = time.time()
        for seq, record in enumerate(records):
            if not isinstance(record, Mapping):
                continue
            record_id = str(record.get("id") or "").strip()
            if not record_id:
                continue
            payload = dict(record)
            favorite, pinned, tags, note = self._metadata(payload)
            payload.pop("favorite", None)
            payload.pop("is_favorite", None)
            payload.pop("pinned", None)
            payload.pop("is_pinned", None)
            payload.pop("tags", None)
            payload.pop("note", None)
            payload.pop("asset_note", None)
            rows.append(
                (
                    record_id,
                    len(rows),
                    str(record.get("time") or ""),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    favorite,
                    pinned,
                    tags,
                    note,
                    float(record.get("created_ts") or now),
                )
            )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM generation_records")
            connection.executemany(
                "INSERT INTO generation_records "
                "(id, seq, time, payload, favorite, pinned, tags, note, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.commit()

    def update_metadata(
        self,
        record_id: str,
        *,
        favorite: Optional[bool] = None,
        pinned: Optional[bool] = None,
        tags: Optional[Iterable[Any]] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        target = str(record_id or "").strip()
        if not target:
            raise ValueError("记录 ID 不能为空")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT favorite, pinned, tags, note FROM generation_records WHERE id = ?",
                (target,),
            ).fetchone()
            if row is None:
                raise ValueError("记录不存在或已清理")
            current_tags = self._decode_tags(row["tags"])
            next_favorite = int(row["favorite"] if favorite is None else bool(favorite))
            next_pinned = int(row["pinned"] if pinned is None else bool(pinned))
            if tags is None:
                next_tags = current_tags
            elif isinstance(tags, str):
                next_tags = [item.strip() for item in tags.replace("，", ",").split(",") if item.strip()]
            else:
                next_tags = [str(item).strip() for item in tags if str(item).strip()]
            next_tags = list(dict.fromkeys(next_tags))[:30]
            next_note = str(row["note"] or "") if note is None else str(note or "").strip()[:2000]
            connection.execute(
                "UPDATE generation_records SET favorite = ?, pinned = ?, tags = ?, note = ? WHERE id = ?",
                (
                    next_favorite,
                    next_pinned,
                    json.dumps(next_tags, ensure_ascii=False),
                    next_note,
                    target,
                ),
            )
            return {
                "id": target,
                "favorite": bool(next_favorite),
                "pinned": bool(next_pinned),
                "tags": next_tags,
                "note": next_note,
            }

    def delete_records(self, record_ids: Iterable[Any]) -> int:
        """Delete a bounded set of records in one transaction."""
        ids = list(dict.fromkeys(str(item or "").strip() for item in record_ids if str(item or "").strip()))
        if not ids:
            return 0
        with self._lock, self._connect() as connection:
            placeholders = ",".join("?" for _ in ids)
            cursor = connection.execute(
                f"DELETE FROM generation_records WHERE id IN ({placeholders})",
                ids,
            )
            return int(cursor.rowcount or 0)

    def clear(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM generation_records")
