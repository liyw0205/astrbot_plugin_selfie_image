"""Studio / 画布工作区 — multi-reference image iteration.

Inspired by infinite-canvas workflows, but stored server-side and generated
through Selfie's existing channel pipeline (no browser-held API keys).
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from astrbot.api import logger

from ..cos.cos_looks import build_cos_third_person_prompt, looks_like_cos_prompt
from ..core.utils import save_json_file, save_json_file_compact


STUDIO_FILENAME = "studio_sessions.json"
MAX_SESSIONS = 40
MAX_SLOTS = 12
MAX_RESULTS_KEEP = 24
CANVAS_MAX_NODES = 128
CANVAS_NODE_GAP_X = 340
CANVAS_NODE_GAP_Y = 220

# Built-in prompt chips / shared presets (no external GitHub sync).
# templates: only listed templates see the chip; empty = all templates.
# global=True: also appear in 画布/试画「预设」总列表.
BUILTIN_PROMPTS: List[Dict[str, Any]] = [
    {
        "id": "duo_warm",
        "title": "双人温馨",
        "prompt": "两人自然并肩合影，轻松微笑，看向镜头，日常暖光",
        "templates": ["duo", "group"],
    },
    {
        "id": "duo_fun",
        "title": "双人活泼",
        "prompt": "双人轻松搞怪合影，比心或比耶，氛围愉快，看向镜头",
        "templates": ["duo", "group"],
    },
    {
        "id": "group_warm",
        "title": "多人温馨",
        "prompt": "自然并肩合影，轻松微笑，看向镜头，日常暖光",
        "templates": ["group"],
    },
    {
        "id": "group_fun",
        "title": "多人活泼",
        "prompt": "轻松搞怪合影，比心或比耶，氛围愉快，看向镜头",
        "templates": ["group"],
    },
    {
        "id": "selfie_soft",
        "title": "自拍柔光",
        "prompt": "看着镜头自然自拍，半身，柔和光线，轻松表情",
        "templates": ["selfie"],
    },
    {
        "id": "selfie_mirror",
        "title": "镜前自拍",
        "prompt": "镜前半身自拍，自然看镜头，日常居家光线",
        "templates": ["selfie"],
    },
    {
        "id": "clothes_cos",
        "title": "换装COS",
        "prompt": "穿着参考图服装自拍，表情自然，看向镜头，身份保持，不锁死原表情",
        "templates": ["clothes"],
    },
    {
        "id": "clothes_daily",
        "title": "日常换装",
        "prompt": "换上参考服装的日常半身自拍，自然微笑，看向镜头",
        "templates": ["clothes"],
    },
    {
        "id": "i2i_refine",
        "title": "精修表情",
        "prompt": "以底图为主稍作精修：自然表情与光线，保持人物身份与构图",
        "templates": ["i2i"],
    },
    {
        "id": "i2i_light",
        "title": "改光线",
        "prompt": "保持主体与构图，优化光线与色调，更干净自然",
        "templates": ["i2i"],
    },
    {
        "id": "t2i_soft",
        "title": "柔和插画感",
        "prompt": "干净构图，柔和光线，主体清晰，细节完整",
        "templates": ["t2i", "blank"],
    },
    {
        "id": "window",
        "title": "窗边柔光",
        "prompt": "窗边柔和自然光，半身，轻松表情，干净背景",
        "templates": ["selfie", "clothes"],
    },
    {
        "id": "cafe",
        "title": "咖啡店",
        "prompt": "咖啡馆座位，暖色灯光，轻松日常",
        "templates": ["selfie", "duo"],
    },
    {
        "id": "look_you",
        "title": "日常他拍",
        "prompt": "朋友随手拍的日常半身照，自然看镜头，生活感",
        "templates": ["selfie"],
    },
    # Shared style presets (画布芯片 + 默认 /预设 名)
    {
        "id": "preset_bite_lip_glance",
        "title": "咬唇回眸",
        "prompt": (
            "成年女性坐在沙发扶手上，身体侧向一边，轻轻咬住下唇后回头看向镜头，"
            "一只手搭在膝上，另一只手拨开发丝，眼神带着试探和深情，紫色氛围灯映出柔和侧脸，"
            "真实人类女孩，竖屏近距离人像，暧昧生活感手机摄影"
        ),
        "templates": ["selfie", "duo", "i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_collar_touch",
        "title": "轻拉衣领",
        "prompt": (
            "成年女性站在镜前，指尖轻轻触到颈侧与衣领边缘，身体微微前倾，"
            "眼神透过镜面深情地停留在镜头上，发丝垂落肩前，夜晚卧室灯光柔暗，"
            "真实人类女孩，镜前半身自拍，iPhone原生暧昧氛围"
        ),
        "templates": ["selfie", "duo", "i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_lean_close",
        "title": "俯身靠近",
        "prompt": (
            "成年女性坐在书桌前，身体微微俯身靠近手机镜头，一只手撑在桌面，"
            "另一只手轻抚耳侧发丝，眼神从睫毛下方深情地望向镜头，"
            "台灯照亮脸部和颈部轮廓，真实人类女孩，竖屏近景，带有呼吸感的生活化抓拍"
        ),
        "templates": ["selfie", "duo", "i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_waist_touch",
        "title": "腰侧轻抚",
        "prompt": (
            "成年女性坐在床沿，一只手沿着腰侧轻轻抚过，身体向镜头方向侧转，"
            "肩膀自然放松，眼神安静而深情地看向镜头，暖黄色床头灯落在腰线和发丝上，"
            "真实人类女孩，竖屏三分之四身构图，私密感手机摄影"
        ),
        "templates": ["selfie", "duo", "i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_ear_close",
        "title": "耳边靠近",
        "prompt": (
            "成年女性坐在沙发上，成年伴侣只以靠近耳边的嘴角、下颌和一侧肩膀局部入镜，"
            "她侧过脸看向镜头，眼神迷离，带着深情与温柔，一只手轻轻抓住画面边缘伸来的衣袖，"
            "昏暗客厅里只有落地灯和窗外城市灯光，真实人类女孩，竖屏半身构图，"
            "iPhone随手拍出的亲密瞬间"
        ),
        "templates": ["selfie", "duo", "i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_hand_pull_close",
        "title": "牵手拉近",
        "prompt": (
            "成年女性站在房间门边，成年伴侣只以从画面外伸入的一只手和前臂出现，轻轻牵住她的手腕，"
            "她顺着力道向镜头靠近，另一只手扶住门框，眼神深情地直视镜头，暖色室内光线，"
            "真实人类女孩，竖屏中近景，暧昧抓拍质感"
        ),
        "templates": ["selfie", "duo", "i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_side_lift_gaze",
        "title": "侧躺抬眼",
        "prompt": (
            "成年女性侧躺在柔软沙发上，一只手支着脸颊，另一只手搭在腰侧，"
            "膝盖自然弯曲，脸颊贴近抱枕边缘，眼神从下方深情地抬起看向镜头，"
            "昏暗暖灯照亮凌乱发丝和柔软布料，真实人类女孩，竖屏近景，慵懒亲密的iPhone摄影"
        ),
        "templates": ["selfie", "duo", "i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_back_glance",
        "title": "背对回眸",
        "prompt": (
            "成年女性站在窗边背对镜头，身体轻轻转向一侧，回眸深情地望向镜头，"
            "长发沿着背部自然垂落，手指轻搭在窗帘边缘，清晨柔光勾勒出肩背和衣料轮廓，"
            "真实人类女孩，竖屏全身构图，安静自然的手机摄影质感"
        ),
        "templates": ["selfie", "duo", "i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_lowered_gaze",
        "title": "低头抬眼",
        "prompt": (
            "成年女性坐在昏暗房间的椅子上，双手轻轻整理衣袖，先低头看向自己的手指，"
            "随后抬眼望向镜头，眼神从克制变得深情而柔软，发丝遮住一侧脸颊，"
            "暖光勾勒出脸部和颈部轮廓，真实人类女孩，竖屏胸像构图，安静而暧昧的原生摄影质感"
        ),
        "templates": ["selfie", "duo", "i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_post_battle_exhaustion",
        "title": "战后力竭",
        "prompt": (
            "成年人物战胜后力竭，如沉眠般仰卧在一处浅滩上，全身出镜；双眸微睁却失焦无神，像睡去般静止。"
            "皮肤被浅水浸得苍白近乎透明，洁净完整，无伤口、无血迹；衣装湿透但覆盖得体，湿发与衣摆随水波轻散。"
            "冷月光与薄雾笼罩，凄美、唯美、死亡般静谧的凋零氛围。人物占画面90%，全身近距离肖像，低机位镜头，"
            "人物清晰突出，真实人体比例，长腿构图，主体靠近镜头，背景虚化，电影级人像摄影，9:16竖版。"
        ),
        "templates": ["selfie", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_submissive_shy_pose",
        "title": "臣服怯态",
        "prompt": (
            "真实人类女孩，成年女性，第一人称上位者视角。她跪坐并俯身向前，身体微微前倾，脸靠近镜头，形成亲密的近距离透视；"
            "双手自然扶在身体前方或膝边，不撑在镜头两侧，手臂不要伸入前景，手部略虚化、弱化处理，手指比例自然，避免过长、变形、多指或手掌畸形。"
            "使用轻微广角并尽量让全身入镜，人物整体比例协调，肩颈、腰线和发丝自然。她有桃花眼，含羞带怯，眼眶微红、泪光盈盈，轻咬下唇，眉头微蹙；"
            "眼神怯生生又欲拒还迎，发丝垂落并轻扫镜头边缘，锁骨与颈线若隐若现，腰肢纤细，神态楚楚可怜。氛围暧昧但克制，电影感近景，"
            "柔和光线，浅景深，脸部清晰，手部弱化。"
        ),
        "templates": ["selfie", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_hold_face",
        "title": "捧脸",
        "prompt": (
            "男生第一视角：女友累了，男生用一只手捧住她的脸颊，高颜值真实人类女孩，"
            "俯拍镜头且只能看到男友的手和手臂，女孩眼神朦胧却饱含爱意，头发凌乱，"
            "房间光线昏暗，iPhone随手抓拍的生活化原生质感"
        ),
        "templates": ["selfie", "duo", "i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_cover_face",
        "title": "遮脸",
        "prompt": (
            "仅使用一部普通手机从脸侧或下方自然遮住半张脸，至少保留一只眼睛和部分面部轮廓可见。"
            "手机必须由人物画面内原有的一只手自然握持，只占用这只既有手；另一只手保持原有动作或道具。"
            "若手机占用的手原本在做动作或手持道具，手机替代该动作或道具，原道具不入镜。"
            "人物始终恰好两条手臂、两只手和正常数量手指，不要为手机新增手、手臂、手掌、手指、镜中倒影手或重复肢体。"
            "若原始姿势或套装已明确占用双手、两只手都在持物或不适合举手机，优先保持原有双手、道具和姿势，"
            "不额外添加手机，也不强行遮脸。手机、手和脸的透视、光影与遮挡关系自然，不要手部变形、手臂穿过脸部、"
            "整张脸完全被盖住、手机贴脸或僵硬摆拍。保持人物身份、服装、姿势和场景不变，像自然随手拍。"
        ),
        "templates": ["selfie", "duo", "i2i", "clothes", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_to_real",
        "title": "变真人",
        "prompt": (
            "将参考图中的二次元/插画角色转换为真实人像照片，保留角色的年龄感、表情、发型、"
            "服装配色、气质和姿势，真实面部结构，自然皮肤纹理，真实摄影光影，电影级写实风格，高质量人像摄影"
        ),
        "templates": ["i2i", "clothes", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_to_anime",
        "title": "变动漫",
        "prompt": (
            "将参考图中的人物转换为高质量二次元动漫角色，保留身份辨识度、脸型、发型发色、服装配色、"
            "表情、姿势、场景和原构图；使用清晰线稿、细腻上色、统一的动漫五官与自然头身比例，"
            "让整张图保持一致的动漫画风，不要照片与动漫各占一半、不要失去人物特征、不要文字或水印"
        ),
        "templates": ["i2i", "clothes", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_to_catgirl",
        "title": "变猫娘",
        "prompt": (
            "将参考图中的人物转换为保留原身份特征的高质量二次元猫娘角色，保留脸型、发型发色、"
            "服装主色、表情、姿势和场景；加入一对与头部自然连接的猫耳、与姿势协调的猫尾和轻微猫科气质，"
            "耳朵与尾巴结构清楚、数量正确、不穿模不重复，不要动物脸、全身毛皮或多余肢体，"
            "统一线稿、上色和光影，保持完整构图，不要文字或水印"
        ),
        "templates": ["i2i", "clothes", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_to_chibi",
        "title": "变Q版",
        "prompt": (
            "将参考图中的人物转换为可爱但结构完整的Q版动漫角色，保留身份辨识度、发型发色、"
            "服装配色、标志性配饰、表情和动作；使用自然的大头身比例、清晰线稿、柔和上色和完整手脚，"
            "不要变成无五官的玩偶、不要丢失服装细节、不要文字或水印"
        ),
        "templates": ["i2i", "clothes", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_to_pixel",
        "title": "变像素",
        "prompt": (
            "将参考图转换为精致像素画风，保留人物身份、发型发色、服装配色、姿势、主要道具和场景布局；"
            "使用统一像素网格、清晰轮廓、有限但协调的色盘和有层次的明暗，不要模糊的低清压缩感、"
            "不要照片与像素风混杂、不要文字或水印"
        ),
        "templates": ["i2i", "clothes", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_jelly",
        "title": "果冻化",
        "prompt": "将第1张图片中的人物处理成果冻风效果，整体呈现Q弹果冻质感，色彩饱和度略高，表面有细微光泽感，原比例。",
        "templates": ["i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_realistic",
        "title": "真人化",
        "prompt": (
            "把参考图中的角色转化为真实人物照片风格，保留原角色的五官特征、发型、服装元素、气质和动作，"
            "真实皮肤质感，自然光线，电影感摄影，真实镜头景深，高细节，写实风格，避免夸张变形"
        ),
        "templates": ["i2i", "clothes", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_cos",
        "title": "变COS",
        "prompt": (
            "把参考图中的人物改造成高质量真人 COSPLAY 摄影风格，保留角色核心特征、发型、服装配色和标志性元素，"
            "精致妆容，真实布料材质，摄影棚灯光，动漫展写真感，高清细节，专业摄影"
        ),
        "templates": ["clothes", "i2i", "selfie", "blank"],
        "global": True,
    },
    {
        "id": "preset_manga_cover",
        "title": "漫画封面",
        "prompt": (
            "把画面改造成高质量漫画封面风格，保留主体特征，强烈构图，精致线稿，鲜明色彩，动态光影，"
            "干净背景，可加入装饰性标题排版但不要乱码文字，高质量插画"
        ),
        "templates": ["t2i", "i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_id_photo",
        "title": "证件照",
        "prompt": (
            "把参考图中的人物改造成真实标准证件照风格，正面视角，干净背景，均匀光线，自然表情，"
            "真实皮肤质感，清晰五官，正式衣着，高清真实摄影"
        ),
        "templates": ["i2i", "selfie", "blank"],
        "global": True,
    },
    {
        "id": "preset_bf_view",
        "title": "男友视角",
        "prompt": (
            "Girlfriend is drunk,  a beautiful 真人女孩,  In a room with purple ambient lighting, "
            "she sits on the bed. her eyes are hazy but full of love, Messy hair, ensure that the hair color "
            "of the girl in the picture remains unchanged. the room is dimly lit, she looked at the camera. "
            "amateurish iPhone shot. Depict the shadow effect in the picture correctly, adjust the shading "
            "of the glasses section to be appropriate"
        ),
        "templates": ["selfie", "duo", "i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_crop_waist",
        "title": "漏腰",
        "prompt": (
            "成年女性。唯一调整是将原有上衣的下摆缩短至露出自然腰线的长度，"
            "原有服装的款式、颜色、材质、领口、袖型、图案、配饰、层次和穿着方式全部保持不变；"
            "保持下装、人物身份、发型、脸部、表情、姿势、镜头和场景不变。"
            "居家休闲自拍，室内光线柔和偏暗，角度略高，放松地坐着或靠在深色沙发/床边，"
            "头发略显凌乱，像日常随意拍的照片。"
        ),
        "templates": ["selfie", "t2i", "i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_deep_open_front",
        "title": "深开襟",
        "prompt": (
            "成年女性。将上衣调整为明显的深开襟结构，左右前襟向两侧展开，从锁骨延伸至胸口下方；"
            "中央仅保留少量细窄装饰带、领结、金属挂件或链条连接，开襟区域保持清晰，不添加抹胸、"
            "内衬、胸衣或额外布料将其封闭。保持原服装配色、材质、袖型和角色饰品。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_underbust_cutout",
        "title": "下胸开窗",
        "prompt": (
            "成年女性。保持原上衣的领口和肩部结构，在胸口下缘至上腹部加入宽幅弧形开窗；"
            "开窗上下边缘分离明确，仅由两侧衣片和少量细带固定，露出自然的胸下缘与腰腹线条。"
            "不要补入肤色布、白色内衬、蕾丝胸衣或封闭面料，不要改变原服装的主要配色和装饰。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_side_bust_cutout",
        "title": "侧胸镂空",
        "prompt": (
            "成年女性。将上衣两侧调整为大面积侧向镂空结构，从腋下延伸至腰侧，正面主体衣片保持完整，"
            "背部以细带或窄幅布片连接；从斜侧面能够清楚看到侧胸与腰侧轮廓。镂空边缘裁剪整齐，"
            "不添加透明打底、内衣肩带或多余遮挡，保持原有领口、袖子与角色配饰。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_cross_strap_top",
        "title": "交叉绑带",
        "prompt": (
            "成年女性。将胸前主体改为多条细窄交叉绑带结构，绑带由颈部、胸侧和腰线上方固定，"
            "中央保留清晰的菱形或水滴形开窗；布料只覆盖必要区域，其余位置由对称绑带连接。"
            "绑带数量适中、受力关系自然，不要生成杂乱绳结、额外内衬或封闭胸衣。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_halter_backless",
        "title": "挂脖露背",
        "prompt": (
            "成年女性。将上衣调整为挂脖式露背结构，正面布料从胸前向上收拢并绕至颈后固定，肩部完全露出；"
            "背面从肩胛骨至腰部保持大面积开放，仅保留颈后系带和腰间固定带。不要增加交叉肩带、"
            "背心内衬或披肩遮住后背，保持原下装与配饰不变。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_double_waist_cutout",
        "title": "侧腰双开窗",
        "prompt": (
            "成年女性。保持胸前和下摆主体结构，在腰部左右两侧加入对称的大面积弧形开窗，"
            "从肋部延伸至髋骨上方；前后衣片仅通过少量金属环、细带或装饰扣连接。"
            "腰侧皮肤清晰可见，不使用肤色网纱填充，不缩小开窗，不改成普通收腰连衣裙。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_high_side_slit",
        "title": "极高侧开衩",
        "prompt": (
            "成年女性。保持原裙装的腰头、面料和图案，将一侧裙摆改为从大腿根部附近开始的极高侧开衩；"
            "前后裙片自然分开并沿腿侧垂落，行走或侧身时完整展示一侧腿部线条。开衩位置和布料连接必须合理，"
            "不增加安全裤外露，不把长裙缩短成普通迷你裙。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_sheer_layers",
        "title": "薄纱叠层",
        "prompt": (
            "成年女性。外层改为轻薄半透明纱质衣片，保留原有刺绣、滚边和装饰纹样，"
            "薄纱贴合身体后呈现清晰层次；内层只保留简洁且与原配色一致的必要结构，不使用厚重衬里遮住薄纱效果。"
            "纱料应具有真实透光、褶皱和叠层关系，不要变成塑料或完全不透明布料。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_open_side_apron",
        "title": "开放侧身",
        "prompt": (
            "成年女性。将服装调整为挂脖围裙式结构，正面保留一片完整的窄幅主体布料，腰部收束；"
            "身体两侧和后背大面积开放，仅通过颈后系带与腰后细带固定。侧面轮廓和腰背线条清晰，"
            "不添加普通连衣裙侧片、内搭背心或额外围裙层。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_open_jacket",
        "title": "敞怀外套",
        "prompt": (
            "成年女性。保留原角色外套的颜色、袖型、刺绣和肩部装饰，将前襟完全向两侧敞开；"
            "内层仅保留细带式或小面积结构，锁骨、胸口中央和腰腹形成连续开放区域。外套不得自动扣合，"
            "不增加高领、衬衫、抹胸或厚重内搭，衣襟随姿势自然垂落。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_split_lapel_ties",
        "title": "前襟分离系带",
        "prompt": (
            "成年女性。保持原有服装的颜色、面料、领口、袖型和角色配饰，将上衣前襟调整为左右分离的开放结构，"
            "从锁骨向下延伸至胸下位置；中央不使用完整布片遮挡，仅由颈后系带、胸下窄带和少量金属扣件固定。"
            "前襟边缘平整利落，不补入抹胸、内衣、肤色网纱或其他打底结构。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_asymmetric_diagonal_opening",
        "title": "单肩斜向开胸",
        "prompt": (
            "成年女性。保留原服装主体和一侧肩袖结构，将另一侧肩部至胸前调整为斜向开放设计；"
            "布料从一侧锁骨斜跨至对侧胸下或腰侧，形成明显的不对称开胸区域。露出的肩线与胸口轮廓清晰，"
            "连接处仅保留窄幅布带或装饰扣，不自动补成对称上衣、封闭领口或额外内搭。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_vertical_torso_opening",
        "title": "胸腹竖向开口",
        "prompt": (
            "成年女性。保持原有领口、袖子、腰线和装饰，将上衣中央调整为从胸骨下方延伸至肚脐上方的纵向开口；"
            "左右衣片保持完整，仅在开口边缘使用少量细链、窄绑带或金属环连接。开口连续且边缘整齐，"
            "不添加内衬、胸衣、背心或额外布料遮挡，不改变原服装的主色与材质。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_underbust_arc_cutout",
        "title": "下胸弧形开窗",
        "prompt": (
            "成年女性。保留原上衣的肩部、袖型和胸前主要装饰，在胸部下缘至上腹部之间加入宽幅弧形开窗；"
            "开窗沿胸下轮廓自然延伸，两侧由原服装同色细带或扣件固定。露出胸下与腰腹之间的皮肤，"
            "不使用透明布、肤色打底或其他方式缩小开窗面积，保持原服装的配色、材质和角色配饰。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_single_side_open_skirt",
        "title": "单侧全开裙摆",
        "prompt": (
            "成年女性。保留原裙装的腰头、面料、刺绣和整体长度，将一侧裙缝调整为从腰部延伸至大腿上部的完整开放结构；"
            "前后裙片只通过腰侧细带、金属环或少量装饰扣连接，站立或侧身时清楚展现单侧腿部线条。"
            "不补入安全裤、短裤、内衬或额外侧片，不把裙摆改成普通短裙。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_split_panel_wrap",
        "title": "前后分片围裹",
        "prompt": (
            "成年女性。将原服装调整为前后分片的围裹式结构，正面和背面保留主要布料与原有装饰，"
            "两侧腰部与髋部保持连续开放；前后衣片仅在肩部、颈后和腰后以细带、扣件或窄幅布条固定。"
            "不添加连体内搭、普通侧片或封闭式腰封，保持原服装的配色、材质和角色配饰。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_high_hip_cutout",
        "title": "高腰侧胯开窗",
        "prompt": (
            "成年女性。保持腰部以上所有服装、发型、脸部、表情、姿势、镜头和场景不变，只调整腰部以下服装。"
            "将下装改为高腰设计，左右髋部加入大面积侧向开窗，从腰侧延伸至大腿根部；前后主体布料保持完整，"
            "仅由腰带、细带或金属环连接。侧胯皮肤清晰可见，不使用肤色网纱、内衬或额外遮挡，保持原服装的主色和材质。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_front_crotch_opening",
        "title": "前裆竖向开口",
        "prompt": (
            "成年女性。只调整腰部以下服装，保留原上衣、人物身份、原表情和原动作。将下装前方改为从下腹部向下延伸的纵向开放结构，"
            "左右裤片或裙片沿开口两侧分离，仅由细窄腰带、装饰扣或少量绑带固定。开口边缘清晰整齐，"
            "不自动补入内裤、短裤、打底裤或肤色布料，保持原服装配色和面料质感。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_back_waist_cutout",
        "title": "高腰后腰开窗",
        "prompt": (
            "成年女性。保持腰部以上所有服装与姿势不变，只改变下装结构。将下装后腰改为大面积横向开窗，从左右腰侧延伸至臀部上方；"
            "后片仅保留窄幅腰带和两侧连接带，形成清晰的后腰与臀上线条。不要增加内衬、安全裤、连体衣或其他遮挡，"
            "开窗边缘自然贴合身体，服装受力和缝线合理。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_single_hip_cutout",
        "title": "单侧臀部镂空",
        "prompt": (
            "成年女性。只调整腰部以下的裙装或短裤。将一侧髋部至臀侧改为连续镂空结构，开放区域从腰侧延伸到大腿上部，"
            "另一侧保持原有布料和装饰，形成明显不对称设计。镂空处不添加透明网纱、肤色打底或额外侧片，"
            "保留原上衣、发型、面部、表情、动作、镜头和背景不变。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_split_crotch_skirt",
        "title": "前后分片开裆裙",
        "prompt": (
            "成年女性。保持原服装颜色、图案和材质，只替换腰部以下结构。将下装改为前后分离的开裆裙，前片与后片分别从腰部垂落，"
            "中央和两侧形成连续开放区域；裙片仅由腰带、细链、窄绑带或装饰扣固定。不要补入安全裤、内衬、短裤或普通完整裙片，"
            "不把结构改成普通短裙，保持原姿势自然呈现服装层次。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_side_slit_bondage_shorts",
        "title": "侧开衩绑带短裤",
        "prompt": (
            "成年女性。只改变下身服饰，保留上衣、脸部、发型、原表情和原动作。将下装改为高腰短裤，左右裤腿外侧从腰线至大腿根部设置连续高开衩，"
            "并用数条平行细绑带连接；裤片边缘整齐，绑带数量适中，皮肤和腿部轮廓清晰可见。"
            "不要添加裙片、丝袜、内衬或多余布料，保持原配色与面料。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_sheer_lower_layers",
        "title": "高腰透明薄纱下装",
        "prompt": (
            "成年女性。保持腰部以上服装和人物姿势不变，只调整下装。改为高腰半透明薄纱短裙或薄纱裤裙，外层纱料轻薄透光，"
            "腰部、臀部和腿部轮廓清晰可见；仅保留与原配色一致的极简必要遮挡结构，薄纱边缘、褶皱和叠层真实自然。"
            "不要厚重衬里、普通长裙、内搭安全裤或塑料质感。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_low_waist_chainwear",
        "title": "低腰链带下装",
        "prompt": (
            "成年女性。只替换腰部以下服饰。将下装改为低腰窄版短裙或低腰短裤，腰线落在胯骨附近，前后主体布料由细金属链、窄皮带和少量装饰扣连接，"
            "腰侧与髋部保持开放。链条数量清晰可数，连接关系合理，不自动补高腰布料、内裤、打底裤或侧片。"
            "上衣、脸部、表情、动作和场景全部保持不变。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_back_hip_cutout_skirt",
        "title": "臀后镂空短裙",
        "prompt": (
            "成年女性。保持原上衣、人物身份、头部角度、表情、姿势、镜头和场景不变，只修改下装。将短裙后片调整为中央大面积镂空，"
            "露出后腰至臀部上方轮廓，左右裙片通过窄腰带、细链或小型金属扣连接。镂空边缘平整，裙摆仍保持原长度，"
            "不添加内衬、短裤或完整后片遮挡。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_double_high_slit_skirt",
        "title": "双侧高开衩长裙",
        "prompt": (
            "成年女性。只调整腰部以下服装，保留原上衣和原动作。将原裙装改为双侧高开衩结构，两侧裙缝从腰部或髋部一直开放至大腿根部附近，"
            "前后裙片自然垂落，仅由腰头和少量装饰带固定。两侧腿部线条清晰可见，不增加安全裤、内衬或额外侧片，"
            "不把长裙缩短成普通迷你裙。"
        ),
        "templates": ["selfie", "clothes", "i2i", "t2i", "blank"],
        "global": True,
    },
]

# These special presets can also be requested through dynamic aliases.
# Keep the aliases themselves out of the fixed preset seed so each one expands
# to one concrete prompt before image generation.
SPECIAL_PRESET_ALIAS = "特殊预设"
ACTION_PRESET_ALIAS = "动作预设"
UPPER_PRESET_ALIAS = "上身预设"
LOWER_PRESET_ALIAS = "下身预设"
SPECIAL_PRESET_GROUP_LABELS = {
    "action": "动作预设",
    "upper": "上身预设",
    "lower": "下身预设",
}
SPECIAL_PRESET_GROUP_ORDER = ("action", "upper", "lower")
ACTION_PROMPT_PRESET_IDS = frozenset(
    {
        "preset_bite_lip_glance",
        "preset_collar_touch",
        "preset_lean_close",
        "preset_waist_touch",
        "preset_ear_close",
        "preset_hand_pull_close",
        "preset_side_lift_gaze",
        "preset_back_glance",
        "preset_lowered_gaze",
        "preset_post_battle_exhaustion",
        "preset_submissive_shy_pose",
        "preset_hold_face",
        "preset_bf_view",
    }
)
UPPER_PROMPT_PRESET_IDS = frozenset(
    {
        "preset_crop_waist",
        "preset_deep_open_front",
        "preset_underbust_cutout",
        "preset_side_bust_cutout",
        "preset_cross_strap_top",
        "preset_halter_backless",
        "preset_double_waist_cutout",
        "preset_high_side_slit",
        "preset_sheer_layers",
        "preset_open_side_apron",
        "preset_open_jacket",
        "preset_split_lapel_ties",
        "preset_asymmetric_diagonal_opening",
        "preset_vertical_torso_opening",
        "preset_underbust_arc_cutout",
        "preset_single_side_open_skirt",
        "preset_split_panel_wrap",
    }
)
LOWER_PROMPT_PRESET_IDS = frozenset(
    {
        "preset_high_hip_cutout",
        "preset_front_crotch_opening",
        "preset_back_waist_cutout",
        "preset_single_hip_cutout",
        "preset_split_crotch_skirt",
        "preset_side_slit_bondage_shorts",
        "preset_sheer_lower_layers",
        "preset_low_waist_chainwear",
        "preset_back_hip_cutout_skirt",
        "preset_double_high_slit_skirt",
    }
)
# "特殊预设" is the combined pool; the dedicated aliases expose action,
# upper-body, and lower-body groups independently.
SPECIAL_PROMPT_PRESET_IDS = ACTION_PROMPT_PRESET_IDS | UPPER_PROMPT_PRESET_IDS | LOWER_PROMPT_PRESET_IDS
ALL_SPECIAL_PROMPT_PRESET_IDS = SPECIAL_PROMPT_PRESET_IDS


def _prompt_presets_by_ids(ids: frozenset[str]) -> List[Dict[str, Any]]:
    return [
        item
        for item in BUILTIN_PROMPTS
        if str(item.get("id") or "").strip() in ids
        and str(item.get("title") or "").strip()
        and str(item.get("prompt") or "").strip()
    ]


def special_prompt_presets() -> List[Dict[str, Any]]:
    """Concrete built-in prompts eligible for the dynamic special-preset alias."""
    return _prompt_presets_by_ids(SPECIAL_PROMPT_PRESET_IDS)


def action_prompt_presets() -> List[Dict[str, Any]]:
    """Concrete built-in prompts for the action-preset alias and special group."""
    return _prompt_presets_by_ids(ACTION_PROMPT_PRESET_IDS)


def upper_prompt_presets() -> List[Dict[str, Any]]:
    """Concrete upper-body clothing-structure prompts."""
    return _prompt_presets_by_ids(UPPER_PROMPT_PRESET_IDS)


def lower_prompt_presets() -> List[Dict[str, Any]]:
    """Concrete lower-body clothing-structure prompts."""
    return _prompt_presets_by_ids(LOWER_PROMPT_PRESET_IDS)


def prompt_preset_group(value: Any) -> str:
    """Return the special-preset group for a built-in or persisted preset."""
    if isinstance(value, dict):
        preset_id = str(value.get("id") or "").strip()
        name = str(value.get("name") or value.get("title") or "").strip()
    else:
        preset_id = ""
        name = str(value or "").strip()
    if preset_id in ACTION_PROMPT_PRESET_IDS:
        return "action"
    if preset_id in UPPER_PROMPT_PRESET_IDS:
        return "upper"
    if preset_id in LOWER_PROMPT_PRESET_IDS:
        return "lower"
    if name:
        for item in BUILTIN_PROMPTS:
            if str(item.get("title") or "").strip() != name:
                continue
            item_id = str(item.get("id") or "").strip()
            if item_id in ACTION_PROMPT_PRESET_IDS:
                return "action"
            if item_id in UPPER_PROMPT_PRESET_IDS:
                return "upper"
            if item_id in LOWER_PROMPT_PRESET_IDS:
                return "lower"
    return ""


def prompt_preset_group_label(value: Any) -> str:
    """Return the human-readable label for a special-preset group."""
    group = prompt_preset_group(value)
    return SPECIAL_PRESET_GROUP_LABELS.get(group, "")


def prompt_preset_display_sort_key(value: Any) -> tuple[int, int, str]:
    """Sort ordinary presets first, then special groups in a stable order."""
    if isinstance(value, dict):
        name = str(value.get("name") or value.get("title") or "").strip()
        group = str(value.get("preset_group") or value.get("special_group") or "").strip()
        if not group:
            group = prompt_preset_group(value)
    else:
        name = str(value or "").strip()
        group = prompt_preset_group(name)
    try:
        group_index = SPECIAL_PRESET_GROUP_ORDER.index(group)
    except ValueError:
        group_index = len(SPECIAL_PRESET_GROUP_ORDER)
    return (1 if group else 0, group_index if group else -1, name)


def dynamic_prompt_preset_groups() -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Aliases and their random prompt pools, longest aliases first at call sites."""
    return [
        (SPECIAL_PRESET_ALIAS, special_prompt_presets()),
        (ACTION_PRESET_ALIAS, action_prompt_presets()),
        (UPPER_PRESET_ALIAS, upper_prompt_presets()),
        (LOWER_PRESET_ALIAS, lower_prompt_presets()),
    ]


