"""Pose, camera, and legwear rules for the ``看看腿`` workflow."""

from __future__ import annotations

import random
import re
from typing import Dict, List, Tuple


# Each entry is a complete lower-body composition.  Keep the upper body out of
# frame so the pool does not accidentally mix a full-body pose with a crop rule.
LEGFOCUS_POSE_POOL: List[Dict[str, str]] = [
    {
        "id": "sofa_front_crop",
        "title": "沙发正坐",
        "prompt": (
            "椅上或沙发自然坐姿；画面严格只拍腰部以下，上半身、头部和脸部完全在画面外。"
            "人物坐在沙发前沿，双膝自然并拢向前，小腿顺着膝盖自然垂下，双脚平稳落在地面并完整可见；"
            "沙发、地毯和地面保持清晰的真实接触关系，姿势简单、重心稳定。"
            "镜头从腰线向下近距离取景，重点记录下装的颜色、材质和褶皱。"
        ),
    },
    {
        "id": "chair_side_crop",
        "title": "椅上侧坐",
        "prompt": (
            "画面严格只包含腰部以下，上半身和头部完全不入镜。"
            "人物坐在木椅上，身体呈三分之一侧向镜头，一侧膝盖自然向前，另一侧略微向后，双脚平稳接触地面，腿部不交叉、不扭曲。"
            "椅面、地板和衣摆形成清晰层次，镜头与膝部高度接近，从腰线向下记录下装和脚部。"
        ),
    },
    {
        "id": "sofa_cross_crop",
        "title": "沙发自然交叠",
        "prompt": (
            "画面只取腰部以下，上半身、头部和脸部都在画面外。"
            "人物坐在沙发上，双腿自然向前伸展，脚踝轻微交叠但膝盖方向正常，脚部保持完整并与地面或沙发表面真实接触。"
            "一侧衣摆自然落下，另一侧保留清晰服装褶皱；镜头从腰线向下近距离记录下装层次。"
        ),
    },
    {
        "id": "floor_knees_crop",
        "title": "地毯收膝",
        "prompt": (
            "画面严格只拍腰部以下，上半身完全位于画面外。"
            "人物坐在地毯上，双膝自然弯曲并靠近身体，双侧小腿顺着姿势向身体后方折叠，衣摆和地毯形成真实遮挡。"
            "被遮挡的脚部沿自然方向连续延伸，不在关节或小腿中段突然截断；低机位近距离记录衣摆、材质和地面接触关系。"
        ),
    },
    {
        "id": "sofa_occlusion_crop",
        "title": "靠垫自然遮挡",
        "prompt": (
            "画面只包含腰部以下，上半身、头部和脸部完全不出镜。"
            "人物坐在沙发角落，由靠垫支撑身体，双膝自然向前弯曲，脚部被沙发边缘或靠垫合理遮挡。"
            "被遮挡的肢体沿真实方向连续延伸，靠垫、沙发和地面有清晰前后关系；镜头从腰线向下取景，保持日常服装局部记录。"
        ),
    },
    {
        "id": "stool_edge_crop",
        "title": "矮凳自然出框",
        "prompt": (
            "画面严格限制在腰部以下，上半身完全在画面外。"
            "人物坐在矮凳上，双膝自然向前，一条腿顺着画面下沿向前延伸并自然出框，另一条腿垂直落下，脚部部分可见。"
            "画面边缘只负责取景，不作为膝盖或小腿中段的裁切线；镜头从腰线向下近距离拍摄，地板和凳脚提供真实透视。"
        ),
    },
    {
        "id": "floor_side_kneel_crop",
        "title": "地面侧跪收膝",
        "prompt": (
            "画面严格只拍腰部以下，上半身、头部和脸部完全在画面外。"
            "人物坐在地板上呈侧身跪坐姿势，双膝弯曲并向画面一侧收拢；靠近镜头的一条小腿向前折叠，脚掌自然贴地，另一条小腿向身体后侧折叠并可由衣摆或身体接触面合理遮挡。"
            "两条腿从大腿到小腿保持连续，膝盖方向自然，不交叉、不反折；裙摆或地面只在有清晰接触关系时遮挡腿部。"
            "镜头从腰线向下近距离侧拍，突出下装褶皱、膝盖弯曲层次和地面接触，姿势稳定自然。"
        ),
    },
    {
        "id": "seat_knees_cross_crop",
        "title": "座椅后仰抬膝",
        "prompt": (
            "画面严格只包含腰部以下，上半身、头部和脸部完全在画面外。"
            "人物后仰坐在宽大座椅上，双膝同时向镜头抬起并彼此靠近，两条小腿沿镜头方向向前延伸；画面下方脚踝自然轻微交叠，一只脚略微搭在另一只脚前方。"
            "座椅靠背、扶手和坐垫形成清晰支撑，腿部从大腿到脚部连续自然，膝盖和脚踝不扭曲、不反向连接。"
            "低机位从腰线向下近距离拍摄，允许脚部接近画面前景，保持真实透视、稳定比例和清晰的座椅接触关系。"
        ),
    },
    {
        "id": "floor_topdown_cross_crop",
        "title": "俯拍交叉坐姿",
        "prompt": (
            "画面严格只拍腰部以下，上半身、头部和脸部完全在画面外。"
            "人物坐在地面或木地板上，双腿自然交叉并保持膝盖、脚踝方向正常，双脚按自然姿势贴近地面；"
            "衣摆、袜身和地面形成清晰层次，腿部从大腿到脚部保持连续自然。"
            "镜头从正上方近距离俯拍，突出下装褶皱、双腿交叉关系和地面接触，保持真实透视与稳定比例。"
        ),
    },
]

