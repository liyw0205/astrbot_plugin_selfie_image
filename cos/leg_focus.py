"""JSON-backed pose and indoor-scene selection for ``看看腿``."""

from __future__ import annotations

import random
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..core.prompt_data import PromptDataError, read_nonempty_list, read_versioned_json


DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "legs" / "compositions.json"


class LegFocusDataError(RuntimeError):
    """Raised when the leg-focus prompt pool is incomplete or incompatible."""


def _load_pool() -> Dict[str, Any]:
    try:
        payload = read_versioned_json(DATA_PATH, "看看腿提示词池")
    except PromptDataError as exc:
        raise LegFocusDataError(str(exc)) from exc
    scenes = read_nonempty_list(payload, "scenes", "看看腿场景池")
    poses = read_nonempty_list(payload, "poses", "看看腿姿势池")
    scene_ids: set[str] = set()
    for index, raw in enumerate(scenes):
        if not isinstance(raw, Mapping):
            raise LegFocusDataError(f"看看腿场景第 {index + 1} 项必须是对象")
        scene_id = str(raw.get("id") or "").strip()
        if not scene_id or scene_id in scene_ids:
            raise LegFocusDataError(f"看看腿场景 id 缺失或重复: {scene_id or index + 1}")
        if not str(raw.get("title") or "").strip() or not str(raw.get("prompt") or "").strip():
            raise LegFocusDataError(f"看看腿场景缺少标题或提示词: {scene_id}")
        scene_ids.add(scene_id)

    pose_ids: set[str] = set()
    for index, raw in enumerate(poses):
        if not isinstance(raw, Mapping):
            raise LegFocusDataError(f"看看腿姿势第 {index + 1} 项必须是对象")
        pose_id = str(raw.get("id") or "").strip()
        if not pose_id or pose_id in pose_ids:
            raise LegFocusDataError(f"看看腿姿势 id 缺失或重复: {pose_id or index + 1}")
        cameras = raw.get("camera_views")
        supported_cameras = set(cameras) if isinstance(cameras, Mapping) else set()
        scene_refs = raw.get("scene_ids")
        if (
            not str(raw.get("title") or "").strip()
            or not str(raw.get("prompt") or "").strip()
            or not isinstance(raw.get("weight"), int)
            or int(raw.get("weight") or 0) < 1
            or not supported_cameras
            or not supported_cameras <= {"selfie", "third"}
            or any(not str(value).strip() for value in cameras.values())
            or not isinstance(scene_refs, list)
            or not scene_refs
            or not set(scene_refs) <= scene_ids
        ):
            raise LegFocusDataError(f"看看腿姿势字段不完整或引用无效: {pose_id}")
        pose_ids.add(pose_id)

    for key in ("framing", "full_framing", "scene_rule", "outfit_rule"):
        if not str(payload.get(key) or "").strip():
            raise LegFocusDataError(f"看看腿提示词池缺少字段: {key}")
    details = read_nonempty_list(payload, "random_leg_details", "看看腿腿部细节池")
    seen_details: set[str] = set()
    for index, raw in enumerate(details):
        if not isinstance(raw, Mapping):
            raise LegFocusDataError(f"看看腿腿部细节第 {index + 1} 项必须是对象")
        detail_id = str(raw.get("id") or "").strip()
        if (
            not detail_id
            or detail_id in seen_details
            or not str(raw.get("title") or "").strip()
            or not str(raw.get("prompt") or "").strip()
            or not isinstance(raw.get("weight"), int)
            or int(raw.get("weight") or 0) < 1
        ):
            raise LegFocusDataError(f"看看腿腿部细节字段不完整或重复: {detail_id or index + 1}")
        seen_details.add(detail_id)
    return dict(payload)


