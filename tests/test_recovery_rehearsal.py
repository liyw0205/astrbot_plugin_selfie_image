"""End-to-end backup/restore rehearsal across the plugin's durable stores."""

from __future__ import annotations

import base64
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_selfie_image.generation.asset_collections import AssetCollectionStore
from astrbot_plugin_selfie_image.generation.generation_store import GenerationStoreMixin
from astrbot_plugin_selfie_image.generation.storage_consistency import inspect_storage_consistency
from astrbot_plugin_selfie_image.scripts import backup_restore as recovery
from astrbot_plugin_selfie_image.studio.studio import StudioStore
from astrbot_plugin_selfie_image.tasks.task_manager import WebTaskMixin


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _store(root: Path) -> GenerationStoreMixin:
    """Build the same storage wiring used by the production plugin startup."""
    root.mkdir(parents=True, exist_ok=True)
    store = GenerationStoreMixin()
    store.data_dir = str(root)
    store.records_path = str(root / "generation_records.json")
    store.records_db_path = str(root / "generation_records.sqlite3")
    store.media_sources_dir = str(root / "media_sources")
    store.generated_dir = str(root / "image_cache")
    store.tasks_path = str(root / "generation_tasks.json")
    store.config = SimpleNamespace(
        image_cache_limit_mb=10,
        image_cache_limit_count=100,
        image_record_keep_limit=100,
    )
    store._records_lock = threading.RLock()
    store._records = []
    store._record_seq = 0
    store._web_task_lock = threading.RLock()
    store._web_tasks = {}
    for directory in (store.media_sources_dir, store.generated_dir):
        Path(directory).mkdir(parents=True, exist_ok=True)
    return store


def _write_task_snapshot(path: Path, media_path: str) -> None:
    path.write_text(
        json.dumps(
            {
                "tasks": {
                    "task-1": {
                        "task_id": "task-1",
                        "status": "succeeded",
                        "success": True,
                        "result": {"files": [media_path], "record_ids": ["record-1"]},
                    }
                },
                "snapshot": {"snapshot_version": 7, "write_count": 1},
            }
        ),
        encoding="utf-8",
    )


