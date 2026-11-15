"""Offline recovery exercises using only synthetic temporary plugin data."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

import pytest

from astrbot_plugin_selfie_image.scripts import backup_restore as recovery


@pytest.fixture
def offline_data(tmp_path):
    root = tmp_path / "plugin-data"
    root.mkdir()
    for name in ("image_cache", "media_sources", "image-persona", "creative_canvas", "empty"):
        (root / name).mkdir()
    documents = {
        "selfie_image_config.json": {"api_key": "synthetic-secret-key", "web": {"token": "synthetic-token"}},
        "generation_tasks.json": {"tasks": {"task-1": {"status": "succeeded"}}, "snapshot": {"snapshot_version": 3}},
        "image_persona.json": {"ref_image_path": str(root / "image-persona" / "persona.png")},
        "media_sources/record-1.json": {"record_id": "record-1", "generated_image_sources": [{"type": "cache_path", "value": "image.png"}]},
        "creative_canvas/studio_sessions.json": {"sessions": []},
        "asset_collections.json": {"collections": []},
        "usage_stats.json": {"users": {}},
    }
    for name, document in documents.items():
        (root / name).write_text(json.dumps(document), encoding="utf-8")
    (root / "image_cache" / "image.png").write_bytes(b"synthetic generated media")
    (root / "image-persona" / "persona.png").write_bytes(b"synthetic persona reference")
    connection = sqlite3.connect(root / "generation_records.sqlite3")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE generation_records(id TEXT PRIMARY KEY, payload TEXT)")
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute(
        "INSERT INTO generation_records VALUES (?, ?)",
        ("record-1", json.dumps({"id": "record-1", "media_sources_file": "record-1.json", "generated_image_paths": ["image.png"]})),
    )
    connection.commit()
    yield root, connection
    connection.close()


def assert_private_tree(root):
    for path in (root, *root.rglob("*")):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


def read_manifest(backup):
    return json.loads((backup / "manifest.json").read_text(encoding="utf-8"))


def write_manifest(backup, manifest):
    (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_offline_roundtrip_keeps_wal_rows_sidecars_persona_and_tasks(tmp_path, offline_data):
    root, source_connection = offline_data
    database = root / "generation_records.sqlite3"
    assert database.with_name(database.name + "-wal").stat().st_size > 0
    # immutable=1 intentionally ignores WAL; it proves a plain main-file copy
    # would miss the committed record included by the standard backup API.
    with sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM generation_records").fetchone()[0] == 0

    backup = tmp_path / "snapshot"
    manifest = recovery.create_backup(root, backup, stopped=True)
    assert manifest["offline"] is True
    assert "generation_records.sqlite3-wal" in manifest["excluded_sqlite_companions"]
    assert not list((backup / "data").glob("*-wal"))
    assert not list((backup / "data").glob("*-shm"))
    assert recovery.verify_backup(backup) == manifest
    assert_private_tree(backup)

    source_connection.close()
    root.rename(tmp_path / "before-upgrade")
    recovery.restore_backup(backup, root, stopped=True)
    assert_private_tree(root)
    assert json.loads((root / "selfie_image_config.json").read_text())["api_key"] == "synthetic-secret-key"
    assert json.loads((root / "generation_tasks.json").read_text())["snapshot"]["snapshot_version"] == 3
    assert Path(json.loads((root / "image_persona.json").read_text())["ref_image_path"]).read_bytes() == b"synthetic persona reference"
    assert (root / "media_sources" / "record-1.json").is_file()
    assert (root / "creative_canvas" / "studio_sessions.json").is_file()
    assert (root / "empty").is_dir()
    with sqlite3.connect(root / "generation_records.sqlite3") as connection:
        record = connection.execute("SELECT id, payload FROM generation_records").fetchone()
        assert record[0] == "record-1"
        payload = json.loads(record[1])
        assert (root / "media_sources" / payload["media_sources_file"]).is_file()
        assert (root / "image_cache" / payload["generated_image_paths"][0]).is_file()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_closed_wal_database_can_be_backed_up_without_existing_companions(tmp_path):
    root = tmp_path / "plugin-data"
    root.mkdir()
    database = root / "generation_records.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE records(id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO records VALUES (1)")
    connection.commit()
    connection.close()
    assert {path.name for path in root.iterdir()} == {database.name}

    backup = tmp_path / "snapshot"
    manifest = recovery.create_backup(root, backup, stopped=True)
    assert recovery.verify_backup(backup) == manifest
    with sqlite3.connect(backup / "data" / database.name) as restored:
        assert restored.execute("SELECT id FROM records").fetchall() == [(1,)]


@pytest.mark.parametrize("storage", ["new_wal", "existing_wal", "main_database"])
def test_sqlite_business_writes_during_backup_are_rejected(tmp_path, monkeypatch, storage):
    root = tmp_path / "plugin-data"
    root.mkdir()
    database = root / "generation_records.sqlite3"
    connection = sqlite3.connect(database)
    if storage != "main_database":
        connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE records(id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO records VALUES (1)")
    connection.commit()
    if storage != "existing_wal":
        connection.close()
        connection = None
    original = recovery._backup_sqlite

    def backup_then_commit(source, destination):
        nonlocal connection
        original(source, destination)
        if connection is None:
            connection = sqlite3.connect(database)
        connection.execute("INSERT INTO records VALUES (2)")
        connection.commit()
        if storage != "main_database":
            assert database.with_name(database.name + "-wal").stat().st_size > 0

    monkeypatch.setattr(recovery, "_backup_sqlite", backup_then_commit)
    backup = tmp_path / "snapshot"
    try:
        with pytest.raises(recovery.BackupError, match="Source changed"):
            recovery.create_backup(root, backup, stopped=True)
        assert not backup.exists()
        assert not list(tmp_path.glob(".snapshot.staging-*"))
        assert connection.execute("SELECT id FROM records ORDER BY id").fetchall() == [(1,), (2,)]
    finally:
        if connection is not None:
            connection.close()


def test_default_cli_creates_backup_and_explicit_verify_and_restore_work(tmp_path, offline_data):
    root, _ = offline_data
    backup = tmp_path / "snapshot"
    command = [sys.executable, recovery.__file__]
    created = subprocess.run(
        [*command, "--data-root", str(root), "--backup-dir", str(backup), "--stopped"],
        capture_output=True, text=True, check=False,
    )
    assert created.returncode == 0, created.stderr
    assert json.loads(created.stdout)["command"] == "backup"
    assert "synthetic-secret" not in created.stdout + created.stderr
    verified = subprocess.run([*command, "verify", "--backup-dir", str(backup)], capture_output=True, text=True)
    assert verified.returncode == 0, verified.stderr
    target = tmp_path / "restored"
    restored = subprocess.run(
        [*command, "restore", "--data-root", str(target), "--backup-dir", str(backup), "--stopped"],
        capture_output=True, text=True,
    )
    assert restored.returncode == 0, restored.stderr
    assert json.loads(restored.stdout)["command"] == "restore"


def test_existing_destination_and_missing_offline_ack_are_rejected(tmp_path, offline_data):
    root, _ = offline_data
    backup = tmp_path / "snapshot"
    with pytest.raises(recovery.BackupError, match="--stopped"):
        recovery.create_backup(root, backup)
    assert not backup.exists()
    recovery.create_backup(root, backup, stopped=True)
    with pytest.raises(recovery.BackupError, match="already exists"):
        recovery.create_backup(root, backup, stopped=True)
    with pytest.raises(recovery.BackupError, match="--stopped"):
        recovery.restore_backup(backup, tmp_path / "restored")
    original = (root / "selfie_image_config.json").read_bytes()
    with pytest.raises(recovery.BackupError, match="already exists"):
        recovery.restore_backup(backup, root, stopped=True)
    assert (root / "selfie_image_config.json").read_bytes() == original


@pytest.mark.parametrize("path", ["../escape", "/tmp/escape", "a/../../escape", "a\\escape", "C:/escape", "a//escape", "a/./escape"])
def test_manifest_path_traversal_is_rejected_before_creating_destination(tmp_path, offline_data, path):
    root, _ = offline_data
    backup = tmp_path / "snapshot"
    recovery.create_backup(root, backup, stopped=True)
    manifest = read_manifest(backup)
    manifest["files"][0]["path"] = path
    write_manifest(backup, manifest)
    target = tmp_path / "restored"
    with pytest.raises(recovery.BackupError):
        recovery.restore_backup(backup, target, stopped=True)
    assert not target.exists()


def test_checksum_corruption_and_unlisted_files_or_directories_are_rejected(tmp_path, offline_data):
    root, _ = offline_data
    backup = tmp_path / "snapshot"
    recovery.create_backup(root, backup, stopped=True)
    config = backup / "data" / "selfie_image_config.json"
    original = config.read_bytes()
    config.write_bytes(b'{"api_key":"tampered"}')
    with pytest.raises(recovery.BackupError, match="checksum mismatch"):
        recovery.restore_backup(backup, tmp_path / "restored", stopped=True)
    config.write_bytes(original)
    extra = backup / "data" / "unlisted.txt"
    extra.write_text("unlisted")
    extra.chmod(0o600)
    with pytest.raises(recovery.BackupError, match="unlisted files"):
        recovery.verify_backup(backup)
    extra.unlink()
    extra_directory = backup / "unlisted-directory"
    extra_directory.mkdir(mode=0o700)
    with pytest.raises(recovery.BackupError, match="unlisted directories"):
        recovery.verify_backup(backup)


def test_sqlite_integrity_and_schema_metadata_are_checked(tmp_path, offline_data):
    root, _ = offline_data
    backup = tmp_path / "snapshot"
    recovery.create_backup(root, backup, stopped=True)
    manifest = read_manifest(backup)
    database_entry = next(item for item in manifest["files"] if item["kind"] == "sqlite")
    database_entry["user_version"] += 1
    write_manifest(backup, manifest)
    with pytest.raises(recovery.BackupError, match="schema metadata mismatch"):
        recovery.verify_backup(backup)
    database_entry["user_version"] -= 1
    corrupted = backup / "data" / database_entry["path"]
    corrupted.write_bytes(b"not a SQLite database")
    database_entry.update(size=corrupted.stat().st_size, sha256=hashlib.sha256(corrupted.read_bytes()).hexdigest())
    write_manifest(backup, manifest)
    with pytest.raises((recovery.BackupError, sqlite3.DatabaseError)):
        recovery.verify_backup(backup)


def test_symlinks_special_files_and_nested_backup_paths_are_rejected(tmp_path, offline_data):
    root, _ = offline_data
    backup = tmp_path / "snapshot"
    with pytest.raises(recovery.BackupError, match="separate trees"):
        recovery.create_backup(root, root / "nested", stopped=True)
    link = root / "external-link"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside this explicit data root")
    link.symlink_to(outside)
    with pytest.raises(recovery.BackupError, match="Symbolic links"):
        recovery.create_backup(root, backup, stopped=True)
    link.unlink()
    fifo = root / "special-fifo"
    os.mkfifo(fifo)
    with pytest.raises(recovery.BackupError, match="Special files"):
        recovery.create_backup(root, backup, stopped=True)
    fifo.unlink()
    recovery.create_backup(root, backup, stopped=True)
    config = backup / "data" / "selfie_image_config.json"
    config.unlink()
    config.symlink_to(outside)
    with pytest.raises(recovery.BackupError, match="Symbolic links"):
        recovery.verify_backup(backup)
    assert outside.read_text() == "outside this explicit data root"


def test_private_permissions_are_enforced_on_backup_verification(tmp_path, offline_data):
    root, _ = offline_data
    backup = tmp_path / "snapshot"
    recovery.create_backup(root, backup, stopped=True)
    (backup / "data" / "selfie_image_config.json").chmod(0o644)
    with pytest.raises(recovery.BackupError, match="permissions"):
        recovery.verify_backup(backup)


@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_copy_failure_leaves_no_partial_destination(tmp_path, offline_data, monkeypatch, operation):
    root, _ = offline_data
    backup = tmp_path / "snapshot"
    if operation == "restore":
        recovery.create_backup(root, backup, stopped=True)
    original = recovery._copy_file
    calls = 0

    def fail_second_copy(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated disk failure")
        original(source, destination)

    monkeypatch.setattr(recovery, "_copy_file", fail_second_copy)
    destination = backup if operation == "backup" else tmp_path / "restored"
    with pytest.raises(OSError, match="simulated disk failure"):
        if operation == "backup":
            recovery.create_backup(root, backup, stopped=True)
        else:
            recovery.restore_backup(backup, destination, stopped=True)
    assert not destination.exists()
    assert not list(tmp_path.glob(f".{destination.name}.staging-*"))
    assert (root / "selfie_image_config.json").is_file()
    if operation == "restore":
        assert recovery.verify_backup(backup)["files"]


def test_invalid_source_json_aborts_backup_without_exposing_contents(tmp_path, offline_data, capsys):
    root, _ = offline_data
    (root / "selfie_image_config.json").write_text("{invalid synthetic-secret-key")
    backup = tmp_path / "snapshot"
    assert recovery.main(["--data-root", str(root), "--backup-dir", str(backup), "--stopped"]) == 1
    assert "synthetic-secret-key" not in capsys.readouterr().err
    assert not backup.exists()


def test_source_change_is_detected_and_no_snapshot_is_published(tmp_path, offline_data, monkeypatch):
    root, _ = offline_data
    backup = tmp_path / "snapshot"
    original_copy = recovery._copy_file
    changed = False

    def copy_and_mutate_source(source, destination):
        nonlocal changed
        original_copy(source, destination)
        if not changed:
            changed = True
            (root / "usage_stats.json").write_text('{"users":{"new":1}}')

    monkeypatch.setattr(recovery, "_copy_file", copy_and_mutate_source)
    with pytest.raises(recovery.BackupError, match="Source changed"):
        recovery.create_backup(root, backup, stopped=True)
    assert not backup.exists()


@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_target_created_during_publish_is_never_overwritten(tmp_path, offline_data, monkeypatch, operation):
    root, _ = offline_data
    backup = tmp_path / "snapshot"
    if operation == "restore":
        recovery.create_backup(root, backup, stopped=True)
    target = backup if operation == "backup" else tmp_path / "restored"
    original_copy = recovery._copy_file
    changed = False

    def copy_and_create_target(source, destination):
        nonlocal changed
        original_copy(source, destination)
        if changed:
            return
        changed = True
        # The implementation owns an empty target directory while staging.
        # Simulate an external process removing that reservation and taking
        # the path before publication.
        target.rmdir()
        target.mkdir(mode=0o700)
        owned = target / "operator-owned.txt"
        owned.write_text("keep this tree", encoding="ascii")
        owned.chmod(0o600)

    monkeypatch.setattr(recovery, "_copy_file", copy_and_create_target)
    with pytest.raises(recovery.BackupError):
        if operation == "backup":
            recovery.create_backup(root, backup, stopped=True)
        else:
            recovery.restore_backup(backup, target, stopped=True)
    assert (target / "operator-owned.txt").read_text(encoding="ascii") == "keep this tree"
    assert not list(tmp_path.glob(f".{target.name}.staging-*"))
    if operation == "restore":
        assert recovery.verify_backup(backup)["files"]


def test_live_destination_reservation_blocks_competing_publish(tmp_path, offline_data):
    root, _ = offline_data
    backup = tmp_path / "snapshot"
    reservation = tmp_path / f".{backup.name}{recovery.RESERVATION_SUFFIX}"
    reservation.write_text(
        json.dumps(
            {
                "format": recovery.FORMAT,
                "version": recovery.VERSION,
                "purpose": "destination-reservation",
                "pid": os.getpid(),
                "token": "a" * 32,
            }
        ),
        encoding="ascii",
    )
    reservation.chmod(0o600)
    with pytest.raises(recovery.BackupError, match="reservation"):
        recovery.create_backup(root, backup, stopped=True)
    assert reservation.is_file()
    assert not list(tmp_path.glob(f".{backup.name}.staging-*"))


def test_sqlite_kind_cannot_be_mislabeled_to_skip_integrity_checks(tmp_path, offline_data):
    root, _ = offline_data
    backup = tmp_path / "snapshot"
    recovery.create_backup(root, backup, stopped=True)
    manifest = read_manifest(backup)
    next(item for item in manifest["files"] if item["kind"] == "sqlite")["kind"] = "file"
    write_manifest(backup, manifest)
    with pytest.raises(recovery.BackupError, match="kind mismatch"):
        recovery.verify_backup(backup)


def test_publish_failure_preserves_existing_source_and_backup(tmp_path, offline_data, monkeypatch):
    root, _ = offline_data
    backup = tmp_path / "snapshot"
    recovery.create_backup(root, backup, stopped=True)
    target = tmp_path / "restored"

    def fail(_source, _destination):
        raise OSError("simulated publish failure")

    monkeypatch.setattr(recovery.os, "replace", fail)
    with pytest.raises(OSError, match="publish failure"):
        recovery.restore_backup(backup, target, stopped=True)
    assert not target.exists()
    assert not list(tmp_path.glob(".restored.staging-*"))
    assert (root / "generation_tasks.json").is_file()
    assert recovery.verify_backup(backup)["files"]


def test_directory_read_errors_are_propagated_instead_of_omitting_files(tmp_path, monkeypatch):
    def unreadable(_root, *, followlinks, onerror):
        onerror(PermissionError("simulated unreadable directory"))
        yield "unused", [], []

    monkeypatch.setattr(recovery.os, "walk", unreadable)
    with pytest.raises(PermissionError, match="unreadable directory"):
        recovery._inventory(tmp_path)
