"""Reference image collection and generated media delivery helpers."""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import aiohttp

try:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.message_components import Image
    from astrbot.api import logger
except ImportError:  # Compatibility with older AstrBot layouts
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.event.components import Image
    from astrbot.api.utils import logger

try:
    from astrbot.api.message_components import Video  # type: ignore
except Exception:
    try:
        from astrbot.api.event.components import Video  # type: ignore
    except Exception:
        Video = None  # type: ignore

from ..core.providers import ImageReference
from .reference_collector import ReferenceCollector
from ..core.utils import detect_mime_by_bytes, extract_event_text, redact_sensitive_text


class ReferenceMediaMixin:
    def _bot_account_ids(self, event: Optional[AstrMessageEvent] = None) -> List[str]:
        ids = set()
        # A global context user_id can describe the current user, not the bot.
        context_keys = ("bot_id", "self_id", "account_id", "qq", "uin")
        event_keys = ("bot_id", "self_id", "account_id", "qq", "uin")
        robot_keys = ("bot_id", "self_id", "account_id", "qq", "uin", "user_id", "id")

        sources: List[Tuple[Any, Tuple[str, ...]]] = [(self.context, context_keys)]
        if event is not None:
            sources.append((event, event_keys))
            message_obj = getattr(event, "message_obj", None)
            if message_obj is not None:
                sources.append((message_obj, event_keys))
            robot = getattr(event, "robot", None)
            if robot is not None:
                sources.append((robot, robot_keys))

        for source, keys in sources:
            for key in keys:
                value = getattr(source, key, None)
                if value:
                    ids.add(str(value).strip())

        for owner, _ in sources:
            for getter_name in ("get_bot_id", "get_self_id", "get_account_id", "get_uin"):
                getter = getattr(owner, getter_name, None)
                if not callable(getter):
                    continue
                try:
                    value = getter()
                    if asyncio.iscoroutine(value):
                        continue
                    if value:
                        ids.add(str(value).strip())
                except Exception:
                    continue
        return [item for item in ids if item]

    def _reference_source_is_bot_avatar(self, source: str, bot_ids: Iterable[str]) -> bool:
        text = str(source or "").strip()
        ids = {str(bot_id).strip() for bot_id in bot_ids if str(bot_id).strip()}
        if not text or not ids:
            return False
        try:
            parsed = urlparse(text)
        except Exception:
            return False
        if "qlogo.cn" not in parsed.netloc.lower():
            return False
        params = parse_qs(parsed.query)
        for key in ("dst_uin", "uin", "nk", "qq", "user_id"):
            for value in params.get(key, []):
                if str(value).strip() in ids:
                    return True
        target = f"{parsed.path}?{parsed.query}"
        return any(
            re.search(rf"(?<!\d){re.escape(bot_id)}(?!\d)", target)
            for bot_id in ids
        )

    def _filter_reference_images(
        self, event: Optional[AstrMessageEvent], sources: List[str]
    ) -> List[str]:
        if not sources:
            return sources
        bot_ids = set(self._bot_account_ids(event))
        if not bot_ids:
            return sources
        return [
            source
            for source in sources
            if not self._reference_source_is_bot_avatar(source, bot_ids)
        ]

    def _reference_collector(
        self,
        event: AstrMessageEvent,
        *,
        include_at_avatar: bool,
        context_hint: str,
        allow_context_fallback: bool,
        include_persona: bool,
        extra_sources: Optional[List[str]],
        include_image_alternates: bool,
    ) -> ReferenceCollector:
        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        persona_path = ""
        if include_persona:
            if self.persona.has_reference_image():
                persona_path = str(self.persona.get_reference_path() or "")
            elif bool(getattr(self.config, "image_use_logo_when_no_persona", True)):
                logo = str(getattr(self, "_bundled_logo_path", "") or "")
                if logo and os.path.isfile(logo):
                    persona_path = logo

        hint = str(context_hint or extract_event_text(event) or "")
        edit_bot = self._looks_like_edit_bot_result_followup(hint)
        clothes = self._looks_like_clothes_followup(hint)
        user_only = clothes and not edit_bot
        bot_only = edit_bot and not clothes
        return ReferenceCollector(
            max_bytes=max_bytes,
            bot_ids=self._bot_account_ids(event),
            persona_path=persona_path,
            context_sources=self._recent_context_image_sources(
                event,
                prefer_user=not edit_bot,
                user_only=user_only,
                bot_only=bot_only,
            ),
            extra_sources=extra_sources or [],
            include_at_avatar=include_at_avatar,
            include_persona=include_persona,
            allow_context_fallback=allow_context_fallback,
            context_hint=hint,
            looks_like_context_ref=self._looks_like_context_image_reference,
            include_image_alternates=include_image_alternates,
        )

    async def _event_reference_images_with_stats(
        self,
        event: AstrMessageEvent,
        include_at_avatar: bool = False,
        context_hint: str = "",
        allow_context_fallback: bool = False,
        include_persona: bool = False,
        extra_sources: Optional[List[str]] = None,
        include_image_alternates: bool = False,
    ) -> Tuple[List[ImageReference], int, int]:
        """Collect event references and preserve historical result semantics."""
        collector = self._reference_collector(
            event,
            include_at_avatar=include_at_avatar,
            context_hint=context_hint,
            allow_context_fallback=allow_context_fallback,
            include_persona=include_persona,
            extra_sources=extra_sources,
            include_image_alternates=include_image_alternates,
        )
        async with aiohttp.ClientSession(trust_env=False) as session:
            collected = await collector.collect(event, session)
        refs = collected.for_draw(include_persona=include_persona)
        if collected.failed_count and not refs:
            logger.warning(
                f"[SelfieImage] 参考图读取失败或超时: "
                f"{collected.failed_count}/{collected.source_count}"
            )
        return refs, collected.source_count, collected.failed_count

    async def _collect_event_references(
        self,
        event: AstrMessageEvent,
        *,
        include_at_avatar: bool = False,
        context_hint: str = "",
        allow_context_fallback: bool = False,
        include_persona: bool = False,
        extra_sources: Optional[List[str]] = None,
        include_image_alternates: bool = False,
    ):
        collector = self._reference_collector(
            event,
            include_at_avatar=include_at_avatar,
            context_hint=context_hint,
            allow_context_fallback=allow_context_fallback,
            include_persona=include_persona,
            extra_sources=extra_sources,
            include_image_alternates=include_image_alternates,
        )
        async with aiohttp.ClientSession(trust_env=False) as session:
            return await collector.collect(event, session)

    async def _event_reference_images(
        self,
        event: AstrMessageEvent,
        include_at_avatar: bool = False,
        context_hint: str = "",
        allow_context_fallback: bool = False,
        include_persona: bool = False,
        extra_sources: Optional[List[str]] = None,
        include_image_alternates: bool = False,
    ) -> List[ImageReference]:
        refs, _, _ = await self._event_reference_images_with_stats(
            event,
            include_at_avatar=include_at_avatar,
            context_hint=context_hint,
            allow_context_fallback=allow_context_fallback,
            include_persona=include_persona,
            extra_sources=extra_sources,
            include_image_alternates=include_image_alternates,
        )
        return refs

    def _create_image_component(self, file_path: str) -> Any:
        path = os.path.abspath(file_path)
        if hasattr(Image, "fromFileSystem"):
            return Image.fromFileSystem(path)
        if hasattr(Image, "from_file_system"):
            return Image.from_file_system(path)
        return Image(file=path)

    def _create_video_component(self, file_path: str) -> Any:
        path = os.path.abspath(file_path)
        if Video is None:
            return {"type": "video", "file": path}
        if hasattr(Video, "fromFileSystem"):
            return Video.fromFileSystem(path)
        if hasattr(Video, "from_file_system"):
            return Video.from_file_system(path)
        if hasattr(Video, "fromURL") and str(file_path).startswith("http"):
            return Video.fromURL(file_path)
        try:
            return Video(file=path)
        except Exception:
            return Video(path) if callable(Video) else {"type": "video", "file": path}

    async def _send_generated_video(
        self, event: AstrMessageEvent, file_path: str, caption: str = ""
    ) -> None:
        components: List[Any] = []
        if caption:
            try:
                from astrbot.api.message_components import Plain  # type: ignore
            except Exception:
                try:
                    from astrbot.api.event.components import Plain  # type: ignore
                except Exception:
                    Plain = None  # type: ignore
            if Plain is not None:
                components.append(Plain(caption))
        components.append(self._create_video_component(file_path))
        try:
            await event.send(event.chain_result(components))
        except Exception:
            if caption:
                try:
                    await event.send(event.plain_result(caption))
                except Exception:
                    pass
            await event.send(event.chain_result([self._create_video_component(file_path)]))

    def _persona_identity_reference(self) -> Optional[ImageReference]:
        """Prefer the configured persona image, with the bundled logo fallback."""
        persona_ref = self.persona.get_reference_image()
        if persona_ref:
            return ImageReference(
                data=persona_ref["data"], mime_type=persona_ref["mime_type"]
            )
        if not bool(getattr(self.config, "image_use_logo_when_no_persona", True)):
            return None
        logo = str(getattr(self, "_bundled_logo_path", "") or "")
        if not logo or not os.path.isfile(logo):
            return None
        try:
            with open(logo, "rb") as handle:
                data = handle.read()
        except OSError:
            return None
        if not data:
            return None
        return ImageReference(
            data=data, mime_type=detect_mime_by_bytes(data) or "image/png"
        )

    def _persona_auxiliary_references(self, action: str = "") -> List[ImageReference]:
        """Load auxiliary identity images; group photos use the primary image only."""
        if self.persona.analyze_selfie_intent(action).is_group_photo:
            return []
        return [
            ImageReference(data=item["data"], mime_type=item["mime_type"])
            for item in self.persona.get_auxiliary_reference_images()
            if item.get("data")
        ]

    def _video_persona_reference(self) -> Optional[ImageReference]:
        return self._persona_identity_reference()

    async def _send_generated_images(
        self, event: AstrMessageEvent, files: Iterable[str]
    ) -> int:
        sent = 0
        for file_path in files:
            try:
                await event.send(
                    event.chain_result([self._create_image_component(file_path)])
                )
            except Exception as exc:
                logger.warning(
                    "[SelfieImage] image send failed: %s",
                    redact_sensitive_text(str(exc)),
                )
                owner = self._session_key(event)
                with self._send_failures_lock:
                    self._send_failures.setdefault(owner, []).append(
                        self._cache_relative_path(file_path)
                    )
                continue
            self._record_bot_image_context(event, [file_path])
            sent += 1
            await asyncio.sleep(0.4)
        return sent

    async def retry_failed_images(self, event: AstrMessageEvent) -> dict:
        owner = self._session_key(event)
        with self._send_failures_lock:
            paths = list(self._send_failures.get(owner) or [])
        sent = 0
        remaining = []
        for rel_path in paths:
            try:
                abs_path = self._cache_absolute_path(rel_path)
            except Exception:
                continue
            if not os.path.isfile(abs_path):
                continue
            if await self._send_generated_images(event, [abs_path]):
                sent += 1
            else:
                remaining.append(rel_path)
        with self._send_failures_lock:
            if remaining:
                self._send_failures[owner] = remaining
            else:
                self._send_failures.pop(owner, None)
        return {"success": sent > 0, "sent": sent, "remaining": len(remaining)}