def test_real_data_root_can_be_rehearsed_and_written_after_restore(tmp_path: Path) -> None:
    source = tmp_path / "source"
    store = _store(source)
    generated = Path(store.generated_dir)
    generated.joinpath("record-1.png").write_bytes(PNG_1X1)
    generated.joinpath("canvas-relationship.png").write_bytes(PNG_1X1)
    generated.joinpath("canvas-creative.png").write_bytes(PNG_1X1)

    store._commit_generation_record(
        {
            "id": "record-1",
            "task_id": "task-1",
            "success": True,
            "generated_image_paths": ["record-1.png"],
            "generated_image_sources": [{"type": "cache_path", "value": "record-1.png"}],
        }
    )
    relationship_store = StudioStore(str(source))
    relationship = relationship_store.create("关系画布")
    relationship_store.attach_run_finish(
        relationship["id"], "task-1", success=True, result_paths=["canvas-relationship.png"]
    )
    creative_store = StudioStore(str(source / "creative_canvas"), canvas_mode="creative")
    creative = creative_store.create("创作画布")
    creative_store.attach_run_finish(
        creative["id"], "task-creative", success=True, result_paths=["canvas-creative.png"]
    )
    collection = AssetCollectionStore(str(source)).create("保留资产", record_ids=["record-1"])
    _write_task_snapshot(Path(store.tasks_path), "record-1.png")

    persona_path = source / "image-persona" / "persona.png"
    persona_path.parent.mkdir()
    persona_path.write_bytes(PNG_1X1)
    (source / "image_persona.json").write_text(
        json.dumps({"ref_image_path": str(persona_path)}), encoding="utf-8"
    )
    (source / "selfie_image_config.json").write_text(
        json.dumps({"image": {"record_keep_limit": 100}}), encoding="utf-8"
    )

    pending_path = store._save_cache_image(PNG_1X1, "pending-request", "image/png")
    assert pending_path in store._inflight_media_paths()

    backup = tmp_path / "backup"
    manifest = recovery.create_backup(source, backup, stopped=True)
    assert recovery.verify_backup(backup) == manifest

    # The old absolute persona path is deliberately not rewritten by the tool.
    source.rename(tmp_path / "source-retired")
    restored = tmp_path / "restored"
    recovery.restore_backup(backup, restored, stopped=True)

    revived = _store(restored)
    revived._records = revived._load_records()
    revived._record_seq = revived._restore_record_sequence()
    assert [row["id"] for row in revived._records] == ["record-1"]
    detail = revived.get_record_for_web("record-1")
    assert detail["generated_image_sources"][0]["type"] == "base64"
    assert detail["generated_image_sources"][0]["value"].startswith("data:image/")

    revived.studio = StudioStore(str(restored))
    revived.creative_canvas = StudioStore(str(restored / "creative_canvas"), canvas_mode="creative")
    revived.asset_collections = AssetCollectionStore(str(restored))
    report = revived.inspect_storage_consistency()
    assert report["ok"] is True, report
    assert revived.studio.get(relationship["id"])["last_run"]["result_paths"] == ["canvas-relationship.png"]
    assert revived.creative_canvas.get(creative["id"])["last_run"]["result_paths"] == ["canvas-creative.png"]
    assert revived.asset_collections.get(collection["id"])["record_ids"] == ["record-1"]
    assert pending_path in revived._inflight_media_paths()

    task_reader = WebTaskMixin()
    task_reader.tasks_path = str(restored / "generation_tasks.json")
    tasks = task_reader._load_web_tasks()
    assert tasks["task-1"]["status"] == "succeeded"
    assert tasks["task-1"]["result"]["files"] == ["record-1.png"]

    persona_ref = json.loads((restored / "image_persona.json").read_text(encoding="utf-8"))["ref_image_path"]
    assert persona_ref == str(persona_path)
    assert not persona_ref.startswith(str(restored))
    assert not os.path.exists(persona_ref)

    revived._commit_generation_record({"id": "record-after-restore", "success": True})
    reloaded = _store(restored)
    reloaded._records = reloaded._load_records()
    assert {row["id"] for row in reloaded._records} == {"record-1", "record-after-restore"}


def test_consistency_checks_sidecar_cache_path_and_record_identity(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    sidecars = tmp_path / "sidecars"
    cache.mkdir()
    sidecars.mkdir()
    (sidecars / "record-1.json").write_text(
        json.dumps(
            {
                "record_id": "other-record",
                "generated_image_sources": [{"type": "cache_path", "value": "missing.png"}],
            }
        ),
        encoding="utf-8",
    )
    report = inspect_storage_consistency(
        [{"id": "record-1", "media_sources_file": "record-1.json"}],
        cache_root=str(cache),
        sidecar_root=str(sidecars),
        protected_paths=["canvas-missing.png"],
    )
    kinds = {item["kind"] for item in report["issues"]}
    assert "sidecar_record_mismatch" in kinds
    assert "missing_cache" in kinds
    assert any(item.get("protected") for item in report["issues"] if item["kind"] == "missing_cache")


def test_consistency_rejects_sidecar_cache_path_escape(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    sidecars = tmp_path / "sidecars"
    cache.mkdir()
    sidecars.mkdir()
    (sidecars / "record-1.json").write_text(
        json.dumps(
            {
                "record_id": "record-1",
                "generated_image_sources": [{"type": "cache_path", "value": "../outside.png"}],
            }
        ),
        encoding="utf-8",
    )
    report = inspect_storage_consistency(
        [{"id": "record-1", "media_sources_file": "record-1.json"}],
        cache_root=str(cache),
        sidecar_root=str(sidecars),
    )
    assert any(item["kind"] == "invalid_path" for item in report["issues"])
