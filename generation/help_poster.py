"""Generate and cache the two Pillow help images used by the bot commands."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from PIL import Image, ImageDraw, ImageFont


HELP_IMAGE_LAYOUT_VERSION = "2026-11-16-wide-v1"
WIDTH = 2200
MARGIN = 56
GAP = 28

COLORS = {
    "bg": "#f4faf8",
    "panel": "#ffffff",
    "ink": "#18332e",
    "muted": "#5c716b",
    "accent": "#087f72",
    "line": "#c5ddd6",
}

QUICK_SECTIONS = [
    ("常用生图", [
        ("/画 /生图", "写想要的画面；附图或引用图时按图改，没有附图时按文字出图。"),
        ("/画 3 / -c 3", "指定本条指令总张数；任务会排队，不会把并发锁在单条指令里。"),
        ("/文生图", "只按文字生成，不使用自拍人设或形象图。"),
        ("/图生图", "必须附图或引用图，按原文改图，不自动使用形象图。"),
        ("/nai /nai5", "选择启用的 NAI 模型；可附图进行图生图。"),
    ]),
    ("自拍与形象", [
        ("/自拍", "使用当前形象自拍；可追加动作、场景、服装和 -c 数量。"),
        ("/看看你", "像别人随手拍你；可用 /看看COS 随机换装。"),
        ("/合影 /合照", "和对象同框；可附图或 @ 对方，自己使用当前形象。"),
        ("/形象设置", "发图设置主形象，也可写 自动、真人、动漫切换类型。"),
        ("/形象设置 编号", "切换 1-10 形象图；/形象移除 编号移除指定形象图。"),
        ("/形象人设添加", "添加文字形象人设；可用 /形象人设设置、/形象人设移除按编号管理。"),
    ]),
    ("模型与任务", [
        ("/生图模型", "查看模型列表；跟序号或 渠道/模型 切换当前会话默认模型。"),
        ("/生图状态", "查看最近 1h、24h、7d、30d 的成功率、耗时、错误和历史。"),
        ("/生图任务", "查看进行中的生图任务，包含 LLM 调用。"),
        ("/生图取消", "取消仍在排队或进行中的任务。"),
    ]),
    ("帮助与管理", [
        ("/生图帮助", "发送本图卡。"),
        ("/生图help", "查看完整说明图。"),
        ("/预设", "查看和管理图像转换、动作、上下身与特殊预设。"),
        ("管理员", "可用 /生图加黑、/生图加白、/生图释放管理权限名单。"),
    ]),
]

FULL_SECTIONS = [
    ("常用生图", [
        ("生图数量", "用 -c 指定张数，可放在提示词任意位置；旧的 /画 3 仍可用。"),
        ("/画 /生图", "有附图或引用图就按图改，没有附图就按文字出图，不自动带入形象图。"),
        ("引用即提示词", "引用文字消息后发 /画、/自拍 等，会把被引用文字当作提示词；自己写的正文优先。"),
        ("/文生图", "只用文字按原文出图，不走自拍人设，也不用形象图。"),
        ("/图生图", "必须附图或引用图，按原文改图，不自动使用形象图。"),
        ("/nai /nai5", "选择启用的 NAI 模型；可写画师名，附图可图生图。"),
        ("/自拍 /看看", "用当前形象自拍；可写动作、场景和换装。"),
        ("/看看腿", "随机展示日常袜装穿搭；可指定长裤、全身、姿势或自拍。"),
        ("/查看提示词", "按六位编号或最近查询最终实际提示词，也支持引用图片。"),
        ("/查看生图提示词", "由当前聊天 LLM 反推图片；反推编号可取回已有结果。"),
        ("/看看COS /看看你", "随机 COS 换装或像别人随手拍你；可固定视角、加 -c 和额外提示词。"),
        ("/合影 /合照", "和对象同框；可附图或 @ 对方，自己使用当前形象。"),
    ]),
    ("自动判断", [
        ("/画", "有图时按图生图，没有图时按文生图；不会自动塞形象图。"),
        ("自拍 / 合影 / 看看", "使用当前形象；形象类型可以设置为自动、真人或动漫。"),
        ("引用", "引用图是参考图，引用文字是提示词；两者可以同时使用。"),
    ]),
    ("模型与进度", [
        ("/生图模型", "查看列表；跟序号或 渠道/模型 切换当前群/私聊默认模型，发清除恢复默认。"),
        ("/生图状态", "查看最近 1h、24h、7d、30d 的成功率、耗时、错误和历史，可跟页码。"),
        ("/生图任务", "查看进行中的生图任务，包含 LLM 调用；可跟任务号。"),
        ("/生图取消", "取消仍在排队或进行中的任务。"),
        ("管理员名单", "管理员可用 /生图加黑、/生图加白、/生图释放管理权限名单。"),
    ]),
    ("形象管理", [
        ("/形象查看", "查看当前参考图、形象类型与今日状态。"),
        ("/形象设置", "发图设置主形象，也可写 自动、真人、动漫切换类型。"),
        ("/形象设置 编号", "选择 1-10 形象图；/形象移除 编号移除对应图片。"),
        ("/形象人设添加", "格式：名称 描述；/形象人设设置 编号选择；/形象人设移除 编号删除。"),
        ("/形象清除", "去掉当前参考图。"),
        ("/形象刷新", "刷新今日穿搭状态。"),
    ]),
    ("预设与说明", [
        ("/预设", "查看图像转换、动作、上下身和特殊预设；可使用变真人、变动漫、变猫娘、变Q版、变像素等。"),
        ("管理员预设", "管理员可用 /预设添加 名称:内容、/预设删除 名称维护预设。"),
        ("数量与排队", "一次写的数量是本条指令总张数；同时最多进行几张由并发上限决定，新任务自动排队。"),
        ("任务完成", "超过同时上限才等待，图好了会直接发送。"),
    ]),
    ("入口", [
        ("/生图帮助", "发送速查图卡。"),
        ("/生图help", "发送本页完整说明图。"),
        ("管理页", "按配置开启时可访问 Web 管理页，也可在 AstrBot 插件详情打开管理界面。"),
    ]),
]


def _wrap(draw: ImageDraw.ImageDraw, value: str, face: Any, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in str(value):
        candidate = current + char
        if current and draw.textlength(candidate, font=face) > width:
            lines.append(current)
            current = char
        else:
            current = candidate
    return lines + ([current] if current else [""])


def _fallback_font(size: int, *, bold: bool = False) -> Any:
    candidates = (
        "/system/fonts/NotoSansCJK-Bold.ttc" if bold else "/system/fonts/NotoSansCJK-Regular.ttc",
        "/system/fonts/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans-Bold.ttf" if bold else "/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans.ttf",
    )
    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _font(font_loader: Callable[..., Any] | None, size: int, *, bold: bool = False) -> Any:
    if callable(font_loader):
        try:
            face = font_loader(size, bold=bold)
        except TypeError:
            face = font_loader(size)
        if face is not None:
            return face
    return _fallback_font(size, bold=bold)


def _add_logo(canvas: Image.Image, logo_path: str, x: int, y: int, size: int) -> None:
    if not logo_path or not os.path.isfile(logo_path):
        return
    try:
        logo = Image.open(logo_path).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size, size), radius=18, fill=255)
        canvas.paste(logo, (x, y), mask)
    except Exception:
        return


def _item_height(draw: ImageDraw.ImageDraw, width: int, body: str, font_loader: Callable[..., Any] | None) -> int:
    body_face = _font(font_loader, 18)
    return 32 + max(1, len(_wrap(draw, body, body_face, width - 26))) * 25 + 10


def _draw_item(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, title: str, body: str, font_loader: Callable[..., Any] | None) -> int:
    title_face = _font(font_loader, 23, bold=True)
    body_face = _font(font_loader, 18)
    draw.ellipse((x, y + 9, x + 9, y + 18), fill=COLORS["accent"])
    text_x = x + 22
    draw.text((text_x, y), title, font=title_face, fill=COLORS["ink"])
    body_y = y + 32
    for line in _wrap(draw, body, body_face, width - 26)[:3]:
        draw.text((text_x, body_y), line, font=body_face, fill=COLORS["muted"])
        body_y += 25
    return body_y + 10


def _panel(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, height: int, title: str, font_loader: Callable[..., Any] | None) -> None:
    draw.rounded_rectangle((x, y, x + width, y + height), 16, fill=COLORS["panel"], outline=COLORS["line"], width=2)
    draw.rectangle((x, y, x + width, y + 8), fill=COLORS["accent"])
    draw.text((x + 24, y + 26), title, font=_font(font_loader, 25, bold=True), fill=COLORS["accent"])


def _render(sections: Iterable[tuple[str, list[tuple[str, str]]]], *, full: bool, logo_path: str, font_loader: Callable[..., Any] | None) -> Image.Image:
    sections = list(sections)
    image = Image.new("RGB", (WIDTH, 1800), COLORS["bg"])
    draw = ImageDraw.Draw(image)
    _add_logo(image, logo_path, MARGIN, 36, 104)
    draw.text((188, 40), "SELFIE IMAGE", font=_font(font_loader, 24, bold=True), fill=COLORS["accent"])
    draw.text((188, 76), "生图完整说明" if full else "生图指令速查", font=_font(font_loader, 48, bold=True), fill=COLORS["ink"])
    subtitle = "FULL COMMAND GUIDE" if full else "COMMANDS AT A GLANCE"
    draw.text((188, 138), f"{subtitle} · CLEAN", font=_font(font_loader, 20), fill=COLORS["muted"])
    draw.text((WIDTH - MARGIN, 54), "HELP" if full else "QUICK", font=_font(font_loader, 22, bold=True), fill=COLORS["accent"], anchor="ra")
    y = 198
    content_width = WIDTH - MARGIN * 2
    columns = 4 if full else 3
    col_width = (content_width - GAP * (columns - 1)) // columns

    title, items = sections[0]
    chunks = [items[index::columns] for index in range(columns)]
    first_height = max(sum(_item_height(draw, col_width, body, font_loader) for _, body in chunk) for chunk in chunks) + 100
    _panel(draw, MARGIN, y, content_width, first_height, title, font_loader)
    for column, chunk in enumerate(chunks):
        item_y = y + 82
        x = MARGIN + column * (col_width + GAP)
        for item_title, body in chunk:
            item_y = _draw_item(draw, x + 24, item_y, col_width - 48, item_title, body, font_loader)
    y += first_height + GAP

    for start in range(1, len(sections), columns):
        row = sections[start:start + columns]
        heights = [96 + sum(_item_height(draw, col_width - 48, body, font_loader) for _, body in items) for _, items in row]
        row_height = max(heights)
        for column, (title, items) in enumerate(row):
            x = MARGIN + column * (col_width + GAP)
            _panel(draw, x, y, col_width, row_height, title, font_loader)
            item_y = y + 82
            for item_title, body in items:
                item_y = _draw_item(draw, x + 24, item_y, col_width - 48, item_title, body, font_loader)
        y += row_height + GAP

    draw.line((MARGIN, y, WIDTH - MARGIN, y), fill=COLORS["line"], width=2)
    footer = "/生图帮助 速查图　/生图help 完整说明图" if full else "/生图help 查看完整说明"
    draw.text((MARGIN, y + 22), footer, font=_font(font_loader, 22, bold=True), fill=COLORS["accent"])
    return image.crop((0, 0, WIDTH, y + 74))


def _write_atomic(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    try:
        image.save(temporary, format="PNG", optimize=True)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def ensure_help_images(data_dir: str, *, logo_path: str = "", font_loader: Callable[..., Any] | None = None) -> Mapping[str, str]:
    """Create both help images once per layout version and return their paths."""
    root = Path(data_dir)
    quick_path = root / "help_poster.png"
    full_path = root / "help_poster_full.png"
    marker_path = root / "help_poster.version"
    try:
        marker = marker_path.read_text(encoding="ascii").strip() if marker_path.is_file() else ""
        valid = marker == HELP_IMAGE_LAYOUT_VERSION and quick_path.is_file() and full_path.is_file()
        if not valid:
            _write_atomic(_render(QUICK_SECTIONS, full=False, logo_path=logo_path, font_loader=font_loader), quick_path)
            _write_atomic(_render(FULL_SECTIONS, full=True, logo_path=logo_path, font_loader=font_loader), full_path)
            marker_path.write_text(HELP_IMAGE_LAYOUT_VERSION + "\n", encoding="ascii")
        return {"quick": str(quick_path), "full": str(full_path)}
    except Exception:
        return {"quick": "", "full": ""}
