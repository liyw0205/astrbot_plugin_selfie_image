"""Shared prompt composition for freeform image and image-edit requests."""

from __future__ import annotations

from typing import List

from .persona import anatomy_constraint_lines
from .providers import ImageReference


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
