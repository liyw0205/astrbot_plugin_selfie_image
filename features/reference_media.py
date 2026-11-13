"""Reference image collection and generated media delivery helpers."""

from __future__ import annotations

import asyncio
import os
import re
import threading
from contextlib import asynccontextmanager
from typing import Any, Callable, Iterable, List, Optional, Tuple
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

from ..core.providers import ImageReference
from .reference_collector import ReferenceCollector
from ..core.utils import detect_mime_by_bytes, extract_event_text, redact_sensitive_text


IMAGE_DELIVERY_PERSIST_BATCH_SIZE = 4


class ImageDeliveryOutcome(int):
    """Count successful sends while retaining ambiguous transport outcomes."""

    def __new__(
        cls,
        sent: int = 0,
        *,
        failed: int = 0,
        unknown: int = 0,
    ):
        value = int.__new__(cls, max(0, int(sent or 0)))
        value.failed_count = max(0, int(failed or 0))
        value.unknown_count = max(0, int(unknown or 0))
        return value


def _image_delivery_is_ambiguous(error: BaseException) -> bool:
    """A send timeout can happen after the adapter accepted the message."""
    if isinstance(error, TimeoutError):
        return True
    text = str(error or "").lower()
    if re.search(r"retcode\s*[:=]\s*1200", text):
        return True
    return any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "retcode=1200",
            "sendmsg",
            "onmsginfolistupdate",
        )
    )


