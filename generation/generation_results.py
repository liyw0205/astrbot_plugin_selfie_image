"""Batch result normalization and deterministic progress text helpers."""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, Tuple


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
    if cancelled:
        status = "cancelled"
    elif succeeded >= requested and not failed:
        status = "succeeded"
    elif succeeded:
        status = "partial_success"
    else:
        status = "failed"
    data.update(
        {
            "files": files,
            "requested_count": requested,
            "succeeded_count": succeeded,
            "failed_count": failed,
            "status": status,
            "success": status == "succeeded",
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
