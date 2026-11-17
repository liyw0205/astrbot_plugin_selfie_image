"""Bounded transfer lifecycle and integrity, without a host or real user data."""

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
import zlib

import pytest

from astrbot_plugin_selfie_image.features.archive_transfer import (
    ArchiveTransferStore, CHUNK_SIZE, MAX_UPLOAD_BYTES,
)


def append(store, transfer_id, offset, data):
    return store.append_upload(transfer_id, offset, data, f"{zlib.crc32(data):08x}")


@pytest.fixture
def store(tmp_path):
    result = ArchiveTransferStore(tmp_path / "transfers")
    yield result
    result.close()


def test_upload_duplicate_and_order_preserve_exact_bytes(store):
    data = b"header-payload-tail"
    session = store.start_upload("wardrobe", len(data), "../wardrobe.zip")
    transfer_id = session["id"]
    assert "path" not in session
    assert session["chunk_size"] == CHUNK_SIZE
    append(store, transfer_id, 0, data[:7])
    assert append(store, transfer_id, 0, data[:7])["next_offset"] == 7
    for offset, chunk in [(0, b"changed"), (8, b"out-of-order"), (6, data[6:10])]:
        with pytest.raises(ValueError):
            append(store, transfer_id, offset, chunk)
    with pytest.raises(ValueError, match="incomplete"):
        with store.consume_upload(transfer_id):
            pytest.fail("incomplete upload escaped")
    append(store, transfer_id, 7, data[7:])
    with store.consume_upload(transfer_id) as (path, info):
        assert path.read_bytes() == data
        assert info["sha256"] == hashlib.sha256(data).hexdigest()
        assert info["filename"] == "wardrobe.zip"
        assert info["kind"] == "wardrobe"
    assert not path.exists()
    with pytest.raises(ValueError, match="expired"):
        with store.consume_upload(transfer_id):
            pytest.fail("upload committed twice")


@pytest.mark.parametrize("size", [0, -1, MAX_UPLOAD_BYTES + 1, True, "12", None])
def test_declared_size_is_bounded_before_creating_files(store, size):
    with pytest.raises(ValueError):
        store.start_upload("backup", size, "file.zip")
    assert not list(store.root.iterdir())


def test_chunk_budget_and_checksum_reject_before_writing(store):
    session = store.start_upload("backup", CHUNK_SIZE, "file.zip")
    transfer_id = session["id"]
    with pytest.raises(ValueError, match="checksum"):
        store.append_upload(transfer_id, 0, b"123", "00000000")
    for data in [b"", b"x" * (CHUNK_SIZE + 1)]:
        with pytest.raises(ValueError):
            append(store, transfer_id, 0, data)
    assert append(store, transfer_id, 0, b"123")["next_offset"] == 3
    assert store.discard(transfer_id) == {"discarded": True}
    assert not list(store.root.iterdir())


def test_upload_corruption_and_consumer_failure_clean_temp_files(store):
    for corrupt in [False, True]:
        session = store.start_upload("backup", 4, "file.zip")
        append(store, session["id"], 0, b"data")
        if corrupt:
            next(store.root.glob("*/archive.zip")).write_bytes(b"evil")
        with pytest.raises(ValueError, match="checksum" if corrupt else "consumer failure"):
            with store.consume_upload(session["id"]):
                raise ValueError("consumer failure")
        assert not list(store.root.iterdir())


def test_capacity_timeout_close_and_restart_cleanup(store, monkeypatch):
    import astrbot_plugin_selfie_image.features.archive_transfer as module

    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    first = store.start_upload("backup", MAX_UPLOAD_BYTES, "first.zip")
    store.start_upload("wardrobe", MAX_UPLOAD_BYTES, "second.zip")
    with pytest.raises(ValueError, match="capacity"):
        store.start_upload("backup", 1, "third.zip")
    now[0] += module.TRANSFER_TTL + 1
    store.cleanup()
    assert not list(store.root.iterdir())
    with pytest.raises(ValueError, match="expired"):
        append(store, first["id"], 0, b"x")
    store.start_upload("backup", 2, "new.zip")
    store.close()
    assert not list(store.root.iterdir())
    with pytest.raises(ValueError, match="closed"):
        store.start_upload("backup", 1, "file.zip")
    (store.root / "abandoned").mkdir()
    with ArchiveTransferStore(store.root) as restarted:
        assert not list(restarted.root.iterdir())


def test_duplicate_chunks_are_serialized_under_concurrency(store):
    session = store.start_upload("backup", 4, "file.zip")
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: append(store, session["id"], 0, b"data"), range(8)))
    assert all(result["next_offset"] == 4 for result in results)
    with store.consume_upload(session["id"]) as (path, _):
        assert path.read_bytes() == b"data"


def test_export_revision_chunk_ranges_and_cleanup(store):
    data = b"exported archive"
    exported = store.create_export(lambda path: path.write_bytes(data))
    first = store.download_export(exported["id"], offset=0, length=4)
    assert base64.b64decode(first["content_base64"]) == data[:4]
    with pytest.raises(ValueError, match="changed"):
        store.download_export(exported["id"], offset=4, length=4, revision="stale")
    for offset, length in [(-1, 4), (0, CHUNK_SIZE + 1), (len(data) + 1, 1)]:
        with pytest.raises(ValueError):
            store.download_export(exported["id"], offset=offset, length=length)
    info = store.download_export(exported["id"], as_file=True)
    assert info["size"] == len(data)
    assert store.discard(exported["id"])["discarded"]
    assert not list(store.root.iterdir())


