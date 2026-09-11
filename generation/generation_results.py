"""Batch result normalization and deterministic progress text helpers."""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, Tuple


_COMPLETION_PATH_KEYS = (
    "files",
    "image_paths",
    "generated_image_paths",
    "video_path",
    "generated_video_paths",
)


def result_has_completion_evidence(result: Any) -> bool:
    """Return whether a result contains proof that generation reached an outcome.

    Cancellation is cooperative, so a late request must not erase a generated
    artifact or a transport outcome.  Keep this predicate deliberately small
    and shared by every task runner instead of relying on one runner's status
    naming conventions.
    """
    if not isinstance(result, dict):
        return False
    if bool(
        result.get("success")
        or result.get("generation_success")
        or result.get("delivery_failed")
        or result.get("delivery_unknown")
    ):
        return True
    status = str(result.get("status") or "").strip().lower()
    if status in {"succeeded", "partial_success", "delivery_failed"}:
        return True
    try:
        if int(result.get("succeeded_count") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    for key in _COMPLETION_PATH_KEYS:
        value = result.get(key)
        if isinstance(value, (list, tuple, set)) and any(str(item or "").strip() for item in value):
            return True
        if isinstance(value, str) and value.strip():
            return True
    return False


def resolve_cancel_request(result: Any, cancel_requested: bool) -> Tuple[Dict[str, Any], bool]:
    """Apply a cooperative cancellation request without discarding evidence.

    The boolean in the return value is true only when cancellation actually
    replaced a result that had no completion evidence.
    """
    data = copy.deepcopy(result) if isinstance(result, dict) else {"success": False, "error": "无效结果"}
    if not cancel_requested or result_has_completion_evidence(data):
        return data, False
    requested = data.get("requested_count") or data.get("batch_total") or 1
    cancelled = {
        **data,
        "success": False,
        "status": "cancelled",
        "cancelled": True,
        "generation_success": False,
        "delivery_success": None,
        "delivery_failed": False,
        "delivery_unknown": False,
        "error": "任务已取消",
        "requested_count": requested,
    }
    return cancelled, True


def build_task_terminal_state(
    result: Any,
    *,
    requested_count: int = 1,
    cancel_requested: bool = False,
) -> Dict[str, Any]:
    """Normalize and classify terminal state shared by all task runners."""
    data = normalize_generation_result(result, requested_count)
    data, cancelled_result = resolve_cancel_request(data, cancel_requested)
    success = bool(data.get("success"))
    cancelled = bool(data.get("cancelled")) or "取消" in str(data.get("error") or "")
    # A late artifact or transport outcome is stronger evidence than a stale
    # cancellation marker left by an upstream adapter.
    if result_has_completion_evidence(data) and not cancelled_result:
        cancelled = False
    generation_success = bool(data.get("generation_success"))
    delivery_unknown = bool(data.get("delivery_unknown"))
    delivery_failed = (
        bool(data.get("delivery_failed"))
        or (
            generation_success
            and data.get("delivery_success") is False
            and not delivery_unknown
        )
        or str(data.get("status") or "") == "delivery_failed"
    ) and not delivery_unknown
    if success and not generation_success:
        generation_success = True
    if delivery_failed:
        data.update(
            {
                "generation_success": generation_success,
                "delivery_success": False,
                "delivery_failed": True,
            }
        )
    try:
        succeeded_count = int(data.get("succeeded_count") or 0)
    except (TypeError, ValueError):
        succeeded_count = 0
    partial_success = str(data.get("status") or "") == "partial_success" or (
        not success and succeeded_count > 0
    )
    terminal_stage = (
        "cancelled"
        if cancelled and not success
        else "complete"
        if success or delivery_failed or partial_success
        else "failed"
    )
    terminal_status = (
        "cancelled"
        if cancelled and not success
        else "delivery_failed"
        if delivery_failed
        else str(data.get("status") or ("succeeded" if success else "failed"))
    )
    return {
        "result": data,
        "cancelled_result": cancelled_result,
        "success": success,
        "cancelled": cancelled,
        "generation_success": generation_success,
        "delivery_unknown": delivery_unknown,
        "delivery_failed": delivery_failed,
        "partial_success": partial_success,
        "terminal_stage": terminal_stage,
        "terminal_status": terminal_status,
        "error": "" if success else str(data.get("error") or ("任务已取消" if cancelled else "这次没顺好")),
    }


def batch_success_text(info: str, index: int, total: int) -> str:
    text = str(info or "").strip()
    if not text:
        return ""
    if total > 1:
        return f"第 {index}/{total} 次请求完成。\n{text}"
    return text


def batch_failure_policy(config: Any) -> Tuple[str, int]:
    """Return ``(mode, skip_max)`` for a batch failure policy."""
    mode = str(getattr(config, "image_batch_on_failure", "skip") or "skip").strip().lower()
    if mode in {"continue", "skip_continue", "skip-continue"}:
        mode = "skip"
    if mode not in {"stop", "skip", "skip_max"}:
        mode = "skip"
    try:
        skip_max = int(getattr(config, "image_batch_skip_max", 2) or 2)
    except Exception:
        skip_max = 2
    return mode, max(0, min(8, skip_max))


def normalize_generation_result(result: Any, requested_count: int = 1) -> Dict[str, Any]:
    """Add stable counts/status while accepting legacy result dictionaries."""
    data = copy.deepcopy(result) if isinstance(result, dict) else {"success": False, "error": "无效结果"}
    files = list(data.get("files") or data.get("image_paths") or data.get("generated_image_paths") or [])
    try:
        requested = max(1, int(data.get("batch_total") or requested_count or 1))
    except (TypeError, ValueError):
        requested = 1
    try:
        succeeded = max(0, min(requested, int(data.get("succeeded_count") or len(files))))
    except (TypeError, ValueError):
        succeeded = min(requested, len(files))
    try:
        failed = max(0, int(data.get("failed_count") or data.get("batch_skipped") or 0))
    except (TypeError, ValueError):
        failed = 0
    if not succeeded and bool(data.get("success")):
        succeeded = requested
    if succeeded + failed > requested:
        failed = max(0, requested - succeeded)
    cancelled = bool(data.get("cancelled"))
    generation_success = data.get("generation_success")
    if generation_success is None:
        generation_success = succeeded > 0
    delivery_success = data.get("delivery_success")
    delivery_unknown = bool(data.get("delivery_unknown"))
    delivery_failed = (
        bool(data.get("delivery_failed")) or str(data.get("status") or "") == "delivery_failed"
    ) and not delivery_unknown
    if delivery_success is False and bool(generation_success) and not delivery_unknown:
        delivery_failed = True
    if cancelled:
        status = "cancelled"
    elif delivery_failed and bool(generation_success):
        status = "delivery_failed"
    elif succeeded >= requested and not failed:
        status = "succeeded"
    elif succeeded:
        status = "partial_success"
    else:
        status = "failed"
    try:
        completed = max(
            0,
            min(
                requested,
                int(data.get("completed_count") if "completed_count" in data else succeeded + failed),
            ),
        )
    except (TypeError, ValueError):
        completed = min(requested, succeeded + failed)
    try:
        progress_percent = max(
            0,
            min(
                100,
                int(
                    data.get("progress_percent")
                    if "progress_percent" in data
                    else round(completed * 100 / requested)
                ),
            ),
        )
    except (TypeError, ValueError):
        progress_percent = int(round(completed * 100 / requested))
    try:
        current_index = max(0, int(data.get("current_index") or completed))
    except (TypeError, ValueError):
        current_index = completed
    data.update(
        {
            "files": files,
            "requested_count": requested,
            "succeeded_count": succeeded,
            "failed_count": failed,
            "status": status,
            "success": status == "succeeded",
            "generation_success": bool(generation_success),
            "delivery_success": (
                False
                if delivery_failed
                else None
                if delivery_unknown
                else (True if delivery_success is None and generation_success else None if delivery_success is None else bool(delivery_success))
            ),
            "delivery_unknown": delivery_unknown,
            "delivery_failed": delivery_failed,
            "completed_count": completed,
            "progress_percent": progress_percent,
            "current_index": current_index,
        }
    )
    return data


def batch_failure_text(
    *,
    index: int,
    total: int,
    done_files: int,
    error: str,
    mode: str,
    skipped: int,
    skip_max: int,
    will_continue: bool,
) -> str:
    """Build the deterministic fallback line for one failed batch shot."""
    single_shot = total <= 1
    base = "这张没生成成功" if single_shot else f"第 {index}/{total} 张没出成"
    detail = str(error or "").strip()
    if detail:
        detail = re.sub(r"\s+", " ", detail)
        if len(detail) > 80:
            detail = detail[:79] + "…"
        base = f"{base}：{detail}"
    if single_shot:
        return base
    base = f"{base}。已出 {done_files} 张"
    if will_continue:
        if mode == "skip_max":
            return f"{base}，已跳过 {skipped}/{skip_max}，继续后面的"
        return f"{base}，继续后面的"
    left = max(0, total - index)
    if left:
        return f"{base}，后面 {left} 张先不跑了"
    return base
