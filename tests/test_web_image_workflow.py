"""Public quick-draw behavior across workflow extraction boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_selfie_image.core.models import AICatConfig
from astrbot_plugin_selfie_image.core.providers import ImageReference
from astrbot_plugin_selfie_image.core.utils import bytes_to_data_url
from astrbot_plugin_selfie_image.main import SelfieImagePlugin


@pytest.fixture
def plugin():
    instance = object.__new__(SelfieImagePlugin)
    instance.config = AICatConfig.from_dict({"image": {"max_batch_count": 10}})
    instance.persona = SimpleNamespace(get_appearance_type=lambda: "auto")
    instance._validate_web_test_selection = Mock()
    instance._find_web_retry_target = Mock(return_value=SimpleNamespace(label="test/model"))
    instance.get_web_image_task = Mock(return_value={"trace_id": "gen-workflow"})
    instance._task_cancel_requested = Mock(return_value=False)
    instance._set_web_image_task = Mock()
    instance._prompt_en_needed = Mock(return_value=False)
    instance._record_task = Mock()
    instance._source_context = Mock(return_value={"source": "web-test"})
    instance._run_image_generation = AsyncMock(return_value=shot_result())
    return instance


def shot_result(index=1, **changes):
    return {
        "success": True,
        "image_paths": [f"generated/{index}.png"],
        "request_image_paths": [f"requests/{index}.png"],
        "used_model": "test/model",
        "elapsed_seconds": 0.25,
        "attempts": [{"success": True, "shot": index}],
        **changes,
    }


def test_result_aggregates_shots_and_preserves_request_links(plugin):
    plugin._run_image_generation.side_effect = [
        shot_result(),
        shot_result(2, request_data={"provider_field": 1}, response_data={"provider_field": 2}),
    ]
    result = asyncio.run(plugin.web_test_image({
        "prompt": "a portrait", "count": 2, "_task_id": "web-1",
        "_retry_record_id": "record-1", "prompt_enhance": False,
    }))

    assert result["status"] == "succeeded"
    assert result["generated_image_paths"] == ["generated/1.png", "generated/2.png"]
    assert result["request_image_paths"] == ["requests/1.png", "requests/2.png"]
    assert result["elapsed_seconds"] == 0.5
    assert result["completed_count"] == result["succeeded_count"] == 2
    assert result["request_data"]["provider_field"] == 1
    assert result["response_data"]["provider_field"] == 2
    assert result["request_data"]["trace_id"] == result["response_data"]["trace_id"] == "gen-workflow"
    assert "_task_id" not in result["request_data"]
    for call in plugin._run_image_generation.call_args_list:
        assert call.kwargs["max_attempts"] == 1
        assert call.kwargs["allow_compat_retry"] is False
        assert call.kwargs["source"] == "web-test"
        assert call.kwargs["record_context"] == {
            "task_id": "web-1", "trace_id": "gen-workflow", "requested_count": 2,
            "retry_record_id": "record-1",
        }
    assert plugin._set_web_image_task.call_args.kwargs["progress_percent"] == 100
    plugin._record_task.assert_not_called()


@pytest.mark.parametrize("succeeded", [0, 1])
def test_batch_stops_at_first_failed_shot(plugin, succeeded):
    plugin._run_image_generation.side_effect = [shot_result()] * succeeded + [
        shot_result(2, success=False, error="upstream failed", image_paths=[], elapsed_seconds="invalid"),
    ]
    result = asyncio.run(plugin.web_test_image({"prompt": "test", "count": 4, "_task_id": "web-1"}))

    assert plugin._run_image_generation.await_count == succeeded + 1
    assert result["success"] is False
    assert result["status"] == ("partial_success" if succeeded else "failed")
    assert result["error"] == "upstream failed"
    assert result["requested_count"] == 4
    assert result["completed_count"] == succeeded + 1
    assert result["succeeded_count"] == succeeded
    assert result["failed_count"] == 1
    assert result["generation_success"] is bool(succeeded)
    assert result["elapsed_seconds"] == succeeded * 0.25
    plugin._record_task.assert_not_called()


@pytest.mark.parametrize("completed", [0, 1])
def test_cancellation_keeps_completed_images_without_starting_more(plugin, completed):
    plugin._task_cancel_requested.side_effect = [False] * completed + [True, True]
    result = asyncio.run(plugin.web_test_image({"prompt": "test", "count": 3, "_task_id": "web-1"}))

    assert plugin._run_image_generation.await_count == completed
    assert result["success"] is False
    assert result["error"] == "任务已取消"
    assert result["completed_count"] == result["succeeded_count"] == completed
    assert result["failed_count"] == 0
    assert result["generation_success"] is bool(completed)
    assert result["generated_image_paths"] == (["generated/1.png"] if completed else [])


@pytest.mark.parametrize("explicit_trace", ["", "gen-explicit"])
def test_preflight_failure_records_inspectable_request_and_trace(plugin, explicit_trace):
    plugin._validate_web_test_selection.side_effect = RuntimeError("channel disabled")
    with pytest.raises(RuntimeError, match="channel disabled"):
        asyncio.run(plugin.web_test_image({
            "prompt": "test", "_task_id": "web-1", "_trace_id": explicit_trace,
            "_retry_record_id": "record-1", "images": ["invalid-image"],
        }))

    plugin._run_image_generation.assert_not_awaited()
    plugin._record_task.assert_called_once()
    record = plugin._record_task.call_args.args[0]
    trace = explicit_trace or "gen-workflow"
    assert record["trace_id"] == record["request_data"]["trace_id"] == record["response_data"]["trace_id"] == trace
    assert record["task_id"] == "web-1"
    assert record["retry_record_id"] == "record-1"
    assert record["response_data"]["stage"] == "web_test_preflight"
    assert record["request_data"]["raw_reference_image_count"] == 1
    assert "images" not in record["request_data"]
    assert record["generated_image_paths"] == []


def test_second_shot_exception_keeps_prior_request_cache_links(plugin):
    plugin._run_image_generation.side_effect = [shot_result(), RuntimeError("generation exception")]
    with pytest.raises(RuntimeError, match="generation exception"):
        asyncio.run(plugin.web_test_image({"prompt": "test", "count": 3}))

    assert plugin._run_image_generation.await_count == 2
    plugin._record_task.assert_called_once()
    assert plugin._record_task.call_args.args[0]["request_image_paths"] == ["requests/1.png"]


@pytest.mark.parametrize("enhance", [False, True])
def test_missing_persona_reference_is_recorded_before_generation(plugin, enhance):
    plugin._persona_identity_reference = Mock(return_value=None)
    plugin._build_selfie_prompt_and_refs_for_event = AsyncMock(return_value=("prompt", [], None))
    with pytest.raises(RuntimeError, match="自拍形象参考图"):
        asyncio.run(plugin.web_test_image({
            "prompt": "test", "use_selfie_reference": True, "prompt_enhance": enhance,
        }))
    plugin._run_image_generation.assert_not_awaited()
    plugin._record_task.assert_called_once()


@pytest.mark.parametrize("enhance", [False, True])
def test_variants_recompose_identity_reference_and_translation(plugin, enhance):
    identity = ImageReference(data=b"identity", mime_type="image/png")
    plugin._persona_identity_reference = Mock(return_value=identity)

    async def compose(_event, action, refs):
        return "enhanced: " + action, [identity] + refs, {"applied": True, "action": action}

    plugin._build_selfie_prompt_and_refs_for_event = AsyncMock(side_effect=compose)
    plugin.build_creative_variations = Mock(return_value=[{"prompt": "scene A"}, {"prompt": "scene B"}])
    result = asyncio.run(plugin.web_test_image({
        "prompt": "portrait", "count": 3, "use_selfie_reference": True,
        "variation_enabled": True, "variation_fields": "scene,pose", "prompt_enhance": enhance,
    }))

    assert result["success"] is True
    assert result["request_data"]["variation_fields"] == ["scene", "pose"]
    for call, scene in zip(plugin._run_image_generation.call_args_list, ["scene A", "scene B", "scene B"]):
        assert call.kwargs["prompt"] == ("enhanced: " if enhance else "") + scene
        assert call.kwargs["refs"] == [identity]
        assert call.kwargs["prompt_en_meta"] == ({"applied": True, "action": scene} if enhance else None)


@pytest.mark.parametrize("applied", [False, True])
def test_plain_prompt_translation_uses_only_applied_result(plugin, applied):
    plugin._prompt_en_needed.return_value = True
    plugin._translate_prompt_to_english = AsyncMock(return_value=("translated", {"applied": applied}))
    result = asyncio.run(plugin.web_test_image({"prompt": "original"}))

    assert result["success"] is True
    call = plugin._run_image_generation.call_args
    assert call.kwargs["prompt"] == ("translated" if applied else "original")
    assert call.kwargs["prompt_en_meta"] == {"applied": applied}
    assert result["original_prompt"] == "original"


def test_image_and_images_are_combined_in_order_with_identity_first(plugin):
    image_a = b"\x89PNG\r\n\x1a\n" + b"a" * 128
    image_b = b"\x89PNG\r\n\x1a\n" + b"b" * 128
    identity = ImageReference(data=b"identity", mime_type="image/png")
    plugin._persona_identity_reference = Mock(return_value=identity)
    result = asyncio.run(plugin.web_test_image({
        "prompt": "test", "images": [bytes_to_data_url(image_a, "image/png"), "invalid"],
        "image": bytes_to_data_url(image_b, "image/png"),
        "use_selfie_reference": True, "prompt_enhance": False,
    }))

    assert result["success"] is True
    refs = plugin._run_image_generation.call_args.kwargs["refs"]
    assert [ref.data for ref in refs] == [b"identity", image_a, image_b]
    assert result["reference_images"] == 3
    assert result["request_data"]["raw_reference_image_count"] == 3


@pytest.mark.parametrize("enhance", [False, True])
def test_text_persona_generates_without_identity_image_or_logo(plugin, tmp_path, enhance):
    from astrbot_plugin_selfie_image.features.persona import PersonaManager

    plugin.persona = PersonaManager(tmp_path)
    plugin.persona.set_identity_mode("persona")
    plugin._ensure_daily_selfie_schedule = AsyncMock()
    result = asyncio.run(plugin.web_test_image({
        "prompt": "站在窗边", "use_selfie_reference": True, "prompt_enhance": enhance,
    }))
    assert result["success"] is True
    request = plugin._run_image_generation.call_args.kwargs
    assert request["refs"] == []
    selected = plugin.persona.get_active_persona()
    assert request["prompt"].count(selected["prompt"]) == 1
    assert selected["name"] in request["prompt"]


def test_plain_text_persona_retry_keeps_existing_identity_without_duplication(plugin, tmp_path):
    from astrbot_plugin_selfie_image.features.persona import PersonaManager

    plugin.persona = PersonaManager(tmp_path)
    plugin.persona.set_identity_mode("persona")
    prompt = "角色设定：旧形象描述\n【角色设定结束】\n站在窗边"
    asyncio.run(plugin.web_test_image({
        "prompt": prompt, "use_selfie_reference": True, "prompt_enhance": False,
    }))
    assert plugin._run_image_generation.call_args.kwargs["prompt"] == " ".join(prompt.split())


@pytest.mark.parametrize("enhance", [False, True])
def test_empty_text_pool_stops_before_generation(plugin, tmp_path, enhance):
    from astrbot_plugin_selfie_image.features.persona import PersonaManager

    plugin.persona = PersonaManager(tmp_path)
    plugin.persona.remove_persona(1)
    plugin.persona.set_identity_mode("persona")
    with pytest.raises(RuntimeError, match="当前未选择形象人设"):
        asyncio.run(plugin.web_test_image({
            "prompt": "portrait", "use_selfie_reference": True, "prompt_enhance": enhance,
        }))
    plugin._run_image_generation.assert_not_awaited()


def test_coroutine_cancellation_propagates_without_recording_preflight_failure(plugin):
    plugin._run_image_generation.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(plugin.web_test_image({"prompt": "test"}))
    plugin._record_task.assert_not_called()
