"""AstrBot plugin-page dashboard API helpers."""

from __future__ import annotations

import copy
import json
import os
import re
from typing import Any, Dict, List, Tuple

from .utils import bytes_to_data_url, redact_sensitive_data, redact_sensitive_text


WEB_TASK_ID_RE = re.compile(r"^web-\d{8,}-\d+$")
MAX_WEB_TASK_ID_LENGTH = 64
MAX_CACHE_IMAGE_PATH_LENGTH = 512
MAX_WEB_RECORD_ID_LENGTH = 128
MAX_RECORD_PAGE_LIMIT = 100
BRIDGE_PAYLOAD_WRAPPER_KEYS = ("payload", "body", "data", "params", "query")
DIRECT_PAYLOAD_KEYS = {
    "channel",
    "config",
    "image",
    "path",
    "record_id",
    "task_id",
    "base_url",
    "baseUrl",
    "api_key",
    "apiKey",
    "provider_type",
    "providerType",
}


class DashboardApiError(Exception):
    """User-facing API error with an HTTP-like status code."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _ensure_payload(payload: Any) -> Dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise DashboardApiError("请求体必须是 JSON 对象", 400)
    if any(key in payload for key in DIRECT_PAYLOAD_KEYS):
        return payload
    for key in BRIDGE_PAYLOAD_WRAPPER_KEYS:
        wrapped = payload.get(key)
        if isinstance(wrapped, dict):
            return wrapped
    return payload


def _int_arg(payload: Dict[str, Any], name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = str(payload.get(name, "") or "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise DashboardApiError(f"{name} 必须是整数", 400) from exc
    if value < minimum:
        raise DashboardApiError(f"{name} 不能小于 {minimum}", 400)
    return min(value, maximum)


def _record_matches_query(record: Any, source: str, model: str, success: str, keyword: str) -> bool:
    if not isinstance(record, dict):
        return False
    if source:
        source_text = " ".join(
            str(record.get(key) or "")
            for key in ("source_label", "source", "group_id", "user_id")
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


class SelfieImageDashboardApi:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def health(self) -> Dict[str, Any]:
        return {
            "status": "ok",
            "mode": "astrbot_plugin_page",
            "config_path": getattr(self.plugin, "config_path", ""),
            "records_path": getattr(self.plugin, "records_path", ""),
            "cache_dir": getattr(self.plugin, "generated_dir", ""),
            "cache_size_mb": round(float(self.plugin._cache_size_bytes()) / 1024 / 1024, 2),
            "cache_limit_mb": getattr(self.plugin.config, "image_cache_limit_mb", 100),
        }

    def get_config(self) -> Dict[str, Any]:
        return self.plugin.get_config_for_web()

    def save_config(self, payload: Any) -> Dict[str, Any]:
        payload = _ensure_payload(payload)
        if "config" in payload:
            if not isinstance(payload.get("config"), dict):
                raise DashboardApiError("config 必须是 JSON 对象", 400)
            patch = payload["config"]
        else:
            patch = payload
        return self.plugin.update_config_from_web(copy.deepcopy(patch))

    def get_selfie_reference(self) -> Dict[str, Any]:
        return self.plugin.get_selfie_reference_payload()

    def save_selfie_reference(self, payload: Any) -> Dict[str, Any]:
        return self.plugin.save_selfie_reference_from_web(_ensure_payload(payload))

    def clear_selfie_reference(self, payload: Any = None) -> Dict[str, Any]:
        _ensure_payload(payload)
        return self.plugin.clear_selfie_reference_from_web()

    async def refresh_selfie_profile(self, payload: Any = None) -> Dict[str, Any]:
        _ensure_payload(payload)
        return await self.plugin.refresh_selfie_profile_from_web()

    async def test_image_channel(self, payload: Any) -> Dict[str, Any]:
        return redact_sensitive_data(await self.plugin.web_test_image(_ensure_payload(payload)))

    def start_image_channel_task(self, payload: Any) -> Dict[str, Any]:
        return redact_sensitive_data(self.plugin.start_web_image_task(_ensure_payload(payload)))

    def get_image_channel_task(self, payload: Any) -> Dict[str, Any]:
        payload = _ensure_payload(payload)
        task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
        if len(task_id) > MAX_WEB_TASK_ID_LENGTH or not WEB_TASK_ID_RE.fullmatch(task_id):
            raise DashboardApiError("非法任务 ID", 400)
        try:
            return redact_sensitive_data(self.plugin.get_web_image_task(task_id))
        except Exception as exc:
            raise DashboardApiError(str(exc), 404) from exc

    async def refresh_image_models(self, payload: Any) -> Tuple[List[str], Dict[str, Any]]:
        data = await self.plugin.web_refresh_image_models(_ensure_payload(payload))
        return data, {"count": len(data)}

    def records(self, payload: Any = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        payload = _ensure_payload(payload)
        data = redact_sensitive_data(self.plugin.get_recent_records())
        source = str(payload.get("source") or "").strip().lower()
        model = str(payload.get("model") or "").strip().lower()
        success = str(payload.get("success") or "").strip().lower()
        keyword = str(payload.get("q") or payload.get("keyword") or "").strip().lower()
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
            raise DashboardApiError("success 必须是 true 或 false", 400)

        default_limit = min(MAX_RECORD_PAGE_LIMIT, len(data))
        offset = _int_arg(payload, "offset", 0, 0, 10000)
        limit = _int_arg(payload, "limit", default_limit, 1, MAX_RECORD_PAGE_LIMIT)
        filtered = [
            record
            for record in data
            if _record_matches_query(record, source, model, success, keyword)
        ]
        page = filtered[offset : offset + limit]
        return page, {
            "total": len(data),
            "filtered": len(filtered),
            "offset": offset,
            "limit": limit,
        }

    def record(self, payload: Any) -> Dict[str, Any]:
        payload = _ensure_payload(payload)
        record_id = str(payload.get("record_id") or payload.get("id") or "").strip()
        if not record_id or len(record_id) > MAX_WEB_RECORD_ID_LENGTH:
            raise DashboardApiError("非法记录 ID", 400)
        try:
            return redact_sensitive_data(self.plugin.get_record_for_web(record_id))
        except Exception as exc:
            raise DashboardApiError(str(exc), 404) from exc

    def clear_records(self, payload: Any = None) -> Dict[str, Any]:
        _ensure_payload(payload)
        return {"deleted": self.plugin.clear_recent_records()}

    def cache_image(self, payload: Any) -> Dict[str, Any]:
        payload = _ensure_payload(payload)
        rel_path = str(payload.get("path") or "").strip()
        if len(rel_path) > MAX_CACHE_IMAGE_PATH_LENGTH:
            raise DashboardApiError("图片路径过长", 400)
        try:
            info = self.plugin.get_cached_image_info(rel_path)
        except Exception as exc:
            raise DashboardApiError(str(exc), 400) from exc
        if not info.get("exists"):
            raise DashboardApiError("图片已清理", 404)
        if info.get("is_image") is False:
            raise DashboardApiError("缓存文件不是有效图片", 400)
        with open(info["absolute_path"], "rb") as handle:
            data = handle.read()
        mime_type = str(info.get("mime_type") or "image/png")
        return {
            "path": rel_path,
            "mime_type": mime_type,
            "image": bytes_to_data_url(data, mime_type),
        }

    def error_text(self, exc: Exception) -> str:
        return redact_sensitive_text(str(exc))
