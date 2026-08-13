from __future__ import annotations

import base64
import copy
import asyncio
import json
import os
import re
import sys
import tempfile
import threading
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
from astrbot_plugin_selfie_image.proxy import parse_channel_proxy
from astrbot_plugin_selfie_image.video import VideoGenerateRequest, VideoGenerateResult, generate_video_with_fallback
from astrbot_plugin_selfie_image.error_classify import (
    classify_generation_error,
    is_non_retryable_generation_error,
    is_param_profile_switch_error,
    is_transport_profile_switch_error,
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
    NovelAIImageAdapter,
    OpenAIImageAdapter,
    build_model_list_urls,
    clean_image_url,
    extract_model_ids_from_response,
    extract_image_urls_from_text,
    fetch_generated_image_url,
    http_error_preview,
    images_from_response_unknown,
    looks_like_binary_image,
    map_aspect_ratio_to_nai_gateway_size,
    map_aspect_ratio_to_nai_size,
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

    def get_recent_records(self, *args, **kwargs):
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
            "is_video": (
                os.path.isfile(path)
                and (Path(path).read_bytes()[:12][4:12].startswith(b"ftyp") or path.lower().endswith((".mp4", ".webm", ".mov")))
            ),
            "mime_type": "video/mp4" if path.lower().endswith(".mp4") else "image/png",
        }

    def close(self) -> None:
        self.temp_dir.cleanup()


