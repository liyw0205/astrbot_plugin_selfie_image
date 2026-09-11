"""AstrBot Dashboard embedded page APIs for Selfie Image.

Dashboard plugin-page APIs registered via ``context.register_web_api``.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Optional

from ..core.constants import PLUGIN_NAME
from ..generation.generation_records import metric_window_seconds
from ..core.utils import (
    generation_record_media_sources,
    redact_generation_record,
    redact_generation_record_for_detail,
    redact_sensitive_data,
    redact_sensitive_text,
)
from .web import (
    MAX_ASSET_PAGE_LIMIT,
    MAX_CACHE_IMAGE_PATH_LENGTH,
    MAX_RECORD_PAGE_LIMIT,
    MAX_TASK_PAGE_LIMIT,
    MAX_TASK_OFFSET,
    MAX_WEB_RECORD_ID_LENGTH,
    MAX_WEB_TASK_ID_LENGTH,
    WEB_TASK_ID_RE,
    dashboard_page_source_status,
)
from .contracts import DASHBOARD_ROUTE_PREFIXES, DASHBOARD_TASK_CANCEL_ROUTES
from .services import (
    WebContractError,
    build_health_payload,
    filter_record_page,
    normalize_task_ids,
    parse_bounded_int,
    parse_query_bool,
    parse_task_query,
    parse_timestamp_query,
    record_matches_query as shared_record_matches_query,
    validate_task_id,
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

    def register(self) -> None:
        context = getattr(self.plugin, "context", None)
        register_web_api = getattr(context, "register_web_api", None)
        if not callable(register_web_api):
            return

        routes = [
            ("health", self.page_health, ["GET"], "Selfie Image health"),
            ("health/channels/clear", self.page_channel_health_clear, ["POST"], "Selfie Image clear channel health"),
            ("config", self.page_config_get, ["GET"], "Selfie Image get config"),
            ("config", self.page_config_post, ["POST"], "Selfie Image save config"),
            ("config/export", self.page_config_export, ["GET"], "Selfie Image export config"),
            ("config/import/preview", self.page_config_import_preview, ["POST"], "Selfie Image preview config import"),
            ("config/import", self.page_config_import, ["POST"], "Selfie Image import config"),
            ("selfie-reference", self.page_selfie_reference_get, ["GET"], "Selfie Image get reference"),
            ("selfie-reference", self.page_selfie_reference_post, ["POST"], "Selfie Image save reference"),
            ("selfie-reference/clear", self.page_selfie_reference_clear, ["POST"], "Selfie Image clear reference"),
            ("selfie-appearance-type", self.page_selfie_appearance_type, ["POST"], "Selfie Image appearance type"),
            ("selfie-profile/refresh", self.page_selfie_profile_refresh, ["POST"], "Selfie Image refresh profile"),
            ("test-image-channel", self.page_test_image_channel, ["POST"], "Selfie Image sync channel test"),
            ("test-image-channel/tasks", self.page_test_image_task_start, ["POST"], "Selfie Image start channel test task"),
            (
                "test-image-channel/tasks/<task_id>",
                self.page_test_image_task_status,
                ["GET"],
                "Selfie Image channel test task status",
            ),
            *[
                (route, self.page_task_cancel, ["POST"], "Selfie Image cancel task")
                for route in DASHBOARD_TASK_CANCEL_ROUTES
            ],
            ("test-video-channel/tasks", self.page_test_video_task_start, ["POST"], "Selfie Image start video channel test"),
            (
                "test-video-channel/tasks/<task_id>",
                self.page_test_image_task_status,
                ["GET"],
                "Selfie Image video channel test task status",
            ),
            ("refresh-image-models", self.page_refresh_image_models, ["POST"], "Selfie Image refresh models"),
            ("records", self.page_records, ["GET"], "Selfie Image generation records"),
            ("metrics", self.page_metrics, ["GET"], "Selfie Image generation metrics"),
            ("tasks", self.page_tasks, ["GET"], "Selfie Image task queue"),
            ("tasks/export", self.page_tasks_export, ["GET"], "Selfie Image export tasks"),
            ("tasks/delete", self.page_tasks_delete, ["POST"], "Selfie Image delete tasks"),
            ("tasks/retry", self.page_tasks_retry, ["POST"], "Selfie Image retry tasks"),
            ("tasks/<task_id>", self.page_task_detail, ["GET"], "Selfie Image task detail"),
            ("records/<record_id>/media-sources", self.page_record_media_sources, ["GET"], "Selfie Image record media sources"),
            ("records/<record_id>", self.page_record_detail, ["GET"], "Selfie Image record detail"),
            ("records/<record_id>/metadata", self.page_record_metadata, ["POST"], "Selfie Image record metadata"),
            ("records/<record_id>/asset", self.page_record_metadata, ["POST"], "Selfie Image record asset metadata"),
            ("records/<record_id>/reuse", self.page_record_reuse, ["GET", "POST"], "Selfie Image reuse record"),
            ("records/<record_id>/retry", self.page_record_retry, ["POST"], "Selfie Image retry record generation"),
            ("records/compare", self.page_records_compare, ["POST"], "Selfie Image compare records"),
            ("assets", self.page_assets, ["GET"], "Selfie Image asset library"),
            ("assets/tags", self.page_assets_tags, ["GET"], "Selfie Image asset tags"),
            ("assets/export", self.page_assets_export, ["GET"], "Selfie Image asset metadata export"),
            ("assets/import", self.page_assets_import, ["POST"], "Selfie Image asset metadata import"),
            ("assets/metadata", self.page_assets_metadata, ["POST"], "Selfie Image batch asset metadata"),
            ("assets/delete", self.page_assets_delete, ["POST"], "Selfie Image batch asset delete"),
            ("assets/studio", self.page_assets_studio, ["POST"], "Selfie Image batch add assets to studio"),
            ("assets/<record_id>/studio", self.page_asset_studio, ["POST"], "Selfie Image add asset to studio"),
            ("records/clear", self.page_records_clear, ["POST"], "Selfie Image clear records"),
            ("cache/cleanup", self.page_cache_cleanup, ["POST"], "Selfie Image cleanup cache"),
            ("cache-image", self.page_cache_image_file, ["GET"], "Selfie Image cache image download"),
            ("cache-image-preview", self.page_cache_image_preview, ["GET"], "Selfie Image cache image preview"),
            ("auth/check", self.page_auth_check, ["POST", "GET"], "Selfie Image dashboard auth check"),
            ("studio/sessions", self.page_studio_list, ["GET"], "Selfie Image studio list"),
            ("studio/sessions", self.page_studio_create, ["POST"], "Selfie Image studio create"),
            ("studio/sessions/<session_id>", self.page_studio_get, ["GET"], "Selfie Image studio get"),
            ("studio/sessions/<session_id>", self.page_studio_update, ["POST"], "Selfie Image studio update"),
            ("studio/sessions/<session_id>/delete", self.page_studio_delete, ["POST"], "Selfie Image studio delete"),
            ("studio/sessions/<session_id>/slots/<slot_id>", self.page_studio_set_slot, ["POST"], "Selfie Image studio slot"),
            ("studio/sessions/<session_id>/slots", self.page_studio_add_slot, ["POST"], "Selfie Image studio add slot"),
            ("studio/sessions/<session_id>/reorder", self.page_studio_reorder, ["POST"], "Selfie Image studio reorder"),
            ("studio/sessions/<session_id>/promote", self.page_studio_promote, ["POST"], "Selfie Image studio promote"),
            ("studio/sessions/<session_id>/run", self.page_studio_run, ["POST"], "Selfie Image studio run"),
            ("studio/tasks/<task_id>", self.page_studio_task, ["GET"], "Selfie Image studio task"),
            ("studio/gallery", self.page_studio_gallery, ["GET"], "Selfie Image studio gallery from records"),
            ("prompt-presets", self.page_prompt_presets, ["GET"], "Selfie Image prompt presets"),
            ("prompt-presets/manage", self.page_prompt_presets_manage, ["GET"], "Selfie Image managed prompt presets"),
            ("prompt-presets/manage/save", self.page_prompt_preset_save, ["POST"], "Selfie Image save prompt preset"),
            ("prompt-presets/manage/delete", self.page_prompt_preset_delete, ["POST"], "Selfie Image delete prompt preset"),
            ("prompt-presets/manage/import", self.page_prompt_presets_import, ["POST"], "Selfie Image import prompt presets"),
            ("cos-look-sets", self.page_cos_look_sets, ["GET"], "Selfie Image COS look sets"),
            ("cos-pools", self.page_cos_pools, ["GET"], "Selfie Image COS pools"),
            ("cos-pools/favorite", self.page_cos_pool_favorite, ["POST"], "Selfie Image favorite COS"),
            ("cos-pools/custom/save", self.page_cos_custom_save, ["POST"], "Selfie Image save custom COS"),
            ("cos-pools/custom/delete", self.page_cos_custom_delete, ["POST"], "Selfie Image delete custom COS"),
            ("cos-pools/export", self.page_cos_pool_export, ["GET"], "Selfie Image export COS pools"),
            ("cos-pools/import", self.page_cos_pool_import, ["POST"], "Selfie Image import COS pools"),
            ("creative/template/render", self.page_creative_template_render, ["POST"], "Selfie Image render prompt template"),
            ("creative/variations", self.page_creative_variations, ["POST"], "Selfie Image build prompt variations"),
            ("creative/storyboard", self.page_creative_storyboard, ["POST"], "Selfie Image parse video storyboard"),
            ("proxies", self.page_proxies_list, ["GET"], "Selfie Image proxy list"),
            ("proxies/test", self.page_proxy_test, ["POST"], "Selfie Image proxy connectivity test"),
            ("proxies/quality-check", self.page_proxy_quality, ["POST"], "Selfie Image proxy quality test"),
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
        if payload is None:
            return {}, None
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

    def _record_matches(self, record: Any, source: str, model: str, success: str, keyword: str, media_type: str = "") -> bool:
        return shared_record_matches_query(record, source, model, success, keyword, media_type)

    @staticmethod
    def _query_bool(value: str) -> Optional[bool]:
        return parse_query_bool(value)

    def _filtered_records(self, records: list[Any]) -> tuple[Optional[list], Optional[dict], Any]:
        try:
            page, meta = filter_record_page(records, request.query)
            return page, meta, None
        except WebContractError as exc:
            return None, None, self._fail(str(exc), exc.status_code)

    async def page_auth_check(self) -> Any:
        return self._ok({"authorized": True, "source": "dashboard"})

    async def page_health(self) -> Any:
        return self._ok(build_health_payload(self.plugin, dashboard_page_source_status()))


    async def page_proxies_list(self) -> Any:
        try:
            return self._ok(self.plugin.list_proxies_for_web(mask_password=True))
        except Exception as exc:
            return self._fail(str(exc))

    async def page_proxy_test(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            data = await self.plugin.test_proxy_connectivity(
                proxy_id=str((payload or {}).get("id") or (payload or {}).get("proxy_id") or ""),
                proxy=(payload or {}).get("proxy") if isinstance((payload or {}).get("proxy"), dict) else payload,
            )
            return self._ok(data)
        except Exception as exc:
            return self._fail(str(exc))

    async def page_proxy_quality(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            data = await self.plugin.test_proxy_quality(
                proxy_id=str((payload or {}).get("id") or (payload or {}).get("proxy_id") or ""),
                proxy=(payload or {}).get("proxy") if isinstance((payload or {}).get("proxy"), dict) else payload,
            )
            return self._ok(data)
        except Exception as exc:
            return self._fail(str(exc))

    async def page_config_get(self) -> Any:
        return self._ok(self.plugin.get_config_for_web())

    async def page_config_post(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        assert payload is not None
        if "config" in payload:
            if not isinstance(payload.get("config"), dict):
                return self._fail("config 必须是 JSON 对象")
            patch = payload["config"]
        else:
            patch = payload
        try:
            data = self.plugin.update_config_from_web(patch)
            return self._ok(data)
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_config_export(self) -> Any:
        return self._ok(self.plugin.export_config_for_web())

    async def page_config_import_preview(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(self.plugin.preview_config_import(payload or {}))
        except Exception as exc:
            return self._fail(str(exc))

    async def page_config_import(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(self.plugin.import_config_from_web(payload or {}), message="配置导入成功")
        except Exception as exc:
            return self._fail(str(exc), 400)

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

    async def page_selfie_profile_refresh(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            data = await self.plugin.refresh_selfie_profile_from_web()
            return self._ok(data, message="今日自拍设定已刷新")
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

    async def page_test_video_task_start(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        assert payload is not None
        try:
            data = self.plugin.start_web_image_task({**payload, "media_type": "video"})
            return self._ok(redact_sensitive_data(data))
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_test_image_task_status(self, task_id: str) -> Any:
        try:
            task_id_text = validate_task_id(task_id, WEB_TASK_ID_RE, max_length=MAX_WEB_TASK_ID_LENGTH)
        except WebContractError as exc:
            return self._fail(str(exc), exc.status_code)
        try:
            return self._ok(redact_sensitive_data(self.plugin.get_web_image_task(task_id_text)))
        except Exception as exc:
            return self._fail(str(exc), 404)

    async def page_task_cancel(self, task_id: str) -> Any:
        try:
            task_id_text = validate_task_id(task_id, WEB_TASK_ID_RE, max_length=MAX_WEB_TASK_ID_LENGTH)
        except WebContractError as exc:
            return self._fail(str(exc), exc.status_code)
        try:
            task = self.plugin.get_web_image_task(task_id_text)
            if str(task.get("status") or "") not in {"queued", "running"}:
                return self._fail("任务已经结束，不能取消", 409)
            message = self.plugin.cancel_image_task(task_id_text, is_admin=True)
            updated = self.plugin.get_web_image_task(task_id_text)
            if str(updated.get("status") or "") not in {"cancelled"} and not updated.get("cancel_requested"):
                return self._fail("任务已经结束，不能取消", 409)
            return self._ok(
                redact_sensitive_data(updated),
                message=message,
            )
        except PermissionError as exc:
            return self._fail(str(exc), 403)
        except Exception as exc:
            return self._fail(str(exc), 404)

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
        data = [
            redact_generation_record(item)
            for item in self.plugin.get_recent_records(summary=True)
            if isinstance(item, dict)
        ]
        page, meta, error = self._filtered_records(data if isinstance(data, list) else [])
        if error:
            return error
        assert page is not None and meta is not None
        return self._ok(page, **meta)

    async def page_channel_health_clear(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        assert payload is not None
        channel = str(payload.get("channel") or "").strip()
        return self._ok(self.plugin.clear_channel_health(channel), message="渠道健康状态已清除")

    async def page_metrics(self) -> Any:
        try:
            try:
                window_seconds = metric_window_seconds(self._query_value("window"))
            except ValueError as exc:
                return self._fail(str(exc), 400)
            if window_seconds is None:
                metrics_data = self.plugin.get_generation_metrics()
            else:
                metrics_data = self.plugin.get_generation_metrics(window_seconds=window_seconds)
            return self._ok(redact_sensitive_data(metrics_data))
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_tasks(self) -> Any:
        try:
            task_kwargs = parse_task_query(request.query)
            return self._ok(
                redact_sensitive_data(
                    self.plugin.list_web_tasks(**task_kwargs)
                )
            )
        except WebContractError as exc:
            return self._fail(str(exc), exc.status_code)
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_task_detail(self, task_id: str) -> Any:
        """Return the redacted task detail used by the embedded dashboard."""
        try:
            task_id_text = validate_task_id(task_id, WEB_TASK_ID_RE, max_length=MAX_WEB_TASK_ID_LENGTH)
        except WebContractError as exc:
            return self._fail(str(exc), exc.status_code)
        try:
            getter = getattr(self.plugin, "get_web_task_detail", None)
            data = getter(task_id_text) if callable(getter) else self.plugin.get_web_image_task(task_id_text)
            return self._ok(redact_sensitive_data(data))
        except Exception as exc:
            return self._fail(str(exc), 404)

    @staticmethod
    def _task_ids_from_payload(payload: Any) -> tuple[Optional[list[str]], Any]:
        return normalize_task_ids(payload, WEB_TASK_ID_RE, max_length=MAX_WEB_TASK_ID_LENGTH)

    async def page_tasks_export(self) -> Any:
        raw_ids = self._query_value("ids").strip()
        ids = [item.strip() for item in raw_ids.split(",") if item.strip()] if raw_ids else None
        if ids is not None:
            if len(ids) > 200:
                return self._fail("包含非法任务 ID", 400)
            try:
                for item in ids:
                    validate_task_id(item, WEB_TASK_ID_RE, max_length=MAX_WEB_TASK_ID_LENGTH)
            except WebContractError as exc:
                return self._fail("包含非法任务 ID", exc.status_code)
        try:
            exporter = getattr(self.plugin, "export_web_tasks", None)
            if not callable(exporter):
                return self._fail("当前版本不支持任务导出", 501)
            return self._ok(exporter(ids))
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_tasks_delete(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        ids, validation_error = self._task_ids_from_payload(payload)
        if validation_error:
            return self._fail(validation_error, 400)
        try:
            deleter = getattr(self.plugin, "delete_web_tasks", None)
            if not callable(deleter):
                return self._fail("当前版本不支持任务删除", 501)
            return self._ok(deleter(ids or []), message="任务记录已删除")
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_tasks_retry(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        ids, validation_error = self._task_ids_from_payload(payload)
        if validation_error:
            return self._fail(validation_error, 400)
        try:
            retrier = getattr(self.plugin, "retry_web_tasks", None)
            if not callable(retrier):
                return self._fail("当前版本不支持任务重试", 501)
            feedback = str((payload or {}).get("feedback") or "").strip()[:2000]
            strategy = str((payload or {}).get("strategy") or (payload or {}).get("retry_strategy") or "full")
            try:
                result = retrier(ids or [], feedback, strategy)
            except TypeError:
                result = retrier(ids or [], feedback)
            return self._ok(result, message="已提交任务重试")
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_record_detail(self, record_id: str) -> Any:
        record_id_text = str(record_id or "").strip()
        if not record_id_text or len(record_id_text) > MAX_WEB_RECORD_ID_LENGTH:
            return self._fail("非法记录 ID", 400)
        try:
            return self._ok(redact_generation_record_for_detail(self.plugin.get_record_for_web(record_id_text)))
        except Exception as exc:
            return self._fail(str(exc), 404)

    async def page_record_media_sources(self, record_id: str) -> Any:
        record_id_text = str(record_id or "").strip()
        if not record_id_text or len(record_id_text) > MAX_WEB_RECORD_ID_LENGTH:
            return self._fail("非法记录 ID", 400)
        try:
            return self._ok(generation_record_media_sources(self.plugin.get_record_for_web(record_id_text)))
        except Exception as exc:
            return self._fail(str(exc), 404)

    async def page_record_metadata(self, record_id: str) -> Any:
        record_id_text = str(record_id or "").strip()
        if not record_id_text or len(record_id_text) > MAX_WEB_RECORD_ID_LENGTH:
            return self._fail("非法记录 ID", 400)
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(self.plugin.update_record_asset_metadata(record_id_text, payload or {}), message="资产标记已保存")
        except Exception as exc:
            return self._fail(str(exc), 404)

    async def page_record_reuse(self, record_id: str) -> Any:
        record_id_text = str(record_id or "").strip()
        if not record_id_text or len(record_id_text) > MAX_WEB_RECORD_ID_LENGTH:
            return self._fail("非法记录 ID", 400)
        try:
            return self._ok(self.plugin.get_record_reuse_payload(record_id_text))
        except Exception as exc:
            return self._fail(str(exc), 404)

    async def page_record_retry(self, record_id: str) -> Any:
        record_id_text = str(record_id or "").strip()
        if not record_id_text or len(record_id_text) > MAX_WEB_RECORD_ID_LENGTH:
            return self._fail("非法记录 ID", 400)
        payload, error = await self._json_object_payload()
        if error:
            return error
        retry = getattr(self.plugin, "start_record_retry_task", None)
        if not callable(retry):
            return self._fail("当前版本不支持记录重试", 501)
        try:
            feedback = str((payload or {}).get("feedback") or "")
            strategy = str((payload or {}).get("strategy") or (payload or {}).get("retry_strategy") or "full")
            try:
                task = retry(record_id_text, feedback, strategy)
            except TypeError:
                task = retry(record_id_text, feedback)
            return self._ok(redact_sensitive_data(task), message="已提交重试任务")
        except ValueError as exc:
            return self._fail(str(exc), 400)
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_records_compare(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        ids = (payload or {}).get("ids", (payload or {}).get("record_ids"))
        if not isinstance(ids, list):
            return self._fail("ids 必须是数组", 400)
        try:
            data = self.plugin.compare_records_for_web(ids, int((payload or {}).get("limit") or 8))
            return self._ok(data)
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_assets(self) -> Any:
        favorite = self._query_bool(self._query_value("favorite"))
        pinned = self._query_bool(self._query_value("pinned"))
        try:
            media_type = self._query_value("media_type").strip().lower()
            if media_type not in {"", "image", "video"}:
                return self._fail("media_type 必须是 image 或 video", 400)
            raw_success = self._query_value("success").strip().lower()
            success = self._query_bool(raw_success)
            if raw_success and success is None:
                return self._fail("success 必须是 true 或 false", 400)
            offset, error = self._int_query("offset", 0, 0, 10000)
            if error:
                return error
            limit, error = self._int_query("limit", 48, 1, MAX_ASSET_PAGE_LIMIT)
            if error:
                return error
            sort = self._query_value("sort", "recent").strip().lower()
            if sort not in {"recent", "oldest", "priority", "pinned", "favorite", "asc"}:
                return self._fail("sort 必须是 recent、oldest、priority、pinned、favorite 或 asc", 400)
            query = getattr(self.plugin, "query_asset_records", None)
            if callable(query):
                data, meta = query(
                    favorite=favorite,
                    pinned=pinned,
                    tag=self._query_value("tag"),
                    source=self._query_value("source"),
                    model=self._query_value("model"),
                    media_type=media_type,
                    success=success,
                    keyword=self._query_value("q") or self._query_value("keyword"),
                    start_time=self._query_value("start_time"),
                    end_time=self._query_value("end_time"),
                    offset=int(offset or 0),
                    limit=int(limit or 48),
                    sort=sort,
                )
                return self._ok(redact_sensitive_data(data), count=len(data), **meta)
            data = self.plugin.get_asset_records(favorite=favorite, pinned=pinned, tag=self._query_value("tag"))
            return self._ok(redact_sensitive_data(data), count=len(data), total=len(data), filtered=len(data), offset=0, limit=len(data))
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_assets_metadata(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        ids = (payload or {}).get("ids", (payload or {}).get("record_ids"))
        if not isinstance(ids, list):
            return self._fail("ids 必须是数组", 400)
        try:
            return self._ok(self.plugin.update_records_asset_metadata(ids, payload or {}), message="批量资产标记已保存")
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_assets_tags(self) -> Any:
        try:
            getter = getattr(self.plugin, "list_asset_tags", None)
            if not callable(getter):
                return self._fail("当前版本不支持资产标签列表", 501)
            return self._ok(getter())
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_assets_export(self) -> Any:
        try:
            exporter = getattr(self.plugin, "export_asset_metadata", None)
            if not callable(exporter):
                return self._fail("当前版本不支持资产导出", 501)
            raw_ids = self._query_value("ids").strip()
            ids = [item.strip() for item in raw_ids.split(",") if item.strip()] if raw_ids else None
            if ids is not None and len(ids) > MAX_RECORD_PAGE_LIMIT:
                return self._fail(f"单次最多导出 {MAX_RECORD_PAGE_LIMIT} 条资产", 400)
            return self._ok(exporter(ids))
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_assets_import(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            importer = getattr(self.plugin, "import_asset_metadata", None)
            if not callable(importer):
                return self._fail("当前版本不支持资产导入", 501)
            return self._ok(importer(payload or {}), message="资产元数据已导入")
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_assets_delete(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        ids = (payload or {}).get("ids", (payload or {}).get("record_ids"))
        if not isinstance(ids, list):
            return self._fail("ids 必须是数组", 400)
        try:
            return self._ok(self.plugin.delete_records(ids), message="资产已删除")
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_asset_studio(self, record_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(self.plugin.studio_add_asset(record_id, payload or {}), message="资产已加入画布")
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_assets_studio(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        ids = (payload or {}).get("ids", (payload or {}).get("record_ids"))
        if not isinstance(ids, list):
            return self._fail("ids 必须是数组", 400)
        try:
            return self._ok(self.plugin.studio_add_assets(ids, payload or {}), message="资产已加入画布")
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_records_clear(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._ok({"deleted": self.plugin.clear_recent_records()})

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
        if not info.get("is_image") and not info.get("is_video"):
            return self._fail("缓存文件不是有效图片或视频", 400)
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
            return self._fail("图片已清理", 404)
        if not info.get("is_image") and not info.get("is_video"):
            return self._fail("缓存文件不是有效图片或视频", 400)
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

    async def page_studio_list(self) -> Any:
        return self._ok(self.plugin.studio_list())

    async def page_studio_create(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(self.plugin.studio_create(payload or {}))
        except Exception as exc:
            return self._fail(str(exc))

    async def page_studio_get(self, session_id: str) -> Any:
        try:
            return self._ok(self.plugin.studio_get(session_id))
        except Exception as exc:
            return self._fail(str(exc), 404)

    async def page_studio_update(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(self.plugin.studio_update(session_id, payload or {}))
        except Exception as exc:
            return self._fail(str(exc))

    async def page_studio_delete(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(self.plugin.studio_delete(session_id))
        except Exception as exc:
            return self._fail(str(exc), 404)

    async def page_studio_set_slot(self, session_id: str, slot_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(self.plugin.studio_set_slot(session_id, slot_id, payload or {}))
        except Exception as exc:
            return self._fail(str(exc))

    async def page_studio_add_slot(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(self.plugin.studio_add_slot(session_id, payload or {}))
        except Exception as exc:
            return self._fail(str(exc))

    async def page_studio_reorder(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(self.plugin.studio_reorder(session_id, payload or {}))
        except Exception as exc:
            return self._fail(str(exc))

    async def page_studio_promote(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(self.plugin.studio_promote(session_id, payload or {}))
        except Exception as exc:
            return self._fail(str(exc))

    async def page_studio_run(self, session_id: str) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(redact_sensitive_data(self.plugin.start_studio_run(session_id, payload or {})))
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_studio_task(self, task_id: str) -> Any:
        try:
            task_id_text = validate_task_id(task_id, WEB_TASK_ID_RE, max_length=MAX_WEB_TASK_ID_LENGTH)
        except WebContractError as exc:
            return self._fail(str(exc), exc.status_code)
        try:
            return self._ok(redact_sensitive_data(self.plugin.get_web_image_task(task_id_text)))
        except Exception as exc:
            return self._fail(str(exc), 404)

    async def page_studio_gallery(self) -> Any:
        try:
            limit = int(self._query_value("limit") or 24)
        except Exception:
            limit = 24
        try:
            return self._ok(self.plugin.studio_gallery_images(limit=limit))
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_prompt_presets(self) -> Any:
        try:
            kind = self._query_value("kind") or self._query_value("media_type") or "image"
            data = self.plugin.list_prompt_presets_for_web(kind)
            status_getter = getattr(self.plugin, "get_prompt_preset_status_for_web", None)
            status = status_getter(kind) if callable(status_getter) else {"ok": True, "source": "legacy", "error": ""}
            return self._ok(data, count=len(data), load_status=status)
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_prompt_presets_manage(self) -> Any:
        try:
            kind = self._query_value("kind") or self._query_value("media_type") or "image"
            data = self.plugin.list_managed_prompt_presets_for_web(kind)
            status_getter = getattr(self.plugin, "get_prompt_preset_status_for_web", None)
            status = status_getter(kind) if callable(status_getter) else {"ok": True, "source": "legacy", "error": ""}
            return self._ok(data, load_status=status)
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_prompt_preset_save(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(self.plugin.save_prompt_preset_from_web(payload or {}))
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_prompt_preset_delete(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            name = str((payload or {}).get("name") or "").strip()
            if not name:
                return self._fail("缺少预设名", 400)
            kind = str((payload or {}).get("kind") or (payload or {}).get("media_type") or "image")
            return self._ok(self.plugin.delete_prompt_preset_from_web(name, kind))
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_prompt_presets_import(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(self.plugin.import_prompt_presets_from_web(payload or {}))
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_cos_look_sets(self) -> Any:
        try:
            data = self.plugin.list_cos_look_sets_for_web()
            return self._ok(data, count=len(data))
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_cos_pools(self) -> Any:
        try:
            return self._ok(self.plugin.list_cos_pools_for_web())
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_cos_pool_favorite(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            look_id = str((payload or {}).get("id") or (payload or {}).get("look_id") or "").strip()
            enabled = (payload or {}).get("enabled", (payload or {}).get("favorite", True))
            enabled = enabled if isinstance(enabled, bool) else str(enabled).lower() in {"1", "true", "yes", "on", "是", "开启"}
            return self._ok(self.plugin.set_cos_favorite_for_web(look_id, enabled))
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_cos_custom_save(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(self.plugin.save_custom_cos_for_web(payload or {}), message="自定义 COS 已保存")
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_cos_custom_delete(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            look_id = str((payload or {}).get("id") or (payload or {}).get("look_id") or "").strip()
            return self._ok(self.plugin.delete_custom_cos_for_web(look_id), message="自定义 COS 已删除")
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_cos_pool_export(self) -> Any:
        try:
            return self._ok(self.plugin.export_cos_pool_for_web())
        except Exception as exc:
            return self._fail(str(exc), 500)

    async def page_cos_pool_import(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            return self._ok(self.plugin.import_cos_pool_for_web(payload or {}), message="COS 池已导入")
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_creative_template_render(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            prompt = str((payload or {}).get("prompt") or (payload or {}).get("template") or "")
            return self._ok(self.plugin.render_creative_prompt(prompt, payload or {}))
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_creative_variations(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            prompt = str((payload or {}).get("prompt") or "")
            count = int((payload or {}).get("count") or 1)
            return self._ok({"variations": self.build_creative_variations(prompt, count, payload or {})})
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_creative_storyboard(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        try:
            prompt = str((payload or {}).get("prompt") or (payload or {}).get("text") or "")
            return self._ok(self.parse_storyboard_for_web(prompt))
        except Exception as exc:
            return self._fail(str(exc), 400)