_POOL = _load_pool()
LEGFOCUS_SCENE_POOL: List[Dict[str, Any]] = [dict(item) for item in _POOL["scenes"]]
LEGFOCUS_SCENE_BY_ID = {str(item["id"]): item for item in LEGFOCUS_SCENE_POOL}
LEGFOCUS_POSE_POOL: List[Dict[str, Any]] = [dict(item) for item in _POOL["poses"]]
LEGFOCUS_POSE_BY_ID = {str(item["id"]): item for item in LEGFOCUS_POSE_POOL}
LEGFOCUS_RANDOM_LEG_DETAILS: List[Dict[str, Any]] = [
    dict(item) for item in _POOL["random_leg_details"]
]
LEGFOCUS_FRAMING = str(_POOL["framing"])
LEGFOCUS_FULL_FRAMING = str(_POOL["full_framing"])
LEGFOCUS_SCENE_RULE = str(_POOL["scene_rule"])
LEGFOCUS_OUTFIT_RULE = str(_POOL["outfit_rule"])
LEGFOCUS_WEAR_PROMPTS = dict(_POOL.get("wear_prompts") or {})
LEGFOCUS_WEAR_LABELS = dict(_POOL.get("wear_labels") or {})
LEGFOCUS_STOCKING_FINISHES: List[Dict[str, Any]] = [
    dict(item) for item in (_POOL.get("hosiery_finishes") or []) if isinstance(item, Mapping)
]

LEGFOCUS_POSE_IDS = frozenset(LEGFOCUS_POSE_BY_ID)
LEGFOCUS_EXPLICIT_ONLY_POSE_IDS = frozenset()
LEGFOCUS_SEATED_POSE_IDS = LEGFOCUS_POSE_IDS
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
    "kneel": "rug_side_fold",
    "kneel_crop": "rug_side_fold",
    "side_lie": "bed_side_recline",
    "side_lie_crop": "bed_side_recline",
    "hug_knee": "bed_one_knee_up",
    "hug_knee_crop": "bed_one_knee_up",
    "cross_leg": "chair_crossed_knee",
    "cross_leg_crop": "chair_crossed_knee",
    "windowsill": "gaming_chair_staggered",
    "windowsill_crop": "gaming_chair_staggered",
    "kneel_up": "rug_side_fold",
    "kneel_front": "rug_side_fold",
    "floor_fold": "rug_back_support",
    "one_knee_fix": "bed_one_knee_up",
    "reclined_knees_crop": "bed_side_recline",
    "floor_knees_up_crop": "rug_side_fold",
    "desk_sit_crop": "chair_crossed_knee",
    "bed_supine_crop": "bed_side_recline",
    "sofa_front_crop": "sofa_ankle_cross",
    "chair_side_crop": "chair_crossed_knee",
    "sofa_cross_crop": "sofa_side_fold",
    "floor_knees_crop": "rug_side_fold",
    "sofa_occlusion_crop": "sofa_forward_leg",
    "stool_edge_crop": "bed_edge_diagonal",
    "floor_side_kneel_crop": "rug_back_support",
    "seat_knees_cross_crop": "sofa_ankle_cross",
    "floor_topdown_cross_crop": "rug_back_support",
    "first_person_topdown_legs_crop": "bed_edge_diagonal",
    "staggered_stair_step_crop": "bed_one_knee_up",
    "cross_step_snapshot_crop": "gaming_chair_staggered",
    "turn_and_settle_crop": "chair_crossed_knee",
    "bench_side_sit_crop": "gaming_chair_staggered",
    "toe_touch_step_crop": "bed_one_knee_up",
    "back_staggered_step_crop": "sofa_side_fold",
    "knees_forward_sit_crop": "bed_edge_diagonal",
    "one_knee_bent_sit_crop": "bed_one_knee_up",
    "side_folded_sit_crop": "sofa_side_fold",
    "reclined_bent_knees_crop": "bed_side_recline",
    "one_leg_extended_sit_crop": "sofa_forward_leg",
    "heels_folded_sit_crop": "rug_side_fold",
    "edge_dangle_sit_crop": "bed_edge_diagonal",
    "crossed_ankles_sit_crop": "sofa_ankle_cross",
    "crossed_knee_sit_crop": "chair_crossed_knee",
    "over_leg_sit_crop": "chair_crossed_knee",
    "supported_legs_forward_crop": "rug_back_support",
    "relaxed_knees_apart_sit_crop": "sofa_ankle_cross",
}

