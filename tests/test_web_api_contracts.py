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
from astrbot_plugin_selfie_image.webui.contracts import (
    ASSET_COLLECTION_ROUTE_CONTRACTS,
    ASSET_ROUTE_CONTRACTS,
    CANVAS_ROUTE_CONTRACTS,
    CONFIG_ROUTE_CONTRACTS,
    PROXY_ROUTE_CONTRACTS,
    RECORD_ROUTE_CONTRACTS,
    TASK_CANCEL_ROUTE_ALIASES,
    TASK_ROUTE_CONTRACTS,
)
from astrbot_plugin_selfie_image.webui.services import WebContractError
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
        self.record_metadata_updates = []
        self.record_compare_calls = []
        self.storage_repair_calls = []
        self.record_clear_calls = 0
        self.asset_query_calls = []
        self.asset_metadata_updates = []
        self.asset_deletions = []
        self.asset_studio_calls = []
        self.asset_collection_calls = []
        self.canvas_calls = []

    def get_recent_records(self, **_kwargs):
        return [{"id": "record-1", "success": True, "source": "web"}]

    def get_generation_metrics(self, *, window_seconds=None):
        return {"window_seconds": window_seconds, "api_key": "metrics-secret"}

    def get_record_for_web(self, record_id):
        if record_id != "record-1":
            raise ValueError("记录不存在")
        return {
            "id": record_id,
            "success": True,
            "generated_image_sources": ["https://images.example.test/result.png"],
            "response_data": {"api_key": "record-secret"},
        }

    def update_record_asset_metadata(self, record_id, payload):
        self.record_metadata_updates.append((record_id, payload))
        return {"id": record_id, "favorite": bool(payload.get("favorite"))}

    def get_record_reuse_payload(self, record_id):
        return {"record_id": record_id, "api_key": "reuse-secret"}

    def start_record_retry_task(self, record_id, feedback):
        return {"record_id": record_id, "feedback": feedback, "api_key": "retry-secret"}

    def inspect_storage_consistency(self):
        return {"issues": [], "api_key": "consistency-secret"}

    def repair_storage_consistency(self, *, confirm=False, kinds=None):
        self.storage_repair_calls.append((confirm, kinds))
        return {"confirmed": confirm, "kinds": kinds, "api_key": "repair-secret"}

    def compare_records_for_web(self, ids, limit=8):
        self.record_compare_calls.append((ids, limit))
        return {"count": len(ids), "ids": ids, "limit": limit}

    def clear_recent_records(self):
        self.record_clear_calls += 1
        return 4

    def query_asset_records(self, **kwargs):
        self.asset_query_calls.append(kwargs)
        data = [{"id": "asset-1", "favorite": kwargs["favorite"] is True}]
        return data, {
            "total": 3, "filtered": len(data), "offset": kwargs["offset"],
            "limit": kwargs["limit"],
        }

    def get_asset_records(self, **kwargs):
        return [{"id": "legacy-asset", "filters": kwargs}]

    def list_asset_tags(self):
        return [{"tag": "portrait", "count": 2}]

    def export_asset_metadata(self, ids=None):
        return {"ids": ids, "count": len(ids) if ids is not None else 1}

    def import_asset_metadata(self, payload):
        return {"records": payload.get("records", []), "updated_count": 1}

    def update_records_asset_metadata(self, ids, payload):
        self.asset_metadata_updates.append((ids, payload))
        return {"updated": ids, "updated_count": len(ids)}

    def delete_records(self, ids):
        self.asset_deletions.append(ids)
        return {"deleted": ids, "deleted_count": len(ids)}

    def studio_add_asset(self, record_id, payload):
        self.asset_studio_calls.append(([record_id], payload))
        return {"session_id": "studio-1", "nodes": [record_id]}

    def studio_add_assets(self, ids, payload):
        self.asset_studio_calls.append((ids, payload))
        return {"session_id": "studio-1", "nodes": ids}

    def list_asset_collections(self):
        return [{"id": "collection-1", "name": "Portraits"}]

    def create_asset_collection(self, name, payload):
        self.asset_collection_calls.append(("create", name, payload))
        return {"id": "collection-2", "name": name}

    def export_asset_collections(self):
        return {"format": "selfie-image-asset-collections", "collections": []}

    def import_asset_collections(self, payload, *, preview=False):
        self.asset_collection_calls.append(("import", payload, preview))
        return {"preview": preview, "imported": 0}

    def get_asset_collection(self, collection_id):
        if collection_id != "collection-1":
            raise ValueError("集合不存在")
        return {"id": collection_id, "name": "Portraits", "record_ids": ["record-1"]}

    def update_asset_collection(self, collection_id, payload):
        self.asset_collection_calls.append(("update", collection_id, payload))
        return {"id": collection_id, **payload}

    def delete_asset_collection(self, collection_id):
        self.asset_collection_calls.append(("delete", collection_id))
        return {"id": collection_id, "deleted": True}

    def update_asset_collection_records(self, collection_id, payload, *, remove=False):
        self.asset_collection_calls.append(("remove" if remove else "add", collection_id, payload))
        return {"id": collection_id, "record_ids": payload.get("record_ids", payload.get("ids", []))}

    def asset_collection_studio(self, collection_id, payload):
        self.asset_collection_calls.append(("studio", collection_id, payload))
        return {"session_id": "studio-1", "collection_id": collection_id}

    def studio_list(self, *, include_metadata=False):
        self.canvas_calls.append(("studio_list", include_metadata))
        return {"namespace": "studio", "include_metadata": include_metadata}

    def studio_create(self, payload):
        self.canvas_calls.append(("studio_create", payload))
        return {"id": "studio-1", "payload": payload}

    def studio_copy(self, session_id, payload):
        self.canvas_calls.append(("studio_copy", session_id, payload))
        return {"id": "studio-2", "source_session_id": session_id}

    def studio_get(self, session_id):
        if session_id == "missing":
            raise ValueError("画布不存在")
        self.canvas_calls.append(("studio_get", session_id))
        return {"id": session_id, "namespace": "studio"}

    def studio_update(self, session_id, payload):
        self.canvas_calls.append(("studio_update", session_id, payload))
        return {"id": session_id, "payload": payload}

    def studio_canvas(self, session_id):
        self.canvas_calls.append(("studio_canvas", session_id))
        return {"session_id": session_id, "nodes": []}

    def studio_canvas_update(self, session_id, payload):
        self.canvas_calls.append(("studio_canvas_update", session_id, payload))
        return {"session_id": session_id, "canvas": payload}

    def studio_canvas_node_create(self, session_id, payload):
        self.canvas_calls.append(("studio_canvas_node_create", session_id, payload))
        return {"session_id": session_id, "node": payload}

    def studio_canvas_node_delete(self, session_id, node_id):
        self.canvas_calls.append(("studio_canvas_node_delete", session_id, node_id))
        return {"session_id": session_id, "deleted_node_id": node_id}

    def studio_canvas_node_connect(self, session_id, node_id, payload):
        self.canvas_calls.append(("studio_canvas_node_connect", session_id, node_id, payload))
        return {"session_id": session_id, "node_id": node_id, "parent_id": payload.get("parent_id")}

    def studio_delete(self, session_id):
        self.canvas_calls.append(("studio_delete", session_id))
        return {"id": session_id, "deleted": True}

    def studio_set_slot(self, session_id, slot_id, payload):
        self.canvas_calls.append(("studio_set_slot", session_id, slot_id, payload))
        return {"session_id": session_id, "slot_id": slot_id}

    def studio_add_slot(self, session_id, payload):
        self.canvas_calls.append(("studio_add_slot", session_id, payload))
        return {"session_id": session_id, "slot": payload}

    def studio_reorder(self, session_id, payload):
        self.canvas_calls.append(("studio_reorder", session_id, payload))
        return {"session_id": session_id, "order": payload.get("order", [])}

    def studio_promote(self, session_id, payload):
        self.canvas_calls.append(("studio_promote", session_id, payload))
        return {"session_id": session_id, "result_id": payload.get("result_id")}

    def start_studio_run(self, session_id, payload):
        self.canvas_calls.append(("studio_run", session_id, payload))
        return {"task_id": "web-12345678-4", "api_key": "canvas-run-secret"}

    def creative_canvas_list(self, *, include_metadata=False):
        self.canvas_calls.append(("creative_canvas_list", include_metadata))
        return {"namespace": "creative", "include_metadata": include_metadata}

    def creative_canvas_create(self, payload):
        self.canvas_calls.append(("creative_canvas_create", payload))
        return {"id": "creative-1", "payload": payload}

    def creative_canvas_get(self, session_id):
        self.canvas_calls.append(("creative_canvas_get", session_id))
        return {"id": session_id, "namespace": "creative"}

    def creative_canvas_update(self, session_id, payload):
        self.canvas_calls.append(("creative_canvas_update", session_id, payload))
        return {"id": session_id, "payload": payload}

    def creative_canvas_delete(self, session_id):
        self.canvas_calls.append(("creative_canvas_delete", session_id))
        return {"id": session_id, "deleted": True}

    def creative_canvas_set_slot(self, session_id, slot_id, payload):
        self.canvas_calls.append(("creative_canvas_set_slot", session_id, slot_id, payload))
        return {"session_id": session_id, "slot_id": slot_id}

    def creative_canvas_add_slot(self, session_id, payload):
        self.canvas_calls.append(("creative_canvas_add_slot", session_id, payload))
        return {"session_id": session_id, "slot": payload}

    def creative_canvas_reorder(self, session_id, payload):
        self.canvas_calls.append(("creative_canvas_reorder", session_id, payload))
        return {"session_id": session_id, "order": payload.get("order", [])}

    def creative_canvas_promote(self, session_id, payload):
        self.canvas_calls.append(("creative_canvas_promote", session_id, payload))
        return {"session_id": session_id, "result_id": payload.get("result_id")}

    def start_creative_canvas_run(self, session_id, payload):
        self.canvas_calls.append(("creative_canvas_run", session_id, payload))
        return {"task_id": "web-12345678-5", "api_key": "creative-run-secret"}

    def studio_gallery_images(self, *, limit=24, offset=0):
        self.canvas_calls.append(("gallery", limit, offset))
        return {"items": [], "limit": limit, "offset": offset}

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
    ("/api/assets", "GET"), ("/api/assets/tags", "GET"), ("/api/assets/export", "GET"),
    ("/api/assets/import", "POST"), ("/api/assets/metadata", "POST"),
    ("/api/assets/delete", "POST"), ("/api/assets/studio", "POST"),
    ("/api/assets/record-1/studio", "POST"),
    ("/api/assets/collections", "GET"), ("/api/assets/collections", "POST"),
    ("/api/assets/collections/export", "GET"), ("/api/assets/collections/import", "POST"),
    ("/api/assets/collections/collection-1", "GET"),
    ("/api/assets/collections/collection-1", "POST"),
    ("/api/assets/collections/collection-1/delete", "POST"),
    ("/api/assets/collections/collection-1/add", "POST"),
    ("/api/assets/collections/collection-1/remove", "POST"),
    ("/api/assets/collections/collection-1/studio", "POST"),
    ("/api/studio/sessions", "GET"), ("/api/studio/sessions", "POST"),
    ("/api/studio/sessions/studio-1/copy", "POST"),
    ("/api/studio/sessions/studio-1", "GET"), ("/api/studio/sessions/studio-1", "POST"),
    ("/api/studio/sessions/studio-1/canvas", "GET"),
    ("/api/studio/sessions/studio-1/canvas", "POST"),
    ("/api/studio/sessions/studio-1/canvas/nodes", "POST"),
    ("/api/studio/sessions/studio-1/canvas/nodes/node-1", "DELETE"),
    ("/api/studio/sessions/studio-1/canvas/nodes/node-1", "POST"),
    ("/api/studio/sessions/studio-1/canvas/nodes/node-1/connect", "POST"),
    ("/api/studio/sessions/studio-1/delete", "POST"),
    ("/api/studio/sessions/studio-1/slots/slot-1", "POST"),
    ("/api/studio/sessions/studio-1/slots", "POST"),
    ("/api/studio/sessions/studio-1/reorder", "POST"),
    ("/api/studio/sessions/studio-1/promote", "POST"),
    ("/api/studio/sessions/studio-1/run", "POST"),
    ("/api/creative-canvas/sessions", "GET"),
    ("/api/creative-canvas/sessions", "POST"),
    ("/api/creative-canvas/sessions/creative-1", "GET"),
    ("/api/creative-canvas/sessions/creative-1", "POST"),
    ("/api/creative-canvas/sessions/creative-1/delete", "POST"),
    ("/api/creative-canvas/sessions/creative-1/slots/slot-1", "POST"),
    ("/api/creative-canvas/sessions/creative-1/slots", "POST"),
    ("/api/creative-canvas/sessions/creative-1/reorder", "POST"),
    ("/api/creative-canvas/sessions/creative-1/promote", "POST"),
    ("/api/creative-canvas/sessions/creative-1/run", "POST"),
    ("/api/studio/tasks/" + TASK_ID, "GET"),
    ("/api/studio/gallery", "GET"), ("/api/creative-canvas/gallery", "GET"),
    ("/api/tasks", "GET"), (f"/api/tasks/{TASK_ID}", "GET"),
    ("/api/tasks/export", "GET"), ("/api/tasks/delete", "POST"), ("/api/tasks/retry", "POST"),
    ("/api/records/consistency", "GET"), ("/api/records/consistency/repair", "POST"),
    ("/api/records/compare", "POST"), ("/api/records/clear", "POST"),
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