class ReferenceMediaMixin:
    @asynccontextmanager
    async def _reference_client_session(self):
        """Reuse reference connections on the plugin loop, never user cookies."""
        if getattr(self, "_terminating", False):
            raise asyncio.CancelledError()
        loop = asyncio.get_running_loop()
        if getattr(self, "loop", None) is not loop:
            async with aiohttp.ClientSession(trust_env=False) as session:
                yield session
            return
        session = getattr(self, "_reference_http_session", None)
        if session is None or session.closed:
            session = aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar())
            self._reference_http_session = session
        yield session

    async def _close_reference_client_session(self) -> None:
        session = getattr(self, "_reference_http_session", None)
        if session is not None:
            await session.close()
            self._reference_http_session = None

    def _remember_reference_failure_reason(self, event: Any, reasons: Iterable[str]) -> None:
        key = str(self._context_session_key(event) if callable(getattr(self, "_context_session_key", None)) else id(event))
        allowed = {"too_large", "invalid_or_unreadable", "unavailable"}
        values = [str(reason) for reason in reasons if str(reason) in allowed]
        lock = getattr(self, "_reference_selection_lock", threading.RLock())
        with lock:
            queue = getattr(self, "_reference_failure_reasons", None)
            if not isinstance(queue, dict):
                queue = {}
                self._reference_failure_reasons = queue
            queue[key] = values

    def _reference_failure_reason(self, event: Any) -> str:
        key = str(self._context_session_key(event) if callable(getattr(self, "_context_session_key", None)) else id(event))
        lock = getattr(self, "_reference_selection_lock", threading.RLock())
        with lock:
            queue = getattr(self, "_reference_failure_reasons", {})
            values = queue.pop(key, []) if isinstance(queue, dict) else []
        if "too_large" in values:
            return "too_large"
        if "invalid_or_unreadable" in values:
            return "invalid_or_unreadable"
        if "unavailable" in values:
            return "unavailable"
        return ""

    def _reference_selection_queue(self) -> dict[str, list[dict[str, Any]]]:
        queue = getattr(self, "_reference_selection_cache", None)
        if not isinstance(queue, dict):
            queue = {}
            self._reference_selection_cache = queue
            self._reference_selection_lock = threading.RLock()
        return queue

    def _remember_reference_selection(self, event: Any, summary: dict[str, Any]) -> None:
        key = str(self._context_session_key(event) if callable(getattr(self, "_context_session_key", None)) else id(event))
        lock = getattr(self, "_reference_selection_lock", threading.RLock())
        with lock:
            queue = self._reference_selection_queue()
            queue.setdefault(key, []).append(dict(summary))
            queue[key] = queue[key][-8:]

    def _consume_reference_selection(self, event: Any) -> dict[str, Any]:
        key = str(self._context_session_key(event) if callable(getattr(self, "_context_session_key", None)) else id(event))
        lock = getattr(self, "_reference_selection_lock", threading.RLock())
        with lock:
            queue = self._reference_selection_queue()
            rows = queue.get(key) or []
            if not rows:
                return {}
            value = rows.pop(0)
            if not rows:
                queue.pop(key, None)
            return dict(value)

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
        async with self._reference_client_session() as session:
            collected = await collector.collect(event, session)
        refs = collected.for_draw(include_persona=include_persona)
        self._remember_reference_failure_reason(event, getattr(collected, "failure_reasons", []))
        if collected.failed_count and not refs:
            logger.warning(
                f"[SelfieImage] 参考图读取失败或超时: "
                f"{collected.failed_count}/{collected.source_count}"
            )
        self._remember_reference_selection(event, collected.selection_summary(include_persona=include_persona))
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
        async with self._reference_client_session() as session:
            collected = await collector.collect(event, session)
        self._remember_reference_selection(event, collected.selection_summary(include_persona=include_persona))
        return collected

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


    def _persona_identity_reference(self, *, allow_logo: bool = True) -> Optional[ImageReference]:
        """Return the configured persona image, optionally with logo fallback."""
        persona_ref = self.persona.get_reference_image()
        if persona_ref:
            source_path = str(self.persona.get_reference_path() or "")
            return ImageReference(
                data=persona_ref["data"],
                mime_type=persona_ref["mime_type"],
                source_url=source_path,
            )
        if not allow_logo or not bool(getattr(self.config, "image_use_logo_when_no_persona", True)):
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
            data=data,
            mime_type=detect_mime_by_bytes(data) or "image/png",
            source_url=logo,
        )

    def _persona_auxiliary_references(self, action: str = "") -> List[ImageReference]:
        """Load auxiliary identity images; group photos use the primary image only."""
        if self.persona.analyze_selfie_intent(action).is_group_photo:
            return []
        return [
            ImageReference(
                data=item["data"],
                mime_type=item["mime_type"],
                source_url=str(item.get("path") or ""),
            )
            for item in self.persona.get_auxiliary_reference_images()
            if item.get("data")
        ]

    async def _send_generated_images(
        self, event: AstrMessageEvent, files: Iterable[str], *,
        remember_failures: bool = True,
        on_outcome: Optional[Callable[[str, str], None]] = None,
    ) -> ImageDeliveryOutcome:
        sent = 0
        failed = 0
        unknown = 0
        pending: List[Tuple[dict, str, Any, Optional[bool], str]] = []

        async def flush_pending() -> None:
            if not pending:
                return
            batch = list(pending)
            pending.clear()
            await self._persist_image_deliveries(
                event, batch, reconcile_task=not remember_failures,
            )

        try:
            for index, file_path in enumerate(files):
                if index:
                    await asyncio.sleep(0.4)
                record = None
                try:
                    wait_record = getattr(self, "_wait_for_image_record", None)
                    if callable(wait_record):
                        await wait_record(file_path)
                        record = (
                            self._prompt_record_for_file(file_path)
                            if remember_failures
                            else self._redelivery_record_for_file(event, file_path)
                        )
                except Exception as exc:
                    logger.warning("[SelfieImage] image code lookup failed: %s", redact_sensitive_text(str(exc)))
                try:
                    result = event.chain_result([self._create_image_component(file_path)])
                    if (
                        record
                        and record.get("image_code")
                        and getattr(getattr(self, "config", None), "image_show_generation_info", False)
                    ):
                        result.message("\n生图编号：" + record["image_code"])
                    receipt = await event.send(result)
                except Exception as exc:
                    logger.warning(
                        "[SelfieImage] image send failed: %s",
                        redact_sensitive_text(str(exc)),
                    )
                    ambiguous = _image_delivery_is_ambiguous(exc)
                    if ambiguous:
                        unknown += 1
                    else:
                        failed += 1
                    if on_outcome is not None:
                        on_outcome(file_path, "unknown" if ambiguous else "failed")
                    if not ambiguous and remember_failures:
                        owner = self._session_key(event)
                        with self._send_failures_lock:
                            self._send_failures.setdefault(owner, []).append(
                                self._cache_relative_path(file_path)
                            )
                    if record:
                        pending.append((record, file_path, None, None if ambiguous else False, str(exc)))
                        if len(pending) >= IMAGE_DELIVERY_PERSIST_BATCH_SIZE:
                            await flush_pending()
                    continue
                sent += 1
                if on_outcome is not None:
                    on_outcome(file_path, "sent")
                if record:
                    pending.append((record, file_path, receipt, True, ""))
                    if len(pending) >= IMAGE_DELIVERY_PERSIST_BATCH_SIZE:
                        await flush_pending()
                self._record_bot_image_context(event, [file_path])
        finally:
            await flush_pending()
        return ImageDeliveryOutcome(sent, failed=failed, unknown=unknown)

    async def _persist_image_deliveries(
        self,
        event: Any,
        deliveries: List[Tuple[dict, str, Any, Optional[bool], str]],
        *,
        reconcile_task: bool = False,
    ) -> None:
        rows = [row for row in deliveries if row and row[0]]
        if not rows:
            return

        def persist() -> None:
            delivered = [(record, receipt) for record, _path, receipt, state, _error in rows if state is True]
            remember_many = getattr(self, "_remember_prompt_deliveries", None)
            if delivered and callable(remember_many):
                try:
                    remember_many(event, delivered)
                except Exception as exc:
                    logger.warning("[SelfieImage] image message link failed: %s", redact_sensitive_text(str(exc)))
            elif delivered:
                remember_one = getattr(self, "_remember_prompt_delivery", None)
                if callable(remember_one):
                    for record, receipt in delivered:
                        try:
                            remember_one(event, record, receipt)
                        except Exception as exc:
                            logger.warning("[SelfieImage] image message link failed: %s", redact_sensitive_text(str(exc)))

            outcomes_by_record: Dict[Tuple[str, str], Dict[str, Tuple[Optional[bool], str]]] = {}
            task_ids = set()
            for record, path, _receipt, state, error in rows:
                task_id = str(record.get("task_id") or "").strip()
                record_id = str(record.get("id") or "").strip()
                key = (task_id, record_id)
                outcomes_by_record.setdefault(key, {})[path] = (state, error)
                if task_id:
                    task_ids.add(task_id)
            update = getattr(self, "_update_generation_records_delivery", None)
            if callable(update):
                for (task_id, record_id), image_outcomes in outcomes_by_record.items():
                    update(
                        task_id,
                        record_id=record_id,
                        delivered=None,
                        paths=image_outcomes,
                        image_outcomes=image_outcomes,
                    )
            if reconcile_task:
                reconcile = getattr(self, "_reconcile_task_image_delivery", None)
                if callable(reconcile):
                    for task_id in task_ids:
                        reconcile(task_id)

        write = asyncio.create_task(asyncio.to_thread(persist))
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            # Confirmed receipts must reach durable storage before cancellation exits.
            await asyncio.gather(write, return_exceptions=True)
            raise
        except Exception as exc:
            logger.warning("[SelfieImage] image delivery persistence failed: %s", redact_sensitive_text(str(exc)))

    async def _persist_image_delivery(
        self, event: Any, record: dict, file_path: str, receipt: Any, *,
        delivered: Optional[bool], error: str = "", reconcile_task: bool = False,
    ) -> None:
        await self._persist_image_deliveries(
            event,
            [(record, file_path, receipt, delivered, error)],
            reconcile_task=reconcile_task,
        )

    async def retry_failed_images(self, event: AstrMessageEvent) -> dict:
        owner = self._session_key(event)
        with self._send_failures_lock:
            active = getattr(self, "_active_image_retries", None)
            if active is None:
                active = {}
                self._active_image_retries = active
            if owner in active:
                return {"success": False, "sent": 0, "remaining": len(active[owner]), "busy": True}
            pending = list(dict.fromkeys(self._send_failures.pop(owner, []) or []))
            active[owner] = pending
        sent = 0
        unknown = 0
        missing = 0
        try:
            for rel_path in list(pending):
                try:
                    abs_path = self._cache_absolute_path(rel_path)
                except (TypeError, ValueError, OSError):
                    pending.remove(rel_path)
                    missing += 1
                    continue
                if not os.path.isfile(abs_path):
                    pending.remove(rel_path)
                    missing += 1
                    continue

                def accepted(_path: str, state: str) -> None:
                    nonlocal sent, unknown
                    if state != "failed" and rel_path in pending:
                        pending.remove(rel_path)
                        sent += state == "sent"
                        unknown += state == "unknown"

                outcome = await self._send_generated_images(
                    event, [abs_path], remember_failures=False, on_outcome=accepted,
                )
                if int(outcome) > 0:
                    accepted(abs_path, "sent")
                elif getattr(outcome, "unknown_count", 0):
                    accepted(abs_path, "unknown")
        finally:
            with self._send_failures_lock:
                remaining = list(dict.fromkeys([*pending, *self._send_failures.get(owner, [])]))
                if remaining:
                    self._send_failures[owner] = remaining
                else:
                    self._send_failures.pop(owner, None)
                active.pop(owner, None)
        return {"success": sent > 0, "sent": sent, "remaining": len(remaining), "unknown": unknown, "missing": missing}
