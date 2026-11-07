from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot_plugin_selfie_image.core.models import AICatConfig
from astrbot_plugin_selfie_image.features.config_manager import ConfigurationMixin
from astrbot_plugin_selfie_image.features.framework_persona import FrameworkPersona, resolve_framework_persona
from astrbot_plugin_selfie_image.features.persona import PersonaManager
from astrbot_plugin_selfie_image.main import SelfieImagePlugin
from astrbot_plugin_selfie_image.prompts.prompt_templates import append_daily_context_to_english_prompt


def _event(origin="platform:GroupMessage:42"):
    return SimpleNamespace(unified_msg_origin=origin, get_platform_name=lambda: "aiocqhttp")


def test_framework_resolver_receives_current_conversation_and_session_config():
    event = _event()
    manager = SimpleNamespace(resolve_selected_persona=AsyncMock(return_value=(
        "session-rule", {"name": "session-rule", "prompt": "当前人格"}, "session-rule", False,
    )))
    conversations = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value="cid"),
        get_conversation=AsyncMock(return_value=SimpleNamespace(persona_id="conversation-persona")),
    )
    settings = {"default_personality": "default-persona"}
    context = SimpleNamespace(
        persona_manager=manager, conversation_manager=conversations,
        get_config=lambda origin: {"provider_settings": settings},
    )

    result = asyncio.run(resolve_framework_persona(context, event))

    assert result == FrameworkPersona("session-rule", "当前人格")
    conversations.get_conversation.assert_awaited_once_with(event.unified_msg_origin, "cid")
    manager.resolve_selected_persona.assert_awaited_once_with(
        umo=event.unified_msg_origin, conversation_persona_id="conversation-persona",
        platform_name="aiocqhttp", provider_settings=settings,
    )


def test_framework_resolver_does_not_override_disabled_persona():
    manager = SimpleNamespace(
        resolve_selected_persona=AsyncMock(return_value=("[%None]", None, None, False)),
        get_default_persona_v3=AsyncMock(),
    )
    context = SimpleNamespace(persona_manager=manager, get_config=lambda origin: {})

    assert asyncio.run(resolve_framework_persona(context, _event())) == FrameworkPersona()
    manager.get_default_persona_v3.assert_not_awaited()


def test_legacy_persona_api_reads_explicit_conversation_persona():
    manager = SimpleNamespace(
        get_persona=AsyncMock(return_value=SimpleNamespace(persona_id="custom", system_prompt="框架人设")),
        get_default_persona_v3=AsyncMock(),
    )
    conversation = SimpleNamespace(persona_id="custom")
    context = SimpleNamespace(
        persona_manager=manager,
        conversation_manager=SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="cid"),
            get_conversation=AsyncMock(return_value=conversation),
        ),
    )

    assert asyncio.run(resolve_framework_persona(context, _event())) == FrameworkPersona("custom", "框架人设")
    manager.get_default_persona_v3.assert_not_awaited()
    conversation.persona_id = "[%None]"
    assert asyncio.run(resolve_framework_persona(context, _event())) == FrameworkPersona()
    manager.get_persona.assert_awaited_once_with("custom")


def test_legacy_disabled_default_is_not_replaced_by_framework_fallback():
    manager = SimpleNamespace(get_default_persona_v3=AsyncMock())
    context = SimpleNamespace(
        persona_manager=manager,
        get_config=lambda origin: {"provider_settings": {"default_personality": "[%None]"}},
    )
    assert asyncio.run(resolve_framework_persona(context)) == FrameworkPersona()
    manager.get_default_persona_v3.assert_not_awaited()


def test_canvas_reads_default_persona_without_borrowing_a_chat_session():
    manager = SimpleNamespace(
        get_default_persona_v3=AsyncMock(return_value={"name": "global", "prompt": "默认人格"}),
        resolve_selected_persona=AsyncMock(),
    )
    context = SimpleNamespace(persona_manager=manager)
    assert asyncio.run(resolve_framework_persona(context)) == FrameworkPersona("global", "默认人格")
    manager.get_default_persona_v3.assert_awaited_once_with(None)
    manager.resolve_selected_persona.assert_not_awaited()


def test_persona_switches_are_read_fresh_and_failures_do_not_reuse_old_identity():
    manager = SimpleNamespace(get_default_persona_v3=AsyncMock(side_effect=[
        {"name": "A", "prompt": "人格A"}, {"name": "B", "prompt": "人格B"}, ValueError("missing"),
    ]))
    context = SimpleNamespace(persona_manager=manager)
    assert asyncio.run(resolve_framework_persona(context, _event("session-A"))).name == "A"
    assert asyncio.run(resolve_framework_persona(context, _event("session-B"))).name == "B"
    assert asyncio.run(resolve_framework_persona(context, _event("session-A"))) == FrameworkPersona()
    assert asyncio.run(resolve_framework_persona(None)) == FrameworkPersona()


