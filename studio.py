"""Studio / 画布工作区 — multi-reference image iteration (Phase A).

Inspired by infinite-canvas workflows, but stored server-side and generated
through Selfie's existing channel pipeline (no browser-held API keys).
"""

from __future__ import annotations

import copy
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .utils import (
    data_url_to_bytes,
    detect_mime_by_bytes,
    load_json_file,
    normalize_image_mime,
    save_json_file,
)

STUDIO_FILENAME = "studio_sessions.json"
MAX_SESSIONS = 40
MAX_SLOTS = 12
MAX_RESULTS_KEEP = 24

# Built-in prompt chips (no external GitHub sync).
BUILTIN_PROMPTS: List[Dict[str, str]] = [
    {"id": "group_warm", "title": "温馨合影", "prompt": "自然并肩合影，轻松微笑，看向镜头，日常暖光"},
    {"id": "group_fun", "title": "活泼合影", "prompt": "轻松搞怪合影，比心或比耶，氛围愉快，看向镜头"},
    {"id": "clothes", "title": "换装自拍", "prompt": "穿着参考图服装自拍，表情自然，看向镜头，身份保持"},
    {"id": "look_you", "title": "日常他拍", "prompt": "朋友随手拍的日常半身照，自然看镜头，生活感"},
    {"id": "window", "title": "窗边柔光", "prompt": "窗边柔和自然光，半身，轻松表情，干净背景"},
    {"id": "cafe", "title": "咖啡店", "prompt": "咖啡馆座位合影或自拍，暖色灯光，轻松日常"},
]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def group_template_slots() -> List[Dict[str, Any]]:
    """Default 合影模板: identity + up to 3 peers + optional scene."""
    return [
        {"id": _new_id("slot"), "role": "identity", "label": "形象（自己）", "image_path": "", "source": "", "mime": ""},
        {"id": _new_id("slot"), "role": "peer", "label": "同框对象 1", "image_path": "", "source": "", "mime": ""},
        {"id": _new_id("slot"), "role": "peer", "label": "同框对象 2", "image_path": "", "source": "", "mime": ""},
        {"id": _new_id("slot"), "role": "peer", "label": "同框对象 3", "image_path": "", "source": "", "mime": ""},
        {"id": _new_id("slot"), "role": "scene", "label": "场景/道具（可选）", "image_path": "", "source": "", "mime": ""},
    ]


def empty_session(title: str = "", *, use_group_template: bool = True) -> Dict[str, Any]:
    slots = group_template_slots() if use_group_template else []
    input_order = [s["id"] for s in slots if s.get("role") != "scene"]
    return {
        "id": _new_id("studio"),
        "title": str(title or "合影画布").strip() or "合影画布",
        "created_at": _now(),
        "updated_at": _now(),
        "template": "group" if use_group_template else "blank",
        "slots": slots,
        "graph": {
            "prompt": "自然并肩合影，轻松微笑，看向镜头，日常暖光",
            "mode": "group",  # group | selfie | i2i | t2i
            "aspect_ratio": "自动",
            "resolution": "1K",
            "count": 1,
            "input_order": input_order,
            "use_persona_identity": True,
            "channel_policy": "priority",  # priority | random
        },
        "results": [],
        "last_run": None,
    }


