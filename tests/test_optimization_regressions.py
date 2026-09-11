from __future__ import annotations

import asyncio
import logging
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_selfie_image.generation.generation_records import build_record_scope_stats
from astrbot_plugin_selfie_image.generation.generation_results import (
    resolve_cancel_request,
    result_has_completion_evidence,
)
from astrbot_plugin_selfie_image.core.models import ImageModelTarget
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
    filter_record_page,
    parse_task_query,
)


ROOT = Path(__file__).resolve().parents[1]


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
