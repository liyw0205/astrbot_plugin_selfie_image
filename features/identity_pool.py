"""Persistent storage helpers for image and text-based identity pools."""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from typing import Any, Dict, Optional

from ..core.utils import detect_mime_by_bytes, ext_from_mime


POOL_LIMIT = 10
_IDENTITY_MODES = {"image", "persona"}
_BUILTIN_PERSONAS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "persona", "builtin_personas.json"
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _slot_number(value: Any, *, allow_zero: bool = False) -> int:
    if type(value) is not int:
        raise ValueError("编号必须是整数")
    lower = 0 if allow_zero else 1
    if not lower <= value <= POOL_LIMIT:
        raise ValueError(f"编号范围必须是 {lower}-{POOL_LIMIT}")
    return value


def _validate_text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是文本")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label}不能为空")
    if len(cleaned) > limit:
        raise ValueError(f"{label}不能超过 {limit} 个字符")
    return cleaned


def _load_builtin_personas() -> list[Dict[str, Any]]:
    try:
        with open(_BUILTIN_PERSONAS_PATH, "r", encoding="utf-8") as file:
            values = json.load(file)
    except (OSError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    result: list[Dict[str, Any]] = []
    for item in values[:POOL_LIMIT]:
        if not isinstance(item, dict):
            continue
        try:
            name = _validate_text(item.get("name"), "形象名称", 120)
            prompt = _validate_text(item.get("prompt"), "形象描述", 6000)
        except ValueError:
            continue
        result.append({
            "slot": len(result) + 1,
            "name": name,
            "prompt": prompt,
            "created_at": "",
        })
    return result


class IdentityPoolMixin:
    """Image/persona pool methods; the host supplies ``data``, ``save`` and a lock."""

    def _identity_pool_lock(self) -> threading.RLock:
        lock = getattr(self, "_identity_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._identity_lock = lock
        return lock

    def _managed_image_path(self, path: Any) -> bool:
        raw = str(path or "").strip()
        if not raw:
            return False
        try:
            root = os.path.realpath(self.image_dir)
            resolved = os.path.realpath(raw)
            return os.path.commonpath((root, resolved)) == root
        except (OSError, ValueError):
            return False

    def _find_image(self, slot: int) -> Optional[Dict[str, Any]]:
        return next(
            (item for item in self.data.get("image_pool", []) if item.get("slot") == slot),
            None,
        )

    def _find_persona(self, slot: int) -> Optional[Dict[str, Any]]:
        return next(
            (item for item in self.data.get("persona_pool", []) if item.get("slot") == slot),
            None,
        )

    def _sync_legacy_image_fields(self) -> None:
        slot = self.data.get("active_image_slot", 0)
        item = self._find_image(slot) if slot else None
        self.data["ref_image_path"] = str(item.get("path") or "") if item else ""
        self.data["ref_mime_type"] = str(item.get("mime_type") or "image/png") if item else "image/png"

    def _identity_snapshot(self) -> Dict[str, Any]:
        return copy.deepcopy(self.data)

    def _commit_identity_change(self, before: Dict[str, Any]) -> None:
        self.data["updated_at"] = _now()
        self._sync_legacy_image_fields()
        try:
            self.save()
        except Exception:
            self.data = before
            raise

    def _allocate_slot(self, items: list[Dict[str, Any]]) -> int:
        used = {item.get("slot") for item in items}
        for slot in range(1, POOL_LIMIT + 1):
            if slot not in used:
                return slot
        raise ValueError("形象池已满，最多 10 个")

    def _normalize_identity_pools(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        image_pool: list[Dict[str, Any]] = []
        raw_images = raw.get("image_pool")
        if isinstance(raw_images, list):
            seen: set[int] = set()
            for item in raw_images:
                if not isinstance(item, dict):
                    continue
                slot = item.get("slot")
                if type(slot) is not int or not 1 <= slot <= POOL_LIMIT or slot in seen:
                    continue
                path = str(item.get("path") or "").strip()
                if not path:
                    continue
                seen.add(slot)
                image_pool.append({
                    "slot": slot,
                    "path": path,
                    "mime_type": str(item.get("mime_type") or "image/png").strip() or "image/png",
                    "created_at": str(item.get("created_at") or "").strip(),
                })
        elif "ref_image_path" in raw and str(raw.get("ref_image_path") or "").strip():
            image_pool.append({
                "slot": 1,
                "path": str(raw.get("ref_image_path") or "").strip(),
                "mime_type": str(raw.get("ref_mime_type") or "image/png").strip() or "image/png",
                "created_at": "",
            })

        raw_personas = raw.get("persona_pool") if "persona_pool" in raw else _load_builtin_personas()
        persona_pool: list[Dict[str, Any]] = []
        if isinstance(raw_personas, list):
            seen_persona_slots: set[int] = set()
            for item in raw_personas:
                if not isinstance(item, dict):
                    continue
                slot = item.get("slot")
                if type(slot) is not int or not 1 <= slot <= POOL_LIMIT or slot in seen_persona_slots:
                    continue
                try:
                    name = _validate_text(item.get("name"), "形象名称", 120)
                    prompt = _validate_text(item.get("prompt"), "形象描述", 6000)
                except ValueError:
                    continue
                seen_persona_slots.add(slot)
                persona_pool.append({
                    "slot": slot,
                    "name": name,
                    "prompt": prompt,
                    "created_at": str(item.get("created_at") or "").strip(),
                })

        image_pool.sort(key=lambda item: item["slot"])
        persona_pool.sort(key=lambda item: item["slot"])
        active_image = raw.get("active_image_slot", 1 if len(image_pool) == 1 and "image_pool" not in raw else 0)
        if type(active_image) is not int or active_image not in {item["slot"] for item in image_pool}:
            active_image = min((item["slot"] for item in image_pool), default=0) if "image_pool" not in raw else 0
        active_persona = raw.get("active_persona_slot", 0)
        if type(active_persona) is not int or active_persona not in {item["slot"] for item in persona_pool}:
            active_persona = min((item["slot"] for item in persona_pool), default=0) if "active_persona_slot" not in raw else 0
        mode = raw.get("identity_mode", "image")
        if not isinstance(mode, str) or mode not in _IDENTITY_MODES:
            mode = "image"
        return {
            "identity_mode": mode,
            "image_pool": image_pool,
            "active_image_slot": active_image,
            "persona_pool": persona_pool,
            "active_persona_slot": active_persona,
        }

    def identity_pool_payload(self) -> Dict[str, Any]:
        with self._identity_pool_lock():
            active_image = self.data.get("active_image_slot", 0)
            images = []
            for item in self.data.get("image_pool", []):
                path = str(item.get("path") or "")
                images.append({
                    "slot": item["slot"],
                    "mime_type": item.get("mime_type") or "image/png",
                    "available": bool(path and os.path.isfile(path)),
                    "active": item["slot"] == active_image,
                })
            active_persona = self.data.get("active_persona_slot", 0)
            personas = [
                {
                    "slot": item["slot"],
                    "name": item["name"],
                    "prompt": item["prompt"],
                    "active": item["slot"] == active_persona,
                }
                for item in self.data.get("persona_pool", [])
            ]
            mode = self.data.get("identity_mode", "image")
            return {
                "identity_mode": mode,
                "image_pool": images,
                "active_image_slot": active_image,
                "persona_pool": personas,
                "active_persona_slot": active_persona,
                "pool_limit": POOL_LIMIT,
                "use_persona_pool": mode == "persona",
            }

    def get_identity_mode(self) -> str:
        return str(self.data.get("identity_mode", "image"))

    def set_identity_mode(self, value: Any) -> str:
        if not isinstance(value, str) or value not in _IDENTITY_MODES:
            raise ValueError("形象模式必须是 image 或 persona")
        with self._identity_pool_lock():
            before = self._identity_snapshot()
            self.data["identity_mode"] = value
            self._commit_identity_change(before)
            return value

    def get_active_persona(self) -> Optional[Dict[str, Any]]:
        with self._identity_pool_lock():
            slot = self.data.get("active_persona_slot", 0)
            item = self._find_persona(slot) if slot else None
            return copy.deepcopy(item) if item else None

    def add_persona(self, name: Any, prompt: Any) -> Dict[str, Any]:
        clean_name = _validate_text(name, "形象名称", 120)
        clean_prompt = _validate_text(prompt, "形象描述", 6000)
        with self._identity_pool_lock():
            before = self._identity_snapshot()
            pool = self.data.setdefault("persona_pool", [])
            slot = self._allocate_slot(pool)
            pool.append({"slot": slot, "name": clean_name, "prompt": clean_prompt, "created_at": _now()})
            pool.sort(key=lambda item: item["slot"])
            if not self.data.get("active_persona_slot"):
                self.data["active_persona_slot"] = slot
            self._commit_identity_change(before)
            return self.get()

    def update_persona(self, slot: Any, *, name: Any = None, prompt: Any = None) -> Dict[str, Any]:
        slot = _slot_number(slot)
        if name is None and prompt is None:
            raise ValueError("至少提供名称或描述")
        clean_name = _validate_text(name, "形象名称", 120) if name is not None else None
        clean_prompt = _validate_text(prompt, "形象描述", 6000) if prompt is not None else None
        with self._identity_pool_lock():
            before = self._identity_snapshot()
            item = self._find_persona(slot)
            if item is None:
                raise ValueError("形象人设不存在")
            if clean_name is not None:
                item["name"] = clean_name
            if clean_prompt is not None:
                item["prompt"] = clean_prompt
            self._commit_identity_change(before)
            return self.get()

    def select_persona(self, slot: Any) -> Dict[str, Any]:
        slot = _slot_number(slot)
        with self._identity_pool_lock():
            if self._find_persona(slot) is None:
                raise ValueError("形象人设不存在")
            before = self._identity_snapshot()
            self.data["active_persona_slot"] = slot
            self._commit_identity_change(before)
            return self.get()

    def remove_persona(self, slot: Any) -> Dict[str, Any]:
        slot = _slot_number(slot)
        with self._identity_pool_lock():
            before = self._identity_snapshot()
            pool = self.data.get("persona_pool", [])
            if self._find_persona(slot) is None:
                raise ValueError("形象人设不存在")
            self.data["persona_pool"] = [item for item in pool if item.get("slot") != slot]
            if self.data.get("active_persona_slot") == slot:
                self.data["active_persona_slot"] = min((item["slot"] for item in self.data["persona_pool"]), default=0)
            self._commit_identity_change(before)
            return self.get()

    def list_image_pool(self) -> list[Dict[str, Any]]:
        with self._identity_pool_lock():
            return copy.deepcopy(self.data.get("image_pool", []))

    def select_reference_image(self, slot: Any) -> Dict[str, Any]:
        slot = _slot_number(slot)
        with self._identity_pool_lock():
            if self._find_image(slot) is None:
                raise ValueError("形象图片不存在")
            before = self._identity_snapshot()
            self.data["active_image_slot"] = slot
            self._commit_identity_change(before)
            return self.get()

    def remove_reference_image(self, slot: Any) -> Dict[str, Any]:
        slot = _slot_number(slot)
        with self._identity_pool_lock():
            before = self._identity_snapshot()
            item = self._find_image(slot)
            if item is None:
                raise ValueError("形象图片不存在")
            self.data["image_pool"] = [entry for entry in self.data.get("image_pool", []) if entry.get("slot") != slot]
            if self.data.get("active_image_slot") == slot:
                self.data["active_image_slot"] = min((entry["slot"] for entry in self.data["image_pool"]), default=0)
            self._commit_identity_change(before)
            path = str(item.get("path") or "")
            if self._managed_image_path(path):
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass
            return self.get()

    def add_reference_image(self, data: bytes, mime_type: str = "") -> Dict[str, Any]:
        if not data:
            raise ValueError("参考图为空")
        mime = mime_type or detect_mime_by_bytes(data)
        ext = ext_from_mime(mime)
        with self._identity_pool_lock():
            before = self._identity_snapshot()
            slot = self._allocate_slot(self.data.get("image_pool", []))
            path = os.path.join(self.image_dir, f"persona_ref_{time.time_ns()}.{ext}")
            try:
                with open(path, "wb") as file:
                    file.write(data)
                self.data.setdefault("image_pool", []).append({
                    "slot": slot,
                    "path": path,
                    "mime_type": mime,
                    "created_at": _now(),
                })
                self.data["image_pool"].sort(key=lambda item: item["slot"])
                self.data["active_image_slot"] = slot
                self._commit_identity_change(before)
            except Exception:
                self.data = before
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass
                raise
            return self.get()

    def clear_active_reference_image(self) -> Dict[str, Any]:
        with self._identity_pool_lock():
            slot = self.data.get("active_image_slot", 0)
            if slot:
                return self.remove_reference_image(slot)
            return self.get()
