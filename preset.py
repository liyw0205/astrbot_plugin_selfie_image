"""Image preset management."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .utils import load_json_file, save_json_file


@dataclass
class ImagePreset:
    prompt: str
    aspect_ratio: str = ""
    resolution: str = ""
    description: str = ""
    extra_prompt: str = ""


class ImagePresetManager:
    def __init__(self, data_dir: str):
        self.file_path = os.path.join(data_dir, "image_presets.json")
        self.presets: Dict[str, ImagePreset] = {}
        self.load()

    def load(self) -> None:
        raw = load_json_file(self.file_path)
        if not isinstance(raw, dict):
            self.presets = {}
            return

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
        self.presets = presets

    def save(self) -> None:
        save_json_file(
            self.file_path,
            {
                name: {
                    "prompt": preset.prompt,
                    "aspect_ratio": preset.aspect_ratio,
                    "resolution": preset.resolution,
                    "description": preset.description,
                    "extra_prompt": preset.extra_prompt,
                }
                for name, preset in self.presets.items()
            },
        )

    def list(self) -> List[Tuple[str, ImagePreset]]:
        return list(self.presets.items())

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
        self.save()
        return True, f"已添加预设 {key}"

    def remove(self, name: str) -> Tuple[bool, str]:
        key = str(name or "").strip()
        if not key:
            return False, "预设名不能为空"
        if key not in self.presets:
            return False, f"预设不存在: {name}"

        self.presets.pop(key, None)
        self.save()
        return True, f"已删除预设 {name}"

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
        items = sorted(self.presets.items(), key=lambda item: len(item[0]), reverse=True)
        for name, preset in items:
            key = self._normalize_text(name)
            key_lower = key.lower()
            if lowered == key_lower:
                return name, preset, ""
            if lowered.startswith(key_lower + " "):
                return name, preset, text[len(key):].strip()
        return "", None, ""

    def _join_prompt(self, parts: List[str]) -> str:
        return " ".join(part for part in parts if str(part or "").strip()).strip()
