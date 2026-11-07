"""Read the active AstrBot persona without a second plugin persona setting."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger


@dataclass(frozen=True)
class FrameworkPersona:
    name: str = ""
    prompt: str = ""


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _normalize(persona: Any) -> FrameworkPersona:
    if persona is None:
        return FrameworkPersona()
    return FrameworkPersona(
        name=str(_field(persona, "name") or _field(persona, "persona_id") or "").strip(),
        prompt=str(_field(persona, "prompt") or _field(persona, "system_prompt") or "").strip(),
    )


async def resolve_framework_persona(context: Any, event: Any = None) -> FrameworkPersona:
    """Follow framework session rules; never share a persona across conversations."""
    manager = getattr(context, "persona_manager", None)
    if manager is None:
        return FrameworkPersona()
    origin = getattr(event, "unified_msg_origin", None)
    try:
        persona_id = None
        conversations = getattr(context, "conversation_manager", None)
        if origin and conversations is not None:
            cid = await conversations.get_curr_conversation_id(origin)
            if cid:
                conversation = await conversations.get_conversation(origin, cid)
                persona_id = _field(conversation, "persona_id")

        resolver = getattr(manager, "resolve_selected_persona", None)
        if origin and callable(resolver):
            config = context.get_config(origin)
            provider_settings = config.get("provider_settings", {})
            _, persona, _, _ = await resolver(
                umo=origin,
                conversation_persona_id=persona_id,
                platform_name=event.get_platform_name(),
                provider_settings=provider_settings,
            )
            return _normalize(persona)

        # Older AstrBot versions expose the default/persona APIs separately.
        if persona_id == "[%None]":
            return FrameworkPersona()
        if persona_id:
            if persona_id == "default":
                getter = getattr(manager, "get_persona_v3_by_id", None)
                if callable(getter):
                    return _normalize(getter(persona_id))
            return _normalize(await manager.get_persona(persona_id))
        config_getter = getattr(context, "get_config", None)
        if callable(config_getter):
            config = config_getter(origin)
            if config.get("provider_settings", {}).get("default_personality") == "[%None]":
                return FrameworkPersona()
        return _normalize(await manager.get_default_persona_v3(origin))
    except Exception as exc:
        logger.debug("[SelfieImage] 读取框架人格失败，使用中性描述: %s", type(exc).__name__)
        return FrameworkPersona()
