from __future__ import annotations

import threading

from astrbot_plugin_selfie_image.core.models import (
    AICatConfig,
    normalize_generation_alert_settings,
)
from astrbot_plugin_selfie_image.features.config_manager import ConfigurationMixin
from astrbot_plugin_selfie_image.generation.generation_records import build_generation_metrics
from astrbot_plugin_selfie_image.generation.generation_store import GenerationStoreMixin


def _alert_records():
    return [
        {
            "status": "failed",
            "elapsed_seconds": 10,
            "retry_count": 1,
            "attempts": [
                {"channel": "primary", "success": False, "error_category": "rate_limit"},
                {"channel": "primary", "success": False, "error_category": "server"},
            ],
        },
        {
            "status": "succeeded",
            "elapsed_seconds": 10,
            "retry_count": 1,
            "attempts": [
                {"channel": "primary", "success": False, "error_category": "rate_limit"},
                {"channel": "primary", "success": True},
            ],
        },
        {
            "status": "succeeded",
            "elapsed_seconds": 10,
            "retry_count": 0,
            "attempts": [{"channel": "primary", "success": True}],
        },
        {
            "status": "succeeded",
            "elapsed_seconds": 10,
            "retry_count": 0,
            "attempts": [{"channel": "primary", "success": True}],
        },
    ]


def test_alert_settings_normalize_bounds_and_defaults():
    settings = normalize_generation_alert_settings({
        "enabled": "false",
        "record_success_rate_below": "nan",
        "record_success_min_records": 0,
        "consecutive_failure_count_at_least": 100_001,
        "channel_success_rate_below": "nan",
        "channel_min_attempts": 0,
        "rate_limit_rate_at_least": 2,
        "rate_limit_min_attempts": "invalid",
        "rate_limit_min_failures": 100_001,
        "retry_rate_at_least": True,
        "retry_min_records": 1.8,
        "generation_p95_seconds_at_least": float("inf"),
        "generation_min_records": -1,
        "cache_orphan_growth_at_least": -1,
        "unexpected": "ignored",
    })

    assert settings == {
        "enabled": False,
        "record_success_rate_below": 0.8,
        "record_success_min_records": 1,
        "consecutive_failure_count_at_least": 100_000,
        "channel_success_rate_below": 0.8,
        "channel_min_attempts": 1,
        "rate_limit_rate_at_least": 1.0,
        "rate_limit_min_attempts": 3,
        "rate_limit_min_failures": 100_000,
        "retry_rate_at_least": 0.5,
        "retry_min_records": 1,
        "generation_p95_seconds_at_least": 60,
        "generation_min_records": 1,
        "cache_orphan_growth_at_least": 1,
    }


def test_legacy_config_receives_default_generation_alerts():
    settings = AICatConfig.from_dict({}).raw["monitoring"]["generation_alerts"]
    assert settings == normalize_generation_alert_settings({})
    assert settings["enabled"] is True
    assert settings["record_success_rate_below"] == 0.8
    assert settings["consecutive_failure_count_at_least"] == 3
    assert settings["channel_success_rate_below"] == 0.8
    assert settings["generation_p95_seconds_at_least"] == 60


def test_generation_alert_thresholds_respect_inclusive_and_exclusive_edges():
    records = _alert_records()
    settings = {
        "channel_success_rate_below": 0.51,
        "channel_min_attempts": 6,
        "rate_limit_rate_at_least": 1 / 3,
        "rate_limit_min_attempts": 6,
        "rate_limit_min_failures": 2,
        "retry_rate_at_least": 0.5,
        "retry_min_records": 4,
        "generation_p95_seconds_at_least": 10,
        "generation_min_records": 4,
        "record_success_rate_below": 0.76,
        "record_success_min_records": 4,
        "consecutive_failure_count_at_least": 5,
        "cache_orphan_growth_at_least": 3,
    }
    metrics = build_generation_metrics(records, alert_settings=settings)
    codes = {item["code"] for item in metrics["recommendations"]}
    assert codes == {
        "channel_low_success", "rate_limit_pressure", "retry_heavy", "slow_generation",
        "record_success_rate_low",
    }

    boundary = build_generation_metrics(records, alert_settings={
        **settings,
        "channel_success_rate_below": 0.5,
        "rate_limit_rate_at_least": 0.34,
        "retry_rate_at_least": 0.51,
        "generation_p95_seconds_at_least": 10.1,
        "record_success_rate_below": 0.75,
        "cache_orphan_growth_at_least": 4,
    })
    assert boundary["recommendations"] == []


