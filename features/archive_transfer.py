"""Private, bounded temporary files for browser archive transfers."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Callable, Iterator
import uuid
import zlib


CHUNK_SIZE = 1024 * 1024
MAX_UPLOAD_BYTES = 128 * CHUNK_SIZE
MAX_JSON_ARCHIVE_BYTES = 16 * CHUNK_SIZE
MAX_TRANSFERS = 2
MAX_TRANSFER_BYTES = 2 * MAX_UPLOAD_BYTES
TRANSFER_TTL = 15 * 60
TRANSFER_LIFETIME = 60 * 60
CLEANUP_INTERVAL = 60


class ArchiveTransferStore:
    """Serialize file mutations and expire process-local transfer handles.

    Consumers run under this store's lock. Callers must release manager locks
    before entering the store, so completion and shutdown cannot invert locks.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        if self.root.is_symlink():
            raise ValueError("Archive transfer directory cannot be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._closed = False
        self._timer: threading.Timer | None = None
        # Handles deliberately do not survive restart or plugin replacement.
        for path in self.root.iterdir():
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)

    def __enter__(self) -> ArchiveTransferStore:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _remove_locked(self, transfer_id: str) -> None:
        entry = self._sessions.pop(transfer_id, None)
        if entry:
            shutil.rmtree(entry["path"].parent)

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise ValueError("Archive transfer store is closed")

    def _prune_locked(self) -> None:
        now = time.monotonic()
        for transfer_id, entry in list(self._sessions.items()):
            if now - entry["touched"] >= TRANSFER_TTL or now - entry["created"] >= TRANSFER_LIFETIME:
                self._remove_locked(transfer_id)

    def _schedule_locked(self) -> None:
        if self._sessions and self._timer is None and not self._closed:
            self._timer = threading.Timer(CLEANUP_INTERVAL, self.cleanup)
            self._timer.daemon = True
            self._timer.start()

    def cleanup(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._prune_locked()
            self._schedule_locked()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._timer:
                self._timer.cancel()
                self._timer = None
            for transfer_id in list(self._sessions):
                self._remove_locked(transfer_id)

    def _reserve_locked(self, mode: str, kind: str, size: int, filename: str) -> dict[str, Any]:
        self._ensure_open_locked()
        self._prune_locked()
        if len(self._sessions) >= MAX_TRANSFERS or sum(e["size"] for e in self._sessions.values()) + size > MAX_TRANSFER_BYTES:
            raise ValueError("Archive transfer capacity reached; finish or cancel another transfer")
        transfer_id = uuid.uuid4().hex
        directory = self.root / transfer_id
        directory.mkdir(mode=0o700)
        entry = {
            "id": transfer_id, "mode": mode, "kind": kind, "size": size,
            "filename": str(filename or "archive.zip").replace("\\", "/").rsplit("/", 1)[-1][:120] or "archive.zip",
            "path": directory / "archive.zip", "offset": 0, "hash": hashlib.sha256(),
            "created": time.monotonic(), "touched": time.monotonic(),
        }
        self._sessions[transfer_id] = entry
        self._schedule_locked()
        return entry

    def _get_locked(self, transfer_id: str, mode: str = "") -> dict[str, Any]:
        self._ensure_open_locked()
        self._prune_locked()
        entry = self._sessions.get(str(transfer_id))
        if entry is None or (mode and entry["mode"] != mode):
            raise ValueError("Archive transfer is missing or expired")
        return entry

    @staticmethod
    def _public(entry: dict[str, Any]) -> dict[str, Any]:
        return {"id": entry["id"], "kind": entry["kind"], "filename": entry["filename"],
                "size": entry["size"], "next_offset": entry["offset"], "chunk_size": CHUNK_SIZE,
                "expires_in": TRANSFER_TTL}

    def start_upload(self, kind: str, size: int, filename: str) -> dict[str, Any]:
        if not isinstance(kind, str) or kind not in {"backup", "wardrobe"} or type(size) is not int or not 0 < size <= MAX_UPLOAD_BYTES:
            raise ValueError("Invalid archive kind or size (maximum 128 MiB)")
        with self._lock:
            entry = self._reserve_locked("upload", kind, size, filename)
            try:
                entry["path"].touch(mode=0o600, exist_ok=False)
            except Exception:
                self._remove_locked(entry["id"])
                raise
            return self._public(entry)

    def append_upload(self, transfer_id: str, offset: int, data: bytes, crc32: str) -> dict[str, Any]:
        if type(offset) is not int or offset < 0 or not isinstance(data, bytes) or not 0 < len(data) <= CHUNK_SIZE:
            raise ValueError("Invalid archive chunk range")
        if not isinstance(crc32, str) or not re.fullmatch(r"[0-9a-fA-F]{8}", crc32) or int(crc32, 16) != zlib.crc32(data):
            raise ValueError("Archive chunk checksum mismatch")
        with self._lock:
            entry = self._get_locked(transfer_id, "upload")
            end = offset + len(data)
            if offset > entry["offset"] or end > entry["size"]:
                raise ValueError("Archive chunk offset or total size mismatch")
            with entry["path"].open("r+b") as output:
                if os.fstat(output.fileno()).st_size != entry["offset"]:
                    raise ValueError("Archive staging file size changed")
                output.seek(offset)
                if offset < entry["offset"]:
                    if end > entry["offset"] or output.read(len(data)) != data:
                        raise ValueError("Conflicting duplicate archive chunk")
                else:
                    try:
                        output.write(data)
                        output.flush()
                    except Exception:
                        output.truncate(offset)
                        raise
                    entry["hash"].update(data)
                    entry["offset"] = end
            entry["touched"] = time.monotonic()
            return self._public(entry)

    @contextmanager
    def consume_upload(self, transfer_id: str) -> Iterator[tuple[Path, dict[str, Any]]]:
        with self._lock:
            entry = self._get_locked(transfer_id, "upload")
            if entry["offset"] != entry["size"]:
                raise ValueError("Archive upload is incomplete")
            try:
                digest = hashlib.sha256()
                with entry["path"].open("rb") as incoming:
                    if os.fstat(incoming.fileno()).st_size != entry["size"]:
                        raise ValueError("Archive staging checksum mismatch")
                    while chunk := incoming.read(CHUNK_SIZE):
                        digest.update(chunk)
                if digest.digest() != entry["hash"].digest():
                    raise ValueError("Archive staging checksum mismatch")
                yield entry["path"], self._public(entry) | {"sha256": digest.hexdigest()}
            finally:
                self._remove_locked(entry["id"])

    def discard(self, transfer_id: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_open_locked()
            self._remove_locked(str(transfer_id))
        return {"discarded": True}

    def create_export(self, build: Callable[[Path], Any]) -> dict[str, Any]:
        with self._lock:
            entry = self._reserve_locked("export", "wardrobe", MAX_UPLOAD_BYTES, "selfie_image_wardrobe.zip")
            try:
                build(entry["path"])
                size = entry["path"].stat().st_size
                if not 0 < size <= MAX_UPLOAD_BYTES:
                    raise ValueError("Exported archive exceeds 128 MiB or is empty")
                entry["size"] = size
                return self._public(entry)
            except Exception:
                self._remove_locked(entry["id"])
                raise

    def download_export(self, transfer_id: str, *, offset: int = 0, length: int = CHUNK_SIZE,
                        revision: str = "", as_file: bool = False) -> dict[str, Any]:
        if type(offset) is not int or offset < 0 or type(length) is not int or not 0 < length <= CHUNK_SIZE:
            raise ValueError("Invalid archive download range")
        with self._lock:
            entry = self._get_locked(transfer_id, "export")
            with entry["path"].open("rb") as incoming:
                stat = os.fstat(incoming.fileno())
                key = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                current_revision = hashlib.sha256(repr(key).encode("ascii")).hexdigest()
                if revision and revision != current_revision:
                    raise ValueError("Exported archive changed; restart the download")
                if stat.st_size != entry["size"] or offset > stat.st_size:
                    raise ValueError("Invalid archive download offset or size")
                entry["touched"] = time.monotonic()
                info = self._public(entry)
                if as_file:
                    return info | {"path": str(entry["path"])}
                incoming.seek(offset)
                data = incoming.read(length)
            return info | {"offset": offset, "next_offset": offset + len(data),
                           "eof": offset + len(data) == stat.st_size, "revision": current_revision,
                           "content_base64": base64.b64encode(data).decode("ascii")}
