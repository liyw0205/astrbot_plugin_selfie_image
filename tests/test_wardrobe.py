import asyncio
import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from astrbot_plugin_selfie_image.features.daily_schedule import DailySchedule, ScheduleEvent
from astrbot_plugin_selfie_image.features.persona import PersonaManager, daily_schedule_date_key
from astrbot_plugin_selfie_image.features.reference_collector import select_wardrobe_selfie_references
from astrbot_plugin_selfie_image.features.wardrobe import WardrobeStore
from astrbot_plugin_selfie_image.core.providers import ImageReference
from astrbot_plugin_selfie_image.generation.selfie_workflow import SelfieWorkflowService


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PNG_1X1_ALT = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR42mNk+M/wHwAF/gL+P5l2WQAAAABJRU5ErkJggg=="
)


def test_wardrobe_add_deduplicates_and_persists_atomically(tmp_path):
    store = WardrobeStore(tmp_path)
    first = store.add(PNG_1X1, "image/png", name="白色连衣裙", tags="夏季, 连衣裙")
    duplicate = store.add(PNG_1X1, "image/png", name="更新名称")

    assert duplicate.id == first.id
    assert len(store.list_items()) == 1
    assert store.get(first.id).name == "更新名称"
    assert store.metadata_path.is_file()
    restored = WardrobeStore(tmp_path)
    assert restored.get(first.id).sha256 == first.sha256
    assert restored.read(first.id)[0] == PNG_1X1


def test_wardrobe_selection_avoids_recent_items_and_supports_disable_delete(tmp_path):
    store = WardrobeStore(tmp_path, recent_days=7)
    first = store.add(PNG_1X1, name="第一套")
    second = store.add(PNG_1X1_ALT, name="第二套")
    store.mark_used(first.id, "2026-11-15")
    selected = store.select_for_date("2026-11-16")
    assert selected is not None and selected.id == second.id
    store.set_enabled(second.id, False)
    assert store.select_for_date("2026-11-17").id == first.id
    store.remove(first.id)
    assert store.get(first.id) is None


def test_schedule_round_trip_keeps_wardrobe_source_fields():
    schedule = DailySchedule(
        date="2026-11-16",
        route_id="route",
        theme="日常",
        mood="平静",
        seed="seed",
        events=[
            ScheduleEvent(
                id="event",
                start="08:00",
                end="09:00",
                title="准备",
                scene="室内",
                action="整理衣袖",
                outfit_source="wardrobe",
                wardrobe_item_id="wardrobe_demo",
            )
        ],
        outfits={"main": "白色连衣裙"},
        outfit_source="wardrobe",
        wardrobe_item_id="wardrobe_demo",
    )
    restored = DailySchedule.from_dict(schedule.to_dict())
    assert restored is not None
    assert restored.outfit_source == "wardrobe"
    assert restored.wardrobe_item_id == "wardrobe_demo"
    assert restored.events[0].wardrobe_item_id == "wardrobe_demo"


def test_persona_selects_one_wardrobe_outfit_for_daily_schedule(tmp_path):
    manager = PersonaManager(tmp_path)
    manager.add_wardrobe_item(PNG_1X1, name="白色夏日连衣裙", description="白色棉麻连衣裙")
    profile = asyncio.run(manager.ensure_daily_selfie_profile("普通自拍"))
    schedule = manager.get_daily_schedule(daily_schedule_date_key())

    assert profile is not None
    assert schedule is not None
    assert schedule.outfit_source == "wardrobe"
    assert schedule.wardrobe_item_id
    assert all(
        event.outfit_ref == "main" or event.outfit_source != "wardrobe"
        for event in schedule.events
    )
    assert manager.current_wardrobe_reference() is not None
    assert "参考图二是自定义衣橱服装参考图" in manager.build_selfie_prompt(
        "自拍", "小助", "温柔", True, 1, has_wardrobe_reference=True, wardrobe_item_name="白色夏日连衣裙"
    )


