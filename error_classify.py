"""Image generation error classification (non-retryable vs retryable).

Source: Railgun19457/astrbot_plugin_image_generation style error dictionaries;
Selfie target 09.
"""

from __future__ import annotations

import re
from typing import Any, Dict


# HTTP statuses that must not burn extra attempts (auth / not found / bad request class).
NON_RETRYABLE_HTTP_STATUSES = {400, 401, 403, 404, 422}

# 429 is rate-limit: retryable (possibly next key later).
# 408/409/425/429/5xx generally retryable except we treat pure client 4xx above as stop.

NON_RETRYABLE_PATTERNS = (
    r"\binvalid[_\s-]?api[_\s-]?key\b",
    r"\binvalid[_\s-]?token\b",
    r"\binvalid[_\s-]?authorization\b",
    r"\bunauthorized\b",
    r"\bauthentication\b",
    r"\bpermission\b",
    r"\bforbidden\b",
    r"\bmodel[_\s-]?not[_\s-]?found\b",
    r"\bno available channel for model\b",
    r"\bdoes not exist\b",
    r"\bunknown model\b",
    r"\bunsafe\b",
    r"\bcontent[_\s-]?policy\b",
    r"\bsafety\b",
    r"\bmoderation\b",
    r"\bresponsibleai\b",
    r"\bblocked\b",
    r"提示词审核未通过",
    r"图片内容审核未通过",
    r"未配置生图模型",
    r"未找到指定生图模型",
    r"已禁用",
    r"不存在",
)

# Parameter / schema issues: may switch payload profile once, but should not loop forever.
PARAM_ERROR_PATTERNS = (
    r"\binvalid[_\s-]?(?:param|parameter|request|value|json|body|field|size|aspect)\b",
    r"\bunsupported\b",
    r"\bunknown[_\s-]?parameter\b",
    r"\bextra[_\s-]?inputs[_\s-]?are[_\s-]?not[_\s-]?permitted\b",
    r"\bnot[_\s-]?supported\b",
    r"\bvalidation\b",
    r"\bschema\b",
    r"HTTP 400",
    r"HTTP 422",
)


def extract_http_status(error: str) -> int | None:
    text = str(error or "")
    match = re.search(r"\bHTTP\s+(\d{3})\b", text, flags=re.I)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def classify_generation_error(error: Any) -> Dict[str, Any]:
    """Classify a provider/generation error for retry policy.

    Returns:
        category: auth | not_found | safety | param | timeout | network | unknown
        retryable: whether outer fallback should spend another attempt on *this* error class
        user_message: single-cause user-facing text (already expects redaction upstream)
    """
    text = str(error or "").strip() or "生成失败"
    lowered = text.lower()
    status = extract_http_status(text)

    if status in NON_RETRYABLE_HTTP_STATUSES:
        if status in {401, 403}:
            category = "auth"
            user_message = "渠道鉴权失败，请检查 API Key 或权限"
        elif status == 404 or re.search(r"model[_\s-]?not[_\s-]?found|no available channel", lowered):
            category = "not_found"
            user_message = "模型不存在或当前分组不可用"
        elif re.search(r"unsafe|safety|moderation|content[_\s-]?policy|审核", lowered):
            category = "safety"
            user_message = "内容未通过上游安全策略"
        else:
            category = "param"
            user_message = "请求参数不被上游接受"
        return {
            "category": category,
            "retryable": False,
            "http_status": status,
            "user_message": user_message,
            "raw": text,
        }

    if re.search(r"timeout|超时|timed?\s*out", lowered):
        return {
            "category": "timeout",
            # Create-image POST timeout: do not blindly resubmit same billable job.
            "retryable": False,
            "http_status": status,
            "user_message": "生图请求超时（为避免重复扣费，不会自动重提同一请求）",
            "raw": text,
        }

    if re.search(
        r"payload is not completed|transferencodingerror|not enough data to satisfy transfer length|connection reset by peer|上游响应未完整接收|content.?length|incomplete",
        lowered,
    ):
        return {
            "category": "network",
            "retryable": True,
            "http_status": status,
            "user_message": "上游响应中断，请换渠道或稍后重试",
            "raw": text,
            "profile_switch_candidate": True,
        }

    for pattern in NON_RETRYABLE_PATTERNS:
        if re.search(pattern, lowered if pattern.isascii() else text, flags=re.I):
            if re.search(r"unsafe|safety|moderation|审核", lowered) or "审核" in text:
                category = "safety"
                user_message = "内容未通过安全策略"
            elif re.search(r"auth|token|key|unauthorized|forbidden|鉴权", lowered):
                category = "auth"
                user_message = "渠道鉴权失败，请检查 API Key"
            elif re.search(r"model|not found|不存在|未找到|未配置", lowered) or "未配置" in text or "未找到" in text:
                category = "not_found"
                user_message = "模型或渠道不可用"
            else:
                category = "fatal"
                user_message = text
            return {
                "category": category,
                "retryable": False,
                "http_status": status,
                "user_message": user_message if category != "fatal" else text,
                "raw": text,
            }

    for pattern in PARAM_ERROR_PATTERNS:
        if re.search(pattern, lowered, flags=re.I):
            return {
                "category": "param",
                "retryable": False,
                "http_status": status,
                "user_message": "请求参数不被上游接受",
                "raw": text,
                "profile_switch_candidate": True,
            }

    if status is not None and status >= 500:
        return {
            "category": "network",
            "retryable": True,
            "http_status": status,
            "user_message": f"上游服务异常（HTTP {status}）",
            "raw": text,
        }

    if status == 429:
        return {
            "category": "rate_limit",
            "retryable": True,
            "http_status": 429,
            "user_message": "请求过于频繁，请稍后重试",
            "raw": text,
        }

    return {
        "category": "unknown",
        "retryable": True,
        "http_status": status,
        "user_message": text,
        "raw": text,
    }


def is_non_retryable_generation_error(error: Any) -> bool:
    return not bool(classify_generation_error(error).get("retryable", True))


def is_param_profile_switch_error(error: Any) -> bool:
    info = classify_generation_error(error)
    if info.get("profile_switch_candidate"):
        return True
    if info.get("category") == "param":
        return True
    text = str(error or "").lower()
    return bool(re.search(r"size|aspect|response_format|quality|parameter|unsupported|not supported|extra inputs", text))


def is_transport_profile_switch_error(error: Any) -> bool:
    """Incomplete transfer / connection reset: try another payload profile once."""
    info = classify_generation_error(error)
    if info.get("profile_switch_candidate") and info.get("category") == "network":
        return True
    text = str(error or "").lower()
    return bool(
        re.search(
            r"payload is not completed|transferencodingerror|not enough data to satisfy transfer length|connection reset by peer|上游响应未完整接收",
            text,
        )
    )
