"""Prompt builders and intent checks for selfie-style image commands."""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from typing import List, Tuple

from ..features.persona import (
    appearance_type_instruction,
    appearance_type_output_instruction,
    current_period,
    group_style_lines,
)
from .prompt_templates import resolve_appearance_type


SELFIE_SHOT_POOL = (
    ("arm_half", 3),
    ("mirror_half", 2),
    ("mirror_full", 2),
    ("window_side", 2),
    ("desk_sit", 2),
    ("sofa_casual", 2),
    ("high_angle", 1),
    ("close_portrait", 1),
    ("back_view", 1),
    ("side_profile", 1),
)

SELFIE_SHOT_LINES = {
    "arm_half": "手机自拍臂中近景：手臂自然伸向镜头，头部到腰线入镜，上衣版型与搭配层次清楚，表情松弛。",
    "mirror_half": "镜前中近景自拍：头部到胯部上方入镜，手机不挡住主要服装，镜中自然看向手机镜头，展示今日穿搭的上下层次。",
    "mirror_full": "全身镜前穿搭自拍：头部、上衣、下装和鞋履完整入镜，手机自然持握且不遮挡衣服，人物与倒影比例一致。",
    "window_side": "窗边侧光自拍：身体略侧，头部自然转向镜头，头部到腰线或胯部上方入镜，衣服层次清楚。",
    "desk_sit": "书桌前坐姿自拍：略俯视中近景，头部到腰部入镜，可自然扶桌沿，今日上衣和配饰清晰。",
    "sofa_casual": "沙发上随手自拍：靠垫只在背景边缘，头部到腰线入镜，穿搭不被抱枕遮挡，姿态放松。",
    "high_angle": "稍高机位自拍：镜头略高于眼，轻微向下拍到腰线，脸自然抬一点看镜头，显精神，不要过度俯拍变形。",
    "close_portrait": "近景胸像自拍：脸与肩为主，眼神对焦镜头，五官清晰，浅景深。",
    "back_view": "背面自拍构图：主角背对镜头，肩胯和头部朝向一致，不回头、不回眸、不扭颈；双手自然垂放，只有场景内已有合适支撑物时才自然搭放。",
    "side_profile": "侧身自拍：身体和头部自然朝向同一侧，肩胯约四十五度侧向镜头，不回头硬露正脸，颈部和肩线保持自然。",
}

SELFIE_GESTURES = (
    "嘴角轻微笑意",
    "一只手整理发丝或衣领",
    "托腮听人说话的轻松感",
    "比个很小的 OK 或比心（含蓄）",
    "双手捧杯刚抬眼",
    "刚坐好整理袖口",
)

SELFIE_CANDID_FINISHES = (
    "手机自动曝光略偏亮，局部高光稍微发白，但脸和服装颜色仍清楚",
    "取景没有完全居中，画框有一点点歪，保留普通人临时举手机的感觉",
    "轻微手持抖动只落在画面边缘，眼睛、脸部和穿搭主体保持清晰",
    "前置镜头白平衡稍偏冷，暗部有一点细颗粒，不加明显滤镜",
)

SELFIE_MIRROR_FINISHES = (
    "镜中取景略偏一侧，留白不太均匀，但人物和衣服轮廓完整",
    "手机直出色彩稍淡，室内光线不完全均匀，穿搭细节仍清楚可辨",
)

THIRD_PERSON_SHOT_POOL = (
    ("half_front", 3),
    ("three_quarter", 3),
    ("env_mid", 2),
    ("walk_candid", 1),
    ("close_smile", 2),
    ("low_over", 1),
    ("lean_wall", 2),
    ("back_view", 1),
    ("side_profile", 1),
)

