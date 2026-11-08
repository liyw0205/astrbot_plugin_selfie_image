"""NAI command routing and prompt composition without selfie wrappers."""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

from ..core.models import ImageModelTarget
from ..core.providers import novelai_model_family
from .artist_presets import NaiArtistPresetManager


def select_nai_targets(targets: Iterable[ImageModelTarget], *, v5_only: bool = False) -> List[ImageModelTarget]:
    """Ignore global routing, preferring enabled NAI4 targets before NAI5."""
    nai = [target for target in targets if target.provider_type == "novelai"]
    v5 = [target for target in nai if novelai_model_family(target.model) == "nai5"]
    if v5_only:
        return v5
    v4 = [
        target for target in nai
        if re.search(
            r"(?:^|-)(?:nai-?4|v4|diffusion-4)(?:-|$)",
            re.sub(r"[^a-z0-9]+", "-", target.model.lower()).strip("-"),
        )
    ]
    return v4 + v5


def extract_artist_request(text: str, manager: NaiArtistPresetManager) -> Tuple[str, str]:
    """Read an explicit --artist/[name] selector or an exact leading preset."""
    raw = str(text or "").strip()
    option = re.search(r'(?:^|\s)--artist(?:=|\s+)(?:"([^"]+)"|\[([^\]]+)\]|([^\s]+))', raw, re.I)
    if option:
        value = next(value for value in option.groups() if value is not None)
        name, _ = manager.find_artist(value)
        if not name:
            raise ValueError(f"找不到 NAI 画师串：{value}")
        return (raw[:option.start()] + " " + raw[option.end():]).strip(), name
    bracket = re.match(r"^[\[【]([^\]】]+)[\]】](?:\s*|$)", raw)
    if bracket:
        name, _ = manager.find_artist(bracket.group(1))
        if name:
            return raw[bracket.end():].strip(), name
    for key, _ in sorted(manager.list(), key=lambda item: len(item[0]), reverse=True):
        if raw.casefold() == key.casefold():
            return "", key
        if raw.casefold().startswith(key.casefold() + " "):
            return raw[len(key):].strip(), key
    return raw, ""


def _top_level_tags(text: str) -> List[str]:
    tags = []
    start = 0
    depth = 0
    weighted = False
    index = 0
    while index < len(text):
        char = text[index]
        if text[index:index + 2] == "::":
            weighted = not weighted
            index += 2
            continue
        if char in "[{(":
            depth += 1
        elif char in "]})":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0 and not weighted:
            tags.append(text[start:index].strip())
            start = index + 1
        index += 1
    tags.append(text[start:].strip())
    return tags


def compose_nai_prompt(prompt: str, artist: str, quality: str) -> str:
    # Weights can cover several comma-delimited tags; deduplicate only complete
    # top-level groups so an artist blend keeps its original weight and syntax.
    parts = [str(part).strip().strip(",") for part in (artist, prompt, quality) if str(part).strip()]
    seen: set[str] = set()
    tags = []
    for part in parts:
        for tag in _top_level_tags(part):
            tag = tag.strip()
            if tag and tag.casefold() not in seen:
                seen.add(tag.casefold())
                tags.append(tag)
    return ", ".join(tags)
