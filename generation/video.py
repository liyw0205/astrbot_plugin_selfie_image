"""OpenAI-compatible video generation (t2v / i2v).

Source patterns: OmniDraw VideoManager + big_banana I2V first-frame constraint.
Endpoint: POST {base}/videos/generations — sync URL or async task_id + poll.
Agnes Video V2.0 uses official POST /v1/videos + GET /agnesapi?video_id= (urllib;
aiohttp often fails TLS/connect to apihub from some hosts).
Never blindly re-POST after a billable create timeout (same discipline as GPT Image).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote, urlparse

import aiohttp
import requests

from ..core.error_classify import classify_generation_error
from ..core.models import ImageModelTarget
from ..core.proxy import channel_client_session, target_session_proxy
from ..core.provider_parser import normalize_image_base_url
from ..core.providers import ImageReference
from ..core.utils import bytes_to_data_url, redact_sensitive_text


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
    video_source: str = ""
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


def build_agnes_videos_endpoint(base_url: str) -> str:
    """Official Agnes Video V2: POST {base}/v1/videos (not /videos/generations)."""
    base = normalize_image_base_url(base_url) or str(base_url or "").rstrip("/")
    if not base:
        return ""
    lowered = base.lower().rstrip("/")
    if lowered.endswith("/v1/videos"):
        return base.rstrip("/")
    if lowered.endswith("/videos") and "/v1/" in lowered:
        return base.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/videos"
    return f"{base.rstrip('/')}/v1/videos"


def build_agnes_result_url(base_url: str, video_id: str, model: str = "") -> str:
    """Official poll: GET {origin}/agnesapi?video_id=...[&model_name=...]."""
    raw = str(base_url or "").strip() or "https://apihub.agnes-ai.com"
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}" if parsed.netloc else "https://apihub.agnes-ai.com"
    vid = quote(str(video_id or "").strip(), safe="")
    url = f"{origin.rstrip('/')}/agnesapi?video_id={vid}"
    model_name = str(model or "").strip()
    if model_name:
        url += f"&model_name={quote(model_name, safe='')}"
    return url


def agnes_num_frames_for_duration(duration_seconds: int, frame_rate: int = 24) -> int:
    """Map seconds → num_frames with official 8n+1 and ≤441 constraints."""
    try:
        seconds = max(1, int(duration_seconds or 5))
    except Exception:
        seconds = 5
    try:
        fps = max(1, min(60, int(frame_rate or 24)))
    except Exception:
        fps = 24
    # nearest 8n+1 around target duration
    target = max(1, int(round(seconds * fps)))
    n = max(0, int(round((target - 1) / 8.0)))
    frames = 8 * n + 1
    if frames > 441:
        frames = 441  # 8*55+1
    if frames < 9:
        frames = 9
    return frames


def agnes_size_wh(size: str) -> tuple[int, int]:
    """Map size/aspect hint to official default-ish width/height."""
    text = str(size or "").strip().lower().replace("：", ":")
    presets = {
        "16:9": (1152, 768),
        "9:16": (768, 1152),
        "1:1": (1024, 1024),
        "4:3": (1152, 864),
        "3:4": (864, 1152),
        "1280x720": (1280, 720),
        "720x1280": (720, 1280),
        "1024x1024": (1024, 1024),
        "1152x768": (1152, 768),
        "768x1152": (768, 1152),
    }
    if text in presets:
        return presets[text]
    m = re.match(r"^(\d{3,4})\s*[x×]\s*(\d{3,4})$", text)
    if m:
        return max(64, int(m.group(1))), max(64, int(m.group(2)))
    if "9:16" in text or "竖" in text:
        return 768, 1152
    if "1:1" in text or "方" in text:
        return 1024, 1024
    return 1152, 768


def _extract_url(text: str) -> str:
    match = re.search(r"(https?://[^\s\]\)\"']+)", text or "")
    return match.group(1) if match else str(text or "").strip()


def _extract_task_id(payload: Any) -> str:
    if isinstance(payload, dict):
        # Agnes official create response prefers video_id for result polling.
        # futureppo/Grok midgates often return only {"request_id": "task_..."}.
        for key in ("video_id", "task_id", "request_id", "id"):
            value = payload.get(key)
            if not value:
                continue
            if key in {"video_id", "task_id", "request_id"}:
                return str(value)
            status = str(payload.get("status", payload.get("task_status", ""))).lower()
            # Prefer explicit task_id; accept id only for async-looking payloads.
            if status in {"submitted", "pending", "queued", "processing", "running", "in_progress"} or payload.get("task_id") or payload.get("video_id") or payload.get("request_id"):
                return str(value)
            # Some gateways return only {"id": "..."} for video jobs.
            if "video" in str(payload).lower() or payload.get("object") in {"video", "video.generation"}:
                return str(value)
            # bare request-looking id with task_ prefix
            text = str(value)
            if text.startswith("task_") or text.startswith("video_"):
                return text
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


def _extract_video_id(payload: Any) -> str:
    if isinstance(payload, dict):
        value = payload.get("video_id")
        if value:
            return str(value)
        # Prefer a nested official video_id over a top-level legacy task id.
        for value in payload.values():
            found = _extract_video_id(value)
            if found:
                return found
        for key in ("task_id", "id"):
            value = payload.get(key)
            if value:
                return str(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            found = _extract_video_id(item)
            if found:
                return found
    return ""


def _extract_agnes_task_id(payload: Any) -> str:
    """Extract the legacy task identifier used by /v1/videos/<TASK_ID>."""
    if isinstance(payload, dict):
        for key in ("task_id", "request_id", "id"):
            value = payload.get(key)
            if value:
                return str(value)
        for value in payload.values():
            found = _extract_agnes_task_id(value)
            if found:
                return found
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            found = _extract_agnes_task_id(item)
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
    # Agnes completed payload: metadata.url
    meta = data.get("metadata")
    if isinstance(meta, dict):
        meta_url = meta.get("url") or meta.get("video_url")
        if isinstance(meta_url, str) and meta_url.strip():
            url = _extract_url(meta_url)
            if url.startswith("http") or url.startswith("data:"):
                return url
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


def _agnes_video_family(model: str) -> str:
    """Return the Agnes video request family selected by the model name."""
    normalized = str(model or "").strip().lower().replace("_", "-")
    if "agnes-video-2.5-flash" in normalized or "agnes-video-25-flash" in normalized:
        return "v25_flash"
    if "agnes-video-2.5" in normalized or "agnes-video-25" in normalized:
        return "v25"
    return "v20"


def _agnes_media_values(
    references: Optional[Sequence[ImageReference]],
    b64_images: Sequence[str],
) -> List[str]:
    """Use public reference URLs when available and data URLs as a local fallback."""
    values: List[str] = []
    refs = list(references or [])
    b64_index = 0
    for ref in refs:
        source_url = str(getattr(ref, "source_url", "") or "").strip()
        if source_url.lower().startswith(("http://", "https://")):
            values.append(source_url)
            continue
        if getattr(ref, "data", None) and b64_index < len(b64_images):
            if b64_images[b64_index]:
                values.append(str(b64_images[b64_index]))
            b64_index += 1
    if not values:
        values.extend(str(item) for item in b64_images if str(item).strip())
    return values


def _agnes_string_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    result: List[str] = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("url") or item.get("source_url") or item.get("uri") or ""
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


async def _read_error(response: aiohttp.ClientResponse) -> str:
    try:
        text = await response.text()
    except Exception:
        text = ""
    return redact_sensitive_text(f"HTTP {response.status}: {(text or '')[:800]}")


async def _download_video_bytes(session: aiohttp.ClientSession, url: str, timeout: int, proxy: str = "") -> bytes:
    if url.startswith("data:"):
        # data:video/mp4;base64,...
        try:
            header, b64 = url.split(",", 1)
            return base64.b64decode(b64)
        except Exception as exc:
            raise RuntimeError(f"无法解码 data URL 视频: {exc}") from exc

    async def _via_aiohttp() -> bytes:
        headers = {"User-Agent": "SelfieImage-Video/1.0"}
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=max(30, timeout)),
            proxy=proxy or None,
        ) as response:
            if response.status >= 400:
                raise RuntimeError(await _read_error(response))
            data = await response.read()
            if not data:
                raise RuntimeError("视频下载结果为空")
            return data

    def _via_urllib() -> bytes:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "SelfieImage-Video/1.0"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy})) if proxy else urllib.request.build_opener()
        with opener.open(req, timeout=max(30, int(timeout or 60))) as resp:
            data = resp.read()
        if not data:
            raise RuntimeError("视频下载结果为空")
        return data

    try:
        return await _via_aiohttp()
    except Exception as aio_exc:
        # Agnes CDN and some hosts reject/hang aiohttp; urllib often works (same as create/poll).
        try:
            return await asyncio.to_thread(_via_urllib)
        except Exception as ur_exc:
            raise RuntimeError(
                redact_sensitive_text(
                    f"视频下载失败: aiohttp={aio_exc}; urllib={ur_exc}"
                )
            ) from ur_exc


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
    proxy: str = "",
) -> str:
    max_retries = max(3, int(timeout_seconds) // 10)
    for attempt in range(max_retries):
        await asyncio.sleep(10)
        try:
            async with session.get(
                poll_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
                proxy=proxy or None,
            ) as response:
                if response.status >= 400:
                    continue
                data = await response.json(content_type=None)
            status = _extract_task_status(data)
            # Agnes uses completed/failed/in_progress/queued/pending (lowercased upstream → upper here)
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
            # Some gateways put URL early without final status
            early = _extract_video_url(data)
            if early and status in {"", "SUCCESS", "SUCCEEDED", "COMPLETED"}:
                return early
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
    """Dispatch by video family protocol (sora/veo/seedance/agnes/…) + transport modes."""
    from ..core.models import normalize_video_provider_type, resolve_video_model_provider_type

    started = time.monotonic()
    protocol = resolve_video_model_provider_type(
        target.model,
        target.provider_type,
        "",
    ) or normalize_video_provider_type(target.provider_type) or "openai_video"
    attempt_info: Dict[str, Any] = {
        "channel": target.channel_name,
        "model": target.model,
        "provider": protocol,
    }
    if not target.base_url or not target.model:
        return VideoGenerateResult(error="视频渠道缺少 base_url 或 model", attempts=[attempt_info])
    keys = target.resolved_api_keys() if hasattr(target, "resolved_api_keys") else ([target.api_key] if target.api_key else [])
    if not keys:
        return VideoGenerateResult(error="视频渠道缺少 api_key", attempts=[attempt_info])

    timeout = max(60, int(target.timeout or 300))
    last_error = ""
    b64_images = [_ref_to_data_url(ref) for ref in (request.images or [])[:3] if ref and ref.data]

    for key_index, api_key in enumerate(keys):
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "SelfieImage-Video/1.0",
            "Connection": "close",
        }
        key_attempt = dict(attempt_info)
        if len(keys) > 1:
            key_attempt["key_index"] = key_index + 1
        video_source = ""
        try:
            if protocol == "video_chat":
                video_url = await _generate_via_chat(
                    session,
                    target=target,
                    request=request,
                    headers=headers,
                    timeout=timeout,
                    b64_images=b64_images,
                )
            elif protocol == "video_sync":
                video_url = await _generate_via_sync(
                    session,
                    target=target,
                    request=request,
                    headers=headers,
                    timeout=timeout,
                    b64_images=b64_images,
                    family=protocol,
                )
            elif protocol == "agnes":
                # Official Agnes Video V2.0: POST /v1/videos + GET /agnesapi?video_id=
                video_url = await _generate_via_agnes(
                    session,
                    target=target,
                    request=request,
                    headers=headers,
                    timeout=timeout,
                    b64_images=b64_images,
                )
            else:
                # Family protocols (sora/veo/seedance/kling/cogvideo/openai_video/grok)
                # share OpenAI-compatible /videos/generations on most midgates; payload tuned per family.
                async_family = protocol
                model_l = str(target.model or "").lower()
                if protocol in {"grok", "grok_video", "xai", "grok_midgate"} or (
                    "grok" in model_l and "video" in model_l
                ):
                    async_family = "grok_midgate"
                video_url = await _generate_via_async(
                    session,
                    target=target,
                    request=request,
                    headers=headers,
                    timeout=timeout,
                    b64_images=b64_images,
                    family=async_family,
                )

            video_source = str(video_url or "")
            raw = await _download_video_bytes(session, video_url, timeout=timeout, proxy=str((target.extra or {}).get("download_proxy") or target.proxy or ""))
            os.makedirs(save_dir, exist_ok=True)
            path = os.path.join(save_dir, f"video_{int(time.time() * 1000)}.mp4")
            with open(path, "wb") as handle:
                handle.write(raw)
            key_attempt["success"] = True
            return VideoGenerateResult(
                video_path=path,
                video_url=video_url if str(video_url).startswith("http") else "",
                video_source=video_source,
                used_model=target.label,
                attempts=[key_attempt],
                elapsed_seconds=round(time.monotonic() - started, 2),
            )
        except asyncio.TimeoutError:
            # Only treat as "create billable timeout" for short create calls; long Agnes polls raise RuntimeError.
            last_error = "视频请求超时（未自动重提，以免重复扣费）"
            key_attempt["error"] = last_error
            key_attempt["error_category"] = "timeout"
            return VideoGenerateResult(
                error=last_error,
                video_source=video_source,
                attempts=[key_attempt],
                used_model=target.label,
                elapsed_seconds=round(time.monotonic() - started, 2),
            )
        except Exception as exc:
            last_error = redact_sensitive_text(str(exc))
            class_info = classify_generation_error(last_error)
            key_attempt["error"] = last_error
            key_attempt["error_category"] = class_info.get("category")
            # Poll/create failures already spent wait time — still report elapsed
            if class_info.get("category") in {"auth", "rate_limit"} and key_index + 1 < len(keys):
                continue
            if not class_info.get("retryable", True):
                return VideoGenerateResult(
                    error=last_error,
                    video_source=video_source,
                    attempts=[key_attempt],
                    used_model=target.label,
                    elapsed_seconds=round(time.monotonic() - started, 2),
                )
            if key_index + 1 < len(keys):
                continue
            return VideoGenerateResult(
                error=last_error,
                video_source=video_source,
                attempts=[key_attempt],
                used_model=target.label,
                elapsed_seconds=round(time.monotonic() - started, 2),
            )

    return VideoGenerateResult(error=last_error or "视频生成失败", video_source=video_source, used_model=target.label)


def _normalize_aspect_ratio(size: str) -> str:
    """Map size-like values to aspect_ratio when possible."""
    raw = str(size or "").strip()
    if not raw:
        return ""
    if ":" in raw and "x" not in raw.lower():
        return raw
    lower = raw.lower().replace("×", "x")
    if "x" in lower:
        try:
            w_s, h_s = lower.split("x", 1)
            w, h = int(float(w_s)), int(float(h_s))
            if w > 0 and h > 0:
                # reduce roughly to common ratios
                ratio = w / h
                presets = {
                    (16, 9): 16 / 9,
                    (9, 16): 9 / 16,
                    (1, 1): 1.0,
                    (4, 3): 4 / 3,
                    (3, 4): 3 / 4,
                    (3, 2): 1.5,
                    (2, 3): 2 / 3,
                }
                best = min(presets.items(), key=lambda item: abs(item[1] - ratio))
                if abs(best[1] - ratio) < 0.08:
                    return f"{best[0][0]}:{best[0][1]}"
        except Exception:
            pass
    return raw


def _video_payload(
    target: ImageModelTarget,
    request: VideoGenerateRequest,
    b64_images: List[str],
    *,
    family: str = "openai_video",
) -> Dict[str, Any]:
    """Build request body with per-family field whitelist (avoid unknown-field 400)."""
    family = str(family or "openai_video").strip().lower() or "openai_video"
    # Aliases for Grok midgates / chat transport already routed elsewhere.
    if family in {"grok", "grok_video", "xai"}:
        family = "grok_midgate"

    prompt = str(request.prompt or "").strip()
    payload: Dict[str, Any] = {
        "model": target.model,
        "prompt": prompt,
    }
    duration = int(request.duration or 5)
    size_raw = str(request.size or "").strip()
    aspect = _normalize_aspect_ratio(size_raw)

    # --- duration / size profiles (minimal by default) ---
    if family in {"grok_midgate"}:
        # futureppo/NewAPI style: unknown fields (seconds/size/n/...) often 400
        if aspect:
            payload["aspect_ratio"] = aspect
        # no duration fields unless gateway documents them
    elif family in {"sora", "openai_video", "video_async"}:
        if duration > 0:
            payload["duration"] = duration
            # seconds as string for gateways that reject number-as-string mismatch either way—
            # prefer int duration only; add seconds only if explicitly needed by extra.
        if size_raw:
            payload["size"] = size_raw
        if aspect and ":" in aspect:
            payload.setdefault("aspect_ratio", aspect)
    elif family in {"veo", "seedance", "kling", "cogvideo"}:
        if duration > 0:
            payload["duration"] = duration
        if aspect:
            payload["aspect_ratio"] = aspect
        elif size_raw:
            payload["size"] = size_raw
    elif family == "agnes":
        # official path uses dedicated builder; generations midgate fallback:
        if duration > 0:
            payload["duration"] = duration
        if size_raw:
            payload["size"] = size_raw
    else:
        # conservative generic midgate
        if duration > 0:
            payload["duration"] = duration
        if aspect:
            payload["aspect_ratio"] = aspect
        elif size_raw:
            payload["size"] = size_raw

    # --- I2V first frame ---
    if b64_images:
        first = b64_images[0]
        if family == "sora":
            payload["input_reference"] = first
            payload["images"] = b64_images[:1]
        elif family == "veo":
            payload["image"] = (
                {"bytesBase64Encoded": first.split(",", 1)[-1]}
                if first.startswith("data:")
                else {"uri": first}
            )
        elif family == "seedance":
            payload["first_frame_image"] = first
            payload["image_url"] = first
        elif family == "kling":
            payload["image_url"] = first
            payload["image"] = first
        elif family == "cogvideo":
            payload["image_url"] = first
        elif family == "agnes":
            payload["image"] = first
            payload["image_urls"] = [first]
        elif family == "grok_midgate":
            # keep single common key only
            payload["image"] = first
        else:
            payload["image"] = first
            payload["images"] = b64_images[:1]

    # Optional family hint only for non-strict gateways
    if family not in {"openai_video", "video_async", "grok_midgate", "agnes"}:
        payload.setdefault("provider", family)

    # request.extra may add documented fields; still drop known-toxic keys for strict families
    if isinstance(request.extra, dict) and request.extra:
        extra = dict(request.extra)
        if family == "grok_midgate":
            for bad in ("seconds", "n_seconds", "n", "size", "video_config", "video_length", "length"):
                extra.pop(bad, None)
        payload.update(extra)
    return payload


async def _generate_via_async(
    session: aiohttp.ClientSession,
    *,
    target: ImageModelTarget,
    request: VideoGenerateRequest,
    headers: Dict[str, str],
    timeout: int,
    b64_images: List[str],
    family: str = "openai_video",
) -> str:
    """POST /videos/generations → task_id poll or immediate URL."""
    endpoint = build_video_generations_endpoint(target.base_url)
    payload = _video_payload(target, request, b64_images, family=family)
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
            raise RuntimeError(redact_sensitive_text(f"HTTP {response.status}: {body_text[:800]}"))
        try:
            data = await response.json(content_type=None)
        except Exception:
            data = {"url": body_text.strip()} if str(body_text).strip().startswith("http") else {"raw": body_text}
    if not isinstance(data, dict):
        data = {"data": data}
    video_url = _extract_video_url(data)
    if video_url:
        return video_url
    task_id = _extract_task_id(data)
    if not task_id:
        raise RuntimeError(f"未返回视频地址或任务号: {str(data)[:400]}")
    poll_url = _task_poll_url(endpoint, task_id, data)
    return await _poll_task(session, poll_url=poll_url, headers=headers, timeout_seconds=timeout, proxy=target.proxy)


def _agnes_v20_payload(
    target: ImageModelTarget,
    request: VideoGenerateRequest,
    b64_images: List[str],
    references: Optional[Sequence[ImageReference]] = None,
) -> Dict[str, Any]:
    """Build official Agnes Video V2.0 create-task body."""
    extra = dict(request.extra or {}) if isinstance(request.extra, dict) else {}
    try:
        frame_rate = int(extra.get("frame_rate") or extra.get("fps") or 24)
    except Exception:
        frame_rate = 24
    frame_rate = max(1, min(60, frame_rate))
    if "num_frames" in extra:
        try:
            num_frames = int(extra.get("num_frames") or 0)
        except Exception:
            num_frames = 0
    else:
        num_frames = 0
    if num_frames <= 0:
        num_frames = agnes_num_frames_for_duration(int(request.duration or 5), frame_rate)
    # enforce 8n+1 and cap
    if (num_frames - 1) % 8 != 0:
        num_frames = agnes_num_frames_for_duration(max(1, int(round(num_frames / float(frame_rate)))), frame_rate)
    num_frames = max(9, min(441, num_frames))
    width, height = agnes_size_wh(str(request.size or extra.get("size") or extra.get("aspect_ratio") or ""))
    if extra.get("width") and extra.get("height"):
        try:
            width, height = int(extra["width"]), int(extra["height"])
        except Exception:
            pass
    payload: Dict[str, Any] = {
        "model": target.model or "agnes-video-v2.0",
        "prompt": str(request.prompt or "").strip(),
        "width": width,
        "height": height,
        "num_frames": num_frames,
        "frame_rate": frame_rate,
    }
    if extra.get("seed") is not None:
        payload["seed"] = extra.get("seed")
    if extra.get("negative_prompt"):
        payload["negative_prompt"] = str(extra.get("negative_prompt"))
    if extra.get("num_inference_steps") is not None:
        payload["num_inference_steps"] = extra.get("num_inference_steps")
    if extra.get("mode"):
        payload["mode"] = str(extra.get("mode"))

    # image / keyframes
    image_url = str(extra.get("image") or extra.get("image_url") or "").strip()
    media_values = _agnes_media_values(references, b64_images)
    if not image_url and media_values:
        image_url = media_values[0]
    if image_url:
        payload["image"] = image_url
    keyframes = extra.get("keyframes") or extra.get("images")
    if isinstance(keyframes, list) and keyframes:
        payload["extra_body"] = {"image": [str(x) for x in keyframes if str(x).strip()], "mode": "keyframes"}
        payload["mode"] = payload.get("mode") or "keyframes"
    elif isinstance(extra.get("extra_body"), dict):
        payload["extra_body"] = extra.get("extra_body")
    return payload


def _agnes_v25_payload(
    target: ImageModelTarget,
    request: VideoGenerateRequest,
    b64_images: List[str],
    references: Optional[Sequence[ImageReference]] = None,
    *,
    flash: bool = False,
) -> Dict[str, Any]:
    """Build Agnes Video 2.5/2.5 Flash's mode-based request body."""
    extra = dict(request.extra or {}) if isinstance(request.extra, dict) else {}
    media_values = _agnes_media_values(references, b64_images)

    try:
        seconds = max(4, min(12, int(extra.get("seconds") or request.duration or 5)))
    except Exception:
        seconds = 5

    raw_size = str(extra.get("size") or request.size or "").strip().upper()
    if raw_size not in {"720P", "1080P", "1K", "2K"}:
        raw_size = "720P"
    if flash:
        raw_size = "720P"

    aspect = str(extra.get("aspect_ratio") or "").strip()
    if not aspect:
        aspect = _normalize_aspect_ratio(str(request.size or "").strip())
    if not re.match(r"^\d+(?::\d+)$", aspect):
        aspect = "16:9"

    first_frame = str(extra.get("first_frame") or "").strip()
    last_frame = str(extra.get("last_frame") or "").strip()
    if not first_frame and isinstance(extra.get("keyframes"), (list, tuple)):
        keyframes = _agnes_string_list(extra.get("keyframes"))
        if keyframes:
            first_frame = keyframes[0]
        if len(keyframes) > 1:
            last_frame = keyframes[1]

    explicit_mode = str(extra.get("mode") or "").strip().lower()
    if explicit_mode in {"keyframes", "key_frame", "i2v"}:
        explicit_mode = "keyframe"
    if explicit_mode not in {"text", "keyframe", "reference"}:
        explicit_mode = ""

    images = _agnes_string_list(extra.get("images"))
    audios = _agnes_string_list(extra.get("audios"))
    videos = extra.get("videos")
    if isinstance(videos, (list, tuple)):
        videos = list(videos)
    elif videos:
        videos = [videos]
    else:
        videos = []

    if explicit_mode:
        mode = explicit_mode
    elif first_frame or last_frame:
        mode = "keyframe"
    elif images or audios or videos:
        mode = "reference"
    elif media_values:
        mode = "keyframe"
        first_frame = media_values[0]
    else:
        mode = "text"

    if mode == "reference" and not images and media_values:
        images = list(media_values)

    payload: Dict[str, Any] = {
        "model": target.model or ("agnes-video-2.5-flash" if flash else "agnes-video-2.5"),
        "prompt": str(request.prompt or "").strip(),
        "seconds": str(seconds),
        "mode": mode,
        "size": raw_size,
        "aspect_ratio": aspect,
        "n": 1,
    }

    if extra.get("seed") is not None:
        payload["seed"] = extra.get("seed")
    if extra.get("negative_prompt"):
        payload["negative_prompt"] = str(extra.get("negative_prompt"))

    # The API rejects media fields that do not belong to the selected mode.
    if mode == "keyframe":
        if first_frame:
            payload["first_frame"] = first_frame
        elif media_values:
            payload["first_frame"] = media_values[0]
        if last_frame:
            payload["last_frame"] = last_frame
    elif mode == "reference":
        if images:
            payload["images"] = images[:5] if flash else images
        if audios:
            payload["audios"] = audios[:3] if flash else audios
        # Flash explicitly does not support valid video inputs.
        if videos and not flash:
            payload["videos"] = videos
    return payload


