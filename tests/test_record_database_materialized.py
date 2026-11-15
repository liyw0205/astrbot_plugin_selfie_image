import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from astrbot_plugin_selfie_image.generation.record_database import RecordDatabase


def test_database_operations_close_connections_without_garbage_collection(tmp_path):
    connections = []

    class TrackedDatabase(RecordDatabase):
        def _connect(self):
            connection = super()._connect()
            connections.append(connection)
            return connection

    database = TrackedDatabase(str(tmp_path / "records.sqlite3"))
    database.replace_records([{"id": "old"}])
    database.upsert_record({"id": "new"}, sequence=2)
    assert database.has_records()
    assert database.max_sequence() == 2
    assert database.load_records()
    assert database.query_records()[0]
    database.update_metadata("new", favorite=True)
    database.prune_to_limit(1)
    database.delete_records(["old"])
    database.clear()
    assert len(connections) == 11
    # Strong references rule out GC closing these handles for us.
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


def test_failed_record_write_rolls_back_and_closes_connection(tmp_path, monkeypatch):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    database.upsert_record({"id": "stable", "prompt": "original"})
    connections = []
    original_connect = database._connect

    def connect():
        connection = original_connect()
        connections.append(connection)
        return connection

    def fail_after_write(_connection, _record_id):
        raise RuntimeError("failure after base-table update")

    monkeypatch.setattr(database, "_connect", connect)
    monkeypatch.setattr(database, "_sync_fts_record", fail_after_write)
    with pytest.raises(RuntimeError, match="after base-table update"):
        database.upsert_record({"id": "stable", "prompt": "replacement"})
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connections[0].execute("SELECT 1")
    assert database.query_records()[0][0]["prompt"] == "original"


def test_materialized_fields_are_written_and_used_for_queries(tmp_path):
    path = tmp_path / "records.sqlite3"
    database = RecordDatabase(str(path))
    record = {
        "id": "record-1",
        "time": "2026-11-14 12:00:00",
        "media_type": " VIDEO ",
        "status": "partial_success",
        "source": "group-1",
        "source_label": "Design Team",
        "used_model": "model-a",
        "image_code": "ABC234",
        "original_prompt": "A quiet studio portrait",
        "success": True,
    }
    database.upsert_record(record, sequence=1)

    with sqlite3.connect(path) as connection:
        columns = {
            row[1]: row[2] for row in connection.execute("PRAGMA table_info(generation_records)")
        }
        row = connection.execute(
            "SELECT media_type, status, source, source_label, used_model, image_code, prompt_digest "
            "FROM generation_records WHERE id = ?",
            ("record-1",),
        ).fetchone()
    assert {"media_type", "status", "source", "source_label", "used_model", "image_code", "prompt_digest"} <= set(columns)
    assert row == (
        "video",
        "partial_success",
        "group-1",
        "Design Team",
        "model-a",
        "ABC234",
        hashlib.sha256("A quiet studio portrait".encode()).hexdigest(),
    )

    rows, metadata = database.query_records(source="design team", model="MODEL-A", media_type="video")
    assert [item["id"] for item in rows] == ["record-1"]
    assert metadata["filtered"] == 1


