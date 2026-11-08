"""Recover image content as tags for NAI text-only regeneration."""

from __future__ import annotations

import asyncio
import json
import re
from io import BytesIO
from pathlib import Path

from PIL import Image

from ..core.prompt_data import read_versioned_json
from .prompts import _top_level_tags, compose_nai_prompt


DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "nai" / "reference.json"
MAX_PROMPT_LENGTH = 24000


def _text(value) -> str:
    if isinstance(value, bytes):
        if value.startswith(b"ASCII\0\0\0"):
            value = value[8:]
        elif value.startswith(b"UNICODE\0"):
            value = value[8:]
            if value.startswith((b"\xff\xfe", b"\xfe\xff")):
                return value.decode("utf-16", errors="replace").strip("\0 ")
            encoding = "utf-16-le" if value[1:2] == b"\0" else "utf-16-be"
            return value.decode(encoding, errors="replace").strip("\0 ")
        return value.decode("utf-8", errors="replace").strip("\0 ")
    return value.strip() if isinstance(value, str) else ""


def _generation_comment(value, *, novelai: bool = False) -> dict:
    text = _text(value)
    if not text or len(text) > MAX_PROMPT_LENGTH * 4:
        return {}
    try:
        payload = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    # A free-form Description/EXIF comment is not proof of generation metadata.
    fields = ("seed", "steps", "sampler", "scale", "v4_prompt")
    if not novelai and sum(key in payload for key in fields) < 2:
        return {}
    v4 = payload.get("v4_prompt")
    caption = v4.get("caption") if isinstance(v4, dict) else None
    caption = caption if isinstance(caption, dict) else {}
    prompt = _text(payload.get("prompt")) or _text(caption.get("base_caption"))
    characters = []
    rows = caption.get("char_captions")
    for row in rows[:16] if isinstance(rows, list) else []:
        value = _text(row.get("char_caption")) if isinstance(row, dict) else ""
        if value:
            characters.append(value[:MAX_PROMPT_LENGTH])
    if not prompt and not characters:
        return {}
    return {"prompt": prompt[:MAX_PROMPT_LENGTH], "characters": characters}


def _sd_parameters(value) -> dict:
    text = _text(value)
    if len(text) > MAX_PROMPT_LENGTH * 4:
        return {}
    match = re.search(r"(?:^|\n)Steps:\s*\d+[^\n]*\bSampler:", text)
    if not match:
        return {}
    prompt = text[:match.start()].split("\nNegative prompt:", 1)[0].strip()
    # SD weights and NAI weights have different meanings. Let the tag resolver
    # convert these rather than forwarding raw SD syntax to the image model.
    return {"prompt": prompt[:MAX_PROMPT_LENGTH], "normalize": True} if prompt else {}


def read_reference_metadata(data: bytes) -> dict:
    """Read standard PNG or EXIF generation metadata, without decoding pixels."""
    try:
        with Image.open(BytesIO(data)) as image:
            info = image.info
            novelai = "novelai" in _text(info.get("Software")).lower()
            parsed = _generation_comment(info.get("Comment"), novelai=novelai)
            if parsed:
                return {"source": "png_comment", **parsed}
            comment = None
            try:
                exif = image.getexif()
                comment = exif.get(37510)
                if comment is None:
                    comment = exif.get_ifd(34665).get(37510)
            except Exception:
                pass
            parsed = _generation_comment(comment, novelai=novelai)
            if parsed:
                return {"source": "exif_comment", **parsed}
            for value in (info.get("parameters"), comment):
                parsed = _sd_parameters(value)
                if parsed:
                    return {"source": "sd_parameters", **parsed}
            description = _text(info.get("Description"))
            if novelai and description:
                return {"source": "nai_description", "prompt": description[:MAX_PROMPT_LENGTH]}
    except Exception:
        # Re-encoded chat images usually have no metadata; vision is the normal
        # fallback, including when an image reader cannot understand its EXIF.
        return {}
    return {}


