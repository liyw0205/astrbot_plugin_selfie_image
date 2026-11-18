"""Cross-process guards for small JSON stores.

The dashboard and command handlers can be served by different plugin
instances during a hot reload.  A per-instance ``RLock`` is not enough for
those writers, while locking the JSON inode is unsafe because writes use an
atomic replace.  This module keeps the lock on a stable sidecar and records a
content fingerprint beside a monotonically increasing revision.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from typing import Any, Iterator

try:  # Windows and POSIX expose different advisory file-lock APIs.
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - exercised on Windows hosts
    fcntl = None

try:  # pragma: no cover - exercised on Windows hosts
    import msvcrt  # type: ignore
except ImportError:
    msvcrt = None

from .utils import load_json_file_result, save_json_file


class PersistentStoreConflictError(RuntimeError):
    """A stale in-memory snapshot attempted to replace a newer file."""

    def __init__(self, expected: int, current: int, label: str = "存储") -> None:
        self.expected = int(expected)
        self.current = int(current)
        self.label = str(label or "存储")
        super().__init__(
            f"{self.label}已被其他进程更新（当前版本 {self.current}，提交版本 {self.expected}），请重新读取后再保存"
        )


@contextmanager
def cross_process_file_lock(path: str) -> Iterator[None]:
    """Hold a portable advisory lock on a stable sidecar file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle = open(path, "a+b")
    locked = False
    try:
        if os.name == "nt" and msvcrt is not None:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            locked = True
        elif fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        if locked:
            try:
                if os.name == "nt" and msvcrt is not None:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                elif fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        else:
            handle.close()


def file_fingerprint(path: str) -> str:
    """Return a SHA-256 fingerprint for an atomically replaced file."""
    if not path or not os.path.exists(path):
        return ""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise RuntimeError("持久化文件读取失败，已停止覆盖原文件") from exc
    return digest.hexdigest()


def read_revision_state(
    path: str,
    revision_path: str,
    *,
    current_revision: int = 1,
    current_fingerprint: str | None = None,
) -> tuple[int, str, bool]:
    """Read sidecar metadata and reconcile it with the live file.

    The boolean is true when metadata was missing/invalid or its fingerprint
    no longer matches the live file.  Callers holding ``cross_process_file_lock``
    may publish the returned state before serving or writing the store.
    """
    fingerprint = file_fingerprint(path)
    result = load_json_file_result(revision_path)
    metadata = result.data if result.ok and isinstance(result.data, dict) else {}
    try:
        revision = max(1, int(metadata.get("revision") or 1))
    except (TypeError, ValueError):
        revision = 1
    previous_fingerprint = str(metadata.get("fingerprint") or "")
    valid = bool(result.ok and previous_fingerprint == fingerprint)
    if valid:
        return revision, fingerprint, False

    # A sidecar mismatch can mean an older plugin, a manual edit, or a
    # restored backup changed the JSON without sharing our lock.  Advance the
    # revision before accepting a write from an already-running instance.
    if previous_fingerprint != fingerprint and (
        result.ok or current_fingerprint is not None
    ):
        revision = max(revision + 1, int(current_revision or 1) + 1)
    return revision, fingerprint, True


def write_revision_state(path: str, revision: int, fingerprint: str) -> None:
    """Publish sidecar metadata only after the JSON replace succeeds."""
    save_json_file(
        path,
        {
            "version": 1,
            "revision": max(1, int(revision)),
            "fingerprint": str(fingerprint or ""),
        },
    )
