"""Restore coordination against real plugin stores, without an AstrBot host."""

import asyncio
import base64
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from astrbot_plugin_selfie_image import main
from astrbot_plugin_selfie_image.features.backup_manager import BackupManager
from astrbot_plugin_selfie_image.generation.record_database import RecordDatabase
from astrbot_plugin_selfie_image.tests.test_backup_manager import _root, _wait
from astrbot_plugin_selfie_image.tests.test_repository_fixes import runtime_plugin


@pytest.mark.parametrize("busy", [
    "task", "queued_task", "worker", "handle", "commit", "usage_commit", "pending_record",
    "pending_image_record", "scheduler", "queued_scheduler", "terminating",
])
def test_restore_refuses_active_generation_or_pending_writes(tmp_path, busy):
    plugin = runtime_plugin(tmp_path)
    manager = BackupManager(_root(tmp_path), defer_restore=True)
    plugin.backup_manager = manager
    try:
        task = manager.start_backup(["config"])
        archive = _wait(manager, task["task_id"])["result"]
        if busy in {"task", "queued_task"}:
            plugin._web_tasks["active"] = {"status": "queued" if busy == "queued_task" else "running"}
        elif busy == "worker":
            plugin._runtime_generation_workers["active"] = Future()
        elif busy == "handle":
            plugin._runtime_generation_tasks["active"] = Future()
        elif busy == "commit":
            plugin._record_commit_tasks = {Future()}
        elif busy == "usage_commit":
            plugin._usage_commit_tasks = {Future()}
        elif busy == "pending_record":
            plugin._pending_record_commits = {"task": {Future()}}
        elif busy == "pending_image_record":
            plugin._pending_image_record_commits = {"image": {Future()}}
        elif busy == "scheduler":
            plugin._image_scheduler_snapshot = lambda: {"active": 1, "pending": 0}
        elif busy == "queued_scheduler":
            plugin._image_scheduler_snapshot = lambda: {"active": 0, "pending": 1}
        else:
            plugin._terminating = True
        with pytest.raises(ValueError, match="卸载" if busy == "terminating" else "等待"):
            plugin.restore_backup_from_web(archive["id"])
        assert not manager.restore_pending
    finally:
        manager.close()


def test_prepared_restore_blocks_web_command_and_direct_generation(tmp_path):
    plugin = runtime_plugin(tmp_path)
    manager = BackupManager(_root(tmp_path), defer_restore=True)
    plugin.backup_manager = manager
    try:
        task = manager.start_backup(["config"])
        archive = _wait(manager, task["task_id"])["result"]
        request = plugin.restore_backup_from_web(archive["id"])
        assert _wait(manager, request["task_id"])["status"] == "completed"
        with pytest.raises(RuntimeError, match="重启"):
            plugin.start_web_image_task({})
        with pytest.raises(RuntimeError, match="重启"):
            plugin.start_command_image_task(SimpleNamespace(user_id="user", group_id="group"), source="test", summary={}, runner=None)
        with pytest.raises(RuntimeError, match="重启"):
            asyncio.run(plugin._run_image_generation("test", "1:1", "1K", []))
        assert plugin._web_tasks == {}
        assert plugin.backup_task_from_web(request["task_id"])["result"]["restart_required"] is True
    finally:
        manager.close()


