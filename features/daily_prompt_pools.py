"""Versioned JSON pools used by daily schedules and selfie fallbacks."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..core.prompt_data import PromptDataError, read_nonempty_list, read_versioned_json


DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DATA_PATH = DATA_DIR / "schedule" / "prompt_pools.json"
COMMON_PATH = DATA_DIR / "common" / "selfie_snapshots.json"


class DailyPromptPoolError(RuntimeError):
    """Raised when the bundled daily prompt data is incomplete or invalid."""


def _read_payload() -> Dict[str, Any]:
    try:
        payload = read_versioned_json(DATA_PATH, "日程提示词")
    except PromptDataError as exc:
        raise DailyPromptPoolError(str(exc)) from exc
    daily = payload.get("daily")
    if not isinstance(daily, Mapping):
        raise DailyPromptPoolError("日程提示词 JSON 缺少 daily 对象")
    return dict(payload)


def _list(value: Any, label: str) -> List[Any]:
    if not isinstance(value, list) or not value:
        raise DailyPromptPoolError(f"日程提示词池必须是非空数组: {label}")
    return value


def _mapping(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DailyPromptPoolError(f"日程提示词池必须是对象: {label}")
    return dict(value)


_PAYLOAD = _read_payload()
_DAILY = _mapping(_PAYLOAD.get("daily"), "daily")
_PERIOD_KEYS = {"morning", "noon", "afternoon", "evening", "night", "late_night"}

try:
    _COMMON_PAYLOAD = read_versioned_json(COMMON_PATH, "通用自拍提示词")
    SELFIE_SNAPSHOT_PRESETS: Tuple[str, ...] = tuple(
        str(item).strip()
        for item in read_nonempty_list(_COMMON_PAYLOAD, "items", "通用自拍提示词")
        if str(item).strip()
    )
except PromptDataError as exc:
    raise DailyPromptPoolError(str(exc)) from exc
if not SELFIE_SNAPSHOT_PRESETS:
    raise DailyPromptPoolError("自拍快照池不能为空")

DAILY_SCENE_VARIANTS = {
    str(period): tuple(str(item).strip() for item in _list(values, f"scene_variants/{period}") if str(item).strip())
    for period, values in _mapping(_DAILY.get("scene_variants"), "scene_variants").items()
}
DAILY_ACTION_VARIANTS = {
    str(period): tuple(str(item).strip() for item in _list(values, f"action_variants/{period}") if str(item).strip())
    for period, values in _mapping(_DAILY.get("action_variants"), "action_variants").items()
}
if set(DAILY_SCENE_VARIANTS) != _PERIOD_KEYS or set(DAILY_ACTION_VARIANTS) != _PERIOD_KEYS:
    raise DailyPromptPoolError("场景和动作变体池必须完整覆盖六个日程时段")
DAILY_SCHEDULE_ROUTES = tuple(
    dict(item)
    for item in _list(_DAILY.get("schedule_routes"), "schedule_routes")
    if isinstance(item, Mapping)
)
route_ids = [str(item.get("id") or "").strip() for item in DAILY_SCHEDULE_ROUTES]
if not DAILY_SCHEDULE_ROUTES or any(not item for item in route_ids) or len(set(route_ids)) != len(route_ids):
    raise DailyPromptPoolError("日程路线 id 缺失或重复")
for route in DAILY_SCHEDULE_ROUTES:
    events = route.get("events")
    if not str(route.get("theme") or "").strip() or not isinstance(events, Mapping) or set(events) != _PERIOD_KEYS:
        raise DailyPromptPoolError(f"日程路线缺少主题或时段事件: {route.get('id')}")
    if any(
        not isinstance(event, Mapping)
        or any(not str(event.get(field) or "").strip() for field in ("status", "scene", "action"))
        for event in events.values()
    ):
        raise DailyPromptPoolError(f"日程路线事件缺少状态、场景或动作: {route.get('id')}")

DAILY_EXTRA_OUTFIT_POOL = tuple(
    str(item).strip()
    for item in _list(_DAILY.get("extra_outfits"), "extra_outfits")
    if str(item).strip()
)
DAILY_MIX_TOPS = tuple(
    (
        str(row[0]),
        str(row[1]),
        set(str(item) for item in row[2]),
        set(str(item) for item in row[3]),
        str(row[4]),
    )
    for row in _list(_DAILY.get("mix_tops"), "mix_tops")
    if isinstance(row, list) and len(row) == 5
)
DAILY_MIX_BOTTOMS = tuple(
    (str(row[0]), str(row[1]), set(str(item) for item in row[2]), str(row[3]))
    for row in _list(_DAILY.get("mix_bottoms"), "mix_bottoms")
    if isinstance(row, list) and len(row) == 4
)
DAILY_MIX_ONE_PIECE_LOOKS = tuple(
    tuple(str(item).strip() for item in row)
    for row in _list(_DAILY.get("one_piece_looks"), "one_piece_looks")
    if isinstance(row, list) and len(row) == 5 and all(
        str(item).strip() for index, item in enumerate(row) if index != 2
    )
)
if not DAILY_MIX_TOPS or not DAILY_MIX_BOTTOMS:
    raise DailyPromptPoolError("混搭穿搭组件池不能为空")
DAILY_SEED_CHOICES = _mapping(_DAILY.get("seed_choices"), "seed_choices")
DAILY_FALLBACK_OUTFIT_POOLS = _mapping(_DAILY.get("fallback_outfits"), "fallback_outfits")

DAILY_FALLBACK_SEASONAL_LAYERS = {
    int(month): tuple(str(item).strip() for item in _list(values, f"fallback_outfits/seasonal_layers/{month}") if str(item).strip())
    for month, values in _mapping(DAILY_FALLBACK_OUTFIT_POOLS.get("seasonal_layers"), "fallback_outfits/seasonal_layers").items()
}
DAILY_FALLBACK_DAY_LOOKS = tuple(
    tuple(str(item).strip() for item in row)
    for row in _list(DAILY_FALLBACK_OUTFIT_POOLS.get("day_looks"), "fallback_outfits/day_looks")
    if isinstance(row, list) and len(row) == 5 and all(str(item).strip() for item in row[:2] + row[3:])
)
DAILY_FALLBACK_EVENING_LOOKS = tuple(
    str(item).strip()
    for item in _list(DAILY_FALLBACK_OUTFIT_POOLS.get("evening_looks"), "fallback_outfits/evening_looks")
    if str(item).strip()
)
DAILY_FALLBACK_HOME_LOOKS = tuple(
    str(item).strip()
    for item in _list(DAILY_FALLBACK_OUTFIT_POOLS.get("home_looks"), "fallback_outfits/home_looks")
    if str(item).strip()
)
DAILY_FALLBACK_MOODS = tuple(
    str(item).strip()
    for item in _list(DAILY_FALLBACK_OUTFIT_POOLS.get("moods"), "fallback_outfits/moods")
    if str(item).strip()
)

for choice_name in ("moods", "places", "activities", "colors"):
    _list(DAILY_SEED_CHOICES.get(choice_name), f"seed_choices/{choice_name}")
for pool_name in ("seasonal_layers", "day_looks", "evening_looks", "home_looks", "moods"):
    value = DAILY_FALLBACK_OUTFIT_POOLS.get(pool_name)
    if not value:
        raise DailyPromptPoolError(f"缺少备用穿搭池: {pool_name}")
if set(DAILY_FALLBACK_SEASONAL_LAYERS) != set(range(1, 13)):
    raise DailyPromptPoolError("seasonal_layers 必须覆盖 1 至 12 月")
if not all(DAILY_FALLBACK_SEASONAL_LAYERS.values()):
    raise DailyPromptPoolError("seasonal_layers 不能包含空月份")
if not DAILY_FALLBACK_DAY_LOOKS or not DAILY_FALLBACK_EVENING_LOOKS or not DAILY_FALLBACK_HOME_LOOKS or not DAILY_FALLBACK_MOODS or not DAILY_MIX_ONE_PIECE_LOOKS:
    raise DailyPromptPoolError("日常穿搭池或一件式穿搭池不能为空")
