import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import zipfile

import pytest

from astrbot_plugin_selfie_image.features.backup_manager import (
    BackupManager,
    BackupManagerError,
    BACKUP_FORMAT,
    BACKUP_VERSION,
    MAX_BACKUPS,
)
from astrbot_plugin_selfie_image.features import backup_manager as backup_module


@pytest.fixture
def manager_factory(tmp_path):
    managers = []

    def create(root=None, **kwargs):
        manager = BackupManager(root or _root(tmp_path), **kwargs)
        managers.append(manager)
        return manager

    yield create
    for manager in managers:
        manager.close()


def _wait(manager: BackupManager, task_id: str) -> dict:
    for _ in range(500):
        task = manager.task(task_id)
        if task["status"] in {"completed", "failed", "cancelled"} and task_id not in manager._futures:
            return task
        time.sleep(0.005)
    raise AssertionError("backup task did not finish")


def _root(tmp_path: Path) -> Path:
    (tmp_path / "selfie_image_config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "wardrobe").mkdir()
    (tmp_path / "wardrobe" / "metadata.json").write_text('{"items": []}', encoding="utf-8")
    return tmp_path


def test_selectable_backup_uses_storage_requirements_and_wardrobe_is_optional(tmp_path):
    manager = BackupManager(_root(tmp_path))
    options = {item["id"]: item for item in manager.options()}
    assert options["config"]["required"] is True
    assert options["config"]["default_selected"] is True
    assert options["wardrobe"]["required"] is False
    assert options["wardrobe"]["default_selected"] is True
    with pytest.raises(BackupManagerError, match="核心"):
        manager.start_backup(["wardrobe"])

    task = manager.start_backup(["config"])
    finished = _wait(manager, task["task_id"])
    assert finished["status"] == "completed"
    assert finished["percent"] == 100
    listing = manager.list_backups()
    assert listing["count"] == 1
    assert listing["items"][0]["selected"] == ["config"]


def test_directory_sync_is_skipped_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(backup_module.os, "name", "nt")
    monkeypatch.setattr(
        backup_module.os,
        "open",
        lambda *_args, **_kwargs: pytest.fail("Windows must not open directories through os.open"),
    )

    BackupManager._sync_directory(tmp_path)


def test_backup_limit_is_enforced_before_background_work(tmp_path):
    manager = BackupManager(_root(tmp_path))
    for _ in range(MAX_BACKUPS):
        task = manager.start_backup(["config"])
        assert _wait(manager, task["task_id"])["status"] == "completed"
    with pytest.raises(BackupManagerError, match="最多保留"):
        manager.start_backup(["config"])


@pytest.mark.parametrize("uploaded", [False, True])
@pytest.mark.parametrize("selected", [["config"], ["wardrobe"]])
def test_partial_restore_survives_restart_and_preserves_unselected_data(tmp_path, manager_factory, uploaded, selected):
    manager = manager_factory(defer_restore=True)
    archive = _wait(manager, manager.start_backup(["config", "wardrobe"])["task_id"])["result"]
    config = tmp_path / "selfie_image_config.json"
    wardrobe = tmp_path / "wardrobe" / "metadata.json"
    originals = {"config": config.read_bytes(), "wardrobe": wardrobe.read_bytes()}
    config.write_bytes(b'{"current":true}')
    wardrobe.write_bytes(b'{"items":["current"]}')
    current = {"config": config.read_bytes(), "wardrobe": wardrobe.read_bytes()}
    if uploaded:
        request = manager.upload_backup("uploaded.zip", (manager.backup_root / archive["filename"]).read_bytes(), selected)
    else:
        request = manager.start_restore(archive["id"], selected)
    finished = _wait(manager, request["task_id"])
    assert finished["status"] == "completed", finished
    assert finished["result"]["restore_selected"] == selected
    assert config.read_bytes() == current["config"]
    assert wardrobe.read_bytes() == current["wardrobe"]
    manager.close()
    restarted = manager_factory(root=tmp_path, defer_restore=True)
    assert restarted.apply_pending_restore()
    for name, path in (("config", config), ("wardrobe", wardrobe)):
        assert path.read_bytes() == (originals[name] if name in selected else current[name])
    assert not restarted.restore_pending
    assert not restarted.apply_pending_restore()


@pytest.mark.parametrize("selected", [[], "config", [True], ["unknown"], ["wardrobe"]])
def test_invalid_restore_selection_does_not_queue_or_retain_upload(manager_factory, selected):
    manager = manager_factory(defer_restore=True)
    archive = _wait(manager, manager.start_backup(["config"])["task_id"])["result"]
    with pytest.raises(BackupManagerError, match="恢复数据项"):
        manager.start_restore(archive["id"], selected)
    with pytest.raises(BackupManagerError, match="恢复数据项"):
        manager.upload_backup("uploaded.zip", (manager.backup_root / archive["filename"]).read_bytes(), selected)
    assert not manager.restore_pending
    assert manager.list_backups()["count"] == 1


def test_restore_upload_selection_skips_options_absent_from_archive(manager_factory):
    manager = manager_factory()
    archive = _wait(manager, manager.start_backup(["config"])["task_id"])["result"]
    request = manager.start_restore(archive["id"], ["config", "wardrobe"])
    finished = _wait(manager, request["task_id"])
    assert finished["status"] == "completed"
    assert finished["result"]["restore_selected"] == ["config"]


def test_backup_uses_actual_cos_pool_storage_path(tmp_path):
    root = _root(tmp_path)
    (root / "cos_pools.json").write_text('{"favorites": ["miku"]}', encoding="utf-8")
    manager = BackupManager(root)
    task = manager.start_backup(["config", "presets"])
    assert _wait(manager, task["task_id"])["status"] == "completed"
    archive = next((root / "backups").glob("*.zip"))
    with zipfile.ZipFile(archive) as incoming:
        assert "data/cos_pools.json" in incoming.namelist()


def test_wardrobe_export_import_round_trip(tmp_path):
    manager = BackupManager(_root(tmp_path))
    original = (tmp_path / "wardrobe" / "metadata.json").read_text(encoding="utf-8")
    exported = manager.export_wardrobe()
    (tmp_path / "wardrobe" / "metadata.json").write_text('{"items": [{"id": "changed"}]}', encoding="utf-8")
    import base64

    manager.import_wardrobe(base64.b64decode(exported["content_base64"]))
    assert (tmp_path / "wardrobe" / "metadata.json").read_text(encoding="utf-8") == original


def test_tampered_backup_is_rejected(tmp_path):
    manager = BackupManager(_root(tmp_path))
    task = manager.start_backup(["config"])
    assert _wait(manager, task["task_id"])["status"] == "completed"
    archive = next((tmp_path / "backups").glob("*.zip"))
    temporary = archive.with_suffix(".tampered")
    with zipfile.ZipFile(archive, "r") as incoming, zipfile.ZipFile(temporary, "w") as output:
        for item in incoming.infolist():
            data = incoming.read(item.filename)
            if item.filename.startswith("data/"):
                data += b"tampered"
            output.writestr(item, data)
    temporary.replace(archive)
    assert manager.list_backups()["count"] == 0


def test_web_backup_listing_can_skip_full_checksum_scan(tmp_path):
    manager = BackupManager(_root(tmp_path))
    task = manager.start_backup(["config"])
    result = _wait(manager, task["task_id"])["result"]
    verify = manager._verify_archive_checksums
    calls = []

    def counted(*args):
        calls.append(True)
        return verify(*args)

    manager._verify_archive_checksums = counted
    assert manager.list_backups(verify=False)["count"] == 1
    assert calls == []
    assert manager.download_file(result["id"])["filename"] == result["filename"]
    assert calls == [True]


@pytest.mark.parametrize("failed_root", ["selfie_image_config.json", "wardrobe"])
def test_restore_publish_failure_preserves_all_original_roots(tmp_path, monkeypatch, failed_root):
    root = _root(tmp_path)
    manager = BackupManager(root)
    archive = manager.backup_root / "test-restore.zip"
    manager._build_archive(archive, ["config", "wardrobe"])
    config = root / "selfie_image_config.json"
    wardrobe = root / "wardrobe" / "metadata.json"
    config.write_text('{"state":"current"}', encoding="utf-8")
    wardrobe.write_text('{"items":["current"]}', encoding="utf-8")
    originals = {path: path.read_bytes() for path in (config, wardrobe)}
    replace = os.replace

    def fail_publish(source, destination):
        if Path(source).parent.name == "data" and Path(destination) == root / failed_root:
            raise OSError("injected publish failure")
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_publish)
    with pytest.raises(OSError, match="injected publish failure"):
        manager._restore_archive(archive)
    for path, content in originals.items():
        assert path.is_file(), f"original missing: {path.name}"
        assert path.read_bytes() == content


