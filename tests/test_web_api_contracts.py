"""Executable contracts at the Flask and registered Dashboard boundaries."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest
from werkzeug.datastructures import MultiDict
from werkzeug.routing import Map, Rule

from astrbot_plugin_selfie_image.core.constants import PLUGIN_NAME
from astrbot_plugin_selfie_image.features.config_manager import ConfigurationMixin
from astrbot_plugin_selfie_image.features.creative_features import CreativeFeaturesMixin
from astrbot_plugin_selfie_image.tasks.task_manager import WebTaskMixin
from astrbot_plugin_selfie_image.webui import dashboard_api as dashboard_module
from astrbot_plugin_selfie_image.webui.contracts import TASK_CANCEL_ROUTE_ALIASES
from astrbot_plugin_selfie_image.webui.web import FlaskWebServer


TOKEN = "contract-token"
TASK_ID = "web-12345678-1"
RUNNING_ID = "web-studio-12345678-2"
FINISHED_ID = "cmd-12345678-3"
MISSING_BODY = object()


class ContractPlugin(ConfigurationMixin, CreativeFeaturesMixin, WebTaskMixin):
    def __init__(self, directory):
        self._config_lock = threading.RLock()
        self._web_task_lock = threading.RLock()
        self.config_path = str(directory / "config.json")
        self.key_config = {"web": {"enable": False, "token": TOKEN}}
        self._apply_raw_config({
            "compatibility_field": {"keep": "legacy"},
            "image_channels": [{
                "name": "legacy", "base_url": "https://relay.example.test",
                "api_key": "channel-secret", "model": "gpt-image-1", "provider_type": "openai",
            }],
            "proxies": [{
                "id": "legacy-proxy", "url": "http://user:proxy-secret@proxy.example.test:8080",
                "compatibility_field": True,
            }],
        })
        self._web_tasks = {}
        for task_id, status, hour, prompt in (
            (TASK_ID, "queued", 10, "Portrait sunrise"),
            (RUNNING_ID, "running", 11, "Portrait sunset"),
            (FINISHED_ID, "succeeded", 12, "Landscape sunrise"),
        ):
            self._web_tasks[task_id] = {
                "task_id": task_id, "status": status, "media_type": "image",
                "source": "web-test" if task_id == TASK_ID else "studio",
                "created_ts": datetime.fromisoformat(f"2026-11-12T{hour}:00:00+00:00").timestamp(),
                "cancel_requested": False,
                "request_data": {"original_prompt": prompt, "model": "vision-a", "api_key": "task-secret"},
            }
        self.persisted_tasks = 0
        self.cancel_calls = []

    def _persist_web_tasks_locked(self):
        self.persisted_tasks += 1

    def cancel_image_task(self, task_id, *, is_admin=False):
        self.cancel_calls.append((task_id, is_admin))
        task = self._web_tasks[task_id]
        if task["status"] == "queued":
            task["status"] = "cancelled"
        else:
            task["cancel_requested"] = True
        return "cancel requested"


@dataclass
class HttpResult:
    status: int
    payload: dict

    @property
    def envelope(self):
        if self.payload.get("status") == "error":
            return {"success": False, "error": self.payload["message"]}
        if "success" in self.payload:
            return self.payload
        return self.payload["data"]


@pytest.fixture
def standalone(tmp_path):
    plugin = ContractPlugin(tmp_path)
    return FlaskWebServer(plugin)._create_app().test_client(), plugin


@pytest.fixture(params=["flask", "dashboard"])
def http(request, monkeypatch, tmp_path):
    plugin = ContractPlugin(tmp_path)
    adapter = request.param

    if adapter == "flask":
        client = FlaskWebServer(plugin)._create_app().test_client()

        def call(path, *, method="GET", body=MISSING_BODY, headers=None, page_prefix=""):
            options = {"headers": {"Authorization": f"Bearer {TOKEN}"} if headers is None else headers}
            if body is not MISSING_BODY:
                options.update(data=json.dumps(body), content_type="application/json")
            response = client.open(path, method=method, **options)
            return HttpResult(response.status_code, response.get_json())

        registered = None
    else:
        class HostRequest:
            query = MultiDict()
            method = "GET"
            body = MISSING_BODY

            async def json(self, default=None):
                return default if self.body is MISSING_BODY else self.body

        host_request = HostRequest()
        registered = []
        plugin.context = SimpleNamespace(register_web_api=lambda *args: registered.append(args))
        api = dashboard_module.SelfieImageDashboardAPI(plugin)
        monkeypatch.setattr(dashboard_module, "request", host_request)
        monkeypatch.setattr(dashboard_module, "json_response", lambda payload, status_code=200: HttpResult(status_code, payload))
        monkeypatch.setattr(dashboard_module, "error_response", lambda message, status_code=400: HttpResult(
            status_code, {"status": "error", "message": message, "data": {}},
        ))
        api.register()
        routes = Map([Rule(path, endpoint=handler, methods=methods) for path, handler, methods, _desc in registered])

        def call(path, *, method="GET", body=MISSING_BODY, headers=None, page_prefix=""):
            parsed = urlsplit(path)
            host_request.query = MultiDict(parse_qsl(parsed.query, keep_blank_values=True))
            host_request.method = method
            host_request.body = body
            path = f"/{PLUGIN_NAME}/{page_prefix}{parsed.path.removeprefix('/api/')}"
            handler, values = routes.bind("localhost").match(path, method=method)
            return asyncio.run(handler(**values))

    return SimpleNamespace(plugin=plugin, adapter=adapter, call=call, registered=registered)


def assert_success(result):
    assert result.status == 200
    assert result.envelope["success"] is True
    return result.envelope["data"]


def assert_failure(result, status, message=None):
    assert result.status == status
    assert result.envelope["success"] is False
    if message:
        assert message in result.envelope["error"]


@pytest.mark.parametrize("prefix", ["", "page/"])
@pytest.mark.parametrize("path,method,body", [
    ("/api/config", "GET", MISSING_BODY),
    ("/api/tasks", "GET", MISSING_BODY),
    ("/api/creative/variations", "POST", {"prompt": "portrait", "variation_seed": "fixed"}),
])
def test_registered_routes_keep_dashboard_prefixes_and_success_wrappers(http, prefix, path, method, body):
    result = http.call(path, method=method, body=body, page_prefix=prefix)
    assert_success(result)
    if http.adapter == "dashboard":
        assert set(result.payload) == {"data"}
        assert result.payload["data"] == result.envelope
    else:
        assert result.payload == result.envelope


PROTECTED_ROUTES = [
    ("/api/config", "GET"), ("/api/config", "POST"), ("/api/config/export", "GET"),
    ("/api/config/import/preview", "POST"), ("/api/config/import", "POST"),
    ("/api/proxies", "GET"), ("/api/proxies/test", "POST"), ("/api/proxies/quality-check", "POST"),
    ("/api/tasks", "GET"), (f"/api/tasks/{TASK_ID}", "GET"),
    ("/api/tasks/export", "GET"), ("/api/tasks/delete", "POST"), ("/api/tasks/retry", "POST"),
    ("/api/creative/variations", "POST"),
    *[(route.format(task_id=TASK_ID), "POST") for route in TASK_CANCEL_ROUTE_ALIASES],
]


@pytest.mark.parametrize("path,method", PROTECTED_ROUTES)
def test_standalone_auth_runs_before_payload_and_business_validation(standalone, path, method):
    client, plugin = standalone
    response = client.open(path, method=method, json=[])
    assert_failure(HttpResult(response.status_code, response.get_json()), 401, "Unauthorized")
    assert not plugin.cancel_calls


@pytest.mark.parametrize("header,value", [
    ("Authorization", f"Bearer {TOKEN}"), ("Authorization", f"bEaReR {TOKEN}"),
    ("X-Selfie-Image-Token", TOKEN), ("X-AICat-Token", TOKEN), ("X-Token", TOKEN),
])
def test_standalone_auth_accepts_legacy_header_aliases(http, header, value):
    assert_success(http.call("/api/config", headers={header: value}))


def test_host_auth_is_not_replaced_by_standalone_token_auth(http):
    if http.adapter == "dashboard":
        data = assert_success(http.call("/api/auth/check", headers={"Authorization": "Bearer wrong"}))
        assert data == {"authorized": True, "source": "dashboard"}
        assert_success(http.call("/api/config", headers={}))
    else:
        assert_failure(http.call(f"/api/config?token={TOKEN}", headers={}), 401)
        assert_failure(http.call("/api/config", headers={"X-Token": "wrong"}), 401)
        assert_success(http.call("/api/config", headers={"Authorization": "Bearer wrong", "X-AICat-Token": TOKEN}))
        http.plugin.config.web_token = ""
        assert_success(http.call("/api/config", headers={}))


def test_config_and_proxy_reads_mask_legacy_credentials_without_losing_fields(http):
    config = assert_success(http.call("/api/config"))
    proxies = assert_success(http.call("/api/proxies"))
    assert "channel-secret" not in json.dumps(config)
    assert "proxy-secret" not in json.dumps(config)
    assert "proxy-secret" not in json.dumps(proxies)
    assert TOKEN not in json.dumps(config)
    assert config["compatibility_field"] == {"keep": "legacy"}
    assert config["image_channels"][0]["id"]
    assert proxies[0]["id"] == "legacy-proxy"
    assert proxies[0]["host"] == "proxy.example.test"
    assert proxies[0]["port"] == 8080
    assert proxies[0]["username"] == "user"
    assert http.plugin.config.image_channels[0].api_key == "channel-secret"
    assert http.plugin.config.proxies[0]["password"] == "proxy-secret"


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("operation", ["save", "import"])
def test_config_flat_and_wrapped_masked_roundtrips_keep_identity_and_runtime_secrets(http, wrapped, operation):
    public = assert_success(http.call("/api/config"))
    channel_id = public["image_channels"][0]["id"]
    public["image_channels"][0]["name"] = "renamed"
    public["proxies"][0]["name"] = "renamed proxy"
    public["web"] = {"token": "browser-token", "enable": True}
    body = {"config": public} if wrapped else public
    if operation == "import":
        before = copy.deepcopy(http.plugin.raw_config)
        preview = assert_success(http.call("/api/config/import/preview", method="POST", body=body))
        assert preview["ok"] is True
        assert http.plugin.raw_config == before
        assert "channel-secret" not in json.dumps(preview)
        assert "proxy-secret" not in json.dumps(preview)
    path = "/api/config" if operation == "save" else "/api/config/import"
    saved = assert_success(http.call(path, method="POST", body=body))
    assert saved["image_channels"][0]["id"] == channel_id
    assert saved["image_channels"][0]["name"] == "renamed"
    assert saved["compatibility_field"] == {"keep": "legacy"}
    assert http.plugin.config.image_channels[0].api_key == "channel-secret"
    assert http.plugin.config.proxies[0]["password"] == "proxy-secret"
    assert http.plugin.config.web_token == TOKEN


@pytest.mark.parametrize("mode,raw", [("", False), ("masked", False), ("unknown", False), (" RAW ", True), ("plain", True), ("original", True)])
def test_config_export_modes_are_explicit_and_startup_token_stays_private(http, mode, raw):
    from urllib.parse import urlencode

    data = assert_success(http.call("/api/config/export?" + urlencode({"mode": mode})))
    assert ("channel-secret" in json.dumps(data)) is raw
    assert ("proxy-secret" in json.dumps(data)) is raw
    assert TOKEN not in json.dumps(data)
    assert data["schema_version"] == 2
    assert ("_export_note" in data) is not raw


OBJECT_BODY_ROUTES = ["/api/config", "/api/config/import/preview", "/api/config/import", "/api/proxies/test", "/api/proxies/quality-check", "/api/creative/variations"]


@pytest.mark.parametrize("path", OBJECT_BODY_ROUTES)
@pytest.mark.parametrize("body", [None, [], "value", 1, True])
def test_json_null_arrays_and_scalars_are_not_object_payloads(http, path, body):
    assert_failure(http.call(path, method="POST", body=body), 400, "JSON")


@pytest.mark.parametrize("path", OBJECT_BODY_ROUTES)
@pytest.mark.parametrize("body,content_type", [("{", "application/json"), ("not-json", "text/plain")])
def test_standalone_rejects_malformed_or_non_json_nonempty_bodies(standalone, path, body, content_type):
    client, _plugin = standalone
    response = client.post(path, data=body, content_type=content_type, headers={"X-Token": TOKEN})
    assert_failure(HttpResult(response.status_code, response.get_json()), 400, "JSON")


@pytest.mark.parametrize("prefix", ["", "page/"])
@pytest.mark.parametrize("path", ["/api/config", "/api/config/import/preview", "/api/config/import"])
def test_missing_config_body_retains_empty_patch_compatibility(http, path, prefix):
    assert_success(http.call(path, method="POST", page_prefix=prefix))


@pytest.mark.parametrize("path", ["/api/config", "/api/config/import/preview", "/api/config/import"])
@pytest.mark.parametrize("value", [None, [], "value"])
def test_present_config_wrapper_must_be_an_object(http, path, value):
    before = copy.deepcopy(http.plugin.raw_config)
    assert_failure(http.call(path, method="POST", body={"config": value}), 400, "config")
    assert http.plugin.raw_config == before


def test_config_preflight_failure_preserves_state_and_legacy_error_statuses(http):
    before = copy.deepcopy(http.plugin.raw_config)
    body = {"image_channels": [{"name": "invalid", "provider_type": "openai", "base_url": "invalid", "model": "gpt-image-1"}]}
    preview = assert_success(http.call("/api/config/import/preview", method="POST", body=body))
    assert preview["ok"] is False
    assert preview["errors"]
    assert_failure(http.call("/api/config/import", method="POST", body=body), 400)
    assert_failure(http.call("/api/config", method="POST", body=body), 500)
    assert http.plugin.raw_config == before


@pytest.mark.parametrize("path", ["/api/config/import/preview", "/api/config/import"])
def test_config_import_runtime_failures_are_server_errors(http, monkeypatch, path):
    method = "preview_config_import" if path.endswith("preview") else "import_config_from_web"

    def fail(_payload):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(http.plugin, method, fail)
    assert_failure(http.call(path, method="POST", body={}), 500, "storage unavailable")


def test_config_export_business_failure_is_a_redacted_http_error(http, monkeypatch):
    def fail_export(**kwargs):
        raise RuntimeError("api_key=export-secret-value")

    monkeypatch.setattr(http.plugin, "export_config_for_web", fail_export)
    result = http.call("/api/config/export")
    assert_failure(result, 500)
    assert "export-secret-value" not in json.dumps(result.payload)


@pytest.mark.parametrize("path,method_name", [("/api/proxies/test", "test_proxy_connectivity"), ("/api/proxies/quality-check", "test_proxy_quality")])
@pytest.mark.parametrize("body,proxy_id,proxy", [
    ({"id": "first", "proxy_id": "legacy"}, "first", {"id": "first", "proxy_id": "legacy"}),
    ({"proxy_id": "legacy"}, "legacy", {"proxy_id": "legacy"}),
    ({"id": 12, "proxy": {"host": "proxy.example.test", "port": 8080}}, "12", {"host": "proxy.example.test", "port": 8080}),
    ({"id": "", "proxy_id": "legacy", "proxy": {}}, "legacy", {}),
    ({"host": "proxy.example.test", "port": 8080}, "", {"host": "proxy.example.test", "port": 8080}),
    ({"proxy": "legacy-url"}, "", {"proxy": "legacy-url"}),
])
def test_proxy_probe_legacy_fields_and_nested_proxy_selection(http, monkeypatch, path, method_name, body, proxy_id, proxy):
    async def probe(**kwargs):
        return kwargs

    monkeypatch.setattr(http.plugin, method_name, probe)
    data = assert_success(http.call(path, method="POST", body=body))
    assert data == {"proxy_id": proxy_id, "proxy": proxy}


@pytest.mark.parametrize("path", ["/api/proxies/test", "/api/proxies/quality-check"])
@pytest.mark.parametrize("body", [{}, {"proxy_id": "missing"}, {"proxy": {}}])
def test_proxy_missing_or_invalid_parameters_are_client_errors_without_network(http, path, body):
    assert_failure(http.call(path, method="POST", body=body), 400)


@pytest.mark.parametrize("path,probe_name", [("/api/proxies/test", "probe_proxy_connectivity"), ("/api/proxies/quality-check", "probe_proxy_quality")])
def test_proxy_saved_identity_selects_runtime_credentials_without_live_requests(http, monkeypatch, path, probe_name):
    from astrbot_plugin_selfie_image.core import proxy as proxy_module

    seen = []

    async def probe(url):
        seen.append(url)
        return {"ok": True}

    monkeypatch.setattr(proxy_module, probe_name, probe)
    data = assert_success(http.call(path, method="POST", body={"proxy_id": "legacy-proxy"}))
    assert data["proxy_id"] == "legacy-proxy"
    assert seen == [http.plugin.config.proxies[0]["url"]]
    assert "proxy-secret" in seen[0]
    assert "proxy-secret" not in json.dumps(data)


@pytest.mark.parametrize("path,method_name", [("/api/proxies/test", "test_proxy_connectivity"), ("/api/proxies/quality-check", "test_proxy_quality")])
@pytest.mark.parametrize("error,status", [(ValueError, 400), (RuntimeError, 500), (TimeoutError, 500)])
def test_proxy_parameter_and_runtime_errors_have_matching_redacted_statuses(http, monkeypatch, path, method_name, error, status):
    async def probe(**kwargs):
        raise error("api_key=proxy-probe-secret")

    monkeypatch.setattr(http.plugin, method_name, probe)
    result = http.call(path, method="POST", body={})
    assert_failure(result, status)
    assert "proxy-probe-secret" not in json.dumps(result.payload)


@pytest.mark.parametrize("query,expected", [
    ("", [RUNNING_ID, TASK_ID]),
    ("include_finished=true", [FINISHED_ID, RUNNING_ID, TASK_ID]),
    ("include_finished=on&limit=1&offset=1", [RUNNING_ID]),
    ("source=WEB-TEST", [TASK_ID]),
    ("status=RUNNING,queued", [RUNNING_ID, TASK_ID]),
    ("q=sunrise&keyword=sunset", [TASK_ID]),
    ("keyword=SUNSET", [RUNNING_ID]),
    ("model=VISION-A&media_type=IMAGE", [RUNNING_ID, TASK_ID]),
    ("start_date=2026-11-12T10:30:00Z&end_date=2026-11-12T11:30:00Z", [RUNNING_ID]),
    ("include_finished=maybe&limit=200&offset=1000000", []),
    ("limit=&offset=", [RUNNING_ID, TASK_ID]),
])
def test_task_query_matrix_filters_before_pagination_at_both_boundaries(http, query, expected):
    data = assert_success(http.call("/api/tasks?" + query))
    assert [row["task_id"] for row in data["tasks"]] == expected
    assert data["total"] == 3
    assert "task-secret" not in json.dumps(data)


@pytest.mark.parametrize("query", [
    "limit=0", "limit=-1", "limit=201", "limit=1.5", "limit=nope",
    "offset=-1", "offset=1000001", "offset=1.5", "status=unknown", "status=running,unknown",
    "media_type=video", "start_date=invalid", "end_date=2026-99-99",
    "start_date=2026-11-13&end_date=2026-11-12",
])
def test_invalid_task_query_matrix_returns_client_errors(http, query):
    assert_failure(http.call("/api/tasks?" + query), 400)


@pytest.mark.parametrize("route", ["tasks/{task_id}", "test-image-channel/tasks/{task_id}", "studio/tasks/{task_id}"])
@pytest.mark.parametrize("task_id,status", [(TASK_ID, 200), (RUNNING_ID, 200), (FINISHED_ID, 200), ("web-12345678-999", 404), ("invalid", 400), ("web-" + "1" * 60 + "-1", 400)])
def test_task_detail_and_legacy_status_routes_validate_and_redact(http, route, task_id, status):
    result = http.call("/api/" + route.format(task_id=task_id))
    if status == 200:
        assert assert_success(result)["task_id"] == task_id
        assert "task-secret" not in json.dumps(result.payload)
    else:
        assert_failure(result, status)


@pytest.mark.parametrize("route", TASK_CANCEL_ROUTE_ALIASES)
@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "partial_success", "failed", "delivery_failed", "cancelled", "expired"])
def test_cancel_aliases_preserve_active_and_terminal_state_matrix(http, route, status):
    http.plugin._web_tasks[TASK_ID]["status"] = status
    result = http.call(route.format(task_id=TASK_ID), method="POST")
    if status in {"queued", "running"}:
        data = assert_success(result)
        assert http.plugin.cancel_calls == [(TASK_ID, True)]
        assert result.envelope["message"] == "cancel requested"
        if status == "queued":
            assert data["status"] == "cancelled"
        else:
            assert data["cancel_requested"] is True
        assert "task-secret" not in json.dumps(result.payload)
    else:
        assert_failure(result, 409)
        assert not http.plugin.cancel_calls
        assert http.plugin._web_tasks[TASK_ID]["status"] == status


@pytest.mark.parametrize("prefix", ["", "page/"])
@pytest.mark.parametrize("route", TASK_CANCEL_ROUTE_ALIASES)
@pytest.mark.parametrize("case,status", [("invalid", 400), ("missing", 404), ("permission", 403), ("completed_during_cancel", 409)])
def test_cancel_aliases_match_validation_permission_and_completion_races(http, monkeypatch, route, case, status, prefix):
    task_id = "invalid" if case == "invalid" else "web-12345678-999" if case == "missing" else TASK_ID
    if case == "permission":
        def cancel(*args, **kwargs):
            raise PermissionError("api_key=permission-secret")
        monkeypatch.setattr(http.plugin, "cancel_image_task", cancel)
    elif case == "completed_during_cancel":
        def cancel(*args, **kwargs):
            http.plugin._web_tasks[TASK_ID]["status"] = "succeeded"
            return "finished"
        monkeypatch.setattr(http.plugin, "cancel_image_task", cancel)
    result = http.call(route.format(task_id=task_id), method="POST", page_prefix=prefix)
    assert_failure(result, status)
    assert "permission-secret" not in json.dumps(result.payload)


@pytest.mark.parametrize("operation", ["delete", "retry"])
@pytest.mark.parametrize("key", ["ids", "task_ids"])
def test_task_batch_legacy_ids_are_trimmed_deduplicated_and_ordered(http, monkeypatch, operation, key):
    def perform(ids, *args):
        return {"ids": ids, "args": list(args)}

    monkeypatch.setattr(http.plugin, f"{operation}_web_tasks", perform)
    body = {key: [f" {TASK_ID} ", TASK_ID, "", None, FINISHED_ID], "feedback": "  correction  ", "retry_strategy": "model_only"}
    data = assert_success(http.call(f"/api/tasks/{operation}", method="POST", body=body))
    assert data["ids"] == [TASK_ID, FINISHED_ID]
    assert data["args"] == (["correction", "model_only"] if operation == "retry" else [])


@pytest.mark.parametrize("operation", ["delete", "retry"])
@pytest.mark.parametrize("body", [{}, {"ids": "value"}, {"ids": []}, {"ids": ["invalid"]}, {"ids": [], "task_ids": [TASK_ID]}, {"ids": [f"web-12345678-{i}" for i in range(201)]}])
def test_task_batch_invalid_ids_do_not_reach_business_layer(http, monkeypatch, operation, body):
    def unexpected(*args):
        pytest.fail("invalid ids reached task mutation")

    monkeypatch.setattr(http.plugin, f"{operation}_web_tasks", unexpected)
    assert_failure(http.call(f"/api/tasks/{operation}", method="POST", body=body), 400)


@pytest.mark.parametrize("legacy_signature", [False, True])
def test_task_retry_feedback_bound_and_strategy_precedence_keep_legacy_call_compatibility(http, monkeypatch, legacy_signature):
    if legacy_signature:
        def retry(ids, feedback):
            return {"ids": ids, "feedback": feedback}
    else:
        def retry(ids, feedback, strategy):
            return {"ids": ids, "feedback": feedback, "strategy": strategy}
    monkeypatch.setattr(http.plugin, "retry_web_tasks", retry)
    body = {"ids": [TASK_ID], "feedback": "  " + "x" * 2100 + "  ", "strategy": "full", "retry_strategy": "model_only"}
    data = assert_success(http.call("/api/tasks/retry", method="POST", body=body))
    assert data["feedback"] == "x" * 2000
    if not legacy_signature:
        assert data["strategy"] == "full"


@pytest.mark.parametrize("query,status,count", [("", 200, 3), (f"ids={TASK_ID},{FINISHED_ID}", 200, 2), ("ids=invalid", 400, None)])
def test_task_export_validation_and_selection(http, query, status, count):
    result = http.call("/api/tasks/export?" + query)
    if status == 200:
        data = assert_success(result)
        assert data["count"] == count
        assert "task-secret" not in json.dumps(data)
    else:
        assert_failure(result, status)


@pytest.mark.parametrize("operation,method_name,method", [("export", "export_web_tasks", "GET"), ("delete", "delete_web_tasks", "POST"), ("retry", "retry_web_tasks", "POST")])
def test_unavailable_task_operations_remain_explicitly_unsupported(http, monkeypatch, operation, method_name, method):
    monkeypatch.setattr(http.plugin, method_name, None)
    assert_failure(http.call(f"/api/tasks/{operation}", method=method, body={"ids": [TASK_ID]}), 501)


@pytest.mark.parametrize("count,expected", [(MISSING_BODY, 1), (None, 1), (0, 1), ([], 1), ({}, 1), (-1, 1), ("2", 2), (2.9, 2), (100, 100), (101, 100)])
def test_creative_count_legacy_defaults_and_bounded_edges(http, count, expected):
    body = {"prompt": "portrait", "variation_seed": "fixed"}
    if count is not MISSING_BODY:
        body["count"] = count
    rows = assert_success(http.call("/api/creative/variations", method="POST", body=body))["variations"]
    assert len(rows) == expected
    assert [row["index"] for row in rows] == list(range(1, expected + 1))
    assert rows[0]["seed"] == "fixed:0"


@pytest.mark.parametrize("count", ["invalid", "2.5", [1], {"value": 1}])
def test_creative_invalid_nonempty_counts_return_client_errors(http, count):
    assert_failure(http.call("/api/creative/variations", method="POST", body={"count": count}), 400)


@pytest.mark.parametrize("field", ["vary", "variation_fields"])
@pytest.mark.parametrize("seed_field", ["template_seed", "variation_seed"])
def test_creative_variation_field_and_seed_aliases_forward_full_payload(http, field, seed_field):
    body = {
        "prompt": "{character} at {scene}", "count": 3, field: "scene,pose", seed_field: "stable",
        "template_values": {"character": "fixed character", "pose": "fixed pose"},
        "template_pools": {"scene": ["harbor", "garden"]},
    }
    first = assert_success(http.call("/api/creative/variations", method="POST", body=body))["variations"]
    second = assert_success(http.call("/api/creative/variations", method="POST", body=body))["variations"]
    assert first == second
    assert all(row["vary"] == ["scene", "pose"] for row in first)
    assert all(row["values"]["character"] == "fixed character" for row in first)
    assert all(row["values"]["pose"] == "fixed pose" for row in first)
    assert all(row["values"]["scene"] in {"harbor", "garden"} for row in first)
    assert [row["seed"] for row in first] == [f"stable:{i}" for i in range(3)]


def test_creative_primary_fields_override_legacy_aliases_and_missing_body_is_supported(http):
    body = {"prompt": "portrait", "variation_fields": ["scene"], "vary": ["pose"], "variation_seed": "primary", "template_seed": "legacy"}
    row = assert_success(http.call("/api/creative/variations", method="POST", body=body))["variations"][0]
    assert row["vary"] == ["scene"]
    assert row["seed"] == "primary:0"
    assert len(assert_success(http.call("/api/creative/variations", method="POST"))["variations"]) == 1
