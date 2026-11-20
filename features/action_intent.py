"""Lightweight action and camera constraints for generated photo prompts."""

from __future__ import annotations

import re
from dataclasses import dataclass


_FULL_BODY_TERMS = (
    r"骑(?:自行车|单车|山地车|公路车|摩托车|电动车|车|行|马)|"
    r"游泳|游水|跑步|慢跑|快走|健走|慢走|散步|遛狗|行走|走路|奔跑|小跑|滑雪|滑冰|滑旱冰|滑板|冲浪|攀岩|徒步|爬山|登山|"
    r"跳舞|练舞(?:蹈)?|舞蹈(?:练习|训练|动作|排练)|跳绳|打(?:球|网球|羽毛球|篮球|排球|乒乓球)|踢足球|"
    r"练瑜伽|做瑜伽|瑜伽(?:动作|训练|练习)?|普拉提|有氧操|健身操|跳操|压腿|劈叉|(?:做|进行|练习)?(?:拉伸|伸展|拉筋|热身)|"
    r"健身|锻炼|举铁|练核心|深蹲训练|力量训练|开车|驾驶|上下楼|爬楼"
)
_TASK_TERMS = (
    r"做饭|做菜|做早餐|做晚餐|做晚饭|做甜点|煮饭|煮菜|煮面|烹饪|备菜|备餐|准备(?:饭菜|早餐|晚餐|晚饭)|"
    r"切菜|切水果|洗菜|炒菜|摆盘|冲咖啡|冲泡咖啡|泡茶|洗碗|清洗餐具|烘焙|揉面|擀面|"
    r"弹(?:钢琴|电子琴|吉他|尤克里里|琴)|弹奏|演奏|拉小提琴|打鼓|吹(?:笛子|萨克斯)|"
    r"画画|绘画|素描|写字|练字|书法|做手工|做模型|陶艺|捏陶|木工|缝纫|编织|刺绣|修理|维修|装配|打字|敲键盘|"
    r"打扫卫生|打扫|扫地|拖地|擦桌|洗衣服|洗衣|晾衣|工作|办公|开会|处理文件|写代码|敲代码|编程|操作电脑"
)
_CLOTHING_SUFFIX = r"(?!服|衣|裤|装|鞋|图案|面料)"
_LOCATION_SUFFIX = r"(?!池|馆|场|道|室|房|中心|区|教室|工作室)"

_ACTIVITY_RE = re.compile(
    rf"(?:{_FULL_BODY_TERMS}|{_TASK_TERMS}){_CLOTHING_SUFFIX}{_LOCATION_SUFFIX}", re.I
)
_FULL_BODY_RE = re.compile(
    rf"(?:{_FULL_BODY_TERMS}){_CLOTHING_SUFFIX}{_LOCATION_SUFFIX}", re.I
)
_TASK_RE = re.compile(rf"(?:{_TASK_TERMS}){_CLOTHING_SUFFIX}{_LOCATION_SUFFIX}", re.I)
_SUPPORT_RE = re.compile(
    r"平板支撑|俯卧撑|撑地|用手撑住|手掌撑住|撑住身体|"
    r"(?:双手|两手|两只手).{0,10}(?:扶|撑|按|支撑|放在)|"
    r"(?:扶|撑|按).{0,5}(?:桌|墙|地面|地板|床沿|台面|栏杆|器材)|"
    r"(?:扶住|撑住|按住).{0,5}(?:桌|墙|地面|地板|床沿|台面|栏杆|器材)", re.I
)
_BOTH_HANDS_RE = re.compile(
    r"(?:双手|两手|两只手).{0,12}(?:拿|握|端|扶|撑|按|持|托|捧|举|操作|比心)|"
    r"(?:拿|握|端|扶|撑|按|持|托|捧|举|操作).{0,12}(?:双手|两手|两只手)", re.I
)
_ONE_HAND_RE = re.compile(
    r"单手|一只手|一手|拿着|握着|端着|拎着|打伞|拿书|端杯|整理发丝|挥手|手持手机|写字|练字|书法|签字|签名|写日记", re.I
)
_POSE_FRAME_RE = re.compile(
    r"坐地|坐在地(?:上|面)|席地|盘腿|盘坐|打坐|坐着|坐下|坐在|坐姿|端坐|"
    r"跪坐|跪姿|跪着|跪下|跪在|趴卧|趴着|趴在|俯卧|仰卧|平躺|侧躺|侧卧|躺着|躺下|躺在|"
    r"深蹲|蹲下|蹲着|蹲在", re.I
)
_SCENE_TERMS = (
    "舞蹈室", "舞蹈教室", "舞蹈工作室", "排练室", "健身房", "健身工作室", "瑜伽馆", "瑜伽教室",
    "游泳馆", "游泳池", "泳池", "球馆", "球场", "篮球场", "羽毛球馆", "网球场", "跑道", "操场",
    "骑行道", "登山道", "徒步道", "画室", "工作坊", "工坊", "厨房", "洗手池", "窗边", "窗前",
    "窗户旁", "书桌", "桌前", "阳台", "镜前", "镜子前", "落地镜", "沙发", "床边", "床沿", "椅子",
    "座位", "栏杆", "护栏", "门框", "墙边", "窗台", "窗框", "立柱", "咖啡馆", "咖啡店", "咖啡厅",
    "街边", "街上", "树荫", "书店", "图书馆", "夜灯房间", "卧室", "客厅", "室内", "室外", "户外", "街道", "雨夜",
    "霓虹", "海边", "沙滩", "公园", "地铁", "影棚", "摄影棚", "校园", "教室", "走廊", "车站", "雪地", "樱花", "河边", "天台", "屋顶",
    "楼梯", "浴室", "温泉", "舞台", "草地", "花丛", "竹林", "和室", "地毯", "场景不变", "背景不变",
    "保持场景", "保持背景",
)
_SCENE_RE = re.compile(
    "|".join(re.escape(term) for term in sorted(_SCENE_TERMS, key=len, reverse=True)), re.I
)
_MIRROR_CAMERA_RE = re.compile(
    r"(?:对镜|对着镜子|镜前|镜子前|镜中|镜子里|落地镜(?:前|中)?).{0,12}(?:自拍|拍照|拍摄|拍一张)|"
    r"(?:自拍|拍照|拍摄|拍一张).{0,12}(?:对镜|对着镜子|镜前|镜子前|镜中|镜子里|落地镜)", re.I
)
HANDHELD_CAMERA_RE = re.compile(
    r"(?:手持|手拿|拿着|拿|举着|举|握着|使用|用|通过)(?:自己的|一部|一台)?(?:手机|相机|自拍杆)"
    r"(?:的)?(?:前置摄像头|前置镜头|前摄)?(?:自拍|拍照|拍摄|拍一张(?:照片|照)?|拍张(?:照片|照))|"
    r"手机(?:的)?(?:前置摄像头|前置镜头|前摄|前置)?(?:自拍|拍照|拍摄)|"
    r"(?:使用|用|通过)?(?:前置摄像头|前置镜头|前摄)(?:自拍|拍照|拍摄)|"
    r"手持(?:手机|相机)?(?:自拍|拍照|拍摄)|"
    r"自拍杆(?:自拍|拍照|拍摄|拍一张(?:照片|照)?|拍张(?:照片|照))?", re.I
)
_OFFSCREEN_CAMERA_RE = re.compile(
    r"定时自拍|定时拍摄|固定机位|画外机位|设备.{0,4}画外|手机.{0,6}(?:支架|固定)|"
    r"(?:手机|相机).{0,6}放在.{0,6}(?:支架|画外)|(?:支架|三脚架).{0,6}(?:自拍|拍照|拍摄)", re.I
)
_NEGATION_RE = re.compile(
    r"(?:不要|不需要|不想|不希望|不必|不允许|不能|不会|不可|别|避免|禁止|拒绝|没有|无需|不用|不是|并非|不|"
    r"don't|do not|doesn't|does not|isn't|is not|without|avoid|exclude|not|no)"
    r"[\w\u4e00-\u9fff]{0,28}$",
    re.I,
)
_CONTRAST_RE = re.compile(
    r"[，,。；;！？?!\n]|改成|改为|换成|改用|而是|而非|不过|但是|但|转为|instead|rather than", re.I
)
_SCENE_CLOTHING_SUFFIX_RE = re.compile(r"^(?:图案|印花|面料|风格|主题|款式|元素|服|衣|裤)")


