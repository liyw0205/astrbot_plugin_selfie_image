"""AstrBot Dashboard embedded page APIs for Selfie Image.

These routes are served through AstrBot Dashboard login state via
``context.register_web_api``. They intentionally do not require the standalone
Flask Web Token.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Optional

from .constants import PLUGIN_NAME
from .utils import redact_sensitive_data, redact_sensitive_text
from .web import (
    MAX_CACHE_IMAGE_PATH_LENGTH,
    MAX_RECORD_PAGE_LIMIT,
    MAX_WEB_RECORD_ID_LENGTH,
    MAX_WEB_TASK_ID_LENGTH,
    WEB_TASK_ID_RE,
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
    """Register and handle Dashboard plugin-page APIs without Web Token."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def register(self) -> None:
        context = getattr(self.plugin, "context", None)
        register_web_api = getattr(context, "register_web_api", None)
        if not callable(register_web_api):
            return

        routes = [
            ("health", self.page_health, ["GET"], "Selfie Image health"),
            ("config", self.page_config_get, ["GET"], "Selfie Image get config"),
            ("config", self.page_config_post, ["POST"], "Selfie Image save config"),
            ("selfie-reference", self.page_selfie_reference_get, ["GET"], "Selfie Image get reference"),
            ("selfie-reference", self.page_selfie_reference_post, ["POST"], "Selfie Image save reference"),
            ("selfie-reference/clear", self.page_selfie_reference_clear, ["POST"], "Selfie Image clear reference"),
            ("selfie-profile/refresh", self.page_selfie_profile_refresh, ["POST"], "Selfie Image refresh profile"),
            ("test-image-channel", self.page_test_image_channel, ["POST"], "Selfie Image sync channel test"),
            ("test-image-channel/tasks", self.page_test_image_task_start, ["POST"], "Selfie Image start channel test task"),
            (
                "test-image-channel/tasks/<task_id>",
                self.page_test_image_task_status,
                ["GET"],
                "Selfie Image channel test task status",
            ),
            ("test-video-channel/tasks", self.page_test_video_task_start, ["POST"], "Selfie Image start video channel test"),
            (
                "test-video-channel/tasks/<task_id>",
                self.page_test_image_task_status,
                ["GET"],
                "Selfie Image video channel test task status",
            ),
            ("refresh-image-models", self.page_refresh_image_models, ["POST"], "Selfie Image refresh models"),
            ("records", self.page_records, ["GET"], "Selfie Image generation records"),
            ("records/<record_id>", self.page_record_detail, ["GET"], "Selfie Image record detail"),
            ("records/clear", self.page_records_clear, ["POST"], "Selfie Image clear records"),
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
        ]
        for route, handler, methods, desc in routes:
            # Match telegram forwarder: bridge strips "/api/" then hits
            # /api/v1/plugins/extensions/<plugin>/<endpoint>
            # which resolves registered routes "/<plugin>/<endpoint>" and
            # "/<plugin>/page/<endpoint>".
            register_web_api(f"/{PLUGIN_NAME}/{route}", handler, methods, desc)
            register_web_api(f"/{PLUGIN_NAME}/page/{route}", handler, methods, desc)

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
        raw_value = self._query_value(name, "").strip()
        if not raw_value:
            return default, None
        try:
            value = int(raw_value)
        except ValueError:
            return None, self._fail(f"{name} 必须是整数", 400)
        if value < minimum:
            return None, self._fail(f"{name} 不能小于 {minimum}", 400)
        return min(value, maximum), None

    def _record_matches(self, record: Any, source: str, model: str, success: str, keyword: str, media_type: str = "") -> bool:
        if not isinstance(record, dict):
            return False
        if media_type:
            record_type = str(record.get("media_type") or "image").strip().lower()
            if record_type != media_type:
                return False
        if source:
            source_text = " ".join(
                str(record.get(key) or "") for key in ("source_label", "source", "group_id", "user_id")
            ).lower()
            if source not in source_text:
                return False
        if model and model not in str(record.get("used_model") or "").lower():
            return False
        if success:
            expected = success in {"1", "true", "yes", "ok", "success", "succeeded", "成功"}
            if bool(record.get("success")) is not expected:
                return False
        if keyword:
            text = json.dumps(record, ensure_ascii=False, default=str).lower()
            if keyword not in text:
                return False
        return True

    def _filtered_records(self, records: list[Any]) -> tuple[Optional[list], Optional[dict], Any]:
        source = self._query_value("source").strip().lower()
        model = self._query_value("model").strip().lower()
        media_type = self._query_value("media_type").strip().lower()
        if media_type not in {"", "image", "video"}:
            return None, None, self._fail("media_type 必须是 image 或 video", 400)
        success = self._query_value("success").strip().lower()
        keyword = (self._query_value("q") or self._query_value("keyword")).strip().lower()
        if success and success not in {
            "1",
            "0",
            "true",
            "false",
            "yes",
            "no",
            "ok",
            "success",
            "succeeded",
            "failed",
            "失败",
            "成功",
        }:
            return None, None, self._fail("success 必须是 true 或 false", 400)

        offset_value, error = self._int_query("offset", 0, 0, 10000)
        if error or offset_value is None:
            return None, None, error
        default_limit = min(MAX_RECORD_PAGE_LIMIT, max(1, len(records) or 1))
        limit_value, error = self._int_query("limit", default_limit, 1, MAX_RECORD_PAGE_LIMIT)
        if error or limit_value is None:
            return None, None, error
        offset = int(offset_value)
        limit = int(limit_value)

        filtered = [
            record
            for record in records
            if self._record_matches(record, source, model, success, keyword, media_type)
        ]
        page = filtered[offset : offset + limit]
        meta = {
            "total": len(records),
            "filtered": len(filtered),
            "offset": offset,
            "limit": limit,
        }
        return page, meta, None

    async def page_auth_check(self) -> Any:
        return self._ok({"authorized": True, "source": "dashboard"})

    async def page_health(self) -> Any:
        plugin = self.plugin
        return self._ok(
            {
                "status": "ok",
                "source": "dashboard",
                "config_path": getattr(plugin, "config_path", ""),
                "records_path": getattr(plugin, "records_path", ""),
                "cache_dir": getattr(plugin, "generated_dir", ""),
                "cache_size_mb": round(float(plugin._cache_size_bytes()) / 1024 / 1024, 2),
                "cache_limit_mb": getattr(plugin.config, "image_cache_limit_mb", 100),
            }
        )

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
        task_id_text = str(task_id or "").strip()
        if len(task_id_text) > MAX_WEB_TASK_ID_LENGTH or not WEB_TASK_ID_RE.fullmatch(task_id_text):
            return self._fail("非法任务 ID", 400)
        try:
            return self._ok(redact_sensitive_data(self.plugin.get_web_image_task(task_id_text)))
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
        data = redact_sensitive_data(self.plugin.get_recent_records())
        page, meta, error = self._filtered_records(data if isinstance(data, list) else [])
        if error:
            return error
        assert page is not None and meta is not None
        return self._ok(page, **meta)

    async def page_record_detail(self, record_id: str) -> Any:
        record_id_text = str(record_id or "").strip()
        if not record_id_text or len(record_id_text) > MAX_WEB_RECORD_ID_LENGTH:
            return self._fail("非法记录 ID", 400)
        try:
            return self._ok(redact_sensitive_data(self.plugin.get_record_for_web(record_id_text)))
        except Exception as exc:
            return self._fail(str(exc), 404)

    async def page_records_clear(self) -> Any:
        payload, error = await self._json_object_payload()
        if error:
            return error
        return self._ok({"deleted": self.plugin.clear_recent_records()})

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
        task_id_text = str(task_id or "").strip()
        if len(task_id_text) > MAX_WEB_TASK_ID_LENGTH or not WEB_TASK_ID_RE.fullmatch(task_id_text):
            return self._fail("非法任务 ID", 400)
        try:
            return self._ok(redact_sensitive_data(self.plugin.get_web_image_task(task_id_text)))
        except Exception as exc:
            return self._fail(str(exc), 404)

    async def page_studio_gallery(self) -> Any:
        try:
            limit = int(request.args.get("limit") or 24)
        except Exception:
            limit = 24
        try:
            return self._ok(self.plugin.studio_gallery_images(limit=limit))
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def page_prompt_presets(self) -> Any:
        try:
            data = self.plugin.list_prompt_presets_for_web()
            return self._ok(data, count=len(data))
        except Exception as exc:
            return self._fail(str(exc), 500)
