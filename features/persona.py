"""AI selfie persona management."""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

from ..cos.cos_looks import (
    adapt_cos_prompt_for_appearance,
    parse_requested_cos_camera,
    requests_phone_face_cover,
)
from ..cos.leg_focus import (
    ensure_leg_focus_action,
    get_leg_focus_outfit,
    has_leg_focus_contract,
    is_leg_calf_crop_action,
)
from ..core.utils import detect_mime_by_bytes, ext_from_mime, load_json_file, save_json_file
from ..prompts.prompt_templates import (
    COS_IDENTITY_CONTRACT_ZH,
    has_viewpoint_change_request,
    resolve_appearance_type,
)


APPEARANCE_TYPES = ("auto", "real", "anime")
APPEARANCE_TYPE_LABELS = {
    "auto": "自动",
    "real": "真人",
    "anime": "动漫",
}

DAILY_PERIOD_KEYS = ("morning", "noon", "afternoon", "evening", "night", "late_night")
SELFIE_SNAPSHOT_PRESETS = (
    "公寓玄关的手机前置自拍，门框和鞋柜只作为普通背景；手臂自然伸向镜头，竖屏从头部拍到大腿上部，人物稍偏一侧，画框轻微歪斜。",
    "家中窗边的手机前置自拍，保留一点窗光造成的局部偏亮；人物不完全居中，肩部自然靠近画面边缘，脸和当前穿搭仍清晰。",
    "坐在客厅沙发上的手机前置自拍，靠垫和毯子留在背景；机位略高于坐姿眼平，头顶留白稍多，只有轻微手抖感，不模糊脸和衣服。",
    "书桌旁的手机前置自拍，桌面只占少量画面下缘；室内白光让色温稍冷，构图不完全端正，保持头部到腰部以上的自然近景。",
    "普通住宅穿衣镜前的全身镜面自拍，手机自然举起但不遮挡穿搭主体；镜中只有一个主角和一部手机，倒影结构一致，人物从头到鞋完整入镜，取景略偏而不倾斜变形。",
)


def normalize_appearance_type(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "": "auto",
        "auto": "auto",
        "automatic": "auto",
        "默认": "auto",
        "自动": "auto",
        "real": "real",
        "realistic": "real",
        "photo": "real",
        "真人": "real",
        "写实": "real",
        "anime": "anime",
        "cartoon": "anime",
        "2d": "anime",
        "动漫": "anime",
        "二次元": "anime",
        "动画": "anime",
    }
    if raw in aliases:
        return aliases[raw]
    return raw if raw in APPEARANCE_TYPES else "auto"


def appearance_type_instruction(
    appearance_type: str,
    *,
    has_reference_image: bool = False,
    is_cos: bool = False,
) -> str:
    kind = normalize_appearance_type(appearance_type)
    gender_lock = (
        "性别、年龄感与同一人身份必须跟随参考图一：参考图是女性就保持女性，是男性就保持男性；"
        "禁止无故改成异性、少年男主或路人男。"
        if has_reference_image
        else "若角色设定未写明性别，默认成年女性；禁止无故改成男性。"
    )
    if kind == "real":
        return (
            "形象类型：真人。主角形象是真人；最终成像媒介保持写实真人摄影与真人五官体态；自拍和 COS 只决定镜头、动作、服装与场景，不改变成像媒介。"
            + ("COS 池中的角色和服装描述照常执行。" if is_cos else "")
            + gender_lock
        )
    if kind == "anime":
        return (
            "形象类型：动漫。主角形象是动漫人物；最终成像媒介保持统一的 2D 柔光京阿尼风格动漫插画与动漫五官体态；自拍、照片、摄影和 COS 只描述镜头构图、动作、服装与场景，不代表真人摄影；"
            "脸更细，气质更仙风；"
            "即使参考图偏写实，也只保留同一人的身份特征并改画成动漫版，不要换成另一个角色；不得混合真人照片与动漫画风。"
            + ("COS 池中的真人 COSER 只表示角色扮演内容，按动漫人物绘制。" if is_cos else "")
            + gender_lock
        )
    if has_reference_image:
        return (
            "形象类型：自动。主角的视觉媒介、质感和人物比例以参考图一为唯一主形象依据；"
            "额外参考图只提供同框对象或用户明确指定的服装、姿势、构图和场景，不得改变主角媒介，不得把主角改成另一种画风。"
            + gender_lock
        )
    return gender_lock if has_reference_image else ""


def appearance_type_output_instruction(appearance_type: str) -> str:
    kind = normalize_appearance_type(appearance_type)
    if kind == "real":
        return "最终画面统一为写实真人照片；如提示词提到动漫或游戏角色，只保留其服装、配色和造型，不生成插画或混合画风。"
    if kind == "anime":
        return "最终画面统一为 2D 柔光京阿尼风格动漫插画；自拍、手机、照片、摄影和 COS 仅控制构图与内容，不生成真人摄影或混合画风。"
    return ""


def group_style_lines(appearance_type: str) -> list[str]:
    kind = normalize_appearance_type(appearance_type)
    if kind == "anime":
        return [
            "整张合影统一为柔光京阿尼风格动漫：柔和散射光、细腻脸部、偏仙风气质；线条、上色、光影与头身比例一致。",
            "脸型更细、五官更精致，氛围干净通透，带一点仙气，不要粗线条厚涂或夸张漫画脸。",
            "额外参考若是真人照片：只提取身份线索（发色发型、配色、饰品、服装色块、体态倾向），改画成与主角同一套柔光京阿尼动漫人物，不要继续写实摄影质感。",
            "二次元头像、插画、表情包、卡通、吉祥物、玩偶、毛绒玩具：只取配色/发型/气质线索，拟人成可并肩站立的完整动漫人物，禁止原样保留玩具/玩偶外形。",
            "风景、建筑、房间、道具等非人物参考：按主色与气质拟人成可并肩站立的完整动漫人物（成年、得体、日常），不要只当背景贴图，也不要原样塞进画面。",
            "拟人无明确性别时默认成年女性；有明确性别线索则按对应性别。",
            "同框必须是独立完整人物：与主角身体/衣物分离，边界清晰，禁止与浴袍/身体粘连融合。",
            "同框人物站位自然，互动友好；整张图像同一场景下的一张柔光京阿尼动漫合影，不要一半真人一半二次元。",
        ]
    if kind == "real":
        return [
            "整张合影统一为真实相机拍下的写实照片：光线、色调、景深与画风一致。",
            "真人照片参考：保留可见人数、大致脸型五官倾向、发型发色、穿搭轮廓、体态站位与相对关系。",
            "二次元 / 动漫头像 / 插画 / Q版 / 表情包 / 卡通 / 吉祥物 / 玩偶 / 毛绒玩具参考：只提取身份线索（性别气质、发色发型、配色、饰品、服装色块），改画成与主角同一套写实真人照片风格的成年人物；禁止继续二次元大眼、平涂、赛璐璐、漫画线稿、Q 版头身，也禁止原样保留玩具/玩偶外形。",
            "风景、建筑、房间、道具、纯色块等非人物图：按主色、线条、材质与气质拟人成可并肩站立的完整写实人物（成年、得体、自然），再与主角同框；不要把原图原样铺成背景或塞进怀里。",
            "拟人性别：参考图或文字已明确性别则按该性别；若无明确性别线索，默认拟人为成年女性，气质柔和好看、得体日常，不要默认男性。",
            "同框必须是独立完整人物：与主角身体/衣物分离，边界清晰，禁止与浴袍/身体粘连融合。",
            "同框对象统一写实；不要一半真人一半二次元。",
        ]
    return [
        "自动媒介策略：主形象参考图是整张合影的主角媒介基线；若主形象是真人照片，主角和场景保持写实摄影质感。",
        "额外参考图只作为独立同框对象或用户明确指定的服装、姿势、构图和场景来源，不得改变主角媒介，不得把主角改成另一种画风。",
        "额外参考图即使是动漫、插画、卡通或玩偶，也只保留其对象身份线索并按主形象媒介处理；除非用户明确指定，不要把整张图动漫化。",
        "额外参考图中的人物、角色、玩偶或非人物对象都要拟人为独立完整同框人物，不要只当背景贴图；无明确性别时默认成年女性。",
        "同框人物必须是独立完整人物，与主角身体/衣物分离，边界清晰，站位自然，互动友好。",
    ]


