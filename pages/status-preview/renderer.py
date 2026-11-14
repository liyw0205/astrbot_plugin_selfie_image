"""Five standalone Pillow layouts for reviewing the model status report."""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
import math
import os
from pathlib import Path
import time

from PIL import Image, ImageDraw, ImageFont


DESIGNS = [
    {"id": "1", "name": "清透简报", "color": "#087f72"},
    {"id": "2", "name": "石墨监控", "color": "#a8e779"},
    {"id": "3", "name": "双栏卡片", "color": "#007eaa"},
    {"id": "4", "name": "瑞士排版", "color": "#e24b40"},
    {"id": "5", "name": "紧凑表格", "color": "#b28b17"},
]
ERRORS = {
    "auth": "鉴权失败", "authentication": "鉴权失败", "unauthorized": "鉴权失败",
    "forbidden": "鉴权失败", "rate_limit": "限流", "quota": "额度不足",
    "throttle": "限流", "timeout": "请求超时", "timeout_create": "创建超时",
    "timeout_poll": "轮询超时", "network": "网络错误", "server": "上游错误",
    "safety": "安全审核", "param": "参数错误", "not_found": "模型不存在",
}
COLORS = {
    "正常": "#078568", "降级": "#a76b09", "限流": "#a76b09",
    "异常": "#cd4346", "断流": "#cd4346", "验证失败": "#cd4346",
    "未使用": "#748088", "无模型": "#748088", "未知": "#748088",
}


