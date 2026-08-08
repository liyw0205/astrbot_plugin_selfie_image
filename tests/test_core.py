from __future__ import annotations

import base64
import copy
import asyncio
import json
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = types.SimpleNamespace(
        ClientError=Exception,
        ClientResponse=object,
        ClientSession=object,
        ClientTimeout=lambda **_: None,
        FormData=lambda: None,
    )

from astrbot_plugin_selfie_image.generator import generate_image_with_fallback
from astrbot_plugin_selfie_image.error_classify import (
    classify_generation_error,
    is_non_retryable_generation_error,
    is_param_profile_switch_error,
)
from astrbot_plugin_selfie_image.models import (
    AICatConfig,
    DEFAULT_CONFIG,
    ImageModelTarget,
    deep_merge,
    normalize_config_tree,
    preflight_image_channel,
    preflight_config_channels,
    resolve_model_provider_type,
)
from astrbot_plugin_selfie_image.providers import (
    AgnesImageAdapter,
    BaseImageAdapter,
    GeminiImageAdapter,
    GeminiOpenAIImageAdapter,
    GrokImageAdapter,
    ImageGenerateResult,
    ImageGenerateRequest,
    ImageReference,
    OpenAIImageAdapter,
    build_model_list_urls,
    clean_image_url,
    extract_model_ids_from_response,
    extract_image_urls_from_text,
    fetch_generated_image_url,
    http_error_preview,
    images_from_response_unknown,
    looks_like_binary_image,
    normalize_gemini_base_url,
    normalize_image_base_url,
    provider_type_from_channel_payload,
    response_preview,
)
from astrbot_plugin_selfie_image.utils import (
    bytes_to_data_url,
    collect_cache_cleanup_candidates,
    collect_record_cache_paths,
    collect_unreferenced_record_cache_paths,
    data_url_to_bytes,
    detect_mime_by_bytes,
    ext_from_mime,
    extract_image_urls,
    extract_group_id_from_text,
    fetch_image_source,
    guess_image_content_type,
    looks_like_image_bytes,
    looks_like_image_url,
    parse_audit_response_text,
    redact_sensitive_data,
    redact_sensitive_text,
    resolve_awaitable,
    safe_delete_relative_files,
)
from astrbot_plugin_selfie_image.web import Flask, FlaskWebServer, INDEX_HTML


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 128


class FakeResponse:
    def __init__(self, data=None, status: int = 200, text: str = "") -> None:
        self.data = {} if data is None else data
        self.status = status
        self._text = text if text else json.dumps(self.data)
        self.charset = "utf-8"
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return self._text.encode("utf-8")

    async def json(self, content_type=None):
        return self.data


class FakeContent:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def iter_chunked(self, size: int):
        if self.data:
            yield self.data


class FakeImageResponse:
    def __init__(self, data: bytes, status: int = 200, headers=None) -> None:
        self.status = status
        self.headers = headers or {"content-type": "image/png", "content-length": str(len(data))}
        self.content = FakeContent(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class FakeSession:
    def __init__(
        self,
        data=None,
        status: int = 200,
        text: str = "",
        get_data: bytes = b"",
        get_status: int = 200,
        get_headers=None,
    ) -> None:
        self.data = {} if data is None else data
        self.status = status
        self.text = text
        self.get_data = get_data
        self.get_status = get_status
        self.get_headers = get_headers
        self.requests = []

    async def post(self, url: str, **kwargs):
        self.requests.append({"method": "POST", "url": url, **kwargs})
        return FakeResponse(self.data, self.status, self.text)

    def get(self, url: str, **kwargs):
        self.requests.append({"method": "GET", "url": url, **kwargs})
        return FakeImageResponse(self.get_data, self.get_status, self.get_headers)


class FakeGenerateAdapter:
    def __init__(self, result: ImageGenerateResult) -> None:
        self.result = result

    async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        return self.result


def make_target(provider_type: str = "agnes", model: str = "agnes-image-2.1-flash") -> ImageModelTarget:
    return ImageModelTarget(
        channel_name="test-channel",
        provider_type=provider_type,
        base_url="https://example.test",
        api_key="test-key",
        model=model,
        timeout=30,
    )


WEB_STARTUP_CONFIG_KEYS = ("web", "webEnable", "webHost", "webPort", "webToken")


def strip_web_startup_config(data):
    cleaned = copy.deepcopy(data if isinstance(data, dict) else {})
    for key in WEB_STARTUP_CONFIG_KEYS:
        cleaned.pop(key, None)
    return cleaned


class FakeWebPlugin:
    def __init__(self, token: str = "secret") -> None:
        self.key_web = copy.deepcopy(DEFAULT_CONFIG["web"])
        self.key_web["token"] = token
        self.raw_config = deep_merge(DEFAULT_CONFIG, {"web": self.key_web, "image": {"cache_limit_mb": 10}})
        self.config = AICatConfig.from_dict(self.raw_config)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.temp_dir.name, "selfie_image_config.json")
        self.records_path = os.path.join(self.temp_dir.name, "selfie_image_records.json")
        self.generated_dir = os.path.join(self.temp_dir.name, "image_cache")
        self.task_status_calls = []
        self.selfie_reference = {"data": b"", "mime_type": "image/png", "updated_at": ""}
        os.makedirs(self.generated_dir, exist_ok=True)

    def _cache_size_bytes(self) -> int:
        return 0

    def get_config_for_web(self):
        return strip_web_startup_config(self.raw_config)

    def update_config_from_web(self, patch):
        patch = strip_web_startup_config(patch)
        self.raw_config = deep_merge(self.raw_config, patch)
        self.raw_config["web"] = copy.deepcopy(self.key_web)
        self.config = AICatConfig.from_dict(self.raw_config)
        return self.get_config_for_web()

    def get_recent_records(self):
        return [{"id": 1, "success": True}]

    def get_record_for_web(self, record_id: str):
        if str(record_id) == "1":
            return {
                "id": 1,
                "success": True,
                "request_data": {"prompt": "test"},
                "response_data": {"model": "model-a"},
            }
        raise ValueError("记录不存在或已清理")

    def clear_recent_records(self):
        return 1

    def get_selfie_reference_payload(self):
        data = self.selfie_reference.get("data") or b""
        if not data:
            return {
                "has_image": False,
                "ref_mime_type": self.selfie_reference.get("mime_type") or "image/png",
                "updated_at": self.selfie_reference.get("updated_at") or "",
                "status": "当前还没有设置自拍参考图",
            }
        return {
            "has_image": True,
            "ref_mime_type": self.selfie_reference.get("mime_type") or detect_mime_by_bytes(data),
            "updated_at": self.selfie_reference.get("updated_at") or "",
            "image": bytes_to_data_url(data, self.selfie_reference.get("mime_type") or detect_mime_by_bytes(data)),
            "status": "当前已设置自拍参考图",
        }

    def save_selfie_reference_from_web(self, payload):
        raw_image = str(payload.get("image") or payload.get("data") or "").strip()
        if not raw_image:
            raise ValueError("缺少 image 字段，支持 data:image/...;base64,... 或纯 base64")
        data, mime = data_url_to_bytes(raw_image)
        if not data:
            raise ValueError("上传图片为空")
        self.selfie_reference = {"data": data, "mime_type": mime, "updated_at": "2026-07-06 00:00:00"}
        return self.get_selfie_reference_payload()

    def clear_selfie_reference_from_web(self):
        self.selfie_reference = {"data": b"", "mime_type": "image/png", "updated_at": "2026-07-06 00:00:00"}
        return {"has_image": False, "status": "cleared"}

    async def refresh_selfie_profile_from_web(self):
        return {"status": "refreshed", "updated_at": "2026-07-04 00:00:00"}

    async def web_test_image(self, payload):
        return {"success": True, "payload": copy.deepcopy(payload)}

    def start_web_image_task(self, payload):
        return {"task_id": "web-12345678-1", "status": "queued", "request_data": copy.deepcopy(payload)}

    async def web_refresh_image_models(self, payload):
        return ["model-a", "model-b"]

    def get_web_image_task(self, task_id: str):
        self.task_status_calls.append(task_id)
        if task_id == "web-12345678-1":
            return {"task_id": task_id, "status": "succeeded", "success": True}
        raise ValueError("任务不存在或已清理")

    def get_cached_image_info(self, rel_path: str):
        base = os.path.abspath(self.generated_dir)
        raw_path = str(rel_path or "").strip()
        if not raw_path:
            raise ValueError("图片路径不能为空")
        path = os.path.abspath(os.path.join(base, raw_path))
        if path == base or not path.startswith(base + os.sep):
            raise ValueError("非法图片路径")
        return {
            "path": rel_path,
            "absolute_path": path,
            "exists": os.path.isfile(path),
            "is_image": looks_like_image_bytes(Path(path).read_bytes()[:512]) if os.path.isfile(path) else False,
            "mime_type": "image/png",
        }

    def close(self) -> None:
        self.temp_dir.cleanup()


class ConfigModelTests(unittest.TestCase):
    def test_runtime_defaults_match_public_schema(self) -> None:
        config = AICatConfig.from_dict({})
        self.assertEqual(config.web_host, "127.0.0.1")
        self.assertEqual(config.image_max_batch_count, 2)

    def test_numeric_config_is_clamped(self) -> None:
        config = AICatConfig.from_dict({"image": {"max_batch_count": 99, "max_concurrent_tasks": 0}})
        self.assertEqual(config.image_max_batch_count, 8)
        self.assertEqual(config.image_max_concurrent_tasks, 1)

    def test_astrbot_wrapped_values_are_unwrapped(self) -> None:
        raw = {"image": {"value": {"max_batch_count": {"value": 4}}, "type": "object"}}
        self.assertEqual(normalize_config_tree(raw), {"image": {"max_batch_count": 4}})

    def test_provider_type_can_be_inferred_from_model(self) -> None:
        self.assertEqual(resolve_model_provider_type("agnes-image-2.1-flash", "openai"), "agnes")
        self.assertEqual(resolve_model_provider_type("grok-imagine-image", "openai"), "grok")
        self.assertEqual(resolve_model_provider_type("unknown-model", "gemini_openai"), "gemini_openai")
        # protocol_lock keeps channel protocol even when model name looks like gemini
        self.assertEqual(
            resolve_model_provider_type("gemini-2.5-flash-image", "openai", protocol_lock=True),
            "openai",
        )
        self.assertEqual(
            resolve_model_provider_type("gemini-2.5-flash-image", "openai", "gemini", protocol_lock=True),
            "gemini",
        )

    def test_openai_channel_protocol_lock_defaults(self) -> None:
        config = AICatConfig.from_dict(
            {
                "image_channels": [
                    {
                        "name": "relay",
                        "provider_type": "openai",
                        "base_url": "https://example.test",
                        "api_key": "sk-test",
                        "enabled_models": ["gemini-2.5-flash-image", "gpt-image-2"],
                    }
                ]
            }
        )
        targets = {t.model: t.provider_type for t in config.get_prioritized_targets()}
        self.assertEqual(targets["gemini-2.5-flash-image"], "openai")
        self.assertEqual(targets["gpt-image-2"], "openai")

    def test_channel_preflight_requires_key_url_and_model(self) -> None:
        bad = preflight_image_channel({"name": "x", "provider_type": "openai"}, kind="image")
        self.assertFalse(bad["ok"])
        fields = {item["field"] for item in bad["errors"]}
        self.assertIn("base_url", fields)
        self.assertIn("api_key", fields)
        # empty models no longer hard-fail: enabled channel is auto-disabled
        self.assertNotIn("enabled_models", fields)
        self.assertTrue(bad.get("auto_disabled"))
        empty_models = {
            "name": "empty",
            "provider_type": "openai",
            "base_url": "https://example.test",
            "api_key": "sk-test",
            "enabled": True,
            "enabled_models": [],
        }
        soft = preflight_image_channel(empty_models, kind="image")
        self.assertTrue(soft["ok"])
        self.assertTrue(soft.get("auto_disabled"))
        self.assertFalse(empty_models.get("enabled"))
        good = preflight_image_channel(
            {
                "name": "ok",
                "provider_type": "openai",
                "base_url": "https://example.test",
                "api_key": "sk-test",
                "model": "gpt-image-2",
            }
        )
        self.assertTrue(good["ok"])
        report = preflight_config_channels({"image_channels": []})
        # empty list is allowed (Web may save while configuring)
        self.assertTrue(report["ok"])

    def test_split_api_keys_and_target_rotation_list(self) -> None:
        from astrbot_plugin_selfie_image.models import split_api_keys

        self.assertEqual(split_api_keys("a\nb\nc"), ["a", "b", "c"])
        self.assertEqual(split_api_keys("a,b;c\na"), ["a", "b", "c"])
        self.assertEqual(split_api_keys(["k1", "k2", "k1"]), ["k1", "k2"])
        config = AICatConfig.from_dict(
            {
                "image_channels": [
                    {
                        "name": "relay",
                        "provider_type": "openai",
                        "base_url": "https://example.test",
                        "api_keys": ["sk-one", "sk-two"],
                        "enabled_models": ["gpt-image-2"],
                    }
                ]
            }
        )
        target = config.get_prioritized_targets()[0]
        self.assertEqual(target.api_key, "sk-one")
        self.assertEqual(target.resolved_api_keys(), ["sk-one", "sk-two"])
        # multiline api_key field also works
        config2 = AICatConfig.from_dict(
            {
                "image_channels": [
                    {
                        "name": "relay2",
                        "provider_type": "openai",
                        "base_url": "https://example.test",
                        "api_key": "sk-a\nsk-b",
                        "model": "gpt-image-2",
                    }
                ]
            }
        )
        t2 = config2.get_prioritized_targets()[0]
        self.assertEqual(t2.resolved_api_keys(), ["sk-a", "sk-b"])

    def test_error_classify_non_retryable(self) -> None:
        self.assertFalse(classify_generation_error("HTTP 401: invalid token")["retryable"])
        self.assertEqual(classify_generation_error("HTTP 401: invalid token")["category"], "auth")
        self.assertFalse(classify_generation_error("No available channel for model gpt-image-1")["retryable"])
        self.assertFalse(classify_generation_error("The generated images appear to be unsafe")["retryable"])
        self.assertTrue(classify_generation_error("HTTP 503 upstream")["retryable"])
        self.assertTrue(classify_generation_error("HTTP 429 rate limit")["retryable"])
        self.assertFalse(classify_generation_error("请求超时")["retryable"])
        self.assertTrue(is_non_retryable_generation_error("HTTP 404 model_not_found"))
        self.assertTrue(is_param_profile_switch_error("HTTP 400: unsupported size"))

    def test_enabled_model_priority_and_manual_provider_types_are_preserved(self) -> None:
        config = AICatConfig.from_dict(
            {
                "image_channels": [
                    {
                        "name": "primary",
                        "provider_type": "openai",
                        "base_url": "https://example.test",
                        "model": "gpt-image-1",
                        "enabled_models": [
                            {"model": "custom-image-model", "provider_type": "grok"},
                            "gpt-image-1",
                        ],
                    },
                    {
                        "name": "secondary",
                        "provider_type": "gemini_openai",
                        "base_url": "https://example.test",
                        "model": "gemini-2.5-flash-image",
                        "enabled_models": ["gemini-2.5-flash-image"],
                    },
                    {
                        "name": "disabled",
                        "provider_type": "openai",
                        "model": "dall-e-3",
                        "enabled": False,
                    },
                ],
                "enabled_image_model_priority": [
                    "secondary/gemini-2.5-flash-image",
                    "custom-image-model",
                ],
            }
        )

        targets = config.get_prioritized_targets()
        self.assertEqual([target.label for target in targets], [
            "secondary/gemini-2.5-flash-image",
            "primary/custom-image-model",
            "primary/gpt-image-1",
        ])
        self.assertEqual(targets[1].provider_type, "grok")
        self.assertNotIn("disabled/dall-e-3", [target.label for target in targets])

    def test_model_priority_skips_disabled_channels_and_disabled_model_items(self) -> None:
        config = AICatConfig.from_dict(
            {
                "image_channels": [
                    {
                        "name": "main",
                        "provider_type": "openai",
                        "base_url": "https://example.test",
                        "model": "fallback-model",
                        "enabled_models": [
                            {"model": "enabled-model", "enabled": True},
                            {"model": "disabled-model", "enabled": False},
                        ],
                    },
                    {
                        "name": "off",
                        "provider_type": "openai",
                        "model": "off-model",
                        "enabled": False,
                    },
                ],
                "enabled_image_model_priority": ["main/disabled-model", "off/off-model", "main/enabled-model"],
            }
        )

        self.assertEqual([target.label for target in config.get_prioritized_targets()], ["main/enabled-model"])


