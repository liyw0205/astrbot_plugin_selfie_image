"""Prompt builders and intent checks for selfie-style image commands."""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from typing import List, Tuple

from ..features.persona import appearance_type_instruction, current_period, group_style_lines


SELFIE_SHOT_POOL = (
    ("arm_half", 3),
    ("mirror_half", 2),
    ("window_side", 2),
    ("desk_sit", 2),
    ("sofa_casual", 2),
    ("high_angle", 1),
    ("close_portrait", 1),
)

SELFIE_SHOT_LINES = {
    "arm_half": "手机自拍臂半身：手臂微伸举机，胸口以上到头顶入镜，眼神看镜头，表情自然松弛。",
    "mirror_half": "镜子半身自拍：镜中看镜头（看向手机镜头方向），构图干净，不要拍进乱糟糟背景堆。",
    "window_side": "窗边侧光半身自拍：身体略侧、脸仍转向镜头，窗光柔和，轮廓清楚。",
    "desk_sit": "书桌前坐下自拍：略俯视半身，手可托腮或扶桌沿，看镜头，居家学习/摸鱼感。",
    "sofa_casual": "沙发窝着随手自拍：半身或胸像，靠垫入镜一点，看镜头，轻松日常。",
    "high_angle": "稍高机位自拍：镜头略高于眼，脸自然抬一点看镜头，显精神，不要过度仰拍变形。",
    "close_portrait": "近景胸像自拍：脸与肩为主，眼神对焦镜头，五官清晰，浅景深。",
}

SELFIE_GESTURES = (
    "嘴角轻微笑意",
    "一只手整理发丝或衣领",
    "托腮听人说话的轻松感",
    "比个很小的 OK 或比心（含蓄）",
    "双手捧杯刚抬眼",
    "刚坐好整理袖口",
)

THIRD_PERSON_SHOT_POOL = (
    ("half_front", 3),
    ("three_quarter", 3),
    ("env_mid", 2),
    ("walk_candid", 1),
    ("close_smile", 2),
    ("low_over", 1),
    ("lean_wall", 2),
)

THIRD_PERSON_SHOT_LINES = {
    "half_front": "半身平视他拍：别人视角的单人半身照，AI 正面或微侧看镜头，眼神有焦点。",
    "three_quarter": "三分之四侧身他拍：身体略侧、头转向镜头，像被叫名字时回头。",
    "env_mid": "中远景环境人像：人物与场景都清楚，AI 仍是主体，自然看向镜头或刚抬头。",
    "walk_candid": "走路抓拍：侧前方跟随感，步伐自然，可看镜头一瞬，生活感强。",
    "close_smile": "近景胸像：脸与肩，浅景深，对镜头带一点笑意，五官清晰。",
    "low_over": "轻微低机位过肩感：从略低处拍半身，仍看镜头，不要过度仰拍变形。",
    "lean_wall": "靠墙/门框他拍：肩背轻靠，双手自然，看镜头，竖构图友好。",
}

THIRD_PERSON_ACTIONS = (
    "端着杯子刚抬眼",
    "托腮听人说话",
    "整理袖口或发丝",
    "翻书停顿抬头",
    "双手插兜靠站",
    "拎袋子侧身回看",
    "刚坐下整理衣角",
    "对镜头轻轻点头笑",
)


def period_scene_light_pools(
    kind: str = "selfie", *, period: str = ""
) -> Tuple[List[str], List[str]]:
    period = str(period or current_period())
    if kind == "look_you":
        day_scenes = ["窗边沙发", "书桌前", "阳台栏杆", "厨房台边"]
        night_scenes = ["窗边沙发", "书桌前", "夜灯房间", "厨房台边"]
        cafe_day = ["咖啡馆座位", "楼下咖啡外摆", "街边树荫", "书店角落"]
        if period in {"morning", "noon"}:
            return day_scenes + cafe_day, ["窗光", "阴天柔光"]
        if period == "afternoon":
            return day_scenes + cafe_day, ["窗光", "阴天柔光"]
        if period == "evening":
            return night_scenes + ["阳台栏杆", "雨后屋檐"], ["暖台灯", "窗光"]
        return night_scenes, ["暖台灯", "夜灯房间柔光"]
    if period in {"morning", "noon"}:
        return ["浅色墙与窗边", "书桌与杯具", "阳台栏杆旁", "镜前整洁台面"], ["窗光柔和", "清晨清透光", "阴天漫射"]
    if period == "afternoon":
        return ["浅色墙与窗边", "书桌与杯具", "沙发与抱枕", "阳台栏杆旁"], ["窗光柔和", "阴天漫射"]
    if period == "evening":
        return ["暖灯房间一角", "沙发与抱枕", "书桌与杯具", "床边靠坐"], ["暖黄台灯", "窗光柔和"]
    return ["暖灯房间一角", "沙发与抱枕", "床边靠坐", "镜前整洁台面"], ["暖黄台灯"]