def test_online_backup_includes_committed_wal_records(tmp_path):
    root = _root(tmp_path)
    manager = BackupManager(root)
    database = root / "generation_records.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE audit (id TEXT)")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("INSERT INTO audit VALUES ('committed')")
        connection.commit()
        assert Path(str(database) + "-wal").stat().st_size > 0
        archive = manager.backup_root / "test-wal.zip"
        manager._build_archive(archive, ["config", "records"])
        manager._read_manifest(archive)
        with zipfile.ZipFile(archive) as incoming:
            restored = incoming.extract("data/generation_records.sqlite3", root / "verify")
        with sqlite3.connect(restored) as snapshot:
            assert snapshot.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert snapshot.execute("SELECT id FROM audit").fetchall() == [("committed",)]
    connection.close()


@pytest.mark.parametrize("kind,selected", [("wardrobe-export", ["wardrobe"]), ("backup", ["config"])])
def test_import_rejects_roots_outside_selected_items_before_writing(tmp_path, kind, selected):
    root = _root(tmp_path)
    manager = BackupManager(root)
    relative = "selfie_image_config.json" if kind == "wardrobe-export" else "wardrobe/metadata.json"
    archive_root = "selfie_image_config.json" if kind == "wardrobe-export" else "wardrobe"
    target = root / relative
    original = target.read_bytes()
    content = b'{"unexpected":true}'
    manifest = {
        "format": BACKUP_FORMAT, "version": BACKUP_VERSION,
        "kind": kind, "selected": selected, "roots": [archive_root],
        "files": [{"path": relative, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        output.writestr("manifest.json", json.dumps(manifest))
        output.writestr("data/" + relative, content)
    with pytest.raises(BackupManagerError):
        if kind == "wardrobe-export":
            manager.import_wardrobe(buffer.getvalue())
        else:
            manager.upload_backup("test.zip", buffer.getvalue())
    assert target.read_bytes() == original


def test_close_drains_running_work_cancels_queued_work_and_rejects_submissions(manager_factory):
    manager = manager_factory()
    manager._executor.shutdown()
    manager._executor = ThreadPoolExecutor(max_workers=1)
    entered = threading.Event()
    release = threading.Event()

    def blocking():
        entered.set()
        assert release.wait(5)
        return {}

    first = manager._submit(manager._new_task("test"), blocking)
    assert entered.wait(2)
    second = manager._submit(manager._new_task("test"), lambda: {})
    manager.close(wait=False)
    with pytest.raises(BackupManagerError, match="关闭"):
        manager.start_backup(["config"])
    release.set()
    manager.close()
    manager.close()
    assert manager.task(first["task_id"])["status"] == "completed"
    assert manager.task(second["task_id"])["status"] == "cancelled"
    assert not manager._futures
    assert not any(thread.is_alive() for thread in manager._executor._threads)


def test_submit_race_with_close_releases_reserved_slot(manager_factory, monkeypatch):
    manager = manager_factory()
    submit = manager._executor.submit

    def close_before_submit(*args, **kwargs):
        manager.close(wait=False)
        return submit(*args, **kwargs)

    monkeypatch.setattr(manager._executor, "submit", close_before_submit)
    with pytest.raises(RuntimeError):
        manager.start_backup(["config"])
    assert manager._reserved_slots == 0
    assert manager._tasks == {}


def test_completed_task_history_is_bounded_and_expires(manager_factory, monkeypatch):
    manager = manager_factory()
    monkeypatch.setattr(backup_module, "MAX_TASK_HISTORY", 3)
    for _ in range(8):
        task = manager._submit(manager._new_task("test"), lambda: {})
        assert _wait(manager, task["task_id"])["status"] == "completed"
    manager.close()
    assert len(manager._tasks) == 3
    monkeypatch.setattr(backup_module, "TASK_HISTORY_TTL", -1)
    with manager._lock:
        manager._prune_tasks_locked()
    assert manager._tasks == {}


def test_deferred_restore_does_not_touch_live_data_and_needs_no_polling(manager_factory):
    manager = manager_factory(defer_restore=True)
    config = manager.data_root / "selfie_image_config.json"
    task = manager.start_backup(["config"])
    archive = _wait(manager, task["task_id"])["result"]
    config.write_text('{"live":true}', encoding="utf-8")
    request = manager.start_restore(archive["id"])
    future = manager._futures.get(request["task_id"])
    if future is not None:
        future.result(timeout=5)
    manager.close()
    assert manager.task(request["task_id"])["result"]["restart_required"] is True
    assert manager.restore_pending
    assert config.read_text(encoding="utf-8") == '{"live":true}'
    restarted = manager_factory(manager.data_root, defer_restore=True)
    assert restarted.apply_pending_restore()
    assert config.read_text(encoding="utf-8") == "{}"
    assert not restarted.restore_pending
    assert not restarted.apply_pending_restore()


@pytest.mark.parametrize("archive_state", ["missing", "directory", "symlink"])
def test_incomplete_pending_restore_fails_before_touching_live_data(manager_factory, archive_state):
    manager = manager_factory(defer_restore=True)
    pending = manager.backup_root / "pending-restore"
    pending.mkdir()
    archive = pending / "archive.zip"
    if archive_state == "directory":
        archive.mkdir()
    elif archive_state == "symlink":
        valid = manager.backup_root / "pending-test.zip"
        manager._build_archive(valid, ["config"])
        archive.symlink_to(valid)
    manager.close()
    restarted = manager_factory(manager.data_root, defer_restore=True)
    config = manager.data_root / "selfie_image_config.json"
    config.write_text('{"live":true}', encoding="utf-8")
    with pytest.raises(BackupManagerError, match="待恢复归档"):
        restarted.apply_pending_restore()
    assert config.read_text(encoding="utf-8") == '{"live":true}'
    assert pending.is_dir()
    assert restarted.restore_pending


def test_failed_rollback_preserves_originals_for_next_startup(manager_factory, monkeypatch):
    manager = manager_factory()
    archive = manager.backup_root / "rollback-test.zip"
    manager._build_archive(archive, ["config"])
    target = manager.data_root / "selfie_image_config.json"
    target.write_text('{"live":true}', encoding="utf-8")
    replace = os.replace

    def fail_publish_and_rollback(source, destination):
        if Path(destination) == target:
            raise OSError("injected disk failure")
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_publish_and_rollback)
    with pytest.raises(OSError, match="injected disk failure"):
        manager._restore_archive(archive)
    original = manager._transaction / "rollback" / target.name
    assert original.read_text(encoding="utf-8") == '{"live":true}'
    monkeypatch.setattr(os, "replace", replace)
    manager._recover_restore_transaction()
    assert target.read_text(encoding="utf-8") == '{"live":true}'
    assert not manager._transaction.exists()


@pytest.mark.parametrize("crash_after", ["move-original", "publish-new"])
def test_process_exit_during_restore_rolls_back_and_can_replay(manager_factory, crash_after):
    manager = manager_factory(defer_restore=True)
    config = manager.data_root / "selfie_image_config.json"
    archive = manager.backup_root / "crash-test.zip"
    manager._build_archive(archive, ["config"])
    config.write_text('{"live":true}', encoding="utf-8")
    request = manager.start_restore(archive.stem)
    assert _wait(manager, request["task_id"])["status"] == "completed"
    manager.close()
    script = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from astrbot_plugin_selfie_image.features.backup_manager import BackupManager
manager = BackupManager(sys.argv[2], defer_restore=True)
replace = os.replace
def crash(source, destination):
    result = replace(source, destination)
    source, destination = Path(source), Path(destination)
    if ((sys.argv[3] == 'move-original' and destination.parent.name == 'rollback') or
        (sys.argv[3] == 'publish-new' and source.parent.name == 'data')):
        os._exit(73)
    return result
os.replace = crash
manager.apply_pending_restore()
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(Path(__file__).resolve().parents[2]), str(manager.data_root), crash_after],
        capture_output=True, text=True, timeout=10,
    )
    assert process.returncode == 73, process.stderr
    restarted = manager_factory(manager.data_root, defer_restore=True)
    restarted._recover_restore_transaction()
    assert config.read_text(encoding="utf-8") == '{"live":true}'
    assert restarted.apply_pending_restore()
    assert config.read_text(encoding="utf-8") == "{}"


