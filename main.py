"""AstrBot image and selfie generation plugin."""

from __future__ import annotations

RECORD_KEEP_LIMIT = 300

import asyncio
import copy
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, MutableMapping
from typing import Any, AsyncGenerator, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import aiohttp

try:
    from astrbot.api.star import Context, Star, register
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.message_components import Image
    from astrbot.api import llm_tool, logger
except ImportError:  # Compatibility with older AstrBot layouts
    from astrbot.api.star import Context, Star, register
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.event.components import Image
    from astrbot.api import llm_tool
    from astrbot.api.utils import logger

try:
    from astrbot.api.message_components import Video  # type: ignore
except Exception:
    try:
        from astrbot.api.event.components import Video  # type: ignore
    except Exception:
        Video = None  # type: ignore

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:
    def get_astrbot_data_path() -> str:
        return os.path.join(os.getcwd(), "data")

from .constants import (
    LEGACY_CONFIG_FILENAME,
    LEGACY_PLUGIN_NAME,
    PLUGIN_AUTHOR,
    PLUGIN_CONFIG_FILENAME,
    PLUGIN_DISPLAY_NAME,
    PLUGIN_NAME,
    PLUGIN_VERSION,
)
from .generator import generate_image_with_fallback
from .preset import ImagePresetManager
from .models import (
    AICatConfig,
    DEFAULT_CONFIG,
    ImageModelTarget,
    deep_merge,
    normalize_config_tree,
    normalize_legacy_keys,
    preflight_video_channel,
)
from .persona import PersonaManager
from .studio import (
    BUILTIN_PROMPTS,
    StudioStore,
    build_studio_action,
    global_prompt_presets,
    list_studio_templates,
    normalize_template_id,
    prompts_for_template,
    resolve_slot_refs_for_run,
)
from .providers import (
    ImageGenerateRequest,
    ImageReference,
    build_model_list_urls,
    extract_model_ids_from_response,
    normalize_image_base_url,
    provider_type_from_channel_payload,
)
from .reference_collector import ReferenceCollector
from .proxy import channel_client_session, http_proxy_url, image_client_timeout, target_session_proxy
from .video import VideoGenerateRequest, generate_video_with_fallback
from .utils import (
    bytes_to_data_url,
    collect_record_cache_paths,
    collect_cache_cleanup_candidates,
    collect_unreferenced_record_cache_paths,
    compact_generation_record,
    split_generation_record_images,
    data_url_to_bytes,
    detect_mime_by_bytes,
    event_group_id,
    event_user_id,
    extract_command_message,
    extract_event_text,
    extract_image_sources_from_event,
    extract_image_urls,
    fetch_image_source,
    load_json_file,
    looks_like_image_bytes,
    normalize_image_mime,
    parse_audit_response_text,
    redact_sensitive_data,
    redact_sensitive_text,
    resolve_awaitable,
    safe_delete_relative_files,
    save_image_bytes,
    save_json_file,
    summarize_record_for_list,
)
from .dashboard_api import SelfieImageDashboardAPI
from .web import FlaskWebServer

LLM_TOOL = getattr(filter, "llm_tool", llm_tool)
WEB_STARTUP_CONFIG_KEYS = ("web", "webEnable", "webHost", "webPort", "webToken")
DEFAULT_WEB_TOKEN = str(DEFAULT_CONFIG["web"].get("token") or "changeme").strip().lower()


def optional_event_message_type(priority: int = 100):
    decorator = getattr(filter, "event_message_type", None)
    event_type = getattr(getattr(filter, "EventMessageType", None), "ALL", None)
    if callable(decorator) and event_type is not None:
        return decorator(event_type, priority=priority)

    def passthrough(func):
        return func

    return passthrough


def anatomy_constraint_lines(*, style: str = "general") -> list[str]:
    """Shared body-part constraints for selfie/draw prompts. Implemented in persona."""
    from .persona import anatomy_constraint_lines as _lines

    return _lines(style=style)


def append_anatomy_constraints(prompt: str, *, language: str = "zh") -> str:
    """Optional quality/anatomy pad for strong fixed selfie builtins only.

    Freeform /画 /文生图 /图生图 should not use this — user/preset text already
    carries intent, and stacking anatomy negatives conflicts or blows length.
    """
    raw = str(prompt or "").strip()
    if not raw:
        return raw
    if language != "en":
        return "\n".join(
            [
                raw,
                "",
                "构图与画面质量：",
                "生成一张光线自然、透视稳定、主体清晰的完整画面；人物或动物结构自然完整。",
                *anatomy_constraint_lines(style="general"),
            ]
        )
    return "\n".join(
        [
            raw,
            "",
            "Composition and quality:",
            "Use a coherent single image with natural lighting, stable perspective, clear subject focus, and complete natural anatomy for people or animals.",
            *anatomy_constraint_lines(style="en"),
        ]
    )


def build_prompt_with_reference_instruction(
    prompt: str,
    images: List[ImageReference],
    *,
    language: str = "zh",
    enhance: bool = False,
) -> str:
    """Freeform draw/img2img wrapper.

    Default (enhance=False): keep user/preset text intact. With refs, add a short
    reference instruction only — no anatomy/negative dump.
    enhance=True is for rare fixed pipelines that still want anatomy padding.
    """
    raw = str(prompt or "").strip()
    if not images:
        return append_anatomy_constraints(raw, language=language) if enhance else raw
    if language != "en":
        lines = [
            "使用提供的参考图作为视觉参考。",
            "按用户要求修改，并尽量保持相关人物身份、服装、姿势、场景与构图一致。",
        ]
        if enhance:
            lines.extend(
                [
                    "画面光线、透视与色调统一，人体结构自然完整。",
                    *anatomy_constraint_lines(style="general"),
                ]
            )
        lines.extend(["", "用户要求：", raw])
        return "\n".join(lines)
    lines = [
        "Use the provided reference image(s) as visual references.",
        "Follow the user's requested changes while preserving relevant identity, outfit, pose, scene, and composition.",
    ]
    if enhance:
        lines.extend(
            [
                "Create one coherent image with unified lighting, perspective, and natural complete anatomy.",
                *anatomy_constraint_lines(style="en"),
            ]
        )
    lines.extend(["", "User request:", raw])
    return "\n".join(lines)


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

# Poses that crop calves/feet off-frame to reduce weird lower-leg anatomy.
CALF_CROP_POSES = frozenset(
    name for name in LEGWEAR_BY_POSE if name.endswith("_crop")
)

# Camera choices are pose-aware so a phone selfie does not fight the physical
# setup.  The weights still leave room for the alternate viewpoint.
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


def is_leg_calf_crop_action(text: str) -> bool:
    raw = str(text or "")
    m = re.search(r"【pose:([a-z_]+)】", raw)
    if m and m.group(1) in CALF_CROP_POSES:
        return True
    if "【legs:outfit】" in raw or "下半身穿搭" in raw or "穿搭展示" in raw:
        return True
    if "【crop:calves】" in raw or "双脚完整裁出画外" in raw:
        return True
    return "大腿" in raw and "小腿" in raw and "画外" in raw

LEGWEAR_PROMPTS = {
    "光腿神器": (
        "本次服装搭配：自然肤色日常搭配，材质自然，作为得体穿搭的一部分。"
    ),
    "白丝": (
        "本次服装搭配：白色不透长袜，袜口和材质自然，作为得体日常穿搭的一部分。"
    ),
    "黑丝": (
        "本次服装搭配：黑色不透长袜，袜口和材质自然，作为得体日常穿搭的一部分。"
    ),
}

# Stocking finishes. User asked to see 花边/腿环 again; keep 镂空/勒痕 out.
STOCKING_FINISH_CHOICES: List[Tuple[str, int]] = [
    ("袜口宽花边，素色袜身。", 4),
    ("膝上细腿环，袜口平整。", 4),
    ("袜口花边加细腿环。", 3),
    ("袜口小蝴蝶结，素色袜身。", 2),
    ("袜身带细竖纹，袜口平整。", 2),
    ("袜口轻轻卷边，素色袜身。", 1),
]


def pick_stocking_finish() -> str:
    bag: List[str] = []
    for text, weight in STOCKING_FINISH_CHOICES:
        bag.extend([text] * max(1, int(weight)))
    return random.choice(bag)

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
    "光腿神器": "自然肤色日常搭配",
    "白丝": "白色不透长袜",
    "黑丝": "黑色不透长袜",
}


# COS pool: garment silhouettes first. Work/source lock is optional —
# character outfits may name a series; category outfits (hanfu, white dress)
# only describe visible structure, never Douyin/author/title provenance.
COS_LOOK_SETS: List[Dict[str, str]] = [
    {"id": "roxy_cream", "title": "洛琪希·奶油睡衣", "prompt": "严格换装为《无职转生》洛琪希的奶油色居家睡衣两件套：米白奶油色无袖睡衣上衣，柔软微皱棉质，领口与肩线细荷叶边抽褶；胸前白色缎带大蝴蝶结与长飘带；同色宽松睡衣短裤；浅薰衣草紫超长发双麻花辫垂在胸前；姿势严格为跪坐在地毯上，双膝并拢，双腿收拢并拢压在身下，臀部坐在脚跟上，脚部不要向两侧张开，不要盘腿、分腿或站立；对镜坐在木地板地毯上；禁止蓝色旅行法师外套、宽檐帽、法杖。"},
    {"id": "hanfu_peach", "title": "齐胸汉服·桃粉", "prompt": "严格换装为桃粉齐胸汉服：齐胸抹胸高腰，桃粉多层长裙，轻薄裙摆与细微花卉刺绣；高腰翠绿宽丝带和长飘带；黑发高髻配粉色牡丹与金簪；细金项链和长吊坠耳环；古风优雅，日常得体，不要现代礼服。"},
    {"id": "mint_sheer_hanfu", "title": "薄荷粉纱汉服", "prompt": "严格换装为薄荷绿与淡粉渐变的轻纱汉服：外层宽袖薄纱袍，淡雅晕染花卉，内衬浅粉齐胸襦裙；深棕长发高髻、空气刘海，右侧白色绢花，左侧长辫垂腰；细银链小吊坠；清雅自然，日常得体，不要铠甲。"},
    {"id": "rem_blue_lolita", "title": "蕾姆·蓝白女仆洛丽塔", "prompt": "严格换装为《Re:Zero》蕾姆的罗兹瓦尔宅邸经典蓝白女仆服，不是泛化女仆装：浅蓝短发、侧分长刘海，白色褶边女仆发箍与蓝色小花发饰；白色长袖蓬袖衬衣，外穿深蓝至蓝黑色贴身无袖女仆连衣裙/束身上衣，白色褶边胸襟和白色蕾丝围裙覆盖前胸，白色围裙腰带在腰后系大蝴蝶结；深蓝色膝上蓬裙，裙摆白色蕾丝荷叶边，后腰有分叉燕尾式深色后摆；白色过膝袜带深色腿环，黑色圆头玛丽珍女仆鞋。严格保持浅蓝、白、深蓝三色结构，室内柔光对镜全身；不要拉姆粉色女仆服、不要普通黑白法式女仆、不要黑色哥特洛丽塔、不要蓝色水手服、不要校服、不要短袖、不要现代服务员制服。"},
    {"id": "white_slip_mini", "title": "白细肩带迷你裙", "prompt": "严格换装为白细肩带迷你裙：黑发直刘海，高马尾从脑后束起垂到后背。上身纯白细肩带吊带裙，肩带约一指宽，低圆领到锁骨下，无袖，胸腰一体、不束腰封。裙身单层轻薄缎面，自然垂褶，A字微扩，裙摆只到大腿中段。裸腿，白色厚底运动鞋、白鞋带。室内柔光对镜全身。不是拖地白婚纱，也不是齐胸长裙汉服。"},
    {"id": "haiyue_petal_jelly", "title": "海月·白蓝花瓣水母", "prompt": "严格换装为《王者荣耀》海月这一版白蓝花瓣水母短款：深蓝近黑长直发垂过腰。上身近无袖，深甜心领，胸前层层象牙白与浅蓝立体花瓣荷叶，左胸一朵深蓝结晶花，细银链小吊坠垂在腹前；右侧肩上白蓝立体花簇。腰侧大开，露出肋与腰腹。下装高腰多层薄纱裙，髋上圈一圈白蓝紫花瓣荷叶，外层白到浅紫薄纱，左侧高开叉到大腿。主色象牙白、浅蓝、浅紫、少量深蓝。室内柔光对镜全身。不是原皮宽袖长袍汉服。"},
    {"id": "xishi_fan_qipao", "title": "西施·同人短旗袍", "prompt": "严格换装为《王者荣耀》西施同人短旗袍这一版：铂金银白长直发中分，两侧长鬓，头顶一对薄荷白鹿角，一侧薄荷小花发饰。上身是贴身无袖短旗袍，高立领金滚边与金盘扣，领下低开到胸口，正中金绣花或贝壳纹。肩臂另接薄荷薄纱荷叶袖，纱袖垂到小腿。旗袍缎面银白微闪，下摆薄荷暗纹，金边包到大腿中段，一侧高开叉。白色不透明过膝袜，袜口白蕾丝。室内素墙对镜全身。不是原皮长裙水莲汉服。"},
    {"id": "lanmeng_dragon_path", "title": "蓝梦·龙之道", "prompt": "严格换装为《永劫无间》蓝梦龙之道这一版：黑发齐刘海，两侧高发包，发上金珠与金缎带，长段泡泡马尾垂下。上身是浅金海波纹一字肩短上衣，无袖，只到胸下，露出腰腹；胸前菱形黑边开窗，黑滚边与盘扣。外搭松垮黑袍从一侧肩臂垂下。下装是黑色短裹裙，前腰大黑蝴蝶结，粗黄绳在腰间打结并垂到一侧，裙摆到大腿中段。黑珠项链配小金牌。室内柔光对镜全身。不是原皮多层长袍。"},
    {"id": "dolia_ocean_ruffle", "title": "朵莉亚·海洋荷叶裙", "prompt": "严格换装为《王者荣耀》朵莉亚这一版海洋荷叶短裙：青绿蓝长卷发披肩，头顶小蓝发饰。上身细肩带低领短上衣，胸前白贝壳与红海星、细珍珠点缀，颈上贝壳吊坠项圈。腰线收束后接多层荷叶迷你裙，外层薄纱从青绿蓝渐变到蓝紫，内层白色衬裙，裙摆只到大腿，珍珠散在荷叶边上。无袖，裸腿。室内素墙对镜全身。不是原皮人鱼长尾。"},
    {"id": "yinzi_white_qipao", "title": "殷紫萍·银白短旗袍", "prompt": "严格换装为《永劫无间》殷紫萍这一版银白短旗袍：银白长直发齐刘海，两侧高发包，包上白花瓣发饰、黑缎带和白流苏。上身高立领白缎旗袍，领口黑里，前中钥匙孔开窗到上胸，胸前银灰花卉云纹绣，腰侧开窗露腰。短荷叶肩饰，长白纱手套到腕。下装同料迷你裙到大腿中段，黑滚边，内层白荷叶衬裙，白色不透明过膝袜。室内柔光对镜全身。不是长袍旗袍。"},
    {"id": "ganyu_bride", "title": "甘雨·花嫁", "prompt": "严格换装为《原神》甘雨花嫁这一版：淡蓝发高髻配齐刘海和侧缕，保留麒麟角，白蕾丝花冠和薄白头纱。上身白蕾丝立领项圈，珍珠链垂到胸口，中央金铃铛；无袖露肩白胸衣收腰，肩臂接蓬松白到淡蓝薄纱荷叶袖。下装多层白到淡蓝迷你蓬裙，薄纱荷叶很多，一侧更长的淡蓝纱片。白色不透明过膝袜。室内柔光对镜全身。不是原皮深蓝金边旗袍。"},
    {"id": "yulinglong_gold_fox", "title": "玉玲珑·金狐", "prompt": "严格换装为《永劫无间》玉玲珑这一版金狐短装：浅金波浪长发中分，头顶竖起狐耳和细金额饰。上身近无袖，肤色薄纱贴身，胸口大颗青绿宝石和金纹，深色金边项圈，上臂金臂环。腰上金腰带垂几何金饰和蓝绿宝石，露出腰腹。下装香槟金薄纱短裙到大腿，高开叉，内层更浅。室内柔光对镜全身。不是原皮覆盖更多的汉服长袍。"},
    {"id": "diaochan_sanguosha", "title": "貂蝉·三国杀", "prompt": "严格换装为《三国杀》貂蝉这一版：黑长直发，头顶两只螺旋黑发包、金缎缠绕，右侧一朵大金花。颈上金项圈。上身白色短上衣只到胸下，下沿荷叶边，整段腰腹露出。外罩粉红宽袖长纱，袖长过手。下装高腰薄荷缎裙，腰上金饰，左侧高开叉到髋，裙摆拖地，赤足。室内柔光对镜全身。不是齐胸长裙汉服。"},
    {"id": "platinum_lace_gown", "title": "铂金发白蕾丝长裙", "prompt": "严格换装为铂金长直发白蕾丝长裙：头发中分，两缕垂到大腿。上身高领白蕾丝抽褶衣，短蓬袖蕾丝边，腰上同色宽带和扣。下装多层奶白长裙，薄纱外层，一侧用手掀起露出大腿。裸腿。室内素墙对镜全身。不是婚纱，也不是齐胸汉服。"},
    {"id": "xishi_cyan_qipao", "title": "西施·青绿渐变旗袍", "prompt": "严格换装为《王者荣耀》西施这一版青绿渐变旗袍：黑长直发披背。上身高立领，白盘扣，领下到腰是青绿转到象牙白，深棕滚边；右胸白花蝶贴饰；七分袖，袖口白蕾丝，袖缝棕滚边。腰下白内裙微微鼓出。外裙下摆蓝花叶纹，一侧高开叉到大腿，内层白蕾丝衬裙。颈上一串白珠。室内素墙对镜全身。不是原皮长裙水莲，也不是鹿角同人短旗袍。"},
    {"id": "yellow_bow_maid", "title": "黄结白围裙女仆", "prompt": "严格换装为深蓝底白围裙女仆装：黑长直发披背。上身深蓝底衣，白色荷叶水手领黑滚边；胸前大黄蝴蝶结，结心绿宝石。肩上白蓬袖，深蓝袖管，袖口白宽边黑条。腰上白围裙束出荷叶，裙摆白荷叶盖在深蓝裙外。白手套。室内柔光对镜全身。不是黑白法式女仆，也不是蕾姆蓝白女仆。"},
    {"id": "silver_deepv_hanfu", "title": "银紫深V广袖", "prompt": "严格换装为银紫长直发深V广袖古装：头发中分垂过腰，右侧白花步摇。上身交领极低开到胸口，领边金云纹滚边；前中一条金绣直襟从领口通到裙摆；外层粉白薄纱，腰上浅粉腰带。广袖，肩头藕粉抽褶，袖身粉白薄纱。下装粉白多层长裙，前中金绣直条。室内柔光对镜全身。不是齐胸抹胸汉服。"},
    {"id": "blue_backless_hanfu", "title": "露背蓝纱古装", "prompt": "严格换装为露背蓝纱古装：黑发高髻。构图必须是单人四分之三侧身，身体朝画面一侧转开，镜头同时看到一侧脸颊、一侧锁骨和从颈到腰的整片裸背，不要正面全身，也不要正对镜头的后脑勺。后颈一条亮蓝丝带打结，两根长带沿背沟垂下；后背无交叉带、无第二套肩带。右肩只搭一层浅蓝暗纹薄纱。袖和裙都是多层蓝纱，从冰蓝渐变到青绿再到宝蓝，广袖鼓起但不挡背，长裙拖地。人体只有两条胳膊、两只手，一只手自然垂在身侧，另一只手轻扶裙或纱，不要第三只手、不要重复手臂、不要镜子里再长出一只手。室内黑底柔光，单人侧身对镜。不是齐胸长裙，也不是白婚纱。"},
    {"id": "jixiaoman_black_gold", "title": "姬小满·黑金橙短装", "prompt": "严格换装为《王者荣耀》姬小满这一版黑金橙短装：浅橙粉到珊瑚橙长发披肩。内层白领立领。外层黑色短款宽袖外套只到胸下，金滚边，胸前金纹与金链吊坠，整段腰腹露出。宽袖外黑内亮橙金，袖口金边。腰上金腰带。髋前一块大金六角护甲板，板上有圆环纹。下装黑色短裤，裤口白边，大腿裸出。髋侧一条浅紫白辫状长尾饰。室内柔光对镜全身。不是黄睡衣家居，不是黄短裙，也不是齐胸长裙汉服。"},
    {"id": "xishi_shiyu_jiangnan", "title": "西施·诗语江南", "prompt": "严格换装为《王者荣耀》西施诗语江南这一版青绿短款：黑长发披肩，一侧编小辫，金花叶发饰。上身贴身青绿短衣，白花绣，金滚边；高立领金边；左肩一团青绿荷叶大结；内衬白底白花金边，前襟掀起露出腰腹。广袖，外层青绿金袖口，内层白袖金边。腰侧粉红流苏小囊。下装浅青绿白多层迷你蓬裙，裙摆只到大腿。白色不透明过膝袜，袜口宽米色边。室内素墙对镜全身。不是原皮长裙水莲，不是鹿角同人短旗袍，也不是青绿长旗袍。"},
    {"id": "gongsunli_lihenyan", "title": "公孙离·离恨烟", "prompt": "严格换装为《王者荣耀》公孙离离恨烟这一版：深蓝近黑长发，头顶两只尖角高发包，包上红珠和金簪，金流苏垂侧。上身贴身白缎短旗袍，高立白领金绣，胸口正中大金螺旋圆徽；一侧长白袖金边，另一侧露肩，上臂金环。腰上金蓝绳结，垂金环、流苏和金葫芦。下装极短白底，两侧高开到髋，外搭青绿、粉、深蓝多层曳地薄纱。赤足。室内柔光对镜全身。不是大乔，不是普通白旗袍，也不是西施青绿旗袍。"},
    {"id": "xishi_crop_qipao", "title": "西施·露腰短旗袍", "prompt": "严格换装为《王者荣耀》西施这一版露腰短旗袍两件套：黑长直发披肩。上身高立领青绿缎，白盘扣/蓝花结盘扣，深棕滚边，白花叶绣，只到胸下，整段腰腹露出；七分袖，袖口白蕾丝，袖身花纹棕滚边。下装高腰白荷叶短裙盖在青绿花纹迷你裙上，白蕾丝裙边，裙摆只到大腿上段。白色不透明过膝袜，袜口白蕾丝花边。室内素墙对镜全身。不是诗语江南广袖短衣，也不是鹿角同人短旗袍，也不是领下接到腰的青绿长旗袍。"},
    {"id": "ying_black_red", "title": "影·黑白红短装", "prompt": "严格换装为《王者荣耀》影这一版黑白红短装：铂金银白长直发披肩，右侧一束鲜红流苏发饰。上身黑立领短衣，银纹护领；胸口正中大红圆宝石，宝石下大红V形开窗到胸，整段腰腹露出。袖不对称：持械那侧白纱垂袖，另一侧黑垂片。下装贴身亮黑短裤，髋侧垂白色长片。右手红环刃陀螺。室内柔光对镜全身。不是齐胸汉服，也不是全包黑袍。"},
    {"id": "lusha_gold_tiara", "title": "露莎·白金短装", "prompt": "严格换装为铂金超长直发白金短装：头发拖地，齐刘海。头顶金冠，正中紫宝石，两侧金饰垂紫金缎带和细金链。上身无袖白抹胸，金交叉胸带，下沿金链；上臂金环。腰上宽金腰带，垂金片和流苏，腰腹露出。下装白短裙，两侧高开到大腿，后摆更长。金绑带高跟凉鞋，脚面紫结。室内柔光对镜全身。不是拖地白婚纱，也不是齐胸长裙汉服。"},
    {"id": "yangyang_blue_floral_swim", "title": "秧秧·蓝白碎花泳装", "prompt": "严格换装为《鸣潮》秧秧这一版蓝白碎花泳装：黑长直发，发尾染蓝，左侧银发卡。上身细吊带白底蓝花比基尼，蓝荷叶滚边，胸前蓝结。无袖，整段腰腹露出。下装同料白底蓝花薄纱裹裙，左髋打结，一侧高开。室内柔光对镜全身。不是原皮长外套白裙，也不是普通白婚纱。"},
    {"id": "cartethyia_black_bird", "title": "卡提希娅·黑裙飞鸟", "prompt": "严格换装为《鸣潮》卡提希娅这一版：浅冰蓝长发。颈上黑立领，正中金翼剑徽。上身白胸衣，蓝藤花绣，外罩黑色金环胸带。肩上白到浅蓝短披，肩头圆环纹。左上臂藕粉袖箍金环。下装黑色迷你裙，裙上白绣飞鸟，一侧蓝带和金链。赤足，银枝状脚环。室内柔光对镜全身。不是白婚纱，也不是齐胸汉服。"},
    {"id": "mint_twin_braid_hanfu", "title": "青绿双辫广袖长裙", "prompt": "严格换装为深棕双麻花辫青绿广袖长裙：两股粗辫垂在胸前。上身淡青绿立领短衣，领前白花绣，内层白襟。广袖淡青绿，褐花藤绣，袖里白。腰侧粉红花囊。下装白到淡青绿多层蓬长裙。室内素墙对镜全身。不是齐胸抹胸汉服，也不是迷你短裙。"},
]

def pick_cos_look_set(*, avoid_id: str = "") -> Dict[str, str]:
    pool = [item for item in COS_LOOK_SETS if str(item.get("id") or "") != str(avoid_id or "")]
    if not pool:
        pool = list(COS_LOOK_SETS)
    return dict(random.choice(pool))


def parse_requested_cos_camera(text: str) -> str:
    """Honor extra text: 他拍 / 自拍. Empty = random later."""
    raw = str(text or "")
    compact = re.sub(r"\s+", "", raw).lower()
    blob = compact + raw.lower()
    third_keys = (
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
        "不要对镜",
        "不拿手机",
        "不要拿手机",
        "不要手持手机",
        "candid",
        "thirdperson",
        "notselfie",
    )
    selfie_keys = ("自拍", "对镜", "镜前", "镜子前", "自己拍", "selfie", "mirror")
    if any(key in blob for key in third_keys):
        return "third"
    if any(key in blob for key in selfie_keys):
        return "selfie"
    return ""


def pick_cos_camera(*, extra_request: str = "", avoid: str = "", camera: str = "") -> str:
    forced = str(camera or "").strip() or parse_requested_cos_camera(extra_request)
    if forced in {"selfie", "third"}:
        return forced
    pool = ["selfie", "third"]
    if avoid in pool:
        pool = [item for item in pool if item != avoid] or pool
    return random.choice(pool)


