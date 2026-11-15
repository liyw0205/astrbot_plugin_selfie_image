"""Create and rehearse private, offline backups of an explicit plugin data root."""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile


FORMAT = "selfie-image-offline-backup"
VERSION = 1
SQLITE_HEADER = b"SQLite format 3\x00"
CHUNK_SIZE = 1024 * 1024
RESERVATION_SUFFIX = ".selfie-image-offline-reservation"


class BackupError(ValueError):
    """A backup cannot be safely completed or restored."""


def _root(value: str | Path, *, exists: bool = True) -> Path:
    path = Path(value).expanduser().absolute()
    if path.is_symlink():
        raise BackupError("Root directories must not be symbolic links")
    path = path.resolve()
    if exists and not path.is_dir():
        raise BackupError(f"Directory does not exist: {path}")
    if not exists and not path.parent.is_dir():
        raise BackupError(f"Destination parent does not exist: {path.parent}")
    return path


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise BackupError("Invalid relative path in backup")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise BackupError("Invalid relative path in backup")
    return value


def _inventory(root: Path) -> tuple[dict[str, os.stat_result], list[str]]:
    files: dict[str, os.stat_result] = {}
    directories: list[str] = []

    def fail(error: OSError) -> None:
        raise error

    for current, dirnames, filenames in os.walk(root, followlinks=False, onerror=fail):
        dirnames.sort()
        filenames.sort()
        for name in [*dirnames, *filenames]:
            path = Path(current) / name
            info = path.lstat()
            relative = _relative(path.relative_to(root).as_posix())
            if stat.S_ISLNK(info.st_mode):
                raise BackupError(f"Symbolic links are not supported: {relative}")
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
            elif stat.S_ISREG(info.st_mode):
                files[relative] = info
            else:
                raise BackupError(f"Special files are not supported: {relative}")
    return files, sorted(directories)


def _private(path: Path) -> None:
    if path.lstat().st_mode & 0o077:
        raise BackupError("Backup permissions must exclude group and other users")


