"""Regression coverage for persistence, dedupe, transport and shutdown fixes."""

from __future__ import annotations

import asyncio
import json
import threading
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import brotli
import aiohttp
import pytest

from astrbot_plugin_selfie_image.core.models import AICatConfig, ImageModelTarget
from astrbot_plugin_selfie_image.core.providers import (
    ImageGenerateRequest, ImageReference, NovelAIImageAdapter, _parse_nai_response_body,
)
from astrbot_plugin_selfie_image.core.utils import load_json_file, save_json_file
from astrbot_plugin_selfie_image.generation.image_scheduler import ImageJobScheduler
from astrbot_plugin_selfie_image.generation.record_database import RecordDatabase
from astrbot_plugin_selfie_image.main import SelfieImagePlugin
from astrbot_plugin_selfie_image.tasks.task_manager import WebTaskMixin
from astrbot_plugin_selfie_image.tests.test_storage import TestStorage as StorageFixture


def runtime_plugin(root: Path) -> SelfieImagePlugin:
    plugin = object.__new__(SelfieImagePlugin)
    plugin.config = AICatConfig.from_dict({"web": {"enable": False}})
    plugin._web_task_lock = threading.RLock()
    plugin._web_tasks = {}
    plugin._web_task_seq = 0
    plugin.tasks_path = str(root / "tasks.json")
    plugin._runtime_generation_tasks = {}
    plugin._runtime_generation_workers = {}
    plugin._image_scheduler = ImageJobScheduler(1)
    plugin._get_session_model_override = lambda *_: ""
    plugin._reserve_quota_for_task = lambda *_a, **_kw: ""
    plugin.web_server = SimpleNamespace(stop=lambda: None)

    async def no_failure(*_a, **_kw):
        pass

    plugin._ensure_task_failure_record = no_failure
    return plugin


@pytest.mark.parametrize("protected", ["pinned", "favorite"])
def test_old_protected_asset_and_sidecar_survive_reload(tmp_path, protected):
    original = StorageFixture()._plugin(str(tmp_path))
    original.config.image_record_keep_limit = 2
    original._commit_generation_record({
        "id": "old-protected", protected: True, "success": True,
        "generated_image_sources": [{"type": "url", "value": "https://example.test/old.png"}],
    })
    for name in ("recent-1", "recent-2"):
        original._commit_generation_record({"id": name, "success": True})
    sidecar = Path(original.media_sources_dir) / "old-protected.json"
    revived = StorageFixture()._plugin(str(tmp_path))
    revived.config.image_record_keep_limit = 2
    revived._records = revived._load_records()
    assert len(revived._records) == 3
    assert sidecar.exists()
    assert revived.get_record_for_web("old-protected")["generated_image_sources"]
    rows, meta = revived.query_asset_records(**{protected: True})
    assert [row["id"] for row in rows] == ["old-protected"]
    assert meta["total"] == 3
    revived._persist_records()
    assert len(revived._record_database().load_records(2)) == 3


def test_smaller_retention_prunes_database_without_deleting_shared_media(tmp_path):
    store = StorageFixture()._plugin(str(tmp_path))
    store.config.image_record_keep_limit = 2
    db = store._record_database()
    shared = Path(store.generated_dir) / "shared.png"
    shared.write_bytes(b"shared")
    rows = [{"id": str(i), "time": f"2026-11-12 00:00:0{i}", "generated_image_paths": ["shared.png"]} for i in range(4)]
    db.replace_records(list(reversed(rows)))
    store._records = store._load_records()
    assert [row["id"] for row in store._records] == ["3", "2"]
    assert [row["id"] for row in db.load_records(100)] == ["3", "2"]
    assert shared.exists()


def test_retention_reload_defers_canvas_media_cleanup_until_stores_loaded(tmp_path):
    from astrbot_plugin_selfie_image.studio.studio import StudioStore

    store = StorageFixture()._plugin(str(tmp_path))
    store.config.image_record_keep_limit = 1
    canvas = StudioStore(str(tmp_path))
    session = canvas.create("test canvas")
    canvas.attach_run_finish(session["id"], "old", success=True, result_paths=["canvas.png"])
    media = Path(store.generated_dir) / "canvas.png"
    media.write_bytes(b"image")
    store._record_database().replace_records([
        {"id": "new", "time": "2026-11-13"},
        {"id": "old", "time": "2026-11-12", "generated_image_paths": ["canvas.png"]},
    ])
    store._records = store._load_records()
    assert media.exists()
    store.studio = StudioStore(str(tmp_path))
    for i in range(12):
        (Path(store.generated_dir) / f"orphan-{i}.png").write_bytes(b"orphan")
    store._cleanup_image_cache_if_needed()
    assert media.exists()