def special_prompt_preset_titles() -> frozenset[str]:
    """Titles belonging to the action and clothing-structure special pools."""
    return frozenset(
        str(item.get("title") or "").strip()
        for _alias, items in dynamic_prompt_preset_groups()
        for item in items
        if str(item.get("title") or "").strip()
    )

# P0 templates: id -> layout defaults
STUDIO_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "duo": {
        "id": "duo",
        "title": "双人合影",
        "description": "形象 + 1 同框，最常用",
        "default_title": "双人合影",
        "mode": "group",
        "aspect_ratio": "3:4",
        "resolution": "1K",
        "prompt": "两人自然并肩合影，轻松微笑，看向镜头，日常暖光",
        "use_persona_identity": True,
        "slots": [
            {"role": "identity", "label": "形象（自己）"},
            {"role": "peer", "label": "同框对象"},
            {"role": "scene", "label": "场景/道具（可选）"},
        ],
    },
    "group": {
        "id": "group",
        "title": "多人合影",
        "description": "形象 + 同框×3 + 场景",
        "default_title": "多人合影",
        "mode": "group",
        "aspect_ratio": "3:4",
        "resolution": "1K",
        "prompt": "自然并肩合影，轻松微笑，看向镜头，日常暖光",
        "use_persona_identity": True,
        "slots": [
            {"role": "identity", "label": "形象（自己）"},
            {"role": "peer", "label": "同框对象 1"},
            {"role": "peer", "label": "同框对象 2"},
            {"role": "peer", "label": "同框对象 3"},
            {"role": "scene", "label": "场景/道具（可选）"},
        ],
    },
    "selfie": {
        "id": "selfie",
        "title": "自拍 / 看看 / 看看腿",
        "description": "看看腿生成日常下装穿搭记录：腰部以下近景，上半身不入镜",
        "default_title": "自拍画布",
        "mode": "selfie",
        "aspect_ratio": "3:4",
        "resolution": "1K",
        "prompt": "看着镜头自然自拍，半身，柔和光线，轻松表情",
        "use_persona_identity": True,
        "slots": [
            {"role": "identity", "label": "形象（自己）"},
            {"role": "outfit", "label": "服装参考（可选）"},
            {"role": "pose", "label": "姿势/构图（可选）"},
        ],
    },
    "clothes": {
        "id": "clothes",
        "title": "换装 / COS",
        "description": "形象 + 服装主参考 + 配饰/场景",
        "default_title": "换装画布",
        "mode": "selfie",
        "aspect_ratio": "3:4",
        "resolution": "1K",
        "prompt": "穿着参考图服装自拍，表情自然，看向镜头，身份保持，不锁死原表情",
        "use_persona_identity": True,
        "slots": [
            {"role": "identity", "label": "形象（自己）"},
            {"role": "outfit", "label": "服装主参考"},
            {"role": "extra", "label": "配饰/材质（可选）"},
            {"role": "scene", "label": "场景（可选）"},
        ],
    },
    "i2i": {
        "id": "i2i",
        "title": "图生图精修",
        "description": "底图为主，可选风格/细节",
        "default_title": "图生图",
        "mode": "i2i",
        "aspect_ratio": "自动",
        "resolution": "1K",
        "prompt": "以底图为主稍作精修：自然表情与光线，保持人物身份与构图",
        "use_persona_identity": False,
        "slots": [
            {"role": "base", "label": "底图（主）"},
            {"role": "style", "label": "风格参考（可选）"},
            {"role": "detail", "label": "细节参考（可选）"},
        ],
    },
    "t2i": {
        "id": "t2i",
        "title": "文生图",
        "description": "纯文案，可选 1 张风格参考",
        "default_title": "文生图",
        "mode": "t2i",
        "aspect_ratio": "自动",
        "resolution": "1K",
        "prompt": "干净构图，柔和光线，主体清晰，细节完整",
        "use_persona_identity": False,
        "slots": [
            {"role": "style", "label": "风格参考（可选）"},
        ],
    },
    "blank": {
        "id": "blank",
        "title": "空白",
        "description": "无预设槽位，自行添加",
        "default_title": "空白画布",
        "mode": "t2i",
        "aspect_ratio": "自动",
        "resolution": "1K",
        "prompt": "",
        "use_persona_identity": False,
        "slots": [],
    },
}

