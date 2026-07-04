from __future__ import annotations

import base64
import copy
import asyncio
import json
import os
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
from astrbot_plugin_selfie_image.models import (
    AICatConfig,
    DEFAULT_CONFIG,
    ImageModelTarget,
    deep_merge,
    normalize_config_tree,
    resolve_model_provider_type,
)
from astrbot_plugin_selfie_image.providers import (
    AgnesImageAdapter,
    GrokImageAdapter,
    ImageGenerateResult,
    ImageGenerateRequest,
    ImageReference,
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

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def text(self) -> str:
        return self._text

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
        os.makedirs(self.generated_dir, exist_ok=True)

    def _cache_size_bytes(self) -> int:
        return 0

    def get_config_for_web(self):
        data = copy.deepcopy(self.raw_config)
        data.pop("web", None)
        return data

    def update_config_from_web(self, patch):
        patch = copy.deepcopy(patch)
        patch.pop("web", None)
        self.raw_config = deep_merge(self.raw_config, patch)
        self.raw_config["web"] = copy.deepcopy(self.key_web)
        self.config = AICatConfig.from_dict(self.raw_config)
        return self.get_config_for_web()

    def get_recent_records(self):
        return [{"id": 1, "success": True}]

    def clear_recent_records(self):
        return 1

    def clear_selfie_reference_from_web(self):
        return {"has_image": False, "status": "cleared"}

    async def refresh_selfie_profile_from_web(self):
        return {"status": "refreshed", "updated_at": "2026-07-04 00:00:00"}

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
                "variants": [{"base64_image": "base64://" + encoded}],
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
        self.assertTrue(response.get_json()["data"]["auth"])

    def test_auth_rejects_non_ascii_and_oversized_tokens(self) -> None:
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

    def test_empty_token_only_allows_local_bind_host(self) -> None:
        self.assertEqual(self.make_client(FakeWebPlugin(""), host="127.0.0.1").get("/api/health").status_code, 200)
        self.assertEqual(self.make_client(FakeWebPlugin(""), host="0.0.0.0").get("/api/health").status_code, 401)

    def test_default_weak_token_is_rejected_on_public_bind_host(self) -> None:
        public_client = self.make_client(FakeWebPlugin("changeme"), host="0.0.0.0")
        local_client = self.make_client(FakeWebPlugin("changeme"), host="127.0.0.1")

        public_response = public_client.get("/api/health", headers={"X-Selfie-Image-Token": "changeme"})
        local_response = local_client.get("/api/health", headers={"X-Selfie-Image-Token": "changeme"})

        self.assertEqual(public_response.status_code, 401)
        self.assertEqual(local_response.status_code, 200)

    def test_config_api_does_not_expose_or_override_web_settings(self) -> None:
        plugin = FakeWebPlugin("secret")
        client = self.make_client(plugin, host="0.0.0.0")
        response = client.post(
            "/api/config",
            json={"config": {"web": {"token": "bad", "host": "0.0.0.0"}, "image": {"max_batch_count": 99}}},
            headers={"X-Selfie-Image-Token": "secret"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("web", response.get_json()["data"])
        self.assertEqual(plugin.config.web_token, "secret")
        self.assertEqual(plugin.config.web_host, "127.0.0.1")
        self.assertEqual(plugin.config.image_max_batch_count, 8)

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

    def test_records_and_task_status_routes_redact_sensitive_data(self) -> None:
        class SensitivePlugin(FakeWebPlugin):
            def get_recent_records(self):
                return [{"error": "api_key=plain-provider-secret", "headers": {"Cookie": "session=abcdef1234567890"}}]

            def get_web_image_task(self, task_id: str):
                self.task_status_calls.append(task_id)
                return {"task_id": task_id, "result": {"error": "Authorization: Bearer sk-live-secret-token"}}

        plugin = SensitivePlugin("secret")
        client = self.make_client(plugin, host="0.0.0.0")
        headers = {"X-Selfie-Image-Token": "secret"}

        records_response = client.get("/api/records", headers=headers)
        task_response = client.get("/api/test-image-channel/tasks/web-12345678-1", headers=headers)

        records_text = json.dumps(records_response.get_json()["data"], ensure_ascii=False)
        task_text = json.dumps(task_response.get_json()["data"], ensure_ascii=False)
        self.assertIn("api_key=[REDACTED]", records_text)
        self.assertIn('"Cookie": "[REDACTED]"', records_text)
        self.assertIn("Bearer [REDACTED]", task_text)
        self.assertNotIn("plain-provider-secret", records_text)
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


class SchemaTests(unittest.TestCase):
    def test_native_conf_schema_only_contains_web_startup_settings(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(set(schema), {"web"})
        self.assertEqual(set(schema["web"]["items"]), {"enable", "host", "port", "token"})


if __name__ == "__main__":
    unittest.main()