def anatomy_constraint_lines(*, style: str = "general") -> list[str]:
    """Shared body-part constraints for selfie/draw prompts.

    Keep wording positive and mild. Avoid injury terms (断臂/幽灵手) and
    avoid the bare token「同框」(it falsely triggers group-photo intent).
    """
    if style == "en":
        return [
            "Keep natural complete anatomy: when hands are visible, show exactly one left hand and one right hand; when feet are visible, show exactly one left foot and one right foot.",
            "Each visible hand should connect continuously through shoulder, elbow, and wrist in the same image; do not invent extra hands or disconnected hands near the legs.",
            "Keep left/right orientation correct; avoid duplicated same-side hands or feet, extra digits, fused fingers, or odd joint placement.",
            "Prefer clean everyday poses with intact limbs and natural proportions.",
        ]
    if style == "legs":
        return [
            "单人限定：画面里只有主角一人。",
            "可见腿部结构自然：左右各一，髋-膝-踝连续，关节朝向正常。",
            "姿势像日常随手拍：重心稳定、可维持，不要拧翻、反折或不可能体位。",
            "小腿或脚可以自然延伸到画面外，也可以被有明确前后关系的实体物体合理遮挡；不得在关节或肢体中段突然终止。",
            "若脚部入镜，脚部朝向自然正位；可见手与肩肘腕连续，自然放在腿侧或膝附近。",
        ]
    return [
        "单人限定：默认只有主角一人出镜（用户明确要求合影除外）。",
        "肢体完整自然：可见时左右手/脚各一，手与肩肘腕连续连接。",
        "保持日常完整肢体与自然比例。",
    ]


@dataclass
class DailySelfieProfile:
    date: str
    outfit: str
    status: str
    mood: str
    seed: str
    updated_at: str
    source: str = "fallback"
    status_by_period: Dict[str, str] = field(default_factory=dict)
    outfit_by_period: Dict[str, str] = field(default_factory=dict)
    occasion: str = ""
    llm_attempted: bool = False


@dataclass
class SelfieIntent:
    raw: str
    compact: str
    is_group_photo: bool
    is_multi_person_group_photo: bool
    change_clothes: bool
    change_pose: bool
    use_today_outfit: bool
    has_reference_style_hint: bool
    is_legs_only: bool = False
    is_third_person_photo: bool = False
    camera_kind: str = ""
    is_cos_look: bool = False


def normalize_intent_text(text: str) -> str:
    return (
        str(text or "")
        .strip()
        .lower()
        .replace(" ", "")
        .replace("\t", "")
        .translate(str.maketrans("", "", "，。！？、；：,.!?"))
    )


def includes_any(text: str, items: list[str]) -> bool:
    return any(item and item in text for item in items)


def extract_user_extra_text(action: str) -> str:
    text = str(action or "")
    match = re.search(r"(?:用户补充要求优先|用户补充要求|额外要求|用户要求)[:：]\s*(.+)", text, re.S)
    if not match:
        return ""
    value = match.group(1)
    value = re.sub(r"\s*【(?:pose|shot|cos|cam|legs|wear|outfit|frame):[a-z0-9_]+】\s*", " ", value)
    return re.sub(r"\s+", " ", value).strip(" 。")


def extract_user_action_text(action: str) -> str:
    """Return only user-authored text, excluding generated mode scaffolding."""
    text = str(action or "").strip()
    extra = extract_user_extra_text(text)
    if extra:
        return extra
    generated_markers = (
        "【自拍 / 看看模式】",
        "【他拍 / 看看你模式】",
        "【自拍 / 看看COS模式】",
        "【他拍 / 看看COS模式】",
        "【第一视角 / 看看COS模式】",
        "【合影 / 同框模式】",
        "【服装局部展示",
        "【腿部自拍模式】",
        "【legs:outfit】",
        "【cos:",
        "【姿势池·",
    )
    if any(marker in text for marker in generated_markers):
        return ""
    return text


def extra_overrides_outfit(extra: str) -> bool:
    compact = normalize_intent_text(extra)
    return includes_any(
        compact,
        [
            "穿这",
            "穿那",
            "身穿",
            "穿上",
            "穿着",
            "换装",
            "换这身",
            "换这套",
            "换衣服",
            "白裙",
            "黑裙",
            "短裙",
            "长裙",
            "上衣",
            "外套",
            "裤子",
            "鞋子",
            "靴子",
            "发饰",
            "配饰",
            "旗袍",
            "制服",
            "outfit",
            "dress",
            "wearing",
            "wearthis",
        ],
    )


def extra_overrides_period(extra: str) -> bool:
    compact = normalize_intent_text(extra)
    return includes_any(
        compact,
        [
            "早上",
            "早晨",
            "清晨",
            "上午",
            "中午",
            "午后",
            "下午",
            "傍晚",
            "黄昏",
            "晚上",
            "夜里",
            "夜晚",
            "深夜",
            "凌晨",
            "白天",
            "白昼",
            "工作日",
            "周末",
        ],
    )


def extra_overrides_context(extra: str) -> bool:
    """Detect an explicit scene or lighting context that replaces daily status."""
    compact = normalize_intent_text(extra)
    return includes_any(
        compact,
        [
            "霓虹",
            "夜色",
            "夜灯",
            "月光",
            "晨光",
            "金色小时",
            "夕阳",
            "日出",
            "日落",
            "暖光",
            "暖灯",
            "冷光",
            "白光",
            "窗光",
            "自然光",
            "柔光",
            "咖啡馆",
            "咖啡店",
            "街上",
            "街边",
            "室外",
            "户外",
            "书店",
            "教室",
            "办公室",
            "客厅",
            "卧室",
            "厨房",
            "阳台",
            "窗边",
            "海边",
            "公园",
            "车站",
            "地铁",
            "影棚",
            "摄影棚",
            "室内",
        ],
    ) or extra_overrides_period(extra)


def extra_overrides_mood(extra: str) -> bool:
    compact = normalize_intent_text(extra)
    return includes_any(
        compact,
        [
            "心情",
            "情绪",
            "开心",
            "快乐",
            "高兴",
            "难过",
            "悲伤",
            "忧郁",
            "生气",
            "愤怒",
            "冷淡",
            "冷酷",
            "温柔",
            "治愈",
            "放松",
            "安静",
            "紧张",
            "疲惫",
            "困倦",
            "慵懒",
            "兴奋",
            "元气",
            "严肃",
            "甜美",
            "俏皮",
            "忧伤",
            "妩媚",
            "性感",
            "可爱",
            "甜蜜",
            "自信",
            "调皮",
            "清冷",
            "优雅",
            "魅惑",
        ],
    )




def local_date_key() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def current_period() -> str:
    hour = time.localtime().tm_hour
    if 5 <= hour < 10:
        return "morning"
    if 10 <= hour < 13:
        return "noon"
    if 13 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    if hour >= 21 or hour < 1:
        return "night"
    return "late_night"


def period_label(period: str) -> str:
    return {
        "morning": "早晨",
        "noon": "中午",
        "afternoon": "下午",
        "evening": "傍晚",
        "night": "夜晚",
        "late_night": "深夜",
    }.get(period, "当前")


def daily_calendar_context(date: str) -> str:
    try:
        parsed = datetime.date.fromisoformat(date)
    except (TypeError, ValueError):
        return f"公历日期：{date}。"
    weekday = ("一", "二", "三", "四", "五", "六", "日")[parsed.weekday()]
    season = {
        12: "冬季", 1: "冬季", 2: "冬季",
        3: "春季", 4: "春季", 5: "春季",
        6: "夏季", 7: "夏季", 8: "夏季",
        9: "秋季", 10: "秋季", 11: "秋季",
    }[parsed.month]
    day_type = "周末" if parsed.weekday() >= 5 else "工作日"
    return f"公历日期：{date}，星期{weekday}，{day_type}，中国大陆北半球{season}。"


def random_pick(items: list[str]) -> str:
    return random.choice(items) if items else ""


def make_random_seed() -> str:
    moods = ["温柔放松", "清爽自然", "安静治愈", "元气明亮", "慵懒惬意", "小小得意"]
    places = ["卧室暖光灯下", "窗边小圆桌旁", "书桌前", "柔软沙发上", "阳台小花架旁", "浴室镜前"]
    activities = ["刚整理完头发", "刚泡好一杯热饮", "正在听轻音乐", "刚从外面散步回来", "准备窝着看书"]
    colors = ["奶油白", "淡粉色", "浅蓝灰", "薄荷绿", "月光白", "柔雾玫瑰色"]
    return "; ".join(
        [
            f"mood={random_pick(moods)}",
            f"place={random_pick(places)}",
            f"activity={random_pick(activities)}",
            f"color={random_pick(colors)}",
            f"rand={random.random():.8f}",
        ]
    )


