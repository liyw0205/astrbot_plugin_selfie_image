"""AstrBot Dashboard embedded page APIs for Selfie Image.
Dashboard plugin-page APIs registered via ``context.register_web_api``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import inspect
from typing import Any, Optional

from ..core.constants import PLUGIN_NAME
from ..core.utils import (
    redact_sensitive_data,
    redact_sensitive_text,
)
from .web import (
    MAX_CACHE_IMAGE_PATH_LENGTH,
    MAX_TASK_PAGE_LIMIT,
    MAX_TASK_OFFSET,
    dashboard_page_source_status,
)
from .contracts import (
    ASSET_COLLECTION_ROUTE_CONTRACTS,
    ASSET_ROUTE_CONTRACTS,
    CANVAS_ROUTE_CONTRACTS,
    CONFIG_ROUTE_CONTRACTS,
    DASHBOARD_ROUTE_PREFIXES,
    MAX_WEB_TASK_ID_LENGTH,
    PROMPT_CATALOG_ROUTE_CONTRACTS,
    PROXY_ROUTE_CONTRACTS,
    RECORD_ROUTE_CONTRACTS,
    TASK_ROUTE_CONTRACTS,
    WEB_TASK_ID_RE,
)
from .controllers import (
    AssetCollectionController,
    AssetController,
    AssetControllerResult,
    CanvasController,
    CanvasControllerResult,
    ConfigController,
    ConfigControllerResult,
    ProxyController,
    ProxyControllerResult,
    RecordController,
    RecordControllerResult,
    TaskController,
    TaskControllerResult,
)
from .prompt_controller import PromptCatalogController, PromptCatalogControllerResult
from .services import (
    WebContractError,
    build_health_payload,
    gallery_pagination,
    normalize_task_ids,
    parse_bounded_int,
    parse_timestamp_query,
    render_image_thumbnail,
)

try:
    from astrbot.api.web import error_response, file_response, json_response, request
except Exception:  # pragma: no cover - unit tests / offline import
    error_response = None  # type: ignore
    file_response = None  # type: ignore
    json_response = None  # type: ignore
    request = None  # type: ignore


PAGE_PREVIEW_MAX_BYTES = 64 * 1024 * 1024


class SelfieImageDashboardAPI:
    """Register and handle Dashboard plugin-page APIs."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._asset_collection_controller = AssetCollectionController(plugin)
        self._asset_controller = AssetController(plugin)
        self._canvas_controller = CanvasController(plugin)
        self._config_controller = ConfigController(plugin)
        self._proxy_controller = ProxyController(plugin)
        self._task_controller = TaskController(plugin)
        self._record_controller = RecordController(plugin)
        self._prompt_catalog_controller = PromptCatalogController(plugin)

    def _config_route_entries(self) -> list[tuple[str, Any, list[str], str]]:
        return [
            (
                route.dashboard_path,
                getattr(self, route.dashboard_handler),
                list(route.methods),
                route.description,
            )
            for route in CONFIG_ROUTE_CONTRACTS
        ]

    def _task_route_entries(self) -> list[tuple[str, Any, list[str], str]]:
        return [
            (
                route.dashboard_path,
                getattr(self, route.dashboard_handler),
                list(route.methods),
                route.description,
            )
            for route in TASK_ROUTE_CONTRACTS
        ]

    def _record_route_entries(self) -> list[tuple[str, Any, list[str], str]]:
        return [
            (
                route.dashboard_path,
                getattr(self, route.dashboard_handler),
                list(route.methods),
                route.description,
            )
            for route in RECORD_ROUTE_CONTRACTS
        ]

    def _proxy_route_entries(self) -> list[tuple[str, Any, list[str], str]]:
        return [
            (
                route.dashboard_path,
                getattr(self, route.dashboard_handler),
                list(route.methods),
                route.description,
            )
            for route in PROXY_ROUTE_CONTRACTS
        ]

    def _asset_route_entries(self) -> list[tuple[str, Any, list[str], str]]:
        return [
            (
                route.dashboard_path,
                getattr(self, route.dashboard_handler),
                list(route.methods),
                route.description,
            )
            for route in ASSET_ROUTE_CONTRACTS
        ]

    def _asset_collection_route_entries(self) -> list[tuple[str, Any, list[str], str]]:
        return [
            (
                route.dashboard_path,
                getattr(self, route.dashboard_handler),
                list(route.methods),
                route.description,
            )
            for route in ASSET_COLLECTION_ROUTE_CONTRACTS
        ]

    def _canvas_route_entries(self) -> list[tuple[str, Any, list[str], str]]:
        return [
            (
                route.dashboard_path,
                getattr(self, route.dashboard_handler),
                list(route.methods),
                route.description,
            )
            for route in CANVAS_ROUTE_CONTRACTS
        ]

    def _prompt_catalog_route_entries(self) -> list[tuple[str, Any, list[str], str]]:
        return [
            (
                route.dashboard_path,
                getattr(self, route.dashboard_handler),
                list(route.methods),
                route.description,
            )
            for route in PROMPT_CATALOG_ROUTE_CONTRACTS
        ]

    def register(self) -> None:
        context = getattr(self.plugin, "context", None)
        register_web_api = getattr(context, "register_web_api", None)
        if not callable(register_web_api):
            return

        routes = [
            ("health", self.page_health, ["GET"], "Selfie Image health"),
            ("health/history", self.page_health_history, ["GET"], "Selfie Image channel health history"),
            ("health/channels/clear", self.page_channel_health_clear, ["POST"], "Selfie Image clear channel health"),
            ("context", self.page_context_get, ["GET"], "Selfie Image conversation context"),
            ("context/clear", self.page_context_clear, ["POST"], "Selfie Image clear conversation context"),
            *self._config_route_entries(),
            *self._proxy_route_entries(),
            ("qq-groups", self.page_qq_groups, ["GET"], "Selfie Image QQ groups"),
            ("qq-groups/config", self.page_qq_groups_config, ["POST"], "Selfie Image QQ group config"),
            ("selfie-reference", self.page_selfie_reference_get, ["GET"], "Selfie Image get reference"),
            ("selfie-reference", self.page_selfie_reference_post, ["POST"], "Selfie Image save reference"),
            ("selfie-reference/clear", self.page_selfie_reference_clear, ["POST"], "Selfie Image clear reference"),
            ("selfie-appearance-type", self.page_selfie_appearance_type, ["POST"], "Selfie Image appearance type"),
            ("selfie-profile/refresh-local", self.page_selfie_profile_refresh_local, ["POST"], "Selfie Image local refresh profile"),
            ("test-image-channel", self.page_test_image_channel, ["POST"], "Selfie Image sync channel test"),
            ("test-image-channel/tasks", self.page_test_image_task_start, ["POST"], "Selfie Image start channel test task"),
            (
                "test-image-channel/tasks/<task_id>",
                self.page_test_image_task_status,
                ["GET"],
                "Selfie Image channel test task status",
            ),
            *self._task_route_entries(),
            ("refresh-image-models", self.page_refresh_image_models, ["POST"], "Selfie Image refresh models"),
            *self._record_route_entries(),
            *self._asset_route_entries(),
            *self._asset_collection_route_entries(),
            *self._canvas_route_entries(),
            ("cache/cleanup", self.page_cache_cleanup, ["POST"], "Selfie Image cleanup cache"),
            ("cache-image", self.page_cache_image_file, ["GET"], "Selfie Image cache image download"),
            ("cache-image-thumbnail", self.page_cache_image_thumbnail, ["GET"], "Selfie Image cache image thumbnail"),
            ("cache-image-preview", self.page_cache_image_preview, ["GET"], "Selfie Image cache image preview"),
            ("auth/check", self.page_auth_check, ["POST", "GET"], "Selfie Image dashboard auth check"),
            *self._prompt_catalog_route_entries(),
        ]
        for route, handler, methods, desc in routes:
            # Register both bare and /page/ endpoints for Dashboard routing.
            for prefix in DASHBOARD_ROUTE_PREFIXES:
                register_web_api(f"/{PLUGIN_NAME}/{prefix}{route}", handler, methods, desc)

    @staticmethod
    def _ok(data: Any = None, **extra: Any) -> Any:
        payload = {"success": True, "data": data}
        payload.update(extra)
        # AstrBot Dashboard parent unwraps response.data once before postMessage.
        # Keep the Flask-compatible envelope inside that field so the iframe still
        # receives {success, data, ...} instead of only the bare data value.
        return json_response({"data": payload})

    @staticmethod
    def _fail(message: str, status: int = 400) -> Any:
        text = redact_sensitive_text(message)
        if error_response is not None:
            return error_response(text, status_code=status)
        return json_response({"status": "error", "message": text, "data": {}}, status_code=status)

    async def _json_object_payload(self) -> tuple[Optional[dict], Any]:
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return None, self._fail("请求体必须是 JSON 对象")
        return payload, None

    def _query_value(self, name: str, default: str = "") -> str:
        query = getattr(request, "query", None)
        if query is None:
            return default
        value = query.get(name, default)
        return default if value is None else str(value)

    def _int_query(self, name: str, default: int, minimum: int, maximum: int) -> tuple[Optional[int], Any]:
        try:
            return parse_bounded_int(request.query, name, default, minimum, maximum), None
        except WebContractError as exc:
            return None, self._fail(str(exc), exc.status_code)

    def _timestamp_query(self, name: str, *, end_of_day: bool = False) -> tuple[Optional[float], Any]:
        try:
            return parse_timestamp_query(request.query, name, end_of_day=end_of_day), None
        except WebContractError as exc:
            return None, self._fail(str(exc), exc.status_code)

    def _record_result(self, call: Any) -> Any:
        try:
            result: RecordControllerResult = call()
        except WebContractError as exc:
            return self._fail(str(exc), exc.status_code)
        except Exception as exc:
            return self._fail(str(exc), 500)
        return self._ok(result.data, **result.extra)

    def _asset_result(self, call: Any) -> Any:
        try:
            result: AssetControllerResult = call()
        except WebContractError as exc:
            return self._fail(str(exc), exc.status_code)
        except Exception as exc:
            return self._fail(str(exc), 500)
        return self._ok(result.data, **result.extra)

    def _canvas_result(self, call: Any) -> Any:
        try:
            result: CanvasControllerResult = call()
        except WebContractError as exc:
            return self._fail(str(exc), exc.status_code)
        except Exception as exc:
            return self._fail(str(exc), 500)
        return self._ok(result.data, **result.extra)

    def _prompt_catalog_result(self, call: Any) -> Any:
        try:
            result: PromptCatalogControllerResult = call()
        except WebContractError as exc:
            return self._fail(str(exc), exc.status_code)
        except Exception as exc:
            return self._fail(str(exc), 500)
        return self._ok(result.data, **result.extra)

    def _config_result(self, call: Any) -> Any:
        try:
            result: ConfigControllerResult = call()
        except WebContractError as exc:
            return self._fail(str(exc), exc.status_code)
        except Exception as exc:
            return self._fail(str(exc), 500)
        return self._ok(result.data, **result.extra)

    async def _proxy_result(self, call: Any) -> Any:
        try:
            result = call()
            if inspect.isawaitable(result):
                result = await result
            result: ProxyControllerResult
        except WebContractError as exc:
            return self._fail(str(exc), exc.status_code)
        except Exception as exc:
            return self._fail(str(exc), 500)
        return self._ok(result.data, **result.extra)

    def _task_result(self, call: Any, *, include_message: bool = False) -> Any:
        try:
            result: TaskControllerResult = call()
        except WebContractError as exc:
            return self._fail(str(exc), exc.status_code)
        extra = {"message": result.message} if include_message else {}
        return self._ok(result.data, **extra)

    def _task_status(self, task_id: str) -> Any:
        return self._task_result(lambda: self._task_controller.status(task_id))

    def _gallery(self) -> Any:
        limit, offset = gallery_pagination(
            self._query_value("limit"), self._query_value("offset")
        )
        return self._canvas_result(
            lambda: self._canvas_controller.gallery(limit, offset)
        )
    async def page_auth_check(self) -> Any:
        return self._ok({"authorized": True, "source": "dashboard"})

    async def page_health(self) -> Any:
        return self._ok(build_health_payload(self.plugin, dashboard_page_source_status()))

    async def page_health_history(self) -> Any:
        getter = getattr(self.plugin, "get_channel_health_history", None)
        if not callable(getter):
            return self._fail("当前版本不支持渠道健康历史", 501)
        try:
            try:
                window = parse_bounded_int(request.query, "window", 86400, 0, 31 * 86400)
            except WebContractError as exc:
                return self._fail(str(exc), exc.status_code)
            export = self._query_value("export").strip().lower() in {"1", "true", "yes", "on"}
            try:
                data = getter(window_seconds=window, media_type=self._query_value("media_type"), export=export)
            except TypeError:
                data = getter(window_seconds=window, media_type=self._query_value("media_type"))
            return self._ok(redact_sensitive_data(data))
        except Exception as exc:
            return self._fail(str(exc), 400)
    async def page_context_get(self) -> Any:
        getter = getattr(self.plugin, "get_conversation_context", None)
        if not callable(getter):
            return self._fail("当前版本不支持会话上下文", 501)
        try:
            return self._ok(redact_sensitive_data(getter(None)))
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_context_clear(self) -> Any:
        clearer = getattr(self.plugin, "clear_conversation_context", None)
        if not callable(clearer):
            return self._fail("当前版本不支持会话上下文", 501)
        try:
            return self._ok(clearer(None), message="会话上下文已清除")
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_storage_consistency(self) -> Any:
        return self._record_result(lambda: self._record_controller.consistency())

    async def page_storage_consistency_repair(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._record_result(
            lambda: self._record_controller.repair_consistency(payload)
        )


    async def page_proxies_list(self) -> Any:
        return await self._proxy_result(lambda: self._proxy_controller.list())

    async def page_proxy_test(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return await self._proxy_result(lambda: self._proxy_controller.test(payload))

    async def page_proxy_quality(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return await self._proxy_result(lambda: self._proxy_controller.quality(payload))

    async def page_config_get(self) -> Any:
        return self._config_result(lambda: self._config_controller.get())

    async def page_config_post(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._config_result(lambda: self._config_controller.update(payload))

    async def page_config_export(self) -> Any:
        return self._config_result(
            lambda: self._config_controller.export(self._query_value("mode"))
        )

    async def page_config_import_preview(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._config_result(
            lambda: self._config_controller.preview_import(payload)
        )

    async def page_config_import(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._config_result(
            lambda: self._config_controller.import_config(payload)
        )

    async def page_qq_groups(self) -> Any:
        try:
            refresh = str(self._query_value("refresh") or "").strip().lower() in {"1", "true", "yes", "on"}
            return self._ok(await self.plugin.list_qq_groups_for_web(force=refresh))
        except Exception as exc:
            return self._fail(str(exc))

    async def page_qq_groups_config(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(self.plugin.update_group_chat_configs_from_web(payload))
        except Exception as exc:
            return self._fail(str(exc))

    async def page_selfie_reference_get(self) -> Any:
        return self._ok(self.plugin.get_selfie_reference_payload())

    async def page_selfie_reference_post(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        assert payload is not None
        try:
            data = self.plugin.save_selfie_reference_from_web(payload)
            return self._ok(data, message="自拍参考图已保存")
        except Exception as exc:
            return self._fail(str(exc))

    async def page_selfie_reference_clear(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._ok(self.plugin.clear_selfie_reference_from_web(), message="自拍参考图已清除")

    async def page_selfie_appearance_type(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        assert payload is not None
        try:
            data = self.plugin.set_selfie_appearance_type_from_web(payload)
            return self._ok(data, message="形象类型已保存")
        except Exception as exc:
            return self._fail(str(exc))

    async def page_selfie_profile_refresh_local(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            data = await self.plugin.refresh_selfie_profile_local_from_web()
            return self._ok(data, message="本地今日穿搭已刷新")
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_test_image_channel(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        assert payload is not None
        try:
            data = await self.plugin.web_test_image(payload)
            return self._ok(redact_sensitive_data(data))
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_test_image_task_start(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        assert payload is not None
        try:
            data = self.plugin.start_web_image_task(payload)
            return self._ok(redact_sensitive_data(data))
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_test_image_task_status(self, task_id: str) -> Any:
        return self._task_status(task_id)

    async def page_task_cancel(self, task_id: str) -> Any:
        return self._task_result(lambda: self._task_controller.cancel(task_id), include_message=True)

    async def page_refresh_image_models(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        assert payload is not None
        try:
            data = await self.plugin.web_refresh_image_models(payload)
            return self._ok(data, count=len(data))
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_records(self) -> Any:
        return self._record_result(lambda: self._record_controller.list(request.query))

    async def page_channel_health_clear(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        assert payload is not None
        channel = str(payload.get("channel") or "").strip()
        return self._ok(self.plugin.clear_channel_health(channel), message="渠道健康状态已清除")

    async def page_metrics(self) -> Any:
        return self._record_result(
            lambda: self._record_controller.metrics(self._query_value("window"))
        )

    async def page_tasks(self) -> Any:
        return self._task_result(lambda: self._task_controller.list_tasks(request.query))

    async def page_task_detail(self, task_id: str) -> Any:
        """Return the redacted task detail used by the embedded dashboard."""
        return self._task_result(lambda: self._task_controller.detail(task_id))

    @staticmethod
    def _task_ids_from_payload(payload: Any) -> tuple[Optional[list[str]], Any]:
        return normalize_task_ids(payload, WEB_TASK_ID_RE, max_length=MAX_WEB_TASK_ID_LENGTH)

    async def page_tasks_export(self) -> Any:
        return self._task_result(lambda: self._task_controller.export(self._query_value("ids")))

    async def page_tasks_delete(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._task_result(lambda: self._task_controller.delete(payload), include_message=True)

    async def page_tasks_retry(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._task_result(lambda: self._task_controller.retry(payload), include_message=True)

    async def page_record_detail(self, record_id: str) -> Any:
        return self._record_result(lambda: self._record_controller.detail(record_id))

    async def page_record_media_sources(self, record_id: str) -> Any:
        return self._record_result(lambda: self._record_controller.media_sources(record_id))

    async def page_record_metadata(self, record_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._record_result(
            lambda: self._record_controller.update_metadata(record_id, payload)
        )

    async def page_record_reuse(self, record_id: str) -> Any:
        return self._record_result(lambda: self._record_controller.reuse(record_id))

    async def page_record_retry(self, record_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._record_result(
            lambda: self._record_controller.retry(record_id, payload)
        )

    async def page_records_compare(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._record_result(lambda: self._record_controller.compare(payload))

    async def page_assets(self) -> Any:
        return self._asset_result(lambda: self._asset_controller.list(request.query))

    async def page_asset_collections(self) -> Any:
        return self._asset_result(lambda: self._asset_collection_controller.list())

    async def page_asset_collection_create(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._asset_result(
            lambda: self._asset_collection_controller.create(payload)
        )

    async def page_asset_collections_export(self) -> Any:
        return self._asset_result(
            lambda: self._asset_collection_controller.export()
        )

    async def page_asset_collections_import(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._asset_result(
            lambda: self._asset_collection_controller.import_data(payload)
        )

    async def page_asset_collection_update(self, collection_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._asset_result(
            lambda: self._asset_collection_controller.update(collection_id, payload)
        )

    async def page_asset_collection_detail(self, collection_id: str) -> Any:
        return self._asset_result(
            lambda: self._asset_collection_controller.detail(collection_id)
        )

    async def page_asset_collection_delete(self, collection_id: str) -> Any:
        return self._asset_result(
            lambda: self._asset_collection_controller.delete(collection_id)
        )

    async def _asset_collection_records(self, collection_id: str, *, remove: bool) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._asset_result(
            lambda: self._asset_collection_controller.update_records(
                collection_id, payload, remove=remove
            )
        )

    async def page_asset_collection_add(self, collection_id: str) -> Any:
        return await self._asset_collection_records(collection_id, remove=False)

    async def page_asset_collection_remove(self, collection_id: str) -> Any:
        return await self._asset_collection_records(collection_id, remove=True)

    async def page_asset_collection_studio(self, collection_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._asset_result(
            lambda: self._asset_collection_controller.studio(collection_id, payload)
        )

    async def page_assets_metadata(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._asset_result(
            lambda: self._asset_controller.update_metadata(payload)
        )

    async def page_assets_tags(self) -> Any:
        return self._asset_result(lambda: self._asset_controller.tags())

    async def page_assets_export(self) -> Any:
        return self._asset_result(
            lambda: self._asset_controller.export(self._query_value("ids"))
        )

    async def page_assets_import(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._asset_result(
            lambda: self._asset_controller.import_metadata(payload)
        )

    async def page_assets_delete(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._asset_result(lambda: self._asset_controller.delete(payload))

    async def page_asset_studio(self, record_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._asset_result(
            lambda: self._asset_controller.studio_one(record_id, payload)
        )

    async def page_assets_studio(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._asset_result(
            lambda: self._asset_controller.studio_many(payload)
        )

    async def page_records_clear(self) -> Any:
        _, error = await self._json_object_payload()
        if error:
            return error
        return self._record_result(lambda: self._record_controller.clear())

    async def page_cache_cleanup(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        cleanup = getattr(self.plugin, "cleanup_image_cache_from_web", None)
        if not callable(cleanup):
            return self._fail("当前版本不支持手动缓存清理", 501)
        raw_confirm = (payload or {}).get("confirm", False)
        confirm = bool(raw_confirm) if not isinstance(raw_confirm, str) else raw_confirm.strip().lower() in {"1", "true", "yes", "on"}
        token = str((payload or {}).get("plan_token") or "").strip()
        try:
            data = cleanup(confirm=confirm, plan_token=token)
            return self._ok(data, message="缓存清理完成" if confirm else "请确认缓存清理")
        except ValueError as exc:
            return self._fail(str(exc), 409)
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_cache_image_file(self) -> Any:
        try:
            rel_path = self._query_value("path")
            if len(rel_path) > MAX_CACHE_IMAGE_PATH_LENGTH:
                return self._fail("图片路径过长", 400)
            info = self.plugin.get_cached_image_info(rel_path)
        except Exception as exc:
            return self._fail(str(exc), 400)
        if not info.get("exists"):
            return self._fail("图片已清理", 404)
        if not info.get("is_image"):
            return self._fail("缓存文件不是有效图片", 400)
        return file_response(
            info["absolute_path"],
            filename=str(info.get("name") or "media.bin"),
            content_type=info.get("mime_type") or "application/octet-stream",
        )

    async def page_cache_image_preview(self) -> Any:
        try:
            rel_path = self._query_value("path")
            if len(rel_path) > MAX_CACHE_IMAGE_PATH_LENGTH:
                return self._fail("图片路径过长", 400)
            info = self.plugin.get_cached_image_info(rel_path)
        except Exception as exc:
            return self._fail(str(exc), 400)
        if not info.get("exists"):
            return self._ok(
                {
                    "path": rel_path,
                    "available": False,
                    "message": "图片已清理",
                },
                message="图片已清理",
            )
        if not info.get("is_image"):
            return self._fail("缓存文件不是有效图片", 400)
        path = str(info.get("absolute_path") or "")
        try:
            with open(path, "rb") as handle:
                raw = handle.read(PAGE_PREVIEW_MAX_BYTES + 1)
        except Exception as exc:
            return self._fail(str(exc), 400)
        if len(raw) > PAGE_PREVIEW_MAX_BYTES:
            return self._fail("图片过大，请改用下载查看", 413)
        mime = info.get("mime_type") or "image/png"
        return self._ok(
            {
                "path": rel_path,
                "mime_type": mime,
                "size": len(raw),
                "data_url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}",
            }
        )

    async def page_cache_image_thumbnail(self) -> Any:
        try:
            rel_path = self._query_value("path")
            if len(rel_path) > MAX_CACHE_IMAGE_PATH_LENGTH:
                return self._fail("图片路径过长", 400)
            info = self.plugin.get_cached_image_info(rel_path)
            if not info.get("exists"):
                return self._fail("图片已清理", 404)
            if not info.get("is_image"):
                return self._fail("缓存文件不是有效图片", 400)
            raw = await asyncio.to_thread(render_image_thumbnail, info["absolute_path"])
        except Exception as exc:
            return self._fail(str(exc), 400)
        return self._ok(
            {
                "path": rel_path,
                "mime_type": "image/jpeg",
                "size": len(raw),
                "data_url": f"data:image/jpeg;base64,{base64.b64encode(raw).decode('ascii')}",
            }
        )

    async def page_studio_list(self) -> Any:
        return self._canvas_result(
            lambda: self._canvas_controller.studio_list(include_metadata=False)
        )

    async def page_studio_create(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.studio_create(payload)
        )

    async def page_studio_copy(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.studio_copy(session_id, payload)
        )

    async def page_studio_get(self, session_id: str) -> Any:
        return self._canvas_result(
            lambda: self._canvas_controller.studio_get(session_id)
        )

    async def page_studio_update(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.studio_update(session_id, payload)
        )

    async def page_studio_canvas_get(self, session_id: str) -> Any:
        return self._canvas_result(
            lambda: self._canvas_controller.studio_canvas(session_id)
        )

    async def page_studio_canvas_update(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.studio_canvas_update(session_id, payload)
        )

    async def page_studio_canvas_node_create(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.studio_canvas_node_create(session_id, payload)
        )

    async def page_studio_canvas_node_delete(self, session_id: str, node_id: str) -> Any:
        return self._canvas_result(
            lambda: self._canvas_controller.studio_canvas_node_delete(session_id, node_id)
        )

    async def page_studio_canvas_node_connect(self, session_id: str, node_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.studio_canvas_node_connect(
                session_id, node_id, payload
            )
        )

    async def page_studio_delete(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.studio_delete(session_id)
        )

    async def page_studio_set_slot(self, session_id: str, slot_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.studio_set_slot(session_id, slot_id, payload)
        )

    async def page_studio_add_slot(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.studio_add_slot(session_id, payload)
        )

    async def page_studio_reorder(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.studio_reorder(session_id, payload)
        )

    async def page_studio_promote(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.studio_promote(session_id, payload)
        )

    async def page_studio_run(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.studio_run(session_id, payload)
        )

    async def page_studio_task(self, task_id: str) -> Any:
        return self._task_status(task_id)

    async def page_studio_gallery(self) -> Any:
        return self._gallery()

    async def page_creative_canvas_list(self) -> Any:
        return self._canvas_result(
            lambda: self._canvas_controller.creative_canvas_list(include_metadata=False)
        )

    async def page_creative_canvas_create(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.creative_canvas_create(payload)
        )

    async def page_creative_canvas_get(self, session_id: str) -> Any:
        return self._canvas_result(
            lambda: self._canvas_controller.creative_canvas_get(session_id)
        )

    async def page_creative_canvas_update(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.creative_canvas_update(session_id, payload)
        )

    async def page_creative_canvas_delete(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.creative_canvas_delete(session_id)
        )

    async def page_creative_canvas_set_slot(self, session_id: str, slot_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.creative_canvas_set_slot(
                session_id, slot_id, payload
            )
        )

    async def page_creative_canvas_add_slot(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.creative_canvas_add_slot(session_id, payload)
        )

    async def page_creative_canvas_reorder(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.creative_canvas_reorder(session_id, payload)
        )

    async def page_creative_canvas_promote(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.creative_canvas_promote(session_id, payload)
        )

    async def page_creative_canvas_run(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._canvas_result(
            lambda: self._canvas_controller.creative_canvas_run(session_id, payload)
        )

    async def page_creative_canvas_gallery(self) -> Any:
        return self._gallery()

    async def page_prompt_presets(self) -> Any:
        query = getattr(request, "query", {}) or {}
        return self._prompt_catalog_result(
            lambda: self._prompt_catalog_controller.list_presets(query)
        )

    async def page_prompt_presets_manage(self) -> Any:
        query = getattr(request, "query", {}) or {}
        return self._prompt_catalog_result(
            lambda: self._prompt_catalog_controller.list_presets(query, managed=True)
        )

    async def page_prompt_preset_save(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._prompt_catalog_result(
            lambda: self._prompt_catalog_controller.save_preset(payload)
        )

    async def page_prompt_preset_delete(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._prompt_catalog_result(
            lambda: self._prompt_catalog_controller.delete_preset(payload)
        )

    async def page_prompt_presets_import(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._prompt_catalog_result(
            lambda: self._prompt_catalog_controller.import_presets(payload)
        )

    async def page_cos_look_sets(self) -> Any:
        return self._prompt_catalog_result(
            self._prompt_catalog_controller.list_cos_look_sets
        )

    async def page_cos_pools(self) -> Any:
        query = getattr(request, "query", {}) or {}
        return self._prompt_catalog_result(
            lambda: self._prompt_catalog_controller.list_cos_pools(query)
        )

    async def page_cos_pool_favorite(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._prompt_catalog_result(
            lambda: self._prompt_catalog_controller.favorite_cos(payload)
        )

    async def page_cos_custom_save(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._prompt_catalog_result(
            lambda: self._prompt_catalog_controller.save_custom_cos(payload)
        )

    async def page_cos_custom_delete(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._prompt_catalog_result(
            lambda: self._prompt_catalog_controller.delete_custom_cos(payload)
        )

    async def page_cos_pool_export(self) -> Any:
        return self._prompt_catalog_result(self._prompt_catalog_controller.export_cos)

    async def page_cos_pool_import(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._prompt_catalog_result(
            lambda: self._prompt_catalog_controller.import_cos(payload)
        )

    async def page_creative_template_render(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._prompt_catalog_result(
            lambda: self._prompt_catalog_controller.render_template(payload)
        )

    async def page_creative_variations(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._prompt_catalog_result(
            lambda: self._prompt_catalog_controller.variations(payload)
        )
