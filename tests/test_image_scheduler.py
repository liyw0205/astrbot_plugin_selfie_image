from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_selfie_image.generation.image_scheduler import ImageJobScheduler


class ImageJobSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def test_scheduler_caps_image_concurrency_at_five(self) -> None:
        from astrbot_plugin_selfie_image.generation.image_scheduler import ImageJobScheduler

        for value in (6, 10, 99, "bad", None):
            scheduler = ImageJobScheduler(value)
            self.assertEqual(scheduler._limit, 5, value)
            scheduler.set_limit(value)
            self.assertEqual(scheduler._limit, 5, value)

    async def test_scheduler_round_robins_ready_tasks(self) -> None:
        """One large task cannot consume every slot before another queued task runs."""

        scheduler = ImageJobScheduler(limit=1)
        started: list[str] = []

        def job(name: str):
            async def run() -> str:
                started.append(name)
                await asyncio.sleep(0)
                return name

            return run

        results = await asyncio.gather(
            scheduler.submit("A", job("A-1")),
            scheduler.submit("A", job("A-2")),
            scheduler.submit("B", job("B-1")),
            scheduler.submit("B", job("B-2")),
        )

        self.assertEqual(results, ["A-1", "A-2", "B-1", "B-2"])
        self.assertEqual(started, ["A-1", "B-1", "A-2", "B-2"])

    async def test_cancel_task_cancels_running_job_and_discards_queued_jobs(self) -> None:
        """Task cancellation propagates to transport and removes its queued shots."""

        scheduler = ImageJobScheduler(limit=1)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def running() -> str:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        queued_ran = False

        async def queued() -> str:
            nonlocal queued_ran
            queued_ran = True
            return "unexpected"

        first = asyncio.create_task(scheduler.submit("task", running))
        await asyncio.wait_for(started.wait(), timeout=1)
        second = asyncio.create_task(scheduler.submit("task", queued))
        await asyncio.sleep(0)

        await scheduler.cancel_task("task")

        with self.assertRaises(asyncio.CancelledError):
            await first
        with self.assertRaises(asyncio.CancelledError):
            await second
        self.assertTrue(cancelled.is_set())
        self.assertFalse(queued_ran)
        self.assertEqual((await scheduler.snapshot())["active"], 0)

    async def test_limit_update_admits_more_work_without_replacing_scheduler(self) -> None:
        """A hot capacity update expands one scheduler instead of creating another gate."""

        scheduler = ImageJobScheduler(limit=1)
        release = asyncio.Event()
        two_started = asyncio.Event()
        active = 0
        peak = 0

        def job() -> object:
            async def run() -> str:
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                if active >= 2:
                    two_started.set()
                try:
                    await release.wait()
                    return "done"
                finally:
                    active -= 1

            return run

        jobs = [asyncio.create_task(scheduler.submit("task", job())) for _ in range(3)]
        for _ in range(20):
            if (await scheduler.snapshot())["active"] == 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual((await scheduler.snapshot())["active"], 1)

        scheduler.set_limit(2)
        await asyncio.wait_for(two_started.wait(), timeout=1)
        release.set()
        self.assertEqual(await asyncio.gather(*jobs), ["done", "done", "done"])
        self.assertEqual(peak, 2)
