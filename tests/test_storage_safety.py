import json
import threading
from pathlib import Path

import pytest

from astrbot_plugin_selfie_image.core.models import AICatConfig, DEFAULT_CONFIG
from astrbot_plugin_selfie_image.core.persistent_store import PersistentStoreConflictError
from astrbot_plugin_selfie_image.core.utils import load_json_file_result
from astrbot_plugin_selfie_image.cos.cos_pool import CosPoolStore
from astrbot_plugin_selfie_image.features import config_manager
from astrbot_plugin_selfie_image.features.config_manager import ConfigurationMixin
from astrbot_plugin_selfie_image.features.daily_schedule import DailySchedule, DailyScheduleStore, ScheduleEvent
from astrbot_plugin_selfie_image.features.wardrobe import WardrobeStore
from astrbot_plugin_selfie_image.generation.asset_collections import AssetCollectionStore
from astrbot_plugin_selfie_image.prompts.preset import ImagePresetManager
from astrbot_plugin_selfie_image.tests.test_wardrobe import PNG_1X1


def _denied(*_args, **_kwargs):
    raise PermissionError("injected file sharing violation")


def test_json_loader_distinguishes_missing_corrupt_and_invalid_schema(tmp_path):
    missing = load_json_file_result(str(tmp_path / "missing.json"))
    assert missing.status == "missing" and missing.ok

    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_bytes(b'{"items": ')
    corrupt = load_json_file_result(str(corrupt_path))
    assert corrupt.status == "corrupt" and not corrupt.ok

    list_path = tmp_path / "list.json"
    list_path.write_text("[]", encoding="utf-8")
    invalid = load_json_file_result(str(list_path))
    assert invalid.status == "invalid_schema" and not invalid.ok


def test_corrupt_preset_file_is_not_seeded_or_overwritten(tmp_path):
    path = tmp_path / "image_presets.json"
    original = b'{"custom": '
    path.write_bytes(original)

    manager = ImagePresetManager(str(tmp_path))

    assert path.read_bytes() == original
    assert manager.get_load_status()["ok"] is False
    with pytest.raises(RuntimeError, match="停止覆盖"):
        manager.add("new", "new prompt")
    assert path.read_bytes() == original
    assert manager.has_preset("new") is False


def test_preset_store_reloads_external_changes_and_rejects_stale_writer(tmp_path):
    first = ImagePresetManager(str(tmp_path))
    stale = ImagePresetManager(str(tmp_path))
    stale_revision = stale.get_revision()

    first.add("writer-one", "first snapshot")
    with pytest.raises(PersistentStoreConflictError):
        stale.add("writer-two", "stale snapshot", expected_revision=stale_revision)

    assert not stale.has_preset("writer-two")
    stale.load()
    assert stale.has_preset("writer-one")
    assert not stale.has_preset("writer-two")
    assert stale.get_revision() == first.get_revision()


def test_cos_pool_reloads_external_changes_and_rejects_stale_writer(tmp_path):
    first = CosPoolStore(str(tmp_path))
    stale = CosPoolStore(str(tmp_path))
    stale_revision = stale.get_revision()

    first.save_custom({"id": "writer-one", "title": "first", "prompt": "first snapshot"})
    with pytest.raises(PersistentStoreConflictError):
        stale.save_custom(
            {"id": "writer-two", "title": "stale", "prompt": "stale snapshot"},
            expected_revision=stale_revision,
        )

    assert [item["id"] for item in stale.list_custom()] == []
    stale.load()
    assert [item["id"] for item in stale.list_custom()] == ["writer-one"]
    assert stale.get_revision() == first.get_revision()


def test_corrupt_asset_collection_file_is_not_overwritten(tmp_path):
    path = tmp_path / "asset_collections.json"
    original = b'{"collections": '
    path.write_bytes(original)
    store = AssetCollectionStore(str(tmp_path))

    assert store.storage_status()["ok"] is False
    with pytest.raises(RuntimeError, match="停止覆盖"):
        store.create("new")
    assert path.read_bytes() == original
    assert AssetCollectionStore(str(tmp_path)).list() == []


