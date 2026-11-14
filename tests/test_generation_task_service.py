from __future__ import annotations

import asyncio

from astrbot_plugin_selfie_image.generation.task_service import GenerationTaskService
from astrbot_plugin_selfie_image.main import SelfieImagePlugin


def test_task_lifecycle_facades_delegate_with_original_hook_arguments(monkeypatch):
    plugin = object.__new__(SelfieImagePlugin)
    calls = []

    def start(service, event, **kwargs):
        calls.append(("start", service.plugin, event, kwargs))
        return {"task_id": "cmd-test"}

    async def run(service, task_id, event, runner):
        calls.append(("run", service.plugin, task_id, event, runner))

    monkeypatch.setattr(GenerationTaskService, "start_command_image_task", start)
    monkeypatch.setattr(GenerationTaskService, "run_command_image_task", run)

    event = object()
    runner = object()
    result = plugin.start_command_image_task(
        event,
        source="command-draw",
        summary={"requested_count": 2},
        runner=runner,
        reference_images=["reference"],
    )
    asyncio.run(plugin._run_command_image_task("cmd-test", event, runner))

    assert result == {"task_id": "cmd-test"}
    assert calls == [
        (
            "start",
            plugin,
            event,
            {
                "source": "command-draw",
                "summary": {"requested_count": 2},
                "runner": runner,
                "reference_images": ["reference"],
            },
        ),
        ("run", plugin, "cmd-test", event, runner),
    ]


def test_cancel_facade_uses_service_and_preserves_keyword_contract(monkeypatch):
    plugin = object.__new__(SelfieImagePlugin)
    calls = []

    def cancel(service, task_id, **kwargs):
        calls.append((service.plugin, task_id, kwargs))
        return "已请求取消"

    monkeypatch.setattr(GenerationTaskService, "cancel_image_task", cancel)

    assert plugin.cancel_image_task("task-1", session_key="session-1", is_admin=True) == "已请求取消"
    assert calls == [(plugin, "task-1", {"session_key": "session-1", "is_admin": True})]
