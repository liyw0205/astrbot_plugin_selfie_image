"""Test-only AstrBot compatibility stubs for offline unit tests."""

from __future__ import annotations

import sys
import types


def _install_astrbot_stub() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    star = types.ModuleType("astrbot.api.star")
    event = types.ModuleType("astrbot.api.event")
    components = types.ModuleType("astrbot.api.message_components")
    event_components = types.ModuleType("astrbot.api.event.components")
    utils = types.ModuleType("astrbot.api.utils")

    class Star:
        def __init__(self, *args, **kwargs):
            pass

    def register(*args, **kwargs):
        return lambda cls: cls

    class Filter:
        class PermissionType:
            ADMIN = "admin"

        @staticmethod
        def command(*args, **kwargs):
            return lambda fn: fn

        @staticmethod
        def permission_type(*args, **kwargs):
            return lambda fn: fn

    logger = types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )

    class Image:
        pass

    star.Context = object
    star.Star = Star
    star.register = register
    event.AstrMessageEvent = object
    event.filter = Filter
    components.Image = Image
    event_components.Image = Image
    api.llm_tool = lambda *args, **kwargs: (lambda fn: fn)
    api.logger = logger
    utils.logger = logger

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.star": star,
            "astrbot.api.event": event,
            "astrbot.api.message_components": components,
            "astrbot.api.event.components": event_components,
            "astrbot.api.utils": utils,
        }
    )


_install_astrbot_stub()
