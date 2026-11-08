"""NovelAI artist strings using the existing persistent preset store."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

from ..core.prompt_data import read_nonempty_list, read_versioned_json
from ..prompts.preset import ImagePresetManager


DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "nai" / "artist_presets.json"


class NaiArtistPresetManager(ImagePresetManager):
    PRESET_FILENAME = "nai_artist_presets.json"

    def __init__(self, data_dir: str):
        self.settings = read_versioned_json(DATA_PATH, "NAI 画师串")
        super().__init__(data_dir)

    @staticmethod
    def _builtin_seed() -> Dict[str, Dict[str, str]]:
        payload = read_versioned_json(DATA_PATH, "NAI 画师串")
        records = read_nonempty_list(payload, "presets", "NAI 画师串")
        seed: Dict[str, Dict[str, str]] = {}
        for row in records:
            if not isinstance(row, dict):
                raise ValueError("NAI 画师串必须是对象")
            name = str(row.get("name") or "").strip()
            prompt = str(row.get("prompt") or "").strip()
            if not name or not prompt or name in seed:
                raise ValueError(f"NAI 画师串缺少名称、提示词或名称重复: {name}")
            seed[name] = {"prompt": prompt, "description": str(row.get("description") or "")}
        return seed

    def find_artist(self, name: str) -> Tuple[str, str]:
        for key, preset in self.list():
            if key.casefold() == str(name or "").strip().casefold():
                return key, self._join_prompt([preset.prompt, preset.extra_prompt])
        return "", ""
