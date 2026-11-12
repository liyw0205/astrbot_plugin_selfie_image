"""Fair, cancellable image-shot scheduler shared by every image entry point."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional


@dataclass
class _ImageJob:
    """One deferred image shot owned by a command or web task."""

    task_id: str
    run: Callable[[], Awaitable[Any]]
    future: asyncio.Future[Any]
    worker: Optional[asyncio.Task[Any]] = None


class ImageJobScheduler:
    """Dispatch image jobs fairly while preserving one global live capacity.

    Jobs are stored per task and dispatched round-robin. A cancellation removes
    pending shots and sends cancellation into an in-flight async transport;
    callers do not receive a freed slot until that transport unwinds.
    """

    def __init__(self, limit: int) -> None:
        try:
            configured = int(limit or 5)
        except (TypeError, ValueError):
            configured = 5
        self._limit = max(1, min(5, configured))
        self._pending: Dict[str, Deque[_ImageJob]] = {}
        self._round_robin: Deque[str] = deque()
        self._running: Dict[str, set[asyncio.Task[Any]]] = {}
        self._active = 0
        self._wake = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._dispatcher: Optional[asyncio.Task[None]] = None
        self._closed = False

    def set_limit(self, limit: int) -> None:
        """Update capacity in place so existing jobs retain the same scheduler."""
        try:
            configured = int(limit or 5)
        except (TypeError, ValueError):
            configured = 5

        def apply_limit() -> None:
            self._limit = max(1, min(5, configured))
            self._wake.set()

        loop = self._loop
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if loop is not None and loop.is_running() and current_loop is not loop:
            loop.call_soon_threadsafe(apply_limit)
        else:
            apply_limit()

    async def submit(self, task_id: str, run: Callable[[], Awaitable[Any]]) -> Any:
        """Queue one shot and return its result when the fair scheduler runs it."""
        if self._closed:
            raise RuntimeError("image scheduler is closed")
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("image scheduler must run on one event loop")
        self._ensure_dispatcher()
        future: asyncio.Future[Any] = loop.create_future()
        key = str(task_id or "").strip() or "anonymous"
        queue = self._pending.setdefault(key, deque())
        job = _ImageJob(task_id=key, run=run, future=future)
        queue.append(job)
        if key not in self._round_robin:
            self._round_robin.append(key)
        self._wake.set()
        try:
            return await future
        except asyncio.CancelledError:
            if job.worker is not None and not job.worker.done() and not job.worker.cancelling():
                job.worker.cancel()
            self._wake.set()
            raise

    async def aclose(self) -> None:
        """Reject new work and await cancellation of every live transport."""
        self._closed = True
        for queue in self._pending.values():
            for job in queue:
                job.future.cancel()
        self._pending.clear()
        self._round_robin.clear()
        workers = [task for tasks in self._running.values() for task in tasks]
        for task in workers:
            if not task.cancelling():
                task.cancel()
        self._wake.set()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        if self._dispatcher is not None:
            await self._dispatcher

    async def cancel_task(self, task_id: str) -> None:
        """Cancel all queued and running work owned by one task."""
        key = str(task_id or "").strip()
        queue = self._pending.pop(key, deque())
        while queue:
            job = queue.popleft()
            if not job.future.done():
                job.future.cancel()
        self._round_robin = deque(item for item in self._round_robin if item != key)
        for running in list(self._running.get(key, set())):
            if not running.cancelling():
                running.cancel()
        self._wake.set()
        await asyncio.sleep(0)

    async def snapshot(self) -> Dict[str, int]:
        """Return current resource state for task dashboards and tests."""
        return {
            "limit": self._limit,
            "active": self._active,
            "pending": sum(len(queue) for queue in self._pending.values()),
            "queued_tasks": len(self._round_robin),
        }

    def _ensure_dispatcher(self) -> None:
        if self._dispatcher is None or self._dispatcher.done():
            self._dispatcher = asyncio.create_task(self._dispatch(), name="selfie-image-scheduler")

    async def _dispatch(self) -> None:
        while True:
            while self._active < self._limit:
                job = self._next_job()
                if job is None:
                    break
                self._active += 1
                running = asyncio.create_task(self._run(job))
                job.worker = running
                self._running.setdefault(job.task_id, set()).add(running)
                running.add_done_callback(lambda task, job=job: self._finish_job(job, task))
            self._wake.clear()
            if self._active == 0 and not self._round_robin:
                return
            await self._wake.wait()

    def _next_job(self) -> Optional[_ImageJob]:
        while self._round_robin:
            task_id = self._round_robin.popleft()
            queue = self._pending.get(task_id)
            if not queue:
                self._pending.pop(task_id, None)
                continue
            job = queue.popleft()
            if queue:
                self._round_robin.append(task_id)
            else:
                self._pending.pop(task_id, None)
            if job.future.cancelled():
                continue
            return job
        return None

    def _finish_job(self, job: _ImageJob, worker: asyncio.Task[Any]) -> None:
        # A task cancelled before its first step never enters _run's finally.
        if worker.cancelled() and not job.future.done():
            job.future.cancel()
        running = self._running.get(job.task_id)
        if running is not None:
            running.discard(worker)
            if not running:
                self._running.pop(job.task_id, None)
        self._active = max(0, self._active - 1)
        self._wake.set()

    async def _run(self, job: _ImageJob) -> None:
        try:
            result = await job.run()
            if not job.future.done():
                job.future.set_result(result)
        except asyncio.CancelledError:
            if not job.future.done():
                job.future.cancel()
            raise
        except Exception as exc:
            if not job.future.done():
                job.future.set_exception(exc)
