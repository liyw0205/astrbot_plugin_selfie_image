"""Pose, camera, and legwear rules for the ``看看腿`` workflow."""

from __future__ import annotations

import random
import re
from typing import Dict, List, Tuple


# Random shots focus on the lower outfit while keeping hosiery and shoes
# naturally visible; unusual viewpoints are reserved for explicit requests.
LEGFOCUS_POSE_POOL: List[Dict[str, str]] = [
    {
        "id": "sofa_front_crop",
        "title": "沙发端坐",
        "prompt": (
            "人物自然端坐在沙发前沿，双脚平稳落地，身体放松。"
            "平视拍摄腰线至鞋履，清楚呈现裙摆、袜装与鞋履，并保留少量环境。"
        ),
    },
    {
        "id": "chair_side_crop",
        "title": "椅上侧坐",
        "prompt": (
            "人物在木椅上自然呈三分之一侧坐，身体放松，姿态舒展。"
            "双脚自然落地；平视拍摄腰线至鞋履，清楚呈现裙摆、袜装与鞋履。"
        ),
    },
    {
        "id": "sofa_cross_crop",
        "title": "沙发轻松坐姿",
        "prompt": (
            "人物舒适地坐在沙发上，双脚与地面自然接触，姿态放松，不交叉双腿。"
            "平视拍摄腰线至鞋履，背景简洁自然，袜装和鞋履清晰可见。"
        ),
    },
    {
        "id": "floor_knees_crop",
        "title": "窗边自然站姿",
        "prompt": (
            "人物在窗边自然站立，身体放松，一侧脚向前半步，不过度摆姿。"
            "平视拍摄腰线至鞋履，清楚呈现裙摆、袜装与鞋履，窗户只作普通生活背景。"
        ),
    },
    {
        "id": "sofa_occlusion_crop",
        "title": "沙发自然坐姿",
        "prompt": (
            "人物坐在沙发一侧，背部自然靠住靠垫，姿势放松，双脚完整落地。"
            "平视拍摄腰线至鞋履，沙发和地面关系清楚，不用家具遮挡袜装或鞋履。"
        ),
    },
    {
        "id": "stool_edge_crop",
        "title": "矮凳端坐",
        "prompt": (
            "人物坐在稳固矮凳上，双脚平稳落地，姿态放松并保持自然比例。"
            "平视拍摄腰线至鞋履，凳子和地面的接触关系清楚，袜装与鞋履完整可见。"
        ),
    },
    {
        "id": "floor_side_kneel_crop",
        "title": "户外小径漫步",
        "prompt": (
            "人物在平整的小径上自然行走，步幅轻松，手臂放松，身体重心稳定。"
            "以平视机位拍摄腰线至鞋履，袜装与鞋履自然清晰，背景保持简洁并有自然纵深。"
        ),
    },
    {
        "id": "seat_knees_cross_crop",
        "title": "站姿展示穿搭",
        "prompt": (
            "人物自然站立，双脚平稳分开与肩同宽，一侧脚尖略微转向外，手臂放松。"
            "平视拍摄腰线至鞋履，以自然距离展示裙摆、袜装与鞋履，不使用夸张透视。"
        ),
    },
    {
        "id": "floor_topdown_cross_crop",
        "title": "明亮室内自然坐姿",
        "prompt": (
            "人物坐在稳固的椅子上，双膝自然并行，双脚放在地面；背部舒展，表情放松。"
            "使用平视或略高机位，完整记录头部、整体穿搭与鞋履，保持普通生活照的距离。"
        ),
    },
    {
        "id": "first_person_topdown_legs_crop",
        "title": "第一人称向下看腿",
        "prompt": (
            "仅在用户明确要求第一人称向下看时使用。人物坐姿放松，双腿自然并行向前，脚部完整可见。"
            "采用自然视线角度，保持衣着完整、腿部比例正常，不使用低机位或夸张透视。"
        ),
    },
    {
        "id": "staggered_stair_step_crop",
        "title": "错层踏阶",
        "prompt": (
            "人物站在宽阔平整的石阶上，一只脚稳稳踩在低一级台阶，另一只脚留在上一级，形成自然错层；"
            "重心落在支撑脚上，膝盖朝向一致，可轻扶现场已有的栏杆，不抬高腿。"
            "摄影机在台阶正前方平视，腰线至鞋履完整入镜，阶梯形成清晰引导线，袜装与鞋履可见。"
        ),
    },
    {
        "id": "cross_step_snapshot_crop",
        "title": "交叉步抓拍",
        "prompt": (
            "抓拍人物轻步行走的一瞬间，前脚自然从支撑腿前方迈过，脚掌即将落地，后腿保持承重，"
            "膝盖与脚尖方向协调，脚踝不扭转，手臂随步伐轻微摆动。"
            "摄影机平视拍摄腰线至鞋履，人物与衣料保持清晰，背景纵深自然，不使用夸张透视。"
        ),
    },
    {
        "id": "turn_and_settle_crop",
        "title": "旋身收步",
        "prompt": (
            "人物刚完成轻盈半转并自然收步站稳，身体斜向镜头，前脚轻点地、后脚稳定承重；"
            "肩胯方向协调，双膝不扭转，手臂放松，裙摆只呈现轻微自然动势。"
            "第三人称平视拍摄腰线至鞋履，完整呈现袜装与鞋履，动作清晰而不夸张。"
        ),
    },
    {
        "id": "bench_side_sit_crop",
        "title": "长凳侧坐",
        "prompt": (
            "人物坐在长凳一端，身体自然朝向一侧；靠外一条腿略向前伸，脚跟稳稳落地，另一腿弯曲并自然支撑，"
            "双膝和脚尖方向一致，可用一只手轻扶凳面保持平衡。"
            "摄影机从斜前方平视拍摄腰线至鞋履，长凳只作清楚支撑，不遮挡袜装和鞋履。"
        ),
    },
    {
        "id": "toe_touch_step_crop",
        "title": "脚尖点阶",
        "prompt": (
            "人物站在宽阔低矮的台阶旁，一条支撑腿平稳站在地面，另一只脚尖轻点面前第一阶台面，"
            "点阶侧膝盖微弯但不抬高，重心稳定，双腿保持自然距离。"
            "第三人称平视拍摄腰线至鞋履，从侧前方清楚展示步履层次、袜装与鞋履，不采用低机位。"
        ),
    },
    {
        "id": "back_staggered_step_crop",
        "title": "背面错步",
        "prompt": (
            "人物背对镜头自然站立，双脚前后错开一个舒适步幅，前脚轻点地面、后脚稳定承重；"
            "肩线、胯部和头部朝向一致，不回头、不回眸、不扭颈，手臂放松垂在身侧。"
            "摄影机从人物后方平视拍摄腰线至鞋履，清楚呈现服装后侧、袜装和鞋履，比例自然。"
        ),
    },
]

