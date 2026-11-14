"""Compatibility helpers for task cancellation across supported Python versions."""

from __future__ import annotations

import threading
from typing import Any
from weakref import WeakSet


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