def adapt_cos_outfit_for_camera(outfit: str, camera: str) -> str:
    text = str(outfit or "")
    if camera != "third":
        return text
    replacements = (
        ("对镜坐在木地板地毯上", "坐在木地板地毯上"),
        ("室内柔光对镜全身", "室内柔光半身"),
        ("室内素墙对镜全身", "室内素墙半身"),
        ("室内黑底柔光，单人侧身对镜", "室内黑底柔光，单人侧身半身"),
        ("对镜全身", "对镜半身"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return text.replace("对镜", "")


def parse_requested_legwear(text: str) -> str:
    """Honor explicit user legwear: 白丝 / 黑丝 / 光腿神器. Empty = random by pose."""
    raw = str(text or "")
    safe_tag = re.search(r"【wear:(white|black|bare)】", raw)
    if safe_tag:
        return {"white": "白丝", "black": "黑丝", "bare": "光腿神器"}[safe_tag.group(1)]
    # Prefer explicit "本次腿部穿搭：X" if already built.
    m = re.search(r"本次腿部穿搭[:：]\s*(光腿神器|白丝|黑丝)", raw)
    if m:
        return str(m.group(1) or "").strip()
    # Drop boilerplate that always lists all three options (must not force 白丝).
    cleaned = re.sub(r"若本次是白丝/黑丝[^。\n]*。?", " ", raw)
    cleaned = re.sub(r"光腿神器[、,，/]白丝[、,，/或]黑丝", " ", cleaned)
    cleaned = re.sub(r"白丝/黑丝", " ", cleaned)
    # User command extras: first match wins (看看腿 白丝).
    if re.search(r"白丝", cleaned):
        return "白丝"
    if re.search(r"黑丝", cleaned):
        return "黑丝"
    if re.search(r"光腿神器|(?<![一-龥])光腿(?![一-龥])", cleaned) or re.search(r"光腿", cleaned):
        # bare 光腿 / 光腿神器
        if re.search(r"光腿神器|光腿", cleaned):
            return "光腿神器"
    return ""


def parse_prompt_en_response(text: str) -> str:
    """Extract English prompt from translator JSON; empty means failure -> keep original."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:\w+)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        import json as _json

        payload = None
        try:
            payload = _json.loads(cleaned)
        except Exception:
            matched = re.search(r"\{(?:[^{}]|\{[^{}]*\})*\}", cleaned, flags=re.S)
            if matched:
                payload = _json.loads(matched.group(0))
        if isinstance(payload, dict):
            if payload.get("ok") is False:
                return ""
            en = payload.get(
                "en",
                payload.get("english", payload.get("prompt", payload.get("translation", ""))),
            )
            en = str(en or "").strip()
            if not en:
                return ""
            low = en.lower()
            if low.startswith("{") or "return only one json" in low or "source prompt:" in low:
                return ""
            return en
    except Exception:
        pass
    # Legacy plain-text fallback for old templates.
    plain = re.sub(r"^(?:English\s*prompt|Translation|译文|英文)[:：]\s*", "", cleaned, flags=re.I).strip()
    if not plain or plain.startswith("{"):
        return ""
    low = plain.lower()
    if ("allow" in low and "reason" in low) or "return only one json" in low or "source prompt:" in low:
        return ""
    if "faithful language conversion only" in low or "translate the image-generation prompt" in low:
        return ""
    if "translate the video-generation prompt" in low:
        return ""
    return plain


@register(PLUGIN_NAME, PLUGIN_AUTHOR, f"{PLUGIN_DISPLAY_NAME} v{PLUGIN_VERSION}", PLUGIN_VERSION)
class SelfieImagePlugin(Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        plugin_data_dir = os.path.join(str(get_astrbot_data_path()), "plugin_data")
        self.data_dir = os.path.join(plugin_data_dir, PLUGIN_NAME)
        self._migrate_legacy_data_dir(plugin_data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.config_path = os.path.join(self.data_dir, PLUGIN_CONFIG_FILENAME)
        self._migrate_legacy_config_file()
        self.usage_path = os.path.join(self.data_dir, "usage_stats.json")
        self.records_path = os.path.join(self.data_dir, "generation_records.json")
        self.tasks_path = os.path.join(self.data_dir, "generation_tasks.json")
        self.generated_dir = os.path.join(self.data_dir, "image_cache")
        os.makedirs(self.generated_dir, exist_ok=True)
        self.video_dir = os.path.join(self.generated_dir, "video")
        os.makedirs(self.video_dir, exist_ok=True)
        self._plugin_root = os.path.dirname(os.path.abspath(__file__))
        self._bundled_logo_path = os.path.join(self._plugin_root, "logo.png")
        # Pre-generated static help poster (shipped in repo; never generated at runtime).
        assets_dir = os.path.join(self._plugin_root, "assets")
        self._bundled_help_poster_path = ""
        for name in ("help_poster.png", "help_poster.jpg", "help_poster.webp"):
            candidate = os.path.join(assets_dir, name)
            if os.path.isfile(candidate):
                self._bundled_help_poster_path = candidate
                break
        if not self._bundled_help_poster_path:
            for name in ("help_poster.png", "help_poster.jpg"):
                candidate = os.path.join(self._plugin_root, name)
                if os.path.isfile(candidate):
                    self._bundled_help_poster_path = candidate
                    break

        self._native_config = config if hasattr(config, "save_config") else None
        self._native_config_path = str(getattr(config, "config_path", "") or "")
        self._config_lock = threading.RLock()
        self._usage_lock = threading.RLock()
        self._records_lock = threading.RLock()
        self._progress_lock = threading.RLock()
        self._progress_last_sent: Dict[str, float] = {}
        self._context_lock = threading.RLock()
        self._conversation_context: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
        self._context_max_messages = 40
        self._context_max_sessions = 100
        self._llm_generation_lock = threading.RLock()
        self._last_llm_generations: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._records: List[Dict[str, Any]] = self._load_records()
        self._record_seq = len(self._records)
        self._web_task_lock = threading.RLock()
        self._web_tasks: Dict[str, Dict[str, Any]] = self._load_web_tasks()
        self._web_task_seq = 0
        self._runtime_generation_tasks: Dict[str, asyncio.Task] = {}
        # 模型选择仅作用于当前会话。
        self._session_model_lock = threading.RLock()
        self._session_model_overrides: Dict[str, str] = {}
        self._last_request_at: Dict[str, float] = {}
        self._channel_health: Dict[str, Dict[str, Any]] = {}
        self._channel_health_lock = threading.RLock()
        self._send_failures: Dict[str, List[str]] = {}
        self._send_failures_lock = threading.RLock()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        native_config = self._config_object_to_dict(config)
        if not native_config and self._native_config_path:
            native_config = load_json_file(self._native_config_path)
        self.key_config = self._extract_native_key_config(native_config)
        self.raw_config = self._load_initial_config()
        self.config = AICatConfig.from_dict(self.raw_config)
        self.persona = PersonaManager(self.data_dir)
        self.presets = ImagePresetManager(self.data_dir)
        self.studio = StudioStore(self.data_dir)
        self._usage_stats = self._load_usage_stats()
        self._semaphore = asyncio.Semaphore(self.config.image_max_concurrent_tasks)
        self._video_semaphore = asyncio.Semaphore(max(1, int(getattr(self.config, "video_max_concurrent_tasks", 1) or 1)))
        # Reserve image slots per requested shot, not per whole command batch.
        self._image_batch_gate = asyncio.Semaphore(self.config.image_max_concurrent_tasks)
        self._selfie_batch_gate = self._image_batch_gate
        self.web_server = FlaskWebServer(self)
        self.dashboard_api = SelfieImageDashboardAPI(self)
        try:
            self.dashboard_api.register()
        except Exception as exc:
            logger.warning(f"[SelfieImage] 注册 AstrBot 内嵌管理页 API 失败: {exc}", exc_info=True)

        # Do not write config files during startup. If AstrBot passes an empty
        # or not-yet-populated config object, writing here would overwrite the
        # user's saved config with defaults before the plugin is usable.

    async def initialize(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._start_web_server()

    async def terminate(self) -> None:
        self.web_server.stop()

    def _migrate_legacy_data_dir(self, plugin_data_dir: str) -> None:
        if os.path.exists(self.data_dir):
            return
        legacy_dir = os.path.join(plugin_data_dir, LEGACY_PLUGIN_NAME)
        if not os.path.isdir(legacy_dir):
            return
        try:
            shutil.copytree(legacy_dir, self.data_dir)
            logger.info(f"[SelfieImage] 已迁移旧数据目录: {legacy_dir} -> {self.data_dir}")
        except Exception as exc:
            logger.warning(f"[SelfieImage] 迁移旧数据目录失败: {exc}", exc_info=True)

    def _migrate_legacy_config_file(self) -> None:
        legacy_path = os.path.join(self.data_dir, LEGACY_CONFIG_FILENAME)
        if os.path.exists(self.config_path) or not os.path.exists(legacy_path):
            return
        try:
            shutil.copy2(legacy_path, self.config_path)
            logger.info(f"[SelfieImage] 已迁移旧配置文件: {legacy_path} -> {self.config_path}")
        except Exception as exc:
            logger.warning(f"[SelfieImage] 迁移旧配置文件失败: {exc}", exc_info=True)

    def _config_object_to_dict(self, value: Any) -> Dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, Mapping):
            return {str(key): self._plain_config_value(item) for key, item in value.items()}
        for method_name in ("to_dict", "dict", "model_dump"):
            method = getattr(value, method_name, None)
            if not callable(method):
                continue
            try:
                converted = method()
                if isinstance(converted, Mapping):
                    return {str(key): self._plain_config_value(item) for key, item in converted.items()}
            except Exception:
                continue
        items = getattr(value, "items", None)
        if callable(items):
            try:
                return {str(key): self._plain_config_value(item) for key, item in items()}
            except Exception:
                pass
        keys = getattr(value, "keys", None)
        if callable(keys):
            try:
                return {str(key): self._plain_config_value(value[key]) for key in keys()}
            except Exception:
                pass
        return {}

    def _plain_config_value(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): self._plain_config_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._plain_config_value(item) for item in value]
        if isinstance(value, tuple):
            return [self._plain_config_value(item) for item in value]
        return copy.deepcopy(value)

    def _generate_web_token(self) -> str:
        return secrets.token_urlsafe(24)

    def _set_mapping_web_token(self, data: MutableMapping[str, Any], token: str) -> None:
        web = data.get("web")
        if isinstance(web, MutableMapping):
            web_value = web.get("value")
            if isinstance(web_value, MutableMapping):
                token_value = web_value.get("token")
                if isinstance(token_value, MutableMapping) and "value" in token_value:
                    token_value["value"] = token
                else:
                    web_value["token"] = token
            else:
                token_value = web.get("token")
                if isinstance(token_value, MutableMapping) and "value" in token_value:
                    token_value["value"] = token
                else:
                    web["token"] = token
        else:
            data["web"] = {"token": token}
        if "webToken" in data:
            legacy_token = data.get("webToken")
            if isinstance(legacy_token, MutableMapping) and "value" in legacy_token:
                legacy_token["value"] = token
            else:
                data["webToken"] = token

    def _try_persist_native_web_token(self, token: str) -> bool:
        persisted = False
        if self._native_config is not None:
            try:
                if isinstance(self._native_config, MutableMapping):
                    self._set_mapping_web_token(self._native_config, token)
                else:
                    native_config = self._config_object_to_dict(self._native_config)
                    self._set_mapping_web_token(native_config, token)
                    update = getattr(self._native_config, "update", None)
                    if callable(update):
                        update(native_config)
                    else:
                        for key, value in native_config.items():
                            self._native_config[key] = value
                save_config = getattr(self._native_config, "save_config", None)
                if callable(save_config):
                    save_config()
                persisted = True
            except Exception as exc:
                logger.warning(f"[SelfieImage] 随机 Web Token 写回 AstrBot 配置对象失败: {exc}", exc_info=True)

        if self._native_config_path:
            try:
                native_file_config = load_json_file(self._native_config_path)
                self._set_mapping_web_token(native_file_config, token)
                save_json_file(self._native_config_path, native_file_config)
                persisted = True
            except Exception as exc:
                logger.warning(f"[SelfieImage] 随机 Web Token 写回 AstrBot 配置文件失败: {exc}", exc_info=True)
        return persisted

    def _extract_native_key_config(self, native_config: Dict[str, Any]) -> Dict[str, Any]:
        normalized = normalize_legacy_keys(normalize_config_tree(copy.deepcopy(native_config or {})))
        web = normalized.get("web") if isinstance(normalized.get("web"), dict) else {}
        key_config = {"web": copy.deepcopy(DEFAULT_CONFIG["web"])}
        for key in ("enable", "host", "port", "token"):
            if key in web:
                key_config["web"][key] = web[key]
        if str(key_config["web"].get("token") or "").strip().lower() == DEFAULT_WEB_TOKEN:
            token = self._generate_web_token()
            key_config["web"]["token"] = token
            persisted = self._try_persist_native_web_token(token)
            suffix = "已写回 AstrBot 原生配置" if persisted else "未能自动写回配置，请手动保存"
            logger.warning(f"[SelfieImage] web.token 为默认 changeme，已自动生成随机 Web Token: {token}（{suffix}）")
        return key_config

    def _strip_web_startup_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        cleaned = copy.deepcopy(data if isinstance(data, dict) else {})
        for key in WEB_STARTUP_CONFIG_KEYS:
            cleaned.pop(key, None)
        return cleaned

    def _load_initial_config(self) -> Dict[str, Any]:
        persisted = self._strip_web_startup_config(load_json_file(self.config_path))
        source = normalize_legacy_keys(normalize_config_tree(deep_merge(DEFAULT_CONFIG, persisted)))
        source["web"] = copy.deepcopy(self.key_config["web"])
        return source

    def _persist_config(self) -> None:
        with self._config_lock:
            web_config = self._strip_web_startup_config(self.raw_config)
            save_json_file(self.config_path, web_config)

    def _apply_raw_config(self, raw: Dict[str, Any]) -> None:
        raw = self._strip_web_startup_config(raw)
        next_config = normalize_legacy_keys(normalize_config_tree(deep_merge(DEFAULT_CONFIG, raw)))
        next_config["web"] = copy.deepcopy(self.key_config["web"])
        self.raw_config = next_config
        self.config = AICatConfig.from_dict(self.raw_config)
        self._semaphore = asyncio.Semaphore(self.config.image_max_concurrent_tasks)
        self._image_batch_gate = asyncio.Semaphore(self.config.image_max_concurrent_tasks)
        self._selfie_batch_gate = self._image_batch_gate
        self._persist_config()

    def _start_web_server(self) -> None:
        if not self.config.web_enable:
            return
        try:
            self.web_server.start(self.config.web_host, self.config.web_port)
            logger.info(f"[SelfieImage] Flask Web 已启动: http://{self.config.web_host}:{self.config.web_port}")
        except Exception as exc:
            logger.error(f"[SelfieImage] Flask Web 启动失败: {exc}", exc_info=True)

    def get_config_for_web(self) -> Dict[str, Any]:
        return self._strip_web_startup_config(self.raw_config)

    def export_config_for_web(self) -> Dict[str, Any]:
        exported = redact_sensitive_data(self.get_config_for_web())
        if isinstance(exported, dict):
            exported["schema_version"] = int(exported.get("schema_version") or 2)
            exported["_export_note"] = "API key、Token、代理密码已脱敏；导入前请补回凭据。"
        return exported

    def preview_config_import(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        candidate = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        if not isinstance(candidate, dict):
            raise ValueError("配置必须是 JSON 对象")
        merged = deep_merge(self.raw_config, candidate)
        from .models import preflight_config_channels
        report = preflight_config_channels(merged)
        return {"ok": bool(report.get("ok")), "schema_version": 2, "errors": report.get("errors", []), "config": redact_sensitive_data(candidate)}

    def import_config_from_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        candidate = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        if not isinstance(candidate, dict):
            raise ValueError("配置必须是 JSON 对象")
        before = copy.deepcopy(self.raw_config)
        try:
            preview = self.preview_config_import(candidate)
            if not preview["ok"]:
                raise RuntimeError("配置预检未通过")
            return self.update_config_from_web(candidate)
        except Exception:
            self._apply_raw_config(before)
            self._persist_config()
            raise

    def list_proxies_for_web(self, *, mask_password: bool = True) -> List[Dict[str, Any]]:
        from .models import normalize_proxies_list, public_proxy_row
        rows = normalize_proxies_list((self.raw_config or {}).get("proxies") or [])
        return [public_proxy_row(row, mask_password=mask_password) for row in rows]

    def _find_proxy_row(self, proxy_id: str) -> Dict[str, Any]:
        from .models import normalize_proxies_list
        pid = str(proxy_id or "").strip()
        if not pid:
            raise ValueError("缺少代理 id")
        for row in normalize_proxies_list((self.raw_config or {}).get("proxies") or []):
            if str(row.get("id") or "") == pid:
                return row
        raise ValueError("代理不存在")

    async def test_proxy_connectivity(self, proxy_id: str = "", proxy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from .models import normalize_proxy_entry
        from .proxy import probe_proxy_connectivity
        if proxy_id:
            row = self._find_proxy_row(proxy_id)
        else:
            row = normalize_proxy_entry(proxy or {})
            if not row:
                raise ValueError("代理参数无效")
        result = await probe_proxy_connectivity(str(row.get("url") or ""))
        result["proxy_id"] = str(row.get("id") or proxy_id or "")
        result["proxy_name"] = str(row.get("name") or "")
        return result

    async def test_proxy_quality(self, proxy_id: str = "", proxy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from .models import normalize_proxy_entry
        from .proxy import probe_proxy_quality
        if proxy_id:
            row = self._find_proxy_row(proxy_id)
        else:
            row = normalize_proxy_entry(proxy or {})
            if not row:
                raise ValueError("代理参数无效")
        result = await probe_proxy_quality(str(row.get("url") or ""))
        result["proxy_id"] = str(row.get("id") or proxy_id or "")
        result["proxy_name"] = str(row.get("name") or "")
        return result

    def update_config_from_web(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._config_lock:
            patch = self._strip_web_startup_config(patch)
            if isinstance(patch, dict) and isinstance(patch.get("proxies"), list):
                # Keep existing proxy passwords when UI sends blank / masked values.
                old_by_id = {
                    str(item.get("id") or ""): item
                    for item in (self.raw_config.get("proxies") or [])
                    if isinstance(item, dict) and item.get("id")
                }
                fixed = []
                for item in patch["proxies"]:
                    if not isinstance(item, dict):
                        continue
                    row = dict(item)
                    pid = str(row.get("id") or "").strip()
                    pwd = str(row.get("password") or "")
                    if pid and old_by_id.get(pid) and pwd in {"", "******", "[REDACTED]", "«redacted»"}:
                        old_pwd = str(old_by_id[pid].get("password") or "")
                        if old_pwd and old_pwd not in {"******", "[REDACTED]"}:
                            row["password"] = old_pwd
                    fixed.append(row)
                patch["proxies"] = fixed
            merged = deep_merge(self.raw_config, patch)
            from .models import preflight_config_channels, sanitize_channels_for_save

            # Soft-fix empty-model channels before merge persist (auto-disable).
            sanitize_channels_for_save(merged)
            if isinstance(patch, dict):
                sanitize_channels_for_save(patch)
            report = preflight_config_channels(merged)
            channel_keys = (
                "image_channels",
                "audit_channels",
                "video_channels",
                "imageChannels",
                "auditChannels",
                "videoChannels",
            )
            if isinstance(patch, dict) and any(key in patch for key in channel_keys):
                if not report.get("ok"):
                    raise RuntimeError(report.get("message") or "渠道配置预检未通过")
                # Prefer sanitized tree from preflight when present.
                if isinstance(report.get("config"), dict):
                    for key in ("image_channels", "audit_channels", "video_channels"):
                        if key in report["config"]:
                            merged[key] = report["config"][key]
            self._apply_raw_config(merged)
            return self.get_config_for_web()

    def _today_key(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def _load_usage_stats(self) -> Dict[str, Any]:
        stats = load_json_file(self.usage_path)
        if stats.get("date") != self._today_key():
            return {"date": self._today_key(), "users": {}}
        if not isinstance(stats.get("users"), dict):
            stats["users"] = {}
        return stats

    def _current_usage_stats(self) -> Dict[str, Any]:
        with self._usage_lock:
            if self._usage_stats.get("date") != self._today_key():
                self._usage_stats = {"date": self._today_key(), "users": {}}
            return self._usage_stats

    def _persist_usage_stats(self) -> None:
        with self._usage_lock:
            save_json_file(self.usage_path, self._current_usage_stats())

    def _load_records(self) -> List[Dict[str, Any]]:
        data = load_json_file(self.records_path)
        items = data.get("records") if isinstance(data.get("records"), list) else []
        records = [item for item in items if isinstance(item, dict)]
        compacted = [compact_generation_record(redact_sensitive_data(item)) for item in records[:RECORD_KEEP_LIMIT]]
        return compacted

    def _persist_records(self) -> None:
        with self._records_lock:
            self._records = [
                compact_generation_record(redact_sensitive_data(item))
                for item in self._records[:RECORD_KEEP_LIMIT]
                if isinstance(item, dict)
            ]
            save_json_file(self.records_path, {"records": self._records})

    def _record_generated_images(self, event: AstrMessageEvent, count: int) -> None:
        user_id = event_user_id(event)
        if not user_id:
            return
        stats = self._current_usage_stats()
        users = stats.setdefault("users", {})
        record = users.setdefault(user_id, {"count": 0, "last_at": 0})
        record["count"] = int(record.get("count", 0)) + max(0, int(count))
        record["last_at"] = int(time.time())
        record["group_id"] = event_group_id(event)
        self._persist_usage_stats()

    def _session_key(self, event: Optional[AstrMessageEvent] = None) -> str:
        if event is None:
            return "web"
        group_id = event_group_id(event)
        user_id = event_user_id(event)
        if group_id:
            return f"group:{group_id}"
        if user_id:
            return f"private:{user_id}"
        origin = getattr(event, "unified_msg_origin", None)
        if origin:
            return f"origin:{origin}"
        return f"event:{id(event)}"

    def _context_session_key(self, event: Optional[AstrMessageEvent] = None) -> str:
        if event is None:
            return "web"
        origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        return origin or self._session_key(event)

    def _event_sender_name(self, event: Optional[AstrMessageEvent], is_bot: bool = False) -> str:
        if is_bot:
            return self._bot_display_name()
        if event is None:
            return "用户"
        for method_name in ("get_sender_name", "get_sender_nickname", "get_user_name"):
            method = getattr(event, method_name, None)
            if callable(method):
                try:
                    value = str(method() or "").strip()
                    if value:
                        return value
                except Exception:
                    continue
        sender = getattr(event, "sender", None)
        for key in ("nickname", "name", "card", "display_name"):
            value = getattr(sender, key, None)
            if value:
                return str(value).strip()
        return event_user_id(event) or "用户"

    def _event_message_id(self, event: Optional[AstrMessageEvent]) -> str:
        if event is None:
            return f"web:{time.time_ns()}"
        message_obj = getattr(event, "message_obj", None)
        for obj in (message_obj, event):
            for key in ("message_id", "msg_id", "id"):
                value = getattr(obj, key, None)
                if value:
                    return str(value)
        return f"event:{id(event)}:{time.time_ns()}"

    def _event_quotes_bot_image(self, event: Optional[AstrMessageEvent]) -> bool:
        """Whether the current event quotes a bot-authored image message.

        aiocqhttp converts a QQ reply into AstrBot ``Reply`` with ``sender_id``
        and ``chain`` populated from ``get_msg``.  That metadata lets prompt
        lookup safely use the plugin's own cached bytes instead of guessing
        from an unrelated recent image.
        """
        if event is None:
            return False
        bot_ids = {str(value).strip() for value in self._bot_account_ids(event) if str(value).strip()}
        if not bot_ids:
            return False
        visited: set[int] = set()

        def has_image(obj: Any, depth: int = 0) -> bool:
            if obj is None or depth > 10 or id(obj) in visited:
                return False
            visited.add(id(obj))
            obj_type = type(obj).__name__
            if obj_type == "Image":
                return True
            if isinstance(obj, dict):
                if str(obj.get("type") or "").lower() == "image":
                    return True
                return any(has_image(value, depth + 1) for value in obj.values())
            if isinstance(obj, (list, tuple, set)):
                return any(has_image(item, depth + 1) for item in obj)
            attrs = []
            if hasattr(obj, "__dict__"):
                attrs.extend(vars(obj).keys())
            if hasattr(obj, "__slots__"):
                attrs.extend(getattr(obj, "__slots__", []) or [])
            for key in set(attrs):
                try:
                    if has_image(getattr(obj, key), depth + 1):
                        return True
                except Exception:
                    continue
            return False

        def search(obj: Any, depth: int = 0) -> bool:
            if obj is None or depth > 10 or id(obj) in visited:
                return False
            visited.add(id(obj))
            if type(obj).__name__ == "Reply":
                sender_value = getattr(obj, "sender_id", None)
                if not sender_value:
                    sender_value = getattr(obj, "qq", "")
                sender_id = str(sender_value or "").strip()
                if sender_id in bot_ids and has_image(
                    getattr(obj, "chain", None)
                    or getattr(obj, "message", None)
                    or getattr(obj, "content", None)
                ):
                    return True
            if isinstance(obj, dict):
                return any(search(value, depth + 1) for value in obj.values())
            if isinstance(obj, (list, tuple, set)):
                return any(search(item, depth + 1) for item in obj)
            attrs = []
            if hasattr(obj, "__dict__"):
                attrs.extend(vars(obj).keys())
            if hasattr(obj, "__slots__"):
                attrs.extend(getattr(obj, "__slots__", []) or [])
            for key in set(attrs):
                try:
                    if search(getattr(obj, key), depth + 1):
                        return True
                except Exception:
                    continue
            return False

        for root in (
            getattr(event, "message_obj", None),
            getattr(event, "message", None),
            getattr(event, "raw_message", None),
        ):
            if search(root):
                return True
        return False

    def _add_context_message(
        self,
        session_key: str,
        sender_id: str,
        sender_name: str,
        content: str,
        is_bot: bool = False,
        image_sources: Optional[List[str]] = None,
        msg_id: str = "",
    ) -> None:
        key = str(session_key or "").strip() or "unknown"
        text = re.sub(r"\s+", " ", str(content or "")).strip()
        sources = [str(item).strip() for item in (image_sources or []) if str(item).strip()]
        if not text and sources:
            text = "[图片]"
        if not text and not sources:
            return
        record = {
            "msg_id": msg_id or f"{time.time_ns()}",
            "sender_id": str(sender_id or "").strip(),
            "sender_name": str(sender_name or "").strip() or ("[Bot]" if is_bot else "用户"),
            "content": text[:500],
            "is_bot": bool(is_bot),
            "has_image": bool(sources),
            "image_sources": sources[:8],
            "timestamp": time.time(),
        }
        with self._context_lock:
            messages = self._conversation_context.setdefault(key, [])
            if any(item.get("msg_id") == record["msg_id"] for item in messages[-5:]):
                return
            messages.append(record)
            if len(messages) > self._context_max_messages:
                del messages[: len(messages) - self._context_max_messages]
            self._conversation_context.move_to_end(key)
            while len(self._conversation_context) > self._context_max_sessions:
                self._conversation_context.popitem(last=False)

    def _recent_context_records(self, event: Optional[AstrMessageEvent], count: int = 12) -> List[Dict[str, Any]]:
        key = self._context_session_key(event)
        with self._context_lock:
            records = list(self._conversation_context.get(key, []))
        return records[-max(1, int(count or 1)) :]

    def _remember_llm_generation(self, event: Optional[AstrMessageEvent], kind: str, params: Dict[str, Any]) -> None:
        """保存本会话最近一次 LLM 生图请求，供重复生成使用。"""
        key = self._context_session_key(event)
        record = {
            "kind": str(kind or ""),
            "params": copy.deepcopy(params if isinstance(params, dict) else {}),
            "timestamp": time.time(),
        }
        with self._llm_generation_lock:
            self._last_llm_generations[key] = record
            self._last_llm_generations.move_to_end(key)
            while len(self._last_llm_generations) > self._context_max_sessions:
                self._last_llm_generations.popitem(last=False)

    def _last_llm_generation(self, event: Optional[AstrMessageEvent], feedback: str = "") -> Dict[str, Any]:
        """获取最近一次 LLM 生图请求，并将用户修正要求附加到内容中。"""
        key = self._context_session_key(event)
        with self._llm_generation_lock:
            record = copy.deepcopy(self._last_llm_generations.get(key) or {})
        params = record.get("params") if isinstance(record.get("params"), dict) else {}
        comment = str(feedback or "").strip()
        if comment:
            field = "action" if record.get("kind") == "selfie" else "prompt"
            original = str(params.get(field) or "").strip()
            params[field] = "\n".join(item for item in (original, f"优先修正用户反馈：{comment}") if item)
        record["params"] = params
        return record

    def _format_context_for_llm(self, event: Optional[AstrMessageEvent], count: int = 12, max_chars: int = 1400) -> str:
        lines: List[str] = []
        total = 0
        for record in reversed(self._recent_context_records(event, count)):
            sender = "[Bot]" if record.get("is_bot") else str(record.get("sender_name") or "用户")
            content = str(record.get("content") or "").strip()
            image_tag = " [含图片]" if record.get("has_image") else ""
            line = f"{sender}: {content}{image_tag}".strip()
            if not line:
                continue
            if total + len(line) > max_chars:
                break
            lines.insert(0, line)
            total += len(line) + 1
        return "\n".join(lines)

    def _extract_context_message_info(self, event: AstrMessageEvent) -> Dict[str, Any]:
        content = extract_event_text(event)
        sources = self._filter_reference_images(event, extract_image_sources_from_event(event, include_at_avatar=False))
        return {"content": content or ("[图片]" if sources else ""), "image_sources": sources}

    def _compact_followup_text(self, text: str) -> str:
        """Normalize spoken follow-up text for keyword matching."""
        compact = re.sub(r"[\s，。！？、；：,.!?;:]+", "", str(text or "").lower())
        if not compact:
            return ""
        # 「这一身/这一套/那一套」口语里常夹「一」，压成「这身/这套/那套」便于匹配。
        compact = re.sub(r"([这那])一([身套件])", r"\1\2", compact)
        return compact

    def _looks_like_context_image_reference(self, text: str) -> bool:
        compact = self._compact_followup_text(text)
        if not compact:
            return False
        keywords = [
            "上图",
            "上一张",
            "上张",
            "上一套",
            "上套",
            "刚才那张",
            "刚刚那张",
            "刚才那套",
            "刚刚那套",
            "刚发的",
            "前面那张",
            "前面那套",
            "这张",
            "这图",
            "这个图",
            "那张",
            "那图",
            "那套",
            "那一套",
            "继续改",
            "接着改",
            "在这个基础上",
            "基于这张",
            "参考这个",
            "参考刚才",
            "按刚才",
            "用刚刚",
            "用刚才",
            "用上一",
            "换回",
            "同款",
            "换成",
            "改一下",
            "修一下",
            # 穿搭跟进：无图时也要回拉用户刚发的服装参考
            "穿这个",
            "穿这",
            "穿上这个",
            "穿上这",
            "是穿这个",
            "换这个",
            "换上这个",
            "换上这",
            "这套",
            "这身",
            "这件",
            "刚刚的衣服",
            "刚才的衣服",
            "不是刚刚的衣服",
            "不是刚才的衣服",
            "一模一样的衣服",
            "复刻",
            "照着穿",
            "按这套",
            "按这身",
        ]
        english = [
            "previousimage",
            "lastimage",
            "lastphoto",
            "thisimage",
            "editthis",
            "continueediting",
            "basedonthis",
            "sameasbefore",
            "wearthis",
            "putthison",
            "sameoutfit",
            "previousoutfit",
            "lastoutfit",
        ]
        return any(keyword in compact for keyword in keywords) or any(keyword in compact for keyword in english)

    def _looks_like_clothes_followup(self, text: str) -> bool:
        """User wants outfit from a prior reference, not the bot's last selfie."""
        compact = self._compact_followup_text(text)
        if not compact:
            return False
        keys = [
            "穿这个",
            "穿这",
            "穿上这个",
            "是穿这个",
            "换这个",
            "换上这个",
            "这套",
            "这身",
            "这件",
            "那套",
            "那一套",
            "上一套",
            "上套",
            "衣服",
            "服装",
            "穿搭",
            "刚刚的衣服",
            "刚才的衣服",
            "刚刚那套",
            "刚才那套",
            "不是刚刚的衣服",
            "不是刚才的衣服",
            "一模一样的衣服",
            "复刻",
            "照着穿",
            "同款",
            "outfit",
            "wearthis",
            "clothes",
        ]
        return any(k in compact for k in keys)

    def _looks_like_edit_bot_result_followup(self, text: str) -> bool:
        """User wants to reuse/edit the bot's recent generated image/outfit."""
        compact = self._compact_followup_text(text)
        if not compact:
            return False
        keys = [
            "刚才那张",
            "刚刚那张",
            "刚才那套",
            "刚刚那套",
            "那一套",
            "那套",
            "上一张",
            "上一套",
            "上套",
            "上张图",
            "用刚刚",
            "用刚才",
            "用上一",
            "换回刚刚",
            "换回刚才",
            "换回那套",
            "换回上一",
            "继续改",
            "接着改",
            "在这个基础上",
            "基于这张",
            "这张再",
            "把刚才",
            "刚生成",
            "刚画的",
            "刚发的",
        ]
        return any(k in compact for k in keys)

    def _recent_context_image_sources(
        self,
        event: Optional[AstrMessageEvent],
        max_images: int = 4,
        *,
        prefer_user: bool = True,
        user_only: bool = False,
        bot_only: bool = False,
    ) -> List[str]:
        """Return recent image sources.

        - clothes follow-up: prefer/only user refs
        - edit-bot follow-up (「用刚刚那一套」): prefer/only bot generated refs
        """
        user_sources: List[str] = []
        bot_sources: List[str] = []
        seen_user: set = set()
        seen_bot: set = set()
        for record in reversed(self._recent_context_records(event, count=24)):
            is_bot = bool(record.get("is_bot"))
            for source in reversed(list(record.get("image_sources") or [])):
                text = str(source or "").strip()
                if not text:
                    continue
                if is_bot:
                    if text in seen_bot:
                        continue
                    seen_bot.add(text)
                    bot_sources.append(text)
                else:
                    if text in seen_user:
                        continue
                    seen_user.add(text)
                    user_sources.append(text)
        limit = max(1, int(max_images or 1))
        if bot_only:
            return bot_sources[:limit]
        if user_only:
            return user_sources[:limit]
        if prefer_user and user_sources:
            # User refs first, then bot only to fill remaining slots if needed
            out = list(user_sources)
            for text in bot_sources:
                if len(out) >= limit:
                    break
                if text not in out:
                    out.append(text)
            return out[:limit]
        if (not prefer_user) and bot_sources:
            # Bot results first (reuse previous outfit / edit last generation)
            out = list(bot_sources)
            for text in user_sources:
                if len(out) >= limit:
                    break
                if text not in out:
                    out.append(text)
            return out[:limit]
        merged = (bot_sources + user_sources) if not prefer_user else (user_sources + bot_sources)
        return merged[:limit]

    @optional_event_message_type(priority=100)
    async def on_message_record(self, event: AstrMessageEvent) -> None:
        try:
            msg = self._extract_context_message_info(event)
            sender_id = event_user_id(event)
            bot_ids = set(self._bot_account_ids(event))
            is_bot = bool(sender_id and sender_id in bot_ids)
            self._add_context_message(
                session_key=self._context_session_key(event),
                sender_id=sender_id,
                sender_name=self._event_sender_name(event, is_bot=is_bot),
                content=str(msg.get("content") or ""),
                is_bot=is_bot,
                image_sources=list(msg.get("image_sources") or []),
                msg_id=self._event_message_id(event),
            )
        except Exception as exc:
            logger.debug(f"[SelfieImage] 记录上下文失败: {exc}")
        return None

    def _access_status(self, event: AstrMessageEvent) -> Dict[str, Any]:
        user_id = event_user_id(event)
        group_id = event_group_id(event)
        status = {"user_id": user_id, "group_id": group_id, "allowed": True, "unlimited": False, "whitelist": False, "reason": ""}
        if user_id and user_id in self.config.blocked_users:
            status.update({"allowed": False, "reason": "用户黑名单"})
            return status
        if self.config.usable_users and user_id not in self.config.usable_users:
            status.update({"allowed": False, "reason": "可使用人员白名单"})
            return status
        if user_id in self.config.whitelist_users or (group_id and group_id in self.config.whitelist_groups):
            status["unlimited"] = True
            status["whitelist"] = True
        return status

    def _permission_denied_message(self, event: AstrMessageEvent) -> str:
        status = self._access_status(event)
        if status.get("allowed"):
            return ""
        if status.get("reason") == "可使用人员白名单":
            return "当前仅允许可使用人员白名单内用户使用生图功能。"
        return "你已被加入用户黑名单，无法使用生图功能。"

    def _quota_error_message(self, event: AstrMessageEvent, requested_count: int = 1) -> str:
        permission_error = self._permission_denied_message(event)
        if permission_error:
            return permission_error
        if not self.config.image_enable_daily_limit:
            return ""
        status = self._access_status(event)
        if status.get("unlimited"):
            return ""
        user_id = status.get("user_id") or ""
        used = int(self._current_usage_stats().get("users", {}).get(user_id, {}).get("count", 0))
        limit = self.config.image_daily_limit_count
        if used + max(1, requested_count) <= limit:
            return ""
        return f"今日生图次数已用完：{used}/{limit}。"

    def _rate_limit_error_message(self, event: AstrMessageEvent) -> str:
        if self._is_whitelisted(event):
            return ""
        seconds = self.config.image_rate_limit_seconds
        if seconds <= 0:
            return ""
        user_id = event_user_id(event)
        if not user_id:
            return ""
        now = time.time()
        last = self._last_request_at.get(user_id, 0)
        remain = int(seconds - (now - last))
        if remain > 0:
            return f"请求太频繁，请 {remain} 秒后再试。"
        self._last_request_at[user_id] = now
        return ""

    def _is_whitelisted(self, event: Optional[AstrMessageEvent] = None, user_id: str = "") -> bool:
        if event is None:
            return True
        status = self._access_status(event)
        return bool(status.get("whitelist") or (user_id and user_id in self.config.whitelist_users))

    def _is_audit_exempt(self, event: Optional[AstrMessageEvent] = None, user_id: str = "") -> bool:
        return bool(event is not None and self._is_whitelisted(event, user_id))

    def _validate_prompt(self, prompt: str, user_id: str = "", event: Optional[AstrMessageEvent] = None) -> str:
        if self._is_audit_exempt(event, user_id):
            return ""
        text = str(prompt or "")
        low_text = text.lower()
        for word in self.config.image_blocked_words:
            if word and str(word).lower() in low_text:
                return f"提示词包含禁用词：{word}"
        return ""

    def _bot_display_name(self) -> str:
        name = str(self.config.bot_name or "").strip()
        return name or "啊呜"

    def _compact_for_repeat_check(self, text: str) -> str:
        return re.sub(r"[\s`*_~\"'“”‘’「」『』《》()\[\]{}，。！？、；：,.!?;:\-_/\\|]+", "", str(text or "")).lower()

    def _ack_repeats_request(self, ack_message: str, user_request: str) -> bool:
        request = str(user_request or "").strip()
        if not request:
            return False
        ack_compact = self._compact_for_repeat_check(ack_message)
        request_compact = self._compact_for_repeat_check(request)
        if len(request_compact) >= 8 and request_compact in ack_compact:
            return True
        for piece in re.split(r"[\s，。！？、；：,.!?;:]+", request):
            piece_compact = self._compact_for_repeat_check(piece)
            if len(piece_compact) >= 8 and piece_compact in ack_compact:
                return True
        return False

    def _looks_like_non_chinese_ack(self, text: str) -> bool:
        raw = str(text or "")
        if not raw:
            return False
        chinese_count = len(re.findall(r"[\u4e00-\u9fff]", raw))
        latin_count = len(re.findall(r"[A-Za-z]", raw))
        if chinese_count == 0 and latin_count >= 4:
            return True
        return latin_count > chinese_count * 2 and latin_count >= 12

    def _clean_ack_message(self, ack_message: str, user_request: str) -> str:
        custom = re.sub(r"\s+", " ", str(ack_message or "")).strip()
        if not custom:
            return ""
        if self._looks_like_non_chinese_ack(custom):
            return ""
        if self._ack_repeats_request(custom, user_request):
            return ""
        stiff_markers = [
            "沿着",
            "顺着",
            "照着",
            "按这个",
            "按照这个",
            "根据你的提示",
            "根据用户",
            "用户需求",
            "用户要求",
            "提示词",
            "prompt",
            "生图",
            "工具",
            "配置",
            "正在生成",
            "开始生成",
            "为你生成",
            "帮你生成",
            "收到",
            "已收到",
        ]
        low = custom.lower()
        if any(marker.lower() in low for marker in stiff_markers):
            return ""
        return custom[:80]

    def _natural_ack_fallback(self, kind: str, count: int) -> str:
        name = self._bot_display_name()
        multi = count > 1
        if kind == "selfie":
            options = [
                f"{name}去找一下角度。",
                "等我一下，我对下光线。",
                "我换个顺眼点的构图。",
                "我先把画面收一下。",
                "稍等，我抓个自然点的瞬间。",
                "我试个更日常的角度。",
                "等我，我把镜头感压轻一点。",
                "我先看一下怎么拍更舒服。",
            ]
            if multi:
                options.extend(["我多试几个角度。", f"{name}多拍几张看看。", "我换几版构图。"])
            return random.choice(options)
        options = [
            f"{name}先把画面理一下。",
            "我先想一下构图。",
            "等我一下，我搭个画面。",
            "我试着把这个感觉做出来。",
            "稍等，我换个画面方向。",
            "我先对一下主体和光线。",
        ]
        if multi:
            options.extend(["我多试几版构图。", f"{name}多跑几张看看。"])
        return random.choice(options)

    def _natural_fail_fallback(self, kind: str = "") -> str:
        options_by_kind = {
            "legs": [
                "刚刚那版腿部比例不太顺。",
                "这次腿部构图没出来。",
                "刚才那张下半身有点乱。",
                "这版角度不太对。",
                "刚刚那张效果不行。",
            ],
            "selfie": [
                "刚刚那版不太像我。",
                "这次镜头感有点跑偏。",
                "刚才那张效果不太对。",
                "这版没出来想要的感觉。",
                "刚刚那张不太行。",
            ],
            "group": [
                "刚刚那版同框效果不太对。",
                "这次合影站位有点乱。",
                "刚才那张人物关系没处理好。",
                "这版合照没出来想要的感觉。",
            ],
            "image": [
                "刚刚那版画面不太对。",
                "这次效果没出来。",
                "刚才那张没成。",
                "这版构图有点跑偏。",
            ],
        }
        options = options_by_kind.get(kind) or options_by_kind["image"]
        return random.choice(options)

    def _selfie_ack_text(self, action: str, count: int, ack_message: str = "") -> str:
        custom = self._clean_ack_message(ack_message, action)
        if custom:
            return custom
        return self._natural_ack_fallback("selfie", count)

    def _image_ack_text(self, prompt: str, count: int, ack_message: str = "") -> str:
        custom = self._clean_ack_message(ack_message, prompt)
        if custom:
            return custom
        return self._natural_ack_fallback("image", count)

    def _progress_text_allowed(self, event: Optional[AstrMessageEvent]) -> bool:
        key = self._session_key(event)
        now = time.time()
        with self._progress_lock:
            last = self._progress_last_sent.get(key, 0.0)
            if now - last < 8:
                return False
            self._progress_last_sent[key] = now
            return True

    def _bot_account_ids(self, event: Optional[AstrMessageEvent] = None) -> List[str]:
        ids = set()
        context_keys = ("bot_id", "self_id", "account_id", "qq", "uin", "user_id")
        event_keys = ("bot_id", "self_id", "account_id", "qq", "uin")
        robot_keys = ("bot_id", "self_id", "account_id", "qq", "uin", "user_id", "id")

        sources: List[Tuple[Any, Tuple[str, ...]]] = [(self.context, context_keys)]
        if event is not None:
            sources.append((event, event_keys))
            message_obj = getattr(event, "message_obj", None)
            if message_obj is not None:
                sources.append((message_obj, event_keys))
            robot = getattr(event, "robot", None)
            if robot is not None:
                sources.append((robot, robot_keys))

        for source, keys in sources:
            for key in keys:
                value = getattr(source, key, None)
                if value:
                    ids.add(str(value).strip())

        for owner, _ in sources:
            for getter_name in ("get_bot_id", "get_self_id", "get_account_id", "get_uin"):
                getter = getattr(owner, getter_name, None)
                if callable(getter):
                    try:
                        value = getter()
                        if asyncio.iscoroutine(value):
                            continue
                        if value:
                            ids.add(str(value).strip())
                    except Exception:
                        continue
        return [item for item in ids if item]

    def _reference_source_is_bot_avatar(self, source: str, bot_ids: Iterable[str]) -> bool:
        text = str(source or "").strip()
        ids = {str(bot_id).strip() for bot_id in bot_ids if str(bot_id).strip()}
        if not text or not ids:
            return False
        try:
            parsed = urlparse(text)
        except Exception:
            return False
        if "qlogo.cn" not in parsed.netloc.lower():
            return False
        params = parse_qs(parsed.query)
        for key in ("dst_uin", "uin", "nk", "qq", "user_id"):
            for value in params.get(key, []):
                if str(value).strip() in ids:
                    return True
        target = f"{parsed.path}?{parsed.query}"
        return any(re.search(rf"(?<!\d){re.escape(bot_id)}(?!\d)", target) for bot_id in ids)

    def _filter_reference_images(self, event: Optional[AstrMessageEvent], sources: List[str]) -> List[str]:
        if not sources:
            return sources
        bot_ids = set(self._bot_account_ids(event))
        if not bot_ids:
            return sources
        filtered: List[str] = []
        for source in sources:
            if self._reference_source_is_bot_avatar(source, bot_ids):
                continue
            filtered.append(source)
        return filtered

    def _parse_audit_response(self, text: str) -> Tuple[bool, str]:
        return parse_audit_response_text(text)

    def _find_audit_target(self, label: str) -> Optional[ImageModelTarget]:
        value = str(label or "").strip()
        if not value:
            return None
        targets = self.config.get_audit_targets()
        if "/" in value:
            channel_name, model = value.split("/", 1)
            channel_name = channel_name.strip()
            model = model.strip()
            for target in targets:
                if target.channel_name == channel_name and target.model == model:
                    return target
            return None
        for target in targets:
            if target.model == value:
                return target
        return None

    def _record_image_md5(self, record: Mapping[str, Any]) -> str:
        """Return the MD5 of cached image bytes, with legacy metadata fallback."""
        value = str(record.get("md5") or "").strip().lower()
        paths = record.get("generated_image_paths")
        if isinstance(paths, list):
            for path in paths:
                try:
                    loaded = self._load_cache_image_bytes(str(path or ""))
                    if loaded:
                        # The file bytes are authoritative; stale metadata cannot create a match.
                        return hashlib.md5(loaded[0]).hexdigest()
                except Exception:
                    continue
        return value if re.fullmatch(r"[0-9a-f]{32}", value) else ""

    def _find_generation_record_by_md5(self, md5: str) -> Optional[Dict[str, Any]]:
        wanted = str(md5 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", wanted):
            return None
        with self._records_lock:
            records = [dict(item) for item in self._records if isinstance(item, dict)]
        for record in records:
            if not record.get("generated_image_paths"):
                continue
            if str(record.get("media_type") or "image").lower() == "video":
                continue
            if self._record_image_md5(record) == wanted:
                record["md5"] = wanted
                return record
            # Legacy batch rows may contain several cached paths but no per-image MD5.
            for path in record.get("generated_image_paths") or []:
                try:
                    loaded = self._load_cache_image_bytes(str(path or ""))
                    if loaded and hashlib.md5(loaded[0]).hexdigest() == wanted:
                        record["md5"] = wanted
                        return record
                except Exception:
                    continue
        return None

    async def _reverse_image_prompt_with_llm(
        self,
        event: Optional[AstrMessageEvent],
        image: bytes,
    ) -> str:
        """Use the current AstrBot chat LLM to reconstruct a prompt from an image."""
        if event is None:
            raise RuntimeError("当前会话不可用，无法调用 LLM 反推提示词。")
        instruct = (
            "请根据这张图片反推出一个适合图像生成模型使用的中文提示词。"
            "只输出提示词正文，不要解释、不要 Markdown、不要猜测图片来源。"
            "尽量描述主体、构图、视角、姿势、服装、场景、光线、风格和画面比例；"
            "看不清或无法确定的内容不要编造。"
        )
        result = await self._call_text_llm(event, instruct, timeout=30, images=[image])
        cleaned = str(result or "").strip()
        fenced = re.match(r"^```(?:\w+)?\s*([\s\S]*?)\s*```$", cleaned)
        if fenced:
            cleaned = fenced.group(1).strip()
        return cleaned[:6000]

    async def _audit_chat_via_target(self, target: ImageModelTarget, text: str, images: Optional[List[bytes]] = None) -> str:
        images = images or []
        provider_type = str(target.provider_type or "").lower()
        timeout = aiohttp.ClientTimeout(total=max(10, int(target.timeout or self.config.image_global_timeout or 180)))
        proxy = str(target.proxy or "").strip() or None
        async with aiohttp.ClientSession(trust_env=False) as session:
            if provider_type == "gemini":
                base = normalize_image_base_url(target.base_url) or "https://generativelanguage.googleapis.com"
                base = re.sub(r"/v1beta(?:/.*)?$", "", base.rstrip("/"), flags=re.I)
                model_path = target.model if target.model.startswith("models/") else f"models/{target.model}"
                url = f"{base}/v1beta/{model_path}:generateContent"
                parts: List[Dict[str, Any]] = [{"text": text}]
                for image in images:
                    parts.append({"inline_data": {"mime_type": detect_mime_by_bytes(image), "data": bytes_to_data_url(image, detect_mime_by_bytes(image)).split(",", 1)[-1]}})
                headers = {"Content-Type": "application/json", "Accept": "application/json"}
                if target.api_key:
                    headers["x-goog-api-key"] = target.api_key
                async with session.post(url, json={"contents": [{"parts": parts}]}, headers=headers, timeout=timeout, proxy=proxy) as response:
                    if response.status >= 400:
                        raise RuntimeError(f"审核接口失败: HTTP {response.status} {redact_sensitive_text(await response.text())[:200]}")
                    data = await response.json(content_type=None)
                texts: List[str] = []
                for candidate in data.get("candidates", []) if isinstance(data, dict) else []:
                    content = candidate.get("content") if isinstance(candidate, dict) else {}
                    for part in content.get("parts", []) if isinstance(content, dict) else []:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            texts.append(part["text"])
                return "\n".join(texts).strip()

            base = normalize_image_base_url(target.base_url) or "https://api.openai.com"
            url = f"{base}/v1/chat/completions"
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if target.api_key:
                headers["Authorization"] = f"Bearer {target.api_key}"
            content: Any = [{"type": "text", "text": text}] if images else text
            if images:
                for image in images:
                    content.append({"type": "image_url", "image_url": {"url": bytes_to_data_url(image, detect_mime_by_bytes(image))}})
            payload = {"model": target.model, "messages": [{"role": "user", "content": content}], "stream": False}
            async with session.post(url, json=payload, headers=headers, timeout=timeout, proxy=proxy) as response:
                if response.status >= 400:
                    raise RuntimeError(f"审核接口失败: HTTP {response.status} {redact_sensitive_text(await response.text())[:200]}")
                data = await response.json(content_type=None)
            if isinstance(data, dict):
                choices = data.get("choices")
                if isinstance(choices, list) and choices:
                    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
                    if isinstance(message, dict):
                        content = message.get("content")
                        if isinstance(content, str):
                            return content.strip()
                        if isinstance(content, list):
                            parts = [str(item.get("text") or "") for item in content if isinstance(item, dict)]
                            return "\n".join(part for part in parts if part).strip()
            return ""

    async def _audit_prompt_via_astrbot(self, event: Optional[AstrMessageEvent], text: str) -> str:
        if event is None:
            return ""
        provider_id = None
        origin = getattr(event, "unified_msg_origin", None)
        try:
            getter = getattr(self.context, "get_using_provider", None)
            if callable(getter):
                provider = getter()
                requester = getattr(provider, "text_chat", None) or getattr(provider, "request", None)
                if callable(requester):
                    response = await resolve_awaitable(requester(prompt=text))
                    return str(getattr(response, "completion_text", response) or "").strip()
        except Exception:
            pass
        try:
            getter = getattr(self.context, "get_current_chat_provider_id", None)
            if callable(getter):
                provider_id = await getter(umo=origin) if origin else await getter()
        except Exception:
            provider_id = None
        try:
            generator = getattr(self.context, "llm_generate", None)
            if callable(generator):
                kwargs = {"prompt": text}
                if provider_id:
                    kwargs["chat_provider_id"] = provider_id
                response = await generator(**kwargs)
                return str(getattr(response, "completion_text", response) or "").strip()
        except Exception:
            return ""
        return ""

    async def _audit_prompt(self, prompt: str, user_id: str = "", event: Optional[AstrMessageEvent] = None) -> Tuple[bool, str]:
        error = self._validate_prompt(prompt, user_id, event)
        if error:
            return False, error
        if self._is_audit_exempt(event, user_id):
            return True, ""
        if not self.config.image_enable_prompt_audit:
            return True, ""

        audit_prompt = self.config.image_prompt_audit_template.replace("{prompt}", str(prompt or ""))
        try:
            target = self._find_audit_target(self.config.image_prompt_audit_model)
            if target:
                text = await self._audit_chat_via_target(target, audit_prompt)
            elif event is not None:
                text = await self._audit_prompt_via_astrbot(event, audit_prompt)
            else:
                return False, "未配置可用提示词审核模型"
        except Exception as exc:
            return False, str(exc)
        return self._parse_audit_response(text)

    async def _audit_output_images(self, files: List[str], user_id: str = "", prompt: str = "", event: Optional[AstrMessageEvent] = None) -> Tuple[bool, str]:
        if self._is_audit_exempt(event, user_id):
            return True, ""
        if not self.config.image_enable_output_audit:
            return True, ""
        if not files:
            return False, "没有待审核图片"

        target = self._find_audit_target(self.config.image_output_audit_model)
        if target is None:
            return False, "未配置可用出图审核模型"
        images: List[bytes] = []
        for file_path in files:
            with open(file_path, "rb") as handle:
                images.append(handle.read())
        audit_prompt = self.config.image_output_audit_template.replace("{prompt}", str(prompt or ""))
        try:
            text = await self._audit_chat_via_target(target, audit_prompt, images=images)
        except Exception as exc:
            return False, str(exc)
        return self._parse_audit_response(text)


    def _prompt_en_needed(self, text: str, *, media: str = "image") -> bool:
        """Whether prompt EN translation is enabled and applicable for this text."""
        if media == "video":
            if not bool(getattr(self.config, "image_enable_video_prompt_en", False)):
                return False
        else:
            if not bool(getattr(self.config, "image_enable_image_prompt_en", False)):
                return False
        mode = str(getattr(self.config, "image_prompt_en_mode", "if_cjk") or "if_cjk").strip().lower()
        if mode == "always":
            return True
        # if_cjk (default): only when CJK present
        return bool(re.search(r"[\u3400-\u9fff\uf900-\ufaff]", str(text or "")))

    async def _translate_prompt_to_english(
        self,
        prompt: str,
        *,
        media: str = "image",
        event: Optional[AstrMessageEvent] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Translate generation prompt to English via audit-channel chat model.

        Returns (translated_or_original, meta). Fail-open: on error keep original.
        """
        raw = str(prompt or "").strip()
        meta: Dict[str, Any] = {"enabled": True, "applied": False, "media": media}
        if not raw:
            return raw, meta
        if not self._prompt_en_needed(raw, media=media):
            meta["skipped"] = "not_needed"
            return raw, meta
        template = (
            getattr(self.config, "image_video_prompt_en_template", "")
            if media == "video"
            else getattr(self.config, "image_image_prompt_en_template", "")
        )
        template = str(template or "").strip()
        if not template or "{prompt}" not in template:
            from .models import DEFAULT_CONFIG
            template = str(
                DEFAULT_CONFIG["image"]["video_prompt_en_template"]
                if media == "video"
                else DEFAULT_CONFIG["image"]["image_prompt_en_template"]
            )
        instruct = template.replace("{prompt}", raw)
        model_label = str(getattr(self.config, "image_prompt_en_model", "") or "").strip()
        # Prefer dedicated EN model, else prompt-audit model, else first audit target.
        try:
            target = self._find_audit_target(model_label) if model_label else None
            if target is None and self.config.image_prompt_audit_model:
                target = self._find_audit_target(self.config.image_prompt_audit_model)
            if target is None:
                targets = self.config.get_audit_targets()
                target = targets[0] if targets else None
            text = ""
            if target:
                text = await self._audit_chat_via_target(target, instruct)
                meta["model"] = target.label
            elif event is not None:
                text = await self._audit_prompt_via_astrbot(event, instruct)
                meta["model"] = "astrbot"
            else:
                meta["error"] = "no_translate_model"
                return raw, meta
            cleaned = parse_prompt_en_response(text)
            if not cleaned:
                meta["error"] = "translate_parse_failed"
                meta["raw_preview"] = redact_sensitive_text(str(text or ""))[:180]
                return raw, meta  # fail-open: keep original prompt
            meta["applied"] = True
            meta["original_len"] = len(raw)
            meta["translated_len"] = len(cleaned)
            meta["format"] = "json"
            return cleaned, meta
        except Exception as exc:
            meta["error"] = redact_sensitive_text(str(exc))[:200]
            return raw, meta  # fail-open


    def _record_task(self, record: Dict[str, Any]) -> None:
        payload = copy.deepcopy(record)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._commit_generation_records(payload)
            return
        loop.create_task(asyncio.to_thread(self._commit_generation_records, payload))

    def _commit_generation_records(self, record: Dict[str, Any]) -> None:
        # One generated image per monitor row. Batch/concurrency must not pile shots together.
        for piece in split_generation_record_images(record):
            self._commit_generation_record(piece)

    def _commit_generation_record(self, record: Dict[str, Any]) -> None:
        stale_cache_paths: List[str] = []
        response_data = record.get("response_data")
        if "attempts" not in record and isinstance(response_data, Mapping):
            record["attempts"] = list(response_data.get("attempts") or [])
        # Enrich failure fields for monitor list/detail (also backfills empty used_model).
        try:
            from .error_classify import summarize_generation_failures

            attempts = list(record.get("attempts") or [])
            if not attempts and isinstance(response_data, Mapping):
                attempts = list(response_data.get("attempts") or [])
            summary = summarize_generation_failures(
                attempts,
                fallback_error=str(record.get("error") or ""),
            )
            # Intermediate failures are useful even when final attempt succeeded.
            if summary.get("failure_reasons"):
                record["failure_reasons"] = summary["failure_reasons"]
            if record.get("success") is False:
                if summary.get("failure_reason"):
                    record["failure_reason"] = summary["failure_reason"]
                if not str(record.get("used_model") or "").strip() and summary.get("last_failed_model"):
                    record["used_model"] = summary["last_failed_model"]
                if not str(record.get("error") or "").strip() and summary.get("failure_reason"):
                    record["error"] = summary["failure_reason"]
            # success path: never promote intermediate failures into top-level failure_reason/error
            elif "failure_reason" in record and record.get("success") is True:
                record.pop("failure_reason", None)
        except Exception:
            pass
        with self._records_lock:
            record = compact_generation_record(redact_sensitive_data(dict(record)))
            self._record_seq += 1
            record.setdefault("id", f"{int(time.time() * 1000)}-{self._record_seq}")
            record["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            self._records.insert(0, record)
            evicted_records = self._records[RECORD_KEEP_LIMIT:]
            if evicted_records:
                del self._records[RECORD_KEEP_LIMIT:]
                stale_cache_paths = collect_unreferenced_record_cache_paths(evicted_records, self._records)
            self._persist_records()
        if stale_cache_paths:
            safe_delete_relative_files(self.generated_dir, stale_cache_paths)

    def get_recent_records(self, *, summary: bool = False) -> List[Dict[str, Any]]:
        with self._records_lock:
            # Avoid deepcopy of fat nested blobs on every list poll.
            records = [dict(item) if isinstance(item, dict) else item for item in self._records[:RECORD_KEEP_LIMIT]]
        if summary:
            return redact_sensitive_data(
                [summarize_record_for_list(self._enrich_record_for_web(item)) for item in records if isinstance(item, dict)]
            )
        return redact_sensitive_data([self._enrich_record_for_web(item) for item in records if isinstance(item, dict)])

    def get_generation_metrics(self) -> Dict[str, Any]:
        """Return redacted aggregate metrics from retained generation records."""
        with self._records_lock:
            records = [dict(item) for item in self._records if isinstance(item, dict)]
        status_counts: Dict[str, int] = {}
        category_counts: Dict[str, int] = {}
        model_counts: Dict[str, Dict[str, int]] = {}
        channel_counts: Dict[str, Dict[str, Any]] = {}
        elapsed_values: List[float] = []
        requested = succeeded = failed = 0
        for record in records:
            response = record.get("response_data") if isinstance(record.get("response_data"), Mapping) else {}
            status = str(record.get("status") or response.get("status") or ("succeeded" if record.get("success") else "failed"))
            status_counts[status] = status_counts.get(status, 0) + 1
            try:
                requested += max(1, int(record.get("requested_count") or response.get("requested_count") or record.get("count") or 1))
                succeeded += max(0, int(record.get("succeeded_count") or response.get("succeeded_count") or (record.get("count") if record.get("success") else 0) or 0))
                failed += max(0, int(record.get("failed_count") or response.get("failed_count") or (0 if record.get("success") else 1)))
            except (TypeError, ValueError):
                pass
            try:
                elapsed = float(record.get("elapsed_seconds") or response.get("elapsed_seconds") or 0)
                if elapsed > 0:
                    elapsed_values.append(elapsed)
            except (TypeError, ValueError):
                pass
            model = str(record.get("used_model") or response.get("used_model") or "未知").strip() or "未知"
            bucket = model_counts.setdefault(model, {"records": 0, "success": 0, "failed": 0})
            bucket["records"] += 1
            bucket["success"] += int(status == "succeeded")
            bucket["failed"] += int(status in {"failed", "partial_success"})
            attempts = list(record.get("attempts") or response.get("attempts") or [])
            attempt_channels: List[str] = []
            for attempt in attempts:
                if not isinstance(attempt, Mapping):
                    continue
                channel = str(attempt.get("channel") or attempt.get("provider_type") or "unknown").strip() or "unknown"
                attempt_channels.append(channel)
                channel_bucket = channel_counts.setdefault(channel, {
                    "attempts": 0,
                    "success": 0,
                    "failed": 0,
                    "elapsed_seconds": 0.0,
                    "error_categories": {},
                    "fallbacks": 0,
                })
                channel_bucket["attempts"] += 1
                success_attempt = bool(attempt.get("success"))
                channel_bucket["success"] += int(success_attempt)
                channel_bucket["failed"] += int(not success_attempt)
                try:
                    channel_bucket["elapsed_seconds"] += max(0.0, float(attempt.get("elapsed_seconds") or 0))
                except (TypeError, ValueError):
                    pass
                if not success_attempt:
                    category = str(attempt.get("error_category") or "unknown")
                    category_counts[category] = category_counts.get(category, 0) + 1
                    categories = channel_bucket["error_categories"]
                    categories[category] = categories.get(category, 0) + 1
            for previous, current in zip(attempt_channels, attempt_channels[1:]):
                if previous != current:
                    channel_counts[current]["fallbacks"] += 1
        elapsed_values.sort()
        def percentile(percent: float) -> float:
            if not elapsed_values:
                return 0.0
            index = min(len(elapsed_values) - 1, int(round((len(elapsed_values) - 1) * percent)))
            return round(elapsed_values[index], 2)
        return {
            "retained_records": len(records),
            "requested_images": requested,
            "succeeded_images": succeeded,
            "failed_images": failed,
            "status_counts": status_counts,
            "error_categories": category_counts,
            "models": model_counts,
            "channels": {
                channel: {
                    **values,
                    "elapsed_seconds": round(float(values["elapsed_seconds"]), 2),
                    "success_rate": round(values["success"] / values["attempts"], 4) if values["attempts"] else 0.0,
                }
                for channel, values in channel_counts.items()
            },
            "elapsed_seconds": {"p50": percentile(0.50), "p95": percentile(0.95), "max": round(max(elapsed_values), 2) if elapsed_values else 0.0},
        }

    def _composition_metadata(self, prompt: str, source: str, aspect_ratio: str, resolution: str, reference_count: int) -> Dict[str, Any]:
        text = str(prompt or "").strip()
        lowered = text.lower()
        if "看看腿" in text or "look_legs" in lowered:
            strategy = "look_legs"
        elif "全身" in text or "full body" in lowered:
            strategy = "full_body"
        elif "半身" in text or "portrait" in lowered:
            strategy = "half_body"
        else:
            strategy = "selfie_default" if "selfie" in str(source or "").lower() else "custom"
        prompt_hash = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16] if text else ""
        return {
            "strategy": strategy,
            "prompt_hash": prompt_hash,
            "aspect_ratio": str(aspect_ratio or "自动"),
            "resolution": str(resolution or "1K"),
            "reference_image_count": max(0, int(reference_count or 0)),
        }

    def get_record_for_web(self, record_id: str) -> Dict[str, Any]:
        target_id = str(record_id or "").strip()
        with self._records_lock:
            for record in self._records:
                if str(record.get("id") or "") == target_id:
                    return redact_sensitive_data(self._enrich_record_for_web(copy.deepcopy(record)))
        raise ValueError("记录不存在或已清理")

    def _enrich_record_for_web(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Backfill failure fields for monitor without rewriting disk."""
        if not isinstance(record, dict):
            return record
        try:
            from .error_classify import summarize_generation_failures

            attempts = list(record.get("attempts") or [])
            response_data = record.get("response_data")
            if not attempts and isinstance(response_data, Mapping):
                attempts = list(response_data.get("attempts") or [])
            if not attempts:
                return record
            summary = summarize_generation_failures(
                attempts,
                fallback_error=str(record.get("error") or record.get("failure_reason") or ""),
            )
            # Always surface intermediate failed attempts (including final-success retries).
            if not record.get("failure_reasons") and summary.get("failure_reasons"):
                record["failure_reasons"] = summary["failure_reasons"]
            if record.get("success") is False:
                if not str(record.get("failure_reason") or "").strip() and summary.get("failure_reason"):
                    record["failure_reason"] = summary["failure_reason"]
                if not str(record.get("used_model") or "").strip() and summary.get("last_failed_model"):
                    record["used_model"] = summary["last_failed_model"]
            elif record.get("success") is True:
                # Final success should not look like a terminal failure in the detail header.
                record.pop("failure_reason", None)
        except Exception:
            pass
        return record

    def clear_recent_records(self) -> int:
        with self._records_lock:
            count = len(self._records)
            records = copy.deepcopy(self._records)
            self._records.clear()
            self._persist_records()
        safe_delete_relative_files(self.generated_dir, collect_record_cache_paths(records))
        return count

    def _web_task_timestamp(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    def _load_web_tasks(self) -> Dict[str, Dict[str, Any]]:
        data = load_json_file(self.tasks_path)
        if not isinstance(data, dict):
            return {}
        raw_tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
        tasks: Dict[str, Dict[str, Any]] = {}
        expired_on_start = False
        for task_id, raw in raw_tasks.items():
            if not isinstance(raw, dict):
                continue
            task = copy.deepcopy(raw)
            task["task_id"] = str(task.get("task_id") or task_id)
            if task.get("status") in {"queued", "running"}:
                task["status"] = "expired"
                task["success"] = False
                task["error"] = "插件重启后未恢复该任务，请重新提交"
                task["finished_ts"] = time.time()
                task["finished_at"] = self._web_task_timestamp()
                expired_on_start = True
            tasks[task["task_id"]] = task
        if expired_on_start:
            save_json_file(self.tasks_path, {"tasks": tasks})
        return tasks

    def _persist_web_tasks_locked(self) -> None:
        path = str(getattr(self, "tasks_path", "") or "").strip()
        if not path:
            return
        save_json_file(path, {"tasks": self._web_tasks})

    def _request_fingerprint(self, payload: Mapping[str, Any], owner_session: str = "") -> str:
        """Build a short-lived dedupe key without persisting request contents."""
        image_values = list(payload.get("images") or [])
        if payload.get("image"):
            image_values.append(payload.get("image"))
        image_hashes = []
        for value in image_values:
            raw = str(value or "").encode("utf-8", "ignore")
            image_hashes.append(hashlib.sha256(raw).hexdigest()[:24])
        fields = {
            "owner_session": str(owner_session or ""),
            "media_type": str(payload.get("media_type") or "image").strip().lower(),
            "prompt": str(payload.get("prompt") or payload.get("original_prompt") or "").strip(),
            "channel": str(payload.get("channel") or "").strip(),
            "model": str(payload.get("model") or "").strip(),
            "aspect_ratio": str(payload.get("aspect_ratio") or "").strip(),
            "resolution": str(payload.get("resolution") or "").strip(),
            "duration": str(payload.get("duration") or "").strip(),
            "count": str(payload.get("count") or 1).strip(),
            "prompt_enhance": str(payload.get("prompt_enhance") or "").strip().lower(),
            "image_hashes": image_hashes,
        }
        encoded = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8", "ignore")).hexdigest()[:24]

    def _find_recent_duplicate_task_locked(self, fingerprint: str, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if not fingerprint:
            return None
        current = float(now or time.time())
        for task in self._web_tasks.values():
            if not isinstance(task, dict) or task.get("request_fingerprint") != fingerprint:
                continue
            if task.get("status") not in {"queued", "running"}:
                continue
            if current - float(task.get("created_ts") or 0) > 120:
                continue
            return copy.deepcopy(task)
        return None

    def _summarize_web_test_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_images = list(payload.get("images") or [])
        if payload.get("image"):
            raw_images.append(payload.get("image"))
        media_type = str(payload.get("media_type") or "image").strip().lower()
        if media_type == "video":
            return {
                "media_type": "video",
                "original_prompt": str(payload.get("prompt") or "").strip() or "一段自然流畅的短视频",
                "channel": str(payload.get("channel") or "").strip(),
                "model": str(payload.get("model") or "").strip(),
                "aspect_ratio": str(payload.get("aspect_ratio") or "16:9"),
                "duration": int(payload.get("duration") or self.config.video_default_duration or 5),
                "raw_reference_image_count": len(raw_images),
            }
        prompt_enhance_raw = payload.get("prompt_enhance", True)
        prompt_enhance = not (
            prompt_enhance_raw is False
            or str(prompt_enhance_raw).strip().lower() in {"false", "0", "no", "off", "关闭", "否"}
        )
        return {
            "original_prompt": str(payload.get("prompt") or "").strip() or "看着镜头自然自拍",
            "channel": str(payload.get("channel") or "").strip(),
            "model": str(payload.get("model") or "").strip(),
            "aspect_ratio": str(payload.get("aspect_ratio") or self.config.image_default_aspect_ratio or "9:16"),
            "resolution": str(payload.get("resolution") or self.config.image_default_resolution or "1K"),
            "prompt_enhance": prompt_enhance,
            "use_selfie_reference": bool(payload.get("use_selfie_reference")),
            "raw_reference_image_count": len(raw_images),
        }

    def _prune_web_tasks_locked(self) -> None:
        if len(self._web_tasks) <= 50:
            return
        finished = [
            (float(task.get("updated_ts") or 0), task_id)
            for task_id, task in self._web_tasks.items()
            if task.get("status") in {"succeeded", "partial_success", "failed", "cancelled", "expired"}
        ]
        finished.sort(key=lambda item: item[0])
        while len(self._web_tasks) > 50 and finished:
            _, task_id = finished.pop(0)
            self._web_tasks.pop(task_id, None)

    def _set_web_image_task(self, task_id: str, **fields: Any) -> None:
        with self._web_task_lock:
            task = self._web_tasks.get(task_id)
            if not task:
                return
            now = time.time()
            task.update(fields)
            task["updated_ts"] = now
            task["updated_at"] = self._web_task_timestamp()
            self._prune_web_tasks_locked()
            self._persist_web_tasks_locked()

    def get_web_image_task(self, task_id: str) -> Dict[str, Any]:
        with self._web_task_lock:
            task = self._web_tasks.get(str(task_id or "").strip())
            if not task:
                raise ValueError("任务不存在或已清理")
            data = copy.deepcopy(task)
        if data.get("status") in {"queued", "running"}:
            started = float(data.get("started_ts") or data.get("created_ts") or time.time())
            data["running_seconds"] = round(max(0.0, time.time() - started), 2)
        return redact_sensitive_data(data)

    def start_web_image_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise RuntimeError("请求体必须是 JSON 对象")
        loop = getattr(self, "loop", None)
        if loop is None or not loop.is_running():
            raise RuntimeError("AstrBot 事件循环未就绪，无法启动后台生图任务")
        payload_copy = copy.deepcopy(payload)
        media_type = str(payload_copy.get("media_type") or "image").strip().lower()
        if media_type not in {"image", "video"}:
            raise RuntimeError("media_type 必须是 image 或 video")
        self._validate_web_test_selection(payload_copy)
        force_regenerate = bool(payload_copy.get("force_regenerate") or payload_copy.get("force"))
        fingerprint = self._request_fingerprint(payload_copy, "web")
        with self._web_task_lock:
            if not force_regenerate:
                duplicate = self._find_recent_duplicate_task_locked(fingerprint)
                if duplicate:
                    duplicate["deduplicated"] = True
                    return redact_sensitive_data(duplicate)
            self._web_task_seq += 1
            task_id = f"web-{int(time.time() * 1000)}-{self._web_task_seq}"
            now = time.time()
            self._web_tasks[task_id] = {
                "task_id": task_id,
                "status": "queued",
                "success": None,
                "error": "",
                "created_ts": now,
                "updated_ts": now,
                "created_at": self._web_task_timestamp(),
                "updated_at": self._web_task_timestamp(),
                "request_data": self._summarize_web_test_payload(payload_copy),
                "result": None,
                "source": "web-video-test" if media_type == "video" else "web-test",
                "owner_session": "web",
                "cancel_requested": False,
                "request_fingerprint": fingerprint,
                "deduplicated": False,
            }
            self._prune_web_tasks_locked()
            self._persist_web_tasks_locked()
        asyncio.run_coroutine_threadsafe(self._run_web_image_task(task_id, payload_copy), loop)
        return self.get_web_image_task(task_id)

    async def _run_web_image_task(self, task_id: str, payload: Dict[str, Any]) -> None:
        self._set_web_image_task(task_id, status="running", started_ts=time.time(), started_at=self._web_task_timestamp())
        try:
            if self._task_cancel_requested(task_id):
                raise RuntimeError("任务已取消")
            media_type = str(payload.get("media_type") or "image").strip().lower()
            result = await (self.web_test_video(payload) if media_type == "video" else self.web_test_image(payload))
            result = self._normalize_generation_result(result, payload.get("count") or 1)
            result = redact_sensitive_data(result)
            if self._task_cancel_requested(task_id):
                self._set_web_image_task(
                    task_id,
                    status="cancelled",
                    success=False,
                    error="任务已取消",
                    result={"success": False, "error": "任务已取消"},
                    finished_ts=time.time(),
                    finished_at=self._web_task_timestamp(),
                )
                return
            success = bool(result.get("success"))
            error = "" if success else redact_sensitive_text(str(result.get("error") or "这次没顺好"))
            self._set_web_image_task(
                task_id,
                status=str(result.get("status") or ("succeeded" if success else "failed")),
                success=success,
                error=error,
                requested_count=result.get("requested_count", 1),
                succeeded_count=result.get("succeeded_count", 0),
                failed_count=result.get("failed_count", 0),
                result=result,
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
        except Exception as exc:
            error = redact_sensitive_text(str(exc))
            cancelled = "取消" in error
            self._set_web_image_task(
                task_id,
                status="cancelled" if cancelled else "failed",
                success=False,
                error=error,
                result={"success": False, "error": error},
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )

    def _cache_relative_path(self, path: str) -> str:
        try:
            return os.path.relpath(os.path.abspath(path), os.path.abspath(self.generated_dir))
        except Exception:
            return str(path or "")

    def _cache_absolute_path(self, rel_path: str) -> str:
        base = os.path.abspath(self.generated_dir)
        raw_path = str(rel_path or "").strip()
        if not raw_path:
            raise ValueError("图片路径不能为空")
        path = os.path.abspath(os.path.join(base, raw_path))
        if path == base or not path.startswith(base + os.sep):
            raise ValueError("非法图片路径")
        return path

    def get_cached_image_info(self, rel_path: str) -> Dict[str, Any]:
        abs_path = self._cache_absolute_path(rel_path)
        exists = os.path.exists(abs_path) and os.path.isfile(abs_path)
        mime = "image/png"
        is_image = False
        is_video = False
        if exists:
            with open(abs_path, "rb") as handle:
                head = handle.read(512)
            is_image = looks_like_image_bytes(head)
            is_video = head[4:12].startswith(b"ftyp") or abs_path.lower().endswith((".mp4", ".webm", ".mov"))
            if is_image:
                mime = detect_mime_by_bytes(head)
            elif is_video:
                mime = "video/webm" if abs_path.lower().endswith(".webm") else "video/quicktime" if abs_path.lower().endswith(".mov") else "video/mp4"
        return {
            "path": rel_path,
            "absolute_path": abs_path,
            "name": os.path.basename(abs_path),
            "exists": exists,
            "is_image": is_image,
            "is_video": is_video,
            "mime_type": mime,
        }

    def _save_cache_image(self, data: bytes, prefix: str, mime: str = "") -> str:
        path = save_image_bytes(data, self.generated_dir, prefix=prefix, mime=mime or detect_mime_by_bytes(data))
        return self._cache_relative_path(path)

    def _load_cache_image_bytes(self, rel_path: str) -> Optional[Tuple[bytes, str]]:
        try:
            info = self.get_cached_image_info(rel_path)
        except Exception:
            return None
        if not info.get("exists") or info.get("is_image") is False:
            return None
        try:
            with open(info["absolute_path"], "rb") as handle:
                data = handle.read()
        except OSError:
            return None
        if not data:
            return None
        mime = str(info.get("mime_type") or detect_mime_by_bytes(data) or "image/png")
        return data, mime

    # --- Studio / 画布 ---
    def studio_list(self) -> Dict[str, Any]:
        # Ensure default presets are seeded for picker / QQ /预设
        try:
            self.presets.load()
        except Exception:
            pass
        return {
            "sessions": self.studio.list_sessions(),
            "builtin_prompts": BUILTIN_PROMPTS,
            "templates": list_studio_templates(),
            "prompt_presets": self.list_prompt_presets_for_web(),
        }

    def list_prompt_presets_for_web(self) -> List[Dict[str, Any]]:
        """Merged builtin global + user image_presets for 画布/试画 picker."""
        merged: Dict[str, Dict[str, Any]] = {}
        for item in global_prompt_presets():
            name = str(item.get("name") or item.get("title") or "").strip()
            if not name:
                continue
            merged[name] = dict(item)
            merged[name]["name"] = name
            merged[name]["title"] = name
        try:
            for item in self.presets.list_public():
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                # user file wins on same name (may already include seeded builtins)
                row = dict(item)
                row["name"] = name
                row["title"] = name
                if name in merged and row.get("source") == "user":
                    row["source"] = "preset"
                merged[name] = row
        except Exception:
            pass
        rows = list(merged.values())
        rows.sort(key=lambda r: str(r.get("name") or ""))
        return rows

    def studio_get(self, session_id: str) -> Dict[str, Any]:
        return self.studio.get(session_id)

    def studio_create(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        title = str(payload.get("title") or "").strip()
        template = str(payload.get("template") or payload.get("template_id") or "").strip()
        use_group = payload.get("use_group_template", None)
        if isinstance(use_group, str):
            use_group = use_group.strip().lower() not in {"0", "false", "no", "off", "否"}
        tid = normalize_template_id(template, use_group_template=use_group if template == "" else None)
        session = self.studio.create(title, template=tid, use_group_template=use_group if not template else None)
        # Prefill identity/base from persona when template wants it
        graph = session.get("graph") or {}
        if graph.get("use_persona_identity") and self.persona.has_reference_image():
            ref = self.persona.get_reference_image()
            if ref and ref.get("data"):
                rel = self._save_cache_image(ref["data"], "studio", ref.get("mime_type") or "image/png")
                identity = next(
                    (
                        s
                        for s in session.get("slots") or []
                        if s.get("role") in {"identity", "base"}
                    ),
                    None,
                )
                if identity:
                    session = self.studio.set_slot_image(
                        session["id"],
                        identity["id"],
                        image_path=rel,
                        source="persona",
                        mime=str(ref.get("mime_type") or "image/png"),
                    )
        return session

    def studio_delete(self, session_id: str) -> Dict[str, Any]:
        self.studio.delete(session_id)
        return {"deleted": True, "id": session_id}

    def studio_update(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        patch = payload.get("graph") if isinstance(payload.get("graph"), dict) else payload
        if "title" in payload and "title" not in patch:
            patch = dict(patch)
            patch["title"] = payload.get("title")
        return self.studio.update_graph(session_id, patch)

    def studio_set_slot(self, session_id: str, slot_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        if payload.get("clear"):
            return self.studio.clear_slot(session_id, slot_id)
        # from existing cache path
        from_path = str(payload.get("image_path") or payload.get("path") or "").strip()
        if from_path:
            info = self.get_cached_image_info(from_path)
            if not info.get("exists") or info.get("is_image") is False:
                raise ValueError("图片不存在或不是有效图片")
            return self.studio.set_slot_image(
                session_id,
                slot_id,
                image_path=from_path,
                source=str(payload.get("source") or "record"),
                mime=str(info.get("mime_type") or ""),
                label=str(payload.get("label") or ""),
            )
        raw = payload.get("image") or payload.get("data_url") or ""
        data, mime = data_url_to_bytes(str(raw or ""))
        if not data:
            raise ValueError("请提供图片 data_url 或 image_path")
        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        if len(data) > max_bytes:
            raise ValueError(f"参考图过大，最大允许 {self.config.image_max_image_size_mb}MB")
        mime = normalize_image_mime(mime or detect_mime_by_bytes(data))
        rel = self._save_cache_image(data, "studio", mime)
        return self.studio.set_slot_image(
            session_id,
            slot_id,
            image_path=rel,
            source=str(payload.get("source") or "upload"),
            mime=mime,
            label=str(payload.get("label") or ""),
        )

    def studio_add_slot(self, session_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        return self.studio.add_slot(
            session_id,
            role=str(payload.get("role") or "extra"),
            label=str(payload.get("label") or ""),
        )

    def studio_reorder(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        order = payload.get("order") or payload.get("input_order") or []
        if not isinstance(order, list):
            raise ValueError("order 必须是数组")
        return self.studio.reorder_slots(session_id, order)

    def studio_promote(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        result_id = str(payload.get("result_id") or "").strip()
        if not result_id:
            raise ValueError("需要 result_id")
        role = str(payload.get("role") or "").strip()
        slot_id = str(payload.get("slot_id") or "").strip()
        if role:
            return self.studio.promote_result_to_role(
                session_id,
                result_id,
                role,
                create_if_missing=payload.get("create_if_missing", True) is not False,
            )
        if not slot_id:
            raise ValueError("需要 slot_id 或 role")
        return self.studio.promote_result_to_slot(session_id, result_id, slot_id)

    def studio_gallery_images(self, limit: int = 24) -> Dict[str, Any]:
        """Recent successful generated images from records for 画布「从记录选图」."""
        try:
            limit_n = max(1, min(48, int(limit or 24)))
        except Exception:
            limit_n = 24
        items: List[Dict[str, Any]] = []
        seen = set()
        for record in self.get_recent_records():
            if not record.get("success"):
                continue
            resp = record.get("response_data") if isinstance(record.get("response_data"), dict) else {}
            req = record.get("request_data") if isinstance(record.get("request_data"), dict) else {}
            paths = list(resp.get("generated_image_paths") or resp.get("image_paths") or [])
            if not paths:
                # some older shapes
                paths = list(record.get("generated_image_paths") or [])
            for path in paths:
                text = str(path or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                info = self.get_cached_image_info(text)
                if not info.get("exists"):
                    continue
                items.append(
                    {
                        "path": text,
                        "record_id": record.get("id"),
                        "created_at": record.get("created_at") or record.get("time") or "",
                        "model": resp.get("model") or req.get("model") or "",
                        "prompt": str(req.get("original_prompt") or req.get("prompt") or "")[:80],
                        "source": record.get("source") or "",
                    }
                )
                if len(items) >= limit_n:
                    return {"items": items, "count": len(items)}
        return {"items": items, "count": len(items)}

    def start_studio_run(self, session_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Queue a studio generation using current session slots + graph."""
        payload = payload if isinstance(payload, dict) else {}
        session = self.studio.get(session_id)
        # Prevent double-submit while last run still running
        last = session.get("last_run") if isinstance(session.get("last_run"), dict) else {}
        if str(last.get("status") or "") == "running":
            task_id = str(last.get("task_id") or "").strip()
            if task_id:
                try:
                    existing = self.get_web_image_task(task_id)
                    st = str(existing.get("status") or "")
                    if st in {"queued", "running"}:
                        raise RuntimeError("当前画布正在生成，请稍候或等完成后再点")
                except ValueError:
                    pass
                except RuntimeError:
                    raise
        if isinstance(payload.get("graph"), dict):
            session = self.studio.update_graph(session_id, payload["graph"])
        loop = getattr(self, "loop", None)
        if loop is None or not loop.is_running():
            raise RuntimeError("AstrBot 事件循环未就绪，无法启动画布生成")

        graph = session.get("graph") or {}
        action = build_studio_action(session)
        aspect = str(graph.get("aspect_ratio") or self.config.image_default_aspect_ratio or "9:16")
        resolution = str(graph.get("resolution") or self.config.image_default_resolution or "1K")
        try:
            count = max(1, min(4, int(graph.get("count") or 1)))
        except Exception:
            count = 1
        mode = str(graph.get("mode") or "group")
        persona_ref = self.persona.get_reference_image() if graph.get("use_persona_identity", True) else None
        raw_refs, used_slots = resolve_slot_refs_for_run(
            session,
            persona_ref=persona_ref,
            load_path_bytes=self._load_cache_image_bytes,
        )
        if mode in {"group", "selfie", "i2i"} and not raw_refs:
            raise RuntimeError("请至少放一张参考图，或先设置形象参考图")

        summary = {
            "session_id": session_id,
            "mode": mode,
            "prompt": action,
            "aspect_ratio": aspect,
            "resolution": resolution,
            "count": count,
            "used_slots": used_slots,
            "kind": "studio",
        }
        with self._web_task_lock:
            self._web_task_seq += 1
            task_id = f"web-studio-{int(time.time() * 1000)}-{self._web_task_seq}"
            now = time.time()
            self._web_tasks[task_id] = {
                "task_id": task_id,
                "status": "queued",
                "success": None,
                "error": "",
                "created_ts": now,
                "updated_ts": now,
                "created_at": self._web_task_timestamp(),
                "updated_at": self._web_task_timestamp(),
                "request_data": redact_sensitive_data(dict(summary)),
                "result": None,
                "source": "studio-run",
                "owner_session": "web",
                "cancel_requested": False,
                "studio_session_id": session_id,
            }
            self._prune_web_tasks_locked()
            self._persist_web_tasks_locked()

        self.studio.attach_run_start(session_id, task_id, summary)
        asyncio.run_coroutine_threadsafe(self._run_studio_task(task_id, session_id), loop)
        return self.get_web_image_task(task_id)

    async def _run_studio_task(self, task_id: str, session_id: str) -> None:
        self._set_web_image_task(task_id, status="running", started_ts=time.time(), started_at=self._web_task_timestamp())
        try:
            if self._task_cancel_requested(task_id):
                raise RuntimeError("任务已取消")
            session = self.studio.get(session_id)
            graph = session.get("graph") or {}
            action = build_studio_action(session)
            aspect = str(graph.get("aspect_ratio") or self.config.image_default_aspect_ratio or "9:16")
            resolution = str(graph.get("resolution") or self.config.image_default_resolution or "1K")
            try:
                count = max(1, min(4, int(graph.get("count") or 1)))
            except Exception:
                count = 1
            mode = str(graph.get("mode") or "group").strip().lower() or "group"
            persona_ref = self.persona.get_reference_image() if graph.get("use_persona_identity", True) else None
            raw_refs, _ = resolve_slot_refs_for_run(
                session,
                persona_ref=persona_ref,
                load_path_bytes=self._load_cache_image_bytes,
            )
            refs = [ImageReference(data=data, mime_type=mime) for data, mime in raw_refs]
            if mode in {"group", "selfie", "i2i"} and not refs:
                raise RuntimeError("请至少放一张参考图，或先设置形象参考图")

            if mode in {"group", "selfie"}:
                if mode == "selfie":
                    action = self._normalize_selfie_action(action, bool(refs))
                await self.persona.ensure_daily_selfie_profile(action)
                # refs already include identity first when available; do not re-prepend persona
                has_identity = bool(refs)
                extra_count = max(0, len(refs) - 1) if has_identity else len(refs)
                prompt = self.persona.build_selfie_prompt(
                    action=action,
                    bot_name=self.config.bot_name,
                    personality=self.config.personality,
                    has_reference_image=has_identity,
                    extra_reference_count=extra_count,
                )
                prompt_en_meta: Dict[str, Any] = {"enabled": False, "applied": False, "scope": "user_text_only"}
                if self._prompt_en_needed(action, media="image"):
                    from .prompt_templates import build_selfie_builtin_prompt, extract_user_prompt

                    user_text = extract_user_prompt(action)
                    translated_user = ""
                    if user_text:
                        translated_user, prompt_en_meta = await self._translate_prompt_to_english(
                            user_text, media="image", event=None
                        )
                        if not prompt_en_meta.get("applied"):
                            translated_user = ""
                    else:
                        prompt_en_meta.update({"enabled": True, "applied": True, "scope": "builtin_only"})
                    if prompt_en_meta.get("applied"):
                        prompt = build_selfie_builtin_prompt(
                            action,
                            language="en",
                            has_reference_image=has_identity,
                            extra_reference_count=extra_count,
                            appearance_type=self.persona.get_appearance_type(),
                            user_text=translated_user,
                        )
            else:
                user_prompt = action
                prompt_en_meta = {"enabled": False, "applied": False, "scope": "user_text_only"}
                if self._prompt_en_needed(user_prompt, media="image"):
                    translated, prompt_en_meta = await self._translate_prompt_to_english(
                        user_prompt, media="image", event=None
                    )
                    if prompt_en_meta.get("applied") and translated:
                        user_prompt = translated
                prompt = build_prompt_with_reference_instruction(
                    user_prompt,
                    refs,
                    language="en" if self.config.image_enable_image_prompt_en else "zh",
                )

            all_paths: List[str] = []
            last_error = ""
            used_model = ""
            last_result: Dict[str, Any] = {}
            for _ in range(max(1, count)):
                if self._task_cancel_requested(task_id):
                    raise RuntimeError("任务已取消")
                result = await self._run_image_generation(
                    prompt=prompt,
                    aspect_ratio=aspect,
                    resolution=resolution,
                    refs=refs,
                    source="studio-run",
                    original_prompt=action,
                    event=None,
                    prompt_en_meta=prompt_en_meta,
                )
                last_result = result if isinstance(result, dict) else {}
                if not last_result.get("success"):
                    last_error = str(last_result.get("error") or "生成失败")
                    break
                used_model = str(last_result.get("used_model") or used_model)
                for path in last_result.get("image_paths") or last_result.get("generated_image_paths") or []:
                    text = str(path or "").strip()
                    if text:
                        all_paths.append(text)

            success = bool(all_paths) and not last_error
            error = "" if success else (last_error or "生成失败")
            self.studio.attach_run_finish(
                session_id,
                task_id,
                success=success,
                error=error,
                result_paths=all_paths,
                used_model=used_model,
            )
            result_payload = {
                "success": success,
                "error": error,
                "image_paths": all_paths,
                "generated_image_paths": all_paths,
                "used_model": used_model,
                "session_id": session_id,
                "elapsed_seconds": last_result.get("elapsed_seconds"),
            }
            self._set_web_image_task(
                task_id,
                status="succeeded" if success else "failed",
                success=success,
                error=error,
                result=redact_sensitive_data(result_payload),
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
        except Exception as exc:
            error = redact_sensitive_text(str(exc))
            try:
                self.studio.attach_run_finish(session_id, task_id, success=False, error=error, result_paths=[])
            except Exception:
                pass
            cancelled = "取消" in error
            self._set_web_image_task(
                task_id,
                status="cancelled" if cancelled else "failed",
                success=False,
                error=error,
                result={"success": False, "error": error, "session_id": session_id},
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )

    def _save_reference_images_to_cache(self, refs: List[ImageReference]) -> List[str]:
        paths: List[str] = []
        for ref in refs:
            if ref.data:
                paths.append(self._save_cache_image(ref.data, "request", ref.mime_type))
        return paths

    def _cache_size_bytes(self) -> int:
        total = 0
        for root, _, files in os.walk(self.generated_dir):
            for name in files:
                path = os.path.join(root, name)
                try:
                    total += os.path.getsize(path)
                except OSError:
                    pass
        return total

    def _cleanup_image_cache_if_needed(self, protected_paths: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        limit = max(10, int(self.config.image_cache_limit_mb or 100)) * 1024 * 1024
        total = self._cache_size_bytes()
        deleted: List[str] = []
        if total <= limit:
            return {"limit_bytes": limit, "total_bytes": total, "deleted": deleted}
        with self._records_lock:
            referenced_paths = collect_record_cache_paths(self._records)
        candidates = collect_cache_cleanup_candidates(self.generated_dir, protected_paths, referenced_paths)
        for path in candidates:
            try:
                size = os.path.getsize(path)
                os.remove(path)
                deleted.append(self._cache_relative_path(path))
                total = max(0, total - size)
            except OSError:
                pass
            if total <= limit:
                break
        return {"limit_bytes": limit, "total_bytes": total, "deleted": deleted}

    def get_cache_cleanup_preview(self, protected_paths: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        """Return a dry-run cache cleanup plan without deleting files."""
        limit = max(10, int(self.config.image_cache_limit_mb or 100)) * 1024 * 1024
        total = self._cache_size_bytes()
        with self._records_lock:
            referenced_paths = collect_record_cache_paths(self._records)
        candidates = collect_cache_cleanup_candidates(self.generated_dir, protected_paths, referenced_paths)
        planned: List[Dict[str, Any]] = []
        remaining = total
        if total > limit:
            for path in candidates:
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                planned.append({"path": self._cache_relative_path(path), "size_bytes": size})
                remaining = max(0, remaining - size)
                if remaining <= limit:
                    break
        return {
            "limit_bytes": limit,
            "total_bytes": total,
            "would_delete_bytes": total - remaining,
            "remaining_bytes": remaining,
            "would_delete": planned,
        }

    def clear_channel_health(self, channel: str = "") -> Dict[str, Any]:
        with self._channel_health_lock:
            if channel:
                self._channel_health.pop(str(channel).strip(), None)
            else:
                self._channel_health.clear()
            return self.get_channel_health()

    def get_channel_health(self) -> Dict[str, Any]:
        now = time.time()
        with self._channel_health_lock:
            return {
                name: {**dict(state), "cooldown_remaining": round(max(0.0, float(state.get("cooldown_until") or 0) - now), 2)}
                for name, state in self._channel_health.items()
            }

    def _channel_is_healthy(self, channel: str) -> bool:
        with self._channel_health_lock:
            state = self._channel_health.get(str(channel).strip()) or {}
            return float(state.get("cooldown_until") or 0) <= time.time()

    def _record_channel_health(self, attempts: Iterable[Mapping[str, Any]]) -> None:
        now = time.time()
        for attempt in attempts:
            channel = str(attempt.get("channel") or "").strip()
            if not channel:
                continue
            category = str(attempt.get("error_category") or "").strip()
            success = bool(attempt.get("success"))
            if not success and category not in {"network", "server", "timeout_create", "timeout_poll"}:
                continue
            with self._channel_health_lock:
                state = self._channel_health.setdefault(channel, {"consecutive_failures": 0, "last_error_category": ""})
                if success:
                    state["consecutive_failures"] = 0
                    state["cooldown_until"] = 0
                    state["last_success_ts"] = now
                    continue
                if category not in {"network", "server", "timeout_create", "timeout_poll"}:
                    continue
                state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
                state["last_error_category"] = category
                state["last_error_ts"] = now
                if state["consecutive_failures"] >= 3:
                    state["cooldown_until"] = now + 60

    def _source_context(self, event: Optional[AstrMessageEvent], source: str, user_id: str = "") -> Dict[str, Any]:
        uid = event_user_id(event) if event is not None else str(user_id or "")
        gid = event_group_id(event) if event is not None else ""
        if gid and uid:
            label = f"群 {gid} / QQ {uid}"
        elif uid:
            label = f"QQ {uid}"
        else:
            label = "Web"
        return {
            "source": source,
            "source_label": label,
            "group_id": gid,
            "user_id": uid,
            "chat_type": "group" if gid else ("private" if uid else "web"),
        }

    def _normalize_count(self, count: Any) -> int:
        try:
            value = int(float(str(count).strip()))
        except Exception:
            value = 1
        return max(1, min(self.config.image_max_batch_count, value))

    def _parse_count_token(self, token: str) -> int:
        text = str(token or "").strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        if not text:
            return 0
        match = re.fullmatch(r"(\d{1,2})(?:张|次|幅)?", text)
        if match:
            value = int(match.group(1))
            return value if value > 0 else 0

        chinese_digits = {
            "一": 1,
            "二": 2,
            "两": 2,
            "俩": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
        }
        chinese = re.fullmatch(r"([一二两俩三四五六七八九十]{1,3})(?:张|次|幅)?", text)
        if not chinese:
            return 0
        value_text = chinese.group(1)
        if value_text == "十":
            return 10
        if "十" in value_text:
            before, _, after = value_text.partition("十")
            tens = chinese_digits.get(before, 1) if before else 1
            ones = chinese_digits.get(after, 0) if after else 0
            value = tens * 10 + ones
            return value if value > 0 else 0
        return chinese_digits.get(value_text, 0)

    def _command_tokens_for_count(self, text: str) -> List[str]:
        raw_tokens = re.sub(r"\s+", " ", str(text or "").strip()).split()
        tokens: List[str] = []
        for index, token in enumerate(raw_tokens):
            if index < 2:
                parts = [part.strip() for part in re.split(r"[\/／]+", token) if part.strip()]
                count_like_parts = sum(1 for part in parts if self._parse_count_token(part))
                if 1 < len(parts) <= 2 and count_like_parts == 1:
                    tokens.extend(parts)
                    continue
            tokens.append(token)
        return tokens

    def _extract_command_count(self, text: str) -> Tuple[str, int]:
        tokens = self._command_tokens_for_count(text)
        if not tokens:
            return "", 1
        for index in (0, 1):
            if index >= len(tokens):
                continue
            count = self._parse_count_token(tokens[index])
            if count:
                remaining = [token for pos, token in enumerate(tokens) if pos != index]
                return " ".join(remaining).strip(), self._normalize_count(count)
        return " ".join(tokens).strip(), 1

    def _parse_prompt_options(self, text: str, aspect_ratio: str = "", resolution: str = "") -> Tuple[str, str, str]:
        prompt = str(text or "").strip()
        aspect = str(aspect_ratio or self.config.image_default_aspect_ratio or "9:16").strip() or "9:16"
        resol = str(resolution or self.config.image_default_resolution or "1K").strip() or "1K"
        matches = list(re.finditer(r"--([a-zA-Z0-9_\-]+)(?:[=\s]+([^\s]+))?", prompt))
        for match in reversed(matches):
            key = match.group(1).lower().replace("-", "_")
            value = str(match.group(2) or "").strip()
            if key in {"ar", "aspect", "aspect_ratio", "ratio"} and value:
                aspect = value
                prompt = prompt[: match.start()] + prompt[match.end() :]
            elif key in {"resolution", "res", "quality"} and value:
                resol = value
                prompt = prompt[: match.start()] + prompt[match.end() :]
            elif key == "size" and value:
                if "2048" in value or value.upper() == "2K":
                    resol = "2K"
                elif "4096" in value or value.upper() == "4K":
                    resol = "4K"
                prompt = prompt[: match.start()] + prompt[match.end() :]
        return re.sub(r"\s+", " ", prompt).strip(), aspect, resol

    def _resolve_image_preset(self, prompt: str, aspect_ratio: str = "", resolution: str = "") -> Tuple[str, str, str, str, str]:
        cleaned_prompt, aspect, resol = self._parse_prompt_options(prompt, aspect_ratio, resolution)
        resolved = self.presets.resolve(cleaned_prompt)
        preset_name = str(resolved.get("preset_name") or "").strip()

        if preset_name:
            cleaned_prompt = str(resolved.get("prompt") or cleaned_prompt).strip()
            default_aspect = str(self.config.image_default_aspect_ratio or "9:16").strip() or "9:16"
            default_resolution = str(self.config.image_default_resolution or "1K").strip() or "1K"
            preset_aspect = str(resolved.get("aspect_ratio") or "").strip()
            preset_resolution = str(resolved.get("resolution") or "").strip()
            if preset_aspect and aspect == default_aspect:
                aspect = preset_aspect
            if preset_resolution and resol == default_resolution:
                resol = preset_resolution

        return cleaned_prompt, aspect, resol, preset_name, str(resolved.get("description") or "").strip()

    def _expand_user_text_with_preset(self, raw_text: str) -> Tuple[str, str, str, str]:
        """Resolve presets against raw user words before action wrappers.

        Commands like /自拍 捧脸 previously wrapped text into a long random action,
        so preset matching (name must be at the start) never fired.
        """
        text = str(raw_text or "").strip()
        if not text:
            return "", "", "", ""
        # Ensure seeded defaults are loaded (no-op if already present).
        try:
            self.presets.load()
        except Exception:
            pass
        expanded, aspect, resolution, preset_name, _ = self._resolve_image_preset(text)
        return str(expanded or text).strip(), aspect, resolution, preset_name

    def _normalize_preset_input(self, text: str) -> str:
        return str(text or "").strip().replace("\r", " ").replace("\n", " ")

    def _split_preset_command(self, text: str) -> Tuple[str, str]:
        value = self._normalize_preset_input(text)
        if not value:
            return "", ""
        if " " in value:
            head, tail = value.split(" ", 1)
            return head.strip(), tail.strip()
        return value, ""

    async def _event_reference_images_with_stats(
        self,
        event: AstrMessageEvent,
        include_at_avatar: bool = False,
        context_hint: str = "",
        allow_context_fallback: bool = False,
        include_persona: bool = False,
        extra_sources: Optional[List[str]] = None,
        include_image_alternates: bool = False,
    ) -> Tuple[List[ImageReference], int, int]:
        """Collect event references via unified ReferenceCollector (target 11)."""
        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        persona_path = ""
        if include_persona:
            if self.persona.has_reference_image():
                persona_path = str(self.persona.get_reference_path() or "")
            elif bool(getattr(self.config, "image_use_logo_when_no_persona", True)):
                logo = str(getattr(self, "_bundled_logo_path", "") or "")
                if logo and os.path.isfile(logo):
                    persona_path = logo
        hint = str(context_hint or extract_event_text(event) or "")
        # Clothes / "wear this" → user prior outfit ref.
        # "用刚刚那一套/上一套" → bot's recent generated image first.
        edit_bot = self._looks_like_edit_bot_result_followup(hint)
        clothes = self._looks_like_clothes_followup(hint)
        user_only = clothes and not edit_bot
        bot_only = edit_bot and not clothes
        prefer_user = not edit_bot
        context_sources = self._recent_context_image_sources(
            event,
            prefer_user=prefer_user,
            user_only=user_only,
            bot_only=bot_only,
        )
        collector = ReferenceCollector(
            max_bytes=max_bytes,
            bot_ids=self._bot_account_ids(event),
            persona_path=persona_path,
            context_sources=context_sources,
            extra_sources=extra_sources or [],
            include_at_avatar=include_at_avatar,
            include_persona=include_persona,
            allow_context_fallback=allow_context_fallback,
            context_hint=hint,
            looks_like_context_ref=self._looks_like_context_image_reference,
            include_image_alternates=include_image_alternates,
        )
        async with aiohttp.ClientSession(trust_env=False) as session:
            collected = await collector.collect(event, session)
        # Default return keeps historical semantics: object refs only (persona separate).
        refs = collected.for_draw(include_persona=include_persona)
        if collected.failed_count and not refs:
            logger.warning(
                f"[SelfieImage] 参考图读取失败或超时: {collected.failed_count}/{collected.source_count}"
            )
        return refs, collected.source_count, collected.failed_count

    async def _collect_event_references(
        self,
        event: AstrMessageEvent,
        *,
        include_at_avatar: bool = False,
        context_hint: str = "",
        allow_context_fallback: bool = False,
        include_persona: bool = False,
        extra_sources: Optional[List[str]] = None,
        include_image_alternates: bool = False,
    ):
        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        persona_path = ""
        if include_persona:
            if self.persona.has_reference_image():
                persona_path = str(self.persona.get_reference_path() or "")
            elif bool(getattr(self.config, "image_use_logo_when_no_persona", True)):
                logo = str(getattr(self, "_bundled_logo_path", "") or "")
                if logo and os.path.isfile(logo):
                    persona_path = logo
        hint = str(context_hint or extract_event_text(event) or "")
        edit_bot = self._looks_like_edit_bot_result_followup(hint)
        clothes = self._looks_like_clothes_followup(hint)
        user_only = clothes and not edit_bot
        bot_only = edit_bot and not clothes
        prefer_user = not edit_bot
        collector = ReferenceCollector(
            max_bytes=max_bytes,
            bot_ids=self._bot_account_ids(event),
            persona_path=persona_path,
            context_sources=self._recent_context_image_sources(
                event,
                prefer_user=prefer_user,
                user_only=user_only,
                bot_only=bot_only,
            ),
            extra_sources=extra_sources or [],
            include_at_avatar=include_at_avatar,
            include_persona=include_persona,
            allow_context_fallback=allow_context_fallback,
            context_hint=hint,
            looks_like_context_ref=self._looks_like_context_image_reference,
            include_image_alternates=include_image_alternates,
        )
        async with aiohttp.ClientSession(trust_env=False) as session:
            return await collector.collect(event, session)

    async def _event_reference_images(
        self,
        event: AstrMessageEvent,
        include_at_avatar: bool = False,
        context_hint: str = "",
        allow_context_fallback: bool = False,
        include_persona: bool = False,
        extra_sources: Optional[List[str]] = None,
        include_image_alternates: bool = False,
    ) -> List[ImageReference]:
        refs, _, _ = await self._event_reference_images_with_stats(
            event,
            include_at_avatar=include_at_avatar,
            context_hint=context_hint,
            allow_context_fallback=allow_context_fallback,
            include_persona=include_persona,
            extra_sources=extra_sources,
            include_image_alternates=include_image_alternates,
        )
        return refs

    def _create_image_component(self, file_path: str) -> Any:
        path = os.path.abspath(file_path)
        if hasattr(Image, "fromFileSystem"):
            return Image.fromFileSystem(path)
        if hasattr(Image, "from_file_system"):
            return Image.from_file_system(path)
        return Image(file=path)

    def _create_video_component(self, file_path: str) -> Any:
        path = os.path.abspath(file_path)
        if Video is None:
            # Fallback: some platforms accept file path as plain text; prefer Image-like file field if present.
            return {"type": "video", "file": path}
        if hasattr(Video, "fromFileSystem"):
            return Video.fromFileSystem(path)
        if hasattr(Video, "from_file_system"):
            return Video.from_file_system(path)
        if hasattr(Video, "fromURL") and str(file_path).startswith("http"):
            return Video.fromURL(file_path)
        try:
            return Video(file=path)
        except Exception:
            return Video(path) if callable(Video) else {"type": "video", "file": path}

    async def _send_generated_video(self, event: AstrMessageEvent, file_path: str, caption: str = "") -> None:
        components: List[Any] = []
        if caption:
            try:
                from astrbot.api.message_components import Plain  # type: ignore
            except Exception:
                try:
                    from astrbot.api.event.components import Plain  # type: ignore
                except Exception:
                    Plain = None  # type: ignore
            if Plain is not None:
                components.append(Plain(caption) if not callable(Plain) or True else Plain(text=caption))
        components.append(self._create_video_component(file_path))
        try:
            await event.send(event.chain_result(components))
        except Exception:
            # Some adapters dislike mixed chains; send separately.
            if caption:
                try:
                    await event.send(event.plain_result(caption))
                except Exception:
                    pass
            await event.send(event.chain_result([self._create_video_component(file_path)]))

    def _persona_identity_reference(self) -> Optional[ImageReference]:
        """形象参考：优先用户上传；无图时按开关回退 logo，或返回 None 走人设文案。"""
        persona_ref = self.persona.get_reference_image()
        if persona_ref:
            return ImageReference(data=persona_ref["data"], mime_type=persona_ref["mime_type"])
        if not bool(getattr(self.config, "image_use_logo_when_no_persona", True)):
            return None
        logo = str(getattr(self, "_bundled_logo_path", "") or "")
        if not logo or not os.path.isfile(logo):
            return None
        try:
            with open(logo, "rb") as handle:
                data = handle.read()
            if not data:
                return None
            return ImageReference(data=data, mime_type=detect_mime_by_bytes(data) or "image/png")
        except OSError:
            return None

    def _persona_auxiliary_references(self, action: str = "") -> List[ImageReference]:
        """Load auxiliary identity images; group photos intentionally use primary only."""
        intent = self.persona.analyze_selfie_intent(action)
        if intent.is_group_photo:
            return []
        return [
            ImageReference(data=item["data"], mime_type=item["mime_type"])
            for item in self.persona.get_auxiliary_reference_images()
            if item.get("data")
        ]

    def _video_persona_reference(self) -> Optional[ImageReference]:
        """将当前形象图作为视频首帧参考。"""
        return self._persona_identity_reference()

    async def _send_generated_images(self, event: AstrMessageEvent, files: Iterable[str]) -> int:
        sent = 0
        for file_path in files:
            try:
                await event.send(event.chain_result([self._create_image_component(file_path)]))
            except Exception as exc:
                logger.warning("[SelfieImage] image send failed: %s", redact_sensitive_text(str(exc)))
                owner = self._session_key(event)
                with self._send_failures_lock:
                    self._send_failures.setdefault(owner, []).append(self._cache_relative_path(file_path))
                continue
            self._record_bot_image_context(event, [file_path])
            sent += 1
            await asyncio.sleep(0.4)
        return sent

    async def retry_failed_images(self, event: AstrMessageEvent) -> Dict[str, Any]:
        owner = self._session_key(event)
        with self._send_failures_lock:
            paths = list(self._send_failures.get(owner) or [])
        sent = 0
        remaining = []
        for rel_path in paths:
            try:
                abs_path = self._cache_absolute_path(rel_path)
            except Exception:
                continue
            if not os.path.isfile(abs_path):
                continue
            if await self._send_generated_images(event, [abs_path]):
                sent += 1
            else:
                remaining.append(rel_path)
        with self._send_failures_lock:
            if remaining:
                self._send_failures[owner] = remaining
            else:
                self._send_failures.pop(owner, None)
        return {"success": sent > 0, "sent": sent, "remaining": len(remaining)}

    async def _send_progress_text(self, event: AstrMessageEvent, text: str) -> None:
        if not self._progress_text_allowed(event):
            return
        try:
            await event.send(event.plain_result(text))
            self._record_bot_text_context(event, text)
        except Exception as exc:
            logger.warning(f"[SelfieImage] 发送进度消息失败: {exc}")

    def _build_progress_text(self, kind: str, user_request: str, count: int, ack_message: str = "") -> str:
        if kind == "selfie":
            return self._selfie_ack_text(user_request, count, ack_message)
        return self._image_ack_text(user_request, count, ack_message)

    async def _call_text_llm(
        self,
        event: Optional[AstrMessageEvent],
        prompt: str,
        timeout: int = 8,
        images: Optional[List[bytes]] = None,
    ) -> str:
        if event is None:
            return ""
        image_urls = [
            bytes_to_data_url(image, detect_mime_by_bytes(image))
            for image in (images or [])
            if image
        ]

        async def request() -> str:
            origin = getattr(event, "unified_msg_origin", None)
            provider_id = None
            try:
                getter = getattr(self.context, "get_using_provider", None)
                if callable(getter):
                    provider = getter()
                    requester = getattr(provider, "text_chat", None) or getattr(provider, "request", None)
                    if callable(requester):
                        kwargs: Dict[str, Any] = {"prompt": prompt}
                        if image_urls:
                            kwargs["image_urls"] = image_urls
                        response = requester(**kwargs)
                        if asyncio.iscoroutine(response):
                            response = await response
                        return str(getattr(response, "completion_text", response) or "").strip()
            except Exception:
                pass
            try:
                getter = getattr(self.context, "get_current_chat_provider_id", None)
                if callable(getter):
                    provider_id = await getter(umo=origin) if origin else await getter()
            except Exception:
                provider_id = None
            try:
                generator = getattr(self.context, "llm_generate", None)
                if callable(generator):
                    kwargs = {"prompt": prompt}
                    if provider_id:
                        kwargs["chat_provider_id"] = provider_id
                    if image_urls:
                        kwargs["image_urls"] = image_urls
                    response = await generator(**kwargs)
                    return str(getattr(response, "completion_text", response) or "").strip()
            except Exception:
                return ""
            return ""

        try:
            return await asyncio.wait_for(request(), timeout=max(2, int(timeout or 8)))
        except Exception:
            return ""

    def _strip_llm_short_reply(self, text: str) -> str:
        value = str(text or "").strip()
        if not value:
            return ""
        fenced = re.match(r"^```(?:\w+)?\s*([\s\S]*?)\s*```$", value)
        if fenced:
            value = fenced.group(1).strip()
        value = re.sub(r"<\s*(?:think|analysis)\b[^>]*>.*?<\s*/\s*(?:think|analysis)\s*>", "", value, flags=re.I | re.S)
        value = value.replace("\r", " ").replace("\n", " ")
        value = re.sub(r"^\s*(?:回复|答复|assistant|bot)\s*[：:]\s*", "", value, flags=re.I)
        value = value.strip(" 「」『』“”\"'`")
        return re.sub(r"\s+", " ", value).strip()

    def _build_ack_prompt_for_llm(self, event: AstrMessageEvent, kind: str, user_request: str, count: int) -> str:
        name = self._bot_display_name()
        context_text = self._format_context_for_llm(event, count=12, max_chars=1400)
        request = str(user_request or "").strip()
        kind_text = "自拍/拍照" if kind == "selfie" else "图片请求"
        count_text = "多张" if count > 1 else "一张"
        return "\n".join(
            [
                f"你是{name}，正在和用户自然聊天。",
                f"用户刚通过指令发起了{kind_text}，数量：{count_text}。",
                "请只输出一句简体中文短回复，像正在聊天时随口接一句。",
                "要求：10-32 个汉字；结合最近上下文；不要复述用户提示词；不要解释；不要列点。",
                "禁止出现：生成、绘制、渲染、工具、提示词、配置、审核、任务、处理中、已收到、开始、为你。",
                "不要套用人设、语气词或氛围设定；只按当前对话自然接一句。",
                "如果是自拍/拍照，可以表现为找角度、看光线、调整镜头；如果是普通图片，可以表现为整理画面或构图。",
                f"最近对话：\n{context_text}" if context_text else "最近对话：无",
                f"当前请求：{request[:300]}",
                "只输出这一句回复：",
            ]
        )

    async def _build_contextual_progress_text(
        self,
        event: AstrMessageEvent,
        kind: str,
        user_request: str,
        count: int,
        ack_message: str = "",
    ) -> str:
        fallback = self._build_progress_text(kind, user_request, count, ack_message)
        if ack_message:
            return fallback
        prompt = self._build_ack_prompt_for_llm(event, kind, user_request, count)
        text = self._strip_llm_short_reply(await self._call_text_llm(event, prompt, timeout=7))
        custom = self._clean_ack_message(text, user_request)
        return custom or fallback

    def _record_bot_text_context(self, event: Optional[AstrMessageEvent], text: str) -> None:
        if not event or not str(text or "").strip():
            return
        self._add_context_message(
            session_key=self._context_session_key(event),
            sender_id="bot",
            sender_name=self._bot_display_name(),
            content=text,
            is_bot=True,
            msg_id=f"bot:{time.time_ns()}",
        )

    def _record_bot_image_context(self, event: Optional[AstrMessageEvent], files: Iterable[str]) -> None:
        if not event:
            return
        for file_path in files:
            if not str(file_path or "").strip():
                continue
            self._add_context_message(
                session_key=self._context_session_key(event),
                sender_id="bot",
                sender_name=self._bot_display_name(),
                content="[图片]",
                is_bot=True,
                image_sources=[os.path.abspath(str(file_path))],
                msg_id=f"bot-image:{time.time_ns()}",
            )

    def _is_admin_event(self, event: AstrMessageEvent) -> bool:
        role = str(getattr(event, "role", "") or "").lower().strip()
        if role in {"admin", "owner"}:
            return True
        sender = getattr(event, "sender", None)
        sender_role = str(getattr(sender, "role", "") or "").lower().strip()
        return sender_role in {"admin", "owner"}

    def _preset_list_text(self, page: int = 1, page_size: int = 20) -> Tuple[str, int, int]:
        presets = self.presets.list()
        total = len(presets)
        total_pages = max(1, (total + page_size - 1) // page_size)
        current_page = min(total_pages, max(1, page))
        start = (current_page - 1) * page_size
        items = presets[start:start + page_size]
        prefix = "/"
        lines = [
            f"📋 生图预设 第 {current_page}/{total_pages} 页",
            f"当前共有 {total} 个预设。",
            "",
            "使用方式：",
            f"1. {prefix}画 预设名 额外提示词",
            f"2. {prefix}自拍 预设名 额外提示词",
            f"3. {prefix}预设 添加 预设名:提示词（管理员）",
            f"4. {prefix}预设 删除 预设名（管理员）",
            f"5. {prefix}预设 查看 [页码/预设名]（管理员）",
            "",
        ]
        if total_pages > 1:
            if current_page < total_pages:
                lines.append(f"下一页：{prefix}预设 {current_page + 1}")
            if current_page > 1:
                lines.append(f"上一页：{prefix}预设 {current_page - 1}")
            lines.append("")

        if not items:
            lines.append("暂无预设。")
        else:
            lines.append("预设名：")
            for idx, (name, _) in enumerate(items, start=start + 1):
                lines.append(f"{idx}. {name}")

        return "\n".join(line for line in lines if line is not None), current_page, total_pages

    def _preset_detail_lines(self, idx: Optional[int], name: str, preset: Any) -> List[str]:
        desc = preset.description or preset.prompt
        extra = preset.extra_prompt
        params = []
        if preset.aspect_ratio:
            params.append(f"比例: {preset.aspect_ratio}")
        if preset.resolution:
            params.append(f"分辨率: {preset.resolution}")
        title = f"{idx}. {name}" if idx is not None else str(name)
        return [
            title,
            f"提示词: {preset.prompt}",
            *( [f"额外提示词: {extra}"] if extra else [] ),
            *( [f"说明: {desc}"] if desc and desc != preset.prompt else [] ),
            *( [f"参数: {' | '.join(params)}"] if params else [] ),
            "",
        ]

    def _preset_detail_text(self, page: int = 1, page_size: int = 20) -> Tuple[str, int, int]:
        presets = self.presets.list()
        total = len(presets)
        total_pages = max(1, (total + page_size - 1) // page_size)
        current_page = min(total_pages, max(1, page))
        start = (current_page - 1) * page_size
        items = presets[start:start + page_size]
        prefix = "/"
        lines = [
            f"📋 生图预设详情 第 {current_page}/{total_pages} 页",
            f"当前共有 {total} 个预设。",
            "仅管理员可见。",
            "",
        ]
        if total_pages > 1:
            if current_page < total_pages:
                lines.append(f"下一页：{prefix}预设 查看 {current_page + 1}")
            if current_page > 1:
                lines.append(f"上一页：{prefix}预设 查看 {current_page - 1}")
            lines.append("")

        if not items:
            lines.append("暂无预设。")
        else:
            for idx, (name, preset) in enumerate(items, start=start + 1):
                lines.extend(self._preset_detail_lines(idx, name, preset))

        return "\n".join(line for line in lines if line is not None), current_page, total_pages

    def _preset_single_detail_text(self, name: str) -> Tuple[bool, str]:
        target = str(name or "").strip()
        if not target:
            return False, "格式：/预设 查看 预设名"
        for preset_name, preset in self.presets.list():
            if preset_name == target:
                return True, "\n".join(
                    [
                        "📋 生图预设详情",
                        "仅管理员可见。",
                        "",
                        *self._preset_detail_lines(None, preset_name, preset),
                    ]
                ).strip()
        return False, f"预设不存在: {target}"

    def _handle_preset_mutation(self, event: AstrMessageEvent, action: str, payload: str) -> Tuple[bool, str]:
        if not self._is_admin_event(event):
            return False, "仅管理员可以管理预设。"
        if action == "add":
            if ":" in payload:
                name, value = payload.split(":", 1)
            elif "：" in payload:
                name, value = payload.split("：", 1)
            else:
                return False, "格式：预设 添加 名称:提示词"
            return self.presets.add(name, value)
        if action == "delete":
            return self.presets.remove(payload)
        return False, "未知操作"

    def _friendly_user_error_message(self, error: str, fallback: str = "") -> str:
        text = str(error or "").strip()
        if not text:
            return fallback or "我刚刚没抓稳这个感觉，换个方向再试试好不好。"
        if "可使用人员白名单" in text:
            return "这个功能现在还只对少数人开放。"
        if "用户黑名单" in text:
            return "现在先不方便给你看这类内容。"
        if "今日生图次数已用完" in text:
            return "我今天已经拍得有点多了，明天再来找我好不好。"
        if "请求太频繁" in text:
            return "先让我缓一小会儿，我等下就继续给你看。"
        if "提示词审核未通过" in text or "图片内容审核未通过" in text or "提示词包含禁用词" in text:
            return "这次这个方向我不太方便照着来，换个说法或者换个感觉试试。"
        if "当前没有可用的生图模型" in text or "未配置可用出图审核模型" in text or "未启用" in text:
            return random.choice(["这会儿接口没接上，晚点再试。", "现在暂时出不了图，等配置恢复再来。"])
        if "缺少生图提示词" in text or "请输入提示词" in text:
            return "你想让我往什么感觉走？也可以直接丢张参考图给我。"
        detail = redact_sensitive_text(text)
        detail = re.sub(r"[\r\n\t]+", " ", detail)
        detail = re.sub(r"Traceback \(most recent call last\):.*", "", detail, flags=re.I)
        detail = re.sub(r"\s+", " ", detail).strip(" ：:;；")
        return detail[:180] + ("…" if len(detail) > 180 else "") if detail else (fallback or self._natural_fail_fallback("image"))

    def _tool_soft_fail(self, error: str, fallback: str = "") -> str:
        message = self._friendly_user_error_message(error, fallback)
        return f"[TOOL_FAILED] {message}\n请用简体中文、用你自己的语气自然安抚用户，别提生成、工具、配置、次数、冷却或审核这些词。"

    def _tool_unavailable(self, fallback: str) -> str:
        return f"[TOOL_UNAVAILABLE] {fallback}\n请用简体中文、用你自己的语气自然安抚用户，别提功能、工具或配置。"

    def _tool_success(self, kind: str = "image", count: int = 1) -> str:
        label = "照片" if kind == "selfie" else "图片"
        count_text = f"，共 {count} 张" if count > 1 else ""
        return (
            f"[TOOL_SUCCESS] {label}已经发给用户{count_text}。\n"
            "请用简体中文、按当前人格自然收尾一句，也可以很短。"
            "不要复述请求，不要说生成、绘制、工具、调用、任务、已完成、已发送、配置、模型、提示词或审核。"
        )

    def _build_leg_focus_action(
        self,
        extra_request: str = "",
        has_refs: bool = False,
        *,
        avoid_pose: str = "",
        force_legwear: str = "",
    ) -> str:
        """生成中性的日常下装构图，并按姿势选择兼容的自拍或第三人称摄影视角。"""
        pose_pool = [
            # Keep the established non-standing pose families.  Only the standing
            # top-down variant was removed; the crop variants remain available.
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
        ]
        if avoid_pose:
            filtered = [(name, w) for name, w in pose_pool if name != avoid_pose]
            if filtered:
                pose_pool = filtered
        names = [n for n, _ in pose_pool]
        weights = [w for _, w in pose_pool]
        pose_bucket = random.choices(names, weights=weights, k=1)[0]

        pose_variants = {
            "sit": ["椅上或沙发自然坐好，衣摆顺着坐姿落下，室内光线柔和。"],
            "sit_crop": ["椅上或沙发自然坐好，衣摆顺着坐姿落下，近距离记录腰线到膝部附近的服装。"],
            "kneel": ["在地毯或软垫上自然跪坐，双膝并拢、重心稳定，上身略前倾，衣摆平整落下。"],
            "kneel_crop": ["在地毯或软垫上自然跪坐，双膝并拢、重心稳定，近距离记录衣摆到膝部附近的搭配。"],
            "side_lie": ["在床边或沙发上侧躺曲腿，一侧自然弯曲、另一侧放松伸展，衣料和靠垫形成生活化背景。"],
            "side_lie_crop": ["在床边或沙发上侧躺曲腿，姿势放松可维持，近距离记录衣摆到膝部附近的服装层次。"],
            "hug_knee": ["坐在床边或地毯上自然抱膝，双手轻扶膝部或衣摆，姿势舒适可维持。"],
            "hug_knee_crop": ["坐在床边或地毯上自然抱膝，衣摆随着收膝姿势落下，画面聚焦衣摆到膝部附近。"],
            "cross_leg": ["坐在椅上或沙发上自然翘二郎腿，衣摆和服装搭配协调，重心稳定，像普通生活随拍。"],
            "cross_leg_crop": ["坐在椅上或沙发上自然交叠双侧下装，近距离记录腰线到膝部附近的搭配，边界清楚。"],
            "windowsill": ["稳坐窗台或矮柜边，一侧轻踩台沿、另一侧自然垂下，衣摆自然落下，窗光柔和。"],
            "windowsill_crop": ["稳坐窗台或矮柜边，保持窗边坐姿与稳定重心，近距离记录衣料、颜色和褶皱。"],
            "kneel_up": ["在软垫上采用较高的跪姿，上身略直、重心稳定，衣物自然垂落。"],
            "kneel_front": ["面向镜头跪坐在地毯上，双膝并拢、衣摆整洁，保持日常记录感。"],
            "floor_fold": ["坐在地毯或木地板上自然屈膝，双侧衣摆向身前折叠，衣物层次清楚。"],
            "one_knee_fix": ["一侧单膝触地、另一侧自然支撑，手轻整理衣摆或袜口，动作生活化且重心稳定。"],
            "reclined_knees_crop": ["在沙发或座椅上轻松靠坐，膝部自然向前弯曲，近距离记录服装版型。"],
            "floor_knees_up_crop": ["在地毯或木地板上轻松席地坐，膝部自然收近，近距离记录衣摆和材质。"],
            "desk_sit_crop": ["坐在桌前椅上，桌沿可入镜，近距离记录衣摆、颜色和材质。"],
            "bed_supine_crop": ["在床上由靠垫支撑轻松靠坐，衣摆和床品自然铺开，保持居家随拍感。"],
        }
        pose_labels = {
            "sit": "坐姿穿搭记录", "sit_crop": "坐姿近景记录",
            "kneel": "跪坐记录", "kneel_crop": "跪坐近景",
            "side_lie": "侧躺曲腿记录", "side_lie_crop": "侧躺曲腿近景",
            "hug_knee": "收膝坐姿记录", "hug_knee_crop": "收膝坐姿近景",
            "cross_leg": "交叠坐姿记录", "cross_leg_crop": "交叠坐姿近景",
            "windowsill": "窗边坐姿记录", "windowsill_crop": "窗边坐姿近景",
            "kneel_up": "高位跪姿", "kneel_front": "正面跪坐",
            "floor_fold": "席地屈膝坐姿",
            "one_knee_fix": "单膝整理衣摆",
            "reclined_knees_crop": "沙发靠坐近景", "floor_knees_up_crop": "席地坐近景",
            "desk_sit_crop": "桌前坐姿近景", "bed_supine_crop": "床上靠坐近景",
        }
        variants = pose_variants.get(pose_bucket) or pose_variants["sit_crop"]
        pose_label = pose_labels.get(pose_bucket, "坐姿下装展示")
        camera_bag = [
            kind
            for kind, weight in LEGFOCUS_CAMERA_WEIGHTS.get(
                pose_bucket,
                (("selfie", 1), ("third", 1)),
            )
            for _ in range(weight)
        ]
        camera_kind = random.choice(camera_bag)
        camera_line = (
            "第一人称手机自拍：人物自己举手机向下记录日常服装局部，镜头从腰线附近取到膝部附近。"
            if camera_kind == "selfie"
            else "第三人称摄影照片：由画面外的朋友用手机拍摄日常服装局部，镜头从腰线附近取到膝部附近。"
        )
        # Keep the frame on the clothing area instead of using body-focused wording.
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
            legwear_options = LEGWEAR_BY_POSE.get(pose_bucket, LEGWEAR_BY_POSE["sit_crop"])
            legwear = random.choices(
                [name for name, _ in legwear_options],
                weights=[weight for _, weight in legwear_options],
                k=1,
            )[0]
        legwear_label = SAFE_LEGWEAR_LABELS.get(legwear, "日常不透明长袜")
        legwear_rule = f"本次服装搭配：{legwear_label}，材质自然，作为得体日常穿搭的一部分。"
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
            base += " 用户提供的图片只参考氛围、构图、服装或姿势；主角身份仍以 AI 自拍形象参考图为准。"
        # Strip sock/legwear tokens from free-text extra so they don't fight the locked choice.
        extra = LEGWEAR_REQUEST_PATTERN.sub("", str(extra_request or ""))
        for risky_text, neutral_text in LEGFOCUS_RISKY_EXTRA_REPLACEMENTS:
            extra = extra.replace(risky_text, neutral_text)
        extra = re.sub(r"\s+", " ", extra).strip(" 。、，")
        if extra:
            base = base.rstrip("。") + f"。用户补充要求优先：{extra}。"
        wear_tag = {"白丝": "white", "黑丝": "black", "光腿神器": "bare"}.get(legwear, "daily")
        base += f" 【cam:{camera_kind}】 【wear:{wear_tag}】 【pose:{pose_bucket}】"
        return base

    def _normalize_selfie_action(self, action: str, has_refs: bool) -> str:
        """为腿部自拍补全单一姿势与腿部穿搭。"""
        raw = str(action or "").strip()
        pose_match = re.search(r"【pose:([a-z_]+)】", raw)
        removed_pose = "stand_" + "topdown"
        if pose_match and pose_match.group(1) == removed_pose:
            return self._build_leg_focus_action(raw, has_refs, avoid_pose=removed_pose)
        if pose_match or not self.persona.analyze_selfie_intent(raw).is_legs_only:
            return raw
        return self._build_leg_focus_action(raw, has_refs)


    @staticmethod
    def _period_scene_light_pools(kind: str = "selfie") -> tuple[list[str], list[str]]:
        from .persona import current_period

        period = current_period()
        if kind == "look_you":
            day_scenes = ["窗边沙发", "书桌前", "阳台栏杆", "厨房台边"]
            night_scenes = ["窗边沙发", "书桌前", "夜灯房间", "厨房台边"]
            cafe_day = ["咖啡馆座位", "楼下咖啡外摆", "街边树荫", "书店角落"]
            if period in {"morning", "noon"}:
                scenes = day_scenes + cafe_day
                lights = ["窗光", "阴天柔光"]
            elif period == "afternoon":
                scenes = day_scenes + cafe_day
                lights = ["窗光", "阴天柔光"]
            elif period == "evening":
                scenes = night_scenes + ["阳台栏杆", "雨后屋檐"]
                lights = ["暖台灯", "窗光"]
            else:
                scenes = night_scenes
                lights = ["暖台灯", "夜灯房间柔光"]
            return scenes, lights
        if period in {"morning", "noon"}:
            return ["浅色墙与窗边", "书桌与杯具", "阳台栏杆旁", "镜前整洁台面"], ["窗光柔和", "清晨清透光", "阴天漫射"]
        if period == "afternoon":
            return ["浅色墙与窗边", "书桌与杯具", "沙发与抱枕", "阳台栏杆旁"], ["窗光柔和", "阴天漫射"]
        if period == "evening":
            return ["暖灯房间一角", "沙发与抱枕", "书桌与杯具", "床边靠坐"], ["暖黄台灯", "窗光柔和"]
        return ["暖灯房间一角", "沙发与抱枕", "床边靠坐", "镜前整洁台面"], ["暖黄台灯"]

    def _build_selfie_look_action(
        self,
        extra_request: str = "",
        has_refs: bool = False,
        *,
        avoid_shot: str = "",
    ) -> str:
        """普通自拍 / 看看：机位+场景+小动作随机，默认看镜头。"""
        extra = re.sub(r"\s+", " ", str(extra_request or "")).strip(" 。")
        # Clothing style presets (e.g. 漏腰) need torso framing, not arm_half face selfie.
        if SelfieImagePlugin._looks_like_crop_waist_request(extra):
            return SelfieImagePlugin._build_crop_waist_selfie_action(self, extra, has_refs)

        shot_pool = [
            ("arm_half", 3),
            ("mirror_half", 2),
            ("window_side", 2),
            ("desk_sit", 2),
            ("sofa_casual", 2),
            ("high_angle", 1),
            ("close_portrait", 1),
        ]
        if avoid_shot:
            filtered = [(n, w) for n, w in shot_pool if n != avoid_shot]
            if filtered:
                shot_pool = filtered
        names = [n for n, _ in shot_pool]
        weights = [w for _, w in shot_pool]
        shot = random.choices(names, weights=weights, k=1)[0]

        shot_lines = {
            "arm_half": "手机自拍臂半身：手臂微伸举机，胸口以上到头顶入镜，眼神看镜头，表情自然松弛。",
            "mirror_half": "镜子半身自拍：镜中看镜头（看向手机镜头方向），构图干净，不要拍进乱糟糟背景堆。",
            "window_side": "窗边侧光半身自拍：身体略侧、脸仍转向镜头，窗光柔和，轮廓清楚。",
            "desk_sit": "书桌前坐下自拍：略俯视半身，手可托腮或扶桌沿，看镜头，居家学习/摸鱼感。",
            "sofa_casual": "沙发窝着随手自拍：半身或胸像，靠垫入镜一点，看镜头，轻松日常。",
            "high_angle": "稍高机位自拍：镜头略高于眼，脸自然抬一点看镜头，显精神，不要过度仰拍变形。",
            "close_portrait": "近景胸像自拍：脸与肩为主，眼神对焦镜头，五官清晰，浅景深。",
        }
        gestures = [
            "嘴角轻微笑意",
            "一只手整理发丝或衣领",
            "托腮听人说话的轻松感",
            "比个很小的 OK 或比心（含蓄）",
            "双手捧杯刚抬眼",
            "刚坐好整理袖口",
        ]
        scenes, lights = SelfieImagePlugin._period_scene_light_pools("selfie")
        scene = random.choice(scenes)
        gesture = random.choice(gestures)
        light = random.choice(lights)
        base = (
            "【自拍 / 看看模式】展示 AI 现在的样子。"
            "第一人称自拍视角（自己举机或镜前自拍），不是别人代拍。"
            "必须看向镜头：眼睛对焦镜头方向，表情自然有神；不要眼神飘走或心不在焉。"
            "保持 AI 当前形象、今日穿搭与气质一致，脸部清晰。"
            "竖屏手机近景半身：像短视频封面那样拍得近，但质感仍是真实皮肤、真实布料和接触阴影；窗光或暖灯，不要棚拍、不要美颜滤镜、不要塑料皮肤。"
            f"本次机位：{shot_lines.get(shot, shot_lines['arm_half'])}"
            f"场景倾向：{scene}。小动作：{gesture}。光线：{light}。"
            "画面干净日常，不要夸张摆拍。"
        )
        if has_refs:
            base = "参考用户提供的图片氛围、场景或构图，" + base + " 主角身份仍以 AI 形象为准。"
        if extra:
            base += f" 用户补充要求优先：{extra}。"
        base += f" 【shot:{shot}】"
        return base

    @staticmethod
    def _looks_like_crop_waist_request(text: str) -> bool:
        raw = str(text or "")
        if not raw.strip():
            return False
        if any(k in raw for k in ("漏腰", "露腰", "露脐短上衣", "小蛮腰", "crop_waist", "crop top", "short top", "短上衣")):
            return True
        low = raw.lower()
        has_inner = ("露脐" in raw) or ("短上衣" in raw) or ("crop top" in low) or ("short top" in low)
        has_outer = (
            ("外衫" in raw)
            or ("开衫" in raw)
            or ("半敞" in raw)
            or ("衬衫" in raw and ("宽松" in raw or "敞开" in raw))
            or ("oversized" in low)
        )
        return has_inner and has_outer

    def _build_crop_waist_selfie_action(self, extra_request: str = "", has_refs: bool = False) -> str:
        """漏腰/露腰：腰腹构图 + 内外两层穿搭，避免被日常半身自拍机位冲掉。"""
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
        if extra and extra not in base:
            if extra not in {"漏腰", "露腰", "漏腰杀", "小蛮腰"}:
                base += f" 用户补充要求优先：{extra}。"
        base += " 【shot:crop_waist】"
        return base

    def _build_cos_look_action(
        self,
        extra_request: str = "",
        has_refs: bool = False,
        *,
        avoid_id: str = "",
        avoid_camera: str = "",
        camera: str = "",
    ) -> str:
        """看看COS：随机套装；机位默认自拍/他拍随机，额外提示词可指定。"""
        chosen = pick_cos_look_set(avoid_id=avoid_id)
        title = str(chosen.get("title") or "随机COS")
        camera_kind = pick_cos_camera(extra_request=extra_request, avoid=avoid_camera, camera=camera)
        outfit = adapt_cos_outfit_for_camera(str(chosen.get("prompt") or "").strip(), camera_kind)
        cos_id = str(chosen.get("id") or "cos")
        if camera_kind == "third":
            framing = (
                "【他拍 / 看看COS模式】"
                "展示 AI 现在的样子，但本次强制换装为指定 COS 套装。"
                "别人视角的单人成品照：竖屏手机近景半身或环境人像，像随手拍的 COS 封面；画面里只有主角一个人；"
                "拍摄者完全在画面外，不要第二个人，不要有人举着手机拍主角，不要拍到拍照过程；"
                "不要对镜、不要镜子、不要手持手机入镜，不要第一人称伸手自拍，不要手臂挡脸挡衣服。"
            )
        else:
            framing = (
                "【自拍 / 看看COS模式】"
                "展示 AI 现在的样子，但本次强制换装为指定 COS 套装。"
                "竖屏手机近景半身自拍：可对镜，但拍胸像到腰线，不要展会式全身棚拍；手机可出现在镜中；"
                "不要第一人称伸手自拍，不要手臂挡脸挡衣服。"
            )
        base = (
            framing
            + "脸型五官必须保持形象参考，不要换成别人的脸；"
            + "假发颜色/发型/发饰可按本套 COS 完整替换。"
            + f"本次套装：{title}。"
            + f"{outfit}"
            + "服装颜色、层数、配饰、开叉、荷叶边、鞋履等结构要尽量齐全高还原；"
            + "构图完整带上腰线；竖屏近景半身即可，不要简化成普通常服；画面干净得体。"
        )
        if has_refs:
            base = "参考用户附图的氛围或构图，" + base
        extra = re.sub(r"\s+", " ", str(extra_request or "")).strip(" 。")
        if extra and extra not in base:
            base += f" 用户补充要求优先：{extra}。"
        base += f" 【cos:{cos_id}】 【cam:{camera_kind}】"
        return base

    def _build_third_person_look_action(
        self,
        extra_request: str = "",
        has_refs: bool = False,
        *,
        avoid_shot: str = "",
    ) -> str:
        """他拍 / 看看你：景别+场景+动作随机；正面近景优先看镜头。"""
        shot_pool = [
            ("half_front", 3),
            ("three_quarter", 3),
            ("env_mid", 2),
            ("walk_candid", 1),
            ("close_smile", 2),
            ("low_over", 1),
            ("lean_wall", 2),
        ]
        if avoid_shot:
            filtered = [(n, w) for n, w in shot_pool if n != avoid_shot]
            if filtered:
                shot_pool = filtered
        names = [n for n, _ in shot_pool]
        weights = [w for _, w in shot_pool]
        shot = random.choices(names, weights=weights, k=1)[0]

        shot_lines = {
            "half_front": "半身平视他拍：别人视角的单人半身照，AI 正面或微侧看镜头，眼神有焦点。",
            "three_quarter": "三分之四侧身他拍：身体略侧、头转向镜头，像被叫名字时回头。",
            "env_mid": "中远景环境人像：人物与场景都清楚，AI 仍是主体，自然看向镜头或刚抬头。",
            "walk_candid": "走路抓拍：侧前方跟随感，步伐自然，可看镜头一瞬，生活感强。",
            "close_smile": "近景胸像：脸与肩，浅景深，对镜头带一点笑意，五官清晰。",
            "low_over": "轻微低机位过肩感：从略低处拍半身，仍看镜头，不要过度仰拍变形。",
            "lean_wall": "靠墙/门框他拍：肩背轻靠，双手自然，看镜头，竖构图友好。",
        }
        actions = [
            "端着杯子刚抬眼",
            "托腮听人说话",
            "整理袖口或发丝",
            "翻书停顿抬头",
            "双手插兜靠站",
            "拎袋子侧身回看",
            "刚坐下整理衣角",
            "对镜头轻轻点头笑",
        ]
        scenes, lights = SelfieImagePlugin._period_scene_light_pools("look_you")
        scene = random.choice(scenes)
        action = random.choice(actions)
        light = random.choice(lights)
        base = (
            "【他拍 / 看看你模式】展示 AI 当前样子的自然日常照片。"
            "别人视角的单人成品照：镜头已经对准主角拍下，画面里只有主角一个人；拍摄者完全在画面外，不要第二个人，不要有人举着手机拍主角。"
            "正面半身或近景时优先看向镜头，眼神自然有焦点；可以轻松回头，但不要整段心不在焉。"
            "保持 AI 当前形象、今日穿搭和生活状态一致，脸部、穿搭、姿态、背景层次和光线都清晰自然。"
            "竖屏手机近景半身：窗光或暖灯，真实皮肤和真实布料，不要美颜滤镜、不要棚拍精修。"
            f"本次机位：{shot_lines.get(shot, shot_lines['half_front'])}"
            f"场景倾向：{scene}。动作瞬间：{action}。光线：{light}。"
            "写实手机拍照质感，不要影楼硬摆。"
        )
        if has_refs:
            base = "参考用户提供的图片氛围、场景或构图，" + base
        extra = re.sub(r"\s+", " ", str(extra_request or "")).strip(" 。")
        if extra:
            base += f" 用户补充要求优先：{extra}。"
        base += f" 【shot:{shot}】"
        return base

    def _build_group_selfie_action(self, extra_request: str = "", has_refs: bool = False) -> str:
        appearance_type = "auto"
        try:
            appearance_type = self.persona.get_appearance_type()
        except Exception:
            appearance_type = "auto"
        from .persona import appearance_type_instruction, group_style_lines

        appearance_line = appearance_type_instruction(appearance_type, has_reference_image=True)
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

    def _looks_like_group_selfie_intent(self, text: str) -> bool:
        value = str(text or "")
        compact = re.sub(r"[\s，。！？、；：,.!?]", "", value.lower())
        compact_keywords = [
            "合影",
            "合照",
            "同框",
            "一起拍",
            "一起照",
            "和我",
            "跟我",
            "与我",
            "陪我",
            "和你",
            "跟你",
            "与你",
            "你和我",
            "我和你",
            "我们一起",
            "groupselfie",
            "groupphoto",
            "phototogether",
            "takeaphototogether",
            "takeapicturetogether",
            "sameframe",
            "inthesameframe",
            "sidebyside",
            "standingnextto",
            "twous",
            "ustogether",
        ]
        if any(keyword in compact for keyword in compact_keywords):
            return True
        low = value.lower()
        phrase_keywords = [
            "group selfie",
            "group photo",
            "photo together",
            "take a photo together",
            "take a picture together",
            "same frame",
            "in the same frame",
            "side by side",
            "standing next to",
            "two of us",
            "us together",
            "with me",
            "with you",
        ]
        for keyword in phrase_keywords:
            pattern = r"(?<![a-z0-9])" + re.escape(keyword).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
            if re.search(pattern, low):
                return True
        return False

    def _looks_like_selfie_intent(self, text: str) -> bool:
        value = str(text or "")
        low = value.lower()
        bot_name = str(self.config.bot_name or "").strip()
        keywords = [
            "自拍",
            "合影",
            "合照",
            "同框",
            "形象照",
            "和我",
            "跟我",
            "与我",
            "陪我",
            "和你",
            "跟你",
            "与你",
            "你和我",
            "我和你",
            "我们一起",
            "一起拍",
            "一起照",
            "你自己",
            "你的照片",
        ]
        english_keywords = [
            "selfie",
            "group selfie",
            "group photo",
            "photo together",
            "take a photo together",
            "take a picture together",
            "together with me",
            "with me",
            "with you",
            "next to me",
            "next to you",
            "standing next to",
            "side by side",
            "same frame",
            "in the same frame",
            "two of us",
            "us together",
            "your photo",
            "yourself",
            "ai assistant",
            "catgirl",
            "ahwu",
        ]
        if bot_name:
            keywords.append(bot_name)
            english_keywords.append(bot_name.lower())
        return any(keyword and keyword in value for keyword in keywords) or any(keyword and keyword in low for keyword in english_keywords)

    async def _run_llm_selfie_flow(
        self,
        event: AstrMessageEvent,
        action: str,
        requested_count: int,
        aspect: str,
        resolution: str,
        ack_message: str = "",
    ) -> Optional[str]:
        if not self.config.image_enable_llm_tool:
            return self._tool_unavailable("我这会儿还没法给你拍这种。")
        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            return self._tool_soft_fail(error)

        action = str(action or "").strip() or "看着镜头自然自拍"
        await self._send_progress_text(
            event,
            await self._build_contextual_progress_text(event, "selfie", action, requested_count, ack_message),
        )
        extra_refs = await self._event_reference_images(
            event,
            include_at_avatar=self._looks_like_group_selfie_intent(action),
            context_hint=action,
            allow_context_fallback=True,
        )
        action = self._normalize_selfie_action(action, bool(extra_refs))
        result = await self._background_selfie_batches(
            "llm-generate-selfie",
            event,
            action,
            extra_refs,
            "llm-generate-selfie",
            requested_count,
            aspect,
            resolution,
            self._natural_fail_fallback("selfie"),
        )
        if not result.get("success") and not result.get("files"):
            return self._tool_soft_fail(str(result.get("error") or ""), self._natural_fail_fallback("selfie"))
        return self._tool_success("selfie", len(result.get("files") or []) or requested_count)

    def _build_success_text(self, elapsed_seconds: float, count: int, used_model: str, event: AstrMessageEvent) -> str:
        lines: List[str] = []
        if self.config.image_show_generation_info:
            lines.append(f"生成成功，耗时 {elapsed_seconds:.2f}s，数量 {count} 张。")
            if self.config.image_enable_daily_limit:
                status = self._access_status(event)
                if status.get("unlimited"):
                    lines.append("今日用量：白名单用户/群组不限制。")
                else:
                    user_id = status.get("user_id") or ""
                    used = int(self._current_usage_stats().get("users", {}).get(user_id, {}).get("count", 0))
                    lines.append(f"今日用量：{used}/{self.config.image_daily_limit_count}。")
        if self.config.image_show_model_info and used_model:
            lines.append(f"模型：{used_model}")
        return "\n".join(lines)

    def _batch_success_text(self, info: str, index: int, total: int) -> str:
        text = str(info or "").strip()
        if not text:
            return ""
        if total > 1:
            return f"第 {index}/{total} 次请求完成。\n{text}"
        return text

    async def _run_image_generation(
        self,
        prompt: str,
        aspect_ratio: str,
        resolution: str,
        refs: List[ImageReference],
        targets: Optional[List[ImageModelTarget]] = None,
        source: str = "command",
        audit_user_id: str = "",
        event: Optional[AstrMessageEvent] = None,
        original_prompt: str = "",
        max_attempts: Optional[int] = None,
        allow_compat_retry: bool = True,
        prompt_en_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        selected_targets = targets or self._resolve_generation_targets(event)
        healthy_targets = [target for target in selected_targets if self._channel_is_healthy(target.channel_name)]
        if selected_targets and not healthy_targets:
            return {"success": False, "error": "所有生图渠道暂时冷却中，请稍后重试或清除渠道健康状态"}
        selected_targets = healthy_targets or selected_targets
        request_prompt = str(prompt or "")
        original_prompt = str(original_prompt or request_prompt)
        # Leg-focus actions are normalized into a neutral outfit prompt before generation.
        # Audit that effective prompt so command labels do not create false positives.
        is_leg_focus_request = (
            source == "command-look-legs"
            or "【legs:outfit】" in original_prompt
            or "下半身穿搭" in original_prompt
        )
        audit_prompt_text = request_prompt if is_leg_focus_request else (original_prompt or request_prompt)
        source_meta = self._source_context(event, source, audit_user_id)
        request_image_paths = self._save_reference_images_to_cache(refs)
        request_data = {
            "original_prompt": original_prompt,
            "request_prompt": request_prompt,
            "audit_prompt": audit_prompt_text,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "reference_image_count": len(refs),
            "request_image_paths": request_image_paths,
            "targets": [redact_sensitive_text(target.label) for target in selected_targets],
        }
        if prompt_en_meta:
            request_data["prompt_en"] = dict(prompt_en_meta)
            if prompt_en_meta.get("applied"):
                request_data["request_prompt_en"] = request_prompt
        request_data["composition"] = self._composition_metadata(
            request_prompt, source, aspect_ratio, resolution, len(refs)
        )
        request_cleanup = self._cleanup_image_cache_if_needed(request_image_paths)
        if request_cleanup.get("deleted"):
            request_data["cache_cleanup_before_generation"] = request_cleanup

        audit_ok, audit_reason = await self._audit_prompt(audit_prompt_text, audit_user_id, event)
        if not audit_ok:
            response_data = {"success": False, "stage": "prompt_audit", "error": f"提示词审核未通过：{audit_reason}"}
            self._record_task(
                {
                    **source_meta,
                    "success": False,
                    "error": response_data["error"],
                    "prompt": request_prompt,
                    "original_prompt": original_prompt,
                    "request_prompt": request_prompt,
                    "used_model": "",
                    "elapsed_seconds": 0,
                    "reference_images": len(refs),
                    "request_data": request_data,
                    "response_data": response_data,
                    "request_image_paths": request_image_paths,
                    "generated_image_paths": [],
                }
            )
            return {"success": False, "error": f"提示词审核未通过：{audit_reason}"}

        # Optional: translate final image prompt to English for models weak on Chinese.
        if prompt_en_meta is None and self._prompt_en_needed(request_prompt, media="image"):
            translated, en_meta = await self._translate_prompt_to_english(
                request_prompt, media="image", event=event
            )
            request_data["prompt_en"] = en_meta
            if en_meta.get("applied") and translated:
                request_prompt = translated
                request_data["request_prompt"] = request_prompt
                request_data["request_prompt_en"] = translated

        if not selected_targets:
            response_data = {
                "success": False,
                "stage": "select_model",
                "error": "当前没有可用的出图模型，请先在管理页启用渠道和模型。",
            }
            self._record_task(
                {
                    **source_meta,
                    "success": False,
                    "error": response_data["error"],
                    "prompt": request_prompt,
                    "original_prompt": original_prompt,
                    "request_prompt": request_prompt,
                    "used_model": "",
                    "elapsed_seconds": 0,
                    "reference_images": len(refs),
                    "request_data": request_data,
                    "response_data": response_data,
                    "request_image_paths": request_image_paths,
                    "generated_image_paths": [],
                }
            )
            return {"success": False, "error": response_data["error"]}

        request = ImageGenerateRequest(
            prompt=request_prompt,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            images=refs,
            allow_compat_retry=allow_compat_retry,
            max_image_bytes=self.config.image_max_image_size_mb * 1024 * 1024,
        )
        started = time.monotonic()
        # trust_env=False: channel.proxy is explicit; do not inherit process HTTP(S)_PROXY
        # (common on ops hosts) and silently stall NewAPI image downloads/posts.
        async with self._semaphore:
            result = await generate_image_with_fallback(
                selected_targets,
                request,
                None,
                max_attempts=max_attempts,
                global_timeout=self.config.image_global_timeout,
            )
        elapsed = time.monotonic() - started
        self._record_channel_health(result.attempts)

        if result.error or not result.images:
            response_data = {
                "success": False,
                "stage": "generate",
                "error": result.error or "未生成任何图片",
                "used_model": result.used_model,
                "elapsed_seconds": round(elapsed, 2),
                "attempts": result.attempts,
            }
            self._record_task(
                {
                    **source_meta,
                    "success": False,
                    "error": response_data["error"],
                    "prompt": request_prompt,
                    "original_prompt": original_prompt,
                    "request_prompt": request_prompt,
                    "used_model": result.used_model,
                    "elapsed_seconds": round(elapsed, 2),
                    "reference_images": len(refs),
                    "request_data": request_data,
                    "response_data": response_data,
                    "request_image_paths": request_image_paths,
                    "generated_image_paths": [],
                }
            )
            return {
                "success": False,
                "error": result.error or "未生成任何图片",
                "elapsed_seconds": elapsed,
                "used_model": result.used_model,
                "request_data": request_data,
                "response_data": response_data,
                "request_image_paths": request_image_paths,
                "attempts": result.attempts,
            }

        generated_images = [image for image in result.images if image]
        generated_image_paths = [
            self._save_cache_image(image, "generated", detect_mime_by_bytes(image))
            for image in generated_images
        ]
        generated_image_md5s = [hashlib.md5(image).hexdigest() for image in generated_images]
        files = [self._cache_absolute_path(path) for path in generated_image_paths]
        output_ok, output_reason = await self._audit_output_images(files, audit_user_id, prompt, event=event)
        if not output_ok:
            response_data = {
                "success": False,
                "stage": "output_audit",
                "error": f"图片内容审核未通过：{output_reason}",
                "used_model": result.used_model,
                "elapsed_seconds": round(elapsed, 2),
                "generated_image_paths": generated_image_paths,
                "blocked_images_retained": True,
                "attempts": result.attempts,
            }
            self._record_task(
                {
                    **source_meta,
                    "success": False,
                    "error": response_data["error"],
                    "prompt": request_prompt,
                    "original_prompt": original_prompt,
                    "request_prompt": request_prompt,
                    "used_model": result.used_model,
                    "elapsed_seconds": round(elapsed, 2),
                    "reference_images": len(refs),
                    "request_data": request_data,
                    "response_data": response_data,
                    "request_image_paths": request_image_paths,
                    "generated_image_paths": generated_image_paths,
                    "md5s": generated_image_md5s,
                }
            )
            return {
                "success": False,
                "error": f"图片内容审核未通过：{output_reason}",
                "elapsed_seconds": elapsed,
                "used_model": result.used_model,
                "image_paths": generated_image_paths,
                "attempts": result.attempts,
            }

        cleanup = self._cleanup_image_cache_if_needed([*request_image_paths, *generated_image_paths])
        response_data = {
            "success": True,
            "used_model": result.used_model,
            "elapsed_seconds": round(elapsed, 2),
            "count": len(files),
            "generated_image_paths": generated_image_paths,
            "cache_cleanup": cleanup,
            "attempts": result.attempts,
        }
        self._record_task(
            {
                **source_meta,
                "success": True,
                "prompt": request_prompt,
                "original_prompt": original_prompt,
                "request_prompt": request_prompt,
                "used_model": result.used_model,
                "elapsed_seconds": round(elapsed, 2),
                "reference_images": len(refs),
                "count": len(files),
                "request_data": request_data,
                "response_data": response_data,
                "request_image_paths": request_image_paths,
                "generated_image_paths": generated_image_paths,
                "md5s": generated_image_md5s,
            }
        )
        return {
            "success": True,
            "files": files,
            "image_paths": generated_image_paths,
            "elapsed_seconds": elapsed,
            "used_model": result.used_model,
            "reference_images": len(refs),
            "request_data": request_data,
            "response_data": response_data,
            "request_image_paths": request_image_paths,
            "attempts": result.attempts,
        }

    async def _build_selfie_prompt_and_refs(self, action: str, extra_refs: List[ImageReference], event: Optional[AstrMessageEvent] = None) -> Tuple[str, List[ImageReference]]:
        llm_generate = (lambda prompt: self._call_text_llm(event, prompt, timeout=6)) if event is not None else None
        await self.persona.ensure_daily_selfie_profile(action, llm_generate=llm_generate)
        persona_ref = self._persona_identity_reference()
        refs: List[ImageReference] = []
        if persona_ref:
            refs.append(persona_ref)
        refs.extend(self._persona_auxiliary_references(action))
        refs.extend(extra_refs)
        prompt = self.persona.build_selfie_prompt(
            action=action or "看着镜头自然自拍，展示你现在的样子",
            bot_name=self.config.bot_name,
            personality=self.config.personality,
            has_reference_image=bool(persona_ref),
            extra_reference_count=len(extra_refs),
        )
        return prompt, refs

    async def _build_selfie_prompt_and_refs_for_event(
        self,
        event: Optional[AstrMessageEvent],
        action: str,
        extra_refs: List[ImageReference],
    ) -> Tuple[str, List[ImageReference], Dict[str, Any]]:
        """Use the central English built-ins and translate only free-form user text."""
        prompt, refs = await self._build_selfie_prompt_and_refs(action, extra_refs, event=event)
        has_identity_reference = bool(self._persona_identity_reference())
        meta: Dict[str, Any] = {"enabled": False, "applied": False, "scope": "user_text_only"}
        if not self._prompt_en_needed(action, media="image"):
            return prompt, refs, meta
        from .prompt_templates import build_selfie_builtin_prompt, extract_user_prompt

        user_text = extract_user_prompt(action)
        if not user_text:
            english = build_selfie_builtin_prompt(
                action,
                language="en",
                has_reference_image=has_identity_reference,
                extra_reference_count=len(extra_refs),
                appearance_type=self.persona.get_appearance_type(),
            )
            meta.update({"enabled": True, "applied": True, "scope": "builtin_only"})
            return english, refs, meta
        translated, translation_meta = await self._translate_prompt_to_english(
            user_text,
            media="image",
            event=event,
        )
        meta.update(translation_meta)
        meta["scope"] = "user_text_only"
        if not translation_meta.get("applied"):
            return prompt, refs, meta
        english = build_selfie_builtin_prompt(
            action,
            language="en",
            has_reference_image=has_identity_reference,
            extra_reference_count=len(extra_refs),
            appearance_type=self.persona.get_appearance_type(),
            user_text=translated,
        )
        return english, refs, meta

    def get_selfie_reference_payload(self) -> Dict[str, Any]:
        data = self.persona.get()
        ref = self.persona.get_reference_image()
        appearance_type = self.persona.get_appearance_type()
        base = {
            "appearance_type": appearance_type,
            "appearance_type_label": self.persona.appearance_type_label(),
            "ref_mime_type": data.get("ref_mime_type") or "image/png",
            "updated_at": data.get("updated_at") or "",
            "status": self.persona.status_text(),
        }
        if not ref:
            return {
                **base,
                "has_image": False,
            }
        return {
            **base,
            "has_image": True,
            "ref_mime_type": ref["mime_type"],
            "image": bytes_to_data_url(ref["data"], ref["mime_type"]),
        }

    def set_selfie_appearance_type_from_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        value = payload.get("appearance_type", payload.get("type", "auto"))
        self.persona.set_appearance_type(value)
        return self.get_selfie_reference_payload()

    def save_selfie_reference_from_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_image = str(payload.get("image") or payload.get("data") or "").strip()
        if not raw_image:
            raise ValueError("缺少 image 字段，支持 data:image/...;base64,... 或纯 base64")
        data, mime = data_url_to_bytes(raw_image)
        if not data:
            raise ValueError("上传图片为空")
        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        if len(data) > max_bytes:
            raise ValueError(f"图片过大，最大允许 {self.config.image_max_image_size_mb}MB")
        self.persona.save_reference_image(data, normalize_image_mime(mime or str(payload.get("mime_type") or "") or detect_mime_by_bytes(data)))
        if "appearance_type" in payload or "type" in payload:
            self.persona.set_appearance_type(payload.get("appearance_type", payload.get("type")))
        return self.get_selfie_reference_payload()

    def clear_selfie_reference_from_web(self) -> Dict[str, Any]:
        self.persona.clear_reference_image()
        return self.get_selfie_reference_payload()

    async def refresh_selfie_profile_from_web(self) -> Dict[str, Any]:
        self.persona.refresh_daily_selfie_profile_for_test()
        await self.persona.ensure_daily_selfie_profile("手动刷新今日自拍设定")
        return {
            "status": self.persona.status_text(),
            "updated_at": self.persona.get().get("updated_at") or "",
        }

    def _find_image_target(self, channel_name: str = "", model: str = "") -> Optional[ImageModelTarget]:
        targets: List[ImageModelTarget] = []
        for channel in self.config.image_channels:
            targets.extend(channel.targets(self.config.image_global_timeout))
        if not channel_name and not model:
            return targets[0] if targets else None
        for target in targets:
            if channel_name and target.channel_name != channel_name:
                continue
            if model and target.model != model:
                continue
            return target
        for target in targets:
            if channel_name and target.channel_name == channel_name and not model:
                return target
        return None

    def _find_video_target(self, channel_name: str = "", model: str = "") -> Optional[ImageModelTarget]:
        targets = self.config.get_prioritized_video_targets()
        if not channel_name and not model:
            return targets[0] if targets else None
        for target in targets:
            if channel_name and target.channel_name != channel_name:
                continue
            if model and target.model != model:
                continue
            return target
        return next(
            (target for target in targets if channel_name and target.channel_name == channel_name and not model),
            None,
        )

    def _available_model_labels(self) -> List[str]:
        labels: List[str] = []
        seen = set()
        for target in self.config.get_prioritized_targets():
            if target.label in seen:
                continue
            seen.add(target.label)
            labels.append(target.label)
        return labels

    def _get_session_model_override(self, event: Optional[AstrMessageEvent] = None) -> str:
        if event is None:
            return ""
        key = self._session_key(event)
        with self._session_model_lock:
            return str(self._session_model_overrides.get(key) or "").strip()

    def _set_session_model_override(self, event: AstrMessageEvent, label: str) -> str:
        key = self._session_key(event)
        value = str(label or "").strip()
        with self._session_model_lock:
            if not value:
                self._session_model_overrides.pop(key, None)
                return ""
            self._session_model_overrides[key] = value
            # Bound memory for long-running bots.
            while len(self._session_model_overrides) > 200:
                self._session_model_overrides.pop(next(iter(self._session_model_overrides)))
        return value

    def _match_model_label(self, raw: str) -> Optional[str]:
        text = str(raw or "").strip()
        if not text:
            return None
        labels = self._available_model_labels()
        if not labels:
            return None
        if text.isdigit():
            index = int(text) - 1
            if 0 <= index < len(labels):
                return labels[index]
            return None
        # Exact label / channel:model / bare model
        for label in labels:
            if text == label or text == label.replace("/", ":"):
                return label
        for label in labels:
            channel, _, model = label.partition("/")
            if text == model or text == f"{channel}:{model}":
                return label
        # Prefix / contains (single hit only)
        hits = [label for label in labels if text in label or label.endswith(f"/{text}")]
        if len(hits) == 1:
            return hits[0]
        return None

    def _resolve_generation_targets(
        self,
        event: Optional[AstrMessageEvent] = None,
        targets: Optional[List[ImageModelTarget]] = None,
    ) -> List[ImageModelTarget]:
        if targets is not None:
            return list(targets)
        all_targets = self.config.get_prioritized_targets()
        override = self._get_session_model_override(event)
        if not override or not all_targets:
            return all_targets
        preferred: List[ImageModelTarget] = []
        for target in all_targets:
            if target.label == override:
                preferred.append(target)
                break
        if not preferred and "/" in override:
            channel, _, model = override.partition("/")
            for target in all_targets:
                if target.channel_name == channel and target.model == model:
                    preferred.append(target)
                    break
        if not preferred:
            return all_targets
        rest = [target for target in all_targets if target.label != preferred[0].label]
        return preferred + rest

    def _list_image_tasks_for_session(
        self,
        session_key: str = "",
        *,
        include_finished: bool = False,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        with self._web_task_lock:
            items = list(self._web_tasks.values())
        active_status = {"queued", "running"}
        rows: List[Dict[str, Any]] = []
        for task in sorted(items, key=lambda item: float(item.get("created_ts") or 0), reverse=True):
            owner = str(task.get("owner_session") or "")
            if session_key and owner and owner != session_key:
                continue
            if not include_finished and task.get("status") not in active_status:
                continue
            # Hide pure web-test tasks from chat unless same session web owner is empty and source command
            source = str(task.get("source") or "")
            if session_key and source.startswith("web") and owner != session_key:
                continue
            rows.append(copy.deepcopy(task))
            if len(rows) >= max(1, limit):
                break
        return [redact_sensitive_data(row) for row in rows]

    def _format_task_list_text(self, tasks: List[Dict[str, Any]]) -> str:
        if not tasks:
            return "现在没有进行中的出图/视频任务。"
        lines = ["进行中的任务："]
        for index, task in enumerate(tasks, 1):
            task_id = str(task.get("task_id") or "")
            status = str(task.get("status") or "")
            status_cn = {"queued": "排队", "running": "进行中", "succeeded": "完成", "failed": "失败", "cancelled": "已取消"}.get(status, status)
            req = task.get("request_data") if isinstance(task.get("request_data"), dict) else {}
            kind = str(req.get("kind") or "")
            if not kind:
                source = str(task.get("source") or "")
                kind = "视频" if "视频" in source or "video" in source.lower() else "出图"
            prompt = str(req.get("original_prompt") or req.get("prompt") or req.get("mode") or "")[:40]
            lines.append(f"{index}. {task_id} [{status_cn}/{kind}] {prompt}")
        lines.append("查看：/生图任务 编号或任务号；取消：/生图取消 …")
        return "\n".join(lines)

    def _format_task_detail_text(self, task: Dict[str, Any]) -> str:
        req = task.get("request_data") if isinstance(task.get("request_data"), dict) else {}
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        status = str(task.get("status") or "")
        status_cn = {"queued": "排队", "running": "绘制中", "succeeded": "完成", "failed": "失败", "cancelled": "已取消"}.get(status, status)
        lines = [
            f"任务 {task.get('task_id')}",
            f"状态：{status_cn}",
            f"说明：{str(req.get('original_prompt') or req.get('prompt') or '')[:120]}",
        ]
        if task.get("error"):
            lines.append(f"原因：{task.get('error')}")
        if result.get("used_model"):
            lines.append(f"模型：{result.get('used_model')}")
        if result.get("files"):
            lines.append(f"图片：{len(result.get('files') or [])} 张")
        if task.get("status") in {"queued", "running"}:
            lines.append(f"已用时：{task.get('running_seconds', 0)} 秒")
        return "\n".join(lines)

    def cancel_image_task(
        self,
        task_id: str,
        *,
        session_key: str = "",
        is_admin: bool = False,
    ) -> str:
        tid = str(task_id or "").strip()
        if not tid:
            raise ValueError("请提供任务ID")
        with self._web_task_lock:
            task = self._web_tasks.get(tid)
            if not task:
                # allow short numeric index against recent active? handled by caller
                raise ValueError("任务不存在或已清理")
            owner = str(task.get("owner_session") or "")
            if owner and session_key and owner != session_key and not is_admin:
                raise PermissionError("不能取消其他会话的生图任务")
            status = str(task.get("status") or "")
            if status in {"succeeded", "failed", "cancelled"}:
                return f"这单已经结束了（{status}），不用再取消"
            task["cancel_requested"] = True
            now = time.time()
            task["status"] = "cancelled"
            task["success"] = False
            task["error"] = "任务已取消"
            task["updated_ts"] = now
            task["updated_at"] = self._web_task_timestamp()
            task["finished_ts"] = now
            task["finished_at"] = self._web_task_timestamp()
            self._persist_web_tasks_locked()
            runtime_task = getattr(self, "_runtime_generation_tasks", {}).get(tid)
            if runtime_task is not None and not runtime_task.done():
                runtime_task.cancel()
            return f"已立即取消 {tid}"

    def _task_cancel_requested(self, task_id: str) -> bool:
        with self._web_task_lock:
            task = self._web_tasks.get(task_id)
            return bool(task and task.get("cancel_requested"))

    def start_command_image_task(
        self,
        event: AstrMessageEvent,
        *,
        source: str,
        summary: Dict[str, Any],
        runner,
    ) -> Dict[str, Any]:
        """Queue a chat-side generation job and return immediately (targets 08/13)."""
        loop = getattr(self, "loop", None) or asyncio.get_running_loop()
        session_key = self._session_key(event)
        request_summary = dict(summary or {})
        force_regenerate = bool(request_summary.get("force_regenerate") or request_summary.get("force"))
        fingerprint = self._request_fingerprint(request_summary, session_key)
        with self._web_task_lock:
            if not force_regenerate:
                duplicate = self._find_recent_duplicate_task_locked(fingerprint)
                if duplicate and duplicate.get("owner_session") == session_key:
                    duplicate["deduplicated"] = True
                    return redact_sensitive_data(duplicate)
            self._web_task_seq += 1
            task_id = f"cmd-{int(time.time() * 1000)}-{self._web_task_seq}"
            now = time.time()
            self._web_tasks[task_id] = {
                "task_id": task_id,
                "status": "queued",
                "success": None,
                "error": "",
                "created_ts": now,
                "updated_ts": now,
                "created_at": self._web_task_timestamp(),
                "updated_at": self._web_task_timestamp(),
                "request_data": redact_sensitive_data(request_summary),
                "result": None,
                "source": source,
                "owner_session": session_key,
                "owner_user_id": event_user_id(event),
                "cancel_requested": False,
                "request_fingerprint": fingerprint,
                "deduplicated": False,
            }
            self._prune_web_tasks_locked()
            self._persist_web_tasks_locked()
        runtime_task = asyncio.create_task(self._run_command_image_task(task_id, event, runner))
        runtime_tasks = getattr(self, "_runtime_generation_tasks", None)
        if runtime_tasks is None:
            runtime_tasks = {}
            self._runtime_generation_tasks = runtime_tasks
        runtime_tasks[task_id] = runtime_task
        runtime_task.add_done_callback(
            lambda _task, tid=task_id: getattr(self, "_runtime_generation_tasks", {}).pop(tid, None)
        )
        return self.get_web_image_task(task_id)

    async def _run_video_generation(
        self,
        event: AstrMessageEvent,
        prompt: str,
        refs: List[ImageReference],
        *,
        source: str = "command-video",
        duration: Optional[int] = None,
    ) -> Dict[str, Any]:
        targets = list(self.config.get_prioritized_video_targets())
        if not getattr(self.config, "video_enable", True):
            return {"success": False, "error": "视频功能已关闭，请在配置里打开 video.enable"}
        if not targets:
            return {"success": False, "error": "还没有可用的视频渠道，请先在配置里添加并启用 video_channels"}
        # Skip malformed targets without blocking later configured video channels.
        valid_targets: List[ImageModelTarget] = []
        invalid_messages: List[str] = []
        for candidate in targets:
            report = preflight_video_channel(
                {
                    "name": candidate.channel_name,
                    "base_url": candidate.base_url,
                    "api_key": candidate.api_key,
                    "api_keys": candidate.api_keys,
                    "model": candidate.model,
                    "enabled_models": [candidate.model] if candidate.model else [],
                    "enabled": True,
                }
            )
            if report.get("ok"):
                valid_targets.append(candidate)
            else:
                invalid_messages.append(str(report.get("message") or candidate.label))
        if not valid_targets:
            return {"success": False, "error": invalid_messages[0] if invalid_messages else "视频渠道配置不完整"}
        targets = valid_targets

        video_prompt = str(prompt or "").strip()
        prompt_en_meta = {}
        if self._prompt_en_needed(video_prompt, media="video"):
            translated, prompt_en_meta = await self._translate_prompt_to_english(
                video_prompt, media="video", event=event
            )
            if prompt_en_meta.get("applied") and translated:
                video_prompt = translated

        req = VideoGenerateRequest(
            prompt=video_prompt,
            images=list(refs or [])[:1],  # I2V: first frame only (big_banana style)
            duration=int(duration if duration is not None else getattr(self.config, "video_default_duration", 5) or 5),
        )
        if not req.prompt:
            return {"success": False, "error": "请写一下想生成的视频内容"}

        async with self._video_semaphore:
            async with aiohttp.ClientSession(trust_env=False) as session:
                result = await generate_video_with_fallback(
                    targets,
                    req,
                    session,
                    save_dir=self.video_dir,
                )
        if result.error or not result.video_path:
            self._record_task(
                {
                    **self._source_context(event, source),
                    "media_type": "video",
                    "success": False,
                    "error": result.error or "视频没有生成出来",
                    "prompt": req.prompt,
                    "original_prompt": prompt,
                    "request_prompt": req.prompt,
                    "used_model": result.used_model,
                    "elapsed_seconds": result.elapsed_seconds,
                    "reference_images": len(refs),
                    "request_data": {
                        "duration": req.duration,
                        "size": req.size,
                        "reference_images": len(refs),
                        "prompt_en": prompt_en_meta,
                        "request_prompt_en": req.prompt if prompt_en_meta.get("applied") else "",
                    },
                    "response_data": {"attempts": result.attempts},
                    "request_image_paths": [],
                    "generated_image_paths": [],
                    "generated_video_paths": [],
                }
            )
            return {
                "success": False,
                "error": result.error or "视频没有生成出来",
                "used_model": result.used_model,
                "attempts": result.attempts,
                "elapsed_seconds": result.elapsed_seconds,
            }
        video_rel = self._cache_relative_path(result.video_path)
        self._record_task(
            {
                **self._source_context(event, source),
                "media_type": "video",
                "success": True,
                "error": "",
                "prompt": req.prompt,
                "original_prompt": prompt,
                "request_prompt": req.prompt,
                "used_model": result.used_model,
                "elapsed_seconds": result.elapsed_seconds,
                "reference_images": len(refs),
                "request_data": {
                    "duration": req.duration,
                    "size": req.size,
                    "reference_images": len(refs),
                    "prompt_en": prompt_en_meta,
                    "request_prompt_en": req.prompt if prompt_en_meta.get("applied") else "",
                },
                "response_data": {"attempts": result.attempts, "video_url": result.video_url},
                "request_image_paths": [],
                "generated_image_paths": [],
                "generated_video_paths": [video_rel],
            }
        )
        return {
            "success": True,
            "video_path": result.video_path,
            "video_url": result.video_url,
            "used_model": result.used_model,
            "attempts": result.attempts,
            "elapsed_seconds": result.elapsed_seconds,
            "files": [result.video_path],
        }

    async def _background_video_job(
        self,
        task_id: str,
        event: AstrMessageEvent,
        prompt: str,
        refs: List[ImageReference],
        source: str,
        mode: str,
    ) -> Dict[str, Any]:
        if self._task_cancel_requested(task_id):
            return {"success": False, "error": "任务已取消", "cancelled": True}
        result = await self._run_video_generation(event, prompt, refs, source=source)
        if self._task_cancel_requested(task_id) and not result.get("success"):
            return {"success": False, "error": "任务已取消", "cancelled": True}
        if not result.get("success"):
            error = self._friendly_user_error_message(str(result.get("error") or ""), "视频没有完成")
            try:
                await event.send(event.plain_result(error))
            except Exception:
                pass
            return result
        path = str(result.get("video_path") or "")
        used = str(result.get("used_model") or "")
        elapsed = result.get("elapsed_seconds") or 0
        bits = ["视频好了。"]
        if self.config.image_show_generation_info and elapsed:
            bits.append(f"用时 {elapsed}s")
        if self.config.image_show_model_info and used:
            bits.append(f"模型 {used}")
        caption = " ".join(bits)
        try:
            await self._send_generated_video(event, path, caption=caption)
        except Exception as exc:
            logger.warning(f"[SelfieImage] 发送视频失败，尝试仅回路径: {exc}")
            try:
                await event.send(event.plain_result(f"{caption}\n文件：{path}"))
            except Exception:
                pass
            return {"success": False, "error": f"视频已生成但发送失败：{exc}", "video_path": path}
        return result

    def _parse_video_duration(self, text: str) -> Tuple[str, Optional[int]]:
        raw = str(text or "")
        duration = None
        match = re.search(r"(?:--duration|--dur|-d)\s*(\d{1,2})\b", raw, flags=re.I)
        if match:
            duration = max(1, min(60, int(match.group(1))))
            raw = (raw[: match.start()] + raw[match.end() :]).strip()
        return raw, duration

    async def _handle_video_command(
        self,
        event: AstrMessageEvent,
        command_name: Any,
        fallback: str,
        *,
        mode: str = "auto",
    ) -> AsyncGenerator[Any, None]:
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        raw_message = extract_command_message(event, command_name, fallback).strip()
        prompt, duration = self._parse_video_duration(raw_message)
        refs = await self._event_reference_images(
            event,
            include_at_avatar=False,
            allow_context_fallback=True,
            include_persona=False,
        )
        if mode == "t2v":
            refs = []
        elif not refs:
            persona_ref = self._video_persona_reference()
            if persona_ref:
                refs = [persona_ref]
            elif mode == "i2v":
                yield event.plain_result("图生视频需要附图、引用图片，或先设置当前形象图作为首帧。")
                return
        if mode == "auto" and refs:
            mode_label = "图生视频"
        elif mode == "i2v":
            mode_label = "图生视频"
        else:
            mode_label = "文生视频"
            refs = []
        if not prompt:
            yield event.plain_result(f"请写上{mode_label}的内容，例如：/{command_name if isinstance(command_name, str) else '视频'} 小猫在草地上跑")
            return

        progress = f"收到，开始{mode_label}（通常比出图慢，请稍等）。"
        if duration:
            progress += f" 时长约 {duration}s。"

        async def runner_with_duration(task_id: str) -> Dict[str, Any]:
            if self._task_cancel_requested(task_id):
                return {"success": False, "error": "任务已取消", "cancelled": True}
            result = await self._run_video_generation(
                event,
                prompt,
                refs,
                source=f"command-{mode_label}",
                duration=duration,
            )
            if not result.get("success"):
                error = self._friendly_user_error_message(str(result.get("error") or ""), "视频没有完成")
                try:
                    await event.send(event.plain_result(error))
                except Exception:
                    pass
                return result
            path = str(result.get("video_path") or "")
            used = str(result.get("used_model") or "")
            elapsed = result.get("elapsed_seconds") or 0
            bits = ["视频好了。"]
            if self.config.image_show_generation_info and elapsed:
                bits.append(f"用时 {elapsed}s")
            if self.config.image_show_model_info and used:
                bits.append(f"模型 {used}")
            try:
                await self._send_generated_video(event, path, caption=" ".join(bits))
            except Exception as exc:
                try:
                    await event.send(event.plain_result(f"{' '.join(bits)}\n文件：{path}"))
                except Exception:
                    pass
                return {"success": False, "error": f"视频已生成但发送失败：{exc}", "video_path": path}
            return result

        task = self.start_command_image_task(
            event,
            source=f"command-{mode_label}",
            summary={
                "prompt": prompt,
                "mode": mode_label,
                "duration": duration or getattr(self.config, "video_default_duration", 5),
                "has_image": bool(refs),
                "kind": "video",
            },
            runner=runner_with_duration,
        )
        yield event.plain_result(progress)

    @filter.command("视频")
    async def cmd_video(self, event: AstrMessageEvent, p1: str = "", p2: str = "", p3: str = "") -> AsyncGenerator[Any, None]:
        """写想要的动态出视频。带图/引用图优先作首帧；没图时用当前形象图作首帧。"""
        fallback = " ".join(item for item in [p1, p2, p3] if item).strip()
        async for item in self._handle_video_command(event, "视频", fallback, mode="auto"):
            yield item

    @filter.command("文生视频")
    async def cmd_t2v(self, event: AstrMessageEvent, p1: str = "", p2: str = "", p3: str = "") -> AsyncGenerator[Any, None]:
        """只用文字出视频，不带图、也不使用形象图。"""
        fallback = " ".join(item for item in [p1, p2, p3] if item).strip()
        async for item in self._handle_video_command(event, "文生视频", fallback, mode="t2v"):
            yield item

    @filter.command("图生视频")
    async def cmd_i2v(self, event: AstrMessageEvent, p1: str = "", p2: str = "", p3: str = "") -> AsyncGenerator[Any, None]:
        """按图出视频。附图/引用图优先作首帧；没图时用当前形象图。"""
        fallback = " ".join(item for item in [p1, p2, p3] if item).strip()
        async for item in self._handle_video_command(event, "图生视频", fallback, mode="i2v"):
            yield item

    async def _run_command_image_task(self, task_id: str, event: AstrMessageEvent, runner) -> None:
        if self._task_cancel_requested(task_id):
            self._set_web_image_task(
                task_id,
                status="cancelled",
                success=False,
                error="任务已取消",
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
            return
        self._set_web_image_task(
            task_id,
            status="running",
            started_ts=time.time(),
            started_at=self._web_task_timestamp(),
        )
        try:
            if self._task_cancel_requested(task_id):
                raise RuntimeError("任务已取消")
            result = await runner(task_id)
            result = self._normalize_generation_result(result)
            result = redact_sensitive_data(result)
            if self._task_cancel_requested(task_id) and not result.get("success"):
                result = {"success": False, "error": "任务已取消", "cancelled": True}
            success = bool(result.get("success"))
            cancelled = bool(result.get("cancelled")) or str(result.get("error") or "").find("取消") >= 0
            error = "" if success else redact_sensitive_text(str(result.get("error") or ("任务已取消" if cancelled else "这次没顺好")))
            self._set_web_image_task(
                task_id,
                status="cancelled" if cancelled and not success else str(result.get("status") or ("succeeded" if success else "failed")),
                success=success,
                error=error,
                requested_count=result.get("requested_count", 1),
                succeeded_count=result.get("succeeded_count", 0),
                failed_count=result.get("failed_count", 0),
                result=result,
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
        except asyncio.CancelledError:
            self._set_web_image_task(
                task_id,
                status="cancelled",
                success=False,
                error="任务已取消",
                result={"success": False, "error": "任务已取消", "cancelled": True},
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
            return
        except Exception as exc:
            error = redact_sensitive_text(str(exc))
            cancelled = "取消" in error
            self._set_web_image_task(
                task_id,
                status="cancelled" if cancelled else "failed",
                success=False,
                error=error,
                result={"success": False, "error": error},
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
            # Ensure monitor still gets a row when runner crashes before generate_images records.
            try:
                task_meta = {}
                try:
                    with self._web_task_lock:
                        task_meta = dict((self._web_tasks or {}).get(task_id) or {})
                except Exception:
                    task_meta = {}
                source = str(task_meta.get("source") or "command-task")
                prompt = ""
                summary = task_meta.get("request_data") or task_meta.get("summary") or {}
                if isinstance(summary, dict):
                    prompt = str(summary.get("prompt") or summary.get("action") or "")
                self._record_task(
                    {
                        "source": source,
                        "source_label": source,
                        "success": False,
                        "error": error,
                        "prompt": prompt,
                        "original_prompt": prompt,
                        "request_prompt": prompt,
                        "used_model": "",
                        "elapsed_seconds": 0,
                        "reference_images": 0,
                        "request_data": {"stage": "task_exception"},
                        "response_data": {"success": False, "stage": "task_exception", "error": error},
                        "request_image_paths": [],
                        "generated_image_paths": [],
                        "attempts": [],
                    }
                )
            except Exception as rec_exc:
                logger.warning(f"[SelfieImage] 任务异常落库失败: {rec_exc}")
            try:
                await event.send(event.plain_result(self._friendly_user_error_message(error, "生图没有完成")))
            except Exception as send_exc:
                logger.warning(f"[SelfieImage] 后台任务失败通知发送失败: {send_exc}")



    def _image_inflight_limit(self) -> int:
        return max(1, min(10, int(getattr(self.config, "image_max_concurrent_tasks", 1) or 1)))

    def _ensure_image_batch_gate(self) -> asyncio.Semaphore:
        gate = getattr(self, "_image_batch_gate", None)
        if gate is None or not isinstance(gate, asyncio.Semaphore):
            self._image_batch_gate = asyncio.Semaphore(self._image_inflight_limit())
            gate = self._image_batch_gate
        self._selfie_batch_gate = gate
        return gate

    def _image_batch_queue_expected(self, total: int = 1) -> bool:
        """Whether the first shot must wait for a currently occupied slot.

        ``total`` is intentionally not used here.  A batch of ten is valid
        with concurrency three and must not be labelled queued merely because
        ten is larger than the configured concurrency.
        """
        gate = self._ensure_image_batch_gate()
        return int(getattr(gate, "_value", 0)) <= 0

    async def _acquire_image_slot(self, task_id: str) -> Optional[asyncio.Semaphore]:
        """Acquire one slot; a batch may be larger than the global limit."""
        gate = self._ensure_image_batch_gate()
        while True:
            if self._task_cancel_requested(task_id):
                return None
            try:
                await asyncio.wait_for(gate.acquire(), timeout=1.0)
                return gate
            except asyncio.TimeoutError:
                continue

    async def _run_counted_generation_shots(
        self,
        *,
        task_id: str,
        event: AstrMessageEvent,
        total: int,
        fail_label: str,
        run_one,
        log_prefix: str,
    ) -> Dict[str, Any]:
        """Queue-friendly batch: at most inflight shots generating; send as each finishes."""
        total = max(1, int(total))
        inflight = min(self._image_inflight_limit(), total)
        sem = asyncio.Semaphore(inflight)
        send_lock = asyncio.Lock()
        stop = False
        skipped_shots = 0
        all_files: List[str] = []
        used_model = ""
        last_elapsed = 0.0
        failed_at = 0
        last_failure_error = ""
        cancelled = False
        succeeded_shots = 0

        async def one(index: int) -> None:
            nonlocal stop, skipped_shots, used_model, last_elapsed, failed_at, cancelled, succeeded_shots, last_failure_error
            async with sem:
                if stop or self._task_cancel_requested(task_id):
                    if self._task_cancel_requested(task_id):
                        cancelled = True
                    return
                logger.info(f"[SelfieImage] {log_prefix} {index + 1}/{total} inflight={inflight} task={task_id}")
                slot_gate = await self._acquire_image_slot(task_id)
                if slot_gate is None:
                    cancelled = True
                    return
                try:
                    result = await run_one(index)
                finally:
                    slot_gate.release()
            async with send_lock:
                if stop:
                    return
                if self._task_cancel_requested(task_id):
                    cancelled = True
                    stop = True
                    return
                if not result.get("success"):
                    raw_err = str(result.get("failure_reason") or result.get("error") or "")
                    if not raw_err:
                        attempts = result.get("attempts") or []
                        if isinstance(attempts, list) and attempts:
                            last = attempts[-1] if isinstance(attempts[-1], dict) else {}
                            raw_err = str(last.get("error_user_message") or last.get("error") or "")
                    last_failure_error = raw_err
                    error = self._friendly_user_error_message(raw_err, fail_label)
                    skipped_shots += 1
                    mode, skip_max = self._batch_failure_policy()
                    has_remaining = index < total
                    will_continue = has_remaining and (
                        mode == "skip" or (mode == "skip_max" and skipped_shots <= skip_max)
                    )
                    msg = self._batch_shot_fail_text(
                        index=index + 1,
                        total=total,
                        done_files=len(all_files),
                        error=error,
                        mode=mode,
                        skipped=skipped_shots,
                        skip_max=skip_max,
                        will_continue=will_continue,
                    )
                    try:
                        await event.send(event.plain_result(msg))
                    except Exception:
                        pass
                    if not will_continue:
                        stop = True
                        failed_at = index + 1
                    return
                files = list(result.get("files") or [])
                used_model = str(result.get("used_model") or used_model)
                last_elapsed = float(result.get("elapsed_seconds") or last_elapsed)
                if files:
                    self._record_generated_images(event, 1)
                    await self._send_generated_images(event, files)
                    all_files.extend(files)
                    succeeded_shots += 1
                info = self._batch_success_text(
                    self._build_success_text(last_elapsed, len(files), used_model, event),
                    index + 1,
                    total,
                )
                if info:
                    try:
                        await event.send(event.plain_result(info))
                    except Exception:
                        pass

        await asyncio.gather(*(one(i) for i in range(total)))
        if cancelled:
            return self._normalize_generation_result(
                {
                    "success": False,
                    "error": "任务已取消",
                    "cancelled": True,
                    "files": all_files,
                    "batch_total": total,
                    "succeeded_count": succeeded_shots,
                    "failed_count": skipped_shots,
                },
                total,
            )
        if failed_at:
            return self._normalize_generation_result({
                "success": False,
                "error": last_failure_error or fail_label or "生图没有完成",
                "files": all_files,
                "batch_total": total,
                "batch_failed_at": failed_at,
                "batch_skipped": skipped_shots,
                "succeeded_count": succeeded_shots,
                "failed_count": skipped_shots,
            }, total)
        return self._normalize_generation_result({
            "success": skipped_shots == 0,
            "files": all_files,
            "used_model": used_model,
            "elapsed_seconds": last_elapsed,
            "batch_total": total,
            "batch_skipped": skipped_shots,
            "succeeded_count": succeeded_shots,
            "failed_count": skipped_shots,
        }, total)

    def _batch_failure_policy(self) -> tuple[str, int]:
        """Return (mode, skip_max). mode: stop | skip | skip_max."""
        mode = str(getattr(self.config, "image_batch_on_failure", "skip") or "skip").strip().lower()
        if mode in {"continue", "skip_continue", "skip-continue"}:
            mode = "skip"
        if mode not in {"stop", "skip", "skip_max"}:
            mode = "skip"
        try:
            skip_max = int(getattr(self.config, "image_batch_skip_max", 2) or 2)
        except Exception:
            skip_max = 2
        skip_max = max(0, min(8, skip_max))
        return mode, skip_max

    def _normalize_generation_result(self, result: Any, requested_count: int = 1) -> Dict[str, Any]:
        """Add stable counts/status while accepting legacy result dictionaries."""
        data = copy.deepcopy(result) if isinstance(result, dict) else {"success": False, "error": "无效结果"}
        files = list(data.get("files") or data.get("image_paths") or data.get("generated_image_paths") or [])
        try:
            requested = max(1, int(data.get("batch_total") or requested_count or 1))
        except (TypeError, ValueError):
            requested = 1
        try:
            succeeded = max(0, min(requested, int(data.get("succeeded_count") or len(files))))
        except (TypeError, ValueError):
            succeeded = min(requested, len(files))
        try:
            failed = max(0, int(data.get("failed_count") or data.get("batch_skipped") or 0))
        except (TypeError, ValueError):
            failed = 0
        if not succeeded and bool(data.get("success")):
            succeeded = requested
        if succeeded + failed > requested:
            failed = max(0, requested - succeeded)
        cancelled = bool(data.get("cancelled"))
        if cancelled:
            status = "cancelled"
        elif succeeded >= requested and not failed:
            status = "succeeded"
        elif succeeded:
            status = "partial_success"
        else:
            status = "failed"
        data.update(
            {
                "files": files,
                "requested_count": requested,
                "succeeded_count": succeeded,
                "failed_count": failed,
                "status": status,
                "success": status == "succeeded",
            }
        )
        return data

    def _batch_shot_fail_text(
        self,
        *,
        index: int,
        total: int,
        done_files: int,
        error: str,
        mode: str,
        skipped: int,
        skip_max: int,
        will_continue: bool,
    ) -> str:
        """Single-cause progress line when one shot in a batch fails."""
        single_shot = total <= 1
        base = "这张没生成成功" if single_shot else f"第 {index}/{total} 张没出成"
        detail = str(error or "").strip()
        if detail:
            # keep short
            detail = re.sub(r"\s+", " ", detail)
            if len(detail) > 80:
                detail = detail[:79] + "…"
            base = f"{base}：{detail}"
        if single_shot:
            return base
        base = f"{base}。已出 {done_files} 张"
        if will_continue:
            if mode == "skip_max":
                base = f"{base}，已跳过 {skipped}/{skip_max}，继续后面的"
            else:
                base = f"{base}，继续后面的"
        else:
            left = max(0, total - index)
            if left:
                base = f"{base}，后面 {left} 张先不跑了"
            else:
                base = f"{base}"
        return base

    async def _batch_shot_fail_message(
        self,
        event: AstrMessageEvent,
        *,
        index: int,
        total: int,
        done_files: int,
        error: str,
        will_continue: bool,
    ) -> str:
        """Prefer a soft LLM sentence while keeping a deterministic fallback."""
        from .prompt_templates import build_batch_failure_llm_prompt

        reason = re.sub(r"\s+", " ", str(error or "").strip())[:160]
        llm_prompt = build_batch_failure_llm_prompt(
            bot_name=self._bot_display_name(),
            reason=reason,
            index=index,
            total=total,
            done_files=done_files,
            will_continue=will_continue,
        )
        reply = self._strip_llm_short_reply(await self._call_text_llm(event, llm_prompt, timeout=6))
        if reply and len(reply) <= 90 and "可能" not in reply:
            return reply
        return self._batch_shot_fail_text(
            index=index,
            total=total,
            done_files=done_files,
            error=reason,
            mode="skip" if will_continue else "stop",
            skipped=0,
            skip_max=0,
            will_continue=will_continue,
        )

    async def _background_draw_batches(
        self,
        task_id: str,
        event: AstrMessageEvent,
        prompt: str,
        aspect: str,
        resolution: str,
        refs: List[ImageReference],
        source: str,
        requested_count: int,
        *,
        passthrough: bool = False,
        fail_label: str = "",
    ) -> Dict[str, Any]:
        total = self._normalize_count(requested_count)

        async def run_one(index: int) -> Dict[str, Any]:
            if passthrough:
                return await self._draw_passthrough_once(event, prompt, aspect, resolution, refs, source)
            return await self._draw_once(event, prompt, aspect, resolution, refs, source)

        return await self._run_counted_generation_shots(
            task_id=task_id,
            event=event,
            total=total,
            fail_label=fail_label or self._natural_fail_fallback("image"),
            run_one=run_one,
            log_prefix="draw batch",
        )

    async def _background_selfie_batches(
        self,
        task_id: str,
        event: AstrMessageEvent,
        action: str,
        extra_refs: List[ImageReference],
        source: str,
        requested_count: int,
        aspect: str,
        resolution: str,
        fail_label: str,
        *,
        queue_notified: bool = False,
    ) -> Dict[str, Any]:
        total = self._normalize_count(requested_count)
        self._ensure_image_batch_gate()
        return await self._run_selfie_batches_unlocked(
            task_id,
            event,
            action,
            extra_refs,
            source,
            total,
            aspect,
            resolution,
            fail_label,
        )

    async def _run_selfie_batches_unlocked(
        self,
        task_id: str,
        event: AstrMessageEvent,
        action: str,
        extra_refs: List[ImageReference],
        source: str,
        requested_count: int,
        aspect: str,
        resolution: str,
        fail_label: str,
    ) -> Dict[str, Any]:
        total = self._normalize_count(requested_count)
        # 多张拍摄时逐张更换机位或姿势。
        rebuild_each = source in {
            "command-look-legs",
            "command-selfie",
            "command-look-you",
            "command-look-cos",
        } or (
            "看看腿" in str(action or "")
            or "【legs:outfit】" in str(action or "")
            or "看看COS" in str(action or "")
            or "【shot:" in str(action or "")
            or "【pose:" in str(action or "")
            or "【cos:" in str(action or "")
        )
        last_pose = ""
        last_shot = ""
        last_cos = ""
        last_cam = ""
        extra_keep = ""
        force_legwear = ""
        if rebuild_each:
            # Extra may contain full preset text with many periods — take rest of line, then strip pose/shot tags.
            m_extra = re.search(r"(?:用户补充要求优先|额外要求)[:：]\s*(.+)", str(action or ""), flags=re.S)
            if m_extra:
                extra_keep = str(m_extra.group(1) or "").strip()
                extra_keep = re.sub(r"\s*【(?:pose|shot|cos|cam|legs|wear):[a-z0-9_]+】\s*", " ", extra_keep)
                extra_keep = re.sub(r"\s+", " ", extra_keep).strip(" 。")
            # Keep user/locked legwear across rebuild rounds (extra text alone may have stripped 白丝).
            force_legwear = parse_requested_legwear(str(action or "")) or parse_requested_legwear(extra_keep)
            m_pose = re.search(r"【pose:([a-z_]+)】", str(action or ""))
            if m_pose:
                last_pose = str(m_pose.group(1) or "")
            m_shot = re.search(r"【shot:([a-z_]+)】", str(action or ""))
            if m_shot:
                last_shot = str(m_shot.group(1) or "")
            m_cos = re.search(r"【cos:([a-z0-9_]+)】", str(action or ""))
            if m_cos:
                last_cos = str(m_cos.group(1) or "")
            m_cam = re.search(r"【cam:(selfie|third)】", str(action or ""))
            if m_cam:
                last_cam = str(m_cam.group(1) or "")
        round_actions: List[str] = []
        for index in range(total):
            round_action = action
            if rebuild_each and total > 1:
                if source == "command-look-legs" or "看看腿" in str(action or "") or "【legs:outfit】" in str(action or "") or "【pose:" in str(action or ""):
                    round_action = self._build_leg_focus_action(
                        extra_keep,
                        bool(extra_refs),
                        avoid_pose=last_pose,
                        force_legwear=force_legwear,
                    )
                    m_pose = re.search(r"【pose:([a-z_]+)】", round_action)
                    if m_pose:
                        last_pose = str(m_pose.group(1) or last_pose)
                elif source == "command-look-cos" or "看看COS" in str(action or "") or "【cos:" in str(action or ""):
                    round_action = self._build_cos_look_action(
                        extra_keep,
                        bool(extra_refs),
                        avoid_id=last_cos,
                        avoid_camera=last_cam,
                    )
                    m_cos = re.search(r"【cos:([a-z0-9_]+)】", round_action)
                    if m_cos:
                        last_cos = str(m_cos.group(1) or last_cos)
                    m_cam = re.search(r"【cam:(selfie|third)】", round_action)
                    if m_cam:
                        last_cam = str(m_cam.group(1) or last_cam)
                elif source == "command-look-you" or "看看你模式" in str(action or ""):
                    round_action = self._build_third_person_look_action(
                        extra_keep,
                        bool(extra_refs),
                        avoid_shot=last_shot,
                    )
                    m_shot = re.search(r"【shot:([a-z_]+)】", round_action)
                    if m_shot:
                        last_shot = str(m_shot.group(1) or last_shot)
                else:
                    round_action = self._build_selfie_look_action(
                        extra_keep,
                        bool(extra_refs),
                        avoid_shot=last_shot,
                    )
                    m_shot = re.search(r"【shot:([a-z_]+)】", round_action)
                    if m_shot:
                        last_shot = str(m_shot.group(1) or last_shot)
            round_actions.append(round_action)

        async def run_one(index: int) -> Dict[str, Any]:
            round_action = round_actions[index]
            prompt, refs, prompt_en_meta = await self._build_selfie_prompt_and_refs_for_event(event, round_action, extra_refs)
            return await self._run_image_generation(
                prompt,
                aspect,
                resolution,
                refs,
                source=source,
                audit_user_id=event_user_id(event),
                event=event,
                original_prompt=round_action,
                prompt_en_meta=prompt_en_meta,
            )

        return await self._run_counted_generation_shots(
            task_id=task_id,
            event=event,
            total=total,
            fail_label=fail_label,
            run_one=run_one,
            log_prefix="selfie batch",
        )

    def _validate_web_test_selection(self, payload: Dict[str, Any]) -> None:
        channel_name = str(payload.get("channel") or "").strip()
        model_name = str(payload.get("model") or "").strip()
        media_type = str(payload.get("media_type") or "image").strip().lower()
        if not channel_name:
            return
        is_video = media_type == "video"
        channels = self.config.video_channels if is_video else self.config.image_channels
        kind_label = "视频" if is_video else "生图"
        matching_channels = [channel for channel in channels if channel.name == channel_name]
        if not matching_channels:
            raise RuntimeError(f"{kind_label}渠道 {channel_name} 不存在")
        if not any(channel.enabled for channel in matching_channels):
            raise RuntimeError(f"{kind_label}渠道 {channel_name} 已禁用，渠道测试不会调用禁用渠道")
        channel = next((item for item in matching_channels if item.enabled), matching_channels[0])
        if is_video:
            report = preflight_video_channel(
                {
                    "name": channel.name,
                    "provider_type": channel.provider_type,
                    "base_url": channel.base_url,
                    "api_key": channel.api_key,
                    "api_keys": channel.api_keys,
                    "model": channel.model,
                    "enabled_models": channel.enabled_models,
                    "timeout": channel.timeout,
                    "enabled": channel.enabled,
                    "proxy": channel.proxy,
                }
            )
        else:
            from .models import preflight_image_channel

            report = preflight_image_channel(
                {
                    "name": channel.name,
                    "provider_type": channel.provider_type,
                    "base_url": channel.base_url,
                    "api_key": channel.api_key,
                    "model": channel.model,
                    "enabled_models": channel.enabled_models,
                    "timeout": channel.timeout,
                    "enabled": channel.enabled,
                    "proxy": channel.proxy,
                },
                kind="image",
            )
        if not report.get("ok"):
            raise RuntimeError(report.get("message") or f"{kind_label}渠道配置预检未通过")
        enabled = channel.enabled_models or ([channel.model] if channel.model else [])
        if model_name and model_name not in enabled:
            raise RuntimeError(f"渠道 {channel_name} 未启用模型 {model_name}，请先在渠道管理中启用并保存")

    async def web_test_image(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        channel_name = str(payload.get("channel") or "").strip()
        model_name = str(payload.get("model") or "").strip()
        raw_images = list(payload.get("images") or [])
        if payload.get("image"):
            raw_images.append(payload.get("image"))

        original_prompt = str(payload.get("prompt") or "").strip() or "看着镜头自然自拍"
        aspect = str(payload.get("aspect_ratio") or self.config.image_default_aspect_ratio or "9:16")
        resolution = str(payload.get("resolution") or self.config.image_default_resolution or "1K")
        prompt_enhance_raw = payload.get("prompt_enhance", True)
        prompt_enhance = not (
            prompt_enhance_raw is False
            or str(prompt_enhance_raw).strip().lower() in {"false", "0", "no", "off", "关闭", "否"}
        )
        request_summary = {
            "original_prompt": original_prompt,
            "channel": channel_name,
            "model": model_name,
            "aspect_ratio": aspect,
            "resolution": resolution,
            "prompt_enhance": prompt_enhance,
            "use_selfie_reference": bool(payload.get("use_selfie_reference")),
            "raw_reference_image_count": len(raw_images),
        }
        prompt_en_meta: Optional[Dict[str, Any]] = None

        try:
            self._validate_web_test_selection(payload)
            target = self._find_image_target(channel_name, model_name)
            if not target:
                raise RuntimeError("未找到指定生图模型")

            max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
            refs: List[ImageReference] = []
            extra_refs: List[ImageReference] = []
            for raw in raw_images:
                data, mime = data_url_to_bytes(str(raw or ""))
                if not data:
                    continue
                if len(data) > max_bytes:
                    raise RuntimeError(f"参考图过大，最大允许 {self.config.image_max_image_size_mb}MB")
                extra_refs.append(ImageReference(data=data, mime_type=normalize_image_mime(mime or detect_mime_by_bytes(data))))

            if not prompt_enhance:
                refs = list(extra_refs)
                if payload.get("use_selfie_reference"):
                    persona_ref = self._persona_identity_reference()
                    if not persona_ref:
                        raise RuntimeError("当前未设置 AI 自拍形象参考图，且未启用 logo 回退；请先上传形象图、开启「无形象图用 logo」，或取消使用自拍形象参考图")
                    refs.insert(0, persona_ref)
                prompt = original_prompt
            elif payload.get("use_selfie_reference"):
                prompt, refs, prompt_en_meta = await self._build_selfie_prompt_and_refs_for_event(
                    None,
                    original_prompt,
                    extra_refs,
                )
                if not refs:
                    raise RuntimeError("当前未设置 AI 自拍形象参考图，请先上传形象图，或取消使用自拍形象参考图")
            else:
                refs = extra_refs
                user_prompt = original_prompt
                prompt_en_meta = {"enabled": False, "applied": False, "scope": "user_text_only"}
                if self._prompt_en_needed(user_prompt, media="image"):
                    translated, prompt_en_meta = await self._translate_prompt_to_english(
                        user_prompt,
                        media="image",
                        event=None,
                    )
                    if prompt_en_meta.get("applied") and translated:
                        user_prompt = translated
                prompt = build_prompt_with_reference_instruction(
                    user_prompt,
                    refs,
                    language="en" if self.config.image_enable_image_prompt_en else "zh",
                )

            result = await self._run_image_generation(
                prompt=prompt,
                aspect_ratio=aspect,
                resolution=resolution,
                refs=refs,
                targets=[target],
                source="web-test",
                original_prompt=original_prompt,
                event=None,
                max_attempts=1,
                allow_compat_retry=False,
                prompt_en_meta=prompt_en_meta,
            )
        except Exception as exc:
            error = str(exc)
            response_data = {"success": False, "stage": "web_test_preflight", "error": error}
            self._record_task(
                {
                    **self._source_context(None, "web-test"),
                    "success": False,
                    "error": error,
                    "prompt": original_prompt,
                    "original_prompt": original_prompt,
                    "request_prompt": original_prompt,
                    "used_model": model_name,
                    "elapsed_seconds": 0,
                    "reference_images": len(raw_images),
                    "request_data": request_summary,
                    "response_data": response_data,
                    "request_image_paths": [],
                    "generated_image_paths": [],
                }
            )
            raise

        if not result.get("success"):
            return {
                "success": False,
                "error": str(result.get("error") or "这次没顺好"),
                "used_model": result.get("used_model"),
                "elapsed_seconds": round(float(result.get("elapsed_seconds") or 0), 2),
                "reference_images": len(refs),
                "original_prompt": original_prompt,
                "final_prompt": prompt,
                "request_data": result.get("request_data") or request_summary,
                "response_data": result.get("response_data") or {},
                "request_image_paths": result.get("request_image_paths") or [],
                "generated_image_paths": result.get("image_paths") or [],
            }

        return {
            "success": True,
            "used_model": result.get("used_model"),
            "elapsed_seconds": round(float(result.get("elapsed_seconds") or 0), 2),
            "reference_images": len(refs),
            "original_prompt": original_prompt,
            "final_prompt": prompt,
            "request_data": result.get("request_data") or {},
            "response_data": result.get("response_data") or {},
            "request_image_paths": result.get("request_image_paths") or [],
            "generated_image_paths": result.get("image_paths") or [],
        }

    async def web_test_video(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        channel_name = str(payload.get("channel") or "").strip()
        model_name = str(payload.get("model") or "").strip()
        prompt = str(payload.get("prompt") or "").strip() or "一段自然流畅的短视频"
        aspect = str(payload.get("aspect_ratio") or "16:9").strip() or "16:9"
        duration = max(1, min(60, int(payload.get("duration") or self.config.video_default_duration or 5)))
        target = self._find_video_target(channel_name, model_name)
        if not target:
            raise RuntimeError("未找到指定视频模型")

        raw_images = list(payload.get("images") or [])
        if payload.get("image"):
            raw_images.append(payload.get("image"))
        refs: List[ImageReference] = []
        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        for raw in raw_images[:1]:
            data, mime = data_url_to_bytes(str(raw or ""))
            if not data:
                continue
            if len(data) > max_bytes:
                raise RuntimeError(f"参考图过大，最大允许 {self.config.image_max_image_size_mb}MB")
            refs.append(ImageReference(data=data, mime_type=normalize_image_mime(mime or detect_mime_by_bytes(data))))
        if payload.get("use_selfie_reference") and not refs:
            persona_ref = self._video_persona_reference()
            if not persona_ref:
                raise RuntimeError("当前未设置 AI 自拍形象参考图，请先上传形象图，或取消使用自拍形象参考图")
            refs = [persona_ref]

        started = time.monotonic()
        req = VideoGenerateRequest(
            prompt=prompt,
            images=refs,
            duration=duration,
            size=aspect,
        )
        async with self._video_semaphore:
            async with aiohttp.ClientSession(trust_env=False) as session:
                result = await generate_video_with_fallback([target], req, session, save_dir=self.video_dir)
        elapsed = round(float(result.elapsed_seconds or (time.monotonic() - started)), 2)
        record = {
            **self._source_context(None, "web-video-test"),
            "media_type": "video",
            "success": not bool(result.error) and bool(result.video_path),
            "error": result.error or "",
            "prompt": prompt,
            "original_prompt": prompt,
            "request_prompt": prompt,
            "used_model": result.used_model or target.label,
            "elapsed_seconds": elapsed,
            "reference_images": len(refs),
            "request_data": self._summarize_web_test_payload(payload),
            "response_data": {"attempts": result.attempts, "video_url": result.video_url},
            "request_image_paths": [],
            "generated_image_paths": [],
            "generated_video_paths": [self._cache_relative_path(result.video_path)] if result.video_path else [],
        }
        self._record_task(record)
        if result.error or not result.video_path:
            return {
                "success": False,
                "error": result.error or "视频没有生成出来",
                "used_model": result.used_model or target.label,
                "elapsed_seconds": elapsed,
                "attempts": result.attempts,
                "generated_video_paths": [],
            }
        return {
            "success": True,
            "used_model": result.used_model or target.label,
            "elapsed_seconds": elapsed,
            "reference_images": len(refs),
            "video_url": result.video_url,
            "generated_video_paths": [self._cache_relative_path(result.video_path)],
            "attempts": result.attempts,
        }

    async def web_refresh_image_models(self, payload: Dict[str, Any]) -> List[str]:
        channel_payload = payload.get("channel") if isinstance(payload.get("channel"), dict) else payload
        base_url = str(channel_payload.get("base_url") or channel_payload.get("baseUrl") or "").strip()
        api_key = str(channel_payload.get("api_key") or channel_payload.get("apiKey") or "").strip()
        provider_type = provider_type_from_channel_payload(channel_payload)
        proxy = str(channel_payload.get("proxy") or "").strip()
        if provider_type == "agnes":
            return ["agnes-image-2.1-flash"]
        candidates = build_model_list_urls(base_url, provider_type)
        if not candidates:
            raise RuntimeError("base_url 为空")
        headers = {"Accept": "application/json"}
        if provider_type == "gemini" and api_key:
            headers["x-goog-api-key"] = api_key
        elif api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        errors: List[str] = []
        async with aiohttp.ClientSession(trust_env=False) as base_session:
            async with channel_client_session(proxy, base_session) as session:
                request_proxy = http_proxy_url(proxy)
                for url in candidates:
                    safe_url = redact_sensitive_text(url)
                    try:
                        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12), proxy=request_proxy) as response:
                            if response.status >= 400:
                                errors.append(f"{safe_url}: HTTP {response.status} {redact_sensitive_text(await response.text())[:200]}")
                                continue
                            data = await response.json(content_type=None)
                        models = self._extract_model_ids(data)
                        if models:
                            return models
                        errors.append(f"{safe_url}: 返回成功但未识别到模型")
                    except Exception as exc:
                        errors.append(f"{safe_url}: {redact_sensitive_text(str(exc))}")
        raise RuntimeError("\n".join(errors))

    def _extract_model_ids(self, data: Any) -> List[str]:
        return extract_model_ids_from_response(data)

    async def _iter_draw_batch(
        self,
        event: AstrMessageEvent,
        prompt: str,
        aspect: str,
        resolution: str,
        refs: List[ImageReference],
        source: str,
        requested_count: int,
        passthrough: bool = False,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        total = self._normalize_count(requested_count)
        for index in range(total):
            if passthrough:
                result = await self._draw_passthrough_once(event, prompt, aspect, resolution, refs, source)
            else:
                result = await self._draw_once(event, prompt, aspect, resolution, refs, source)
            result["batch_index"] = index + 1
            result["batch_total"] = total
            yield result
            if not result.get("success"):
                return

    async def _draw_once(self, event: AstrMessageEvent, prompt: str, aspect: str, resolution: str, refs: List[ImageReference], source: str) -> Dict[str, Any]:
        user_prompt = str(prompt or "").strip()
        prompt_en_meta: Dict[str, Any] = {"enabled": False, "applied": False, "scope": "user_text_only"}
        if self._prompt_en_needed(user_prompt, media="image"):
            translated, prompt_en_meta = await self._translate_prompt_to_english(
                user_prompt, media="image", event=event
            )
            if prompt_en_meta.get("applied") and translated:
                user_prompt = translated
        final_prompt = build_prompt_with_reference_instruction(
            user_prompt,
            refs,
            language="en" if self.config.image_enable_image_prompt_en else "zh",
        )
        return await self._run_image_generation(
            final_prompt,
            aspect,
            resolution,
            refs,
            source=source,
            audit_user_id=event_user_id(event),
            event=event,
            original_prompt=prompt,
            prompt_en_meta=prompt_en_meta,
        )

    async def _draw_passthrough_once(self, event: AstrMessageEvent, prompt: str, aspect: str, resolution: str, refs: List[ImageReference], source: str) -> Dict[str, Any]:
        user_prompt = str(prompt or "").strip()
        prompt_en_meta: Dict[str, Any] = {"enabled": False, "applied": False, "scope": "user_text_only"}
        if self._prompt_en_needed(user_prompt, media="image"):
            translated, prompt_en_meta = await self._translate_prompt_to_english(
                user_prompt, media="image", event=event
            )
            if prompt_en_meta.get("applied") and translated:
                user_prompt = translated
        return await self._run_image_generation(
            user_prompt,
            aspect,
            resolution,
            refs,
            source=source,
            audit_user_id=event_user_id(event),
            event=event,
            original_prompt=prompt,
            prompt_en_meta=prompt_en_meta,
        )

    async def _handle_selfie_command(
        self,
        event: AstrMessageEvent,
        command_name: Any,
        fallback: str,
        default_action: str,
        default_action_with_refs: str,
        progress_label: str,
        source: str,
        fail_label: str,
        message_override: str = "",
        include_at_avatar: bool = False,
        allow_context_fallback: bool = True,
        requested_count_override: int = 0,
        preset_aspect: str = "",
        preset_resolution: str = "",
        preset_name: str = "",
    ) -> AsyncGenerator[Any, None]:
        message = message_override.strip() if message_override else extract_command_message(event, command_name, fallback)
        if requested_count_override > 0:
            requested_count = self._normalize_count(requested_count_override)
        else:
            message, requested_count = self._extract_command_count(message)

        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        # Prefer aspect/resolution resolved from raw user text (before action wrappers).
        if str(preset_name or "").strip():
            action = message
            default_aspect = str(self.config.image_default_aspect_ratio or "9:16").strip() or "9:16"
            default_resolution = str(self.config.image_default_resolution or "1K").strip() or "1K"
            aspect = str(preset_aspect or "").strip() or default_aspect
            resolution = str(preset_resolution or "").strip() or default_resolution
        else:
            action, aspect, resolution, _, _ = self._resolve_image_preset(message)
        extra_refs = await self._event_reference_images(
            event,
            include_at_avatar=include_at_avatar,
            context_hint=action,
            allow_context_fallback=allow_context_fallback,
        )
        if not action:
            action = default_action_with_refs if extra_refs else default_action
        hints: List[str] = []
        if not self.persona.has_reference_image():
            if bool(getattr(self.config, "image_use_logo_when_no_persona", True)):
                hints.append("当前还没有设置 AI 形象参考图，将用插件 logo 作为形象回退；关闭「无形象图用 logo」后改为仅按人设生成。")
            else:
                hints.append("当前还没有设置 AI 形象参考图，会按人设与今日设定生成主角。")
        if progress_label == "合影" and not extra_refs:
            hints.append("没有读取到合影对象参考图，会按文字要求生成同框对象。")
        progress = await self._build_contextual_progress_text(event, "selfie", action, requested_count)
        if hints:
            progress += "\n" + "\n".join(hints)
        queue_notified = self._image_batch_queue_expected(requested_count)
        if queue_notified:
            progress = "当前生图并发已满，本次先排队，空出槽位后继续。\n" + progress
        self._record_bot_text_context(event, progress)

        async def runner(task_id: str) -> Dict[str, Any]:
            return await self._background_selfie_batches(
                task_id,
                event,
                action,
                extra_refs,
                source,
                requested_count,
                aspect,
                resolution,
                fail_label,
                queue_notified=queue_notified,
            )

        task = self.start_command_image_task(
            event,
            source=source,
            summary={
                "original_prompt": action,
                "aspect_ratio": aspect,
                "resolution": resolution,
                "requested_count": requested_count,
                "kind": progress_label,
                "preset_name": str(preset_name or "").strip(),
            },
            runner=runner,
        )
        yield event.plain_result(progress)

    @filter.command("生图帮助")
    async def cmd_help(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """只看图卡帮助。完整文字说明请发 /生图help。"""
        help_path = self._resolve_help_image_path()
        if help_path:
            yield event.chain_result([self._create_image_component(help_path)])
            return
        # 无图时退回简短提示，避免空白
        yield event.plain_result("帮助图暂不可用。发 /生图help 看文字说明。")

    @filter.command("生图help")
    async def cmd_help_text(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """完整文字指令说明。"""
        yield event.plain_result(self._help_text_body())

    def _help_text_body(self) -> str:
        return "\n".join(
            [
                f"{PLUGIN_DISPLAY_NAME} v{PLUGIN_VERSION}",
                "",
                "常用：",
                "· /画 或 /生图　写想要的画面；可写数量如 /画 3；有附图/引用图就按图改，没图就按文字出；不自动带入形象图",
                "· /文生图　只用文字按原文出图，不走自拍人设，也不用形象图；可写数量",
                "· /图生图　必须附图或引用图，按原文改图；可写数量；不自动使用形象图",
                "· /自拍 或 /看看　用当前形象自拍；可写动作、场景、换装；可写数量如 /自拍 3",
                "· /看看腿　日常下装穿搭记录；随机手机记录或朋友协助拍摄视角；可写数量如 /看看腿 3",
                "· /查看提示词　引用图片后查看原生图提示词；没有生图记录时由当前聊天 LLM 反推",
                "· /看看COS　随机一套内置 COS 换装；默认随机自拍或他拍，也可写「自拍」「他拍」；可写数量如 /看看COS 2",
                "· /看看你　像别人随手拍你；可写数量",
                "· /合影 或 /合照　和对象同框；可附图或@对方，自己用当前形象；可写数量",
                "",
                "视频：",
                "· /视频　写想要的动态；有图就图生视频，没图就用当前形象图作首帧",
                "· /文生视频　只用文字出视频，不带图、不用形象图",
                "· /图生视频　附图/引用图优先作首帧；没图时用当前形象图",
                "",
                "自动判断：",
                "· /画：有图=图生图，没图=文生图；不会自动塞形象图",
                "· /视频：有图=图生视频，没图=用形象图作首帧",
                "· 自拍/合影/看看：会用当前形象；形象类型可设自动、真人、动漫",
                "",
                "模型与进度：",
                "· /生图模型　看列表；跟序号或 渠道/模型 切换（只影响当前群/私聊）；发「清除」恢复默认",
                "· /生图任务　看出图/视频进行中的任务；可跟任务号",
                "· /生图取消　取消还在排的/进行中的任务",
                "",
                "形象：",
                "· /形象查看　看当前参考图、形象类型与今日状态",
                "· /形象设置　发图设形象；也可写 自动 / 真人 / 动漫 改形象类型",
                "· /辅助形象设置　附带图片增加辅助形象；最多 3 张，普通自拍/换装会使用，合影时只使用主形象图",
                "· /辅助形象清除　清空辅助形象图，不影响主形象",
                "· /形象清除　去掉参考图",
                "· /形象刷新　刷新今日穿搭状态",
                "",
                "预设：/预设　列表；管理员可 /预设添加 名称:内容、/预设删除 名称",
                "",
                "说明：一次可写数量表示本条指令要生成的总张数；同时最多进行几张由「同时画几张上限」决定，不锁在单条指令里，新任务自动排队，超过同时上限才等待。图好了会直接发过来。",
                "· /生图帮助　只看图卡",
                "· /生图help　看本页完整说明",
                f"管理页：{'已开' if self.config.web_enable else '未开'}　http://{self.config.web_host}:{self.config.web_port}",
                "也可在 AstrBot 插件页打开管理界面。",
            ]
        )

    def _resolve_help_image_path(self) -> str:
        """Return shipped static help poster only (no runtime generation)."""
        for path in (getattr(self, "_bundled_help_poster_path", ""),):
            if path and os.path.isfile(path):
                try:
                    with open(path, "rb") as handle:
                        head = handle.read(32)
                    if looks_like_image_bytes(head):
                        return path
                except Exception:
                    continue
        return ""

    @filter.command("查看提示词")
    async def cmd_view_prompt(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """引用一张图片查看原生图提示词；没有记录时由当前聊天 LLM 反推。"""
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        # AstrBot's aiocqhttp adapter records bot replies in ``Reply.chain``.
        # The plugin already retains the exact generated cache path for each
        # sent image, so include those bytes when the quoted sender is the bot.
        bot_cache_sources = (
            self._recent_context_image_sources(
                event,
                max_images=8,
                prefer_user=False,
                bot_only=True,
            )
            if self._event_quotes_bot_image(event)
            else []
        )
        refs = await self._event_reference_images(
            event,
            include_at_avatar=False,
            context_hint="查看提示词",
            allow_context_fallback=False,
            include_persona=False,
            # QQ/AstrBot may expose both a transcoded local path and the
            # original URL.  Prompt lookup must try both to match the cache.
            include_image_alternates=True,
            extra_sources=bot_cache_sources,
        )
        if not refs:
            yield event.plain_result("请引用一张图片后再使用 /查看提示词。")
            return

        ref = refs[0]
        md5 = hashlib.md5(ref.data).hexdigest()
        record = None
        for candidate in refs:
            candidate_md5 = hashlib.md5(candidate.data).hexdigest()
            candidate_record = self._find_generation_record_by_md5(candidate_md5)
            if candidate_record is not None:
                ref = candidate
                md5 = candidate_md5
                record = candidate_record
                break
        logger.debug(
            "[SelfieImage] 查看提示词图片候选 MD5: %s",
            ", ".join(hashlib.md5(candidate.data).hexdigest() for candidate in refs),
        )
        if record is not None:
            prompt = str(
                record.get("request_prompt")
                or record.get("prompt")
                or record.get("original_prompt")
                or ""
            ).strip()
            if prompt:
                yield event.plain_result(f"图片 MD5：{md5}\n生图提示词：\n{prompt}")
            else:
                yield event.plain_result(f"图片 MD5：{md5}\n这是本插件生成的图片，但历史记录中没有保存提示词。")
            return

        yield event.plain_result(f"图片 MD5：{md5}\n未找到本插件的生图记录，正在让当前 LLM 反推提示词……")
        try:
            prompt = await self._reverse_image_prompt_with_llm(event, ref.data)
        except Exception as exc:
            logger.warning("[SelfieImage] 反推图片提示词失败: %s", redact_sensitive_text(str(exc)))
            yield event.plain_result(f"图片 MD5：{md5}\n暂时无法反推提示词：{redact_sensitive_text(str(exc))[:200]}")
            return
        if not prompt:
            yield event.plain_result(f"图片 MD5：{md5}\n当前 LLM 没有返回有效提示词。")
            return
        yield event.plain_result(f"图片 MD5：{md5}\nLLM 反推提示词：\n{prompt}")

    @filter.command("生图模型")
    async def cmd_image_model(self, event: AstrMessageEvent, p1: str = "", p2: str = "", p3: str = "") -> AsyncGenerator[Any, None]:
        """查看或切换当前聊天使用的模型。可跟序号、渠道/模型，或发「清除」恢复默认。"""
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        fallback = " ".join(item for item in [p1, p2, p3] if item).strip()
        message = extract_command_message(event, "生图模型", fallback).strip()
        labels = self._available_model_labels()
        current = self._get_session_model_override(event)
        default_label = labels[0] if labels else ""
        effective = current or default_label

        if not message:
            if not labels:
                yield event.plain_result("现在还没有可用模型，请先在管理页启用渠道和模型。")
                return
            lines = ["可用模型（只改当前聊天，不影响其他群）："]
            for index, label in enumerate(labels, 1):
                mark = " ✓" if label == effective else ""
                lines.append(f"{index}. {label}{mark}")
            lines.append(f"当前：{effective or '（未配置）'}")
            if current:
                lines.append(f"本会话指定：{current}（发 /生图模型 清除 可恢复默认）")
            else:
                lines.append("切换：/生图模型 序号　或　/生图模型 渠道/模型")
            yield event.plain_result("\n".join(lines))
            return

        if message in {"清除", "取消", "默认", "reset", "clear"}:
            self._set_session_model_override(event, "")
            yield event.plain_result(f"已恢复默认顺序：{default_label or '（无模型）'}")
            return

        matched = self._match_model_label(message)
        if not matched:
            yield event.plain_result("没对上模型。先 /生图模型 看列表，再发序号或 渠道/模型。")
            return
        self._set_session_model_override(event, matched)
        yield event.plain_result(f"本会话已换成：{matched}\n之后这里的 /画、/自拍 等会优先用它。")

    @filter.command("生图任务")
    async def cmd_image_tasks(self, event: AstrMessageEvent, p1: str = "", p2: str = "") -> AsyncGenerator[Any, None]:
        """查看进行中的出图/视频任务。可跟任务号或列表编号。"""
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        fallback = " ".join(item for item in [p1, p2] if item).strip()
        message = extract_command_message(event, "生图任务", fallback).strip()
        session_key = self._session_key(event)
        is_admin = self._is_admin_event(event)

        if message:
            try:
                task = self.get_web_image_task(message)
            except Exception:
                # numeric index into active list
                active = self._list_image_tasks_for_session(session_key, include_finished=False, limit=20)
                if message.isdigit():
                    index = int(message) - 1
                    if 0 <= index < len(active):
                        task = active[index]
                    else:
                        yield event.plain_result("没找到这个进行中的编号，或任务号不对。")
                        return
                else:
                    yield event.plain_result("没有这单，或已经清理了。")
                    return
            owner = str(task.get("owner_session") or "")
            if owner and owner != session_key and not is_admin:
                yield event.plain_result("不能看别人会话里的出图。")
                return
            # refresh running_seconds
            if task.get("status") in {"queued", "running"}:
                try:
                    task = self.get_web_image_task(str(task.get("task_id") or message))
                except Exception:
                    pass
            yield event.plain_result(self._format_task_detail_text(task))
            return

        tasks = self._list_image_tasks_for_session(session_key, include_finished=False, limit=10)
        yield event.plain_result(self._format_task_list_text(tasks))

    @filter.command("生图取消")
    async def cmd_image_task_cancel(self, event: AstrMessageEvent, p1: str = "", p2: str = "") -> AsyncGenerator[Any, None]:
        """取消排队中或进行中的出图/视频任务。可跟任务号或列表编号。"""
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        fallback = " ".join(item for item in [p1, p2] if item).strip()
        message = extract_command_message(event, "生图取消", fallback).strip()
        session_key = self._session_key(event)
        is_admin = self._is_admin_event(event)
        if not message:
            active = self._list_image_tasks_for_session(session_key, include_finished=False, limit=5)
            if active:
                yield event.plain_result("请跟任务号或列表里的编号。\n" + self._format_task_list_text(active))
            else:
                yield event.plain_result("现在没有可取消的出图。")
            return
        task_id = message
        if message.isdigit():
            active = self._list_image_tasks_for_session(session_key, include_finished=False, limit=20)
            index = int(message) - 1
            if 0 <= index < len(active):
                task_id = str(active[index].get("task_id") or "")
            else:
                yield event.plain_result("未找到对应的进行中任务，请检查编号或任务ID。")
                return
        try:
            text = self.cancel_image_task(task_id, session_key=session_key, is_admin=is_admin)
            yield event.plain_result(text)
        except PermissionError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            yield event.plain_result(redact_sensitive_text(str(exc)))

    @filter.command("生图重发")
    async def cmd_image_retry_failed(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """重发本会话最近发送失败、仍存在于缓存中的图片。"""
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        result = await self.retry_failed_images(event)
        if result["sent"]:
            yield event.plain_result(f"已重发 {result['sent']} 张图片。")
        else:
            yield event.plain_result("没有可重发的图片。")

    @filter.command("画", alias={"生图"})
    async def cmd_draw(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        """写想要的画面出图。有附图/引用图时按图改；没图时按文字出。不会自动带入形象图。"""
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        message = extract_command_message(event, ("画", "生图"), fallback)
        message, requested_count = self._extract_command_count(message)
        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        prompt, aspect, resolution, _, _ = self._resolve_image_preset(message)
        refs = await self._event_reference_images(
            event,
            include_at_avatar=True,
            context_hint=prompt,
            allow_context_fallback=True,
        )
        if not prompt and refs:
            prompt = "根据参考图生成一张自然、清晰、符合原图语义的图片。"
        if not prompt:
            yield event.plain_result("请输入提示词或附带参考图。")
            return

        progress = await self._build_contextual_progress_text(event, "image", prompt, requested_count)
        self._record_bot_text_context(event, progress)

        async def runner(task_id: str) -> Dict[str, Any]:
            return await self._background_draw_batches(
                task_id,
                event,
                prompt,
                aspect,
                resolution,
                refs,
                "command-draw",
                requested_count,
                passthrough=True,
            )

        task = self.start_command_image_task(
            event,
            source="command-draw",
            summary={
                "original_prompt": prompt,
                "aspect_ratio": aspect,
                "resolution": resolution,
                "requested_count": requested_count,
                "reference_image_count": len(refs),
            },
            runner=runner,
        )
        yield event.plain_result(progress)

    @filter.command("文生图")
    async def cmd_raw_text_to_image(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        """按你写的原文出图，不走自拍人设包装，也不使用形象图。"""
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        message = extract_command_message(event, "文生图", fallback)
        message, requested_count = self._extract_command_count(message)
        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        prompt, aspect, resolution = self._parse_prompt_options(message)
        if not prompt:
            yield event.plain_result("请输入文生图提示词。")
            return

        progress = await self._build_contextual_progress_text(event, "image", prompt, requested_count)
        self._record_bot_text_context(event, progress)

        async def runner(task_id: str) -> Dict[str, Any]:
            return await self._background_draw_batches(
                task_id,
                event,
                prompt,
                aspect,
                resolution,
                [],
                "command-raw-text-to-image",
                requested_count,
                passthrough=True,
            )

        task = self.start_command_image_task(
            event,
            source="command-raw-text-to-image",
            summary={
                "original_prompt": prompt,
                "aspect_ratio": aspect,
                "resolution": resolution,
                "requested_count": requested_count,
            },
            runner=runner,
        )
        yield event.plain_result(progress)

    @filter.command("图生图")
    async def cmd_raw_image_to_image(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        """带图或引用图，按原文改图。需要附图，不会自动使用形象图。"""
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        message = extract_command_message(event, "图生图", fallback)
        message, requested_count = self._extract_command_count(message)
        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        prompt, aspect, resolution = self._parse_prompt_options(message)
        refs, source_count, failed_count = await self._event_reference_images_with_stats(
            event,
            include_at_avatar=True,
            context_hint=prompt,
            allow_context_fallback=True,
        )
        if not refs:
            if source_count and failed_count:
                yield event.plain_result("参考图读取失败或超时，请重新发送原图后再试。")
                return
            yield event.plain_result("请附带、引用图片，或艾特要作为参考的对象。")
            return
        if not prompt:
            yield event.plain_result("请输入图生图提示词。")
            return

        progress = await self._build_contextual_progress_text(event, "image", prompt, requested_count)
        self._record_bot_text_context(event, progress)

        async def runner(task_id: str) -> Dict[str, Any]:
            return await self._background_draw_batches(
                task_id,
                event,
                prompt,
                aspect,
                resolution,
                refs,
                "command-raw-image-to-image",
                requested_count,
                passthrough=True,
            )

        task = self.start_command_image_task(
            event,
            source="command-raw-image-to-image",
            summary={
                "original_prompt": prompt,
                "aspect_ratio": aspect,
                "resolution": resolution,
                "requested_count": requested_count,
                "reference_image_count": len(refs),
            },
            runner=runner,
        )
        yield event.plain_result(progress)

    @filter.command("自拍", alias={"看看"})
    async def cmd_selfie(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        """用当前形象自拍。可写动作、场景、换装；有附图时作服装/场景参考。"""
        fallback_args = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        raw_message = extract_command_message(event, ("自拍", "看看"), fallback_args)
        raw_extra, requested_count = self._extract_command_count(raw_message)
        # Resolve presets on raw user words first (e.g. /自拍 捧脸), then wrap action.
        expanded_extra, preset_aspect, preset_resolution, preset_name = self._expand_user_text_with_preset(raw_extra)
        has_refs = bool(extract_image_sources_from_event(event))
        if expanded_extra.strip():
            base_action = self._build_selfie_look_action(expanded_extra, has_refs)
        else:
            base_action = self._build_selfie_look_action("", has_refs)
        async for item in self._handle_selfie_command(
            event=event,
            command_name=("自拍", "看看"),
            fallback=base_action,
            default_action=self._build_selfie_look_action("", False),
            default_action_with_refs=self._build_selfie_look_action("", True),
            progress_label="自拍",
            source="command-selfie",
            fail_label=self._natural_fail_fallback("selfie"),
            message_override=base_action,
            requested_count_override=requested_count,
            preset_aspect=preset_aspect,
            preset_resolution=preset_resolution,
            preset_name=preset_name,
        ):
            yield item

    @filter.command("看看腿")
    async def cmd_look_legs(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        """日常下装穿搭记录；随机手机记录或朋友协助拍摄视角；可写数量。"""
        fallback_args = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        raw_message = extract_command_message(event, "看看腿", fallback_args)
        raw_extra, requested_count = self._extract_command_count(raw_message)
        expanded_extra, preset_aspect, preset_resolution, preset_name = self._expand_user_text_with_preset(raw_extra)
        fallback = self._build_leg_focus_action(expanded_extra, bool(extract_image_sources_from_event(event)))
        async for item in self._handle_selfie_command(
            event=event,
            command_name="看看腿",
            fallback=fallback,
            default_action=self._build_leg_focus_action("", False),
            default_action_with_refs=self._build_leg_focus_action("", True),
            progress_label="自拍",
            source="command-look-legs",
            fail_label=self._natural_fail_fallback("legs"),
            message_override=fallback,
            requested_count_override=requested_count,
            preset_aspect=preset_aspect,
            preset_resolution=preset_resolution,
            preset_name=preset_name,
        ):
            yield item

    @filter.command("看看COS", alias={"看看cos", "看看Cos"})
    async def cmd_look_cos(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        """随机一套内置 COS 换装；默认随机自拍或他拍。可写数量如 /看看COS 3。"""
        fallback_args = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        raw_message = extract_command_message(event, "看看COS", fallback_args)
        # Also accept lowercase command text extraction fallbacks.
        if not raw_message:
            raw_message = extract_command_message(event, "看看cos", fallback_args)
        raw_extra, requested_count = self._extract_command_count(raw_message)
        expanded_extra, preset_aspect, preset_resolution, preset_name = self._expand_user_text_with_preset(raw_extra)
        has_refs = bool(extract_image_sources_from_event(event))
        fallback = self._build_cos_look_action(expanded_extra, has_refs)
        async for item in self._handle_selfie_command(
            event=event,
            command_name="看看COS",
            fallback=fallback,
            default_action=self._build_cos_look_action("", False),
            default_action_with_refs=self._build_cos_look_action("", True),
            progress_label="自拍",
            source="command-look-cos",
            fail_label=self._natural_fail_fallback("selfie"),
            message_override=fallback,
            allow_context_fallback=False,
            requested_count_override=requested_count,
            preset_aspect=preset_aspect,
            preset_resolution=preset_resolution,
            preset_name=preset_name,
        ):
            yield item

    @filter.command("看看你")
    async def cmd_look_you(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        """像别人随手拍你，带一点日常他拍感。使用当前形象。"""
        fallback_args = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        raw_message = extract_command_message(event, "看看你", fallback_args)
        raw_extra, requested_count = self._extract_command_count(raw_message)
        expanded_extra, preset_aspect, preset_resolution, preset_name = self._expand_user_text_with_preset(raw_extra)
        fallback = self._build_third_person_look_action(expanded_extra, bool(extract_image_sources_from_event(event)))
        async for item in self._handle_selfie_command(
            event=event,
            command_name="看看你",
            fallback=fallback,
            default_action=self._build_third_person_look_action("", False),
            default_action_with_refs=self._build_third_person_look_action("", True),
            progress_label="自拍",
            source="command-look-you",
            fail_label=self._natural_fail_fallback("selfie"),
            message_override=fallback,
            requested_count_override=requested_count,
            preset_aspect=preset_aspect,
            preset_resolution=preset_resolution,
            preset_name=preset_name,
        ):
            yield item

    @filter.command("合影", alias={"合照"})
    async def cmd_group_selfie(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        """和对象同框合影。可附图或@对方；自己使用当前形象。"""
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        raw_message = extract_command_message(event, ("合影", "合照"), fallback)
        raw_message, requested_count = self._extract_command_count(raw_message)
        expanded_message, preset_aspect, preset_resolution, preset_name = self._expand_user_text_with_preset(raw_message)
        action = self._build_group_selfie_action(
            expanded_message,
            bool(extract_image_sources_from_event(event, include_at_avatar=True)),
        )
        async for item in self._handle_selfie_command(
            event=event,
            command_name=("合影", "合照"),
            fallback=fallback,
            default_action=self._build_group_selfie_action("", False),
            default_action_with_refs=self._build_group_selfie_action("", True),
            progress_label="合影",
            source="command-group-selfie",
            fail_label=self._natural_fail_fallback("group"),
            message_override=action,
            include_at_avatar=True,
            requested_count_override=requested_count,
            preset_aspect=preset_aspect,
            preset_resolution=preset_resolution,
            preset_name=preset_name,
        ):
            yield item

    @filter.command("形象查看")
    async def cmd_persona_status(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """查看当前形象参考图、形象类型与今日状态。"""
        await self.persona.ensure_daily_selfie_profile("查看今日自拍设定")
        path = self.persona.get_reference_path()
        if path:
            yield event.chain_result([self._create_image_component(path)])
        yield event.plain_result(self.persona.status_text())

    @filter.command("形象设置")
    async def cmd_persona_set(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """设置形象参考图，或改形象类型：自动 / 真人 / 动漫。"""
        sources = extract_image_sources_from_event(event, include_at_avatar=False)
        text = extract_event_text(event)
        sources.extend(extract_image_urls(text))
        sources = list(dict.fromkeys(sources))
        # 允许「形象设置 动漫/真人/自动」只改类型；也可与附图一起设置
        type_hint = ""
        compact = re.sub(r"[\s，。！？、；：,.!?]", "", str(text or ""))
        for token, value in (
            ("二次元", "anime"),
            ("动漫", "anime"),
            ("动画", "anime"),
            ("真人", "real"),
            ("写实", "real"),
            ("自动", "auto"),
            ("默认", "auto"),
        ):
            if token in compact:
                type_hint = value
                break
        if type_hint:
            self.persona.set_appearance_type(type_hint)
        if not sources:
            if type_hint:
                yield event.plain_result(f"形象类型已设为{self.persona.appearance_type_label()}。\n" + self.persona.status_text())
                return
            yield event.plain_result(
                "请发送图片、引用图片，或在指令后附带图片链接。\n"
                "也可：形象设置 自动 / 形象设置 真人 / 形象设置 动漫"
            )
            return
        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        async with aiohttp.ClientSession(trust_env=False) as session:
            for source in sources:
                fetched = await fetch_image_source(source, session, max_bytes=max_bytes)
                if not fetched:
                    continue
                data, mime = fetched
                self.persona.save_reference_image(data, mime)
                msg = "AI 自拍形象参考图已保存。"
                if type_hint:
                    msg += f" 形象类型：{self.persona.appearance_type_label()}。"
                yield event.plain_result(msg)
                return
        yield event.plain_result("没有读取到可用图片，或图片超过大小限制。")

    @filter.command("辅助形象设置")
    async def cmd_persona_auxiliary_set(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """增加辅助形象参考图；辅助图最多保存 3 张。"""
        event_text = extract_event_text(event)
        text = extract_command_message(event, "辅助形象设置", event_text)
        compact = re.sub(r"[\s，。！？、；：,.!?]", "", str(text or "")).lower()
        clear_requested = compact in {"清除", "删除", "重置", "clear", "reset", "全部清除"} or any(
            compact.startswith(prefix) for prefix in ("清除辅助形象", "删除辅助形象", "重置辅助形象")
        )
        if clear_requested:
            self.persona.clear_auxiliary_reference_images()
            yield event.plain_result("辅助形象参考图已全部清除，主形象不受影响。")
            return

        sources = extract_image_sources_from_event(event, include_at_avatar=False)
        sources.extend(extract_image_urls(text))
        sources = list(dict.fromkeys(sources))
        current_count = len(self.persona.get_auxiliary_reference_entries())
        if not sources:
            if current_count:
                yield event.plain_result(
                    f"当前已有 {current_count} 张辅助形象参考图（最多 3 张）。"
                    "请附带图片上传，合影时不会使用辅助图。"
                )
            else:
                yield event.plain_result(
                    "请发送图片、引用图片，或在指令后附带图片链接，作为辅助形象参考图。"
                    "最多可设置 3 张。"
                )
            return
        if current_count >= 3:
            yield event.plain_result("辅助形象参考图已达到上限 3 张，请先使用 /辅助形象清除。")
            return

        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        saved = 0
        failed = 0
        async with aiohttp.ClientSession(trust_env=False) as session:
            for source in sources:
                if current_count + saved >= 3:
                    break
                try:
                    fetched = await fetch_image_source(source, session, max_bytes=max_bytes)
                except Exception:
                    fetched = None
                if not fetched:
                    failed += 1
                    continue
                data, mime = fetched
                try:
                    self.persona.add_auxiliary_reference_image(data, mime)
                except (OSError, ValueError):
                    failed += 1
                    continue
                saved += 1

        total = len(self.persona.get_auxiliary_reference_entries())
        if saved:
            message = f"已保存 {saved} 张辅助形象参考图，当前共 {total} 张。合影时只使用主形象图。"
            if failed:
                message += f"另有 {failed} 张图片读取失败。"
            if total >= 3 and len(sources) > saved:
                message += "辅助形象图已达到上限 3 张。"
            yield event.plain_result(message)
            return
        if failed:
            yield event.plain_result("没有读取到可用图片，或图片超过大小限制。")
        else:
            yield event.plain_result("没有新增辅助形象参考图。")

    @filter.command("辅助形象清除")
    async def cmd_persona_auxiliary_clear(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """清除全部辅助形象参考图，不影响主形象。"""
        count = len(self.persona.get_auxiliary_reference_entries())
        self.persona.clear_auxiliary_reference_images()
        yield event.plain_result(
            "辅助形象参考图已清除，不影响主形象。"
            if count
            else "当前没有辅助形象参考图。主形象不受影响。"
        )

    @filter.command("形象清除")
    async def cmd_persona_clear(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """清除当前形象参考图。"""
        self.persona.clear_reference_image()
        yield event.plain_result("AI 自拍形象参考图已清除。")

    @filter.command("形象刷新")
    async def cmd_persona_refresh(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """刷新今日穿搭与状态。"""
        self.persona.refresh_daily_selfie_profile_for_test()
        await self.persona.ensure_daily_selfie_profile("手动刷新今日自拍设定")
        yield event.plain_result("今日自拍设定已刷新。\n" + self.persona.status_text())

    @filter.command("预设", prefix_optional=True)
    async def cmd_preset(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        """查看预设列表，或按预设名生成。"""
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        message = extract_command_message(event, "预设", fallback)
        text = self._normalize_preset_input(message)

        if not text:
            body, _, _ = self._preset_list_text(1)
            yield event.plain_result(body)
            return

        head, tail = self._split_preset_command(text)
        if head.isdigit():
            body, _, _ = self._preset_list_text(int(head))
            yield event.plain_result(body)
            return

        if head in {"列表", "list"}:
            page = int(tail) if tail.isdigit() else 1
            body, _, _ = self._preset_list_text(page)
            yield event.plain_result(body)
            return

        if head in {"查看", "详情", "view", "detail"}:
            if not self._is_admin_event(event):
                yield event.plain_result("仅管理员可以查看预设内容。")
                return
            if not tail or tail.isdigit():
                body, _, _ = self._preset_detail_text(int(tail) if tail.isdigit() else 1)
                yield event.plain_result(body)
                return
            success, body = self._preset_single_detail_text(tail)
            yield event.plain_result(body if success else f"❌ {body}")
            return

        if head in {"添加", "add", "新增"}:
            if not tail:
                yield event.plain_result("格式：/预设添加 名称:提示词")
                return
            success, message = self._handle_preset_mutation(event, "add", tail)
            yield event.plain_result(f"{'✅' if success else '❌'} {message}")
            return

        if head in {"删除", "del", "delete", "remove", "删"}:
            if not tail:
                yield event.plain_result("格式：/预设删除 名称")
                return
            success, message = self._handle_preset_mutation(event, "delete", tail)
            yield event.plain_result(f"{'✅' if success else '❌'} {message}")
            return

        body, _, _ = self._preset_list_text(1)
        yield event.plain_result(
            "\n".join(
                [
                    body,
                    "",
                    "用法：/预设 2、/预设 添加 名称:提示词、/预设 删除 名称、/预设 查看 [页码/预设名]（管理员）",
                ]
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("预设添加", prefix_optional=True)
    async def cmd_preset_add(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        """管理员添加预设。格式：名称:内容"""
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        payload = self._normalize_preset_input(extract_command_message(event, "预设添加", fallback))
        if not payload:
            yield event.plain_result("格式：/预设添加 名称:提示词")
            return
        success, message = self._handle_preset_mutation(event, "add", payload)
        yield event.plain_result(f"{'✅' if success else '❌'} {message}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("预设删除", prefix_optional=True)
    async def cmd_preset_delete(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        """管理员删除指定预设。"""
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        payload = self._normalize_preset_input(extract_command_message(event, "预设删除", fallback))
        if not payload:
            yield event.plain_result("格式：/预设删除 名称")
            return
        success, message = self._handle_preset_mutation(event, "delete", payload)
        yield event.plain_result(f"{'✅' if success else '❌'} {message}")

    @LLM_TOOL(name="generate_image")
    async def tool_generate_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
        count: int = 1,
        aspect_ratio: str = "",
        resolution: str = "",
        size: str = "",
        ack_message: str = "",
    ) -> Optional[str]:
        """
        使用生图模型生成普通图片，支持文生图和参考图图生图。
        自拍、AI 自己、合影、合照、同框、与用户一起拍照等请求使用 generate_selfie。
        prompt 保持简洁，保留主体、场景、动作/风格、构图和参考图关系即可。
        闲聊中顺势画图时，ack_message 用当前人格自然短句接话，简体中文 10-40 字。
        Args:
            prompt(string): 简洁生图提示词，描述主体、场景、动作/风格、构图和参考图使用方式。
            count(number): 调用生图次数，默认 1；每次调用可能返回一张或多张图片。
            aspect_ratio(string): 宽高比，例如 1:1、3:4、9:16、16:9；留空使用默认值。
            resolution(string): 分辨率，例如 1K、2K、4K；留空使用默认值。
            size(string): 兼容参数，可传 1024x1024、2048x2048 或 4096x4096。
            ack_message(string): 可选。根据当前对话和机器人人格生成的简体中文短进度回复。
        """
        if not self.config.image_enable_llm_tool:
            return self._tool_unavailable("我这会儿还没法把这个画面整理出来。")
        self._remember_llm_generation(
            event,
            "image",
            {
                "prompt": prompt,
                "count": count,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "size": size,
            },
        )
        requested_count = self._normalize_count(count)
        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            return self._tool_soft_fail(error)
        prompt, aspect, resol, _, _ = self._resolve_image_preset(prompt, aspect_ratio, resolution or size)
        if not prompt:
            return self._tool_soft_fail("缺少生图提示词", "你想让我往什么感觉走？")
        if self._looks_like_selfie_intent(prompt):
            return await self._run_llm_selfie_flow(event, prompt, requested_count, aspect, resol, ack_message)

        await self._send_progress_text(
            event,
            await self._build_contextual_progress_text(event, "image", prompt, requested_count, ack_message),
        )
        refs = await self._event_reference_images(
            event,
            include_at_avatar=True,
            context_hint=prompt,
            allow_context_fallback=True,
        )
        result = await self._background_draw_batches(
            "llm-generate-image",
            event,
            prompt,
            aspect,
            resol,
            refs,
            "llm-generate-image",
            requested_count,
            passthrough=True,
            fail_label=self._natural_fail_fallback("image"),
        )
        if not result.get("success") and not result.get("files"):
            return self._tool_soft_fail(str(result.get("error") or ""), self._natural_fail_fallback("image"))
        return self._tool_success("image", len(result.get("files") or []) or requested_count)

    @LLM_TOOL(name="generate_selfie")
    async def tool_generate_selfie(
        self,
        event: AstrMessageEvent,
        action: str,
        count: int = 1,
        aspect_ratio: str = "",
        resolution: str = "",
        size: str = "",
        ack_message: str = "",
    ) -> Optional[str]:
        """
        以当前 AI 助手自己的形象生成自拍、形象照、换装照、姿势照、合影或同框照。
        用户要求“合影/合照/同框/和我一起拍/和你一起拍/我们拍一张”时使用这个工具。
        用户要求 AI 自己“穿这个/穿这套/换这身/换衣服/用这个姿势/摆这个姿势/照这个姿势”并附带参考图时，也使用这个工具。
        本工具会自动带上 AI 当前形象参考图；如果用户消息里附带图片，也会作为合影对象或参考图一起传入。
        非合影换装或换姿势时，附带图片默认只作为服装、姿势、构图或风格参考，AI 的脸和身份仍来自当前形象参考图。
        如果附带图片里的人用手机、手、道具、口罩、面具或其他东西挡脸，默认不要把挡脸物迁移到 AI 身上，除非用户明确要求遮脸。
        action 保持简洁，整理出动作/场景/情绪/服装/镜头语言；合影时写清同框关系和参考图对象。
        ack_message 使用简体中文，以当前人格自然回应，10-40 字。
        Args:
            action(string): 简洁自拍/合影要求，包含动作、表情、服装、环境、镜头或同框关系。
            count(number): 调用自拍生图次数，默认 1；每次调用可能返回一张或多张图片。
            aspect_ratio(string): 宽高比，例如 1:1、3:4、9:16、16:9；留空使用默认值。
            resolution(string): 分辨率，例如 1K、2K、4K；留空使用默认值。
            size(string): 兼容参数，可传 1024x1024、2048x2048 或 4096x4096。
            ack_message(string): 可选。根据当前对话和机器人人格生成的简体中文短进度回复。
        """
        if not self.config.image_enable_llm_tool:
            return self._tool_unavailable("我这会儿还没法拍这个给你看。")
        self._remember_llm_generation(
            event,
            "selfie",
            {
                "action": action,
                "count": count,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "size": size,
            },
        )
        requested_count = self._normalize_count(count)
        action, aspect, resol, _, _ = self._resolve_image_preset(action or "看着镜头自然自拍", aspect_ratio, resolution or size)
        return await self._run_llm_selfie_flow(event, action, requested_count, aspect, resol, ack_message)

    @LLM_TOOL(name="generate_video")
    async def tool_generate_video(
        self,
        event: AstrMessageEvent,
        prompt: str,
        duration: int = 5,
        ack_message: str = "",
    ) -> Optional[str]:
        """
        生成短视频。默认把当前 AI 形象图作为首帧；用户附图时优先使用附图作为首帧。
        用户要求 AI 自己动态、自拍视频、让 AI 出镜动作时使用本工具。
        Args:
            prompt(string): 视频内容，描述动作、镜头、场景和光线。
            duration(number): 视频时长，1-60 秒；留空使用 5 秒。
            ack_message(string): 可选。根据当前对话和机器人人格生成的简体中文短进度回复。
        """
        if not self.config.video_enable:
            return self._tool_unavailable("我这会儿还没法录这个给你看。")
        self._remember_llm_generation(event, "video", {"prompt": prompt, "duration": duration})
        action = str(prompt or "").strip()
        if not action:
            return self._tool_soft_fail("缺少视频内容", "你想让我怎么动？")
        seconds = max(1, min(60, int(duration or self.config.video_default_duration or 5)))
        refs = await self._event_reference_images(
            event,
            include_at_avatar=False,
            context_hint=action,
            allow_context_fallback=True,
        )
        if not refs:
            persona_ref = self._video_persona_reference()
            if persona_ref:
                refs = [persona_ref]
        await self._send_progress_text(
            event,
            await self._build_contextual_progress_text(event, "video", action, 1, ack_message),
        )
        result = await self._run_video_generation(event, action, refs, source="llm-generate-video", duration=seconds)
        if not result.get("success"):
            return self._tool_soft_fail(str(result.get("error") or ""), self._natural_fail_fallback("video"))
        path = str(result.get("video_path") or "")
        if path:
            await self._send_generated_video(event, path, caption="视频好了。")
        return self._tool_success("video", 1)

    @LLM_TOOL(name="retry_last_generation")
    async def tool_retry_last_generation(
        self,
        event: AstrMessageEvent,
        feedback: str = "",
    ) -> Optional[str]:
        """
        重新生成本会话最近一次图片、自拍或视频。
        用户说“再来”“重试”“重新生成”“再试一次”等，或明确指出上一张的问题时必须使用本工具，不能只回复文字。
        feedback 填用户对上一张的明确修改要求，例如“更年轻一点”“不像我”“衣服不对”。
        Args:
            feedback(string): 可选。用户对上一轮结果的具体修正要求；留空时按原要求重新生成。
        """
        previous = self._last_llm_generation(event, feedback)
        kind = str(previous.get("kind") or "")
        params = previous.get("params") if isinstance(previous.get("params"), dict) else {}
        if kind == "image":
            return await self.tool_generate_image(event, **params)
        if kind == "selfie":
            return await self.tool_generate_selfie(event, **params)
        if kind == "video":
            return await self.tool_generate_video(event, **params)
        return self._tool_soft_fail("没有找到本会话最近一次生成请求", "你想重新来哪一张？")
