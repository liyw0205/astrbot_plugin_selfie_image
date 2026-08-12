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
        value = re.sub(r"\s*【(?:pose|shot):[a-z_]+】\s*", " ", value)
        return re.sub(r"\s+", " ", value).strip(" 。")
    if any(marker in text for marker in ("【自拍 / 看看模式】", "【他拍 / 看看你模式】", "【合影 / 合照模式】", "看看腿。", "【唯一姿势·不可混用】")):
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
    is_legs = "看看腿" in str(action) or "腿部" in str(action) or "【pose:" in str(action)
    is_group = "合影" in str(action) or "合照" in str(action) or "同框" in str(action)
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
        lines = [
            f"Create one natural {style} everyday photo.",
            "Keep the main subject's identity, gender, face, hair, body proportions, and overall appearance stable; change expression naturally with the scene.",
            "When facing the camera, keep natural focused eye contact unless the request says otherwise.",
        ]
        if has_reference_image:
            lines.append("Use reference image 1 only as the main identity anchor; later references must not replace the main subject.")
        else:
            lines.append("Use the character settings as the stable identity anchor; default to an adult woman when gender is unspecified.")
        if extra_reference_count:
            lines.append("Use extra references for clothing, pose, composition, lighting, or scene only.")
        # Legs first: action text may contain "不要合影" which must not flip into group mode.
        if is_legs:
            if feet_cropped:
                lines.extend([
                    "Single subject, close first-person downward shot: show only a little waist or skirt, thighs, and knees; crop both ankles and feet fully outside the frame.",
                    "Do not show the face, hair, or head; crop the face fully outside the frame; no knees pressed against the face.",
                    "Keep exactly two natural thighs and knees connected from the hips.",
                    "Use only the selected bare-leg effect or separate white/black opaque thigh-high stockings with a clear rolled cuff that show leg shape, not skin through; never become tights.",
                    "Do not add toes, soles, shoes, or any foot detail; preserve natural close-range perspective without forced symmetry.",
                ])
            else:
                lines.extend([
                    "Single subject only: a tasteful lower-body close-up with no second person, background people, or shoes.",
                    "Use natural thighs; selected legwear is bare-leg effect or white/black opaque mid-calf stockings that emphasize leg shape with a light sock-top indent, not sheer skin-see-through.",
                    "Keep two natural legs and feet, correct joints, stable pose, realistic anatomy, and perspective.",
                ])
        elif is_group:
            lines.extend([
                "Turn each referenced person or non-person subject into an independent complete person with clear boundaries; do not leave toys or flat cutouts.",
                "Keep everyone in one coherent scene with natural positions, scale, perspective, and friendly interaction.",
            ])
        else:
            lines.extend([
                "Keep the photo complete and everyday, with coherent light, color, depth, camera perspective, clothing, and scene relationships.",
                "Keep visible anatomy complete and natural: one left and one right hand or foot, connected limbs, and complete fingers or toes.",
            ])
        pose = re.search(r"【(?:pose|shot):([a-z_]+)】", str(action))
        if pose:
            lines.append(f"Keep the selected composition tag: {pose.group(1)}.")
        return _join(*lines, f"User request: {translated_user}" if translated_user else "")

    user = translated_user or extract_user_prompt(action)
    if is_legs and feet_cropped:
        legs_line = "单人近距离俯拍，只露少量腰腹/裙摆、大腿与膝部；小腿与双脚裁出画外；脸部/头发也裁出画外，禁止膝盖顶脸；腿部穿搭只用已选定的光腿神器、白丝或黑丝，白丝/黑丝袜口有卷边，禁止连裤袜。"
    elif is_legs:
        legs_line = "单人下半身近景，只保留自然完整的两条腿和双脚；光腿时脚趾自然清晰；腿部穿搭只用已选定的光腿神器、白丝或黑丝，白丝/黑丝袜口有卷边，不要鞋子。"
    else:
        legs_line = ""
    return _join(
        "这是自拍/日常照片。",
        "固定主角身份：脸型五官、发型发色、性别、体态与整体长相保持稳定；表情按本次场景自然变化。",
        "正对镜头时自然看向镜头，眼神有焦点。",
        "参考图一只作为主角身份锚点，额外参考图只用于服装、姿势、构图、光线或场景。" if has_reference_image else "按角色设定保持主角身份稳定，未说明性别时默认成年女性。",
        "" if is_legs else ("合影对象必须落实为独立完整人物，站位自然，边界清晰。" if is_group else ""),
        legs_line,
        "画面自然完整，光线、色调、景深和相机透视统一。" if not is_legs else "",
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
