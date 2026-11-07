"""Studio / 画布工作区 — multi-reference image iteration.

Inspired by infinite-canvas workflows, but stored server-side and generated
through Selfie's existing channel pipeline (no browser-held API keys).
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from astrbot.api import logger

from ..cos.cos_looks import build_cos_third_person_prompt, looks_like_cos_prompt
from ..core.prompt_data import PromptDataError, read_versioned_json
from ..core.utils import save_json_file, save_json_file_compact


STUDIO_FILENAME = "studio_sessions.json"
MAX_SESSIONS = 40
MAX_SLOTS = 12
MAX_RESULTS_KEEP = 24
CANVAS_MAX_NODES = 128
CANVAS_NODE_GAP_X = 340
CANVAS_NODE_GAP_Y = 220

# Built-in prompt chips / shared presets (no external GitHub sync).
# templates: only listed templates see the chip; empty = all templates.
# global=True: also appear in 画布/试画「预设」总列表.
class BuiltinPromptDataError(RuntimeError):
    """Raised when the bundled preset JSON is incomplete or invalid."""


def _load_builtin_prompts() -> List[Dict[str, Any]]:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "studio", "builtin_prompts.json")
    try:
        payload = read_versioned_json(Path(path), "内置预设")
    except PromptDataError as exc:
        raise BuiltinPromptDataError(str(exc)) from exc
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise BuiltinPromptDataError("内置预设 items 必须是非空数组")
    items: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise BuiltinPromptDataError(f"内置预设第 {index + 1} 项必须是对象")
        item = {
            "id": str(raw.get("id") or "").strip(),
            "title": str(raw.get("title") or "").strip(),
            "prompt": str(raw.get("prompt") or "").strip(),
            "templates": raw.get("templates"),
        }
        preset_id = item["id"]
        if not preset_id or preset_id in seen_ids:
            raise BuiltinPromptDataError(f"内置预设 id 缺失或重复: {preset_id or index + 1}")
        if not item["title"] or not item["prompt"]:
            raise BuiltinPromptDataError(f"内置预设缺少标题或提示词: {preset_id}")
        templates = item["templates"]
        if not isinstance(templates, list) or any(not isinstance(value, str) or not value.strip() for value in templates):
            raise BuiltinPromptDataError(f"内置预设 templates 格式错误: {preset_id}")
        item["templates"] = list(dict.fromkeys(value.strip() for value in templates))
        if "global" in raw:
            if not isinstance(raw["global"], bool):
                raise BuiltinPromptDataError(f"内置预设 global 必须是布尔值: {preset_id}")
            if raw["global"]:
                item["global"] = True
        seen_ids.add(preset_id)
        items.append(item)
    return items


BUILTIN_PROMPTS: List[Dict[str, Any]] = _load_builtin_prompts()

# These special presets can also be requested through dynamic aliases.
# Keep the aliases themselves out of the fixed preset seed so each one expands
# to one concrete prompt before image generation.
SPECIAL_PRESET_ALIAS = "特殊预设"
ACTION_PRESET_ALIAS = "动作预设"
UPPER_PRESET_ALIAS = "上身预设"
LOWER_PRESET_ALIAS = "下身预设"
SPECIAL_PRESET_GROUP_LABELS = {
    "action": "动作预设",
    "upper": "上身预设",
    "lower": "下身预设",
}
SPECIAL_PRESET_GROUP_ORDER = ("action", "upper", "lower")
ACTION_PROMPT_PRESET_IDS = frozenset(
    {
        "preset_bite_lip_glance",
        "preset_collar_touch",
        "preset_lean_close",
        "preset_waist_touch",
        "preset_ear_close",
        "preset_hand_pull_close",
        "preset_side_lift_gaze",
        "preset_back_glance",
        "preset_lowered_gaze",
        "preset_post_battle_exhaustion",
        "preset_submissive_shy_pose",
        "preset_hold_face",
        "preset_bf_view",
    }
)
UPPER_PROMPT_PRESET_IDS = frozenset(
    {
        "preset_crop_waist",
        "preset_deep_open_front",
        "preset_underbust_cutout",
        "preset_side_bust_cutout",
        "preset_cross_strap_top",
        "preset_halter_backless",
        "preset_double_waist_cutout",
        "preset_high_side_slit",
        "preset_sheer_layers",
        "preset_open_side_apron",
        "preset_open_jacket",
        "preset_split_lapel_ties",
        "preset_asymmetric_diagonal_opening",
        "preset_vertical_torso_opening",
        "preset_underbust_arc_cutout",
        "preset_single_side_open_skirt",
        "preset_split_panel_wrap",
    }
)
LOWER_PROMPT_PRESET_IDS = frozenset(
    {
        "preset_high_hip_cutout",
        "preset_front_crotch_opening",
        "preset_back_waist_cutout",
        "preset_single_hip_cutout",
        "preset_split_crotch_skirt",
        "preset_side_slit_bondage_shorts",
        "preset_sheer_lower_layers",
        "preset_low_waist_chainwear",
        "preset_back_hip_cutout_skirt",
        "preset_double_high_slit_skirt",
    }
)
# "特殊预设" is the combined pool; the dedicated aliases expose action,
# upper-body, and lower-body groups independently.
SPECIAL_PROMPT_PRESET_IDS = ACTION_PROMPT_PRESET_IDS | UPPER_PROMPT_PRESET_IDS | LOWER_PROMPT_PRESET_IDS
ALL_SPECIAL_PROMPT_PRESET_IDS = SPECIAL_PROMPT_PRESET_IDS


def _prompt_presets_by_ids(ids: frozenset[str]) -> List[Dict[str, Any]]:
    return [
        item
        for item in BUILTIN_PROMPTS
        if str(item.get("id") or "").strip() in ids
        and str(item.get("title") or "").strip()
        and str(item.get("prompt") or "").strip()
    ]


def special_prompt_presets() -> List[Dict[str, Any]]:
    """Concrete built-in prompts eligible for the dynamic special-preset alias."""
    return _prompt_presets_by_ids(SPECIAL_PROMPT_PRESET_IDS)


def action_prompt_presets() -> List[Dict[str, Any]]:
    """Concrete built-in prompts for the action-preset alias and special group."""
    return _prompt_presets_by_ids(ACTION_PROMPT_PRESET_IDS)


def upper_prompt_presets() -> List[Dict[str, Any]]:
    """Concrete upper-body clothing-structure prompts."""
    return _prompt_presets_by_ids(UPPER_PROMPT_PRESET_IDS)


def lower_prompt_presets() -> List[Dict[str, Any]]:
    """Concrete lower-body clothing-structure prompts."""
    return _prompt_presets_by_ids(LOWER_PROMPT_PRESET_IDS)


def prompt_preset_group(value: Any) -> str:
    """Return the special-preset group for a built-in or persisted preset."""
    if isinstance(value, dict):
        preset_id = str(value.get("id") or "").strip()
        name = str(value.get("name") or value.get("title") or "").strip()
    else:
        preset_id = ""
        name = str(value or "").strip()
    if preset_id in ACTION_PROMPT_PRESET_IDS:
        return "action"
    if preset_id in UPPER_PROMPT_PRESET_IDS:
        return "upper"
    if preset_id in LOWER_PROMPT_PRESET_IDS:
        return "lower"
    if name:
        for item in BUILTIN_PROMPTS:
            if str(item.get("title") or "").strip() != name:
                continue
            item_id = str(item.get("id") or "").strip()
            if item_id in ACTION_PROMPT_PRESET_IDS:
                return "action"
            if item_id in UPPER_PROMPT_PRESET_IDS:
                return "upper"
            if item_id in LOWER_PROMPT_PRESET_IDS:
                return "lower"
    return ""


def prompt_preset_group_label(value: Any) -> str:
    """Return the human-readable label for a special-preset group."""
    group = prompt_preset_group(value)
    return SPECIAL_PRESET_GROUP_LABELS.get(group, "")


def prompt_preset_display_sort_key(value: Any) -> tuple[int, int, str]:
    """Sort ordinary presets first, then special groups in a stable order."""
    if isinstance(value, dict):
        name = str(value.get("name") or value.get("title") or "").strip()
        group = str(value.get("preset_group") or value.get("special_group") or "").strip()
        if not group:
            group = prompt_preset_group(value)
    else:
        name = str(value or "").strip()
        group = prompt_preset_group(name)
    try:
        group_index = SPECIAL_PRESET_GROUP_ORDER.index(group)
    except ValueError:
        group_index = len(SPECIAL_PRESET_GROUP_ORDER)
    return (1 if group else 0, group_index if group else -1, name)


def dynamic_prompt_preset_groups() -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Aliases and their random prompt pools, longest aliases first at call sites."""
    return [
        (SPECIAL_PRESET_ALIAS, special_prompt_presets()),
        (ACTION_PRESET_ALIAS, action_prompt_presets()),
        (UPPER_PRESET_ALIAS, upper_prompt_presets()),
        (LOWER_PRESET_ALIAS, lower_prompt_presets()),
    ]