class ImageUtilityTests(unittest.TestCase):
    def test_data_url_to_bytes_detects_png(self) -> None:
        data_url = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
        data, mime = data_url_to_bytes(data_url)
        self.assertEqual(data, PNG_BYTES)
        self.assertEqual(mime, "image/png")
        self.assertTrue(looks_like_binary_image(data))

    def test_image_base64_inputs_are_case_insensitive(self) -> None:
        payload = base64.b64encode(PNG_BYTES).decode("ascii")
        self.assertEqual(data_url_to_bytes("DATA:image/png;BASE64," + payload), (PNG_BYTES, "image/png"))
        self.assertEqual(data_url_to_bytes("BASE64://" + payload), (PNG_BYTES, "image/png"))
        self.assertTrue(looks_like_image_url("DATA:image/png;BASE64," + payload))
        self.assertTrue(looks_like_image_url("BASE64://" + payload))
        self.assertEqual(
            extract_image_urls("refs DATA:image/png;BASE64," + payload + " and BASE64://" + payload),
            ["DATA:image/png;BASE64," + payload, "BASE64://" + payload],
        )

    def test_data_url_to_bytes_prefers_detected_mime_over_declared_mime(self) -> None:
        data_url = "data:image/jpeg;base64," + base64.b64encode(PNG_BYTES).decode("ascii")

        data, mime = data_url_to_bytes(data_url)

        self.assertEqual(data, PNG_BYTES)
        self.assertEqual(mime, "image/png")

    def test_data_url_to_bytes_accepts_extra_data_url_parameters(self) -> None:
        payload = base64.b64encode(PNG_BYTES).decode("ascii")
        data_url = f"data:image/png;name=ref.png;charset=utf-8;base64,{payload}"

        data, mime = data_url_to_bytes(data_url)

        self.assertEqual((data, mime), (PNG_BYTES, "image/png"))
        self.assertEqual(extract_image_urls(f"ref {data_url}"), [data_url])

    def test_data_url_to_bytes_accepts_urlsafe_base64_without_padding(self) -> None:
        image = PNG_BYTES + b"\xfb\xff\xff"
        payload = base64.urlsafe_b64encode(image).decode("ascii").rstrip("=")

        self.assertEqual(data_url_to_bytes("data:image/png;base64," + payload), (image, "image/png"))
        self.assertEqual(data_url_to_bytes("base64://" + payload), (image, "image/png"))
        self.assertEqual(data_url_to_bytes(payload), (image, "image/png"))

    def test_data_url_to_bytes_rejects_malformed_base64_without_raising(self) -> None:
        self.assertEqual(data_url_to_bytes("data:image/png;base64,abc"), (b"", "image/png"))
        self.assertEqual(data_url_to_bytes("base64://abc"), (b"", "image/png"))
        self.assertEqual(data_url_to_bytes("abc"), (b"", "image/png"))

    def test_data_url_to_bytes_rejects_valid_base64_non_image_payloads(self) -> None:
        payload = base64.b64encode(b'{"error":"not image"}').decode("ascii")

        self.assertEqual(data_url_to_bytes("data:image/png;base64," + payload), (b"", "image/png"))
        self.assertEqual(data_url_to_bytes("base64://" + payload), (b"", "image/png"))
        self.assertEqual(data_url_to_bytes(payload), (b"", "image/png"))

    def test_image_signature_accepts_avif_container(self) -> None:
        self.assertTrue(looks_like_binary_image(b"\x00\x00\x00 ftypavif\x00\x00\x00\x00"))
        self.assertTrue(looks_like_binary_image(b"\x00\x00\x00 ftypheif\x00\x00\x00\x00"))
        self.assertTrue(looks_like_binary_image(b"II*\x00\x08\x00\x00\x00"))
        self.assertTrue(looks_like_binary_image(b"<?xml version='1.0'?><svg></svg>"))
        self.assertFalse(looks_like_binary_image(b"RIFF1234WAVEfmt "))
        self.assertFalse(looks_like_binary_image(b'{"error":"not an image"}'))

    def test_mime_detection_preserves_modern_image_formats(self) -> None:
        self.assertEqual(detect_mime_by_bytes(b"\x00\x00\x00 ftypavif\x00\x00\x00\x00"), "image/avif")
        self.assertEqual(detect_mime_by_bytes(b"\x00\x00\x00 ftypheic\x00\x00\x00\x00"), "image/heic")
        self.assertEqual(detect_mime_by_bytes(b"MM\x00*\x00\x00\x00\x08"), "image/tiff")
        self.assertEqual(detect_mime_by_bytes(b"<?xml version='1.0'?><svg></svg>"), "image/svg+xml")
        self.assertEqual(detect_mime_by_bytes(b"RIFF1234WAVEfmt "), "image/png")
        self.assertFalse(looks_like_image_bytes(b"RIFF1234WAVEfmt "))
        self.assertEqual(ext_from_mime("image/svg+xml"), "svg")
        self.assertEqual(ext_from_mime("image/tiff"), "tiff")
        self.assertEqual(ext_from_mime("image/avif"), "avif")
        self.assertEqual(guess_image_content_type("https://example.test/a.tiff"), "image/tiff")
        self.assertEqual(guess_image_content_type("https://example.test/a.png?token=1#view"), "image/png")
        self.assertEqual(guess_image_content_type("https://example.test/a.jfif?download=1"), "image/jpeg")
        self.assertEqual(guess_image_content_type("https://example.test/a.heif"), "image/heif")
        self.assertEqual(guess_image_content_type("https://example.test/a.svg#icon"), "image/svg+xml")

    def test_image_url_detection_uses_actual_path_suffix(self) -> None:
        self.assertTrue(looks_like_image_url("https://example.test/ref.avif?token=1#preview"))
        self.assertTrue(looks_like_image_url("https://example.test/icons/ref.svg#icon"))
        self.assertTrue(looks_like_image_url("https://example.test/download?file=ref"))
        self.assertFalse(looks_like_image_url("https://example.test/view?file=ref.png"))
        self.assertFalse(looks_like_image_url("https://example.test/archive.png/metadata"))

        urls = extract_image_urls(
            "ok https://example.test/a.heic?x=1 "
            "bad https://example.test/view?file=b.png "
            "also-bad https://example.test/archive.png/metadata"
        )
        self.assertEqual(urls, ["https://example.test/a.heic?x=1"])

    def test_web_upload_accept_list_matches_supported_image_formats(self) -> None:
        for mime in ("image/avif", "image/heic", "image/heif", "image/tiff", "image/svg+xml"):
            self.assertIn(mime, INDEX_HTML)

    def test_web_monitor_uses_backend_record_pagination(self) -> None:
        self.assertIn("function monitorQueryPath", INDEX_HTML)
        self.assertIn("params.set('limit', String(MONITOR_PAGE_SIZE))", INDEX_HTML)
        self.assertIn("api(monitorQueryPath(MONITOR_PAGE))", INDEX_HTML)
        self.assertIn("RECORD_META.filtered", INDEX_HTML)

    def test_web_prunes_invalid_model_priority_before_save(self) -> None:
        self.assertIn("function prunePriorityList", INDEX_HTML)
        self.assertIn("prunePriorityList();\n      CONFIG.enabled_image_model_priority = textList('priorityList');", INDEX_HTML)
        self.assertIn("keys.push(`${ch.name}/${model}`, `${ch.name}:${model}`, model);", INDEX_HTML)

    def test_base_url_normalization(self) -> None:
        self.assertEqual(normalize_image_base_url("https://example.com/v1/images/generations"), "https://example.com")
        self.assertEqual(normalize_image_base_url("https://example.com/v1/chat/completions"), "https://example.com")
        self.assertEqual(normalize_gemini_base_url("https://example.com/v1beta/models/gemini:generateContent"), "https://example.com")

    def test_model_list_urls_are_provider_specific(self) -> None:
        self.assertEqual(
            build_model_list_urls("https://api.openai.com/v1/images/generations", "openai"),
            [
                "https://api.openai.com/v1/models",
                "https://api.openai.com/models",
                "https://api.openai.com/v1beta/models",
            ],
        )
        self.assertEqual(
            build_model_list_urls("https://generativelanguage.googleapis.com/v1beta/models/gemini:generateContent", "google"),
            [
                "https://generativelanguage.googleapis.com/v1beta/models",
                "https://generativelanguage.googleapis.com/v1/models",
                "https://generativelanguage.googleapis.com/models",
            ],
        )

    def test_channel_payload_provider_type_accepts_legacy_keys_and_aliases(self) -> None:
        self.assertEqual(provider_type_from_channel_payload({"providerType": "google"}), "gemini")
        self.assertEqual(provider_type_from_channel_payload({"api_type": "xai"}), "grok")
        self.assertEqual(provider_type_from_channel_payload({"apiType": "openai_compatible"}), "gemini_openai")
        self.assertEqual(provider_type_from_channel_payload({}), "openai")

    def test_model_id_extraction_accepts_provider_field_variants(self) -> None:
        payload = {
            "object": "list",
            "data": [
                {"id": "gpt-image-1", "owned_by": "system"},
                {"model": "seedream-4.0"},
                {"model_id": "grok-imagine-image"},
                {"modelName": "agnes-image-2.1-flash"},
                {"slug": "slug-image-model"},
            ],
            "models": [{"name": "models/gemini-2.5-flash-image"}],
            "modelIds": ["modelids-image-model"],
            "metadata": {"owner": "not-a-model-id"},
        }

        self.assertEqual(
            extract_model_ids_from_response(payload),
            [
                "agnes-image-2.1-flash",
                "gpt-image-1",
                "grok-imagine-image",
                "modelids-image-model",
                "models/gemini-2.5-flash-image",
                "seedream-4.0",
                "slug-image-model",
            ],
        )

    def test_http_error_preview_extracts_common_error_shapes(self) -> None:
        self.assertEqual(http_error_preview('{"error":"invalid api key"}'), "invalid api key")
        self.assertEqual(http_error_preview('{"detail":"quota exceeded"}'), "quota exceeded")
        self.assertEqual(http_error_preview('{"error_description":"bad bearer token"}'), "bad bearer token")
        self.assertEqual(http_error_preview('{"msg":"rate limited"}'), "rate limited")
        self.assertEqual(http_error_preview('{"detail":{"message":"nested quota exceeded"}}'), "nested quota exceeded")
        self.assertEqual(http_error_preview('{"errors":[{"message":"first error"},{"message":"second error"}]}'), "first error")

    def test_error_preview_redacts_common_secret_shapes(self) -> None:
        raw = '{"error":{"message":"Authorization: Bearer sk-live-secret-token and api_key=AIzaSySecretTokenValue"}}'
        preview = http_error_preview(raw)

        self.assertIn("Bearer [REDACTED]", preview)
        self.assertIn("api_key=[REDACTED]", preview)
        self.assertNotIn("sk-live-secret-token", preview)
        self.assertNotIn("AIzaSySecretTokenValue", preview)
        self.assertEqual(redact_sensitive_text('"token":"abcdefghijklmnop"'), '"token":"[REDACTED]"')
        self.assertEqual(redact_sensitive_text('"accessToken":"abcdefghijklmnop"'), '"accessToken":"[REDACTED]"')
        self.assertEqual(redact_sensitive_text('"clientSecret":"secret-value-12345"'), '"clientSecret":"[REDACTED]"')
        self.assertEqual(redact_sensitive_text("access_token=abcdefghijklmnop"), "access_token=[REDACTED]")
        self.assertEqual(redact_sensitive_text("x-api-key: provider-secret-value"), "x-api-key: [REDACTED]")

    def test_sensitive_text_redacts_proxy_and_url_credentials(self) -> None:
        text = (
            "proxy=http://user:password@example.test:7890 failed; "
            "download https://name:secret-pass@images.example.test/out.png; "
            '"password":"supersecretvalue"'
        )
        redacted = redact_sensitive_text(text)

        self.assertIn("proxy=[REDACTED]", redacted)
        self.assertIn("https://[REDACTED]@images.example.test/out.png", redacted)
        self.assertIn('"password":"[REDACTED]"', redacted)
        self.assertNotIn("user:password", redacted)
        self.assertNotIn("name:secret-pass", redacted)
        self.assertNotIn("supersecretvalue", redacted)

    def test_response_preview_redacts_raw_and_json_secret_fields(self) -> None:
        raw_preview = response_preview("not json api_key=AIzaSySecretTokenValue")
        json_preview = response_preview(
            {
                "debug": {
                    "access_token": "abcdefghijklmnop",
                    "accessToken": "camel-access-token",
                    "client_secret": "secret-value-12345",
                    "clientSecret": "camel-client-secret",
                    "x-goog-api-key": "plain-provider-secret",
                    "message": "failed",
                }
            }
        )

        self.assertIn("api_key=[REDACTED]", raw_preview)
        self.assertNotIn("AIzaSySecretTokenValue", raw_preview)
        self.assertIn('"access_token": "[REDACTED]"', json_preview)
        self.assertIn('"accessToken": "[REDACTED]"', json_preview)
        self.assertIn('"client_secret": "[REDACTED]"', json_preview)
        self.assertIn('"clientSecret": "[REDACTED]"', json_preview)
        self.assertIn('"x-goog-api-key": "[REDACTED]"', json_preview)
        self.assertNotIn("abcdefghijklmnop", json_preview)
        self.assertNotIn("camel-access-token", json_preview)
        self.assertNotIn("camel-client-secret", json_preview)
        self.assertNotIn("plain-provider-secret", json_preview)

    def test_sensitive_data_redaction_handles_nested_monitor_payloads(self) -> None:
        payload = {
            "channel": {"api_key": "sk-live-secret-token", "proxy": "http://user:password@example.test"},
            "headers": {
                "Authorization": "Bearer abcdefghijklmnop",
                "X-Goog-Api-Key": "plain-provider-secret",
                "x-api-key": "another-provider-secret",
                "Cookie": "session=abcdef1234567890",
            },
            "error": "request failed with token=abcdefghijklmnop",
            "safe": {"model": "gpt-image-1"},
        }

        redacted = redact_sensitive_data(payload)

        self.assertEqual(redacted["channel"]["api_key"], "[REDACTED]")
        self.assertEqual(redacted["channel"]["proxy"], "[REDACTED]")
        self.assertEqual(redacted["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(redacted["headers"]["X-Goog-Api-Key"], "[REDACTED]")
        self.assertEqual(redacted["headers"]["x-api-key"], "[REDACTED]")
        self.assertEqual(redacted["headers"]["Cookie"], "[REDACTED]")
        self.assertEqual(redacted["error"], "request failed with token=[REDACTED]")
        self.assertEqual(redacted["safe"], {"model": "gpt-image-1"})

    def test_group_id_extraction(self) -> None:
        self.assertEqual(extract_group_id_from_text("aiocqhttp:group:123456"), "123456")
        self.assertEqual(extract_group_id_from_text("group_id=98765"), "98765")
        self.assertEqual(extract_group_id_from_text("private:123"), "")

    def test_record_cache_path_collection_and_safe_delete(self) -> None:
        records = [
            {
                "request_image_paths": ["request_a.png", "request_a.png"],
                "response_data": {"generated_image_paths": ["nested/generated_b.png"]},
                "image_paths": "legacy_c.png",
            },
            {"generated_image_paths": ["../outside.png", ""]},
        ]

        paths = collect_record_cache_paths(records)
        self.assertEqual(paths, ["request_a.png", "legacy_c.png", "nested/generated_b.png", "../outside.png"])

        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "image_cache"
            base.mkdir()
            (base / "nested").mkdir()
            outside = Path(temp_dir) / "outside.png"
            absolute_inside = base / "absolute_inside.png"
            files = [
                base / "request_a.png",
                base / "nested" / "generated_b.png",
                base / "legacy_c.png",
                absolute_inside,
                outside,
            ]
            for path in files:
                path.write_bytes(PNG_BYTES)

            deleted = safe_delete_relative_files(str(base), [*paths, str(absolute_inside)])

            self.assertEqual(deleted, ["request_a.png", "legacy_c.png", "nested/generated_b.png"])
            self.assertFalse((base / "request_a.png").exists())
            self.assertFalse((base / "nested" / "generated_b.png").exists())
            self.assertFalse((base / "legacy_c.png").exists())
            self.assertTrue(absolute_inside.exists())
            self.assertTrue(outside.exists())

            self.assertEqual(safe_delete_relative_files("", ["absolute_inside.png"]), [])
            self.assertTrue(absolute_inside.exists())

    def test_unreferenced_record_cache_paths_keep_shared_files(self) -> None:
        removed = [
            {"request_image_paths": ["old_request.png", "shared.png"]},
            {"response_data": {"generated_image_paths": ["old_generated.png"]}},
        ]
        retained = [
            {"generated_image_paths": ["shared.png"]},
            {"image_paths": ["still_visible.png"]},
        ]

        self.assertEqual(
            collect_unreferenced_record_cache_paths(removed, retained),
            ["old_request.png", "old_generated.png"],
        )

    def test_cache_cleanup_candidates_prefer_unreferenced_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "image_cache"
            base.mkdir()
            paths = {
                "protected.png": base / "protected.png",
                "referenced_old.png": base / "referenced_old.png",
                "unreferenced_new.png": base / "unreferenced_new.png",
                "unreferenced_old.png": base / "unreferenced_old.png",
            }
            for path in paths.values():
                path.write_bytes(PNG_BYTES)
            os.utime(paths["referenced_old.png"], (10, 10))
            os.utime(paths["unreferenced_old.png"], (20, 20))
            os.utime(paths["unreferenced_new.png"], (30, 30))
            os.utime(paths["protected.png"], (40, 40))

            candidates = collect_cache_cleanup_candidates(
                str(base),
                protected_paths=["protected.png", str(base / "../outside.png")],
                referenced_paths=["referenced_old.png"],
            )

            self.assertEqual(
                [os.path.relpath(path, base) for path in candidates],
                ["unreferenced_old.png", "unreferenced_new.png", "referenced_old.png"],
            )

    def test_audit_response_parser_handles_json_and_text_variants(self) -> None:
        self.assertEqual(parse_audit_response_text('```json\n{"allow": "yes", "reason": "ok"}\n```'), (True, "ok"))
        self.assertEqual(parse_audit_response_text('{"safe": true, "risk": false, "reason": "clean"}'), (True, "clean"))
        self.assertEqual(parse_audit_response_text('{"unsafe": true, "reason": "blocked"}'), (False, "blocked"))
        self.assertEqual(parse_audit_response_text('{"safe": true, "unsafe": true, "reason": "conflict"}'), (False, "conflict"))
        self.assertEqual(parse_audit_response_text('{"allow": false, "risk": false, "reason": "deny wins"}'), (False, "deny wins"))
        self.assertEqual(parse_audit_response_text("safe: true"), (True, "safe: true"))
        self.assertEqual(parse_audit_response_text("risk: false"), (True, "risk: false"))
        self.assertFalse(parse_audit_response_text("不安全，拒绝")[0])


class AsyncUtilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_awaitable_handles_plain_nested_and_future_values(self) -> None:
        async def inner():
            return "nested"

        async def outer():
            return inner()

        future = asyncio.get_running_loop().create_future()
        future.set_result("future")

        self.assertEqual(await resolve_awaitable("plain"), "plain")
        self.assertEqual(await resolve_awaitable(outer()), "nested")
        self.assertEqual(await resolve_awaitable(future), "future")

    async def test_fetch_image_source_rejects_non_image_http_response(self) -> None:
        session = FakeSession(get_data=b'{"error":"not image"}', get_headers={"content-type": "application/json"})

        result = await fetch_image_source("https://example.test/ref.png", session, max_bytes=1024 * 1024)

        self.assertIsNone(result)

    async def test_fetch_image_source_accepts_uppercase_inline_image_prefixes(self) -> None:
        payload = base64.b64encode(PNG_BYTES).decode("ascii")

        self.assertEqual(
            await fetch_image_source("DATA:image/png;BASE64," + payload, FakeSession(), max_bytes=1024 * 1024),
            (PNG_BYTES, "image/png"),
        )
        self.assertEqual(
            await fetch_image_source("BASE64://" + payload, FakeSession(), max_bytes=1024 * 1024),
            (PNG_BYTES, "image/png"),
        )

    async def test_fetch_image_source_rejects_fake_image_http_response(self) -> None:
        session = FakeSession(get_data=b'{"error":"not image"}', get_headers={"content-type": "image/png"})

        result = await fetch_image_source("https://example.test/ref.png", session, max_bytes=1024 * 1024)

        self.assertIsNone(result)

    async def test_fetch_image_source_accepts_binary_image_with_invalid_length(self) -> None:
        session = FakeSession(
            get_data=PNG_BYTES,
            get_headers={"content-type": "application/x-binary", "content-length": "unknown"},
        )

        result = await fetch_image_source("https://example.test/ref.bin", session, max_bytes=1024 * 1024)

        self.assertEqual(result, (PNG_BYTES, "image/png"))

    async def test_fetch_image_source_prefers_detected_mime_over_header_mime(self) -> None:
        session = FakeSession(get_data=PNG_BYTES, get_headers={"content-type": "image/jpeg"})

        result = await fetch_image_source("https://example.test/ref.jpg", session, max_bytes=1024 * 1024)

        self.assertEqual(result, (PNG_BYTES, "image/png"))

    async def test_fetch_image_source_validates_local_file_signature(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            text_path = Path(temp_dir) / "not_image.png"
            image_path = Path(temp_dir) / "ref.png"
            text_path.write_text('{"error":"not image"}', encoding="utf-8")
            image_path.write_bytes(PNG_BYTES)

            self.assertIsNone(await fetch_image_source(str(text_path), FakeSession(), max_bytes=1024 * 1024))
            self.assertEqual(
                await fetch_image_source(str(image_path), FakeSession(), max_bytes=1024 * 1024),
                (PNG_BYTES, "image/png"),
            )


class ProviderAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_openai_payload_builder_keeps_gpt_image_response_format_out(self) -> None:
        adapter = OpenAIImageAdapter(make_target("openai", "gpt-image-1"), FakeSession())

        payload = adapter.build_image_payload(ImageGenerateRequest(prompt="cat", aspect_ratio="16:9"))

        self.assertEqual(payload["model"], "gpt-image-1")
        self.assertEqual(payload["prompt"], "cat")
        self.assertEqual(payload["size"], "1536x1024")
        self.assertNotIn("response_format", payload)

    def test_gemini_openai_payload_builder_embeds_reference_images(self) -> None:
        adapter = GeminiOpenAIImageAdapter(make_target("gemini_openai", "gemini-2.0-flash"), FakeSession())

        payload = adapter.build_payload(
            ImageGenerateRequest(
                prompt="cat",
                images=[ImageReference(data=PNG_BYTES, mime_type="image/png")],
            )
        )

        content = payload["messages"][0]["content"]
        self.assertEqual(payload["modalities"], ["image", "text"])
        self.assertEqual(content[0], {"type": "text", "text": "Generate an image: cat"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    async def test_adapter_json_post_helper_sends_bearer_auth_by_default(self) -> None:
        session = FakeSession({"ok": True})
        adapter = BaseImageAdapter(make_target(), session)

        data, error = await adapter.post_json_data_or_error("https://example.test/v1/images/generations", {"prompt": "cat"})

        self.assertEqual(data, {"ok": True})
        self.assertEqual(error, "")
        request = session.requests[0]
        self.assertEqual(request["json"], {"prompt": "cat"})
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(request["headers"]["Accept"], "application/json")

    async def test_adapter_json_response_helper_extracts_http_error_and_redacts(self) -> None:
        adapter = BaseImageAdapter(make_target(), FakeSession())
        response = FakeResponse(
            status=400,
            text='{"error":{"message":"Authorization: Bearer sk-live-secret-token and token=abcdefghijklmnop"}}',
        )

        data, error = await adapter.response_json_or_error(response)

        self.assertIsNone(data)
        self.assertIn("HTTP 400", error)
        self.assertIn("Bearer [REDACTED]", error)
        self.assertIn("token=[REDACTED]", error)
        self.assertNotIn("sk-live-secret-token", error)

    async def test_adapter_json_response_helper_reports_non_json_preview(self) -> None:
        adapter = BaseImageAdapter(make_target(), FakeSession())
        response = FakeResponse(status=200, text="<html>token=abcdefghijklmnop</html>")

        data, error = await adapter.response_json_or_error(response)

        self.assertIsNone(data)
        self.assertIn("接口返回非 JSON 内容", error)
        self.assertIn("token=[REDACTED]", error)
        self.assertNotIn("abcdefghijklmnop", error)

    async def test_gemini_json_post_uses_api_key_header_without_bearer_auth(self) -> None:
        payload = {"candidates": [{"content": {"parts": [{"inlineData": {"data": base64.b64encode(PNG_BYTES).decode("ascii")}}]}}]}
        session = FakeSession(payload)
        adapter = GeminiImageAdapter(make_target("gemini", "gemini-2.0-flash"), session)

        result = await adapter.generate(ImageGenerateRequest(prompt="cat"))

        self.assertEqual(result.images, [PNG_BYTES])
        headers = session.requests[0]["headers"]
        self.assertEqual(headers["x-goog-api-key"], "test-key")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(headers["Accept"], "application/json")

    async def test_unknown_response_parser_deduplicates_nested_base64_images(self) -> None:
        data_url = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
        payload = {
            "data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}],
            "choices": [{"message": {"content": f"generated: {data_url}"}}],
        }
        images = await images_from_response_unknown(FakeSession(), payload, timeout=5)
        self.assertEqual(images, [PNG_BYTES])

    async def test_unknown_response_parser_accepts_urlsafe_base64_without_padding(self) -> None:
        image = PNG_BYTES + b"\xfb\xff\xff"
        encoded = base64.urlsafe_b64encode(image).decode("ascii").rstrip("=")
        payload = {"data": [{"b64_json": encoded}]}

        images = await images_from_response_unknown(FakeSession(), payload, timeout=5)

        self.assertEqual(images, [image])

    async def test_unknown_response_parser_accepts_direct_base64_string_items(self) -> None:
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")

        images = await images_from_response_unknown(FakeSession(), [encoded, "BASE64://" + encoded], timeout=5)

        self.assertEqual(images, [PNG_BYTES])

    async def test_unknown_response_parser_accepts_parameterized_data_urls(self) -> None:
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        data_url = f"data:image/png;name=result.png;charset=utf-8;base64,{encoded}"
        payload = {"choices": [{"message": {"content": f"generated: {data_url}"}}]}

        images = await images_from_response_unknown(FakeSession(), payload, timeout=5)

        self.assertEqual(images, [PNG_BYTES])

    async def test_unknown_response_parser_reads_base64_field_aliases(self) -> None:
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        payload = {
            "result": {
                "imageBase64": encoded,
                "variants": [
                    {"base64_image": "base64://" + encoded},
                    {"base64Data": encoded},
                    {"imageB64": encoded},
                    {"b64": encoded},
                ],
            }
        }

        images = await images_from_response_unknown(FakeSession(), payload, timeout=5)

        self.assertEqual(images, [PNG_BYTES])

    async def test_generated_data_url_download_rejects_fake_image_content(self) -> None:
        payload = base64.b64encode(b'{"error":"not image"}').decode("ascii")
        data_url = "data:image/png;base64," + payload

        image = await fetch_generated_image_url(FakeSession(), data_url, timeout=5)

        self.assertIsNone(image)

    async def test_unknown_response_parser_accepts_uppercase_inline_image_prefixes(self) -> None:
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        payload = {
            "data": [
                {"url": "DATA:image/png;BASE64," + encoded},
                {"imageBase64": "BASE64://" + encoded},
            ]
        }

        images = await images_from_response_unknown(FakeSession(), payload, timeout=5)

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(
            extract_image_urls_from_text("generated BASE64://" + encoded)["b64"],
            ["BASE64://" + encoded],
        )

    async def test_unknown_response_parser_resolves_relative_image_urls(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"data": [{"url": "/outputs/generated.png"}]}

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["method"], "GET")
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/generated.png")

    async def test_unknown_response_parser_resolves_protocol_relative_urls(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"data": [{"url": "//cdn.example.test/outputs/generated.png"}]}

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["method"], "GET")
        self.assertEqual(session.requests[0]["url"], "https://cdn.example.test/outputs/generated.png")

    async def test_unknown_response_parser_reads_text_protocol_relative_urls(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"choices": [{"message": {"content": "result //cdn.example.test/outputs/text-result.webp."}}]}

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(
            extract_image_urls_from_text("result //cdn.example.test/outputs/preview.png,")["others"],
            ["//cdn.example.test/outputs/preview.png"],
        )
        self.assertEqual(session.requests[0]["url"], "https://cdn.example.test/outputs/text-result.webp")

    async def test_unknown_response_parser_strips_trailing_ascii_url_punctuation(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"choices": [{"message": {"content": "result https://example.test/outputs/generated.png, done"}}]}

        images = await images_from_response_unknown(session, payload, timeout=5)

        extracted = extract_image_urls_from_text("one https://example.test/a.webp. two https://example.test/b.jpg;")
        self.assertEqual(set(extracted["urls"]), {"https://example.test/a.webp", "https://example.test/b.jpg"})
        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["method"], "GET")
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/generated.png")

    async def test_unknown_response_parser_strips_trailing_url_brackets(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"choices": [{"message": {"content": "result [https://example.test/outputs/bracketed.png]"}}]}

        images = await images_from_response_unknown(session, payload, timeout=5)

        extracted = extract_image_urls_from_text("one [https://example.test/a.webp] two {https://example.test/b.jpg}")
        self.assertEqual(set(extracted["urls"]), {"https://example.test/a.webp", "https://example.test/b.jpg"})
        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["method"], "GET")
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/bracketed.png")

    async def test_unknown_response_parser_unescapes_json_slash_urls_in_text(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"choices": [{"message": {"content": "result https:\\/\\/example.test\\/outputs\\/escaped.png."}}]}

        images = await images_from_response_unknown(session, payload, timeout=5)

        extracted = extract_image_urls_from_text("result https:\\/\\/example.test\\/outputs\\/escaped.webp.")
        self.assertEqual(extracted["urls"], ["https://example.test/outputs/escaped.webp"])
        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["method"], "GET")
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/escaped.png")

    async def test_unknown_response_parser_unescapes_unicode_slash_urls_in_text(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"choices": [{"message": {"content": "result https:\\u002F\\u002Fexample.test\\u002Foutputs\\u002Funicode.png."}}]}

        images = await images_from_response_unknown(session, payload, timeout=5)

        extracted = extract_image_urls_from_text("result https:\\u002f\\u002fexample.test\\u002foutputs\\u002funicode.webp.")
        self.assertEqual(extracted["urls"], ["https://example.test/outputs/unicode.webp"])
        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["method"], "GET")
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/unicode.png")

    async def test_unknown_response_parser_unescapes_unicode_colon_urls_in_text(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"choices": [{"message": {"content": "result https\\u003A\\u002F\\u002Fexample.test\\u002Foutputs\\u002Funicode-colon.png."}}]}

        images = await images_from_response_unknown(session, payload, timeout=5)

        extracted = extract_image_urls_from_text("result https\\u003a\\u002f\\u002fexample.test\\u002foutputs\\u002funicode-colon.webp.")
        self.assertEqual(extracted["urls"], ["https://example.test/outputs/unicode-colon.webp"])
        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["method"], "GET")
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/unicode-colon.png")

    async def test_unknown_response_parser_unescapes_unicode_query_urls_in_text(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "result https\\u003A\\u002F\\u002Fexample.test\\u002Foutputs\\u002Fquery.png\\u003Ftoken\\u003Dabc\\u0026size\\u003D1."
                    }
                }
            ]
        }

        images = await images_from_response_unknown(session, payload, timeout=5)

        extracted = extract_image_urls_from_text(
            "result https\\u003a\\u002f\\u002fexample.test\\u002foutputs\\u002fquery.webp\\u003ftoken\\u003dabc\\u0026size\\u003d1."
        )
        self.assertEqual(extracted["urls"], ["https://example.test/outputs/query.webp?token=abc&size=1"])
        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["method"], "GET")
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/query.png?token=abc&size=1")

    async def test_unknown_response_parser_unescapes_unicode_plus_fragment_urls_in_text(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "result https\\u003A\\u002F\\u002Fexample.test\\u002Foutputs\\u002Fsig.png\\u003Fsig\\u003Da\\u002Bb\\u0023preview"
                    }
                }
            ]
        }

        images = await images_from_response_unknown(session, payload, timeout=5)

        extracted = extract_image_urls_from_text(
            "result https\\u003a\\u002f\\u002fexample.test\\u002foutputs\\u002fsig.webp\\u003fsig\\u003da\\u002bb\\u0023preview"
        )
        self.assertEqual(extracted["urls"], ["https://example.test/outputs/sig.webp?sig=a+b#preview"])
        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["method"], "GET")
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/sig.png?sig=a+b#preview")

    async def test_unknown_response_parser_unescapes_hex_escaped_urls_in_text(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "result https\\x3A\\x2F\\x2Fexample.test\\x2Foutputs\\x2Fhex.png\\x3Fsig\\x3Da\\x2Bb\\x23preview"
                    }
                }
            ]
        }

        images = await images_from_response_unknown(session, payload, timeout=5)

        extracted = extract_image_urls_from_text(
            "result https\\x3a\\x2f\\x2fexample.test\\x2foutputs\\x2fhex.webp\\x3fsig\\x3da\\x2bb\\x23preview"
        )
        self.assertEqual(extracted["urls"], ["https://example.test/outputs/hex.webp?sig=a+b#preview"])
        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["method"], "GET")
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/hex.png?sig=a+b#preview")

    async def test_unknown_response_parser_resolves_modern_relative_filenames(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"data": [{"output": "generated.tiff"}]}

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["method"], "GET")
        self.assertEqual(session.requests[0]["url"], "https://example.test/generated.tiff")

    async def test_unknown_response_parser_resolves_plain_relative_string_items(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"images": ["outputs/plain-list.png"]}

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["method"], "GET")
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/plain-list.png")

    async def test_unknown_response_parser_reads_inline_relative_image_paths(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"choices": [{"message": {"content": "done outputs/inline-result.webp and ignored notes.txt"}}]}

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        extracted = extract_image_urls_from_text("preview generated.png and archive/report.txt")
        self.assertEqual(extracted["others"], ["generated.png"])
        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["method"], "GET")
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/inline-result.webp")

    async def test_unknown_response_parser_reads_nested_artifact_urls(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"artifacts": [{"asset": {"downloadUrl": "/media/generated.webp"}}]}

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["method"], "GET")
        self.assertEqual(session.requests[0]["url"], "https://example.test/media/generated.webp")

    async def test_unknown_response_parser_reads_uri_resource_url_aliases(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "data": [
                {"imageUri": "/media/from-uri.png"},
                {"resource": {"publicUrl": "/media/from-resource.png"}},
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(
            {request["url"] for request in session.requests},
            {
                "https://example.test/media/from-uri.png",
                "https://example.test/media/from-resource.png",
            },
        )

    async def test_unknown_response_parser_reads_path_result_url_aliases(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "data": [
                {"path": "outputs/from-path.png"},
                {"filePath": "/outputs/from-file-path.webp"},
                {"outputUrl": "/outputs/from-output-url.jpg"},
                {"resultUrl": "/outputs/from-result-url.png"},
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(
            {request["url"] for request in session.requests},
            {
                "https://example.test/outputs/from-path.png",
                "https://example.test/outputs/from-file-path.webp",
                "https://example.test/outputs/from-output-url.jpg",
                "https://example.test/outputs/from-result-url.png",
            },
        )

    async def test_unknown_response_parser_reads_file_asset_url_aliases(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "artifacts": [
                {"fileUrl": "/outputs/from-file-url.png"},
                {"asset_url": "/outputs/from-asset-url.webp"},
                {"artifactUrl": "/outputs/from-artifact-url.jpg"},
                {"downloadURL": "/outputs/from-download-url.png"},
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(
            {request["url"] for request in session.requests},
            {
                "https://example.test/outputs/from-file-url.png",
                "https://example.test/outputs/from-asset-url.webp",
                "https://example.test/outputs/from-artifact-url.jpg",
                "https://example.test/outputs/from-download-url.png",
            },
        )

    async def test_unknown_response_parser_reads_link_signed_cdn_aliases(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "files": [
                {"link": "outputs/from-link.png"},
                {"location": "/outputs/from-location.webp"},
                {"signedUrl": "/outputs/from-signed-url.jpg"},
                {"cdn_url": "/outputs/from-cdn-url.png"},
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(
            {request["url"] for request in session.requests},
            {
                "https://example.test/outputs/from-link.png",
                "https://example.test/outputs/from-location.webp",
                "https://example.test/outputs/from-signed-url.jpg",
                "https://example.test/outputs/from-cdn-url.png",
            },
        )

    async def test_unknown_response_parser_reads_preview_thumbnail_aliases(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "assets": [
                {"previewUrl": "/outputs/from-preview.png"},
                {"thumbnail_url": "/outputs/from-thumbnail.webp"},
                {"secureUrl": "/outputs/from-secure.jpg"},
                {"source_url": "/outputs/from-source-url.png"},
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(
            {request["url"] for request in session.requests},
            {
                "https://example.test/outputs/from-preview.png",
                "https://example.test/outputs/from-thumbnail.webp",
                "https://example.test/outputs/from-secure.jpg",
                "https://example.test/outputs/from-source-url.png",
            },
        )

    async def test_unknown_response_parser_reads_json_text_content(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": '```json\n{"image": {"uri": "/outputs/from-json-text.png"}}\n```'
                    }
                }
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/from-json-text.png")

    async def test_unknown_response_parser_reads_embedded_json_text_content(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": 'result follows:\n```json\n{"publicUrl": "/outputs/embedded-json.png"}\n```'
                    }
                }
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/embedded-json.png")

    async def test_unknown_response_parser_reads_sse_data_json_text_content(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": 'event: result\ndata: {"resourceUrl": "/outputs/from-sse.png"}\ndata: [DONE]'
                    }
                }
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/from-sse.png")

    async def test_unknown_response_parser_reads_compact_sse_data_json_text_content(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"text": 'data:{"url":"/outputs/compact-sse.png"}\ndata:{"url":"/outputs/compact-sse.png"}'}

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual([request["url"] for request in session.requests], ["https://example.test/outputs/compact-sse.png"])

    async def test_unknown_response_parser_resolves_markdown_relative_url_with_title(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"choices": [{"message": {"content": '![result](outputs/generated.png "preview")'}}]}

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["method"], "GET")
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/generated.png")

    async def test_unknown_response_parser_reads_unquoted_html_img_src(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"choices": [{"message": {"content": "<img src=/outputs/unquoted.png alt=result>"}}]}

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(
            extract_image_urls_from_text("<img src=outputs/unquoted.webp alt=result>")["others"],
            ["outputs/unquoted.webp"],
        )
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/unquoted.png")

    async def test_unknown_response_parser_reads_html_srcset(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"choices": [{"message": {"content": '<source srcset="/outputs/small.webp 1x, /outputs/large.webp 2x">'}}]}

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(
            set(extract_image_urls_from_text('<img srcset="outputs/a.webp 1x, https://cdn.example.test/b.png 2x">')["others"]),
            {"outputs/a.webp"},
        )
        self.assertIn(
            "https://cdn.example.test/b.png",
            extract_image_urls_from_text('<img srcset="outputs/a.webp 1x, https://cdn.example.test/b.png 2x">')["urls"],
        )
        self.assertEqual(
            {request["url"] for request in session.requests},
            {
                "https://example.test/outputs/small.webp",
                "https://example.test/outputs/large.webp",
            },
        )

    async def test_unknown_response_parser_reads_html_href_image_links(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": '<a href="outputs/from-anchor.png">download</a><link href=/outputs/from-link.webp rel=preload>'
                    }
                }
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        extracted = extract_image_urls_from_text('<a href="#top">top</a><a href="outputs/from-anchor.png">download</a>')
        self.assertEqual(extracted["others"], ["outputs/from-anchor.png"])
        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(
            {request["url"] for request in session.requests},
            {
                "https://example.test/outputs/from-anchor.png",
                "https://example.test/outputs/from-link.webp",
            },
        )

    async def test_unknown_response_parser_reads_html_meta_image_content(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": '<meta property="og:image" content="outputs/from-og.png"><meta name=twitter:image content=/outputs/from-twitter.webp>'
                    }
                }
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        extracted = extract_image_urls_from_text('<meta name="description" content="not an image"><meta property="og:image" content="outputs/meta.webp">')
        self.assertEqual(extracted["others"], ["outputs/meta.webp"])
        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(
            {request["url"] for request in session.requests},
            {
                "https://example.test/outputs/from-og.png",
                "https://example.test/outputs/from-twitter.webp",
            },
        )

    async def test_unknown_response_parser_reads_html_poster_background_attrs(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": '<video poster="outputs/from-poster.png"></video><table background=/outputs/from-background.webp></table>'
                    }
                }
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        extracted = extract_image_urls_from_text('<video poster="#ignored"></video><body background="outputs/body-bg.webp">')
        self.assertEqual(extracted["others"], ["outputs/body-bg.webp"])
        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(
            {request["url"] for request in session.requests},
            {
                "https://example.test/outputs/from-poster.png",
                "https://example.test/outputs/from-background.webp",
            },
        )

    async def test_unknown_response_parser_reads_embedded_html_image_attrs(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": '<source src="outputs/from-source.png"><object data=/outputs/from-object.webp></object>'
                    }
                }
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        extracted = extract_image_urls_from_text('<embed src="outputs/from-embed.webp"><script src="app.js"></script>')
        self.assertEqual(extracted["others"], ["outputs/from-embed.webp"])
        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(
            {request["url"] for request in session.requests},
            {
                "https://example.test/outputs/from-source.png",
                "https://example.test/outputs/from-object.webp",
            },
        )

    async def test_unknown_response_parser_reads_json_script_image_content(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": '<script type="application/ld+json">{"image":{"url":"/outputs/from-script.png"}}</script>'
                    }
                }
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/from-script.png")

    async def test_unknown_response_parser_reads_assigned_json_script_image_content(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": '<script>window.__DATA__ = {"result":{"imageUrl":"/outputs/from-assigned-script.webp"}};</script>'
                    }
                }
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/from-assigned-script.webp")

    async def test_unknown_response_parser_reads_jsonp_image_content(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {"choices": [{"message": {"content": 'callback({"resultUrl":"/outputs/from-jsonp.png"});'}}]}

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/from-jsonp.png")

    async def test_unknown_response_parser_reads_css_url_image_links(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "<div style=\"background-image:url('/outputs/from-css.png')\"></div>"
                    }
                }
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        extracted = extract_image_urls_from_text("background:url(outputs/from-css.webp), url(#icon)")
        self.assertEqual(extracted["others"], ["outputs/from-css.webp"])
        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["url"], "https://example.test/outputs/from-css.png")

    async def test_unknown_response_parser_reads_lazy_html_image_attrs(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": '<img data-src="outputs/from-data-src.png"><img data-original=/outputs/from-original.webp>'
                    }
                }
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        extracted = extract_image_urls_from_text('<img data-lazy-src="outputs/from-lazy.webp"><div data-url="#ignored"></div>')
        self.assertEqual(extracted["others"], ["outputs/from-lazy.webp"])
        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(
            {request["url"] for request in session.requests},
            {
                "https://example.test/outputs/from-data-src.png",
                "https://example.test/outputs/from-original.webp",
            },
        )

    async def test_unknown_response_parser_reads_lazy_html_srcset_attrs(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": '<img data-srcset="outputs/lazy-small.webp 1x, /outputs/lazy-large.webp 2x">'
                    }
                }
            ]
        }

        images = await images_from_response_unknown(
            session,
            payload,
            timeout=5,
            base_url="https://example.test/v1/images/generations",
        )

        extracted = extract_image_urls_from_text('<img data-lazy-srcset="outputs/from-lazy.webp 1x, #ignored 2x">')
        self.assertEqual(extracted["others"], ["outputs/from-lazy.webp"])
        self.assertEqual(images, [PNG_BYTES])
        self.assertEqual(
            {request["url"] for request in session.requests},
            {
                "https://example.test/outputs/lazy-small.webp",
                "https://example.test/outputs/lazy-large.webp",
            },
        )

    async def test_unknown_response_parser_ignores_invalid_content_length_header(self) -> None:
        session = FakeSession(get_data=PNG_BYTES, get_headers={"content-type": "image/png", "content-length": "unknown"})
        payload = {"data": [{"url": "https://example.test/generated.png"}]}

        images = await images_from_response_unknown(session, payload, timeout=5)

        self.assertEqual(images, [PNG_BYTES])

    async def test_unknown_response_parser_accepts_binary_content_type_aliases(self) -> None:
        session = FakeSession(get_data=PNG_BYTES, get_headers={"content-type": "application/x-binary"})
        payload = {"data": [{"url": "https://example.test/generated.bin"}]}

        images = await images_from_response_unknown(session, payload, timeout=5)

        self.assertEqual(images, [PNG_BYTES])

    async def test_unknown_response_parser_rejects_fake_image_content(self) -> None:
        session = FakeSession(get_data=b'{"error":"not image"}', get_headers={"content-type": "image/png"})
        payload = {"data": [{"url": "https://example.test/generated.png"}]}

        images = await images_from_response_unknown(session, payload, timeout=5)

        self.assertEqual(images, [])

    def test_text_url_extraction_cleans_markdown_html_and_trailing_punctuation(self) -> None:
        extracted = extract_image_urls_from_text(
            '<img src="https://example.test/a.png?x=1&amp;y=2"> '
            "![ref](https://example.test/b.webp). "
            "![rel](relative/generated.png \"preview\") "
            "![angle](<relative/angle.webp> 'preview') "
            "raw https://example.test/c.jpg)。"
        )
        self.assertIn("https://example.test/a.png?x=1&y=2", extracted["urls"])
        self.assertIn("https://example.test/b.webp", extracted["urls"])
        self.assertIn("https://example.test/c.jpg", extracted["urls"])
        self.assertIn("relative/generated.png", extracted["others"])
        self.assertIn("relative/angle.webp", extracted["others"])
        self.assertEqual(clean_image_url("https://example.test/d.png)。"), "https://example.test/d.png")
        self.assertEqual(clean_image_url('relative/generated.png "preview"'), "relative/generated.png")
        self.assertEqual(clean_image_url("<relative/angle.webp> 'preview'"), "relative/angle.webp")

    def test_grok_payload_maps_auto_aspect_and_resolution(self) -> None:
        adapter = GrokImageAdapter(make_target("grok", "grok-imagine-image"), FakeSession())
        payload = adapter.build_payload(ImageGenerateRequest(prompt="cat", aspect_ratio="自动", resolution="4K"))
        self.assertEqual(payload["aspect_ratio"], "auto")
        self.assertEqual(payload["resolution"], "4k")
        self.assertEqual(payload["response_format"], "b64_json")

    def test_agnes_payload_builder_keeps_remote_reference_url(self) -> None:
        adapter = AgnesImageAdapter(make_target(), FakeSession())

        payload = adapter.build_payload(
            ImageGenerateRequest(
                prompt="portrait",
                aspect_ratio="3:2",
                images=[ImageReference(data=PNG_BYTES, source_url="https://example.test/ref.png")],
            )
        )

        self.assertEqual(payload["size"], "1024x682")
        self.assertEqual(payload["extra_body"]["image"], ["https://example.test/ref.png"])
        self.assertEqual(payload["extra_body"]["response_format"], "url")

    async def test_agnes_payload_keeps_reference_image_and_size(self) -> None:
        response = {"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]}
        session = FakeSession(response)
        adapter = AgnesImageAdapter(make_target(), session)
        result = await adapter.generate(
            ImageGenerateRequest(
                prompt="portrait",
                aspect_ratio="9:16",
                images=[ImageReference(data=PNG_BYTES, mime_type="image/png")],
            )
        )

        self.assertEqual(result.images, [PNG_BYTES])
        payload = session.requests[0]["json"]
        self.assertEqual(session.requests[0]["url"], "https://example.test/v1/images/generations")
        self.assertEqual(payload["size"], "576x1024")
        self.assertEqual(payload["extra_body"]["response_format"], "url")
        self.assertTrue(payload["extra_body"]["image"][0].startswith("data:image/png;base64,"))

    async def test_agnes_http_error_uses_error_message_preview(self) -> None:
        session = FakeSession({"error": {"message": "model unavailable"}}, status=400)
        adapter = AgnesImageAdapter(make_target(), session)
        result = await adapter.generate(ImageGenerateRequest(prompt="cat"))
        self.assertIn("HTTP 400", result.error)
        self.assertIn("model unavailable", result.error)

    async def test_agnes_adapter_downloads_relative_response_url(self) -> None:
        response = {"data": [{"url": "/outputs/agnes.png"}]}
        session = FakeSession(response, text=json.dumps(response), get_data=PNG_BYTES)
        adapter = AgnesImageAdapter(make_target(), session)

        result = await adapter.generate(ImageGenerateRequest(prompt="portrait"))

        self.assertEqual(result.images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["url"], "https://example.test/v1/images/generations")
        self.assertEqual(session.requests[1]["method"], "GET")
        self.assertEqual(session.requests[1]["url"], "https://example.test/outputs/agnes.png")


class GeneratorFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_records_failed_attempt_then_success(self) -> None:
        first = make_target("grok", "bad-model")
        second = make_target("grok", "good-model")

        async def no_sleep(seconds):
            return None

        def create_fake_adapter(target, session):
            if target.model == "bad-model":
                return FakeGenerateAdapter(ImageGenerateResult(error="temporary failure"))
            return FakeGenerateAdapter(ImageGenerateResult(images=[PNG_BYTES]))

        with (
            patch("astrbot_plugin_selfie_image.generator.create_adapter", side_effect=create_fake_adapter),
            patch("astrbot_plugin_selfie_image.generator.asyncio.sleep", side_effect=no_sleep),
        ):
            result = await generate_image_with_fallback(
                [first, second],
                ImageGenerateRequest(prompt="cat"),
                FakeSession(),
                max_attempts=2,
            )

        self.assertEqual(result.images, [PNG_BYTES])
        self.assertEqual(result.used_model, second.label)
        self.assertEqual([attempt["success"] for attempt in result.attempts], [False, True])
        self.assertEqual(result.attempts[0]["error"], "temporary failure")

    async def test_fallback_stops_on_non_retryable_auth_error(self) -> None:
        first = make_target("openai", "bad-model")
        second = make_target("openai", "good-model")
        calls = {"n": 0}

        def create_fake_adapter(target, session):
            calls["n"] += 1
            if target.model == "bad-model":
                return FakeGenerateAdapter(ImageGenerateResult(error="HTTP 401: Invalid token"))
            return FakeGenerateAdapter(ImageGenerateResult(images=[PNG_BYTES]))

        with patch("astrbot_plugin_selfie_image.generator.create_adapter", side_effect=create_fake_adapter):
            result = await generate_image_with_fallback(
                [first, second],
                ImageGenerateRequest(prompt="cat"),
                FakeSession(),
                max_attempts=3,
            )

        self.assertFalse(result.images)
        self.assertEqual(calls["n"], 1)
        self.assertIn("鉴权", result.error)
        self.assertEqual(result.attempts[0].get("error_category"), "auth")

    async def test_fallback_rotates_api_key_on_auth_then_succeeds(self) -> None:
        target = ImageModelTarget(
            channel_name="relay",
            provider_type="openai",
            base_url="https://example.test",
            api_key="bad-key",
            model="gpt-image-2",
            timeout=30,
            api_keys=["bad-key", "good-key"],
        )
        calls = []

        class RotatingAdapter:
            def __init__(self, active_target, session):
                self.target = active_target

            async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
                calls.append(self.target.api_key)
                if self.target.api_key == "bad-key":
                    return ImageGenerateResult(error="HTTP 401: Invalid token")
                return ImageGenerateResult(images=[PNG_BYTES])

        def create_fake_adapter(active_target, session):
            return RotatingAdapter(active_target, session)

        with patch("astrbot_plugin_selfie_image.generator.create_adapter", side_effect=create_fake_adapter):
            result = await generate_image_with_fallback(
                [target],
                ImageGenerateRequest(prompt="cat"),
                FakeSession(),
                max_attempts=1,
            )

        self.assertEqual(calls, ["bad-key", "good-key"])
        self.assertEqual(result.images, [PNG_BYTES])
        self.assertEqual([a.get("success") for a in result.attempts], [False, True])
        self.assertEqual(result.attempts[0].get("key_index"), 1)
        self.assertEqual(result.attempts[1].get("key_index"), 2)

    async def test_fallback_returns_clear_error_without_targets(self) -> None:
        result = await generate_image_with_fallback([], ImageGenerateRequest(prompt="cat"), FakeSession())
        self.assertFalse(result.images)
        self.assertEqual(result.error, "未配置生图模型")

    async def test_fallback_redacts_sensitive_adapter_errors(self) -> None:
        target = make_target("grok", "bad-model")
        secret_error = "Authorization: Bearer sk-live-secret-token and token=abcdefghijklmnop"

        def create_fake_adapter(target, session):
            return FakeGenerateAdapter(ImageGenerateResult(error=secret_error))

        with patch("astrbot_plugin_selfie_image.generator.create_adapter", side_effect=create_fake_adapter):
            result = await generate_image_with_fallback(
                [target],
                ImageGenerateRequest(prompt="cat"),
                FakeSession(),
                max_attempts=1,
            )

        attempt_text = json.dumps(result.attempts, ensure_ascii=False)
        self.assertIn("Bearer [REDACTED]", result.error)
        self.assertIn("token=[REDACTED]", result.error)
        self.assertNotIn("sk-live-secret-token", result.error)
        self.assertNotIn("abcdefghijklmnop", attempt_text)

    async def test_fallback_redacts_sensitive_exceptions(self) -> None:
        target = make_target("grok", "bad-model")

        class RaisingAdapter:
            async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
                raise RuntimeError("api_key=AIzaSySecretTokenValue")

        with patch("astrbot_plugin_selfie_image.generator.create_adapter", return_value=RaisingAdapter()):
            result = await generate_image_with_fallback(
                [target],
                ImageGenerateRequest(prompt="cat"),
                FakeSession(),
                max_attempts=1,
            )

        self.assertIn("api_key=[REDACTED]", result.error)
        self.assertNotIn("AIzaSySecretTokenValue", json.dumps(result.attempts, ensure_ascii=False))

    async def test_fallback_redacts_sensitive_target_fields_on_final_failure(self) -> None:
        target = ImageModelTarget(
            channel_name="api_key=abcdefghijklmnop",
            provider_type="grok",
            base_url="https://example.test",
            api_key="test-key",
            model="token=secretmodelvalue12345",
            timeout=30,
        )

        def create_fake_adapter(target, session):
            return FakeGenerateAdapter(ImageGenerateResult(error="temporary failure"))

        with patch("astrbot_plugin_selfie_image.generator.create_adapter", side_effect=create_fake_adapter):
            result = await generate_image_with_fallback(
                [target],
                ImageGenerateRequest(prompt="cat"),
                FakeSession(),
                max_attempts=1,
            )

        attempt_text = json.dumps(result.attempts, ensure_ascii=False)
        self.assertIn("api_key=[REDACTED]", result.error)
        self.assertIn("token=[REDACTED]", attempt_text)
        self.assertIn("[REDACTED]", attempt_text)
        self.assertNotIn("abcdefghijklmnop", result.error)
        self.assertNotIn("secretmodelvalue12345", result.error)
        self.assertNotIn("abcdefghijklmnop", attempt_text)
        self.assertNotIn("secretmodelvalue12345", attempt_text)


@unittest.skipIf(Flask is None, "Flask is not installed")
class WebApiTests(unittest.TestCase):
    def make_client(self, plugin: FakeWebPlugin, host: str = "127.0.0.1"):
        if hasattr(plugin, "close"):
            self.addCleanup(plugin.close)
        server = FlaskWebServer(plugin)
        server.host = host
        server.port = 14514
        return server._create_app().test_client()

    def test_api_requires_token_when_configured(self) -> None:
        client = self.make_client(FakeWebPlugin("secret"), host="0.0.0.0")
        self.assertEqual(client.get("/api/health").status_code, 401)

        response = client.get("/api/health", headers={"Authorization": "Bearer secret"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(data["status"], "ok")
        for key in ("auth", "host", "port", "token"):
            self.assertNotIn(key, data)

    def test_auth_rejects_mismatched_tokens(self) -> None:
        client = self.make_client(FakeWebPlugin("secret"), host="0.0.0.0")

        non_ascii = client.get("/api/health", headers={"Authorization": "Bearer 密码"})
        oversized = client.get("/api/health", headers={"X-Selfie-Image-Token": "x" * 5000})

        self.assertEqual(non_ascii.status_code, 401)
        self.assertEqual(oversized.status_code, 401)
        self.assertIn("no-store", non_ascii.headers.get("Cache-Control", ""))
        self.assertIn("no-store", oversized.headers.get("Cache-Control", ""))

    def test_auth_accepts_any_valid_token_header(self) -> None:
        client = self.make_client(FakeWebPlugin("secret"), host="0.0.0.0")

        response = client.get(
            "/api/health",
            headers={
                "Authorization": "Bearer wrong-token",
                "X-Selfie-Image-Token": "secret",
            },
        )
        response_with_non_ascii_auth = client.get(
            "/api/health",
            headers={
                "Authorization": "Bearer 密码",
                "X-Token": "secret",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response_with_non_ascii_auth.status_code, 200)

    def test_auth_supports_non_ascii_configured_token(self) -> None:
        client = self.make_client(FakeWebPlugin("密钥"), host="0.0.0.0")

        wrong = client.get("/api/health", headers={"Authorization": "Bearer 密码"})
        right = client.get("/api/health", headers={"Authorization": "Bearer 密钥"})

        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(right.status_code, 200)

    def test_api_responses_are_not_cached(self) -> None:
        client = self.make_client(FakeWebPlugin("secret"), host="0.0.0.0")

        api_response = client.get("/api/health", headers={"Authorization": "Bearer secret"})
        page_response = client.get("/index.html")

        self.assertIn("no-store", api_response.headers.get("Cache-Control", ""))
        self.assertEqual(api_response.headers.get("Pragma"), "no-cache")
        self.assertEqual(api_response.headers.get("Expires"), "0")
        self.assertNotIn("no-store", page_response.headers.get("Cache-Control", ""))
        for response in (api_response, page_response):
            self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(response.headers.get("Referrer-Policy"), "no-referrer")
            self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")

    def test_empty_token_skips_auth_for_any_bind_host(self) -> None:
        self.assertEqual(self.make_client(FakeWebPlugin(""), host="127.0.0.1").get("/api/health").status_code, 200)
        self.assertEqual(self.make_client(FakeWebPlugin(""), host="0.0.0.0").get("/api/health").status_code, 200)

    def test_default_token_is_not_special_cased_by_web_auth(self) -> None:
        public_client = self.make_client(FakeWebPlugin("changeme"), host="0.0.0.0")
        local_client = self.make_client(FakeWebPlugin("changeme"), host="127.0.0.1")

        public_response = public_client.get("/api/health", headers={"X-Selfie-Image-Token": "changeme"})
        local_response = local_client.get("/api/health", headers={"X-Selfie-Image-Token": "changeme"})

        self.assertEqual(public_response.status_code, 200)
        self.assertEqual(local_response.status_code, 200)

    def test_config_api_does_not_expose_or_override_web_settings(self) -> None:
        plugin = FakeWebPlugin("secret")
        client = self.make_client(plugin, host="0.0.0.0")
        response = client.post(
            "/api/config",
            json={
                "config": {
                    "web": {"token": "bad", "host": "0.0.0.0"},
                    "webHost": "0.0.0.0",
                    "webPort": 9999,
                    "webToken": "bad",
                    "image": {"max_batch_count": 99},
                }
            },
            headers={"X-Selfie-Image-Token": "secret"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        for key in ("web", "webHost", "webPort", "webToken"):
            self.assertNotIn(key, data)
        self.assertEqual(plugin.config.web_token, "secret")
        self.assertEqual(plugin.config.web_host, "127.0.0.1")
        self.assertEqual(plugin.config.image_max_batch_count, 8)

    def test_frontend_does_not_display_startup_web_settings(self) -> None:
        self.assertNotIn("<b>监听", INDEX_HTML)
        self.assertNotIn("<b>Token", INDEX_HTML)
        self.assertIn("await enterApp(!AUTH_TOKEN)", INDEX_HTML)

    def test_frontend_api_helper_handles_network_invalid_json_and_auth_errors(self) -> None:
        self.assertIn("网络请求失败，请检查 Web 服务连接", INDEX_HTML)
        self.assertIn("接口返回了无效响应", INDEX_HTML)
        self.assertIn("document.body.classList.remove(\"authed\")", INDEX_HTML)
        self.assertIn("Object.assign({}, headers(), options.headers || {})", INDEX_HTML)

    def test_config_api_get_and_save_round_trip_common_settings(self) -> None:
        plugin = FakeWebPlugin("secret")
        client = self.make_client(plugin, host="0.0.0.0")
        headers = {"X-Selfie-Image-Token": "secret"}

        response = client.get("/api/config", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("web", response.get_json()["data"])

        response = client.post(
            "/api/config",
            json={
                "config": {
                    "bot_name": "自拍助手",
                    "image": {"max_batch_count": 4, "cache_limit_mb": 12},
                    "permission": {"usable_users": "1001,1002"},
                    "image_channels": [
                        {
                            "name": "main",
                            "provider_type": "openai",
                            "base_url": "https://example.test",
                            "model": "gpt-image-1",
                            "enabled_models": ["gpt-image-1", "gpt-image-2"],
                            "enabled": True,
                        }
                    ],
                    "enabled_image_model_priority": ["main/gpt-image-2", "main/gpt-image-1"],
                    "web": {"token": "bad", "host": "0.0.0.0"},
                }
            },
            headers=headers,
        )

        data = response.get_json()["data"]
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("web", data)
        self.assertEqual(data["bot_name"], "自拍助手")
        self.assertEqual(plugin.config.bot_name, "自拍助手")
        self.assertEqual(plugin.config.image_max_batch_count, 4)
        self.assertEqual(plugin.config.image_cache_limit_mb, 12)
        self.assertEqual(plugin.config.usable_users, ["1001", "1002"])
        self.assertEqual(plugin.config.enabled_image_model_priority, ["main/gpt-image-2", "main/gpt-image-1"])
        self.assertEqual([target.label for target in plugin.config.get_prioritized_targets()], ["main/gpt-image-2", "main/gpt-image-1"])
        self.assertEqual(plugin.config.web_token, "secret")
        self.assertEqual(plugin.config.web_host, "127.0.0.1")

    def test_config_api_rejects_invalid_json_shapes(self) -> None:
        client = self.make_client(FakeWebPlugin("secret"), host="0.0.0.0")
        headers = {"X-Selfie-Image-Token": "secret"}

        response = client.post("/api/config", data="{bad", content_type="application/json", headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("请求体必须是 JSON 对象", response.get_json()["error"])

        response = client.post("/api/config", json=["bad"], headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("请求体必须是 JSON 对象", response.get_json()["error"])

        response = client.post("/api/config", json={"config": ["bad"]}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("config 必须是 JSON 对象", response.get_json()["error"])

    def test_json_post_apis_reject_non_object_payloads_before_plugin_call(self) -> None:
        client = self.make_client(FakeWebPlugin("secret"), host="0.0.0.0")
        headers = {"X-Selfie-Image-Token": "secret"}
        routes = [
            "/api/selfie-reference",
            "/api/selfie-reference/clear",
            "/api/selfie-profile/refresh",
            "/api/test-image-channel",
            "/api/test-image-channel/tasks",
            "/api/refresh-image-models",
            "/api/records/clear",
        ]

        for route in routes:
            with self.subTest(route=route):
                response = client.post(route, json=["bad"], headers=headers)
                self.assertEqual(response.status_code, 400)
                self.assertIn("请求体必须是 JSON 对象", response.get_json()["error"])

    def test_records_api_requires_auth_and_returns_records(self) -> None:
        plugin = FakeWebPlugin("secret")
        client = self.make_client(plugin, host="0.0.0.0")

        self.assertEqual(client.get("/api/records").status_code, 401)
        response = client.get("/api/records", headers={"X-Selfie-Image-Token": "secret"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"], [{"id": 1, "success": True}])

        self.assertEqual(client.post("/api/records/clear", json={}).status_code, 401)
        response = client.post("/api/records/clear", json={}, headers={"X-Selfie-Image-Token": "secret"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"], {"deleted": 1})

    def test_records_api_supports_filtering_and_pagination(self) -> None:
        class RecordPlugin(FakeWebPlugin):
            def get_recent_records(self):
                return [
                    {"id": "1", "source_label": "群A", "group_id": "100", "user_id": "u1", "used_model": "model-a", "success": True},
                    {"id": "2", "source_label": "群A", "group_id": "100", "user_id": "u2", "used_model": "model-b", "success": False},
                    {"id": "3", "source_label": "私聊", "group_id": "", "user_id": "u3", "used_model": "model-a", "success": True},
                    {"id": "4", "source_label": "群B", "group_id": "200", "user_id": "u4", "used_model": "model-a", "success": False},
                ]

        client = self.make_client(RecordPlugin("secret"), host="0.0.0.0")
        headers = {"X-Selfie-Image-Token": "secret"}

        response = client.get(
            "/api/records",
            query_string={"source": "群", "model": "model-a", "success": "false", "limit": "1", "offset": "0"},
            headers=headers,
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in payload["data"]], ["4"])
        self.assertEqual(payload["total"], 4)
        self.assertEqual(payload["filtered"], 1)
        self.assertEqual(payload["offset"], 0)
        self.assertEqual(payload["limit"], 1)

        response = client.get("/api/records", query_string={"q": "u", "limit": "2", "offset": "1"}, headers=headers)
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in payload["data"]], ["2", "3"])
        self.assertEqual(payload["filtered"], 4)

    def test_records_api_rejects_invalid_filter_arguments(self) -> None:
        client = self.make_client(FakeWebPlugin("secret"), host="0.0.0.0")
        headers = {"X-Selfie-Image-Token": "secret"}

        response = client.get("/api/records", query_string={"limit": "0"}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("limit 不能小于 1", response.get_json()["error"])

        response = client.get("/api/records", query_string={"offset": "bad"}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("offset 必须是整数", response.get_json()["error"])

        response = client.get("/api/records", query_string={"success": "maybe"}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("success 必须是 true 或 false", response.get_json()["error"])

    def test_record_detail_api_requires_auth_validates_id_and_returns_record(self) -> None:
        plugin = FakeWebPlugin("secret")
        client = self.make_client(plugin, host="0.0.0.0")
        headers = {"X-Selfie-Image-Token": "secret"}

        self.assertEqual(client.get("/api/records/1").status_code, 401)

        response = client.get("/api/records/1", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["id"], 1)
        self.assertEqual(response.get_json()["data"]["request_data"], {"prompt": "test"})

        response = client.get("/api/records/" + ("x" * 200), headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("非法记录 ID", response.get_json()["error"])

        response = client.get("/api/records/not-found", headers=headers)
        self.assertEqual(response.status_code, 404)
        self.assertIn("记录不存在", response.get_json()["error"])

    def test_records_and_task_status_routes_redact_sensitive_data(self) -> None:
        class SensitivePlugin(FakeWebPlugin):
            def get_recent_records(self):
                return [{"error": "api_key=plain-provider-secret", "headers": {"Cookie": "session=abcdef1234567890"}}]

            def get_record_for_web(self, record_id: str):
                return {"id": record_id, "error": "token=abcdefghijklmnop", "headers": {"Authorization": "Bearer sk-live-secret-token"}}

            def get_web_image_task(self, task_id: str):
                self.task_status_calls.append(task_id)
                return {"task_id": task_id, "result": {"error": "Authorization: Bearer sk-live-secret-token"}}

        plugin = SensitivePlugin("secret")
        client = self.make_client(plugin, host="0.0.0.0")
        headers = {"X-Selfie-Image-Token": "secret"}

        records_response = client.get("/api/records", headers=headers)
        detail_response = client.get("/api/records/1", headers=headers)
        task_response = client.get("/api/test-image-channel/tasks/web-12345678-1", headers=headers)

        records_text = json.dumps(records_response.get_json()["data"], ensure_ascii=False)
        detail_text = json.dumps(detail_response.get_json()["data"], ensure_ascii=False)
        task_text = json.dumps(task_response.get_json()["data"], ensure_ascii=False)
        self.assertIn("api_key=[REDACTED]", records_text)
        self.assertIn('"Cookie": "[REDACTED]"', records_text)
        self.assertIn("token=[REDACTED]", detail_text)
        self.assertIn('"Authorization": "[REDACTED]"', detail_text)
        self.assertIn("Bearer [REDACTED]", task_text)
        self.assertNotIn("plain-provider-secret", records_text)
        self.assertNotIn("abcdefghijklmnop", detail_text)
        self.assertNotIn("sk-live-secret-token", detail_text)
        self.assertNotIn("sk-live-secret-token", task_text)

    def test_selfie_write_apis_accept_empty_object_payloads(self) -> None:
        client = self.make_client(FakeWebPlugin("secret"), host="0.0.0.0")
        headers = {"X-Selfie-Image-Token": "secret"}

        response = client.post("/api/selfie-reference/clear", json={}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["status"], "cleared")

        response = client.post("/api/selfie-profile/refresh", json={}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["status"], "refreshed")

        response = client.post("/api/selfie-profile/refresh", data="", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["status"], "refreshed")

    def test_selfie_reference_route_saves_and_rejects_invalid_images(self) -> None:
        plugin = FakeWebPlugin("secret")
        client = self.make_client(plugin, host="0.0.0.0")
        headers = {"X-Selfie-Image-Token": "secret"}

        response = client.get("/api/selfie-reference", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["data"]["has_image"])

        image = bytes_to_data_url(PNG_BYTES, "image/png")
        response = client.post("/api/selfie-reference", json={"image": image, "filename": "avatar.png"}, headers=headers)
        data = response.get_json()["data"]
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["has_image"])
        self.assertEqual(data["ref_mime_type"], "image/png")
        self.assertTrue(data["image"].startswith("data:image/png;base64,"))

        response = client.post("/api/selfie-reference/clear", json={}, headers=headers)
        self.assertEqual(response.status_code, 200)
        response = client.get("/api/selfie-reference", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["data"]["has_image"])
        self.assertNotIn("image", response.get_json()["data"])

        response = client.post("/api/selfie-reference", json={"image": "bm90IGFuIGltYWdl"}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("上传图片为空", response.get_json()["error"])

    def test_refresh_models_route_returns_count_and_redacts_failures(self) -> None:
        class FailingPlugin(FakeWebPlugin):
            async def web_refresh_image_models(self, payload):
                raise RuntimeError("api_key=plain-provider-secret")

        plugin = FakeWebPlugin("secret")
        client = self.make_client(plugin, host="0.0.0.0")
        headers = {"X-Selfie-Image-Token": "secret"}

        response = client.post(
            "/api/refresh-image-models",
            json={"channel": {"name": "main", "api_key": "sk-live-secret-token"}},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"], ["model-a", "model-b"])
        self.assertEqual(response.get_json()["count"], 2)

        client = self.make_client(FailingPlugin("secret"), host="0.0.0.0")
        response = client.post("/api/refresh-image-models", json={"channel": {"name": "main"}}, headers=headers)
        self.assertEqual(response.status_code, 500)
        self.assertIn("api_key=[REDACTED]", response.get_json()["error"])
        self.assertNotIn("plain-provider-secret", response.get_json()["error"])

    def test_channel_test_routes_redact_sensitive_success_payloads(self) -> None:
        class SensitivePlugin(FakeWebPlugin):
            async def web_test_image(self, payload):
                return {
                    "success": False,
                    "error": "Authorization: Bearer sk-live-secret-token",
                    "request_data": {"channel": {"api_key": "plain-provider-secret"}},
                }

            def start_web_image_task(self, payload):
                return {
                    "task_id": "web-12345678-9",
                    "status": "failed",
                    "result": {"error": "token=abcdefghijklmnop"},
                }

        client = self.make_client(SensitivePlugin("secret"), host="0.0.0.0")
        headers = {"X-Selfie-Image-Token": "secret"}

        sync_response = client.post("/api/test-image-channel", json={"prompt": "test"}, headers=headers)
        task_response = client.post("/api/test-image-channel/tasks", json={"prompt": "test"}, headers=headers)

        sync_text = json.dumps(sync_response.get_json()["data"], ensure_ascii=False)
        task_text = json.dumps(task_response.get_json()["data"], ensure_ascii=False)
        self.assertEqual(sync_response.status_code, 200)
        self.assertEqual(task_response.status_code, 200)
        self.assertIn("Bearer [REDACTED]", sync_text)
        self.assertIn('"api_key": "[REDACTED]"', sync_text)
        self.assertIn("token=[REDACTED]", task_text)
        self.assertNotIn("sk-live-secret-token", sync_text)
        self.assertNotIn("plain-provider-secret", sync_text)
        self.assertNotIn("abcdefghijklmnop", task_text)

    def test_web_error_responses_redact_sensitive_text(self) -> None:
        class FailingPlugin(FakeWebPlugin):
            async def refresh_selfie_profile_from_web(self):
                raise RuntimeError("Authorization: Bearer sk-live-secret-token")

        client = self.make_client(FailingPlugin("secret"), host="0.0.0.0")
        response = client.post("/api/selfie-profile/refresh", json={}, headers={"X-Selfie-Image-Token": "secret"})

        self.assertEqual(response.status_code, 500)
        self.assertIn("Bearer [REDACTED]", response.get_json()["error"])
        self.assertNotIn("sk-live-secret-token", response.get_json()["error"])

    def test_web_task_status_validates_task_id(self) -> None:
        plugin = FakeWebPlugin("secret")
        client = self.make_client(plugin, host="0.0.0.0")
        headers = {"X-Selfie-Image-Token": "secret"}

        self.assertEqual(client.get("/api/test-image-channel/tasks/web-12345678-1").status_code, 401)

        response = client.get("/api/test-image-channel/tasks/not-a-task", headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("非法任务 ID", response.get_json()["error"])
        self.assertEqual(plugin.task_status_calls, [])

        response = client.get("/api/test-image-channel/tasks/web-" + "1" * 200 + "-1", headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("非法任务 ID", response.get_json()["error"])
        self.assertEqual(plugin.task_status_calls, [])

        response = client.get("/api/test-image-channel/tasks/web-12345678-1", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["status"], "succeeded")

        response = client.get("/api/test-image-channel/tasks/web-12345678-2", headers=headers)
        self.assertEqual(response.status_code, 404)
        self.assertIn("任务不存在", response.get_json()["error"])

    def test_cache_image_route_serves_files_and_rejects_traversal(self) -> None:
        plugin = FakeWebPlugin("secret")
        image_path = os.path.join(plugin.generated_dir, "ok.png")
        Path(image_path).write_bytes(PNG_BYTES)
        client = self.make_client(plugin, host="0.0.0.0")
        headers = {"X-Selfie-Image-Token": "secret"}

        response = client.get("/api/cache-image?path=ok.png", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, PNG_BYTES)
        response.close()

        response = client.get("/api/cache-image?path=../secret.png", headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("非法图片路径", response.get_json()["error"])

        response = client.get("/api/cache-image", headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("图片路径不能为空", response.get_json()["error"])

        response = client.get("/api/cache-image?path=", headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("图片路径不能为空", response.get_json()["error"])

        response = client.get("/api/cache-image?path=.", headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("非法图片路径", response.get_json()["error"])

        response = client.get("/api/cache-image?path=" + ("a" * 600) + ".png", headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("图片路径过长", response.get_json()["error"])

    def test_cache_image_route_rejects_non_image_cache_files(self) -> None:
        plugin = FakeWebPlugin("secret")
        text_path = os.path.join(plugin.generated_dir, "not-image.txt")
        Path(text_path).write_text("not an image", encoding="utf-8")
        client = self.make_client(plugin, host="0.0.0.0")
        headers = {"X-Selfie-Image-Token": "secret"}

        response = client.get("/api/cache-image?path=not-image.txt", headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("缓存文件不是有效图片", response.get_json()["error"])


class SchemaTests(unittest.TestCase):
    def test_native_conf_schema_only_contains_web_startup_settings(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(set(schema), {"web"})
        self.assertEqual(set(schema["web"]["items"]), {"enable", "host", "port", "token"})


class SessionModelAndTaskTests(unittest.TestCase):
    def _plugin_stub(self):
        # main.py imports astrbot; stub minimal modules for unit tests outside runtime.
        if "astrbot" not in sys.modules:
            astrbot = types.ModuleType("astrbot")
            api = types.ModuleType("astrbot.api")
            star = types.ModuleType("astrbot.api.star")
            event = types.ModuleType("astrbot.api.event")
            comps = types.ModuleType("astrbot.api.message_components")

            class Star:
                def __init__(self, *a, **k):
                    pass

            def register(*a, **k):
                def deco(cls):
                    return cls

                return deco

            class filter:
                class PermissionType:
                    ADMIN = "admin"

                @staticmethod
                def command(*a, **k):
                    def deco(fn):
                        return fn

                    return deco

                @staticmethod
                def permission_type(*a, **k):
                    def deco(fn):
                        return fn

                    return deco

            star.Context = object
            star.Star = Star
            star.register = register
            event.AstrMessageEvent = object
            event.filter = filter
            comps.Image = type("Image", (), {})
            api.star = star
            api.event = event
            api.message_components = comps
            api.llm_tool = lambda *a, **k: (lambda f: f)
            api.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None, debug=lambda *a, **k: None)
            astrbot.api = api
            sys.modules["astrbot"] = astrbot
            sys.modules["astrbot.api"] = api
            sys.modules["astrbot.api.star"] = star
            sys.modules["astrbot.api.event"] = event
            sys.modules["astrbot.api.message_components"] = comps
            sys.modules["astrbot.core"] = types.ModuleType("astrbot.core")
            sys.modules["astrbot.core.utils"] = types.ModuleType("astrbot.core.utils")
            pathmod = types.ModuleType("astrbot.core.utils.astrbot_path")
            pathmod.get_astrbot_data_path = lambda: tempfile.gettempdir()
            sys.modules["astrbot.core.utils.astrbot_path"] = pathmod

        from astrbot_plugin_selfie_image import main as plugin_main

        plugin = object.__new__(plugin_main.SelfieImagePlugin)
        plugin._session_model_lock = __import__("threading").RLock()
        plugin._session_model_overrides = {}
        plugin._web_task_lock = __import__("threading").RLock()
        plugin._web_tasks = {}
        plugin._web_task_seq = 0
        plugin.config = AICatConfig.from_dict(
            {
                "image_channels": [
                    {
                        "name": "primary",
                        "provider_type": "openai",
                        "base_url": "https://example.test",
                        "api_key": "sk-test",
                        "enabled_models": ["gpt-image-2", "gpt-image-1"],
                    },
                    {
                        "name": "secondary",
                        "provider_type": "openai",
                        "base_url": "https://example.test",
                        "api_key": "sk-test",
                        "enabled_models": ["alt-model"],
                    },
                ],
                "enabled_image_model_priority": ["secondary/alt-model", "primary/gpt-image-2"],
            }
        )
        return plugin

    def test_session_model_override_reorders_targets(self) -> None:
        plugin = self._plugin_stub()

        class Ev:
            pass

        plugin._session_key = lambda event=None: "group:g1"
        labels = plugin._available_model_labels()
        self.assertEqual(labels[0], "secondary/alt-model")
        matched = plugin._match_model_label("2")
        self.assertEqual(matched, "primary/gpt-image-2")
        plugin._set_session_model_override(Ev(), matched)
        ordered = plugin._resolve_generation_targets(Ev())
        self.assertEqual(ordered[0].label, "primary/gpt-image-2")
        plugin._set_session_model_override(Ev(), "")
        ordered2 = plugin._resolve_generation_targets(Ev())
        self.assertEqual(ordered2[0].label, "secondary/alt-model")

    def test_cancel_image_task_session_isolation(self) -> None:
        plugin = self._plugin_stub()
        plugin._web_task_timestamp = lambda: "t"
        now = 1.0
        plugin._web_tasks["cmd-1"] = {
            "task_id": "cmd-1",
            "status": "queued",
            "owner_session": "group:a",
            "source": "command-draw",
            "created_ts": now,
            "updated_ts": now,
        }
        plugin._web_tasks["cmd-2"] = {
            "task_id": "cmd-2",
            "status": "running",
            "owner_session": "group:b",
            "source": "command-draw",
            "created_ts": now,
            "updated_ts": now,
            "cancel_requested": False,
        }
        msg = plugin.cancel_image_task("cmd-1", session_key="group:a")
        self.assertIn("已取消", msg)
        self.assertEqual(plugin._web_tasks["cmd-1"]["status"], "cancelled")
        with self.assertRaises(PermissionError):
            plugin.cancel_image_task("cmd-2", session_key="group:a")
        msg2 = plugin.cancel_image_task("cmd-2", session_key="group:b")
        self.assertIn("已记下取消", msg2)
        self.assertTrue(plugin._web_tasks["cmd-2"]["cancel_requested"])
        listed = plugin._list_image_tasks_for_session("group:b", include_finished=False)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["task_id"], "cmd-2")


class ReferenceCollectorTests(unittest.TestCase):
    def test_extract_buckets_message_quote_at_and_forward(self) -> None:
        from astrbot_plugin_selfie_image.reference_collector import (
            CollectedReferences,
            dedupe_image_references,
            extract_structured_image_sources,
            filter_bot_avatar_sources,
        )
        from astrbot_plugin_selfie_image.providers import ImageReference

        class Image:
            def __init__(self, url="", path=""):
                self.url = url
                self.path = path

        class Plain:
            def __init__(self, text=""):
                self.text = text

        class At:
            def __init__(self, qq):
                self.qq = qq

        class Quote:
            def __init__(self, message):
                self.message = message

        class Node:
            def __init__(self, message):
                self.message = message

        class Forward:
            def __init__(self, nodes):
                self.nodes = nodes

        class MessageObj:
            def __init__(self):
                self.message = [
                    Plain("see https://cdn.example/a.png"),
                    Image(url="https://cdn.example/msg.jpg"),
                    At("10001"),
                    Forward([Node([Image(path="/tmp/forward.webp")])]),
                ]
                self.quote = Quote([Image(url="https://cdn.example/quote.png")])

        class Event:
            def __init__(self):
                self.message_obj = MessageObj()
                self.message = None
                self.raw_message = None

        buckets = extract_structured_image_sources(Event(), include_at_avatar=True)
        self.assertTrue(any("a.png" in s or "msg.jpg" in s for s in buckets["message"]))
        self.assertTrue(any("quote.png" in s for s in buckets["quote"]))
        self.assertTrue(any("forward.webp" in s for s in buckets["forward"]))
        self.assertTrue(any("10001" in s for s in buckets["at_avatar"]))

        filtered = filter_bot_avatar_sources(
            [
                "https://q4.qlogo.cn/headimg_dl?dst_uin=999&spec=640",
                "https://cdn.example/keep.png",
            ],
            ["999"],
        )
        self.assertEqual(filtered, ["https://cdn.example/keep.png"])

        refs = [
            ImageReference(data=b"\x89PNG" + b"1" * 20, mime_type="image/png"),
            ImageReference(data=b"\x89PNG" + b"1" * 20, mime_type="image/png"),
            ImageReference(data=b"\x89PNG" + b"2" * 20, mime_type="image/png"),
        ]
        self.assertEqual(len(dedupe_image_references(refs)), 2)

        collected = CollectedReferences(
            message=[ImageReference(data=b"m" * 32, mime_type="image/png")],
            persona=[ImageReference(data=b"p" * 32, mime_type="image/png")],
        )
        self.assertEqual(len(collected.for_group_selfie()), 1)
        self.assertEqual(len(collected.for_draw(include_persona=True)), 2)
        self.assertEqual(len(collected.for_draw(include_persona=False)), 1)

    def test_plain_text_event_has_no_images(self) -> None:
        from astrbot_plugin_selfie_image.reference_collector import extract_structured_image_sources

        class Plain:
            def __init__(self, text=""):
                self.text = text

        class MessageObj:
            def __init__(self):
                self.message = [Plain("hello only text")]
                self.quote = None

        class Event:
            def __init__(self):
                self.message_obj = MessageObj()
                self.message = None
                self.raw_message = None

        buckets = extract_structured_image_sources(Event(), include_at_avatar=True)
        self.assertEqual(sum(len(v) for v in buckets.values()), 0)


class DashboardEmbedContractTests(unittest.TestCase):
    """Target 06: lock embed/token-free boot and channel-test poll contracts."""

    def setUp(self) -> None:
        self.html = INDEX_HTML or ""

    def test_index_html_has_embed_safe_storage_and_bridge_boot(self) -> None:
        self.assertIn("function safeStorageGet", self.html)
        self.assertIn("function safeStorageSet", self.html)
        self.assertIn("function safeStorageRemove", self.html)
        self.assertIn("function isDashboardPage", self.html)
        self.assertIn("function isEmbeddedFrame", self.html)
        self.assertIn("function bridgeEndpoint", self.html)
        self.assertIn("waitForDashboardBridge", self.html)
        self.assertIn("bridge-sdk.js", self.html)
        # Must strip /api/ prefix like telegram forwarder
        compact = re.sub(r"\s+", "", self.html)
        self.assertIn("noQuery.startsWith('/api/')", compact)
        self.assertIn("noQuery.slice('/api/'.length)", compact)
        self.assertIn("returnnoQuery.slice('/api/'.length)", compact)
        # No bare localStorage at boot for token (sandbox SecurityError risk)
        self.assertNotRegex(self.html, r"(?m)^\s*localStorage\.getItem\(")
        self.assertIn("safeStorageGet('selfieImageToken')", self.html)

    def test_bridge_endpoint_contract_examples(self) -> None:
        # Execute bridgeEndpoint pure logic copied from page contract.
        def bridge_endpoint(path: str) -> str:
            value = str(path or "").strip()
            no_query = value.split("?", 1)[0]
            if no_query.startswith("/api/"):
                return no_query[len("/api/") :]
            return no_query.lstrip("/")

        self.assertEqual(bridge_endpoint("/api/config"), "config")
        self.assertEqual(bridge_endpoint("/api/test-image-channel/tasks"), "test-image-channel/tasks")
        self.assertEqual(bridge_endpoint("/api/health?x=1"), "health")
        self.assertNotEqual(bridge_endpoint("/api/config"), "page/api/config")

    def test_channel_test_defaults_and_poll_retry_contract(self) -> None:
        self.assertIn('id="promptEnhance"', self.html)
        # default unchecked (no checked attribute on enhance)
        self.assertNotRegex(self.html, r'id="promptEnhance"[^>]*checked')
        self.assertIn("pollImageTestTask(taskId, failStreak = 0)", self.html)
        self.assertIn("nextFail = failStreak + 1", self.html)
        self.assertIn("failStreak", self.html)
        self.assertTrue(("15–60" in self.html) or ("15-60" in self.html) or ("15–60秒" in self.html.replace(" ", "")))
        # 1:1 preference when auto
        self.assertTrue("1:1" in self.html)

    def test_p0_visual_tokens_shared_by_index_and_dashboard_page(self) -> None:
        self.assertIn("#3c96ca", self.html)
        self.assertIn("#f6f8fb", self.html)
        self.assertIn("header-brand", self.html)
        self.assertIn("header-logo", self.html)
        self.assertIn("--radius-lg", self.html)
        self.assertIn("nav.page-nav", self.html)
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr))", self.html)
        self.assertIn("nav.page-nav button.active", self.html)
        self.assertIn("addVideoChannel", self.html)
        self.assertIn("channelTabVideo", self.html)
        self.assertIn("videoChannelList", self.html)
        self.assertIn("softDisableChannelIfNoModels", self.html)
        self.assertIn("isChannelModalOpen", self.html)
        self.assertIn("EDITING_CHANNEL_IS_NEW", self.html)
        self.assertIn("CHANNEL_DRAFT", self.html)
        self.assertIn("openChannelModal(-1, 'image', { isNew: true })", self.html)
        self.assertIn("scheduleFormAutoSave", self.html)
        self.assertIn("scheduleChannelListAutoSave", self.html)
        self.assertIn("modalProvider", self.html)
        self.assertIn("VIDEO_PROVIDERS", self.html)
        self.assertIn("openai_video", self.html)
        self.assertIn("sora", self.html)
        self.assertIn("veo", self.html)
        self.assertIn("seedance", self.html)
        self.assertIn("agnes", self.html)
        self.assertIn("video_sync", self.html)
        self.assertIn("video_chat", self.html)
        self.assertIn("resolveVideoModelProviderType", self.html)
        self.assertIn("normalizeVideoProviderType", self.html)
        self.assertIn("toastOnOk", self.html)
        self.assertIn("allowWhileModal", self.html)
        page = Path(__file__).resolve().parents[1] / "pages" / "dashboard" / "index.html"
        page_html = page.read_text(encoding="utf-8")
        self.assertIn("#3c96ca", page_html)
        self.assertIn("header-brand", page_html)
        self.assertIn("page-nav", page_html)
        self.assertIn("channelTabVideo", page_html)
        self.assertIn("开始试画", page_html)
        logo = Path(__file__).resolve().parents[1] / "pages" / "dashboard" / "logo.png"
        self.assertTrue(logo.is_file())
        from astrbot_plugin_selfie_image.web import render_index_html

        rendered = render_index_html()
        self.assertIn("data:image/png;base64,", rendered)
        self.assertNotIn("__SELFIE_LOGO_SRC__", rendered)

    def test_dashboard_api_registers_token_free_routes(self) -> None:
        from astrbot_plugin_selfie_image.dashboard_api import SelfieImageDashboardAPI
        from astrbot_plugin_selfie_image.constants import PLUGIN_NAME

        registered = []

        class Ctx:
            def register_web_api(self, path, handler, methods, desc):
                registered.append((path, tuple(methods), desc))

        class Plugin:
            context = Ctx()

        api = SelfieImageDashboardAPI(Plugin())
        api.register()
        paths = [item[0] for item in registered]
        self.assertTrue(any(p == f"/{PLUGIN_NAME}/health" for p in paths))
        self.assertTrue(any(p == f"/{PLUGIN_NAME}/page/health" for p in paths))
        self.assertTrue(any(p.endswith("/config") for p in paths))
        self.assertTrue(any("test-image-channel/tasks" in p for p in paths))
        # dual registration for bridge compatibility
        self.assertGreaterEqual(len(registered), 20)

    def test_openai_fast_path_and_trust_env_false_still_present(self) -> None:
        providers = Path(__file__).resolve().parents[1] / "providers.py"
        main = Path(__file__).resolve().parents[1] / "main.py"
        parser = Path(__file__).resolve().parents[1] / "provider_parser.py"
        self.assertIn("extract_openai_images_data", providers.read_text(encoding="utf-8"))
        self.assertIn("def extract_openai_images_data", parser.read_text(encoding="utf-8"))
        self.assertIn("ClientSession(trust_env=False)", main.read_text(encoding="utf-8"))


class AstrBotSmokeContractTests(unittest.TestCase):
    """Target 05: minimal runtime stubs without real AstrBot process."""

    def test_plugin_class_registers_with_stubbed_astrbot(self) -> None:
        if "astrbot" not in sys.modules:
            # reuse session test stubbing pattern
            astrbot = types.ModuleType("astrbot")
            api = types.ModuleType("astrbot.api")
            star = types.ModuleType("astrbot.api.star")
            event = types.ModuleType("astrbot.api.event")
            comps = types.ModuleType("astrbot.api.message_components")

            class Star:
                def __init__(self, *a, **k):
                    pass

            def register(*a, **k):
                def deco(cls):
                    return cls

                return deco

            class filter:
                class PermissionType:
                    ADMIN = "admin"

                @staticmethod
                def command(*a, **k):
                    def deco(fn):
                        return fn

                    return deco

                @staticmethod
                def permission_type(*a, **k):
                    def deco(fn):
                        return fn

                    return deco

            star.Context = object
            star.Star = Star
            star.register = register
            event.AstrMessageEvent = object
            event.filter = filter
            comps.Image = type("Image", (), {})
            api.star = star
            api.event = event
            api.message_components = comps
            api.llm_tool = lambda *a, **k: (lambda f: f)
            api.logger = types.SimpleNamespace(
                info=lambda *a, **k: None,
                warning=lambda *a, **k: None,
                error=lambda *a, **k: None,
                debug=lambda *a, **k: None,
            )
            astrbot.api = api
            sys.modules["astrbot"] = astrbot
            sys.modules["astrbot.api"] = api
            sys.modules["astrbot.api.star"] = star
            sys.modules["astrbot.api.event"] = event
            sys.modules["astrbot.api.message_components"] = comps
            sys.modules["astrbot.core"] = types.ModuleType("astrbot.core")
            sys.modules["astrbot.core.utils"] = types.ModuleType("astrbot.core.utils")
            pathmod = types.ModuleType("astrbot.core.utils.astrbot_path")
            pathmod.get_astrbot_data_path = lambda: tempfile.gettempdir()
            sys.modules["astrbot.core.utils.astrbot_path"] = pathmod

        from astrbot_plugin_selfie_image import main as plugin_main

        self.assertTrue(hasattr(plugin_main, "SelfieImagePlugin"))
        # command handlers exist
        for name in ("cmd_help", "cmd_help_text", "cmd_draw", "cmd_image_model", "cmd_image_tasks", "cmd_image_task_cancel", "cmd_video", "cmd_t2v", "cmd_i2v"):
            self.assertTrue(hasattr(plugin_main.SelfieImagePlugin, name), name)

    def test_help_uses_shipped_static_poster_only(self) -> None:
        root = Path(__file__).resolve().parents[1]
        poster = root / "assets" / "help_poster.png"
        logo = root / "logo.png"
        self.assertTrue(logo.is_file(), "logo.png must ship in repo")
        self.assertTrue(poster.is_file(), "assets/help_poster.png must ship in repo")
        self.assertGreater(poster.stat().st_size, 1000)
        self.assertTrue(looks_like_image_bytes(poster.read_bytes()[:32]))
        main_src = (root / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("刷新图", main_src)
        self.assertNotIn("_generate_help_poster", main_src)
        self.assertIn("assets", main_src)
        self.assertIn("help_poster.png", main_src)
        self.assertIn("_bundled_help_poster_path", main_src)
        self.assertIn("async def cmd_help", main_src)
        self.assertIn('@filter.command("生图help")', main_src)
        self.assertIn("async def cmd_help_text", main_src)
        # image-only path must not auto-append full help text in cmd_help body
        help_fn = main_src.split("async def cmd_help", 1)[1].split("async def cmd_help_text", 1)[0]
        self.assertNotIn("_help_text_body()", help_fn)
        self.assertIn("chain_result", help_fn)

    def test_selfie_prompt_requires_eye_contact_when_facing_camera(self) -> None:
        from astrbot_plugin_selfie_image.persona import PersonaManager

        with tempfile.TemporaryDirectory() as tmp:
            manager = PersonaManager(tmp)
            text = manager.build_selfie_prompt(
                action="",
                bot_name="小助",
                personality="温柔",
                has_reference_image=True,
                extra_reference_count=0,
            )
            self.assertIn("看向镜头", text)
            self.assertIn("心不在焉", text)
            group = manager.build_selfie_prompt(
                action="合影",
                bot_name="小助",
                personality="温柔",
                has_reference_image=True,
                extra_reference_count=1,
            )
            self.assertIn("看向镜头", group)
            self.assertIn("二次元", group)
            self.assertIn("写实", group)
            self.assertTrue(("禁止继续二次元" in group) or ("禁止把对方继续画成二次元" in group) or ("禁止画面里再出现二次元" in group))


    def test_legs_persona_mentions_kneel_and_light_leg(self) -> None:
        from astrbot_plugin_selfie_image.persona import PersonaManager

        with tempfile.TemporaryDirectory() as tmp:
            manager = PersonaManager(tmp)
            text = manager.build_selfie_prompt(
                action="看看腿",
                bot_name="小助",
                personality="温柔",
                has_reference_image=True,
                extra_reference_count=0,
            )
            self.assertIn("光腿神器", text)
            # persona no longer lists multi-pose menu (avoids model mixing poses)
            self.assertNotIn("主姿势在多种日常拍腿姿势间变化", text)
            self.assertNotIn("· 坐姿拍腿", text)
            self.assertNotIn("· 侧躺曲腿", text)
            self.assertIn("本次主姿势", text)
            self.assertIn("禁止系鞋带", text)
            self.assertIn("两条腿", text)
            self.assertIn("堆堆袜", text)
            self.assertIn("过膝袜", text)
            self.assertIn("避免只盖到脚踝的短袜", text)

    def test_look_you_and_selfie_persona_have_variety_hints(self) -> None:
        from astrbot_plugin_selfie_image.persona import PersonaManager

        with tempfile.TemporaryDirectory() as tmp:
            manager = PersonaManager(tmp)
            selfie = manager.build_selfie_prompt(
                action="看着镜头自然自拍",
                bot_name="小助",
                personality="温柔",
                has_reference_image=True,
                extra_reference_count=0,
            )
            self.assertIn("自拍", selfie)
            self.assertIn("看向镜头", selfie)
            # variety menu
            self.assertTrue(("自拍臂" in selfie) or ("镜前" in selfie) or ("窗边" in selfie))
            look = manager.build_selfie_prompt(
                action="看看你",
                bot_name="小助",
                personality="温柔",
                has_reference_image=True,
                extra_reference_count=0,
            )
            self.assertIn("他拍", look)
            self.assertTrue(("半身" in look) or ("三分之四" in look) or ("抓拍" in look))


class VideoV1Tests(unittest.TestCase):
    def test_video_channel_config_and_preflight(self) -> None:
        from astrbot_plugin_selfie_image.models import AICatConfig, preflight_video_channel

        bad = preflight_video_channel({"name": "v1"})
        self.assertFalse(bad["ok"])
        good = preflight_video_channel(
            {
                "name": "vid",
                "base_url": "https://example.com/v1",
                "api_key": "sk-test",
                "model": "sora-like",
                "enabled": True,
            }
        )
        self.assertTrue(good["ok"], good.get("message"))

        cfg = AICatConfig.from_dict(
            {
                "video": {"enable": True, "default_duration": 6, "global_timeout": 320},
                "video_channels": [
                    {
                        "name": "vid",
                        "base_url": "https://example.com/v1",
                        "api_key": "sk-a\nsk-b",
                        "model": "sora-like",
                        "enabled_models": ["sora-like", "gpt-4o-video-chat"],
                        "enabled": True,
                        "provider_type": "openai_video",
                        "model_provider_types": {"gpt-4o-video-chat": "video_chat"},
                    }
                ],
                "enabled_video_model_priority": ["vid/sora-like", "vid/gpt-4o-video-chat"],
            }
        )
        self.assertTrue(cfg.video_enable)
        self.assertEqual(cfg.video_default_duration, 6)
        targets = cfg.get_prioritized_video_targets()
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0].label, "vid/sora-like")
        # model name sora-like auto-infers family sora (not channel default)
        self.assertEqual(targets[0].provider_type, "sora")
        self.assertEqual(targets[1].provider_type, "video_chat")
        self.assertEqual(targets[0].resolved_api_keys(), ["sk-a", "sk-b"])
        self.assertGreaterEqual(targets[0].timeout, 60)

        # legacy openai label on video channel maps to openai_video
        legacy = AICatConfig.from_dict(
            {
                "video": {"enable": True},
                "video_channels": [
                    {
                        "name": "old",
                        "base_url": "https://x/v1",
                        "api_key": "k",
                        "model": "midgate-video-1",
                        "enabled_models": ["midgate-video-1"],
                        "enabled": True,
                        "provider_type": "openai",
                    }
                ],
            }
        )
        self.assertEqual(legacy.video_channels[0].provider_type, "openai_video")
        self.assertEqual(legacy.get_prioritized_video_targets()[0].provider_type, "openai_video")

        disabled = AICatConfig.from_dict({"video": {"enable": False}, "video_channels": [{"name": "vid", "base_url": "https://x", "api_key": "k", "model": "m"}]})
        self.assertEqual(disabled.get_prioritized_video_targets(), [])

    def test_video_protocol_normalize_and_infer(self) -> None:
        from astrbot_plugin_selfie_image.models import (
            infer_video_provider_type_from_model,
            normalize_video_provider_type,
            resolve_video_model_provider_type,
        )

        self.assertEqual(normalize_video_provider_type("async_task"), "openai_video")
        self.assertEqual(normalize_video_provider_type("sora"), "sora")
        self.assertEqual(normalize_video_provider_type("veo-3"), "veo")
        self.assertEqual(normalize_video_provider_type("seedance"), "seedance")
        self.assertEqual(normalize_video_provider_type("agnes"), "agnes")
        self.assertEqual(normalize_video_provider_type("openai_sync"), "video_sync")
        self.assertEqual(normalize_video_provider_type("openai_chat"), "video_chat")
        self.assertEqual(normalize_video_provider_type("openai"), "")  # image protocol
        self.assertEqual(infer_video_provider_type_from_model("sora-2"), "sora")
        self.assertEqual(infer_video_provider_type_from_model("veo-3.1"), "veo")
        self.assertEqual(infer_video_provider_type_from_model("doubao-seedance-1.0"), "seedance")
        self.assertEqual(infer_video_provider_type_from_model("agnes-video-pro"), "agnes")
        self.assertEqual(infer_video_provider_type_from_model("kling-v2"), "kling")
        self.assertEqual(
            resolve_video_model_provider_type("unknown", "video_sync", ""),
            "video_sync",
        )
        self.assertEqual(
            resolve_video_model_provider_type("x", "openai_video", "sora"),
            "sora",
        )


    def test_video_endpoint_and_extractors(self) -> None:
        from astrbot_plugin_selfie_image.video import (
            build_video_generations_endpoint,
            _extract_task_id,
            _extract_video_url,
            _extract_task_status,
        )

        self.assertTrue(build_video_generations_endpoint("https://api.example.com/v1").endswith("/videos/generations"))
        self.assertTrue(build_video_generations_endpoint("https://api.example.com/v1/videos/generations").endswith("/videos/generations"))
        self.assertEqual(_extract_task_id({"task_id": "abc"}), "abc")
        self.assertEqual(_extract_task_status({"status": "succeeded"}), "SUCCEEDED")
        self.assertEqual(_extract_video_url({"data": [{"url": "https://cdn.example/a.mp4"}]}), "https://cdn.example/a.mp4")

    def test_main_help_mentions_video_commands(self) -> None:
        main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn('@filter.command("视频")', main_src)
        self.assertIn('@filter.command("文生视频")', main_src)
        self.assertIn('@filter.command("图生视频")', main_src)
        self.assertIn("视频：", main_src)


class LegFocusTests(unittest.TestCase):
    def test_leg_focus_action_random_poses(self) -> None:
        # Import main with minimal astrbot stubs (same pattern as smoke tests).
        import sys
        import types
        import tempfile

        if "astrbot" not in sys.modules:
            astrbot = types.ModuleType("astrbot")
            api = types.ModuleType("astrbot.api")
            star = types.ModuleType("astrbot.api.star")
            event = types.ModuleType("astrbot.api.event")
            comps = types.ModuleType("astrbot.api.message_components")

            class Star:
                pass

            def register(*a, **k):
                def deco(cls):
                    return cls
                return deco

            class filter:
                @staticmethod
                def command(*a, **k):
                    def deco(fn):
                        return fn
                    return deco

                class PermissionType:
                    ADMIN = "admin"
                    MEMBER = "member"

                @staticmethod
                def permission_type(*a, **k):
                    def deco(fn):
                        return fn
                    return deco

            star.Context = object
            star.Star = Star
            star.register = register
            event.AstrMessageEvent = object
            event.filter = filter
            comps.Image = type("Image", (), {})
            api.star = star
            api.event = event
            api.message_components = comps
            api.llm_tool = lambda *a, **k: (lambda f: f)
            api.logger = types.SimpleNamespace(
                info=lambda *a, **k: None,
                warning=lambda *a, **k: None,
                error=lambda *a, **k: None,
                debug=lambda *a, **k: None,
            )
            astrbot.api = api
            sys.modules["astrbot"] = astrbot
            sys.modules["astrbot.api"] = api
            sys.modules["astrbot.api.star"] = star
            sys.modules["astrbot.api.event"] = event
            sys.modules["astrbot.api.message_components"] = comps
            sys.modules["astrbot.core"] = types.ModuleType("astrbot.core")
            sys.modules["astrbot.core.utils"] = types.ModuleType("astrbot.core.utils")
            pathmod = types.ModuleType("astrbot.core.utils.astrbot_path")
            pathmod.get_astrbot_data_path = lambda: tempfile.gettempdir()
            sys.modules["astrbot.core.utils.astrbot_path"] = pathmod

        # Ensure permission_type exists even if astrbot was stubbed earlier without it
        filt = sys.modules.get("astrbot.api.event")
        if filt is not None and hasattr(filt, "filter"):
            fobj = filt.filter
            if not hasattr(fobj, "PermissionType"):
                class PermissionType:
                    ADMIN = "admin"
                    MEMBER = "member"
                fobj.PermissionType = PermissionType
            if not hasattr(fobj, "permission_type"):
                fobj.permission_type = staticmethod(lambda *a, **k: (lambda fn: fn))

        from importlib import reload
        import astrbot_plugin_selfie_image.main as plugin_main
        # if previous import failed, modules may be partial; force reimport path
        if not hasattr(plugin_main, "SelfieImagePlugin"):
            plugin_main = reload(plugin_main)

        class _P:
            pass

        found = set()
        for _ in range(120):
            t = plugin_main.SelfieImagePlugin._build_leg_focus_action(_P(), "", False)
            self.assertIn("光腿神器", t)
            self.assertIn("脸部", t)
            m = re.search(r"【pose:([a-z_]+)】", t)
            if m:
                found.add(m.group(1))
        for key in ("sit", "kneel", "side_lie", "hug_knee", "cross_leg"):
            self.assertIn(key, found, f"missing pose {key} in samples {found}")
        main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        for key in ("stand_topdown", "windowsill", "kneel_up", "side_lie", "hug_knee", "cross_leg"):
            self.assertIn(f'"{key}"', main_src)
        self.assertNotIn("one_knee_fix", main_src)
        self.assertIn("禁止系鞋带", main_src)
        self.assertIn("两条腿", main_src)
        self.assertIn("堆堆袜", main_src)
        self.assertIn("过膝袜", main_src)
        self.assertIn("避免只盖到脚踝", main_src)

    def test_send_one_by_one_comment_present(self) -> None:
        main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn("生成一张发一张", main_src)
        self.assertIn("rebuild_each", main_src)
        self.assertIn("avoid_pose", main_src)
        self.assertIn("_build_selfie_look_action", main_src)
        self.assertIn("arm_half", main_src)
        self.assertIn("half_front", main_src)
        self.assertIn("command-look-you", main_src)


if __name__ == "__main__":
    unittest.main()
