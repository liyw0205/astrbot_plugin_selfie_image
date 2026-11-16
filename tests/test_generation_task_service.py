from __future__ import annotations

import asyncio

from astrbot_plugin_selfie_image import main as main_module
from astrbot_plugin_selfie_image.generation.image_command_service import ImageCommandService
from astrbot_plugin_selfie_image.generation.image_prompt_service import ImagePromptService
from astrbot_plugin_selfie_image.generation.runtime_service import GenerationRuntimeService
from astrbot_plugin_selfie_image.generation.selfie_workflow import SelfieWorkflowService
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


def test_command_adapters_are_reused_and_refresh_parser_hooks(monkeypatch):
    plugin = object.__new__(SelfieImagePlugin)

    image_service = plugin._get_image_command_service()
    assert isinstance(image_service, ImageCommandService)
    assert plugin._get_image_command_service() is image_service

    selfie_service = plugin._get_selfie_workflow_service()
    assert isinstance(selfie_service, SelfieWorkflowService)
    assert plugin._get_selfie_workflow_service() is selfie_service

    parser = lambda *_args, **_kwargs: "patched"
    extractor = lambda *_args, **_kwargs: []
    monkeypatch.setattr(main_module, "extract_command_message", parser)
    monkeypatch.setattr(main_module, "extract_image_sources_from_event", extractor)

    assert plugin._get_selfie_workflow_service() is selfie_service
    assert selfie_service.extract_command_message is parser
    assert selfie_service.extract_image_sources is extractor

    task_service = plugin._get_generation_task_service()
    runtime_service = plugin._get_generation_runtime_service()
    assert isinstance(task_service, GenerationTaskService)
    assert isinstance(runtime_service, GenerationRuntimeService)
    assert plugin._get_generation_task_service() is task_service
    assert plugin._get_generation_runtime_service() is runtime_service


def test_image_prompt_adapter_is_reused_and_preserves_plugin_parser_contract():
    plugin = object.__new__(SelfieImagePlugin)
    plugin.config = type(
        "Config",
        (),
        {
            "image_default_aspect_ratio": "3:4",
            "image_default_resolution": "2K",
            "image_max_batch_count": 4,
        },
    )()

    service = plugin._get_image_prompt_service()

    assert isinstance(service, ImagePromptService)
    assert plugin._get_image_prompt_service() is service
    assert plugin._image_parser_defaults() == ("3:4", "2K")
    assert plugin._extract_command_count("2 测试", allow_trailing=True) == ("测试", 2)
