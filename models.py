"""Configuration models and normalization."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .constants import PROVIDER_TYPES, VIDEO_PROVIDER_TYPES


DEFAULT_CONFIG: Dict[str, Any] = {
    "bot_name": "啊呜",
    "personality": "可爱猫娘助手，说话带“喵”等语气词，活泼俏皮会撒娇",
    "web": {
        "enable": True,
        "host": "127.0.0.1",
        "port": 14514,
        "token": "changeme",
    },
    "image": {
        "enable_llm_tool": True,
        "default_aspect_ratio": "自动",
        "default_resolution": "1K",
        "max_concurrent_tasks": 3,
        "global_timeout": 280,
        "max_image_size_mb": 10,
        "cache_limit_mb": 100,
        "show_generation_info": False,
        "show_model_info": False,
        "rate_limit_seconds": 0,
        "enable_daily_limit": False,
        "daily_limit_count": 10,
        "max_batch_count": 2,
        # stop: 一张全失败整批停（旧行为）；skip: 跳过失败张继续凑满；skip_max: 最多跳过 N 张
        "batch_on_failure": "skip",
        "batch_skip_max": 2,
        "blocked_words": [],
        "enable_prompt_audit": False,
        "enable_output_audit": False,
        "prompt_audit_model": "",
        "output_audit_model": "",
        "ocr_model": "",
        "prompt_audit_template": "你是生图安全审核员。请判断以下提示词是否安全。提示词：{prompt}。仅输出 JSON：{\"allow\":true/false,\"reason\":\"原因\"}",
        "output_audit_template": "你是图像安全审核员。请判断以下图片是否适合普通用户。仅输出 JSON：{\"allow\":true/false,\"reason\":\"原因\"}",
        # 无形象参考图时：true=回退 logo 图；false=仅用人设文案生成（不注图）
        "use_logo_when_no_persona": True,
        # Prompt EN for models weak on Chinese (uses audit-channel chat).
        "enable_image_prompt_en": False,
        "enable_video_prompt_en": False,
        "prompt_en_mode": "if_cjk",  # if_cjk | always
        "prompt_en_model": "",
        "image_prompt_en_template": (
            "Translate the image-generation prompt into natural English.\n"
            "Goal: faithful language conversion only. Do not rewrite creative intent.\n"
            "\n"
            "Rules:\n"
            "1. Keep the same meaning, detail order, style tags, and constraints.\n"
            "2. Do not add, remove, or strengthen subjects, clothing, poses, scenes, or bans.\n"
            "3. Do not summarize, beautify, or restructure into a new prompt.\n"
            "4. Proper nouns, model tags, and bracket markers may stay as-is when needed.\n"
            "5. If already English, return it with only minimal grammar fixes.\n"
            "\n"
            "Return ONLY one JSON object (no markdown, no extra text):\n"
            '{"ok":true,"en":"<english prompt>"}\n'
            'On failure still return JSON: {"ok":false,"en":""}\n'
            "\n"
            "Source prompt:\n"
            "{prompt}"
        ),
        "video_prompt_en_template": (
            "Translate the video-generation prompt into natural English.\n"
            "Goal: faithful language conversion only. Do not rewrite creative intent.\n"
            "\n"
            "Rules:\n"
            "1. Keep the same meaning, detail order, motion, camera cues, style, and constraints.\n"
            "2. Do not add, remove, or invent actions, shots, characters, or plot.\n"
            "3. Do not summarize, beautify, or restructure into a new prompt.\n"
            "4. Proper nouns and technical tags may stay as-is when needed.\n"
            "5. If already English, return it with only minimal grammar fixes.\n"
            "\n"
            "Return ONLY one JSON object (no markdown, no extra text):\n"
            '{"ok":true,"en":"<english prompt>"}\n'
            'On failure still return JSON: {"ok":false,"en":""}\n'
            "\n"
            "Source prompt:\n"
            "{prompt}"
        ),

    },
    "permission": {
        "usable_users": "",
        "blocked_users": "",
        "whitelist_users": "",
        "whitelist_groups": "",
    },
    "proxies": [],
    "image_channels": [],
    "audit_channels": [],
    "video_channels": [],
    "enabled_image_model_priority": [],
    "enabled_audit_model_priority": [],
    "enabled_video_model_priority": [],
    # sequence: priority list only, or enabled order when empty; random: one from priority; fixed: priority 1 or enabled 1.
    "image_model_call_mode": "sequence",
    "video": {
        "enable": True,
        "default_duration": 5,
        "max_concurrent_tasks": 1,
        "global_timeout": 300,
    },
}


@dataclass
class ImageModelTarget:
    channel_name: str
    provider_type: str
    base_url: str
    api_key: str
    model: str
    timeout: int
    proxy: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    api_keys: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.channel_name}/{self.model}"

    def resolved_api_keys(self) -> List[str]:
        keys = [str(item).strip() for item in (self.api_keys or []) if str(item).strip()]
        if keys:
            return unique_values(keys)
        primary = str(self.api_key or "").strip()
        return [primary] if primary else []


def split_api_keys(value: Any) -> List[str]:
    """Accept str / list / multiline api_key(s) into ordered unique keys."""
    if value is None:
        return []
    items: List[str] = []
    if isinstance(value, list):
        for item in value:
            items.extend(split_api_keys(item))
        return unique_values([str(item).strip() for item in items if str(item).strip()])
    text = str(value or "").strip()
    if not text:
        return []
    # Support newline / comma / semicolon separated multi-keys in one field.
    for part in re.split(r"[\n\r,;]+", text):
        key = str(part or "").strip()
        if key:
            items.append(key)
    return unique_values(items)


@dataclass
class ImageChannelConfig:
    name: str
    provider_type: str
    base_url: str
    api_key: str
    model: str
    timeout: int = 180
    enabled: bool = True
    enabled_models: List[str] = field(default_factory=list)
    model_provider_types: Dict[str, str] = field(default_factory=dict)
    model_download_proxy_ids: Dict[str, str] = field(default_factory=dict)
    models_cache: List[str] = field(default_factory=list)
    proxy: str = ""  # runtime resolved URL only; prefer proxy_id
    proxy_id: str = ""
    protocol_lock: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)
    api_keys: List[str] = field(default_factory=list)

    def resolved_api_keys(self) -> List[str]:
        keys = split_api_keys(self.api_keys)
        if keys:
            return keys
        return split_api_keys(self.api_key)

    def targets(self, global_timeout: int) -> List[ImageModelTarget]:
        if not self.enabled:
            return []
        models = self.enabled_models or ([self.model] if self.model else [])
        is_video_proto = bool(normalize_video_provider_type(self.provider_type)) or str(self.provider_type or "").startswith(
            "video_"
        )
        # Default-lock OpenAI-compatible *image* channel types so model names cannot jump protocols.
        lock = (not is_video_proto) and (bool(self.protocol_lock) or self.provider_type in {"openai", "gemini_openai"})
        keys = self.resolved_api_keys()
        primary_key = keys[0] if keys else str(self.api_key or "").strip()
        result: List[ImageModelTarget] = []
        for model in models:
            if not model:
                continue
            model_video_override = normalize_video_provider_type(self.model_provider_types.get(model, ""))
            if is_video_proto or model_video_override:
                ptype = resolve_video_model_provider_type(
                    model,
                    self.provider_type if is_video_proto else "openai_video",
                    self.model_provider_types.get(model, ""),
                )
            else:
                ptype = resolve_model_provider_type(
                    model,
                    self.provider_type,
                    self.model_provider_types.get(model, ""),
                    protocol_lock=lock,
                )
            extra = copy.deepcopy(self.extra)
            # Per-model download-only proxy (result URL fetch). Request still uses channel proxy.
            dl_id = str((self.model_download_proxy_ids or {}).get(model) or "").strip()
            if dl_id:
                extra["download_proxy_id"] = dl_id
            result.append(
                ImageModelTarget(
                    channel_name=self.name,
                    provider_type=ptype,
                    base_url=self.base_url,
                    api_key=primary_key,
                    model=model,
                    timeout=max(10, int(global_timeout or self.timeout or 180)),
                    proxy=self.proxy,
                    extra=extra,
                    api_keys=list(keys),
                )
            )
        return result


@dataclass
class AICatConfig:
    raw: Dict[str, Any]
    bot_name: str
    personality: str
    web_enable: bool
    web_host: str
    web_port: int
    web_token: str
    image_enable_llm_tool: bool
    image_default_aspect_ratio: str
    image_default_resolution: str
    image_max_concurrent_tasks: int
    image_global_timeout: int
    image_max_image_size_mb: int
    image_cache_limit_mb: int
    image_show_generation_info: bool
    image_show_model_info: bool
    image_rate_limit_seconds: int
    image_enable_daily_limit: bool
    image_daily_limit_count: int
    image_max_batch_count: int
    image_batch_on_failure: str
    image_batch_skip_max: int
    image_blocked_words: List[str]
    image_enable_prompt_audit: bool
    image_enable_output_audit: bool
    image_prompt_audit_model: str
    image_output_audit_model: str
    image_ocr_model: str
    image_prompt_audit_template: str
    image_output_audit_template: str
    image_use_logo_when_no_persona: bool
    image_enable_image_prompt_en: bool
    image_enable_video_prompt_en: bool
    image_prompt_en_mode: str
    image_prompt_en_model: str
    image_image_prompt_en_template: str
    image_video_prompt_en_template: str
    usable_users: List[str]
    blocked_users: List[str]
    whitelist_users: List[str]
    whitelist_groups: List[str]
    proxies: List[Dict[str, Any]]
    image_channels: List[ImageChannelConfig]
    audit_channels: List[ImageChannelConfig]
    video_channels: List[ImageChannelConfig]
    enabled_image_model_priority: List[str]
    enabled_audit_model_priority: List[str]
    enabled_video_model_priority: List[str]
    image_model_call_mode: str
    video_enable: bool
    video_default_duration: int
    video_max_concurrent_tasks: int
    video_global_timeout: int

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AICatConfig":
        raw = normalize_config_tree(deep_merge(DEFAULT_CONFIG, data if isinstance(data, dict) else {}))
        raw = normalize_legacy_keys(raw)

        web = ensure_dict(raw, "web")
        image = ensure_dict(raw, "image")
        video = ensure_dict(raw, "video")
        permission = ensure_dict(raw, "permission")

        proxies = normalize_proxies_list(raw.get("proxies") or raw.get("proxy_list") or [])
        # Migrate legacy per-channel proxy URL strings into shared proxies list once.
        def _migrate_channel_proxy(channel: ImageChannelConfig) -> None:
            if channel.proxy_id:
                channel.proxy = resolve_proxy_url_from_config(proxies, channel.proxy_id, "")
                return
            legacy = str(channel.proxy or "").strip()
            if not legacy:
                channel.proxy = ""
                return
            # Reuse existing matching proxy if same URL
            for row in proxies:
                if str(row.get("url") or "") == legacy:
                    channel.proxy_id = str(row.get("id") or "")
                    channel.proxy = legacy if row.get("enabled") is not False else ""
                    return
            row = normalize_proxy_entry({"url": legacy, "name": f"迁移·{channel.name}", "enabled": True})
            if row:
                proxies.append(row)
                channel.proxy_id = row["id"]
                channel.proxy = row["url"]
            else:
                # keep raw legacy if unparsable
                channel.proxy = legacy

        channels = [_build_image_channel(item) for item in template_list_items(raw.get("image_channels"))]
        channels = [channel for channel in channels if channel.name and channel.provider_type in PROVIDER_TYPES]
        audit_channels = [_build_image_channel(item) for item in template_list_items(raw.get("audit_channels"))]
        audit_channels = [channel for channel in audit_channels if channel.name and channel.provider_type in PROVIDER_TYPES]
        video_channels = [_build_video_channel(item) for item in template_list_items(raw.get("video_channels"))]
        video_channels = [channel for channel in video_channels if channel.name]
        for channel in (*channels, *audit_channels, *video_channels):
            _migrate_channel_proxy(channel)
        raw["proxies"] = proxies
        # Persist proxy_id on raw channel dicts for Web round-trip; drop bare proxy string preference.
        for key, chs in (("image_channels", channels), ("audit_channels", audit_channels), ("video_channels", video_channels)):
            raw_list = template_list_items(raw.get(key))
            by_name = {c.name: c for c in chs}
            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("id") or "").strip()
                ch = by_name.get(name)
                if not ch:
                    continue
                if ch.proxy_id:
                    item["proxy_id"] = ch.proxy_id
                # Keep resolved URL only for runtime compatibility; UI uses proxy_id.
                if ch.proxy:
                    item["proxy"] = ch.proxy
                elif "proxy" in item and ch.proxy_id:
                    item.pop("proxy", None)

        prompt_en_mode = str(image.get("prompt_en_mode") or image.get("promptEnMode") or "if_cjk").strip().lower() or "if_cjk"
        if prompt_en_mode not in {"if_cjk", "always", "cjk", "chinese", "zh"}:
            prompt_en_mode = "if_cjk"
        if prompt_en_mode in {"cjk", "chinese", "zh"}:
            prompt_en_mode = "if_cjk"
        batch_on_failure = str(image.get("batch_on_failure") or image.get("batchOnFailure") or "skip").strip().lower() or "skip"
        if batch_on_failure in {"continue", "skip_continue", "skip-continue"}:
            batch_on_failure = "skip"
        if batch_on_failure not in {"stop", "skip", "skip_max"}:
            batch_on_failure = "skip"
        source_data = data if isinstance(data, dict) else {}
        image_model_call_mode = str(
            source_data.get("image_model_call_mode") or source_data.get("imageModelCallMode") or ""
        ).strip().lower()
        if not image_model_call_mode:
            image_model_call_mode = "random" if to_bool(
                source_data.get("random_image_model")
                or source_data.get("randomImageModel")
                or source_data.get("image_model_random")
                or source_data.get("imageModelRandom"),
                False,
            ) else "sequence"
        if image_model_call_mode not in {"sequence", "random", "fixed"}:
            image_model_call_mode = "sequence"
        raw["image_model_call_mode"] = image_model_call_mode
        raw.pop("random_image_model", None)
        return cls(
            raw=raw,
            bot_name=str(raw.get("bot_name") or raw.get("botName") or DEFAULT_CONFIG["bot_name"]).strip() or "AI",
            personality=str(raw.get("personality") or DEFAULT_CONFIG["personality"]).strip(),
            web_enable=to_bool(web.get("enable"), True),
            web_host=str(web.get("host") or "127.0.0.1").strip() or "127.0.0.1",
            web_port=to_int(web.get("port"), 14514, minimum=1, maximum=65535),
            web_token=str(web.get("token") or "").strip(),
            image_enable_llm_tool=to_bool(image.get("enable_llm_tool"), True),
            image_default_aspect_ratio=str(image.get("default_aspect_ratio") or "自动").strip() or "自动",
            image_default_resolution=str(image.get("default_resolution") or "1K").strip() or "1K",
            image_max_concurrent_tasks=to_int(image.get("max_concurrent_tasks"), 3, minimum=1, maximum=20),
            image_global_timeout=to_int(image.get("global_timeout"), 180, minimum=10, maximum=900),
            image_max_image_size_mb=to_int(image.get("max_image_size_mb"), 10, minimum=1, maximum=100),
            image_cache_limit_mb=to_int(image.get("cache_limit_mb"), 100, minimum=10, maximum=102400),
            image_show_generation_info=to_bool(image.get("show_generation_info"), False),
            image_show_model_info=to_bool(image.get("show_model_info"), False),
            image_rate_limit_seconds=to_int(image.get("rate_limit_seconds"), 0, minimum=0, maximum=3600),
            image_enable_daily_limit=to_bool(image.get("enable_daily_limit"), False),
            image_daily_limit_count=to_int(image.get("daily_limit_count"), 10, minimum=1, maximum=1000),
            image_max_batch_count=to_int(image.get("max_batch_count"), 2, minimum=1, maximum=8),
            image_batch_on_failure=batch_on_failure,
            image_batch_skip_max=to_int(image.get("batch_skip_max") or image.get("batchSkipMax"), 2, minimum=0, maximum=8),
            image_blocked_words=split_values(image.get("blocked_words")),
            image_enable_prompt_audit=to_bool(image.get("enable_prompt_audit"), False),
            image_enable_output_audit=to_bool(image.get("enable_output_audit"), False),
            image_prompt_audit_model=str(image.get("prompt_audit_model") or "").strip(),
            image_output_audit_model=str(image.get("output_audit_model") or "").strip(),
            image_ocr_model=str(image.get("ocr_model") or "").strip(),
            image_prompt_audit_template=str(image.get("prompt_audit_template") or DEFAULT_CONFIG["image"]["prompt_audit_template"]),
            image_output_audit_template=str(image.get("output_audit_template") or DEFAULT_CONFIG["image"]["output_audit_template"]),
            image_use_logo_when_no_persona=to_bool(image.get("use_logo_when_no_persona"), True),
            image_enable_image_prompt_en=to_bool(image.get("enable_image_prompt_en") or image.get("enableImagePromptEn"), False),
            image_enable_video_prompt_en=to_bool(image.get("enable_video_prompt_en") or image.get("enableVideoPromptEn"), False),
            image_prompt_en_mode=prompt_en_mode,
            image_prompt_en_model=str(image.get("prompt_en_model") or image.get("promptEnModel") or "").strip(),
            image_image_prompt_en_template=str(image.get("image_prompt_en_template") or image.get("imagePromptEnTemplate") or "").strip() or DEFAULT_CONFIG["image"]["image_prompt_en_template"],
            image_video_prompt_en_template=str(image.get("video_prompt_en_template") or image.get("videoPromptEnTemplate") or "").strip() or DEFAULT_CONFIG["image"]["video_prompt_en_template"],
            usable_users=split_values(permission.get("usable_users")),
            blocked_users=split_values(permission.get("blocked_users")),
            whitelist_users=split_values(permission.get("whitelist_users")),
            whitelist_groups=split_values(permission.get("whitelist_groups")),
            proxies=proxies,
            image_channels=channels,
            audit_channels=audit_channels,
            video_channels=video_channels,
            enabled_image_model_priority=split_values(raw.get("enabled_image_model_priority")),
            enabled_audit_model_priority=split_values(raw.get("enabled_audit_model_priority")),
            enabled_video_model_priority=split_values(raw.get("enabled_video_model_priority")),
            image_model_call_mode=image_model_call_mode,
            video_enable=to_bool(video.get("enable"), True),
            video_default_duration=to_int(video.get("default_duration"), 5, minimum=1, maximum=60),
            video_max_concurrent_tasks=to_int(video.get("max_concurrent_tasks"), 1, minimum=1, maximum=5),
            video_global_timeout=to_int(video.get("global_timeout"), 300, minimum=30, maximum=1800),
        )

    @staticmethod
    def _prioritize_targets(
        all_targets: List[ImageModelTarget],
        priority: List[str],
    ) -> List[ImageModelTarget]:
        if not priority:
            return all_targets
        by_key: Dict[str, ImageModelTarget] = {}
        for target in all_targets:
            by_key[target.label] = target
            by_key[f"{target.channel_name}:{target.model}"] = target
            by_key[target.model] = target
        ordered: List[ImageModelTarget] = []
        seen = set()
        for raw_key in priority:
            target = by_key.get(str(raw_key).strip())
            if target and target.label not in seen:
                ordered.append(target)
                seen.add(target.label)
        for target in all_targets:
            if target.label not in seen:
                ordered.append(target)
                seen.add(target.label)
        return ordered


    def _bind_download_proxies(self, targets: List[ImageModelTarget]) -> List[ImageModelTarget]:
        """Attach resolved download-only proxy URL onto target.extra for result fetching."""
        if not targets:
            return targets
        by_id = {str(row.get("id") or ""): row for row in (self.proxies or []) if isinstance(row, dict)}
        out: List[ImageModelTarget] = []
        for target in targets:
            extra = dict(target.extra or {})
            # Prefer explicit id on extra; also accept per-model map via channel already expanded
            dl_id = str(extra.get("download_proxy_id") or "").strip()
            if dl_id:
                row = by_id.get(dl_id)
                if row and row.get("enabled") is not False:
                    extra["download_proxy"] = str(row.get("url") or "").strip()
                else:
                    extra.pop("download_proxy", None)
            # If no per-model override, do not set download_proxy (fall back to request proxy).
            out.append(
                ImageModelTarget(
                    channel_name=target.channel_name,
                    provider_type=target.provider_type,
                    base_url=target.base_url,
                    api_key=target.api_key,
                    model=target.model,
                    timeout=target.timeout,
                    proxy=target.proxy,
                    extra=extra,
                    api_keys=list(target.api_keys or []),
                )
            )
        return out

    def get_prioritized_targets(self) -> List[ImageModelTarget]:
        all_targets: List[ImageModelTarget] = []
        for channel in self.image_channels:
            all_targets.extend(channel.targets(self.image_global_timeout))

        priority = self.enabled_image_model_priority
        selected = self._prioritize_targets(all_targets, priority)
        if priority:
            allowed = set(priority)
            selected = [
                target for target in selected
                if target.label in allowed or f"{target.channel_name}:{target.model}" in allowed or target.model in allowed
            ]
        mode = self.image_model_call_mode
        if mode == "random":
            if not selected:
                return []
            import random

            selected = [random.choice(selected)]
        elif mode == "fixed":
            selected = selected[:1]
        return self._bind_download_proxies(selected)

    def get_audit_targets(self) -> List[ImageModelTarget]:
        targets: List[ImageModelTarget] = []
        for channel in self.audit_channels:
            targets.extend(channel.targets(self.image_global_timeout))
        return self._bind_download_proxies(self._prioritize_targets(targets, self.enabled_audit_model_priority))

    def get_prioritized_video_targets(self) -> List[ImageModelTarget]:
        if not self.video_enable:
            return []
        targets: List[ImageModelTarget] = []
        for channel in self.video_channels:
            targets.extend(channel.targets(self.video_global_timeout or self.image_global_timeout))
        return self._bind_download_proxies(self._prioritize_targets(targets, self.enabled_video_model_priority))


def normalize_legacy_keys(raw: Dict[str, Any]) -> Dict[str, Any]:
    raw = copy.deepcopy(raw)

    if "imageChannels" in raw and "image_channels" not in raw:
        raw["image_channels"] = raw["imageChannels"]
    if "auditChannels" in raw and "audit_channels" not in raw:
        raw["audit_channels"] = raw["auditChannels"]
    if "enabledImageModelPriority" in raw and "enabled_image_model_priority" not in raw:
        raw["enabled_image_model_priority"] = raw["enabledImageModelPriority"]
    if "enabledAuditModelPriority" in raw and "enabled_audit_model_priority" not in raw:
        raw["enabled_audit_model_priority"] = raw["enabledAuditModelPriority"]
    if "enabledVideoModelPriority" in raw and "enabled_video_model_priority" not in raw:
        raw["enabled_video_model_priority"] = raw["enabledVideoModelPriority"]
    if "videoChannels" in raw and "video_channels" not in raw:
        raw["video_channels"] = raw["videoChannels"]
    if "botName" in raw and "bot_name" not in raw:
        raw["bot_name"] = raw["botName"]

    image = ensure_dict(raw, "image")
    image.pop("audit_whitelist", None)
    legacy_image_keys = {
        "imageEnableLLMTool": "enable_llm_tool",
        "imageDefaultAspectRatio": "default_aspect_ratio",
        "imageDefaultResolution": "default_resolution",
        "imageMaxConcurrentTasks": "max_concurrent_tasks",
        "imageGlobalTimeoutMs": "global_timeout",
        "imageMaxImageSizeMB": "max_image_size_mb",
        "imageCacheLimitMB": "cache_limit_mb",
        "imageShowGenerationInfo": "show_generation_info",
        "imageShowModelInfo": "show_model_info",
        "imageRateLimitSeconds": "rate_limit_seconds",
        "imageEnableDailyLimit": "enable_daily_limit",
        "imageDailyLimitCount": "daily_limit_count",
        "imagePromptBlockedWords": "blocked_words",
        "imageEnablePromptAudit": "enable_prompt_audit",
        "imageEnableOutputAudit": "enable_output_audit",
        "imagePromptAuditModel": "prompt_audit_model",
        "imageOutputAuditModel": "output_audit_model",
        "ocrModel": "ocr_model",
        "imagePromptAuditTemplate": "prompt_audit_template",
        "imageOutputAuditTemplate": "output_audit_template",
    }
    for legacy_key, new_key in legacy_image_keys.items():
        if legacy_key in raw and new_key not in image:
            value = raw[legacy_key]
            if legacy_key == "imageGlobalTimeoutMs":
                value = to_int(value, 180000, minimum=10000) // 1000
            image[new_key] = value

    web = ensure_dict(raw, "web")
    if "webEnable" in raw and "enable" not in web:
        web["enable"] = raw["webEnable"]
    if "webPort" in raw and "port" not in web:
        web["port"] = raw["webPort"]
    if "webToken" in raw and "token" not in web:
        web["token"] = raw["webToken"]

    permission = ensure_dict(raw, "permission")
    if "whitelistQQs" in raw and "whitelist_users" not in permission:
        permission["whitelist_users"] = raw["whitelistQQs"]
    if "ownerQQs" in raw and "whitelist_users" not in permission:
        permission["whitelist_users"] = raw["ownerQQs"]
    if "unlimited_users" in permission and "whitelist_users" not in permission:
        permission["whitelist_users"] = permission["unlimited_users"]
    if "unlimited_groups" in permission and "whitelist_groups" not in permission:
        permission["whitelist_groups"] = permission["unlimited_groups"]

    return raw


def _new_proxy_id() -> str:
    import secrets
    return "px_" + secrets.token_hex(4)


def normalize_proxy_entry(raw: Any) -> Optional[Dict[str, Any]]:
    """Normalize a proxy row into config dict."""
    from .proxy import build_proxy_url, parse_channel_proxy

    data = raw if isinstance(raw, dict) else {}
    protocol = str(data.get("protocol") or data.get("scheme") or "http").strip().lower()
    host = str(data.get("host") or data.get("ip") or "").strip()
    port = data.get("port")
    username = str(data.get("username") or data.get("user") or "").strip()
    password = str(data.get("password") or data.get("pass") or "").strip()
    name = str(data.get("name") or "").strip()
    enabled = to_bool(data.get("enabled"), True)
    # status active/inactive alias
    status = str(data.get("status") or "").strip().lower()
    if status == "inactive":
        enabled = False
    elif status == "active":
        enabled = True
    proxy_id = str(data.get("id") or data.get("proxy_id") or "").strip()
    # Legacy free-form URL field
    legacy_url = str(data.get("url") or data.get("proxy") or "").strip()
    if not host and legacy_url:
        try:
            parsed = parse_channel_proxy(legacy_url)
        except Exception:
            parsed = None
        if parsed:
            protocol = parsed.scheme
            host = parsed.host
            port = parsed.port
            username = parsed.username
            password = parsed.password
    if not host:
        return None
    try:
        url = build_proxy_url(protocol, host, int(port or 0), username, password)
        parsed = parse_channel_proxy(url)
    except Exception:
        return None
    assert parsed is not None
    if not proxy_id:
        proxy_id = _new_proxy_id()
    if not name:
        name = f"{parsed.scheme}://{parsed.host}:{parsed.port}"
    return {
        "id": proxy_id,
        "name": name,
        "protocol": parsed.scheme,
        "host": parsed.host,
        "port": parsed.port,
        "username": parsed.username,
        "password": parsed.password,
        "enabled": bool(enabled),
        "url": parsed.url,
    }


def normalize_proxies_list(raw: Any) -> List[Dict[str, Any]]:
    items = template_list_items(raw) if not isinstance(raw, list) else raw
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for item in items:
        row = normalize_proxy_entry(item)
        if not row:
            continue
        if row["id"] in seen:
            row["id"] = _new_proxy_id()
        seen.add(row["id"])
        out.append(row)
    return out


def resolve_proxy_url_from_config(proxies: List[Dict[str, Any]], proxy_id: str = "", legacy_proxy: str = "") -> str:
    """Resolve runtime proxy URL from proxy_id list, with legacy channel.proxy fallback."""
    pid = str(proxy_id or "").strip()
    if pid:
        for row in proxies or []:
            if str(row.get("id") or "") == pid and row.get("enabled") is not False:
                return str(row.get("url") or "").strip()
        # selected but missing/disabled → no proxy
        return ""
    # legacy free-form URL on channel
    return str(legacy_proxy or "").strip()


def public_proxy_row(row: Dict[str, Any], *, mask_password: bool = True) -> Dict[str, Any]:
    data = dict(row or {})
    if mask_password and data.get("password"):
        data["password"] = "******"
        # keep has_auth flag for UI
    data["has_auth"] = bool(str(row.get("username") or "").strip())
    return data



def _build_image_channel(raw: Any) -> ImageChannelConfig:
    raw = normalize_config_tree(raw)
    if isinstance(raw, dict):
        for key in ("data", "config", "values"):
            if isinstance(raw.get(key), dict):
                raw = normalize_config_tree(raw[key])
                break
        if isinstance(raw.get("items"), dict):
            raw = normalize_config_tree(raw["items"])

    if not isinstance(raw, dict):
        raw = {}

    provider_type = normalize_provider_type(raw.get("provider_type") or raw.get("providerType") or raw.get("api_type") or "openai") or "openai"

    enabled_models: List[str] = []
    model_provider_types: Dict[str, str] = {}
    raw_model_provider_types = raw.get("model_provider_types") or raw.get("modelProviderTypes") or raw.get("provider_types") or raw.get("providerTypes")
    if isinstance(raw_model_provider_types, dict):
        for key, value in raw_model_provider_types.items():
            model_key = str(key or "").strip()
            resolved_type = normalize_provider_type(value)
            if model_key and resolved_type:
                model_provider_types[model_key] = resolved_type

    for item in as_list(raw.get("enabled_models") or raw.get("enabledModels")):
        if isinstance(item, dict):
            if to_bool(item.get("enabled"), True):
                value = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
                if value:
                    enabled_models.append(value)
                    item_provider_type = normalize_provider_type(item.get("provider_type") or item.get("providerType") or item.get("api_type") or item.get("apiType"))
                    if item_provider_type:
                        model_provider_types[value] = item_provider_type
        else:
            value = str(item or "").strip()
            if value:
                enabled_models.append(value)

    api_key_value = raw.get("api_key") or raw.get("apiKey") or ""
    api_keys_value = raw.get("api_keys") or raw.get("apiKeys") or ""
    # Prefer explicit api_keys list; fall back to api_key (may be multiline).
    api_keys = split_api_keys(api_keys_value) or split_api_keys(api_key_value)
    if not api_keys and api_key_value:
        api_keys = split_api_keys(api_key_value)
    api_key_primary = api_keys[0] if api_keys else str(api_key_value or "").strip()
    # Persist multi-line form in api_key for Web textarea round-trip compatibility.
    api_key_stored = "\n".join(api_keys) if len(api_keys) > 1 else api_key_primary

    model = str(raw.get("model") or "").strip()
    if provider_type == "agnes" and not model:
        model = "agnes-image-2.1-flash"
    if model and not enabled_models:
        enabled_models = [model]

    download_proxy_ids: Dict[str, str] = {}
    raw_dl = raw.get("model_download_proxy_ids") or raw.get("modelDownloadProxyIds") or raw.get("download_proxy_ids") or {}
    if isinstance(raw_dl, dict):
        enabled_set = set(enabled_models)
        for model_name, proxy_ref in raw_dl.items():
            name = str(model_name or "").strip()
            pid = str(proxy_ref or "").strip()
            if name and pid and name in enabled_set:
                download_proxy_ids[name] = pid

    return ImageChannelConfig(
        name=str(raw.get("name") or raw.get("id") or "default").strip(),
        provider_type=provider_type,
        base_url=str(raw.get("base_url") or raw.get("baseUrl") or "").strip(),
        api_key=api_key_stored,
        model=model or (enabled_models[0] if enabled_models else ""),
        timeout=to_int(raw.get("timeout"), 180, minimum=10, maximum=900),
        enabled=to_bool(raw.get("enabled"), True),
        enabled_models=unique_values(enabled_models),
        model_provider_types={model: provider for model, provider in model_provider_types.items() if model in set(enabled_models)},
        model_download_proxy_ids=download_proxy_ids,
        models_cache=split_values(raw.get("models_cache") or raw.get("modelsCache") or raw.get("available_models")),
        proxy=str(raw.get("proxy") or "").strip(),  # legacy; resolved later via proxy_id
        proxy_id=str(raw.get("proxy_id") or raw.get("proxyId") or "").strip(),
        protocol_lock=to_bool(raw.get("protocol_lock") or raw.get("protocolLock") or raw.get("disable_model_infer"), False),
        extra=copy.deepcopy(raw.get("extra") if isinstance(raw.get("extra"), dict) else {}),
        api_keys=api_keys,
    )


def _build_video_channel(raw: Any) -> ImageChannelConfig:
    """Video channels reuse ImageChannelConfig; keep video protocol types (async/sync/chat)."""
    data = raw if isinstance(raw, dict) else {}
    # Temporarily allow video provider types through image builder by mapping first.
    patched = dict(data)
    raw_type = patched.get("provider_type", patched.get("providerType", patched.get("api_type", "")))
    # bare openai / empty / legacy async on video channel → generic openai_video
    raw_lower = str(raw_type or "").strip().lower().replace("-", "_")
    if raw_lower in {"", "openai", "openai_compatible", "openai_image", "video_async", "async", "async_task"}:
        vtype = "openai_video"
    else:
        vtype = normalize_video_provider_type(raw_type) or "openai_video"
    # Feed image builder a benign type so it does not blank provider_type.
    patched["provider_type"] = "openai"
    channel = _build_image_channel(patched)
    channel.provider_type = vtype
    channel.protocol_lock = False
    # Keep per-model video protocol overrides.
    fixed_map: Dict[str, str] = {}
    for model, ptype in (channel.model_provider_types or {}).items():
        vt = normalize_video_provider_type(ptype)
        if vt:
            fixed_map[str(model)] = vt
    # Also accept raw map before image normalize stripped unknown types.
    raw_map = data.get("model_provider_types") or data.get("modelProviderTypes") or {}
    if isinstance(raw_map, dict):
        for model, ptype in raw_map.items():
            vt = normalize_video_provider_type(ptype)
            if vt:
                fixed_map[str(model)] = vt
    channel.model_provider_types = fixed_map
    if channel.timeout < 60:
        channel.timeout = 300
    return channel


def normalize_video_provider_type(value: Any) -> str:
    """Map free-form labels to video model-family protocols.

    Families (primary): openai_video / sora / veo / seedance / agnes / kling / cogvideo
    Transport (advanced): video_chat / video_sync
    Legacy video_async → openai_video
    """
    text = str(value or "").strip().lower().replace("-", "_")
    if not text:
        return ""
    raw = str(value or "")
    if "对话" in raw:
        return "video_chat"
    if "同步" in raw and "异步" not in raw:
        return "video_sync"
    aliases = {
        # generic OpenAI-compatible video gateway
        "openai_video": "openai_video",
        "openai_compatible": "openai_video",
        "openai_videos": "openai_video",
        "async": "openai_video",
        "async_task": "openai_video",
        "video_async": "openai_video",
        "video": "openai_video",
        "videos": "openai_video",
        "poll": "openai_video",
        "通用": "openai_video",
        # families
        "sora": "sora",
        "sora2": "sora",
        "openai_sora": "sora",
        "veo": "veo",
        "veo2": "veo",
        "veo3": "veo",
        "google_veo": "veo",
        "gemini_veo": "veo",
        "seedance": "seedance",
        "doubao_seedance": "seedance",
        "seedance_1": "seedance",
        "jimeng_video": "seedance",
        "即梦视频": "seedance",
        "agnes": "agnes",
        "agnes_video": "agnes",
        "agnes_ai": "agnes",
        "kling": "kling",
        "可灵": "kling",
        "kuaishou_kling": "kling",
        "cogvideo": "cogvideo",
        "cogvideox": "cogvideo",
        "zhipu_video": "cogvideo",
        "智谱视频": "cogvideo",
        "grok": "grok",
        "grok_video": "grok",
        "grok_imagine": "grok",
        "grok_midgate": "grok",
        "xai": "grok",
        "x_ai": "grok",
        # transport
        "sync": "video_sync",
        "openai_sync": "video_sync",
        "video_sync": "video_sync",
        "chat": "video_chat",
        "openai_chat": "video_chat",
        "chat_completions": "video_chat",
        "video_chat": "video_chat",
    }
    # bare openai is image protocol — only video channel builder maps it
    text = aliases.get(text, text)
    if text in VIDEO_PROVIDER_TYPES:
        return text
    # fuzzy contains
    if "sora" in text:
        return "sora"
    if "veo" in text:
        return "veo"
    if "seedance" in text or "seedance" in raw.lower():
        return "seedance"
    if "agnes" in text:
        return "agnes"
    if "kling" in text or "可灵" in raw:
        return "kling"
    if "cogvideo" in text or "zhipu" in text:
        return "cogvideo"
    if "grok" in text or "xai" in text or "x_ai" in text:
        return "grok"
    return ""


def infer_video_provider_type_from_model(model: str) -> str:
    """Heuristic auto family from model name (like image provider infer)."""
    text = str(model or "").strip().lower()
    compact = re.sub(r"[\s_]+", "-", text)
    if not compact:
        return ""
    if "sora" in compact:
        return "sora"
    if "veo" in compact:
        return "veo"
    if "seedance" in compact or "doubao-seedance" in compact:
        return "seedance"
    if "agnes" in compact:
        return "agnes"
    if "kling" in compact or "可灵" in text:
        return "kling"
    if "cogvideo" in compact or "cog-videox" in compact or "viduq" in compact:
        return "cogvideo"
    if "grok" in compact and "video" in compact:
        return "grok"
    if compact.startswith("grok-imagine-video") or compact.startswith("grok_imagine_video"):
        return "grok"
    # chat-style models that return video links
    if any(k in compact for k in ("gpt-4o", "gpt4o", "claude", "deepseek-chat")) and "video" in compact:
        return "video_chat"
    if compact.endswith("-chat") or compact.startswith("chat-"):
        return "video_chat"
    # generic async video gateways still common
    if any(k in compact for k in ("luma", "runway", "minimax", "hailuo", "vidu", "wanx", "wan2", "gen-3", "gen3")):
        return "openai_video"
    if "sync" in compact and "video" in compact:
        return "video_sync"
    return ""


def resolve_video_model_provider_type(
    model: str,
    channel_provider_type: str,
    model_override: str = "",
) -> str:
    manual = normalize_video_provider_type(model_override)
    if manual:
        return manual
    inferred = infer_video_provider_type_from_model(model)
    if inferred:
        return inferred
    channel = normalize_video_provider_type(channel_provider_type) or "openai_video"
    return channel


def normalize_provider_type(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "openai_image": "openai",
        "openai_images": "openai",
        "openai_chat": "gemini_openai",
        "openai_compatible": "gemini_openai",
        "chat_completions": "gemini_openai",
        "google": "gemini",
        "google_gemini": "gemini",
        "zimage": "z_image_gitee",
        "z_image": "z_image_gitee",
        "gitee": "z_image_gitee",
        "jimeng": "jimeng2api",
        "jimeng2": "jimeng2api",
        "xai": "grok",
        "x_ai": "grok",
        "nai": "novelai",
        "novel_ai": "novelai",
        "novelai_image": "novelai",
        "nai2api": "novelai",
        "nai_image": "novelai",
        "bestnai": "novelai",
        "ppnai": "novelai",
    }
    text = aliases.get(text, text)
    return text if text in PROVIDER_TYPES else ""


def infer_provider_type_from_model(model: str) -> str:
    text = str(model or "").strip().lower()
    compact = re.sub(r"[\s_]+", "-", text)
    if not compact:
        return ""
    if "agnes" in compact:
        return "agnes"
    if "z-image" in compact or compact.startswith("zimage"):
        return "z_image_gitee"
    if "jimeng" in compact or "seedream" in compact or "doubao-seedream" in compact:
        return "jimeng2api"
    if "grok" in compact or "xai" in compact or "x-ai" in compact:
        return "grok"
    if "gpt-image" in compact or "dall-e" in compact or "dalle" in compact:
        return "openai"
    if "gemini" in compact or "nano-banana" in compact:
        return "gemini"
    if "nai-diffusion" in compact or compact.startswith("nai-") or "novelai" in compact:
        return "novelai"
    return ""


# Native adapters that cannot ride the OpenAI images/edits codepath.
STRONG_NATIVE_PROVIDER_TYPES = frozenset(
    {"grok", "novelai", "agnes", "jimeng2api", "z_image_gitee"}
)


def resolve_model_provider_type(
    model: str,
    default_provider_type: str,
    manual_provider_type: str = "",
    *,
    protocol_lock: bool = False,
) -> str:
    """Resolve per-model provider protocol.

    When protocol_lock is True (OpenAI-compatible relays / NewAPI), do not infer
    gemini from the model *name* onto a different chat/native path — keep the
    channel provider_type. Explicit model_provider_types still wins, except a
    no-op override that merely repeats the channel default while the model name
    clearly requires a strong native adapter (grok/novelai/agnes/…).

    Source: target 07 + shoubanhua dual-protocol caution; fixed 2026-08-13 for
    grok-on-openai-channel selfie refs.
    """
    manual = normalize_provider_type(manual_provider_type)
    default = normalize_provider_type(default_provider_type) or "openai"
    inferred = infer_provider_type_from_model(model)

    if manual:
        # "openai" stored for grok/nai/agnes is usually accidental (channel default
        # copied into model_provider_types). Prefer the native adapter.
        if (
            inferred in STRONG_NATIVE_PROVIDER_TYPES
            and manual == default
            and inferred != manual
        ):
            return inferred
        return manual

    if protocol_lock:
        if inferred in STRONG_NATIVE_PROVIDER_TYPES:
            return inferred
        return default

    if inferred:
        return inferred
    return default


def preflight_image_channel(raw: Any, *, kind: str = "image") -> Dict[str, Any]:
    """Local channel config preflight (target 03). No network.

    Returns {ok, errors:[{field, message}], channel_name, auto_disabled}.
    Enabled channel with zero models is auto-disabled instead of hard error.
    """
    errors: List[Dict[str, str]] = []
    channel = _build_image_channel(raw if isinstance(raw, dict) else {})
    label = channel.name or "未命名渠道"
    kind_label = "审核渠道" if kind == "audit" else "生图渠道"
    auto_disabled = False

    if not str(channel.name or "").strip():
        errors.append({"field": "name", "message": f"{kind_label}缺少名称"})
    if channel.provider_type not in PROVIDER_TYPES:
        errors.append({"field": "provider_type", "message": f"{kind_label} {label} 的 provider_type 无效"})
    if not str(channel.base_url or "").strip():
        errors.append({"field": "base_url", "message": f"{kind_label} {label} 缺少 base_url"})
    if not str(channel.api_key or "").strip() and not (getattr(channel, "api_keys", None) or []):
        errors.append({"field": "api_key", "message": f"{kind_label} {label} 缺少 api_key"})
    models = channel.enabled_models or ([channel.model] if channel.model else [])
    if not models and channel.enabled:
        # Auto-disable channels with no models instead of blocking save.
        auto_disabled = True
        channel.enabled = False
        if isinstance(raw, dict):
            raw["enabled"] = False
    if channel.timeout < 10:
        errors.append({"field": "timeout", "message": f"{kind_label} {label} 超时过短"})

    return {
        "ok": not errors,
        "errors": errors,
        "channel_name": label,
        "kind": kind,
        "auto_disabled": auto_disabled,
        "message": "；".join(item["message"] for item in errors),
    }


def preflight_video_channel(raw: Any) -> Dict[str, Any]:
    """Local video channel preflight (VIDEO V1). No network."""
    errors: List[Dict[str, str]] = []
    channel = _build_video_channel(raw if isinstance(raw, dict) else {})
    label = channel.name or "未命名视频渠道"
    auto_disabled = False
    if not str(channel.name or "").strip():
        errors.append({"field": "name", "message": "视频渠道缺少名称"})
    if not str(channel.base_url or "").strip():
        errors.append({"field": "base_url", "message": f"视频渠道 {label} 缺少 base_url"})
    if not str(channel.api_key or "").strip() and not (getattr(channel, "api_keys", None) or []):
        errors.append({"field": "api_key", "message": f"视频渠道 {label} 缺少 api_key"})
    models = channel.enabled_models or ([channel.model] if channel.model else [])
    if not models and channel.enabled:
        auto_disabled = True
        channel.enabled = False
        if isinstance(raw, dict):
            raw["enabled"] = False
    return {
        "ok": not errors,
        "errors": errors,
        "channel_name": label,
        "kind": "video",
        "auto_disabled": auto_disabled,
        "message": "；".join(item["message"] for item in errors),
    }


def sanitize_channels_for_save(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Mutate channel lists in-place: enabled + no models → disable (no hard fail)."""
    data = raw if isinstance(raw, dict) else {}
    for key, kind in (("image_channels", "image"), ("audit_channels", "audit"), ("video_channels", "video")):
        items = template_list_items(data.get(key))
        cleaned: List[Any] = []
        for item in items:
            if not isinstance(item, dict):
                cleaned.append(item)
                continue
            row = copy.deepcopy(item)
            if kind == "video":
                preflight_video_channel(row)
            else:
                preflight_image_channel(row, kind=kind)
            cleaned.append(row)
        if key in data or cleaned:
            data[key] = cleaned
    return data


