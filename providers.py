"""Image generation provider adapters."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set
from urllib.parse import urljoin

import aiohttp

from .models import ImageModelTarget, normalize_provider_type
from .utils import IMAGE_EXTENSIONS, bytes_to_data_url, decode_base64_payload, decode_html_entities, looks_like_image_bytes, redact_sensitive_data, redact_sensitive_text


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

    async def post_json(self, url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> aiohttp.ClientResponse:
        request_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "close",
            "User-Agent": "AI-Cat/1.0",
        }
        if self.target.api_key:
            request_headers["Authorization"] = f"Bearer {self.target.api_key}"
        if headers:
            request_headers.update(headers)
        return await self.session.post(
            url,
            json=payload,
            headers=request_headers,
            timeout=aiohttp.ClientTimeout(total=self.target.timeout),
            proxy=str(self.target.proxy or "").strip() or None,
        )

    async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        raise NotImplementedError


def normalize_image_base_url(url: str) -> str:
    value = str(url or "").strip().rstrip("/")
    value = re.sub(r"/v1(?:/.*)?$", "", value, flags=re.I)
    value = re.sub(r"/chat/completions$", "", value, flags=re.I)
    value = re.sub(r"/images/(?:generations|edits)$", "", value, flags=re.I)
    return value


def normalize_gemini_base_url(url: str) -> str:
    value = str(url or "").strip().rstrip("/")
    value = re.sub(r"/v1beta(?:/.*)?$", "", value, flags=re.I)
    value = re.sub(r"/v1(?:/.*)?$", "", value, flags=re.I)
    return value


def build_model_list_urls(base_url: str, provider_type: str = "") -> List[str]:
    provider = normalize_provider_type(provider_type) or str(provider_type or "").strip().lower().replace("-", "_")
    if provider == "gemini":
        base = normalize_gemini_base_url(base_url)
        suffixes = ("/v1beta/models", "/v1/models", "/models")
    else:
        base = normalize_image_base_url(base_url)
        suffixes = ("/v1/models", "/models", "/v1beta/models")
    if not base:
        return []
    return [f"{base}{suffix}" for suffix in suffixes]


def provider_type_from_channel_payload(payload: Any, default: str = "openai") -> str:
    if isinstance(payload, dict):
        value = (
            payload.get("provider_type")
            or payload.get("providerType")
            or payload.get("api_type")
            or payload.get("apiType")
            or default
        )
    else:
        value = default
    return normalize_provider_type(value) or normalize_provider_type(default) or "openai"


def extract_model_ids_from_response(data: Any) -> List[str]:
    result: Set[str] = set()
    primary_keys = ("id", "name", "model", "model_id", "modelId", "model_name", "modelName", "slug")
    fallback_keys = ("display_name", "displayName")
    container_keys = ("data", "models", "items", "results", "list", "model_list", "modelList", "available_models", "availableModels", "model_ids", "modelIds")

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text:
            result.add(text)

    def walk(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            add(value)
            return
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return

        found_primary = False
        for key in primary_keys:
            if isinstance(value.get(key), str):
                add(value.get(key))
                found_primary = True
        if not found_primary:
            for key in fallback_keys:
                if isinstance(value.get(key), str):
                    add(value.get(key))

        for key in container_keys:
            walk(value.get(key))

    walk(data)
    return sorted(result)


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


def b64_to_bytes(value: str) -> bytes:
    return decode_base64_payload(value)


def http_error_preview(text: str, limit: int = 500) -> str:
    raw = str(text or "").strip()
    preview = raw

    def first_error_text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("message", "detail", "error_description", "description", "msg", "reason", "error"):
                if key in value:
                    found = first_error_text(value.get(key))
                    if found:
                        return found
            for key in ("errors", "error_messages"):
                if key in value:
                    found = first_error_text(value.get(key))
                    if found:
                        return found
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
            for item in value:
                found = first_error_text(item)
                if found:
                    return found
        return ""

    try:
        data = json.loads(raw)
        preview = first_error_text(data) or raw
    except Exception:
        pass
    preview = re.sub(r"\s+", " ", preview).strip()
    return redact_sensitive_text(preview)[:limit]


def response_preview(value: Any, limit: int = 1000) -> str:
    if isinstance(value, str):
        preview = value
    else:
        try:
            preview = json.dumps(redact_sensitive_data(value), ensure_ascii=False)
        except Exception:
            preview = str(value)
    return redact_sensitive_text(preview)[:limit]


def clean_image_url(url: str) -> str:
    text = decode_html_entities(str(url or "")).strip()
    if text.startswith("<") and ">" in text:
        candidate, rest = text[1:].split(">", 1)
        rest = rest.strip()
        if not rest or re.fullmatch(r"(?:\"[^\"]*\"|'[^']*'|\([^)]*\))", rest):
            text = candidate.strip()
    else:
        match = re.match(r"^(\S+?)(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))$", text, flags=re.I)
        if match:
            text = match.group(1)
    text = text.strip("<> \t\r\n").rstrip("，。！？、；：")
    while text.endswith(")") and "(" not in text:
        text = text[:-1].strip()
    return text


def looks_like_binary_image(data: bytes) -> bool:
    return looks_like_image_bytes(data)


async def fetch_generated_image_url(
    session: aiohttp.ClientSession,
    url: str,
    timeout: int,
    referer: str = "https://flow.google/",
    max_bytes: int = 25 * 1024 * 1024,
    proxy: str = "",
) -> Optional[bytes]:
    text = clean_image_url(url)
    if not text:
        return None
    if text.lower().startswith(("data:image/", "base64://")):
        data = b64_to_bytes(text)
        return data if data and len(data) <= max_bytes and looks_like_binary_image(data) else None
    if not text.lower().startswith(("http://", "https://")):
        return None
    headers = {
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "close",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": referer,
    }
    try:
        async with session.get(
            text,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
            proxy=str(proxy or "").strip() or None,
        ) as response:
            if response.status >= 400:
                return None
            content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            binary_content_types = {"application/octet-stream", "binary/octet-stream", "application/binary", "application/x-binary"}
            if content_type and not content_type.startswith("image/") and content_type not in binary_content_types:
                return None
            content_length = response.headers.get("content-length", "")
            try:
                if content_length and int(content_length) > max_bytes:
                    return None
            except (TypeError, ValueError):
                pass
            chunks: List[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
            data = b"".join(chunks)
            if not data:
                return None
            if not looks_like_binary_image(data):
                return None
            return data
    except Exception:
        return None


def add_maybe_image_url(value: str, b64: Set[str], urls: Set[str], others: Set[str]) -> None:
    url = clean_image_url(value)
    if not url:
        return
    if url.lower().startswith(("data:image/", "base64://")):
        b64.add(url)
    elif url.lower().startswith(("http://", "https://")):
        urls.add(url)
    else:
        others.add(url)


def looks_like_relative_image_url(value: str) -> bool:
    text = clean_image_url(value)
    if not text or re.search(r"\s", text):
        return False
    if text.lower().startswith(("http://", "https://", "data:image/")):
        return False
    path = text.split("?", 1)[0].split("#", 1)[0].lower()
    return text.startswith(("/", "./", "../")) or path.endswith(tuple(IMAGE_EXTENSIONS))


def extract_image_urls_from_text(text: str) -> Dict[str, List[str]]:
    raw = decode_html_entities(str(text or ""))
    b64: Set[str] = set()
    urls: Set[str] = set()
    others: Set[str] = set()

    for match in re.finditer(r"!?\[[^\]]*]\(([\s\S]*?)\)", raw):
        inside = str(match.group(1) or "").strip()
        if inside:
            add_maybe_image_url(inside, b64, urls, others)

    for match in re.finditer(r"<img[^>]+src=[\"']([^\"']+)[\"'][^>]*>", raw, flags=re.I):
        add_maybe_image_url(match.group(1), b64, urls, others)

    for match in re.finditer(r"data:image/[a-zA-Z0-9.+-]+(?:;[^,\s\"'<>;]*)*;base64,[A-Za-z0-9+/=_-]+", raw, flags=re.I):
        b64.add(match.group(0))

    for match in re.finditer(r"base64://[A-Za-z0-9+/=_-]+", raw, flags=re.I):
        b64.add(match.group(0))

    for match in re.finditer(r"https?://[^\s\"'<>]+", raw):
        add_maybe_image_url(match.group(0), b64, urls, others)

    return {"b64": list(b64), "urls": list(urls), "others": list(others)}


def collect_images_from_unknown(value: Any) -> Dict[str, List[str]]:
    b64: Set[str] = set()
    urls: Set[str] = set()
    others: Set[str] = set()

    def walk(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            text = item.strip()
            extracted = extract_image_urls_from_text(item)
            b64.update(extracted["b64"])
            urls.update(extracted["urls"])
            others.update(extracted["others"])
            if text.lower().startswith(("data:image/", "base64://")):
                b64.add(text)
            elif len(text) > 100 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", text):
                b64.add(text)
            json_candidates: List[str] = []
            fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.I)
            json_candidates.append(fenced.group(1).strip() if fenced else text)
            json_candidates.extend(match.group(1).strip() for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.I))
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.lower().startswith("data:"):
                    payload = stripped.split(":", 1)[1].strip()
                    if payload and payload != "[DONE]":
                        json_candidates.append(payload)
            seen_json: Set[str] = set()
            for json_text in json_candidates:
                if not json_text.startswith(("{", "[")):
                    continue
                if json_text in seen_json:
                    continue
                seen_json.add(json_text)
                try:
                    walk(json.loads(json_text))
                except Exception:
                    pass
            return
        if isinstance(item, Sequence) and not isinstance(item, (bytes, bytearray, str)):
            for child in item:
                walk(child)
            return
        if not isinstance(item, dict):
            return

        b64_keys = (
            "b64_json",
            "base64",
            "data",
            "image_base64",
            "imageBase64",
            "base64_image",
            "base64Image",
            "image_data",
            "imageData",
            "encoded_image",
            "encodedImage",
        )
        for key in b64_keys:
            if key in item and isinstance(item[key], str):
                text = item[key].strip()
                if text:
                    if text.lower().startswith(("data:image/", "base64://")):
                        b64.add(text)
                    elif len(text) > 100:
                        b64.add(text)

        urlish_keys = (
            "url",
            "uri",
            "href",
            "src",
            "image",
            "imageUri",
            "image_uri",
            "imageUrl",
            "image_url",
            "output",
            "artifact",
            "artifacts",
            "asset",
            "assets",
            "resource",
            "resources",
            "resource_url",
            "resourceUrl",
            "file",
            "files",
            "download_url",
            "downloadUrl",
            "media_url",
            "mediaUrl",
            "public_url",
            "publicUrl",
        )
        text_keys = ("content", "text")
        for key in (*urlish_keys, *text_keys):
            field_value = item.get(key)
            if isinstance(field_value, str):
                if key in urlish_keys and (
                    field_value.lower().startswith(("http://", "https://", "data:image/"))
                    or looks_like_relative_image_url(field_value)
                ):
                    add_maybe_image_url(field_value, b64, urls, others)
                else:
                    walk(field_value)
            elif isinstance(field_value, dict):
                nested_url = field_value.get("url") or field_value.get("uri")
                if isinstance(nested_url, str):
                    add_maybe_image_url(nested_url, b64, urls, others)
                walk(field_value)
            else:
                walk(field_value)

        inline_data = item.get("inline_data") or item.get("inlineData")
        if isinstance(inline_data, dict) and isinstance(inline_data.get("data"), str):
            b64.add(inline_data["data"].strip())

        for child in item.values():
            walk(child)

    walk(value)
    return {"b64": list(b64), "urls": list(urls), "others": list(others)}


async def images_from_response_unknown(
    session: aiohttp.ClientSession,
    data: Any,
    timeout: int,
    max_bytes: int = 25 * 1024 * 1024,
    proxy: str = "",
    base_url: str = "",
) -> List[bytes]:
    collected = collect_images_from_unknown(data)
    images: List[bytes] = []
    seen = set()

    for item in collected["b64"]:
        try:
            image = b64_to_bytes(item)
        except Exception:
            continue
        if len(image) > max_bytes or not looks_like_binary_image(image):
            continue
        key = (len(image), image[:32])
        if image and key not in seen:
            images.append(image)
            seen.add(key)

    download_urls: List[str] = []
    seen_urls = set()
    for item in collected["urls"]:
        url = str(item or "").strip()
        if url and url not in seen_urls:
            download_urls.append(url)
            seen_urls.add(url)
    if base_url:
        for item in collected["others"]:
            url = resolve_response_url(str(item or ""), base_url)
            if url.lower().startswith(("http://", "https://")) and url not in seen_urls:
                download_urls.append(url)
                seen_urls.add(url)

    for item in download_urls:
        image = await fetch_generated_image_url(session, item, timeout, max_bytes=max_bytes, proxy=proxy)
        key = (len(image or b""), (image or b"")[:32])
        if image and key not in seen:
            images.append(image)
            seen.add(key)

    return images


class OpenAIImageAdapter(BaseImageAdapter):
    async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        if req.images:
            if not is_gpt_image_model(self.target.model):
                return ImageGenerateResult(error="OpenAI 图生图仅支持 gpt-image 系列模型，DALL-E 系列不支持参考图")
            return await self.generate_edit(req)
        return await self.generate_image(req)

    async def generate_image(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        gpt_image = is_gpt_image_model(self.target.model)
        base = normalize_image_base_url(self.target.base_url) or "https://api.openai.com"
        url = f"{base}/v1/images/generations"
        payload: Dict[str, Any] = {
            "model": self.target.model or ("gpt-image-1" if gpt_image else "dall-e-3"),
            "prompt": req.prompt,
            "n": 1,
        }
        if not gpt_image:
            payload["response_format"] = "b64_json"
        payload["size"] = map_aspect_ratio_to_gpt_image_size(req.aspect_ratio) if gpt_image else map_aspect_ratio_to_openai_size(req.aspect_ratio)
        async with await self.post_json(url, payload) as response:
            if response.status >= 400:
                return ImageGenerateResult(error=f"HTTP {response.status}: {http_error_preview(await response.text())}")
            data = await response.json(content_type=None)
        images = await images_from_response_unknown(self.session, data, self.target.timeout, req.max_image_bytes, self.target.proxy, base)
        return ImageGenerateResult(images=images) if images else ImageGenerateResult(error="未生成任何图片")

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
            text = await response.text()
            if response.status >= 400:
                return None, f"HTTP {response.status}: {http_error_preview(text)}"
            try:
                return json.loads(text), ""
            except json.JSONDecodeError:
                return None, f"接口返回非 JSON 内容: {response_preview(text, 300)}"

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
        images = await images_from_response_unknown(self.session, data, self.target.timeout, req.max_image_bytes, self.target.proxy, base)
        return ImageGenerateResult(images=images) if images else ImageGenerateResult(error="未生成任何图片")


class GeminiImageAdapter(BaseImageAdapter):
    async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        base = normalize_gemini_base_url(self.target.base_url) or "https://generativelanguage.googleapis.com"
        model_path = self.target.model if self.target.model.startswith("models/") else f"models/{self.target.model}"
        url = f"{base}/v1beta/{model_path}:generateContent"
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
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-goog-api-key": self.target.api_key,
        }
        try:
            async with self.session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.target.timeout),
                proxy=str(self.target.proxy or "").strip() or None,
            ) as response:
                if response.status >= 400:
                    return ImageGenerateResult(error=f"HTTP {response.status}: {http_error_preview(await response.text())}")
                data = await response.json(content_type=None)
        except asyncio.TimeoutError:
            return ImageGenerateResult(error=f"Gemini 生图请求超时（{self.target.timeout}秒）")
        images = await images_from_response_unknown(self.session, data, self.target.timeout, req.max_image_bytes, self.target.proxy, base)
        return ImageGenerateResult(images=images) if images else ImageGenerateResult(error="未生成任何图片")


class GeminiOpenAIImageAdapter(BaseImageAdapter):
    async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        base = normalize_image_base_url(self.target.base_url)
        url = f"{base}/v1/chat/completions"
        content: List[Dict[str, Any]] = [{"type": "text", "text": f"Generate an image: {req.prompt}"}]
        for image in req.images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": bytes_to_data_url(image.data, image.mime_type)},
                }
            )
        payload = {
            "model": self.target.model,
            "messages": [{"role": "user", "content": content}],
            "modalities": ["image", "text"],
            "stream": False,
        }
        async with await self.post_json(url, payload) as response:
            if response.status >= 400:
                return ImageGenerateResult(error=f"HTTP {response.status}: {http_error_preview(await response.text())}")
            data = await response.json(content_type=None)
        images = await images_from_response_unknown(self.session, data, self.target.timeout, req.max_image_bytes, self.target.proxy, base)
        if images:
            return ImageGenerateResult(images=images)
        preview = response_preview(data)
        collected = collect_images_from_unknown(data)
        if collected["urls"]:
            return ImageGenerateResult(error=f"接口返回了图片链接但下载失败。链接数: {len(collected['urls'])}；返回预览: {preview}")
        if collected["b64"]:
            return ImageGenerateResult(error=f"接口返回了 base64 图片但解码失败。数量: {len(collected['b64'])}；返回预览: {preview}")
        return ImageGenerateResult(error=f"未识别到可下载图片字段。返回预览: {preview}")


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
        async with await self.post_json(url, self.build_payload(req)) as response:
            if response.status >= 400:
                return ImageGenerateResult(error=f"HTTP {response.status}: {http_error_preview(await response.text(), 300)}")
            data = await response.json(content_type=None)
        images = await images_from_response_unknown(self.session, data, self.target.timeout, req.max_image_bytes, self.target.proxy, base)
        return ImageGenerateResult(images=images) if images else ImageGenerateResult(error="未生成任何图片")


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

    async def generate(self, req: ImageGenerateRequest) -> ImageGenerateResult:
        base = normalize_image_base_url(self.target.base_url) or self.default_base_url
        url = f"{base}/v1/images/generations"
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
        async with await self.post_json(url, payload) as response:
            text = await response.text()
            if response.status >= 400:
                return ImageGenerateResult(error=f"HTTP {response.status}: {http_error_preview(text, 300)}")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return ImageGenerateResult(error=f"接口返回非 JSON 内容: {response_preview(text, 300)}")
        images = await images_from_response_unknown(self.session, data, self.target.timeout, req.max_image_bytes, self.target.proxy, base)
        if images:
            return ImageGenerateResult(images=images)
        preview = response_preview(data)
        collected = collect_images_from_unknown(data)
        if collected["urls"]:
            return ImageGenerateResult(error=f"Agnes 返回了图片链接但下载失败。链接数: {len(collected['urls'])}；返回预览: {preview}")
        return ImageGenerateResult(error=f"未生成任何图片。返回预览: {preview}")


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


def resolve_response_url(img_url: str, base_url: str) -> str:
    if img_url.lower().startswith(("http://", "https://", "data:")):
        return img_url
    api_root = normalize_image_base_url(base_url)
    if api_root.endswith("/v1"):
        api_root = api_root[:-3]
    return urljoin(api_root.rstrip("/") + "/", img_url.lstrip("/"))
