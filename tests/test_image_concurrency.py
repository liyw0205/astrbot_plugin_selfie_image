from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_selfie_image.core.models import ImageModelTarget
from astrbot_plugin_selfie_image.core.providers import ImageGenerateRequest, ImageGenerateResult
from astrbot_plugin_selfie_image.generation import generator


class _ImmediateSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class ImageTransportLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_waits_for_async_transport_cancellation_cleanup(self) -> None:
        """A timed out call finishes only after its transport cleanup completes."""

        target = ImageModelTarget("test", "openai", "https://example.test", "key", "model", 1)
        cancelled = asyncio.Event()

        async def slow_transport(*_args):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                cancelled.set()
                raise

        with patch.object(generator, "_try_on_session", side_effect=slow_transport):
            result = await generator._try_model(
                target,
                ImageGenerateRequest(prompt="test"),
                budget=0.01,
                session=object(),
            )

        self.assertEqual(result.error, "模型超时（1s）")
        self.assertTrue(cancelled.is_set())

    async def test_default_transport_path_does_not_delegate_to_thread(self) -> None:
        """Image transports must remain cancellable async work, not hidden threads."""

        target = ImageModelTarget("test", "openai", "https://example.test", "key", "model", 1)
        called = False

        async def immediate_transport(*_args):
            return ImageGenerateResult(images=[])

        def forbidden_thread(*_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError("image transport must not create a worker thread")

        with (
            patch.object(generator, "_try_on_session", side_effect=immediate_transport),
            patch.object(generator, "aiohttp", SimpleNamespace(
                ClientSession=lambda **_kwargs: _ImmediateSession(),
                ClientTimeout=lambda **_kwargs: SimpleNamespace(**_kwargs),
            )),
            patch.object(generator.asyncio, "to_thread", side_effect=forbidden_thread),
        ):
            result = await generator._try_model(
                target,
                ImageGenerateRequest(prompt="test"),
                budget=1,
                session=object(),
            )

        self.assertEqual(result.error, "")
        self.assertFalse(called)


class ImageSchedulerConfigurationTests(unittest.TestCase):
    def test_hot_config_update_keeps_existing_image_scheduler(self) -> None:
        """A config update changes capacity without replacing a live scheduler."""

        from astrbot_plugin_selfie_image.features.config_manager import ConfigurationMixin

        class Plugin(ConfigurationMixin):
            pass

        plugin = Plugin()
        plugin._config_lock = threading.RLock()
        plugin._persist_config = lambda: None
        plugin.key_config = {"web": {"enable": False, "host": "127.0.0.1", "port": 14514, "token": "test"}}
        plugin.raw_config = {"image": {"max_concurrent_tasks": 2}}
        plugin.config = SimpleNamespace(image_max_concurrent_tasks=2, video_max_concurrent_tasks=1)
        plugin._image_scheduler = SimpleNamespace(set_limit=lambda _limit: None)
        scheduler = plugin._image_scheduler

        plugin._apply_raw_config({"image": {"max_concurrent_tasks": 3}})

        self.assertIs(plugin._image_scheduler, scheduler)
