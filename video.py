"""OpenAI-compatible video generation (t2v / i2v).

Source patterns: OmniDraw VideoManager + big_banana I2V first-frame constraint.
Endpoint: POST {base}/videos/generations — sync URL or async task_id + poll.
Never blindly re-POST after a billable create timeout (same discipline as GPT Image).
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

import aiohttp

from .error_classify import classify_generation_error
from .models import ImageModelTarget
from .provider_parser import normalize_image_base_url
from .providers import ImageReference
from .utils import bytes_to_data_url, redact_sensitive_text


@dataclass
class VideoGenerateRequest:
    prompt: str
    images: List[ImageReference] = field(default_factory=list)
    duration: int = 5
    size: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VideoGenerateResult:
    video_path: str = ""
    video_url: str = ""
    error: str = ""
    used_model: str = ""
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    elapsed_seconds: float = 0.0


def build_video_generations_endpoint(base_url: str) -> str:
    base = normalize_image_base_url(base_url) or str(base_url or "").rstrip("/")
    if not base:
        return ""
    lowered = base.lower()
    if lowered.endswith("/videos/generations"):
        return base
    if base.endswith("/v1"):
        return f"{base}/videos/generations"
    if "/v1/" in lowered:
        # already has some v1 path stripped by normalize; still append
        return f"{base.rstrip('/')}/videos/generations"
    return f"{base.rstrip('/')}/v1/videos/generations"


def _extract_url(text: str) -> str:
    match = re.search(r"(https?://[^\s\]\)\"']+)", text or "")
    return match.group(1) if match else str(text or "").strip()


def _extract_task_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("task_id", "id"):
            value = payload.get(key)
            if value and key == "task_id":
                return str(value)
            if value and key == "id":
                status = str(payload.get("status", payload.get("task_status", ""))).lower()
                # Prefer explicit task_id; accept id only for async-looking payloads.
                if status in {"submitted", "pending", "queued", "processing", "running", "in_progress"} or payload.get("task_id"):
                    return str(value)
                # Some gateways return only {"id": "..."} for video jobs.
                if "video" in str(payload).lower() or payload.get("object") in {"video", "video.generation"}:
                    return str(value)
        for value in payload.values():
            found = _extract_task_id(value)
            if found:
                return found
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            found = _extract_task_id(item)
            if found:
                return found
    return ""


def _extract_task_status(payload: Any) -> str:
    if isinstance(payload, dict):
        status = payload.get("status", payload.get("task_status", payload.get("state", "")))
        if status:
            return str(status).upper()
        for value in payload.values():
            found = _extract_task_status(value)
            if found:
                return found
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            found = _extract_task_status(item)
            if found:
                return found
    return ""


def _extract_video_url(data: Any) -> str:
    if not isinstance(data, dict):
        if isinstance(data, str) and data.startswith("http"):
            return _extract_url(data)
        return ""
    for key in ("video_url", "url", "output", "video"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            url = _extract_url(value)
            if url.startswith("http") or url.startswith("data:"):
                return url
        if isinstance(value, dict):
            nested = _extract_video_url(value)
            if nested:
                return nested
    data_field = data.get("data")
    if isinstance(data_field, list) and data_field:
        item = data_field[0]
        if isinstance(item, dict):
            return _extract_video_url(item)
        if isinstance(item, str):
            return _extract_url(item)
    if isinstance(data_field, dict):
        return _extract_video_url(data_field)
    # chat-style
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                return _extract_url(content)
    return ""


def _ref_to_data_url(ref: ImageReference) -> str:
    mime = str(ref.mime_type or "image/png").strip() or "image/png"
    return bytes_to_data_url(ref.data, mime)


async def _read_error(response: aiohttp.ClientResponse) -> str:
    try:
        text = await response.text()
    except Exception:
        text = ""
    return redact_sensitive_text(f"HTTP {response.status}: {(text or '')[:800]}")


async def _download_video_bytes(session: aiohttp.ClientSession, url: str, timeout: int) -> bytes:
    if url.startswith("data:"):
        # data:video/mp4;base64,...
        try:
            header, b64 = url.split(",", 1)
            return base64.b64decode(b64)
        except Exception as exc:
            raise RuntimeError(f"无法解码 data URL 视频: {exc}") from exc
    headers = {"User-Agent": "SelfieImage-Video/1.0"}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=max(30, timeout))) as response:
        if response.status >= 400:
            raise RuntimeError(await _read_error(response))
        data = await response.read()
        if not data:
            raise RuntimeError("视频下载结果为空")
        return data


def _task_poll_url(endpoint: str, task_id: str, submission: Dict[str, Any]) -> str:
    # OmniDraw: list-shaped data often means /api/tasks/{id}
    if isinstance(submission.get("data"), list):
        parsed = urlparse(endpoint)
        root = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else endpoint.rsplit("/v1", 1)[0]
        return f"{root.rstrip('/')}/api/tasks/{quote(task_id, safe='')}"
    return f"{endpoint.rstrip('/')}/{quote(task_id, safe='')}"


async def _poll_task(
    session: aiohttp.ClientSession,
    *,
    poll_url: str,
    headers: Dict[str, str],
    timeout_seconds: int,
) -> str:
    max_retries = max(3, int(timeout_seconds) // 10)
    for attempt in range(max_retries):
        await asyncio.sleep(10)
        try:
            async with session.get(poll_url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status >= 400:
                    continue
                data = await response.json(content_type=None)
            status = _extract_task_status(data)
            if status in {"SUCCESS", "SUCCEEDED", "COMPLETED", "COMPLETE", "DONE"}:
                url = _extract_video_url(data)
                if url:
                    return url
                raise RuntimeError(f"任务成功但未找到视频地址: {str(data)[:300]}")
            if status in {"FAIL", "FAILED", "FAILURE", "ERROR", "CANCELLED", "CANCELED"}:
                err = data.get("error") if isinstance(data, dict) else ""
                if isinstance(err, dict):
                    err = err.get("message") or str(err)
                raise RuntimeError(str(err or data.get("message") or "视频任务失败"))
        except RuntimeError:
            raise
        except Exception:
            continue
    raise RuntimeError(f"视频生成轮询超时（已等约 {timeout_seconds}s）")


async def generate_video_openai_compatible(
    target: ImageModelTarget,
    request: VideoGenerateRequest,
    session: aiohttp.ClientSession,
    *,
    save_dir: str,
) -> VideoGenerateResult:
    started = time.monotonic()
    attempt_info: Dict[str, Any] = {
        "channel": target.channel_name,
        "model": target.model,
        "provider": target.provider_type,
    }
    if not target.base_url or not target.model:
        return VideoGenerateResult(error="视频渠道缺少 base_url 或 model", attempts=[attempt_info])
    keys = target.resolved_api_keys() if hasattr(target, "resolved_api_keys") else ([target.api_key] if target.api_key else [])
    if not keys:
        return VideoGenerateResult(error="视频渠道缺少 api_key", attempts=[attempt_info])

    endpoint = build_video_generations_endpoint(target.base_url)
    timeout = max(60, int(target.timeout or 300))
    last_error = ""

    for key_index, api_key in enumerate(keys):
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "SelfieImage-Video/1.0",
            "Connection": "close",
        }
        b64_images = [_ref_to_data_url(ref) for ref in (request.images or [])[:3] if ref and ref.data]
        payload: Dict[str, Any] = {
            "model": target.model,
            "prompt": str(request.prompt or "").strip(),
        }
        duration = int(request.duration or 5)
        if duration > 0:
            payload["duration"] = duration
            payload["seconds"] = duration
        if request.size:
            payload["size"] = str(request.size).strip()
        if b64_images:
            # Common gateway field names for I2V first frame / refs.
            payload["images"] = b64_images
            payload["image"] = b64_images[0]
            payload["image_url"] = b64_images[0]
            payload["input_reference"] = b64_images[0]
        if isinstance(request.extra, dict) and request.extra:
            payload.update(request.extra)

        key_attempt = dict(attempt_info)
        if len(keys) > 1:
            key_attempt["key_index"] = key_index + 1

        try:
            # Prefer short create timeout then poll; if gateway is sync, allow full timeout once.
            create_timeout = min(45, timeout)
            async with session.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=create_timeout),
                proxy=(target.proxy or None),
            ) as response:
                body_text = await response.text()
                if response.status >= 400:
                    err = redact_sensitive_text(f"HTTP {response.status}: {body_text[:800]}")
                    class_info = classify_generation_error(err)
                    key_attempt["error"] = err
                    key_attempt["error_category"] = class_info.get("category")
                    last_error = err
                    if class_info.get("category") in {"auth", "rate_limit"} and key_index + 1 < len(keys):
                        continue
                    if not class_info.get("retryable", True):
                        return VideoGenerateResult(error=err, attempts=[key_attempt], used_model=target.label)
                    # Fall through: try long sync POST once for this key only if create looked transient.
                    async with session.post(
                        endpoint,
                        headers=headers,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                        proxy=(target.proxy or None),
                    ) as response2:
                        body_text = await response2.text()
                        if response2.status >= 400:
                            err2 = redact_sensitive_text(f"HTTP {response2.status}: {body_text[:800]}")
                            key_attempt["error"] = err2
                            return VideoGenerateResult(error=err2, attempts=[key_attempt], used_model=target.label)
                        try:
                            data = await response2.json(content_type=None)
                        except Exception:
                            data = {"raw": body_text}
                else:
                    try:
                        data = await response.json(content_type=None)
                    except Exception:
                        # Maybe plain URL
                        data = {"url": body_text.strip()} if str(body_text).strip().startswith("http") else {"raw": body_text}

            if not isinstance(data, dict):
                data = {"data": data}

            video_url = _extract_video_url(data)
            task_id = ""
            if not video_url:
                task_id = _extract_task_id(data)
                # Some APIs return id even on success without status — only poll if no URL.
                if task_id:
                    poll_url = _task_poll_url(endpoint, task_id, data)
                    video_url = await _poll_task(session, poll_url=poll_url, headers=headers, timeout_seconds=timeout)

            if not video_url:
                # Final long sync attempt only if neither URL nor task — do not re-POST if task already created.
                raise RuntimeError(f"未返回视频地址或任务号: {str(data)[:400]}")

            raw = await _download_video_bytes(session, video_url, timeout=timeout)
            os.makedirs(save_dir, exist_ok=True)
            path = os.path.join(save_dir, f"video_{int(time.time() * 1000)}.mp4")
            with open(path, "wb") as handle:
                handle.write(raw)
            key_attempt["success"] = True
            return VideoGenerateResult(
                video_path=path,
                video_url=video_url if video_url.startswith("http") else "",
                used_model=target.label,
                attempts=[key_attempt],
                elapsed_seconds=round(time.monotonic() - started, 2),
            )
        except asyncio.TimeoutError:
            # Do not automatically re-POST create (double bill risk).
            last_error = "视频请求超时（未自动重提，以免重复扣费）"
            key_attempt["error"] = last_error
            key_attempt["error_category"] = "timeout"
            return VideoGenerateResult(error=last_error, attempts=[key_attempt], used_model=target.label)
        except Exception as exc:
            last_error = redact_sensitive_text(str(exc))
            class_info = classify_generation_error(last_error)
            key_attempt["error"] = last_error
            key_attempt["error_category"] = class_info.get("category")
            if class_info.get("category") in {"auth", "rate_limit"} and key_index + 1 < len(keys):
                continue
            if not class_info.get("retryable", True):
                return VideoGenerateResult(error=last_error, attempts=[key_attempt], used_model=target.label)
            # try next key if any
            if key_index + 1 < len(keys):
                continue
            return VideoGenerateResult(error=last_error, attempts=[key_attempt], used_model=target.label)

    return VideoGenerateResult(error=last_error or "视频生成失败", used_model=target.label)


async def generate_video_with_fallback(
    targets: List[ImageModelTarget],
    request: VideoGenerateRequest,
    session: aiohttp.ClientSession,
    *,
    save_dir: str,
) -> VideoGenerateResult:
    if not targets:
        return VideoGenerateResult(error="当前没有可用的视频模型，请先在配置里启用视频渠道")
    attempts: List[Dict[str, Any]] = []
    last_error = ""
    for target in targets:
        result = await generate_video_openai_compatible(target, request, session, save_dir=save_dir)
        attempts.extend(result.attempts or [])
        if result.video_path and not result.error:
            result.attempts = attempts
            return result
        last_error = result.error or last_error
        # stop on non-retryable
        if result.attempts:
            cat = str((result.attempts[-1] or {}).get("error_category") or "")
            if cat in {"auth", "unsafe", "not_found"} and len(targets) == 1:
                break
    return VideoGenerateResult(error=last_error or "视频生成失败", attempts=attempts)
