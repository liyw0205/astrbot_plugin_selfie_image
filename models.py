"""Configuration models and normalization."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .constants import PROVIDER_TYPES


DEFAULT_CONFIG: Dict[str, Any] = {
    "bot_name": "啊呜",
    "personality": "可爱猫娘助手，说话带“喵”等语气词，活泼俏皮会撒娇",
    "web": {
        "enable": True,
        "host": "127.0.0.1",
        "port": 14514,
        "token": "",
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
        "blocked_words": [],
        "enable_prompt_audit": False,
        "enable_output_audit": False,
        "prompt_audit_model": "",
        "output_audit_model": "",
        "ocr_model": "",
        "prompt_audit_template": "你是生图安全审核员。请判断以下提示词是否安全。提示词：{prompt}。仅输出 JSON：{\"allow\":true/false,\"reason\":\"原因\"}",
        "output_audit_template": "你是图像安全审核员。请判断以下图片是否适合普通用户。仅输出 JSON：{\"allow\":true/false,\"reason\":\"原因\"}",
    },
    "permission": {
        "usable_users": "",
        "blocked_users": "",
        "whitelist_users": "",
        "whitelist_groups": "",
    },
    "image_channels": [],
    "audit_channels": [],
    "enabled_image_model_priority": [],
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

    @property
    def label(self) -> str:
        return f"{self.channel_name}/{self.model}"


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
    models_cache: List[str] = field(default_factory=list)
    proxy: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def targets(self, global_timeout: int) -> List[ImageModelTarget]:
        if not self.enabled:
            return []
        models = self.enabled_models or ([self.model] if self.model else [])
        result: List[ImageModelTarget] = []
        for model in models:
            if not model:
                continue
            result.append(
                ImageModelTarget(
                    channel_name=self.name,
                    provider_type=resolve_model_provider_type(model, self.provider_type, self.model_provider_types.get(model, "")),
                    base_url=self.base_url,
                    api_key=self.api_key,
                    model=model,
                    timeout=max(10, int(global_timeout or self.timeout or 180)),
                    proxy=self.proxy,
                    extra=copy.deepcopy(self.extra),
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
    image_blocked_words: List[str]
    image_enable_prompt_audit: bool
    image_enable_output_audit: bool
    image_prompt_audit_model: str
    image_output_audit_model: str
    image_ocr_model: str
    image_prompt_audit_template: str
    image_output_audit_template: str
    usable_users: List[str]
    blocked_users: List[str]
    whitelist_users: List[str]
    whitelist_groups: List[str]
    image_channels: List[ImageChannelConfig]
    audit_channels: List[ImageChannelConfig]
    enabled_image_model_priority: List[str]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AICatConfig":
        raw = normalize_config_tree(deep_merge(DEFAULT_CONFIG, data if isinstance(data, dict) else {}))
        raw = normalize_legacy_keys(raw)

        web = ensure_dict(raw, "web")
        image = ensure_dict(raw, "image")
        permission = ensure_dict(raw, "permission")

        channels = [_build_image_channel(item) for item in template_list_items(raw.get("image_channels"))]
        channels = [channel for channel in channels if channel.name and channel.provider_type in PROVIDER_TYPES]
        audit_channels = [_build_image_channel(item) for item in template_list_items(raw.get("audit_channels"))]
        audit_channels = [channel for channel in audit_channels if channel.name and channel.provider_type in PROVIDER_TYPES]

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
            image_blocked_words=split_values(image.get("blocked_words")),
            image_enable_prompt_audit=to_bool(image.get("enable_prompt_audit"), False),
            image_enable_output_audit=to_bool(image.get("enable_output_audit"), False),
            image_prompt_audit_model=str(image.get("prompt_audit_model") or "").strip(),
            image_output_audit_model=str(image.get("output_audit_model") or "").strip(),
            image_ocr_model=str(image.get("ocr_model") or "").strip(),
            image_prompt_audit_template=str(image.get("prompt_audit_template") or DEFAULT_CONFIG["image"]["prompt_audit_template"]),
            image_output_audit_template=str(image.get("output_audit_template") or DEFAULT_CONFIG["image"]["output_audit_template"]),
            usable_users=split_values(permission.get("usable_users")),
            blocked_users=split_values(permission.get("blocked_users")),
            whitelist_users=split_values(permission.get("whitelist_users")),
            whitelist_groups=split_values(permission.get("whitelist_groups")),
            image_channels=channels,
            audit_channels=audit_channels,
            enabled_image_model_priority=split_values(raw.get("enabled_image_model_priority")),
        )

    def get_prioritized_targets(self) -> List[ImageModelTarget]:
        all_targets: List[ImageModelTarget] = []
        for channel in self.image_channels:
            all_targets.extend(channel.targets(self.image_global_timeout))

        if not self.enabled_image_model_priority:
            return all_targets

        by_key: Dict[str, ImageModelTarget] = {}
        for target in all_targets:
            by_key[target.label] = target
            by_key[f"{target.channel_name}:{target.model}"] = target
            by_key[target.model] = target

        ordered: List[ImageModelTarget] = []
        seen = set()
        for raw_key in self.enabled_image_model_priority:
            key = str(raw_key).strip()
            target = by_key.get(key)
            if target and target.label not in seen:
                ordered.append(target)
                seen.add(target.label)

        for target in all_targets:
            if target.label not in seen:
                ordered.append(target)
                seen.add(target.label)
        return ordered

    def get_audit_targets(self) -> List[ImageModelTarget]:
        targets: List[ImageModelTarget] = []
        for channel in self.audit_channels:
            targets.extend(channel.targets(self.image_global_timeout))
        return targets


def normalize_legacy_keys(raw: Dict[str, Any]) -> Dict[str, Any]:
    raw = copy.deepcopy(raw)

    if "imageChannels" in raw and "image_channels" not in raw:
        raw["image_channels"] = raw["imageChannels"]
    if "auditChannels" in raw and "audit_channels" not in raw:
        raw["audit_channels"] = raw["auditChannels"]
    if "enabledImageModelPriority" in raw and "enabled_image_model_priority" not in raw:
        raw["enabled_image_model_priority"] = raw["enabledImageModelPriority"]
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

    api_key_value = raw.get("api_key") or raw.get("apiKey") or raw.get("api_keys") or raw.get("apiKeys") or ""
    if isinstance(api_key_value, list):
        api_key_value = "\n".join(str(item) for item in api_key_value if str(item).strip())

    model = str(raw.get("model") or "").strip()
    if provider_type == "agnes" and not model:
        model = "agnes-image-2.1-flash"
    if model and not enabled_models:
        enabled_models = [model]

    return ImageChannelConfig(
        name=str(raw.get("name") or raw.get("id") or "default").strip(),
        provider_type=provider_type,
        base_url=str(raw.get("base_url") or raw.get("baseUrl") or "").strip(),
        api_key=str(api_key_value).strip(),
        model=model or (enabled_models[0] if enabled_models else ""),
        timeout=to_int(raw.get("timeout"), 180, minimum=10, maximum=900),
        enabled=to_bool(raw.get("enabled"), True),
        enabled_models=unique_values(enabled_models),
        model_provider_types={model: provider for model, provider in model_provider_types.items() if model in set(enabled_models)},
        models_cache=split_values(raw.get("models_cache") or raw.get("modelsCache") or raw.get("available_models")),
        proxy=str(raw.get("proxy") or "").strip(),
        extra=copy.deepcopy(raw.get("extra") if isinstance(raw.get("extra"), dict) else {}),
    )


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
    return ""


def resolve_model_provider_type(model: str, default_provider_type: str, manual_provider_type: str = "") -> str:
    manual = normalize_provider_type(manual_provider_type)
    if manual:
        return manual
    inferred = infer_provider_type_from_model(model)
    if inferred:
        return inferred
    return normalize_provider_type(default_provider_type) or "openai"


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
