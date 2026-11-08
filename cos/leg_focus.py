"""JSON-backed pose and indoor-scene selection for ``看看腿``."""

from __future__ import annotations

import random
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..core.prompt_data import PromptDataError, read_nonempty_list, read_versioned_json


DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "legs" / "compositions.json"
OUTFIT_DATA_PATH = DATA_PATH.with_name("outfits.json")
VIEW_DATA_PATH = DATA_PATH.with_name("views.json")


class LegFocusDataError(RuntimeError):
    """Raised when the leg-focus prompt pool is incomplete or incompatible."""


def _load_pool() -> Dict[str, Any]:
    try:
        payload = read_versioned_json(DATA_PATH, "看看腿提示词池")
        view_payload = read_versioned_json(VIEW_DATA_PATH, "看看腿机位池")
        scenes = read_nonempty_list(payload, "scenes", "看看腿场景池")
        poses = read_nonempty_list(payload, "poses", "看看腿姿势池")
        views = read_nonempty_list(view_payload, "views", "看看腿机位池")
    except PromptDataError as exc:
        raise LegFocusDataError(str(exc)) from exc
    scene_ids: set[str] = set()
    support_types: set[str] = set()
    for index, raw in enumerate(scenes):
        if not isinstance(raw, Mapping):
            raise LegFocusDataError(f"看看腿场景第 {index + 1} 项必须是对象")
        scene_id = str(raw.get("id") or "").strip()
        if not scene_id or scene_id in scene_ids:
            raise LegFocusDataError(f"看看腿场景 id 缺失或重复: {scene_id or index + 1}")
        if not str(raw.get("title") or "").strip() or not str(raw.get("prompt") or "").strip():
            raise LegFocusDataError(f"看看腿场景缺少标题或提示词: {scene_id}")
        cameras = raw.get("cameras", ["selfie", "third"])
        if (
            not isinstance(cameras, list) or not cameras
            or any(camera not in ("selfie", "third") for camera in cameras)
            or len(set(cameras)) != len(cameras)
        ):
            raise LegFocusDataError(f"看看腿场景的相机人称限制无效: {scene_id}")
        supports = raw.get("supports")
        if not isinstance(supports, Mapping) or not supports or any(
            not isinstance(key, str) or not key or not isinstance(value, str) or not value.strip()
            for key, value in supports.items()
        ):
            raise LegFocusDataError(f"看看腿场景缺少有效支撑条件: {scene_id}")
        support_types.update(supports)
        scene_ids.add(scene_id)

    views_by_id: Dict[str, Any] = {}
    for raw in views:
        if not isinstance(raw, Mapping):
            raise LegFocusDataError("看看腿机位必须是对象")
        view_id = str(raw.get("id") or "").strip()
        supports = raw.get("support_types")
        framings = raw.get("framings")
        camera = raw.get("camera")
        if (
            not re.fullmatch(r"[a-z_]+", view_id) or view_id in views_by_id
            or not str(raw.get("title") or "").strip() or not str(raw.get("prompt") or "").strip()
            or type(raw.get("weight")) is not int or raw["weight"] < 1
            or camera not in {"selfie", "third"}
            or raw.get("direction") not in ({"front", "left", "right"} if camera == "selfie" else {"front", "oblique", "side"})
            or raw.get("elevation") not in ({"down", "level"} if camera == "selfie" else {"down", "up", "level", "top"})
            or not isinstance(supports, list) or not supports or not set(supports) <= support_types
            or not isinstance(framings, list) or not framings
            or not set(framings) <= ({"leg_detail"} if camera == "selfie" else {"leg_detail", "full_outfit"})
        ):
            raise LegFocusDataError(f"看看腿机位字段或兼容条件无效: {view_id}")
        views_by_id[view_id] = raw

    pose_ids: set[str] = set()
    legacy_ids: set[str] = set()
    for index, raw in enumerate(poses):
        if not isinstance(raw, Mapping):
            raise LegFocusDataError(f"看看腿姿势第 {index + 1} 项必须是对象")
        pose_id = str(raw.get("id") or "").strip()
        if not pose_id or pose_id in pose_ids:
            raise LegFocusDataError(f"看看腿姿势 id 缺失或重复: {pose_id or index + 1}")
        view_refs = raw.get("view_ids")
        supports = raw.get("support_types")
        aliases = raw.get("legacy_ids")
        posture = raw.get("posture")
        if (
            not str(raw.get("title") or "").strip()
            or not str(raw.get("prompt") or "").strip()
            or posture not in {"seated", "reclined", "lying", "kneeling", "standing"}
            or type(raw.get("weight")) is not int
            or int(raw.get("weight") or 0) < 1
            or not isinstance(view_refs, list) or not view_refs or len(set(view_refs)) != len(view_refs)
            or not set(view_refs) <= views_by_id.keys()
            or not isinstance(supports, list) or not supports or not set(supports) <= support_types
            or not isinstance(aliases, list) or any(not isinstance(value, str) or not value for value in aliases)
            or bool(set(aliases) & legacy_ids)
        ):
            raise LegFocusDataError(f"看看腿姿势字段不完整或引用无效: {pose_id}")
        for view_id in view_refs:
            if not set(supports) & set(views_by_id[view_id]["support_types"]):
                raise LegFocusDataError(f"看看腿姿势与机位没有兼容支撑: {pose_id}/{view_id}")
        crops = raw.get("crop_prompts")
        cameras = {views_by_id[view_id]["camera"] for view_id in view_refs}
        if not isinstance(crops, Mapping) or set(crops) != cameras or any(
            not isinstance(value, str) or not value.strip() for value in crops.values()
        ):
            raise LegFocusDataError(f"看看腿姿势缺少对应人称的裁切描述: {pose_id}/crop_prompts")
        legacy_ids.update(aliases)
        pose_ids.add(pose_id)
    if legacy_ids & pose_ids:
        raise LegFocusDataError("看看腿旧姿势 id 与当前 id 冲突")

    for key in ("framing", "full_framing", "scene_rule"):
        if not str(payload.get(key) or "").strip():
            raise LegFocusDataError(f"看看腿提示词池缺少字段: {key}")
    for key, required in (
        ("camera_framings", {"selfie", "third"}),
        ("reference_rules", {"zh", "en"}),
    ):
        values = payload.get(key)
        if not isinstance(values, Mapping) or set(values) != required or any(
            not isinstance(value, str) or not value.strip() for value in values.values()
        ):
            raise LegFocusDataError(f"看看腿提示词池字段不完整或无效: {key}")
    return {**payload, "views": views}


