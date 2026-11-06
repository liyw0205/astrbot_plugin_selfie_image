"""COS outfit pool, matching, camera selection, and prompt construction."""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Iterable, Mapping
from itertools import product
from typing import Any, Dict, List, Optional

from .cos_looks_loader import (
    COS_JSON_COMBINATIONS,
    COS_JSON_COMPOSITIONS,
    COS_JSON_COMPOSITION_REQUEST_TERMS,
    COS_JSON_CAMERA_REQUEST_TERMS,
    COS_JSON_FACE_RISK_PROMPTS,
    COS_JSON_EXPRESSIONS,
    COS_JSON_FRAMINGS,
    COS_JSON_FRAMING_OPTION_TERMS,
    COS_JSON_FRAMING_OPTION_SUFFIX_TERMS,
    COS_JSON_FRAMING_REQUEST_TERMS,
    COS_JSON_LOOK_SETS,
    COS_JSON_LOOK_THEME_CONFLICTS,
    COS_JSON_LOOK_THEME_MARKERS,
    COS_JSON_MAKEUPS,
    COS_JSON_MULTI_VIEW_COLLAGE,
    COS_JSON_MULTI_VIEW_COLLAGE_TERMS,
    COS_JSON_POSES,
    COS_JSON_POSE_CUSTOM_REQUEST_TERMS,
    COS_JSON_POSE_REQUEST_ALIASES,
    COS_JSON_POSE_CLASSIFICATION_TERMS,
    COS_JSON_POSE_REQUEST_SYNTAX_TERMS,
    COS_JSON_SCENES,
    COS_JSON_SCENE_REQUEST_TERMS,
    COS_JSON_SCENE_REQUEST_SYNTAX_TERMS,
    COS_JSON_STYLES,
    COS_JSON_STYLE_REQUEST_TERMS,
)


# Titles use ``·`` for the main split. Matching accepts a full title or a
# complete title segment and never guesses from an arbitrary substring.
COS_LOOK_SETS: List[Dict[str, Any]] = []

_COS_COMMON_NEGATIVE_PROMPT = (
    "不要额外人物、文字、字幕或水印；禁止动漫插画、赛璐璐、游戏立绘、"
    "3D渲染、塑料皮肤和超现实发光。"
)
_COS_ANIME_NEGATIVE_PROMPT = (
    "不要额外人物、文字、字幕或水印；禁止真人照片质感、写实摄影、真人皮肤纹理、"
    "3D渲染、塑料皮肤和超现实发光。"
)
_COS_AUTO_REFERENCE_NEGATIVE_PROMPT = "不要额外人物、文字、字幕或水印。"
_COS_COMMON_NEGATIVE_VARIANTS = (
    "第二个人", "第二人物", "品牌水印", "字幕", "可读文字",
    "额外人物", "文字", "字幕", "水印", "动漫插画", "赛璐璐",
    "游戏立绘", "真人照片质感", "写实摄影", "真人皮肤纹理",
    "3D渲染", "塑料皮肤", "超现实发光",
)
_COS_NEGATIVE_MARKERS = (
    "不要", "禁止", "不出现", "不携带", "不添加", "不自动出现", "不应出现",
)


def _remove_common_negative_terms(clause: str) -> str:
    """Remove shared exclusions from one negative sentence."""
    updated = clause
    for phrase in _COS_COMMON_NEGATIVE_VARIANTS:
        updated = re.sub(
            rf"{re.escape(phrase)}\s*(?:、|，|,|和|或)?",
            "",
            updated,
        )
    updated = re.sub(r"([、，,])\s*(?=[。；]|$)", "", updated)
    updated = re.sub(
        rf"(?:{'|'.join(map(re.escape, _COS_NEGATIVE_MARKERS))})"
        r"\s*(?:、|，|,|和|或|\s)*(?=[。；]|$)",
        "",
        updated,
    )
    return updated