def fallback_daily_profile(
    date: str, seed: str, *, llm_attempted: bool = False
) -> DailySelfieProfile:
    try:
        month = datetime.date.fromisoformat(date).month
    except (TypeError, ValueError):
        month = 4
    outerwear = {
        12: "莓红短款呢外套", 1: "莓红短款呢外套", 2: "莓红短款呢外套",
        3: "浅卡其薄风衣", 4: "浅卡其薄风衣", 5: "浅卡其薄风衣",
        6: "轻薄白色亚麻衬衫外搭", 7: "轻薄白色亚麻衬衫外搭", 8: "轻薄白色亚麻衬衫外搭",
        9: "深橄榄短夹克", 10: "深橄榄短夹克", 11: "深橄榄短夹克",
    }[month]
    day_looks = [
        ("莓果红方领针织上衣", "深蓝高腰直筒牛仔裤", "焦糖棕乐福鞋", "细金属耳饰"),
        ("奶油黄翻领针织背心叠穿白衬衫", "墨绿色百褶长裙", "黑色短靴", "窄版深棕皮带"),
        ("雾蓝细条纹衬衫", "炭灰色高腰阔腿裤", "银灰复古运动鞋", "小型银色耳圈"),
        ("炭灰短款拉链针织上衣", "酒红色直筒半身裙", "黑色玛丽珍鞋", "一枚亮色发夹"),
    ]
    top, bottoms, shoes, accent = random.choice(day_looks)
    day_outfit = f"{outerwear}敞开穿着，内搭{top}，配{bottoms}和{shoes}，以{accent}作一个清晰点缀。"
    home_looks = [
        "雾蓝色柔软针织上衣配奶油白直筒长裤，剪裁宽松但有清楚层次。",
        "深莓色薄款针织套头衫配炭灰色舒适长裤，配色沉稳有重点。",
        "浅绿色棉质短袖上衣配深蓝色宽松长裤，简洁清爽且版型利落。",
        "奶油黄罗纹针织上衣配烟灰色直筒长裤，柔软材质与清晰轮廓兼顾。",
    ]
    home_outfit = random.choice(home_looks)
    outfit_by_period = {
        "morning": day_outfit,
        "noon": day_outfit,
        "afternoon": day_outfit,
        "evening": day_outfit,
        "night": home_outfit,
        "late_night": home_outfit,
    }
    status_by_period = {
        "morning": "刚开始一天，神情清爽安静，还带着一点没完全醒透的松弛感。",
        "noon": "上午忙碌告一段落，精神明亮些，状态轻快但不刻意摆拍。",
        "afternoon": "下午节奏稍缓，神情自然，带一点慵懒的生活感。",
        "evening": "一天接近尾声，状态放松下来，心情柔和而安定。",
        "night": "夜间进入舒适的居家节奏，动作放松，神情安静。",
        "late_night": "深夜更安静松弛，像准备休息前顺手记录下此刻。",
    }
    period = current_period()
    return DailySelfieProfile(
        date=date,
        outfit=outfit_by_period.get(period, day_outfit),
        status=status_by_period.get(period, "处于自然放松的日常状态，画面安静、统一、真实。"),
        status_by_period=status_by_period,
        outfit_by_period=outfit_by_period,
        mood=random_pick(["放松、安静、柔和", "清爽、自然、轻松", "温柔、治愈、稳定", "元气、明亮、轻快"]),
        seed=seed,
        updated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        source="fallback",
        llm_attempted=llm_attempted,
    )