def test_old_database_is_migrated_and_materialized_values_backfilled(tmp_path):
    path = tmp_path / "records.sqlite3"
    payload = {
        "id": "legacy-1",
        "time": "2026-11-13 10:00:00",
        "media_type": "image",
        "status": "succeeded",
        "source": "legacy-group",
        "source_label": "Legacy",
        "used_model": "legacy-model",
        "image_code": "OLD123",
        "prompt": "legacy prompt",
        "success": True,
    }
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE generation_records (
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
            """
        )
        connection.execute(
            "INSERT INTO generation_records (id, seq, time, payload) VALUES (?, ?, ?, ?)",
            ("legacy-1", 1, payload["time"], json.dumps(payload)),
        )

    database = RecordDatabase(str(path))
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT media_type, status, source, source_label, used_model, image_code, prompt_digest "
            "FROM generation_records WHERE id = ?",
            ("legacy-1",),
        ).fetchone()
        prompt_prefix = connection.execute(
            "SELECT prompt_prefix FROM generation_record_prompt_prefix WHERE record_id = ?",
            ("legacy-1",),
        ).fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert row[:6] == (
        "image", "succeeded", "legacy-group", "Legacy", "legacy-model", "OLD123"
    )
    assert row[6] == hashlib.sha256(b"legacy prompt").hexdigest()
    assert prompt_prefix == "  legacy prompt"
    assert version == RecordDatabase._MATERIALIZED_SCHEMA_VERSION
    assert database.load_records()[0]["id"] == "legacy-1"
    assert [item["id"] for item in database.query_records(keyword="gacy pro")[0]] == ["legacy-1"]


def test_retained_total_and_filtering_preserve_invalid_payload_boundaries(tmp_path):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    with sqlite3.connect(database.path) as connection:
        connection.executemany(
            """
            INSERT INTO generation_records (id, seq, time, payload, favorite, pinned)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ("protected-invalid", 1, "2026-11-14 10:00:00", "{broken", 1, 0),
                ("newest-invalid", 4, "2026-11-14 14:00:00", "{broken", 0, 0),
                ("newest-valid", 3, "2026-11-14 13:00:00", '{"id":"newest-valid"}', 0, 0),
                ("older-valid", 2, "2026-11-14 12:00:00", '{"id":"older-valid"}', 0, 0),
            ],
        )

    rows, metadata = database.query_records(keep_limit=2, limit=10)
    assert metadata["total"] == 3
    assert metadata["filtered"] == 1
    assert [item["id"] for item in rows] == ["newest-valid"]


def test_retained_total_uses_index_counts_and_preserves_empty_and_keep_boundaries(tmp_path, monkeypatch):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    database.replace_records([
        {"id": "normal-new", "time": "2026-11-14 14:00:00", "prompt": "new"},
        {"id": "normal-old", "time": "2026-11-14 13:00:00", "prompt": "old"},
        {"id": "protected", "time": "2026-11-14 12:00:00", "favorite": True, "prompt": "saved"},
    ])
    statements = []
    original_connect = database._connect

    def connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database, "_connect", connect)
    rows, metadata = database.query_records(keep_limit=1, limit=2)
    assert metadata["total"] == 2
    assert metadata["filtered"] == 2
    assert [item["id"] for item in rows] == ["normal-new"]
    total_queries = [sql for sql in statements if "SELECT (SELECT COUNT(*)" in sql]
    assert total_queries
    assert all("MIN(1" in sql for sql in total_queries)
    assert not any("retained_ids" in sql for sql in statements)

    rows, metadata = database.query_records(keep_limit=2, limit=10, offset=2)
    assert metadata["total"] == 3
    assert metadata["filtered"] == 3
    assert [item["id"] for item in rows] == ["protected"]

    statements.clear()
    database.clear()
    rows, metadata = database.query_records(keep_limit=100000, limit=10)
    assert rows == []
    assert metadata["total"] == 0
    assert metadata["filtered"] == 0
    assert any("SELECT (SELECT COUNT(*)" in sql for sql in statements)


def test_fts5_projection_is_migrated_and_tracks_replace_update_and_delete(tmp_path):
    path = tmp_path / "records.sqlite3"
    database = RecordDatabase(str(path))
    if not database._fts5_enabled:
        pytest.skip("SQLite build has no trigram FTS5")

    database.replace_records([
        {"id": "first", "time": "2026-11-14", "prompt": "quiet mountain"},
        {"id": "second", "time": "2026-11-13", "prompt": "city lights"},
    ])
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT record_id FROM generation_records_fts WHERE generation_records_fts MATCH ?",
            ('"mountain"',),
        ).fetchone() == ("first",)

    database.upsert_record(
        {"id": "first", "time": "2026-11-14", "prompt": "forest cabin"},
        sequence=2,
    )
    assert database.query_records(keyword="mountain")[0] == []
    assert [item["id"] for item in database.query_records(keyword="cabin")[0]] == ["first"]

    database.update_metadata("first", note="waterfall note")
    assert [item["id"] for item in database.query_records(keyword="waterfall")[0]] == ["first"]
    database.delete_records(["first"])
    assert database.query_records(keyword="waterfall")[0] == []
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM generation_records_fts WHERE record_id = ?", ("first",)
        ).fetchone()[0] == 0


