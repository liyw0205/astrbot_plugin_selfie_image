"""Reference-cache lookup I/O, compatibility, and reservation regressions."""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from astrbot_plugin_selfie_image.core.providers import ImageReference
from astrbot_plugin_selfie_image.generation.generation_store import GenerationStoreMixin


def make_store(root):
    store = GenerationStoreMixin()
    store.data_dir = str(root)
    store.generated_dir = str(root / "image_cache")
    Path(store.generated_dir).mkdir(exist_ok=True)
    return store


def save(store, data, mime="image/png"):
    return store._save_or_reuse_reference_image(ImageReference(data, mime_type=mime))


def test_warm_unique_misses_do_not_reread_unchanged_cache(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    cache = Path(store.generated_dir)
    for index in range(12):
        (cache / f"request_legacy_{index}.png").write_bytes(bytes([index]) * 4096)
    save(store, b"initial" * 512)
    original_open = builtins.open
    reads = []

    def counted_open(path, mode="r", *args, **kwargs):
        if mode == "rb" and Path(path).name.startswith("request_"):
            reads.append(str(path))
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counted_open)
    for index in range(4):
        save(store, b"new" + bytes([index]) * 4096)
    assert reads == []


def test_legacy_content_reuse_in_nested_directory_with_different_extension(tmp_path):
    store = make_store(tmp_path)
    legacy = Path(store.generated_dir) / "old" / "request_20250101.jpg"
    legacy.parent.mkdir()
    legacy.write_bytes(b"same reference bytes")
    assert save(store, legacy.read_bytes()) == "old/request_20250101.jpg"
    assert list(Path(store.generated_dir).glob("request_*")) == []


def test_cold_legacy_hit_does_not_read_files_after_matching_candidate(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    cache = Path(store.generated_dir)
    legacy = cache / "request_match.jpg"
    legacy.write_bytes(b"reference")
    other = cache / "request_other.png"
    other.write_bytes(b"unrelated")
    original_open = builtins.open
    reads = []

    def counted_open(path, mode="r", *args, **kwargs):
        if mode == "rb":
            reads.append(str(path))
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "walk", lambda _: iter([(str(cache), [], [legacy.name, other.name])]))
    monkeypatch.setattr(builtins, "open", counted_open)
    assert save(store, b"reference") == legacy.name
    assert str(other) not in reads


def test_same_bytes_reuse_deterministic_path_across_mime_types(tmp_path):
    store = make_store(tmp_path)
    first = save(store, b"same reference bytes")
    assert save(store, b"same reference bytes", "image/jpeg") == first
    assert len(list(Path(store.generated_dir).glob("request_*"))) == 1


@pytest.mark.parametrize("nested", [False, True])
def test_external_legacy_addition_is_discovered_after_index_warms(tmp_path, nested):
    store = make_store(tmp_path)
    save(store, b"warm the index")
    directory = Path(store.generated_dir)
    if nested:
        directory = directory / "imported"
        directory.mkdir()
    legacy = directory / "request_added.jpg"
    legacy.write_bytes(b"external legacy reference")
    assert save(store, legacy.read_bytes()) == os.path.relpath(legacy, store.generated_dir)