def special_prompt_preset_titles() -> frozenset[str]:
    """Titles belonging to the action and clothing-structure special pools."""
    return frozenset(
        str(item.get("title") or "").strip()
        for _alias, items in dynamic_prompt_preset_groups()
        for item in items
        if str(item.get("title") or "").strip()
    )

# P0 templates: id -> layout defaults
STUDIO_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "duo": {
        "id": "duo",
        "title": "双人合影",
        "description": "形象 + 1 同框，最常用",
        "default_title": "双人合影",
        "mode": "group",
        "aspect_ratio": "3:4",
        "resolution": "1K",
        "prompt": "两人自然并肩合影，轻松微笑，看向镜头，日常暖光",
        "use_persona_identity": True,
        "slots": [
            {"role": "identity", "label": "形象（自己）"},
            {"role": "peer", "label": "同框对象"},
            {"role": "scene", "label": "场景/道具（可选）"},
        ],
    },
    "group": {
        "id": "group",
        "title": "多人合影",
        "description": "形象 + 同框×3 + 场景",
        "default_title": "多人合影",
        "mode": "group",
        "aspect_ratio": "3:4",
        "resolution": "1K",
        "prompt": "自然并肩合影，轻松微笑，看向镜头，日常暖光",
        "use_persona_identity": True,
        "slots": [
            {"role": "identity", "label": "形象（自己）"},
            {"role": "peer", "label": "同框对象 1"},
            {"role": "peer", "label": "同框对象 2"},
            {"role": "peer", "label": "同框对象 3"},
            {"role": "scene", "label": "场景/道具（可选）"},
        ],
    },
    "selfie": {
        "id": "selfie",
        "title": "自拍 / 看看 / 看看腿",
        "description": "看看腿生成日常下装穿搭记录：腰部以下近景，上半身不入镜",
        "default_title": "自拍画布",
        "mode": "selfie",
        "aspect_ratio": "3:4",
        "resolution": "1K",
        "prompt": "看着镜头自然自拍，半身，柔和光线，轻松表情",
        "use_persona_identity": True,
        "slots": [
            {"role": "identity", "label": "形象（自己）"},
            {"role": "outfit", "label": "服装参考（可选）"},
            {"role": "pose", "label": "姿势/构图（可选）"},
        ],
    },
    "clothes": {
        "id": "clothes",
        "title": "换装 / COS",
        "description": "形象 + 服装主参考 + 配饰/场景",
        "default_title": "换装画布",
        "mode": "selfie",
        "aspect_ratio": "3:4",
        "resolution": "1K",
        "prompt": "穿着参考图服装自拍，表情自然，看向镜头，身份保持，不锁死原表情",
        "use_persona_identity": True,
        "slots": [
            {"role": "identity", "label": "形象（自己）"},
            {"role": "outfit", "label": "服装主参考"},
            {"role": "extra", "label": "配饰/材质（可选）"},
            {"role": "scene", "label": "场景（可选）"},
        ],
    },
    "i2i": {
        "id": "i2i",
        "title": "图生图精修",
        "description": "底图为主，可选风格/细节",
        "default_title": "图生图",
        "mode": "i2i",
        "aspect_ratio": "自动",
        "resolution": "1K",
        "prompt": "以底图为主稍作精修：自然表情与光线，保持人物身份与构图",
        "use_persona_identity": False,
        "slots": [
            {"role": "base", "label": "底图（主）"},
            {"role": "style", "label": "风格参考（可选）"},
            {"role": "detail", "label": "细节参考（可选）"},
        ],
    },
    "t2i": {
        "id": "t2i",
        "title": "文生图",
        "description": "纯文案，可选 1 张风格参考",
        "default_title": "文生图",
        "mode": "t2i",
        "aspect_ratio": "自动",
        "resolution": "1K",
        "prompt": "干净构图，柔和光线，主体清晰，细节完整",
        "use_persona_identity": False,
        "slots": [
            {"role": "style", "label": "风格参考（可选）"},
        ],
    },
    "blank": {
        "id": "blank",
        "title": "空白",
        "description": "无预设槽位，自行添加",
        "default_title": "空白画布",
        "mode": "t2i",
        "aspect_ratio": "自动",
        "resolution": "1K",
        "prompt": "",
        "use_persona_identity": False,
        "slots": [],
    },
}

# Stable order for UI select
STUDIO_TEMPLATE_ORDER = ["duo", "group", "selfie", "clothes", "i2i", "t2i", "blank"]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def normalize_template_id(template: str = "", *, use_group_template: Optional[bool] = None) -> str:
    text = str(template or "").strip().lower()
    if text in STUDIO_TEMPLATES:
        return text
    # legacy flag
    if use_group_template is False:
        return "blank"
    if use_group_template is True or text in {"", "default", "true", "1"}:
        # Prefer duo as the everyday default going forward
        return "duo"
    return "duo"


