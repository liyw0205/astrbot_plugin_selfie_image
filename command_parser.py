"""Shared parsing for image command counts, options, and prompt presets."""

from __future__ import annotations

import re
from typing import Any, List, Tuple


FULLWIDTH_DIGIT_TRANS = str.maketrans("０１２３４５６７８９", "0123456789")
CHINESE_DIGITS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "俩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
COUNT_PATTERN = r"(?:\d{1,2}|[一二两俩三四五六七八九十]{1,3})"
COUNT_SUFFIX_PATTERN = r"(?:张|次|幅)?"
PROMPT_SEPARATOR_PATTERN = r"[\s·/／、，,：:（）()\[\]【】;；。.!！？?]+"


def normalize_count(count: Any, max_count: int) -> int:
    try:
        value = int(float(str(count).strip()))
    except Exception:
        value = 1
    return max(1, min(max(1, int(max_count or 1)), value))


def parse_count_token(token: str) -> int:
    text = str(token or "").strip().translate(FULLWIDTH_DIGIT_TRANS)
    if not text:
        return 0
    match = re.fullmatch(r"(\d{1,2})(?:张|次|幅)?", text)
    if match:
        value = int(match.group(1))
        return value if value > 0 else 0

    chinese = re.fullmatch(r"([一二两俩三四五六七八九十]{1,3})(?:张|次|幅)?", text)
    if not chinese:
        return 0
    value_text = chinese.group(1)
    if value_text == "十":
        return 10
    if "十" in value_text:
        before, _, after = value_text.partition("十")
        tens = CHINESE_DIGITS.get(before, 1) if before else 1
        ones = CHINESE_DIGITS.get(after, 0) if after else 0
        value = tens * 10 + ones
        return value if value > 0 else 0
    return CHINESE_DIGITS.get(value_text, 0)


def split_attached_count_token(token: str) -> Tuple[str, int]:
    """Split shorthand such as ``3旗袍``, ``3张西施``, or ``西施3``."""
    text = str(token or "").strip().translate(FULLWIDTH_DIGIT_TRANS)
    if not text:
        return "", 0
    count = parse_count_token(text)
    if count:
        return "", count
    match = re.fullmatch(rf"({COUNT_PATTERN}){COUNT_SUFFIX_PATTERN}(.+)", text)
    if match:
        count = parse_count_token(match.group(1))
        remainder = match.group(2).strip()
        classifiers = "只位个名件套条张幅次本辆杯碗颗粒朵座间栋台部份种组对双"
        is_measure_phrase = bool(re.match(rf"[{classifiers}]", remainder))
        if count and remainder and not re.search(r"[\dA-Za-z]", remainder) and not is_measure_phrase:
            return remainder, count
    match = re.fullmatch(rf"(.+?)({COUNT_PATTERN}){COUNT_SUFFIX_PATTERN}", text)
    if match:
        count = parse_count_token(match.group(2))
        remainder = match.group(1).strip()
        if count and remainder and not re.search(r"[\dA-Za-z]", remainder):
            return remainder, count
    return text, 0


def command_tokens_for_count(text: str) -> List[str]:
    raw_tokens = re.sub(r"\s+", " ", str(text or "").strip()).split()
    tokens: List[str] = []
    for index, token in enumerate(raw_tokens):
        if index < 2:
            parts = [part.strip() for part in re.split(r"[/／]+", token) if part.strip()]
            count_like_parts = sum(1 for part in parts if parse_count_token(part))
            if 1 < len(parts) <= 2 and count_like_parts == 1:
                tokens.extend(parts)
                continue
        tokens.append(token)
    return tokens


def extract_command_count(
    text: str,
    max_count: int,
    *,
    allow_attached: bool = False,
    allow_trailing: bool = False,
) -> Tuple[str, int]:
    """Extract a batch count from flexible command parameter positions."""
    tokens = command_tokens_for_count(text)
    if not tokens:
        return "", 1
    indices = [0, 1]
    if allow_trailing or allow_attached:
        indices.extend(range(2, len(tokens)))
    for index in dict.fromkeys(indices):
        if index >= len(tokens):
            continue
        count = parse_count_token(tokens[index])
        if count:
            remaining = [token for pos, token in enumerate(tokens) if pos != index]
            return " ".join(remaining).strip(), normalize_count(count, max_count)
        if allow_attached:
            remainder, count = split_attached_count_token(tokens[index])
            if count:
                remaining = [token for pos, token in enumerate(tokens) if pos != index]
                if remainder:
                    remaining.insert(index, remainder)
                return " ".join(remaining).strip(), normalize_count(count, max_count)
    return " ".join(tokens).strip(), 1


def parse_prompt_options(
    text: str,
    aspect_ratio: str = "",
    resolution: str = "",
    *,
    default_aspect_ratio: str = "9:16",
    default_resolution: str = "1K",
) -> Tuple[str, str, str]:
    prompt = str(text or "").strip()
    aspect = str(aspect_ratio or default_aspect_ratio or "9:16").strip() or "9:16"
    resol = str(resolution or default_resolution or "1K").strip() or "1K"
    matches = list(re.finditer(r"--([a-zA-Z0-9_\-]+)(?:[=\s]+([^\s]+))?", prompt))
    for match in reversed(matches):
        key = match.group(1).lower().replace("-", "_")
        value = str(match.group(2) or "").strip()
        if key in {"ar", "aspect", "aspect_ratio", "ratio"} and value:
            aspect = value
            prompt = prompt[: match.start()] + prompt[match.end() :]
        elif key in {"resolution", "res", "quality"} and value:
            resol = value
            prompt = prompt[: match.start()] + prompt[match.end() :]
        elif key == "size" and value:
            if "2048" in value or value.upper() == "2K":
                resol = "2K"
            elif "4096" in value or value.upper() == "4K":
                resol = "4K"
            prompt = prompt[: match.start()] + prompt[match.end() :]
    return re.sub(r"\s+", " ", prompt).strip(), aspect, resol


