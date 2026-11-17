"""Restore coordination against real plugin stores, without an AstrBot host."""

import asyncio
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from astrbot_plugin_selfie_image import main
from astrbot_plugin_selfie_image.features.backup_manager import BackupManager
from astrbot_plugin_selfie_image.generation.record_database import RecordDatabase
from astrbot_plugin_selfie_image.tests.test_backup_manager import _root, _wait
from astrbot_plugin_selfie_image.tests.test_repository_fixes import runtime_plugin


@pytest.mark.parametrize("busy", ["task", "worker", "commit", "scheduler"])
def test_restore_refuses_active_generation_or_pending_writes(tmp_path, busy):
    plugin = runtime_plugin(tmp_path)
    manager = BackupManager(_root(tmp_path), defer_restore=True)
    plugin.backup_manager = manager
    try:
        task = manager.start_backup(["config"])
        archive = _wait(manager, task["task_id"])["result"]
        if busy == "task":
            plugin._web_tasks["active"] = {"status": "running"}
        elif busy == "worker":
            plugin._runtime_generation_tasks["active"] = Future()
        elif busy == "commit":
            plugin._record_commit_tasks = {Future()}
        else:
            plugin._image_scheduler_snapshot = lambda: {"active": 1, "pending": 0}
        with pytest.raises(ValueError, match="等待"):
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
