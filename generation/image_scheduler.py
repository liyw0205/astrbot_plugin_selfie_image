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


class ImageJobScheduler:
    """Dispatch image jobs fairly while preserving one global live capacity.

    Jobs are stored per task and dispatched round-robin. A cancellation removes
    pending shots and sends cancellation into an in-flight async transport;
    callers do not receive a freed slot until that transport unwinds.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit or 1))
        self._pending: Dict[str, Deque[_ImageJob]] = {}
        self._round_robin: Deque[str] = deque()
        self._running: Dict[str, set[asyncio.Task[Any]]] = {}
        self._active = 0
        self._wake = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._dispatcher: Optional[asyncio.Task[None]] = None

    def set_limit(self, limit: int) -> None:
        """Update capacity in place so existing jobs retain the same scheduler."""
        self._limit = max(1, int(limit or 1))
        self._wake.set()

    async def submit(self, task_id: str, run: Callable[[], Awaitable[Any]]) -> Any:
        """Queue one shot and return its result when the fair scheduler runs it."""
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("image scheduler must run on one event loop")
        self._ensure_dispatcher()
        future: asyncio.Future[Any] = loop.create_future()
        key = str(task_id or "").strip() or "anonymous"
        queue = self._pending.setdefault(key, deque())
        queue.append(_ImageJob(task_id=key, run=run, future=future))
        if key not in self._round_robin:
            self._round_robin.append(key)
        self._wake.set()
        return await future

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
                self._running.setdefault(job.task_id, set()).add(running)
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

    async def _run(self, job: _ImageJob) -> None:
        current = asyncio.current_task()
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
        finally:
            running = self._running.get(job.task_id)
            if running is not None and current is not None:
                running.discard(current)
                if not running:
                    self._running.pop(job.task_id, None)
            self._active = max(0, self._active - 1)
            self._wake.set()