_LEGFOCUS_POSE_IDS = frozenset(str(item["id"]) for item in LEGFOCUS_POSE_POOL)
LEGFOCUS_EXPLICIT_ONLY_POSE_IDS = frozenset(
    {"floor_topdown_cross_crop", "first_person_topdown_legs_crop"}
)

LEGFOCUS_SCENE_TERMS = (
    "咖啡馆", "咖啡店", "咖啡厅", "休息区", "长凳", "座椅", "书桌", "窗边",
    "卧室", "客厅", "厨房", "书店", "茶室", "餐廊", "浴室", "走廊", "家中", "室内",
    "沙发", "床边", "街上", "街边", "天台", "海边", "河边", "公园", "阳台", "室外",
    "庭院", "花园", "小径", "步道", "石阶", "楼梯", "草地", "户外",
)

# Older persisted actions use these broad pose ids. Resolve them to the newer
# complete compositions while keeping the original tag for prompt compatibility.
LEGFOCUS_POSE_ALIASES = {
    "sit": "sofa_front_crop",
    "sit_crop": "sofa_front_crop",
    "kneel": "floor_knees_crop",
    "kneel_crop": "floor_knees_crop",
    "side_lie": "sofa_cross_crop",
    "side_lie_crop": "sofa_cross_crop",
    "hug_knee": "floor_knees_crop",
    "hug_knee_crop": "floor_knees_crop",
    "cross_leg": "sofa_cross_crop",
    "cross_leg_crop": "sofa_cross_crop",
    "windowsill": "chair_side_crop",
    "windowsill_crop": "chair_side_crop",
    "kneel_up": "floor_knees_crop",
    "kneel_front": "floor_knees_crop",
    "floor_fold": "floor_knees_crop",
    "one_knee_fix": "floor_side_kneel_crop",
    "reclined_knees_crop": "seat_knees_cross_crop",
    "floor_knees_up_crop": "floor_knees_crop",
    "desk_sit_crop": "chair_side_crop",
    "bed_supine_crop": "sofa_cross_crop",
}


