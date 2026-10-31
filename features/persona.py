"""AI selfie persona management."""

from __future__ import annotations

import asyncio
import datetime
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

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
from .daily_schedule import (
    DEFAULT_BUSINESS_DAY_START,
    PERIOD_TIME_RANGES,
    DailySchedule,
    DailyScheduleStore,
    legacy_profile_to_schedule,
    project_schedule_to_period_maps,
    resolve_business_date,
    timeline_from_period_route,
)
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
DAILY_PROFILE_SCHEMA_VERSION = 4
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
    outfit_pool: list[str] = field(default_factory=list)
    scene_by_period: Dict[str, str] = field(default_factory=dict)
    action_by_period: Dict[str, str] = field(default_factory=dict)
    schedule_by_period: Dict[str, Dict[str, str]] = field(default_factory=dict)
    day_theme: str = ""
    occasion: str = ""
    llm_attempted: bool = False
    timeline: Optional[DailySchedule] = None


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


def daily_schedule_date_key(day_start: str = DEFAULT_BUSINESS_DAY_START) -> str:
    return resolve_business_date(day_start=day_start)


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


DAILY_SCENE_VARIANTS: Dict[str, tuple[str, ...]] = {
    "morning": (
        "窗边早餐桌旁的清晨光线",
        "住宅玄关镜前，刚整理好外出穿搭",
        "阳台小桌旁的清透晨光",
        "安静卧室窗边，床铺已经整理好",
    ),
    "noon": (
        "明亮咖啡馆靠窗座位",
        "书店入口的自然散射光",
        "社区花园长椅旁的午间光线",
        "干净厨房台边的白色日光",
    ),
    "afternoon": (
        "书桌前的下午散射光",
        "街边树荫下的短暂停留",
        "画廊走廊的安静背景",
        "车站玻璃窗旁的柔和反光",
    ),
    "evening": (
        "暖灯咖啡馆靠墙座位",
        "雨后街角的蓝调天光",
        "家中落地灯旁的傍晚一角",
        "天台内侧的城市晚霞背景",
    ),
    "night": (
        "客厅落地灯旁的安静角落",
        "书桌暖灯下的夜间记录",
        "卧室窗边的低亮度灯光",
        "沙发一角的舒适夜晚空间",
    ),
    "late_night": (
        "睡前床边的小夜灯",
        "浴室镜前的柔和顶灯",
        "收拾好的书桌与一盏台灯旁",
        "窗帘半掩的安静卧室一角",
    ),
}

DAILY_ACTION_VARIANTS: Dict[str, tuple[str, ...]] = {
    "morning": (
        "一手整理发丝，一手拿着水杯刚抬眼",
        "扣好袖口后自然看向镜头",
        "刚穿好鞋，在玄关停步整理衣领",
        "坐在窗边把围巾或薄外套理顺",
    ),
    "noon": (
        "刚放下咖啡杯，转身自然看向镜头",
        "翻过书页后抬眼，手指轻压住书角",
        "从座位起身时短暂停步，肩背放松",
        "把随身小包放到身侧，轻轻整理发尾",
    ),
    "afternoon": (
        "翻资料停在一页，抬头露出短暂笑意",
        "走过街角时放慢脚步，顺手整理袖口",
        "手臂轻搭栏杆或窗台，身体自然侧向镜头",
        "靠近车站玻璃窗，低头看完消息后抬眼",
    ),
    "evening": (
        "刚坐下把包放到身旁，抬眼回应镜头",
        "把外套搭在手臂上，侧身整理衣摆",
        "手指轻碰耳饰，像刚结束一段聊天",
        "站在晚霞里收拢发丝，姿态自然松弛",
    ),
    "night": (
        "坐下收拢抱枕，轻轻抬眼看向镜头",
        "整理桌面上的书和衣袖，动作刚好停住",
        "把头发挽到肩后，安静地靠着窗边",
        "端着温水靠在沙发边，表情放松",
    ),
    "late_night": (
        "整理睡前发丝后靠在床头，神情柔和",
        "拉好窗帘后回身停步，动作轻而自然",
        "关掉一本书前抬眼留下轻松的一帧",
        "把薄外套或家居披肩搭到椅背上",
    ),
}

