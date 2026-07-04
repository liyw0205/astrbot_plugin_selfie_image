"""Generation orchestration with retry and fallback."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import aiohttp

from .models import ImageModelTarget
from .providers import ImageGenerateRequest, ImageGenerateResult, create_adapter
from .utils import redact_sensitive_data, redact_sensitive_text


IMAGE_RETRY_ATTEMPTS = 3


def _target_attempt_base(target: ImageModelTarget, attempt: int) -> Dict[str, Any]:
    return {
        "attempt": attempt,
        "channel": target.channel_name,
        "provider_type": target.provider_type,
        "model": target.model,
        "label": target.label,
        "timeout_seconds": target.timeout,
    }


async def generate_image_with_fallback(
    targets: List[ImageModelTarget],
    req: ImageGenerateRequest,
    session: aiohttp.ClientSession,
    max_attempts: Optional[int] = None,
) -> ImageGenerateResult:
    if not targets:
        return ImageGenerateResult(error="未配置生图模型")

    global_timeout = max(10, int(targets[0].timeout or 180))
    deadline = time.monotonic() + global_timeout
    last_error = "未配置生图模型"
    total_attempts = max(1, int(max_attempts)) if max_attempts is not None else max(IMAGE_RETRY_ATTEMPTS, len(targets))
    attempts: List[Dict[str, Any]] = []

    for attempt in range(1, total_attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ImageGenerateResult(error=f"生图全局超时（{global_timeout}秒），最后错误: {last_error}", attempts=attempts)

        target = targets[(attempt - 1) % len(targets)]
        label = target.label
        adapter = create_adapter(target, session)
        attempt_info = _target_attempt_base(target, attempt)
        started = time.monotonic()

        try:
            result = await asyncio.wait_for(adapter.generate(req), timeout=max(1, min(target.timeout, int(remaining))))
            attempt_info["elapsed_seconds"] = round(time.monotonic() - started, 2)
            if result.images and not result.error:
                attempt_info["success"] = True
                attempt_info["image_count"] = len(result.images)
                attempts.append(attempt_info)
                result.used_model = label
                result.attempts = redact_sensitive_data([*attempts, *result.attempts])
                return result
            error_text = redact_sensitive_text(result.error or "生成失败")
            last_error = f"{label}: {error_text}"
            attempt_info["success"] = False
            attempt_info["error"] = error_text
            attempt_info["image_count"] = len(result.images or [])
            attempts.append(attempt_info)
        except asyncio.TimeoutError:
            last_error = f"{label}: 请求超时"
            attempt_info["success"] = False
            attempt_info["error"] = "请求超时"
            attempt_info["elapsed_seconds"] = round(time.monotonic() - started, 2)
            attempts.append(attempt_info)
        except Exception as exc:
            error_text = redact_sensitive_text(str(exc))
            last_error = f"{label}: {error_text}"
            attempt_info["success"] = False
            attempt_info["error"] = error_text
            attempt_info["elapsed_seconds"] = round(time.monotonic() - started, 2)
            attempts.append(attempt_info)

        if attempt < total_attempts:
            wait_seconds = attempt
            if deadline - time.monotonic() <= wait_seconds:
                return ImageGenerateResult(error=f"生图全局超时（{global_timeout}秒），最后错误: {last_error}", attempts=attempts)
            await asyncio.sleep(wait_seconds)

    return ImageGenerateResult(error=last_error, attempts=attempts)
