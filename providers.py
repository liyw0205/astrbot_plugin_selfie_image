"""Image generation provider adapters."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

from .models import ImageModelTarget
from .provider_parser import (
    add_html_image_candidate,
    add_maybe_image_url,
    add_srcset_image_urls,
    b64_to_bytes,
    build_model_list_urls,
    clean_image_url,
    collect_images_from_unknown,
    extract_image_urls_from_text,
    extract_model_ids_from_response,
    extract_openai_images_data,
    fetch_generated_image_url,
    http_error_preview,
    images_from_response_unknown,
    looks_like_binary_image,
    looks_like_relative_image_url,
    normalize_gemini_base_url,
    normalize_image_base_url,
    provider_type_from_channel_payload,
    response_preview,
    resolve_response_url,
)
from .utils import bytes_to_data_url


@dataclass
class ImageReference:
    data: bytes
    mime_type: str = "image/png"
    source_url: str = ""


@dataclass
class ImageGenerateRequest:
    prompt: str
    aspect_ratio: str = "自动"
    resolution: str = "1K"
    images: List[ImageReference] = field(default_factory=list)
    allow_compat_retry: bool = True
    max_image_bytes: int = 25 * 1024 * 1024


@dataclass
class ImageGenerateResult:
    images: List[bytes] = field(default_factory=list)
    error: str = ""
    used_model: str = ""
    attempts: List[Dict[str, Any]] = field(default_factory=list)


class BaseImageAdapter:
    def __init__(self, target: ImageModelTarget, session: aiohttp.ClientSession):
        self.target = target
        self.session = session

    def build_json_headers(self, headers: Optional[Dict[str, str]] = None, *, bearer_auth: bool = True) -> Dict[str, str]:
        request_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "close",
            "User-Agent": "AI-Cat/1.0",
        }
        if bearer_auth and self.target.api_key:
            request_headers["Authorization"] = f"Bearer {self.target.api_key}"
        if headers:
            request_headers.update(headers)
        return request_headers

    async def post_json(
        self,
        url: str,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
        *,
        bearer_auth: bool = True,
    ) -> aiohttp.ClientResponse:
        return await self.session.post(
            url,
            json=payload,
            headers=self.build_json_headers(headers, bearer_auth=bearer_auth),
            timeout=aiohttp.ClientTimeout(total=self.target.timeout),
            proxy=str(self.target.proxy or "").strip() or None,
        )

    async def response_json_or_error(
        self,
        response: aiohttp.ClientResponse,
        *,
        http_preview_limit: int = 500,
        invalid_json_preview_limit: int = 300,
    ) -> tuple[Optional[Any], str]:
        # Prefer raw bytes then utf-8 decode: large b64_json bodies are common for image APIs.
        try:
            raw = await response.read()
        except Exception as exc:
            # Some relays advertise Content-Length / chunked then reset mid-body
            # (TransferEncodingError / Connection reset). Surface as switchable error.
            msg = str(exc).strip() or type(exc).__name__
            return None, f"上游响应未完整接收: {msg}"
        try:
            text = raw.decode(response.charset or "utf-8", errors="replace")
        except Exception:
            text = raw.decode("utf-8", errors="replace")
        if response.status >= 400:
            return None, f"HTTP {response.status}: {http_error_preview(text, http_preview_limit)}"
        try:
            return json.loads(text), ""
        except json.JSONDecodeError:
            return None, f"接口返回非 JSON 内容: {response_preview(text, invalid_json_preview_limit)}"

    async def post_json_data_or_error(
        self,
        url: str,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
        *,
        bearer_auth: bool = True,
        http_preview_limit: int = 500,
        invalid_json_preview_limit: int = 300,
    ) -> tuple[Optional[Any], str]:
        async with await self.post_json(url, payload, headers=headers, bearer_auth=bearer_auth) as response:
            return await self.response_json_or_error(
                response,
                http_preview_limit=http_preview_limit,
                invalid_json_preview_limit=invalid_json_preview_limit,
            )

    async def result_from_response(
        self,
        data: Any,
        req: ImageGenerateRequest,
        base_url: str,
        *,
        provider_name: str = "",
        detailed_error: bool = False,
    ) -> ImageGenerateResult:
        images = await images_from_response_unknown(
            self.session, data, self.target.timeout, req.max_image_bytes, self.target.proxy, base_url
        )
        if images:
            return ImageGenerateResult(images=images)
        if not detailed_error:
            return ImageGenerateResult(error="未生成任何图片")

        preview = response_preview(data)
        collected = collect_images_from_unknown(data)
        prefix = f"{provider_name} " if provider_name else ""
        if collected["urls"]:
            return ImageGenerateResult(
                error=f"{prefix}接口返回了图片链接但下载失败。链接数: {len(collected['urls'])}；返回预览: {preview}"
            )
        if collected["b64"]:
            return ImageGenerateResult(
                error=f"{prefix}接口返回了 base64 图片但解码失败。数量: {len(collected['b64'])}；返回预览: {preview}"
            )
        return ImageGenerateResult(error=f"{prefix}未识别到可下载图片字段。返回预览: {preview}")

    async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        raise NotImplementedError


def map_aspect_ratio_to_openai_size(aspect: str) -> str:
    if not aspect or aspect in {"自动", "1:1"}:
        return "1024x1024"
    if aspect in {"16:9", "3:2", "4:3", "5:4", "21:9"}:
        return "1792x1024"
    return "1024x1792"


def map_aspect_ratio_to_gpt_image_size(aspect: str, *, allow_omit_auto: bool = False) -> str:
    """Map aspect to GPT Image size tokens.

    Official GPT Image accepts only 1024x1024 / 1536x1024 / 1024x1536.
    When allow_omit_auto and aspect is 自动/空: return "" so callers can omit size
    (img_gen leaves unspecified size unset; some relays reject explicit size on edits).
    """
    if not aspect or aspect in {"自动", ""}:
        return "" if allow_omit_auto else "1024x1024"
    if aspect in {"1:1"}:
        return "1024x1024"
    if aspect in {"3:2", "16:9", "4:3", "5:4", "21:9"}:
        return "1536x1024"
    if aspect in {"2:3", "3:4", "9:16", "4:5"}:
        return "1024x1536"
    return "1024x1024"


def map_aspect_ratio_to_agnes_size(aspect: str) -> str:
    if not aspect or aspect in {"自动", "1:1"}:
        return "1024x1024"
    if aspect == "16:9":
        return "1024x576"
    if aspect == "9:16":
        return "576x1024"
    if aspect == "3:2":
        return "1024x682"
    if aspect == "2:3":
        return "682x1024"
    if aspect == "4:3":
        return "1024x768"
    if aspect == "3:4":
        return "768x1024"
    if aspect == "4:5":
        return "819x1024"
    if aspect == "5:4":
        return "1024x819"
    if aspect == "21:9":
        return "1024x439"
    return "1024x1024"


def is_gpt_image_model(model: str) -> bool:
    return "gpt-image" in str(model or "").lower()


def _gpt_image_payload_profiles(req: ImageGenerateRequest, model: str) -> List[Dict[str, Any]]:
    """Build one or two OpenAI Images payloads (standard then flexible).

    Source: starmiaoa/astrbot-plugin-gpt-image dual-profile idea (target 10).
    - standard: explicit pixel size (official / many NewAPI relays)
    - flexible: omit rigid size when aspect is 自动, or add quality-friendly fields for picky relays
    """
    base_model = model or "gpt-image-1"
    size = map_aspect_ratio_to_gpt_image_size(req.aspect_ratio)
    standard: Dict[str, Any] = {
        "model": base_model,
        "prompt": req.prompt,
        "n": 1,
        "size": size,
    }
    profiles = [standard]
    # Second profile: some relays dislike size / prefer response_format or auto-ish body.
    flexible: Dict[str, Any] = {
        "model": base_model,
        "prompt": req.prompt,
        "n": 1,
    }
    if req.aspect_ratio and req.aspect_ratio not in {"自动", "1:1", ""}:
        flexible["size"] = size
    else:
        # Keep an explicit square for 自动 to avoid upstream auto stalls, but drop extra fields.
        flexible["size"] = "1024x1024"
    # Only add a distinct second attempt when it differs or when compat retry is allowed.
    if flexible != standard:
        profiles.append(flexible)
    elif req.allow_compat_retry:
        alt = dict(standard)
        alt["response_format"] = "b64_json"
        if alt != standard:
            profiles.append(alt)
    return profiles


class OpenAIImageAdapter(BaseImageAdapter):
    async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        if req.images:
            if not is_gpt_image_model(self.target.model):
                return ImageGenerateResult(error="OpenAI 图生图仅支持 gpt-image 系列模型，DALL-E 系列不支持参考图")
            return await self.generate_edit(req)
        return await self.generate_image(req)

    def build_image_payload(self, req: ImageGenerateRequest, *, profile: str = "standard") -> Dict[str, Any]:
        gpt_image = is_gpt_image_model(self.target.model)
        if not gpt_image:
            payload: Dict[str, Any] = {
                "model": self.target.model or "dall-e-3",
                "prompt": req.prompt,
                "n": 1,
                "size": map_aspect_ratio_to_openai_size(req.aspect_ratio),
                "response_format": "b64_json",
            }
            return payload
        profiles = _gpt_image_payload_profiles(req, self.target.model or "gpt-image-1")
        if profile == "flexible" and len(profiles) > 1:
            return profiles[1]
        return profiles[0]

    async def generate_image(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        base = normalize_image_base_url(self.target.base_url) or "https://api.openai.com"
        url = f"{base}/v1/images/generations"
        from .error_classify import is_param_profile_switch_error

        if is_gpt_image_model(self.target.model):
            profiles = _gpt_image_payload_profiles(req, self.target.model or "gpt-image-1")
        else:
            profiles = [self.build_image_payload(req)]

        last_error = ""
        # At most one create POST per profile; never loop the same billable body on timeout.
        for index, payload in enumerate(profiles):
            try:
                data, error = await self.post_json_data_or_error(url, payload)
            except asyncio.TimeoutError:
                return ImageGenerateResult(
                    error=f"OpenAI 生图请求超时（{self.target.timeout}秒；为避免重复扣费，不会自动重提）"
                )
            if error or data is None:
                last_error = error or "接口未返回有效 JSON"
                # Switch profile only on parameter-class failures, and only once.
                if (
                    index == 0
                    and len(profiles) > 1
                    and req.allow_compat_retry
                    and is_param_profile_switch_error(last_error)
                ):
                    continue
                return ImageGenerateResult(error=last_error)
            images = extract_openai_images_data(data, req.max_image_bytes)
            if images:
                return ImageGenerateResult(images=images)
            return await self.result_from_response(data, req, base, detailed_error=True)
        return ImageGenerateResult(error=last_error or "接口未返回有效 JSON")

    def _build_edit_form(
        self,
        req: ImageGenerateRequest,
        image_field_name: str,
        *,
        include_size: bool = True,
    ) -> aiohttp.FormData:
        form = aiohttp.FormData()
        form.add_field("model", self.target.model or "gpt-image-1")
        form.add_field("prompt", req.prompt)
        form.add_field("n", "1")
        if include_size:
            # When profile asks for size: always send one of the 3 official tokens.
            # 自动 / empty → 1024x1024 (do not omit here; omit is a separate profile).
            size = map_aspect_ratio_to_gpt_image_size(req.aspect_ratio, allow_omit_auto=False)
            if size:
                form.add_field("size", size)
        # Official GPT Image / many NewAPI relays expect repeated image[] parts (img_gen).
        # Some older relays only accept bare "image". Caller tries preferred name first.
        for index, image in enumerate(req.images):
            ext = "jpg" if "jpeg" in image.mime_type else "webp" if "webp" in image.mime_type else "gif" if "gif" in image.mime_type else "png"
            form.add_field(
                image_field_name,
                image.data,
                filename=f"image_{index}.{ext}",
                content_type=image.mime_type or "image/png",
            )
        return form

    async def _post_edit_form(
        self,
        url: str,
        req: ImageGenerateRequest,
        image_field_name: str,
        *,
        include_size: bool = True,
    ) -> tuple[Optional[Any], str]:
        headers = {
            "Accept": "application/json",
            "Connection": "close",
            "User-Agent": "AI-Cat/1.0",
        }
        if self.target.api_key:
            headers["Authorization"] = f"Bearer {self.target.api_key}"
        async with self.session.post(
            url,
            data=self._build_edit_form(req, image_field_name, include_size=include_size),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=self.target.timeout),
            proxy=str(self.target.proxy or "").strip() or None,
        ) as response:
            return await self.response_json_or_error(response)

    async def generate_edit(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        base = normalize_image_base_url(self.target.base_url) or "https://api.openai.com"
        url = f"{base}/v1/images/edits"
        # Profiles: field name + size omit. Cap attempts to avoid multi-bill spam.
        # Prefer sized image[] first: some relays (e.g. WisArt) often reset mid-body
        # on omit-size edits, while sized body completes more reliably.
        if req.allow_compat_retry:
            profiles = [
                ("image[]", True),
                ("image", True),
                ("image[]", False),
            ]
        else:
            profiles = [("image[]", True)]
        # If user fixed a non-auto ratio, keep sized body first (already default).
        if req.aspect_ratio and req.aspect_ratio not in {"自动", "", "1:1"}:
            profiles = [
                ("image[]", True),
                ("image", True),
                ("image[]", False),
            ] if req.allow_compat_retry else [("image[]", True)]

        last_error = ""
        try:
            from .error_classify import is_param_profile_switch_error, is_transport_profile_switch_error

            for index, (field_name, include_size) in enumerate(profiles):
                data, error = await self._post_edit_form(
                    url, req, field_name, include_size=include_size
                )
                if not error and data is not None:
                    images = extract_openai_images_data(data, req.max_image_bytes)
                    if images:
                        return ImageGenerateResult(images=images)
                    return await self.result_from_response(data, req, base, detailed_error=True)
                last_error = error or "接口未返回有效 JSON"
                # Switch profile on param/schema issues or incomplete transfer resets.
                if index + 1 < len(profiles) and (
                    is_param_profile_switch_error(last_error)
                    or is_transport_profile_switch_error(last_error)
                    or "image" in str(last_error).lower()
                ):
                    continue
                break
            return ImageGenerateResult(error=last_error or "接口未返回有效 JSON")
        except asyncio.TimeoutError:
            return ImageGenerateResult(error=f"OpenAI 图生图请求超时（{self.target.timeout}秒；为避免重复扣费，不会自动重提）")


class GeminiImageAdapter(BaseImageAdapter):
    def build_payload(self, req: ImageGenerateRequest) -> Dict[str, Any]:
        parts: List[Dict[str, Any]] = [{"text": req.prompt}]
        for image in req.images:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": image.mime_type or "image/png",
                        "data": base64.b64encode(image.data).decode("utf-8"),
                    }
                }
            )
        return {
            "contents": [{"parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }

    async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        base = normalize_gemini_base_url(self.target.base_url) or "https://generativelanguage.googleapis.com"
        model_path = self.target.model if self.target.model.startswith("models/") else f"models/{self.target.model}"
        url = f"{base}/v1beta/{model_path}:generateContent"
        headers = {
            "x-goog-api-key": self.target.api_key,
        }
        try:
            data, error = await self.post_json_data_or_error(url, self.build_payload(req), headers=headers, bearer_auth=False)
            if error or data is None:
                return ImageGenerateResult(error=error or "接口未返回有效 JSON")
        except asyncio.TimeoutError:
            return ImageGenerateResult(error=f"Gemini 生图请求超时（{self.target.timeout}秒）")
        return await self.result_from_response(data, req, base)


class GeminiOpenAIImageAdapter(BaseImageAdapter):
    def build_payload(self, req: ImageGenerateRequest) -> Dict[str, Any]:
        content: List[Dict[str, Any]] = [{"type": "text", "text": f"Generate an image: {req.prompt}"}]
        for image in req.images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": bytes_to_data_url(image.data, image.mime_type)},
                }
            )
        return {
            "model": self.target.model,
            "messages": [{"role": "user", "content": content}],
            "modalities": ["image", "text"],
            "stream": False,
        }

    async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        base = normalize_image_base_url(self.target.base_url)
        url = f"{base}/v1/chat/completions"
        data, error = await self.post_json_data_or_error(url, self.build_payload(req))
        if error or data is None:
            return ImageGenerateResult(error=error or "接口未返回有效 JSON")
        return await self.result_from_response(data, req, base, detailed_error=True)


class SimpleOpenAIImageAdapter(BaseImageAdapter):
    default_base_url = ""
    default_model = ""

    def build_payload(self, req: ImageGenerateRequest) -> Dict[str, Any]:
        return {
            "model": self.target.model or self.default_model,
            "prompt": req.prompt,
            "response_format": "b64_json",
        }

    async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        base = normalize_image_base_url(self.target.base_url) or self.default_base_url
        url = f"{base}/v1/images/generations"
        data, error = await self.post_json_data_or_error(url, self.build_payload(req), http_preview_limit=300)
        if error or data is None:
            return ImageGenerateResult(error=error or "接口未返回有效 JSON")
        return await self.result_from_response(data, req, base)


class ZImageAdapter(SimpleOpenAIImageAdapter):
    default_base_url = "https://ai.gitee.com"
    default_model = "z-image-turbo"

    def build_payload(self, req: ImageGenerateRequest) -> Dict[str, Any]:
        return {
            "model": self.target.model or self.default_model,
            "prompt": req.prompt,
            "size": "1024x1024",
            "num_inference_steps": 9,
        }


class JimengImageAdapter(SimpleOpenAIImageAdapter):
    default_base_url = "http://localhost:5100"
    default_model = "jimeng-4.5"


class GrokImageAdapter(SimpleOpenAIImageAdapter):
    default_base_url = "https://api.x.ai"
    default_model = "grok-imagine-image"

    def build_payload(self, req: ImageGenerateRequest) -> Dict[str, Any]:
        return {
            "model": self.target.model or self.default_model,
            "prompt": req.prompt,
            "aspect_ratio": "auto" if req.aspect_ratio == "自动" else (req.aspect_ratio or "auto"),
            "resolution": (req.resolution or "2K").lower(),
            "response_format": "b64_json",
        }


class AgnesImageAdapter(BaseImageAdapter):
    default_base_url = "https://apihub.agnes-ai.com"
    default_model = "agnes-image-2.1-flash"

    def _reference_image_value(self, image: ImageReference) -> str:
        source_url = str(image.source_url or "").strip()
        if source_url.lower().startswith(("http://", "https://")):
            return source_url
        return bytes_to_data_url(image.data, image.mime_type)

    def build_payload(self, req: ImageGenerateRequest) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.target.model or self.default_model,
            "prompt": req.prompt,
            "size": map_aspect_ratio_to_agnes_size(req.aspect_ratio),
        }
        extra_body: Dict[str, Any] = {}
        if req.images:
            extra_body["image"] = [self._reference_image_value(image) for image in req.images if image.data]
            extra_body["response_format"] = "url"
        if extra_body:
            payload["extra_body"] = extra_body
        return payload

    async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        base = normalize_image_base_url(self.target.base_url) or self.default_base_url
        url = f"{base}/v1/images/generations"
        payload = self.build_payload(req)
        data, error = await self.post_json_data_or_error(url, payload, http_preview_limit=300)
        if error or data is None:
            return ImageGenerateResult(error=error or "接口未返回有效 JSON")
        return await self.result_from_response(data, req, base, provider_name="Agnes", detailed_error=True)


# NovelAI default UC — keep short; selfie prompts already carry identity/quality text.
NAI_DEFAULT_NEGATIVE = (
    "lowres, worst quality, bad quality, bad anatomy, bad hands, extra digits, "
    "fewer digits, cropped, jpeg artifacts, blurry, watermark, text, logo"
)


def map_aspect_ratio_to_nai_size(aspect: str, resolution: str = "1K") -> tuple[int, int]:
    """Map Selfie aspect presets to NovelAI-friendly pixel sizes."""
    aspect = str(aspect or "自动").strip()
    res = str(resolution or "1K").strip().upper()
    # Official common presets; 2K/4K only meaningfully used by gateway size labels.
    if aspect in {"16:9", "3:2", "4:3", "5:4", "21:9"}:
        base = (1216, 832)
    elif aspect in {"9:16", "2:3", "3:4", "4:5"}:
        base = (832, 1216)
    else:
        base = (1024, 1024)
    if res == "2K":
        return (min(base[0] * 2, 2048), min(base[1] * 2, 2048))
    if res == "4K":
        # Official NAI rarely accepts true 4K; keep modest upscale for gateways.
        return (min(base[0] * 2, 1536) if base[0] != base[1] else 1536, min(base[1] * 2, 1536) if base[0] != base[1] else 1536)
    return base


def map_aspect_ratio_to_nai_gateway_size(aspect: str, resolution: str = "1K") -> str:
    """Nai2API / third-party GET gateways use Chinese size labels."""
    aspect = str(aspect or "自动").strip()
    res = str(resolution or "1K").strip().upper()
    if aspect in {"16:9", "3:2", "4:3", "5:4", "21:9"}:
        orient = "横图"
    elif aspect in {"9:16", "2:3", "3:4", "4:5"}:
        orient = "竖图"
    else:
        orient = "方图"
    if res == "4K":
        return f"4K{orient}"
    if res == "2K":
        return f"2K{orient}"
    return orient


def _extract_image_bytes_from_nai_body(body: bytes) -> Optional[bytes]:
    """Official NAI returns zip; gateways may return raw image bytes."""
    if not body:
        return None
    if looks_like_binary_image(body):
        return body
    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            for name in zf.namelist():
                lower = name.lower()
                if lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
                    data = zf.read(name)
                    if data:
                        return data
            # first file fallback
            names = zf.namelist()
            if names:
                data = zf.read(names[0])
                if data:
                    return data
    except zipfile.BadZipFile:
        pass
    return None


class NovelAIImageAdapter(BaseImageAdapter):
    """NovelAI image channel.

    Modes (auto):
    - official: POST {base}/ai/generate-image  (default base https://api.novelai.net)
      Bearer token; response zip or raw image.
    - gateway: GET {base}/generate?...  (Nai2API / nai.sta1n.cn style)
      token query param; response raw image bytes.

    Source protocols: astrbot_plugin_nai_canvas, AstrBot_Nai2API, ppnai official body.
    """

    default_base_url = "https://api.novelai.net"
    default_model = "nai-diffusion-4-5-full"

    def _mode(self) -> str:
        base = (normalize_image_base_url(self.target.base_url) or self.default_base_url).lower()
        if "novelai.net" in base or base.rstrip("/").endswith("/ai") or "/ai/generate-image" in base:
            return "official"
        # Common gateway hosts / path hints
        if any(k in base for k in ("sta1n", "nai2api", "loliyc", "/generate")):
            return "gateway"
        # If user points base_url at a host without novelai, prefer gateway GET /generate
        if "novelai" not in base:
            return "gateway"
        return "official"

    def _endpoint(self, mode: str) -> str:
        raw = str(self.target.base_url or "").strip()
        base = normalize_image_base_url(raw) or self.default_base_url
        lower = raw.lower()
        if mode == "official":
            if lower.endswith("/ai/generate-image") or lower.endswith("generate-image"):
                return raw.rstrip("/")
            return f"{base.rstrip('/')}/ai/generate-image"
        # gateway
        if lower.rstrip("/").endswith("/generate"):
            return raw.rstrip("/")
        return f"{base.rstrip('/')}/generate"

    def build_official_payload(self, req: ImageGenerateRequest) -> Dict[str, Any]:
        import random

        width, height = map_aspect_ratio_to_nai_size(req.aspect_ratio, req.resolution)
        prompt = str(req.prompt or "").strip()
        negative = NAI_DEFAULT_NEGATIVE
        seed = random.randint(1, 2**31 - 1)
        model = self.target.model or self.default_model
        parameters: Dict[str, Any] = {
            "params_version": 3,
            "width": width,
            "height": height,
            "scale": 5.0,
            "sampler": "k_dpmpp_2m",
            "steps": 28,
            "seed": seed,
            "n_samples": 1,
            "ucPreset": 0,
            "qualityToggle": False,
            "sm": False,
            "sm_dyn": False,
            "dynamic_thresholding": False,
            "controlnet_strength": 1,
            "legacy": False,
            "add_original_image": True,
            "cfg_rescale": 0,
            "noise_schedule": "karras",
            "legacy_v3_extend": False,
            "skip_cfg_above_sigma": None,
            "use_coords": False,
            "legacy_uc": False,
            "normalize_strength": 1,
            "inpaintImg2ImgStrength": 1,
            "noise": 0,
            "strength": 0.7,
            "negative_prompt": negative,
            "uc": negative,
            "v4_prompt": {
                "caption": {"base_caption": prompt, "char_captions": []},
                "use_coords": False,
                "use_order": True,
            },
            "v4_negative_prompt": {
                "caption": {"base_caption": negative, "char_captions": []},
                "legacy_uc": False,
            },
        }
        action = "generate"
        if req.images:
            # img2img: first reference as base image (ppnai style bare base64)
            raw_b64 = base64.b64encode(req.images[0].data).decode("ascii")
            parameters["image"] = raw_b64
            parameters["strength"] = 0.55
            parameters["noise"] = 0
            action = "img2img"
        return {
            "input": prompt,
            "model": model,
            "action": action,
            "parameters": parameters,
        }

    def build_gateway_params(self, req: ImageGenerateRequest) -> Dict[str, str]:
        params = {
            "token": str(self.target.api_key or "").strip(),
            "tag": str(req.prompt or "").strip(),
            "model": self.target.model or self.default_model,
            "size": map_aspect_ratio_to_nai_gateway_size(req.aspect_ratio, req.resolution),
            "steps": "28",
            "scale": "6",
            "cfg": "0",
            "sampler": "k_dpmpp_2m_sde",
            "noise_schedule": "karras",
            "nocache": "1",
            "negative": NAI_DEFAULT_NEGATIVE,
        }
        return {k: v for k, v in params.items() if v is not None and str(v) != ""}

    async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        if not str(self.target.api_key or "").strip():
            return ImageGenerateResult(error="NovelAI 渠道缺少 API Token")
        if not str(req.prompt or "").strip():
            return ImageGenerateResult(error="提示词为空")

        mode = self._mode()
        url = self._endpoint(mode)
        proxy = str(self.target.proxy or "").strip() or None
        timeout = aiohttp.ClientTimeout(total=self.target.timeout)
        try:
            if mode == "official":
                payload = self.build_official_payload(req)
                headers = self.build_json_headers()
                async with self.session.post(url, json=payload, headers=headers, timeout=timeout, proxy=proxy) as response:
                    body = await response.read()
                    if response.status >= 400:
                        preview = body[:400].decode("utf-8", errors="replace")
                        return ImageGenerateResult(error=f"HTTP {response.status}: {preview or 'NovelAI 请求失败'}")
                    image = _extract_image_bytes_from_nai_body(body)
                    if image:
                        return ImageGenerateResult(images=[image])
                    ctype = response.headers.get("Content-Type", "")
                    return ImageGenerateResult(error=f"NovelAI 未返回可解析图片（Content-Type={ctype}）")
            # gateway GET
            params = self.build_gateway_params(req)
            # token in query — do not also send Authorization unless host wants both
            headers = {
                "Accept": "image/*,application/zip,application/json,*/*",
                "Connection": "close",
                "User-Agent": "AI-Cat/1.0",
            }
            async with self.session.get(url, params=params, headers=headers, timeout=timeout, proxy=proxy) as response:
                body = await response.read()
                if response.status >= 400:
                    preview = body[:400].decode("utf-8", errors="replace")
                    return ImageGenerateResult(error=f"HTTP {response.status}: {preview or 'NAI 网关请求失败'}")
                image = _extract_image_bytes_from_nai_body(body)
                if image:
                    return ImageGenerateResult(images=[image])
                # some gateways wrap JSON
                try:
                    data = json.loads(body.decode("utf-8", errors="replace"))
                except Exception:
                    data = None
                if data is not None:
                    return await self.result_from_response(data, req, normalize_image_base_url(self.target.base_url) or "", provider_name="NovelAI", detailed_error=True)
                return ImageGenerateResult(error="NAI 网关未返回可解析图片")
        except asyncio.TimeoutError:
            return ImageGenerateResult(error=f"NovelAI 请求超时（{self.target.timeout}秒）")
        except Exception as exc:
            return ImageGenerateResult(error=str(exc) or "NovelAI 请求失败")


def create_adapter(target: ImageModelTarget, session: aiohttp.ClientSession) -> BaseImageAdapter:
    if target.provider_type == "openai":
        return OpenAIImageAdapter(target, session)
    if target.provider_type == "gemini":
        return GeminiImageAdapter(target, session)
    if target.provider_type == "gemini_openai":
        return GeminiOpenAIImageAdapter(target, session)
    if target.provider_type == "z_image_gitee":
        return ZImageAdapter(target, session)
    if target.provider_type == "jimeng2api":
        return JimengImageAdapter(target, session)
    if target.provider_type == "grok":
        return GrokImageAdapter(target, session)
    if target.provider_type == "agnes":
        return AgnesImageAdapter(target, session)
    if target.provider_type == "novelai":
        return NovelAIImageAdapter(target, session)
    raise ValueError(f"未知生图渠道类型: {target.provider_type}")