def _load_outfit_pool() -> Dict[str, Any]:
    try:
        payload = read_versioned_json(OUTFIT_DATA_PATH, "看看腿服饰池")
        for key in ("outfits", "random_leg_details", "hosiery_styles"):
            records = read_nonempty_list(payload, key, "看看腿服饰池")
            seen: set[str] = set()
            for raw in records:
                if not isinstance(raw, Mapping):
                    raise LegFocusDataError(f"看看腿服饰池 {key} 必须是对象数组")
                item_id = str(raw.get("id") or "").strip()
                if (
                    not item_id or item_id in seen
                    or not str(raw.get("title") or "").strip()
                    or not str(raw.get("prompt") or "").strip()
                    or type(raw.get("weight")) is not int or raw["weight"] < 1
                ):
                    raise LegFocusDataError(f"看看腿服饰池字段不完整或重复: {key}/{item_id}")
                seen.add(item_id)
    except PromptDataError as exc:
        raise LegFocusDataError(str(exc)) from exc
    for key, required in (
        ("preserve_prompts", {"custom", "reference", "custom_detail", "reference_detail", "requested_detail"}),
        ("wear_prompts", {"日常裤装", "用户指定", "光腿", "光腿神器", "黑丝", "白丝", "赤足", "连裤袜"}),
        ("wear_labels", {"日常裤装", "用户指定", "光腿", "光腿神器", "黑丝", "白丝", "赤足", "连裤袜"}),
    ):
        values = payload.get(key)
        if not isinstance(values, Mapping) or not all(str(values.get(name) or "").strip() for name in required):
            raise LegFocusDataError(f"看看腿服饰池缺少字段: {key}")
    for detail in payload["random_leg_details"]:
        if detail.get("wear") not in {"黑丝", "白丝", "光腿", "赤足"}:
            raise LegFocusDataError(f"看看腿腿部细节无效: {detail['id']}")
    for style in payload["hosiery_styles"]:
        if (
            style.get("wear") not in {"黑丝", "白丝"}
            or not str(style.get("family") or "").strip()
            or style.get("kind") not in {"tights", "thigh_high", "stirrup", "ankle"}
            or type(style.get("explicit_only", False)) is not bool
            or not isinstance(style.get("aliases"), list)
            or not style["aliases"]
            or any(not isinstance(alias, str) or not alias.strip() for alias in style["aliases"])
        ):
            raise LegFocusDataError(f"看看腿袜装款式无效: {style['id']}")
    families = {style["family"] for style in payload["hosiery_styles"]}
    for outfit in payload["outfits"]:
        allowed = outfit.get("hosiery_families")
        if not isinstance(allowed, list) or not allowed or not set(allowed) <= families:
            raise LegFocusDataError(f"看看腿服饰兼容袜装无效: {outfit['id']}")
        for wear in ("黑丝", "白丝"):
            if not any(
                style["family"] in allowed and style["wear"] == wear and style["kind"] == "tights"
                for style in payload["hosiery_styles"]
            ):
                raise LegFocusDataError(f"看看腿服饰缺少兼容连裤袜: {outfit['id']}/{wear}")
    return payload


_POOL = _load_pool()
_OUTFITS = _load_outfit_pool()
LEGFOCUS_SCENE_POOL: List[Dict[str, Any]] = [dict(item) for item in _POOL["scenes"]]
LEGFOCUS_SCENE_BY_ID = {str(item["id"]): item for item in LEGFOCUS_SCENE_POOL}
LEGFOCUS_POSE_POOL: List[Dict[str, Any]] = [dict(item) for item in _POOL["poses"]]
LEGFOCUS_POSE_BY_ID = {str(item["id"]): item for item in LEGFOCUS_POSE_POOL}
LEGFOCUS_VIEW_POOL: List[Dict[str, Any]] = [dict(item) for item in _POOL["views"]]
LEGFOCUS_VIEW_BY_ID = {str(item["id"]): item for item in LEGFOCUS_VIEW_POOL}
LEGFOCUS_RANDOM_LEG_DETAILS: List[Dict[str, Any]] = [
    dict(item) for item in _OUTFITS["random_leg_details"]
]
LEGFOCUS_FRAMING = str(_POOL["framing"])
LEGFOCUS_CAMERA_FRAMINGS = dict(_POOL["camera_framings"])
LEGFOCUS_FULL_FRAMING = str(_POOL["full_framing"])
LEGFOCUS_REFERENCE_RULES = dict(_POOL["reference_rules"])
LEGFOCUS_SCENE_RULE = str(_POOL["scene_rule"])
LEGFOCUS_PRESERVE_PROMPTS = dict(_OUTFITS["preserve_prompts"])
LEGFOCUS_WEAR_PROMPTS = dict(_OUTFITS["wear_prompts"])
LEGFOCUS_WEAR_LABELS = dict(_OUTFITS["wear_labels"])
LEGFOCUS_HOSIERY_STYLES: List[Dict[str, Any]] = [dict(item) for item in _OUTFITS["hosiery_styles"]]

LEGFOCUS_POSE_IDS = frozenset(LEGFOCUS_POSE_BY_ID)
LEGFOCUS_EXPLICIT_ONLY_POSE_IDS = frozenset()
LEGFOCUS_SEATED_POSE_IDS = frozenset(
    item["id"] for item in LEGFOCUS_POSE_POOL if item.get("posture") == "seated"
)
LEGFOCUS_SCENE_TERMS = tuple(
    dict.fromkeys(
        str(term).strip()
        for scene in LEGFOCUS_SCENE_POOL
        for term in (scene.get("title"), *(scene.get("aliases") or []))
        if str(term or "").strip()
    )
)