def test_restore_discards_old_sqlite_wal_and_shm(manager_factory):
    manager = manager_factory()
    database = manager.data_root / "generation_records.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE audit (id TEXT)")
    connection.close()
    archive = manager.backup_root / "sqlite-restore.zip"
    manager._build_archive(archive, ["config", "records"])
    for suffix in ("-wal", "-shm"):
        Path(str(database) + suffix).write_bytes(b"stale-live-database-sidecar")
    manager._restore_archive(archive)
    assert not Path(str(database) + "-wal").exists()
    assert not Path(str(database) + "-shm").exists()
    with sqlite3.connect(database) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    restored.close()


def test_repeated_listing_reuses_checksum_validation_but_revalidates_changed_files(manager_factory, monkeypatch):
    manager = manager_factory()
    task = manager.start_backup(["config"])
    result = _wait(manager, task["task_id"])["result"]
    verify = manager._verify_archive_checksums
    calls = []

    def counted(*args):
        calls.append(True)
        return verify(*args)

    monkeypatch.setattr(manager, "_verify_archive_checksums", counted)
    for _ in range(5):
        assert manager.list_backups()["count"] == 1
    assert len(calls) == 1
    archive = manager.backup_root / result["filename"]
    with zipfile.ZipFile(archive) as incoming:
        entries = [(item, incoming.read(item.filename)) for item in incoming.infolist()]
    with zipfile.ZipFile(archive, "w") as output:
        for item, data in entries:
            output.writestr(item, b"[]" if item.filename.startswith("data/") else data)
    assert manager.list_backups()["count"] == 0
    assert len(calls) == 2


