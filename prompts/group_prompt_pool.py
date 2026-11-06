"""Validated data for random group-photo compositions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List

from ..core.prompt_data import PromptDataError, read_nonempty_list, read_versioned_json


DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "group" / "compositions.json"


def _load_group_compositions() -> List[Dict[str, Any]]:
    payload = read_versioned_json(DATA_PATH, "合影构图池")
    items = read_nonempty_list(payload, "items", "合影构图池")
    compositions: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(items):
        if not isinstance(raw, Mapping):
            raise PromptDataError(f"合影构图第 {index + 1} 项必须是对象")
        item = dict(raw)
        item_id = str(item.get("id") or "").strip()
        if not item_id or item_id in seen:
            raise PromptDataError(f"合影构图 id 缺失或重复: {item_id or index + 1}")
        if not str(item.get("title") or "").strip() or not str(item.get("prompt") or "").strip():
            raise PromptDataError(f"合影构图缺少标题或提示词: {item_id}")
        try:
            weight = int(item.get("weight", 1))
        except (TypeError, ValueError) as exc:
            raise PromptDataError(f"合影构图权重格式错误: {item_id}") from exc
        if weight < 1:
            raise PromptDataError(f"合影构图权重必须大于零: {item_id}")
        item["id"] = item_id
        item["weight"] = weight
        item["title"] = str(item["title"]).strip()
        item["prompt"] = str(item["prompt"]).strip()
        orientation = str(item.get("orientation") or "front").strip()
        framing = str(item.get("framing") or "near").strip()
        if orientation not in {"front", "side", "back", "mixed"}:
            raise PromptDataError(f"合影朝向格式错误: {item_id}")
        if framing not in {"near", "medium", "environment", "close_medium"}:
            raise PromptDataError(f"合影景别格式错误: {item_id}")
        item["orientation"] = orientation
        item["framing"] = framing
        seen.add(item_id)
        compositions.append(item)
    return compositions


GROUP_COMPOSITIONS = _load_group_compositions()
GROUP_COMPOSITIONS_BY_ID = {str(item["id"]): item for item in GROUP_COMPOSITIONS}


def has_group_composition_contract(text: str) -> bool:
    match = re.search(r"【shot:([a-z0-9_]+)】", str(text or ""), re.I)
    return bool(match and match.group(1) in GROUP_COMPOSITIONS_BY_ID)
