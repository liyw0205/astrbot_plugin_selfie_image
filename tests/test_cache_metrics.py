"""Cache diagnostics use the same file snapshot as cleanup decisions."""

from __future__ import annotations

import os
from pathlib import Path
import threading
from types import SimpleNamespace

from astrbot_plugin_selfie_image.core.providers import ImageReference
from astrbot_plugin_selfie_image.generation.generation_store import GenerationStoreMixin
from astrbot_plugin_selfie_image.generation.record_metrics import CacheMetrics
from astrbot_plugin_selfie_image.webui.services import build_health_payload


def cache_store(root: Path) -> GenerationStoreMixin:
    store = GenerationStoreMixin()
    store.data_dir = str(root)
    store.generated_dir = str(root / "image_cache")
    store.records_path = str(root / "records.json")
    store.records_db_path = str(root / "records.sqlite3")
    store.media_sources_dir = str(root / "media_sources")
    store.config = SimpleNamespace(image_cache_limit_mb=10, image_cache_limit_count=10, image_record_keep_limit=10)
    store._records_lock = threading.RLock()
    store._records = []
    store._channel_health_lock = threading.RLock()
    store._channel_health = {}
    Path(store.generated_dir).mkdir(parents=True)
    return store


def test_reference_cache_hit_rate_counts_exact_content_reuse(tmp_path):
    store = cache_store(tmp_path)
    first = store._save_or_reuse_reference_image(ImageReference(b"reusable bytes", mime_type="image/png"))
    second = store._save_or_reuse_reference_image(ImageReference(b"reusable bytes", mime_type="image/png"))
    third = store._save_or_reuse_reference_image(ImageReference(b"new bytes", mime_type="image/png"))

    assert first == second
    assert third != first
    assert store.get_cache_metrics()["reference_cache"] == {
        "lookups": 3, "hits": 1, "misses": 2, "hit_rate": round(1 / 3, 4),
    }


def test_cleanup_scan_separates_orphans_references_protection_and_candidates(tmp_path):
    store = cache_store(tmp_path)
    cache = Path(store.generated_dir)
    names = ["referenced.png", "inflight.png", *(f"orphan-{index}.png" for index in range(10))]
    for name in names:
        (cache / name).write_bytes(name.encode("ascii"))
    store._records = [{"id": "record", "generated_image_paths": ["referenced.png"]}]
    store._register_inflight_media(["inflight.png"])

    preview = store.get_cache_cleanup_preview()
    assert preview["total_count"] == 12
    assert preview["referenced_count"] == 1
    assert preview["protected_count"] == 1
    assert preview["orphan_count"] == 10
    assert preview["candidate_count"] == 11
    assert preview["would_delete_count"] == 2
    assert (cache / "referenced.png").exists()
    assert (cache / "inflight.png").exists()

    result = store._cleanup_image_cache_if_needed(plan=preview)
    metrics = store.get_cache_metrics()["cleanup"]
    assert result["deleted_count"] == 2
    assert metrics["scans"] == 1
    assert metrics["runs"] == 1
    assert metrics["deleted_files"] == 2
    assert metrics["deleted_bytes"] == result["deleted_bytes"]
    assert metrics["last_scan"]["orphan_count"] == 10
    assert metrics["last_run"]["failed_delete_count"] == 0


def test_health_reuses_cleanup_scan_and_exposes_process_cache_counters(tmp_path, monkeypatch):
    store = cache_store(tmp_path)
    (Path(store.generated_dir) / "orphan.png").write_bytes(b"orphan")
    store._save_or_reuse_reference_image(ImageReference(b"cache image"))
    calls = []
    preview = store.get_cache_cleanup_preview

    def counted_preview():
        calls.append("scan")
        return preview()

    original_stats = store._cache_stats

    def existing_scan_stats(entries=None):
        assert entries is not None, "health performed a duplicate cache traversal"
        return original_stats(entries)

    monkeypatch.setattr(store, "get_cache_cleanup_preview", counted_preview)
    monkeypatch.setattr(store, "_cache_stats", existing_scan_stats)
    payload = build_health_payload(store)
    assert calls == ["scan"]
    assert payload["cache_file_count"] == payload["cache_cleanup_preview"]["total_count"] == 2
    assert payload["cache_metrics"]["reference_cache"] == {
        "lookups": 1, "hits": 0, "misses": 1, "hit_rate": 0.0,
    }
    assert payload["cache_metrics"]["cleanup"]["last_scan"]["orphan_count"] == 1


def test_orphan_growth_uses_existing_scans_and_resets_after_deletion():
    metrics = CacheMetrics()
    metrics.cleanup_scan({"orphan_count": 4})
    first = metrics.snapshot()["cleanup"]["orphan_growth"]
    assert first["available"] is False
    assert first["baseline_orphan_count"] == 4

    metrics.cleanup_scan({"orphan_count": 9})
    growth = metrics.snapshot()["cleanup"]["orphan_growth"]
    assert growth == {
        "available": True,
        "baseline_orphan_count": 4,
        "latest_orphan_count": 9,
        "increase": 5,
        "scans_since_baseline": 2,
    }

    metrics.cleanup_result({"deleted_count": 2})
    metrics.cleanup_scan({"orphan_count": 3})
    after_cleanup_baseline = metrics.snapshot()["cleanup"]["orphan_growth"]
    assert after_cleanup_baseline["available"] is False
    assert after_cleanup_baseline["baseline_orphan_count"] == 3

    metrics.cleanup_scan({"orphan_count": 6})
    assert metrics.snapshot()["cleanup"]["orphan_growth"]["increase"] == 3


def test_generation_metrics_use_cache_growth_snapshot_without_rescanning(tmp_path, monkeypatch):
    store = cache_store(tmp_path)
    store.raw_config = {"monitoring": {"generation_alerts": {
        "cache_orphan_growth_at_least": 2,
        "record_success_min_records": 100,
        "channel_min_attempts": 100,
        "rate_limit_min_attempts": 100,
        "retry_min_records": 100,
        "generation_min_records": 100,
    }}}
    store._cache_metrics().cleanup_scan({"orphan_count": 1})
    store._cache_metrics().cleanup_scan({"orphan_count": 4})
    monkeypatch.setattr(
        store,
        "get_cache_cleanup_preview",
        lambda: (_ for _ in ()).throw(AssertionError("metrics triggered a cache scan")),
    )

    metrics = store.get_generation_metrics()
    assert metrics["cache_orphan_growth"]["increase"] == 3
    assert {item["code"] for item in metrics["recommendations"]} == {"cache_orphan_growth"}