# Persisted action tags remain readable while resolving to the current indoor pool.
LEGFOCUS_POSE_ALIASES = {
    "sit": "sofa_ankle_cross",
    "sit_crop": "sofa_ankle_cross",
    "kneel": "kneeling_sit_back",
    "kneel_crop": "kneeling_sit_back",
    "side_lie": "side_lying_offset_legs",
    "side_lie_crop": "side_lying_offset_legs",
    "hug_knee": "bed_one_knee_up",
    "hug_knee_crop": "bed_one_knee_up",
    "cross_leg": "chair_crossed_knee",
    "cross_leg_crop": "chair_crossed_knee",
    "windowsill": "gaming_chair_staggered",
    "windowsill_crop": "gaming_chair_staggered",
    "kneel_up": "kneeling_upright",
    "kneel_front": "kneeling_sit_back",
    "floor_fold": "rug_back_support",
    "one_knee_fix": "bed_one_knee_up",
    "reclined_knees_crop": "bed_side_recline",
    "floor_knees_up_crop": "rug_side_fold",
    "desk_sit_crop": "chair_crossed_knee",
    "bed_supine_crop": "supine_offset_legs",
    "sofa_front_crop": "seated_knees_together",
    "chair_side_crop": "chair_crossed_knee",
    "sofa_cross_crop": "sofa_side_fold",
    "floor_knees_crop": "rug_side_fold",
    "sofa_occlusion_crop": "sofa_forward_leg",
    "stool_edge_crop": "bed_edge_diagonal",
    "floor_side_kneel_crop": "kneeling_sit_back",
    "seat_knees_cross_crop": "sofa_ankle_cross",
    "floor_topdown_cross_crop": "rug_back_support",
    "first_person_topdown_legs_crop": "bed_edge_diagonal",
    "staggered_stair_step_crop": "bed_one_knee_up",
    "cross_step_snapshot_crop": "gaming_chair_staggered",
    "turn_and_settle_crop": "chair_crossed_knee",
    "bench_side_sit_crop": "gaming_chair_staggered",
    "toe_touch_step_crop": "bed_one_knee_up",
    "back_staggered_step_crop": "sofa_side_fold",
    "knees_forward_sit_crop": "edge_parallel_forward",
    "one_knee_bent_sit_crop": "bed_one_knee_up",
    "side_folded_sit_crop": "sofa_side_fold",
    "reclined_bent_knees_crop": "bed_side_recline",
    "one_leg_extended_sit_crop": "sofa_forward_leg",
    "heels_folded_sit_crop": "rug_side_fold",
    "edge_dangle_sit_crop": "edge_knees_together_dangle",
    "crossed_ankles_sit_crop": "sofa_ankle_cross",
    "crossed_knee_sit_crop": "chair_crossed_knee",
    "over_leg_sit_crop": "chair_crossed_knee",
    "supported_legs_forward_crop": "rug_back_support",
    "relaxed_knees_apart_sit_crop": "sofa_ankle_cross",
}
_DATA_POSE_ALIASES = {
    legacy_id: str(pose["id"])
    for pose in LEGFOCUS_POSE_POOL for legacy_id in pose["legacy_ids"]
}
LEGFOCUS_POSE_ALIASES = {
    alias: _DATA_POSE_ALIASES.get(pose_id, pose_id)
    for alias, pose_id in LEGFOCUS_POSE_ALIASES.items()
}
LEGFOCUS_POSE_ALIASES.update(_DATA_POSE_ALIASES)

LEGFOCUS_CAMERA_WEIGHTS: Dict[str, Tuple[Tuple[str, int], ...]] = {
    str(item["id"]): tuple(
        (camera, 1) for camera in dict.fromkeys(LEGFOCUS_VIEW_BY_ID[view_id]["camera"] for view_id in item["view_ids"])
    )
    for item in LEGFOCUS_POSE_POOL
}
for old_id, current_id in LEGFOCUS_POSE_ALIASES.items():
    LEGFOCUS_CAMERA_WEIGHTS[old_id] = LEGFOCUS_CAMERA_WEIGHTS[current_id]

LEGWEAR_PROMPTS = {
    str(key): str(value)
    for key, value in LEGFOCUS_WEAR_PROMPTS.items()
    if str(key).strip() and str(value).strip()
}
SAFE_LEGWEAR_LABELS = {
    str(key): str(value)
    for key, value in LEGFOCUS_WEAR_LABELS.items()
    if str(key).strip() and str(value).strip()
}
_DETAIL_TAGS = {str(item["id"]): str(item["wear"]) for item in LEGFOCUS_RANDOM_LEG_DETAILS}
_DETAIL_WEIGHTS = [
    _DETAIL_TAGS[str(item["id"])]
    for item in LEGFOCUS_RANDOM_LEG_DETAILS
    for _ in range(int(item["weight"]))
    if str(item["id"]) in _DETAIL_TAGS
]
LEGWEAR_BY_POSE = {
    pose_id: tuple((tag, sum(1 for value in _DETAIL_WEIGHTS if value == tag)) for tag in dict.fromkeys(_DETAIL_WEIGHTS))
    for pose_id in LEGFOCUS_POSE_IDS
}
for old_id, current_id in LEGFOCUS_POSE_ALIASES.items():
    LEGWEAR_BY_POSE[old_id] = LEGWEAR_BY_POSE[current_id]

LEGFOCUS_OUTFIT_POOL: List[Dict[str, Any]] = [dict(item) for item in _OUTFITS["outfits"]]
LEGFOCUS_OUTFIT_BY_ID = {str(item["id"]): item for item in LEGFOCUS_OUTFIT_POOL}
LEGFOCUS_RISKY_EXTRA_REPLACEMENTS = (
    ("掀衣摆", "整理衣摆"),
)

LEGWEAR_REQUEST_PATTERN = re.compile(
    r"(?:光腿神器|光腿|白丝|黑丝|丝袜|短袜|堆堆袜|过膝袜|长筒袜|踩脚袜|肉色丝袜|肉丝|连裤袜|长袜|赤足|裸足|光脚)"
)
LEGWEAR_STYLE_TERMS = tuple(
    dict.fromkeys(
        str(term).strip()
        for style in LEGFOCUS_HOSIERY_STYLES
        for term in (style["title"], *(style.get("aliases") or []))
        if str(term or "").strip()
    )
)
LEGWEAR_NEGATION_PATTERN = re.compile(
    r"(?:不穿|不要|不需要|无需|不必|不加|不搭配|不用|不带|没有|没穿)\s*(?:任何)?\s*"
    r"(?:袜子|袜装|袜|鞋履|鞋子|鞋|靴子|靴)"
    r"(?:\s*(?:和|与|或|、|及)\s*(?:袜子|袜装|袜|鞋履|鞋子|鞋|靴子|靴))*"
)
LEG_NEGATION_PREFIX = r"(?:不要|别|避免|禁止|不得|不需要|不想要|无需|不必|不穿|不加|不搭配|不用|不带|没有|没穿)"
LEG_DETAIL_REQUEST_PATTERN = re.compile(
    r"(?:下半身特写|腿部特写|下装特写|局部特写|腰部以下|只拍腿|只拍下装|不拍上半身|上半身不入镜|不露脸|不露头|近距离拍腿)"
)
LEG_FACE_REQUEST_PATTERN = re.compile(r"(?:露脸|露头|带脸|看脸|面部入镜|表情|看向镜头|微笑|wink|眨眼)", re.I)
LEG_FULL_OUTFIT_REQUEST_PATTERN = re.compile(
    r"(?:全身(?!镜)|从头到脚|头部到鞋履|full[- ]?body)", re.I
)
LEGFOCUS_OUTFIT_REQUEST_PATTERN = re.compile(
    r"(?:裙|百褶|A字|裤|鞋|靴|平底|乐福|玛丽珍|帆布鞋|凉鞋|JK|洛丽塔|水手服|套装|T恤|衬衫|针织|卫衣|背心)", re.I
)
LEGFOCUS_COMPLETE_OUTFIT_REQUEST_PATTERN = re.compile(
    r"(?:身穿|穿着|换成|换上|改穿|连体(?:衣|服)|连衣裙|COS服|cosplay服装|制服|和服|旗袍|女仆装|礼服|参考图.{0,8}(?:服装|衣服|穿搭))",
    re.I,
)
LEG_FACE_ACTIONS: Dict[str, str] = {}