LEGFOCUS_CAMERA_WEIGHTS: Dict[str, Tuple[Tuple[str, int], ...]] = {
    str(item["id"]): tuple((str(camera), 1) for camera in item["camera_views"])
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
_DETAIL_TAGS = {
    "black_tights": "黑丝",
    "white_tights": "白丝",
    "bare_legs": "光腿",
    "barefoot": "赤足",
}
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

# The former complete outfit pool is intentionally empty: leg focus no longer
# invents bottoms, shoes, or a replacement costume.
LEGFOCUS_OUTFIT_POOL: List[Dict[str, str]] = []
LEGFOCUS_RISKY_EXTRA_REPLACEMENTS = (
    ("掀衣摆", "整理衣摆"),
    ("晒腿", "记录穿搭"),
    ("主要看腿形", "重点看服装版型"),
    ("看腿形", "看服装版型"),
)

LEGWEAR_REQUEST_PATTERN = re.compile(
    r"(?:光腿神器|光腿|白丝|黑丝|丝袜|短袜|堆堆袜|过膝袜|长筒袜|肉色丝袜|肉丝|连裤袜|长袜|赤足|裸足|光脚)"
)
LEGWEAR_NEGATION_PATTERN = re.compile(
    r"(?:不穿|不要|不需要|无需|不必|不加|不搭配|不用|不带|没有|没穿)\s*(?:任何)?\s*"
    r"(?:袜子|袜装|袜|鞋履|鞋子|鞋|靴子|靴)"
    r"(?:\s*(?:和|与|或|、|及)\s*(?:袜子|袜装|袜|鞋履|鞋子|鞋|靴子|靴))*"
)
LEG_DETAIL_REQUEST_PATTERN = re.compile(
    r"(?:下半身特写|腿部特写|下装特写|局部特写|腰部以下|只拍腿|只拍下装|不拍上半身|上半身不入镜|不露脸|不露头|近距离拍腿)"
)
LEG_FACE_REQUEST_PATTERN = re.compile(r"(?:露脸|露头|带脸|看脸|面部入镜|表情|看向镜头|微笑|wink|眨眼)", re.I)
LEG_FULL_OUTFIT_REQUEST_PATTERN = re.compile(
    r"(?:全身|从头到脚|头部到鞋履|全身入镜|全身构图|full[- ]?body)", re.I
)
LEGFOCUS_OUTFIT_REQUEST_PATTERN = re.compile(r"(?:裙|百褶|A字|半身|裤|鞋|靴|平底|乐福|玛丽珍|帆布鞋|凉鞋)")
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


def pick_stocking_finish() -> str:
    if not LEGFOCUS_STOCKING_FINISHES:
        return ""
    pool = [
        str(item.get("prompt") or "")
        for item in LEGFOCUS_STOCKING_FINISHES
        for _ in range(max(1, int(item.get("weight") or 1)))
        if str(item.get("prompt") or "").strip()
    ]
    return random.choice(pool) if pool else ""


def _has_positive_term(text: str, term: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    start = 0
    while True:
        index = compact.find(term, start)
        if index < 0:
            return False
        prefix = compact[max(0, index - 6):index]
        if not re.search(r"(?:不要|不在|不去|不选|避免|禁止|别)(?:再|去|在|用|选择|采用)?$", prefix):
            return True
        start = index + len(term)


def parse_requested_leg_scene(text: str) -> str:
    raw = re.sub(r"场景不变|背景不变|保持场景|保持背景", "", str(text or ""))
    matches: List[str] = []
    for scene in LEGFOCUS_SCENE_POOL:
        terms = (str(scene["id"]), str(scene["title"]), *(scene.get("aliases") or []))
        if any(_has_positive_term(raw, str(term)) for term in terms if str(term).strip()):
            matches.append(str(scene["id"]))
    return " ".join(dict.fromkeys(matches))


def _resolve_pose_id(pose_id: str) -> str:
    value = str(pose_id or "").strip()
    if value in LEGFOCUS_POSE_BY_ID:
        return value
    return LEGFOCUS_POSE_ALIASES.get(value, "")


def _leg_pose_compatible_with_camera(pose: Dict[str, Any], camera: str) -> bool:
    return not camera or camera in (pose.get("camera_views") or {})


def _leg_pose_compatible_with_scene(pose: Dict[str, Any], scene: str) -> bool:
    requested_scenes = set(str(scene or "").split())
    return not requested_scenes or bool(requested_scenes & set(pose.get("scene_ids") or []))


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
        if str(item["id"]) != avoid_id
        and _leg_pose_compatible_with_camera(item, camera_kind)
        and _leg_pose_compatible_with_scene(item, scene_hint)
    ]
    if not pool:
        pool = [
            item for item in LEGFOCUS_POSE_POOL
            if _leg_pose_compatible_with_camera(item, camera_kind)
        ]
    if not pool:
        pool = list(LEGFOCUS_POSE_POOL)
    return _weighted_pose_choice(pool)


def pick_leg_focus_outfit(*, legwear: str = "", avoid_id: str = "") -> Dict[str, str]:
    del legwear, avoid_id
    return {}


def get_leg_focus_outfit(outfit_id: str) -> Dict[str, str]:
    del outfit_id
    return {}


def has_explicit_leg_outfit(text: str) -> bool:
    cleaned = LEGWEAR_NEGATION_PATTERN.sub(" ", str(text or ""))
    cleaned = LEGWEAR_REQUEST_PATTERN.sub(" ", cleaned)
    cleaned = re.sub(r"(?:参考图中的)?鞋袜搭配|鞋袜", " ", cleaned)
    return bool(
        LEGFOCUS_OUTFIT_REQUEST_PATTERN.search(cleaned)
        or LEGFOCUS_COMPLETE_OUTFIT_REQUEST_PATTERN.search(cleaned)
    )


def parse_requested_leg_camera(text: str) -> str:
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    if any(key in compact for key in (
        "第三人称", "第三人称视角", "他拍", "别人拍", "朋友拍", "摄影师拍",
        "路人视角", "不要自拍", "非自拍", "notselfie", "thirdperson",
    )):
        return "third"
    if any(key in compact for key in (
        "第一人称", "第一人称视角", "第一视角", "主观视角", "手机自拍", "自拍", "自己拍", "selfie",
    )):
        return "selfie"
    return ""


def parse_requested_leg_pose(text: str) -> str:
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    for pose in LEGFOCUS_POSE_POOL:
        terms = (str(pose["id"]), str(pose["title"]), *(pose.get("aliases") or []))
        if any(re.sub(r"\s+", "", str(term)).lower() in compact for term in terms if str(term).strip()):
            return str(pose["id"])
    if any(key in compact for key in ("第一人称", "第一视角", "主观视角", "自己视线")) and any(
        key in compact for key in ("向下看", "往下看", "镜头朝下", "看向腿", "看腿", "俯视")
    ):
        return "bed_edge_diagonal"
    if any(key in compact for key in ("俯拍", "正上方", "顶拍", "topdown", "top-down")):
        return "rug_back_support"
    return ""


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
    explicit_values = ("白丝", "黑丝", "光腿神器", "光腿", "赤足", "连裤袜", "丝袜", "光脚", "裸足")
    for value in explicit_values:
        if value in LEGWEAR_NEGATION_PATTERN.sub(" ", raw):
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


def _weighted_detail_choice() -> Dict[str, Any]:
    return dict(random.choices(
        LEGFOCUS_RANDOM_LEG_DETAILS,
        weights=[int(item["weight"]) for item in LEGFOCUS_RANDOM_LEG_DETAILS],
        k=1,
    )[0])


def _clean_extra_request(extra_request: str) -> str:
    extra = re.sub(r"/?看看腿", " ", str(extra_request or ""), flags=re.I)
    extra = re.sub(r"【(?:legs|legs_face|cam|wear|pose|outfit|frame):[a-z_]+】", " ", extra, flags=re.I)
    extra = re.sub(
        r"(?:第一人称(?:视角)?|第三人称(?:视角)?|手机自拍|自拍|自己拍|他拍|别人拍|朋友拍|摄影师拍|俯拍|正上方|顶拍|top[- ]?down)",
        " ", extra, flags=re.I,
    )
    extra = LEGWEAR_REQUEST_PATTERN.sub(" ", extra)
    for pose in LEGFOCUS_POSE_POOL:
        for term in (str(pose["title"]), *(pose.get("aliases") or [])):
            if term:
                extra = extra.replace(str(term), " ")
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
    preferred_scene: str = "",
    scheduled_outfit: str = "",
) -> str:
    raw_extra = str(extra_request or "")
    requested_scene = parse_requested_leg_scene(raw_extra)
    explicit_scene = bool(requested_scene)
    if not requested_scene and preferred_scene:
        requested_scene = parse_requested_leg_scene(preferred_scene)
    requested_camera = str(force_camera or "").strip()
    if requested_camera not in {"selfie", "third"}:
        requested_camera = parse_requested_leg_camera(raw_extra)
    requested_pose = _resolve_pose_id(force_pose) or _resolve_pose_id(parse_requested_leg_pose(raw_extra))
    avoid_id = _resolve_pose_id(avoid_pose) or str(avoid_pose or "")

    pose = LEGFOCUS_POSE_BY_ID.get(requested_pose or "")
    if pose and not _leg_pose_compatible_with_camera(pose, requested_camera):
        pose = None
    if pose and explicit_scene and not _leg_pose_compatible_with_scene(pose, requested_scene):
        pose = None
    if pose is None:
        pose = pick_leg_focus_pose(
            avoid_id=avoid_id,
            preferred_scene=requested_scene,
            camera_kind=requested_camera,
        )
    pose_id = str(pose["id"])
    camera_views = pose["camera_views"]
    camera_kind = requested_camera if requested_camera in camera_views else random.choice(tuple(camera_views))

    scene_options = [
        LEGFOCUS_SCENE_BY_ID[str(scene_id)]
        for scene_id in pose["scene_ids"]
        if str(scene_id) in LEGFOCUS_SCENE_BY_ID
    ]
    matching_scenes = [item for item in scene_options if str(item["id"]) in set(requested_scene.split())]
    scene = random.choice(matching_scenes or scene_options)

    requested_wear = str(force_legwear or "").strip()
    if requested_wear not in LEGWEAR_PROMPTS:
        requested_wear = parse_requested_legwear(raw_extra)
    explicit_outfit = has_explicit_leg_outfit(raw_extra)
    scheduled_outfit = str(scheduled_outfit or "").strip()
    if explicit_outfit:
        outfit_source = "custom"
    elif scheduled_outfit:
        outfit_source = "scheduled"
    elif has_refs:
        outfit_source = "reference"
    else:
        outfit_source = ""

    detail: Dict[str, Any] = {}
    if requested_wear:
        wear = requested_wear
        if wear in {"日常裤装", "用户指定"}:
            wear = "用户指定"
    elif outfit_source:
        wear = "用户指定"
    else:
        detail = _weighted_detail_choice()
        wear = _DETAIL_TAGS.get(str(detail.get("id") or ""), "用户指定")

    framing = str(force_framing or "").strip()
    if framing not in {"full_outfit", "leg_detail"}:
        explicit_full = bool(LEG_FULL_OUTFIT_REQUEST_PATTERN.search(raw_extra)) and not bool(
            re.search(r"(?:不|不要|无需|不需要)\s*(?:全身|从头到脚)", raw_extra)
        )
        framing = "full_outfit" if explicit_full else "leg_detail"

    outfit_line = ""
    if outfit_source == "scheduled":
        outfit_line = f"今日日程当前事件完整穿搭：{scheduled_outfit}。只改姿势、构图与室内背景，不新增、删减或替换任何服饰部件。"
        if requested_wear:
            outfit_line += f"用户额外指定的腿部细节仅在不破坏该套装结构时执行：{LEGWEAR_PROMPTS.get(requested_wear, requested_wear)}"
    elif outfit_source == "reference":
        outfit_line = "引用图中的现有穿搭是本次服装来源；保留原有上装、下装、连体结构、袜鞋与配饰，只调整姿势和构图。"
        if requested_wear:
            outfit_line += f"用户额外指定的腿部细节仅在与引用图穿搭兼容时执行：{LEGWEAR_PROMPTS.get(requested_wear, requested_wear)}"
    elif outfit_source == "custom":
        outfit_line = "用户本次明确指定的服饰是唯一服装来源；完整保留连体结构、上下装关系、袜鞋与配饰，不自行搭配其他服饰。"
    elif requested_wear:
        outfit_line = (
            "用户明确指定的腿部细节只在现有穿搭兼容时执行："
            + LEGWEAR_PROMPTS.get(requested_wear, requested_wear)
        )
    elif detail:
        outfit_line = "穿搭按人物设定执行，不推测裙装、裤装或连体服；不替换已有鞋履。"
    else:
        outfit_line = "服装按用户文字或人物当前设定执行，不猜测下装类别，不补造鞋袜。"

    framing_line = LEGFOCUS_FULL_FRAMING if framing == "full_outfit" else LEGFOCUS_FRAMING
    camera_line = str(camera_views[camera_kind])
    scene_prompt = str(scene["prompt"])
    base = (
        "成年人物的室内竖屏腿部穿搭照片。"
        + framing_line
        + LEGFOCUS_SCENE_RULE
        + str(pose["prompt"])
        + camera_line
        + f"本张室内场景：{scene_prompt}"
        + LEGFOCUS_OUTFIT_RULE
        + outfit_line
    )
    if detail and not outfit_source:
        base += f"本次随机腿部细节：{detail['prompt']}"
    if has_refs:
        base += "引用图用于保持人物身份和自然体态；服装按本条指定的日程或引用图来源规则执行。"
    extra = _clean_extra_request(raw_extra)
    if extra:
        base += f"用户补充要求优先：{extra}。"

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
        + f" 【cam:{camera_kind}】 【wear:{wear_tag}】 【pose:{pose_id}】 【outfit:{outfit_source or 'random'}】"
    )