def normalize_cos_prompt_negatives(
    prompt: str,
    appearance_type: str = "auto",
    *,
    preserve_reference_medium: bool = False,
) -> str:
    """Normalize shared COS exclusions for the selected visual medium."""
    kind = str(appearance_type or "").strip().lower()
    anime_medium = kind in {"anime", "cartoon", "2d", "动漫", "动画", "二次元"}
    reference_medium = kind == "auto" and preserve_reference_medium
    text = str(prompt or "").strip()
    if not text:
        if anime_medium:
            return _COS_ANIME_NEGATIVE_PROMPT
        if reference_medium:
            return _COS_AUTO_REFERENCE_NEGATIVE_PROMPT
        return _COS_COMMON_NEGATIVE_PROMPT
    chunks = re.split(r"([。；])", text)
    normalized: List[str] = []
    for chunk in chunks:
        if any(marker in chunk for marker in _COS_NEGATIVE_MARKERS):
            chunk = _remove_common_negative_terms(chunk)
        normalized.append(chunk)
    text = "".join(normalized)
    text = re.sub(r"[、，,]\s*([。；])", r"\1", text)
    text = re.sub(r"[。；]\s*[；，,]\s*[。；]", "。", text)
    text = re.sub(r"[；，,]\s*[。；]", "。", text)
    text = re.sub(r"[。；]\s*[。；]", "。", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if anime_medium:
        shared_negative = _COS_ANIME_NEGATIVE_PROMPT
    elif reference_medium:
        shared_negative = _COS_AUTO_REFERENCE_NEGATIVE_PROMPT
    else:
        shared_negative = _COS_COMMON_NEGATIVE_PROMPT
    if shared_negative not in text:
        text = text.rstrip("。； ") + "。" + shared_negative
    return text


def adapt_cos_prompt_for_appearance(prompt: str, appearance_type: str = "auto") -> str:
    """Treat pool cosplayer wording as role semantics, not a forced medium."""
    text = str(prompt or "")
    anime_kinds = {"anime", "cartoon", "2d", "动漫", "动画", "二次元"}
    if str(appearance_type or "").strip().lower() in anime_kinds:
        text = re.sub(r"真人\s*COSER", "动漫风格 COS 人物", text, flags=re.I)
    return text


def is_special_cos_look(item_or_id: Any) -> bool:
    """Return whether a COS record owns a complete fixed staging contract."""
    if isinstance(item_or_id, Mapping):
        item_id = str(item_or_id.get("id") or "").strip()
        mode = str(
            item_or_id.get("mode")
            or item_or_id.get("cos_mode")
            or item_or_id.get("staging_mode")
            or ""
        ).strip().lower()
        if item_or_id.get("special") is True or mode in {"special", "fixed", "standalone"}:
            return True
    else:
        item_id = str(item_or_id or "").strip()
    return item_id in COS_SPECIAL_LOOK_CONFIG


def _apply_cos_negative_normalization() -> None:
    for item in COS_LOOK_SETS:
        if is_special_cos_look(item):
            continue
        item["prompt"] = normalize_cos_prompt_negatives(item.get("prompt", ""))


from .cos_looks_special import (
    COS_SPECIAL_LOOK_CONFIG,
    COS_SPECIAL_LOOK_SETS,
)

_apply_cos_negative_normalization()

# The catalog is data-owned by ``cos/cos_looks/*.json``.
_json_prompt_by_id = {
    str(item.get("id") or ""): str(item.get("prompt") or "")
    for item in COS_JSON_LOOK_SETS
}
COS_LOOK_SETS[:] = [
    {
        key: item[key]
        for key in ("id", "title", "cos_type", "prompt")
        if key in item
    }
    for item in COS_JSON_LOOK_SETS
]


# Identity-layer locks are audited separately from costume details. Hair, eye
# color, makeup, masks, and head ornaments intentionally remain allowed.
COS_IDENTITY_RISK_PATTERNS = (
    ("face_shape", re.compile(r"(?:固定|指定|保持|改成|设为)[^。；，,]{0,20}(?:脸型|脸宽)|(?:圆脸|方脸|瓜子脸|鹅蛋脸)")),
    ("facial_proportions", re.compile(r"(?:固定|指定|保持|改成|设为)[^。；，,]{0,20}(?:五官比例|五官结构|眼裂比例|嘴唇轮廓)")),
    ("skin_tone", re.compile(r"(?:固定|指定|保持|改成|设为)[^。；，,]{0,20}(?:肤色|皮肤颜色)|(?:深棕色皮肤|浅棕色皮肤|黑色皮肤|白色皮肤)")),
    ("character_face", re.compile(r"(?:严格按|按照|使用)[^。；，,]{0,20}(?:角色原画的脸|角色的脸|角色脸|另一张脸)")),
)


def audit_cos_look_catalog(items: Optional[List[Mapping[str, Any]]] = None) -> List[Dict[str, str]]:
    """Report identity-layer hard-coding in built-in COS descriptions."""
    findings: List[Dict[str, str]] = []
    for item in items if items is not None else COS_LOOK_SETS:
        if not isinstance(item, Mapping):
            continue
        look_id = str(item.get("id") or "").strip()
        prompt = str(item.get("prompt") or "")
        for risk, pattern in COS_IDENTITY_RISK_PATTERNS:
            match = pattern.search(prompt)
            if not match:
                continue
            findings.append(
                {
                    "id": look_id,
                    "risk": risk,
                    "match": match.group(0),
                    "strategy": "move_to_identity_contract_or_make_user-explicit",
                }
            )
    return findings


# Shared pose prompts and metadata are authored in the JSON catalog.
COS_RANDOM_POSE_CLASSES: Dict[str, Dict[str, Any]] = {
    pose_id: dict(pose) for pose_id, pose in COS_JSON_POSES.items()
}
COS_EXPLICIT_ONLY_POSE_IDS = {
    pose_id for pose_id, pose in COS_RANDOM_POSE_CLASSES.items() if pose.get("explicit_only")
}

COS_EXPLICIT_ONLY_FRAMING_IDS = {
    framing_id
    for framing_id, framing in COS_JSON_FRAMINGS.items()
    if framing.get("explicit_only")
}
COS_MULTI_VIEW_COLLAGE_ID = str(COS_JSON_MULTI_VIEW_COLLAGE["id"])
COS_MULTI_VIEW_COLLAGE_PANEL_COUNT = int(COS_JSON_MULTI_VIEW_COLLAGE["panel_count"])
COS_MULTI_VIEW_COLLAGE_LABELS = tuple(COS_JSON_MULTI_VIEW_COLLAGE["panel_labels"])
COS_MULTI_VIEW_COLLAGE_TERMS = COS_JSON_MULTI_VIEW_COLLAGE_TERMS

# Scene prompts, style families, tags, and pose compatibility live in JSON.
COS_RANDOM_SCENE_CLASSES: Dict[str, Dict[str, Any]] = {
    scene_id: dict(scene) for scene_id, scene in COS_JSON_SCENES.items()
}
COS_SCENE_THEME_TAGS: Dict[str, frozenset[str]] = {
    scene_id: frozenset(scene.get("tags") or ())
    for scene_id, scene in COS_RANDOM_SCENE_CLASSES.items()
}

COS_LOOK_THEME_MARKERS: Dict[str, tuple[str, ...]] = {
    theme_id: tuple(terms) for theme_id, terms in COS_JSON_LOOK_THEME_MARKERS.items()
}
COS_LOOK_SCENE_THEME_CONFLICTS = {
    theme_id: frozenset(tags) for theme_id, tags in COS_JSON_LOOK_THEME_CONFLICTS.items()
}

COS_SUBJECT_CLARITY_PROMPT = (
    "人物面部、身体轮廓、发型边缘和服装关键结构是画面最高优先级，曝光自然、五官轮廓可辨；"
    "背景、远处陈设和服装细小纹理允许随真实景深自然柔化，胶片颗粒、色调或柔光只作用于氛围；"
    "不要把人物主体处理成整体雾化、磨皮、低对比失焦或运动模糊，保留少量生活抓拍的不完美。"
)

COS_SUBJECT_CLARITY_COVERED_EYES_PROMPT = (
    "人物面部是画面最高优先级，鼻梁、嘴唇、下颌、眼带边缘、发丝轮廓与服装纹理清晰对焦，曝光自然；"
    "眼带保持连续、平整、边缘清楚，不因柔光、景深或颗粒出现透明、破洞或重影；"
    "胶片颗粒、色调、柔光或景深效果只作用于氛围与背景，不得抹平或模糊人物主体。"
)

COS_EYE_MODE_BY_LOOK_ID: Dict[str, str] = {
    # These skins use Ning Hongye's opaque red blindfold as a defining costume
    # element. Keep the eye-covering rule separate from generic face clarity.
    "ninghongye_plain_white_snake": "full_cover",
    "ninghongye_dragon_path_baizhi": "full_cover",
    "ninghongye_underworld_goddess": "full_cover",
    "ninghongye_cloud_fairy": "full_cover",
    "ninghongye_red_hao_new_joy": "full_cover",
    "ninghongye_spider_demon": "full_cover",
    "ninghongye_clear_eye": "visible",
}

COS_FULL_EYE_COVER_PROMPT = (
    "眼部造型锁定为独立的正红色不透明绸缎眼带：横向贴合面部，左端延伸至左太阳穴，"
    "右端延伸至右太阳穴；带体覆盖双眼所在区域与上鼻梁，完整遮住双眼、眼白、眼睑和睫毛；"
    "带体是连续平整的实体布面，无孔洞、无网纱、无透明区域、无眼睛图案，"
    "不是眼妆、发饰、额饰或悬浮装饰；眼带下方不绘制可见眼睛，只保留鼻子、嘴唇和下半脸轮廓。"
)

COS_VISIBLE_EYE_PROMPT = (
    "眼部造型锁定为双眼自然可见，眼白、虹膜、眼睑和睫毛清晰；不佩戴横跨双眼的眼罩或眼带。"
)


def cos_eye_mode(cos_id: str) -> str:
    """Return the explicit eye treatment for a COS look when catalogued."""
    return str(COS_EYE_MODE_BY_LOOK_ID.get(str(cos_id or ""), "unspecified"))


def _cos_subject_clarity_prompt(cos_id: str = "") -> str:
    if cos_eye_mode(cos_id) == "full_cover":
        return COS_SUBJECT_CLARITY_COVERED_EYES_PROMPT
    return COS_SUBJECT_CLARITY_PROMPT


def normalize_cos_eye_contract(prompt: str, cos_id: str = "") -> str:
    """Normalize eye-cover wording without weakening other costume details."""
    text = str(prompt or "").strip()
    mode = cos_eye_mode(cos_id)
    if mode == "visible" and COS_VISIBLE_EYE_PROMPT not in text:
        return f"{text}{COS_VISIBLE_EYE_PROMPT}"
    if mode != "full_cover":
        return text
    old_contract = re.compile(
        r"宁红夜的双眼必须被横向红色绸缎仪式眼罩完整覆盖；.*?"
        r"眼带不是发饰、头巾、透明纱、蕾丝、金属面罩或悬浮装饰。"
    )
    text = old_contract.sub(COS_FULL_EYE_COVER_PROMPT, text, count=1)
    if COS_FULL_EYE_COVER_PROMPT not in text:
        text = f"{COS_FULL_EYE_COVER_PROMPT}{text}"
    return text

COS_SCENE_FACE_RISK_TAGS: Dict[str, frozenset[str]] = {
    scene_id: frozenset(scene.get("face_risk_tags") or ())
    for scene_id, scene in COS_JSON_SCENES.items()
    if scene.get("face_risk_tags")
}

COS_LOOK_FACE_RISK_TAGS: Dict[str, frozenset[str]] = {
    # These are stable catalogue facts, not changes to the costume wording.
    "rezero_echidna_witch_scholar": frozenset({"hat_shadow"}),
    "hi3_fu_hua_red_sash": frozenset({"hair_shadow"}),
    "luna_frost_moon_mirror": frozenset({"reflective_costume"}),
}

COS_FACE_RISK_PROMPTS: Dict[str, str] = dict(COS_JSON_FACE_RISK_PROMPTS)

COS_EXPLICIT_SOFT_FOCUS_TERMS = (
    "面部柔焦", "脸部柔焦", "面部虚化", "脸部虚化", "主体虚化", "人物失焦",
    "故意失焦", "人物运动模糊", "面部运动模糊",
)

COS_POSE_REQUEST_ALIASES = {
    pose_id: tuple(aliases) for pose_id, aliases in COS_JSON_POSE_REQUEST_ALIASES.items()
}
COS_SCENE_REQUEST_TERMS = COS_JSON_SCENE_REQUEST_TERMS


def _as_id_set(values: Any) -> set[str]:
    if isinstance(values, str):
        return {values} if values else set()
    if not isinstance(values, Iterable):
        return set()
    return {str(value).strip() for value in values if str(value).strip()}


def _has_positive_request(text: str, term: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    needle = re.sub(r"\s+", "", str(term or "")).lower()
    start = 0
    while needle:
        index = compact.find(needle, start)
        if index < 0:
            return False
        prefix = compact[max(0, index - 6) : index]
        if not any(prefix.endswith(marker) for marker in ("不要", "不需要", "不必", "不能", "不可", "别", "不", "避免", "禁止")):
            return True
        start = index + len(needle)
    return False


def _requested_cos_pose(text: str) -> str:
    raw = re.sub(r"\s+", "", str(text or ""))
    terms = COS_JSON_POSE_CLASSIFICATION_TERMS
    has_edge = any(_has_positive_request(raw, term) for term in terms["edge"])
    has_bend = any(_has_positive_request(raw, term) for term in terms["bend"])
    has_back = any(_has_positive_request(raw, term) for term in terms["back"])
    has_side = any(_has_positive_request(raw, term) for term in terms["side"])
    if has_edge and has_back and has_bend:
        return "rear_edge_curve"
    if has_edge and has_back:
        return "rear_hip_shift"
    if has_edge and has_bend:
        return "side_edge_bend"
    if has_edge and has_side:
        return "side_edge_hip_shift"
    matches = [
        (len(alias), -index, pose_id)
        for index, (pose_id, aliases) in enumerate(COS_POSE_REQUEST_ALIASES.items())
        for alias in aliases
        if _has_positive_request(raw, alias)
    ]
    if matches:
        return max(matches)[2]
    if any(
        not _is_negated_request(raw, match.start())
        for term in COS_JSON_POSE_CUSTOM_REQUEST_TERMS
        for match in re.finditer(re.escape(term), raw)
    ):
        return "user"
    pose_syntax = COS_JSON_POSE_REQUEST_SYNTAX_TERMS
    pose_labels = "|".join(re.escape(term) for term in pose_syntax["labels"])
    pose_assignments = "|".join(re.escape(term) for term in pose_syntax["assignments"])
    if re.search(rf"(?:{pose_labels})(?:{pose_assignments})", raw):
        return "user"
    return ""


def _is_negated_request(text: str, index: int) -> bool:
    prefix = str(text or "")[max(0, index - 6) : index]
    return bool(
        re.search(
            r"(?:不要|不需要|不必|不想|不允许|不能|不会|不可|别|不|避免|禁止|拒绝)(?:再|去|在|用|选择|采用|进行)*$",
            prefix,
        )
    )


def _requested_cos_orientation(text: str) -> str:
    terms = COS_JSON_POSE_CLASSIFICATION_TERMS
    if any(_has_positive_request(text, term) for term in terms["orientation_back"]):
        return "back"
    if any(_has_positive_request(text, term) for term in terms["orientation_side"]):
        return "side"
    if any(_has_positive_request(text, term) for term in terms["orientation_front"]):
        return "front"
    return ""


def _requested_cos_scene(text: str) -> str:
    raw = str(text or "")
    matches = [
        (len(alias), scene_id)
        for scene_id, scene in COS_RANDOM_SCENE_CLASSES.items()
        for alias in scene["aliases"]
        if _has_positive_request(raw, alias)
    ]
    if matches:
        return max(matches)[1]
    if any(_has_positive_request(raw, term) for term in COS_SCENE_REQUEST_TERMS):
        return "user"
    syntax = COS_JSON_SCENE_REQUEST_SYNTAX_TERMS
    labels = "|".join(re.escape(term) for term in syntax["labels"])
    assignments = "|".join(re.escape(term) for term in syntax["assignments"])
    leads = "|".join(re.escape(term) for term in syntax["freeform_leads"])
    locations = "|".join(
        re.escape(term)
        for term in sorted(syntax["freeform_locations"], key=len, reverse=True)
    )
    if re.search(rf"(?:{labels})(?:{assignments})(?!随机)", raw):
        return "user"
    if re.search(rf"(?:{leads})(?!随机)[^，,。；;]{{0,12}}(?:{locations})", raw):
        return "user"
    return ""


def _strip_parsed_cos_request_terms(
    text: str,
    *,
    requested_camera: str = "",
    requested_pose: str = "",
    requested_scene: str = "",
    requested_orientation: str = "",
) -> str:
    """Remove only request words already represented by COS contract tags."""
    cleaned = str(text or "")
    if requested_pose and requested_pose != "user":
        for alias in sorted(COS_POSE_REQUEST_ALIASES.get(requested_pose, ()), key=len, reverse=True):
            if _has_positive_request(text, alias):
                cleaned = re.sub(re.escape(alias), " ", cleaned)
    if requested_scene and requested_scene != "user":
        scene = COS_RANDOM_SCENE_CLASSES.get(requested_scene, {})
        for alias in sorted(scene.get("aliases") or (), key=len, reverse=True):
            if _has_positive_request(text, alias):
                cleaned = re.sub(re.escape(alias), " ", cleaned)
    if requested_orientation:
        orientation_terms = COS_JSON_POSE_CLASSIFICATION_TERMS["orientation_strip"]
        for term in sorted(orientation_terms, key=len, reverse=True):
            if _has_positive_request(text, term):
                cleaned = re.sub(re.escape(term), " ", cleaned)
    if requested_camera:
        camera_terms = tuple(
            term
            for terms in COS_JSON_CAMERA_REQUEST_TERMS.values()
            for term in terms
        )
        for term in sorted(camera_terms, key=len, reverse=True):
            if _has_positive_request(text, term):
                cleaned = re.sub(re.escape(term), " ", cleaned)
    for term in sorted(COS_MULTI_VIEW_COLLAGE_TERMS, key=len, reverse=True):
        if _has_positive_request(text, term):
            cleaned = re.sub(re.escape(term), " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s*([，,、；;])\s*", r"\1", cleaned)
    cleaned = re.sub(r"^[，,、；;\s]+|[，,、；;\s]+$", "", cleaned)
    cleaned = re.sub(r"(?:，|,|、|；|;)\s*(?:。|$)", "。", cleaned)
    return cleaned.strip(" 。，,、；;")

# Framing prompts and compatibility metadata come from the shared catalog.
COS_FRAMING_CLASSES: Dict[str, Dict[str, Any]] = {
    framing_id: dict(framing)
    for framing_id, framing in COS_JSON_FRAMINGS.items()
}
COS_POSE_FRAMING_COMPATIBILITY: Dict[str, tuple[str, ...]] = {
    pose_id: tuple(pose.get("framing_ids") or ())
    for pose_id, pose in COS_RANDOM_POSE_CLASSES.items()
}

# Scene-dependent composition treatments are separate from the camera crop:
# they add depth and visual direction without fighting the selected pose.
COS_RANDOM_COMPOSITION_CLASSES: Dict[str, Dict[str, Any]] = {
    composition_id: dict(composition)
    for composition_id, composition in COS_JSON_COMPOSITIONS.items()
}

COS_FRAMING_SCENE_COMPATIBILITY: Dict[str, tuple[str, ...]] = {
    framing_id: tuple(framing["scene_ids"])
    for framing_id, framing in COS_JSON_FRAMINGS.items()
    if framing.get("scene_ids")
}
COS_FRAMING_CAMERA_COMPATIBILITY: Dict[str, tuple[str, ...]] = {
    framing_id: tuple(framing["camera_ids"])
    for framing_id, framing in COS_JSON_FRAMINGS.items()
    if framing.get("camera_ids")
}
COS_POSE_CAMERA_COMPATIBILITY: Dict[str, tuple[str, ...]] = {
    pose_id: tuple(pose["camera_ids"])
    for pose_id, pose in COS_JSON_POSES.items()
    if pose.get("camera_ids")
}

COS_BODY_CURVE_POSE_IDS = frozenset(
    pose_id
    for pose_id, pose in COS_JSON_POSES.items()
    if "body_curve" in (pose.get("traits") or ())
)
COS_BODY_CURVE_SUBJECT_PROMPT = (
    "人物身份和 COS 服装仍以形象图或用户引用图为准；本次构图优先保证支撑关系、"
    "侧身或背身轮廓、腰胯曲线和服装侧后方剪裁，面部只在自然进入画面时保持清晰，"
    "不为了露正脸改变头部、肩胯或支撑姿势。背景细节适度柔化，不堆叠复杂道具。"
)


def _cos_subject_clarity_prompt_for_pose(cos_id: str, pose_id: str) -> str:
    if pose_id in COS_BODY_CURVE_POSE_IDS:
        return COS_BODY_CURVE_SUBJECT_PROMPT
    return _cos_subject_clarity_prompt(cos_id)

COS_SCENE_POSE_FRAMING_EXCLUSIONS: Dict[tuple[str, str], frozenset[str]] = {}

# Selfie is a valid explicit request for every pose, but some poses are poor
# random defaults because the phone viewpoint cannot document the action
# cleanly.  Keep the restriction in the random picker only.
COS_RANDOM_CAMERA_POSE_IDS: Dict[str, frozenset[str]] = {
    camera: frozenset(
        pose_id for pose_id, pose in COS_JSON_POSES.items()
        if camera in (pose.get("random_camera_ids") or ())
    )
    for camera in ("third", "first", "selfie")
}
COS_RANDOM_SELFIE_POSE_IDS = COS_RANDOM_CAMERA_POSE_IDS["selfie"]
COS_DEFAULT_RANDOM_POSE_IDS = frozenset(
    pose_id for pose_id, pose in COS_JSON_POSES.items()
    if not pose.get("explicit_only")
)
COS_RANDOM_CAMERA_FRAMING_IDS: Dict[str, frozenset[str]] = {
    camera: frozenset(
        framing_id for framing_id, framing in COS_JSON_FRAMINGS.items()
        if camera in (framing.get("random_camera_ids") or ())
    )
    for camera in ("third", "first", "selfie")
}

COS_COMBINATION_PRESETS: Dict[str, Dict[str, Any]] = {
    combination_id: dict(combination)
    for combination_id, combination in COS_JSON_COMBINATIONS.items()
}

COS_FRAMING_TERMS = tuple(dict.fromkeys(
    (*COS_JSON_FRAMING_REQUEST_TERMS, *COS_JSON_COMPOSITION_REQUEST_TERMS)
))
COS_FRAMING_TOKEN = "|".join(
    re.escape(term) for term in sorted(COS_JSON_FRAMING_OPTION_TERMS, key=len, reverse=True)
)
COS_FRAMING_OPTION_RE = re.compile(
    rf"(?P<first>{COS_FRAMING_TOKEN})"
    rf"\s*(?P<join>或|/|至|到)\s*"
    rf"(?P<rest>{COS_FRAMING_TOKEN}(?:\s*(?:或|/|至|到)\s*{COS_FRAMING_TOKEN})*)"
    rf"(?P<suffix>{'|'.join(re.escape(term) for term in sorted(COS_JSON_FRAMING_OPTION_SUFFIX_TERMS, key=len, reverse=True))})?"
)


def resolve_cos_framing_options(text: str) -> tuple[str, List[str]]:
    """Resolve each ambiguous framing chain into one concrete candidate."""
    resolved, selected, _ = _resolve_cos_framing_options(text)
    return resolved, selected


def _resolve_cos_framing_options(
    text: str,
    *,
    avoid_framings: Iterable[str] = (),
) -> tuple[str, List[str], str]:
    resolved = str(text or "")
    matches = list(COS_FRAMING_OPTION_RE.finditer(resolved))[:4]
    if not matches:
        return resolved, [], ""

    chains: List[tuple[List[str], str]] = []
    for match in matches:
        candidates = [match.group("first"), *re.findall(COS_FRAMING_TOKEN, match.group("rest"))]
        chains.append((candidates, match.group("suffix") or ""))

    option_indexes = [range(len(candidates)) for candidates, _ in chains]
    avoided = _as_id_set(avoid_framings)
    indexes = tuple(random.choice(options) for options in option_indexes)
    if _framing_option_id(indexes) in avoided:
        for _ in range(8):
            candidate = tuple(random.choice(options) for options in option_indexes)
            if _framing_option_id(candidate) not in avoided:
                indexes = candidate
                break
        else:
            indexes = next(
                (
                    candidate
                    for candidate in product(*option_indexes)
                    if _framing_option_id(candidate) not in avoided
                ),
                indexes,
            )
    selected = [
        f"{chains[index][0][choice]}{chains[index][1]}"
        for index, choice in enumerate(indexes)
    ]
    option_id = _framing_option_id(indexes)

    parts: List[str] = []
    cursor = 0
    for match, (_, suffix) in zip(matches, chains):
        parts.append(resolved[cursor : match.start()])
        parts.append(suffix)
        cursor = match.end()
    parts.append(resolved[cursor:])
    return "".join(parts), selected, option_id


def _framing_option_id(indexes: Iterable[int]) -> str:
    return "option_" + "_".join(str(index) for index in indexes)


def cos_prompt_has_framing(text: str) -> bool:
    """Return whether text already fixes a crop, camera angle, or framing."""
    compact = re.sub(r"\s+", "", str(text or ""))
    return any(term in compact for term in COS_FRAMING_TERMS)


def _pick_cos_composition(
    *,
    pose_id: str,
    scene_id: str,
    camera: str,
    avoid: Iterable[str] = (),
) -> Dict[str, str]:
    if pose_id == "user" or pose_id in COS_EXPLICIT_ONLY_POSE_IDS or camera != "third":
        return {}

    candidates = _compatible_cos_composition_ids(
        pose_id=pose_id,
        scene_id=scene_id,
        camera=camera,
    )
    available = [item for item in candidates if item not in _as_id_set(avoid)]
    if not available:
        available = candidates
    if not available:
        return {}
    composition_id = random.choice(available)
    selected = COS_RANDOM_COMPOSITION_CLASSES[composition_id]
    return {
        "composition_id": composition_id,
        "title": str(selected.get("title") or ""),
        "prompt": str(selected.get("prompt") or ""),
    }


def _compatible_cos_composition_ids(*, pose_id: str, scene_id: str, camera: str) -> List[str]:
    if pose_id == "user" or pose_id in COS_EXPLICIT_ONLY_POSE_IDS or camera != "third":
        return []
    candidates = []
    for composition_id, item in COS_RANDOM_COMPOSITION_CLASSES.items():
        if item.get("poses") and pose_id not in item["poses"]:
            continue
        if item.get("scenes") and scene_id not in item["scenes"]:
            continue
        if item.get("cameras") and camera not in item["cameras"]:
            continue
        candidates.append(composition_id)
    return candidates


def pick_cos_framing(
    *,
    outfit: str = "",
    extra_request: str = "",
    avoid_framings: Iterable[str] = (),
    avoid_compositions: Iterable[str] = (),
    pose_id: str = "",
    scene_id: str = "",
    camera: str = "third",
    allowed_framings: Iterable[str] = (),
    framing_weights: Optional[Mapping[str, int]] = None,
    multi_view_collage: bool = False,
    forced_framing_id: str = "",
) -> Dict[str, Any]:
    """Pick a concrete framing unless the user's extra request already fixes it."""
    camera = str(camera or "third")
    # Framing text authored inside an outfit is descriptive context, not a lock.
    # Keep it intact so the selected random view can take precedence in the
    # action contract without rewriting costume details.
    resolved_outfit = str(outfit or "")
    resolved_extra, extra_options, option_id = _resolve_cos_framing_options(
        extra_request,
        avoid_framings=avoid_framings,
    )
    options = extra_options
    if pose_id in COS_BODY_CURVE_POSE_IDS or pose_id == "back_to_camera":
        # Body-line poses stay close and directional even when the caller
        # provides an ambiguous framing option; never reintroduce flat,
        # top-down, or distant views for this family.
        unsafe_terms = ("平视", "平拍", "俯拍", "高机位", "高位", "远景", "全景")
        if options:
            options = [
                option for option in options
                if not any(term in option for term in unsafe_terms)
            ]
    if options:
        prompt = "；".join(dict.fromkeys(options))
        return {
            "view_id": option_id,
            "title": prompt,
            "prompt": prompt,
            "outfit_text": resolved_outfit,
            "extra_request_text": resolved_extra,
            "randomized_from_options": True,
        }
    orientation = str(COS_RANDOM_POSE_CLASSES.get(pose_id, {}).get("orientation") or "")
    # Pose and orientation words describe the subject, not a fixed crop. Keep
    # actual framing requests such as "低机位" while removing parsed pose
    # aliases so "扶墙侧身展示腰线" can still choose a compatible close view.
    framing_detection_text = extra_request
    if pose_id != "user":
        framing_detection_text = _strip_parsed_cos_request_terms(
            extra_request,
            requested_pose=pose_id,
            requested_orientation=orientation,
        )
    explicit_framing = cos_prompt_has_framing(framing_detection_text)
    if pose_id in COS_BODY_CURVE_POSE_IDS or pose_id == "back_to_camera":
        unsafe_terms = ("平视", "平拍", "俯拍", "高机位", "高位", "远景", "全景")
        safe_terms = ("仰拍", "低机位", "低角度", "轻仰")
        if explicit_framing and any(term in extra_request for term in unsafe_terms) and not any(
            term in extra_request for term in safe_terms
        ):
            explicit_framing = False
    if explicit_framing:
        return {
            "view_id": "user",
            "title": "",
            "prompt": "",
            "outfit_text": resolved_outfit,
            "extra_request_text": resolved_extra,
            "randomized_from_options": False,
            "explicit": True,
        }
    forced = str(forced_framing_id or "").strip()
    if forced in COS_FRAMING_CLASSES and pose_id != "user":
        forced_allowed = forced not in COS_EXPLICIT_ONLY_FRAMING_IDS
        forced_allowed = forced_allowed and forced in COS_RANDOM_CAMERA_FRAMING_IDS.get(camera, frozenset())
        forced_allowed = forced_allowed and (
            not allowed_framings or forced in _as_id_set(allowed_framings)
        )
        pose_framings = COS_POSE_FRAMING_COMPATIBILITY.get(pose_id)
        forced_allowed = forced_allowed and (not pose_framings or forced in pose_framings)
        forced_allowed = forced_allowed and (
            forced not in COS_FRAMING_SCENE_COMPATIBILITY
            or scene_id in COS_FRAMING_SCENE_COMPATIBILITY[forced]
        )
        forced_allowed = forced_allowed and (
            forced not in COS_FRAMING_CAMERA_COMPATIBILITY
            or camera in COS_FRAMING_CAMERA_COMPATIBILITY[forced]
        )
        forced_allowed = forced_allowed and forced not in COS_SCENE_POSE_FRAMING_EXCLUSIONS.get(
            (scene_id, pose_id), frozenset()
        )
        forced_allowed = forced_allowed and forced not in _as_id_set(avoid_framings)
        forced_allowed = forced_allowed and forced not in _cos_unsafe_framings(scene_id, pose_id, camera)
        if forced_allowed:
            selected = dict(COS_FRAMING_CLASSES[forced])
            selected["view_id"] = forced
            selected["outfit_text"] = resolved_outfit
            selected["extra_request_text"] = resolved_extra
            selected["randomized_from_options"] = False
            selected["explicit"] = False
            selected["combination_locked"] = True
            selected["composition"] = _pick_cos_composition(
                pose_id=pose_id,
                scene_id=scene_id,
                camera=camera,
                avoid=avoid_compositions,
            )
            return selected
    if pose_id == "user":
        return {
            "view_id": "unspecified",
            "title": "",
            "prompt": "",
            "outfit_text": resolved_outfit,
            "extra_request_text": resolved_extra,
            "randomized_from_options": False,
            "explicit": False,
            "composition": {},
        }
    if multi_view_collage and camera == "third":
        return {
            "view_id": COS_MULTI_VIEW_COLLAGE_ID,
            "title": str(COS_JSON_MULTI_VIEW_COLLAGE["title"]),
            "prompt": str(COS_JSON_MULTI_VIEW_COLLAGE["prompt"]),
            "outfit_text": resolved_outfit,
            "extra_request_text": resolved_extra,
            "randomized_from_options": False,
            "explicit": False,
            "composition": {},
        }
    avoided = _as_id_set(avoid_framings)
    look_framings = _as_id_set(allowed_framings)
    random_camera_framings = COS_RANDOM_CAMERA_FRAMING_IDS.get(camera)
    pose_framings = COS_POSE_FRAMING_COMPATIBILITY.get(pose_id)
    orientation = str(COS_RANDOM_POSE_CLASSES.get(pose_id, {}).get("orientation") or "")
    compatible = set(pose_framings) if pose_framings else {
        "back": {"rear_three_quarter", "rear_low_medium", "rear_low_hero", "environmental", "eye_level_full"},
        "side": {"side_profile", "environmental", "eye_level_full", "low_angle_full"},
    }.get(orientation)
    candidates = [
        item for item in COS_FRAMING_CLASSES
        if (
            item not in COS_EXPLICIT_ONLY_FRAMING_IDS
        )
        and (compatible is None or item in compatible)
        and (not look_framings or item in look_framings)
        and (
            random_camera_framings is None
            or item in random_camera_framings
        )
    ]
    if scene_id:
        excluded_framings = COS_SCENE_POSE_FRAMING_EXCLUSIONS.get(
            (scene_id, pose_id), frozenset()
        )
        candidates = [
            item for item in candidates
            if (
                item not in COS_FRAMING_SCENE_COMPATIBILITY
                or scene_id in COS_FRAMING_SCENE_COMPATIBILITY[item]
            )
            and item not in excluded_framings
        ]
    else:
        excluded_framings = frozenset()
        candidates = [item for item in candidates if item not in COS_FRAMING_SCENE_COMPATIBILITY]
    if camera:
        candidates = [
            item for item in candidates
            if item not in COS_FRAMING_CAMERA_COMPATIBILITY
            or camera in COS_FRAMING_CAMERA_COMPATIBILITY[item]
        ]
    unsafe_framings = _cos_unsafe_framings(scene_id, pose_id, camera)
    safe_candidates = [item for item in candidates if item not in unsafe_framings]
    if safe_candidates:
        candidates = safe_candidates
    if not candidates:
        candidates = [
            item for item in COS_FRAMING_CLASSES
            if item not in COS_EXPLICIT_ONLY_FRAMING_IDS
            and (compatible is None or item in compatible)
            and (not look_framings or item in look_framings)
            and (
                random_camera_framings is None
                or item in random_camera_framings
            )
            and (
                item not in COS_FRAMING_CAMERA_COMPATIBILITY
                or not camera
                or camera in COS_FRAMING_CAMERA_COMPATIBILITY[item]
            )
            and (
                item not in COS_FRAMING_SCENE_COMPATIBILITY
                or (scene_id and scene_id in COS_FRAMING_SCENE_COMPATIBILITY[item])
            )
            and item not in excluded_framings
            and item not in unsafe_framings
        ]
    if not candidates:
        return {
            "view_id": "unspecified",
            "title": "",
            "prompt": "",
            "outfit_text": resolved_outfit,
            "extra_request_text": resolved_extra,
            "randomized_from_options": False,
            "explicit": False,
            "composition": {},
        }
    available = [item for item in candidates if item not in avoided]
    if not available:
        # Avoid lists may exhaust the pool, but must not bypass pose/framing safety.
        available = candidates
    selection_pool = available
    weights = framing_weights or {}
    if pose_id == "back_to_camera" and camera == "third":
        # A restrained low angle is useful for a rear outfit view, but keep
        # eye-level and rear three-quarter options in rotation to avoid making
        # every back view look like a forced hero shot.
        selection_pool = [
            item
            for item in available
            for _ in range(max(1, int(weights.get(item, 1))) * (
                3 if item == "rear_low_hero"
                else 2 if item == "rear_low_medium"
                else 1
            ))
        ]
    elif weights:
        selection_pool = [
            item
            for item in available
            for _ in range(max(1, int(weights.get(item, 1))))
        ]
    view_id = random.choice(selection_pool)
    selected = dict(COS_FRAMING_CLASSES[view_id])
    selected["view_id"] = view_id
    selected["outfit_text"] = resolved_outfit
    selected["extra_request_text"] = resolved_extra
    selected["randomized_from_options"] = False
    selected["explicit"] = False
    selected["composition"] = _pick_cos_composition(
        pose_id=pose_id,
        scene_id=scene_id,
        camera=camera,
        avoid=avoid_compositions,
    )
    return selected

def cos_look_compatibility(cos_id: str) -> Dict[str, Any]:
    """Return a look-specific staging profile, falling back to the shared pool."""
    look_id = str(cos_id or "").strip()
    all_poses = list(COS_RANDOM_POSE_CLASSES)
    all_scenes = list(COS_RANDOM_SCENE_CLASSES)
    special = COS_SPECIAL_LOOK_CONFIG.get(look_id)
    if special:
        fixed_camera = str(special.get("camera") or "third").strip()
        fixed_view_ids = [fixed_camera] if fixed_camera in {"selfie", "first", "third"} else []
        return {
            "variation_enabled": False,
            "mode": "special",
            "pose_ids": [],
            "scene_ids": [],
            "view_ids": fixed_view_ids,
            "recommended_pose_ids": [],
            "recommended_scene_ids": [],
            "recommended_view_ids": fixed_view_ids,
            "recommended_framing_ids": [],
            "recommended_framing_weights": {},
            "incompatible_pose_ids": all_poses,
            "incompatible_scene_ids": all_scenes,
            "scene_pose_compatibility": {},
            "pose_framing_compatibility": {
                pose_id: list(framings)
                for pose_id, framings in COS_POSE_FRAMING_COMPATIBILITY.items()
            },
            "framing_scene_compatibility": {
                framing_id: list(scene_ids)
                for framing_id, scene_ids in COS_FRAMING_SCENE_COMPATIBILITY.items()
            },
            "framing_camera_compatibility": {
                framing_id: list(cameras)
                for framing_id, cameras in COS_FRAMING_CAMERA_COMPATIBILITY.items()
            },
            "composition_constraints": {
                composition_id: {
                    **{
                        key: list(item[key])
                        for key in ("poses", "scenes")
                        if item.get(key)
                    },
                    "cameras": list(item.get("cameras") or ("third",)),
                }
                for composition_id, item in COS_RANDOM_COMPOSITION_CLASSES.items()
            },
            "pose_camera_compatibility": {
                pose_id: list(cameras)
                for pose_id, cameras in COS_POSE_CAMERA_COMPATIBILITY.items()
            },
            "random_camera_pose_compatibility": {
                camera: sorted(pose_ids)
                for camera, pose_ids in COS_RANDOM_CAMERA_POSE_IDS.items()
            },
            "random_camera_framing_compatibility": {
                camera: sorted(framing_ids)
                for camera, framing_ids in COS_RANDOM_CAMERA_FRAMING_IDS.items()
            },
            "fixed_reason": str(
                special.get("fixed_reason")
                or "特殊 COS 可参与随机抽取，但保持完整固定成片规则"
            ),
        }
    # Every outfit shares the same reviewed image staging pool.  Costume
    # metadata describes clothing only; it must not silently force a bespoke
    # scene/pose/camera contract or re-introduce old per-look templates.
    override: Dict[str, Any] = {}
    poses = list(all_poses)
    scene_ids = all_scenes
    scenes = [
        item for item in scene_ids
        if item in COS_RANDOM_SCENE_CLASSES
        and any(pose_id in poses for pose_id in _scene_pose_ids(COS_RANDOM_SCENE_CLASSES[item]))
    ]
    views = ["selfie", "first", "third"]
    framings = list(override.get("framing_ids") or ())
    scene_pose_compatibility = {
        scene_id: [pose_id for pose_id in _scene_pose_ids(COS_RANDOM_SCENE_CLASSES[scene_id]) if pose_id in poses]
        for scene_id in scenes
    }
    recommended_poses = [
        pose_id for pose_id in poses
        if pose_id not in COS_EXPLICIT_ONLY_POSE_IDS
        and pose_id in COS_DEFAULT_RANDOM_POSE_IDS
        and any(pose_id in scene_poses for scene_poses in scene_pose_compatibility.values())
    ]
    return {
        "variation_enabled": True,
        "pose_ids": poses,
        "scene_ids": scenes,
        "view_ids": views,
        "recommended_pose_ids": recommended_poses,
        "recommended_scene_ids": list(scenes),
        "recommended_view_ids": list(views),
        "recommended_framing_ids": framings,
        "recommended_framing_weights": dict(override.get("framing_weights") or {}),
        "incompatible_pose_ids": [item for item in all_poses if item not in poses],
        "incompatible_scene_ids": [item for item in all_scenes if item not in scenes],
        "scene_pose_compatibility": scene_pose_compatibility,
        "pose_framing_compatibility": {
            pose_id: list(framings)
            for pose_id, framings in COS_POSE_FRAMING_COMPATIBILITY.items()
        },
        "framing_scene_compatibility": {
            framing_id: list(scene_ids)
            for framing_id, scene_ids in COS_FRAMING_SCENE_COMPATIBILITY.items()
        },
        "framing_camera_compatibility": {
            framing_id: list(cameras)
            for framing_id, cameras in COS_FRAMING_CAMERA_COMPATIBILITY.items()
        },
        "composition_constraints": {
            composition_id: {
                **{
                    key: list(item[key])
                    for key in ("poses", "scenes")
                    if item.get(key)
                },
                "cameras": list(item.get("cameras") or ("third",)),
            }
            for composition_id, item in COS_RANDOM_COMPOSITION_CLASSES.items()
        },
        "pose_camera_compatibility": {
            pose_id: list(cameras)
            for pose_id, cameras in COS_POSE_CAMERA_COMPATIBILITY.items()
        },
        "random_camera_pose_compatibility": {
            camera: sorted(pose_ids)
            for camera, pose_ids in COS_RANDOM_CAMERA_POSE_IDS.items()
        },
        "random_camera_framing_compatibility": {
            camera: sorted(framing_ids)
            for camera, framing_ids in COS_RANDOM_CAMERA_FRAMING_IDS.items()
        },
        "fixed_reason": "",
    }


def get_cos_look_by_id(cos_id: str) -> Dict[str, Any]:
    look_id = str(cos_id or "").strip()
    return next((dict(item) for item in COS_LOOK_SETS if str(item.get("id") or "") == look_id), {})


def _scene_pose_ids(scene: Mapping[str, Any]) -> List[str]:
    return list(dict.fromkeys(scene.get("pose_ids") or []))


def _positive_cos_look_description(cos_id: str) -> str:
    """Return costume wording without negative clauses used for exclusions."""
    look = get_cos_look_by_id(cos_id)
    if not look:
        return ""
    clauses = re.split(r"[。；;\n]", str(look.get("prompt") or ""))
    return " ".join(
        clause for clause in clauses
        if not re.search(r"不要|禁止|避免|不出现|不添加|不带", clause)
    )


def _cos_scene_face_risk_tags(scene_id: str) -> set[str]:
    scene = COS_RANDOM_SCENE_CLASSES.get(str(scene_id or ""), {})
    risk = set(COS_SCENE_FACE_RISK_TAGS.get(str(scene_id or ""), ()))
    scene_tags = set(COS_SCENE_THEME_TAGS.get(str(scene_id or ""), ()))
    if "night" in scene_tags:
        risk.add("low_light")
    if "rain" in scene_tags:
        risk.add("reflective_background")
    if "sunset" in scene_tags and "backlight" not in risk:
        risk.add("backlight")
    return risk


def _cos_look_face_risk_tags(cos_id: str) -> set[str]:
    description = _positive_cos_look_description(cos_id)
    risk = set(COS_LOOK_FACE_RISK_TAGS.get(str(cos_id or ""), ()))
    if re.search(r"宽檐帽|帽檐|大号.*帽|兜帽", description):
        risk.add("hat_shadow")
    if re.search(r"齐刘海|厚刘海|长刘海|碎刘海|刘海", description):
        risk.add("hair_shadow")
    if re.search(r"透明|薄纱|金属|亮缎|反光|镜面|亮片|水晶", description):
        risk.add("reflective_costume")
    return risk


def _cos_face_risk_tags(
    cos_id: str,
    scene_id: str,
    pose_id: str = "",
    camera: str = "",
) -> set[str]:
    risk = _cos_scene_face_risk_tags(scene_id) | _cos_look_face_risk_tags(cos_id)
    if pose_id == "seated_composed" and camera == "selfie" and (
        {"low_light", "glass_reflection", "backlight"} & risk
    ):
        risk.add("selfie_seated_light")
    return risk


def _cos_face_safety_prompt(
    cos_id: str,
    scene_id: str,
    pose_id: str = "",
    camera: str = "",
    *,
    include_base: bool = False,
) -> str:
    risk = _cos_face_risk_tags(cos_id, scene_id, pose_id, camera)
    prompts = [COS_FACE_RISK_PROMPTS[tag] for tag in sorted(risk) if tag in COS_FACE_RISK_PROMPTS]
    if "selfie_seated_light" in risk:
        prompts.append("自拍坐姿优先使用三分之二身或三分之四侧前方构图，手机和手部不遮挡眼睛与眉眼。")
    if not prompts:
        return (
            _cos_subject_clarity_prompt_for_pose(cos_id, pose_id)
            if include_base else ""
        )
    prefix = (
        _cos_subject_clarity_prompt_for_pose(cos_id, pose_id)
        if include_base else ""
    )
    return prefix + "".join(prompts)


def _cos_unsafe_random_pair(cos_id: str, scene_id: str, pose_id: str, camera: str) -> bool:
    """Reject only known random combinations that spend sharpness on effects."""
    risk = _cos_face_risk_tags(cos_id, scene_id, pose_id, camera)
    if camera == "selfie" and pose_id == "seated_composed" and (
        {"low_light", "glass_reflection", "backlight"} & risk
    ):
        return True
    return False


def _cos_unsafe_framings(scene_id: str, pose_id: str, camera: str) -> set[str]:
    risk = _cos_scene_face_risk_tags(scene_id)
    unsafe: set[str] = set()
    if camera == "selfie" and pose_id == "seated_composed":
        if "low_light" in risk:
            unsafe.update({"front_half", "high_angle_three_quarter"})
        if "glass_reflection" in risk or "backlight" in risk:
            unsafe.add("high_angle_three_quarter")
    return unsafe


def _pick_cos_combination(
    cos_id: str,
    *,
    allowed_poses: Iterable[str],
    allowed_scenes: Iterable[str],
    camera: str,
    avoid_poses: Iterable[str] = (),
    avoid_scenes: Iterable[str] = (),
) -> Optional[Dict[str, Any]]:
    """Pick one curated scene/pose bundle without fixing its camera crop."""
    # Anonymous picker fixtures and user-supplied one-off prompts do not have
    # enough costume metadata for a curated bundle; keep them on the atomic
    # scene/pose randomizer.
    if camera != "third" or not get_cos_look_by_id(cos_id):
        return None
    pose_set = set(allowed_poses)
    scene_set = set(allowed_scenes)
    avoided_poses = _as_id_set(avoid_poses)
    avoided_scenes = _as_id_set(avoid_scenes)
    candidates: List[tuple[str, Mapping[str, Any]]] = []
    for combination_id, combination in COS_COMBINATION_PRESETS.items():
        scene_id = str(combination.get("scene_id") or "")
        pose_id = str(combination.get("pose_id") or "")
        if not scene_id or not pose_id:
            continue
        if scene_id not in scene_set or pose_id not in pose_set:
            continue
        if scene_id in avoided_scenes or pose_id in avoided_poses:
            continue
        scene = COS_RANDOM_SCENE_CLASSES.get(scene_id)
        if not scene or scene.get("cos_look_only") or pose_id not in _scene_pose_ids(scene):
            continue
        if pose_id in COS_EXPLICIT_ONLY_POSE_IDS:
            continue
        if _cos_unsafe_random_pair(cos_id, scene_id, pose_id, "third"):
            continue
        candidates.append((combination_id, combination))
    if not candidates:
        return None
    weighted = [
        combination_id
        for combination_id, combination in candidates
        for _ in range(max(1, int(combination.get("weight") or 1)))
    ]
    chosen_id = random.choice(weighted)
    chosen = dict(COS_COMBINATION_PRESETS[chosen_id])
    chosen["combination_id"] = chosen_id
    return chosen


COS_STYLE_REQUEST_TERMS = COS_JSON_STYLE_REQUEST_TERMS


def _requested_cos_style(text: str, *, requested_scene: str = "") -> bool:
    # Scene aliases such as "窗边CCD" contain style words themselves. They
    # select a complete scene contract and must not suppress that contract's
    # own style prompt; only style text left outside the alias is explicit.
    residual = _strip_parsed_cos_request_terms(
        text,
        requested_scene=requested_scene,
    )
    compact = re.sub(r"\s+", "", residual).lower()
    return any(term in compact for term in COS_STYLE_REQUEST_TERMS)


def _requested_cos_style_id(text: str, *, requested_scene: str = "") -> str:
    residual = _strip_parsed_cos_request_terms(
        text,
        requested_scene=requested_scene,
    )
    return _requested_vocab_id(residual, COS_JSON_STYLES)


def _requested_vocab_id(
    text: str,
    vocab: Mapping[str, Mapping[str, Any]],
) -> str:
    raw = str(text or "")
    matches = [
        (len(alias), item_id)
        for item_id, item in vocab.items()
        for alias in item.get("aliases") or ()
        if _has_positive_request(raw, alias)
    ]
    for item_id, item in vocab.items():
        for value in (item_id, item.get("title"), item.get("label")):
            if value and _has_positive_request(raw, str(value)):
                matches.append((len(str(value)), item_id))
    return max(matches)[1] if matches else ""


def _pick_weighted_cos_id(ids: Iterable[str], *, weights: Optional[Mapping[str, Any]] = None) -> str:
    values = [str(value) for value in ids if str(value)]
    if not values:
        return ""
    pool = []
    for value in values:
        try:
            weight = max(1, int((weights or {}).get(value, 1)))
        except (TypeError, ValueError):
            weight = 1
        pool.extend([value] * weight)
    return random.choice(pool)


def _pick_cos_pose(pose_ids: Iterable[str]) -> str:
    """Choose among compatible poses using their catalog weights."""
    values = [str(value) for value in pose_ids if str(value)]
    weights = {
        pose_id: COS_RANDOM_POSE_CLASSES.get(pose_id, {}).get("random_weight", 1)
        for pose_id in values
    }
    return _pick_weighted_cos_id(values, weights=weights)


def _pick_scene_style(scene_id: str, extra_request: str = "") -> tuple[str, str]:
    """Choose one compatible medium variation without changing the scene."""
    scene = COS_RANDOM_SCENE_CLASSES.get(str(scene_id or ""), {})
    if not scene:
        return "", ""
    style_ids = tuple(
        style_id for style_id in (scene.get("style_ids") or ())
        if style_id in COS_JSON_STYLES
    )
    requested_style_id = _requested_cos_style_id(
        extra_request,
        requested_scene=_requested_cos_scene(extra_request),
    )
    if requested_style_id:
        if requested_style_id not in style_ids:
            return "", ""
        style_ids = (requested_style_id,)
    elif _requested_cos_style(extra_request, requested_scene=_requested_cos_scene(extra_request)):
        return "", ""
    if not style_ids:
        return "", str(scene.get("style_prompt") or "")
    style_id = _pick_weighted_cos_id(style_ids)
    style = COS_JSON_STYLES.get(style_id) or {}
    base = str(scene.get("style_prompt") or "").strip()
    variant = str(style.get("prompt") or "").strip()
    label = str(style.get("title") or style.get("label") or style_id).strip()
    context_line = (
        "室内背景保持简单，只保留与姿势有关的墙面、窗边、椅子、床边或少量结构线条，"
        "其余陈设适度景深柔化，避免复杂布景抢走 COS 主体。"
        if "indoor" in COS_SCENE_THEME_TAGS.get(str(scene_id or ""), frozenset())
        else "环境只作为氛围层次，人物仍占据画面主要区域，远处与细小背景适度柔化，不喧宾夺主。"
    )
    prompt = base
    if variant:
        prompt = (
            f"{base}；{context_line}本次在该场景风格族内采用{label}的轻微媒介变化：{variant}"
            if base else f"本次采用{label}的轻微媒介变化：{variant}"
        )
    return style_id, prompt


def _pick_scene_expression(pose_id: str, extra_request: str = "") -> tuple[str, str]:
    requested = _requested_vocab_id(extra_request, COS_JSON_EXPRESSIONS)
    pose = COS_RANDOM_POSE_CLASSES.get(pose_id, {})
    allowed = tuple(pose.get("expression_ids") or COS_JSON_EXPRESSIONS)
    incompatible = set(pose.get("incompatible_expression_ids") or ())
    if requested and requested not in incompatible:
        expression_id = requested
    else:
        expression_id = _pick_weighted_cos_id(
            (item for item in allowed if item in COS_JSON_EXPRESSIONS and item not in incompatible)
        )
    expression = COS_JSON_EXPRESSIONS.get(expression_id) or {}
    return expression_id, str(expression.get("prompt") or "").strip()


def _pick_scene_makeup(scene_id: str, extra_request: str = "") -> tuple[str, str]:
    requested = _requested_vocab_id(extra_request, COS_JSON_MAKEUPS)
    scene = COS_RANDOM_SCENE_CLASSES.get(str(scene_id or ""), {})
    allowed = tuple(
        item for item in (scene.get("makeup_ids") or COS_JSON_MAKEUPS)
        if item in COS_JSON_MAKEUPS
    )
    makeup_id = requested if requested in COS_JSON_MAKEUPS else _pick_weighted_cos_id(allowed)
    makeup = COS_JSON_MAKEUPS.get(makeup_id) or {}
    return makeup_id, str(makeup.get("prompt") or "").strip()


def _poses_for_orientation(pose_ids: Iterable[str], orientation: str) -> List[str]:
    if not orientation:
        return list(pose_ids)
    return [
        pose_id for pose_id in pose_ids
        if not COS_RANDOM_POSE_CLASSES.get(pose_id, {}).get("orientation")
        or COS_RANDOM_POSE_CLASSES[pose_id]["orientation"] == orientation
    ]


def _poses_for_camera(pose_ids: Iterable[str], camera: str) -> List[str]:
    if not camera:
        return list(pose_ids)
    random_allowed = COS_RANDOM_CAMERA_POSE_IDS.get(camera)
    compatible = []
    for pose_id in pose_ids:
        if random_allowed is not None and pose_id not in random_allowed:
            continue
        allowed_cameras = COS_POSE_CAMERA_COMPATIBILITY.get(pose_id)
        if allowed_cameras and camera not in allowed_cameras:
            continue
        compatible.append(pose_id)
    return compatible


def _cos_pose_prompt_for_camera(pose_id: str, camera: str) -> str:
    """Keep first-person action wording in the observer's coordinate system."""
    return str(COS_RANDOM_POSE_CLASSES.get(pose_id, {}).get("prompt") or "")


def _cos_look_theme_tags(cos_id: str) -> set[str]:
    look = get_cos_look_by_id(cos_id)
    if not look:
        return set()
    clauses = re.split(r"[。；;\n]", str(look.get("prompt") or ""))
    positive_prompt = " ".join(
        clause for clause in clauses
        if not re.search(r"不要|禁止|避免|不出现|不添加", clause)
    )
    description = f"{look.get('title') or ''} {positive_prompt}"
    return {
        theme
        for theme, markers in COS_LOOK_THEME_MARKERS.items()
        if any(marker in description for marker in markers)
    }


def _weighted_compatible_scene_ids(scene_ids: Iterable[str], cos_id: str) -> List[str]:
    available = list(dict.fromkeys(scene_ids))
    look_themes = _cos_look_theme_tags(cos_id)
    if not available or not look_themes:
        return available

    compatible = [
        scene_id for scene_id in available
        if all(
            not (COS_LOOK_SCENE_THEME_CONFLICTS.get(look_theme, frozenset()) & COS_SCENE_THEME_TAGS.get(scene_id, frozenset()))
            for look_theme in look_themes
        )
    ]
    if not compatible:
        compatible = available

    preferred = [
        scene_id for scene_id in compatible
        if look_themes & COS_SCENE_THEME_TAGS.get(scene_id, frozenset())
    ]
    other = [scene_id for scene_id in compatible if scene_id not in preferred]
    if not preferred or not other:
        return compatible

    # Keep compatible alternatives in rotation, while making coherent matches
    # the normal outcome rather than an occasional accident.
    repetitions = max(1, (2 * len(other) + len(preferred) - 1) // len(preferred))
    return [*([scene_id for scene_id in preferred for _ in range(repetitions)]), *other]


def _requests_explicit_soft_focus(text: str) -> bool:
    return any(_has_positive_request(text, term) for term in COS_EXPLICIT_SOFT_FOCUS_TERMS)


def requests_cos_multi_view_collage(text: str) -> bool:
    return any(_has_positive_request(text, term) for term in COS_MULTI_VIEW_COLLAGE_TERMS)


def _pick_cos_collage_variation(
    cos_id: str,
    *,
    extra_request: str,
    avoid_poses: Iterable[str],
    avoid_scenes: Iterable[str],
) -> Optional[Dict[str, Any]]:
    profile = cos_look_compatibility(cos_id)
    allowed_poses = set(profile["recommended_pose_ids"])
    allowed_scenes = set(profile["recommended_scene_ids"])
    allowed_framings = _as_id_set(profile.get("recommended_framing_ids"))
    requested_scene = _requested_cos_scene(extra_request)
    if requested_scene == "user":
        return None

    if requested_scene:
        scene_ids = [requested_scene] if requested_scene in COS_RANDOM_SCENE_CLASSES else []
    else:
        scene_ids = list(allowed_scenes)
    if not scene_ids:
        return None

    scene_pose_frames: Dict[str, Dict[str, List[str]]] = {}
    for scene_id in scene_ids:
        scene = COS_RANDOM_SCENE_CLASSES[scene_id]
        if not requested_scene and scene_id not in allowed_scenes:
            continue
        poses = _scene_pose_ids(scene)
        if scene_id in allowed_scenes:
            poses = [pose_id for pose_id in poses if pose_id in allowed_poses]
        else:
            poses = [pose_id for pose_id in poses if pose_id not in COS_EXPLICIT_ONLY_POSE_IDS]
        safe_poses = [
            pose_id for pose_id in poses
            if not _cos_unsafe_random_pair(cos_id, scene_id, pose_id, "third")
        ]
        if safe_poses:
            poses = safe_poses
        compatible: Dict[str, List[str]] = {}
        for pose_id in poses:
            frames = [
                framing_id
                for framing_id in COS_POSE_FRAMING_COMPATIBILITY.get(pose_id, ())
                if framing_id not in COS_EXPLICIT_ONLY_FRAMING_IDS
                and (not allowed_framings or framing_id in allowed_framings)
                and framing_id in COS_RANDOM_CAMERA_FRAMING_IDS["third"]
                and (
                    framing_id not in COS_FRAMING_SCENE_COMPATIBILITY
                    or scene_id in COS_FRAMING_SCENE_COMPATIBILITY[framing_id]
                )
                and (
                    framing_id not in COS_FRAMING_CAMERA_COMPATIBILITY
                    or "third" in COS_FRAMING_CAMERA_COMPATIBILITY[framing_id]
                )
                and framing_id not in COS_SCENE_POSE_FRAMING_EXCLUSIONS.get(
                    (scene_id, pose_id), frozenset()
                )
            ]
            if frames:
                compatible[pose_id] = frames
        if len(compatible) >= 3:
            scene_pose_frames[scene_id] = compatible

    if not scene_pose_frames:
        return None
    avoided_scenes = _as_id_set(avoid_scenes)
    scene_candidates = [scene_id for scene_id in scene_pose_frames if scene_id not in avoided_scenes]
    if not scene_candidates:
        scene_candidates = list(scene_pose_frames)
    if not requested_scene:
        scene_candidates = _weighted_compatible_scene_ids(scene_candidates, cos_id)
    scene_id = random.choice(scene_candidates)
    compatible = scene_pose_frames[scene_id]
    avoided_poses = _as_id_set(avoid_poses)
    pose_candidates = [pose_id for pose_id in compatible if pose_id not in avoided_poses]
    if not pose_candidates:
        pose_candidates = list(compatible)
    main_pose_candidates = [
        pose_id for pose_id in pose_candidates
        if "eye_level_full" in compatible[pose_id]
    ]
    main_pose = random.choice(main_pose_candidates or pose_candidates)
    main_framing_id = (
        "eye_level_full"
        if "eye_level_full" in compatible[main_pose]
        else random.choice(compatible[main_pose])
    )
    panel_views = [(main_pose, main_framing_id)]

    # Use each compatible pose once before adding alternate camera views.
    for pose_id in random.sample(
        [item for item in pose_candidates if item != main_pose],
        len([item for item in pose_candidates if item != main_pose]),
    ):
        if len(panel_views) >= COS_MULTI_VIEW_COLLAGE_PANEL_COUNT:
            break
        panel_views.append((pose_id, random.choice(compatible[pose_id])))

    pose_use_counts = {pose_id: 0 for pose_id in compatible}
    for pose_id, _ in panel_views:
        pose_use_counts[pose_id] += 1
    used_views = set(panel_views)
    while len(panel_views) < COS_MULTI_VIEW_COLLAGE_PANEL_COUNT:
        pose_pool = pose_candidates or list(compatible)
        view_candidates = [
            (pose_id, framing_id)
            for pose_id in pose_pool
            for framing_id in compatible[pose_id]
            if (pose_id, framing_id) not in used_views
        ]
        if not view_candidates:
            view_candidates = [
                (pose_id, framing_id)
                for pose_id, frames in compatible.items()
                for framing_id in frames
                if (pose_id, framing_id) not in used_views
            ]
        if not view_candidates:
            return None
        least_used = min(pose_use_counts[pose_id] for pose_id, _ in view_candidates)
        view_candidates = [
            view for view in view_candidates
            if pose_use_counts[view[0]] == least_used
        ]
        view = random.choice(view_candidates)
        panel_views.append(view)
        used_views.add(view)
        pose_use_counts[view[0]] += 1

    panel_poses = [pose_id for pose_id, _ in panel_views]
    slot_labels = COS_MULTI_VIEW_COLLAGE_LABELS
    def collage_framing_prompt(framing_id: str) -> str:
        # Panel ratios describe individual photos, not the output canvas. Keep
        # numeric ratios out of the collage prompt so the global aspect parser
        # cannot mistake a side panel for the square canvas ratio.
        prompt = str(COS_FRAMING_CLASSES[framing_id]["prompt"])
        return (
            prompt.replace("9:16", "竖幅")
            .replace("4:5", "窄竖幅")
            .replace("16:9", "横幅")
            .replace("1:1", "方形")
        )

    panels = [
        f"{slot_labels[index]}：{collage_framing_prompt(framing_id)}；"
        f"{_cos_pose_prompt_for_camera(pose_id, 'third')}"
        for index, (pose_id, framing_id) in enumerate(panel_views)
    ]

    scene = COS_RANDOM_SCENE_CLASSES[scene_id]
    style_id, scene_style_prompt = _pick_scene_style(scene_id, extra_request)
    expression_id, expression_prompt = _pick_scene_expression(main_pose, extra_request)
    if (
        expression_id == "deep_affection"
        and any(pose_id in {"back_to_camera", "over_shoulder"} for pose_id in panel_poses)
    ):
        # One expression line applies to every panel, including rear-facing poses.
        expression_id, expression_prompt = _pick_scene_expression(
            "over_shoulder", extra_request
        )
    makeup_id, makeup_prompt = _pick_scene_makeup(scene_id, extra_request)
    prompt_parts = []
    prompt_parts.append(
        ("按用户指定场景补充环境细节：" if requested_scene else "本次随机场景优先于套装正文中残留的背景或环境描述：")
        + scene["prompt"]
    )
    if scene_style_prompt:
        prompt_parts.append("本次场景影像风格族保持与环境一致：" + scene_style_prompt)
    if not _requests_explicit_soft_focus(extra_request):
        prompt_parts.append(_cos_subject_clarity_prompt_for_pose(cos_id, main_pose))
    if expression_prompt:
        prompt_parts.append("本次兼容神态：" + expression_prompt)
    if makeup_prompt:
        prompt_parts.append("本次兼容妆造：" + makeup_prompt)
    panel_prompt = (
        "本次随机姿势优先于套装正文中残留的人物姿势描述：采用9:16竖幅画布上的自然错落式七格摄影拼贴，拒绝规整宫格；"
        "中央是一张占据主要高度的竖幅大主图，明显大于其余画格；六张辅助照片分布在主图上下与两侧，"
        "分割线错落，小画格的长宽和面积略有变化，不要等分或等高排列；"
        "画格边界采用自然的不对称斜切与轻微错位，留白宽度可有细微变化，整体仍保持平衡、清晰易读；"
        "总数严格为七张独立照片，不增不减，不合并或拆分画格，不做表格、九宫格或规则卡片墙；"
        "每格都是同一人物、同一套 COS 服装、同一地点和同一影像风格下的独立照片，"
        "每格各自完整、自然地呈现同一位主角，不是多人同框；照片之间不重叠，人物身体、脸、手和服装不得跨格拼接或被边界切断，不做镜像或重复曝光；"
        "各画格的机位和姿势分别为：" + "；".join(panels)
        + "。所有画格保持发型、妆容、服装结构、配饰和人物身份连续一致；"
        "不添加文字、编号、水印、边框标题或额外人物，画面干净清晰。"
    )
    return {
        "pose_id": main_pose,
        "scene_id": scene_id,
        "prompt": "".join(prompt_parts),
        "panel_prompt": panel_prompt,
        "panel_pose_ids": panel_poses,
        "panel_framing_ids": [framing_id for _, framing_id in panel_views],
        "main_framing_id": main_framing_id,
        "style_id": style_id,
        "expression_id": expression_id,
        "makeup_id": makeup_id,
        "style_family": str(scene.get("style_family") or ""),
    }


def pick_cos_variation(
    cos_id: str,
    *,
    avoid_pose: str = "",
    avoid_scene: str = "",
    avoid_framings: Iterable[str] = (),
    avoid_poses: Iterable[str] = (),
    avoid_scenes: Iterable[str] = (),
    extra_request: str = "",
    camera: str = "",
    multi_view_collage: bool = False,
    use_combinations: bool = False,
) -> Dict[str, Any]:
    """Pick a compatible scene and pose, honoring explicit user constraints."""
    profile = cos_look_compatibility(cos_id)
    if not profile["variation_enabled"]:
        return {"pose_id": "", "scene_id": "", "prompt": ""}

    allowed_poses = set(profile["recommended_pose_ids"])
    allowed_scenes = set(profile["recommended_scene_ids"])
    avoided_poses = _as_id_set(avoid_poses) | _as_id_set(avoid_pose)
    avoided_scenes = _as_id_set(avoid_scenes) | _as_id_set(avoid_scene)
    requested_pose = _requested_cos_pose(extra_request)
    requested_scene = _requested_cos_scene(extra_request)
    requested_orientation = _requested_cos_orientation(extra_request)
    requested_style_id = _requested_cos_style_id(
        extra_request,
        requested_scene=requested_scene,
    )
    if requested_style_id and not requested_scene:
        allowed_scenes = {
            scene_id for scene_id in allowed_scenes
            if requested_style_id in (COS_RANDOM_SCENE_CLASSES.get(scene_id, {}).get("style_ids") or ())
        }
    selected_combination: Optional[Dict[str, Any]] = None

    if multi_view_collage and not requested_pose and camera == "third":
        collage = _pick_cos_collage_variation(
            cos_id,
            extra_request=extra_request,
            avoid_poses=avoided_poses,
            avoid_scenes=avoided_scenes,
        )
        if collage:
            return collage

    if (
        use_combinations
        and camera == "third"
        and not multi_view_collage
        and not requested_scene
        and not requested_pose
        and not requested_orientation
        and random.random() < 0.6
    ):
        selected_combination = _pick_cos_combination(
            cos_id,
            allowed_poses=allowed_poses,
            allowed_scenes=allowed_scenes,
            camera=camera,
            avoid_poses=avoided_poses,
            avoid_scenes=avoided_scenes,
        )

    if selected_combination:
        scene_id = str(selected_combination["scene_id"])
        pose_id = str(selected_combination["pose_id"])
    elif requested_scene and requested_pose:
        scene_id, pose_id = requested_scene, requested_pose
    elif requested_scene:
        scene_id = requested_scene
        scene = COS_RANDOM_SCENE_CLASSES.get(scene_id)
        compatible_poses = _scene_pose_ids(scene) if scene else list(profile["recommended_pose_ids"])
        if scene_id in allowed_scenes:
            compatible_poses = [item for item in compatible_poses if item in allowed_poses]
        else:
            compatible_poses = [item for item in compatible_poses if item not in COS_EXPLICIT_ONLY_POSE_IDS]
        if not requested_pose:
            compatible_poses = _poses_for_orientation(compatible_poses, requested_orientation)
            compatible_poses = _poses_for_camera(compatible_poses, camera)
        compatible_poses = [item for item in compatible_poses if item not in COS_EXPLICIT_ONLY_POSE_IDS]
        if not requested_pose:
            safe_poses = [
                item for item in compatible_poses
                if not _cos_unsafe_random_pair(cos_id, scene_id, item, camera)
            ]
            if safe_poses:
                compatible_poses = safe_poses
        available_poses = [item for item in compatible_poses if item not in avoided_poses]
        pose_id = _pick_cos_pose(available_poses or compatible_poses) if compatible_poses else "user"
    elif requested_pose:
        pose_id = requested_pose
        compatible_scenes = [
            scene_id
            for scene_id, scene in COS_RANDOM_SCENE_CLASSES.items()
            if scene_id in allowed_scenes
            if requested_pose == "user" or requested_pose in _scene_pose_ids(scene)
        ]
        if compatible_scenes:
            if not requested_scene:
                safe_scenes = [
                    item for item in compatible_scenes
                    if not _cos_unsafe_random_pair(cos_id, item, pose_id, camera)
                ]
                if safe_scenes:
                    compatible_scenes = safe_scenes
            available_scenes = [item for item in compatible_scenes if item not in avoided_scenes]
            weighted_scenes = _weighted_compatible_scene_ids(
                available_scenes or compatible_scenes,
                cos_id,
            )
            scene_id = random.choice(weighted_scenes or compatible_scenes)
        else:
            scene_id = "user"
    else:
        pairs = [
            (scene_id, pose_id)
            for scene_id, scene in COS_RANDOM_SCENE_CLASSES.items()
            if scene_id in allowed_scenes
            for pose_id in _scene_pose_ids(scene)
            if pose_id in allowed_poses
            and pose_id not in COS_EXPLICIT_ONLY_POSE_IDS
            and pose_id in _poses_for_camera((pose_id,), camera)
            and not _cos_unsafe_random_pair(cos_id, scene_id, pose_id, camera)
        ]
        compatible_scene_ids = set(
            _weighted_compatible_scene_ids((scene_id for scene_id, _ in pairs), cos_id)
        )
        pairs = [pair for pair in pairs if pair[0] in compatible_scene_ids]
        if requested_orientation:
            pairs = [
                pair for pair in pairs
                if pair[1] not in COS_RANDOM_POSE_CLASSES
                or not COS_RANDOM_POSE_CLASSES[pair[1]].get("orientation")
                or COS_RANDOM_POSE_CLASSES[pair[1]]["orientation"] == requested_orientation
            ]
        available_pairs = [
            pair for pair in pairs
            if pair[0] not in avoided_scenes and pair[1] not in avoided_poses
        ]
        if not available_pairs:
            available_pairs = [pair for pair in pairs if pair[0] not in avoided_scenes]
        if not available_pairs:
            available_pairs = [pair for pair in pairs if pair[1] not in avoided_poses]
        if not available_pairs:
            available_pairs = pairs
        if available_pairs:
            candidate_scenes = list(dict.fromkeys(pair[0] for pair in available_pairs))
            weighted_scenes = _weighted_compatible_scene_ids(candidate_scenes, cos_id)
            scene_id = random.choice(weighted_scenes or candidate_scenes)
            candidate_poses = [pair[1] for pair in available_pairs if pair[0] == scene_id]
            pose_id = _pick_cos_pose(candidate_poses)
        else:
            candidate_scenes = [scene_id for scene_id in allowed_scenes if scene_id not in avoided_scenes]
            scene_id = random.choice(candidate_scenes or list(allowed_scenes))
            scene = COS_RANDOM_SCENE_CLASSES[scene_id]
            candidate_poses = [
                pose_id for pose_id in _scene_pose_ids(scene)
                if pose_id in allowed_poses and pose_id in _poses_for_camera((pose_id,), camera)
            ]
            safe_candidate_poses = [
                pose_id for pose_id in candidate_poses
                if not _cos_unsafe_random_pair(cos_id, scene_id, pose_id, camera)
            ]
            if safe_candidate_poses:
                candidate_poses = safe_candidate_poses
            pose_id = _pick_cos_pose(candidate_poses or list(allowed_poses))

    scene = COS_RANDOM_SCENE_CLASSES.get(scene_id)
    style_id, scene_style_prompt = _pick_scene_style(scene_id, extra_request)
    expression_id, expression_prompt = _pick_scene_expression(pose_id, extra_request)
    makeup_id, makeup_prompt = _pick_scene_makeup(scene_id, extra_request)

    prompt_parts = []
    if scene and requested_scene != "user":
        prompt_parts.append(
            (
                "本次随机场景优先于套装正文中残留的背景或环境描述："
                if not requested_scene
                else "按用户指定场景补充环境细节："
            )
            + scene["prompt"]
        )
    if scene_style_prompt:
        prompt_parts.append("本次场景影像风格族保持与环境一致：" + scene_style_prompt)
    if scene and not _requests_explicit_soft_focus(extra_request):
        prompt_parts.append(_cos_subject_clarity_prompt_for_pose(cos_id, pose_id))
        safety_prompt = _cos_face_safety_prompt(
            cos_id,
            scene_id,
            pose_id,
            camera,
        )
        if safety_prompt:
            prompt_parts.append(safety_prompt)
    if pose_id in COS_RANDOM_POSE_CLASSES:
        pose_prefix = (
            "本次指定姿势按以下细节执行："
            if requested_pose
            else "本次随机姿势优先于套装正文中残留的人物姿势描述："
        )
        prompt_parts.append(
            pose_prefix + _cos_pose_prompt_for_camera(pose_id, camera)
        )
    if expression_prompt:
        prompt_parts.append("本次兼容神态：" + expression_prompt)
    if makeup_prompt:
        prompt_parts.append("本次兼容妆造：" + makeup_prompt)
    result = {
        "pose_id": pose_id,
        "scene_id": scene_id,
        "prompt": "".join(prompt_parts),
        "style_id": style_id,
        "expression_id": expression_id,
        "makeup_id": makeup_id,
        "style_family": str((scene or {}).get("style_family") or ""),
    }
    if selected_combination:
        result.update(
            {
                "combination_id": selected_combination["combination_id"],
                "combination_title": selected_combination.get("title", ""),
            }
        )
    return result


def keep_cos_outfit_requested(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    return any(
        marker in compact
        for marker in ("保持服饰", "服装不变", "不换衣服", "保持套装", "保持cos", "只换姿势", "只换场景", "只换镜头")
    )


COS_LOOK_CATEGORY_TERMS = (
    "旗袍", "汉服", "女仆", "睡衣", "长裙", "短裙", "短装", "运动服", "体操服", "礼服", "泳装", "古装", "古风", "巫女", "肚兜", "挂脖",
    "洛丽塔", "花嫁", "围裙", "白熊", "和风", "荷叶裙", "兜兜", "袴裙",
)

# Common short names keep command matching practical without adding fields to
# the stable four-key catalog schema.
_COS_LOOK_NAME_ALIASES = {
    "original_blue_stage_opera": ("蓝色戏曲舞台服", "蓝色戏曲", "国风戏曲舞台服"),
    "blue_archive_hoshino": ("星野",),
    "blue_archive_hoshino_private_gym": ("星野", "星野体操服", "星野运动服"),
    "blue_archive_hoshino_private_beach": ("星野海边", "星野泳装", "星野夏日", "星野荷叶边"),
    "blue_archive_chinatsu": ("千夏",),
    "blue_archive_serika": ("芹香",),
    "blue_archive_iori": ("伊织",),
    "blue_archive_maki": ("真纪",),
    "blue_archive_fuuka": ("风香",),
    "blue_archive_toki": ("纺希",),
    "zzz_sunna_private_sportswear": ("千夏", "Sunna", "妄想天使千夏", "绝区零千夏"),
}

# Every outfit is stored in one catalog. ``cos_type`` identifies the source
# game/series or a fallback outfit/style category while title segments identify
# the character and look. Keep the old series name as an API alias for
# dashboard and plugin compatibility.
COS_LOOK_TYPE_ALIASES: Dict[str, tuple[str, ...]] = {
    "碧蓝航线": ("碧蓝航线", "Azur Lane", "azurlane", "舰娘"),
    "蔚蓝档案": ("蔚蓝档案", "Blue Archive", "bluearchive", "BA"),
    "公主连结": ("公主连结", "Princess Connect", "princessconnect", "公主连接"),
    "无职转生": ("无职转生", "Mushoku Tensei", "mushokutensei", "无职转生到了异世界就拿出真本事"),
    "从零开始的异世界生活": ("从零开始的异世界生活", "Re:Zero", "Re:ゼロ", "rezero"),
    "世界计划 缤纷舞台": ("世界计划 缤纷舞台", "Project SEKAI", "Project Sekai", "プロジェクトセカイ", "プロセカ"),
    "明日方舟": ("明日方舟", "Arknights", "Ark Nights"),
    "崩坏3": ("崩坏3", "崩坏 3", "Honkai Impact 3rd", "Honkai Impact 3"),
    "埃罗芒阿老师": ("埃罗芒阿老师", "Eromanga Sensei", "eromangasensei"),
    "斗罗大陆": ("斗罗大陆", "Soul Land", "Douluo Dalu", "douluodalu"),
    "斗破苍穹": ("斗破苍穹", "BTTH", "Fights Break Sphere"),
    "王者荣耀": ("王者荣耀", "王者", "honorofkings", "hok"),
    "永劫无间": ("永劫无间", "永劫", "naraka", "narakabladepoint"),
    "原神": ("原神", "genshin", "genshinimpact"),
    "Fate": (
        "Fate", "Fate/stay night", "fate stay night", "Fate/Grand Order", "FGO",
        "Fate/EXTRA", "Fate/Apocrypha",
    ),
    "叛逆的鲁鲁修": ("叛逆的鲁鲁修", "Code Geass", "codegeass"),
    "三国杀": ("三国杀", "Sanguosha", "sanguosha"),
    "崩坏：星穹铁道": ("崩坏：星穹铁道", "崩坏星穹铁道", "Honkai Star Rail", "honkaistarrail", "hsr"),
    "缘之空": ("缘之空", "Yosuga no Sora", "yosuganosora"),
    "Hololive": ("Hololive", "hololive"),
    "永雏塔菲": ("永雏塔菲", "塔菲"),
    "绝区零": ("绝区零", "zenless", "zenlesszonezero", "zzz"),
    "鸣潮": ("鸣潮", "wuthering", "wutheringwaves", "wuwa"),
}
COS_LOOK_SERIES_ALIASES = COS_LOOK_TYPE_ALIASES
COS_LOOK_SERIES_MARKERS = ("系列", "作品", "游戏")
# ``COS`` can remain in adapter fallback arguments when the command prefix is
# parsed separately. It is a mode marker, not an outfit/character term.
COS_LOOK_IGNORED_QUERY_TERMS = frozenset(
    {
        "cos",
        "特殊预设",
        "动作预设",
        "上身预设",
        "下身预设",
        "列表",
        "全部",
        "查看",
        "list",
        "all",
        "view",
    }
)


def _compact_cos_match(value: str, separators: str) -> str:
    normalized = re.sub(r"(?<=[a-z])\.(?=[a-z])", "", str(value or "").lower())
    return re.sub(separators, "", normalized)


def _cos_item_series(item: Mapping[str, Any]) -> str:
    explicit = str(item.get("cos_type") or item.get("series") or "").strip()
    if explicit:
        separators = r"[\s·/／、，,：:（）()\[\]【】;；。.!！？?]+"
        compact_explicit = _compact_cos_match(explicit, separators)
        for series, aliases in COS_LOOK_SERIES_ALIASES.items():
            known = {
                _compact_cos_match(alias, separators)
                for alias in (series, *aliases)
                if _compact_cos_match(alias, separators)
            }
            if compact_explicit in known:
                return series
        # Keep explicit fallback categories such as ``古风`` and ``围裙``
        # visible to the catalog instead of turning them back into blanks.
        return explicit
    prompt = str(item.get("prompt") or "")
    title = str(item.get("title") or "")
    for series in COS_LOOK_SERIES_ALIASES:
        if f"《{series}》" in prompt or series in title:
            return series
    return ""


def _populate_cos_type_fields() -> None:
    """Materialize the type field for legacy entries in the single catalog."""
    for item in COS_LOOK_SETS:
        if str(item.get("cos_type") or "").strip():
            continue
        cos_type = _cos_item_series(item)
        if cos_type:
            item["cos_type"] = cos_type


COS_LOOK_CATEGORY_ORDER = (
    "sleepwear",
    "swimwear",
    "qipao",
    "maid",
    "hanfu",
    "formal",
    "uniform",
    "armor",
    "short",
    "other",
)
_COS_LOOK_CATEGORY_MARKERS = (
    ("sleepwear", ("睡衣", "家居")),
    ("swimwear", ("泳装", "水着")),
    ("qipao", ("旗袍",)),
    ("maid", ("女仆", "管家")),
    ("hanfu", ("汉服", "古风", "古装", "巫女", "和风", "肚兜", "挂脖", "袴裙", "战国袍", "青花瓷")),
    ("formal", ("礼服", "花嫁", "长裙", "荷叶裙", "大摆")),
    ("uniform", ("工装", "制服", "研究服", "街头装")),
    ("armor", ("轻甲", "护卫", "特勤", "潜行", "机能", "骑士", "猎人", "战士", "护甲")),
    ("short", ("短装", "迷你裙", "短裙", "短款")),
)
_COS_LOOK_CATEGORY_RANK = {
    category: rank for rank, category in enumerate(COS_LOOK_CATEGORY_ORDER)
}
_COS_LOOK_SERIES_ORDER = (
    "",
    "碧蓝航线", "蔚蓝档案", "公主连结", "无职转生", "从零开始的异世界生活",
    "世界计划 缤纷舞台", "明日方舟", "崩坏3",
    "原神", "绝区零", "鸣潮", "王者荣耀", "永劫无间",
    "斗罗大陆", "斗破苍穹", "缘之空", "叛逆的鲁鲁修", "崩坏：星穹铁道", "埃罗芒阿老师",
    "Fate", "三国杀", "Hololive", "永雏塔菲",
    "古风", "和风", "巫女", "围裙", "礼服", "通用COS", "原创COS",
)
_COS_LOOK_SERIES_RANK = {
    series: rank for rank, series in enumerate(_COS_LOOK_SERIES_ORDER)
}
_COS_LOOK_FALLBACK_TYPES = frozenset(
    {"古风", "和风", "巫女", "围裙", "礼服", "通用COS", "原创COS"}
)


def _cos_item_category(item: Mapping[str, Any]) -> str:
    title = str(item.get("title") or "")
    if "睡裙" in title:
        return "sleepwear"
    if "围裙" in title:
        return "maid"
    if "运动服" in title or "运动装" in title or "体操服" in title:
        return "uniform"
    # Keep positive clauses while dropping negative examples such as
    # ``不要普通旗袍`` or ``不是普通旗袍`` from the category signal.
    prompt_clauses = re.split(r"[。；]", str(item.get("prompt") or ""))
    prompt = " ".join(
        clause for clause in prompt_clauses
        if not re.search(r"不要|禁止|不是", clause)
    )
    description = f"{title} {prompt}"
    for category, markers in _COS_LOOK_CATEGORY_MARKERS:
        if any(marker in description for marker in markers):
            return category
    return "other"


def _cos_item_category_rank(item: Mapping[str, Any]) -> int:
    return _COS_LOOK_CATEGORY_RANK[_cos_item_category(item)]


def _cos_item_series_rank(item: Mapping[str, Any]) -> int:
    series = _cos_item_series(item)
    # These are useful catalog categories rather than source works. Keep them
    # in the legacy no-series position so adding metadata does not reshuffle
    # existing category search results.
    if series in _COS_LOOK_FALLBACK_TYPES:
        return 0
    return _COS_LOOK_SERIES_RANK.get(series, len(_COS_LOOK_SERIES_ORDER))


def _sort_cos_look_sets() -> None:
    original_items = list(enumerate(COS_LOOK_SETS))
    original_items.sort(
        key=lambda pair: (
            _cos_item_category_rank(pair[1]),
            _cos_item_series_rank(pair[1]),
            pair[0],
        )
    )
    COS_LOOK_SETS[:] = [item for _, item in original_items]


_populate_cos_type_fields()
_sort_cos_look_sets()


def _cos_item_series_aliases(item: Mapping[str, Any], separators: str) -> set[str]:
    series = _cos_item_series(item)
    if not series:
        return set()
    return {
        _compact_cos_match(alias, separators)
        for alias in COS_LOOK_SERIES_ALIASES.get(series, (series,))
        if _compact_cos_match(alias, separators)
    }


def cos_query_has_series_constraint(text: str) -> bool:
    """Return whether a query explicitly narrows the COS pool by series."""
    raw_query = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not raw_query:
        return False
    separators = r"[\s·/／、，,：:（）()\[\]【】;；。.!！？?]+"
    compact = _compact_cos_match(raw_query, separators)
    aliases = {
        _compact_cos_match(alias, separators)
        for values in COS_LOOK_SERIES_ALIASES.values()
        for alias in values
        if _compact_cos_match(alias, separators)
    }
    return any(alias and alias in compact for alias in aliases) or any(
        marker in raw_query for marker in COS_LOOK_SERIES_MARKERS
    )


def _cos_item_terms(item: Mapping[str, Any]) -> List[str]:
    title = str(item.get("title") or "")
    compact_title = re.sub(r"\s+", "", title).lower()
    terms: List[str] = [compact_title]
    for part in re.split(r"[·/\s、，,：:（）()]+", title):
        part = part.strip().lower()
        if len(part) >= 2 or (len(part) == 1 and "\u4e00" <= part <= "\u9fff"):
            terms.append(part)
    # Explicit metadata is part of the same catalog matcher.  This also
    # supports future types that are not in the built-in alias table.
    cos_type = str(item.get("cos_type") or item.get("series") or "").strip().lower()
    if cos_type:
        terms.append(cos_type)
    for term in COS_LOOK_CATEGORY_TERMS:
        if term in title:
            terms.append(term.lower())
    terms.extend(_COS_LOOK_NAME_ALIASES.get(str(item.get("id") or ""), ()))
    return list(
        dict.fromkeys(
            term
            for term in terms
            if len(term) >= 2
            or (len(term) == 1 and "\u4e00" <= term <= "\u9fff")
        )
    )


def match_cos_look_sets(text: str) -> List[Dict[str, str]]:
    """Match by full title, character, or outfit category."""
    raw_query = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not raw_query:
        return []
    all_items = [dict(item) for item in COS_LOOK_SETS]
    separators = r"[\s·/／、，,：:（）()\[\]【】;；。.!！？?]+"

    def normalize_dotted_initials(value: str) -> str:
        # Keep abbreviations such as C.C. together while retaining other punctuation as separators.
        return re.sub(r"(?<=[a-z])\.(?=[a-z])", "", value)

    def compact(value: str) -> str:
        normalized = normalize_dotted_initials(str(value or "").lower())
        return re.sub(separators, "", normalized)

    item_terms: Dict[str, set[str]] = {}
    item_series: Dict[str, str] = {}
    term_items: Dict[str, set[str]] = {}
    series_alias_to_key = {
        _compact_cos_match(alias, separators): series
        for series, aliases in COS_LOOK_SERIES_ALIASES.items()
        for alias in aliases
        if _compact_cos_match(alias, separators)
    }
    category_term_set = {compact(term) for term in COS_LOOK_CATEGORY_TERMS}
    for item in all_items:
        item_id = str(item.get("id") or "")
        terms = {compact(term) for term in _cos_item_terms(item)}
        terms.discard("")
        item_terms[item_id] = terms
        item_series[item_id] = _cos_item_series(item)
        for term in terms:
            term_items.setdefault(term, set()).add(item_id)
        for alias in _cos_item_series_aliases(item, separators):
            term_items.setdefault(alias, set()).add(item_id)
    known_terms = sorted(term_items, key=lambda term: (-len(term), term))

    def segment_token(token: str) -> List[str]:
        value = compact(token)
        if not value:
            return []
        best: List[Optional[List[str]]] = [None] * (len(value) + 1)
        best[0] = []
        for start in range(len(value)):
            if best[start] is None:
                continue
            for term in known_terms:
                if value.startswith(term, start):
                    end = start + len(term)
                    candidate = [*best[start], term]
                    previous = best[end]
                    if previous is None or len(candidate) < len(previous):
                        best[end] = candidate
        return best[-1] or []

    raw_query = normalize_dotted_initials(raw_query)
    compact_query = compact(raw_query)
    exact = [
        dict(item)
        for item in all_items
        if compact(str(item.get("title") or "")) == compact_query
    ]
    if exact:
        return exact

    selected_name_terms: List[str] = []
    selected_category_terms: List[str] = []
    selected_series_terms: List[str] = []
    known_series_in_query = any(alias and alias in compact_query for alias in series_alias_to_key)
    # An explicit but unknown series marker must not degrade into a character
    # or category-only match (for example ``未知作品 西施``).
    for token in re.split(separators, raw_query):
        token_compact = compact(token)
        if token_compact and any(marker in token for marker in COS_LOOK_SERIES_MARKERS):
            # Accept natural forms such as ``王者荣耀作品`` while still
            # rejecting an unknown marker before the query can fall back to a
            # character/category-only match.
            marker_stripped = token
            for marker in COS_LOOK_SERIES_MARKERS:
                marker_stripped = marker_stripped.replace(marker, "")
            marker_compact = compact(marker_stripped)
            if not marker_compact and known_series_in_query:
                continue
            if not any(alias and alias in marker_compact for alias in series_alias_to_key):
                return []
    # Detect aliases across whitespace (for example ``Honor of Kings``) and
    # when a series token is adjacent to a count or preset label.
    for alias, series in sorted(series_alias_to_key.items(), key=lambda pair: -len(pair[0])):
        if alias and alias in compact_query:
            selected_series_terms.append(series)
    for token in re.split(separators, raw_query):
        segments = segment_token(token)
        if not segments:
            continue
        for term in segments:
            if term in series_alias_to_key:
                selected_series_terms.append(series_alias_to_key[term])
            elif term in {compact(value) for value in COS_LOOK_IGNORED_QUERY_TERMS}:
                continue
            elif term in category_term_set:
                selected_category_terms.append(term)
            else:
                selected_name_terms.append(term)
    selected_name_terms = list(dict.fromkeys(selected_name_terms))
    selected_category_terms = list(dict.fromkeys(selected_category_terms))
    selected_series_terms = list(dict.fromkeys(selected_series_terms))
    if selected_name_terms or selected_category_terms or selected_series_terms:
        pool = []
        for item in all_items:
            item_id = str(item.get("id") or "")
            terms = item_terms.get(item_id, set())
            if all(term in terms for term in selected_name_terms) and all(
                category in terms for category in selected_category_terms
            ) and all(item_series.get(item_id) == series for series in selected_series_terms):
                pool.append(dict(item))
        return pool
    return []


def pick_cos_look_set(
    *,
    avoid_id: str = "",
    query: str = "",
    exclude_special: bool = False,
) -> Dict[str, str]:
    normalized_query = re.sub(r"\s+", " ", str(query or "")).strip()
    if normalized_query:
        exact_prompt = next(
            (
                item
                for item in COS_LOOK_SETS
                if re.sub(r"\s+", " ", str(item.get("prompt") or "")).strip()
                == normalized_query
            ),
            None,
        )
        if exact_prompt:
            return dict(exact_prompt)
    matched = match_cos_look_sets(query)
    # An explicit series constraint with no candidates must not silently
    # broaden to the entire catalog.  Ordinary unmatched extra text retains
    # the historical random fallback behavior.
    if not matched and cos_query_has_series_constraint(query):
        return {}
    # Special looks participate in selection just like ordinary looks. Their
    # fixed staging contract is preserved later by build_cos_look_action.
    pool = matched or list(COS_LOOK_SETS)
    if exclude_special:
        compatible_pool = [item for item in pool if not is_special_cos_look(item)]
        if compatible_pool:
            pool = compatible_pool
        elif matched:
            return {}
    pool = [item for item in pool if str(item.get("id") or "") != str(avoid_id or "")]
    if not pool:
        pool = matched or list(COS_LOOK_SETS)
    return dict(random.choice(pool))


def parse_cos_look_list_request(text: str) -> Optional[int]:
    """Return the requested catalog page, or ``None`` for a generation query."""
    compact = re.sub(r"[\s，。！？、；：,.!?;:]+", "", str(text or "")).lower()
    match = re.fullmatch(
        r"(?:列表|全部|查看|list|all|view)(?:第?(\d{1,4})页?)?",
        compact,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return max(1, int(match.group(1) or 1))


def format_cos_look_list(page: int = 1, page_size: int = 20) -> str:
    """Format a bounded COS catalog page suitable for QQ text delivery."""
    safe_page_size = max(1, int(page_size or 20))
    random_pool = list(COS_LOOK_SETS)
    total = len(random_pool)
    total_pages = max(1, (total + safe_page_size - 1) // safe_page_size)
    current_page = min(total_pages, max(1, int(page or 1)))
    start = (current_page - 1) * safe_page_size
    items = random_pool[start : start + safe_page_size]
    lines = [
        f"看看COS 随机池（{total}套）：",
        f"第 {current_page}/{total_pages} 页，每页 {safe_page_size} 套。",
        "翻页：/看看COS 全部 页码",
        "",
    ]
    if total_pages > 1:
        if current_page < total_pages:
            lines.append(f"下一页：/看看COS 全部 {current_page + 1}")
        if current_page > 1:
            lines.append(f"上一页：/看看COS 全部 {current_page - 1}")
        lines.append("")
    lines.extend(
        f"{start + offset + 1}. {item.get('title') or '未命名套装'}"
        for offset, item in enumerate(items)
    )
    return "\n".join(lines)


def list_cos_look_sets() -> List[Dict[str, Any]]:
    """Return validated copies for dashboard and quick-test pickers."""
    return [
        {
            "id": str(item.get("id") or "").strip(),
            "title": str(item.get("title") or "").strip(),
            "prompt": str(item.get("prompt") or "").strip(),
            "cos_type": str(item.get("cos_type") or _cos_item_series(item)).strip(),
            "series": _cos_item_series(item),
            "series_aliases": list(COS_LOOK_SERIES_ALIASES.get(_cos_item_series(item), ())),
            "mode": "special" if is_special_cos_look(item) else "random",
            "compatibility": cos_look_compatibility(str(item.get("id") or "")),
        }
        for item in COS_LOOK_SETS
        if str(item.get("id") or "").strip()
        and str(item.get("title") or "").strip()
        and str(item.get("prompt") or "").strip()
    ]


COS_SELFIE_REQUEST_TERMS = tuple(COS_JSON_CAMERA_REQUEST_TERMS["selfie"])


def parse_requested_cos_camera(text: str) -> str:
    raw = str(text or "")
    compact = re.sub(r"\s+", "", raw).lower()
    blob = compact + raw.lower()
    third_keys = tuple(term.lower() for term in COS_JSON_CAMERA_REQUEST_TERMS["third"])
    first_keys = tuple(term.lower() for term in COS_JSON_CAMERA_REQUEST_TERMS["first"])
    selfie_keys = tuple(term.lower() for term in COS_SELFIE_REQUEST_TERMS)
    if any(key in blob for key in third_keys):
        return "third"
    if any(key in blob for key in first_keys):
        return "first"
    if any(key in blob for key in selfie_keys):
        return "selfie"
    return ""


def requests_phone_face_cover(text: str) -> bool:
    """Return whether an explicit request asks for a phone to cover the face."""
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    phone_words = ("手机", "iphone", "phone")
    face_cover_words = (
        "遮脸", "遮住脸", "挡脸", "挡住脸", "遮面", "挡面",
        "遮住半张脸", "半张脸", "遮挡脸部", "遮住部分脸",
    )
    return any(word in compact for word in phone_words) and any(
        word in compact for word in face_cover_words
    )


def pick_cos_camera(
    *,
    extra_request: str = "",
    avoid: Iterable[str] = (),
    camera: str = "",
    pose_id: str = "",
    allowed_cameras: Iterable[str] = (),
) -> str:
    forced = str(camera or "").strip() or parse_requested_cos_camera(extra_request)
    if forced in {"selfie", "first", "third"}:
        return forced
    choices = ("third", "first", "selfie")
    allowed = _as_id_set(allowed_cameras)
    if allowed:
        choices = tuple(kind for kind in choices if kind in allowed)
    compatible = COS_POSE_CAMERA_COMPATIBILITY.get(pose_id)
    if compatible:
        choices = tuple(kind for kind in choices if kind in compatible)
    elif pose_id and pose_id not in COS_RANDOM_SELFIE_POSE_IDS:
        choices = tuple(kind for kind in choices if kind != "selfie")
    avoided = _as_id_set(avoid)
    available = tuple(kind for kind in choices if kind not in avoided)
    return random.choice(available or choices or ("third",))


def adapt_cos_outfit_for_camera(outfit: str, camera: str) -> str:
    text = str(outfit or "")
    if camera not in {"first", "third"}:
        return text
    capture_phrase = "由摄影师拍摄" if camera == "third" else "由主观镜头记录"
    replacements = (
        ("对镜坐在木地板地毯上", "坐在木地板地毯上"),
        ("室内柔光对镜全身", "室内柔光半身"),
        ("室内素墙对镜全身", "室内素墙半身"),
        ("室内黑底柔光，单人侧身对镜", "室内黑底柔光，单人侧身半身"),
        ("对镜全身", "对镜半身"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    # Built-in looks predate the camera marker and some still contain
    # "自拍/镜前" wording.  Strip those capture semantics before the action
    # reaches the camera prompt builder; otherwise the two prompt families
    # compete and models may invent a phone or an extra arm.
    text = re.sub(
        r"在([^。；，,]{0,24})(?:全身)?镜(?:中|前)?自拍",
        f"在\\1环境中{capture_phrase}",
        text,
    )
    text = text.replace("不出现镜子中的倒影或额外人物", "背景保持干净，不出现额外人物")
    text = text.replace("近距离自拍", "近距离摄影")
    text = text.replace("自拍成片", "摄影成片")
    text = text.replace("自拍照片", "摄影照片")
    text = text.replace("自拍", "摄影")
    text = text.replace("镜子前", "室内环境")
    text = text.replace("全身镜前", "室内环境")
    text = text.replace("全身镜", "室内背景")
    text = text.replace("镜子", "室内背景")
    text = text.replace("镜前", "室内环境")
    # Replace standalone capture wording only.  A literal ``面对镜头`` is
    # already valid Chinese; replacing its ``对镜`` substring produces the
    # corrupted ``面面向镜头头`` seen in generated prompts.
    text = re.sub(r"(?<!面)对镜头", "面向镜头", text)
    text = re.sub(r"(?<!面)对镜(?!头)", "面向镜头", text)
    return text


def looks_like_cos_prompt(text: str) -> bool:
    """Detect a COS-pool/outfit prompt before generic selfie composition runs."""
    raw = str(text or "")
    upper = raw.upper()
    return bool(
        "【COS:" in upper
        or re.search(r"(?<![A-Z0-9])COS(?:PLAY)?(?![A-Z0-9])", upper)
        or any(marker in raw for marker in ("换装", "角色扮演"))
    )


def build_cos_third_person_prompt(text: str) -> str:
    """Wrap a raw COS-pool prompt in the shared third-person camera contract."""
    raw = str(text or "").strip()
    if not looks_like_cos_prompt(raw):
        return raw
    if "【cos_mode:special】" in raw.lower():
        if "【特殊 COS / 固定成片模式】" in raw:
            return raw
        tagged_id = re.search(r"【cos:([a-z0-9_]+)】", raw, flags=re.I)
        tagged_item = get_cos_look_by_id(tagged_id.group(1)) if tagged_id else {}
        if tagged_item and is_special_cos_look(tagged_item):
            return build_special_cos_look_action(tagged_item, has_refs=False)
        return raw
    exact_special = next(
        (
            item for item in COS_SPECIAL_LOOK_SETS
            if re.sub(r"\s+", " ", str(item.get("prompt") or "")).strip() == raw
        ),
        None,
    )
    if exact_special:
        return build_special_cos_look_action(exact_special, has_refs=False)
    adapted = adapt_cos_outfit_for_camera(raw, "third")
    adapted = re.sub(r"【cam:(?:selfie|first|third)】", "", adapted, flags=re.I)
    variation = {}
    for tag_name, key in (
        ("cos_scene", "scene_id"),
        ("cos_pose", "pose_id"),
        ("cos_style", "style_id"),
        ("cos_expression", "expression_id"),
        ("cos_makeup", "makeup_id"),
    ):
        match = re.search(rf"【{tag_name}:([a-z0-9_]+)】", raw, flags=re.I)
        if match:
            variation[key] = match.group(1)
    look_match = re.search(r"【cos:([a-z0-9_]+)】", raw, flags=re.I)
    has_shared_variation = bool(
        re.search(r"【cos_(?:pose|scene|style|expression|makeup|framing|view|composition):", raw, flags=re.I)
    )
    if not has_shared_variation:
        variation = pick_cos_variation(
            look_match.group(1) if look_match else "",
            extra_request=raw,
            camera="third",
        )
    if look_match:
        adapted = normalize_cos_eye_contract(adapted, look_match.group(1))
    safety_line = ""
    if look_match and has_shared_variation and variation.get("scene_id") and not _requests_explicit_soft_focus(adapted):
        safety_line = _cos_face_safety_prompt(
            look_match.group(1),
            str(variation.get("scene_id") or ""),
            str(variation.get("pose_id") or ""),
            "third",
            include_base="人物面部是画面最高优先级" not in adapted,
        )
    framing = pick_cos_framing(
        extra_request=adapted,
        scene_id=str(variation.get("scene_id") or ""),
        pose_id=str(variation.get("pose_id") or ""),
        camera="third",
        framing_weights=(
            cos_look_compatibility(look_match.group(1))["recommended_framing_weights"]
            if look_match
            else cos_look_compatibility("")["recommended_framing_weights"]
        ),
    )
    adapted = framing.get("extra_request_text", adapted)
    if framing.get("randomized_from_options"):
        framing_line = f"本次从已有视角选项中随机确定为{framing['prompt']}；"
    else:
        framing_line = (
            f"本次未指定视角，随机采用{framing['prompt']}；该随机机位优先于套装正文中的通用景别和机位描述；"
            if framing["prompt"]
            else "本次按已确定的单一构图执行；"
        )
    composition = framing.get("composition") or {}
    if composition.get("prompt"):
        framing_line += f"附加构图强化：{composition['prompt']}；"
    pose_id = str(variation.get("pose_id") or "")
    if pose_id in COS_BODY_CURVE_POSE_IDS or pose_id == "back_to_camera":
        face_rule = "面部按已选姿势自然呈现，可只见侧脸或不露脸；不得为了露脸回头、扭颈或改变肩胯方向。"
    else:
        face_rule = "保持自然进入画面的面部、发型、服装与配饰清晰；不强行转头露正脸。"
    shared_prompt = str(variation.get("prompt") or "").strip()
    shared_line = f"通用 COS 词库组合：{shared_prompt}" if shared_prompt else ""
    scene_id = str(variation.get("scene_id") or "")
    style_id = str(variation.get("style_id") or "")
    expression_id = str(variation.get("expression_id") or "")
    makeup_id = str(variation.get("makeup_id") or "")
    framing_id = str(framing.get("view_id") or "")
    composition_id = str((framing.get("composition") or {}).get("composition_id") or "")
    return (
        "【cos:web】 【cam:third】 【他拍 / 看看COS模式】"
        "COS 换装他拍：摄影师在画面外；"
        + framing_line
        + "画面只展示主角自身的两条手臂和两只手，手臂从肩部到手腕连接自然；"
        + "完整展示套装层次和腰线；"
        + face_rule
        + "发型、服装、姿势和表情按本次 COS 套装或用户要求执行；明确要求的歪头、仰头、低头、闭眼或夸张表情应保留，未指定时保持头部、视线和表情连贯。"
        + safety_line
        + shared_line
        + f" 用户补充要求：{adapted}。"
        + (f" 【cos_pose:{pose_id}】" if pose_id in COS_RANDOM_POSE_CLASSES else "")
        + (f" 【cos_scene:{scene_id}】" if scene_id in COS_RANDOM_SCENE_CLASSES else "")
        + (f" 【cos_style:{style_id}】" if style_id in COS_JSON_STYLES else "")
        + (f" 【cos_expression:{expression_id}】" if expression_id in COS_JSON_EXPRESSIONS else "")
        + (f" 【cos_makeup:{makeup_id}】" if makeup_id in COS_JSON_MAKEUPS else "")
        + (f" 【cos_framing:{framing_id}】" if framing_id in COS_FRAMING_CLASSES else "")
        + (
            f" 【cos_composition:{composition_id}】"
            if composition_id in COS_RANDOM_COMPOSITION_CLASSES else ""
        )
    )


def build_special_cos_look_action(
    chosen: Mapping[str, Any],
    *,
    has_refs: bool = False,
    appearance_type: str = "auto",
) -> str:
    """Build a fixed COS action without injecting shared random staging."""
    item = dict(chosen or {})
    look_id = str(item.get("id") or "cos").strip()
    title = str(item.get("title") or "特殊 COS").strip()
    cos_type = str(item.get("cos_type") or "").strip()
    config = dict(COS_SPECIAL_LOOK_CONFIG.get(look_id) or {})
    if str(item.get("mode") or item.get("cos_mode") or "").strip().lower() in {
        "special", "fixed", "standalone"
    }:
        config = {**config, "mode": "special"}
    camera = str(
        item.get("special_camera")
        or item.get("fixed_camera")
        or config.get("camera")
        or "third"
    ).strip()
    if camera not in {"selfie", "first", "third"}:
        camera = "third"
    prompt = str(item.get("special_prompt") or item.get("prompt") or "").strip()
    if not prompt:
        return ""
    # A special prompt is deliberately kept verbatim.  Only the outer mode
    # contract and machine-readable tags are added for downstream routing.
    medium = {
        "third": "【他拍 / 看看COS模式】",
        "selfie": "【自拍 / 看看COS模式】",
        "first": "【第一视角 / 看看COS模式】",
    }[camera]
    source_prefix = f"《{cos_type}》" if cos_type else ""
    coherence_line = ""
    if "未明确的部分保持连贯，不额外改写。" not in prompt:
        coherence_line = "未明确的部分保持连贯，不额外改写。"
    return (
        "【特殊 COS / 固定成片模式】"
        + medium
        + "本次特殊 COS 自带完整的服装、场景、影像风格、姿势和镜头构图规则；"
        "以下提示词必须作为一个整体执行，不从普通 COS 随机池追加或覆盖场景、风格、姿势、视角、景别和构图；"
        + coherence_line
        + f"本次套装：{source_prefix}{title}。"
        + prompt
        + "不要把本条改写成普通随机 COS。"
        + f" 【cos:{look_id}】 【cos_mode:special】 【cam:{camera}】"
    )



def build_cos_look_action(
    extra_request: str = "",
    has_refs: bool = False,
    *,
    avoid_id: str = "",
    avoid_camera: Iterable[str] = (),
    avoid_pose: str = "",
    avoid_scene: str = "",
    avoid_framings: Iterable[str] = (),
    avoid_compositions: Iterable[str] = (),
    avoid_poses: Iterable[str] = (),
    avoid_scenes: Iterable[str] = (),
    avoid_cameras: Iterable[str] = (),

    camera: str = "",
    appearance_type: str = "auto",
    match_query: str = "",
    force_id: str = "",
    force_multi_view_collage: bool = False,
    picker: Optional[Callable[..., Mapping[str, Any]]] = None,
) -> str:
    """Build one matched or random COS action with a compatible camera."""
    choose = picker or pick_cos_look_set
    query = str(match_query).strip() if str(match_query).strip() else extra_request
    requested_camera = str(camera or "").strip() or parse_requested_cos_camera(extra_request)
    chosen = get_cos_look_by_id(force_id) if force_id else None
    if not chosen and requested_camera and choose is pick_cos_look_set:
        chosen = choose(avoid_id=avoid_id, query=query, exclude_special=True)
    elif not chosen:
        chosen = choose(avoid_id=avoid_id, query=query)
    if not chosen:
        # Keep the action contract intact when an explicit series has no
        # matching outfit; callers can surface the empty selection instead of
        # accidentally generating from the full catalog.
        return ""
    if is_special_cos_look(chosen):
        return build_special_cos_look_action(
            chosen,
            has_refs=has_refs,
            appearance_type=appearance_type,
        )
    title = str(chosen.get("title") or "随机COS")
    cos_type = str(chosen.get("cos_type") or "").strip()
    cos_id = str(chosen.get("id") or "cos")
    requested_camera_in_text = parse_requested_cos_camera(extra_request)
    explicit_collage = requests_cos_multi_view_collage(extra_request)
    explicit_selfie = any(
        _has_positive_request(extra_request, term)
        for term in COS_SELFIE_REQUEST_TERMS
    )
    if (
        (explicit_collage or force_multi_view_collage)
        and not camera
        and not explicit_selfie
        and requested_camera != "first"
    ):
        requested_camera = "third"
    phone_face_cover = requests_phone_face_cover(extra_request)
    look_profile = cos_look_compatibility(cos_id)
    requested_pose = _requested_cos_pose(extra_request)
    requested_scene = _requested_cos_scene(extra_request)
    requested_orientation = _requested_cos_orientation(extra_request)
    pose_avoidance = _as_id_set(avoid_poses)
    if requested_camera == "selfie" and any(
        _has_positive_request(extra_request, term)
        for term in COS_JSON_CAMERA_REQUEST_TERMS["mirror_selfie"]
    ):
        pose_avoidance.add("back_to_camera")
    camera_kind = pick_cos_camera(
        extra_request=extra_request,
        avoid=_as_id_set(avoid_cameras) | _as_id_set(avoid_camera),
        camera=requested_camera,
        pose_id=requested_pose,
        allowed_cameras=look_profile["recommended_view_ids"],
    )
    compact_extra = re.sub(r"\s+", "", str(extra_request or ""))
    collage_negated = (
        any(term in compact_extra for term in COS_MULTI_VIEW_COLLAGE_TERMS)
        and not explicit_collage
    )
    collage_eligible = camera_kind == "third" and (
        explicit_collage
        or force_multi_view_collage
        or not requested_camera_in_text
    ) and not (
        requested_pose or cos_prompt_has_framing(extra_request) or collage_negated
    )
    collage_active = collage_eligible and (
        explicit_collage or force_multi_view_collage or random.choice([False] * 23 + [True])
    )
    variation = pick_cos_variation(
        cos_id,
        avoid_pose=avoid_pose,
        avoid_scene=avoid_scene,
        avoid_framings=avoid_framings,
        avoid_poses=pose_avoidance,
        avoid_scenes=avoid_scenes,
        extra_request=extra_request,
        camera=requested_camera or camera_kind,
        multi_view_collage=collage_active,
        use_combinations=True,
    )
    collage_active = bool(variation.get("panel_prompt"))
    collage_fallback_reason = ""
    if explicit_collage and not collage_active:
        if requested_pose or cos_prompt_has_framing(extra_request):
            collage_fallback_reason = "因本次同时指定了单一姿势或景别，按明确要求生成单张照片；"
        elif camera_kind != "third":
            collage_fallback_reason = "当前相机视角不适合多格构图，按当前视角生成单张照片；"
        else:
            collage_fallback_reason = "当前套装与指定场景的兼容姿势不足，改为兼容的单张成片；"
    outfit = adapt_cos_outfit_for_camera(
        str(chosen.get("prompt") or "").strip(), camera_kind
    )
    outfit = normalize_cos_eye_contract(outfit, cos_id)
    outfit = adapt_cos_prompt_for_appearance(outfit, appearance_type)
    outfit = normalize_cos_prompt_negatives(
        outfit,
        appearance_type=appearance_type,
        preserve_reference_medium=has_refs,
    )
    framing_choice = pick_cos_framing(
        outfit=outfit,
        extra_request=extra_request,
        avoid_framings=avoid_framings,
        avoid_compositions=avoid_compositions,
        pose_id=variation["pose_id"],
        scene_id=variation["scene_id"],
        camera=camera_kind,
        allowed_framings=look_profile["recommended_framing_ids"],
        framing_weights=look_profile["recommended_framing_weights"],
        multi_view_collage=collage_active,
    )
    # A camera/pose pair can legitimately have only one compatible crop (for
    # example a selfie half-turn). When a caller is rotating a batch, retry
    # the random pose/scene before accepting a previously used framing. This
    # preserves the safety matrix without making rotation flaky.
    avoided_framing_ids = _as_id_set(avoid_framings)
    if (
        avoided_framing_ids
        and framing_choice.get("view_id") in avoided_framing_ids
        and not requested_pose
        and not requested_scene
        and not cos_prompt_has_framing(extra_request)
    ):
        for _ in range(8):
            retry_variation = pick_cos_variation(
                cos_id,
                avoid_pose=avoid_pose,
                avoid_scene=avoid_scene,
                avoid_framings=avoid_framings,
                avoid_poses=pose_avoidance | {variation.get("pose_id", "")},
                avoid_scenes=_as_id_set(avoid_scenes) | {variation.get("scene_id", "")},
                extra_request=extra_request,
                camera=requested_camera or camera_kind,
                multi_view_collage=collage_active,
                use_combinations=True,
            )
            retry_framing = pick_cos_framing(
                outfit=outfit,
                extra_request=extra_request,
                avoid_framings=avoid_framings,
                avoid_compositions=avoid_compositions,
                pose_id=retry_variation.get("pose_id", ""),
                scene_id=retry_variation.get("scene_id", ""),
                camera=camera_kind,
                allowed_framings=look_profile["recommended_framing_ids"],
                framing_weights=look_profile["recommended_framing_weights"],
                multi_view_collage=bool(retry_variation.get("panel_prompt")),
            )
            if retry_framing.get("view_id") not in avoided_framing_ids:
                variation = retry_variation
                framing_choice = retry_framing
                collage_active = bool(variation.get("panel_prompt"))
                break
    outfit = framing_choice.get("outfit_text", outfit)
    extra_request = framing_choice.get("extra_request_text", extra_request)
    series_prefix = (
        f"《{cos_type}》"
        if cos_type in COS_LOOK_TYPE_ALIASES
        and f"《{cos_type}》" not in title
        and f"《{cos_type}》" not in outfit
        else ""
    )
    if collage_active:
        framing_line = (
            "本次采用9:16竖幅画布上的自然错落式多视角拼贴，优先于套装正文中的单一景别描述；"
            "以下各画格保持同一人物、同一套装、同一场景和影像风格；"
        )
    elif framing_choice.get("randomized_from_options"):
        framing_line = f"本次从已有视角选项中随机确定为{framing_choice['prompt']}；"
    elif variation.get("combination_id") and framing_choice.get("prompt"):
        framing_line = (
            f"本次采用精选组合“{variation.get('combination_title') or variation['combination_id']}”，"
            f"镜头构图锁定为{framing_choice['prompt']}；"
        )
    elif framing_choice.get("explicit"):
        framing_line = "按用户指定的镜头构图执行；"
    elif framing_choice["view_id"] and framing_choice["prompt"]:
        framing_line = (
            f"本次未指定视角，随机采用{framing_choice['prompt']}；"
            "该随机机位优先于套装正文中的通用景别和机位描述；"
        )
    else:
        framing_line = ""
    if collage_fallback_reason:
        framing_line = collage_fallback_reason + framing_line
    composition = framing_choice.get("composition") or {}
    if composition.get("prompt"):
        framing_line += f"附加构图强化：{composition['prompt']}；"
    if camera_kind == "third":
        hand_rule = (
            "背面他拍：人物背对镜头，肩胯和头部朝向一致，不回头、不回眸、不扭颈；"
            "双手按已选姿势自然摆放，人物恰好两条手臂和两只手，不额外添加拍摄设备，不要求露出面部。"
            if variation["pose_id"] == "back_to_camera"
            else
            "背后三分之四展示：人物肩线、胯部与头部朝向一致，面部自然朝向画外；不要求露出侧脸或正脸，不为了露脸回头、回眸或扭颈，优先呈现背部、腰胯曲线和服装侧后方。"
            if variation["pose_id"] == "over_shoulder"
            else
            "侧边支撑前倾：不出现手机、镜子或拍摄设备；人物双手在肩部至头部附近同时扶住同一条窄侧边，可相邻或轻微上下错开并共同支撑身体，头部和视线朝向侧边；"
            "不要求露出正脸，保留自然侧脸或让面部部分、全部处于画外，不为了露脸回头或扭颈；人物恰好两条手臂和两只手。"
            if variation["pose_id"] == "side_edge_bend"
            else
            "不要对镜、不要镜子；画面只允许主角既有的一只手握持唯一一部普通手机，从脸侧或下方自然遮住半张脸；"
            "若该手原本做动作或持道具，手机替代它原本的动作或道具，原道具不入镜；"
            "另一只手保持原有动作或道具。人物恰好两条手臂、两只手和正常数量手指，禁止新增手、手臂、镜像手、第二部手机或第二台拍摄设备；"
            "若套装姿势已明确占用双手或不宜举手机，保留原姿势和道具，不额外添加手机，也不强行遮脸。"
            if phone_face_cover
            else "主角双手自然做动作，画面只展示主角自身的两条手臂和两只手，手臂从肩部到手腕连接自然；未明确要求遮脸时保持面部、身体轮廓和服装关键结构清楚，细小纹理允许随景深自然柔化；严格遵循已选机位与姿势呈现面部，侧脸按自然透视呈现，不为露出正脸强行扭头或扭颈。"
        )
        framing = (
            "【他拍 / 看看COS模式】"
            "展示 AI 现在的样子，但本次强制换装为指定 COS 套装。"
            + ("同一人物的七格连续照片拼贴：" if collage_active else "别人视角的单人成品照：")
            + framing_line
            + "画面只保留主角，拍摄者完全在画面外；不要拍到"
            + ("拍摄过程或其它拍摄设备；" if phone_face_cover else "拍摄设备或拍摄过程；")
            + hand_rule
        )
    elif camera_kind == "selfie":
        hand_rule = (
            "本次为背面定时自拍：主角自行启动手机定时拍摄，手机和支架留在画面外；主角背对镜头，双手按已选姿势自然摆放，不拿手机、不回头，也不额外生成拍摄者。"
            if variation["pose_id"] == "back_to_camera"
            else
            "本次为背后三分之四定时拍摄：拍摄设备留在画面外，人物肩线、胯部与头部朝向一致，面部自然朝向画外；不要求露出侧脸或正脸，不为了露脸回头、回眸或扭颈。"
            if variation["pose_id"] == "over_shoulder"
            else
            "侧边支撑前倾优先于通用自拍手势：采用固定或定时拍摄，设备留在画面外，不让手机占用双手；双手必须同时扶住同一条窄侧边并共同支撑身体前倾，头部和视线朝向支撑边，不看镜头；不要求正脸，可只见侧脸或不露脸，不为露脸回头或扭颈。"
            if variation["pose_id"] == "side_edge_bend"
            else
            "可对镜取景；画面只允许主角既有的一只手握持唯一一部普通手机，从脸侧或下方自然遮住半张脸；"
            "若该手原本做动作或持道具，手机替代它原本的动作或道具，原道具不入镜；"
            "另一只手保持原有动作或道具。人物恰好两条手臂、两只手和正常数量手指，禁止新增手、手臂、镜像手、第二部手机或其它拍摄设备；"
            "若套装姿势已明确占用双手或不宜举手机，保留原姿势和道具，不额外添加手机，也不强行遮脸。"
            if phone_face_cover
            else "可对镜取景，但拍摄设备完全在画面外；主角双手自然入镜或放在身侧，未明确要求遮脸时不要用物件遮脸挡衣服。"
        )
        framing = (
            "【自拍 / 看看COS模式】"
            "展示 AI 现在的样子，但本次强制换装为指定 COS 套装。"
            "单人自拍成片："
            + framing_line
            + hand_rule
        )
    else:
        hand_rule = (
            "第一视角主观镜头：镜头位于观察者自然视线的位置，像观察者亲眼看到主角；"
            "画面只出现主角一个人，主角自身两条手臂和两只手自然连接；"
            "不自动出现手持设备、镜面反射、拍摄者、记录设备、观察者身体、观察者手臂或第二人物；"
            + (
                "按背面构图清楚展示头发和服装后侧，不要求主角回头露出面部。"
                if variation["pose_id"] == "back_to_camera"
                else
                "背后三分之四展示优先于面部展示：肩线、胯部与头部朝向一致，脸自然朝向画外；不要求露出侧脸或正脸，不为了露脸回头、回眸或扭颈。"
                if variation["pose_id"] == "over_shoulder"
                else
                "双手在肩部至头部附近同时扶住同一条窄侧边，可相邻或轻微上下错开，头部和视线朝向支撑边；不要求露出正脸，可只见侧脸或不露脸，不为露脸扭头。"
                if variation["pose_id"] == "side_edge_bend"
                else "保持面部、发型、服装和配饰清晰可见。"
            )
        )
        framing = (
            "【第一视角 / 看看COS模式】"
            "展示 AI 现在的样子，但本次强制换装为指定 COS 套装。"
            "第一视角主观单人成片："
            + framing_line
            + hand_rule
        )
    base = (
        framing
        + f"本次套装：{series_prefix}{title}。"
        + outfit
        + "严格还原套装提示词明确列出的颜色、层次、配饰、鞋履和材质；未提及或未核实的部件不自行添加；妆造和神态按本次兼容组合执行；"
        + (
            "保持七张照片的不规则竖幅拼贴关系：中央竖幅主图明显更大，六张辅助照片错落分布于主图上下与两侧，避免等分网格；"
            "各画格使用不同姿势和机位但场景、服装与影像风格一致；不要简化成普通常服；画面干净得体。"
            if collage_active
            else "保持上方已确定的单一构图，不在多个景别或视角之间切换；不要简化成普通常服；画面干净得体。"
        )
    )
    if variation["prompt"]:
        base += adapt_cos_outfit_for_camera(variation["prompt"], camera_kind)
    if variation.get("panel_prompt"):
        base += variation["panel_prompt"]
    if has_refs:
        base = "用户提供的引用图片作为本次主角身份来源，服装、场景和构图按本次已锁定的 COS 规则执行；" + base
    extra = _strip_parsed_cos_request_terms(
        re.sub(r"\s+", " ", str(extra_request or "")).strip(" 。"),
        requested_camera=requested_camera,
        requested_pose=requested_pose,
        requested_orientation=requested_orientation,
    )
    if collage_fallback_reason:
        term_pattern = "|".join(
            re.escape(term)
            for term in sorted(COS_MULTI_VIEW_COLLAGE_TERMS, key=len, reverse=True)
        )
        extra = re.sub(term_pattern, "", extra)
        extra = re.sub(r"^[，,、；;\s]+|[，,、；;\s]+$", "", extra)
    if extra and extra not in base:
        base += f" 用户补充要求优先：{extra}。"
    framing_id = str(
        variation.get("main_framing_id") or framing_choice.get("view_id") or ""
    )
    return (
        base
        + f" 【cos:{cos_id}】 【cam:{camera_kind}】"
        + (f" 【cos_eye:{cos_eye_mode(cos_id)}】" if cos_eye_mode(cos_id) != "unspecified" else "")
        + (f" 【cos_pose:{variation['pose_id']}】" if variation["pose_id"] else "")
        + (f" 【cos_scene:{variation['scene_id']}】" if variation["scene_id"] else "")
        + (f" 【cos_style:{variation['style_id']}】" if variation.get("style_id") else "")
        + (f" 【cos_expression:{variation['expression_id']}】" if variation.get("expression_id") else "")
        + (f" 【cos_makeup:{variation['makeup_id']}】" if variation.get("makeup_id") else "")
        + (f" 【cos_framing:{framing_id}】" if framing_id else "")
        + (f" 【cos_combo:{variation['combination_id']}】" if variation.get("combination_id") else "")
        + (f" 【cos_view:{COS_MULTI_VIEW_COLLAGE_ID}】" if collage_active else "")
        + (f" 【cos_composition:{composition['composition_id']}】" if composition.get("composition_id") else "")
    )