@dataclass(frozen=True)
class ActionCameraIntent:
    is_activity: bool = False
    full_body_motion: bool = False
    hands_occupied: str = "unknown"
    needs_support: bool = False
    needs_pose_framing: bool = False
    explicit_scene: bool = False
    explicit_camera: str = ""

    @property
    def requires_offscreen_camera(self) -> bool:
        return self.full_body_motion or self.hands_occupied in {"both", "support", "task"} or self.is_task

    @property
    def is_task(self) -> bool:
        return self.is_activity and self.hands_occupied == "task"

    @property
    def needs_action_framing(self) -> bool:
        return self.full_body_motion or self.needs_support or self.needs_pose_framing or self.is_task


def is_positive_request_match(text: str, match: re.Match[str]) -> bool:
    prefix = text[max(0, match.start() - 40):match.start()]
    prefix = _CONTRAST_RE.split(prefix)[-1]
    return not _NEGATION_RE.search(prefix)


def _has_positive_match(pattern: re.Pattern[str], text: str) -> bool:
    return any(is_positive_request_match(text, match) for match in pattern.finditer(text))


def _has_positive_scene(text: str) -> bool:
    for match in _SCENE_RE.finditer(text):
        suffix = text[match.end():match.end() + 6]
        if is_positive_request_match(text, match) and not _SCENE_CLOTHING_SUFFIX_RE.search(suffix):
            return True
    return False


def classify_action_camera_intent(text: str, *, is_work: bool = False) -> ActionCameraIntent:
    """Classify only the body, hand, framing, and scene constraints relevant to capture."""
    value = re.sub(r"\s+", "", str(text or "")).lower()
    activity = _has_positive_match(_ACTIVITY_RE, value) or is_work
    full_body = _has_positive_match(_FULL_BODY_RE, value)
    task = _has_positive_match(_TASK_RE, value) or is_work
    support = _has_positive_match(_SUPPORT_RE, value)
    both_hands = _has_positive_match(_BOTH_HANDS_RE, value)
    one_hand = _has_positive_match(_ONE_HAND_RE, value)
    if support:
        hand_occupancy = "support"
    elif both_hands:
        hand_occupancy = "both"
    elif one_hand:
        hand_occupancy = "one"
    elif task:
        hand_occupancy = "task"
    else:
        hand_occupancy = "unknown"

    explicit_camera = ""
    if _has_positive_match(_OFFSCREEN_CAMERA_RE, value):
        explicit_camera = "offscreen"
    elif _has_positive_match(_MIRROR_CAMERA_RE, value):
        explicit_camera = "mirror"
    elif _has_positive_match(HANDHELD_CAMERA_RE, value):
        explicit_camera = "handheld"

    return ActionCameraIntent(
        is_activity=activity,
        full_body_motion=full_body,
        hands_occupied=hand_occupancy,
        needs_support=support,
        needs_pose_framing=_has_positive_match(_POSE_FRAME_RE, value),
        explicit_scene=_has_positive_scene(value),
        explicit_camera=explicit_camera,
    )
