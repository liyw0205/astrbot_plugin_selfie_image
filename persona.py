"""AI selfie persona management."""

from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .utils import detect_mime_by_bytes, ext_from_mime, load_json_file, save_json_file


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
        "清晨穿着奶油白细针织上衣和浅杏短开衫，下身是轻薄格纹短裙，配白色短袜，像刚收拾好准备出门前的样子。",
        "午后是浅粉宽松卫衣配奶白色短裙和柔软居家袜，发间夹一枚小珍珠发夹，像在家里随手窝着休息。",
        "傍晚换成雾紫色针织连衣裙，外披奶白毛绒小披肩，布料柔软，适合暖灯下安静坐着。",
        "白天偏清爽的浅蓝灰宽松衬衫配白色高腰半身裙，袖口松松卷起，像随手拍到的日常穿搭。",
        "夜里是月白宽松毛衣配浅灰百褶裙和软绵中筒袜，整体温暖、松弛，像准备窝回房间休息。",
        "深夜会偏居家一点，宽松薄针织或短款家居上衣配轻软短裙，袜子和鞋子不固定，重点是舒服自然。",
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

    def analyze_selfie_intent(self, action: str) -> SelfieIntent:
        raw = str(action or "").strip()
        compact = normalize_intent_text(raw)
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
            compact,
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
            compact,
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
        is_legs_only = includes_any(compact, ["看看腿", "看腿", "拍腿", "自拍腿", "丝袜", "黑丝", "白丝", "肉丝", "光腿", "美腿", "大腿", "腿"])
        use_today = not compact or includes_any(compact, ["看看你", "看下你", "你长什么样", "你的样子", "今日穿搭", "今天穿搭", "今天这身"])
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
        )

    async def ensure_daily_selfie_profile(self, action: str = "") -> DailySelfieProfile:
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
        daily = self.get_daily_selfie_profile()

        identity_lines = (
            [
                "存在固定自拍形象参考图。",
                "参考图一是唯一主体身份参考图。",
                "必须保持参考图一中的同一角色身份、脸部特征、发型、发色、气质和整体形象，不要变成另一个人。",
                "角色名称和人设只用于语气、氛围和角色一致性，不要覆盖参考图一的脸和身份。",
            ]
            if has_reference_image
            else [
                "当前没有固定自拍形象参考图。",
                "请严格根据角色名称、人设和今日状态生成同一个稳定角色，不要生成随机路人脸。",
                "没有形象参考图时，角色名称、人设和今日设定就是主角身份的主要依据。",
            ]
        )

        reference_lines: list[str] = []
        if extra_reference_count > 0:
            reference_lines.append(f"当前除{'参考图一' if has_reference_image else '主角设定'}外，还有 {extra_reference_count} 张额外参考图。")
            if intent.is_group_photo:
                reference_lines.extend(
                    [
                        "额外参考图在合照模式下优先作为不同同框对象的身份与外观参考，其次才作为服装、姿势、构图和风格参考。",
                        "必须先检查每张额外参考图里实际可见的人物 / 角色数量；如果单张额外参考图里有多个人，就把这些人都作为独立同框对象保留。",
                        "真人参考图里的每一个真人都应按实际可见身份进入合影，分别保留脸部特征、发型、穿搭、体型、姿态和相对站位；除非用户明确指定只要其中一人，否则不要只提取一个人。",
                        "不要把单张多人参考图里的多个人融合成一个人，也不要复制同一张脸代替不同人物。",
                        "如果有多张额外参考图，应尽量把每张图理解为一个独立同框对象或独立参考来源。",
                        "不要只使用其中一张额外参考图，也不要把多张参考图的人脸融合成一个人。",
                        "如果额外参考图是动漫、游戏、二次元、插画、头像、Q版、表情包、贴纸、动物、物品或卡通角色，默认将其真人化 / 拟人化为真实或半写实的人类同框对象。",
                        "真人化 / 拟人化时要保留参考图角色的关键识别特征，例如发色、发型、耳朵/角/尾巴等标志性元素、主色调、服装轮廓、表情气质和可识别的小配饰。",
                        "不要把二维原图、表情包、贴纸、卡通头或原图文字直接贴进画面；不要让同框对象仍然保持扁平卡通、Q版或表情包形态。",
                        "如果参考图上带有文字、UI、边框、水印或表情包字幕，这些只用于理解角色来源，不要在最终图片里复现。",
                    ]
                )
            else:
                reference_lines.extend(
                    [
                        "额外参考图只能作为服装、姿势、构图、风格、场景、道具参考，不要覆盖主角身份。",
                        "参考图一始终是 AI 自己的主体身份参考图；不要把额外参考图中的人物身份替换成 AI 自己。",
                    ]
                    if has_reference_image
                    else ["额外参考图可以辅助构图、衣服、姿势，但主角仍应符合角色名称和人设。"]
                )

        mode_lines: list[str] = []
        if intent.is_group_photo:
            mode_lines.extend(
                [
                    "【合照 / 同框模式】",
                    "本次要求是合照 / 合影 / 同框，主角仍然是你自己。",
                    "先确定你自己的形象，再为额外参考图生成独立同框对象；不要把你和参考图对象合成同一个人。",
                    "当额外参考图不是现实人物照片时，必须做拟人化 / 真人化处理，让它成为能自然站在你身边的人类角色。",
                    "同框人物应自然站位或坐位，有合理距离、遮挡关系、视线方向和肢体互动。",
                    "所有人物必须处在同一个场景中，使用统一光线、统一色调、统一画风和统一相机透视。",
                    "整体效果应像同一时间、同一地点、同一相机拍下的一张照片，而不是多张图拼接。",
                ]
            )
            if intent.is_multi_person_group_photo or extra_reference_count >= 2:
                mode_lines.append("本次允许多人合影；每个人都应有清晰、独立、稳定的身份，不要复制脸，不要融合脸。")
        elif intent.is_legs_only:
            mode_lines.extend(
                [
                    "【特写自拍 / 晒腿模式】",
                    "本次重点是成年角色的自然坐姿自拍，构图重点放在腿部线条和当前穿搭上，画面得体、日常、柔和、非挑逗，不要拍成普通正面自拍。",
                    "优先使用第一人称俯视视角（POV, first-person view, looking down at own legs），像低头看向自己腿部的随手拍；也可以使用自然低角度坐姿自拍，但除非用户明确指定，否则不要使用完整露脸、对镜或站姿构图。",
                    "主角可以坐在床沿、沙发、单人椅、窗边椅或地毯边，双腿自然向前、斜侧摆放、轻微交叠或并拢放松；膝盖和脚尖方向协调，脚踝线条清楚，坐姿要顺眼、放松、符合人体结构。",
                    "画面重点呈现裙摆、膝盖、小腿、脚踝、鞋袜搭配和衣料垂落，让腿部线条、穿搭层次、袜口、鞋面材质、地毯/床单/木地板纹理都自然好看。",
                    "手部互动要像日常自拍里的小动作：轻轻整理或拉住裙摆/衣角、扶住膝盖、调整坐姿、整理袜口或鞋带；如果用户明确要求丝袜，可以轻轻整理丝袜边缘，但动作要含蓄自然，不要固定成拉扯姿势。",
                    "构图重点必须放在腿部和下半身，脸部只可少量入镜甚至完全不入镜；避免夸张广角、畸形拉伸、腿部变短、关节不自然、膝盖脚踝被乱裁。",
                    "环境和光线要跟当前时间段、当天穿搭、房间状态一起变化：可以是晨光、午后漫反射、傍晚暖灯、夜里床边小灯，也可以是居家地毯、沙发边、窗边、床单、木地板或浅色系房间，不要每次都像同一个模板房间。",
                    "细节刻画应根据当前穿搭自然变化，可以突出裸腿、短袜、长袜、丝袜、裙摆垂落、鞋面材质、毛毯纹理和柔和室内光影，但不要每次固定成同一种丝袜拉扯画面。",
                    "避免过度暴露、内衣视角、性暗示姿势或夸张肢体特写。画面干净、自然、写实，保持私密但温柔的日常随手拍氛围。",
                ]
            )
            if intent.change_clothes:
                mode_lines.append("本次同时包含换装要求：优先使用用户指定的服装/穿搭，不要用今日穿搭覆盖它。")
        elif intent.change_clothes and intent.change_pose:
            mode_lines.extend(
                [
                    "【换衣服 + 改姿势模式】",
                    "保持你自己的身份、脸部特征、发型气质和核心形象不变。",
                    "本次优化重点：先锁定身份，再同时迁移服装/配饰与姿势/动作，不改变人物身份。",
                    "额外参考图优先用于服装、配饰、颜色、材质、姿势、动作、镜头角度和构图。",
                    "不要使用今日穿搭覆盖用户指定或参考图中的衣服。",
                ]
            )
        elif intent.change_clothes:
            mode_lines.extend(
                [
                    "【改衣服 / 改穿搭模式】",
                    "本次重点是换装 / 穿搭 / 服装变化。",
                    "保持你自己的身份、脸部特征、发型气质和核心形象不变。",
                    "本次优化重点：只替换衣服、配饰、材质、配色和造型氛围，不迁移参考图人物身份。",
                    "额外参考图只用于服装、配饰、造型、颜色、材质参考，不要把参考图中的人替换成你。",
                ]
            )
        elif intent.change_pose:
            mode_lines.extend(
                [
                    "【改姿势 / 改动作模式】",
                    "本次重点是姿势 / 动作 / 表情变化。",
                    "本次优化重点：保持身份和穿搭稳定，只改变姿势、动作、表情、镜头角度和构图。",
                    "优先保持今天的穿搭不变，只改变姿势、表情、镜头角度或肢体动作。",
                    "额外参考图只参考其姿势、动作、表情、镜头角度或构图。",
                ]
            )
        else:
            mode_lines.extend(
                [
                    "【今日穿搭 / 普通自拍模式】",
                    "本次是普通自拍 / 看看你现在的样子。",
                    "优先使用你今天的穿搭、状态和心情来生成一张自然照片。",
                ]
            )

        today_lines: list[str] = []
        if daily and daily.outfit and not intent.change_clothes:
            today_lines.append(f"今日穿搭：{daily.outfit}")
        if daily and daily.status:
            today_lines.append(f"当前时间段：{period_label(current_period())}")
            today_lines.append(f"当前状态：{daily.status}")
        if daily and daily.mood:
            today_lines.append(f"当前心情：{daily.mood}")

        action_line = f"用户要求：{act}" if act else "用户要求：看着镜头自然自拍，展示你现在的样子。"
        output_lines = (
            [
                "【生成要求】",
                "1. 你必须是画面主角之一，且身份来自参考图一或角色设定。",
                "2. 额外参考图中实际可见的每一个真人或角色都应作为独立同框对象保留；单张多人参考图不能只取一个人。",
                "3. 所有人物必须在同一个完整场景中，自然站位或坐位，姿势协调，比例合理，透视一致。",
                "4. 整张图像应像真实拍下的一张自然合照，不要多视角，不要拼图，不要分镜。",
                "5. 所有人物人体结构必须完整自然；头、手臂、手、手指、腿和脚数量正确，比例合理。",
                "6. 不要肢体残缺、不要多肢异肢、不要多手多脚、不要手指缺失或多指、不要手脚融合、不要断腕扭手、不要身体部位漂浮或错位。",
                "7. 非真人额外参考图必须真人化 / 拟人化成同框人类角色，同时保留其核心识别特征。",
                "8. 不要文字水印，不要角色设定图，不要多人复制脸。",
                "single coherent group photo, natural group selfie, include every distinct visible real person or character from the reference images, preserve the actual number of visible people in each multi-person reference image, do not extract only one person from a group reference photo, multiple distinct real human people if references are provided, anatomically complete bodies, correct number of arms hands fingers legs and feet, no missing limbs, no extra limbs, no malformed limbs, no fused limbs, no detached body parts, no extra fingers, no missing fingers, no broken wrists, anthropomorphize or humanize anime/cartoon/sticker/non-human references into realistic human companions, preserve key recognizable traits, consistent lighting, same camera perspective, no collage, no split screen, no face merging, no duplicated faces, no watermark, no text",
            ]
            if intent.is_group_photo
            else [
                "【生成要求】",
                "1. 保持主角就是你自己，不要变成另一个人。",
                "2. 可以根据本次要求自然变化衣服、姿势、表情、室内氛围和小道具。",
                "3. 整张图应像今天真实拍下的一张照片，而不是模板图。",
                "4. 人体结构必须完整自然；头、手臂、手、手指、腿和脚数量正确，比例合理。",
                "5. 不要肢体残缺、不要多肢异肢、不要多手多脚、不要手指缺失或多指、不要手脚融合、不要断腕扭手、不要身体部位漂浮或错位。",
                "6. 不要拼图，不要分镜，不要角色展示板，不要多视角，不要文字水印。",
                "single image, natural selfie photo, complete and unified scene, anatomically complete body, correct number of arms hands fingers legs and feet, no missing limbs, no extra limbs, no malformed limbs, no fused limbs, no detached body parts, no extra fingers, no missing fingers, no broken wrists, no collage, no grid, no split screen, no character sheet, no multiple views, no watermark, no text",
            ]
        )

        return "\n".join(
            line
            for line in [
                f"这是 {bot_name or 'AI'} 的自拍照片。",
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