def test_wardrobe_metadata_update_reconciles_same_id_daily_outfit(tmp_path):
    manager = PersonaManager(tmp_path)
    item = manager.add_wardrobe_item(
        PNG_1X1,
        name="旧名称",
        description="旧衣橱描述",
    )
    asyncio.run(manager.ensure_daily_selfie_profile())
    schedule = manager.get_daily_schedule(daily_schedule_date_key())
    assert schedule is not None and schedule.outfits["main"] == "旧衣橱描述"

    manager.update_wardrobe_item(item.id, name="新名称", description="新衣橱描述")
    manager.reconcile_wardrobe_schedule(reason="测试更新")

    refreshed = manager.get_daily_schedule(daily_schedule_date_key())
    assert refreshed is not None
    assert refreshed.wardrobe_item_id == item.id
    assert refreshed.outfits["main"] == "新衣橱描述"
    assert manager.get_daily_selfie_profile().wardrobe_item_name == "新名称"


def test_wardrobe_reconcile_preserves_explicit_schedule_outfit(tmp_path):
    manager = PersonaManager(tmp_path)
    item = manager.add_wardrobe_item(PNG_1X1, name="衣橱名称", description="衣橱描述")
    asyncio.run(manager.ensure_daily_selfie_profile())
    schedule = manager.get_daily_schedule(daily_schedule_date_key())
    assert schedule is not None
    schedule.outfits["main"] = "用户手写穿搭"
    schedule.outfit_source = "manual"
    manager._persist_daily_selfie_profile(manager._profile_from_timeline(schedule))

    manager.update_wardrobe_item(item.id, description="更新后的衣橱描述")
    manager.reconcile_wardrobe_schedule(reason="测试显式覆盖")

    refreshed = manager.get_daily_schedule(daily_schedule_date_key())
    assert refreshed is not None
    assert refreshed.outfits["main"] == "用户手写穿搭"
    assert refreshed.outfit_source == "manual"


def test_wardrobe_reference_selection_has_stable_identity_then_clothing_order():
    selection = select_wardrobe_selfie_references(
        ImageReference(data=b"identity"),
        ImageReference(data=b"wardrobe"),
        wardrobe_item_id="wardrobe_demo",
        wardrobe_item_name="白色连衣裙",
    )
    assert selection is not None
    assert [ref.data for ref in selection.refs] == [b"identity", b"wardrobe"]
    assert selection.summary()["roles"] == {"persona": 1, "wardrobe": 1}
    assert selection.summary()["wardrobe_item_id"] == "wardrobe_demo"


def _outfits(store, count=4):
    items = []
    for index in range(count):
        buffer = io.BytesIO()
        Image.new("RGB", (2, 2), (index * 40, 80, 100)).save(buffer, format="PNG")
        items.append(store.add(buffer.getvalue(), name=f"服装 {index + 1}"))
    return items


def test_sequential_rotation_preserves_import_order_same_day_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("astrbot_plugin_selfie_image.features.wardrobe._now", lambda: "2026-11-17T00:00:00")
    store = WardrobeStore(tmp_path)
    items = _outfits(store)
    store.set_selection_mode("sequential")
    assert [item.id for item in store.list_items()] == [item.id for item in items]
    for day, expected in [(17, items[0]), (18, items[1]), (19, items[2]), (20, items[3]), (21, items[0])]:
        date = f"2026-11-{day}"
        chosen = store.select_for_date(date)
        assert chosen.id == expected.id
        store.mark_used(chosen.id, date)
        store = WardrobeStore(tmp_path)
        assert store.select_for_date(date).id == chosen.id
        assert store.selection_mode == "sequential"


def test_sequential_rotation_skips_disabled_deleted_and_corrupt_images(tmp_path):
    store = WardrobeStore(tmp_path)
    first, second, third, fourth = _outfits(store)
    store.set_selection_mode("sequential")
    store.mark_used(first.id, "2026-11-17")
    store.set_enabled(second.id, False)
    store.resolve_path(third).write_bytes(b"broken image")
    assert store.select_for_date("2026-11-18").id == fourth.id
    store.mark_used(fourth.id, "2026-11-18")
    store.remove(fourth.id)
    assert store.select_for_date("2026-11-19").id == first.id
    store.set_enabled(first.id, False)
    assert store.select_for_date("2026-11-19") is None
    store.set_enabled(second.id, True)
    assert store.select_for_date("2026-11-19").id == second.id


