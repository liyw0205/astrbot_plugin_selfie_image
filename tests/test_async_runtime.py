"""Transport and shutdown contracts on runtimes without Python 3.11 task APIs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_selfie_image.core.models import ImageModelTarget
from astrbot_plugin_selfie_image.core.providers import ImageGenerateRequest, ImageGenerateResult
from astrbot_plugin_selfie_image.generation import generator
from astrbot_plugin_selfie_image.generation.image_scheduler import ImageJobScheduler
from astrbot_plugin_selfie_image.tests.test_repository_fixes import runtime_plugin
from astrbot_plugin_selfie_image.webui import web as web_module


class LegacyTask(asyncio.Task):
    pass


def legacy_tasks(loop):
    def factory(loop, coro, **kwargs):
        task = LegacyTask(coro, loop=loop, **kwargs)
        if getattr(getattr(coro, "cr_code", None), "co_name", "") in {
            "tracked", "_run", "_dispatch",
        }:
            task.cancelling = None
        return task

    loop.set_task_factory(factory)


def test_transport_does_not_require_asyncio_timeout(monkeypatch):
    async def transport(*_args):
        return ImageGenerateResult(images=[b"generated"])

    monkeypatch.delattr(asyncio, "timeout", raising=False)
    monkeypatch.setattr(generator, "_try_on_session", transport)
    target = ImageModelTarget("test", "openai", "https://example.test", "key", "model", 1)
    result = asyncio.run(generator._try_model(target, ImageGenerateRequest(prompt="test"), 1, object()))
    assert result.images == [b"generated"]
    assert not result.error


def test_scheduler_repeated_cancel_waits_for_legacy_transport_cleanup():
    async def run():
        loop = asyncio.get_running_loop()
        legacy_tasks(loop)
        scheduler = ImageJobScheduler(1)
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def transport():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        submitter = asyncio.create_task(scheduler.submit("task", transport))
        await started.wait()
        closer = None
        try:
            await scheduler.cancel_task("task")
            await asyncio.wait_for(cleaning.wait(), 1)
            await scheduler.cancel_task("task")
            closer = asyncio.create_task(scheduler.aclose())
            await asyncio.sleep(0)
            assert not closer.done()
            assert (await scheduler.snapshot())["active"] == 1
            release.set()
            await asyncio.wait_for(closer, 1)
            assert submitter.cancelled()
            assert (await scheduler.snapshot())["active"] == 0
        finally:
            release.set()
            scheduler._closed = True
            scheduler._wake.set()
            for worker in [task for bucket in scheduler._running.values() for task in bucket]:
                worker.cancel()
            await asyncio.gather(submitter, *([closer] if closer else []), return_exceptions=True)
            if scheduler._dispatcher is not None:
                await scheduler._dispatcher

    asyncio.run(run())


def test_proxy_cancel_then_shutdown_preserves_legacy_worker_cleanup(tmp_path):
    async def run():
        loop = asyncio.get_running_loop()
        legacy_tasks(loop)
        plugin = runtime_plugin(tmp_path)
        plugin.loop = loop
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def worker():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        handle = await asyncio.to_thread(plugin._start_runtime_generation, "task", worker())
        await started.wait()
        await asyncio.to_thread(handle.cancel)
        await asyncio.wait_for(cleaning.wait(), 1)
        shutdown = asyncio.create_task(plugin.terminate())
        try:
            for _ in range(20):
                await asyncio.sleep(0.005)
                assert not shutdown.done()
            assert plugin._runtime_generation_workers
            release.set()
            await asyncio.wait_for(shutdown, 1)
            assert not plugin._runtime_generation_workers
        finally:
            release.set()
            await asyncio.gather(shutdown, return_exceptions=True)

    asyncio.run(run())


def test_flask_bridge_cancels_legacy_future_timeout(monkeypatch):
    class LegacyFutureTimeout(TimeoutError):
        pass

    cancelled = []

    def result(_timeout):
        raise LegacyFutureTimeout("deadline")

    future = SimpleNamespace(result=result, cancel=lambda: cancelled.append(True))
    monkeypatch.setattr(web_module, "FutureTimeoutError", LegacyFutureTimeout, raising=False)
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda *_args: future)
    server = web_module.FlaskWebServer(SimpleNamespace(loop=SimpleNamespace(is_running=lambda: True)))
    with pytest.raises(TimeoutError):
        server._run_async(object(), 0.01)
    assert cancelled == [True]
