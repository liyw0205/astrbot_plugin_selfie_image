from __future__ import annotations

import asyncio
import copy
import logging
import json
import tempfile
import os
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_selfie_image.generation.generation_records import build_record_scope_stats
from astrbot_plugin_selfie_image.generation.generation_results import (
    build_task_terminal_state,
    resolve_cancel_request,
    result_has_completion_evidence,
)
from astrbot_plugin_selfie_image.core.models import AICatConfig, ImageModelTarget
from astrbot_plugin_selfie_image.core.error_classify import classify_generation_error
from astrbot_plugin_selfie_image.core.providers import (
    BaseImageAdapter,
    IMAGE_ADAPTER_TYPES,
    create_adapter,
)
from astrbot_plugin_selfie_image.prompts.preset import ImagePresetManager
from astrbot_plugin_selfie_image.main import SelfieImagePlugin
from astrbot_plugin_selfie_image.tasks.task_manager import WebTaskMixin
from astrbot_plugin_selfie_image.webui.web import FlaskWebServer, dashboard_page_source_status
from astrbot_plugin_selfie_image.webui.contracts import (
    DASHBOARD_ROUTE_PREFIXES,
    DASHBOARD_TASK_CANCEL_ROUTES,
    TASK_CANCEL_ROUTE_ALIASES,
)
from astrbot_plugin_selfie_image.webui.services import (
    WebContractError,
    build_health_payload,
    filter_record_page,
    parse_task_query,
)
from astrbot_plugin_selfie_image.features.config_manager import ConfigurationMixin
from astrbot_plugin_selfie_image.features.conversation_context import ConversationContextMixin
from astrbot_plugin_selfie_image.generation.asset_collections import AssetCollectionStore
from astrbot_plugin_selfie_image.generation.generation_store import GenerationStoreMixin
from astrbot_plugin_selfie_image.generation.video import VideoGenerateRequest, VideoGenerateResult, generate_video_with_fallback
from astrbot_plugin_selfie_image.studio.studio import StudioStore
from astrbot_plugin_selfie_image.features.reference_collector import CollectedReferences
from astrbot_plugin_selfie_image.features.audit_pipeline import AuditMixin


ROOT = Path(__file__).resolve().parents[1]


def test_config_preflight_health_summary_distinguishes_unconfigured_and_ready() -> None:
    probe = object.__new__(ConfigurationMixin)
    probe.raw_config = {}
    unconfigured = probe.get_config_preflight_for_web()
    assert unconfigured["status"] == "unconfigured"
    assert unconfigured["ready"] is False

    probe.raw_config = {
        "image_channels": [
            {
                "name": "local",
                "provider_type": "openai",
                "base_url": "https://example.test/v1",
                "api_key": "sk-secret-must-not-leak",
                "model": "gpt-image-2",
                "enabled": True,
            }
        ]
    }
    ready = probe.get_config_preflight_for_web()
    assert ready["status"] == "ok"
    assert ready["ready"] is True
    assert ready["ready_count"] == 1
    assert "sk-secret-must-not-leak" not in str(ready)


def test_config_preflight_health_summary_reports_invalid_channel_without_secret() -> None:
    probe = object.__new__(ConfigurationMixin)
    probe.raw_config = {
        "image_channels": [
            {
                "name": "broken",
                "provider_type": "openai",
                "api_key": "sk-secret-must-not-leak",
            }
        ]
    }
    report = probe.get_config_preflight_for_web()
    assert report["status"] == "invalid"
    assert report["ready"] is False
    assert report["invalid_count"] == 1
    assert "sk-secret-must-not-leak" not in str(report)


def test_masked_config_export_can_be_previewed_and_imported_without_losing_secrets() -> None:
    plugin = object.__new__(ConfigurationMixin)
    plugin.raw_config = {
        "schema_version": 2,
        "image_channels": [
            {
                "id": "main",
                "name": "main",
                "provider_type": "openai",
                "base_url": "https://example.test/v1",
                "api_key": "key-one",
                "api_keys": ["key-one", "key-two"],
                "model": "gpt-image-2",
                "enabled": True,
            }
        ],
        "proxies": [{"id": "proxy-1", "url": "http://proxy.test:8080", "password": "proxy-secret"}],
        "compatibility_field": {"keep": True},
    }
    exported = plugin.export_config_for_web()
    assert exported["image_channels"][0]["api_key"] == "[REDACTED]"
    assert exported["proxies"][0]["password"] == "[REDACTED]"
    preview = plugin.preview_config_import(exported)
    assert preview["ok"] is True
    assert "key-one" not in str(preview)
    assert "proxy-secret" not in str(preview)

    import threading

    plugin._config_lock = threading.RLock()
    applied = []
    plugin._apply_raw_config = lambda value: (applied.append(value), setattr(plugin, "raw_config", value))
    plugin._persist_config = lambda: None
    result = plugin.import_config_from_web(exported)
    assert result["image_channels"][0]["api_key"] == "******"
    assert applied[0]["image_channels"][0]["api_key"] == "key-one"
    assert applied[0]["image_channels"][0]["api_keys"] == ["key-one", "key-two"]
    assert applied[0]["proxies"][0]["password"] == "proxy-secret"
    assert applied[0]["compatibility_field"] == {"keep": True}


