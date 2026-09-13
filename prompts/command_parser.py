"""Shared parsing for image command counts, options, and prompt presets."""

from __future__ import annotations

import math
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
NON_COUNT_FOLLOWING_UNITS = {
    "岁", "年", "月", "日", "号", "点", "时", "分", "秒",
    "厘米", "米", "公里", "mm", "cm", "m", "km", "kg", "斤", "%",
}

# Creative template options are intentionally limited to known variable names.
# Unknown ``--foo`` tokens remain in the prompt so existing provider-specific
# options and natural language are not silently discarded.
TEMPLATE_OPTION_ALIASES = {
    "角色": "role",
    "人物": "role",
    "主体": "role",
    "role": "role",
    "character": "role",
    "服饰": "outfit",
    "服装": "outfit",
    "穿搭": "outfit",
    "outfit": "outfit",
    "clothes": "outfit",
    "场景": "scene",
    "环境": "scene",
    "地点": "scene",
    "scene": "scene",
    "environment": "scene",
    "姿势": "pose",
    "动作": "pose",
    "pose": "pose",
    "action": "pose",
    "镜头": "shot",
    "机位": "shot",
    "shot": "shot",
    "camera": "shot",
    "视角": "view",
    "view": "view",
    "构图": "composition",
    "composition": "composition",
    "光线": "lighting",
    "lighting": "lighting",
    "时长": "duration",
    "duration": "duration",
}

# Natural-language prompts often carry their own framing hint.  This is
# deliberately separate from ``--ar`` parsing so templates such as COS looks
# can provide a ratio without requiring a command option.
_NUMERIC_ASPECT_RATIO_RE = re.compile(
    r"(?<![\dA-Za-z])(?P<width>\d{1,2})\s*[:：/]\s*(?P<height>\d{1,2})(?![\dA-Za-z])"
)
_DIMENSION_ASPECT_RATIO_RE = re.compile(
    r"(?<![\dA-Za-z])(?P<width>\d{2,5})\s*[x×✕Ｘ]\s*(?P<height>\d{2,5})(?![\dA-Za-z])",
    re.IGNORECASE,
)
_ORIENTATION_ASPECT_RE = re.compile(
    r"(?P<orientation>竖屏|竖图|纵向|人像构图|portrait|vertical|横屏|横图|横向|landscape|horizontal|方图|方形|正方形|square)",
    re.IGNORECASE,
)
_ASPECT_OVERRIDE_MARKER_RE = re.compile(
    r"(?:用户补充要求优先|额外要求|用户要求|用户提示|用户输入)\s*[:：]",
    re.IGNORECASE,
)
_NEGATED_ASPECT_PREFIX_RE = re.compile(r"(?:不要|禁止|避免|不使用|不是|非)[^。；;，,。!?！？]{0,10}$")


def normalize_aspect_ratio_value(value: str) -> str:
    """Normalize a ratio or pixel dimension into a canonical ``width:height``."""
    raw = str(value or "").strip().replace("：", ":").replace("／", "/")
    match = re.fullmatch(r"(\d{1,5})\s*[:/]\s*(\d{1,5})", raw)
    if not match:
        match = re.fullmatch(r"(\d{2,5})\s*[x×✕Ｘ]\s*(\d{2,5})", raw, flags=re.IGNORECASE)
    if not match:
        return ""
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        return ""
    divisor = math.gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def _orientation_aspect_ratio(value: str) -> str:
    lowered = str(value or "").strip().lower()
    if lowered in {"竖屏", "竖图", "纵向", "人像构图", "portrait", "vertical"}:
        return "9:16"
    if lowered in {"横屏", "横图", "横向", "landscape", "horizontal"}:
        return "16:9"
    if lowered in {"方图", "方形", "正方形", "square"}:
        return "1:1"
    return ""


def _aspect_search_scope(text: str) -> str:
    """Prefer the user-supplement section appended by action builders."""
    raw = str(text or "")
    markers = list(_ASPECT_OVERRIDE_MARKER_RE.finditer(raw))
    if not markers:
        return raw
    return raw[markers[-1].end() :]


def _is_negated_aspect(text: str, start: int) -> bool:
    prefix = str(text or "")[max(0, int(start) - 14) : int(start)]
    return bool(_NEGATED_ASPECT_PREFIX_RE.search(prefix))


def extract_prompt_aspect_ratio(text: str) -> str:
    """Extract a prompt-provided aspect ratio, preferring explicit supplements.

    Numeric forms (``9:16``, ``9/16``, ``1024x1536``) win over orientation
    words.  A negated hint such as ``不要横屏`` is ignored.  When an action
    contains a ``用户补充要求优先：`` section, only that section is considered
    first so the user's extra request can override a template's framing.
    """
    raw = str(text or "")
    if not raw.strip():
        return ""

    def find_in(scope: str) -> str:
        numeric_matches = [
            match
            for pattern in (_DIMENSION_ASPECT_RATIO_RE, _NUMERIC_ASPECT_RATIO_RE)
            for match in pattern.finditer(scope)
            if not _is_negated_aspect(scope, match.start())
        ]
        if numeric_matches:
            match = max(numeric_matches, key=lambda item: item.start())
            ratio = normalize_aspect_ratio_value(match.group(0))
            if ratio:
                return ratio
        orientation_matches = [
            match
            for match in _ORIENTATION_ASPECT_RE.finditer(scope)
            if not _is_negated_aspect(scope, match.start())
        ]
        if orientation_matches:
            return _orientation_aspect_ratio(max(orientation_matches, key=lambda item: item.start()).group("orientation"))
        return ""

    scoped = _aspect_search_scope(raw)
    return find_in(scoped) or (find_in(raw) if scoped != raw else "")


