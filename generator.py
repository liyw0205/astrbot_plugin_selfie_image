"""Generation orchestration with retry and fallback.

Retry policy (targets 09/10, inspired by general image-gen + starmiao GPT Image):
- Billable create POST timeouts / auth / model-not-found / safety: do not resubmit blindly.
- Multi-target fallback may still advance to the *next* channel/model on retryable errors only.
- Non-retryable errors stop the whole attempt loop immediately.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import aiohttp

from .error_classify import classify_generation_error
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
    # Avoid immediately re-POSTing the same target after a create timeout / non-retryable miss.
    skip_labels: set[str] = set()

    for attempt in range(1, total_attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ImageGenerateResult(
                error=redact_sensitive_text(f"生图全局超时（{global_timeout}秒），最后错误: {last_error}"),
                attempts=redact_sensitive_data(attempts),
            )

        # Prefer next unused target when some labels are skipped.
        target = None
        for offset in range(len(targets)):
            candidate = targets[(attempt - 1 + offset) % len(targets)]
            if candidate.label not in skip_labels or len(skip_labels) >= len(targets):
                target = candidate
                break
        if target is None:
            target = targets[(attempt - 1) % len(targets)]

        label = redact_sensitive_text(target.label)
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
            class_info = classify_generation_error(error_text)
            last_error = f"{label}: {class_info.get('user_message') or error_text}"
            attempt_info["success"] = False
            attempt_info["error"] = error_text
            attempt_info["error_category"] = class_info.get("category")
            attempt_info["retryable"] = bool(class_info.get("retryable"))
            attempt_info["image_count"] = len(result.images or [])
            attempts.append(attempt_info)
            if not class_info.get("retryable", True):
                # Do not burn more attempts on auth / not_found / safety / create-timeout class.
                return ImageGenerateResult(
                    error=redact_sensitive_text(last_error),
                    attempts=redact_sensitive_data(attempts),
                )
        except asyncio.TimeoutError:
            # Billable create may already have succeeded upstream; do not auto-resubmit.
            last_error = f"{label}: 请求超时（为避免重复扣费，不会自动重提同一请求）"
            attempt_info["success"] = False
            attempt_info["error"] = "请求超时"
            attempt_info["error_category"] = "timeout"
            attempt_info["retryable"] = False
            attempt_info["elapsed_seconds"] = round(time.monotonic() - started, 2)
            attempts.append(attempt_info)
            skip_labels.add(target.label)
            if len(targets) <= 1 or len(skip_labels) >= len(targets):
                return ImageGenerateResult(
                    error=redact_sensitive_text(last_error),
                    attempts=redact_sensitive_data(attempts),
                )
        except Exception as exc:
            error_text = redact_sensitive_text(str(exc))
            class_info = classify_generation_error(error_text)
            last_error = f"{label}: {class_info.get('user_message') or error_text}"
            attempt_info["success"] = False
            attempt_info["error"] = error_text
            attempt_info["error_category"] = class_info.get("category")
            attempt_info["retryable"] = bool(class_info.get("retryable"))
            attempt_info["elapsed_seconds"] = round(time.monotonic() - started, 2)
            attempts.append(attempt_info)
            if not class_info.get("retryable", True):
                return ImageGenerateResult(
                    error=redact_sensitive_text(last_error),
                    attempts=redact_sensitive_data(attempts),
                )

        if attempt < total_attempts:
            wait_seconds = min(attempt, 2)
            if deadline - time.monotonic() <= wait_seconds:
                return ImageGenerateResult(
                    error=redact_sensitive_text(f"生图全局超时（{global_timeout}秒），最后错误: {last_error}"),
                    attempts=redact_sensitive_data(attempts),
                )
            await asyncio.sleep(wait_seconds)

    return ImageGenerateResult(error=redact_sensitive_text(last_error), attempts=redact_sensitive_data(attempts))
