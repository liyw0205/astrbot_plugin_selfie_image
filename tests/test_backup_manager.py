import time
from pathlib import Path
import zipfile

import pytest

from astrbot_plugin_selfie_image.features.backup_manager import (
    BackupManager,
    BackupManagerError,
    MAX_BACKUPS,
)


def _wait(manager: BackupManager, task_id: str) -> dict:
    for _ in range(500):
        task = manager.task(task_id)
        if task["status"] in {"completed", "failed"}:
            return task
        time.sleep(0.005)
    raise AssertionError("backup task did not finish")


def _root(tmp_path: Path) -> Path:
    (tmp_path / "selfie_image_config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "wardrobe").mkdir()
    (tmp_path / "wardrobe" / "metadata.json").write_text('{"items": []}', encoding="utf-8")
    return tmp_path


def test_selectable_backup_has_required_wardrobe_and_progress(tmp_path):
    manager = BackupManager(_root(tmp_path))
    with pytest.raises(BackupManagerError, match="衣橱"):
        manager.start_backup(["config"])

    task = manager.start_backup(["config", "wardrobe"])
    finished = _wait(manager, task["task_id"])
    assert finished["status"] == "completed"
    assert finished["percent"] == 100
    listing = manager.list_backups()
    assert listing["count"] == 1
    assert listing["items"][0]["selected"] == ["config", "wardrobe"]


def test_backup_limit_is_enforced_before_background_work(tmp_path):
    manager = BackupManager(_root(tmp_path))
    for _ in range(MAX_BACKUPS):
        task = manager.start_backup(["wardrobe"])
        assert _wait(manager, task["task_id"])["status"] == "completed"
    with pytest.raises(BackupManagerError, match="最多保留"):
        manager.start_backup(["wardrobe"])


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
    task = manager.start_backup(["wardrobe"])
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