_LEGFOCUS_POSE_IDS = frozenset(str(item["id"]) for item in LEGFOCUS_POSE_POOL)

# Older persisted actions use these broad pose ids. Resolve them to the newer
# complete compositions while keeping the original tag for prompt compatibility.
LEGFOCUS_POSE_ALIASES = {
    "sit": "sofa_front_crop",
    "sit_crop": "sofa_front_crop",
    "kneel": "floor_knees_crop",
    "kneel_crop": "floor_knees_crop",
    "side_lie": "sofa_cross_crop",
    "side_lie_crop": "sofa_cross_crop",
    "hug_knee": "floor_knees_crop",
    "hug_knee_crop": "floor_knees_crop",
    "cross_leg": "sofa_cross_crop",
    "cross_leg_crop": "sofa_cross_crop",
    "windowsill": "chair_side_crop",
    "windowsill_crop": "chair_side_crop",
    "kneel_up": "floor_knees_crop",
    "kneel_front": "floor_knees_crop",
    "floor_fold": "floor_knees_crop",
    "one_knee_fix": "floor_side_kneel_crop",
    "reclined_knees_crop": "seat_knees_cross_crop",
    "floor_knees_up_crop": "floor_knees_crop",
    "desk_sit_crop": "chair_side_crop",
    "bed_supine_crop": "sofa_cross_crop",
}


