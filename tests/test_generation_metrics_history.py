import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from astrbot_plugin_selfie_image.generation.generation_records import build_generation_metrics
from astrbot_plugin_selfie_image.generation.generation_store import GenerationStoreMixin
from astrbot_plugin_selfie_image.generation.record_database import RecordDatabase


def store_at(root, limit=1):
    store = GenerationStoreMixin()
    store.records_path = str(root / "records.json")
    store.records_db_path = str(root / "records.sqlite3")
    store.generated_dir = str(root / "cache")
    store.config = SimpleNamespace(image_record_keep_limit=limit)
    store._records_lock = threading.RLock()
    store._records = store._load_records()
    store._record_seq = store._restore_record_sequence()
    return store


def sample_record(index, *, timestamp=None):
    success = index % 2 == 0
    return {
        "id": f"record-{index}", "timestamp": timestamp or time.time(),
        "success": success, "status": "succeeded" if success else "failed",
        "count": 1, "used_model": "provider/model", "elapsed_seconds": index + 1,
        "queue_wait_seconds": index, "stage_timings": {"image_to_text": index + 0.5},
        "original_prompt": "private prompt", "request_data": {"api_key": "private-key"},
        "response_data": {"raw": "private response"},
        "attempts": [{"channel": "provider", "model": "model", "label": "provider/model", "success": success,
                      "elapsed_seconds": index + 1, "error_category": "" if success else "timeout",
                      "http_status": 0 if success else 504, "error": "private error"}],
    }


def assert_metrics_equal(actual, expected):
    for key in ("requested_images", "succeeded_images", "failed_images", "quality",
                "status_counts", "error_categories", "models", "model_attempts", "channels",
                "retries", "elapsed_seconds", "queue_wait_seconds", "stage_timings"):
        assert actual[key] == expected[key], key


def test_metrics_survive_retention_deletion_clear_and_restart(tmp_path):
    store = store_at(tmp_path)
    for index in range(3):
        store._commit_generation_record(sample_record(index))
    assert len(store.get_recent_records()) == 1
    expected = build_generation_metrics([sample_record(index) for index in range(3)])
    assert_metrics_equal(store.get_generation_metrics(), expected)

    store = store_at(tmp_path)
    assert_metrics_equal(store.get_generation_metrics(window_seconds=3600), expected)
    store.delete_records(["record-2"])
    assert_metrics_equal(store.get_generation_metrics(), expected)
    store._commit_generation_record(sample_record(3))
    store.clear_recent_records()
    store = store_at(tmp_path)
    assert store.get_recent_records() == []
    expected = build_generation_metrics([sample_record(index) for index in range(4)])
    assert_metrics_equal(store.get_generation_metrics(), expected)


def test_upgrade_seeds_all_existing_rows_before_retention_is_reduced(tmp_path):
    path = tmp_path / "records.sqlite3"
    database = RecordDatabase(str(path))
    records = [sample_record(index) for index in range(4)]
    database.replace_records(records)
    # Recreate the previous release's schema without a statistics projection.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE IF EXISTS generation_metric_records")
    store = store_at(tmp_path)
    assert len(store.get_recent_records()) == 1
    assert_metrics_equal(store.get_generation_metrics(), build_generation_metrics(records))


def test_legacy_json_seed_includes_rows_beyond_new_retention_limit(tmp_path):
    records = [sample_record(index) for index in range(4)]
    (tmp_path / "records.json").write_text(json.dumps({"records": records}), encoding="utf-8")
    store = store_at(tmp_path)
    assert len(store.get_recent_records()) == 1
    assert_metrics_equal(store.get_generation_metrics(), build_generation_metrics(records))