@pytest.mark.parametrize("message", ["no such tokenizer: trigram", "no such module: fts5"])
def test_keyword_query_falls_back_when_fts5_is_unavailable(tmp_path, monkeypatch, message):
    original_connect = sqlite3.connect

    class MissingFTSConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if "CREATE VIRTUAL TABLE" in sql.upper():
                raise sqlite3.OperationalError(message)
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        return original_connect(*args, **kwargs, factory=MissingFTSConnection)

    monkeypatch.setattr(sqlite3, "connect", connect)
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    assert not database._fts5_enabled
    database.replace_records([
        {"id": "fallback", "time": "2026-11-14", "prompt": "substring-search"},
    ])
    rows, metadata = database.query_records(keyword="string-sear")
    assert [item["id"] for item in rows] == ["fallback"]
    assert metadata["filtered"] == 1


@pytest.mark.parametrize("keyword", [
    "trait", "PORTRAIT", "设计团队", "设计", "tail-match", 'say "hi"',
    "blue  room", "blue room", "line\nbreak", "tab\tspace", "\" OR *",
    "x:y", "İST", "ßABC", "STRASSE", "source     ", "\x00after",
])
def test_fts_keyword_preserves_scalar_substring_contract(tmp_path, keyword):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    database.replace_records([
        {
            "id": "match", "time": "2026-11-14", "source": "source",
            "prompt": 'portrait 设计团队 tail-match say "hi" blue  room line\nbreak tab\tspace " OR * x:y İSTANBUL ẞABC Straße \x00after',
        },
        {"id": "other", "time": "2026-11-13", "prompt": "unrelated text"},
    ])
    indexed_rows, indexed_metadata = database.query_records(keyword=keyword)
    database._fts5_enabled = False
    scalar_rows, scalar_metadata = database.query_records(keyword=keyword)
    assert indexed_rows == scalar_rows
    assert indexed_metadata == scalar_metadata


def test_fts_text_exactly_matches_scalar_projection_and_excludes_response_only_fields(tmp_path):
    path = tmp_path / "records.sqlite3"
    database = RecordDatabase(str(path))
    database.upsert_record({
        "id": "exact", "source": "source", "original_prompt": "  Original\tText  ",
        "request_prompt": "req  prompt", "prompt": " final\nprompt ",
        "image_code": "ABC123", "response_data": {"error": "response-only-error"},
        "error": "  JSON error  ", "note": " note ", "status": "status-only-text",
    })
    if not database._fts5_enabled:
        pytest.skip("SQLite build has no trigram FTS5")
    with closing(database._connect()) as connection:
        scalar = connection.execute(
            f"SELECT text_lower({database._keyword_search_expression()}) FROM generation_records"
        ).fetchone()[0]
        indexed = connection.execute("SELECT search_text FROM generation_records_fts").fetchone()[0]
    assert indexed == scalar
    assert "  original\ttext  " in indexed
    assert indexed.count("abc123") == 1
    assert "response-only-error" not in indexed
    assert "status-only-text" not in indexed
    assert database.query_records(keyword="response-only-error")[0] == []
    assert database.query_records(keyword="status-only-text")[0] == []


def test_keyword_prompt_fast_path_preserves_cross_field_matches(tmp_path):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    database.replace_records([
        {"id": "prompt-boundary", "original_prompt": "original", "request_prompt": "request"},
        {"id": "source-boundary", "source": "source", "source_label": "label"},
        {"id": "error-boundary", "error": "broken", "failure_reason": "request"},
    ])

    assert [row["id"] for row in database.query_records(keyword="original request")[0]] == [
        "prompt-boundary"
    ]
    assert [row["id"] for row in database.query_records(keyword="source label")[0]] == [
        "source-boundary"
    ]
    assert [row["id"] for row in database.query_records(keyword="broken request")[0]] == [
        "error-boundary"
    ]