def extract_template_options(text: str) -> Tuple[str, dict[str, str], bool]:
    """Extract known creative variable options from arbitrary prompt positions.

    Examples::

        ``{角色}在{场景} --角色 宁红夜 --场景=庭院``
        ``--template-random`` / ``--随机变量`` enables random defaults.

    Values are one shell-like token, with a quoted value allowed for spaces.
    Unknown options are preserved for the existing option/preset parsers.
    """
    raw = str(text or "")
    values: dict[str, str] = {}
    randomize = False
    pattern = re.compile(
        r"(?<!\S)--(?P<name>[A-Za-z][A-Za-z0-9_-]*|[^\s=]+)"
        r"(?:\s*=\s*|\s+)"
        r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s]+)"
    )

    def replace(match: re.Match[str]) -> str:
        nonlocal randomize
        raw_name = str(match.group("name") or "").strip()
        lowered = raw_name.lower()
        if lowered in {"template-random", "randomize", "random-template"} or raw_name in {"随机变量", "随机缺省"}:
            # A value-looking token belongs to the prompt unless it is a
            # recognized boolean switch; this branch is handled below too.
            return match.group(0)
        key = TEMPLATE_OPTION_ALIASES.get(raw_name, TEMPLATE_OPTION_ALIASES.get(lowered, ""))
        if not key:
            return match.group(0)
        value = str(match.group("value") or "").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1].strip()
        if value:
            values[key] = value
        return " "

    cleaned = pattern.sub(replace, raw)
    # Boolean switches do not need a value and therefore are handled after the
    # value-bearing expression.  They are removed only when standalone.
    switch = re.compile(r"(?<!\S)(?:--template-random|--randomize|--random-template|--随机变量|--随机缺省)(?=\s|$)", re.IGNORECASE)
    if switch.search(cleaned):
        randomize = True
        cleaned = switch.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(), values, randomize


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


def extract_explicit_count_option(tokens: List[str]) -> Tuple[List[str], int]:
    """Extract ``-c`` count options from any position in a command prompt."""
    for index, raw_token in enumerate(tokens):
        token = str(raw_token or "").strip().translate(FULLWIDTH_DIGIT_TRANS)
        lowered = token.lower()
        if lowered == "-c":
            if index + 1 >= len(tokens):
                continue
            count = parse_count_token(tokens[index + 1])
            if count:
                remaining = [
                    item for pos, item in enumerate(tokens) if pos not in {index, index + 1}
                ]
                return remaining, count
            continue

        if not lowered.startswith("-c"):
            continue
        value = token[2:]
        if value.startswith("="):
            value = value[1:]
        count = parse_count_token(value)
        if count:
            remaining = [item for pos, item in enumerate(tokens) if pos != index]
            return remaining, count
    return tokens, 0


def is_prompt_quantity(tokens: List[str], index: int) -> bool:
    """Keep natural prompt numbers such as ``约 20 岁`` out of legacy counts."""
    if not 0 <= index < len(tokens):
        return False
    token = str(tokens[index] or "").strip().translate(FULLWIDTH_DIGIT_TRANS)
    if not re.fullmatch(COUNT_PATTERN, token):
        return False
    if index + 1 >= len(tokens):
        return False
    following = str(tokens[index + 1] or "").strip().lower()
    return any(following == unit or following.startswith(unit) for unit in NON_COUNT_FOLLOWING_UNITS)


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

    # ``-c`` is explicit, can be placed anywhere, and must win over a number
    # that happens to be part of the prompt, such as "20 岁".
    remaining, count = extract_explicit_count_option(tokens)
    if count:
        return " ".join(remaining).strip(), normalize_count(count, max_count)

    indices = [0, 1]
    if allow_trailing or allow_attached:
        indices.extend(range(2, len(tokens)))
    for index in dict.fromkeys(indices):
        if index >= len(tokens):
            continue
        count = parse_count_token(tokens[index])
        if count and not is_prompt_quantity(tokens, index):
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
    option_aspect = ""
    matches = list(re.finditer(r"--([a-zA-Z0-9_\-]+)(?:[=\s]+([^\s]+))?", prompt))
    for match in reversed(matches):
        key = match.group(1).lower().replace("-", "_")
        value = str(match.group(2) or "").strip()
        if key in {"ar", "aspect", "aspect_ratio", "ratio"} and value:
            option_aspect = normalize_aspect_ratio_value(value) or value
            aspect = option_aspect
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
    # Template and natural-language framing is more specific than the global
    # setting (or a preset fallback). An explicit ``--ar`` remains strongest.
    if not option_aspect:
        detected_aspect = extract_prompt_aspect_ratio(prompt)
        if detected_aspect:
            aspect = detected_aspect
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
        detected_aspect = extract_prompt_aspect_ratio(cleaned_prompt)
        if detected_aspect:
            aspect = detected_aspect
        elif preset_aspect and aspect == default_aspect_ratio:
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
            detected_part_aspect = extract_prompt_aspect_ratio(output[-1])
            if detected_part_aspect:
                aspect = detected_part_aspect
            elif part_aspect and aspect == default_aspect_ratio:
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
