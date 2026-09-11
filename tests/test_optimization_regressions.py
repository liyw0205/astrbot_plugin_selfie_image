from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_selfie_image.generation.generation_records import build_record_scope_stats
from astrbot_plugin_selfie_image.generation.generation_results import (
    resolve_cancel_request,
    result_has_completion_evidence,
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
    assert '"scope_stats": build_record_scope_stats(filtered)' in flask
    assert '"scope_stats": build_record_scope_stats(filtered)' in dashboard
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
    dashboard = (ROOT / "webui" / "dashboard_api.py").read_text(encoding="utf-8")
    assert "不能大于 {maximum}" in dashboard


def test_flask_registers_every_task_cancel_alias() -> None:
    plugin = SimpleNamespace(
        config=SimpleNamespace(web_token=""),
    )
    app = FlaskWebServer(plugin)._create_app()
    routes = {rule.rule for rule in app.url_map.iter_rules()}
    assert {route.replace("{task_id}", "<task_id>") for route in TASK_CANCEL_ROUTE_ALIASES} <= routes
