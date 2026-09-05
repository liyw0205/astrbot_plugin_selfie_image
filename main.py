"""AstrBot image and selfie generation plugin."""

from __future__ import annotations

import asyncio
import copy
import hashlib
from io import BytesIO
import json
import os
import random
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Dict, Iterable, List, Optional, Tuple

import aiohttp

try:
    from astrbot.api.star import Context, Star, register
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api import llm_tool, logger
except ImportError:  # Compatibility with older AstrBot layouts
    from astrbot.api.star import Context, Star, register
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api import llm_tool
    from astrbot.api.utils import logger

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:
    def get_astrbot_data_path() -> str:
        return os.path.join(os.getcwd(), "data")

from .core.constants import (
    PLUGIN_AUTHOR,
    PLUGIN_CONFIG_FILENAME,
    PLUGIN_DISPLAY_NAME,
    PLUGIN_NAME,
    PLUGIN_VERSION,
)
from .features.access_policy import (
    access_status,
    blocked_prompt_word,
    permission_denied_message,
    quota_error_message,
)
from .features.context_routing import (
    compact_followup_text,
    format_context_for_llm,
    looks_like_clothes_followup,
    looks_like_context_image_reference,
    looks_like_edit_bot_result_followup,
    recent_context_image_sources,
)
from .prompts.command_parser import (
    command_tokens_for_count,
    expand_cos_user_text_with_preset,
    expand_user_text_with_preset,
    extract_command_count,
    normalize_count,
    normalize_preset_input,
    parse_count_token,
    parse_prompt_options,
    resolve_image_preset,
    split_attached_count_token,
    split_preset_command,
)
from .cos.cos_looks import (
    COS_LOOK_CATEGORY_TERMS,
    COS_LOOK_SETS,
    _cos_item_terms,
    adapt_cos_outfit_for_camera,
    build_cos_look_action,
    format_cos_look_list,
    list_cos_look_sets,
    match_cos_look_sets,
    parse_requested_cos_camera,
    pick_cos_camera,
    pick_cos_look_set,
)
from .generation.generator import generate_image_with_fallback
from .generation.generation_store import GenerationStoreMixin, RECORD_KEEP_LIMIT
from .generation.generation_results import (
    batch_failure_policy,
    batch_failure_text,
    batch_success_text,
    normalize_generation_result,
)
from .cos.leg_focus import (
    CALF_CROP_POSES,
    LEGFOCUS_CAMERA_WEIGHTS,
    LEGFOCUS_RISKY_EXTRA_REPLACEMENTS,
    LEGWEAR_BY_POSE,
    LEGWEAR_PROMPTS,
    LEGWEAR_REQUEST_PATTERN,
    SAFE_LEGWEAR_LABELS,
    STOCKING_FINISH_CHOICES,
    build_leg_focus_action,
    is_leg_calf_crop_action,
    parse_requested_legwear,
    pick_stocking_finish,
)
from .features.model_selection import (
    available_model_labels,
    find_model_target,
    match_model_label,
    prioritize_model_target,
)
from .prompts.preset import ImagePresetManager, VideoPresetManager
from .core.models import (
    AICatConfig,
    DEFAULT_CONFIG,
    ImageModelTarget,
    preflight_video_channel,
)
from .features.persona import PersonaManager
from .studio.studio import (
    BUILTIN_PROMPTS,
    StudioStore,
    build_studio_action,
    global_prompt_presets,
    list_studio_templates,
    normalize_template_id,
    prompts_for_template,
    resolve_slot_refs_for_run,
)
from .studio.studio_adapter import StudioMixin
from .features.config_manager import ConfigurationMixin
from .features.conversation_context import ConversationContextMixin
from .tasks.task_views import (
    filter_image_tasks,
    format_task_detail_text,
    format_task_list_text,
)
from .tasks.task_manager import WebTaskMixin
from .core.providers import (
    ImageGenerateRequest,
    ImageReference,
    build_model_list_urls,
    extract_model_ids_from_response,
    normalize_image_base_url,
    provider_type_from_channel_payload,
)
from .prompts.prompt_composition import (
    append_anatomy_constraints,
    build_prompt_with_reference_instruction,
)
from .prompts.prompt_translation import parse_prompt_en_response
from .features.audit_pipeline import AuditMixin
from .features.reference_collector import extract_structured_image_sources
from .features.reference_media import ReferenceMediaMixin
from .prompts.response_text import (
    ack_repeats_request,
    clean_ack_message,
    compact_for_repeat_check,
    friendly_user_error_message,
    looks_like_non_chinese_ack,
    natural_ack_fallback,
    natural_fail_fallback,
    tool_soft_fail,
    tool_success,
    tool_unavailable,
)
from .prompts.selfie_actions import (
    build_crop_waist_selfie_action,
    build_group_selfie_action,
    build_selfie_look_action,
    build_third_person_look_action,
    looks_like_crop_waist_request,
    looks_like_group_selfie_intent,
    looks_like_selfie_intent,
    period_scene_light_pools,
)
from .core.proxy import LOCAL_IMAGE_WAIT_SECONDS, channel_client_session, http_proxy_url, image_client_timeout, target_session_proxy
from .generation.video import VideoGenerateRequest, generate_video_with_fallback
from .core.utils import (
    bytes_to_data_url,
    collect_record_cache_paths,
    collect_cache_cleanup_candidates,
    collect_unreferenced_record_cache_paths,
    compact_generation_record,
    split_generation_record_images,
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
    looks_like_image_bytes,
    normalize_image_mime,
    parse_audit_response_text,
    redact_sensitive_data,
    redact_sensitive_text,
    resolve_awaitable,
    safe_delete_relative_files,
    save_image_bytes,
    save_json_file,
    summarize_record_for_list,
)
from .webui.dashboard_api import SelfieImageDashboardAPI
from .webui.web import FlaskWebServer

LLM_TOOL = getattr(filter, "llm_tool", llm_tool)
IMAGE_BATCH_REQUEST_COOLDOWN_SECONDS = 2.0


def optional_event_message_type(priority: int = 100):
    decorator = getattr(filter, "event_message_type", None)
    event_type = getattr(getattr(filter, "EventMessageType", None), "ALL", None)
    if callable(decorator) and event_type is not None:
        return decorator(event_type, priority=priority)

    def passthrough(func):
        return func

    return passthrough


