"""Reusable creative helpers for prompt variables, variations and storyboards.

The helpers in this module are deliberately side-effect free.  They are used by
commands, the embedded dashboard and the standalone Web UI so every entry point
can expose the same semantics without duplicating prompt parsing.
"""

from __future__ import annotations

import copy
import hashlib
import random
import re
from collections.abc import Mapping, Sequence
from typing import Any, Dict, Iterable, List, Optional, Tuple


TEMPLATE_VARIABLE_ALIASES: Dict[str, str] = {
    "角色": "role",
    "人物": "role",
    "主体": "role",
    "服饰": "outfit",
    "服装": "outfit",
    "穿搭": "outfit",
    "场景": "scene",
    "环境": "scene",
    "地点": "scene",
    "姿势": "pose",
    "动作": "pose",
    "机位": "shot",
    "镜头": "shot",
    "视角": "view",
    "构图": "composition",
    "光线": "lighting",
    "时长": "duration",
    "role": "role",
    "character": "role",
    "subject": "role",
    "outfit": "outfit",
    "clothes": "outfit",
    "scene": "scene",
    "environment": "scene",
    "pose": "pose",
    "action": "pose",
    "shot": "shot",
    "camera": "shot",
    "view": "view",
    "composition": "composition",
    "lighting": "lighting",
    "duration": "duration",
}

GENERIC_TEMPLATE_POOLS: Dict[str, Tuple[str, ...]] = {
    "scene": ("简洁室内", "窗边客厅", "安静庭院", "城市街角", "简洁影棚"),
    "pose": ("自然站姿", "轻步转身", "侧身回看", "双手自然放在身侧", "坐姿看向镜头"),
    "shot": ("竖屏半身", "三分之四侧面", "环境人像", "近距离人像", "全身构图"),
    "view": ("自拍视角", "他拍视角"),
    "lighting": ("窗光", "柔和室内光", "阴天散射光", "暖色环境光"),
}

_VARIABLE_RE = re.compile(r"\{\s*([^{}|]+?)\s*(?:\|\s*([^{}]*?)\s*)?\}")
_STORYBOARD_LINE_RE = re.compile(
    r"^(?:镜头|shot|scene|分镜)\s*([0-9]+)?\s*[:：\-]?\s*(.*)$",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(
    r"(?:[（(]\s*)?(?:时长|duration)\s*[:：=]?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:秒|s)?\s*(?:[）)]\s*)?",
    re.IGNORECASE,
)
_PAREN_DURATION_RE = re.compile(r"[（(]\s*([0-9]+(?:\.[0-9]+)?)\s*(?:秒|s)\s*[）)]", re.IGNORECASE)


def canonical_template_variable(name: Any) -> str:
    """Normalize Chinese/English aliases to one stable key."""
    raw = str(name or "").strip()
    return TEMPLATE_VARIABLE_ALIASES.get(raw, TEMPLATE_VARIABLE_ALIASES.get(raw.lower(), raw.lower()))


def _pick_pool_value(key: str, pools: Mapping[str, Any], rng: random.Random) -> str:
    values = pools.get(key) if isinstance(pools, Mapping) else None
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        values = GENERIC_TEMPLATE_POOLS.get(key, ())
    options = [str(value).strip() for value in values if str(value).strip()]
    return rng.choice(options) if options else ""


def render_prompt_template(
    template: str,
    values: Optional[Mapping[str, Any]] = None,
    *,
    randomize_missing: bool = False,
    pools: Optional[Mapping[str, Any]] = None,
    seed: Any = None,
) -> Dict[str, Any]:
    """Render ``{变量}`` placeholders while preserving unknown placeholders.

    A placeholder can provide a local fallback, for example ``{场景|室内}``.
    Values may use either Chinese or canonical English names.  Missing values
    are randomized only when explicitly requested; this prevents a normal
    command from silently changing its prompt.
    """
    text = str(template or "")
    provided = values if isinstance(values, Mapping) else {}
    normalized: Dict[str, str] = {}
    for key, value in provided.items():
        canonical = canonical_template_variable(key)
        if str(value or "").strip():
            normalized[canonical] = str(value).strip()
    if seed is None:
        seed = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]
    rng = random.Random(str(seed))
    replacements: Dict[str, str] = {}
    unresolved: List[str] = []
    randomized: Dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        raw_name = str(match.group(1) or "").strip()
        fallback = str(match.group(2) or "").strip()
        key = canonical_template_variable(raw_name)
        value = normalized.get(key, "")
        if not value and randomize_missing:
            value = _pick_pool_value(key, pools or {}, rng)
            if value:
                randomized[key] = value
        if not value:
            value = fallback
        if not value:
            unresolved.append(raw_name)
            return match.group(0)
        replacements[raw_name] = value
        return value

    rendered = _VARIABLE_RE.sub(replace, text)
    return {
        "template": text,
        "prompt": rendered,
        "values": replacements,
        "randomized": randomized,
        "unresolved": list(dict.fromkeys(unresolved)),
        "changed": rendered != text,
    }


