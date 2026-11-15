from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from astrbot_plugin_selfie_image.core.proxy import ClientSessionRegistry
from astrbot_plugin_selfie_image.core.models import ImageModelTarget
from astrbot_plugin_selfie_image.core.providers import ImageGenerateRequest, ImageGenerateResult
from astrbot_plugin_selfie_image.generation import generator
from astrbot_plugin_selfie_image.webui.services import build_health_payload


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

    async def test_registry_reports_reuse_recycle_and_close_counters(self) -> None:
        registry = ClientSessionRegistry()
        direct = await registry.get_session()
        self.assertIs(direct, await registry.get_session())
        proxy = await registry.get_session("http://proxy.example.test:8080")
        await direct.close()
        replacement = await registry.get_session()

        before_close = registry.get_stats()
        self.assertEqual(before_close["lookups"], 4)
        self.assertEqual(before_close["hits"], 1)
        self.assertEqual(before_close["misses"], 3)
        self.assertEqual(before_close["created"], 3)
        self.assertEqual(before_close["recycled"], 1)
        self.assertEqual(before_close["active_sessions"], 2)
        self.assertEqual(before_close["active_by_transport"], {"direct": 1, "http": 1, "socks": 0})

        await registry.close()
        after_close = registry.get_stats()
        self.assertEqual(after_close["closed"], 2)
        self.assertEqual(after_close["active_sessions"], 0)
        self.assertEqual(after_close["status"], "closed")
        self.assertTrue(replacement.closed)
        self.assertTrue(proxy.closed)

    async def test_registry_reaps_idle_sessions_before_reuse(self) -> None:
        registry = ClientSessionRegistry(idle_timeout_seconds=0.01)
        try:
            first = await registry.get_session()
            await asyncio.sleep(0.02)
            self.assertEqual(await registry.reap_idle_sessions(), 1)
            self.assertTrue(first.closed)
            replacement = await registry.get_session()
            stats = registry.get_stats()
            self.assertIsNot(first, replacement)
            self.assertEqual(stats["recycled"], 1)
            self.assertEqual(stats["closed"], 1)
            self.assertEqual(stats["active_sessions"], 1)
        finally:
            await registry.close()

    async def test_registry_reuses_a_real_loopback_connection(self) -> None:
        from aiohttp import web

        peer_ports = set()

        async def handle(request):
            peer = request.transport.get_extra_info("peername")
            if peer:
                peer_ports.add(peer[1])
            return web.Response(text="ok")

        app = web.Application()
        app.router.add_get("/health", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        registry = ClientSessionRegistry()
        try:
            session = await registry.get_session()
            self.assertIs(session, await registry.get_session())
            for _ in range(2):
                async with session.get(f"http://127.0.0.1:{port}/health") as response:
                    self.assertEqual(await response.text(), "ok")
            self.assertEqual(len(peer_ports), 1)
            self.assertEqual(registry.get_stats()["hits"], 1)
        finally:
            await registry.close()
            await runner.cleanup()

    async def test_registry_failure_diagnostics_are_bounded_and_credential_free(self) -> None:
        registry = ClientSessionRegistry()
        with self.assertRaises(ValueError):
            await registry.get_session("http://user:password@proxy.example.test:not-a-port")
        await registry.close()
        with self.assertRaisesRegex(RuntimeError, "registry is closed"):
            await registry.get_session("socks5://user:password@proxy.example.test:1080")

        stats = registry.get_stats()
        self.assertEqual(stats["failures"], 2)
        self.assertTrue(stats["failure_reasons"])
        self.assertNotIn("proxy.example.test", json.dumps(stats))
        self.assertNotIn("password", json.dumps(stats))

    async def test_registry_lazily_reaps_idle_sessions_but_keeps_acquired_connections(self) -> None:
        registry = ClientSessionRegistry(idle_timeout_seconds=0.01)
        session = await registry.get_session()
        await asyncio.sleep(0.02)
        self.assertEqual(await registry.reap_idle_sessions(), 1)
        self.assertTrue(session.closed)

        protected = ClientSessionRegistry(idle_timeout_seconds=0.01)
        active = await protected.get_session()
        marker = object()
        active.connector._acquired.add(marker)
        await asyncio.sleep(0.02)
        self.assertEqual(await protected.reap_idle_sessions(), 0)
        self.assertFalse(active.closed)
        active.connector._acquired.remove(marker)
        self.assertEqual(await protected.reap_idle_sessions(), 1)
        await protected.close()

    def test_health_payload_includes_http_session_pool_health(self) -> None:
        class Plugin:
            config = SimpleNamespace(
                image_cache_limit_count=10,
                image_record_keep_limit=10,
                image_cache_limit_mb=10,
            )
            _cache_stats = staticmethod(lambda: (0, 0))
            get_cache_cleanup_preview = staticmethod(lambda: {})
            get_http_session_health = staticmethod(lambda: {"status": "ok", "active_sessions": 2})

        payload = build_health_payload(Plugin())
        self.assertEqual(payload["http_session_health"]["active_sessions"], 2)

        Plugin.get_http_session_health = staticmethod(
            lambda: (_ for _ in ()).throw(RuntimeError("proxy.example.test: secret"))
        )
        degraded = build_health_payload(Plugin())
        self.assertEqual(degraded["status"], "degraded")
        self.assertEqual(degraded["http_session_health"]["status"], "error")

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