# One entry is selected as a whole so a generic request never leaves skirt,
# hosiery, or shoes for the model to invent independently.
LEGFOCUS_OUTFIT_POOL: List[Dict[str, str]] = [
    {
        "id": "blue_plaid_pleated",
        "legwear": "连裤袜",
        "skirt": "浅蓝色格纹百褶裙",
        "shoes": "深棕色圆头平底鞋",
        "prompt_en": "a light-blue plaid pleated skirt, the selected pantyhose, and dark-brown round-toe flats",
    },
    {
        "id": "navy_a_line",
        "legwear": "连裤袜",
        "skirt": "深蓝色A字半身裙",
        "shoes": "黑色玛丽珍平底鞋",
        "prompt_en": "a navy A-line skirt, the selected pantyhose, and black Mary Jane flats",
    },
    {
        "id": "cream_pleated",
        "legwear": "白丝",
        "skirt": "奶油白细褶半身裙",
        "shoes": "浅棕色乐福平底鞋",
        "prompt_en": "a cream finely pleated skirt, the selected white stockings, and light-brown loafers",
    },
    {
        "id": "gray_check",
        "legwear": "白丝",
        "skirt": "灰色格纹百褶裙",
        "shoes": "黑色圆头平底鞋",
        "prompt_en": "a gray plaid pleated skirt, the selected white stockings, and black round-toe flats",
    },
    {
        "id": "burgundy_pleated",
        "legwear": "黑丝",
        "skirt": "酒红色百褶裙",
        "shoes": "黑色圆头平底鞋",
        "prompt_en": "a burgundy pleated skirt, the selected black stockings, and black round-toe flats",
    },
    {
        "id": "denim_a_line",
        "legwear": "黑丝",
        "skirt": "深蓝色牛仔A字裙",
        "shoes": "白色低帮帆布鞋",
        "prompt_en": "a dark-blue denim A-line skirt, the selected black stockings, and white low-top canvas shoes",
    },
    {
        "id": "oatmeal_knit",
        "legwear": "光腿神器",
        "skirt": "燕麦色针织半身裙",
        "shoes": "米色软底平底鞋",
        "prompt_en": "an oatmeal knit skirt, the selected skin-tone leg-cover styling, and beige soft flats",
    },
    {
        "id": "black_corduroy",
        "legwear": "光腿神器",
        "skirt": "黑色灯芯绒A字裙",
        "shoes": "深棕色乐福鞋",
        "prompt_en": "a black corduroy A-line skirt, the selected skin-tone leg-cover styling, and dark-brown loafers",
    },
]
_LEGFOCUS_OUTFIT_BY_ID = {str(item["id"]): item for item in LEGFOCUS_OUTFIT_POOL}


LEGWEAR_BY_POSE = {
    "sofa_front_crop": (("光腿神器", 3), ("白丝", 4), ("黑丝", 3), ("连裤袜", 2)),
    "chair_side_crop": (("光腿神器", 3), ("白丝", 4), ("黑丝", 3), ("连裤袜", 2)),
    "sofa_cross_crop": (("光腿神器", 3), ("白丝", 3), ("黑丝", 4), ("连裤袜", 2)),
    "floor_knees_crop": (("光腿神器", 4), ("白丝", 3), ("黑丝", 3), ("连裤袜", 2)),
    "sofa_occlusion_crop": (("光腿神器", 4), ("白丝", 3), ("黑丝", 3), ("连裤袜", 2)),
    "stool_edge_crop": (("光腿神器", 3), ("白丝", 4), ("黑丝", 3), ("连裤袜", 2)),
    "floor_side_kneel_crop": (("光腿神器", 4), ("白丝", 3), ("黑丝", 3), ("连裤袜", 2)),
    "seat_knees_cross_crop": (("光腿神器", 3), ("白丝", 4), ("黑丝", 3), ("连裤袜", 2)),
    "floor_topdown_cross_crop": (("光腿神器", 2), ("白丝", 3), ("黑丝", 3), ("连裤袜", 2)),
    # Keep old keys readable for persisted actions and third-party callers.
    "sit": (("光腿神器", 4), ("白丝", 3), ("黑丝", 3), ("连裤袜", 2)),
    "sit_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 5), ("连裤袜", 2)),
    "kneel": (("光腿神器", 5), ("白丝", 3), ("黑丝", 2), ("连裤袜", 2)),
    "kneel_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 5), ("连裤袜", 2)),
    "side_lie": (("光腿神器", 6), ("白丝", 3), ("黑丝", 1), ("连裤袜", 2)),
    "side_lie_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 4), ("连裤袜", 2)),
    "hug_knee": (("光腿神器", 5), ("白丝", 3), ("黑丝", 2), ("连裤袜", 2)),
    "hug_knee_crop": (("光腿神器", 4), ("白丝", 3), ("黑丝", 3), ("连裤袜", 2)),
    "cross_leg": (("光腿神器", 2), ("白丝", 4), ("黑丝", 4), ("连裤袜", 2)),
    "cross_leg_crop": (("光腿神器", 2), ("白丝", 4), ("黑丝", 4), ("连裤袜", 2)),
    "windowsill": (("光腿神器", 5), ("白丝", 3), ("黑丝", 2), ("连裤袜", 2)),
    "windowsill_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 5), ("连裤袜", 2)),
    "kneel_up": (("光腿神器", 5), ("白丝", 2), ("黑丝", 3), ("连裤袜", 2)),
    "kneel_front": (("光腿神器", 6), ("白丝", 2), ("黑丝", 2), ("连裤袜", 2)),
    "floor_fold": (("光腿神器", 6), ("白丝", 2), ("黑丝", 2), ("连裤袜", 2)),
    "one_knee_fix": (("光腿神器", 4), ("白丝", 3), ("黑丝", 3), ("连裤袜", 2)),
    "reclined_knees_crop": (("光腿神器", 1), ("白丝", 6), ("黑丝", 5), ("连裤袜", 2)),
    "floor_knees_up_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 5), ("连裤袜", 2)),
    "desk_sit_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 5), ("连裤袜", 2)),
    "bed_supine_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 4), ("连裤袜", 2)),
}

