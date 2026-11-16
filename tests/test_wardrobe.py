import asyncio
import base64

from astrbot_plugin_selfie_image.features.daily_schedule import DailySchedule, ScheduleEvent
from astrbot_plugin_selfie_image.features.persona import PersonaManager, daily_schedule_date_key
from astrbot_plugin_selfie_image.features.reference_collector import select_wardrobe_selfie_references
from astrbot_plugin_selfie_image.features.wardrobe import WardrobeStore
from astrbot_plugin_selfie_image.core.providers import ImageReference


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


def test_wardrobe_reference_selection_has_stable_identity_then_clothing_order():
    selection = select_wardrobe_selfie_references(
        ImageReference(data=b"identity"),
        ImageReference(data=b"wardrobe"),
        wardrobe_item_id="wardrobe_demo",
        wardrobe_item_name="白色连衣裙",
        persona_extra_refs=[ImageReference(data=b"auxiliary")],
    )
    assert selection is not None
    assert [ref.data for ref in selection.refs] == [b"identity", b"wardrobe", b"auxiliary"]
    assert selection.summary()["roles"] == {"persona": 1, "persona_extra": 1, "wardrobe": 1}
    assert selection.summary()["wardrobe_item_id"] == "wardrobe_demo"
