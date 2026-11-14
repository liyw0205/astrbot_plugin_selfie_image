from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PREVIEW_DIR = ROOT / "pages" / "status-preview"


def load_preview_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample_status(count: int = 13) -> dict:
    return {
        "window_seconds": 86400,
        "updated_at": 2_000_000,
        "models": [{"label": f"channel/model-{index}"} for index in range(count)],
    }


def test_preview_prepare_clamps_pages_and_slices_custom_page_size(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.syspath_prepend(str(PREVIEW_DIR))
    renderer = load_preview_module("status_preview_renderer_test", PREVIEW_DIR / "renderer.py")
    status = sample_status()

    first, first_rows = renderer.prepare(status)
    assert (first["page"], first["pages"], first["start"], first["page_size"]) == (1, 3, 0, 6)
    assert [row["label"] for row in first_rows] == [f"channel/model-{i}" for i in range(6)]

    second, second_rows = renderer.prepare(status, page=2)
    assert second["start"] == 6
    assert [row["label"] for row in second_rows] == [f"channel/model-{i}" for i in range(6, 12)]

    last, last_rows = renderer.prepare(status, page=999)
    assert (last["page"], last["start"]) == (3, 12)
    assert [row["label"] for row in last_rows] == ["channel/model-12"]

    custom, custom_rows = renderer.prepare(status, page=2, page_size=4)
    assert (custom["page"], custom["pages"], custom["start"], custom["page_size"]) == (2, 4, 4, 4)
    assert [row["label"] for row in custom_rows] == [f"channel/model-{i}" for i in range(4, 8)]


def test_official_preview_design_receives_custom_pagination_once(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.syspath_prepend(str(PREVIEW_DIR))
    renderer = load_preview_module("status_preview_renderer_pagination", PREVIEW_DIR / "renderer.py")
    from generation import model_status_report

    observed = {}
    marker = object()

    def fake_render(status, **kwargs):
        observed.update(status=status, kwargs=kwargs)
        return marker

    monkeypatch.setattr(model_status_report, "render_model_status_report", fake_render)
    result = renderer.render_status_report(sample_status(), "3", page=2, page_size=4)

    assert result is marker
    assert observed["kwargs"]["page"] == 2
    assert observed["kwargs"]["page_size"] == 4
    assert len(observed["status"]["models"]) == 13


def test_preview_server_binds_only_to_loopback(monkeypatch):
    monkeypatch.syspath_prepend(str(PREVIEW_DIR))
    server_module = load_preview_module("status_preview_server_test", PREVIEW_DIR / "server.py")
    addresses = []
    closed = []

    class FakeServer:
        server_port = 8765

        def __init__(self, address, _handler):
            addresses.append(address)

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            closed.append(True)

    monkeypatch.setattr(server_module, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(server_module, "png", lambda *_args: (b"demo", (1, 1)))
    monkeypatch.setattr(sys, "argv", [str(PREVIEW_DIR / "server.py")])

    server_module.main()

    assert addresses == [("127.0.0.1", 8765)]
    assert closed == [True]
    assert set(server_module.SAMPLES) == {"mixed", "healthy", "empty", "stress"}


def test_status_command_falls_back_when_image_result_creation_fails(monkeypatch):
    import astrbot_plugin_selfie_image.main as main_module

    plugin = main_module.SelfieImagePlugin.__new__(main_module.SelfieImagePlugin)
    plugin._permission_denied_message = lambda _event: ""
    plugin.get_model_status = lambda **_kwargs: {"models": []}
    plugin._render_model_status_image = lambda _status, **_kwargs: "status.png"
    plugin._format_model_status_text = lambda _status, **_kwargs: "文字状态回退"
    monkeypatch.setattr(main_module, "extract_command_message", lambda *_args: "")

    class Event:
        @staticmethod
        def plain_result(text):
            return ("text", text)

        @staticmethod
        def image_result(_path):
            raise RuntimeError("image transport unavailable")

    async def collect():
        return [item async for item in plugin.cmd_image_model_status(Event())]

    assert asyncio.run(collect()) == [("text", "文字状态回退")]