CALF_CROP_POSES = frozenset(
    set(_LEGFOCUS_POSE_IDS)
    | {name for name in LEGWEAR_BY_POSE if name.endswith("_crop")}
)

LEGFOCUS_CAMERA_WEIGHTS = {
    "sofa_front_crop": (("selfie", 3), ("third", 2)),
    "chair_side_crop": (("selfie", 2), ("third", 3)),
    "sofa_cross_crop": (("selfie", 2), ("third", 3)),
    "floor_knees_crop": (("selfie", 3), ("third", 2)),
    "sofa_occlusion_crop": (("selfie", 4), ("third", 1)),
    "stool_edge_crop": (("selfie", 2), ("third", 3)),
    "floor_side_kneel_crop": (("selfie", 2), ("third", 3)),
    "seat_knees_cross_crop": (("selfie", 4), ("third", 1)),
    "floor_topdown_cross_crop": (("selfie", 4), ("third", 1)),
    # Compatibility weights for actions generated before the pool migration.
    "sit": (("selfie", 3), ("third", 2)),
    "sit_crop": (("selfie", 4), ("third", 1)),
    "side_lie": (("selfie", 3), ("third", 2)),
    "side_lie_crop": (("selfie", 4), ("third", 1)),
    "cross_leg": (("selfie", 2), ("third", 4)),
    "cross_leg_crop": (("selfie", 3), ("third", 3)),
    "windowsill": (("selfie", 1), ("third", 4)),
    "windowsill_crop": (("selfie", 2), ("third", 4)),
    "kneel_up": (("selfie", 1), ("third", 4)),
    "kneel_front": (("selfie", 2), ("third", 4)),
    "floor_fold": (("selfie", 4), ("third", 1)),
    "one_knee_fix": (("selfie", 4), ("third", 2)),
    "reclined_knees_crop": (("selfie", 5), ("third", 1)),
    "floor_knees_up_crop": (("selfie", 5), ("third", 1)),
    "desk_sit_crop": (("selfie", 3), ("third", 2)),
    "bed_supine_crop": (("selfie", 6), ("third", 1)),
}

LEGWEAR_PROMPTS = {
    "光腿神器": "本次服装搭配：自然肤色光腿神器，沿可见腿部连续覆盖，材质自然，作为得体穿搭的一部分。",
    "白丝": "本次服装搭配：白色不透白丝，从大腿上部沿可见腿部连续向下覆盖，袜口位于大腿上部，作为得体日常穿搭的一部分。",
    "黑丝": "本次服装搭配：黑色不透黑丝，从大腿上部沿可见腿部连续向下覆盖，袜口位于大腿上部，作为得体日常穿搭的一部分。",
    "连裤袜": "本次服装搭配：白色不透连裤袜，从腰部沿可见腿部连续覆盖到脚趾，袜身平整自然，作为得体日常穿搭的一部分。",
}