# A daily profile is a small route through a day, not six unrelated prompts.
# Keep scene, action and status together so a fallback profile cannot combine
# an indoor gesture with an outdoor location (or vice versa).
DAILY_SCHEDULE_ROUTES: tuple[Dict[str, Any], ...] = (
    {
        "id": "rooftop_gallery_riverside",
        "theme": "从城市高处的晨光、画廊白昼到河岸蓝调的一天",
        "events": {
            "morning": {
                "status": "刚整理好出门状态，清醒而松弛，先在有植物和城市天际线的高处透气。",
                "scene": "高层天台的玻璃花房入口，晨光穿过叶片，远处是刚亮起的城市天际线。",
                "action": "推开玻璃门后停在门边，抬手收拢被风吹散的发丝，另一只手自然垂落。",
            },
            "noon": {
                "status": "上午的事情告一段落，在有开阔视野的地方慢下来，状态明亮利落。",
                "scene": "旧工业建筑改造的玻璃咖啡厅，钢梁、大片窗和桌面上的光影形成干净背景。",
                "action": "端起桌边的饮品抿过一口，放下杯子后靠回椅背，眼神自然看向窗外。",
            },
            "afternoon": {
                "status": "午后进入安静的观展节奏，神情专注但不紧绷，像偶然留下的一帧。",
                "scene": "白色当代艺术馆的长廊，天窗光落在墙面装置上，空间开阔而安静。",
                "action": "停在一件大型抽象装置前读完说明，身体微侧，手指轻轻压住随身小包的带子。",
            },
            "evening": {
                "status": "天色转蓝，走出室内后心情变得轻快，保留一天中自然累积的松弛感。",
                "scene": "河岸高架步道的蓝调时刻，水面倒映远处桥灯，风把衣摆轻轻带起。",
                "action": "双手自然搭在低矮栏杆上看了一会儿水面，随后侧身回望镜头，站姿稳定。",
            },
            "night": {
                "status": "回家后从外出的节奏收回来，保留一点夜景余韵，状态安静舒适。",
                "scene": "家中落地窗前的低亮度角落，城市灯光成为窗外细碎的背景。",
                "action": "把随身小包放在椅背上，手指碰了碰窗帘边缘，转身停在柔和灯影里。",
            },
            "late_night": {
                "status": "准备休息前只保留柔和、整洁的生活感，动作轻而自然。",
                "scene": "窗帘半掩的卧室，床边小夜灯和一张打开的书形成安静的暖色层次。",
                "action": "合上书后坐到床沿，整理睡前发丝，肩背放松地抬眼留下最后一帧。",
            },
        },
    },
    {
        "id": "seaside_lighthouse",
        "theme": "沿海风、灯塔和暮色码头串起的轻旅行日",
        "events": {
            "morning": {
                "status": "清晨带着轻旅行的期待，动作不急，先在海风里整理好状态。",
                "scene": "临海民宿的白色露台，玻璃栏杆外是薄雾、海面和远处的灯塔。",
                "action": "扶着露台栏杆看向海面，随后低头整理袖口，发丝被海风自然吹向一侧。",
            },
            "noon": {
                "status": "午间在灯塔附近短暂停留，神情明亮，像刚发现一个值得记录的角度。",
                "scene": "灯塔脚下的弧形海景咖啡馆，白墙、蓝色遮阳篷和开阔海面形成清爽背景。",
                "action": "把地图或小册子折好放在桌边，抬头望向窗外的灯塔，肩膀自然放松。",
            },
            "afternoon": {
                "status": "午后沿海边慢慢走，光线更通透，步调从容而有一点探险感。",
                "scene": "临海悬崖的木栈道，低矮护栏、礁石和连续的蓝色海面构成远景。",
                "action": "走到栈道转弯处停步，双手轻搭护栏，身体朝向海面，侧脸被风吹亮。",
            },
            "evening": {
                "status": "日落后海风转凉，沿着码头收拢一天的情绪，神情柔和又有余兴。",
                "scene": "渔港木码头的金蓝交界时刻，串灯沿着栏杆亮起，海面有细碎反光。",
                "action": "拢住被风吹起的外层衣摆，靠着系船桩旁的栏杆看向远处，站姿自然稳定。",
            },
            "night": {
                "status": "回到临海住处后安静下来，仍带着海边光线留下的清爽气息。",
                "scene": "临海民宿的木质客厅，落地窗外是深蓝海面和很远的零星灯光。",
                "action": "把外出鞋放到门边，端着温水走到窗前，靠在窗台边短暂停留。",
            },
            "late_night": {
                "status": "深夜回到轻松的休息状态，保留干净、克制的生活感。",
                "scene": "带圆窗的临海卧室，月光落在床沿，床头只亮一盏小灯。",
                "action": "拉好薄窗帘后坐到床边，手掌轻压住被角，抬眼露出放松的神情。",
            },
        },
    },
    {
        "id": "greenhouse_theatre",
        "theme": "植物温室、老电影和剧院灯影组成的文艺路线",
        "events": {
            "morning": {
                "status": "早晨从植物和水汽之间开始，心情清透，像为一天选定了柔和的节奏。",
                "scene": "城市植物园的圆顶玻璃温室，叶片、水汽和斜射晨光组成层层绿色背景。",
                "action": "站在小径边触碰一片高大的叶片，随后收回手整理发尾，身体保持自然侧向。",
            },
            "noon": {
                "status": "午间在温室旁安静用餐，状态轻快，不急着离开这片明亮的绿色。",
                "scene": "植物园外侧的半开放木质餐廊，藤蔓、白色桌布和玻璃顶投下细碎光斑。",
                "action": "合上随手翻看的书，手指轻压住书角，抬眼看向斑驳光影中的镜头。",
            },
            "afternoon": {
                "status": "下午转入室内观影，神情安静专注，动作带着生活中的小停顿。",
                "scene": "老电影院的红色走廊和复古海报墙，暖光从门缝投出一小块光斑。",
                "action": "站在放映厅门口看完海报上的片名，手臂轻搭门框，转身留下侧面轮廓。",
            },
            "evening": {
                "status": "傍晚进入剧院前的等待时刻，情绪被灯光提亮，姿态从容而有期待。",
                "scene": "剧院红绒门厅的高窗下，金色壁灯、深红地毯和镜面形成戏剧化但真实的层次。",
                "action": "在检票口旁整理耳饰和衣领，动作停住后朝侧前方看去，肩线保持放松。",
            },
            "night": {
                "status": "结束观影回到家，情绪沉静下来，仍保留一点剧院灯光的余韵。",
                "scene": "家中书架旁的暖灯阅读角，椅背、书页和窗外暗色构成干净画面。",
                "action": "把票根夹进书里放到桌面，随后靠在椅背上轻轻抬眼，神情柔和。",
            },
            "late_night": {
                "status": "睡前恢复最舒服的节奏，画面安静、整洁，不刻意摆拍。",
                "scene": "卧室床头的小夜灯照亮一角，窗外只剩模糊的城市光点。",
                "action": "把当天翻过的书放回床头，整理好发丝后靠在枕边，动作轻而自然。",
            },
        },
    },
    {
        "id": "old_city_lanterns",
        "theme": "旧城坡道、庭院茶室和灯笼街巷的城市漫游日",
        "events": {
            "morning": {
                "status": "早晨从旧城的高处出发，空气清爽，整个人还保留一点漫游前的期待。",
                "scene": "旧城坡道尽头的砖墙露台，屋檐、晾晒植物和初升的光线勾出层次。",
                "action": "一手扶住砖墙边缘看向远处屋顶，另一手整理衣领，身体轻轻侧向晨光。",
            },
            "noon": {
                "status": "午间走进安静庭院，节奏放慢，状态明亮而从容。",
                "scene": "老宅改造的庭院茶室，木格窗、青石地面和一小片竹影形成清雅背景。",
                "action": "放下茶杯后把袖口向上理顺，靠在窗边看庭院，表情自然放松。",
            },
            "afternoon": {
                "status": "午后在旧书店里避开人流，神情专注，动作带着翻找发现的兴致。",
                "scene": "旧城书店的夹层木梯与高书墙，斜窗光照亮书脊，空间深而安静。",
                "action": "从高层书架抽出一本书，站在木梯平台上翻开目录，肩背自然靠近栏杆。",
            },
            "evening": {
                "status": "天色暗下来后继续沿街散步，灯笼和湿润石板路让情绪变得柔和。",
                "scene": "旧城窄巷的连续灯笼和雨后石板路，拐角处有一小块暖色店铺灯光。",
                "action": "走到巷口时放慢脚步，手指轻碰耳饰，侧身回望灯影而不刻意停摆。",
            },
            "night": {
                "status": "回家后把旧城漫游的余韵收进安静的居家状态，心情满足而松弛。",
                "scene": "家中靠窗的木桌和一盏琥珀色台灯旁，窗外还能看见远处的街灯。",
                "action": "把买回的小书放到桌边，端起温水靠在桌沿，低头整理书签后抬眼。",
            },
            "late_night": {
                "status": "深夜只剩柔和的灯光和放松的神情，准备把一天慢慢收尾。",
                "scene": "带木质床头柜的卧室，薄窗帘透进一点城市光，空间安静完整。",
                "action": "拉好窗帘后回到床边，轻轻把头发拢到肩后，动作自然地停住。",
            },
        },
    },
    {
        "id": "rain_tram_neon",
        "theme": "雨天电车、玻璃展馆和霓虹倒影的电影感日常",
        "events": {
            "morning": {
                "status": "雨天早晨出门前很安静，衣着和头发整理得利落，心情没有被天气打乱。",
                "scene": "公寓高层的雨窗和窄阳台，玻璃上有水痕，远处楼群被灰蓝晨光压低。",
                "action": "站在窗边看了一眼雨势，手指收拢耳侧发丝，另一只手自然握住薄外套边缘。",
            },
            "noon": {
                "status": "午间在电车站短暂停留，雨声和城市人流把状态衬得清醒。",
                "scene": "有透明雨棚的城市电车站，湿亮地面反射站牌灯光，背景保持空旷。",
                "action": "把伞尖收在身侧，低头看完一条消息后抬眼，站姿端正而不僵硬。",
            },
            "afternoon": {
                "status": "下午在室内避雨，节奏慢下来，神情专注而有一点好奇。",
                "scene": "玻璃穹顶的摄影与设计展馆，雨滴在顶棚上形成纹理，墙面作品保持简洁。",
                "action": "站在一面半透明装置前观察光影，手臂轻搭身侧栏杆，身体自然侧向光源。",
            },
            "evening": {
                "status": "傍晚乘电车穿过亮起的街区，城市倒影让心情变得轻盈。",
                "scene": "夜色中的电车靠窗座位，雨水拖开霓虹倒影，车厢内只有柔和顶灯。",
                "action": "额头轻靠车窗看了一会儿倒影，随后转向镜头整理袖口，动作克制自然。",
            },
            "night": {
                "status": "回到家后换回舒适节奏，雨声还在窗外，整个人明显松弛下来。",
                "scene": "家中玄关连着暖灯客厅，湿伞被收在门边，窗外雨线成为远景。",
                "action": "把外套搭到玄关椅背上，走到客厅灯下停步，抬手整理刚被雨气打乱的发丝。",
            },
            "late_night": {
                "status": "深夜的雨声变轻，准备休息前只留下柔和而完整的生活片段。",
                "scene": "卧室窗边的小桌和暖色台灯，玻璃上的雨痕在背景中保持轻微存在感。",
                "action": "关掉桌面上的灯前看向窗外，随后坐回床沿，双手自然放在膝上。",
            },
        },
    },
    {
        "id": "mountain_lake_cabin",
        "theme": "山间缆车、湖畔木栈道和小屋灯火的松弛旅程",
        "events": {
            "morning": {
                "status": "清晨准备去山里走走，状态清爽，穿搭和随身物品都已整理妥当。",
                "scene": "山间缆车站的开放平台，薄雾、松树和远处山谷被晨光拉开层次。",
                "action": "站在站台边确认缆车方向，抬手压住被风吹起的发丝，肩背自然挺直。",
            },
            "noon": {
                "status": "午间到达半山，空气清凉，像在风景里短暂放空。",
                "scene": "半山木屋咖啡馆的宽窗边，窗外是层叠山脊和一小片明亮天空。",
                "action": "把杯子放回木桌上，双手交叠搭在桌边，侧头看向窗外的山线。",
            },
            "afternoon": {
                "status": "午后沿湖边慢走，状态安静又有活力，动作保持可以维持的自然重心。",
                "scene": "高山湖泊的木栈道，清澈水面、低矮护栏和远处岩壁组成开阔背景。",
                "action": "在栈道转角停下，把手掌轻放在护栏上，身体朝向湖面，头部保持自然侧转。",
            },
            "evening": {
                "status": "日落前回到山口，风景从明亮变成金色，神情带着放松后的满足。",
                "scene": "山口观景台的晚霞与薄雾，木质观景框把远处的山谷像画面一样框住。",
                "action": "靠在观景框侧边看完最后一束光，随后把围巾或外层衣摆整理好，姿态松弛。",
            },
            "night": {
                "status": "回到山间小屋后安静下来，身体放松，保留一点户外空气的清爽感。",
                "scene": "山间小屋的壁炉与木桌一角，窗外是深色树林和零星雾气。",
                "action": "把随身小包放到木椅上，靠近壁炉旁的桌沿，双手捧住温水短暂停留。",
            },
            "late_night": {
                "status": "深夜在安静的山屋里准备休息，灯光低而温暖，动作慢下来。",
                "scene": "小屋卧室的木质床头和圆窗，窗外只有深蓝夜色与一层薄雾。",
                "action": "关好窗锁后坐到床边，整理发丝和袖口，肩膀自然下沉。",
            },
        },
    },
    {
        "id": "observatory_bookstore",
        "theme": "观景台、玻璃书店和夜色书桌之间的都会灵感日",
        "events": {
            "morning": {
                "status": "早晨先到城市高处透气，神情清醒，像给自己留出一段安静的开场。",
                "scene": "城市观景塔的环形平台，玻璃护栏外是日光刚铺开的楼群和远处河面。",
                "action": "手掌轻搭玻璃护栏看向远处，随后回身整理发尾，保持自然侧身构图。",
            },
            "noon": {
                "status": "午间进入书店咖啡区，状态明亮，像在熟悉的书页里补充能量。",
                "scene": "通往夹层的玻璃书店，书墙、旋转楼梯和高窗光线形成清晰纵深。",
                "action": "从书架抽出一本薄书夹在臂弯，另一只手轻压书脊，抬眼看向楼梯方向。",
            },
            "afternoon": {
                "status": "午后在设计展厅里慢慢看展，神情专注，动作自然而有分寸。",
                "scene": "旧仓库改造的设计展厅，天花吊灯、粗粝墙面和一张孤立展台形成对比。",
                "action": "站在展台侧面读完卡片说明，手指轻碰下巴后放下，身体微侧向展品。",
            },
            "evening": {
                "status": "傍晚站到屋顶观景平台，城市灯光逐渐亮起，情绪变得开阔。",
                "scene": "高层屋顶的透明风挡和城市夜景，远处桥梁、车流和晚霞形成连续光带。",
                "action": "双手搭在风挡边缘看向车流，随后转过肩线而不扭动身体，留下自然侧脸。",
            },
            "night": {
                "status": "回家后把一天的灵感整理下来，状态安静但并不疲惫。",
                "scene": "家中靠窗书桌和一盏窄幅台灯，桌面只放着书、笔和刚买的书签。",
                "action": "把书签夹进笔记本，手肘轻靠桌面，停在写完一行字后的自然姿态。",
            },
            "late_night": {
                "status": "睡前把桌面收拾干净，情绪柔和，留下简单而有画面感的一帧。",
                "scene": "书桌旁连着卧室的低亮度灯影，窗帘留一条缝让城市光线进入。",
                "action": "合上笔记本放到一旁，起身拉好窗帘后回到床边，动作轻而稳定。",
            },
        },
    },
    {
        "id": "courtyard_night_market",
        "theme": "庭院花影、旧唱片店和夜市灯火的轻快城市日",
        "events": {
            "morning": {
                "status": "早晨从有花影的庭院开始，状态清爽，像刚把一天的心情调到喜欢的频道。",
                "scene": "带拱门的城市庭院，爬藤、白色长椅和晨光在地面留下斑驳影子。",
                "action": "坐在长椅边系好鞋带，起身后轻抚一片垂下的藤叶，动作自然不刻意。",
            },
            "noon": {
                "status": "午间在街区小店之间短暂停留，神情轻快，保留一点发现新东西的兴致。",
                "scene": "老街转角的唱片店门口，彩色玻璃橱窗和窄门廊形成有层次的背景。",
                "action": "从橱窗前退开半步看完封面，手指轻碰耳饰，身体自然侧向店门。",
            },
            "afternoon": {
                "status": "午后进入安静的室内空间，节奏放缓，神情专注而有一点满足。",
                "scene": "唱片店二层的木质试听角，圆窗、唱片墙和一张复古扶手椅构成画面。",
                "action": "把选中的唱片放到桌边，手掌轻扶椅背，站在窗光里听完一小段旋律。",
            },
            "evening": {
                "status": "天黑后去夜市吃点简单的小食，灯光让状态变得鲜活，但动作仍然自然。",
                "scene": "河边夜市的高处步道，串灯、彩色摊棚和远处水面形成丰富但不拥挤的背景。",
                "action": "避开人流停在栏杆旁，手里只拿一杯饮品，抬眼看向灯串后轻轻整理发尾。",
            },
            "night": {
                "status": "回家后把夜市的热闹收进舒适的室内，神情满足而放松。",
                "scene": "家中厨房与客厅相连的一角，暖灯照着干净台面，窗外仍有一点城市亮光。",
                "action": "把饮品杯放到台面，靠在厨房门边整理袖口，停住后看向镜头。",
            },
            "late_night": {
                "status": "深夜回到柔软的休息状态，画面干净，动作像生活里随手的一帧。",
                "scene": "卧室床边的低矮灯和一把放好外套的椅子，空间安静而有完整层次。",
                "action": "把外套搭到椅背上，坐到床沿轻轻揉松肩颈，随后抬眼放松微笑。",
            },
        },
    },
)