@lru_cache(maxsize=32)
def font(size: int):
    configured = os.environ.get("ASTRBOT_STATUS_FONT", "")
    candidates = [
        configured, "/system/fonts/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    ]
    windows = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windows:
        candidates.append(str(Path(windows) / "Fonts" / "msyh.ttc"))
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().is_file():
            result = ImageFont.truetype(str(Path(candidate).expanduser()), size)
            masks = [bytes(result.getmask(char)) for char in "模型状态"]
            if len(set(masks)) == len(masks):
                return result
    raise RuntimeError("未找到中文字体，请通过 ASTRBOT_STATUS_FONT 指定字体文件")


class Canvas:
    def __init__(self, height: int, *, dark: bool = False, background: str | None = None):
        self.image = Image.new("RGB", (1080, height), background or ("#181818" if dark else "#ffffff"))
        self.draw = ImageDraw.Draw(self.image)
        self.ink = "#f4f4f4" if dark else "#20272b"
        self.muted = "#a9afa9" if dark else "#707b80"
        self.line_color = "#313131" if dark else "#e3e9e8"
        self.dark = dark

    def text(self, x, y, value, size=24, color=None, *, bold=False, width=None, align="left"):
        value = str(value)
        face = font(size)
        if width is not None and self.draw.textlength(value, font=face) > width:
            while value and self.draw.textlength(value + "…", font=face) > width:
                value = value[:-1]
            value += "…"
        if align == "right":
            x -= self.draw.textlength(value, font=face)
        self.draw.text((x, y), value, font=face, fill=color or self.ink,
                       anchor="lt", stroke_width=1 if bold else 0,
                       stroke_fill=color or self.ink)

    def box(self, x, y, width, height, fill, *, outline=None, radius=8):
        self.draw.rounded_rectangle((x, y, x + width, y + height), radius,
                                    fill=fill, outline=outline)

    def line(self, y, x1=48, x2=1032, color=None, thickness=1):
        self.draw.line((x1, y, x2, y), fill=color or self.line_color, width=thickness)

    def badge(self, x, y, label, *, right=False):
        color = COLORS.get(label, COLORS["未知"])
        if self.dark:
            color = {"#078568": "#a8e779", "#a76b09": "#edc66d",
                     "#cd4346": "#ff8c87", "#748088": "#b1b8bc"}[color]
        width = int(self.draw.textlength(label, font=font(24))) + 40
        left = x - width if right else x
        bg = "#272727" if self.dark else {
            "#078568": "#e8f6ef", "#a76b09": "#fbf1dc",
            "#cd4346": "#fff0ef", "#748088": "#edf0f1",
        }[color]
        self.box(left, y, width, 40, bg, radius=4)
        self.draw.ellipse((left + 12, y + 17, left + 18, y + 23), fill=color)
        self.text(left + 26, y + 6, label, 24, color)

    def history(self, x, y, value, width=600, height=16):
        markers = str(value or "-")[:24].ljust(24, "-")
        gap = 4
        step = (width + gap) / 24
        palette = {"■": "#2ba88a", "▲": "#e3b149", "×": "#dc7272"}
        if self.dark:
            palette = {"■": "#a8e779", "▲": "#e4bd64", "×": "#f18a82"}
        for index, marker in enumerate(markers):
            left = round(x + index * step)
            self.box(left, y, max(2, int(step - gap)), height,
                     palette.get(marker, "#393939" if self.dark else "#e5eae9"), radius=2)


def details(item):
    errors = item.get("error_categories") or {}
    parts = [f"{ERRORS.get(str(key), str(key))} {int(value)}" for key, value in list(errors.items())[:3]]
    streak = int(item.get("consecutive_failures") or 0)
    if streak:
        parts.append(f"连续失败 {streak} 次")
    return " · ".join(parts) or ("尚无请求记录" if not item.get("attempts") else "暂无错误")


def metrics(item):
    attempts = int(item.get("attempts") or 0)
    rate = f"{float(item.get('success_rate') or 0) * 100:.1f}%" if attempts else "—"
    average = float(item.get("average_elapsed_seconds") or 0)
    elapsed = f"{average:.2f}s" if average > 0 else "—"
    return rate, f"{int(item.get('successes') or 0)}/{attempts}", elapsed


def recent(item, updated):
    timestamp = float(item.get("last_timestamp") or 0)
    if not timestamp:
        return "尚无请求"
    age = max(0, int(updated - timestamp))
    age_text = f"{age} 秒前" if age < 60 else f"{age // 60} 分钟前" if age < 3600 else f"{age // 3600} 小时前" if age < 86400 else f"{age // 86400} 天前"
    outcome = "成功" if item.get("last_success") is True else ERRORS.get(str(item.get("last_error_category")), "失败")
    return f"{age_text} · {outcome}"


def title(c, data, *, accent="#087f72", eyebrow="SELFIE IMAGE / MODEL REPORT"):
    c.text(48, 36, eyebrow, 18, accent)
    c.text(48, 76, "生图模型运行状态", 48, bold=True)
    c.badge(1032, 84, str(data.get("overall_status") or "未知"), right=True)
    c.text(48, 148, data["window_label"], 24, c.muted)
    c.text(1032, 152, data["updated_label"], 20, c.muted, align="right")


def kpis(c, data, y, *, accent="#087f72"):
    items = [("模型总数", data.get("model_count", 0)), ("渠道总数", data.get("channel_count", 0)),
             ("可用模型", data.get("available_count", 0)), ("告警模型", data.get("alert_count", 0))]
    for index, (label, value) in enumerate(items):
        x = 48 + index * 252
        c.text(x, y, label, 20, c.muted)
        c.text(x, y + 36, value, 48, accent if index == 2 else c.ink, bold=True)


def footer(c, data, y):
    c.line(y)
    y += 24
    c.text(48, y, "历史", 18, c.muted)
    for x, color, label in [(112, "#2ba88a", "正常"), (232, "#e3b149", "波动 / 慢"),
                             (400, "#dc7272", "失败"), (520, "#88928f", "无请求")]:
        c.box(x, y + 4, 12, 12, color, radius=2)
        c.text(x + 24, y, label, 18, c.muted)
    c.text(1032, y, f"第 {data['page']} / {data['pages']} 页", 20, c.muted, align="right")
    c.text(48, y + 40, "数据来自最近生图请求记录", 18, c.muted)
    if data["pages"] > 1:
        next_page = data["page"] % data["pages"] + 1
        c.text(1032, y + 40, f"下一页：/生图记录状态 {data['window_token']} {next_page}", 18, c.muted, align="right")


def empty(c, y):
    c.line(y)
    c.text(48, y + 64, "暂无模型记录", 36, bold=True)
    c.text(48, y + 120, "配置生图模型后，运行状态将在这里显示。", 24, c.muted)


def brief(data, rows):
    end = 388 + max(1, len(rows)) * 184
    c = Canvas(end + 120)
    title(c, data)
    c.line(200)
    kpis(c, data, 232)
    c.line(344)
    c.text(48, 368, "模型与最近请求", 20, c.muted)
    c.text(816, 368, "成功率 / 成功请求", 20, c.muted)
    if not rows:
        empty(c, 420)
    for index, item in enumerate(rows):
        y = 420 + index * 184
        c.text(48, y, item.get("label") or "未命名模型", 30, bold=True, width=652)
        c.badge(1032, y - 4, item.get("status", "未知"), right=True)
        c.text(48, y + 48, f"平均 {metrics(item)[2]}  ·  {recent(item, data['updated_at'])}", 24, c.muted, width=700)
        rate, requests, _ = metrics(item)
        c.text(1032, y + 48, rate, 36, bold=True, align="right")
        c.history(48, y + 96, item.get("history"), 672)
        c.text(1032, y + 104, f"{requests} 次", 24, c.muted, align="right")
        c.text(48, y + 128, details(item), 20, c.muted, width=960)
        if index < len(rows) - 1:
            c.line(y + 164)
    footer(c, data, end)
    return c.image


def graphite(data, rows):
    end = 400 + max(1, len(rows)) * 176
    c = Canvas(end + 120, dark=True)
    title(c, data, accent="#a8e779", eyebrow="SELFIE IMAGE / OPERATIONS")
    c.line(200)
    kpis(c, data, 232, accent="#a8e779")
    c.line(344)
    c.text(48, 368, "MODEL / REQUEST HISTORY", 18, "#a8e779")
    if not rows:
        empty(c, 420)
    for index, item in enumerate(rows):
        y = 416 + index * 176
        c.text(48, y, f"{data['start'] + index + 1:02}", 24, "#a8e779")
        c.text(112, y, item.get("label") or "未命名模型", 30, bold=True, width=620)
        c.badge(1032, y - 6, item.get("status", "未知"), right=True)
        rate, requests, elapsed = metrics(item)
        c.text(112, y + 48, f"{rate}  /  {requests} 次  /  {elapsed}", 24)
        c.history(112, y + 92, item.get("history"), 920, 12)
        c.text(112, y + 124, details(item), 20, c.muted, width=560)
        c.text(1032, y + 124, recent(item, data["updated_at"]), 18, c.muted, align="right")
        if index < len(rows) - 1:
            c.line(y + 156, x1=112)
    footer(c, data, end)
    return c.image


def tiles(data, rows):
    from generation.model_status_report import render_model_status_report

    # The selected preview uses the exact renderer shipped by the command.
    return render_model_status_report(
        data, font_loader=font,
        error_label=lambda key: ERRORS.get(key, key or "未知错误"),
        page=data["page"], page_size=data["page_size"],
    )


def swiss(data, rows):
    end = 412 + max(1, len(rows)) * 192
    c = Canvas(end + 120)
    c.draw.rectangle((0, 0, 1080, 16), fill="#e24b40")
    c.text(48, 48, "SELFIE IMAGE", 24, "#e24b40", bold=True)
    c.text(1032, 48, "运行简报 / " + data["window_label"], 24, align="right")
    c.text(48, 104, "生图记录状态", 60, bold=True)
    c.text(48, 192, data["updated_label"], 20, c.muted)
    c.badge(1032, 184, str(data.get("overall_status") or "未知"), right=True)
    c.line(248, color="#20272b", thickness=3)
    kpis(c, data, 280, accent="#e24b40")
    c.line(392, color="#20272b", thickness=2)
    if not rows:
        empty(c, 436)
    for index, item in enumerate(rows):
        y = 436 + index * 192
        c.text(48, y, f"{data['start'] + index + 1:02}", 48, "#e24b40", bold=True)
        c.text(152, y, item.get("label") or "未命名模型", 30, bold=True, width=636)
        c.badge(1032, y - 4, item.get("status", "未知"), right=True)
        rate, requests, elapsed = metrics(item)
        c.text(152, y + 48, f"成功率 {rate}    请求 {requests}    平均 {elapsed}", 24, width=880)
        c.history(152, y + 96, item.get("history"), 880, 12)
        c.text(152, y + 128, details(item), 20, c.muted, width=580)
        c.text(1032, y + 128, recent(item, data["updated_at"]), 18, c.muted, align="right")
        if index < len(rows) - 1:
            c.line(y + 168)
    footer(c, data, end)
    return c.image


def table(data, rows):
    end = 448 + max(1, len(rows)) * 136
    c = Canvas(end + 120)
    c.draw.rectangle((0, 0, 1080, 192), fill="#f8f4dd")
    title(c, data, accent="#796514", eyebrow="SELFIE IMAGE / STATUS INDEX")
    kpis(c, data, 224, accent="#078568")
    c.line(352)
    for x, label in [(48, "模型 / 请求历史"), (480, "状态"), (664, "成功率"), (824, "成功 / 总计"), (960, "平均耗时")]:
        c.text(x, 384, label, 20, c.muted)
    c.line(428)
    if not rows:
        empty(c, 460)
    for index, item in enumerate(rows):
        y = 452 + index * 136
        c.text(48, y, item.get("label") or "未命名模型", 24, bold=True, width=392)
        c.badge(472, y - 8, item.get("status", "未知"))
        rate, requests, elapsed = metrics(item)
        c.text(744, y, rate, 24, align="right")
        c.text(900, y, requests, 24, align="right")
        c.text(1032, y, elapsed, 24, align="right")
        c.history(48, y + 48, item.get("history"), 384, 12)
        c.text(48, y + 80, recent(item, data["updated_at"]), 18, c.muted, width=392)
        c.text(472, y + 68, details(item), 20, c.muted, width=560)
        if index < len(rows) - 1:
            c.line(y + 112)
    footer(c, data, end)
    return c.image


def prepare(status: Mapping, page=1, page_size=6):
    models = [item for item in status.get("models", []) if isinstance(item, Mapping)]
    size = max(1, min(20, int(page_size)))
    pages = max(1, math.ceil(len(models) / size))
    safe_page = max(1, min(int(page), pages))
    start = (safe_page - 1) * size
    data = dict(status)
    data.update(page=safe_page, pages=pages, start=start, page_size=size)
    seconds = int(status.get("window_seconds") or 604800)
    data["window_label"] = {3600: "最近 1 小时", 86400: "最近 24 小时", 604800: "最近 7 天", 2592000: "最近 30 天"}.get(seconds, f"最近 {max(1, seconds // 3600)} 小时")
    data["window_token"] = {3600: "1h", 86400: "24h", 604800: "7d", 2592000: "30d"}.get(seconds, "7d")
    data["updated_at"] = float(status.get("updated_at") or time.time())
    data["updated_label"] = time.strftime("%Y.%m.%d  %H:%M:%S", time.localtime(data["updated_at"]))
    return data, models[start:start + size]


def render_status_report(status: Mapping, design="1", *, page=1, page_size=6):
    layouts = {"1": brief, "2": graphite, "3": tiles, "4": swiss, "5": table}
    return layouts[str(design)](*prepare(status, page, page_size))
