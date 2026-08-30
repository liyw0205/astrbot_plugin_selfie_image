"""Pose, camera, and legwear rules for the ``看看腿`` workflow."""

from __future__ import annotations

import random
import re
from typing import List, Tuple


LEGWEAR_BY_POSE = {
    "sit": (("光腿神器", 4), ("白丝", 3), ("黑丝", 3)),
    "sit_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 5)),
    "kneel": (("光腿神器", 5), ("白丝", 3), ("黑丝", 2)),
    "kneel_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 5)),
    "side_lie": (("光腿神器", 6), ("白丝", 3), ("黑丝", 1)),
    "side_lie_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 4)),
    "hug_knee": (("光腿神器", 5), ("白丝", 3), ("黑丝", 2)),
    "hug_knee_crop": (("光腿神器", 4), ("白丝", 3), ("黑丝", 3)),
    "cross_leg": (("光腿神器", 2), ("白丝", 4), ("黑丝", 4)),
    "cross_leg_crop": (("光腿神器", 2), ("白丝", 4), ("黑丝", 4)),
    "windowsill": (("光腿神器", 5), ("白丝", 3), ("黑丝", 2)),
    "windowsill_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 5)),
    "kneel_up": (("光腿神器", 5), ("白丝", 2), ("黑丝", 3)),
    "kneel_front": (("光腿神器", 6), ("白丝", 2), ("黑丝", 2)),
    "floor_fold": (("光腿神器", 6), ("白丝", 2), ("黑丝", 2)),
    "one_knee_fix": (("光腿神器", 4), ("白丝", 3), ("黑丝", 3)),
    "reclined_knees_crop": (("光腿神器", 1), ("白丝", 6), ("黑丝", 5)),
    "floor_knees_up_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 5)),
    "desk_sit_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 5)),
    "bed_supine_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 4)),
}

CALF_CROP_POSES = frozenset(
    name for name in LEGWEAR_BY_POSE if name.endswith("_crop")
)