def test_startup_uses_saved_retention_before_loading_records(tmp_path, monkeypatch):
    from astrbot_plugin_selfie_image import main

    data = tmp_path / "plugin_data" / "astrbot_plugin_selfie_image"
    db = RecordDatabase(str(data / "generation_records.sqlite3"))
    db.replace_records([{"id": str(i), "time": f"2026-11-12 00:00:{i:02}"} for i in range(12, 0, -1)])
    save_json_file(str(data / "selfie_image_config.json"), {"image": {"record_keep_limit": 10}})
    monkeypatch.setattr(main, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = SelfieImagePlugin(SimpleNamespace(), {"web": {"enable": False, "token": "test"}})
    assert plugin.config.image_record_keep_limit == 10
    assert len(plugin._records) == 10
    assert len(db.load_records(100)) == 10


@pytest.mark.parametrize("change", [
    {"requested_count": 3}, {"reference_image_count": 2}, {"use_selfie_reference": True},
    {"template_values": {"outfit": "red"}}, {"template_randomize": True},
    {"template_pools": {"outfit": ["red"]}}, {"template_seed": 3},
    {"variation_enabled": True}, {"variation_fields": ["pose"]},
    {"requested_model": "other"}, {"source": "other"}, {"owner_user_id": "other"},
    {"retry_record_id": "record-1"}, {"special_preset_variants": ["other"]},
    {"owner_origin": "other-platform:group:1"},
])
def test_dedupe_includes_request_semantics(change):
    plugin = WebTaskMixin()
    request = {"original_prompt": "portrait", "requested_count": 1, "reference_image_count": 1}
    assert plugin._request_fingerprint(request, "group:1") != plugin._request_fingerprint({**request, **change}, "group:1")


def test_reference_bytes_participate_without_url_or_object_identity():
    plugin = WebTaskMixin()
    payload = {"original_prompt": "portrait", "reference_image_count": 1}
    first = ImageReference(b"image-a", source_url="https://example.test/a")
    equivalent = ImageReference(b"image-a", source_url="https://example.test/b")
    second = ImageReference(b"image-b")
    key = plugin._request_fingerprint(payload, "group:1", [first])
    assert key == plugin._request_fingerprint(payload, "group:1", [equivalent])
    assert key != plugin._request_fingerprint(payload, "group:1", [second])
    assert "image-a" not in key


def test_command_dedupe_distinguishes_count_media_and_owner(tmp_path):
    async def run():
        plugin = runtime_plugin(tmp_path)
        plugin.loop = asyncio.get_running_loop()
        event = SimpleNamespace(group_id="g", user_id="u")
        release = asyncio.Event()

        async def runner(_task_id):
            await release.wait()
            return {"success": True, "requested_count": 1}

        def submit(count=1, data=b"a", user=event):
            return plugin.start_command_image_task(
                user, source="test", summary={"original_prompt": "portrait", "requested_count": count},
                runner=runner, reference_images=[ImageReference(data)],
            )

        first = submit()
        assert submit()["task_id"] == first["task_id"]
        assert submit(3)["task_id"] != first["task_id"]
        assert submit(data=b"b")["task_id"] != first["task_id"]
        assert submit(user=SimpleNamespace(group_id="g", user_id="other"))["task_id"] != first["task_id"]
        assert submit(user=SimpleNamespace(group_id="g", user_id="u", unified_msg_origin="other-platform:g"))["task_id"] != first["task_id"]
        assert "image" not in plugin._web_tasks[first["task_id"]]["request_data"]
        await plugin.terminate()
        assert all(row["status"] == "cancelled" for row in plugin._web_tasks.values())

    asyncio.run(run())


def test_selfie_reference_fingerprint_tracks_identity_auxiliary_and_selection(tmp_path):
    plugin = runtime_plugin(tmp_path)
    plugin._is_cos_action = lambda *_: False
    media = {"identity": b"identity-a", "auxiliary": b"aux-a"}
    plugin._persona_identity_reference = lambda **_: ImageReference(media["identity"])
    plugin._persona_auxiliary_references = lambda *_: [ImageReference(media["auxiliary"])]
    payload = {"original_prompt": "portrait"}

    def fingerprint():
        refs = plugin._selfie_reference_images("portrait", [])
        return plugin._request_fingerprint(payload, "g", refs)

    first = fingerprint()
    media["identity"] = b"identity-b"
    assert fingerprint() != first
    second = fingerprint()
    media["auxiliary"] = b"aux-b"
    assert fingerprint() != second
    selection = SimpleNamespace(refs=[ImageReference(b"selected")])
    assert [ref.data for ref in plugin._selfie_reference_images("portrait", [], selection)] == [b"selected"]


def test_web_selfie_dedupe_changes_when_persona_media_changes(tmp_path):
    async def run():
        plugin = runtime_plugin(tmp_path)
        plugin.loop = asyncio.get_running_loop()
        plugin._validate_web_test_selection = lambda *_: None
        media = {"identity": b"identity-a", "auxiliary": b"aux-a"}
        plugin._persona_identity_reference = lambda **_: ImageReference(media["identity"])
        plugin._persona_auxiliary_references = lambda *_: [ImageReference(media["auxiliary"])]

        async def worker(*_):
            await asyncio.Event().wait()

        plugin._run_web_image_task = worker
        payload = {"prompt": "portrait", "use_selfie_reference": True}
        first = plugin.start_web_image_task(payload)
        assert plugin.start_web_image_task(payload)["task_id"] == first["task_id"]
        media["identity"] = b"identity-b"
        second = plugin.start_web_image_task(payload)
        assert second["task_id"] != first["task_id"]
        media["auxiliary"] = b"aux-b"
        assert plugin.start_web_image_task(payload)["task_id"] != second["task_id"]
        await plugin.terminate()

    asyncio.run(run())


def test_web_dedupe_uses_actual_count_instead_of_stale_summary_count(tmp_path):
    async def run():
        plugin = runtime_plugin(tmp_path)
        plugin.loop = asyncio.get_running_loop()
        plugin._validate_web_test_selection = lambda *_: None

        async def worker(*_):
            await asyncio.Event().wait()

        plugin._run_web_image_task = worker
        first = plugin.start_web_image_task({"prompt": "portrait", "count": 2, "requested_count": 1})
        second = plugin.start_web_image_task({"prompt": "portrait", "count": 3, "requested_count": 1})
        assert first["task_id"] != second["task_id"]
        assert second["requested_count"] == 3
        assert plugin.start_web_image_task({"prompt": "portrait", "count": 2})["task_id"] == first["task_id"]
        await plugin.terminate()

    asyncio.run(run())


def test_shutdown_stops_preflight_and_rejects_new_submissions(tmp_path):
    async def run():
        plugin = runtime_plugin(tmp_path)
        plugin.loop = asyncio.get_running_loop()
        started = asyncio.Event()
        gate = asyncio.Event()
        cleaned = asyncio.Event()
        calls = []

        async def worker():
            started.set()
            try:
                await gate.wait()
                await plugin._image_scheduler.submit("task", lambda: asyncio.sleep(0))
                calls.append("provider")
            finally:
                await asyncio.sleep(0.01)
                cleaned.set()

        plugin._web_tasks["task"] = {"task_id": "task", "status": "running"}
        handle = plugin._start_runtime_generation("task", worker())
        await started.wait()
        await plugin.terminate()
        gate.set()
        assert cleaned.is_set()
        assert handle.done()
        assert calls == []
        assert plugin._web_tasks["task"]["status"] == "cancelled"
        with pytest.raises(RuntimeError, match="closed"):
            await plugin._image_scheduler.submit("late", lambda: asyncio.sleep(0))
        assert load_json_file(plugin.tasks_path)["tasks"]["task"]["status"] == "cancelled"

    asyncio.run(run())


def test_shutdown_waits_for_web_proxy_coroutine_cleanup(tmp_path):
    async def run():
        plugin = runtime_plugin(tmp_path)
        plugin.loop = asyncio.get_running_loop()
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def worker():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.03)
                cleaned.set()

        proxy = await asyncio.to_thread(plugin._start_runtime_generation, "web-task", worker())
        await started.wait()
        await plugin.terminate()
        assert proxy.cancelled()
        assert cleaned.is_set()
        assert not plugin._runtime_generation_workers

    asyncio.run(run())


