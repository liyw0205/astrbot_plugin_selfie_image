"""Central prompt templates shared by image-generation paths."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..cos.leg_focus import (
    LEGFOCUS_POOL_REFERENCE_PROMPTS,
    LEGFOCUS_REFERENCE_RULES,
    ensure_leg_focus_action,
    get_leg_focus_outfit,
    has_leg_face_context,
    has_leg_focus_contract,
    is_leg_calf_crop_action,
    leg_focus_appearance_prompt,
)
from .group_prompt_pool import has_group_composition_contract


COS_IDENTITY_CONTRACT_ZH = (
    "以身份参考图为本人身份来源：保留本人脸型轮廓、五官比例、眼睛与嘴唇轮廓、性别、年龄感和肤色。\n"
    "COS 只改变服装、配饰、角色假发/发型和妆容，不把角色原画的脸当作新身份。\n"
    "除非用户明确要求，不固定角色瞳色、脸型或五官比例。"
)

COS_COVERED_EYES_IDENTITY_CONTRACT_ZH = (
    "以身份参考图为本人身份来源：保留本人脸型轮廓、鼻唇轮廓、性别、年龄感和肤色；"
    "眼睛服从本次套装的眼罩遮挡规则，不要求复现参考图中可见的双眼。\n"
    "COS 只改变服装、配饰、角色假发/发型和妆容，不把角色原画的脸当作新身份。\n"
    "除非用户明确要求，不固定角色瞳色、脸型或五官比例。"
)

COS_HAIR_OVERRIDE_CONTRACT_ZH = (
    "套装明示的发型、发色优先于身份参考图的原头发，不以是否出现“假发”字样为条件。"
    "指定单一发色时，发际线、发根、内层、刘海、鬓发和发尾均为该色，不在新发型下面叠留参考图原色刘海或鬓发。"
    "挑染、拼色或渐变按明示的颜色及分布呈现；局部异色仅用于对应发束或区域，其余头发遵循本次主发色，不把局部配色扩大为整片异色刘海。"
    "指定假发时，参考图原发完整收在假发内，可见发际线、刘海和鬓发均属于同一顶假发。"
    "指定发型轮廓时，按指定内容替换参考图中对应的刘海、鬓发、分缝、长度或束发；未写明的发型细节自然补全。"
    "仅指定局部挑染而未指定底色时，底色沿用身份参考图；完全未指定发色时也沿用参考图发色。未指定发型时保留与本次设定兼容的自然发型，不按角色名补造未写明的造型。"
    "头饰需贴合头部并与刘海、发束协调，不穿插、悬浮或与头发融合。"
)

COS_IDENTITY_CONTRACT_EN = (
    "Use the identity reference image as the same person's identity anchor: preserve the person's face outline, "
    "facial feature proportions, eye and lip contours, gender, apparent age, and skin tone.\n"
    "COS changes only clothing, accessories, the character wig or hairstyle, and makeup; do not replace the person "
    "with the face of the original character.\n"
    "Unless the user explicitly requests it, do not lock the character's eye color, face shape, or facial feature proportions."
)

COS_COVERED_EYES_IDENTITY_CONTRACT_EN = (
    "Use the identity reference image as the same person's identity anchor: preserve the person's face outline, "
    "nose and lip contours, gender, apparent age, and skin tone; the eyes follow the costume's blindfold rule "
    "and do not need to remain visibly reproduced from the reference.\n"
    "COS changes only clothing, accessories, the character wig or hairstyle, and makeup; do not replace the person "
    "with the face of the original character.\n"
    "Unless the user explicitly requests it, do not lock the character's eye color, face shape, or facial feature proportions."
)

COS_HAIR_OVERRIDE_CONTRACT_EN = (
    "The costume's explicit hairstyle and hair colors override the identity reference's original hair, whether or not the word wig appears. "
    "For a single specified color, use that color throughout the visible hairline, roots, inner layers, bangs, side locks, and ends; do not layer new hair over bangs or side locks in the reference's original color. "
    "Use the stated colors and distribution for highlights, color blocking, or gradients. Local accents apply only to the corresponding strands or regions; the remaining hair follows the chosen base color. Do not expand a local accent into an entire contrasting fringe. "
    "When a wig is specified, tuck all reference hair inside it; the visible hairline, bangs, and side locks belong to the same wig. "
    "Replace only the hairstyle features that are specified, such as bangs, side locks, parting, length, or tied sections. Complete unspecified hairstyle details naturally. "
    "If only local highlights are specified without a base color, retain the identity reference's base color; retain its hair color as well when no color is specified at all. Keep unspecified hairstyle features compatible with the chosen look; do not invent unlisted hair details from the character name. "
    "Fit headwear naturally without floating, intersecting, or merging it with the hair."
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
    """Keep the resolved identity and daily context during EN conversion."""
    labels = {
        "今日穿搭": "Today's outfit",
        "当前时间段": "Current time period",
        "当前状态": "Current state",
        "当前心情": "Current mood",
        "今日日程基调": "Today's schedule theme",
        "当前日程事件": "Current schedule event",
        "当前日程场景": "Current schedule scene",
        "今日时令 / 节日氛围": "Today's seasonal or holiday atmosphere",
    }
    lines = []
    source = str(source_prompt or "")
    character = re.search(r"^角色设定：([\s\S]*?)\n【角色设定结束】", source, re.M)
    identity = f"Character settings from AstrBot:\n{character.group(1).strip()}" if character else ""
    if character:
        source = source[:character.start()] + source[character.end():]
    for source_label, english_label in labels.items():
        match = re.search(rf"^{re.escape(source_label)}：(.+)$", source, re.M)
        if match:
            lines.append(f"{english_label}: {match.group(1).strip()}")
    if re.search(r"^腿部日程约束：", source, re.M):
        lines.append(
            "For leg-focused images, daily context is only a time, scene, and atmosphere reference. "
            "It must not override the locked pose, camera, framing, or complete lower outfit, "
            "and must not copy the schedule's original action or outfit change."
        )
    if not lines:
        return _join(identity, english_prompt)
    return _join(identity, english_prompt, "Daily context (follow unless the user explicitly overrides it):", *lines)


def extract_user_prompt(action: str) -> str:
    """Extract free-form user text from a wrapped selfie action."""
    text = str(action or "").strip()
    match = re.search(r"(?:用户补充要求优先|用户补充要求|用户指定内容|额外要求|用户要求)[:：]\s*(.+)", text, re.S)
    if match:
        value = match.group(1)
        value = re.sub(r"\s*【(?:pose|shot|cos|cam|legs|legs_face|leg_view|leg_scene|leg_support|wear|outfit|frame):[a-z0-9_]+】\s*", " ", value)
        value = re.sub(r"\s*【(?:姿势池|场景池)·[^】]+】\s*", " ", value)
        return re.sub(r"\s+", " ", value).strip(" 。")
    if any(marker in text for marker in ("【自拍 / 看看模式】", "【他拍 / 看看你模式】", "【自拍 / 看看COS模式】", "【他拍 / 看看COS模式】", "【第一视角 / 看看COS模式】", "【合影 / 合照模式】", "看看腿。", "【legs:outfit】", "成年人物日常下半身穿搭展示", "【唯一姿势·不可混用】")):
        return ""
    return text


def extract_generated_action_contract(action: str) -> str:
    """Keep the generated action body separate from its user supplement."""
    text = str(action or "").strip()
    match = re.search(r"\s*(?:用户补充要求优先|用户补充要求|用户指定内容)[:：]", text)
    if match:
        return text[: match.start()].rstrip(" 。")
    return text


def explicit_selfie_action_mode(action: str) -> str:
    """Generated command markers take precedence over words inside descriptions."""
    raw = str(action or "")
    for marker, mode in (
        ("【自拍 / 看看模式】", "selfie"),
        ("【他拍 / 看看你模式】", "third"),
        ("【shot:crop_waist】", "selfie"),
        ("【legs:outfit】", "legs"),
        ("【cos:", "cos"),
        ("【cos：", "cos"),
    ):
        if marker in raw:
            return mode
    if has_group_composition_contract(raw):
        return "group"
    return ""


def has_daily_staging_contract(action: str) -> bool:
    return explicit_selfie_action_mode(action) in {"selfie", "third"} and bool(
        re.search(r"【shot:[a-z_]+】", str(action or ""))
    )


def has_explicit_gaze_request(text: str) -> bool:
    return bool(re.search(
        r"视线|目光|眼神|闭眼|闭目|眯眼|看向|望向|看着|看镜头|看彼此|看别处|不看|"
        r"\b(?:gaze|looking|look at|eyes closed|eye contact)\b",
        str(text or ""), re.I,
    ))


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
    mode = explicit_selfie_action_mode(raw_action)
    raw_is_cos = mode == "cos" or (not mode and "看看cos" in raw_action.lower())
    raw_is_group = mode == "group" or (not mode and not raw_is_cos and any(
        marker in raw_action for marker in ("合影", "合照", "同框", "多人合照", "group photo", "group_photo")
    ))
    raw_is_legs = mode == "legs" or (not mode and not raw_is_cos and not raw_is_group and any(
        marker in raw_action
        for marker in ("看看腿", "腿部", "下半身穿搭", "穿搭展示", "【legs:outfit】", "【pose:")
    ))
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
    is_cos = raw_is_cos
    covered_eyes = "【cos_eye:full_cover】" in str(action).lower()
    is_group = raw_is_group
    is_legs = raw_is_legs
    leg_reference_rule = ""
    if is_legs and has_reference_image and not any(
        rule in rendered_action for rule in LEGFOCUS_REFERENCE_RULES.values()
    ):
        leg_reference_rule = LEGFOCUS_REFERENCE_RULES["en" if language == "en" else "zh"]
    leg_pool_reference_rule = ""
    outfit_match = re.search(r"【outfit:([a-z_]+)】", str(action))
    if (
        is_legs
        and has_reference_image
        and outfit_match
        and get_leg_focus_outfit(outfit_match.group(1))
    ):
        leg_pool_reference_rule = LEGFOCUS_POOL_REFERENCE_PROMPTS["en" if language == "en" else "zh"]
    is_group_contract = is_group and has_group_composition_contract(str(action))
    feet_cropped = is_legs and is_leg_calf_crop_action(str(action))
    leg_face_context = is_legs and feet_cropped and has_leg_face_context(str(action))
    if language == "en":
        style = {
            "real": "realistic photographic",
            "anime": "soft Kyoto Animation-like anime with a finer face and ethereal atmosphere",
            "auto": "the primary identity reference's visual medium",
        }.get(effective_appearance_type, "the primary reference's visual medium")
        if effective_appearance_type == "anime":
            opening = (
                "Create one cohesive soft Kyoto Animation-like anime illustration with a natural vertical legwear-focused outfit composition."
                if is_legs and feet_cropped
                else "Create one cohesive soft Kyoto Animation-like anime illustration with a complete vertical outfit composition."
                if is_legs
                else "Create one cohesive soft Kyoto Animation-like anime cosplay illustration."
                if is_cos
                else "Create one cohesive soft Kyoto Animation-like vertical group-photo illustration with framing set by the selected composition contract."
                if is_group_contract
                else "Create one cohesive soft Kyoto Animation-like anime illustration in vertical smartphone selfie composition."
            )
        elif is_legs:
            opening = (
                f"Create one natural {style} vertical legwear-focused outfit photograph."
                if feet_cropped
                else f"Create one natural {style} vertical full-length outfit photograph."
            )
        elif is_cos:
            opening = f"Create one natural {style} COS cover photo."
        elif is_group_contract:
            opening = f"Create one natural {style} vertical group photograph following the selected composition contract."
        else:
            opening = f"Create one natural {style} vertical smartphone cover photo."
        lines = [opening]
        if has_daily_staging_contract(raw_action):
            lines = [
                f"Create one cohesive {style} vertical everyday image following the selected capture contract.",
                "Preserve the main subject's identity and the resolved daily outfit. The contract below controls camera, framing, pose, gaze, scene, and lighting; do not add another shot or force eye contact.",
                "Use the primary identity reference for the same person; extra references only supply explicitly requested details."
                if has_reference_image else "Use the character settings as the identity anchor; default to an adult woman when gender is unspecified.",
                rendered_action,
            ]
            if translated_user and translated_user not in rendered_action:
                lines.append(f"User request: {translated_user}")
            return _join(*lines)
        if is_legs:
            lines = [
                leg_reference_rule if has_reference_image
                else "Use the character settings as the subject's identity anchor.",
                leg_pool_reference_rule,
                rendered_action,
                leg_focus_appearance_prompt(effective_appearance_type, "en", rendered_action),
            ]
            if extra_reference_count:
                lines.append("Use extra references only for the specific clothing or visual details requested; do not let them override the selected camera or pose.")
            if translated_user and translated_user not in rendered_action:
                lines.append(f"User request: {translated_user}")
            return _join(*lines)
        if is_cos:
            lines.extend([
                COS_COVERED_EYES_IDENTITY_CONTRACT_EN
                if has_reference_image and covered_eyes
                else COS_IDENTITY_CONTRACT_EN
                if has_reference_image
                else "Use the character settings as the stable identity anchor; default to an adult woman when gender is unspecified.",
                COS_HAIR_OVERRIDE_CONTRACT_EN,
                "Follow the complete COS action contract below exactly once. Preserve its selected outfit, pose, scene, camera, and single framing without adding another camera or crop choice.",
                rendered_action,
            ])
        elif not is_group_contract:
            lines.extend([
                "Keep the main subject's identity, gender, face, hair, body proportions, and overall appearance stable; change expression naturally with the scene.",
                "When facing the camera, keep natural focused eye contact unless the request says otherwise.",
            ])
        if has_reference_image and not (is_cos or is_group_contract):
            lines.append(
                (
                    "Use reference image 1 as the identity anchor. The selected appearance type controls the rendered visual medium; do not copy the reference's medium."
                    if effective_appearance_type != "auto"
                    else "Use reference image 1 as the primary identity and visual-medium anchor; extra references must not replace or override the primary subject's visual medium."
                )
            )
            if viewpoint_change and not is_legs:
                lines.append(
                    "The requested camera view takes priority over the reference projection. Preserve identity cues, but do not copy the reference head orientation, facial projection, shoulder line, or pose. Reconstruct the face naturally for this view; in profile or three-quarter views, let far-side features be naturally occluded instead of forcing both eyes into view."
                )
        elif not is_cos and not is_group_contract:
            lines.append("Use the character settings as the stable identity anchor; default to an adult woman when gender is unspecified.")
        if extra_reference_count and not (is_cos or is_group_contract):
            lines.append(
                "Use extra references only for independent companions or explicitly requested clothing, pose, composition, lighting, or scene. Extra references must not override the primary subject's visual medium."
            )
        if is_group:
            if is_group_contract:
                lines.extend([
                    "Follow the selected group composition contract below as the sole source for people, scene, pose, gaze, and framing.",
                    rendered_action,
                ])
            else:
                lines.extend([
                    "Turn each referenced person or non-person subject into an independent complete person with clear boundaries; do not leave toys or flat cutouts.",
                    "Keep everyone in one coherent vertical half-body scene with natural positions, scale, perspective, and friendly interaction.",
                ])
        elif not is_cos:
            lines.extend([
                "Keep a vertical close half-body everyday photo, with coherent light, color, depth, camera perspective, clothing, and scene relationships.",
                "Keep visible anatomy complete and natural: one left and one right hand or foot, connected limbs, and complete fingers or toes.",
            ])
        pose = re.search(r"【(?:pose|shot):([a-z_]+)】", str(action))
        if pose and not is_group_contract:
            pose_id = pose.group(1)
            pose_descriptions = {
                "sofa_front_crop": "Sit naturally at the front edge of a sofa with both feet grounded.",
                "chair_side_crop": "Sit naturally on a wooden chair in a slight three-quarter view with both feet grounded.",
                "sofa_cross_crop": "Use a relaxed seated pose on a sofa without crossing the legs.",
                "floor_knees_crop": "Stand naturally by a window with one foot half a step forward.",
                "sofa_occlusion_crop": "Sit comfortably at the side of a sofa with both feet grounded and unobstructed.",
                "stool_edge_crop": "Sit naturally on a stable low stool with both feet grounded.",
                "floor_side_kneel_crop": "Walk at an easy pace on a flat path with balanced posture.",
                "seat_knees_cross_crop": "Stand naturally with feet about shoulder-width apart and arms relaxed.",
                "floor_topdown_cross_crop": "Follow the explicitly requested overhead angle with a stable seated pose.",
                "first_person_topdown_legs_crop": "Use the explicitly requested first-person downward view with both legs naturally aligned.",
                "staggered_stair_step_crop": "Stand on broad steps with one foot on a lower step and the other on the upper step; keep a stable balance and eye-level lower-body framing.",
                "cross_step_snapshot_crop": "Capture a light walking step with the front foot crossing naturally ahead of the weight-bearing leg; keep ankles aligned and the lower-body view sharp.",
                "turn_and_settle_crop": "Show a gentle half-turn ending in a stable stance, with one foot lightly touching down and the other bearing weight; keep the lower-body framing natural.",
                "bench_side_sit_crop": "Sit at one end of a bench in a relaxed side-facing pose, with one heel grounded and the other leg comfortably bent; keep the lower outfit and feet unobstructed.",
                "toe_touch_step_crop": "Stand beside a broad low step with one foot grounded and the other toe lightly touching the first step; keep a stable eye-level lower-body view.",
                "back_staggered_step_crop": "Show the subject from behind with feet in a comfortable staggered stance; keep shoulders, hips, and head facing away without looking back.",
                "sit": "Sit naturally on a chair or sofa with both feet grounded.",
                "sit_crop": "Sit naturally on a chair or sofa with both feet grounded.",
                "kneel": "Use a stable seated pose on a chair or sofa.",
                "kneel_crop": "Use a stable seated pose on a chair or sofa.",
                "side_lie": "Use a relaxed seated pose with balanced posture.",
                "side_lie_crop": "Use a relaxed seated pose with balanced posture.",
                "hug_knee": "Use a comfortable seated pose with feet resting naturally.",
                "hug_knee_crop": "Use a comfortable seated pose with feet resting naturally.",
                "cross_leg": "Use a relaxed seated pose with feet resting naturally.",
                "cross_leg_crop": "Use a relaxed seated pose with feet resting naturally.",
                "windowsill": "Sit on a stable chair beside a window.",
                "windowsill_crop": "Sit on a stable chair beside a window.",
                "kneel_up": "Use a relaxed seated pose on a stable chair.",
                "kneel_front": "Use a relaxed seated pose on a stable chair.",
                "floor_fold": "Use a relaxed seated pose with feet resting naturally.",
                "one_knee_fix": "Stand naturally with one foot half a step forward.",
                "floor_knees_up_crop": "Use a relaxed seated pose on a stable chair.",
                "reclined_knees_crop": "Sit comfortably while keeping the torso upright.",
                "desk_sit_crop": "Sit naturally on a chair beside a desk.",
                "bed_supine_crop": "Use a relaxed seated pose on a stable chair.",
            }
            pose_text = pose_descriptions.get(pose_id)
            lines.append(pose_text or f"Keep the selected composition tag: {pose_id}.")
        user_line = ""
        if translated_user and translated_user not in rendered_action:
            user_line = f"User request: {translated_user}"
        final_style_contract = {
            "real": "Final visual medium: a cohesive realistic human photograph. Selfie, phone, camera, and COS wording controls composition and content, not a different visual medium.",
            "anime": "Final visual medium: a cohesive 2D anime illustration. Selfie, phone, camera, photo, and COS wording controls framing and content only; never render photorealistically or mix anime and live-action styles.",
        }.get(effective_appearance_type, "") if not is_group_contract else ""
        return _join(*lines, user_line, final_style_contract)

    user = translated_user or extract_user_prompt(action)
    if is_legs:
        lines = [
            leg_reference_rule if has_reference_image
            else "按角色设定保持主角身份和自然体态。",
            leg_pool_reference_rule,
            rendered_action,
            leg_focus_appearance_prompt(effective_appearance_type, "zh", rendered_action),
        ]
        if user and user not in rendered_action:
            lines.append(f"用户补充要求：{user}")
        return _join(*lines)
    camera_match = re.search(r"【cam:(selfie|third)】", str(action))
    camera_kind = str(camera_match.group(1) if camera_match else "selfie")
    wear_match = re.search(r"【wear:(daily|custom|white|black|bare|barefoot|pantyhose)】", str(action))
    selected_wear = str(wear_match.group(1) if wear_match else "daily")
    foot_target_zh = "足部" if selected_wear == "barefoot" else "鞋履"
    lower_outfit_display_zh = "下装和赤足双脚" if selected_wear == "barefoot" else "下装、袜装与鞋履"
    foot_visibility_zh = "双脚自然赤足并清晰可见" if selected_wear == "barefoot" else "日常袜装与鞋履清楚可见"
    camera_line_zh = (
        "第一人称腿部展示构图：本人视线自然朝向腿部，使用普通手机焦段与正常拍摄距离；近侧膝部和大腿不贴近镜头，避免夸张近大远小。"
        if feet_cropped and camera_kind == "selfie"
        else f"全身镜前自拍：人物从头部到{foot_target_zh}完整入镜，手机自然持握且不遮挡主要穿搭。"
        if camera_kind == "selfie"
        else "第三人称腿部穿搭照片：摄影者完全在画面外，以普通焦段和适中距离取景。"
        if feet_cropped
        else f"第三人称全身日常照片：拍摄者完全在画面外，人物从头部到{foot_target_zh}完整入镜。"
    )
    if is_legs and feet_cropped:
        legs_line = camera_line_zh + (
            "若头部进入画面，必须同时有与腿部姿势配套的视线、表情和上肢动作；不要把脸单独悬在腿部构图上。"
            if leg_face_context
            else "画面上缘裁过肩部，头部、脸部和肩部不入镜，胸部可自然保留；不通过镜面或倒影补出脸。"
        ) + f"成年人物腿部穿搭展示：从肩部裁切线上缘至{foot_target_zh}自然入镜，{foot_visibility_zh}，保持服装完整、比例自然。"
    elif is_legs:
        legs_line = camera_line_zh + f"成年人物全身穿搭照片，头部、上衣、下装与{foot_target_zh}完整入镜，以正常拍摄距离自然呈现整体穿搭。"
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
        "参考图用于保持主角身份和自然体态，服装以本次锁定搭配为准。"
        if is_legs
        else "参考图只锁定主角身份、脸型五官、性别、肤色和身体比例。"
        if is_cos
        else "固定主角身份：脸型五官、发型发色、性别、体态与整体长相保持稳定；表情按本次场景自然变化。"
    )
    eye_line = (
        "本次套装的眼罩遮挡双眼，材质与纹样按套装描述保留，不要求复现参考图中可见的双眼；"
        "头部、视线和表情按已选姿势保持连贯，鼻唇轮廓和眼罩边缘清晰。"
        if is_cos and covered_eyes
        else (
            "COS 套装或用户明确要求的歪头、仰头、低头、闭眼或夸张表情应保留；"
            "未指定时自然参考形象图，头部、视线和表情保持连贯；优先保留脸型轮廓、五官比例、眼睛和嘴唇轮廓，面部边缘清晰、肤色自然。"
            if is_cos
            else "" if is_legs else "正对镜头时自然看向镜头，眼神有焦点。"
        )
    )
    leg_crop_line = (
        f"以腰线至{foot_target_zh}的自然构图展示{lower_outfit_display_zh}，肢体比例自然连续，避免家具遮挡足部。"
        if is_legs and feet_cropped
        else ""
    )
    outfit_match = re.search(r"【outfit:([a-z_]+)】", str(action))
    outfit = get_leg_focus_outfit(outfit_match.group(1) if outfit_match else "")
    if outfit:
        outfit_parts = [str(outfit.get("bottoms") or outfit.get("skirt") or "").strip()]
        if outfit.get("shoes"):
            outfit_parts.append(str(outfit["shoes"]).strip())
        leg_outfit_line = f"代码已锁定下装服饰组合：{'、'.join(part for part in outfit_parts if part)}。"
        if selected_wear == "barefoot":
            leg_outfit_line += "双脚保持自然赤足，不添加袜子或鞋履。"
    else:
        leg_outfit_line = (
            "裙装或鞋履按用户明确指定内容执行。"
            if is_legs and outfit_match
            else ""
        )
    if is_legs and effective_appearance_type == "anime":
        photo_style_line = (
            "动漫插画质感：保持统一的柔光京阿尼风格；"
            + (f"以自然腰线至{foot_target_zh}构图清楚呈现{lower_outfit_display_zh}，" if feet_cropped else "以自然全身构图清楚呈现整体服装，")
            + "光线服从用户指定场景，未指定时使用柔和自然光或环境光。"
        )
    elif is_legs and effective_appearance_type == "real":
        photo_style_line = (
            "真人摄影质感：像普通相机拍摄的竖屏"
            + (f"腰线至{foot_target_zh}的日常穿搭照片；" if feet_cropped else "全身日常照片；")
            + "光线服从用户指定场景，未指定时使用柔和自然光或环境光，"
            "保留真实布料厚度、细小褶皱和自然曝光变化；避免棚拍精修、塑料材质、插画感、3D渲染感和过度虚化。"
        )
    elif is_cos:
        photo_style_line = (
            "真人摄影质感：像自然拍到的 COS 成片；光照遵循本次 COS 场景与用户要求，"
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
        COS_HAIR_OVERRIDE_CONTRACT_ZH if is_cos else "",
        (
            "参考图用于保持主角身份和自然体态；额外参考图仅提供用户指定的服装、姿势、构图、光线或场景。"
            if is_legs and not feet_cropped
            else (
                f"主角形象参考图用于保持身份与自然体态，按当前选定的腰线至{foot_target_zh}构图呈现"
                + ("下装和自然赤足双脚，不添加袜子或鞋履；" if selected_wear == "barefoot" else "袜装穿搭；")
                + "默认不露头，除非动作合同明确允许配套露脸。"
            )
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


def build_batch_failure_llm_prompt(*, reason: str, index: int, total: int, done_files: int, will_continue: bool) -> str:
    continuation = "还会继续尝试后面的图片" if will_continue else "后面的图片这次先不继续了"
    return _join(
        "请给用户写一句自然、柔和的简体中文说明，延续当前对话身份。",
        f"一次多张图片请求中，第 {index}/{total} 张未完成；已经得到 {done_files} 张。",
        f"确定原因只有：{str(reason or '这张没有顺利完成')[:160]}。{continuation}。",
        "只输出一句 12-45 字的话，不要用不确定猜测，不要罗列多个原因，不要提工具、模型、提示词、配置、审核、任务或内部流程。",
    )
