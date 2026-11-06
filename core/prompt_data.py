"""Shared readers for bundled, versioned prompt-pool JSON files."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict


class PromptDataError(RuntimeError):
    """Raised when bundled prompt data is missing or malformed."""


def read_versioned_json(path: Path, label: str, *, version: int = 1) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PromptDataError(f"无法读取{label} JSON: {path}") from exc
    if not isinstance(payload, Mapping) or payload.get("version") != version:
        raise PromptDataError(f"{label} JSON 缺少受支持的 version: {path}")
    return dict(payload)


def read_nonempty_list(payload: Mapping[str, Any], key: str, label: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise PromptDataError(f"{label}必须是非空数组: {key}")
    return value