def looks_like_crop_waist_request(text: str) -> bool:
    raw = str(text or "")
    if not raw.strip():
        return False
    if any(
        key in raw
        for key in (
            "漏腰", "露腰", "露脐短上衣", "小蛮腰", "crop_waist",
            "crop top", "short top", "短上衣",
        )
    ):
        return True
    low = raw.lower()
    has_inner = (
        "露脐" in raw
        or "短上衣" in raw
        or "crop top" in low
        or "short top" in low
    )
    has_outer = (
        "外衫" in raw
        or "开衫" in raw
        or "半敞" in raw
        or ("衬衫" in raw and ("宽松" in raw or "敞开" in raw))
        or "oversized" in low
    )
    return has_inner and has_outer


def build_crop_waist_selfie_action(
    extra_request: str = "", has_refs: bool = False
) -> str:
    base = (
        "【自拍 / 漏腰模式】"
        "一张漂亮的真人女孩，居家休闲自拍，室内光线柔和偏暗，角度略高。"
        "本次换装优先：黑色短上衣，外面套宽松 oversized 深色长袖衬衫并敞开，"
        "外层宽松带柔软褶皱，微微露出自然腰线；外衫不要整件贴肉。"
        "放松地坐着或靠在深色沙发/床边，头发略显凌乱，像日常随意拍的照片。"
        "保持 AI 身份长相与发色一致，画面干净得体。"
    )
    if has_refs:
        base = "参考用户附图的氛围或构图，" + base
    extra = re.sub(r"\s+", " ", str(extra_request or "")).strip(" 。")
    if extra and extra not in base and extra not in {"漏腰", "露腰", "漏腰杀", "小蛮腰"}:
        base += f" 用户补充要求优先：{extra}。"
    return base + " 【shot:crop_waist】"


def build_selfie_look_action(
    extra_request: str = "",
    has_refs: bool = False,
    *,
    avoid_shot: str = "",
    scene_light_provider: Callable[[str], Tuple[List[str], List[str]]] = period_scene_light_pools,
) -> str:
    extra = re.sub(r"\s+", " ", str(extra_request or "")).strip(" 。")
    if looks_like_crop_waist_request(extra):
        return build_crop_waist_selfie_action(extra, has_refs)
    shot_pool = [item for item in SELFIE_SHOT_POOL if item[0] != avoid_shot]
    if not shot_pool:
        shot_pool = list(SELFIE_SHOT_POOL)
    shot = random.choices(
        [name for name, _ in shot_pool],
        weights=[weight for _, weight in shot_pool],
        k=1,
    )[0]
    scenes, lights = scene_light_provider("selfie")
    scene = random.choice(scenes)
    gesture = random.choice(SELFIE_GESTURES)
    light = random.choice(lights)
    base = (
        "【自拍 / 看看模式】展示 AI 现在的样子。"
        "第一人称自拍视角（自己举机或镜前自拍），不是别人代拍。"
        "必须看向镜头：眼睛对焦镜头方向，表情自然有神；不要眼神飘走或心不在焉。"
        "保持 AI 当前形象、今日穿搭与气质一致，脸部清晰。"
        "竖屏手机近景半身：像短视频封面那样拍得近，但质感仍是真实皮肤、真实布料和接触阴影；窗光或暖灯，不要棚拍、不要美颜滤镜、不要塑料皮肤。"
        f"本次机位：{SELFIE_SHOT_LINES.get(shot, SELFIE_SHOT_LINES['arm_half'])}"
        f"场景倾向：{scene}。小动作：{gesture}。光线：{light}。"
        "画面干净日常，不要夸张摆拍。"
    )
    if has_refs:
        base = "参考用户提供的图片氛围、场景或构图，" + base + " 主角身份仍以 AI 形象为准。"
    if extra:
        base += f" 用户补充要求优先：{extra}。"
    return base + f" 【shot:{shot}】"


def build_third_person_look_action(
    extra_request: str = "",
    has_refs: bool = False,
    *,
    avoid_shot: str = "",
    scene_light_provider: Callable[[str], Tuple[List[str], List[str]]] = period_scene_light_pools,
) -> str:
    shot_pool = [item for item in THIRD_PERSON_SHOT_POOL if item[0] != avoid_shot]
    if not shot_pool:
        shot_pool = list(THIRD_PERSON_SHOT_POOL)
    shot = random.choices(
        [name for name, _ in shot_pool],
        weights=[weight for _, weight in shot_pool],
        k=1,
    )[0]
    scenes, lights = scene_light_provider("look_you")
    scene = random.choice(scenes)
    action = random.choice(THIRD_PERSON_ACTIONS)
    light = random.choice(lights)
    base = (
        "【他拍 / 看看你模式】展示 AI 当前样子的自然日常照片。"
        "别人视角的单人成品照：镜头已经对准主角拍下，画面里只有主角一个人；拍摄者完全在画面外，不要第二个人，不要有人举着手机拍主角。"
        "正面半身或近景时优先看向镜头，眼神自然有焦点；可以轻松回头，但不要整段心不在焉。"
        "保持 AI 当前形象、今日穿搭和生活状态一致，脸部、穿搭、姿态、背景层次和光线都清晰自然。"
        "竖屏手机近景半身：窗光或暖灯，真实皮肤和真实布料，不要美颜滤镜、不要棚拍精修。"
        f"本次机位：{THIRD_PERSON_SHOT_LINES.get(shot, THIRD_PERSON_SHOT_LINES['half_front'])}"
        f"场景倾向：{scene}。动作瞬间：{action}。光线：{light}。"
        "写实手机拍照质感，不要影楼硬摆。"
    )
    if has_refs:
        base = "参考用户提供的图片氛围、场景或构图，" + base
    extra = re.sub(r"\s+", " ", str(extra_request or "")).strip(" 。")
    if extra:
        base += f" 用户补充要求优先：{extra}。"
    return base + f" 【shot:{shot}】"


