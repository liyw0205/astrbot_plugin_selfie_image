from __future__ import annotations

import unittest
from unittest.mock import ANY, AsyncMock, patch

from astrbot_plugin_selfie_image.core.proxy import ClientSessionRegistry
from astrbot_plugin_selfie_image.core.models import ImageModelTarget
from astrbot_plugin_selfie_image.core.providers import ImageGenerateRequest, ImageGenerateResult
from astrbot_plugin_selfie_image.generation import generator


class ClientSessionRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_reuses_direct_and_http_proxy_sessions(self) -> None:
        registry = ClientSessionRegistry()
        try:
            direct = await registry.get_session()
            self.assertIs(direct, await registry.get_session(""))

            first_proxy = await registry.get_session("http://proxy.example.test:8080")
            same_proxy = await registry.get_session("http://proxy.example.test:8080")
            other_proxy = await registry.get_session("http://other.example.test:8080")
            self.assertIs(first_proxy, same_proxy)
            self.assertIsNot(first_proxy, other_proxy)
            self.assertIsNot(direct, first_proxy)
        finally:
            await registry.close()

        self.assertTrue(direct.closed)
        self.assertTrue(first_proxy.closed)
        self.assertTrue(other_proxy.closed)


    async def test_registry_keeps_socks_connectors_isolated_and_closes_them(self) -> None:
        registry = ClientSessionRegistry()
        try:
            first = await registry.get_session("socks5://proxy.example.test:1080")
            same = await registry.get_session("socks5://proxy.example.test:1080")
            other = await registry.get_session("socks5://other.example.test:1080")
            self.assertIs(first, same)
            self.assertIsNot(first, other)
            self.assertIsNot(first.connector, other.connector)
        finally:
            await registry.close()

        self.assertTrue(first.closed)
        self.assertTrue(other.closed)


    async def test_registry_rejects_new_sessions_after_close(self) -> None:
        registry = ClientSessionRegistry()
        await registry.close()
        with self.assertRaisesRegex(RuntimeError, "registry is closed"):
            await registry.get_session()

    async def test_generator_routes_attempts_through_registry(self) -> None:
        registry = ClientSessionRegistry()
        target = ImageModelTarget("test", "openai", "https://example.test", "", "model", 1)
        expected = ImageGenerateResult(images=[b"image"])
        try:
            with patch.object(generator, "_try_on_registry", new=AsyncMock(return_value=expected)) as run:
                result = await generator._try_model(
                    target,
                    ImageGenerateRequest(prompt="test"),
                    budget=1,
                    session=registry,
                )
            self.assertIs(result, expected)
            run.assert_awaited_once_with(target, ANY, registry, 1)
        finally:
            await registry.close()
