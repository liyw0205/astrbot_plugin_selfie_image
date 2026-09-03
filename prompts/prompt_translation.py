"""Prompt translation response parsing helpers."""

from __future__ import annotations

import re

def parse_prompt_en_response(text: str) -> str:
    """Extract English prompt from translator JSON; empty means failure -> keep original."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:\w+)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        import json as _json

        payload = None
        try:
            payload = _json.loads(cleaned)
        except Exception:
            matched = re.search(r"\{(?:[^{}]|\{[^{}]*\})*\}", cleaned, flags=re.S)
            if matched:
                payload = _json.loads(matched.group(0))
        if isinstance(payload, dict):
            if payload.get("ok") is False:
                return ""
            en = payload.get(
                "en",
                payload.get("english", payload.get("prompt", payload.get("translation", ""))),
            )
            en = str(en or "").strip()
            if not en:
                return ""
            low = en.lower()
            if low.startswith("{") or "return only one json" in low or "source prompt:" in low:
                return ""
            return en
    except Exception:
        pass
    # Legacy plain-text fallback for old templates.
    plain = re.sub(r"^(?:English\s*prompt|Translation|译文|英文)[:：]\s*", "", cleaned, flags=re.I).strip()
    if not plain or plain.startswith("{"):
        return ""
    low = plain.lower()
    if ("allow" in low and "reason" in low) or "return only one json" in low or "source prompt:" in low:
        return ""
    if "faithful language conversion only" in low or "translate the image-generation prompt" in low:
        return ""
    if "translate the video-generation prompt" in low:
        return ""
    return plain