def test_legacy_rows_without_ids_keep_stable_statistics_when_protected(tmp_path):
    records = [sample_record(index) for index in range(3)]
    for record in records:
        record.pop("id")
    records[-1]["favorite"] = True
    (tmp_path / "records.json").write_text(json.dumps({"records": records}), encoding="utf-8")
    store = store_at(tmp_path)
    assert len(store.get_recent_records()) == 2
    assert_metrics_equal(store.get_generation_metrics(), build_generation_metrics(records))
    assert store_at(tmp_path).get_generation_metrics()["total_records"] == 3


def test_cumulative_statistics_have_no_thousand_record_cap(tmp_path):
    records = [sample_record(index) for index in range(1005)]
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    database.replace_records(records)
    database.prune_to_limit(1000)
    store = store_at(tmp_path, limit=1000)
    assert len(store.get_recent_records()) == 1000
    metrics = store.get_generation_metrics()
    assert metrics["records"] == 1005
    assert_metrics_equal(metrics, build_generation_metrics(records))


def test_batch_size_is_not_recounted_for_each_image_record(tmp_path):
    store = store_at(tmp_path)
    records = [sample_record(index) for index in range(10)]
    for record in records:
        record["requested_count"] = 10
        if not record["success"]:
            record.pop("count")
        store._commit_generation_record(record)
    for metrics in (build_generation_metrics(records), store_at(tmp_path).get_generation_metrics()):
        assert metrics["requested_images"] == 10
        assert metrics["succeeded_images"] == 5
        assert metrics["failed_images"] == 5
        assert metrics["quality"]["image_success_rate"] == 0.5


@pytest.mark.parametrize("response_summary", [False, True])
def test_statistics_projection_preserves_explicit_batch_summary(tmp_path, response_summary):
    record = sample_record(1)
    summary = {"requested_count": 10, "succeeded_count": 7, "failed_count": 3}
    record["count"] = 7
    record["status"] = "partial_success"
    if response_summary:
        record["response_data"] = summary
    else:
        record.update(summary)
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    database.upsert_record(record)
    projected, _, _ = database.load_metric_records()
    for metrics in (build_generation_metrics([record]), build_generation_metrics(projected)):
        assert metrics["requested_images"] == 10
        assert metrics["succeeded_images"] == 7
        assert metrics["failed_images"] == 3
        assert metrics["quality"]["image_success_rate"] == 0.7


@pytest.mark.parametrize("window", [None, 3600, 86400, 7 * 86400, 30 * 86400])
def test_windows_include_evicted_history_and_share_one_scope(tmp_path, window):
    store = store_at(tmp_path)
    now = time.time()
    records = [sample_record(index, timestamp=now - age) for index, age in enumerate(
        [60, 1800, 7200, 2 * 86400, 10 * 86400, 40 * 86400]
    )]
    database = store._record_database()
    database.replace_records(records)
    store._records = database.load_records(1)
    database.prune_to_limit(1)
    metrics = store.get_generation_metrics(window_seconds=window)
    expected = build_generation_metrics(records, window_seconds=window, now=now)
    assert_metrics_equal(metrics, expected)
    assert metrics["records"] == expected["quality"]["records"]
    assert metrics["total_records"] == 6
    assert metrics["total_retained_records"] == 1
    assert metrics["statistics_scope"] == "cumulative"
    assert metrics["statistics_started_at"]


def test_projection_excludes_prompts_raw_payloads_and_image_data(tmp_path):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    record = sample_record(1)
    record["generated_image_sources"] = [{"type": "base64", "value": "private-base64"}]
    record["attempts"][0]["api_key"] = "attempt-private-key"
    database.upsert_record(record)
    database.clear()
    records, total, _ = database.load_metric_records()
    assert total == 1
    assert "private" not in json.dumps(records)
    assert_metrics_equal(build_generation_metrics(records), build_generation_metrics([record]))


