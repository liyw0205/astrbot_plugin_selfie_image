"""AI selfie persona management."""

from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

from ..core.utils import detect_mime_by_bytes, ext_from_mime, load_json_file, save_json_file


APPEARANCE_TYPES = ("auto", "real", "anime")
APPEARANCE_TYPE_LABELS = {
    "auto": "自动",
    "real": "真人",
    "anime": "动漫",
}


def is_leg_calf_crop_action(text: str) -> bool:
    """Detect the compact lower-outfit framing used by the look-legs command."""
    raw = str(text or "")
    if "【legs:outfit】" in raw or "【crop:calves】" in raw or "双脚完整裁出画外" in raw or "不展示脚部" in raw:
        return True
    m = re.search(r"【pose:([a-z_]+)】", raw)
    if m and str(m.group(1)).endswith("_crop"):
        return True
    # Default look-legs path always hides feet now.
    return "看看腿" in raw or "下半身穿搭" in raw or "穿搭展示" in raw



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


def appearance_type_instruction(appearance_type: str, *, has_reference_image: bool = False) -> str:
    kind = normalize_appearance_type(appearance_type)
    gender_lock = (
        "性别、年龄感与同一人身份必须跟随参考图一：参考图是女性就保持女性，是男性就保持男性；"
        "禁止无故改成异性、少年男主或路人男。"
        if has_reference_image
        else "若角色设定未写明性别，默认成年女性；禁止无故改成男性。"
    )
    if kind == "real":
        return (
            "形象类型：真人。主角形象是真人，输出保持写实真人照片质感与真人五官体态。"
            + gender_lock
        )
    if kind == "anime":
        return (
            "形象类型：动漫。主角形象是动漫人物，输出保持柔光京阿尼风格与动漫五官体态；"
            "脸更细，气质更仙风；"
            "即使参考图偏写实，也只把同一人改画成动漫版，不要换成另一个角色。"
            + gender_lock
        )
    return gender_lock if has_reference_image else ""


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
        "合影画风：不额外指定真人/动漫，由模型根据主角形象与参考图自行判断，整张图保持统一画风。",
        "额外参考图一律作为同框角色来源，必须落实为独立完整人物，不要只当背景墙纸、贴图或怀里原样玩偶。",
        "人物类参考：保留身份线索（发色发型、配色、饰品、服装色块、体态倾向），并改画成与主角同一套画风的完整人物。",
        "玩偶、毛绒玩具、吉祥物、卡通立牌、表情包、道具等：只取配色/外形气质线索，拟人成可并肩站立的完整人物；禁止原样保留玩具外形、平面简笔画肢体或贴在主角衣服上。",
        "风景、建筑、房间、道具等非人物参考：按主色与气质拟人成可并肩站立的完整人物（成年、得体、日常），不要只当背景贴图。",
        "拟人无明确性别时默认成年女性；有明确性别线索则按对应性别。",
        "同框人物与主角身体/衣物分离，边界清晰，站位自然，互动友好；整张合影画风统一。",
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
    value = re.sub(r"\s*【(?:pose|shot|cos|cam|legs|wear):[a-z0-9_]+】\s*", " ", value)
    return re.sub(r"\s+", " ", value).strip(" 。")