def resolve_image_preset(
    prompt: str,
    aspect_ratio: str,
    resolution: str,
    *,
    presets: Any,
    default_aspect_ratio: str,
    default_resolution: str,
) -> Tuple[str, str, str, str, str]:
    cleaned_prompt, aspect, resol = parse_prompt_options(
        prompt,
        aspect_ratio,
        resolution,
        default_aspect_ratio=default_aspect_ratio,
        default_resolution=default_resolution,
    )
    resolved = presets.resolve(cleaned_prompt)
    preset_name = str(resolved.get("preset_name") or "").strip()
    if preset_name:
        cleaned_prompt = str(resolved.get("prompt") or cleaned_prompt).strip()
        preset_aspect = str(resolved.get("aspect_ratio") or "").strip()
        preset_resolution = str(resolved.get("resolution") or "").strip()
        if preset_aspect and aspect == default_aspect_ratio:
            aspect = preset_aspect
        if preset_resolution and resol == default_resolution:
            resol = preset_resolution
    return (
        cleaned_prompt,
        aspect,
        resol,
        preset_name,
        str(resolved.get("description") or "").strip(),
    )


def expand_user_text_with_preset(
    raw_text: str,
    *,
    presets: Any,
    default_aspect_ratio: str,
    default_resolution: str,
) -> Tuple[str, str, str, str]:
    """Resolve preset parameters in any position while retaining other text."""
    text = str(raw_text or "").strip()
    if not text:
        return "", "", "", ""
    try:
        presets.load()
    except Exception:
        pass
    expanded, aspect, resolution, preset_name, _ = resolve_image_preset(
        text,
        "",
        "",
        presets=presets,
        default_aspect_ratio=default_aspect_ratio,
        default_resolution=default_resolution,
    )
    if preset_name:
        return str(expanded or text).strip(), aspect, resolution, preset_name

    cleaned, aspect, resolution = parse_prompt_options(
        text,
        default_aspect_ratio=default_aspect_ratio,
        default_resolution=default_resolution,
    )
    pieces = re.split(rf"({PROMPT_SEPARATOR_PATTERN})", cleaned)
    output: List[str] = []
    found_name = ""
    for piece in pieces:
        if not piece or re.fullmatch(PROMPT_SEPARATOR_PATTERN, piece):
            output.append(piece)
            continue
        resolved = presets.resolve(piece)
        part_name = str(resolved.get("preset_name") or "").strip()
        if not part_name:
            output.append(piece)
            continue
        output.append(str(resolved.get("prompt") or piece).strip())
        if not found_name:
            found_name = part_name
            part_aspect = str(resolved.get("aspect_ratio") or "").strip()
            part_resolution = str(resolved.get("resolution") or "").strip()
            if part_aspect and aspect == default_aspect_ratio:
                aspect = part_aspect
            if part_resolution and resolution == default_resolution:
                resolution = part_resolution
    return "".join(output).strip() or cleaned, aspect, resolution, found_name


def expand_cos_user_text_with_preset(
    raw_text: str,
    *,
    presets: Any,
    default_aspect_ratio: str,
    default_resolution: str,
) -> Tuple[str, str, str, str]:
    """Expand one COS preset token without changing the pool matching query."""
    text = str(raw_text or "").strip()
    if not text:
        return "", "", "", ""
    expanded, aspect, resolution, preset_name = expand_user_text_with_preset(
        text,
        presets=presets,
        default_aspect_ratio=default_aspect_ratio,
        default_resolution=default_resolution,
    )
    if preset_name:
        return expanded, aspect, resolution, preset_name

    pieces = re.split(rf"({PROMPT_SEPARATOR_PATTERN})", expanded)
    output: List[str] = []
    found_name = ""
    for piece in pieces:
        if not piece or re.fullmatch(PROMPT_SEPARATOR_PATTERN, piece):
            output.append(piece)
            continue
        part_expanded, part_aspect, part_resolution, part_name = expand_user_text_with_preset(
            piece,
            presets=presets,
            default_aspect_ratio=default_aspect_ratio,
            default_resolution=default_resolution,
        )
        if not part_name:
            output.append(piece)
            continue
        output.append(part_expanded)
        if not found_name:
            found_name = part_name
            if aspect == default_aspect_ratio and part_aspect:
                aspect = part_aspect
            if resolution == default_resolution and part_resolution:
                resolution = part_resolution
    return "".join(output).strip(), aspect, resolution, found_name


def normalize_preset_input(text: str) -> str:
    return str(text or "").strip().replace("\r", " ").replace("\n", " ")


def split_preset_command(text: str) -> Tuple[str, str]:
    value = normalize_preset_input(text)
    if not value:
        return "", ""
    if " " in value:
        head, tail = value.split(" ", 1)
        return head.strip(), tail.strip()
    return value, ""