STOCKING_FINISH_CHOICES: List[Tuple[str, int]] = [
    ("袜口宽花边，素色袜身。", 4),
    ("膝上细腿环，袜口平整。", 4),
    ("袜口花边加细腿环。", 3),
    ("袜口小蝴蝶结，素色袜身。", 2),
    ("袜身带细竖纹，袜口平整。", 2),
    ("袜口轻轻卷边，素色袜身。", 1),
]

LEGWEAR_REQUEST_PATTERN = re.compile(
    r"(?:光腿神器|光腿|白丝|黑丝|丝袜|短袜|堆堆袜|过膝袜|长筒袜|肉色丝袜|肉丝|连裤袜|长袜)"
)

LEGFOCUS_RISKY_EXTRA_REPLACEMENTS = (
    ("掀衣摆", "整理衣摆"),
    ("晒腿", "记录穿搭"),
    ("主要看腿形", "重点看服装版型"),
    ("看腿形", "看服装版型"),
    ("不露脸", "服装局部取景"),
)

LEGFOCUS_OUTFIT_REQUEST_PATTERN = re.compile(
    r"(?:裙|百褶|A字|半身|裤|鞋|靴|平底|乐福|玛丽珍|帆布鞋|凉鞋)"
)

SAFE_LEGWEAR_LABELS = {
    "光腿神器": "自然肤色光腿神器，沿可见腿部连续覆盖",
    "白丝": "白色不透白丝，从大腿上部沿可见腿部连续向下覆盖，袜口位于大腿上部",
    "黑丝": "黑色不透黑丝，从大腿上部沿可见腿部连续向下覆盖，袜口位于大腿上部",
    "连裤袜": "白色不透连裤袜，从腰部沿可见腿部连续覆盖到脚趾，袜身平整自然",
}


def is_leg_calf_crop_action(text: str) -> bool:
    raw = str(text or "")
    match = re.search(r"【pose:([a-z_]+)】", raw)
    if match and match.group(1) in CALF_CROP_POSES:
        return True
    if "【legs:outfit】" in raw or "下半身穿搭" in raw or "穿搭展示" in raw:
        return True
    if "【crop:calves】" in raw or "双脚完整裁出画外" in raw:
        return True
    return "大腿" in raw and "小腿" in raw and "画外" in raw


def pick_stocking_finish() -> str:
    bag: List[str] = []
    for text, weight in STOCKING_FINISH_CHOICES:
        bag.extend([text] * max(1, int(weight)))
    return random.choice(bag)


def pick_leg_focus_pose(*, avoid_id: str = "") -> Dict[str, str]:
    """Pick one complete lower-body pose entry, avoiding the previous shot."""
    pool = [
        item for item in LEGFOCUS_POSE_POOL
        if str(item.get("id") or "") != str(avoid_id or "")
    ]
    if not pool:
        pool = list(LEGFOCUS_POSE_POOL)
    return dict(random.choice(pool))


def pick_leg_focus_outfit(*, legwear: str = "", avoid_id: str = "") -> Dict[str, str]:
    """Pick one complete skirt, legwear, and shoe combination."""
    pool = [
        item for item in LEGFOCUS_OUTFIT_POOL
        if (not legwear or str(item.get("legwear") or "") == legwear)
        and str(item.get("id") or "") != str(avoid_id or "")
    ]
    if not pool:
        pool = [
            item for item in LEGFOCUS_OUTFIT_POOL
            if not legwear or str(item.get("legwear") or "") == legwear
        ] or list(LEGFOCUS_OUTFIT_POOL)
    return dict(random.choice(pool))


def get_leg_focus_outfit(outfit_id: str) -> Dict[str, str]:
    """Return a validated copy for prompt renderers."""
    return dict(_LEGFOCUS_OUTFIT_BY_ID.get(str(outfit_id or "").strip(), {}))


def has_explicit_leg_outfit(text: str) -> bool:
    """Whether the user supplied a skirt, trouser, or shoe description."""
    cleaned = re.sub(
        r"(?:白色|黑色|肉色)?\s*(?:不透)?(?:连裤袜|白丝|黑丝|丝袜|肉丝|光腿神器|光腿)",
        " ",
        str(text or ""),
    )
    cleaned = re.sub(r"(?:参考图中的)?鞋袜搭配|鞋袜", " ", cleaned)
    return bool(LEGFOCUS_OUTFIT_REQUEST_PATTERN.search(cleaned))


