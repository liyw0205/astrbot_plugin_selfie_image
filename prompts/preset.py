"""Image preset management."""

from __future__ import annotations

import os
import random
import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

from ..core.utils import load_json_file, save_json_file

def _preset_group_for_name(name: object) -> str:
    try:
        from ..studio.studio import prompt_preset_group

        return str(prompt_preset_group(str(name or "")) or "")
    except Exception:
        return ""


_PRESET_GROUP_ORDER = {"action": 0, "upper": 1, "lower": 2}
_PRESET_GROUP_LABELS = {
    "action": "动作预设",
    "upper": "上身预设",
    "lower": "下身预设",
}


def preset_group_label(name: object) -> str:
    """Return the display label for a special preset group."""
    return _PRESET_GROUP_LABELS.get(_preset_group_for_name(name), "")


def preset_display_sort_key(name: object) -> tuple[int, int, str]:
    """Keep ordinary presets first, then action/upper/lower special groups."""
    value = str(name or "").strip()
    group = _preset_group_for_name(value)
    return (1 if group else 0, _PRESET_GROUP_ORDER.get(group, len(_PRESET_GROUP_ORDER)), value)


def preset_row_display_sort_key(row: object) -> tuple[int, int, str]:
    """Sort a public preset row while honoring its already-resolved group."""
    if isinstance(row, dict):
        name = str(row.get("name") or row.get("title") or "").strip()
        group = str(row.get("preset_group") or row.get("special_group") or "").strip()
        if group:
            return (1, _PRESET_GROUP_ORDER.get(group, len(_PRESET_GROUP_ORDER)), name)
        return (0, -1, name)
    return preset_display_sort_key(row)


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


# Upgrade only the shipped hip presets that still contain the old shared
# action skeleton. Hash matching keeps same-name user presets untouched.
VIDEO_LEGACY_BUILTIN_PROMPT_HASHES = {
    "正太扭腰": "cdebddfc59f5330f1289622d568298596ab2e012a95390869b30972c9fc9c7aa",
    "左右顶胯": "cc758f2272bbe061d054982325cefc53b671e21ccdbe9daf16ea4285d7debf7c",
    "八字胯": "c3586d1301deb68bd7e46a017dcb5a4d3461a776d6cb622ce6b8f952ffeff0cd",
    "点胯坐胯": "ab367e54175710cbb3c72cad03028488260ef7f5027dfa534d5400cc11289a04",
    "坐胯": "a3da828ca1834652774d3866c99b6ea5c7470afbb1d2a84392b9aee112c49cef",
    "绕胯": "6c390da3638a98e8f29eb825a646be07f052009054b3542c64caea1b6417cae9",
}


@dataclass
class ImagePreset:
    prompt: str
    aspect_ratio: str = ""
    resolution: str = ""
    description: str = ""
    extra_prompt: str = ""
    duration: int = 0


def _default_seed() -> Dict[str, Dict[str, str]]:
    """Return image defaults while preserving a diagnostic for callers.

    ImportError/AttributeError are compatibility cases for older plugin
    layouts. Other exceptions indicate a broken built-in seed and are logged
    with a traceback instead of looking like an empty preset installation.
    """
    try:
        from ..studio.studio import default_image_preset_seed

        seed = default_image_preset_seed()
        if not isinstance(seed, dict):
            raise TypeError("default image preset seed must be a mapping")
        return seed
    except (ImportError, AttributeError):
        return {}
    except Exception:
        logger.exception("[SelfieImage] failed to load built-in image presets")
        raise