def _open_regular(path: Path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise BackupError("Only regular files can be copied")
    return os.fdopen(descriptor, "rb")


def _copy_file(source: Path, destination: Path) -> None:
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output, _open_regular(source) as incoming:
        shutil.copyfileobj(incoming, output, length=CHUNK_SIZE)
        output.flush()
        os.fsync(output.fileno())


def _checksum(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with _open_regular(path) as incoming:
        for chunk in iter(lambda: incoming.read(CHUNK_SIZE), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _is_sqlite(path: Path) -> bool:
    with _open_regular(path) as incoming:
        header = incoming.read(len(SQLITE_HEADER))
    if path.name == "generation_records.sqlite3" and header != SQLITE_HEADER:
        raise BackupError("generation_records.sqlite3 has an invalid SQLite header")
    return header == SQLITE_HEADER


def _sqlite_info(path: Path) -> dict:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
            raise BackupError("SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("SQLite foreign key check failed")
        return {
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "schema_version": connection.execute("PRAGMA schema_version").fetchone()[0],
        }


def _backup_sqlite(source: Path, destination: Path) -> None:
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    # backup() includes committed pages still in WAL.  Copying only the main
    # database file would silently lose those transactions.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as incoming:
        with closing(sqlite3.connect(str(destination))) as output:
            incoming.backup(output)
            output.execute("PRAGMA journal_mode=DELETE")
    with _open_regular(destination) as output:
        os.fsync(output.fileno())


def _validate_file(path: Path, kind: str) -> dict:
    if (kind == "sqlite") != _is_sqlite(path):
        raise BackupError(f"SQLite file kind mismatch: {path.name}")
    if kind == "sqlite":
        return _sqlite_info(path)
    if path.suffix.lower() == ".json":
        try:
            with path.open("r", encoding="utf-8-sig") as incoming:
                json.load(incoming)
        except (ValueError, UnicodeError) as error:
            raise BackupError(f"Invalid JSON file: {path.name}") from error
    return {}


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reservation_path(destination: Path) -> Path:
    return destination.parent / f".{destination.name}{RESERVATION_SUFFIX}"


def _read_reservation(path: Path) -> tuple[os.stat_result, dict] | None:
    """Read a private reservation marker without following links."""
    descriptor = None
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            return None
        if not stat.S_ISREG(opened.st_mode) or opened.st_mode & 0o077:
            return None
        incoming = os.fdopen(descriptor, "r", encoding="ascii")
        descriptor = None
        with incoming:
            payload = json.load(incoming)
    except (FileNotFoundError, OSError, UnicodeError, ValueError, TypeError):
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("format") != FORMAT
        or type(payload.get("version")) is not int
        or payload.get("version") != VERSION
        or payload.get("purpose") != "destination-reservation"
        or type(payload.get("pid")) is not int
        or payload["pid"] <= 0
        or not isinstance(payload.get("token"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", payload["token"])
    ):
        return None
    return info, payload


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _unlink_owned_reservation(
    path: Path,
    token: str,
    expected_info: os.stat_result | None = None,
) -> None:
    marker = _read_reservation(path)
    if marker is None or marker[1].get("token") != token or marker[1].get("pid") != os.getpid():
        return
    if expected_info is not None and (
        marker[0].st_dev,
        marker[0].st_ino,
    ) != (expected_info.st_dev, expected_info.st_ino):
        return
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except FileNotFoundError:
        pass
    except OSError:
        # A published tree is still valid if cleanup of its advisory lock is
        # interrupted; the next invocation will see the live destination.
        pass


def _unlink_reservation_inode(path: Path, expected_info: os.stat_result | None) -> None:
    """Remove a partially written marker only when its inode is unchanged."""
    if expected_info is None:
        return
    try:
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (expected_info.st_dev, expected_info.st_ino):
            return
        path.unlink()
        _fsync_directory(path.parent)
    except (FileNotFoundError, OSError):
        pass


def _remove_empty_owned_destination(destination: Path, expected_info: os.stat_result | None) -> None:
    if expected_info is None:
        return
    try:
        current = destination.lstat()
        if (
            (current.st_dev, current.st_ino) != (expected_info.st_dev, expected_info.st_ino)
            or not stat.S_ISDIR(current.st_mode)
            or next(destination.iterdir(), None) is not None
        ):
            return
        destination.rmdir()
        _fsync_directory(destination.parent)
    except (FileNotFoundError, OSError):
        pass


def _acquire_reservation(destination: Path) -> tuple[Path, os.stat_result, str, os.stat_result]:
    """Reserve both a sibling lock and an empty target directory atomically."""
    path = _reservation_path(destination)
    for _ in range(2):
        try:
            destination.lstat()
        except FileNotFoundError:
            pass
        else:
            marker = _read_reservation(path)
            if marker is None or _pid_is_alive(int(marker[1]["pid"])):
                raise BackupError("Destination already exists; existing data is never overwritten")
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) != (marker[0].st_dev, marker[0].st_ino):
                    continue
                if not destination.is_dir() or next(destination.iterdir(), None) is not None:
                    raise BackupError("Destination already exists; existing data is never overwritten")
                destination.rmdir()
                _fsync_directory(path.parent)
                _unlink_reservation_inode(path, marker[0])
            except FileNotFoundError:
                continue
            # The stale marker is removed only after its empty reservation
            # directory has been removed.  A target changed by another writer
            # remains untouched and retains its marker for manual inspection.

        token = secrets.token_hex(16)
        payload = {
            "format": FORMAT,
            "version": VERSION,
            "purpose": "destination-reservation",
            "pid": os.getpid(),
            "token": token,
        }
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            marker = _read_reservation(path)
            if marker is None or _pid_is_alive(int(marker[1]["pid"])):
                raise BackupError("Destination reservation already exists")
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) != (marker[0].st_dev, marker[0].st_ino):
                    continue
                _unlink_reservation_inode(path, marker[0])
            except FileNotFoundError:
                continue
            continue
        marker_info = None
        try:
            marker_info = os.fstat(descriptor)
            with os.fdopen(descriptor, "w", encoding="ascii") as output:
                json.dump(payload, output, ensure_ascii=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            _fsync_directory(path.parent)
            try:
                destination.mkdir(mode=0o700)
            except FileExistsError as error:
                raise BackupError("Destination already exists; existing data is never overwritten") from error
            destination_info = destination.lstat()
            return path, marker_info, token, destination_info
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            _unlink_reservation_inode(path, marker_info)
            raise
    raise BackupError("Destination reservation changed during maintenance")


@contextmanager
def _new_destination(destination: Path):
    """Publish a complete tree behind crash-detectable target reservations."""
    reservation = None
    reservation_info = None
    reservation_token = ""
    destination_info = None
    staging = None
    try:
        reservation, reservation_info, reservation_token, destination_info = _acquire_reservation(destination)
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent))
        yield staging
        for current, _, _ in os.walk(staging, topdown=False):
            _fsync_directory(Path(current))
        current_reservation = _read_reservation(reservation)
        if (
            current_reservation is None
            or current_reservation[1].get("token") != reservation_token
            or current_reservation[1].get("pid") != os.getpid()
            or (current_reservation[0].st_dev, current_reservation[0].st_ino)
            != (reservation_info.st_dev, reservation_info.st_ino)
        ):
            raise BackupError("Destination reservation changed during maintenance")
        current_destination = destination.lstat()
        if (
            (current_destination.st_dev, current_destination.st_ino)
            != (destination_info.st_dev, destination_info.st_ino)
            or not stat.S_ISDIR(current_destination.st_mode)
            or next(destination.iterdir(), None) is not None
        ):
            raise BackupError("Destination changed during maintenance")
        os.replace(staging, destination)
        _fsync_directory(destination.parent)
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
        _remove_empty_owned_destination(destination, destination_info)
        if reservation is not None:
            _unlink_owned_reservation(reservation, reservation_token, reservation_info)


def _separate(first: Path, second: Path) -> None:
    if first == second or first in second.parents or second in first.parents:
        raise BackupError("Data and backup directories must be separate trees")


def create_backup(data_root: str | Path, backup_dir: str | Path, *, stopped: bool = False) -> dict:
    if not stopped:
        raise BackupError("Stop the plugin and pass --stopped before backing up")
    source = _root(data_root)
    destination = _root(backup_dir, exists=False)
    _separate(source, destination)
    files, directories = _inventory(source)
    sqlite_files = {name for name in files if _is_sqlite(source / name)}
    companions = {name + suffix for name in sqlite_files for suffix in ("-wal", "-shm", "-journal")}
    sqlite_content_hashes = {
        name: _checksum(source / name)[1]
        for name in sqlite_files | {
            companion
            for name in sqlite_files
            for companion in (name + "-wal", name + "-journal")
            if companion in files
        }
    }
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "original_data_root": str(source),
        "offline": True,
        "directories": directories,
        "files": [],
        "excluded_sqlite_companions": sorted(set(files) & companions),
    }
    with _new_destination(destination) as staging:
        payload = staging / "data"
        payload.mkdir(mode=0o700)
        for name in sorted(directories, key=lambda value: (value.count("/"), value)):
            (payload / name).mkdir(mode=0o700)
        for name in sorted(set(files) - companions):
            kind = "sqlite" if name in sqlite_files else "file"
            if kind == "sqlite":
                _backup_sqlite(source / name, payload / name)
            else:
                _copy_file(source / name, payload / name)
            metadata = _validate_file(payload / name, kind)
            size, digest = _checksum(payload / name)
            manifest["files"].append({"path": name, "size": size, "sha256": digest, "kind": kind, **metadata})
        # Offline maintenance is still required: this detects ordinary file
        # changes but does not provide a transaction spanning multiple stores.
        after, after_directories = _inventory(source)
        fingerprint = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        # A read-only connection can create empty WAL/SHM companions and update
        # SHM read marks. Existing WALs and new WALs containing data stay checked.
        shared_memory = {name + "-shm" for name in sqlite_files}
        new_empty_wals = {
            name + "-wal" for name in sqlite_files
            if name + "-wal" not in files and name + "-wal" in after and after[name + "-wal"].st_size == 0
        }
        reader_side_effects = shared_memory | new_empty_wals
        checked_files = set(files) - reader_side_effects
        if after_directories != directories or set(after) - reader_side_effects != checked_files or any(
            fingerprint(files[name]) != fingerprint(after[name]) for name in checked_files
        ):
            raise BackupError("Source changed while backing up; stop all writers and retry")
        try:
            sqlite_changed = any(
                _checksum(source / name)[1] != digest
                for name, digest in sqlite_content_hashes.items()
            )
        except OSError as error:
            raise BackupError("Source changed while backing up; stop all writers and retry") from error
        if sqlite_changed:
            raise BackupError("Source changed while backing up; stop all writers and retry")
        descriptor = os.open(staging / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(manifest, output, ensure_ascii=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        _verify_payload(payload, manifest)
    return manifest


def _manifest(backup: Path) -> dict:
    files, directories = _inventory(backup)
    if "manifest.json" not in files or "data" not in directories:
        raise BackupError("Backup is incomplete: manifest.json and data/ are required")
    for path in [backup, *(backup / name for name in files), *(backup / name for name in directories)]:
        _private(path)
    with (backup / "manifest.json").open("r", encoding="utf-8") as incoming:
        manifest = json.load(incoming)
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or type(manifest.get("version")) is not int or manifest["version"] != VERSION:
        raise BackupError("Unsupported backup manifest")
    if manifest.get("offline") is not True or not isinstance(manifest.get("files"), list) or not isinstance(manifest.get("directories"), list):
        raise BackupError("Invalid backup manifest")
    if not isinstance(manifest.get("created_at"), str) or not manifest["created_at"]:
        raise BackupError("Invalid backup creation time")
    expected_directories = {"data", *("data/" + _relative(name) for name in manifest["directories"])}
    if set(directories) != expected_directories:
        raise BackupError("Backup contains missing or unlisted directories")
    if set(files) - {"manifest.json"} != {"data/" + _relative(item.get("path")) for item in manifest["files"] if isinstance(item, dict)}:
        raise BackupError("Backup contains missing or unlisted files")
    return manifest


def _verify_payload(payload: Path, manifest: dict) -> None:
    files, directories = _inventory(payload)
    expected_directories = [_relative(value) for value in manifest["directories"]]
    if len(expected_directories) != len(set(expected_directories)) or set(directories) != set(expected_directories):
        raise BackupError("Backup directory inventory does not match manifest")
    seen = set()
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise BackupError("Invalid backup file entry")
        name = _relative(item.get("path"))
        if name in seen or name not in files or not isinstance(item.get("kind"), str) or item["kind"] not in {"sqlite", "file"}:
            raise BackupError("Invalid or duplicate backup file entry")
        seen.add(name)
        if type(item.get("size")) is not int or item["size"] < 0 or not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise BackupError("Invalid backup checksum entry")
        if _checksum(payload / name) != (item["size"], item["sha256"]):
            raise BackupError(f"Backup checksum mismatch: {name}")
        metadata = _validate_file(payload / name, item["kind"])
        if any(item.get(key) != value for key, value in metadata.items()):
            raise BackupError(f"SQLite schema metadata mismatch: {name}")
    if seen != set(files):
        raise BackupError("Backup contains missing or unlisted files")


def verify_backup(backup_dir: str | Path) -> dict:
    backup = _root(backup_dir)
    manifest = _manifest(backup)
    _verify_payload(backup / "data", manifest)
    return manifest


def restore_backup(backup_dir: str | Path, data_root: str | Path, *, stopped: bool = False) -> dict:
    if not stopped:
        raise BackupError("Stop the plugin and pass --stopped before restoring")
    backup = _root(backup_dir)
    destination = _root(data_root, exists=False)
    _separate(backup, destination)
    manifest = verify_backup(backup)
    with _new_destination(destination) as staging:
        for name in sorted(manifest["directories"], key=lambda value: (value.count("/"), value)):
            (staging / name).mkdir(mode=0o700)
        for item in manifest["files"]:
            name = item["path"]
            _copy_file(backup / "data" / name, staging / name)
        # Repeat validation after copying so a changed backup cannot publish
        # a partially copied or corrupt destination.
        _verify_payload(staging, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in {"backup", "verify", "restore", "-h", "--help"}:
        arguments.insert(0, "backup")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "verify", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--backup-dir", required=True)
        if name != "verify":
            command.add_argument("--data-root", required=True)
            command.add_argument("--stopped", action="store_true", help="plugin and other writers are stopped")
    args = parser.parse_args(arguments)
    try:
        if args.command == "backup":
            manifest = create_backup(args.data_root, args.backup_dir, stopped=args.stopped)
        elif args.command == "restore":
            manifest = restore_backup(args.backup_dir, args.data_root, stopped=args.stopped)
        else:
            manifest = verify_backup(args.backup_dir)
    except (BackupError, OSError, sqlite3.DatabaseError, ValueError) as error:
        # Do not print JSON bodies, SQLite row contents or configuration values.
        print(f"{args.command} failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"command": args.command, "ok": True, "files": len(manifest["files"]), "created_at": manifest["created_at"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