def test_standalone_auth_rate_limits_repeated_failures_without_storing_tokens(standalone):
    client, plugin = standalone
    for _ in range(7):
        response = client.get("/api/config", headers={"Authorization": "Bearer wrong-token"})
        assert response.status_code == 401
    blocked = client.get("/api/config", headers={"Authorization": "Bearer wrong-token"})
    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After") == "60"
    assert "wrong-token" not in json.dumps(plugin.__dict__, default=str)
    # A valid credential clears the source's failure window without exposing
    # the previously rejected token in any response body.
    recovered = client.get("/api/config", headers={"Authorization": f"Bearer {TOKEN}"})
    assert recovered.status_code == 200


def test_standalone_auth_block_outlives_failure_window_and_is_source_scoped(standalone, monkeypatch):
    from astrbot_plugin_selfie_image.webui import web as web_module

    client, _ = standalone
    clock = [0.0]
    monkeypatch.setattr(web_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    headers = {"Authorization": "Bearer wrong-token"}
    for _ in range(7):
        assert client.get("/api/config", headers=headers).status_code == 401
    clock[0] = 59.0
    assert client.get("/api/config", headers=headers).status_code == 429
    clock[0] = 61.0
    assert client.get("/api/config", headers=headers).status_code == 429
    assert client.get(
        "/api/config", headers=headers, environ_overrides={"REMOTE_ADDR": "192.0.2.2"},
    ).status_code == 401
    clock[0] = 119.0
    assert client.get("/api/config", headers=headers).status_code == 401


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


OBJECT_BODY_ROUTES = [
    "/api/config", "/api/config/import/preview", "/api/config/import",
    "/api/proxies/test", "/api/proxies/quality-check", "/api/creative/variations",
    "/api/tasks/delete", "/api/tasks/retry",
    "/api/records/consistency/repair", "/api/records/compare", "/api/records/clear",
    "/api/assets/import", "/api/assets/metadata", "/api/assets/delete",
    "/api/assets/studio", "/api/assets/record-1/studio",
    "/api/assets/collections", "/api/assets/collections/import",
    "/api/assets/collections/collection-1", "/api/assets/collections/collection-1/add",
    "/api/assets/collections/collection-1/remove", "/api/assets/collections/collection-1/studio",
    "/api/studio/sessions", "/api/studio/sessions/studio-1/copy",
    "/api/studio/sessions/studio-1", "/api/studio/sessions/studio-1/canvas",
    "/api/studio/sessions/studio-1/canvas/nodes",
    "/api/studio/sessions/studio-1/canvas/nodes/node-1/connect",
    "/api/studio/sessions/studio-1/delete", "/api/studio/sessions/studio-1/slots/slot-1",
    "/api/studio/sessions/studio-1/slots", "/api/studio/sessions/studio-1/reorder",
    "/api/studio/sessions/studio-1/promote", "/api/studio/sessions/studio-1/run",
    "/api/creative-canvas/sessions", "/api/creative-canvas/sessions/creative-1",
    "/api/creative-canvas/sessions/creative-1/delete",
    "/api/creative-canvas/sessions/creative-1/slots/slot-1",
    "/api/creative-canvas/sessions/creative-1/slots",
    "/api/creative-canvas/sessions/creative-1/reorder",
    "/api/creative-canvas/sessions/creative-1/promote",
    "/api/creative-canvas/sessions/creative-1/run",
]


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


def test_task_detail_falls_back_to_status_hook_when_detail_hook_is_unavailable(http, monkeypatch):
    monkeypatch.setattr(http.plugin, "get_web_task_detail", None, raising=False)
    data = assert_success(http.call(f"/api/tasks/{TASK_ID}"))
    assert data["task_id"] == TASK_ID


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


@pytest.mark.parametrize("query,expected", [
    ("", None),
    ("ids=,,,", []),
    (f"ids={TASK_ID},{TASK_ID}", [TASK_ID, TASK_ID]),
    ("ids=" + ",".join([TASK_ID] * 200), [TASK_ID] * 200),
])
def test_task_export_preserves_legacy_empty_duplicate_and_count_contracts(http, monkeypatch, query, expected):
    seen = []

    def export(ids):
        seen.append(ids)
        return {"count": len(ids) if ids is not None else 0}

    monkeypatch.setattr(http.plugin, "export_web_tasks", export)
    assert_success(http.call("/api/tasks/export?" + query))
    assert seen == [expected]


def test_task_export_rejects_more_than_two_hundred_ids_without_calling_plugin(http, monkeypatch):
    def unexpected(_ids):
        pytest.fail("oversized export reached task hook")

    monkeypatch.setattr(http.plugin, "export_web_tasks", unexpected)
    query = "ids=" + ",".join([TASK_ID] * 201)
    assert_failure(http.call("/api/tasks/export?" + query), 400)


@pytest.mark.parametrize("operation", ["export", "delete", "retry"])
def test_task_operation_results_are_recursively_redacted(http, monkeypatch, operation):
    secret = "task-operation-secret"

    def result(*_args):
        return {"nested": [{"api_key": secret}, {"safe": "visible"}]}

    if operation == "export":
        monkeypatch.setattr(http.plugin, "export_web_tasks", result)
        response = http.call("/api/tasks/export")
    else:
        monkeypatch.setattr(http.plugin, f"{operation}_web_tasks", result)
        response = http.call(f"/api/tasks/{operation}", method="POST", body={"ids": [TASK_ID]})
    data = assert_success(response)
    assert data["nested"][1]["safe"] == "visible"
    assert secret not in json.dumps(response.payload)


@pytest.mark.parametrize("operation", ["export", "delete", "retry"])
def test_task_hook_web_contract_errors_keep_operation_error_mapping(http, monkeypatch, operation):
    def fail(*_args):
        raise WebContractError("api_key=hook-error-secret", 409)

    method = {"export": "export_web_tasks", "delete": "delete_web_tasks", "retry": "retry_web_tasks"}[operation]
    monkeypatch.setattr(http.plugin, method, fail)
    if operation == "export":
        response = http.call("/api/tasks/export")
    else:
        response = http.call(f"/api/tasks/{operation}", method="POST", body={"ids": [TASK_ID]})
    assert_failure(response, 400)
    assert "hook-error-secret" not in json.dumps(response.payload)


def test_task_cancel_redacts_plugin_message(http, monkeypatch):
    secret = "cancel-message-secret"

    def cancel(task_id, *, is_admin=False):
        assert is_admin is True
        http.plugin._web_tasks[task_id]["status"] = "cancelled"
        return f"api_key={secret}"

    monkeypatch.setattr(http.plugin, "cancel_image_task", cancel)
    response = http.call(f"/api/tasks/{TASK_ID}/cancel", method="POST")
    assert_success(response)
    assert secret not in json.dumps(response.payload)


def test_shared_task_route_contract_matches_both_live_registries(tmp_path):
    plugin = ContractPlugin(tmp_path)
    flask_app = FlaskWebServer(plugin)._create_app()
    expected_flask = {
        (route.flask_path, method, route.endpoint)
        for route in TASK_ROUTE_CONTRACTS
        for method in route.methods
    }
    actual_flask = {
        (rule.rule, method, rule.endpoint)
        for rule in flask_app.url_map.iter_rules()
        for method in rule.methods - {"HEAD", "OPTIONS"}
        if rule.rule in {route.flask_path for route in TASK_ROUTE_CONTRACTS}
    }
    assert actual_flask == expected_flask

    registered = []
    plugin.context = SimpleNamespace(register_web_api=lambda *args: registered.append(args))
    api = dashboard_module.SelfieImageDashboardAPI(plugin)
    api.register()
    expected_paths = {
        f"/{PLUGIN_NAME}/{prefix}{route.dashboard_path}"
        for route in TASK_ROUTE_CONTRACTS
        for prefix in ("", "page/")
    }
    expected_dashboard = {
        (f"/{PLUGIN_NAME}/{prefix}{route.dashboard_path}", method, getattr(api, route.dashboard_handler))
        for route in TASK_ROUTE_CONTRACTS
        for prefix in ("", "page/")
        for method in route.methods
    }
    actual_dashboard = {
        (path, method, handler)
        for path, handler, methods, _description in registered
        for method in methods
        if path in expected_paths
    }
    assert actual_dashboard == expected_dashboard


def test_shared_config_route_contract_matches_both_live_registries(tmp_path):
    plugin = ContractPlugin(tmp_path)
    flask_app = FlaskWebServer(plugin)._create_app()
    expected_flask = {
        (route.flask_path, method, route.endpoint)
        for route in CONFIG_ROUTE_CONTRACTS
        for method in route.methods
    }
    actual_flask = {
        (rule.rule, method, rule.endpoint)
        for rule in flask_app.url_map.iter_rules()
        for method in rule.methods - {"HEAD", "OPTIONS"}
        if rule.rule in {route.flask_path for route in CONFIG_ROUTE_CONTRACTS}
    }
    assert actual_flask == expected_flask

    registered = []
    plugin.context = SimpleNamespace(register_web_api=lambda *args: registered.append(args))
    api = dashboard_module.SelfieImageDashboardAPI(plugin)
    api.register()
    expected_paths = {
        f"/{PLUGIN_NAME}/{prefix}{route.dashboard_path}"
        for route in CONFIG_ROUTE_CONTRACTS
        for prefix in ("", "page/")
    }
    expected_dashboard = {
        (
            f"/{PLUGIN_NAME}/{prefix}{route.dashboard_path}",
            method,
            getattr(api, route.dashboard_handler),
        )
        for route in CONFIG_ROUTE_CONTRACTS
        for prefix in ("", "page/")
        for method in route.methods
    }
    actual_dashboard = {
        (path, method, handler)
        for path, handler, methods, _description in registered
        for method in methods
        if path in expected_paths
    }
    assert actual_dashboard == expected_dashboard


def test_shared_proxy_route_contract_matches_both_live_registries(tmp_path):
    plugin = ContractPlugin(tmp_path)
    flask_app = FlaskWebServer(plugin)._create_app()
    expected_flask = {
        (route.flask_path, method, route.endpoint)
        for route in PROXY_ROUTE_CONTRACTS
        for method in route.methods
    }
    actual_flask = {
        (rule.rule, method, rule.endpoint)
        for rule in flask_app.url_map.iter_rules()
        for method in rule.methods - {"HEAD", "OPTIONS"}
        if rule.rule in {route.flask_path for route in PROXY_ROUTE_CONTRACTS}
    }
    assert actual_flask == expected_flask

    registered = []
    plugin.context = SimpleNamespace(register_web_api=lambda *args: registered.append(args))
    api = dashboard_module.SelfieImageDashboardAPI(plugin)
    api.register()
    expected_paths = {
        f"/{PLUGIN_NAME}/{prefix}{route.dashboard_path}"
        for route in PROXY_ROUTE_CONTRACTS
        for prefix in ("", "page/")
    }
    expected_dashboard = {
        (
            f"/{PLUGIN_NAME}/{prefix}{route.dashboard_path}",
            method,
            getattr(api, route.dashboard_handler),
        )
        for route in PROXY_ROUTE_CONTRACTS
        for prefix in ("", "page/")
        for method in route.methods
    }
    actual_dashboard = {
        (path, method, handler)
        for path, handler, methods, _description in registered
        for method in methods
        if path in expected_paths
    }
    assert actual_dashboard == expected_dashboard


def test_shared_asset_route_contract_matches_both_live_registries(tmp_path):
    plugin = ContractPlugin(tmp_path)
    flask_app = FlaskWebServer(plugin)._create_app()
    expected_flask = {
        (route.flask_path, method, route.endpoint)
        for route in ASSET_ROUTE_CONTRACTS
        for method in route.methods
    }
    actual_flask = {
        (rule.rule, method, rule.endpoint)
        for rule in flask_app.url_map.iter_rules()
        for method in rule.methods - {"HEAD", "OPTIONS"}
        if rule.rule in {route.flask_path for route in ASSET_ROUTE_CONTRACTS}
    }
    assert actual_flask == expected_flask

    registered = []
    plugin.context = SimpleNamespace(register_web_api=lambda *args: registered.append(args))
    api = dashboard_module.SelfieImageDashboardAPI(plugin)
    api.register()
    expected_paths = {
        f"/{PLUGIN_NAME}/{prefix}{route.dashboard_path}"
        for route in ASSET_ROUTE_CONTRACTS
        for prefix in ("", "page/")
    }
    expected_dashboard = {
        (
            f"/{PLUGIN_NAME}/{prefix}{route.dashboard_path}",
            method,
            getattr(api, route.dashboard_handler),
        )
        for route in ASSET_ROUTE_CONTRACTS
        for prefix in ("", "page/")
        for method in route.methods
    }
    actual_dashboard = {
        (path, method, handler)
        for path, handler, methods, _description in registered
        for method in methods
        if path in expected_paths
    }
    assert actual_dashboard == expected_dashboard


def test_shared_asset_collection_route_contract_matches_both_live_registries(tmp_path):
    plugin = ContractPlugin(tmp_path)
    flask_app = FlaskWebServer(plugin)._create_app()
    expected_flask = {
        (route.flask_path, method, route.endpoint)
        for route in ASSET_COLLECTION_ROUTE_CONTRACTS
        for method in route.methods
    }
    actual_flask = {
        (rule.rule, method, rule.endpoint)
        for rule in flask_app.url_map.iter_rules()
        for method in rule.methods - {"HEAD", "OPTIONS"}
        if rule.rule in {route.flask_path for route in ASSET_COLLECTION_ROUTE_CONTRACTS}
    }
    assert actual_flask == expected_flask

    registered = []
    plugin.context = SimpleNamespace(register_web_api=lambda *args: registered.append(args))
    api = dashboard_module.SelfieImageDashboardAPI(plugin)
    api.register()
    expected_paths = {
        f"/{PLUGIN_NAME}/{prefix}{route.dashboard_path}"
        for route in ASSET_COLLECTION_ROUTE_CONTRACTS
        for prefix in ("", "page/")
    }
    expected_dashboard = {
        (
            f"/{PLUGIN_NAME}/{prefix}{route.dashboard_path}",
            method,
            getattr(api, route.dashboard_handler),
        )
        for route in ASSET_COLLECTION_ROUTE_CONTRACTS
        for prefix in ("", "page/")
        for method in route.methods
    }
    actual_dashboard = {
        (path, method, handler)
        for path, handler, methods, _description in registered
        for method in methods
        if path in expected_paths
    }
    assert actual_dashboard == expected_dashboard


def test_asset_collection_controller_operations_preserve_both_adapter_contracts(http):
    collections = assert_success(http.call("/api/assets/collections"))
    assert collections[0]["id"] == "collection-1"

    created = http.call(
        "/api/assets/collections", method="POST", body={"name": "New", "note": "test"}
    )
    assert_success(created)
    assert created.envelope["message"] == "资产集合已创建"

    exported = assert_success(http.call("/api/assets/collections/export"))
    assert exported["format"] == "selfie-image-asset-collections"

    preview = http.call(
        "/api/assets/collections/import", method="POST",
        body={"mode": "preview", "collections": []},
    )
    assert_success(preview)
    assert preview.envelope["message"] == "导入预览完成"
    imported = http.call(
        "/api/assets/collections/import", method="POST",
        body={"collections": []},
    )
    assert_success(imported)
    assert imported.envelope["message"] == "资产集合已导入"
    assert http.plugin.asset_collection_calls[:3] == [
        ("create", "New", {"name": "New", "note": "test"}),
        ("import", {"mode": "preview", "collections": []}, True),
        ("import", {"collections": []}, False),
    ]

    detail = assert_success(http.call("/api/assets/collections/collection-1"))
    assert detail["record_ids"] == ["record-1"]
    assert_failure(http.call("/api/assets/collections/missing"), 404)

    updated = http.call(
        "/api/assets/collections/collection-1", method="POST", body={"name": "Updated"}
    )
    assert_success(updated)
    assert updated.envelope["message"] == "资产集合已更新"
    added = http.call(
        "/api/assets/collections/collection-1/add", method="POST",
        body={"record_ids": ["record-1"]},
    )
    assert_success(added)
    assert added.envelope["message"] == "集合内容已更新"
    removed = http.call(
        "/api/assets/collections/collection-1/remove", method="POST",
        body={"ids": ["record-1"]},
    )
    assert_success(removed)
    assert http.plugin.asset_collection_calls[-2:] == [
        ("add", "collection-1", {"record_ids": ["record-1"]}),
        ("remove", "collection-1", {"ids": ["record-1"]}),
    ]
    sent = http.call(
        "/api/assets/collections/collection-1/studio", method="POST", body={"mode": "append"}
    )
    assert_success(sent)
    assert sent.envelope["message"] == "集合已加入画布"
    deleted = http.call(
        "/api/assets/collections/collection-1/delete", method="POST"
    )
    assert_success(deleted)
    assert deleted.envelope["message"] == "资产集合已删除"


@pytest.mark.parametrize("hook,path,method,body,status", [
    ("list_asset_collections", "/api/assets/collections", "GET", MISSING_BODY, 400),
    ("export_asset_collections", "/api/assets/collections/export", "GET", MISSING_BODY, 500),
    ("import_asset_collections", "/api/assets/collections/import", "POST", {}, 400),
    ("update_asset_collection", "/api/assets/collections/collection-1", "POST", {}, 400),
    ("delete_asset_collection", "/api/assets/collections/collection-1/delete", "POST", MISSING_BODY, 400),
    ("asset_collection_studio", "/api/assets/collections/collection-1/studio", "POST", {}, 400),
])
def test_asset_collection_controller_preserves_operation_error_statuses(
    http, monkeypatch, hook, path, method, body, status,
):
    def fail(*_args, **_kwargs):
        raise RuntimeError("collection hook failed")

    monkeypatch.setattr(http.plugin, hook, fail)
    assert_failure(http.call(path, method=method, body=body), status)


def test_shared_canvas_route_contract_matches_both_live_registries(tmp_path):
    plugin = ContractPlugin(tmp_path)
    flask_app = FlaskWebServer(plugin)._create_app()
    expected_flask = {
        (route.flask_path, method, route.endpoint)
        for route in CANVAS_ROUTE_CONTRACTS
        for method in route.methods
    }
    actual_flask = {
        (rule.rule, method, rule.endpoint)
        for rule in flask_app.url_map.iter_rules()
        for method in rule.methods - {"HEAD", "OPTIONS"}
        if rule.rule in {route.flask_path for route in CANVAS_ROUTE_CONTRACTS}
    }
    assert actual_flask == expected_flask

    registered = []
    plugin.context = SimpleNamespace(register_web_api=lambda *args: registered.append(args))
    api = dashboard_module.SelfieImageDashboardAPI(plugin)
    api.register()
    expected_paths = {
        f"/{PLUGIN_NAME}/{prefix}{route.dashboard_path}"
        for route in CANVAS_ROUTE_CONTRACTS
        for prefix in ("", "page/")
    }
    expected_dashboard = {
        (
            f"/{PLUGIN_NAME}/{prefix}{route.dashboard_path}",
            method,
            getattr(api, route.dashboard_handler),
        )
        for route in CANVAS_ROUTE_CONTRACTS
        for prefix in ("", "page/")
        for method in route.methods
    }
    actual_dashboard = {
        (path, method, handler)
        for path, handler, methods, _description in registered
        for method in methods
        if path in expected_paths
    }
    assert actual_dashboard == expected_dashboard


def test_canvas_controller_preserves_studio_and_creation_canvas_operations(http):
    studio_list = assert_success(http.call("/api/studio/sessions?metadata=true"))
    assert studio_list == {
        "namespace": "studio",
        "include_metadata": http.adapter == "flask",
    }
    created = assert_success(http.call(
        "/api/studio/sessions", method="POST", body={"title": "Studio"}
    ))
    assert created["id"] == "studio-1"
    copied = http.call(
        "/api/studio/sessions/studio-1/copy", method="POST", body={"title": "Copy"}
    )
    assert_success(copied)
    assert copied.envelope["message"] == "画布会话已复制"
    assert assert_success(http.call("/api/studio/sessions/studio-1"))["namespace"] == "studio"
    updated = assert_success(http.call(
        "/api/studio/sessions/studio-1", method="POST", body={"title": "Updated"}
    ))
    assert updated["payload"] == {"title": "Updated"}
    assert assert_success(http.call("/api/studio/sessions/studio-1/canvas"))["nodes"] == []
    canvas = assert_success(http.call(
        "/api/studio/sessions/studio-1/canvas", method="POST", body={"nodes": []}
    ))
    assert canvas["canvas"] == {"nodes": []}
    node = assert_success(http.call(
        "/api/studio/sessions/studio-1/canvas/nodes", method="POST", body={"type": "image"}
    ))
    assert node["node"] == {"type": "image"}
    assert_success(http.call(
        "/api/studio/sessions/studio-1/canvas/nodes/node-1", method="DELETE"
    ))
    assert_success(http.call(
        "/api/studio/sessions/studio-1/canvas/nodes/node-1", method="POST"
    ))
    connected = assert_success(http.call(
        "/api/studio/sessions/studio-1/canvas/nodes/node-1/connect",
        method="POST", body={"parent_id": "node-0"},
    ))
    assert connected["parent_id"] == "node-0"
    assert_success(http.call("/api/studio/sessions/studio-1/delete", method="POST"))
    assert_success(http.call(
        "/api/studio/sessions/studio-1/slots/slot-1", method="POST", body={"clear": True}
    ))
    assert_success(http.call(
        "/api/studio/sessions/studio-1/slots", method="POST", body={"role": "extra"}
    ))
    assert_success(http.call(
        "/api/studio/sessions/studio-1/reorder", method="POST", body={"order": ["slot-1"]}
    ))
    assert_success(http.call(
        "/api/studio/sessions/studio-1/promote", method="POST", body={"result_id": "r1"}
    ))
    run = http.call(
        "/api/studio/sessions/studio-1/run", method="POST", body={"target_node_id": "node-1"}
    )
    assert assert_success(run)["task_id"] == "web-12345678-4"
    assert "canvas-run-secret" not in json.dumps(run.payload)

    creative_list = assert_success(http.call("/api/creative-canvas/sessions?metadata=true"))
    assert creative_list == {
        "namespace": "creative",
        "include_metadata": http.adapter == "flask",
    }
    assert assert_success(http.call(
        "/api/creative-canvas/sessions", method="POST", body={"title": "Creative"}
    ))["id"] == "creative-1"
    assert assert_success(http.call(
        "/api/creative-canvas/sessions/creative-1"
    ))["namespace"] == "creative"
    assert_success(http.call(
        "/api/creative-canvas/sessions/creative-1", method="POST", body={"title": "Updated"}
    ))
    assert_success(http.call(
        "/api/creative-canvas/sessions/creative-1/delete", method="POST"
    ))
    assert_success(http.call(
        "/api/creative-canvas/sessions/creative-1/slots/slot-1", method="POST", body={"clear": True}
    ))
    assert_success(http.call(
        "/api/creative-canvas/sessions/creative-1/slots", method="POST", body={"role": "extra"}
    ))
    assert_success(http.call(
        "/api/creative-canvas/sessions/creative-1/reorder", method="POST", body={"order": ["slot-1"]}
    ))
    assert_success(http.call(
        "/api/creative-canvas/sessions/creative-1/promote", method="POST", body={"result_id": "r2"}
    ))
    creative_run = http.call(
        "/api/creative-canvas/sessions/creative-1/run", method="POST", body={}
    )
    assert assert_success(creative_run)["task_id"] == "web-12345678-5"
    assert "creative-run-secret" not in json.dumps(creative_run.payload)
    task = assert_success(http.call(f"/api/studio/tasks/{TASK_ID}"))
    assert task["task_id"] == TASK_ID
    gallery = assert_success(http.call("/api/studio/gallery?limit=12&offset=4"))
    assert gallery["limit"] == 12 and gallery["offset"] == 4
    creative_gallery = assert_success(http.call("/api/creative-canvas/gallery"))
    assert creative_gallery["limit"] == 24 and creative_gallery["offset"] == 0
    assert http.plugin.canvas_calls[:4] == [
        ("studio_list", http.adapter == "flask"),
        ("studio_create", {"title": "Studio"}),
        ("studio_copy", "studio-1", {"title": "Copy"}),
        ("studio_get", "studio-1"),
    ]


@pytest.mark.parametrize("hook,path,method,body,status", [
    ("studio_get", "/api/studio/sessions/missing", "GET", MISSING_BODY, 404),
    ("studio_canvas_node_delete", "/api/studio/sessions/s1/canvas/nodes/n1", "DELETE", MISSING_BODY, 404),
    ("studio_update", "/api/studio/sessions/s1", "POST", {}, 400),
    ("studio_copy", "/api/studio/sessions/s1/copy", "POST", {}, 409),
    ("start_studio_run", "/api/studio/sessions/s1/run", "POST", {}, 500),
    ("creative_canvas_list", "/api/creative-canvas/sessions", "GET", MISSING_BODY, 500),
    ("studio_gallery_images", "/api/studio/gallery", "GET", MISSING_BODY, 400),
])
def test_canvas_controller_preserves_operation_error_statuses(
    http, monkeypatch, hook, path, method, body, status,
):
    def fail(*_args, **_kwargs):
        raise ValueError("canvas hook failed")

    monkeypatch.setattr(http.plugin, hook, fail)
    assert_failure(http.call(path, method=method, body=body), status)


def test_shared_record_route_contract_matches_both_live_registries(tmp_path):
    plugin = ContractPlugin(tmp_path)
    flask_app = FlaskWebServer(plugin)._create_app()
    expected_flask = {
        (route.flask_path, method, route.endpoint)
        for route in RECORD_ROUTE_CONTRACTS
        for method in route.methods
    }
    actual_flask = {
        (rule.rule, method, rule.endpoint)
        for rule in flask_app.url_map.iter_rules()
        for method in rule.methods - {"HEAD", "OPTIONS"}
        if rule.rule in {route.flask_path for route in RECORD_ROUTE_CONTRACTS}
    }
    assert actual_flask == expected_flask

    registered = []
    plugin.context = SimpleNamespace(register_web_api=lambda *args: registered.append(args))
    api = dashboard_module.SelfieImageDashboardAPI(plugin)
    api.register()
    expected_paths = {
        f"/{PLUGIN_NAME}/{prefix}{route.dashboard_path}"
        for route in RECORD_ROUTE_CONTRACTS
        for prefix in ("", "page/")
    }
    expected_dashboard = {
        (
            f"/{PLUGIN_NAME}/{prefix}{route.dashboard_path}",
            method,
            getattr(api, route.dashboard_handler),
        )
        for route in RECORD_ROUTE_CONTRACTS
        for prefix in ("", "page/")
        for method in route.methods
    }
    actual_dashboard = {
        (path, method, handler)
        for path, handler, methods, _description in registered
        for method in methods
        if path in expected_paths
    }
    assert actual_dashboard == expected_dashboard


def test_record_controller_operations_preserve_both_adapter_contracts(http):
    metrics = assert_success(http.call("/api/metrics?window=24h"))
    assert metrics["window_seconds"] == 86_400
    assert "metrics-secret" not in json.dumps(metrics)
    assert_failure(http.call("/api/metrics?window=unknown"), 400)

    detail = assert_success(http.call("/api/records/record-1"))
    assert detail["id"] == "record-1"
    assert "record-secret" not in json.dumps(detail)
    assert_failure(http.call(f"/api/records/{'x' * 129}"), 400)
    assert_failure(http.call("/api/records/missing"), 404)

    sources = assert_success(http.call("/api/records/record-1/media-sources"))
    assert sources["generated_image_sources"] == [
        "https://images.example.test/result.png",
    ]
    metadata = assert_success(http.call(
        "/api/records/record-1/metadata", method="POST", body={"favorite": True}
    ))
    assert metadata["favorite"] is True
    assert http.plugin.record_metadata_updates == [("record-1", {"favorite": True})]

    reuse = assert_success(http.call("/api/records/record-1/reuse"))
    assert reuse["record_id"] == "record-1"
    assert "reuse-secret" not in json.dumps(reuse)
    retry = assert_success(http.call(
        "/api/records/record-1/retry", method="POST", body={"feedback": "retry"}
    ))
    assert retry["feedback"] == "retry"
    assert "retry-secret" not in json.dumps(retry)


def test_record_controller_remaining_operations_preserve_both_adapter_contracts(http):
    report = assert_success(http.call("/api/records/consistency"))
    assert report["issues"] == []
    assert "consistency-secret" not in json.dumps(report)

    preview = http.call(
        "/api/records/consistency/repair", method="POST",
        body={"kinds": ["orphan_cache"]},
    )
    assert preview.status == 200
    assert preview.envelope["message"] == "已生成修复预览"
    assert preview.envelope["data"]["confirmed"] is False
    assert "repair-secret" not in json.dumps(preview.payload)

    confirmed = http.call(
        "/api/records/consistency/repair", method="POST",
        body={"confirm": "yes", "kinds": "orphan_sidecar"},
    )
    assert confirmed.status == 200
    assert confirmed.envelope["message"] == "一致性修复完成"
    assert http.plugin.storage_repair_calls == [
        (False, ["orphan_cache"]), (True, "orphan_sidecar"),
    ]

    compared = assert_success(http.call(
        "/api/records/compare", method="POST",
        body={"record_ids": ["record-1", "record-2"], "limit": "3"},
    ))
    assert compared == {"count": 2, "ids": ["record-1", "record-2"], "limit": 3}
    assert http.plugin.record_compare_calls == [(["record-1", "record-2"], 3)]
    assert_failure(http.call("/api/records/compare", method="POST", body={"ids": "record-1"}), 400, "ids 必须是数组")
    assert_failure(http.call("/api/records/compare", method="POST", body={"ids": [], "limit": "invalid"}), 400)

    cleared = assert_success(http.call("/api/records/clear", method="POST", body={}))
    assert cleared == {"deleted": 4}
    assert http.plugin.record_clear_calls == 1


def test_asset_controller_operations_preserve_both_adapter_contracts(http):
    assets = assert_success(http.call(
        "/api/assets?favorite=true&pinned=false&tag=portrait&source=web&model=vision-a"
        "&media_type=image&success=true&q=sample&start_time=2026-11-01"
        "&end_time=2026-11-14&offset=24&limit=12&sort=priority"
    ))
    assert assets[0]["id"] == "asset-1"
    assert http.plugin.asset_query_calls == [{
        "favorite": True, "pinned": False, "tag": "portrait", "source": "web",
        "model": "vision-a", "media_type": "image", "success": True,
        "keyword": "sample", "start_time": "2026-11-01", "end_time": "2026-11-14",
        "offset": 24, "limit": 12, "sort": "priority",
    }]
    assert_failure(http.call("/api/assets?media_type=audio"), 400, "仅支持图片")
    assert_failure(http.call("/api/assets?success=maybe"), 400, "success 必须")
    assert_failure(http.call("/api/assets?offset=10001"), 400, "offset 不能大于")
    assert_failure(http.call("/api/assets?limit=101"), 400, "limit 不能大于")

    http.plugin.query_asset_records = None
    legacy = assert_success(http.call("/api/assets?favorite=true&tag=legacy"))
    assert legacy[0]["id"] == "legacy-asset"
    assert legacy[0]["filters"] == {"favorite": True, "pinned": None, "tag": "legacy"}

    tags = assert_success(http.call("/api/assets/tags"))
    assert tags == [{"tag": "portrait", "count": 2}]
    exported = assert_success(http.call("/api/assets/export?ids=asset-1,asset-2"))
    assert exported == {"ids": ["asset-1", "asset-2"], "count": 2}
    assert_failure(http.call("/api/assets/export?ids=" + ",".join(["asset-1"] * 1001)), 400, "最多导出")

    imported = http.call(
        "/api/assets/import", method="POST", body={"records": [{"id": "asset-1"}]}
    )
    assert_success(imported)
    assert imported.envelope["message"] == "资产元数据已导入"
    metadata = http.call(
        "/api/assets/metadata", method="POST",
        body={"record_ids": ["asset-1"], "favorite": True},
    )
    assert_success(metadata)
    assert metadata.envelope["message"] == "批量资产标记已保存"
    assert http.plugin.asset_metadata_updates == [
        (["asset-1"], {"record_ids": ["asset-1"], "favorite": True}),
    ]

    deleted = http.call(
        "/api/assets/delete", method="POST", body={"ids": ["asset-1"]}
    )
    assert_success(deleted)
    assert deleted.envelope["message"] == "资产已删除"
    single_studio = http.call(
        "/api/assets/asset-1/studio", method="POST", body={"session_id": "studio-1"}
    )
    assert_success(single_studio)
    batch_studio = http.call(
        "/api/assets/studio", method="POST",
        body={"record_ids": ["asset-1", "asset-2"], "session_id": "studio-1"},
    )
    assert_success(batch_studio)
    assert [call[0] for call in http.plugin.asset_studio_calls] == [
        ["asset-1"], ["asset-1", "asset-2"],
    ]


@pytest.mark.parametrize("method_name,path,method,body", [
    ("list_asset_tags", "/api/assets/tags", "GET", MISSING_BODY),
    ("export_asset_metadata", "/api/assets/export", "GET", MISSING_BODY),
    ("import_asset_metadata", "/api/assets/import", "POST", {}),
])
def test_asset_controller_reports_unsupported_capabilities(http, monkeypatch, method_name, path, method, body):
    monkeypatch.setattr(http.plugin, method_name, None)
    assert_failure(http.call(path, method=method, body=body), 501)


@pytest.mark.parametrize("method_name,path,method,body", [
    ("inspect_storage_consistency", "/api/records/consistency", "GET", MISSING_BODY),
    ("repair_storage_consistency", "/api/records/consistency/repair", "POST", {}),
    ("compare_records_for_web", "/api/records/compare", "POST", {"ids": ["a", "b"]}),
    ("clear_recent_records", "/api/records/clear", "POST", {}),
])
def test_remaining_record_operations_report_unsupported_capabilities(http, monkeypatch, method_name, path, method, body):
    monkeypatch.setattr(http.plugin, method_name, None)
    assert_failure(http.call(path, method=method, body=body), 501)


def test_record_consistency_failure_statuses_match_across_adapters(http, monkeypatch):
    def fail(_self, **_kwargs):
        raise RuntimeError("repair failed")

    monkeypatch.setattr(http.plugin, "repair_storage_consistency", fail.__get__(http.plugin))
    assert_failure(http.call("/api/records/consistency/repair", method="POST", body={}), 400, "repair failed")
    assert_failure(http.call("/api/records/consistency/repair", method="POST", body={"confirm": True}), 409, "repair failed")


def test_record_retry_keeps_unsupported_capability_contract(http):
    http.plugin.start_record_retry_task = None
    assert_failure(http.call(
        "/api/records/record-1/retry", method="POST", body={}
    ), 501)


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
