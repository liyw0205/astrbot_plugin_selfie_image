"""Measure record-query latency using a temporary, synthetic SQLite database."""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_selfie_image.generation.record_database import RecordDatabase


LONG_PROMPT_MARKER = "extended prompt section"
_PROMPT_FILLER = f"{LONG_PROMPT_MARKER}, layered lighting, detailed environment, "


def _prompt_for(index: int, target_chars: int) -> str:
    prompt = f"studio portrait {index}, soft light, detailed background"
    if target_chars > len(prompt):
        needed = target_chars - len(prompt)
        prompt += (_PROMPT_FILLER * math.ceil(needed / len(_PROMPT_FILLER)))[:needed]
    if index % 997 == 0:
        prompt += " rare-needle"
    return prompt


def _drop_os_file_cache(path: str) -> str:
    """Request best-effort page-cache eviction; kernels may ignore the hint."""
    advise = getattr(os, "posix_fadvise", None)
    dontneed = getattr(os, "POSIX_FADV_DONTNEED", None)
    if not callable(advise) or dontneed is None:
        return "unsupported"
    advised = 0
    try:
        for candidate in (path, path + "-wal"):
            if not os.path.isfile(candidate):
                continue
            descriptor = os.open(candidate, os.O_RDONLY)
            try:
                advise(descriptor, 0, 0, dontneed)
                advised += 1
            finally:
                os.close(descriptor)
    except OSError as exc:
        return f"error:{type(exc).__name__}"
    return f"requested:{advised}" if advised else "no-readable-database-files"


def _latencies(durations: list[float]) -> dict:
    return {
        "samples": len(durations),
        "percentile_method": "nearest-rank",
        "p50_ms": round(statistics.median(durations), 3),
        "p95_ms": round(sorted(durations)[math.ceil(len(durations) * 0.95) - 1], 3),
        "durations_ms": [round(value, 3) for value in durations],
    }


def _latency_summary(durations: list[float], rows: list[dict], metadata: dict) -> dict:
    return {
        **_latencies(durations),
        "retained": metadata["total"],
        "matched": metadata["filtered"],
        "returned": len(rows),
    }


def _measure_queries(database: RecordDatabase, options: dict, samples: int) -> dict:
    durations = []
    rows = []
    metadata = {}
    for _ in range(samples):
        started = time.perf_counter()
        rows, metadata = database.query_records(**options)
        durations.append((time.perf_counter() - started) * 1000)
    return _latency_summary(durations, rows, metadata)


@contextmanager
def _search_mode(database: RecordDatabase, mode: str):
    enabled = database._fts5_enabled
    try:
        if mode == "scalar":
            database._fts5_enabled = False
        elif mode == "phrase":
            database._fts_selective_match_query = (
                lambda _connection, keyword, _retained: '"' + keyword.replace('"', '""') + '"'
            )
        yield
    finally:
        database._fts5_enabled = enabled
        if mode == "phrase":
            del database._fts_selective_match_query