def preflight_config_channels(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Preflight all image/audit/video channels in a config dict."""
    data = sanitize_channels_for_save(copy.deepcopy(raw if isinstance(raw, dict) else {}))
    image_results = [preflight_image_channel(item, kind="image") for item in template_list_items(data.get("image_channels"))]
    audit_results = [preflight_image_channel(item, kind="audit") for item in template_list_items(data.get("audit_channels"))]
    video_results = [preflight_video_channel(item) for item in template_list_items(data.get("video_channels"))]
    errors: List[Dict[str, str]] = []
    for result in image_results + audit_results + video_results:
        errors.extend(result.get("errors") or [])
    return {
        "ok": not errors,
        "errors": errors,
        "image_channels": image_results,
        "audit_channels": audit_results,
        "video_channels": video_results,
        "message": "；".join(item["message"] for item in errors),
        "config": data,
    }


def deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def normalize_config_tree(value: Any) -> Any:
    """Unwrap common AstrBot config-page value containers.

    Some AstrBot versions pass native config values as plain JSON, while others
    may preserve UI wrappers such as {"value": ...} or template entries with
    {"data": {...}}. The runtime config should always work with plain values.
    """
    if isinstance(value, list):
        return [normalize_config_tree(item) for item in value]
    if isinstance(value, tuple):
        return [normalize_config_tree(item) for item in value]
    if not isinstance(value, dict):
        return value

    if "value" in value and (
        len(value) == 1
        or any(key in value for key in ("type", "description", "hint", "default", "options"))
    ):
        return normalize_config_tree(value.get("value"))

    return {str(key): normalize_config_tree(item) for key, item in value.items()}


def template_list_items(value: Any) -> List[Any]:
    value = normalize_config_tree(value)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        for key in ("value", "items", "data", "list"):
            nested = value.get(key)
            if isinstance(nested, (list, tuple)):
                return list(nested)
        for nested in value.values():
            if isinstance(nested, (list, tuple)):
                return list(nested)
        if any(key in value for key in ("name", "id", "provider_type", "api_type", "base_url", "model")):
            return [value]
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        return [item for item in re.split(r"[\n,]+", text) if item.strip()]
    return [value]


def ensure_dict(data: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        value = {}
        data[key] = value
    return value


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        return [item for item in re.split(r"[\n,]+", text) if item.strip()]
    return [value]


def split_values(value: Any) -> List[str]:
    if isinstance(value, list):
        items: Iterable[Any] = value
    elif isinstance(value, tuple) or isinstance(value, set):
        items = value
    else:
        items = re.split(r"[\s,]+", str(value or "").replace("\r", "\n"))
    return unique_values(str(item).strip() for item in items if str(item).strip())


def unique_values(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disable", "disabled"}:
        return False
    return default


def to_int(value: Any, default: int, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    try:
        result = int(float(str(value).strip()))
    except Exception:
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result
