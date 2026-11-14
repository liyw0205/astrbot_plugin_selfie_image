"""Create and rehearse private, offline backups of an explicit plugin data root."""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager, suppress
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import sys
import tempfile


FORMAT = "selfie-image-offline-backup"
VERSION = 1
SQLITE_HEADER = b"SQLite format 3\x00"
CHUNK_SIZE = 1024 * 1024


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


@contextmanager
def _new_destination(destination: Path):
    """Publish a complete tree over an empty reservation owned by this call."""
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError as error:
        raise BackupError("Destination already exists; existing data is never overwritten") from error
    reserved = destination.stat()
    staging = None
    published = False
    try:
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent))
        yield staging
        for current, _, _ in os.walk(staging, topdown=False):
            _fsync_directory(Path(current))
        current = destination.lstat()
        if (current.st_dev, current.st_ino) != (reserved.st_dev, reserved.st_ino) or next(destination.iterdir(), None):
            raise BackupError("Destination changed during maintenance")
        os.replace(staging, destination)
        published = True
        _fsync_directory(destination.parent)
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
        if not published:
            with suppress(OSError):
                current = destination.lstat()
                if (current.st_dev, current.st_ino) == (reserved.st_dev, reserved.st_ino):
                    destination.rmdir()


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