def test_modified_legacy_file_is_reindexed_even_when_mtime_is_preserved(tmp_path):
    store = make_store(tmp_path)
    legacy = Path(store.generated_dir) / "request_old.png"
    legacy.write_bytes(b"old-data")
    save(store, b"warm the index")
    previous = legacy.stat()
    # Some Android filesystems update ctime only once per short clock tick.
    time.sleep(0.025)
    legacy.write_bytes(b"new-data")
    os.utime(legacy, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    assert save(store, b"new-data") == legacy.name
    original = save(store, b"old-data")
    assert original != legacy.name
    assert (Path(store.generated_dir) / original).read_bytes() == b"old-data"


def test_candidate_bytes_are_verified_even_if_stat_signature_is_unchanged(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    legacy = Path(store.generated_dir) / "request_unchanged_stat.png"
    legacy.write_bytes(b"old-data")
    save(store, b"warm the index")
    signature = store._reference_cache_file_signature(str(legacy))
    signature_getter = store._reference_cache_file_signature
    monkeypatch.setattr(
        store, "_reference_cache_file_signature",
        lambda path: signature if path == str(legacy) else signature_getter(path),
    )
    legacy.write_bytes(b"new-data")
    recovered = save(store, b"old-data")
    assert recovered != legacy.name
    assert (Path(store.generated_dir) / recovered).read_bytes() == b"old-data"
    assert save(store, b"new-data") == legacy.name


def test_deleted_legacy_candidate_recovers_to_deterministic_file(tmp_path):
    store = make_store(tmp_path)
    legacy = Path(store.generated_dir) / "request_deleted.png"
    legacy.write_bytes(b"deleted reference")
    assert save(store, b"deleted reference") == legacy.name
    legacy.unlink()
    recovered = save(store, b"deleted reference")
    assert recovered != legacy.name
    assert (Path(store.generated_dir) / recovered).read_bytes() == b"deleted reference"


def test_corrupt_deterministic_file_is_replaced_without_reusing_wrong_bytes(tmp_path):
    store = make_store(tmp_path)
    reference = b"correct reference"
    first = save(store, reference)
    (Path(store.generated_dir) / first).write_bytes(b"corrupt reference")
    assert save(store, reference) == first
    assert (Path(store.generated_dir) / first).read_bytes() == reference
    assert not list(Path(store.generated_dir).glob("*.tmp"))


def test_corrupt_deterministic_file_can_fall_back_to_legacy_copy(tmp_path):
    store = make_store(tmp_path)
    data = b"correct reference"
    target = Path(store.generated_dir) / f"request_{hashlib.sha256(data).hexdigest()}.png"
    target.write_bytes(b"bad data")
    legacy = Path(store.generated_dir) / "request_legacy.jpg"
    legacy.write_bytes(data)
    assert save(store, data) == legacy.name


def test_restart_rebuilds_index_and_reuses_legacy_with_durable_reservation(tmp_path):
    store = make_store(tmp_path)
    legacy = Path(store.generated_dir) / "request_restart.jpg"
    legacy.write_bytes(b"restart reference")
    save(store, b"unrelated reference")
    restored = make_store(tmp_path)
    assert save(restored, b"restart reference") == legacy.name
    journal = json.loads((tmp_path / "pending_request_media.json").read_text())
    assert legacy.name in journal["paths"]
    assert legacy.name in make_store(tmp_path)._inflight_media_paths()


def test_generated_directory_change_does_not_reuse_previous_directory_index(tmp_path):
    store = make_store(tmp_path)
    legacy = Path(store.generated_dir) / "request_old_root.jpg"
    legacy.write_bytes(b"reference")
    assert save(store, b"reference") == legacy.name
    store.generated_dir = str(tmp_path / "other_cache")
    recovered = save(store, b"reference")
    assert recovered != legacy.name
    assert (Path(store.generated_dir) / recovered).read_bytes() == b"reference"


def test_concurrent_reference_requests_share_file_and_register_reservation(tmp_path):
    store = make_store(tmp_path)
    barrier = threading.Barrier(8)

    def request():
        barrier.wait(timeout=5)
        return save(store, b"concurrent reference")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: request(), range(8)))
    assert len(set(results)) == 1
    assert len(list(Path(store.generated_dir).glob("request_*"))) == 1
    assert store._inflight_media_paths() == [results[0]]


def test_reference_write_and_journal_registration_keep_inflight_lock(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    at_registration = threading.Event()
    release_registration = threading.Event()
    trying_cleanup = threading.Event()
    registration = store._register_inflight_media

    def paused_registration(paths):
        at_registration.set()
        assert release_registration.wait(timeout=5)
        return registration(paths)

    def cleanup_snapshot():
        trying_cleanup.set()
        with store._inflight_media_mutex():
            return store._inflight_media_paths()

    monkeypatch.setattr(store, "_register_inflight_media", paused_registration)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(save, store, b"pending reference")
        try:
            assert at_registration.wait(timeout=5)
            cleanup = pool.submit(cleanup_snapshot)
            assert trying_cleanup.wait(timeout=5)
            assert not cleanup.done()
        finally:
            release_registration.set()
        path = writer.result(timeout=5)
        assert path in cleanup.result(timeout=5)
