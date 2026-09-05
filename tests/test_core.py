from __future__ import annotations

import base64
import copy
import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
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

from astrbot_plugin_selfie_image.generation.generator import generate_image_with_fallback
from astrbot_plugin_selfie_image.core.proxy import (
    IMAGE_DOWNLOAD_WAIT_SECONDS,
    LOCAL_IMAGE_WAIT_SECONDS,
    image_client_timeout,
    image_download_timeout,
    parse_channel_proxy,
)
from astrbot_plugin_selfie_image.generation.video import VideoGenerateRequest, VideoGenerateResult, generate_video_with_fallback
from astrbot_plugin_selfie_image.core.error_classify import (
    classify_generation_error,
    is_non_retryable_generation_error,
    is_param_profile_switch_error,
    is_transport_profile_switch_error,
)
from astrbot_plugin_selfie_image.core.models import (
    AICatConfig,
    DEFAULT_CONFIG,
    ImageModelTarget,
    deep_merge,
    normalize_config_tree,
    preflight_image_channel,
    preflight_config_channels,
    normalize_provider_type,
    resolve_model_provider_type,
)
from astrbot_plugin_selfie_image.core.providers import (
    AgnesImageAdapter,
    BaseImageAdapter,
    GeminiImageAdapter,
    GeminiOpenAIImageAdapter,
    GrokImageAdapter,
    ImageGenerateResult,
    ImageGenerateRequest,
    ImageReference,
    NovelAIImageAdapter,
    OpenAIChatImageAdapter,
    OpenAIImageAdapter,
    build_model_list_urls,
    build_openai_chat_completions_endpoint,
    clean_image_url,
    create_adapter,
    extract_model_ids_from_response,
    extract_image_urls_from_text,
    fetch_generated_image_url,
    image_sources_from_response,
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
from astrbot_plugin_selfie_image.core.utils import (
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
from astrbot_plugin_selfie_image.webui.web import Flask, FlaskWebServer, INDEX_HTML


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 128


class FakeResponse:
    def __init__(self, data=None, status: int = 200, text: str = "", raw: bytes | None = None) -> None:
        self.data = {} if data is None else data
        self.status = status
        self._text = text if text else json.dumps(self.data)
        self._raw = raw
        self.charset = "utf-8"
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        if self._raw is not None:
            return self._raw
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
        raw: bytes | None = None,
    ) -> None:
        self.data = {} if data is None else data
        self.status = status
        self.text = text
        self.raw = raw
        self.get_data = get_data
        self.get_status = get_status
        self.get_headers = get_headers
        self.requests = []

    async def post(self, url: str, **kwargs):
        self.requests.append({"method": "POST", "url": url, **kwargs})
        return FakeResponse(self.data, self.status, self.text, raw=self.raw)

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

    def list_prompt_presets_for_web(self):
        return [{"name": "捧脸", "title": "捧脸", "prompt": "捧住脸颊"}]

    def list_cos_look_sets_for_web(self):
        return [{"id": "roxy_cream", "title": "洛琪希·奶油睡衣", "prompt": "洛琪希 COS 完整提示词"}]

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
        from astrbot_plugin_selfie_image.core.constants import PLUGIN_VERSION

        metadata = (Path(__file__).resolve().parents[1] / "metadata.yaml").read_text(encoding="utf-8")
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"version: {PLUGIN_VERSION}", metadata)
        self.assertIn(f"当前稳定版：`{PLUGIN_VERSION}`", readme)
        self.assertEqual(PLUGIN_VERSION, "1.4.13")

    def test_runtime_defaults_match_public_schema(self) -> None:
        config = AICatConfig.from_dict({})
        self.assertEqual(config.web_host, "127.0.0.1")
        self.assertEqual(config.image_max_batch_count, 10)
        self.assertEqual(config.image_default_aspect_ratio, "9:16")
        self.assertEqual(DEFAULT_CONFIG["image"]["default_aspect_ratio"], "9:16")

    def test_image_targets_use_model_cap_and_video_keeps_global_timeout(self) -> None:
        config = AICatConfig.from_dict(
            {
                "image": {"global_timeout": 280},
                "image_channels": [
                    {
                        "name": "image",
                        "provider_type": "openai",
                        "base_url": "https://image.test",
                        "enabled_models": ["gpt-image-2"],
                    }
                ],
                "video": {"global_timeout": 320},
                "video_channels": [
                    {
                        "name": "video",
                        "provider_type": "openai",
                        "base_url": "https://video.test",
                        "enabled_models": ["video-model"],
                    }
                ],
            }
        )

        self.assertEqual(config.get_prioritized_targets()[0].timeout, LOCAL_IMAGE_WAIT_SECONDS)
        self.assertEqual(config.get_prioritized_video_targets()[0].timeout, 320)

    def test_image_to_text_models_are_normalized_and_mark_targets(self) -> None:
        config = AICatConfig.from_dict(
            {
                "image_channels": [
                    {
                        "name": "image",
                        "provider_type": "openai",
                        "base_url": "https://image.test",
                        "api_key": "sk-test",
                        "enabled_models": ["plain", "describe-first"],
                        "image_to_text_models": ["describe-first", "disabled-model"],
                    }
                ]
            }
        )

        channel = config.image_channels[0]
        self.assertEqual(channel.image_to_text_models, ["describe-first"])
        self.assertEqual(
            config.raw["image_channels"][0]["image_to_text_models"],
            ["describe-first"],
        )
        targets = {target.model: target for target in channel.targets(180)}
        self.assertFalse(targets["plain"].extra["image_to_text_enabled"])
        self.assertTrue(targets["describe-first"].extra["image_to_text_enabled"])

    def test_dashboard_exposes_image_to_text_controls_and_auxiliary_labels(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "pages/dashboard/index.html").read_text(encoding="utf-8")
        for doc in (html, INDEX_HTML):
            self.assertIn("model-image-to-text", doc)
            self.assertIn("image_to_text_models", doc)
            self.assertIn("图转文模型", doc)
            self.assertIn("留空时使用当前 LLM", doc)
            self.assertIn("辅助功能", doc)
            self.assertIn("加辅助渠道", doc)

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
        from astrbot_plugin_selfie_image.core.models import AICatConfig
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
        from astrbot_plugin_selfie_image.webui.web import INDEX_HTML
        providers = (Path(__file__).resolve().parents[1] / "core/providers.py").read_text(encoding="utf-8")
        self.assertIn("download_proxy", providers)
        html = (Path(__file__).resolve().parents[1] / "pages/dashboard/index.html").read_text(encoding="utf-8")
        for doc in (html, INDEX_HTML):
            self.assertIn("modelDownloadProxySelectHtml", doc)
            self.assertIn("model-download-proxy", doc)
            self.assertIn("btn.dataset.tab === 'proxies'", doc)
            self.assertIn("model-controls", doc)
            self.assertNotIn("minmax(120px, 150px) minmax(120px, 160px)", doc)
            self.assertNotIn("minmax(150px, 190px) auto", doc)
            self.assertIn(".model-row .name { min-width: 0;", doc)
            self.assertIn("white-space: normal", doc)
            self.assertNotIn(".model-provider { min-width: 150px;", doc)
            self.assertNotIn(".model-download-proxy { min-width: 120px;", doc)


    

    def test_parse_prompt_en_response_json(self) -> None:
        from pathlib import Path
        import json
        import re as _re
        from astrbot_plugin_selfie_image.core.models import DEFAULT_CONFIG

        img_t = DEFAULT_CONFIG["image"]["image_prompt_en_template"]
        vid_t = DEFAULT_CONFIG["image"]["video_prompt_en_template"]
        self.assertIn("faithful language conversion only", img_t)
        self.assertIn('"ok":true', img_t)
        self.assertIn("{prompt}", img_t)
        self.assertNotIn("Task: rewrite the user prompt", img_t)
        self.assertIn("faithful language conversion only", vid_t)
        self.assertIn('"ok":true', vid_t)
        root = Path(__file__).resolve().parents[1]
        main_src = (root / "main.py").read_text(encoding="utf-8")
        translation_src = (root / "prompts/prompt_translation.py").read_text(encoding="utf-8")
        audit_src = (root / "features/audit_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("from .prompts.prompt_translation import parse_prompt_en_response", main_src)
        self.assertIn("def parse_prompt_en_response", translation_src)
        self.assertIn("translate_parse_failed", audit_src)
        self.assertIn("fail-open: keep original prompt", audit_src)

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
        from astrbot_plugin_selfie_image.prompts.prompt_templates import BilingualPrompt

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
        from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt

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
        self.assertIn("服装局部", zh)
        self.assertIn("自然的下半身服装局部", zh)
        self.assertIn("服装颜色、材质和层次", zh)
        self.assertIn("真人摄影质感", zh)
        self.assertIn("普通手机", zh)
        self.assertIn("竖屏", zh)
        selfie_zh = build_selfie_builtin_prompt(
            "【自拍 / 看看模式】展示现在的样子。",
            language="zh",
            has_reference_image=True,
            appearance_type="real",
        )
        self.assertIn("竖屏", selfie_zh)
        self.assertIn("真人摄影质感", selfie_zh)
        self.assertIn("普通手机", selfie_zh)
        self.assertIn("natural close lower-body outfit detail", en)
        self.assertIn("everyday clothing", en)
        self.assertIn("smartphone outfit record", en)
        self.assertIn("plastic skin", en)
        self.assertIn("mid-calf socks", en)
        self.assertIn("skin-tone leg-cover styling", en)
        self.assertIn("vertical", en.lower())
        self.assertNotRegex(en, r"[\u3400-\u9fff]")
        self.assertNotIn("User request:", en)
        self.assertLess(len(en), 2400)

        pose_expectations = {
            "sit_crop": "Sit naturally on a chair or sofa",
            "kneel_crop": "natural kneeling pose on a rug or cushion",
            "side_lie_crop": "Rest comfortably on one side on a bed",
            "windowsill_crop": "Sit naturally on a windowsill or low cabinet",
            "desk_sit_crop": "Sit naturally at a desk",
            "floor_knees_up_crop": "relaxed seated pose on a rug or wood floor",
            "reclined_knees_crop": "relaxed seated pose leaning lightly against a sofa or chair",
            "bed_supine_crop": "Rest comfortably on a bed with the outfit falling naturally",
        }
        for pose_id, expected in pose_expectations.items():
            pose_en = build_selfie_builtin_prompt(
                f"看看腿。 【cam:selfie】 【pose:{pose_id}】",
                language="en",
                has_reference_image=True,
            )
            self.assertIn(expected, pose_en)

        translated = build_selfie_builtin_prompt(
            "看看腿。用户补充要求优先：窗边白裙。 【pose:sit】",
            language="en",
            has_reference_image=True,
            appearance_type="real",
            user_text="a white dress by the window",
        )
        self.assertIn("User request: a white dress by the window", translated)

    def test_batch_failure_llm_prompt_is_soft_and_keeps_single_reason(self) -> None:
        from astrbot_plugin_selfie_image.prompts.prompt_templates import build_batch_failure_llm_prompt

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
        from astrbot_plugin_selfie_image.core.models import AICatConfig, DEFAULT_CONFIG
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

    def test_single_shot_failure_does_not_claim_batch_continuation(self) -> None:
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin

        plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
        message = plugin._batch_shot_fail_text(
            index=1,
            total=1,
            done_files=0,
            error="这版构图有点跑偏",
            mode="skip",
            skipped=1,
            skip_max=2,
            will_continue=False,
        )
        self.assertEqual(message, "这张没生成成功：这版构图有点跑偏")
        self.assertNotIn("第 1/1", message)
        self.assertNotIn("继续后面的", message)

    def test_generation_result_normalizes_legacy_and_partial_results(self) -> None:
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin
        from astrbot_plugin_selfie_image.generation.generation_results import normalize_generation_result

        plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
        success = plugin._normalize_generation_result({"success": True, "files": ["a.png"]}, 1)
        self.assertEqual(success["status"], "succeeded")
        self.assertEqual(success["succeeded_count"], 1)
        partial = plugin._normalize_generation_result(
            {"success": False, "files": ["a.png"], "batch_total": 3, "batch_skipped": 1},
            3,
        )
        self.assertEqual(partial["status"], "partial_success")
        self.assertEqual(partial["requested_count"], 3)
        self.assertEqual(partial["succeeded_count"], 1)
        self.assertEqual(partial["failed_count"], 1)
        self.assertFalse(partial["success"])
        direct = normalize_generation_result(
            {"success": False, "files": ["a.png"], "batch_total": 3, "batch_skipped": 1},
            3,
        )
        self.assertEqual(direct["status"], "partial_success")

    def test_generation_metrics_aggregates_without_exposing_prompt(self) -> None:
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin
        from astrbot_plugin_selfie_image.generation.generation_records import build_generation_metrics

        plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
        plugin._records_lock = threading.RLock()
        plugin._records = [
            {"success": True, "status": "succeeded", "count": 2, "used_model": "model-a", "elapsed_seconds": 4.0, "request_data": {"original_prompt": "secret prompt"}, "attempts": [{"channel": "channel-a", "success": False, "elapsed_seconds": 2, "error_category": "server"}, {"channel": "channel-b", "success": True, "elapsed_seconds": 3}]},
            {"success": False, "status": "failed", "used_model": "model-a", "elapsed_seconds": 8.0, "attempts": [{"success": False, "error_category": "server"}]},
        ]
        metrics = plugin.get_generation_metrics()
        self.assertEqual(metrics["retained_records"], 2)
        self.assertEqual(metrics["requested_images"], 3)
        self.assertEqual(metrics["succeeded_images"], 2)
        self.assertEqual(metrics["failed_images"], 1)
        self.assertEqual(metrics["error_categories"]["server"], 2)
        self.assertEqual(metrics["channels"]["channel-a"]["failed"], 1)
        self.assertEqual(metrics["channels"]["channel-b"]["success"], 1)
        self.assertEqual(metrics["channels"]["channel-b"]["fallbacks"], 1)
        self.assertEqual(metrics["channels"]["channel-b"]["success_rate"], 1.0)
        self.assertNotIn("secret prompt", json.dumps(metrics, ensure_ascii=False))
        self.assertEqual(
            build_generation_metrics(plugin._records)["requested_images"],
            metrics["requested_images"],
        )

    def test_composition_metadata_is_hashed_and_classified(self) -> None:
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin

        plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
        metadata = plugin._composition_metadata("看看腿，白丝，全身", "command", "9:16", "1K", 2)
        self.assertEqual(metadata["strategy"], "look_legs")
        self.assertEqual(len(metadata["prompt_hash"]), 16)
        self.assertEqual(metadata["reference_image_count"], 2)
        self.assertNotIn("白丝", json.dumps(metadata, ensure_ascii=False))

    def test_generation_metrics_counts_channel_fallback_only_on_channel_change(self) -> None:
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin

        plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
        plugin._records_lock = threading.RLock()
        plugin._records = [{
            "success": True,
            "status": "succeeded",
            "count": 1,
            "attempts": [
                {"channel": "a", "success": False, "elapsed_seconds": 1, "error_category": "auth"},
                {"channel": "a", "success": False, "elapsed_seconds": 2, "error_category": "rate_limit"},
                {"channel": "b", "success": True, "elapsed_seconds": 3},
            ],
        }]
        channels = plugin.get_generation_metrics()["channels"]
        self.assertEqual(channels["a"]["attempts"], 2)
        self.assertEqual(channels["a"]["fallbacks"], 0)
        self.assertEqual(channels["b"]["fallbacks"], 1)
        self.assertEqual(channels["a"]["error_categories"], {"auth": 1, "rate_limit": 1})

    def test_config_schema_migration_preserves_explicit_concurrency(self) -> None:
        migrated = AICatConfig.from_dict({"image": {"max_concurrent_tasks": 3}})
        self.assertEqual(migrated.raw["schema_version"], 2)
        self.assertEqual(migrated.image_max_concurrent_tasks, 3)

    def test_channel_health_records_operational_failures_without_cooldown(self) -> None:
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin

        plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
        plugin._channel_health = {}
        plugin._channel_health_lock = threading.RLock()
        plugin._record_channel_health([
            {"channel": "x", "success": False, "error_category": "param"},
            {"channel": "x", "success": False, "error_category": "safety"},
        ])
        self.assertEqual(plugin.get_channel_health(), {})
        plugin._record_channel_health([{"channel": "x", "success": False, "error_category": "server"}] * 3)
        health = plugin.get_channel_health()["x"]
        self.assertEqual(health["consecutive_failures"], 3)
        self.assertNotIn("cooldown_until", health)
        self.assertNotIn("cooldown_remaining", health)
        plugin.clear_channel_health("x")
        self.assertNotIn("x", plugin.get_channel_health())

    def test_request_fingerprint_is_stable_and_does_not_include_plain_prompt(self) -> None:
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin

        plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
        first = plugin._request_fingerprint({"prompt": "  hello  ", "count": 1, "images": ["reference-data"]}, "web")
        second = plugin._request_fingerprint({"prompt": "hello", "count": 1, "images": ["reference-data"]}, "web")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 24)
        self.assertNotIn("hello", first)
        self.assertNotIn("reference-data", first)

    def test_duplicate_task_lookup_only_returns_recent_active_task(self) -> None:
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin

        plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
        plugin._web_tasks = {
            "old": {"task_id": "old", "status": "queued", "request_fingerprint": "same", "created_ts": 1},
            "active": {"task_id": "active", "status": "running", "request_fingerprint": "same", "created_ts": 100},
        }
        self.assertEqual(plugin._find_recent_duplicate_task_locked("same", now=130)["task_id"], "active")
        self.assertIsNone(plugin._find_recent_duplicate_task_locked("same", now=300))

    def test_cache_cleanup_preview_does_not_delete_files(self) -> None:
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory) / "image_cache"
            cache_dir.mkdir()
            cache_file = cache_dir / "old.bin"
            cache_file.write_bytes(b"x" * 32)
            plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
            plugin.generated_dir = str(cache_dir)
            plugin.config = SimpleNamespace(image_cache_limit_mb=10)
            plugin._records_lock = threading.RLock()
            plugin._records = []
            preview = plugin.get_cache_cleanup_preview()
            self.assertEqual(preview["total_bytes"], 32)
            self.assertEqual(preview["would_delete"], [])
            self.assertTrue(cache_file.exists())

    def test_persisted_running_task_is_marked_expired_after_restart(self) -> None:
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generation_tasks.json"
            path.write_text(
                json.dumps({"tasks": {"task-1": {"task_id": "task-1", "status": "running"}}}),
                encoding="utf-8",
            )
            plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
            plugin.tasks_path = str(path)
            tasks = plugin._load_web_tasks()
            self.assertEqual(tasks["task-1"]["status"], "expired")
            self.assertFalse(tasks["task-1"]["success"])
            self.assertIn("重新提交", tasks["task-1"]["error"])

    def test_video_payload_grok_midgate_minimal(self) -> None:
        from astrbot_plugin_selfie_image.core.models import ImageModelTarget
        from astrbot_plugin_selfie_image.generation.video import VideoGenerateRequest, _extract_task_id, _video_payload, build_video_generations_endpoint
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
        from astrbot_plugin_selfie_image.core.models import AICatConfig, DEFAULT_CONFIG
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
        from astrbot_plugin_selfie_image.core.models import AICatConfig, normalize_proxy_entry
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
        from astrbot_plugin_selfie_image.webui.web import INDEX_HTML
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
        self.assertEqual(resolve_model_provider_type("gpt-image-2", "openai"), "openai")
        self.assertEqual(resolve_model_provider_type("gpt-image-2", "openai", "openai_chat"), "openai_chat")
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

    def test_openai_chat_channel_stays_on_chat_protocol(self) -> None:
        config = AICatConfig.from_dict(
            {
                "image_channels": [
                    {
                        "name": "chami",
                        "provider_type": "openai_chat",
                        "base_url": "http://chami.yyqzx.com/GPTimage/chami/v1/chat/completions",
                        "api_key": "sk-test",
                        "enabled_models": ["gpt-image-2", "gemini-2.5-flash-image"],
                    }
                ]
            }
        )
        targets = {t.model: t.provider_type for t in config.get_prioritized_targets()}
        self.assertEqual(targets["gpt-image-2"], "openai_chat")
        self.assertEqual(targets["gemini-2.5-flash-image"], "openai_chat")
        self.assertEqual(config.image_channels[0].base_url, "http://chami.yyqzx.com/GPTimage/chami")

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
        from astrbot_plugin_selfie_image.core.models import split_api_keys

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
        from astrbot_plugin_selfie_image.generation.generator import _should_advance_to_next_target
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
        self.assertEqual(classify_generation_error("HTTP 503 upstream")["category"], "server")
        self.assertEqual(
            classify_generation_error("HTTP 502: Upstream service temporarily unavailable")["user_message"],
            "上游服务异常（HTTP 502）：Upstream service temporarily unavailable",
        )
        self.assertEqual(
            classify_generation_error("HTTP 503: No available compatible accounts")["user_message"],
            "上游服务异常（HTTP 503）：No available compatible accounts",
        )
        upstream_channel_error = classify_generation_error(
            "HTTP 502: 分组 image-2 下模型 gpt-image-2 的可用渠道不存在（retry） (request id: test-request)"
        )
        self.assertTrue(upstream_channel_error["retryable"])
        self.assertEqual(upstream_channel_error["category"], "server")
        self.assertIn("可用渠道不存在", upstream_channel_error["user_message"])
        self.assertEqual(
            classify_generation_error("HTTP 502: <!doctype html><html><body>Bad gateway</body></html>")["user_message"],
            "上游服务异常（HTTP 502）",
        )
        redacted_server_error = classify_generation_error(
            "HTTP 500: token=abcdefghijklmnop upstream failed"
        )["user_message"]
        self.assertIn("token=[REDACTED]", redacted_server_error)
        self.assertNotIn("abcdefghijklmnop", redacted_server_error)
        self.assertTrue(classify_generation_error("HTTP 429 rate limit")["retryable"])
        self.assertFalse(classify_generation_error("请求超时")["retryable"])
        self.assertEqual(classify_generation_error("请求超时")["user_message"], "模型超时（180s）")
        self.assertEqual(classify_generation_error("gateway timeout")["user_message"], "上游模型超时")
        self.assertEqual(classify_generation_error("生图全局超时（280秒），最后错误: x")["user_message"], "生图超时（280s）")
        self.assertEqual(classify_generation_error("该模型超时，已改试下一个")["user_message"], "模型超时（180s）")
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
        from astrbot_plugin_selfie_image.core.error_classify import summarize_generation_failures

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

        server_summary = summarize_generation_failures(
            [
                {
                    "attempt": 1,
                    "label": "lmm/gpt-image-2",
                    "success": False,
                    "error": "HTTP 503: No available compatible accounts",
                    "error_user_message": "上游服务异常（HTTP 503）",
                }
            ],
            fallback_error="lmm/gpt-image-2: 上游服务异常（HTTP 503）",
        )
        self.assertEqual(
            server_summary["failure_reason"],
            "lmm/gpt-image-2: 上游服务异常（HTTP 503）：No available compatible accounts",
        )
        self.assertEqual(
            server_summary["failure_reasons"][0]["error_user_message"],
            "上游服务异常（HTTP 503）：No available compatible accounts",
        )

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
        from astrbot_plugin_selfie_image.core.utils import compact_generation_record, summarize_record_for_list

        fat = {
            "success": False,
            "md5": "A" * 32,
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
        self.assertEqual(slim["md5"], "a" * 32)
        row = summarize_record_for_list(slim)
        self.assertTrue(row.get("has_detail"))
        self.assertEqual(row.get("failed_attempt_count"), 1)
        self.assertNotIn("request_data", row)
        self.assertLessEqual(len(row.get("request_prompt") or ""), 241)

    def test_media_sources_keep_original_values_and_follow_image_order(self) -> None:
        from astrbot_plugin_selfie_image.core.utils import compact_generation_record, redact_generation_record

        sources = image_sources_from_response(
            {"data": [{"b64_json": "data:image/png;base64,AAAA"}, {"url": "/images/result.png"}]},
            "https://api.example.test/v1/images",
        )
        self.assertEqual(
            sources,
            [
                {"type": "base64", "value": "data:image/png;base64,AAAA"},
                {"type": "url", "value": "https://api.example.test/images/result.png"},
            ],
        )
        record = redact_generation_record(
            {
                "generated_image_sources": [{"type": "url", "value": "https://cdn.example.test/a.png?api_key=secretvalue"}],
                "response_data": {"video_source": "https://cdn.example.test/v.mp4?api_key=secretvalue"},
            }
        )
        slim = compact_generation_record(record)
        self.assertEqual(record["generated_image_sources"][0]["value"], "https://cdn.example.test/a.png?api_key=secretvalue")
        self.assertEqual(slim["response_data"]["video_source"], "https://cdn.example.test/v.mp4?api_key=secretvalue")

    def test_generation_record_keeps_only_channel_error_raw(self) -> None:
        from astrbot_plugin_selfie_image.core.utils import compact_generation_record, redact_generation_record

        raw_error = "HTTP 500: token=raw-channel-secret"
        record = redact_generation_record(
            {
                "error": "token=top-level-secret",
                "headers": {"Authorization": "Bearer top-level-secret"},
                "attempts": [
                    {
                        "label": "api_key=channel-label-secret",
                        "success": False,
                        "error": raw_error,
                        "error_user_message": "HTTP 500: token=raw-channel-secret",
                    }
                ],
            }
        )
        slim = compact_generation_record(record)

        self.assertEqual(slim["attempts"][0]["error"], raw_error)
        self.assertIn("api_key=[REDACTED]", slim["attempts"][0]["label"])
        self.assertIn("token=[REDACTED]", slim["attempts"][0]["error_user_message"])
        self.assertIn("token=[REDACTED]", slim["error"])
        self.assertEqual(slim["headers"]["Authorization"], "[REDACTED]")

    def test_record_task_splits_multi_image_rows(self) -> None:
        from astrbot_plugin_selfie_image.core.utils import split_generation_record_images

        pieces = split_generation_record_images(
            {
                "success": True,
                "source": "command-look-cos",
                "prompt": "cos",
                "generated_image_paths": ["a.png", "b.png", "c.png"],
                "md5s": ["a" * 32, "b" * 32, "c" * 32],
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
            self.assertEqual(row["md5"], row["generated_image_paths"][0].replace(".png", "") * 32)
            self.assertEqual(row["response_data"]["count"], 1)
            self.assertEqual(len(row["response_data"]["generated_image_paths"]), 1)
            self.assertNotIn("id", row)
        single = split_generation_record_images({"success": True, "generated_image_paths": ["only.png"], "count": 9})
        self.assertEqual(len(single), 1)
        self.assertEqual(single[0]["count"], 1)
        self.assertEqual(single[0]["generated_image_paths"], ["only.png"])
        self.assertEqual(single[0]["md5"], "")

    def test_find_generation_record_by_md5_backfills_legacy_cache(self) -> None:
        import hashlib
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin

        with tempfile.TemporaryDirectory() as directory:
            cache_file = Path(directory) / "legacy.png"
            cache_file.write_bytes(PNG_BYTES)
            plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
            plugin.generated_dir = directory
            plugin._records_lock = threading.RLock()
            plugin._records = [
                {
                    "success": True,
                    "generated_image_paths": ["legacy.png"],
                    "request_prompt": "legacy prompt",
                }
            ]
            found = plugin._find_generation_record_by_md5(hashlib.md5(PNG_BYTES).hexdigest())
            self.assertIsNotNone(found)
            self.assertEqual(found["md5"], hashlib.md5(PNG_BYTES).hexdigest())

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
        self.assertEqual(
            normalize_image_base_url("http://chami.yyqzx.com/GPTimage/chami/v1/chat/completions"),
            "http://chami.yyqzx.com/GPTimage/chami",
        )
        self.assertEqual(normalize_gemini_base_url("https://example.com/v1beta/models/gemini:generateContent"), "https://example.com")
        self.assertEqual(
            build_openai_chat_completions_endpoint("https://example.com"),
            "https://example.com/v1/chat/completions",
        )
        self.assertEqual(
            build_openai_chat_completions_endpoint("https://example.com/v1"),
            "https://example.com/v1/chat/completions",
        )
        self.assertEqual(
            build_openai_chat_completions_endpoint("http://chami.yyqzx.com/GPTimage/chami"),
            "http://chami.yyqzx.com/GPTimage/chami/v1/chat/completions",
        )
        self.assertEqual(
            build_openai_chat_completions_endpoint("http://chami.yyqzx.com/GPTimage/chami/v1"),
            "http://chami.yyqzx.com/GPTimage/chami/v1/chat/completions",
        )
        self.assertEqual(
            build_openai_chat_completions_endpoint("http://chami.yyqzx.com/GPTimage/chami/v1/chat/completions"),
            "http://chami.yyqzx.com/GPTimage/chami/v1/chat/completions",
        )
        self.assertEqual(
            build_openai_chat_completions_endpoint("http://chami.yyqzx.com/GPTimage/chami/v1/images/generations"),
            "http://chami.yyqzx.com/GPTimage/chami/v1/chat/completions",
        )

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
        self.assertEqual(provider_type_from_channel_payload({"provider_type": "openai_chat"}), "openai_chat")
        self.assertEqual(provider_type_from_channel_payload({"apiType": "chat_completions"}), "openai_chat")
        self.assertEqual(normalize_provider_type("openai_chat"), "openai_chat")
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

    async def test_fetch_image_source_accepts_file_uri(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "image with spaces.png"
            image_path.write_bytes(PNG_BYTES)
            source = image_path.as_uri()
            self.assertEqual(
                await fetch_image_source(source, FakeSession(), max_bytes=1024 * 1024),
                (PNG_BYTES, "image/png"),
            )


class ProviderAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_openai_payload_builder_keeps_gpt_image_response_format_out(self) -> None:
        adapter = OpenAIImageAdapter(make_target("openai", "gpt-image-1"), FakeSession())

        payload = adapter.build_image_payload(ImageGenerateRequest(prompt="cat", aspect_ratio="16:9"))

        self.assertEqual(payload["model"], "gpt-image-1")
        self.assertEqual(payload["prompt"], "cat")
        self.assertEqual(payload["n"], 1)
        self.assertEqual(payload["size"], "1536x1024")
        self.assertNotIn("response_format", payload)
        self.assertNotIn("messages", payload)

    async def test_openai_generate_stays_on_images_endpoint(self) -> None:
        response = {"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]}
        session = FakeSession(response)
        adapter = OpenAIImageAdapter(make_target("openai", "gpt-image-2"), session)

        result = await adapter.generate(ImageGenerateRequest(prompt="cat", aspect_ratio="1:1"))

        self.assertEqual(result.images, [PNG_BYTES])
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(session.requests[0]["url"], "https://example.test/v1/images/generations")
        self.assertEqual(session.requests[0]["json"]["prompt"], "cat")

    async def test_openai_edit_timeout_returns_model_timeout(self) -> None:
        target = make_target("openai", "gpt-image-2")
        target.timeout = 280
        adapter = OpenAIImageAdapter(target, FakeSession())
        request = ImageGenerateRequest(
            prompt="cat",
            images=[ImageReference(data=PNG_BYTES, mime_type="image/png")],
        )

        with patch.object(adapter, "_post_edit_form", side_effect=asyncio.TimeoutError):
            result = await adapter.generate(request)

        self.assertEqual(result.error, "模型超时（280s）")

    async def test_openai_chat_generate_posts_prompt_body(self) -> None:
        response = {"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]}
        session = FakeSession(response)
        target = make_target("openai_chat", "gpt-image-2")
        target.base_url = "http://chami.yyqzx.com/GPTimage/chami"
        adapter = OpenAIChatImageAdapter(target, session)

        result = await adapter.generate(ImageGenerateRequest(prompt="一只橘猫", aspect_ratio="1:1"))

        self.assertEqual(result.images, [PNG_BYTES])
        self.assertEqual(len(session.requests), 1)
        request = session.requests[0]
        self.assertEqual(request["url"], "http://chami.yyqzx.com/GPTimage/chami/v1/chat/completions")
        self.assertEqual(request["json"]["model"], "gpt-image-2")
        self.assertEqual(request["json"]["prompt"], "一只橘猫")
        self.assertEqual(request["json"]["n"], 1)
        self.assertEqual(request["json"]["size"], "1024x1024")
        self.assertNotIn("messages", request["json"])

    async def test_openai_chat_generate_does_not_call_images_endpoint(self) -> None:
        session = FakeSession({"error": {"message": "invalid api key"}}, status=401)
        adapter = OpenAIChatImageAdapter(make_target("openai_chat", "gpt-image-2"), session)
        result = await adapter.generate(ImageGenerateRequest(prompt="cat"))

        self.assertIn("HTTP 401", result.error)
        self.assertEqual(len(session.requests), 1)
        self.assertTrue(session.requests[0]["url"].endswith("/v1/chat/completions"))

    async def test_openai_chat_generate_parses_chat_choices_image(self) -> None:
        data_url = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
        session = FakeSession({"choices": [{"message": {"content": f"done {data_url}"}}]})
        adapter = OpenAIChatImageAdapter(make_target("openai_chat", "gpt-image-2"), session)

        result = await adapter.generate(ImageGenerateRequest(prompt="cat"))

        self.assertEqual(result.images, [PNG_BYTES])
        self.assertEqual(session.requests[0]["url"], "https://example.test/v1/chat/completions")

    async def test_openai_chat_generate_parses_raw_png_body(self) -> None:
        session = FakeSession(raw=PNG_BYTES)
        adapter = OpenAIChatImageAdapter(make_target("openai_chat", "gpt-image-2"), session)

        result = await adapter.generate(ImageGenerateRequest(prompt="cat"))

        self.assertEqual(result.images, [PNG_BYTES])
        self.assertFalse(result.error)

    async def test_openai_chat_generate_parses_raw_base64_body(self) -> None:
        session = FakeSession(text=base64.b64encode(PNG_BYTES).decode("ascii"))
        adapter = OpenAIChatImageAdapter(make_target("openai_chat", "gpt-image-2"), session)

        result = await adapter.generate(ImageGenerateRequest(prompt="cat"))

        self.assertEqual(result.images, [PNG_BYTES])
        self.assertFalse(result.error)

    async def test_openai_chat_generate_parses_quoted_base64_body(self) -> None:
        session = FakeSession(text=json.dumps(base64.b64encode(PNG_BYTES).decode("ascii")))
        adapter = OpenAIChatImageAdapter(make_target("openai_chat", "gpt-image-2"), session)

        result = await adapter.generate(ImageGenerateRequest(prompt="cat"))

        self.assertEqual(result.images, [PNG_BYTES])
        self.assertFalse(result.error)

    async def test_openai_chat_generate_parses_sse_keepalive_then_image(self) -> None:
        b64 = base64.b64encode(PNG_BYTES).decode("ascii")
        sse = (
            ": initial_keepalive_heartbeat_to_prevent_504\n\n"
            ": keepalive_heartbeat\n\n"
            ": keepalive_heartbeat\n\n"
            "event: balance_update\n"
            'data: {"GPTimage_balance": 32}\n\n'
            'data: {"choices": [{"delta": {"content": "data:image/png;base64,' + b64 + '"}}]}\n\n'
        )
        session = FakeSession(text=sse)
        adapter = OpenAIChatImageAdapter(make_target("openai_chat", "gpt-image-2"), session)

        result = await adapter.generate(ImageGenerateRequest(prompt="cat"))

        self.assertEqual(result.images, [PNG_BYTES])
        self.assertFalse(result.error)

    async def test_openai_chat_generate_parses_sse_split_delta_content(self) -> None:
        b64 = base64.b64encode(PNG_BYTES).decode("ascii")
        sse = (
            ": keepalive_heartbeat\n\n"
            'data: {"choices": [{"delta": {"content": "data:image/png;base64,"}}]}\n\n'
            'data: {"choices": [{"delta": {"content": "' + b64 + '"}}]}\n\n'
            "data: [DONE]\n"
        )
        session = FakeSession(text=sse)
        adapter = OpenAIChatImageAdapter(make_target("openai_chat", "gpt-image-2"), session)

        result = await adapter.generate(ImageGenerateRequest(prompt="cat"))

        self.assertEqual(result.images, [PNG_BYTES])
        self.assertFalse(result.error)

    async def test_openai_chat_generate_parses_mixed_keepalive_and_image_url(self) -> None:
        sse = (
            ": keepalive_heartbeat\n\n"
            "event: balance_update\n"
            'data: {"GPTimage_balance": 32}\n\n'
            "data: https://cdn.example.test/cat.png\n"
        )
        session = FakeSession(text=sse, get_data=PNG_BYTES)
        adapter = OpenAIChatImageAdapter(make_target("openai_chat", "gpt-image-2"), session)

        result = await adapter.generate(ImageGenerateRequest(prompt="cat"))

        self.assertEqual(result.images, [PNG_BYTES])
        self.assertFalse(result.error)

    async def test_openai_chat_generate_parses_large_mixed_keepalive_quickly(self) -> None:
        image = PNG_BYTES + bytes((index * 17) % 256 for index in range(200_000))
        b64 = base64.b64encode(image).decode("ascii")
        sse = (
            ": initial_keepalive_heartbeat_to_prevent_504\n\n"
            ": keepalive_heartbeat\n\n"
            "event: balance_update\n"
            'data: {"GPTimage_balance": 32}\n\n'
            'data: {"choices": [{"delta": {"content": "data:image/png;base64,' + b64 + '"}}]}\n\n'
        )
        session = FakeSession(text=sse)
        adapter = OpenAIChatImageAdapter(make_target("openai_chat", "gpt-image-2"), session)

        started = time.perf_counter()
        result = await adapter.generate(ImageGenerateRequest(prompt="cat"))
        elapsed = time.perf_counter() - started

        self.assertEqual(result.images, [image])
        self.assertFalse(result.error)
        self.assertLess(elapsed, 1.0, f"mixed keepalive parse stalled for {elapsed:.2f}s")

    def test_create_adapter_uses_openai_chat_type(self) -> None:
        adapter = create_adapter(make_target("openai_chat", "gpt-image-2"), FakeSession())
        self.assertIsInstance(adapter, OpenAIChatImageAdapter)
        self.assertNotIsInstance(create_adapter(make_target("openai", "gpt-image-2"), FakeSession()), OpenAIChatImageAdapter)

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

    async def test_result_from_response_explains_generated_url_download_failure(self) -> None:
        session = FakeSession(
            data={"data": [{"url": "https://cdn.example.test/result.png?token=secret-token-value"}]},
            get_data=b"not-an-image",
        )
        adapter = BaseImageAdapter(make_target(), session)

        result = await adapter.result_from_response(
            session.data,
            ImageGenerateRequest(prompt="cat"),
            "https://example.test",
            detailed_error=True,
        )

        self.assertFalse(result.images)
        self.assertIn("图片链接但下载失败", result.error)
        self.assertIn("cdn.example.test/result.png", result.error)
        self.assertNotIn("secret-token-value", result.error)

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

    async def test_generated_url_download_has_independent_timeout_budget(self) -> None:
        session = FakeSession(get_data=PNG_BYTES)
        diagnostics = []

        timeout_factory = lambda **kwargs: types.SimpleNamespace(**kwargs)
        with patch("astrbot_plugin_selfie_image.core.proxy.aiohttp.ClientTimeout", side_effect=timeout_factory):
            image = await fetch_generated_image_url(
                session,
                "https://cdn.example.test/generated.png?signature=secret",
                timeout=180,
                diagnostics=diagnostics,
            )

        self.assertEqual(image, PNG_BYTES)
        self.assertEqual(session.requests[0]["timeout"].total, IMAGE_DOWNLOAD_WAIT_SECONDS)
        with patch("astrbot_plugin_selfie_image.core.proxy.aiohttp.ClientTimeout", side_effect=timeout_factory):
            self.assertEqual(image_download_timeout(180).total, IMAGE_DOWNLOAD_WAIT_SECONDS)

    async def test_generated_url_download_diagnostics_do_not_include_query(self) -> None:
        session = FakeSession(get_data=b"not-an-image")
        diagnostics = []

        image = await fetch_generated_image_url(
            session,
            "https://cdn.example.test/generated.png?token=secret-token",
            timeout=180,
            diagnostics=diagnostics,
        )

        self.assertIsNone(image)
        self.assertTrue(diagnostics)
        self.assertIn("cdn.example.test/generated.png", diagnostics[0])
        self.assertNotIn("secret-token", diagnostics[0])

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
    async def test_fallback_builds_a_separate_request_for_each_target(self) -> None:
        first = make_target("openai", "describe-first")
        second = make_target("openai", "plain-edit")
        calls = []

        async def fake_try_model(active, request, budget, session):
            calls.append((active.model, request.prompt, len(request.images)))
            if active.model == "describe-first":
                return ImageGenerateResult(error="temporary failure")
            return ImageGenerateResult(images=[PNG_BYTES])

        reference = ImageReference(data=PNG_BYTES, mime_type="image/png")
        base_request = ImageGenerateRequest(prompt="original", images=[reference])

        def request_factory(target):
            if target.model == "describe-first":
                return ImageGenerateRequest(prompt="original\n\n参考图内容描述：红衣人物", images=[])
            return ImageGenerateRequest(prompt="original", images=[reference])

        with patch("astrbot_plugin_selfie_image.generation.generator._try_model", side_effect=fake_try_model):
            result = await generate_image_with_fallback(
                [first, second],
                base_request,
                FakeSession(),
                request_factory=request_factory,
            )

        self.assertEqual(result.images, [PNG_BYTES])
        self.assertEqual(
            calls,
            [
                ("describe-first", "original\n\n参考图内容描述：红衣人物", 0),
                ("plain-edit", "original", 1),
            ],
        )
    async def test_fallback_caps_each_model_request_at_180_seconds(self) -> None:
        target = make_target("openai", "gpt-image-2")
        target.timeout = 280
        budgets = []

        async def fake_try_model(active, req, budget, session):
            budgets.append(budget)
            return ImageGenerateResult(images=[PNG_BYTES])

        with patch("astrbot_plugin_selfie_image.generation.generator._try_model", side_effect=fake_try_model):
            result = await generate_image_with_fallback(
                [target],
                ImageGenerateRequest(prompt="cat"),
                FakeSession(),
                global_timeout=400,
            )

        self.assertEqual(result.images, [PNG_BYTES])
        self.assertEqual(budgets, [180])

    async def test_fallback_retries_with_remaining_global_budget(self) -> None:
        first = make_target("openai", "first")
        second = make_target("openai", "second")
        third = make_target("openai", "third")
        first.timeout = second.timeout = third.timeout = 280
        clock = types.SimpleNamespace(now=0.0)
        budgets = []

        def monotonic():
            return clock.now

        async def fake_try_model(active, req, budget, session):
            budgets.append(budget)
            clock.now += budget
            return ImageGenerateResult(error="temporary failure")

        with (
            patch("astrbot_plugin_selfie_image.generation.generator.time.monotonic", side_effect=monotonic),
            patch("astrbot_plugin_selfie_image.generation.generator._try_model", side_effect=fake_try_model),
        ):
            result = await generate_image_with_fallback(
                [first, second, third],
                ImageGenerateRequest(prompt="cat"),
                FakeSession(),
                global_timeout=280,
            )

        self.assertEqual(budgets, [180, 100])
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(result.error, "生图超时（280s）")

    def test_image_client_timeout_preserves_requested_model_budget(self) -> None:
        timeout_factory = lambda **kwargs: types.SimpleNamespace(**kwargs)
        with patch("astrbot_plugin_selfie_image.core.proxy.aiohttp.ClientTimeout", side_effect=timeout_factory):
            timeout = image_client_timeout(280)
        self.assertEqual(timeout.total, 280)
        self.assertEqual(timeout.sock_read, 280)

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
            patch("astrbot_plugin_selfie_image.generation.generator.create_adapter", side_effect=create_fake_adapter),
            patch("astrbot_plugin_selfie_image.generation.generator.asyncio.sleep", side_effect=no_sleep),
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

    async def test_fallback_preserves_server_error_detail(self) -> None:
        target = make_target("openai", "gpt-image-2")
        upstream_detail = "分组 image-2 下模型 gpt-image-2 的可用渠道不存在（retry）"

        def create_fake_adapter(target, session):
            return FakeGenerateAdapter(ImageGenerateResult(error=f"HTTP 502: {upstream_detail}"))

        with patch("astrbot_plugin_selfie_image.generation.generator.create_adapter", side_effect=create_fake_adapter):
            result = await generate_image_with_fallback(
                [target],
                ImageGenerateRequest(prompt="cat"),
                FakeSession(),
                max_attempts=1,
            )

        self.assertIn(upstream_detail, result.error)
        self.assertIn(upstream_detail, result.attempts[0]["error_user_message"])
        self.assertEqual(result.attempts[0]["error_category"], "server")
        self.assertTrue(result.attempts[0]["retryable"])

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

        with patch("astrbot_plugin_selfie_image.generation.video.generate_video_openai_compatible", side_effect=fake_generate):
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

        with patch("astrbot_plugin_selfie_image.generation.generator.create_adapter", side_effect=create_fake_adapter):
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
            patch("astrbot_plugin_selfie_image.generation.generator.create_adapter", side_effect=create_fake_adapter),
            patch("astrbot_plugin_selfie_image.generation.generator.asyncio.sleep", side_effect=no_sleep),
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

        with patch("astrbot_plugin_selfie_image.generation.generator.create_adapter", side_effect=create_fake_adapter):
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

    async def test_fallback_keeps_raw_error_only_in_channel_record(self) -> None:
        target = make_target("grok", "bad-model")
        secret_error = "Authorization: Bearer sk-live-secret-token and token=abcdefghijklmnop"

        def create_fake_adapter(target, session):
            return FakeGenerateAdapter(ImageGenerateResult(error=secret_error))

        with patch("astrbot_plugin_selfie_image.generation.generator.create_adapter", side_effect=create_fake_adapter):
            result = await generate_image_with_fallback(
                [target],
                ImageGenerateRequest(prompt="cat"),
                FakeSession(),
                max_attempts=1,
            )

        self.assertIn("Bearer [REDACTED]", result.error)
        self.assertIn("token=[REDACTED]", result.error)
        self.assertNotIn("sk-live-secret-token", result.error)
        self.assertEqual(result.attempts[0]["error"], secret_error)
        self.assertIn("Bearer [REDACTED]", result.attempts[0]["error_user_message"])
        self.assertNotIn("sk-live-secret-token", result.attempts[0]["error_user_message"])

    async def test_fallback_redacts_sensitive_exceptions(self) -> None:
        target = make_target("grok", "bad-model")

        class RaisingAdapter:
            async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
                raise RuntimeError("api_key=AIzaSySecretTokenValue")

        with patch("astrbot_plugin_selfie_image.generation.generator.create_adapter", return_value=RaisingAdapter()):
            result = await generate_image_with_fallback(
                [target],
                ImageGenerateRequest(prompt="cat"),
                FakeSession(),
                max_attempts=1,
            )

        self.assertIn("api_key=[REDACTED]", result.error)
        self.assertIn("api_key=AIzaSySecretTokenValue", result.attempts[0]["error"])
        self.assertIn("api_key=[REDACTED]", result.attempts[0]["error_user_message"])

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

        with patch("astrbot_plugin_selfie_image.generation.generator.create_adapter", side_effect=create_fake_adapter):
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

    def test_cos_look_sets_api_uses_plugin_pool(self) -> None:
        client = self.make_client(FakeWebPlugin(""))
        response = client.get("/api/cos-look-sets")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["data"],
            [{"id": "roxy_cream", "title": "洛琪希·奶油睡衣", "prompt": "洛琪希 COS 完整提示词"}],
        )

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
                return [{
                    "error": "api_key=plain-provider-secret",
                    "headers": {"Cookie": "session=abcdef1234567890"},
                    "attempts": [{
                        "error": "HTTP 500: token=raw-channel-secret",
                        "error_user_message": "HTTP 500: token=[REDACTED]",
                    }],
                }]

            def get_record_for_web(self, record_id: str):
                return {
                    "id": record_id,
                    "error": "token=abcdefghijklmnop",
                    "headers": {"Authorization": "Bearer sk-live-secret-token"},
                    "attempts": [{
                        "error": "HTTP 500: token=raw-channel-secret",
                        "error_user_message": "HTTP 500: token=[REDACTED]",
                    }],
                }

            def get_web_image_task(self, task_id: str):
                self.task_status_calls.append(task_id)
                return {
                    "task_id": task_id,
                    "result": {
                        "error": "Authorization: Bearer sk-live-secret-token",
                        "attempts": [{"error": "HTTP 500: token=raw-channel-secret"}],
                    },
                }

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
        self.assertIn("token=raw-channel-secret", records_text)
        self.assertIn("token=raw-channel-secret", detail_text)
        self.assertNotIn("plain-provider-secret", records_text)
        self.assertNotIn("abcdefghijklmnop", detail_text)
        self.assertNotIn("sk-live-secret-token", detail_text)
        self.assertNotIn("sk-live-secret-token", task_text)
        self.assertNotIn("raw-channel-secret", task_text)

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

    def test_image_to_text_uses_configured_auxiliary_model(self) -> None:
        plugin = self._plugin_stub()
        plugin.config = AICatConfig.from_dict(
            {
                "image": {"ocr_model": "vision/qwen-vl"},
                "audit_channels": [
                    {
                        "name": "vision",
                        "provider_type": "openai",
                        "base_url": "https://vision.test",
                        "api_key": "sk-test",
                        "enabled_models": ["qwen-vl"],
                    }
                ],
            }
        )
        calls = []

        async def via_target(target, prompt, images=None):
            calls.append((target.label, images))
            return "```\n红色外套人物，室内自然光\n```"

        plugin._audit_chat_via_target = via_target
        plugin._call_text_llm = lambda *_args, **_kwargs: self.fail("unexpected current LLM call")

        description, meta = asyncio.run(
            plugin._describe_reference_images_for_generation(object(), [PNG_BYTES])
        )

        self.assertEqual(description, "红色外套人物，室内自然光")
        self.assertEqual(meta["model"], "vision/qwen-vl")
        self.assertEqual(calls, [("vision/qwen-vl", [PNG_BYTES])])

    def test_image_to_text_without_model_uses_current_llm(self) -> None:
        plugin = self._plugin_stub()
        plugin.config = AICatConfig.from_dict({"image": {"ocr_model": ""}})
        event = object()
        calls = []

        async def current_llm(actual_event, prompt, timeout=8, images=None):
            calls.append((actual_event, timeout, images))
            return "人物侧身站立，暖光背景"

        plugin._call_text_llm = current_llm
        plugin._audit_chat_via_target = lambda *_args, **_kwargs: self.fail("unexpected auxiliary model call")

        description, meta = asyncio.run(
            plugin._describe_reference_images_for_generation(event, [PNG_BYTES])
        )

        self.assertEqual(description, "人物侧身站立，暖光背景")
        self.assertEqual(meta["model"], "astrbot")
        self.assertEqual(calls, [(event, 30, [PNG_BYTES])])

    def test_prompt_audit_without_model_uses_current_llm(self) -> None:
        plugin = self._plugin_stub()
        plugin.config = AICatConfig.from_dict(
            {"image": {"enable_prompt_audit": True, "prompt_audit_model": ""}}
        )
        plugin._validate_prompt = lambda *_args: ""
        plugin._is_audit_exempt = lambda *_args: False
        calls = []

        async def current_llm(event, prompt, timeout=8, images=None):
            calls.append((event, timeout, images))
            return '{"allow":true,"reason":""}'

        plugin._call_text_llm = current_llm
        allowed, reason = asyncio.run(plugin._audit_prompt("cat", event=None))

        self.assertTrue(allowed)
        self.assertEqual(reason, "")
        self.assertEqual(calls, [(None, 30, None)])

    def test_output_audit_without_model_uses_current_llm_with_images(self) -> None:
        plugin = self._plugin_stub()
        plugin.config = AICatConfig.from_dict(
            {"image": {"enable_output_audit": True, "output_audit_model": ""}}
        )
        plugin._is_audit_exempt = lambda *_args: False
        calls = []
        with tempfile.NamedTemporaryFile(suffix=".png") as handle:
            handle.write(PNG_BYTES)
            handle.flush()

            async def current_llm(event, prompt, timeout=8, images=None):
                calls.append((event, timeout, images))
                return '{"allow":true,"reason":""}'

            plugin._call_text_llm = current_llm
            allowed, reason = asyncio.run(
                plugin._audit_output_images([handle.name], event=None)
            )

        self.assertTrue(allowed)
        self.assertEqual(reason, "")
        self.assertEqual(calls, [(None, 30, [PNG_BYTES])])

    def test_translation_without_model_uses_current_llm(self) -> None:
        plugin = self._plugin_stub()
        plugin.config = AICatConfig.from_dict(
            {
                "image": {
                    "enable_image_prompt_en": True,
                    "prompt_en_mode": "always",
                    "prompt_en_model": "",
                    "prompt_audit_model": "",
                }
            }
        )
        calls = []

        async def current_llm(event, prompt, timeout=8, images=None):
            calls.append((event, timeout, images))
            return '{"ok":true,"en":"a natural portrait"}'

        plugin._call_text_llm = current_llm
        translated, meta = asyncio.run(
            plugin._translate_prompt_to_english("一位自然的人像", event=None)
        )

        self.assertEqual(translated, "a natural portrait")
        self.assertTrue(meta["applied"])
        self.assertEqual(meta["model"], "astrbot")
        self.assertEqual(calls, [(None, 30, None)])

    def test_image_generation_converts_only_enabled_targets_to_text_requests(self) -> None:
        plugin = self._plugin_stub()
        from astrbot_plugin_selfie_image import main as plugin_main

        plugin.config = AICatConfig.from_dict(
            {
                "image_channels": [
                    {
                        "name": "image",
                        "provider_type": "openai",
                        "base_url": "https://image.test",
                        "api_key": "sk-test",
                        "enabled_models": ["describe-first", "plain-edit"],
                        "image_to_text_models": ["describe-first"],
                    }
                ]
            }
        )
        targets = plugin.config.get_prioritized_targets()
        reference = ImageReference(data=PNG_BYTES, mime_type="image/png")
        captured = []

        plugin._save_reference_images_to_cache = lambda _refs: []
        plugin._source_context = lambda *_args, **_kwargs: {}
        plugin._cleanup_image_cache_if_needed = lambda _paths: {}
        plugin._composition_metadata = lambda *_args, **_kwargs: {}
        plugin._record_task = lambda _record: None
        plugin._record_channel_health = lambda _attempts: None
        plugin._semaphore = asyncio.Semaphore(1)

        async def describe(_event, images):
            self.assertEqual(images, [PNG_BYTES])
            return "红衣人物，室内暖光", {"enabled": True, "applied": True, "model": "astrbot"}

        async def audit(_prompt, _user_id, _event):
            return True, ""

        async def fake_generate(selected, request, session, **kwargs):
            factory = kwargs["request_factory"]
            for target in selected:
                prepared = factory(target)
                captured.append((target.model, prepared.prompt, len(prepared.images)))
            return ImageGenerateResult(error="test stop", attempts=[])

        plugin._describe_reference_images_for_generation = describe
        plugin._audit_prompt = audit
        plugin._prompt_en_needed = lambda *_args, **_kwargs: False

        with patch.object(plugin_main, "generate_image_with_fallback", side_effect=fake_generate):
            result = asyncio.run(
                plugin._run_image_generation(
                    "保持人物神态",
                    "9:16",
                    "1K",
                    [reference],
                    targets=targets,
                    event=object(),
                )
            )

        self.assertFalse(result["success"])
        self.assertEqual(
            captured,
            [
                ("describe-first", "保持人物神态\n\n参考图内容描述：红衣人物，室内暖光", 0),
                ("plain-edit", "保持人物神态", 1),
            ],
        )

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
        self.assertIn("已立即取消", msg)
        self.assertEqual(plugin._web_tasks["cmd-1"]["status"], "cancelled")
        with self.assertRaises(PermissionError):
            plugin.cancel_image_task("cmd-2", session_key="group:a")
        msg2 = plugin.cancel_image_task("cmd-2", session_key="group:b")
        self.assertIn("已立即取消", msg2)
        self.assertTrue(plugin._web_tasks["cmd-2"]["cancel_requested"])
        self.assertEqual(plugin._web_tasks["cmd-2"]["status"], "cancelled")
        listed = plugin._list_image_tasks_for_session("group:b", include_finished=False)
        self.assertEqual(listed, [])


class ReferenceCollectorTests(unittest.TestCase):
    def test_extract_buckets_message_quote_at_and_forward(self) -> None:
        from astrbot_plugin_selfie_image.features.reference_collector import (
            CollectedReferences,
            dedupe_image_references,
            extract_structured_image_sources,
            filter_bot_avatar_sources,
        )
        from astrbot_plugin_selfie_image.core.providers import ImageReference

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
        from astrbot_plugin_selfie_image.features.reference_collector import extract_structured_image_sources

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

    def test_context_fallback_is_opt_in_for_cos_style_collection(self) -> None:
        from astrbot_plugin_selfie_image.features.reference_collector import ReferenceCollector

        class Event:
            message_obj = None
            message = None
            raw_message = None

        collector = ReferenceCollector(
            max_bytes=1024 * 1024,
            context_sources=["https://cdn.example/old-1.png", "https://cdn.example/old-2.png"],
            context_hint="上一张换成 COS",
            allow_context_fallback=False,
            looks_like_context_ref=lambda text: "上一张" in text,
        )
        buckets = collector.collect_source_buckets(Event())
        self.assertEqual(buckets["context"], [])

        explicit = ReferenceCollector(
            max_bytes=1024 * 1024,
            context_sources=["https://cdn.example/old.png"],
            context_hint="看看COS",
            allow_context_fallback=False,
        )
        explicit_buckets = explicit.collect_source_buckets(Event())
        self.assertEqual(explicit_buckets["context"], [])

    def test_explicit_current_message_image_remains_message_reference(self) -> None:
        from astrbot_plugin_selfie_image.features.reference_collector import ReferenceCollector

        class Image:
            def __init__(self):
                self.url = "https://cdn.example/current.png"
                self.path = ""

        class MessageObj:
            def __init__(self):
                self.message = [Image()]
                self.quote = None

        class Event:
            def __init__(self):
                self.message_obj = MessageObj()
                self.message = None
                self.raw_message = None

        collector = ReferenceCollector(
            max_bytes=1024 * 1024,
            context_sources=["https://cdn.example/old.png"],
            context_hint="看看COS",
            allow_context_fallback=False,
        )
        buckets = collector.collect_source_buckets(Event())
        self.assertEqual(buckets["message"], ["https://cdn.example/current.png"])
        self.assertEqual(buckets["context"], [])

    def test_image_file_is_used_when_path_has_empty_default(self) -> None:
        from astrbot_plugin_selfie_image.features.reference_collector import extract_structured_image_sources

        class Image:
            def __init__(self):
                self.file = "file:///storage/emulated/0/Pictures/QQ/original.png"
                self.path = ""
                self.url = "https://cdn.example/transcoded.png"

        class MessageObj:
            def __init__(self):
                self.message = [Image()]
                self.quote = None

        class Event:
            def __init__(self):
                self.message_obj = MessageObj()
                self.message = None
                self.raw_message = None

        buckets = extract_structured_image_sources(Event())
        self.assertEqual(
            buckets["message"],
            [
                "file:///storage/emulated/0/Pictures/QQ/original.png",
            ],
        )
        alternates = extract_structured_image_sources(Event(), include_image_alternates=True)
        self.assertEqual(
            alternates["message"],
            [
                "file:///storage/emulated/0/Pictures/QQ/original.png",
                "https://cdn.example/transcoded.png",
            ],
        )

    def test_raw_onebot_image_segment_is_extracted(self) -> None:
        from astrbot_plugin_selfie_image.features.reference_collector import extract_structured_image_sources

        class Event:
            message_obj = None
            message = None
            raw_message = {
                "post_type": "message",
                "message": [
                    {
                        "type": "image",
                        "data": {
                            "file": "qq-file-id",
                            "url": "https://cdn.example/original.png",
                        },
                    }
                ],
            }

        buckets = extract_structured_image_sources(Event(), include_image_alternates=True)
        self.assertEqual(
            buckets["message"],
            ["qq-file-id", "https://cdn.example/original.png"],
        )

    async def _collect_with_onebot_image_resolution(self, local_path: str):
        from astrbot_plugin_selfie_image.features.reference_collector import ReferenceCollector

        class Image:
            file = "qq-file-id"
            url = "https://cdn.example/transcoded.png"
            path = ""

        class MessageObj:
            def __init__(self):
                self.message = [Image()]
                self.quote = None

        class Api:
            def __init__(self):
                self.calls = []

            async def call_action(self, action, **params):
                self.calls.append((action, params))
                if action == "get_image" and params == {"file": "qq-file-id"}:
                    return {"file": local_path}
                raise RuntimeError("unsupported action")

        class Event:
            def __init__(self):
                self.message_obj = MessageObj()
                self.message = None
                self.raw_message = None
                self.bot = type("Bot", (), {"api": Api()})()

        collector = ReferenceCollector(
            max_bytes=1024 * 1024,
            include_image_alternates=True,
        )
        event = Event()
        return await collector.collect(event, FakeSession(get_data=PNG_BYTES)), event.bot.api.calls

    def test_onebot_get_image_resolution_prefers_returned_local_bytes(self) -> None:
        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "original.png"
                path.write_bytes(PNG_BYTES)
                collected, calls = await self._collect_with_onebot_image_resolution(str(path))
                self.assertEqual(collected.message[0].data, PNG_BYTES)
                self.assertEqual(calls[0], ("get_image", {"file": "qq-file-id"}))

        asyncio.run(run())


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
        self.assertIn("openai_chat", self.html)
        self.assertIn("IMAGE_PROVIDER_LABELS", self.html)
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
        from astrbot_plugin_selfie_image.webui.web import render_index_html

        rendered = render_index_html()
        self.assertIn("data:image/png;base64,", rendered)
        self.assertNotIn("__SELFIE_LOGO_SRC__", rendered)

    def test_dashboard_api_registers_token_free_routes(self) -> None:
        from astrbot_plugin_selfie_image.webui.dashboard_api import SelfieImageDashboardAPI
        from astrbot_plugin_selfie_image.core.constants import PLUGIN_NAME

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
        self.assertTrue(any(p.endswith("/cos-look-sets") for p in paths))
        # dual registration for bridge compatibility
        self.assertGreaterEqual(len(registered), 20)

    def test_openai_fast_path_and_trust_env_false_still_present(self) -> None:
        providers = Path(__file__).resolve().parents[1] / "core/providers.py"
        main = Path(__file__).resolve().parents[1] / "main.py"
        parser = Path(__file__).resolve().parents[1] / "core/provider_parser.py"
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
        for name in ("cmd_help", "cmd_help_text", "cmd_draw", "cmd_image_model", "cmd_image_tasks", "cmd_image_task_cancel", "cmd_video", "cmd_t2v", "cmd_i2v", "cmd_persona_video"):
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
            "cmd_video": "没图默认按文字生成",
            "cmd_t2v": "也不使用形象图",
            "cmd_i2v": "不会自动使用当前形象图",
            "cmd_persona_video": "当前形象图作为首帧",
            "cmd_selfie": "用当前形象自拍",
            "cmd_group_selfie": "自己使用当前形象",
            "cmd_persona_set": "自动 / 真人 / 动漫",
            "cmd_view_prompt": "引用一张图片",
            "cmd_reverse_image_prompt": "当前聊天 LLM 反推",
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
        self.assertIn("没图=文生视频", help_body)
        self.assertIn("/形象视频", help_body)
        self.assertIn("/画 3", help_body)
        self.assertIn("/自拍 3", help_body)
        self.assertIn("新任务自动排队", help_body)
        self.assertIn("同时画几张", help_body)
        self.assertIn("/查看提示词", help_body)
        self.assertIn("/查看生图提示词", help_body)
        llm_selfie = main_src.split("async def _run_llm_selfie_flow", 1)[1].split("def _build_success_text", 1)[0]
        self.assertIn("_background_selfie_batches", llm_selfie)
        self.assertNotIn("for _ in range(requested_count)", llm_selfie)
        llm_image = main_src.split("async def tool_generate_image", 1)[1].split("async def tool_generate_selfie", 1)[0]
        self.assertIn("_background_draw_batches", llm_image)
        selfie_batch = main_src.split("async def _background_selfie_batches", 1)[1].split("def _validate_web_test_selection", 1)[0]
        self.assertIn("for index in range(total)", selfie_batch)
        self.assertIn("_ensure_image_batch_gate", selfie_batch)
        self.assertIn("_run_counted_generation_shots", selfie_batch)
        self.assertIn("_run_selfie_batches_unlocked", selfie_batch)
        self.assertNotIn("_run_generation_jobs_parallel", main_src)
        self.assertIn("_run_counted_generation_shots", main_src)

    def test_anatomy_constraints_ban_third_limb_and_same_side_pairs(self) -> None:
        from astrbot_plugin_selfie_image.features.persona import PersonaManager, anatomy_constraint_lines

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
            self.assertIn("服装局部展示", legs)
            self.assertIn("自然的下半身服装局部", legs)
            self.assertIn("成年人物", legs)
            self.assertIn("服装的颜色、材质、层次", legs)
            self.assertIn("自然坐姿、跪坐、侧躺、抱膝、交叠坐姿、窗边坐或席地屈膝", legs)
            self.assertNotIn("晒腿", legs)
            self.assertNotIn("主要看腿形", legs)
            self.assertNotIn("不露脸", legs)
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
        self.assertIn("姿态放松自然", legs_action)
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
            self.assertIn("服装局部展示", prompt)
            self.assertNotIn("勒进大腿肉", prompt)
            self.assertNotIn("微胖软肉", prompt)
            self.assertNotIn("不要大象腿猪腿", prompt)
            self.assertNotIn("【合影 / 同框模式】", prompt)
            self.assertIn("画面只有主角一人", prompt)

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
            self.assertIn("【cam:", cos_action)
            rem = next(x for x in plugin_main.COS_LOOK_SETS if x["id"] == "rem_blue_lolita")
            forced = plugin_main.SelfieImagePlugin._build_cos_look_action(_P(), rem["prompt"], False)
            forced_intent = manager.analyze_selfie_intent(forced)
            self.assertFalse(forced_intent.is_legs_only, forced)
            self.assertTrue(forced_intent.is_cos_look)
            cos_prompt = manager.build_selfie_prompt(forced, "小助", "温柔", True, 0)
            self.assertNotIn("晒腿模式", cos_prompt)
            self.assertTrue("COS换装自拍模式" in cos_prompt or "COS换装他拍模式" in cos_prompt, cos_prompt)
            self.assertIn("看看COS", cos_prompt)
            self.assertIn("换装", cos_prompt)
            selfie_action = plugin_main.SelfieImagePlugin._build_cos_look_action(_P(), "自拍", False, camera="selfie")
            selfie_intent = manager.analyze_selfie_intent(selfie_action)
            self.assertTrue(selfie_intent.is_cos_look)
            self.assertFalse(selfie_intent.is_third_person_photo, selfie_action)
            selfie_prompt = manager.build_selfie_prompt(selfie_action, "小助", "温柔", True, 0)
            self.assertIn("COS换装自拍模式", selfie_prompt)
            self.assertIn("对镜", selfie_prompt)
            third_action = plugin_main.SelfieImagePlugin._build_cos_look_action(_P(), "他拍", False, camera="third")
            third_intent = manager.analyze_selfie_intent(third_action)
            self.assertTrue(third_intent.is_cos_look)
            self.assertTrue(third_intent.is_third_person_photo, third_action)
            third_prompt = manager.build_selfie_prompt(third_action, "小助", "温柔", True, 0)
            self.assertIn("COS换装他拍模式", third_prompt)
            self.assertIn("别人视角的单人成品照", third_prompt)
            self.assertIn("不要第二个人", third_prompt)
            self.assertIn("不要拍到拍摄设备或拍摄过程", third_prompt)
            self.assertNotIn("手机", third_prompt)
            self.assertNotIn("站在穿衣镜前", third_prompt)
            self.assertNotIn("朋友在旁边", third_prompt)
            # auto appearance: no forced real/anime style line
            manager.set_appearance_type("auto")
            auto_prompt = manager.build_selfie_prompt(legs_action, "小助", "温柔", True, 0)
            self.assertNotIn("形象是真人", auto_prompt)
            self.assertNotIn("形象是动漫人物", auto_prompt)
            self.assertNotIn("形象类型：", auto_prompt)

    def test_appearance_type_auto_real_anime_prompt_injection(self) -> None:
        from astrbot_plugin_selfie_image.features.persona import PersonaManager

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
        from astrbot_plugin_selfie_image.features.persona import PersonaManager

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
        from astrbot_plugin_selfie_image.features.persona import PersonaManager

        with tempfile.TemporaryDirectory() as tmp:
            manager = PersonaManager(tmp)
            text = manager.build_selfie_prompt(
                action="看看腿",
                bot_name="小助",
                personality="温柔",
                has_reference_image=True,
                extra_reference_count=0,
            )
            self.assertIn("服装局部展示", text)
            self.assertIn("自然的下半身服装局部", text)
            self.assertIn("成年人物", text)
            self.assertIn("不透明", text)
            self.assertIn("服装的颜色、材质、层次", text)
            self.assertNotIn("晒腿", text)
            self.assertNotIn("主要看腿形", text)
            self.assertNotIn("脚部画外", text)
            self.assertNotIn("双脚裁出画外", text)
            self.assertNotIn("不露脸", text)
            for forbidden in ("过膝袜", "长筒袜", "肉色丝袜", "丝袜", "勒进大腿肉", "半透明", "赤足", "碰脚", "脚趾自然清晰", "完整包住脚部"):
                self.assertNotIn(forbidden, text)
            self.assertIn("腿部穿搭只允许光腿神器、白丝或黑丝三选一", text)
            self.assertIn("禁止中筒袜、短袜", text)
            self.assertNotIn("微胖软肉", text)
            self.assertNotIn("不要大象腿猪腿", text)
            self.assertNotIn("主姿势在多种日常拍腿姿势间变化", text)
            self.assertNotIn("· 坐姿拍腿", text)
            self.assertNotIn("小皮鞋", text)
            self.assertNotIn("居家拖鞋", text)

    def test_daily_profile_does_not_add_unselected_legwear(self) -> None:
        from astrbot_plugin_selfie_image.features.persona import fallback_daily_profile

        for _ in range(30):
            outfit = fallback_daily_profile("2026-08-09", "seed").outfit
            for forbidden in ("短袜", "居家袜", "中筒袜", "堆堆袜", "过膝袜"):
                self.assertNotIn(forbidden, outfit)

    def test_look_you_and_selfie_persona_have_variety_hints(self) -> None:
        from astrbot_plugin_selfie_image.features.persona import PersonaManager

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
        from astrbot_plugin_selfie_image.core.models import AICatConfig, preflight_video_channel

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
        from astrbot_plugin_selfie_image.core.models import AICatConfig

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
        from astrbot_plugin_selfie_image.webui.web import INDEX_HTML

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
        from astrbot_plugin_selfie_image.core.models import (
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
        self.assertEqual(normalize_video_provider_type("video_chat"), "video_chat")
        self.assertEqual(normalize_video_provider_type("openai_chat"), "")  # image protocol
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
        from astrbot_plugin_selfie_image.generation.video import (
            VideoGenerateRequest,
            _agnes_payload,
            _agnes_video_family,
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

        target25 = SimpleNamespace(model="agnes-video-2.5")
        reference = ImageReference(data=PNG_BYTES, source_url="https://cdn.example/ref.png")
        payload25 = _agnes_payload(
            target25,
            VideoGenerateRequest(
                prompt="animate this",
                duration=9,
                size="9:16",
                extra={"size": "1080P", "seconds": "7"},
                images=[reference],
            ),
            ["data:image/png;base64,AAAA"],
            [reference],
        )
        self.assertEqual(_agnes_video_family(target25.model), "v25")
        self.assertEqual(payload25["mode"], "keyframe")
        self.assertEqual(payload25["seconds"], "7")
        self.assertEqual(payload25["size"], "1080P")
        self.assertEqual(payload25["aspect_ratio"], "9:16")
        self.assertEqual(payload25["first_frame"], "https://cdn.example/ref.png")
        self.assertNotIn("num_frames", payload25)
        self.assertNotIn("frame_rate", payload25)

        target_flash = SimpleNamespace(model="agnes-video-2.5-flash")
        payload_flash = _agnes_payload(
            target_flash,
            VideoGenerateRequest(
                prompt="reference mode",
                extra={
                    "mode": "reference",
                    "size": "2K",
                    "images": [f"https://cdn.example/{i}.png" for i in range(7)],
                    "audios": [f"https://cdn.example/{i}.mp3" for i in range(5)],
                    "videos": [{"url": "https://cdn.example/input.mp4"}],
                },
            ),
            [],
        )
        self.assertEqual(_agnes_video_family(target_flash.model), "v25_flash")
        self.assertEqual(payload_flash["size"], "720P")
        self.assertEqual(len(payload_flash["images"]), 5)
        self.assertEqual(len(payload_flash["audios"]), 3)
        self.assertNotIn("videos", payload_flash)


    def test_video_endpoint_and_extractors(self) -> None:
        from astrbot_plugin_selfie_image.generation.video import (
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
        self.assertIn('@filter.command("形象视频")', main_src)
        self.assertIn("视频：", main_src)

    def test_video_proxy_covers_polling_and_download(self) -> None:
        video_src = (Path(__file__).resolve().parents[1] / "generation/video.py").read_text(encoding="utf-8")
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

    def test_video_persona_reference_is_explicit_across_entry_points(self) -> None:
        from astrbot_plugin_selfie_image import main as plugin_main

        root = Path(__file__).resolve().parents[1]
        main_src = (root / "main.py").read_text(encoding="utf-8")
        reference_src = (root / "features/reference_media.py").read_text(encoding="utf-8")
        self.assertTrue(
            hasattr(plugin_main.SelfieImagePlugin, "_video_persona_reference")
        )
        self.assertIn("def _video_persona_reference", reference_src)
        for token in (
            "@LLM_TOOL(name=\"generate_video\")",
            "tool_generate_video",
            "_video_prompt_requests_persona",
            "persona_ref = self._configured_video_persona_reference()",
            "refs = [persona_ref]",
            "if payload.get(\"use_selfie_reference\") and not refs:",
            "valid_targets: List[ImageModelTarget] = []",
            "targets = valid_targets",
        ):
            self.assertIn(token, main_src)
        for html in (INDEX_HTML, (Path(__file__).resolve().parents[1] / "pages/dashboard/index.html").read_text(encoding="utf-8")):
            self.assertIn("TEST_MODE !== 't2v'", html)
            self.assertIn("use_selfie_reference: TEST_MODE !== 't2v'", html)
            self.assertIn("勾选后使用当前形象参考", html)

    def test_video_prompt_persona_intent_requires_explicit_self_reference(self) -> None:
        from astrbot_plugin_selfie_image import main as plugin_main

        matcher = plugin_main.SelfieImagePlugin._video_prompt_requests_persona
        self.assertTrue(matcher("我出镜跳一段舞"))
        self.assertTrue(matcher("使用当前形象，保持我的脸"))
        self.assertTrue(matcher("自拍视频，镜头推进"))
        self.assertTrue(matcher("让你出镜跳一段舞"))
        self.assertFalse(matcher("一只小猫在草地上奔跑"))
        self.assertFalse(matcher("不要使用形象图，纯文字生成"))
        self.assertFalse(matcher("不要使用当前形象，纯文生视频"))


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
        expected_poses = {
            "sofa_front_crop", "chair_side_crop", "sofa_cross_crop",
            "floor_knees_crop", "sofa_occlusion_crop", "stool_edge_crop",
            "floor_side_kneel_crop", "seat_knees_cross_crop",
        }
        for _ in range(360):
            t = plugin_main.SelfieImagePlugin._build_leg_focus_action(_P(), "", False)
            self.assertIn("【legs:outfit】", t)
            self.assertIn("成年人物", t)
            self.assertIn("保持近距离下半身构图", t)
            self.assertIn("【姿势池·", t)
            self.assertIn("上半身", t)
            self.assertIn("本次服装搭配", t)
            self.assertNotIn("晒腿", t)
            self.assertNotIn("主要看腿形", t)
            self.assertNotIn("不露脸", t)
            self.assertNotIn("双脚完整裁出画外", t)
            m = re.search(r"【pose:([a-z_]+)】", t)
            if m:
                pose = m.group(1)
                found.add(pose)
                self.assertIn(pose, expected_poses)
        for key in expected_poses:
            self.assertTrue(any(p == key for p in found), f"missing pose family {key} in {found}")
        self.assertNotIn("stand_topdown", found)
        forced_crop = None
        with patch(
            "astrbot_plugin_selfie_image.cos.leg_focus.pick_leg_focus_pose",
            return_value={
                "id": "sofa_front_crop",
                "title": "沙发正坐",
                "prompt": "画面严格只拍腰部以下，上半身完全在画面外。",
            },
        ), patch("astrbot_plugin_selfie_image.main.random.choices", return_value=["白丝"]):
            forced_crop = plugin_main.SelfieImagePlugin._build_leg_focus_action(_P(), "", False)
        self.assertIn("【pose:sofa_front_crop】", forced_crop)
        self.assertIn("【姿势池·沙发正坐】", forced_crop)
        self.assertIn("【legs:outfit】", forced_crop)
        self.assertIn("白色不透白丝", forced_crop)
        self.assertIn("从大腿上部沿可见腿部连续向下覆盖", forced_crop)
        self.assertIn("不把膝关节或小腿作为固定裁切线", forced_crop)
        self.assertNotIn("脚部画外", forced_crop)
        from astrbot_plugin_selfie_image.features.persona import PersonaManager
        from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt
        with tempfile.TemporaryDirectory() as tmp:
            final_crop = PersonaManager(tmp).build_selfie_prompt(forced_crop, "小助", "温柔", True, 0)
        self.assertIn("服装局部展示", final_crop)
        self.assertNotIn("换装要求", final_crop)
        self.assertIn("自然的下半身服装局部", final_crop)
        self.assertIn("本张使用看看腿随机姿势池条目", final_crop)
        self.assertIn("上半身完全在画面外", final_crop)
        self.assertIn("白色不透白丝（从大腿上部沿可见腿部连续向下覆盖", final_crop)
        self.assertIn("禁止中筒袜、短袜", final_crop)
        self.assertIn("自然延伸到画面外", final_crop)
        self.assertIn("合理遮挡", final_crop)
        self.assertIn("禁止在膝关节、小腿中段或脚踝附近突然终止", final_crop)
        self.assertIn("袜筒下缘直接当作小腿终点", final_crop)
        for conflict in ("脚趾五个分开", "身体从入镜部位连续到脚", "包住整脚到脚趾", "勒进大腿肉", "晒腿", "主要看腿形"):
            self.assertNotIn(conflict, final_crop)
        self.assertNotIn("微胖软肉", final_crop)
        self.assertNotIn("不要大象腿猪腿", final_crop)
        final_crop_en = build_selfie_builtin_prompt(forced_crop, language="en", has_reference_image=True)
        self.assertIn("natural close lower-body outfit detail", final_crop_en)
        self.assertIn("everyday clothing", final_crop_en)
        self.assertIn("opaque white thigh-high stockings, continuous down every visible part of the legs", final_crop_en)
        self.assertIn("mid-calf socks", final_crop_en)
        with patch(
            "astrbot_plugin_selfie_image.main.random.choice",
            side_effect=lambda values: values[0],
        ):
            selfie_action = plugin_main.SelfieImagePlugin._build_leg_focus_action(_P(), "", False)
        with patch(
            "astrbot_plugin_selfie_image.main.random.choice",
            side_effect=lambda values: values[-1],
        ):
            third_action = plugin_main.SelfieImagePlugin._build_leg_focus_action(_P(), "", False)
        self.assertIn("【cam:selfie】", selfie_action)
        self.assertIn("【cam:third】", third_action)
        self.assertIn("第一人称手机自拍", selfie_action)
        self.assertIn("第三人称摄影照片", third_action)
        for camera_kind, zh_label, zh_phrase, en_phrase in (
            ("selfie", "第一人称手机自拍", "第一人称手机自拍", "First-person phone selfie"),
            ("third", "第三人称摄影", "第三人称摄影照片", "Third-person candid outfit photo"),
        ):
            action = f"看看腿。下半身特写。 【cam:{camera_kind}】 【pose:sit_crop】"
            with tempfile.TemporaryDirectory() as tmp:
                camera_prompt = PersonaManager(tmp).build_selfie_prompt(
                    action, "小助", "温柔", True, 0
                )
            self.assertIn(zh_label, camera_prompt)
            self.assertIn(zh_phrase, camera_prompt)
            self.assertIn("椅上或沙发自然坐姿", camera_prompt)
            camera_prompt_en = build_selfie_builtin_prompt(
                action, language="en", has_reference_image=True
            )
            self.assertIn(en_phrase, camera_prompt_en)
            other_en_phrase = (
                "Third-person candid outfit photo"
                if camera_kind == "selfie"
                else "First-person phone selfie"
            )
            self.assertNotIn(other_en_phrase, camera_prompt_en)
        filtered = plugin_main.SelfieImagePlugin._build_leg_focus_action(_P(), "短袜 过膝袜 肉丝 清晨", False)
        self.assertIn("清晨", filtered)
        filtered_extra = re.split(r"用户补充要求优先[:：]", filtered, maxsplit=1)[-1]
        for forbidden in ("短袜", "过膝袜", "肉丝"):
            self.assertNotIn(forbidden, filtered_extra)
        neutralized = plugin_main.SelfieImagePlugin._build_leg_focus_action(
            _P(), "侧躺 跪坐 掀衣摆 不露脸 主要看腿形 短裙", False
        )
        self.assertIn("侧躺", neutralized)
        self.assertIn("跪坐", neutralized)
        self.assertIn("整理衣摆", neutralized)
        self.assertIn("服装局部取景", neutralized)
        self.assertIn("重点看服装版型", neutralized)
        for risky in ("掀衣摆", "不露脸", "主要看腿形", "短裙"):
            self.assertNotIn(risky, neutralized)

        self.assertEqual(set(plugin_main.LEGWEAR_PROMPTS), {"光腿神器", "白丝", "黑丝"})
        bare_leg = plugin_main.LEGWEAR_PROMPTS["光腿神器"]
        self.assertIn("自然肤色光腿神器", bare_leg)
        self.assertNotIn("主要看腿形", bare_leg)
        self.assertNotIn("脚趾", bare_leg)
        for name in ("白丝", "黑丝"):
            text = plugin_main.LEGWEAR_PROMPTS[name]
            self.assertIn("不透", text)
            self.assertIn(name, text)
            self.assertIn("从大腿上部沿可见腿部连续向下覆盖", text)
            self.assertIn("袜口", text)
            self.assertNotIn("主要看腿形", text)
            self.assertNotIn("脚部", text)
            self.assertNotIn("微胖软肉", text)
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
            cam = re.search(r"【cam:(selfie|third)】", t)
            self.assertTrue(cam, t)
        self.assertGreaterEqual(len(ids), 3, ids)
        self.assertEqual(len(plugin_main.COS_LOOK_SETS), 122)
        web_pool = plugin_main.SelfieImagePlugin.list_cos_look_sets_for_web(_P())
        self.assertEqual(len(web_pool), 122)
        self.assertEqual(
            [(item["id"], item["title"], item["prompt"]) for item in web_pool],
            [(item["id"], item["title"], item["prompt"]) for item in plugin_main.COS_LOOK_SETS],
        )
        titles = {x["title"] for x in plugin_main.COS_LOOK_SETS}
        self.assertEqual(
            titles,
            {
                "洛琪希·奶油睡衣",
                "古风·齐胸汉服·桃粉",
                "古风·薄荷粉纱汉服",
                "蕾姆·蓝白女仆洛丽塔",
                "白细肩带迷你裙",
                "海月·白蓝花瓣水母",
                "西施·同人短旗袍",
                "蓝梦·龙之道",
                "朵莉亚·海洋荷叶裙",
                "殷紫萍·银白短旗袍",
                "甘雨·花嫁",
                "玉玲珑·金狐",
                "貂蝉·三国杀",
                "铂金发白蕾丝长裙",
                "西施·青绿渐变旗袍",
                "黄结白围裙女仆",
                "古风·汉服·银紫深V广袖",
                "古风·露背蓝纱古装",
                "姬小满·黑金橙短装",
                "西施·诗语江南",
                "公孙离·离恨烟",
                "西施·露腰短旗袍",
                "影·黑白红短装",
                "露莎·白金短装",
                "秧秧·蓝白碎花泳装",
                "卡提希娅·黑裙飞鸟",
                "古风·青绿双辫广袖长裙",
                "尤诺·金粉轻甲",
                "古风·汉服挂脖肚兜套装",
                "满穗·灰白和风",
                "小乔·白熊围巾",
                "芙宁娜·奶油浅蓝荷叶裙",
                "小舞·浅粉白兔",
                "王昭君·旧版原皮蓝白",
                "玉玲珑·喜缘红线",
                "二次元少女·橙青半透",
                "瑶·薄荷冰蓝短裙",
                "露娜·紫霞仙子",
                "古风·浅蓝花卉挂脖兜兜",
                "神里绫华·白袖蓝袴",
                "古风·青白水晶挂脖套装",
                "杨玉环·银翎春语",
                "少司缘·红金宽袖",
                "古风·青绿花卉短襦裙",
                "古风·淡粉月夜薄纱古装",
                "白发·黑结挂脖围裙",
                "古风·青红双丸子烟斗",
                "古风·粉蓝花卉立领长裙",
                "和泉纱雾·粉色家居服",
                "小乔·丁香结",
                "公孙离·玉兔公主",
                "貂蝉·原皮",
                "西施·乘鲤谣",
                "西施·续相思",
                "宵宫",
                "小乔·线条小狗",
                "雷电将军",
                "芙宁娜·白蓝礼服",
                "胡桃·往生堂黑红",
                "刻晴·紫色短礼服",
                "甘雨·冰蓝试衣",
                "可莉·红白童趣",
                "宵宫·烟花祭典",
                "永雏塔菲·粉白黄宅舞服",
                "古拉·小鲨鱼",
                "小乔·少御粉色短装",
                "瑶·小鹿主题",
                "和泉纱雾·粉绿印花短装",
                "芭芭拉·白色偶像礼服",
                "姬小满·黄黑坐姿短装",
                "Saber·青花瓷短装",
                "银狼·蓝黑街头短装",
                "甘雨·蓝白麒麟装",
                "少司缘·红绿宅舞宽袖",
                "穹·电竞房白色短裙",
                "嫦娥·落星盏金月短装",
                "露娜·霜月吟冰蓝宽袖",
                "少萝·粉白兔系短装",
                "海月·水母薄纱试衣",
                "公孙离·离恨烟展袖",
                "公孙离·玉兔公主花辫",
                "孙尚香·荧光绿短装",
                "云缨·红白黑短装",
                "少司缘·三星堆青绿金饰",
                "少司缘·红绿超大宽袖",
                "小乔·战国袍",
                "妲己·狐耳黑金短装",
                "公孙离·白绿金宽袖",
                "爱弥斯·黑白蓝坐姿",
                "洛琪希·露肩短睡裙",
                "卡提希娅·白黑蓝短装",
                "水兰儿·羊角提花短旗袍",
                "胡桃·龙之道·至纯",
                "胡桃·鲤梦浮光",
                "胡桃·春日序曲·晨风",
                "胡桃·怦然心动·软暖甜梦",
                "胡桃·夏日派对·高校水着",
                "胡桃·春桃笑",
                "殷紫萍·春日序曲·朝颜",
                "殷紫萍·绛璇仙子",
                "殷紫萍·兰庭月",
                "殷紫萍·古岛异闻·永夜幽华",
                "殷紫萍·2674·幻械姬",
                "殷紫萍·诗画四友·春鸠梅",
                "宁红夜·赤皓新囍",
                "宁红夜·西行劫·蜘蛛精",
                "宁红夜·封神录·云霄娘娘",
                "宁红夜·冥神",
                "宁红夜·梨园传奇·素贞",
                "宁红夜·龙之道·白知",
                "季莹莹·寂熄祸星",
                "季莹莹·观山海·夫诸",
                "季莹莹·琳琅·观山海·太阳星主",
                "季莹莹·封神录·哪吒",
                "砂狼白子·黑白运动服",
                "菲比·白金圣洁礼服",
                "古风·白金红绸宽袖",
                "丹花伊吹·红白运动装",
                "白色蕾丝·露腰短装",
                "古风·白蓝露背仙纱",
                "C.C.（CC）·皇后装·白虎粉金礼服",
                "辉夜·巫女红白神乐服",
            },
        )
        for item in plugin_main.COS_LOOK_SETS:
            blob = item["title"] + item["prompt"]
            self.assertNotIn("抖音", blob)
            self.assertNotIn("擦边", blob)
            self.assertNotIn("反差", blob)
        roxy = next(x for x in plugin_main.COS_LOOK_SETS if x["id"] == "roxy_cream")
        self.assertIn("《无职转生》洛琪希", roxy["prompt"])
        self.assertIn("跪坐在地毯上", roxy["prompt"])
        self.assertIn("双膝并拢", roxy["prompt"])
        self.assertIn("双腿收拢并拢压在身下", roxy["prompt"])
        self.assertIn("不要盘腿、分腿或站立", roxy["prompt"])
        self.assertIn("禁止蓝色旅行法师外套", roxy["prompt"])
        self.assertIn("双麻花辫", roxy["prompt"])
        self.assertIn("睡衣", roxy["prompt"])
        rem = next(x for x in plugin_main.COS_LOOK_SETS if x["id"] == "rem_blue_lolita")
        self.assertIn("蓝白女仆洛丽塔", rem["title"])
        self.assertIn("罗兹瓦尔宅邸经典蓝白女仆服", rem["prompt"])
        self.assertIn("白色长袖蓬袖衬衣", rem["prompt"])
        self.assertIn("分叉燕尾式深色后摆", rem["prompt"])
        self.assertIn("不要拉姆粉色女仆服", rem["prompt"])
        prompts = {x["id"]: x["prompt"] for x in plugin_main.COS_LOOK_SETS}
        self.assertEqual(
            [item["id"] for item in plugin_main.match_cos_look_sets("宁红夜 赤皓新囍")],
            ["ninghongye_red_hao_new_joy"],
        )
        self.assertEqual(
            [item["id"] for item in plugin_main.match_cos_look_sets("季莹莹 封神录 哪吒")],
            ["jiyingying_nezha"],
        )
        self.assertEqual(
            [item["id"] for item in plugin_main.match_cos_look_sets("砂狼白子")],
            ["shiroko_black_white_tracksuit"],
        )
        self.assertEqual(
            [item["id"] for item in plugin_main.match_cos_look_sets("菲比")],
            ["phoebe_white_gold_sanctuary"],
        )
        self.assertEqual(
            [item["id"] for item in plugin_main.match_cos_look_sets("丹花伊吹")],
            ["ibuki_red_white_sportswear"],
        )
        for query in ("C.C.", "CC", "C.C.皇后装", "CC皇后装"):
            self.assertEqual(
                [item["id"] for item in plugin_main.match_cos_look_sets(query)],
                ["cc_white_tiger_pink_gold_dress"],
            )
        self.assertEqual(plugin_main.match_cos_look_sets("C"), [])
        for query in ("辉夜", "巫女", "辉夜巫女"):
            self.assertEqual(
                [item["id"] for item in plugin_main.match_cos_look_sets(query)],
                ["kaguya_miko_red_white_crown"],
            )
        for ninghongye_id in (
            "ninghongye_red_hao_new_joy",
            "ninghongye_spider_demon",
            "ninghongye_cloud_fairy",
            "ninghongye_underworld_goddess",
            "ninghongye_plain_white_snake",
            "ninghongye_dragon_path_baizhi",
        ):
            self.assertIn("横向红色绸缎仪式眼罩", prompts[ninghongye_id])
            self.assertIn("绝不能露出眼睛", prompts[ninghongye_id])
            self.assertIn("完全不透明", prompts[ninghongye_id])
            self.assertIn("不得透光", prompts[ninghongye_id])
            self.assertIn("眼睛、眼白和眼睑全部不可见", prompts[ninghongye_id])
        sun_star_lord = prompts["jiyingying_sun_star_lord"]
        self.assertIn("白色、浅金色的半透明纱裙片与珠网结构", sun_star_lord)
        self.assertIn("透明长纱裙片", sun_star_lord)
        self.assertIn("赤足", sun_star_lord)
        self.assertNotIn("黑色三角底层", sun_star_lord)
        self.assertNotIn("白色贴身短裤", sun_star_lord)
        self.assertNotIn("脚穿细带凉鞋", sun_star_lord)
        orchid = prompts["yinzi_orchid_courtyard_moon"]
        self.assertIn("双腿裸露不穿袜", orchid)
        self.assertNotIn("白色连裤袜", orchid)
        spring_oriole = prompts["yinzi_spring_oriole_plum"]
        self.assertIn("双腿裸露不穿袜", spring_oriole)
        self.assertNotIn("白色丝袜", spring_oriole)
        warm_dream = prompts["hutao_warm_sweet_dream"]
        self.assertIn("后背大面积露出", warm_dream)
        self.assertIn("细肩带固定", warm_dream)
        cloud_fairy = prompts["ninghongye_cloud_fairy"]
        self.assertIn("双臂大部裸露", cloud_fairy)
        self.assertIn("不形成连贯长袖", cloud_fairy)
        self.assertIn("齐胸抹胸高腰", prompts["hanfu_peach"])
        self.assertIn("外层宽袖薄纱袍", prompts["mint_sheer_hanfu"])
        white = prompts["white_slip_mini"]
        self.assertIn("细肩带", white)
        self.assertIn("裙摆只到大腿中段", white)
        self.assertIn("白色厚底运动鞋", white)
        self.assertNotIn("《", white)
        self.assertNotIn("抖音", white)
        self.assertNotIn("天宫", white)
        haiyue = prompts["haiyue_petal_jelly"]
        self.assertIn("《王者荣耀》海月", haiyue)
        self.assertIn("深甜心领", haiyue)
        self.assertIn("不是原皮宽袖长袍汉服", haiyue)
        self.assertNotIn("抖音", haiyue)
        xishi = prompts["xishi_fan_qipao"]
        self.assertIn("《王者荣耀》西施同人短旗袍", xishi)
        self.assertIn("薄荷白鹿角", xishi)
        self.assertIn("不是原皮长裙水莲汉服", xishi)
        self.assertNotIn("抖音", xishi)
        lanmeng = prompts["lanmeng_dragon_path"]
        self.assertIn("《永劫无间》蓝梦“龙之道”COS", lanmeng)
        self.assertIn("黑金短款露腰服装", lanmeng)
        self.assertIn("白色皮革电竞椅", lanmeng)
        self.assertNotIn("抖音", lanmeng)
        dolia = prompts["dolia_ocean_ruffle"]
        self.assertIn("《王者荣耀》朵莉亚", dolia)
        self.assertIn("白贝壳与红海星", dolia)
        self.assertIn("不是原皮人鱼长尾", dolia)
        self.assertNotIn("抖音", dolia)
        yinzi = prompts["yinzi_white_qipao"]
        self.assertIn("《永劫无间》殷紫萍", yinzi)
        self.assertIn("钥匙孔开窗", yinzi)
        self.assertIn("不是长袍旗袍", yinzi)
        ganyu = prompts["ganyu_bride"]
        self.assertIn("《原神》甘雨花嫁", ganyu)
        self.assertIn("麒麟角", ganyu)
        self.assertIn("不是原皮深蓝金边旗袍", ganyu)
        fox = prompts["yulinglong_gold_fox"]
        self.assertIn("《永劫无间》玉玲珑", fox)
        self.assertIn("狐耳", fox)
        self.assertIn("不是原皮覆盖更多的汉服长袍", fox)
        diaochan = prompts["diaochan_sanguosha"]
        self.assertIn("《三国杀》貂蝉", diaochan)
        self.assertIn("螺旋黑发包", diaochan)
        self.assertIn("薄荷缎裙", diaochan)
        lace = prompts["platinum_lace_gown"]
        self.assertIn("铂金长直发白蕾丝长裙", lace)
        self.assertNotIn("《", lace)
        self.assertNotIn("星穹", lace)
        cyan = prompts["xishi_cyan_qipao"]
        self.assertIn("《王者荣耀》西施这一版青绿渐变旗袍", cyan)
        self.assertIn("白盘扣", cyan)
        self.assertIn("不是鹿角同人短旗袍", cyan)
        maid = prompts["yellow_bow_maid"]
        self.assertIn("大黄蝴蝶结", maid)
        self.assertIn("白围裙", maid)
        self.assertNotIn("永劫", maid)
        self.assertNotIn("宁红夜", maid)
        self.assertNotIn("《", maid)
        deepv = prompts["silver_deepv_hanfu"]
        self.assertIn("交领极低开到胸口", deepv)
        self.assertIn("金绣直襟", deepv)
        self.assertNotIn("《", deepv)
        backless = prompts["blue_backless_hanfu"]
        self.assertIn("四分之三侧身", backless)
        self.assertIn("从颈到腰的整片裸背", backless)
        self.assertIn("只有两条胳膊、两只手", backless)
        self.assertIn("不要第三只手", backless)
        self.assertIn("冰蓝渐变到青绿再到宝蓝", backless)
        self.assertNotIn("《", backless)
        jixiaoman = prompts["jixiaoman_black_gold"]
        self.assertIn("《王者荣耀》姬小满", jixiaoman)
        self.assertIn("黑金橙短装", jixiaoman)
        self.assertIn("短款宽袖外套只到胸下", jixiaoman)
        self.assertIn("整段腰腹露出", jixiaoman)
        self.assertIn("宽袖外黑内亮橙金", jixiaoman)
        self.assertIn("大金六角护甲板", jixiaoman)
        self.assertIn("黑色短裤", jixiaoman)
        self.assertIn("浅紫白辫状长尾饰", jixiaoman)
        self.assertIn("不是黄睡衣家居", jixiaoman)
        self.assertIn("不是黄短裙", jixiaoman)
        shiyu = prompts["xishi_shiyu_jiangnan"]
        self.assertIn("《王者荣耀》西施诗语江南", shiyu)
        self.assertIn("青绿荷叶大结", shiyu)
        self.assertIn("不是鹿角同人短旗袍", shiyu)
        lihen = prompts["gongsunli_lihenyan"]
        self.assertIn("《王者荣耀》公孙离离恨烟", lihen)
        self.assertIn("尖角高发包", lihen)
        self.assertIn("大金螺旋圆徽", lihen)
        self.assertIn("不是大乔", lihen)
        self.assertNotIn("大乔离恨烟", lihen)
        crop = prompts["xishi_crop_qipao"]
        self.assertIn("《王者荣耀》西施这一版露腰短旗袍两件套", crop)
        self.assertIn("只到胸下", crop)
        self.assertIn("不是诗语江南广袖短衣", crop)
        ying = prompts["ying_black_red"]
        self.assertIn("《王者荣耀》影", ying)
        self.assertIn("大红圆宝石", ying)
        self.assertIn("贴身亮黑短裤", ying)
        lusha = prompts["lusha_gold_tiara"]
        self.assertIn("铂金超长直发白金短装", lusha)
        self.assertIn("金交叉胸带", lusha)
        self.assertNotIn("《", lusha)
        yangyang = prompts["yangyang_blue_floral_swim"]
        self.assertIn("《鸣潮》秧秧", yangyang)
        self.assertIn("白底蓝花比基尼", yangyang)
        cart = prompts["cartethyia_black_bird"]
        self.assertIn("《鸣潮》卡提希娅", cart)
        self.assertIn("白绣飞鸟", cart)
        self.assertIn("银枝状脚环", cart)
        yuno = prompts["yuno_gold_pink_armor"]
        self.assertIn("《鸣潮》尤诺", yuno)
        self.assertIn("香槟金光泽胸甲", yuno)
        self.assertIn("粉色沙发或软垫", yuno)
        self.assertNotIn("抖音", yuno)
        dudou = prompts["ancient_hanfu_halter_dudou"]
        self.assertIn("国风汉服挂脖肚兜套装", dudou)
        self.assertIn("花卉蕾丝刺绣", dudou)
        self.assertIn("淡紫色薄纱披肩", dudou)
        self.assertIn("保持自然遮挡", dudou)
        xiaowu = prompts["xiaowu_pink_rabbit"]
        self.assertIn("《斗罗大陆》小舞", xiaowu)
        self.assertIn("浅粉白兔系 COS", xiaowu)
        self.assertIn("银白镂空花纹饰片", xiaowu)
        wangzhaojun = prompts["wangzhaojun_old_blue"]
        self.assertIn("《王者荣耀》王昭君旧版原皮", wangzhaojun)
        self.assertIn("白色毛绒披肩", wangzhaojun)
        self.assertIn("亮蓝色缎面", wangzhaojun)
        red_thread = prompts["yulinglong_red_thread"]
        self.assertIn("《永劫无间》玉玲珑", red_thread)
        self.assertIn("喜缘红线", red_thread)
        self.assertIn("粉红色折扇", red_thread)
        anime_girl = prompts["anime_girl_orange_mint"]
        self.assertIn("橙金与薄荷青配色", anime_girl)
        self.assertIn("粉色扶手椅", anime_girl)
        yao = prompts["yao_mint_blue_dress"]
        self.assertIn("《王者荣耀》瑶", yao)
        self.assertIn("薄荷冰蓝梦幻短裙", yao)
        self.assertIn("小狗毛绒挂件", yao)
        luna = prompts["luna_zixia_fairy"]
        self.assertIn("《王者荣耀》露娜", luna)
        self.assertIn("紫霞仙子", luna)
        self.assertIn("金色蝴蝶形护饰", luna)
        twin = prompts["mint_twin_braid_hanfu"]
        self.assertIn("深棕双麻花辫青绿广袖长裙", twin)
        self.assertIn("粉红花囊", twin)
        self.assertNotIn("《", twin)
        mansui = prompts["mansui_gray_wafu"]
        self.assertIn("满穗风格的灰白色和风 COS 造型", mansui)
        self.assertIn("自然裸腿", mansui)
        self.assertIn("不要中筒袜、短袜或厚重打底袜", mansui)
        xiao_qiao = prompts["xiao_qiao_white_bear"]
        self.assertIn("薄荷绿白熊主题", xiao_qiao)
        self.assertIn("黄色长围巾", xiao_qiao)
        self.assertIn("白色不透明过膝袜或连贯白色腿部服装", xiao_qiao)
        self.assertIn("不要中筒袜、短袜或袜口截断", xiao_qiao)
        self.assertNotIn("客厅电视柜前", xiao_qiao)
        furina = prompts["furina_cream_blue_ruffle"]
        self.assertIn("芙宁娜风格的奶油浅蓝荷叶裙 COS 造型", furina)
        self.assertIn("灰蓝色大蝴蝶结", furina)
        self.assertIn("多层蓝灰色布料和奶油白荷叶边", furina)
        blue_doudou = prompts["ancient_blue_floral_halter_doudou"]
        self.assertIn("轻国风浅蓝花卉挂脖兜兜套装", blue_doudou)
        self.assertIn("圆形镂空和金色古典扣饰", blue_doudou)
        self.assertIn("浅蓝色半透明薄纱短衫", blue_doudou)
        ayaka = prompts["kamisato_ayaka_white_blue_hakama"]
        self.assertIn("《原神》神里绫华", ayaka)
        self.assertIn("粉色水引结蝴蝶结耳饰", ayaka)
        self.assertIn("深海军蓝色褶裙或袴裙", ayaka)
        crystal = prompts["ancient_mint_crystal_halter"]
        self.assertIn("青白薄荷色轻国风挂脖套装", crystal)
        self.assertIn("透明蓝绿色圆球水晶", crystal)
        yangyuhuan = prompts["yangyuhuan_silver_feather"]
        self.assertIn("《王者荣耀》杨玉环“银翎春语”COS", yangyuhuan)
        self.assertIn("银色羽翎装饰", yangyuhuan)
        shaosiyuan = prompts["shaosiyuan_red_gold_sleeves"]
        self.assertIn("《王者荣耀》少司缘红金主题COS", shaosiyuan)
        self.assertIn("极宽的红色长袖", shaosiyuan)
        floral_short = prompts["ancient_mint_floral_short_ruqun"]
        self.assertIn("青绿色花卉古风COS", floral_short)
        self.assertIn("白色花朵发饰", floral_short)
        moon_sheer = prompts["ancient_pink_moon_sheer"]
        self.assertIn("淡粉色月夜薄纱古装", moon_sheer)
        self.assertIn("白色长毛毯铺成的软榻", moon_sheer)
        self.assertIn("一轮明月", moon_sheer)
        sagiri = prompts["izumi_sagiri_pink_loungewear"]
        self.assertIn("《埃罗芒阿老师》和泉纱雾风格", sagiri)
        self.assertIn("面部清晰无遮挡", sagiri)
        self.assertNotIn("手机", sagiri)
        self.assertNotIn("不要裸露", sagiri)
        xiaoqiao_clove = prompts["xiaoqiao_dingxiangjie"]
        self.assertIn("《王者荣耀》小乔“丁香结”", xiaoqiao_clove)
        self.assertIn("紫色大蝴蝶结发饰", xiaoqiao_clove)
        gongsunli_rabbit = prompts["gongsunli_yutu_princess"]
        self.assertIn("《王者荣耀》公孙离“玉兔公主”", gongsunli_rabbit)
        self.assertIn("黄色宽腰封", gongsunli_rabbit)
        diaochan_original = prompts["diaochan_original"]
        self.assertIn("《王者荣耀》貂蝉原皮", diaochan_original)
        self.assertIn("蓝色方形宝石", diaochan_original)
        chengliyiao = prompts["xishi_chengliyiao"]
        self.assertIn("《王者荣耀》西施“乘鲤谣”", chengliyiao)
        self.assertIn("银色大型双螺旋", chengliyiao)
        xuxiangsi = prompts["xishi_xuxiangsi"]
        self.assertIn("《王者荣耀》西施“续相思”", xuxiangsi)
        self.assertIn("复杂的银色、青绿色与粉金色莲花形花冠", xuxiangsi)
        yoimiya = prompts["yoimiya_firework"]
        self.assertIn("《原神》宵宫风格", yoimiya)
        self.assertIn("黑色猫耳或兽耳发饰", yoimiya)
        xiaoqiao_dog = prompts["xiaoqiao_line_dog"]
        self.assertIn("小乔·线条小狗", xiaoqiao_dog)
        self.assertIn("粉色爪印徽章", xiaoqiao_dog)
        raiden = prompts["raiden_shogun_purple"]
        self.assertIn("《原神》雷电将军风格 COS", raiden)
        self.assertIn("紫色长发编织成麻花辫", raiden)
        self.assertIn("雷元素印记", raiden)
        furina_classic = prompts["furina_classic_blue_white"]
        self.assertIn("《原神》芙宁娜经典白蓝礼服 COS", furina_classic)
        self.assertIn("全身镜前", furina_classic)
        hutao = prompts["hutao_wansheng"]
        self.assertIn("《原神》胡桃经典黑红短装 COS", hutao)
        self.assertIn("往生堂礼帽", hutao)
        keqing = prompts["keqing_purple_dress"]
        self.assertIn("《原神》刻晴经典紫色 COS", keqing)
        self.assertIn("猫耳般轮廓", keqing)
        ganyu_classic = prompts["ganyu_ice_blue"]
        self.assertIn("《原神》甘雨经典 COS", ganyu_classic)
        self.assertIn("麒麟角", ganyu_classic)
        klee = prompts["klee_red_clover"]
        self.assertIn("《原神》可莉经典 COS", klee)
        self.assertIn("四叶草", klee)
        self.assertNotIn("儿童角色必须完全穿衣", klee)
        self.assertNotIn("不强调身体曲线", klee)
        self.assertNotIn("不使用性感或成人化姿势", klee)
        yoimiya_festival = prompts["yoimiya_firework_festival"]
        self.assertIn("《原神》宵宫经典 COS", yoimiya_festival)
        self.assertIn("烟花主题发饰", yoimiya_festival)
        taifei = prompts["yongchutafei_pink_yellow_dance"]
        self.assertIn("永雏塔菲风格的粉白黄宅舞COS服", taifei)
        self.assertIn("米白色室内墙面", taifei)
        self.assertIn("巨大的淡粉色蝴蝶结", taifei)
        self.assertNotIn("猫爪手套", taifei)
        gura = prompts["gura_shark"]
        self.assertIn("Hololive Gawr Gura", gura)
        self.assertIn("红白色鲨鱼牙齿图案", gura)
        xiaoqiao_shaoyu = prompts["xiaoqiao_shaoyu_pink"]
        self.assertIn("小乔少御风格", xiaoqiao_shaoyu)
        self.assertIn("白色大荷叶边", xiaoqiao_shaoyu)
        self.assertNotIn("头部、脸部和发型完全不入镜", xiaoqiao_shaoyu)
        yao_deer = prompts["yao_deer_theme"]
        self.assertIn("小鹿主题COS", yao_deer)
        self.assertIn("黑色枝状细角", yao_deer)
        sagiri_print = prompts["izumi_sagiri_pink_green_print"]
        self.assertIn("和泉纱雾风格的粉绿印花短装COS", sagiri_print)
        self.assertIn("固定印有日文「エロマンガ先生」", sagiri_print)
        self.assertNotIn("参考视频", sagiri_print)
        new_cos_looks = {
            "barbara_white_idol_dress": ("《原神》芭芭拉", "白色窗帘"),
            "jixiaoman_yellow_black_seated": ("《王者荣耀》姬小满", "一条腿屈起并横向靠近镜头"),
            "saber_blue_porcelain": ("《Fate》系列Saber", "正上方近距离俯拍"),
            "silver_wolf_blue_black_street": ("《崩坏：星穹铁道》银狼", "彩色反光护目镜"),
            "ganyu_blue_white_qilin_close": ("《原神》甘雨", "黑红渐变麒麟角"),
            "shaosiyuan_red_green_dance": ("《王者荣耀》少司缘", "一只手食指竖起"),
            "qiong_white_gaming_room": ("穹白色电竞房短裙造型", "黑色电竞椅"),
            "change_luoxingzhan_close": ("《王者荣耀》嫦娥", "大型银金色弯月胸甲"),
            "luna_frost_moon_mirror": ("《王者荣耀》露娜", "银色交叉绑带"),
            "shaoluo_pink_white_rabbit": ("少萝粉白兔系短装", "白色兔耳发饰"),
            "haiyue_jellyfish_midshot": ("《王者荣耀》海月", "水母触须状布带"),
            "gongsunli_lihenyan_sleeves": ("《王者荣耀》公孙离“离恨烟”", "双臂水平向左右展开"),
            "gongsunli_yutu_braid_shadow": ("《王者荣耀》公孙离“玉兔公主”", "粗长麻花辫"),
            "sun_shangxiang_neon_green": ("《王者荣耀》孙尚香", "荧光绿色多层不规则荷叶短裙"),
            "yunying_red_white_black": ("《王者荣耀》云缨", "交叉金色皮带"),
            "shaosiyuan_sanxingdui": ("《王者荣耀》少司缘三星堆", "方形近距离肖像"),
            "shaosiyuan_oversized_red_green_sleeves": ("《王者荣耀》少司缘", "宽袖从身体两侧铺满整个画面"),
            "xiaoqiao_warring_states_robe": ("《王者荣耀》小乔“战国袍”", "紫色流苏发簪"),
            "daji_fox_black_gold": ("《王者荣耀》妲己", "俏皮猫爪动作"),
            "gongsunli_white_green_ruffle": ("《王者荣耀》公孙离", "横屏近距离俯拍"),
            "aimisi_black_white_blue_seated": ("《鸣潮》爱弥斯", "胸口至膝部"),
            "roxy_off_shoulder_sleep_dress": ("《无职转生》洛琪希", "奶油白色露肩短睡裙"),
            "cartethyia_white_black_blue_short": ("《鸣潮》卡提希娅", "画面中只有一名人物"),
            "shuilaner_horned_brocade_qipao": ("水兰儿AS109风格", "平视机位和正常拍摄距离"),
            "shiroko_black_white_tracksuit": ("《碧蓝档案》砂狼白子", "黑色拉链运动外套"),
            "phoebe_white_gold_sanctuary": ("《鸣潮》菲比", "白色与浅金色多层短裙"),
        }
        for cos_id, (source, composition) in new_cos_looks.items():
            self.assertIn(source, prompts[cos_id])
            self.assertIn(composition, prompts[cos_id])
            self.assertNotIn("参考视频", prompts[cos_id])
            self.assertNotIn("手机", prompts[cos_id])
        shaoluo = prompts["shaoluo_pink_white_rabbit"]
        self.assertIn("脸部清晰可见", shaoluo)
        self.assertNotIn("白色兔子卡通面具", shaoluo)
        self.assertNotIn("面具完整覆盖脸部", shaoluo)
        shiroko = prompts["shiroko_black_white_tracksuit"]
        self.assertIn("亮青绿色滚边", shiroko)
        self.assertIn("白色不透明过膝长袜", shiroko)
        self.assertIn("住宅走廊或室内门厅", shiroko)
        phoebe = prompts["phoebe_white_gold_sanctuary"]
        self.assertIn("金黄色长卷发", phoebe)
        self.assertIn("背景明亮、轮廓清楚", phoebe)
        self.assertIn("不添加雾气、雾霾、烟雾或朦胧遮挡", phoebe)
        self.assertNotIn("参考视频", phoebe)
        white_gold_red = prompts["ancient_white_gold_red_sleeves"]
        self.assertIn("浅金色织锦抹胸", white_gold_red)
        self.assertIn("亮蓝色细滚边", white_gold_red)
        self.assertIn("正红色长丝带", white_gold_red)
        ibuki = prompts["ibuki_red_white_sportswear"]
        self.assertIn("《碧蓝档案》丹花伊吹", ibuki)
        self.assertIn("红色高腰运动短裤", ibuki)
        self.assertIn("白色短袖紧身运动T恤", ibuki)
        lace_shorts = prompts["white_lace_waist_shorts"]
        self.assertIn("白色蕾丝束腰短背心", lace_shorts)
        self.assertIn("白色宽松高腰灯笼短裤", lace_shorts)
        backless = prompts["ancient_white_blue_backless_gauze"]
        self.assertIn("整片裸背", backless)
        self.assertIn("浅冰蓝色宽袖", backless)
        cc_dress = prompts["cc_white_tiger_pink_gold_dress"]
        self.assertIn("《叛逆的鲁鲁修》（Code Geass）C.C.（CC）皇后装", cc_dress)
        self.assertIn("极长橄榄绿色直发", cc_dress)
        self.assertIn("粉色缎面高开叉长裙", cc_dress)
        kaguya = prompts["kaguya_miko_red_white_crown"]
        self.assertIn("巫女辉夜主题", kaguya)
        self.assertIn("中央固定一颗圆形金色球体", kaguya)
        self.assertIn("正红色缎面大蝴蝶结", kaguya)
        self.assertIn("白色半透明长纱片", kaguya)
        shuilaner_action = plugin_main.build_cos_look_action(
            camera="third",
            picker=lambda **_: next(
                item
                for item in plugin_main.COS_LOOK_SETS
                if item["id"] == "shuilaner_horned_brocade_qipao"
            ),
        )
        self.assertIn("具体画幅、景别和机位遵循本套套装描述", shuilaner_action)
        self.assertIn("平视机位和正常拍摄距离", shuilaner_action)
        self.assertIn("竖屏全身构图", shuilaner_action)
        for title in (
            "满穗·灰白和风",
            "小乔·白熊围巾",
            "芙宁娜·奶油浅蓝荷叶裙",
            "古风·浅蓝花卉挂脖兜兜",
            "神里绫华·白袖蓝袴",
            "古拉·小鲨鱼",
            "小乔·少御粉色短装",
            "瑶·小鹿主题",
            "和泉纱雾·粉绿印花短装",
            "芭芭拉·白色偶像礼服",
            "Saber·青花瓷短装",
            "银狼·蓝黑街头短装",
            "嫦娥·落星盏金月短装",
            "小乔·战国袍",
            "爱弥斯·黑白蓝坐姿",
            "洛琪希·露肩短睡裙",
            "卡提希娅·白黑蓝短装",
            "水兰儿·羊角提花短旗袍",
            "砂狼白子·黑白运动服",
            "菲比·白金圣洁礼服",
        ):
            self.assertEqual(
                [item["title"] for item in plugin_main.match_cos_look_sets(title)],
                [title],
            )

    def test_cos_look_matching_and_count_shorthand(self) -> None:
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
                class PermissionType:
                    ADMIN = "admin"
                    MEMBER = "member"

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
        import astrbot_plugin_selfie_image.main as plugin_main

        xishi_ids = {item["id"] for item in plugin_main.match_cos_look_sets("西施")}
        self.assertEqual(
            xishi_ids,
            {
                "xishi_fan_qipao", "xishi_cyan_qipao", "xishi_shiyu_jiangnan",
                "xishi_crop_qipao", "xishi_chengliyiao", "xishi_xuxiangsi",
            },
        )
        qipao_ids = {item["id"] for item in plugin_main.match_cos_look_sets("旗袍")}
        self.assertEqual(
            qipao_ids,
            {
                "xishi_fan_qipao", "yinzi_white_qipao", "xishi_cyan_qipao",
                "xishi_crop_qipao", "shuilaner_horned_brocade_qipao",
            },
        )
        xishi_qipao_ids = {item["id"] for item in plugin_main.match_cos_look_sets("西施旗袍")}
        self.assertEqual(
            xishi_qipao_ids,
            {"xishi_fan_qipao", "xishi_cyan_qipao", "xishi_crop_qipao"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("芙宁娜")},
            {"furina_cream_blue_ruffle", "furina_classic_blue_white"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("甘雨")},
            {"ganyu_bride", "ganyu_ice_blue", "ganyu_blue_white_qilin_close"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("洛琪希")},
            {"roxy_cream", "roxy_off_shoulder_sleep_dress"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("卡提希娅")},
            {"cartethyia_black_bird", "cartethyia_white_black_blue_short"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("宵宫")},
            {"yoimiya_firework"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("宵宫·烟花祭典")},
            {"yoimiya_firework_festival"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("胡桃")},
            {
                "hutao_wansheng",
                "hutao_dragon_path_zhichun",
                "hutao_koi_dream_glow",
                "hutao_spring_morning_breeze",
                "hutao_warm_sweet_dream",
                "hutao_high_school_swimsuit",
                "hutao_spring_peach_smile",
            },
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("刻晴")},
            {"keqing_purple_dress"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("可莉")},
            {"klee_red_clover"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("满穗")},
            {"mansui_gray_wafu"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("白熊")},
            {"xiao_qiao_white_bear"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("小舞")},
            {"xiaowu_pink_rabbit"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("王昭君")},
            {"wangzhaojun_old_blue"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("玉玲珑")},
            {"yulinglong_gold_fox", "yulinglong_red_thread"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("二次元少女")},
            {"anime_girl_orange_mint"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("瑶")},
            {"yao_mint_blue_dress", "yao_deer_theme"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("露娜")},
            {"luna_zixia_fairy", "luna_frost_moon_mirror"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("神里绫华")},
            {"kamisato_ayaka_white_blue_hakama"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("汉服")},
            {
                "hanfu_peach", "mint_sheer_hanfu", "silver_deepv_hanfu",
                "ancient_hanfu_halter_dudou",
            },
        )
        for query in ("银紫深V广袖", "古风·汉服·银紫深V广袖"):
            self.assertEqual(
                {item["id"] for item in plugin_main.match_cos_look_sets(query)},
                {"silver_deepv_hanfu"},
            )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("古风")},
            {
                "hanfu_peach", "mint_sheer_hanfu", "silver_deepv_hanfu",
                "blue_backless_hanfu", "mint_twin_braid_hanfu",
                "ancient_hanfu_halter_dudou", "ancient_blue_floral_halter_doudou",
                "ancient_mint_crystal_halter", "ancient_mint_floral_short_ruqun",
                "ancient_pink_moon_sheer",
                "ancient_teal_red_pipe", "ancient_pink_blue_floral_collar",
                "ancient_white_gold_red_sleeves", "ancient_white_blue_backless_gauze",
            },
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("兜兜")},
            {"ancient_blue_floral_halter_doudou"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("肚兜")},
            {"ancient_hanfu_halter_dudou"},
        )
        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("挂脖肚兜")},
            {"ancient_hanfu_halter_dudou"},
        )
        for query in ("公孙离", "离恨烟", "公孙离·离恨烟"):
            expected = {"gongsunli_lihenyan"}
            if query == "公孙离":
                expected.update(
                    {
                        "gongsunli_yutu_princess",
                        "gongsunli_lihenyan_sleeves",
                        "gongsunli_yutu_braid_shadow",
                        "gongsunli_white_green_ruffle",
                    }
                )
            self.assertEqual(
                {item["id"] for item in plugin_main.match_cos_look_sets(query)},
                expected,
            )
        self.assertEqual(plugin_main.match_cos_look_sets("离恨"), [])
        self.assertEqual(
            plugin_main._cos_item_terms({"title": "公孙离·离恨烟"}),
            ["公孙离·离恨烟", "公孙离", "离恨烟"],
        )
        self.assertEqual(plugin_main.match_cos_look_sets("夜晚霓虹街道"), [])
        self.assertEqual(plugin_main.match_cos_look_sets("鹿角"), [])
        self.assertEqual(plugin_main.match_cos_look_sets("王者荣耀"), [])

        class _P:
            pass

        self.assertEqual(
            {item["id"] for item in plugin_main.match_cos_look_sets("洛琪希 夜景")},
            {"roxy_cream", "roxy_off_shoulder_sleep_dress"},
        )
        for query in ("洛琪希xxx", "洛琪希夜景"):
            self.assertEqual(plugin_main.match_cos_look_sets(query), [])
        unmatched_roxy = plugin_main.SelfieImagePlugin._build_cos_look_action(_P(), "洛琪希xxx", False)
        self.assertIn("用户补充要求优先：洛琪希xxx", unmatched_roxy)

        for query, expected_ids in (
            ("西施", xishi_ids),
            ("旗袍", qipao_ids),
            ("西施旗袍", xishi_qipao_ids),
        ):
            for _ in range(20):
                action = plugin_main.SelfieImagePlugin._build_cos_look_action(_P(), query, False)
                match = re.search(r"【cos:([a-z0-9_]+)】", action)
                self.assertIsNotNone(match, action)
                self.assertIn(match.group(1), expected_ids, action)
            unmatched = plugin_main.SelfieImagePlugin._build_cos_look_action(_P(), f"{query} 夜景", False)
            self.assertIn(f"用户补充要求优先：{query} 夜景", unmatched)

        plugin = object.__new__(plugin_main.SelfieImagePlugin)
        plugin.config = type("Config", (), {"image_max_batch_count": 10})()
        for text, expected_extra, expected_count in (
            ("3 西施", "西施", 3),
            ("西施 3", "西施", 3),
            ("3旗袍", "旗袍", 3),
            ("三旗袍", "旗袍", 3),
            ("旗袍3", "旗袍", 3),
            ("3张西施", "西施", 3),
            ("洛琪希 夜景 3", "洛琪希 夜景", 3),
            ("洛琪希 夜景 3张", "洛琪希 夜景", 3),
            ("洛琪希 夜景三", "洛琪希 夜景", 3),
        ):
            extra, count = plugin._extract_command_count(text, allow_attached=True)
            self.assertEqual((extra, count), (expected_extra, expected_count))
        self.assertEqual(plugin._extract_command_count("3旗袍"), ("3旗袍", 1))

        # A batch rebuild must retain a matched COS query even when the query
        # is already present in the selected outfit title/prompt.
        batch_plugin = object.__new__(plugin_main.SelfieImagePlugin)
        batch_plugin.config = type("Config", (), {"image_max_batch_count": 10})()
        rebuilt_actions = []
        rebuilt_queries = []

        async def fake_build_prompt(event, action, extra_refs):
            rebuilt_actions.append(action)
            return action, [], {}

        async def fake_generate(prompt, aspect, resolution, refs, **kwargs):
            return {"success": True, "files": ["generated.png"]}

        async def fake_counted(*, task_id, event, total, fail_label, run_one, log_prefix):
            for index in range(total):
                await run_one(index)
            return {"success": True, "files": []}

        real_build_cos_action = plugin_main.SelfieImagePlugin._build_cos_look_action

        def fake_build_cos_action(extra_request="", has_refs=False, **kwargs):
            rebuilt_queries.append(extra_request)
            return real_build_cos_action(batch_plugin, extra_request, has_refs, **kwargs)

        batch_plugin._build_selfie_prompt_and_refs_for_event = fake_build_prompt
        batch_plugin._run_image_generation = fake_generate
        batch_plugin._run_counted_generation_shots = fake_counted
        batch_plugin._build_cos_look_action = fake_build_cos_action
        initial_action = plugin_main.SelfieImagePlugin._build_cos_look_action(_P(), "洛琪希", False)
        asyncio.run(
            batch_plugin._run_selfie_batches_unlocked(
                "test-cos-batch",
                object(),
                initial_action,
                [],
                "command-look-cos",
                3,
                "9:16",
                "1K",
                "生成失败",
                "洛琪希",
            )
        )
        self.assertEqual(len(rebuilt_actions), 3)
        self.assertEqual(rebuilt_queries, ["洛琪希"] * 3)
        self.assertEqual(
            {re.search(r"【cos:([a-z0-9_]+)】", action).group(1) for action in rebuilt_actions},
            {"roxy_cream", "roxy_off_shoulder_sleep_dress"},
        )

        class Event:
            def __init__(self, message: str) -> None:
                self.message_str = message

            def plain_result(self, text: str) -> str:
                return text

        async def collect_list_response(message: str) -> list[str]:
            return [
                item
                async for item in plugin_main.SelfieImagePlugin.cmd_look_cos(
                    object.__new__(plugin_main.SelfieImagePlugin), Event(message)
                )
            ]

        for alias in ("列表", "全部", "查看"):
            response = asyncio.run(collect_list_response(f"/看看COS {alias}"))
            self.assertEqual(len(response), 1)
            self.assertIn("看看COS 随机池（122套）：", response[0])
            for title in (item["title"] for item in plugin_main.COS_LOOK_SETS):
                self.assertIn(title, response[0])
        self.assertNotIn("lusha_cat_crown", {x["id"] for x in plugin_main.COS_LOOK_SETS})
        self.assertNotIn("yao_cinnamoroll", {x["id"] for x in plugin_main.COS_LOOK_SETS})
        for removed in (
            "lusha_white_gold",
            "elsa_ice_cape",
            "miku_formula_blue",
            "sakura_pink_magic",
            "shinobu_butterfly",
            "mitsuri_love",
            "tracen_academy",
            "yao_cinnamoroll",
            "barbara_idol",
        ):
            self.assertNotIn(removed, {x["id"] for x in plugin_main.COS_LOOK_SETS})
        for item in plugin_main.COS_LOOK_SETS:
            self.assertNotIn("手机", item["prompt"], item["id"])
        wrap = plugin_main.SelfieImagePlugin._build_cos_look_action(_P(), "", False)
        self.assertTrue("对镜" in wrap or "他拍" in wrap, wrap)
        self.assertNotIn("第一人称自拍或居家随手拍", wrap)
        self.assertEqual(plugin_main.parse_requested_cos_camera("他拍"), "third")
        self.assertEqual(plugin_main.parse_requested_cos_camera("自拍"), "selfie")
        self.assertEqual(plugin_main.parse_requested_cos_camera(""), "")
        forced_selfie = plugin_main.SelfieImagePlugin._build_cos_look_action(_P(), "对镜自拍", False)
        self.assertIn("【cam:selfie】", forced_selfie)
        self.assertIn("【自拍 / 看看COS模式】", forced_selfie)
        self.assertNotIn("手机", forced_selfie)
        forced_third = plugin_main.SelfieImagePlugin._build_cos_look_action(_P(), "他拍", False)
        self.assertIn("【cam:third】", forced_third)
        self.assertIn("【他拍 / 看看COS模式】", forced_third)
        self.assertNotIn("对镜全身或大半身自拍", forced_third)
        adapted = plugin_main.adapt_cos_outfit_for_camera("室内柔光对镜全身。不是婚纱。", "third")
        self.assertIn("室内柔光半身", adapted)
        self.assertNotIn("对镜", adapted)
        self.assertIn("不要第二个人", forced_third)
        self.assertIn("不要拍到拍摄设备或拍摄过程", forced_third)
        self.assertNotIn("手机", forced_third)
        look_you = plugin_main.SelfieImagePlugin._build_third_person_look_action(_P(), "", False)
        self.assertIn("别人视角的单人成品照", look_you)
        self.assertNotIn("朋友在对面用手机拍", look_you)
        self.assertNotIn("朋友在旁边", look_you)
        self.assertIn("竖屏", look_you)
        self.assertIn("半身", look_you)
        self.assertIn("窗光", look_you)
        self.assertIn("真实皮肤", look_you)
        self.assertIn("不要美颜滤镜", look_you)
        selfie_cover = plugin_main.SelfieImagePlugin._build_selfie_look_action(_P(), "", False)
        self.assertIn("竖屏", selfie_cover)
        self.assertIn("半身", selfie_cover)
        self.assertIn("窗光", selfie_cover)
        self.assertIn("真实皮肤", selfie_cover)
        self.assertIn("不要美颜滤镜", selfie_cover)
        self.assertNotIn("过度美颜磨皮", selfie_cover)
        group_cover = plugin_main.SelfieImagePlugin._build_group_selfie_action(_P(), "", False)
        self.assertIn("竖屏", group_cover)
        self.assertIn("半身", group_cover)
        self.assertIn("窗光", group_cover)
        self.assertIn("不要美颜滤镜", group_cover)
        self.assertIn("竖屏", forced_selfie)
        self.assertIn("半身", forced_selfie)
        self.assertNotIn("对镜全身或大半身自拍", forced_selfie)
        self.assertIn("竖屏", forced_third)
        self.assertIn("半身", forced_third)
        self.assertNotIn("全身或大半身", forced_third)
        self.assertIn("室内柔光半身", adapted)
        self.assertNotIn("室内柔光全身", adapted)
        rem_prompt = next(x for x in plugin_main.COS_LOOK_SETS if x["id"] == "rem_blue_lolita")["prompt"]
        self.assertIn("室内柔光对镜全身", rem_prompt)
        self.assertIn("白色长袖蓬袖衬衣", rem_prompt)


        from astrbot_plugin_selfie_image.features.persona import PersonaManager, current_period, period_label
        with tempfile.TemporaryDirectory() as tmp:
            manager = PersonaManager(tmp)
            manager.data["daily_selfie_profile"] = {
                "date": "2099-01-01",
                "outfit": "浅蓝衬衫和白色长裙",
                "status": "stale-status",
                "status_by_period": {
                    "morning": "晨光清爽",
                    "noon": "午间明亮",
                    "afternoon": "午后偏软",
                    "evening": "傍晚暖灯",
                    "night": "夜里小灯",
                    "late_night": "深夜私密",
                },
                "mood": "放松、安静、柔和",
                "seed": "test",
                "updated_at": "",
                "source": "fallback",
            }
            period = current_period()
            label = period_label(period)
            expected_status = manager.data["daily_selfie_profile"]["status_by_period"][period]

            look_you = plugin_main.SelfieImagePlugin._build_third_person_look_action(_P(), "", False)
            selfie = plugin_main.SelfieImagePlugin._build_selfie_look_action(_P(), "", False)
            night_lights = ("霓虹夜色", "金色小时", "清晨清透光", "傍晚金色余晖")
            if period in {"morning", "noon", "afternoon"}:
                for token in ("霓虹夜色", "暖黄台灯"):
                    self.assertNotIn(token, look_you)
                    self.assertNotIn(token, selfie)
            if period in {"evening", "night", "late_night"}:
                for token in ("清晨清透光", "金色小时", "树荫斑驳"):
                    self.assertNotIn(token, look_you)
                    self.assertNotIn(token, selfie)
            self.assertIn("用户补充要求优先", plugin_main.SelfieImagePlugin._build_third_person_look_action(_P(), "晚上霓虹", False))

            look_prompt = manager.build_selfie_prompt(
                plugin_main.SelfieImagePlugin._build_third_person_look_action(_P(), "", False),
                "小助",
                "温柔",
                True,
                0,
            )
            self.assertIn("今日穿搭：浅蓝衬衫和白色长裙", look_prompt)
            self.assertIn(f"当前时间段：{label}", look_prompt)
            self.assertIn(f"当前状态：{expected_status}", look_prompt)
            self.assertNotIn("stale-status", look_prompt)

            override = manager.build_selfie_prompt(
                plugin_main.SelfieImagePlugin._build_third_person_look_action(_P(), "晚上霓虹街上穿黑裙", False),
                "小助",
                "温柔",
                True,
                0,
            )
            self.assertIn("用户补充要求优先：晚上霓虹街上穿黑裙", override)
            self.assertNotIn("今日穿搭：", override)
            self.assertNotIn("当前时间段：", override)
            self.assertNotIn("当前状态：", override)

            clothes_only = manager.build_selfie_prompt(
                plugin_main.SelfieImagePlugin._build_third_person_look_action(_P(), "穿白裙", False),
                "小助",
                "温柔",
                True,
                0,
            )
            self.assertNotIn("今日穿搭：", clothes_only)
            self.assertIn(f"当前时间段：{label}", clothes_only)

            cos = manager.build_selfie_prompt(
                plugin_main.SelfieImagePlugin._build_cos_look_action(_P(), "", False, camera="selfie"),
                "小助",
                "温柔",
                True,
                0,
            )
            self.assertNotIn("今日穿搭：", cos)
            self.assertNotIn("当前时间段：", cos)
            legs = manager.build_selfie_prompt("看看腿", "小助", "温柔", True, 0)
            self.assertNotIn("今日穿搭：", legs)
            self.assertNotIn("当前时间段：", legs)

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
        self.assertIn("本次服装搭配已锁定为：白色不透白丝", normalized)
        self.assertNotIn("本次腿部穿搭：", normalized)
        normalized_extra = re.split(r"用户补充要求优先[:：]", normalized, maxsplit=1)[-1]
        self.assertNotIn("短袜", normalized_extra)
        self.assertEqual(plugin._normalize_selfie_action(normalized, False), normalized)

    def test_selfie_batch_cos_text_does_not_switch_to_cos_pool(self) -> None:
        """Only /看看COS may rebuild an action from the random COS pool."""
        stub_factory = SessionModelAndTaskTests()
        batch_plugin = stub_factory._plugin_stub()
        from astrbot_plugin_selfie_image import main as plugin_main

        batch_plugin.config = type("Config", (), {"image_max_batch_count": 10})()
        rebuilt_actions = []
        selfie_requests = []

        async def fake_build_prompt(event, action, extra_refs):
            rebuilt_actions.append(action)
            return action, [], {}

        async def fake_generate(prompt, aspect, resolution, refs, **kwargs):
            return {"success": True, "files": ["generated.png"]}

        async def fake_counted(*, task_id, event, total, fail_label, run_one, log_prefix):
            for index in range(total):
                await run_one(index)
            return {"success": True, "files": []}

        def fake_build_selfie_action(extra_request="", has_refs=False, **kwargs):
            selfie_requests.append(extra_request)
            return f"普通自拍重建 {len(selfie_requests)} 【shot:arm_half】"

        def fail_build_cos_action(*args, **kwargs):
            raise AssertionError("普通 /自拍 不应进入 COS 随机池")

        batch_plugin._build_selfie_prompt_and_refs_for_event = fake_build_prompt
        batch_plugin._run_image_generation = fake_generate
        batch_plugin._run_counted_generation_shots = fake_counted
        batch_plugin._build_selfie_look_action = fake_build_selfie_action
        batch_plugin._build_cos_look_action = fail_build_cos_action

        for initial_action in (
            "【自拍 / 看看模式】用户补充要求优先：COS 菲比",
            "【自拍 / 看看模式】用户补充要求优先：看看COS风格的菲比",
            "【自拍 / 看看模式】用户补充要求优先：COS 菲比 【cos:phoebe_white_gold_sanctuary】",
        ):
            asyncio.run(
                batch_plugin._run_selfie_batches_unlocked(
                    "test-selfie-cos-text",
                    object(),
                    initial_action,
                    [],
                    "command-selfie",
                    2,
                    "9:16",
                    "1K",
                    "生成失败",
                )
            )

        self.assertEqual(len(rebuilt_actions), 6)
        self.assertEqual(len(selfie_requests), 6)

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
        self.assertIn("本次服装搭配已锁定为：白色不透白丝", white)
        self.assertNotIn("本次腿部穿搭：", white)
        black = plugin_main.SelfieImagePlugin._build_leg_focus_action(_P(), "黑丝", False)
        self.assertIn("本次服装搭配已锁定为：黑色不透黑丝", black)
        bare = plugin_main.SelfieImagePlugin._build_leg_focus_action(_P(), "光腿神器", False)
        self.assertIn("本次服装搭配已锁定为：自然肤色光腿神器", bare)
        forced = plugin_main.SelfieImagePlugin._build_leg_focus_action(_P(), "", False, force_legwear="白丝")
        self.assertIn("本次服装搭配已锁定为：白色不透白丝", forced)
        from astrbot_plugin_selfie_image.features.persona import PersonaManager

        for legwear, expected in (
            ("光腿神器", "自然肤色光腿神器（沿可见腿部连续覆盖）"),
            ("白丝", "白色不透白丝（从大腿上部沿可见腿部连续向下覆盖，袜口在大腿上部）"),
            ("黑丝", "黑色不透黑丝（从大腿上部沿可见腿部连续向下覆盖，袜口在大腿上部）"),
        ):
            action = plugin_main.SelfieImagePlugin._build_leg_focus_action(
                _P(), "", False, force_legwear=legwear
            )
            with tempfile.TemporaryDirectory() as tmp:
                final = PersonaManager(tmp).build_selfie_prompt(action, "小助", "温柔", True, 0)
            self.assertIn(expected, final)

    def test_legwear_is_pose_weighted(self) -> None:
        from astrbot_plugin_selfie_image.cos.leg_focus import LEGWEAR_BY_POSE

        self.assertEqual(
            LEGWEAR_BY_POSE["side_lie"],
            (("光腿神器", 6), ("白丝", 3), ("黑丝", 1)),
        )
        self.assertEqual(
            LEGWEAR_BY_POSE["cross_leg"],
            (("光腿神器", 2), ("白丝", 4), ("黑丝", 4)),
        )
        self.assertNotIn("stand_topdown", LEGWEAR_BY_POSE)

    def test_multi_image_commands_rebuild_each_shot(self) -> None:
        main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn("rebuild_each", main_src)
        self.assertIn("avoid_pose", main_src)
        self.assertIn("_build_selfie_look_action", main_src)
        from astrbot_plugin_selfie_image.prompts.selfie_actions import (
            SELFIE_SHOT_LINES,
            THIRD_PERSON_SHOT_LINES,
        )
        self.assertIn("arm_half", SELFIE_SHOT_LINES)
        self.assertIn("half_front", THIRD_PERSON_SHOT_LINES)
        self.assertIn("command-look-you", main_src)


    def test_leg_focus_camera_weights_follow_pose_context(self) -> None:
        import astrbot_plugin_selfie_image.main as plugin_main

        weights = plugin_main.LEGFOCUS_CAMERA_WEIGHTS
        self.assertGreater(dict(weights["bed_supine_crop"])["selfie"], dict(weights["bed_supine_crop"])["third"])
        self.assertGreater(dict(weights["windowsill_crop"])["third"], dict(weights["windowsill_crop"])["selfie"])
        self.assertGreater(dict(weights["cross_leg"])["third"], dict(weights["cross_leg"])["selfie"])
        self.assertNotIn("stand_topdown", weights)


class StudioStoreTests(unittest.TestCase):
    def test_selfie_template_mentions_look_legs_outfit_record(self) -> None:
        from astrbot_plugin_selfie_image.studio.studio import list_studio_templates

        templates = {item["id"]: item for item in list_studio_templates()}
        description = templates["selfie"]["description"]
        self.assertIn("看看腿", description)
        self.assertIn("日常下装穿搭记录", description)

    def test_group_template_and_persist(self) -> None:
        import tempfile
        from astrbot_plugin_selfie_image.studio.studio import (
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
        from astrbot_plugin_selfie_image.studio.studio import StudioStore, list_studio_templates, prompts_for_template

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
        from astrbot_plugin_selfie_image.studio.studio import (
            SPECIAL_PRESET_ALIAS,
            default_image_preset_seed,
            global_prompt_presets,
            prompts_for_template,
            special_prompt_presets,
        )

        duo = prompts_for_template("duo")
        titles = {str(x.get("title")) for x in duo}
        self.assertIn("双人温馨", titles)
        self.assertNotIn("多人温馨", titles)  # group-only
        self.assertNotIn("精修表情", titles)  # i2i-only
        self.assertIn("捧脸", titles)
        globals_ = global_prompt_presets()
        gnames = {str(x.get("name")) for x in globals_}
        structure_presets = (
            "深开襟", "下胸开窗", "侧胸镂空", "交叉绑带", "挂脖露背",
            "侧腰双开窗", "极高侧开衩", "薄纱叠层", "开放侧身", "敞怀外套",
            "前襟分离系带", "单肩斜向开胸", "胸腹竖向开口", "下胸弧形开窗",
            "单侧全开裙摆", "前后分片围裹",
        )
        for need in (
            "捧脸", "遮脸", "变真人", "果冻化", "真人化", "变COS",
            "漫画封面", "证件照", "男友视角", "漏腰", *structure_presets,
        ):
            self.assertIn(need, gnames)
        special = special_prompt_presets()
        self.assertEqual(SPECIAL_PRESET_ALIAS, "特殊预设")
        self.assertEqual({str(item.get("title")) for item in special}, set(structure_presets))
        seed = default_image_preset_seed()
        self.assertIn("捧脸", seed)
        self.assertIn("遮脸", seed)
        self.assertIn("漏腰", seed)
        self.assertTrue(seed["捧脸"]["prompt"])
        cover = seed["遮脸"]["prompt"]
        self.assertIn("仅使用一部普通手机", cover)
        self.assertIn("只占用这只既有手", cover)
        self.assertIn("手机替代该动作或道具", cover)
        self.assertIn("恰好两条手臂、两只手", cover)
        self.assertIn("不额外添加手机，也不强行遮脸", cover)
        self.assertNotIn("随机选择一种遮挡方式", cover)
        lou = seed["漏腰"]["prompt"]
        self.assertIn("短上衣", lou)
        self.assertIn("oversized", lou)
        self.assertIn("腰线", lou)
        self.assertIn("居家休闲自拍", lou)
        for bad in ("露脐", "肚脐", "胸部", "boyfriend-view", "参考男友", "midriff", "bra"):
            self.assertNotIn(bad, lou)
        for name in structure_presets:
            self.assertIn(name, seed)
            self.assertIn("成年女性", seed[name]["prompt"])
        self.assertIn("左右前襟向两侧展开", seed["深开襟"]["prompt"])
        self.assertIn("宽幅弧形开窗", seed["下胸开窗"]["prompt"])
        self.assertIn("大面积侧向镂空结构", seed["侧胸镂空"]["prompt"])
        self.assertIn("大腿根部附近", seed["极高侧开衩"]["prompt"])
        self.assertIn("左右分离的开放结构", seed["前襟分离系带"]["prompt"])
        self.assertIn("斜向开放设计", seed["单肩斜向开胸"]["prompt"])
        self.assertIn("纵向开口", seed["胸腹竖向开口"]["prompt"])
        self.assertIn("宽幅弧形开窗", seed["下胸弧形开窗"]["prompt"])
        self.assertIn("完整开放结构", seed["单侧全开裙摆"]["prompt"])
        self.assertIn("前后分片的围裹式结构", seed["前后分片围裹"]["prompt"])
    def test_default_presets_seed(self) -> None:
        import tempfile
        from astrbot_plugin_selfie_image.prompts.preset import (
            LEGACY_BUILTIN_PROMPT_REPLACEMENTS,
            ImagePresetManager,
        )
        from astrbot_plugin_selfie_image.studio.studio import special_prompt_presets

        with tempfile.TemporaryDirectory() as tmp:
            mgr = ImagePresetManager(tmp)
            names = {n for n, _ in mgr.list()}
            for need in (
                "捧脸", "遮脸", "变真人", "果冻化", "真人化", "变COS",
                "漫画封面", "证件照", "男友视角", "漏腰", "深开襟",
                "下胸开窗", "侧胸镂空", "交叉绑带", "挂脖露背",
                "侧腰双开窗", "极高侧开衩", "薄纱叠层", "开放侧身", "敞怀外套",
                "前襟分离系带", "单肩斜向开胸", "胸腹竖向开口", "下胸弧形开窗",
                "单侧全开裙摆", "前后分片围裹",
            ):
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
            covered = mgr.resolve("遮脸")
            self.assertEqual(covered.get("preset_name"), "遮脸")
            self.assertIn("仅使用一部普通手机", covered.get("prompt") or "")
            self.assertIn("不要为手机新增手", covered.get("prompt") or "")
            opened = mgr.resolve("深开襟 保持原发型")
            self.assertEqual(opened.get("preset_name"), "深开襟")
            self.assertIn("左右前襟向两侧展开", opened.get("prompt") or "")
            self.assertIn("保持原发型", opened.get("prompt") or "")
            selected = special_prompt_presets()[0]
            with patch("astrbot_plugin_selfie_image.prompts.preset.random.choice", return_value=selected):
                special = mgr.resolve("特殊预设 夜景")
            self.assertEqual(special.get("preset_name"), "特殊预设")
            self.assertEqual(special.get("description"), f"随机选中：{selected['title']}")
            self.assertIn(selected["prompt"], special.get("prompt") or "")
            self.assertIn("夜景", special.get("prompt") or "")

    def test_legacy_cover_face_preset_upgrades_without_overwriting_custom_value(self) -> None:
        import tempfile
        from astrbot_plugin_selfie_image.prompts.preset import (
            LEGACY_BUILTIN_PROMPT_REPLACEMENTS,
            ImagePresetManager,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "image_presets.json"
            path.write_text(
                json.dumps({"遮脸": {"prompt": LEGACY_BUILTIN_PROMPT_REPLACEMENTS["遮脸"]}}),
                encoding="utf-8",
            )
            upgraded = ImagePresetManager(tmp)
            self.assertIn("仅使用一部普通手机", upgraded.presets["遮脸"].prompt)

            upgraded.add("遮脸", "我的自定义遮脸提示词")
            preserved = ImagePresetManager(tmp)
            self.assertEqual(preserved.presets["遮脸"].prompt, "我的自定义遮脸提示词")

    def test_web_preset_management_round_trip_and_atomic_import(self) -> None:
        import tempfile

        from astrbot_plugin_selfie_image.prompts.preset import ImagePresetManager

        with tempfile.TemporaryDirectory() as tmp:
            manager = ImagePresetManager(tmp)
            ok, message = manager.save_management(
                {
                    "name": "夜景人像",
                    "prompt": "夜景人像，柔和侧光",
                    "description": "管理页测试",
                    "aspect_ratio": "9:16",
                    "resolution": "1K",
                    "extra_prompt": "保留自然肤质",
                }
            )
            self.assertTrue(ok, message)
            row = next(item for item in manager.list_management() if item["name"] == "夜景人像")
            self.assertEqual(row["extra_prompt"], "保留自然肤质")

            with self.assertRaises(ValueError):
                manager.import_management(
                    [
                        {"name": "应回滚", "prompt": "有效内容"},
                        {"name": "无效", "prompt": ""},
                    ]
                )
            self.assertFalse(manager.has_preset("应回滚"))

            self.assertTrue(manager.remove("遮脸")[0])
            reloaded = ImagePresetManager(tmp)
            self.assertFalse(reloaded.has_preset("遮脸"))
            self.assertTrue(reloaded.has_preset("夜景人像"))

    def test_dashboard_contains_preset_management_page_and_apis(self) -> None:
        from pathlib import Path
        from astrbot_plugin_selfie_image.webui.web import INDEX_HTML

        page = (Path(__file__).resolve().parents[1] / "pages" / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        for document in (page, INDEX_HTML):
            for marker in (
                'data-tab="presets"',
                'id="presetManagerList"',
                'id="presetTransferModal"',
                "/api/prompt-presets/manage",
                "导出并复制",
                "自动复制失败",
            ):
                self.assertIn(marker, document)

    def test_phone_cover_face_cos_reuses_existing_hand(self) -> None:
        import tempfile

        from astrbot_plugin_selfie_image.cos.cos_looks import COS_LOOK_SETS, build_cos_look_action
        from astrbot_plugin_selfie_image.features.persona import PersonaManager
        from astrbot_plugin_selfie_image.studio.studio import default_image_preset_seed

        cover = default_image_preset_seed()["遮脸"]["prompt"]
        pipe_cos = next(item for item in COS_LOOK_SETS if item["id"] == "ancient_teal_red_pipe")
        action = build_cos_look_action(
            cover,
            camera="third",
            picker=lambda **_kwargs: pipe_cos,
        )
        self.assertIn("唯一一部普通手机", action)
        self.assertIn("手机替代该动作或道具，原道具不入镜", action)
        self.assertIn("恰好两条手臂、两只手", action)
        self.assertIn("第二台拍摄设备", action)
        self.assertNotIn("不要用物件遮脸挡衣服", action)

        with tempfile.TemporaryDirectory() as tmp:
            prompt = PersonaManager(tmp).build_selfie_prompt(
                action, "小助", "温柔", True, 0
            )
        self.assertIn("画面只允许主角既有的一只手握持唯一一部普通手机", prompt)
        self.assertIn("替代它原本的动作或道具，原道具不入镜", prompt)
        self.assertIn("恰好两条手臂、两只手", prompt)
        self.assertNotIn("不要用物件遮脸挡衣服", prompt)

    def test_selfie_command_expands_preset_before_action_wrap(self) -> None:
        """/自拍 捧脸 must expand preset on raw user text, not after long action wrap."""
        import tempfile
        from astrbot_plugin_selfie_image.prompts.preset import ImagePresetManager
        from astrbot_plugin_selfie_image.studio.studio import special_prompt_presets

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
            selected = special_prompt_presets()[-1]
            with patch("astrbot_plugin_selfie_image.prompts.preset.random.choice", return_value=selected):
                special_expanded, _, _, special_name = (
                    plugin_main.SelfieImagePlugin._expand_user_text_with_preset(
                        stub, "看看旗袍 特殊预设"
                    )
                )
            self.assertEqual(special_name, "特殊预设")
            self.assertIn("看看旗袍", special_expanded)
            self.assertIn(selected["prompt"], special_expanded)
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

    def test_cos_command_expands_presets_without_polluting_pool_query(self) -> None:
        """COS keeps character matching separate from expanded preset text."""
        import tempfile
        from astrbot_plugin_selfie_image.prompts.preset import ImagePresetManager

        stub = SessionModelAndTaskTests()._plugin_stub()
        from astrbot_plugin_selfie_image import main as plugin_main

        with tempfile.TemporaryDirectory() as tmp:
            stub.presets = ImagePresetManager(tmp)
            for raw, expected_count in (
                ("捧脸 3", 3),
                ("西施 捧脸 2", 2),
                ("西施 捧脸 夜景 5", 5),
            ):
                extra, count = stub._extract_command_count(raw, allow_attached=True)
                self.assertEqual(count, expected_count)
                expanded, aspect, resolution, preset_name = (
                    plugin_main.SelfieImagePlugin._expand_cos_user_text_with_preset(stub, extra)
                )
                self.assertEqual(preset_name, "捧脸")
                self.assertIn("捧住她的脸颊", expanded)
                self.assertEqual(aspect, "9:16")
                self.assertEqual(resolution, "1K")
                if raw.startswith("西施"):
                    self.assertIn("西施", expanded)
                if "夜景" in raw:
                    self.assertIn("夜景", expanded)

            raw = "西施 捧脸 夜景"
            expanded, _, _, _ = plugin_main.SelfieImagePlugin._expand_cos_user_text_with_preset(stub, raw)
            xishi_ids = {item["id"] for item in plugin_main.match_cos_look_sets("西施")}
            for _ in range(20):
                action = plugin_main.SelfieImagePlugin._build_cos_look_action(
                    stub, expanded, False, match_query=raw
                )
                match = re.search(r"【cos:([a-z0-9_]+)】", action)
                self.assertIsNotNone(match, action)
                self.assertIn(match.group(1), xishi_ids, action)

            # A preset body may mention a COS category; only the raw command
            # query is allowed to select the pool.
            stub.presets.add("捧脸", "捧住她的脸颊，旗袍细节清晰")
            expanded, _, _, _ = plugin_main.SelfieImagePlugin._expand_cos_user_text_with_preset(stub, "捧脸")
            with patch("astrbot_plugin_selfie_image.main.pick_cos_look_set") as picker:
                picker.return_value = plugin_main.COS_LOOK_SETS[0]
                plugin_main.SelfieImagePlugin._build_cos_look_action(
                    stub, expanded, False, match_query="捧脸"
                )
                self.assertEqual(picker.call_args.kwargs["query"], "捧脸")

    def test_cos_command_parameter_matrix(self) -> None:
        """COS parses count placement and forwards raw matching text end to end."""
        import tempfile

        from astrbot_plugin_selfie_image.prompts.preset import ImagePresetManager
        stub_factory = SessionModelAndTaskTests()
        stub_factory._plugin_stub()
        from astrbot_plugin_selfie_image import main as plugin_main

        class Event:
            def __init__(self, message: str) -> None:
                self.message_str = message

            def plain_result(self, text: str) -> str:
                return text

        cases = (
            ("/看看COS 捧脸 3", 3, "捧脸", "捧脸", "捧脸"),
            ("/看看COS 西施 捧脸 2", 2, "西施 捧脸", "西施 捧脸", "捧脸"),
            ("/看看COS 西施 捧脸 夜景 5张", 5, "西施 捧脸 夜景", "西施 捧脸 夜景", "捧脸"),
            ("/看看COS 夜景 捧脸 西施 5", 5, "夜景 捧脸 西施", "夜景 捧脸 西施", "捧脸"),
            ("/看看COS 捧脸 5 西施 夜景", 5, "捧脸 西施 夜景", "捧脸 西施 夜景", "捧脸"),
            ("/看看COS 洛琪希 夜景 男友视角 3", 3, "洛琪希 夜景 男友视角", "洛琪希 夜景 男友视角", "男友视角"),
            ("/看看COS 3张西施", 3, "西施", "西施", ""),
            ("/看看COS 西施3", 3, "西施", "西施", ""),
            ("/看看COS 西施 夜景三", 3, "西施 夜景", "西施 夜景", ""),
            ("/看看COS 洛琪希xxx 3", 3, "洛琪希xxx", "洛琪希xxx", ""),
        )

        with tempfile.TemporaryDirectory() as tmp:
            for message, expected_count, expected_extra, expected_query, expected_preset in cases:
                stub = stub_factory._plugin_stub()
                stub.presets = ImagePresetManager(tmp)
                captured = {}

                async def fake_handle(**kwargs):
                    captured.update(kwargs)
                    if False:
                        yield None

                stub._handle_selfie_command = fake_handle
                async def invoke_command():
                    return [
                        item
                        async for item in plugin_main.SelfieImagePlugin.cmd_look_cos(stub, Event(message))
                    ]

                output = asyncio.run(invoke_command())
                self.assertEqual(output, [])
                self.assertEqual(captured["requested_count_override"], expected_count, message)
                self.assertEqual(captured["rebuild_match_query"], expected_query, message)
                self.assertEqual(captured["preset_name"], expected_preset, message)
                if expected_preset:
                    preset_markers = {
                        "捧脸": "捧住她的脸颊",
                        "男友视角": "Girlfriend is drunk",
                    }
                    self.assertIn(preset_markers[expected_preset], captured["rebuild_extra_request"], message)
                else:
                    self.assertIn(expected_extra.split()[0], captured["rebuild_extra_request"], message)
                if message == "/看看COS 洛琪希 夜景 男友视角 3":
                    cos_marker = re.search(r"【cos:([a-z0-9_]+)】", captured["fallback"])
                    self.assertIsNotNone(cos_marker, message)
                    self.assertIn(
                        cos_marker.group(1),
                        {"roxy_cream", "roxy_off_shoulder_sleep_dress"},
                        message,
                    )
                    self.assertIn("洛琪希", captured["rebuild_extra_request"], message)
                    self.assertIn("夜景", captured["rebuild_extra_request"], message)

        stub = stub_factory._plugin_stub()
        for text in ("西施 2026 夜景", "西施 12a 夜景", "西施 3.14 夜景", "西施 9999", "3旗袍abc"):
            expected = ("3旗袍abc", 1) if text == "3旗袍abc" else (text, 1)
            self.assertEqual(stub._extract_command_count(text, allow_attached=True), expected)
        self.assertEqual(
            stub._extract_command_count("十分漂亮", allow_trailing=True),
            ("十分漂亮", 1),
        )
        self.assertEqual(
            stub._extract_command_count("一只猫3", allow_trailing=True),
            ("一只猫3", 1),
        )

    def test_image_commands_accept_count_before_or_after_prompt(self) -> None:
        """All image/selfie commands accept the common count positions."""
        import tempfile

        factory = SessionModelAndTaskTests()
        factory._plugin_stub()
        from astrbot_plugin_selfie_image import main as plugin_main
        from astrbot_plugin_selfie_image.prompts.preset import ImagePresetManager

        class Event:
            def __init__(self, message: str) -> None:
                self.message_str = message

            def plain_result(self, text: str) -> str:
                return text

        async def invoke_prompt(command: str, message: str, with_ref: bool = False):
            stub = factory._plugin_stub()
            stub.presets = ImagePresetManager(tempfile.mkdtemp())
            captured = {}
            stub._quota_error_message = lambda *args, **kwargs: ""
            stub._rate_limit_error_message = lambda *args, **kwargs: ""
            async def progress(*args, **kwargs):
                return "progress"

            stub._build_contextual_progress_text = progress
            stub._record_bot_text_context = lambda *args, **kwargs: None

            async def refs(*args, **kwargs):
                return [object()] if with_ref else []

            async def refs_stats(*args, **kwargs):
                return ([object()], 1, 0) if with_ref else ([], 0, 0)

            stub._event_reference_images = refs
            stub._event_reference_images_with_stats = refs_stats
            stub.start_command_image_task = (
                lambda *args, **kwargs: captured.update(summary=kwargs.get("summary", {})) or "task"
            )
            output = [item async for item in getattr(plugin_main.SelfieImagePlugin, command)(stub, Event(message))]
            return captured, output

        async def invoke_selfie(command: str, message: str):
            stub = factory._plugin_stub()
            stub.presets = ImagePresetManager(tempfile.mkdtemp())
            captured = {}

            async def handle(**kwargs):
                captured.update(kwargs)
                if False:
                    yield None

            stub._handle_selfie_command = handle
            output = [item async for item in getattr(plugin_main.SelfieImagePlugin, command)(stub, Event(message))]
            return captured, output

        prompt_cases = (
            ("cmd_draw", "/画 3 一只猫", False, "一只猫", 3),
            ("cmd_draw", "/画 一只猫 3", False, "一只猫", 3),
            ("cmd_draw", "/生图 一只猫 3", False, "一只猫", 3),
            ("cmd_draw", "@心酱 画 捧脸 10", False, "捧住她的脸颊", 10),
            ("cmd_raw_text_to_image", "/文生图 3 一只猫", False, "一只猫", 3),
            ("cmd_raw_text_to_image", "/文生图 一只猫 3", False, "一只猫", 3),
            ("cmd_raw_text_to_image", "/文生图 一只猫 三张", False, "一只猫", 3),
            ("cmd_raw_text_to_image", "/文生图 一位美女 捧脸 2", False, "一位美女", 2),
            ("cmd_raw_text_to_image", "/文生图 2 捧脸 一位美女", False, "一位美女", 2),
            ("cmd_raw_text_to_image", "/文生图 捧脸 2 一位美女", False, "一位美女", 2),
            ("cmd_raw_text_to_image", "/文生图 一位美女 2 捧脸", False, "一位美女", 2),
            ("cmd_raw_image_to_image", "/图生图 改成素描 3", True, "改成素描", 3),
        )
        for command, message, with_ref, expected_prompt, expected_count in prompt_cases:
            captured, output = asyncio.run(invoke_prompt(command, message, with_ref))
            self.assertEqual(output, ["progress"], message)
            self.assertEqual(captured["summary"]["requested_count"], expected_count, message)
            self.assertIn(expected_prompt, captured["summary"]["original_prompt"], message)
            if "捧脸" in message:
                self.assertIn("捧住她的脸颊", captured["summary"]["original_prompt"], message)

        from astrbot_plugin_selfie_image.studio.studio import special_prompt_presets

        selected = special_prompt_presets()[0]
        with patch("astrbot_plugin_selfie_image.prompts.preset.random.choice", return_value=selected):
            captured, output = asyncio.run(
                invoke_prompt("cmd_draw", "@心酱 画 特殊预设 10")
            )
        self.assertEqual(output, ["progress"])
        self.assertEqual(captured["summary"]["requested_count"], 10)
        self.assertIn(selected["prompt"], captured["summary"]["original_prompt"])
        self.assertNotIn("特殊预设", captured["summary"]["original_prompt"])

        selfie_cases = (
            ("cmd_selfie", "/看看 一位美女 捧脸 2", "一位美女"),
            ("cmd_selfie", "/自拍 2 一位美女 捧脸", "一位美女"),
            ("cmd_look_legs", "/看看腿 白丝 夜景 2", "白丝"),
            ("cmd_look_you", "/看看你 夜景 2", "夜景"),
            ("cmd_group_selfie", "/合影 一位美女 捧脸 2", "一位美女"),
        )
        for command, message, expected_marker in selfie_cases:
            captured, output = asyncio.run(invoke_selfie(command, message))
            self.assertEqual(output, [])
            self.assertEqual(captured["requested_count_override"], 2, message)
            self.assertIn(expected_marker, captured.get("message_override", "") or captured.get("fallback", ""), message)

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

    def test_view_prompt_does_not_mix_recent_bot_images_into_quote_lookup(self) -> None:
        """Each quoted image must be matched only against its own bytes."""
        plugin = SessionModelAndTaskTests()._plugin_stub()
        quoted_a = ImageReference(data=b"quoted-image-a", mime_type="image/png")
        quoted_b = ImageReference(data=b"quoted-image-b", mime_type="image/png")
        prompts = {
            hashlib.md5(quoted_a.data).hexdigest(): "prompt for quoted A",
            hashlib.md5(quoted_b.data).hexdigest(): "prompt for quoted B",
        }
        collected_kwargs = []

        class Event:
            def __init__(self, image):
                self.image = image

            def plain_result(self, text):
                return text

        async def collect_refs(event, **kwargs):
            collected_kwargs.append(kwargs)
            return [event.image]

        def find_record(md5):
            prompt = prompts.get(md5)
            return {"request_prompt": prompt} if prompt else None

        plugin._permission_denied_message = lambda _event: ""
        plugin._event_reference_images = collect_refs
        plugin._find_generation_record_by_md5 = find_record
        # A prior implementation searched this global session cache when a
        # bot image was quoted, which made every lookup resolve to the latest
        # generated image. Prompt lookup must never touch it.
        plugin._recent_context_image_sources = lambda *_args, **_kwargs: self.fail("unexpected recent-image lookup")
        plugin._event_quotes_bot_image = lambda _event: True

        async def run(event):
            return [item async for item in plugin.cmd_view_prompt(event)]

        first = asyncio.run(run(Event(quoted_a)))
        second = asyncio.run(run(Event(quoted_b)))
        self.assertEqual(first, [f"图片 MD5：{hashlib.md5(quoted_a.data).hexdigest()}\n生图提示词：\nprompt for quoted A"])
        self.assertEqual(second, [f"图片 MD5：{hashlib.md5(quoted_b.data).hexdigest()}\n生图提示词：\nprompt for quoted B"])
        self.assertEqual(len(collected_kwargs), 2)
        for kwargs in collected_kwargs:
            self.assertTrue(kwargs["include_image_alternates"])
            self.assertNotIn("extra_sources", kwargs)

    def test_reverse_image_prompt_always_calls_llm_without_record_lookup(self) -> None:
        plugin = SessionModelAndTaskTests()._plugin_stub()
        quoted = ImageReference(data=b"quoted-image", mime_type="image/png")
        collected_kwargs = []
        reversed_images = []

        class Event:
            def plain_result(self, text):
                return text

        async def collect_refs(_event, **kwargs):
            collected_kwargs.append(kwargs)
            return [quoted]

        async def reverse(_event, image):
            reversed_images.append(image)
            return "一位少女，半身构图，自然光，写实摄影，9:16"

        plugin._permission_denied_message = lambda _event: ""
        plugin._quoted_original_image_sources = lambda _event: asyncio.sleep(0, result=[])
        plugin._event_reference_images = collect_refs
        plugin._reverse_image_prompt_with_llm = reverse
        plugin._find_generation_record_by_md5 = lambda _md5: self.fail("unexpected generation record lookup")

        async def run():
            return [item async for item in plugin.cmd_reverse_image_prompt(Event())]

        md5 = hashlib.md5(quoted.data).hexdigest()
        output = asyncio.run(run())
        self.assertEqual(
            output,
            [
                f"图片 MD5：{md5}\n正在让当前 LLM 反推生图提示词……",
                f"图片 MD5：{md5}\nLLM 反推提示词：\n一位少女，半身构图，自然光，写实摄影，9:16",
            ],
        )
        self.assertEqual(reversed_images, [quoted.data])
        self.assertEqual(
            collected_kwargs,
            [
                {
                    "include_at_avatar": False,
                    "context_hint": "查看生图提示词",
                    "allow_context_fallback": False,
                    "include_persona": False,
                    "include_image_alternates": True,
                }
            ],
        )

    def test_reverse_image_prompt_requires_an_image(self) -> None:
        plugin = SessionModelAndTaskTests()._plugin_stub()

        class Event:
            def plain_result(self, text):
                return text

        plugin._permission_denied_message = lambda _event: ""
        plugin._quoted_original_image_sources = lambda _event: asyncio.sleep(0, result=[])
        plugin._event_reference_images = lambda _event, **_kwargs: asyncio.sleep(0, result=[])
        plugin._reverse_image_prompt_with_llm = lambda *_args: self.fail("unexpected LLM call")

        async def run():
            return [item async for item in plugin.cmd_reverse_image_prompt(Event())]

        self.assertEqual(
            asyncio.run(run()),
            ["请引用或附带一张图片后再使用 /查看生图提示词。"],
        )

    def test_quoted_original_sources_are_fetched_by_reply_id(self) -> None:
        plugin = SessionModelAndTaskTests()._plugin_stub()

        class Reply:
            def __init__(self, message_id):
                self.id = message_id
                self.chain = []

        class MessageObj:
            message = [Reply(2132965351)]
            quote = None
            raw_message = {"self_id": "3834455831"}

        class Bot:
            def __init__(self):
                self.calls = []

            async def call_action(self, action, **params):
                self.calls.append((action, params))
                return {
                    "message": [
                        {
                            "type": "image",
                            "data": {
                                "file": "qq-original-file",
                                "url": "https://cdn.example/original.png",
                            },
                        }
                    ]
                }

        event = types.SimpleNamespace(message_obj=MessageObj(), message=None, raw_message=None, bot=Bot())
        sources = asyncio.run(plugin._quoted_original_image_sources(event))
        self.assertEqual(sources, ["qq-original-file", "https://cdn.example/original.png"])
        self.assertEqual(event.bot.calls, [("get_msg", {"message_id": 2132965351, "self_id": "3834455831"})])

    def test_find_generation_record_by_md5_accepts_astrbot_jpeg_variant(self) -> None:
        """A quoted PNG may arrive after AstrBot's deterministic JPEG conversion."""
        from io import BytesIO
        try:
            from PIL import Image as PILImage
        except ImportError:
            self.skipTest("Pillow is required to reproduce AstrBot JPEG conversion")

        plugin = SessionModelAndTaskTests()._plugin_stub()
        with tempfile.TemporaryDirectory() as directory:
            source = BytesIO()
            PILImage.new("RGB", (3, 2), (42, 128, 231)).save(source, "PNG")
            png_data = source.getvalue()
            normalized = BytesIO()
            with PILImage.open(BytesIO(png_data)) as image:
                image.convert("RGB").save(normalized, "JPEG", quality=95, subsampling=0)
            normalized_md5 = hashlib.md5(normalized.getvalue()).hexdigest()
            self.assertNotEqual(hashlib.md5(png_data).hexdigest(), normalized_md5)

            cache_file = Path(directory) / "generated_3a23dd4a59f0cb.png"
            cache_file.write_bytes(png_data)
            plugin.generated_dir = directory
            plugin._records_lock = threading.RLock()
            plugin._records = [
                {
                    "success": True,
                    "generated_image_paths": [cache_file.name],
                    "md5": hashlib.md5(png_data).hexdigest(),
                    "request_prompt": "recorded prompt",
                }
            ]

            found = plugin._find_generation_record_by_md5(normalized_md5)
            self.assertIsNotNone(found)
            self.assertEqual(found["request_prompt"], "recorded prompt")
            self.assertEqual(found["md5"], normalized_md5)

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
        from astrbot_plugin_selfie_image.webui.web import INDEX_HTML, WEB_TASK_ID_RE

        self.assertIn('data-tab="studio"', INDEX_HTML)
        self.assertIn("studioTemplateSelect", INDEX_HTML)
        self.assertIn("按模板新建", INDEX_HTML)
        self.assertIn("studioPresetBtn", INDEX_HTML)
        self.assertIn("testPresetBtn", INDEX_HTML)
        self.assertIn("studioCosBtn", INDEX_HTML)
        self.assertIn("testCosBtn", INDEX_HTML)
        self.assertIn("renderCosPanel", INDEX_HTML)
        self.assertIn("/api/cos-look-sets", INDEX_HTML)
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

        dashboard_html = (
            Path(__file__).resolve().parents[1] / "pages" / "dashboard" / "index.html"
        ).read_text(encoding="utf-8")
        for marker in ("studioCosBtn", "testCosBtn", "renderCosPanel", "/api/cos-look-sets"):
            self.assertIn(marker, dashboard_html)

    def test_studio_promote_role_and_gallery(self) -> None:
        import tempfile
        from astrbot_plugin_selfie_image.studio.studio import StudioStore

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


class ImageBatchSchedulingRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_batch_is_not_called_previous_task_queue_when_idle(self) -> None:
        from types import SimpleNamespace
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin

        plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
        plugin.config = SimpleNamespace(image_max_concurrent_tasks=3)
        plugin._image_batch_gate = asyncio.Semaphore(3)
        self.assertFalse(plugin._image_batch_queue_expected(10))
        for _ in range(3):
            await plugin._image_batch_gate.acquire()
        self.assertTrue(plugin._image_batch_queue_expected(10))

    async def test_batch_larger_than_limit_is_drained_without_deadlock(self) -> None:
        from types import SimpleNamespace
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin

        class Event:
            def plain_result(self, text):
                return text

            async def send(self, _message):
                return None

        plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
        plugin.config = SimpleNamespace(
            image_max_concurrent_tasks=3,
            image_max_batch_count=10,
            image_show_generation_info=False,
            image_enable_daily_limit=False,
            image_show_model_info=False,
        )
        plugin._image_batch_gate = asyncio.Semaphore(3)
        plugin._selfie_batch_gate = plugin._image_batch_gate
        plugin._web_task_lock = threading.RLock()
        plugin._web_tasks = {"task": {"cancel_requested": False}}
        plugin._record_generated_images = lambda *_args: None
        plugin._send_generated_images = lambda *_args: asyncio.sleep(0)
        plugin._friendly_user_error_message = lambda error, fallback: error or fallback
        active = 0
        peak = 0

        async def run_one(_index):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.002)
            active -= 1
            return {"success": True, "files": ["generated.png"]}

        with patch("astrbot_plugin_selfie_image.main.IMAGE_BATCH_REQUEST_COOLDOWN_SECONDS", 0):
            result = await asyncio.wait_for(
                plugin._run_counted_generation_shots(
                    task_id="task",
                    event=Event(),
                    total=10,
                    fail_label="failed",
                    run_one=run_one,
                    log_prefix="test batch",
                ),
                timeout=2,
            )
        self.assertEqual(result["succeeded_count"], 10)
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(peak, 3)
        self.assertEqual(plugin._image_batch_gate._value, 3)

    async def test_batch_shots_wait_after_each_global_slot_acquisition(self) -> None:
        from types import SimpleNamespace
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin

        class Event:
            def plain_result(self, text):
                return text

            async def send(self, _message):
                return None

        plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
        plugin.config = SimpleNamespace(
            image_max_concurrent_tasks=1,
            image_show_generation_info=False,
            image_enable_daily_limit=False,
            image_show_model_info=False,
        )
        plugin._image_batch_gate = asyncio.Semaphore(1)
        plugin._selfie_batch_gate = plugin._image_batch_gate
        plugin._image_batch_cooldown_lock = asyncio.Lock()
        plugin._web_task_lock = threading.RLock()
        plugin._web_tasks = {"task": {"cancel_requested": False}}
        plugin._record_generated_images = lambda *_args: None

        async def send_images(*_args):
            return None

        plugin._send_generated_images = send_images
        acquired_at = []
        started_at = []
        acquire_slot = plugin._acquire_image_slot

        async def tracked_acquire(task_id):
            gate = await acquire_slot(task_id)
            acquired_at.append(time.monotonic())
            return gate

        async def run_one(_index):
            started_at.append(time.monotonic())
            return {"success": True, "files": ["generated.png"]}

        plugin._acquire_image_slot = tracked_acquire
        cooldown = 0.02
        with patch("astrbot_plugin_selfie_image.main.IMAGE_BATCH_REQUEST_COOLDOWN_SECONDS", cooldown):
            result = await plugin._run_counted_generation_shots(
                task_id="task",
                event=Event(),
                total=3,
                fail_label="failed",
                run_one=run_one,
                log_prefix="cooldown batch",
            )

        self.assertEqual(result["succeeded_count"], 3)
        self.assertEqual(len(acquired_at), 3)
        self.assertEqual(len(started_at), 3)
        for acquired, started in zip(acquired_at, started_at):
            self.assertGreaterEqual(started - acquired, cooldown * 0.75)
        for previous, current in zip(started_at, started_at[1:]):
            self.assertGreaterEqual(current - previous, cooldown * 0.75)

    async def test_batch_with_free_parallel_slots_staggers_upstream_starts(self) -> None:
        from types import SimpleNamespace
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin

        class Event:
            def plain_result(self, text):
                return text

            async def send(self, _message):
                return None

        plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
        plugin.config = SimpleNamespace(
            image_max_concurrent_tasks=3,
            image_show_generation_info=False,
            image_enable_daily_limit=False,
            image_show_model_info=False,
        )
        plugin._image_batch_gate = asyncio.Semaphore(3)
        plugin._selfie_batch_gate = plugin._image_batch_gate
        plugin._image_batch_cooldown_lock = asyncio.Lock()
        plugin._web_task_lock = threading.RLock()
        plugin._web_tasks = {"task": {"cancel_requested": False}}
        plugin._record_generated_images = lambda *_args: None
        plugin._send_generated_images = lambda *_args: asyncio.sleep(0)
        started_at = []

        async def run_one(_index):
            started_at.append(time.monotonic())
            return {"success": True, "files": ["generated.png"]}

        cooldown = 0.02
        with patch("astrbot_plugin_selfie_image.main.IMAGE_BATCH_REQUEST_COOLDOWN_SECONDS", cooldown):
            result = await plugin._run_counted_generation_shots(
                task_id="task",
                event=Event(),
                total=3,
                fail_label="failed",
                run_one=run_one,
                log_prefix="parallel cooldown batch",
            )

        self.assertEqual(result["succeeded_count"], 3)
        self.assertEqual(len(started_at), 3)
        for previous, current in zip(started_at, started_at[1:]):
            self.assertGreaterEqual(current - previous, cooldown * 0.75)

    async def test_single_shot_skips_batch_cooldown(self) -> None:
        from types import SimpleNamespace
        from astrbot_plugin_selfie_image.main import SelfieImagePlugin

        class Event:
            def plain_result(self, text):
                return text

            async def send(self, _message):
                return None

        plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
        plugin.config = SimpleNamespace(
            image_max_concurrent_tasks=1,
            image_show_generation_info=False,
            image_enable_daily_limit=False,
            image_show_model_info=False,
        )
        plugin._image_batch_gate = asyncio.Semaphore(1)
        plugin._web_task_lock = threading.RLock()
        plugin._web_tasks = {"task": {"cancel_requested": False}}
        plugin._record_generated_images = lambda *_args: None

        async def send_images(*_args):
            return None

        async def unexpected_cooldown(_task_id):
            raise AssertionError("single image must not enter batch cooldown")

        async def run_one(_index):
            return {"success": True, "files": ["generated.png"]}

        plugin._send_generated_images = send_images
        plugin._wait_for_image_batch_cooldown = unexpected_cooldown
        result = await plugin._run_counted_generation_shots(
            task_id="task",
            event=Event(),
            total=1,
            fail_label="failed",
            run_one=run_one,
            log_prefix="single shot",
        )

        self.assertEqual(result["succeeded_count"], 1)
        self.assertEqual(plugin._image_batch_gate._value, 1)


class DailySelfieLlmFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_profile_prefers_valid_llm_json(self) -> None:
        from astrbot_plugin_selfie_image.features.persona import PersonaManager

        periods = {key: f"{key} 的自然状态" for key in ("morning", "noon", "afternoon", "evening", "night", "late_night")}
        with tempfile.TemporaryDirectory() as tmp:
            manager = PersonaManager(tmp)

            async def generate(_: str) -> str:
                return json.dumps({"outfit": "浅蓝衬衫和白色长裙", "mood": "清爽", "status_by_period": periods}, ensure_ascii=False)

            profile = await manager.ensure_daily_selfie_profile("看看你", llm_generate=generate)
            self.assertEqual(profile.source, "llm")
            self.assertEqual(profile.outfit, "浅蓝衬衫和白色长裙")

    async def test_daily_profile_falls_back_when_llm_fails(self) -> None:
        from astrbot_plugin_selfie_image.features.persona import PersonaManager

        with tempfile.TemporaryDirectory() as tmp:
            manager = PersonaManager(tmp)

            async def generate(_: str) -> str:
                raise TimeoutError("timeout")

            profile = await manager.ensure_daily_selfie_profile("看看你", llm_generate=generate)
            self.assertEqual(profile.source, "fallback")
            self.assertTrue(profile.outfit)
            for token in ("清晨穿着", "午后是", "傍晚换成", "夜里是", "深夜会偏居家"):
                self.assertNotIn(token, profile.outfit)

    def test_auxiliary_identity_references_are_limited_and_keep_primary(self) -> None:
        from astrbot_plugin_selfie_image.features.persona import PersonaManager

        with tempfile.TemporaryDirectory() as tmp:
            manager = PersonaManager(tmp)
            manager.save_reference_image(PNG_BYTES, "image/png")
            primary = manager.get_reference_image()
            self.assertIsNotNone(primary)
            for _ in range(3):
                manager.add_auxiliary_reference_image(PNG_BYTES, "image/png")
            auxiliary = manager.get_auxiliary_reference_images()
            self.assertEqual(len(auxiliary), 3)
            self.assertTrue(all(item["data"] == PNG_BYTES for item in auxiliary))
            with self.assertRaisesRegex(ValueError, "最多 3 张"):
                manager.add_auxiliary_reference_image(PNG_BYTES, "image/png")
            removed_id = str(auxiliary[1]["id"])
            manager.remove_auxiliary_reference_image(removed_id)
            self.assertEqual(len(manager.get_auxiliary_reference_images()), 2)
            self.assertEqual(manager.get_reference_image()["data"], primary["data"])
            manager.clear_reference_image()
            self.assertFalse(manager.has_reference_image())
            self.assertEqual(len(manager.get_auxiliary_reference_images()), 2)
            manager.clear_auxiliary_reference_images()
            self.assertEqual(len(manager.get_auxiliary_reference_images()), 0)

    def test_auxiliary_persona_commands_are_documented_and_registered(self) -> None:
        main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        readme_src = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        for token in ('@filter.command("辅助形象设置")', '@filter.command("辅助形象清除")', "合影时只使用主形象图"):
            self.assertIn(token, main_src)
        self.assertIn("/辅助形象设置", readme_src)
        self.assertIn("/辅助形象清除", readme_src)

if __name__ == "__main__":
    unittest.main()