# Stable order for UI select
STUDIO_TEMPLATE_ORDER = ["duo", "group", "selfie", "clothes", "i2i", "t2i", "blank"]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def normalize_template_id(template: str = "", *, use_group_template: Optional[bool] = None) -> str:
    text = str(template or "").strip().lower()
    if text in STUDIO_TEMPLATES:
        return text
    # legacy flag
    if use_group_template is False:
        return "blank"
    if use_group_template is True or text in {"", "default", "true", "1"}:
        # Prefer duo as the everyday default going forward
        return "duo"
    return "duo"


def list_studio_templates() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for key in STUDIO_TEMPLATE_ORDER:
        meta = STUDIO_TEMPLATES.get(key) or {}
        out.append(
            {
                "id": meta.get("id") or key,
                "title": meta.get("title") or key,
                "description": meta.get("description") or "",
                "mode": meta.get("mode") or "t2i",
                "aspect_ratio": meta.get("aspect_ratio") or "自动",
                "slot_count": len(meta.get("slots") or []),
            }
        )
    return out


def prompts_for_template(template_id: str) -> List[Dict[str, Any]]:
    """Chips for one template only — do not leak other templates' prompts."""
    tid = normalize_template_id(template_id)
    out: List[Dict[str, Any]] = []
    for item in BUILTIN_PROMPTS:
        tags = [str(x).strip() for x in (item.get("templates") or []) if str(x).strip()]
        if tags and tid not in tags:
            continue
        out.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "prompt": item.get("prompt"),
                "templates": tags,
                "global": bool(item.get("global")),
                "preset_group": prompt_preset_group(item),
                "special_group": prompt_preset_group(item),
            }
        )
    out.sort(key=prompt_preset_display_sort_key)
    return out


def global_prompt_presets() -> List[Dict[str, Any]]:
    """Builtin entries shown in the shared 预设 picker (画布/试画)."""
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in BUILTIN_PROMPTS:
        title = str(item.get("title") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if not title or not prompt:
            continue
        if not item.get("global") and item.get("templates"):
            # template-only chips stay out of global picker unless marked global
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "id": item.get("id") or title,
                "name": title,
                "title": title,
                "prompt": prompt,
                "source": "builtin",
                "templates": list(item.get("templates") or []),
                "preset_group": prompt_preset_group(item),
                "special_group": prompt_preset_group(item),
            }
        )
    out.sort(key=prompt_preset_display_sort_key)
    return out