@pytest.mark.parametrize("budget,value", [("MAX_ARCHIVE_FILES", 1), ("MAX_ARCHIVE_UNCOMPRESSED_BYTES", 2), ("MAX_UPLOAD_BYTES", 1), ("MAX_MANIFEST_BYTES", 1)])
def test_creation_enforces_reader_budgets_without_publishing_partial_archive(manager_factory, monkeypatch, budget, value):
    manager = manager_factory()
    monkeypatch.setattr(backup_module, budget, value)
    task = manager.start_backup(["config", "wardrobe"])
    assert _wait(manager, task["task_id"])["status"] == "failed"
    assert list(manager.backup_root.glob("*.zip")) == []
    assert manager._reserved_slots == 0


def test_chunk_download_reassembles_archive_and_checks_revision(manager_factory):
    import base64

    manager = manager_factory()
    task = manager.start_backup(["config"])
    result = _wait(manager, task["task_id"])["result"]
    data = bytearray()
    revision = ""
    while True:
        chunk = manager.download_chunk(result["id"], len(data), 31, revision)
        data.extend(base64.b64decode(chunk["content_base64"]))
        assert chunk["next_offset"] == len(data)
        revision = chunk["revision"]
        if chunk["eof"]:
            break
    assert bytes(data) == (manager.backup_root / result["filename"]).read_bytes()
    for offset, length in [(-1, 1), (0, 0), (len(data) + 1, 1), (0, backup_module.CHUNK_SIZE + 1)]:
        with pytest.raises(BackupManagerError):
            manager.download_chunk(result["id"], offset, length)
    with pytest.raises(BackupManagerError, match="变化"):
        manager.download_chunk(result["id"], 0, 31, "stale-revision")


def test_legacy_json_download_has_a_bounded_fallback(manager_factory, monkeypatch):
    manager = manager_factory()
    task = manager.start_backup(["config"])
    result = _wait(manager, task["task_id"])["result"]
    assert manager.download_backup(result["id"])["content_base64"]
    monkeypatch.setattr(backup_module, "MAX_JSON_DOWNLOAD_BYTES", 1)
    with pytest.raises(BackupManagerError, match="分块"):
        manager.download_backup(result["id"])
    assert manager.download_chunk(result["id"], 0, 31)["content_base64"]


def test_uploaded_archive_uses_local_id_in_listing(manager_factory):
    manager = manager_factory(defer_restore=True)
    task = manager.start_backup(["config"])
    result = _wait(manager, task["task_id"])["result"]
    content = (manager.backup_root / result["filename"]).read_bytes()
    upload = manager.upload_backup("original-name.zip", content)
    uploaded = _wait(manager, upload["task_id"])
    assert uploaded["status"] == "completed"
    items = manager.list_backups()["items"]
    assert len({item["id"] for item in items}) == 2
    for item in items:
        assert manager.download_file(item["id"])["filename"] == item["filename"]
