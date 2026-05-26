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
    is_third_person_photo: bool = False


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
                "丝袜",
                "黑丝",
                "白丝",
                "肉丝",
                "光腿",
                "连裤袜",
                "过膝袜",
                "长袜",
                "短袜",
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
        is_third_person_photo = includes_any(
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
                "thirdperson",
                "notselfie",
                "candidphoto",
                "takenbyanotherperson",
                "shotbyanotherperson",
            ],
        )
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
            is_third_person_photo=is_third_person_photo,
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
                "固定形象参考：参考图一是主角身份。",
                "保持同一角色的脸、五官、发型、发色、气质、体态和整体形象；角色名称和人设只辅助语气与氛围。",
            ]
            if has_reference_image
            else [
                "形象参考以角色名称、人设和今日状态为准，生成稳定主角身份。",
            ]
        )

        reference_lines: list[str] = []
        if extra_reference_count > 0:
            reference_lines.append(f"另有 {extra_reference_count} 张额外参考图。")
            if intent.is_group_photo:
                reference_lines.extend(
                    [
                        "合照时，额外参考图优先作为同框对象；保留每张图中可见的人物/角色数量、脸部特征、发型、穿搭、体型、姿态和相对站位。",
                        "多张额外参考图可分别作为独立同框对象或独立参考来源。",
                        "非真人参考默认真人化/拟人化为自然同框的人类角色，保留发色、发型、耳朵/角/尾巴等标志元素、主色调、服装轮廓、表情气质和小配饰。",
                    ]
                )
            else:
                reference_lines.extend(
                    [
                        "额外参考图用于服装、姿势、构图、风格、场景、道具、镜头角度或光线氛围；主角身份仍来自参考图一。",
                    ]
                    if has_reference_image
                    else ["额外参考图用于构图、衣服、姿势、场景或光线氛围；主角仍符合角色名称和人设。"]
                )

        mode_lines: list[str] = []
        if intent.is_group_photo:
            mode_lines.extend(
                [
                    "【合照 / 同框模式】",
                    "先确定你自己的形象，再为额外参考图生成独立同框对象。",
                    "非真人参考真人化/拟人化为可自然站在身边的人类角色。",
                    "同框人物自然站位或坐位，有合理距离、遮挡关系、视线方向和肢体互动。",
                    "所有人物在同一场景中，光线、色调、画风和相机透视统一。",
                    "整体像同一时间、同一地点、同一相机拍下的一张自然合照。",
                ]
            )
            if intent.is_multi_person_group_photo or extra_reference_count >= 2:
                mode_lines.append("多人合影时，每个人都有清晰、独立、稳定的身份。")
        elif intent.is_legs_only:
            mode_lines.extend(
                [
                    "【特写自拍 / 晒腿模式】",
                    "成年角色自然坐姿随手拍，构图重点放在腿部线条和袜装上，画面得体、日常、柔和。",
                    "默认黑色透肤丝袜/黑丝；用户明确指定其他袜装时按用户要求。",
                    "优先第一人称俯视视角（POV，低头看自己的腿）或自然低角度坐姿自拍。",
                    "主角可坐在床沿、沙发、单人椅、窗边椅或地毯边，双腿向前、斜侧摆放、轻微交叠或并拢放松；膝盖和脚尖方向协调，脚踝线条清楚。",
                    "画面重点呈现裙摆/裤脚、膝盖、小腿、脚踝、鞋袜搭配、衣料垂落、袜口、鞋面材质、地毯/床单/木地板纹理。",
                    "脸部保持在画面外，构图集中在下半身、腿部和周围居家环境。",
                    "手部可自然整理裙摆、衣角、袜口或鞋带，也可以轻扶膝盖；动作含蓄放松。",
                    "环境和光线跟当前时间段变化：晨光、午后漫反射、傍晚暖灯、夜里床边小灯、居家地毯、沙发边、窗边、床单、木地板或浅色系房间。",
                    "画面干净、自然、写实，有私密但温柔的日常随手拍氛围。",
                ]
            )
            if intent.change_clothes:
                mode_lines.append("本次同时包含换装要求：优先使用用户指定的服装/穿搭。")
        elif intent.is_third_person_photo:
            mode_lines.extend(
                [
                    "【他拍 / 日常照片模式】",
                    "镜头视角来自画面外的拍摄者，像朋友在旁边用相机或手机自然拍下你。",
                    "你可以看向镜头、轻松回头、坐着发呆、整理东西或自然做自己的事，姿态像生活里被随手拍到。",
                    "画面带轻微抓拍感和生活感，同时脸部、穿搭、姿态、背景层次和光线清晰自然。",
                ]
            )
            if intent.change_clothes:
                mode_lines.append("本次同时包含换装要求：优先使用用户指定的服装/穿搭。")
            if intent.change_pose:
                mode_lines.append("本次同时包含姿势/动作要求：在他拍视角下自然完成用户指定的动作或表情。")
        elif intent.change_clothes and intent.change_pose:
            mode_lines.extend(
                [
                    "【换衣服 + 改姿势模式】",
                    "保持身份、脸部特征、发型气质和核心形象稳定。",
                    "先锁定身份，再同时迁移服装/配饰与姿势/动作。",
                    "额外参考图优先用于服装、配饰、颜色、材质、姿势、动作、镜头角度和构图。",
                ]
            )
        elif intent.change_clothes:
            mode_lines.extend(
                [
                    "【改衣服 / 改穿搭模式】",
                    "重点是换装、穿搭或服装变化。",
                    "保持身份、脸部特征、发型气质和核心形象稳定。",
                    "只替换衣服、配饰、材质、配色和造型氛围。",
                    "额外参考图用于服装、配饰、造型、颜色、材质和穿搭层次参考。",
                ]
            )
        elif intent.change_pose:
            mode_lines.extend(
                [
                    "【改姿势 / 改动作模式】",
                    "重点是姿势、动作或表情变化。",
                    "保持身份和穿搭稳定，调整姿势、动作、表情、镜头角度和构图。",
                    "姿势要自然放松，身体重心、手脚位置、视线方向和画面留白协调。",
                    "额外参考图用于姿势、动作、表情、镜头角度或构图参考。",
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
        if daily and daily.outfit and not intent.change_clothes and not intent.is_legs_only:
            today_lines.append(f"今日穿搭：{daily.outfit}")
        if daily and daily.status:
            today_lines.append(f"当前时间段：{period_label(current_period())}")
            today_lines.append(f"当前状态：{daily.status}")
        if daily and daily.mood:
            today_lines.append(f"当前心情：{daily.mood}")

        action_line = f"用户要求：{act}" if act else "用户要求：看着镜头自然自拍，展示你现在的样子。"
        subject_photo_label = "日常他拍照片" if intent.is_third_person_photo and not intent.is_group_photo else "自拍照片"
        if intent.is_group_photo:
            output_lines = [
                "【生成要求】",
                "1. 主角是你自己，身份来自参考图一或角色设定。",
                "2. 额外参考图里的真人/角色按实际数量作为独立同框对象，保留关键外观和相对关系。",
                "3. 所有人物在同一完整场景中，自然站位或坐位，姿势协调，比例合理，透视一致。",
                "4. 整张图像像真实拍下的一张自然合照，统一光线、色调、景深和相机视角。",
                "5. 人体结构自然完整；多人合照身份清晰，脸、发型、服装、体态和身体各自独立。",
                "6. 非真人额外参考图真人化/拟人化为同框人类角色，保留核心识别特征。",
            ]
        elif intent.is_legs_only:
            output_lines = [
                "【生成要求】",
                "1. 主角身份稳定，来自参考图一或角色设定。",
                "2. 构图集中在下半身、腿部线条、袜装、鞋面、衣料垂落和居家材质，脸部保持在画面外。",
                "3. 镜头语言：第一人称俯视或自然低角度坐姿自拍，膝盖、小腿、脚踝和鞋袜清晰。",
                "4. 人体结构自然完整，腿部比例、坐姿重心、手部互动、衣料和光影关系可信。",
                "5. 保持单张完整照片效果，统一光线、色调、景深和相机透视。",
            ]
        else:
            output_lines = [
                "【生成要求】",
                "1. 主角身份稳定，来自参考图一或角色设定。",
                "2. 根据本次要求自然调整衣服、姿势、表情、环境和小道具。",
                "3. 画面像今天真实拍下的一张照片，构图完整，主体清晰，背景有生活细节。",
                "4. 人体结构自然完整，手脚和身体比例协调，姿态、衣料、发丝和光影关系可信。",
                "5. 镜头语言：" + ("画面外拍摄者的日常他拍，带自然抓拍感。" if intent.is_third_person_photo else "自然自拍，视角、手臂位置、脸部表情和环境关系协调。"),
                "6. 保持单张完整照片效果，统一光线、色调、景深和相机透视。",
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