def test_invalid_config_import_rolls_back_without_overwriting_current_config() -> None:
    import threading

    plugin = object.__new__(ConfigurationMixin)
    plugin.raw_config = {
        "image_channels": [
            {
                "id": "main",
                "name": "main",
                "provider_type": "openai",
                "base_url": "https://example.test/v1",
                "api_key": "key-one",
                "model": "gpt-image-2",
                "enabled": True,
            }
        ]
    }
    before = copy.deepcopy(plugin.raw_config)
    plugin._config_lock = threading.RLock()
    plugin._persist_config = lambda: None
    plugin._apply_raw_config = lambda value: setattr(plugin, "raw_config", value)
    with pytest.raises(RuntimeError, match="预检未通过"):
        plugin.import_config_from_web(
            {"image_channels": [{"id": "main", "name": "main", "provider_type": "openai", "enabled": True}]}
        )
    assert plugin.raw_config == before


def test_legacy_schema_and_proxy_shape_upgrade_in_memory_without_dropping_compat_fields() -> None:
    config = AICatConfig.from_dict(
        {
            "schema_version": 1,
            "imageChannels": [
                {
                    "name": "legacy",
                    "providerType": "openai",
                    "baseUrl": "https://legacy.test/v1",
                    "apiKey": "legacy-key",
                    "model": "gpt-image-2",
                    "proxy": "socks5://127.0.0.1:1080",
                }
            ],
            "compatibility_field": {"preserve": "yes"},
        }
    )
    assert config.raw["schema_version"] == 2
    assert config.image_channels[0].resolved_api_keys() == ["legacy-key"]
    assert config.image_channels[0].proxy_id
    assert config.raw["compatibility_field"] == {"preserve": "yes"}


