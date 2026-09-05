"""Image preset management."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..core.utils import load_json_file, save_json_file


# Upgrade only the exact old built-in value. User-customized presets remain untouched.
LEGACY_BUILTIN_PROMPT_REPLACEMENTS = {
    "遮脸": (
        "自然遮住部分脸部，随机选择一种遮挡方式（二选一）：用一只手轻轻挡住脸颊或嘴部，"
        "或者用手机从脸侧或下方自然遮住半张脸，不要同时出现两种遮挡。"
        "至少保留一只眼睛和部分面部轮廓可见；手、手指、手臂或手机与人物连接自然，"
        "透视、光影和遮挡关系正确，手指数量正常，不要手掌变形、手臂穿过脸部、整张脸完全被盖住、"
        "道具贴脸或僵硬摆拍。保持人物身份、服装、姿势和场景不变，像自然随手拍。"
    ),
}


@dataclass
class ImagePreset:
    prompt: str
    aspect_ratio: str = ""
    resolution: str = ""
    description: str = ""
    extra_prompt: str = ""


def _default_seed() -> Dict[str, Dict[str, str]]:
    try:
        from ..studio.studio import default_image_preset_seed

        return default_image_preset_seed()
    except Exception:
        return {}


class ImagePresetManager:
    _DELETED_BUILTINS_KEY = "__deleted_builtin_presets__"

    def __init__(self, data_dir: str):
        self.file_path = os.path.join(data_dir, "image_presets.json")
        self.presets: Dict[str, ImagePreset] = {}
        self._deleted_builtin_names: set[str] = set()
        self.load()

    def load(self) -> None:
        raw = load_json_file(self.file_path)
        if not isinstance(raw, dict):
            raw = {}

        deleted = raw.get(self._DELETED_BUILTINS_KEY)
        if isinstance(deleted, dict):
            deleted = deleted.get("names")
        self._deleted_builtin_names = {
            str(item or "").strip() for item in (deleted or []) if str(item or "").strip()
        } if isinstance(deleted, (list, tuple, set)) else set()

        presets: Dict[str, ImagePreset] = {}
        for name, value in raw.items():
            key = str(name or "").strip()
            if not key or not isinstance(value, dict):
                continue
            prompt = str(value.get("prompt") or "").strip()
            if not prompt:
                continue
            presets[key] = ImagePreset(
                prompt=prompt,
                aspect_ratio=str(value.get("aspect_ratio") or "").strip(),
                resolution=str(value.get("resolution") or "").strip(),
                description=str(value.get("description") or "").strip(),
                extra_prompt=str(value.get("extra_prompt") or value.get("extraPrompt") or "").strip(),
            )

        # Seed missing built-in defaults and upgrade only exact legacy defaults.
        dirty = False
        for name, value in _default_seed().items():
            key = str(name or "").strip()
            prompt = str((value or {}).get("prompt") or "").strip()
            if not key or not prompt:
                continue
            existing = presets.get(key)
            if key in self._deleted_builtin_names:
                continue
            if existing is None:
                presets[key] = ImagePreset(
                    prompt=prompt,
                    description=str((value or {}).get("description") or key).strip(),
                )
                dirty = True
            elif existing.prompt == LEGACY_BUILTIN_PROMPT_REPLACEMENTS.get(key):
                existing.prompt = prompt
                dirty = True

        self.presets = presets
        if dirty or (not raw and presets):
            self.save()

    def save(self) -> None:
        payload = {
            name: {
                "prompt": preset.prompt,
                "aspect_ratio": preset.aspect_ratio,
                "resolution": preset.resolution,
                "description": preset.description,
                "extra_prompt": preset.extra_prompt,
            }
            for name, preset in self.presets.items()
        }
        if self._deleted_builtin_names:
            payload[self._DELETED_BUILTINS_KEY] = {"names": sorted(self._deleted_builtin_names)}
        save_json_file(self.file_path, payload)

    def list(self) -> List[Tuple[str, ImagePreset]]:
        return list(self.presets.items())

    def is_builtin_deleted(self, name: str) -> bool:
        """Whether a seeded preset was explicitly removed by the user."""
        return str(name or "").strip() in self._deleted_builtin_names

    def list_public(self) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        for name, preset in self.list():
            rows.append(
                {
                    "name": name,
                    "title": name,
                    "prompt": preset.prompt,
                    "description": preset.description or "",
                    "aspect_ratio": preset.aspect_ratio or "",
                    "resolution": preset.resolution or "",
                    "extra_prompt": preset.extra_prompt or "",
                    "source": "user",
                }
            )
        rows.sort(key=lambda item: str(item.get("name") or ""))
        return rows

    def add(self, name: str, raw_value: str) -> Tuple[bool, str]:
        key = str(name or "").strip()
        value = str(raw_value or "").strip()
        if not key:
            return False, "预设名不能为空"
        if not value:
            return False, "预设内容不能为空"

        preset = self._parse_value(value)
        if not preset.prompt:
            return False, "预设内容不能为空"

        self.presets[key] = preset
        self._deleted_builtin_names.discard(key)
        self.save()
        return True, f"已添加预设 {key}"

    def remove(self, name: str) -> Tuple[bool, str]:
        key = str(name or "").strip()
        if not key:
            return False, "预设名不能为空"
        if key not in self.presets:
            return False, f"预设不存在: {name}"

        self.presets.pop(key, None)
        if key in _default_seed():
            self._deleted_builtin_names.add(key)
        self.save()
        return True, f"已删除预设 {name}"

    def list_management(self) -> List[Dict[str, str]]:
        """Return editable presets, including fields hidden by the picker."""
        builtin_names = set(_default_seed())
        rows: List[Dict[str, str]] = []
        for name, preset in self.list():
            rows.append(
                {
                    "name": name,
                    "title": name,
                    "prompt": preset.prompt,
                    "description": preset.description or "",
                    "aspect_ratio": preset.aspect_ratio or "",
                    "resolution": preset.resolution or "",
                    "extra_prompt": preset.extra_prompt or "",
                    "source": "builtin" if name in builtin_names else "user",
                }
            )
        rows.sort(key=lambda item: str(item.get("name") or ""))
        return rows

    def save_management(self, payload: Dict[str, object]) -> Tuple[bool, str]:
        prepared, error = self._prepare_management_payload(payload)
        if prepared is None:
            return False, error
        name, original_name, preset = prepared
        previous_presets = dict(self.presets)
        previous_deleted = set(self._deleted_builtin_names)
        if original_name and original_name != name:
            self.presets.pop(original_name, None)
            if original_name in _default_seed():
                self._deleted_builtin_names.add(original_name)
        self.presets[name] = preset
        self._deleted_builtin_names.discard(name)
        try:
            self.save()
        except Exception:
            self.presets = previous_presets
            self._deleted_builtin_names = previous_deleted
            raise
        return True, f"已保存预设 {name}"

    @staticmethod
    def _prepare_management_payload(
        payload: Dict[str, object],
    ) -> Tuple[Optional[Tuple[str, str, ImagePreset]], str]:
        if not isinstance(payload, dict):
            return None, "预设参数必须是对象"
        name = str(payload.get("name") or "").strip()
        original_name = str(payload.get("original_name") or payload.get("originalName") or "").strip()
        prompt = str(payload.get("prompt") or "").strip()
        if not name:
            return None, "预设名不能为空"
        if len(name) > 100:
            return None, "预设名不能超过 100 个字符"
        if not prompt:
            return None, "预设内容不能为空"
        if len(prompt) > 20000:
            return None, "预设内容不能超过 20000 个字符"
        return (
            (
                name,
                original_name,
                ImagePreset(
                    prompt=prompt,
                    aspect_ratio=str(payload.get("aspect_ratio") or payload.get("aspectRatio") or "").strip(),
                    resolution=str(payload.get("resolution") or "").strip(),
                    description=str(payload.get("description") or "").strip(),
                    extra_prompt=str(payload.get("extra_prompt") or payload.get("extraPrompt") or "").strip(),
                ),
            ),
            "",
        )

    def import_management(self, items: object) -> Tuple[int, str]:
        if isinstance(items, dict):
            normalized = []
            for name, value in items.items():
                if isinstance(value, dict):
                    item = dict(value)
                    item.setdefault("name", name)
                    normalized.append(item)
                elif isinstance(value, str):
                    normalized.append({"name": name, "prompt": value})
                else:
                    normalized.append(value)
            items = normalized
        if not isinstance(items, (list, tuple)):
            raise ValueError("导入内容必须包含 presets 数组或名称对象")
        errors: List[str] = []
        prepared_items: List[Tuple[str, str, ImagePreset]] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                errors.append(f"第 {index} 项不是对象")
                continue
            prepared, message = self._prepare_management_payload(item)
            if prepared is None:
                errors.append(f"第 {index} 项：{message}")
                continue
            prepared_items.append(prepared)
        if errors:
            raise ValueError("；".join(errors[:5]))
        candidate_presets = dict(self.presets)
        candidate_deleted = set(self._deleted_builtin_names)
        for name, original_name, preset in prepared_items:
            if original_name and original_name != name:
                candidate_presets.pop(original_name, None)
                if original_name in _default_seed():
                    candidate_deleted.add(original_name)
            candidate_presets[name] = preset
            candidate_deleted.discard(name)
        previous_presets = self.presets
        previous_deleted = self._deleted_builtin_names
        self.presets = candidate_presets
        self._deleted_builtin_names = candidate_deleted
        try:
            self.save()
        except Exception:
            self.presets = previous_presets
            self._deleted_builtin_names = previous_deleted
            raise
        imported = len(prepared_items)
        return imported, f"已导入 {imported} 个预设"

    def resolve(self, raw_prompt: str) -> Dict[str, str]:
        text = self._normalize_text(raw_prompt)
        if not text:
            return {"prompt": ""}

        preset_name, preset, rest = self._match_preset(text)
        if not preset:
            return {"prompt": text}

        prompt_parts = [preset.prompt]
        if preset.extra_prompt:
            prompt_parts.append(preset.extra_prompt)
        if rest:
            prompt_parts.append(rest)

        return {
            "prompt": self._join_prompt(prompt_parts),
            "aspect_ratio": preset.aspect_ratio,
            "resolution": preset.resolution,
            "preset_name": preset_name,
            "description": preset.description,
            "extra_prompt": preset.extra_prompt,
        }

    def has_preset(self, name: str) -> bool:
        return str(name or "").strip() in self.presets

    def _parse_value(self, raw_value: str) -> ImagePreset:
        if raw_value.startswith("{"):
            try:
                import json

                obj = json.loads(raw_value)
                if isinstance(obj, dict):
                    return ImagePreset(
                        prompt=str(obj.get("prompt") or "").strip(),
                        aspect_ratio=str(obj.get("aspect_ratio") or "").strip(),
                        resolution=str(obj.get("resolution") or "").strip(),
                        description=str(obj.get("description") or "").strip(),
                        extra_prompt=str(obj.get("extra_prompt") or obj.get("extraPrompt") or "").strip(),
                    )
            except Exception:
                pass
        return ImagePreset(prompt=raw_value)

    def _normalize_text(self, text: str) -> str:
        return str(text or "").strip().replace("\t", " ").replace("\n", " ").replace("\r", " ").replace("  ", " ")

    def _match_preset(self, text: str) -> Tuple[str, Optional[ImagePreset], str]:
        lowered = text.lower()
        # "特殊预设" expands to one concrete built-in structure preset at run time.
        try:
            from ..studio.studio import SPECIAL_PRESET_ALIAS, special_prompt_presets

            alias = str(SPECIAL_PRESET_ALIAS or "特殊预设").strip()
            alias_lower = alias.lower()
            if alias and (lowered == alias_lower or lowered.startswith(alias_lower + " ")):
                choices = special_prompt_presets()
                if choices:
                    selected = random.choice(choices)
                    title = str(selected.get("title") or "").strip()
                    prompt = str(selected.get("prompt") or "").strip()
                    rest = "" if lowered == alias_lower else text[len(alias) :].strip()
                    if prompt:
                        return alias, ImagePreset(
                            prompt=prompt,
                            description=f"随机选中：{title}" if title else alias,
                        ), rest
        except Exception:
            pass

        # Common aliases for built-in titles (user often types near-homophones).
        alias_map = {
            "露腰": "漏腰",
            "漏腰杀": "漏腰",
            "小蛮腰": "漏腰",
        }
        for alias, target in alias_map.items():
            alias_l = alias.lower()
            if lowered == alias_l or lowered.startswith(alias_l + " "):
                if target in self.presets:
                    rest = "" if lowered == alias_l else text[len(alias):].strip()
                    return target, self.presets[target], rest
        items = sorted(self.presets.items(), key=lambda item: len(item[0]), reverse=True)
        for name, preset in items:
            key = self._normalize_text(name)
            key_lower = key.lower()
            if lowered == key_lower:
                return name, preset, ""
            if lowered.startswith(key_lower + " "):
                return name, preset, text[len(key) :].strip()
        return "", None, ""

    def _join_prompt(self, parts: List[str]) -> str:
        return " ".join(part for part in parts if str(part or "").strip()).strip()