def test_rotation_cursor_survives_removal_and_new_import(tmp_path):
    store = WardrobeStore(tmp_path)
    first, second, third, fourth = _outfits(store)
    store.set_selection_mode("sequential")
    store.mark_used(fourth.id, "2026-11-17")
    store.remove(fourth.id)
    added = store.add(PNG_1X1, name="新增")
    assert added.sort_order > fourth.sort_order
    assert store.select_for_date("2026-11-18").id == added.id
    store.mark_used(added.id, "2026-11-18")
    assert store.select_for_date("2026-11-18", force_new=True, avoid_id=added.id).id == first.id


@pytest.mark.parametrize("legacy_list", [False, True])
def test_legacy_metadata_migration_keeps_recorded_order_and_random_default(tmp_path, legacy_list):
    store = WardrobeStore(tmp_path)
    items = _outfits(store)
    values = [item.to_dict() for item in reversed(items)]
    for value in values:
        value.pop("sort_order")
    store.metadata_path.write_text(json.dumps(values if legacy_list else {"schema_version": 1, "items": values}))
    restored = WardrobeStore(tmp_path)
    assert restored.selection_mode == "random"
    assert [item.id for item in restored.list_items()] == [item.id for item in reversed(items)]
    restored.save()
    assert [item.id for item in WardrobeStore(tmp_path).list_items()] == [item.id for item in reversed(items)]


def test_manual_today_does_not_consume_rotation_and_expires_next_business_day(tmp_path):
    store = WardrobeStore(tmp_path)
    first, second, third, fourth = _outfits(store)
    store.set_selection_mode("sequential")
    store.mark_used(first.id, "2026-11-17")
    store.select_today(fourth.id, "2026-11-17")
    store.mark_used(fourth.id, "2026-11-17")
    store = WardrobeStore(tmp_path)
    assert store.select_for_date("2026-11-17", force_new=True, avoid_id=fourth.id).id == fourth.id
    assert store.summary(current_id=fourth.id, date="2026-11-17")["manual_today"] is True
    assert store.select_for_date("2026-11-18").id == second.id
    store.set_enabled(fourth.id, False)
    with pytest.raises(ValueError, match="启用"):
        store.select_today(fourth.id, "2026-11-18")
    store.resolve_path(third).unlink()
    with pytest.raises(ValueError, match="不可读"):
        store.select_today(third.id, "2026-11-18")


def test_manual_choice_updates_canonical_schedule_and_generation_references(tmp_path, monkeypatch):
    persona_module = "astrbot_plugin_selfie_image.features.persona"
    dates = []
    today = "2026-11-17"
    def business_date(start=0):
        dates.append(start)
        return today
    monkeypatch.setattr(f"{persona_module}.daily_schedule_date_key", business_date)
    manager = PersonaManager(tmp_path)
    manager.business_day_start = 5
    first, second, third, fourth = _outfits(manager.wardrobe)
    manager.wardrobe.set_selection_mode("sequential")

    async def exercise():
        initial = await manager.ensure_daily_selfie_profile()
        overrides = {event.id: event.to_dict() for event in initial.timeline.events if event.outfit_ref not in {"", "main", "same"}}
        chosen = await manager.select_wardrobe_today(fourth.id)
        assert chosen.wardrobe_item_id == fourth.id
        assert manager.get_daily_schedule(today).wardrobe_item_id == fourth.id
        assert manager.data["daily_selfie_profile"]["wardrobe_item_id"] == fourth.id
        for event in chosen.timeline.events:
            if event.id in overrides:
                assert event.to_dict() == overrides[event.id]
            else:
                assert event.wardrobe_item_id == fourth.id
        workflow = SelfieWorkflowService(SimpleNamespace(
            persona=manager,
            _persona_identity_reference=lambda **_: ImageReference(data=b"identity"),
        ))
        selection = workflow.select_selfie_references("自拍", [])
        assert selection.refs[0].data == b"identity"
        assert selection.refs[1].data == manager.wardrobe.read(fourth)[0]
        assert selection.wardrobe_item_id == fourth.id
        assert workflow.select_selfie_references("自拍", [ImageReference(data=b"message")]) is None
        assert (await manager.refresh_daily_selfie_profile_local()).wardrobe_item_id == fourth.id
    asyncio.run(exercise())
    assert dates and all(start == 5 for start in dates)
    restored = PersonaManager(tmp_path)
    restored.business_day_start = 5
    assert asyncio.run(restored.ensure_daily_selfie_profile()).wardrobe_item_id == fourth.id
    today = "2026-11-18"
    assert asyncio.run(restored.ensure_daily_selfie_profile()).wardrobe_item_id == second.id


