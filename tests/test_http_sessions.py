from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from astrbot_plugin_selfie_image.core.proxy import ClientSessionRegistry
from astrbot_plugin_selfie_image.core.provider_parser import fetch_generated_image_url
from astrbot_plugin_selfie_image.core.models import ImageModelTarget
from astrbot_plugin_selfie_image.core.providers import ImageGenerateRequest, ImageGenerateResult
from astrbot_plugin_selfie_image.generation import generator
from astrbot_plugin_selfie_image.webui.services import build_health_payload


class ClientSessionRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_url_socks_proxy_is_connector_level_only(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 128

        class Content:
            async def iter_chunked(self, _size):
                yield png

        class Response:
            status = 200
            headers = {"content-type": "image/png", "content-length": str(len(png))}
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        class Session:
            def __init__(self):
                self.requests = []

            def get(self, url, **kwargs):
                self.requests.append((url, kwargs))
                return Response()

        session = Session()
        image = await fetch_generated_image_url(
            session,
            "http://cdn.example.test/result.png",
            timeout=5,
            proxy="socks5://proxy.example.test:1080",
        )

        self.assertEqual(image, png)
        self.assertIsNone(session.requests[0][1]["proxy"])


    async def test_registry_allocates_independent_download_proxy_session(self) -> None:
        registry = ClientSessionRegistry()
        target = ImageModelTarget(
            "test",
            "openai",
            "https://example.test",
            "",
            "model",
            10,
            proxy="socks5://request.example.test:1080",
            extra={"download_proxy": "http://download.example.test:8080"},
        )
        expected = ImageGenerateResult(images=[b"image"])

        class Adapter:
            async def generate(self, _request):
                return expected

        try:
            with patch.object(generator, "create_adapter", return_value=Adapter()) as factory:
                result = await generator._try_on_registry(
                    target,
                    ImageGenerateRequest(prompt="test"),
                    registry,
                    10,
                )
            self.assertIs(result, expected)
            self.assertEqual(len(factory.call_args.args), 3)
            self.assertIsNot(factory.call_args.args[1], factory.call_args.args[2])
            stats = registry.get_stats()
            self.assertEqual(stats["active_by_transport"], {"direct": 0, "http": 1, "socks": 1})
        finally:
            await registry.close()


    async def test_temporary_download_socks_session_closes_after_generation(self) -> None:
        target = ImageModelTarget(
            "test",
            "openai",
            "https://example.test",
            "",
            "model",
            10,
            extra={"download_proxy": "socks5://download.example.test:1080"},
        )
        captured = {}
        expected = ImageGenerateResult(images=[b"image"])

        class Adapter:
            async def generate(self, _request):
                captured["download_session_open"] = not captured["download_session"].closed
                return expected

        def factory(_target, _request_session, download_session):
            captured["download_session"] = download_session
            return Adapter()

        with patch.object(generator, "create_adapter", side_effect=factory):
            result = await generator._try_on_session(
                target,
                ImageGenerateRequest(prompt="test"),
                object(),
                10,
            )

        self.assertIs(result, expected)
        self.assertTrue(captured["download_session_open"])
        self.assertTrue(captured["download_session"].closed)


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
            stats = registry.get_stats()
            self.assertEqual(stats["hits"], 1)
            self.assertEqual(stats["requests"], 2)
            self.assertEqual(stats["connection_created"], 1)
            self.assertGreaterEqual(stats["connection_reused"], 1)
            self.assertEqual(stats["request_failures"], 0)
            self.assertEqual(stats["requests_by_transport"]["direct"], 2)
            self.assertEqual(stats["connection_created_by_transport"]["direct"], 1)
            self.assertGreaterEqual(stats["connection_reused_by_transport"]["direct"], 1)
            self.assertGreaterEqual(stats["idle_connections"], 1)
            self.assertGreaterEqual(stats["pool_connections"], 1)
            self.assertGreaterEqual(stats["idle_connections_by_transport"]["direct"], 1)
        finally:
            await registry.close()
            await runner.cleanup()

    async def test_registry_reports_connections_acquired_during_request(self) -> None:
        from aiohttp import web

        entered = asyncio.Event()
        release = asyncio.Event()

        async def handle(_request):
            entered.set()
            await release.wait()
            return web.Response(text="held")

        app = web.Application()
        app.router.add_get("/held", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        registry = ClientSessionRegistry()
        try:
            session = await registry.get_session()

            async def fetch():
                async with session.get(f"http://127.0.0.1:{port}/held") as response:
                    return await response.text()

            request = asyncio.create_task(fetch())
            await asyncio.wait_for(entered.wait(), timeout=2)
            stats = registry.get_stats()
            thread_snapshot = await asyncio.to_thread(registry.get_stats)
            self.assertGreaterEqual(stats["active_connections"], 1)
            self.assertGreaterEqual(stats["pool_connections"], 1)
            self.assertGreaterEqual(stats["active_connections_by_transport"]["direct"], 1)
            self.assertEqual(thread_snapshot["requests"], 1)
            self.assertEqual(thread_snapshot["request_failures"], 0)
            release.set()
            self.assertEqual(await request, "held")
        finally:
            if not release.is_set():
                release.set()
            await registry.close()
            await runner.cleanup()

    async def test_registry_counts_http_failure_responses(self) -> None:
        from aiohttp import web

        async def handle(_request):
            return web.Response(status=503, text="upstream unavailable")

        app = web.Application()
        app.router.add_get("/failure", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        registry = ClientSessionRegistry()
        try:
            session = await registry.get_session()
            async with session.get(f"http://127.0.0.1:{port}/failure") as response:
                self.assertEqual(response.status, 503)
                await response.read()
            stats = registry.get_stats()
            self.assertEqual(stats["requests"], 1)
            self.assertEqual(stats["request_failures"], 1)
            self.assertEqual(stats["request_failures_by_transport"]["direct"], 1)
        finally:
            await registry.close()
            await runner.cleanup()

    async def test_registry_routes_real_requests_through_socks5_and_reuses_tunnel(self) -> None:
        """Exercise the connector against a local SOCKS5 server, not a mock."""
        import contextlib
        import ipaddress
        import struct

        from aiohttp import web

        peer_ports = set()
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 128

        async def handle(request):
            peer = request.transport.get_extra_info("peername")
            if peer:
                peer_ports.add(peer[1])
            if request.path == "/image":
                return web.Response(body=png, content_type="image/png")
            return web.Response(text="socks-ok")

        app = web.Application()
        app.router.add_get("/health", handle)
        app.router.add_get("/image", handle)
        target_runner = web.AppRunner(app)
        await target_runner.setup()
        target_site = web.TCPSite(target_runner, "127.0.0.1", 0)
        await target_site.start()
        target_port = target_site._server.sockets[0].getsockname()[1]
        handshake_count = 0

        async def forward(source, destination):
            try:
                while True:
                    chunk = await source.read(64 * 1024)
                    if not chunk:
                        break
                    destination.write(chunk)
                    await destination.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass

        async def socks_client(reader, writer):
            nonlocal handshake_count
            remote_writer = None
            tasks = ()
            try:
                version, method_count = await reader.readexactly(2)
                if version != 5:
                    raise ValueError("unexpected SOCKS version")
                methods = await reader.readexactly(method_count)
                if 0 not in methods:
                    writer.write(b"\x05\xff")
                    await writer.drain()
                    return
                writer.write(b"\x05\x00")
                await writer.drain()

                version, command, _reserved, address_type = await reader.readexactly(4)
                if version != 5 or command != 1:
                    raise ValueError("unsupported SOCKS request")
                if address_type == 1:
                    host = str(ipaddress.ip_address(await reader.readexactly(4)))
                elif address_type == 3:
                    length = (await reader.readexactly(1))[0]
                    host = (await reader.readexactly(length)).decode("idna")
                elif address_type == 4:
                    host = str(ipaddress.ip_address(await reader.readexactly(16)))
                else:
                    raise ValueError("unsupported SOCKS address type")
                port = struct.unpack("!H", await reader.readexactly(2))[0]
                remote_reader, remote_writer = await asyncio.open_connection(host, port)
                writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                handshake_count += 1
                tasks = (
                    asyncio.create_task(forward(reader, remote_writer)),
                    asyncio.create_task(forward(remote_reader, writer)),
                )
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            except (asyncio.IncompleteReadError, ConnectionError, ValueError):
                pass
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                if remote_writer is not None:
                    remote_writer.close()
                    with contextlib.suppress(Exception):
                        await remote_writer.wait_closed()
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

        proxy_server = await asyncio.start_server(socks_client, "127.0.0.1", 0)
        proxy_port = proxy_server.sockets[0].getsockname()[1]
        registry = ClientSessionRegistry()
        try:
            session = await registry.get_session(f"socks5://127.0.0.1:{proxy_port}")
            self.assertIs(session, await registry.get_session(f"socks5://127.0.0.1:{proxy_port}"))
            for _ in range(2):
                async with session.get(f"http://127.0.0.1:{target_port}/health") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(await response.text(), "socks-ok")
            downloaded = await fetch_generated_image_url(
                session,
                f"http://127.0.0.1:{target_port}/image",
                timeout=5,
                proxy=f"socks5://127.0.0.1:{proxy_port}",
            )
            self.assertEqual(downloaded, png)
            self.assertEqual(handshake_count, 1)
            self.assertEqual(len(peer_ports), 1)
            self.assertEqual(registry.get_stats()["active_by_transport"]["socks"], 1)
        finally:
            await registry.close()
            proxy_server.close()
            await proxy_server.wait_closed()
            await target_runner.cleanup()
        self.assertEqual(registry.get_stats()["active_sessions"], 0)

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