def _checkpoint(path: str) -> list[int]:
    with closing(sqlite3.connect(path)) as connection:
        status = list(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
    if status[0]:
        raise RuntimeError(f"benchmark checkpoint was busy: {status}")
    return status


def _file_sizes(path: str) -> dict:
    return {
        name: os.path.getsize(candidate) if os.path.isfile(candidate) else 0
        for name, candidate in (("database_bytes", path), ("wal_bytes", path + "-wal"), ("shm_bytes", path + "-shm"))
    }


def _storage_summary(path: str) -> dict:
    result = _file_sizes(path)
    with closing(sqlite3.connect(path)) as connection:
        try:
            result["fts_table_bytes"] = connection.execute(
                "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name GLOB 'generation_records_fts*'"
            ).fetchone()[0]
        except sqlite3.DatabaseError:
            result["fts_table_bytes"] = None
        try:
            result["record_index_bytes"] = connection.execute(
                "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat "
                "WHERE name GLOB 'idx_generation_records_*'"
            ).fetchone()[0]
        except sqlite3.DatabaseError:
            result["record_index_bytes"] = None
    return result


def _measure_concurrent_writes(
    reader: RecordDatabase,
    options_by_name: dict[str, dict],
    *,
    samples: int,
    prompt_chars: int,
    next_sequence: int,
    shared_instance: bool,
) -> dict:
    writer = reader if shared_instance else RecordDatabase(reader.path)
    writer_started = threading.Event()
    stop_writer = threading.Event()
    state = {"writes": 0, "errors": [], "durations": []}
    state_lock = threading.Lock()

    def write_loop() -> None:
        while not stop_writer.is_set():
            with state_lock:
                index = state["writes"]
            try:
                started = time.perf_counter()
                writer.upsert_record(
                    {
                        "id": "benchmark-writer-probe",
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "source": "benchmark-writer",
                        "used_model": "model-7",
                        "original_prompt": _prompt_for(index + next_sequence, prompt_chars),
                    },
                    sequence=next_sequence + index,
                )
                duration = (time.perf_counter() - started) * 1000
                with state_lock:
                    state["writes"] += 1
                    state["durations"].append(duration)
                writer_started.set()
            except Exception as exc:  # Report contention failures instead of hiding them.
                with state_lock:
                    state["errors"].append(type(exc).__name__)
                writer_started.set()
                break
            stop_writer.wait(0.001)

    thread = threading.Thread(target=write_loop, name="record-db-benchmark-writer")
    thread.start()
    if not writer_started.wait(timeout=30):
        stop_writer.set()
        thread.join()
        raise RuntimeError("concurrent benchmark writer did not start")

    query_results = {}
    measurement_started = time.perf_counter()
    try:
        for name, options in options_by_name.items():
            with state_lock:
                writes_before = state["writes"]
            started = time.perf_counter()
            result = _measure_queries(reader, options, samples)
            with state_lock:
                writes_after = state["writes"]
                write_durations = state["durations"][writes_before:writes_after]
            result.update({
                "measurement_seconds": round(time.perf_counter() - started, 6),
                "writes_before": writes_before,
                "writes_after": writes_after,
                "writes_completed_during_window": writes_after - writes_before,
                "write_latency": _latencies(write_durations) if write_durations else None,
                "overlap_observed": writes_after > writes_before,
            })
            query_results[name] = result
    finally:
        stop_writer.set()
        # Do not remove the temporary DB while a writer is still using it,
        # including when a reader raised before completing its samples.
        thread.join()
    if state["errors"]:
        raise RuntimeError(f"concurrent benchmark writer failed: {state['errors']}")
    return {
        "queries": query_results, "successful_writes": state["writes"],
        "write_latency": _latencies(state["durations"]),
        "measurement_seconds": round(time.perf_counter() - measurement_started, 6),
        "writer_pause_seconds": 0.001, "errors": state["errors"],
        "model": "shared_instance_rlock" if shared_instance else "independent_instances_wal_stress",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=100000)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--keep-limit", type=int, default=10000)
    parser.add_argument("--prompt-chars", type=int, default=96)
    parser.add_argument("--cold-cache-samples", type=int, default=0)
    parser.add_argument("--concurrent-samples", type=int, default=0)
    parser.add_argument("--compare-phrase", action="store_true", help="also time the previous full-phrase FTS candidate query")
    args = parser.parse_args()
    if (
        args.records < 1 or args.samples < 3 or not 1 <= args.keep_limit <= 10000
        or args.prompt_chars < 1 or args.cold_cache_samples < 0 or args.concurrent_samples < 0
    ):
        parser.error("records/prompt-chars must be positive, samples >= 3, keep-limit in 1..10000, optional sample counts non-negative")

    epoch = datetime(2026, 1, 1)
    # Protected records can exceed normal retention. At the default size,
    # 90,000 favorites plus 10,000 normal records exercise the full corpus.
    records = [{
        "id": f"record-{index}",
        "time": (epoch + timedelta(seconds=index)).strftime("%Y-%m-%d %H:%M:%S"),
        "source": f"group-{index % 50}",
        "source_label": f"Group {index % 50}",
        "used_model": f"model-{index % 20}",
        "success": index % 7 != 0,
        "favorite": index % 10 != 0,
        "original_prompt": _prompt_for(index, args.prompt_chars),
    } for index in range(args.records)]
    prompt_lengths = [len(record["original_prompt"]) for record in records]
    with tempfile.TemporaryDirectory(prefix="selfie-record-bench-") as temporary:
        database = RecordDatabase(str(Path(temporary) / "records.sqlite3"))
        started = time.perf_counter()
        database.replace_records(reversed(records))
        setup_seconds = round(time.perf_counter() - started, 3)
        setup_storage = _storage_summary(database.path)
        fts_enabled = database._fts5_enabled
        queries = {
            "recent": ({}, "adaptive"),
            "favorite": ({"favorite": True}, "adaptive"),
            "model": ({"model": "model-7"}, "adaptive"),
            "keyword": ({"keyword": "rare-needle"}, "adaptive"),
            "keyword_scalar": ({"keyword": "rare-needle"}, "scalar"),
        }
        if args.prompt_chars >= 256:
            queries["long_prompt_keyword"] = ({"keyword": LONG_PROMPT_MARKER}, "adaptive")
            queries["long_prompt_keyword_scalar"] = ({"keyword": LONG_PROMPT_MARKER}, "scalar")
            if args.compare_phrase:
                queries["long_prompt_keyword_phrase"] = ({"keyword": LONG_PROMPT_MARKER}, "phrase")
        results = {}
        keyword_results = {}
        for label, (filters, mode) in queries.items():
            options = {"keep_limit": args.keep_limit, "limit": 30, **filters}
            with _search_mode(database, mode):
                database.query_records(**options)
                summary = _measure_queries(database, options, args.samples)
                rows, metadata = database.query_records(**options)
                if mode != "scalar" and fts_enabled and not database._fts5_enabled:
                    raise RuntimeError(f"FTS unexpectedly fell back during {label}")
            if "keyword" in filters:
                previous = keyword_results.setdefault(filters["keyword"], (rows, metadata))
                if previous != (rows, metadata):
                    raise RuntimeError(f"FTS and scalar results differ for {label}")
            summary["candidate_mode"] = mode if fts_enabled else "scalar_unavailable_fts"
            results[label] = summary

        cache_results = {}
        if args.cold_cache_samples:
            cache_queries = {"recent": {}, "keyword": {"keyword": "rare-needle"}}
            if args.prompt_chars >= 256:
                cache_queries["long_prompt_keyword"] = {"keyword": LONG_PROMPT_MARKER}
            for name, filters in cache_queries.items():
                options = {"keep_limit": args.keep_limit, "limit": 30, **filters}
                database.query_records(**options)
                warm = _measure_queries(database, options, args.samples)
                cold_durations = []
                eviction_modes = []
                checkpoints = []
                file_states = []
                for _ in range(args.cold_cache_samples):
                    checkpoints.append(_checkpoint(database.path))
                    file_states.append(_file_sizes(database.path))
                    eviction_modes.append(_drop_os_file_cache(database.path))
                    started = time.perf_counter()
                    rows, metadata = database.query_records(**options)
                    cold_durations.append((time.perf_counter() - started) * 1000)
                cache_results[name] = {
                    "warm_queries": warm,
                    "best_effort_file_cache_evict": _latency_summary(cold_durations, rows, metadata),
                    "eviction_requests": eviction_modes,
                    "checkpoint_status": checkpoints,
                    "files_before_evict": file_states,
                }

        concurrent_results = {}
        if args.concurrent_samples:
            concurrent_queries = {
                name: {"keep_limit": args.keep_limit, "limit": 30, **filters}
                for name, (filters, mode) in queries.items()
                if mode == "adaptive"
            }
            for shared in (True, False):
                name = "shared_instance" if shared else "independent_instances"
                concurrent_results[name] = _measure_concurrent_writes(
                    database,
                    concurrent_queries,
                    samples=args.concurrent_samples,
                    prompt_chars=args.prompt_chars,
                    next_sequence=args.records + 1,
                    shared_instance=shared,
                )
                database.delete_records(["benchmark-writer-probe"])
        _checkpoint(database.path)
        print(json.dumps({
            "environment": {
                "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                "platform": platform.platform(),
            },
            "fixture": {
                "records": args.records, "samples": args.samples,
                "keep_limit": args.keep_limit, "favorite_fraction": 0.9,
                "prompt_chars": args.prompt_chars,
                "actual_prompt_chars": {"min": min(prompt_lengths), "max": max(prompt_lengths)},
                "prompt_distribution": "low-entropy repeated filler; rare-needle every 997 records; long marker matches all extended prompts",
                "fts_enabled": fts_enabled, "setup_seconds": setup_seconds,
                "storage_after_setup": setup_storage,
            },
            "storage_after_benchmark": _storage_summary(database.path),
            "methodology": {
                "connection_lifetime": "each operation commits/rolls back and closes; no anchor connection",
                "cache_evict": "checkpoint and close, then POSIX_FADV_DONTNEED best-effort hint; does not prove cold storage",
                "statistics": "nearest-rank p95 is the maximum with fewer than 20 samples; synthetic device-local evidence only",
                "concurrency": "shared instance models runtime RLock; independent instances model additional WAL contention; inspect overlap per query",
            },
            "queries": results,
            "cache_scenarios": cache_results,
            "concurrent_writes": concurrent_results,
        }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
