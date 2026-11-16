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
import shutil
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Dict, Iterable, List, Optional, Tuple

import aiohttp

try:
    from PIL import Image as PILImage
    from PIL import ImageDraw, ImageFont
except Exception:  # Pillow is bundled by AstrBot but keep local imports optional.
    PILImage = None
    ImageDraw = None
    ImageFont = None

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
    from astrbot.api.provider import LLMResponse, ProviderRequest
except ImportError:  # Older AstrBot versions do not expose provider hook types.
    LLMResponse = Any
    ProviderRequest = Any

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
from .core.async_compat import cancel_task_once
from .features.access_policy import (
    access_status,
    blocked_prompt_word,
    permission_denied_message,
)
from .features.qq_group_cache import QQGroupCache
from .features.context_routing import (
    compact_followup_text,
    format_context_for_llm,
    looks_like_clothes_followup,
    looks_like_context_image_reference,
    looks_like_edit_bot_result_followup,
    recent_context_image_sources,
)
from .cos.cos_looks import (
    COS_LOOK_CATEGORY_TERMS,
    COS_LOOK_SERIES_ALIASES,
    COS_LOOK_SETS,
    _cos_item_terms,
    adapt_cos_outfit_for_camera,
    build_cos_third_person_prompt,
    build_cos_look_action,
    cos_query_has_series_constraint,
    finalize_cos_prompt_for_request,
    format_cos_look_list,
    is_special_cos_look,
    list_cos_look_sets,
    looks_like_cos_prompt,
    match_cos_look_sets,
    parse_cos_look_list_request,
    parse_requested_cos_camera,
    pick_cos_camera,
    pick_cos_look_set,
)
from .generation.generator import generate_image_with_fallback
from .generation.image_workflow import run_image_generation_workflow
from .generation.counted_workflow import CountedGenerationWorkflow
from .generation.image_command_service import ImageCommandService
from .generation.image_prompt_service import ImagePromptService
from .generation.runtime_service import (
    GenerationRuntimeService,
    IMAGE_BATCH_REQUEST_COOLDOWN_SECONDS,
    _IMAGE_JOB_ALREADY_SCHEDULED,
)
from .generation.selfie_batch_workflow import SelfieBatchWorkflow
from .generation.selfie_workflow import SelfieWorkflowService
from .generation.web_test_workflow import WebTestImageWorkflow
from .generation.image_scheduler import ImageJobScheduler
from .generation.model_status_report import ImageStatusService
from .generation.help_poster import ensure_help_images
from .generation.task_service import GenerationTaskService
from .features.framework_persona import resolve_framework_persona
from .generation.generation_records import metric_window_seconds
from .generation.generation_store import GenerationStoreMixin, RECORD_KEEP_LIMIT
from .generation.generation_results import (
    batch_failure_policy,
    batch_failure_text,
    batch_success_text,
    normalize_generation_result,
)
from .cos.leg_focus import (
    LegFocusDataError,
    build_leg_focus_action,
    ensure_leg_focus_action,
    has_leg_focus_contract,
)
from .features.model_selection import (
    available_model_labels,
    find_model_target,
    match_model_label,
    match_model_request,
    prioritize_model_target,
)
from .prompts.preset import (
    ImagePresetManager,
    preset_display_sort_key,
    preset_group_label,
)
from .core.models import (
    AICatConfig,
    DEFAULT_CONFIG,
    ImageModelTarget,
    usable_api_keys,
)
from .features.persona import PersonaManager, current_period
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
from .nai.artist_presets import NaiArtistPresetManager
from .nai.commands import NaiCommandMixin
from .prompts.prompt_composition import (
    append_anatomy_constraints,
    build_prompt_with_reference_instruction,
)
from .prompts.prompt_translation import parse_prompt_en_response
from .features.audit_pipeline import AuditMixin
from .features.creative_features import (
    CreativeFeaturesMixin,
    apply_retry_strategy,
    parse_variation_request,
)
from .features.reference_collector import (
    CosReferenceSelection,
    extract_structured_image_sources,
    select_group_references,
    select_cos_references,
)
from .features.reference_media import ReferenceMediaMixin
from .features.reference_media import _image_delivery_is_ambiguous
from .prompts.response_text import (
    ack_repeats_request,
    clean_ack_message,
    command_model_name,
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
from .prompts.group_prompt_pool import has_group_composition_contract
from .core.proxy import (
    LOCAL_IMAGE_WAIT_SECONDS,
    ClientSessionRegistry,
    channel_client_session,
    http_proxy_url,
    image_client_timeout,
    target_session_proxy,
)
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
    image_validation_limits,
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


def optional_event_message_type(priority: int = 100):
    decorator = getattr(filter, "event_message_type", None)
    event_type = getattr(getattr(filter, "EventMessageType", None), "ALL", None)
    if callable(decorator) and event_type is not None:
        return decorator(event_type, priority=priority)

    def passthrough(func):
        return func

    return passthrough


def optional_filter_hook(name: str):
    """Use a lifecycle hook when the host supports it, otherwise stay loadable."""
    decorator = getattr(filter, str(name or ""), None)
    if callable(decorator):
        try:
            return decorator()
        except TypeError:
            return decorator

    def passthrough(func):
        return func

    return passthrough


def _appearance_type_for_request(persona: Any, text: str = "") -> str:
    try:
        from .prompts.prompt_templates import resolve_appearance_type

        return resolve_appearance_type(persona.get_appearance_type(), text)
    except Exception:
        return "auto"


def _cached_plugin_service(plugin: Any, attribute: str, factory) -> Any:
    """Cache stateless adapters without requiring a full plugin subclass."""
    service = getattr(plugin, attribute, None)
    if getattr(service, "plugin", None) is plugin:
        return service
    service = factory(plugin)
    try:
        setattr(plugin, attribute, service)
    except Exception:
        # Legacy mixins and lightweight host probes may not allow attributes.
        pass
    return service


def _generation_task_service_for(plugin: Any) -> GenerationTaskService:
    return _cached_plugin_service(plugin, "_generation_task_service", GenerationTaskService)


def _generation_runtime_service_for(plugin: Any) -> GenerationRuntimeService:
    return _cached_plugin_service(plugin, "_generation_runtime_service", GenerationRuntimeService)


@register(PLUGIN_NAME, PLUGIN_AUTHOR, f"{PLUGIN_DISPLAY_NAME} v{PLUGIN_VERSION}", PLUGIN_VERSION)
class SelfieImagePlugin(
    StudioMixin,
    NaiCommandMixin,
    CreativeFeaturesMixin,
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
        self._image_status_service = ImageStatusService()
        self._migrate_legacy_data_dir(plugin_data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.config_path = os.path.join(self.data_dir, PLUGIN_CONFIG_FILENAME)
        self._migrate_legacy_config_file()
        self._migrate_config_file_schema()
        self.usage_path = os.path.join(self.data_dir, "usage_stats.json")
        self.records_path = os.path.join(self.data_dir, "generation_records.json")
        self.records_db_path = os.path.join(self.data_dir, "generation_records.sqlite3")
        self.media_sources_dir = os.path.join(self.data_dir, "media_sources")
        self.tasks_path = os.path.join(self.data_dir, "generation_tasks.json")
        self.generated_dir = os.path.join(self.data_dir, "image_cache")
        os.makedirs(self.generated_dir, exist_ok=True)
        os.makedirs(self.media_sources_dir, exist_ok=True)
        self._plugin_root = os.path.dirname(os.path.abspath(__file__))
        self._bundled_logo_path = os.path.join(self._plugin_root, "logo.png")
        self._dashboard_logo_path = os.path.join(
            self._plugin_root, "pages", "dashboard", "logo.png"
        )
        self._sync_dashboard_logo()
        self._help_image_paths: Dict[str, str] = {"quick": "", "full": ""}

        self._native_config = config if hasattr(config, "save_config") else None
        self._native_config_path = str(getattr(config, "config_path", "") or "")
        self._config_lock = threading.RLock()
        self._usage_lock = threading.RLock()
        self._quota_reservation_lock = threading.RLock()
        self._quota_reservations: Dict[str, Dict[str, Any]] = {}
        self._records_lock = threading.RLock()
        self._progress_lock = threading.RLock()
        self._progress_last_sent: Dict[str, float] = {}
        self._context_lock = threading.RLock()
        self._conversation_context: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
        self._context_max_messages = 40
        self._context_max_sessions = 100
        self._llm_generation_lock = threading.RLock()
        self._last_llm_generations: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        native_config = self._config_object_to_dict(config)
        if not native_config and self._native_config_path:
            native_config = load_json_file(self._native_config_path)
        self.key_config = self._extract_native_key_config(native_config)
        self.raw_config, self.config = self._load_initial_config_state()
        self._records: List[Dict[str, Any]] = self._load_records()
        self._record_seq = self._restore_record_sequence()
        self._web_task_lock = threading.RLock()
        self._web_tasks: Dict[str, Dict[str, Any]] = self._load_web_tasks()
        self._web_task_seq = 0
        # Reconcile tasks that were persisted as active before a process
        # restart.  The queue cannot safely resume an in-flight provider
        # request, so the task loader marks it expired and this pass links a
        # single durable record for history/retry visibility.
        self.reconcile_expired_tasks_after_restart()
        self._runtime_generation_tasks: Dict[str, Any] = {}
        self._runtime_generation_workers: Dict[str, asyncio.Task] = {}
        self._terminating = False
        # 模型选择仅作用于当前会话。
        self._session_model_lock = threading.RLock()
        self._session_model_overrides: Dict[str, str] = {}
        self._last_request_at: Dict[str, float] = {}
        self._channel_health: Dict[str, Dict[str, Any]] = {}
        self._channel_health_lock = threading.RLock()
        self._send_failures: Dict[str, List[str]] = {}
        self._send_failures_lock = threading.RLock()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        self.qq_group_cache = QQGroupCache(self)
        self._load_persisted_conversation_context()
        self.persona = PersonaManager(self.data_dir)
        _wardrobe_dimension, _wardrobe_pixels, _wardrobe_frames = image_validation_limits(self.config)
        self.persona.configure_wardrobe_limits(
            max_bytes=self.config.image_max_image_size_mb * 1024 * 1024,
            max_dimension=_wardrobe_dimension,
            max_pixels=_wardrobe_pixels,
            max_frames=_wardrobe_frames,
        )
        self.presets = ImagePresetManager(self.data_dir)
        self.nai_artist_presets = NaiArtistPresetManager(self.data_dir)
        from .cos.cos_pool import CosPoolStore
        from .generation.asset_collections import AssetCollectionStore

        self.cos_pool = CosPoolStore(self.data_dir)
        self.studio = StudioStore(self.data_dir)
        # The legacy creation canvas has its own session namespace and file;
        # it must never appear in the relationship/infinite canvas list.
        self.creative_canvas = StudioStore(
            os.path.join(self.data_dir, "creative_canvas"),
            canvas_mode="creative",
        )
        self.asset_collections = AssetCollectionStore(self.data_dir)
        self._usage_stats = self._load_usage_stats()
        self._image_scheduler = ImageJobScheduler(self.config.image_max_concurrent_tasks)
        self._image_session_registry = ClientSessionRegistry()
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
        self.log_config_preflight()
        try:
            self._help_image_paths = dict(
                await asyncio.to_thread(
                    ensure_help_images,
                    self.data_dir,
                    logo_path=self._bundled_logo_path,
                    font_loader=self._status_report_font,
                )
            )
        except Exception as exc:
            logger.warning("[SelfieImage] 启动帮助图生成失败：%s", redact_sensitive_text(str(exc)))
        try:
            cleanup = self._cleanup_image_cache_if_needed()
            deleted = cleanup.get("deleted") if isinstance(cleanup, Mapping) else []
            if deleted:
                logger.info("[SelfieImage] startup cache cleanup removed %d orphan file(s)", len(deleted))
        except Exception as exc:
            logger.warning(f"[SelfieImage] 启动缓存清理失败：{exc}")
        self._start_web_server()

    def get_http_session_health(self) -> Dict[str, Any]:
        """Expose credential-free image transport-pool counters to health APIs."""
        registry = getattr(self, "_image_session_registry", None)
        getter = getattr(registry, "get_stats", None)
        if not callable(getter):
            return {"status": "unavailable", "active_sessions": 0}
        try:
            return dict(getter() or {})
        except Exception:
            return {
                "status": "error",
                "active_sessions": 0,
                "last_failure_reason": "HTTP 会话池健康信息读取失败",
            }

    async def terminate(self) -> None:
        with self._web_task_lock:
            self._terminating = True
            for task in self._web_tasks.values():
                if task.get("status") in {"queued", "running"}:
                    task["cancel_requested"] = True
            runtime_tasks = dict(getattr(self, "_runtime_generation_tasks", {}))
            handles = list(runtime_tasks.values())
        # Stop Flask without blocking cancellation work on the bot event loop.
        await asyncio.to_thread(self.web_server.stop)
        for handle in handles:
            if isinstance(handle, asyncio.Task):
                cancel_task_once(handle)
            elif not handle.done():
                handle.cancel()
        # A concurrent Future becomes cancelled before its coroutine has
        # unwound. Await the actual loop workers, not just the proxy handles.
        await asyncio.sleep(0)
        workers_by_id = dict(getattr(self, "_runtime_generation_workers", {}))
        workers = set(workers_by_id.values())
        workers.update(handle for handle in handles if isinstance(handle, asyncio.Task))
        cancelled_proxies = {
            task_id
            for task_id, handle in runtime_tasks.items()
            if not isinstance(handle, asyncio.Task)
            and callable(getattr(handle, "cancelled", None))
            and handle.cancelled()
        }
        for task_id, worker in workers_by_id.items():
            if task_id not in cancelled_proxies:
                cancel_task_once(worker)
        for worker in workers.difference(workers_by_id.values()):
            cancel_task_once(worker)
        await _generation_runtime_service_for(self).close_scheduler()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        session_registry = getattr(self, "_image_session_registry", None)
        if session_registry is not None:
            await session_registry.close()
        await self._close_reference_client_session()
        commits = set(getattr(self, "_record_commit_tasks", set()))
        commits.update(getattr(self, "_usage_commit_tasks", set()))
        commits.update(future for bucket in getattr(self, "_pending_record_commits", {}).values() for future in bucket)
        commits.update(future for bucket in getattr(self, "_pending_image_record_commits", {}).values() for future in bucket)
        if commits:
            await asyncio.gather(*commits, return_exceptions=True)
        with self._web_task_lock:
            unfinished = [tid for tid, task in self._web_tasks.items() if task.get("status") in {"queued", "running"}]
        for task_id in unfinished:
            self._release_quota_reservation(task_id)
            self._set_web_image_task(
                task_id, status="cancelled", success=False, error="插件卸载，任务已取消",
                generation_stage="cancelled", generation_stage_label="已取消",
                finished_ts=time.time(), finished_at=self._web_task_timestamp(),
            )
        await asyncio.to_thread(self._flush_web_task_snapshots)

    def _flush_web_task_snapshots(self) -> None:
        with self._web_task_lock:
            try:
                self._persist_web_tasks_locked()
            except Exception as exc:
                # A quarantined or otherwise unreadable snapshot must not
                # prevent the rest of plugin shutdown from completing.
                logger.error("[SelfieImage] 任务快照最终写入失败: %s", exc)

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
        with self._usage_lock:
            stats = self._current_usage_stats()
            users = stats.setdefault("users", {})
            record = users.setdefault(user_id, {"count": 0, "last_at": 0})
            record["count"] = int(record.get("count", 0)) + max(0, int(count))
            record["last_at"] = int(time.time())
            record["group_id"] = event_group_id(event)
            self._persist_usage_stats()

    async def _record_generated_images_async(self, event: AstrMessageEvent, count: int) -> None:
        task = asyncio.create_task(asyncio.to_thread(self._record_generated_images, event, count))
        pending = getattr(self, "_usage_commit_tasks", None)
        if pending is None:
            pending = set()
            self._usage_commit_tasks = pending
        pending.add(task)

        def commit_finished(future: asyncio.Future) -> None:
            pending.discard(future)
            if not future.cancelled():
                exc = future.exception()
                if exc is not None:
                    logger.error("[SelfieImage] 额度统计写入失败: %s", exc)

        task.add_done_callback(commit_finished)
        await asyncio.shield(task)

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
                source="event",
            )
        except Exception as exc:
            logger.debug(f"[SelfieImage] 记录上下文失败: {exc}")
        return None

    @optional_filter_hook("on_llm_request")
    async def on_llm_request_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """同步 AstrBot 官方会话上下文，补齐未经过消息记录钩子的当前消息。"""
        try:
            self._sync_provider_context(event, req)
            if event is None:
                return
            msg = self._extract_context_message_info(event)
            sender_id = event_user_id(event)
            bot_ids = set(self._bot_account_ids(event))
            is_bot = bool(sender_id and sender_id in bot_ids)
            self._add_context_message(
                session_key=self._provider_context_session_key(event, req),
                sender_id=sender_id,
                sender_name=self._event_sender_name(event, is_bot=is_bot),
                content=str(msg.get("content") or ""),
                is_bot=is_bot,
                image_sources=list(msg.get("image_sources") or []),
                msg_id=self._event_message_id(event),
                source="llm_request",
            )
        except Exception as exc:
            logger.debug(f"[SelfieImage] 同步 AstrBot 请求上下文失败: {exc}")

    @optional_filter_hook("on_llm_response")
    async def on_llm_response_context(
        self, event: AstrMessageEvent, resp: LLMResponse
    ) -> None:
        """记录完成的 assistant 回复，跳过工具调用中间轮和流式分片。"""
        try:
            self._record_llm_response_context(event, resp)
        except Exception as exc:
            logger.debug(f"[SelfieImage] 记录 AstrBot 回复上下文失败: {exc}")

    def _access_status(self, event: AstrMessageEvent) -> Dict[str, Any]:
        user_id = event_user_id(event)
        group_id = event_group_id(event)
        status = access_status(
            user_id=user_id,
            group_id=group_id,
            blocked_users=self.config.blocked_users,
            usable_users=self.config.usable_users,
            whitelist_users=self.config.whitelist_users,
            whitelist_groups=self.config.whitelist_groups,
            group_chat_configs=self.config.group_chat_configs,
            usable_users_enabled=self.config.usable_users_enabled,
        )
        # Administrators get the same audit/quota exemptions as configured
        # unlimited users, while explicit access denials still take priority.
        if status.get("allowed") and self._is_admin_event(event):
            status["unlimited"] = True
            status["admin"] = True
        return status

    async def list_qq_groups_for_web(self, *, force: bool = False) -> Dict[str, Any]:
        configured_groups = dict(self.config.group_chat_configs)
        configured_groups.update({group_id: {} for group_id in self.config.whitelist_groups})
        payload = await self.qq_group_cache.list_groups(configured_groups, force=force)
        configs = self.config.group_chat_configs
        legacy_whitelist = set(self.config.whitelist_groups)
        for row in payload.get("groups", []):
            group_id = str(row.get("group_id") or "")
            row["config"] = copy.deepcopy(configs.get(group_id, {
                "chat_type": "white" if group_id in legacy_whitelist else "none",
                "model_priority_mode": "default",
                "fixed_model": "",
            }))
        payload["models"] = [target.label for target in self.config.get_enabled_image_targets()]
        payload["bot_ids"] = self._bot_account_ids()
        return payload

    def update_group_chat_configs_from_web(self, payload: Any) -> Dict[str, Any]:
        rows = payload.get("groups") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError("groups 必须是数组")
        configs: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            group_id = str(row.get("group_id") or "").strip()
            if not group_id.isdigit():
                continue
            config = row.get("config") if isinstance(row.get("config"), dict) else row
            configs[group_id] = {
                "chat_type": str(config.get("chat_type") or "none"),
                "model_priority_mode": str(config.get("model_priority_mode") or "default"),
                "fixed_model": str(config.get("fixed_model") or "").strip(),
            }
        patch: Dict[str, Any] = {}
        if configs:
            patch["group_chat_configs"] = configs
        if isinstance(payload, dict) and isinstance(payload.get("permission"), dict):
            permission = payload["permission"]
            patch["permission"] = {
                key: permission[key]
                for key in (
                    "usable_users", "usable_users_enabled",
                    "blocked_users", "blocked_user_reasons",
                    "whitelist_users", "whitelist_user_reasons",
                )
                if key in permission
            }
        updated = self.update_config_from_web(patch) if patch else self.get_config_for_web()
        return {
            "group_chat_configs": self.config.group_chat_configs,
            "permission": updated.get("permission", {}),
        }

    def _permission_denied_message(self, event: AstrMessageEvent) -> str:
        return permission_denied_message(self._access_status(event))

    @staticmethod
    def _parse_access_user_command(event: AstrMessageEvent, payload: str) -> Tuple[str, str]:
        target = ""
        message_obj = getattr(event, "message_obj", None)
        segments = getattr(message_obj, "message", None) or getattr(event, "message", None) or []
        if not isinstance(segments, (list, tuple)):
            segments = []
        for segment in segments:
            segment_type = str(getattr(segment, "type", "") or type(segment).__name__).lower()
            data = getattr(segment, "data", None)
            if not isinstance(data, Mapping):
                data = {}
            if segment_type in {"at", "messagecomponenttype.at"} or type(segment).__name__.lower() == "at":
                candidate = getattr(segment, "qq", None) or getattr(segment, "user_id", None) or data.get("qq") or data.get("user_id")
                if candidate:
                    target = str(candidate).strip()
                    break

        text = str(payload or "").strip()
        mention_patterns = (
            r"\[CQ:at,qq=(\d+)(?:,[^\]]*)?\]",
            r"\[At:(\d+)\]",
            r"<at:(\d+)>",
            r"@([0-9]+)",
        )
        if not target:
            for pattern in mention_patterns:
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if match:
                    target = match.group(1)
                    break
        if not target:
            match = re.search(r"(?<!\w)(\d+)(?!\w)", text)
            if match:
                target = match.group(1)
        if not target or not target.isdigit():
            return "", ""

        cleaned = text
        for pattern in mention_patterns:
            cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(rf"(?<!\w){re.escape(target)}(?!\w)", " ", cleaned, count=1)
        reason = re.sub(r"^[\s,，:：|]+|[\s,，:：|]+$", "", cleaned).strip()
        return target, reason

    @staticmethod
    def _access_user_ids(value: Any) -> List[str]:
        values = value if isinstance(value, (list, tuple, set)) else re.split(r"[\s,|]+", str(value or ""))
        return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))

    def _mutate_access_user(self, action: str, event: AstrMessageEvent, payload: str) -> Tuple[bool, str]:
        target, reason = self._parse_access_user_command(event, payload)
        if not target:
            return False, "请提供 QQ 号或 @ 用户。"
        reason = reason[:200]

        permission = copy.deepcopy(self.raw_config.get("permission") or {})
        blocked = self._access_user_ids(permission.get("blocked_users"))
        whitelisted = self._access_user_ids(permission.get("whitelist_users"))
        blocked_reasons = permission.get("blocked_user_reasons")
        whitelist_reasons = permission.get("whitelist_user_reasons")
        blocked_reasons = copy.deepcopy(blocked_reasons) if isinstance(blocked_reasons, dict) else {}
        whitelist_reasons = copy.deepcopy(whitelist_reasons) if isinstance(whitelist_reasons, dict) else {}

        if action == "black":
            whitelisted = [user_id for user_id in whitelisted if user_id != target]
            whitelist_reasons.pop(target, None)
            if target not in blocked:
                blocked.append(target)
            if reason:
                blocked_reasons[target] = reason
            label = "黑名单"
        elif action == "white":
            blocked = [user_id for user_id in blocked if user_id != target]
            blocked_reasons.pop(target, None)
            if target not in whitelisted:
                whitelisted.append(target)
            if reason:
                whitelist_reasons[target] = reason
            label = "白名单"
        elif action == "release":
            blocked = [user_id for user_id in blocked if user_id != target]
            whitelisted = [user_id for user_id in whitelisted if user_id != target]
            blocked_reasons.pop(target, None)
            whitelist_reasons.pop(target, None)
            label = "已释放"
        else:
            return False, "未知的名单操作。"

        self.update_config_from_web({"permission": {
            "blocked_users": blocked,
            "blocked_user_reasons": blocked_reasons,
            "whitelist_users": whitelisted,
            "whitelist_user_reasons": whitelist_reasons,
        }})
        detail = f"（{reason}）" if reason and action != "release" else ""
        return True, f"{target} 已加入{label}{detail}。" if action != "release" else f"{target} 已从黑名单和白名单释放。"

    @staticmethod
    def _claim_media_command_event(event: AstrMessageEvent) -> None:
        """Keep an explicit media command out of AstrBot's default LLM stage."""
        should_call_llm = getattr(event, "should_call_llm", None)
        if callable(should_call_llm):
            should_call_llm(True)
            return
        # AstrBot versions before ``should_call_llm`` exposed the flag directly.
        try:
            event.call_llm = True
        except Exception:
            pass

    def _quota_error_message(self, event: AstrMessageEvent, requested_count: int = 1) -> str:
        return _generation_task_service_for(self).quota_error_message(event, requested_count)

    def _reserve_quota_for_task(
        self,
        task_id: str,
        event: AstrMessageEvent,
        requested_count: int,
        *,
        media_type: str = "image",
    ) -> str:
        return _generation_task_service_for(self).reserve_quota_for_task(
            task_id, event, requested_count, media_type=media_type
        )

    def _release_quota_reservation(self, task_id: str) -> None:
        _generation_task_service_for(self).release_quota_reservation(task_id)

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
        return bool(
            status.get("whitelist")
            or status.get("admin")
            or (user_id and user_id in self.config.whitelist_users)
        )

    def _is_audit_exempt(self, event: Optional[AstrMessageEvent] = None, user_id: str = "") -> bool:
        return bool(event is not None and self._is_whitelisted(event, user_id))

    def _validate_prompt(self, prompt: str, user_id: str = "", event: Optional[AstrMessageEvent] = None) -> str:
        if self._is_audit_exempt(event, user_id):
            return ""
        return blocked_prompt_word(prompt, self.config.image_blocked_words)

    def _bot_display_name(self) -> str:
        return "助手"

    def _compact_for_repeat_check(self, text: str) -> str:
        return compact_for_repeat_check(text)

    def _ack_repeats_request(self, ack_message: str, user_request: str) -> bool:
        return ack_repeats_request(ack_message, user_request)

    def _looks_like_non_chinese_ack(self, text: str) -> bool:
        return looks_like_non_chinese_ack(text)

    def _clean_ack_message(self, ack_message: str, user_request: str) -> str:
        return clean_ack_message(ack_message, user_request)

    def _natural_ack_fallback(self, kind: str, count: int) -> str:
        return natural_ack_fallback(kind, count)

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
            "lookup_scope": self._context_session_key(event) if event is not None else "",
            "chat_type": "group" if gid else ("private" if uid else "web"),
        }

    def _image_parser_defaults(self) -> Tuple[str, str]:
        return self._get_image_prompt_service().image_parser_defaults()

    def _normalize_count(self, count: Any) -> int:
        return self._get_image_prompt_service().normalize_count(count)

    def _parse_count_token(self, token: str) -> int:
        return self._get_image_prompt_service().parse_count_token(token)

    def _split_attached_count_token(self, token: str) -> Tuple[str, int]:
        return self._get_image_prompt_service().split_attached_count_token(token)

    def _command_tokens_for_count(self, text: str) -> List[str]:
        return self._get_image_prompt_service().command_tokens_for_count(text)

    def _extract_command_count(
        self,
        text: str,
        *,
        allow_attached: bool = False,
        allow_trailing: bool = False,
    ) -> Tuple[str, int]:
        return self._get_image_prompt_service().extract_command_count(
            text,
            allow_attached=allow_attached,
            allow_trailing=allow_trailing,
        )

    def _render_command_template(self, text: str) -> Tuple[str, Dict[str, str], bool, List[str]]:
        return self._get_image_prompt_service().render_command_template(text)

    def _parse_prompt_options(
        self,
        text: str,
        aspect_ratio: str = "",
        resolution: str = "",
    ) -> Tuple[str, str, str]:
        return self._get_image_prompt_service().parse_prompt_options(
            text,
            aspect_ratio,
            resolution,
        )

    def _resolve_image_preset(
        self,
        prompt: str,
        aspect_ratio: str = "",
        resolution: str = "",
    ) -> Tuple[str, str, str, str, str]:
        return self._get_image_prompt_service().resolve_image_preset(
            prompt,
            aspect_ratio,
            resolution,
        )

    def _expand_user_text_with_preset(
        self, raw_text: str
    ) -> Tuple[str, str, str, str]:
        return self._get_image_prompt_service().expand_user_text_with_preset(raw_text)

    def _expand_cos_user_text_with_preset(
        self, raw_text: str
    ) -> Tuple[str, str, str, str]:
        return self._get_image_prompt_service().expand_cos_user_text_with_preset(raw_text)

    def _expand_cos_special_preset_variants(
        self,
        raw_text: str,
        count: int,
    ) -> Tuple[List[str], str, str, str]:
        return self._get_image_prompt_service().expand_cos_special_preset_variants(raw_text, count)

    def _expand_dynamic_preset_variants(
        self,
        raw_text: str,
        count: int,
    ) -> Tuple[List[str], str, str, str]:
        return self._get_image_prompt_service().expand_dynamic_preset_variants(raw_text, count)

    def _normalize_preset_input(self, text: str) -> str:
        return self._get_image_prompt_service().normalize_preset_input(text)

    def _split_preset_command(self, text: str) -> Tuple[str, str]:
        return self._get_image_prompt_service().split_preset_command(text)

    async def _send_progress_text(self, event: AstrMessageEvent, text: str) -> None:
        if not self._progress_text_allowed(event):
            return
        try:
            await event.send(event.plain_result(text))
            self._record_bot_text_context(event, text)
        except Exception as exc:
            logger.warning(f"[SelfieImage] 发送进度消息失败: {exc}")

    async def _send_task_notification(
        self,
        task_id: str,
        event: AstrMessageEvent,
        text: str,
    ) -> bool:
        """Send a terminal task message and persist its delivery outcome."""
        try:
            await event.send(event.plain_result(text))
        except Exception as exc:
            reason = redact_sensitive_text(str(exc))[:320]
            setter = getattr(self, "_set_task_notification_status", None)
            if callable(setter):
                setter(task_id, "failed", reason)
            logger.warning(f"[SelfieImage] 任务 {task_id} 通知发送失败: {reason}")
            return False
        setter = getattr(self, "_set_task_notification_status", None)
        if callable(setter):
            setter(task_id, "sent")
        return True

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
        *,
        use_persona: bool = False,
    ) -> str:
        image_urls = [
            bytes_to_data_url(image, detect_mime_by_bytes(image))
            for image in (images or [])
            if image
        ]

        async def request() -> str:
            origin = getattr(event, "unified_msg_origin", None)
            framework_persona = await resolve_framework_persona(self.context, event) if use_persona else None
            system_prompt = framework_persona.prompt if framework_persona else ""
            provider_id = None
            try:
                getter = getattr(self.context, "get_using_provider", None)
                if callable(getter):
                    provider = getter(umo=origin) if origin else getter()
                    requester = getattr(provider, "text_chat", None) or getattr(provider, "request", None)
                    if callable(requester):
                        kwargs: Dict[str, Any] = {"prompt": prompt, "system_prompt": system_prompt}
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
                    kwargs = {"prompt": prompt, "system_prompt": system_prompt}
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
        context_text = self._format_context_for_llm(event, count=12, max_chars=1400)
        request = str(user_request or "").strip()
        kind_text = "自拍/拍照" if kind == "selfie" else "图片请求"
        count_text = "多张" if count > 1 else "一张"
        return "\n".join(
            [
                "延续当前对话，给用户一句自然回复。",
                f"用户刚通过指令发起了{kind_text}，数量：{count_text}。",
                "请只输出一句简体中文短回复，像正在聊天时随口接一句。",
                "要求：10-32 个汉字；结合最近上下文；不要复述用户提示词；不要解释；不要列点。",
                "禁止出现：生成、绘制、渲染、工具、提示词、配置、审核、任务、处理中、已收到、开始、为你。",
                "不要复述人格设定或编造身份；按当前对话自然接一句。",
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
        text = self._strip_llm_short_reply(await self._call_text_llm(event, prompt, timeout=7, use_persona=True))
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
            source="plugin",
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
                source="plugin",
            )

    def _is_admin_event(self, event: AstrMessageEvent) -> bool:
        role = str(getattr(event, "role", "") or "").lower().strip()
        if role in {"admin", "owner"}:
            return True
        sender = getattr(event, "sender", None)
        sender_role = str(getattr(sender, "role", "") or "").lower().strip()
        return sender_role in {"admin", "owner"}

    def _preset_list_text(self, page: int = 1, page_size: int = 20) -> Tuple[str, int, int]:
        presets = sorted(self.presets.list(), key=lambda item: preset_display_sort_key(item[0]))
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
            last_group = None
            for idx, (name, _) in enumerate(items, start=start + 1):
                group = preset_group_label(name) or "普通预设"
                if group != last_group:
                    lines.append(f"【{group}】")
                    last_group = group
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
        presets = sorted(self.presets.list(), key=lambda item: preset_display_sort_key(item[0]))
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
            last_group = None
            for idx, (name, preset) in enumerate(items, start=start + 1):
                group = preset_group_label(name) or "普通预设"
                if group != last_group:
                    lines.append(f"【{group}】")
                    last_group = group
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

    async def _ensure_daily_selfie_schedule(self, action: str, has_refs: bool) -> None:
        if has_refs:
            return
        persona = getattr(self, "persona", None)
        ensure = getattr(persona, "ensure_daily_selfie_profile", None)
        analyze = getattr(persona, "analyze_selfie_intent", None)
        if not callable(ensure) or not callable(analyze):
            return
        intent = analyze(str(action or ""))
        if intent.is_cos_look or intent.is_group_photo or intent.is_legs_only:
            return
        await ensure(action)

    def _build_leg_focus_action(
        self,
        extra_request: str = "",
        has_refs: bool = False,
        *,
        avoid_pose: str = "",
        force_legwear: str = "",
        force_pose: str = "",
        force_camera: str = "",
        force_view: str = "",
        avoid_combinations: Optional[set[Tuple[str, str]]] = None,
    ) -> str:
        return build_leg_focus_action(
            extra_request,
            has_refs,
            avoid_pose=avoid_pose,
            force_legwear=force_legwear,
            force_pose=force_pose,
            force_camera=force_camera,
            force_view=force_view,
            avoid_combinations=avoid_combinations,
        )

    def _normalize_selfie_action(self, action: str, has_refs: bool) -> str:
        """为腿部自拍补全单一姿势与腿部穿搭。"""
        raw = str(action or "").strip()
        pose_match = re.search(r"【pose:([a-z_]+)】", raw)
        removed_pose = "stand_" + "topdown"
        if pose_match and pose_match.group(1) == removed_pose:
            return self._build_leg_focus_action(raw, has_refs, avoid_pose=removed_pose)
        if not self.persona.analyze_selfie_intent(raw).is_legs_only:
            return raw
        if has_leg_focus_contract(raw):
            return raw
        if "【legs:outfit】" in raw:
            return ensure_leg_focus_action(raw, has_refs)
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
        # An attached message image is the visual source for this shot. Do not
        # mix today's identity-free schedule scene into a reference-driven
        # composition.
        daily = (
            getattr(getattr(self, "persona", None), "get_daily_selfie_profile", lambda: None)()
            if not has_refs
            else None
        )
        daily_event = (
            getattr(getattr(self, "persona", None), "get_current_daily_event", lambda: None)()
            if not has_refs
            else None
        )
        period = current_period()
        return build_selfie_look_action(
            extra_request,
            has_refs,
            avoid_shot=avoid_shot,
            appearance_type=_appearance_type_for_request(getattr(self, "persona", None), extra_request),
            scene_light_provider=scene_provider,
            preferred_scene=(str(getattr(daily_event, "scene", "") or "").strip() or str(getattr(daily, "scene_by_period", {}).get(period) or "")) if daily else "",
            preferred_gesture=(str(getattr(daily_event, "action", "") or "").strip() or str(getattr(daily, "action_by_period", {}).get(period) or "")) if daily else "",
            preferred_tags=getattr(daily_event, "tags", ()) if daily_event else (),
        )

    def _build_selfie_request_action(self, extra_request: str = "", has_refs: bool = False) -> str:
        """Build one ordinary selfie action; COS pool matching belongs to /看看COS."""
        text = str(extra_request or "").strip()
        return self._build_selfie_look_action(text, has_refs)

    @staticmethod
    def _looks_like_crop_waist_request(text: str) -> bool:
        return looks_like_crop_waist_request(text)

    def _build_crop_waist_selfie_action(
        self, extra_request: str = "", has_refs: bool = False
    ) -> str:
        return build_crop_waist_selfie_action(
            extra_request,
            has_refs,
            appearance_type=_appearance_type_for_request(getattr(self, "persona", None), extra_request),
        )

    def _build_third_person_look_action(
        self,
        extra_request: str = "",
        has_refs: bool = False,
        *,
        avoid_shot: str = "",
    ) -> str:
        scene_provider = getattr(self, "_period_scene_light_pools", period_scene_light_pools)
        daily = (
            getattr(getattr(self, "persona", None), "get_daily_selfie_profile", lambda: None)()
            if not has_refs
            else None
        )
        daily_event = (
            getattr(getattr(self, "persona", None), "get_current_daily_event", lambda: None)()
            if not has_refs
            else None
        )
        period = current_period()
        return build_third_person_look_action(
            extra_request,
            has_refs,
            avoid_shot=avoid_shot,
            appearance_type=_appearance_type_for_request(getattr(self, "persona", None), extra_request),
            scene_light_provider=scene_provider,
            preferred_scene=(str(getattr(daily_event, "scene", "") or "").strip() or str(getattr(daily, "scene_by_period", {}).get(period) or "")) if daily else "",
            preferred_action=(str(getattr(daily_event, "action", "") or "").strip() or str(getattr(daily, "action_by_period", {}).get(period) or "")) if daily else "",
            preferred_tags=getattr(daily_event, "tags", ()) if daily_event else (),
        )

    def _build_group_selfie_action(
        self,
        extra_request: str = "",
        has_refs: bool = False,
        *,
        avoid_shot: str = "",
    ) -> str:
        persona = getattr(self, "persona", None)
        has_identity_reference = getattr(persona, "has_reference_image", None)
        if callable(has_identity_reference):
            has_identity_reference = bool(has_identity_reference())
        else:
            # Lightweight test/integration stubs historically exposed only
            # ``get_appearance_type``; keep their ``has_refs`` behavior while
            # real PersonaManager instances use the actual identity state.
            has_identity_reference = bool(has_refs)
        return build_group_selfie_action(
            extra_request,
            has_refs,
            avoid_shot=avoid_shot,
            appearance_type=_appearance_type_for_request(persona, extra_request),
            has_identity_reference=has_identity_reference,
        )

    def _looks_like_group_selfie_intent(self, text: str) -> bool:
        return looks_like_group_selfie_intent(text)

    def _looks_like_selfie_intent(self, text: str, bot_name: str = "") -> bool:
        return looks_like_selfie_intent(text, bot_name=bot_name)

    def _build_cos_look_action(
        self,
        extra_request: str = "",
        has_refs: bool = False,
        *,
        avoid_id: str = "",
        avoid_camera: Iterable[str] = (),
        avoid_pose: str = "",
        avoid_scene: str = "",
        avoid_framings: Iterable[str] = (),
        avoid_compositions: Iterable[str] = (),
        avoid_poses: Iterable[str] = (),
        avoid_scenes: Iterable[str] = (),
        avoid_cameras: Iterable[str] = (),
        camera: str = "",
        match_query: str = "",
        force_id: str = "",
        force_multi_view_collage: bool = False,
    ) -> str:
        appearance_type = _appearance_type_for_request(getattr(self, "persona", None), extra_request)
        chosen_force = force_id
        custom = getattr(getattr(self, "cos_pool", None), "list_custom", lambda: [])()
        custom_special_ids = {
            str(item.get("id") or "").strip()
            for item in custom
            if is_special_cos_look(item)
        }
        if not chosen_force:
            favorites = getattr(getattr(self, "cos_pool", None), "list_favorites", lambda: [])()
            favorites = [
                item for item in favorites
                if not is_special_cos_look(item) and str(item) not in custom_special_ids
            ]
            if favorites and not match_query and not extra_request:
                chosen_force = random.choice(favorites)
        custom_item = next((item for item in custom if str(item.get("id") or "") == str(chosen_force or "")), None)
        if custom_item:
            return build_cos_look_action(
                extra_request,
                has_refs,
                avoid_id=avoid_id,
                avoid_camera=avoid_camera,
                avoid_pose=avoid_pose,
                avoid_scene=avoid_scene,
                avoid_framings=avoid_framings,
                avoid_compositions=avoid_compositions,
                avoid_poses=avoid_poses,
                avoid_scenes=avoid_scenes,
                avoid_cameras=avoid_cameras,
                camera=camera,
                appearance_type=appearance_type,
                match_query="",
                force_id="",
                force_multi_view_collage=force_multi_view_collage,
                picker=lambda **_: dict(custom_item),
            )
        return build_cos_look_action(
            extra_request,
            has_refs,
            avoid_id=avoid_id,
            avoid_camera=avoid_camera,
            avoid_pose=avoid_pose,
            avoid_scene=avoid_scene,
            avoid_framings=avoid_framings,
            avoid_compositions=avoid_compositions,
            avoid_poses=avoid_poses,
            avoid_scenes=avoid_scenes,
            avoid_cameras=avoid_cameras,
            camera=camera,
            appearance_type=appearance_type,
            match_query=match_query,
            force_id=chosen_force,
            force_multi_view_collage=force_multi_view_collage,
            picker=pick_cos_look_set,
        )

    def _prepare_llm_selfie_action(
        self, action: str, has_refs: bool
    ) -> Tuple[str, str, str]:
        """Route an LLM selfie request through its mode-specific prompt builder."""
        raw_action = str(action or "").strip() or "看着镜头自然自拍"
        intent = self.persona.analyze_selfie_intent(raw_action)
        request_text = re.sub(
            r"^【LLM 自拍】\s*|^LLM\s*自拍[:：]?\s*", "", raw_action
        ).strip()
        leg_rebuild_extra = (
            request_text
            if intent.is_legs_only and "【legs:outfit】" not in raw_action
            else ""
        )
        cos_rebuild_query = ""
        if intent.is_cos_look:
            if not re.search(r"【cos:[a-z0-9_]+】", raw_action, re.I):
                cos_rebuild_query = request_text
                raw_action = self._build_cos_look_action(
                    request_text,
                    has_refs,
                    match_query=request_text,
                )
        elif intent.is_legs_only:
            raw_action = self._normalize_selfie_action(request_text, has_refs)
        elif intent.is_group_photo and not has_group_composition_contract(raw_action):
            raw_action = self._build_group_selfie_action(request_text, has_refs)
        elif intent.is_third_person_photo and not re.search(r"【shot:", raw_action):
            raw_action = self._build_third_person_look_action(request_text, has_refs)
        elif not re.search(r"【shot:", raw_action):
            raw_action = self._build_selfie_request_action(request_text, has_refs)
        return raw_action, leg_rebuild_extra, cos_rebuild_query

    async def _run_llm_selfie_flow(
        self,
        event: AstrMessageEvent,
        action: str,
        requested_count: int,
        aspect: str,
        resolution: str,
        ack_message: str = "",
        model_request: str = "",
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
        await self._ensure_daily_selfie_schedule(action, bool(extra_refs))
        cos_selection = self._select_selfie_references(action, extra_refs)
        group_selection = (
            self._select_group_references(extra_refs)
            if self._looks_like_group_selfie_intent(action) and not self._is_cos_action(action)
            else None
        )
        selection = group_selection or cos_selection
        action, leg_rebuild_extra, cos_rebuild_query = self._prepare_llm_selfie_action(
            action,
            bool(extra_refs),
        )
        async def runner(task_id: str) -> Dict[str, Any]:
            return await self._background_selfie_batches(
                task_id,
                event,
                action,
                extra_refs,
                "llm-generate-selfie",
                requested_count,
                aspect,
                resolution,
                self._natural_fail_fallback("selfie"),
                rebuild_extra_request=leg_rebuild_extra,
                rebuild_match_query=cos_rebuild_query,
                cos_selection=selection,
                reference_selection=selection.summary() if selection else None,
                model_request=model_request,
            )

        task = self.start_command_image_task(
            event,
            source="llm-generate-selfie",
            summary={
                "original_prompt": action,
                "aspect_ratio": aspect,
                "resolution": resolution,
                "requested_count": requested_count,
                "reference_image_count": len(extra_refs),
                "requested_model": str(model_request or "").strip(),
                "kind": "LLM 自拍",
            },
            runner=runner,
            reference_images=self._selfie_reference_images(action, extra_refs, selection),
        )
        result = await self._await_command_image_task(task)
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
                    lines.append("今日用量：白名单用户/群组和管理员不限制。")
                else:
                    user_id = status.get("user_id") or ""
                    used = int(self._current_usage_stats().get("users", {}).get(user_id, {}).get("count", 0))
                    lines.append(f"今日用量：{used}/{self.config.image_daily_limit_count}。")
        model_name = command_model_name(used_model)
        if self.config.image_show_model_info and model_name:
            lines.append(f"模型：{model_name}")
        return "\n".join(lines)

    def _batch_success_text(self, info: str, index: int, total: int) -> str:
        return batch_success_text(info, index, total)

    def _set_generation_stage(
        self,
        record_context: Optional[Mapping[str, Any]],
        stage: str,
        label: str,
    ) -> None:
        """Publish a live task stage when a generation record has a task link."""
        _generation_runtime_service_for(self).set_generation_stage(record_context, stage, label)

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
        record_context: Optional[Mapping[str, Any]] = None,
        reference_selection: Optional[Mapping[str, Any]] = None,
        reference_fidelity: str = "",
    ) -> Dict[str, Any]:
        """Run one image shot through the shared fair scheduler."""
        return await _generation_runtime_service_for(self).run_image_generation(
            prompt,
            aspect_ratio,
            resolution,
            refs,
            targets=targets,
            source=source,
            audit_user_id=audit_user_id,
            event=event,
            original_prompt=original_prompt,
            max_attempts=max_attempts,
            allow_compat_retry=allow_compat_retry,
            prompt_en_meta=prompt_en_meta,
            record_context=record_context,
            reference_selection=reference_selection,
            reference_fidelity=reference_fidelity,
        )

    async def _run_image_generation_unscheduled(
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
        record_context: Optional[Mapping[str, Any]] = None,
        reference_selection: Optional[Mapping[str, Any]] = None,
        reference_fidelity: str = "",
        *,
        queue_wait_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        return await run_image_generation_workflow(
            self, prompt, aspect_ratio, resolution, refs, targets, source, audit_user_id,
            event, original_prompt, max_attempts, allow_compat_retry, prompt_en_meta,
            record_context, reference_selection, reference_fidelity,
            queue_wait_seconds=queue_wait_seconds,
            generate_images=generate_image_with_fallback,
        )

    def _get_image_command_service(self) -> ImageCommandService:
        """Reuse the stateless regular-image command adapter for this plugin."""
        service = getattr(self, "_image_command_service", None)
        if getattr(service, "plugin", None) is not self:
            service = ImageCommandService(self)
            self._image_command_service = service
        return service

    def _get_image_prompt_service(self) -> ImagePromptService:
        """Reuse the parser adapter while preserving legacy plugin methods."""
        return _cached_plugin_service(self, "_image_prompt_service", ImagePromptService)

    def _get_selfie_workflow_service(self) -> SelfieWorkflowService:
        """Reuse the selfie adapter while preserving patchable parser hooks."""
        service = getattr(self, "_selfie_workflow_service", None)
        if getattr(service, "plugin", None) is not self:
            service = SelfieWorkflowService(
                self, extract_command_message, extract_image_sources_from_event
            )
            self._selfie_workflow_service = service
        else:
            # Keep compatibility with tests and hosts that replace these hooks.
            service.extract_command_message = extract_command_message
            service.extract_image_sources = extract_image_sources_from_event
        return service

    def _get_generation_task_service(self) -> GenerationTaskService:
        """Reuse the task lifecycle adapter for quota and delivery hooks."""
        return _generation_task_service_for(self)

    def _get_generation_runtime_service(self) -> GenerationRuntimeService:
        """Reuse the runtime adapter for scheduler and stage hooks."""
        return _generation_runtime_service_for(self)

    def _is_cos_action(self, action: str) -> bool:
        return self._get_selfie_workflow_service().is_cos_action(action)

    def _reference_failure_message(self, event: Any) -> str:
        getter = getattr(self, "_reference_failure_reason", None)
        reason = getter(event) if callable(getter) else ""
        if reason == "too_large":
            limit = int(getattr(self.config, "image_max_image_size_mb", 10) or 10)
            return f"参考图片过大，请压缩到 {limit}MB 以内后重新发送。"
        if reason == "invalid_or_unreadable":
            return "参考图片不够清晰或无法识别，请发送清晰的原图后重试。"
        return "参考图片无法读取，请重新发送原图后再试。"

    def _select_group_references(
        self, message_refs: List[ImageReference]
    ) -> CosReferenceSelection:
        return self._get_selfie_workflow_service().select_group_references(message_refs)

    def _select_cos_references(
        self, action: str, message_refs: List[ImageReference]
    ) -> CosReferenceSelection:
        return self._get_selfie_workflow_service().select_cos_references(action, message_refs)

    def _select_selfie_references(
        self, action: str, message_refs: List[ImageReference]
    ) -> Optional[CosReferenceSelection]:
        return self._get_selfie_workflow_service().select_selfie_references(action, message_refs)

    def _selfie_reference_images(
        self,
        action: str,
        message_refs: List[ImageReference],
        selection: Optional[CosReferenceSelection] = None,
    ) -> List[ImageReference]:
        return self._get_selfie_workflow_service().selfie_reference_images(action, message_refs, selection)

    async def _build_selfie_prompt_and_refs(
        self,
        action: str,
        extra_refs: List[ImageReference],
        event: Optional[AstrMessageEvent] = None,
        cos_selection: Optional[CosReferenceSelection] = None,
    ) -> Tuple[str, List[ImageReference]]:
        # These shots carry their own composition and do not need a daily snapshot.
        await self._ensure_daily_selfie_schedule(action, bool(extra_refs))
        persona_ref = self._persona_identity_reference(allow_logo=not self._is_cos_action(action))
        if cos_selection is None:
            cos_selection = self._select_selfie_references(action, extra_refs)
        if cos_selection is not None:
            refs = list(cos_selection.refs)
            identity_ref = cos_selection.identity_ref
        else:
            refs = []
            if persona_ref:
                refs.append(persona_ref)
            refs.extend(self._persona_auxiliary_references(action))
            refs.extend(extra_refs)
            identity_ref = persona_ref
        framework_persona = await resolve_framework_persona(getattr(self, "context", None), event)
        has_wardrobe_reference = bool(cos_selection and cos_selection.wardrobe_ref)
        wardrobe_item_name = str(cos_selection.wardrobe_item_name or "") if cos_selection else ""
        prompt = self.persona.build_selfie_prompt(
            action=action or "看着镜头自然自拍，展示你现在的样子",
            bot_name=framework_persona.name,
            personality=framework_persona.prompt,
            has_reference_image=bool(identity_ref),
            extra_reference_count=(cos_selection.extra_reference_image_count if cos_selection else len(extra_refs)),
            has_external_reference=bool(extra_refs),
            has_wardrobe_reference=has_wardrobe_reference,
            wardrobe_item_name=wardrobe_item_name,
        )
        return prompt, refs

    async def _build_selfie_prompt_and_refs_for_event(
        self,
        event: Optional[AstrMessageEvent],
        action: str,
        extra_refs: List[ImageReference],
        cos_selection: Optional[CosReferenceSelection] = None,
    ) -> Tuple[str, List[ImageReference], Dict[str, Any]]:
        """Use the central English built-ins and translate only free-form user text."""
        # Retry records can contain a complete but obsolete leg contract. Normalize
        # before reference selection and before extracting the English contract so
        # an old first-person/downward seated shot cannot re-enter the final prompt.
        action = self._normalize_selfie_action(action, bool(extra_refs))
        if cos_selection is None:
            cos_selection = self._select_selfie_references(action, extra_refs)
        prompt, refs = await self._build_selfie_prompt_and_refs(
            action,
            extra_refs,
            event=event,
            cos_selection=cos_selection,
        )
        has_identity_reference = bool(cos_selection.identity_ref) if cos_selection else bool(
            self._persona_identity_reference(allow_logo=not self._is_cos_action(action))
        )
        extra_reference_count = (
            cos_selection.extra_reference_image_count
            if cos_selection is not None
            else len(extra_refs)
        )
        meta: Dict[str, Any] = {"enabled": False, "applied": False, "scope": "user_text_only"}
        if not self._prompt_en_needed(action, media="image"):
            return prompt, refs, meta
        from .prompts.prompt_templates import (
            append_daily_context_to_english_prompt,
            build_selfie_builtin_prompt,
            extract_generated_action_contract,
            extract_user_prompt,
            has_daily_staging_contract,
        )

        user_text = extract_user_prompt(action)
        has_generated_contract = (
            looks_like_cos_prompt(action)
            or has_leg_focus_contract(action)
            or has_group_composition_contract(action)
            or has_daily_staging_contract(action)
        )
        action_source = extract_generated_action_contract(action) if has_generated_contract else action
        translated_action = action_source
        action_translation_meta: Dict[str, Any] = {}
        if has_generated_contract:
            translated_action, action_translation_meta = await self._translate_prompt_to_english(
                action_source,
                media="image",
                event=event,
            )
            if not action_translation_meta.get("applied"):
                translated_action = action_source
            meta["action_contract"] = action_translation_meta
        if not user_text:
            english = build_selfie_builtin_prompt(
                action,
                language="en",
                has_reference_image=has_identity_reference,
                extra_reference_count=extra_reference_count,
                appearance_type=self.persona.get_appearance_type(),
                action_content=translated_action,
                has_wardrobe_reference=bool(cos_selection and cos_selection.wardrobe_ref),
                wardrobe_item_name=str(cos_selection.wardrobe_item_name or "") if cos_selection else "",
            )
            english = append_daily_context_to_english_prompt(english, prompt)
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
            extra_reference_count=extra_reference_count,
            appearance_type=self.persona.get_appearance_type(),
            user_text=translated,
            action_content=translated_action,
            has_wardrobe_reference=bool(cos_selection and cos_selection.wardrobe_ref),
            wardrobe_item_name=str(cos_selection.wardrobe_item_name or "") if cos_selection else "",
        )
        english = append_daily_context_to_english_prompt(english, prompt)
        return english, refs, meta

    def get_selfie_reference_payload(self) -> Dict[str, Any]:
        data = self.persona.get()
        ref = self.persona.get_reference_image()
        appearance_type = self.persona.get_appearance_type()
        wardrobe = self.get_wardrobe_payload()
        base = {
            "appearance_type": appearance_type,
            "appearance_type_label": self.persona.appearance_type_label(),
            "ref_mime_type": data.get("ref_mime_type") or "image/png",
            "updated_at": data.get("updated_at") or "",
            "status": self.persona.status_text(),
            "daily_schedule": self.persona.daily_schedule_payload(),
            "wardrobe": wardrobe,
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

    def get_wardrobe_payload(self) -> Dict[str, Any]:
        current = self.persona.current_wardrobe_item()
        return {
            "items": [item.to_dict() for item in self.persona.list_wardrobe()],
            "enabled_count": sum(1 for item in self.persona.list_wardrobe() if item.enabled),
            "current_item_id": current.id if current else "",
            "current_item_name": current.name if current else "",
        }

    def save_wardrobe_item_from_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_image = str(payload.get("image") or payload.get("data") or "").strip()
        if not raw_image:
            raise ValueError("缺少 image 字段，支持 data:image/...;base64,... 或纯 base64")
        max_dimension, max_pixels, max_frames = image_validation_limits(self.config)
        data, mime = data_url_to_bytes(
            raw_image,
            max_dimension=max_dimension,
            max_pixels=max_pixels,
            max_frames=max_frames,
        )
        if not data:
            raise ValueError("上传图片为空或尺寸/动画帧数超限")
        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        if len(data) > max_bytes:
            raise ValueError(f"图片过大，最大允许 {self.config.image_max_image_size_mb}MB")
        self.persona.add_wardrobe_item(
            data,
            normalize_image_mime(mime or str(payload.get("mime_type") or "") or detect_mime_by_bytes(data)),
            name=str(payload.get("name") or payload.get("title") or "").strip(),
            description=str(payload.get("description") or "").strip(),
            tags=payload.get("tags"),
        )
        return self.get_wardrobe_payload()

    def update_wardrobe_item_from_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        item_id = str(payload.get("id") or payload.get("item_id") or "").strip()
        if not item_id:
            raise ValueError("缺少衣橱服装 id")
        self.persona.update_wardrobe_item(
            item_id,
            name=payload.get("name") if "name" in payload else None,
            description=payload.get("description") if "description" in payload else None,
            tags=payload.get("tags") if "tags" in payload else None,
            enabled=payload.get("enabled") if "enabled" in payload else None,
        )
        return self.get_wardrobe_payload()

    def delete_wardrobe_item_from_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        item_id = str(payload.get("id") or payload.get("item_id") or "").strip()
        if not item_id:
            raise ValueError("缺少衣橱服装 id")
        self.persona.remove_wardrobe_item(item_id)
        return self.get_wardrobe_payload()

    def toggle_wardrobe_item_from_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        item_id = str(payload.get("id") or payload.get("item_id") or "").strip()
        if not item_id:
            raise ValueError("缺少衣橱服装 id")
        enabled = payload.get("enabled")
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in {"1", "true", "yes", "on", "启用"}
        self.persona.set_wardrobe_enabled(item_id, bool(enabled))
        return self.get_wardrobe_payload()

    def set_selfie_appearance_type_from_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        value = payload.get("appearance_type", payload.get("type", "auto"))
        self.persona.set_appearance_type(value)
        return self.get_selfie_reference_payload()

    def save_selfie_reference_from_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_image = str(payload.get("image") or payload.get("data") or "").strip()
        if not raw_image:
            raise ValueError("缺少 image 字段，支持 data:image/...;base64,... 或纯 base64")
        max_dimension, max_pixels, max_frames = image_validation_limits(self.config)
        data, mime = data_url_to_bytes(
            raw_image,
            max_dimension=max_dimension,
            max_pixels=max_pixels,
            max_frames=max_frames,
        )
        if not data:
            raise ValueError("上传图片为空或尺寸/动画帧数超限")
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

    async def refresh_selfie_profile_local_from_web(self) -> Dict[str, Any]:
        """Refresh today's profile using only the local varied fallback pool."""
        profile = await self.persona.refresh_daily_selfie_profile_local()
        return {
            "status": self.persona.status_text(),
            "updated_at": profile.updated_at,
            "source": profile.source,
            "outfit": profile.outfit,
            "outfit_by_period": dict(profile.outfit_by_period),
            "outfit_pool": list(profile.outfit_pool),
            "scene_by_period": dict(profile.scene_by_period),
            "action_by_period": dict(profile.action_by_period),
            "schedule_by_period": copy.deepcopy(profile.schedule_by_period),
            "daily_schedule": self.persona.daily_schedule_payload(),
            "day_theme": profile.day_theme,
            "occasion": profile.occasion,
            "mood": profile.mood,
        }

    def _find_image_target(self, channel_name: str = "", model: str = "") -> Optional[ImageModelTarget]:
        targets: List[ImageModelTarget] = []
        for channel in self.config.image_channels:
            targets.extend(
                channel.targets(self.config.image_global_timeout, request_timeout=LOCAL_IMAGE_WAIT_SECONDS)
            )
        return find_model_target(targets, channel_name, model)

    @staticmethod
    def _retry_attempt_route(payload: Mapping[str, Any]) -> Tuple[str, str]:
        """Return the channel/model that the selected retry strategy must avoid."""
        raw = payload.get("retry_from_attempt") if isinstance(payload, Mapping) else None
        row = raw if isinstance(raw, Mapping) else {}
        channel = str(row.get("channel") or "").strip()
        model = str(row.get("model") or "").strip()
        if "/" in model:
            label_channel, _, label_model = model.partition("/")
            if not channel:
                channel = label_channel.strip()
            model = label_model.strip()
        return channel, model

    def _find_web_retry_target(
        self,
        payload: Mapping[str, Any],
        *,
        media_type: str = "image",
    ) -> Optional[ImageModelTarget]:
        """Select a retry target while honoring model-only/channel-only semantics."""
        strategy = str(payload.get("retry_strategy") or "full").strip().lower().replace("-", "_")
        channel_name = str(payload.get("channel") or "").strip()
        model_name = str(payload.get("model") or "").strip()
        if strategy not in {"model_only", "channel_only"}:
            return self._find_image_target(channel_name, model_name)
        failed_channel, failed_model = self._retry_attempt_route(payload)
        targets: List[ImageModelTarget] = []
        for channel in self.config.image_channels:
            targets.extend(channel.targets(self.config.image_global_timeout, request_timeout=LOCAL_IMAGE_WAIT_SECONDS))
        if not targets:
            return None
        candidates = list(targets)
        if strategy == "model_only":
            wanted_channel = channel_name or failed_channel
            candidates = [item for item in candidates if item.channel_name == wanted_channel]
            if failed_model:
                candidates = [item for item in candidates if item.model != failed_model]
            if not candidates:
                raise RuntimeError(f"渠道 {wanted_channel or '原渠道'} 没有可切换的其他模型")
            return candidates[0]
        if strategy == "channel_only":
            wanted_model = model_name or failed_model
            if wanted_model:
                candidates = [item for item in candidates if item.model == wanted_model]
            if failed_channel:
                candidates = [item for item in candidates if item.channel_name != failed_channel]
            elif channel_name:
                candidates = [item for item in candidates if item.channel_name != channel_name]
            if not candidates:
                raise RuntimeError(f"模型 {wanted_model or '原模型'} 没有可切换的其他渠道")
            return candidates[0]
        return find_model_target(candidates, channel_name, model_name)

    def _web_channel_runtime_credentials(
        self,
        channel_payload: Mapping[str, Any],
        media_type: str = "image",
    ) -> Tuple[str, str]:
        """Resolve a dashboard channel's runtime key without trusting masked input.

        The dashboard intentionally receives ``******`` instead of secrets.  A
        model refresh is a transient request and therefore bypasses the config
        save/restore path; resolve an existing channel by name (or base URL)
        before building its Authorization header.  New unsaved channels still
        use a key explicitly entered in the modal.
        """
        payload = channel_payload if isinstance(channel_payload, Mapping) else {}
        def first_usable(value: Any) -> str:
            keys = usable_api_keys(value)
            return keys[0] if keys else ""

        kind = str(media_type or "image").strip().lower()
        if kind == "audit":
            configured_channels = list(getattr(self.config, "audit_channels", []) or [])
        else:
            configured_channels = list(getattr(self.config, "image_channels", []) or [])

        name = str(
            payload.get("name")
            or payload.get("id")
            or payload.get("channel_id")
            or payload.get("channelId")
            or ""
        ).strip()
        base_url = str(payload.get("base_url") or payload.get("baseUrl") or "").strip().rstrip("/")

        def base_variants(value: Any) -> set[str]:
            text = str(value or "").strip().rstrip("/")
            variants = {text}
            if text.lower().endswith("/v1"):
                variants.add(text[:-3].rstrip("/"))
            else:
                variants.add(f"{text}/v1")
            return {item for item in variants if item}

        payload_bases = base_variants(base_url)
        matched = None
        if name:
            matched = next(
                (
                    channel
                    for channel in configured_channels
                    if str(getattr(channel, "name", "") or "").strip().casefold() == name.casefold()
                    or str(getattr(channel, "id", "") or "").strip().casefold() == name.casefold()
                ),
                None,
            )
        if matched is None and base_url:
            matched = next(
                (
                    channel
                    for channel in configured_channels
                    if base_variants(getattr(channel, "base_url", "")) & payload_bases
                ),
                None,
            )
        if matched is not None:
            keys = getattr(matched, "resolved_api_keys", None)
            if callable(keys):
                runtime_key = first_usable(keys())
            else:
                runtime_key = first_usable(getattr(matched, "api_key", ""))
            if runtime_key:
                return runtime_key, str(getattr(matched, "proxy", "") or "").strip()

        incoming_key = first_usable(payload.get("api_keys")) or first_usable(
            payload.get("api_key") or payload.get("apiKey")
        )
        return incoming_key, str(payload.get("proxy") or "").strip()

    def _record_retry_reference_data_urls(self, record: Mapping[str, Any]) -> List[str]:
        """Load cached request images for a record retry without exposing paths."""
        raw_paths = record.get("request_image_paths")
        if not isinstance(raw_paths, list):
            return []
        urls: List[str] = []
        max_bytes = int(getattr(self.config, "image_max_image_size_mb", 10) or 10) * 1024 * 1024
        for raw_path in raw_paths[:16]:
            try:
                loaded = self._load_cache_image_bytes(str(raw_path or ""))
            except Exception:
                loaded = None
            if not loaded:
                continue
            data, mime = loaded
            if not data or len(data) > max_bytes:
                continue
            urls.append(bytes_to_data_url(data, mime))
        return urls

    def build_record_retry_payload(
        self,
        record_id: str,
        feedback: str = "",
        strategy: str = "full",
    ) -> Dict[str, Any]:
        """Build a sanitized web-task request from a retained generation record."""
        record = self.get_record_for_web(record_id)
        request_data = record.get("request_data") if isinstance(record.get("request_data"), Mapping) else {}
        media_type = str(record.get("media_type") or "image").strip().lower()
        if media_type != "image":
            raise ValueError("仅支持图片记录重试")
        prompt = str(
            record.get("request_prompt")
            or record.get("prompt")
            or record.get("original_prompt")
            or request_data.get("original_prompt")
            or ""
        ).strip()
        if not prompt:
            raise ValueError("这条记录没有可重试的提示词")
        feedback_text = str(feedback or "").strip()[:2000]
        if feedback_text:
            prompt = f"{prompt}\n\nAdditional revision request: {feedback_text}"
        payload: Dict[str, Any] = {
            "media_type": media_type,
            "prompt": prompt[:50000],
            "channel": str(request_data.get("channel") or "").strip(),
            "model": str(request_data.get("model") or "").strip(),
            "aspect_ratio": str(
                request_data.get("aspect_ratio")
                or request_data.get("size")
                or getattr(self.config, "image_default_aspect_ratio", "9:16")
            ).strip(),
            "images": self._record_retry_reference_data_urls(record),
            "force_regenerate": True,
            "_retry_record_id": str(record.get("id") or record_id).strip(),
        }
        if media_type == "image":
            # The stored request prompt is already the expanded prompt. Reusing
            # it avoids translating or appending a second reference instruction.
            payload["resolution"] = str(
                request_data.get("resolution") or getattr(self.config, "image_default_resolution", "1K")
            ).strip()
            payload["prompt_enhance"] = False
            selection = request_data.get("reference_selection")
            selection_roles = selection.get("roles") if isinstance(selection, Mapping) and isinstance(selection.get("roles"), Mapping) else {}
            payload["use_selfie_reference"] = bool(
                request_data.get("use_selfie_reference")
                or request_data.get("persona_reference_count")
                or (isinstance(selection, Mapping) and selection.get("used_persona"))
                or selection_roles.get("persona")
                or selection_roles.get("persona_extra")
            )
        response_data = record.get("response_data") if isinstance(record.get("response_data"), Mapping) else {}
        attempts = response_data.get("attempts") if isinstance(response_data.get("attempts"), list) else record.get("attempts")
        return apply_retry_strategy(payload, strategy, attempts=attempts if isinstance(attempts, list) else None)

    def start_record_retry_task(
        self,
        record_id: str,
        feedback: str = "",
        strategy: str = "full",
    ) -> Dict[str, Any]:
        """Queue a record retry through the existing persistent web task manager."""
        payload = self.build_record_retry_payload(record_id, feedback, strategy)
        return self.start_web_image_task(payload)

    def start_record_reuse_task(
        self,
        record_id: str,
        *,
        feedback: str = "",
        strategy: str = "full",
        owner_session: str = "web",
    ) -> Dict[str, Any]:
        """Start a deduplicated retry intended for a chat session."""
        payload = self.build_record_retry_payload(record_id, feedback, strategy)
        payload["force_regenerate"] = False
        payload["owner_session"] = str(owner_session or "web").strip()[:200] or "web"
        payload["source"] = "chat-record-reuse"
        return self.start_web_image_task(payload)

    def _available_model_labels(self) -> List[str]:
        return available_model_labels(self.config.get_prioritized_targets())

    @staticmethod
    def _format_model_status_age(timestamp: Any, now: Optional[float] = None) -> str:
        return ImageStatusService.format_age(timestamp, now)

    @staticmethod
    def _model_status_error_label(category: str) -> str:
        return ImageStatusService.error_label(category)

    def _format_model_status_text(
        self,
        status: Mapping[str, Any],
        *,
        page: int = 1,
        page_size: int = 6,
    ) -> str:
        service = getattr(self, "_image_status_service", None) or ImageStatusService()
        return service.format_text(
            status,
            page=page,
            page_size=page_size,
            error_label=self._model_status_error_label,
            age_formatter=self._format_model_status_age,
        )

    @staticmethod
    def _status_report_font(size: int, *, bold: bool = False) -> Any:
        if ImageFont is None:
            return None
        suffix = "Bold" if bold else "Regular"
        candidates: List[str] = []
        configured_font = str(os.environ.get("ASTRBOT_STATUS_FONT") or "").strip()
        if configured_font:
            candidates.append(os.path.expandvars(os.path.expanduser(configured_font)))

        windows_root = str(
            os.environ.get("WINDIR") or os.environ.get("SystemRoot") or ""
        ).strip()
        if windows_root:
            windows_fonts = os.path.join(windows_root, "Fonts")
            windows_names = (
                (
                    "msyhbd.ttc",
                    "simhei.ttf",
                    "simsunb.ttf",
                    "Dengb.ttf",
                    "msyh.ttc",
                    "simsun.ttc",
                    "Deng.ttf",
                )
                if bold
                else ("msyh.ttc", "simhei.ttf", "simsun.ttc", "Deng.ttf")
            )
            candidates.extend(os.path.join(windows_fonts, name) for name in windows_names)

        candidates.extend(
            [
                "/system/fonts/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/source-han/SourceHanSansCN-Regular.otf",
                "/root/xiu2/data/xiuxian/font/SourceHanSerifCN-Heavy.otf",
                "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
                f"/usr/share/fonts/truetype/noto/NotoSansCJK-{suffix}.ttc",
                f"/root/AstrBot/data/plugins/astrbot_plugin_status/templates/res/fonts/Noto-Sans-SC-{suffix}.ttf",
                f"/usr/share/fonts/truetype/noto/NotoSansMono-{suffix}.ttf",
                f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
                f"/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans{'-Bold' if bold else ''}.ttf",
            ]
        )
        for path in candidates:
            if os.path.isfile(path):
                try:
                    font = ImageFont.truetype(path, size=size)
                    # Pillow's default glyph box can be non-empty even when it is
                    # the same missing-character square for every Chinese glyph.
                    glyphs = []
                    for sample in "模型状态成功率最近":
                        mask = font.getmask(sample)
                        if mask.getbbox() is None:
                            glyphs = []
                            break
                        glyphs.append((mask.size, bytes(mask)))
                    if len(set(glyphs)) != len("模型状态成功率最近"):
                        continue
                    return font
                except Exception:
                    continue
        return None

    def _sync_dashboard_logo(self) -> bool:
        """Copy the single bundled logo for direct Dashboard file serving."""
        source = os.path.abspath(str(getattr(self, "_bundled_logo_path", "") or ""))
        target = os.path.abspath(str(getattr(self, "_dashboard_logo_path", "") or ""))
        if not source or not target or source == target or not os.path.isfile(source):
            return False
        temporary = f"{target}.tmp-{os.getpid()}"
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
            return True
        except OSError as exc:
            try:
                if os.path.isfile(temporary):
                    os.unlink(temporary)
            except OSError:
                pass
            logger.warning("[SelfieImage] Dashboard logo 同步失败: %s", redact_sensitive_text(str(exc)))
            return False

    @staticmethod
    def _status_report_wrap(draw: Any, text: str, font: Any, max_width: int, max_lines: int = 2) -> List[str]:
        return ImageStatusService.wrap_text(draw, text, font, max_width, max_lines)

    def _render_model_status_image(
        self,
        status: Mapping[str, Any],
        *,
        page: int = 1,
        page_size: int = 6,
    ) -> Optional[str]:
        """Render the model monitor as a self-contained QQ-friendly image."""
        if PILImage is None or ImageDraw is None or ImageFont is None:
            return None
        try:
            service = getattr(self, "_image_status_service", None) or ImageStatusService()
            return service.render_to_file(
                status,
                data_dir=self.data_dir,
                font_loader=self._status_report_font,
                error_label=self._model_status_error_label,
                page=page,
                page_size=page_size,
            )
        except Exception as exc:
            logger.warning("[SelfieImage] 生图模型状态图片渲染失败: %s", redact_sensitive_text(str(exc)))
            return None

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
        group_id = event_group_id(event) if event is not None else ""
        group_config = self.config.group_chat_configs.get(group_id, {}) if group_id else {}
        mode = str(group_config.get("model_priority_mode") or "default").strip().lower()
        all_targets = self.config.get_group_image_targets(
            mode,
            str(group_config.get("fixed_model") or ""),
        )
        return prioritize_model_target(all_targets, self._get_session_model_override(event))

    def _prioritize_targets_by_model_request(
        self,
        targets: List[ImageModelTarget],
        model_request: str = "",
    ) -> List[ImageModelTarget]:
        """Put the requested model first while keeping the fallback chain.

        LLM tool calls may carry a free-text model hint ("gpt").  A confident
        match moves that target to the front of the retry chain; anything else
        keeps the default routing untouched.
        """
        items = list(targets or [])
        preferred = match_model_request(model_request, items)
        if preferred is None:
            return items
        return [preferred] + [target for target in items if target.label != preferred.label]

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

    def _format_task_list_text(
        self,
        tasks: List[Dict[str, Any]],
        *,
        include_finished: bool = False,
        media_type: str = "",
    ) -> str:
        return format_task_list_text(
            tasks,
            include_finished=include_finished,
            media_type=media_type,
        )

    @staticmethod
    def _is_task_history_request(message: str) -> bool:
        is_history, _ = SelfieImagePlugin._parse_task_history_request(message)
        return is_history

    @staticmethod
    def _parse_task_history_request(message: str) -> Tuple[bool, Optional[int]]:
        """Parse `历史` and `历史 1` without colliding with active task indices."""
        text = str(message or "").strip()
        matched = re.fullmatch(
            r"(?:历史|最近|完成|记录|history|recent)[\s，。！？、；：,.!?;:#]*(?:([0-9]+)[\s，。！？、；：,.!?;:#]*)?",
            text,
            flags=re.IGNORECASE,
        )
        if not matched:
            return False, None
        value = matched.group(1)
        return True, int(value) if value else None

    def _format_task_detail_text(self, task: Dict[str, Any]) -> str:
        return format_task_detail_text(task)

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

        history_requested, history_index = self._parse_task_history_request(message)
        if history_requested:
            tasks = self._list_image_tasks_for_session(
                session_key,
                include_finished=True,
                limit=20,
                media_type=media_type,
            )
            if history_index is not None:
                index = history_index - 1
                if not 0 <= index < len(tasks):
                    hint = "/生图任务 历史"
                    yield event.plain_result(f"没找到这个历史编号。先 {hint} 查看最近记录。")
                    return
                yield event.plain_result(self._format_task_detail_text(tasks[index]))
                return
            yield event.plain_result(
                self._format_task_list_text(
                    tasks,
                    include_finished=True,
                    media_type=media_type,
                )
            )
            return

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
                yield event.plain_result("这个任务不属于生图任务。")
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
            self._format_task_list_text(tasks)
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
                yield event.plain_result("现在没有可取消的出图。")
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
                yield event.plain_result("这个任务不属于生图任务。")
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
        return _generation_task_service_for(self).cancel_image_task(
            task_id, session_key=session_key, is_admin=is_admin
        )

    @staticmethod
    def _log_scheduler_cancel_error(future: asyncio.Future) -> None:
        GenerationTaskService.log_scheduler_cancel_error(future)

    def _task_cancel_requested(self, task_id: str) -> bool:
        return _generation_task_service_for(self).task_cancel_requested(task_id)

    def _mark_task_delivery_failed(self, task_id: str, error: str) -> None:
        _generation_task_service_for(self).mark_task_delivery_failed(task_id, error)

    def start_command_image_task(
        self,
        event: AstrMessageEvent,
        *,
        source: str,
        summary: Dict[str, Any],
        runner,
        reference_images: Optional[Iterable[ImageReference]] = None,
    ) -> Dict[str, Any]:
        return _generation_task_service_for(self).start_command_image_task(
            event,
            source=source,
            summary=summary,
            runner=runner,
            reference_images=reference_images,
        )

    async def _await_command_image_task(self, task: Mapping[str, Any]) -> Dict[str, Any]:
        return await _generation_task_service_for(self).await_command_image_task(task)

    async def _ensure_task_failure_record(
        self,
        task_id: str,
        *,
        error: str,
        cancelled: bool = False,
        stage: str = "task_exception",
        media_type: str = "",
        source: str = "",
        prompt: str = "",
        request_data: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Persist one failure row when a queue task stops before generation.

        Generation handlers normally write their own row.  Queue cancellation
        or a runner exception can happen before those handlers run, so keep a
        small idempotent fallback that preserves the task-to-record link.
        """
        tid = str(task_id or "").strip()
        if not tid:
            return
        wait_commits = getattr(self, "_wait_for_record_commits", None)
        if callable(wait_commits):
            await wait_commits(tid)
        try:
            task = self.get_web_image_task(tid)
        except Exception:
            task = {}
        linked = task.get("record_ids") if isinstance(task, Mapping) else []
        result = task.get("result") if isinstance(task, Mapping) else {}
        if not isinstance(linked, list):
            linked = []
        if isinstance(result, Mapping) and isinstance(result.get("record_ids"), list):
            linked = [*linked, *result.get("record_ids")]
        records = getattr(self, "_records", None)
        records_lock = getattr(self, "_records_lock", None)
        linked_records = []
        if isinstance(records, list) and records_lock is not None:
            try:
                with records_lock:
                    linked_records = [
                        row
                        for row in records
                        if isinstance(row, Mapping)
                        and str(row.get("task_id") or "").strip() == tid
                    ]
                    if any(
                        isinstance(row, Mapping)
                        and (
                            row.get("success") is False
                            or str(row.get("status") or "")
                            in {"failed", "cancelled", "delivery_failed"}
                        )
                        for row in linked_records
                    ):
                        return
            except Exception:
                linked_records = []
        # Earlier batch shots may already be successful while this task later
        # gets cancelled or crashes. Suppress only when a matching failure row
        # exists; otherwise keep the later terminal state inspectable.
        if any(str(item or "").strip() for item in linked) and not linked_records:
            return

        request = dict(request_data) if isinstance(request_data, Mapping) else {}
        task_request = task.get("request_data") if isinstance(task, Mapping) else {}
        trace_id = str(task.get("trace_id") or "").strip() if isinstance(task, Mapping) else ""
        if isinstance(task_request, Mapping):
            request = {**dict(task_request), **request}
        # Queue payloads may carry full reference-image data URLs. Keep the
        # fallback row inspectable without duplicating those blobs in history.
        for key in ("images", "image", "_task_id", "api_key", "api_keys"):
            request.pop(key, None)
        source = str(source or (task.get("source") if isinstance(task, Mapping) else "") or "task").strip()
        prompt = str(
            prompt
            or request.get("prompt")
            or request.get("action")
            or request.get("original_prompt")
            or ""
        ).strip()
        kind = "image"
        safe_error = redact_sensitive_text(str(error or ("任务已取消" if cancelled else "任务失败")))[:2000]
        request["stage"] = str(stage or "task_exception")
        request["task_id"] = tid
        if trace_id:
            request["trace_id"] = trace_id
        try:
            reference_images = max(
                0,
                int(
                    request.get("reference_image_count")
                    or request.get("raw_reference_image_count")
                    or 0
                ),
            )
        except (TypeError, ValueError):
            reference_images = 0
        record = {
            "source": source,
            "source_label": (
                f"Web/{str(request.get('studio_session_title') or '画布').strip() or '画布'}"
                if source == "studio-run"
                else "Web/快速试画" if source == "web-test"
                else "Web" if source.startswith("web") else source
            ),
            "media_type": kind,
            "success": False,
            "generation_success": False,
            "delivery_success": None,
            "status": "cancelled" if cancelled else "failed",
            "cancelled": bool(cancelled),
            "error": safe_error,
            "prompt": prompt,
            "original_prompt": str(request.get("original_prompt") or prompt),
            "request_prompt": prompt,
            "final_prompt": prompt,
            "used_model": "",
            "elapsed_seconds": 0,
            "reference_images": reference_images,
            "request_data": redact_sensitive_data(request),
            "response_data": redact_sensitive_data(
                {"success": False, "stage": str(stage or "task_exception"), "error": safe_error, "cancelled": bool(cancelled)}
            ),
            "request_image_paths": list(request.get("request_image_paths") or []),
            "generated_image_paths": [],
            "attempts": [],
            "retry_count": 0,
            "retry_exhausted": False,
            "task_id": tid,
            "trace_id": trace_id,
        }
        if trace_id:
            record["response_data"]["trace_id"] = trace_id
        try:
            self._record_task(record)
        except Exception as exc:
            logger.warning(f"[SelfieImage] 任务失败记录落库失败: {exc}")
            return
        if callable(wait_commits):
            await wait_commits(tid)

    async def _run_command_image_task(self, task_id: str, event: AstrMessageEvent, runner) -> None:
        await _generation_task_service_for(self).run_command_image_task(task_id, event, runner)



    def _image_inflight_limit(self) -> int:
        return _generation_runtime_service_for(self).image_inflight_limit()

    def _image_scheduler_snapshot(self) -> Dict[str, int]:
        return _generation_runtime_service_for(self).image_scheduler_snapshot()

    def _image_batch_queue_expected(self, total: int = 1) -> bool:
        """Whether the shared scheduler already has no physical image slot."""
        return _generation_runtime_service_for(self).image_batch_queue_expected(total)

    def _ensure_image_batch_gate(self) -> asyncio.Semaphore:
        """Keep compatibility with older hosts that have no image scheduler."""
        return _generation_runtime_service_for(self).ensure_image_batch_gate()

    async def _acquire_image_slot(self, task_id: str) -> Optional[asyncio.Semaphore]:
        """Acquire the legacy image slot when running without the scheduler."""
        return await _generation_runtime_service_for(self).acquire_image_slot(task_id)

    def _ensure_image_batch_cooldown_lock(self) -> asyncio.Lock:
        return _generation_runtime_service_for(self).ensure_image_batch_cooldown_lock()

    async def _wait_for_image_batch_cooldown(self, task_id: str) -> bool:
        """Stagger actual upstream starts, including shots released from the queue."""
        return await _generation_runtime_service_for(self).wait_for_image_batch_cooldown(
            task_id, IMAGE_BATCH_REQUEST_COOLDOWN_SECONDS
        )

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
        return await CountedGenerationWorkflow(
            self,
            task_id=task_id,
            event=event,
            total=total,
            fail_label=fail_label,
            run_one=run_one,
            log_prefix=log_prefix,
            log_info=logger.info,
        ).run()

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
            reason=reason,
            index=index,
            total=total,
            done_files=done_files,
            will_continue=will_continue,
        )
        reply = self._strip_llm_short_reply(await self._call_text_llm(event, llm_prompt, timeout=6, use_persona=True))
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
        special_preset_variants: Optional[List[str]] = None,
        model_request: str = "",
    ) -> Dict[str, Any]:
        total = self._normalize_count(requested_count)
        base_prompt, variation_enabled, variation_field = parse_variation_request(prompt)
        # Every batch path, including ordinary non-variation prompts, feeds the
        # same prompt list into the per-shot closure below.
        variation_prompts = [base_prompt]
        if special_preset_variants:
            variation_prompts = [
                parse_variation_request(str(item or ""))[0].strip()
                for item in special_preset_variants[:total]
                if str(item or "").strip()
            ]
            if not variation_prompts:
                variation_prompts = [base_prompt]
            elif len(variation_prompts) < total:
                variation_prompts.extend([variation_prompts[-1]] * (total - len(variation_prompts)))
        elif variation_enabled and total > 1:
            fields = [variation_field] if variation_field else ["pose", "scene", "shot"]
            variation_prompts = [
                str(item.get("prompt") or base_prompt).strip()
                for item in self.build_creative_variations(base_prompt, total, vary=fields)
            ]
        elif variation_enabled:
            variation_prompts = [base_prompt]

        async def run_one(index: int) -> Dict[str, Any]:
            shot_prompt = variation_prompts[min(index, len(variation_prompts) - 1)]
            if passthrough:
                return await self._draw_passthrough_once(
                    event,
                    shot_prompt,
                    aspect,
                    resolution,
                    refs,
                    source,
                    task_id=task_id,
                    requested_count=total,
                    model_request=model_request,
                )
            return await self._draw_once(
                event,
                shot_prompt,
                aspect,
                resolution,
                refs,
                source,
                task_id=task_id,
                requested_count=total,
                model_request=model_request,
            )

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
        rebuild_special_preset_variants: Optional[List[str]] = None,
        cos_selection: Optional[CosReferenceSelection] = None,
        reference_selection: Optional[Mapping[str, Any]] = None,
        model_request: str = "",
    ) -> Dict[str, Any]:
        total = self._normalize_count(requested_count)
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
            rebuild_special_preset_variants,
            cos_selection,
            reference_selection,
            model_request,
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
        rebuild_special_preset_variants: Optional[List[str]] = None,
        cos_selection: Optional[CosReferenceSelection] = None,
        reference_selection: Optional[Mapping[str, Any]] = None,
        model_request: str = "",
    ) -> Dict[str, Any]:
        return await SelfieBatchWorkflow(
            self,
            task_id,
            event,
            action,
            extra_refs,
            source,
            requested_count,
            aspect,
            resolution,
            fail_label,
            rebuild_extra_request,
            rebuild_match_query,
            rebuild_special_preset_variants,
            cos_selection,
            reference_selection,
            model_request,
        ).run()

    def _validate_web_test_selection(self, payload: Dict[str, Any]) -> None:
        channel_name = str(payload.get("channel") or "").strip()
        model_name = str(payload.get("model") or "").strip()
        media_type = "image"
        if not channel_name:
            return
        channels = self.config.image_channels
        kind_label = "生图"
        matching_channels = [channel for channel in channels if channel.name == channel_name]
        if not matching_channels:
            raise RuntimeError(f"{kind_label}渠道 {channel_name} 不存在")
        if not any(channel.enabled for channel in matching_channels):
            raise RuntimeError(f"{kind_label}渠道 {channel_name} 已禁用，渠道测试不会调用禁用渠道")
        channel = next((item for item in matching_channels if item.enabled), matching_channels[0])
        from .core.models import preflight_image_channel
        report = preflight_image_channel(
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
                },
                kind="image",
            )
        if not report.get("ok"):
            raise RuntimeError(report.get("message") or f"{kind_label}渠道配置预检未通过")
        enabled = channel.enabled_models or ([channel.model] if channel.model else [])
        if model_name and model_name not in enabled:
            raise RuntimeError(f"渠道 {channel_name} 未启用模型 {model_name}，请先在渠道管理中启用并保存")

    async def web_test_image(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await WebTestImageWorkflow(
            self,
            payload,
            appearance_type_for_request=_appearance_type_for_request,
        ).run()

    async def web_refresh_image_models(self, payload: Dict[str, Any]) -> List[str]:
        raw_payload = payload if isinstance(payload, dict) else {}
        channel_payload = raw_payload.get("channel") if isinstance(raw_payload.get("channel"), dict) else raw_payload
        base_url = str(channel_payload.get("base_url") or channel_payload.get("baseUrl") or "").strip()
        media_type = str(channel_payload.get("media_type") or raw_payload.get("media_type") or "image").strip().lower()
        api_key, configured_proxy = self._web_channel_runtime_credentials(channel_payload, media_type)
        provider_type = provider_type_from_channel_payload(channel_payload)
        proxy = str(channel_payload.get("proxy") or configured_proxy or "").strip()
        candidates = build_model_list_urls(base_url, provider_type)
        if not candidates:
            raise RuntimeError("base_url 为空")
        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive",
            "User-Agent": "AI-Cat/1.0",
        }
        if provider_type == "gemini" and api_key:
            headers["x-goog-api-key"] = api_key
        elif api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        errors: List[str] = []
        model_list_unavailable = True
        async with aiohttp.ClientSession(trust_env=False) as base_session:
            async with channel_client_session(proxy, base_session) as session:
                request_proxy = http_proxy_url(proxy)
                for url in candidates:
                    safe_url = redact_sensitive_text(url)
                    try:
                        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12), proxy=request_proxy) as response:
                            if response.status >= 400:
                                if response.status not in {404, 405, 501}:
                                    model_list_unavailable = False
                                errors.append(f"{safe_url}: HTTP {response.status} {redact_sensitive_text(await response.text())[:200]}")
                                continue
                            model_list_unavailable = False
                            data = await response.json(content_type=None)
                        models = self._extract_model_ids(data)
                        if models:
                            return models
                        errors.append(f"{safe_url}: 返回成功但未识别到模型")
                    except Exception as exc:
                        model_list_unavailable = False
                        errors.append(f"{safe_url}: {redact_sensitive_text(str(exc))}")
        if provider_type == "novelai" and model_list_unavailable:
            raise RuntimeError(
                "此 NovelAI 原生接口未提供模型列表，请手动添加上游支持的模型名（如 nai-diffusion-4-5-full）。\n"
                + "\n".join(errors)
            )
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
                result = await self._draw_passthrough_once(
                    event,
                    prompt,
                    aspect,
                    resolution,
                    refs,
                    source,
                    requested_count=total,
                )
            else:
                result = await self._draw_once(
                    event,
                    prompt,
                    aspect,
                    resolution,
                    refs,
                    source,
                    requested_count=total,
                )
            result["batch_index"] = index + 1
            result["batch_total"] = total
            yield result
            if not result.get("success"):
                return

    async def _draw_once(
        self,
        event: AstrMessageEvent,
        prompt: str,
        aspect: str,
        resolution: str,
        refs: List[ImageReference],
        source: str,
        *,
        task_id: str = "",
        requested_count: int = 1,
        model_request: str = "",
    ) -> Dict[str, Any]:
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
            targets=(
                self._prioritize_targets_by_model_request(
                    self._resolve_generation_targets(event), model_request
                )
                if str(model_request or "").strip()
                else None
            ),
            source=source,
            audit_user_id=event_user_id(event),
            event=event,
            original_prompt=prompt,
            prompt_en_meta=prompt_en_meta,
            record_context={
                key: value
                for key, value in {"task_id": task_id, "requested_count": requested_count}.items()
                if value
            }
            or None,
        )

    async def _draw_passthrough_once(
        self,
        event: AstrMessageEvent,
        prompt: str,
        aspect: str,
        resolution: str,
        refs: List[ImageReference],
        source: str,
        *,
        task_id: str = "",
        requested_count: int = 1,
        model_request: str = "",
    ) -> Dict[str, Any]:
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
            targets=(
                self._prioritize_targets_by_model_request(
                    self._resolve_generation_targets(event), model_request
                )
                if str(model_request or "").strip()
                else None
            ),
            source=source,
            audit_user_id=event_user_id(event),
            event=event,
            original_prompt=prompt,
            prompt_en_meta=prompt_en_meta,
            record_context={
                key: value
                for key, value in {"task_id": task_id, "requested_count": requested_count}.items()
                if value
            }
            or None,
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
        rebuild_special_preset_variants: Optional[List[str]] = None,
    ) -> AsyncGenerator[Any, None]:
        async for item in self._get_selfie_workflow_service().handle_command(
            event=event,
            command_name=command_name,
            fallback=fallback,
            default_action=default_action,
            default_action_with_refs=default_action_with_refs,
            progress_label=progress_label,
            source=source,
            fail_label=fail_label,
            message_override=message_override,
            include_at_avatar=include_at_avatar,
            allow_context_fallback=allow_context_fallback,
            requested_count_override=requested_count_override,
            preset_aspect=preset_aspect,
            preset_resolution=preset_resolution,
            preset_name=preset_name,
            rebuild_extra_request=rebuild_extra_request,
            rebuild_match_query=rebuild_match_query,
            rebuild_special_preset_variants=rebuild_special_preset_variants,
        ):
            yield item

    @filter.command("生图帮助")
    async def cmd_help(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """发送生图指令速查图。完整说明请发 /生图help。"""
        help_path = self._resolve_help_image_path("quick")
        if help_path:
            yield event.chain_result([self._create_image_component(help_path)])
            return
        # 无图时退回简短提示，避免空白
        yield event.plain_result("帮助图暂不可用。发 /生图help 看文字说明。")

    @filter.command("生图help")
    async def cmd_help_text(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """发送完整的生图指令说明图。"""
        help_path = self._resolve_help_image_path("full")
        if help_path:
            yield event.chain_result([self._create_image_component(help_path)])
            return
        yield event.plain_result(self._help_text_body())

    def _help_text_body(self) -> str:
        return "\n".join(
            [
                f"{PLUGIN_DISPLAY_NAME} v{PLUGIN_VERSION}",
                "",
                "常用：",
                "· 生图数量　用 -c 指定张数，可放在提示词任意位置，如 /画 一位约 20 岁的角色 -c 3 夜景；旧的 /画 3 仍可用",
                "· /画 或 /生图　写想要的画面；有附图/引用图就按图改，没图就按文字出；不自动带入形象图",
                "· 引用即提示词　引用一条文字消息后直接发 /画、/自拍 等，会把被引用文字当作提示词；自己写的正文优先",
                "· /文生图　只用文字按原文出图，不走自拍人设，也不用形象图",
                "· /图生图　必须附图或引用图，按原文改图；不自动使用形象图",
                "· /nai　仅选启用的 NAI 模型，优先 NAI4、其次 NAI5，无需加入模型优先级；可写 [画师名] 提示词，附图可图生图",
                "· /nai5　仅使用启用的 NAI5；/nai 画师 查看画师串，/nai 画师名 设置本会话默认，/nai 默认 恢复；前端预设管理可编辑画师串",
                "· /自拍 或 /看看　用当前形象自拍；可写动作、场景、换装",
                "· /看看腿　随机展示黑丝、白丝等日常袜装穿搭，默认腰线至鞋履的第三人称平视构图；可指定长裤、全身、姿势或自拍",
                "· /查看提示词 [六位编号|最近]　查询最终实际提示词，也支持引用图片；编号和正文分两条消息",
                "· /查看生图提示词 [六位编号]　由当前聊天 LLM 反推图片；反推编号可取回已有结果",
                "· /看看COS　随机一套内置 COS 换装；未指定时每次随机使用他拍、第一视角或自拍视角，可明确写「他拍」「第一视角」或「自拍」固定视角；可用 -c 指定数量，预设、随机池角色/类别和额外提示词可任意顺序，未匹配文本保留为额外提示；可发「看看COS 列表/全部/查看」浏览标题",
                "· /看看你　像别人随手拍你",
                "· /合影 或 /合照　和对象同框；可附图或@对方，自己用当前形象",
                "",
                "自动判断：",
                "· /画：有图=图生图，没图=文生图；不会自动塞形象图",
                "· 自拍/合影/看看：会用当前形象；形象类型可设自动、真人、动漫",
                "· 引用：引用图=参考图，引用文字=提示词；两者可同时用，自己写的正文优先",
                "",
                "模型与进度：",
                "· /生图模型　看列表；跟序号或 渠道/模型 切换（只影响当前群/私聊）；发「清除」恢复默认",
                "· /生图状态　看最近 7 天模型成功率、耗时、错误和历史；可跟 1h、24h、7d、30d 与页码（兼容 /生图记录状态）",
                "· /生图任务　看进行中的生图任务，包含 LLM 调用；可跟任务号",
                "· /生图取消　取消还在排的/进行中的任务",
                "· 管理员名单：/生图加黑 ID/@用户 [理由]、/生图加白 ID/@用户 [理由]、/生图释放 ID/@用户",
                "",
                "形象：",
                "· /形象查看　看当前参考图、形象类型与今日状态",
                "· /形象设置　发图设形象；也可写 自动 / 真人 / 动漫 改形象类型",
                "· /辅助形象设置　附带图片增加辅助形象；最多 3 张，普通自拍/换装会使用，合影时只使用主形象图",
                "· /辅助形象清除　清空辅助形象图，不影响主形象",
                "· /形象清除　去掉参考图",
                "· /形象刷新　刷新今日穿搭状态",
                "",
                "预设：/预设　列表；可在任意生图指令中写「变真人」「变动漫」「变猫娘」「变Q版」「变像素」等图像转换预设；「特殊预设」随机选择上下身结构，「动作预设」随机选择动作，「上身预设」随机选择上身结构，「下身预设」随机选择下身结构；管理员可 /预设添加 名称:内容、/预设删除 名称",
                "",
                "说明：一次可写数量表示本条指令要生成的总张数；同时最多进行几张由「同时画几张上限」决定，不锁在单条指令里，新任务自动排队，超过同时上限才等待。图好了会直接发过来。",
                "· /生图帮助　只看图卡",
                "· /生图help　看本页完整说明",
                f"管理页：{'已开' if self.config.web_enable else '未开'}　http://{self.config.web_host}:{self.config.web_port}",
                "也可在 AstrBot 插件页打开管理界面。",
            ]
        )

    def _resolve_help_image_path(self, kind: str = "quick") -> str:
        """Return a startup-generated help image without regenerating it."""
        key = "full" if str(kind or "").strip().lower() == "full" else "quick"
        path = str(getattr(self, "_help_image_paths", {}).get(key, "") or "")
        if path and os.path.isfile(path):
            try:
                with open(path, "rb") as handle:
                    head = handle.read(32)
                if looks_like_image_bytes(head):
                    return path
            except Exception:
                pass
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
            message = payload.get("message") or []
            text = message if isinstance(message, str) else "".join(
                str(part.get("data", {}).get("text") or "")
                for part in message if isinstance(part, Mapping) and part.get("type") == "text"
            )
            linked = self._prompt_records_from_quote_text(event, text)
            for record in linked:
                await asyncio.to_thread(self._remember_prompt_delivery, event, record, {"message_id": reply_id})
            raw_event = SimpleNamespace(message_obj=None, message=None, raw_message=payload)
            buckets = extract_structured_image_sources(raw_event, include_image_alternates=True)
            for role in ("message", "quote", "forward"):
                for source in buckets.get(role, []):
                    if source not in sources:
                        sources.append(source)
        return sources

    @filter.command("查看提示词")
    async def cmd_view_prompt(self, event: AstrMessageEvent, code: str = "") -> AsyncGenerator[Any, None]:
        """按编号、最近或引用一张图片，查询已保存的最终提示词。"""
        from .generation.prompt_lookup import CODE_PATTERN, record_final_prompt

        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        argument = extract_command_message(event, "查看提示词", code).strip().upper()
        if argument and argument not in {"最近", "最新"} and not CODE_PATTERN.fullmatch(argument):
            yield event.plain_result("用法：/查看提示词 [六位编号|最近]，也可以直接引用图片查询。")
            return
        refs = []
        candidates = []
        if argument in {"最近", "最新"}:
            record = self._recent_prompt_record(event)
            method = "本人当前会话最近一张"
        elif argument:
            record = self._prompt_record_by_code(event, argument)
            method = "编号查询"
            if record:
                await asyncio.to_thread(self._confirm_prompt_candidate, event, record)
            if record and self._quoted_reply_ids(event):
                refs = await self._prompt_lookup_references(event, "查看提示词")
                await asyncio.to_thread(self._remember_prompt_aliases, record, refs)
        else:
            reply_ids = self._quoted_reply_ids(event)
            record, method, candidates = await asyncio.to_thread(self._lookup_prompt_image, event, [], reply_ids)
            if record is None and not candidates:
                refs = await self._prompt_lookup_references(event, "查看提示词")
                record, method, candidates = await asyncio.to_thread(self._lookup_prompt_image, event, refs, reply_ids)
        if candidates:
            await asyncio.to_thread(self._remember_prompt_candidates, event, refs, candidates)
            yield event.plain_result(method + "。请核对图片后使用 /查看提示词 六位编号。")
            for candidate in candidates:
                label = f"生图编号：{candidate.get('image_code', '')}\n时间：{candidate.get('time', '')}"
                paths = candidate.get("generated_image_paths") or []
                try:
                    path = self._cache_absolute_path(paths[0]) if paths else ""
                    if path and os.path.isfile(path):
                        yield event.chain_result([self._create_image_component(path)]).message(label)
                        continue
                except (OSError, ValueError):
                    pass
                yield event.plain_result(label)
            return
        if record is None:
            yield event.plain_result("未找到可访问的生图记录。可用 /查看提示词 六位编号 或 /查看提示词 最近；需要反推图片时使用 /查看生图提示词。")
            return
        prompt = record_final_prompt(record)
        label = f"生图编号：{record.get('image_code', '')}\n时间：{record.get('time', '')}\n匹配方式：{method}"
        if not prompt:
            yield event.plain_result(label + "\n历史记录没有保存提示词。")
            return
        yield event.plain_result(label)
        yield event.plain_result(prompt)

    async def _prompt_lookup_references(self, event: AstrMessageEvent, context_hint: str) -> List[ImageReference]:
        quoted_sources = await self._quoted_original_image_sources(event)
        collect_kwargs: Dict[str, Any] = {
            "include_at_avatar": False,
            "context_hint": context_hint,
            "allow_context_fallback": False,
            "include_persona": False,
            # QQ/AstrBot may expose both a transcoded local path and the
            # original URL. Prompt lookup must try only representations of
            # the quoted image; unrelated recent bot images must never match.
            "include_image_alternates": True,
        }
        if quoted_sources:
            collect_kwargs["extra_sources"] = quoted_sources
        return await self._event_reference_images(event, **collect_kwargs)

    @filter.command("查看生图提示词")
    async def cmd_reverse_image_prompt(self, event: AstrMessageEvent, code: str = "") -> AsyncGenerator[Any, None]:
        """引用图片由当前聊天 LLM 反推，或按编号取回已有反推结果。"""
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        from .generation.prompt_lookup import CODE_PATTERN

        argument = extract_command_message(event, "查看生图提示词", code).strip().upper()
        if argument and not CODE_PATTERN.fullmatch(argument):
            yield event.plain_result("用法：/查看生图提示词 [六位编号]，或引用/附带图片。")
            return
        if argument:
            saved = self._reverse_prompt_by_code(event, argument)
            if saved:
                yield event.plain_result(f"反推编号：{saved['image_code']}\nLLM 反推结果，非原始提示词。")
                yield event.plain_result(saved["prompt"])
                return
            generated = self._prompt_record_by_code(event, argument)
            paths = (generated or {}).get("generated_image_paths") or []
            loaded = await asyncio.to_thread(self._load_cache_image_bytes, paths[0]) if paths else None
            refs = [ImageReference(data=loaded[0], mime_type=loaded[1])] if loaded else []
        else:
            refs = await self._prompt_lookup_references(event, "查看生图提示词")
        if not refs:
            yield event.plain_result("编号不存在或图片缓存已清理，请引用或附带图片。" if argument else "请引用或附带一张图片后再使用 /查看生图提示词。")
            return

        ref = refs[0]
        try:
            prompt = await self._reverse_image_prompt_with_llm(event, ref.data)
        except Exception as exc:
            logger.warning("[SelfieImage] 反推图片提示词失败: %s", redact_sensitive_text(str(exc)))
            yield event.plain_result(f"暂时无法反推提示词：{redact_sensitive_text(str(exc))[:200]}")
            return
        if not prompt:
            yield event.plain_result("当前 LLM 没有返回有效提示词。")
            return
        saved = await asyncio.to_thread(self._save_reverse_prompt, event, ref.data, prompt)
        yield event.plain_result(f"反推编号：{saved['image_code']}\nLLM 反推结果，非原始提示词。")
        yield event.plain_result(prompt)

    @filter.command("生图状态", alias={"生图记录状态"})
    async def cmd_image_model_status(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
    ) -> AsyncGenerator[Any, None]:
        """查看近期生图模型的成功率、耗时、错误和历史状态。"""
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        fallback = " ".join(item for item in (p1, p2, p3) if item).strip()
        message = extract_command_message(event, ("生图状态", "生图记录状态"), fallback).strip()
        window_token = "7d"
        page = 1
        invalid: List[str] = []
        for token in re.split(r"[\s,，]+", message):
            token = str(token or "").strip().lower()
            if not token:
                continue
            if token in {"1h", "24h", "7d", "30d"}:
                window_token = token
            elif token.isdigit() and int(token) > 0:
                page = int(token)
            elif token not in {"状态", "查看", "默认", "全部"}:
                invalid.append(token)
        if invalid:
            yield event.plain_result("参数格式：/生图状态 [1h|24h|7d|30d] [页码]")
            return
        try:
            window_seconds = metric_window_seconds(window_token)
            if window_seconds is None:
                window_seconds = 7 * 24 * 60 * 60
            status = self.get_model_status(window_seconds=window_seconds)
        except Exception as exc:
            logger.warning("[SelfieImage] 生图模型状态读取失败: %s", redact_sensitive_text(str(exc)))
            yield event.plain_result("生图模型状态暂时无法读取，请稍后再试。")
            return
        try:
            report_path = self._render_model_status_image(status, page=page)
        except Exception as exc:
            logger.warning("[SelfieImage] 生图模型状态图片渲染失败: %s", redact_sensitive_text(str(exc)))
            report_path = None
        if report_path:
            try:
                image_result = event.image_result(report_path)
            except Exception as exc:
                logger.warning("[SelfieImage] 生图模型状态图片发送失败: %s", redact_sensitive_text(str(exc)))
                image_result = None
            if image_result is not None:
                yield image_result
                return
        yield event.plain_result(self._format_model_status_text(status, page=page))

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
        """查看生图任务；`历史` 可列出最近完成任务。"""
        fallback = " ".join(item for item in [p1, p2] if item).strip()
        async for item in self._command_task_list(event, "生图任务", fallback):
            yield item

    @filter.command("生图取消")
    async def cmd_image_task_cancel(self, event: AstrMessageEvent, p1: str = "", p2: str = "") -> AsyncGenerator[Any, None]:
        """取消排队中或进行中的生图任务。可跟任务号或列表编号。"""
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

    @filter.command("生图重发")
    async def cmd_image_retry_failed(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """重发本会话最近发送失败、仍存在于缓存中的图片。"""
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        result = await self.retry_failed_images(event)
        if result.get("busy"):
            yield event.plain_result("本会话正在重发图片，请稍后再试。")
        elif result["sent"] or result.get("unknown"):
            text = f"已重发 {result['sent']} 张图片。" if result["sent"] else ""
            if result.get("unknown"):
                text += f"{result['unknown']} 张图片的投递结果未知，未自动重复发送。"
            yield event.plain_result(text)
        elif result.get("remaining"):
            yield event.plain_result(f"重发失败，仍有 {result['remaining']} 张图片待重发。")
        else:
            yield event.plain_result("没有可重发的图片。")

    @filter.command("上下文")
    async def cmd_context_view(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """查看当前会话的截断上下文摘要。"""
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        data = self.get_conversation_context(event, 12)
        messages = data.get("messages") if isinstance(data, dict) else []
        lines = [f"当前会话上下文：{len(messages)} 条（持久化{'已开启' if data.get('enabled') else '未开启'}）"]
        for item in messages[-12:]:
            if not isinstance(item, dict):
                continue
            content = " ".join(str(item.get("content") or "").split())[:100]
            if content:
                source_labels = {
                    "astrbot": "框架上下文",
                    "llm_response": "助手回复",
                    "plugin": "插件消息",
                    "llm_request": "LLM请求",
                }
                source = source_labels.get(str(item.get("source") or ""), "消息")
                lines.append(
                    f"[{source}] {'我' if item.get('is_bot') else item.get('sender_name') or '用户'}：{content}"
                )
        yield event.plain_result("\n".join(lines))

    @filter.command("上下文清除")
    async def cmd_context_clear(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        result = self.clear_conversation_context(event)
        yield event.plain_result(f"已清除当前会话上下文 {result.get('cleared', 0)} 条。")

    @filter.command("记录复用", alias={"记录重生"})
    async def cmd_record_reuse(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
    ) -> AsyncGenerator[Any, None]:
        """按记录 ID 创建一条独立的图片重生成任务。"""
        denied = self._permission_denied_message(event)
        if denied:
            yield event.plain_result(denied)
            return
        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6] if item).strip()
        message = extract_command_message(event, "记录复用", fallback)
        parts = message.split(maxsplit=1)
        if not parts or not parts[0].strip():
            yield event.plain_result("用法：记录复用 <记录编号> [修改要求]")
            return
        record_id = parts[0].strip()
        feedback = parts[1].strip() if len(parts) > 1 else ""
        try:
            task = self.start_record_reuse_task(
                record_id,
                feedback=feedback,
                owner_session=self._session_key(event),
            )
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:
            yield event.plain_result(f"记录复用失败：{redact_sensitive_text(str(exc))}")
            return
        yield event.plain_result(f"已提交记录复用任务：{task.get('task_id', '')}")
        result = await self._await_command_image_task(task)
        files = [str(item) for item in (result.get("files") or []) if str(item).strip()]
        if files:
            await self._send_generated_images(event, files)
        if not result.get("success") and not files:
            yield event.plain_result(f"记录复用失败：{result.get('error') or '生成失败'}")

    @filter.command("nai", alias={"NAI"})
    async def cmd_nai(
        self, event: AstrMessageEvent,
        p1: str = "", p2: str = "", p3: str = "", p4: str = "", p5: str = "",
        p6: str = "", p7: str = "", p8: str = "", p9: str = "", p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        """NAI 生图：优先已启用 NAI4，否则 NAI5，支持 [画师名]。"""
        fallback = " ".join(item for item in (p1, p2, p3, p4, p5, p6, p7, p8, p9, p10) if item)
        async for result in self._handle_nai_command(event, fallback):
            yield result

    @filter.command("nai5", alias={"NAI5"})
    async def cmd_nai5(
        self, event: AstrMessageEvent,
        p1: str = "", p2: str = "", p3: str = "", p4: str = "", p5: str = "",
        p6: str = "", p7: str = "", p8: str = "", p9: str = "", p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        """只使用已启用 NAI5 模型，支持 [画师名]。"""
        fallback = " ".join(item for item in (p1, p2, p3, p4, p5, p6, p7, p8, p9, p10) if item)
        async for result in self._handle_nai_command(event, fallback, v5_only=True):
            yield result

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
        async for result in self._get_image_command_service().draw(
            event, p1, p2, p3, p4, p5, p6, p7, p8, p9, p10
        ):
            yield result

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
        async for result in self._get_image_command_service().raw_text_to_image(
            event, p1, p2, p3, p4, p5, p6, p7, p8, p9, p10
        ):
            yield result

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
        async for result in self._get_image_command_service().raw_image_to_image(
            event, p1, p2, p3, p4, p5, p6, p7, p8, p9, p10
        ):
            yield result

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
        async for item in self._get_selfie_workflow_service().selfie(
            event, p1, p2, p3, p4, p5, p6, p7, p8, p9, p10
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
        """生成日常袜装穿搭展示；可指定服装、姿势、自拍或全身构图。"""
        async for item in self._get_selfie_workflow_service().look_legs(
            event, p1, p2, p3, p4, p5, p6, p7, p8, p9, p10
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
        async for item in self._get_selfie_workflow_service().look_cos(
            event, p1, p2, p3, p4, p5, p6, p7, p8, p9, p10
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
        async for item in self._get_selfie_workflow_service().look_you(
            event, p1, p2, p3, p4, p5, p6, p7, p8, p9, p10
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
        async for item in self._get_selfie_workflow_service().group_selfie(
            event, p1, p2, p3, p4, p5, p6, p7, p8, p9, p10
        ):
            yield item

    def _wardrobe_item_by_token(self, token: str):
        wanted = str(token or "").strip()
        items = self.persona.list_wardrobe()
        if wanted.isdigit():
            index = int(wanted) - 1
            return items[index] if 0 <= index < len(items) else None
        return next((item for item in items if item.id == wanted), None)

    def _wardrobe_list_text(self) -> str:
        current = self.persona.current_wardrobe_item()
        items = self.persona.list_wardrobe()
        if not items:
            return "衣橱还是空的。请使用 衣橱添加 并附上一张服装图。"
        lines = [
            f"自定义衣橱：{len(items)} 套，已启用 {sum(1 for item in items if item.enabled)} 套。",
            (
                f"今日服装：{current.name}（{current.id}）"
                if current
                else "今日服装：尚未从衣橱选择，下一次普通自拍时会自动选择。"
            ),
        ]
        for index, item in enumerate(items, 1):
            state = "启用" if item.enabled else "停用"
            tags = f"；标签={','.join(item.tags)}" if item.tags else ""
            lines.append(f"{index}. [{state}] {item.name}（{item.id}）{tags}")
        return "\n".join(lines)

    async def _add_wardrobe_from_event(self, event: AstrMessageEvent, command_name: str, fallback: str):
        text = extract_command_message(event, command_name, fallback, include_quote_text=True)
        sources = extract_image_sources_from_event(event, include_at_avatar=False)
        sources.extend(extract_image_urls(text))
        sources = list(dict.fromkeys(sources))
        if not sources:
            return 0, 0
        name = ""
        description = ""
        tags = ""
        for key, target in (("名称", "name"), ("名字", "name"), ("name", "name"), ("描述", "description"), ("description", "description"), ("标签", "tags"), ("tags", "tags")):
            match = re.search(rf"(?:^|[\s,，;；]){re.escape(key)}\s*[=:：]\s*([^,，;；\n]+)", text, re.I)
            if not match:
                continue
            value = match.group(1).strip()
            if target == "name":
                name = value
            elif target == "description":
                description = value
            else:
                tags = value
        if not name:
            plain = re.sub(r"(?:名称|名字|name|描述|description|标签|tags)\s*[=:：]\s*[^,，;；\n]+", "", text, flags=re.I)
            plain = re.sub(r"https?://\S+", "", plain).strip(" \t,，;；")
            name = plain[:120]
        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        max_dimension, max_pixels, max_frames = image_validation_limits(self.config)
        saved = 0
        failed = 0
        async with aiohttp.ClientSession(trust_env=False) as session:
            for source in sources:
                try:
                    fetched = await fetch_image_source(
                        source,
                        session,
                        max_bytes=max_bytes,
                        max_dimension=max_dimension,
                        max_pixels=max_pixels,
                        max_frames=max_frames,
                    )
                    if not fetched:
                        failed += 1
                        continue
                    data, mime = fetched
                    self.persona.add_wardrobe_item(
                        data,
                        mime,
                        name=name,
                        description=description,
                        tags=tags,
                    )
                    saved += 1
                except (OSError, ValueError):
                    failed += 1
        if saved:
            await self.persona.ensure_daily_selfie_profile("衣橱添加")
        return saved, failed

    @filter.command("衣橱")
    async def cmd_wardrobe(self, event: AstrMessageEvent, p1: str = "", p2: str = "", p3: str = "") -> AsyncGenerator[Any, None]:
        """查看自定义衣橱；今日服装仍由今日日程统一显示。"""
        fallback = " ".join(item for item in (p1, p2, p3) if item).strip()
        yield event.plain_result(self._wardrobe_list_text())

    @filter.command("衣橱添加")
    async def cmd_wardrobe_add(self, event: AstrMessageEvent, p1: str = "", p2: str = "", p3: str = "") -> AsyncGenerator[Any, None]:
        fallback = " ".join(item for item in (p1, p2, p3) if item).strip()
        saved, failed = await self._add_wardrobe_from_event(event, "衣橱添加", fallback)
        if not saved and not failed:
            yield event.plain_result("请发送服装图片，可附加 名称=白色连衣裙 描述=夏日连衣裙 标签=夏季,连衣裙。")
            return
        message = f"已添加 {saved} 套衣橱服装。" if saved else "没有添加可用的衣橱服装。"
        if failed:
            message += f"另有 {failed} 张图片读取失败。"
        yield event.plain_result(message + "\n" + self._wardrobe_list_text())

    @filter.command("衣橱删除")
    async def cmd_wardrobe_delete(self, event: AstrMessageEvent, p1: str = "", p2: str = "") -> AsyncGenerator[Any, None]:
        token = " ".join(item for item in (p1, p2) if item).strip()
        item = self._wardrobe_item_by_token(token)
        if item is None:
            yield event.plain_result("请提供正确的衣橱编号或 id。\n" + self._wardrobe_list_text())
            return
        self.persona.remove_wardrobe_item(item.id)
        await self.persona.ensure_daily_selfie_profile("衣橱删除")
        yield event.plain_result(f"已删除衣橱服装：{item.name}。\n" + self._wardrobe_list_text())

    @filter.command("衣橱启用")
    async def cmd_wardrobe_enable(self, event: AstrMessageEvent, p1: str = "", p2: str = "") -> AsyncGenerator[Any, None]:
        token = " ".join(item for item in (p1, p2) if item).strip()
        item = self._wardrobe_item_by_token(token)
        if item is None:
            yield event.plain_result("请提供正确的衣橱编号或 id。\n" + self._wardrobe_list_text())
            return
        self.persona.set_wardrobe_enabled(item.id, True)
        await self.persona.ensure_daily_selfie_profile("衣橱启用")
        yield event.plain_result(f"已启用衣橱服装：{item.name}。\n" + self._wardrobe_list_text())

    @filter.command("衣橱停用")
    async def cmd_wardrobe_disable(self, event: AstrMessageEvent, p1: str = "", p2: str = "") -> AsyncGenerator[Any, None]:
        token = " ".join(item for item in (p1, p2) if item).strip()
        item = self._wardrobe_item_by_token(token)
        if item is None:
            yield event.plain_result("请提供正确的衣橱编号或 id。\n" + self._wardrobe_list_text())
            return
        self.persona.set_wardrobe_enabled(item.id, False)
        await self.persona.ensure_daily_selfie_profile("衣橱停用")
        yield event.plain_result(f"已停用衣橱服装：{item.name}。\n" + self._wardrobe_list_text())

    @filter.command("形象查看")
    async def cmd_persona_status(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """查看当前形象参考图、形象类型与今日状态。"""
        await self.persona.ensure_daily_selfie_profile("查看今日自拍设定")
        path = self.persona.get_reference_path()
        if path:
            yield event.chain_result([self._create_image_component(path)])
        yield event.plain_result(self.persona.status_text())

    @filter.command("今日日程")
    async def cmd_daily_schedule(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """查看当前本地日程事件链。"""
        await self.persona.ensure_daily_selfie_profile("查看今日日程")
        yield event.plain_result(self.persona.daily_schedule_text())

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
        max_dimension, max_pixels, max_frames = image_validation_limits(self.config)
        async with aiohttp.ClientSession(trust_env=False) as session:
            for source in sources:
                fetched = await fetch_image_source(
                    source,
                    session,
                    max_bytes=max_bytes,
                    max_dimension=max_dimension,
                    max_pixels=max_pixels,
                    max_frames=max_frames,
                )
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
        max_dimension, max_pixels, max_frames = image_validation_limits(self.config)
        saved = 0
        failed = 0
        async with aiohttp.ClientSession(trust_env=False) as session:
            for source in sources:
                if current_count + saved >= 3:
                    break
                try:
                    fetched = await fetch_image_source(
                        source,
                        session,
                        max_bytes=max_bytes,
                        max_dimension=max_dimension,
                        max_pixels=max_pixels,
                        max_frames=max_frames,
                    )
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
        await self.persona.refresh_daily_selfie_profile_local()
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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("生图加黑", prefix_optional=True)
    async def cmd_image_access_black(
        self, event: AstrMessageEvent,
        p1: str = "", p2: str = "", p3: str = "", p4: str = "", p5: str = "",
        p6: str = "", p7: str = "", p8: str = "", p9: str = "", p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        """将用户加入生图黑名单，可附加原因。"""
        fallback = " ".join(item for item in (p1, p2, p3, p4, p5, p6, p7, p8, p9, p10) if item).strip()
        payload = extract_command_message(event, "生图加黑", fallback)
        success, message = self._mutate_access_user("black", event, payload)
        yield event.plain_result(f"{'✅' if success else '❌'} {message}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("生图加白", prefix_optional=True)
    async def cmd_image_access_white(
        self, event: AstrMessageEvent,
        p1: str = "", p2: str = "", p3: str = "", p4: str = "", p5: str = "",
        p6: str = "", p7: str = "", p8: str = "", p9: str = "", p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        """将用户加入生图白名单，可附加原因。"""
        fallback = " ".join(item for item in (p1, p2, p3, p4, p5, p6, p7, p8, p9, p10) if item).strip()
        payload = extract_command_message(event, "生图加白", fallback)
        success, message = self._mutate_access_user("white", event, payload)
        yield event.plain_result(f"{'✅' if success else '❌'} {message}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("生图释放", prefix_optional=True)
    async def cmd_image_access_release(
        self, event: AstrMessageEvent,
        p1: str = "", p2: str = "", p3: str = "", p4: str = "", p5: str = "",
        p6: str = "", p7: str = "", p8: str = "", p9: str = "", p10: str = "",
    ) -> AsyncGenerator[Any, None]:
        """从生图黑名单和白名单中移除用户。"""
        fallback = " ".join(item for item in (p1, p2, p3, p4, p5, p6, p7, p8, p9, p10) if item).strip()
        payload = extract_command_message(event, "生图释放", fallback)
        success, message = self._mutate_access_user("release", event, payload)
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
        model: str = "",
        ack_message: str = "",
    ) -> Optional[str]:
        """
        使用生图模型生成普通图片，支持文生图和参考图图生图。
        自拍、看看COS、看看腿、看看你、AI 自己、合影、合照、同框、与用户一起拍照等请求使用 generate_selfie。
        prompt 保持简洁，保留主体、场景、动作/风格、构图和参考图关系即可。
        用户点名要用某个模型时（例如“用gpt画一只猫”“用即梦生成”），把提到的模型关键词填入 model；没有点名就留空，自动按启用顺序路由。
        闲聊中顺势画图时，ack_message 用当前人格自然短句接话，简体中文 10-40 字。
        Args:
            prompt(string): 简洁生图提示词，描述主体、场景、动作/风格、构图和参考图使用方式。
            count(number): 调用生图次数，默认 1；每次调用可能返回一张或多张图片。
            aspect_ratio(string): 宽高比，例如 1:1、3:4、9:16、16:9；留空使用默认值。
            resolution(string): 分辨率，例如 1K、2K、4K；留空使用默认值。
            size(string): 兼容参数，可传 1024x1024、2048x2048 或 4096x4096。
            model(string): 可选。用户点名使用的模型关键词，如 "gpt-image-2"、"gpt"、"即梦"；留空表示不指定，自动使用启用的模型顺序。指定的模型不存在时自动回退到启用模型。
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
                "model": model,
            },
        )
        requested_count = self._normalize_count(count)
        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            return self._tool_soft_fail(error)
        prompt, aspect, resol, _, _ = self._resolve_image_preset(prompt, aspect_ratio, resolution or size)
        if not prompt:
            return self._tool_soft_fail("缺少生图提示词", "你想让我往什么感觉走？")
        framework_persona = await resolve_framework_persona(getattr(self, "context", None), event)
        if self._looks_like_selfie_intent(prompt, bot_name=framework_persona.name):
            return await self._run_llm_selfie_flow(event, prompt, requested_count, aspect, resol, ack_message, model_request=model)

        await self._send_progress_text(
            event,
            await self._build_contextual_progress_text(event, "image", prompt, requested_count, ack_message),
        )
        # Plain LLM image generation may use the explicitly attached image or
        # a clear "use the previous image" follow-up, but must not silently
        # borrow the bot persona image.  Persona references belong to the
        # dedicated generate_selfie path.
        refs = await self._event_reference_images(
            event,
            include_at_avatar=True,
            context_hint=prompt,
            allow_context_fallback=True,
            include_persona=False,
        )
        async def runner(task_id: str) -> Dict[str, Any]:
            return await self._background_draw_batches(
                task_id,
                event,
                prompt,
                aspect,
                resol,
                refs,
                "llm-generate-image",
                requested_count,
                passthrough=True,
                fail_label=self._natural_fail_fallback("image"),
                model_request=model,
            )

        task = self.start_command_image_task(
            event,
            source="llm-generate-image",
            summary={
                "original_prompt": prompt,
                "aspect_ratio": aspect,
                "resolution": resol,
                "requested_count": requested_count,
                "reference_image_count": len(refs),
                "requested_model": str(model or "").strip(),
                "kind": "LLM 生图",
            },
            runner=runner,
            reference_images=refs,
        )
        result = await self._await_command_image_task(task)
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
        model: str = "",
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
        对“看看COS / 看看腿 / 看看你 / 自拍”等模式请求，在 action 中保留对应模式词，不要改写成丢失模式的纯服装描述。
        用户点名要用某个模型时，把提到的模型关键词填入 model；没有点名就留空，自动按启用顺序路由。
        ack_message 使用简体中文，以当前人格自然回应，10-40 字。
        Args:
            action(string): 简洁自拍/合影要求，包含动作、表情、服装、环境、镜头或同框关系。
            count(number): 调用自拍生图次数，默认 1；每次调用可能返回一张或多张图片。
            aspect_ratio(string): 宽高比，例如 1:1、3:4、9:16、16:9；留空使用默认值。
            resolution(string): 分辨率，例如 1K、2K、4K；留空使用默认值。
            size(string): 兼容参数，可传 1024x1024、2048x2048 或 4096x4096。
            model(string): 可选。用户点名使用的模型关键词，如 "gpt-image-2"、"gpt"；留空表示不指定，自动使用启用的模型顺序。指定的模型不存在时自动回退到启用模型。
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
                "model": model,
            },
        )
        requested_count = self._normalize_count(count)
        action, aspect, resol, _, _ = self._resolve_image_preset(action or "看着镜头自然自拍", aspect_ratio, resolution or size)
        return await self._run_llm_selfie_flow(event, action, requested_count, aspect, resol, ack_message, model_request=model)

    @LLM_TOOL(name="retry_last_generation")
    async def tool_retry_last_generation(
        self,
        event: AstrMessageEvent,
        feedback: str = "",
    ) -> Optional[str]:
        """
        重新生成本会话最近一次图片或自拍。
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
        return self._tool_soft_fail("没有找到本会话最近一次生成请求", "你想重新来哪一张？")