class PersonaManager:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.file_path = os.path.join(data_dir, "image_persona.json")
        self.image_dir = os.path.join(data_dir, "image-persona")
        os.makedirs(self.image_dir, exist_ok=True)
        self.data: Dict[str, Any] = {
            "ref_image_path": "",
            "ref_mime_type": "image/png",
            "auxiliary_identity_refs": [],
            "appearance_type": "auto",
            "updated_at": "",
            "daily_selfie_profile": None,
        }
        self._daily_profile_lock = asyncio.Lock()
        self.load()

    def load(self) -> None:
        raw = load_json_file(self.file_path)
        if not raw:
            return
        self.data.update(
            {
                "ref_image_path": str(raw.get("ref_image_path") or ""),
                "ref_mime_type": str(raw.get("ref_mime_type") or "image/png"),
                "auxiliary_identity_refs": [
                    {
                        "id": str(item.get("id") or "").strip(),
                        "path": str(item.get("path") or "").strip(),
                        "mime_type": str(item.get("mime_type") or "image/png").strip() or "image/png",
                        "created_at": str(item.get("created_at") or "").strip(),
                    }
                    for item in (raw.get("auxiliary_identity_refs") or [])
                    if isinstance(item, dict) and str(item.get("id") or "").strip() and str(item.get("path") or "").strip()
                ][:3],
                "appearance_type": normalize_appearance_type(raw.get("appearance_type")),
                "updated_at": str(raw.get("updated_at") or ""),
                "daily_selfie_profile": raw.get("daily_selfie_profile"),
            }
        )

    def save(self) -> None:
        save_json_file(self.file_path, self.data)

    def get(self) -> Dict[str, Any]:
        return dict(self.data)

    def get_reference_path(self) -> str:
        path = str(self.data.get("ref_image_path") or "")
        return path if path and os.path.exists(path) else ""

    def has_reference_image(self) -> bool:
        return bool(self.get_reference_path())

    def get_auxiliary_reference_entries(self) -> list[Dict[str, Any]]:
        entries: list[Dict[str, Any]] = []
        for item in self.data.get("auxiliary_identity_refs") or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            ref_id = str(item.get("id") or "").strip()
            if not ref_id or not path or not os.path.isfile(path):
                continue
            entries.append(
                {
                    "id": ref_id,
                    "path": path,
                    "mime_type": str(item.get("mime_type") or "image/png"),
                    "created_at": str(item.get("created_at") or ""),
                }
            )
        return entries[:3]

    def get_auxiliary_reference_images(self) -> list[Dict[str, Any]]:
        images: list[Dict[str, Any]] = []
        for entry in self.get_auxiliary_reference_entries():
            try:
                with open(entry["path"], "rb") as file:
                    data = file.read()
            except OSError:
                continue
            if data:
                images.append({"id": entry["id"], "data": data, "mime_type": entry["mime_type"], "created_at": entry["created_at"]})
        return images

    def add_auxiliary_reference_image(self, data: bytes, mime_type: str = "") -> Dict[str, Any]:
        if not data:
            raise ValueError("辅助形象图为空")
        entries = self.get_auxiliary_reference_entries()
        if len(entries) >= 3:
            raise ValueError("辅助形象图最多 3 张，请先删除一张")
        mime = mime_type or detect_mime_by_bytes(data)
        ext = ext_from_mime(mime)
        ref_id = f"aux_{time.time_ns()}"
        path = os.path.join(self.image_dir, f"persona_aux_{time.time_ns()}.{ext}")
        with open(path, "wb") as file:
            file.write(data)
        entries.append(
            {
                "id": ref_id,
                "path": path,
                "mime_type": mime,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            }
        )
        self.data["auxiliary_identity_refs"] = entries
        self.data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        self.save()
        return self.get()

    def remove_auxiliary_reference_image(self, ref_id: str) -> Dict[str, Any]:
        target_id = str(ref_id or "").strip()
        if not target_id:
            raise ValueError("缺少辅助形象图 id")
        entries = list(self.data.get("auxiliary_identity_refs") or [])
        kept: list[Dict[str, Any]] = []
        removed = None
        for item in entries:
            if isinstance(item, dict) and str(item.get("id") or "") == target_id:
                removed = item
            elif isinstance(item, dict):
                kept.append(item)
        if removed is None:
            raise ValueError("辅助形象图不存在")
        path = str(removed.get("path") or "")
        if path:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        self.data["auxiliary_identity_refs"] = kept
        self.data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        self.save()
        return self.get()

    def clear_auxiliary_reference_images(self) -> Dict[str, Any]:
        """Remove all auxiliary identity images while keeping the primary image."""
        for item in self.data.get("auxiliary_identity_refs") or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        self.data["auxiliary_identity_refs"] = []
        self.data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        self.save()
        return self.get()

    def save_reference_image(self, data: bytes, mime_type: str = "") -> Dict[str, Any]:
        if not data:
            raise ValueError("参考图为空")
        mime = mime_type or detect_mime_by_bytes(data)
        ext = ext_from_mime(mime)
        path = os.path.join(self.image_dir, f"persona_ref_{time.time_ns()}.{ext}")
        with open(path, "wb") as file:
            file.write(data)

        old_path = str(self.data.get("ref_image_path") or "")
        if old_path and old_path != path:
            try:
                if os.path.exists(old_path):
                    os.remove(old_path)
            except OSError:
                pass

        self.data["ref_image_path"] = path
        self.data["ref_mime_type"] = mime
        self.data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        self.save()
        return self.get()

    def clear_reference_image(self) -> Dict[str, Any]:
        path = str(self.data.get("ref_image_path") or "")
        if path:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        self.data["ref_image_path"] = ""
        self.data["ref_mime_type"] = "image/png"
        self.data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        self.save()
        return self.get()

    def get_reference_image(self) -> Optional[Dict[str, Any]]:
        path = self.get_reference_path()
        if not path:
            return None
        try:
            with open(path, "rb") as file:
                data = file.read()
            return {"data": data, "mime_type": str(self.data.get("ref_mime_type") or detect_mime_by_bytes(data))}
        except OSError:
            return None

    def get_appearance_type(self) -> str:
        return normalize_appearance_type(self.data.get("appearance_type"))

    def set_appearance_type(self, value: Any) -> str:
        kind = normalize_appearance_type(value)
        self.data["appearance_type"] = kind
        self.data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        self.save()
        return kind

    def appearance_type_label(self) -> str:
        return APPEARANCE_TYPE_LABELS.get(self.get_appearance_type(), "自动")

    def analyze_selfie_intent(self, action: str) -> SelfieIntent:
        raw = str(action or "").strip()
        extra = extract_user_action_text(raw)
        compact = normalize_intent_text(raw)
        extra_compact = normalize_intent_text(extra)
        requests_daily_outfit = includes_any(
            extra_compact, ["今日穿搭", "今天穿搭", "今天这身", "按今天的穿搭"]
        )
        is_group_photo = includes_any(
            compact,
            [
                "合照",
                "合影",
                "同框",
                "一起拍",
                "一起照",
                "跟我拍",
                "和我拍",
                "双人",
                "多人",
                "大合照",
                "集体照",
                "全员",
                "一起出镜",
                "groupselfie",
                "groupphoto",
                "phototogether",
                "picturetogether",
                "takeaphototogether",
                "takeapicturetogether",
                "togetherwithme",
                "withme",
                "withyou",
                "nexttome",
                "nexttoyou",
                "standingnextto",
                "sidebyside",
                "sameframe",
                "inthesameframe",
                "twoofus",
                "ustogether",
            ],
        )
        is_multi = includes_any(compact, ["多人", "大合照", "集体照", "全员", "三人", "四人", "五人", "多人合照", "大家一起"]) or bool(
            re.search(r"[3-9三四五六七八九十]人", compact)
        )
        change_clothes = (not requests_daily_outfit) and includes_any(
            extra_compact,
            [
                "穿这个",
                "穿这身",
                "穿这套",
                "穿这件",
                "身穿",
                "穿着",
                "穿上",
                "换装",
                "换这身",
                "换这套",
                "换衣服",
                "衣服",
                "服装",
                "穿搭",
                "造型",
                "旗袍",
                "裙子",
                "短裙",
                "长裙",
                "上衣",
                "外套",
                "裤子",
                "鞋子",
                "靴子",
                "发饰",
                "配饰",
                "礼服",
                "制服",
                "女仆装",
                "水手服",
                "丝袜",
                "黑丝",
                "白丝",
                "连裤袜",
                "光腿",
                "jk",
                "cos",
                "cosplay",
                "扮成",
                "outfit",
                "changeoutfit",
                "clothes",
                "clothing",
                "dress",
                "wear",
                "wearing",
                "puton",
                "costume",
                "uniform",
                "maid",
                "schooluniform",
            ],
        )
        change_pose = includes_any(
            extra_compact,
            [
                "姿势",
                "动作",
                "表情",
                "站着",
                "坐着",
                "回头",
                "叉腰",
                "比心",
                "托脸",
                "wink",
                "眨眼",
                "微笑",
                "歪头",
                "看镜头",
                "回眸",
                "脚",
                "手",
                "全身",
                "半身",
                "侧身",
                "站起来",
                "转身",
                "pose",
                "posture",
                "action",
                "standing",
                "sitting",
                "smile",
                "lookingatcamera",
                "peace",
                "hearthands",
                "heartgesture",
                "tilthead",
                "turnaround",
                "holding",
                "leaning",
            ],
        )
        is_cos_look = (
            includes_any(compact, ["看看cos", "看看cos模式", "cos换装", "角色扮演"])
            or "【cos:" in raw.lower()
            or "【cos：" in raw
        )
        is_legs_only = (not is_cos_look) and (
            "【legs:outfit】" in raw
            or includes_any(
                compact,
                [
                    "看看腿",
                    "看腿",
                    "拍腿",
                    "自拍腿",
                    "下半身穿搭",
                    "穿搭展示",
                    "日常下装",
                    "丝袜",
                    "黑丝",
                    "白丝",
                    "连裤袜",
                    "肉丝",
                    "光腿",
                    "美腿",
                    "大腿",
                ],
            )
        )
        is_third_person_photo = (not is_cos_look) and includes_any(
            compact,
            [
                "他拍",
                "别人拍",
                "别人帮拍",
                "朋友拍",
                "有人拍",
                "被拍",
                "抓拍",
                "第三人称",
                "路人视角",
                "摄影师拍",
                "不是自拍",
                "非自拍",
                "不要自拍",
                "不拿手机",
                "不要拿手机",
                "不要手持手机",
                "不要自拍杆",
                "不要对镜",
                "看看你",
                "看下你",
                "看看现在的你",
                "thirdperson",
                "notselfie",
                "candidphoto",
                "takenbyanotherperson",
                "shotbyanotherperson",
            ],
        )
        camera_kind = ""
        if not is_cos_look:
            if "【cam:third】" in raw:
                is_third_person_photo = True
            elif "【cam:selfie】" in raw and is_legs_only:
                is_third_person_photo = False
        # COS is its own outfit mode: never fall into legs / group / daily-outfit.
        # Camera may still be selfie or third-person; extra text can pick either.
        if is_cos_look:
            is_legs_only = False
            is_group_photo = False
            is_multi = False
            change_clothes = True
            use_today = False
            camera_match = re.search(r"【cam:(selfie|first|third)】", raw, re.I)
            camera_kind = str(camera_match.group(1)).lower() if camera_match else ""
            if not camera_kind:
                camera_kind = parse_requested_cos_camera(raw)
            if not camera_kind:
                if "【他拍 / 看看cos模式】" in compact or "【他拍 / 看看COS模式】" in raw:
                    camera_kind = "third"
                elif "【自拍 / 看看cos模式】" in compact or "【自拍 / 看看COS模式】" in raw:
                    camera_kind = "selfie"
                else:
                    # Raw COS prompts historically used the selfie fallback;
                    # generated COS-pool actions carry an explicit camera tag.
                    camera_kind = "selfie"
            is_third_person_photo = camera_kind == "third"
        elif is_legs_only:
            is_group_photo = False
            is_multi = False
            # Legwear terms are lower-body styling, not outfit-change; the change-clothes line confuses gpt-image.
            change_clothes = False
            use_today = False
        else:
            use_today = (
                not is_cos_look
                and not is_legs_only
                and not change_clothes
            )
        if is_cos_look:
            use_today = False
        has_ref_hint = includes_any(
            compact,
            [
                "长这个",
                "长这样",
                "像这个",
                "像这样",
                "照这个",
                "按这个",
                "参考这个",
                "参考图",
                "引用图",
                "attachedimage",
                "providedimage",
                "referenceimage",
                "basedonthis",
                "sameasthis",
            ],
        )
        return SelfieIntent(
            raw=raw,
            compact=compact,
            is_group_photo=is_group_photo,
            is_multi_person_group_photo=is_multi,
            change_clothes=change_clothes,
            change_pose=change_pose,
            use_today_outfit=use_today,
            has_reference_style_hint=has_ref_hint,
            is_legs_only=is_legs_only,
            is_third_person_photo=is_third_person_photo,
            camera_kind=camera_kind,
            is_cos_look=is_cos_look,
        )

    async def ensure_daily_selfie_profile(
        self,
        action: str = "",
        *,
        llm_generate: Optional[Callable[[str], Awaitable[str]]] = None,
    ) -> DailySelfieProfile:
        # Batch shots may initialize the profile concurrently; serialize the
        # read/generate/save sequence so every shot shares one daily snapshot.
        async with self._daily_profile_lock:
            return await self._ensure_daily_selfie_profile_unlocked(
                action, llm_generate=llm_generate
            )

    async def _ensure_daily_selfie_profile_unlocked(
        self,
        action: str = "",
        *,
        llm_generate: Optional[Callable[[str], Awaitable[str]]] = None,
    ) -> DailySelfieProfile:
        today = local_date_key()
        existed = self.data.get("daily_selfie_profile")
        if isinstance(existed, dict) and existed.get("date") == today and existed.get("outfit") and existed.get("status"):
            status_by_period = existed.get("status_by_period") if isinstance(existed.get("status_by_period"), dict) else {}
            outfit_by_period = existed.get("outfit_by_period") if isinstance(existed.get("outfit_by_period"), dict) else {}
            clean_outfits = {str(k): str(v).strip() for k, v in outfit_by_period.items()}
            source = str(existed.get("source") or "fallback")
            llm_attempted = bool(existed.get("llm_attempted", source == "llm"))
            has_period_outfits = all(clean_outfits.get(key) for key in DAILY_PERIOD_KEYS)
            waiting_for_llm = source == "fallback" and llm_generate is not None and not llm_attempted
            if has_period_outfits and not waiting_for_llm:
                return DailySelfieProfile(
                    date=str(existed.get("date") or today),
                    outfit=clean_outfits.get(current_period()) or str(existed.get("outfit") or ""),
                    status=str(status_by_period.get(current_period()) or existed.get("status") or ""),
                    mood=str(existed.get("mood") or ""),
                    seed=str(existed.get("seed") or ""),
                    updated_at=str(existed.get("updated_at") or ""),
                    source=source,
                    status_by_period={str(k): str(v) for k, v in status_by_period.items()},
                    outfit_by_period=clean_outfits,
                    occasion=str(existed.get("occasion") or ""),
                    llm_attempted=llm_attempted,
                )
            if llm_generate is None:
                return DailySelfieProfile(
                    date=str(existed.get("date") or today),
                    outfit=clean_outfits.get(current_period()) or str(existed.get("outfit") or ""),
                    status=str(status_by_period.get(current_period()) or existed.get("status") or ""),
                    mood=str(existed.get("mood") or ""),
                    seed=str(existed.get("seed") or ""),
                    updated_at=str(existed.get("updated_at") or ""),
                    source=source,
                    status_by_period={str(k): str(v) for k, v in status_by_period.items()},
                    outfit_by_period=clean_outfits,
                    occasion=str(existed.get("occasion") or ""),
                    llm_attempted=llm_attempted,
                )

        profile: Optional[DailySelfieProfile] = None
        if llm_generate is not None:
            prompt = (
                "为 AI 自拍角色规划今天一整天的日常穿搭与状态。只返回一个 JSON 对象，不要 Markdown："
                '{"outfit_by_period":{"morning":"...","noon":"...","afternoon":"...",'
                '"evening":"...","night":"...","late_night":"..."},"occasion":"...",'
                '"mood":"...","status_by_period":{"morning":"...","noon":"...",'
                '"afternoon":"...","evening":"...","night":"...","late_night":"..."}}。'
                f"{daily_calendar_context(today)}"
                "角色始终是成年人物。穿搭适合日常自拍，但要有明确的风格方向和视觉重点：写清廓形、层次、材质、主色与一个点缀色，搭配完整具体，避免只写普通的上衣加裙子/裤子，也不要堆无关配饰或奢侈品牌。"
                "六个时段分别给出完整可直接画出的服装搭配；早晨到下午保持同一套主搭配或做合理的小调整，傍晚可以因活动/回家自然增减外套或更换搭配，夜晚和深夜可换成协调、得体、舒适的居家服。变化要符合一天的连续性，不要每个时段像完全不同的人，也不要臆造出行或活动。"
                "结合给出的公历日期、星期和季节选择适合的面料与层次。若日期确实对应你能确认的节日或节气，可在 occasion 中说明，并只用配色或一两处小细节呼应；不确定时留空，不编造节日、天气或活动，不把日常穿搭变成主题服/戏服。"
                "outfit_by_period 的每个值只写当时实际穿着的衣服、鞋履和少量配饰，不写时间、地点、光线、心情或拍照动作。occasion 可为空；mood 描述全天基调；status_by_period 描述各时段的状态和时间感，不锁定具体地点、镜头或构图，留给自拍场景选择。"
                "内容得体，不写暴露、色情或未成年人内容；每个穿搭字段不超过 220 字，occasion 不超过 100 字，mood 不超过 80 字，各状态不超过 160 字。"
            )
            try:
                raw = str(await llm_generate(prompt) or "").strip()
                match = re.search(r"\{[\s\S]*\}", raw)
                data = json.loads(match.group(0)) if match else {}
                periods = data.get("status_by_period") if isinstance(data, dict) and isinstance(data.get("status_by_period"), dict) else {}
                outfits = data.get("outfit_by_period") if isinstance(data, dict) and isinstance(data.get("outfit_by_period"), dict) else {}
                clean_outfits = {
                    key: str(outfits.get(key) or "").strip()
                    for key in DAILY_PERIOD_KEYS
                }
                mood = str(data.get("mood") or "自然、放松、轻松").strip()
                clean_periods = {
                    key: str(periods.get(key) or "").strip()
                    for key in DAILY_PERIOD_KEYS
                }
                occasion = str(data.get("occasion") or "").strip()
                forbidden = ("未成年", "裸", "裸体", "色情", "暴露", "内衣", "乳", "私密")
                all_text = " ".join([*clean_outfits.values(), occasion, mood, *clean_periods.values()])
                time_locked = ("清晨", "早晨", "午后", "傍晚", "夜里", "夜晚", "深夜", "凌晨")
                if (
                    all(clean_outfits.values())
                    and all(len(value) <= 220 for value in clean_outfits.values())
                    and len(occasion) <= 100
                    and len(mood) <= 80
                    and all(clean_periods.values())
                    and all(len(value) <= 160 for value in clean_periods.values())
                    and not any(word in all_text for word in forbidden)
                    and not any(word in value for value in clean_outfits.values() for word in time_locked)
                ):
                    profile = DailySelfieProfile(
                        date=today,
                        outfit=clean_outfits.get(current_period(), clean_outfits["morning"]),
                        status=clean_periods.get(current_period(), clean_periods["morning"]),
                        status_by_period=clean_periods,
                        outfit_by_period=clean_outfits,
                        occasion=occasion,
                        mood=mood,
                        seed=make_random_seed(),
                        updated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
                        source="llm",
                        llm_attempted=True,
                    )
            except Exception:
                profile = None
        if profile is None:
            profile = fallback_daily_profile(
                today, make_random_seed(), llm_attempted=llm_generate is not None
            )
        self.data["daily_selfie_profile"] = {
            "date": profile.date,
            "outfit": profile.outfit,
            "outfit_by_period": profile.outfit_by_period,
            "occasion": profile.occasion,
            "status": profile.status,
            "status_by_period": profile.status_by_period,
            "mood": profile.mood,
            "seed": profile.seed,
            "updated_at": profile.updated_at,
            "source": profile.source,
            "llm_attempted": profile.llm_attempted,
        }
        self.data["updated_at"] = profile.updated_at
        self.save()
        return profile

    def refresh_daily_selfie_profile_for_test(self) -> None:
        self.data["daily_selfie_profile"] = None
        self.data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        self.save()

    def get_daily_selfie_profile(self) -> Optional[DailySelfieProfile]:
        raw = self.data.get("daily_selfie_profile")
        if not isinstance(raw, dict):
            return None
        status_by_period = raw.get("status_by_period") if isinstance(raw.get("status_by_period"), dict) else {}
        outfit_by_period = raw.get("outfit_by_period") if isinstance(raw.get("outfit_by_period"), dict) else {}
        clean_outfits = {str(k): str(v) for k, v in outfit_by_period.items()}
        return DailySelfieProfile(
            date=str(raw.get("date") or ""),
            outfit=str(clean_outfits.get(current_period()) or raw.get("outfit") or ""),
            status=str(status_by_period.get(current_period()) or raw.get("status") or ""),
            mood=str(raw.get("mood") or ""),
            seed=str(raw.get("seed") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            source=str(raw.get("source") or "fallback"),
            status_by_period={str(k): str(v) for k, v in status_by_period.items()},
            outfit_by_period=clean_outfits,
            occasion=str(raw.get("occasion") or ""),
            llm_attempted=bool(raw.get("llm_attempted", str(raw.get("source") or "") == "llm")),
        )

    def build_selfie_prompt(
        self,
        action: str,
        bot_name: str,
        personality: str,
        has_reference_image: bool,
        extra_reference_count: int = 0,
    ) -> str:
        act = str(action or "").strip()
        intent = self.analyze_selfie_intent(act)
        if intent.is_legs_only and not has_leg_focus_contract(act):
            act = ensure_leg_focus_action(act, has_reference_image)
            intent = self.analyze_selfie_intent(act)
        user_text = extract_user_action_text(act)
        appearance_type = resolve_appearance_type(
            self.get_appearance_type(),
            user_text,
        )
        if intent.is_cos_look:
            act = adapt_cos_prompt_for_appearance(act, appearance_type)
        cos_contract = bool(
            intent.is_cos_look and re.search(r"【cam:(?:selfie|first|third)】", act, re.I)
        )
        shot_match = re.search(r"【shot:([a-z_]+)】", act, re.I)
        shot_id = str(shot_match.group(1) if shot_match else "")
        cos_pose_match = re.search(r"【cos_pose:([a-z_]+)】", act, re.I)
        cos_pose_id = str(cos_pose_match.group(1) if cos_pose_match else "")
        back_facing_shot = shot_id in {"back_view", "rear_rooftop"} or cos_pose_id in {
            "back_to_camera",
            "over_shoulder",
        }
        side_facing_shot = shot_id == "side_profile" or cos_pose_id == "side_profile"
        feet_cropped = is_leg_calf_crop_action(act)
        daily = self.get_daily_selfie_profile()

        identity_lines = (
            [
                "固定形象参考：参考图一是主角身份锚点。",
                "主角的脸型、五官结构、发型发色、体态、性别与整体长相来自参考图一；额外参考图不得替换这些身份特征。",
                "参考图一是女性则主角必须是女性，是男性则必须是男性；禁止把女形象改成男、把男形象改成女。",
                "表情、眼神、嘴角与微表情默认按本次场景自然重画，不要原样复制参考图一的固定表情或僵硬眼神；只锁身份长相与性别。",
                "面向镜头时自然看向镜头、眼神有焦点；用户明确要求侧脸、低头、看彼此或看别处时按要求，不要心不在焉。",
            ]
            if has_reference_image
            else [
                "形象参考以角色名称、人设和今日状态为准；未说明性别默认成年女性，不无故改成男性。",
                "正对镜头时自然看向镜头，表情有焦点。",
            ]
        )
        if intent.is_cos_look and cos_contract:
            # A generated COS action already carries its camera, outfit, pose,
            # head, and expression contract. The shared identity contract is
            # emitted here exactly once for every referenced COS identity.
            identity_lines = [
                COS_IDENTITY_CONTRACT_ZH
                if has_reference_image
                else "形象参考以角色名称、人设和本次 COS 套装为准；未说明性别默认成年女性。"
            ]
        elif intent.is_cos_look:
            identity_lines = (
                [
                    "固定形象参考：参考图一是主角身份锚点。",
                    "主角的脸型、五官结构、性别、肤色和身体比例来自参考图一；优先保留脸型轮廓、五官比例、眼睛和嘴唇轮廓，不因妆容或表情重塑成另一张脸；发型发色和造型按本套 COS 替换。",
                    "参考图一是女性则主角必须是女性，是男性则主角必须是男性。",
                ]
                if has_reference_image
                else [
                    "形象参考以角色名称、人设和本次 COS 套装为准；未说明性别默认成年女性。",
                ]
            )
        if intent.is_legs_only and feet_cropped:
            identity_lines = (
                [
                    "参考图一只用于自然体态和构图；服装按本次锁定搭配执行，人物身份保持一致。",
                ]
                if has_reference_image
                else [
                    "本张按用户明确要求进行服装局部取景，服装保持完整、人物比例自然。",
                ]
            )
        if has_reference_image and not intent.is_legs_only and has_viewpoint_change_request(act):
            identity_lines.append(
                "本次指定或随机选定的镜头视角优先于参考图投影；保留同一人的身份线索，但不要复制参考图里的头部朝向、脸部透视、肩线和原姿势。按目标视角自然重建面部透视；侧脸或三分之四侧面时，远侧五官允许自然遮挡，不要为保留参考图而强行露出双眼或扭脸。"
            )
        appearance_line = appearance_type_instruction(
            appearance_type,
            has_reference_image=has_reference_image,
            is_cos=intent.is_cos_look,
        )
        if appearance_line and not (intent.is_legs_only and feet_cropped):
            identity_lines.insert(1 if has_reference_image else 0, appearance_line)

        reference_lines: list[str] = []
        if extra_reference_count > 0:
            reference_lines.append(f"另有 {extra_reference_count} 张额外参考图。")
            if intent.is_group_photo:
                reference_lines.extend(group_style_lines(appearance_type))
            elif intent.is_legs_only:
                reference_lines.append(
                    "额外参考图只参考氛围和构图，不改变已锁定的单一视角、姿势或完整服装搭配。"
                )
            else:
                reference_lines.extend(
                    [
                        "额外参考图仅用于服装、姿势、构图、风格、场景、道具、镜头角度或光线；不替换参考图一的主角身份。",
                        "换装 / COS 时只迁移服装、配饰、颜色、材质、印花和造型氛围，不迁移额外参考图人物的脸型、五官、发型发色或体态。",
                        (
                            "性别跟随参考图一；若当前构图允许面部入镜，面部清晰自然，表情眼神按本次场景重画。"
                            if back_facing_shot
                            else "性别跟随参考图一；除非用户要求遮脸，否则主角面部清晰自然，表情眼神按本次场景重画。"
                        ),
                    ]
                    if has_reference_image
                    else [
                        "额外参考图用于构图、衣服、姿势、场景或光线；主角仍符合角色名称和人设。",
                        (
                            "若当前构图允许面部入镜，主角面部清晰自然。"
                            if back_facing_shot
                            else "除非用户要求遮脸，否则主角面部清晰自然。"
                        ),
                    ]
                )

        explicit_context = (
            extra_overrides_outfit(user_text)
            or extra_overrides_context(user_text)
            or extra_overrides_mood(user_text)
        )
        snapshot_prompt = (
            random.choice(SELFIE_SNAPSHOT_PRESETS)
            if not explicit_context and not back_facing_shot and not side_facing_shot
            else ""
        )
        mode_lines: list[str] = []
        if intent.is_cos_look:
            camera_kind = intent.camera_kind or ("third" if intent.is_third_person_photo else "selfie")
            camera_is_third = camera_kind == "third"
            camera_labels = {
                "third": "【COS换装他拍模式】",
                "first": "【COS换装第一视角模式】",
                "selfie": "【COS换装自拍模式】",
            }
            mode_lines.append(camera_labels.get(camera_kind, "【COS换装自拍模式】"))
            # Built COS actions already contain the complete camera and outfit
            # contract. Raw COS actions still get one compact camera fallback.
            if cos_contract:
                mode_lines.append(act)
            else:
                if camera_is_third:
                    mode_lines.append(
                        "别人视角的单人成品照：摄影师在画面外，机位、景别、姿势和表情按用户要求或套装描述执行；"
                        "未指定时保持单人、面部清晰、动作自然。"
                    )
                elif camera_kind == "first":
                    mode_lines.append(
                        "第一视角主观单人成品照：镜头位于观察者自然视线的位置，像观察者亲眼看到主角；"
                        "不出现手持设备、镜面反射、拍摄者、记录设备、观察者身体或第二人物；未指定时保持单人、面部清晰、动作自然。"
                    )
                else:
                    mode_lines.append(
                        "单人自拍成片：机位、景别、姿势和表情按用户要求或套装描述执行；"
                        "未指定时保持面部清晰、动作自然。"
                    )
                mode_lines.append(
                    "套装或用户明确要求的歪头、仰头、低头、闭眼或夸张表情应保留；"
                    "未指定时自然参考形象图，头部、视线和表情保持连贯，避免无依据过度变形；面部边缘清晰、肤色自然，不用白色薄膜或雾化遮住五官。"
                )
        elif intent.is_group_photo:
            mode_lines.append("【合影 / 同框模式】")
            if appearance_type == "anime":
                mode_lines.append("先确定你自己的柔光京阿尼动漫形象，再把每张额外参考图落实为独立的同框动漫人物。")
            elif appearance_type == "real":
                mode_lines.append("先确定你自己的写实形象，再把每张额外参考图落实为独立的同框写实人物。")
            else:
                mode_lines.append("先锁定主形象参考图的媒介和身份，再把每张额外参考图落实为独立同框对象；额外参考不得改变主角媒介。")
            # With extra references, the same style/conversion guidance is
            # already emitted in reference_lines above.
            if not extra_reference_count:
                mode_lines.extend(group_style_lines(appearance_type))
            mode_lines.extend(
                [
                    "同框对象必须是独立完整人物（有头有身体），禁止原样保留玩偶/毛绒玩具/吉祥物外形，禁止贴在主角衣服上或与身体衣物粘连融合。",
                    "同框人物自然站位或坐位，距离、遮挡、视线和互动关系合理；与主角边界清晰、可并肩，不要融成一团。",
                    "正面合影默认多数人看向镜头（或看向画面中的相机方向），表情自然、有互动；背面或侧身构图按选定朝向执行，不强迫主角回头；你自己的表情按合影氛围重画，不要僵住参考图一的原表情。",
                    "除非用户明确要求看向彼此或看向别处，不要全员心不在焉、眼神飘走。",
                    "你自己作为主角之一时，若面向镜头，优先与镜头有眼神交流，像认真合影而不是走神。",
                ]
            )
            if extra_reference_count > 1:
                mode_lines.append("多人合影时，每个人都有清晰、独立、稳定的身份。")
        elif intent.is_legs_only:
            camera_match = re.search(r"【cam:(selfie|third)】", act)
            camera_kind = str(camera_match.group(1) if camera_match else "selfie")
            pose_match = re.search(r"【pose:([a-z_]+)】", act)
            pool_pose_match = re.search(
                r"【姿势池·[^】]+】(.*?)(?=本次服装搭配已锁定为：|完整下装服饰组合已由代码锁定：|用户提供的图片只参考|【cam:|$)",
                act,
                re.S,
            )
            pool_pose_text = re.sub(
                r"\s+", " ", str(pool_pose_match.group(1) if pool_pose_match else "")
            ).strip(" 。；")
            wear_tag_match = re.search(r"【wear:(daily|custom|white|black|bare|pantyhose)】", act)
            selected = {
                "daily": "日常裤装",
                "custom": "用户指定",
                "white": "白丝",
                "black": "黑丝",
                "bare": "光腿神器",
                "pantyhose": "连裤袜",
            }.get(wear_tag_match.group(1) if wear_tag_match else "", "")
            if not selected:
                wear_match = re.search(r"本次服装搭配(?:已锁定为)?[:：]\s*([^。]+)", act)
                if wear_match:
                    selected_source = str(wear_match.group(1)).strip().split("；", 1)[0]
                    selected = next((name for name in ("日常裤装", "用户指定", "光腿神器", "白丝", "黑丝", "连裤袜") if name in selected_source), "")
            selected_text = {
                "日常裤装": "完整的日常长裤搭配",
                "用户指定": "用户指定的完整服装搭配",
                "光腿神器": "自然肤色不透打底，完整日常穿搭",
                "白丝": "白色不透连裤袜，完整日常穿搭",
                "黑丝": "黑色不透连裤袜，完整日常穿搭",
                "连裤袜": "白色不透连裤袜，完整日常穿搭",
            }.get(selected)
            outfit_match = re.search(r"【outfit:([a-z_]+)】", act)
            outfit = get_leg_focus_outfit(outfit_match.group(1) if outfit_match else "")
            mode_lines.extend(
                [
                    f"【{'腿部穿搭展示' if feet_cropped else '全身穿搭展示'} / {'全身镜自拍' if camera_kind == 'selfie' and not feet_cropped else '第一人称腰线至鞋履构图' if camera_kind == 'selfie' else '第三人称摄影'}】",
                    (
                        "第一人称自然视角服装照：腰线至鞋履入镜，清楚呈现下装、袜装与鞋履。"
                        if camera_kind == "selfie" and feet_cropped
                        else "全身镜前自拍：从头部到鞋履完整入镜，手机自然持握且不遮挡主要穿搭。"
                        if camera_kind == "selfie"
                        else "第三人称腿部穿搭照片：摄影者完全在画面外，平视拍摄腰线至鞋履，保留少量环境。"
                        if feet_cropped
                        else "第三人称全身日常照片：摄影者完全在画面外，从头部到鞋履完整入镜。"
                    ),
                    "成年人物腿部穿搭构图：腰线至鞋履自然入镜，袜装与鞋履清晰可见，平视并保留少量环境。"
                    if feet_cropped
                    else "成年人物单人全身构图：头部、上衣、下装和鞋履都完整入镜，以正常拍摄距离呈现整体穿搭。",
                    f"本次穿搭固定为{selected_text}；按这一套完整呈现。"
                    if selected_text
                    else "本次下装和鞋履已由代码锁定；按同一套日常穿搭完成画面。",
                    "服装穿着完整，颜色、材质和层次清楚自然。",
                    "人物比例与肢体结构自然连续；袜装和鞋履保持完整可见，不被家具遮挡。"
                    if feet_cropped
                    else "人物比例、站坐姿态和鞋履完整自然，服装边缘清晰，不裁掉头部或脚部。",
                    "严格按已锁定的单一视角、姿势和完整服装搭配生成，不在选项之间切换，不自行补充其他方案。",
                ]
            )
            if outfit:
                mode_lines.append(
                    f"代码锁定下装服饰组合：{outfit.get('bottoms') or outfit.get('skirt', '')}、{outfit.get('shoes', '')}。"
                )
            elif outfit_match:
                mode_lines.append("裙装或鞋履按用户明确指定内容执行，不自行替换。")
            if pose_match:
                pose_descriptions = {
                    "sit": "椅上或沙发自然坐姿，全身与鞋履完整入镜",
                    "sit_crop": "椅上或沙发自然坐姿，全身与鞋履完整入镜",
                    "kneel": "自然稳定的跪坐姿势，人物与服装完整入镜",
                    "kneel_crop": "自然稳定的跪坐姿势，人物与服装完整入镜",
                    "side_lie": "靠坐或侧坐的放松姿势，完整呈现人物与穿搭",
                    "side_lie_crop": "靠坐或侧坐的放松姿势，完整呈现人物与穿搭",
                    "hug_knee": "自然舒适的坐姿，头部到鞋履完整入镜",
                    "hug_knee_crop": "自然舒适的坐姿，头部到鞋履完整入镜",
                    "cross_leg": "椅上或沙发上的自然坐姿，完整呈现人物与穿搭",
                    "cross_leg_crop": "椅上或沙发上的自然坐姿，完整呈现人物与穿搭",
                    "windowsill": "稳固座椅上的自然坐姿，头部到鞋履完整入镜",
                    "windowsill_crop": "稳固座椅上的自然坐姿，头部到鞋履完整入镜",
                    "kneel_up": "稳定自然的跪姿，人物与服装完整入镜",
                    "kneel_front": "正面自然跪坐，人物与服装完整入镜",
                    "floor_fold": "放松的席地坐姿，保持自然全身构图",
                    "one_knee_fix": "一侧单膝触地的自然姿势，人物完整入镜",
                    "floor_knees_up_crop": "地毯或木地板上的自然坐姿，人物完整入镜",
                    "reclined_knees_crop": "沙发或座椅上的轻松靠坐姿势，人物完整入镜",
                    "desk_sit_crop": "桌前椅上的自然坐姿，头部到鞋履完整入镜",
                    "bed_supine_crop": "床边舒适坐姿，人物和整体穿搭完整入镜",
                    "floor_topdown_cross_crop": "按用户指定的俯拍角度完整呈现人物与穿搭",
                    "first_person_topdown_legs_crop": "按用户明确要求，从自然视线向下拍摄服装局部",
                }
                pose_text = pose_descriptions.get(pose_match.group(1))
                if pose_text and not pool_pose_text:
                    mode_lines.append(f"本张构图固定为：{pose_text}；不要改成其他姿势。")
            if pool_pose_text:
                mode_lines.append(f"本张固定姿势与构图：{pool_pose_text}。")
            if intent.change_clothes:
                mode_lines.append("本次同时包含换装要求：优先使用用户指定的服装/穿搭。")
        elif intent.is_third_person_photo:
            if explicit_context:
                mode_lines.extend(
                    [
                        "【他拍 / 用户指定场景模式】",
                        "别人视角的单人成品照：拍摄者完全在画面外，画面里只有主角；机位、景别、场景和光线以用户要求为准。",
                    ]
                )
            else:
                mode_lines.extend(
                    [
                        "【他拍 / 日常照片模式】",
                        "别人视角的单人成品照：镜头已经对准你拍下，画面里只有你一个人；拍摄者完全在画面外，不要第二个人，不要有人举着手机拍你。",
                        "正面构图时可看向镜头，眼神自然有焦点；侧身或背对镜头时头部顺着身体方向，不强迫回头或扭颈。",
                        "机位与场景可自然变化，例如：半身平视、三分之四侧身、中远景环境人像、近景胸像、靠墙门框、轻微低机位、走路抓拍等。",
                        "场景可在窗边沙发、书桌、咖啡馆、街边树荫、阳台、夜灯房间、书店角落等日常地点中自然选择。",
                        "可带一点生活瞬间：端杯抬眼、托腮、整理发丝/袖口、翻书抬头、插兜靠站等，仍保持写实抓拍。",
                        "画面带轻微抓拍感和生活感，同时脸部、穿搭、姿态、背景层次和光线清晰自然。",
                    ]
                )
            if intent.change_clothes:
                mode_lines.append("本次同时包含换装要求：在他拍视角下优先使用用户指定的服装/穿搭。")
            if intent.change_pose:
                mode_lines.append("本次同时包含姿势/动作要求：在他拍视角下自然完成用户指定的动作或表情。")
        elif intent.change_clothes and intent.change_pose:
            mode_lines.extend(
                [
                    "【换衣服 + 改姿势模式】",
                    "保持身份：脸型五官、发型发色、体态稳定。",
                    "先锁定身份长相，再同时迁移服装/配饰与姿势/动作。",
                    "表情、眼神按新姿势与场景自然重画，不要原样保留参考图一的固定表情。",
                    "额外参考图优先用于服装、配饰、颜色、材质、姿势、动作、镜头角度和构图。",
                    "额外参考图里的遮挡面部物件或动作，默认不保留，除非用户明确要求。",
                ]
            )
        elif intent.change_clothes:
            mode_lines.extend(
                [
                    "【改衣服 / 改穿搭模式】",
                    "重点是换装、穿搭或服装变化。",
                    "保持身份：脸型五官、发型发色、体态稳定——是「你」穿上参考服装，不是变成参考图里的另一个人。",
                    "只替换衣服、配饰、材质、配色、印花文字和造型氛围；不要迁移参考图人物的脸或发型。",
                    "表情眼神随新穿搭与场景自然调整（可微笑、害羞、俏皮等），不要整脸复制参考图原表情。",
                    "默认面部清晰可见、自然看向镜头；不要背对镜头、低头藏脸或被头发大面积遮挡。",
                    "额外参考图用于服装、配饰、造型、颜色、材质和穿搭层次参考。",
                    "额外参考图里的遮挡面部物件不属于穿搭内容，除非用户明确要求保留。",
                ]
            )
        elif intent.change_pose:
            mode_lines.extend(
                [
                    "【改姿势 / 改动作模式】",
                    "重点是姿势、动作或表情变化。",
                    "保持身份和穿搭稳定，调整姿势、动作、表情、镜头角度和构图。",
                    "姿势要自然放松，身体重心、手脚位置、视线方向和画面留白协调。",
                    "表情眼神按新姿势自然变化，不要僵住参考图原表情。",
                    "额外参考图用于姿势、动作、表情、镜头角度或构图参考。",
                    "额外参考图里的遮挡面部物件不属于姿势目标，除非用户明确要求保留。",
                ]
            )
        elif explicit_context:
            mode_lines.extend(
                [
                    "【用户指定场景模式】",
                    "按用户指定的服装、场景、光线、动作和情绪生成；未指定的部分保持自然，不额外改写为普通居家自拍。",
                ]
            )
        else:
            mode_lines.extend(
                [
                    "【今日穿搭 / 普通自拍模式】",
                    (
                        "本次为主角自行启动的定时自摄；按已选场景完成背面构图，头部与肩胯朝向一致，不回头、不扭颈，不要求露出正脸。"
                        if back_facing_shot
                        else "本次是普通自拍 / 看看你现在的样子（自己举机或镜前自拍，不是别人代拍）。"
                    ),
                    "按当前时段的今日穿搭、状态和心情生成自然自拍；场景与镜头只改变拍摄氛围，不得替换、增添或删减穿搭。",
                    (
                        "保持竖屏中近景和自然透视；双手放松垂在身侧，只有场景内已有合适支撑物时才自然搭放。"
                        if back_facing_shot
                        else snapshot_prompt
                        or "竖屏手机自拍，采用自然的侧身机位和中近景构图；保持当前穿搭清楚可辨，不强迫回头硬露正脸。"
                    ),
                    (
                        "身体可以自然侧向镜头，肩胯和头部方向连贯，不强迫回头硬露正脸。"
                        if side_facing_shot
                        else "生活场景不要求额外道具或特定活动；可有轻微笑意或自然整理发丝，避免摆拍感和过量画面瑕疵。"
                    ),
                ]
            )

        extra = user_text
        skip_outfit = extra_overrides_outfit(extra) or intent.change_clothes
        skip_period = extra_overrides_period(extra)
        skip_status = extra_overrides_context(extra)
        skip_mood = extra_overrides_mood(extra)
        use_daily_now = (not intent.is_legs_only) and (not intent.is_cos_look)
        today_lines: list[str] = []
        if use_daily_now and daily and not skip_period:
            period = current_period()
            label = period_label(period)
            today_lines.append(f"当前时间段：{label}")
            selected_outfit = str(daily.outfit_by_period.get(period) or daily.outfit or "").strip()
            if intent.use_today_outfit and selected_outfit and not skip_outfit:
                outfit_label = f"今日穿搭（{label}）" if daily.outfit_by_period else "今日穿搭"
                today_lines.append(f"{outfit_label}：{selected_outfit}")
            if daily.occasion:
                today_lines.append(f"今日时令 / 节日氛围：{daily.occasion}")
            if not skip_status:
                status = ""
                if daily.status_by_period:
                    status = str(daily.status_by_period.get(period) or "")
                status = status or str(daily.status or "")
                if status:
                    today_lines.append(f"当前状态：{status}")
        if use_daily_now and daily and daily.mood and not skip_mood:
            today_lines.append(f"当前心情：{daily.mood}")

        if intent.is_legs_only:
            extra_action = extract_user_extra_text(act)
            action_line = f"用户要求：{extra_action}" if extra_action else (
                "用户要求：按腰线至鞋履的日常袜装穿搭构图生成。"
                if feet_cropped
                else "用户要求：按全身穿搭展示模式生成。"
            )
        elif intent.is_cos_look and cos_contract:
            action_line = "用户要求：按以上已锁定的 COS 套装、相机、构图、姿势和场景规则生成。"
        else:
            action_line = f"用户要求：{act}" if act else "用户要求：看着镜头自然自拍，展示你现在的样子。"
        if intent.is_legs_only:
            subject_photo_label = (
                "第三人称腿部穿搭照片" if feet_cropped else "第三人称全身穿搭照片"
            ) if camera_kind == "third" else (
                "第一人称腰线至鞋履照片" if feet_cropped else "全身镜自拍照片"
            )
        elif intent.is_cos_look:
            subject_photo_label = {
                "third": "动漫风格他拍画面" if appearance_type == "anime" else "日常他拍照片",
                "first": "第一视角动漫画面" if appearance_type == "anime" else "第一视角照片",
                "selfie": "自拍动漫画面" if appearance_type == "anime" else "自拍照片",
            }.get(
                intent.camera_kind or ("third" if intent.is_third_person_photo else "selfie"),
                "自拍动漫画面" if appearance_type == "anime" else "自拍照片",
            )
        else:
            if appearance_type == "anime":
                subject_photo_label = "日常他拍动漫画面" if intent.is_third_person_photo and not intent.is_group_photo else "动漫自拍画面"
            else:
                subject_photo_label = "日常他拍照片" if intent.is_third_person_photo and not intent.is_group_photo else "自拍照片"
        if intent.is_cos_look:
            output_lines = ["【生成要求】"]
            output_lines.append(
                "按以上完整 COS 规则生成一张自然成片；未明确的部分保持连贯，不额外改写。"
                if cos_contract
                else "按以上 COS 套装、构图、姿势和表情生成一张完整自然的成片。"
            )
            if not cos_contract:
                output_lines.append(
                    "人体结构自然完整：左右手/脚各一只，手与胳膊连续连接；第三人称摄影画面只出现主角自身的两条手臂和两只手。"
                )
        elif intent.is_group_photo:
            output_lines = [
                "【生成要求】保持所有人物独立、边界清晰，统一场景与画风。",
                "人体结构自然完整：每人左右手/脚各一只，手与胳膊连续连接。",
                "正面合影时同框角色得体成年、互动自然并默认看向镜头；选定侧身或背面构图时保持身体与头部朝向连贯，不强迫回头。",
            ]
        elif intent.is_legs_only:
            # The mode lines above already define the requested framing and outfit.
            output_lines = []
        else:
            output_lines = [
                (
                    "【生成要求】按以上身份、场景和动作生成一张完整自然的竖屏动漫插画。"
                    if appearance_type == "anime"
                    else "【生成要求】按以上身份、场景和动作生成一张完整自然的竖屏照片。"
                ),
                *anatomy_constraint_lines(style="general"),
            ]
        final_style_instruction = appearance_type_output_instruction(appearance_type)
        if final_style_instruction:
            output_lines.append(final_style_instruction)

        return "\n".join(
            line
            for line in [
                f"这是 {bot_name or 'AI'} 的{subject_photo_label}。",
                "" if has_reference_image else (f"角色设定：{personality}" if personality else ""),
                *identity_lines,
                *reference_lines,
                *today_lines,
                *mode_lines,
                action_line,
                *output_lines,
            ]
            if line
        )

    def status_text(self) -> str:
        daily = self.get_daily_selfie_profile()
        lines = []
        lines.append("当前已设置 AI 自拍参考图。" if self.has_reference_image() else "当前还没有设置 AI 自拍参考图。")
        lines.append(f"形象类型：{self.appearance_type_label()}（自动=不追加类型说明；真人/动漫会补对应说明）")
        if daily:
            lines.extend(
                [
                    f"今日自拍设定：{daily.date}",
                    f"来源：{'本地随机兜底' if daily.source == 'fallback' else daily.source}",
                    f"当前状态({period_label(current_period())})：{daily.status}",
                    f"当前心情：{daily.mood}",
                ]
            )
            if daily.occasion:
                lines.append(f"今日时令 / 节日氛围：{daily.occasion}")
            if daily.outfit_by_period:
                lines.extend(
                    f"{period_label(period)}穿搭：{daily.outfit_by_period[period]}"
                    for period in DAILY_PERIOD_KEYS
                    if daily.outfit_by_period.get(period)
                )
            else:
                lines.append(f"今日穿搭：{daily.outfit}")
        return "\n".join(lines)