def public_session(session: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow copy safe for web (paths only, no bytes)."""
    return copy.deepcopy(session)


class StudioStore:
    def __init__(self, data_dir: str) -> None:
        self.path = os.path.join(data_dir, STUDIO_FILENAME)
        self._lock = threading.RLock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        raw = load_json_file(self.path)
        items = raw.get("sessions") if isinstance(raw, dict) else None
        out: Dict[str, Dict[str, Any]] = {}
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("id"):
                    out[str(item["id"])] = item
        elif isinstance(items, dict):
            for key, item in items.items():
                if isinstance(item, dict):
                    item = dict(item)
                    item.setdefault("id", key)
                    out[str(item["id"])] = item
        self._sessions = out

    def _persist(self) -> None:
        # Keep newest first, cap count
        ordered = sorted(
            self._sessions.values(),
            key=lambda s: str(s.get("updated_at") or s.get("created_at") or ""),
            reverse=True,
        )
        if len(ordered) > MAX_SESSIONS:
            for drop in ordered[MAX_SESSIONS:]:
                self._sessions.pop(str(drop.get("id") or ""), None)
            ordered = ordered[:MAX_SESSIONS]
        save_json_file(self.path, {"sessions": ordered, "updated_at": _now()})

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
                    "slot_count": len(s.get("slots") or []),
                    "result_count": len(s.get("results") or []),
                    "last_run": s.get("last_run"),
                }
                for s in ordered
            ]

    def get(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            sid = str(session_id or "").strip()
            session = self._sessions.get(sid)
            if not session:
                raise ValueError("画布会话不存在")
            return public_session(session)

    def create(self, title: str = "", *, use_group_template: bool = True) -> Dict[str, Any]:
        with self._lock:
            session = empty_session(title, use_group_template=use_group_template)
            self._sessions[session["id"]] = session
            self._persist()
            return public_session(session)

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
                "channel_policy",
            ):
                if key in graph_patch:
                    graph[key] = graph_patch[key]
            # normalize
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
            policy = str(graph.get("channel_policy") or "priority").strip().lower()
            graph["channel_policy"] = "random" if policy == "random" else "priority"
            session["graph"] = graph
            if "title" in graph_patch and str(graph_patch.get("title") or "").strip():
                session["title"] = str(graph_patch.get("title")).strip()[:80]
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
    ) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            slot = self._find_slot(session, slot_id)
            slot["image_path"] = str(image_path or "").strip()
            slot["source"] = str(source or "upload").strip()
            slot["mime"] = str(mime or "").strip()
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
            # Keep any missing slots at end
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
    ) -> Dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            paths = [str(p) for p in (result_paths or []) if str(p).strip()]
            last = dict(session.get("last_run") or {})
            last.update(
                {
                    "task_id": task_id,
                    "status": "succeeded" if success else "failed",
                    "finished_at": _now(),
                    "error": str(error or ""),
                    "result_paths": paths,
                    "used_model": str(used_model or ""),
                }
            )
            session["last_run"] = last
            if success and paths:
                results = list(session.get("results") or [])
                for path in paths:
                    results.insert(
                        0,
                        {
                            "id": _new_id("res"),
                            "image_path": path,
                            "created_at": _now(),
                            "task_id": task_id,
                            "used_model": used_model,
                        },
                    )
                session["results"] = results[:MAX_RESULTS_KEEP]
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
            slot = self._find_slot(session, slot_id)
            slot["image_path"] = str(result.get("image_path") or "")
            slot["source"] = "generated"
            session["updated_at"] = _now()
            self._persist()
            return public_session(session)

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
        order = [str(s.get("id")) for s in (session.get("slots") or []) if s.get("image_path") or s.get("role") == "identity"]

    refs: List[Tuple[bytes, str]] = []
    used: List[str] = []
    use_persona = bool(graph.get("use_persona_identity", True))
    identity_filled = False

    for sid in order:
        slot = slots.get(sid) or {}
        role = str(slot.get("role") or "")
        path = str(slot.get("image_path") or "").strip()
        if role == "identity" and not path and use_persona and persona_ref and persona_ref.get("data"):
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
        if role == "identity":
            identity_filled = True

    # If identity never in order but persona required for group/selfie
    mode = str(graph.get("mode") or "group")
    if mode in {"group", "selfie"} and use_persona and not identity_filled and persona_ref and persona_ref.get("data"):
        refs.insert(0, (persona_ref["data"], str(persona_ref.get("mime_type") or "image/png")))

    return refs, used


def build_studio_action(session: Dict[str, Any]) -> str:
    """Build generation action text from graph mode + prompt."""
    graph = session.get("graph") or {}
    prompt = str(graph.get("prompt") or "").strip()
    mode = str(graph.get("mode") or "group").strip().lower()
    if mode == "group":
        base = (
            "合影 / 合照 / 同框。AI 自己必须作为画面主角之一，与参考图对象自然同框。"
            "身份锁脸型五官发型体态；表情按合影氛围自然重画。"
            "非人物参考拟人时无明确性别默认成年女性。"
        )
        return f"{base} 用户补充要求：{prompt}。" if prompt else base
    if mode == "selfie":
        base = "看着镜头自然自拍，展示你现在的样子。"
        return f"{base} {prompt}".strip() if prompt else base
    # i2i / t2i raw-ish
    return prompt or "看着镜头自然自拍"
