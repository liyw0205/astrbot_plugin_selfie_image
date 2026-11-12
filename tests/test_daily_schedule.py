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
        preferred_tags=("supports_back_pose",),
    )
    assert "【shot:back_three_quarter】" in action
    assert "回望镜头" not in action
    assert "搭在栏杆" not in action
    assert "背后三分之四构图" in action
    assert "自然承重" in action
    assert "双手放松垂在身侧" not in action


def test_daily_back_action_uses_existing_scene_without_inventing_support() -> None:
    action = build_selfie_look_action(
        scene_light_provider=lambda _kind: (["默认场景"], ["窗光"]),
        preferred_scene="卧室窗边",
        preferred_gesture="背后三分之四，抬手整理发丝",
        preferred_tags=("supports_back_pose", "indoor"),
    )
    assert "【shot:back_three_quarter】" in action
    assert "场景倾向：卧室窗边" in action
    assert "小动作：背后三分之四，抬手整理发丝" in action
    assert "不凭空增加栏杆" in action


def test_daily_back_orientation_survives_a_mirror_scene_filter() -> None:
    action = build_third_person_look_action(
        scene_light_provider=lambda _kind: (["镜前", "卧室窗边"], ["窗光"]),
        preferred_scene="镜前",
        preferred_action="背身整理发丝",
        preferred_tags=("back_orientation",),
    )
    assert "【shot:back_three_quarter】" in action
    assert "场景倾向：镜前" not in action
    assert "场景倾向：卧室窗边" in action


def test_explicit_back_side_aliases_and_pure_back_remain_distinct() -> None:
    scene_light_provider = lambda _kind: (["安静卧室"], ["小夜灯"])
    back_side = build_selfie_look_action("背面侧身", scene_light_provider=scene_light_provider)
    pure_back = build_third_person_look_action("背身", scene_light_provider=scene_light_provider)

    assert "【shot:back_three_quarter】" in back_side
    assert "背后三分之四角度" in back_side
    assert "【shot:back_view】" in pure_back
    assert "双手按动作自然配合" in pure_back
    assert "双手放松垂在身侧" not in pure_back


def test_explicit_back_pose_keeps_the_requested_action():
    scene_light_provider = lambda _kind: (["安静卧室"], ["小夜灯"])
    seated = build_selfie_look_action(
        "背身坐在床边整理发丝",
        scene_light_provider=scene_light_provider,
        preferred_scene="咖啡馆窗边",
        preferred_gesture="端杯看向窗外",
    )
    hair = build_third_person_look_action(
        "背面抬手整理发丝",
        scene_light_provider=scene_light_provider,
    )

    assert "在用户指定的床沿或座位边自然坐下" in seated
    assert "整理发丝" in seated
    assert "端杯看向窗外" not in seated
    assert "一只手自然整理发丝" in hair
    assert "双手自然垂在身侧" not in hair


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


def test_double_heart_selfie_uses_offscreen_timer_and_overrides_daily_action() -> None:
    action = build_selfie_look_action(
        "双手比心",
        scene_light_provider=lambda _kind: (["窗边沙发"], ["窗光"]),
        preferred_scene="书桌前",
        preferred_gesture="刚坐好整理袖口",
    )

    assert "【shot:timer_half】" in action
    assert "画外固定机位定时自拍" in action
    assert "双手在胸前自然组成清晰完整的爱心" in action
    assert "小动作：双手在胸前自然组成清晰完整的爱心" in action
    assert "整理袖口" not in action
    assert "手臂自然伸向镜头" not in action
    assert "自己举机或镜前自拍" not in action
    assert "手机与支架均留在画面外" in action


def test_single_heart_selfie_leaves_the_other_hand_free_and_normal_selfie_keeps_arm_shot() -> None:
    single = build_selfie_look_action(
        "单手比心",
        scene_light_provider=lambda _kind: (["浅色墙与窗边"], ["窗光"]),
    )
    assert "【shot:timer_half】" in single
    assert "一只手在胸前自然比心，另一只手放松垂在身体侧边" in single
    assert "双手在胸前清楚组成完整爱心" not in single
    assert "手臂自然伸向镜头" not in single

    with patch(
        "astrbot_plugin_selfie_image.prompts.selfie_actions.random.choices",
        return_value=["arm_half"],
    ):
        ordinary = build_selfie_look_action(
            scene_light_provider=lambda _kind: (["浅色墙与窗边"], ["窗光"]),
        )
    assert "【shot:arm_half】" in ordinary

    with patch(
        "astrbot_plugin_selfie_image.prompts.selfie_actions.random.choices",
        return_value=["arm_half"],
    ):
        negated_actions = [
            build_selfie_look_action(
                request,
                scene_light_provider=lambda _kind: (["浅色墙与窗边"], ["窗光"]),
            )
            for request in ("不要比心", "不要双手在胸前比心", "不要一只手比心")
        ]
    assert all("【shot:arm_half】" in action for action in negated_actions)
    assert all("指定比心动作" not in action for action in negated_actions)


