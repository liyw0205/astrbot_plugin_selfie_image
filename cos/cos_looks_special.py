"""JSON-backed fixed COS exports.

Special looks keep a complete prompt and therefore bypass dynamic staging. The
prompt text is owned by the JSON catalog; this module only preserves the old
registry and configuration shape.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .cos_looks_loader import get_json_legacy_group


COS_SPECIAL_LOOK_SETS: List[Dict[str, Any]] = get_json_legacy_group("special")

COS_SPECIAL_LOOK_CONFIG: Dict[str, Dict[str, Any]] = {
    str(item["id"]): {
        "mode": "special",
        "camera": str(item.get("camera") or "third"),
        "fixed_reason": "特殊 COS 使用独立的完整造型提示词",
    }
    for item in COS_SPECIAL_LOOK_SETS
    if item.get("id")
}
