"""Web profile and wardrobe operations over the plugin's current persona store."""

from __future__ import annotations

import copy
from typing import Any

from ..core.utils import (
    bytes_to_data_url,
    data_url_to_bytes,
    detect_mime_by_bytes,
    image_validation_limits,
    normalize_image_mime,
)
from .persona import daily_schedule_date_key


class SelfieProfileWebService:
    """Keep profile uploads and response projections out of the entrypoint."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def reference(self) -> dict[str, Any]:
        plugin = self.plugin
        data = plugin.persona.get()
        ref = plugin.persona.get_reference_image()
        appearance_type = plugin.persona.get_appearance_type()
        wardrobe = plugin.get_wardrobe_payload()
        base = {
            "appearance_type": appearance_type,
            "appearance_type_label": plugin.persona.appearance_type_label(),
            "ref_mime_type": data.get("ref_mime_type") or "image/png",
            "updated_at": data.get("updated_at") or "",
            "status": plugin.persona.status_text(),
            "daily_schedule": plugin.persona.daily_schedule_payload(),
            "wardrobe": wardrobe,
        }
        if not ref:
            return {**base, "has_image": False}
        return {
            **base,
            "has_image": True,
            "ref_mime_type": ref["mime_type"],
            "image": bytes_to_data_url(ref["data"], ref["mime_type"]),
        }

    async def ensure_daily_schedule(self) -> dict[str, Any]:
        """Create today's schedule only when it is missing or stale."""
        plugin = self.plugin
        profile = await plugin.persona.ensure_daily_selfie_profile("查看每日安排")
        return {
            "daily_schedule": plugin.persona.daily_schedule_payload(),
            "wardrobe": plugin.get_wardrobe_payload(),
            "updated_at": profile.updated_at,
        }

    def wardrobe(self) -> dict[str, Any]:
        persona = self.plugin.persona
        current = persona.current_wardrobe_item()
        return persona.wardrobe.summary(
            current_id=current.id if current else "",
            date=daily_schedule_date_key(persona.business_day_start),
        )

    def wardrobe_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        mode = payload.get("selection_mode")
        if not isinstance(mode, str) or mode not in {"random", "sequential"}:
            raise ValueError("请选择随机或按顺序轮换")
        self.plugin.persona.set_wardrobe_selection_mode(mode)
        return self.plugin.get_wardrobe_payload()

    async def select_wardrobe_today(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = payload.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("缺少衣橱服装 id")
        plugin = self.plugin
        await plugin.persona.select_wardrobe_today(item_id.strip())
        return {"wardrobe": plugin.get_wardrobe_payload(), "daily_schedule": plugin.persona.daily_schedule_payload()}

    def _decode_image(self, payload: dict[str, Any]) -> tuple[bytes, str]:
        raw_image = str(payload.get("image") or payload.get("data") or "").strip()
        if not raw_image:
            raise ValueError("缺少 image 字段，支持 data:image/...;base64,... 或纯 base64")
        config = self.plugin.config
        max_dimension, max_pixels, max_frames = image_validation_limits(config)
        data, mime = data_url_to_bytes(
            raw_image,
            max_dimension=max_dimension,
            max_pixels=max_pixels,
            max_frames=max_frames,
        )
        if not data:
            raise ValueError("上传图片为空或尺寸/动画帧数超限")
        if len(data) > config.image_max_image_size_mb * 1024 * 1024:
            raise ValueError(f"图片过大，最大允许 {config.image_max_image_size_mb}MB")
        return data, normalize_image_mime(mime or str(payload.get("mime_type") or "") or detect_mime_by_bytes(data))

    async def save_wardrobe_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        data, mime = self._decode_image(payload)
        plugin = self.plugin
        plugin.persona.add_wardrobe_item(
            data,
            mime,
            name=str(payload.get("name") or payload.get("title") or "").strip(),
            description=str(payload.get("description") or "").strip(),
            tags=payload.get("tags"),
        )
        await plugin.persona.ensure_daily_selfie_profile("衣橱添加")
        return plugin.get_wardrobe_payload()

    def update_wardrobe_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = str(payload.get("id") or payload.get("item_id") or "").strip()
        if not item_id:
            raise ValueError("缺少衣橱服装 id")
        self.plugin.persona.update_wardrobe_item(
            item_id,
            name=payload.get("name") if "name" in payload else None,
            description=payload.get("description") if "description" in payload else None,
            tags=payload.get("tags") if "tags" in payload else None,
            enabled=payload.get("enabled") if "enabled" in payload else None,
        )
        return self.plugin.get_wardrobe_payload()

    async def delete_wardrobe_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = str(payload.get("id") or payload.get("item_id") or "").strip()
        if not item_id:
            raise ValueError("缺少衣橱服装 id")
        plugin = self.plugin
        plugin.persona.remove_wardrobe_item(item_id)
        await plugin.persona.ensure_daily_selfie_profile("衣橱删除")
        return plugin.get_wardrobe_payload()

    async def toggle_wardrobe_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = str(payload.get("id") or payload.get("item_id") or "").strip()
        if not item_id:
            raise ValueError("缺少衣橱服装 id")
        enabled = payload.get("enabled")
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in {"1", "true", "yes", "on", "启用"}
        plugin = self.plugin
        plugin.persona.set_wardrobe_enabled(item_id, bool(enabled))
        await plugin.persona.ensure_daily_selfie_profile("衣橱启停")
        return plugin.get_wardrobe_payload()

    def set_appearance_type(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.plugin.persona.set_appearance_type(payload.get("appearance_type", payload.get("type", "auto")))
        return self.plugin.get_selfie_reference_payload()

    def save_reference(self, payload: dict[str, Any]) -> dict[str, Any]:
        data, mime = self._decode_image(payload)
        self.plugin.persona.save_reference_image(data, mime)
        if "appearance_type" in payload or "type" in payload:
            self.plugin.persona.set_appearance_type(payload.get("appearance_type", payload.get("type")))
        return self.plugin.get_selfie_reference_payload()

    def clear_reference(self) -> dict[str, Any]:
        self.plugin.persona.clear_reference_image()
        return self.plugin.get_selfie_reference_payload()

    async def refresh_local(self) -> dict[str, Any]:
        plugin = self.plugin
        profile = await plugin.persona.refresh_daily_selfie_profile_local()
        return {
            "status": plugin.persona.status_text(),
            "updated_at": profile.updated_at,
            "source": profile.source,
            "outfit": profile.outfit,
            "outfit_by_period": dict(profile.outfit_by_period),
            "outfit_pool": list(profile.outfit_pool),
            "scene_by_period": dict(profile.scene_by_period),
            "action_by_period": dict(profile.action_by_period),
            "schedule_by_period": copy.deepcopy(profile.schedule_by_period),
            "daily_schedule": plugin.persona.daily_schedule_payload(),
            "day_theme": profile.day_theme,
            "wardrobe": plugin.get_wardrobe_payload(),
            "occasion": profile.occasion,
            "mood": profile.mood,
        }
