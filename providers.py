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
        text = await response.text()
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


def map_aspect_ratio_to_gpt_image_size(aspect: str) -> str:
    if not aspect or aspect == "自动":
        return "auto"
    if aspect == "1:1":
        return "1024x1024"
    if aspect in {"3:2", "16:9", "4:3", "5:4", "21:9"}:
        return "1536x1024"
    if aspect in {"2:3", "3:4", "9:16", "4:5"}:
        return "1024x1536"
    return "auto"


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


class OpenAIImageAdapter(BaseImageAdapter):
    async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        if req.images:
            if not is_gpt_image_model(self.target.model):
                return ImageGenerateResult(error="OpenAI 图生图仅支持 gpt-image 系列模型，DALL-E 系列不支持参考图")
            return await self.generate_edit(req)
        return await self.generate_image(req)

    def build_image_payload(self, req: ImageGenerateRequest) -> Dict[str, Any]:
        gpt_image = is_gpt_image_model(self.target.model)
        payload: Dict[str, Any] = {
            "model": self.target.model or ("gpt-image-1" if gpt_image else "dall-e-3"),
            "prompt": req.prompt,
            "n": 1,
            "size": map_aspect_ratio_to_gpt_image_size(req.aspect_ratio) if gpt_image else map_aspect_ratio_to_openai_size(req.aspect_ratio),
        }
        if not gpt_image:
            payload["response_format"] = "b64_json"
        return payload

    async def generate_image(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        base = normalize_image_base_url(self.target.base_url) or "https://api.openai.com"
        url = f"{base}/v1/images/generations"
        data, error = await self.post_json_data_or_error(url, self.build_image_payload(req))
        if error or data is None:
            return ImageGenerateResult(error=error or "接口未返回有效 JSON")
        return await self.result_from_response(data, req, base)

    def _build_edit_form(self, req: ImageGenerateRequest, image_field_name: str) -> aiohttp.FormData:
        form = aiohttp.FormData()
        form.add_field("model", self.target.model or "gpt-image-1")
        form.add_field("prompt", req.prompt)
        form.add_field("n", "1")
        size = map_aspect_ratio_to_gpt_image_size(req.aspect_ratio)
        if size:
            form.add_field("size", size)
        for index, image in enumerate(req.images):
            ext = "jpg" if "jpeg" in image.mime_type else "webp" if "webp" in image.mime_type else "gif" if "gif" in image.mime_type else "png"
            form.add_field(
                image_field_name,
                image.data,
                filename=f"image_{index}.{ext}",
                content_type=image.mime_type or "image/png",
            )
        return form

    async def _post_edit_form(self, url: str, req: ImageGenerateRequest, image_field_name: str) -> tuple[Optional[Any], str]:
        headers = {
            "Accept": "application/json",
            "Connection": "close",
            "User-Agent": "AI-Cat/1.0",
        }
        if self.target.api_key:
            headers["Authorization"] = f"Bearer {self.target.api_key}"
        async with self.session.post(
            url,
            data=self._build_edit_form(req, image_field_name),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=self.target.timeout),
            proxy=str(self.target.proxy or "").strip() or None,
        ) as response:
            return await self.response_json_or_error(response)

    async def generate_edit(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        base = normalize_image_base_url(self.target.base_url) or "https://api.openai.com"
        url = f"{base}/v1/images/edits"
        try:
            data, error = await self._post_edit_form(url, req, "image")
            if error and req.allow_compat_retry:
                fallback_data, fallback_error = await self._post_edit_form(url, req, "image[]")
                if fallback_data is not None:
                    data, error = fallback_data, ""
                elif fallback_error:
                    error = f"{error}；兼容 image[] 重试也失败: {fallback_error}"
            if error or data is None:
                return ImageGenerateResult(error=error or "接口未返回有效 JSON")
        except asyncio.TimeoutError:
            return ImageGenerateResult(error=f"OpenAI 图生图请求超时（{self.target.timeout}秒）")
        return await self.result_from_response(data, req, base)


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
    raise ValueError(f"未知生图渠道类型: {target.provider_type}")
