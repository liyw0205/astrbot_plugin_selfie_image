"""Measure record-query latency using a temporary, synthetic SQLite database."""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import threading
import time

try:
    import resource
except ImportError:  # pragma: no cover - platform-dependent stdlib module
    resource = None


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_selfie_image.generation.record_database import RecordDatabase


LONG_PROMPT_MARKER = "extended prompt section"
_PROMPT_FILLER = f"{LONG_PROMPT_MARKER}, layered lighting, detailed environment, "
BUNDLED_PREFIX_MARKER = "bundle-prefix-probe"
BUNDLED_TAIL_MARKER = "bundle-tail-probe"
BUNDLED_ABSENT_MARKER = "bundle-absent-probe"
_PROMPT_CORPUS_DIRS = (
    "data/common", "data/cos", "data/group", "data/legs", "data/nai",
    "data/schedule", "data/studio",
)


def _prompt_for(index: int, target_chars: int) -> str:
    prompt = f"studio portrait {index}, soft light, detailed background"
    if target_chars > len(prompt):
        needed = target_chars - len(prompt)
        prompt += (_PROMPT_FILLER * math.ceil(needed / len(_PROMPT_FILLER)))[:needed]
    if index % 997 == 0:
        prompt += " rare-needle"
    return prompt


def _collect_prompt_entries(value, source: str, entries: list[tuple[str, str]]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_prompt_entries(item, source, entries)
        return
    if not isinstance(value, dict):
        return
    prompt = value.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        entries.append((prompt.strip(), source))
    if all(isinstance(value.get(key), str) for key in ("status", "scene", "action")):
        entries.append((" ".join(value[key].strip() for key in ("status", "scene", "action")), source))
    for item in value.values():
        _collect_prompt_entries(item, source, entries)


def _load_bundled_prompt_corpus(root: Path = ROOT) -> tuple[list[tuple[str, str]], dict]:
    entries = []
    files = sorted(
        path
        for directory in _PROMPT_CORPUS_DIRS
        for path in (root / directory).rglob("*.json")
    )
    for path in files:
        relative = str(path.relative_to(root))
        _collect_prompt_entries(json.loads(path.read_text(encoding="utf-8")), relative, entries)
        if relative == "data/common/selfie_snapshots.json":
            snapshots = json.loads(path.read_text(encoding="utf-8"))
            entries.extend(
                (item.strip(), relative)
                for item in snapshots.get("items", [])
                if isinstance(item, str) and item.strip()
            )
    if not entries:
        raise RuntimeError("bundled prompt corpus is empty")
    digest_input = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    texts = [text for text, _source in entries]
    unique_texts = sorted(set(texts))
    return entries, {
        "source": "bundled prompt fields and schedule status/scene/action from repository data JSON",
        "files": len(files),
        "entries": len(entries),
        "unique_entries": len(unique_texts),
        "sha256": hashlib.sha256(digest_input).hexdigest(),
        "source_files": len({source for _text, source in entries}),
        "text_length_chars": _length_distribution([len(text) for text in texts]),
    }


def _length_distribution(values: list[int]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "min": ordered[0],
        "p50": ordered[math.ceil(len(ordered) * 0.50) - 1],
        "p90": ordered[math.ceil(len(ordered) * 0.90) - 1],
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "p99": ordered[math.ceil(len(ordered) * 0.99) - 1],
        "max": ordered[-1],
    }


def _bundled_prompt_fields(index: int, corpus: list[str], rng: random.Random) -> dict[str, str]:
    original = rng.choice(corpus)
    request = original
    if rng.random() < 0.35:
        request = f"{request}，{rng.choice(corpus)}"

    fields = {"original_prompt": original, "request_prompt": request}
    if index % 10 == 0:
        fields["original_prompt"] = f"{BUNDLED_PREFIX_MARKER} {original}"
    elif index % 10 == 1:
        padding = ""
        while len(fields["original_prompt"]) + len(padding) <= 160:
            padding += f"{rng.choice(corpus)}，"
        fields["request_prompt"] = f"{padding}{BUNDLED_TAIL_MARKER}，{request}"
    if index % 997 == 0:
        fields["request_prompt"] = f"{fields['request_prompt']} rare-needle"
    if index % 7 == 0:
        fields["prompt"] = fields["request_prompt"]
    return fields


def _build_fixture(
    record_count: int,
    prompt_chars: int,
    prompt_profile: str,
    seed: int,
    root: Path = ROOT,
) -> tuple[list[dict], dict]:
    corpus_entries = []
    corpus_metadata = None
    rng = random.Random(seed)
    if prompt_profile == "bundled-assets":
        corpus_entries, corpus_metadata = _load_bundled_prompt_corpus(root)
        corpus = [text for text, _source in corpus_entries]
    else:
        corpus = []

    records = []
    prompt_lengths = []
    prompt_payloads = set()
    epoch = datetime(2026, 1, 1)
    for index in range(record_count):
        prompt_fields = (
            _bundled_prompt_fields(index, corpus, rng)
            if prompt_profile == "bundled-assets"
            else {"original_prompt": _prompt_for(index, prompt_chars)}
        )
        prompt_payload = tuple(prompt_fields.get(key, "") for key in ("original_prompt", "request_prompt", "prompt"))
        prompt_payloads.add(prompt_payload)
        prompt_lengths.append(sum(map(len, prompt_payload)))
        records.append({
            "id": f"record-{index}",
            "time": (epoch + timedelta(seconds=index)).strftime("%Y-%m-%d %H:%M:%S"),
            "source": f"group-{index % 50}",
            "source_label": f"Group {index % 50}",
            "used_model": f"model-{index % 20}",
            "success": index % 7 != 0,
            "favorite": index % 10 != 0,
            **prompt_fields,
        })

    metadata = {
        "records": record_count,
        "prompt_profile": prompt_profile,
        "seed": seed,
        "prompt_payload_lengths_chars": _length_distribution(prompt_lengths),
        "distinct_prompt_payloads": len(prompt_payloads),
        "distinct_prompt_payload_fraction": round(len(prompt_payloads) / record_count, 6),
    }
    if prompt_profile == "filler":
        metadata.update({
            "keep_limit_default": 10000,
            "favorite_fraction": 0.9,
            "prompt_chars": prompt_chars,
            "prompt_distribution": "low-entropy repeated filler; rare-needle every 997 records; long marker matches all extended prompts",
        })
    else:
        metadata.update({
            "favorite_fraction": 0.9,
            "corpus": corpus_metadata,
            "prompt_distribution": (
                "seeded repository-asset-derived synthetic prompts; 35% append a second corpus phrase; "
                "original_prompt/request_prompt are both indexed and prompt is duplicated every seventh record; "
                "controlled 10% prefix and 10% post-prefix marker probes"
            ),
            "controlled_marker_match_fraction": {"prefix": 0.1, "post_prefix": 0.1},
        })
    return records, metadata


def _bundled_natural_keywords(corpus: list[tuple[str, str]]) -> dict[str, str]:
    candidates = ("自然光", "看向镜头", "成年女性", "高质量", "保持身份", "服装细节", "光线", "自然")
    texts = [text.lower() for text, _source in corpus]
    counts = {
        keyword: sum(keyword.lower() in text for text in texts)
        for keyword in candidates
    }
    available = [keyword for keyword, count in counts.items() if 0 < count < len(texts)]
    fts_terms = [keyword for keyword in available if len(keyword) >= 3]
    selected = {}
    if fts_terms:
        selected["narrow"] = min(fts_terms, key=lambda keyword: counts[keyword])
        selected["broad"] = max(fts_terms, key=lambda keyword: counts[keyword])
    short_terms = [keyword for keyword in available if len(keyword) < 3]
    if short_terms:
        selected["short"] = max(short_terms, key=lambda keyword: counts[keyword])
    return selected


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
    ordered = sorted(durations)
    if not ordered:
        return {"samples": 0, "percentile_method": "nearest-rank"}

    def percentile(fraction: float) -> float:
        return ordered[math.ceil(len(ordered) * fraction) - 1]

    return {
        "samples": len(ordered),
        "percentile_method": "nearest-rank",
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(percentile(0.95), 3),
        "p99_ms": round(percentile(0.99), 3),
        "max_ms": round(ordered[-1], 3),
        "durations_ms": [round(value, 3) for value in durations],
    }


@contextmanager
def _connection_meter(database: RecordDatabase):
    """Count connection creation without changing the production class."""
    original = database._connect
    state = {"opened": 0}

    def counted_connect():
        state["opened"] += 1
        return original()

    database._connect = counted_connect
    try:
        yield state
    finally:
        database._connect = original


def _connection_summary(opened: int, operations: int) -> dict:
    operations = max(0, int(operations))
    opened = max(0, int(opened))
    reused = max(0, operations - opened)
    return {
        "operations": operations,
        "connections_opened": opened,
        "connections_reused": reused,
        "connection_reuse_rate": round(reused / operations, 6) if operations else None,
    }


def _max_rss_bytes() -> int | None:
    """Return a process peak-RSS reading with platform units normalized."""
    if resource is None:
        return None
    try:
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (AttributeError, OSError, ValueError):
        return None
    # Linux/Android expose KiB; macOS exposes bytes.
    return value if sys.platform == "darwin" else value * 1024


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
    storage_before = _file_sizes(reader.path)
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
                "write_progress_observed": writes_after > writes_before,
            })
            query_results[name] = result
    finally:
        stop_writer.set()
        # Do not remove the temporary DB while a writer is still using it,
        # including when a reader raised before completing its samples.
        thread.join()
    if state["errors"]:
        raise RuntimeError(f"concurrent benchmark writer failed: {state['errors']}")
    storage_after = _file_sizes(reader.path)
    storage_delta = max(
        0,
        sum(storage_after.values()) - sum(storage_before.values()),
    )
    return {
        "queries": query_results, "successful_writes": state["writes"],
        "write_latency": _latencies(state["durations"]),
        "storage_bytes_before": sum(storage_before.values()),
        "storage_bytes_after": sum(storage_after.values()),
        "storage_delta_bytes": storage_delta,
        "write_amplification_bytes_per_write": round(
            storage_delta / state["writes"], 3
        ) if state["writes"] else None,
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
    parser.add_argument("--prompt-profile", choices=("filler", "bundled-assets"), default="filler")
    parser.add_argument("--seed", type=int, default=73131)
    parser.add_argument("--cold-cache-samples", type=int, default=0)
    parser.add_argument("--concurrent-samples", type=int, default=0)
    parser.add_argument("--output", default="", help="write the JSON report to this path instead of stdout")
    parser.add_argument("--compare-phrase", action="store_true", help="also time the previous full-phrase FTS candidate query")
    args = parser.parse_args()
    if (
        args.records < 1 or args.samples < 3 or not 1 <= args.keep_limit <= 10000
        or args.prompt_chars < 1 or args.cold_cache_samples < 0 or args.concurrent_samples < 0
    ):
        parser.error("records/prompt-chars must be positive, samples >= 3, keep-limit in 1..10000, optional sample counts non-negative")

    # Protected records can exceed normal retention. At the default size,
    # 90,000 favorites plus 10,000 normal records exercise the full corpus.
    records, fixture = _build_fixture(
        args.records, args.prompt_chars, args.prompt_profile, args.seed,
    )
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
        if args.prompt_profile == "filler" and args.prompt_chars >= 256:
            queries["long_prompt_keyword"] = ({"keyword": LONG_PROMPT_MARKER}, "adaptive")
            queries["long_prompt_keyword_scalar"] = ({"keyword": LONG_PROMPT_MARKER}, "scalar")
            if args.compare_phrase:
                queries["long_prompt_keyword_phrase"] = ({"keyword": LONG_PROMPT_MARKER}, "phrase")
        bundled_keywords = {}
        if args.prompt_profile == "bundled-assets":
            corpus, _corpus_metadata = _load_bundled_prompt_corpus()
            bundled_keywords = _bundled_natural_keywords(corpus)
            bundled_queries = {
                "bundled_prefix_partial": BUNDLED_PREFIX_MARKER,
                "bundled_tail_partial": BUNDLED_TAIL_MARKER,
                "bundled_absent": BUNDLED_ABSENT_MARKER,
            }
            bundled_queries.update({
                f"bundled_natural_{name}": keyword
                for name, keyword in bundled_keywords.items()
            })
            for name, keyword in bundled_queries.items():
                queries[name] = ({"keyword": keyword}, "adaptive")
                queries[f"{name}_scalar"] = ({"keyword": keyword}, "scalar")
        results = {}
        keyword_results = {}
        for label, (filters, mode) in queries.items():
            options = {"keep_limit": args.keep_limit, "limit": 30, **filters}
            with _search_mode(database, mode):
                database.query_records(**options)
                with _connection_meter(database) as connection_meter:
                    summary = _measure_queries(database, options, args.samples)
                rows, metadata = database.query_records(**options)
                if mode != "scalar" and fts_enabled and not database._fts5_enabled:
                    raise RuntimeError(f"FTS unexpectedly fell back during {label}")
            summary.update(_connection_summary(connection_meter["opened"], args.samples))
            if "keyword" in filters:
                previous = keyword_results.setdefault(filters["keyword"], (rows, metadata))
                if previous != (rows, metadata):
                    raise RuntimeError(f"FTS and scalar results differ for {label}")
            summary["candidate_mode"] = mode if fts_enabled else "scalar_unavailable_fts"
            results[label] = summary

        cache_results = {}
        if args.cold_cache_samples:
            cache_queries = {"recent": {}, "keyword": {"keyword": "rare-needle"}}
            if args.prompt_profile == "filler" and args.prompt_chars >= 256:
                cache_queries["long_prompt_keyword"] = {"keyword": LONG_PROMPT_MARKER}
            if args.prompt_profile == "bundled-assets":
                cache_queries["bundled_prefix_partial"] = {"keyword": BUNDLED_PREFIX_MARKER}
                cache_queries["bundled_tail_partial"] = {"keyword": BUNDLED_TAIL_MARKER}
                if "broad" in bundled_keywords:
                    cache_queries["bundled_natural_broad"] = {
                        "keyword": bundled_keywords["broad"],
                    }
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
                    with _connection_meter(database) as connection_meter:
                        rows, metadata = database.query_records(**options)
                    cold_durations.append((time.perf_counter() - started) * 1000)
                cache_results[name] = {
                    "warm_queries": warm,
                    "best_effort_file_cache_evict": _latency_summary(cold_durations, rows, metadata),
                    "connection_metrics": _connection_summary(
                        connection_meter["opened"], args.cold_cache_samples
                    ),
                    "eviction_requests": eviction_modes,
                    "checkpoint_status": checkpoints,
                    "files_before_evict": file_states,
                }

        concurrent_results = {}
        if args.concurrent_samples:
            concurrent_names = {
                "recent", "favorite", "model", "keyword", "bundled_prefix_partial",
                "bundled_tail_partial", "bundled_natural_broad",
            }
            concurrent_queries = {
                name: {"keep_limit": args.keep_limit, "limit": 30, **filters}
                for name, (filters, mode) in queries.items()
                if mode == "adaptive" and (args.prompt_profile == "filler" or name in concurrent_names)
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
        report = json.dumps({
            "environment": {
                "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                "platform": platform.platform(), "max_rss_bytes": _max_rss_bytes(),
            },
            "fixture": {
                **fixture,
                "samples": args.samples,
                "keep_limit": args.keep_limit, "favorite_fraction": 0.9,
                "natural_keyword_candidates": bundled_keywords,
                "fts_enabled": fts_enabled, "setup_seconds": setup_seconds,
                "storage_after_setup": setup_storage,
            },
            "storage_after_benchmark": _storage_summary(database.path),
            "methodology": {
                "connection_lifetime": "each operation commits/rolls back and closes; no anchor connection",
                "connection_reuse": "counted around measured query samples; current implementation opens one SQLite connection per operation",
                "write_amplification": "database/WAL/SHM size delta divided by successful concurrent writes; approximate storage-level indicator",
                "cache_evict": "checkpoint and close, then POSIX_FADV_DONTNEED best-effort hint; does not prove cold storage",
                "statistics": "nearest-rank p95/p99 are maxima with fewer than 20 samples; synthetic device-local evidence only",
                "concurrency": "shared instance RLock serializes query/write calls; write_progress_observed means writes completed in the query window, not simultaneous SQL execution; independent instances model additional WAL contention",
            },
            "queries": results,
            "cache_scenarios": cache_results,
            "concurrent_writes": concurrent_results,
        }, indent=2)
        if args.output:
            destination = Path(args.output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(report + "\n", encoding="utf-8")
            print(f"benchmark report written: {destination}")
        else:
            print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
