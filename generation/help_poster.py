"""Generate and cache the two Pillow help images used by the bot commands."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from PIL import Image, ImageDraw, ImageFont


HELP_IMAGE_LAYOUT_VERSION = "2026-11-24-commands-v4"
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
        ("/画 /生图", "自动判断：附图或引用图时按图改，没有图时按文字生成；不会自动塞形象图。"),
        ("/文生图 /图生图", "文生图只用文字；图生图必须附图或引用图。两者均不自动带入形象图。"),
        ("-c 3 / --ar 1:1", "-c 指定总张数，--ar 指定比例，--resolution 2K 指定分辨率；实际支持取决于模型。"),
        ("/nai /nai5", "nai 优先启用的 NAI4，其次 NAI5；nai5 只用 NAI5。附图会先反推，再仅用文字重新生成。"),
        ("/查看提示词", "按六位编号、最近或引用图片查最终提示词；/查看生图提示词 用辅助功能中选择的识图模型反推，未选择时用当前聊天 LLM。"),
        ("数量与排队", "-c 3 表示本条共生成 3 张；旧的 /画 3 仍兼容。新任务自动排队，生成完成后发送图片。"),
    ]),
    ("自拍与形象", [
        ("/自拍 /看看 /看看你", "自拍使用当前形象；看看你使用他拍视角。/合影、/合照 可附图或 @ 对象。"),
        ("/看看COS /看看腿", "COS 随机换装，可写角色、类别、视角或预设；看看腿默认腿部穿搭近中景。"),
        ("/形象设置 [编号]", "附图添加并选中，写 1-10 切换；/形象移除 编号 删除，空位优先补齐。"),
        ("/形象切换 人设池 / 形象池", "分别用 /形象切换 人设池 或 /形象切换 形象池，切换自拍使用的身份池。"),
        ("/形象人设添加 名称 描述", "添加文字外貌；/形象人设设置 编号 选择。用 /形象切换 或管理页选择使用哪种池。"),
        ("/衣橱 /衣橱添加", "查看衣橱或附图添加服装；/衣橱穿 编号 指定今天穿哪套。"),
        ("/今日日程 /形象查看", "查看日程、穿搭与当前形象；/形象刷新 刷新今日状态。"),
    ]),
    ("模型与任务", [
        ("/生图模型", "查看列表；跟序号或 渠道/模型 切换当前会话默认模型，发 清除 恢复默认。"),
        ("/生图状态 [时间] [页码]", "查看成功率、耗时、错误和历史；时间支持 1h、24h、7d、30d，默认 7d。"),
        ("/生图任务 /生图取消", "查看任务；取消时写任务号或列表编号，支持排队和进行中的任务。"),
        ("/生图重发", "重发本会话已生成但发送失败的缓存图片，不重新生成。"),
        ("/记录复用 记录ID", "从生成记录详情获取 ID，可追加修改要求，提交重新生成任务。"),
    ]),
    ("帮助与管理", [
        ("/生图帮助 /生图help", "分别发送速查图和完整说明图；完整图不可用时返回文字说明。"),
        ("/看看COS 列表", "浏览 COS 池标题；也支持 全部、查看。"),
        ("/nai 画师", "查看画师名称；/nai 画师名 设置会话默认，/nai 默认 恢复；[画师名] 提示词 可临时使用。"),
        ("/预设 [页码]", "查看预设，名称可写入生图指令；管理员可用 /预设添加、/预设删除 维护。"),
        ("管理员名单", "用 /生图加黑、/生图加白、/生图释放，跟 ID 或 @用户 管理名单。"),
        ("管理面板", "AstrBot 插件详情打开 dashboard；独立 Web 地址与 Token 以插件配置为准。"),
    ]),
]

FULL_SECTIONS = [
    ("常用生图", [
        ("/画 /生图", "自动判断：有附图或引用图就按图改，没有图就按文字生成；不会自动塞形象图。"),
        ("/文生图", "只用文字按原文出图，不走自拍人设，也不用形象图。"),
        ("/图生图", "必须附图或引用图，按原文改图，不自动使用形象图。"),
        ("/自拍 /看看", "用当前形象自拍；可写动作、场景和换装。"),
        ("/看看你", "以他拍视角生成当前形象的日常照片，可追加画面要求。"),
        ("/看看COS", "从 COS 池换装；可写角色、类别、预设和额外提示，支持 -c 数量。未指定视角时随机他拍、第一视角或自拍。"),
        ("/看看COS 列表", "浏览池内标题；也支持 全部、查看。明确写 他拍、第一视角 或 自拍 可固定视角。"),
        ("/看看腿", "默认腿部穿搭近中景，上身和头脸可自然入画；可指定服装、姿势、视角或全身。全身或明确露脸使用第三人称，请求图服装会保留。"),
        ("/合影 /合照", "和对象同框；可附图或 @ 对方，自己使用当前形象。"),
    ]),
    ("参数与参考图", [
        ("数量 -c 3", "指定本条总张数，可放在提示词任意位置；旧的 /画 3 仍可用。年龄如 20 岁不会当作数量。"),
        ("--ar / --resolution", "如 --ar 1:1、--resolution 2K；比例、分辨率的实际支持取决于模型。"),
        ("引用文字", "引用文字后发 /画、/自拍 等，会作为提示词；自己写的正文优先。引用图作为参考图。"),
        ("@ 用户头像", "普通生图和 COS 等会过滤触发者与 bot 的 @头像，其他用户头像可作参考；合影保留。"),
        ("数量与并发", "总张数与「同时画几张上限」分别设置；新任务自动排队，超过并发上限才等待，图好后发送。"),
    ]),
    ("NovelAI", [
        ("/nai /nai5", "仅选启用的 NAI 模型，无需加入普通模型优先级；nai 优先 NAI4，再选 NAI5，nai5 只选 NAI5。"),
        ("/nai 画师 /nai 默认", "画师 查看名称，默认 恢复默认串；/nai 画师名 设置本群或私聊默认。nai 与 nai5 共用选择。"),
        ("[画师名] 提示词", "本次临时指定画师；管理页预设管理可编辑画师串。NAI4 中文转英文标签，NAI5 保留原文。"),
        ("附图后重新生成", "优先读取 PNG/EXIF 提示词，否则用当前 LLM 识图，再合并修改要求；只向 NAI 发文字，不传原图。"),
        ("批量附图", "一次任务仅反推、合并一次；失败则停止。不保证复现原图人物、构图或种子。"),
    ]),
    ("形象池", [
        ("/形象设置 [编号]", "附图、引用图或图片 URL 添加并选中；写 1-10 选已有图片，写 自动、真人、动漫 改类型。"),
        ("/形象切换 人设池 / 形象池", "分别用 /形象切换 人设池 或 /形象切换 形象池；切换后另一池内容保留。"),
        ("/形象移除 编号", "删除指定形象图；/形象清除 只删当前图。两池各 10 位，删除不重排，新增优先补空位。"),
        ("/形象人设添加 名称 描述", "添加固定外貌，也支持 名称=小玉 描述=外貌特征；添加后需按编号选择。"),
        ("/形象人设设置 编号", "选择文字人设；/形象人设移除 编号 删除。内置小玉、祈枝枝o.o、奶包菜、燕子回来了、蛋姐。"),
        ("图片池 / 文字池", "用 /形象切换 人设池 或 /形象切换 形象池；管理页开关也可切换。选编号不切模式，只使用当前选中项。人设不固定服装或动作。"),
        ("/形象查看 /形象刷新", "查看当前形象、类型与今日状态；刷新今日穿搭，不改变当天手动指定的衣橱服装。"),
    ]),
    ("衣橱与日程", [
        ("/衣橱 /衣橱添加", "查看衣橱；附图添加服装，可写 名称=、描述=、标签=，支持多图。"),
        ("/衣橱穿 编号或ID", "指定今天的主服装；次日按原轮换方式自动选衣。"),
        ("/衣橱轮换 顺序或随机", "每天自动选衣；当天重复查看或重启不会推进顺序。"),
        ("/衣橱启用 /衣橱停用", "跟编号或 ID 调整可选服装；/衣橱删除 编号或ID 删除服装。"),
        ("/今日日程", "查看今日事件、穿搭变化和衣橱来源；衣橱与形象日程共用一套选择。"),
    ]),
    ("模型、任务与记录", [
        ("/生图模型", "跟序号或 渠道/模型 切换当前群/私聊默认模型，发 清除 恢复默认。"),
        ("/生图状态 [时间] [页码]", "默认最近 7 天，可选 1h、24h、7d、30d；兼容 /生图记录状态。"),
        ("/生图任务 /生图取消", "查看任务可跟任务号；取消可跟任务号或列表编号，支持排队和进行中的任务。"),
        ("/生图重发", "重发本会话已生成但发送失败且仍缓存的图片，不重新生成。"),
        ("/记录复用 记录ID [修改要求]", "从生成记录详情取 ID，提交重新生成任务；兼容 /记录重生。"),
        ("/查看提示词 [六位编号|最近]", "查询最终提示词，也可引用图片；编号与正文分开发送。本人仅查当前会话，管理员可跨会话。"),
        ("/查看生图提示词 [六位编号]", "用所选识图模型反推图片，未选择时用当前聊天 LLM；反推编号取回已有结果，生图编号指定缓存图。"),
    ]),
    ("预设与权限", [
        ("/预设 [页码]", "查看列表；把预设名写入生图指令，如 /图生图 变猫娘。管理员用 /预设 查看 查详情。"),
        ("/预设添加 /预设删除", "管理员用 /预设添加 名称:内容 添加，/预设删除 名称 删除。"),
        ("动作与服装预设", "动作预设 随机动作；上身预设、下身预设 选择对应结构，特殊预设 同时选择上下身结构。"),
        ("/生图加黑 /生图加白", "管理员跟 ID 或 @用户，可追加理由；实际访问和额度以权限配置为准。"),
        ("/生图释放 ID或@用户", "管理员将该用户同时移出黑名单和白名单。"),
    ]),
    ("帮助与管理", [
        ("/生图帮助 /生图help", "分别发送速查图和完整说明图；完整图不可用时返回同内容的文字说明。"),
        ("AstrBot 内嵌页", "在插件详情打开 dashboard，跟随 AstrBot 登录态。"),
        ("独立 Web", "按配置开启后访问对应地址，使用插件 Web Token 登录；入口配置变更需重载插件。"),
    ]),
]


def _wrap(draw: ImageDraw.ImageDraw, value: str, face: Any, width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in str(value).split("\n"):
        current = ""
        for char in paragraph:
            candidate = current + char
            left, _, right, _ = draw.textbbox((0, 0), candidate, font=face)
            if current and max(right - left, draw.textlength(candidate, font=face)) > width:
                lines.append(current)
                current = char
            else:
                current = candidate
        lines.append(current)
    return lines


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


def _text_layout(draw: ImageDraw.ImageDraw, value: str, face: Any, width: int) -> tuple[list[str], int]:
    lines = _wrap(draw, value, face, width)
    boxes = [draw.textbbox((0, 0), line or "国Ag", font=face, anchor="lt") for line in lines]
    return lines, max(bottom - top for _, top, _, bottom in boxes) + 7


def _draw_lines(draw: ImageDraw.ImageDraw, x: int, y: int, lines: list[str], step: int, face: Any, color: str) -> int:
    for line in lines:
        left = draw.textbbox((0, 0), line, font=face, anchor="lt")[0]
        draw.text((x - left, y), line, font=face, fill=color, anchor="lt")
        y += step
    return y


def _item_height(draw: ImageDraw.ImageDraw, width: int, title: str, body: str, font_loader: Callable[..., Any] | None) -> int:
    title_lines, title_step = _text_layout(draw, title, _font(font_loader, 23, bold=True), width - 26)
    body_lines, body_step = _text_layout(draw, body, _font(font_loader, 18), width - 26)
    return len(title_lines) * title_step + 7 + len(body_lines) * body_step + 14


def _draw_item(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, title: str, body: str, font_loader: Callable[..., Any] | None) -> int:
    title_face = _font(font_loader, 23, bold=True)
    body_face = _font(font_loader, 18)
    draw.ellipse((x, y + 9, x + 9, y + 18), fill=COLORS["accent"])
    text_x = x + 22
    title_lines, title_step = _text_layout(draw, title, title_face, width - 26)
    body_lines, body_step = _text_layout(draw, body, body_face, width - 26)
    body_y = _draw_lines(draw, text_x, y, title_lines, title_step, title_face, COLORS["ink"]) + 7
    return _draw_lines(draw, text_x, body_y, body_lines, body_step, body_face, COLORS["muted"]) + 14


def _panel_header_height(draw: ImageDraw.ImageDraw, width: int, title: str, font_loader: Callable[..., Any] | None) -> int:
    lines, step = _text_layout(draw, title, _font(font_loader, 25, bold=True), width - 48)
    return 26 + len(lines) * step + 24


def _panel(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, height: int, title: str, font_loader: Callable[..., Any] | None) -> None:
    draw.rounded_rectangle((x, y, x + width, y + height), 16, fill=COLORS["panel"], outline=COLORS["line"], width=2)
    draw.rectangle((x, y, x + width, y + 8), fill=COLORS["accent"])
    face = _font(font_loader, 25, bold=True)
    lines, step = _text_layout(draw, title, face, width - 48)
    _draw_lines(draw, x + 24, y + 26, lines, step, face, COLORS["accent"])


def _render(sections: Iterable[tuple[str, list[tuple[str, str]]]], *, full: bool, logo_path: str, font_loader: Callable[..., Any] | None) -> Image.Image:
    sections = list(sections)
    faces: dict[tuple[int, bool], Any] = {}

    def load_font(size: int, *, bold: bool = False) -> Any:
        key = (size, bold)
        if key not in faces:
            faces[key] = _font(font_loader, size, bold=bold)
        return faces[key]

    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    header = []
    y = 40
    for value, size, color, bold in (
        ("SELFIE IMAGE", 24, COLORS["accent"], True),
        ("生图完整说明" if full else "生图指令速查", 48, COLORS["ink"], True),
        ("FULL COMMAND GUIDE" if full else "COMMANDS AT A GLANCE", 20, COLORS["muted"], False),
    ):
        face = load_font(size, bold=bold)
        lines, step = _text_layout(draw, value, face, WIDTH - MARGIN - 360)
        header.append((188, y, lines, step, face, color))
        y += len(lines) * step + 10
    y = max(y, 140) + 26
    content_width = WIDTH - MARGIN * 2
    columns = 4 if full else 3
    col_width = (content_width - GAP * (columns - 1)) // columns
    item_width = col_width - 48
    panels = []

    def panel_height(width: int, title: str, chunks: list[list[tuple[str, str]]]) -> int:
        return _panel_header_height(draw, width, title, load_font) + 20 + max(
            sum(_item_height(draw, item_width, item_title, body, load_font) for item_title, body in chunk)
            for chunk in chunks
        )

    # Measure with the exact drawing widths before allocating the final canvas.
    if sections:
        title, items = sections[0]
        chunks = [items[index::columns] for index in range(columns)]
        height = panel_height(content_width, title, chunks)
        panels.append((MARGIN, y, content_width, height, title, chunks))
        y += height + GAP

    for start in range(1, len(sections), columns):
        row = sections[start:start + columns]
        row_height = max(panel_height(col_width, title, [items]) for title, items in row)
        for column, (title, items) in enumerate(row):
            x = MARGIN + column * (col_width + GAP)
            panels.append((x, y, col_width, row_height, title, [items]))
        y += row_height + GAP

    footer = "/生图帮助 速查图　/生图help 完整说明图" if full else "/生图help 查看完整说明"
    footer_face = load_font(22, bold=True)
    footer_lines, footer_step = _text_layout(draw, footer, footer_face, content_width)
    image = Image.new("RGB", (WIDTH, y + 22 + len(footer_lines) * footer_step + 28), COLORS["bg"])
    draw = ImageDraw.Draw(image)
    _add_logo(image, logo_path, MARGIN, 36, 104)
    for x, text_y, lines, step, face, color in header:
        _draw_lines(draw, x, text_y, lines, step, face, color)
    draw.text((WIDTH - MARGIN, 54), "HELP" if full else "QUICK", font=load_font(22, bold=True), fill=COLORS["accent"], anchor="rt")
    for x, panel_y, width, height, title, chunks in panels:
        _panel(draw, x, panel_y, width, height, title, load_font)
        for column, chunk in enumerate(chunks):
            item_y = panel_y + _panel_header_height(draw, width, title, load_font)
            item_x = x + column * (col_width + GAP) + 24
            for item_title, body in chunk:
                item_y = _draw_item(draw, item_x, item_y, item_width, item_title, body, load_font)
    draw.line((MARGIN, y, WIDTH - MARGIN, y), fill=COLORS["line"], width=2)
    _draw_lines(draw, MARGIN, y + 22, footer_lines, footer_step, footer_face, COLORS["accent"])
    return image


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
