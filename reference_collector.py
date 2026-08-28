"""Unified reference image collection for command / LLM / selfie paths.

Sources of design:
- piexian / Railgun image_generation reference collectors
- Selfie existing extract_image_sources_from_event + persona rules
Target 11: one collector for message, quote/reply, forward nodes, @avatar,
persona selfie image, and group-photo object images without mixing roles.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, urlparse

from .providers import ImageReference
from .utils import (
    detect_mime_by_bytes,
    extract_image_sources_from_event,
    extract_image_urls,
    fetch_image_source,
    normalize_image_mime,
    unique,
)


FORWARD_TYPE_NAMES = {
    "Forward",
    "ForwardMessage",
    "Nodes",
    "Node",
    "MergedForward",
    "Reply",
    "Quote",
}


@dataclass
class CollectedReferences:
    """Role-separated reference images after download."""

    message: List[ImageReference] = field(default_factory=list)
    quote: List[ImageReference] = field(default_factory=list)
    forward: List[ImageReference] = field(default_factory=list)
    at_avatar: List[ImageReference] = field(default_factory=list)
    context: List[ImageReference] = field(default_factory=list)
    persona: List[ImageReference] = field(default_factory=list)
    extra: List[ImageReference] = field(default_factory=list)
    source_count: int = 0
    failed_count: int = 0
    roles: Dict[str, int] = field(default_factory=dict)

    def all_object_refs(self) -> List[ImageReference]:
        """Non-persona references suitable as draw / group-photo objects."""
        return dedupe_image_references(
            [*self.message, *self.quote, *self.forward, *self.at_avatar, *self.context, *self.extra]
        )

    def for_draw(self, *, include_persona: bool = False) -> List[ImageReference]:
        refs = self.all_object_refs()
        if include_persona:
            refs = dedupe_image_references([*self.persona, *refs])
        return refs

    def for_group_selfie(self) -> List[ImageReference]:
        """Group selfie objects only — persona stays on selfie builder side."""
        return self.all_object_refs()

    def for_img2img(self) -> List[ImageReference]:
        return self.all_object_refs()


def content_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dedupe_image_references(refs: Sequence[ImageReference]) -> List[ImageReference]:
    result: List[ImageReference] = []
    seen: Set[str] = set()
    for ref in refs:
        if not ref or not getattr(ref, "data", None):
            continue
        digest = content_digest(ref.data)
        if digest in seen:
            continue
        seen.add(digest)
        result.append(ref)
    return result


def normalize_source_items(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        urls = extract_image_urls(text)
        return urls or [text]
    if isinstance(raw, dict):
        for key in ("url", "path", "file", "file_path", "image", "src", "name"):
            items = normalize_source_items(raw.get(key))
            if items:
                return items
        return []
    if isinstance(raw, (bytes, bytearray)):
        return []
    if isinstance(raw, Iterable):
        items: List[str] = []
        for value in raw:
            items.extend(normalize_source_items(value))
        return items
    text = str(raw).strip()
    return [text] if text else []


def _component_type_name(obj: Any) -> str:
    return str(type(obj).__name__ or "")


def _looks_like_forward_or_quote(obj: Any) -> bool:
    name = _component_type_name(obj)
    if name in FORWARD_TYPE_NAMES:
        return True
    lowered = name.lower()
    return any(token in lowered for token in ("forward", "node", "quote", "reply", "merge"))


def extract_structured_image_sources(
    event: Any,
    *,
    include_at_avatar: bool = False,
    include_image_alternates: bool = False,
) -> Dict[str, List[str]]:
    """Walk event tree and bucket image sources by role.

    Most callers only need one source per image.  Prompt lookup can opt into
    both ``path`` and ``url`` because adapters sometimes expose a transcoded
    local copy alongside the original remote image.
    """

    buckets: Dict[str, List[str]] = {
        "message": [],
        "quote": [],
        "forward": [],
        "at_avatar": [],
    }
    visited: Set[int] = set()

    def add(role: str, value: Any) -> None:
        text = str(value or "").strip()
        if text:
            buckets.setdefault(role, []).append(text)

    def search(obj: Any, role: str, depth: int = 0) -> None:
        if obj is None or depth > 12 or id(obj) in visited:
            return
        visited.add(id(obj))
        obj_type = _component_type_name(obj)

        if obj_type == "Image":
            # AstrBot's Image model declares ``path`` with an empty default,
            # while QQ adapters often put the usable local file in ``file``.
            # Gather every representation and prefer local/inline data over a
            # remote URL, otherwise prompt lookup can hash a transcoded URL.
            raw_candidates: List[Any] = []
            for attr in ("path", "file", "file_path", "url"):
                try:
                    value = getattr(obj, attr, None)
                except Exception:
                    value = None
                if value:
                    raw_candidates.append(value)
            candidates: List[Any] = []
            for value in raw_candidates:
                text = str(value).strip()
                if text and text not in candidates:
                    candidates.append(text)
            local = [
                value
                for value in candidates
                if not value.lower().startswith(("http://", "https://"))
            ]
            remote = [
                value
                for value in candidates
                if value.lower().startswith(("http://", "https://"))
            ]
            candidates = [*local, *remote]
            for value in (candidates if include_image_alternates else candidates[:1]):
                add(role, value)
            return

        if obj_type == "Plain":
            text = str(getattr(obj, "text", "") or "")
            for url in extract_image_urls(text):
                add(role, url)
            return

        if include_at_avatar and obj_type in {"At", "AtSomeone"}:
            qq = str(getattr(obj, "qq", getattr(obj, "id", "")) or "").strip()
            if qq and qq != "all":
                add("at_avatar", f"https://q4.qlogo.cn/headimg_dl?dst_uin={qq}&spec=640")
            return

        if isinstance(obj, str):
            for url in extract_image_urls(obj):
                add(role, url)
            return

        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                next_role = "forward" if role == "forward" or _looks_like_forward_or_quote(item) else role
                search(item, next_role, depth + 1)
            return

        next_role = "forward" if _looks_like_forward_or_quote(obj) else role
        if obj_type in {"Reply", "Quote"} or "quote" in obj_type.lower() or "reply" in obj_type.lower():
            next_role = "quote" if role == "message" else role

        attrs: List[str] = []
        if hasattr(obj, "__dict__"):
            attrs.extend(vars(obj).keys())
        if hasattr(obj, "__slots__"):
            attrs.extend(list(getattr(obj, "__slots__", []) or []))
        blocked = {"context", "star", "bot", "provider", "session", "config", "plugin_config", "logger"}
        for key in set(attrs) - blocked:
            try:
                child = getattr(obj, key)
            except Exception:
                continue
            child_role = next_role
            key_l = str(key).lower()
            if key_l in {"quote", "reply", "source"}:
                child_role = "quote"
            elif key_l in {"nodes", "forward", "forward_nodes", "content"} and _looks_like_forward_or_quote(obj):
                child_role = "forward"
            search(child, child_role, depth + 1)

    message_obj = getattr(event, "message_obj", None)
    search(message_obj, "message")
    quote_obj = getattr(message_obj, "quote", None) if message_obj is not None else None
    if quote_obj:
        search(quote_obj, "quote")
    search(getattr(event, "message", None), "message")
    search(getattr(event, "raw_message", None), "message")

    # Also merge legacy flat extractor so we do not regress older event shapes.
    legacy = extract_image_sources_from_event(event, include_at_avatar=include_at_avatar)
    known = {item for values in buckets.values() for item in values}
    for item in legacy:
        if item not in known:
            buckets["message"].append(item)

    return {key: unique(values) for key, values in buckets.items()}


def filter_bot_avatar_sources(sources: Sequence[str], bot_ids: Sequence[str]) -> List[str]:
    ids = {str(item).strip() for item in bot_ids if str(item).strip()}
    if not ids:
        return list(sources)
    filtered: List[str] = []
    for source in sources:
        text = str(source or "").strip()
        if not text:
            continue
        try:
            parsed = urlparse(text)
        except Exception:
            filtered.append(text)
            continue
        if "qlogo.cn" not in parsed.netloc.lower():
            filtered.append(text)
            continue
        params = parse_qs(parsed.query)
        hit = False
        for key in ("dst_uin", "uin", "nk", "qq", "user_id"):
            for value in params.get(key, []):
                if str(value).strip() in ids:
                    hit = True
                    break
            if hit:
                break
        if hit:
            continue
        target = f"{parsed.path}?{parsed.query}"
        if any(re.search(rf"(?<!\d){re.escape(bot_id)}(?!\d)", target) for bot_id in ids):
            continue
        filtered.append(text)
    return filtered


async def download_sources_as_references(
    sources: Sequence[str],
    session: Any,
    *,
    max_bytes: int,
) -> Tuple[List[ImageReference], int]:
    refs: List[ImageReference] = []
    failed = 0
    seen: Set[str] = set()
    for source in sources:
        fetched = await fetch_image_source(source, session, max_bytes=max_bytes)
        if not fetched:
            failed += 1
            continue
        data, mime = fetched
        if not data:
            failed += 1
            continue
        digest = content_digest(data)
        if digest in seen:
            continue
        seen.add(digest)
        source_url = str(source or "").strip() if str(source or "").strip().lower().startswith(("http://", "https://")) else ""
        refs.append(
            ImageReference(
                data=data,
                mime_type=normalize_image_mime(mime or detect_mime_by_bytes(data)),
                source_url=source_url,
            )
        )
    return refs, failed


class ReferenceCollector:
    """Collect and role-tag reference images for Selfie Image commands/tools."""

    def __init__(
        self,
        *,
        max_bytes: int,
        bot_ids: Optional[Sequence[str]] = None,
        persona_path: str = "",
        context_sources: Optional[Sequence[str]] = None,
        extra_sources: Optional[Sequence[str]] = None,
        include_at_avatar: bool = False,
        include_persona: bool = False,
        allow_context_fallback: bool = False,
        context_hint: str = "",
        looks_like_context_ref=None,
        include_image_alternates: bool = False,
    ) -> None:
        self.max_bytes = max(1024, int(max_bytes or 10 * 1024 * 1024))
        self.bot_ids = list(bot_ids or [])
        self.persona_path = str(persona_path or "").strip()
        self.context_sources = list(context_sources or [])
        self.extra_sources = list(extra_sources or [])
        self.include_at_avatar = bool(include_at_avatar)
        self.include_persona = bool(include_persona)
        self.allow_context_fallback = bool(allow_context_fallback)
        self.context_hint = str(context_hint or "")
        self.looks_like_context_ref = looks_like_context_ref
        self.include_image_alternates = bool(include_image_alternates)

    def collect_source_buckets(self, event: Any) -> Dict[str, List[str]]:
        buckets = extract_structured_image_sources(
            event,
            include_at_avatar=self.include_at_avatar,
            include_image_alternates=self.include_image_alternates,
        )
        for key, values in list(buckets.items()):
            buckets[key] = filter_bot_avatar_sources(values, self.bot_ids)

        total = sum(len(values) for values in buckets.values())
        if (
            total == 0
            and self.allow_context_fallback
            and callable(self.looks_like_context_ref)
            and self.looks_like_context_ref(self.context_hint)
        ):
            buckets["context"] = filter_bot_avatar_sources(self.context_sources, self.bot_ids)
        else:
            buckets.setdefault("context", [])

        buckets["extra"] = filter_bot_avatar_sources(normalize_source_items(self.extra_sources), self.bot_ids)
        if self.include_persona and self.persona_path:
            buckets["persona"] = [self.persona_path]
        else:
            buckets["persona"] = []
        return buckets

    async def collect(self, event: Any, session: Any) -> CollectedReferences:
        buckets = self.collect_source_buckets(event)
        collected = CollectedReferences()
        role_order = ("message", "quote", "forward", "at_avatar", "context", "extra", "persona")
        for role in role_order:
            sources = buckets.get(role) or []
            collected.source_count += len(sources)
            refs, failed = await download_sources_as_references(sources, session, max_bytes=self.max_bytes)
            collected.failed_count += failed
            setattr(collected, role if hasattr(collected, role) else "extra", refs)
            collected.roles[role] = len(refs)
        # ensure attributes always lists
        collected.message = list(collected.message)
        collected.quote = list(collected.quote)
        collected.forward = list(collected.forward)
        collected.at_avatar = list(collected.at_avatar)
        collected.context = list(collected.context)
        collected.extra = list(collected.extra)
        collected.persona = list(collected.persona)
        return collected