def parse_requested_leg_camera(text: str) -> str:
    """Return an explicitly requested capture direction, if present."""
    raw = str(text or "")
    compact = re.sub(r"\s+", "", raw).lower()
    third_keys = (
        "第三人称", "第三人称视角", "他拍", "别人拍", "朋友拍", "摄影师拍",
        "路人视角", "不要自拍", "非自拍", "notselfie", "thirdperson",
    )
    selfie_keys = (
        "第一人称", "第一人称视角", "手机自拍", "自拍", "自己拍", "selfie",
    )
    if any(key in compact for key in third_keys):
        return "third"
    if any(key in compact for key in selfie_keys):
        return "selfie"
    return ""


def parse_requested_leg_pose(text: str) -> str:
    """Map an explicit top-down request to the matching complete pose."""
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    if any(key in compact for key in ("俯拍", "正上方", "顶拍", "topdown", "top-down")):
        return "floor_topdown_cross_crop"
    return ""


def parse_requested_legwear(text: str) -> str:
    """Honor explicit user legwear; empty means random selection by pose."""
    raw = str(text or "")
    safe_tag = re.search(r"【wear:(white|black|bare|pantyhose)】", raw)
    if safe_tag:
        return {
            "white": "白丝",
            "black": "黑丝",
            "bare": "光腿神器",
            "pantyhose": "连裤袜",
        }[safe_tag.group(1)]
    match = re.search(r"本次腿部穿搭[:：]\s*(光腿神器|白丝|黑丝|连裤袜)", raw)
    if match:
        return str(match.group(1) or "").strip()
    cleaned = re.sub(r"若本次是白丝/黑丝[^。\n]*。?", " ", raw)
    cleaned = re.sub(r"光腿神器[、,，/]白丝[、,，/或]黑丝", " ", cleaned)
    cleaned = re.sub(r"白丝/黑丝", " ", cleaned)
    if re.search(r"连裤袜", cleaned):
        return "连裤袜"
    if re.search(r"白丝", cleaned):
        return "白丝"
    if re.search(r"黑丝", cleaned):
        return "黑丝"
    if re.search(r"光腿神器|光腿", cleaned):
        return "光腿神器"
    return ""


