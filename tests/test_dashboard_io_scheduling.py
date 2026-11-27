"""Dashboard reads must not stall the shared AstrBot event loop."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from astrbot_plugin_selfie_image.webui import dashboard_api
from astrbot_plugin_selfie_image.webui.dashboard_api import SelfieImageDashboardAPI


@pytest.mark.parametrize(
    ("handler", "args", "controller", "operation"),
    [
        ("page_backups_options", (), "_backup_controller", "options"),
        ("page_backups_list", (), "_backup_controller", "list"),
        ("page_backups_create", (), "_backup_controller", "create"),
        ("page_backups_task", ("backup-1",), "_backup_controller", "task"),
        ("page_backups_delete", (), "_backup_controller", "delete"),
        ("page_test_image_task_status", ("web-12345678-1",), "_task_controller", "status"),
        ("page_studio_task", ("web-12345678-1",), "_task_controller", "status"),
        ("page_task_detail", ("web-12345678-1",), "_task_controller", "detail"),
        ("page_tasks", (), "_task_controller", "list_tasks"),
        ("page_tasks_export", (), "_task_controller", "export"),
        ("page_studio_get", ("studio-1",), "_canvas_controller", "studio_get"),
        ("page_studio_canvas_get", ("studio-1",), "_canvas_controller", "studio_canvas"),
        ("page_studio_gallery", (), "_canvas_controller", "gallery"),
    ],
)
def test_slow_dashboard_io_keeps_event_loop_responsive(
    monkeypatch, handler, args, controller, operation,
):
    entered = threading.Event()
    release = threading.Event()

    def slow_operation(*_args, **_kwargs):
        entered.set()
        assert release.wait(2), "event loop did not release the slow controller"
        payload = {"loaded": True}
        if controller == "_backup_controller":
            return payload
        return SimpleNamespace(data=payload, extra={}, message=None)

    async def json_payload(**_kwargs):
        return {}

    api = SelfieImageDashboardAPI(SimpleNamespace())
    setattr(api, controller, SimpleNamespace(**{operation: slow_operation}))
    monkeypatch.setattr(dashboard_api, "request", SimpleNamespace(query={}, json=json_payload))
    monkeypatch.setattr(api, "_ok", lambda data, **_extra: data)

    async def scenario():
        task = asyncio.create_task(getattr(api, handler)(*args))
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set(), "controller did not start"
            # The controller is still waiting, while this coroutine can run.
            assert not task.done()
            release.set()
            assert await task == {"loaded": True}
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_health_diagnostics_run_off_event_loop(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def slow_health(*_args):
        entered.set()
        assert release.wait(2), "event loop did not release the slow health check"
        return {"status": "ok"}

    monkeypatch.setattr(dashboard_api, "build_health_payload", slow_health)
    monkeypatch.setattr(dashboard_api, "dashboard_page_source_status", lambda: {})
    monkeypatch.setattr(dashboard_api, "json_response", lambda payload, **_: payload)
    api = SelfieImageDashboardAPI(SimpleNamespace())
    monkeypatch.setattr(api, "_ok", lambda data, **_extra: data)

    async def scenario():
        task = asyncio.create_task(api.page_health())
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set(), "health diagnostics did not start"
            assert not task.done()
            release.set()
            assert await task == {"status": "ok"}
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
