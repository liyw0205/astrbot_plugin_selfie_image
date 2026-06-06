"""AstrBot image and selfie generation plugin."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import random
import re
import shutil
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any, AsyncGenerator, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import aiohttp

try:
    from astrbot.api.star import Context, Star, register
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.message_components import Image
    from astrbot.api import llm_tool, logger
except ImportError:  # Compatibility with older AstrBot layouts
    from astrbot.api.star import Context, Star, register
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.event.components import Image
    from astrbot.api import llm_tool
    from astrbot.api.utils import logger

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:
    def get_astrbot_data_path() -> str:
        return os.path.join(os.getcwd(), "data")

from .constants import (
    LEGACY_CONFIG_FILENAME,
    LEGACY_PLUGIN_NAME,
    PLUGIN_AUTHOR,
    PLUGIN_CONFIG_FILENAME,
    PLUGIN_DISPLAY_NAME,
    PLUGIN_NAME,
    PLUGIN_VERSION,
)
from .generator import generate_image_with_fallback
from .preset import ImagePresetManager
from .models import (
    AICatConfig,
    DEFAULT_CONFIG,
    ImageModelTarget,
    deep_merge,
    normalize_config_tree,
    normalize_legacy_keys,
)
from .persona import PersonaManager
from .providers import ImageGenerateRequest, ImageReference, normalize_image_base_url
from .utils import (
    bytes_to_data_url,
    data_url_to_bytes,
    detect_mime_by_bytes,
    event_group_id,
    event_user_id,
    extract_command_message,
    extract_event_text,
    extract_image_sources_from_event,
    extract_image_urls,
    fetch_image_source,
    load_json_file,
    normalize_image_mime,
    save_image_bytes,
    save_json_file,
)
from .web import FlaskWebServer

LLM_TOOL = getattr(filter, "llm_tool", llm_tool)


def optional_event_message_type(priority: int = 100):
    decorator = getattr(filter, "event_message_type", None)
    event_type = getattr(getattr(filter, "EventMessageType", None), "ALL", None)
    if callable(decorator) and event_type is not None:
        return decorator(event_type, priority=priority)

    def passthrough(func):
        return func

    return passthrough


def append_anatomy_constraints(prompt: str) -> str:
    raw = str(prompt or "").strip()
    if not raw:
        return raw
    return "\n".join(
        [
            raw,
            "",
            "Composition and quality:",
            "Use a coherent single image with natural lighting, stable perspective, clear subject focus, and complete natural anatomy for people or animals.",
            "Frame visible bodies cleanly and keep hands, feet, posture, clothing, and scene relationships consistent.",
        ]
    )


def build_prompt_with_reference_instruction(prompt: str, images: List[ImageReference]) -> str:
    raw = str(prompt or "").strip()
    if not images:
        return append_anatomy_constraints(raw)
    return "\n".join(
        [
            "Use the provided reference image(s) as visual references.",
            "Follow the user's requested changes while preserving relevant identity, face, hairstyle, outfit, body shape, pose, camera angle, scene, style, and composition.",
            "If a reference contains multiple visible people or characters, keep them as distinct subjects; follow any user-requested subset.",
            "When multiple references are provided, assign each reference to its requested role: identity, clothing, pose, style, scene, object, or group member.",
            "Create one coherent image with unified lighting, perspective, color tone, natural complete anatomy, and clear spatial relationships.",
            "Frame the subjects cleanly with a finished photo-like composition.",
            "",
            "User request:",
            raw,
        ]
    )


@register(PLUGIN_NAME, PLUGIN_AUTHOR, f"{PLUGIN_DISPLAY_NAME} v{PLUGIN_VERSION}", PLUGIN_VERSION)
class SelfieImagePlugin(Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        plugin_data_dir = os.path.join(str(get_astrbot_data_path()), "plugin_data")
        self.data_dir = os.path.join(plugin_data_dir, PLUGIN_NAME)
        self._migrate_legacy_data_dir(plugin_data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.config_path = os.path.join(self.data_dir, PLUGIN_CONFIG_FILENAME)
        self._migrate_legacy_config_file()
        self.usage_path = os.path.join(self.data_dir, "usage_stats.json")
        self.records_path = os.path.join(self.data_dir, "generation_records.json")
        self.generated_dir = os.path.join(self.data_dir, "image_cache")
        os.makedirs(self.generated_dir, exist_ok=True)

        self._native_config = config if hasattr(config, "save_config") else None
        self._native_config_path = str(getattr(config, "config_path", "") or "")
        self._config_lock = threading.RLock()
        self._usage_lock = threading.RLock()
        self._records_lock = threading.RLock()
        self._progress_lock = threading.RLock()
        self._progress_last_sent: Dict[str, float] = {}
        self._context_lock = threading.RLock()
        self._conversation_context: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
        self._context_max_messages = 40
        self._context_max_sessions = 100
        self._records: List[Dict[str, Any]] = self._load_records()
        self._record_seq = len(self._records)
        self._web_task_lock = threading.RLock()
        self._web_tasks: Dict[str, Dict[str, Any]] = {}
        self._web_task_seq = 0
        self._last_request_at: Dict[str, float] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        native_config = self._config_object_to_dict(config)
        if not native_config and self._native_config_path:
            native_config = load_json_file(self._native_config_path)
        self.key_config = self._extract_native_key_config(native_config)
        self.raw_config = self._load_initial_config()
        self.config = AICatConfig.from_dict(self.raw_config)
        self.persona = PersonaManager(self.data_dir)
        self.presets = ImagePresetManager(self.data_dir)
        self._usage_stats = self._load_usage_stats()
        self._semaphore = asyncio.Semaphore(self.config.image_max_concurrent_tasks)
        self.web_server = FlaskWebServer(self)

        # Do not write config files during startup. If AstrBot passes an empty
        # or not-yet-populated config object, writing here would overwrite the
        # user's saved config with defaults before the plugin is usable.

    async def initialize(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._start_web_server()

    async def terminate(self) -> None:
        self.web_server.stop()

    def _migrate_legacy_data_dir(self, plugin_data_dir: str) -> None:
        if os.path.exists(self.data_dir):
            return
        legacy_dir = os.path.join(plugin_data_dir, LEGACY_PLUGIN_NAME)
        if not os.path.isdir(legacy_dir):
            return
        try:
            shutil.copytree(legacy_dir, self.data_dir)
            logger.info(f"[SelfieImage] 已迁移旧数据目录: {legacy_dir} -> {self.data_dir}")
        except Exception as exc:
            logger.warning(f"[SelfieImage] 迁移旧数据目录失败: {exc}", exc_info=True)

    def _migrate_legacy_config_file(self) -> None:
        legacy_path = os.path.join(self.data_dir, LEGACY_CONFIG_FILENAME)
        if os.path.exists(self.config_path) or not os.path.exists(legacy_path):
            return
        try:
            shutil.copy2(legacy_path, self.config_path)
            logger.info(f"[SelfieImage] 已迁移旧配置文件: {legacy_path} -> {self.config_path}")
        except Exception as exc:
            logger.warning(f"[SelfieImage] 迁移旧配置文件失败: {exc}", exc_info=True)

    def _config_object_to_dict(self, value: Any) -> Dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, Mapping):
            return {str(key): self._plain_config_value(item) for key, item in value.items()}
        for method_name in ("to_dict", "dict", "model_dump"):
            method = getattr(value, method_name, None)
            if not callable(method):
                continue
            try:
                converted = method()
                if isinstance(converted, Mapping):
                    return {str(key): self._plain_config_value(item) for key, item in converted.items()}
            except Exception:
                continue
        items = getattr(value, "items", None)
        if callable(items):
            try:
                return {str(key): self._plain_config_value(item) for key, item in items()}
            except Exception:
                pass
        keys = getattr(value, "keys", None)
        if callable(keys):
            try:
                return {str(key): self._plain_config_value(value[key]) for key in keys()}
            except Exception:
                pass
        return {}

    def _plain_config_value(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): self._plain_config_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._plain_config_value(item) for item in value]
        if isinstance(value, tuple):
            return [self._plain_config_value(item) for item in value]
        return copy.deepcopy(value)

    def _extract_native_key_config(self, native_config: Dict[str, Any]) -> Dict[str, Any]:
        normalized = normalize_legacy_keys(normalize_config_tree(copy.deepcopy(native_config or {})))
        web = normalized.get("web") if isinstance(normalized.get("web"), dict) else {}
        key_config = {"web": copy.deepcopy(DEFAULT_CONFIG["web"])}
        for key in ("enable", "host", "port", "token"):
            if key in web:
                key_config["web"][key] = web[key]
        return key_config

    def _load_initial_config(self) -> Dict[str, Any]:
        persisted = load_json_file(self.config_path)
        source = normalize_legacy_keys(normalize_config_tree(deep_merge(DEFAULT_CONFIG, persisted)))
        source["web"] = copy.deepcopy(self.key_config["web"])
        return source

    def _persist_config(self) -> None:
        with self._config_lock:
            web_config = copy.deepcopy(self.raw_config)
            web_config.pop("web", None)
            save_json_file(self.config_path, web_config)

    def _apply_raw_config(self, raw: Dict[str, Any]) -> None:
        next_config = normalize_legacy_keys(normalize_config_tree(deep_merge(DEFAULT_CONFIG, raw)))
        next_config["web"] = copy.deepcopy(self.key_config["web"])
        self.raw_config = next_config
        self.config = AICatConfig.from_dict(self.raw_config)
        self._semaphore = asyncio.Semaphore(self.config.image_max_concurrent_tasks)
        self._persist_config()

    def _start_web_server(self) -> None:
        if not self.config.web_enable:
            return
        try:
            self.web_server.start(self.config.web_host, self.config.web_port)
            logger.info(f"[SelfieImage] Flask Web 已启动: http://{self.config.web_host}:{self.config.web_port}")
        except Exception as exc:
            logger.error(f"[SelfieImage] Flask Web 启动失败: {exc}", exc_info=True)

    def get_config_for_web(self) -> Dict[str, Any]:
        web_config = copy.deepcopy(self.raw_config)
        web_config.pop("web", None)
        return web_config

    def update_config_from_web(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._config_lock:
            patch = copy.deepcopy(patch)
            patch.pop("web", None)
            self._apply_raw_config(deep_merge(self.raw_config, patch))
            return self.get_config_for_web()

    def _today_key(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def _load_usage_stats(self) -> Dict[str, Any]:
        stats = load_json_file(self.usage_path)
        if stats.get("date") != self._today_key():
            return {"date": self._today_key(), "users": {}}
        if not isinstance(stats.get("users"), dict):
            stats["users"] = {}
        return stats

    def _current_usage_stats(self) -> Dict[str, Any]:
        with self._usage_lock:
            if self._usage_stats.get("date") != self._today_key():
                self._usage_stats = {"date": self._today_key(), "users": {}}
            return self._usage_stats

    def _persist_usage_stats(self) -> None:
        with self._usage_lock:
            save_json_file(self.usage_path, self._current_usage_stats())

    def _load_records(self) -> List[Dict[str, Any]]:
        data = load_json_file(self.records_path)
        items = data.get("records") if isinstance(data.get("records"), list) else []
        records = [item for item in items if isinstance(item, dict)]
        return copy.deepcopy(records[:100])

    def _persist_records(self) -> None:
        with self._records_lock:
            save_json_file(self.records_path, {"records": self._records[:100]})

    def _record_generated_images(self, event: AstrMessageEvent, count: int) -> None:
        user_id = event_user_id(event)
        if not user_id:
            return
        stats = self._current_usage_stats()
        users = stats.setdefault("users", {})
        record = users.setdefault(user_id, {"count": 0, "last_at": 0})
        record["count"] = int(record.get("count", 0)) + max(0, int(count))
        record["last_at"] = int(time.time())
        record["group_id"] = event_group_id(event)
        self._persist_usage_stats()

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

    def _recent_context_records(self, event: Optional[AstrMessageEvent], count: int = 12) -> List[Dict[str, Any]]:
        key = self._context_session_key(event)
        with self._context_lock:
            records = list(self._conversation_context.get(key, []))
        return records[-max(1, int(count or 1)) :]

    def _format_context_for_llm(self, event: Optional[AstrMessageEvent], count: int = 12, max_chars: int = 1400) -> str:
        lines: List[str] = []
        total = 0
        for record in reversed(self._recent_context_records(event, count)):
            sender = "[Bot]" if record.get("is_bot") else str(record.get("sender_name") or "用户")
            content = str(record.get("content") or "").strip()
            image_tag = " [含图片]" if record.get("has_image") else ""
            line = f"{sender}: {content}{image_tag}".strip()
            if not line:
                continue
            if total + len(line) > max_chars:
                break
            lines.insert(0, line)
            total += len(line) + 1
        return "\n".join(lines)

    def _extract_context_message_info(self, event: AstrMessageEvent) -> Dict[str, Any]:
        content = extract_event_text(event)
        sources = self._filter_reference_images(event, extract_image_sources_from_event(event, include_at_avatar=False))
        return {"content": content or ("[图片]" if sources else ""), "image_sources": sources}

    def _looks_like_context_image_reference(self, text: str) -> bool:
        compact = re.sub(r"[\s，。！？、；：,.!?;:]+", "", str(text or "").lower())
        if not compact:
            return False
        keywords = [
            "上图",
            "上一张",
            "上张",
            "刚才那张",
            "刚刚那张",
            "刚发的",
            "前面那张",
            "这张",
            "这图",
            "这个图",
            "那张",
            "那图",
            "继续改",
            "接着改",
            "在这个基础上",
            "基于这张",
            "参考这个",
            "参考刚才",
            "按刚才",
            "同款",
            "换成",
            "改一下",
            "修一下",
        ]
        english = [
            "previousimage",
            "lastimage",
            "lastphoto",
            "thisimage",
            "editthis",
            "continueediting",
            "basedonthis",
            "sameasbefore",
        ]
        return any(keyword in compact for keyword in keywords) or any(keyword in compact for keyword in english)

    def _recent_context_image_sources(self, event: Optional[AstrMessageEvent], max_images: int = 4) -> List[str]:
        sources: List[str] = []
        seen = set()
        for record in reversed(self._recent_context_records(event, count=20)):
            for source in reversed(list(record.get("image_sources") or [])):
                text = str(source or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                sources.append(text)
                if len(sources) >= max_images:
                    return sources
        return sources

    @optional_event_message_type(priority=100)
    async def on_message_record(self, event: AstrMessageEvent) -> None:
        try:
            msg = self._extract_context_message_info(event)
            sender_id = event_user_id(event)
            bot_ids = set(self._bot_account_ids(event))
            is_bot = bool(sender_id and sender_id in bot_ids)
            self._add_context_message(
                session_key=self._context_session_key(event),
                sender_id=sender_id,
                sender_name=self._event_sender_name(event, is_bot=is_bot),
                content=str(msg.get("content") or ""),
                is_bot=is_bot,
                image_sources=list(msg.get("image_sources") or []),
                msg_id=self._event_message_id(event),
            )
        except Exception as exc:
            logger.debug(f"[SelfieImage] 记录上下文失败: {exc}")
        return None

    def _access_status(self, event: AstrMessageEvent) -> Dict[str, Any]:
        user_id = event_user_id(event)
        group_id = event_group_id(event)
        status = {"user_id": user_id, "group_id": group_id, "allowed": True, "unlimited": False, "whitelist": False, "reason": ""}
        if user_id and user_id in self.config.blocked_users:
            status.update({"allowed": False, "reason": "用户黑名单"})
            return status
        if self.config.usable_users and user_id not in self.config.usable_users:
            status.update({"allowed": False, "reason": "可使用人员白名单"})
            return status
        if user_id in self.config.whitelist_users or (group_id and group_id in self.config.whitelist_groups):
            status["unlimited"] = True
            status["whitelist"] = True
        return status

    def _permission_denied_message(self, event: AstrMessageEvent) -> str:
        status = self._access_status(event)
        if status.get("allowed"):
            return ""
        if status.get("reason") == "可使用人员白名单":
            return "当前仅允许可使用人员白名单内用户使用生图功能。"
        return "你已被加入用户黑名单，无法使用生图功能。"

    def _quota_error_message(self, event: AstrMessageEvent, requested_count: int = 1) -> str:
        permission_error = self._permission_denied_message(event)
        if permission_error:
            return permission_error
        if not self.config.image_enable_daily_limit:
            return ""
        status = self._access_status(event)
        if status.get("unlimited"):
            return ""
        user_id = status.get("user_id") or ""
        used = int(self._current_usage_stats().get("users", {}).get(user_id, {}).get("count", 0))
        limit = self.config.image_daily_limit_count
        if used + max(1, requested_count) <= limit:
            return ""
        return f"今日生图次数已用完：{used}/{limit}。"

    def _rate_limit_error_message(self, event: AstrMessageEvent) -> str:
        if self._is_whitelisted(event):
            return ""
        seconds = self.config.image_rate_limit_seconds
        if seconds <= 0:
            return ""
        user_id = event_user_id(event)
        if not user_id:
            return ""
        now = time.time()
        last = self._last_request_at.get(user_id, 0)
        remain = int(seconds - (now - last))
        if remain > 0:
            return f"请求太频繁，请 {remain} 秒后再试。"
        self._last_request_at[user_id] = now
        return ""

    def _is_whitelisted(self, event: Optional[AstrMessageEvent] = None, user_id: str = "") -> bool:
        if event is None:
            return True
        status = self._access_status(event)
        return bool(status.get("whitelist") or (user_id and user_id in self.config.whitelist_users))

    def _is_audit_exempt(self, event: Optional[AstrMessageEvent] = None, user_id: str = "") -> bool:
        return bool(event is not None and self._is_whitelisted(event, user_id))

    def _validate_prompt(self, prompt: str, user_id: str = "", event: Optional[AstrMessageEvent] = None) -> str:
        if self._is_audit_exempt(event, user_id):
            return ""
        text = str(prompt or "")
        low_text = text.lower()
        for word in self.config.image_blocked_words:
            if word and str(word).lower() in low_text:
                return f"提示词包含禁用词：{word}"
        return ""

    def _bot_display_name(self) -> str:
        name = str(self.config.bot_name or "").strip()
        return name or "啊呜"

    def _compact_for_repeat_check(self, text: str) -> str:
        return re.sub(r"[\s`*_~\"'“”‘’「」『』《》()\[\]{}，。！？、；：,.!?;:\-_/\\|]+", "", str(text or "")).lower()

    def _ack_repeats_request(self, ack_message: str, user_request: str) -> bool:
        request = str(user_request or "").strip()
        if not request:
            return False
        ack_compact = self._compact_for_repeat_check(ack_message)
        request_compact = self._compact_for_repeat_check(request)
        if len(request_compact) >= 8 and request_compact in ack_compact:
            return True
        for piece in re.split(r"[\s，。！？、；：,.!?;:]+", request):
            piece_compact = self._compact_for_repeat_check(piece)
            if len(piece_compact) >= 8 and piece_compact in ack_compact:
                return True
        return False

    def _looks_like_non_chinese_ack(self, text: str) -> bool:
        raw = str(text or "")
        if not raw:
            return False
        chinese_count = len(re.findall(r"[\u4e00-\u9fff]", raw))
        latin_count = len(re.findall(r"[A-Za-z]", raw))
        if chinese_count == 0 and latin_count >= 4:
            return True
        return latin_count > chinese_count * 2 and latin_count >= 12

    def _clean_ack_message(self, ack_message: str, user_request: str) -> str:
        custom = re.sub(r"\s+", " ", str(ack_message or "")).strip()
        if not custom:
            return ""
        if self._looks_like_non_chinese_ack(custom):
            return ""
        if self._ack_repeats_request(custom, user_request):
            return ""
        stiff_markers = [
            "沿着",
            "顺着",
            "照着",
            "按这个",
            "按照这个",
            "根据你的提示",
            "根据用户",
            "用户需求",
            "用户要求",
            "提示词",
            "prompt",
            "生图",
            "工具",
            "配置",
            "正在生成",
            "开始生成",
            "为你生成",
            "帮你生成",
            "收到",
            "已收到",
        ]
        low = custom.lower()
        if any(marker.lower() in low for marker in stiff_markers):
            return ""
        return custom[:80]

    def _natural_ack_fallback(self, kind: str, count: int) -> str:
        name = self._bot_display_name()
        multi = count > 1
        if kind == "selfie":
            options = [
                f"{name}去找一下角度。",
                "等我一下，我对下光线。",
                "我换个顺眼点的构图。",
                "我先把画面收一下。",
                "稍等，我抓个自然点的瞬间。",
                "我试个更日常的角度。",
                "等我，我把镜头感压轻一点。",
                "我先看一下怎么拍更舒服。",
            ]
            if multi:
                options.extend(["我多试几个角度。", f"{name}多拍几张看看。", "我换几版构图。"])
            return random.choice(options)
        options = [
            f"{name}先把画面理一下。",
            "我先想一下构图。",
            "等我一下，我搭个画面。",
            "我试着把这个感觉做出来。",
            "稍等，我换个画面方向。",
            "我先对一下主体和光线。",
        ]
        if multi:
            options.extend(["我多试几版构图。", f"{name}多跑几张看看。"])
        return random.choice(options)

    def _natural_fail_fallback(self, kind: str = "") -> str:
        options_by_kind = {
            "legs": [
                "刚刚那版腿部比例不太顺。",
                "这次腿部构图没出来。",
                "刚才那张下半身有点乱。",
                "这版角度不太对。",
                "刚刚那张效果不行。",
            ],
            "selfie": [
                "刚刚那版不太像我。",
                "这次镜头感有点跑偏。",
                "刚才那张效果不太对。",
                "这版没出来想要的感觉。",
                "刚刚那张不太行。",
            ],
            "group": [
                "刚刚那版同框效果不太对。",
                "这次合影站位有点乱。",
                "刚才那张人物关系没处理好。",
                "这版合照没出来想要的感觉。",
            ],
            "image": [
                "刚刚那版画面不太对。",
                "这次效果没出来。",
                "刚才那张没成。",
                "这版构图有点跑偏。",
            ],
        }
        options = options_by_kind.get(kind) or options_by_kind["image"]
        return random.choice(options)

    def _selfie_ack_text(self, action: str, count: int, ack_message: str = "") -> str:
        custom = self._clean_ack_message(ack_message, action)
        if custom:
            return custom
        return self._natural_ack_fallback("selfie", count)

    def _image_ack_text(self, prompt: str, count: int, ack_message: str = "") -> str:
        custom = self._clean_ack_message(ack_message, prompt)
        if custom:
            return custom
        return self._natural_ack_fallback("image", count)

    def _progress_text_allowed(self, event: Optional[AstrMessageEvent]) -> bool:
        key = self._session_key(event)
        now = time.time()
        with self._progress_lock:
            last = self._progress_last_sent.get(key, 0.0)
            if now - last < 8:
                return False
            self._progress_last_sent[key] = now
            return True

    def _bot_account_ids(self, event: Optional[AstrMessageEvent] = None) -> List[str]:
        ids = set()
        context_keys = ("bot_id", "self_id", "account_id", "qq", "uin", "user_id")
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
                if callable(getter):
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
        return any(re.search(rf"(?<!\d){re.escape(bot_id)}(?!\d)", target) for bot_id in ids)

    def _filter_reference_images(self, event: Optional[AstrMessageEvent], sources: List[str]) -> List[str]:
        if not sources:
            return sources
        bot_ids = set(self._bot_account_ids(event))
        if not bot_ids:
            return sources
        filtered: List[str] = []
        for source in sources:
            if self._reference_source_is_bot_avatar(source, bot_ids):
                continue
            filtered.append(source)
        return filtered

    def _parse_audit_response(self, text: str) -> Tuple[bool, str]:
        raw = str(text or "").strip()
        if not raw:
            return False, "审核模型返回为空"
        fenced = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", raw, flags=re.I)
        json_text = fenced.group(1).strip() if fenced else raw
        obj: Any = None
        try:
            obj = json.loads(json_text)
        except Exception:
            match = re.search(r"\{[\s\S]*\}", json_text)
            if match:
                try:
                    obj = json.loads(match.group(0))
                except Exception:
                    obj = None
        if isinstance(obj, dict):
            allow = obj.get("allow")
            reason = str(obj.get("reason") or "").strip()
            if isinstance(allow, bool):
                return allow, reason
            if isinstance(allow, str):
                low_allow = allow.strip().lower()
                if low_allow in ("true", "yes", "allow", "allowed", "通过", "允许", "安全"):
                    return True, reason
                if low_allow in ("false", "no", "deny", "denied", "拒绝", "不通过", "违规", "不安全"):
                    return False, reason

        low = json_text.lower()
        if "false" in low or "unsafe" in low or "拒绝" in json_text or "不通过" in json_text or "违规" in json_text or "不安全" in json_text:
            return False, json_text[:120]
        if "true" in low or "通过" in json_text or "允许" in json_text or "安全" in json_text:
            return True, json_text[:120]
        return False, f"无法判定审核结果: {json_text[:120]}"

    def _find_audit_target(self, label: str) -> Optional[ImageModelTarget]:
        value = str(label or "").strip()
        if not value:
            return None
        targets = self.config.get_audit_targets()
        if "/" in value:
            channel_name, model = value.split("/", 1)
            channel_name = channel_name.strip()
            model = model.strip()
            for target in targets:
                if target.channel_name == channel_name and target.model == model:
                    return target
            return None
        for target in targets:
            if target.model == value:
                return target
        return None

    async def _audit_chat_via_target(self, target: ImageModelTarget, text: str, images: Optional[List[bytes]] = None) -> str:
        images = images or []
        provider_type = str(target.provider_type or "").lower()
        timeout = aiohttp.ClientTimeout(total=max(10, int(target.timeout or self.config.image_global_timeout or 180)))
        proxy = str(target.proxy or "").strip() or None

        async with aiohttp.ClientSession() as session:
            if provider_type == "gemini":
                base = normalize_image_base_url(target.base_url) or "https://generativelanguage.googleapis.com"
                base = re.sub(r"/v1beta(?:/.*)?$", "", base.rstrip("/"), flags=re.I)
                model_path = target.model if target.model.startswith("models/") else f"models/{target.model}"
                url = f"{base}/v1beta/{model_path}:generateContent"
                parts: List[Dict[str, Any]] = [{"text": text}]
                for image in images:
                    parts.append(
                        {
                            "inline_data": {
                                "mime_type": detect_mime_by_bytes(image),
                                "data": bytes_to_data_url(image, detect_mime_by_bytes(image)).split(",", 1)[-1],
                            }
                        }
                    )
                headers = {"Content-Type": "application/json", "Accept": "application/json"}
                if target.api_key:
                    headers["x-goog-api-key"] = target.api_key
                async with session.post(url, json={"contents": [{"parts": parts}]}, headers=headers, timeout=timeout, proxy=proxy) as response:
                    if response.status >= 400:
                        raise RuntimeError(f"审核接口失败: HTTP {response.status} {(await response.text())[:200]}")
                    data = await response.json(content_type=None)
                texts: List[str] = []
                for candidate in data.get("candidates", []) if isinstance(data, dict) else []:
                    content = candidate.get("content") if isinstance(candidate, dict) else {}
                    for part in content.get("parts", []) if isinstance(content, dict) else []:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            texts.append(part["text"])
                return "\n".join(texts).strip()

            base = normalize_image_base_url(target.base_url) or "https://api.openai.com"
            url = f"{base}/v1/chat/completions"
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if target.api_key:
                headers["Authorization"] = f"Bearer {target.api_key}"
            content: Any
            if images:
                content = [{"type": "text", "text": text}]
                for image in images:
                    content.append({"type": "image_url", "image_url": {"url": bytes_to_data_url(image, detect_mime_by_bytes(image))}})
            else:
                content = text
            payload = {"model": target.model, "messages": [{"role": "user", "content": content}], "stream": False}
            async with session.post(url, json=payload, headers=headers, timeout=timeout, proxy=proxy) as response:
                if response.status >= 400:
                    raise RuntimeError(f"审核接口失败: HTTP {response.status} {(await response.text())[:200]}")
                data = await response.json(content_type=None)
            if isinstance(data, dict):
                choices = data.get("choices")
                if isinstance(choices, list) and choices:
                    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
                    if isinstance(message, dict):
                        content = message.get("content")
                        if isinstance(content, str):
                            return content.strip()
                        if isinstance(content, list):
                            parts = [str(item.get("text") or "") for item in content if isinstance(item, dict)]
                            return "\n".join(part for part in parts if part).strip()
            return ""

    async def _audit_prompt_via_astrbot(self, event: Optional[AstrMessageEvent], text: str) -> str:
        if event is None:
            return ""
        provider_id = None
        origin = getattr(event, "unified_msg_origin", None)
        try:
            getter = getattr(self.context, "get_using_provider", None)
            if callable(getter):
                provider = getter()
                requester = getattr(provider, "text_chat", None) or getattr(provider, "request", None)
                if callable(requester):
                    response = requester(prompt=text)
                    if asyncio.iscoroutine(response):
                        response = await response
                    return str(getattr(response, "completion_text", response) or "").strip()
        except Exception:
            pass
        try:
            getter = getattr(self.context, "get_current_chat_provider_id", None)
            if callable(getter):
                provider_id = await getter(umo=origin) if origin else await getter()
        except Exception:
            provider_id = None
        try:
            generator = getattr(self.context, "llm_generate", None)
            if callable(generator):
                kwargs = {"prompt": text}
                if provider_id:
                    kwargs["chat_provider_id"] = provider_id
                response = await generator(**kwargs)
                return str(getattr(response, "completion_text", response) or "").strip()
        except Exception:
            return ""
        return ""

    async def _audit_prompt(self, prompt: str, user_id: str = "", event: Optional[AstrMessageEvent] = None) -> Tuple[bool, str]:
        error = self._validate_prompt(prompt, user_id, event)
        if error:
            return False, error
        if self._is_audit_exempt(event, user_id):
            return True, ""
        if not self.config.image_enable_prompt_audit:
            return True, ""

        audit_prompt = self.config.image_prompt_audit_template.replace("{prompt}", str(prompt or ""))
        try:
            target = self._find_audit_target(self.config.image_prompt_audit_model)
            if target:
                text = await self._audit_chat_via_target(target, audit_prompt)
            elif event is not None:
                text = await self._audit_prompt_via_astrbot(event, audit_prompt)
            else:
                return False, "未配置可用提示词审核模型"
        except Exception as exc:
            return False, str(exc)
        return self._parse_audit_response(text)

    async def _audit_output_images(self, files: List[str], user_id: str = "", prompt: str = "", event: Optional[AstrMessageEvent] = None) -> Tuple[bool, str]:
        if self._is_audit_exempt(event, user_id):
            return True, ""
        if not self.config.image_enable_output_audit:
            return True, ""
        if not files:
            return False, "没有待审核图片"

        target = self._find_audit_target(self.config.image_output_audit_model)
        if target is None:
            return False, "未配置可用出图审核模型"
        images: List[bytes] = []
        for file_path in files:
            with open(file_path, "rb") as handle:
                images.append(handle.read())
        audit_prompt = self.config.image_output_audit_template.replace("{prompt}", str(prompt or ""))
        try:
            text = await self._audit_chat_via_target(target, audit_prompt, images=images)
        except Exception as exc:
            return False, str(exc)
        return self._parse_audit_response(text)

    def _record_task(self, record: Dict[str, Any]) -> None:
        with self._records_lock:
            record = dict(record)
            self._record_seq += 1
            record.setdefault("id", f"{int(time.time() * 1000)}-{self._record_seq}")
            record["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            self._records.insert(0, record)
            del self._records[100:]
            self._persist_records()

    def get_recent_records(self) -> List[Dict[str, Any]]:
        with self._records_lock:
            return copy.deepcopy(self._records[:100])

    def clear_recent_records(self) -> int:
        with self._records_lock:
            count = len(self._records)
            self._records.clear()
            self._persist_records()
            return count

    def _web_task_timestamp(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    def _summarize_web_test_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_images = list(payload.get("images") or [])
        if payload.get("image"):
            raw_images.append(payload.get("image"))
        prompt_enhance_raw = payload.get("prompt_enhance", True)
        prompt_enhance = not (
            prompt_enhance_raw is False
            or str(prompt_enhance_raw).strip().lower() in {"false", "0", "no", "off", "关闭", "否"}
        )
        return {
            "original_prompt": str(payload.get("prompt") or "").strip() or "看着镜头自然自拍",
            "channel": str(payload.get("channel") or "").strip(),
            "model": str(payload.get("model") or "").strip(),
            "aspect_ratio": str(payload.get("aspect_ratio") or self.config.image_default_aspect_ratio or "自动"),
            "resolution": str(payload.get("resolution") or self.config.image_default_resolution or "1K"),
            "prompt_enhance": prompt_enhance,
            "use_selfie_reference": bool(payload.get("use_selfie_reference")),
            "raw_reference_image_count": len(raw_images),
        }

    def _prune_web_tasks_locked(self) -> None:
        if len(self._web_tasks) <= 50:
            return
        finished = [
            (float(task.get("updated_ts") or 0), task_id)
            for task_id, task in self._web_tasks.items()
            if task.get("status") in {"succeeded", "failed"}
        ]
        finished.sort(key=lambda item: item[0])
        while len(self._web_tasks) > 50 and finished:
            _, task_id = finished.pop(0)
            self._web_tasks.pop(task_id, None)

    def _set_web_image_task(self, task_id: str, **fields: Any) -> None:
        with self._web_task_lock:
            task = self._web_tasks.get(task_id)
            if not task:
                return
            now = time.time()
            task.update(fields)
            task["updated_ts"] = now
            task["updated_at"] = self._web_task_timestamp()
            self._prune_web_tasks_locked()

    def get_web_image_task(self, task_id: str) -> Dict[str, Any]:
        with self._web_task_lock:
            task = self._web_tasks.get(str(task_id or "").strip())
            if not task:
                raise ValueError("任务不存在或已清理")
            data = copy.deepcopy(task)
        if data.get("status") in {"queued", "running"}:
            started = float(data.get("started_ts") or data.get("created_ts") or time.time())
            data["running_seconds"] = round(max(0.0, time.time() - started), 2)
        return data

    def start_web_image_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise RuntimeError("请求体必须是 JSON 对象")
        loop = getattr(self, "loop", None)
        if loop is None or not loop.is_running():
            raise RuntimeError("AstrBot 事件循环未就绪，无法启动后台生图任务")
        payload_copy = copy.deepcopy(payload)
        self._validate_web_test_selection(payload_copy)
        with self._web_task_lock:
            self._web_task_seq += 1
            task_id = f"web-{int(time.time() * 1000)}-{self._web_task_seq}"
            now = time.time()
            self._web_tasks[task_id] = {
                "task_id": task_id,
                "status": "queued",
                "success": None,
                "error": "",
                "created_ts": now,
                "updated_ts": now,
                "created_at": self._web_task_timestamp(),
                "updated_at": self._web_task_timestamp(),
                "request_data": self._summarize_web_test_payload(payload_copy),
                "result": None,
            }
            self._prune_web_tasks_locked()
        asyncio.run_coroutine_threadsafe(self._run_web_image_task(task_id, payload_copy), loop)
        return self.get_web_image_task(task_id)

    async def _run_web_image_task(self, task_id: str, payload: Dict[str, Any]) -> None:
        self._set_web_image_task(task_id, status="running", started_ts=time.time(), started_at=self._web_task_timestamp())
        try:
            result = await self.web_test_image(payload)
            success = bool(result.get("success"))
            self._set_web_image_task(
                task_id,
                status="succeeded" if success else "failed",
                success=success,
                error="" if success else str(result.get("error") or "这次没顺好"),
                result=result,
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
        except Exception as exc:
            error = str(exc)
            self._set_web_image_task(
                task_id,
                status="failed",
                success=False,
                error=error,
                result={"success": False, "error": error},
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )

    def _cache_relative_path(self, path: str) -> str:
        try:
            return os.path.relpath(os.path.abspath(path), os.path.abspath(self.generated_dir))
        except Exception:
            return str(path or "")

    def _cache_absolute_path(self, rel_path: str) -> str:
        base = os.path.abspath(self.generated_dir)
        path = os.path.abspath(os.path.join(base, str(rel_path or "")))
        if path != base and not path.startswith(base + os.sep):
            raise ValueError("非法图片路径")
        return path

    def get_cached_image_info(self, rel_path: str) -> Dict[str, Any]:
        abs_path = self._cache_absolute_path(rel_path)
        exists = os.path.exists(abs_path) and os.path.isfile(abs_path)
        mime = "image/png"
        if exists:
            with open(abs_path, "rb") as handle:
                mime = detect_mime_by_bytes(handle.read(16))
        return {
            "path": rel_path,
            "absolute_path": abs_path,
            "exists": exists,
            "mime_type": mime,
        }

    def _save_cache_image(self, data: bytes, prefix: str, mime: str = "") -> str:
        path = save_image_bytes(data, self.generated_dir, prefix=prefix, mime=mime or detect_mime_by_bytes(data))
        return self._cache_relative_path(path)

    def _save_reference_images_to_cache(self, refs: List[ImageReference]) -> List[str]:
        paths: List[str] = []
        for ref in refs:
            if ref.data:
                paths.append(self._save_cache_image(ref.data, "request", ref.mime_type))
        return paths

    def _cache_size_bytes(self) -> int:
        total = 0
        for root, _, files in os.walk(self.generated_dir):
            for name in files:
                path = os.path.join(root, name)
                try:
                    total += os.path.getsize(path)
                except OSError:
                    pass
        return total

    def _cleanup_image_cache_if_needed(self, protected_paths: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        limit = max(10, int(self.config.image_cache_limit_mb or 100)) * 1024 * 1024
        total = self._cache_size_bytes()
        deleted: List[str] = []
        if total <= limit:
            return {"limit_bytes": limit, "total_bytes": total, "deleted": deleted}
        protected = set()
        for item in protected_paths or []:
            try:
                protected.add(self._cache_absolute_path(str(item)))
            except Exception:
                protected.add(os.path.abspath(str(item)))
        candidates: List[Tuple[float, str]] = []
        for root, _, files in os.walk(self.generated_dir):
            for name in files:
                path = os.path.join(root, name)
                if os.path.abspath(path) in protected:
                    continue
                try:
                    candidates.append((os.path.getmtime(path), path))
                except OSError:
                    pass
        candidates.sort(key=lambda item: item[0])
        for _, path in candidates:
            try:
                size = os.path.getsize(path)
                os.remove(path)
                deleted.append(self._cache_relative_path(path))
                total = max(0, total - size)
            except OSError:
                pass
            if total <= limit:
                break
        return {"limit_bytes": limit, "total_bytes": total, "deleted": deleted}

    def _source_context(self, event: Optional[AstrMessageEvent], source: str, user_id: str = "") -> Dict[str, Any]:
        uid = event_user_id(event) if event is not None else str(user_id or "")
        gid = event_group_id(event) if event is not None else ""
        if gid and uid:
            label = f"群 {gid} / QQ {uid}"
        elif uid:
            label = f"QQ {uid}"
        else:
            label = "Web"
        return {
            "source": source,
            "source_label": label,
            "group_id": gid,
            "user_id": uid,
            "chat_type": "group" if gid else ("private" if uid else "web"),
        }

    def _normalize_count(self, count: Any) -> int:
        try:
            value = int(float(str(count).strip()))
        except Exception:
            value = 1
        return max(1, min(self.config.image_max_batch_count, value))

    def _parse_count_token(self, token: str) -> int:
        text = str(token or "").strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        if not text:
            return 0
        match = re.fullmatch(r"(\d{1,2})(?:张|次|幅)?", text)
        if match:
            value = int(match.group(1))
            return value if value > 0 else 0

        chinese_digits = {
            "一": 1,
            "二": 2,
            "两": 2,
            "俩": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
        }
        chinese = re.fullmatch(r"([一二两俩三四五六七八九十]{1,3})(?:张|次|幅)?", text)
        if not chinese:
            return 0
        value_text = chinese.group(1)
        if value_text == "十":
            return 10
        if "十" in value_text:
            before, _, after = value_text.partition("十")
            tens = chinese_digits.get(before, 1) if before else 1
            ones = chinese_digits.get(after, 0) if after else 0
            value = tens * 10 + ones
            return value if value > 0 else 0
        return chinese_digits.get(value_text, 0)

    def _command_tokens_for_count(self, text: str) -> List[str]:
        raw_tokens = re.sub(r"\s+", " ", str(text or "").strip()).split()
        tokens: List[str] = []
        for index, token in enumerate(raw_tokens):
            if index < 2:
                parts = [part.strip() for part in re.split(r"[\/／]+", token) if part.strip()]
                count_like_parts = sum(1 for part in parts if self._parse_count_token(part))
                if 1 < len(parts) <= 2 and count_like_parts == 1:
                    tokens.extend(parts)
                    continue
            tokens.append(token)
        return tokens

    def _extract_command_count(self, text: str) -> Tuple[str, int]:
        tokens = self._command_tokens_for_count(text)
        if not tokens:
            return "", 1
        for index in (0, 1):
            if index >= len(tokens):
                continue
            count = self._parse_count_token(tokens[index])
            if count:
                remaining = [token for pos, token in enumerate(tokens) if pos != index]
                return " ".join(remaining).strip(), self._normalize_count(count)
        return " ".join(tokens).strip(), 1

    def _parse_prompt_options(self, text: str, aspect_ratio: str = "", resolution: str = "") -> Tuple[str, str, str]:
        prompt = str(text or "").strip()
        aspect = str(aspect_ratio or self.config.image_default_aspect_ratio or "自动").strip() or "自动"
        resol = str(resolution or self.config.image_default_resolution or "1K").strip() or "1K"
        matches = list(re.finditer(r"--([a-zA-Z0-9_\-]+)(?:[=\s]+([^\s]+))?", prompt))
        for match in reversed(matches):
            key = match.group(1).lower().replace("-", "_")
            value = str(match.group(2) or "").strip()
            if key in {"ar", "aspect", "aspect_ratio", "ratio"} and value:
                aspect = value
                prompt = prompt[: match.start()] + prompt[match.end() :]
            elif key in {"resolution", "res", "quality"} and value:
                resol = value
                prompt = prompt[: match.start()] + prompt[match.end() :]
            elif key == "size" and value:
                if "2048" in value or value.upper() == "2K":
                    resol = "2K"
                elif "4096" in value or value.upper() == "4K":
                    resol = "4K"
                prompt = prompt[: match.start()] + prompt[match.end() :]
        return re.sub(r"\s+", " ", prompt).strip(), aspect, resol

    def _resolve_image_preset(self, prompt: str, aspect_ratio: str = "", resolution: str = "") -> Tuple[str, str, str, str, str]:
        cleaned_prompt, aspect, resol = self._parse_prompt_options(prompt, aspect_ratio, resolution)
        resolved = self.presets.resolve(cleaned_prompt)
        preset_name = str(resolved.get("preset_name") or "").strip()

        if preset_name:
            cleaned_prompt = str(resolved.get("prompt") or cleaned_prompt).strip()
            default_aspect = str(self.config.image_default_aspect_ratio or "自动").strip() or "自动"
            default_resolution = str(self.config.image_default_resolution or "1K").strip() or "1K"
            preset_aspect = str(resolved.get("aspect_ratio") or "").strip()
            preset_resolution = str(resolved.get("resolution") or "").strip()
            if preset_aspect and aspect == default_aspect:
                aspect = preset_aspect
            if preset_resolution and resol == default_resolution:
                resol = preset_resolution

        return cleaned_prompt, aspect, resol, preset_name, str(resolved.get("description") or "").strip()

    def _normalize_preset_input(self, text: str) -> str:
        return str(text or "").strip().replace("\r", " ").replace("\n", " ")

    def _split_preset_command(self, text: str) -> Tuple[str, str]:
        value = self._normalize_preset_input(text)
        if not value:
            return "", ""
        if " " in value:
            head, tail = value.split(" ", 1)
            return head.strip(), tail.strip()
        return value, ""

    async def _event_reference_images_with_stats(
        self,
        event: AstrMessageEvent,
        include_at_avatar: bool = False,
        context_hint: str = "",
        allow_context_fallback: bool = False,
    ) -> Tuple[List[ImageReference], int, int]:
        sources = self._filter_reference_images(event, extract_image_sources_from_event(event, include_at_avatar=include_at_avatar))
        if not sources and allow_context_fallback and self._looks_like_context_image_reference(context_hint or extract_event_text(event)):
            sources = self._filter_reference_images(event, self._recent_context_image_sources(event))
        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        result: List[ImageReference] = []
        failed_count = 0
        seen = set()
        if not sources:
            return result, 0, 0
        async with aiohttp.ClientSession() as session:
            for source in sources:
                fetched = await fetch_image_source(source, session, max_bytes=max_bytes)
                if not fetched:
                    failed_count += 1
                    continue
                data, mime = fetched
                if not data:
                    failed_count += 1
                    continue
                key = (len(data), data[:64])
                if data and key not in seen:
                    source_url = str(source or "").strip() if str(source or "").strip().lower().startswith(("http://", "https://")) else ""
                    result.append(ImageReference(data=data, mime_type=normalize_image_mime(mime or detect_mime_by_bytes(data)), source_url=source_url))
                    seen.add(key)
        if failed_count and not result:
            logger.warning(f"[SelfieImage] 参考图读取失败或超时: {failed_count}/{len(sources)}")
        return result, len(sources), failed_count

    async def _event_reference_images(
        self,
        event: AstrMessageEvent,
        include_at_avatar: bool = False,
        context_hint: str = "",
        allow_context_fallback: bool = False,
    ) -> List[ImageReference]:
        refs, _, _ = await self._event_reference_images_with_stats(
            event,
            include_at_avatar=include_at_avatar,
            context_hint=context_hint,
            allow_context_fallback=allow_context_fallback,
        )
        return refs

    def _create_image_component(self, file_path: str) -> Any:
        path = os.path.abspath(file_path)
        if hasattr(Image, "fromFileSystem"):
            return Image.fromFileSystem(path)
        if hasattr(Image, "from_file_system"):
            return Image.from_file_system(path)
        return Image(file=path)

    async def _send_generated_images(self, event: AstrMessageEvent, files: Iterable[str]) -> int:
        sent = 0
        for file_path in files:
            await event.send(event.chain_result([self._create_image_component(file_path)]))
            self._record_bot_image_context(event, [file_path])
            sent += 1
            await asyncio.sleep(0.4)
        return sent

    async def _send_progress_text(self, event: AstrMessageEvent, text: str) -> None:
        if not self._progress_text_allowed(event):
            return
        try:
            await event.send(event.plain_result(text))
            self._record_bot_text_context(event, text)
        except Exception as exc:
            logger.warning(f"[SelfieImage] 发送进度消息失败: {exc}")

    def _build_progress_text(self, kind: str, user_request: str, count: int, ack_message: str = "") -> str:
        if kind == "selfie":
            return self._selfie_ack_text(user_request, count, ack_message)
        return self._image_ack_text(user_request, count, ack_message)

    async def _call_text_llm(self, event: Optional[AstrMessageEvent], prompt: str, timeout: int = 8) -> str:
        if event is None:
            return ""

        async def request() -> str:
            origin = getattr(event, "unified_msg_origin", None)
            provider_id = None
            try:
                getter = getattr(self.context, "get_using_provider", None)
                if callable(getter):
                    provider = getter()
                    requester = getattr(provider, "text_chat", None) or getattr(provider, "request", None)
                    if callable(requester):
                        response = requester(prompt=prompt)
                        if asyncio.iscoroutine(response):
                            response = await response
                        return str(getattr(response, "completion_text", response) or "").strip()
            except Exception:
                pass
            try:
                getter = getattr(self.context, "get_current_chat_provider_id", None)
                if callable(getter):
                    provider_id = await getter(umo=origin) if origin else await getter()
            except Exception:
                provider_id = None
            try:
                generator = getattr(self.context, "llm_generate", None)
                if callable(generator):
                    kwargs = {"prompt": prompt}
                    if provider_id:
                        kwargs["chat_provider_id"] = provider_id
                    response = await generator(**kwargs)
                    return str(getattr(response, "completion_text", response) or "").strip()
            except Exception:
                return ""
            return ""

        try:
            return await asyncio.wait_for(request(), timeout=max(2, int(timeout or 8)))
        except Exception:
            return ""

    def _strip_llm_short_reply(self, text: str) -> str:
        value = str(text or "").strip()
        if not value:
            return ""
        fenced = re.match(r"^```(?:\w+)?\s*([\s\S]*?)\s*```$", value)
        if fenced:
            value = fenced.group(1).strip()
        value = re.sub(r"<\s*(?:think|analysis)\b[^>]*>.*?<\s*/\s*(?:think|analysis)\s*>", "", value, flags=re.I | re.S)
        value = value.replace("\r", " ").replace("\n", " ")
        value = re.sub(r"^\s*(?:回复|答复|assistant|bot)\s*[：:]\s*", "", value, flags=re.I)
        value = value.strip(" 「」『』“”\"'`")
        return re.sub(r"\s+", " ", value).strip()

    def _build_ack_prompt_for_llm(self, event: AstrMessageEvent, kind: str, user_request: str, count: int) -> str:
        name = self._bot_display_name()
        context_text = self._format_context_for_llm(event, count=12, max_chars=1400)
        request = str(user_request or "").strip()
        kind_text = "自拍/拍照" if kind == "selfie" else "图片请求"
        count_text = "多张" if count > 1 else "一张"
        personality = str(self.config.personality or "").strip()
        return "\n".join(
            [
                f"你是{name}，正在和用户自然聊天。",
                f"用户刚通过指令发起了{kind_text}，数量：{count_text}。",
                "请只输出一句简体中文短回复，像正在聊天时随口接一句。",
                "要求：10-32 个汉字；结合最近上下文和人设；不要复述用户提示词；不要解释；不要列点。",
                "禁止出现：生成、绘制、渲染、工具、提示词、配置、审核、任务、处理中、已收到、开始、为你。",
                "如果是自拍/拍照，可以表现为找角度、看光线、调整镜头；如果是普通图片，可以表现为整理画面或构图。",
                f"人设补充：{personality[:300]}" if personality else "",
                f"最近对话：\n{context_text}" if context_text else "最近对话：无",
                f"当前请求：{request[:300]}",
                "只输出这一句回复：",
            ]
        )

    async def _build_contextual_progress_text(
        self,
        event: AstrMessageEvent,
        kind: str,
        user_request: str,
        count: int,
        ack_message: str = "",
    ) -> str:
        fallback = self._build_progress_text(kind, user_request, count, ack_message)
        if ack_message:
            return fallback
        prompt = self._build_ack_prompt_for_llm(event, kind, user_request, count)
        text = self._strip_llm_short_reply(await self._call_text_llm(event, prompt, timeout=7))
        custom = self._clean_ack_message(text, user_request)
        return custom or fallback

    def _record_bot_text_context(self, event: Optional[AstrMessageEvent], text: str) -> None:
        if not event or not str(text or "").strip():
            return
        self._add_context_message(
            session_key=self._context_session_key(event),
            sender_id="bot",
            sender_name=self._bot_display_name(),
            content=text,
            is_bot=True,
            msg_id=f"bot:{time.time_ns()}",
        )

    def _record_bot_image_context(self, event: Optional[AstrMessageEvent], files: Iterable[str]) -> None:
        if not event:
            return
        for file_path in files:
            if not str(file_path or "").strip():
                continue
            self._add_context_message(
                session_key=self._context_session_key(event),
                sender_id="bot",
                sender_name=self._bot_display_name(),
                content="[图片]",
                is_bot=True,
                image_sources=[os.path.abspath(str(file_path))],
                msg_id=f"bot-image:{time.time_ns()}",
            )

    def _is_admin_event(self, event: AstrMessageEvent) -> bool:
        role = str(getattr(event, "role", "") or "").lower().strip()
        if role in {"admin", "owner"}:
            return True
        sender = getattr(event, "sender", None)
        sender_role = str(getattr(sender, "role", "") or "").lower().strip()
        return sender_role in {"admin", "owner"}

    def _preset_list_text(self, page: int = 1, page_size: int = 20) -> Tuple[str, int, int]:
        presets = self.presets.list()
        total = len(presets)
        total_pages = max(1, (total + page_size - 1) // page_size)
        current_page = min(total_pages, max(1, page))
        start = (current_page - 1) * page_size
        items = presets[start:start + page_size]
        prefix = "/"
        lines = [
            f"📋 生图预设 第 {current_page}/{total_pages} 页",
            f"当前共有 {total} 个预设。",
            "",
            "使用方式：",
            f"1. {prefix}画 预设名 额外提示词",
            f"2. {prefix}自拍 预设名 额外提示词",
            f"3. {prefix}预设 添加 预设名:提示词（管理员）",
            f"4. {prefix}预设 删除 预设名（管理员）",
            f"5. {prefix}预设 查看 [页码/预设名]（管理员）",
            "",
        ]
        if total_pages > 1:
            if current_page < total_pages:
                lines.append(f"下一页：{prefix}预设 {current_page + 1}")
            if current_page > 1:
                lines.append(f"上一页：{prefix}预设 {current_page - 1}")
            lines.append("")

        if not items:
            lines.append("暂无预设。")
        else:
            lines.append("预设名：")
            for idx, (name, _) in enumerate(items, start=start + 1):
                lines.append(f"{idx}. {name}")

        return "\n".join(line for line in lines if line is not None), current_page, total_pages

    def _preset_detail_lines(self, idx: Optional[int], name: str, preset: Any) -> List[str]:
        desc = preset.description or preset.prompt
        extra = preset.extra_prompt
        params = []
        if preset.aspect_ratio:
            params.append(f"比例: {preset.aspect_ratio}")
        if preset.resolution:
            params.append(f"分辨率: {preset.resolution}")
        title = f"{idx}. {name}" if idx is not None else str(name)
        return [
            title,
            f"提示词: {preset.prompt}",
            *( [f"额外提示词: {extra}"] if extra else [] ),
            *( [f"说明: {desc}"] if desc and desc != preset.prompt else [] ),
            *( [f"参数: {' | '.join(params)}"] if params else [] ),
            "",
        ]

    def _preset_detail_text(self, page: int = 1, page_size: int = 20) -> Tuple[str, int, int]:
        presets = self.presets.list()
        total = len(presets)
        total_pages = max(1, (total + page_size - 1) // page_size)
        current_page = min(total_pages, max(1, page))
        start = (current_page - 1) * page_size
        items = presets[start:start + page_size]
        prefix = "/"
        lines = [
            f"📋 生图预设详情 第 {current_page}/{total_pages} 页",
            f"当前共有 {total} 个预设。",
            "仅管理员可见。",
            "",
        ]
        if total_pages > 1:
            if current_page < total_pages:
                lines.append(f"下一页：{prefix}预设 查看 {current_page + 1}")
            if current_page > 1:
                lines.append(f"上一页：{prefix}预设 查看 {current_page - 1}")
            lines.append("")

        if not items:
            lines.append("暂无预设。")
        else:
            for idx, (name, preset) in enumerate(items, start=start + 1):
                lines.extend(self._preset_detail_lines(idx, name, preset))

        return "\n".join(line for line in lines if line is not None), current_page, total_pages

    def _preset_single_detail_text(self, name: str) -> Tuple[bool, str]:
        target = str(name or "").strip()
        if not target:
            return False, "格式：/预设 查看 预设名"
        for preset_name, preset in self.presets.list():
            if preset_name == target:
                return True, "\n".join(
                    [
                        "📋 生图预设详情",
                        "仅管理员可见。",
                        "",
                        *self._preset_detail_lines(None, preset_name, preset),
                    ]
                ).strip()
        return False, f"预设不存在: {target}"

    def _handle_preset_mutation(self, event: AstrMessageEvent, action: str, payload: str) -> Tuple[bool, str]:
        if not self._is_admin_event(event):
            return False, "仅管理员可以管理预设。"
        if action == "add":
            if ":" in payload:
                name, value = payload.split(":", 1)
            elif "：" in payload:
                name, value = payload.split("：", 1)
            else:
                return False, "格式：预设 添加 名称:提示词"
            return self.presets.add(name, value)
        if action == "delete":
            return self.presets.remove(payload)
        return False, "未知操作"

    def _friendly_user_error_message(self, error: str, fallback: str = "") -> str:
        text = str(error or "").strip()
        if not text:
            return fallback or "我刚刚没抓稳这个感觉，换个方向再试试好不好。"
        if "可使用人员白名单" in text:
            return "这个功能现在还只对少数人开放。"
        if "用户黑名单" in text:
            return "现在先不方便给你看这类内容。"
        if "今日生图次数已用完" in text:
            return "我今天已经拍得有点多了，明天再来找我好不好。"
        if "请求太频繁" in text:
            return "先让我缓一小会儿，我等下就继续给你看。"
        if "提示词审核未通过" in text or "图片内容审核未通过" in text or "提示词包含禁用词" in text:
            return "这次这个方向我不太方便照着来，换个说法或者换个感觉试试。"
        if "当前没有可用的生图模型" in text or "未配置可用出图审核模型" in text or "未启用" in text:
            return random.choice(["这会儿接口没接上，晚点再试。", "现在暂时出不了图，等配置恢复再来。"])
        if "缺少生图提示词" in text or "请输入提示词" in text:
            return "你想让我往什么感觉走？也可以直接丢张参考图给我。"
        return fallback or self._natural_fail_fallback("image")

    def _tool_soft_fail(self, error: str, fallback: str = "") -> str:
        message = self._friendly_user_error_message(error, fallback)
        return f"[TOOL_FAILED] {message}\n请用简体中文、用你自己的语气自然安抚用户，别提生成、工具、配置、次数、冷却或审核这些词。"

    def _tool_unavailable(self, fallback: str) -> str:
        return f"[TOOL_UNAVAILABLE] {fallback}\n请用简体中文、用你自己的语气自然安抚用户，别提功能、工具或配置。"

    def _tool_success(self, kind: str = "image", count: int = 1) -> str:
        label = "照片" if kind == "selfie" else "图片"
        count_text = f"，共 {count} 张" if count > 1 else ""
        return (
            f"[TOOL_SUCCESS] {label}已经发给用户{count_text}。\n"
            "请用简体中文、按当前人格自然收尾一句，也可以很短。"
            "不要复述请求，不要说生成、绘制、工具、调用、任务、已完成、已发送、配置、模型、提示词或审核。"
        )

    def _build_leg_focus_action(self, extra_request: str = "", has_refs: bool = False) -> str:
        variants = [
            (
                "第一人称自拍视角，俯拍半身下半身特写，发色和发型严格沿用 AI 形象参考图，只露出部分发丝（若参考图是淡紫色长卷发则保持淡紫色长卷发），宽松白色翻领衬衫，"
                "黑色短百褶裙，珠光白过膝长筒丝袜，脚踝堆堆白棉袜，黑色厚底乐福小皮鞋，坐在米白色毛绒地毯上，"
                "窗边百叶窗洒落条状柔和阳光，暖调自然光，日系居家氛围感，胶片质感，细腻皮肤纹理，高清写实，8K，"
                "浅景深，低饱和奶油色调，生活化私房穿搭，手部轻扯裙边细节。"
            ),
            (
                "第一人称低头随手拍，坐在床沿，双腿向前自然伸展后轻微斜放，一只手整理袜口，"
                "柔软针织上衣和短裙边缘进入画面，过膝袜贴合腿部但不过度紧绷，脚踝线条干净，室内暖光和浅色床单背景。"
            ),
            (
                "窗边单人椅坐姿自拍，双腿并拢后向一侧自然倾斜，膝盖和脚尖方向协调，"
                "裙摆自然垂落，长筒袜和小皮鞋材质清楚，地板有柔和反光，画面像日常穿搭记录。"
            ),
            (
                "米白色地毯上的居家坐姿，下半身近景，双腿轻微交叠但不扭曲，手指轻扶膝盖或裙边，"
                "袜口、鞋面、衣料褶皱和地毯绒毛清晰，暖调自然光，日系生活感，干净柔和。"
            ),
        ]
        base = (
            "看看腿。"
            "主角身份必须来自 AI 自拍形象参考图：即使脸部不入镜，露出的发丝、发色、体态、肤色、手部和整体气质也要像同一个角色。"
            "不要换成陌生人物，不要改变主角发色和体态。"
            f"本次腿部特写构图：{random.choice(variants)}"
            "脚背、脚踝和腿部皮肤保持干净自然；不要明显青筋、血管、突兀肌腱、脏污脚面、皱皮或夸张骨节。"
            "腿部比例自然，膝盖、小腿、脚踝、鞋袜和手部互动都要协调，姿势不要僵硬或重复。"
        )
        if has_refs:
            base += " 用户提供的图片只参考氛围、构图、服装或姿势；主角身份仍以 AI 自拍形象参考图为准。"
        extra = re.sub(r"\s+", " ", str(extra_request or "")).strip(" 。")
        if extra:
            base = base.rstrip("。") + f"。用户补充要求优先：{extra}。"
        return base

    def _build_third_person_look_action(self, extra_request: str = "", has_refs: bool = False) -> str:
        base = (
            "【他拍 / 看看你模式】展示 AI 当前样子的自然日常照片。"
            "像朋友在画面外用相机或手机随手拍下 AI，镜头来自旁边的拍摄者，带一点生活抓拍感。"
            "主角可看向镜头、轻松回头、坐着发呆、整理东西或自然做自己的事。"
            "保持 AI 当前形象、今日穿搭和生活状态一致，脸部、穿搭、姿态、背景层次和光线都清晰自然。"
        )
        if has_refs:
            base = "参考用户提供的图片氛围、场景或构图，" + base
        extra = re.sub(r"\s+", " ", str(extra_request or "")).strip(" 。")
        if extra:
            base += f" 额外要求：{extra}。"
        return base

    def _build_group_selfie_action(self, extra_request: str = "", has_refs: bool = False) -> str:
        base = (
            "合影 / 合照 / 同框。AI 自己必须作为画面主角之一，与用户指定或提供的对象自然同框合影，保持 AI 当前形象一致。"
            "如果同一张参考图里有多个可见人物 / 角色，按实际可见人数全部保留为独立同框对象。"
            "动漫、卡通、表情包或非真人对象默认拟人化 / 真人化成自然同框的人类角色，并保留主要识别特征。"
            "所有同框对象处在同一场景中，站位或坐位自然，视线、距离、遮挡、互动、光线、色调和相机透视统一。"
            "整体像同一时间、同一地点真实拍下的一张自然合照。"
        )
        if has_refs:
            base += " 用户提供或艾特对象的头像/图片是合影对象参考。"
        else:
            base += " 没有合影对象参考图时，按文字要求生成自然同框对象。"
        extra = re.sub(r"\s+", " ", str(extra_request or "")).strip(" 。")
        if extra:
            base += f" 用户补充要求：{extra}。"
        return base

    def _looks_like_group_selfie_intent(self, text: str) -> bool:
        value = str(text or "")
        compact = re.sub(r"[\s，。！？、；：,.!?]", "", value.lower())
        compact_keywords = [
            "合影",
            "合照",
            "同框",
            "一起拍",
            "一起照",
            "和我",
            "跟我",
            "与我",
            "陪我",
            "和你",
            "跟你",
            "与你",
            "你和我",
            "我和你",
            "我们一起",
            "groupselfie",
            "groupphoto",
            "phototogether",
            "takeaphototogether",
            "takeapicturetogether",
            "sameframe",
            "inthesameframe",
            "sidebyside",
            "standingnextto",
            "twous",
            "ustogether",
        ]
        if any(keyword in compact for keyword in compact_keywords):
            return True
        low = value.lower()
        phrase_keywords = [
            "group selfie",
            "group photo",
            "photo together",
            "take a photo together",
            "take a picture together",
            "same frame",
            "in the same frame",
            "side by side",
            "standing next to",
            "two of us",
            "us together",
            "with me",
            "with you",
        ]
        for keyword in phrase_keywords:
            pattern = r"(?<![a-z0-9])" + re.escape(keyword).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
            if re.search(pattern, low):
                return True
        return False

    def _looks_like_selfie_intent(self, text: str) -> bool:
        value = str(text or "")
        low = value.lower()
        bot_name = str(self.config.bot_name or "").strip()
        keywords = [
            "自拍",
            "合影",
            "合照",
            "同框",
            "形象照",
            "和我",
            "跟我",
            "与我",
            "陪我",
            "和你",
            "跟你",
            "与你",
            "你和我",
            "我和你",
            "我们一起",
            "一起拍",
            "一起照",
            "你自己",
            "你的照片",
        ]
        english_keywords = [
            "selfie",
            "group selfie",
            "group photo",
            "photo together",
            "take a photo together",
            "take a picture together",
            "together with me",
            "with me",
            "with you",
            "next to me",
            "next to you",
            "standing next to",
            "side by side",
            "same frame",
            "in the same frame",
            "two of us",
            "us together",
            "your photo",
            "yourself",
            "ai assistant",
            "catgirl",
            "ahwu",
        ]
        if bot_name:
            keywords.append(bot_name)
            english_keywords.append(bot_name.lower())
        return any(keyword and keyword in value for keyword in keywords) or any(keyword and keyword in low for keyword in english_keywords)

    async def _run_llm_selfie_flow(
        self,
        event: AstrMessageEvent,
        action: str,
        requested_count: int,
        aspect: str,
        resolution: str,
        ack_message: str = "",
    ) -> Optional[str]:
        if not self.config.image_enable_llm_tool:
            return self._tool_unavailable("我这会儿还没法给你拍这种。")
        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            return self._tool_soft_fail(error)

        action = str(action or "").strip() or "看着镜头自然自拍"
        await self._send_progress_text(
            event,
            await self._build_contextual_progress_text(event, "selfie", action, requested_count, ack_message),
        )
        extra_refs = await self._event_reference_images(
            event,
            include_at_avatar=self._looks_like_group_selfie_intent(action),
            context_hint=action,
            allow_context_fallback=True,
        )
        total_sent = 0
        for _ in range(requested_count):
            prompt, refs = await self._build_selfie_prompt_and_refs(action, extra_refs)
            result = await self._run_image_generation(prompt, aspect, resolution, refs, source="llm-generate-selfie", audit_user_id=event_user_id(event), event=event, original_prompt=action)
            if not result.get("success"):
                return self._tool_soft_fail(str(result.get("error") or ""), self._natural_fail_fallback("selfie"))
            sent = await self._send_generated_images(event, result.get("files", []))
            total_sent += sent
            if sent:
                self._record_generated_images(event, 1)
        return self._tool_success("selfie", total_sent or requested_count)

    def _build_success_text(self, elapsed_seconds: float, count: int, used_model: str, event: AstrMessageEvent) -> str:
        lines: List[str] = []
        if self.config.image_show_generation_info:
            lines.append(f"生成成功，耗时 {elapsed_seconds:.2f}s，数量 {count} 张。")
            if self.config.image_enable_daily_limit:
                status = self._access_status(event)
                if status.get("unlimited"):
                    lines.append("今日用量：白名单用户/群组不限制。")
                else:
                    user_id = status.get("user_id") or ""
                    used = int(self._current_usage_stats().get("users", {}).get(user_id, {}).get("count", 0))
                    lines.append(f"今日用量：{used}/{self.config.image_daily_limit_count}。")
        if self.config.image_show_model_info and used_model:
            lines.append(f"模型：{used_model}")
        return "\n".join(lines)

    def _batch_success_text(self, info: str, index: int, total: int) -> str:
        text = str(info or "").strip()
        if not text:
            return ""
        if total > 1:
            return f"第 {index}/{total} 次请求完成。\n{text}"
        return text

    async def _run_image_generation(
        self,
        prompt: str,
        aspect_ratio: str,
        resolution: str,
        refs: List[ImageReference],
        targets: Optional[List[ImageModelTarget]] = None,
        source: str = "command",
        audit_user_id: str = "",
        event: Optional[AstrMessageEvent] = None,
        original_prompt: str = "",
        max_attempts: Optional[int] = None,
        allow_compat_retry: bool = True,
    ) -> Dict[str, Any]:
        selected_targets = targets or self.config.get_prioritized_targets()
        request_prompt = str(prompt or "")
        original_prompt = str(original_prompt or request_prompt)
        audit_prompt_text = original_prompt or request_prompt
        source_meta = self._source_context(event, source, audit_user_id)
        request_image_paths = self._save_reference_images_to_cache(refs)
        request_data = {
            "original_prompt": original_prompt,
            "request_prompt": request_prompt,
            "audit_prompt": audit_prompt_text,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "reference_image_count": len(refs),
            "request_image_paths": request_image_paths,
            "targets": [target.label for target in selected_targets],
        }
        request_cleanup = self._cleanup_image_cache_if_needed(request_image_paths)
        if request_cleanup.get("deleted"):
            request_data["cache_cleanup_before_generation"] = request_cleanup

        audit_ok, audit_reason = await self._audit_prompt(audit_prompt_text, audit_user_id, event)
        if not audit_ok:
            response_data = {"success": False, "stage": "prompt_audit", "error": f"提示词审核未通过：{audit_reason}"}
            self._record_task(
                {
                    **source_meta,
                    "success": False,
                    "error": response_data["error"],
                    "prompt": request_prompt,
                    "original_prompt": original_prompt,
                    "request_prompt": request_prompt,
                    "used_model": "",
                    "elapsed_seconds": 0,
                    "reference_images": len(refs),
                    "request_data": request_data,
                    "response_data": response_data,
                    "request_image_paths": request_image_paths,
                    "generated_image_paths": [],
                }
            )
            return {"success": False, "error": f"提示词审核未通过：{audit_reason}"}

        if not selected_targets:
            response_data = {"success": False, "stage": "select_model", "error": "当前没有可用的生图模型，请先配置 image_channels。"}
            self._record_task(
                {
                    **source_meta,
                    "success": False,
                    "error": response_data["error"],
                    "prompt": request_prompt,
                    "original_prompt": original_prompt,
                    "request_prompt": request_prompt,
                    "used_model": "",
                    "elapsed_seconds": 0,
                    "reference_images": len(refs),
                    "request_data": request_data,
                    "response_data": response_data,
                    "request_image_paths": request_image_paths,
                    "generated_image_paths": [],
                }
            )
            return {"success": False, "error": "当前没有可用的生图模型，请先配置 image_channels。"}

        request = ImageGenerateRequest(
            prompt=request_prompt,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            images=refs,
            allow_compat_retry=allow_compat_retry,
            max_image_bytes=self.config.image_max_image_size_mb * 1024 * 1024,
        )
        started = time.monotonic()
        async with self._semaphore:
            async with aiohttp.ClientSession() as session:
                result = await generate_image_with_fallback(selected_targets, request, session, max_attempts=max_attempts)
        elapsed = time.monotonic() - started

        if result.error or not result.images:
            response_data = {
                "success": False,
                "stage": "generate",
                "error": result.error or "未生成任何图片",
                "used_model": result.used_model,
                "elapsed_seconds": round(elapsed, 2),
                "attempts": result.attempts,
            }
            self._record_task(
                {
                    **source_meta,
                    "success": False,
                    "error": response_data["error"],
                    "prompt": request_prompt,
                    "original_prompt": original_prompt,
                    "request_prompt": request_prompt,
                    "used_model": result.used_model,
                    "elapsed_seconds": round(elapsed, 2),
                    "reference_images": len(refs),
                    "request_data": request_data,
                    "response_data": response_data,
                    "request_image_paths": request_image_paths,
                    "generated_image_paths": [],
                }
            )
            return {
                "success": False,
                "error": result.error or "未生成任何图片",
                "elapsed_seconds": elapsed,
                "used_model": result.used_model,
                "request_data": request_data,
                "response_data": response_data,
                "request_image_paths": request_image_paths,
                "attempts": result.attempts,
            }

        generated_image_paths = [self._save_cache_image(image, "generated", detect_mime_by_bytes(image)) for image in result.images if image]
        files = [self._cache_absolute_path(path) for path in generated_image_paths]
        output_ok, output_reason = await self._audit_output_images(files, audit_user_id, prompt, event=event)
        if not output_ok:
            response_data = {
                "success": False,
                "stage": "output_audit",
                "error": f"图片内容审核未通过：{output_reason}",
                "used_model": result.used_model,
                "elapsed_seconds": round(elapsed, 2),
                "generated_image_paths": generated_image_paths,
                "blocked_images_retained": True,
                "attempts": result.attempts,
            }
            self._record_task(
                {
                    **source_meta,
                    "success": False,
                    "error": response_data["error"],
                    "prompt": request_prompt,
                    "original_prompt": original_prompt,
                    "request_prompt": request_prompt,
                    "used_model": result.used_model,
                    "elapsed_seconds": round(elapsed, 2),
                    "reference_images": len(refs),
                    "request_data": request_data,
                    "response_data": response_data,
                    "request_image_paths": request_image_paths,
                    "generated_image_paths": generated_image_paths,
                }
            )
            return {
                "success": False,
                "error": f"图片内容审核未通过：{output_reason}",
                "elapsed_seconds": elapsed,
                "used_model": result.used_model,
                "image_paths": generated_image_paths,
                "attempts": result.attempts,
            }

        cleanup = self._cleanup_image_cache_if_needed([*request_image_paths, *generated_image_paths])
        response_data = {
            "success": True,
            "used_model": result.used_model,
            "elapsed_seconds": round(elapsed, 2),
            "count": len(files),
            "generated_image_paths": generated_image_paths,
            "cache_cleanup": cleanup,
            "attempts": result.attempts,
        }
        self._record_task(
            {
                **source_meta,
                "success": True,
                "prompt": request_prompt,
                "original_prompt": original_prompt,
                "request_prompt": request_prompt,
                "used_model": result.used_model,
                "elapsed_seconds": round(elapsed, 2),
                "reference_images": len(refs),
                "count": len(files),
                "request_data": request_data,
                "response_data": response_data,
                "request_image_paths": request_image_paths,
                "generated_image_paths": generated_image_paths,
            }
        )
        return {
            "success": True,
            "files": files,
            "image_paths": generated_image_paths,
            "elapsed_seconds": elapsed,
            "used_model": result.used_model,
            "reference_images": len(refs),
            "request_data": request_data,
            "response_data": response_data,
            "request_image_paths": request_image_paths,
            "attempts": result.attempts,
        }

    async def _build_selfie_prompt_and_refs(self, action: str, extra_refs: List[ImageReference]) -> Tuple[str, List[ImageReference]]:
        await self.persona.ensure_daily_selfie_profile(action)
        persona_ref = self.persona.get_reference_image()
        refs: List[ImageReference] = []
        if persona_ref:
            refs.append(ImageReference(data=persona_ref["data"], mime_type=persona_ref["mime_type"]))
        refs.extend(extra_refs)
        prompt = self.persona.build_selfie_prompt(
            action=action or "看着镜头自然自拍，展示你现在的样子",
            bot_name=self.config.bot_name,
            personality=self.config.personality,
            has_reference_image=bool(persona_ref),
            extra_reference_count=len(extra_refs),
        )
        return prompt, refs

    def get_selfie_reference_payload(self) -> Dict[str, Any]:
        data = self.persona.get()
        ref = self.persona.get_reference_image()
        if not ref:
            return {
                "has_image": False,
                "ref_mime_type": data.get("ref_mime_type") or "image/png",
                "updated_at": data.get("updated_at") or "",
                "status": self.persona.status_text(),
            }
        return {
            "has_image": True,
            "ref_mime_type": ref["mime_type"],
            "updated_at": data.get("updated_at") or "",
            "image": bytes_to_data_url(ref["data"], ref["mime_type"]),
            "status": self.persona.status_text(),
        }

    def save_selfie_reference_from_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_image = str(payload.get("image") or payload.get("data") or "").strip()
        if not raw_image:
            raise ValueError("缺少 image 字段，支持 data:image/...;base64,... 或纯 base64")
        data, mime = data_url_to_bytes(raw_image)
        if not data:
            raise ValueError("上传图片为空")
        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        if len(data) > max_bytes:
            raise ValueError(f"图片过大，最大允许 {self.config.image_max_image_size_mb}MB")
        self.persona.save_reference_image(data, normalize_image_mime(str(payload.get("mime_type") or mime or detect_mime_by_bytes(data))))
        return self.get_selfie_reference_payload()

    def clear_selfie_reference_from_web(self) -> Dict[str, Any]:
        self.persona.clear_reference_image()
        return self.get_selfie_reference_payload()

    async def refresh_selfie_profile_from_web(self) -> Dict[str, Any]:
        self.persona.refresh_daily_selfie_profile_for_test()
        await self.persona.ensure_daily_selfie_profile("手动刷新今日自拍设定")
        return {
            "status": self.persona.status_text(),
            "updated_at": self.persona.get().get("updated_at") or "",
        }

    def _find_image_target(self, channel_name: str = "", model: str = "") -> Optional[ImageModelTarget]:
        targets = self.config.get_prioritized_targets()
        if not channel_name and not model:
            return targets[0] if targets else None
        for target in targets:
            if channel_name and target.channel_name != channel_name:
                continue
            if model and target.model != model:
                continue
            return target
        for target in targets:
            if channel_name and target.channel_name == channel_name and not model:
                return target
        return None

    def _validate_web_test_selection(self, payload: Dict[str, Any]) -> None:
        channel_name = str(payload.get("channel") or "").strip()
        if not channel_name:
            return
        matching_channels = [channel for channel in self.config.image_channels if channel.name == channel_name]
        if not matching_channels:
            raise RuntimeError(f"生图渠道 {channel_name} 不存在")
        if not any(channel.enabled for channel in matching_channels):
            raise RuntimeError(f"生图渠道 {channel_name} 已禁用，渠道测试不会调用禁用渠道")

    async def web_test_image(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        channel_name = str(payload.get("channel") or "").strip()
        model_name = str(payload.get("model") or "").strip()
        raw_images = list(payload.get("images") or [])
        if payload.get("image"):
            raw_images.append(payload.get("image"))

        original_prompt = str(payload.get("prompt") or "").strip() or "看着镜头自然自拍"
        aspect = str(payload.get("aspect_ratio") or self.config.image_default_aspect_ratio or "自动")
        resolution = str(payload.get("resolution") or self.config.image_default_resolution or "1K")
        prompt_enhance_raw = payload.get("prompt_enhance", True)
        prompt_enhance = not (
            prompt_enhance_raw is False
            or str(prompt_enhance_raw).strip().lower() in {"false", "0", "no", "off", "关闭", "否"}
        )
        request_summary = {
            "original_prompt": original_prompt,
            "channel": channel_name,
            "model": model_name,
            "aspect_ratio": aspect,
            "resolution": resolution,
            "prompt_enhance": prompt_enhance,
            "use_selfie_reference": bool(payload.get("use_selfie_reference")),
            "raw_reference_image_count": len(raw_images),
        }

        try:
            self._validate_web_test_selection(payload)
            target = self._find_image_target(channel_name, model_name)
            if not target:
                raise RuntimeError("未找到指定生图模型")

            max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
            refs: List[ImageReference] = []
            extra_refs: List[ImageReference] = []
            for raw in raw_images:
                data, mime = data_url_to_bytes(str(raw or ""))
                if not data:
                    continue
                if len(data) > max_bytes:
                    raise RuntimeError(f"参考图过大，最大允许 {self.config.image_max_image_size_mb}MB")
                extra_refs.append(ImageReference(data=data, mime_type=normalize_image_mime(mime or detect_mime_by_bytes(data))))

            if not prompt_enhance:
                refs = list(extra_refs)
                if payload.get("use_selfie_reference"):
                    persona_ref = self.persona.get_reference_image()
                    if not persona_ref:
                        raise RuntimeError("当前未设置 AI 自拍形象参考图，请先上传形象图，或取消使用自拍形象参考图")
                    refs.insert(0, ImageReference(data=persona_ref["data"], mime_type=persona_ref["mime_type"]))
                prompt = original_prompt
            elif payload.get("use_selfie_reference"):
                prompt, refs = await self._build_selfie_prompt_and_refs(original_prompt, extra_refs)
                if not refs:
                    raise RuntimeError("当前未设置 AI 自拍形象参考图，请先上传形象图，或取消使用自拍形象参考图")
            else:
                refs = extra_refs
                prompt = build_prompt_with_reference_instruction(original_prompt, refs)

            result = await self._run_image_generation(
                prompt=prompt,
                aspect_ratio=aspect,
                resolution=resolution,
                refs=refs,
                targets=[target],
                source="web-test",
                original_prompt=original_prompt,
                event=None,
                max_attempts=1,
                allow_compat_retry=False,
            )
        except Exception as exc:
            error = str(exc)
            response_data = {"success": False, "stage": "web_test_preflight", "error": error}
            self._record_task(
                {
                    **self._source_context(None, "web-test"),
                    "success": False,
                    "error": error,
                    "prompt": original_prompt,
                    "original_prompt": original_prompt,
                    "request_prompt": original_prompt,
                    "used_model": model_name,
                    "elapsed_seconds": 0,
                    "reference_images": len(raw_images),
                    "request_data": request_summary,
                    "response_data": response_data,
                    "request_image_paths": [],
                    "generated_image_paths": [],
                }
            )
            raise

        if not result.get("success"):
            return {
                "success": False,
                "error": str(result.get("error") or "这次没顺好"),
                "used_model": result.get("used_model"),
                "elapsed_seconds": round(float(result.get("elapsed_seconds") or 0), 2),
                "reference_images": len(refs),
                "original_prompt": original_prompt,
                "final_prompt": prompt,
                "request_data": result.get("request_data") or request_summary,
                "response_data": result.get("response_data") or {},
                "request_image_paths": result.get("request_image_paths") or [],
                "generated_image_paths": result.get("image_paths") or [],
            }

        return {
            "success": True,
            "used_model": result.get("used_model"),
            "elapsed_seconds": round(float(result.get("elapsed_seconds") or 0), 2),
            "reference_images": len(refs),
            "original_prompt": original_prompt,
            "final_prompt": prompt,
            "request_data": result.get("request_data") or {},
            "response_data": result.get("response_data") or {},
            "request_image_paths": result.get("request_image_paths") or [],
            "generated_image_paths": result.get("image_paths") or [],
        }

    async def web_refresh_image_models(self, payload: Dict[str, Any]) -> List[str]:
        channel_payload = payload.get("channel") if isinstance(payload.get("channel"), dict) else payload
        base = normalize_image_base_url(str(channel_payload.get("base_url") or channel_payload.get("baseUrl") or "").strip())
        api_key = str(channel_payload.get("api_key") or channel_payload.get("apiKey") or "").strip()
        provider_type = str(channel_payload.get("provider_type") or "openai")
        proxy = str(channel_payload.get("proxy") or "").strip() or None
        if provider_type == "agnes":
            return ["agnes-image-2.1-flash"]
        if not base:
            raise RuntimeError("base_url 为空")
        headers = {"Accept": "application/json"}
        if provider_type == "gemini" and api_key:
            headers["x-goog-api-key"] = api_key
        elif api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        candidates = [f"{base}/v1/models", f"{base}/models", f"{base}/v1beta/models"]
        errors: List[str] = []
        async with aiohttp.ClientSession() as session:
            for url in candidates:
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12), proxy=proxy) as response:
                        if response.status >= 400:
                            errors.append(f"{url}: HTTP {response.status} {(await response.text())[:200]}")
                            continue
                        data = await response.json(content_type=None)
                    models = self._extract_model_ids(data)
                    if models:
                        return models
                    errors.append(f"{url}: 返回成功但未识别到模型")
                except Exception as exc:
                    errors.append(f"{url}: {exc}")
        raise RuntimeError("\n".join(errors))

    def _extract_model_ids(self, data: Any) -> List[str]:
        result = set()

        def walk(value: Any) -> None:
            if value is None:
                return
            if isinstance(value, str):
                text = value.strip()
                if text:
                    result.add(text)
                return
            if isinstance(value, list):
                for item in value:
                    walk(item)
                return
            if not isinstance(value, dict):
                return
            if isinstance(value.get("id"), str):
                result.add(value["id"].strip())
            elif isinstance(value.get("name"), str):
                result.add(value["name"].strip())
            for key in ("data", "models", "items", "results", "list"):
                walk(value.get(key))

        walk(data)
        return sorted(item for item in result if item)

    async def _iter_draw_batch(
        self,
        event: AstrMessageEvent,
        prompt: str,
        aspect: str,
        resolution: str,
        refs: List[ImageReference],
        source: str,
        requested_count: int,
        passthrough: bool = False,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        total = self._normalize_count(requested_count)
        for index in range(total):
            if passthrough:
                result = await self._draw_passthrough_once(event, prompt, aspect, resolution, refs, source)
            else:
                result = await self._draw_once(event, prompt, aspect, resolution, refs, source)
            result["batch_index"] = index + 1
            result["batch_total"] = total
            yield result
            if not result.get("success"):
                return

    async def _draw_once(self, event: AstrMessageEvent, prompt: str, aspect: str, resolution: str, refs: List[ImageReference], source: str) -> Dict[str, Any]:
        final_prompt = build_prompt_with_reference_instruction(prompt, refs)
        return await self._run_image_generation(final_prompt, aspect, resolution, refs, source=source, audit_user_id=event_user_id(event), event=event, original_prompt=prompt)

    async def _draw_passthrough_once(self, event: AstrMessageEvent, prompt: str, aspect: str, resolution: str, refs: List[ImageReference], source: str) -> Dict[str, Any]:
        return await self._run_image_generation(prompt, aspect, resolution, refs, source=source, audit_user_id=event_user_id(event), event=event, original_prompt=prompt)

    async def _handle_selfie_command(
        self,
        event: AstrMessageEvent,
        command_name: Any,
        fallback: str,
        default_action: str,
        default_action_with_refs: str,
        progress_label: str,
        source: str,
        fail_label: str,
        message_override: str = "",
        include_at_avatar: bool = False,
        requested_count_override: int = 0,
    ) -> AsyncGenerator[Any, None]:
        message = message_override.strip() if message_override else extract_command_message(event, command_name, fallback)
        if requested_count_override > 0:
            requested_count = self._normalize_count(requested_count_override)
        else:
            message, requested_count = self._extract_command_count(message)

        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        action, aspect, resolution, _, _ = self._resolve_image_preset(message)
        extra_refs = await self._event_reference_images(
            event,
            include_at_avatar=include_at_avatar,
            context_hint=action,
            allow_context_fallback=True,
        )
        if not action:
            action = default_action_with_refs if extra_refs else default_action
        hints: List[str] = []
        if not self.persona.has_reference_image():
            hints.append("当前还没有设置 AI 形象参考图，会按人设与今日设定生成主角。")
        if progress_label == "合影" and not extra_refs:
            hints.append("没有读取到合影对象参考图，会按文字要求生成同框对象。")
        progress = await self._build_contextual_progress_text(event, "selfie", action, requested_count)
        if hints:
            progress += "\n" + "\n".join(hints)
        self._record_bot_text_context(event, progress)
        yield event.plain_result(progress)

        for index in range(requested_count):
            prompt, refs = await self._build_selfie_prompt_and_refs(action, extra_refs)
            result = await self._run_image_generation(prompt, aspect, resolution, refs, source=source, audit_user_id=event_user_id(event), event=event, original_prompt=action)
            if not result.get("success"):
                yield event.plain_result(self._friendly_user_error_message(str(result.get("error") or ""), fail_label))
                return
            files = result.get("files", [])
            if files:
                self._record_generated_images(event, 1)
                self._record_bot_image_context(event, files)
                yield event.chain_result([self._create_image_component(path) for path in files])
            info = self._batch_success_text(
                self._build_success_text(float(result.get("elapsed_seconds") or 0), len(files), str(result.get("used_model") or ""), event),
                index + 1,
                requested_count,
            )
            if info:
                yield event.plain_result(info)

    @filter.command("生图帮助")
    async def cmd_help(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        yield event.plain_result(
            "\n".join(
                [
                    f"{PLUGIN_DISPLAY_NAME} v{PLUGIN_VERSION}",
                    "/画 [数量] <预设名或提示词> [数量] [额外提示词] [--ar 1:1] [--resolution 2K]（别名 /生图）",
                    "/文生图 [数量] <原始提示词> [--ar 1:1] [--resolution 2K]（提示词直通）",
                    "/图生图 [数量] <原始提示词> [--ar 1:1] [--resolution 2K]（附带/引用图片，提示词直通）",
                    "/预设",
                    "/预设 查看 [页码/预设名]（管理员查看内容）",
                    "/预设添加 名称:提示词",
                    "/预设删除 名称",
                    "/自拍 [数量] <预设名或动作/场景/换装/合照要求> [--ar 3:4]（别名 /看看）",
                    "/看看腿 [数量] [额外要求] [--ar 3:4]",
                    "/看看你 [数量] [动作/场景] [--ar 3:4]（他拍感，不是手持自拍）",
                    "/合影 [数量] <动作/场景/合照要求> [--ar 1:1]（别名 /合照）",
                    "数量表示调用生图次数；模型每次实际返回 1 张或多张都会照常发送。",
                    "/形象查看",
                    "/形象设置 <发送图片、引用图片或图片链接>",
                    "/形象清除",
                    "/形象刷新",
                    "LLM 工具：generate_image、generate_selfie",
                    f"Flask Web：{'已启用' if self.config.web_enable else '未启用'} http://{self.config.web_host}:{self.config.web_port}",
                ]
            )
        )

    @filter.command("画", alias={"生图"})
    async def cmd_draw(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        message = extract_command_message(event, ("画", "生图"), fallback)
        message, requested_count = self._extract_command_count(message)
        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        prompt, aspect, resolution, _, _ = self._resolve_image_preset(message)
        refs = await self._event_reference_images(
            event,
            include_at_avatar=True,
            context_hint=prompt,
            allow_context_fallback=True,
        )
        if not prompt and refs:
            prompt = "根据参考图生成一张自然、清晰、符合原图语义的图片。"
        if not prompt:
            yield event.plain_result("请输入提示词或附带参考图。")
            return

        progress = await self._build_contextual_progress_text(event, "image", prompt, requested_count)
        self._record_bot_text_context(event, progress)
        yield event.plain_result(progress)
        async for result in self._iter_draw_batch(event, prompt, aspect, resolution, refs, "command-draw", requested_count):
            if not result.get("success"):
                yield event.plain_result(self._friendly_user_error_message(str(result.get("error") or ""), self._natural_fail_fallback("image")))
                return
            files = result.get("files", [])
            if files:
                self._record_generated_images(event, 1)
                self._record_bot_image_context(event, files)
                yield event.chain_result([self._create_image_component(path) for path in files])
            info = self._batch_success_text(
                self._build_success_text(float(result.get("elapsed_seconds") or 0), len(files), str(result.get("used_model") or ""), event),
                int(result.get("batch_index") or 1),
                int(result.get("batch_total") or requested_count),
            )
            if info:
                yield event.plain_result(info)

    @filter.command("文生图")
    async def cmd_raw_text_to_image(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        message = extract_command_message(event, "文生图", fallback)
        message, requested_count = self._extract_command_count(message)
        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        prompt, aspect, resolution = self._parse_prompt_options(message)
        if not prompt:
            yield event.plain_result("请输入文生图提示词。")
            return

        progress = await self._build_contextual_progress_text(event, "image", prompt, requested_count)
        self._record_bot_text_context(event, progress)
        yield event.plain_result(progress)
        async for result in self._iter_draw_batch(event, prompt, aspect, resolution, [], "command-raw-text-to-image", requested_count, passthrough=True):
            if not result.get("success"):
                yield event.plain_result(self._friendly_user_error_message(str(result.get("error") or ""), self._natural_fail_fallback("image")))
                return
            files = result.get("files", [])
            if files:
                self._record_generated_images(event, 1)
                self._record_bot_image_context(event, files)
                yield event.chain_result([self._create_image_component(path) for path in files])
            info = self._batch_success_text(
                self._build_success_text(float(result.get("elapsed_seconds") or 0), len(files), str(result.get("used_model") or ""), event),
                int(result.get("batch_index") or 1),
                int(result.get("batch_total") or requested_count),
            )
            if info:
                yield event.plain_result(info)

    @filter.command("图生图")
    async def cmd_raw_image_to_image(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        message = extract_command_message(event, "图生图", fallback)
        message, requested_count = self._extract_command_count(message)
        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        prompt, aspect, resolution = self._parse_prompt_options(message)
        refs, source_count, failed_count = await self._event_reference_images_with_stats(
            event,
            include_at_avatar=True,
            context_hint=prompt,
            allow_context_fallback=True,
        )
        if not refs:
            if source_count and failed_count:
                yield event.plain_result("参考图读取失败或超时，请重新发送原图后再试。")
                return
            yield event.plain_result("请附带、引用图片，或艾特要作为参考的对象。")
            return
        if not prompt:
            yield event.plain_result("请输入图生图提示词。")
            return

        progress = await self._build_contextual_progress_text(event, "image", prompt, requested_count)
        self._record_bot_text_context(event, progress)
        yield event.plain_result(progress)
        async for result in self._iter_draw_batch(event, prompt, aspect, resolution, refs, "command-raw-image-to-image", requested_count, passthrough=True):
            if not result.get("success"):
                yield event.plain_result(self._friendly_user_error_message(str(result.get("error") or ""), self._natural_fail_fallback("image")))
                return
            files = result.get("files", [])
            if files:
                self._record_generated_images(event, 1)
                self._record_bot_image_context(event, files)
                yield event.chain_result([self._create_image_component(path) for path in files])
            info = self._batch_success_text(
                self._build_success_text(float(result.get("elapsed_seconds") or 0), len(files), str(result.get("used_model") or ""), event),
                int(result.get("batch_index") or 1),
                int(result.get("batch_total") or requested_count),
            )
            if info:
                yield event.plain_result(info)

    @filter.command("自拍", alias={"看看"})
    async def cmd_selfie(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        async for item in self._handle_selfie_command(
            event=event,
            command_name=("自拍", "看看"),
            fallback=fallback,
            default_action="看着镜头自然自拍，展示你现在的样子",
            default_action_with_refs="参考用户提供的图片氛围和构图，看着镜头自然自拍，保持 AI 当前形象一致。",
            progress_label="自拍",
            source="command-selfie",
            fail_label=self._natural_fail_fallback("selfie"),
        ):
            yield item

    @filter.command("看看腿")
    async def cmd_look_legs(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        fallback_args = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        raw_message = extract_command_message(event, "看看腿", fallback_args)
        raw_extra, requested_count = self._extract_command_count(raw_message)
        fallback = self._build_leg_focus_action(raw_extra, bool(extract_image_sources_from_event(event)))
        async for item in self._handle_selfie_command(
            event=event,
            command_name="看看腿",
            fallback=fallback,
            default_action=self._build_leg_focus_action("", False),
            default_action_with_refs=self._build_leg_focus_action("", True),
            progress_label="自拍",
            source="command-look-legs",
            fail_label=self._natural_fail_fallback("legs"),
            message_override=fallback,
            requested_count_override=requested_count,
        ):
            yield item

    @filter.command("看看你")
    async def cmd_look_you(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        fallback_args = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        raw_message = extract_command_message(event, "看看你", fallback_args)
        raw_extra, requested_count = self._extract_command_count(raw_message)
        fallback = self._build_third_person_look_action(raw_extra, bool(extract_image_sources_from_event(event)))
        async for item in self._handle_selfie_command(
            event=event,
            command_name="看看你",
            fallback=fallback,
            default_action=self._build_third_person_look_action("", False),
            default_action_with_refs=self._build_third_person_look_action("", True),
            progress_label="自拍",
            source="command-look-you",
            fail_label=self._natural_fail_fallback("selfie"),
            message_override=fallback,
            requested_count_override=requested_count,
        ):
            yield item

    @filter.command("合影", alias={"合照"})
    async def cmd_group_selfie(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        raw_message = extract_command_message(event, ("合影", "合照"), fallback)
        raw_message, requested_count = self._extract_command_count(raw_message)
        action = self._build_group_selfie_action(raw_message, bool(extract_image_sources_from_event(event, include_at_avatar=True)))
        async for item in self._handle_selfie_command(
            event=event,
            command_name=("合影", "合照"),
            fallback=fallback,
            default_action=self._build_group_selfie_action("", False),
            default_action_with_refs=self._build_group_selfie_action("", True),
            progress_label="合影",
            source="command-group-selfie",
            fail_label=self._natural_fail_fallback("group"),
            message_override=action,
            include_at_avatar=True,
            requested_count_override=requested_count,
        ):
            yield item

    @filter.command("形象查看")
    async def cmd_persona_status(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        await self.persona.ensure_daily_selfie_profile("查看今日自拍设定")
        path = self.persona.get_reference_path()
        if path:
            yield event.chain_result([self._create_image_component(path)])
        yield event.plain_result(self.persona.status_text())

    @filter.command("形象设置")
    async def cmd_persona_set(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        sources = extract_image_sources_from_event(event, include_at_avatar=False)
        text = extract_event_text(event)
        sources.extend(extract_image_urls(text))
        sources = list(dict.fromkeys(sources))
        if not sources:
            yield event.plain_result("请发送图片、引用图片，或在指令后附带图片链接。")
            return
        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        async with aiohttp.ClientSession() as session:
            for source in sources:
                fetched = await fetch_image_source(source, session, max_bytes=max_bytes)
                if not fetched:
                    continue
                data, mime = fetched
                self.persona.save_reference_image(data, mime)
                yield event.plain_result("AI 自拍形象参考图已保存。")
                return
        yield event.plain_result("没有读取到可用图片，或图片超过大小限制。")

    @filter.command("形象清除")
    async def cmd_persona_clear(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        self.persona.clear_reference_image()
        yield event.plain_result("AI 自拍形象参考图已清除。")

    @filter.command("形象刷新")
    async def cmd_persona_refresh(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        self.persona.refresh_daily_selfie_profile_for_test()
        await self.persona.ensure_daily_selfie_profile("手动刷新今日自拍设定")
        yield event.plain_result("今日自拍设定已刷新。\n" + self.persona.status_text())

    @filter.command("预设", prefix_optional=True)
    async def cmd_preset(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        message = extract_command_message(event, "预设", fallback)
        text = self._normalize_preset_input(message)

        if not text:
            body, _, _ = self._preset_list_text(1)
            yield event.plain_result(body)
            return

        head, tail = self._split_preset_command(text)
        if head.isdigit():
            body, _, _ = self._preset_list_text(int(head))
            yield event.plain_result(body)
            return

        if head in {"列表", "list"}:
            page = int(tail) if tail.isdigit() else 1
            body, _, _ = self._preset_list_text(page)
            yield event.plain_result(body)
            return

        if head in {"查看", "详情", "view", "detail"}:
            if not self._is_admin_event(event):
                yield event.plain_result("仅管理员可以查看预设内容。")
                return
            if not tail or tail.isdigit():
                body, _, _ = self._preset_detail_text(int(tail) if tail.isdigit() else 1)
                yield event.plain_result(body)
                return
            success, body = self._preset_single_detail_text(tail)
            yield event.plain_result(body if success else f"❌ {body}")
            return

        if head in {"添加", "add", "新增"}:
            if not tail:
                yield event.plain_result("格式：/预设添加 名称:提示词")
                return
            success, message = self._handle_preset_mutation(event, "add", tail)
            yield event.plain_result(f"{'✅' if success else '❌'} {message}")
            return

        if head in {"删除", "del", "delete", "remove", "删"}:
            if not tail:
                yield event.plain_result("格式：/预设删除 名称")
                return
            success, message = self._handle_preset_mutation(event, "delete", tail)
            yield event.plain_result(f"{'✅' if success else '❌'} {message}")
            return

        body, _, _ = self._preset_list_text(1)
        yield event.plain_result(
            "\n".join(
                [
                    body,
                    "",
                    "用法：/预设 2、/预设 添加 名称:提示词、/预设 删除 名称、/预设 查看 [页码/预设名]（管理员）",
                ]
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("预设添加", prefix_optional=True)
    async def cmd_preset_add(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        payload = self._normalize_preset_input(extract_command_message(event, "预设添加", fallback))
        if not payload:
            yield event.plain_result("格式：/预设添加 名称:提示词")
            return
        success, message = self._handle_preset_mutation(event, "add", payload)
        yield event.plain_result(f"{'✅' if success else '❌'} {message}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("预设删除", prefix_optional=True)
    async def cmd_preset_delete(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        payload = self._normalize_preset_input(extract_command_message(event, "预设删除", fallback))
        if not payload:
            yield event.plain_result("格式：/预设删除 名称")
            return
        success, message = self._handle_preset_mutation(event, "delete", payload)
        yield event.plain_result(f"{'✅' if success else '❌'} {message}")

    @LLM_TOOL(name="generate_image")
    async def tool_generate_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
        count: int = 1,
        aspect_ratio: str = "",
        resolution: str = "",
        size: str = "",
        ack_message: str = "",
    ) -> Optional[str]:
        """
        使用生图模型生成普通图片，支持文生图和参考图图生图。
        自拍、AI 自己、合影、合照、同框、与用户一起拍照等请求使用 generate_selfie。
        prompt 保持简洁，保留主体、场景、动作/风格、构图和参考图关系即可。
        闲聊中顺势画图时，ack_message 用当前人格自然短句接话，简体中文 10-40 字。
        Args:
            prompt(string): 简洁生图提示词，描述主体、场景、动作/风格、构图和参考图使用方式。
            count(number): 调用生图次数，默认 1；每次调用可能返回一张或多张图片。
            aspect_ratio(string): 宽高比，例如 1:1、3:4、9:16、16:9；留空使用默认值。
            resolution(string): 分辨率，例如 1K、2K、4K；留空使用默认值。
            size(string): 兼容参数，可传 1024x1024、2048x2048 或 4096x4096。
            ack_message(string): 可选。根据当前对话和机器人人格生成的简体中文短进度回复。
        """
        if not self.config.image_enable_llm_tool:
            return self._tool_unavailable("我这会儿还没法把这个画面整理出来。")
        requested_count = self._normalize_count(count)
        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            return self._tool_soft_fail(error)
        prompt, aspect, resol, _, _ = self._resolve_image_preset(prompt, aspect_ratio, resolution or size)
        if not prompt:
            return self._tool_soft_fail("缺少生图提示词", "你想让我往什么感觉走？")
        if self._looks_like_selfie_intent(prompt):
            return await self._run_llm_selfie_flow(event, prompt, requested_count, aspect, resol, ack_message)

        await self._send_progress_text(
            event,
            await self._build_contextual_progress_text(event, "image", prompt, requested_count, ack_message),
        )
        refs = await self._event_reference_images(
            event,
            include_at_avatar=True,
            context_hint=prompt,
            allow_context_fallback=True,
        )
        total_sent = 0
        for _ in range(requested_count):
            result = await self._draw_once(event, prompt, aspect, resol, refs, "llm-generate-image")
            if not result.get("success"):
                return self._tool_soft_fail(str(result.get("error") or ""), self._natural_fail_fallback("image"))
            sent = await self._send_generated_images(event, result.get("files", []))
            total_sent += sent
            if sent:
                self._record_generated_images(event, 1)
        return self._tool_success("image", total_sent or requested_count)

    @LLM_TOOL(name="generate_selfie")
    async def tool_generate_selfie(
        self,
        event: AstrMessageEvent,
        action: str,
        count: int = 1,
        aspect_ratio: str = "",
        resolution: str = "",
        size: str = "",
        ack_message: str = "",
    ) -> Optional[str]:
        """
        以当前 AI 助手自己的形象生成自拍、形象照、换装照、姿势照、合影或同框照。
        用户要求“合影/合照/同框/和我一起拍/和你一起拍/我们拍一张”时使用这个工具。
        用户要求 AI 自己“穿这个/穿这套/换这身/换衣服/用这个姿势/摆这个姿势/照这个姿势”并附带参考图时，也使用这个工具。
        本工具会自动带上 AI 当前形象参考图；如果用户消息里附带图片，也会作为合影对象或参考图一起传入。
        非合影换装或换姿势时，附带图片默认只作为服装、姿势、构图或风格参考，AI 的脸和身份仍来自当前形象参考图。
        如果附带图片里的人用手机、手、道具、口罩、面具或其他东西挡脸，默认不要把挡脸物迁移到 AI 身上，除非用户明确要求遮脸。
        action 保持简洁，整理出动作/场景/情绪/服装/镜头语言；合影时写清同框关系和参考图对象。
        ack_message 使用简体中文，以当前人格自然回应，10-40 字。
        Args:
            action(string): 简洁自拍/合影要求，包含动作、表情、服装、环境、镜头或同框关系。
            count(number): 调用自拍生图次数，默认 1；每次调用可能返回一张或多张图片。
            aspect_ratio(string): 宽高比，例如 1:1、3:4、9:16、16:9；留空使用默认值。
            resolution(string): 分辨率，例如 1K、2K、4K；留空使用默认值。
            size(string): 兼容参数，可传 1024x1024、2048x2048 或 4096x4096。
            ack_message(string): 可选。根据当前对话和机器人人格生成的简体中文短进度回复。
        """
        if not self.config.image_enable_llm_tool:
            return self._tool_unavailable("我这会儿还没法拍这个给你看。")
        requested_count = self._normalize_count(count)
        action, aspect, resol, _, _ = self._resolve_image_preset(action or "看着镜头自然自拍", aspect_ratio, resolution or size)
        return await self._run_llm_selfie_flow(event, action, requested_count, aspect, resol, ack_message)