def test_daily_heart_gesture_uses_timer_shot_without_competing_pose() -> None:
    action = build_selfie_look_action(
        scene_light_provider=lambda _kind: (["咖啡馆座位"], ["窗光"]),
        preferred_scene="咖啡馆座位",
        preferred_gesture="双手在胸前比心",
    )

    assert "【shot:timer_half】" in action
    assert "小动作：双手在胸前自然组成清晰完整的爱心" in action
    assert "手机随手拍质感" not in action


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


def test_leg_focus_ignores_outdoor_schedule_scene_and_keeps_indoor_support() -> None:
    from astrbot_plugin_selfie_image.cos.leg_focus import LEGFOCUS_OUTFIT_POOL

    with patch(
        "astrbot_plugin_selfie_image.cos.leg_focus.random.choice",
        side_effect=lambda items: items[0],
    ):
        outdoor = build_leg_focus_action("黑丝", preferred_scene="城市天台步道")
        indoor = build_leg_focus_action("白丝", preferred_scene="安静卧室窗边")

    for action, wear in ((outdoor, "black"), (indoor, "white")):
        assert "本张室内场景：" in action
        assert "城市天台步道" not in action
        assert "户外小径漫步" not in action
        assert f"【wear:{wear}】" in action
        assert sum(item["prompt"] in action for item in LEGFOCUS_OUTFIT_POOL) == 1


def test_full_outfit_leg_focus_does_not_keep_lower_body_only_rule(tmp_path) -> None:
    from astrbot_plugin_selfie_image.features.persona import PersonaManager

    action = build_leg_focus_action(
        force_pose="chair_thigh_cross_drop",
        force_camera="third",
        force_framing="full_outfit",
        force_legwear="赤足",
    )
    assert "【frame:full_outfit】" in action
    assert "头部到足部完整入镜" in action
    assert "本次默认只拍下半身" not in action

    prompt = PersonaManager(tmp_path).build_selfie_prompt(
        action, "小助", "温柔", True, 0, has_external_reference=False
    )
    assert "头部到足部完整入镜" in prompt
    assert "严格从胸下或腰线开始" not in prompt
    assert "本次默认只拍下半身" not in prompt


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
    assert "当前时间段" not in prompt
    assert "日程穿搭" not in prompt


def test_leg_focus_uses_dedicated_or_reference_outfit_without_daily_clothes(tmp_path) -> None:
    from types import SimpleNamespace
    from astrbot_plugin_selfie_image.features.persona import PersonaManager, daily_schedule_date_key
    from astrbot_plugin_selfie_image.features.daily_schedule import DailySchedule, ScheduleEvent
    from astrbot_plugin_selfie_image.main import SelfieImagePlugin
    from astrbot_plugin_selfie_image.cos.leg_focus import LEGFOCUS_OUTFIT_POOL

    manager = PersonaManager(tmp_path)
    outfit = "蓝白连体COS服，保留原有长袜与短靴"
    manager.daily_schedule_store.set_current(
        DailySchedule(
            date=daily_schedule_date_key(), route_id="test-route", theme="城市漫游", mood="放松", seed="test",
            events=[ScheduleEvent(id="all-day", start="00:00", end="23:59", title="散步", scene="城市天台", action="日程动作不得照搬", outfit_ref="main")],
            outfits={"main": outfit},
        ),
    )
    plugin = SimpleNamespace(persona=manager)
    for has_refs in (False, True):
        action = SelfieImagePlugin._build_leg_focus_action(
            plugin, "", has_refs, force_pose="bed_supine_knees_together", force_camera="selfie",
        )
        prompt = manager.build_selfie_prompt(
            action, "小助", "温柔", True, int(has_refs), has_external_reference=has_refs,
        )
        assert outfit not in prompt
        if has_refs:
            assert "【outfit:reference】" in prompt
            assert "保留原有上装、下装、连体结构、袜鞋与配饰" in prompt
            assert not any(item["prompt"] in prompt for item in LEGFOCUS_OUTFIT_POOL)
        else:
            assert sum(item["prompt"] in prompt for item in LEGFOCUS_OUTFIT_POOL) == 1
        assert "【pose:bed_supine_knees_together】" in prompt
        assert "日程动作不得照搬" not in prompt
        assert "城市天台" not in prompt
        assert "本次随机腿部细节" not in prompt
