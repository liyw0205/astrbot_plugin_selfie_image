"""Compatibility helpers for task cancellation across supported Python versions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable
import threading
from typing import Any, TypeVar
from weakref import WeakSet


T = TypeVar("T")


async def gather_cancel_on_error(awaitables: Iterable[Awaitable[T]]) -> list[T]:
    """Gather one bounded batch and stop its siblings if it fails or is cancelled."""
    tasks = []
    try:
        for awaitable in awaitables:
            tasks.append(asyncio.ensure_future(awaitable))
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        raise


_CANCEL_REQUESTED: WeakSet[Any] = WeakSet()
_CANCEL_LOCK = threading.Lock()


def task_is_cancelling(task: Any) -> bool:
    """Check cancellation state on Python versions with and without Task.cancelling."""
    cancelling = getattr(task, "cancelling", None)
    if callable(cancelling):
        try:
            if cancelling():
                return True
        except (AttributeError, TypeError):
            pass
    try:
        with _CANCEL_LOCK:
            return task in _CANCEL_REQUESTED
    except TypeError:
        return False


def mark_task_cancelling(task: Any) -> None:
    """Remember cancellation issued through an external Future proxy."""
    try:
        with _CANCEL_LOCK:
            _CANCEL_REQUESTED.add(task)
    except TypeError:
        pass


def cancel_task_once(task: Any) -> bool:
    """Request cancellation once so repeated shutdown paths cannot interrupt cleanup."""
    if task is None or task.done() or task_is_cancelling(task):
        return False
    try:
        with _CANCEL_LOCK:
            if task in _CANCEL_REQUESTED:
                return False
            _CANCEL_REQUESTED.add(task)
    except TypeError:
        pass
    task.cancel()
    return True
