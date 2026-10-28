"""Central prompt templates shared by image-generation paths."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..cos.leg_focus import (
    ensure_leg_focus_action,
    get_leg_focus_outfit,
    has_leg_focus_contract,
)


COS_IDENTITY_CONTRACT_ZH = (
    "以身份参考图为本人身份来源：保留本人脸型轮廓、五官比例、眼睛与嘴唇轮廓、性别、年龄感和肤色。\n"
    "COS 只改变服装、配饰、角色假发/发型和妆容，不把角色原画的脸当作新身份。\n"
    "除非用户明确要求，不固定角色瞳色、脸型或五官比例。"
)

COS_IDENTITY_CONTRACT_EN = (
    "Use the identity reference image as the same person's identity anchor: preserve the person's face outline, "
    "facial feature proportions, eye and lip contours, gender, apparent age, and skin tone.\n"
    "COS changes only clothing, accessories, the character wig or hairstyle, and makeup; do not replace the person "
    "with the face of the original character.\n"
    "Unless the user explicitly requests it, do not lock the character's eye color, face shape, or facial feature proportions."
)


@dataclass(frozen=True)
class BilingualPrompt:
    builtin_zh: str
    builtin_en: str
    user_text: str = ""

    def render_zh(self) -> str:
        return _join(self.builtin_zh, f"用户要求：{self.user_text}" if self.user_text else "")

    def render_en(self, translated_user_text: str = "") -> str:
        user = str(translated_user_text or "").strip()
        return _join(self.builtin_en, f"User request: {user}" if user else "")


def _join(*parts: str) -> str:
    return "\n".join(str(part).strip() for part in parts if str(part).strip())


_VIEWPOINT_CHANGE_RE = re.compile(
    r"正面(?:机位|拍摄|构图|视角|半身|人像|镜头|图)|正对镜头|正脸(?:视角|镜头|近景|半身|朝向|照片|转)|"
    r"(?:正面|正脸)(?:转|改|变)(?:为|成)?侧(?:面|脸)|侧(?:面|脸)(?:转|改|变)(?:为|成)?正(?:面|脸)|"
    r"三分之四侧(?:前方|身|面)?|侧脸(?:视角|特写|近景|人像|朝向)?|侧身(?:回看|拍摄|机位|构图|人像)|"
    r"侧前方(?:机位|拍摄|人像|构图)?|侧后方(?:机位|拍摄|视角|构图)?|从侧面拍(?:摄)?|"
    r"侧面(?:机位|拍摄|视角|镜头|近景|人像|构图|照片|图|转|改|变)|侧拍|背影|背面(?:视角|机位|拍摄|构图)|"
    r"从后方|后方(?:机位|视角)|俯拍|俯视|仰拍|仰视|高机位|低机位|顶视|鸟瞰|过肩|上方机位|"
    r"低角度|高角度|换(?:个|一个)?角度|改变视角|变换视角|切换视角|"
    r"face[- ]?on|front[- ]view|profile|side[- ](?:view|angle)|three[- ]quarter(?:[- ](?:view|angle))?|"
    r"rear[- ]view|from[- ]behind|from (?:the )?(?:side|rear|above|below)|top[- ]down|overhead|bird['’]?s[- ]eye|high[- ]angle|low[- ]angle|"
    r"change (?:the )?(?:camera )?angle|different angle|change viewpoint",
    re.I,
)
_VIEWPOINT_NEGATION_RE = re.compile(
    r"(?:不要|别|避免|禁止|不得|不需要|不想|"
    r"do not|don't|avoid|never|no)\s*(?:过度|刻意|强行|use|render|make|show)?"
    r"\s*(?:从|向|用|采用|使用|生成|拍成|画成|use|render|make|show|镜头|机位|视角|角度)?"
    r"\s*(?:a|the)?"
    r"\s*(?:过度|刻意|强行)?\s*$",
    re.I,
)


def has_viewpoint_change_request(text: str) -> bool:
    """Detect a concrete requested or selected camera view, not clothing details."""
    value = str(text or "")
    for match in _VIEWPOINT_CHANGE_RE.finditer(value):
        clause_start = max(value.rfind(mark, 0, match.start()) for mark in "，,。；;\n") + 1
        prefix = value[clause_start : match.start()]
        if not _VIEWPOINT_NEGATION_RE.search(prefix):
            return True
    return False


def resolve_appearance_type(appearance_type: str, user_text: str = "") -> str:
    """Let an explicit per-request medium override the saved appearance setting."""
    configured = str(appearance_type or "auto").strip().lower()
    configured = {
        "realistic": "real",
        "photo": "real",
        "photographic": "real",
        "真人": "real",
        "写实": "real",
        "cartoon": "anime",
        "2d": "anime",
        "动漫": "anime",
        "动画": "anime",
        "二次元": "anime",
    }.get(configured, configured)
    if configured not in {"auto", "real", "anime"}:
        configured = "auto"

    text = str(user_text or "")
    explicit_patterns = {
        "real": (
            r"真人(?:风格|照片|摄影|版|形象)|写实(?:风格|照片|摄影)|真实照片|照片写实",
            r"\b(?:photorealistic|realistic\s+(?:photo|photography|image)|real[- ]life\s+photo)\b",
        ),
        "anime": (
            r"动漫(?:风格|插画|画风|版|形象)|二次元(?:风格|插画|画风|版|形象)|"
            r"动画(?:风格|插画|画风)|漫画风格|插画风格",
            r"\b(?:anime\s+(?:style|illustration|art|look)|2d\s+anime|cartoon\s+illustration)\b",
        ),
    }
    negation = re.compile(
        r"(?:不要|不想要|避免|禁止|拒绝|不使用|不需要|别(?:用|画成|做成)?)"
        r"(?:生成|绘制|画成|做成|采用|使用)?\s*$",
        re.I,
    )
    matches = []
    opposite = {"real": "anime", "anime": "real"}
    for kind, patterns in explicit_patterns.items():
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.I):
                prefix = text[max(0, match.start() - 8) : match.start()]
                matches.append(
                    (match.start(), opposite[kind] if negation.search(prefix) else kind)
                )
    return max(matches)[1] if matches else configured


def append_daily_context_to_english_prompt(english_prompt: str, source_prompt: str) -> str:
    """Keep daily lines selected by the Chinese builder during EN conversion."""
    labels = {
        "今日穿搭": "Today's outfit",
        "当前时间段": "Current time period",
        "当前状态": "Current state",
        "当前心情": "Current mood",
    }
    lines = []
    for source_label, english_label in labels.items():
        match = re.search(rf"^{re.escape(source_label)}：(.+)$", str(source_prompt or ""), re.M)
        if match:
            lines.append(f"{english_label}: {match.group(1).strip()}")
    if not lines:
        return str(english_prompt or "").strip()
    return _join(english_prompt, "Daily context (follow unless the user explicitly overrides it):", *lines)


def extract_user_prompt(action: str) -> str:
    """Extract free-form user text from a wrapped selfie action."""
    text = str(action or "").strip()
    match = re.search(r"(?:用户补充要求优先|用户补充要求|额外要求|用户要求)[:：]\s*(.+)", text, re.S)
    if match:
        value = match.group(1)
        value = re.sub(r"\s*【(?:pose|shot|cos|cam|legs|wear|outfit):[a-z0-9_]+】\s*", " ", value)
        return re.sub(r"\s+", " ", value).strip(" 。")
    if any(marker in text for marker in ("【自拍 / 看看模式】", "【他拍 / 看看你模式】", "【自拍 / 看看COS模式】", "【他拍 / 看看COS模式】", "【第一视角 / 看看COS模式】", "【合影 / 合照模式】", "看看腿。", "【legs:outfit】", "成年人物日常下半身穿搭展示", "【唯一姿势·不可混用】")):
        return ""
    return text


def extract_generated_action_contract(action: str) -> str:
    """Keep the generated action body separate from its user supplement."""
    text = str(action or "").strip()
    match = re.search(r"\s*用户补充要求优先[:：]", text)
    if match:
        return text[: match.start()].rstrip(" 。")
    return text


def build_selfie_builtin_prompt(
    action: str,
    *,
    language: str = "zh",
    has_reference_image: bool = False,
    extra_reference_count: int = 0,
    appearance_type: str = "auto",
    user_text: str = "",
    action_content: str = "",
) -> str:
    """Build a compact central built-in prompt; user text is appended separately."""
    raw_action = str(action or "").strip()
    raw_is_cos = "看看COS" in raw_action or "看看cos" in raw_action.lower() or "【cos:" in raw_action
    raw_is_legs = (not raw_is_cos) and any(
        marker in raw_action
        for marker in ("看看腿", "腿部", "下半身穿搭", "穿搭展示", "【legs:outfit】", "【pose:")
    )
    if raw_is_legs and not has_leg_focus_contract(raw_action):
        action = ensure_leg_focus_action(raw_action, has_reference_image)
    translated_user = str(user_text or "").strip()
    viewpoint_change = has_viewpoint_change_request(
        _join(raw_action, str(action_content or ""), translated_user)
    )
    effective_appearance_type = resolve_appearance_type(
        appearance_type,
        _join(extract_user_prompt(raw_action), translated_user),
    )
    rendered_action = str(action_content or action or "").strip()
    is_cos = "看看COS" in str(action) or "看看cos" in str(action).lower() or "【cos:" in str(action)
    is_legs = (not is_cos) and (
        "看看腿" in str(action)
        or "腿部" in str(action)
        or "下半身穿搭" in str(action)
        or "【legs:outfit】" in str(action)
        or "【pose:" in str(action)
    )
    is_group = (not is_cos) and ("合影" in str(action) or "合照" in str(action) or "同框" in str(action))
    # Look-legs uses a close lower-body composition by default; feet may remain visible when natural.
    feet_cropped = (
        is_legs
        or "【pose:reclined_knees_crop】" in str(action)
        or "【crop:calves】" in str(action)
        or bool(re.search(r"【pose:\w+_crop】", str(action)))
        or "双脚完整裁出画外" in str(action)
        or "不展示脚部" in str(action)
    )
    if language == "en":
        style = {
            "real": "realistic photographic",
            "anime": "soft Kyoto Animation-like anime with a finer face and ethereal atmosphere",
            "auto": "the primary identity reference's visual medium",
        }.get(effective_appearance_type, "the primary reference's visual medium")
        if effective_appearance_type == "anime":
            opening = (
                "Create one cohesive soft Kyoto Animation-like anime illustration with a close vertical lower-body outfit composition."
                if is_legs
                else "Create one cohesive soft Kyoto Animation-like anime cosplay illustration."
                if is_cos
                else "Create one cohesive soft Kyoto Animation-like anime illustration in vertical smartphone selfie composition."
            )
        elif is_legs:
            opening = f"Create one natural {style} vertical smartphone outfit record."
        elif is_cos:
            opening = f"Create one natural {style} COS cover photo."
        else:
            opening = f"Create one natural {style} vertical smartphone cover photo."
        lines = [opening]
        if is_legs:
            lines.append("Use the main reference only to keep the everyday outfit and natural proportions consistent; the complete person is outside the crop.")
        elif is_cos:
            lines.extend([
                COS_IDENTITY_CONTRACT_EN if has_reference_image else "Use the character settings as the stable identity anchor; default to an adult woman when gender is unspecified.",
                "Follow the complete COS action contract below exactly once. Preserve its selected outfit, pose, scene, camera, and single framing without adding another camera or crop choice.",
                rendered_action,
            ])
        else:
            lines.extend([
                "Keep the main subject's identity, gender, face, hair, body proportions, and overall appearance stable; change expression naturally with the scene.",
                "When facing the camera, keep natural focused eye contact unless the request says otherwise.",
            ])
        if has_reference_image:
            lines.append(
                (
                    "Use reference image 1 as the identity anchor. The selected appearance type controls the rendered visual medium; do not copy the reference's medium."
                    if effective_appearance_type != "auto"
                    else "Use reference image 1 as the primary identity and visual-medium anchor; extra references must not replace or override the primary subject's visual medium."
                )
                if not is_legs
                else "Use reference image 1 only for natural proportions and composition; ignore all legwear, socks, and shoes in every reference; later references must not widen the crop."
            )
            if viewpoint_change and not is_legs:
                lines.append(
                    "The requested camera view takes priority over the reference projection. Preserve identity cues, but do not copy the reference head orientation, facial projection, shoulder line, or pose. Reconstruct the face naturally for this view; in profile or three-quarter views, let far-side features be naturally occluded instead of forcing both eyes into view."
                )
        else:
            lines.append("Use the character settings as the stable identity anchor; default to an adult woman when gender is unspecified.")
        if extra_reference_count:
            lines.append(
                "Use extra references only for independent companions or explicitly requested clothing, pose, composition, lighting, or scene. Extra references must not override the primary subject's visual medium."
            )
        # Legs first: action text may contain "不要合影" which must not flip into group mode.
        if is_legs:
            camera_match = re.search(r"【cam:(selfie|first|third)】", str(action))
            camera_kind = str(camera_match.group(1) if camera_match else "selfie")
            wear_match = re.search(r"本次服装搭配(?:已锁定为)?[:：]\s*([^。]+)", str(action))
            selected = ""
            if wear_match:
                selected_source = str(wear_match.group(1)).strip().split("；", 1)[0]
                selected = next((name for name in ("光腿神器", "白丝", "黑丝", "连裤袜") if name in selected_source), "")
            selected_text = {
                "光腿神器": "skin-tone leg-cover styling, continuous along every visible part of the legs",
                "白丝": "opaque white thigh-high stockings, continuous down every visible part of the legs",
                "黑丝": "opaque black thigh-high stockings, continuous down every visible part of the legs",
                "连裤袜": "opaque white pantyhose, continuous from the waist through the toes",
            }.get(selected)
            legwear_line = (
                f"Use only {selected_text}; ignore all legwear, socks, and shoes in the references; avoid ordinary short, crew, or mid-calf socks."
                if selected_text
                else "Use the single legwear option selected by the code; ignore all legwear, socks, and shoes in the references; avoid ordinary short, crew, or mid-calf socks."
            )
            outfit_match = re.search(r"【outfit:([a-z_]+)】", str(action))
            outfit = get_leg_focus_outfit(outfit_match.group(1) if outfit_match else "")
            if outfit:
                outfit_line = (
                    "Use the code-locked lower outfit exactly: "
                    f"{outfit.get('prompt_en', '')}. Do not replace the skirt or shoes."
                )
            else:
                outfit_line = "Follow the user's explicitly specified skirt or shoes exactly."
            lines.append(
                "First-person phone selfie framing: the camera points down from the waist and records a natural close lower-body outfit detail; the phone, arms, and upper body stay outside the frame."
                if camera_kind == "selfie"
                else "Third-person candid outfit photo framing: the photographer stays outside the frame and records a natural close lower-body outfit detail."
            )
            if feet_cropped:
                lines.extend([
                    (
                        "Vertical anime outfit illustration: use a close lower-body everyday clothing composition for one subject; never widen to a half-body or full-body image, and do not use a knee or calf as a fixed crop line."
                        if effective_appearance_type == "anime"
                        else "Casual vertical smartphone outfit record: use a close lower-body everyday clothing composition for one subject; never widen to a half-body or full-body photo, and do not use a knee or calf as a fixed crop line."
                    ),
                    legwear_line,
                    outfit_line,
                    "Keep both legs anatomically continuous and natural. A leg may continue naturally beyond the image edge or be coherently occluded by clothing, furniture, or a foreground object with clear depth and boundaries.",
                    "If a calf or foot is not shown, it must continue beyond the frame or be fully hidden by a solid object. Never let a leg abruptly end at a knee, mid-calf, or ankle; when kneeling, the ankle should transition naturally into the instep or sole before the body, clothing, or real contact hides it, never treat a stocking hem as the end of the calf. Rugs, floors, beds, or sofa surfaces may occlude a limb only with real contact and clear depth, never as an unexplained cutoff.",
                    "Follow the selected seated composition exactly, with stable proportions and a close camera distance.",
                    "Match the lighting to the requested scene; otherwise use soft available light, natural exposure, realistic fabric, and an unretouched everyday smartphone feel. Avoid studio polish, plastic skin, or 3D-rendered surfaces.",
                ])
            else:
                lines.extend([
                    "Single subject only: a natural everyday clothing composition with no second person or background people.",
                    legwear_line,
                    "Keep a close lower-body outfit composition with natural clothing folds, proportions, perspective, and continuous visible limbs.",
                    "Use ordinary smartphone perspective, scene-appropriate ambient light, realistic fabric texture, and an unretouched everyday feel; avoid studio polish, plastic skin, or 3D-rendered surfaces.",
                ])
        elif is_group:
            lines.extend([
                "Turn each referenced person or non-person subject into an independent complete person with clear boundaries; do not leave toys or flat cutouts.",
                "Keep everyone in one coherent vertical half-body scene with natural positions, scale, perspective, and friendly interaction.",
            ])
        else:
            lines.extend([
                "Keep a vertical close half-body everyday photo, with coherent light, color, depth, camera perspective, clothing, and scene relationships.",
                "Keep visible anatomy complete and natural: one left and one right hand or foot, connected limbs, and complete fingers or toes.",
                ])
        pose = re.search(r"【(?:pose|shot):([a-z_]+)】", str(action))
        if pose:
            pose_id = pose.group(1)
            pose_descriptions = {
                "sofa_front_crop": "Sit at the front edge of a sofa with both knees naturally together, lower legs dropping straight down, and both feet resting fully on the floor; keep the waist-down framing stable.",
                "chair_side_crop": "Sit on a wooden chair in a slight three-quarter side view; place one knee a little forward and the other slightly back, with both feet grounded and legs uncrossed.",
                "sofa_cross_crop": "Sit on a sofa with both legs relaxed forward and ankles lightly overlapped while the knees stay aligned; keep the feet and sofa contact natural.",
                "floor_knees_crop": "Sit on a rug with both knees gathered naturally and both lower legs folded behind the body; let the clothing and rug create coherent occlusion.",
                "sofa_occlusion_crop": "Sit supported in the corner of a sofa with both knees bent forward; allow the sofa edge or cushion to hide the feet only with clear depth and continuous limbs.",
                "stool_edge_crop": "Sit on a low stool with both knees forward; let one leg continue naturally beyond the lower image edge while the other drops vertically, without a joint-level crop.",
                "floor_side_kneel_crop": "Use a stable side-kneeling seated pose on the floor with both knees gathered to one side; fold one lower leg forward with the foot grounded and the other naturally behind, keeping continuous limbs and coherent clothing or floor occlusion.",
                "seat_knees_cross_crop": "Recline on a wide seat with both knees raised toward the camera; extend both lower legs forward and let the ankles overlap lightly in the foreground while keeping the seat support and leg anatomy natural.",
                "floor_topdown_cross_crop": "Sit on the floor or a wood floor with both legs naturally crossed and knees and ankles aligned; use a close top-down view that keeps the clothing folds, crossed legs, and ground contact coherent.",
                "first_person_topdown_legs_crop": "Sit comfortably and look down toward your own legs from a natural first-person viewpoint, not from an overhead photographer; extend both legs parallel toward the distance with knees and feet aligned, and keep both complete feet visible.",
                "sit": "Sit naturally on a chair or sofa; let the everyday outfit drape naturally in the close lower-body composition.",
                "sit_crop": "Sit naturally on a chair or sofa; let the everyday outfit drape naturally in the close lower-body composition.",
                "kneel": "Use a natural kneeling pose on a rug or cushion; fold both lower legs naturally behind the body.",
                "kneel_crop": "Use a natural kneeling pose on a rug or cushion; fold both lower legs naturally behind the body or hide them coherently behind the outfit.",
                "side_lie": "Rest comfortably on one side on a bed or sofa; keep the outfit, visible legs, and bedding natural in the close composition.",
                "side_lie_crop": "Rest comfortably on one side on a bed or sofa; keep the outfit, visible legs, and bedding natural in the close composition.",
                "hug_knee": "Use a comfortable seated pose with the knees gathered naturally; keep the outfit and visible legs relaxed in the close composition.",
                "hug_knee_crop": "Use a comfortable seated pose with the knees gathered naturally; keep the outfit and visible legs relaxed in the close composition.",
                "cross_leg": "Use a relaxed seated pose with the clothing naturally crossed or staggered; keep the close lower-body composition stable.",
                "cross_leg_crop": "Use a relaxed seated pose with the clothing naturally crossed or staggered; keep the close lower-body composition stable.",
                "windowsill": "Sit naturally on a windowsill or low cabinet; keep the close lower-body outfit composition stable with soft window light.",
                "windowsill_crop": "Sit naturally on a windowsill or low cabinet; keep the close lower-body outfit composition stable with soft window light.",
                "kneel_up": "Use a slightly raised kneeling pose on a soft cushion; keep the outfit and folded lower legs natural in the close composition.",
                "kneel_front": "Use a front-facing kneeling pose on a rug; keep both lower legs naturally folded behind the body.",
                "floor_fold": "Use a relaxed bent-knee seated pose on a rug or wood floor; keep the close lower-body outfit composition stable.",
                "one_knee_fix": "Use a natural one-knee clothing adjustment pose; keep the close lower-body outfit composition stable.",
                "floor_knees_up_crop": "Use a relaxed seated pose on a rug or wood floor; keep the knees naturally bent and the close lower-body outfit composition stable.",
                "reclined_knees_crop": "Use a relaxed seated pose leaning lightly against a sofa or chair; keep the close lower-body outfit composition natural and stable.",
                "desk_sit_crop": "Sit naturally at a desk; keep the close lower-body outfit composition aligned with the desk edge and clothing details visible.",
                "bed_supine_crop": "Rest comfortably on a bed with the outfit falling naturally; keep the close lower-body composition calm and everyday.",
            }
            lines.append(pose_descriptions.get(pose_id, f"Keep the selected composition tag: {pose_id}."))
        user_line = ""
        if translated_user and translated_user not in rendered_action:
            user_line = f"User request: {translated_user}"
        final_style_contract = {
            "real": "Final visual medium: a cohesive realistic human photograph. Selfie, phone, camera, and COS wording controls composition and content, not a different visual medium.",
            "anime": "Final visual medium: a cohesive 2D anime illustration. Selfie, phone, camera, photo, and COS wording controls framing and content only; never render photorealistically or mix anime and live-action styles.",
        }.get(effective_appearance_type, "")
        return _join(*lines, user_line, final_style_contract)

    user = translated_user or extract_user_prompt(action)
    camera_match = re.search(r"【cam:(selfie|third)】", str(action))
    camera_kind = str(camera_match.group(1) if camera_match else "selfie")
    camera_line_zh = (
        "第一人称手机自拍：手机镜头从腰线向下记录下装局部，手机、手臂和上半身都在画面外。"
        if camera_kind == "selfie"
        else "第三人称摄影照片：拍摄者完全在画面外，镜头只记录腰部以下的下装局部。"
    )
    if is_legs and feet_cropped:
        legs_line = camera_line_zh + "成年人物的得体日常穿搭记录，采用近距离下半身局部构图；展示服装颜色、材质和层次，不刻意强调身体细节。"
    elif is_legs:
        legs_line = camera_line_zh + "成年人物的得体日常穿搭记录，采用近距离下半身局部构图；展示服装颜色、材质和层次，不刻意强调身体细节。"
    else:
        legs_line = ""
    if is_legs:
        photo_type_line = (
            "这是第一人称手机自拍照片。"
            if camera_kind == "selfie"
            else "这是第三人称摄影照片。"
        )
    elif is_cos:
        photo_type_line = "这是 COS 换装成片，构图以动作中的单一相机和构图约束为准。"
    else:
        photo_type_line = "这是自拍/日常照片。"
    identity_line = (
        "参考图只用于保持服装比例与自然体态，完整人物位于画面之外。"
        if is_legs
        else "参考图只锁定主角身份、脸型五官、性别、肤色和身体比例。"
        if is_cos
        else "固定主角身份：脸型五官、发型发色、性别、体态与整体长相保持稳定；表情按本次场景自然变化。"
    )
    eye_line = (
        "COS 套装或用户明确要求的歪头、仰头、低头、闭眼或夸张表情应保留；"
        "未指定时自然参考形象图，头部、视线和表情保持连贯；优先保留脸型轮廓、五官比例、眼睛和嘴唇轮廓，面部边缘清晰、肤色自然。"
        if is_cos
        else "" if is_legs else "正对镜头时自然看向镜头，眼神有焦点。"
    )
    leg_crop_line = (
        "严格近距离构图：保持自然的下半身服装局部，不扩展为半身或全身，不把膝关节或小腿作为固定裁切线。"
        "腿部必须连续、自然并符合真实人体结构；画面边缘可以自然裁出腿部，衣物、家具或前景也可以按明确的前后关系合理遮挡。"
        "若小腿或脚不展示，必须自然延伸到画面外，或被边界清楚的实体物体完整遮挡；禁止在膝关节、小腿中段或脚踝附近突然终止；跪坐时脚踝应自然过渡到脚背或脚底，再由身体、衣摆或真实接触关系遮挡，不能把袜筒下缘直接当作小腿终点；地毯、床面或沙发面只有在真实接触和清晰前后关系下才可遮挡肢体，不能无缘无故吞没可见腿部。"
        if is_legs
        else ""
    )
    outfit_match = re.search(r"【outfit:([a-z_]+)】", str(action))
    outfit = get_leg_focus_outfit(outfit_match.group(1) if outfit_match else "")
    leg_outfit_line = (
        f"代码已锁定下装服饰组合：{outfit.get('skirt', '')}、{outfit.get('shoes', '')}；袜装严格使用本次唯一腿部穿搭。"
        if outfit
        else "裙装或鞋履按用户明确指定内容执行。"
        if is_legs and outfit_match
        else ""
    )
    if is_legs and effective_appearance_type == "anime":
        photo_style_line = (
            "动漫插画质感：保持统一的柔光京阿尼风格；用手机近距离取景的透视与构图记录服装，光线服从用户指定场景，未指定时使用柔和自然光或环境光。"
        )
    elif is_legs and effective_appearance_type == "real":
        photo_style_line = (
            "真人摄影质感：像普通手机竖屏近距离记录服装；光线服从用户指定场景，未指定时使用柔和自然光或环境光，"
            "保留真实布料厚度、细小褶皱和自然曝光变化；避免棚拍精修、塑料材质、插画感、3D渲染感和过度虚化。"
        )
    elif is_cos:
        photo_style_line = (
            "真人摄影质感：像自然拍到的 COS 成片；使用窗光或房间环境光，"
            "保留自然曝光变化、真实布料厚度与细小褶皱、轻微皮肤纹理和接触阴影；避免棚拍精修、塑料皮肤、插画感、3D渲染感和过度虚化。"
            if effective_appearance_type == "real" else ""
        )
    else:
        photo_style_line = (
            "真人摄影质感：像普通手机竖屏近景随手拍到的日常照片；使用窗光或房间环境光，"
            "保留自然曝光变化、真实布料厚度与细小褶皱、轻微皮肤纹理和接触阴影；避免棚拍精修、塑料皮肤、插画感、3D渲染感和过度虚化。"
            if effective_appearance_type == "real" else ""
        )
    return _join(
        photo_type_line,
        identity_line,
        eye_line,
        (
            "参考图一只用于服装比例和构图，额外参考图只用于服装、姿势、光线或场景，不得扩大服装取景。"
            if is_legs
            else "参考图一只锁定主角身份和身体比例；额外参考图只用于用户指定的服装、姿势、构图、光线或场景。"
            if is_cos
            else "参考图一只作为主角身份锚点，额外参考图只用于服装、姿势、构图、光线或场景。"
        ) if has_reference_image else "按角色设定保持主角身份稳定，未说明性别时默认成年女性。",
        "" if is_legs else ("合影对象必须落实为独立完整人物，站位自然，边界清晰。" if is_group else ""),
        legs_line,
        leg_outfit_line,
        leg_crop_line,
        photo_style_line,
        "竖屏近景半身，光线、色调、景深和相机透视统一。" if not is_legs else "",
        f"用户要求：{user}" if user else "",
    )


def build_batch_failure_llm_prompt(*, bot_name: str, reason: str, index: int, total: int, done_files: int, will_continue: bool) -> str:
    continuation = "还会继续尝试后面的图片" if will_continue else "后面的图片这次先不继续了"
    return _join(
        f"你是{str(bot_name or '助手')}，请替用户写一句自然、柔和的简体中文说明。",
        f"一次多张图片请求中，第 {index}/{total} 张未完成；已经得到 {done_files} 张。",
        f"确定原因只有：{str(reason or '这张没有顺利完成')[:160]}。{continuation}。",
        "只输出一句 12-45 字的话，不要用不确定猜测，不要罗列多个原因，不要提工具、模型、提示词、配置、审核、任务或内部流程。",
    )