# One entry is selected as a whole so a generic request never leaves bottoms,
# legwear, or shoes for the model to invent independently.
LEGFOCUS_OUTFIT_POOL: List[Dict[str, str]] = [
    {
        "id": "blue_plaid_pleated",
        "legwear": "连裤袜",
        "bottoms": "浅蓝色及膝格纹百褶裙",
        "skirt": "浅蓝色及膝格纹百褶裙",
        "shoes": "深棕色圆头平底鞋",
        "prompt_en": "a light-blue knee-length plaid pleated skirt, opaque full-length tights, and dark-brown round-toe flats",
    },
    {
        "id": "navy_a_line",
        "legwear": "连裤袜",
        "bottoms": "深蓝色及膝A字裙",
        "skirt": "深蓝色及膝A字裙",
        "shoes": "黑色玛丽珍平底鞋",
        "prompt_en": "a navy knee-length A-line skirt, opaque full-length tights, and black Mary Jane flats",
    },
    {
        "id": "cream_pleated",
        "legwear": "白丝",
        "bottoms": "奶油白中长细褶半身裙",
        "skirt": "奶油白中长细褶半身裙",
        "shoes": "浅棕色乐福平底鞋",
        "prompt_en": "a cream midi pleated skirt, opaque white full-length tights, and light-brown loafers",
    },
    {
        "id": "gray_check",
        "legwear": "白丝",
        "bottoms": "灰色及膝格纹百褶裙",
        "skirt": "灰色及膝格纹百褶裙",
        "shoes": "黑色圆头平底鞋",
        "prompt_en": "a gray knee-length plaid pleated skirt, opaque white full-length tights, and black round-toe flats",
    },
    {
        "id": "burgundy_pleated",
        "legwear": "黑丝",
        "bottoms": "酒红色及膝百褶裙",
        "skirt": "酒红色及膝百褶裙",
        "shoes": "黑色圆头平底鞋",
        "prompt_en": "a burgundy knee-length pleated skirt, opaque black full-length tights, and black round-toe flats",
    },
    {
        "id": "denim_a_line",
        "legwear": "黑丝",
        "bottoms": "深蓝色及膝牛仔A字裙",
        "skirt": "深蓝色及膝牛仔A字裙",
        "shoes": "白色低帮帆布鞋",
        "prompt_en": "a dark-blue knee-length denim A-line skirt, opaque black full-length tights, and white low-top canvas shoes",
    },
    {
        "id": "oatmeal_knit",
        "legwear": "光腿神器",
        "bottoms": "燕麦色中长针织半身裙",
        "skirt": "燕麦色中长针织半身裙",
        "shoes": "米色软底平底鞋",
        "prompt_en": "an oatmeal midi knit skirt, opaque skin-tone tights, and beige soft flats",
    },
    {
        "id": "black_corduroy",
        "legwear": "光腿神器",
        "bottoms": "黑色及膝灯芯绒A字裙",
        "skirt": "黑色及膝灯芯绒A字裙",
        "shoes": "深棕色乐福鞋",
        "prompt_en": "a black knee-length corduroy A-line skirt, opaque skin-tone tights, and dark-brown loafers",
    },
    {
        "id": "straight_denim_trousers",
        "legwear": "日常裤装",
        "bottoms": "深蓝色直筒牛仔长裤",
        "shoes": "白色低帮帆布鞋",
        "prompt_en": "straight dark-blue denim trousers with a full-length hem and white low-top canvas shoes",
    },
    {
        "id": "charcoal_tailored_trousers",
        "legwear": "日常裤装",
        "bottoms": "炭灰色直筒西裤",
        "shoes": "黑色简洁乐福鞋",
        "prompt_en": "charcoal straight-leg tailored trousers and simple black loafers",
    },
    {
        "id": "olive_cotton_trousers",
        "legwear": "日常裤装",
        "bottoms": "橄榄灰棉质直筒长裤",
        "shoes": "米白色休闲鞋",
        "prompt_en": "olive-gray straight-leg cotton trousers and off-white casual shoes",
    },
    {
        "id": "soft_gray_wide_trousers",
        "legwear": "日常裤装",
        "bottoms": "浅灰色垂感阔腿长裤",
        "shoes": "深棕色平底鞋",
        "prompt_en": "soft light-gray full-length wide-leg trousers and dark-brown flats",
    },
]
_LEGFOCUS_OUTFIT_BY_ID = {str(item["id"]): item for item in LEGFOCUS_OUTFIT_POOL}


LEGWEAR_BY_POSE = {
    "sofa_front_crop": (("白丝", 4), ("黑丝", 4), ("光腿神器", 2)),
    "chair_side_crop": (("白丝", 4), ("黑丝", 4), ("光腿神器", 2)),
    "sofa_cross_crop": (("白丝", 4), ("黑丝", 4), ("光腿神器", 2)),
    "floor_knees_crop": (("白丝", 4), ("黑丝", 4), ("光腿神器", 2)),
    "sofa_occlusion_crop": (("白丝", 4), ("黑丝", 4), ("光腿神器", 2)),
    "stool_edge_crop": (("白丝", 4), ("黑丝", 4), ("光腿神器", 2)),
    "floor_side_kneel_crop": (("白丝", 4), ("黑丝", 4), ("光腿神器", 2)),
    "seat_knees_cross_crop": (("白丝", 4), ("黑丝", 4), ("光腿神器", 2)),
    "floor_topdown_cross_crop": (("白丝", 4), ("黑丝", 4), ("光腿神器", 2)),
    "first_person_topdown_legs_crop": (("白丝", 4), ("黑丝", 4), ("光腿神器", 2)),
    "staggered_stair_step_crop": (("白丝", 4), ("黑丝", 4), ("光腿神器", 2)),
    "cross_step_snapshot_crop": (("白丝", 4), ("黑丝", 4), ("光腿神器", 2)),
    "turn_and_settle_crop": (("白丝", 4), ("黑丝", 4), ("光腿神器", 2)),
    "bench_side_sit_crop": (("白丝", 4), ("黑丝", 4), ("光腿神器", 2)),
    "toe_touch_step_crop": (("白丝", 4), ("黑丝", 4), ("光腿神器", 2)),
    "back_staggered_step_crop": (("白丝", 4), ("黑丝", 4), ("光腿神器", 2)),
    # Keep old keys readable for persisted actions and third-party callers.
    "sit": (("光腿神器", 4), ("白丝", 3), ("黑丝", 3), ("连裤袜", 2)),
    "sit_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 5), ("连裤袜", 2)),
    "kneel": (("光腿神器", 5), ("白丝", 3), ("黑丝", 2), ("连裤袜", 2)),
    "kneel_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 5), ("连裤袜", 2)),
    "side_lie": (("光腿神器", 6), ("白丝", 3), ("黑丝", 1), ("连裤袜", 2)),
    "side_lie_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 4), ("连裤袜", 2)),
    "hug_knee": (("光腿神器", 5), ("白丝", 3), ("黑丝", 2), ("连裤袜", 2)),
    "hug_knee_crop": (("光腿神器", 4), ("白丝", 3), ("黑丝", 3), ("连裤袜", 2)),
    "cross_leg": (("光腿神器", 2), ("白丝", 4), ("黑丝", 4), ("连裤袜", 2)),
    "cross_leg_crop": (("光腿神器", 2), ("白丝", 4), ("黑丝", 4), ("连裤袜", 2)),
    "windowsill": (("光腿神器", 5), ("白丝", 3), ("黑丝", 2), ("连裤袜", 2)),
    "windowsill_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 5), ("连裤袜", 2)),
    "kneel_up": (("光腿神器", 5), ("白丝", 2), ("黑丝", 3), ("连裤袜", 2)),
    "kneel_front": (("光腿神器", 6), ("白丝", 2), ("黑丝", 2), ("连裤袜", 2)),
    "floor_fold": (("光腿神器", 6), ("白丝", 2), ("黑丝", 2), ("连裤袜", 2)),
    "one_knee_fix": (("光腿神器", 4), ("白丝", 3), ("黑丝", 3), ("连裤袜", 2)),
    "reclined_knees_crop": (("光腿神器", 1), ("白丝", 6), ("黑丝", 5), ("连裤袜", 2)),
    "floor_knees_up_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 5), ("连裤袜", 2)),
    "desk_sit_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 5), ("连裤袜", 2)),
    "bed_supine_crop": (("光腿神器", 2), ("白丝", 5), ("黑丝", 4), ("连裤袜", 2)),
}