@pytest.mark.parametrize("commit_kind", ["usage", "record"])
def test_shutdown_waits_for_background_commits_without_task_record_link(tmp_path, commit_kind):
    async def run():
        plugin = runtime_plugin(tmp_path)
        plugin.loop = asyncio.get_running_loop()
        started = threading.Event()
        release = threading.Event()
        committed = []

        def commit(*_):
            started.set()
            assert release.wait(2)
            committed.append(commit_kind)

        if commit_kind == "usage":
            plugin._record_generated_images = commit
            plugin._start_runtime_generation("usage", plugin._record_generated_images_async(None, 1))
        else:
            plugin._commit_generation_records = commit
            plugin._record_task({"id": "unlinked-record", "success": False})
        assert await asyncio.to_thread(started.wait, 1)
        shutdown = asyncio.create_task(plugin.terminate())
        await asyncio.sleep(0.02)
        assert not shutdown.done()
        release.set()
        await asyncio.wait_for(shutdown, 1)
        assert committed == [commit_kind]
        assert not getattr(plugin, "_record_commit_tasks", set())
        assert not getattr(plugin, "_usage_commit_tasks", set())

    asyncio.run(run())


def test_reference_session_is_reused_without_cookies_and_closed_on_shutdown(tmp_path):
    async def run():
        plugin = runtime_plugin(tmp_path)
        plugin.loop = asyncio.get_running_loop()
        async with plugin._reference_client_session() as first:
            assert isinstance(first.cookie_jar, aiohttp.DummyCookieJar)
        assert not first.closed
        async with plugin._reference_client_session() as second:
            assert second is first
        await plugin.terminate()
        assert first.closed
        with pytest.raises(asyncio.CancelledError):
            async with plugin._reference_client_session():
                pytest.fail("shutdown must reject new reference fetches")

    asyncio.run(run())