def test_config_file_schema_migration_creates_backup_and_round_trips_legacy_keys() -> None:
    legacy = {
        "schema_version": 1,
        "imageChannels": [
            {
                "name": "legacy",
                "providerType": "openai",
                "baseUrl": "https://legacy.test/v1",
                "apiKey": "legacy-key",
                "model": "gpt-image-2",
            }
        ],
        "compatibility_field": {"preserve": "yes"},
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "selfie_image_config.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        original = path.read_bytes()
        plugin = object.__new__(ConfigurationMixin)
        plugin.config_path = str(path)
        result = plugin._migrate_config_file_schema()

        assert result["migrated"] is True
        backup = Path(f"{path}.bak")
        assert backup.read_bytes() == original
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted["schema_version"] == 2
        assert persisted["image_channels"][0]["apiKey"] == "legacy-key"
        assert persisted["compatibility_field"] == {"preserve": "yes"}
        reloaded = AICatConfig.from_dict(persisted)
        assert reloaded.image_channels[0].resolved_api_keys() == ["legacy-key"]


def test_config_file_schema_migration_failure_restores_original_file(monkeypatch) -> None:
    import astrbot_plugin_selfie_image.features.config_manager as config_module

    legacy = {"schema_version": 1, "imageChannels": [{"name": "legacy", "providerType": "openai"}]}
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "selfie_image_config.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        original = path.read_bytes()
        plugin = object.__new__(ConfigurationMixin)
        plugin.config_path = str(path)

        def fail_save(*_args, **_kwargs):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(config_module, "save_json_file", fail_save)
        result = plugin._migrate_config_file_schema()
        assert result == {"migrated": False, "reason": "failed"}
        assert path.read_bytes() == original
        assert Path(f"{path}.bak").read_bytes() == original


def test_terminal_state_matrix_preserves_media_completion_and_late_success() -> None:
    cases = [
        ({"success": True, "files": ["image.png"]}, 1, "succeeded"),
        ({"success": True, "video_path": "video.mp4"}, 1, "succeeded"),
        ({"success": False, "files": ["one.png"], "requested_count": 2, "succeeded_count": 1, "failed_count": 1}, 2, "partial_success"),
        ({"success": False, "generation_success": True, "delivery_failed": True, "files": ["video.mp4"]}, 1, "delivery_failed"),
        ({"success": False, "error": "nope"}, 1, "failed"),
        ({"success": False, "cancelled": True, "error": "任务已取消"}, 1, "cancelled"),
    ]
    for result, requested, expected in cases:
        terminal = build_task_terminal_state(result, requested_count=requested)
        assert terminal["terminal_status"] == expected

    late = build_task_terminal_state(
        {
            "success": False,
            "cancelled": True,
            "status": "cancelled",
            "files": ["late.png"],
            "requested_count": 1,
            "succeeded_count": 1,
        },
        requested_count=1,
        cancel_requested=True,
    )
    assert late["cancelled"] is False
    assert late["terminal_status"] == "succeeded"
    assert late["result"]["cancelled"] is False


def test_video_auth_failure_falls_back_to_next_channel_but_timeout_does_not(monkeypatch) -> None:
    from astrbot_plugin_selfie_image.generation import video as video_module

    first = ImageModelTarget("first", "openai_video", "https://first.test", "bad", "v1", 30)
    second = ImageModelTarget("second", "openai_video", "https://second.test", "good", "v2", 30)
    calls = []

    async def fake_generate(target, request, session, *, save_dir, timeout_override=None):
        calls.append(target.channel_name)
        if target.channel_name == "first":
            return VideoGenerateResult(
                error="HTTP 401: invalid token",
                attempts=[{"channel": "first", "success": False, "error_category": "auth"}],
            )
        return VideoGenerateResult(
            video_path="/tmp/video.mp4",
            used_model=target.label,
            attempts=[{"channel": "second", "success": True}],
        )

    monkeypatch.setattr(video_module, "generate_video_openai_compatible", fake_generate)
    result = asyncio.run(
        generate_video_with_fallback(
            [first, second],
            VideoGenerateRequest(prompt="move"),
            object(),
            save_dir=tempfile.gettempdir(),
        )
    )
    assert calls == ["first", "second"]
    assert result.video_path == "/tmp/video.mp4"

    calls.clear()

    async def timeout_generate(target, request, session, *, save_dir, timeout_override=None):
        calls.append(target.channel_name)
        return VideoGenerateResult(
            error="视频请求超时（未自动重提，以免重复提交）",
            attempts=[{"channel": target.channel_name, "success": False, "error_category": "timeout"}],
        )

    monkeypatch.setattr(video_module, "generate_video_openai_compatible", timeout_generate)
    timeout_result = asyncio.run(
        generate_video_with_fallback(
            [first, second],
            VideoGenerateRequest(prompt="move"),
            object(),
            save_dir=tempfile.gettempdir(),
        )
    )
    assert calls == ["first"]
    assert timeout_result.attempts[-1]["error_category"] == "timeout"


@pytest.mark.parametrize(
    ("message", "category", "retryable"),
    [
        ("request timeout", "timeout", False),
        ("HTTP 401: invalid token", "auth", False),
        ("HTTP 429: rate limit", "rate_limit", True),
        ("HTTP 503: upstream unavailable", "server", True),
        ("", "unknown", True),
    ],
)
def test_provider_error_matrix_has_stable_category_and_retry_policy(message, category, retryable) -> None:
    info = classify_generation_error(message)
    assert info["category"] == category
    assert info["retryable"] is retryable


def test_health_payload_marks_configuration_degraded_but_preserves_diagnostics() -> None:
    class Plugin:
        config = SimpleNamespace(image_cache_limit_mb=200, image_cache_limit_count=100)
        config_path = records_path = records_db_path = media_sources_dir = generated_dir = ""

        def _cache_stats(self):
            return 0, 0

        def get_channel_health(self):
            return {}

        def get_cache_cleanup_preview(self):
            return {}

        def get_config_preflight_for_web(self):
            return {
                "status": "invalid",
                "ok": False,
                "ready": False,
                "errors": [{"field": "api_key", "message": "生图渠道 broken 缺少 api_key"}],
            }

    payload = build_health_payload(Plugin())
    assert payload["status"] == "degraded"
    assert payload["config_preflight"]["status"] == "invalid"
    assert payload["config_preflight"]["errors"][0]["field"] == "api_key"


def test_dashboard_health_uses_the_same_configuration_diagnostics() -> None:
    import astrbot_plugin_selfie_image.webui.dashboard_api as dashboard_api
    from astrbot_plugin_selfie_image.webui.dashboard_api import SelfieImageDashboardAPI

    class Plugin:
        config = SimpleNamespace(image_cache_limit_mb=200, image_cache_limit_count=100)
        config_path = records_path = records_db_path = media_sources_dir = generated_dir = ""

        def _cache_stats(self):
            return 0, 0

        def get_channel_health(self):
            return {}

        def get_cache_cleanup_preview(self):
            return {}

        def get_config_preflight_for_web(self):
            return {"status": "unavailable", "ok": True, "ready": False, "errors": []}

    previous = dashboard_api.json_response
    dashboard_api.json_response = lambda payload, **_: payload
    try:
        result = asyncio.run(SelfieImageDashboardAPI(Plugin()).page_health())
    finally:
        dashboard_api.json_response = previous
    assert result["data"]["data"]["status"] == "degraded"
    assert result["data"]["data"]["config_preflight"]["status"] == "unavailable"


def test_flask_health_uses_the_same_configuration_diagnostics() -> None:
    class Plugin:
        config = SimpleNamespace(web_token="secret", image_cache_limit_mb=200, image_cache_limit_count=100)
        config_path = records_path = records_db_path = media_sources_dir = generated_dir = ""

        def _cache_stats(self):
            return 0, 0

        def get_channel_health(self):
            return {}

        def get_cache_cleanup_preview(self):
            return {}

        def get_config_preflight_for_web(self):
            return {"status": "invalid", "ok": False, "ready": False, "errors": []}

    client = FlaskWebServer(Plugin())._create_app().test_client()
    response = client.get("/api/health", headers={"X-Selfie-Image-Token": "secret"})
    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert payload["status"] == "degraded"
    assert payload["config_preflight"]["status"] == "invalid"


def test_health_history_rejects_out_of_range_windows_in_both_adapters(monkeypatch) -> None:
    import astrbot_plugin_selfie_image.webui.dashboard_api as dashboard_module

    class Plugin:
        config = SimpleNamespace(web_token="secret")

        def get_channel_health_history(self, **kwargs):
            return kwargs

    plugin = Plugin()
    client = FlaskWebServer(plugin)._create_app().test_client()
    response = client.get(
        "/api/health/history?window=-1",
        headers={"X-Selfie-Image-Token": "secret"},
    )
    assert response.status_code == 400
    assert "不能小于 0" in response.get_json()["error"]

    class FakeRequest:
        query = {"window": "-1"}

        async def json(self, default=None):
            return default

    monkeypatch.setattr(dashboard_module, "request", FakeRequest())
    monkeypatch.setattr(dashboard_module, "json_response", lambda payload, **_: payload)
    monkeypatch.setattr(
        dashboard_module,
        "error_response",
        lambda message, **kwargs: {"status": kwargs.get("status_code"), "error": message},
    )
    api = dashboard_module.SelfieImageDashboardAPI(plugin)
    result = asyncio.run(api.page_health_history())
    assert result["status"] == 400
    assert "不能小于 0" in result["error"]

    FakeRequest.query = {"window": str(31 * 86400 + 1)}
    result = asyncio.run(api.page_health_history())
    assert result["status"] == 400
    assert "不能大于" in result["error"]


def test_storage_consistency_is_read_only_and_reports_orphans_and_missing_media() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cache = Path(directory) / "image_cache"
        sidecars = Path(directory) / "media_sources"
        cache.mkdir()
        sidecars.mkdir()
        (cache / "orphan.png").write_bytes(b"x")
        plugin = object.__new__(GenerationStoreMixin)
        plugin._records_lock = __import__("threading").RLock()
        plugin._records = [{"id": "record-1", "generated_image_paths": ["missing.png"]}]
        plugin.generated_dir = str(cache)
        plugin.media_sources_dir = str(sidecars)
        report = plugin.inspect_storage_consistency()
        assert report["read_only"] is True
        assert report["counts"]["missing_cache"] == 1
        assert report["counts"]["orphan_cache"] == 1
        assert (cache / "orphan.png").exists()
        preview = plugin.repair_storage_consistency()
        assert preview["dry_run"] is True
        assert (cache / "orphan.png").exists()
        done = plugin.repair_storage_consistency(confirm=True)
        assert done["deleted"] == [{"kind": "orphan_cache", "path": "orphan.png"}]
        assert not (cache / "orphan.png").exists()


def test_storage_consistency_repair_accepts_one_kind_string() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cache = Path(directory) / "image_cache"
        sidecars = Path(directory) / "media_sources"
        cache.mkdir()
        sidecars.mkdir()
        (cache / "orphan.png").write_bytes(b"x")
        plugin = object.__new__(GenerationStoreMixin)
        plugin._records_lock = __import__("threading").RLock()
        plugin._records = []
        plugin.generated_dir = str(cache)
        plugin.media_sources_dir = str(sidecars)
        result = plugin.repair_storage_consistency(confirm=True, kinds="orphan_cache")
        assert result["deleted"] == [{"kind": "orphan_cache", "path": "orphan.png"}]


def test_storage_consistency_keeps_retained_video_references() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cache = Path(directory) / "image_cache"
        sidecars = Path(directory) / "media_sources"
        (cache / "video").mkdir(parents=True)
        sidecars.mkdir()
        (cache / "video" / "clip.mp4").write_bytes(b"video")
        plugin = object.__new__(GenerationStoreMixin)
        plugin._records_lock = __import__("threading").RLock()
        plugin._records = [{"id": "video-1", "generated_video_paths": ["video/clip.mp4"]}]
        plugin.generated_dir = str(cache)
        plugin.media_sources_dir = str(sidecars)
        report = plugin.inspect_storage_consistency()
        assert report["ok"] is True
        assert report["counts"] == {}


def test_studio_copy_clears_results_and_remaps_slots() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = StudioStore(directory)
        source = store.create("原会话", template="duo")
        source = store.update_graph(source["id"], {"prompt": "keep", "input_order": [source["slots"][0]["id"]]})
        source = store.attach_run_finish(source["id"], "task-1", success=True, result_paths=["cache/a.png"])
        copied = store.copy_session(source["id"], title="副本")
        assert copied["title"] == "副本"
        assert copied["results"] == []
        assert copied["last_run"] is None
        assert copied["graph"]["prompt"] == "keep"
        assert copied["slots"][0]["id"] != source["slots"][0]["id"]
        assert copied["graph"]["input_order"] == [copied["slots"][0]["id"]]
        store.update_graph(copied["id"], {"prompt": "changed"})
        assert store.get(source["id"])["graph"]["prompt"] == "keep"


def test_context_persistence_is_opt_in_and_never_writes_image_sources() -> None:
    with tempfile.TemporaryDirectory() as directory:
        context = object.__new__(ConversationContextMixin)
        context.data_dir = directory
        context.raw_config = {"image": {"persist_context": True}}
        context._context_lock = __import__("threading").RLock()
        context._conversation_context = OrderedDict()
        context._context_max_messages = 40
        context._context_max_sessions = 100
        context._add_context_message("group:1", "u", "用户", "hello", image_sources=["https://secret.invalid/a.png"])
        path = Path(directory) / "conversation_context.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["sessions"]["group:1"][0]["image_count"] == 1
        assert "secret.invalid" not in path.read_text(encoding="utf-8")
        view = context.get_conversation_context(SimpleNamespace(unified_msg_origin="group:1"))
        assert view["messages"][0]["image_sources"] == []
        assert context.clear_conversation_context(SimpleNamespace(unified_msg_origin="group:1"))["cleared"] == 1


def test_channel_health_history_aggregates_empty_and_media_scopes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        plugin = object.__new__(GenerationStoreMixin)
        plugin.data_dir = directory
        plugin._channel_health = {}
        plugin._channel_health_lock = __import__("threading").RLock()
        plugin._record_channel_health([
            {"channel": "img", "success": True, "elapsed_seconds": 1.0},
            {"channel": "img", "success": False, "elapsed_seconds": 2.0, "error_category": "server"},
        ], media_type="image")
        plugin._record_channel_health([
            {"channel": "vid", "success": False, "elapsed_seconds": 3.0, "error_category": "timeout"},
        ], media_type="video")
        image = plugin.get_channel_health_history(window_seconds=3600, media_type="image")
        assert image["sample_count"] == 2
        assert image["channels"][0]["success_rate"] == 0.5
        assert plugin.get_channel_health_history(window_seconds=3600, media_type="audit")["empty"] is True


def test_auxiliary_target_health_event_is_scoped_and_sanitized() -> None:
    class Plugin(AuditMixin):
        def __init__(self):
            self.events = []

        async def _audit_chat_via_target(self, target, text, images=None):
            return "允许"

        def _record_channel_health(self, attempts, *, media_type="image"):
            self.events.append((attempts, media_type))

    plugin = Plugin()
    target = SimpleNamespace(channel_name="audit-channel", label="audit-channel/moderator")
    result = asyncio.run(
        plugin._call_audit_target_with_health(target, "审核", media_type="audit")
    )
    assert result == "允许"
    assert plugin.events[0][1] == "audit"
    assert plugin.events[0][0][0]["channel"] == "audit-channel"
    assert plugin.events[0][0][0]["success"] is True


def test_asset_collection_delete_does_not_touch_record_ids() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = AssetCollectionStore(directory)
        collection = store.create("精选", record_ids=["r1", "r1", "r2"])
        assert collection["record_ids"] == ["r1", "r2"]
        assert store.delete(collection["id"])["deleted"] is True
        assert not (Path(directory) / "generation_records.sqlite3").exists()


def test_reference_selection_summary_is_credential_free() -> None:
    collected = CollectedReferences(source_count=4, failed_count=1, roles={"message": 1, "context": 2, "persona": 1})
    summary = collected.selection_summary(include_persona=True)
    assert summary["selected_count"] == 4
    assert summary["used_persona"] is True
    assert summary["used_context_fallback"] is True


def test_record_scope_stats_uses_filtered_full_set_and_explains_empty_scope() -> None:
    stats = build_record_scope_stats(
        [
            {"success": True, "elapsed_seconds": 1.0},
            {"success": False, "elapsed_seconds": 3.0},
            {"success": True, "elapsed_seconds": 5.0},
        ]
    )
    assert stats["sample_count"] == 3
    assert stats["success_count"] == 2
    assert stats["failure_count"] == 1
    assert stats["success_rate"] == 66.67
    assert stats["average_elapsed_seconds"] == 3.0
    empty = build_record_scope_stats([])
    assert empty["scope"] == "filtered"
    assert empty["success_rate"] is None


def test_preset_builtin_failure_is_diagnostic_not_a_successful_empty_list(caplog) -> None:
    class BrokenPresetManager(ImagePresetManager):
        @staticmethod
        def _builtin_seed():
            raise RuntimeError("broken seed")

    with tempfile.TemporaryDirectory() as directory, caplog.at_level(logging.ERROR):
        manager = BrokenPresetManager(directory)
        assert manager.list() == []
        status = manager.get_load_status()
        assert status["ok"] is False
        assert "RuntimeError" in str(status["error"])
        assert "built-in" in caplog.text


def test_cancel_request_keeps_active_task_running_until_runner_reaches_boundary() -> None:
    with tempfile.TemporaryDirectory() as directory:
        plugin = object.__new__(SelfieImagePlugin)
        plugin._web_task_lock = __import__("threading").RLock()
        plugin._web_tasks = {
            "web-12345678-1": {
                "task_id": "web-12345678-1",
                "status": "running",
                "owner_session": "web",
                "cancel_requested": False,
            }
        }
        plugin._runtime_generation_tasks = {"web-12345678-1": SimpleNamespace(done=lambda: False)}
        plugin._web_task_timestamp = lambda: "now"
        plugin._persist_web_tasks_locked = lambda: None
        message = plugin.cancel_image_task("web-12345678-1", is_admin=True)
        assert "安全边界" in message
        task = plugin._web_tasks["web-12345678-1"]
        assert task["status"] == "running"
        assert task["cancel_requested"] is True


def test_task_operation_matrix_preserves_terminal_evidence() -> None:
    active = WebTaskMixin.task_operation_capabilities({"status": "running"})
    assert active == {"can_cancel": True, "can_delete": False, "can_retry": False, "is_terminal": False}
    delivered = WebTaskMixin.task_operation_capabilities({
        "status": "delivery_failed",
        "record_ids": ["record-1"],
    })
    assert delivered == {"can_cancel": False, "can_delete": True, "can_retry": True, "is_terminal": True}
    requested = WebTaskMixin.task_operation_capabilities({"status": "running", "cancel_requested": True})
    assert requested["can_cancel"] is False


def test_task_operation_matrix_covers_unknown_delivery_and_partial_batch_states() -> None:
    unknown = WebTaskMixin.task_operation_capabilities({
        "status": "succeeded",
        "delivery_unknown": True,
        "record_ids": ["record-1"],
    })
    assert unknown == {"can_cancel": False, "can_delete": True, "can_retry": True, "is_terminal": True}
    partial = WebTaskMixin.task_operation_capabilities({
        "status": "partial_success",
        "record_ids": ["record-1", "record-2"],
    })
    assert partial["is_terminal"] is True
    assert partial["can_cancel"] is False
    assert partial["can_retry"] is True


def test_cancel_resolution_preserves_all_completion_evidence_classes() -> None:
    assert result_has_completion_evidence({"success": True})
    assert result_has_completion_evidence({"delivery_failed": True, "generation_success": True})
    assert result_has_completion_evidence({"delivery_unknown": True, "generation_success": True})
    assert result_has_completion_evidence({"status": "partial_success", "succeeded_count": 1})
    for result in (
        {"success": True, "files": ["a.png"]},
        {"delivery_failed": True, "generation_success": True, "error": "send"},
        {"delivery_unknown": True, "generation_success": True},
    ):
        resolved, cancelled = resolve_cancel_request(result, True)
        assert cancelled is False
        assert resolved == result

    resolved, cancelled = resolve_cancel_request({"success": False, "error": "slow"}, True)
    assert cancelled is True
    assert resolved["status"] == "cancelled"
    assert resolved["cancelled"] is True


@pytest.mark.parametrize("media_type", ["image", "video"])
def test_web_runner_publishes_terminal_state_after_late_cancel(media_type: str) -> None:
    import threading

    plugin = object.__new__(SelfieImagePlugin)
    task_id = f"web-12345678-{1 if media_type == 'image' else 2}"
    plugin._web_task_lock = threading.RLock()
    plugin._web_tasks = {
        task_id: {
            "task_id": task_id,
            "status": "queued",
            "cancel_requested": False,
            "request_data": {"prompt": "test"},
            "source": "web-test",
        }
    }
    plugin._web_task_timestamp = lambda: "now"
    plugin._persist_web_tasks_locked = lambda: None
    plugin._prune_web_tasks_locked = lambda: None
    delivered = []

    async def wait_commits(_task_id):
        return None

    async def mark_delivery(*_args, **kwargs):
        delivered.append(kwargs)

    async def generate(payload, **_kwargs):
        # Simulate a user clicking cancel while the provider response is in
        # flight. Completion evidence must win over the late request.
        plugin._web_tasks[task_id]["cancel_requested"] = True
        result = {
            "success": True,
            "files": ["cache/result.png" if media_type == "image" else "cache/result.mp4"],
            "requested_count": 1,
            "succeeded_count": 1,
            "failed_count": 0,
            "delivery_success": True,
        }
        result["image_paths" if media_type == "image" else "generated_video_paths"] = result["files"]
        return result

    plugin._wait_for_record_commits = wait_commits
    plugin._mark_task_records_delivery = mark_delivery
    if media_type == "image":
        plugin.web_test_image = generate
    else:
        plugin.web_test_video = generate

    asyncio.run(
        plugin._run_web_image_task(
            task_id,
            {"media_type": media_type, "prompt": "test", "count": 1},
        )
    )
    task = plugin._web_tasks[task_id]
    assert task["status"] == "succeeded"
    assert task["success"] is True
    assert task["cancel_requested"] is False
    assert delivered and delivered[0]["delivered"] is True


def test_command_runner_releases_quota_after_terminal_publish() -> None:
    import threading

    plugin = object.__new__(SelfieImagePlugin)
    task_id = "cmd-12345678-1"
    plugin._web_task_lock = threading.RLock()
    plugin._web_tasks = {
        task_id: {
            "task_id": task_id,
            "status": "queued",
            "cancel_requested": False,
            "request_data": {"prompt": "chat"},
            "source": "command-image",
            "user_notification_status": "sent",
        }
    }
    plugin._web_task_timestamp = lambda: "now"
    plugin._persist_web_tasks_locked = lambda: None
    plugin._prune_web_tasks_locked = lambda: None
    plugin._wait_for_record_commits = lambda _task_id: asyncio.sleep(0)
    released = []
    plugin._release_quota_reservation = lambda tid: released.append(tid)

    async def runner(_task_id):
        return {
            "success": True,
            "files": ["cache/chat.png"],
            "image_paths": ["cache/chat.png"],
            "requested_count": 1,
            "succeeded_count": 1,
            "failed_count": 0,
            "delivery_success": True,
        }

    asyncio.run(plugin._run_command_image_task(task_id, object(), runner))
    task = plugin._web_tasks[task_id]
    assert task["status"] == "succeeded"
    assert task["generation_success"] is True
    assert task["delivery_success"] is True
    assert released == [task_id]


def test_dashboard_page_has_single_external_source_status_and_p0_controls() -> None:
    page = (ROOT / "pages" / "dashboard" / "index.html").read_text(encoding="utf-8")
    status = dashboard_page_source_status()
    assert status["source"] == "external"
    assert status["path"].endswith("pages/dashboard/index.html")
    fallback = (ROOT / "webui" / "index_fallback.py").read_text(encoding="utf-8")
    assert "Generated by scripts/generate_dashboard_fallback.py" in fallback
    assert "function prepareActiveTestTask" in page
    assert "画布保存失败，未启动生成" in page
    assert "scope_stats" in page


def test_web_route_alias_contract_is_shared_by_both_adapters() -> None:
    assert len(TASK_CANCEL_ROUTE_ALIASES) == len(DASHBOARD_TASK_CANCEL_ROUTES) == 4
    assert DASHBOARD_ROUTE_PREFIXES == ("", "page/")
    assert TASK_CANCEL_ROUTE_ALIASES[0].endswith("tasks/{task_id}/cancel")
    assert DASHBOARD_TASK_CANCEL_ROUTES[0] == "tasks/<task_id>/cancel"


def test_web_contract_matrix_documents_shared_envelopes_and_host_auth_boundary() -> None:
    flask = (ROOT / "webui" / "web.py").read_text(encoding="utf-8")
    dashboard = (ROOT / "webui" / "dashboard_api.py").read_text(encoding="utf-8")
    services = (ROOT / "webui" / "services.py").read_text(encoding="utf-8")
    assert "filter_record_page" in flask
    assert "filter_record_page" in dashboard
    assert "def filter_record_page" in services
    assert "def parse_task_query" in services
    assert "redact_sensitive_data(data)" in flask
    assert "redact_sensitive_data(data)" in dashboard
    assert 'return fail("当前版本不支持资产导出", 501)' in flask
    assert 'return self._fail("当前版本不支持资产导出", 501)' in dashboard
    assert 'return ok(data, count=len(data), load_status=status)' in flask
    assert 'return self._ok(data, count=len(data), load_status=status)' in dashboard
    # The embedded host owns authentication; standalone Flask owns its token.
    assert 'return self._ok({"authorized": True, "source": "dashboard"})' in dashboard
    assert 'if not check_auth():' in flask


def test_dashboard_query_bounds_match_flask_contract() -> None:
    assert parse_task_query({"limit": "200"})["limit"] == 200
    with pytest.raises(WebContractError, match="不能大于"):
        parse_task_query({"limit": "201"})


def test_shared_web_contract_keeps_flask_and_dashboard_record_scope_identical() -> None:
    records = [
        {"success": True, "source": "web", "media_type": "image", "tags": ["keep"]},
        {"success": False, "source": "cmd", "media_type": "video", "tags": []},
    ]
    page, meta = filter_record_page(records, {"success": "true", "tag": "keep"})
    assert len(page) == 1
    assert meta["filtered"] == 1
    assert meta["scope_stats"]["sample_count"] == 1


def test_provider_factory_returns_concrete_generate_implementation_for_every_type() -> None:
    for provider_type, adapter_type in IMAGE_ADAPTER_TYPES.items():
        target = ImageModelTarget(
            channel_name="test",
            provider_type=provider_type,
            base_url="https://example.test",
            api_key="test-key",
            model="test-model",
            timeout=10,
        )
        adapter = create_adapter(target, object())
        assert isinstance(adapter, adapter_type)
        assert adapter.__class__.generate is not BaseImageAdapter.generate
        assert adapter.is_adapter_contract is True


def test_restart_reconciliation_links_expired_task_to_one_record() -> None:
    plugin = object.__new__(SelfieImagePlugin)
    plugin._web_task_lock = __import__("threading").RLock()
    plugin._records_lock = __import__("threading").RLock()
    plugin._web_tasks = {
        "web-12345678-1": {
            "task_id": "web-12345678-1",
            "status": "expired",
            "error": "插件重启后未恢复该任务，请重新提交",
            "source": "web-test",
            "request_data": {"original_prompt": "restart me", "model": "vision-a"},
        }
    }
    plugin._records = []
    plugin._web_task_timestamp = lambda: "now"
    plugin._persist_web_tasks_locked = lambda: None
    plugin._record_task = lambda row: plugin._records.append({"id": "record-1", **row})

    assert plugin.reconcile_expired_tasks_after_restart() == 1
    assert len(plugin._records) == 1
    assert plugin._records[0]["task_id"] == "web-12345678-1"
    assert plugin._records[0]["status"] == "expired"
    assert plugin._records[0]["response_data"]["notification_status"] == "not_sent"
    task = plugin._web_tasks["web-12345678-1"]
    assert task["restart_reconciled"] is True
    assert task["user_notification_status"] == "not_sent"
    assert plugin.reconcile_expired_tasks_after_restart() == 0


def test_restart_load_releases_persisted_quota_marker() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "generation_tasks.json"
        path.write_text(
            json.dumps(
                {
                    "tasks": {
                        "web-12345678-1": {
                            "task_id": "web-12345678-1",
                            "status": "running",
                            "quota_reserved": 3,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        plugin = object.__new__(SelfieImagePlugin)
        plugin.tasks_path = str(path)
        plugin._web_task_timestamp = lambda: "now"
        tasks = plugin._load_web_tasks()
        task = tasks["web-12345678-1"]
        assert task["status"] == "expired"
        assert task["quota_reserved"] == 0
        assert task["quota_released"] is True
        assert task["quota_released_ts"]


def test_quota_release_is_idempotent_and_task_notification_outcomes_are_durable() -> None:
    plugin = object.__new__(SelfieImagePlugin)
    plugin._web_task_lock = __import__("threading").RLock()
    plugin._quota_reservation_lock = __import__("threading").RLock()
    plugin._quota_reservations = {
        "cmd-12345678-1": {"user_id": "u1", "count": 2},
    }
    plugin._web_tasks = {
        "cmd-12345678-1": {
            "task_id": "cmd-12345678-1",
            "status": "running",
            "quota_reserved": 2,
            "quota_released": False,
            "user_notification_status": "pending",
        }
    }
    persisted = []
    plugin._persist_web_tasks_locked = lambda: persisted.append(True)
    plugin._web_task_timestamp = lambda: "now"

    plugin._release_quota_reservation("cmd-12345678-1")
    plugin._release_quota_reservation("cmd-12345678-1")

    task = plugin._web_tasks["cmd-12345678-1"]
    assert plugin._quota_reservations == {}
    assert task["quota_reserved"] == 0
    assert task["quota_released"] is True
    assert len(persisted) == 1

    class Event:
        def plain_result(self, text):
            return text

        async def send(self, _message):
            return None

    assert asyncio.run(plugin._send_task_notification("cmd-12345678-1", Event(), "已完成")) is True
    assert task["user_notification_status"] == "sent"

    class FailingEvent(Event):
        async def send(self, _message):
            raise RuntimeError("transport receipt unavailable")

    assert asyncio.run(plugin._send_task_notification("cmd-12345678-1", FailingEvent(), "失败")) is False
    assert task["user_notification_status"] == "failed"
    assert "transport" in task["user_notification_reason"]


def test_dashboard_and_flask_handlers_share_response_matrix(monkeypatch) -> None:
    """Exercise both transport adapters against one plugin contract surface."""
    import astrbot_plugin_selfie_image.webui.dashboard_api as dashboard_module

    class ContractPlugin:
        def __init__(self):
            self.config = SimpleNamespace(web_token="secret")

        def get_recent_records(self, *args, **kwargs):
            return [{"id": "record-1", "success": True, "source": "web"}]

        def cleanup_image_cache_from_web(self, **kwargs):
            if kwargs.get("confirm"):
                raise ValueError("缓存内容已变化，请重新预览后再确认清理")
            return {"requires_confirmation": True}

    plugin = ContractPlugin()
    flask_client = FlaskWebServer(plugin)._create_app().test_client()
    headers = {"X-Selfie-Image-Token": "secret"}

    class FakeRequest:
        query = {}
        payload = {}

        async def json(self, default=None):
            return self.payload if self.payload is not None else default

    response_factory = lambda payload, **_kwargs: payload
    error_factory = lambda message, **kwargs: {"error": message, "status": kwargs.get("status_code")}
    monkeypatch.setattr(dashboard_module, "request", FakeRequest())
    monkeypatch.setattr(dashboard_module, "json_response", response_factory)
    monkeypatch.setattr(dashboard_module, "error_response", error_factory)
    api = dashboard_module.SelfieImageDashboardAPI(plugin)

    FakeRequest.query = {"limit": "10"}
    dashboard_success = asyncio.run(api.page_records())
    flask_success = flask_client.get("/api/records?limit=10", headers=headers)
    assert flask_success.status_code == 200
    assert dashboard_success["data"]["success"] is True
    assert dashboard_success["data"]["data"][0]["id"] == flask_success.get_json()["data"][0]["id"]

    FakeRequest.query = {"success": "maybe"}
    dashboard_bad = asyncio.run(api.page_records())
    flask_bad = flask_client.get("/api/records?success=maybe", headers=headers)
    assert dashboard_bad["status"] == flask_bad.status_code == 400

    FakeRequest.payload = {"confirm": True, "plan_token": "stale"}
    dashboard_conflict = asyncio.run(api.page_cache_cleanup())
    flask_conflict = flask_client.post("/api/cache/cleanup", json=FakeRequest.payload, headers=headers)
    assert dashboard_conflict["status"] == flask_conflict.status_code == 409

    FakeRequest.query = {}
    dashboard_unsupported = asyncio.run(api.page_assets_export())
    flask_unsupported = flask_client.get("/api/assets/export", headers=headers)
    assert dashboard_unsupported["status"] == flask_unsupported.status_code == 501

    dashboard_missing = asyncio.run(api.page_task_detail("web-12345678-1"))
    flask_missing = flask_client.get("/api/tasks/web-12345678-1", headers=headers)
    assert dashboard_missing["status"] == flask_missing.status_code == 404

    FakeRequest.payload = {}
    dashboard_server_error = asyncio.run(api.page_config_post())
    flask_server_error = flask_client.post("/api/config", json={}, headers=headers)
    assert dashboard_server_error["status"] == flask_server_error.status_code == 500

    assert flask_client.get("/api/records").status_code == 401
    auth = asyncio.run(api.page_auth_check())
    assert auth["data"]["data"] == {"authorized": True, "source": "dashboard"}


def test_flask_registers_every_task_cancel_alias() -> None:
    plugin = SimpleNamespace(
        config=SimpleNamespace(web_token=""),
    )
    app = FlaskWebServer(plugin)._create_app()
    routes = {rule.rule for rule in app.url_map.iter_rules()}
    assert {route.replace("{task_id}", "<task_id>") for route in TASK_CANCEL_ROUTE_ALIASES} <= routes
