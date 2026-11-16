"""AstrBot framework conversation adapter and transient generation helpers.

The host owns conversation history. This module deliberately does not keep a
second message store, register context hooks, or persist conversation data.
"""

from __future__ import annotations

import copy
import inspect
import json
import re
import time
from contextlib import nullcontext
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional

try:
    from astrbot.api.event import AstrMessageEvent
except ImportError:
    from astrbot.api.event import AstrMessageEvent

from .context_routing import (
    compact_followup_text,
    format_context_for_llm,
    looks_like_clothes_followup,
    looks_like_context_image_reference,
    looks_like_edit_bot_result_followup,
    recent_context_image_sources,
)
from ..core.utils import event_group_id, event_user_id


class ConversationContextMixin:
    """Read the current AstrBot conversation without duplicating its storage."""

    def _session_key(self, event: Optional[AstrMessageEvent] = None) -> str:
        """Return the plugin's transient scope key for non-conversation state."""
        if event is None:
            return "web"
        group_id = event_group_id(event)
        user_id = event_user_id(event)
        if group_id:
            return f"group:{group_id}"
        if user_id:
            return f"private:{user_id}"
        origin = getattr(event, "unified_msg_origin", None)
        if origin:
            return f"origin:{origin}"
        return f"event:{id(event)}"

    def _context_session_key(self, event: Optional[AstrMessageEvent] = None) -> str:
        if event is None:
            return "web"
        origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        return origin or self._session_key(event)

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    async def _call_context_method(
        self,
        method: Any,
        attempts: Iterable[tuple[tuple[Any, ...], dict[str, Any]]],
    ) -> Any:
        if not callable(method):
            return None
        for args, kwargs in attempts:
            try:
                return await self._maybe_await(method(*args, **kwargs))
            except (TypeError, AttributeError):
                continue
            except Exception:
                return None
        return None

    def _framework_conversation_manager(self) -> Any:
        context = getattr(self, "context", None)
        return getattr(context, "conversations", None) or getattr(context, "conversation_manager", None)

    @staticmethod
    def _history_payload(history: Any) -> list[Any]:
        if isinstance(history, str):
            try:
                history = json.loads(history)
            except (TypeError, ValueError, json.JSONDecodeError):
                return []
        if isinstance(history, Mapping):
            for key in ("messages", "history", "contexts", "items"):
                value = history.get(key)
                if isinstance(value, list):
                    return value
            return []
        return list(history) if isinstance(history, (list, tuple)) else []

    @staticmethod
    def _media_source(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, Mapping):
            for key in ("url", "path", "file", "source"):
                candidate = value.get(key)
                if candidate:
                    return str(candidate).strip()
            return ConversationContextMixin._media_source(value.get("image_url"))
        for key in ("url", "path", "file", "source"):
            candidate = getattr(value, key, None)
            if candidate:
                return str(candidate).strip()
        return ConversationContextMixin._media_source(getattr(value, "image_url", None))

    @classmethod
    def _message_parts(cls, content: Any) -> tuple[str, int, list[str]]:
        if isinstance(content, str):
            return re.sub(r"\s+", " ", content).strip(), 0, []
        if isinstance(content, Mapping):
            part_type = str(content.get("type") or "").strip().lower()
            if part_type in {"image_url", "image", "input_image"}:
                source = cls._media_source(content)
                return "", 1, [source] if source else []
            value = content.get("text") or content.get("content") or ""
            return re.sub(r"\s+", " ", str(value)).strip(), 0, []
        if not isinstance(content, (list, tuple)):
            text = getattr(content, "text", "") or getattr(content, "content", "") or ""
            return re.sub(r"\s+", " ", str(text)).strip(), 0, []

        texts: list[str] = []
        image_count = 0
        sources: list[str] = []
        for part in content:
            part_type = str(
                part.get("type") if isinstance(part, Mapping) else getattr(part, "type", "")
            ).strip().lower()
            if part_type in {"image_url", "image", "input_image"}:
                image_count += 1
                source = cls._media_source(part)
                if source:
                    sources.append(source)
                continue
            value = (
                (part.get("text") or part.get("content") or "")
                if isinstance(part, Mapping)
                else (getattr(part, "text", "") or getattr(part, "content", ""))
            )
            if value:
                texts.append(str(value))
        return re.sub(r"\s+", " ", " ".join(texts)).strip(), image_count, sources[:8]

    async def _framework_context_records(
        self, event: Optional[AstrMessageEvent], count: int = 24
    ) -> list[dict[str, Any]]:
        """Load recent messages from AstrBot's current conversation."""
        if event is None:
            return []
        origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        manager = self._framework_conversation_manager()
        if not origin or manager is None:
            return []
        conversation_id = await self._call_context_method(
            getattr(manager, "get_curr_conversation_id", None),
            [((origin,), {}), ((), {"umo": origin}), ((), {"unified_msg_origin": origin})],
        )
        if conversation_id is None:
            return []
        conversation = await self._call_context_method(
            getattr(manager, "get_conversation", None),
            [
                ((origin, conversation_id), {}),
                ((), {"umo": origin, "conversation_id": conversation_id}),
                ((), {"umo": origin, "cid": conversation_id}),
                ((conversation_id,), {}),
            ],
        )
        if conversation is None:
            return []
        history = (
            conversation.get("history")
            if isinstance(conversation, Mapping)
            else getattr(conversation, "history", None)
        )
        rows = self._history_payload(history)
        output: list[dict[str, Any]] = []
        for item in rows[-max(1, min(80, int(count or 24))) :]:
            if not isinstance(item, Mapping):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            text, image_count, sources = self._message_parts(item.get("content"))
            if not text and not image_count:
                continue
            try:
                timestamp = float(item.get("timestamp") or item.get("created_at") or 0)
            except (TypeError, ValueError):
                timestamp = 0.0
            bot_name_getter = getattr(self, "_bot_display_name", None)
            bot_name = str(bot_name_getter() if callable(bot_name_getter) else "Bot")
            output.append(
                {
                    "msg_id": str(item.get("id") or item.get("message_id") or ""),
                    "sender_id": "astrbot",
                    "sender_name": "用户" if role == "user" else bot_name,
                    "content": text[:500] or "[图片]",
                    "is_bot": role == "assistant",
                    "source": "astrbot",
                    "has_image": bool(image_count),
                    "image_count": max(0, min(8, image_count)),
                    "image_sources": sources,
                    "timestamp": timestamp,
                }
            )
        return output[-max(1, min(40, int(count or 24))) :]

    async def _recent_context_records_async(
        self, event: Optional[AstrMessageEvent], count: int = 12
    ) -> list[dict[str, Any]]:
        return await self._framework_context_records(event, count)

    def _recent_context_records(self, event: Optional[AstrMessageEvent], count: int = 12) -> List[Dict[str, Any]]:
        """Compatibility shim; synchronous callers must not invent local history."""
        return []

    async def _format_context_for_llm_async(
        self, event: Optional[AstrMessageEvent], count: int = 12, max_chars: int = 1400
    ) -> str:
        return format_context_for_llm(await self._recent_context_records_async(event, count), max_chars)

    def _format_context_for_llm(self, event: Optional[AstrMessageEvent], count: int = 12, max_chars: int = 1400) -> str:
        return ""

    async def _recent_context_image_sources_async(
        self,
        event: Optional[AstrMessageEvent],
        max_images: int = 4,
        *,
        prefer_user: bool = True,
        user_only: bool = False,
        bot_only: bool = False,
    ) -> List[str]:
        return recent_context_image_sources(
            await self._recent_context_records_async(event, count=24),
            max_images,
            prefer_user=prefer_user,
            user_only=user_only,
            bot_only=bot_only,
        )

    def _recent_context_image_sources(
        self,
        event: Optional[AstrMessageEvent],
        max_images: int = 4,
        *,
        prefer_user: bool = True,
        user_only: bool = False,
        bot_only: bool = False,
    ) -> List[str]:
        return []

    def _remember_llm_generation(self, event: Optional[AstrMessageEvent], kind: str, params: Dict[str, Any]) -> None:
        """保存本会话最近一次 LLM 生图请求，供重复生成使用。"""
        key = self._context_session_key(event)
        record = {
            "kind": str(kind or ""),
            "params": copy.deepcopy(params if isinstance(params, dict) else {}),
            "timestamp": time.time(),
        }
        lock = getattr(self, "_llm_generation_lock", None)
        if lock is None:
            return
        with lock:
            records = getattr(self, "_last_llm_generations", None)
            if not isinstance(records, OrderedDict):
                records = OrderedDict()
                self._last_llm_generations = records
            records[key] = record
            records.move_to_end(key)
            while len(records) > 100:
                records.popitem(last=False)

    def _last_llm_generation(self, event: Optional[AstrMessageEvent], feedback: str = "") -> Dict[str, Any]:
        """获取最近一次 LLM 生图请求；这不是会话历史存储。"""
        key = self._context_session_key(event)
        lock = getattr(self, "_llm_generation_lock", None)
        with lock if lock is not None else nullcontext():
            records = getattr(self, "_last_llm_generations", {})
            record = copy.deepcopy(records.get(key) or {})
        params = record.get("params") if isinstance(record.get("params"), dict) else {}
        comment = str(feedback or "").strip()
        if comment:
            field = "action" if record.get("kind") == "selfie" else "prompt"
            original = str(params.get(field) or "").strip()
            params[field] = "\n".join(item for item in (original, f"优先修正用户反馈：{comment}") if item)
        record["params"] = params
        return record

    def _compact_followup_text(self, text: str) -> str:
        return compact_followup_text(text)

    def _looks_like_context_image_reference(self, text: str) -> bool:
        return looks_like_context_image_reference(text)

    def _looks_like_clothes_followup(self, text: str) -> bool:
        return looks_like_clothes_followup(text)

    def _looks_like_edit_bot_result_followup(self, text: str) -> bool:
        return looks_like_edit_bot_result_followup(text)
