"""Exercise NAI routing, persisted artist management and final provider tags."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from astrbot_plugin_selfie_image.core.models import AICatConfig, ImageModelTarget
from astrbot_plugin_selfie_image.core.providers import (
    ImageGenerateRequest, ImageReference, NovelAIImageAdapter, novelai_model_family,
)
from astrbot_plugin_selfie_image.main import SelfieImagePlugin
from astrbot_plugin_selfie_image.nai.artist_presets import NaiArtistPresetManager
from astrbot_plugin_selfie_image.nai.prompts import compose_nai_prompt, extract_artist_request, select_nai_targets
from astrbot_plugin_selfie_image.prompts.preset import ImagePresetManager
from astrbot_plugin_selfie_image.webui.web import FlaskWebServer
from astrbot_plugin_selfie_image.webui.dashboard_api import SelfieImageDashboardAPI
import astrbot_plugin_selfie_image.webui.dashboard_api as dashboard_api


def config_with_models(models, **extra):
    return AICatConfig.from_dict({
        "image_channels": [{
            "name": "relay", "provider_type": "openai", "api_key": "test",
            "base_url": "https://relay.test/v1", "enabled_models": models,
        }], **extra,
    })


@pytest.mark.parametrize("model", ["nai5", "nai-5-full", "nai-diffusion-5-full", "novelai-v5", "NAI5_Full"])
def test_nai5_family_names(model):
    assert novelai_model_family(model) == "nai5"


def test_commands_use_enabled_models_outside_global_priority_and_mode():
    config = config_with_models(
        ["gpt-image-2", "nai5", "nai4.5", "nai-diffusion-4-5-full"],
        enabled_image_model_priority=["relay/gpt-image-2"], image_model_call_mode="fixed",
    )
    assert [target.model for target in config.get_prioritized_targets()] == ["gpt-image-2"]
    targets = config.get_enabled_image_targets()
    assert [target.model for target in select_nai_targets(targets)] == ["nai4.5", "nai-diffusion-4-5-full", "nai5"]
    assert [target.model for target in select_nai_targets(targets, v5_only=True)] == ["nai5"]


def test_disabled_nai_channels_and_models_are_never_selected():
    config = AICatConfig.from_dict({"image_channels": [
        {"name": "off", "enabled": False, "provider_type": "novelai", "enabled_models": ["nai4.5"]},
        {"name": "on", "provider_type": "novelai", "enabled_models": ["nai5"], "models_cache": ["nai4.5", "nai5"]},
    ]})
    targets = select_nai_targets(config.get_enabled_image_targets())
    assert [target.label for target in targets] == ["on/nai5"]
    assert select_nai_targets(config_with_models(["gpt-image-2"]).get_enabled_image_targets()) == []


def test_legacy_or_unknown_nai_models_are_not_misclassified_as_nai4():
    targets = [
        ImageModelTarget("relay", "novelai", "https://relay.test", "test", model, 30)
        for model in ("nai-diffusion-3", "custom-nai", "novelai-v4.5-full", "nai5")
    ]
    assert [target.model for target in select_nai_targets(targets)] == ["novelai-v4.5-full", "nai5"]


def test_artist_management_is_persistent_and_isolated_from_image_presets(tmp_path):
    plugin = object.__new__(SelfieImagePlugin)
    plugin.presets = ImagePresetManager(str(tmp_path))
    plugin.nai_artist_presets = NaiArtistPresetManager(str(tmp_path))
    image_names = {row["name"] for row in plugin.list_managed_prompt_presets_for_web()}
    builtins = plugin.list_managed_prompt_presets_for_web("nai")
    assert len(builtins) >= 5
    plugin.save_prompt_preset_from_web({"kind": "nai", "name": "test blend", "prompt": "1.2::artist:a, artist:b::"})
    assert plugin.nai_artist_presets.find_artist("TEST BLEND")[1] == "1.2::artist:a, artist:b::"
    assert not plugin.presets.has_preset("test blend")
    assert {row["name"] for row in plugin.list_managed_prompt_presets_for_web()} == image_names
    plugin.import_prompt_presets_from_web({"kind": "nai", "presets": [{"name": "imported", "prompt": "artist:c"}]})
    assert plugin.nai_artist_presets.find_artist("imported")[1] == "artist:c"
    removed = builtins[0]["name"]
    plugin.delete_prompt_preset_from_web(removed, "nai")
    reloaded = NaiArtistPresetManager(str(tmp_path))
    assert reloaded.find_artist(removed) == ("", "")
    assert reloaded.find_artist("test blend")[1] == "1.2::artist:a, artist:b::"
    plugin.save_prompt_preset_from_web({"kind": "nai", "original_name": "test blend", "name": "renamed", "prompt": "artist:d"})
    assert plugin.nai_artist_presets.find_artist("test blend") == ("", "")
    assert plugin.nai_artist_presets.find_artist("renamed")[1] == "artist:d"


@pytest.mark.parametrize("query", ["柔和色彩 portrait", "[柔和色彩] portrait", "【柔和色彩】 portrait", "portrait --artist 柔和色彩"])
def test_artist_selection_keeps_user_prompt_separate(tmp_path, query):
    manager = NaiArtistPresetManager(str(tmp_path))
    assert extract_artist_request(query, manager) == ("portrait", "柔和色彩")
    assert extract_artist_request("[artist:weighted] portrait", manager) == ("[artist:weighted] portrait", "")
    with pytest.raises(ValueError, match="找不到"):
        extract_artist_request("portrait --artist does-not-exist", manager)


def test_weighted_artist_groups_survive_composition_and_quality_is_not_duplicated():
    artist = "1.2::artist:a, artist:b::, {artist:c, artist:d}"
    prompt = compose_nai_prompt("1girl, best quality, 1girl", artist, "best quality, amazing quality")
    assert prompt.startswith(artist + ", ")
    assert prompt.count("best quality") == 1
    assert prompt.count("1girl") == 1
    adapter = NovelAIImageAdapter(ImageModelTarget("relay", "novelai", "https://relay.test", "test", "nai4.5", 30), None)
    payload = adapter.build_relay_payload(ImageGenerateRequest(prompt=prompt))
    import json
    assert json.loads(payload["messages"][1]["content"])["prompt"] == prompt


class Event:
    def __init__(self, message, session="session-a"):
        self.message_str = message
        self.unified_msg_origin = session

    def plain_result(self, text):
        return text

    def get_sender_id(self):
        return "user"


def plugin_for_nai(tmp_path, models=("gpt-image-2", "nai5", "nai4.5"), refs=()):
    plugin = object.__new__(SelfieImagePlugin)
    plugin.data_dir = str(tmp_path)
    plugin._session_model_lock = threading.RLock()
    plugin.nai_artist_presets = NaiArtistPresetManager(str(tmp_path))
    plugin.config = config_with_models(list(models), enabled_image_model_priority=["relay/gpt-image-2"])
    plugin._claim_media_command_event = Mock()
    plugin._quota_error_message = Mock(return_value="")
    plugin._rate_limit_error_message = Mock(return_value="")
    plugin._record_bot_text_context = Mock()
    plugin._set_web_image_task = Mock()
    plugin._session_key = lambda event: event.unified_msg_origin
    plugin._event_reference_images_with_stats = AsyncMock(return_value=(list(refs), len(refs), 0))
    plugin._call_text_llm = AsyncMock(return_value='{"prompt":"1girl, cherry blossoms, best quality"}')
    plugin._run_image_generation = AsyncMock(return_value={"success": True, "files": []})
    captured = {}

    def start(event, **kwargs):
        captured.update(kwargs)
        return {"id": "cmd-test"}

    async def run_counted(**kwargs):
        captured["shots"] = kwargs["total"]
        return [await kwargs["run_one"](index) for index in range(kwargs["total"])]

    plugin.start_command_image_task = start
    plugin._run_counted_generation_shots = run_counted
    return plugin, captured


def run_command(plugin, captured, text, *, v5=False, session="session-a"):
    async def run():
        command = plugin.cmd_nai5 if v5 else plugin.cmd_nai
        messages = [item async for item in command(Event(text, session))]
        if captured.get("runner"):
            await captured["runner"]("cmd-test")
        return messages
    return asyncio.run(run())


@pytest.mark.parametrize("v5,models,expected", [
    (False, ("gpt-image-2", "nai5", "nai4.5"), ["nai4.5", "nai5"]),
    (False, ("gpt-image-2", "nai5"), ["nai5"]),
    (True, ("gpt-image-2", "nai5", "nai4.5"), ["nai5"]),
])
def test_final_command_routes_only_nai_and_merges_artist_tags_once(tmp_path, v5, models, expected):
    plugin, captured = plugin_for_nai(tmp_path, models=models)
    command = "nai5" if v5 else "nai"
    messages = run_command(plugin, captured, f"/{command} [海报构图] 樱花树下 --ar 2:3 -c 2", v5=v5)
    assert "正在生成" in messages[0]
    assert captured["shots"] == 2
    call = plugin._run_image_generation.call_args
    final_prompt, aspect, _, refs = call.args
    assert [target.model for target in call.kwargs["targets"]] == expected
    assert aspect == "2:3"
    assert refs == []
    assert final_prompt.count("artist:ciloranko") == 1
    assert final_prompt.count("best quality") == 1
    assert "看看" not in final_prompt
    assert "--ar" not in final_prompt and "-c" not in final_prompt
    assert captured["summary"]["nai_artist"] == "海报构图"
    kwargs = plugin._event_reference_images_with_stats.call_args.kwargs
    assert kwargs["include_persona"] is False
    if expected[0] == "nai4.5":
        plugin._call_text_llm.assert_awaited_once()
        assert "樱花树下" not in final_prompt
        assert plugin._call_text_llm.call_args.kwargs["use_persona"] is False
    else:
        plugin._call_text_llm.assert_not_awaited()
        assert "樱花树下" in final_prompt


@pytest.mark.parametrize("v5,models", [(False, ("gpt-image-2",)), (True, ("nai4.5",))])
def test_missing_model_exits_without_falling_back_to_other_image_models(tmp_path, v5, models):
    plugin, captured = plugin_for_nai(tmp_path, models=models)
    messages = run_command(plugin, captured, "/nai5 portrait" if v5 else "/nai portrait", v5=v5)
    assert "没有已启用" in messages[0]
    assert "runner" not in captured
    plugin._run_image_generation.assert_not_awaited()


def test_reference_images_are_reversed_without_persona_or_second_rewrite(tmp_path):
    ref = ImageReference(data=b"image", mime_type="image/png")
    plugin, captured = plugin_for_nai(tmp_path, refs=[ref])
    original_targets = plugin.config.get_enabled_image_targets()
    for target in original_targets:
        target.extra.update({"prompt_en_enabled": True, "image_to_text_enabled": True})
    plugin.config.get_enabled_image_targets = lambda: original_targets
    run_command(plugin, captured, "/NAI [柔和色彩] 1girl, blue dress")
    call = plugin._run_image_generation.call_args
    assert call.args[3] == []
    assert call.kwargs["record_context"]["nai_reference"]["references"][0]["source"] == "vision"
    selected = plugin._run_image_generation.call_args.kwargs["targets"]
    assert all(not target.extra["prompt_en_enabled"] and not target.extra["image_to_text_enabled"] for target in selected)
    assert all(target.extra["prompt_en_enabled"] and target.extra["image_to_text_enabled"] for target in original_targets)
    plugin._call_text_llm.assert_awaited_once()
    assert plugin._call_text_llm.call_args.kwargs["images"] == [ref.data]
    assert plugin._call_text_llm.call_args.kwargs["use_persona"] is False


def test_empty_description_with_reference_still_generates(tmp_path):
    plugin, captured = plugin_for_nai(tmp_path, refs=[ImageReference(data=b"image", mime_type="image/png")])
    run_command(plugin, captured, "/nai5", v5=True)
    final = plugin._run_image_generation.call_args.args[0]
    assert "cherry blossoms" in final
    assert "根据参考图" not in final
    assert plugin._run_image_generation.call_args.args[3] == []


def test_reference_edits_replace_old_attributes_and_batch_resolves_once(tmp_path):
    from io import BytesIO
    import json
    from PIL import Image, PngImagePlugin

    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Software", "NovelAI")
    metadata.add_text("Comment", json.dumps({
        "prompt": "1girl, red dress, sitting, black hair, artist:old, best quality",
        "seed": 1, "steps": 28,
    }))
    buffer = BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, format="PNG", pnginfo=metadata)
    plugin, captured = plugin_for_nai(tmp_path, refs=[ImageReference(data=buffer.getvalue(), mime_type="image/png")])
    plugin._call_text_llm.return_value = '{"prompt":"1girl, blue dress, standing, pink hair, pink bangs"}'
    run_command(plugin, captured, "/nai 换蓝裙站起来，粉色头发包括刘海 -c 3")

    plugin._call_text_llm.assert_awaited_once()
    call = plugin._call_text_llm.call_args
    assert call.kwargs["images"] == []
    assert '"prompt": "1girl, red dress, sitting, black hair"' in call.args[1]
    assert "replace every conflicting old attribute" in call.args[1]
    assert '"user_instruction": "换蓝裙站起来，粉色头发包括刘海"' in call.args[1]
    assert plugin._run_image_generation.await_count == 3
    for shot in plugin._run_image_generation.call_args_list:
        final = shot.args[0]
        assert "blue dress, standing, pink hair, pink bangs" in final
        assert not any(old in final for old in ("red dress", "sitting", "black hair", "artist:old"))
        assert final.count("best quality") == final.count("artist:ciloranko") == 1
        assert shot.args[3] == []


@pytest.mark.parametrize("response", ["", "cannot inspect image", '{"ok":false}', '{"prompt":[]}', '{"prompt":"best quality"}'])
def test_failed_reference_reverse_stops_before_image_generation(tmp_path, response):
    plugin, captured = plugin_for_nai(tmp_path, refs=[ImageReference(data=b"image", mime_type="image/png")])
    plugin._call_text_llm.return_value = response
    with pytest.raises(RuntimeError, match="未发送生图请求"):
        run_command(plugin, captured, "/nai 换成蓝裙")
    plugin._run_image_generation.assert_not_awaited()


def test_reference_reverse_exception_stops_before_image_generation(tmp_path):
    plugin, captured = plugin_for_nai(tmp_path, refs=[ImageReference(data=b"image", mime_type="image/png")])
    plugin._call_text_llm.side_effect = RuntimeError("provider failed")
    with pytest.raises(RuntimeError, match="未发送生图请求"):
        run_command(plugin, captured, "/nai5", v5=True)
    plugin._run_image_generation.assert_not_awaited()


@pytest.mark.parametrize("raises", [False, True])
def test_chinese_translation_failure_keeps_original_and_artist_weights(tmp_path, raises):
    plugin, captured = plugin_for_nai(tmp_path)
    plugin._call_text_llm.return_value = ""
    if raises:
        plugin._call_text_llm.side_effect = RuntimeError("text model unavailable")
    run_command(plugin, captured, "/nai [海报构图] 樱花树下")
    assert "樱花树下" in plugin._run_image_generation.call_args.args[0]
    assert "{artist:menthako}" in plugin._run_image_generation.call_args.args[0]


def test_session_artist_switch_is_isolated_persistent_and_resettable(tmp_path):
    plugin, captured = plugin_for_nai(tmp_path)
    message = run_command(plugin, captured, "/nai [海报构图]")
    assert "设为" in message[0]
    assert "runner" not in captured
    plugin, captured = plugin_for_nai(tmp_path)
    run_command(plugin, captured, "/nai portrait")
    assert captured["summary"]["nai_artist"] == "海报构图"
    captured.clear()
    run_command(plugin, captured, "/nai portrait", session="another-session")
    assert captured["summary"]["nai_artist"] == "柔和色彩"
    captured.clear()
    run_command(plugin, captured, "/nai 恢复默认")
    assert "runner" not in captured
    run_command(plugin, captured, "/nai portrait")
    assert captured["summary"]["nai_artist"] == "柔和色彩"


def test_quota_denial_prevents_reference_collection_and_task_creation(tmp_path):
    plugin, captured = plugin_for_nai(tmp_path)
    plugin._quota_error_message.return_value = "quota denied"
    assert run_command(plugin, captured, "/nai portrait") == ["quota denied"]
    plugin._event_reference_images_with_stats.assert_not_awaited()
    assert "runner" not in captured


@pytest.mark.parametrize("surface", ["flask", "dashboard"])
def test_artist_api_crud_pagination_search_and_image_isolation(tmp_path, monkeypatch, surface):
    plugin, _ = plugin_for_nai(tmp_path)
    plugin.presets = ImagePresetManager(str(tmp_path))
    image_names = {item["name"] for item in plugin.list_managed_prompt_presets_for_web()}
    if surface == "flask":
        client = FlaskWebServer(plugin)._create_app().test_client()

        def request(action="", payload=None, query=None):
            url = "/api/prompt-presets/manage" + ("/" + action if action else "")
            headers = {"Authorization": f"Bearer {plugin.config.web_token}"}
            response = client.post(url, json=payload, headers=headers) if action else client.get(url, query_string=query, headers=headers)
            assert response.status_code == 200
            result = response.get_json()
            assert result["success"]
            return result["data"]
    else:
        api = SelfieImageDashboardAPI(plugin)
        monkeypatch.setattr(dashboard_api, "json_response", lambda payload, **_: payload)
        handlers = {"": api.page_prompt_presets_manage, "save": api.page_prompt_preset_save,
                    "delete": api.page_prompt_preset_delete, "import": api.page_prompt_presets_import}

        def request(action="", payload=None, query=None):
            monkeypatch.setattr(dashboard_api, "request", SimpleNamespace(
                query=query or {}, json=AsyncMock(return_value=payload),
            ))
            result = asyncio.run(handlers[action]())["data"]
            assert result["success"]
            return result["data"]

    request("import", {"kind": "nai", "presets": [
        {"name": f"batch-{index:02}", "prompt": "artist:a"} for index in range(65)
    ]})
    page = request(query={"kind": "nai", "search": "batch-", "offset": 30, "limit": 30})
    assert page["total"] == 65 and len(page["items"]) == 30 and page["offset"] == 30
    assert all(item["name"].startswith("batch-") for item in page["items"])
    request("save", {"kind": "nai", "name": "api blend", "prompt": "1.2::artist:a, artist:b::"})
    assert request(query={"kind": "nai", "search": "api blend", "limit": 30})["items"][0]["prompt"] == "1.2::artist:a, artist:b::"
    request("delete", {"kind": "nai", "name": "api blend"})
    assert request(query={"kind": "nai", "search": "api blend", "limit": 30})["total"] == 0
    assert {item["name"] for item in plugin.list_managed_prompt_presets_for_web()} == image_names


@pytest.mark.parametrize("global_translation", [False, True])
def test_real_generation_pipeline_sends_resolved_tags_without_reference_payload(tmp_path, global_translation):
    from astrbot_plugin_selfie_image.core.providers import ImageGenerateResult

    ref = ImageReference(data=b"reference", mime_type="image/png")
    plugin, captured = plugin_for_nai(tmp_path, refs=[ref])
    plugin.config.image_enable_image_prompt_en = global_translation
    plugin._run_image_generation = SelfieImagePlugin._run_image_generation.__get__(plugin)
    plugin._select_healthy_generation_targets = lambda targets: (targets, [])
    plugin._save_reference_images_to_cache = lambda _refs: []
    plugin._source_context = lambda *_a, **_kw: {}
    plugin._cleanup_image_cache_if_needed = lambda _paths: {}
    plugin._composition_metadata = lambda *_a, **_kw: {}
    plugin._record_task = Mock()
    plugin._set_web_image_task = Mock()
    plugin._record_channel_health = Mock()
    plugin._set_generation_stage = Mock()
    plugin._is_stable_reference = lambda _ref: False
    plugin._audit_prompt = AsyncMock(return_value=(True, ""))
    plugin._describe_reference_images_for_generation = AsyncMock(side_effect=AssertionError("unexpected image description"))
    plugin._translate_prompt_to_english = AsyncMock(side_effect=AssertionError("unexpected artist translation"))
    requests = []

    async def generate(targets, request, _session, **kwargs):
        assert [target.model for target in targets] == ["nai4.5", "nai5"]
        for target in targets:
            prepared = kwargs["request_factory"](target)
            requests.append(prepared)
        return ImageGenerateResult(error="test stop")

    with patch("astrbot_plugin_selfie_image.main.generate_image_with_fallback", side_effect=generate):
        run_command(plugin, captured, "/nai [海报构图] 樱花树下")
    assert len(requests) == 2
    for request in requests:
        assert request.images == []
        assert "cherry blossoms" in request.prompt
        assert request.prompt.count("artist:ciloranko") == request.prompt.count("best quality") == 1
        assert "{artist:menthako}" in request.prompt
        adapter = NovelAIImageAdapter(plugin.config.get_enabled_image_targets()[-1], None)
        payload = adapter.build_official_payload(request)
        assert payload["action"] == "generate"
        assert "image" not in payload["parameters"]
        import json
        relay_payload = adapter.build_relay_payload(request)
        assert "image" not in json.loads(relay_payload["messages"][1]["content"])
    plugin._call_text_llm.assert_awaited_once()
    record = plugin._record_task.call_args.args[0]
    assert record["raw_reference_image_count"] == 1
    assert record["request_data"]["reference_image_count"] == 0
    assert record["request_data"]["nai_reference"]["mode"] == "pseudo_img2img"
    assert "cherry blossoms" in record["request_data"]["nai_reference"]["resolved_prompt"]
    plugin._translate_prompt_to_english.assert_not_awaited()
    plugin._describe_reference_images_for_generation.assert_not_awaited()