def has_leg_focus_contract(text: str) -> bool:
    raw = str(text or "")
    return bool(
        "【legs:outfit】" in raw
        and re.search(r"【cam:(?:selfie|third)】", raw)
        and re.search(r"【wear:(?:daily|custom|white|black|bare|barefoot|pantyhose)】", raw)
        and re.search(r"【frame:(?:full_outfit|leg_detail)】", raw)
        and re.search(r"【pose:[a-z_]+】", raw)
        and re.search(r"【outfit:[a-z_]+】", raw)
    )


def ensure_leg_focus_action(text: str, has_refs: bool = False) -> str:
    raw = str(text or "").strip()
    if not raw or has_leg_focus_contract(raw):
        return raw
    pose_match = re.search(r"【pose:([a-z_]+)】", raw)
    camera_match = re.search(r"【cam:(selfie|third)】", raw)
    wear_match = re.search(r"【wear:(daily|custom|white|black|bare|barefoot|pantyhose)】", raw)
    frame_match = re.search(r"【frame:(full_outfit|leg_detail)】", raw)
    extra = re.sub(r"【(?:legs|legs_face|cam|wear|pose|outfit|frame):[a-z_]+】", " ", raw, flags=re.I)
    extra = re.sub(r"/?看看腿", " ", extra, flags=re.I)
    return build_leg_focus_action(
        extra,
        has_refs,
        force_pose=_resolve_pose_id(str(pose_match.group(1) if pose_match else "")),
        force_camera=str(camera_match.group(1) if camera_match else ""),
        force_legwear=parse_requested_legwear(str(wear_match.group(0) if wear_match else "")),
        force_framing=str(frame_match.group(1) if frame_match else ""),
    )