@pytest.mark.parametrize("failure", ["builder", "empty", "oversize"])
def test_failed_exports_release_files_and_capacity(store, failure):
    def build(path):
        with path.open("wb") as output:
            output.write(b"partial")
            if failure == "builder":
                raise RuntimeError("export failed")
            output.truncate(0 if failure == "empty" else MAX_UPLOAD_BYTES + 1)

    with pytest.raises((ValueError, RuntimeError)):
        store.create_export(build)
    assert not list(store.root.iterdir())
    store.start_upload("backup", MAX_UPLOAD_BYTES, "first.zip")
    store.start_upload("backup", MAX_UPLOAD_BYTES, "second.zip")


def test_export_changed_on_disk_rejects_continuation(store):
    exported = store.create_export(lambda path: path.write_bytes(b"original"))
    first = store.download_export(exported["id"], length=4)
    path = next(store.root.glob("*/archive.zip"))
    replacement = path.with_suffix(".new")
    replacement.write_bytes(b"modified")
    replacement.replace(path)
    with pytest.raises(ValueError, match="changed"):
        store.download_export(exported["id"], offset=4, revision=first["revision"])


def test_periodic_cleanup_expires_abandoned_upload_without_traffic(store, monkeypatch):
    import astrbot_plugin_selfie_image.features.archive_transfer as module

    real_monotonic = module.time.monotonic
    expired = threading.Event()
    monkeypatch.setattr(module, "CLEANUP_INTERVAL", 0.01)
    monkeypatch.setattr(module, "TRANSFER_TTL", 1)
    monkeypatch.setattr(module.time, "monotonic", lambda: real_monotonic() + (2 if expired.is_set() else 0))
    cleaned = threading.Event()
    original_cleanup = store.cleanup

    def cleanup():
        original_cleanup()
        if not list(store.root.iterdir()):
            cleaned.set()

    monkeypatch.setattr(store, "cleanup", cleanup)
    store.start_upload("backup", 4, "abandoned.zip")
    expired.set()
    assert cleaned.wait(2)
    assert not list(store.root.iterdir())


def test_legacy_archive_rejects_oversize_before_copying_text(monkeypatch):
    import astrbot_plugin_selfie_image.main as main_module

    class LargeText(str):
        def __str__(self):
            pytest.fail("oversized legacy archive was coerced before the limit check")

        def strip(self, *_args):
            pytest.fail("oversized legacy archive was copied before the limit check")

    monkeypatch.setattr(main_module, "MAX_JSON_ARCHIVE_BYTES", 3)
    with pytest.raises(ValueError, match="分块"):
        main_module.SelfieImagePlugin._decode_web_archive({"content_base64": LargeText("x" * 2048)}, "备份")


def test_close_waits_for_consumer_then_removes_files(store):
    entered, release = threading.Event(), threading.Event()
    session = store.start_upload("backup", 4, "file.zip")
    append(store, session["id"], 0, b"data")

    def consume():
        with store.consume_upload(session["id"]) as (path, _):
            entered.set()
            assert release.wait(5)
            assert path.read_bytes() == b"data"

    with ThreadPoolExecutor(max_workers=2) as executor:
        consuming = executor.submit(consume)
        assert entered.wait(2)
        closing = executor.submit(store.close)
        assert not closing.done()
        release.set()
        consuming.result(timeout=2)
        closing.result(timeout=2)
    assert not list(store.root.iterdir())


def test_absolute_expiry_is_not_extended_by_repeated_chunks(store, monkeypatch):
    import astrbot_plugin_selfie_image.features.archive_transfer as module

    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    session = store.start_upload("backup", 8, "file.zip")
    append(store, session["id"], 0, b"data")
    for _ in range(module.TRANSFER_LIFETIME // 60 - 1):
        now[0] += 60
        append(store, session["id"], 0, b"data")
    now[0] += 60
    with pytest.raises(ValueError, match="expired"):
        append(store, session["id"], 0, b"data")
    assert not list(store.root.iterdir())


def test_chunked_backup_waits_for_restart_and_legacy_decode_has_preallocation_limit(tmp_path, monkeypatch):
    import astrbot_plugin_selfie_image.main as main_module
    from astrbot_plugin_selfie_image.features.backup_manager import BackupManager
    from astrbot_plugin_selfie_image.main import SelfieImagePlugin
    from astrbot_plugin_selfie_image.tests.test_backup_manager import _root, _wait

    root = _root(tmp_path)
    manager = BackupManager(root, defer_restore=True)
    config = root / "selfie_image_config.json"
    archive = tmp_path / "source.zip"
    manager._build_archive(archive, ["config"])
    original = config.read_bytes()
    config.write_bytes(b'{"changed":true}')
    plugin = object.__new__(SelfieImagePlugin)
    plugin.backup_manager = manager
    plugin._web_task_lock = threading.RLock()
    plugin._assert_backup_restore_idle = lambda: None
    try:
        data = archive.read_bytes()
        session = plugin.start_archive_upload_from_web({"kind": "backup", "size": len(data)})
        plugin.append_archive_upload_from_web(session["id"], {"offset": 0,
            "content_base64": base64.b64encode(data).decode(), "crc32": f"{zlib.crc32(data):08x}"})
        task = plugin.commit_archive_upload_from_web(session["id"])
        assert _wait(manager, task["task_id"])["result"]["restart_required"]
        assert config.read_bytes() == b'{"changed":true}'
        assert not list(manager.transfers.root.iterdir())
        manager.apply_pending_restore()
        assert config.read_bytes() == original
        monkeypatch.setattr(main_module, "MAX_JSON_ARCHIVE_BYTES", 3)
        monkeypatch.setattr(main_module.base64, "b64decode", lambda *_a, **_k: pytest.fail("decoded over budget"))
        with pytest.raises(ValueError, match="分块"):
            plugin._decode_web_archive({"content_base64": "eHh4eHh4"}, "备份")
    finally:
        manager.close()
