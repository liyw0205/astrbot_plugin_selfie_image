"""Conversation context and last-generation request helpers."""

from __future__ import annotations

import copy
import json
import os
import re
import time
import tempfile
from typing import Any, Dict, List, Optional

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
from ..core.utils import event_group_id, event_user_id, extract_event_text, extract_image_sources_from_event


class ConversationContextMixin:
    def _context_persistence_enabled(self) -> bool:
        raw = getattr(self, "raw_config", {})
        image = raw.get("image") if isinstance(raw, dict) else {}
        value = image.get("persist_context") if isinstance(image, dict) else False
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是", "开启"}
        return bool(value)

    def _context_store_path(self) -> str:
        data_dir = str(getattr(self, "data_dir", "") or "").strip()
        return os.path.join(data_dir, "conversation_context.json") if data_dir else ""

    def _load_persisted_conversation_context(self) -> None:
        if not self._context_persistence_enabled():
            return
        path = self._context_store_path()
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            raw_sessions = payload.get("sessions") if isinstance(payload, dict) else {}
            if not isinstance(raw_sessions, dict):
                return
            with self._context_lock:
                self._conversation_context.clear()
                for key, items in raw_sessions.items():
                    if not isinstance(items, list):
                        continue
                    rows = [self._normalize_persisted_context_row(item) for item in items]
                    rows = [item for item in rows if item]
                    if rows:
                        self._conversation_context[str(key)] = rows[-self._context_max_messages :]
                while len(self._conversation_context) > self._context_max_sessions:
                    self._conversation_context.popitem(last=False)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            # A corrupt optional store must not prevent plugin startup.
            return

    @staticmethod
    def _normalize_persisted_context_row(item: Any) -> Dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        try:
            image_count = int(item.get("image_count") or (1 if item.get("has_image") else 0))
        except (TypeError, ValueError):
            image_count = 1 if item.get("has_image") else 0
        try:
            timestamp = float(item.get("timestamp") or 0)
        except (TypeError, ValueError):
            timestamp = 0.0
        return {
            "msg_id": str(item.get("msg_id") or "")[:160],
            "sender_id": str(item.get("sender_id") or "")[:160],
            "sender_name": str(item.get("sender_name") or "用户")[:80],
            "content": str(item.get("content") or "")[:500],
            "is_bot": bool(item.get("is_bot")),
            "has_image": bool(item.get("has_image")),
            "image_sources": [],
            "image_count": max(0, min(8, image_count)),
            "timestamp": timestamp,
        }

    def _persist_conversation_context(self) -> None:
        if not self._context_persistence_enabled():
            return
        path = self._context_store_path()
        if not path:
            return
        with self._context_lock:
            sessions = {
                key: [
                    {
                        "msg_id": str(row.get("msg_id") or "")[:160],
                        "sender_id": str(row.get("sender_id") or "")[:160],
                        "sender_name": str(row.get("sender_name") or "用户")[:80],
                        "content": str(row.get("content") or "")[:500],
                        "is_bot": bool(row.get("is_bot")),
                        "has_image": bool(row.get("has_image")),
                        "image_count": max(0, min(8, len(row.get("image_sources") or []) or (1 if row.get("has_image") else 0))),
                        "timestamp": float(row.get("timestamp") or 0),
                    }
                    for row in rows[-self._context_max_messages :]
                    if isinstance(row, dict)
                ]
                for key, rows in self._conversation_context.items()
            }
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, temp_path = tempfile.mkstemp(prefix="conversation_context.", suffix=".tmp", dir=os.path.dirname(path))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "sessions": sessions}, handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(temp_path, path)
        except OSError:
            try:
                if 'temp_path' in locals() and os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass

    def get_conversation_context(self, event: Optional[AstrMessageEvent] = None, count: int = 40) -> Dict[str, Any]:
        key = self._context_session_key(event)
        with self._context_lock:
            rows = copy.deepcopy(self._conversation_context.get(key, []))[-max(1, min(40, int(count or 40))) :]
        for row in rows:
            row["image_sources"] = []
            row["image_count"] = max(0, min(8, int(row.get("image_count") or (1 if row.get("has_image") else 0))))
        return {"session_key": key, "enabled": self._context_persistence_enabled(), "count": len(rows), "messages": rows}

    def clear_conversation_context(self, event: Optional[AstrMessageEvent] = None) -> Dict[str, Any]:
        key = self._context_session_key(event)
        with self._context_lock:
            removed = len(self._conversation_context.pop(key, []) or [])
        self._persist_conversation_context()
        return {"session_key": key, "cleared": removed}

    def get_context(self, event: Optional[AstrMessageEvent] = None, count: int = 40) -> Dict[str, Any]:
        return self.get_conversation_context(event, count)

    def clear_context(self, event: Optional[AstrMessageEvent] = None) -> Dict[str, Any]:
        return self.clear_conversation_context(event)

    def _session_key(self, event: Optional[AstrMessageEvent] = None) -> str:
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

    def _event_sender_name(self, event: Optional[AstrMessageEvent], is_bot: bool = False) -> str:
        if is_bot:
            return self._bot_display_name()
        if event is None:
            return "用户"
        for method_name in ("get_sender_name", "get_sender_nickname", "get_user_name"):
            method = getattr(event, method_name, None)
            if callable(method):
                try:
                    value = str(method() or "").strip()
                    if value:
                        return value
                except Exception:
                    continue
        sender = getattr(event, "sender", None)
        for key in ("nickname", "name", "card", "display_name"):
            value = getattr(sender, key, None)
            if value:
                return str(value).strip()
        return event_user_id(event) or "用户"

    def _event_message_id(self, event: Optional[AstrMessageEvent]) -> str:
        if event is None:
            return f"web:{time.time_ns()}"
        message_obj = getattr(event, "message_obj", None)
        for obj in (message_obj, event):
            for key in ("message_id", "msg_id", "id"):
                value = getattr(obj, key, None)
                if value:
                    return str(value)
        return f"event:{id(event)}:{time.time_ns()}"

    def _add_context_message(
        self,
        session_key: str,
        sender_id: str,
        sender_name: str,
        content: str,
        is_bot: bool = False,
        image_sources: Optional[List[str]] = None,
        msg_id: str = "",
    ) -> None:
        key = str(session_key or "").strip() or "unknown"
        text = re.sub(r"\s+", " ", str(content or "")).strip()
        sources = [str(item).strip() for item in (image_sources or []) if str(item).strip()]
        if not text and sources:
            text = "[图片]"
        if not text and not sources:
            return
        record = {
            "msg_id": msg_id or f"{time.time_ns()}",
            "sender_id": str(sender_id or "").strip(),
            "sender_name": str(sender_name or "").strip() or ("[Bot]" if is_bot else "用户"),
            "content": text[:500],
            "is_bot": bool(is_bot),
            "has_image": bool(sources),
            "image_sources": sources[:8],
            "timestamp": time.time(),
        }
        with self._context_lock:
            messages = self._conversation_context.setdefault(key, [])
            if any(item.get("msg_id") == record["msg_id"] for item in messages[-5:]):
                return
            messages.append(record)
            if len(messages) > self._context_max_messages:
                del messages[: len(messages) - self._context_max_messages]
            self._conversation_context.move_to_end(key)
            while len(self._conversation_context) > self._context_max_sessions:
                self._conversation_context.popitem(last=False)
        self._persist_conversation_context()

    def _recent_context_records(self, event: Optional[AstrMessageEvent], count: int = 12) -> List[Dict[str, Any]]:
        key = self._context_session_key(event)
        with self._context_lock:
            records = list(self._conversation_context.get(key, []))
        return records[-max(1, int(count or 1)) :]

    def _remember_llm_generation(self, event: Optional[AstrMessageEvent], kind: str, params: Dict[str, Any]) -> None:
        """保存本会话最近一次 LLM 生图请求，供重复生成使用。"""
        key = self._context_session_key(event)
        record = {
            "kind": str(kind or ""),
            "params": copy.deepcopy(params if isinstance(params, dict) else {}),
            "timestamp": time.time(),
        }
        with self._llm_generation_lock:
            self._last_llm_generations[key] = record
            self._last_llm_generations.move_to_end(key)
            while len(self._last_llm_generations) > self._context_max_sessions:
                self._last_llm_generations.popitem(last=False)

    def _last_llm_generation(self, event: Optional[AstrMessageEvent], feedback: str = "") -> Dict[str, Any]:
        """获取最近一次 LLM 生图请求，并将用户修正要求附加到内容中。"""
        key = self._context_session_key(event)
        with self._llm_generation_lock:
            record = copy.deepcopy(self._last_llm_generations.get(key) or {})
        params = record.get("params") if isinstance(record.get("params"), dict) else {}
        comment = str(feedback or "").strip()
        if comment:
            field = "action" if record.get("kind") == "selfie" else "prompt"
            original = str(params.get(field) or "").strip()
            params[field] = "\n".join(item for item in (original, f"优先修正用户反馈：{comment}") if item)
        record["params"] = params
        return record

    def _format_context_for_llm(self, event: Optional[AstrMessageEvent], count: int = 12, max_chars: int = 1400) -> str:
        return format_context_for_llm(self._recent_context_records(event, count), max_chars)

    def _extract_context_message_info(self, event: AstrMessageEvent) -> Dict[str, Any]:
        content = extract_event_text(event)
        sources = self._filter_reference_images(event, extract_image_sources_from_event(event, include_at_avatar=False))
        return {"content": content or ("[图片]" if sources else ""), "image_sources": sources}

    def _compact_followup_text(self, text: str) -> str:
        return compact_followup_text(text)

    def _looks_like_context_image_reference(self, text: str) -> bool:
        return looks_like_context_image_reference(text)

    def _looks_like_clothes_followup(self, text: str) -> bool:
        return looks_like_clothes_followup(text)

    def _looks_like_edit_bot_result_followup(self, text: str) -> bool:
        return looks_like_edit_bot_result_followup(text)

    def _recent_context_image_sources(
        self,
        event: Optional[AstrMessageEvent],
        max_images: int = 4,
        *,
        prefer_user: bool = True,
        user_only: bool = False,
        bot_only: bool = False,
    ) -> List[str]:
        return recent_context_image_sources(
            self._recent_context_records(event, count=24),
            max_images,
            prefer_user=prefer_user,
            user_only=user_only,
            bot_only=bot_only,
        )