def test_old_plugin_persona_config_is_removed_from_model_and_web_payload():
    legacy = {"botName": "旧称呼", "bot_name": "旧名字", "personality": "旧猫娘人设", "image": {"max_batch_count": 3}}
    config = AICatConfig.from_dict(legacy)
    cleaned = ConfigurationMixin._strip_web_startup_config(None, legacy)
    for key in ("botName", "bot_name", "personality"):
        assert key not in config.raw
        assert key not in cleaned
        assert not hasattr(config, key)
    assert config.image_max_batch_count == 3


def test_reply_llm_uses_session_provider_and_one_framework_persona():
    requester = AsyncMock(return_value=SimpleNamespace(completion_text="自然回复"))
    get_provider = Mock(return_value=SimpleNamespace(text_chat=requester))
    manager = SimpleNamespace(get_default_persona_v3=AsyncMock(return_value={"name": "A", "prompt": "框架人格A"}))
    plugin = SimpleNamespace(context=SimpleNamespace(persona_manager=manager, get_using_provider=get_provider))
    event = _event()

    reply = asyncio.run(SelfieImagePlugin._call_text_llm(plugin, event, "接一句", use_persona=True))

    assert reply == "自然回复"
    get_provider.assert_called_once_with(umo=event.unified_msg_origin)
    assert requester.call_args.kwargs == {"prompt": "接一句", "system_prompt": "框架人格A"}
    requester.reset_mock()
    manager.get_default_persona_v3.reset_mock()
    asyncio.run(SelfieImagePlugin._call_text_llm(plugin, event, "翻译为JSON"))
    assert requester.call_args.kwargs["system_prompt"] == ""
    manager.get_default_persona_v3.assert_not_awaited()


def test_reply_llm_fallback_api_receives_the_same_resolved_persona():
    generator = AsyncMock(return_value=SimpleNamespace(completion_text="备用接口回复"))
    context = SimpleNamespace(
        persona_manager=SimpleNamespace(get_default_persona_v3=AsyncMock(return_value={"prompt": "当前人格"})),
        get_current_chat_provider_id=AsyncMock(return_value="session-provider"),
        llm_generate=generator,
    )
    assert asyncio.run(SelfieImagePlugin._call_text_llm(
        SimpleNamespace(context=context), _event(), "接一句", use_persona=True,
    )) == "备用接口回复"
    generator.assert_awaited_once_with(
        chat_provider_id="session-provider", prompt="接一句", system_prompt="当前人格",
    )


def test_image_prompt_uses_framework_identity_only_without_identity_image(tmp_path):
    persona = PersonaManager(str(tmp_path))
    context = SimpleNamespace(persona_manager=SimpleNamespace(
        get_default_persona_v3=AsyncMock(return_value={"name": "框架名", "prompt": "成年女性\n银发蓝眼"}),
    ))
    plugin = SimpleNamespace(
        context=context, persona=persona,
        _ensure_daily_selfie_schedule=AsyncMock(),
        _persona_identity_reference=lambda **kwargs: None,
        _persona_auxiliary_references=lambda action: [],
        _select_selfie_references=lambda action, refs: None,
        _is_cos_action=lambda action: False,
    )
    prompt, refs = asyncio.run(SelfieImagePlugin._build_selfie_prompt_and_refs(plugin, "自拍", [], _event()))
    assert refs == []
    assert prompt.count("成年女性\n银发蓝眼") == 1
    english = append_daily_context_to_english_prompt("Create a portrait.", prompt)
    assert english.count("成年女性\n银发蓝眼") == 1
    identity_ref = object()
    plugin._persona_identity_reference = lambda **kwargs: identity_ref
    prompt, refs = asyncio.run(SelfieImagePlugin._build_selfie_prompt_and_refs(plugin, "自拍", [], _event()))
    assert refs == [identity_ref]
    assert "银发蓝眼" not in prompt
    assert "角色设定：" not in prompt


def test_leg_normalization_obeys_known_reference_origin_instead_of_image_presence(tmp_path):
    persona = PersonaManager(str(tmp_path))
    for external in (False, True):
        prompt = persona.build_selfie_prompt(
            "看看腿", "", "", has_reference_image=True, has_external_reference=external,
        )
        assert ("【outfit:reference】" in prompt) is external


def test_dashboard_has_no_separate_persona_controls():
    from astrbot_plugin_selfie_image.webui.index_fallback import INDEX_HTML

    assert "selfieBotName" not in INDEX_HTML
    assert "selfiePersonality" not in INDEX_HTML
    assert "人设（语气与氛围）" not in INDEX_HTML
