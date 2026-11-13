"""Compact two-column model status image, independent of the bot runtime."""

from collections.abc import Mapping
from functools import lru_cache
import math
import time
from typing import Any, Callable


STATUS_COLORS = {
    "正常": ("#078568", "#e8f6ef"),
    "降级": ("#a76b09", "#fbf1dc"),
    "限流": ("#a76b09", "#fbf1dc"),
    "异常": ("#cd4346", "#fff0ef"),
    "断流": ("#cd4346", "#fff0ef"),
    "验证失败": ("#cd4346", "#fff0ef"),
}


def render_model_status_report(
    status: Mapping[str, Any],
    *,
    font_loader: Callable[[int], Any],
    error_label: Callable[[str], str],
    page: int = 1,
    page_size: int = 6,
):
    # Keep Pillow optional for installations that use the text fallback.
    from PIL import Image, ImageDraw

    models = [item for item in status.get("models", []) if isinstance(item, Mapping)]
    size = max(1, int(page_size or 6))
    page_count = max(1, math.ceil(len(models) / size))
    safe_page = max(1, min(int(page or 1), page_count))
    rows = models[(safe_page - 1) * size:safe_page * size]
    footer_y = 384 + max(1, math.ceil(len(rows) / 2)) * 340
    image = Image.new("RGB", (1080, footer_y + 120), "#eef7f8")
    draw = ImageDraw.Draw(image)
    ink, muted = "#20272b", "#707b80"

    @lru_cache(maxsize=None)
    def font(font_size):
        face = font_loader(font_size)
        if face is None:
            raise ValueError("未找到支持中文的状态图字体")
        return face

    def text(x, y, value, font_size=24, color=ink, *, bold=False, width=None, right=False):
        value = str(value)
        face = font(font_size)
        if width is not None and draw.textlength(value, font=face) > width:
            while value and draw.textlength(value + "…", font=face) > width:
                value = value[:-1]
            value += "…"
        if right:
            x -= draw.textlength(value, font=face)
        draw.text((x, y), value, font=face, fill=color, anchor="lt",
                  stroke_width=1 if bold else 0, stroke_fill=color)

    def box(x, y, width, height, fill, *, outline=None, radius=8):
        draw.rounded_rectangle((x, y, x + width, y + height), radius,
                               fill=fill, outline=outline)

    def badge(x, y, label):
        color, fill = STATUS_COLORS.get(label, ("#748088", "#edf0f1"))
        width = int(draw.textlength(label, font=font(24))) + 40
        left = x - width
        box(left, y, width, 40, fill, radius=4)
        draw.ellipse((left + 12, y + 17, left + 18, y + 23), fill=color)
        text(left + 26, y + 6, label, 24, color)

    def history(x, y, value):
        markers = str(value or "-")[:24].ljust(24, "-")
        palette = {"■": "#2ba88a", "▲": "#e3b149", "×": "#dc7272"}
        step = (432 + 4) / 24
        for index, marker in enumerate(markers):
            box(round(x + index * step), y, int(step - 4), 12,
                palette.get(marker, "#e5eae9"), radius=2)

    seconds = int(status.get("window_seconds") or 0)
    window_label = {
        3600: "最近 1 小时", 86400: "最近 24 小时",
        604800: "最近 7 天", 2592000: "最近 30 天",
    }.get(seconds, f"最近 {max(1, seconds // 3600)} 小时")
    try:
        updated = float(status.get("updated_at") or time.time())
        updated_text = time.strftime("%Y.%m.%d  %H:%M:%S", time.localtime(updated))
    except (TypeError, ValueError, OverflowError, OSError):
        updated = time.time()
        updated_text = "未知"

    text(48, 36, "SELFIE IMAGE / MODEL OVERVIEW", 18, "#007eaa")
    text(48, 76, "生图模型运行状态", 48, bold=True)
    badge(1032, 84, str(status.get("overall_status") or "未知"))
    text(48, 148, window_label, 24, muted)
    text(1032, 152, updated_text, 20, muted, right=True)
    kpis = [
        ("模型总数", int(status.get("model_count") or 0)),
        ("渠道总数", int(status.get("channel_count") or 0)),
        ("可用模型", int(status.get("available_count") or 0)),
        ("告警模型", int(status.get("alert_count") or 0)),
    ]
    for index, (label, value) in enumerate(kpis):
        x = 48 + index * 252
        text(x, 224, label, 20, muted)
        text(x, 260, value, 48, "#007eaa" if index == 2 else ink, bold=True)

    for index, item in enumerate(rows):
        x = 48 + index % 2 * 504
        y = 368 + index // 2 * 340
        box(x, y, 480, 316, "#ffffff", outline="#d9e6e8")
        state = str(item.get("status") or "未知")
        badge_width = int(draw.textlength(state, font=font(24))) + 40
        text(x + 24, y + 24, item.get("channel") or "模型", 20, muted,
             width=432 - badge_width - 16)
        badge(x + 456, y + 16, state)
        text(x + 24, y + 64, item.get("model") or item.get("label") or "未命名模型",
             30, bold=True, width=432)
        attempts = int(item.get("attempts") or 0)
        successes = int(item.get("successes") or 0)
        rate = f"{float(item.get('success_rate') or 0) * 100:.1f}%" if attempts else "—"
        elapsed = float(item.get("average_elapsed_seconds") or 0)
        text(x + 24, y + 120, rate, 48, bold=True)
        text(x + 456, y + 128, f"{elapsed:.2f}s" if elapsed > 0 else "—", 30, right=True)
        text(x + 24, y + 184, f"成功率 · {successes}/{attempts} 次", 20, muted, width=296)
        text(x + 456, y + 184, "平均耗时", 20, muted, right=True)
        history(x + 24, y + 224, item.get("history"))
        errors = item.get("error_categories")
        errors = errors if isinstance(errors, Mapping) else {}
        parts = [f"{error_label(str(key))} {int(value)}" for key, value in list(errors.items())[:3]]
        streak = int(item.get("consecutive_failures") or 0)
        if streak:
            parts.append(f"连续失败 {streak} 次")
        error_text = " · ".join(parts) or ("尚无请求记录" if not attempts else "暂无错误")
        text(x + 24, y + 252, error_text, 20, muted, width=432)
        timestamp = float(item.get("last_timestamp") or 0)
        if timestamp:
            age = max(0, int(updated - timestamp))
            if age < 60:
                age_text = f"{age} 秒前"
            elif age < 3600:
                age_text = f"{age // 60} 分钟前"
            elif age < 86400:
                age_text = f"{age // 3600} 小时前"
            else:
                age_text = f"{age // 86400} 天前"
            outcome = "成功" if item.get("last_success") is True else error_label(str(item.get("last_error_category") or "unknown"))
            recent_text = f"{age_text} · {outcome}"
        else:
            recent_text = "尚无请求"
        text(x + 24, y + 284, recent_text, 18, muted, width=432)

    if not rows:
        draw.line((48, 384, 1032, 384), fill="#e3e9e8")
        text(48, 448, "暂无模型记录", 36, bold=True)
        text(48, 504, "配置生图模型后，运行状态将在这里显示。", 24, muted)

    draw.line((48, footer_y, 1032, footer_y), fill="#e3e9e8")
    text(48, footer_y + 24, "历史", 18, muted)
    for x, color, label in [
        (112, "#2ba88a", "正常"), (232, "#e3b149", "波动 / 慢"),
        (400, "#dc7272", "失败"), (520, "#88928f", "无请求"),
    ]:
        box(x, footer_y + 28, 12, 12, color, radius=2)
        text(x + 24, footer_y + 24, label, 18, muted)
    text(1032, footer_y + 24, f"第 {safe_page} / {page_count} 页", 20, muted, right=True)
    text(48, footer_y + 64, "数据来自最近生图请求记录", 18, muted)
    if page_count > 1:
        token = {3600: "1h", 86400: "24h", 604800: "7d", 2592000: "30d"}.get(seconds, "7d")
        text(1032, footer_y + 64, f"下一页：/生图记录状态 {token} {safe_page % page_count + 1}",
             18, muted, right=True)
    return image
