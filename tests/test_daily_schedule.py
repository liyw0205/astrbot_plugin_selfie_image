import datetime
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astrbot_plugin_selfie_image.features.daily_schedule import (
    DailySchedule,
    DailyScheduleStore,
    ScheduleEvent,
    legacy_profile_to_schedule,
    resolve_business_date,
    select_current_event,
    timeline_from_period_route,
)
from astrbot_plugin_selfie_image.prompts.selfie_actions import (
    build_selfie_look_action,
    build_third_person_look_action,
)
from astrbot_plugin_selfie_image.cos.leg_focus import build_leg_focus_action


def _event(event_id: str, start: str, end: str) -> ScheduleEvent:
    return ScheduleEvent(
        id=event_id,
        start=start,
        end=end,
        title=event_id,
        scene="有明确层次的场景",
        action="自然停留的动作",
    )


def test_business_date_respects_day_boundary() -> None:
    assert resolve_business_date(datetime.datetime(2026, 10, 29, 4, 59), "05:00") == "2026-10-28"
    assert resolve_business_date(datetime.datetime(2026, 10, 29, 5, 0), "05:00") == "2026-10-29"


def test_current_event_prefers_active_then_previous_or_next() -> None:
    events = [_event("morning", "08:00", "09:00"), _event("evening", "18:00", "19:00")]
    assert select_current_event(events, 8 * 60 + 30).id == "morning"
    assert select_current_event(events, 12 * 60).id == "morning"
    assert select_current_event(events, 6 * 60).id == "morning"
    assert select_current_event(events, 6 * 60, wrap_previous_day=True).id == "evening"


def test_schedule_round_trips_and_rejects_overlapping_events() -> None:
    schedule = DailySchedule(
        date="2026-10-29",
        route_id="route-a",
        theme="主题",
        mood="松弛",
        seed="seed",
        events=[_event("a", "08:00", "09:00"), _event("b", "10:00", "11:00")],
        outfits={"main": "白衬衫和深色长裙"},
    )
    restored = DailySchedule.from_dict(schedule.to_dict())
    assert restored is not None
    assert [item.id for item in restored.events] == ["a", "b"]
    assert DailySchedule.from_dict({**schedule.to_dict(), "events": [
        {"id": "a", "start": "08:00", "end": "10:00", "title": "a", "scene": "场景", "action": "动作"},
        {"id": "b", "start": "09:00", "end": "11:00", "title": "b", "scene": "场景", "action": "动作"},
    ]}) is None


def test_store_keeps_bounded_history(tmp_path) -> None:
    path = tmp_path / "daily_schedule.json"
    store = DailyScheduleStore(path, history_limit=1)
    first = DailySchedule(
        date="2026-10-28", route_id="first", theme="一", mood="", seed="1",
        events=[_event("a", "08:00", "09:00")], outfits={"main": "一"},
    )
    second = DailySchedule(
        date="2026-10-29", route_id="second", theme="二", mood="", seed="2",
        events=[_event("b", "08:00", "09:00")], outfits={"main": "二"},
    )
    store.set_current(first)
    store.set_current(second)
    reloaded = DailyScheduleStore(path, history_limit=1)
    assert reloaded.current is not None
    assert reloaded.current.route_id == "second"
    assert [item["route_id"] for item in reloaded.history] == ["first"]


def test_legacy_profile_migrates_to_timeline() -> None:
    raw = {
        "date": "2026-10-29",
        "outfit": "主穿搭",
        "outfit_by_period": {"morning": "主穿搭", "night": "居家服"},
        "status_by_period": {"morning": "准备出门", "night": "回家休息"},
        "scene_by_period": {"morning": "玄关", "night": "客厅"},
        "action_by_period": {"morning": "整理衣领", "night": "放下包"},
        "mood": "自然",
    }
    schedule = legacy_profile_to_schedule(
        raw,
        date="2026-10-29",
        period_keys=("morning", "night"),
        period_ranges={"morning": ("05:00", "10:00"), "night": ("21:00", "23:59")},
    )
    assert schedule is not None
    assert schedule.source == "legacy_migration"
    assert [event.scene for event in schedule.events] == ["玄关", "客厅"]
    assert schedule.outfits["legacy_night"] == "居家服"


def test_timeline_builder_is_not_a_fixed_six_period_map() -> None:
    route = {
        "id": "route",
        "theme": "有转场的一天",
        "events": {
            key: {"status": key, "scene": f"{key}场景", "action": f"{key}动作"}
            for key in ("morning", "noon", "afternoon", "evening", "night", "late_night")
        },
    }
    schedule = timeline_from_period_route(
        route,
        date="2026-10-29",
        seed="fixed-seed",
        outfit_by_period={
            "morning": "主穿搭",
            "noon": "主穿搭",
            "afternoon": "主穿搭",
            "evening": "主穿搭",
            "night": "居家服",
            "late_night": "居家服",
        },
        period_keys=("morning", "noon", "afternoon", "evening", "night", "late_night"),
    )
    assert 4 <= len(schedule.events) <= 6
    assert schedule.events[0].start != "05:00"
    assert schedule.events[-1].end != "05:00"
    assert schedule.outfit_for_event(next(event for event in schedule.events if event.outfit_ref == "home")) == "居家服"


