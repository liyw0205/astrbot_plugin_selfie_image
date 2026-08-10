"""Generation orchestration with retry and fallback.

Retry policy (targets 09/10/12):
- Billable create POST timeouts / model-not-found / safety: do not resubmit blindly.
- Multi-target fallback may advance to the next channel/model on retryable errors.
- Auth/rate-limit can rotate to the next API key on the *same* target before giving up.
- If all keys for a target fail auth, stop with a single-cause auth error.
"""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any, Dict, List, Optional

import aiohttp

from .error_classify import classify_generation_error
from .models import ImageModelTarget
from .proxy import channel_client_session, target_session_proxy
from .providers import ImageGenerateRequest, ImageGenerateResult, create_adapter
from .utils import redact_sensitive_data, redact_sensitive_text


IMAGE_RETRY_ATTEMPTS = 3


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
    """Whether outer fallback may try another channel/model after this error.

    - auth / rate_limit: key rotation first; if keys exhausted, next target OK
    - param / not_found / safety: this model rejected — try next configured model
    - timeout: skip this label for re-POST, but other targets OK
    """
    category = str(class_info.get("category") or "")
    # Safety is model/channel-specific in practice (Grok vs GPT filters differ).
    # Skip this label and continue priority list instead of aborting the whole job.
    if category in {"param", "not_found", "timeout", "network", "rate_limit", "auth", "unknown", "fatal", "safety"}:
        return True
    return bool(class_info.get("retryable", True))


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
    # Avoid immediately re-POSTing the same target after create timeout / exhausted param on that label.
    skip_labels: set[str] = set()

    for attempt in range(1, total_attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ImageGenerateResult(
                error=redact_sensitive_text(f"生图全局超时（{global_timeout}秒），最后错误: {last_error}"),
                attempts=redact_sensitive_data(attempts),
            )

        target = None
        for offset in range(len(targets)):
            candidate = targets[(attempt - 1 + offset) % len(targets)]
            if candidate.label not in skip_labels or len(skip_labels) >= len(targets):
                target = candidate
                break
        if target is None:
            target = targets[(attempt - 1) % len(targets)]

        label = redact_sensitive_text(target.label)
        api_keys = target.resolved_api_keys() if hasattr(target, "resolved_api_keys") else ([target.api_key] if target.api_key else [""])
        if not api_keys:
            api_keys = [""]

        stop_all = False
        for key_index, api_key in enumerate(api_keys):
            active_target = _target_with_api_key(target, api_key) if api_key else target
            attempt_info = _target_attempt_base(target, attempt, key_index=key_index, multi_key=len(api_keys) > 1)
            started = time.monotonic()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ImageGenerateResult(
                    error=redact_sensitive_text(f"生图全局超时（{global_timeout}秒），最后错误: {last_error}"),
                    attempts=redact_sensitive_data(attempts),
                )
            # Reserve budget for later channels so one slow timeout cannot burn the whole global window.
            remaining_targets = max(0, len([t for t in targets if t.label not in skip_labels]) - 1)
            reserve = min(45, int(remaining // 3)) if remaining_targets > 0 else 0
            per_try = max(1, min(target.timeout, int(remaining) - reserve))
            if per_try < 15 and remaining_targets > 0 and remaining > 20:
                # Still give a short try, but leave at least ~15s for next model.
                per_try = max(15, int(remaining) - 15)
            try:
                result = await asyncio.wait_for(
                    _generate_with_target_proxy(active_target, session, req),
                    timeout=per_try,
                )
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
                attempt_info["error_user_message"] = str(class_info.get("user_message") or error_text)
                attempt_info["error_category"] = class_info.get("category")
                attempt_info["retryable"] = bool(class_info.get("retryable"))
                attempt_info["image_count"] = len(result.images or [])
                attempts.append(attempt_info)

                # Rotate to next key on auth/rate-limit when more keys remain.
                if _should_rotate_api_key(class_info) and key_index + 1 < len(api_keys):
                    continue

                # This target is done for this error class: skip re-POST same label.
                if class_info.get("category") in {"param", "not_found", "timeout", "auth"} or not class_info.get(
                    "retryable", True
                ):
                    skip_labels.add(target.label)

                # Safety: skip this model and continue priority list.
                if class_info.get("category") == "safety":
                    skip_labels.add(target.label)
                    break

                # Auth exhausted all keys on this target — try next target if any.
                if class_info.get("category") == "auth" and key_index + 1 >= len(api_keys):
                    last_error = f"{label}: 渠道鉴权失败，请检查 API Key 或权限"
                    if len(targets) <= 1 or len(skip_labels) >= len(targets):
                        return ImageGenerateResult(
                            error=redact_sensitive_text(last_error),
                            attempts=redact_sensitive_data(attempts),
                        )
                    break

                # Param / not_found on this model: advance to another channel/model instead of ending the job.
                if _should_advance_to_next_target(class_info):
                    break

                # Non-retryable and not advanceable
                return ImageGenerateResult(
                    error=redact_sensitive_text(last_error),
                    attempts=redact_sensitive_data(attempts),
                )
            except asyncio.TimeoutError:
                last_error = f"{label}: 请求超时（为避免重复扣费，不会自动重提同一请求）"
                attempt_info["success"] = False
                attempt_info["error"] = "请求超时"
                attempt_info["error_user_message"] = "生图请求超时（为避免重复扣费，不会自动重提同一请求）"
                attempt_info["error_category"] = "timeout"
                attempt_info["retryable"] = False
                attempt_info["elapsed_seconds"] = round(time.monotonic() - started, 2)
                attempts.append(attempt_info)
                skip_labels.add(target.label)
                if len(targets) <= 1 or len(skip_labels) >= len(targets):
                    stop_all = True
                break
            except Exception as exc:
                error_text = redact_sensitive_text(str(exc))
                class_info = classify_generation_error(error_text)
                last_error = f"{label}: {class_info.get('user_message') or error_text}"
                attempt_info["success"] = False
                attempt_info["error"] = error_text
                attempt_info["error_user_message"] = str(class_info.get("user_message") or error_text)
                attempt_info["error_category"] = class_info.get("category")
                attempt_info["retryable"] = bool(class_info.get("retryable"))
                attempt_info["elapsed_seconds"] = round(time.monotonic() - started, 2)
                attempts.append(attempt_info)
                if _should_rotate_api_key(class_info) and key_index + 1 < len(api_keys):
                    continue
                if class_info.get("category") in {"param", "not_found", "timeout", "auth"} or not class_info.get(
                    "retryable", True
                ):
                    skip_labels.add(target.label)
                if class_info.get("category") == "safety":
                    skip_labels.add(target.label)
                    break
                if _should_advance_to_next_target(class_info):
                    break
                return ImageGenerateResult(
                    error=redact_sensitive_text(last_error),
                    attempts=redact_sensitive_data(attempts),
                )

        if stop_all:
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