def extra_overrides_outfit(extra: str) -> bool:
    compact = normalize_intent_text(extra)
    return includes_any(
        compact,
        [
            "穿这",
            "穿那",
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
            "霓虹",
            "夜色",
            "夜灯",
            "月光",
            "晨光",
            "金色小时",
            "夕阳",
            "日出",
            "日落",
            "咖啡馆",
            "街上",
            "街边",
            "室外",
            "户外",
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


def fallback_daily_profile(date: str, seed: str) -> DailySelfieProfile:
    outfits = [
        "奶油白细针织上衣和浅杏短开衫，下身轻薄格纹短裙，腿部穿搭简洁。",
        "浅粉宽松卫衣配奶白色短裙，发间夹一枚小珍珠发夹。",
        "雾紫色针织连衣裙，外披奶白毛绒小披肩，布料柔软。",
        "浅蓝灰宽松衬衫配白色高腰半身裙，袖口松松卷起。",
        "月白宽松毛衣配浅灰百褶裙，整体温暖、松弛。",
        "宽松薄针织或短款家居上衣配轻软短裙，重点是舒服自然。",
    ]
    status_by_period = {
        "morning": "刚整理好头发和衣服，窗边是偏白一点的晨光，整个人清爽、安静，还带点没完全醒透的松弛感。",
        "noon": "白天光线更亮，像在家里或窗边短暂歇着，衣服和状态都偏轻松，不是刻意摆拍。",
        "afternoon": "下午的光线开始变软，像刚在房间里磨蹭了一会儿，姿态自然，身上有一点慵懒的生活感。",
        "evening": "傍晚开了暖灯，房间慢慢安静下来，适合更柔和、松弛、有氛围感的随手拍。",
        "night": "夜里已经换到更舒服的状态，光线偏暖，动作自然收着，像准备窝着休息前拍一下。",
        "late_night": "深夜只留柔和小灯，整个人更安静、更懒散一点，像睡前低头顺手拍到的私密日常。",
    }
    period = current_period()
    return DailySelfieProfile(
        date=date,
        outfit=random_pick(outfits),
        status=status_by_period.get(period, "处于自然放松的日常状态，画面安静、统一、真实。"),
        status_by_period=status_by_period,
        mood=random_pick(["放松、安静、柔和", "清爽、自然、轻松", "温柔、治愈、稳定", "元气、明亮、轻快"]),
        seed=seed,
        updated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        source="fallback",
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
        extra = extract_user_extra_text(raw)
        compact = normalize_intent_text(raw)
        extra_compact = normalize_intent_text(extra)
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
        change_clothes = includes_any(
            extra_compact,
            [
                "穿这个",
                "穿这身",
                "穿这套",
                "穿这件",
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
                "礼服",
                "制服",
                "女仆装",
                "水手服",
                "丝袜",
                "黑丝",
                "白丝",
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
            includes_any(compact, ["看看cos", "看看cos模式", "cos换装自拍"])
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
            if "【cam:third】" in raw or "【他拍 / 看看cos模式】" in compact or "【他拍 / 看看COS模式】" in raw:
                is_third_person_photo = True
            elif "【cam:selfie】" in raw or "【自拍 / 看看cos模式】" in compact or "【自拍 / 看看COS模式】" in raw:
                is_third_person_photo = False
        elif is_legs_only:
            is_group_photo = False
            is_multi = False
            # 光腿/白丝/黑丝 are legwear, not outfit-change; 换装 line confuses gpt-image.
            change_clothes = False
            use_today = False
        else:
            use_today = (
                not compact
                or includes_any(compact, ["看看你", "看下你", "你长什么样", "你的样子", "今日穿搭", "今天穿搭", "今天这身"])
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
            is_cos_look=is_cos_look,
        )

    async def ensure_daily_selfie_profile(
        self,
        action: str = "",
        *,
        llm_generate: Optional[Callable[[str], Awaitable[str]]] = None,
    ) -> DailySelfieProfile:
        today = local_date_key()
        existed = self.data.get("daily_selfie_profile")
        if isinstance(existed, dict) and existed.get("date") == today and existed.get("outfit") and existed.get("status"):
            status_by_period = existed.get("status_by_period") if isinstance(existed.get("status_by_period"), dict) else {}
            profile = DailySelfieProfile(
                date=str(existed.get("date") or today),
                outfit=str(existed.get("outfit") or ""),
                status=str(status_by_period.get(current_period()) or existed.get("status") or ""),
                mood=str(existed.get("mood") or ""),
                seed=str(existed.get("seed") or ""),
                updated_at=str(existed.get("updated_at") or ""),
                source=str(existed.get("source") or "fallback"),
                status_by_period={str(k): str(v) for k, v in status_by_period.items()},
            )
            return profile

        profile: Optional[DailySelfieProfile] = None
        if llm_generate is not None:
            prompt = (
                "请为 AI 自拍角色生成今天的日常拍照设定。只返回一个 JSON 对象，不要 Markdown："
                '{"outfit":"...","mood":"...","status_by_period":{"morning":"...",'
                '"noon":"...","afternoon":"...","evening":"...","night":"...",'
                '"late_night":"..."}}。要求：角色是成年人物，穿搭得体日常，内容适合普通自拍；'
                "不要写敏感、暴露或未成年人内容；outfit 只写衣服本身，不要写清晨、午后、夜里等时间词；时间氛围只放进 status_by_period；每个字段简短自然。"
            )
            try:
                raw = str(await llm_generate(prompt) or "").strip()
                match = re.search(r"\{[\s\S]*\}", raw)
                data = json.loads(match.group(0)) if match else {}
                periods = data.get("status_by_period") if isinstance(data, dict) and isinstance(data.get("status_by_period"), dict) else {}
                outfit = str(data.get("outfit") or "").strip()
                mood = str(data.get("mood") or "自然、放松、轻松").strip()
                clean_periods = {
                    key: str(periods.get(key) or "").strip()
                    for key in ("morning", "noon", "afternoon", "evening", "night", "late_night")
                }
                forbidden = ("未成年", "裸", "裸体", "色情", "暴露", "内衣", "乳", "私密")
                all_text = " ".join([outfit, mood, *clean_periods.values()])
                time_locked = ("清晨", "早晨", "午后", "傍晚", "夜里", "夜晚", "深夜", "凌晨")
                if (
                    outfit
                    and len(outfit) <= 180
                    and len(mood) <= 80
                    and all(clean_periods.values())
                    and all(len(value) <= 160 for value in clean_periods.values())
                    and not any(word in all_text for word in forbidden)
                    and not any(word in outfit for word in time_locked)
                ):
                    if all(clean_periods.values()):
                        profile = DailySelfieProfile(
                            date=today,
                            outfit=str(data["outfit"]).strip(),
                            status=clean_periods.get(current_period(), clean_periods["morning"]),
                            status_by_period=clean_periods,
                            mood=str(data.get("mood") or "自然、放松、轻松").strip(),
                            seed=make_random_seed(),
                            updated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
                            source="llm",
                        )
            except Exception:
                profile = None
        if profile is None:
            profile = fallback_daily_profile(today, make_random_seed())
        self.data["daily_selfie_profile"] = {
            "date": profile.date,
            "outfit": profile.outfit,
            "status": profile.status,
            "status_by_period": profile.status_by_period,
            "mood": profile.mood,
            "seed": profile.seed,
            "updated_at": profile.updated_at,
            "source": profile.source,
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
        return DailySelfieProfile(
            date=str(raw.get("date") or ""),
            outfit=str(raw.get("outfit") or ""),
            status=str(status_by_period.get(current_period()) or raw.get("status") or ""),
            mood=str(raw.get("mood") or ""),
            seed=str(raw.get("seed") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            source=str(raw.get("source") or "fallback"),
            status_by_period={str(k): str(v) for k, v in status_by_period.items()},
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
        feet_cropped = is_leg_calf_crop_action(act)
        daily = self.get_daily_selfie_profile()
        appearance_type = self.get_appearance_type()

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
        if intent.is_legs_only and feet_cropped:
            identity_lines = (
                [
                    "参考图一只用于自然体态和构图；腿部穿搭完全以本次锁定选项为准，忽略参考图中的袜子、下装和鞋袜搭配；完整人物在画外。",
                ]
                if has_reference_image
                else [
                    "本张按日常服装局部记录生成，不展示完整人物。",
                ]
            )
        appearance_line = appearance_type_instruction(appearance_type, has_reference_image=has_reference_image)
        if appearance_line and not (intent.is_legs_only and feet_cropped):
            identity_lines.insert(1 if has_reference_image else 0, appearance_line)

        reference_lines: list[str] = []
        if extra_reference_count > 0:
            reference_lines.append(f"另有 {extra_reference_count} 张额外参考图。")
            if intent.is_group_photo:
                reference_lines.extend(group_style_lines(appearance_type))
            elif intent.is_legs_only:
                reference_lines.extend(
                    [
                        "额外参考图只参考坐靠姿势、构图、室内环境和光线；腿部穿搭完全以本次锁定选项为准，不参考或复制参考图中的袜子、下装或鞋袜搭配。",
                        "所有参考图都服从近距离服装取景，保持单人、得体、日常的记录效果。",
                    ]
                )
            else:
                reference_lines.extend(
                    [
                        "额外参考图仅用于服装、姿势、构图、风格、场景、道具、镜头角度或光线；不替换参考图一的主角身份。",
                        "换装 / COS 时只迁移服装、配饰、颜色、材质、印花和造型氛围，不迁移额外参考图人物的脸型、五官、发型发色或体态。",
                        "性别跟随参考图一；除非用户要求遮脸，否则主角面部清晰自然，表情眼神按本次场景重画。",
                    ]
                    if has_reference_image
                    else [
                        "额外参考图用于构图、衣服、姿势、场景或光线；主角仍符合角色名称和人设。",
                        "除非用户要求遮脸，否则主角面部清晰自然。",
                    ]
                )

        mode_lines: list[str] = []
        if intent.is_cos_look:
            camera_is_third = bool(intent.is_third_person_photo)
            mode_lines.extend(
                [
                    "【COS换装他拍模式】" if camera_is_third else "【COS换装自拍模式】",
                    "这是 COS 换装"
                    + ("他拍" if camera_is_third else "自拍")
                    + "，不是晒腿、不是合影。",
                    (
                        "别人视角的单人成品照：竖屏近景半身，画面里只有主角一个人；拍摄者完全在画面外，不要第二个人，不要拍到拍摄设备或拍摄过程，不要对镜；主角双手自然做动作，不要用物件遮脸挡衣服。"
                        if camera_is_third
                        else "竖屏近景半身自拍成片：可对镜取景，但拍摄设备完全在画面外；拍胸像到腰线，不要展会式全身棚拍；主角双手自然入镜或放在身侧，不要用物件遮脸挡衣服。"
                    ),
                    "保持形象参考的脸型五官与体态；假发颜色、发型、发饰按本套 COS 完整替换。",
                    "完整展示套装层次和腰线；竖屏近景半身即可，不要裁成只拍腿或只拍脸。",
                    "构图以展示 COS 服装为主；表情按新造型自然重画。",
                ]
            )
        elif intent.is_group_photo:
            mode_lines.append("【合影 / 同框模式】")
            if appearance_type == "anime":
                mode_lines.append("先确定你自己的柔光京阿尼动漫形象，再把每张额外参考图落实为独立的同框动漫人物。")
            elif appearance_type == "real":
                mode_lines.append("先确定你自己的写实形象，再把每张额外参考图落实为独立的同框写实人物。")
            else:
                mode_lines.append("先确定你自己的形象，再把每张额外参考图落实为独立的同框人物；画风由模型判断并整图统一。")
            # With extra references, the same style/conversion guidance is
            # already emitted in reference_lines above.
            if not extra_reference_count:
                mode_lines.extend(group_style_lines(appearance_type))
            mode_lines.extend(
                [
                    "同框对象必须是独立完整人物（有头有身体），禁止原样保留玩偶/毛绒玩具/吉祥物外形，禁止贴在主角衣服上或与身体衣物粘连融合。",
                    "同框人物自然站位或坐位，距离、遮挡、视线和互动关系合理；与主角边界清晰、可并肩，不要融成一团。",
                    "合影默认多数人看向镜头（或看向画面中的相机方向），表情自然、有互动；你自己的表情按合影氛围重画，不要僵住参考图一的原表情。",
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
                r"【姿势池·[^】]+】(.*?)(?=本次服装搭配已锁定为：|用户提供的图片只参考|【cam:|$)",
                act,
                re.S,
            )
            pool_pose_text = re.sub(
                r"\s+", " ", str(pool_pose_match.group(1) if pool_pose_match else "")
            ).strip(" 。；")
            camera_line = (
                "第一人称手机自拍：手机镜头从腰线向下记录下装局部，手机、手臂和上半身都在画面外。"
                if camera_kind == "selfie"
                else "第三人称摄影照片：拍摄者完全在画面外，镜头只记录腰部以下的下装局部。"
            )
            wear_match = re.search(r"本次服装搭配(?:已锁定为)?[:：]\s*([^。]+)", act)
            selected = ""
            if wear_match:
                selected_source = str(wear_match.group(1)).strip().split("；", 1)[0]
                selected = next((name for name in ("光腿神器", "白丝", "黑丝") if name in selected_source), "")
            selected_text = {
                "光腿神器": "自然肤色光腿神器（沿可见腿部连续覆盖）",
                "白丝": "白色不透白丝（从大腿上部沿可见腿部连续向下覆盖，袜口在大腿上部）",
                "黑丝": "黑色不透黑丝（从大腿上部沿可见腿部连续向下覆盖，袜口在大腿上部）",
            }.get(selected)
            legwear_line = (
                f"腿部穿搭已锁定为{selected_text}；只生成该选项，不参考参考图中的袜子或其他腿部穿搭；"
                "禁止中筒袜、短袜等停在小腿中段的普通袜型。"
                if selected_text
                else "腿部穿搭只允许光腿神器、白丝或黑丝三选一；不参考参考图中的袜子或其他腿部穿搭；禁止中筒袜、短袜等停在小腿中段的普通袜型。"
            )
            mode_lines.extend(
                [
                    f"【服装局部展示 / {'第一人称手机自拍' if camera_kind == 'selfie' else '第三人称摄影'}】",
                    camera_line,
                    "严格近距离取景：画面只有主角一人，保持自然的下半身服装局部构图，不扩展为半身或全身，不把膝关节或小腿作为固定裁切线。",
                    "画面主体为成年人物的得体日常服装展示，重点展示服装的颜色、材质、层次和自然版型；衣物穿着完整且不透明，保持室内柔和光线。",
                    legwear_line,
                    "腿部必须连续、自然并符合真实人体结构。画面边缘可以自然裁出腿部，衣物、家具或前景也可以按明确的前后关系合理遮挡；若小腿或脚不展示，必须自然延伸到画面外，或被边界清楚的实体物体完整遮挡。禁止在膝关节、小腿中段或脚踝附近突然终止；跪坐时脚踝应自然过渡到脚背或脚底，再由身体、衣摆或真实接触关系遮挡，不能把袜筒下缘直接当作小腿终点。地毯、床面或沙发面只有在真实接触和清晰前后关系下才可遮挡肢体，不能无缘无故吞没可见腿部。",
                    "按动作描述保持自然姿势，重心稳定，画面不出现多余人物或杂乱肢体。"
                    if pose_match
                    else "按动作描述保持自然坐姿、跪坐、侧躺、抱膝、交叠坐姿、窗边坐或席地屈膝，重心稳定，服装纹理清楚。",
                ]
            )
            if pose_match:
                pose_descriptions = {
                    "sit": "椅上或沙发自然坐姿，服装自然垂落",
                    "sit_crop": "椅上或沙发自然坐姿，服装自然垂落",
                    "kneel": "地毯或软垫上的自然跪坐，衣摆平整落下",
                    "kneel_crop": "地毯或软垫上的自然跪坐，衣摆平整落下",
                    "side_lie": "床边或沙发上的侧躺曲腿姿势，靠背和衣料自然入镜",
                    "side_lie_crop": "床边或沙发上的侧躺曲腿姿势，靠背和衣料自然入镜",
                    "hug_knee": "床边或地毯上的收膝坐姿，衣摆自然落下",
                    "hug_knee_crop": "床边或地毯上的收膝坐姿，衣摆自然落下",
                    "cross_leg": "椅上或沙发上的自然交叠坐姿",
                    "cross_leg_crop": "椅上或沙发上的自然交叠坐姿",
                    "windowsill": "窗台或矮柜自然坐姿，窗光柔和",
                    "windowsill_crop": "窗台或矮柜自然坐姿，窗光柔和",
                    "kneel_up": "软垫上的较高跪姿，衣物自然垂落",
                    "kneel_front": "地毯上的正面跪坐，衣摆整洁",
                    "floor_fold": "地毯或木地板上的轻松屈膝坐姿",
                    "one_knee_fix": "一侧单膝触地的自然整理衣摆动作",
                    "floor_knees_up_crop": "地毯或木地板上的轻松席地坐姿",
                    "reclined_knees_crop": "沙发或座椅上的轻松靠坐姿势",
                    "desk_sit_crop": "桌前椅上的自然坐姿，桌沿可入镜",
                    "bed_supine_crop": "床上由枕头支撑的舒适靠坐，衣摆和床品自然铺开",
                }
                pose_text = pose_descriptions.get(pose_match.group(1))
                if pose_text:
                    mode_lines.append(f"本张构图固定为：{pose_text}；不要改成其他姿势。")
            if pool_pose_text:
                mode_lines.append(f"本张使用看看腿随机姿势池条目：{pool_pose_text}。")
            if intent.change_clothes:
                mode_lines.append("本次同时包含换装要求：优先使用用户指定的服装/穿搭。")
        elif intent.is_third_person_photo:
            mode_lines.extend(
                [
                    "【他拍 / 日常照片模式】",
                    "别人视角的单人成品照：镜头已经对准你拍下，画面里只有你一个人；拍摄者完全在画面外，不要第二个人，不要有人举着手机拍你。",
                    "可以看向镜头、轻松回头；若画面是正面半身/近景，优先看向镜头，眼神自然有焦点，不要整段心不在焉。",
                    "机位与场景可自然变化，例如：半身平视、三分之四侧回头、中远景环境人像、近景胸像、靠墙门框、轻微低机位、走路抓拍等。",
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
        else:
            mode_lines.extend(
                [
                    "【今日穿搭 / 普通自拍模式】",
                    "本次是普通自拍 / 看看你现在的样子（自己举机或镜前自拍，不是别人代拍）。",
                    "优先使用你今天的穿搭、状态和心情来生成一张自然照片。",
                    "竖屏手机近景半身：自拍臂、镜前、窗边侧光、书桌坐拍、沙发随手、近景胸像，拍得近一些，不要次次同一构图。",
                    "场景与小动作保持日常：窗边、暖灯房间、书桌杯具、沙发抱枕、阳台、镜前台面等；可轻微笑、整理发丝/衣领、托腮、捧杯抬眼。",
                ]
            )

        extra = extract_user_extra_text(act)
        skip_outfit = extra_overrides_outfit(extra) or intent.change_clothes
        skip_period = extra_overrides_period(extra)
        use_daily_now = (not intent.is_legs_only) and (not intent.is_cos_look)
        today_lines: list[str] = []
        if use_daily_now and daily and daily.outfit and not skip_outfit:
            today_lines.append(f"今日穿搭：{daily.outfit}")
        if use_daily_now and daily and not skip_period:
            period = current_period()
            status = ""
            if daily.status_by_period:
                status = str(daily.status_by_period.get(period) or "")
            status = status or str(daily.status or "")
            if status:
                today_lines.append(f"当前时间段：{period_label(period)}")
                today_lines.append(f"当前状态：{status}")
            if daily.mood:
                today_lines.append(f"当前心情：{daily.mood}")

        if intent.is_legs_only:
            extra_action = extract_user_extra_text(act)
            action_line = f"用户要求：{extra_action}" if extra_action else "用户要求：按服装局部展示模式生成。"
        else:
            action_line = f"用户要求：{act}" if act else "用户要求：看着镜头自然自拍，展示你现在的样子。"
        if intent.is_legs_only:
            subject_photo_label = "第三人称摄影服装记录" if intent.is_third_person_photo else "第一人称自拍服装记录"
        else:
            subject_photo_label = "日常他拍照片" if intent.is_third_person_photo and not intent.is_group_photo else "自拍照片"
        if intent.is_cos_look:
            camera_is_third = bool(intent.is_third_person_photo)
            output_lines = [
                "【生成要求】",
                "1. 主角身份稳定：脸型五官、体态来自参考图一；假发/发饰按 COS 套装。",
                "2. 这是 COS 换装"
                + ("他拍" if camera_is_third else "自拍")
                + "：完整展示指定套装层次，竖屏近景半身，不要改成晒腿近景或合影。",
                "3. 面部清晰可见、自然看向镜头，除非用户明确要求遮脸。",
                "4. 画面像随手拍的竖屏 COS 封面，主体清晰，服装还原优先，不要棚拍全身。",
                "5. 人体结构自然完整：左右手/脚各一只，手与胳膊连续连接。",
            ]
        elif intent.is_group_photo:
            output_lines = [
                "【生成要求】保持所有人物独立、边界清晰，统一场景与画风。",
                "人体结构自然完整：每人左右手/脚各一只，手与胳膊连续连接。",
                "同框角色得体成年、互动自然，默认看向镜头（用户另有要求除外）。",
            ]
        elif intent.is_legs_only:
            # The legs-only mode lines above already contain the complete crop,
            # subject, clothing, and lighting constraints; do not repeat them.
            output_lines = []
        else:
            output_lines = [
                "【生成要求】按以上身份、场景和动作生成一张完整自然的竖屏照片。",
                *anatomy_constraint_lines(style="general"),
            ]

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
                    f"今日穿搭：{daily.outfit}",
                    f"当前状态({period_label(current_period())})：{daily.status}",
                    f"当前心情：{daily.mood}",
                ]
            )
        return "\n".join(lines)
