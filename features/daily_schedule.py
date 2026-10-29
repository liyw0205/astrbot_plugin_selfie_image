"""Structured local daily schedules.

The selfie persona used to persist six period maps directly in its profile.
This module keeps the schedule domain independent: a day is a timeline of
events, and an outfit is an event attribute rather than a time-period slot.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


SCHEDULE_SCHEMA_VERSION = 1
DEFAULT_BUSINESS_DAY_START = "05:00"
MAX_HISTORY_DAYS = 30

PERIOD_TIME_RANGES: dict[str, tuple[str, str]] = {
    "morning": ("05:00", "10:00"),
    "noon": ("10:00", "13:00"),
    "afternoon": ("13:00", "17:00"),
    "evening": ("17:00", "21:00"),
    "night": ("21:00", "24:00"),
    "late_night": ("00:00", "05:00"),
}


def _clean_text(value: Any, *, limit: int = 600) -> str:
    return str(value or "").strip()[:limit]


def parse_clock(value: Any, default: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """Parse HH:MM without accepting values outside a civil day."""
    raw = str(value or "").strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError):
        return default
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return default


def clock_minutes(value: Any, default: int = 0) -> int:
    if str(value or "").strip() == "24:00":
        return 24 * 60
    hour, minute = parse_clock(value)
    if not str(value or "").strip() or (hour == 0 and minute == 0 and str(value).strip() != "00:00"):
        return default
    return hour * 60 + minute


def format_clock(minutes: int) -> str:
    return f"{(int(minutes) // 60) % 24:02d}:{int(minutes) % 60:02d}"


def resolve_business_date(
    now: _datetime.datetime | _datetime.date | None = None,
    day_start: str = DEFAULT_BUSINESS_DAY_START,
) -> str:
    """Return the schedule date, allowing a day to begin after midnight."""
    if now is None:
        current = _datetime.datetime.now()
    elif isinstance(now, _datetime.datetime):
        current = now
    elif isinstance(now, _datetime.date):
        current = _datetime.datetime.combine(now, _datetime.time.min)
    else:
        current = _datetime.datetime.now()
    boundary_hour, boundary_minute = parse_clock(day_start, (5, 0))
    boundary = current.replace(
        hour=boundary_hour,
        minute=boundary_minute,
        second=0,
        microsecond=0,
    )
    if current < boundary:
        current -= _datetime.timedelta(days=1)
    return current.date().isoformat()


@dataclass(slots=True)
class ScheduleEvent:
    id: str
    start: str
    end: str
    title: str
    status: str = ""
    scene: str = ""
    action: str = ""
    outfit_ref: str = "main"
    outfit_change: str = ""
    tags: list[str] = field(default_factory=list)
    allowed_camera: list[str] = field(default_factory=list)
    allowed_pose: list[str] = field(default_factory=list)
    forbidden_pose: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: Any, *, fallback_id: str = "event") -> Optional["ScheduleEvent"]:
        if not isinstance(value, dict):
            return None
        event_id = _clean_text(value.get("id") or fallback_id, limit=80)
        start = _clean_text(value.get("start"), limit=5)
        end = _clean_text(value.get("end"), limit=5)
        if not event_id or not start or not end:
            return None
        start_minutes = clock_minutes(start, -1)
        end_minutes = clock_minutes(end, -1)
        if start_minutes < 0 or end_minutes < 0:
            return None
        if end_minutes <= start_minutes:
            # Events are daytime records. A cross-midnight event should be
            # split by the route builder instead of becoming an inverted span.
            return None
        return cls(
            id=event_id,
            start=format_clock(start_minutes),
            end="23:59" if end_minutes == 24 * 60 else format_clock(end_minutes),
            title=_clean_text(value.get("title") or value.get("name") or event_id, limit=120),
            status=_clean_text(value.get("status") or value.get("state")),
            scene=_clean_text(value.get("scene") or value.get("setting") or value.get("location")),
            action=_clean_text(value.get("action") or value.get("gesture") or value.get("pose")),
            outfit_ref=_clean_text(value.get("outfit_ref") or value.get("outfit") or "main", limit=180),
            outfit_change=_clean_text(value.get("outfit_change") or value.get("change")),
            tags=_clean_list(value.get("tags")),
            allowed_camera=_clean_list(value.get("allowed_camera")),
            allowed_pose=_clean_list(value.get("allowed_pose")),
            forbidden_pose=_clean_list(value.get("forbidden_pose")),
        )

    def start_minute(self) -> int:
        return clock_minutes(self.start)

    def end_minute(self) -> int:
        return clock_minutes(self.end)

    def contains(self, minute: int) -> bool:
        return self.start_minute() <= minute < self.end_minute()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean_list(value: Any, *, limit: int = 16) -> list[str]:
    if isinstance(value, str):
        values: Iterable[Any] = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []
    result: list[str] = []
    for item in values:
        text = _clean_text(item, limit=80)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


@dataclass(slots=True)
class DailySchedule:
    date: str
    route_id: str
    theme: str
    mood: str
    seed: str
    events: list[ScheduleEvent]
    outfits: dict[str, str] = field(default_factory=dict)
    occasion: str = ""
    source: str = "local"
    schema_version: int = SCHEDULE_SCHEMA_VERSION
    updated_at: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> Optional["DailySchedule"]:
        if not isinstance(value, dict):
            return None
        date = _clean_text(value.get("date"), limit=20)
        route_id = _clean_text(value.get("route_id") or value.get("route") or "local", limit=100)
        events: list[ScheduleEvent] = []
        raw_events = value.get("events")
        if isinstance(raw_events, dict):
            raw_events = [dict(item, id=key) if isinstance(item, dict) else item for key, item in raw_events.items()]
        if isinstance(raw_events, list):
            for index, item in enumerate(raw_events):
                event = ScheduleEvent.from_dict(item, fallback_id=f"event_{index + 1}")
                if event:
                    events.append(event)
        events.sort(key=lambda item: (item.start_minute(), item.end_minute(), item.id))
        events = _dedupe_and_validate_events(events)
        if not date or not events:
            return None
        raw_outfits = value.get("outfits")
        outfits = {}
        if isinstance(raw_outfits, dict):
            outfits = {
                _clean_text(key, limit=80): _clean_text(item, limit=300)
                for key, item in raw_outfits.items()
                if _clean_text(key, limit=80) and _clean_text(item, limit=300)
            }
        return cls(
            date=date,
            route_id=route_id,
            theme=_clean_text(value.get("theme") or value.get("day_theme")),
            mood=_clean_text(value.get("mood")),
            seed=_clean_text(value.get("seed"), limit=160),
            events=events,
            outfits=outfits,
            occasion=_clean_text(value.get("occasion")),
            source=_clean_text(value.get("source") or "local", limit=40) or "local",
            schema_version=int(value.get("schema_version") or SCHEDULE_SCHEMA_VERSION),
            updated_at=_clean_text(value.get("updated_at"), limit=40),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def current_event(self, now: _datetime.datetime | None = None) -> Optional[ScheduleEvent]:
        current = now or _datetime.datetime.now()
        minute = current.hour * 60 + current.minute
        return select_current_event(self.events, minute, wrap_previous_day=True)

    def event_for_id(self, event_id: str) -> Optional[ScheduleEvent]:
        wanted = str(event_id or "").strip()
        return next((event for event in self.events if event.id == wanted), None)

    def outfit_for_event(self, event: Optional[ScheduleEvent]) -> str:
        if event is None:
            return self.outfits.get("main", "")
        ref = event.outfit_ref.strip()
        return self.outfits.get(ref, ref if ref and ref not in {"main", "same"} else self.outfits.get("main", ""))


def _dedupe_and_validate_events(events: list[ScheduleEvent]) -> list[ScheduleEvent]:
    result: list[ScheduleEvent] = []
    seen: set[str] = set()
    last_end = -1
    for event in events:
        if event.id in seen or event.start_minute() < last_end:
            return []
        if not event.title or not event.scene or not event.action:
            return []
        seen.add(event.id)
        result.append(event)
        last_end = event.end_minute()
    return result


def select_current_event(
    events: Iterable[ScheduleEvent],
    minute: int,
    *,
    wrap_previous_day: bool = False,
) -> Optional[ScheduleEvent]:
    """Return the active event, or the nearest event during a schedule gap."""
    ordered = sorted(events, key=lambda item: (item.start_minute(), item.end_minute()))
    for event in ordered:
        if event.contains(minute):
            return event
    previous = [event for event in ordered if event.end_minute() <= minute]
    if previous:
        return previous[-1]
    if wrap_previous_day and ordered:
        return ordered[-1]
    return ordered[0] if ordered else None


def schedule_signature(schedule: DailySchedule) -> str:
    payload = "|".join(
        [
            schedule.route_id,
            schedule.theme,
            *[f"{event.scene}:{event.action}" for event in schedule.events],
            *schedule.outfits.values(),
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _infer_event_tags(scene: str, action: str) -> list[str]:
    text = f"{scene} {action}"
    tags = []
    indoor_words = ("家中", "室内", "卧室", "客厅", "书店", "咖啡馆", "剧院", "温室", "餐廊", "厨房")
    outdoor_words = ("天台", "河岸", "街", "海", "码头", "公园", "露台", "步道", "户外", "城市")
    if any(word in text for word in indoor_words):
        tags.append("indoor")
    elif any(word in text for word in outdoor_words):
        tags.append("outdoor")
    if any(word in text for word in ("坐", "椅", "沙发", "桌", "床沿")):
        tags.append("seated")
    if any(word in text for word in ("走", "步道", "经过", "散步")):
        tags.append("walking")
    if any(word in text for word in ("栏杆", "窗台", "门框", "墙边")):
        tags.append("needs_support")
    if "背" in action or "后侧" in action:
        tags.append("supports_back_pose")
    if "侧" in action:
        tags.append("supports_side_pose")
    return tags


def _outfit_ref_for_text(
    text: str,
    main_outfit: str,
    outfits: dict[str, str],
    period: str,
) -> str:
    if text == main_outfit or not text:
        return "main"
    if period == "night":
        outfits["home"] = text
        return "home"
    if period == "late_night" and text == outfits.get("home"):
        return "home"
    if period == "late_night":
        outfits["sleep"] = text
        return "sleep"
    for key, value in outfits.items():
        if value == text:
            return key
    ref = "home" if any(word in text for word in ("家居", "居家", "睡衣", "室内服")) else f"look_{period}"
    outfits.setdefault(ref, text)
    return ref


def timeline_from_period_route(
    route: dict[str, Any],
    *,
    date: str,
    seed: str,
    outfit_by_period: dict[str, str],
    period_keys: Iterable[str],
    avoid_event_signatures: Optional[set[str]] = None,
) -> DailySchedule:
    """Build a varied event timeline from an existing coherent route.

    The old route pool remains useful content, but its period keys are only
    source buckets here. The persisted result has real event times and may
    omit a quiet transition, so a day is not forced into six equal slots.
    """
    import random

    rng = random.Random(f"{date}|{seed}|{route.get('id', 'route')}")
    keys = [key for key in period_keys if isinstance(route.get("events"), dict) and key in route["events"]]
    if len(keys) < 4:
        keys = list(period_keys)
    optional = [key for key in ("noon", "afternoon", "evening") if key in keys]
    if len(keys) > 4 and rng.random() < 0.72:
        keys.remove(rng.choice(optional))
    keys = sorted(keys, key=lambda item: (0 if item == "morning" else 1 if item == "noon" else 2 if item == "afternoon" else 3 if item == "evening" else 4 if item == "night" else 5))

    base_start = {
        "morning": 7 * 60 + 20,
        "noon": 11 * 60 + 10,
        "afternoon": 14 * 60 + 10,
        "evening": 17 * 60 + 50,
        "night": 21 * 60 + 10,
        "late_night": 22 * 60 + 40,
    }
    shift = rng.choice((-18, -8, 0, 8, 16))
    outfits = {"main": str(outfit_by_period.get("morning") or "").strip()}
    events: list[ScheduleEvent] = []
    for index, period in enumerate(keys):
        source = route.get("events", {}).get(period)
        if not isinstance(source, dict):
            continue
        start = base_start.get(period, 8 * 60) + shift
        # A later event is allowed to move slightly, but never backwards.
        start += rng.choice((-6, 0, 6))
        if events:
            start = max(start, events[-1].end_minute() + 20)
        duration = rng.choice((55, 70, 85, 100))
        if period in {"night", "late_night"}:
            duration = rng.choice((45, 60, 75))
        end = min(start + duration, 23 * 60 + 58)
        if end <= start:
            end = start + 45
        scene = str(source.get("scene") or "").strip()
        action = str(source.get("action") or "").strip()
        outfit = str(outfit_by_period.get(period) or outfits["main"]).strip()
        ref = _outfit_ref_for_text(outfit, outfits["main"], outfits, period)
        previous_ref = events[-1].outfit_ref if events else ref
        outfit_change = ""
        if ref != previous_ref:
            outfit_change = (
                "回到室内后换成居家状态。"
                if ref in {"home", "sleep"}
                else f"自然换成 {outfit}。"
            )
        events.append(
            ScheduleEvent(
                id=f"{route.get('id', 'route')}_{period}_{index}",
                start=format_clock(start),
                end=format_clock(end),
                title=str(source.get("title") or period_label_fallback(period)),
                status=str(source.get("status") or "").strip(),
                scene=scene,
                action=action,
                outfit_ref=ref,
                outfit_change=outfit_change,
                tags=_infer_event_tags(scene, action),
            )
        )
    events = _dedupe_and_validate_events(events)
    if not events:
        raise ValueError("route produced no valid schedule events")
    schedule = DailySchedule(
        date=date,
        route_id=_clean_text(route.get("id") or "local", limit=100),
        theme=_clean_text(route.get("theme") or "一条有自然转场的连续日程"),
        mood="",
        seed=_clean_text(seed, limit=160),
        events=events,
        outfits=outfits,
        source="local",
        updated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
    )
    signature = schedule_signature(schedule)
    blocked = avoid_event_signatures or set()
    if signature in blocked:
        # The caller can choose another route/seed when this candidate was
        # recently used. Returning the valid candidate keeps this helper pure.
        schedule.seed = f"{seed}:alternate"
    return schedule


def period_label_fallback(period: str) -> str:
    return {
        "morning": "晨间准备",
        "noon": "午间停留",
        "afternoon": "午后安排",
        "evening": "傍晚转场",
        "night": "回家后的夜晚",
        "late_night": "睡前整理",
    }.get(period, "日程事件")


def project_schedule_to_period_maps(
    schedule: DailySchedule,
    period_keys: Iterable[str],
    period_ranges: dict[str, tuple[str, str]] = PERIOD_TIME_RANGES,
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str], dict[str, dict[str, str]]]:
    """Expose compatibility maps without making them the persisted source."""
    statuses: dict[str, str] = {}
    outfits: dict[str, str] = {}
    scenes: dict[str, str] = {}
    actions: dict[str, str] = {}
    nodes: dict[str, dict[str, str]] = {}
    for period in period_keys:
        start, end = period_ranges.get(period, ("00:00", "23:59"))
        midpoint = (clock_minutes(start, 0) + clock_minutes(end, 24 * 60)) // 2
        if midpoint >= 24 * 60:
            midpoint %= 24 * 60
        event = select_current_event(schedule.events, midpoint)
        if event is None:
            continue
        statuses[period] = event.status
        outfits[period] = schedule.outfit_for_event(event)
        scenes[period] = event.scene
        actions[period] = event.action
        nodes[period] = {
            "status": event.status,
            "scene": event.scene,
            "action": event.action,
            "outfit_ref": event.outfit_ref,
            "outfit_change": event.outfit_change,
        }
    return statuses, outfits, scenes, actions, nodes


def legacy_profile_to_schedule(
    raw: dict[str, Any],
    *,
    date: str,
    period_keys: Iterable[str],
    period_ranges: dict[str, tuple[str, str]],
) -> Optional[DailySchedule]:
    """Convert the current six-period profile into one timeline snapshot."""
    if not isinstance(raw, dict):
        return None
    status = raw.get("status_by_period") if isinstance(raw.get("status_by_period"), dict) else {}
    scenes = raw.get("scene_by_period") if isinstance(raw.get("scene_by_period"), dict) else {}
    actions = raw.get("action_by_period") if isinstance(raw.get("action_by_period"), dict) else {}
    outfits = raw.get("outfit_by_period") if isinstance(raw.get("outfit_by_period"), dict) else {}
    pool = raw.get("outfit_pool") if isinstance(raw.get("outfit_pool"), list) else []
    main_outfit = str(outfits.get("morning") or raw.get("outfit") or (pool[0] if pool else "")).strip()
    if not main_outfit:
        return None
    normalized_outfits: dict[str, str] = {"main": main_outfit}
    for key, value in outfits.items():
        text = _clean_text(value, limit=300)
        if text:
            normalized_outfits[key] = text
    events: list[ScheduleEvent] = []
    for period in period_keys:
        start, end = period_ranges.get(period, ("00:00", "23:59"))
        scene = _clean_text(scenes.get(period))
        action = _clean_text(actions.get(period))
        if not scene or not action:
            continue
        outfit = _clean_text(outfits.get(period) or main_outfit, limit=300)
        ref = "main"
        if outfit != main_outfit:
            ref = f"legacy_{period}"
            normalized_outfits[ref] = outfit
        events.append(
            ScheduleEvent(
                id=f"legacy_{period}",
                start=start,
                end=end,
                title=_clean_text(status.get(period) or period, limit=120),
                status=_clean_text(status.get(period)),
                scene=scene,
                action=action,
                outfit_ref=ref,
                outfit_change="" if ref == "main" else "兼容旧版时段穿搭",
            )
        )
    events = _dedupe_and_validate_events(sorted(events, key=lambda item: item.start_minute()))
    if not events:
        return None
    return DailySchedule(
        date=date,
        route_id="legacy_period_profile",
        theme=_clean_text(raw.get("day_theme") or "兼容旧版日程"),
        mood=_clean_text(raw.get("mood")),
        seed=_clean_text(raw.get("seed"), limit=160),
        events=events,
        outfits=normalized_outfits,
        occasion=_clean_text(raw.get("occasion")),
        source="legacy_migration",
        updated_at=_clean_text(raw.get("updated_at"), limit=40),
    )


class DailyScheduleStore:
    """Atomic JSON store for the current day and a bounded history."""

    def __init__(self, path: str | os.PathLike[str], *, history_limit: int = MAX_HISTORY_DAYS):
        self.path = Path(path)
        self.history_limit = max(1, int(history_limit))
        self.current: Optional[DailySchedule] = None
        self.history: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        self.current = None
        self.history = []
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(raw, dict):
            return
        self.current = DailySchedule.from_dict(raw.get("current"))
        raw_history = raw.get("history")
        if isinstance(raw_history, list):
            self.history = [item for item in raw_history if isinstance(item, dict)][: self.history_limit]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEDULE_SCHEMA_VERSION,
            "current": self.current.to_dict() if self.current else None,
            "history": self.history[: self.history_limit],
        }
        temp_path = self.path.with_suffix(f".{time.time_ns()}.tmp")
        try:
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self.path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def set_current(self, schedule: DailySchedule, *, remember_previous: bool = True) -> None:
        if remember_previous and self.current and self.current.date != schedule.date:
            self.remember(self.current)
        elif remember_previous and self.current and schedule_signature(self.current) != schedule_signature(schedule):
            self.remember(self.current)
        self.current = schedule
        self.save()

    def remember(self, schedule: DailySchedule) -> None:
        item = {
            "date": schedule.date,
            "route_id": schedule.route_id,
            "signature": schedule_signature(schedule),
            "scene_ids": [event.scene for event in schedule.events[:8]],
            "outfit_refs": sorted({event.outfit_ref for event in schedule.events}),
        }
        self.history = [entry for entry in self.history if entry.get("date") != schedule.date]
        self.history.insert(0, item)
        self.history = self.history[: self.history_limit]

    def recent_route_ids(self, limit: int = 7) -> set[str]:
        return {
            str(item.get("route_id"))
            for item in self.history[: max(0, limit)]
            if str(item.get("route_id") or "").strip()
        }

    def has_date(self, date: str) -> bool:
        return bool(self.current and self.current.date == str(date))
