"""Wardrobe archives must update today's persisted outfit and references together."""

import asyncio
import base64
import copy
import threading
import zlib
from types import SimpleNamespace
from zipfile import BadZipFile

import pytest

from astrbot_plugin_selfie_image.features.backup_manager import BackupManager
from astrbot_plugin_selfie_image.features.persona import PersonaManager
from astrbot_plugin_selfie_image.core.providers import ImageReference
from astrbot_plugin_selfie_image.generation.selfie_workflow import SelfieWorkflowService
from astrbot_plugin_selfie_image.tests.test_repository_fixes import runtime_plugin
from astrbot_plugin_selfie_image.tests.test_wardrobe import PNG_1X1, PNG_1X1_ALT


@pytest.fixture
def archive_state(tmp_path, monkeypatch):
    monkeypatch.setattr("astrbot_plugin_selfie_image.features.persona.daily_schedule_date_key", lambda *_: "2026-11-17")
    monkeypatch.setattr("astrbot_plugin_selfie_image.features.selfie_profile_web_service.daily_schedule_date_key", lambda *_: "2026-11-17")
    source = PersonaManager(tmp_path / "source")
    plugin = runtime_plugin(tmp_path)
    plugin.persona = PersonaManager(tmp_path / "target")
    plugin.backup_manager = BackupManager(tmp_path / "target")
    exporter = BackupManager(tmp_path / "source")
    try:
        yield plugin, source, exporter
    finally:
        exporter.close()
        plugin.backup_manager.close()


def import_archive(plugin, exported, chunked):
    if not chunked:
        return plugin.import_wardrobe_from_web(exported)
    data = base64.b64decode(exported["content_base64"])
    session = plugin.start_archive_upload_from_web({"kind": "wardrobe", "size": len(data)})
    plugin.append_archive_upload_from_web(session["id"], {
        "offset": 0, "content_base64": exported["content_base64"], "crc32": f"{zlib.crc32(data):08x}",
    })
    return plugin.commit_archive_upload_from_web(session["id"])


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("contents", ["replacement", "same_id", "empty", "disabled", "corrupt", "manual"])
def test_import_reconciles_today_without_changing_other_schedule_fields(archive_state, contents, chunked):
    plugin, source, exporter = archive_state
    persona = plugin.persona
    old = persona.add_wardrobe_item(PNG_1X1, name="old outfit", description="old clothing details")
    profile = asyncio.run(persona.select_wardrobe_today(old.id))
    before = copy.deepcopy(profile.timeline.to_dict())
    if contents == "same_id":
        expected = source.add_wardrobe_item(PNG_1X1, name="renamed outfit", description="new clothing details")
    elif contents in {"replacement", "disabled", "corrupt", "manual"}:
        expected = source.add_wardrobe_item(PNG_1X1_ALT, name="new outfit", description="new clothing details")
        if contents == "disabled":
            source.set_wardrobe_enabled(expected.id, False)
            expected = None
        elif contents == "corrupt":
            source.wardrobe.resolve_path(expected).write_bytes(b"broken image")
            expected = None
        elif contents == "manual":
            source.add_wardrobe_item(PNG_1X1, name="other outfit")
            source.wardrobe.select_today(expected.id, "2026-11-17")
    else:
        expected = None
        source.wardrobe.save()
    exported = exporter.export_wardrobe()
    result = import_archive(plugin, exported, chunked)
    expected_id = expected.id if expected else ""
    assert result["current_item_id"] == expected_id
    schedule = persona.get_daily_schedule("2026-11-17")
    assert schedule.wardrobe_item_id == expected_id
    assert persona.data["daily_selfie_profile"]["wardrobe_item_id"] == expected_id
    assert persona.data["daily_selfie_profile"]["wardrobe_item_name"] == (expected.name if expected else "")
    if expected:
        assert schedule.outfits["main"] == expected.description
        assert persona.current_wardrobe_reference().data == source.wardrobe.read(expected)[0]
        workflow = SelfieWorkflowService(SimpleNamespace(
            persona=persona,
            _persona_identity_reference=lambda **_: ImageReference(data=b"identity"),
            _persona_auxiliary_references=lambda *_: [],
        ))
        selection = workflow.select_selfie_references("自拍", [])
        assert selection.wardrobe_item_id == expected.id
        assert [ref.data for ref in selection.refs] == [b"identity", source.wardrobe.read(expected)[0]]
    else:
        assert schedule.outfits["main"] != old.description
        assert persona.current_wardrobe_reference() is None
    after = schedule.to_dict()
    for key in before.keys() - {"outfits", "outfit_source", "wardrobe_item_id", "events"}:
        assert after[key] == before[key]
    assert {key: value for key, value in after["outfits"].items() if key != "main"} == {
        key: value for key, value in before["outfits"].items() if key != "main"
    }
    for original, updated in zip(before["events"], after["events"], strict=True):
        if original["outfit_ref"] not in {"", "main", "same"}:
            assert updated == original
        else:
            assert updated["wardrobe_item_id"] == expected_id
            for key in original.keys() - {"outfit_ref", "outfit_source", "wardrobe_item_id"}:
                assert updated[key] == original[key]
    restored = PersonaManager(persona.data_dir)
    assert restored.get_daily_schedule("2026-11-17").to_dict() == after
    assert restored.data["daily_selfie_profile"] == persona.data["daily_selfie_profile"]
    assert not list(plugin.backup_manager.transfers.root.iterdir())