def test_reference_session_outside_plugin_loop_is_short_lived(tmp_path):
    async def run():
        plugin = runtime_plugin(tmp_path)
        async with plugin._reference_client_session() as session:
            assert not session.closed
        assert session.closed
        assert getattr(plugin, "_reference_http_session", None) is None

    asyncio.run(run())


def test_flask_async_timeout_cancels_the_submitted_coroutine(tmp_path):
    from astrbot_plugin_selfie_image.webui.web import FlaskWebServer

    async def run():
        plugin = runtime_plugin(tmp_path)
        plugin.loop = asyncio.get_running_loop()
        server = FlaskWebServer(plugin)
        cleaned = asyncio.Event()

        async def worker():
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        with pytest.raises(TimeoutError):
            await asyncio.to_thread(server._run_async, worker(), 0.02)
        await asyncio.wait_for(cleaned.wait(), 1)
        await plugin.terminate()

    asyncio.run(run())


def test_cancel_from_flask_thread_uses_debug_loop_safely(tmp_path):
    async def run():
        plugin = runtime_plugin(tmp_path)
        plugin.loop = asyncio.get_running_loop()
        plugin.loop.set_debug(True)
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def transport():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        plugin._web_tasks["task"] = {"task_id": "task", "status": "running", "owner_session": "web"}
        plugin._runtime_generation_tasks["task"] = object()
        submitter = asyncio.create_task(plugin._image_scheduler.submit("task", transport))
        await started.wait()
        await asyncio.to_thread(plugin.cancel_image_task, "task", is_admin=True)
        await asyncio.wait_for(cleaned.wait(), 1)
        with pytest.raises(asyncio.CancelledError):
            await submitter
        plugin._runtime_generation_tasks.clear()
        await plugin.terminate()

    asyncio.run(run())


def test_scheduler_limit_can_be_changed_from_flask_thread():
    async def run():
        loop = asyncio.get_running_loop()
        loop.set_debug(True)
        scheduler = ImageJobScheduler(1)
        started = asyncio.Event()

        async def transport():
            started.set()
            await asyncio.Event().wait()

        submitter = asyncio.create_task(scheduler.submit("a", transport))
        await started.wait()
        await asyncio.to_thread(scheduler.set_limit, 3)
        await asyncio.sleep(0)
        assert (await scheduler.snapshot())["limit"] == 3
        await scheduler.aclose()
        assert submitter.cancelled()

    asyncio.run(run())