LEGFOCUS_CAMERA_WEIGHTS = {
    "sit": (("selfie", 3), ("third", 2)),
    "sit_crop": (("selfie", 4), ("third", 1)),
    "kneel": (("selfie", 4), ("third", 1)),
    "kneel_crop": (("selfie", 4), ("third", 1)),
    "side_lie": (("selfie", 3), ("third", 2)),
    "side_lie_crop": (("selfie", 4), ("third", 1)),
    "hug_knee": (("selfie", 4), ("third", 1)),
    "hug_knee_crop": (("selfie", 4), ("third", 1)),
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

LEGFOCUS_POSE_POOL = (
    ("sit", 2),
    ("sit_crop", 3),
    ("kneel", 2),
    ("kneel_crop", 3),
    ("side_lie", 2),
    ("side_lie_crop", 3),
    ("hug_knee", 2),
    ("hug_knee_crop", 3),
    ("cross_leg", 2),
    ("cross_leg_crop", 3),
    ("windowsill", 2),
    ("windowsill_crop", 3),
    ("kneel_up", 2),
    ("kneel_front", 2),
    ("floor_fold", 2),
    ("one_knee_fix", 2),
    ("reclined_knees_crop", 3),
    ("floor_knees_up_crop", 3),
    ("desk_sit_crop", 3),
    ("bed_supine_crop", 3),
)

LEGFOCUS_POSE_VARIANTS = {
    "sit": ("椅上或沙发自然坐好，衣摆顺着坐姿落下，室内光线柔和。",),
    "sit_crop": ("椅上或沙发自然坐好，衣摆顺着坐姿落下，近距离记录腰线到膝部附近的服装。",),
    "kneel": ("在地毯或软垫上自然跪坐，双膝并拢、重心稳定，上身略前倾，衣摆平整落下。",),
    "kneel_crop": ("在地毯或软垫上自然跪坐，双膝并拢、重心稳定，近距离记录衣摆到膝部附近的搭配。",),
    "side_lie": ("在床边或沙发上侧躺曲腿，一侧自然弯曲、另一侧放松伸展，衣料和靠垫形成生活化背景。",),
    "side_lie_crop": ("在床边或沙发上侧躺曲腿，姿势放松可维持，近距离记录衣摆到膝部附近的服装层次。",),
    "hug_knee": ("坐在床边或地毯上自然抱膝，双手轻扶膝部或衣摆，姿势舒适可维持。",),
    "hug_knee_crop": ("坐在床边或地毯上自然抱膝，衣摆随着收膝姿势落下，画面聚焦衣摆到膝部附近。",),
    "cross_leg": ("坐在椅上或沙发上自然翘二郎腿，衣摆和服装搭配协调，重心稳定，像普通生活随拍。",),
    "cross_leg_crop": ("坐在椅上或沙发上自然交叠双侧下装，近距离记录腰线到膝部附近的搭配，边界清楚。",),
    "windowsill": ("稳坐窗台或矮柜边，一侧轻踩台沿、另一侧自然垂下，衣摆自然落下，窗光柔和。",),
    "windowsill_crop": ("稳坐窗台或矮柜边，保持窗边坐姿与稳定重心，近距离记录衣料、颜色和褶皱。",),
    "kneel_up": ("在软垫上采用较高的跪姿，上身略直、重心稳定，衣物自然垂落。",),
    "kneel_front": ("面向镜头跪坐在地毯上，双膝并拢、衣摆整洁，保持日常记录感。",),
    "floor_fold": ("坐在地毯或木地板上自然屈膝，双侧衣摆向身前折叠，衣物层次清楚。",),
    "one_knee_fix": ("一侧单膝触地、另一侧自然支撑，手轻整理衣摆或袜口，动作生活化且重心稳定。",),
    "reclined_knees_crop": ("在沙发或座椅上轻松靠坐，膝部自然向前弯曲，近距离记录服装版型。",),
    "floor_knees_up_crop": ("在地毯或木地板上轻松席地坐，膝部自然收近，近距离记录衣摆和材质。",),
    "desk_sit_crop": ("坐在桌前椅上，桌沿可入镜，近距离记录衣摆、颜色和材质。",),
    "bed_supine_crop": ("在床上由靠垫支撑轻松靠坐，衣摆和床品自然铺开，保持居家随拍感。",),
}

LEGFOCUS_POSE_LABELS = {
    "sit": "坐姿穿搭记录",
    "sit_crop": "坐姿近景记录",
    "kneel": "跪坐记录",
    "kneel_crop": "跪坐近景",
    "side_lie": "侧躺曲腿记录",
    "side_lie_crop": "侧躺曲腿近景",
    "hug_knee": "收膝坐姿记录",
    "hug_knee_crop": "收膝坐姿近景",
    "cross_leg": "交叠坐姿记录",
    "cross_leg_crop": "交叠坐姿近景",
    "windowsill": "窗边坐姿记录",
    "windowsill_crop": "窗边坐姿近景",
    "kneel_up": "高位跪姿",
    "kneel_front": "正面跪坐",
    "floor_fold": "席地屈膝坐姿",
    "one_knee_fix": "单膝整理衣摆",
    "reclined_knees_crop": "沙发靠坐近景",
    "floor_knees_up_crop": "席地坐近景",
    "desk_sit_crop": "桌前坐姿近景",
    "bed_supine_crop": "床上靠坐近景",
}

LEGWEAR_PROMPTS = {
    "光腿神器": "本次服装搭配：自然肤色光腿神器，从大腿连续覆盖到膝部，材质自然，作为得体穿搭的一部分。",
    "白丝": "本次服装搭配：白色不透白丝，从大腿连续覆盖到膝部，袜口位于大腿上部，作为得体日常穿搭的一部分。",
    "黑丝": "本次服装搭配：黑色不透黑丝，从大腿连续覆盖到膝部，袜口位于大腿上部，作为得体日常穿搭的一部分。",
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
    ("短裙", "日常裙装"),
)

SAFE_LEGWEAR_LABELS = {
    "光腿神器": "自然肤色光腿神器，从大腿连续覆盖到膝部",
    "白丝": "白色不透白丝，从大腿连续覆盖到膝部，袜口位于大腿上部",
    "黑丝": "黑色不透黑丝，从大腿连续覆盖到膝部，袜口位于大腿上部",
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


def parse_requested_legwear(text: str) -> str:
    """Honor explicit user legwear; empty means random selection by pose."""
    raw = str(text or "")
    safe_tag = re.search(r"【wear:(white|black|bare)】", raw)
    if safe_tag:
        return {"white": "白丝", "black": "黑丝", "bare": "光腿神器"}[safe_tag.group(1)]
    match = re.search(r"本次腿部穿搭[:：]\s*(光腿神器|白丝|黑丝)", raw)
    if match:
        return str(match.group(1) or "").strip()
    cleaned = re.sub(r"若本次是白丝/黑丝[^。\n]*。?", " ", raw)
    cleaned = re.sub(r"光腿神器[、,，/]白丝[、,，/或]黑丝", " ", cleaned)
    cleaned = re.sub(r"白丝/黑丝", " ", cleaned)
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
) -> str:
    """Build one pose-aware, clothing-focused action for ``看看腿``."""
    pose_pool = [item for item in LEGFOCUS_POSE_POOL if item[0] != avoid_pose]
    if not pose_pool:
        pose_pool = list(LEGFOCUS_POSE_POOL)
    pose_bucket = random.choices(
        [name for name, _ in pose_pool],
        weights=[weight for _, weight in pose_pool],
        k=1,
    )[0]
    variants = LEGFOCUS_POSE_VARIANTS.get(
        pose_bucket, LEGFOCUS_POSE_VARIANTS["sit_crop"]
    )
    pose_label = LEGFOCUS_POSE_LABELS.get(pose_bucket, "坐姿下装展示")
    camera_bag = [
        kind
        for kind, weight in LEGFOCUS_CAMERA_WEIGHTS.get(
            pose_bucket, (("selfie", 1), ("third", 1))
        )
        for _ in range(weight)
    ]
    camera_kind = random.choice(camera_bag)
    camera_line = (
        "第一人称手机自拍：人物自己举手机向下记录日常服装局部，镜头从腰线附近取到膝部附近。"
        if camera_kind == "selfie"
        else "第三人称摄影照片：由画面外的朋友用手机拍摄日常服装局部，镜头从腰线附近取到膝部附近。"
    )
    hard_crop = (
        "日常服装局部记录，取景范围从腰线附近到膝部附近；服装得体、不透明，"
        "画面用于记录颜色、材质和层次，不强调身体细节。"
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
    legwear_label = SAFE_LEGWEAR_LABELS.get(
        legwear, "光腿神器、白丝或黑丝三选一"
    )
    legwear_rule = (
        f"本次服装搭配已锁定为：{legwear_label}；"
        "腿部穿搭只允许光腿神器、白丝、黑丝三选一；参考图中的袜子、腿部服装和鞋袜搭配全部忽略，不得复制；"
        "禁止中筒袜、短袜、运动袜、船袜、堆堆袜、普通棉袜或袜子停在小腿中段。"
    )
    base = (
        "成年人物日常穿搭记录。竖屏手机记录照。"
        f"{camera_line}"
        "【legs:outfit】"
        f"【姿势】{pose_label}：{random.choice(variants)}"
        f"{legwear_rule}"
        f"{anatomy_rules}"
        f"{hard_crop}"
    )
    if has_refs:
        base += " 用户提供的图片只参考氛围、构图、姿势和室内环境；不参考或复制其中任何袜子、腿部穿搭或鞋袜搭配，主角身份仍以 AI 自拍形象参考图为准。"
    extra = LEGWEAR_REQUEST_PATTERN.sub("", str(extra_request or ""))
    for risky_text, neutral_text in LEGFOCUS_RISKY_EXTRA_REPLACEMENTS:
        extra = extra.replace(risky_text, neutral_text)
    extra = re.sub(r"\s+", " ", extra).strip(" 。、，")
    if extra:
        base = base.rstrip("。") + f"。用户补充要求优先：{extra}。"
    wear_tag = {"白丝": "white", "黑丝": "black", "光腿神器": "bare"}.get(
        legwear, "daily"
    )
    return base + f" 【cam:{camera_kind}】 【wear:{wear_tag}】 【pose:{pose_bucket}】"
