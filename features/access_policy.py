"""Pure access, quota, and prompt policy checks."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Dict


def access_status(
    *,
    user_id: str,
    group_id: str,
    blocked_users: Iterable[str],
    usable_users: Iterable[str],
    whitelist_users: Iterable[str],
    whitelist_groups: Iterable[str],
) -> Dict[str, Any]:
    blocked = set(blocked_users or [])
    usable = set(usable_users or [])
    user_whitelist = set(whitelist_users or [])
    group_whitelist = set(whitelist_groups or [])
    status = {
        "user_id": user_id,
        "group_id": group_id,
        "allowed": True,
        "unlimited": False,
        "whitelist": False,
        "reason": "",
    }
    if user_id and user_id in blocked:
        status.update({"allowed": False, "reason": "用户黑名单"})
        return status
    if usable and user_id not in usable:
        status.update({"allowed": False, "reason": "可使用人员白名单"})
        return status
    if user_id in user_whitelist or (group_id and group_id in group_whitelist):
        status["unlimited"] = True
        status["whitelist"] = True
    return status


def permission_denied_message(status: Mapping[str, Any]) -> str:
    if status.get("allowed"):
        return ""
    if status.get("reason") == "可使用人员白名单":
        return "当前仅允许可使用人员白名单内用户使用生图功能。"
    return "你已被加入用户黑名单，无法使用生图功能。"


def quota_error_message(
    status: Mapping[str, Any],
    usage_stats: Mapping[str, Any],
    *,
    enabled: bool,
    limit: int,
    requested_count: int = 1,
) -> str:
    permission_error = permission_denied_message(status)
    if permission_error:
        return permission_error
    if not enabled or status.get("unlimited"):
        return ""
    user_id = str(status.get("user_id") or "")
    users = usage_stats.get("users") if isinstance(usage_stats.get("users"), Mapping) else {}
    row = users.get(user_id) if isinstance(users.get(user_id), Mapping) else {}
    used = int(row.get("count", 0))
    if used + max(1, requested_count) <= limit:
        return ""
    return f"今日生图次数已用完：{used}/{limit}。"


def blocked_prompt_word(prompt: str, blocked_words: Iterable[str]) -> str:
    low_text = str(prompt or "").lower()
    for word in blocked_words or []:
        if word and str(word).lower() in low_text:
            return f"提示词包含禁用词：{word}"
    return ""