def strip_reference_controls(prompt: str, control_tags: list) -> str:
    """Keep content weights while excluding the original artist/quality controls."""
    excluded = {str(tag).casefold().replace("_", " ") for tag in control_tags}

    def clean_group(group: str) -> str:
        group = group.strip()
        weight = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)::([\s\S]*)::", group)
        if weight:
            content = strip_reference_controls(weight.group(2), control_tags)
            return f"{weight.group(1)}::{content}::" if content else ""
        if group[:1] in {"{", "[", "("}:
            closing = {"{": "}", "[": "]", "(": ")"}[group[0]]
            depth = 0
            for index, char in enumerate(group):
                if char == group[0]:
                    depth += 1
                elif char == closing:
                    depth -= 1
                if depth == 0:
                    if index == len(group) - 1:
                        content = strip_reference_controls(group[1:-1], control_tags)
                        return group[0] + content + closing if content else ""
                    break
        normalized = group.casefold().replace("_", " ")
        if normalized in excluded or normalized.startswith(("artist:", "rating:")):
            return ""
        return group

    return compose_nai_prompt(
        ", ".join(filter(None, (clean_group(tag) for tag in _top_level_tags(prompt)))), "", ""
    )


def parse_reference_response(response: str) -> str:
    """A failed/refused description must never become a billable image prompt."""
    value = str(response or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value).strip()
    try:
        payload = json.loads(value)
    except ValueError:
        return ""
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return ""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not 0 < len(prompt.strip()) <= MAX_PROMPT_LENGTH:
        return ""
    return prompt.strip()


async def resolve_reference_prompt(call_llm, event, refs, user_prompt: str) -> tuple[str, dict]:
    """Resolve references and edits once; callers send only the resulting text to NAI."""
    if not refs:
        raise RuntimeError("NAI 参考图为空，未发送生图请求。")
    settings = read_versioned_json(DATA_PATH, "NAI 参考图反推")
    references = []
    images = []
    for index, ref in enumerate(refs, 1):
        if not ref or not ref.data:
            raise RuntimeError("NAI 参考图为空，未发送生图请求。")
        metadata = await asyncio.to_thread(read_reference_metadata, ref.data)
        if metadata:
            metadata["prompt"] = strip_reference_controls(metadata.get("prompt", ""), settings["control_tags"])
            metadata["characters"] = [
                strip_reference_controls(text, settings["control_tags"])
                for text in metadata.get("characters", [])
            ]
        if not metadata.get("prompt") and not any(metadata.get("characters", [])):
            images.append(ref.data)
            metadata = {"source": "vision", "image_index": len(images)}
        references.append({"index": index, **metadata})

    first = references[0]
    needs_llm = bool(
        user_prompt.strip() or images or len(references) > 1
        or first.get("characters") or first.get("normalize")
    )
    if needs_llm:
        # Serialize input as data: braces and weight syntax must survive, and
        # edits replace conflicting attributes instead of appending to old tags.
        request = settings["resolve_template"] + "\n" + json.dumps({
            "references": references, "user_instruction": user_prompt.strip(),
        }, ensure_ascii=False)
        try:
            response = await call_llm(event, request, timeout=60, images=images, use_persona=False)
        except Exception as exc:
            raise RuntimeError("NAI 参考图反推或修改合并失败，未发送生图请求。请检查当前 LLM 的识图能力。") from exc
        prompt = parse_reference_response(response)
    else:
        prompt = first["prompt"]
    prompt = strip_reference_controls(prompt, settings["control_tags"])
    if not prompt:
        raise RuntimeError("NAI 参考图反推或修改合并没有返回有效标签，未发送生图请求。请检查当前 LLM 的识图能力。")
    return prompt, {
        "mode": "pseudo_img2img", "applied": True,
        "reference_image_count": len(refs), "visual_image_count": len(images),
        "references": references, "user_instruction": user_prompt.strip(),
        "resolved_prompt": prompt,
    }
