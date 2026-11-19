"""Render the real help content and check its geometry and command coverage."""

import ast
import asyncio
from itertools import combinations
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw
import pytest

from astrbot_plugin_selfie_image.generation import help_poster as poster
from astrbot_plugin_selfie_image.main import SelfieImagePlugin


ROOT = Path(__file__).resolve().parents[1]


def render_and_check(monkeypatch, sections, *, full, font_loader=None):
    text_runs = []
    panels = []
    draw_text = ImageDraw.ImageDraw.text
    draw_panel = ImageDraw.ImageDraw.rounded_rectangle

    def capture_text(draw, xy, value, *args, **kwargs):
        box = draw.textbbox(xy, value, font=kwargs.get("font"), anchor=kwargs.get("anchor"))
        text_runs.append((draw._image, value, box, kwargs.get("fill")))
        return draw_text(draw, xy, value, *args, **kwargs)

    def capture_panel(draw, xy, *args, **kwargs):
        if kwargs.get("fill") == poster.COLORS["panel"]:
            panels.append(tuple(xy))
        return draw_panel(draw, xy, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(ImageDraw.ImageDraw, "text", capture_text)
        context.setattr(ImageDraw.ImageDraw, "rounded_rectangle", capture_panel)
        image = poster._render(sections, full=full, logo_path=str(ROOT / "logo.png"), font_loader=font_loader)

    boxes = []
    for canvas, value, (left, top, right, bottom), color in text_runs:
        if canvas is not image or not value:
            continue
        assert 0 <= left <= right <= image.width, value
        assert 0 <= top <= bottom <= image.height, value
        for x1, y1, x2, y2 in panels:
            if y1 <= top < y2 and x1 <= left < x2:
                assert x1 + 20 <= left <= right <= x2 - 20, value
                assert y1 + 20 <= top <= bottom <= y2 - 20, value
        boxes.append((value, left, top, right, bottom))
    for first, second in combinations(boxes, 2):
        assert (
            first[3] <= second[1] or second[3] <= first[1]
            or first[4] <= second[2] or second[4] <= first[2]
        ), (first[0], second[0])

    for color, field in ((poster.COLORS["ink"], 0), (poster.COLORS["muted"], 1)):
        rendered = "".join(value for canvas, value, _, fill in text_runs if canvas is image and fill == color)
        for _, items in sections:
            for item in items:
                assert item[field].replace("\n", "") in rendered, item[field]
    assert image.getpixel((0, image.height - 1)) == (244, 250, 248)
    return image


@pytest.mark.parametrize("full", [False, True], ids=["quick", "full"])
@pytest.mark.parametrize("font_scale", [1, 1.5], ids=["normal-font", "larger-font"])
def test_actual_posters_keep_every_line_inside_panels(monkeypatch, full, font_scale):
    def load_font(size, *, bold=False):
        return SelfieImagePlugin._status_report_font(round(size * font_scale), bold=bold)

    sections = poster.FULL_SECTIONS if full else poster.QUICK_SECTIONS
    render_and_check(monkeypatch, sections, full=full, font_loader=load_font)


def test_long_titles_and_bodies_are_not_clipped_at_three_lines_or_1800_pixels(monkeypatch):
    items = [
        ("很长的命令标题" * 5 + str(index), "服装细节清晰，描述需要完整保留。" * 10 + f"正文结束标记{index}")
        for index in range(9)
    ]
    sections = [("常用", [("/画", "自动判断")]), ("需要换行的分节标题" * 4, items)]
    image = render_and_check(monkeypatch, sections, full=True)
    assert image.height > 1800


def test_wrap_preserves_explicit_line_breaks():
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    assert poster._wrap(draw, "第一行\n\n第三行", poster._fallback_font(18), 500) == ["第一行", "", "第三行"]


def test_old_layout_rebuilds_once_then_reuses_both_images(tmp_path):
    (tmp_path / "help_poster.version").write_text("2026-11-16-wide-v1\n", encoding="ascii")
    for name in ("help_poster.png", "help_poster_full.png"):
        Image.new("RGB", (8, 8)).save(tmp_path / name)
    with patch.object(poster, "_render", wraps=poster._render) as render:
        paths = poster.ensure_help_images(str(tmp_path), logo_path=str(ROOT / "logo.png"))
        assert render.call_count == 2
        timestamps = {kind: Path(path).stat().st_mtime_ns for kind, path in paths.items()}
        assert poster.ensure_help_images(str(tmp_path)) == paths
        assert render.call_count == 2
    assert timestamps == {kind: Path(path).stat().st_mtime_ns for kind, path in paths.items()}
    for path in paths.values():
        with Image.open(path) as image:
            assert image.width == poster.WIDTH
            image.verify()


@pytest.mark.parametrize("kind,handler", [("quick", "cmd_help"), ("full", "cmd_help_text")])
@pytest.mark.parametrize("available", [False, True], ids=["text-fallback", "cached-image"])
def test_help_commands_use_cached_image_or_fallback_without_rendering(tmp_path, kind, handler, available):
    plugin = object.__new__(SelfieImagePlugin)
    plugin.config = SimpleNamespace(web_enable=True, web_host="127.0.0.1", web_port=14514)
    path = tmp_path / "help.png"
    if available:
        Image.new("RGB", (8, 8)).save(path)
    plugin._help_image_paths = {kind: str(path)}
    plugin._create_image_component = lambda image_path: ("image", image_path)
    event = SimpleNamespace(chain_result=lambda chain: chain, plain_result=lambda value: value)

    async def run():
        return [result async for result in getattr(plugin, handler)(event)]

    with patch.object(poster, "_render", side_effect=AssertionError("commands must not render posters")):
        results = asyncio.run(run())
    if available:
        assert results == [[("image", str(path))]]
    elif kind == "full":
        assert results == [plugin._help_text_body()]
        for title, items in poster.FULL_SECTIONS:
            assert title in results[0]
            assert all(command in results[0] and body in results[0] for command, body in items)
        assert "http://127.0.0.1:14514" in results[0]
    else:
        assert "/生图help" in results[0]


def test_full_help_and_readme_cover_registered_chat_commands():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    commands = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name) and decorator.func.value.id == "filter"
                and decorator.func.attr == "command" and decorator.args
            ):
                commands.add(ast.literal_eval(decorator.args[0]))
    help_content = "\n".join(
        command + " " + body for _, items in poster.FULL_SECTIONS for command, body in items
    )
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    pattern = r"/([\w]+)"
    assert commands <= set(re.findall(pattern, help_content))
    assert commands <= set(re.findall(pattern, readme))
