from __future__ import annotations

import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


_SPEC = spec_from_file_location(
    "benchmark_record_database",
    Path(__file__).resolve().parents[1] / "scripts" / "benchmark_record_database.py",
)
_BENCHMARK = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BENCHMARK)

(
    BUNDLED_ABSENT_MARKER,
    BUNDLED_PREFIX_MARKER,
    BUNDLED_TAIL_MARKER,
    _build_fixture,
    _bundled_natural_keywords,
    _load_bundled_prompt_corpus,
    _search_mode,
) = (
    _BENCHMARK.BUNDLED_ABSENT_MARKER,
    _BENCHMARK.BUNDLED_PREFIX_MARKER,
    _BENCHMARK.BUNDLED_TAIL_MARKER,
    _BENCHMARK._build_fixture,
    _BENCHMARK._bundled_natural_keywords,
    _BENCHMARK._load_bundled_prompt_corpus,
    _BENCHMARK._search_mode,
)
from astrbot_plugin_selfie_image.generation.record_database import RecordDatabase


def test_bundled_fixture_is_seeded_and_reports_prompt_distribution():
    first, first_metadata = _build_fixture(100, 96, "bundled-assets", 73131)
    second, second_metadata = _build_fixture(100, 96, "bundled-assets", 73131)

    assert first == second
    assert first_metadata == second_metadata
    assert first_metadata["prompt_profile"] == "bundled-assets"
    assert first_metadata["corpus"]["entries"] > 1000
    assert first_metadata["distinct_prompt_payload_fraction"] == 1.0
    assert first_metadata["controlled_marker_match_fraction"] == {
        "prefix": 0.1,
        "post_prefix": 0.1,
    }
    assert all("original_prompt" in record and "request_prompt" in record for record in first)
    assert sum(BUNDLED_PREFIX_MARKER in record["original_prompt"] for record in first) == 10
    assert sum(BUNDLED_TAIL_MARKER in record["request_prompt"] for record in first) == 10
    tail_record = first[1]
    joined_prompts = " ".join(
        tail_record.get(field, "")
        for field in ("original_prompt", "request_prompt", "prompt")
    )
    assert BUNDLED_TAIL_MARKER not in joined_prompts[:128]
    assert BUNDLED_TAIL_MARKER in joined_prompts[128:]


def test_bundled_keyword_scenarios_match_scalar_results(tmp_path):
    records, _metadata = _build_fixture(100, 96, "bundled-assets", 73131)
    corpus, _corpus_metadata = _load_bundled_prompt_corpus()
    keywords = _bundled_natural_keywords(corpus)
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    database.replace_records(records)

    scenarios = [
        (BUNDLED_PREFIX_MARKER, 10),
        (BUNDLED_TAIL_MARKER, 10),
        (BUNDLED_ABSENT_MARKER, 0),
        *((keyword, None) for keyword in keywords.values()),
    ]
    for keyword, expected_matches in scenarios:
        options = {"keep_limit": 100, "limit": 100, "keyword": keyword}
        adaptive = database.query_records(**options)
        with _search_mode(database, "scalar"):
            scalar = database.query_records(**options)
        assert adaptive == scalar
        if expected_matches is not None:
            assert adaptive[1]["filtered"] == expected_matches


def test_latency_and_connection_summaries_cover_tail_and_empty_samples():
    latency = _BENCHMARK._latencies([5.0, 1.0, 3.0, 10.0])

    assert latency["samples"] == 4
    assert latency["p50_ms"] == 4.0
    assert latency["p95_ms"] == 10.0
    assert latency["p99_ms"] == 10.0
    assert latency["max_ms"] == 10.0
    assert _BENCHMARK._latencies([]) == {
        "samples": 0,
        "percentile_method": "nearest-rank",
    }
    assert _BENCHMARK._connection_summary(2, 5) == {
        "operations": 5,
        "connections_opened": 2,
        "connections_reused": 3,
        "connection_reuse_rate": 0.6,
    }
    assert _BENCHMARK._connection_summary(0, 0)["connection_reuse_rate"] is None


def test_benchmark_report_includes_resource_and_connection_metrics(monkeypatch, tmp_path):
    report_path = tmp_path / "record-db.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_record_database.py",
            "--records",
            "20",
            "--samples",
            "3",
            "--output",
            str(report_path),
        ],
    )

    assert _BENCHMARK.main() == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    recent = report["queries"]["recent"]

    assert "max_rss_bytes" in report["environment"]
    assert recent["p99_ms"] >= recent["p95_ms"]
    assert recent["max_ms"] >= recent["p99_ms"]
    assert recent["operations"] == 3
    assert recent["connections_opened"] >= 1
    assert 0 <= recent["connection_reuse_rate"] <= 1