class ImagePresetManager:
    _DELETED_BUILTINS_KEY = "__deleted_builtin_presets__"
    PRESET_FILENAME = "image_presets.json"

    @staticmethod
    def _builtin_seed() -> Dict[str, Dict[str, str]]:
        return _default_seed()

    def __init__(self, data_dir: str):
        self.file_path = os.path.join(data_dir, self.PRESET_FILENAME)
        self.presets: Dict[str, ImagePreset] = {}
        self._deleted_builtin_names: set[str] = set()
        self.load_status: Dict[str, object] = {
            "ok": True,
            "source": "builtin",
            "error": "",
        }
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
                duration=self._parse_duration(value.get("duration")),
            )

        # Seed missing built-in defaults and upgrade only exact legacy defaults.
        seed = self._load_builtin_seed()
        dirty = False
        for name, value in seed.items():
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
                    duration=self._parse_duration((value or {}).get("duration")),
                )
                dirty = True
            elif existing.prompt == LEGACY_BUILTIN_PROMPT_REPLACEMENTS.get(key):
                existing.prompt = prompt
                dirty = True

        self.presets = presets
        if dirty or (not raw and presets):
            self.save()

    def _load_builtin_seed(self) -> Dict[str, Dict[str, str]]:
        try:
            seed = self._builtin_seed()
            if not isinstance(seed, dict):
                raise TypeError("built-in preset seed must be a mapping")
            self.load_status = {"ok": True, "source": "builtin", "error": ""}
            return seed
        except (ImportError, AttributeError) as exc:
            self.load_status = {
                "ok": False,
                "source": "builtin",
                "error": f"兼容性缺少内置预设：{type(exc).__name__}",
            }
            logger.warning("[SelfieImage] built-in %s presets unavailable: %s", self.PRESET_FILENAME, exc)
            return {}
        except Exception as exc:
            self.load_status = {
                "ok": False,
                "source": "builtin",
                "error": f"内置预设加载失败：{type(exc).__name__}: {exc}",
            }
            logger.exception("[SelfieImage] built-in %s presets failed", self.PRESET_FILENAME)
            return {}

    def get_load_status(self) -> Dict[str, object]:
        """Return a safe status object for management/health APIs."""
        return dict(self.load_status)

    def save(self) -> None:
        payload = {
            name: {
                "prompt": preset.prompt,
                "aspect_ratio": preset.aspect_ratio,
                "resolution": preset.resolution,
                "description": preset.description,
                "extra_prompt": preset.extra_prompt,
                "duration": preset.duration,
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
                    "duration": preset.duration,
                    "source": "user",
                    "preset_group": _preset_group_for_name(name),
                    "special_group": _preset_group_for_name(name),
                }
            )
        rows.sort(key=preset_row_display_sort_key)
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
        if key in self._load_builtin_seed():
            self._deleted_builtin_names.add(key)
        self.save()
        return True, f"已删除预设 {name}"

    def list_management(self) -> List[Dict[str, str]]:
        """Return editable presets, including fields hidden by the picker."""
        builtin_names = set(self._load_builtin_seed())
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
                    "duration": preset.duration,
                    "source": "builtin" if name in builtin_names else "user",
                    "preset_group": _preset_group_for_name(name),
                    "special_group": _preset_group_for_name(name),
                }
            )
        rows.sort(key=preset_row_display_sort_key)
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
            if original_name in self._load_builtin_seed():
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

    def _prepare_management_payload(
        self,
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
                    duration=self._parse_duration(payload.get("duration")),
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
                if original_name in self._load_builtin_seed():
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
            "duration": preset.duration,
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
                        duration=self._parse_duration(obj.get("duration")),
                    )
            except Exception:
                pass
        return ImagePreset(prompt=raw_value)

    @staticmethod
    def _parse_duration(value: object) -> int:
        try:
            return max(0, min(60, int(float(str(value or 0).strip()))))
        except Exception:
            return 0

    def _normalize_text(self, text: str) -> str:
        return str(text or "").strip().replace("\t", " ").replace("\n", " ").replace("\r", " ").replace("  ", " ")

    def _match_preset(self, text: str) -> Tuple[str, Optional[ImagePreset], str]:
        lowered = text.lower()
        # Structure aliases expand to one concrete built-in prompt at run time.
        try:
            from ..studio.studio import dynamic_prompt_preset_groups

            groups = sorted(
                dynamic_prompt_preset_groups(),
                key=lambda item: len(str(item[0] or "")),
                reverse=True,
            )
            for alias, choices in groups:
                alias = str(alias or "").strip()
                alias_lower = alias.lower()
                if not alias or not (lowered == alias_lower or lowered.startswith(alias_lower + " ")):
                    continue
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


class VideoPresetManager(ImagePresetManager):
    """独立保存视频提示词预设，沿用管理页的增删改导入导出格式。"""

    PRESET_FILENAME = "video_presets.json"
    _REMOVED_BUILTIN_NAMES = frozenset({"Whiplash完整舞蹈", "镜头推进", "转身定格"})

    def load(self) -> None:
        super().load()
        seed = self._load_builtin_seed()
        dirty = False
        for name, legacy_hash in VIDEO_LEGACY_BUILTIN_PROMPT_HASHES.items():
            existing = self.presets.get(name)
            replacement = seed.get(name) if isinstance(seed, dict) else None
            if not existing or not isinstance(replacement, dict):
                continue
            current_hash = hashlib.sha256(existing.prompt.encode("utf-8")).hexdigest()
            if current_hash != legacy_hash:
                continue
            existing.prompt = str(replacement.get("prompt") or existing.prompt).strip()
            existing.description = str(replacement.get("description") or existing.description).strip()
            existing.duration = self._parse_duration(replacement.get("duration")) or existing.duration
            dirty = True
        stale_names = [name for name in self._REMOVED_BUILTIN_NAMES if name in self.presets]
        if not stale_names and not dirty:
            return
        for name in stale_names:
            self.presets.pop(name, None)
            self._deleted_builtin_names.add(name)
        if stale_names or dirty:
            self.save()

    @staticmethod
    def _builtin_seed() -> Dict[str, Dict[str, str]]:
        try:
            from ..studio.studio import default_video_preset_seed

            seed = default_video_preset_seed()
            if not isinstance(seed, dict):
                raise TypeError("default video preset seed must be a mapping")
            return seed
        except (ImportError, AttributeError):
            return {}
