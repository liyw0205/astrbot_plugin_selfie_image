"""One-pass model fallback. No same-label re-POST. No loop-blocking waits."""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any, Dict, List, Optional

import aiohttp

from .error_classify import classify_generation_error, format_timeout_user_message
from .models import ImageModelTarget
from .proxy import LOCAL_IMAGE_WAIT_SECONDS, channel_client_session, target_session_proxy
from .providers import ImageGenerateRequest, ImageGenerateResult, create_adapter
from .utils import redact_sensitive_data, redact_sensitive_text


def _target_attempt_base(target: ImageModelTarget, attempt: int, key_index: int = 0, *, multi_key: bool = False) -> Dict[str, Any]:
    info = {
        "attempt": attempt,
        "channel": target.channel_name,
        "provider_type": target.provider_type,
        "model": target.model,
        "label": target.label,
        "timeout_seconds": target.timeout,
    }
    if multi_key:
        info["key_index"] = key_index + 1
    return info


def _target_with_api_key(target: ImageModelTarget, api_key: str) -> ImageModelTarget:
    cloned = copy.deepcopy(target)
    cloned.api_key = str(api_key or "").strip()
    return cloned


async def _generate_with_target_proxy(
    target: ImageModelTarget,
    fallback_session: aiohttp.ClientSession,
    request: ImageGenerateRequest,
) -> ImageGenerateResult:
    async with channel_client_session(target.proxy, fallback_session) as target_session:
        adapter = create_adapter(target_session_proxy(target), target_session)
        return await adapter.generate(request)


def _should_rotate_api_key(class_info: Dict[str, Any]) -> bool:
    category = str(class_info.get("category") or "")
    status = class_info.get("http_status")
    if category in {"auth", "rate_limit"}:
        return True
    if status in {401, 403, 429}:
        return True
    return False


def _should_advance_to_next_target(class_info: Dict[str, Any]) -> bool:
    category = str(class_info.get("category") or "")
    if category in {"param", "not_found", "timeout", "network", "rate_limit", "auth", "unknown", "fatal", "safety"}:
        return True
    return bool(class_info.get("retryable", True))


async def _run_one_target(
    *,
    target: ImageModelTarget,
    session: aiohttp.ClientSession,
    req: ImageGenerateRequest,
    budget: int,
) -> ImageGenerateResult:
    work = asyncio.create_task(_generate_with_target_proxy(target, session, req))
    done, _ = await asyncio.wait({work}, timeout=max(1, int(budget)))
    if work not in done:
        work.cancel()
        return ImageGenerateResult(error=format_timeout_user_message("local", budget))
    try:
        return work.result()
    except asyncio.CancelledError:
        return ImageGenerateResult(error=format_timeout_user_message("local", budget))
    except Exception as exc:
        return ImageGenerateResult(error=redact_sensitive_text(str(exc)))


async def generate_image_with_fallback(
    targets: List[ImageModelTarget],
    req: ImageGenerateRequest,
    session: aiohttp.ClientSession,
    max_attempts: Optional[int] = None,
    global_timeout: Optional[int] = None,
) -> ImageGenerateResult:
    if not targets:
        return ImageGenerateResult(error="未配置生图模型")

    chain_timeout = max(10, int(global_timeout or targets[0].timeout or 180))
    deadline = time.monotonic() + chain_timeout
    last_error = "未配置生图模型"
    attempts: List[Dict[str, Any]] = []
    limit = len(targets)
    if max_attempts is not None:
        limit = max(1, min(len(targets), int(max_attempts)))

    for index, target in enumerate(targets[:limit], start=1):
        remain = deadline - time.monotonic()
        if remain <= 1:
            return ImageGenerateResult(
                error=redact_sensitive_text(format_timeout_user_message("global", chain_timeout)),
                attempts=redact_sensitive_data(attempts),
            )

        label = redact_sensitive_text(target.label)
        api_keys = target.resolved_api_keys() if hasattr(target, "resolved_api_keys") else ([target.api_key] if target.api_key else [""])
        if not api_keys:
            api_keys = [""]

        for key_index, api_key in enumerate(api_keys):
            remain = deadline - time.monotonic()
            if remain <= 1:
                return ImageGenerateResult(
                    error=redact_sensitive_text(format_timeout_user_message("global", chain_timeout)),
                    attempts=redact_sensitive_data(attempts),
                )
            budget = max(1, min(LOCAL_IMAGE_WAIT_SECONDS, int(remain)))
            attempt_info = _target_attempt_base(target, index, key_index=key_index, multi_key=len(api_keys) > 1)
            started = time.monotonic()
            active = _target_with_api_key(target, api_key) if api_key else target
            result = await _run_one_target(target=active, session=session, req=req, budget=budget)
            wall = time.monotonic() - started
            if wall > budget + 1:
                result = ImageGenerateResult(error=format_timeout_user_message("local", budget))
            elapsed = round(min(wall, float(budget)), 2)
            attempt_info["elapsed_seconds"] = elapsed

            if result.images and not result.error:
                attempt_info["success"] = True
                attempt_info["image_count"] = len(result.images)
                attempts.append(attempt_info)
                result.used_model = label
                result.attempts = redact_sensitive_data([*attempts, *result.attempts])
                return result

            error_text = redact_sensitive_text(result.error or "生成失败")
            class_info = classify_generation_error(error_text)
            last_error = f"{label}: {class_info.get('user_message') or error_text}"
            attempt_info["success"] = False
            attempt_info["error"] = error_text
            attempt_info["error_user_message"] = str(class_info.get("user_message") or error_text)
            attempt_info["error_category"] = class_info.get("category")
            attempt_info["retryable"] = bool(class_info.get("retryable"))
            attempt_info["image_count"] = len(result.images or [])
            attempts.append(attempt_info)

            if _should_rotate_api_key(class_info) and key_index + 1 < len(api_keys):
                continue
            break

    return ImageGenerateResult(error=redact_sensitive_text(last_error), attempts=redact_sensitive_data(attempts))