def _agnes_payload(
    target: ImageModelTarget,
    request: VideoGenerateRequest,
    b64_images: List[str],
    references: Optional[Sequence[ImageReference]] = None,
) -> Dict[str, Any]:
    """Build the Agnes request selected by ``target.model``."""
    family = _agnes_video_family(str(target.model or ""))
    if family == "v25_flash":
        return _agnes_v25_payload(target, request, b64_images, references, flash=True)
    if family == "v25":
        return _agnes_v25_payload(target, request, b64_images, references)
    return _agnes_v20_payload(target, request, b64_images, references)


async def _generate_via_agnes(
    session: aiohttp.ClientSession,
    *,
    target: ImageModelTarget,
    request: VideoGenerateRequest,
    headers: Dict[str, str],
    timeout: int,
    b64_images: List[str],
) -> str:
    """Official Agnes async video via urllib in a worker thread.

    Note: ``session`` is accepted for API symmetry but unused — aiohttp often
    hits ConnectionTimeout against apihub.agnes-ai.com while urllib/httpx work.
    """
    _ = session  # kept for call-site compatibility
    endpoint = build_agnes_videos_endpoint(target.base_url)
    if not endpoint:
        raise RuntimeError("Agnes 视频渠道 base_url 无效")
    payload = _agnes_payload(target, request, b64_images, request.images)
    auth = str(headers.get("Authorization") or "").strip()
    if not auth and target.api_key:
        auth = f"Bearer {target.api_key}"

    socks_proxy = str((target.extra or {}).get("_socks_proxy") or "")

    def _create() -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request_headers = {
            "Authorization": auth,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "SelfieImage-Video/1.0",
        }
        if socks_proxy:
            try:
                response = requests.post(
                    endpoint,
                    data=body,
                    headers=request_headers,
                    timeout=min(90, max(30, int(timeout // 3) if timeout else 60)),
                    proxies={"http": socks_proxy, "https": socks_proxy},
                )
                raw = response.text
                if response.status_code >= 400:
                    raise RuntimeError(redact_sensitive_text(f"HTTP {response.status_code}: {raw[:800]}"))
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(redact_sensitive_text(f"Agnes 创建任务失败: {exc}")) from exc
        else:
            req = urllib.request.Request(endpoint, data=body, method="POST", headers=request_headers)
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": target.proxy, "https": target.proxy})) if target.proxy else urllib.request.build_opener()
                with opener.open(req, timeout=min(90, max(30, int(timeout // 3) if timeout else 60))) as response:
                    raw = response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                raise RuntimeError(redact_sensitive_text(f"HTTP {exc.code}: {raw[:800]}")) from exc
            except Exception as exc:
                raise RuntimeError(redact_sensitive_text(f"Agnes 创建任务失败: {exc}")) from exc
        try:
            data = json.loads(raw)
        except Exception:
            data = {"raw": raw}
        if not isinstance(data, dict):
            data = {"data": data}
        return data

    data = await asyncio.to_thread(_create)
    video_url = _extract_video_url(data)
    if video_url:
        return video_url
    video_id = _extract_video_id(data)
    task_id = _extract_agnes_task_id(data)
    if not video_id:
        video_id = task_id
    if not video_id:
        raise RuntimeError(f"Agnes 未返回 video_id/task_id: {str(data)[:400]}")
    poll_url = build_agnes_result_url(target.base_url, video_id, model=str(target.model or "agnes-video-v2.0"))
    legacy_id = task_id or video_id
    legacy = f"{endpoint.rstrip('/')}/{quote(legacy_id, safe='')}"
    return await _poll_agnes_task_urllib(
        poll_url=poll_url,
        legacy_poll_url=legacy,
        authorization=auth,
        timeout_seconds=timeout,
        proxy=socks_proxy or target.proxy,
    )


async def _poll_agnes_task_urllib(
    *,
    poll_url: str,
    legacy_poll_url: str,
    authorization: str,
    timeout_seconds: int,
    proxy: str = "",
) -> str:
    max_retries = max(6, int(timeout_seconds) // 8)
    urls = [u for u in (poll_url, legacy_poll_url) if u]
    last_error = ""
    deadline = time.monotonic() + max(30, int(timeout_seconds or 300))

    def _get(url: str) -> Dict[str, Any]:
        if str(proxy or "").lower().startswith(("socks5://", "socks5h://")):
            try:
                response = requests.get(
                    url,
                    headers={"Authorization": authorization, "Accept": "application/json", "User-Agent": "SelfieImage-Video/1.0"},
                    timeout=30,
                    proxies={"http": proxy, "https": proxy},
                )
                raw = response.text
                if response.status_code >= 400:
                    raise RuntimeError(redact_sensitive_text(f"HTTP {response.status_code}: {raw[:400]}"))
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(redact_sensitive_text(f"Agnes 轮询失败: {exc}")) from exc
        else:
            req = urllib.request.Request(
                url,
                method="GET",
                headers={
                    "Authorization": authorization,
                    "Accept": "application/json",
                    "User-Agent": "SelfieImage-Video/1.0",
                },
            )
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy})) if proxy else urllib.request.build_opener()
                with opener.open(req, timeout=30) as response:
                    raw = response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                raise RuntimeError(redact_sensitive_text(f"HTTP {exc.code}: {raw[:400]}")) from exc
        try:
            data = json.loads(raw)
        except Exception as exc:
            raise RuntimeError(f"Agnes 轮询返回非 JSON: {raw[:200]}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Agnes 轮询返回异常: {str(data)[:200]}")
        return data

    for attempt in range(max_retries):
        if time.monotonic() > deadline:
            break
        await asyncio.sleep(8 if attempt else 3)
        for url in urls:
            try:
                data = await asyncio.to_thread(_get, url)
            except RuntimeError as exc:
                last_error = str(exc)
                continue
            except Exception as exc:
                last_error = redact_sensitive_text(str(exc))
                continue
            status = _extract_task_status(data)
            if status in {"SUCCESS", "SUCCEEDED", "COMPLETED", "COMPLETE", "DONE"}:
                got = _extract_video_url(data)
                if got:
                    return got
                raise RuntimeError(f"Agnes 任务完成但无 metadata.url: {str(data)[:300]}")
            if status in {"FAIL", "FAILED", "FAILURE", "ERROR", "CANCELLED", "CANCELED"}:
                err = data.get("error")
                if isinstance(err, dict):
                    err = err.get("message") or str(err)
                raise RuntimeError(str(err or data.get("message") or "Agnes 视频任务失败"))
            # queued / pending / in_progress — continue outer loop
            break
    raise RuntimeError(
        f"Agnes 视频轮询超时（已等约 {timeout_seconds}s）" + (f"：{last_error}" if last_error else "")
    )


async def _poll_agnes_task(
    session: aiohttp.ClientSession,
    *,
    poll_url: str,
    legacy_poll_url: str,
    headers: Dict[str, str],
    timeout_seconds: int,
) -> str:
    """Deprecated aiohttp poll kept for compatibility; prefer urllib path."""
    auth = str(headers.get("Authorization") or "")
    return await _poll_agnes_task_urllib(
        poll_url=poll_url,
        legacy_poll_url=legacy_poll_url,
        authorization=auth,
        timeout_seconds=timeout_seconds,
    )


async def _generate_via_sync(
    session: aiohttp.ClientSession,
    *,
    target: ImageModelTarget,
    request: VideoGenerateRequest,
    headers: Dict[str, str],
    timeout: int,
    b64_images: List[str],
    family: str = "openai_video",
) -> str:
    """Long POST /videos/generations waiting for final URL (no re-POST on timeout)."""
    endpoint = build_video_generations_endpoint(target.base_url)
    payload = _video_payload(target, request, b64_images, family=family)
    async with session.post(
        endpoint,
        headers=headers,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=timeout),
        proxy=(target.proxy or None),
    ) as response:
        body_text = await response.text()
        if response.status >= 400:
            raise RuntimeError(redact_sensitive_text(f"HTTP {response.status}: {body_text[:800]}"))
        try:
            data = await response.json(content_type=None)
        except Exception:
            data = {"url": body_text.strip()} if str(body_text).strip().startswith("http") else {"raw": body_text}
    if not isinstance(data, dict):
        data = {"data": data}
    video_url = _extract_video_url(data)
    if video_url:
        return video_url
    task_id = _extract_task_id(data)
    if task_id:
        poll_url = _task_poll_url(endpoint, task_id, data)
        return await _poll_task(session, poll_url=poll_url, headers=headers, timeout_seconds=timeout, proxy=target.proxy)
    raise RuntimeError(f"同步接口未返回视频地址: {str(data)[:400]}")


def _chat_completions_endpoint(base_url: str) -> str:
    base = normalize_image_base_url(base_url) or str(base_url or "").rstrip("/")
    if not base:
        return ""
    lowered = base.lower()
    if lowered.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    if "/v1/" in lowered:
        return f"{base.rstrip('/')}/chat/completions"
    return f"{base.rstrip('/')}/v1/chat/completions"


async def _generate_via_chat(
    session: aiohttp.ClientSession,
    *,
    target: ImageModelTarget,
    request: VideoGenerateRequest,
    headers: Dict[str, str],
    timeout: int,
    b64_images: List[str],
) -> str:
    """Chat Completions style: model returns video URL / markdown link in content (OmniDraw openai_chat)."""
    endpoint = _chat_completions_endpoint(target.base_url)
    user_content: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"请根据以下描述生成视频，并在回复中给出可下载的视频直链（http/https 或 data:video）。\n"
                f"时长约 {int(request.duration or 5)} 秒。\n"
                f"描述：{str(request.prompt or '').strip()}"
            ),
        }
    ]
    for img in b64_images[:2]:
        user_content.append({"type": "image_url", "image_url": {"url": img}})
    payload = {
        "model": target.model,
        "messages": [{"role": "user", "content": user_content if len(user_content) > 1 else user_content[0]["text"]}],
        "stream": False,
    }
    if isinstance(request.extra, dict) and request.extra:
        # allow temperature etc., but do not override messages/model casually
        for key, value in request.extra.items():
            if key not in {"messages", "model", "stream"}:
                payload[key] = value
    async with session.post(
        endpoint,
        headers=headers,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=timeout),
        proxy=(target.proxy or None),
    ) as response:
        body_text = await response.text()
        if response.status >= 400:
            raise RuntimeError(redact_sensitive_text(f"HTTP {response.status}: {body_text[:800]}"))
        try:
            data = await response.json(content_type=None)
        except Exception:
            data = {"raw": body_text}
    # Standard chat choices
    content = ""
    if isinstance(data, dict):
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            msg = (choices[0] or {}).get("message") or {}
            content = str(msg.get("content") or "")
        if not content:
            content = str(data.get("content") or data.get("output") or data.get("raw") or "")
    video_url = _extract_video_url(data) if isinstance(data, dict) else ""
    if not video_url:
        video_url = _extract_url(content)
    if not video_url or not (video_url.startswith("http") or video_url.startswith("data:")):
        raise RuntimeError(f"对话接口未解析到视频链接: {str(content or data)[:400]}")
    return video_url


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
    last_source = ""
    for target in targets:
        async with channel_client_session(target.proxy, session) as target_session:
            result = await generate_video_openai_compatible(
                target_session_proxy(target),
                request,
                target_session,
                save_dir=save_dir,
            )
        attempts.extend(result.attempts or [])
        if result.video_path and not result.error:
            result.attempts = attempts
            return result
        last_error = result.error or last_error
        last_source = result.video_source or last_source
        # stop on non-retryable
        if result.attempts:
            cat = str((result.attempts[-1] or {}).get("error_category") or "")
            if cat in {"auth", "unsafe", "not_found"} and len(targets) == 1:
                break
    return VideoGenerateResult(error=last_error or "视频生成失败", video_source=last_source, attempts=attempts)