def test_manual_choice_before_first_selfie_and_concurrent_ensure(tmp_path, monkeypatch):
    today = "2026-11-17"
    monkeypatch.setattr("astrbot_plugin_selfie_image.features.persona.daily_schedule_date_key", lambda *_: today)
    manager = PersonaManager(tmp_path)
    first, second, third, fourth = _outfits(manager.wardrobe)
    manager.wardrobe.set_selection_mode("sequential")
    async def select_and_ensure():
        profiles = await asyncio.gather(manager.select_wardrobe_today(third.id), *(manager.ensure_daily_selfie_profile() for _ in range(4)))
        assert {profile.wardrobe_item_id for profile in profiles} == {third.id}
    asyncio.run(select_and_ensure())
    assert manager.wardrobe.last_auto_order == 0
    today = "2026-11-18"
    assert asyncio.run(manager.ensure_daily_selfie_profile()).wardrobe_item_id == first.id


def test_disabling_last_outfit_clears_stale_schedule_and_reference(tmp_path):
    manager = PersonaManager(tmp_path)
    item = manager.add_wardrobe_item(PNG_1X1, name="只用于校验的衣服")
    asyncio.run(manager.select_wardrobe_today(item.id))
    manager.set_wardrobe_enabled(item.id, False)
    profile = asyncio.run(manager.ensure_daily_selfie_profile())
    assert not profile.wardrobe_item_id
    assert not profile.timeline.wardrobe_item_id
    assert item.name not in profile.timeline.outfits.values()
    assert not any(event.wardrobe_item_id for event in profile.timeline.events)
    assert manager.current_wardrobe_reference() is None


def test_wardrobe_commands_set_rotation_and_today_selection(tmp_path):
    from astrbot_plugin_selfie_image.main import SelfieImagePlugin
    plugin = object.__new__(SelfieImagePlugin)
    plugin.persona = PersonaManager(tmp_path)
    first, second, third, fourth = _outfits(plugin.persona.wardrobe)
    event = SimpleNamespace(plain_result=lambda text: text)
    async def exercise():
        rotation = [text async for text in plugin.cmd_wardrobe_rotation(event, "顺序")]
        assert "按导入顺序" in rotation[0]
        chosen = [text async for text in plugin.cmd_wardrobe_wear(event, "2")]
        assert second.name in chosen[0]
        assert plugin.persona.current_wardrobe_item().id == second.id
        invalid = [text async for text in plugin.cmd_wardrobe_wear(event, "99")]
        assert "正确的衣橱编号" in invalid[0]
        plugin.persona.set_wardrobe_enabled(first.id, False)
        disabled = [text async for text in plugin.cmd_wardrobe_wear(event, "1")]
        assert "启用" in disabled[0]
    asyncio.run(exercise())


def test_enabling_sequential_rotation_anchors_legacy_current_outfit(tmp_path, monkeypatch):
    today = "2026-11-17"
    monkeypatch.setattr("astrbot_plugin_selfie_image.features.persona.daily_schedule_date_key", lambda *_: today)
    manager = PersonaManager(tmp_path)
    first, second, third, fourth = _outfits(manager.wardrobe)
    profile = asyncio.run(manager.ensure_daily_selfie_profile())
    manager._apply_wardrobe_item(profile.timeline, second)
    manager._persist_daily_selfie_profile(manager._profile_from_timeline(profile.timeline))
    raw = json.loads(manager.wardrobe.metadata_path.read_text())
    raw.pop("selection_state")
    manager.wardrobe.metadata_path.write_text(json.dumps(raw))
    manager.wardrobe.load()
    manager.set_wardrobe_selection_mode("sequential")
    assert manager.wardrobe.last_auto_order == second.sort_order
    assert manager.current_wardrobe_item().id == second.id
    today = "2026-11-18"
    assert asyncio.run(manager.ensure_daily_selfie_profile()).wardrobe_item_id == third.id
