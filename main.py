"""AstrBot port of Napcat AICat image and selfie features."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import random
import re
import threading
import time
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

from .constants import PLUGIN_AUTHOR, PLUGIN_NAME, PLUGIN_VERSION
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


def append_anatomy_constraints(prompt: str) -> str:
    raw = str(prompt or "").strip()
    if not raw:
        return raw
    return "\n".join(
        [
            raw,
            "",
            "Quality and anatomy constraints:",
            "1. Human bodies must be anatomically complete and natural.",
            "2. Keep the correct number of heads, arms, hands, fingers, legs and feet for each person.",
            "3. No missing limbs, no extra limbs, no malformed limbs, no fused limbs, no detached body parts.",
            "4. No extra fingers, no missing fingers, no twisted hands, no broken wrists, no duplicated hands or feet.",
        ]
    )


def build_prompt_with_reference_instruction(prompt: str, images: List[ImageReference]) -> str:
    raw = str(prompt or "").strip()
    if not images:
        return append_anatomy_constraints(raw)
    multi_person_rules: List[str] = [
        "8. Carefully inspect every reference image for all distinct visible people or characters.",
        "9. If a single reference image contains multiple people, preserve the actual number of visible people from that image unless the user explicitly asks for only one person.",
        "10. Do not extract only one person from a multi-person reference image; do not merge multiple people into one face or body.",
        "11. For real-person reference photos, keep each real person as a separate person with their own recognizable identity, face, hairstyle, outfit, body shape, and relative position.",
    ]
    return "\n".join(
        [
            "The user has provided reference image(s).",
            "",
            "Reference image rules:",
            "1. Use the provided image(s) as visual references.",
            "2. If the user asks to change clothes, outfit, pose, action, style, composition, character appearance, scene, or camera angle, follow the reference image(s).",
            "3. Do not ignore the reference image(s).",
            "4. If there are multiple reference images, use them according to the user request.",
            "5. Keep the final image as a single complete coherent image, not a collage, not split screen, not multiple panels.",
            "6. Do not add text, watermark, UI, borders, or captions.",
            "7. Keep human anatomy complete and natural: correct arms, hands, fingers, legs and feet; no missing limbs, extra limbs, fused limbs, malformed limbs, detached body parts, extra fingers or broken hands.",
            *multi_person_rules,
            "",
            "User request:",
            append_anatomy_constraints(raw),
        ]
    )


@register(PLUGIN_NAME, PLUGIN_AUTHOR, f"AICat 生图自拍 v{PLUGIN_VERSION}", PLUGIN_VERSION)
class AICatPlugin(Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        self.data_dir = os.path.join(str(get_astrbot_data_path()), "plugin_data", PLUGIN_NAME)
        os.makedirs(self.data_dir, exist_ok=True)
        self.config_path = os.path.join(self.data_dir, "aicat_config.json")
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
        self._records: List[Dict[str, Any]] = self._load_records()
        self._record_seq = len(self._records)
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
            logger.info(f"[AICat] Flask Web 已启动: http://{self.config.web_host}:{self.config.web_port}")
        except Exception as exc:
            logger.error(f"[AICat] Flask Web 启动失败: {exc}", exc_info=True)

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
            return "当前仅允许可使用人员白名单内用户使用 AICat 生图。"
        return "你已被加入用户黑名单，无法使用 AICat 生图。"

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

    def _validate_prompt(self, prompt: str, user_id: str = "", event: Optional[AstrMessageEvent] = None) -> str:
        if self._is_whitelisted(event, user_id):
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
                f"{name}去整理一下镜头，很快回来。",
                "我去找个自然点的角度，等我一下。",
                "这张我想拍得松弛一点，马上给你看。",
                "我先收拾一下表情和光线。",
            ]
            if multi:
                options.extend(["我多试几个角度，挑顺眼的给你看。", f"{name}多拍几张，别急。"])
            return random.choice(options)
        options = [
            f"{name}先把画面理顺，很快给你看。",
            "我先想一下构图和光线。",
            "这个我有画面了，等我一下。",
            "我去把画面搭起来。",
        ]
        if multi:
            options.extend(["我多试几版构图，等我一下。", f"{name}多跑几张看看效果。"])
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
                async with session.post(url, json={"contents": [{"parts": parts}]}, headers=headers, timeout=timeout) as response:
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
            async with session.post(url, json=payload, headers=headers, timeout=timeout) as response:
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
        if event is None:
            return True, ""
        error = self._validate_prompt(prompt, user_id, event)
        if error:
            return False, error
        if self._is_whitelisted(event, user_id):
            return True, ""
        if not self.config.image_enable_prompt_audit:
            return True, ""

        audit_prompt = self.config.image_prompt_audit_template.replace("{prompt}", str(prompt or ""))
        try:
            target = self._find_audit_target(self.config.image_prompt_audit_model)
            text = await self._audit_chat_via_target(target, audit_prompt) if target else await self._audit_prompt_via_astrbot(event, audit_prompt)
        except Exception as exc:
            return False, str(exc)
        return self._parse_audit_response(text)

    async def _audit_output_images(self, files: List[str], user_id: str = "", prompt: str = "", event: Optional[AstrMessageEvent] = None) -> Tuple[bool, str]:
        if event is None:
            return True, ""
        if self._is_whitelisted(event, user_id):
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
        for _, path in candidates[:10]:
            try:
                os.remove(path)
                deleted.append(self._cache_relative_path(path))
            except OSError:
                pass
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

    async def _event_reference_images(self, event: AstrMessageEvent, include_at_avatar: bool = False) -> List[ImageReference]:
        sources = self._filter_reference_images(event, extract_image_sources_from_event(event, include_at_avatar=include_at_avatar))
        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        result: List[ImageReference] = []
        seen = set()
        if not sources:
            return result
        async with aiohttp.ClientSession() as session:
            for source in sources:
                fetched = await fetch_image_source(source, session, max_bytes=max_bytes)
                if not fetched:
                    continue
                data, mime = fetched
                key = (len(data), data[:64])
                if data and key not in seen:
                    result.append(ImageReference(data=data, mime_type=normalize_image_mime(mime or detect_mime_by_bytes(data))))
                    seen.add(key)
        return result

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
            sent += 1
            await asyncio.sleep(0.4)
        return sent

    async def _send_progress_text(self, event: AstrMessageEvent, text: str) -> None:
        if not self._progress_text_allowed(event):
            return
        try:
            await event.send(event.plain_result(text))
        except Exception as exc:
            logger.warning(f"[AICat] 发送进度消息失败: {exc}")

    def _build_progress_text(self, kind: str, user_request: str, count: int, ack_message: str = "") -> str:
        if kind == "selfie":
            return self._selfie_ack_text(user_request, count, ack_message)
        return self._image_ack_text(user_request, count, ack_message)

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
            f"2. {prefix}预设 添加 预设名:提示词",
            f"3. {prefix}预设 删除 预设名",
            "",
        ]
        if total_pages > 1:
            if current_page < total_pages:
                lines.append(f"下一页：{prefix}预设 {current_page + 1}")
            if current_page > 1:
                lines.append(f"上一页：{prefix}预设 {current_page - 1}")
            lines.append("")

        for idx, (name, preset) in enumerate(items, start=start + 1):
            desc = preset.description or preset.prompt
            extra = preset.extra_prompt
            params = []
            if preset.aspect_ratio:
                params.append(f"比例: {preset.aspect_ratio}")
            if preset.resolution:
                params.append(f"分辨率: {preset.resolution}")
            lines.extend(
                [
                    f"{idx}. {name}",
                    f"提示词: {preset.prompt}",
                    *( [f"额外提示词: {extra}"] if extra else [] ),
                    *( [f"说明: {desc}"] if desc and desc != preset.prompt else [] ),
                    *( [f"参数: {' | '.join(params)}"] if params else [] ),
                    "",
                ]
            )

        return "\n".join(line for line in lines if line is not None), current_page, total_pages

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
            return "我这会儿还没法把这张整理出来，晚点再来找我。"
        if "缺少生图提示词" in text or "请输入提示词" in text:
            return "你想让我往什么感觉走？也可以直接丢张参考图给我。"
        return fallback or "我刚刚这张没拍稳，换个角度再试一次好不好。"

    def _tool_soft_fail(self, error: str, fallback: str = "") -> str:
        message = self._friendly_user_error_message(error, fallback)
        return f"[TOOL_FAILED] {message}\n请用简体中文、用你自己的语气自然安抚用户，别提生成、工具、配置、次数、冷却或审核这些词。"

    def _tool_unavailable(self, fallback: str) -> str:
        return f"[TOOL_UNAVAILABLE] {fallback}\n请用简体中文、用你自己的语气自然安抚用户，别提功能、工具或配置。"

    def _build_leg_focus_action(self, extra_request: str = "", has_refs: bool = False) -> str:
        base = (
            "【特写自拍 / 晒腿模式】成年角色自然坐姿自拍，第一人称俯视视角（POV，低头看自己的腿）。"
            "像低头看向自己腿部的自然随手拍；也可以使用自然低角度坐姿自拍，但不要拍成正面露脸自拍或对镜站姿时漏脸。"
            "坐在椅子、床沿、沙发或地毯边，双腿自然向前或斜侧摆放，轻微交叠或并拢放松，膝盖与脚尖方向协调，"
            "脚踝线条清晰，避免广角畸变。构图重点：裙摆/裤脚、膝盖、小腿、脚踝完整美观。"
            "如果用户明确要求丝袜，允许穿丝袜（连裤袜、过膝袜、透肤丝袜等），丝袜贴合腿部曲线，可以有轻微勒肉或自然贴合痕迹，"
            "丝袜贴合腿部曲线，允许有自然的肌肤起伏或袜口轻微陷入感，不刻意追求完美紧绷。"
            "脚踝处可有轻微堆积褶皱或自然松弛感，鞋面干净。可有自然手部互动：轻拉袜头、抚平丝袜边缘、整理裙摆或扶膝盖，"
            "不做固定拉扯姿势。整体放松慵懒居家，结合时段光线（晨光/午后漫反射/傍晚暖灯/床边小灯），"
            "环境浅色系、毛绒地毯、木地板、柔和低饱和清透色调。不露完整人脸，膝盖、脚踝边缘可不完全卡紧，允许轻微裁切但不要突兀。"
            " 不要完整露脸，不要把膝盖、脚踝裁得很乱。"
        )
        if has_refs:
            base = "参考用户提供的图片氛围和构图，" + base
        extra = re.sub(r"\s+", " ", str(extra_request or "")).strip(" 。")
        if extra:
            base += f" 额外要求：{extra}。"
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
        await self._send_progress_text(event, self._build_progress_text("selfie", action, requested_count, ack_message))
        extra_refs = await self._event_reference_images(event, include_at_avatar=self._looks_like_group_selfie_intent(action))
        files: List[str] = []
        used_model = ""
        for _ in range(requested_count):
            prompt, refs = await self._build_selfie_prompt_and_refs(action, extra_refs)
            result = await self._run_image_generation(prompt, aspect, resolution, refs, source="llm-generate-selfie", audit_user_id=event_user_id(event), event=event, original_prompt=action)
            if not result.get("success"):
                return self._tool_soft_fail(str(result.get("error") or ""), "这张我刚刚没拍稳。")
            files.extend(result.get("files", []))
            used_model = str(result.get("used_model") or used_model)
        sent = await self._send_generated_images(event, files)
        self._record_generated_images(event, sent)
        return None

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

        request = ImageGenerateRequest(prompt=request_prompt, aspect_ratio=aspect_ratio, resolution=resolution, images=refs)
        started = time.monotonic()
        async with self._semaphore:
            async with aiohttp.ClientSession() as session:
                result = await generate_image_with_fallback(selected_targets, request, session)
        elapsed = time.monotonic() - started

        if result.error or not result.images:
            response_data = {
                "success": False,
                "stage": "generate",
                "error": result.error or "未生成任何图片",
                "used_model": result.used_model,
                "elapsed_seconds": round(elapsed, 2),
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
            return {"success": False, "error": result.error or "未生成任何图片", "elapsed_seconds": elapsed, "used_model": result.used_model}

        generated_image_paths = [self._save_cache_image(image, "generated", detect_mime_by_bytes(image)) for image in result.images if image]
        files = [self._cache_absolute_path(path) for path in generated_image_paths]
        output_ok, output_reason = await self._audit_output_images(files, audit_user_id, prompt, event=event)
        if not output_ok:
            for file_path in files:
                try:
                    os.remove(file_path)
                except OSError:
                    pass
            response_data = {
                "success": False,
                "stage": "output_audit",
                "error": f"图片内容审核未通过：{output_reason}",
                "used_model": result.used_model,
                "elapsed_seconds": round(elapsed, 2),
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
            return {"success": False, "error": f"图片内容审核未通过：{output_reason}", "elapsed_seconds": elapsed, "used_model": result.used_model}

        cleanup = self._cleanup_image_cache_if_needed([*request_image_paths, *generated_image_paths])
        response_data = {
            "success": True,
            "used_model": result.used_model,
            "elapsed_seconds": round(elapsed, 2),
            "count": len(files),
            "generated_image_paths": generated_image_paths,
            "cache_cleanup": cleanup,
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

    async def web_test_image(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        target = self._find_image_target(str(payload.get("channel") or "").strip(), str(payload.get("model") or "").strip())
        if not target:
            raise RuntimeError("未找到指定生图模型")

        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        refs: List[ImageReference] = []
        raw_images = list(payload.get("images") or [])
        if payload.get("image"):
            raw_images.append(payload.get("image"))

        extra_refs: List[ImageReference] = []
        for raw in raw_images:
            data, mime = data_url_to_bytes(str(raw or ""))
            if not data:
                continue
            if len(data) > max_bytes:
                raise RuntimeError(f"参考图过大，最大允许 {self.config.image_max_image_size_mb}MB")
            extra_refs.append(ImageReference(data=data, mime_type=normalize_image_mime(mime or detect_mime_by_bytes(data))))

        original_prompt = str(payload.get("prompt") or "").strip() or "看着镜头自然自拍"
        aspect = str(payload.get("aspect_ratio") or self.config.image_default_aspect_ratio or "自动")
        resolution = str(payload.get("resolution") or self.config.image_default_resolution or "1K")

        if payload.get("use_selfie_reference"):
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
        )
        if not result.get("success"):
            raise RuntimeError(str(result.get("error") or "这次没顺好"))

        return {
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
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as response:
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

    async def _draw_once(self, event: AstrMessageEvent, prompt: str, aspect: str, resolution: str, refs: List[ImageReference], source: str) -> Dict[str, Any]:
        final_prompt = build_prompt_with_reference_instruction(prompt, refs)
        return await self._run_image_generation(final_prompt, aspect, resolution, refs, source=source, audit_user_id=event_user_id(event), event=event, original_prompt=prompt)

    async def _handle_selfie_command(
        self,
        event: AstrMessageEvent,
        command_name: str,
        fallback: str,
        default_action: str,
        default_action_with_refs: str,
        progress_label: str,
        source: str,
        fail_label: str,
        message_override: str = "",
        include_at_avatar: bool = False,
    ) -> AsyncGenerator[Any, None]:
        error = self._quota_error_message(event, 1) or self._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        message = message_override.strip() if message_override else extract_command_message(event, command_name, fallback)
        action, aspect, resolution = self._parse_prompt_options(message)
        extra_refs = await self._event_reference_images(event, include_at_avatar=include_at_avatar)
        if not action:
            action = default_action_with_refs if extra_refs else default_action
        prompt, refs = await self._build_selfie_prompt_and_refs(action, extra_refs)

        hints: List[str] = []
        if not self.persona.has_reference_image():
            hints.append("当前还没有设置 AI 形象参考图，会按人设与今日设定生成主角。")
        if progress_label == "合影" and not extra_refs:
            hints.append("没有读取到合影对象参考图，会按文字要求生成同框对象。")
        progress = self._build_progress_text("selfie", action, 1)
        if hints:
            progress += "\n" + "\n".join(hints)
        yield event.plain_result(progress)

        result = await self._run_image_generation(prompt, aspect, resolution, refs, source=source, audit_user_id=event_user_id(event), event=event, original_prompt=action)
        if not result.get("success"):
            yield event.plain_result(self._friendly_user_error_message(str(result.get("error") or ""), fail_label))
            return

        files = result.get("files", [])
        self._record_generated_images(event, len(files))
        yield event.chain_result([self._create_image_component(path) for path in files])
        info = self._build_success_text(float(result.get("elapsed_seconds") or 0), len(files), str(result.get("used_model") or ""), event)
        if info:
            yield event.plain_result(info)

    def _extract_compact_command_message(self, event: AstrMessageEvent, command_name: str, fallback: str = "") -> str:
        text = extract_event_text(event)
        if not text:
            return fallback.strip()
        match = re.match(rf"^\s*[/!！.]?{re.escape(command_name)}\s*([\s\S]*)$", text)
        if not match:
            return fallback.strip()
        return (match.group(1) or fallback or "").strip()

    def _build_quick_look_action(self, message: str, has_refs: bool) -> Tuple[str, str, str, str]:
        value = re.sub(r"\s+", " ", str(message or "")).strip()
        compact = re.sub(r"[\s，。！？、；：,.!?]", "", value.lower())
        is_group = self._looks_like_group_selfie_intent(value)
        if is_group:
            if value:
                action = f"{value}。AI 自己必须作为画面主角之一，与用户提供的参考图对象自然同框合影；如果同一张参考图里有多个可见人物 / 角色，按实际可见人数全部保留为独立同框对象；如果参考图是动漫、卡通、表情包或非真人对象，默认拟人化 / 真人化成自然同框的人类角色，并保留主要识别特征。"
            else:
                action = "自然同框合影，AI 自己必须作为画面主角之一。"
            if not has_refs:
                action += " 没有合影对象参考图时，按文字要求生成同框对象。"
            return action, "合影", "command-look-group-selfie", "这次合影我没拍稳"

        if not value or compact in {"你", "你自己", "你的样子", "现在", "自拍", "看看你", "看你"}:
            return "看着镜头自然自拍，展示你现在的样子。", "自拍", "command-look-selfie", "这次我没拍稳"

        body_terms = ["腿", "脚", "手", "全身", "半身", "侧身", "站起来", "转身", "姿势", "动作", "表情"]
        clothes_terms = ["旗袍", "裙", "衣服", "服装", "穿搭", "造型", "礼服", "制服", "女仆", "水手服", "jk", "cos", "cosplay"]
        has_body_term = any(term in compact for term in body_terms)
        has_clothes_term = any(term in compact for term in clothes_terms)
        if has_body_term and has_clothes_term:
            action = f"按「{value}」这个组合要求自然自拍：优先呈现指定服装/穿搭，同时重点展示相关身体部位，构图得体，保持 AI 当前形象一致。"
        elif has_clothes_term:
            action = f"穿着{value}自然自拍，展示{value}造型，保持 AI 当前形象一致。"
        elif has_body_term:
            action = f"自然自拍，重点展示{value}，构图得体，保持 AI 当前形象一致。"
        else:
            action = f"按「{value}」这个要求自然自拍，保持 AI 当前形象一致。"
        return action, "自拍", "command-look-selfie", "这次我没拍稳"

    @filter.command("aicat帮助")
    async def cmd_help(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        yield event.plain_result(
            "\n".join(
                [
                    f"AICat 生图自拍 v{PLUGIN_VERSION}",
                    "/画 <预设名或提示词> [额外提示词] [--ar 1:1] [--resolution 2K]",
                    "/预设",
                    "/预设添加 名称:提示词",
                    "/预设删除 名称",
                    "/自拍 <动作/场景/换装/合照要求> [--ar 3:4]",
                    "/看看<内容> 例如 /看看腿、/看看旗袍、/看看合影",
                    "/合影 <动作/场景/合照要求> [--ar 1:1]",
                    "/形象查看",
                    "/形象设置 <发送图片、引用图片或图片链接>",
                    "/形象清除",
                    "/形象刷新",
                    "LLM 工具：generate_image、generate_selfie",
                    f"Flask Web：{'已启用' if self.config.web_enable else '未启用'} http://{self.config.web_host}:{self.config.web_port}",
                ]
            )
        )

    @filter.command("画")
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
        error = self._quota_error_message(event, 1) or self._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        message = extract_command_message(event, "画", fallback)
        prompt, aspect, resolution, _, _ = self._resolve_image_preset(message)
        refs = await self._event_reference_images(event, include_at_avatar=True)
        if not prompt and refs:
            prompt = "根据参考图生成一张自然、清晰、符合原图语义的图片。"
        if not prompt:
            yield event.plain_result("请输入提示词或附带参考图。")
            return

        yield event.plain_result(self._build_progress_text("image", prompt, 1))
        result = await self._draw_once(event, prompt, aspect, resolution, refs, "command-draw")
        if not result.get("success"):
            yield event.plain_result(self._friendly_user_error_message(str(result.get("error") or ""), "这张我刚刚没理顺。"))
            return

        files = result.get("files", [])
        self._record_generated_images(event, len(files))
        yield event.chain_result([self._create_image_component(path) for path in files])
        info = self._build_success_text(float(result.get("elapsed_seconds") or 0), len(files), str(result.get("used_model") or ""), event)
        if info:
            yield event.plain_result(info)

    @filter.command("自拍")
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
            command_name="自拍",
            fallback=fallback,
            default_action="看着镜头自然自拍，展示你现在的样子",
            default_action_with_refs="参考用户提供的图片氛围和构图，看着镜头自然自拍，保持 AI 当前形象一致。",
            progress_label="自拍",
            source="command-selfie",
            fail_label="这次我没拍稳",
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
        raw_extra = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        fallback = self._build_leg_focus_action(raw_extra, bool(extract_image_sources_from_event(event)))
        async for item in self._handle_selfie_command(
            event=event,
            command_name="看看腿",
            fallback=fallback,
            default_action=self._build_leg_focus_action("", False),
            default_action_with_refs=self._build_leg_focus_action("", True),
            progress_label="自拍",
            source="command-look-legs",
            fail_label="这张腿部特写我刚刚没拍稳",
            message_override=fallback,
        ):
            yield item

    @filter.command("看看")
    async def cmd_quick_look(
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
        error = self._quota_error_message(event, 1) or self._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        fallback = " ".join(item for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item).strip()
        message = self._extract_compact_command_message(event, "看看", fallback)
        group_request = self._looks_like_group_selfie_intent(message)
        has_refs = bool(extract_image_sources_from_event(event, include_at_avatar=group_request))
        action, progress_label, source, fail_label = self._build_quick_look_action(message, has_refs)
        async for item in self._handle_selfie_command(
            event=event,
            command_name="看看",
            fallback=action,
            default_action=action,
            default_action_with_refs=action,
            progress_label=progress_label,
            source=source,
            fail_label=fail_label,
            message_override=action,
            include_at_avatar=progress_label == "合影",
        ):
            yield item

    @filter.command("合影")
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
        async for item in self._handle_selfie_command(
            event=event,
            command_name="合影",
            fallback=fallback,
            default_action="和用户自然合影，同框自拍，保持 AI 当前形象一致。",
            default_action_with_refs="和用户提供的参考图角色自然合影，同框自拍，保持 AI 当前形象一致；如果同一张参考图里有多个可见人物 / 角色，按实际可见人数全部保留为独立同框对象；如果参考图是动漫、卡通、表情包或非真人对象，默认拟人化 / 真人化成自然同框的人类角色，并保留主要识别特征。",
            progress_label="合影",
            source="command-group-selfie",
            fail_label="这次合影我没拍稳",
            include_at_avatar=True,
        ):
            yield item

    @filter.command("合照")
    async def cmd_group_photo(
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
            command_name="合照",
            fallback=fallback,
            default_action="和用户自然合照，同框自拍，保持 AI 当前形象一致。",
            default_action_with_refs="和用户提供的参考图角色自然合照，同框自拍，保持 AI 当前形象一致；如果同一张参考图里有多个可见人物 / 角色，按实际可见人数全部保留为独立同框对象；如果参考图是动漫、卡通、表情包或非真人对象，默认拟人化 / 真人化成自然同框的人类角色，并保留主要识别特征。",
            progress_label="合影",
            source="command-group-selfie",
            fail_label="这次合照我没拍稳",
            include_at_avatar=True,
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

        if head in {"列表", "list", "查看"}:
            page = int(tail) if tail.isdigit() else 1
            body, _, _ = self._preset_list_text(page)
            yield event.plain_result(body)
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
                    "用法：/预设 2、/预设 添加 名称:提示词、/预设 删除 名称",
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
        使用 AICat 生图模型生成图片。支持文生图和参考图图生图。
        不要用于生成 AI 自己、自拍、合影、合照、同框、与用户一起拍照；这些请求必须调用 generate_selfie。
        即使你把用户需求整理成英文，只要包含 group selfie、group photo、with you、with me、standing next to、same frame 等含义，也必须调用 generate_selfie。
        如果误把合影/同框/自拍请求传到这里，插件会自动转入 generate_selfie 流程以带上 AI 形象参考图。
        调用前应先根据当前对话、用户语气、上下文和机器人人设，把用户的自然语言需求整理成适合生图的 prompt。
        不要只把用户原话机械复制进 prompt；要补全主体、场景、动作、氛围、构图、风格、约束和参考图关系。
        如果用户是在闲聊中顺势要求画图，ack_message 应像当前人格自然接话，而不是“收到/正在生成”这类模板。
        ack_message 不要复读用户原话或整理后的 prompt，不要把 prompt 包在引号里发给用户。
        ack_message 必须使用简体中文，即使 prompt 或用户原文是英文，也只写 10-40 个中文字的自然反应。
        避免“沿着/顺着/照着 xxx”“收到”“马上为你生成”等僵硬句式。
        Args:
            prompt(string): LLM 根据当前对话整理后的生图提示词，描述主体、风格、场景、构图、细节和参考图使用方式。
            count(number): 生成张数，默认 1。
            aspect_ratio(string): 宽高比，例如 1:1、3:4、9:16、16:9；留空使用默认值。
            resolution(string): 分辨率，例如 1K、2K、4K；留空使用默认值。
            size(string): 兼容参数，可传 1024x1024、2048x2048 或 4096x4096。
            ack_message(string): 可选。根据当前对话和机器人人格生成的简体中文短进度回复，会在开始生图前直接发给用户；应自然、有上下文感，避免模板化和复述用户需求。
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

        await self._send_progress_text(event, self._build_progress_text("image", prompt, requested_count, ack_message))
        refs = await self._event_reference_images(event, include_at_avatar=True)
        files: List[str] = []
        used_model = ""
        for _ in range(requested_count):
            result = await self._draw_once(event, prompt, aspect, resol, refs, "llm-generate-image")
            if not result.get("success"):
                return self._tool_soft_fail(str(result.get("error") or ""), "这张我刚刚没理顺。")
            files.extend(result.get("files", []))
            used_model = str(result.get("used_model") or used_model)
        sent = await self._send_generated_images(event, files)
        self._record_generated_images(event, sent)
        return None

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
        用户要求“合影/合照/同框/和我一起拍/和你一起拍/我们拍一张”时必须使用这个工具，不要使用 generate_image。
        英文整理结果包含 group selfie、group photo、with you、with me、standing next to、same frame 等含义时也属于这个工具。
        本工具会自动带上 AI 当前形象参考图；如果用户消息里附带图片，也会作为合影对象或参考图一起传入。
        调用前应根据当前对话和机器人人设，把用户的要求整理成自拍动作/场景/情绪/服装/镜头语言。
        ack_message 必须使用简体中文，即使 action 或用户原文是英文，也不要用英文回复用户。
        ack_message 应表现出当前人格的反应，例如害羞、认真、调皮或吐槽，而不是固定进度句。
        ack_message 不要复读用户原话或整理后的 action，不要使用“沿着/顺着/照着 xxx”“收到”“马上为你生成”等僵硬句式。
        Args:
            action(string): LLM 根据当前对话整理后的自拍动作、表情、服装、姿势、环境、镜头或合照要求。
            count(number): 生成张数，默认 1。
            aspect_ratio(string): 宽高比，例如 1:1、3:4、9:16、16:9；留空使用默认值。
            resolution(string): 分辨率，例如 1K、2K、4K；留空使用默认值。
            size(string): 兼容参数，可传 1024x1024、2048x2048 或 4096x4096。
            ack_message(string): 可选。根据当前对话和机器人人格生成的简体中文短进度回复，会在开始自拍前直接发给用户；应自然、有上下文感，避免模板化和复述用户需求。
        """
        if not self.config.image_enable_llm_tool:
            return self._tool_unavailable("我这会儿还没法拍这个给你看。")
        requested_count = self._normalize_count(count)
        action, aspect, resol = self._parse_prompt_options(action or "看着镜头自然自拍", aspect_ratio, resolution or size)
        return await self._run_llm_selfie_flow(event, action, requested_count, aspect, resol, ack_message)