def test_window_success_rate_consecutive_failures_and_cache_growth_alerts():
    records = [
        {"id": "old-fail", "timestamp": 9_500, "status": "failed"},
        {"id": "cancelled", "timestamp": 9_600, "status": "cancelled"},
        {"id": "partial", "timestamp": 9_700, "status": "partial_success"},
        {"id": "delivery", "timestamp": 9_800, "status": "delivery_failed"},
        {"id": "new-fail", "timestamp": 9_900, "status": "failed"},
        {"id": "newer-fail", "timestamp": 9_950, "status": "failed"},
        {"id": "outside-window", "timestamp": 9_000, "status": "succeeded"},
    ]
    metrics = build_generation_metrics(
        records,
        window_seconds=500,
        now=10_000,
        alert_settings={
            "record_success_rate_below": 0.5,
            "record_success_min_records": 4,
            "consecutive_failure_count_at_least": 2,
            "cache_orphan_growth_at_least": 4,
        },
        cache_metrics={"cleanup": {"orphan_growth": {
            "available": True,
            "baseline_orphan_count": 3,
            "latest_orphan_count": 7,
            "increase": 4,
            "scans_since_baseline": 2,
        }}},
    )
    codes = {item["code"] for item in metrics["recommendations"]}
    assert metrics["retained_records"] == 6
    assert metrics["quality"]["record_success_rate"] == 0
    assert codes == {
        "record_success_rate_low",
        "consecutive_generation_failures",
        "cache_orphan_growth",
    }
    assert next(
        item for item in metrics["recommendations"]
        if item["code"] == "consecutive_generation_failures"
    )["value"] == 2


def test_success_resets_consecutive_failure_recommendation():
    records = [
        {"timestamp": 1, "status": "failed"},
        {"timestamp": 2, "status": "failed"},
        {"timestamp": 3, "status": "succeeded"},
    ]
    metrics = build_generation_metrics(
        records,
        alert_settings={
            "enabled": True,
            "consecutive_failure_count_at_least": 2,
            "record_success_min_records": 100,
            "channel_min_attempts": 100,
            "rate_limit_min_attempts": 100,
            "retry_min_records": 100,
            "generation_min_records": 100,
        },
    )
    assert "consecutive_generation_failures" not in {
        item["code"] for item in metrics["recommendations"]
    }


def test_disabled_alerts_emit_no_recommendations():
    metrics = build_generation_metrics(_alert_records(), alert_settings={"enabled": False})
    assert metrics["recommendations"] == []
    assert metrics["alert_settings"]["enabled"] is False


def test_config_save_normalizes_and_metrics_use_saved_thresholds(tmp_path):
    plugin = ConfigurationMixin()
    plugin.config_path = str(tmp_path / "config.json")
    plugin.key_config = {"web": {"enable": False, "host": "127.0.0.1", "port": 14514, "token": "test"}}
    plugin._config_lock = threading.RLock()
    plugin.raw_config, plugin.config = plugin._load_initial_config_state()

    saved = plugin.update_config_from_web({"monitoring": {"generation_alerts": {
        "enabled": False,
        "channel_min_attempts": 0,
        "generation_p95_seconds_at_least": 30,
        "record_success_min_records": 2,
    }}})
    alerts = saved["monitoring"]["generation_alerts"]
    assert alerts["enabled"] is False
    assert alerts["channel_min_attempts"] == 1
    assert alerts["generation_p95_seconds_at_least"] == 30
    assert alerts["record_success_min_records"] == 2

    plugin._records_lock = threading.RLock()
    plugin._records = _alert_records()
    metrics = GenerationStoreMixin.get_generation_metrics(plugin)
    assert metrics["alert_settings"] == alerts
    assert metrics["recommendations"] == []