def build_leg_focus_action(
    extra_request: str = "",
    has_refs: bool = False,
    *,
    avoid_pose: str = "",
    force_legwear: str = "",
    force_pose: str = "",
    force_camera: str = "",
) -> str:
    """Build one complete lower-body, clothing-focused action for ``看看腿``."""
    forced_pose_id = str(force_pose or "").strip()
    if not forced_pose_id:
        forced_pose_id = parse_requested_leg_pose(extra_request)
    pose_tag = forced_pose_id
    canonical_pose_id = LEGFOCUS_POSE_ALIASES.get(forced_pose_id, forced_pose_id)
    pose = next(
        (dict(item) for item in LEGFOCUS_POSE_POOL if str(item.get("id") or "") == canonical_pose_id),
        None,
    )
    if pose is None:
        pose = pick_leg_focus_pose(avoid_id=avoid_pose)
        pose_tag = ""
    pose_bucket = str(pose.get("id") or "sofa_front_crop")
    if not pose_tag:
        pose_tag = pose_bucket
    pose_prompt = str(pose.get("prompt") or "").strip()
    camera_bag = [
        kind
        for kind, weight in LEGFOCUS_CAMERA_WEIGHTS.get(
            pose_bucket, (("selfie", 1), ("third", 1))
        )
        for _ in range(weight)
    ]
    camera_kind = str(force_camera or "").strip()
    if camera_kind not in {"selfie", "third"}:
        camera_kind = parse_requested_leg_camera(extra_request)
    if camera_kind not in {"selfie", "third"}:
        camera_kind = random.choice(camera_bag)
    camera_line = (
        "第一人称手机自拍：手机镜头从腰线向下记录下装局部，手机、手臂和上半身都在画面外。"
        if camera_kind == "selfie"
        else "第三人称摄影照片：拍摄者完全在画面外，镜头只记录腰部以下的下装局部。"
    )
    framing_rule = (
        "日常服装局部记录，保持近距离下半身构图，不把膝关节或小腿作为固定裁切线；"
        "服装得体、不透明，画面用于记录颜色、材质和层次，不强调身体细节。"
    )
    leg_continuity_rule = (
        "腿部必须连续、自然并符合真实人体结构。画面边缘可以自然裁出腿部，衣物、家具或前景也可以按明确的前后关系合理遮挡；"
        "若小腿或脚不展示，必须自然延伸到画面外，或被边界清楚的实体物体完整遮挡。"
        "禁止在膝关节、小腿中段或脚踝附近突然终止；跪坐时脚踝应自然过渡到脚背或脚底，再由身体、衣摆或真实接触关系遮挡，不能把袜筒下缘直接当作小腿终点。"
        "地毯、床面或沙发面只有在真实接触和清晰前后关系下才可遮挡肢体，不能无缘无故吞没可见腿部。"
    )
    anatomy_rules = "单人、成年人；姿态放松自然，服装穿着完整；画面重点是日常服装搭配。"
    requested = str(force_legwear or "").strip()
    if requested not in LEGWEAR_PROMPTS:
        requested = parse_requested_legwear(extra_request)
    if requested in LEGWEAR_PROMPTS:
        legwear = requested
    else:
        legwear_options = LEGWEAR_BY_POSE.get(
            pose_bucket, LEGWEAR_BY_POSE["sit_crop"]
        )
        legwear = random.choices(
            [name for name, _ in legwear_options],
            weights=[weight for _, weight in legwear_options],
            k=1,
        )[0]
    explicit_outfit = has_explicit_leg_outfit(extra_request)
    outfit = {} if explicit_outfit else pick_leg_focus_outfit(legwear=legwear)
    legwear_label = SAFE_LEGWEAR_LABELS.get(
        legwear, "代码选定的单一腿部穿搭"
    )
    legwear_rule = (
        f"本次服装搭配已锁定为：{legwear_label}；"
        "只生成这一种已锁定的腿部穿搭；参考图中的袜子、腿部服装和鞋袜搭配全部忽略，不得复制；"
        "禁止中筒袜、短袜、运动袜、船袜、堆堆袜、普通棉袜或袜子停在小腿中段。"
    )
    outfit_rule = ""
    outfit_tag = "custom"
    if outfit:
        outfit_tag = str(outfit.get("id") or "custom")
        outfit_rule = (
            f"完整下装服饰组合已由代码锁定：{outfit.get('skirt', '')}、"
            f"{outfit.get('shoes', '')}；袜装严格使用上方锁定选项，只生成这一套组合，不自行替换裙装或鞋履。"
        )
    base = (
        "成年人物日常下装穿搭记录。竖屏手机近景。"
        f"{camera_line}"
        "【legs:outfit】"
        f"【姿势池·{pose.get('title') or '随机姿势'}】{pose_prompt}"
        f"{legwear_rule}"
        f"{outfit_rule}"
        f"{anatomy_rules}"
        f"{framing_rule}"
        f"{leg_continuity_rule}"
    )
    if has_refs:
        base += " 用户提供的图片只参考氛围、构图、姿势和场景环境；不参考或复制其中任何袜子、腿部穿搭或鞋袜搭配，主角身份仍以 AI 自拍形象参考图为准。"
    extra = str(extra_request or "")
    # Camera and top-down words are already resolved into tags and pose text.
    extra = re.sub(
        r"(?:第一人称(?:视角)?|第三人称(?:视角)?|手机自拍|自拍|自己拍|他拍|别人拍|朋友拍|摄影师拍|俯拍|正上方|顶拍|top[- ]?down)",
        " ",
        extra,
        flags=re.I,
    )
    # Remove a colour adjective together with a stocking term so it does not
    # leave a dangling phrase such as "搭配白色" in the supplement.
    extra = re.sub(r"(?:白色|黑色|肉色)?\s*(?:不透)?(?:连裤袜|白丝|黑丝|丝袜|肉丝)", " ", extra)
    # Removing an explicit stocking can leave a dangling "搭配 和" phrase.
    extra = re.sub(r"搭配\s*(?:和\s*)?", " ", extra, count=1)
    extra = extra.replace("百褶短裙", "百褶裙")
    extra = re.sub(r"短裙", "日常裙装", extra)
    extra = re.sub(r"坐姿\s*[,，]?\s*双腿交叉", "", extra)
    extra = LEGWEAR_REQUEST_PATTERN.sub("", extra)
    for risky_text, neutral_text in LEGFOCUS_RISKY_EXTRA_REPLACEMENTS:
        extra = extra.replace(risky_text, neutral_text)
    extra = re.sub(r"\s+", " ", extra).strip(" 。、，")
    extra = re.sub(r"\s*([，,])\s*", r"\1", extra)
    extra = re.sub(r"[。．]+", "。", extra).strip(" 。、，")
    if extra:
        base = base.rstrip("。") + f"。用户补充要求优先：{extra}。"
    wear_tag = {
        "白丝": "white",
        "黑丝": "black",
        "光腿神器": "bare",
        "连裤袜": "pantyhose",
    }.get(
        legwear, "daily"
    )
    return base + f" 【cam:{camera_kind}】 【wear:{wear_tag}】 【pose:{pose_tag}】 【outfit:{outfit_tag}】"