class ConfigModelTests(unittest.TestCase):
    def test_plugin_version_matches_metadata(self) -> None:
        from astrbot_plugin_selfie_image.constants import PLUGIN_VERSION

        metadata = (Path(__file__).resolve().parents[1] / "metadata.yaml").read_text(encoding="utf-8")
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"version: {PLUGIN_VERSION}", metadata)
        self.assertIn(f"当前版本：`{PLUGIN_VERSION}`", readme)
        self.assertEqual(PLUGIN_VERSION, "1.3.76")

    def test_runtime_defaults_match_public_schema(self) -> None:
        config = AICatConfig.from_dict({})
        self.assertEqual(config.web_host, "127.0.0.1")
        self.assertEqual(config.image_max_batch_count, 2)

    def test_numeric_config_is_clamped(self) -> None:
        config = AICatConfig.from_dict({"image": {"max_batch_count": 99, "max_concurrent_tasks": 0}})
        self.assertEqual(config.image_max_batch_count, 20)
        self.assertEqual(config.image_max_concurrent_tasks, 1)

    def test_astrbot_wrapped_values_are_unwrapped(self) -> None:
        raw = {"image": {"value": {"max_batch_count": {"value": 4}}, "type": "object"}}
        self.assertEqual(normalize_config_tree(raw), {"image": {"max_batch_count": 4}})

    def test_channel_proxy_parser_supports_http_and_socks5_auth(self) -> None:
        cases = {
            "http://admin:wowull@127.0.0.1:40500": ("http", True),
            "http://127.0.0.1:40500": ("http", False),
            "socks5://admin:wowull@127.0.0.1:40500": ("socks5", True),
            "socks5://127.0.0.1:40500": ("socks5", False),
        }
        for value, expected in cases.items():
            parsed = parse_channel_proxy(value)
            self.assertEqual((parsed.scheme, parsed.has_auth), expected)
            self.assertEqual(parsed.host, "127.0.0.1")
            self.assertEqual(parsed.port, 40500)

    def test_channel_proxy_parser_rejects_partial_auth_and_unknown_scheme(self) -> None:
        for value in ("socks5://admin@127.0.0.1:40500", "ftp://127.0.0.1:40500", "http://127.0.0.1"):
            with self.assertRaises(ValueError):
                parse_channel_proxy(value)



    def test_model_download_proxy_override(self) -> None:
        from astrbot_plugin_selfie_image.models import AICatConfig
        cfg = AICatConfig.from_dict({
            "proxies": [
                {"id": "px_req", "protocol": "http", "host": "1.1.1.1", "port": 7890, "name": "req", "enabled": True},
                {"id": "px_dl", "protocol": "http", "host": "2.2.2.2", "port": 7890, "name": "dl", "enabled": True},
            ],
            "image_channels": [{
                "name": "c1",
                "provider_type": "openai",
                "base_url": "https://api.openai.com",
                "api_key": "k",
                "model": "m1",
                "enabled_models": ["m1", "m2"],
                "proxy_id": "px_req",
                "model_download_proxy_ids": {"m2": "px_dl"},
            }],
        })
        by = {t.model: t for t in cfg.get_prioritized_targets()}
        self.assertIn("1.1.1.1", by["m1"].proxy)
        self.assertFalse((by["m1"].extra or {}).get("download_proxy"))
        self.assertIn("1.1.1.1", by["m2"].proxy)
        self.assertEqual((by["m2"].extra or {}).get("download_proxy_id"), "px_dl")
        self.assertIn("2.2.2.2", (by["m2"].extra or {}).get("download_proxy") or "")
        from pathlib import Path
        providers = (Path(__file__).resolve().parents[1] / "providers.py").read_text(encoding="utf-8")
        self.assertIn("download_proxy", providers)
        html = (Path(__file__).resolve().parents[1] / "pages/dashboard/index.html").read_text(encoding="utf-8")
        self.assertIn("modelDownloadProxySelectHtml", html)
        self.assertIn("model-download-proxy", html)
        self.assertIn("btn.dataset.tab === 'proxies'", html)


    

    def test_parse_prompt_en_response_json(self) -> None:
        from pathlib import Path
        import json
        import re as _re
        from astrbot_plugin_selfie_image.models import DEFAULT_CONFIG

        img_t = DEFAULT_CONFIG["image"]["image_prompt_en_template"]
        vid_t = DEFAULT_CONFIG["image"]["video_prompt_en_template"]
        self.assertIn("faithful language conversion only", img_t)
        self.assertIn('"ok":true', img_t)
        self.assertIn("{prompt}", img_t)
        self.assertNotIn("Task: rewrite the user prompt", img_t)
        self.assertIn("faithful language conversion only", vid_t)
        self.assertIn('"ok":true', vid_t)
        main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn("def parse_prompt_en_response", main_src)
        self.assertIn("translate_parse_failed", main_src)
        self.assertIn("fail-open: keep original prompt", main_src)

        def parse(text: str) -> str:
            cleaned = str(text or "").strip()
            if not cleaned:
                return ""
            if cleaned.startswith("```"):
                cleaned = _re.sub(r"^```(?:\w+)?\s*", "", cleaned)
                cleaned = _re.sub(r"\s*```$", "", cleaned).strip()
            try:
                payload = json.loads(cleaned)
            except Exception:
                m = _re.search(r"\{.*?\}", cleaned, flags=_re.S)
                payload = json.loads(m.group(0)) if m else None
            if isinstance(payload, dict):
                if payload.get("ok") is False:
                    return ""
                return str(payload.get("en") or "").strip()
            return ""

        self.assertEqual(parse('{"ok":true,"en":"white stockings"}'), "white stockings")
        self.assertEqual(parse('{"ok":false,"en":""}'), "")
        self.assertEqual(parse('{"ok":true,"en":""}'), "")
        fenced = "```json\n{\"ok\":true,\"en\":\"cat walk\"}\n```"
        self.assertEqual(parse(fenced), "cat walk")

    def test_bilingual_prompt_only_replaces_user_text(self) -> None:
        from astrbot_plugin_selfie_image.prompt_templates import BilingualPrompt

        prompt = BilingualPrompt(
            builtin_zh="内置中文约束",
            builtin_en="Built-in English constraints.",
            user_text="海边回头微笑",
        )
        self.assertEqual(prompt.render_zh(), "内置中文约束\n用户要求：海边回头微笑")
        self.assertEqual(
            prompt.render_en("turn back and smile by the sea"),
            "Built-in English constraints.\nUser request: turn back and smile by the sea",
        )
        self.assertEqual(prompt.render_en(), "Built-in English constraints.")
        self.assertNotIn("内置中文约束", prompt.render_en("turn back and smile by the sea"))

    def test_selfie_builtin_prompt_has_compact_english_version(self) -> None:
        from astrbot_plugin_selfie_image.prompt_templates import build_selfie_builtin_prompt

        zh = build_selfie_builtin_prompt(
            "看看腿。用户补充要求优先：窗边白裙。 【pose:sit】",
            language="zh",
            has_reference_image=True,
            extra_reference_count=0,
            appearance_type="real",
        )
        en = build_selfie_builtin_prompt(
            "看看腿。用户补充要求优先：窗边白裙。 【pose:sit】",
            language="en",
            has_reference_image=True,
            extra_reference_count=0,
            appearance_type="real",
        )
        self.assertIn("大腿", zh)
        self.assertIn("双脚裁出画外", zh)
        self.assertIn("脸部", zh)
        self.assertIn("crop both ankles and feet", en)
        self.assertIn("Do not show the face", en)
        self.assertNotRegex(en, r"[\u3400-\u9fff]")
        self.assertNotIn("User request:", en)
        self.assertLess(len(en), 2400)

        translated = build_selfie_builtin_prompt(
            "看看腿。用户补充要求优先：窗边白裙。 【pose:sit】",
            language="en",
            has_reference_image=True,
            appearance_type="real",
            user_text="a white dress by the window",
        )
        self.assertIn("User request: a white dress by the window", translated)

    def test_batch_failure_llm_prompt_is_soft_and_keeps_single_reason(self) -> None:
        from astrbot_plugin_selfie_image.prompt_templates import build_batch_failure_llm_prompt

        prompt = build_batch_failure_llm_prompt(
            bot_name="啊呜",
            reason="上游暂时繁忙",
            index=2,
            total=4,
            done_files=1,
            will_continue=True,
        )
        self.assertIn("上游暂时繁忙", prompt)
        self.assertIn("还会继续尝试后面的", prompt)
        self.assertIn("自然、柔和", prompt)
        self.assertNotIn("可能", prompt)

    
    def test_batch_on_failure_config(self) -> None:
        from astrbot_plugin_selfie_image.models import AICatConfig, DEFAULT_CONFIG
        self.assertEqual(DEFAULT_CONFIG["image"].get("batch_on_failure"), "skip")
        cfg = AICatConfig.from_dict({"image": {"batch_on_failure": "stop", "batch_skip_max": 3}})
        self.assertEqual(cfg.image_batch_on_failure, "stop")
        self.assertEqual(cfg.image_batch_skip_max, 3)
        cfg2 = AICatConfig.from_dict({"image": {"batch_on_failure": "skip_continue"}})
        self.assertEqual(cfg2.image_batch_on_failure, "skip")
        html = (Path(__file__).resolve().parents[1] / "pages/dashboard/index.html").read_text(encoding="utf-8")
        self.assertIn("batchOnFailure", html)
        self.assertIn("batchSkipMax", html)
        main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn("_batch_failure_policy", main_src)
        self.assertIn("_batch_shot_fail_text", main_src)
        self.assertIn("will_continue", main_src)

    def test_video_payload_grok_midgate_minimal(self) -> None:
        from astrbot_plugin_selfie_image.models import ImageModelTarget
        from astrbot_plugin_selfie_image.video import VideoGenerateRequest, _extract_task_id, _video_payload, build_video_generations_endpoint
        target = ImageModelTarget(
            channel_name="t",
            provider_type="grok",
            base_url="https://api.futureppo.top",
            api_key="k",
            model="grok-imagine-video",
            timeout=60,
        )
        req = VideoGenerateRequest(prompt="a cat", duration=6, size="1280x720")
        payload = _video_payload(target, req, [], family="grok")
        self.assertEqual(payload.get("model"), "grok-imagine-video")
        self.assertEqual(payload.get("prompt"), "a cat")
        self.assertNotIn("seconds", payload)
        self.assertNotIn("n_seconds", payload)
        self.assertNotIn("n", payload)
        self.assertNotIn("size", payload)
        self.assertIn(payload.get("aspect_ratio"), {"16:9", "1280x720"})
        # default openai family still has duration
        payload2 = _video_payload(target, req, [], family="openai_video")
        self.assertIn("duration", payload2)
        self.assertEqual(
            build_video_generations_endpoint("https://api.futureppo.top"),
            "https://api.futureppo.top/v1/videos/generations",
        )
        self.assertEqual(_extract_task_id({"request_id": "task_abc"}), "task_abc")
        self.assertEqual(_extract_task_id({"data": {"request_id": "task_nested"}}), "task_nested")


    def test_prompt_en_config_and_cjk_gate(self) -> None:
        from astrbot_plugin_selfie_image.models import AICatConfig, DEFAULT_CONFIG
        self.assertIn("enable_image_prompt_en", DEFAULT_CONFIG["image"])
        self.assertIn("enable_video_prompt_en", DEFAULT_CONFIG["image"])
        cfg = AICatConfig.from_dict({
            "image": {
                "enable_image_prompt_en": True,
                "enable_video_prompt_en": True,
                "prompt_en_mode": "if_cjk",
                "prompt_en_model": "audit/gpt",
            }
        })
        self.assertTrue(cfg.image_enable_image_prompt_en)
        self.assertTrue(cfg.image_enable_video_prompt_en)
        self.assertEqual(cfg.image_prompt_en_mode, "if_cjk")
        self.assertEqual(cfg.image_prompt_en_model, "audit/gpt")
        html = (Path(__file__).resolve().parents[1] / "pages/dashboard/index.html").read_text(encoding="utf-8")
        self.assertIn("enableImagePromptEn", html)
        self.assertIn("enableVideoPromptEn", html)
        self.assertIn("promptEnMode", html)
        self.assertIn("生图提示词转英文", html)
        self.assertIn("视频提示词转英文", html)
        main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn("_translate_prompt_to_english", main_src)
        self.assertIn("_prompt_en_needed", main_src)
        self.assertIn('media="image"', main_src)
        self.assertIn('media="video"', main_src)

    def test_proxy_list_migrate_channel_proxy(self) -> None:
        from astrbot_plugin_selfie_image.models import AICatConfig, normalize_proxy_entry
        row = normalize_proxy_entry({
            "protocol": "http", "host": "10.0.0.2", "port": 7890,
            "username": "u", "password": "p", "name": "home",
        })
        self.assertTrue(row and row["id"].startswith("px_"))
        self.assertIn("10.0.0.2", row["url"])
        cfg = AICatConfig.from_dict({
            "proxies": [row],
            "image_channels": [{
                "name": "c1",
                "provider_type": "openai",
                "base_url": "https://api.openai.com",
                "api_key": "k",
                "model": "m",
                "enabled_models": ["m"],
                "proxy_id": row["id"],
            }],
        })
        self.assertEqual(cfg.image_channels[0].proxy_id, row["id"])
        self.assertIn("7890", cfg.image_channels[0].proxy)
        # legacy free-form migrates into proxies
        cfg2 = AICatConfig.from_dict({
            "image_channels": [{
                "name": "c2",
                "provider_type": "openai",
                "base_url": "https://api.openai.com",
                "api_key": "k",
                "model": "m",
                "enabled_models": ["m"],
                "proxy": "socks5://127.0.0.1:1080",
            }],
        })
        self.assertTrue(cfg2.image_channels[0].proxy_id)
        self.assertTrue(any(p.get("port") == 1080 for p in cfg2.proxies))

    def test_dashboard_has_proxy_page_and_channel_proxy_select(self) -> None:
        from pathlib import Path
        from astrbot_plugin_selfie_image.web import INDEX_HTML
        html = (Path(__file__).resolve().parents[1] / "pages/dashboard/index.html").read_text(encoding="utf-8")
        for doc in (html, INDEX_HTML):
            self.assertIn('data-tab="proxies"', doc)
            self.assertIn('id="proxies"', doc)
            self.assertIn("modalProxyId", doc)
            self.assertIn("function renderProxyList", doc)
            self.assertIn("proxy-card", doc)
            self.assertIn("renderProxyQualitySummary", doc)
            self.assertIn("badge-code", doc)
            self.assertIn("/api/proxies/test", doc)
            self.assertIn("/api/proxies/quality-check", doc)
            self.assertNotIn('id="modalProxy"', doc)
            self.assertNotIn("代理 URL", doc)


    def test_provider_type_can_be_inferred_from_model(self) -> None:
        self.assertEqual(resolve_model_provider_type("agnes-image-2.1-flash", "openai"), "agnes")
        self.assertEqual(resolve_model_provider_type("grok-imagine-image", "openai"), "grok")
        self.assertEqual(resolve_model_provider_type("nai-diffusion-4-5-full", "openai"), "novelai")
        self.assertEqual(resolve_model_provider_type("unknown-model", "gemini_openai"), "gemini_openai")
        # protocol_lock keeps channel protocol for gemini-like names
        self.assertEqual(
            resolve_model_provider_type("gemini-2.5-flash-image", "openai", protocol_lock=True),
            "openai",
        )
        self.assertEqual(
            resolve_model_provider_type("gemini-2.5-flash-image", "openai", "gemini", protocol_lock=True),
            "gemini",
        )
        # strong natives still resolve under openai channel lock
        self.assertEqual(
            resolve_model_provider_type("grok-imagine-image", "openai", protocol_lock=True),
            "grok",
        )
        self.assertEqual(
            resolve_model_provider_type("nai-diffusion-4-5-full", "openai", protocol_lock=True),
            "novelai",
        )
        self.assertEqual(
            resolve_model_provider_type("agnes-image-2.1-flash", "openai", protocol_lock=True),
            "agnes",
        )
        # accidental mpt=openai (same as channel) must not pin grok to OpenAI adapter
        self.assertEqual(
            resolve_model_provider_type("grok-imagine-image", "openai", "openai", protocol_lock=True),
            "grok",
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
                        "enabled_models": [
                            "gemini-2.5-flash-image",
                            "gpt-image-2",
                            "grok-imagine-image",
                            "nai-diffusion-4-5-full",
                            "agnes-image-2.1-flash",
                        ],
                        "model_provider_types": {
                            # accidental same-as-channel pin must not stick for strong natives
                            "grok-imagine-image": "openai",
                        },
                    }
                ]
            }
        )
        targets = {t.model: t.provider_type for t in config.get_prioritized_targets()}
        self.assertEqual(targets["gemini-2.5-flash-image"], "openai")
        self.assertEqual(targets["gpt-image-2"], "openai")
        self.assertEqual(targets["grok-imagine-image"], "grok")
        self.assertEqual(targets["nai-diffusion-4-5-full"], "novelai")
        self.assertEqual(targets["agnes-image-2.1-flash"], "agnes")

    def test_grok_edit_payload_uses_data_url_image(self) -> None:
        adapter = GrokImageAdapter(make_target("grok", "grok-imagine-image"), FakeSession())
        payload = adapter.build_edit_payload(
            ImageGenerateRequest(
                prompt="keep identity",
                images=[ImageReference(data=PNG_BYTES, mime_type="image/png")],
            )
        )
        self.assertEqual(payload["model"], "grok-imagine-image-edit")
        self.assertIn("image", payload)
        self.assertTrue(str(payload["image"]["url"]).startswith("data:image/png;base64,"))

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

    def test_safety_advances_to_next_target(self) -> None:
        from astrbot_plugin_selfie_image.generator import _should_advance_to_next_target
        self.assertTrue(_should_advance_to_next_target({"category": "safety"}))
        self.assertTrue(_should_advance_to_next_target({"category": "param"}))
        self.assertTrue(_should_advance_to_next_target({"category": "timeout"}))

    def test_use_logo_when_no_persona_default_true(self) -> None:
        cfg = AICatConfig.from_dict({})
        self.assertTrue(cfg.image_use_logo_when_no_persona)
        cfg2 = AICatConfig.from_dict({"image": {"use_logo_when_no_persona": False}})
        self.assertFalse(cfg2.image_use_logo_when_no_persona)
        self.assertTrue(DEFAULT_CONFIG["image"]["use_logo_when_no_persona"])

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
        self.assertTrue(
            is_transport_profile_switch_error(
                "Response payload is not completed: <TransferEncodingError: 400, message='Not enough data to satisfy transfer length header.'>. ConnectionResetError(104, 'Connection reset by peer')"
            )
        )
        self.assertEqual(
            classify_generation_error("上游响应未完整接收: Connection reset by peer")["category"],
            "network",
        )
        zh_safety = classify_generation_error(
            "HTTP 400: 您的请求无法用于生成图像。该请求可能因安全政策被拦截，或不适合进行图像生成。"
        )
        self.assertEqual(zh_safety["category"], "safety")
        self.assertIn("安全", zh_safety["user_message"])
        from astrbot_plugin_selfie_image.error_classify import summarize_generation_failures

        summary = summarize_generation_failures(
            [
                {
                    "attempt": 1,
                    "label": "自建聚合/gpt-image-2",
                    "success": False,
                    "error": "HTTP 400: 您的请求无法用于生成图像。该请求可能因安全政策被拦截，或不适合进行图像生成。",
                    "error_category": "safety",
                },
                {
                    "attempt": 2,
                    "label": "自建聚合/grok-imagine-image-quality",
                    "success": False,
                    "error": "HTTP 400: Generated image rejected by content moderation.",
                    "error_category": "safety",
                },
            ]
        )
        self.assertEqual(summary["last_failed_model"], "自建聚合/grok-imagine-image-quality")
        self.assertIn("内容未通过上游安全策略", summary["failure_reason"])
        self.assertEqual(len(summary["failure_reasons"]), 2)

        # Success path should still keep intermediate failure rows for monitor detail.
        success_summary = summarize_generation_failures(
            [
                {
                    "attempt": 1,
                    "label": "A/model-a",
                    "success": False,
                    "error": "HTTP 400: Generated image rejected by content moderation.",
                },
                {
                    "attempt": 2,
                    "label": "B/model-b",
                    "success": True,
                },
            ]
        )
        self.assertEqual(len(success_summary["failure_reasons"]), 1)
        self.assertIn("A/model-a", success_summary["failure_reasons"][0]["label"])
        self.assertIn("内容未通过上游安全策略", success_summary["failure_reasons"][0]["error_user_message"])


    def test_compact_and_summarize_generation_records(self) -> None:
        from astrbot_plugin_selfie_image.utils import compact_generation_record, summarize_record_for_list

        fat = {
            "success": False,
            "prompt": "P" * 9000,
            "original_prompt": "O" * 2000,
            "request_prompt": "R" * 9000,
            "error": "boom",
            "request_data": {
                "original_prompt": "dup",
                "request_prompt": "dup2",
                "aspect_ratio": "1:1",
                "resolution": "1K",
                "reference_image_count": 1,
                "targets": ["m1"],
                "request_image_paths": ["a.png"],
            },
            "response_data": {
                "success": False,
                "stage": "generate",
                "error": "e",
                "attempts": [{"label": "m1", "success": False, "error": "x" * 2000}],
            },
            "attempts": [{"label": "m1", "success": False, "error": "x" * 2000, "error_category": "safety"}],
        }
        slim = compact_generation_record(fat)
        self.assertLessEqual(len(slim["prompt"]), 6001)
        self.assertNotIn("original_prompt", slim.get("request_data") or {})
        self.assertLessEqual(len(slim["attempts"][0]["error"]), 801)
        row = summarize_record_for_list(slim)
        self.assertTrue(row.get("has_detail"))
        self.assertEqual(row.get("failed_attempt_count"), 1)
        self.assertNotIn("request_data", row)
        self.assertLessEqual(len(row.get("request_prompt") or ""), 241)

    def test_record_task_splits_multi_image_rows(self) -> None:
        from astrbot_plugin_selfie_image.utils import split_generation_record_images

        pieces = split_generation_record_images(
            {
                "success": True,
                "source": "command-look-cos",
                "prompt": "cos",
                "generated_image_paths": ["a.png", "b.png", "c.png"],
                "response_data": {"success": True, "count": 3, "generated_image_paths": ["a.png", "b.png", "c.png"]},
                "count": 3,
                "id": "keep-out",
            }
        )
        self.assertEqual(len(pieces), 3)
        self.assertEqual(sorted(p["generated_image_paths"][0] for p in pieces), ["a.png", "b.png", "c.png"])
        for row in pieces:
            self.assertEqual(row["count"], 1)
            self.assertEqual(len(row["generated_image_paths"]), 1)
            self.assertEqual(row["response_data"]["count"], 1)
            self.assertEqual(len(row["response_data"]["generated_image_paths"]), 1)
            self.assertNotIn("id", row)
        single = split_generation_record_images({"success": True, "generated_image_paths": ["only.png"], "count": 9})
        self.assertEqual(len(single), 1)
        self.assertEqual(single[0]["count"], 1)
        self.assertEqual(single[0]["generated_image_paths"], ["only.png"])

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

    def test_empty_priority_uses_channel_enable_order(self) -> None:
        config = AICatConfig.from_dict(
            {
                "image_channels": [
                    {
                        "name": "a",
                        "provider_type": "openai",
                        "base_url": "https://a.test",
                        "enabled_models": ["m1", "m2"],
                        "enabled": True,
                    },
                    {
                        "name": "b",
                        "provider_type": "openai",
                        "base_url": "https://b.test",
                        "enabled_models": ["m3"],
                        "enabled": True,
                    },
                ],
                "enabled_image_model_priority": [],
            }
        )
        self.assertEqual(
            [t.label for t in config.get_prioritized_targets()],
            ["a/m1", "a/m2", "b/m3"],
        )

    def test_random_image_model_chooses_one_priority_model(self) -> None:
        raw = {
            "image_channels": [
                {
                    "name": "a",
                    "provider_type": "openai",
                    "base_url": "https://a.test",
                    "enabled_models": ["m1", "m2"],
                    "enabled": True,
                },
                {
                    "name": "b",
                    "provider_type": "openai",
                    "base_url": "https://b.test",
                    "enabled_models": ["m3"],
                    "enabled": True,
                },
            ],
            "enabled_image_model_priority": ["b/m3", "a/m1"],
            "image_model_call_mode": "random",
        }
        config = AICatConfig.from_dict(raw)
        self.assertEqual(config.image_model_call_mode, "random")
        self.assertEqual(config.enabled_image_model_priority, ["b/m3", "a/m1"])
        labels = [t.label for t in config.get_prioritized_targets()]
        self.assertEqual(len(labels), 1)
        self.assertIn(labels[0], {"a/m1", "b/m3"})
        raw["image_model_call_mode"] = "sequence"
        ordered = AICatConfig.from_dict(raw).get_prioritized_targets()
        self.assertEqual([t.label for t in ordered], ["b/m3", "a/m1"])

    def test_random_image_model_uses_enabled_models_without_priority(self) -> None:
        config = AICatConfig.from_dict({
            "image_model_call_mode": "random",
            "image_channels": [{"name": "a", "provider_type": "openai", "base_url": "https://a.test", "enabled_models": ["m1", "m2"]}],
        })
        labels = [target.label for target in config.get_prioritized_targets()]
        self.assertEqual(len(labels), 1)
        self.assertIn(labels[0], {"a/m1", "a/m2"})

    def test_fixed_image_model_uses_priority_one_or_first_enabled(self) -> None:
        raw = {
            "image_model_call_mode": "fixed",
            "image_channels": [{"name": "a", "provider_type": "openai", "base_url": "https://a.test", "enabled_models": ["m1", "m2"]}],
            "enabled_image_model_priority": ["a/m2", "a/m1"],
        }
        self.assertEqual([t.label for t in AICatConfig.from_dict(raw).get_prioritized_targets()], ["a/m2"])
        raw["enabled_image_model_priority"] = []
        self.assertEqual([t.label for t in AICatConfig.from_dict(raw).get_prioritized_targets()], ["a/m1"])

    def test_image_model_call_mode_defaults_and_migrates_legacy_random(self) -> None:
        self.assertEqual(AICatConfig.from_dict({}).image_model_call_mode, "sequence")
        migrated = AICatConfig.from_dict({"random_image_model": True})
        self.assertEqual(migrated.image_model_call_mode, "random")
        self.assertEqual(migrated.raw.get("image_model_call_mode"), "random")
        self.assertNotIn("random_image_model", migrated.raw)

    def test_web_image_model_call_mode_is_select(self) -> None:
        self.assertIn('id="imageModelCallMode"', INDEX_HTML)
        self.assertIn('<option value="sequence">顺序</option>', INDEX_HTML)
        self.assertIn('<option value="random">随机</option>', INDEX_HTML)
        self.assertIn('<option value="fixed">固定</option>', INDEX_HTML)
        self.assertIn("CONFIG.image_model_call_mode", INDEX_HTML)
        self.assertNotIn('id="randomImageModel"', INDEX_HTML)
        self.assertNotIn("isRandomImageModel", INDEX_HTML)


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
        self.assertIn("api(monitorQueryPath(MONITOR_PAGE))", INDEX_HTML)

    def test_monitor_separates_final_model_from_retry_chain(self) -> None:
        for token in (
            "function recordFinalModelBadge",
            "function recordRetryModelChain",
            "model-retry-arrow",
            "recordFinalModelBadge(r)",
            "recordRetryModelChain(r)",
        ):
            self.assertIn(token, INDEX_HTML)
        self.assertNotIn("<td>${recordModelBadges(r)}</td>", INDEX_HTML)

        self.assertIn("RECORD_META.filtered", INDEX_HTML)

    def test_web_prunes_invalid_model_priority_before_save(self) -> None:
        self.assertIn("function prunePriorityList", INDEX_HTML)
        self.assertIn("for (const kind of ['image','audit','video']) prunePriorityList(kind);", INDEX_HTML)
        self.assertIn("CONFIG.enabled_image_model_priority = textList('priorityList');", INDEX_HTML)
        self.assertIn("CONFIG.enabled_audit_model_priority = textList('auditPriorityList');", INDEX_HTML)
        self.assertIn("CONFIG.enabled_video_model_priority = textList('videoPriorityList');", INDEX_HTML)

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

    def test_novelai_size_and_official_payload(self) -> None:
        self.assertEqual(map_aspect_ratio_to_nai_size("9:16"), (832, 1216))
        self.assertEqual(map_aspect_ratio_to_nai_size("16:9"), (1216, 832))
        self.assertEqual(map_aspect_ratio_to_nai_gateway_size("9:16"), "竖图")
        self.assertEqual(map_aspect_ratio_to_nai_gateway_size("1:1", "2K"), "2K方图")
        target = make_target("novelai", "nai-diffusion-4-5-full")
        target.base_url = "https://api.novelai.net"
        target.api_key = "tok"
        adapter = NovelAIImageAdapter(target, FakeSession())
        self.assertEqual(adapter._mode(), "official")
        payload = adapter.build_official_payload(ImageGenerateRequest(prompt="1girl, solo", aspect_ratio="9:16"))
        self.assertEqual(payload["model"], "nai-diffusion-4-5-full")
        self.assertEqual(payload["action"], "generate")
        self.assertEqual(payload["parameters"]["width"], 832)
        self.assertEqual(payload["parameters"]["height"], 1216)
        self.assertIn("v4_prompt", payload["parameters"])
        # gateway mode
        gw = make_target("novelai", "nai-diffusion-4-5-full")
        gw.base_url = "https://nai.sta1n.cn"
        gw.api_key = "tok"
        gw_adapter = NovelAIImageAdapter(gw, FakeSession())
        self.assertEqual(gw_adapter._mode(), "gateway")
        params = gw_adapter.build_gateway_params(ImageGenerateRequest(prompt="cat", aspect_ratio="1:1"))
        self.assertEqual(params["size"], "方图")
        self.assertEqual(params["token"], "tok")
        self.assertEqual(params["tag"], "cat")

    async def test_novelai_official_parses_zip_response(self) -> None:
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("image_0.png", PNG_BYTES)
        body = buf.getvalue()
        target = make_target("novelai", "nai-diffusion-4-5-full")
        target.base_url = "https://api.novelai.net"
        target.api_key = "tok"

        class RawResp:
            status = 200
            headers = {"Content-Type": "application/zip"}

            async def read(self):
                return body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class RawSession:
            def post(self, *a, **k):
                return RawResp()

            def get(self, *a, **k):
                return RawResp()

        adapter = NovelAIImageAdapter(target, RawSession())  # type: ignore
        result = await adapter.generate(ImageGenerateRequest(prompt="1girl"))
        self.assertEqual(result.images, [PNG_BYTES])
        self.assertFalse(result.error)

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

    async def test_video_fallback_tries_next_target_after_failure(self) -> None:
        first = make_target("openai", "bad-video")
        second = make_target("openai", "good-video")
        calls = []

        async def fake_generate(target, request, session, *, save_dir):
            calls.append(target.model)
            if target.model == "bad-video":
                return VideoGenerateResult(
                    error="temporary video failure",
                    used_model=target.label,
                    attempts=[{"label": target.label, "success": False, "error_category": "network"}],
                )
            return VideoGenerateResult(
                video_path="/tmp/good.mp4",
                used_model=target.label,
                attempts=[{"label": target.label, "success": True}],
            )

        with patch("astrbot_plugin_selfie_image.video.generate_video_openai_compatible", side_effect=fake_generate):
            result = await generate_video_with_fallback(
                [first, second],
                VideoGenerateRequest(prompt="move naturally"),
                object(),
                save_dir=tempfile.gettempdir(),
            )

        self.assertEqual(calls, ["bad-video", "good-video"])
        self.assertEqual(result.used_model, second.label)
        self.assertEqual([item["success"] for item in result.attempts], [False, True])

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

        # Auth on first target is skipped; second target should still run.
        self.assertEqual(result.images, [PNG_BYTES])
        self.assertEqual(calls["n"], 2)
        self.assertEqual(result.used_model, second.label)
        self.assertEqual(result.attempts[0].get("error_category"), "auth")
        self.assertTrue(result.attempts[1].get("success"))

    async def test_fallback_advances_on_param_error_to_next_channel(self) -> None:
        first = make_target("openai", "gpt-image-2")
        first.channel_name = "小水管"
        second = make_target("openai", "gpt-image-1")
        second.channel_name = "备用"
        calls = []

        def create_fake_adapter(target, session):
            calls.append(target.label)

            class A:
                async def generate(self, req):
                    if "小水管" in target.label:
                        return ImageGenerateResult(
                            error="HTTP 400: 请求参数不受当前模型支持 (request id: x)；兼容 image[] 重试也失败: HTTP 400"
                        )
                    return ImageGenerateResult(images=[PNG_BYTES])

            return A()

        async def no_sleep(seconds):
            return None

        with (
            patch("astrbot_plugin_selfie_image.generator.create_adapter", side_effect=create_fake_adapter),
            patch("astrbot_plugin_selfie_image.generator.asyncio.sleep", side_effect=no_sleep),
        ):
            result = await generate_image_with_fallback(
                [first, second],
                ImageGenerateRequest(prompt="cat", images=[ImageReference(data=PNG_BYTES, mime_type="image/png")]),
                FakeSession(),
                max_attempts=2,
            )

        self.assertEqual(result.images, [PNG_BYTES])
        self.assertEqual(result.used_model, second.label)
        self.assertEqual(result.attempts[0].get("error_category"), "param")
        self.assertTrue(result.attempts[1].get("success"))
        self.assertEqual(len(calls), 2)

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
        self.assertEqual(plugin.config.image_max_batch_count, 20)

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

        response = client.post(
            "/api/config",
            json={
                "config": {
                    "image_channels": [
                        {
                            "name": "main",
                            "provider_type": "openai",
                            "base_url": "https://example.test",
                            "model": "gpt-image-1",
                            "enabled_models": ["gpt-image-1", "gpt-image-2"],
                            "enabled": False,
                        }
                    ],
                }
            },
            headers=headers,
        )
        data = response.get_json()["data"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["image_channels"][0]["enabled"], False)
        self.assertEqual(plugin.config.image_channels[0].enabled, False)
        self.assertEqual(plugin.config.get_prioritized_targets(), [])

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
            def get_recent_records(self, *args, **kwargs):
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
            def get_recent_records(self, *args, **kwargs):
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
        self.assertIn("缓存文件不是有效图片或视频", response.get_json()["error"])

    def test_video_task_route_marks_payload_and_serves_video(self) -> None:
        plugin = FakeWebPlugin("secret")
        client = self.make_client(plugin, host="0.0.0.0")
        headers = {"X-Selfie-Image-Token": "secret"}

        response = client.post(
            "/api/test-video-channel/tasks",
            json={"channel": "video", "model": "v1", "prompt": "waves"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        request_data = response.get_json()["data"]["request_data"]
        self.assertEqual(request_data["media_type"], "video")

        video_path = os.path.join(plugin.generated_dir, "clip.mp4")
        video_bytes = b"\x00\x00\x00\x20ftypisom" + b"video-data"
        Path(video_path).write_bytes(video_bytes)
        response = client.get("/api/cache-image?path=clip.mp4", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, video_bytes)
        response.close()


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
        self.assertIn("PENDING_DELETE", self.html)
        self.assertIn("再点一次「确认删除」才会删除渠道", self.html)
        self.assertNotIn("confirm('确认删除这个渠道？')", self.html)
        self.assertNotIn("confirm('确认删除这个代理？", self.html)
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
        self.assertNotIn("_generate_help_poster", main_src)
        self.assertIn("help_poster.png", main_src)
        self.assertIn("_bundled_help_poster_path", main_src)
        self.assertIn("async def cmd_help", main_src)
        self.assertIn('@filter.command("生图help")', main_src)
        self.assertIn("async def cmd_help_text", main_src)
        # image-only path must not auto-append full help text in cmd_help body
        help_fn = main_src.split("async def cmd_help", 1)[1].split("async def cmd_help_text", 1)[0]
        self.assertNotIn("_help_text_body()", help_fn)
        self.assertIn("chain_result", help_fn)

    def test_command_docstrings_for_plugin_panel_descriptions(self) -> None:
        main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        required = {
            "cmd_draw": "不会自动带入形象图",
            "cmd_raw_text_to_image": "不使用形象图",
            "cmd_raw_image_to_image": "不会自动使用形象图",
            "cmd_video": "当前形象图作首帧",
            "cmd_t2v": "也不使用形象图",
            "cmd_i2v": "用当前形象图",
            "cmd_selfie": "用当前形象自拍",
            "cmd_group_selfie": "自己使用当前形象",
            "cmd_persona_set": "自动 / 真人 / 动漫",
        }
        for name, token in required.items():
            block = main_src.split(f"async def {name}", 1)[1].split("async def ", 1)[0]
            self.assertIn('"""', block, name)
            self.assertIn(token, block, name)
            # no developer jargon in player-facing command descriptions
            self.assertNotIn("passthrough", block)
            doc = block.lower().split('"""', 2)[1] if '"""' in block else ""
            self.assertNotIn("fallback", doc)
        help_body = main_src.split("def _help_text_body", 1)[1].split("def _resolve_help_image_path", 1)[0]
        self.assertIn("自动判断", help_body)
        self.assertIn("不会自动塞形象图", help_body)
        self.assertIn("有图=图生视频", help_body)
        self.assertIn("/画 3", help_body)
        self.assertIn("/自拍 3", help_body)
        self.assertIn("一张一张", help_body)
        llm_selfie = main_src.split("async def _run_llm_selfie_flow", 1)[1].split("def _build_success_text", 1)[0]
        self.assertIn("_background_selfie_batches", llm_selfie)
        self.assertNotIn("for _ in range(requested_count)", llm_selfie)
        llm_image = main_src.split("async def tool_generate_image", 1)[1].split("async def tool_generate_selfie", 1)[0]
        self.assertIn("_background_draw_batches", llm_image)
        selfie_batch = main_src.split("async def _background_selfie_batches", 1)[1].split("def _validate_web_test_selection", 1)[0]
        self.assertIn("for index in range(total)", selfie_batch)
        self.assertNotIn("_run_generation_jobs_parallel", main_src)

    def test_anatomy_constraints_ban_third_limb_and_same_side_pairs(self) -> None:
        from astrbot_plugin_selfie_image.persona import PersonaManager, anatomy_constraint_lines

        lines = "\n".join(anatomy_constraint_lines(style="general"))
        for token in ("左右手/脚各一", "肩肘腕连续连接", "单人限定"):
            self.assertIn(token, lines)
        self.assertNotIn("同框", lines)
        for banned in ("断臂", "幽灵手", "残缺", "severed", "ghost hands", "stump"):
            self.assertNotIn(banned, lines.lower() if banned.isascii() else lines)
        leg_lines = "\n".join(anatomy_constraint_lines(style="legs"))
        self.assertIn("髋-膝-踝连续", leg_lines)
        self.assertIn("重心稳定", leg_lines)
        self.assertIn("肩肘腕连续", leg_lines)
        self.assertIn("只有主角一人", leg_lines)
        self.assertNotIn("同框", leg_lines)
        self.assertNotIn("断臂", leg_lines)
        self.assertNotIn("幽灵手", leg_lines)
        en = "\n".join(anatomy_constraint_lines(style="en"))
        self.assertIn("one left hand", en)
        self.assertIn("one right hand", en)
        self.assertIn("continuously", en)
        self.assertIn("disconnected hands", en)
        self.assertNotIn("severed", en.lower())
        self.assertNotIn("ghost", en.lower())
        self.assertNotIn("stump", en.lower())

        with tempfile.TemporaryDirectory() as tmp:
            manager = PersonaManager(tmp)
            selfie = manager.build_selfie_prompt("自拍", "小助", "温柔", True, 0)
            self.assertIn("左右手/脚各一", selfie)
            self.assertIn("肩肘腕连续连接", selfie)
            self.assertNotIn("幽灵手", selfie)
            self.assertNotIn("断臂", selfie)
            legs = manager.build_selfie_prompt("看看腿", "小助", "温柔", True, 0)
            self.assertIn("脚部画外", legs)
            self.assertIn("双脚裁出画外", legs)
            self.assertIn("不露脸", legs)
            self.assertIn("晒腿", legs)
            self.assertIn("髋到膝", legs)
            self.assertIn("禁止膝盖顶脸", legs)
            self.assertNotIn("【合影 / 同框模式】", legs)
            self.assertNotIn("幽灵手", legs)
            self.assertNotIn("勒进大腿肉", legs)
            self.assertNotIn("不要大象腿猪腿", legs)
            self.assertNotIn("微胖软肉", legs)
            self.assertNotIn("赤足", legs)
            self.assertNotIn("碰脚", legs)
            self.assertNotIn("脚趾自然清晰", legs)
            group = manager.build_selfie_prompt("合影", "小助", "温柔", True, 1)
            self.assertIn("手与胳膊连续连接", group)

        if "astrbot" not in sys.modules:
            astrbot = types.ModuleType("astrbot")
            api = types.ModuleType("astrbot.api")
            star = types.ModuleType("astrbot.api.star")
            event = types.ModuleType("astrbot.api.event")
            comps = types.ModuleType("astrbot.api.message_components")
            star.Context = object
            star.Star = object
            star.register = lambda *a, **k: (lambda cls: cls)
            class filter:
                @staticmethod
                def command(*a, **k):
                    return lambda f: f
                class PermissionType:
                    ADMIN = "admin"
                    MEMBER = "member"
                @staticmethod
                def permission_type(*a, **k):
                    return lambda f: f
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

        wrapped_zh = plugin_main.append_anatomy_constraints("女孩站立")
        self.assertIn("构图与画面质量", wrapped_zh)
        self.assertNotIn("Composition and quality", wrapped_zh)
        wrapped = plugin_main.append_anatomy_constraints("a girl standing", language="en")
        self.assertIn("one left hand", wrapped)
        self.assertIn("disconnected hands", wrapped)
        self.assertNotIn("severed", wrapped.lower())
        self.assertNotIn("ghost", wrapped.lower())
        ref = ImageReference(data=PNG_BYTES, mime_type="image/png")
        bare = plugin_main.build_prompt_with_reference_instruction("女孩站立", [])
        self.assertEqual(bare, "女孩站立")
        self.assertNotIn("构图与画面质量", bare)
        self.assertNotIn("一只左手", bare)
        ref_zh = plugin_main.build_prompt_with_reference_instruction("把衣服改成蓝色", [ref])
        self.assertIn("使用提供的参考图", ref_zh)
        self.assertIn("用户要求：", ref_zh)
        self.assertNotIn("Use the provided", ref_zh)
        self.assertNotIn("一只左手", ref_zh)
        self.assertNotIn("来源不清", ref_zh)
        ref_en = plugin_main.build_prompt_with_reference_instruction("change the outfit to blue", [ref], language="en")
        self.assertIn("Use the provided", ref_en)
        self.assertIn("User request:", ref_en)
        self.assertNotIn("one left hand", ref_en)
        enhanced = plugin_main.build_prompt_with_reference_instruction("女孩站立", [], enhance=True)
        self.assertIn("构图与画面质量", enhanced)
        self.assertIn("左右手/脚各一", enhanced)
        legs_action = plugin_main.SelfieImagePlugin._build_leg_focus_action(object.__new__(plugin_main.SelfieImagePlugin), "", False)
        self.assertIn("连续", legs_action)
        self.assertIn("单人", legs_action)
        self.assertNotIn("同框", legs_action)
        self.assertNotIn("幽灵手", legs_action)
        self.assertNotIn("断臂", legs_action)
        self.assertNotIn("勒进大腿肉", legs_action)
        self.assertNotIn("不要大象腿猪腿", legs_action)
        self.assertNotIn("微胖软肉", legs_action)
        # full pipeline: leg action must stay legs-only, never group
        with tempfile.TemporaryDirectory() as tmp:
            manager = PersonaManager(tmp)
            intent = manager.analyze_selfie_intent(legs_action)
            self.assertTrue(intent.is_legs_only)
            self.assertFalse(intent.is_group_photo)
            prompt = manager.build_selfie_prompt(legs_action, "小助", "温柔", True, 0)
            self.assertIn("晒腿模式", prompt)
            self.assertNotIn("勒进大腿肉", prompt)
            self.assertNotIn("微胖软肉", prompt)
            self.assertNotIn("不要大象腿猪腿", prompt)
            self.assertNotIn("【合影 / 同框模式】", prompt)
            self.assertIn("只有主角一人", prompt)

        # /看看COS must not be hijacked into 晒腿 by 高叉/与腿 wording
        class _P:
            pass
        cos_action = plugin_main.SelfieImagePlugin._build_cos_look_action(_P(), "", False)
        with tempfile.TemporaryDirectory() as tmp:
            manager = PersonaManager(tmp)
            cos_intent = manager.analyze_selfie_intent(cos_action)
            self.assertFalse(cos_intent.is_legs_only, cos_action)
            self.assertTrue(getattr(cos_intent, "is_cos_look", False), cos_action)
            self.assertFalse(cos_intent.is_group_photo)
            self.assertFalse(cos_intent.is_third_person_photo)
            shaosiyuan = next(x for x in plugin_main.COS_LOOK_SETS if x["id"] == "shaosiyuan_red")
            forced = plugin_main.SelfieImagePlugin._build_cos_look_action(_P(), shaosiyuan["prompt"], False)
            forced_intent = manager.analyze_selfie_intent(forced)
            self.assertFalse(forced_intent.is_legs_only, forced)
            self.assertTrue(forced_intent.is_cos_look)
            cos_prompt = manager.build_selfie_prompt(forced, "小助", "温柔", True, 0)
            self.assertNotIn("晒腿模式", cos_prompt)
            self.assertIn("COS换装自拍模式", cos_prompt)
            self.assertIn("对镜", cos_prompt)
            self.assertIn("看看COS", cos_prompt)
            self.assertIn("换装", cos_prompt)
            # auto appearance: no forced real/anime style line
            manager.set_appearance_type("auto")
            auto_prompt = manager.build_selfie_prompt(legs_action, "小助", "温柔", True, 0)
            self.assertNotIn("形象是真人", auto_prompt)
            self.assertNotIn("形象是动漫人物", auto_prompt)
            self.assertNotIn("形象类型：", auto_prompt)

    def test_appearance_type_auto_real_anime_prompt_injection(self) -> None:
        from astrbot_plugin_selfie_image.persona import PersonaManager

        with tempfile.TemporaryDirectory() as tmp:
            manager = PersonaManager(tmp)
            self.assertEqual(manager.get_appearance_type(), "auto")
            auto = manager.build_selfie_prompt(
                action="自拍",
                bot_name="小助",
                personality="温柔",
                has_reference_image=True,
                extra_reference_count=0,
            )
            self.assertNotIn("形象是真人", auto)
            self.assertNotIn("形象是动漫人物", auto)

            manager.set_appearance_type("real")
            self.assertEqual(manager.get_appearance_type(), "real")
            real = manager.build_selfie_prompt(
                action="自拍",
                bot_name="小助",
                personality="温柔",
                has_reference_image=True,
                extra_reference_count=0,
            )
            self.assertIn("形象是真人", real)
            self.assertNotIn("形象是动漫人物", real)

            manager.set_appearance_type("anime")
            self.assertEqual(manager.get_appearance_type(), "anime")
            anime = manager.build_selfie_prompt(
                action="自拍",
                bot_name="小助",
                personality="温柔",
                has_reference_image=True,
                extra_reference_count=0,
            )
            self.assertIn("形象是动漫人物", anime)
            self.assertIn("柔光京阿尼", anime)
            self.assertIn("脸更细", anime)
            self.assertIn("仙风", anime)
            self.assertNotIn("形象是真人", anime)
            self.assertIn("是女性就保持女性", anime)
            self.assertIn("禁止无故改成异性", anime)
            self.assertIn("禁止把女形象改成男", anime)

            # 参考图即使像真人，类型=动漫时仍按动漫形象，不强制写实真人
            group_anime = manager.build_selfie_prompt(
                action="合影",
                bot_name="小助",
                personality="温柔",
                has_reference_image=True,
                extra_reference_count=1,
            )
            self.assertIn("形象是动漫人物", group_anime)
            self.assertIn("柔光京阿尼", group_anime)
            self.assertIn("是女性则主角必须是女性", group_anime)
            self.assertNotIn("默认一律写实真人合影", group_anime)
            self.assertNotIn("必须改画成与主角同一套写实真人照片风格", group_anime)
            self.assertNotIn("同框对象默认都是写实真人", group_anime)

            # invalid falls back to auto and persists
            manager.set_appearance_type("whatever")
            self.assertEqual(manager.get_appearance_type(), "auto")
            reloaded = PersonaManager(tmp)
            self.assertEqual(reloaded.get_appearance_type(), "auto")

    def test_group_action_respects_appearance_type_anime(self) -> None:
        if "astrbot" not in sys.modules:
            astrbot = types.ModuleType("astrbot")
            api = types.ModuleType("astrbot.api")
            star = types.ModuleType("astrbot.api.star")
            event = types.ModuleType("astrbot.api.event")
            comps = types.ModuleType("astrbot.api.message_components")
            star.Context = object
            star.Star = object
            star.register = lambda *a, **k: (lambda cls: cls)
            class filter:
                @staticmethod
                def command(*a, **k):
                    return lambda f: f
                class PermissionType:
                    ADMIN = "admin"
                    MEMBER = "member"
                @staticmethod
                def permission_type(*a, **k):
                    return lambda f: f
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

        stub = object.__new__(plugin_main.SelfieImagePlugin)
        stub.persona = type("P", (), {"get_appearance_type": staticmethod(lambda: "anime")})()
        text = plugin_main.SelfieImagePlugin._build_group_selfie_action(stub, extra_request="", has_refs=True)
        self.assertIn("形象是动漫人物", text)
        self.assertIn("是女性则 AI 必须是女性", text)
        self.assertNotIn("写实真人照片风格", text)
        self.assertNotIn("仅当用户明确要求二次元/动漫合影时，才允许整体二次元画风", text)

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
            self.assertIn("拟人", group)
            self.assertIn("玩偶", group)
            self.assertIn("完整人物", group)
            self.assertIn("粘连融合", group)
            # 默认自动：由模型判断画风，不再强制写实真人
            self.assertNotIn("默认一律写实真人合影", group)
            self.assertIn("默认成年女性", group)
            self.assertTrue(("表情" in group and "重画" in group) or ("表情眼神按本次合影" in group) or ("不要僵住参考图" in group) or ("自然重画" in group))
            # clothes mode: expression not locked to identity ref
            clothes = manager.build_selfie_prompt(
                action="穿上这件衣服自拍",
                bot_name="小助",
                personality="温柔",
                has_reference_image=True,
                extra_reference_count=1,
            )
            self.assertTrue(("表情" in clothes) and (("重画" in clothes) or ("自然" in clothes)))
            self.assertIn("只锁身份长相", clothes)


    def test_legs_persona_uses_only_supported_legwear(self) -> None:
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
            self.assertIn("白丝", text)
            self.assertIn("黑丝", text)
            self.assertIn("不透", text)
            self.assertIn("主要看腿形", text)
            self.assertIn("晒腿模式", text)
            self.assertIn("脚部画外", text)
            self.assertIn("双脚裁出画外", text)
            self.assertIn("不露脸", text)
            self.assertTrue(any(k in text for k in ("袜口", "卷边", "平口", "蝴蝶结", "竖纹")), text)
            for forbidden in ("短袜", "堆堆袜", "过膝袜", "长筒袜", "肉色丝袜", "袜装", "勒进大腿肉", "半透明", "赤足", "碰脚", "脚趾自然清晰", "完整包住脚部", "中筒丝袜"):
                self.assertNotIn(forbidden, text)
            self.assertNotIn("微胖软肉", text)
            self.assertIn("重心稳定", text)
            self.assertNotIn("不要大象腿猪腿", text)
            self.assertNotIn("主姿势在多种日常拍腿姿势间变化", text)
            self.assertNotIn("· 坐姿拍腿", text)
            self.assertNotIn("小皮鞋", text)
            self.assertNotIn("居家拖鞋", text)

    def test_daily_profile_does_not_add_unselected_legwear(self) -> None:
        from astrbot_plugin_selfie_image.persona import fallback_daily_profile

        for _ in range(30):
            outfit = fallback_daily_profile("2026-08-09", "seed").outfit
            for forbidden in ("短袜", "居家袜", "中筒袜", "堆堆袜", "过膝袜"):
                self.assertNotIn(forbidden, outfit)

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

    def test_image_audit_video_priorities_are_independent(self) -> None:
        from astrbot_plugin_selfie_image.models import AICatConfig

        cfg = AICatConfig.from_dict(
            {
                "image_channels": [
                    {"name": "img", "base_url": "https://x", "api_key": "i", "enabled_models": ["i1", "i2"]}
                ],
                "audit_channels": [
                    {"name": "audit", "base_url": "https://x", "api_key": "a", "enabled_models": ["a1", "a2"]}
                ],
                "video_channels": [
                    {"name": "video", "base_url": "https://x", "api_key": "v", "enabled_models": ["v1", "v2"]}
                ],
                "enabled_image_model_priority": ["img/i2"],
                "enabled_audit_model_priority": ["audit/a2"],
                "enabled_video_model_priority": ["video/v2"],
            }
        )
        self.assertEqual(cfg.get_prioritized_targets()[0].label, "img/i2")
        self.assertEqual(cfg.get_audit_targets()[0].label, "audit/a2")
        self.assertEqual(cfg.get_prioritized_video_targets()[0].label, "video/v2")
        self.assertNotEqual(cfg.enabled_image_model_priority, cfg.enabled_audit_model_priority)
        self.assertNotEqual(cfg.enabled_image_model_priority, cfg.enabled_video_model_priority)

    def test_dashboard_separates_priorities_and_has_video_test(self) -> None:
        from astrbot_plugin_selfie_image.web import INDEX_HTML

        for token in (
            "enabled_audit_model_priority",
            "enabled_video_model_priority",
            "auditPriorityRows",
            "videoPriorityRows",
            "testVideoBtn",
            "/api/test-video-channel/tasks",
            "generated_video_paths",
            "记录类型",
        ):
            self.assertIn(token, INDEX_HTML)

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
        self.assertEqual(normalize_video_provider_type("grok"), "grok")
        self.assertEqual(normalize_video_provider_type("grok_video"), "grok")
        self.assertEqual(normalize_video_provider_type("xai"), "grok")
        self.assertEqual(normalize_video_provider_type("openai_sync"), "video_sync")
        self.assertEqual(normalize_video_provider_type("openai_chat"), "video_chat")
        self.assertEqual(normalize_video_provider_type("openai"), "")  # image protocol
        self.assertEqual(infer_video_provider_type_from_model("sora-2"), "sora")
        self.assertEqual(infer_video_provider_type_from_model("veo-3.1"), "veo")
        self.assertEqual(infer_video_provider_type_from_model("doubao-seedance-1.0"), "seedance")
        self.assertEqual(infer_video_provider_type_from_model("agnes-video-pro"), "agnes")
        self.assertEqual(infer_video_provider_type_from_model("kling-v2"), "kling")
        self.assertEqual(infer_video_provider_type_from_model("grok-imagine-video"), "grok")
        self.assertEqual(infer_video_provider_type_from_model("grok-imagine-video-1.5"), "grok")
        self.assertEqual(
            resolve_video_model_provider_type("unknown", "video_sync", ""),
            "video_sync",
        )
        self.assertEqual(
            resolve_video_model_provider_type("x", "openai_video", "sora"),
            "sora",
        )


    def test_agnes_official_endpoints_and_frames(self) -> None:
        from types import SimpleNamespace
        from astrbot_plugin_selfie_image.video import (
            VideoGenerateRequest,
            _agnes_payload,
            _extract_video_url,
            agnes_num_frames_for_duration,
            agnes_size_wh,
            build_agnes_result_url,
            build_agnes_videos_endpoint,
        )

        self.assertTrue(build_agnes_videos_endpoint("https://apihub.agnes-ai.com").endswith("/v1/videos"))
        self.assertTrue(build_agnes_videos_endpoint("https://apihub.agnes-ai.com/v1").endswith("/v1/videos"))
        poll = build_agnes_result_url("https://apihub.agnes-ai.com/v1", "task_abc", model="agnes-video-v2.0")
        self.assertIn("/agnesapi?video_id=task_abc", poll)
        self.assertIn("model_name=agnes-video-v2.0", poll)
        frames = agnes_num_frames_for_duration(5, 24)
        self.assertEqual(frames, 121)
        self.assertEqual((frames - 1) % 8, 0)
        self.assertLessEqual(agnes_num_frames_for_duration(99, 24), 441)
        self.assertEqual(agnes_size_wh("16:9"), (1152, 768))
        self.assertEqual(agnes_size_wh("9:16"), (768, 1152))
        completed = {"status": "completed", "metadata": {"url": "https://cdn.example/a.mp4"}}
        self.assertEqual(_extract_video_url(completed), "https://cdn.example/a.mp4")
        target = SimpleNamespace(model="agnes-video-v2.0")
        payload = _agnes_payload(target, VideoGenerateRequest(prompt="cat on beach", duration=5, size="16:9"), [])
        self.assertEqual(payload["model"], "agnes-video-v2.0")
        self.assertEqual(payload["num_frames"], 121)
        self.assertEqual(payload["frame_rate"], 24)
        self.assertEqual(payload["width"], 1152)
        self.assertEqual(payload["height"], 768)
        self.assertIn("cat on beach", payload["prompt"])


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

    def test_video_proxy_covers_polling_and_download(self) -> None:
        video_src = (Path(__file__).resolve().parents[1] / "video.py").read_text(encoding="utf-8")
        for token in (
            "async def _download_video_bytes(session: aiohttp.ClientSession, url: str, timeout: int, proxy: str = \"\")",
            "proxy=proxy or None",
            "return await _poll_task(session, poll_url=poll_url, headers=headers, timeout_seconds=timeout, proxy=target.proxy)",
            "proxy=target.proxy",
        ):
            self.assertIn(token, video_src)

    def test_new_image_channel_shows_channel_type_selector(self) -> None:
        for html in (INDEX_HTML, (Path(__file__).resolve().parents[1] / "pages/dashboard/index.html").read_text(encoding="utf-8")):
            self.assertIn("EDITING_CHANNEL_KIND === 'image' || EDITING_CHANNEL_KIND === 'video'", html)

    def test_video_uses_persona_first_frame_across_entry_points(self) -> None:
        main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        for token in (
            "def _video_persona_reference",
            "@LLM_TOOL(name=\"generate_video\")",
            "tool_generate_video",
            "persona_ref = self._video_persona_reference()",
            "refs = [persona_ref]",
            "if payload.get(\"use_selfie_reference\") and not refs:",
            "valid_targets: List[ImageModelTarget] = []",
            "targets = valid_targets",
        ):
            self.assertIn(token, main_src)
        for html in (INDEX_HTML, (Path(__file__).resolve().parents[1] / "pages/dashboard/index.html").read_text(encoding="utf-8")):
            self.assertIn("TEST_MODE !== 't2v'", html)
            self.assertIn("use_selfie_reference: TEST_MODE !== 't2v'", html)
            self.assertIn("无首帧时使用当前形象参考", html)


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
        legwear_by_pose = {}
        for _ in range(180):
            t = plugin_main.SelfieImagePlugin._build_leg_focus_action(_P(), "", False)
            self.assertIn("近大远小", t)
            self.assertNotIn("小皮鞋", t)
            self.assertNotIn("居家拖鞋", t)
            self.assertNotIn("鞋面可入镜", t)
            for forbidden in ("短袜", "堆堆袜", "过膝袜", "长筒袜", "肉色丝袜", "极薄肉色", "袜装"):
                self.assertNotIn(forbidden, t)
            selected = [name for name in ("光腿神器", "白丝", "黑丝") if f"本次腿部穿搭：{name}" in t]
            self.assertEqual(len(selected), 1, t)
            m = re.search(r"【pose:([a-z_]+)】", t)
            if m:
                pose = m.group(1)
                found.add(pose)
                legwear_by_pose.setdefault(pose, set()).update(selected)
                # Look-legs always crops feet now — all samples must hide feet.
                self.assertIn("双脚完整裁出画外", t)
                self.assertIn("【crop:calves】", t)
                self.assertIn("不露脸", t)
                self.assertNotIn("脚趾五个分开", t)
                self.assertNotIn("丝袜必须包住整只脚到脚趾", t)
                self.assertNotIn("脚部自然完整", t)
                self.assertNotIn("脚趾自然清晰", t)
        # calf-crop families only (no full-foot; no cross/hug — high deformity on gpt-image)
        for key in ("sit_crop", "kneel_crop", "side_lie_crop", "windowsill_crop", "reclined_knees_crop"):
            self.assertTrue(any(p == key for p in found), f"missing pose family {key} in {found}")
        self.assertFalse(any(p in found for p in ("cross_leg_crop", "hug_knee_crop")), f"retired poses leaked: {found}")
        self.assertTrue(all(p.endswith("_crop") for p in found), f"non-crop poses leaked: {found}")
        forced_crop = None
        with patch("astrbot_plugin_selfie_image.main.random.choices", side_effect=[["sit_crop"], ["白丝"]]):
            forced_crop = plugin_main.SelfieImagePlugin._build_leg_focus_action(_P(), "", False)
        self.assertIn("【pose:sit_crop】", forced_crop)
        self.assertIn("【crop:calves】", forced_crop)
        self.assertTrue(any(k in forced_crop for k in ("卷边", "平口", "袜口", "蝴蝶结", "竖纹")), forced_crop)
        self.assertIn("小腿", forced_crop)
        self.assertIn("画外", forced_crop)
        self.assertNotIn("脚趾五个分开", forced_crop)
        self.assertNotIn("丝袜必须包住整只脚到脚趾", forced_crop)
        from astrbot_plugin_selfie_image.persona import PersonaManager
        from astrbot_plugin_selfie_image.prompt_templates import build_selfie_builtin_prompt
        with tempfile.TemporaryDirectory() as tmp:
            final_crop = PersonaManager(tmp).build_selfie_prompt(forced_crop, "小助", "温柔", True, 0)
        self.assertIn("脚部画外", final_crop)
        self.assertNotIn("换装要求", final_crop)
        self.assertTrue(("双脚完整裁出画外" in final_crop) or ("双脚裁出画外" in final_crop), final_crop)
        self.assertTrue(any(k in final_crop for k in ("袜口", "卷边", "平口", "蝴蝶结", "竖纹")), final_crop)
        for conflict in ("脚趾五个分开", "身体从入镜部位连续到脚", "包住整脚到脚趾", "勒进大腿肉"):
            self.assertNotIn(conflict, final_crop)
        self.assertNotIn("微胖软肉", final_crop)
        self.assertNotIn("不要大象腿猪腿", final_crop)
        final_crop_en = build_selfie_builtin_prompt(forced_crop, language="en", has_reference_image=True)
        self.assertIn("crop both ankles and feet fully outside the frame", final_crop_en)
        self.assertNotIn("stockings cover the whole foot", final_crop_en)
        for pose, choices in legwear_by_pose.items():
            self.assertTrue(choices <= {"光腿神器", "白丝", "黑丝"}, (pose, choices))
        filtered = plugin_main.SelfieImagePlugin._build_leg_focus_action(_P(), "短袜 过膝袜 肉丝 清晨", False)
        self.assertIn("清晨", filtered)
        for forbidden in ("短袜", "过膝袜", "肉丝"):
            self.assertNotIn(forbidden, filtered)
        self.assertEqual(set(plugin_main.LEGWEAR_PROMPTS), {"光腿神器", "白丝", "黑丝"})
        bare_leg = plugin_main.LEGWEAR_PROMPTS["光腿神器"]
        self.assertIn("光腿效果", bare_leg)
        self.assertNotIn("干净匀净", bare_leg)
        self.assertNotIn("皮肤纹理", bare_leg)
        self.assertIn("不展示脚部", bare_leg)
        self.assertNotIn("脚趾自然清晰", bare_leg)
        for name in ("白丝", "黑丝"):
            text = plugin_main.LEGWEAR_PROMPTS[name]
            self.assertIn("不透", text)
            self.assertIn("主要看腿形", text)
            self.assertIn("长袜", text)
            self.assertIn("袜口", text)
            self.assertIn("不展示脚部", text)
            self.assertNotIn("微胖软肉", text)
            self.assertIn("不要超薄透视", text)
            self.assertNotIn("半透明", text)
            self.assertNotIn("勒进大腿肉", text)
            self.assertNotIn("浅压痕", text)
            self.assertNotIn("大象腿", text)
            self.assertNotIn("细杆腿", text)
            self.assertNotIn("连裤丝袜：", text)

        # /看看COS random outfit pool
        ids = set()
        for _ in range(40):
            t = plugin_main.SelfieImagePlugin._build_cos_look_action(_P(), "", False)
            self.assertIn("看看COS模式", t)
            self.assertIn("换装", t)
            self.assertIn("不要换成别人的脸", t)
            m = re.search(r"【cos:([a-z0-9_]+)】", t)
            self.assertTrue(m, t)
            ids.add(m.group(1))
        self.assertGreaterEqual(len(ids), 4, ids)
        self.assertEqual(len(plugin_main.COS_LOOK_SETS), 26)
        titles = {x["title"] for x in plugin_main.COS_LOOK_SETS}
        self.assertIn("公孙离·青金短裙", titles)
        self.assertIn("公孙离·墨染江湖", titles)
        self.assertIn("西施·诗雨江南", titles)
        self.assertIn("薄荷粉纱汉服", titles)
        self.assertIn("爱莉希雅·粉白奇幻", titles)
        self.assertIn("洛琪希·奶油睡衣", titles)
        self.assertIn("和泉纱雾·粉结白T", titles)
        self.assertIn("菲比·双马尾白裙", titles)
        self.assertIn("貂蝉·猫影幻舞", titles)
        self.assertIn("大乔·白鹤梁神女", titles)
        self.assertIn("海月·潮汐", titles)
        self.assertIn("戈娅·荒野猎手", titles)
        self.assertIn("今汐·朔雷之鳞", titles)
        self.assertIn("长离·焚羽", titles)
        self.assertIn("坎特蕾拉·紫海", titles)
        self.assertIn("珂莱塔·冰冕", titles)
        self.assertIn("约尔·荆棘姬", titles)
        self.assertIn("艾斯德斯·冰将军", titles)
        for item in plugin_main.COS_LOOK_SETS:
            blob = item["title"] + item["prompt"]
            self.assertNotIn("抖音", blob)
            self.assertNotIn("擦边", blob)
            self.assertNotIn("反差", blob)
        roxy = next(x for x in plugin_main.COS_LOOK_SETS if x["id"] == "roxy_cream")
        self.assertIn("禁止蓝色旅行法师外套", roxy["prompt"])
        self.assertIn("双麻花辫", roxy["prompt"])
        self.assertIn("睡衣", roxy["prompt"])
        mansui = next(x for x in plugin_main.COS_LOOK_SETS if x["id"] == "mansui_xianxia")
        self.assertIn("跪坐", mansui["prompt"])
        self.assertIn("高开衩", mansui["prompt"])
        self.assertNotIn("反差", mansui["prompt"])
        ink = next(x for x in plugin_main.COS_LOOK_SETS if x["id"] == "gongsunli_ink")
        self.assertIn("泼墨", ink["prompt"])
        self.assertIn("高开叉", ink["prompt"])
        wrap = plugin_main.SelfieImagePlugin._build_cos_look_action(_P(), "", False)
        self.assertIn("对镜", wrap)
        self.assertNotIn("第一人称自拍或居家随手拍", wrap)

        class PersonaStub:
            class Intent:
                is_legs_only = True

            def analyze_selfie_intent(self, action: str):
                return self.Intent()

        plugin = object.__new__(plugin_main.SelfieImagePlugin)
        plugin.persona = PersonaStub()
        normalized = plugin._normalize_selfie_action("看看腿 白丝 短袜 清晨", False)
        self.assertIn("【pose:", normalized)
        self.assertIn("清晨", normalized)
        selected = [name for name in ("光腿神器", "白丝", "黑丝") if f"本次腿部穿搭：{name}" in normalized]
        self.assertEqual(selected, ["白丝"], normalized)
        self.assertNotIn("短袜", normalized)
        self.assertEqual(plugin._normalize_selfie_action(normalized, False), normalized)

    def test_user_requested_legwear_is_honored(self) -> None:
        import sys
        import types
        import tempfile
        import re

        if "astrbot" not in sys.modules:
            astrbot = types.ModuleType("astrbot")
            api = types.ModuleType("astrbot.api")
            star = types.ModuleType("astrbot.api.star")
            event = types.ModuleType("astrbot.api.event")
            comps = types.ModuleType("astrbot.api.message_components")
            class Star: pass
            class Context: pass
            def register(*a, **k):
                def deco(cls): return cls
                return deco
            star.Star = Star
            star.Context = Context
            star.register = register
            class filter:
                @staticmethod
                def command(*a, **k):
                    return lambda fn: fn
                class PermissionType:
                    ADMIN = "admin"
                    MEMBER = "member"
                @staticmethod
                def permission_type(*a, **k):
                    return lambda fn: fn
            event.filter = filter
            api.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None, debug=lambda *a, **k: None)
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

        from importlib import reload
        import astrbot_plugin_selfie_image.main as plugin_main
        if not hasattr(plugin_main, "parse_requested_legwear"):
            plugin_main = reload(plugin_main)

        self.assertEqual(plugin_main.parse_requested_legwear("看看腿 白丝"), "白丝")
        self.assertEqual(plugin_main.parse_requested_legwear("黑丝 3"), "黑丝")
        self.assertEqual(plugin_main.parse_requested_legwear("光腿"), "光腿神器")
        boilerplate = "若本次是白丝/黑丝：丝袜必须包住整只脚到脚趾。本次腿部穿搭：光腿神器。"
        self.assertEqual(plugin_main.parse_requested_legwear(boilerplate), "光腿神器")

        class _P:
            pass

        white = plugin_main.SelfieImagePlugin._build_leg_focus_action(_P(), "白丝 清晨", False)
        self.assertIn("本次腿部穿搭：白丝", white)
        self.assertNotIn("本次腿部穿搭：光腿神器", white)
        self.assertNotIn("本次腿部穿搭：黑丝", white)
        black = plugin_main.SelfieImagePlugin._build_leg_focus_action(_P(), "黑丝", False)
        self.assertIn("本次腿部穿搭：黑丝", black)
        bare = plugin_main.SelfieImagePlugin._build_leg_focus_action(_P(), "光腿神器", False)
        self.assertIn("本次腿部穿搭：光腿神器", bare)
        forced = plugin_main.SelfieImagePlugin._build_leg_focus_action(_P(), "", False, force_legwear="白丝")
        self.assertIn("本次腿部穿搭：白丝", forced)

    def test_legwear_is_pose_weighted(self) -> None:
        main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn('"side_lie": (("光腿神器", 6), ("白丝", 3), ("黑丝", 1))', main_src)
        self.assertIn('"cross_leg": (("光腿神器", 2), ("白丝", 4), ("黑丝", 4))', main_src)
        self.assertIn('"stand_topdown": (("光腿神器", 3), ("白丝", 3), ("黑丝", 4))', main_src)

    def test_multi_image_commands_rebuild_each_shot(self) -> None:
        main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn("rebuild_each", main_src)
        self.assertIn("avoid_pose", main_src)
        self.assertIn("_build_selfie_look_action", main_src)
        self.assertIn("arm_half", main_src)
        self.assertIn("half_front", main_src)
        self.assertIn("command-look-you", main_src)


class StudioStoreTests(unittest.TestCase):
    def test_selfie_template_mentions_look_legs_legwear(self) -> None:
        from astrbot_plugin_selfie_image.studio import list_studio_templates

        templates = {item["id"]: item for item in list_studio_templates()}
        description = templates["selfie"]["description"]
        self.assertIn("看看腿", description)
        self.assertIn("光腿神器、白丝或黑丝", description)

    def test_group_template_and_persist(self) -> None:
        import tempfile
        from astrbot_plugin_selfie_image.studio import (
            StudioStore,
            build_studio_action,
            BUILTIN_PROMPTS,
            list_studio_templates,
            normalize_template_id,
            prompts_for_template,
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = StudioStore(tmp)
            # legacy flag still creates a usable session (defaults to duo)
            session = store.create("测试合影", use_group_template=True)
            self.assertIn(session.get("template"), {"duo", "group"})
            roles = [s.get("role") for s in session.get("slots") or []]
            self.assertIn("identity", roles)
            self.assertIn("peer", roles)
            self.assertTrue(any(p.get("prompt") for p in BUILTIN_PROMPTS))
            action = build_studio_action(session)
            self.assertIn("合影", action)
            updated = store.update_graph(session["id"], {"prompt": "窗边合影", "mode": "group", "count": 2})
            self.assertEqual(updated["graph"]["prompt"], "窗边合影")
            self.assertEqual(updated["graph"]["count"], 2)
            again = StudioStore(tmp).get(session["id"])
            self.assertEqual(again["graph"]["prompt"], "窗边合影")

    def test_p0_templates_layouts(self) -> None:
        import tempfile
        from astrbot_plugin_selfie_image.studio import StudioStore, list_studio_templates, prompts_for_template

        ids = {t["id"] for t in list_studio_templates()}
        for need in ("duo", "group", "selfie", "clothes", "i2i", "t2i", "blank"):
            self.assertIn(need, ids)
        with tempfile.TemporaryDirectory() as tmp:
            store = StudioStore(tmp)
            duo = store.create("双人", template="duo")
            self.assertEqual(duo["template"], "duo")
            self.assertEqual(duo["graph"]["mode"], "group")
            self.assertEqual(sum(1 for s in duo["slots"] if s["role"] == "peer"), 1)
            selfie = store.create("自拍", template="selfie")
            self.assertEqual(selfie["graph"]["mode"], "selfie")
            self.assertTrue(any(s["role"] == "outfit" for s in selfie["slots"]))
            clothes = store.create("换装", template="clothes")
            self.assertTrue(any(s["role"] == "outfit" for s in clothes["slots"]))
            i2i = store.create("精修", template="i2i")
            self.assertEqual(i2i["graph"]["mode"], "i2i")
            self.assertFalse(i2i["graph"]["use_persona_identity"])
            self.assertTrue(any(s["role"] == "base" for s in i2i["slots"]))
            t2i = store.create("文生", template="t2i")
            self.assertEqual(t2i["graph"]["mode"], "t2i")
            blank = store.create("空", template="blank")
            self.assertEqual(blank["slots"], [])
            chips = prompts_for_template("duo")
            self.assertTrue(any("合影" in (c.get("prompt") or "") or "双人" in (c.get("title") or "") for c in chips))


    def test_template_chips_do_not_leak(self) -> None:
        from astrbot_plugin_selfie_image.studio import prompts_for_template, global_prompt_presets, default_image_preset_seed

        duo = prompts_for_template("duo")
        titles = {str(x.get("title")) for x in duo}
        self.assertIn("双人温馨", titles)
        self.assertNotIn("多人温馨", titles)  # group-only
        self.assertNotIn("精修表情", titles)  # i2i-only
        self.assertIn("捧脸", titles)
        globals_ = global_prompt_presets()
        gnames = {str(x.get("name")) for x in globals_}
        for need in ("捧脸", "变真人", "果冻化", "真人化", "变COS", "漫画封面", "证件照", "男友视角", "漏腰"):
            self.assertIn(need, gnames)
        seed = default_image_preset_seed()
        self.assertIn("捧脸", seed)
        self.assertIn("漏腰", seed)
        self.assertTrue(seed["捧脸"]["prompt"])
        lou = seed["漏腰"]["prompt"]
        self.assertIn("短上衣", lou)
        self.assertIn("oversized", lou)
        self.assertIn("腰线", lou)
        self.assertIn("居家休闲自拍", lou)
        for bad in ("露脐", "肚脐", "胸部", "boyfriend-view", "参考男友", "midriff", "bra"):
            self.assertNotIn(bad, lou)
    def test_default_presets_seed(self) -> None:
        import tempfile
        from astrbot_plugin_selfie_image.preset import ImagePresetManager

        with tempfile.TemporaryDirectory() as tmp:
            mgr = ImagePresetManager(tmp)
            names = {n for n, _ in mgr.list()}
            for need in ("捧脸", "变真人", "果冻化", "真人化", "变COS", "漫画封面", "证件照", "男友视角", "漏腰"):
                self.assertIn(need, names)
            # user override not clobbered
            mgr.add("捧脸", "自定义捧脸提示词")
            mgr2 = ImagePresetManager(tmp)
            self.assertEqual(mgr2.presets["捧脸"].prompt, "自定义捧脸提示词")
            # 露腰 alias resolves to 漏腰 preset body
            resolved = mgr.resolve("露腰")
            self.assertEqual(resolved.get("preset_name"), "漏腰")
            rp = resolved.get("prompt") or ""
            self.assertIn("短上衣", rp)
            self.assertIn("oversized", rp)
            self.assertIn("腰线", rp)
            self.assertNotIn("露脐", rp)
            self.assertNotIn("参考男友", rp)
    def test_selfie_command_expands_preset_before_action_wrap(self) -> None:
        """/自拍 捧脸 must expand preset on raw user text, not after long action wrap."""
        import tempfile
        from astrbot_plugin_selfie_image.preset import ImagePresetManager

        stub = SessionModelAndTaskTests()._plugin_stub()
        from astrbot_plugin_selfie_image import main as plugin_main

        with tempfile.TemporaryDirectory() as tmp:
            stub.presets = ImagePresetManager(tmp)
            stub.config.image_default_aspect_ratio = "自动"
            stub.config.image_default_resolution = "1K"
            expanded, aspect, resolution, name = plugin_main.SelfieImagePlugin._expand_user_text_with_preset(
                stub, "捧脸"
            )
            self.assertEqual(name, "捧脸")
            self.assertIn("捧住她的脸颊", expanded)
            self.assertNotEqual(expanded, "捧脸")
            # Wrapped action still carries expanded preset as 用户补充要求
            action = plugin_main.SelfieImagePlugin._build_selfie_look_action(stub, expanded, False)
            self.assertIn("捧住她的脸颊", action)
            self.assertIn("用户补充要求优先", action)
            # Late resolve on wrapped action alone would fail; early expand is required
            late = stub.presets.resolve(action)
            self.assertFalse(late.get("preset_name"))
            # 露腰 alias + dedicated crop-waist selfie framing
            expanded2, _, _, name2 = plugin_main.SelfieImagePlugin._expand_user_text_with_preset(stub, "露腰")
            self.assertEqual(name2, "漏腰")
            self.assertIn("短上衣", expanded2)
            waist = plugin_main.SelfieImagePlugin._build_selfie_look_action(stub, expanded2, False)
            self.assertIn("【shot:crop_waist】", waist)
            self.assertIn("漏腰模式", waist)
            self.assertIn("宽松", waist)
            self.assertIn("腰线", waist)
            self.assertNotIn("arm_half", waist)
            self.assertNotIn("今日穿搭与气质一致", waist)
            self.assertNotIn("露脐", waist)
            self.assertNotIn("肚脐", waist)
            self.assertNotIn("参考男友", waist)
            short = plugin_main.SelfieImagePlugin._build_selfie_look_action(stub, "露腰", False)
            self.assertIn("【shot:crop_waist】", short)
    def test_clothes_followup_prefers_user_context_images(self) -> None:
        stub = SessionModelAndTaskTests()._plugin_stub()
        from astrbot_plugin_selfie_image import main as plugin_main

        # Fake context: bot selfie first (newer), then user outfit ref
        stub._conversation_context = __import__("collections").OrderedDict()
        key = "group:g1"
        stub._context_session_key = lambda event=None: key
        stub._context_lock = __import__("threading").RLock()
        stub._conversation_context[key] = [
            {
                "msg_id": "1",
                "is_bot": False,
                "image_sources": ["user_outfit.jpg"],
                "content": "[图片]",
            },
            {
                "msg_id": "2",
                "is_bot": True,
                "image_sources": ["bot_selfie.jpg"],
                "content": "[图片]",
            },
        ]
        self.assertTrue(plugin_main.SelfieImagePlugin._looks_like_clothes_followup(stub, "但是怎么不是刚刚的衣服"))
        self.assertTrue(plugin_main.SelfieImagePlugin._looks_like_context_image_reference(stub, "是穿这个"))
        self.assertTrue(plugin_main.SelfieImagePlugin._looks_like_context_image_reference(stub, "穿这个看看"))
        # Prefer user only for clothes follow-up
        srcs = plugin_main.SelfieImagePlugin._recent_context_image_sources(
            stub, None, max_images=4, prefer_user=True, user_only=True
        )
        self.assertEqual(srcs, ["user_outfit.jpg"])
        # Default prefer_user still puts user first even if bot is newer in list order
        srcs2 = plugin_main.SelfieImagePlugin._recent_context_image_sources(
            stub, None, max_images=4, prefer_user=True, user_only=False
        )
        self.assertEqual(srcs2[0], "user_outfit.jpg")

    def test_reuse_previous_outfit_prefers_bot_context_images(self) -> None:
        """「这一身不好看，用刚刚那一套」应挂 bot 近图，不是用户图。"""
        stub = SessionModelAndTaskTests()._plugin_stub()
        from astrbot_plugin_selfie_image import main as plugin_main

        phrase = "这一身不好看，你能用刚刚那一套吗"
        self.assertTrue(plugin_main.SelfieImagePlugin._looks_like_edit_bot_result_followup(stub, phrase))
        self.assertTrue(plugin_main.SelfieImagePlugin._looks_like_clothes_followup(stub, phrase))
        self.assertTrue(plugin_main.SelfieImagePlugin._looks_like_context_image_reference(stub, phrase))
        for t in ("用刚刚那一套", "上一套", "刚才那套", "换回刚刚那套", "刚刚那一套"):
            self.assertTrue(
                plugin_main.SelfieImagePlugin._looks_like_edit_bot_result_followup(stub, t),
                msg=t,
            )
            self.assertTrue(
                plugin_main.SelfieImagePlugin._looks_like_context_image_reference(stub, t),
                msg=t,
            )

        stub._conversation_context = __import__("collections").OrderedDict()
        key = "group:g2"
        stub._context_session_key = lambda event=None: key
        stub._context_lock = __import__("threading").RLock()
        stub._conversation_context[key] = [
            {
                "msg_id": "1",
                "is_bot": False,
                "image_sources": ["user_noise.jpg"],
                "content": "[图片]",
            },
            {
                "msg_id": "2",
                "is_bot": True,
                "image_sources": ["bot_outfit_night.jpg"],
                "content": "[图片]",
            },
            {
                "msg_id": "3",
                "is_bot": True,
                "image_sources": ["bot_outfit_day.jpg"],
                "content": "[图片]",
            },
        ]
        # bot_only / prefer bot first
        bot_only = plugin_main.SelfieImagePlugin._recent_context_image_sources(
            stub, None, max_images=4, prefer_user=False, bot_only=True
        )
        self.assertEqual(bot_only[0], "bot_outfit_day.jpg")
        self.assertNotIn("user_noise.jpg", bot_only)
        prefer_bot = plugin_main.SelfieImagePlugin._recent_context_image_sources(
            stub, None, max_images=4, prefer_user=False, user_only=False, bot_only=False
        )
        self.assertEqual(prefer_bot[0], "bot_outfit_day.jpg")

        # Collector routing mirrors production: edit_bot wins over pure clothes user_only
        edit_bot = plugin_main.SelfieImagePlugin._looks_like_edit_bot_result_followup(stub, phrase)
        clothes = plugin_main.SelfieImagePlugin._looks_like_clothes_followup(stub, phrase)
        user_only = clothes and not edit_bot
        bot_only_flag = edit_bot and not clothes
        prefer_user = not edit_bot
        # dual-match phrase: not user_only, not bot_only exclusive, but prefer_user False → bot first
        self.assertFalse(user_only)
        self.assertFalse(prefer_user)
        routed = plugin_main.SelfieImagePlugin._recent_context_image_sources(
            stub,
            None,
            max_images=4,
            prefer_user=prefer_user,
            user_only=user_only,
            bot_only=bot_only_flag,
        )
        self.assertEqual(routed[0], "bot_outfit_day.jpg")

    def test_llm_generation_retry_cache_preserves_request_and_feedback(self) -> None:
        stub = SessionModelAndTaskTests()._plugin_stub()
        from astrbot_plugin_selfie_image import main as plugin_main

        stub._llm_generation_lock = __import__("threading").RLock()
        stub._last_llm_generations = __import__("collections").OrderedDict()
        stub._context_max_sessions = 100
        stub._context_session_key = lambda event=None: "group:g1"
        plugin_main.SelfieImagePlugin._remember_llm_generation(
            stub,
            None,
            "selfie",
            {"action": "白裙坐在窗边", "count": 1, "aspect_ratio": "9:16", "resolution": "2K"},
        )
        cached = plugin_main.SelfieImagePlugin._last_llm_generation(stub, None, "太成熟了，年轻一点")
        self.assertEqual(cached["kind"], "selfie")
        self.assertEqual(cached["params"]["aspect_ratio"], "9:16")
        self.assertIn("太成熟了，年轻一点", cached["params"]["action"])
        self.assertIn("优先修正", cached["params"]["action"])

    def test_retry_last_generation_replays_selfie_with_feedback(self) -> None:
        stub = SessionModelAndTaskTests()._plugin_stub()
        from astrbot_plugin_selfie_image import main as plugin_main

        stub._llm_generation_lock = __import__("threading").RLock()
        stub._last_llm_generations = __import__("collections").OrderedDict()
        stub._context_max_sessions = 100
        stub._context_session_key = lambda event=None: "group:g1"
        stub._remember_llm_generation(None, "selfie", {"action": "窗边白裙", "count": 1})
        received = {}

        async def replay(event, **params):
            received.update(params)
            return "ok"

        stub.tool_generate_selfie = replay
        result = asyncio.run(plugin_main.SelfieImagePlugin.tool_retry_last_generation(stub, object(), "更年轻一点"))
        self.assertEqual(result, "ok")
        self.assertIn("窗边白裙", received["action"])
        self.assertIn("更年轻一点", received["action"])

    def test_dashboard_has_studio_tab(self) -> None:
        from astrbot_plugin_selfie_image.web import INDEX_HTML, WEB_TASK_ID_RE

        self.assertIn('data-tab="studio"', INDEX_HTML)
        self.assertIn("studioTemplateSelect", INDEX_HTML)
        self.assertIn("按模板新建", INDEX_HTML)
        self.assertIn("studioPresetBtn", INDEX_HTML)
        self.assertIn("testPresetBtn", INDEX_HTML)
        self.assertIn("syncPresetToggleButton", INDEX_HTML)
        self.assertIn("preset-chip", INDEX_HTML)
        self.assertIn("'收回'", INDEX_HTML)
        self.assertNotIn("上方芯片随模板变化", INDEX_HTML)
        # click-to-use should not auto collapse panel
        panel_fn = INDEX_HTML.split("function renderPresetPanel", 1)[-1].split("async function ensurePromptPresetsLoaded", 1)[0]
        self.assertNotIn("STUDIO.presetOpen = false", panel_fn)
        self.assertNotIn("__TEST_PRESET_OPEN = false", panel_fn)
        self.assertIn("/api/prompt-presets", INDEX_HTML)
        self.assertIn("tags.includes(tid)", INDEX_HTML)
        self.assertIn("/api/studio/sessions", INDEX_HTML)
        self.assertIn("/api/studio/gallery", INDEX_HTML)
        self.assertIn("studioPickRecordBtn", INDEX_HTML)
        self.assertIn("作底图", INDEX_HTML)
        self.assertIn("作服装", INDEX_HTML)
        self.assertIn("studioMoveSlot", INDEX_HTML)
        self.assertIn("setStudioRunningUI", INDEX_HTML)
        self.assertIn("data-cache-path", INDEX_HTML)
        self.assertIn("loadProtectedImages(wrap)", INDEX_HTML)
        self.assertTrue(WEB_TASK_ID_RE.fullmatch("web-studio-12345678-1"))

    def test_studio_promote_role_and_gallery(self) -> None:
        import tempfile
        from astrbot_plugin_selfie_image.studio import StudioStore

        with tempfile.TemporaryDirectory() as tmp:
            store = StudioStore(tmp)
            session = store.create("b1", template="i2i")
            # fake result
            with store._lock:
                s = store._require(session["id"])
                s["results"] = [{"id": "res1", "image_path": "generated/a.png", "created_at": "t"}]
                store._persist()
            out = store.promote_result_to_role(session["id"], "res1", "outfit")
            roles = [x.get("role") for x in out.get("slots") or []]
            self.assertIn("outfit", roles)
            outfit = next(x for x in out["slots"] if x["role"] == "outfit")
            self.assertEqual(outfit["image_path"], "generated/a.png")
            listed = store.list_sessions()
            self.assertTrue(listed)
            self.assertIn("thumb_path", listed[0])


if __name__ == "__main__":
    unittest.main()
