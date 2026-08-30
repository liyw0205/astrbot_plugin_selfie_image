"""Conversation follow-up classification and recent-image routing helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List


def format_context_for_llm(
    records: Iterable[Mapping[str, Any]], max_chars: int = 1400
) -> str:
    lines: List[str] = []
    total = 0
    for record in reversed(list(records)):
        sender = "[Bot]" if record.get("is_bot") else str(record.get("sender_name") or "用户")
        content = str(record.get("content") or "").strip()
        image_tag = " [含图片]" if record.get("has_image") else ""
        line = f"{sender}: {content}{image_tag}".strip()
        if not line:
            continue
        if total + len(line) > max_chars:
            break
        lines.insert(0, line)
        total += len(line) + 1
    return "\n".join(lines)


def compact_followup_text(text: str) -> str:
    """Normalize spoken follow-up text for keyword matching."""
    compact = re.sub(r"[\s，。！？、；：,.!?;:]+", "", str(text or "").lower())
    if not compact:
        return ""
    return re.sub(r"([这那])一([身套件])", r"\1\2", compact)


def looks_like_context_image_reference(text: str) -> bool:
    compact = compact_followup_text(text)
    if not compact:
        return False
    keywords = (
        "上图",
        "上一张",
        "上张",
        "上一套",
        "上套",
        "刚才那张",
        "刚刚那张",
        "刚才那套",
        "刚刚那套",
        "刚发的",
        "前面那张",
        "前面那套",
        "这张",
        "这图",
        "这个图",
        "那张",
        "那图",
        "那套",
        "那一套",
        "继续改",
        "接着改",
        "在这个基础上",
        "基于这张",
        "参考这个",
        "参考刚才",
        "按刚才",
        "用刚刚",
        "用刚才",
        "用上一",
        "换回",
        "同款",
        "换成",
        "改一下",
        "修一下",
        "穿这个",
        "穿这",
        "穿上这个",
        "穿上这",
        "是穿这个",
        "换这个",
        "换上这个",
        "换上这",
        "这套",
        "这身",
        "这件",
        "刚刚的衣服",
        "刚才的衣服",
        "不是刚刚的衣服",
        "不是刚才的衣服",
        "一模一样的衣服",
        "复刻",
        "照着穿",
        "按这套",
        "按这身",
    )
    english = (
        "previousimage",
        "lastimage",
        "lastphoto",
        "thisimage",
        "editthis",
        "continueediting",
        "basedonthis",
        "sameasbefore",
        "wearthis",
        "putthison",
        "sameoutfit",
        "previousoutfit",
        "lastoutfit",
    )
    return any(keyword in compact for keyword in keywords) or any(
        keyword in compact for keyword in english
    )


def looks_like_clothes_followup(text: str) -> bool:
    """Return whether the user refers to an earlier outfit reference."""
    compact = compact_followup_text(text)
    if not compact:
        return False
    keys = (
        "穿这个",
        "穿这",
        "穿上这个",
        "是穿这个",
        "换这个",
        "换上这个",
        "这套",
        "这身",
        "这件",
        "那套",
        "那一套",
        "上一套",
        "上套",
        "衣服",
        "服装",
        "穿搭",
        "刚刚的衣服",
        "刚才的衣服",
        "刚刚那套",
        "刚才那套",
        "不是刚刚的衣服",
        "不是刚才的衣服",
        "一模一样的衣服",
        "复刻",
        "照着穿",
        "同款",
        "outfit",
        "wearthis",
        "clothes",
    )
    return any(key in compact for key in keys)


def looks_like_edit_bot_result_followup(text: str) -> bool:
    """Return whether the user wants to edit a recent generated result."""
    compact = compact_followup_text(text)
    if not compact:
        return False
    keys = (
        "刚才那张",
        "刚刚那张",
        "刚才那套",
        "刚刚那套",
        "那一套",
        "那套",
        "上一张",
        "上一套",
        "上套",
        "上张图",
        "用刚刚",
        "用刚才",
        "用上一",
        "换回刚刚",
        "换回刚才",
        "换回那套",
        "换回上一",
        "继续改",
        "接着改",
        "在这个基础上",
        "基于这张",
        "这张再",
        "把刚才",
        "刚生成",
        "刚画的",
        "刚发的",
    )
    return any(key in compact for key in keys)


def recent_context_image_sources(
    records: Iterable[Mapping[str, Any]],
    max_images: int = 4,
    *,
    prefer_user: bool = True,
    user_only: bool = False,
    bot_only: bool = False,
) -> List[str]:
    """Select recent user or generated image sources in follow-up priority order."""
    user_sources: List[str] = []
    bot_sources: List[str] = []
    seen_user = set()
    seen_bot = set()
    for record in reversed(list(records)):
        is_bot = bool(record.get("is_bot"))
        for source in reversed(list(record.get("image_sources") or [])):
            text = str(source or "").strip()
            if not text:
                continue
            if is_bot:
                if text in seen_bot:
                    continue
                seen_bot.add(text)
                bot_sources.append(text)
            else:
                if text in seen_user:
                    continue
                seen_user.add(text)
                user_sources.append(text)

    limit = max(1, int(max_images or 1))
    if bot_only:
        return bot_sources[:limit]
    if user_only:
        return user_sources[:limit]
    primary, secondary = (
        (user_sources, bot_sources) if prefer_user else (bot_sources, user_sources)
    )
    output = list(primary)
    for text in secondary:
        if len(output) >= limit:
            break
        if text not in output:
            output.append(text)
    return output[:limit]