def test_prompt_prefix_projection_tracks_writes_and_falls_back_after_prefix(tmp_path):
    path = tmp_path / "records.sqlite3"
    database = RecordDatabase(str(path))
    database.replace_records([
        {"id": "early", "original_prompt": "Extended prompt section, soft light"},
        {"id": "late", "original_prompt": "x" * 300 + " distant-needle"},
    ])

    assert [row["id"] for row in database.query_records(keyword="PROMPT SECTION")[0]] == ["early"]
    assert [row["id"] for row in database.query_records(keyword="distant-needle")[0]] == ["late"]
    with sqlite3.connect(path) as connection:
        prefix_rows = connection.execute(
            "SELECT record_id, prompt_prefix FROM generation_record_prompt_prefix ORDER BY record_id"
        ).fetchall()
    assert prefix_rows == [
        ("early", "extended prompt section, soft light  "),
        ("late", "x" * 128),
    ]

    database.upsert_record({"id": "early", "original_prompt": "replacement token"})
    assert database.query_records(keyword="extended prompt")[0] == []
    assert [row["id"] for row in database.query_records(keyword="replacement token")[0]] == ["early"]
    assert database.delete_records(["late"]) == 1
    assert database.query_records(keyword="distant-needle")[0] == []
    database.clear()
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM generation_record_prompt_prefix"
        ).fetchone()[0] == 0


def test_scalar_keyword_page_uses_one_filtered_scan_and_preserves_empty_page_count(tmp_path):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    database.replace_records([
        {"id": "new", "time": "2026-11-14 12:00:00", "prompt": "common marker"},
        {"id": "old", "time": "2026-11-13 12:00:00", "prompt": "common marker"},
        {"id": "other", "time": "2026-11-12 12:00:00", "prompt": "unrelated"},
    ])
    database._fts5_enabled = False

    rows, metadata = database.query_records(keyword="common marker", limit=1)
    assert [row["id"] for row in rows] == ["new"]
    assert metadata["filtered"] == 2

    rows, metadata = database.query_records(keyword="common marker", limit=1, offset=5)
    assert rows == []
    assert metadata["filtered"] == 2


def test_regular_page_uses_window_count_and_preserves_empty_page_count(tmp_path, monkeypatch):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    database.replace_records([
        {"id": "new", "time": "2026-11-14 12:00:00", "prompt": "common marker"},
        {"id": "old", "time": "2026-11-13 12:00:00", "prompt": "common marker"},
        {"id": "other", "time": "2026-11-12 12:00:00", "prompt": "unrelated"},
    ])
    statements = []
    original_connect = database._connect

    def connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database, "_connect", connect)
    rows, metadata = database.query_records(model="", limit=1)
    assert [row["id"] for row in rows] == ["new"]
    assert metadata["filtered"] == 3
    assert any("COUNT(*) OVER() AS filtered_count" in sql for sql in statements)
    assert not any("SELECT COUNT(*) FROM retained WHERE" in sql for sql in statements)

    rows, metadata = database.query_records(model="", limit=1, offset=5)
    assert rows == []
    assert metadata["filtered"] == 3


def test_all_prefix_match_shortcut_returns_complete_filtered_count(tmp_path, monkeypatch):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    database.replace_records([
        {"id": "new", "time": "2026-11-14 12:00:00", "prompt": "common marker"},
        {"id": "old", "time": "2026-11-13 12:00:00", "prompt": "common marker"},
    ])
    database._fts5_enabled = False
    statements = []
    original_connect = database._connect

    def connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database, "_connect", connect)
    rows, metadata = database.query_records(keyword="common marker", limit=1)

    assert [row["id"] for row in rows] == ["new"]
    assert metadata["filtered"] == 2
    assert any(
        "COUNT(*) FROM generation_record_prompt_prefix WHERE instr" in sql
        for sql in statements
    )


