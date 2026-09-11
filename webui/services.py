"""Shared business/query contracts for the standalone and Dashboard Web APIs.

The two HTTP adapters deliberately keep different authentication and response
wrappers.  Query normalization and record/task selection, however, must have
one implementation so the adapters cannot silently drift.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Optional

from ..generation.generation_records import build_record_scope_stats


class WebContractError(ValueError):
    """A user-facing validation error with the HTTP status the adapters use."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = int(status_code or 400)


VALID_TASK_STATUSES = frozenset(
    {
        "queued",
        "running",
        "succeeded",
        "partial_success",
        "failed",
        "delivery_failed",
        "cancelled",
        "expired",
    }
)
VALID_SUCCESS_VALUES = frozenset(
    {
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
    }
)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "是", "开启"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "否", "关闭"})


def query_value(params: Mapping[str, Any], name: str, default: str = "") -> str:
    value = params.get(name, default)
    return default if value is None else str(value)


def parse_query_bool(value: Any) -> Optional[bool]:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return None
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    return None


def parse_bounded_int(
    params: Mapping[str, Any],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = query_value(params, name).strip()
    if not raw:
        return int(default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise WebContractError(f"{name} 必须是整数") from None
    if value < minimum:
        raise WebContractError(f"{name} 不能小于 {minimum}")
    if value > maximum:
        raise WebContractError(f"{name} 不能大于 {maximum}")
    return value


def parse_timestamp_query(
    params: Mapping[str, Any], name: str, *, end_of_day: bool = False
) -> Optional[float]:
    raw = query_value(params, name).strip()
    if not raw:
        return None
    try:
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            parsed = datetime.strptime(raw, "%Y-%m-%d")
            if end_of_day:
                parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
            return time.mktime(parsed.timetuple()) + parsed.microsecond / 1_000_000
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return time.mktime(parsed.timetuple()) + parsed.microsecond / 1_000_000
        return parsed.astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError):
        raise WebContractError(f"{name} 必须是 YYYY-MM-DD 或 ISO 时间") from None


def parse_task_query(params: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the shared ``GET /api/tasks`` query contract."""
    media_type = query_value(params, "media_type").strip().lower()
    if media_type not in {"", "image", "video"}:
        raise WebContractError("media_type 必须是 image 或 video")
    status_query = query_value(params, "status").strip().lower()
    if status_query:
        requested = {item.strip() for item in status_query.split(",") if item.strip()}
        if requested - VALID_TASK_STATUSES:
            raise WebContractError("status 包含不支持的任务状态")
    result: dict[str, Any] = {
        "include_finished": query_value(params, "include_finished").strip().lower()
        in _TRUE_VALUES,
        "limit": parse_bounded_int(params, "limit", 50, 1, 200),
        "offset": parse_bounded_int(params, "offset", 0, 0, 1_000_000),
        "media_type": media_type,
    }
    for key, value in (
        ("source", query_value(params, "source")),
        ("status", query_value(params, "status")),
        ("model", query_value(params, "model")),
        ("keyword", query_value(params, "q") or query_value(params, "keyword")),
    ):
        if value.strip():
            result[key] = value
    start_ts = parse_timestamp_query(params, "start_date")
    end_ts = parse_timestamp_query(params, "end_date", end_of_day=True)
    if start_ts is not None:
        result["start_ts"] = start_ts
    if end_ts is not None:
        result["end_ts"] = end_ts
    return result


def record_matches_query(
    record: Any,
    source: str,
    model: str,
    success: str,
    keyword: str,
    media_type: str = "",
) -> bool:
    if not isinstance(record, dict):
        return False
    if media_type and str(record.get("media_type") or "image").strip().lower() != media_type:
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
        text = " ".join(
            str(record.get(key) or "")
            for key in (
                "source",
                "source_label",
                "used_model",
                "error",
                "failure_reason",
                "original_prompt",
                "request_prompt",
                "group_id",
                "user_id",
            )
        ).lower()
        if keyword not in text:
            return False
    return True


def filter_record_page(records: list[Any], params: Mapping[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    """Filter and paginate records using the contract shared by both APIs."""
    source = query_value(params, "source").strip().lower()
    model = query_value(params, "model").strip().lower()
    media_type = query_value(params, "media_type").strip().lower()
    if media_type not in {"", "image", "video"}:
        raise WebContractError("media_type 必须是 image 或 video")
    success = query_value(params, "success").strip().lower()
    if success and success not in VALID_SUCCESS_VALUES:
        raise WebContractError("success 必须是 true 或 false")
    favorite = parse_query_bool(query_value(params, "favorite"))
    pinned = parse_query_bool(query_value(params, "pinned"))
    tag = query_value(params, "tag").strip().lower()
    keyword = (query_value(params, "q") or query_value(params, "keyword")).strip().lower()
    offset = parse_bounded_int(params, "offset", 0, 0, 10_000)
    default_limit = min(1_000, max(1, len(records)))
    limit = parse_bounded_int(params, "limit", default_limit, 1, 1_000)
    filtered = [
        record
        for record in records
        if record_matches_query(record, source, model, success, keyword, media_type)
        and (favorite is None or bool(record.get("favorite")) is favorite)
        and (pinned is None or bool(record.get("pinned")) is pinned)
        and (not tag or tag in {str(item).strip().lower() for item in (record.get("tags") or [])})
    ]
    page = filtered[offset : offset + limit]
    return page, {
        "total": len(records),
        "filtered": len(filtered),
        "offset": offset,
        "limit": limit,
        "scope_stats": build_record_scope_stats(filtered),
    }


def normalize_task_ids(
    payload: Any,
    task_id_pattern: re.Pattern[str],
    *,
    max_length: int = 64,
    max_count: int = 200,
) -> tuple[Optional[list[str]], Optional[str]]:
    raw = (payload or {}).get("ids", (payload or {}).get("task_ids")) if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return None, "ids 必须是数组"
    ids = list(dict.fromkeys(str(item or "").strip() for item in raw if str(item or "").strip()))
    if not ids:
        return None, "至少选择一条任务"
    if len(ids) > max_count:
        return None, f"单次最多操作 {max_count} 条任务"
    if any(len(task_id) > max_length or not task_id_pattern.fullmatch(task_id) for task_id in ids):
        return None, "包含非法任务 ID"
    return ids, None


def validate_task_id(task_id: Any, task_id_pattern: re.Pattern[str], *, max_length: int = 64) -> str:
    value = str(task_id or "").strip()
    if len(value) > max_length or not task_id_pattern.fullmatch(value):
        raise WebContractError("非法任务 ID")
    return value


def build_health_payload(plugin: Any, page_status: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Build the non-transport health payload used by both API adapters."""
    stats = getattr(plugin, "_cache_stats", None)
    cache_bytes, cache_count = stats() if callable(stats) else (plugin._cache_size_bytes(), 0)
    get_health = getattr(plugin, "get_channel_health", None)
    get_preview = getattr(plugin, "get_cache_cleanup_preview", None)
    return {
        "status": "ok",
        "config_path": getattr(plugin, "config_path", ""),
        "records_path": getattr(plugin, "records_path", ""),
        "records_db_path": getattr(plugin, "records_db_path", ""),
        "media_sources_dir": getattr(plugin, "media_sources_dir", ""),
        "cache_dir": getattr(plugin, "generated_dir", ""),
        "cache_size_mb": round(float(cache_bytes) / 1024 / 1024, 2),
        "cache_file_count": cache_count,
        "cache_limit_mb": getattr(plugin.config, "image_cache_limit_mb", 200),
        "cache_limit_count": getattr(plugin.config, "image_cache_limit_count", 100),
        "channel_health": get_health() if callable(get_health) else {},
        "cache_cleanup_preview": get_preview() if callable(get_preview) else {},
        "dashboard_page": dict(page_status or {}),
    }
