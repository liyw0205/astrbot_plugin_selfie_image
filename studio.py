"""Studio / 画布工作区 — multi-reference image iteration.

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

from .utils import load_json_file, save_json_file

STUDIO_FILENAME = "studio_sessions.json"
MAX_SESSIONS = 40
MAX_SLOTS = 12
MAX_RESULTS_KEEP = 24

# Built-in prompt chips / shared presets (no external GitHub sync).
# templates: only listed templates see the chip; empty = all templates.
# global=True: also appear in 画布/试画「预设」总列表.
BUILTIN_PROMPTS: List[Dict[str, Any]] = [
    {
        "id": "duo_warm",
        "title": "双人温馨",
        "prompt": "两人自然并肩合影，轻松微笑，看向镜头，日常暖光",
        "templates": ["duo", "group"],
    },
    {
        "id": "duo_fun",
        "title": "双人活泼",
        "prompt": "双人轻松搞怪合影，比心或比耶，氛围愉快，看向镜头",
        "templates": ["duo", "group"],
    },
    {
        "id": "group_warm",
        "title": "多人温馨",
        "prompt": "自然并肩合影，轻松微笑，看向镜头，日常暖光",
        "templates": ["group"],
    },
    {
        "id": "group_fun",
        "title": "多人活泼",
        "prompt": "轻松搞怪合影，比心或比耶，氛围愉快，看向镜头",
        "templates": ["group"],
    },
    {
        "id": "selfie_soft",
        "title": "自拍柔光",
        "prompt": "看着镜头自然自拍，半身，柔和光线，轻松表情",
        "templates": ["selfie"],
    },
    {
        "id": "selfie_mirror",
        "title": "镜前自拍",
        "prompt": "镜前半身自拍，自然看镜头，日常居家光线",
        "templates": ["selfie"],
    },
    {
        "id": "clothes_cos",
        "title": "换装COS",
        "prompt": "穿着参考图服装自拍，表情自然，看向镜头，身份保持，不锁死原表情",
        "templates": ["clothes"],
    },
    {
        "id": "clothes_daily",
        "title": "日常换装",
        "prompt": "换上参考服装的日常半身自拍，自然微笑，看向镜头",
        "templates": ["clothes"],
    },
    {
        "id": "i2i_refine",
        "title": "精修表情",
        "prompt": "以底图为主稍作精修：自然表情与光线，保持人物身份与构图",
        "templates": ["i2i"],
    },
    {
        "id": "i2i_light",
        "title": "改光线",
        "prompt": "保持主体与构图，优化光线与色调，更干净自然",
        "templates": ["i2i"],
    },
    {
        "id": "t2i_soft",
        "title": "柔和插画感",
        "prompt": "干净构图，柔和光线，主体清晰，细节完整",
        "templates": ["t2i", "blank"],
    },
    {
        "id": "window",
        "title": "窗边柔光",
        "prompt": "窗边柔和自然光，半身，轻松表情，干净背景",
        "templates": ["selfie", "clothes"],
    },
    {
        "id": "cafe",
        "title": "咖啡店",
        "prompt": "咖啡馆座位，暖色灯光，轻松日常",
        "templates": ["selfie", "duo"],
    },
    {
        "id": "look_you",
        "title": "日常他拍",
        "prompt": "朋友随手拍的日常半身照，自然看镜头，生活感",
        "templates": ["selfie"],
    },
    # Shared style presets (画布芯片 + 默认 /预设 名)
    {
        "id": "preset_hold_face",
        "title": "捧脸",
        "prompt": (
            "男生第一视角：女友累了，男生用一只手捧住她的脸颊，高颜值真实人类女孩，"
            "俯拍镜头且只能看到男友的手和手臂，女孩眼神朦胧却饱含爱意，头发凌乱，"
            "房间光线昏暗，iPhone随手抓拍的生活化原生质感"
        ),
        "templates": ["selfie", "duo", "i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_to_real",
        "title": "变真人",
        "prompt": (
            "将参考图中的二次元/插画角色转换为真实人像照片，保留角色的年龄感、表情、发型、"
            "服装配色、气质和姿势，真实面部结构，自然皮肤纹理，真实摄影光影，电影级写实风格，高质量人像摄影"
        ),
        "templates": ["i2i", "clothes", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_jelly",
        "title": "果冻化",
        "prompt": "将第1张图片中的人物处理成果冻风效果，整体呈现Q弹果冻质感，色彩饱和度略高，表面有细微光泽感，原比例。",
        "templates": ["i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_realistic",
        "title": "真人化",
        "prompt": (
            "把参考图中的角色转化为真实人物照片风格，保留原角色的五官特征、发型、服装元素、气质和动作，"
            "真实皮肤质感，自然光线，电影感摄影，真实镜头景深，高细节，写实风格，避免夸张变形"
        ),
        "templates": ["i2i", "clothes", "t2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_cos",
        "title": "变COS",
        "prompt": (
            "把参考图中的人物改造成高质量真人 COSPLAY 摄影风格，保留角色核心特征、发型、服装配色和标志性元素，"
            "精致妆容，真实布料材质，摄影棚灯光，动漫展写真感，高清细节，专业摄影"
        ),
        "templates": ["clothes", "i2i", "selfie", "blank"],
        "global": True,
    },
    {
        "id": "preset_manga_cover",
        "title": "漫画封面",
        "prompt": (
            "把画面改造成高质量漫画封面风格，保留主体特征，强烈构图，精致线稿，鲜明色彩，动态光影，"
            "干净背景，可加入装饰性标题排版但不要乱码文字，高质量插画"
        ),
        "templates": ["t2i", "i2i", "blank"],
        "global": True,
    },
    {
        "id": "preset_id_photo",
        "title": "证件照",
        "prompt": (
            "把参考图中的人物改造成真实标准证件照风格，正面视角，干净背景，均匀光线，自然表情，"
            "真实皮肤质感，清晰五官，正式衣着，高清真实摄影"
        ),
        "templates": ["i2i", "selfie", "blank"],
        "global": True,
    },
    {
        "id": "preset_bf_view",
        "title": "男友视角",
        "prompt": (
            "Girlfriend is drunk,  a beautiful 真人女孩,  In a room with purple ambient lighting, "
            "she sits on the bed. her eyes are hazy but full of love, Messy hair, ensure that the hair color "
            "of the girl in the picture remains unchanged. the room is dimly lit, she looked at the camera. "
            "amateurish iPhone shot. Depict the shadow effect in the picture correctly, adjust the shading "
            "of the glasses section to be appropriate"
        ),
        "templates": ["selfie", "duo", "i2i", "blank"],
        "global": True,
    },
]

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
        "description": "看看腿会按姿势搭配光腿神器、白丝或黑丝",
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
            }
        )
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
            }
        )
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
    return {
        "id": _new_id("studio"),
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
            "channel_policy": "priority",
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

    def get(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            sid = str(session_id or "").strip()
            session = self._sessions.get(sid)
            if not session:
                raise ValueError("画布会话不存在")
            return public_session(session)

    def create(
        self,
        title: str = "",
        *,
        template: str = "duo",
        use_group_template: Optional[bool] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            session = empty_session(title, template=template, use_group_template=use_group_template)
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
            path = str(result.get("image_path") or "").strip()
            if not path:
                raise ValueError("结果没有图片")
            slot = self._find_slot(session, slot_id)
            slot["image_path"] = path
            slot["source"] = "generated"
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


def build_studio_action(session: Dict[str, Any]) -> str:
    """Build generation action text from graph mode + prompt."""
    graph = session.get("graph") or {}
    prompt = str(graph.get("prompt") or "").strip()
    mode = str(graph.get("mode") or "group").strip().lower()
    template = str(session.get("template") or "").strip().lower()
    if mode == "group" or template in {"duo", "group"}:
        base = (
            "合影 / 合照 / 同框。AI 自己必须作为画面主角之一，与参考图对象自然同框。"
            "身份锁脸型五官发型体态；表情按合影氛围自然重画。"
            "非人物参考拟人时无明确性别默认成年女性。"
        )
        return f"{base} 用户补充要求：{prompt}。" if prompt else base
    if mode == "selfie" or template in {"selfie", "clothes"}:
        if template == "clothes" or "换装" in prompt or "COS" in prompt.upper() or "cos" in prompt:
            base = "换装/穿搭自拍：服装来自参考，身份保持，表情眼神按本次场景自然重画，看向镜头。"
        else:
            base = "看着镜头自然自拍，展示你现在的样子。"
        return f"{base} {prompt}".strip() if prompt else base
    return prompt or "看着镜头自然自拍"