def list_studio_templates() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for key in STUDIO_TEMPLATE_ORDER:
        meta = STUDIO_TEMPLATES.get(key) or {}
        out.append(
            {
                "id": meta.get("id") or key,
                "title": meta.get("title") or key,
                "description": meta.get("description") or "",
                "mode": meta.get("mode") or "t2i",
                "aspect_ratio": meta.get("aspect_ratio") or "自动",
                "slot_count": len(meta.get("slots") or []),
            }
        )
    return out


def prompts_for_template(template_id: str) -> List[Dict[str, Any]]:
    """Chips for one template only — do not leak other templates' prompts."""
    tid = normalize_template_id(template_id)
    out: List[Dict[str, Any]] = []
    for item in BUILTIN_PROMPTS:
        tags = [str(x).strip() for x in (item.get("templates") or []) if str(x).strip()]
        if tags and tid not in tags:
            continue
        out.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "prompt": item.get("prompt"),
                "templates": tags,
                "global": bool(item.get("global")),
                "preset_group": prompt_preset_group(item),
                "special_group": prompt_preset_group(item),
            }
        )
    out.sort(key=prompt_preset_display_sort_key)
    return out


def global_prompt_presets() -> List[Dict[str, Any]]:
    """Builtin entries shown in the shared 预设 picker (画布/试画)."""
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in BUILTIN_PROMPTS:
        title = str(item.get("title") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if not title or not prompt:
            continue
        if not item.get("global") and item.get("templates"):
            # template-only chips stay out of global picker unless marked global
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "id": item.get("id") or title,
                "name": title,
                "title": title,
                "prompt": prompt,
                "source": "builtin",
                "templates": list(item.get("templates") or []),
                "preset_group": prompt_preset_group(item),
                "special_group": prompt_preset_group(item),
            }
        )
    out.sort(key=prompt_preset_display_sort_key)
    return out