def parse_requested_leg_face(text: str) -> bool:
    """Only explicit face requests opt out of the default faceless crop."""
    raw = str(text or "")
    if re.search(r"(?:不露脸|不露头|不拍脸|只拍腿|只拍下装|上半身不入镜)", raw):
        return False
    return bool(LEG_FACE_REQUEST_PATTERN.search(raw))


def has_leg_face_context(text: str) -> bool:
    return "【legs_face:context】" in str(text or "")


def is_leg_calf_crop_action(text: str) -> bool:
    raw = str(text or "")
    frame = re.search(r"【frame:(full_outfit|leg_detail)】", raw)
    if frame:
        return frame.group(1) == "leg_detail"
    if LEG_DETAIL_REQUEST_PATTERN.search(raw):
        return True
    pose = re.search(r"【pose:([a-z_]+)】", raw)
    return bool(pose)


CALF_CROP_POSES = frozenset(
    set(LEGFOCUS_POSE_IDS) | set(LEGFOCUS_POSE_ALIASES) | {"sit", "sit_crop"}
)


def _has_positive_term(text: str, term: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    start = 0
    while True:
        index = compact.find(term, start)
        if index < 0:
            return False
        prefix = compact[max(0, index - 12):index]
        if not re.search(
            r"(?:不要|不在|不去|不选|不需要|不想要|无需|不必|不穿|不加|不搭配|不用|不带|没有|没穿|避免|禁止|别)"
            r"(?:再|去|在|用|穿|选择|采用|使用)*$",
            prefix,
        ):
            return True
        start = index + len(term)


def _has_negative_term(text: str, term: str) -> bool:
    """Return whether a term is explicitly negated in the user request."""
    compact = re.sub(r"\s+", "", str(text or ""))
    needle = re.sub(r"\s+", "", str(term or ""))
    if not needle:
        return False
    for match in re.finditer(re.escape(needle), compact, re.I):
        prefix = compact[max(0, match.start() - 12):match.start()]
        if re.search(LEG_NEGATION_PREFIX + r"(?:再|去|在|用|穿|选择|采用|使用)*$", prefix, re.I):
            return True
    return False


def _constraint_kinds(text: str, categories: Dict[str, tuple[str, ...]]) -> Tuple[set[str], set[str]]:
    """Classify longest phrases once, so '非自拍' does not also mean '自拍'."""
    raw = re.sub(r"\s+", "", str(text or "")).lower()
    terms = {term.lower(): kind for kind, aliases in categories.items() for term in aliases}
    pattern = "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
    positive: set[str] = set()
    negative: set[str] = set()
    for match in re.finditer(pattern, raw):
        prefix = raw[max(0, match.start() - 12):match.start()]
        negated = re.search(LEG_NEGATION_PREFIX + r"(?:再|去|在|用|穿|选择|采用|使用)*$", prefix)
        target = negative if negated else positive
        target.add(terms[match.group()])
    return positive, negative


def _preserve_unhandled_legwear(text: str) -> str:
    """Keep unsupported sock requests intact instead of reducing them to a color."""
    raw = str(text or "")
    supported = [term for term in LEGWEAR_STYLE_TERMS if term in raw]
    for term in sorted(supported, key=len, reverse=True):
        raw = raw.replace(term, " ")
    unknown = [
        match.group(0)
        for match in re.finditer(r"(?:[黑白粉灰蓝红紫肤肉乳][色白]?|纯色|条纹|蕾丝)?(?:短袜|堆堆袜|长筒袜|过膝袜|踩脚袜|长袜)", raw)
        if _has_positive_term(raw, match.group(0))
    ]
    return "、".join(dict.fromkeys(unknown))


def parse_requested_leg_scene(text: str) -> str:
    raw = re.sub(r"场景不变|背景不变|保持场景|保持背景", "", str(text or ""))
    matches: List[str] = []
    for scene in LEGFOCUS_SCENE_POOL:
        terms = (str(scene["id"]), str(scene["title"]), *(scene.get("aliases") or []))
        if any(_has_positive_term(raw, str(term)) for term in terms if str(term).strip()):
            matches.append(str(scene["id"]))
    return " ".join(dict.fromkeys(matches))


def parse_excluded_leg_scenes(text: str) -> set[str]:
    raw = str(text or "")
    excluded = set()
    for scene in LEGFOCUS_SCENE_POOL:
        terms = (str(scene["id"]), str(scene["title"]), *(scene.get("aliases") or []))
        if any(_has_negative_term(raw, term) for term in terms if str(term).strip()):
            excluded.add(str(scene["id"]))
    return excluded


def _resolve_pose_id(pose_id: str) -> str:
    value = str(pose_id or "").strip()
    if value in LEGFOCUS_POSE_BY_ID:
        return value
    return LEGFOCUS_POSE_ALIASES.get(value, "")


def compatible_leg_compositions(
    pose: Dict[str, Any], *, camera: str = "", scene_ids: str = "",
    view_id: str = "", framing: str = "leg_detail", angles: Dict[str, Any] | None = None,
) -> List[Tuple[Dict[str, Any], str, Dict[str, Any]]]:
    requested_scenes = set(scene_ids.split())
    filters = angles or {}
    combinations = []
    for scene in LEGFOCUS_SCENE_POOL:
        if requested_scenes and scene["id"] not in requested_scenes:
            continue
        if scene["id"] in filters.get("excluded_scenes", ()):
            continue
        # Selection uses the first compatible support for a given scene/view.
        selected_views: set[str] = set()
        for support in pose["support_types"]:
            if support not in scene["supports"]:
                continue
            for candidate_id in pose["view_ids"]:
                view = LEGFOCUS_VIEW_BY_ID[candidate_id]
                if (
                    candidate_id in selected_views
                    or (view_id and candidate_id != view_id)
                    or (camera and view["camera"] != camera)
                    or view["camera"] not in scene.get("cameras", ("selfie", "third"))
                    or framing not in view["framings"]
                    or support not in view["support_types"]
                    or (filters.get("direction") and view["direction"] != filters["direction"])
                    or (filters.get("elevations") and view["elevation"] not in filters["elevations"])
                    or view["direction"] in filters.get("excluded_directions", ())
                    or view["elevation"] in filters.get("excluded_elevations", ())
                    or view["camera"] in filters.get("excluded_cameras", ())
                ):
                    continue
                combinations.append((scene, support, view))
                selected_views.add(candidate_id)
    return combinations


def _weighted_pose_choice(pool: List[Dict[str, Any]]) -> Dict[str, Any]:
    return dict(random.choices(pool, weights=[int(item["weight"]) for item in pool], k=1)[0])


def pick_leg_focus_pose(
    *,
    avoid_id: str = "",
    preferred_scene: str = "",
    camera_kind: str = "",
    include_face: bool = False,
) -> Dict[str, Any]:
    del include_face
    scene_hint = parse_requested_leg_scene(preferred_scene) or str(preferred_scene or "")
    avoid_id = _resolve_pose_id(avoid_id) or str(avoid_id or "")
    pool = [
        item for item in LEGFOCUS_POSE_POOL
        if compatible_leg_compositions(item, camera=camera_kind, scene_ids=scene_hint)
    ]
    if not pool:
        raise LegFocusDataError("看看腿没有兼容当前场景与相机的姿势")
    pool = [item for item in pool if item["id"] != avoid_id] or pool
    return _weighted_pose_choice(pool)


def pick_leg_focus_outfit(*, avoid_id: str = "", hosiery_family: str = "") -> Dict[str, Any]:
    pool = [
        item for item in LEGFOCUS_OUTFIT_POOL
        if item["id"] != avoid_id
        and (not hosiery_family or hosiery_family in item["hosiery_families"])
    ]
    if not pool:
        pool = [item for item in LEGFOCUS_OUTFIT_POOL if not hosiery_family or hosiery_family in item["hosiery_families"]]
    if not pool:
        raise LegFocusDataError(f"看看腿服饰池没有兼容袜装: {hosiery_family}")
    return dict(random.choices(pool, weights=[item["weight"] for item in pool], k=1)[0])


def get_leg_focus_outfit(outfit_id: str) -> Dict[str, Any]:
    return dict(LEGFOCUS_OUTFIT_BY_ID.get(str(outfit_id or ""), {}))


def _requested_hosiery_style(text: str) -> Dict[str, Any]:
    matches = [
        (len(alias), style)
        for style in LEGFOCUS_HOSIERY_STYLES
        for alias in (style["title"], *style["aliases"])
        if _has_positive_term(text, alias)
    ]
    return dict(max(matches, key=lambda item: item[0])[1]) if matches else {}


def _hosiery_allowed(style: Dict[str, Any], text: str) -> bool:
    kind_terms = {
        "tights": ("连裤袜",), "thigh_high": ("过膝袜", "长筒袜", "长袜"),
        "stirrup": ("踩脚袜",), "ankle": ("短袜", "踝袜"),
    }
    return not any(
        _has_negative_term(text, term)
        for term in (style["title"], *style["aliases"], *kind_terms[style["kind"]])
    )


def _pick_hosiery_style(wear: str, outfit: Dict[str, Any], text: str = "") -> Dict[str, Any]:
    pool = [
        style for style in LEGFOCUS_HOSIERY_STYLES
        if style["family"] in outfit["hosiery_families"]
        and (style["wear"] == wear or (wear == "连裤袜" and style["kind"] == "tights"))
        and not style.get("explicit_only")
        and _hosiery_allowed(style, text)
    ]
    return dict(random.choices(pool, weights=[item["weight"] for item in pool], k=1)[0]) if pool else {}


def has_explicit_leg_outfit(text: str) -> bool:
    cleaned = LEGWEAR_NEGATION_PATTERN.sub(" ", str(text or ""))
    for style in LEGFOCUS_HOSIERY_STYLES:
        for term in (style["title"], *style["aliases"]):
            cleaned = cleaned.replace(term, " ")
    cleaned = LEGWEAR_REQUEST_PATTERN.sub(" ", cleaned)
    cleaned = re.sub(r"(?:身穿|穿着|换成|换上|改穿)\s*$", " ", cleaned)
    cleaned = re.sub(r"(?:参考图中的)?鞋袜搭配|鞋袜", " ", cleaned)
    return bool(
        LEGFOCUS_OUTFIT_REQUEST_PATTERN.search(cleaned)
        or LEGFOCUS_COMPLETE_OUTFIT_REQUEST_PATTERN.search(cleaned)
    )


def parse_requested_leg_camera(text: str) -> str:
    positive, negative = _constraint_kinds(text, LEG_CAMERA_TERMS)
    if len(positive) > 1 or positive & negative:
        raise LegFocusDataError("看看腿指定了冲突的相机人称，请选择第一人称或第三人称中的一个")
    return next(iter(positive), "")


LEG_CAMERA_TERMS = {
    "third": ("第三人称", "第三人称视角", "他拍", "别人拍", "朋友拍", "摄影师拍", "路人视角", "非自拍", "notselfie", "thirdperson"),
    "selfie": ("第一人称", "第一人称视角", "第一视角", "主观视角", "手机自拍", "自拍", "自己拍", "selfie"),
}


def parse_excluded_leg_cameras(text: str) -> set[str]:
    return _constraint_kinds(text, LEG_CAMERA_TERMS)[1]


def parse_requested_leg_pose(text: str) -> str:
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    matches = []
    for pose in LEGFOCUS_POSE_POOL:
        terms = (pose["id"], pose["title"], *pose["legacy_ids"], *pose["aliases"])
        matches.extend(
            (len(term), pose["id"]) for term in terms
            if _has_positive_term(compact, re.sub(r"\s+", "", term).lower())
        )
    return max(matches, key=lambda item: item[0])[1] if matches else ""


def parse_excluded_leg_poses(text: str) -> set[str]:
    raw = str(text or "")
    excluded = set()
    for pose in LEGFOCUS_POSE_POOL:
        terms = (pose["id"], pose["title"], *pose["legacy_ids"], *pose["aliases"])
        if any(_has_negative_term(raw, term) for term in terms if str(term).strip()):
            excluded.add(str(pose["id"]))
    return excluded


def parse_requested_leg_view(text: str) -> str:
    raw = str(text or "")
    tag = re.search(r"【leg_view:([a-z_]+)】", raw)
    if tag and tag.group(1) in LEGFOCUS_VIEW_BY_ID:
        return tag.group(1)
    matches = [
        (len(term), view["id"]) for view in LEGFOCUS_VIEW_POOL
        for term in (view["id"], view["title"]) if _has_positive_term(raw, term)
    ]
    return max(matches, key=lambda item: item[0])[1] if matches else ""


def parse_requested_leg_angles(text: str) -> Dict[str, Any]:
    raw = str(text or "").lower()
    if any(_has_positive_term(raw, term) for term in ("背拍", "背身", "背面", "侧后方", "后方拍")):
        raise LegFocusDataError("看看腿不支持背面或侧后方机位，请使用正前方、斜前方或侧面机位")
    filters: Dict[str, Any] = {"excluded_directions": set(), "excluded_elevations": set()}
    direction_kinds, excluded_directions = _constraint_kinds(raw, dict((
        ("oblique", ("斜前方", "侧前方", "斜侧拍")),
        ("side", ("侧拍", "侧面", "侧方")),
        ("front", ("正前方", "正面拍", "正拍")),
        ("left", ("偏左",)), ("right", ("偏右",)),
    )))
    filters["excluded_directions"] = excluded_directions
    if len(direction_kinds) > 1:
        raise LegFocusDataError("看看腿同时指定了不兼容的镜头方向，请选择正前方、斜前方或侧面中的一个")
    if direction_kinds:
        filters["direction"] = next(iter(direction_kinds))
    level_requested = any(_has_positive_term(raw, term) for term in ("平拍", "平视"))
    explicit_up = any(_has_positive_term(raw, term) for term in ("仰拍", "仰视", "向上拍"))
    explicit_down = any(_has_positive_term(raw, term) for term in ("俯拍", "俯视", "向下拍", "向下看"))
    # “低机位/高机位平拍” describes camera height, not a tilted lens.
    if level_requested and not (explicit_up or explicit_down):
        filters["elevations"] = {"level"}
    elif level_requested and (explicit_up or explicit_down):
        raise LegFocusDataError("看看腿同时指定了不兼容的俯仰角度，请选择仰拍、俯拍或平拍中的一个")
    for values, terms in (
        ({"top"}, ("正上方", "顶拍", "topdown", "top-down")),
        ({"down"}, ("俯拍", "俯视", "向下拍", "向下看")),
        ({"up"}, ("仰拍", "低机位", "向上拍")),
        ({"level"}, ("平拍", "平视")),
    ):
        if level_requested and values == {"level"}:
            continue
        positive = any(_has_positive_term(raw, term) for term in terms)
        if values == {"up"} and level_requested and not explicit_up:
            positive = False
        if positive:
            selected = filters.get("elevations", values) & values
            if not selected:
                raise LegFocusDataError("看看腿同时指定了不兼容的俯仰角度，请选择一个角度")
            filters["elevations"] = selected
        elif any(_has_negative_term(raw, term) for term in terms):
            filters["excluded_elevations"].update(values)
            if values == {"down"}:
                filters["excluded_elevations"].add("top")
    return filters


def parse_excluded_legwear(text: str) -> set[str]:
    raw = str(text or "")
    excluded: set[str] = set()
    no_socks = any(
        re.search(r"袜", match.group()) for match in LEGWEAR_NEGATION_PATTERN.finditer(raw)
    )
    if no_socks or any(_has_negative_term(raw, term) for term in ("袜子", "袜装", "袜", "丝袜")):
        excluded.update({"黑丝", "白丝", "连裤袜"})
    if any(_has_negative_term(raw, term) for term in ("黑丝", "黑色丝袜", "黑色袜装")):
        excluded.add("黑丝")
    if any(_has_negative_term(raw, term) for term in ("白丝", "白色丝袜", "白色袜装")):
        excluded.add("白丝")
    if any(_has_negative_term(raw, term) for term in ("光腿", "光腿神器")):
        excluded.update({"光腿", "光腿神器", "赤足"})
    if any(_has_negative_term(raw, term) for term in ("赤足", "裸足", "光脚")):
        excluded.add("赤足")
    return excluded


def parse_requested_legwear(text: str) -> str:
    raw = str(text or "")
    safe_tag = re.search(r"【wear:(daily|custom|white|black|bare|barefoot|pantyhose)】", raw)
    if safe_tag:
        return {
            "daily": "日常裤装",
            "custom": "用户指定",
            "white": "白丝",
            "black": "黑丝",
            "bare": "光腿神器",
            "barefoot": "赤足",
            "pantyhose": "连裤袜",
        }[safe_tag.group(1)]
    requested_style = _requested_hosiery_style(raw)
    if requested_style:
        return str(requested_style["wear"])
    explicit_values = ("白丝", "黑丝", "光腿神器", "光腿", "赤足", "连裤袜", "丝袜", "光脚", "裸足")
    for value in explicit_values:
        if _has_positive_term(LEGWEAR_NEGATION_PATTERN.sub(" ", raw), value):
            if value in {"光脚", "裸足"}:
                return "赤足"
            if value == "丝袜":
                return "连裤袜"
            if value == "光腿神器":
                return "光腿"
            return value
    if re.search(r"(?:长裤|牛仔裤|西裤|休闲裤|阔腿裤)", raw):
        return "日常裤装"
    return ""


def _weighted_detail_choice(excluded_wears: set[str] | None = None) -> Dict[str, Any]:
    excluded = set(excluded_wears or ())
    pool = [
        item for item in LEGFOCUS_RANDOM_LEG_DETAILS
        if str(item.get("wear") or "") not in excluded
    ]
    if not pool:
        raise LegFocusDataError("看看腿没有满足排除条件的腿部细节，请放宽袜装要求")
    return dict(random.choices(pool, weights=[int(item["weight"]) for item in pool], k=1)[0])


def _clean_extra_request(extra_request: str) -> str:
    extra = re.sub(r"/?看看腿", " ", str(extra_request or ""), flags=re.I)
    extra = re.sub(r"【(?:legs|legs_face|leg_view|leg_scene|leg_support|cam|wear|pose|outfit|frame):[a-z_]+】", " ", extra, flags=re.I)
    # Keep the original constraint once; deleting fragments loses negation,
    # sock lengths, or the support required by a free-form pose.
    for risky_text, neutral_text in LEGFOCUS_RISKY_EXTRA_REPLACEMENTS:
        extra = extra.replace(risky_text, neutral_text)
    extra = re.sub(r"\s+", " ", extra).strip(" 。、，")
    return re.sub(r"\s*([，,])\s*", r"\1", extra)


def build_leg_focus_action(
    extra_request: str = "",
    has_refs: bool = False,
    *,
    avoid_pose: str = "",
    force_legwear: str = "",
    force_pose: str = "",
    force_camera: str = "",
    force_framing: str = "",
    force_view: str = "",
    avoid_combinations: set[Tuple[str, str]] | None = None,
    preferred_scene: str = "",
) -> str:
    raw_extra = str(extra_request or "")
    requested_view = str(force_view or parse_requested_leg_view(raw_extra))
    if requested_view and requested_view not in LEGFOCUS_VIEW_BY_ID:
        raise LegFocusDataError(f"看看腿机位不存在: {requested_view}")
    angles = parse_requested_leg_angles(raw_extra)
    framing = str(force_framing or "").strip()
    if framing not in {"full_outfit", "leg_detail"}:
        explicit_full = bool(LEG_FULL_OUTFIT_REQUEST_PATTERN.search(raw_extra)) and not bool(
            re.search(r"(?:不|不要|无需|不需要)\s*(?:全身(?!镜)|从头到脚)", raw_extra)
        )
        framing = "full_outfit" if explicit_full else "leg_detail"
    requested_scene = parse_requested_leg_scene(raw_extra)
    excluded_scenes = parse_excluded_leg_scenes(raw_extra)
    explicit_scene = bool(requested_scene)
    if not requested_scene and preferred_scene:
        requested_scene = parse_requested_leg_scene(preferred_scene)
    requested_camera = str(force_camera or "").strip()
    if requested_camera not in {"selfie", "third"}:
        requested_camera = parse_requested_leg_camera(raw_extra)
    # A self-view camera cannot include its owner's head-to-toe body.
    if framing == "full_outfit":
        if requested_camera == "selfie":
            raise LegFocusDataError("看看腿的第一人称自视不能拍到本人从头到脚，请选择第三人称全身或第一人称腿部构图")
        requested_camera = "third"
    excluded_cameras = parse_excluded_leg_cameras(raw_extra)
    if requested_camera and requested_camera in excluded_cameras:
        raise LegFocusDataError("看看腿指定的相机人称同时被排除，请选择第一人称或第三人称中的一个")
    requested_pose = _resolve_pose_id(force_pose) or _resolve_pose_id(parse_requested_leg_pose(raw_extra))
    excluded_poses = parse_excluded_leg_poses(raw_extra)
    if requested_pose and requested_pose in excluded_poses:
        raise LegFocusDataError("看看腿指定的姿势同时被排除，请选择其他姿势")
    avoid_id = _resolve_pose_id(avoid_pose) or str(avoid_pose or "")

    pose_options = [LEGFOCUS_POSE_BY_ID[requested_pose]] if requested_pose else LEGFOCUS_POSE_POOL
    selection_angles = {
        **angles,
        "excluded_scenes": excluded_scenes,
        "excluded_cameras": excluded_cameras,
    }

    def candidates(scene_hint: str) -> Dict[str, List[Tuple[Dict[str, Any], str, Dict[str, Any]]]]:
        filtered_scene_hint = " ".join(
            scene_id for scene_id in scene_hint.split() if scene_id not in excluded_scenes
        )
        if scene_hint.strip() and not filtered_scene_hint:
            return {}
        return {
            pose["id"]: combinations for pose in pose_options
            if pose["id"] not in excluded_poses
            if (combinations := compatible_leg_compositions(
                pose, camera=requested_camera, scene_ids=filtered_scene_hint,
                view_id=requested_view, framing=framing, angles=selection_angles,
            ))
        }

    eligible = candidates(requested_scene)
    if not eligible and requested_scene and not explicit_scene:
        eligible = candidates("")
    if not eligible:
        raise LegFocusDataError("看看腿指定的姿势、场景或视角不兼容，请调整其中一项；不会强行改变支撑动作或相机人称")
    unused = {
        pose_id: options for pose_id, combinations in eligible.items()
        if (options := [item for item in combinations if (pose_id, item[2]["id"]) not in (avoid_combinations or set())])
    }
    eligible = unused or eligible
    pose_choices = [LEGFOCUS_POSE_BY_ID[pose_id] for pose_id in eligible]
    if not requested_pose:
        pose_choices = [pose for pose in pose_choices if pose["id"] != avoid_id] or pose_choices
    pose = _weighted_pose_choice(pose_choices)
    pose_id = pose["id"]
    combinations = eligible[pose_id]
    scene_ids = list(dict.fromkeys(item[0]["id"] for item in combinations))
    scene = LEGFOCUS_SCENE_BY_ID[random.choice(scene_ids)]
    combinations = [item for item in combinations if item[0]["id"] == scene["id"]]
    view_ids = list(dict.fromkeys(item[2]["id"] for item in combinations))
    view = random.choices(
        [LEGFOCUS_VIEW_BY_ID[view_id] for view_id in view_ids],
        weights=[LEGFOCUS_VIEW_BY_ID[view_id]["weight"] for view_id in view_ids], k=1,
    )[0]
    support = next(item[1] for item in combinations if item[2]["id"] == view["id"])
    camera_kind = view["camera"]

    excluded_wears = parse_excluded_legwear(raw_extra)
    requested_wear = str(force_legwear or "").strip()
    if requested_wear not in LEGWEAR_PROMPTS:
        requested_wear = parse_requested_legwear(raw_extra)
    unhandled_wear = _preserve_unhandled_legwear(raw_extra)
    if unhandled_wear and requested_wear == "用户指定":
        requested_wear = ""
    if requested_wear and requested_wear in excluded_wears:
        raise LegFocusDataError("看看腿的袜装要求同时被排除，请只保留一个袜装条件")
    requested_style = _requested_hosiery_style(raw_extra)
    if requested_style and requested_wear not in {requested_style["wear"], ""}:
        requested_style = {}
    explicit_outfit = has_explicit_leg_outfit(raw_extra)
    if explicit_outfit:
        outfit_source = "custom"
    elif has_refs:
        outfit_source = "reference"
    else:
        outfit_source = "pool"

    detail: Dict[str, Any] = {}
    outfit: Dict[str, Any] = {}
    style: Dict[str, Any] = {}
    if requested_wear:
        wear = requested_wear
        if wear in {"日常裤装", "用户指定"}:
            wear = "用户指定"
    elif outfit_source != "pool" or unhandled_wear:
        wear = "用户指定"
    else:
        detail = _weighted_detail_choice(excluded_wears)
        wear = _DETAIL_TAGS.get(str(detail.get("id") or ""), "用户指定")
    if outfit_source == "pool":
        outfit = pick_leg_focus_outfit(hosiery_family=str(requested_style.get("family") or ""))
        style = requested_style or ({} if unhandled_wear else _pick_hosiery_style(wear, outfit, raw_extra))
        if style and not _hosiery_allowed(style, raw_extra):
            raise LegFocusDataError("看看腿指定的袜装款式同时被排除，请调整袜装要求")
        if wear in {"黑丝", "白丝", "连裤袜"} and not style and not unhandled_wear:
            raise LegFocusDataError("看看腿没有符合本次要求的袜装款式，请放宽袜型限制")

    if outfit_source == "pool":
        outfit_line = str(outfit["prompt"])
        if unhandled_wear:
            outfit_line += f"袜装只按用户明确要求呈现：{unhandled_wear}；不额外随机其他袜装或赤足。"
        elif style:
            outfit_line += str(style["prompt"])
        elif requested_wear:
            outfit_line += LEGWEAR_PROMPTS[requested_wear]
        elif detail:
            outfit_line += str(detail["prompt"])
    else:
        detail_override = bool(requested_wear or unhandled_wear or excluded_wears or LEGWEAR_NEGATION_PATTERN.search(raw_extra))
        preserve_line = LEGFOCUS_PRESERVE_PROMPTS[
            f"{outfit_source}_detail" if detail_override else outfit_source
        ]
        outfit_line = preserve_line
        if requested_wear:
            detail_prompt = str(requested_style.get("prompt") or LEGWEAR_PROMPTS[requested_wear])
            outfit_line += LEGFOCUS_PRESERVE_PROMPTS["requested_detail"].format(detail=detail_prompt)
        elif unhandled_wear:
            outfit_line += LEGFOCUS_PRESERVE_PROMPTS["requested_detail"].format(detail=unhandled_wear) + "。"
    if re.search(r"(?:不穿|不要|不需要|无需|不加|不用|不带)\s*(?:鞋履|鞋子|鞋|靴子|靴)", raw_extra):
        outfit_line += "不添加鞋履；袜装或赤足按本次已锁定的腿部细节执行。"

    # Each layer owns one constraint: crop, camera, action, setting, or clothing.
    framing_lines = [LEGFOCUS_FULL_FRAMING] if framing == "full_outfit" else [
        LEGFOCUS_FRAMING,
        LEGFOCUS_CAMERA_FRAMINGS[camera_kind],
        str(pose["crop_prompts"][camera_kind]),
    ]
    base = "\n".join((
        *framing_lines,
        str(view["prompt"]),
        str(pose["prompt"]),
        LEGFOCUS_SCENE_RULE,
        f"本张室内场景：{scene['prompt']}",
        f"本张姿势支撑：{scene['supports'][support]}。",
        outfit_line,
    ))
    extra = _clean_extra_request(raw_extra)
    if extra:
        base += f"\n用户补充要求优先：{extra}。"

    wear_tag = {
        "日常裤装": "daily",
        "用户指定": "custom",
        "白丝": "white",
        "黑丝": "black",
        "光腿": "bare",
        "光腿神器": "bare",
        "赤足": "barefoot",
        "连裤袜": "pantyhose",
    }.get(wear, "custom")
    return (
        base
        + f" 【legs:outfit】 【frame:{framing if framing == 'full_outfit' else 'leg_detail'}】"
        + f"【姿势池·{pose['title']}】【场景池·{scene['title']}】"
        + f" 【leg_view:{view['id']}】 【leg_scene:{scene['id']}】 【leg_support:{support}】"
        + f" 【cam:{camera_kind}】 【wear:{wear_tag}】 【pose:{pose_id}】 【outfit:{outfit.get('id') or outfit_source}】"
    )


def has_leg_focus_contract(text: str) -> bool:
    raw = str(text or "")
    if not (
        "【legs:outfit】" in raw
        and re.search(r"【cam:(?:selfie|third)】", raw)
        and re.search(r"【wear:(?:daily|custom|white|black|bare|barefoot|pantyhose)】", raw)
        and re.search(r"【frame:(?:full_outfit|leg_detail)】", raw)
        and re.search(r"【pose:[a-z_]+】", raw)
        and re.search(r"【outfit:[a-z_]+】", raw)
        and re.search(r"【leg_view:[a-z_]+】", raw)
    ):
        return False
    pose_id = _resolve_pose_id(re.search(r"【pose:([a-z_]+)】", raw).group(1))
    scene = re.search(r"【leg_scene:([a-z_]+)】", raw)
    support = re.search(r"【leg_support:([a-z_]+)】", raw)
    if not pose_id or not scene or not support:
        return False
    combinations = compatible_leg_compositions(
        LEGFOCUS_POSE_BY_ID[pose_id],
        camera=re.search(r"【cam:(selfie|third)】", raw).group(1),
        view_id=re.search(r"【leg_view:([a-z_]+)】", raw).group(1),
        scene_ids=scene.group(1),
        framing=re.search(r"【frame:(full_outfit|leg_detail)】", raw).group(1),
    )
    return any(selected_support == support.group(1) for _, selected_support, _ in combinations)


def ensure_leg_focus_action(text: str, has_refs: bool = False) -> str:
    raw = str(text or "").strip()
    if not raw or has_leg_focus_contract(raw):
        return raw
    pose_match = re.search(r"【pose:([a-z_]+)】", raw)
    camera_match = re.search(r"【cam:(selfie|third)】", raw)
    wear_match = re.search(r"【wear:(daily|custom|white|black|bare|barefoot|pantyhose)】", raw)
    frame_match = re.search(r"【frame:(full_outfit|leg_detail)】", raw)
    scene_match = re.search(r"【leg_scene:([a-z_]+)】", raw)
    extra = raw
    if "【legs:outfit】" in raw:
        supplement = re.search(r"用户补充要求优先[:：](.*?)(?=【legs:outfit】|$)", raw, re.S)
        extra = supplement.group(1).strip() if supplement else ""
    extra = re.sub(r"【(?:legs|legs_face|leg_view|leg_scene|leg_support|cam|wear|pose|outfit|frame):[a-z_]+】", " ", extra, flags=re.I)
    extra = re.sub(r"/?看看腿", " ", extra, flags=re.I)
    pose_id = _resolve_pose_id(str(pose_match.group(1) if pose_match else ""))
    camera = str(camera_match.group(1) if camera_match else "")
    # An obsolete generated camera tag must not override the current pose rules.
    if "【legs:outfit】" in raw and pose_id and camera and not compatible_leg_compositions(
        LEGFOCUS_POSE_BY_ID[pose_id], camera=camera,
    ):
        camera = ""
    return build_leg_focus_action(
        extra,
        has_refs,
        force_pose=pose_id,
        force_camera=camera,
        force_legwear=parse_requested_legwear(str(wear_match.group(0) if wear_match else "")),
        force_framing=str(frame_match.group(1) if frame_match else ""),
        preferred_scene=str(scene_match.group(1) if scene_match else ""),
    )
