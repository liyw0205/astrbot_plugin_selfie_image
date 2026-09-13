"""Shared prompt composition for freeform image and image-edit requests."""

from __future__ import annotations

from typing import List

from ..features.persona import anatomy_constraint_lines
from ..core.providers import ImageReference


def append_anatomy_constraints(prompt: str, *, language: str = "zh") -> str:
    """Add optional quality constraints for fixed prompt pipelines."""
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
    """Keep freeform text intact and add only the required reference wrapper."""
    raw = str(prompt or "").strip()
    if not images:
        return append_anatomy_constraints(raw, language=language) if enhance else raw
    is_cos = bool(
        "【cos:" in raw.lower()
        or "【cos：" in raw
        or "看看cos" in raw.lower()
        or "cos换装" in raw.lower()
        or "角色扮演" in raw
    )
    if language != "en":
        lines = [
            "使用提供的参考图作为视觉参考。",
            (
                "COS 换装时，参考图只锁定同一人的身份、脸型五官、性别、肤色和身体比例；"
                "不要复制参考图原有动作、手势、头部角度或固定表情，姿势、头部朝向和表情按 COS 要求重新生成。"
                if is_cos
                else "按用户要求修改，并尽量保持相关人物身份、服装、姿势、场景与构图一致。"
            ),
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
        (
            "For COS outfit changes, use the reference only for the same person's identity, facial structure, gender, skin tone, and body proportions; "
            "do not copy the reference pose, gestures, head angle, or fixed expression. Recreate the pose, head direction, and expression from the COS request."
            if is_cos
            else "Follow the user's requested changes while preserving relevant identity, outfit, pose, scene, and composition."
        ),
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