def default_image_preset_seed() -> Dict[str, Dict[str, str]]:
    """Name -> preset dict for ImagePresetManager seed (QQ /预设 + Web)."""
    seed: Dict[str, Dict[str, str]] = {}
    for item in BUILTIN_PROMPTS:
        if not item.get("global"):
            continue
        title = str(item.get("title") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if title and prompt:
            seed[title] = {"prompt": prompt, "description": title}
    return seed


def _hip_sway_video_preset(name: str, movement: str, steps: List[str]) -> Dict[str, Any]:
    """Build one movement-specific 10-beat preset with shared media constraints."""
    if len(steps) != 10:
        raise ValueError(f"{name} must contain 10 dance steps")
    action_text = "动作必须按以下顺序完整执行，不循环、不跳过：" + "；".join(
        f"{index}，{step}" for index, step in enumerate(steps, start=1)
    ) + "。"
    prompt = (
        f"生成10秒9:16竖屏高清真人舞蹈视频，带原生同步音轨；人物从第0秒开始跟拍舞动，始终居中，"
        f"镜头随律动轻微晃动但保持稳定，不突然推拉、不切镜；长发随肢体惯性自然飘动，始终不遮挡脸部。"
        "严格继承输入生图/首帧中的人物身份、脸部、发型、体型、服饰和场景，不换装。"
        f"整体是活泼、卡点精准的{movement}风格，动作俏皮有力。{action_text}"
        "所有动作必须卡在DJ电子鼓点的重拍上，卡点精准，节奏明快。"
        "音轨必须从第0秒开始并与动作同步：BGM使用DJ电子鼓点版，加入短促人声采样片段但不得出现对白；"
        "禁止古风乐器音效，不要生成琵琶、古筝或其他古风乐器音效。必须输出可听见的BGM，禁止静音、禁止口型说话。"
        "自然光影、真实肤质、动态流畅；避免动作延迟、机械循环、手指粘连、手腕反折、肢体穿模、额外手臂、"
        "手指畸形、头发遮脸、服装漂移、背景闪烁和镜头剧烈抖动。"
    )
    return {
        "prompt": prompt,
        "description": f"{name}10秒活泼卡点扭胯舞，动作清晰，带同步BGM",
        "duration": 10,
    }


def default_video_preset_seed() -> Dict[str, Dict[str, Any]]:
    """Name -> preset dict for video command and Web management defaults."""
    return {
        "动作转场": {
            "prompt": (
                "生成8秒9:16竖屏、带原生同步音轨的高清真人动作转场视频；严格继承输入生图/首帧中的人物身份、脸部、"
                "发型、体型、服饰和场景，不换装。人物始终位于画面中央，动作清晰完整，按四段顺序切换，每段约2秒，"
                "切换干净利落，不能跳帧、残影、动作叠加或提前结束。"
                "动作1：人物保持跪姿，身体微微前倾，一只手比耶，另一只手自然放在身侧，头微微歪向镜头，保持短暂定格；"
                "动作2：切到下一个动作，双手抬到脸颊两侧，手指轻轻弯曲，肩膀小幅左右晃动，表情自然；"
                "动作3：切到下一个动作，一只手向前伸出，另一只手放在腰侧，身体轻微侧转，动作停留一拍；"
                "动作4：切到下一个动作，双手自然垂下，身体微微后仰，看向镜头，短暂定格收尾。"
                "镜头固定平视中景，人物全程居中且不出画，四段动作都要看清手部、跪姿和身体变化；自然光影，真实肤质，"
                "动作衔接明快，头发和衣物只产生自然惯性。音轨从第0秒开始，使用轻快有节奏感的电子鼓点舞曲，"
                "重拍与每次动作切换同步，必须输出可听见的BGM，禁止静音、对白、唱歌和口型说话。避免站桩、动作延迟、"
                "随机挥手、手指粘连、额外手臂、肢体穿模、姿势崩坏、服装漂移、背景闪烁和镜头抖动。"
            ),
            "description": "跪姿四段动作切换，手势变化与定格收尾",
            "duration": 8,
        },
        "动作转场2": {
            "prompt": (
                "生成6秒9:16竖屏、带原生同步音轨的高清真人动作转场视频；以输入原图中的女性人物为主体，严格保持原图的"
                "脸部身份、发型、体型、服饰和场景，不换装。视频采用分段式卡点结构，三个动作按顺序呈现，每段约一至两秒，"
                "每次动作变化之间插入短暂黑屏切镜，切换节奏清楚，不要溶解、残影、动作叠加或跳帧。"
                "第一段：人物侧身面对镜头，身体随鼓点左右摆胯，动作自然利落；"
                "黑屏切镜后进入第二段：人物正面面对镜头，连续扭胯，肩部和上身随节奏轻微摆动；"
                "再次黑屏切镜后进入第三段：人物背对镜头，背面摆臀，身体跟随节拍左右律动，保持动作连贯自然。"
                "镜头全程固定平视中景，人物位于画面中央，三段动作构图稳定且身体不出画，确保侧身、正面和背面姿态清晰可见。"
                "自然光影，真实肤质，高清真人质感，动作卡点准确，头发和衣物产生符合惯性的轻微摆动。"
                "音轨从第0秒开始，使用明快有鼓点的卡点电子舞曲，黑屏切镜与重拍同步，必须输出可听见的BGM，"
                "禁止静音、对白和口型说话。避免站桩、动作延迟、镜头晃动、肢体穿模、额外手臂、姿势崩坏、服装漂移和背景闪烁。"
            ),
            "description": "侧身、正面、背面三段摆胯摆臀黑屏卡点转场",
            "duration": 6,
        },
        "小半": {
            "prompt": (
                "生成12秒9:16竖屏、带声音的完整卡点舞蹈视频；运动和节拍优先，人物从第0秒就开始动作，"
                "不要静止展示服装。严格继承输入生图/首帧中的人物身份、脸部、发型、体型、服饰和场景，不换装。"
                "整体是冷感、干脆、有力量的‘小半’风格，眼神始终冷盯镜头。动作按清晰4/4鼓点连续完成："
                "第1段（0-2秒）：膝盖微弯，左右点胯2次；双手在胸前交叉后立刻向两侧推开。"
                "第2段（2-4秒）：右手向前指向镜头，同时点胯2下，指尖和胯部在重拍同时到位。"
                "第3段（4-7秒）：连续左右点胯4次，双手在胸前做推拉，配合同步顶胸2次；"
                "随后双臂向后夹，身体微微下沉再弹起，重心清楚，不能拖拍。"
                "第4段（7-9.5秒）：右手擦过嘴角并顶胯，左手向前推并点胯；双手向下压，快速点胯2下。"
                "第5段（9.5-12秒）：左右摆胯，双手划出连续大圈，最后猛地定住；定格时单手抱胸或插兜并顶胯，"
                "保持‘眼神杀’收尾。动作短促、干净、有重量，手臂、胸口、胯部和膝盖协调，头发与衣摆只产生自然惯性。"
                "镜头全程稳定的平视中景单镜头，人物保持在画面中央，动作完整可见，无剪辑、无突然变焦、无运镜干扰。"
                "音轨必须从第0秒开始并与动作同步：使用网上流行的‘baby, i am worth it’作为BGM，"
                "有明显鼓点、低音和拍手节奏；若无法调用原曲，生成同样冷感、有弹性、重拍清晰的替代电子舞曲。"
                "必须输出可听见的BGM，禁止静音、禁止人声对白、禁止口型说话。避免站桩、动作延迟、随机挥手、"
                "左右方向错乱、肢体穿模、额外手臂、手指畸形、服装漂移、背景闪烁。"
            ),
            "description": "小半风格冷感卡点舞，点胯推拉与眼神杀收尾，带同步BGM",
            "duration": 12,
        },
        "嘉桐摇": {
            "prompt": (
                "生成10秒9:16竖屏、带声音的《嘉桐摇》慵懒卡点舞蹈视频；人物从第0秒开始连续运动，"
                "禁止站着不动或只展示服装。严格继承输入生图/首帧中的人物身份、脸部、发型、体型、服饰和场景，不换装。"
                "舞蹈BPM为115，整体慵懒、丝滑、松弛但节拍明确，动作之间用自然重心转移连接，不要突然抽动。"
                "动作时间轴：0-2秒，身体放松、膝盖微屈，左右摆胯；2-3.5秒，双手抬到脸侧，"
                "掌心贴近脸颊，随节拍做轻柔的手腕转动；3.5-5秒，双手回到胸前做小幅左右轻摆，胸口跟随摆动。"
                "5-6.5秒，小幅度抖肩并绕臂，肩、肘、腕连成圆润轨迹；6.5-8秒，双手做可爱的猫爪姿势，"
                "手腕向外扣出两次，保持慵懒节拍；8-9秒，左右交替撞肩，身体和胯部顺势回弹；"
                "9-10秒，一手点脸、一手点胯，完成最后一个重拍并自然定格。"
                "全程动作幅度适中、连贯丝滑，手指数量正常，头发和衣物随动作轻微摆动。自然光影、真实肤质、"
                "画面自然，固定平视中景单镜头，人物居中且全程不出画，无剪辑、无突然变焦、无夸张运镜。"
                "音轨必须从第0秒开始：使用《嘉桐摇》或同名可用音轨，115 BPM，清晰底鼓、轻快低音和柔和节拍，"
                "所有摆胯、转腕、抖肩、撞肩和点脸点胯动作必须卡在鼓点上。必须输出可听见的BGM，禁止静音、"
                "禁止人声对白和口型说话。避免动作延迟、机械循环、手部粘脸、猫爪变形、肢体穿模、额外手臂、"
                "手指畸形、服装漂移和背景闪烁。"
            ),
            "description": "《嘉桐摇》115 BPM慵懒丝滑卡点舞，真实自然光影，带同步BGM",
            "duration": 10,
        },
        "兰花指卡点舞": {
            "prompt": (
                "生成12秒9:16竖屏、带原生同步音轨的单人舞蹈视频；人物从第0秒立即开始运动，禁止静止展示服装。"
                "严格继承输入生图/首帧中的人物身份、脸部、发型、体型、服饰和场景，不换装。动作必须严格按1至19的原顺序逐拍完成，"
                "对应5个八拍（共40拍），每个动作自然接下一个，不循环、不跳过、不提前结束。除双手锤动作短暂半握拳外，"
                "手型全程保持兰花指；交叉和绕圈时手腕利落不拖沓，胯部只做小幅顶胯或坐胯，手到胯到，不甩大胯。"
                "动作细化：1，右手从左侧横向甩到右侧，手心朝下，腕部发力，动作短促；"
                "2，右手沿头顶绕到身后，再划回胸前，完整画一圈；"
                "3，右手做兰花指指向斜上方，眼神同步看向斜上方；"
                "4，双手半握拳位于胸口两侧，向内快速捶4次，每次配合小含胸；"
                "5，右手胸前交叉后沿肩侧绕一圈回正；6，右手轻敲右侧太阳穴，头微侧；"
                "7，双手从胸前往下、沿腰侧推绕，连续完成3次波浪手；"
                "8，双手兰花指同时指向斜前方，胯部轻顶；"
                "9，双手快速上下抖动，模拟哗啦啦下雨节奏，胯部小幅摆动；"
                "10，单手指向斜下方，同时完成一次小蹲起；"
                "11，双手交替拨弦，模拟弹琵琶或古筝，胯部随节奏轻晃；"
                "12，双手快速交替点指，节奏加快；"
                "13，双手做小五花绕转，手腕灵活发力；"
                "14，重复波浪手3次，幅度比第7项稍大；"
                "15，延续波浪手，放慢速度做5次，线条柔美；"
                "16，双手手腕顺时针转3圈，掌心向外翻；"
                "17，双手向左右交替推送5次，每次推送与送胯同步；"
                "18，重复小五花绕转，完成后定住1拍；"
                "19，右手握拳轻敲胯侧，最后收尾定格。"
                "八拍卡拍分配必须执行：第1个八拍为1甩手、2绕圈、3斜上指、4-7双手锤4次、8收拳回胸；"
                "第2个八拍为1右手交叉绕圈、2敲头、3-5波浪3次、6双手斜前指、7哗啦啦抖手、8斜下指；"
                "第3个八拍为1弹琴、2快速点指、3-4转花手、5-7波浪3次、8预备调整站姿；"
                "第4个八拍为1-5波浪5次（速度放缓更柔美）、6-8双手手腕顺时针转3圈；"
                "第5个八拍为1-5双手左右交替推送并同步送胯、6-7转花手、8右手敲胯侧并定格。"
                "整体风格顺接流畅、手部细节清楚、动作干脆又有柔美变化，眼神甜媚并随指尖转移，最后定格看向镜头。"
                "镜头全程固定平视中景单镜头，人物居中且动作完整可见，无剪辑、无突然变焦、无夸张运镜。"
                "音轨必须从第0秒开始并与动作同步：生成适配DJ版的节奏型舞曲，鼓点明快清晰、低音有弹性，约110至120 BPM；"
                "甩手、捶胸、波浪、抖手、转腕和点指必须逐拍卡准。必须输出可听见的BGM，禁止静音、禁止人声对白、禁止口型说话。"
                "避免动作延迟、机械循环、手指粘连、手腕反折、肢体穿模、额外手臂、手指畸形、服装漂移和背景闪烁。"
            ),
            "description": "兰花指原顺序逐拍卡点舞，19个动作、5个八拍，带同步BGM",
            "duration": 12,
        },
        "复仇摇": {
            "prompt": (
                "生成12秒9:16竖屏、带原生同步音轨的高清真人舞蹈视频；人物从第0秒开始跟拍舞动，人物始终居中，禁止静止展示服装。"
                "严格继承输入生图/首帧中的人物身份、脸部、发型、体型、服饰和场景，不换装。镜头随律动轻微晃动但保持稳定，"
                "不突然推拉、不切镜；长发随肢体惯性自然飘动，始终不遮挡脸部。整体是清冷、慵懒、带复仇氛围感的卡点舞，动作精准有力。"
                "动作必须按以下顺序完整执行，不循环、不跳过：1，右手从左向右横向交叉甩，手心朝下，腕部利落发力；"
                "2，右手沿头顶绕一圈到身后再划回胸前；3，右手保持兰花指指向斜上方，眼神同步看向斜上方；"
                "4，双手在胸口两侧快速向内捶4次，配合小含胸；5，右手胸前交叉后沿肩侧绕一圈回正；"
                "6，右手轻敲右侧太阳穴，头部微侧；7，双手从胸前向下沿腰侧推绕，连续做3次波浪手；"
                "8，双手兰花指同时指向斜前方，胯部轻顶；9，双手快速上下抖动模拟哗啦啦节奏，胯部小幅摆动；"
                "10，单手指向斜下方并完成一次小蹲起；11，双手交替拨弦，模拟弹琵琶或古筝的手部动作，胯部轻晃；"
                "12，双手快速交替点指；13，双手做小五花绕转；14，双手手腕顺时针转3圈并掌心外翻；"
                "15，双手向左右交替推送5次，每次推送与送胯同步；16，重复小五花绕转，右手握拳轻敲胯侧后收尾定格。"
                "除捶胸和敲胯的短暂收指动作外，手型尽量保持兰花指；胯部只做小幅顶胯或坐胯，手到胯到，不甩大胯。"
                "音轨必须从第0秒开始并与动作同步：BGM使用有明显重拍的DJ电子鼓点舞曲，加入短促人声采样片段但不得出现对白；"
                "禁止古风乐器独奏，弹琵琶仅表现手部拨弦手势，不要生成琵琶、古筝或其他古风乐器音效。所有甩手、绕圈、捶胸、波浪、"
                "抖手、点指、转腕、送胯和敲胯必须卡在鼓点上。必须输出可听见的BGM，禁止静音、禁止口型说话。"
                "自然光影、真实肤质、动态流畅；避免动作延迟、机械循环、手指粘连、手腕反折、肢体穿模、额外手臂、手指畸形、"
                "头发遮脸、服装漂移、背景闪烁和镜头剧烈抖动。"
            ),
            "description": "复仇摇清冷慵懒跟拍卡点舞，DJ人声鼓点，琵琶仅手势无音效",
            "duration": 12,
        },
        "复仇摇2": {
            "prompt": (
                "生成12秒9:16竖屏、带原生同步音轨的高清真人舞蹈视频；镜头全程跟随女孩跳‘复仇摇’，人物始终居中，"
                "镜头仅随身体律动做轻微左右跟拍和晃动，保持平滑稳定，不突然推拉、不切镜。人物从第0秒开始运动，禁止静止展示服装。"
                "严格继承输入生图/首帧中的人物身份、脸部、发型、体型、服饰和场景，不换装。长发随转头、甩腕、摆胯自然飘动，"
                "发丝具有真实惯性但全程不遮脸。舞蹈风格清冷、慵懒、带复仇感，动作克制柔媚，节奏适配DJ版快节奏，卡点清晰。"
                "动作按顺序完整执行并自然顺接：1，右手从左向右横向交叉甩，腕部发力；2，右手沿头顶绕一整圈；"
                "3，右手兰花指斜上指，眼神跟随指尖；4，双手在胸口两侧快速向内捶，配合小含胸；"
                "5，右手胸前交叉后沿肩侧绕圈回正；6，右手轻敲太阳穴，头部微侧；7，双手连续做波浪手；"
                "8，双手兰花指指向斜前方，胯部轻顶；9，双手快速上下抖动，胯部小幅律动；"
                "10，单手斜下指并配合一次小蹲起；11，双手交替拨弦，模拟弹琵琶的手势，胯部轻晃；"
                "12，双手快速点指；13，双手小五花绕转；14，双手手腕顺时针转动并掌心外翻；"
                "15，双手左右交替推送，配合左右交替送胯；16，右手握拳轻敲胯侧，最后定格。"
                "除短暂捶胸和敲胯收指外，手型保持兰花指；胯部只做小幅顶胯或坐胯，手到胯到，不甩大胯。"
                "镜头跟拍必须服务于动作，不能遮挡手部细节；动作干净、丝滑、连贯，肢体比例和手指保持正常。"
                "音轨必须从第0秒开始并与动作同步：使用节奏明快的DJ电子鼓点舞曲，重拍清晰、低音有弹性，可加入短促人声采样但不得出现对白；"
                "弹琵琶仅表现手部拨弦手势，不生成琵琶、古筝或其他古风乐器音效。必须输出可听见的BGM，禁止静音、禁止口型说话。"
                "避免动作延迟、机械循环、手指粘连、手腕反折、肢体穿模、额外手臂、手指畸形、头发遮脸、服装漂移、"
                "背景闪烁和镜头剧烈抖动。"
            ),
            "description": "复仇摇2清冷慵懒轻跟拍舞，长发惯性不遮脸，DJ鼓点卡拍",
            "duration": 12,
        },
        "鱼块摇": {
            "prompt": (
                "生成12秒9:16竖屏高清真人舞蹈视频，带原生同步音轨；人物从第0秒开始跟拍舞动，始终居中，禁止静止展示服装。"
                "严格继承输入首帧中的人物身份、脸部、发型、体型、服饰和场景，不换装。镜头随律动轻微晃动但保持稳定，"
                "不突然推拉、不切镜；长发随肢体惯性自然飘动，始终不遮挡脸部。整体是魔性、带感、卡点精准的鱼块摇风格，动作干脆利落。"
                "动作必须按以下顺序完整执行，不循环、不跳过：1，右手放胯部，坐胯；"
                "2，双手交替点点点，同时双手绕腕扭胯；3，双手打开拉回，右手轻敲；"
                "4，双手交替打，右手前指抓回；5，双手依次平摊，转花手；"
                "6，右手指向上划，摆胯；7，左手锤右肩3次；8，双手交叠放胸口，坐胯；"
                "9，右手下压2次，双手下压；10，双手依次摊开，转花手；"
                "11，划动，右手拍，左手锤；12，双手打开屈肘下捶，左右交替下捶；"
                "13，顶胯，抬手，呼气；14，双手举起卖萌，收尾定格。"
                "动作幅度自然清晰，胯部以小幅坐胯和顶胯为主，手到胯到，不甩大胯；身体踮脚踩节奏，动作衔接流畅，"
                "手指数量正常，头发和衣物只产生符合惯性的自然摆动。"
                "所有动作必须卡在《Blow (鱼块摇)》DJ电子鼓点的重拍上，卡点精准，节奏明快。"
                "音轨必须从第0秒开始并与动作同步：BGM使用《Blow (鱼块摇)》DJ电子鼓点版，加入短促人声采样片段但不得出现对白；"
                "禁止古风乐器独奏；禁止古风乐器音效；弹琵琶仅表现手部拨弦手势，不要生成琵琶、古筝或其他古风乐器音效。"
                "所有点指、绕腕、敲击、下压、转花手、摆胯、下捶和定格必须卡在鼓点重拍上。"
                "必须输出可听见的BGM，禁止静音、禁止唱歌、禁止口型说话。"
                "自然光影、真实肤质、动态流畅；避免动作延迟、机械循环、手指粘连、手腕反折、肢体穿模、额外手臂、手指畸形、"
                "头发遮脸、服装漂移、背景闪烁和镜头剧烈抖动。"
            ),
            "description": "鱼块摇魔性带感12秒卡点舞，Blow DJ鼓点，动作干脆利落",
            "duration": 12,
        },
        "提裙摇": {
            "prompt": (
                "生成10秒9:16竖屏高清真人舞蹈视频，带原生同步音轨；人物从第0秒开始跟拍舞动，始终居中。"
                "镜头随律动轻微晃动但保持稳定，不突然推拉、不切镜；长发随肢体惯性自然飘动，始终不遮挡脸部。"
                "整体是俏皮、带感、卡点精准的提裙摇风格。动作重点是单手从长裙前片底部正中抓起，将整个前裙向上兜起，"
                "而不是只提起侧边一小部分。动作必须按以下顺序完整执行，不循环、不跳过："
                "1，人物正面站立，一只手从长裙前片底部正中位置抓住整个前裙摆，不是抓侧边，不是只捏边缘；"
                "把前裙整体向上兜起，裙摆边缘提到大腿上方一半，露出一侧腿部，另一半裙摆自然垂落，形成横向自然褶皱；"
                "2，保持手抓前片正中的位置，身体随节奏左右小幅度摆胯；被兜起的前裙保持大面积上提，不要缩成一小条，不要滑回腰侧；"
                "3，配合左右坐胯，身体轻微前后晃动；手始终位于长裙前侧中央，前裙被提起的部分呈扇形铺开，裙面有自然横向褶皱；"
                "4，手把前片裙摆再向上兜高一点，让裙摆边缘停在大腿上方，露出部分腿部；另一只手随节奏抬起或向侧方打开，配合左右摆胯；"
                "5，保持单手从正中兜起长裙前片，裙摆不要被拉到身体一侧；前片上提区域保持居中，左右摆胯时裙摆边缘仍然横向展开；"
                "6，小幅度下蹲，单手继续抓住前片底部正中，长裙前片保持被兜起，不拖地、不踩裙，另一半裙摆自然垂落；"
                "7，单手轻展被兜起的前裙，让裙摆边缘形成自然弧线，但不要松开正中抓点；配合左右摆胯；"
                "8，重新确认手在长裙前侧中央位置，保持前片整体上提，配合左右顶胯，身体轻微前后晃动；"
                "9，收尾定格时，人物仍保持单手从长裙前片底部正中兜起的姿势，裙摆拉到大腿上方一半，露出一侧腿部，"
                "另一半自然垂落，横向褶皱清晰自然。"
                "关键动作要求：不是抓侧边，不是捏边缘，不是只提起一小条布料。手必须位于长裙前片底部正中，"
                "把整个前裙像兜起一样向上提；被提起的裙摆应呈横向展开的扇形，有明显自然褶皱。"
                "视觉效果是长裙前片被整体提起来，而不是侧边被轻轻拎起一点。"
                "所有动作必须卡在DJ电子鼓点的重拍上，卡点精准，节奏明快。"
                "音轨必须从第0秒开始并与动作同步：BGM使用DJ电子鼓点版，加入短促人声采样片段但不得出现对白；"
                "禁止古风乐器独奏，仅保留鼓点节奏，不要生成琵琶、古筝或其他古风乐器音效。必须输出可听见的BGM，禁止静音、禁止口型说话。"
                "自然光影、真实肤质、动态流畅；避免动作延迟、机械循环、手指粘连、手腕反折、肢体穿模、额外手臂、"
                "手指畸形、头发遮脸、服装漂移、背景闪烁和镜头剧烈抖动。"
            ),
            "description": "提裙摇10秒单手正中兜起前裙卡点舞，前裙扇形展开，带同步BGM",
            "duration": 10,
        },
        "椅子摇": {
            "prompt": (
                "生成10秒9:16竖屏高清真人舞蹈视频，带原生同步音轨；人物从第0秒开始坐在椅子上舞动，始终居中。"
                "镜头随律动轻微晃动但保持稳定，不突然推拉、不切镜；长发随肢体惯性自然飘动，始终不遮挡脸部。"
                "整体是俏皮、带感、卡点精准的椅子摇风格，动作慵懒有力。动作必须按以下顺序完整执行，不循环、不跳过："
                "1，双手轻抓红色飘袖，身体随节奏左右小幅度摆胯，双腿交叉保持坐姿；"
                "2，双手交替甩动飘袖，同时配合左右坐胯，身体轻微前后晃动；"
                "3，双手向上抬起，手腕轻转，同时小幅度顶胯，双腿保持交叉；"
                "4，双手向两侧打开，飘袖自然垂落，配合左右摆胯；"
                "5，双手交叠放于身前，配合左右顶胯，身体轻微前后晃动；"
                "6，双手向上抬起至头顶，手腕轻转，同时小幅度下蹲（臀部微抬）；"
                "7，双手向两侧打开，飘袖自然垂落，配合左右摆胯；"
                "8，双手交叠放于身前，配合左右顶胯，身体轻微前后晃动；"
                "9，双手自然垂放于身体两侧，配合左右摆胯，收尾定格。"
                "所有动作必须卡在《椅子摇》DJ电子鼓点的重拍上，卡点精准，节奏明快。"
                "音轨必须从第0秒开始并与动作同步：BGM使用《椅子摇》DJ电子鼓点版，加入短促人声采样片段但不得出现对白；"
                "禁止古风乐器音效，不要生成琵琶、古筝或其他古风乐器音效。必须输出可听见的BGM，禁止静音、禁止口型说话。"
                "自然光影、真实肤质、动态流畅；避免动作延迟、机械循环、手指粘连、手腕反折、肢体穿模、额外手臂、"
                "手指畸形、头发遮脸、服装漂移、背景闪烁和镜头剧烈抖动。"
            ),
            "description": "椅子摇10秒俏皮坐姿卡点舞，飘袖和坐胯动作，带同步BGM",
            "duration": 10,
        },
        "慢摇": {
            "prompt": (
                "生成10秒9:16竖屏高清真人舞蹈视频，带原生同步音轨；人物从第0秒开始跟拍舞动，始终居中，禁止静止展示服装。"
                "严格继承输入首帧中的人物身份、脸部、发型、体型、服饰和场景，不换装。镜头随律动轻微晃动但保持稳定，"
                "不突然推拉、不切镜；长发随肢体惯性自然飘动，始终不遮挡脸部。整体是慵懒、带感、卡点精准的慢摇风格，动作柔和有节奏。"
                "动作必须按以下顺序完整执行，不循环、不跳过：1，身体随节奏缓慢左右摆胯，双手自然垂放于身体两侧；"
                "2，双手轻搭腰侧，配合左右坐胯，身体轻微前后晃动；3，双手向上抬起至胸前，手腕轻转，同时小幅度顶胯；"
                "4，双手向两侧打开，手腕轻转，配合左右摆胯；5，双手交叠放于身前，配合左右顶胯，身体轻微前后晃动；"
                "6，双手向上抬起至头顶，手腕轻转，同时小幅度下蹲；7，双手向两侧打开，手腕轻转，配合左右摆胯；"
                "8，双手交叠放于身前，配合左右顶胯，身体轻微前后晃动；9，双手向上抬起至胸前，手腕轻转，同时小幅度顶胯；"
                "10，双手自然垂放于身体两侧，配合左右摆胯，收尾定格。"
                "所有动作必须卡在慢摇DJ电子鼓点的重拍上，卡点精准，节奏舒缓。"
                "音轨必须从第0秒开始并与动作同步：BGM使用慢摇DJ电子鼓点版，加入短促人声采样片段但不得出现对白；"
                "禁止古风乐器音效，不要生成琵琶、古筝或其他古风乐器音效。必须输出可听见的BGM，禁止静音、禁止口型说话。"
                "自然光影、真实肤质、动态流畅；避免动作延迟、机械循环、手指粘连、手腕反折、肢体穿模、额外手臂、"
                "手指畸形、头发遮脸、服装漂移、背景闪烁和镜头剧烈抖动。"
            ),
            "description": "慢摇10秒慵懒丝滑卡点舞，柔和摆胯，带同步BGM",
            "duration": 10,
        },
        "后裔摇": {
            "prompt": (
                "生成12秒9:16竖屏高清真人舞蹈视频，带原生同步音轨；人物从第0秒开始跟拍舞动，始终居中。"
                "镜头随律动轻微晃动但保持稳定，不突然推拉、不切镜；长发随肢体惯性自然飘动，始终不遮挡脸部。"
                "动作必须按以下顺序完整执行，不循环、不跳过：1，双手打开，手腕轻转，同时左右摆胯；"
                "2，右手举至耳边，左手叉腰，配合左右坐胯；3，双手握拳，交替前后摆动，同时小幅度扭胯；"
                "4，双手交叠放于身前，配合左右顶胯；5，双手向上抬起，手腕轻转，同时小幅度下蹲；"
                "6，双手向两侧打开，手腕轻转，配合左右摆胯；7，双手握拳，交替向上抬起，同时小幅度扭胯；"
                "8，双手交叠放于身前，配合左右顶胯；9，双手向上抬起，手腕轻转，同时小幅度下蹲；"
                "10，双手向两侧打开，手腕轻转，配合左右摆胯；11，双手握拳，交替前后摆动，同时小幅度扭胯；"
                "12，双手交叠放于身前，配合左右顶胯，收尾定格。所有动作必须卡在《后裔摇》DJ电子鼓点的重拍上，卡点精准，节奏明快。"
                "音轨必须从第0秒开始并与动作同步：BGM使用《后裔摇》DJ电子鼓点版，加入短促人声采样片段但不得出现对白；"
                "禁止古风乐器音效，弹琵琶仅表现手部拨弦手势，不要生成琵琶、古筝或其他古风乐器音效。必须输出可听见的BGM，禁止静音、禁止口型说话。"
                "自然光影、真实肤质、动态流畅；避免动作延迟、机械循环、手指粘连、手腕反折、肢体穿模、额外手臂、"
                "手指畸形、头发遮脸、服装漂移、背景闪烁和镜头剧烈抖动。"
            ),
            "description": "后裔摇12秒节奏卡点舞，摆胯扭胯与拳臂动作，带同步BGM",
            "duration": 12,
        },
        "正太扭腰": _hip_sway_video_preset(
            "正太扭腰",
            "正太扭腰",
            [
                "双手轻搭腰侧，膝盖放松，腰部向右再向左各扭一次，胯部跟随回弹",
                "右肩向前送、左肩向后收，腰部连续扭动两拍，双脚保持原地",
                "胸前做连续左右摆臂，腰部同步左右扭转，眼神跟随手臂移动",
                "双手举到头顶画小圈，脚跟交替抬起，腰部保持轻快扭动",
                "右脚向右迈半步再收回，双手向下压两次，腰部跟着脚步摆动",
                "双臂在身前交叉后打开，身体先向左扭再向右扭，重心清楚",
                "一手扶腰、一手从胸前向外划弧，腰部做两次短促扭转",
                "双手在肩前交替拍点，膝盖左右弹动，腰部保持连续小幅旋转",
                "双手向两侧展开后收回腰侧，身体做一次完整左右扭腰连接",
                "双手回到腰侧，向左向右各扭一次后正面定格，保持自然微笑",
            ],
        ),
        "左右顶胯": _hip_sway_video_preset(
            "左右顶胯",
            "左右顶胯",
            [
                "双脚打开略宽于肩，右胯向右顶一下并停一拍，双手握拳放在胸前",
                "左胯向左顶一下并停一拍，右手向前短促出拳，左手护在胸侧",
                "左右交替快速顶胯四次，双手跟着重拍做前后推拉",
                "右脚向侧点地，胯部向右顶；收脚时左胯反向顶回，身体不跳起",
                "双臂向上推过头顶，连续左右顶胯两次，肩膀保持放松",
                "双手向斜下方劈出，右左右三次顶胯，脚底始终贴地",
                "身体小幅转向左侧，面对镜头完成两次交替顶胯，手臂向前平推",
                "回到正面，双手交叉于胸前后打开，左右顶胯各一次并停顿",
                "膝盖微屈下沉，双手向下压，左右各顶一次后立即起身",
                "双脚收回并正面站稳，连续左右顶胯两次，双拳停在胸前定格",
            ],
        ),
        "八字胯": _hip_sway_video_preset(
            "八字胯",
            "八字胯",
            [
                "双手放在腰侧，髋部画‘8’字，先从左上绕到右下再回到中心",
                "髋部画‘8’字的右半圈，双手在胸前交替向外推，脚步保持稳定",
                "左脚向侧迈半步，髋部沿横向画‘8’字，双臂打开保持平衡",
                "右脚收回，髋部画‘8’字并配合一次小蹲起，双手交叠放在胸前",
                "双手举过头顶，髋部连续完成一个完整‘8’字，膝盖随轨迹弹动",
                "双臂向左侧伸展，髋部先向左绕再向右绕，头部跟随手臂转动",
                "双手从头顶落到腰侧，髋部做反向‘8’字，重心由右脚换到左脚",
                "身体保持正面，双手在身前画小圆，髋部沿横向完成两个小‘8’字",
                "双手向两侧推开，髋部由大圈收成小圈，最后回到正中",
                "双手回到腰侧，完成一个清晰完整的‘8’字后正面定格",
            ],
        ),
        "点胯坐胯": _hip_sway_video_preset(
            "点胯坐胯",
            "点胯坐胯",
            [
                "双手放在腰侧，先向右点胯两次，再向下坐胯一次，膝盖同步弯曲",
                "双手在胸前合十，点胯一次后立刻打开手臂，保持上身挺直",
                "左脚向前点地，胯部向前点一下再收回，随后完成一次向下坐胯",
                "双臂向两侧伸展，左右各点胯一次，第三拍向下坐胯并停住",
                "双手举到头顶，连续完成‘点、点、坐’三拍，身体中心垂直下降",
                "右手指向地面，左手扶腰，点胯两次后双手向上带起坐胯动作",
                "换左脚向前点地，胯部先点后坐，身体保持正面不转向",
                "双手交叉于胸前，左右点胯各一次，最后打开手臂完成深一点的坐胯",
                "双手向下压，点胯一次后缓慢坐胯，停一拍再回到站姿",
                "双手收回腰侧，最后完成‘点、点、坐’并正面定格",
            ],
        ),
        "坐胯": _hip_sway_video_preset(
            "坐胯",
            "坐胯",
            [
                "双脚打开，双手放在大腿上方，膝盖弯曲下坐，背部保持挺直",
                "从低位站起半程，双手向前平推，再按重拍回到低位坐胯",
                "双手向上举过头顶，保持腿部低位，连续两拍小幅坐胯",
                "右脚向侧迈一步，身体下沉坐胯，左脚跟随收回不跳起",
                "双手交叉于胸前，向下坐胯后向左侧转髋，保持肩线稳定",
                "双手扶腰，膝盖向外打开，连续完成三次有弹性的下坐与回弹",
                "双臂向两侧打开，身体下坐时重心落在双脚中间，停一拍",
                "双手从腰侧滑到大腿上方，低位坐胯并小幅左右移动膝盖",
                "双手向上抬起，最后一次下坐加深但脚底贴地，随后缓慢起身",
                "双手回到腰侧，膝盖微屈完成一次干净下坐，低位正面定格",
            ],
        ),
        "绕胯": _hip_sway_video_preset(
            "绕胯",
            "绕胯",
            [
                "双手放在腰侧，髋部从左前方开始顺时针绕髋一整圈，膝盖柔软跟随",
                "双手向胸前打开，髋部做半圈绕动，右肩随轨迹向后带动",
                "左脚向侧点地，髋部继续顺时针绕半圈，手臂向左侧延展",
                "双手在身前画圆，髋部完成一整圈，重心从左脚平稳转到右脚",
                "双臂举过头顶，髋部连续绕圈两次，身体保持竖直不前后摇晃",
                "右手扶腰、左手向外划弧，髋部改为逆时针半圈作为方向变化",
                "双脚回正，髋部顺时针绕圈一周，双手从侧面收回胸前",
                "双手向两侧打开，连续做两个小幅绕髋，圈幅由大收小",
                "膝盖微屈，髋部从后向前绕过一圈，双手向下压住最后一个重拍",
                "双手回到腰侧，顺时针绕髋一整圈后回到正面，收尾定格",
            ],
        ),
    }


def slots_for_template(template_id: str) -> List[Dict[str, Any]]:
    meta = STUDIO_TEMPLATES.get(normalize_template_id(template_id)) or {}
    slots: List[Dict[str, Any]] = []
    for spec in meta.get("slots") or []:
        if not isinstance(spec, dict):
            continue
        slots.append(
            {
                "id": _new_id("slot"),
                "role": str(spec.get("role") or "extra"),
                "label": str(spec.get("label") or "参考"),
                "image_path": "",
                "source": "",
                "mime": "",
            }
        )
    return slots


def group_template_slots() -> List[Dict[str, Any]]:
    """Backward-compatible alias → multi-person group layout."""
    return slots_for_template("group")


def empty_session(
    title: str = "",
    *,
    template: str = "duo",
    use_group_template: Optional[bool] = None,
) -> Dict[str, Any]:
    tid = normalize_template_id(template, use_group_template=use_group_template)
    meta = STUDIO_TEMPLATES.get(tid) or STUDIO_TEMPLATES["duo"]
    slots = slots_for_template(tid)
    # input_order: skip pure optional scene at end unless it's the only content
    input_order = [s["id"] for s in slots if s.get("role") not in {"scene"}]
    if not input_order:
        input_order = [s["id"] for s in slots]
    default_title = str(meta.get("default_title") or meta.get("title") or "画布")
    session_id = _new_id("studio")
    root_id = f"node-root-{session_id}"
    root_node = {
        "id": root_id,
        "parent_id": None,
        "record_id": None,
        "title": str(title or default_title).strip() or default_title,
        "template_id": tid,
        "params": {
            "prompt": str(meta.get("prompt") or ""),
            "mode": str(meta.get("mode") or "t2i"),
            "aspect_ratio": str(meta.get("aspect_ratio") or "自动"),
            "resolution": str(meta.get("resolution") or "1K"),
            "count": 1,
        },
        "position": {"x": 0, "y": 0},
        "status": "draft",
        "result": {"media_path": "", "thumbnail_path": ""},
        "created_at": _now(),
    }
    return {
        "id": session_id,
        "title": str(title or default_title).strip() or default_title,
        "created_at": _now(),
        "updated_at": _now(),
        "template": tid,
        "slots": slots,
        "graph": {
            "prompt": str(meta.get("prompt") or ""),
            "mode": str(meta.get("mode") or "t2i"),
            "aspect_ratio": str(meta.get("aspect_ratio") or "自动"),
            "resolution": str(meta.get("resolution") or "1K"),
            "count": 1,
            "input_order": input_order,
            "use_persona_identity": bool(meta.get("use_persona_identity", True)),
        },
        "results": [],
        "last_run": None,
        "canvas": {
            "version": 1,
            "root_node_id": root_id,
            "viewport": {"x": 120, "y": 80, "zoom": 1},
            "nodes": [root_node],
            "edges": [],
        },
    }


def public_session(session: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow copy safe for web (paths only, no bytes)."""
    return copy.deepcopy(session)


class StudioStore:
    def __init__(self, data_dir: str, *, canvas_mode: str = "relationship") -> None:
        self.path = os.path.join(data_dir, STUDIO_FILENAME)
        self.canvas_mode = "creative" if str(canvas_mode or "").strip().lower() == "creative" else "relationship"
        self._lock = threading.RLock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._load_status: Dict[str, Any] = {"ok": True, "error": ""}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            self._load_status = {"ok": True, "error": "", "source": "new"}
            raw = {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self._load_status = {"ok": False, "error": f"画布存储读取失败：{type(exc).__name__}: {exc}", "source": self.path}
            logger.exception("[SelfieImage] studio storage read failed: %s", self.path)
            self._sessions = {}
            return
        items = raw.get("sessions") if isinstance(raw, dict) else None
        out: Dict[str, Dict[str, Any]] = {}
        if isinstance(items, list):
            for index, item in enumerate(items, start=1):
                if isinstance(item, dict) and item.get("id"):
                    item.setdefault("canvas_mode", self.canvas_mode)
                    self._ensure_canvas(item)
                    out[str(item["id"])] = item
                elif item not in (None, {}):
                    logger.warning("[SelfieImage] skipped invalid studio session row %s", index)
        elif isinstance(items, dict):
            for key, item in items.items():
                if isinstance(item, dict):
                    item = dict(item)
                    item.setdefault("id", key)
                    item.setdefault("canvas_mode", self.canvas_mode)
                    self._ensure_canvas(item)
                    out[str(item["id"])] = item
                else:
                    logger.warning("[SelfieImage] skipped invalid studio session %s", key)
        self._load_status = {"ok": True, "error": "", "source": self.path}
        self._sessions = out

    @staticmethod
    def _canvas_params(session: Dict[str, Any]) -> Dict[str, Any]:
        graph = session.get("graph") if isinstance(session.get("graph"), dict) else {}
        return {
            "prompt": str(graph.get("prompt") or ""),
            "mode": str(graph.get("mode") or "group"),
            "aspect_ratio": str(graph.get("aspect_ratio") or "自动"),
            "resolution": str(graph.get("resolution") or "1K"),
            "count": max(1, min(4, int(graph.get("count") or 1))) if str(graph.get("count") or "").isdigit() else 1,
        }

    @classmethod
    def _ensure_canvas(cls, session: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize new and legacy sessions into a drawable graph projection."""
        if not isinstance(session, dict):
            return {}
        canvas = session.get("canvas") if isinstance(session.get("canvas"), dict) else {}
        session_id = str(session.get("id") or "studio")
        root_id = str(canvas.get("root_node_id") or f"node-root-{session_id}")
        raw_nodes = canvas.get("nodes") if isinstance(canvas.get("nodes"), list) else []
        nodes = [dict(node) for node in raw_nodes if isinstance(node, dict) and str(node.get("id") or "").strip()]
        by_id = {str(node.get("id")): node for node in nodes}
        if root_id not in by_id:
            root = {
                "id": root_id,
                "parent_id": None,
                "record_id": None,
                "title": str(session.get("title") or "起始节点"),
                "template_id": str(session.get("template") or ""),
                "params": cls._canvas_params(session),
                "position": {"x": 0, "y": 0},
                "status": "draft",
                "result": {"media_path": "", "thumbnail_path": ""},
                "created_at": str(session.get("created_at") or _now()),
            }
            nodes.insert(0, root)
            by_id[root_id] = root
        else:
            root = by_id[root_id]
            root.setdefault("parent_id", None)
            root.setdefault("params", cls._canvas_params(session))
            root.setdefault("position", {"x": 0, "y": 0})
            root.setdefault("status", "draft")
            root.setdefault("result", {"media_path": "", "thumbnail_path": ""})

        # The legacy creation canvas has no relationship graph. Keep only its
        # root placeholder even when old result rows are present.
        is_creative = str(session.get("canvas_mode") or "").strip().lower() == "creative"
        if is_creative:
            nodes = [by_id[root_id]]
            by_id = {root_id: nodes[0]}

        # Legacy sessions have result rows but no graph. Reconstruct a simple
        # horizontal lineage in creation order; new branches can be appended
        # later without changing the persisted generation records.
        result_rows = [item for item in (session.get("results") or []) if isinstance(item, dict)]
        known_paths = {
            str((node.get("result") or {}).get("media_path") or "")
            for node in nodes
            if isinstance(node.get("result"), dict)
        }
        previous_id = root_id
        for index, result in enumerate(reversed(result_rows) if not is_creative else []):
            path = str(result.get("image_path") or "").strip()
            if not path or path in known_paths:
                continue
            task_id = str(result.get("task_id") or f"result-{index}")
            node_id = f"node-result-{task_id}-{index}"
            while node_id in by_id:
                node_id = f"{node_id}-x"
            node = {
                "id": node_id,
                "parent_id": previous_id,
                "record_id": result.get("record_id"),
                "title": "生成结果",
                "template_id": str(session.get("template") or ""),
                "params": cls._canvas_params(session),
                "position": {"x": (index + 1) * CANVAS_NODE_GAP_X, "y": 0},
                "status": "succeeded",
                "result": {"media_path": path, "thumbnail_path": path},
                "created_at": str(result.get("created_at") or _now()),
            }
            nodes.append(node)
            by_id[node_id] = node
            known_paths.add(path)
            previous_id = node_id

        edges = []
        for node in nodes:
            parent_id = str(node.get("parent_id") or "").strip()
            node_id = str(node.get("id") or "").strip()
            if parent_id and parent_id in by_id and node_id != parent_id:
                edges.append({"id": f"edge-{parent_id}-{node_id}", "source": parent_id, "target": node_id})
        viewport = canvas.get("viewport") if isinstance(canvas.get("viewport"), dict) else {}
        try:
            zoom = max(0.35, min(1.8, float(viewport.get("zoom", 1))))
        except (TypeError, ValueError):
            zoom = 1
        normalized = {
            "version": 1,
            "root_node_id": root_id,
            "viewport": {
                "x": float(viewport.get("x", 120) or 0),
                "y": float(viewport.get("y", 80) or 0),
                "zoom": zoom,
            },
            "nodes": nodes[-CANVAS_MAX_NODES:],
            "edges": edges[-CANVAS_MAX_NODES:],
        }
        session["canvas"] = normalized
        return normalized

    def storage_status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._load_status)

    def _persist(self) -> None:
        ordered = sorted(
            self._sessions.values(),
            key=lambda s: str(s.get("updated_at") or s.get("created_at") or ""),
            reverse=True,
        )
        if len(ordered) > MAX_SESSIONS:
            for drop in ordered[MAX_SESSIONS:]:
                self._sessions.pop(str(drop.get("id") or ""), None)
            ordered = ordered[:MAX_SESSIONS]
        save_json_file_compact(self.path, {"sessions": ordered, "updated_at": _now()})

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._lock:
            ordered = sorted(
                self._sessions.values(),
                key=lambda s: str(s.get("updated_at") or ""),
                reverse=True,
            )
            return [
                {
                    "id": s.get("id"),
                    "title": s.get("title"),
                    "updated_at": s.get("updated_at"),
                    "template": s.get("template"),
                    "mode": ((s.get("graph") or {}).get("mode") if isinstance(s.get("graph"), dict) else "") or "",
                    "slot_count": len(s.get("slots") or []),
                    "result_count": len(s.get("results") or []),
                    "thumb_path": next(
                        (
                            str(r.get("image_path") or "")
                            for r in (s.get("results") or [])
                            if isinstance(r, dict) and str(r.get("image_path") or "").strip()
                        ),
                        "",
                    ),
                    "last_status": str((s.get("last_run") or {}).get("status") or ""),
                    "last_run": s.get("last_run"),
                }
                for s in ordered
            ]

    def referenced_cache_paths(self) -> List[str]:
        """Return cache media paths referenced by every live canvas session."""
        with self._lock:
            paths: List[str] = []
            seen: set[str] = set()

            def add(value: Any) -> None:
                if isinstance(value, str):
                    values = [value]
                elif isinstance(value, (list, tuple, set)):
                    values = list(value)
                else:
                    return
                for item in values:
                    path = str(item or "").strip()
                    if path and path not in seen:
                        seen.add(path)
                        paths.append(path)

            for session in self._sessions.values():
                if not isinstance(session, dict):
                    continue
                for slot in session.get("slots") or []:
                    if isinstance(slot, dict):
                        add(slot.get("image_path"))
                for result in session.get("results") or []:
                    if isinstance(result, dict):
                        add(result.get("image_path"))
                last_run = session.get("last_run")
                if isinstance(last_run, dict):
                    add(last_run.get("result_paths"))
                canvas = session.get("canvas")
                if not isinstance(canvas, dict):
                    continue
                for node in canvas.get("nodes") or []:
                    if not isinstance(node, dict):
                        continue
                    result = node.get("result")
                    if isinstance(result, dict):
                        add(result.get("media_path"))
                        add(result.get("thumbnail_path"))
            return paths

    def get(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            sid = str(session_id or "").strip()
            session = self._sessions.get(sid)
            if not session:
                raise ValueError("画布会话不存在")
            self._ensure_canvas(session)
            return public_session(session)

    def canvas_graph(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            return public_session(self._ensure_canvas(session))

    def update_canvas(self, session_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            canvas = self._ensure_canvas(session)
            if not isinstance(patch, dict):
                raise ValueError("canvas 必须是对象")
            before_canvas = copy.deepcopy(canvas)
            viewport = patch.get("viewport")
            if isinstance(viewport, dict):
                try:
                    canvas["viewport"] = {
                        "x": max(-100000, min(100000, float(viewport.get("x", 0)))),
                        "y": max(-100000, min(100000, float(viewport.get("y", 0)))),
                        "zoom": max(0.35, min(1.8, float(viewport.get("zoom", 1)))),
                    }
                except (TypeError, ValueError):
                    raise ValueError("viewport 参数无效") from None
            positions = patch.get("positions")
            if isinstance(positions, dict):
                by_id = {str(node.get("id")): node for node in canvas["nodes"]}
                for node_id, position in positions.items():
                    node = by_id.get(str(node_id))
                    if not node or not isinstance(position, dict):
                        continue
                    try:
                        node["position"] = {
                            "x": max(-100000, min(100000, float(position.get("x", 0)))),
                            "y": max(-100000, min(100000, float(position.get("y", 0)))),
                        }
                    except (TypeError, ValueError):
                        continue
            node_updates = patch.get("nodes")
            if isinstance(node_updates, list):
                by_id = {str(node.get("id")): node for node in canvas["nodes"]}
                for raw in node_updates:
                    if not isinstance(raw, dict):
                        continue
                    node = by_id.get(str(raw.get("id") or "").strip())
                    if not node:
                        continue
                    if "title" in raw:
                        node["title"] = str(raw.get("title") or "节点").strip()[:80] or "节点"
                    if "template_id" in raw:
                        node["template_id"] = normalize_template_id(str(raw.get("template_id") or ""))
                    params = raw.get("params")
                    if isinstance(params, dict):
                        current = dict(node.get("params") or {})
                        for key in ("prompt", "mode", "aspect_ratio", "resolution", "count", "use_persona_identity"):
                            if key in params:
                                current[key] = params[key]
                        current["prompt"] = str(current.get("prompt") or "").strip()
                        current["mode"] = str(current.get("mode") or "group").strip() or "group"
                        current["aspect_ratio"] = str(current.get("aspect_ratio") or "自动").strip() or "自动"
                        current["resolution"] = str(current.get("resolution") or "1K").strip() or "1K"
                        try:
                            current["count"] = max(1, min(4, int(current.get("count") or 1)))
                        except (TypeError, ValueError):
                            current["count"] = 1
                        node["params"] = current
            session["canvas"] = canvas
            if canvas != before_canvas:
                session["updated_at"] = _now()
                self._persist()
            return public_session(canvas)

    def add_canvas_node(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            canvas = self._ensure_canvas(session)
            if len(canvas["nodes"]) >= CANVAS_MAX_NODES:
                raise ValueError(f"关系网最多 {CANVAS_MAX_NODES} 个节点")
            parent_id = str(payload.get("parent_id") or canvas.get("root_node_id") or "").strip()
            by_id = {str(node.get("id")): node for node in canvas["nodes"]}
            if parent_id not in by_id:
                raise ValueError("父节点不存在")
            node_id = _new_id("node")
            parent = by_id[parent_id]
            position = payload.get("position") if isinstance(payload.get("position"), dict) else {}
            node = {
                "id": node_id,
                "parent_id": parent_id,
                "record_id": None,
                "title": str(payload.get("title") or "新节点").strip()[:80] or "新节点",
                "template_id": str(payload.get("template_id") or session.get("template") or ""),
                "params": dict(payload.get("params") or self._canvas_params(session)) if isinstance(payload.get("params") or {}, dict) else self._canvas_params(session),
                "position": {
                    "x": float(position.get("x", float((parent.get("position") or {}).get("x", 0)) + CANVAS_NODE_GAP_X)),
                    "y": float(position.get("y", float((parent.get("position") or {}).get("y", 0)) + (CANVAS_NODE_GAP_Y if len(canvas["nodes"]) % 2 else -CANVAS_NODE_GAP_Y))),
                },
                "status": "draft",
                "result": {"media_path": "", "thumbnail_path": ""},
                "created_at": _now(),
            }
            canvas["nodes"].append(node)
            canvas["edges"].append({"id": f"edge-{parent_id}-{node_id}", "source": parent_id, "target": node_id})
            session["canvas"] = canvas
            session["updated_at"] = _now()
            self._persist()
            return public_session(node)

    def delete_canvas_node(self, session_id: str, node_id: str) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            canvas = self._ensure_canvas(session)
            node_id = str(node_id or "").strip()
            if node_id == str(canvas.get("root_node_id") or ""):
                raise ValueError("起始节点不能删除")
            before = len(canvas["nodes"])
            canvas["nodes"] = [node for node in canvas["nodes"] if str(node.get("id")) != node_id]
            if len(canvas["nodes"]) == before:
                raise ValueError("节点不存在")
            canvas["edges"] = [edge for edge in canvas["edges"] if edge.get("source") != node_id and edge.get("target") != node_id]
            session["canvas"] = canvas
            session["updated_at"] = _now()
            self._persist()
            return {"deleted": True, "node_id": node_id, "canvas": public_session(canvas)}

    def connect_canvas_node(self, session_id: str, node_id: str, parent_id: str) -> Dict[str, Any]:
        """Reconnect a dangling node to an earlier node in the canvas."""
        with self._lock:
            session = self._require(session_id)
            canvas = self._ensure_canvas(session)
            node_id = str(node_id or "").strip()
            parent_id = str(parent_id or "").strip()
            root_id = str(canvas.get("root_node_id") or "").strip()
            if node_id == root_id:
                raise ValueError("起始节点不能重连")
            by_id = {str(node.get("id")): node for node in canvas.get("nodes") or []}
            target = by_id.get(node_id)
            parent = by_id.get(parent_id)
            if not target or not parent:
                raise ValueError("节点不存在")
            if node_id == parent_id:
                raise ValueError("不能连接到节点自身")

            # Reject descendants as parents so the graph remains acyclic.
            children: Dict[str, List[str]] = {}
            for item in canvas.get("nodes") or []:
                child_parent = str(item.get("parent_id") or "").strip()
                child_id = str(item.get("id") or "").strip()
                if child_parent and child_id:
                    children.setdefault(child_parent, []).append(child_id)
            pending = list(children.get(node_id) or [])
            descendants = set()
            while pending:
                current = pending.pop()
                if current in descendants:
                    continue
                descendants.add(current)
                pending.extend(children.get(current) or [])
            if parent_id in descendants:
                raise ValueError("不能连接到当前节点的后续节点")

            target["parent_id"] = parent_id
            canvas["edges"] = [
                edge for edge in canvas.get("edges") or []
                if str(edge.get("target") or "") != node_id
            ]
            canvas["edges"].append({"id": f"edge-{parent_id}-{node_id}", "source": parent_id, "target": node_id})
            session["canvas"] = canvas
            session["updated_at"] = _now()
            self._persist()
            return {"connected": True, "node_id": node_id, "parent_id": parent_id, "canvas": public_session(canvas)}

    def create(
        self,
        title: str = "",
        *,
        template: str = "duo",
        use_group_template: Optional[bool] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            session = empty_session(title, template=template, use_group_template=use_group_template)
            session["canvas_mode"] = self.canvas_mode
            self._sessions[session["id"]] = session
            self._persist()
            return public_session(session)

    def copy_session(
        self,
        session_id: str,
        *,
        title: str = "",
        valid_slot_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Copy editable canvas state without carrying execution artifacts."""
        with self._lock:
            source = self._require(session_id)
            copied = empty_session(
                title or f"{str(source.get('title') or '未命名画布').strip()} · 副本",
                template=str(source.get("template") or "duo"),
            )
            copied["graph"] = copy.deepcopy(source.get("graph") or {})
            copied["graph"]["input_order"] = list(copied["graph"].get("input_order") or [])
            keep = set(str(item) for item in valid_slot_ids) if valid_slot_ids is not None else None
            slots: List[Dict[str, Any]] = []
            id_map: Dict[str, str] = {}
            for raw_slot in source.get("slots") or []:
                if not isinstance(raw_slot, dict):
                    continue
                old_id = str(raw_slot.get("id") or "").strip()
                if keep is not None and old_id not in keep:
                    continue
                new_slot = copy.deepcopy(raw_slot)
                new_id = _new_id("slot")
                id_map[old_id] = new_id
                new_slot["id"] = new_id
                new_slot.pop("source_record_id", None)
                slots.append(new_slot)
            copied["slots"] = slots[:MAX_SLOTS]
            copied["graph"]["input_order"] = [id_map[item] for item in copied["graph"].get("input_order", []) if item in id_map]
            copied["results"] = []
            copied["last_run"] = None
            copied["updated_at"] = _now()
            self._sessions[copied["id"]] = copied
            self._persist()
            return public_session(copied)

    def delete(self, session_id: str) -> None:
        with self._lock:
            sid = str(session_id or "").strip()
            if sid not in self._sessions:
                raise ValueError("画布会话不存在")
            del self._sessions[sid]
            self._persist()

    def update_graph(self, session_id: str, graph_patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            graph = dict(session.get("graph") or {})
            if not isinstance(graph_patch, dict):
                raise ValueError("graph 必须是对象")
            for key in (
                "prompt",
                "mode",
                "aspect_ratio",
                "resolution",
                "count",
                "input_order",
                "use_persona_identity",
            ):
                if key in graph_patch:
                    graph[key] = graph_patch[key]
            graph["prompt"] = str(graph.get("prompt") or "").strip()
            graph["mode"] = str(graph.get("mode") or "group").strip() or "group"
            graph["aspect_ratio"] = str(graph.get("aspect_ratio") or "自动").strip() or "自动"
            graph["resolution"] = str(graph.get("resolution") or "1K").strip() or "1K"
            try:
                graph["count"] = max(1, min(4, int(graph.get("count") or 1)))
            except Exception:
                graph["count"] = 1
            order = graph.get("input_order") or []
            if not isinstance(order, list):
                order = []
            graph["input_order"] = [str(x) for x in order if str(x).strip()]
            graph["use_persona_identity"] = bool(graph.get("use_persona_identity", True))
            session["graph"] = graph
            if "title" in graph_patch and str(graph_patch.get("title") or "").strip():
                session["title"] = str(graph_patch.get("title")).strip()[:80]
            if "template" in graph_patch and str(graph_patch.get("template") or "").strip():
                # metadata only — do not rebuild slots on graph save
                session["template"] = normalize_template_id(str(graph_patch.get("template")))
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def set_slot_image(
        self,
        session_id: str,
        slot_id: str,
        *,
        image_path: str,
        source: str = "upload",
        mime: str = "",
        label: str = "",
        source_record_id: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            slot = self._find_slot(session, slot_id)
            slot["image_path"] = str(image_path or "").strip()
            slot["source"] = str(source or "upload").strip()
            slot["mime"] = str(mime or "").strip()
            if source_record_id:
                slot["source_record_id"] = str(source_record_id).strip()[:128]
            else:
                slot.pop("source_record_id", None)
            if label:
                slot["label"] = str(label).strip()[:40]
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def clear_slot(self, session_id: str, slot_id: str) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            slot = self._find_slot(session, slot_id)
            slot["image_path"] = ""
            slot["source"] = ""
            slot["mime"] = ""
            slot.pop("source_record_id", None)
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def add_slot(self, session_id: str, role: str = "extra", label: str = "") -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            slots = list(session.get("slots") or [])
            if len(slots) >= MAX_SLOTS:
                raise ValueError(f"槽位最多 {MAX_SLOTS} 个")
            slot = {
                "id": _new_id("slot"),
                "role": str(role or "extra").strip() or "extra",
                "label": str(label or "额外参考").strip()[:40] or "额外参考",
                "image_path": "",
                "source": "",
                "mime": "",
            }
            slots.append(slot)
            session["slots"] = slots
            order = list((session.get("graph") or {}).get("input_order") or [])
            order.append(slot["id"])
            session.setdefault("graph", {})["input_order"] = order
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def reorder_slots(self, session_id: str, order: List[str]) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            ids = [str(x) for x in (order or []) if str(x).strip()]
            by_id = {str(s.get("id")): s for s in (session.get("slots") or []) if isinstance(s, dict)}
            if not ids:
                raise ValueError("顺序不能为空")
            for sid in ids:
                if sid not in by_id:
                    raise ValueError(f"未知槽位 {sid}")
            rest = [s for sid, s in by_id.items() if sid not in ids]
            session["slots"] = [by_id[sid] for sid in ids] + rest
            session.setdefault("graph", {})["input_order"] = ids
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def attach_run_start(self, session_id: str, task_id: str, summary: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            session["last_run"] = {
                "task_id": task_id,
                "status": "running",
                "started_at": _now(),
                "summary": dict(summary or {}),
                "error": "",
                "result_paths": [],
            }
            canvas = self._ensure_canvas(session)
            target_id = str((summary or {}).get("target_node_id") or "").strip()
            by_id = {str(node.get("id")): node for node in canvas.get("nodes") or []}
            target = by_id.get(target_id)
            if target:
                target["status"] = "running"
                target["error"] = ""
                if str((summary or {}).get("template") or "").strip():
                    target["template_id"] = normalize_template_id(str(summary.get("template")))
                if isinstance((summary or {}).get("graph_params"), dict):
                    target["params"] = dict(summary["graph_params"])
                target["task_id"] = task_id
            session["canvas"] = canvas
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def attach_run_finish(
        self,
        session_id: str,
        task_id: str,
        *,
        success: bool,
        error: str = "",
        result_paths: Optional[List[str]] = None,
        used_model: str = "",
        source_asset_ids: Optional[List[str]] = None,
        status: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            paths = [str(p) for p in (result_paths or []) if str(p).strip()]
            last = dict(session.get("last_run") or {})
            last.update(
                {
                    "task_id": task_id,
                    "status": status if status in {"succeeded", "partial_success", "failed", "delivery_failed", "cancelled"} else ("succeeded" if success else "failed"),
                    "finished_at": _now(),
                    "error": str(error or ""),
                    "result_paths": paths,
                    "used_model": str(used_model or ""),
                }
            )
            session["last_run"] = last
            # Keep generated paths visible for partial or delivery-failed
            # terminal states as well; ``success=False`` only describes the
            # overall operation, not whether an artifact exists.
            if paths and (success or status in {"partial_success", "delivery_failed"}):
                # Normalize before adding result rows so legacy reconstruction
                # cannot create a duplicate node for the current task.
                canvas = self._ensure_canvas(session)
                results = list(session.get("results") or [])
                asset_ids = [str(item).strip() for item in (source_asset_ids or []) if str(item).strip()][:24]
                created_results: List[Dict[str, Any]] = []
                for path in paths:
                    result = {
                        "id": _new_id("res"),
                        "image_path": path,
                        "created_at": _now(),
                        "task_id": task_id,
                        "used_model": used_model,
                        "studio_session_id": session_id,
                    }
                    if asset_ids:
                        result["source_asset_ids"] = list(asset_ids)
                    results.insert(
                        0,
                        result,
                    )
                    created_results.append(result)
                session["results"] = results[:MAX_RESULTS_KEEP]
                by_id = {str(node.get("id")): node for node in canvas["nodes"]}
                summary = last.get("summary") if isinstance(last.get("summary"), dict) else {}
                if str(summary.get("canvas_mode") or "").strip().lower() == "creative":
                    # The legacy creation canvas keeps a flat result list. Its
                    # sessions deliberately do not receive relationship edges
                    # or generated nodes from the infinite canvas namespace.
                    session["canvas"] = canvas
                    session["updated_at"] = _now()
                    self._persist()
                    return public_session(session)
                parent_id = str(summary.get("parent_node_id") or canvas.get("root_node_id") or "").strip()
                if parent_id not in by_id:
                    parent_id = str(canvas.get("root_node_id") or "")
                params = summary.get("graph_params") if isinstance(summary.get("graph_params"), dict) else self._canvas_params(session)
                sibling_count = sum(1 for node in canvas["nodes"] if str(node.get("parent_id") or "") == parent_id)
                target_id = str(summary.get("target_node_id") or "").strip()
                target = by_id.get(target_id)
                if target and str(target.get("status") or "") in {"draft", "queued", "running"} and created_results:
                    first = created_results.pop(0)
                    target.update(
                        {
                            "record_id": first.get("id"),
                            "title": "生成结果",
                            "template_id": str(summary.get("template") or session.get("template") or ""),
                            "params": dict(params),
                            "status": "succeeded",
                            "error": "",
                            "task_id": task_id,
                            "result": {"media_path": first.get("image_path") or "", "thumbnail_path": first.get("image_path") or ""},
                        }
                    )
                    parent_id = target_id
                for index, result in enumerate(created_results):
                    node_id = f"node-task-{task_id}-{index}"
                    if node_id in by_id:
                        continue
                    parent = by_id.get(parent_id) or {}
                    parent_pos = parent.get("position") if isinstance(parent.get("position"), dict) else {}
                    node = {
                        "id": node_id,
                        "parent_id": parent_id or None,
                        "record_id": result.get("id"),
                        "title": "生成结果" if len(created_results) == 1 else f"生成结果 {index + 1}",
                        "template_id": str(summary.get("template") or session.get("template") or ""),
                        "params": dict(params),
                        "position": {
                            "x": float(parent_pos.get("x", 0) or 0) + CANVAS_NODE_GAP_X,
                            "y": float(parent_pos.get("y", 0) or 0) + (sibling_count + index - 0.5) * CANVAS_NODE_GAP_Y,
                        },
                        "status": "succeeded",
                        "result": {"media_path": result.get("image_path") or "", "thumbnail_path": result.get("image_path") or ""},
                        "created_at": result.get("created_at") or _now(),
                    }
                    canvas["nodes"].append(node)
                    by_id[node_id] = node
                    if parent_id:
                        canvas["edges"].append({"id": f"edge-{parent_id}-{node_id}", "source": parent_id, "target": node_id})
                canvas["nodes"] = canvas["nodes"][-CANVAS_MAX_NODES:]
                canvas["edges"] = canvas["edges"][-CANVAS_MAX_NODES:]
                session["canvas"] = canvas
            else:
                canvas = self._ensure_canvas(session)
            summary = last.get("summary") if isinstance(last.get("summary"), dict) else {}
            target_id = str(summary.get("target_node_id") or "").strip()
            target = next(
                (node for node in canvas.get("nodes") or [] if str(node.get("id") or "") == target_id),
                None,
            )
            if target and str(target.get("status") or "") in {"draft", "queued", "running"}:
                target["status"] = "cancelled" if status == "cancelled" else "failed"
                target["error"] = str(error or ("任务已取消" if status == "cancelled" else "生成失败"))
                target["task_id"] = task_id
            session["canvas"] = canvas
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def promote_result_to_slot(self, session_id: str, result_id: str, slot_id: str) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            result = None
            for item in session.get("results") or []:
                if str(item.get("id")) == str(result_id):
                    result = item
                    break
            if not result:
                raise ValueError("结果不存在")
            path = str(result.get("image_path") or "").strip()
            if not path:
                raise ValueError("结果没有图片")
            slot = self._find_slot(session, slot_id)
            slot["image_path"] = path
            slot["source"] = "generated"
            slot.pop("source_record_id", None)
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def promote_result_to_role(
        self,
        session_id: str,
        result_id: str,
        role: str,
        *,
        create_if_missing: bool = True,
    ) -> Dict[str, Any]:
        """Put a result into the first slot of role; optionally create that slot."""
        role_key = str(role or "").strip().lower() or "extra"
        role_labels = {
            "identity": "形象",
            "base": "底图",
            "outfit": "服装",
            "peer": "同框",
            "pose": "姿势",
            "scene": "场景",
            "style": "风格",
            "detail": "细节",
            "extra": "额外参考",
        }
        with self._lock:
            session = self._require(session_id)
            result = None
            for item in session.get("results") or []:
                if str(item.get("id")) == str(result_id):
                    result = item
                    break
            if not result:
                raise ValueError("结果不存在")
            path = str(result.get("image_path") or "").strip()
            if not path:
                raise ValueError("结果没有图片")
            slot = next(
                (
                    s
                    for s in (session.get("slots") or [])
                    if isinstance(s, dict) and str(s.get("role") or "") == role_key
                ),
                None,
            )
            if not slot and create_if_missing:
                slots = list(session.get("slots") or [])
                if len(slots) >= MAX_SLOTS:
                    raise ValueError(f"槽位最多 {MAX_SLOTS} 个")
                slot = {
                    "id": _new_id("slot"),
                    "role": role_key,
                    "label": role_labels.get(role_key, role_key),
                    "image_path": "",
                    "source": "",
                    "mime": "",
                }
                slots.append(slot)
                session["slots"] = slots
                order = list((session.get("graph") or {}).get("input_order") or [])
                order.append(slot["id"])
                session.setdefault("graph", {})["input_order"] = order
            if not slot:
                raise ValueError(f"没有「{role_labels.get(role_key, role_key)}」槽位")
            slot["image_path"] = path
            slot["source"] = "generated"
            slot.pop("source_record_id", None)
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def set_slot_from_cache_path(
        self,
        session_id: str,
        slot_id: str,
        image_path: str,
        *,
        source: str = "record",
        mime: str = "",
    ) -> Dict[str, Any]:
        return self.set_slot_image(
            session_id,
            slot_id,
            image_path=image_path,
            source=source,
            mime=mime,
        )

    def _require(self, session_id: str) -> Dict[str, Any]:
        sid = str(session_id or "").strip()
        session = self._sessions.get(sid)
        if not session:
            raise ValueError("画布会话不存在")
        return session

    def _find_slot(self, session: Dict[str, Any], slot_id: str) -> Dict[str, Any]:
        sid = str(slot_id or "").strip()
        for slot in session.get("slots") or []:
            if isinstance(slot, dict) and str(slot.get("id")) == sid:
                return slot
        raise ValueError("槽位不存在")


def resolve_slot_refs_for_run(
    session: Dict[str, Any],
    *,
    persona_ref: Optional[Dict[str, Any]],
    load_path_bytes,
) -> Tuple[List[Tuple[bytes, str]], List[str]]:
    """Return (refs as (bytes,mime), ordered slot ids used).

    load_path_bytes(rel_path) -> Optional[Tuple[bytes, mime]]
    """
    graph = session.get("graph") or {}
    slots = {str(s.get("id")): s for s in (session.get("slots") or []) if isinstance(s, dict)}
    order = [str(x) for x in (graph.get("input_order") or []) if str(x) in slots]
    if not order:
        order = [
            str(s.get("id"))
            for s in (session.get("slots") or [])
            if s.get("image_path") or s.get("role") in {"identity", "base"}
        ]

    refs: List[Tuple[bytes, str]] = []
    used: List[str] = []
    use_persona = bool(graph.get("use_persona_identity", True))
    identity_filled = False

    for sid in order:
        slot = slots.get(sid) or {}
        role = str(slot.get("role") or "")
        path = str(slot.get("image_path") or "").strip()
        if role in {"identity", "base"} and not path and use_persona and persona_ref and persona_ref.get("data"):
            refs.append((persona_ref["data"], str(persona_ref.get("mime_type") or "image/png")))
            used.append(sid)
            identity_filled = True
            continue
        if not path:
            continue
        loaded = load_path_bytes(path)
        if not loaded:
            continue
        data, mime = loaded
        if not data:
            continue
        refs.append((data, mime or "image/png"))
        used.append(sid)
        if role in {"identity", "base"}:
            identity_filled = True

    mode = str(graph.get("mode") or "group")
    if mode in {"group", "selfie"} and use_persona and not identity_filled and persona_ref and persona_ref.get("data"):
        refs.insert(0, (persona_ref["data"], str(persona_ref.get("mime_type") or "image/png")))

    return refs, used


def build_studio_action(session: Dict[str, Any]) -> str:
    """Build generation action text from graph mode + prompt."""
    graph = session.get("graph") or {}
    prompt = str(graph.get("prompt") or "").strip()
    mode = str(graph.get("mode") or "group").strip().lower()
    template = str(session.get("template") or "").strip().lower()
    if mode == "group" or template in {"duo", "group"}:
        base = (
            "合影 / 合照 / 同框。AI 自己必须作为画面主角之一，与参考图对象自然同框。"
            "身份锁脸型五官发型体态；表情按合影氛围自然重画。"
            "同框对象按主角形象类型统一画风（自动/真人/动漫）；非人物参考拟人时无明确性别默认成年女性。"
            "不要再强制把所有对象改成写实真人。"
        )
        return f"{base} 用户补充要求：{prompt}。" if prompt else base
    if mode == "selfie" or template in {"selfie", "clothes"}:
        if looks_like_cos_prompt(prompt):
            # COS pool entries may contain legacy mirror/selfie wording.  The
            # canvas must emit the same third-person contract as /看看COS,
            # otherwise the clothes template adds a phone-selfie instruction.
            return build_cos_third_person_prompt(prompt)
        if template == "clothes" or "换装" in prompt or "COS" in prompt.upper() or "cos" in prompt:
            base = "换装/穿搭自拍：服装来自参考，身份保持，表情眼神按本次场景自然重画，看向镜头。"
        else:
            base = "看着镜头自然自拍，展示你现在的样子。"
        return f"{base} {prompt}".strip() if prompt else base
    return prompt or "看着镜头自然自拍"
