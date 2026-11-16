"""Shared parsing and preset expansion for image command entry points."""

from __future__ import annotations

import random
import re
from typing import Any, Dict, List, Tuple

from ..prompts.command_parser import (
    command_tokens_for_count,
    expand_cos_user_text_with_preset,
    expand_user_text_with_preset,
    extract_command_count,
    extract_template_options,
    normalize_count,
    normalize_preset_input,
    parse_count_token,
    parse_prompt_options,
    resolve_image_preset,
    split_attached_count_token,
    split_preset_command,
)


class ImagePromptService:
    """Own command parsing while keeping plugin state and hooks injectable."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def image_parser_defaults(self) -> Tuple[str, str]:
        config = self.plugin.config
        aspect = str(config.image_default_aspect_ratio or "9:16").strip() or "9:16"
        resolution = str(config.image_default_resolution or "1K").strip() or "1K"
        return aspect, resolution

    def normalize_count(self, count: Any) -> int:
        return normalize_count(count, self.plugin.config.image_max_batch_count)

    def parse_count_token(self, token: str) -> int:
        return parse_count_token(token)

    def split_attached_count_token(self, token: str) -> Tuple[str, int]:
        return split_attached_count_token(token)

    def command_tokens_for_count(self, text: str) -> List[str]:
        return command_tokens_for_count(text)

    def extract_command_count(
        self,
        text: str,
        *,
        allow_attached: bool = False,
        allow_trailing: bool = False,
    ) -> Tuple[str, int]:
        return extract_command_count(
            text,
            self.plugin.config.image_max_batch_count,
            allow_attached=allow_attached,
            allow_trailing=allow_trailing,
        )

    def render_command_template(self, text: str) -> Tuple[str, Dict[str, str], bool, List[str]]:
        """Render known variables without consuming provider-specific options."""
        cleaned, values, randomize = extract_template_options(text)
        rendered = self.plugin.render_creative_prompt(
            cleaned,
            {
                "template_values": values,
                "template_randomize": randomize,
            },
        )
        return (
            str(rendered.get("prompt") or cleaned).strip(),
            {str(key): str(value) for key, value in values.items()},
            bool(randomize),
            [str(item) for item in (rendered.get("unresolved") or []) if str(item).strip()],
        )

    def parse_prompt_options(
        self,
        text: str,
        aspect_ratio: str = "",
        resolution: str = "",
    ) -> Tuple[str, str, str]:
        default_aspect, default_resolution = self.image_parser_defaults()
        return parse_prompt_options(
            text,
            aspect_ratio,
            resolution,
            default_aspect_ratio=default_aspect,
            default_resolution=default_resolution,
        )

    def resolve_image_preset(
        self,
        prompt: str,
        aspect_ratio: str = "",
        resolution: str = "",
    ) -> Tuple[str, str, str, str, str]:
        default_aspect, default_resolution = self.image_parser_defaults()
        return resolve_image_preset(
            prompt,
            aspect_ratio,
            resolution,
            presets=self.plugin.presets,
            default_aspect_ratio=default_aspect,
            default_resolution=default_resolution,
        )

    def expand_user_text_with_preset(self, raw_text: str) -> Tuple[str, str, str, str]:
        default_aspect, default_resolution = self.image_parser_defaults()
        return expand_user_text_with_preset(
            raw_text,
            presets=self.plugin.presets,
            default_aspect_ratio=default_aspect,
            default_resolution=default_resolution,
        )

    def expand_cos_user_text_with_preset(self, raw_text: str) -> Tuple[str, str, str, str]:
        default_aspect, default_resolution = self.image_parser_defaults()
        return expand_cos_user_text_with_preset(
            raw_text,
            presets=self.plugin.presets,
            default_aspect_ratio=default_aspect,
            default_resolution=default_resolution,
        )

    def expand_cos_special_preset_variants(
        self,
        raw_text: str,
        count: int,
    ) -> Tuple[List[str], str, str, str]:
        """Expand a dynamic COS preset independently for every batch shot."""
        text = str(raw_text or "").strip()
        if not text or count <= 0:
            return [], "", "", ""
        from ..studio.studio import dynamic_prompt_preset_groups

        groups = [
            (str(alias or "").strip(), list(items or []))
            for alias, items in dynamic_prompt_preset_groups()
            if str(alias or "").strip()
        ]
        if not groups:
            return [], "", "", ""
        groups.sort(key=lambda item: len(item[0]), reverse=True)
        pattern = "|".join(re.escape(alias) for alias, _items in groups)
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            return [], "", "", ""
        matched_alias = match.group(0)
        alias_map = {alias.lower(): (alias, choices) for alias, choices in groups}
        alias, choices = alias_map.get(matched_alias.lower(), (matched_alias, []))
        choices = [dict(item) for item in choices if str(item.get("prompt") or "").strip()]
        if not choices:
            return [], "", "", ""

        variants: List[str] = []
        first_aspect = ""
        first_resolution = ""
        for index in range(max(1, int(count))):
            selected = random.choice(choices)
            selected_prompt = str(selected.get("prompt") or "").strip()
            prefix = text[: match.start()].strip()
            suffix = text[match.end() :].strip()
            remaining = " ".join(part for part in (prefix, suffix) if part).strip()
            if remaining:
                remaining, aspect, resolution, _ = self.expand_cos_user_text_with_preset(remaining)
            else:
                aspect = ""
                resolution = ""
            variants.append(" ".join(part for part in (prefix, selected_prompt, suffix) if part).strip())
            if index == 0:
                first_aspect = aspect
                first_resolution = resolution
        return variants, first_aspect, first_resolution, alias

    def expand_dynamic_preset_variants(
        self,
        raw_text: str,
        count: int,
    ) -> Tuple[List[str], str, str, str]:
        """Resolve one dynamic image preset independently for every shot."""
        text = str(raw_text or "").strip()
        if not text or count <= 0:
            return [], "", "", ""

        from ..studio.studio import dynamic_prompt_preset_groups

        aliases = [
            str(alias or "").strip()
            for alias, _choices in dynamic_prompt_preset_groups()
            if str(alias or "").strip()
        ]
        aliases.sort(key=len, reverse=True)
        alias_boundary = r"[\s·/／、，,：:（）()\[\]【】;；。.!！？?]"
        matched_alias = next(
            (
                alias
                for alias in aliases
                if re.search(
                    rf"(?<!\w){re.escape(alias)}(?={alias_boundary}|$)",
                    text,
                    flags=re.IGNORECASE,
                )
            ),
            "",
        )
        if not matched_alias:
            return [], "", "", ""

        alias_match = re.search(
            rf"(?<!\w){re.escape(matched_alias)}(?={alias_boundary}|$)",
            text,
            flags=re.IGNORECASE,
        )
        if not alias_match:
            return [], "", "", ""

        prefix = text[: alias_match.start()].strip()
        suffix = text[alias_match.end() :].strip()
        variants: List[str] = []
        first_aspect = ""
        first_resolution = ""
        for index in range(max(1, int(count))):
            selected, selected_aspect, selected_resolution, _ = self.expand_user_text_with_preset(
                matched_alias
            )
            if prefix:
                prefix_expanded, prefix_aspect, prefix_resolution, _ = self.expand_user_text_with_preset(prefix)
            else:
                prefix_expanded, prefix_aspect, prefix_resolution = "", "", ""
            if suffix:
                suffix_expanded, suffix_aspect, suffix_resolution, _ = self.expand_user_text_with_preset(suffix)
            else:
                suffix_expanded, suffix_aspect, suffix_resolution = "", "", ""
            variants.append(
                " ".join(
                    part
                    for part in (prefix_expanded, selected, suffix_expanded)
                    if str(part or "").strip()
                ).strip()
            )
            if index == 0:
                first_aspect = selected_aspect or prefix_aspect or suffix_aspect
                first_resolution = selected_resolution or prefix_resolution or suffix_resolution
        return variants, first_aspect, first_resolution, matched_alias

    def normalize_preset_input(self, text: str) -> str:
        return normalize_preset_input(text)

    def split_preset_command(self, text: str) -> Tuple[str, str]:
        return split_preset_command(text)