def parse_variation_request(text: str) -> Tuple[str, bool, str]:
    """Extract ``--variation``/``--vary`` without touching natural language."""
    value = str(text or "")
    found = False
    vary = ""

    def remove(match: re.Match[str]) -> str:
        nonlocal found, vary
        found = True
        vary = str(match.group(1) or "").strip()
        return " "

    cleaned = re.sub(r"(?:^|\s)--(?:variation|vary)(?:[=\s]+([^\s]+))?", remove, value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip(), found, vary


def build_prompt_variations(
    prompt: str,
    count: int,
    *,
    fixed_values: Optional[Mapping[str, Any]] = None,
    vary: Optional[Iterable[str]] = None,
    pools: Optional[Mapping[str, Any]] = None,
    seed: Any = None,
) -> List[Dict[str, Any]]:
    """Create deterministic variation metadata for one batch.

    Fixed values are rendered identically in every item.  Only requested
    variation keys are randomized; if the prompt has no matching placeholder,
    a compact ``key: value`` suffix is added so the variation is still visible
    to the generation model.
    """
    try:
        total = max(1, min(100, int(count or 1)))
    except (TypeError, ValueError):
        total = 1
    requested = [canonical_template_variable(item) for item in (vary or ("pose", "scene", "shot"))]
    requested = list(dict.fromkeys(item for item in requested if item))
    fixed = dict(fixed_values or {})
    base_seed = str(seed if seed is not None else hashlib.sha256(str(prompt).encode("utf-8", "ignore")).hexdigest())
    rows: List[Dict[str, Any]] = []
    for index in range(total):
        row_seed = f"{base_seed}:{index}"
        values = dict(fixed)
        for key in requested:
            if key not in values or not str(values.get(key) or "").strip():
                # Use a per-item seed to avoid duplicate choices where the
                # global RNG happens to pick the same first value repeatedly.
                values[key] = _pick_pool_value(key, pools or {}, random.Random(row_seed + key))
        rendered = render_prompt_template(
            prompt,
            values,
            randomize_missing=False,
            pools=pools,
            seed=row_seed,
        )
        output = str(rendered["prompt"] or prompt).strip()
        missing_keys = [key for key in requested if key not in rendered["values"] and values.get(key)]
        if missing_keys:
            suffix = "；".join(f"{key}：{values[key]}" for key in missing_keys if values.get(key))
            if suffix:
                output = f"{output}。本张变化：{suffix}。"
        rows.append(
            {
                "index": index + 1,
                "prompt": output,
                "values": {key: str(value) for key, value in values.items() if str(value or "").strip()},
                "vary": requested,
                "seed": row_seed,
            }
        )
    return rows


def parse_video_storyboard(text: str) -> Dict[str, Any]:
    """Parse a lightweight storyboard from lines or ``|``-separated shots."""
    raw = str(text or "").replace("\r", "").strip()
    if not raw:
        return {"enabled": False, "shots": [], "prompt": ""}
    lines = [item.strip() for item in re.split(r"\n+|\s*\|\s*", raw) if item.strip()]
    explicit: List[Dict[str, Any]] = []
    for line in lines:
        match = _STORYBOARD_LINE_RE.match(line)
        if not match:
            continue
        description = str(match.group(2) or "").strip()
        if not description:
            continue
        duration_match = _DURATION_RE.search(description) or _PAREN_DURATION_RE.search(description)
        duration = float(duration_match.group(1)) if duration_match else 0.0
        description = _DURATION_RE.sub("", description)
        description = _PAREN_DURATION_RE.sub("", description).strip(" ,，。")
        explicit.append({"index": len(explicit) + 1, "description": description, "duration": duration})
    if not explicit and len(lines) > 1:
        explicit = [{"index": index + 1, "description": line, "duration": 0.0} for index, line in enumerate(lines)]
    if not explicit:
        return {"enabled": False, "shots": [], "prompt": raw}
    default_duration = round(5.0 / len(explicit), 2)
    for shot in explicit:
        if not shot["duration"]:
            shot["duration"] = default_duration
    prompt_parts = [
        f"镜头{shot['index']}（约{shot['duration']:g}秒）：{shot['description']}"
        for shot in explicit
    ]
    return {
        "enabled": True,
        "shots": explicit,
        "prompt": "；".join(prompt_parts),
    }


def apply_retry_strategy(
    payload: Mapping[str, Any],
    strategy: str = "full",
    *,
    attempts: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return a retry payload adjusted according to a user-facing strategy."""
    normalized = str(strategy or "full").strip().lower().replace("-", "_")
    aliases = {
        "模型": "model_only",
        "只换模型": "model_only",
        "渠道": "channel_only",
        "只换渠道": "channel_only",
        "降分辨率": "lower_resolution",
        "降低分辨率": "lower_resolution",
        "保留参数": "full",
        "完整": "full",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"full", "model_only", "channel_only", "lower_resolution"}:
        raise ValueError("不支持的重试策略，可选：full、model_only、channel_only、lower_resolution")
    result = copy.deepcopy(dict(payload))
    rows = [item for item in (attempts or ()) if isinstance(item, Mapping)]
    failed = [item for item in rows if not item.get("success")]
    last_failed = failed[-1] if failed else (rows[-1] if rows else {})
    if normalized == "model_only":
        result["channel"] = ""
        result["model"] = ""
        result["retry_strategy"] = normalized
    elif normalized == "channel_only":
        result["model"] = ""
        result["retry_strategy"] = normalized
    elif normalized == "lower_resolution":
        current = str(result.get("resolution") or result.get("size") or "1K").strip().upper()
        result["resolution"] = {"4K": "2K", "2K": "1K", "1K": "1K"}.get(current, "1K")
        if str(result.get("media_type") or "image").lower() == "video":
            try:
                result["duration"] = max(1, min(60, int(result.get("duration") or 5)))
            except (TypeError, ValueError):
                result["duration"] = 5
        result["retry_strategy"] = normalized
    else:
        result["retry_strategy"] = "full"
    if last_failed:
        result["retry_from_attempt"] = {
            "channel": str(last_failed.get("channel") or "")[:120],
            "model": str(last_failed.get("model") or last_failed.get("label") or "")[:160],
            "error_category": str(last_failed.get("error_category") or "")[:64],
        }
    return result


def compare_generation_records(records: Iterable[Mapping[str, Any]], limit: int = 8) -> Dict[str, Any]:
    """Build a redacted, stable comparison view for records sharing a prompt."""
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in records:
        if not isinstance(raw, Mapping):
            continue
        record_id = str(raw.get("id") or "").strip()
        if not record_id or record_id in seen:
            continue
        seen.add(record_id)
        request = raw.get("request_data") if isinstance(raw.get("request_data"), Mapping) else {}
        response = raw.get("response_data") if isinstance(raw.get("response_data"), Mapping) else {}
        paths = list(raw.get("generated_image_paths") or response.get("generated_image_paths") or [])
        video_paths = list(raw.get("generated_video_paths") or response.get("generated_video_paths") or [])
        rows.append(
            {
                "id": record_id,
                "created_at": str(raw.get("created_at") or raw.get("time") or ""),
                "success": bool(raw.get("success")),
                "media_type": str(raw.get("media_type") or "image"),
                "prompt": str(raw.get("original_prompt") or raw.get("prompt") or request.get("original_prompt") or "")[:10000],
                "final_prompt": str(raw.get("final_prompt") or raw.get("request_prompt") or "")[:10000],
                "channel": str(raw.get("channel") or request.get("channel") or "")[:160],
                "model": str(raw.get("used_model") or request.get("model") or "")[:180],
                "elapsed_seconds": raw.get("elapsed_seconds") or response.get("elapsed_seconds"),
                "favorite": bool(raw.get("favorite")),
                "pinned": bool(raw.get("pinned")),
                "image_paths": [str(path) for path in paths[:8]],
                "video_paths": [str(path) for path in video_paths[:2]],
            }
        )
        if len(rows) >= max(1, min(50, int(limit or 8))):
            break
    return {
        "count": len(rows),
        "records": rows,
        "common_prompt": rows[0]["prompt"] if rows else "",
        "can_reuse": bool(rows),
    }


class CreativeFeaturesMixin:
    """Plugin-facing adapters for the pure creative helpers above."""

    def render_creative_prompt(self, prompt: str, payload: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        data = payload if isinstance(payload, Mapping) else {}
        values = data.get("template_values") if isinstance(data.get("template_values"), Mapping) else {}
        randomize = data.get("template_randomize") in {True, 1, "1", "true", "yes", "on", "是", "开启"}
        return render_prompt_template(
            prompt,
            values,
            randomize_missing=randomize,
            pools=data.get("template_pools") if isinstance(data.get("template_pools"), Mapping) else None,
            seed=data.get("template_seed"),
        )

    def build_creative_variations(
        self,
        prompt: str,
        count: int,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        vary: Optional[Iterable[str]] = None,
        fixed_values: Optional[Mapping[str, Any]] = None,
        pools: Optional[Mapping[str, Any]] = None,
        seed: Any = None,
    ) -> List[Dict[str, Any]]:
        data = payload if isinstance(payload, Mapping) else {}
        raw_vary = vary or data.get("variation_fields") or data.get("vary") or ("pose", "scene", "shot")
        if isinstance(raw_vary, str):
            raw_vary = re.split(r"[,，\s]+", raw_vary)
        values = fixed_values or (data.get("template_values") if isinstance(data.get("template_values"), Mapping) else {})
        pools = pools or (data.get("template_pools") if isinstance(data.get("template_pools"), Mapping) else None)
        return build_prompt_variations(
            prompt,
            count,
            fixed_values=values,
            vary=raw_vary if isinstance(raw_vary, Iterable) else None,
            pools=pools,
            seed=seed or data.get("variation_seed") or data.get("template_seed"),
        )

    def parse_storyboard_for_web(self, prompt: str) -> Dict[str, Any]:
        return parse_video_storyboard(prompt)

    def list_cos_pools_for_web(self) -> Dict[str, Any]:
        from ..cos.cos_looks import list_cos_look_sets

        pool = getattr(self, "cos_pool", None)
        favorites = set(pool.list_favorites()) if pool is not None else set()
        builtins = list_cos_look_sets()
        for item in builtins:
            item["source"] = "builtin"
            item["favorite"] = str(item.get("id") or "") in favorites
        custom = pool.list_custom() if pool is not None else []
        for item in custom:
            item["favorite"] = str(item.get("id") or "") in favorites
        return {
            "favorites": sorted(favorites),
            "builtin": builtins,
            "custom": custom,
        }

    def set_cos_favorite_for_web(self, look_id: str, enabled: bool) -> Dict[str, Any]:
        pool = getattr(self, "cos_pool", None)
        if pool is None:
            raise RuntimeError("COS 收藏池未初始化")
        return pool.set_favorite(look_id, enabled)

    def save_custom_cos_for_web(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        pool = getattr(self, "cos_pool", None)
        if pool is None:
            raise RuntimeError("COS 自定义池未初始化")
        return pool.save_custom(payload)

    def delete_custom_cos_for_web(self, look_id: str) -> Dict[str, Any]:
        pool = getattr(self, "cos_pool", None)
        if pool is None:
            raise RuntimeError("COS 自定义池未初始化")
        return pool.delete_custom(look_id)

    def export_cos_pool_for_web(self) -> Dict[str, Any]:
        pool = getattr(self, "cos_pool", None)
        return pool.export_data() if pool is not None else {"version": 1, "favorites": [], "custom": []}

    def import_cos_pool_for_web(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        pool = getattr(self, "cos_pool", None)
        if pool is None:
            raise RuntimeError("COS 自定义池未初始化")
        return pool.import_data(payload)

    def compare_records_for_web(self, record_ids: Iterable[Any], limit: int = 8) -> Dict[str, Any]:
        ids = list(dict.fromkeys(str(item or "").strip() for item in (record_ids or []) if str(item or "").strip()))
        if len(ids) < 2:
            raise ValueError("至少选择两条记录进行对比")
        if len(ids) > 50:
            raise ValueError("单次最多对比 50 条记录")
        records = []
        for record_id in ids:
            records.append(self.get_record_for_web(record_id))
        return compare_generation_records(records, limit=limit)