def test_scheduler_close_waits_for_transport_cleanup_and_empty_queue():
    async def run():
        scheduler = ImageJobScheduler(1)
        started = asyncio.Event()
        cleanup = asyncio.Event()

        async def transport():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.02)
                cleanup.set()

        first = asyncio.create_task(scheduler.submit("a", transport))
        await started.wait()
        queued = asyncio.create_task(scheduler.submit("b", lambda: asyncio.sleep(0)))
        await asyncio.sleep(0)
        await scheduler.aclose()
        await asyncio.gather(first, queued, return_exceptions=True)
        assert cleanup.is_set()
        assert await scheduler.snapshot() == {"limit": 1, "active": 0, "pending": 0, "queued_tasks": 0}

    asyncio.run(run())


def test_scheduler_close_before_worker_first_step_releases_slot():
    async def run():
        scheduler = ImageJobScheduler(1)
        calls = []

        async def transport():
            calls.append("started")

        submitter = asyncio.create_task(scheduler.submit("a", transport))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert scheduler._active == 1
        assert calls == []
        async with asyncio.timeout(1):
            await scheduler.aclose()
        assert submitter.cancelled()
        assert calls == []
        assert not scheduler._running
        assert (await scheduler.snapshot())["active"] == 0

    asyncio.run(run())


def test_progress_snapshot_coalesces_and_terminal_flushes(tmp_path, monkeypatch):
    from astrbot_plugin_selfie_image.tasks import task_manager

    plugin = runtime_plugin(tmp_path)
    plugin.WEB_TASK_SNAPSHOT_DELAY = 0.04
    plugin._web_tasks["task"] = {"task_id": "task", "status": "running"}
    writes = []
    persisted = threading.Event()
    original = task_manager.save_json_file_compact

    def save(path, data):
        original(path, data)
        writes.append(data["tasks"]["task"]["completed_count"])
        persisted.set()

    monkeypatch.setattr(task_manager, "save_json_file_compact", save)
    for count in range(20):
        plugin._set_web_image_task("task", completed_count=count)
    assert persisted.wait(1)
    assert writes == [19]
    plugin._set_web_image_task("task", completed_count=20)
    plugin._set_web_image_task("task", status="succeeded", completed_count=21)
    assert writes == [19, 21]
    assert load_json_file(plugin.tasks_path)["tasks"]["task"]["status"] == "succeeded"
    assert plugin._web_task_snapshot_timer is None


def test_asset_query_decodes_only_requested_page(tmp_path, monkeypatch):
    db = RecordDatabase(str(tmp_path / "records.sqlite3"))
    db.replace_records([{
        "id": str(i), "time": f"2026-11-12 00:00:{i:02}", "source": "group",
        "original_prompt": "portrait", "used_model": "model-a", "success": True,
    } for i in range(50, 0, -1)])
    decoded = []
    original = db._decode_rows

    def decode(rows):
        decoded.append(len(list(rows)))
        return original(rows)

    monkeypatch.setattr(db, "_decode_rows", decode)
    rows, meta = db.query_records(source="GROUP", keyword="portrait", offset=10, limit=3)
    assert len(rows) == 3
    assert decoded == [3]
    assert meta == {"total": 50, "filtered": 50, "offset": 10, "limit": 3}


@pytest.mark.parametrize("filters", [
    {}, {"favorite": True}, {"favorite": False}, {"pinned": True}, {"pinned": False},
    {"tag": "TAG"}, {"source": "GROUP"}, {"model": "model-a"},
    {"media_type": "image"}, {"media_type": "video"}, {"success": False},
    {"keyword": "portrait"}, {"keyword": "tail-match"}, {"keyword": "provider-only"},
    {"source": "\u0130"}, {"start_time": "2026-11-12"}, {"end_time": "2026-11-12"},
    {"sort": "priority"}, {"sort": "oldest"}, {"offset": 1, "limit": 1},
])
def test_sql_asset_query_matches_fallback_filters_and_pagination(tmp_path, monkeypatch, filters):
    store = StorageFixture()._plugin(str(tmp_path))
    store.config.image_record_keep_limit = 2
    db = store._record_database()
    db.replace_records([
        {"id": "new", "time": "2026-11-13", "source": "group-a", "success": True,
         "used_model": "model-a", "original_prompt": "portrait " + "x" * 300 + " tail-match"},
        {"id": "ordinary", "time": "2026-11-12", "source": "\u0130", "success": False,
         "media_type": "video", "tags": ["tag"], "prompt": "provider-only"},
        {"id": "pruned", "time": "2026-11-11"},
        {"id": "favorite", "time": "2026-11-10", "favorite": True, "source": "group-b", "tags": ["TAG"]},
        {"id": "pinned", "time": "2026-11-09", "pinned": True, "note": "portrait"},
    ])
    store._records = db.load_records(2)
    sql_rows, sql_meta = store.query_asset_records(**filters)
    monkeypatch.setattr(store, "_record_database", lambda: None)
    fallback_rows, fallback_meta = store.query_asset_records(**filters)
    assert sql_meta == fallback_meta
    assert [row["id"] for row in sql_rows] == [row["id"] for row in fallback_rows]
    assert sql_rows == fallback_rows