def has_leg_focus_contract(text: str) -> bool:
    """Whether an action already carries one concrete pose, camera, and wear."""
    raw = str(text or "")
    return bool(
        "【legs:outfit】" in raw
        and re.search(r"【cam:(?:selfie|third)】", raw)
        and re.search(r"【wear:(?:white|black|bare|pantyhose)】", raw)
        and re.search(r"【pose:[a-z_]+】", raw)
        and re.search(r"【outfit:[a-z_]+】", raw)
    )


def ensure_leg_focus_action(text: str, has_refs: bool = False) -> str:
    """Complete raw/incomplete leg requests before prompt rendering.

    This keeps direct callers of the prompt builders consistent with the command
    path, which normally performs the same normalization in ``main.py``.
    """
    raw = str(text or "").strip()
    if not raw or has_leg_focus_contract(raw):
        return raw
    pose_match = re.search(r"【pose:([a-z_]+)】", raw)
    camera_match = re.search(r"【cam:(selfie|third)】", raw)
    wear_match = re.search(r"【wear:(white|black|bare|pantyhose)】", raw)
    forced_pose = str(pose_match.group(1) if pose_match else "").strip()
    forced_camera = str(camera_match.group(1) if camera_match else "").strip()
    requested_pose = parse_requested_leg_pose(raw)
    requested_camera = parse_requested_leg_camera(raw)
    if not forced_pose:
        forced_pose = requested_pose
    if not forced_camera:
        forced_camera = requested_camera
    forced_wear = parse_requested_legwear(raw)
    extra = re.sub(r"/?看看腿", " ", raw, flags=re.I)
    extra = re.sub(r"【(?:legs|cam|wear|pose|outfit):[a-z_]+】", " ", extra, flags=re.I)
    # These words have already been resolved into concrete tags; retaining them
    # would restate the same choice in the free-form supplement.
    extra = re.sub(
        r"(?:第一人称(?:视角)?|第三人称(?:视角)?|手机自拍|自拍|自己拍|他拍|别人拍|朋友拍|摄影师拍|俯拍|正上方|顶拍|top[- ]?down)",
        " ",
        extra,
        flags=re.I,
    )
    extra = re.sub(r"\s+", " ", extra).strip(" 。、，")
    return build_leg_focus_action(
        extra,
        has_refs,
        force_legwear=forced_wear,
        force_pose=forced_pose,
        force_camera=forced_camera,
    )