THIRD_PERSON_SHOT_LINES = {
    "half_front": "半身平视他拍：别人视角的单人半身照，AI 正面或微侧看镜头，眼神有焦点。",
    "three_quarter": "三分之四侧身他拍：身体略侧，头部顺着身体方向自然转向，不做突然回头动作。",
    "env_mid": "中远景环境人像：人物与场景都清楚，AI 仍是主体，自然看向镜头或刚抬头。",
    "walk_candid": "走路抓拍：侧前方跟随感，步伐自然，可看镜头一瞬，生活感强。",
    "close_smile": "近景胸像：脸与肩，浅景深，对镜头带一点笑意，五官清晰。",
    "low_over": "轻微低机位侧前方拍摄半身：头颈保持自然，不做过肩回望，不要过度仰拍变形。",
    "lean_wall": "靠墙/门框他拍：肩背轻靠，双手自然，看镜头，竖构图友好。",
    "back_view": "背面他拍构图：人物背对镜头，肩胯和头部朝向一致，不回头、不回眸、不扭颈；双手放松垂在身侧，只有场景内已有合适支撑物时才自然搭放。",
    "side_profile": "纯侧身他拍：身体、肩线和面部朝向一致，以自然侧面或侧后三分之四角度呈现，不回头、不为露正脸扭转脖子。",
}

THIRD_PERSON_ACTIONS = (
    "端着杯子刚抬眼",
    "托腮听人说话",
    "整理袖口或发丝",
    "翻书停顿抬头",
    "双手插兜靠站",
    "拎袋子自然侧身停步",
    "刚坐下整理衣角",
    "对镜头轻轻点头笑",
)

THIRD_PERSON_SIDE_ACTIONS = (
    "端杯自然停留，视线顺着身体方向",
    "整理袖口或发丝，不转头看镜头",
    "翻书继续阅读，肩线保持侧向",
    "靠墙放松站立，手臂自然下垂",
    "行走中自然停步，头部顺着行进方向",
)

SELFIE_ENVIRONMENT_TERMS = (
    "窗边", "窗前", "窗户旁", "书桌", "桌前", "阳台", "镜前", "沙发", "床边",
    "咖啡馆", "咖啡店", "咖啡厅", "街边", "树荫", "书店", "夜灯房间", "厨房",
    "卧室", "客厅", "室内", "室外", "街道", "雨夜", "霓虹", "海边", "沙滩",
    "校园", "教室", "走廊", "车站", "雪地", "樱花", "河边", "天台", "屋顶",
    "楼梯", "浴室", "温泉", "舞台", "草地", "花丛", "竹林", "和室", "地毯",
    "场景不变", "背景不变", "保持场景", "保持背景",
)

SELFIE_LIGHT_TERMS = (
    "光线", "灯光", "日光", "阳光", "窗光", "逆光", "侧光", "柔光", "暖光",
    "冷光", "闪光灯", "过曝", "阴天", "昏暗", "霓虹",
)


def _requested_staging_orientation(text: str) -> str:
    compact = re.sub(r"\s+", "", str(text or ""))
    if any(_has_positive_staging_term(compact, term) for term in ("背对镜头", "背向镜头", "背影", "背身")):
        return "back"
    if any(_has_positive_staging_term(compact, term) for term in ("侧身", "侧面", "侧后方", "侧前方")):
        return "side"
    return ""


def _has_positive_staging_term(text: str, term: str) -> bool:
    start = 0
    while True:
        index = text.find(term, start)
        if index < 0:
            return False
        prefix = text[max(0, index - 6) : index]
        if not re.search(
            r"(?:不要|不需要|不必|不想|不允许|不能|不会|不可|别|不|避免|禁止|拒绝)(?:再|去|在|用|选择|采用|进行)*$",
            prefix,
        ):
            return True
        start = index + len(term)


