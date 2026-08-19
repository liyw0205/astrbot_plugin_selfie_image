"""Image generation error classification (non-retryable vs retryable).

Source: Railgun19457/astrbot_plugin_image_generation style error dictionaries;
Selfie target 09.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from .proxy import LOCAL_IMAGE_WAIT_SECONDS


def format_timeout_user_message(kind: str, seconds: Optional[int] = None) -> str:
    """Record-facing timeout copy. Do not mention fallback/next model."""
    kind = str(kind or "").strip().lower()
    if kind in {"global", "chain", "job"}:
        n = max(1, int(seconds or 280))
        return f"生图超时（{n}s）"
    if kind in {"local", "wait", "client"}:
        n = max(1, int(seconds or LOCAL_IMAGE_WAIT_SECONDS))
        return f"模型超时（{n}s）"
    return "上游模型超时"


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
        category: auth | not_found | safety | param | timeout | network | server | rate_limit | unknown
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
        elif re.search(
            r"unsafe|safety|moderation|content[_\s-]?policy|审核|安全政策|安全策略|"
            r"不适合进行图像生成|rejected by content|content moderation",
            lowered,
        ) or re.search(r"安全政策|安全策略|内容审核|不适合进行图像生成", text):
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
        g = re.search(r"生图全局超时[（(](\d+)\s*秒[）)]|生图超时[（(](\d+)\s*s[）)]", text)
        if g:
            n = int(next(x for x in g.groups() if x))
            user_message = format_timeout_user_message("global", n)
        elif re.search(
            r"改试下一个|已改试下一个|模型超时|生图请求超时|图生图请求超时|NovelAI 请求超时|^请求超时$",
            text.strip(),
        ):
            sec = re.search(r"[（(](\d+)\s*(?:秒|s)[）)]", text)
            n = int(sec.group(1)) if sec else LOCAL_IMAGE_WAIT_SECONDS
            user_message = format_timeout_user_message("global" if n >= 200 else "local", n)
        else:
            user_message = format_timeout_user_message("upstream")
        return {
            "category": "timeout",
            "retryable": False,
            "http_status": status,
            "user_message": user_message,
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
            if (
                re.search(
                    r"unsafe|safety|moderation|审核|安全政策|安全策略|不适合进行图像生成|rejected by content",
                    lowered,
                )
                or "审核" in text
                or "安全政策" in text
            ):
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
            "category": "server",
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


def summarize_generation_failures(
    attempts: Any,
    *,
    fallback_error: str = "",
) -> Dict[str, Any]:
    """Build list/detail failure summaries from attempt rows.

    - failure_reason: last failed attempt's single-cause text (for table column)
    - failure_reasons: each failed attempt with model label + parsed reason
    - last_failed_model: last failed attempt label (fill empty used_model)
    """
    rows: list[Dict[str, Any]] = []
    for item in list(attempts or []):
        if not isinstance(item, dict):
            continue
        if item.get("success") is True:
            continue
        label = str(item.get("label") or item.get("model") or item.get("channel") or "").strip()
        raw = str(item.get("error") or "").strip()
        info = classify_generation_error(" ".join(part for part in (raw, str(item.get("error_user_message") or ""), fallback_error) if part) or "生成失败")
        category = str(info.get("category") or item.get("error_category") or "").strip()
        user_message = str(info.get("user_message") or item.get("error_user_message") or raw or "生成失败").strip()
        if not raw and not user_message:
            continue
        rows.append(
            {
                "label": label,
                "error": raw,
                "error_user_message": user_message,
                "error_category": category,
                "elapsed_seconds": item.get("elapsed_seconds"),
                "attempt": item.get("attempt"),
            }
        )

    last = rows[-1] if rows else None
    if last:
        label = str(last.get("label") or "").strip()
        reason = str(last.get("error_user_message") or last.get("error") or "").strip()
        failure_reason = f"{label}: {reason}".strip(": ").strip() if label else reason
        last_failed_model = label
    else:
        failure_reason = str(fallback_error or "").strip()
        last_failed_model = ""
        # Prefer "channel/model: reason" already present in top-level error.
        if failure_reason and ":" in failure_reason:
            head = failure_reason.split(":", 1)[0].strip()
            if head and "/" in head:
                last_failed_model = head

    return {
        "failure_reason": failure_reason,
        "failure_reasons": rows,
        "last_failed_model": last_failed_model,
    }