CALF_CROP_POSES = frozenset(
    set(_LEGFOCUS_POSE_IDS)
    | {name for name in LEGWEAR_BY_POSE if name.endswith("_crop")}
)

LEGFOCUS_CAMERA_WEIGHTS = {
    "sofa_front_crop": (("third", 1),),
    "chair_side_crop": (("third", 1),),
    "sofa_cross_crop": (("third", 1),),
    "floor_knees_crop": (("third", 1),),
    "sofa_occlusion_crop": (("third", 1),),
    "stool_edge_crop": (("third", 1),),
    "floor_side_kneel_crop": (("third", 1),),
    "seat_knees_cross_crop": (("third", 1),),
    "floor_topdown_cross_crop": (("third", 1),),
    "first_person_topdown_legs_crop": (("selfie", 1),),
    "staggered_stair_step_crop": (("third", 1),),
    "cross_step_snapshot_crop": (("third", 1),),
    "turn_and_settle_crop": (("third", 1),),
    "bench_side_sit_crop": (("third", 1),),
    "toe_touch_step_crop": (("third", 1),),
    "back_staggered_step_crop": (("third", 1),),
    # Compatibility weights for actions generated before the pool migration.
    "sit": (("selfie", 3), ("third", 2)),
    "sit_crop": (("selfie", 4), ("third", 1)),
    "side_lie": (("selfie", 3), ("third", 2)),
    "side_lie_crop": (("selfie", 4), ("third", 1)),
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

LEGWEAR_PROMPTS = {
    "日常裤装": "本次下装搭配：不透日常长裤，裤脚自然落至鞋面，作为完整得体的日常穿搭。",
    "用户指定": "本次服装搭配按用户明确指定的下装和鞋履执行。",
    "光腿神器": "本次腿部穿搭：自然肤色不透打底，完整覆盖至脚踝，材质自然，作为得体日常穿搭的一部分。",
    "白丝": "本次腿部穿搭：白色不透连裤袜，腰部至脚踝连续覆盖，作为得体日常穿搭的一部分。",
    "黑丝": "本次腿部穿搭：黑色不透连裤袜，腰部至脚踝连续覆盖，作为得体日常穿搭的一部分。",
    "连裤袜": "本次腿部穿搭：白色不透连裤袜，腰部至脚踝连续覆盖，作为得体日常穿搭的一部分。",
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

LEG_DETAIL_REQUEST_PATTERN = re.compile(
    r"(?:下半身特写|腿部特写|下装特写|局部特写|腰部以下|只拍腿|只拍下装|不拍上半身|上半身不入镜|不露脸|近距离拍腿)"
)
LEG_FULL_OUTFIT_REQUEST_PATTERN = re.compile(
    r"(?:全身|从头到脚|头部到鞋履|全身入镜|全身构图|full[- ]?body)",
    re.I,
)

LEGFOCUS_RISKY_EXTRA_REPLACEMENTS = (
    ("掀衣摆", "整理衣摆"),
    ("晒腿", "记录穿搭"),
    ("主要看腿形", "重点看服装版型"),
    ("看腿形", "看服装版型"),
    ("不露脸", "服装局部取景"),
)

LEGFOCUS_OUTFIT_REQUEST_PATTERN = re.compile(
    r"(?:裙|百褶|A字|半身|裤|鞋|靴|平底|乐福|玛丽珍|帆布鞋|凉鞋)"
)

SAFE_LEGWEAR_LABELS = {
    "日常裤装": "不透日常长裤，裤脚自然落至鞋面",
    "用户指定": "按用户指定的完整下装与鞋履",
    "光腿神器": "自然肤色不透打底，完整覆盖至脚踝",
    "白丝": "白色不透连裤袜，腰部至脚踝连续覆盖",
    "黑丝": "黑色不透连裤袜，腰部至脚踝连续覆盖",
    "连裤袜": "白色不透连裤袜，腰部至脚踝连续覆盖",
}


def is_leg_calf_crop_action(text: str) -> bool:
    raw = str(text or "")
    frame_match = re.search(r"【frame:(full_outfit|leg_detail)】", raw)
    if frame_match:
        return frame_match.group(1) == "leg_detail"
    if LEG_DETAIL_REQUEST_PATTERN.search(raw):
        return True
    match = re.search(r"【pose:([a-z_]+)】", raw)
    if match and (match.group(1) in CALF_CROP_POSES or match.group(1).endswith("_crop")):
        return True
    if "【crop:calves】" in raw or "双脚完整裁出画外" in raw or "不展示脚部" in raw:
        return True
    return "大腿" in raw and "小腿" in raw and "画外" in raw


def pick_stocking_finish() -> str:
    bag: List[str] = []
    for text, weight in STOCKING_FINISH_CHOICES:
        bag.extend([text] * max(1, int(weight)))
    return random.choice(bag)


def _has_positive_leg_scene_term(text: str, term: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    start = 0
    while True:
        index = compact.find(term, start)
        if index < 0:
            return False
        prefix = compact[max(0, index - 6) : index]
        if not re.search(r"(?:不要|不在|不去|不选|避免|禁止|别)(?:再|去|在|用|选择|采用)?$", prefix):
            return True
        start = index + len(term)


def parse_requested_leg_scene(text: str) -> str:
    """Extract a concrete scene hint used only for pose compatibility."""
    raw = str(text or "")
    raw = re.sub(r"场景不变|背景不变|保持场景|保持背景", "", raw)
    matched = [
        term for term in sorted(LEGFOCUS_SCENE_TERMS, key=len, reverse=True)
        if _has_positive_leg_scene_term(raw, term)
    ]
    return " ".join(dict.fromkeys(matched))


def _leg_pose_compatible_with_camera(pose: Dict[str, str], camera: str) -> bool:
    if str(camera or "").strip() != "selfie":
        return True
    prompt = str(pose.get("prompt") or "")
    return not re.search(r"第三人称|摄影机|摄影者", prompt)


def _adapt_leg_pose_prompt_for_camera(prompt: str, camera: str) -> str:
    """Remove third-person wording when a legacy pose is used for a selfie."""
    if str(camera or "").strip() != "selfie":
        return prompt
    adapted = str(prompt or "")
    replacements = (
        ("第三人称平视拍摄", "第一人称自然视角平视拍摄"),
        ("摄影机在台阶正前方平视", "自拍镜头在台阶正前方平视"),
        ("摄影机从斜前方平视拍摄", "自拍镜头从斜前方平视拍摄"),
        ("摄影机从人物后方平视拍摄", "画外固定镜头从人物后方平视拍摄"),
        ("摄影机平视拍摄", "自拍镜头平视拍摄"),
        ("摄影者完全在画面外", "拍摄设备留在画面外"),
    )
    for source, target in replacements:
        adapted = adapted.replace(source, target)
    return adapted


def _leg_pose_compatible_with_scene(pose: Dict[str, str], scene: str) -> bool:
    scene_text = str(scene or "")
    if not scene_text:
        return True
    pose_id = str(pose.get("id") or "")
    indoor = any(token in scene_text for token in ("室内", "卧室", "客厅", "咖啡", "书店", "劇院", "剧院", "餐廊", "厨房", "家中", "室内"))
    outdoor = any(token in scene_text for token in ("街", "海", "河岸", "码头", "天台", "公园", "户外", "露台", "步道", "庭院", "石阶", "小径"))
    if pose_id in {"sofa_front_crop", "sofa_cross_crop", "sofa_occlusion_crop"}:
        return any(token in scene_text for token in ("沙发", "床", "客厅", "休息区"))
    if pose_id == "chair_side_crop":
        return any(token in scene_text for token in ("椅", "座位", "咖啡", "书桌", "餐廊", "茶室", "长凳"))
    if pose_id == "floor_knees_crop":
        return "窗" in scene_text
    if pose_id == "floor_side_kneel_crop":
        return any(token in scene_text for token in ("小径", "步道", "草地", "庭院", "花园"))
    if pose_id in {"staggered_stair_step_crop", "toe_touch_step_crop"}:
        return any(token in scene_text for token in ("台阶", "石阶", "楼梯"))
    if pose_id == "bench_side_sit_crop":
        return "长凳" in scene_text or "座椅" in scene_text
    if pose_id in {"cross_step_snapshot_crop", "turn_and_settle_crop"}:
        return not indoor or any(token in scene_text for token in ("走廊", "大厅", "通道"))
    if pose_id == "back_staggered_step_crop":
        return not indoor or any(token in scene_text for token in ("空地", "走廊", "大厅"))
    if pose_id == "stool_edge_crop":
        return any(token in scene_text for token in ("矮凳", "座位", "咖啡", "茶室", "书桌"))
    if outdoor and not indoor and pose_id in {"sofa_front_crop", "sofa_cross_crop", "sofa_occlusion_crop", "stool_edge_crop"}:
        return False
    return True


def pick_leg_focus_pose(
    *, avoid_id: str = "", preferred_scene: str = "", camera_kind: str = ""
) -> Dict[str, str]:
    """Pick one neutral full-length pose, avoiding explicit detail-only views."""
    pool = [
        item for item in LEGFOCUS_POSE_POOL
        if str(item.get("id") or "") not in LEGFOCUS_EXPLICIT_ONLY_POSE_IDS
        and str(item.get("id") or "") != str(avoid_id or "")
        and _leg_pose_compatible_with_scene(item, preferred_scene)
        and _leg_pose_compatible_with_camera(item, camera_kind)
    ]
    if not pool:
        pool = [
            item for item in LEGFOCUS_POSE_POOL
            if str(item.get("id") or "") != str(avoid_id or "")
            and _leg_pose_compatible_with_camera(item, camera_kind)
        ] or list(LEGFOCUS_POSE_POOL)
    return dict(random.choice(pool))


def pick_leg_focus_outfit(*, legwear: str = "", avoid_id: str = "") -> Dict[str, str]:
    """Pick one complete bottoms, legwear, and shoe combination."""
    pool = [
        item for item in LEGFOCUS_OUTFIT_POOL
        if (not legwear or str(item.get("legwear") or "") == legwear)
        and str(item.get("id") or "") != str(avoid_id or "")
    ]
    if not pool:
        pool = [
            item for item in LEGFOCUS_OUTFIT_POOL
            if not legwear or str(item.get("legwear") or "") == legwear
        ] or list(LEGFOCUS_OUTFIT_POOL)
    return dict(random.choice(pool))


def get_leg_focus_outfit(outfit_id: str) -> Dict[str, str]:
    """Return a validated copy for prompt renderers."""
    return dict(_LEGFOCUS_OUTFIT_BY_ID.get(str(outfit_id or "").strip(), {}))


def has_explicit_leg_outfit(text: str) -> bool:
    """Whether the user supplied a skirt, trouser, or shoe description."""
    cleaned = re.sub(
        r"(?:白色|黑色|肉色)?\s*(?:不透)?(?:连裤袜|白丝|黑丝|丝袜|肉丝|光腿神器|光腿)",
        " ",
        str(text or ""),
    )
    cleaned = re.sub(r"(?:参考图中的)?鞋袜搭配|鞋袜", " ", cleaned)
    return bool(LEGFOCUS_OUTFIT_REQUEST_PATTERN.search(cleaned))


def parse_requested_leg_camera(text: str) -> str:
    """Return an explicitly requested capture direction, if present."""
    raw = str(text or "")
    compact = re.sub(r"\s+", "", raw).lower()
    third_keys = (
        "第三人称", "第三人称视角", "他拍", "别人拍", "朋友拍", "摄影师拍",
        "路人视角", "不要自拍", "非自拍", "notselfie", "thirdperson",
    )
    selfie_keys = (
        "第一人称", "第一人称视角", "第一视角", "主观视角", "手机自拍", "自拍", "自己拍", "selfie",
    )
    if any(key in compact for key in third_keys):
        return "third"
    if any(key in compact for key in selfie_keys):
        return "selfie"
    return ""


def parse_requested_leg_pose(text: str) -> str:
    """Map explicit first-person or generic top-down wording to a complete pose."""
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    first_person_downward = (
        any(key in compact for key in ("第一人称", "第一视角", "主观视角", "自己视线"))
        and any(
            key in compact
            for key in ("俯视", "向下看", "往下看", "镜头朝下", "看向腿", "看腿")
        )
    )
    extended_legs = any(
        key in compact
        for key in (
            "向下看腿", "俯视双腿", "俯拍伸腿", "俯视伸腿",
            "双腿平行伸展", "双腿平行向前伸展",
        )
    )
    if first_person_downward or extended_legs:
        return "first_person_topdown_legs_crop"
    if any(key in compact for key in ("俯拍", "正上方", "顶拍", "topdown", "top-down")):
        return "floor_topdown_cross_crop"
    return ""


def parse_requested_legwear(text: str) -> str:
    """Honor explicit user legwear; empty means random selection by pose."""
    raw = str(text or "")
    safe_tag = re.search(r"【wear:(daily|custom|white|black|bare|pantyhose)】", raw)
    if safe_tag:
        return {
            "daily": "日常裤装",
            "custom": "用户指定",
            "white": "白丝",
            "black": "黑丝",
            "bare": "光腿神器",
            "pantyhose": "连裤袜",
        }[safe_tag.group(1)]
    match = re.search(r"本次(?:腿部穿搭|服装搭配|下装搭配)(?:已锁定为)?[:：]\s*(日常裤装|光腿神器|白丝|黑丝|连裤袜)", raw)
    if match:
        return str(match.group(1) or "").strip()
    cleaned = re.sub(r"若本次是白丝/黑丝[^。\n]*。?", " ", raw)
    cleaned = re.sub(r"光腿神器[、,，/]白丝[、,，/或]黑丝", " ", cleaned)
    cleaned = re.sub(r"白丝/黑丝", " ", cleaned)
    if re.search(r"日常裤装|长裤|牛仔裤|西裤|休闲裤|阔腿裤", cleaned):
        return "日常裤装"
    if re.search(r"连裤袜", cleaned):
        return "连裤袜"
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
    force_pose: str = "",
    force_camera: str = "",
    force_framing: str = "",
    preferred_scene: str = "",
) -> str:
    """Build a complete outfit action for ``看看腿`` with optional local framing."""
    explicit_scene = parse_requested_leg_scene(extra_request)
    pose_scene = explicit_scene or str(preferred_scene or "")
    schedule_scene = "" if explicit_scene else str(preferred_scene or "")
    camera_kind = str(force_camera or "").strip()
    if camera_kind not in {"selfie", "third"}:
        camera_kind = parse_requested_leg_camera(extra_request)
    forced_pose_id = str(force_pose or "").strip()
    if not forced_pose_id:
        forced_pose_id = parse_requested_leg_pose(extra_request)
    pose_tag = forced_pose_id
    canonical_pose_id = LEGFOCUS_POSE_ALIASES.get(forced_pose_id, forced_pose_id)
    pose = next(
        (dict(item) for item in LEGFOCUS_POSE_POOL if str(item.get("id") or "") == canonical_pose_id),
        None,
    )
    if pose is None:
        pose = pick_leg_focus_pose(
            avoid_id=avoid_pose,
            preferred_scene=pose_scene,
            camera_kind=camera_kind,
        )
        pose_tag = ""
    pose_bucket = str(pose.get("id") or "sofa_front_crop")
    if not pose_tag:
        pose_tag = pose_bucket
    pose_prompt = str(pose.get("prompt") or "").strip()
    camera_bag = [
        kind
        for kind, weight in LEGFOCUS_CAMERA_WEIGHTS.get(
            pose_bucket, (("selfie", 1), ("third", 1))
        )
        for _ in range(weight)
    ]
    if camera_kind not in {"selfie", "third"}:
        camera_kind = random.choice(camera_bag)
    pose_prompt = _adapt_leg_pose_prompt_for_camera(pose_prompt, camera_kind)
    requested_framing = str(force_framing or "").strip()
    if requested_framing not in {"full_outfit", "leg_detail"}:
        explicit_detail = bool(LEG_DETAIL_REQUEST_PATTERN.search(extra_request))
        explicit_full_outfit = bool(
            LEG_FULL_OUTFIT_REQUEST_PATTERN.search(extra_request)
            and not re.search(r"(?:不|不要|无需|不需要)\s*(?:全身|从头到脚)", extra_request)
        )
        requested_framing = (
            "full_outfit"
            if explicit_full_outfit and not explicit_detail
            else "leg_detail"
        )
    is_detail = requested_framing == "leg_detail"
    if camera_kind == "selfie":
        camera_line = (
            "第一人称自然视角服装照：腰线至鞋履入镜，清楚呈现下装、袜装与鞋履。"
            if is_detail
            else "全身镜前自拍：人物从头部到鞋履完整入镜，镜中只有主角，手机自然持握且不遮挡主要穿搭。"
        )
    else:
        camera_line = (
            "第三人称腿部穿搭照片：摄影者完全在画面外，平视拍摄腰线至鞋履，保留少量环境。"
            if is_detail
            else "第三人称全身日常照片：摄影者完全在画面外，人物从头部到鞋履完整入镜。"
        )
    framing_rule = (
        "采用腰线至鞋履的腿部穿搭构图，自然展示裙摆、袜装与鞋履；保持正常拍摄距离、平视机位和适量环境，不使用夸张透视。"
        if is_detail
        else "全身穿搭构图，头部、上衣、下装和鞋履完整入镜；镜头保持正常拍摄距离，不集中裁切身体局部。"
    )
    leg_continuity_rule = (
        "腰线至鞋履的构图完整自然，袜装和鞋履清楚可见，不被家具遮挡；人物比例与肢体结构自然连续。"
        if is_detail
        else "人物身体比例自然，左右肢体结构清晰，站坐重心稳定；衣物与鞋履边缘完整呈现。"
    )
    anatomy_rules = "单人、成年人；姿态放松自然，服装完整合身，呈现清晰的日常穿搭照片。"
    requested = str(force_legwear or "").strip()
    if requested not in LEGWEAR_PROMPTS:
        requested = parse_requested_legwear(extra_request)
    explicit_outfit = has_explicit_leg_outfit(extra_request)
    if requested in LEGWEAR_PROMPTS:
        legwear = requested
    elif explicit_outfit:
        legwear = "用户指定"
    else:
        legwear_options = LEGWEAR_BY_POSE.get(
            pose_bucket, LEGWEAR_BY_POSE["sit_crop"]
        )
        legwear = random.choices(
            [name for name, _ in legwear_options],
            weights=[weight for _, weight in legwear_options],
            k=1,
        )[0]
    outfit = {} if explicit_outfit else pick_leg_focus_outfit(legwear=legwear)
    legwear_label = SAFE_LEGWEAR_LABELS.get(
        legwear, "代码选定的单一腿部穿搭"
    )
    legwear_rule = (
        f"本次服装搭配已锁定为：{legwear_label}；只生成这一套日常下装。"
        if legwear == "日常裤装"
        else f"本次服装搭配已锁定为：{legwear_label}；按该单一选项完成日常穿搭。"
    )
    outfit_rule = ""
    outfit_tag = "custom"
    if outfit:
        outfit_tag = str(outfit.get("id") or "custom")
        outfit_rule = (
            f"完整下装服饰组合已由代码锁定：{outfit.get('bottoms') or outfit.get('skirt', '')}、"
            f"{outfit.get('shoes', '')}；只生成这一套组合，不自行替换下装或鞋履。"
        )
    base = (
        "成年人物日常穿搭记录。竖屏照片。"
        f"{camera_line}"
        "【legs:outfit】"
        f"【frame:{requested_framing}】"
        f"【姿势池·{pose.get('title') or '随机姿势'}】{pose_prompt}"
        f"{'今日日程场景参考：' + schedule_scene + '；腿部姿势已按该场景筛选。' if schedule_scene else ''}"
        f"{legwear_rule}"
        f"{outfit_rule}"
        f"{anatomy_rules}"
        f"{framing_rule}"
        f"{leg_continuity_rule}"
    )
    if has_refs:
        base += " 用户提供的图片只参考氛围、构图、姿势和场景环境；服装按本次锁定的完整搭配执行，主角身份仍以 AI 形象参考图为准。"
    extra = str(extra_request or "")
    # Camera and top-down words are already resolved into tags and pose text.
    extra = re.sub(
        r"(?:第一人称(?:视角)?|第三人称(?:视角)?|手机自拍|自拍|自己拍|他拍|别人拍|朋友拍|摄影师拍|俯拍|正上方|顶拍|top[- ]?down)",
        " ",
        extra,
        flags=re.I,
    )
    # Remove a colour adjective together with a stocking term so it does not
    # leave a dangling phrase such as "搭配白色" in the supplement.
    extra = re.sub(r"(?:白色|黑色|肉色)?\s*(?:不透)?(?:连裤袜|白丝|黑丝|丝袜|肉丝)", " ", extra)
    # Removing an explicit stocking can leave a dangling "搭配 和" phrase.
    extra = re.sub(r"搭配\s*(?:和\s*)?", " ", extra, count=1)
    extra = extra.replace("百褶短裙", "百褶裙")
    extra = re.sub(r"短裙", "日常裙装", extra)
    extra = re.sub(r"坐姿\s*[,，]?\s*双腿交叉", "", extra)
    extra = LEGWEAR_REQUEST_PATTERN.sub("", extra)
    for risky_text, neutral_text in LEGFOCUS_RISKY_EXTRA_REPLACEMENTS:
        extra = extra.replace(risky_text, neutral_text)
    extra = re.sub(r"\s+", " ", extra).strip(" 。、，")
    extra = re.sub(r"\s*([，,])\s*", r"\1", extra)
    extra = re.sub(r"[。．]+", "。", extra).strip(" 。、，")
    if extra:
        base = base.rstrip("。") + f"。用户补充要求优先：{extra}。"
    wear_tag = {
        "日常裤装": "daily",
        "用户指定": "custom",
        "白丝": "white",
        "黑丝": "black",
        "光腿神器": "bare",
        "连裤袜": "pantyhose",
    }.get(
        legwear, "daily"
    )
    return base + f" 【cam:{camera_kind}】 【wear:{wear_tag}】 【pose:{pose_tag}】 【outfit:{outfit_tag}】"


def has_leg_focus_contract(text: str) -> bool:
    """Whether an action already carries one concrete pose, camera, and wear."""
    raw = str(text or "")
    return bool(
        "【legs:outfit】" in raw
        and re.search(r"【cam:(?:selfie|third)】", raw)
        and re.search(r"【wear:(?:daily|custom|white|black|bare|pantyhose)】", raw)
        and re.search(r"【frame:(?:full_outfit|leg_detail)】", raw)
        and re.search(r"【pose:[a-z_]+】", raw)
        and re.search(r"【outfit:[a-z_]+】", raw)
    )


def ensure_leg_focus_action(text: str, has_refs: bool = False) -> str:
    """Complete raw/incomplete leg requests before prompt rendering.

    This keeps direct callers of the prompt builders consistent with the command
    path, which normally performs the same normalization in ``main.py``.
    """
    raw = str(text or "").strip()
    if not raw or has_leg_focus_contract(raw):
        return raw
    pose_match = re.search(r"【pose:([a-z_]+)】", raw)
    camera_match = re.search(r"【cam:(selfie|third)】", raw)
    wear_match = re.search(r"【wear:(daily|custom|white|black|bare|pantyhose)】", raw)
    forced_pose = str(pose_match.group(1) if pose_match else "").strip()
    forced_camera = str(camera_match.group(1) if camera_match else "").strip()
    requested_pose = parse_requested_leg_pose(raw)
    requested_camera = parse_requested_leg_camera(raw)
    if not forced_pose:
        forced_pose = requested_pose
    if not forced_camera:
        forced_camera = requested_camera
    forced_wear = parse_requested_legwear(raw)
    frame_match = re.search(r"【frame:(full_outfit|leg_detail)】", raw)
    forced_framing = str(frame_match.group(1) if frame_match else "")
    extra = re.sub(r"/?看看腿", " ", raw, flags=re.I)
    extra = re.sub(r"【(?:legs|cam|wear|pose|outfit|frame):[a-z_]+】", " ", extra, flags=re.I)
    # These words have already been resolved into concrete tags; retaining them
    # would restate the same choice in the free-form supplement.
    extra = re.sub(
        r"(?:第一人称(?:视角)?|第三人称(?:视角)?|手机自拍|自拍|自己拍|他拍|别人拍|朋友拍|摄影师拍|俯拍|正上方|顶拍|top[- ]?down)",
        " ",
        extra,
        flags=re.I,
    )
    extra = re.sub(r"\s+", " ", extra).strip(" 。、，")
    return build_leg_focus_action(
        extra,
        has_refs,
        force_legwear=forced_wear,
        force_pose=forced_pose,
        force_camera=forced_camera,
        force_framing=forced_framing,
    )