def _has_explicit_staging_scene(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    if "场景随机" in compact or "背景随机" in compact:
        return False
    return any(_has_positive_staging_term(compact, term) for term in SELFIE_ENVIRONMENT_TERMS)


def _has_explicit_staging_light(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    return any(_has_positive_staging_term(compact, term) for term in SELFIE_LIGHT_TERMS)


def _scene_contains(scene: str, *terms: str) -> bool:
    compact = re.sub(r"\s+", "", str(scene or "")).lower()
    return any(term.lower() in compact for term in terms)


def _scene_supports_shot(scene: str, shot: str) -> bool:
    """Keep a randomly selected shot grounded in the selected environment."""
    if shot in {"mirror_half", "mirror_full"}:
        return _scene_contains(scene, "镜", "mirror")
    if shot == "window_side":
        return _scene_contains(scene, "窗", "window", "阳台")
    if shot == "desk_sit":
        return _scene_contains(scene, "桌", "书桌", "table", "咖啡馆", "咖啡店")
    if shot == "sofa_casual":
        return _scene_contains(scene, "沙发", "床", "榻", "sofa", "bed")
    if shot == "lean_wall":
        return _scene_contains(scene, "墙", "门框", "窗框", "立柱", "wall", "door")
    if shot == "walk_candid":
        return _scene_contains(
            scene,
            "街", "树荫", "校园", "走廊", "车站", "海边", "沙滩", "河边",
            "天台", "屋顶", "楼梯", "草地", "花丛", "竹林", "street", "park",
        )
    if shot in {"mirror_half", "mirror_full", "window_side", "desk_sit", "sofa_casual", "lean_wall", "walk_candid"}:
        return False
    return True


def _pick_staging_scene(scenes: List[str], orientation: str = "", shot: str = "") -> str:
    compatible = [scene for scene in scenes if not shot or _scene_supports_shot(scene, shot)]
    if orientation == "back":
        compatible = [scene for scene in compatible if "镜" not in scene]
    return random.choice(compatible or scenes)


def _shots_supported_by_scenes(
    shot_pool: List[Tuple[str, int]],
    scenes: List[str],
    explicit_scene_text: str = "",
) -> List[Tuple[str, int]]:
    source = [explicit_scene_text] if explicit_scene_text else list(scenes)
    compatible = [
        item for item in shot_pool
        if any(_scene_supports_shot(scene, item[0]) for scene in source)
    ]
    return compatible or shot_pool


def _selfie_gestures_for_scene(scene: str, shot: str) -> List[str]:
    gestures = list(SELFIE_GESTURES)
    if not _scene_contains(scene, "杯", "咖啡馆", "咖啡店", "厨房", "cup", "cafe"):
        gestures = [gesture for gesture in gestures if "捧杯" not in gesture]
    if shot in {"mirror_half", "mirror_full"}:
        gestures = [gesture for gesture in gestures if "捧杯" not in gesture]
    return gestures or ["嘴角轻微笑意"]


def _third_person_actions_for_scene(scene: str, shot: str) -> List[str]:
    if shot == "lean_wall":
        return ["靠墙或门框放松站立，手臂自然下垂", "整理袖口或发丝，肩背轻靠支撑面"]
    if shot == "walk_candid":
        return ["行走中自然停步，头部顺着行进方向", "拎袋子自然侧身停步，脚步刚好被捕捉"]
    actions = list(THIRD_PERSON_SIDE_ACTIONS if shot == "side_profile" else THIRD_PERSON_ACTIONS)
    if not _scene_contains(scene, "杯", "咖啡馆", "咖啡店", "厨房", "cup", "cafe"):
        actions = [action for action in actions if "杯" not in action]
    if not _scene_contains(scene, "书店", "书桌", "图书", "阅读", "book", "desk"):
        actions = [action for action in actions if "翻书" not in action]
    return actions or ["整理袖口或发丝"]


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
        return ["浅色墙与窗边", "书桌与杯具", "沙发与抱枕", "阳台栏杆旁", "镜前整洁台面"], ["窗光柔和", "阴天漫射"]
    if period == "evening":
        return ["暖灯房间一角", "沙发与抱枕", "书桌与杯具", "床边靠坐", "镜前整洁台面"], ["暖黄台灯", "窗光柔和"]
    return ["暖灯房间一角", "沙发与抱枕", "床边靠坐", "镜前整洁台面"], ["暖黄台灯"]


def looks_like_crop_waist_request(text: str) -> bool:
    raw = str(text or "")
    if not raw.strip():
        return False
    if any(
        key in raw
        for key in (
            "漏腰", "露腰", "露脐短上衣", "小蛮腰", "crop_waist",
            "crop top", "short top", "短上衣", "下摆缩短", "露出自然腰线", "露出腰线",
        )
    ):
        return True
    low = raw.lower()
    has_inner = (
        "露脐" in raw
        or "短上衣" in raw
        or "下摆缩短" in raw
        or "露出自然腰线" in raw
        or "露出腰线" in raw
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
    extra_request: str = "",
    has_refs: bool = False,
    *,
    appearance_type: str = "auto",
) -> str:
    appearance_type = resolve_appearance_type(appearance_type, extra_request)
    base = (
        "【自拍 / 漏腰模式】"
        "以当前 AI 形象为主角的居家休闲自拍成片，室内光线柔和偏暗，角度略高。"
        "唯一调整是将原有上衣的下摆缩短至露出自然腰线的长度，"
        "原有服装的款式、颜色、材质、领口、袖型、图案、配饰、层次和穿着方式全部保持不变，"
        "保持下装、人物身份、发型、脸部、表情、姿势、镜头和场景不变。"
        "放松地坐着或靠在深色沙发/床边，头发略显凌乱，像日常随意拍的照片。"
        "保持 AI 身份长相与发色一致，画面干净得体。"
    )
    if appearance_type == "real":
        base += "真人摄影质感，保留自然皮肤纹理、布料质感与接触阴影。"
    elif appearance_type == "anime":
        base += "统一为柔光京阿尼风格动漫插画，线条、上色、光影与动漫五官保持一致。"
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
    appearance_type: str = "auto",
    scene_light_provider: Callable[[str], Tuple[List[str], List[str]]] = period_scene_light_pools,
) -> str:
    extra = re.sub(r"\s+", " ", str(extra_request or "")).strip(" 。")
    appearance_type = resolve_appearance_type(appearance_type, extra)
    if looks_like_crop_waist_request(extra):
        return build_crop_waist_selfie_action(
            extra,
            has_refs,
            appearance_type=appearance_type,
        )
    scenes, lights = scene_light_provider("selfie")
    explicit_scene = _has_explicit_staging_scene(extra)
    shot_pool = [item for item in SELFIE_SHOT_POOL if item[0] != avoid_shot]
    if not shot_pool:
        shot_pool = list(SELFIE_SHOT_POOL)
    shot_pool = _shots_supported_by_scenes(shot_pool, scenes, extra if explicit_scene else "")
    orientation = _requested_staging_orientation(extra)
    shot = (
        "back_view" if orientation == "back"
        else "side_profile" if orientation == "side"
        else random.choices(
            [name for name, _ in shot_pool],
            weights=[weight for _, weight in shot_pool],
            k=1,
        )[0]
    )
    explicit_light = _has_explicit_staging_light(extra)
    if shot == "back_view":
        scene = "" if explicit_scene else _pick_staging_scene(scenes, "back", shot)
        gesture = "双手自然垂放；场景中已有合适支撑物时可轻轻搭放"
        light = "" if explicit_light else random.choice(lights)
    else:
        scene = "" if explicit_scene else _pick_staging_scene(scenes, orientation, shot)
        gesture_scene = scene or next(
            (item for item in scenes if _scene_supports_shot(item, shot)),
            "",
        )
        gesture = random.choice(_selfie_gestures_for_scene(gesture_scene, shot))
        light = "" if explicit_light else random.choice(lights)
    rear_view = shot == "back_view"
    side_view = shot == "side_profile"
    full_mirror_view = shot == "mirror_full"
    capture_finishes = SELFIE_MIRROR_FINISHES if shot in {"mirror_half", "mirror_full"} else SELFIE_CANDID_FINISHES
    capture_finish = random.choice(capture_finishes) if not extra else ""
    selfie_framing = (
        "竖屏全身镜面自拍：人物从头到鞋完整入镜，手机不遮挡今日穿搭；"
        if full_mirror_view
        else "竖屏手机中近景半身：画面至少到腰线，优先看清今日上衣和搭配层次；"
    )
    base = (
        "【自拍 / 看看模式】展示 AI 现在的样子。"
        + (
            "由主角自行启动手机定时拍摄，拍摄者不入画；这是自摄成片，不要求出现手机或自拍手臂。"
            "主角背对镜头，肩胯和头部朝向一致，不回头、不回眸、不扭颈，也不要求露出正脸；双手放松垂在身侧，不凭空增加栏杆、手机或其他道具。"
            "保持竖屏半身中近景与自然透视，重点呈现背面发型和当前穿搭；不要美颜滤镜、不要棚拍精修。"
            if rear_view
            else (
                "身体侧向镜头的自拍构图：肩胯和头部朝向自然连贯，不回头硬露正脸；表情放松，保持面部自然可见。"
                "保持 AI 当前形象、今日穿搭与气质一致，脸部清晰。"
                "竖屏手机侧身半身中近景；"
                + ("" if explicit_light else "窗光或暖灯；")
                + "不要美颜滤镜、不要棚拍精修。"
                if side_view
                else "第一人称自拍视角（自己举机或镜前自拍），不是别人代拍。"
                "必须看向镜头：眼睛对焦镜头方向，表情自然有神；不要眼神飘走或心不在焉。"
                "保持 AI 当前形象、今日穿搭与气质一致，脸部清晰。"
                + selfie_framing
                + ("" if explicit_light else "窗光或暖灯；")
                + "不要美颜滤镜、不要棚拍精修。"
            )
        )
        + f"本次机位：{SELFIE_SHOT_LINES.get(shot, SELFIE_SHOT_LINES['arm_half'])}"
        + (f"场景倾向：{scene}。" if scene else "")
        + f"小动作：{gesture}。"
        + (f"光线：{light}。" if light else "")
        + (f"手机随手拍质感：{capture_finish}。" if capture_finish else "")
        + "场景和镜头不得改变、增添或删减今日穿搭；服装的廓形、颜色和层次保持清楚。"
        + "画面干净日常，不要夸张摆拍。"
    )
    if appearance_type == "real":
        base += "真人摄影质感：保留自然皮肤纹理、真实布料与接触阴影，避免塑料皮肤。"
    elif appearance_type == "anime":
        base += "柔光京阿尼风格动漫插画：线条、上色、光影与动漫五官统一，不生成真人皮肤纹理。"
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
    appearance_type: str = "auto",
    scene_light_provider: Callable[[str], Tuple[List[str], List[str]]] = period_scene_light_pools,
) -> str:
    scenes, lights = scene_light_provider("look_you")
    explicit_scene = _has_explicit_staging_scene(extra_request)
    shot_pool = [item for item in THIRD_PERSON_SHOT_POOL if item[0] != avoid_shot]
    if not shot_pool:
        shot_pool = list(THIRD_PERSON_SHOT_POOL)
    shot_pool = _shots_supported_by_scenes(
        shot_pool,
        scenes,
        extra_request if explicit_scene else "",
    )
    orientation = _requested_staging_orientation(extra_request)
    shot = (
        "back_view" if orientation == "back"
        else "side_profile" if orientation == "side"
        else random.choices(
            [name for name, _ in shot_pool],
            weights=[weight for _, weight in shot_pool],
            k=1,
        )[0]
    )
    explicit_light = _has_explicit_staging_light(extra_request)
    if shot == "back_view":
        scene = "" if explicit_scene else _pick_staging_scene(scenes, "back", shot)
        action = "双手放松垂在身侧；场景中已有合适支撑物时可轻轻搭放"
        light = "" if explicit_light else random.choice(lights)
    else:
        scene = "" if explicit_scene else _pick_staging_scene(scenes, orientation, shot)
        action_scene = scene or next(
            (item for item in scenes if _scene_supports_shot(item, shot)),
            "",
        )
        action = random.choice(_third_person_actions_for_scene(action_scene, shot))
        light = "" if explicit_light else random.choice(lights)
    appearance_type = resolve_appearance_type(appearance_type, extra_request)
    rear_view = shot == "back_view"
    side_view = shot == "side_profile"
    base = (
        "【他拍 / 看看你模式】展示 AI 当前样子的自然日常成片。"
        "别人视角的单人成品照：镜头已经对准主角拍下，画面里只有主角一个人；拍摄者完全在画面外，不要第二个人，不要有人举着手机拍主角。"
        + (
            "本次明确采用背面构图：人物背对镜头，肩胯和头部朝向一致，不回头、不回眸、不扭颈；双手放松垂在身侧，不凭空增加支撑物，优先展示发型和服装后侧。"
            "竖屏半身中近景，透视自然，拍摄者留在画面外；不要美颜滤镜、不要影楼精修。"
            if rear_view
            else (
                "本次采用纯侧身构图：身体、肩线和头部朝向一致，不回头或扭颈硬露正脸。"
                "保持 AI 当前形象、今日穿搭和生活状态一致，采用竖屏半身中近景，姿态、背景层次和光线自然清楚。"
                if side_view
                else (
                    "正面构图时可自然看向镜头；侧面构图时头部顺着身体方向，不强行回头露正脸。"
                    "保持 AI 当前形象、今日穿搭和生活状态一致，脸部、穿搭、姿态、背景层次和光线都清晰自然。"
                    "竖屏手机近景半身："
                    + ("" if explicit_light else "窗光或暖灯，")
                    + "构图干净自然，不要美颜滤镜、不要影楼精修。"
                )
            )
        )
        + f"本次机位：{THIRD_PERSON_SHOT_LINES.get(shot, THIRD_PERSON_SHOT_LINES['half_front'])}"
        + (f"场景倾向：{scene}。" if scene else "")
        + f"动作瞬间：{action}。"
        + (f"光线：{light}。" if light else "")
        + "生活化随手记录感，不要影楼硬摆。"
    )
    if appearance_type == "real":
        base += "真人摄影质感：保留自然皮肤纹理、真实布料与接触阴影。"
    elif appearance_type == "anime":
        base += "柔光京阿尼风格动漫插画：线条、上色、光影与动漫五官统一，不生成真人皮肤纹理。"
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
    avoid_shot: str = "",
    appearance_type: str = "auto",
) -> str:
    orientation = _requested_staging_orientation(extra_request)
    if orientation == "back":
        group_shot = "back_view"
    elif orientation == "side":
        group_shot = "side_profile"
    else:
        shot_options = [
            ("group_portrait", 5),
            ("group_environment", 3),
            ("back_view", 1),
        ]
        available_options = [item for item in shot_options if item[0] != avoid_shot]
        if not available_options:
            available_options = shot_options
        group_shot = random.choices(
            [name for name, _ in available_options],
            weights=[weight for _, weight in available_options],
            k=1,
        )[0]
    appearance_type = resolve_appearance_type(appearance_type, extra_request)
    explicit_light = _has_explicit_staging_light(extra_request)
    appearance_line = appearance_type_instruction(
        appearance_type,
        has_reference_image=True,
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
    skin_line = "真实皮肤，" if appearance_type == "auto" else ""
    if group_shot == "back_view":
        group_staging = (
            "本次合影采用背面构图：主角背对镜头，头部与肩胯朝向一致，不回头、不回眸、不扭颈；"
            "采用竖屏中远景，保证所有同框对象自然入镜；双手放松垂在身侧，不凭空增加支撑物；"
            "其他同框对象按用户提供的实际人数和站位自然安排，不增删人物。"
        )
    elif group_shot == "side_profile":
        group_staging = (
            "本次主角采用自然侧身构图：身体、肩线和头部朝向一致，不回头硬露正脸；"
            "采用竖屏中景，保证所有同框对象自然入镜；其他同框对象按用户提供的实际人数和站位自然安排，不增删人物。"
        )
    elif group_shot == "group_environment":
        group_staging = "本次采用竖屏中远景环境合影构图，所有同框对象和周围场景都清楚，保持对象数量与身份不变。"
    else:
        group_staging = "本次采用自然竖屏近景合影构图，保持所有同框对象数量与身份不变。"
    base += (
        "所有同框对象处在同一场景中，站位或坐位自然，视线、距离、遮挡、互动统一。"
        + (
            "正面合影时可自然看向镜头；人物侧身或背对镜头时，头部顺着身体方向，不强迫回头或露出正脸。"
            if group_shot in {"back_view", "side_profile"}
            else (
                f"合影默认多数人看向镜头；{skin_line}"
                + ("" if explicit_light else "窗光或暖灯，")
                + "不要美颜滤镜、不要证件照站排。"
                + (
                    "所有人保持中远景可辨识，留出足够环境空间。"
                    if group_shot == "group_environment"
                    else "竖屏手机近景半身。"
                )
            )
        )
        + "AI 若面向镜头，优先与镜头有眼神交流，表情自然生动，不要心不在焉或整脸僵住参考图表情。"
        + group_staging
    )
    if appearance_type == "real":
        base += "皮肤、光线与材质保持自然写实摄影质感。"
    elif appearance_type == "anime":
        base += "皮肤、线条、上色与光影都保持统一动漫插画质感，不生成真人皮肤纹理。"
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
    final_style_instruction = appearance_type_output_instruction(appearance_type)
    if final_style_instruction:
        base += final_style_instruction
    return base + f" 【shot:{group_shot}】"


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
        "你自己", "你的照片", "看看cos", "看看腿", "看腿", "看看你", "看下你",
        "看看现在的你", "看看你现在的样子",
    ]
    english_keywords = [
        "selfie", "group selfie", "group photo", "photo together", "take a photo together",
        "take a picture together", "together with me", "with me", "with you", "next to me",
        "next to you", "standing next to", "side by side", "same frame", "in the same frame",
        "two of us", "us together", "your photo", "yourself", "ai assistant", "catgirl", "ahwu",
    ]
    explicit = any(keyword and keyword.lower() in low for keyword in keywords) or any(
        keyword and keyword in low for keyword in english_keywords
    )
    if explicit:
        return True

    # A bot name is not enough to select the persona/selfie pipeline.  LLM
    # image prompts commonly mention the character name in a design sheet,
    # portrait, or story illustration request.  Only treat a name as a
    # selfie cue when it is paired with an unambiguous photo relationship.
    name = str(bot_name or "").strip()
    if not name:
        return False
    name_pos = value.find(name)
    if name_pos < 0:
        name_pos = low.find(name.lower())
    if name_pos < 0:
        return False
    nearby = value[max(0, name_pos - 18) : name_pos + len(name) + 18]
    relationship_tokens = (
        "自拍", "拍照", "照片", "形象照", "看镜头", "对镜", "换装", "穿上",
        "和我", "跟我", "与我", "陪我", "同框", "合影", "合照",
        "selfie", "photo", "portrait", "outfit", "wear", "with me", "next to me",
    )
    return any(token in nearby.lower() for token in relationship_tokens)
