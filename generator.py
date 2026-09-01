"""Isolated one-pass generation: each model runs in its own thread+loop.

The main event loop never awaits aiohttp. Timeouts fire even if another
shot is parsing a large body. Same label is never re-POSTed.
"""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any, Callable, Dict, List, Optional

import aiohttp

from .error_classify import classify_generation_error, format_timeout_user_message
from .models import ImageModelTarget
from .proxy import LOCAL_IMAGE_WAIT_SECONDS, channel_client_session, image_client_timeout, target_session_proxy
from .providers import ImageGenerateRequest, ImageGenerateResult, create_adapter
from .utils import redact_channel_attempts, redact_sensitive_text

SUCCESS = "success"
NEXT_KEY = "next_key"
NEXT_MODEL = "next_model"


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
    if category in {"param", "not_found", "timeout", "network", "server", "rate_limit", "auth", "unknown", "fatal", "safety"}:
        return True
    return bool(class_info.get("retryable", True))


def judge_attempt(result: ImageGenerateResult) -> str:
    """Pure decision: keep going to next key / next model, or accept success."""
    if result.images and not result.error:
        return SUCCESS
    info = classify_generation_error(result.error or "生成失败")
    if _should_rotate_api_key(info):
        return NEXT_KEY
    return NEXT_MODEL


def _sync_try_model(target: ImageModelTarget, req: ImageGenerateRequest, budget: int) -> ImageGenerateResult:
    """Run one model in a private loop so the bot loop cannot be blocked."""

    async def _go() -> ImageGenerateResult:
        timeout = image_client_timeout(budget)
        request_target = copy.deepcopy(target)
        request_target.timeout = int(budget)
        async with aiohttp.ClientSession(trust_env=False, timeout=timeout) as base:
            async with channel_client_session(getattr(request_target, "proxy", "") or "", base, budget) as owned:
                adapter = create_adapter(target_session_proxy(request_target), owned)
                return await adapter.generate(req)

    try:
        return asyncio.run(asyncio.wait_for(_go(), timeout=max(1, int(budget))))
    except asyncio.TimeoutError:
        return ImageGenerateResult(error=format_timeout_user_message("local", budget))
    except Exception as exc:
        return ImageGenerateResult(error=str(exc))


async def _try_model(
    target: ImageModelTarget,
    req: ImageGenerateRequest,
    budget: int,
    session: Optional[aiohttp.ClientSession],
) -> ImageGenerateResult:
    if session is None:
        work = asyncio.create_task(asyncio.to_thread(_sync_try_model, target, req, budget))
        done, _ = await asyncio.wait({work}, timeout=max(1, int(budget)))
        if work not in done:
            work.cancel()
            return ImageGenerateResult(error=format_timeout_user_message("local", budget))
        try:
            return work.result()
        except Exception as exc:
            return ImageGenerateResult(error=str(exc))
    work = asyncio.create_task(_try_on_session(target, req, session, budget))
    done, _ = await asyncio.wait({work}, timeout=max(1, int(budget)))
    if work not in done:
        work.cancel()
        return ImageGenerateResult(error=format_timeout_user_message("local", budget))
    try:
        return work.result()
    except Exception as exc:
        return ImageGenerateResult(error=str(exc))


async def _try_on_session(
    target: ImageModelTarget,
    req: ImageGenerateRequest,
    session: aiohttp.ClientSession,
    budget: int,
) -> ImageGenerateResult:
    request_target = copy.deepcopy(target)
    request_target.timeout = int(budget)
    async with channel_client_session(getattr(request_target, "proxy", "") or "", session, budget) as owned:
        adapter = create_adapter(target_session_proxy(request_target), owned)
        return await adapter.generate(req)


async def generate_image_with_fallback(
    targets: List[ImageModelTarget],
    req: ImageGenerateRequest,
    session: Optional[aiohttp.ClientSession] = None,
    max_attempts: Optional[int] = None,
    global_timeout: Optional[int] = None,
    request_factory: Optional[Callable[[ImageModelTarget], ImageGenerateRequest]] = None,
) -> ImageGenerateResult:
    if not targets:
        return ImageGenerateResult(error="未配置生图模型")

    chain_timeout = max(10, int(global_timeout or getattr(targets[0], "timeout", None) or 180))
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
                attempts=redact_channel_attempts(attempts),
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
                    attempts=redact_channel_attempts(attempts),
                )
            # The global deadline limits the whole fallback chain. A single
            # image-model request is hard-capped at 180s even if a caller
            # supplies a target that still carries the global timeout.
            target_timeout = max(
                1,
                min(LOCAL_IMAGE_WAIT_SECONDS, int(getattr(target, "timeout", 0) or LOCAL_IMAGE_WAIT_SECONDS)),
            )
            budget = max(1, min(target_timeout, int(remain)))
            attempt_info = _target_attempt_base(target, index, key_index=key_index, multi_key=len(api_keys) > 1)
            attempt_info["timeout_seconds"] = budget
            started = time.monotonic()
            active = _target_with_api_key(target, api_key) if api_key else target
            active_req = request_factory(target) if request_factory is not None else req
            result = await _try_model(active, active_req, budget, session)
            wall = time.monotonic() - started
            elapsed = round(min(wall, float(budget)), 2)
            attempt_info["elapsed_seconds"] = elapsed

            decision = judge_attempt(result)
            if decision == SUCCESS:
                attempt_info["success"] = True
                attempt_info["image_count"] = len(result.images)
                attempts.append(attempt_info)
                result.used_model = label
                result.attempts = redact_channel_attempts([*attempts, *getattr(result, "attempts", [])])
                return result

            raw_error = str(result.error or "生成失败")
            safe_error = redact_sensitive_text(raw_error)
            class_info = classify_generation_error(safe_error)
            last_error = f"{label}: {class_info.get('user_message') or safe_error}"
            attempt_info["success"] = False
            attempt_info["error"] = raw_error
            attempt_info["error_user_message"] = str(class_info.get("user_message") or safe_error)
            attempt_info["error_category"] = class_info.get("category")
            attempt_info["retryable"] = bool(class_info.get("retryable"))
            attempt_info["image_count"] = len(result.images or [])
            attempts.append(attempt_info)

            if decision == NEXT_KEY and key_index + 1 < len(api_keys):
                continue
            break

    if time.monotonic() >= deadline:
        return ImageGenerateResult(
            error=redact_sensitive_text(format_timeout_user_message("global", chain_timeout)),
            attempts=redact_channel_attempts(attempts),
        )
    return ImageGenerateResult(error=redact_sensitive_text(last_error), attempts=redact_channel_attempts(attempts))