def _select_daily_schedule_route(
    *,
    avoid_scenes: Optional[set[str]] = None,
    avoid_actions: Optional[set[str]] = None,
    avoid_statuses: Optional[set[str]] = None,
    avoid_route_ids: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """Choose one coherent route, preferring a route not used by a refresh."""
    blocked_scenes = {str(item).strip() for item in (avoid_scenes or set()) if str(item).strip()}
    blocked_actions = {str(item).strip() for item in (avoid_actions or set()) if str(item).strip()}
    blocked_statuses = {str(item).strip() for item in (avoid_statuses or set()) if str(item).strip()}
    blocked_routes = {str(item).strip() for item in (avoid_route_ids or set()) if str(item).strip()}

    def score(route: Dict[str, Any]) -> int:
        events = route.get("events") or {}
        score = sum(
            (event.get("scene") in blocked_scenes)
            + (event.get("action") in blocked_actions)
            + (event.get("status") in blocked_statuses)
            for event in events.values()
            if isinstance(event, dict)
        )
        if str(route.get("id") or "") in blocked_routes:
            score += 100
        return score

    best_score = min(score(route) for route in DAILY_SCHEDULE_ROUTES)
    candidates = [route for route in DAILY_SCHEDULE_ROUTES if score(route) == best_score]
    return random.choice(candidates) if candidates else DAILY_SCHEDULE_ROUTES[0]


def _route_context_maps(route: Dict[str, Any]) -> tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    events = route.get("events") if isinstance(route, dict) else {}
    statuses: Dict[str, str] = {}
    scenes: Dict[str, str] = {}
    actions: Dict[str, str] = {}
    for period in DAILY_PERIOD_KEYS:
        event = events.get(period) if isinstance(events, dict) else {}
        event = event if isinstance(event, dict) else {}
        statuses[period] = str(event.get("status") or "").strip()
        scenes[period] = str(event.get("scene") or "").strip()
        actions[period] = str(event.get("action") or "").strip()
    return statuses, scenes, actions


def _pick_daily_contexts(
    pools: Dict[str, tuple[str, ...]],
    *,
    avoid: Optional[set[str]] = None,
) -> Dict[str, str]:
    blocked = {str(item).strip() for item in (avoid or set()) if str(item).strip()}
    selected: set[str] = set()
    result: Dict[str, str] = {}
    for period in DAILY_PERIOD_KEYS:
        options = list(pools.get(period, ()))
        candidates = [item for item in options if item not in blocked and item not in selected]
        if not candidates:
            candidates = [item for item in options if item not in selected] or options
        value = random.choice(candidates) if candidates else ""
        result[period] = value
        if value:
            selected.add(value)
    return result


def _pick_daily_context_pairs(
    scene_pools: Dict[str, tuple[str, ...]],
    action_pools: Dict[str, tuple[str, ...]],
    *,
    avoid_scenes: Optional[set[str]] = None,
    avoid_actions: Optional[set[str]] = None,
) -> tuple[Dict[str, str], Dict[str, str]]:
    blocked_scenes = {str(item).strip() for item in (avoid_scenes or set()) if str(item).strip()}
    blocked_actions = {str(item).strip() for item in (avoid_actions or set()) if str(item).strip()}
    used_scenes: set[str] = set()
    used_actions: set[str] = set()
    scenes: Dict[str, str] = {}
    actions: Dict[str, str] = {}
    for period in DAILY_PERIOD_KEYS:
        pairs = list(zip(scene_pools.get(period, ()), action_pools.get(period, ())))
        candidates = [
            pair
            for pair in pairs
            if pair[0] not in blocked_scenes
            and pair[1] not in blocked_actions
            and pair[0] not in used_scenes
            and pair[1] not in used_actions
        ]
        if not candidates:
            candidates = [pair for pair in pairs if pair[0] not in used_scenes and pair[1] not in used_actions] or pairs
        scene, action = random.choice(candidates) if candidates else ("", "")
        scenes[period] = scene
        actions[period] = action
        if scene:
            used_scenes.add(scene)
        if action:
            used_actions.add(action)
    return scenes, actions


def _complete_daily_contexts(
    pools: Dict[str, tuple[str, ...]],
    values: Optional[Dict[str, str]],
) -> Dict[str, str]:
    provided = {
        str(key): str(value).strip()
        for key, value in (values or {}).items()
        if str(value).strip()
    }
    generated = _pick_daily_contexts(pools, avoid=set(provided.values()))
    return {
        period: provided.get(period) or generated.get(period, "")
        for period in DAILY_PERIOD_KEYS
    }


DAILY_EXTRA_OUTFIT_POOL = (
    "米白短款针织开衫叠穿浅蓝衬衫，配深灰直筒长裤、棕色便鞋和细金属耳饰。",
    "烟灰短款西装外套搭配奶油白针织背心、墨绿半身裙和黑色乐福鞋，线条利落。",
    "浅卡其工装夹克内搭白色T恤，配深蓝丹宁长裙、米白帆布鞋和窄版皮带。",
    "雾蓝色薄风衣搭配奶油色细针织衫、炭灰阔腿裤和银灰运动鞋，层次轻盈。",
    "酒红短款针织外套叠穿浅灰衬衫，配黑色高腰长裤、玛丽珍鞋和一枚小胸针。",
    "浅杏色亚麻立领衬衫配深橄榄阔腿裤、棕色系带便鞋和简洁帆布腰带。",
    "藏蓝细针织上衣配米白A字中长裙、红棕便鞋和小型珍珠耳钉，干净克制。",
    "浅灰紫开衫叠穿白色背心，配雾粉直筒长裙、米白低跟鞋和珍珠发夹。",
    "黑白细格纹马甲叠穿浅蓝衬衫，配黑色高腰长裤、黑色乐福鞋和银色窄戒指。",
    "轻薄白色亚麻衬衫外搭浅色背心，配砖红A字裙、奶油白帆布鞋和丝巾发带。",
    "米白连帽卫衣配深蓝运动长裙、灰白运动鞋和低调棒球帽，休闲但轮廓清楚。",
    "奶油黄罗纹针织上衣配烟灰直筒长裤、深棕短靴和复古方形腕表。",
)

# These are intentionally component-based: only compatible palettes and style
# families are combined. One-piece looks use an empty bottom field so they
# cannot accidentally receive a second skirt or pair of trousers.
DAILY_MIX_TOPS = (
    ("轻法式复古风", "奶油白方领针织上衣", {"denim", "dark", "cream", "brown"}, {"french", "natural"}, "细金属耳饰"),
    ("清爽学院风", "浅蓝翻领衬衫", {"denim", "navy", "gray", "dark", "cream"}, {"preppy", "minimal"}, "窄版深棕皮带"),
    ("极简通勤风", "雾蓝细条纹衬衫", {"gray", "dark", "cream", "navy"}, {"minimal", "work"}, "小型银色耳圈"),
    ("自然休闲风", "浅杏色薄针织上衣", {"denim", "olive", "dark", "cream"}, {"casual", "natural"}, "小巧丝巾发带"),
    ("运动街头风", "米白短款连帽卫衣", {"navy", "gray", "denim", "dark"}, {"sport", "casual"}, "低调棒球帽"),
    ("文艺亚麻风", "燕麦色亚麻立领衬衫", {"olive", "denim", "brown", "cream"}, {"natural", "casual"}, "窄边帆布腰带"),
    ("复古甜酷风", "炭灰短款拉链针织上衣", {"dark", "denim", "wine", "gray"}, {"retro", "casual"}, "一枚哑光金属胸针"),
    ("轻盈日系风", "浅青翻领短袖衬衫", {"cream", "denim", "gray", "dark"}, {"japanese", "preppy"}, "小型珍珠耳钉"),
)

DAILY_MIX_BOTTOMS = (
    ("深蓝高腰直筒牛仔裤", "denim", {"casual", "french", "natural", "sport", "preppy"}, "白色低帮帆布鞋"),
    ("炭灰色高腰阔腿裤", "gray", {"minimal", "work", "natural"}, "银灰复古运动鞋"),
    ("墨绿色百褶长裙", "dark", {"preppy", "french", "natural"}, "黑色短靴"),
    ("奶油白A字中长裙", "cream", {"french", "preppy", "natural", "japanese"}, "棕色便鞋"),
    ("深橄榄色阔腿长裤", "olive", {"natural", "casual"}, "棕色系带便鞋"),
    ("深蓝侧开衩运动长裙", "navy", {"sport", "casual"}, "灰白厚底运动鞋"),
    ("墨黑色高腰直筒裙", "dark", {"french", "minimal", "work", "retro"}, "黑色玛丽珍鞋"),
    ("焦糖色高腰伞裙", "brown", {"french", "natural", "retro"}, "深棕短靴"),
    ("酒红色直筒半身裙", "wine", {"retro", "french"}, "酒红玛丽珍鞋"),
)


def _daily_mix_match_looks() -> list[tuple[str, str, str, str, str]]:
    looks: list[tuple[str, str, str, str, str]] = []
    for style, top, palettes, styles, accessory in DAILY_MIX_TOPS:
        for bottom, palette, bottom_styles, shoes in DAILY_MIX_BOTTOMS:
            if palette in palettes and styles.intersection(bottom_styles):
                looks.append((style, top, bottom, shoes, accessory))
    # Dresses are complete looks; they deliberately have no bottom component.
    looks.extend([
        ("轻盈日系风", "雾蓝色棉麻连衣裙", "", "棕色便鞋", "小型珍珠耳钉"),
        ("温柔复古风", "奶油白方领针织连衣裙", "", "深棕短靴", "复古方形腕表"),
    ])
    random.shuffle(looks)
    return looks


def _complete_outfit_pool(
    outfit_by_period: Optional[Dict[str, str]] = None,
    provided: Any = None,
    *,
    minimum: int = 10,
) -> list[str]:
    values: list[str] = []
    if isinstance(provided, str):
        candidates = [provided]
    elif isinstance(provided, (list, tuple)):
        candidates = list(provided)
    else:
        candidates = []
    period_map = outfit_by_period or {}
    period_values = [period_map.get("morning"), *period_map.values()]
    # The morning look is the stable `main` reference for a daily schedule.
    for value in [*period_values, *candidates]:
        text = str(value or "").strip()
        if text and len(text) <= 220 and text not in values:
            values.append(text)
        if len(values) >= 24:
            return values
    for value in DAILY_EXTRA_OUTFIT_POOL:
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)
        if len(values) >= max(1, minimum) or len(values) >= 24:
            break
    return values