def build_group_selfie_action(
    extra_request: str = "",
    has_refs: bool = False,
    *,
    appearance_type: str = "auto",
) -> str:
    appearance_line = appearance_type_instruction(
        appearance_type, has_reference_image=True
    )
    style_blob = " ".join(group_style_lines(appearance_type))
    base = (
        "合影 / 合照 / 同框。AI 自己必须作为画面主角之一，与用户指定或提供的对象自然同框合影。"
        "AI 保持当前身份：性别、脸型五官、发型发色、体态一致；表情与眼神按本次合影氛围自然重画，不要原样复制形象参考图的固定表情。"
        "形象参考图是女性则 AI 必须是女性，是男性则必须是男性；禁止无故改成异性。"
        "如果同一张参考图里有多个可见人物 / 角色，按实际可见人数全部保留为独立同框对象。"
    )
    if appearance_line:
        base += appearance_line
    base += style_blob
    base += (
        "所有同框对象处在同一场景中，站位或坐位自然，视线、距离、遮挡、互动统一。"
        "合影默认多数人看向镜头；竖屏手机近景半身，窗光或暖灯，真实皮肤，不要美颜滤镜、不要证件照站排。"
        "AI 若面向镜头，优先与镜头有眼神交流，表情自然生动，不要心不在焉或整脸僵住参考图表情。"
    )
    if has_refs:
        base += (
            " 用户提供或艾特对象的图片是合影角色来源，必须拟人/改画成与主角同一画风的独立完整人物；"
            "玩偶、毛绒玩具、吉祥物、卡通立牌、表情包、道具等只取配色气质线索，禁止原样保留玩具外形、平面简笔画肢体，"
            "禁止贴在主角衣服上或与身体衣物粘连融合；非人物按风格拟人，无性别默认女性。"
        )
    else:
        base += " 没有合影对象参考图时，按文字要求生成自然同框完整人物；未指定性别时默认成年女性。"
    extra = re.sub(r"\s+", " ", str(extra_request or "")).strip(" 。")
    if extra:
        base += f" 用户补充要求：{extra}。"
    return base


def looks_like_group_selfie_intent(text: str) -> bool:
    value = str(text or "")
    compact = re.sub(r"[\s，。！？、；：,.!?]", "", value.lower())
    compact_keywords = (
        "合影", "合照", "同框", "一起拍", "一起照", "和我", "跟我", "与我", "陪我",
        "和你", "跟你", "与你", "你和我", "我和你", "我们一起", "groupselfie",
        "groupphoto", "phototogether", "takeaphototogether", "takeapicturetogether",
        "sameframe", "inthesameframe", "sidebyside", "standingnextto", "twous", "ustogether",
    )
    if any(keyword in compact for keyword in compact_keywords):
        return True
    low = value.lower()
    phrase_keywords = (
        "group selfie", "group photo", "photo together", "take a photo together",
        "take a picture together", "same frame", "in the same frame", "side by side",
        "standing next to", "two of us", "us together", "with me", "with you",
    )
    for keyword in phrase_keywords:
        pattern = r"(?<![a-z0-9])" + re.escape(keyword).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
        if re.search(pattern, low):
            return True
    return False


def looks_like_selfie_intent(text: str, *, bot_name: str = "") -> bool:
    value = str(text or "")
    low = value.lower()
    keywords = [
        "自拍", "合影", "合照", "同框", "形象照", "和我", "跟我", "与我", "陪我",
        "和你", "跟你", "与你", "你和我", "我和你", "我们一起", "一起拍", "一起照",
        "你自己", "你的照片",
    ]
    english_keywords = [
        "selfie", "group selfie", "group photo", "photo together", "take a photo together",
        "take a picture together", "together with me", "with me", "with you", "next to me",
        "next to you", "standing next to", "side by side", "same frame", "in the same frame",
        "two of us", "us together", "your photo", "yourself", "ai assistant", "catgirl", "ahwu",
    ]
    name = str(bot_name or "").strip()
    if name:
        keywords.append(name)
        english_keywords.append(name.lower())
    return any(keyword and keyword in value for keyword in keywords) or any(
        keyword and keyword in low for keyword in english_keywords
    )
