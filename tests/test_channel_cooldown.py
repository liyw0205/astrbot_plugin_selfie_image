"""Provider-wide failures stop batches without treating skipped targets as requests."""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_selfie_image.core.models import AICatConfig, ImageModelTarget
from astrbot_plugin_selfie_image.core.providers import ImageGenerateResult
from astrbot_plugin_selfie_image.generation.image_workflow import run_image_generation_workflow
from astrbot_plugin_selfie_image.main import SelfieImagePlugin


@pytest.fixture
def plugin(tmp_path):
    instance = object.__new__(SelfieImagePlugin)
    instance.config = AICatConfig.from_dict({})
    instance.data_dir = str(tmp_path)
    instance._channel_health = {}
    instance._channel_health_lock = threading.RLock()
    instance._source_context = Mock(return_value={})
    instance._set_generation_stage = Mock()
    instance._set_web_image_task = Mock()
    instance._save_reference_images_to_cache = Mock(return_value=[])
    instance._cleanup_image_cache_if_needed = Mock(return_value={})
    instance._composition_metadata = Mock(return_value={})
    instance._record_task = Mock()
    instance._audit_prompt = AsyncMock(return_value=(True, ""))
    instance._prompt_en_needed = Mock(return_value=False)
    instance._translate_prompt_to_english = AsyncMock()
    instance._describe_reference_images_for_generation = AsyncMock()
    return instance


def target(channel="channel"):
    return ImageModelTarget(channel, "openai", "https://image.test", "test-key", "model", 30)


@pytest.mark.parametrize("category", ["auth", "quota", "rate_limit", "timeout", "server", "network"])
def test_provider_failures_cool_down_and_synthetic_skips_preserve_health(plugin, category):
    attempt = {"channel": "channel", "success": False, "error_category": category}
    plugin._record_channel_health([attempt] * 3)
    before = plugin.get_channel_health()["channel"]
    history_before = list(plugin._load_channel_health_history())
    assert len(history_before) == 3
    assert before["cooling_down"] is True
    assert before["consecutive_failures"] == before["attempts"] == 3

    plugin._record_channel_health([
        {**attempt, "error_category": "cooldown", "error": "skipped"},
    ] * 20)

    after = plugin.get_channel_health()["channel"]
    for key in ("attempts", "failures", "consecutive_failures", "last_error_category", "cooldown_until"):
        assert after[key] == before[key]
    assert plugin._load_channel_health_history() == history_before

    plugin._record_channel_health([{"channel": "channel", "success": True}])
    recovered = plugin.get_channel_health()["channel"]
    assert recovered["cooling_down"] is False
    assert recovered["consecutive_failures"] == 0
    assert recovered["successes"] == 1


@pytest.mark.parametrize("cooling", [False, True])
def test_no_available_target_returns_before_auxiliary_requests_and_keeps_record_link(plugin, cooling):
    targets = [target()] if cooling else []
    plugin._resolve_generation_targets = Mock(return_value=[target("unexpected")])
    plugin._channel_health = {"channel": {"cooldown_until": time.time() + 60}}
    generate = AsyncMock()

    result = asyncio.run(run_image_generation_workflow(
        plugin, "test prompt", "9:16", "1K", [], targets=targets,
        record_context={"task_id": "task", "trace_id": "trace"},
        generate_images=generate,
    ))

    assert result["success"] is False
    assert bool(result.get("all_targets_cooling_down")) is cooling
    if cooling:
        assert 0 < result["cooldown_remaining_seconds"] <= 60
        assert result["attempts"][0]["error_category"] == "cooldown"
    else:
        assert "启用渠道和模型" in result["error"]
    plugin._resolve_generation_targets.assert_not_called()
    plugin._save_reference_images_to_cache.assert_not_called()
    plugin._audit_prompt.assert_not_awaited()
    plugin._translate_prompt_to_english.assert_not_awaited()
    plugin._describe_reference_images_for_generation.assert_not_awaited()
    generate.assert_not_awaited()
    record = plugin._record_task.call_args.args[0]
    assert record["task_id"] == "task"
    assert record["trace_id"] == "trace"
    assert record["response_data"]["stage"] == "select_model"


@pytest.mark.parametrize("other_cooling", [False, True])
def test_request_that_triggers_last_channel_cooldown_signals_batch_stop(plugin, other_cooling):
    if other_cooling:
        plugin._channel_health = {
            "other": {"cooldown_until": time.time() + 60},
        }
    plugin._record_channel_health([
        {"channel": "channel", "success": False, "error_category": "auth"},
    ] * 2)
    generate = AsyncMock(return_value=ImageGenerateResult(
        error="account disabled", attempts=[{
            "channel": "channel", "model": "model", "success": False,
            "error_category": "auth", "elapsed_seconds": 0.1,
        }],
    ))

    result = asyncio.run(run_image_generation_workflow(
        plugin, "test prompt", "9:16", "1K", [],
        targets=[target("other"), target()], generate_images=generate,
    ))

    assert bool(result.get("all_targets_cooling_down")) is other_cooling
    assert result["error"] == "account disabled"
    assert bool(result["response_data"].get("all_targets_cooling_down")) is other_cooling
    record = plugin._record_task.call_args.args[0]
    assert bool(record["response_data"].get("all_targets_cooling_down")) is other_cooling
    expected = ["channel"] if other_cooling else ["other", "channel"]
    assert [item.channel_name for item in generate.call_args.args[0]] == expected
    assert plugin.get_channel_health()["channel"]["attempts"] == 3
    assert "attempts" not in plugin.get_channel_health().get("other", {})