def _clean_schedule_node(value: Any) -> Dict[str, str]:
    """Normalize one daily event without letting arbitrary JSON leak through."""
    if not isinstance(value, dict):
        return {}
    aliases = {
        "status": ("status", "state"),
        "scene": ("scene", "setting", "location"),
        "action": ("action", "gesture", "pose"),
        "outfit_ref": ("outfit_ref", "outfit", "clothes_ref"),
        "outfit_change": ("outfit_change", "change", "clothing_change"),
    }
    result: Dict[str, str] = {}
    for field_name, keys in aliases.items():
        for key in keys:
            raw = value.get(key)
            if raw is not None and str(raw).strip():
                result[field_name] = str(raw).strip()
                break
    return result


def _normalize_schedule_by_period(value: Any) -> Dict[str, Dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    result: Dict[str, Dict[str, str]] = {}
    for period in DAILY_PERIOD_KEYS:
        node = _clean_schedule_node(value.get(period))
        if node:
            node.setdefault("outfit_ref", "main")
            result[period] = node
    return result


def _remove_outer_layer(outfit: str) -> str:
    """Describe a natural layer change while retaining the main outfit details."""
    text = str(outfit or "").strip()
    if not text:
        return ""
    # Local fallback looks use the compact form "外层敞开穿着，内搭...".
    # Remove that first clause instead of leaking a literal "脱去外层" into
    # the resolved outfit reference.
    compact = re.sub(
        r"^[^，。；]{1,28}(?:敞开穿着|披着|搭在肩上)[，。；]\s*",
        "",
        text,
        count=1,
    )
    if compact != text:
        return compact.strip(" ，。；")
    without_outer = re.sub(
        r"(?:外搭|外穿|搭配|配)[^，。；；]{0,18}(?:外套|风衣|开衫|夹克|大衣|西装外套)[，。；；]?",
        "",
        text,
        count=1,
    )
    without_outer = re.sub(r"\s+", " ", without_outer).strip(" ，。；")
    return without_outer or f"保留内搭与下装，脱去外层：{text}"


def _find_outfit_ref(ref: str, outfit_pool: list[str], main_outfit: str) -> str:
    token = re.sub(r"[\s\-]+", "_", str(ref or "").strip().lower())
    if token in {"", "main", "same", "default", "main_outfit", "primary"}:
        return main_outfit
    if token in {"main_minus_outer", "minus_outer", "remove_outer", "no_outer"}:
        return _remove_outer_layer(main_outfit)
    if token in {"home", "homewear", "casual_home"}:
        for item in outfit_pool:
            if any(word in item for word in ("家居", "居家", "睡衣", "室内服")):
                return item
        return main_outfit
    if token in {"sleep", "sleepwear", "nightwear"}:
        for item in outfit_pool:
            if any(word in item for word in ("睡衣", "睡前", "家居连衣裙")):
                return item
        return main_outfit
    if token in {"previous", "continue", "last"}:
        return main_outfit
    # A schedule may reference an exact candidate by its full text.
    for item in outfit_pool:
        if token == re.sub(r"[\s\-]+", "_", item.lower()):
            return item
    return ""


def _schedule_from_legacy_maps(
    *,
    status_by_period: Dict[str, str],
    outfit_by_period: Dict[str, str],
    scene_by_period: Dict[str, str],
    action_by_period: Dict[str, str],
    outfit_pool: Optional[list[str]] = None,
) -> Dict[str, Dict[str, str]]:
    """Build event nodes for profiles written before schedule support."""
    main = str(
        (outfit_pool or [None])[0]
        or outfit_by_period.get("morning")
        or next(iter(outfit_by_period.values()), "")
    ).strip()
    schedule: Dict[str, Dict[str, str]] = {}
    previous_outfit = main
    for period in DAILY_PERIOD_KEYS:
        outfit = str(outfit_by_period.get(period) or previous_outfit or main).strip()
        ref = "main" if outfit == main else "custom"
        change = ""
        if outfit != previous_outfit:
            if outfit == _remove_outer_layer(main):
                ref = "main_minus_outer"
                change = "回到室内后脱下外层，保留内搭和下装。"
            elif any(word in outfit for word in ("家居", "居家", "睡衣")):
                ref = "sleep" if "睡衣" in outfit else "home"
                change = "回家后换成舒适的居家状态。"
            else:
                # Keep the exact candidate as the reference so a reload can
                # resolve the same look instead of losing a custom transition.
                ref = outfit
                change = f"自然换成：{outfit}"
        schedule[period] = {
            "status": str(status_by_period.get(period) or "").strip(),
            "scene": str(scene_by_period.get(period) or "").strip(),
            "action": str(action_by_period.get(period) or "").strip(),
            "outfit_ref": ref,
            "outfit_change": change,
        }
        previous_outfit = outfit
    return schedule


def _derive_period_maps_from_schedule(
    schedule: Dict[str, Dict[str, str]],
    outfit_pool: list[str],
    legacy_outfits: Optional[Dict[str, str]] = None,
    legacy_status: Optional[Dict[str, str]] = None,
    legacy_scenes: Optional[Dict[str, str]] = None,
    legacy_actions: Optional[Dict[str, str]] = None,
) -> tuple[Dict[str, str], Dict[str, str], Dict[str, str], Dict[str, str]]:
    """Resolve event nodes into the old per-period maps used by callers."""
    legacy = legacy_outfits or {}
    legacy_status = legacy_status or {}
    legacy_scenes = legacy_scenes or {}
    legacy_actions = legacy_actions or {}
    main = str(outfit_pool[0] if outfit_pool else legacy.get("morning") or "").strip()
    outfits: Dict[str, str] = {}
    statuses: Dict[str, str] = {}
    scenes: Dict[str, str] = {}
    actions: Dict[str, str] = {}
    previous = main
    for period in DAILY_PERIOD_KEYS:
        node = _clean_schedule_node(schedule.get(period))
        ref = node.get("outfit_ref", "main")
        resolved = _find_outfit_ref(ref, outfit_pool, main)
        if ref.lower() in {"previous", "continue", "last"}:
            resolved = previous
        if not resolved:
            resolved = str(legacy.get(period) or previous or main).strip()
        outfits[period] = resolved
        previous = resolved
        statuses[period] = node.get("status") or str(legacy_status.get(period) or "").strip()
        scenes[period] = node.get("scene") or str(legacy_scenes.get(period) or "").strip()
        actions[period] = node.get("action") or str(legacy_actions.get(period) or "").strip()
    return outfits, statuses, scenes, actions


def _complete_schedule_from_maps(
    schedule: Dict[str, Dict[str, str]],
    *,
    status_by_period: Dict[str, str],
    outfit_by_period: Dict[str, str],
    scene_by_period: Dict[str, str],
    action_by_period: Dict[str, str],
    outfit_pool: Optional[list[str]] = None,
) -> Dict[str, Dict[str, str]]:
    base = _schedule_from_legacy_maps(
        status_by_period=status_by_period,
        outfit_by_period=outfit_by_period,
        scene_by_period=scene_by_period,
        action_by_period=action_by_period,
        outfit_pool=outfit_pool,
    )
    for period, node in (schedule or {}).items():
        if period in base:
            base[period].update(_clean_schedule_node(node))
    return base


def fallback_daily_profile(
    date: str,
    seed: str,
    *,
    llm_attempted: bool = False,
    avoid_outfits: Optional[set[str]] = None,
    avoid_statuses: Optional[set[str]] = None,
    avoid_scenes: Optional[set[str]] = None,
    avoid_actions: Optional[set[str]] = None,
    avoid_route_ids: Optional[set[str]] = None,
) -> DailySelfieProfile:
    try:
        month = datetime.date.fromisoformat(date).month
    except (TypeError, ValueError):
        month = 4
    seasonal_layers = {
        12: ["莓红短款呢外套", "深灰短款羊毛夹克", "奶油白绗缝外套"],
        1: ["莓红短款呢外套", "深灰短款羊毛夹克", "奶油白绗缝外套"],
        2: ["莓红短款呢外套", "深灰短款羊毛夹克", "奶油白绗缝外套"],
        3: ["浅卡其薄风衣", "雾蓝短款针织开衫", "米白轻薄工装夹克"],
        4: ["浅卡其薄风衣", "雾蓝短款针织开衫", "米白轻薄工装夹克"],
        5: ["浅卡其薄风衣", "雾蓝短款针织开衫", "米白轻薄工装夹克"],
        6: ["轻薄白色亚麻衬衫外搭", "浅灰薄针织开衫", "淡青色防晒衬衫"],
        7: ["轻薄白色亚麻衬衫外搭", "浅灰薄针织开衫", "淡青色防晒衬衫"],
        8: ["轻薄白色亚麻衬衫外搭", "浅灰薄针织开衫", "淡青色防晒衬衫"],
        9: ["深橄榄短夹克", "烟灰薄风衣", "酒红短款针织外套"],
        10: ["深橄榄短夹克", "烟灰薄风衣", "酒红短款针织外套"],
        11: ["深橄榄短夹克", "烟灰薄风衣", "酒红短款针织外套"],
    }[month]
    day_looks = [
        ("轻法式复古", "莓果红方领针织上衣", "深蓝高腰直筒牛仔裤", "焦糖棕乐福鞋", "细金属耳饰"),
        ("清爽学院风", "奶油黄翻领针织背心叠穿白衬衫", "墨绿色百褶长裙", "黑色短靴", "窄版深棕皮带"),
        ("极简通勤风", "雾蓝细条纹衬衫", "炭灰色高腰阔腿裤", "银灰复古运动鞋", "小型银色耳圈"),
        ("复古甜酷风", "炭灰短款拉链针织上衣", "酒红色直筒半身裙", "黑色玛丽珍鞋", "一枚亮色发夹"),
        ("温柔知性风", "奶油白方领衬衫", "焦糖色高腰伞裙", "深棕短靴", "复古方形腕表"),
        ("自然休闲风", "浅杏色薄针织上衣", "靛青色直筒牛仔裤", "白色低帮帆布鞋", "一只小巧丝巾发带"),
        ("都市利落风", "雾紫色短款开衫叠穿白色背心", "深灰高腰西装裤", "黑色尖头平底鞋", "银色细项链"),
        ("轻盈日系风", "浅青色翻领短袖衬衫", "奶油白A字中长裙", "棕色便鞋", "小型珍珠耳钉"),
        ("轻户外工装风", "深蓝细针织马甲叠穿条纹衬衫", "浅灰高腰工装长裤", "米白复古运动鞋", "窄版帆布腰带"),
        ("暗色复古风", "砖红色圆领针织上衣", "墨黑色高腰直筒裙", "酒红玛丽珍鞋", "一枚哑光金属胸针"),
        ("运动街头风", "米白短款连帽卫衣", "深蓝侧开衩运动长裙", "灰白厚底运动鞋", "低调棒球帽"),
        ("文艺亚麻风", "燕麦色亚麻立领衬衫", "深橄榄色阔腿长裤", "棕色系带便鞋", "窄边草编腰带"),
        ("干净海军风", "藏蓝针织短袖", "米白高腰直筒长裤", "红棕色便鞋", "一条细条纹发带"),
        ("复古丹宁风", "浅蓝牛仔短外套叠穿白色T恤", "砖红色A字中长裙", "奶油白帆布鞋", "小巧帆布斜挎包"),
        ("柔雾针织风", "浅灰紫薄针织套头衫", "雾粉色直筒长裙", "米白低跟鞋", "一枚珍珠发夹"),
        ("结构感马甲风", "黑白细格纹西装马甲叠穿浅蓝衬衫", "黑色高腰长裤", "黑色乐福鞋", "银色窄戒指"),
    ]
    day_looks.extend(_daily_mix_match_looks())
    avoid = {str(value).strip() for value in (avoid_outfits or set()) if str(value).strip()}
    used_outfits: set[str] = set()

    def choose_rendered(
        items: list[Any],
        render: Callable[[Any], str],
        *,
        blocked: Optional[set[str]] = None,
    ) -> str:
        rendered = [(item, render(item)) for item in items]
        blocked_values = avoid | set(blocked or set())
        candidates = [value for value in rendered if value[1] not in blocked_values]
        return random.choice(candidates or rendered)[1]

    def render_day_look(item: tuple[str, str, str, str, str], layer: str) -> str:
        style, top, bottom, shoes, accessory = item
        if not bottom:
            return f"{style}：{top}，配{shoes}和{accessory}。"
        return (
            f"{style}：{layer}敞开穿着，内搭{top}，"
            f"配{bottom}和{shoes}，以{accessory}作一个清晰点缀。"
        )

    day_candidates = [(layer, look) for layer in seasonal_layers for look in day_looks]
    day_rendered = [
        (
            item,
            render_day_look(item[1], item[0]),
        )
        for item in day_candidates
    ]

    def choose_day_outfit(*, exclude: Optional[set[str]] = None) -> str:
        blocked_values = avoid | set(exclude or set())
        candidates = [value for _, value in day_rendered if value not in blocked_values]
        return random.choice(candidates or [value for _, value in day_rendered])

    # Keep one coherent daytime look by default. A rare change can happen at a
    # real transition, but a new outfit is never assigned merely because the
    # clock crossed into another period.
    day_outfit = choose_day_outfit()
    day_outfits: Dict[str, str] = {
        "morning": day_outfit,
        "noon": day_outfit,
        "afternoon": day_outfit,
    }
    used_outfits.update(day_outfits.values())
    evening_looks = [
        "墨绿色细针织连衣裙，外搭短款米白开衫，配棕色低跟便鞋和细金属耳饰。",
        "雾蓝衬衫裙，腰间用窄版深棕腰带收束，配奶油白便鞋和小银圈耳饰。",
        "酒红色针织上衣配深灰直筒半身裙，外搭短款黑色夹克，配黑色乐福鞋。",
        "奶油白针织上衣配深蓝高腰长裙，外搭浅灰开衫，配一枚复古腕表。",
        "浅杏色薄风衣内搭白色细针织衫和炭灰长裤，配棕色短靴和丝巾发带。",
        "深蓝短款针织上衣配焦糖色伞裙，外搭酒红薄外套，配深棕便鞋。",
    ]
    evening_outfit = (
        day_outfits["afternoon"]
        if random.random() < 0.68
        else _remove_outer_layer(day_outfits["afternoon"])
    )
    if not evening_outfit or evening_outfit == day_outfits["afternoon"]:
        evening_outfit = day_outfits["afternoon"]
    used_outfits.add(evening_outfit)
    home_looks = [
        "雾蓝色柔软针织上衣配奶油白直筒长裤，剪裁宽松但有清楚层次。",
        "深莓色薄款针织套头衫配炭灰色舒适长裤，配色沉稳有重点。",
        "浅绿色棉质短袖上衣配深蓝色宽松长裤，简洁清爽且版型利落。",
        "奶油黄罗纹针织上衣配烟灰色直筒长裤，柔软材质与清晰轮廓兼顾。",
        "雾粉色家居针织开衫配米白色直筒长裤，内搭干净的浅灰背心。",
        "浅蓝色宽松棉质衬衫配柔灰色针织长裤，袖口自然挽起，舒适但不邋遢。",
        "奶油白薄卫衣配深绿色家居长裤，配色安静，轮廓清楚利落。",
        "浅紫灰色针织套头衫配暖灰色阔腿长裤，材质柔软，整体轻松整洁。",
        "雾灰蓝柔软睡衣套装，配室内软底拖鞋和简洁发带，舒适但保持整洁轮廓。",
        "奶油白针织家居连衣裙，配浅灰室内拖鞋和一枚小巧发夹，适合睡前状态。",
    ]
    home_outfit = choose_rendered(home_looks, lambda item: item, blocked=used_outfits)
    used_outfits.add(home_outfit)
    late_home_candidate = choose_rendered(home_looks, lambda item: item, blocked=used_outfits)
    # The route's night and late-night events are home-based. Returning home
    # is therefore the natural clothing transition; staying in the outdoor
    # look here creates an avoidable scene/outfit mismatch.
    night_outfit = home_outfit
    late_home_outfit = (
        night_outfit
        if random.random() < 0.74
        else late_home_candidate
    )
    outfit_by_period = {
        "morning": day_outfits["morning"],
        "noon": day_outfits["noon"],
        "afternoon": day_outfits["afternoon"],
        "evening": evening_outfit,
        "night": home_outfit,
        "late_night": late_home_outfit,
    }
    special_pool_candidates = [
        render_day_look(item[1], item[0])
        for item in day_candidates
        if not item[1][2]
    ] + [
        str(item)
        for item in home_looks
        if "睡衣" in str(item) or "连衣裙" in str(item)
    ]
    pool_candidates = special_pool_candidates + [
        render_day_look(item[1], item[0])
        for item in day_candidates
        if item[1][2]
    ] + [str(item) for item in [*evening_looks, *home_looks] if "睡衣" not in str(item) and "连衣裙" not in str(item)]
    random.shuffle(pool_candidates)
    outfit_pool = _complete_outfit_pool(
        outfit_by_period,
        [*outfit_by_period.values(), *pool_candidates],
    )
    route = _select_daily_schedule_route(
        avoid_scenes=avoid_scenes,
        avoid_actions=avoid_actions,
        avoid_statuses=avoid_statuses,
        avoid_route_ids=avoid_route_ids,
    )
    status_by_period, scene_by_period, action_by_period = _route_context_maps(route)
    schedule_by_period = _schedule_from_legacy_maps(
        status_by_period=status_by_period,
        outfit_by_period=outfit_by_period,
        scene_by_period=scene_by_period,
        action_by_period=action_by_period,
        outfit_pool=outfit_pool,
    )
    mood = random_pick([
        "放松、安静、柔和", "清爽、自然、轻松", "温柔、治愈、稳定", "元气、明亮、轻快",
        "清醒、松弛、有一点小得意", "安静、利落、带着生活气", "柔和、从容、不刻意",
    ])
    timeline = timeline_from_period_route(
        route,
        date=date,
        seed=seed,
        outfit_by_period=outfit_by_period,
        period_keys=DAILY_PERIOD_KEYS,
    )
    timeline.mood = mood
    period = current_period()
    return DailySelfieProfile(
        date=date,
        outfit=outfit_by_period.get(period, day_outfit),
        status=status_by_period.get(period, "处于自然放松的日常状态，画面安静、统一、真实。"),
        status_by_period=status_by_period,
        outfit_by_period=outfit_by_period,
        outfit_pool=outfit_pool,
        scene_by_period=scene_by_period,
        action_by_period=action_by_period,
        schedule_by_period=schedule_by_period,
        day_theme=str(route.get("theme") or "一条有自然转场的连续日程。"),
        mood=mood,
        seed=seed,
        updated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        source="fallback",
        llm_attempted=llm_attempted,
        timeline=timeline,
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
        self.daily_schedule_store = DailyScheduleStore(
            os.path.join(data_dir, "daily_schedule.json")
        )
        self.business_day_start = DEFAULT_BUSINESS_DAY_START
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
        self._migrate_legacy_daily_schedule(self.data.get("daily_selfie_profile"))

    def _migrate_legacy_daily_schedule(self, raw: Any) -> Optional[DailySchedule]:
        """Import the old period snapshot once; the schedule store is canonical."""
        if self.daily_schedule_store.current is not None:
            return self.daily_schedule_store.current
        if not isinstance(raw, dict):
            return None
        date = str(raw.get("date") or "").strip()
        if not date:
            return None
        schedule = legacy_profile_to_schedule(
            raw,
            date=date,
            period_keys=DAILY_PERIOD_KEYS,
            period_ranges=PERIOD_TIME_RANGES,
        )
        if schedule is None:
            return None
        self.daily_schedule_store.set_current(schedule, remember_previous=False)
        return schedule

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
                images.append({
                    "id": entry["id"],
                    "data": data,
                    "mime_type": entry["mime_type"],
                    "path": entry["path"],
                    "created_at": entry["created_at"],
                })
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
    ) -> DailySelfieProfile:
        # Batch shots may initialize the profile concurrently; serialize the
        # read/generate/save sequence so every shot shares one daily snapshot.
        async with self._daily_profile_lock:
            return await self._ensure_daily_selfie_profile_unlocked(action)

    def _profile_from_timeline(
        self,
        schedule: DailySchedule,
        *,
        legacy: Optional[dict[str, Any]] = None,
    ) -> DailySelfieProfile:
        statuses, outfits, scenes, actions, nodes = project_schedule_to_period_maps(
            schedule,
            DAILY_PERIOD_KEYS,
            PERIOD_TIME_RANGES,
        )
        event = schedule.current_event()
        outfit_pool = _complete_outfit_pool(
            outfits,
            list(schedule.outfits.values()) + list((legacy or {}).get("outfit_pool") or []),
        )
        return DailySelfieProfile(
            date=schedule.date,
            outfit=schedule.outfit_for_event(event),
            status=(event.status if event else "") or str((legacy or {}).get("status") or ""),
            mood=schedule.mood or str((legacy or {}).get("mood") or ""),
            seed=schedule.seed,
            updated_at=schedule.updated_at,
            source="fallback",
            status_by_period=statuses,
            outfit_by_period=outfits,
            outfit_pool=outfit_pool,
            scene_by_period=scenes,
            action_by_period=actions,
            schedule_by_period=nodes,
            day_theme=schedule.theme,
            occasion=schedule.occasion,
            llm_attempted=False,
            timeline=schedule,
        )

    async def _ensure_daily_selfie_profile_unlocked(
        self,
        action: str = "",
    ) -> DailySelfieProfile:
        today = daily_schedule_date_key(self.business_day_start)
        existed = self.data.get("daily_selfie_profile")
        canonical = self.get_daily_schedule(today)
        if canonical is not None:
            profile = self._profile_from_timeline(
                canonical,
                legacy=existed if isinstance(existed, dict) else None,
            )
            # Keep the old in-memory/API shape for callers, while all future
            # prompt selection reads the canonical timeline store.
            if not isinstance(existed, dict) or existed.get("schema_version") != DAILY_PROFILE_SCHEMA_VERSION:
                self._persist_daily_selfie_profile(profile)
            return profile
        if isinstance(existed, dict) and existed.get("date") == today:
            status_by_period = existed.get("status_by_period") if isinstance(existed.get("status_by_period"), dict) else {}
            outfit_by_period = existed.get("outfit_by_period") if isinstance(existed.get("outfit_by_period"), dict) else {}
            scene_by_period = existed.get("scene_by_period") if isinstance(existed.get("scene_by_period"), dict) else {}
            action_by_period = existed.get("action_by_period") if isinstance(existed.get("action_by_period"), dict) else {}
            clean_status = {str(key): str(value).strip() for key, value in status_by_period.items()}
            clean_outfits = {str(key): str(value).strip() for key, value in outfit_by_period.items()}
            clean_scenes = {str(key): str(value).strip() for key, value in scene_by_period.items()}
            clean_actions = {str(key): str(value).strip() for key, value in action_by_period.items()}
            outfit_pool = _complete_outfit_pool(clean_outfits, existed.get("outfit_pool"))
            raw_schedule = _normalize_schedule_by_period(existed.get("schedule_by_period"))
            raw_schedule_complete = bool(raw_schedule) and all(
                raw_schedule.get(key, {}).get("status")
                and raw_schedule.get(key, {}).get("scene")
                and raw_schedule.get(key, {}).get("action")
                and raw_schedule.get(key, {}).get("outfit_ref")
                for key in DAILY_PERIOD_KEYS
            )
            schedule = raw_schedule
            if schedule:
                schedule = _complete_schedule_from_maps(
                    schedule,
                    status_by_period=clean_status,
                    outfit_by_period=clean_outfits,
                    scene_by_period=clean_scenes,
                    action_by_period=clean_actions,
                    outfit_pool=outfit_pool,
                )
                clean_outfits, clean_status, clean_scenes, clean_actions = _derive_period_maps_from_schedule(
                    schedule,
                    outfit_pool,
                    legacy_outfits=clean_outfits,
                    legacy_status=clean_status,
                    legacy_scenes=clean_scenes,
                    legacy_actions=clean_actions,
                )
            elif all(clean_outfits.get(key) for key in DAILY_PERIOD_KEYS):
                schedule = _schedule_from_legacy_maps(
                    status_by_period=clean_status,
                    outfit_by_period=clean_outfits,
                    scene_by_period=clean_scenes,
                    action_by_period=clean_actions,
                    outfit_pool=outfit_pool,
                )

            source = str(existed.get("source") or "fallback").strip().lower()
            complete = (
                source == "fallback"
                and raw_schedule_complete
                and all(clean_status.get(key) for key in DAILY_PERIOD_KEYS)
                and all(clean_outfits.get(key) for key in DAILY_PERIOD_KEYS)
                and all(clean_scenes.get(key) and clean_actions.get(key) for key in DAILY_PERIOD_KEYS)
                and all(
                    schedule.get(key, {}).get("scene")
                    and schedule.get(key, {}).get("action")
                    and schedule.get(key, {}).get("outfit_ref")
                    for key in DAILY_PERIOD_KEYS
                )
            )
            if complete:
                profile = DailySelfieProfile(
                    date=today,
                    outfit=clean_outfits.get(current_period()) or str(existed.get("outfit") or ""),
                    status=clean_status.get(current_period()) or str(existed.get("status") or ""),
                    mood=str(existed.get("mood") or ""),
                    seed=str(existed.get("seed") or ""),
                    updated_at=str(existed.get("updated_at") or ""),
                    source="fallback",
                    status_by_period=clean_status,
                    outfit_by_period=clean_outfits,
                    outfit_pool=outfit_pool,
                    scene_by_period=clean_scenes,
                    action_by_period=clean_actions,
                    schedule_by_period=schedule,
                    day_theme=str(existed.get("day_theme") or ""),
                    occasion=str(existed.get("occasion") or ""),
                    llm_attempted=False,
                )
                # Normalize legacy fields once; ordinary shots reuse the
                # in-memory snapshot without rewriting the profile file.
                if (
                    existed.get("schema_version") != DAILY_PROFILE_SCHEMA_VERSION
                    or existed.get("source") != "fallback"
                    or existed.get("llm_attempted") is not False
                    or existed.get("outfit") != profile.outfit
                    or existed.get("status") != profile.status
                    or existed.get("status_by_period") != clean_status
                    or existed.get("outfit_by_period") != clean_outfits
                    or existed.get("outfit_pool") != outfit_pool
                    or existed.get("scene_by_period") != clean_scenes
                    or existed.get("action_by_period") != clean_actions
                    or existed.get("schedule_by_period") != schedule
                ):
                    self._persist_daily_selfie_profile(profile)
                return profile

            avoid_outfits = set(clean_outfits.values())
            avoid_statuses = set(clean_status.values())
            avoid_scenes = set(clean_scenes.values())
            avoid_actions = set(clean_actions.values())
        else:
            avoid_outfits = set()
            avoid_statuses = set()
            avoid_scenes = set()
            avoid_actions = set()

        profile = fallback_daily_profile(
            today,
            make_random_seed(),
            llm_attempted=False,
            avoid_outfits=avoid_outfits,
            avoid_statuses=avoid_statuses,
            avoid_scenes=avoid_scenes,
            avoid_actions=avoid_actions,
            avoid_route_ids=self.daily_schedule_store.recent_route_ids(),
        )
        self._persist_daily_selfie_profile(profile)
        return profile

    def _persist_daily_selfie_profile(self, profile: DailySelfieProfile) -> None:
        timeline = profile.timeline
        if timeline is None:
            timeline = legacy_profile_to_schedule(
                {
                    "date": profile.date,
                    "outfit": profile.outfit,
                    "outfit_by_period": profile.outfit_by_period,
                    "outfit_pool": profile.outfit_pool,
                    "scene_by_period": profile.scene_by_period,
                    "action_by_period": profile.action_by_period,
                    "status_by_period": profile.status_by_period,
                    "day_theme": profile.day_theme,
                    "occasion": profile.occasion,
                    "mood": profile.mood,
                    "seed": profile.seed,
                    "updated_at": profile.updated_at,
                },
                date=profile.date,
                period_keys=DAILY_PERIOD_KEYS,
                period_ranges=PERIOD_TIME_RANGES,
            )
        if timeline is not None:
            timeline.mood = profile.mood
            timeline.theme = profile.day_theme or timeline.theme
            timeline.occasion = profile.occasion
            timeline.updated_at = profile.updated_at or timeline.updated_at
            self.daily_schedule_store.set_current(timeline)
        self.data["daily_selfie_profile"] = {
            "schema_version": DAILY_PROFILE_SCHEMA_VERSION,
            "date": profile.date,
            "outfit": profile.outfit,
            "outfit_by_period": profile.outfit_by_period,
            "outfit_pool": profile.outfit_pool,
            "scene_by_period": profile.scene_by_period,
            "action_by_period": profile.action_by_period,
            "schedule_by_period": profile.schedule_by_period,
            "day_theme": profile.day_theme,
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

    async def refresh_daily_selfie_profile_local(self) -> DailySelfieProfile:
        """Create a fresh local-random daily profile without calling an LLM."""
        async with self._daily_profile_lock:
            previous = self.get_daily_selfie_profile()
            avoid_outfits = set(previous.outfit_by_period.values()) if previous else set()
            avoid_statuses = set(previous.status_by_period.values()) if previous else set()
            avoid_scenes = set(previous.scene_by_period.values()) if previous else set()
            avoid_actions = set(previous.action_by_period.values()) if previous else set()
            profile = fallback_daily_profile(
                daily_schedule_date_key(self.business_day_start),
                make_random_seed(),
                llm_attempted=False,
                avoid_outfits=avoid_outfits,
                avoid_statuses=avoid_statuses,
                avoid_scenes=avoid_scenes,
                avoid_actions=avoid_actions,
                avoid_route_ids=self.daily_schedule_store.recent_route_ids(),
            )
            self._persist_daily_selfie_profile(profile)
            return profile

    def get_daily_schedule(self, date: Optional[str] = None) -> Optional[DailySchedule]:
        """Return the canonical timeline, migrating an old snapshot if needed."""
        schedule = self.daily_schedule_store.current
        if schedule is None:
            schedule = self._migrate_legacy_daily_schedule(self.data.get("daily_selfie_profile"))
        if schedule is None:
            return None
        if date is not None and schedule.date != str(date):
            return None
        return schedule

    def get_current_daily_event(self) -> Optional[Any]:
        schedule = self.get_daily_schedule(daily_schedule_date_key(self.business_day_start))
        return schedule.current_event() if schedule else None

    def daily_schedule_payload(self) -> Dict[str, Any]:
        schedule = self.get_daily_schedule(daily_schedule_date_key(self.business_day_start))
        if schedule is None:
            return {"schedule": None, "current_event": None, "next_event": None}
        current = schedule.current_event()
        current_index = schedule.events.index(current) if current in schedule.events else -1
        next_event = schedule.events[current_index + 1] if current_index >= 0 and current_index + 1 < len(schedule.events) else None
        return {
            "schedule": schedule.to_dict(),
            "current_event": current.to_dict() if current else None,
            "next_event": next_event.to_dict() if next_event else None,
        }

    def daily_schedule_text(self) -> str:
        schedule = self.get_daily_schedule(daily_schedule_date_key(self.business_day_start))
        if schedule is None:
            return "今天还没有可用的本地日程。"
        current = schedule.current_event()
        lines = [
            f"日期：{schedule.date}",
            f"主题：{schedule.theme}" if schedule.theme else "",
            f"心情：{schedule.mood}" if schedule.mood else "",
            "当前事件：" + (
                f"{current.start}-{current.end} {current.title}｜{current.scene}｜{current.action}"
                if current else "暂无"
            ),
            "日程安排：",
        ]
        for event in schedule.events:
            outfit = schedule.outfit_for_event(event)
            suffix = f"｜穿搭变化：{event.outfit_change}" if event.outfit_change else ""
            lines.append(
                f"{event.start}-{event.end} {event.title}：{event.scene}；{event.action}；穿搭：{outfit}{suffix}"
            )
        return "\n".join(line for line in lines if line)

    def get_daily_selfie_profile(self) -> Optional[DailySelfieProfile]:
        raw = self.data.get("daily_selfie_profile")
        if not isinstance(raw, dict):
            return None
        status_by_period = raw.get("status_by_period") if isinstance(raw.get("status_by_period"), dict) else {}
        outfit_by_period = raw.get("outfit_by_period") if isinstance(raw.get("outfit_by_period"), dict) else {}
        outfit_pool = _complete_outfit_pool(
            {str(k): str(v) for k, v in outfit_by_period.items()},
            raw.get("outfit_pool"),
        )
        scene_by_period = raw.get("scene_by_period") if isinstance(raw.get("scene_by_period"), dict) else {}
        action_by_period = raw.get("action_by_period") if isinstance(raw.get("action_by_period"), dict) else {}
        clean_outfits = {str(k): str(v) for k, v in outfit_by_period.items()}
        clean_scenes = {str(k): str(v) for k, v in scene_by_period.items()}
        clean_actions = {str(k): str(v) for k, v in action_by_period.items()}
        schedule_by_period = _normalize_schedule_by_period(raw.get("schedule_by_period"))
        if schedule_by_period:
            schedule_by_period = _complete_schedule_from_maps(
                schedule_by_period,
                status_by_period={str(k): str(v) for k, v in status_by_period.items()},
                outfit_by_period=clean_outfits,
                scene_by_period=clean_scenes,
                action_by_period=clean_actions,
                outfit_pool=outfit_pool,
            )
            (
                clean_outfits,
                clean_status,
                clean_scenes,
                clean_actions,
            ) = _derive_period_maps_from_schedule(
                schedule_by_period,
                outfit_pool,
                legacy_outfits=clean_outfits,
                legacy_status={str(k): str(v) for k, v in status_by_period.items()},
                legacy_scenes=clean_scenes,
                legacy_actions=clean_actions,
            )
            status_by_period = clean_status
        elif all(clean_outfits.get(key) for key in DAILY_PERIOD_KEYS):
            schedule_by_period = _schedule_from_legacy_maps(
                status_by_period={str(k): str(v) for k, v in status_by_period.items()},
                outfit_by_period=clean_outfits,
                scene_by_period=clean_scenes,
                action_by_period=clean_actions,
                outfit_pool=outfit_pool,
            )
        timeline = self.get_daily_schedule(str(raw.get("date") or ""))
        return DailySelfieProfile(
            date=str(raw.get("date") or ""),
            outfit=str(clean_outfits.get(current_period()) or raw.get("outfit") or ""),
            status=str(status_by_period.get(current_period()) or raw.get("status") or ""),
            mood=str(raw.get("mood") or ""),
            seed=str(raw.get("seed") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            # Daily schedules are local-only; legacy LLM snapshots are read
            # only as data to be replaced by the ensure path.
            source="fallback",
            status_by_period={str(k): str(v) for k, v in status_by_period.items()},
            outfit_by_period=clean_outfits,
            outfit_pool=outfit_pool,
            scene_by_period=clean_scenes,
            action_by_period=clean_actions,
            schedule_by_period=schedule_by_period,
            day_theme=str(raw.get("day_theme") or ""),
            occasion=str(raw.get("occasion") or ""),
            llm_attempted=False,
            timeline=timeline,
        )

    def build_selfie_prompt(
        self,
        action: str,
        bot_name: str,
        personality: str,
        has_reference_image: bool,
        extra_reference_count: int = 0,
        has_external_reference: Optional[bool] = None,
    ) -> str:
        act = str(action or "").strip()
        intent = self.analyze_selfie_intent(act)
        if intent.is_legs_only and not has_leg_focus_contract(act):
            act = ensure_leg_focus_action(act, has_reference_image)
            intent = self.analyze_selfie_intent(act)
        # Mode-specific action builders already select one coherent shot,
        # scene, gesture, and light. Do not layer an unrelated legacy snapshot
        # preset on top of that contract.
        has_staging_contract = (
            "本次机位：" in act
            and any(
                marker in act
                for marker in (
                    "场景倾向：",
                    "小动作：",
                    "动作瞬间：",
                    "今日日程场景参考：",
                )
            )
        )
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
            if (
                not explicit_context
                and not has_staging_contract
                and not back_facing_shot
                and not side_facing_shot
            )
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
        # Schedule context belongs to identity-free daily shots. Leg-focus
        # can borrow the event's location, but keeps its own pose and outfit.
        external_reference = (
            bool(has_external_reference)
            if has_external_reference is not None
            else int(extra_reference_count or 0) > 0
        )
        has_daily_context = (
            not intent.is_cos_look
            and not intent.is_group_photo
            and not external_reference
        )
        use_daily_now = has_daily_context and not intent.is_legs_only
        today_lines: list[str] = []
        if use_daily_now and daily and not skip_period:
            period = current_period()
            label = period_label(period)
            today_lines.append(f"当前时间段：{label}")
            timeline = self.get_daily_schedule(daily_schedule_date_key(self.business_day_start))
            timeline_event = timeline.current_event() if timeline else None
            current_event = (
                {
                    "title": timeline_event.title,
                    "start": timeline_event.start,
                    "end": timeline_event.end,
                    "status": timeline_event.status,
                    "scene": timeline_event.scene,
                    "action": timeline_event.action,
                    "outfit_change": timeline_event.outfit_change,
                }
                if timeline_event
                else (daily.schedule_by_period.get(period) if daily.schedule_by_period else {})
            )
            if daily.day_theme:
                today_lines.append(f"今日日程基调：{daily.day_theme}")
            if current_event and intent.is_legs_only and not skip_status:
                if current_event.get("scene"):
                    today_lines.append(f"当前日程场景：{current_event['scene']}")
            elif current_event and not skip_status and not intent.change_pose:
                today_lines.append(
                    "当前日程事件："
                    + "；".join(
                        item
                        for item in (
                            f"{current_event.get('start')}-{current_event.get('end')} {current_event.get('title')}"
                            if current_event.get("title")
                            else "",
                            f"状态={current_event.get('status')}" if current_event.get("status") else "",
                            f"场景={current_event.get('scene')}" if current_event.get("scene") else "",
                            f"动作={current_event.get('action')}" if current_event.get("action") else "",
                            f"穿搭变化={current_event.get('outfit_change')}" if current_event.get("outfit_change") else "",
                        )
                        if item
                    )
                )
            selected_outfit = (
                timeline.outfit_for_event(timeline_event)
                if timeline and timeline_event
                else str(daily.outfit_by_period.get(period) or daily.outfit or "").strip()
            )
            if intent.use_today_outfit and not intent.is_legs_only and selected_outfit and not skip_outfit:
                outfit_label = f"今日穿搭（{label}）" if daily.outfit_by_period else "今日穿搭"
                today_lines.append(f"{outfit_label}：{selected_outfit}")
            if daily.occasion:
                today_lines.append(f"今日时令 / 节日氛围：{daily.occasion}")
            if not skip_status:
                status = ""
                if timeline_event and timeline_event.status:
                    status = timeline_event.status
                elif daily.status_by_period:
                    status = str(daily.status_by_period.get(period) or "")
                status = status or str(daily.status or "")
                if status:
                    today_lines.append(f"当前状态：{status}")
                scene = (
                    timeline_event.scene.strip()
                    if timeline_event
                    else str(daily.scene_by_period.get(period) or "").strip()
                )
                daily_action = (
                    timeline_event.action.strip()
                    if timeline_event
                    else str(daily.action_by_period.get(period) or "").strip()
                )
                if scene and not has_staging_contract:
                    today_lines.append(f"当前日常场景参考：{scene}")
                if daily_action and not intent.is_legs_only and not has_staging_contract:
                    today_lines.append(f"当前日常动作参考：{daily_action}")
        if has_daily_context and intent.is_legs_only and daily and not skip_status:
            period = current_period()
            timeline = self.get_daily_schedule(daily_schedule_date_key(self.business_day_start))
            timeline_event = timeline.current_event() if timeline else None
            scene = (
                timeline_event.scene.strip()
                if timeline_event
                else str(daily.scene_by_period.get(period) or "").strip()
            )
            if scene and "今日日程场景参考：" not in act:
                today_lines.append(f"当前日程场景：{scene}")
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
            period = current_period()
            lines.extend(
                [
                    f"今日自拍设定：{daily.date}",
                    f"来源：{'本地随机兜底' if daily.source == 'fallback' else daily.source}",
                    f"当前状态({period_label(current_period())})：{daily.status}",
                    f"当前心情：{daily.mood}",
                    f"可选穿搭池：{len(daily.outfit_pool)}套",
                ]
            )
            if daily.day_theme:
                lines.append(f"今日日程基调：{daily.day_theme}")
            timeline = self.get_daily_schedule(daily_schedule_date_key(self.business_day_start))
            timeline_event = timeline.current_event() if timeline else None
            if timeline_event:
                lines.append(
                    f"当前日程事件：{timeline_event.start}-{timeline_event.end} {timeline_event.title}；"
                    f"场景={timeline_event.scene}；动作={timeline_event.action}"
                )
            elif daily.scene_by_period.get(period):
                lines.append(f"当前场景({period_label(period)})：{daily.scene_by_period[period]}")
            if timeline and timeline_event:
                lines.append(f"当前事件穿搭：{timeline.outfit_for_event(timeline_event)}")
            if daily.occasion:
                lines.append(f"今日时令 / 节日氛围：{daily.occasion}")
            if timeline:
                lines.append("今日日程：")
                for event in timeline.events:
                    outfit = timeline.outfit_for_event(event)
                    change = f"；变化={event.outfit_change}" if event.outfit_change else ""
                    lines.append(
                        f"{event.start}-{event.end} {event.title}：{event.scene}；"
                        f"{event.action}；穿搭={outfit}{change}"
                    )
            elif daily.outfit_by_period:
                lines.extend(
                    f"{period_label(period)}穿搭：{daily.outfit_by_period[period]}"
                    for period in DAILY_PERIOD_KEYS
                    if daily.outfit_by_period.get(period)
                )
            else:
                lines.append(f"今日穿搭：{daily.outfit}")
        return "\n".join(lines)
