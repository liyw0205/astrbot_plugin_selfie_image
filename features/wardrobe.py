"""User-managed clothing references used by the daily selfie schedule.

The wardrobe is deliberately independent from generated-image records.  Items
are small metadata entries backed by their own image files so cache cleanup or
record pruning cannot remove a user's source clothing image.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..core.providers import ImageReference
from ..core.utils import (
    DEFAULT_MAX_ANIMATION_FRAMES,
    DEFAULT_MAX_IMAGE_DIMENSION,
    DEFAULT_MAX_IMAGE_PIXELS,
    detect_mime_by_bytes,
    ext_from_mime,
    image_geometry_error,
    looks_like_image_bytes,
    normalize_image_mime,
)


WARDROBE_SCHEMA_VERSION = 1
DEFAULT_RECENT_DAYS = 7
MAX_NAME_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 600
MAX_TAGS = 16
MAX_TAG_LENGTH = 80


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _tags(value: Any) -> list[str]:
    if isinstance(value, str):
        values: Iterable[Any] = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []
    result: list[str] = []
    for raw in values:
        tag = _text(raw, MAX_TAG_LENGTH)
        if tag and tag not in result:
            result.append(tag)
        if len(result) >= MAX_TAGS:
            break
    return result


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


@dataclass(slots=True)
class WardrobeItem:
    id: str
    name: str
    path: str
    mime_type: str
    sha256: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    created_at: str = ""
    last_used_date: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> Optional["WardrobeItem"]:
        if not isinstance(value, dict):
            return None
        item_id = _text(value.get("id"), 80)
        name = _text(value.get("name") or value.get("title") or item_id, MAX_NAME_LENGTH)
        path = _text(value.get("path"), 260)
        digest = _text(value.get("sha256") or value.get("digest"), 64).lower()
        if not item_id or not name or not path or len(digest) != 64:
            return None
        return cls(
            id=item_id,
            name=name,
            path=path,
            mime_type=normalize_image_mime(value.get("mime_type") or "image/png"),
            sha256=digest,
            description=_text(value.get("description"), MAX_DESCRIPTION_LENGTH),
            tags=_tags(value.get("tags")),
            enabled=bool(value.get("enabled", True)),
            created_at=_text(value.get("created_at"), 40),
            last_used_date=_text(value.get("last_used_date"), 20),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WardrobeStore:
    """Atomic metadata store and source-image manager for wardrobe items."""

    def __init__(
        self,
        data_dir: str | os.PathLike[str],
        *,
        max_bytes: int = 25 * 1024 * 1024,
        max_dimension: int = DEFAULT_MAX_IMAGE_DIMENSION,
        max_pixels: int = DEFAULT_MAX_IMAGE_PIXELS,
        max_frames: int = DEFAULT_MAX_ANIMATION_FRAMES,
        recent_days: int = DEFAULT_RECENT_DAYS,
    ) -> None:
        self.root = Path(data_dir) / "wardrobe"
        self.metadata_path = self.root / "metadata.json"
        self.max_bytes = max(1, int(max_bytes))
        self.max_dimension = max(1, int(max_dimension))
        self.max_pixels = max(1, int(max_pixels))
        self.max_frames = max(1, int(max_frames))
        self.recent_days = max(0, int(recent_days))
        self.items: dict[str, WardrobeItem] = {}
        self.load()

    def load(self) -> None:
        self.items = {}
        try:
            raw = json.loads(self.metadata_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return
        values = raw.get("items") if isinstance(raw, dict) else raw
        if not isinstance(values, list):
            return
        for value in values:
            item = WardrobeItem.from_dict(value)
            if item and item.id not in self.items:
                self.items[item.id] = item

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": WARDROBE_SCHEMA_VERSION,
            "items": [item.to_dict() for item in self.list_items(include_disabled=True)],
        }
        temp = self.metadata_path.with_name(f"{self.metadata_path.name}.{time.time_ns()}.tmp")
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.metadata_path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def list_items(self, *, include_disabled: bool = True) -> list[WardrobeItem]:
        values = [item for item in self.items.values() if include_disabled or item.enabled]
        return sorted(values, key=lambda item: (item.created_at or "", item.id))

    def get(self, item_id: str) -> Optional[WardrobeItem]:
        return self.items.get(str(item_id or "").strip())

    def resolve_path(self, item: WardrobeItem | str) -> Path:
        value = item if isinstance(item, WardrobeItem) else self.get(str(item))
        if value is None:
            raise ValueError("衣橱服装不存在")
        path = (self.root / value.path).resolve()
        root = self.root.resolve()
        if path != root and root not in path.parents:
            raise ValueError("衣橱图片路径无效")
        return path

    def _validate_data(self, data: bytes, mime_type: str = "") -> str:
        if not data:
            raise ValueError("衣橱图片为空")
        if len(data) > self.max_bytes:
            raise ValueError(f"衣橱图片过大，最大允许 {self.max_bytes} bytes")
        if not looks_like_image_bytes(data):
            raise ValueError("衣橱仅支持图片文件")
        geometry_error = image_geometry_error(
            data,
            max_dimension=self.max_dimension,
            max_pixels=self.max_pixels,
            max_frames=self.max_frames,
        )
        if geometry_error:
            raise ValueError(f"衣橱图片尺寸或动画帧数超限：{geometry_error}")
        detected = detect_mime_by_bytes(data)
        return normalize_image_mime(mime_type or detected, detected or "image/png")

    def add(
        self,
        data: bytes,
        mime_type: str = "",
        *,
        name: str = "",
        description: str = "",
        tags: Any = None,
    ) -> WardrobeItem:
        mime = self._validate_data(data, mime_type)
        digest = hashlib.sha256(data).hexdigest()
        for item in self.items.values():
            if item.sha256 == digest:
                # Updating metadata on a duplicate is explicit and predictable:
                # an omitted name never erases the existing label.
                changed = False
                if name and _text(name, MAX_NAME_LENGTH) != item.name:
                    item.name = _text(name, MAX_NAME_LENGTH)
                    changed = True
                if description is not None and str(description).strip() and _text(description, MAX_DESCRIPTION_LENGTH) != item.description:
                    item.description = _text(description, MAX_DESCRIPTION_LENGTH)
                    changed = True
                parsed_tags = _tags(tags)
                if parsed_tags and parsed_tags != item.tags:
                    item.tags = parsed_tags
                    changed = True
                if changed:
                    self.save()
                return item

        item_id = f"wardrobe_{digest[:16]}"
        ext = ext_from_mime(mime)
        filename = f"{item_id}.{ext}"
        path = self.root / filename
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.root / f".{filename}.{time.time_ns()}.tmp"
        try:
            temp.write_bytes(data)
            temp.replace(path)
            item = WardrobeItem(
                id=item_id,
                name=_text(name or f"衣橱服装 {len(self.items) + 1}", MAX_NAME_LENGTH),
                path=filename,
                mime_type=mime,
                sha256=digest,
                description=_text(description, MAX_DESCRIPTION_LENGTH),
                tags=_tags(tags),
                created_at=_now(),
            )
            self.items[item.id] = item
            try:
                self.save()
            except Exception:
                self.items.pop(item.id, None)
                path.unlink(missing_ok=True)
                raise
            return item
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def update(
        self,
        item_id: str,
        *,
        name: Any = None,
        description: Any = None,
        tags: Any = None,
        enabled: Any = None,
    ) -> WardrobeItem:
        item = self.get(item_id)
        if item is None:
            raise ValueError("衣橱服装不存在")
        if name is not None:
            value = _text(name, MAX_NAME_LENGTH)
            if value:
                item.name = value
        if description is not None:
            item.description = _text(description, MAX_DESCRIPTION_LENGTH)
        if tags is not None:
            item.tags = _tags(tags)
        if enabled is not None:
            item.enabled = bool(enabled)
        self.save()
        return item

    def set_enabled(self, item_id: str, enabled: bool) -> WardrobeItem:
        return self.update(item_id, enabled=bool(enabled))

    def remove(self, item_id: str) -> WardrobeItem:
        item = self.get(item_id)
        if item is None:
            raise ValueError("衣橱服装不存在")
        self.items.pop(item.id, None)
        try:
            path = self.resolve_path(item)
            path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        self.save()
        return item

    def read(self, item: WardrobeItem | str) -> tuple[bytes, str]:
        value = item if isinstance(item, WardrobeItem) else self.get(str(item))
        if value is None:
            raise ValueError("衣橱服装不存在")
        path = self.resolve_path(value)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ValueError("衣橱图片文件不可读") from exc
        if hashlib.sha256(data).hexdigest() != value.sha256:
            raise ValueError("衣橱图片校验失败")
        return data, value.mime_type

    def image_reference(self, item: WardrobeItem | str) -> Optional[ImageReference]:
        value = item if isinstance(item, WardrobeItem) else self.get(str(item))
        if value is None or not value.enabled:
            return None
        try:
            data, mime = self.read(value)
        except ValueError:
            return None
        return ImageReference(data=data, mime_type=mime, source_url=str(self.resolve_path(value)))

    def _recent_ids(self, date: str) -> set[str]:
        if self.recent_days <= 0:
            return set()
        try:
            current = _datetime.date.fromisoformat(str(date))
        except (TypeError, ValueError):
            return set()
        result: set[str] = set()
        for item in self.items.values():
            if not item.last_used_date:
                continue
            try:
                used = _datetime.date.fromisoformat(item.last_used_date)
            except ValueError:
                continue
            age = (current - used).days
            if 0 <= age <= self.recent_days:
                result.add(item.id)
        return result

    def select_for_date(
        self,
        date: str,
        *,
        avoid_id: str = "",
        force_new: bool = False,
    ) -> Optional[WardrobeItem]:
        enabled = [item for item in self.list_items(include_disabled=False) if item.enabled]
        if not enabled:
            return None
        current_id = str(avoid_id or "").strip()
        recent = self._recent_ids(str(date))
        candidates = [item for item in enabled if item.id not in recent]
        if force_new:
            candidates = [item for item in candidates if item.id != current_id]
        if not candidates:
            candidates = [item for item in enabled if not force_new or item.id != current_id]
        if not candidates:
            candidates = enabled
        digest = hashlib.sha256(str(date).encode("utf-8")).digest()
        index = int.from_bytes(digest[:8], "big") % len(candidates)
        return candidates[index]

    def mark_used(self, item_id: str, date: str) -> WardrobeItem:
        item = self.get(item_id)
        if item is None:
            raise ValueError("衣橱服装不存在")
        item.last_used_date = _text(date, 20)
        self.save()
        return item

    def summary(self, *, current_id: str = "") -> dict[str, Any]:
        current = self.get(current_id) if current_id else None
        return {
            "items": [item.to_dict() for item in self.list_items()],
            "enabled_count": sum(1 for item in self.items.values() if item.enabled),
            "current_item_id": current.id if current else "",
            "current_item_name": current.name if current else "",
        }
