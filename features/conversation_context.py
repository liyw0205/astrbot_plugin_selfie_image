"""Conversation context and last-generation request helpers."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
import tempfile
from collections.abc import Mapping
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
            "source": str(item.get("source") or "event")[:40],
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
                        "source": str(row.get("source") or "event")[:40],
                        "has_image": bool(row.get("has_image")),
                        "image_count": max(
                            0,
                            min(
                                8,
                                int(row.get("image_count") or 0)
                                or len(row.get("image_sources") or [])
                                or (1 if row.get("has_image") else 0),
                            ),
                        ),
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
        image_count: int = 0,
        source: str = "event",
        persist: bool = True,
    ) -> bool:
        key = str(session_key or "").strip() or "unknown"
        text = re.sub(r"\s+", " ", str(content or "")).strip()
        sources = [str(item).strip() for item in (image_sources or []) if str(item).strip()]
        try:
            declared_image_count = max(0, min(8, int(image_count or 0)))
        except (TypeError, ValueError):
            declared_image_count = 0
        actual_image_count = max(declared_image_count, len(sources))
        if not text and actual_image_count:
            text = "[图片]"
        if not text and not actual_image_count:
            return False
        record = {
            "msg_id": msg_id or f"{time.time_ns()}",
            "sender_id": str(sender_id or "").strip(),
            "sender_name": str(sender_name or "").strip() or ("[Bot]" if is_bot else "用户"),
            "content": text[:500],
            "is_bot": bool(is_bot),
            "source": str(source or "event").strip()[:40] or "event",
            "has_image": bool(actual_image_count),
            "image_sources": sources[:8],
            "image_count": actual_image_count,
            "timestamp": time.time(),
        }
        with self._context_lock:
            messages = self._conversation_context.setdefault(key, [])
            if any(item.get("msg_id") == record["msg_id"] for item in messages):
                return False
            messages.append(record)
            if len(messages) > self._context_max_messages:
                del messages[: len(messages) - self._context_max_messages]
            self._conversation_context.move_to_end(key)
            while len(self._conversation_context) > self._context_max_sessions:
                self._conversation_context.popitem(last=False)
        if persist:
            self._persist_conversation_context()
        return True

    @staticmethod
    def _provider_message_parts(content: Any) -> tuple[str, int]:
        """Extract text and media count from an OpenAI-style context message."""
        if isinstance(content, str):
            return re.sub(r"\s+", " ", content).strip(), 0
        if isinstance(content, Mapping):
            content_type = str(content.get("type") or "").strip().lower()
            if content_type in {"image_url", "image", "input_image"}:
                return "", 1
            value = content.get("text") or content.get("content") or ""
            return re.sub(r"\s+", " ", str(value)).strip(), 0
        if not isinstance(content, (list, tuple)):
            text = str(getattr(content, "text", "") or "").strip()
            return re.sub(r"\s+", " ", text).strip(), 0

        texts: List[str] = []
        image_count = 0
        for part in content:
            if isinstance(part, Mapping):
                part_type = str(part.get("type") or "").strip().lower()
                if part_type in {"image_url", "image", "input_image"}:
                    image_count += 1
                elif part_type in {"text", "input_text"}:
                    value = part.get("text") or part.get("content") or ""
                    if value:
                        texts.append(str(value))
                continue
            part_type = str(getattr(part, "type", "") or "").strip().lower()
            if part_type in {"image_url", "image", "input_image"}:
                image_count += 1
            else:
                value = getattr(part, "text", "") or getattr(part, "content", "") or ""
                if value:
                    texts.append(str(value))
        return re.sub(r"\s+", " ", " ".join(texts)).strip(), image_count

    def _provider_context_session_key(self, event: Optional[AstrMessageEvent], request: Any = None) -> str:
        """Use AstrBot's event origin first, then ProviderRequest.session_id."""
        if event is not None:
            origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if origin:
                return origin
        session_id = str(getattr(request, "session_id", "") or "").strip()
        return session_id or self._context_session_key(event)

    def _sync_provider_context(self, event: Optional[AstrMessageEvent], request: Any) -> int:
        """Mirror a bounded, sanitized view of AstrBot's official context."""
        contexts = getattr(request, "contexts", None)
        if not isinstance(contexts, (list, tuple)):
            return 0
        session_key = self._provider_context_session_key(event, request)
        provider_rows: List[Dict[str, Any]] = []
        provider_ids = set()
        for item in list(contexts)[-self._context_max_messages :]:
            if not isinstance(item, Mapping):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            text, image_count = self._provider_message_parts(item.get("content"))
            if not text and not image_count:
                continue
            digest = hashlib.sha1(
                f"{role}\0{text}\0{image_count}".encode("utf-8", "ignore")
            ).hexdigest()[:24]
            msg_id = f"astrbot:{role}:{digest}"
            provider_ids.add(msg_id)
            provider_rows.append(
                {
                    "role": role,
                    "text": text,
                    "image_count": image_count,
                    "msg_id": msg_id,
                }
            )
        removed = 0
        with self._context_lock:
            messages = self._conversation_context.get(session_key, [])
            if messages:
                kept = [
                    row
                    for row in messages
                    if str(row.get("source") or "") != "astrbot"
                    or row.get("msg_id") in provider_ids
                ]
                removed = len(messages) - len(kept)
                if removed:
                    self._conversation_context[session_key] = kept
        added = 0
        # The host owns the full history; the plugin only needs a small recent view
        # for follow-up routing and its own acknowledgement prompt.
        for row in provider_rows:
            role = row["role"]
            added += int(
                self._add_context_message(
                    session_key=session_key,
                    sender_id="astrbot",
                    sender_name=(
                        "用户"
                        if role == "user"
                        else (
                            self._bot_display_name()
                            if callable(getattr(self, "_bot_display_name", None))
                            else "Bot"
                        )
                    ),
                    content=row["text"],
                    is_bot=role == "assistant",
                    image_count=row["image_count"],
                    msg_id=row["msg_id"],
                    source="astrbot",
                    persist=False,
                )
            )
        if added or removed:
            self._persist_conversation_context()
        return added

    def _record_llm_response_context(self, event: Optional[AstrMessageEvent], response: Any) -> bool:
        """Record a completed assistant response from AstrBot's response hook."""
        if response is None or str(getattr(response, "role", "") or "").lower() != "assistant":
            return False
        if bool(getattr(response, "is_chunk", False)):
            return False
        if getattr(response, "tools_call_name", None) or getattr(response, "tools_call_extra_content", None):
            return False
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            return False
        if event is None and not str(getattr(response, "id", "") or "").strip():
            return False
        key = self._provider_context_session_key(event)
        recent = self._recent_context_records(event, count=6)
        now = time.time()
        if any(
            bool(row.get("is_bot"))
            and str(row.get("content") or "").strip() == text
            and now - float(row.get("timestamp") or 0) <= 8
            for row in recent
        ):
            return False
        response_id = str(getattr(response, "id", "") or "").strip()
        event_id = self._event_message_id(event) if event is not None else ""
        digest = hashlib.sha1(f"{key}\0{event_id}\0{text}".encode("utf-8", "ignore")).hexdigest()[:24]
        return self._add_context_message(
            session_key=key,
            sender_id="bot",
            sender_name=self._bot_display_name() if callable(getattr(self, "_bot_display_name", None)) else "Bot",
            content=text,
            is_bot=True,
            msg_id=f"llm-response:{response_id or digest}",
            source="llm_response",
        )

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