def test_keyword_search_uses_match_candidates_and_scalar_confirmation(tmp_path, monkeypatch):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    if not database._fts5_enabled:
        pytest.skip("SQLite build has no trigram FTS5")
    database.upsert_record({"id": "candidate", "prompt": "quiet mountain"})
    statements = []
    original_connect = database._connect

    def connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database, "_connect", connect)
    assert [row["id"] for row in database.query_records(keyword="ounta")[0]] == ["candidate"]
    candidate_queries = [sql for sql in statements if " MATCH " in sql and not sql.startswith("--")]
    assert candidate_queries
    assert all("instr(search_text," in sql for sql in candidate_queries)
    assert all("instr(text_lower(" not in sql for sql in candidate_queries)


def test_short_keyword_uses_materialized_fts_text_and_preserves_scalar_results(tmp_path, monkeypatch):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    if not database._fts5_enabled:
        pytest.skip("SQLite build has no trigram FTS5")
    database.replace_records([
        {"id": "match", "time": "2026-11-14", "prompt": "自然光 portrait"},
        {"id": "field", "time": "2026-11-13", "source": "自然来源", "prompt": "unrelated"},
        {"id": "other", "time": "2026-11-12", "prompt": "unrelated"},
    ])
    statements = []
    original_connect = database._connect

    def connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database, "_connect", connect)
    indexed_rows, indexed_metadata = database.query_records(keyword="自然", limit=10)
    assert [row["id"] for row in indexed_rows] == ["match", "field"]
    assert indexed_metadata["filtered"] == 2
    assert any(
        "generation_records_fts" in sql and "instr(search_text" in sql
        for sql in statements if not sql.startswith("--")
    )
    assert not any(
        " MATCH " in sql for sql in statements if not sql.startswith("--")
    )

    database._fts5_enabled = False
    scalar_rows, scalar_metadata = database.query_records(keyword="自然", limit=10)
    assert (indexed_rows, indexed_metadata) == (scalar_rows, scalar_metadata)


def test_fts_search_text_confirmation_preserves_legacy_fields_and_prompt_fallback(tmp_path):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    if not database._fts5_enabled:
        pytest.skip("SQLite build has no trigram FTS5")
    database.replace_records([
        {"id": "original", "original_prompt": "quiet mountain", "source": "legacy-source"},
        {"id": "request", "request_prompt": "quiet mountain", "note": "legacy-note"},
        {"id": "prompt", "prompt": "quiet mountain", "error": "legacy-error"},
        {"id": "disjoint", "source": "quiet", "note": "mountain"},
    ])
    indexed = database.query_records(keyword="mountain")[0]
    database._fts5_enabled = False
    scalar = database.query_records(keyword="mountain")[0]
    assert [row["id"] for row in indexed] == [row["id"] for row in scalar]
    assert {row["id"] for row in indexed} == {"original", "request", "prompt", "disjoint"}


def test_fts_keyword_window_count_preserves_filtered_count_on_empty_page(tmp_path):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    if not database._fts5_enabled:
        pytest.skip("SQLite build has no trigram FTS5")
    database.replace_records([
        {"id": "new", "time": "2026-11-14 12:00:00", "prompt": "common marker"},
        {"id": "old", "time": "2026-11-13 12:00:00", "prompt": "common marker"},
        {"id": "other", "time": "2026-11-12 12:00:00", "prompt": "unrelated"},
    ])
    rows, metadata = database.query_records(keyword="common marker", limit=1, offset=5)
    assert rows == []
    assert metadata["filtered"] == 2