def test_same_record_updates_statistics_without_double_counting(tmp_path):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    record = sample_record(0)
    database.upsert_record(record)
    database.upsert_record({**record, "status": "delivery_failed", "stage_timings": {"delivery_receipt": 5}})
    database = RecordDatabase(database.path)
    records, total, _ = database.load_metric_records()
    metrics = build_generation_metrics(records)
    assert total == 1
    assert metrics["requested_images"] == 1
    assert metrics["status_counts"] == {"delivery_failed": 1}
    assert metrics["stage_timings"]["delivery_receipt"]["max"] == 5


def test_failed_record_write_does_not_leave_statistical_history(tmp_path, monkeypatch):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    original = database._upsert_metric_record

    def fail(connection, record_id, record):
        original(connection, record_id, record)
        raise RuntimeError("failed statistics write")

    monkeypatch.setattr(database, "_upsert_metric_record", fail)
    with pytest.raises(RuntimeError, match="statistics write"):
        database.upsert_record(sample_record(0))
    assert database.has_records() is False
    assert database.load_metric_records()[1] == 0


def test_failed_statistics_migration_can_retry_without_losing_history(tmp_path, monkeypatch):
    path = str(tmp_path / "records.sqlite3")
    database = RecordDatabase(path)
    records = [sample_record(index) for index in range(3)]
    database.replace_records(records)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE generation_metric_records")
    original = RecordDatabase._upsert_metric_record

    def fail(connection, record_id, record):
        original(connection, record_id, record)
        raise RuntimeError("failed statistics migration")

    with monkeypatch.context() as patch:
        patch.setattr(RecordDatabase, "_upsert_metric_record", staticmethod(fail))
        with pytest.raises(RuntimeError, match="statistics migration"):
            RecordDatabase(path)
    recovered = RecordDatabase(path)
    projected, total, _ = recovered.load_metric_records()
    assert total == 3
    assert_metrics_equal(build_generation_metrics(projected), build_generation_metrics(records))


def test_latest_failure_streak_preserves_order_for_equal_timestamps(tmp_path):
    database = RecordDatabase(str(tmp_path / "records.sqlite3"))
    timestamp = time.time()
    records = [sample_record(1, timestamp=timestamp), sample_record(0, timestamp=timestamp)]
    database.replace_records(records)
    projected, _, _ = database.load_metric_records()
    expected = build_generation_metrics(records, alert_settings={"consecutive_failure_count_at_least": 1})
    actual = build_generation_metrics(projected, alert_settings={"consecutive_failure_count_at_least": 1})
    assert actual["recommendations"] == expected["recommendations"]


def test_dashboard_uses_cumulative_counts_instead_of_retention_counts():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = Path(__file__).resolve().parents[1] / "pages/dashboard/source/monitoring.js"
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = {
  monitorMetricButtons: {innerHTML:'', classList:{add(){},remove(){}}},
  monitorMetricsWindow: {value:'all',selectedOptions:[{textContent:'全部累计统计'}]},
  monitorMetricsWindowLabel: {textContent:''},
};
const context = vm.createContext({
  $:id => elements[id], escapeHtml:String, METRICS_LOAD_SEQ:0, METRICS_CONTENTS:{},
  ACTIVE_METRICS_CATEGORY:'', api:async () => ({data:{
    statistics_scope:'cumulative', statistics_started_at:'2026-11-27 08:00:00',
    records:1500,total_records:1500,retained_records:1000,total_retained_records:1000,
    requested_images:1500,succeeded_images:1200,failed_images:300,
    quality:{record_success_rate:0.8},
  }}),
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
(async () => {
  await vm.runInContext('loadMetricsNow()', context);
  assert.match(context.METRICS_CONTENTS.output.html, /1500 \/ 1500/);
  assert.ok(!context.METRICS_CONTENTS.output.html.includes('1000'));
  assert.match(context.METRICS_CONTENTS.output.html, /当前窗口 \/ 累计记录/);
  assert.match(context.METRICS_CONTENTS.quality.html, /80\.0%/);
  assert.match(elements.monitorMetricsWindowLabel.textContent, /2026-11-27 08:00:00/);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, str(source)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