@pytest.mark.parametrize("compressed", [False, True])
def test_zip_expansion_is_bounded(compressed):
    archive = BytesIO()
    with ZipFile(archive, "w", ZIP_DEFLATED) as zf:
        zf.writestr("image.png", b"\x89PNG\r\n\x1a\n" + b"a" * 2_000_000)
    raw = archive.getvalue()
    assert len(raw) < 4096
    if compressed:
        raw = brotli.compress(raw)
    with pytest.raises(ValueError, match="4096"):
        _parse_nai_response_body(raw, "utf-8", 4096)


@pytest.mark.parametrize("wire_limit,image_limit", [(1024, 4096), (4096, 1024)])
def test_nai_gateway_rejects_oversized_wire_or_image(wire_limit, image_limit):
    from astrbot_plugin_selfie_image.tests.test_novelai_native import Session

    target = ImageModelTarget("test", "novelai", "https://example.test/generate", "dummy", "nai", 30)
    target.extra["max_response_bytes"] = wire_limit
    session = Session(b"\x89PNG\r\n\x1a\n" + b"a" * 2048)
    req = ImageGenerateRequest(prompt="portrait", max_image_bytes=image_limit)
    result = asyncio.run(NovelAIImageAdapter(target, session).generate(req))
    assert result.images == []
    assert "1024" in result.error


@pytest.mark.parametrize("mode", ["official", "gateway"])
@pytest.mark.parametrize("size", [1024, 1025])
def test_nai_zip_respects_image_limit_even_when_wire_limit_is_larger(mode, size):
    from astrbot_plugin_selfie_image.tests.test_novelai_native import Session

    base_url = "https://example.test/ai/generate-image" if mode == "official" else "https://example.test/generate"
    target = ImageModelTarget("test", "novelai", base_url, "dummy", "nai", 30)
    target.extra["max_response_bytes"] = 4096
    image = b"\x89PNG\r\n\x1a\n" + b"a" * (size - 8)
    archive = BytesIO()
    with ZipFile(archive, "w", ZIP_DEFLATED) as zf:
        zf.writestr("image.png", image)
    adapter = NovelAIImageAdapter(target, Session(archive.getvalue()))
    assert adapter._mode() == mode
    result = asyncio.run(adapter.generate(
        ImageGenerateRequest(prompt="portrait", max_image_bytes=1024),
    ))
    if size == 1024:
        assert result.images == [image]
        assert not result.error
    else:
        assert result.images == []
        assert "1024" in result.error


def test_nai_gateway_stops_reading_stream_after_response_limit():
    from astrbot_plugin_selfie_image.tests.test_novelai_native import Session

    target = ImageModelTarget("test", "novelai", "https://example.test/generate", "dummy", "nai", 30)
    target.extra["max_response_bytes"] = 1024
    session = Session(b"")
    reads = []

    async def chunks(_):
        for i in range(10):
            reads.append(i)
            yield b"a" * 512

    session.response.content = SimpleNamespace(iter_chunked=chunks)
    result = asyncio.run(NovelAIImageAdapter(target, session).generate(ImageGenerateRequest(prompt="portrait")))
    assert "1024" in result.error
    assert reads == [0, 1, 2]


def test_usage_count_updates_are_atomic_across_worker_threads(tmp_path):
    async def run():
        plugin = runtime_plugin(tmp_path)
        plugin._usage_lock = threading.RLock()
        plugin._usage_stats = {"date": plugin._today_key(), "users": {}}
        plugin.usage_path = str(tmp_path / "usage.json")
        event = SimpleNamespace(user_id="u", group_id="g")
        await asyncio.gather(*(asyncio.to_thread(plugin._record_generated_images, event, 1) for _ in range(30)))
        assert plugin._usage_stats["users"]["u"]["count"] == 30
        assert load_json_file(plugin.usage_path)["users"]["u"]["count"] == 30

    asyncio.run(run())