@pytest.mark.parametrize("keyword", [
    "extended prompt section", "abc" * 1000, 'abc " OR * xyz',
    "abc\ndef\tghi", "设计团队的山间小屋", "abc  def   ghi",
])
def test_bounded_trigram_candidates_preserve_literal_substring_results(tmp_path, keyword):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    if not database._fts5_enabled:
        pytest.skip("SQLite build has no trigram FTS5")
    last = len(keyword) - 3
    terms = list(dict.fromkeys(keyword[start:start + 3] for start in (0, last // 2, last)))
    database.replace_records([
        {"id": "exact", "prompt": f"before {keyword} after"},
        {"id": "disjoint", "prompt": " ... ".join(terms)},
        {"id": "reordered", "prompt": " ... ".join(reversed(terms))},
        {"id": "unrelated", "prompt": "quiet lake"},
    ])
    with closing(database._connect()) as connection:
        candidates = {row[0] for row in connection.execute(
            "SELECT record_id FROM generation_records_fts WHERE generation_records_fts MATCH ?",
            (database._fts_match_query(keyword),),
        )}
    assert {"exact", "disjoint", "reordered"} <= candidates
    indexed_rows, indexed_metadata = database.query_records(keyword=keyword)
    assert database._fts5_enabled
    assert [row["id"] for row in indexed_rows] == ["exact"]
    database._fts5_enabled = False
    assert (indexed_rows, indexed_metadata) == database.query_records(keyword=keyword)


def test_long_keyword_candidate_expression_has_bounded_terms():
    assert RecordDatabase._fts_match_query("extended prompt section") == '"ext" AND "rom" AND "ion"'
    assert RecordDatabase._fts_match_query("a" * 10000) == '"aaa"'
    assert RecordDatabase._fts_match_query('a"b') == '"a""b"'
    assert RecordDatabase._fts_match_query("短词") == ""
    assert RecordDatabase._fts_match_query("nul\x00text") == ""


def test_broad_candidates_use_scalar_and_selective_candidates_use_fts(tmp_path, monkeypatch):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    if not database._fts5_enabled:
        pytest.skip("SQLite build has no trigram FTS5")
    database.replace_records([
        {"id": f"broad-{index}", "prompt": "extended prompt section " * 5}
        for index in range(12)
    ] + [
        {"id": "rare-1", "prompt": "rare-candidate-needle"},
        {"id": "rare-2", "prompt": "rare-candidate-needle"},
    ])
    statements = []
    original_connect = database._connect

    def connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database, "_connect", connect)
    broad_rows, broad_metadata = database.query_records(keyword="extended prompt section")
    broad_statements = list(statements)
    assert len(broad_rows) == 12
    assert broad_metadata["filtered"] == 12
    assert not any(" MATCH " in sql for sql in broad_statements if not sql.startswith("--"))

    statements.clear()
    rare_rows, rare_metadata = database.query_records(keyword="rare-candidate-needle")
    assert {row["id"] for row in rare_rows} == {"rare-1", "rare-2"}
    assert rare_metadata["filtered"] == 2
    assert any(" MATCH " in sql for sql in statements if not sql.startswith("--"))

    statements.clear()
    assert database.query_records(keyword="absent-text")[0] == []
    assert any(" MATCH " in sql for sql in statements if not sql.startswith("--"))


def test_missing_vocabulary_keeps_exact_keyword_results_and_recreates_on_restart(tmp_path):
    path = tmp_path / "records.sqlite3"
    database = RecordDatabase(str(path))
    if not database._fts5_enabled:
        pytest.skip("SQLite build has no trigram FTS5")
    database.replace_records([
        {"id": "exact", "prompt": "extended prompt section"},
        {"id": "disjoint", "prompt": "ext ... rom ... ion"},
    ])
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE generation_records_fts_vocab")
    indexed_rows, indexed_metadata = database.query_records(keyword="extended prompt section")
    assert [row["id"] for row in indexed_rows] == ["exact"]
    assert database._fts5_enabled
    database._fts5_enabled = False
    assert (indexed_rows, indexed_metadata) == database.query_records(keyword="extended prompt section")
    reloaded = RecordDatabase(str(path))
    with closing(reloaded._connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM generation_records_fts_vocab").fetchone()[0] > 0


def test_existing_unicode61_table_uses_scalar_search(tmp_path):
    path = tmp_path / "records.sqlite3"
    database = RecordDatabase(str(path))
    database.upsert_record({"id": "unicode", "prompt": "substring 设计团队"})
    if not database._fts5_enabled:
        pytest.skip("SQLite build has no trigram FTS5")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE generation_records_fts")
        connection.execute(
            "CREATE VIRTUAL TABLE generation_records_fts USING fts5("
            "record_id UNINDEXED, search_text, tokenize='unicode61')"
        )
        connection.execute(
            "INSERT INTO generation_records_fts VALUES (?, ?)", ("unicode", "substring 设计团队")
        )
    reloaded = RecordDatabase(str(path))
    assert not reloaded._fts5_enabled
    assert [row["id"] for row in reloaded.query_records(keyword="string")[0]] == ["unicode"]
    assert [row["id"] for row in reloaded.query_records(keyword="计团队")[0]] == ["unicode"]


def test_fts_pruning_clear_and_restart_keep_projection_in_sync(tmp_path):
    path = tmp_path / "records.sqlite3"
    database = RecordDatabase(str(path))
    if not database._fts5_enabled:
        pytest.skip("SQLite build has no trigram FTS5")
    database.replace_records([
        {"id": "new", "time": "2026-11-14", "prompt": "new mountain"},
        {"id": "old", "time": "2026-11-13", "prompt": "old mountain"},
        {"id": "pinned", "time": "2026-11-12", "pinned": True, "prompt": "pinned mountain"},
    ])
    assert database.prune_to_limit(1) == ["old"]
    reloaded = RecordDatabase(str(path))
    assert {row["id"] for row in reloaded.query_records(keyword="mountain")[0]} == {"new", "pinned"}
    with sqlite3.connect(path) as connection:
        assert {row[0] for row in connection.execute("SELECT record_id FROM generation_records_fts")} == {"new", "pinned"}
        assert {row[0] for row in connection.execute(
            "SELECT record_id FROM generation_record_prompt_prefix"
        )} == {"new", "pinned"}
    reloaded.clear()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM generation_records_fts").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM generation_record_prompt_prefix"
        ).fetchone()[0] == 0
    assert RecordDatabase(str(path)).query_records(keyword="mountain")[0] == []


def test_fts_query_failure_falls_back_and_rebuilds_after_disabled_same_count_write(tmp_path):
    path = tmp_path / "records.sqlite3"
    database = RecordDatabase(str(path))
    database.upsert_record({"id": "record", "prompt": "original mountain"})
    if not database._fts5_enabled:
        pytest.skip("SQLite build has no trigram FTS5")
    # A same-shape plain table lets maintenance succeed but MATCH fail,
    # exercising the runtime fallback without losing the original base row.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE generation_records_fts")
        connection.execute("CREATE TABLE generation_records_fts (record_id TEXT, search_text TEXT)")
        connection.execute("INSERT INTO generation_records_fts VALUES ('record', 'original mountain')")
    assert [row["id"] for row in database.query_records(keyword="mountain")[0]] == ["record"]
    assert not database._fts5_enabled
    database.upsert_record({"id": "record", "prompt": "replacement forest"})
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT valid FROM generation_records_fts_state").fetchone()[0] == 0
        connection.execute("DROP TABLE generation_records_fts")
        connection.execute(
            "CREATE VIRTUAL TABLE generation_records_fts USING fts5("
            "record_id UNINDEXED, search_text, tokenize='trigram')"
        )
        # Keep row counts equal so recovery depends on the invalid marker.
        connection.execute("INSERT INTO generation_records_fts VALUES ('record', 'original mountain')")
    reloaded = RecordDatabase(str(path))
    assert reloaded._fts5_enabled
    assert reloaded.query_records(keyword="mountain")[0] == []
    assert [row["id"] for row in reloaded.query_records(keyword="forest")[0]] == ["record"]


def test_fts_rebuild_failure_does_not_abort_record_replacement(tmp_path, monkeypatch):
    path = tmp_path / "records.sqlite3"
    database = RecordDatabase(str(path))
    if not database._fts5_enabled:
        pytest.skip("SQLite build has no trigram FTS5")
    database.upsert_record({"id": "old", "prompt": "old prompt"})

    def fail(_connection):
        raise sqlite3.OperationalError("simulated FTS maintenance error")

    monkeypatch.setattr(database, "_rebuild_fts", fail)
    database.replace_records([{"id": "new", "prompt": "new prompt"}])
    assert not database._fts5_enabled
    assert [row["id"] for row in database.query_records(keyword="new prompt")[0]] == ["new"]
    assert [row["id"] for row in RecordDatabase(str(path)).query_records(keyword="new prompt")[0]] == ["new"]
