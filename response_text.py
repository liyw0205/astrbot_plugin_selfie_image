"""User-facing acknowledgement, failure, and tool response text helpers."""

from __future__ import annotations

import random
import re
from collections.abc import Callable

from .utils import redact_sensitive_text


def compact_for_repeat_check(text: str) -> str:
    return re.sub(
        r"[\s`*_~\"'“”‘’「」『』《》()\[\]{}，。！？、；：,.!?;:\-_/\\|]+",
        "",
        str(text or ""),
    ).lower()


def ack_repeats_request(ack_message: str, user_request: str) -> bool:
    request = str(user_request or "").strip()
    if not request:
        return False
    ack_compact = compact_for_repeat_check(ack_message)
    request_compact = compact_for_repeat_check(request)
    if len(request_compact) >= 8 and request_compact in ack_compact:
        return True
    for piece in re.split(r"[\s，。！？、；：,.!?;:]+", request):
        piece_compact = compact_for_repeat_check(piece)
        if len(piece_compact) >= 8 and piece_compact in ack_compact:
            return True
    return False


def looks_like_non_chinese_ack(text: str) -> bool:
    raw = str(text or "")
    if not raw:
        return False
    chinese_count = len(re.findall(r"[\u4e00-\u9fff]", raw))
    latin_count = len(re.findall(r"[A-Za-z]", raw))
    if chinese_count == 0 and latin_count >= 4:
        return True
    return latin_count > chinese_count * 2 and latin_count >= 12


def clean_ack_message(ack_message: str, user_request: str) -> str:
    custom = re.sub(r"\s+", " ", str(ack_message or "")).strip()
    if not custom or looks_like_non_chinese_ack(custom):
        return ""
    if ack_repeats_request(custom, user_request):
        return ""
    stiff_markers = (
        "沿着",
        "顺着",
        "照着",
        "按这个",
        "按照这个",
        "根据你的提示",
        "根据用户",
        "用户需求",
        "用户要求",
        "提示词",
        "prompt",
        "生图",
        "工具",
        "配置",
        "正在生成",
        "开始生成",
        "为你生成",
        "帮你生成",
        "收到",
        "已收到",
    )
    low = custom.lower()
    if any(marker.lower() in low for marker in stiff_markers):
        return ""
    return custom[:80]


def natural_ack_fallback(kind: str, count: int, bot_name: str = "啊呜") -> str:
    name = str(bot_name or "").strip() or "啊呜"
    multi = count > 1
    if kind == "selfie":
        options = [
            f"{name}去找一下角度。",
            "等我一下，我对下光线。",
            "我换个顺眼点的构图。",
            "我先把画面收一下。",
            "稍等，我抓个自然点的瞬间。",
            "我试个更日常的角度。",
            "等我，我把镜头感压轻一点。",
            "我先看一下怎么拍更舒服。",
        ]
        if multi:
            options.extend(["我多试几个角度。", f"{name}多拍几张看看。", "我换几版构图。"])
        return random.choice(options)
    options = [
        f"{name}先把画面理一下。",
        "我先想一下构图。",
        "等我一下，我搭个画面。",
        "我试着把这个感觉做出来。",
        "稍等，我换个画面方向。",
        "我先对一下主体和光线。",
    ]
    if multi:
        options.extend(["我多试几版构图。", f"{name}多跑几张看看。"])
    return random.choice(options)


def natural_fail_fallback(kind: str = "") -> str:
    options_by_kind = {
        "legs": [
            "刚刚那版腿部比例不太顺。",
            "这次腿部构图没出来。",
            "刚才那张下半身有点乱。",
            "这版角度不太对。",
            "刚刚那张效果不行。",
        ],
        "selfie": [
            "刚刚那版不太像我。",
            "这次镜头感有点跑偏。",
            "刚才那张效果不太对。",
            "这版没出来想要的感觉。",
            "刚刚那张不太行。",
        ],
        "group": [
            "刚刚那版同框效果不太对。",
            "这次合影站位有点乱。",
            "刚才那张人物关系没处理好。",
            "这版合照没出来想要的感觉。",
        ],
        "image": [
            "刚刚那版画面不太对。",
            "这次效果没出来。",
            "刚才那张没成。",
            "这版构图有点跑偏。",
        ],
    }
    return random.choice(options_by_kind.get(kind) or options_by_kind["image"])


def friendly_user_error_message(
    error: str,
    fallback: str = "",
    default_fail: Callable[[], str] | None = None,
) -> str:
    text = str(error or "").strip()
    if not text:
        return fallback or "我刚刚没抓稳这个感觉，换个方向再试试好不好。"
    if "可使用人员白名单" in text:
        return "这个功能现在还只对少数人开放。"
    if "用户黑名单" in text:
        return "现在先不方便给你看这类内容。"
    if "今日生图次数已用完" in text:
        return "我今天已经拍得有点多了，明天再来找我好不好。"
    if "请求太频繁" in text:
        return "先让我缓一小会儿，我等下就继续给你看。"
    if (
        "提示词审核未通过" in text
        or "图片内容审核未通过" in text
        or "提示词包含禁用词" in text
    ):
        return "这次这个方向我不太方便照着来，换个说法或者换个感觉试试。"
    if (
        "当前没有可用的生图模型" in text
        or "未配置可用出图审核模型" in text
        or "未启用" in text
    ):
        return random.choice(["这会儿接口没接上，晚点再试。", "现在暂时出不了图，等配置恢复再来。"])
    if "缺少生图提示词" in text or "请输入提示词" in text:
        return "你想让我往什么感觉走？也可以直接丢张参考图给我。"
    detail = redact_sensitive_text(text)
    detail = re.sub(r"[\r\n\t]+", " ", detail)
    detail = re.sub(r"Traceback \(most recent call last\):.*", "", detail, flags=re.I)
    detail = re.sub(r"\s+", " ", detail).strip(" ：:;；")
    if detail:
        return detail[:180] + ("…" if len(detail) > 180 else "")
    return fallback or (default_fail() if default_fail else natural_fail_fallback("image"))


def tool_soft_fail(
    error: str,
    fallback: str = "",
    error_formatter: Callable[[str, str], str] | None = None,
) -> str:
    formatter = error_formatter or friendly_user_error_message
    message = formatter(error, fallback)
    return f"[TOOL_FAILED] {message}\n请用简体中文、用你自己的语气自然安抚用户，别提生成、工具、配置、次数、冷却或审核这些词。"


def tool_unavailable(fallback: str) -> str:
    return f"[TOOL_UNAVAILABLE] {fallback}\n请用简体中文、用你自己的语气自然安抚用户，别提功能、工具或配置。"


def tool_success(kind: str = "image", count: int = 1) -> str:
    label = "照片" if kind == "selfie" else "图片"
    count_text = f"，共 {count} 张" if count > 1 else ""
    return (
        f"[TOOL_SUCCESS] {label}已经发给用户{count_text}。\n"
        "请用简体中文、按当前人格自然收尾一句，也可以很短。"
        "不要复述请求，不要说生成、绘制、工具、调用、任务、已完成、已发送、配置、模型、提示词或审核。"
    )
