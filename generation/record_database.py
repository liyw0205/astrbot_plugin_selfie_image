"""SQLite persistence for generation record indexes and asset metadata."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional


class RecordStorageUnavailableError(RuntimeError):
    """A record write was rejected because schema initialization failed."""


class RecordDatabase:
    """Small SQLite-backed index with one JSON payload per retained record.

    Media origins are deliberately not part of ``payload``. The generation
    store keeps those in a separate sidecar directory and joins them only for
    explicit detail/copy requests.
    """

    _MATERIALIZED_SCHEMA_VERSION = 1
    _MATERIALIZED_COLUMNS = {
        "media_type": "TEXT NOT NULL DEFAULT 'image'",
        "status": "TEXT NOT NULL DEFAULT ''",
        "source": "TEXT NOT NULL DEFAULT ''",
        "source_label": "TEXT NOT NULL DEFAULT ''",
        "used_model": "TEXT NOT NULL DEFAULT ''",
        "image_code": "TEXT NOT NULL DEFAULT ''",
        "prompt_digest": "TEXT NOT NULL DEFAULT ''",
    }
    # FTS5 is optional in the SQLite builds used by some Android/Python
    # distributions.  The normal generation_records table remains the source
    # of truth and all queries have a scalar-SQL fallback when this table
    # cannot be created or maintained.
    _FTS_TABLE = "generation_records_fts"
    _FTS_STATE_TABLE = "generation_records_fts_state"
    _FTS_VOCAB_TABLE = "generation_records_fts_vocab"
    _FTS_SCHEMA_VERSION = 2
    _PROMPT_PREFIX_TABLE = "generation_record_prompt_prefix"
    _PROMPT_PREFIX_CHARS = 128

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(str(path))
        self._lock = threading.RLock()
        self._fts5_enabled = False
        started = time.monotonic()
        started_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        self._schema_health = {
            "status": "running",
            "started_at": started_at,
            "finished_at": "",
            "duration_ms": 0.0,
            "migration_applied": False,
            "last_failure_reason": "",
            "error_type": "",
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            self._initialize()
        except Exception as error:
            self._schema_health.update({
                "status": "error",
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "error_type": type(error).__name__,
                "last_failure_reason": str(error)[:400] or "记录数据库初始化失败",
            })
            raise
        self._schema_health.update({
            "status": "ok",
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
            "migration_applied": bool(getattr(self, "_schema_migration_applied", False)),
        })

    def schema_health(self) -> Dict[str, Any]:
        return dict(self._schema_health)

    @classmethod
    def load_records_readonly(cls, path: str, limit: int = 1000) -> Optional[List[Dict[str, Any]]]:
        """Read the stable base schema without attempting migration or writes."""
        safe_limit = max(1, min(int(limit or 1000), 10000))
        uri = Path(os.path.abspath(str(path))).as_uri() + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        except sqlite3.DatabaseError:
            return None
        try:
            connection.row_factory = sqlite3.Row
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(generation_records)").fetchall()
            }
            required = {"id", "seq", "time", "payload", "favorite", "pinned", "tags", "note", "created_ts"}
            if not required <= columns:
                return None
            rows = connection.execute(
                "SELECT id, seq, time, payload, favorite, pinned, tags, note, created_ts "
                "FROM generation_records WHERE pinned != 0 OR favorite != 0 OR id IN ("
                "SELECT id FROM generation_records WHERE pinned = 0 AND favorite = 0 "
                "ORDER BY time DESC, seq DESC, id DESC LIMIT ?) "
                "ORDER BY time DESC, seq DESC, id DESC",
                (safe_limit,),
            ).fetchall()
            return cls._decode_rows(rows)
        except sqlite3.DatabaseError:
            return None
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.create_function("text_lower", 1, lambda value: str(value or "").lower(), deterministic=True)
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            # sqlite3's transaction context commits/rolls back but never closes.
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")
            initial_user_version = int(connection.execute("PRAGMA user_version").fetchone()[0] or 0)
            initial_indexes = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_generation_records_%'"
                ).fetchall()
            }
            prompt_prefix_table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (self._PROMPT_PREFIX_TABLE,),
            ).fetchone() is not None
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
                CREATE INDEX IF NOT EXISTS idx_generation_records_created
                    ON generation_records(time DESC, seq DESC);
                CREATE INDEX IF NOT EXISTS idx_generation_records_normal
                    ON generation_records(time DESC, seq DESC, id DESC)
                    WHERE pinned = 0 AND favorite = 0;
                CREATE INDEX IF NOT EXISTS idx_generation_records_protected
                    ON generation_records(time DESC, seq DESC, id DESC)
                    WHERE pinned != 0 OR favorite != 0;
                """
            )
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {self._PROMPT_PREFIX_TABLE} ("
                "record_id TEXT PRIMARY KEY, prompt_prefix TEXT NOT NULL DEFAULT ''"
                ") WITHOUT ROWID"
            )
            existing_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(generation_records)").fetchall()
            }
            added_columns = False
            for name, definition in self._MATERIALIZED_COLUMNS.items():
                if name in existing_columns:
                    continue
                connection.execute(f"ALTER TABLE generation_records ADD COLUMN {name} {definition}")
                added_columns = True
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_generation_records_media_type
                    ON generation_records(media_type, time DESC, seq DESC);
                CREATE INDEX IF NOT EXISTS idx_generation_records_status
                    ON generation_records(status, time DESC, seq DESC);
                CREATE INDEX IF NOT EXISTS idx_generation_records_source
                    ON generation_records(source, time DESC, seq DESC);
                CREATE INDEX IF NOT EXISTS idx_generation_records_source_label
                    ON generation_records(source_label, time DESC, seq DESC);
                CREATE INDEX IF NOT EXISTS idx_generation_records_used_model
                    ON generation_records(used_model, time DESC, seq DESC);
                CREATE INDEX IF NOT EXISTS idx_generation_records_image_code
                    ON generation_records(image_code);
                CREATE INDEX IF NOT EXISTS idx_generation_records_prompt_digest
                    ON generation_records(prompt_digest);
                CREATE INDEX IF NOT EXISTS idx_generation_records_valid_query
                    ON generation_records(time DESC, seq DESC, id DESC, favorite, pinned, used_model)
                    WHERE json_valid(payload);
                """
            )
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0] or 0)
            if added_columns or user_version < self._MATERIALIZED_SCHEMA_VERSION:
                self._backfill_materialized_fields(connection)
                connection.execute(f"PRAGMA user_version = {self._MATERIALIZED_SCHEMA_VERSION}")
            required_indexes = {
                "idx_generation_records_seq", "idx_generation_records_asset",
                "idx_generation_records_created", "idx_generation_records_normal",
                "idx_generation_records_protected", "idx_generation_records_media_type",
                "idx_generation_records_status", "idx_generation_records_source",
                "idx_generation_records_source_label", "idx_generation_records_used_model",
                "idx_generation_records_image_code", "idx_generation_records_prompt_digest",
                "idx_generation_records_valid_query",
            }
            self._schema_migration_applied = bool(
                added_columns
                or initial_user_version < self._MATERIALIZED_SCHEMA_VERSION
                or not required_indexes <= initial_indexes
                or not prompt_prefix_table_exists
            )
            self._schema_migration_applied = (
                self._ensure_prompt_prefix_projection(connection) or self._schema_migration_applied
            )
            self._initialize_fts(connection, force_rebuild=added_columns)

    @staticmethod
    def _keyword_search_expression() -> str:
        """Share the exact legacy joined text between FTS and scalar search."""
        return " || ' ' || ".join((
            "COALESCE(NULLIF(source, ''), json_extract(payload, '$.source'), '')",
            "COALESCE(NULLIF(source_label, ''), json_extract(payload, '$.source_label'), '')",
            "COALESCE(NULLIF(used_model, ''), json_extract(payload, '$.used_model'), '')",
            "COALESCE(json_extract(payload, '$.error'), '')",
            "COALESCE(json_extract(payload, '$.failure_reason'), '')",
            "COALESCE(json_extract(payload, '$.original_prompt'), '')",
            "COALESCE(json_extract(payload, '$.request_prompt'), '')",
            "COALESCE(json_extract(payload, '$.prompt'), '')",
            "COALESCE(json_extract(payload, '$.group_id'), '')",
            "COALESCE(json_extract(payload, '$.user_id'), '')",
            "COALESCE(NULLIF(image_code, ''), json_extract(payload, '$.image_code'), '')",
            "note",
        ))

    @staticmethod
    def _keyword_prompt_search_expression() -> str:
        """Keep the common prompt-only check ahead of the full legacy text."""
        return " || ' ' || ".join(
            f"COALESCE(json_extract(payload, '$.{field}'), '')"
            for field in ("original_prompt", "request_prompt", "prompt")
        )

    @classmethod
    def _keyword_prompt_prefix_expression(cls) -> str:
        return (
            "text_lower(substr("
            f"{cls._keyword_prompt_search_expression()}, 1, {cls._PROMPT_PREFIX_CHARS}"
            "))"
        )

    @classmethod
    def _keyword_search_predicate(cls) -> str:
        """Short-circuit prompt matches, preserving cross-field fallback semantics."""
        return (
            "CASE WHEN retained.id IN (SELECT record_id FROM "
            f"{cls._PROMPT_PREFIX_TABLE} WHERE instr(prompt_prefix, ?) > 0) THEN 1 "
            "WHEN instr(text_lower("
            f"{cls._keyword_prompt_search_expression()}"
            "), ?) > 0 THEN 1 "
            "WHEN instr(text_lower("
            f"{cls._keyword_search_expression()}"
            "), ?) > 0 THEN 1 ELSE 0 END = 1"
        )

    @classmethod
    def _rebuild_prompt_prefix_projection(cls, connection: sqlite3.Connection) -> None:
        connection.execute(f"DELETE FROM {cls._PROMPT_PREFIX_TABLE}")
        connection.execute(
            f"INSERT INTO {cls._PROMPT_PREFIX_TABLE} (record_id, prompt_prefix) "
            "SELECT id, CASE WHEN json_valid(payload) THEN "
            f"{cls._keyword_prompt_prefix_expression()} ELSE '' END "
            "FROM generation_records"
        )

    @classmethod
    def _ensure_prompt_prefix_projection(cls, connection: sqlite3.Connection) -> bool:
        record_count = int(connection.execute("SELECT COUNT(*) FROM generation_records").fetchone()[0])
        prefix_count = int(
            connection.execute(f"SELECT COUNT(*) FROM {cls._PROMPT_PREFIX_TABLE}").fetchone()[0]
        )
        if record_count != prefix_count:
            cls._rebuild_prompt_prefix_projection(connection)
            return True
        return False

    @classmethod
    def _sync_prompt_prefix_record(cls, connection: sqlite3.Connection, record_id: str) -> None:
        connection.execute(f"DELETE FROM {cls._PROMPT_PREFIX_TABLE} WHERE record_id = ?", (record_id,))
        connection.execute(
            f"INSERT INTO {cls._PROMPT_PREFIX_TABLE} (record_id, prompt_prefix) "
            "SELECT id, CASE WHEN json_valid(payload) THEN "
            f"{cls._keyword_prompt_prefix_expression()} ELSE '' END "
            "FROM generation_records WHERE id = ?",
            (record_id,),
        )

    @classmethod
    def _delete_prompt_prefix_records(
        cls, connection: sqlite3.Connection, record_ids: Iterable[str],
    ) -> None:
        ids = [str(item) for item in record_ids if str(item)]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        connection.execute(
            f"DELETE FROM {cls._PROMPT_PREFIX_TABLE} WHERE record_id IN ({placeholders})", ids
        )

    @staticmethod
    def _fts_trigrams(keyword: str) -> list[str]:
        # SQLite's FTS query parser treats NUL as a terminator.  Short text
        # cannot produce a trigram; both cases retain the scalar path.
        if len(keyword) < 3 or "\x00" in keyword:
            return []
        last = len(keyword) - 3
        return list(dict.fromkeys(keyword[start:start + 3] for start in (0, last // 2, last)))

    @staticmethod
    def _quote_fts_term(term: str) -> str:
        return '"' + term.replace('"', '""') + '"'

    @staticmethod
    def _fts_match_query(keyword: str) -> str:
        """Bound candidate matching work; scalar SQL confirms the substring."""
        trigrams = RecordDatabase._fts_trigrams(keyword)
        if not trigrams:
            return ""
        # A true substring contains every sampled trigram. Matching a whole
        # long phrase needlessly merges positional postings for repeated text.
        return " AND ".join(RecordDatabase._quote_fts_term(term) for term in trigrams)

    def _fts_selective_match_query(
        self, connection: sqlite3.Connection, keyword: str, retained_count: int,
    ) -> str:
        """Use the rarest sampled trigram only when it meaningfully narrows rows."""
        trigrams = self._fts_trigrams(keyword)
        if not trigrams:
            return ""
        placeholders = ",".join("?" for _ in trigrams)
        rows = connection.execute(
            f"SELECT term, doc FROM {self._FTS_VOCAB_TABLE} WHERE term IN ({placeholders})",
            trigrams,
        ).fetchall()
        known_terms = {str(row["term"]) for row in rows}
        for term in trigrams:
            if term not in known_terms:
                # MATCH handles tokenizer folding even when the vocabulary
                # stores a different spelling, and avoids scanning absent text.
                return self._quote_fts_term(term)
        term, documents = min(rows, key=lambda row: int(row["doc"]))
        # FTS setup/merging is not worthwhile when most rows still need the
        # authoritative scalar check. This is a cost heuristic, not a limit.
        threshold = max(8, int(retained_count * 0.1))
        if int(documents) > threshold:
            return ""
        return self._quote_fts_term(str(term))

    def _initialize_fts(self, connection: sqlite3.Connection, *, force_rebuild: bool = False) -> None:
        """Create/rebuild the optional FTS5 projection for an existing DB."""
        self._fts5_enabled = False
        connection.execute("SAVEPOINT initialize_record_fts")
        try:
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {self._FTS_STATE_TABLE} ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
                "schema_version INTEGER NOT NULL, valid INTEGER NOT NULL)"
            )
            state = connection.execute(
                f"SELECT schema_version, valid FROM {self._FTS_STATE_TABLE} WHERE singleton = 1"
            ).fetchone()
            if state is None or int(state["schema_version"]) != self._FTS_SCHEMA_VERSION or not int(state["valid"]):
                force_rebuild = True
            existing = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (self._FTS_TABLE,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    f"CREATE VIRTUAL TABLE {self._FTS_TABLE} USING fts5("
                    "record_id UNINDEXED, search_text, tokenize='trigram')"
                )
                force_rebuild = True
            else:
                schema = str(existing["sql"] or "")
                # Standard unicode61 tokenization misses interior substrings
                # and CJK fragments.  Only trigram tables can be an exact
                # candidate index for the existing keyword search contract.
                if not re.search(r"\bUSING\s+fts5\b", schema, flags=re.IGNORECASE) or not re.search(
                    r"\btokenize\s*=\s*(['\"])\s*trigram(?:\s+[^'\"]*)?\1",
                    schema,
                    flags=re.IGNORECASE,
                ):
                    self._invalidate_fts(connection)
                    connection.execute("RELEASE SAVEPOINT initialize_record_fts")
                    return
                columns = {
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({self._FTS_TABLE})").fetchall()
                }
                if not {"record_id", "search_text"} <= columns:
                    self._invalidate_fts(connection)
                    connection.execute("RELEASE SAVEPOINT initialize_record_fts")
                    return
            connection.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {self._FTS_VOCAB_TABLE} "
                f"USING fts5vocab({self._FTS_TABLE}, 'row')"
            )
            self._fts5_enabled = True
            if force_rebuild:
                self._rebuild_fts(connection)
            else:
                # Base rows and the projection commit together.  This also
                # detects rows restored by legacy writers outside this class.
                base_count = int(connection.execute("SELECT COUNT(*) FROM generation_records").fetchone()[0])
                fts_count = int(connection.execute(f"SELECT COUNT(*) FROM {self._FTS_TABLE}").fetchone()[0])
                if base_count != fts_count:
                    self._rebuild_fts(connection)
            connection.execute("RELEASE SAVEPOINT initialize_record_fts")
        except sqlite3.DatabaseError:
            # SQLite without the FTS5 module is supported.  Do not fail plugin
            # startup; query_records will retain its scalar fallback.
            connection.execute("ROLLBACK TO SAVEPOINT initialize_record_fts")
            connection.execute("RELEASE SAVEPOINT initialize_record_fts")
            self._invalidate_fts(connection)

    def _rebuild_fts(self, connection: sqlite3.Connection) -> None:
        if not self._fts5_enabled:
            return
        connection.execute(f"DELETE FROM {self._FTS_TABLE}")
        # SQL builds the exact old joined string, including empty fields;
        # text_lower uses Python's Unicode lower().
        connection.execute(
            f"INSERT INTO {self._FTS_TABLE} (rowid, record_id, search_text) "
            "SELECT rowid, id, CASE WHEN json_valid(payload) THEN "
            f"text_lower({self._keyword_search_expression()}) ELSE '' END "
            "FROM generation_records"
        )
        connection.execute(
            f"INSERT INTO {self._FTS_STATE_TABLE} (singleton, schema_version, valid) VALUES (1, ?, 1) "
            "ON CONFLICT(singleton) DO UPDATE SET schema_version = excluded.schema_version, valid = 1",
            (self._FTS_SCHEMA_VERSION,),
        )

    def _invalidate_fts(self, connection: sqlite3.Connection) -> None:
        """Remember that this projection needs rebuilding on the next startup."""
        self._fts5_enabled = False
        try:
            connection.execute(
                f"INSERT INTO {self._FTS_STATE_TABLE} (singleton, schema_version, valid) VALUES (1, ?, 0) "
                "ON CONFLICT(singleton) DO UPDATE SET valid = 0",
                (self._FTS_SCHEMA_VERSION,),
            )
        except sqlite3.DatabaseError:
            pass

    def _sync_fts_record(self, connection: sqlite3.Connection, record_id: str) -> None:
        if not self._fts5_enabled:
            self._invalidate_fts(connection)
            return
        try:
            row = connection.execute(
                "SELECT rowid FROM generation_records WHERE id = ?",
                (record_id,),
            ).fetchone()
            if row is not None:
                # FTS5's rowid lookup avoids scanning the UNINDEXED record_id
                # column each time a retained record changes.
                connection.execute(f"DELETE FROM {self._FTS_TABLE} WHERE rowid = ?", (row[0],))
                connection.execute(
                    f"INSERT INTO {self._FTS_TABLE} (rowid, record_id, search_text) "
                    "SELECT rowid, id, CASE WHEN json_valid(payload) THEN "
                    f"text_lower({self._keyword_search_expression()}) ELSE '' END "
                    "FROM generation_records WHERE id = ?",
                    (record_id,),
                )
        except sqlite3.DatabaseError:
            self._invalidate_fts(connection)

    def _delete_fts_records(self, connection: sqlite3.Connection, record_ids: Iterable[str]) -> None:
        if not self._fts5_enabled:
            self._invalidate_fts(connection)
            return
        ids = [str(item) for item in record_ids if str(item)]
        if not ids:
            return
        try:
            placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"DELETE FROM {self._FTS_TABLE} WHERE rowid IN "
                f"(SELECT rowid FROM generation_records WHERE id IN ({placeholders}))", ids
            )
        except sqlite3.DatabaseError:
            self._invalidate_fts(connection)

    @staticmethod
    def _materialized_fields(record: Mapping[str, Any]) -> tuple[str, str, str, str, str, str, str]:
        """Extract the small, frequently filtered fields from a record.

        These values are intentionally duplicated outside the JSON payload so
        list queries do not have to parse every payload. The payload remains
        the source of truth and is retained for forward/backward compatibility.
        """

        def text(value: Any, *, lower: bool = False) -> str:
            result = str(value or "").strip()
            return result.lower() if lower else result

        response_data = record.get("response_data")
        response = response_data if isinstance(response_data, Mapping) else {}
        media_type = text(record.get("media_type"), lower=True) or "image"
        status = text(record.get("status")) or text(response.get("status"))
        source = text(record.get("source"))
        source_label = text(record.get("source_label"))
        used_model = text(record.get("used_model"))
        image_code = text(record.get("image_code"))
        prompt_digest = text(record.get("prompt_digest"))
        if not prompt_digest:
            prompt = next(
                (
                    text(record.get(key))
                    for key in ("prompt", "request_prompt", "original_prompt", "final_prompt")
                    if text(record.get(key))
                ),
                "",
            )
            if prompt:
                prompt_digest = hashlib.sha256(prompt.encode("utf-8", "ignore")).hexdigest()
        return media_type, status, source, source_label, used_model, image_code, prompt_digest

    @classmethod
    def _backfill_materialized_fields(cls, connection: sqlite3.Connection) -> None:
        """Backfill materialized columns for records written by older versions."""
        rows = connection.execute("SELECT id, payload FROM generation_records").fetchall()
        updates = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, Mapping):
                continue
            updates.append((cls._materialized_fields(payload), str(row["id"])))
        if updates:
            connection.executemany(
                """
                UPDATE generation_records
                SET media_type = ?, status = ?, source = ?, source_label = ?,
                    used_model = ?, image_code = ?, prompt_digest = ?
                WHERE id = ?
                """,
                [(*fields, record_id) for fields, record_id in updates],
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
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT 1 FROM generation_records LIMIT 1").fetchone()
        return row is not None

    def max_sequence(self) -> int:
        """Highest ``seq`` currently stored, 0 when the table is empty.

        Callers must restore their counter above this value: deriving it from
        the row count instead reuses numbers as soon as any record is deleted
        (``max(seq)`` stays high while the count drops), which makes a fresh
        record tie with an existing row and lets pruning delete the new one.
        """
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT MAX(seq) AS highest FROM generation_records"
            ).fetchone()
        try:
            return max(0, int(row["highest"] or 0))
        except (TypeError, ValueError):
            return 0

    def load_records(self, limit: int = 1000) -> List[Dict[str, Any]]:
        """Load the newest normal rows plus every protected asset.

        ``limit`` is the retention allowance for ordinary rows. Favorites and
        pinned assets deliberately sit outside that allowance, so loading only
        the newest ``limit`` rows can otherwise make an old protected asset
        disappear after restart.
        """
        safe_limit = max(1, min(int(limit or 1000), 10000))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT id, seq, time, payload, favorite, pinned, tags, note, created_ts "
                "FROM generation_records "
                "WHERE pinned != 0 OR favorite != 0 OR id IN ("
                "SELECT id FROM generation_records WHERE pinned = 0 AND favorite = 0 "
                "ORDER BY time DESC, seq DESC, id DESC LIMIT ?) "
                "ORDER BY time DESC, seq DESC, id DESC",
                (safe_limit,),
            ).fetchall()
        return self._decode_rows(rows)

    @classmethod
    def _decode_rows(cls, rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
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
            payload["tags"] = cls._decode_tags(row["tags"])
            payload["note"] = str(row["note"] or "")
            if row["created_ts"]:
                payload.setdefault("created_ts", row["created_ts"])
            records.append(payload)
        return records

    def query_records(
        self, *, keep_limit: int = 1000, favorite: Optional[bool] = None,
        pinned: Optional[bool] = None, tag: str = "", source: str = "", model: str = "",
        media_type: str = "", success: Optional[bool] = None, keyword: str = "",
        start_time: str = "", end_time: str = "", offset: int = 0,
        limit: int = 1000, sort: str = "recent",
    ) -> tuple[List[Dict[str, Any]], Dict[str, int]]:
        """Filter and page in SQLite before decoding record payloads."""
        keep = max(1, min(int(keep_limit), 10000))
        safe_limit = max(1, min(int(limit or keep), keep))
        safe_offset = max(0, int(offset or 0))
        retained = (
            "WITH retained AS (SELECT * FROM generation_records WHERE pinned != 0 OR favorite != 0 "
            "OR id IN (SELECT id FROM generation_records WHERE pinned = 0 AND favorite = 0 "
            "ORDER BY time DESC, seq DESC, id DESC LIMIT ?)) "
        )
        retained_ids = (
            "WITH retained_ids AS (SELECT id FROM generation_records WHERE pinned != 0 OR favorite != 0 "
            "UNION ALL SELECT id FROM (SELECT id FROM generation_records WHERE pinned = 0 AND favorite = 0 "
            "ORDER BY time DESC, seq DESC, id DESC LIMIT ?)) "
        )
        clauses = ["json_valid(payload)"]
        params: List[Any] = [keep]
        for name, value in (("favorite", favorite), ("pinned", pinned)):
            if value is not None:
                clauses.append(f"{name} = ?")
                params.append(int(bool(value)))
        if tag:
            clauses.append("EXISTS (SELECT 1 FROM json_each(tags) WHERE text_lower(value) = ?)")
            params.append(str(tag).strip().lower())

        # Prefer materialized columns while retaining JSON fallbacks for a
        # database interrupted during migration or populated by an old writer.
        source_field = (
            "COALESCE(NULLIF(source, ''), json_extract(payload, '$.source'), '')"
        )
        source_label_field = (
            "COALESCE(NULLIF(source_label, ''), json_extract(payload, '$.source_label'), '')"
        )
        model_field = "COALESCE(NULLIF(used_model, ''), json_extract(payload, '$.used_model'), '')"
        media_type_field = (
            "COALESCE(NULLIF(media_type, ''), NULLIF(json_extract(payload, '$.media_type'), ''), 'image')"
        )
        search_columns = {
            "source": " || ' ' || ".join((
                source_field, source_label_field,
                "COALESCE(json_extract(payload, '$.group_id'), '')",
                "COALESCE(json_extract(payload, '$.user_id'), '')",
            )),
            "model": model_field,
            "keyword": self._keyword_search_expression(),
        }
        keyword_text = str(keyword or "").strip()
        keyword_lower = keyword_text.lower()
        for name, value in (("source", source), ("model", model)):
            if str(value or "").strip():
                clauses.append(f"instr(text_lower({search_columns[name]}), ?) > 0")
                params.append(str(value).strip().lower())
        fts_clause = ""
        fts_param_index: Optional[int] = None
        fts_param_count = 0
        if media_type:
            clauses.append(f"text_lower({media_type_field}) = ?")
            params.append(str(media_type).strip().lower())
        if success is not None:
            clauses.append("COALESCE(json_extract(payload, '$.success'), 0) = ?")
            params.append(int(bool(success)))
        if start_time:
            clauses.append("time >= ?")
            params.append(str(start_time).strip())
        if end_time:
            end = str(end_time).strip()
            clauses.append("time <= ?")
            params.append(end + " 23:59:59" if len(end) == 10 else end)
        sort_text = str(sort or "recent").strip().lower()
        if sort_text in {"priority", "pinned", "favorite"}:
            order = "pinned DESC, favorite DESC, time DESC, seq DESC, id DESC"
        elif sort_text in {"oldest", "asc"}:
            order = "time ASC, seq ASC, id ASC"
        else:
            order = "time DESC, seq DESC, id DESC"
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN")
            total = connection.execute(
                retained_ids + "SELECT COUNT(*) FROM retained_ids", (keep,)
            ).fetchone()[0]
            if keyword_text and self._fts5_enabled:
                if not self._fts_trigrams(keyword_lower):
                    # Trigram tokenizers cannot answer one- or two-character
                    # terms (nor NUL-containing terms).  The FTS projection
                    # still stores the exact lower-cased legacy search text,
                    # so scan that materialized column instead of reparsing
                    # every JSON payload in the scalar fallback.
                    fts_clause = (
                        f"id IN (SELECT record_id FROM {self._FTS_TABLE} "
                        "WHERE instr(search_text, ?) > 0)"
                    )
                    clauses.append(fts_clause)
                    fts_param_index = len(params)
                    fts_param_count = 1
                    params.append(keyword_lower)
                else:
                    try:
                        fts_keyword = self._fts_selective_match_query(
                            connection, keyword_lower, int(total)
                        )
                    except sqlite3.DatabaseError:
                        # Missing vocabulary support only affects query planning.
                        fts_keyword = self._fts_match_query(keyword_lower)
                    if fts_keyword:
                        fts_clause = (
                            f"id IN (SELECT record_id FROM {self._FTS_TABLE} "
                            f"WHERE {self._FTS_TABLE} MATCH ? AND instr(search_text, ?) > 0)"
                        )
                        clauses.append(fts_clause)
                        fts_param_index = len(params)
                        fts_param_count = 2
                        params.extend((fts_keyword, keyword_lower))
                    else:
                        clauses.append(self._keyword_search_predicate())
                        params.extend((keyword_lower, keyword_lower, keyword_lower))
            elif keyword_text:
                clauses.append(self._keyword_search_predicate())
                params.extend((keyword_lower, keyword_lower, keyword_lower))
            if keyword_text and not fts_clause and len(clauses) == 2:
                record_count = int(
                    connection.execute("SELECT COUNT(*) FROM generation_records").fetchone()[0]
                )
                if record_count == int(total):
                    prefix_count = int(connection.execute(
                        f"SELECT COUNT(*) FROM {self._PROMPT_PREFIX_TABLE} "
                        "WHERE instr(prompt_prefix, ?) > 0",
                        (keyword_lower,),
                    ).fetchone()[0])
                    if prefix_count == int(total):
                        # Every retained row is a valid prefix match, so filtering cannot remove rows.
                        rows = connection.execute(
                            retained + f"SELECT * FROM retained ORDER BY {order} LIMIT ? OFFSET ?",
                            [keep, safe_limit, safe_offset],
                        ).fetchall()
                        return self._decode_rows(rows), {
                            "total": int(total), "filtered": int(total),
                            "offset": safe_offset, "limit": safe_limit,
                        }
            where = " WHERE " + " AND ".join(clauses)
            try:
                if keyword_text:
                    page_query = (
                        retained
                        + ", filtered AS (SELECT id, time, seq, pinned, favorite, "
                        "COUNT(*) OVER() AS filtered_count FROM retained"
                        + where
                        + f" ORDER BY {order} LIMIT ? OFFSET ?) "
                        "SELECT generation_records.*, filtered.filtered_count "
                        "FROM filtered JOIN generation_records ON generation_records.id = filtered.id "
                        f"ORDER BY {order}"
                    )
                    rows = connection.execute(
                        page_query, [*params, safe_limit, safe_offset]
                    ).fetchall()
                    if rows:
                        count = int(rows[0]["filtered_count"])
                    else:
                        count = connection.execute(
                            retained + "SELECT COUNT(*) FROM retained" + where, params
                        ).fetchone()[0]
                else:
                    count = connection.execute(
                        retained + "SELECT COUNT(*) FROM retained" + where, params
                    ).fetchone()[0]
                    rows = connection.execute(
                        retained + "SELECT * FROM retained" + where + f" ORDER BY {order} LIMIT ? OFFSET ?",
                        [*params, safe_limit, safe_offset],
                    ).fetchall()
            except sqlite3.DatabaseError:
                if fts_param_index is None:
                    raise
                # Disable this instance for subsequent calls, then rerun the
                # same transaction using the pre-FTS scalar expression.
                self._invalidate_fts(connection)
                fallback_clauses = [clause for clause in clauses if clause != fts_clause]
                fallback_clauses.append(self._keyword_search_predicate())
                fallback_where = " WHERE " + " AND ".join(fallback_clauses)
                fallback_params = list(params)
                del fallback_params[fts_param_index:fts_param_index + fts_param_count]
                fallback_params.extend((keyword_lower, keyword_lower, keyword_lower))
                count = connection.execute(
                    retained + "SELECT COUNT(*) FROM retained" + fallback_where,
                    fallback_params,
                ).fetchone()[0]
                rows = connection.execute(
                    retained + "SELECT * FROM retained" + fallback_where + f" ORDER BY {order} LIMIT ? OFFSET ?",
                    [*fallback_params, safe_limit, safe_offset],
                ).fetchall()
        return self._decode_rows(rows), {"total": total, "filtered": count, "offset": safe_offset, "limit": safe_limit}

    def replace_records(self, records: Iterable[Mapping[str, Any]]) -> None:
        incoming = [item for item in records if isinstance(item, Mapping)]
        rows = []
        now = time.time()
        # records are newest-first; give the newest the largest seq so
        # ORDER BY seq DESC keeps matching insertion order.
        total = len(incoming)
        for seq, record in enumerate(incoming):
            if not isinstance(record, Mapping):
                continue
            record_id = str(record.get("id") or "").strip()
            if not record_id:
                continue
            payload = dict(record)
            favorite, pinned, tags, note = self._metadata(payload)
            materialized = self._materialized_fields(payload)
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
                    total - len(rows),
                    str(record.get("time") or ""),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    favorite,
                    pinned,
                    tags,
                    note,
                    float(record.get("created_ts") or now),
                    *materialized,
                )
            )
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM generation_records")
            connection.execute(f"DELETE FROM {self._PROMPT_PREFIX_TABLE}")
            connection.executemany(
                "INSERT INTO generation_records "
                "(id, seq, time, payload, favorite, pinned, tags, note, created_ts, "
                "media_type, status, source, source_label, used_model, image_code, prompt_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._rebuild_prompt_prefix_projection(connection)
            if self._fts5_enabled:
                try:
                    self._rebuild_fts(connection)
                except sqlite3.DatabaseError:
                    self._invalidate_fts(connection)
            else:
                self._invalidate_fts(connection)
            connection.commit()

    def upsert_record(self, record: Mapping[str, Any], *, sequence: Optional[int] = None) -> None:
        """Insert or update one retained record without rebuilding the table."""
        payload = dict(record)
        record_id = str(payload.get("id") or "").strip()
        if not record_id:
            raise ValueError("generation record id is required")
        favorite, pinned, tags, note = self._metadata(payload)
        materialized = self._materialized_fields(payload)
        payload.pop("favorite", None)
        payload.pop("is_favorite", None)
        payload.pop("pinned", None)
        payload.pop("is_pinned", None)
        payload.pop("tags", None)
        payload.pop("note", None)
        payload.pop("asset_note", None)
        with self._lock, self._connection() as connection:
            if sequence is None:
                row = connection.execute(
                    "SELECT seq FROM generation_records WHERE id = ?", (record_id,)
                ).fetchone()
                sequence_value = int(row["seq"]) if row is not None else 0
            else:
                sequence_value = int(sequence)
            connection.execute(
                """
                INSERT INTO generation_records
                    (id, seq, time, payload, favorite, pinned, tags, note, created_ts,
                     media_type, status, source, source_label, used_model, image_code, prompt_digest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    seq = excluded.seq,
                    time = excluded.time,
                    payload = excluded.payload,
                    favorite = excluded.favorite,
                    pinned = excluded.pinned,
                    tags = excluded.tags,
                    note = excluded.note,
                    created_ts = excluded.created_ts,
                    media_type = excluded.media_type,
                    status = excluded.status,
                    source = excluded.source,
                    source_label = excluded.source_label,
                    used_model = excluded.used_model,
                    image_code = excluded.image_code,
                    prompt_digest = excluded.prompt_digest
                """,
                (
                    record_id,
                    int(sequence_value),
                    str(payload.get("time") or ""),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    favorite,
                    pinned,
                    tags,
                    note,
                    float(payload.get("created_ts") or time.time()),
                    *materialized,
                ),
            )
            self._sync_prompt_prefix_record(connection, record_id)
            self._sync_fts_record(connection, record_id)

    def prune_to_limit(self, limit: int) -> List[str]:
        """Drop the oldest unpinned records beyond ``limit``.

        Pinned and favorite records are never evicted: they are excluded from
        the retention allowance entirely, so a pinned entry cannot push a
        normal record out of the pool and then be deleted itself.

        Ordering uses the durable ``time`` string (newest first) with ``seq``
        and ``id`` as deterministic tie-breakers. Ordering by ``seq`` alone was
        wrong as soon as the counter and the stored rows disagreed: a record
        added after any deletion reused an existing ``seq``, sorted as the
        oldest row, and was therefore the one this method deleted.
        ``created_ts`` is not usable here because a bulk rewrite stamps many
        rows with the same value.
        """
        keep = max(1, int(limit or 1))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT id FROM generation_records "
                "WHERE COALESCE(pinned, 0) = 0 AND COALESCE(favorite, 0) = 0 "
                "ORDER BY time DESC, seq DESC, id DESC LIMIT -1 OFFSET ?",
                (keep,),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self._delete_fts_records(connection, ids)
                self._delete_prompt_prefix_records(connection, ids)
                connection.execute(
                    f"DELETE FROM generation_records WHERE id IN ({placeholders})", ids
                )
        return ids

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
        with self._lock, self._connection() as connection:
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
            self._sync_fts_record(connection, target)
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
        with self._lock, self._connection() as connection:
            placeholders = ",".join("?" for _ in ids)
            self._delete_fts_records(connection, ids)
            self._delete_prompt_prefix_records(connection, ids)
            cursor = connection.execute(
                f"DELETE FROM generation_records WHERE id IN ({placeholders})",
                ids,
            )
            return int(cursor.rowcount or 0)

    def clear(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM generation_records")
            connection.execute(f"DELETE FROM {self._PROMPT_PREFIX_TABLE}")
            if self._fts5_enabled:
                try:
                    connection.execute(f"DELETE FROM {self._FTS_TABLE}")
                except (sqlite3.OperationalError, sqlite3.DatabaseError):
                    self._invalidate_fts(connection)
            else:
                self._invalidate_fts(connection)