def test_corrupt_daily_schedule_is_not_overwritten(tmp_path):
    path = tmp_path / "daily_schedule.json"
    original = b'{"current": '
    path.write_bytes(original)
    store = DailyScheduleStore(path)
    schedule = DailySchedule(
        date="2026-11-18",
        route_id="route",
        theme="theme",
        mood="mood",
        seed="seed",
        events=[ScheduleEvent("event", "08:00", "09:00", "title", "scene", "action")],
        outfits={"main": "outfit"},
    )

    assert store.storage_status()["ok"] is False
    with pytest.raises(RuntimeError, match="停止覆盖"):
        store.set_current(schedule)
    assert path.read_bytes() == original


def test_daily_schedule_save_failure_restores_current_and_history(tmp_path, monkeypatch):
    path = tmp_path / "daily_schedule.json"
    store = DailyScheduleStore(path)
    first = DailySchedule(
        date="2026-11-17", route_id="first", theme="one", mood="", seed="",
        events=[ScheduleEvent("first", "08:00", "09:00", "title", "scene", "action")],
        outfits={"main": "one"},
    )
    second = DailySchedule(
        date="2026-11-18", route_id="second", theme="two", mood="", seed="",
        events=[ScheduleEvent("second", "08:00", "09:00", "title", "scene", "action")],
        outfits={"main": "two"},
    )
    store.set_current(first)
    original = path.read_bytes()
    monkeypatch.setattr(store, "save", _denied)

    with pytest.raises(PermissionError):
        store.set_current(second)

    assert path.read_bytes() == original
    assert store.current is not None and store.current.route_id == "first"
    assert store.history == []


def test_wardrobe_delete_save_failure_keeps_metadata_and_source_image(tmp_path, monkeypatch):
    store = WardrobeStore(tmp_path)
    item = store.add(PNG_1X1, name="original")
    image = store.resolve_path(item)
    original_metadata = store.metadata_path.read_bytes()
    monkeypatch.setattr(store, "save", _denied)

    with pytest.raises(PermissionError):
        store.remove(item.id)

    assert store.metadata_path.read_bytes() == original_metadata
    assert image.exists()
    assert store.get(item.id) is not None
    assert WardrobeStore(tmp_path).get(item.id) is not None


def test_wardrobe_update_save_failure_restores_memory(tmp_path, monkeypatch):
    store = WardrobeStore(tmp_path)
    item = store.add(PNG_1X1, name="before", description="before details")
    monkeypatch.setattr(store, "save", _denied)

    with pytest.raises(PermissionError):
        store.update(item.id, name="after", description="after details")

    restored = store.get(item.id)
    assert restored is not None
    assert restored.name == "before"
    assert restored.description == "before details"


def test_config_save_failure_restores_runtime_and_raw_state(tmp_path, monkeypatch):
    plugin = object.__new__(ConfigurationMixin)
    plugin._config_lock = threading.RLock()
    plugin.config_path = str(tmp_path / "selfie_image_config.json")
    plugin.key_config = {"web": dict(DEFAULT_CONFIG["web"])}
    plugin.raw_config = {"image": {"max_concurrent_tasks": 1}}
    plugin.config = AICatConfig.from_dict(plugin.raw_config)
    old_raw = json.loads(json.dumps(plugin.raw_config))
    old_limit = plugin.config.image_max_concurrent_tasks
    monkeypatch.setattr(config_manager, "save_json_file", _denied)

    with pytest.raises(PermissionError):
        plugin.update_config_from_web({"image": {"max_concurrent_tasks": 2}})

    assert plugin.raw_config == old_raw
    assert plugin.config.image_max_concurrent_tasks == old_limit
    assert not Path(plugin.config_path).exists()