@pytest.mark.parametrize("chunked", [False, True])
def test_import_does_not_create_a_schedule_before_the_first_selfie(archive_state, chunked):
    plugin, source, exporter = archive_state
    item = source.add_wardrobe_item(PNG_1X1_ALT, name="new outfit")
    result = import_archive(plugin, exporter.export_wardrobe(), chunked)
    assert result["items"][0]["id"] == item.id
    assert plugin.persona.get_daily_schedule() is None
    assert not plugin.persona.daily_schedule_store.path.exists()


def test_import_preserves_a_previous_business_day_until_the_next_profile_is_requested(archive_state, monkeypatch):
    plugin, source, exporter = archive_state
    old = plugin.persona.add_wardrobe_item(PNG_1X1, name="old outfit")
    asyncio.run(plugin.persona.select_wardrobe_today(old.id))
    before = copy.deepcopy(plugin.persona.daily_schedule_store.current.to_dict())
    item = source.add_wardrobe_item(PNG_1X1_ALT, name="new outfit")
    monkeypatch.setattr("astrbot_plugin_selfie_image.features.persona.daily_schedule_date_key", lambda *_: "2026-11-18")
    import_archive(plugin, exporter.export_wardrobe(), True)
    assert plugin.persona.daily_schedule_store.current.to_dict() == before
    assert asyncio.run(plugin.persona.ensure_daily_selfie_profile()).wardrobe_item_id == item.id


@pytest.mark.parametrize("operation", ["ensure", "select", "refresh", "cancel_select"])
def test_profile_waits_for_import_without_blocking_the_event_loop(archive_state, monkeypatch, operation):
    plugin, source, exporter = archive_state
    old = plugin.persona.add_wardrobe_item(PNG_1X1, name="old outfit")
    asyncio.run(plugin.persona.select_wardrobe_today(old.id))
    item = source.add_wardrobe_item(PNG_1X1_ALT, name="new outfit")
    exported = exporter.export_wardrobe()
    entered, release = threading.Event(), threading.Event()
    original_import = plugin.backup_manager.import_wardrobe

    def held_import(content):
        entered.set()
        assert release.wait(3), "event loop was blocked during archive import"
        return original_import(content)

    monkeypatch.setattr(plugin.backup_manager, "import_wardrobe", held_import)

    async def exercise():
        imported = asyncio.create_task(asyncio.to_thread(import_archive, plugin, exported, False))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            action = {
                "ensure": lambda: plugin.persona.ensure_daily_selfie_profile(),
                "select": lambda: plugin.persona.select_wardrobe_today(item.id),
                "refresh": lambda: plugin.persona.refresh_daily_selfie_profile_local(),
                "cancel_select": lambda: plugin.persona.select_wardrobe_today(item.id),
            }[operation]
            pending = asyncio.create_task(action())
            await asyncio.sleep(0.04)
            assert not pending.done()
            if operation == "cancel_select":
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            release.set()
            assert (await imported)["current_item_id"] == item.id
            if operation != "cancel_select":
                assert (await pending).wardrobe_item_id == item.id
            assert (plugin.persona.wardrobe.manual_id == item.id) is (operation == "select")
            assert (await plugin.persona.ensure_daily_selfie_profile()).wardrobe_item_id == item.id
        finally:
            release.set()
            await imported

    asyncio.run(exercise())


@pytest.mark.parametrize("chunked", [False, True])
def test_invalid_import_leaves_profile_and_wardrobe_unchanged(archive_state, chunked):
    plugin, _, _ = archive_state
    item = plugin.persona.add_wardrobe_item(PNG_1X1, name="old outfit")
    asyncio.run(plugin.persona.select_wardrobe_today(item.id))
    root = plugin.backup_manager.data_root
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(BadZipFile):
        import_archive(plugin, {"content_base64": base64.b64encode(b"invalid archive").decode()}, chunked)
    assert plugin.persona.current_wardrobe_item().id == item.id
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