@register(PLUGIN_NAME, PLUGIN_AUTHOR, f"{PLUGIN_DISPLAY_NAME} v{PLUGIN_VERSION}", PLUGIN_VERSION)
class SelfieImagePlugin(
    StudioMixin,
    ReferenceMediaMixin,
    ConversationContextMixin,
    ConfigurationMixin,
    AuditMixin,
    GenerationStoreMixin,
    WebTaskMixin,
    Star,
):
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
        self.tasks_path = os.path.join(self.data_dir, "generation_tasks.json")
        self.generated_dir = os.path.join(self.data_dir, "image_cache")
        os.makedirs(self.generated_dir, exist_ok=True)
        self.video_dir = os.path.join(self.generated_dir, "video")
        os.makedirs(self.video_dir, exist_ok=True)
        self._plugin_root = os.path.dirname(os.path.abspath(__file__))
        self._bundled_logo_path = os.path.join(self._plugin_root, "logo.png")
        # Pre-generated static help poster (shipped in repo; never generated at runtime).
        assets_dir = os.path.join(self._plugin_root, "assets")
        self._bundled_help_poster_path = ""
        for name in ("help_poster.png", "help_poster.jpg", "help_poster.webp"):
            candidate = os.path.join(assets_dir, name)
            if os.path.isfile(candidate):
                self._bundled_help_poster_path = candidate
                break
        if not self._bundled_help_poster_path:
            for name in ("help_poster.png", "help_poster.jpg"):
                candidate = os.path.join(self._plugin_root, name)
                if os.path.isfile(candidate):
                    self._bundled_help_poster_path = candidate
                    break

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
        self._llm_generation_lock = threading.RLock()
        self._last_llm_generations: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._records: List[Dict[str, Any]] = self._load_records()
        self._record_seq = len(self._records)
        self._web_task_lock = threading.RLock()
        self._web_tasks: Dict[str, Dict[str, Any]] = self._load_web_tasks()
        self._web_task_seq = 0
        self._runtime_generation_tasks: Dict[str, asyncio.Task] = {}
        # 模型选择仅作用于当前会话。
        self._session_model_lock = threading.RLock()
        self._session_model_overrides: Dict[str, str] = {}
        self._last_request_at: Dict[str, float] = {}
        self._channel_health: Dict[str, Dict[str, Any]] = {}
        self._channel_health_lock = threading.RLock()
        self._send_failures: Dict[str, List[str]] = {}
        self._send_failures_lock = threading.RLock()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        native_config = self._config_object_to_dict(config)
        if not native_config and self._native_config_path:
            native_config = load_json_file(self._native_config_path)
        self.key_config = self._extract_native_key_config(native_config)
        self.raw_config = self._load_initial_config()
        self.config = AICatConfig.from_dict(self.raw_config)
        self.persona = PersonaManager(self.data_dir)
        self.presets = ImagePresetManager(self.data_dir)
        self.video_presets = VideoPresetManager(self.data_dir)
        self.studio = StudioStore(self.data_dir)
        self._usage_stats = self._load_usage_stats()
        self._semaphore = asyncio.Semaphore(self.config.image_max_concurrent_tasks)
        self._video_semaphore = asyncio.Semaphore(max(1, int(getattr(self.config, "video_max_concurrent_tasks", 1) or 1)))
        # Reserve image slots per requested shot, not per whole command batch.
        self._image_batch_gate = asyncio.Semaphore(self.config.image_max_concurrent_tasks)
        self._selfie_batch_gate = self._image_batch_gate
        self._image_batch_cooldown_lock = asyncio.Lock()
        self.web_server = FlaskWebServer(self)
        self.dashboard_api = SelfieImageDashboardAPI(self)
        try:
            self.dashboard_api.register()
        except Exception as exc:
            logger.warning(f"[SelfieImage] 注册 AstrBot 内嵌管理页 API 失败: {exc}", exc_info=True)

        # Do not write config files during startup. If AstrBot passes an empty
        # or not-yet-populated config object, writing here would overwrite the
        # user's saved config with defaults before the plugin is usable.

    async def initialize(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._start_web_server()

    async def terminate(self) -> None:
        self.web_server.stop()

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
        return access_status(
            user_id=user_id,
            group_id=group_id,
            blocked_users=self.config.blocked_users,
            usable_users=self.config.usable_users,
            whitelist_users=self.config.whitelist_users,
            whitelist_groups=self.config.whitelist_groups,
        )

    def _permission_denied_message(self, event: AstrMessageEvent) -> str:
        return permission_denied_message(self._access_status(event))

    def _quota_error_message(self, event: AstrMessageEvent, requested_count: int = 1) -> str:
        permission_error = self._permission_denied_message(event)
        if permission_error:
            return permission_error
        status = self._access_status(event)
        status["allowed"] = True
        return quota_error_message(
            status,
            self._current_usage_stats(),
            enabled=self.config.image_enable_daily_limit,
            limit=self.config.image_daily_limit_count,
            requested_count=requested_count,
        )

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
        return blocked_prompt_word(prompt, self.config.image_blocked_words)

    def _bot_display_name(self) -> str:
        name = str(self.config.bot_name or "").strip()
        return name or "啊呜"

    def _compact_for_repeat_check(self, text: str) -> str:
        return compact_for_repeat_check(text)

    def _ack_repeats_request(self, ack_message: str, user_request: str) -> bool:
        return ack_repeats_request(ack_message, user_request)

    def _looks_like_non_chinese_ack(self, text: str) -> bool:
        return looks_like_non_chinese_ack(text)

    def _clean_ack_message(self, ack_message: str, user_request: str) -> str:
        return clean_ack_message(ack_message, user_request)

    def _natural_ack_fallback(self, kind: str, count: int) -> str:
        return natural_ack_fallback(kind, count, self._bot_display_name())

    def _natural_fail_fallback(self, kind: str = "") -> str:
        return natural_fail_fallback(kind)

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

    def _image_parser_defaults(self) -> Tuple[str, str]:
        aspect = str(self.config.image_default_aspect_ratio or "9:16").strip() or "9:16"
        resolution = str(self.config.image_default_resolution or "1K").strip() or "1K"
        return aspect, resolution

    def _normalize_count(self, count: Any) -> int:
        return normalize_count(count, self.config.image_max_batch_count)

    def _parse_count_token(self, token: str) -> int:
        return parse_count_token(token)

    def _split_attached_count_token(self, token: str) -> Tuple[str, int]:
        return split_attached_count_token(token)

    def _command_tokens_for_count(self, text: str) -> List[str]:
        return command_tokens_for_count(text)

    def _extract_command_count(
        self,
        text: str,
        *,
        allow_attached: bool = False,
        allow_trailing: bool = False,
    ) -> Tuple[str, int]:
        return extract_command_count(
            text,
            self.config.image_max_batch_count,
            allow_attached=allow_attached,
            allow_trailing=allow_trailing,
        )

    def _parse_prompt_options(
        self,
        text: str,
        aspect_ratio: str = "",
        resolution: str = "",
    ) -> Tuple[str, str, str]:
        default_aspect, default_resolution = self._image_parser_defaults()
        return parse_prompt_options(
            text,
            aspect_ratio,
            resolution,
            default_aspect_ratio=default_aspect,
            default_resolution=default_resolution,
        )

    def _resolve_image_preset(
        self,
        prompt: str,
        aspect_ratio: str = "",
        resolution: str = "",
    ) -> Tuple[str, str, str, str, str]:
        default_aspect, default_resolution = self._image_parser_defaults()
        return resolve_image_preset(
            prompt,
            aspect_ratio,
            resolution,
            presets=self.presets,
            default_aspect_ratio=default_aspect,
            default_resolution=default_resolution,
        )

    def _expand_user_text_with_preset(
        self, raw_text: str
    ) -> Tuple[str, str, str, str]:
        default_aspect, default_resolution = self._image_parser_defaults()
        return expand_user_text_with_preset(
            raw_text,
            presets=self.presets,
            default_aspect_ratio=default_aspect,
            default_resolution=default_resolution,
        )

    def _expand_cos_user_text_with_preset(
        self, raw_text: str
    ) -> Tuple[str, str, str, str]:
        default_aspect, default_resolution = self._image_parser_defaults()
        return expand_cos_user_text_with_preset(
            raw_text,
            presets=self.presets,
            default_aspect_ratio=default_aspect,
            default_resolution=default_resolution,
        )

    def _normalize_preset_input(self, text: str) -> str:
        return normalize_preset_input(text)

    def _split_preset_command(self, text: str) -> Tuple[str, str]:
        return split_preset_command(text)

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

    async def _call_text_llm(
        self,
        event: Optional[AstrMessageEvent],
        prompt: str,
        timeout: int = 8,
        images: Optional[List[bytes]] = None,
    ) -> str:
        image_urls = [
            bytes_to_data_url(image, detect_mime_by_bytes(image))
            for image in (images or [])
            if image
        ]

        async def request() -> str:
            origin = getattr(event, "unified_msg_origin", None)
            provider_id = None
            try:
                getter = getattr(self.context, "get_using_provider", None)
                if callable(getter):
                    provider = getter()
                    requester = getattr(provider, "text_chat", None) or getattr(provider, "request", None)
                    if callable(requester):
                        kwargs: Dict[str, Any] = {"prompt": prompt}
                        if image_urls:
                            kwargs["image_urls"] = image_urls
                        response = requester(**kwargs)
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
                    if image_urls:
                        kwargs["image_urls"] = image_urls
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
        return "\n".join(
            [
                f"你是{name}，正在和用户自然聊天。",
                f"用户刚通过指令发起了{kind_text}，数量：{count_text}。",
                "请只输出一句简体中文短回复，像正在聊天时随口接一句。",
                "要求：10-32 个汉字；结合最近上下文；不要复述用户提示词；不要解释；不要列点。",
                "禁止出现：生成、绘制、渲染、工具、提示词、配置、审核、任务、处理中、已收到、开始、为你。",
                "不要套用人设、语气词或氛围设定；只按当前对话自然接一句。",
                "如果是自拍/拍照，可以表现为找角度、看光线、调整镜头；如果是普通图片，可以表现为整理画面或构图。",
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

    def _video_preset_list_text(self, page: int = 1, page_size: int = 20) -> Tuple[str, int, int]:
        self.video_presets.load()
        presets = self.video_presets.list()
        total = len(presets)
        total_pages = max(1, (total + page_size - 1) // page_size)
        current_page = min(total_pages, max(1, page))
        start = (current_page - 1) * page_size
        items = presets[start:start + page_size]
        lines = [
            f"📋 视频预设 第 {current_page}/{total_pages} 页",
            f"当前共有 {total} 个预设。",
            "",
            "使用方式：",
            "1. /视频 预设名 额外动作说明",
            "2. /视频预设 添加 名称:提示词（管理员）",
            "3. /视频预设 删除 名称（管理员）",
            "4. /视频预设 查看 [页码/预设名]（管理员）",
            "",
        ]
        if total_pages > 1:
            if current_page < total_pages:
                lines.append(f"下一页：/视频预设 {current_page + 1}")
            if current_page > 1:
                lines.append(f"上一页：/视频预设 {current_page - 1}")
            lines.append("")
        lines.append("暂无预设。" if not items else "预设名：")
        for idx, (name, preset) in enumerate(items, start=start + 1):
            duration = f" ({preset.duration}s)" if preset.duration else ""
            lines.append(f"{idx}. {name}{duration}")
        return "\n".join(lines), current_page, total_pages

    def _video_preset_detail_text(self, page: int = 1, page_size: int = 20) -> Tuple[str, int, int]:
        self.video_presets.load()
        presets = self.video_presets.list()
        total = len(presets)
        total_pages = max(1, (total + page_size - 1) // page_size)
        current_page = min(total_pages, max(1, page))
        start = (current_page - 1) * page_size
        items = presets[start:start + page_size]
        lines = [f"📋 视频预设详情 第 {current_page}/{total_pages} 页", f"当前共有 {total} 个预设。", "仅管理员可见。", ""]
        if not items:
            lines.append("暂无预设。")
        else:
            for idx, (name, preset) in enumerate(items, start=start + 1):
                lines.extend(self._preset_detail_lines(idx, name, preset))
        return "\n".join(lines), current_page, total_pages

    def _video_preset_single_detail_text(self, name: str) -> Tuple[bool, str]:
        target = str(name or "").strip()
        if not target:
            return False, "格式：/视频预设 查看 预设名"
        self.video_presets.load()
        for preset_name, preset in self.video_presets.list():
            if preset_name == target:
                duration = f"\n默认时长: {preset.duration}s" if preset.duration else ""
                return True, f"📋 视频预设详情\n\n{preset_name}\n提示词: {preset.prompt}{duration}"
        return False, f"预设不存在: {target}"

    def _handle_video_preset_mutation(self, event: AstrMessageEvent, action: str, payload: str) -> Tuple[bool, str]:
        if not self._is_admin_event(event):
            return False, "仅管理员可以管理预设。"
        if action == "add":
            if ":" in payload:
                name, value = payload.split(":", 1)
            elif "：" in payload:
                name, value = payload.split("：", 1)
            else:
                return False, "格式：/视频预设 添加 名称:提示词"
            return self.video_presets.add(name, value)
        if action == "delete":
            return self.video_presets.remove(payload)
        return False, "未知操作"

    def _friendly_user_error_message(self, error: str, fallback: str = "") -> str:
        return friendly_user_error_message(
            error,
            fallback,
            default_fail=lambda: self._natural_fail_fallback("image"),
        )

    def _tool_soft_fail(self, error: str, fallback: str = "") -> str:
        return tool_soft_fail(error, fallback, self._friendly_user_error_message)

    def _tool_unavailable(self, fallback: str) -> str:
        return tool_unavailable(fallback)

    def _tool_success(self, kind: str = "image", count: int = 1) -> str:
        return tool_success(kind, count)

    def _build_leg_focus_action(
        self,
        extra_request: str = "",
        has_refs: bool = False,
        *,
        avoid_pose: str = "",
        force_legwear: str = "",
    ) -> str:
        return build_leg_focus_action(
            extra_request,
            has_refs,
            avoid_pose=avoid_pose,
            force_legwear=force_legwear,
        )

    def _normalize_selfie_action(self, action: str, has_refs: bool) -> str:
        """为腿部自拍补全单一姿势与腿部穿搭。"""
        raw = str(action or "").strip()
        pose_match = re.search(r"【pose:([a-z_]+)】", raw)
        removed_pose = "stand_" + "topdown"
        if pose_match and pose_match.group(1) == removed_pose:
            return self._build_leg_focus_action(raw, has_refs, avoid_pose=removed_pose)
        if pose_match or not self.persona.analyze_selfie_intent(raw).is_legs_only:
            return raw
        return self._build_leg_focus_action(raw, has_refs)


    @staticmethod
    def _period_scene_light_pools(kind: str = "selfie") -> tuple[list[str], list[str]]:
        return period_scene_light_pools(kind)

    def _build_selfie_look_action(
        self,
        extra_request: str = "",
        has_refs: bool = False,
        *,
        avoid_shot: str = "",
    ) -> str:
        scene_provider = getattr(self, "_period_scene_light_pools", period_scene_light_pools)
        return build_selfie_look_action(
            extra_request,
            has_refs,
            avoid_shot=avoid_shot,
            scene_light_provider=scene_provider,
        )

    @staticmethod
    def _looks_like_crop_waist_request(text: str) -> bool:
        return looks_like_crop_waist_request(text)

    def _build_crop_waist_selfie_action(
        self, extra_request: str = "", has_refs: bool = False
    ) -> str:
        return build_crop_waist_selfie_action(extra_request, has_refs)

    def _build_third_person_look_action(
        self,
        extra_request: str = "",
        has_refs: bool = False,
        *,
        avoid_shot: str = "",
    ) -> str:
        scene_provider = getattr(self, "_period_scene_light_pools", period_scene_light_pools)
        return build_third_person_look_action(
            extra_request,
            has_refs,
            avoid_shot=avoid_shot,
            scene_light_provider=scene_provider,
        )

    def _build_group_selfie_action(self, extra_request: str = "", has_refs: bool = False) -> str:
        appearance_type = "auto"
        try:
            appearance_type = self.persona.get_appearance_type()
        except Exception:
            pass
        return build_group_selfie_action(
            extra_request,
            has_refs,
            appearance_type=appearance_type,
        )

    def _looks_like_group_selfie_intent(self, text: str) -> bool:
        return looks_like_group_selfie_intent(text)

    def _looks_like_selfie_intent(self, text: str) -> bool:
        return looks_like_selfie_intent(text, bot_name=str(self.config.bot_name or "").strip())

    def _build_cos_look_action(
        self,
        extra_request: str = "",
        has_refs: bool = False,
        *,
        avoid_id: str = "",
        avoid_camera: str = "",
        camera: str = "",
        match_query: str = "",
    ) -> str:
        return build_cos_look_action(
            extra_request,
            has_refs,
            avoid_id=avoid_id,
            avoid_camera=avoid_camera,
            camera=camera,
            match_query=match_query,
            picker=pick_cos_look_set,
        )

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
        action = self._normalize_selfie_action(action, bool(extra_refs))
        result = await self._background_selfie_batches(
            "llm-generate-selfie",
            event,
            action,
            extra_refs,
            "llm-generate-selfie",
            requested_count,
            aspect,
            resolution,
            self._natural_fail_fallback("selfie"),
        )
        if not result.get("success") and not result.get("files"):
            return self._tool_soft_fail(str(result.get("error") or ""), self._natural_fail_fallback("selfie"))
        return self._tool_success("selfie", len(result.get("files") or []) or requested_count)

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
        return batch_success_text(info, index, total)

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
        prompt_en_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # Enabled channels remain eligible after transient failures. Operators
        # decide whether to disable or remove an unavailable channel.
        selected_targets = targets or self._resolve_generation_targets(event)
        request_prompt = str(prompt or "")
        plain_request_prompt = request_prompt
        original_prompt = str(original_prompt or request_prompt)
        # Leg-focus actions are normalized into a neutral outfit prompt before generation.
        # Audit that effective prompt so command labels do not create false positives.
        is_leg_focus_request = (
            source == "command-look-legs"
            or "【legs:outfit】" in original_prompt
            or "下半身穿搭" in original_prompt
        )
        audit_prompt_text = request_prompt if is_leg_focus_request else (original_prompt or request_prompt)
        source_meta = self._source_context(event, source, audit_user_id)
        request_image_paths = self._save_reference_images_to_cache(refs)
        image_to_text_targets = [
            target
            for target in selected_targets
            if bool((getattr(target, "extra", {}) or {}).get("image_to_text_enabled"))
        ]
        image_to_text_meta: Dict[str, Any] = {
            "enabled": bool(refs and image_to_text_targets),
            "applied": False,
            "target_count": len(image_to_text_targets),
        }
        if refs and image_to_text_targets:
            try:
                reference_description, image_to_text_meta = await self._describe_reference_images_for_generation(
                    event, [ref.data for ref in refs if ref and ref.data]
                )
                # Keep the user's instruction intact while making the visual
                # details available to models that receive a text-only request.
                request_prompt = "\n\n".join(
                    part for part in (
                        request_prompt.strip(),
                        f"参考图内容描述：{reference_description}",
                    ) if part
                )
                audit_prompt_text = request_prompt
            except Exception as exc:
                error = f"图转文失败：{redact_sensitive_text(str(exc))}"
                image_to_text_meta = {
                    **image_to_text_meta,
                    "applied": False,
                    "error": redact_sensitive_text(str(exc)),
                }
                plain_targets = [target for target in selected_targets if target not in image_to_text_targets]
                if plain_targets:
                    # A failed helper must not block fallback models that still
                    # support ordinary image-to-image requests.
                    selected_targets = plain_targets
                    image_to_text_targets = []
                else:
                    response_data = {"success": False, "stage": "image_to_text", "error": error}
                    request_data = {
                        "original_prompt": original_prompt,
                        "request_prompt": request_prompt,
                        "audit_prompt": audit_prompt_text,
                        "aspect_ratio": aspect_ratio,
                        "resolution": resolution,
                        "reference_image_count": len(refs),
                        "request_image_paths": request_image_paths,
                        "targets": [redact_sensitive_text(target.label) for target in selected_targets],
                        "image_to_text": image_to_text_meta,
                    }
                    self._record_task(
                        {
                            **source_meta,
                            "success": False,
                            "error": error,
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
                    return {"success": False, "error": error, "request_data": request_data, "response_data": response_data}
        request_data = {
            "original_prompt": original_prompt,
            "request_prompt": request_prompt,
            "audit_prompt": audit_prompt_text,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "reference_image_count": len(refs),
            "request_image_paths": request_image_paths,
            "targets": [redact_sensitive_text(target.label) for target in selected_targets],
            "image_to_text": image_to_text_meta,
        }
        if prompt_en_meta:
            request_data["prompt_en"] = dict(prompt_en_meta)
            if prompt_en_meta.get("applied"):
                request_data["request_prompt_en"] = request_prompt
        request_data["composition"] = self._composition_metadata(
            request_prompt, source, aspect_ratio, resolution, len(refs)
        )
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

        # Optional: translate final image prompt to English for models weak on Chinese.
        if (
            (prompt_en_meta is None or image_to_text_meta.get("applied"))
            and self._prompt_en_needed(request_prompt, media="image")
        ):
            translated, en_meta = await self._translate_prompt_to_english(
                request_prompt, media="image", event=event
            )
            request_data["prompt_en"] = en_meta
            if en_meta.get("applied") and translated:
                request_prompt = translated
                request_data["request_prompt"] = request_prompt
                request_data["request_prompt_en"] = translated

        if not selected_targets:
            response_data = {
                "success": False,
                "stage": "select_model",
                "error": "当前没有可用的出图模型，请先在管理页启用渠道和模型。",
            }
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
            return {"success": False, "error": response_data["error"]}

        request = ImageGenerateRequest(
            prompt=request_prompt,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            images=refs,
            allow_compat_retry=allow_compat_retry,
            max_image_bytes=self.config.image_max_image_size_mb * 1024 * 1024,
        )

        def request_for_target(target: ImageModelTarget) -> ImageGenerateRequest:
            use_text_description = bool(
                (getattr(target, "extra", {}) or {}).get("image_to_text_enabled") and refs
            )
            return ImageGenerateRequest(
                prompt=request_prompt if use_text_description else plain_request_prompt,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                images=[] if use_text_description else refs,
                allow_compat_retry=allow_compat_retry,
                max_image_bytes=self.config.image_max_image_size_mb * 1024 * 1024,
            )

        started = time.monotonic()
        # trust_env=False: channel.proxy is explicit; do not inherit process HTTP(S)_PROXY
        # (common on ops hosts) and silently stall NewAPI image downloads/posts.
        async with self._semaphore:
            result = await generate_image_with_fallback(
                selected_targets,
                request,
                None,
                max_attempts=max_attempts,
                global_timeout=self.config.image_global_timeout,
                request_factory=request_for_target if image_to_text_targets else None,
            )
        elapsed = time.monotonic() - started
        self._record_channel_health(result.attempts)

        if not result.error and result.images:
            used_target = next(
                (
                    target
                    for target in selected_targets
                    if redact_sensitive_text(target.label) == result.used_model
                ),
                None,
            )
            used_image_to_text = bool(
                used_target
                and refs
                and (getattr(used_target, "extra", {}) or {}).get("image_to_text_enabled")
            )
            request_data["image_to_text"]["used_by_model"] = used_image_to_text
            if not used_image_to_text:
                request_prompt = plain_request_prompt
                request_data["request_prompt"] = plain_request_prompt
                request_data["composition"] = self._composition_metadata(
                    plain_request_prompt, source, aspect_ratio, resolution, len(refs)
                )

        if result.error or not result.images:
            response_data = {
                "success": False,
                "stage": "generate",
                "error": result.error or "未生成任何图片",
                "used_model": result.used_model,
                "elapsed_seconds": round(elapsed, 2),
                "generated_image_sources": result.source_media,
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
                    "generated_image_sources": result.source_media,
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

        generated_images = [image for image in result.images if image]
        generated_image_paths = [
            self._save_cache_image(image, "generated", detect_mime_by_bytes(image))
            for image in generated_images
        ]
        generated_image_md5s = [hashlib.md5(image).hexdigest() for image in generated_images]
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
                "generated_image_sources": result.source_media,
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
                    "generated_image_sources": result.source_media,
                    "md5s": generated_image_md5s,
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
            "generated_image_sources": result.source_media,
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
                "generated_image_sources": result.source_media,
                "md5s": generated_image_md5s,
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
            "generated_image_sources": result.source_media,
        }

    async def _build_selfie_prompt_and_refs(self, action: str, extra_refs: List[ImageReference], event: Optional[AstrMessageEvent] = None) -> Tuple[str, List[ImageReference]]:
        llm_generate = (lambda prompt: self._call_text_llm(event, prompt, timeout=6)) if event is not None else None
        await self.persona.ensure_daily_selfie_profile(action, llm_generate=llm_generate)
        persona_ref = self._persona_identity_reference()
        refs: List[ImageReference] = []
        if persona_ref:
            refs.append(persona_ref)
        refs.extend(self._persona_auxiliary_references(action))
        refs.extend(extra_refs)
        prompt = self.persona.build_selfie_prompt(
            action=action or "看着镜头自然自拍，展示你现在的样子",
            bot_name=self.config.bot_name,
            personality=self.config.personality,
            has_reference_image=bool(persona_ref),
            extra_reference_count=len(extra_refs),
        )
        return prompt, refs

    async def _build_selfie_prompt_and_refs_for_event(
        self,
        event: Optional[AstrMessageEvent],
        action: str,
        extra_refs: List[ImageReference],
    ) -> Tuple[str, List[ImageReference], Dict[str, Any]]:
        """Use the central English built-ins and translate only free-form user text."""
        prompt, refs = await self._build_selfie_prompt_and_refs(action, extra_refs, event=event)
        has_identity_reference = bool(self._persona_identity_reference())
        meta: Dict[str, Any] = {"enabled": False, "applied": False, "scope": "user_text_only"}
        if not self._prompt_en_needed(action, media="image"):
            return prompt, refs, meta
        from .prompts.prompt_templates import build_selfie_builtin_prompt, extract_user_prompt

        user_text = extract_user_prompt(action)
        if not user_text:
            english = build_selfie_builtin_prompt(
                action,
                language="en",
                has_reference_image=has_identity_reference,
                extra_reference_count=len(extra_refs),
                appearance_type=self.persona.get_appearance_type(),
            )
            meta.update({"enabled": True, "applied": True, "scope": "builtin_only"})
            return english, refs, meta
        translated, translation_meta = await self._translate_prompt_to_english(
            user_text,
            media="image",
            event=event,
        )
        meta.update(translation_meta)
        meta["scope"] = "user_text_only"
        if not translation_meta.get("applied"):
            return prompt, refs, meta
        english = build_selfie_builtin_prompt(
            action,
            language="en",
            has_reference_image=has_identity_reference,
            extra_reference_count=len(extra_refs),
            appearance_type=self.persona.get_appearance_type(),
            user_text=translated,
        )
        return english, refs, meta

    def get_selfie_reference_payload(self) -> Dict[str, Any]:
        data = self.persona.get()
        ref = self.persona.get_reference_image()
        appearance_type = self.persona.get_appearance_type()
        base = {
            "appearance_type": appearance_type,
            "appearance_type_label": self.persona.appearance_type_label(),
            "ref_mime_type": data.get("ref_mime_type") or "image/png",
            "updated_at": data.get("updated_at") or "",
            "status": self.persona.status_text(),
        }
        if not ref:
            return {
                **base,
                "has_image": False,
            }
        return {
            **base,
            "has_image": True,
            "ref_mime_type": ref["mime_type"],
            "image": bytes_to_data_url(ref["data"], ref["mime_type"]),
        }

    def set_selfie_appearance_type_from_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        value = payload.get("appearance_type", payload.get("type", "auto"))
        self.persona.set_appearance_type(value)
        return self.get_selfie_reference_payload()

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
        self.persona.save_reference_image(data, normalize_image_mime(mime or str(payload.get("mime_type") or "") or detect_mime_by_bytes(data)))
        if "appearance_type" in payload or "type" in payload:
            self.persona.set_appearance_type(payload.get("appearance_type", payload.get("type")))
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
        targets: List[ImageModelTarget] = []
        for channel in self.config.image_channels:
            targets.extend(
                channel.targets(self.config.image_global_timeout, request_timeout=LOCAL_IMAGE_WAIT_SECONDS)
            )
        return find_model_target(targets, channel_name, model)

    def _find_video_target(self, channel_name: str = "", model: str = "") -> Optional[ImageModelTarget]:
        return find_model_target(
            self.config.get_prioritized_video_targets(), channel_name, model
        )

    def _available_model_labels(self) -> List[str]:
        return available_model_labels(self.config.get_prioritized_targets())

    def _get_session_model_override(self, event: Optional[AstrMessageEvent] = None) -> str:
        if event is None:
            return ""
        key = self._session_key(event)
        with self._session_model_lock:
            return str(self._session_model_overrides.get(key) or "").strip()

    def _set_session_model_override(self, event: AstrMessageEvent, label: str) -> str:
        key = self._session_key(event)
        value = str(label or "").strip()
        with self._session_model_lock:
            if not value:
                self._session_model_overrides.pop(key, None)
                return ""
            self._session_model_overrides[key] = value
            # Bound memory for long-running bots.
            while len(self._session_model_overrides) > 200:
                self._session_model_overrides.pop(next(iter(self._session_model_overrides)))
        return value

    def _match_model_label(self, raw: str) -> Optional[str]:
        return match_model_label(raw, self._available_model_labels())

    def _resolve_generation_targets(
        self,
        event: Optional[AstrMessageEvent] = None,
        targets: Optional[List[ImageModelTarget]] = None,
    ) -> List[ImageModelTarget]:
        if targets is not None:
            return list(targets)
        all_targets = self.config.get_prioritized_targets()
        return prioritize_model_target(
            all_targets, self._get_session_model_override(event)
        )

    def _list_image_tasks_for_session(
        self,
        session_key: str = "",
        *,
        include_finished: bool = False,
        limit: int = 10,
        media_type: str = "",
    ) -> List[Dict[str, Any]]:
        with self._web_task_lock:
            items = list(self._web_tasks.values())
        rows = filter_image_tasks(
            items,
            session_key,
            include_finished=include_finished,
            limit=limit,
            media_type=media_type,
        )
        return [redact_sensitive_data(row) for row in rows]

    def _format_task_list_text(self, tasks: List[Dict[str, Any]]) -> str:
        return format_task_list_text(tasks)

    def _format_task_detail_text(self, task: Dict[str, Any]) -> str:
        return format_task_detail_text(task)

    @staticmethod
    def _task_media_type(task: Mapping[str, Any]) -> str:
        request = task.get("request_data") if isinstance(task.get("request_data"), dict) else {}
        value = str(task.get("media_type") or request.get("media_type") or "").strip().lower()
        if value in {"image", "video"}:
            return value
        kind = str(request.get("kind") or "").strip().lower()
        source = str(task.get("source") or "").strip().lower()
        return "video" if kind == "video" or "视频" in kind or "video" in source else "image"

    async def _command_task_list(
        self,
        event: AstrMessageEvent,
        command_name: str,
        fallback: str,
        *,
        media_type: str = "",
    ) -> AsyncGenerator[Any, None]:
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        message = extract_command_message(event, command_name, fallback).strip()
        session_key = self._session_key(event)
        is_admin = self._is_admin_event(event)
        if message:
            try:
                task = self.get_web_image_task(message)
            except Exception:
                active = self._list_image_tasks_for_session(
                    session_key, include_finished=False, limit=20, media_type=media_type
                )
                if message.isdigit():
                    index = int(message) - 1
                    if 0 <= index < len(active):
                        task = active[index]
                    else:
                        yield event.plain_result("没找到这个进行中的编号，或任务号不对。")
                        return
                else:
                    yield event.plain_result("没有这单，或已经清理了。")
                    return
            if media_type and self._task_media_type(task) != media_type:
                yield event.plain_result("这个任务不属于视频任务。" if media_type == "video" else "这个任务不属于生图任务。")
                return
            owner = str(task.get("owner_session") or "")
            if owner and owner != session_key and not is_admin:
                yield event.plain_result("不能看别人会话里的任务。")
                return
            if task.get("status") in {"queued", "running"}:
                try:
                    task = self.get_web_image_task(str(task.get("task_id") or message))
                except Exception:
                    pass
            yield event.plain_result(self._format_task_detail_text(task))
            return
        tasks = self._list_image_tasks_for_session(
            session_key, include_finished=False, limit=10, media_type=media_type
        )
        yield event.plain_result(
            "现在没有进行中的视频任务。"
            if media_type == "video" and not tasks
            else self._format_task_list_text(tasks)
        )

    async def _command_task_cancel(
        self,
        event: AstrMessageEvent,
        command_name: str,
        fallback: str,
        *,
        media_type: str = "",
    ) -> AsyncGenerator[Any, None]:
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        message = extract_command_message(event, command_name, fallback).strip()
        session_key = self._session_key(event)
        is_admin = self._is_admin_event(event)
        if not message:
            active = self._list_image_tasks_for_session(
                session_key, include_finished=False, limit=5, media_type=media_type
            )
            if active:
                yield event.plain_result("请跟任务号或列表里的编号。\n" + self._format_task_list_text(active))
            else:
                yield event.plain_result("现在没有可取消的视频任务。" if media_type == "video" else "现在没有可取消的出图。")
            return
        task_id = message
        if message.isdigit():
            active = self._list_image_tasks_for_session(
                session_key, include_finished=False, limit=20, media_type=media_type
            )
            index = int(message) - 1
            if 0 <= index < len(active):
                task_id = str(active[index].get("task_id") or "")
            else:
                yield event.plain_result("未找到对应的进行中任务，请检查编号或任务ID。")
                return
        elif media_type:
            try:
                task = self.get_web_image_task(task_id)
            except Exception:
                task = None
            if task and self._task_media_type(task) != media_type:
                yield event.plain_result("这个任务不属于视频任务。" if media_type == "video" else "这个任务不属于生图任务。")
                return
        try:
            text = self.cancel_image_task(task_id, session_key=session_key, is_admin=is_admin)
            yield event.plain_result(text)
        except PermissionError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            yield event.plain_result(redact_sensitive_text(str(exc)))

    def cancel_image_task(
        self,
        task_id: str,
        *,
        session_key: str = "",
        is_admin: bool = False,
    ) -> str:
        tid = str(task_id or "").strip()
        if not tid:
            raise ValueError("请提供任务ID")
        with self._web_task_lock:
            task = self._web_tasks.get(tid)
            if not task:
                # allow short numeric index against recent active? handled by caller
                raise ValueError("任务不存在或已清理")
            owner = str(task.get("owner_session") or "")
            if owner and session_key and owner != session_key and not is_admin:
                raise PermissionError("不能取消其他会话的生图任务")
            status = str(task.get("status") or "")
            if status in {"succeeded", "failed", "cancelled"}:
                return f"这单已经结束了（{status}），不用再取消"
            task["cancel_requested"] = True
            now = time.time()
            task["status"] = "cancelled"
            task["success"] = False
            task["error"] = "任务已取消"
            task["updated_ts"] = now
            task["updated_at"] = self._web_task_timestamp()
            task["finished_ts"] = now
            task["finished_at"] = self._web_task_timestamp()
            self._persist_web_tasks_locked()
            runtime_task = getattr(self, "_runtime_generation_tasks", {}).get(tid)
            if runtime_task is not None and not runtime_task.done():
                runtime_task.cancel()
            return f"已立即取消 {tid}"

    def _task_cancel_requested(self, task_id: str) -> bool:
        with self._web_task_lock:
            task = self._web_tasks.get(task_id)
            return bool(task and task.get("cancel_requested"))

    def start_command_image_task(
        self,
        event: AstrMessageEvent,
        *,
        source: str,
        summary: Dict[str, Any],
        runner,
    ) -> Dict[str, Any]:
        """Queue a chat-side generation job and return immediately (targets 08/13)."""
        loop = getattr(self, "loop", None) or asyncio.get_running_loop()
        session_key = self._session_key(event)
        request_summary = dict(summary or {})
        force_regenerate = bool(request_summary.get("force_regenerate") or request_summary.get("force"))
        fingerprint = self._request_fingerprint(request_summary, session_key)
        with self._web_task_lock:
            if not force_regenerate:
                duplicate = self._find_recent_duplicate_task_locked(fingerprint)
                if duplicate and duplicate.get("owner_session") == session_key:
                    duplicate["deduplicated"] = True
                    return redact_sensitive_data(duplicate)
            self._web_task_seq += 1
            task_id = f"cmd-{int(time.time() * 1000)}-{self._web_task_seq}"
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
                "request_data": redact_sensitive_data(request_summary),
                "result": None,
                "source": source,
                "owner_session": session_key,
                "owner_user_id": event_user_id(event),
                "cancel_requested": False,
                "request_fingerprint": fingerprint,
                "deduplicated": False,
            }
            self._prune_web_tasks_locked()
            self._persist_web_tasks_locked()
        runtime_task = asyncio.create_task(self._run_command_image_task(task_id, event, runner))
        runtime_tasks = getattr(self, "_runtime_generation_tasks", None)
        if runtime_tasks is None:
            runtime_tasks = {}
            self._runtime_generation_tasks = runtime_tasks
        runtime_tasks[task_id] = runtime_task
        runtime_task.add_done_callback(
            lambda _task, tid=task_id: getattr(self, "_runtime_generation_tasks", {}).pop(tid, None)
        )
        return self.get_web_image_task(task_id)

    async def _run_video_generation(
        self,
        event: AstrMessageEvent,
        prompt: str,
        refs: List[ImageReference],
        *,
        source: str = "command-video",
        duration: Optional[int] = None,
    ) -> Dict[str, Any]:
        targets = list(self.config.get_prioritized_video_targets())
        if not getattr(self.config, "video_enable", True):
            return {"success": False, "error": "视频功能已关闭，请在配置里打开 video.enable"}
        if not targets:
            return {"success": False, "error": "还没有可用的视频渠道，请先在配置里添加并启用 video_channels"}
        # Skip malformed targets without blocking later configured video channels.
        valid_targets: List[ImageModelTarget] = []
        invalid_messages: List[str] = []
        for candidate in targets:
            report = preflight_video_channel(
                {
                    "name": candidate.channel_name,
                    "base_url": candidate.base_url,
                    "api_key": candidate.api_key,
                    "api_keys": candidate.api_keys,
                    "model": candidate.model,
                    "enabled_models": [candidate.model] if candidate.model else [],
                    "enabled": True,
                }
            )
            if report.get("ok"):
                valid_targets.append(candidate)
            else:
                invalid_messages.append(str(report.get("message") or candidate.label))
        if not valid_targets:
            return {"success": False, "error": invalid_messages[0] if invalid_messages else "视频渠道配置不完整"}
        targets = valid_targets

        video_prompt = str(prompt or "").strip()
        prompt_en_meta = {}
        if self._prompt_en_needed(video_prompt, media="video"):
            translated, prompt_en_meta = await self._translate_prompt_to_english(
                video_prompt, media="video", event=event
            )
            if prompt_en_meta.get("applied") and translated:
                video_prompt = translated

        req = VideoGenerateRequest(
            prompt=video_prompt,
            images=list(refs or [])[:1],  # I2V: first frame only (big_banana style)
            duration=int(duration if duration is not None else getattr(self.config, "video_default_duration", 5) or 5),
        )
        if not req.prompt:
            return {"success": False, "error": "请写一下想生成的视频内容"}

        async with self._video_semaphore:
            async with aiohttp.ClientSession(trust_env=False) as session:
                result = await generate_video_with_fallback(
                    targets,
                    req,
                    session,
                    save_dir=self.video_dir,
                )
        if result.error or not result.video_path:
            self._record_task(
                {
                    **self._source_context(event, source),
                    "media_type": "video",
                    "success": False,
                    "error": result.error or "视频没有生成出来",
                    "prompt": req.prompt,
                    "original_prompt": prompt,
                    "request_prompt": req.prompt,
                    "used_model": result.used_model,
                    "elapsed_seconds": result.elapsed_seconds,
                    "reference_images": len(refs),
                    "request_data": {
                        "duration": req.duration,
                        "size": req.size,
                        "reference_images": len(refs),
                        "prompt_en": prompt_en_meta,
                        "request_prompt_en": req.prompt if prompt_en_meta.get("applied") else "",
                    },
                    "response_data": {
                        "attempts": result.attempts,
                        "video_url": result.video_url,
                        "video_source": result.video_source,
                    },
                    "request_image_paths": [],
                    "generated_image_paths": [],
                    "generated_video_paths": [],
                }
            )
            return {
                "success": False,
                "error": result.error or "视频没有生成出来",
                "used_model": result.used_model,
                "attempts": result.attempts,
                "elapsed_seconds": result.elapsed_seconds,
            }
        video_rel = self._cache_relative_path(result.video_path)
        self._record_task(
            {
                **self._source_context(event, source),
                "media_type": "video",
                "success": True,
                "error": "",
                "prompt": req.prompt,
                "original_prompt": prompt,
                "request_prompt": req.prompt,
                "used_model": result.used_model,
                "elapsed_seconds": result.elapsed_seconds,
                "reference_images": len(refs),
                "request_data": {
                    "duration": req.duration,
                    "size": req.size,
                    "reference_images": len(refs),
                    "prompt_en": prompt_en_meta,
                    "request_prompt_en": req.prompt if prompt_en_meta.get("applied") else "",
                },
                "response_data": {
                    "attempts": result.attempts,
                    "video_url": result.video_url,
                    "video_source": result.video_source,
                },
                "request_image_paths": [],
                "generated_image_paths": [],
                "generated_video_paths": [video_rel],
            }
        )
        return {
            "success": True,
            "video_path": result.video_path,
            "video_url": result.video_url,
            "video_source": result.video_source,
            "used_model": result.used_model,
            "attempts": result.attempts,
            "elapsed_seconds": result.elapsed_seconds,
            "files": [result.video_path],
        }

    async def _background_video_job(
        self,
        task_id: str,
        event: AstrMessageEvent,
        prompt: str,
        refs: List[ImageReference],
        source: str,
        mode: str,
    ) -> Dict[str, Any]:
        if self._task_cancel_requested(task_id):
            return {"success": False, "error": "任务已取消", "cancelled": True}
        result = await self._run_video_generation(event, prompt, refs, source=source)
        if self._task_cancel_requested(task_id) and not result.get("success"):
            return {"success": False, "error": "任务已取消", "cancelled": True}
        if not result.get("success"):
            error = self._friendly_user_error_message(str(result.get("error") or ""), "视频没有完成")
            try:
                await event.send(event.plain_result(error))
            except Exception:
                pass
            return result
        path = str(result.get("video_path") or "")
        used = str(result.get("used_model") or "")
        elapsed = result.get("elapsed_seconds") or 0
        bits = ["视频好了。"]
        if self.config.image_show_generation_info and elapsed:
            bits.append(f"用时 {elapsed}s")
        if self.config.image_show_model_info and used:
            bits.append(f"模型 {used}")
        caption = " ".join(bits)
        try:
            await self._send_generated_video(event, path, caption=caption)
        except Exception as exc:
            logger.warning(f"[SelfieImage] 发送视频失败，尝试仅回路径: {exc}")
            try:
                await event.send(event.plain_result(f"{caption}\n文件：{path}"))
            except Exception:
                pass
            return {"success": False, "error": f"视频已生成但发送失败：{exc}", "video_path": path}
        return result

    def _parse_video_duration(self, text: str) -> Tuple[str, Optional[int]]:
        raw = str(text or "")
        duration = None
        match = re.search(r"(?:--duration|--dur|-d)(?:\s*=\s*|\s+)(\d{1,2})\s*s?\b", raw, flags=re.I)
        if match:
            duration = max(1, min(60, int(match.group(1))))
            raw = (raw[: match.start()] + raw[match.end() :]).strip()
        if duration is None:
            suffix = re.search(r"(?:^|\s)(?:时长|长度)\s*(\d{1,2})\s*秒?\b", raw, flags=re.I)
            if suffix:
                duration = max(1, min(60, int(suffix.group(1))))
                raw = (raw[: suffix.start()] + raw[suffix.end() :]).strip()
        if duration is None:
            bare = re.search(r"(?:^|\s)(\d{1,2})\s*秒(?:\s|$)", raw, flags=re.I)
            if bare:
                duration = max(1, min(60, int(bare.group(1))))
                raw = (raw[: bare.start()] + raw[bare.end() :]).strip()
        return raw, duration

    @staticmethod
    def _video_prompt_requests_persona(text: str) -> bool:
        """Return whether a video request explicitly asks to use the current persona."""
        raw = str(text or "").strip().lower()
        if not raw:
            return False
        compact = re.sub(r"[\s,，。.!！?？、;；:：'\"“”‘’()（）【】\[\]]+", "", raw)
        if any(
            token in compact
            for token in (
                "不要形象图",
                "不用形象图",
                "不使用形象图",
                "不带形象图",
                "不要用当前形象",
                "不用当前形象",
                "不使用当前形象",
                "不要带当前形象",
                "不要使用当前形象",
                "不要用我的形象",
                "不用我的形象",
                "不使用我的形象",
                "纯文字生成",
                "纯文生视频",
                "不要首帧",
                "不需要首帧",
            )
        ):
            return False
        return any(
            token in compact
            for token in (
                "我出镜",
                "自己出镜",
                "本人出镜",
                "我来出镜",
                "我的形象",
                "当前形象",
                "用我的形象",
                "用当前形象",
                "使用我的形象",
                "使用当前形象",
                "形象图",
                "我的脸",
                "保持我的脸",
                "保持脸部身份",
                "自拍视频",
                "自拍",
                "ai出镜",
                "ai自己",
                "让你出镜",
                "让你跳",
                "你出镜",
                "你跳舞",
                "你来跳",
            )
        )

    def _configured_video_persona_reference(self) -> Optional[ImageReference]:
        """Use only a configured persona image; never substitute the plugin logo."""
        if not self.persona.has_reference_image():
            return None
        return self._video_persona_reference()

    def _expand_video_prompt_with_preset(
        self, raw_prompt: str, duration: Optional[int] = None
    ) -> Tuple[str, Optional[int], str]:
        text = str(raw_prompt or "").strip()
        if not text:
            return "", duration, ""
        try:
            self.video_presets.load()
            resolved = self.video_presets.resolve(text)
        except Exception:
            return text, duration, ""
        preset_name = str(resolved.get("preset_name") or "").strip()
        prompt = str(resolved.get("prompt") or text).strip()
        preset_duration = self.video_presets._parse_duration(resolved.get("duration"))
        return prompt, duration if duration is not None else (preset_duration or None), preset_name

    async def _handle_video_command(
        self,
        event: AstrMessageEvent,
        command_name: Any,
        fallback: str,
        *,
        mode: str = "auto",
    ) -> AsyncGenerator[Any, None]:
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        raw_message = extract_command_message(event, command_name, fallback).strip()
        prompt, duration = self._parse_video_duration(raw_message)
        prompt, duration, _ = self._expand_video_prompt_with_preset(prompt, duration)
        if not prompt:
            label = "形象图生视频" if mode == "persona" else ("图生视频" if mode == "i2v" else "文生视频")
            yield event.plain_result(f"请写上{label}的内容，例如：/{command_name if isinstance(command_name, str) else '视频'} 小猫在草地上跑")
            return

        refs: List[ImageReference] = []
        event_refs: List[ImageReference] = []
        use_persona_reference = mode == "persona"
        if mode != "t2v" and mode != "persona":
            event_refs = await self._event_reference_images(
                event,
                include_at_avatar=False,
                allow_context_fallback=False,
                include_persona=False,
            )
        if mode == "i2v":
            refs = event_refs
            if not refs:
                yield event.plain_result("图生视频需要附图或引用图片；如要使用当前形象图，请使用 /形象视频。")
                return
        elif mode == "persona":
            persona_ref = self._configured_video_persona_reference()
            if not persona_ref:
                yield event.plain_result("形象视频需要先使用 /形象设置 上传当前形象图。")
                return
            refs = [persona_ref]
        elif mode == "auto":
            if event_refs:
                refs = event_refs
            elif self._video_prompt_requests_persona(prompt):
                persona_ref = self._configured_video_persona_reference()
                if not persona_ref:
                    yield event.plain_result("这段视频要求使用当前形象图，请先使用 /形象设置 上传形象图，或改用 /文生视频。")
                    return
                refs = [persona_ref]
                use_persona_reference = True

        if use_persona_reference:
            mode_label = "形象图生视频"
        elif mode == "auto" and refs:
            mode_label = "图生视频"
        elif mode == "i2v":
            mode_label = "图生视频"
        else:
            mode_label = "文生视频"
            refs = []

        progress = f"收到，开始{mode_label}（通常比出图慢，请稍等）。"
        if duration:
            progress += f" 时长约 {duration}s。"

        async def runner_with_duration(task_id: str) -> Dict[str, Any]:
            if self._task_cancel_requested(task_id):
                return {"success": False, "error": "任务已取消", "cancelled": True}
            result = await self._run_video_generation(
                event,
                prompt,
                refs,
                source=f"command-{mode_label}",
                duration=duration,
            )
            if not result.get("success"):
                error = self._friendly_user_error_message(str(result.get("error") or ""), "视频没有完成")
                try:
                    await event.send(event.plain_result(error))
                except Exception:
                    pass
                return result
            path = str(result.get("video_path") or "")
            used = str(result.get("used_model") or "")
            elapsed = result.get("elapsed_seconds") or 0
            bits = ["视频好了。"]
            if self.config.image_show_generation_info and elapsed:
                bits.append(f"用时 {elapsed}s")
            if self.config.image_show_model_info and used:
                bits.append(f"模型 {used}")
            try:
                await self._send_generated_video(event, path, caption=" ".join(bits))
            except Exception as exc:
                try:
                    await event.send(event.plain_result(f"{' '.join(bits)}\n文件：{path}"))
                except Exception:
                    pass
                return {"success": False, "error": f"视频已生成但发送失败：{exc}", "video_path": path}
            return result

        task = self.start_command_image_task(
            event,
            source=f"command-{mode_label}",
            summary={
                "prompt": prompt,
                "mode": mode_label,
                "duration": duration or getattr(self.config, "video_default_duration", 5),
                "has_image": bool(refs),
                "kind": "video",
            },
            runner=runner_with_duration,
        )
        yield event.plain_result(progress)

    @filter.command("视频")
    async def cmd_video(self, event: AstrMessageEvent, p1: str = "", p2: str = "", p3: str = "") -> AsyncGenerator[Any, None]:
        """写想要的动态出视频；有附图/引用图时作首帧，没图默认按文字生成，明确本人出镜时才使用当前形象图。"""
        fallback = " ".join(item for item in [p1, p2, p3] if item).strip()
        async for item in self._handle_video_command(event, "视频", fallback, mode="auto"):
            yield item

    @filter.command("文生视频")
    async def cmd_t2v(self, event: AstrMessageEvent, p1: str = "", p2: str = "", p3: str = "") -> AsyncGenerator[Any, None]:
        """只用文字出视频，不带图、也不使用形象图。"""
        fallback = " ".join(item for item in [p1, p2, p3] if item).strip()
        async for item in self._handle_video_command(event, "文生视频", fallback, mode="t2v"):
            yield item

    @filter.command("图生视频")
    async def cmd_i2v(self, event: AstrMessageEvent, p1: str = "", p2: str = "", p3: str = "") -> AsyncGenerator[Any, None]:
        """按附图或引用图出视频；没有图片时不会自动使用当前形象图。"""
        fallback = " ".join(item for item in [p1, p2, p3] if item).strip()
        async for item in self._handle_video_command(event, "图生视频", fallback, mode="i2v"):
            yield item

    @filter.command("形象视频")
    async def cmd_persona_video(self, event: AstrMessageEvent, p1: str = "", p2: str = "", p3: str = "") -> AsyncGenerator[Any, None]:
        """使用当前形象图作为首帧出视频；需要先设置形象图。"""
        fallback = " ".join(item for item in [p1, p2, p3] if item).strip()
        async for item in self._handle_video_command(event, "形象视频", fallback, mode="persona"):
            yield item

    @filter.command("看看视频")
    async def cmd_look_video(self, event: AstrMessageEvent, p1: str = "", p2: str = "", p3: str = "") -> AsyncGenerator[Any, None]:
        """使用当前形象图主动出视频；需要先设置形象图。"""
        fallback = " ".join(item for item in [p1, p2, p3] if item).strip()
        async for item in self._handle_video_command(event, "看看视频", fallback, mode="persona"):
            yield item

    async def _run_command_image_task(self, task_id: str, event: AstrMessageEvent, runner) -> None:
        if self._task_cancel_requested(task_id):
            self._set_web_image_task(
                task_id,
                status="cancelled",
                success=False,
                error="任务已取消",
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
            return
        self._set_web_image_task(
            task_id,
            status="running",
            started_ts=time.time(),
            started_at=self._web_task_timestamp(),
        )
        try:
            if self._task_cancel_requested(task_id):
                raise RuntimeError("任务已取消")
            result = await runner(task_id)
            result = self._normalize_generation_result(result)
            result = redact_sensitive_data(result)
            if self._task_cancel_requested(task_id) and not result.get("success"):
                result = {"success": False, "error": "任务已取消", "cancelled": True}
            success = bool(result.get("success"))
            cancelled = bool(result.get("cancelled")) or str(result.get("error") or "").find("取消") >= 0
            error = "" if success else redact_sensitive_text(str(result.get("error") or ("任务已取消" if cancelled else "这次没顺好")))
            self._set_web_image_task(
                task_id,
                status="cancelled" if cancelled and not success else str(result.get("status") or ("succeeded" if success else "failed")),
                success=success,
                error=error,
                requested_count=result.get("requested_count", 1),
                succeeded_count=result.get("succeeded_count", 0),
                failed_count=result.get("failed_count", 0),
                result=result,
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
        except asyncio.CancelledError:
            self._set_web_image_task(
                task_id,
                status="cancelled",
                success=False,
                error="任务已取消",
                result={"success": False, "error": "任务已取消", "cancelled": True},
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
            return
        except Exception as exc:
            error = redact_sensitive_text(str(exc))
            cancelled = "取消" in error
            self._set_web_image_task(
                task_id,
                status="cancelled" if cancelled else "failed",
                success=False,
                error=error,
                result={"success": False, "error": error},
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
            # Ensure monitor still gets a row when runner crashes before generate_images records.
            try:
                task_meta = {}
                try:
                    with self._web_task_lock:
                        task_meta = dict((self._web_tasks or {}).get(task_id) or {})
                except Exception:
                    task_meta = {}
                source = str(task_meta.get("source") or "command-task")
                prompt = ""
                summary = task_meta.get("request_data") or task_meta.get("summary") or {}
                if isinstance(summary, dict):
                    prompt = str(summary.get("prompt") or summary.get("action") or "")
                self._record_task(
                    {
                        "source": source,
                        "source_label": source,
                        "success": False,
                        "error": error,
                        "prompt": prompt,
                        "original_prompt": prompt,
                        "request_prompt": prompt,
                        "used_model": "",
                        "elapsed_seconds": 0,
                        "reference_images": 0,
                        "request_data": {"stage": "task_exception"},
                        "response_data": {"success": False, "stage": "task_exception", "error": error},
                        "request_image_paths": [],
                        "generated_image_paths": [],
                        "attempts": [],
                    }
                )
            except Exception as rec_exc:
                logger.warning(f"[SelfieImage] 任务异常落库失败: {rec_exc}")
            try:
                await event.send(event.plain_result(self._friendly_user_error_message(error, "生图没有完成")))
            except Exception as send_exc:
                logger.warning(f"[SelfieImage] 后台任务失败通知发送失败: {send_exc}")



    def _image_inflight_limit(self) -> int:
        return max(1, min(10, int(getattr(self.config, "image_max_concurrent_tasks", 1) or 1)))

    def _ensure_image_batch_gate(self) -> asyncio.Semaphore:
        gate = getattr(self, "_image_batch_gate", None)
        if gate is None or not isinstance(gate, asyncio.Semaphore):
            self._image_batch_gate = asyncio.Semaphore(self._image_inflight_limit())
            gate = self._image_batch_gate
        self._selfie_batch_gate = gate
        return gate

    def _image_batch_queue_expected(self, total: int = 1) -> bool:
        """Whether the first shot must wait for a currently occupied slot.

        ``total`` is intentionally not used here.  A batch of ten is valid
        with concurrency three and must not be labelled queued merely because
        ten is larger than the configured concurrency.
        """
        gate = self._ensure_image_batch_gate()
        return int(getattr(gate, "_value", 0)) <= 0

    async def _acquire_image_slot(self, task_id: str) -> Optional[asyncio.Semaphore]:
        """Acquire one slot; a batch may be larger than the global limit."""
        gate = self._ensure_image_batch_gate()
        while True:
            if self._task_cancel_requested(task_id):
                return None
            try:
                await asyncio.wait_for(gate.acquire(), timeout=1.0)
                return gate
            except asyncio.TimeoutError:
                continue

    def _ensure_image_batch_cooldown_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_image_batch_cooldown_lock", None)
        if lock is None or not isinstance(lock, asyncio.Lock):
            self._image_batch_cooldown_lock = asyncio.Lock()
            lock = self._image_batch_cooldown_lock
        return lock

    async def _wait_for_image_batch_cooldown(self, task_id: str) -> bool:
        """Stagger actual upstream starts, including shots released from the queue."""
        lock = self._ensure_image_batch_cooldown_lock()
        async with lock:
            if self._task_cancel_requested(task_id):
                return False
            await asyncio.sleep(IMAGE_BATCH_REQUEST_COOLDOWN_SECONDS)
            return not self._task_cancel_requested(task_id)

    async def _run_counted_generation_shots(
        self,
        *,
        task_id: str,
        event: AstrMessageEvent,
        total: int,
        fail_label: str,
        run_one,
        log_prefix: str,
    ) -> Dict[str, Any]:
        """Queue-friendly batch: at most inflight shots generating; send as each finishes."""
        total = max(1, int(total))
        inflight = min(self._image_inflight_limit(), total)
        sem = asyncio.Semaphore(inflight)
        send_lock = asyncio.Lock()
        stop = False
        skipped_shots = 0
        all_files: List[str] = []
        used_model = ""
        last_elapsed = 0.0
        failed_at = 0
        last_failure_error = ""
        cancelled = False
        succeeded_shots = 0

        async def one(index: int) -> None:
            nonlocal stop, skipped_shots, used_model, last_elapsed, failed_at, cancelled, succeeded_shots, last_failure_error
            async with sem:
                if stop or self._task_cancel_requested(task_id):
                    if self._task_cancel_requested(task_id):
                        cancelled = True
                    return
                logger.info(f"[SelfieImage] {log_prefix} {index + 1}/{total} inflight={inflight} task={task_id}")
                slot_gate = await self._acquire_image_slot(task_id)
                if slot_gate is None:
                    cancelled = True
                    return
                try:
                    if total > 1 and not await self._wait_for_image_batch_cooldown(task_id):
                        cancelled = True
                        return
                    result = await run_one(index)
                finally:
                    slot_gate.release()
            async with send_lock:
                if stop:
                    return
                if self._task_cancel_requested(task_id):
                    cancelled = True
                    stop = True
                    return
                if not result.get("success"):
                    raw_err = str(result.get("failure_reason") or result.get("error") or "")
                    if not raw_err:
                        attempts = result.get("attempts") or []
                        if isinstance(attempts, list) and attempts:
                            last = attempts[-1] if isinstance(attempts[-1], dict) else {}
                            raw_err = str(last.get("error_user_message") or last.get("error") or "")
                    last_failure_error = raw_err
                    error = self._friendly_user_error_message(raw_err, fail_label)
                    skipped_shots += 1
                    mode, skip_max = self._batch_failure_policy()
                    has_remaining = index < total
                    will_continue = has_remaining and (
                        mode == "skip" or (mode == "skip_max" and skipped_shots <= skip_max)
                    )
                    msg = self._batch_shot_fail_text(
                        index=index + 1,
                        total=total,
                        done_files=len(all_files),
                        error=error,
                        mode=mode,
                        skipped=skipped_shots,
                        skip_max=skip_max,
                        will_continue=will_continue,
                    )
                    try:
                        await event.send(event.plain_result(msg))
                    except Exception:
                        pass
                    if not will_continue:
                        stop = True
                        failed_at = index + 1
                    return
                files = list(result.get("files") or [])
                used_model = str(result.get("used_model") or used_model)
                last_elapsed = float(result.get("elapsed_seconds") or last_elapsed)
                if files:
                    self._record_generated_images(event, 1)
                    await self._send_generated_images(event, files)
                    all_files.extend(files)
                    succeeded_shots += 1
                info = self._batch_success_text(
                    self._build_success_text(last_elapsed, len(files), used_model, event),
                    index + 1,
                    total,
                )
                if info:
                    try:
                        await event.send(event.plain_result(info))
                    except Exception:
                        pass

        await asyncio.gather(*(one(i) for i in range(total)))
        if cancelled:
            return self._normalize_generation_result(
                {
                    "success": False,
                    "error": "任务已取消",
                    "cancelled": True,
                    "files": all_files,
                    "batch_total": total,
                    "succeeded_count": succeeded_shots,
                    "failed_count": skipped_shots,
                },
                total,
            )
        if failed_at:
            return self._normalize_generation_result({
                "success": False,
                "error": last_failure_error or fail_label or "生图没有完成",
                "files": all_files,
                "batch_total": total,
                "batch_failed_at": failed_at,
                "batch_skipped": skipped_shots,
                "succeeded_count": succeeded_shots,
                "failed_count": skipped_shots,
            }, total)
        return self._normalize_generation_result({
            "success": skipped_shots == 0,
            "files": all_files,
            "used_model": used_model,
            "elapsed_seconds": last_elapsed,
            "batch_total": total,
            "batch_skipped": skipped_shots,
            "succeeded_count": succeeded_shots,
            "failed_count": skipped_shots,
        }, total)

    def _batch_failure_policy(self) -> tuple[str, int]:
        return batch_failure_policy(self.config)

    def _normalize_generation_result(self, result: Any, requested_count: int = 1) -> Dict[str, Any]:
        return normalize_generation_result(result, requested_count)

    def _batch_shot_fail_text(
        self,
        *,
        index: int,
        total: int,
        done_files: int,
        error: str,
        mode: str,
        skipped: int,
        skip_max: int,
        will_continue: bool,
    ) -> str:
        return batch_failure_text(
            index=index,
            total=total,
            done_files=done_files,
            error=error,
            mode=mode,
            skipped=skipped,
            skip_max=skip_max,
            will_continue=will_continue,
        )

    async def _batch_shot_fail_message(
        self,
        event: AstrMessageEvent,
        *,
        index: int,
        total: int,
        done_files: int,
        error: str,
        will_continue: bool,
    ) -> str:
        """Prefer a soft LLM sentence while keeping a deterministic fallback."""
        from .prompts.prompt_templates import build_batch_failure_llm_prompt

        reason = re.sub(r"\s+", " ", str(error or "").strip())[:160]
        llm_prompt = build_batch_failure_llm_prompt(
            bot_name=self._bot_display_name(),
            reason=reason,
            index=index,
            total=total,
            done_files=done_files,
            will_continue=will_continue,
        )
        reply = self._strip_llm_short_reply(await self._call_text_llm(event, llm_prompt, timeout=6))
        if reply and len(reply) <= 90 and "可能" not in reply:
            return reply
        return self._batch_shot_fail_text(
            index=index,
            total=total,
            done_files=done_files,
            error=reason,
            mode="skip" if will_continue else "stop",
            skipped=0,
            skip_max=0,
            will_continue=will_continue,
        )

    async def _background_draw_batches(
        self,
        task_id: str,
        event: AstrMessageEvent,
        prompt: str,
        aspect: str,
        resolution: str,
        refs: List[ImageReference],
        source: str,
        requested_count: int,
        *,
        passthrough: bool = False,
        fail_label: str = "",
    ) -> Dict[str, Any]:
        total = self._normalize_count(requested_count)

        async def run_one(index: int) -> Dict[str, Any]:
            if passthrough:
                return await self._draw_passthrough_once(event, prompt, aspect, resolution, refs, source)
            return await self._draw_once(event, prompt, aspect, resolution, refs, source)

        return await self._run_counted_generation_shots(
            task_id=task_id,
            event=event,
            total=total,
            fail_label=fail_label or self._natural_fail_fallback("image"),
            run_one=run_one,
            log_prefix="draw batch",
        )

    async def _background_selfie_batches(
        self,
        task_id: str,
        event: AstrMessageEvent,
        action: str,
        extra_refs: List[ImageReference],
        source: str,
        requested_count: int,
        aspect: str,
        resolution: str,
        fail_label: str,
        *,
        queue_notified: bool = False,
        rebuild_extra_request: str = "",
        rebuild_match_query: str = "",
    ) -> Dict[str, Any]:
        total = self._normalize_count(requested_count)
        self._ensure_image_batch_gate()
        return await self._run_selfie_batches_unlocked(
            task_id,
            event,
            action,
            extra_refs,
            source,
            total,
            aspect,
            resolution,
            fail_label,
            rebuild_extra_request,
            rebuild_match_query,
        )

    async def _run_selfie_batches_unlocked(
        self,
        task_id: str,
        event: AstrMessageEvent,
        action: str,
        extra_refs: List[ImageReference],
        source: str,
        requested_count: int,
        aspect: str,
        resolution: str,
        fail_label: str,
        rebuild_extra_request: str = "",
        rebuild_match_query: str = "",
    ) -> Dict[str, Any]:
        total = self._normalize_count(requested_count)
        # 多张拍摄时逐张更换机位或姿势。
        rebuild_each = source in {
            "command-look-legs",
            "command-selfie",
            "command-look-you",
            "command-look-cos",
        } or (
            "看看腿" in str(action or "")
            or "【legs:outfit】" in str(action or "")
            or "看看COS" in str(action or "")
            or "【shot:" in str(action or "")
            or "【pose:" in str(action or "")
            or "【cos:" in str(action or "")
        )
        last_pose = ""
        last_shot = ""
        last_cos = ""
        last_cam = ""
        extra_keep = ""
        force_legwear = ""
        if rebuild_each:
            # Extra may contain full preset text with many periods — take rest of line, then strip pose/shot tags.
            m_extra = re.search(r"(?:用户补充要求优先|额外要求)[:：]\s*(.+)", str(action or ""), flags=re.S)
            if m_extra:
                extra_keep = str(m_extra.group(1) or "").strip()
                extra_keep = re.sub(r"\s*【(?:pose|shot|cos|cam|legs|wear):[a-z0-9_]+】\s*", " ", extra_keep)
                extra_keep = re.sub(r"\s+", " ", extra_keep).strip(" 。")
            # Keep the original command query even when it is already present
            # in the selected outfit title/prompt and therefore has no user
            # supplement marker in the wrapped action.
            if rebuild_extra_request:
                extra_keep = str(rebuild_extra_request).strip()
            # Keep user/locked legwear across rebuild rounds (extra text alone may have stripped 白丝).
            force_legwear = parse_requested_legwear(str(action or "")) or parse_requested_legwear(extra_keep)
            m_pose = re.search(r"【pose:([a-z_]+)】", str(action or ""))
            if m_pose:
                last_pose = str(m_pose.group(1) or "")
            m_shot = re.search(r"【shot:([a-z_]+)】", str(action or ""))
            if m_shot:
                last_shot = str(m_shot.group(1) or "")
            m_cos = re.search(r"【cos:([a-z0-9_]+)】", str(action or ""))
            if m_cos:
                last_cos = str(m_cos.group(1) or "")
            m_cam = re.search(r"【cam:(selfie|third)】", str(action or ""))
            if m_cam:
                last_cam = str(m_cam.group(1) or "")
        round_actions: List[str] = []
        for index in range(total):
            round_action = action
            if rebuild_each and total > 1:
                if source == "command-look-legs" or "看看腿" in str(action or "") or "【legs:outfit】" in str(action or "") or "【pose:" in str(action or ""):
                    round_action = self._build_leg_focus_action(
                        extra_keep,
                        bool(extra_refs),
                        avoid_pose=last_pose,
                        force_legwear=force_legwear,
                    )
                    m_pose = re.search(r"【pose:([a-z_]+)】", round_action)
                    if m_pose:
                        last_pose = str(m_pose.group(1) or last_pose)
                elif source == "command-look-cos":
                    round_action = self._build_cos_look_action(
                        extra_keep,
                        bool(extra_refs),
                        avoid_id=last_cos,
                        avoid_camera=last_cam,
                        match_query=rebuild_match_query or extra_keep,
                    )
                    m_cos = re.search(r"【cos:([a-z0-9_]+)】", round_action)
                    if m_cos:
                        last_cos = str(m_cos.group(1) or last_cos)
                    m_cam = re.search(r"【cam:(selfie|third)】", round_action)
                    if m_cam:
                        last_cam = str(m_cam.group(1) or last_cam)
                elif source == "command-look-you" or "看看你模式" in str(action or ""):
                    round_action = self._build_third_person_look_action(
                        extra_keep,
                        bool(extra_refs),
                        avoid_shot=last_shot,
                    )
                    m_shot = re.search(r"【shot:([a-z_]+)】", round_action)
                    if m_shot:
                        last_shot = str(m_shot.group(1) or last_shot)
                else:
                    round_action = self._build_selfie_look_action(
                        extra_keep,
                        bool(extra_refs),
                        avoid_shot=last_shot,
                    )
                    m_shot = re.search(r"【shot:([a-z_]+)】", round_action)
                    if m_shot:
                        last_shot = str(m_shot.group(1) or last_shot)
            round_actions.append(round_action)

        async def run_one(index: int) -> Dict[str, Any]:
            round_action = round_actions[index]
            prompt, refs, prompt_en_meta = await self._build_selfie_prompt_and_refs_for_event(event, round_action, extra_refs)
            return await self._run_image_generation(
                prompt,
                aspect,
                resolution,
                refs,
                source=source,
                audit_user_id=event_user_id(event),
                event=event,
                original_prompt=round_action,
                prompt_en_meta=prompt_en_meta,
            )

        return await self._run_counted_generation_shots(
            task_id=task_id,
            event=event,
            total=total,
            fail_label=fail_label,
            run_one=run_one,
            log_prefix="selfie batch",
        )

    def _validate_web_test_selection(self, payload: Dict[str, Any]) -> None:
        channel_name = str(payload.get("channel") or "").strip()
        model_name = str(payload.get("model") or "").strip()
        media_type = str(payload.get("media_type") or "image").strip().lower()
        if not channel_name:
            return
        is_video = media_type == "video"
        channels = self.config.video_channels if is_video else self.config.image_channels
        kind_label = "视频" if is_video else "生图"
        matching_channels = [channel for channel in channels if channel.name == channel_name]
        if not matching_channels:
            raise RuntimeError(f"{kind_label}渠道 {channel_name} 不存在")
        if not any(channel.enabled for channel in matching_channels):
            raise RuntimeError(f"{kind_label}渠道 {channel_name} 已禁用，渠道测试不会调用禁用渠道")
        channel = next((item for item in matching_channels if item.enabled), matching_channels[0])
        if is_video:
            report = preflight_video_channel(
                {
                    "name": channel.name,
                    "provider_type": channel.provider_type,
                    "base_url": channel.base_url,
                    "api_key": channel.api_key,
                    "api_keys": channel.api_keys,
                    "model": channel.model,
                    "enabled_models": channel.enabled_models,
                    "enabled": channel.enabled,
                    "proxy": channel.proxy,
                }
            )
        else:
            from .core.models import preflight_image_channel

            report = preflight_image_channel(
                {
                    "name": channel.name,
                    "provider_type": channel.provider_type,
                    "base_url": channel.base_url,
                    "api_key": channel.api_key,
                    "model": channel.model,
                    "enabled_models": channel.enabled_models,
                    "enabled": channel.enabled,
                    "proxy": channel.proxy,
                },
                kind="image",
            )
        if not report.get("ok"):
            raise RuntimeError(report.get("message") or f"{kind_label}渠道配置预检未通过")
        enabled = channel.enabled_models or ([channel.model] if channel.model else [])
        if model_name and model_name not in enabled:
            raise RuntimeError(f"渠道 {channel_name} 未启用模型 {model_name}，请先在渠道管理中启用并保存")

    async def web_test_image(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        channel_name = str(payload.get("channel") or "").strip()
        model_name = str(payload.get("model") or "").strip()
        raw_images = list(payload.get("images") or [])
        if payload.get("image"):
            raw_images.append(payload.get("image"))

        original_prompt = str(payload.get("prompt") or "").strip() or "看着镜头自然自拍"
        aspect = str(payload.get("aspect_ratio") or self.config.image_default_aspect_ratio or "9:16")
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
        prompt_en_meta: Optional[Dict[str, Any]] = None

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
                    persona_ref = self._persona_identity_reference()
                    if not persona_ref:
                        raise RuntimeError("当前未设置 AI 自拍形象参考图，且未启用 logo 回退；请先上传形象图、开启「无形象图用 logo」，或取消使用自拍形象参考图")
                    refs.insert(0, persona_ref)
                prompt = original_prompt
            elif payload.get("use_selfie_reference"):
                prompt, refs, prompt_en_meta = await self._build_selfie_prompt_and_refs_for_event(
                    None,
                    original_prompt,
                    extra_refs,
                )
                if not refs:
                    raise RuntimeError("当前未设置 AI 自拍形象参考图，请先上传形象图，或取消使用自拍形象参考图")
            else:
                refs = extra_refs
                user_prompt = original_prompt
                prompt_en_meta = {"enabled": False, "applied": False, "scope": "user_text_only"}
                if self._prompt_en_needed(user_prompt, media="image"):
                    translated, prompt_en_meta = await self._translate_prompt_to_english(
                        user_prompt,
                        media="image",
                        event=None,
                    )
                    if prompt_en_meta.get("applied") and translated:
                        user_prompt = translated
                prompt = build_prompt_with_reference_instruction(
                    user_prompt,
                    refs,
                    language="en" if self.config.image_enable_image_prompt_en else "zh",
                )

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
                prompt_en_meta=prompt_en_meta,
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

    async def web_test_video(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        channel_name = str(payload.get("channel") or "").strip()
        model_name = str(payload.get("model") or "").strip()
        prompt = str(payload.get("prompt") or "").strip() or "一段自然流畅的短视频"
        aspect = str(payload.get("aspect_ratio") or "16:9").strip() or "16:9"
        duration = max(1, min(60, int(payload.get("duration") or self.config.video_default_duration or 5)))
        target = self._find_video_target(channel_name, model_name)
        if not target:
            raise RuntimeError("未找到指定视频模型")

        raw_images = list(payload.get("images") or [])
        if payload.get("image"):
            raw_images.append(payload.get("image"))
        refs: List[ImageReference] = []
        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        for raw in raw_images[:1]:
            data, mime = data_url_to_bytes(str(raw or ""))
            if not data:
                continue
            if len(data) > max_bytes:
                raise RuntimeError(f"参考图过大，最大允许 {self.config.image_max_image_size_mb}MB")
            refs.append(ImageReference(data=data, mime_type=normalize_image_mime(mime or detect_mime_by_bytes(data))))
        if payload.get("use_selfie_reference") and not refs:
            persona_ref = self._video_persona_reference()
            if not persona_ref:
                raise RuntimeError("当前未设置 AI 自拍形象参考图，请先上传形象图，或取消使用自拍形象参考图")
            refs = [persona_ref]

        started = time.monotonic()
        req = VideoGenerateRequest(
            prompt=prompt,
            images=refs,
            duration=duration,
            size=aspect,
        )
        async with self._video_semaphore:
            async with aiohttp.ClientSession(trust_env=False) as session:
                result = await generate_video_with_fallback([target], req, session, save_dir=self.video_dir)
        elapsed = round(float(result.elapsed_seconds or (time.monotonic() - started)), 2)
        record = {
            **self._source_context(None, "web-video-test"),
            "media_type": "video",
            "success": not bool(result.error) and bool(result.video_path),
            "error": result.error or "",
            "prompt": prompt,
            "original_prompt": prompt,
            "request_prompt": prompt,
            "used_model": result.used_model or target.label,
            "elapsed_seconds": elapsed,
            "reference_images": len(refs),
            "request_data": self._summarize_web_test_payload(payload),
            "response_data": {
                "attempts": result.attempts,
                "video_url": result.video_url,
                "video_source": result.video_source,
            },
            "request_image_paths": [],
            "generated_image_paths": [],
            "generated_video_paths": [self._cache_relative_path(result.video_path)] if result.video_path else [],
        }
        self._record_task(record)
        if result.error or not result.video_path:
            return {
                "success": False,
                "error": result.error or "视频没有生成出来",
                "used_model": result.used_model or target.label,
                "elapsed_seconds": elapsed,
                "attempts": result.attempts,
                "generated_video_paths": [],
            }
        return {
            "success": True,
            "used_model": result.used_model or target.label,
            "elapsed_seconds": elapsed,
            "reference_images": len(refs),
            "video_url": result.video_url,
            "video_source": result.video_source,
            "generated_video_paths": [self._cache_relative_path(result.video_path)],
            "attempts": result.attempts,
        }

    async def web_refresh_image_models(self, payload: Dict[str, Any]) -> List[str]:
        raw_payload = payload if isinstance(payload, dict) else {}
        channel_payload = raw_payload.get("channel") if isinstance(raw_payload.get("channel"), dict) else raw_payload
        base_url = str(channel_payload.get("base_url") or channel_payload.get("baseUrl") or "").strip()
        api_key = str(channel_payload.get("api_key") or channel_payload.get("apiKey") or "").strip()
        provider_type = provider_type_from_channel_payload(channel_payload)
        proxy = str(channel_payload.get("proxy") or "").strip()
        # Agnes image channels historically use the native default model because
        # some Agnes deployments do not expose a model-list endpoint. Video
        # channels use the same gateway/key but must query its real model list.
        media_type = str(
            channel_payload.get("media_type")
            or channel_payload.get("mediaType")
            or raw_payload.get("media_type")
            or raw_payload.get("mediaType")
            or ""
        ).strip().lower()
        if provider_type == "agnes" and media_type != "video":
            return ["agnes-image-2.1-flash"]
        candidates = build_model_list_urls(base_url, provider_type)
        if not candidates:
            raise RuntimeError("base_url 为空")
        headers = {"Accept": "application/json"}
        if provider_type == "gemini" and api_key:
            headers["x-goog-api-key"] = api_key
        elif api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        errors: List[str] = []
        async with aiohttp.ClientSession(trust_env=False) as base_session:
            async with channel_client_session(proxy, base_session) as session:
                request_proxy = http_proxy_url(proxy)
                for url in candidates:
                    safe_url = redact_sensitive_text(url)
                    try:
                        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12), proxy=request_proxy) as response:
                            if response.status >= 400:
                                errors.append(f"{safe_url}: HTTP {response.status} {redact_sensitive_text(await response.text())[:200]}")
                                continue
                            data = await response.json(content_type=None)
                        models = self._extract_model_ids(data)
                        if models:
                            return models
                        errors.append(f"{safe_url}: 返回成功但未识别到模型")
                    except Exception as exc:
                        errors.append(f"{safe_url}: {redact_sensitive_text(str(exc))}")
        raise RuntimeError("\n".join(errors))

    def _extract_model_ids(self, data: Any) -> List[str]:
        # Refreshing a channel is an inventory operation: keep every model the
        # upstream returned, in its original order, without family-name rules.
        return extract_model_ids_from_response(data, preserve_order=True)

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
        user_prompt = str(prompt or "").strip()
        prompt_en_meta: Dict[str, Any] = {"enabled": False, "applied": False, "scope": "user_text_only"}
        if self._prompt_en_needed(user_prompt, media="image"):
            translated, prompt_en_meta = await self._translate_prompt_to_english(
                user_prompt, media="image", event=event
            )
            if prompt_en_meta.get("applied") and translated:
                user_prompt = translated
        final_prompt = build_prompt_with_reference_instruction(
            user_prompt,
            refs,
            language="en" if self.config.image_enable_image_prompt_en else "zh",
        )
        return await self._run_image_generation(
            final_prompt,
            aspect,
            resolution,
            refs,
            source=source,
            audit_user_id=event_user_id(event),
            event=event,
            original_prompt=prompt,
            prompt_en_meta=prompt_en_meta,
        )

    async def _draw_passthrough_once(self, event: AstrMessageEvent, prompt: str, aspect: str, resolution: str, refs: List[ImageReference], source: str) -> Dict[str, Any]:
        user_prompt = str(prompt or "").strip()
        prompt_en_meta: Dict[str, Any] = {"enabled": False, "applied": False, "scope": "user_text_only"}
        if self._prompt_en_needed(user_prompt, media="image"):
            translated, prompt_en_meta = await self._translate_prompt_to_english(
                user_prompt, media="image", event=event
            )
            if prompt_en_meta.get("applied") and translated:
                user_prompt = translated
        return await self._run_image_generation(
            user_prompt,
            aspect,
            resolution,
            refs,
            source=source,
            audit_user_id=event_user_id(event),
            event=event,
            original_prompt=prompt,
            prompt_en_meta=prompt_en_meta,
        )

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
        allow_context_fallback: bool = True,
        requested_count_override: int = 0,
        preset_aspect: str = "",
        preset_resolution: str = "",
        preset_name: str = "",
        rebuild_extra_request: str = "",
        rebuild_match_query: str = "",
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

        # Prefer aspect/resolution resolved from raw user text (before action wrappers).
        if str(preset_name or "").strip():
            action = message
            default_aspect = str(self.config.image_default_aspect_ratio or "9:16").strip() or "9:16"
            default_resolution = str(self.config.image_default_resolution or "1K").strip() or "1K"
            aspect = str(preset_aspect or "").strip() or default_aspect
            resolution = str(preset_resolution or "").strip() or default_resolution
        else:
            action, aspect, resolution, _, _ = self._resolve_image_preset(message)
        extra_refs = await self._event_reference_images(
            event,
            include_at_avatar=include_at_avatar,
            context_hint=action,
            allow_context_fallback=allow_context_fallback,
        )
        if not action:
            action = default_action_with_refs if extra_refs else default_action
        hints: List[str] = []
        if not self.persona.has_reference_image():
            if bool(getattr(self.config, "image_use_logo_when_no_persona", True)):
                hints.append("当前还没有设置 AI 形象参考图，将用插件 logo 作为形象回退；关闭「无形象图用 logo」后改为仅按人设生成。")
            else:
                hints.append("当前还没有设置 AI 形象参考图，会按人设与今日设定生成主角。")
        if progress_label == "合影" and not extra_refs:
            hints.append("没有读取到合影对象参考图，会按文字要求生成同框对象。")
        progress = await self._build_contextual_progress_text(event, "selfie", action, requested_count)
        if hints:
            progress += "\n" + "\n".join(hints)
        queue_notified = self._image_batch_queue_expected(requested_count)
        if queue_notified:
            progress = "当前生图并发已满，本次先排队，空出槽位后继续。\n" + progress
        self._record_bot_text_context(event, progress)

        async def runner(task_id: str) -> Dict[str, Any]:
            return await self._background_selfie_batches(
                task_id,
                event,
                action,
                extra_refs,
                source,
                requested_count,
                aspect,
                resolution,
                fail_label,
                queue_notified=queue_notified,
                rebuild_extra_request=rebuild_extra_request,
                rebuild_match_query=rebuild_match_query,
            )

        task = self.start_command_image_task(
            event,
            source=source,
            summary={
                "original_prompt": action,
                "aspect_ratio": aspect,
                "resolution": resolution,
                "requested_count": requested_count,
                "kind": progress_label,
                "preset_name": str(preset_name or "").strip(),
            },
            runner=runner,
        )
        yield event.plain_result(progress)

    @filter.command("生图帮助")
    async def cmd_help(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """只看图卡帮助。完整文字说明请发 /生图help。"""
        help_path = self._resolve_help_image_path()
        if help_path:
            yield event.chain_result([self._create_image_component(help_path)])
            return
        # 无图时退回简短提示，避免空白
        yield event.plain_result("帮助图暂不可用。发 /生图help 看文字说明。")

    @filter.command("生图help")
    async def cmd_help_text(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """完整文字指令说明。"""
        yield event.plain_result(self._help_text_body())

    def _help_text_body(self) -> str:
        return "\n".join(
            [
                f"{PLUGIN_DISPLAY_NAME} v{PLUGIN_VERSION}",
                "",
                "常用：",
                "· /画 或 /生图　写想要的画面；可写数量如 /画 3；有附图/引用图就按图改，没图就按文字出；不自动带入形象图",
                "· /文生图　只用文字按原文出图，不走自拍人设，也不用形象图；可写数量",
                "· /图生图　必须附图或引用图，按原文改图；可写数量；不自动使用形象图",
                "· /自拍 或 /看看　用当前形象自拍；可写动作、场景、换装；可写数量如 /自拍 3",
                "· /看看腿　腰部以下的日常下装穿搭近景，上半身不入镜；腿部穿搭仅随机光腿神器、白丝或黑丝，可直接指定；随机手机记录或朋友协助拍摄视角；可写数量如 /看看腿 3",
                "· /查看提示词　引用图片后查看原生图提示词；没有生图记录时由当前聊天 LLM 反推",
                "· /查看生图提示词　引用或附带图片后，始终由当前聊天 LLM 反推生图提示词，不查询生图记录",
                "· /看看COS　随机一套内置 COS 换装；数量、预设、随机池角色/类别和额外提示词可任意顺序，未匹配文本保留为额外提示；可发「看看COS 列表/全部/查看」浏览标题，如「看看COS 西施 捧脸 夜景 3」「看看COS 捧脸 3 西施」；默认随机自拍或他拍，也可写「自拍」「他拍」",
                "· /看看你　像别人随手拍你；可写数量",
                "· /合影 或 /合照　和对象同框；可附图或@对方，自己用当前形象；可写数量",
                "",
                "视频：",
                "· /视频　写想要的动态；有附图/引用图就图生视频，没图默认按文字生成；明确我出镜时才用当前形象图",
                "· /文生视频　只用文字出视频，不带图、不用形象图",
                "· /图生视频　必须附图或引用图作首帧，不会自动使用当前形象图",
                "· /形象视频　使用当前形象图作首帧；需先设置形象图",
                "· /看看视频　使用当前形象图主动出视频；需先设置形象图",
                "· /视频任务　只看进行中的视频任务；可跟任务号或列表编号",
                "· /视频取消　取消视频任务；可跟任务号或列表编号",
                "· /视频预设　查看、使用和管理视频预设；时长可写 --duration 8 或 时长8秒",
                "",
                "自动判断：",
                "· /画：有图=图生图，没图=文生图；不会自动塞形象图",
                "· /视频：有图=图生视频，没图=文生视频；明确要求本人/当前形象出镜时才使用形象图",
                "· 自拍/合影/看看：会用当前形象；形象类型可设自动、真人、动漫",
                "",
                "模型与进度：",
                "· /生图模型　看列表；跟序号或 渠道/模型 切换（只影响当前群/私聊）；发「清除」恢复默认",
                "· /生图任务　看出图/视频进行中的任务；可跟任务号",
                "· /生图取消　取消还在排的/进行中的任务",
                "",
                "形象：",
                "· /形象查看　看当前参考图、形象类型与今日状态",
                "· /形象设置　发图设形象；也可写 自动 / 真人 / 动漫 改形象类型",
                "· /辅助形象设置　附带图片增加辅助形象；最多 3 张，普通自拍/换装会使用，合影时只使用主形象图",
                "· /辅助形象清除　清空辅助形象图，不影响主形象",
                "· /形象清除　去掉参考图",
                "· /形象刷新　刷新今日穿搭状态",
                "",
                "预设：/预设　列表；可在任意生图指令中写「特殊预设」随机选择一条服装结构预设；管理员可 /预设添加 名称:内容、/预设删除 名称",
                "",
                "说明：一次可写数量表示本条指令要生成的总张数；同时最多进行几张由「同时画几张上限」决定，不锁在单条指令里，新任务自动排队，超过同时上限才等待。图好了会直接发过来。",
                "· /生图帮助　只看图卡",
                "· /生图help　看本页完整说明",
                f"管理页：{'已开' if self.config.web_enable else '未开'}　http://{self.config.web_host}:{self.config.web_port}",
                "也可在 AstrBot 插件页打开管理界面。",
            ]
        )

    def _resolve_help_image_path(self) -> str:
        """Return shipped static help poster only (no runtime generation)."""
        for path in (getattr(self, "_bundled_help_poster_path", ""),):
            if path and os.path.isfile(path):
                try:
                    with open(path, "rb") as handle:
                        head = handle.read(32)
                    if looks_like_image_bytes(head):
                        return path
                except Exception:
                    continue
        return ""

    def _quoted_reply_ids(self, event: Optional[AstrMessageEvent]) -> List[str]:
        """Return message IDs from Reply components in the current event."""
        if event is None:
            return []
        found: List[str] = []
        seen_ids: set[str] = set()
        visited: set[int] = set()

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in seen_ids:
                seen_ids.add(text)
                found.append(text)

        def walk(obj: Any, depth: int = 0) -> None:
            if obj is None or depth > 12 or id(obj) in visited:
                return
            visited.add(id(obj))
            obj_type = type(obj).__name__
            if obj_type in {"Reply", "Quote"} or "reply" in obj_type.lower() or "quote" in obj_type.lower():
                for key in ("id", "message_id", "msg_id"):
                    add(getattr(obj, key, None))
                # A Reply's chain contains the converted image, but nested
                # replies may still carry useful original message IDs.
                walk(getattr(obj, "chain", None), depth + 1)
                return
            if isinstance(obj, Mapping):
                if str(obj.get("type") or "").lower() == "reply":
                    data = obj.get("data") if isinstance(obj.get("data"), Mapping) else obj
                    add(data.get("id"))
                for value in obj.values():
                    walk(value, depth + 1)
                return
            if isinstance(obj, (list, tuple, set)):
                for value in obj:
                    walk(value, depth + 1)
                return
            attrs: List[str] = []
            if hasattr(obj, "__dict__"):
                attrs.extend(vars(obj).keys())
            if hasattr(obj, "__slots__"):
                attrs.extend(list(getattr(obj, "__slots__", []) or []))
            blocked = {"bot", "context", "star", "provider", "session", "config", "plugin_config", "logger"}
            for key in set(attrs) - blocked:
                try:
                    walk(getattr(obj, key), depth + 1)
                except Exception:
                    continue

        message_obj = getattr(event, "message_obj", None)
        walk(getattr(message_obj, "message", None))
        walk(getattr(message_obj, "quote", None))
        walk(getattr(event, "message", None))
        walk(getattr(event, "raw_message", None))
        return found

    async def _quoted_original_image_sources(self, event: Optional[AstrMessageEvent]) -> List[str]:
        """Fetch original sources for current replies before AstrBot JPEG conversion."""
        if event is None:
            return []
        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)
        if not callable(call_action):
            call_action = getattr(getattr(bot, "api", None), "call_action", None)
        if not callable(call_action):
            return []
        reply_ids = self._quoted_reply_ids(event)
        if not reply_ids:
            return []

        routing: Dict[str, Any] = {}
        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        for source in (raw_message, getattr(event, "raw_message", None), message_obj, event):
            if isinstance(source, Mapping) and source.get("self_id"):
                routing["self_id"] = source.get("self_id")
                break
            value = getattr(source, "self_id", None)
            if value:
                routing["self_id"] = value
                break

        sources: List[str] = []
        for reply_id in reply_ids:
            try:
                numeric_id = int(reply_id)
            except (TypeError, ValueError):
                continue
            try:
                payload = await asyncio.wait_for(
                    call_action("get_msg", message_id=numeric_id, **routing),
                    timeout=5,
                )
            except Exception as exc:
                logger.debug("[SelfieImage] 回查引用原图失败 id=%s: %s", reply_id, exc)
                continue
            if not isinstance(payload, Mapping):
                continue
            if not payload.get("message") and isinstance(payload.get("data"), Mapping):
                payload = payload["data"]
            raw_event = SimpleNamespace(message_obj=None, message=None, raw_message=payload)
            buckets = extract_structured_image_sources(raw_event, include_image_alternates=True)
            for role in ("message", "quote", "forward"):
                for source in buckets.get(role, []):
                    if source not in sources:
                        sources.append(source)
        return sources

    @filter.command("查看提示词")
    async def cmd_view_prompt(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """引用一张图片查看原生图提示词；没有记录时由当前聊天 LLM 反推。"""
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        quoted_sources = await self._quoted_original_image_sources(event)
        collect_kwargs: Dict[str, Any] = {
            "include_at_avatar": False,
            "context_hint": "查看提示词",
            "allow_context_fallback": False,
            "include_persona": False,
            # QQ/AstrBot may expose both a transcoded local path and the
            # original URL. Prompt lookup must try only representations of
            # the quoted image; unrelated recent bot images must never match.
            "include_image_alternates": True,
        }
        if quoted_sources:
            collect_kwargs["extra_sources"] = quoted_sources
        refs = await self._event_reference_images(
            event,
            **collect_kwargs,
        )
        if not refs:
            yield event.plain_result("请引用一张图片后再使用 /查看提示词。")
            return

        ref = refs[0]
        md5 = hashlib.md5(ref.data).hexdigest()
        record = None
        for candidate in refs:
            candidate_md5 = hashlib.md5(candidate.data).hexdigest()
            candidate_record = self._find_generation_record_by_md5(candidate_md5)
            if candidate_record is not None:
                ref = candidate
                md5 = candidate_md5
                record = candidate_record
                break
        logger.debug(
            "[SelfieImage] 查看提示词图片候选 MD5: %s",
            ", ".join(hashlib.md5(candidate.data).hexdigest() for candidate in refs),
        )
        if record is not None:
            prompt = str(
                record.get("request_prompt")
                or record.get("prompt")
                or record.get("original_prompt")
                or ""
            ).strip()
            if prompt:
                yield event.plain_result(f"图片 MD5：{md5}\n生图提示词：\n{prompt}")
            else:
                yield event.plain_result(f"图片 MD5：{md5}\n这是本插件生成的图片，但历史记录中没有保存提示词。")
            return

        yield event.plain_result(f"图片 MD5：{md5}\n未找到本插件的生图记录，正在让当前 LLM 反推提示词……")
        try:
            prompt = await self._reverse_image_prompt_with_llm(event, ref.data)
        except Exception as exc:
            logger.warning("[SelfieImage] 反推图片提示词失败: %s", redact_sensitive_text(str(exc)))
            yield event.plain_result(f"图片 MD5：{md5}\n暂时无法反推提示词：{redact_sensitive_text(str(exc))[:200]}")
            return
        if not prompt:
            yield event.plain_result(f"图片 MD5：{md5}\n当前 LLM 没有返回有效提示词。")
            return
        yield event.plain_result(f"图片 MD5：{md5}\nLLM 反推提示词：\n{prompt}")

    @filter.command("查看生图提示词")
    async def cmd_reverse_image_prompt(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """引用或附带一张图片，始终由当前聊天 LLM 反推生图提示词。"""
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        quoted_sources = await self._quoted_original_image_sources(event)
        collect_kwargs: Dict[str, Any] = {
            "include_at_avatar": False,
            "context_hint": "查看生图提示词",
            "allow_context_fallback": False,
            "include_persona": False,
            "include_image_alternates": True,
        }
        if quoted_sources:
            collect_kwargs["extra_sources"] = quoted_sources
        refs = await self._event_reference_images(event, **collect_kwargs)
        if not refs:
            yield event.plain_result("请引用或附带一张图片后再使用 /查看生图提示词。")
            return

        ref = refs[0]
        md5 = hashlib.md5(ref.data).hexdigest()
        yield event.plain_result(f"图片 MD5：{md5}\n正在让当前 LLM 反推生图提示词……")
        try:
            prompt = await self._reverse_image_prompt_with_llm(event, ref.data)
        except Exception as exc:
            logger.warning("[SelfieImage] 反推图片提示词失败: %s", redact_sensitive_text(str(exc)))
            yield event.plain_result(f"图片 MD5：{md5}\n暂时无法反推提示词：{redact_sensitive_text(str(exc))[:200]}")
            return
        if not prompt:
            yield event.plain_result(f"图片 MD5：{md5}\n当前 LLM 没有返回有效提示词。")
            return
        yield event.plain_result(f"图片 MD5：{md5}\nLLM 反推提示词：\n{prompt}")

    @filter.command("生图模型")
    async def cmd_image_model(self, event: AstrMessageEvent, p1: str = "", p2: str = "", p3: str = "") -> AsyncGenerator[Any, None]:
        """查看或切换当前聊天使用的模型。可跟序号、渠道/模型，或发「清除」恢复默认。"""
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        fallback = " ".join(item for item in [p1, p2, p3] if item).strip()
        message = extract_command_message(event, "生图模型", fallback).strip()
        labels = self._available_model_labels()
        current = self._get_session_model_override(event)
        default_label = labels[0] if labels else ""
        effective = current or default_label

        if not message:
            if not labels:
                yield event.plain_result("现在还没有可用模型，请先在管理页启用渠道和模型。")
                return
            lines = ["可用模型（只改当前聊天，不影响其他群）："]
            for index, label in enumerate(labels, 1):
                mark = " ✓" if label == effective else ""
                lines.append(f"{index}. {label}{mark}")
            lines.append(f"当前：{effective or '（未配置）'}")
            if current:
                lines.append(f"本会话指定：{current}（发 /生图模型 清除 可恢复默认）")
            else:
                lines.append("切换：/生图模型 序号　或　/生图模型 渠道/模型")
            yield event.plain_result("\n".join(lines))
            return

        if message in {"清除", "取消", "默认", "reset", "clear"}:
            self._set_session_model_override(event, "")
            yield event.plain_result(f"已恢复默认顺序：{default_label or '（无模型）'}")
            return

        matched = self._match_model_label(message)
        if not matched:
            yield event.plain_result("没对上模型。先 /生图模型 看列表，再发序号或 渠道/模型。")
            return
        self._set_session_model_override(event, matched)
        yield event.plain_result(f"本会话已换成：{matched}\n之后这里的 /画、/自拍 等会优先用它。")

    @filter.command("生图任务")
    async def cmd_image_tasks(self, event: AstrMessageEvent, p1: str = "", p2: str = "") -> AsyncGenerator[Any, None]:
        """查看进行中的出图/视频任务。可跟任务号或列表编号。"""
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        fallback = " ".join(item for item in [p1, p2] if item).strip()
        message = extract_command_message(event, "生图任务", fallback).strip()
        session_key = self._session_key(event)
        is_admin = self._is_admin_event(event)

        if message:
            try:
                task = self.get_web_image_task(message)
            except Exception:
                # numeric index into active list
                active = self._list_image_tasks_for_session(session_key, include_finished=False, limit=20)
                if message.isdigit():
                    index = int(message) - 1
                    if 0 <= index < len(active):
                        task = active[index]
                    else:
                        yield event.plain_result("没找到这个进行中的编号，或任务号不对。")
                        return
                else:
                    yield event.plain_result("没有这单，或已经清理了。")
                    return
            owner = str(task.get("owner_session") or "")
            if owner and owner != session_key and not is_admin:
                yield event.plain_result("不能看别人会话里的出图。")
                return
            # refresh running_seconds
            if task.get("status") in {"queued", "running"}:
                try:
                    task = self.get_web_image_task(str(task.get("task_id") or message))
                except Exception:
                    pass
            yield event.plain_result(self._format_task_detail_text(task))
            return

        tasks = self._list_image_tasks_for_session(session_key, include_finished=False, limit=10)
        yield event.plain_result(self._format_task_list_text(tasks))

    @filter.command("生图取消")
    async def cmd_image_task_cancel(self, event: AstrMessageEvent, p1: str = "", p2: str = "") -> AsyncGenerator[Any, None]:
        """取消排队中或进行中的出图/视频任务。可跟任务号或列表编号。"""
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        fallback = " ".join(item for item in [p1, p2] if item).strip()
        message = extract_command_message(event, "生图取消", fallback).strip()
        session_key = self._session_key(event)
        is_admin = self._is_admin_event(event)
        if not message:
            active = self._list_image_tasks_for_session(session_key, include_finished=False, limit=5)
            if active:
                yield event.plain_result("请跟任务号或列表里的编号。\n" + self._format_task_list_text(active))
            else:
                yield event.plain_result("现在没有可取消的出图。")
            return
        task_id = message
        if message.isdigit():
            active = self._list_image_tasks_for_session(session_key, include_finished=False, limit=20)
            index = int(message) - 1
            if 0 <= index < len(active):
                task_id = str(active[index].get("task_id") or "")
            else:
                yield event.plain_result("未找到对应的进行中任务，请检查编号或任务ID。")
                return
        try:
            text = self.cancel_image_task(task_id, session_key=session_key, is_admin=is_admin)
            yield event.plain_result(text)
        except PermissionError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            yield event.plain_result(redact_sensitive_text(str(exc)))

    @filter.command("视频任务")
    async def cmd_video_tasks(self, event: AstrMessageEvent, p1: str = "", p2: str = "") -> AsyncGenerator[Any, None]:
        """只查看进行中的视频任务。可跟任务号或当前列表编号。"""
        fallback = " ".join(item for item in [p1, p2] if item).strip()
        async for item in self._command_task_list(
            event, "视频任务", fallback, media_type="video"
        ):
            yield item

    @filter.command("视频取消")
    async def cmd_video_task_cancel(self, event: AstrMessageEvent, p1: str = "", p2: str = "") -> AsyncGenerator[Any, None]:
        """只取消排队中或进行中的视频任务。可跟任务号或当前列表编号。"""
        fallback = " ".join(item for item in [p1, p2] if item).strip()
        async for item in self._command_task_cancel(
            event, "视频取消", fallback, media_type="video"
        ):
            yield item

    @filter.command("生图重发")
    async def cmd_image_retry_failed(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """重发本会话最近发送失败、仍存在于缓存中的图片。"""
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        result = await self.retry_failed_images(event)
        if result["sent"]:
            yield event.plain_result(f"已重发 {result['sent']} 张图片。")
        else:
            yield event.plain_result("没有可重发的图片。")

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
        """写想要的画面出图。有附图/引用图时按图改；没图时按文字出。不会自动带入形象图。"""
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        message = extract_command_message(event, ("画", "生图"), fallback)
        message, requested_count = self._extract_command_count(message, allow_trailing=True)
        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        prompt, aspect, resolution, _ = self._expand_user_text_with_preset(message)
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

        async def runner(task_id: str) -> Dict[str, Any]:
            return await self._background_draw_batches(
                task_id,
                event,
                prompt,
                aspect,
                resolution,
                refs,
                "command-draw",
                requested_count,
                passthrough=True,
            )

        task = self.start_command_image_task(
            event,
            source="command-draw",
            summary={
                "original_prompt": prompt,
                "aspect_ratio": aspect,
                "resolution": resolution,
                "requested_count": requested_count,
                "reference_image_count": len(refs),
            },
            runner=runner,
        )
        yield event.plain_result(progress)

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
        """按你写的原文出图，不走自拍人设包装，也不使用形象图。"""
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        message = extract_command_message(event, "文生图", fallback)
        message, requested_count = self._extract_command_count(message, allow_trailing=True)
        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        prompt, aspect, resolution, _ = self._expand_user_text_with_preset(message)
        if not prompt:
            yield event.plain_result("请输入文生图提示词。")
            return

        progress = await self._build_contextual_progress_text(event, "image", prompt, requested_count)
        self._record_bot_text_context(event, progress)

        async def runner(task_id: str) -> Dict[str, Any]:
            return await self._background_draw_batches(
                task_id,
                event,
                prompt,
                aspect,
                resolution,
                [],
                "command-raw-text-to-image",
                requested_count,
                passthrough=True,
            )

        task = self.start_command_image_task(
            event,
            source="command-raw-text-to-image",
            summary={
                "original_prompt": prompt,
                "aspect_ratio": aspect,
                "resolution": resolution,
                "requested_count": requested_count,
            },
            runner=runner,
        )
        yield event.plain_result(progress)

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
        """带图或引用图，按原文改图。需要附图，不会自动使用形象图。"""
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        message = extract_command_message(event, "图生图", fallback)
        message, requested_count = self._extract_command_count(message, allow_trailing=True)
        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        prompt, aspect, resolution, _ = self._expand_user_text_with_preset(message)
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

        async def runner(task_id: str) -> Dict[str, Any]:
            return await self._background_draw_batches(
                task_id,
                event,
                prompt,
                aspect,
                resolution,
                refs,
                "command-raw-image-to-image",
                requested_count,
                passthrough=True,
            )

        task = self.start_command_image_task(
            event,
            source="command-raw-image-to-image",
            summary={
                "original_prompt": prompt,
                "aspect_ratio": aspect,
                "resolution": resolution,
                "requested_count": requested_count,
                "reference_image_count": len(refs),
            },
            runner=runner,
        )
        yield event.plain_result(progress)

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
        """用当前形象自拍。可写动作、场景、换装；有附图时作服装/场景参考。"""
        fallback_args = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        raw_message = extract_command_message(event, ("自拍", "看看"), fallback_args)
        raw_extra, requested_count = self._extract_command_count(raw_message, allow_trailing=True)
        # Resolve presets on raw user words first (e.g. /自拍 捧脸), then wrap action.
        expanded_extra, preset_aspect, preset_resolution, preset_name = self._expand_user_text_with_preset(raw_extra)
        has_refs = bool(extract_image_sources_from_event(event))
        if expanded_extra.strip():
            base_action = self._build_selfie_look_action(expanded_extra, has_refs)
        else:
            base_action = self._build_selfie_look_action("", has_refs)
        async for item in self._handle_selfie_command(
            event=event,
            command_name=("自拍", "看看"),
            fallback=base_action,
            default_action=self._build_selfie_look_action("", False),
            default_action_with_refs=self._build_selfie_look_action("", True),
            progress_label="自拍",
            source="command-selfie",
            fail_label=self._natural_fail_fallback("selfie"),
            message_override=base_action,
            requested_count_override=requested_count,
            preset_aspect=preset_aspect,
            preset_resolution=preset_resolution,
            preset_name=preset_name,
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
        """腰部以下的日常下装穿搭近景；上半身不入镜；可写数量。"""
        fallback_args = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        raw_message = extract_command_message(event, "看看腿", fallback_args)
        raw_extra, requested_count = self._extract_command_count(raw_message, allow_trailing=True)
        expanded_extra, preset_aspect, preset_resolution, preset_name = self._expand_user_text_with_preset(raw_extra)
        fallback = self._build_leg_focus_action(expanded_extra, bool(extract_image_sources_from_event(event)))
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
            preset_aspect=preset_aspect,
            preset_resolution=preset_resolution,
            preset_name=preset_name,
        ):
            yield item

    @filter.command("看看COS", alias={"看看cos", "看看Cos"})
    async def cmd_look_cos(
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
        """随机一套内置 COS 换装；可用列表/全部/查看浏览标题，或按标题分段指定套装并附加数量。"""
        fallback_args = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        raw_message = extract_command_message(event, "看看COS", fallback_args)
        # Also accept lowercase command text extraction fallbacks.
        if not raw_message:
            raw_message = extract_command_message(event, "看看cos", fallback_args)
        list_request = re.sub(r"[\s，。！？、；：,.!?;:]+", "", str(raw_message or "")).lower()
        if list_request in {"列表", "全部", "查看", "list", "all", "view"}:
            yield event.plain_result(format_cos_look_list())
            return
        raw_extra, requested_count = self._extract_command_count(raw_message, allow_attached=True)
        expanded_extra, preset_aspect, preset_resolution, preset_name = self._expand_cos_user_text_with_preset(raw_extra)
        has_refs = bool(extract_image_sources_from_event(event))
        fallback = self._build_cos_look_action(expanded_extra, has_refs, match_query=raw_extra)
        async for item in self._handle_selfie_command(
            event=event,
            command_name="看看COS",
            fallback=fallback,
            default_action=self._build_cos_look_action("", False),
            default_action_with_refs=self._build_cos_look_action("", True),
            progress_label="自拍",
            source="command-look-cos",
            fail_label=self._natural_fail_fallback("selfie"),
            message_override=fallback,
            allow_context_fallback=False,
            requested_count_override=requested_count,
            preset_aspect=preset_aspect,
            preset_resolution=preset_resolution,
            preset_name=preset_name,
            rebuild_extra_request=expanded_extra,
            rebuild_match_query=raw_extra,
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
        """像别人随手拍你，带一点日常他拍感。使用当前形象。"""
        fallback_args = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        raw_message = extract_command_message(event, "看看你", fallback_args)
        raw_extra, requested_count = self._extract_command_count(raw_message, allow_trailing=True)
        expanded_extra, preset_aspect, preset_resolution, preset_name = self._expand_user_text_with_preset(raw_extra)
        fallback = self._build_third_person_look_action(expanded_extra, bool(extract_image_sources_from_event(event)))
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
            preset_aspect=preset_aspect,
            preset_resolution=preset_resolution,
            preset_name=preset_name,
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
        """和对象同框合影。可附图或@对方；自己使用当前形象。"""
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        raw_message = extract_command_message(event, ("合影", "合照"), fallback)
        raw_message, requested_count = self._extract_command_count(raw_message, allow_trailing=True)
        expanded_message, preset_aspect, preset_resolution, preset_name = self._expand_user_text_with_preset(raw_message)
        action = self._build_group_selfie_action(
            expanded_message,
            bool(extract_image_sources_from_event(event, include_at_avatar=True)),
        )
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
            preset_aspect=preset_aspect,
            preset_resolution=preset_resolution,
            preset_name=preset_name,
        ):
            yield item

    @filter.command("形象查看")
    async def cmd_persona_status(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """查看当前形象参考图、形象类型与今日状态。"""
        await self.persona.ensure_daily_selfie_profile("查看今日自拍设定")
        path = self.persona.get_reference_path()
        if path:
            yield event.chain_result([self._create_image_component(path)])
        yield event.plain_result(self.persona.status_text())

    @filter.command("形象设置")
    async def cmd_persona_set(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """设置形象参考图，或改形象类型：自动 / 真人 / 动漫。"""
        sources = extract_image_sources_from_event(event, include_at_avatar=False)
        text = extract_event_text(event)
        sources.extend(extract_image_urls(text))
        sources = list(dict.fromkeys(sources))
        # 允许「形象设置 动漫/真人/自动」只改类型；也可与附图一起设置
        type_hint = ""
        compact = re.sub(r"[\s，。！？、；：,.!?]", "", str(text or ""))
        for token, value in (
            ("二次元", "anime"),
            ("动漫", "anime"),
            ("动画", "anime"),
            ("真人", "real"),
            ("写实", "real"),
            ("自动", "auto"),
            ("默认", "auto"),
        ):
            if token in compact:
                type_hint = value
                break
        if type_hint:
            self.persona.set_appearance_type(type_hint)
        if not sources:
            if type_hint:
                yield event.plain_result(f"形象类型已设为{self.persona.appearance_type_label()}。\n" + self.persona.status_text())
                return
            yield event.plain_result(
                "请发送图片、引用图片，或在指令后附带图片链接。\n"
                "也可：形象设置 自动 / 形象设置 真人 / 形象设置 动漫"
            )
            return
        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        async with aiohttp.ClientSession(trust_env=False) as session:
            for source in sources:
                fetched = await fetch_image_source(source, session, max_bytes=max_bytes)
                if not fetched:
                    continue
                data, mime = fetched
                self.persona.save_reference_image(data, mime)
                msg = "AI 自拍形象参考图已保存。"
                if type_hint:
                    msg += f" 形象类型：{self.persona.appearance_type_label()}。"
                yield event.plain_result(msg)
                return
        yield event.plain_result("没有读取到可用图片，或图片超过大小限制。")

    @filter.command("辅助形象设置")
    async def cmd_persona_auxiliary_set(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """增加辅助形象参考图；辅助图最多保存 3 张。"""
        event_text = extract_event_text(event)
        text = extract_command_message(event, "辅助形象设置", event_text)
        compact = re.sub(r"[\s，。！？、；：,.!?]", "", str(text or "")).lower()
        clear_requested = compact in {"清除", "删除", "重置", "clear", "reset", "全部清除"} or any(
            compact.startswith(prefix) for prefix in ("清除辅助形象", "删除辅助形象", "重置辅助形象")
        )
        if clear_requested:
            self.persona.clear_auxiliary_reference_images()
            yield event.plain_result("辅助形象参考图已全部清除，主形象不受影响。")
            return

        sources = extract_image_sources_from_event(event, include_at_avatar=False)
        sources.extend(extract_image_urls(text))
        sources = list(dict.fromkeys(sources))
        current_count = len(self.persona.get_auxiliary_reference_entries())
        if not sources:
            if current_count:
                yield event.plain_result(
                    f"当前已有 {current_count} 张辅助形象参考图（最多 3 张）。"
                    "请附带图片上传，合影时不会使用辅助图。"
                )
            else:
                yield event.plain_result(
                    "请发送图片、引用图片，或在指令后附带图片链接，作为辅助形象参考图。"
                    "最多可设置 3 张。"
                )
            return
        if current_count >= 3:
            yield event.plain_result("辅助形象参考图已达到上限 3 张，请先使用 /辅助形象清除。")
            return

        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        saved = 0
        failed = 0
        async with aiohttp.ClientSession(trust_env=False) as session:
            for source in sources:
                if current_count + saved >= 3:
                    break
                try:
                    fetched = await fetch_image_source(source, session, max_bytes=max_bytes)
                except Exception:
                    fetched = None
                if not fetched:
                    failed += 1
                    continue
                data, mime = fetched
                try:
                    self.persona.add_auxiliary_reference_image(data, mime)
                except (OSError, ValueError):
                    failed += 1
                    continue
                saved += 1

        total = len(self.persona.get_auxiliary_reference_entries())
        if saved:
            message = f"已保存 {saved} 张辅助形象参考图，当前共 {total} 张。合影时只使用主形象图。"
            if failed:
                message += f"另有 {failed} 张图片读取失败。"
            if total >= 3 and len(sources) > saved:
                message += "辅助形象图已达到上限 3 张。"
            yield event.plain_result(message)
            return
        if failed:
            yield event.plain_result("没有读取到可用图片，或图片超过大小限制。")
        else:
            yield event.plain_result("没有新增辅助形象参考图。")

    @filter.command("辅助形象清除")
    async def cmd_persona_auxiliary_clear(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """清除全部辅助形象参考图，不影响主形象。"""
        count = len(self.persona.get_auxiliary_reference_entries())
        self.persona.clear_auxiliary_reference_images()
        yield event.plain_result(
            "辅助形象参考图已清除，不影响主形象。"
            if count
            else "当前没有辅助形象参考图。主形象不受影响。"
        )

    @filter.command("形象清除")
    async def cmd_persona_clear(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """清除当前形象参考图。"""
        self.persona.clear_reference_image()
        yield event.plain_result("AI 自拍形象参考图已清除。")

    @filter.command("形象刷新")
    async def cmd_persona_refresh(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """刷新今日穿搭与状态。"""
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
        """查看预设列表，或按预设名生成。"""
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

    @filter.command("视频预设", prefix_optional=True)
    async def cmd_video_preset(
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
        """查看、使用或管理视频提示词预设。"""
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        text = self._normalize_preset_input(extract_command_message(event, "视频预设", fallback))
        if not text:
            body, _, _ = self._video_preset_list_text(1)
            yield event.plain_result(body)
            return
        head, tail = self._split_preset_command(text)
        if head.isdigit() or head in {"列表", "list"}:
            page = int(head) if head.isdigit() else (int(tail) if tail.isdigit() else 1)
            body, _, _ = self._video_preset_list_text(page)
            yield event.plain_result(body)
            return
        if head in {"查看", "详情", "view", "detail"}:
            if not self._is_admin_event(event):
                yield event.plain_result("仅管理员可以查看视频预设内容。")
                return
            if not tail or tail.isdigit():
                body, _, _ = self._video_preset_detail_text(int(tail) if tail.isdigit() else 1)
                yield event.plain_result(body)
            else:
                success, body = self._video_preset_single_detail_text(tail)
                yield event.plain_result(body if success else f"❌ {body}")
            return
        if head in {"添加", "add", "新增"}:
            if not tail:
                yield event.plain_result("格式：/视频预设 添加 名称:提示词")
                return
            success, message = self._handle_video_preset_mutation(event, "add", tail)
            yield event.plain_result(f"{'✅' if success else '❌'} {message}")
            return
        if head in {"删除", "del", "delete", "remove", "删"}:
            if not tail:
                yield event.plain_result("格式：/视频预设 删除 名称")
                return
            success, message = self._handle_video_preset_mutation(event, "delete", tail)
            yield event.plain_result(f"{'✅' if success else '❌'} {message}")
            return
        success, body = self._video_preset_single_detail_text(text)
        if success:
            yield event.plain_result(body + "\n\n使用：/视频 " + text)
        else:
            body, _, _ = self._video_preset_list_text(1)
            yield event.plain_result(body + "\n\n用法：/视频预设 名称、/视频预设 添加 名称:提示词、/视频预设 删除 名称")

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
        """管理员添加预设。格式：名称:内容"""
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
        """管理员删除指定预设。"""
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
        self._remember_llm_generation(
            event,
            "image",
            {
                "prompt": prompt,
                "count": count,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "size": size,
            },
        )
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
        result = await self._background_draw_batches(
            "llm-generate-image",
            event,
            prompt,
            aspect,
            resol,
            refs,
            "llm-generate-image",
            requested_count,
            passthrough=True,
            fail_label=self._natural_fail_fallback("image"),
        )
        if not result.get("success") and not result.get("files"):
            return self._tool_soft_fail(str(result.get("error") or ""), self._natural_fail_fallback("image"))
        return self._tool_success("image", len(result.get("files") or []) or requested_count)

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
        self._remember_llm_generation(
            event,
            "selfie",
            {
                "action": action,
                "count": count,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "size": size,
            },
        )
        requested_count = self._normalize_count(count)
        action, aspect, resol, _, _ = self._resolve_image_preset(action or "看着镜头自然自拍", aspect_ratio, resolution or size)
        return await self._run_llm_selfie_flow(event, action, requested_count, aspect, resol, ack_message)

    @LLM_TOOL(name="generate_video")
    async def tool_generate_video(
        self,
        event: AstrMessageEvent,
        prompt: str,
        duration: int = 5,
        ack_message: str = "",
    ) -> Optional[str]:
        """
        生成短视频。用户附图或引用图时使用该图片作为首帧；明确要求 AI 自己出镜时使用当前形象图，普通视频请求不自动带入形象图。
        用户要求 AI 自己动态、自拍视频、让 AI 出镜动作时使用本工具；普通场景视频直接按文字生成。
        Args:
            prompt(string): 视频内容，描述动作、镜头、场景和光线。
            duration(number): 视频时长，1-60 秒；留空使用 5 秒。
            ack_message(string): 可选。根据当前对话和机器人人格生成的简体中文短进度回复。
        """
        if not self.config.video_enable:
            return self._tool_unavailable("我这会儿还没法录这个给你看。")
        self._remember_llm_generation(event, "video", {"prompt": prompt, "duration": duration})
        action = str(prompt or "").strip()
        if not action:
            return self._tool_soft_fail("缺少视频内容", "你想让我怎么动？")
        seconds = max(1, min(60, int(duration or self.config.video_default_duration or 5)))
        refs = await self._event_reference_images(
            event,
            include_at_avatar=False,
            context_hint=action,
            allow_context_fallback=False,
        )
        if not refs and self._video_prompt_requests_persona(action):
            persona_ref = self._configured_video_persona_reference()
            if not persona_ref:
                return self._tool_soft_fail(
                    "这段视频要求使用当前形象图，但还没有设置形象图",
                    "请先上传形象图，再让我生成这段视频。",
                )
            refs = [persona_ref]
        await self._send_progress_text(
            event,
            await self._build_contextual_progress_text(event, "video", action, 1, ack_message),
        )
        result = await self._run_video_generation(event, action, refs, source="llm-generate-video", duration=seconds)
        if not result.get("success"):
            return self._tool_soft_fail(str(result.get("error") or ""), self._natural_fail_fallback("video"))
        path = str(result.get("video_path") or "")
        if path:
            await self._send_generated_video(event, path, caption="视频好了。")
        return self._tool_success("video", 1)

    @LLM_TOOL(name="retry_last_generation")
    async def tool_retry_last_generation(
        self,
        event: AstrMessageEvent,
        feedback: str = "",
    ) -> Optional[str]:
        """
        重新生成本会话最近一次图片、自拍或视频。
        用户说“再来”“重试”“重新生成”“再试一次”等，或明确指出上一张的问题时必须使用本工具，不能只回复文字。
        feedback 填用户对上一张的明确修改要求，例如“更年轻一点”“不像我”“衣服不对”。
        Args:
            feedback(string): 可选。用户对上一轮结果的具体修正要求；留空时按原要求重新生成。
        """
        previous = self._last_llm_generation(event, feedback)
        kind = str(previous.get("kind") or "")
        params = previous.get("params") if isinstance(previous.get("params"), dict) else {}
        if kind == "image":
            return await self.tool_generate_image(event, **params)
        if kind == "selfie":
            return await self.tool_generate_selfie(event, **params)
        if kind == "video":
            return await self.tool_generate_video(event, **params)
        return self._tool_soft_fail("没有找到本会话最近一次生成请求", "你想重新来哪一张？")