def default_image_preset_seed() -> Dict[str, Dict[str, str]]:
    """Name -> preset dict for ImagePresetManager seed (QQ /预设 + Web)."""
    seed: Dict[str, Dict[str, str]] = {}
    for item in BUILTIN_PROMPTS:
        if not item.get("global"):
            continue
        title = str(item.get("title") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if title and prompt:
            seed[title] = {"prompt": prompt, "description": title}
    return seed


def slots_for_template(template_id: str) -> List[Dict[str, Any]]:
    meta = STUDIO_TEMPLATES.get(normalize_template_id(template_id)) or {}
    slots: List[Dict[str, Any]] = []
    for spec in meta.get("slots") or []:
        if not isinstance(spec, dict):
            continue
        slots.append(
            {
                "id": _new_id("slot"),
                "role": str(spec.get("role") or "extra"),
                "label": str(spec.get("label") or "参考"),
                "image_path": "",
                "source": "",
                "mime": "",
            }
        )
    return slots


def group_template_slots() -> List[Dict[str, Any]]:
    """Backward-compatible alias → multi-person group layout."""
    return slots_for_template("group")


def empty_session(
    title: str = "",
    *,
    template: str = "duo",
    use_group_template: Optional[bool] = None,
) -> Dict[str, Any]:
    tid = normalize_template_id(template, use_group_template=use_group_template)
    meta = STUDIO_TEMPLATES.get(tid) or STUDIO_TEMPLATES["duo"]
    slots = slots_for_template(tid)
    # input_order: skip pure optional scene at end unless it's the only content
    input_order = [s["id"] for s in slots if s.get("role") not in {"scene"}]
    if not input_order:
        input_order = [s["id"] for s in slots]
    default_title = str(meta.get("default_title") or meta.get("title") or "画布")
    session_id = _new_id("studio")
    root_id = f"node-root-{session_id}"
    root_node = {
        "id": root_id,
        "parent_id": None,
        "record_id": None,
        "title": str(title or default_title).strip() or default_title,
        "template_id": tid,
        "params": {
            "prompt": str(meta.get("prompt") or ""),
            "mode": str(meta.get("mode") or "t2i"),
            "aspect_ratio": str(meta.get("aspect_ratio") or "自动"),
            "resolution": str(meta.get("resolution") or "1K"),
            "count": 1,
        },
        "position": {"x": 0, "y": 0},
        "status": "draft",
        "result": {"media_path": "", "thumbnail_path": ""},
        "created_at": _now(),
    }
    return {
        "id": session_id,
        "title": str(title or default_title).strip() or default_title,
        "created_at": _now(),
        "updated_at": _now(),
        "template": tid,
        "slots": slots,
        "graph": {
            "prompt": str(meta.get("prompt") or ""),
            "mode": str(meta.get("mode") or "t2i"),
            "aspect_ratio": str(meta.get("aspect_ratio") or "自动"),
            "resolution": str(meta.get("resolution") or "1K"),
            "count": 1,
            "input_order": input_order,
            "use_persona_identity": bool(meta.get("use_persona_identity", True)),
        },
        "results": [],
        "last_run": None,
        "canvas": {
            "version": 1,
            "root_node_id": root_id,
            "viewport": {"x": 120, "y": 80, "zoom": 1},
            "nodes": [root_node],
            "edges": [],
        },
    }


def public_session(session: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow copy safe for web (paths only, no bytes)."""
    return copy.deepcopy(session)


class StudioStore:
    def __init__(self, data_dir: str, *, canvas_mode: str = "relationship") -> None:
        self.path = os.path.join(data_dir, STUDIO_FILENAME)
        self.canvas_mode = "creative" if str(canvas_mode or "").strip().lower() == "creative" else "relationship"
        self._lock = threading.RLock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._load_status: Dict[str, Any] = {"ok": True, "error": ""}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            self._load_status = {"ok": True, "error": "", "source": "new"}
            raw = {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self._load_status = {"ok": False, "error": f"画布存储读取失败：{type(exc).__name__}: {exc}", "source": self.path}
            logger.exception("[SelfieImage] studio storage read failed: %s", self.path)
            self._sessions = {}
            return
        items = raw.get("sessions") if isinstance(raw, dict) else None
        out: Dict[str, Dict[str, Any]] = {}
        if isinstance(items, list):
            for index, item in enumerate(items, start=1):
                if isinstance(item, dict) and item.get("id"):
                    item.setdefault("canvas_mode", self.canvas_mode)
                    self._ensure_canvas(item)
                    out[str(item["id"])] = item
                elif item not in (None, {}):
                    logger.warning("[SelfieImage] skipped invalid studio session row %s", index)
        elif isinstance(items, dict):
            for key, item in items.items():
                if isinstance(item, dict):
                    item = dict(item)
                    item.setdefault("id", key)
                    item.setdefault("canvas_mode", self.canvas_mode)
                    self._ensure_canvas(item)
                    out[str(item["id"])] = item
                else:
                    logger.warning("[SelfieImage] skipped invalid studio session %s", key)
        self._load_status = {"ok": True, "error": "", "source": self.path}
        self._sessions = out

    @staticmethod
    def _canvas_params(session: Dict[str, Any]) -> Dict[str, Any]:
        graph = session.get("graph") if isinstance(session.get("graph"), dict) else {}
        return {
            "prompt": str(graph.get("prompt") or ""),
            "mode": str(graph.get("mode") or "group"),
            "aspect_ratio": str(graph.get("aspect_ratio") or "自动"),
            "resolution": str(graph.get("resolution") or "1K"),
            "count": max(1, min(4, int(graph.get("count") or 1))) if str(graph.get("count") or "").isdigit() else 1,
        }

    @classmethod
    def _ensure_canvas(cls, session: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize new and legacy sessions into a drawable graph projection."""
        if not isinstance(session, dict):
            return {}
        canvas = session.get("canvas") if isinstance(session.get("canvas"), dict) else {}
        session_id = str(session.get("id") or "studio")
        root_id = str(canvas.get("root_node_id") or f"node-root-{session_id}")
        raw_nodes = canvas.get("nodes") if isinstance(canvas.get("nodes"), list) else []
        nodes = [dict(node) for node in raw_nodes if isinstance(node, dict) and str(node.get("id") or "").strip()]
        by_id = {str(node.get("id")): node for node in nodes}
        if root_id not in by_id:
            root = {
                "id": root_id,
                "parent_id": None,
                "record_id": None,
                "title": str(session.get("title") or "起始节点"),
                "template_id": str(session.get("template") or ""),
                "params": cls._canvas_params(session),
                "position": {"x": 0, "y": 0},
                "status": "draft",
                "result": {"media_path": "", "thumbnail_path": ""},
                "created_at": str(session.get("created_at") or _now()),
            }
            nodes.insert(0, root)
            by_id[root_id] = root
        else:
            root = by_id[root_id]
            root.setdefault("parent_id", None)
            root.setdefault("params", cls._canvas_params(session))
            root.setdefault("position", {"x": 0, "y": 0})
            root.setdefault("status", "draft")
            root.setdefault("result", {"media_path": "", "thumbnail_path": ""})

        # The legacy creation canvas has no relationship graph. Keep only its
        # root placeholder even when old result rows are present.
        is_creative = str(session.get("canvas_mode") or "").strip().lower() == "creative"
        if is_creative:
            nodes = [by_id[root_id]]
            by_id = {root_id: nodes[0]}

        # Legacy sessions have result rows but no graph. Reconstruct a simple
        # horizontal lineage in creation order; new branches can be appended
        # later without changing the persisted generation records.
        result_rows = [item for item in (session.get("results") or []) if isinstance(item, dict)]
        known_paths = {
            str((node.get("result") or {}).get("media_path") or "")
            for node in nodes
            if isinstance(node.get("result"), dict)
        }
        previous_id = root_id
        for index, result in enumerate(reversed(result_rows) if not is_creative else []):
            path = str(result.get("image_path") or "").strip()
            if not path or path in known_paths:
                continue
            task_id = str(result.get("task_id") or f"result-{index}")
            node_id = f"node-result-{task_id}-{index}"
            while node_id in by_id:
                node_id = f"{node_id}-x"
            node = {
                "id": node_id,
                "parent_id": previous_id,
                "record_id": result.get("record_id"),
                "title": "生成结果",
                "template_id": str(session.get("template") or ""),
                "params": cls._canvas_params(session),
                "position": {"x": (index + 1) * CANVAS_NODE_GAP_X, "y": 0},
                "status": "succeeded",
                "result": {"media_path": path, "thumbnail_path": path},
                "created_at": str(result.get("created_at") or _now()),
            }
            nodes.append(node)
            by_id[node_id] = node
            known_paths.add(path)
            previous_id = node_id

        edges = []
        for node in nodes:
            parent_id = str(node.get("parent_id") or "").strip()
            node_id = str(node.get("id") or "").strip()
            if parent_id and parent_id in by_id and node_id != parent_id:
                edges.append({"id": f"edge-{parent_id}-{node_id}", "source": parent_id, "target": node_id})
        viewport = canvas.get("viewport") if isinstance(canvas.get("viewport"), dict) else {}
        try:
            zoom = max(0.35, min(1.8, float(viewport.get("zoom", 1))))
        except (TypeError, ValueError):
            zoom = 1
        normalized = {
            "version": 1,
            "root_node_id": root_id,
            "viewport": {
                "x": float(viewport.get("x", 120) or 0),
                "y": float(viewport.get("y", 80) or 0),
                "zoom": zoom,
            },
            "nodes": nodes[-CANVAS_MAX_NODES:],
            "edges": edges[-CANVAS_MAX_NODES:],
        }
        session["canvas"] = normalized
        return normalized

    def storage_status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._load_status)

    def _persist(self) -> None:
        ordered = sorted(
            self._sessions.values(),
            key=lambda s: str(s.get("updated_at") or s.get("created_at") or ""),
            reverse=True,
        )
        if len(ordered) > MAX_SESSIONS:
            for drop in ordered[MAX_SESSIONS:]:
                self._sessions.pop(str(drop.get("id") or ""), None)
            ordered = ordered[:MAX_SESSIONS]
        save_json_file_compact(self.path, {"sessions": ordered, "updated_at": _now()})

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._lock:
            ordered = sorted(
                self._sessions.values(),
                key=lambda s: str(s.get("updated_at") or ""),
                reverse=True,
            )
            return [
                {
                    "id": s.get("id"),
                    "title": s.get("title"),
                    "updated_at": s.get("updated_at"),
                    "template": s.get("template"),
                    "mode": ((s.get("graph") or {}).get("mode") if isinstance(s.get("graph"), dict) else "") or "",
                    "slot_count": len(s.get("slots") or []),
                    "result_count": len(s.get("results") or []),
                    "thumb_path": next(
                        (
                            str(r.get("image_path") or "")
                            for r in (s.get("results") or [])
                            if isinstance(r, dict) and str(r.get("image_path") or "").strip()
                        ),
                        "",
                    ),
                    "last_status": str((s.get("last_run") or {}).get("status") or ""),
                    "last_run": s.get("last_run"),
                }
                for s in ordered
            ]

    def referenced_cache_paths(self) -> List[str]:
        """Return cache media paths referenced by every live canvas session."""
        with self._lock:
            paths: List[str] = []
            seen: set[str] = set()

            def add(value: Any) -> None:
                if isinstance(value, str):
                    values = [value]
                elif isinstance(value, (list, tuple, set)):
                    values = list(value)
                else:
                    return
                for item in values:
                    path = str(item or "").strip()
                    if path and path not in seen:
                        seen.add(path)
                        paths.append(path)

            for session in self._sessions.values():
                if not isinstance(session, dict):
                    continue
                for slot in session.get("slots") or []:
                    if isinstance(slot, dict):
                        add(slot.get("image_path"))
                for result in session.get("results") or []:
                    if isinstance(result, dict):
                        add(result.get("image_path"))
                last_run = session.get("last_run")
                if isinstance(last_run, dict):
                    add(last_run.get("result_paths"))
                canvas = session.get("canvas")
                if not isinstance(canvas, dict):
                    continue
                for node in canvas.get("nodes") or []:
                    if not isinstance(node, dict):
                        continue
                    result = node.get("result")
                    if isinstance(result, dict):
                        add(result.get("media_path"))
                        add(result.get("thumbnail_path"))
            return paths

    def get(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            sid = str(session_id or "").strip()
            session = self._sessions.get(sid)
            if not session:
                raise ValueError("画布会话不存在")
            self._ensure_canvas(session)
            return public_session(session)

    def canvas_graph(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            return public_session(self._ensure_canvas(session))

    def update_canvas(self, session_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            canvas = self._ensure_canvas(session)
            if not isinstance(patch, dict):
                raise ValueError("canvas 必须是对象")
            before_canvas = copy.deepcopy(canvas)
            viewport = patch.get("viewport")
            if isinstance(viewport, dict):
                try:
                    canvas["viewport"] = {
                        "x": max(-100000, min(100000, float(viewport.get("x", 0)))),
                        "y": max(-100000, min(100000, float(viewport.get("y", 0)))),
                        "zoom": max(0.35, min(1.8, float(viewport.get("zoom", 1)))),
                    }
                except (TypeError, ValueError):
                    raise ValueError("viewport 参数无效") from None
            positions = patch.get("positions")
            if isinstance(positions, dict):
                by_id = {str(node.get("id")): node for node in canvas["nodes"]}
                for node_id, position in positions.items():
                    node = by_id.get(str(node_id))
                    if not node or not isinstance(position, dict):
                        continue
                    try:
                        node["position"] = {
                            "x": max(-100000, min(100000, float(position.get("x", 0)))),
                            "y": max(-100000, min(100000, float(position.get("y", 0)))),
                        }
                    except (TypeError, ValueError):
                        continue
            node_updates = patch.get("nodes")
            if isinstance(node_updates, list):
                by_id = {str(node.get("id")): node for node in canvas["nodes"]}
                for raw in node_updates:
                    if not isinstance(raw, dict):
                        continue
                    node = by_id.get(str(raw.get("id") or "").strip())
                    if not node:
                        continue
                    if "title" in raw:
                        node["title"] = str(raw.get("title") or "节点").strip()[:80] or "节点"
                    if "template_id" in raw:
                        node["template_id"] = normalize_template_id(str(raw.get("template_id") or ""))
                    params = raw.get("params")
                    if isinstance(params, dict):
                        current = dict(node.get("params") or {})
                        for key in ("prompt", "mode", "aspect_ratio", "resolution", "count", "use_persona_identity"):
                            if key in params:
                                current[key] = params[key]
                        current["prompt"] = str(current.get("prompt") or "").strip()
                        current["mode"] = str(current.get("mode") or "group").strip() or "group"
                        current["aspect_ratio"] = str(current.get("aspect_ratio") or "自动").strip() or "自动"
                        current["resolution"] = str(current.get("resolution") or "1K").strip() or "1K"
                        try:
                            current["count"] = max(1, min(4, int(current.get("count") or 1)))
                        except (TypeError, ValueError):
                            current["count"] = 1
                        node["params"] = current
            session["canvas"] = canvas
            if canvas != before_canvas:
                session["updated_at"] = _now()
                self._persist()
            return public_session(canvas)

    def add_canvas_node(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            canvas = self._ensure_canvas(session)
            if len(canvas["nodes"]) >= CANVAS_MAX_NODES:
                raise ValueError(f"关系网最多 {CANVAS_MAX_NODES} 个节点")
            parent_id = str(payload.get("parent_id") or canvas.get("root_node_id") or "").strip()
            by_id = {str(node.get("id")): node for node in canvas["nodes"]}
            if parent_id not in by_id:
                raise ValueError("父节点不存在")
            node_id = _new_id("node")
            parent = by_id[parent_id]
            position = payload.get("position") if isinstance(payload.get("position"), dict) else {}
            node = {
                "id": node_id,
                "parent_id": parent_id,
                "record_id": None,
                "title": str(payload.get("title") or "新节点").strip()[:80] or "新节点",
                "template_id": str(payload.get("template_id") or session.get("template") or ""),
                "params": dict(payload.get("params") or self._canvas_params(session)) if isinstance(payload.get("params") or {}, dict) else self._canvas_params(session),
                "position": {
                    "x": float(position.get("x", float((parent.get("position") or {}).get("x", 0)) + CANVAS_NODE_GAP_X)),
                    "y": float(position.get("y", float((parent.get("position") or {}).get("y", 0)) + (CANVAS_NODE_GAP_Y if len(canvas["nodes"]) % 2 else -CANVAS_NODE_GAP_Y))),
                },
                "status": "draft",
                "result": {"media_path": "", "thumbnail_path": ""},
                "created_at": _now(),
            }
            canvas["nodes"].append(node)
            canvas["edges"].append({"id": f"edge-{parent_id}-{node_id}", "source": parent_id, "target": node_id})
            session["canvas"] = canvas
            session["updated_at"] = _now()
            self._persist()
            return public_session(node)

    def delete_canvas_node(self, session_id: str, node_id: str) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            canvas = self._ensure_canvas(session)
            node_id = str(node_id or "").strip()
            if node_id == str(canvas.get("root_node_id") or ""):
                raise ValueError("起始节点不能删除")
            before = len(canvas["nodes"])
            canvas["nodes"] = [node for node in canvas["nodes"] if str(node.get("id")) != node_id]
            if len(canvas["nodes"]) == before:
                raise ValueError("节点不存在")
            canvas["edges"] = [edge for edge in canvas["edges"] if edge.get("source") != node_id and edge.get("target") != node_id]
            session["canvas"] = canvas
            session["updated_at"] = _now()
            self._persist()
            return {"deleted": True, "node_id": node_id, "canvas": public_session(canvas)}

    def connect_canvas_node(self, session_id: str, node_id: str, parent_id: str) -> Dict[str, Any]:
        """Reconnect a dangling node to an earlier node in the canvas."""
        with self._lock:
            session = self._require(session_id)
            canvas = self._ensure_canvas(session)
            node_id = str(node_id or "").strip()
            parent_id = str(parent_id or "").strip()
            root_id = str(canvas.get("root_node_id") or "").strip()
            if node_id == root_id:
                raise ValueError("起始节点不能重连")
            by_id = {str(node.get("id")): node for node in canvas.get("nodes") or []}
            target = by_id.get(node_id)
            parent = by_id.get(parent_id)
            if not target or not parent:
                raise ValueError("节点不存在")
            if node_id == parent_id:
                raise ValueError("不能连接到节点自身")

            # Reject descendants as parents so the graph remains acyclic.
            children: Dict[str, List[str]] = {}
            for item in canvas.get("nodes") or []:
                child_parent = str(item.get("parent_id") or "").strip()
                child_id = str(item.get("id") or "").strip()
                if child_parent and child_id:
                    children.setdefault(child_parent, []).append(child_id)
            pending = list(children.get(node_id) or [])
            descendants = set()
            while pending:
                current = pending.pop()
                if current in descendants:
                    continue
                descendants.add(current)
                pending.extend(children.get(current) or [])
            if parent_id in descendants:
                raise ValueError("不能连接到当前节点的后续节点")

            target["parent_id"] = parent_id
            canvas["edges"] = [
                edge for edge in canvas.get("edges") or []
                if str(edge.get("target") or "") != node_id
            ]
            canvas["edges"].append({"id": f"edge-{parent_id}-{node_id}", "source": parent_id, "target": node_id})
            session["canvas"] = canvas
            session["updated_at"] = _now()
            self._persist()
            return {"connected": True, "node_id": node_id, "parent_id": parent_id, "canvas": public_session(canvas)}

    def create(
        self,
        title: str = "",
        *,
        template: str = "duo",
        use_group_template: Optional[bool] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            session = empty_session(title, template=template, use_group_template=use_group_template)
            session["canvas_mode"] = self.canvas_mode
            self._sessions[session["id"]] = session
            self._persist()
            return public_session(session)

    def copy_session(
        self,
        session_id: str,
        *,
        title: str = "",
        valid_slot_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Copy editable canvas state without carrying execution artifacts."""
        with self._lock:
            source = self._require(session_id)
            copied = empty_session(
                title or f"{str(source.get('title') or '未命名画布').strip()} · 副本",
                template=str(source.get("template") or "duo"),
            )
            copied["graph"] = copy.deepcopy(source.get("graph") or {})
            copied["graph"]["input_order"] = list(copied["graph"].get("input_order") or [])
            keep = set(str(item) for item in valid_slot_ids) if valid_slot_ids is not None else None
            slots: List[Dict[str, Any]] = []
            id_map: Dict[str, str] = {}
            for raw_slot in source.get("slots") or []:
                if not isinstance(raw_slot, dict):
                    continue
                old_id = str(raw_slot.get("id") or "").strip()
                if keep is not None and old_id not in keep:
                    continue
                new_slot = copy.deepcopy(raw_slot)
                new_id = _new_id("slot")
                id_map[old_id] = new_id
                new_slot["id"] = new_id
                new_slot.pop("source_record_id", None)
                slots.append(new_slot)
            copied["slots"] = slots[:MAX_SLOTS]
            copied["graph"]["input_order"] = [id_map[item] for item in copied["graph"].get("input_order", []) if item in id_map]
            copied["results"] = []
            copied["last_run"] = None
            copied["updated_at"] = _now()
            self._sessions[copied["id"]] = copied
            self._persist()
            return public_session(copied)

    def delete(self, session_id: str) -> None:
        with self._lock:
            sid = str(session_id or "").strip()
            if sid not in self._sessions:
                raise ValueError("画布会话不存在")
            del self._sessions[sid]
            self._persist()

    def update_graph(self, session_id: str, graph_patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            graph = dict(session.get("graph") or {})
            if not isinstance(graph_patch, dict):
                raise ValueError("graph 必须是对象")
            for key in (
                "prompt",
                "mode",
                "aspect_ratio",
                "resolution",
                "count",
                "input_order",
                "use_persona_identity",
            ):
                if key in graph_patch:
                    graph[key] = graph_patch[key]
            graph["prompt"] = str(graph.get("prompt") or "").strip()
            graph["mode"] = str(graph.get("mode") or "group").strip() or "group"
            graph["aspect_ratio"] = str(graph.get("aspect_ratio") or "自动").strip() or "自动"
            graph["resolution"] = str(graph.get("resolution") or "1K").strip() or "1K"
            try:
                graph["count"] = max(1, min(4, int(graph.get("count") or 1)))
            except Exception:
                graph["count"] = 1
            order = graph.get("input_order") or []
            if not isinstance(order, list):
                order = []
            graph["input_order"] = [str(x) for x in order if str(x).strip()]
            graph["use_persona_identity"] = bool(graph.get("use_persona_identity", True))
            session["graph"] = graph
            if "title" in graph_patch and str(graph_patch.get("title") or "").strip():
                session["title"] = str(graph_patch.get("title")).strip()[:80]
            if "template" in graph_patch and str(graph_patch.get("template") or "").strip():
                # metadata only — do not rebuild slots on graph save
                session["template"] = normalize_template_id(str(graph_patch.get("template")))
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def set_slot_image(
        self,
        session_id: str,
        slot_id: str,
        *,
        image_path: str,
        source: str = "upload",
        mime: str = "",
        label: str = "",
        source_record_id: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            slot = self._find_slot(session, slot_id)
            slot["image_path"] = str(image_path or "").strip()
            slot["source"] = str(source or "upload").strip()
            slot["mime"] = str(mime or "").strip()
            if source_record_id:
                slot["source_record_id"] = str(source_record_id).strip()[:128]
            else:
                slot.pop("source_record_id", None)
            if label:
                slot["label"] = str(label).strip()[:40]
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def clear_slot(self, session_id: str, slot_id: str) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            slot = self._find_slot(session, slot_id)
            slot["image_path"] = ""
            slot["source"] = ""
            slot["mime"] = ""
            slot.pop("source_record_id", None)
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def add_slot(self, session_id: str, role: str = "extra", label: str = "") -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            slots = list(session.get("slots") or [])
            if len(slots) >= MAX_SLOTS:
                raise ValueError(f"槽位最多 {MAX_SLOTS} 个")
            slot = {
                "id": _new_id("slot"),
                "role": str(role or "extra").strip() or "extra",
                "label": str(label or "额外参考").strip()[:40] or "额外参考",
                "image_path": "",
                "source": "",
                "mime": "",
            }
            slots.append(slot)
            session["slots"] = slots
            order = list((session.get("graph") or {}).get("input_order") or [])
            order.append(slot["id"])
            session.setdefault("graph", {})["input_order"] = order
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def reorder_slots(self, session_id: str, order: List[str]) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            ids = [str(x) for x in (order or []) if str(x).strip()]
            by_id = {str(s.get("id")): s for s in (session.get("slots") or []) if isinstance(s, dict)}
            if not ids:
                raise ValueError("顺序不能为空")
            for sid in ids:
                if sid not in by_id:
                    raise ValueError(f"未知槽位 {sid}")
            rest = [s for sid, s in by_id.items() if sid not in ids]
            session["slots"] = [by_id[sid] for sid in ids] + rest
            session.setdefault("graph", {})["input_order"] = ids
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def attach_run_start(self, session_id: str, task_id: str, summary: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            session["last_run"] = {
                "task_id": task_id,
                "status": "running",
                "started_at": _now(),
                "summary": dict(summary or {}),
                "error": "",
                "result_paths": [],
            }
            canvas = self._ensure_canvas(session)
            target_id = str((summary or {}).get("target_node_id") or "").strip()
            by_id = {str(node.get("id")): node for node in canvas.get("nodes") or []}
            target = by_id.get(target_id)
            if target:
                target["status"] = "running"
                target["error"] = ""
                if str((summary or {}).get("template") or "").strip():
                    target["template_id"] = normalize_template_id(str(summary.get("template")))
                if isinstance((summary or {}).get("graph_params"), dict):
                    target["params"] = dict(summary["graph_params"])
                target["task_id"] = task_id
            session["canvas"] = canvas
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def attach_run_finish(
        self,
        session_id: str,
        task_id: str,
        *,
        success: bool,
        error: str = "",
        result_paths: Optional[List[str]] = None,
        used_model: str = "",
        source_asset_ids: Optional[List[str]] = None,
        status: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            paths = [str(p) for p in (result_paths or []) if str(p).strip()]
            last = dict(session.get("last_run") or {})
            last.update(
                {
                    "task_id": task_id,
                    "status": status if status in {"succeeded", "partial_success", "failed", "delivery_failed", "cancelled"} else ("succeeded" if success else "failed"),
                    "finished_at": _now(),
                    "error": str(error or ""),
                    "result_paths": paths,
                    "used_model": str(used_model or ""),
                }
            )
            session["last_run"] = last
            # Keep generated paths visible for partial or delivery-failed
            # terminal states as well; ``success=False`` only describes the
            # overall operation, not whether an artifact exists.
            if paths and (success or status in {"partial_success", "delivery_failed"}):
                # Normalize before adding result rows so legacy reconstruction
                # cannot create a duplicate node for the current task.
                canvas = self._ensure_canvas(session)
                results = list(session.get("results") or [])
                asset_ids = [str(item).strip() for item in (source_asset_ids or []) if str(item).strip()][:24]
                created_results: List[Dict[str, Any]] = []
                for path in paths:
                    result = {
                        "id": _new_id("res"),
                        "image_path": path,
                        "created_at": _now(),
                        "task_id": task_id,
                        "used_model": used_model,
                        "studio_session_id": session_id,
                    }
                    if asset_ids:
                        result["source_asset_ids"] = list(asset_ids)
                    results.insert(
                        0,
                        result,
                    )
                    created_results.append(result)
                session["results"] = results[:MAX_RESULTS_KEEP]
                by_id = {str(node.get("id")): node for node in canvas["nodes"]}
                summary = last.get("summary") if isinstance(last.get("summary"), dict) else {}
                if str(summary.get("canvas_mode") or "").strip().lower() == "creative":
                    # The legacy creation canvas keeps a flat result list. Its
                    # sessions deliberately do not receive relationship edges
                    # or generated nodes from the infinite canvas namespace.
                    session["canvas"] = canvas
                    session["updated_at"] = _now()
                    self._persist()
                    return public_session(session)
                parent_id = str(summary.get("parent_node_id") or canvas.get("root_node_id") or "").strip()
                if parent_id not in by_id:
                    parent_id = str(canvas.get("root_node_id") or "")
                params = summary.get("graph_params") if isinstance(summary.get("graph_params"), dict) else self._canvas_params(session)
                sibling_count = sum(1 for node in canvas["nodes"] if str(node.get("parent_id") or "") == parent_id)
                target_id = str(summary.get("target_node_id") or "").strip()
                target = by_id.get(target_id)
                if target and str(target.get("status") or "") in {"draft", "queued", "running"} and created_results:
                    first = created_results.pop(0)
                    target.update(
                        {
                            "record_id": first.get("id"),
                            "title": "生成结果",
                            "template_id": str(summary.get("template") or session.get("template") or ""),
                            "params": dict(params),
                            "status": "succeeded",
                            "error": "",
                            "task_id": task_id,
                            "result": {"media_path": first.get("image_path") or "", "thumbnail_path": first.get("image_path") or ""},
                        }
                    )
                    parent_id = target_id
                for index, result in enumerate(created_results):
                    node_id = f"node-task-{task_id}-{index}"
                    if node_id in by_id:
                        continue
                    parent = by_id.get(parent_id) or {}
                    parent_pos = parent.get("position") if isinstance(parent.get("position"), dict) else {}
                    node = {
                        "id": node_id,
                        "parent_id": parent_id or None,
                        "record_id": result.get("id"),
                        "title": "生成结果" if len(created_results) == 1 else f"生成结果 {index + 1}",
                        "template_id": str(summary.get("template") or session.get("template") or ""),
                        "params": dict(params),
                        "position": {
                            "x": float(parent_pos.get("x", 0) or 0) + CANVAS_NODE_GAP_X,
                            "y": float(parent_pos.get("y", 0) or 0) + (sibling_count + index - 0.5) * CANVAS_NODE_GAP_Y,
                        },
                        "status": "succeeded",
                        "result": {"media_path": result.get("image_path") or "", "thumbnail_path": result.get("image_path") or ""},
                        "created_at": result.get("created_at") or _now(),
                    }
                    canvas["nodes"].append(node)
                    by_id[node_id] = node
                    if parent_id:
                        canvas["edges"].append({"id": f"edge-{parent_id}-{node_id}", "source": parent_id, "target": node_id})
                canvas["nodes"] = canvas["nodes"][-CANVAS_MAX_NODES:]
                canvas["edges"] = canvas["edges"][-CANVAS_MAX_NODES:]
                session["canvas"] = canvas
            else:
                canvas = self._ensure_canvas(session)
            summary = last.get("summary") if isinstance(last.get("summary"), dict) else {}
            target_id = str(summary.get("target_node_id") or "").strip()
            target = next(
                (node for node in canvas.get("nodes") or [] if str(node.get("id") or "") == target_id),
                None,
            )
            if target and str(target.get("status") or "") in {"draft", "queued", "running"}:
                target["status"] = "cancelled" if status == "cancelled" else "failed"
                target["error"] = str(error or ("任务已取消" if status == "cancelled" else "生成失败"))
                target["task_id"] = task_id
            session["canvas"] = canvas
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def promote_result_to_slot(self, session_id: str, result_id: str, slot_id: str) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            result = None
            for item in session.get("results") or []:
                if str(item.get("id")) == str(result_id):
                    result = item
                    break
            if not result:
                raise ValueError("结果不存在")
            path = str(result.get("image_path") or "").strip()
            if not path:
                raise ValueError("结果没有图片")
            slot = self._find_slot(session, slot_id)
            slot["image_path"] = path
            slot["source"] = "generated"
            slot.pop("source_record_id", None)
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def promote_result_to_role(
        self,
        session_id: str,
        result_id: str,
        role: str,
        *,
        create_if_missing: bool = True,
    ) -> Dict[str, Any]:
        """Put a result into the first slot of role; optionally create that slot."""
        role_key = str(role or "").strip().lower() or "extra"
        role_labels = {
            "identity": "形象",
            "base": "底图",
            "outfit": "服装",
            "peer": "同框",
            "pose": "姿势",
            "scene": "场景",
            "style": "风格",
            "detail": "细节",
            "extra": "额外参考",
        }
        with self._lock:
            session = self._require(session_id)
            result = None
            for item in session.get("results") or []:
                if str(item.get("id")) == str(result_id):
                    result = item
                    break
            if not result:
                raise ValueError("结果不存在")
            path = str(result.get("image_path") or "").strip()
            if not path:
                raise ValueError("结果没有图片")
            slot = next(
                (
                    s
                    for s in (session.get("slots") or [])
                    if isinstance(s, dict) and str(s.get("role") or "") == role_key
                ),
                None,
            )
            if not slot and create_if_missing:
                slots = list(session.get("slots") or [])
                if len(slots) >= MAX_SLOTS:
                    raise ValueError(f"槽位最多 {MAX_SLOTS} 个")
                slot = {
                    "id": _new_id("slot"),
                    "role": role_key,
                    "label": role_labels.get(role_key, role_key),
                    "image_path": "",
                    "source": "",
                    "mime": "",
                }
                slots.append(slot)
                session["slots"] = slots
                order = list((session.get("graph") or {}).get("input_order") or [])
                order.append(slot["id"])
                session.setdefault("graph", {})["input_order"] = order
            if not slot:
                raise ValueError(f"没有「{role_labels.get(role_key, role_key)}」槽位")
            slot["image_path"] = path
            slot["source"] = "generated"
            slot.pop("source_record_id", None)
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

    def set_slot_from_cache_path(
        self,
        session_id: str,
        slot_id: str,
        image_path: str,
        *,
        source: str = "record",
        mime: str = "",
    ) -> Dict[str, Any]:
        return self.set_slot_image(
            session_id,
            slot_id,
            image_path=image_path,
            source=source,
            mime=mime,
        )

    def _require(self, session_id: str) -> Dict[str, Any]:
        sid = str(session_id or "").strip()
        session = self._sessions.get(sid)
        if not session:
            raise ValueError("画布会话不存在")
        return session

    def _find_slot(self, session: Dict[str, Any], slot_id: str) -> Dict[str, Any]:
        sid = str(slot_id or "").strip()
        for slot in session.get("slots") or []:
            if isinstance(slot, dict) and str(slot.get("id")) == sid:
                return slot
        raise ValueError("槽位不存在")


def resolve_slot_refs_for_run(
    session: Dict[str, Any],
    *,
    persona_ref: Optional[Dict[str, Any]],
    load_path_bytes,
) -> Tuple[List[Tuple[bytes, str]], List[str]]:
    """Return (refs as (bytes,mime), ordered slot ids used).

    load_path_bytes(rel_path) -> Optional[Tuple[bytes, mime]]
    """
    graph = session.get("graph") or {}
    slots = {str(s.get("id")): s for s in (session.get("slots") or []) if isinstance(s, dict)}
    order = [str(x) for x in (graph.get("input_order") or []) if str(x) in slots]
    if not order:
        order = [
            str(s.get("id"))
            for s in (session.get("slots") or [])
            if s.get("image_path") or s.get("role") in {"identity", "base"}
        ]

    refs: List[Tuple[bytes, str]] = []
    used: List[str] = []
    use_persona = bool(graph.get("use_persona_identity", True))
    identity_filled = False

    for sid in order:
        slot = slots.get(sid) or {}
        role = str(slot.get("role") or "")
        path = str(slot.get("image_path") or "").strip()
        if role in {"identity", "base"} and not path and use_persona and persona_ref and persona_ref.get("data"):
            refs.append((persona_ref["data"], str(persona_ref.get("mime_type") or "image/png")))
            used.append(sid)
            identity_filled = True
            continue
        if not path:
            continue
        loaded = load_path_bytes(path)
        if not loaded:
            continue
        data, mime = loaded
        if not data:
            continue
        refs.append((data, mime or "image/png"))
        used.append(sid)
        if role in {"identity", "base"}:
            identity_filled = True

    mode = str(graph.get("mode") or "group")
    if mode in {"group", "selfie"} and use_persona and not identity_filled and persona_ref and persona_ref.get("data"):
        refs.insert(0, (persona_ref["data"], str(persona_ref.get("mime_type") or "image/png")))

    return refs, used


def build_studio_action(
    session: Dict[str, Any], *, appearance_type: str = "auto", has_external_reference: bool = False,
) -> str:
    """Build generation action text from graph mode + prompt."""
    graph = session.get("graph") or {}
    prompt = str(graph.get("prompt") or "").strip()
    mode = str(graph.get("mode") or "group").strip().lower()
    template = str(session.get("template") or "").strip().lower()
    if mode == "group" or template in {"duo", "group"}:
        from ..prompts.group_prompt_pool import has_group_composition_contract
        from ..prompts.selfie_actions import build_group_selfie_action

        if has_group_composition_contract(prompt):
            return prompt
        return build_group_selfie_action(
            prompt, has_refs=True, has_identity_reference=True, appearance_type=appearance_type,
        )
    if mode == "selfie" or template in {"selfie", "clothes"}:
        if looks_like_cos_prompt(prompt):
            # COS pool entries may contain legacy mirror/selfie wording.  The
            # canvas must emit the same third-person contract as /看看COS,
            # otherwise the clothes template adds a phone-selfie instruction.
            return build_cos_third_person_prompt(prompt)
        from ..prompts.prompt_templates import has_daily_staging_contract
        from ..prompts.selfie_actions import build_selfie_look_action

        if has_daily_staging_contract(prompt) or "【legs:outfit】" in prompt or re.match(r"^/?看看腿", prompt):
            return prompt
        if template == "clothes":
            prompt = f"服装按参考图的完整穿搭换装。{prompt}".strip()
        return build_selfie_look_action(
            prompt, has_refs=has_external_reference, appearance_type=appearance_type,
        )
    return prompt or "看着镜头自然自拍"
