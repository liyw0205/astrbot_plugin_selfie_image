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
    # Look-legs always hides feet by default.
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
        opening = (
            f"Create one natural {style} vertical smartphone outfit record."
            if is_legs
            else f"Create one natural {style} vertical smartphone cover photo."
        )
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
                "Use reference image 1 only for outfit proportions and composition; later references must not widen the crop."
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
            lines.append(
                "First-person phone selfie: the subject holds the phone and records an everyday outfit detail from above; use natural waistline-to-knee framing."
                if camera_kind == "selfie"
                else "Third-person candid outfit photo: a friend outside the frame photographs the subject's everyday outfit detail naturally; use waistline-to-knee framing."
            )
            if feet_cropped:
                lines.extend([
                    "Casual indoor vertical smartphone outfit record: strict close crop of one subject's everyday clothing only; the upper edge is around the waistline and the lower edge is around the knees.",
                    "Keep the complete person outside the frame and never widen to a half-body or full-body photo.",
                    "Use exactly one selected legwear option: skin-tone leg-cover styling, opaque white thigh-high stockings, or opaque black thigh-high stockings; keep continuous coverage from the upper thigh to the knee. Never generate crew socks, mid-calf socks, ankle socks, athletic socks, or ordinary cotton socks.",
                    "Follow the selected seated composition exactly, with stable proportions and a close camera distance.",
                    "Use ordinary window or room light, natural exposure, realistic fabric thickness and small clothing wrinkles; avoid studio polish, plastic skin, illustration, or 3D-rendered surfaces.",
                    "Keep an everyday indoor perspective and an unretouched candid-photo feel.",
                ])
            else:
                lines.extend([
                    "Single subject only: a natural everyday clothing composition with no second person or background people.",
                    "Use exactly one selected legwear option: skin-tone leg-cover styling, opaque white thigh-high stockings, or opaque black thigh-high stockings; keep continuous coverage from the upper thigh to the knee. Never generate crew socks, mid-calf socks, ankle socks, athletic socks, or ordinary cotton socks.",
                    "Keep the close waist-to-knee crop stable, with natural clothing folds, proportions, and perspective.",
                    "Use ordinary smartphone perspective, ambient window or room light, subtle exposure variation, realistic fabric texture, and lightly textured skin; avoid studio polish, plastic skin, illustration, or 3D-rendered surfaces.",
                ])
            wear_match = re.search(r"本次服装搭配(?:已锁定为)?[:：]\s*([^。]+)", str(action))
            if wear_match:
                wear_text = str(wear_match.group(1)).strip()
                selected_source = wear_text.split("；", 1)[0]
                selected = next((name for name in ("光腿神器", "白丝", "黑丝") if name in selected_source), "")
                selected_text = {
                    "光腿神器": "skin-tone leg-cover styling, continuous from upper thigh to knee",
                    "白丝": "opaque white thigh-high stockings, continuous from upper thigh to knee",
                    "黑丝": "opaque black thigh-high stockings, continuous from upper thigh to knee",
                }.get(selected, "one of skin-tone leg-cover styling, opaque white thigh-high stockings, or opaque black thigh-high stockings")
                lines.append(
                    f"Selected outfit detail: {selected_text}; never use crew socks, mid-calf socks, ankle socks, athletic socks, or ordinary cotton socks."
                )
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
                "sit": "Sit naturally on a chair or sofa; let the everyday outfit drape naturally in the waistline-to-knee frame.",
                "sit_crop": "Sit naturally on a chair or sofa; let the everyday outfit drape naturally in the waistline-to-knee frame.",
                "kneel": "Use a natural kneeling pose on a rug or cushion; let the outfit fall naturally in the waistline-to-knee frame.",
                "kneel_crop": "Use a natural kneeling pose on a rug or cushion; let the outfit fall naturally in the waistline-to-knee frame.",
                "side_lie": "Rest comfortably on one side on a bed or sofa; keep the outfit and bedding natural in the waistline-to-knee frame.",
                "side_lie_crop": "Rest comfortably on one side on a bed or sofa; keep the outfit and bedding natural in the waistline-to-knee frame.",
                "hug_knee": "Use a comfortable seated pose with the knees gathered naturally; keep the outfit relaxed in the waistline-to-knee frame.",
                "hug_knee_crop": "Use a comfortable seated pose with the knees gathered naturally; keep the outfit relaxed in the waistline-to-knee frame.",
                "cross_leg": "Use a relaxed seated pose with the clothing naturally crossed or staggered; keep the waistline-to-knee frame stable.",
                "cross_leg_crop": "Use a relaxed seated pose with the clothing naturally crossed or staggered; keep the waistline-to-knee frame stable.",
                "windowsill": "Sit naturally on a windowsill or low cabinet; keep the waistline-to-knee outfit frame stable with soft window light.",
                "windowsill_crop": "Sit naturally on a windowsill or low cabinet; keep the waistline-to-knee outfit frame stable with soft window light.",
                "kneel_up": "Use a slightly raised kneeling pose on a soft cushion; keep the outfit natural in the waistline-to-knee frame.",
                "kneel_front": "Use a front-facing kneeling pose on a rug; keep the outfit neat in the waistline-to-knee frame.",
                "floor_fold": "Use a relaxed bent-knee seated pose on a rug or wood floor; keep the waistline-to-knee outfit frame stable.",
                "one_knee_fix": "Use a natural one-knee clothing adjustment pose; keep the waistline-to-knee outfit frame stable.",
                "floor_knees_up_crop": "Use a relaxed seated pose on a rug or wood floor; keep the knees naturally bent and the waistline-to-knee outfit frame stable.",
                "reclined_knees_crop": "Use a relaxed seated pose leaning lightly against a sofa or chair; keep the waistline-to-knee outfit frame natural and stable.",
                "desk_sit_crop": "Sit naturally at a desk; keep the waistline-to-knee outfit frame aligned with the desk edge and clothing details visible.",
                "bed_supine_crop": "Rest comfortably on a bed with the outfit falling naturally; keep the waistline-to-knee frame calm and everyday.",
            }
            lines.append(pose_descriptions.get(pose_id, f"Keep the selected composition tag: {pose_id}."))
        return _join(*lines, f"User request: {translated_user}" if translated_user else "")

    user = translated_user or extract_user_prompt(action)
    camera_match = re.search(r"【cam:(selfie|third)】", str(action))
    camera_kind = str(camera_match.group(1) if camera_match else "selfie")
    camera_line_zh = (
        "第一人称手机自拍：人物自己举手机向下记录日常服装局部，镜头从腰线附近取到膝部附近。"
        if camera_kind == "selfie"
        else "第三人称摄影照片：由画面外的朋友用手机拍摄日常服装局部，镜头从腰线附近取到膝部附近。"
    )
    if is_legs and feet_cropped:
        legs_line = camera_line_zh + "成年人物的得体日常穿搭记录，取景集中在腰线附近到膝部附近；展示服装颜色、材质和层次，不刻意强调身体细节。"
    elif is_legs:
        legs_line = camera_line_zh + "成年人物的得体日常穿搭记录，取景集中在腰线附近到膝部附近；展示服装颜色、材质和层次，不刻意强调身体细节。"
    else:
        legs_line = ""
    if is_legs:
        photo_type_line = (
            "这是第一人称手机自拍照片。"
            if camera_kind == "selfie"
            else "这是第三人称摄影照片。"
        )
    else:
        photo_type_line = "这是自拍/日常照片。"
    identity_line = (
        "参考图只用于保持服装比例与自然体态，完整人物位于画面之外。"
        if is_legs
        else "固定主角身份：脸型五官、发型发色、性别、体态与整体长相保持稳定；表情按本次场景自然变化。"
    )
    eye_line = "" if is_legs else "正对镜头时自然看向镜头，眼神有焦点。"
    leg_crop_line = (
        "严格近距离构图：画面只保留腰线附近到膝部附近的服装区域，不扩展为半身或全身照片。"
        if is_legs
        else ""
    )
    if is_legs:
        photo_style_line = (
            "真人摄影质感：像普通手机竖屏近距离记录服装；使用窗光或房间环境光，"
            "保留真实布料厚度、细小褶皱和自然曝光变化；避免棚拍精修、塑料材质、插画感、3D渲染感和过度虚化。"
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