def test_store_reloads_as_single_canonical_schedule(tmp_path) -> None:
    schedule = DailySchedule(
        date="2026-10-29",
        route_id="route-a",
        theme="主题",
        mood="松弛",
        seed="seed",
        events=[_event("a", "08:00", "09:00")],
        outfits={"main": "主穿搭"},
    )
    path = tmp_path / "daily_schedule.json"
    store = DailyScheduleStore(path)
    store.set_current(schedule, remember_previous=False)
    reloaded = DailyScheduleStore(path)
    assert reloaded.current is not None
    assert reloaded.current.to_dict() == schedule.to_dict()


def test_daily_event_keeps_indoor_scene_away_from_walk_camera() -> None:
    action = build_selfie_look_action(
        scene_light_provider=lambda _kind: (["默认场景"], ["窗光"]),
        preferred_scene="安静卧室窗边",
        preferred_gesture="沿街走过时自然停步",
    )
    assert "场景倾向：安静卧室窗边" in action
    assert "【shot:walk_candid】" not in action


def test_daily_back_event_drops_turn_back_and_missing_support_prop() -> None:
    action = build_third_person_look_action(
        scene_light_provider=lambda _kind: (["安静卧室"], ["小夜灯"]),
        preferred_scene="安静卧室",
        preferred_action="背部朝镜头，双手搭在栏杆上并回望镜头",
        preferred_tags=("back_orientation",),
    )
    assert "【shot:back_view】" in action
    assert "回望镜头" not in action
    assert "搭在栏杆" not in action
    assert "双手自然垂在身体两侧" in action


def test_daily_side_event_uses_side_camera_without_forcing_face_turn() -> None:
    action = build_selfie_look_action(
        scene_light_provider=lambda _kind: (["城市天台步道"], ["傍晚光线"]),
        preferred_scene="城市天台步道",
        preferred_gesture="身体微侧，随后回眸看向镜头",
        preferred_tags=("side_orientation", "outdoor"),
    )
    assert "【shot:side_profile】" in action
    assert "回眸看向镜头" not in action
    assert "不回头硬露正脸" in action


def test_reference_driven_selfie_does_not_inject_daily_schedule(tmp_path) -> None:
    from astrbot_plugin_selfie_image.features.persona import PersonaManager, local_date_key

    manager = PersonaManager(tmp_path)
    today = local_date_key()
    manager.data["daily_selfie_profile"] = {
        "date": today,
        "outfit": "日程穿搭",
        "outfit_by_period": {"morning": "日程穿搭"},
        "status": "日程状态",
        "status_by_period": {"morning": "日程状态"},
        "scene_by_period": {"morning": "日程场景"},
        "action_by_period": {"morning": "日程动作"},
        "source": "fallback",
        "llm_attempted": False,
    }
    with_reference = manager.build_selfie_prompt(
        "自拍", "小助", "温柔", True, 1, has_external_reference=True
    )
    persona_auxiliary_only = manager.build_selfie_prompt(
        "自拍", "小助", "温柔", True, 1, has_external_reference=False
    )
    without_reference = manager.build_selfie_prompt("自拍", "小助", "温柔", True, 0)
    assert "日程穿搭" not in with_reference
    assert "当前日程事件" not in with_reference
    assert "当前时间段" in persona_auxiliary_only
    assert "当前时间段" in without_reference


def test_leg_focus_borrows_scene_but_keeps_compatible_pose_and_outfit() -> None:
    with patch(
        "astrbot_plugin_selfie_image.cos.leg_focus.random.choice",
        side_effect=lambda items: items[0],
    ):
        outdoor = build_leg_focus_action("黑丝", preferred_scene="城市天台步道")
        indoor = build_leg_focus_action("白丝", preferred_scene="安静卧室窗边")

    assert "今日日程场景参考：城市天台步道" in outdoor
    assert "姿势池·沙发" not in outdoor
    assert "日程场景参考：安静卧室窗边" in indoor
    assert "姿势池·户外小径漫步" not in indoor
    assert "黑色不透连裤袜" in outdoor
    assert "白色不透连裤袜" in indoor


def test_reference_driven_leg_focus_does_not_inject_daily_scene(tmp_path) -> None:
    from astrbot_plugin_selfie_image.features.persona import PersonaManager, local_date_key

    manager = PersonaManager(tmp_path)
    today = local_date_key()
    manager.data["daily_selfie_profile"] = {
        "date": today,
        "outfit": "日程穿搭",
        "outfit_by_period": {"morning": "日程穿搭"},
        "scene_by_period": {"morning": "日程场景"},
        "action_by_period": {"morning": "日程动作"},
        "source": "fallback",
        "llm_attempted": False,
    }
    action = build_leg_focus_action("/看看腿 黑丝")
    prompt = manager.build_selfie_prompt(
        action, "小助", "温柔", True, 0, has_external_reference=True
    )
    assert "当前日程场景" not in prompt
    assert "日程穿搭" not in prompt
