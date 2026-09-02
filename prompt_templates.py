"""Central prompt templates shared by image-generation paths."""

from __future__ import annotations

import re
from dataclasses import dataclass


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


def extract_user_prompt(action: str) -> str:
    """Extract free-form user text from a wrapped selfie action."""
    text = str(action or "").strip()
    match = re.search(r"(?:用户补充要求优先|用户补充要求|额外要求|用户要求)[:：]\s*(.+)", text, re.S)
    if match:
        value = match.group(1)
        value = re.sub(r"\s*【(?:pose|shot|cos|cam|legs|wear):[a-z0-9_]+】\s*", " ", value)
        return re.sub(r"\s+", " ", value).strip(" 。")
    if any(marker in text for marker in ("【自拍 / 看看模式】", "【他拍 / 看看你模式】", "【自拍 / 看看COS模式】", "【他拍 / 看看COS模式】", "【合影 / 合照模式】", "看看腿。", "【legs:outfit】", "成年人物日常下半身穿搭展示", "【唯一姿势·不可混用】")):
        return ""
    return text


def build_selfie_builtin_prompt(
    action: str,
    *,
    language: str = "zh",
    has_reference_image: bool = False,
    extra_reference_count: int = 0,
    appearance_type: str = "auto",
    user_text: str = "",
) -> str:
    """Build a compact central built-in prompt; user text is appended separately."""
    translated_user = str(user_text or "").strip()
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
            "real": "realistic",
            "anime": "soft Kyoto Animation-like anime with a finer face and ethereal atmosphere",
        }.get(str(appearance_type), "visually consistent")
        if is_legs:
            opening = f"Create one natural {style} vertical smartphone outfit record."
        elif is_cos:
            opening = f"Create one natural {style} vertical COS cover photo."
        else:
            opening = f"Create one natural {style} vertical smartphone cover photo."
        lines = [opening]
        if is_legs:
            lines.append("Use the main reference only to keep the everyday outfit and natural proportions consistent; the complete person is outside the crop.")
        else:
            lines.extend([
                "Keep the main subject's identity, gender, face, hair, body proportions, and overall appearance stable; change expression naturally with the scene.",
                "When facing the camera, keep natural focused eye contact unless the request says otherwise.",
            ])
        if has_reference_image:
            lines.append(
                "Use reference image 1 only for natural proportions and composition; ignore all legwear, socks, and shoes in every reference; later references must not widen the crop."
                if is_legs
                else "Use reference image 1 only as the main identity anchor; later references must not replace the main subject."
            )
        else:
            lines.append("Use the character settings as the stable identity anchor; default to an adult woman when gender is unspecified.")
        if extra_reference_count:
            lines.append("Use extra references for clothing, pose, composition, lighting, or scene only.")
        # Legs first: action text may contain "不要合影" which must not flip into group mode.
        if is_legs:
            camera_match = re.search(r"【cam:(selfie|third)】", str(action))
            camera_kind = str(camera_match.group(1) if camera_match else "selfie")
            wear_match = re.search(r"本次服装搭配(?:已锁定为)?[:：]\s*([^。]+)", str(action))
            selected = ""
            if wear_match:
                selected_source = str(wear_match.group(1)).strip().split("；", 1)[0]
                selected = next((name for name in ("光腿神器", "白丝", "黑丝") if name in selected_source), "")
            selected_text = {
                "光腿神器": "skin-tone leg-cover styling, continuous along every visible part of the legs",
                "白丝": "opaque white thigh-high stockings, continuous down every visible part of the legs",
                "黑丝": "opaque black thigh-high stockings, continuous down every visible part of the legs",
            }.get(selected)
            legwear_line = (
                f"Use only {selected_text}; ignore all legwear, socks, and shoes in the references; avoid ordinary short, crew, or mid-calf socks."
                if selected_text
                else "Use exactly one selected legwear option: skin-tone leg-cover styling, opaque white thigh-high stockings, or opaque black thigh-high stockings; ignore all legwear, socks, and shoes in the references; avoid ordinary short, crew, or mid-calf socks."
            )
            lines.append(
                "First-person phone selfie framing: the camera points down from the waist and records a natural close lower-body outfit detail; the phone, arms, and upper body stay outside the frame."
                if camera_kind == "selfie"
                else "Third-person candid outfit photo framing: the photographer stays outside the frame and records a natural close lower-body outfit detail."
            )
            if feet_cropped:
                lines.extend([
                    "Casual indoor vertical smartphone outfit record: use a close lower-body everyday clothing composition for one subject; never widen to a half-body or full-body photo, and do not use a knee or calf as a fixed crop line.",
                    legwear_line,
                    "Keep both legs anatomically continuous and natural. A leg may continue naturally beyond the image edge or be coherently occluded by clothing, furniture, or a foreground object with clear depth and boundaries.",
                    "If a calf or foot is not shown, it must continue beyond the frame or be fully hidden by a solid object. Never let a leg abruptly end at a knee, mid-calf, or ankle; when kneeling, the ankle should transition naturally into the instep or sole before the body, clothing, or real contact hides it, never treat a stocking hem as the end of the calf. Rugs, floors, beds, or sofa surfaces may occlude a limb only with real contact and clear depth, never as an unexplained cutoff.",
                    "Follow the selected seated composition exactly, with stable proportions and a close camera distance.",
                    "Use ordinary room light, natural exposure, realistic fabric and an unretouched everyday smartphone feel; avoid studio polish, plastic skin, or 3D-rendered surfaces.",
                ])
            else:
                lines.extend([
                    "Single subject only: a natural everyday clothing composition with no second person or background people.",
                    legwear_line,
                    "Keep a close lower-body outfit composition with natural clothing folds, proportions, perspective, and continuous visible limbs.",
                    "Use ordinary smartphone perspective, ambient room light, realistic fabric texture, and an unretouched everyday feel; avoid studio polish, plastic skin, or 3D-rendered surfaces.",
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
        return _join(*lines, f"User request: {translated_user}" if translated_user else "")

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
        photo_type_line = "这是竖屏 COS 换装成片。"
    else:
        photo_type_line = "这是自拍/日常照片。"
    identity_line = (
        "参考图只用于保持服装比例与自然体态，完整人物位于画面之外。"
        if is_legs
        else "固定主角身份：脸型五官、发型发色、性别、体态与整体长相保持稳定；表情按本次场景自然变化。"
    )
    eye_line = "" if is_legs else "正对镜头时自然看向镜头，眼神有焦点。"
    leg_crop_line = (
        "严格近距离构图：保持自然的下半身服装局部，不扩展为半身或全身，不把膝关节或小腿作为固定裁切线。"
        "腿部必须连续、自然并符合真实人体结构；画面边缘可以自然裁出腿部，衣物、家具或前景也可以按明确的前后关系合理遮挡。"
        "若小腿或脚不展示，必须自然延伸到画面外，或被边界清楚的实体物体完整遮挡；禁止在膝关节、小腿中段或脚踝附近突然终止；跪坐时脚踝应自然过渡到脚背或脚底，再由身体、衣摆或真实接触关系遮挡，不能把袜筒下缘直接当作小腿终点；地毯、床面或沙发面只有在真实接触和清晰前后关系下才可遮挡肢体，不能无缘无故吞没可见腿部。"
        if is_legs
        else ""
    )
    if is_legs:
        photo_style_line = (
            "真人摄影质感：像普通手机竖屏近距离记录服装；使用窗光或房间环境光，"
            "保留真实布料厚度、细小褶皱和自然曝光变化；避免棚拍精修、塑料材质、插画感、3D渲染感和过度虚化。"
        )
    elif is_cos:
        photo_style_line = (
            "真人摄影质感：像竖屏近景随手拍到的 COS 成片；使用窗光或房间环境光，"
            "保留自然曝光变化、真实布料厚度与细小褶皱、轻微皮肤纹理和接触阴影；避免棚拍精修、塑料皮肤、插画感、3D渲染感和过度虚化。"
            if str(appearance_type) == "real" else ""
        )
    else:
        photo_style_line = (
            "真人摄影质感：像普通手机竖屏近景随手拍到的日常照片；使用窗光或房间环境光，"
            "保留自然曝光变化、真实布料厚度与细小褶皱、轻微皮肤纹理和接触阴影；避免棚拍精修、塑料皮肤、插画感、3D渲染感和过度虚化。"
            if str(appearance_type) == "real" else ""
        )
    return _join(
        photo_type_line,
        identity_line,
        eye_line,
        (
            "参考图一只用于服装比例和构图，额外参考图只用于服装、姿势、光线或场景，不得扩大服装取景。"
            if is_legs
            else "参考图一只作为主角身份锚点，额外参考图只用于服装、姿势、构图、光线或场景。"
        ) if has_reference_image else "按角色设定保持主角身份稳定，未说明性别时默认成年女性。",
        "" if is_legs else ("合影对象必须落实为独立完整人物，站位自然，边界清晰。" if is_group else ""),
        legs_line,
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