def test_restart_restores_before_loading_configuration_and_sqlite(tmp_path, monkeypatch):
    data = tmp_path / "plugin_data" / "astrbot_plugin_selfie_image"
    data.mkdir(parents=True)
    _root(data)
    config = data / "selfie_image_config.json"
    config.write_text('{"image":{"record_keep_limit":17}}', encoding="utf-8")
    database = RecordDatabase(str(data / "generation_records.sqlite3"))
    database.upsert_record({"id": "backup-record", "success": True}, sequence=1)
    manager = BackupManager(data, defer_restore=True)
    try:
        task = manager.start_backup(["config", "records"])
        archive = _wait(manager, task["task_id"])["result"]
        config.write_text('{"image":{"record_keep_limit":10}}', encoding="utf-8")
        database.replace_records([{"id": "live-record"}])
        request = manager.start_restore(archive["id"])
        assert _wait(manager, request["task_id"])["status"] == "completed"
    finally:
        manager.close()
    monkeypatch.setattr(main, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main.SelfieImagePlugin(SimpleNamespace(), {"web": {"enable": False}})
    try:
        assert plugin.config.image_record_keep_limit == 17
        assert [record["id"] for record in plugin._records] == ["backup-record"]
        assert not plugin.backup_manager.restore_pending
    finally:
        asyncio.run(plugin.terminate())


def test_plugin_termination_closes_backup_workers_even_if_runtime_shutdown_fails(tmp_path):
    plugin = runtime_plugin(tmp_path)
    plugin.backup_manager = BackupManager(_root(tmp_path))
    task = plugin.backup_manager.start_backup(["config"])
    assert _wait(plugin.backup_manager, task["task_id"])["status"] == "completed"

    def broken_stop():
        raise OSError("injected server shutdown failure")

    plugin.web_server.stop = broken_stop
    with pytest.raises(OSError, match="shutdown failure"):
        asyncio.run(plugin.terminate())
    assert plugin.backup_manager._closed
    assert not any(worker.is_alive() for worker in plugin.backup_manager._executor._threads)


@pytest.mark.parametrize("chunked", [False, True])
def test_backup_service_uses_reloaded_stores_for_wardrobe_import(tmp_path, chunked):
    from astrbot_plugin_selfie_image.features.persona import PersonaManager
    from astrbot_plugin_selfie_image.tests.test_wardrobe import PNG_1X1, PNG_1X1_ALT
    import zlib

    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    old_manager = BackupManager(_root(old_root))
    new_manager = BackupManager(_root(new_root))
    plugin = runtime_plugin(tmp_path)
    plugin.backup_manager = old_manager
    plugin.persona = PersonaManager(old_root)
    original_persona = plugin.persona
    original = original_persona.add_wardrobe_item(PNG_1X1, name="备份衣服")
    try:
        exported = plugin.export_wardrobe_from_web()
        service = plugin._backup_web_service
        old_manager.close()
        plugin.backup_manager = new_manager
        plugin.persona = PersonaManager(new_root)
        replaced = plugin.persona.add_wardrobe_item(PNG_1X1_ALT, name="当前衣服")
        if chunked:
            data = base64.b64decode(exported["content_base64"])
            upload = plugin.start_archive_upload_from_web({"kind": "wardrobe", "size": len(data)})
            plugin.append_archive_upload_from_web(upload["id"], {
                "offset": 0, "content_base64": exported["content_base64"], "crc32": f"{zlib.crc32(data):08x}",
            })
            result = plugin.commit_archive_upload_from_web(upload["id"])
        else:
            result = plugin.import_wardrobe_from_web(exported)
        assert plugin._backup_web_service is service
        assert result["imported"] is True
        assert [item["id"] for item in result["items"]] == [original.id]
        assert original_persona.wardrobe.get(original.id).name == "备份衣服"
        assert plugin.persona.wardrobe.get(replaced.id) is None
        assert plugin.persona.wardrobe.read(original.id)[0] == PNG_1X1
        assert not list(new_manager.transfers.root.iterdir())
    finally:
        old_manager.close()
        new_manager.close()


def test_legacy_host_can_restore_without_service_cache_attribute(tmp_path):
    manager = BackupManager(_root(tmp_path), defer_restore=True)

    class Host:
        __slots__ = ("backup_manager", "_web_task_lock", "checked")
        create_backup_from_web = main.SelfieImagePlugin.create_backup_from_web
        restore_backup_from_web = main.SelfieImagePlugin.restore_backup_from_web
        _decode_web_archive = staticmethod(main.SelfieImagePlugin._decode_web_archive)

        def _assert_backup_restore_idle(self):
            assert self._web_task_lock._is_owned()
            self.checked += 1

    import threading
    host = Host()
    host.backup_manager = manager
    host._web_task_lock = threading.RLock()
    host.checked = 0
    try:
        created = host.create_backup_from_web({"items": ["config"]})
        backup = _wait(manager, created["task_id"])["result"]
        request = host.restore_backup_from_web(backup["id"])
        assert _wait(manager, request["task_id"])["result"]["restart_required"] is True
        assert host.checked == 1
        assert manager.restore_pending
        assert not hasattr(host, "_backup_web_service")
    finally:
        manager.close()
