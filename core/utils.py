"""Shared utility helpers."""

from __future__ import annotations

import base64
import asyncio
import binascii
import hashlib
import inspect
import json
import mimetypes
import os
import re
import time
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

import aiohttp


IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".avif",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
    ".jfif",
    ".svg",
}


async def resolve_awaitable(value: Any) -> Any:
    result = value
    while inspect.isawaitable(result):
        result = await result
    return result


def load_json_file(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_json_file(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{time.time_ns()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def detect_mime_by_bytes(data: bytes) -> str:
    b = data or b""
    if len(b) >= 2 and b[0] == 0xFF and b[1] == 0xD8:
        return "image/jpeg"
    if len(b) >= 4 and b[:4] == b"\x89PNG":
        return "image/png"
    if len(b) >= 3 and b[:3] == b"GIF":
        return "image/gif"
    if len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if len(b) >= 2 and b[:2] == b"BM":
        return "image/bmp"
    if len(b) >= 4 and b[:4] in {b"II*\x00", b"MM\x00*"}:
        return "image/tiff"
    if len(b) >= 12 and b[4:8] == b"ftyp":
        brand = b[8:12]
        if brand == b"avif":
            return "image/avif"
        if brand in {b"heic", b"heix"}:
            return "image/heic"
        if brand in {b"heif", b"mif1"}:
            return "image/heif"
    stripped = b.lstrip().lower()
    if stripped.startswith(b"<svg") or (stripped.startswith(b"<?xml") and b"<svg" in stripped[:512]):
        return "image/svg+xml"
    return "image/png"


def looks_like_image_bytes(data: bytes) -> bool:
    b = data or b""
    stripped = b.lstrip().lower()
    return (
        len(b) >= 2 and b[:2] == b"\xff\xd8"
        or len(b) >= 4 and b[:4] == b"\x89PNG"
        or len(b) >= 3 and b[:3] == b"GIF"
        or len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP"
        or len(b) >= 2 and b[:2] == b"BM"
        or len(b) >= 4 and b[:4] in {b"II*\x00", b"MM\x00*"}
        or len(b) >= 12 and b[4:8] == b"ftyp" and b[8:12] in {b"avif", b"heic", b"heix", b"heif", b"mif1"}
        or stripped.startswith(b"<svg")
        or stripped.startswith(b"<?xml") and b"<svg" in stripped[:512]
    )


def normalize_image_mime(mime: str, fallback: str = "image/png") -> str:
    text = str(mime or "").split(";", 1)[0].strip().lower()
    if text in {"image/jpg", "image/pjpeg"}:
        return "image/jpeg"
    if text.startswith("image/"):
        return text
    return fallback


def ext_from_mime(mime: str) -> str:
    mime = normalize_image_mime(mime)
    if "jpeg" in mime or "jpg" in mime:
        return "jpg"
    if "webp" in mime:
        return "webp"
    if "gif" in mime:
        return "gif"
    if "bmp" in mime:
        return "bmp"
    if "tiff" in mime or "tif" in mime:
        return "tiff"
    if "avif" in mime:
        return "avif"
    if "heic" in mime:
        return "heic"
    if "heif" in mime:
        return "heif"
    if "svg" in mime:
        return "svg"
    return "png"


def guess_image_content_type(source: str, fallback: str = "image/png") -> str:
    text = str(source or "")
    lowered = text.lower()
    if lowered.startswith("data:"):
        header = text.split(",", 1)[0]
        media_type = header[5:].split(";", 1)[0].strip()
        if media_type.startswith("image/"):
            return normalize_image_mime(media_type)
    path_text = lowered.split("#", 1)[0].split("?", 1)[0]
    guessed = mimetypes.guess_type(path_text)[0] or ""
    if guessed.startswith("image/"):
        return normalize_image_mime(guessed)
    if path_text.endswith((".jpg", ".jpeg", ".jfif")):
        return "image/jpeg"
    if path_text.endswith(".webp"):
        return "image/webp"
    if path_text.endswith(".gif"):
        return "image/gif"
    if path_text.endswith(".bmp"):
        return "image/bmp"
    if path_text.endswith((".tif", ".tiff")):
        return "image/tiff"
    if path_text.endswith(".avif"):
        return "image/avif"
    if path_text.endswith(".heic"):
        return "image/heic"
    if path_text.endswith(".heif"):
        return "image/heif"
    if path_text.endswith(".svg"):
        return "image/svg+xml"
    return fallback


def decode_base64_payload(value: str) -> bytes:
    text = str(value or "").strip()
    if "," in text:
        text = text.split(",", 1)[1]
    if text.lower().startswith("base64://"):
        text = text[len("base64://") :]
    text = re.sub(r"\s+", "", text)
    if not text:
        return b""
    padded = text + ("=" * (-len(text) % 4))
    try:
        if "-" in padded or "_" in padded:
            return base64.urlsafe_b64decode(padded)
        return base64.b64decode(padded, validate=False)
    except (binascii.Error, ValueError):
        try:
            return base64.urlsafe_b64decode(padded)
        except (binascii.Error, ValueError):
            return b""


def data_url_to_bytes(input_text: str) -> Tuple[bytes, str]:
    text = str(input_text or "").strip()
    if not text:
        return b"", "image/png"

    def valid_image_or_empty(data: bytes, mime: str = "") -> Tuple[bytes, str]:
        if not data or not looks_like_image_bytes(data):
            return b"", normalize_image_mime(mime or "image/png")
        return data, detect_mime_by_bytes(data)

    match = re.match(r"^data:([^;,]+)(?:;[^,;]*)*;base64,([\s\S]+)$", text, flags=re.I)
    if match:
        mime = normalize_image_mime(match.group(1))
        return valid_image_or_empty(decode_base64_payload(match.group(2)), mime)

    prefix = "base64://"
    if text.lower().startswith(prefix):
        return valid_image_or_empty(decode_base64_payload(text))

    return valid_image_or_empty(decode_base64_payload(text))


def bytes_to_data_url(data: bytes, mime: str = "") -> str:
    resolved = normalize_image_mime(mime or detect_mime_by_bytes(data))
    return f"data:{resolved};base64,{base64.b64encode(data).decode('utf-8')}"


def save_image_bytes(data: bytes, save_dir: str, prefix: str = "img", mime: str = "") -> str:
    os.makedirs(save_dir, exist_ok=True)
    resolved = normalize_image_mime(mime or detect_mime_by_bytes(data))
    digest = hashlib.sha256(data[:1024] + str(time.time_ns()).encode("ascii")).hexdigest()[:14]
    path = os.path.join(save_dir, f"{prefix}_{digest}.{ext_from_mime(resolved)}")
    with open(path, "wb") as file:
        file.write(data)
    return path


def collect_record_cache_paths(records: Any) -> List[str]:
    result: List[str] = []
    seen = set()
    path_keys = ("request_image_paths", "generated_image_paths", "image_paths", "generated_video_paths", "video_paths")

    def add(value: Any) -> None:
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, (list, tuple, set)):
            values = list(value)
        else:
            return
        for item in values:
            text = str(item or "").strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in path_keys:
                add(value.get(key))
            for child in value.values():
                if isinstance(child, (dict, list, tuple)):
                    walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                if isinstance(child, (dict, list, tuple)):
                    walk(child)

    walk(records)
    return result


def collect_unreferenced_record_cache_paths(removed_records: Any, retained_records: Any) -> List[str]:
    retained = set(collect_record_cache_paths(retained_records))
    result: List[str] = []
    seen = set()
    for path in collect_record_cache_paths(removed_records):
        if path in retained or path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def safe_delete_relative_files(base_dir: str, rel_paths: Iterable[Any]) -> List[str]:
    deleted: List[str] = []
    raw_base = str(base_dir or "").strip()
    if not raw_base:
        return deleted
    base = os.path.abspath(raw_base)
    for item in rel_paths:
        rel_path = str(item or "").strip()
        if not rel_path or os.path.isabs(rel_path):
            continue
        path = os.path.abspath(os.path.join(base, rel_path))
        if path == base or not path.startswith(base + os.sep):
            continue
        try:
            if os.path.isfile(path):
                os.remove(path)
                deleted.append(rel_path)
        except OSError:
            continue
    return deleted


def collect_cache_cleanup_candidates(
    base_dir: str,
    protected_paths: Optional[Iterable[Any]] = None,
    referenced_paths: Optional[Iterable[Any]] = None,
    record_timestamps: Optional[Dict[Any, Any]] = None,
) -> List[str]:
    raw_base = str(base_dir or "").strip()
    if not raw_base:
        return []
    base = os.path.abspath(raw_base)

    def inside_cache(path: str) -> bool:
        return path != base and path.startswith(base + os.sep)

    def normalize_to_abs(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        path = os.path.abspath(text if os.path.isabs(text) else os.path.join(base, text))
        return path if inside_cache(path) else ""

    protected = {path for path in (normalize_to_abs(item) for item in protected_paths or []) if path}
    referenced = {path for path in (normalize_to_abs(item) for item in referenced_paths or []) if path}
    timestamps: Dict[str, float] = {}
    for raw_path, raw_timestamp in (record_timestamps or {}).items():
        path = normalize_to_abs(raw_path)
        if not path:
            continue
        try:
            timestamp = float(raw_timestamp or 0)
        except (TypeError, ValueError):
            timestamp = 0.0
        if timestamp > 0:
            timestamps[path] = max(timestamp, timestamps.get(path, 0.0))
    candidates: List[Tuple[int, float, float, str]] = []
    for root, _, files in os.walk(base):
        for name in files:
            path = os.path.abspath(os.path.join(root, name))
            if path in protected or not inside_cache(path):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            priority = 1 if path in referenced else 0
            # Referenced media follows its generation record's timestamp;
            # request files often have an older mtime than their response.
            sort_time = timestamps.get(path, 0.0) if path in referenced else 0.0
            if sort_time <= 0:
                sort_time = mtime
            candidates.append((priority, sort_time, mtime, path))
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return [path for _, _, _, path in candidates]


def looks_like_image_url(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    lowered = value.lower()
    if lowered.startswith(("data:image/", "base64://")):
        return True
    if not lowered.startswith(("http://", "https://")):
        return False
    try:
        path = urlsplit(value).path.lower()
    except ValueError:
        path = lowered.split("#", 1)[0].split("?", 1)[0]
    return (
        path.endswith(tuple(IMAGE_EXTENSIONS))
        or "qpic.cn" in lowered
        or "qlogo.cn" in lowered
        or "multimedia.nt.qq.com.cn" in lowered
        or "/download?" in lowered
    )


def decode_html_entities(text: str) -> str:
    return (
        str(text or "")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
        .replace("&#44;", ",")
        .replace("&#91;", "[")
        .replace("&#93;", "]")
    )


def redact_sensitive_text(text: str) -> str:
    value = str(text or "")
    if not value:
        return ""
    patterns = [
        (r"(?i)([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^@\s/]+@", r"\1[REDACTED]@"),
        (r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._\-+/=]{8,}", r"\1[REDACTED]"),
        (r"(?i)((?:x-api-key|x-goog-api-key|[A-Za-z0-9_-]*(?:api[_-]?key|apikey|token|secret))\s*[:=]\s*)[A-Za-z0-9._\-+/=]{8,}", r"\1[REDACTED]"),
        (r"(?i)((?:proxy|password)\s*[=:]\s*)[^\s,;]{8,}", r"\1[REDACTED]"),
        (r"(?i)([\"'](?:api[_-]?key|apikey|token|secret|authorization|password|proxy|cookie|set-cookie|x-api-key|x-goog-api-key|[A-Za-z0-9_-]*(?:token|secret|api[_-]?key))[\"']\s*:\s*[\"'])[^\"']{8,}([\"'])", r"\1[REDACTED]\2"),
        (r"sk-[A-Za-z0-9._\-]{8,}", "sk-[REDACTED]"),
        (r"AIza[0-9A-Za-z_\-]{12,}", "AIza[REDACTED]"),
        (r"Bearer\s+[A-Za-z0-9._\-+/=]{8,}", "Bearer [REDACTED]"),
    ]
    for pattern, replacement in patterns:
        value = re.sub(pattern, replacement, value)
    return value


def redact_sensitive_data(value: Any) -> Any:
    sensitive_keys = {"api_key", "apikey", "api-key", "token", "secret", "authorization", "password", "proxy", "cookie", "set-cookie"}

    def is_sensitive_key(key: Any) -> bool:
        key_text = str(key or "").strip().lower()
        return (
            key_text in sensitive_keys
            or key_text.endswith("_token")
            or key_text.endswith("-token")
            or key_text.endswith("_secret")
            or key_text.endswith("-secret")
            or key_text.endswith("secret")
            or key_text.endswith("token")
            or key_text.endswith("apikey")
            or key_text.endswith("_api_key")
            or key_text.endswith("-api-key")
            or "api_key" in key_text
            or "api-key" in key_text
        )

    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        result: Dict[Any, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(key):
                result[key] = "[REDACTED]" if item else item
            else:
                result[key] = redact_sensitive_data(item)
        return result
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item) for item in value)
    if isinstance(value, set):
        return {redact_sensitive_data(item) for item in value}
    return value


def redact_channel_attempts(attempts: Any) -> List[Any]:
    """Redact attempt metadata while retaining the channel's original error."""
    source = list(attempts or [])
    redacted = redact_sensitive_data(source)
    if not isinstance(redacted, list):
        return []
    for original, safe in zip(source, redacted):
        if isinstance(original, dict) and isinstance(safe, dict) and "error" in original:
            safe["error"] = original.get("error")
    return redacted


def _media_source_kind(value: Any, source_type: Any = "") -> str:
    """Infer a stable source type without inspecting or displaying the payload."""
    kind = str(source_type or "").strip().lower()
    text = str(value or "").strip()
    lowered = text.lower()
    if kind == "base64" or lowered.startswith(("data:", "base64://")):
        return "base64"
    compact = re.sub(r"\s+", "", text)
    if len(compact) >= 100 and re.fullmatch(r"[A-Za-z0-9+/_=-]+", compact):
        return "base64"
    if kind in {"url", "local", "base64"}:
        return kind
    if lowered.startswith(("http://", "https://")):
        return "url"
    return "local"


def _preserve_media_sources(sources: Any) -> List[Dict[str, str]]:
    """Retain media origins from both current objects and legacy string arrays."""
    if not isinstance(sources, list):
        return []
    preserved: List[Dict[str, str]] = []
    for item in sources:
        if isinstance(item, dict):
            value = item.get("value")
            if value is None:
                value = item.get("url") or item.get("source") or item.get("data")
            source_type = item.get("type") or ""
        elif isinstance(item, str):
            value = item
            source_type = ""
        else:
            continue
        text = str(value or "").strip()
        if not text:
            continue
        preserved.append({"type": _media_source_kind(text, source_type), "value": text})
    return preserved


def redact_generation_record(record: Any) -> Dict[str, Any]:
    """Redact record metadata but keep authenticated channel diagnostics intact."""
    if not isinstance(record, dict):
        return {}

    # Media origins are restored below. Remove them from the recursive secret
    # scanner first so startup/persistence does not rescan multi-megabyte Base64.
    working = dict(record)
    working_response = record.get("response_data")
    if isinstance(working_response, dict):
        working_response = dict(working_response)
        working["response_data"] = working_response
    for container in (working, working_response):
        if not isinstance(container, dict):
            continue
        if "generated_image_sources" in container:
            container["generated_image_sources"] = []
        for key in ("video_url", "video_source"):
            if key in container:
                container[key] = ""

    redacted = redact_sensitive_data(working)
    if not isinstance(redacted, dict):
        return {}

    # Media origins are explicitly shown/copied in the dashboard. Preserve the
    # upstream value instead of redacting query parameters or base64 payloads.
    def restore_media_fields(source: Dict[str, Any], target: Dict[str, Any]) -> None:
        for key in ("video_url", "video_source"):
            if key in source:
                target[key] = source.get(key) or ""
        if isinstance(source.get("generated_image_sources"), list):
            target["generated_image_sources"] = _preserve_media_sources(source["generated_image_sources"])

    restore_media_fields(record, redacted)
    original_response = record.get("response_data")
    safe_response = redacted.get("response_data")
    if isinstance(original_response, dict) and isinstance(safe_response, dict):
        restore_media_fields(original_response, safe_response)

    for key in ("attempts", "failure_reasons"):
        if isinstance(record.get(key), list):
            redacted[key] = redact_channel_attempts(record.get(key))

    if isinstance(original_response, dict) and isinstance(safe_response, dict):
        if isinstance(original_response.get("attempts"), list):
            safe_response["attempts"] = redact_channel_attempts(original_response.get("attempts"))
    return redacted


def _is_inline_media_source(value: Any, source_type: str = "") -> bool:
    return _media_source_kind(value, source_type) == "base64"


def _detail_media_source(source: Any, index: int = -1) -> Any:
    """Keep URL sources in detail responses, but defer large inline payloads."""
    if isinstance(source, dict):
        source_type = str(source.get("type") or "").strip().lower()
        value = source.get("value") or ""
    else:
        source_type = ""
        value = source
    if not str(value or "").strip():
        return {"type": source_type or "local", "value": "", "index": index} if index >= 0 else ""
    if _is_inline_media_source(value, source_type):
        result = {
            "type": "base64",
            "value": "[Base64，点击复制按钮获取原文]",
            "deferred": True,
            "size": len(str(value)),
            "index": index,
        }
        return result
    result = {"type": source_type or "url", "value": str(value)}
    if index >= 0:
        result["index"] = index
    return result


def redact_generation_record_for_detail(record: Any) -> Dict[str, Any]:
    """Return a fast detail payload; inline media is fetched only on demand."""
    if not isinstance(record, dict):
        return {}

    # Do not run the sensitive-text scanner over multi-megabyte data URLs. Keep
    # a typed placeholder through redaction, then restore only the small detail
    # metadata (URL values remain visible; inline payloads stay deferred).
    working = dict(record)
    detail_media: Dict[str, Any] = {}

    def strip_media(container: Dict[str, Any], target: Dict[str, Any]) -> None:
        sources = container.get("generated_image_sources")
        if isinstance(sources, list):
            target["generated_image_sources"] = [
                _detail_media_source(item, index)
                for index, item in enumerate(sources)
                if isinstance(item, (dict, str))
            ]
            container["generated_image_sources"] = target["generated_image_sources"]
        for key in ("video_source", "video_url"):
            if key in container:
                target[key] = _detail_media_source(container.get(key)) if _is_inline_media_source(container.get(key)) else container.get(key) or ""
                container[key] = target[key]

    strip_media(working, detail_media)
    response_data = working.get("response_data")
    if isinstance(response_data, dict):
        response_copy = dict(response_data)
        working["response_data"] = response_copy
        detail_response: Dict[str, Any] = {}
        strip_media(response_copy, detail_response)
        detail_media["response_data"] = detail_response

    redacted = redact_sensitive_data(working)
    if not isinstance(redacted, dict):
        return {}

    for key, value in detail_media.items():
        if key == "response_data" and isinstance(value, dict) and isinstance(redacted.get(key), dict):
            redacted[key].update(value)
        else:
            redacted[key] = value
    for key in ("attempts", "failure_reasons"):
        if isinstance(record.get(key), list):
            redacted[key] = redact_channel_attempts(record.get(key))
    if isinstance(record.get("response_data"), dict) and isinstance(record["response_data"].get("attempts"), list):
        if isinstance(redacted.get("response_data"), dict):
            redacted["response_data"]["attempts"] = redact_channel_attempts(record["response_data"]["attempts"])
    return redacted


def generation_record_media_sources(record: Any) -> Dict[str, Any]:
    """Extract original media sources for an explicit copy action."""
    if not isinstance(record, dict):
        return {"generated_image_sources": [], "video_source": ""}
    response_data = record.get("response_data") if isinstance(record.get("response_data"), dict) else {}
    image_sources = record.get("generated_image_sources")
    if not isinstance(image_sources, list) or not image_sources:
        fallback_sources = response_data.get("generated_image_sources")
        if isinstance(fallback_sources, list) and fallback_sources:
            image_sources = fallback_sources
    if not isinstance(image_sources, list):
        image_sources = []
    video_source = record.get("video_source") or record.get("video_url") or response_data.get("video_source") or response_data.get("video_url") or ""
    return {
        "generated_image_sources": image_sources,
        "video_source": video_source,
    }


def _truncate_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def split_generation_record_images(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One generated image per monitor row."""
    if not isinstance(record, dict):
        return []
    raw = dict(record)
    paths = [str(p).strip() for p in (raw.get("generated_image_paths") or []) if str(p or "").strip()]
    raw_md5s = raw.get("md5s")
    if not isinstance(raw_md5s, list):
        raw_md5s = raw.get("generated_image_md5s")
    md5s = [str(value or "").strip().lower() for value in (raw_md5s or [])]
    raw_sources = raw.get("generated_image_sources")
    if not isinstance(raw_sources, list):
        raw_sources = []
    if not md5s and raw.get("md5"):
        md5s = [str(raw.get("md5") or "").strip().lower()]
    # ``md5s`` is only an input for splitting a batch; persisted rows use ``md5``.
    raw.pop("md5s", None)
    raw.pop("generated_image_md5s", None)
    if len(paths) <= 1:
        if paths:
            raw["generated_image_paths"] = paths
            if raw_sources:
                raw["generated_image_sources"] = raw_sources[:1]
            if raw.get("success"):
                raw["count"] = 1
            raw["md5"] = md5s[0] if md5s else str(raw.get("md5") or "").strip().lower()
        return [raw]
    resp = raw.get("response_data") if isinstance(raw.get("response_data"), dict) else {}
    pieces: List[Dict[str, Any]] = []
    for index, path in enumerate(paths):
        piece = dict(raw)
        piece["generated_image_paths"] = [path]
        if index < len(raw_sources):
            piece["generated_image_sources"] = [raw_sources[index]]
        piece["count"] = 1
        piece["md5"] = md5s[index] if index < len(md5s) else ""
        piece.pop("id", None)
        if resp:
            slim_resp = dict(resp)
            slim_resp["generated_image_paths"] = [path]
            if index < len(raw_sources):
                slim_resp["generated_image_sources"] = [raw_sources[index]]
            slim_resp["count"] = 1
            piece["response_data"] = slim_resp
        pieces.append(piece)
    return pieces


def compact_generation_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Shrink persisted generation records: drop prompt duplicates, cap attempt errors."""
    if not isinstance(record, dict):
        return {}
    out = dict(record)
    # Older records only persisted ``success``.  Materialize the lifecycle
    # status during compaction so list/detail consumers can distinguish a
    # delivery failure from a generation failure without re-deriving it.
    status = str(out.get("status") or "").strip().lower()
    if not status:
        if bool(out.get("cancelled")):
            status = "cancelled"
        elif bool(out.get("delivery_failed")) or (
            bool(out.get("generation_success")) and out.get("delivery_success") is False
        ):
            status = "delivery_failed"
        elif bool(out.get("success")):
            status = "succeeded"
        elif bool(out.get("generation_success")):
            status = "partial_success"
        else:
            status = "failed"
        out["status"] = status
    value = str(out.get("md5") or "").strip().lower()
    # Keep the key present so older records render an explicit empty value.
    out["md5"] = value if re.fullmatch(r"[0-9a-f]{32}", value) else ""
    for key in ("prompt", "original_prompt", "request_prompt", "error", "failure_reason"):
        if key in out:
            lim = 6000 if key in {"prompt", "original_prompt", "request_prompt"} else 800
            out[key] = _truncate_text(out.get(key), lim)

    rd = out.get("request_data")
    # Keep an explicit route on every record. Older rows only exposed the
    # combined ``used_model`` label (or buried the route in attempts), which
    # made the detail view depend on parsing provider-specific strings.
    route_channel = str(out.get("channel") or "").strip()
    route_model = str(out.get("model") or "").strip()
    if isinstance(rd, dict):
        route_channel = route_channel or str(rd.get("channel") or "").strip()
        route_model = route_model or str(rd.get("model") or "").strip()
    route_attempts: List[Any] = []
    if isinstance(out.get("attempts"), list):
        route_attempts.extend(out.get("attempts") or [])
    response_for_route = out.get("response_data")
    if not route_attempts and isinstance(response_for_route, dict) and isinstance(response_for_route.get("attempts"), list):
        route_attempts.extend(response_for_route.get("attempts") or [])
    # Prefer the successful attempt; otherwise use the last attempted target.
    for attempt in reversed(route_attempts):
        if not isinstance(attempt, dict):
            continue
        if attempt.get("success") or not route_channel or not route_model:
            channel_value = str(attempt.get("channel") or "").strip()
            model_value = str(attempt.get("model") or "").strip()
            if channel_value:
                route_channel = channel_value
            if model_value:
                route_model = model_value
            if route_channel and route_model and attempt.get("success"):
                break
    used_model = str(out.get("used_model") or "").strip()
    if (not route_channel or not route_model) and used_model:
        # Target labels are emitted as ``channel/model``. Keep this only as a
        # legacy fallback; structured attempt fields above remain authoritative.
        label_channel, separator, label_model = used_model.partition("/")
        if separator:
            route_channel = route_channel or label_channel.strip()
            route_model = route_model or label_model.strip()
        elif not route_model:
            route_model = used_model
    if route_channel:
        out["channel"] = _truncate_text(route_channel, 300)
    if route_model:
        out["model"] = _truncate_text(route_model, 300)
    if isinstance(rd, dict) and (route_channel or route_model):
        # Retry payloads read the compact request_data object, so backfill the
        # inferred route there as well as at the record top level.
        rd = dict(rd)
        if route_channel:
            rd.setdefault("channel", route_channel)
        if route_model:
            rd.setdefault("model", route_model)

    if isinstance(rd, dict):
        slim_rd: Dict[str, Any] = {
            "aspect_ratio": rd.get("aspect_ratio"),
            "resolution": rd.get("resolution"),
            "reference_image_count": rd.get("reference_image_count"),
            "reference_images": rd.get("reference_images"),
            "targets": rd.get("targets") if isinstance(rd.get("targets"), list) else [],
            "request_image_paths": rd.get("request_image_paths")
            if isinstance(rd.get("request_image_paths"), list)
            else [],
        }
        # Keep the small, user-visible request parameters needed to explain a
        # historical generation.  Provider request bodies and raw prompt
        # duplicates intentionally stay out of the compact index.
        for key in (
            "requested_count",
            "count",
            "raw_reference_image_count",
            "duration",
            "requested_duration",
            "timeout_seconds",
            "size",
            "media_type",
            "channel",
            "model",
            "prompt_enhance",
            "use_selfie_reference",
            "reference_selection",
            # Keep the effective audit text available when a request was
            # normalized before the provider call.  It is still bounded and
            # passes through the normal redaction step above.
            "audit_prompt",
            "request_prompt_en",
            "cos",
            "cos_pose",
            "cos_scene",
            "cos_view",
        ):
            value = rd.get(key)
            if isinstance(value, (str, int, float, bool)):
                if isinstance(value, str) and key in {"audit_prompt", "request_prompt_en"}:
                    value = _truncate_text(value, 6000)
                slim_rd[key] = value
        image_to_text = rd.get("image_to_text")
        if isinstance(image_to_text, dict):
            # Only retain the small status fields used by the detail view;
            # provider responses and inline reference data stay excluded.
            slim_image_to_text: Dict[str, Any] = {}
            for key in ("enabled", "applied", "target_count", "used_by_model"):
                value = image_to_text.get(key)
                if isinstance(value, (str, int, float, bool)):
                    slim_image_to_text[key] = value
            if image_to_text.get("error"):
                slim_image_to_text["error"] = _truncate_text(image_to_text.get("error"), 300)
            if slim_image_to_text:
                slim_rd["image_to_text"] = slim_image_to_text
        reference_selection = rd.get("reference_selection")
        if isinstance(reference_selection, Mapping):
            roles = reference_selection.get("roles")
            if isinstance(roles, Mapping):
                normalized_roles = {}
                for key, value in roles.items():
                    if not str(key).strip():
                        continue
                    try:
                        count = int(value or 0)
                    except (TypeError, ValueError):
                        count = 0
                    normalized_roles[str(key)[:40]] = max(0, min(24, count))
                try:
                    selected_count = int(reference_selection.get("selected_count") or 0)
                except (TypeError, ValueError):
                    selected_count = 0
                try:
                    failed_count = int(reference_selection.get("failed_count") or 0)
                except (TypeError, ValueError):
                    failed_count = 0
                slim_rd["reference_selection"] = {
                    "roles": normalized_roles,
                    "selected_count": max(0, min(24, selected_count)),
                    "failed_count": max(0, min(24, failed_count)),
                    "used_persona": bool(reference_selection.get("used_persona")),
                    "used_context_fallback": bool(reference_selection.get("used_context_fallback")),
                }
        cache_cleanup = rd.get("cache_cleanup") or rd.get("cache_cleanup_before_generation")
        if isinstance(cache_cleanup, dict):
            slim_cleanup: Dict[str, Any] = {}
            for key in (
                "deleted_count",
                "would_delete_count",
                "deleted_bytes",
                "would_delete_bytes",
            ):
                value = cache_cleanup.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    slim_cleanup[key] = value
            for key in ("deleted", "would_delete"):
                value = cache_cleanup.get(key)
                if isinstance(value, list):
                    slim_cleanup[key] = [
                        str(item.get("path") if isinstance(item, dict) else item).strip()[:300]
                        for item in value
                        if str(item.get("path") if isinstance(item, dict) else item).strip()
                    ][:100]
            if slim_cleanup:
                slim_rd["cache_cleanup"] = slim_cleanup
        composition = rd.get("composition")
        if isinstance(composition, dict):
            # ``composition`` is generated locally and contains only stable
            # classification fields; copy an allow-list to avoid retaining
            # arbitrary provider metadata.
            slim_composition: Dict[str, Any] = {}
            for key in (
                "strategy",
                "prompt_hash",
                "aspect_ratio",
                "resolution",
                "reference_image_count",
            ):
                value = composition.get(key)
                if isinstance(value, (str, int, float, bool)):
                    slim_composition[key] = value
            if slim_composition:
                slim_rd["composition"] = slim_composition
        # Keep lightweight workflow links while still excluding the original
        # prompt and provider-sensitive request details from the index payload.
        for key in (
            "studio_session_id",
            "studio_task_id",
            "studio_template",
            "studio_source_asset_ids",
            "retry_record_id",
        ):
            value = rd.get(key)
            if isinstance(value, (str, int, float, bool)):
                slim_rd[key] = value
            elif isinstance(value, list):
                slim_rd[key] = [str(item).strip() for item in value if str(item).strip()][:24]
        pe = rd.get("prompt_en")
        if isinstance(pe, dict):
            slim_rd["prompt_en"] = {
                "applied": pe.get("applied"),
                "error": _truncate_text(pe.get("error"), 200),
                "format": pe.get("format"),
            }
        out["request_data"] = slim_rd
        for key in (
            "requested_count",
            "duration",
            "requested_duration",
            "raw_reference_image_count",
            "timeout_seconds",
            "size",
            "composition",
        ):
            if key not in out and key in slim_rd:
                out[key] = slim_rd[key]
        if "requested_count" not in out:
            count_value = slim_rd.get("count")
            if isinstance(count_value, (int, float)) and not isinstance(count_value, bool):
                out["requested_count"] = max(1, int(count_value))

    # ``prompt`` is the actual text sent to the provider after reference
    # instructions / optional translation.  Persist an explicit alias so the
    # detail view can distinguish it from the user's original request.
    final_prompt = out.get("final_prompt")
    if not final_prompt and isinstance(rd, dict):
        final_prompt = rd.get("request_prompt_en") or rd.get("request_prompt")
    if not final_prompt:
        final_prompt = out.get("prompt") or out.get("request_prompt")
    if final_prompt:
        out["final_prompt"] = _truncate_text(final_prompt, 6000)

    # COS randomization is encoded in action markers.  Promote the markers to
    # compact scalar fields so records remain searchable/reproducible even
    # when the surrounding prompt is truncated or translated.
    marker_text = " ".join(
        str(out.get(key) or "")
        for key in ("original_prompt", "request_prompt", "prompt", "final_prompt")
    )
    if isinstance(rd, dict):
        marker_text += " " + " ".join(str(rd.get(key) or "") for key in ("request_prompt", "request_prompt_en"))
    for key, pattern in (
        ("cos", r"【cos:([a-z0-9_]+)】"),
        ("cos_pose", r"【cos_pose:([a-z0-9_]+)】"),
        ("cos_scene", r"【cos_scene:([a-z0-9_]+)】"),
        ("cos_view", r"【cos_view:([a-z0-9_]+)】"),
    ):
        value = out.get(key)
        if not isinstance(value, str) or not value.strip():
            match = re.search(pattern, marker_text, flags=re.I)
            value = match.group(1).lower() if match else ""
        if value:
            out[key] = str(value).strip().lower()
            if isinstance(out.get("request_data"), dict):
                out["request_data"].setdefault(key, out[key])

    resp = out.get("response_data")
    if isinstance(resp, dict):
        out["response_data"] = {
            "success": resp.get("success"),
            "status": resp.get("status"),
            "stage": resp.get("stage"),
            "error": _truncate_text(resp.get("error"), 500),
            "generation_success": resp.get("generation_success"),
            "delivery_success": resp.get("delivery_success"),
            "delivery_failed": resp.get("delivery_failed"),
            "delivery_error": _truncate_text(resp.get("delivery_error"), 800),
            "blocked_images_retained": resp.get("blocked_images_retained"),
            "used_model": resp.get("used_model"),
            "elapsed_seconds": resp.get("elapsed_seconds"),
            "count": resp.get("count"),
            "generated_image_paths": resp.get("generated_image_paths")
            if isinstance(resp.get("generated_image_paths"), list)
            else [],
            "generated_video_paths": resp.get("generated_video_paths")
            if isinstance(resp.get("generated_video_paths"), list)
            else [],
            "generated_image_sources": resp.get("generated_image_sources")
            if isinstance(resp.get("generated_image_sources"), list)
            else [],
            "video_url": resp.get("video_url") or "",
            "video_source": resp.get("video_source") or "",
            "retry_count": resp.get("retry_count"),
            "retry_exhausted": resp.get("retry_exhausted"),
        }
        cache_cleanup = resp.get("cache_cleanup")
        if isinstance(cache_cleanup, dict):
            slim_cleanup: Dict[str, Any] = {}
            for key in (
                "limit_bytes",
                "limit_count",
                "initial_total_bytes",
                "initial_total_count",
                "total_bytes",
                "total_count",
                "deleted_count",
                "would_delete_count",
                "deleted_bytes",
                "would_delete_bytes",
            ):
                value = cache_cleanup.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    slim_cleanup[key] = value
            for key in ("deleted", "would_delete"):
                value = cache_cleanup.get(key)
                if isinstance(value, list):
                    slim_cleanup[key] = [str(item).strip()[:300] for item in value if str(item).strip()][:100]
            if slim_cleanup:
                out["response_data"]["cache_cleanup"] = slim_cleanup

    if isinstance(out.get("generated_image_sources"), list):
        out["generated_image_sources"] = _preserve_media_sources(out["generated_image_sources"])

    attempts_in = out.get("attempts")
    if not isinstance(attempts_in, list) and isinstance(resp, dict):
        attempts_in = resp.get("attempts")
    slim_attempts: List[Dict[str, Any]] = []
    if isinstance(attempts_in, list):
        for item in attempts_in:
            if not isinstance(item, dict):
                continue
            slim_attempts.append(
                {
                    "attempt": item.get("attempt"),
                    "channel": item.get("channel") or "",
                    "label": item.get("label") or item.get("model") or item.get("channel") or "",
                    "model": item.get("model") or "",
                    "success": item.get("success"),
                    "error": _truncate_text(item.get("error"), 800),
                    "error_user_message": _truncate_text(item.get("error_user_message"), 300),
                    "error_category": item.get("error_category") or "",
                    "elapsed_seconds": item.get("elapsed_seconds"),
                    "timeout": item.get("timeout"),
                    "retry_count": item.get("retry_count"),
                    "retry_action": item.get("retry_action") or "",
                    "retry_reason": item.get("retry_reason") or "",
                    "retry_after_seconds": item.get("retry_after_seconds"),
                }
            )
    if slim_attempts:
        out["attempts"] = slim_attempts

    fr = out.get("failure_reasons")
    if isinstance(fr, list):
        slim_fr: List[Dict[str, Any]] = []
        for item in fr:
            if not isinstance(item, dict):
                continue
            slim_fr.append(
                {
                    "label": item.get("label") or "",
                    "error": _truncate_text(item.get("error"), 500),
                    "error_user_message": _truncate_text(item.get("error_user_message"), 300),
                    "error_category": item.get("error_category") or "",
                    "elapsed_seconds": item.get("elapsed_seconds"),
                    "attempt": item.get("attempt"),
                }
            )
        out["failure_reasons"] = slim_fr
    return out


def summarize_record_for_list(record: Dict[str, Any]) -> Dict[str, Any]:
    """Lightweight monitor-list row; full body stays on detail endpoint."""
    if not isinstance(record, dict):
        return {}
    attempts = record.get("attempts") if isinstance(record.get("attempts"), list) else []
    failed_attempts = [a for a in attempts if isinstance(a, dict) and a.get("success") is False]
    fr = record.get("failure_reasons") if isinstance(record.get("failure_reasons"), list) else []
    if not fr and failed_attempts:
        fr = [
            {
                "label": a.get("label") or a.get("model") or "",
                "error": a.get("error") or "",
                "error_user_message": a.get("error_user_message") or "",
                "error_category": a.get("error_category") or "",
                "elapsed_seconds": a.get("elapsed_seconds"),
            }
            for a in failed_attempts
        ]
    request_data = record.get("request_data") if isinstance(record.get("request_data"), dict) else {}
    channel = str(record.get("channel") or request_data.get("channel") or "").strip()
    model = str(record.get("model") or request_data.get("model") or "").strip()
    attempts_for_route = attempts
    if not attempts_for_route:
        response = record.get("response_data") if isinstance(record.get("response_data"), dict) else {}
        attempts_for_route = response.get("attempts") if isinstance(response.get("attempts"), list) else []
    for attempt in reversed(attempts_for_route):
        if not isinstance(attempt, dict):
            continue
        if attempt.get("success") or not channel or not model:
            channel = channel or str(attempt.get("channel") or "").strip()
            model = model or str(attempt.get("model") or "").strip()
        if channel and model and attempt.get("success"):
            break
    if (not channel or not model) and record.get("used_model"):
        label_channel, separator, label_model = str(record.get("used_model") or "").partition("/")
        if separator:
            channel = channel or label_channel.strip()
            model = model or label_model.strip()
        else:
            model = model or str(record.get("used_model") or "").strip()
    return {
        "id": record.get("id"),
        "task_id": str(record.get("task_id") or "").strip(),
        "time": record.get("time"),
        "source": record.get("source"),
        "source_label": record.get("source_label"),
        "success": record.get("success"),
        "status": record.get("status") or ("succeeded" if record.get("success") else "failed"),
        "generation_success": record.get("generation_success"),
        "delivery_success": record.get("delivery_success"),
        "media_type": record.get("media_type") or "image",
        "channel": channel,
        "model": model,
        "used_model": record.get("used_model") or "",
        "elapsed_seconds": record.get("elapsed_seconds"),
        "group_id": record.get("group_id") or "",
        "user_id": record.get("user_id") or "",
        "chat_type": record.get("chat_type") or "",
        "count": record.get("count"),
        "md5": str(record.get("md5") or "").strip().lower(),
        "reference_images": record.get("reference_images"),
        "error": _truncate_text(record.get("error") or record.get("failure_reason"), 300),
        "failure_reason": _truncate_text(record.get("failure_reason") or record.get("error"), 300),
        "failure_reasons": fr[:12],
        "attempt_count": len(attempts),
        "failed_attempt_count": len(failed_attempts),
        "original_prompt": _truncate_text(record.get("original_prompt"), 240),
        "request_prompt": _truncate_text(record.get("request_prompt") or record.get("prompt"), 240),
        "request_image_paths": record.get("request_image_paths")
        if isinstance(record.get("request_image_paths"), list)
        else [],
        "generated_image_paths": record.get("generated_image_paths")
        if isinstance(record.get("generated_image_paths"), list)
        else [],
        "generated_video_paths": record.get("generated_video_paths")
        if isinstance(record.get("generated_video_paths"), list)
        else [],
        "favorite": bool(record.get("favorite") or record.get("is_favorite")),
        "pinned": bool(record.get("pinned") or record.get("is_pinned")),
        "tags": [str(item).strip() for item in (record.get("tags") or []) if str(item).strip()][:30],
        "note": str(record.get("note") or record.get("asset_note") or "")[:2000],
        "has_detail": True,
    }


def _audit_bool_value(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "y", "1", "allow", "allowed", "pass", "passed", "safe", "ok", "通过", "允许", "安全"}:
        return True
    if text in {"false", "no", "n", "0", "deny", "denied", "block", "blocked", "unsafe", "violation", "risk", "拒绝", "不通过", "违规", "不安全"}:
        return False
    return None


def parse_audit_response_text(text: str) -> Tuple[bool, str]:
    raw = str(text or "").strip()
    if not raw:
        return False, "审核模型返回为空"

    fenced = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", raw, flags=re.I)
    json_text = fenced.group(1).strip() if fenced else raw
    obj: Any = None
    try:
        obj = json.loads(json_text)
    except Exception:
        match = re.search(r"\{[\s\S]*\}", json_text)
        if match:
            try:
                obj = json.loads(match.group(0))
            except Exception:
                obj = None

    if isinstance(obj, dict):
        reason = str(obj.get("reason") or obj.get("message") or obj.get("detail") or "").strip()
        positive_keys = ("allow", "allowed", "pass", "passed", "safe", "is_safe", "approved")
        negative_keys = ("deny", "denied", "block", "blocked", "unsafe", "is_unsafe", "violation", "violated", "risk", "has_risk", "flagged")
        verdict_keys = ("result", "status", "decision", "verdict", "label")
        for key in negative_keys:
            if key in obj:
                value = _audit_bool_value(obj.get(key))
                if value is True:
                    return False, reason
        for key in positive_keys:
            if key in obj:
                value = _audit_bool_value(obj.get(key))
                if value is False:
                    return False, reason
        for key in positive_keys:
            if key in obj:
                value = _audit_bool_value(obj.get(key))
                if value is True:
                    return True, reason
        for key in negative_keys:
            if key in obj:
                value = _audit_bool_value(obj.get(key))
                if value is False:
                    return True, reason
        for key in verdict_keys:
            if key in obj:
                value = _audit_bool_value(obj.get(key))
                if value is not None:
                    return value, reason
        return False, f"无法判定审核结果: {json_text[:120]}"

    low = json_text.lower().strip()
    if re.fullmatch(r"(false|no|deny|denied|unsafe|violation|risk)", low):
        return False, json_text[:120]
    if re.fullmatch(r"(true|yes|allow|allowed|pass|passed|safe|ok)", low):
        return True, json_text[:120]
    if (
        re.search(r"\b(?:allow|allowed|pass|passed|safe|approved)\s*[:=]\s*(?:false|no|0)\b", low)
        or re.search(r"\b(?:deny|denied|block|blocked|unsafe|violation|risk|flagged)\s*[:=]\s*(?:true|yes|1)\b", low)
        or "拒绝" in json_text
        or "不通过" in json_text
        or "违规" in json_text
        or "不安全" in json_text
    ):
        return False, json_text[:120]
    if (
        re.search(r"\b(?:allow|allowed|pass|passed|safe|approved)\s*[:=]\s*(?:true|yes|1)\b", low)
        or re.search(r"\b(?:deny|denied|block|blocked|unsafe|violation|risk|flagged)\s*[:=]\s*(?:false|no|0)\b", low)
        or "通过" in json_text
        or "允许" in json_text
        or "安全" in json_text
    ):
        return True, json_text[:120]
    return False, f"无法判定审核结果: {json_text[:120]}"


def extract_image_urls(text: str) -> List[str]:
    raw = decode_html_entities(text)
    result: List[str] = []

    for match in re.finditer(r"https?://[^\s\"'<>，。！？、；：)\]}]+", raw, flags=re.I):
        url = match.group(0).strip().rstrip("，。！？、；：)]}>")
        if looks_like_image_url(url):
            result.append(url)

    for match in re.finditer(r"data:image/[a-zA-Z0-9.+-]+(?:;[^,\s\"'<>;]*)*;base64,[A-Za-z0-9+/=_-]+", raw, flags=re.I):
        result.append(match.group(0))

    for match in re.finditer(r"base64://[A-Za-z0-9+/=_-]+", raw, flags=re.I):
        result.append(match.group(0))

    return unique(result)


def unique(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


async def fetch_image_source(
    source: str,
    session: aiohttp.ClientSession,
    max_bytes: int,
    timeout: int = 30,
) -> Optional[Tuple[bytes, str]]:
    text = decode_html_entities(str(source or "").strip())
    if not text:
        return None

    try:
        # AstrBot Image.file may be a file URI while Image.path is empty.
        # Resolve it before checking local storage so QQ's original bytes are
        # used instead of downloading a potentially transcoded remote URL.
        if text.lower().startswith("file://"):
            parsed = urlsplit(text)
            if parsed.netloc and parsed.netloc.lower() not in {"", "localhost"}:
                return None
            text = unquote(parsed.path)
            if not text:
                return None
        lowered = text.lower()
        if lowered.startswith(("data:image/", "base64://")):
            data, mime = data_url_to_bytes(text)
            if data and len(data) <= max_bytes:
                return data, mime
            return None

        if os.path.exists(text) and os.path.isfile(text):
            if os.path.getsize(text) > max_bytes:
                return None
            with open(text, "rb") as file:
                data = file.read()
            if not data or not looks_like_image_bytes(data):
                return None
            return data, detect_mime_by_bytes(data)

        if not lowered.startswith(("http://", "https://")):
            return None

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://im.qq.com/",
        }
        request_timeout = aiohttp.ClientTimeout(total=max(1, int(timeout or 30)))
        async with session.get(text, headers=headers, timeout=request_timeout, allow_redirects=True) as response:
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
            if not looks_like_image_bytes(data):
                return None
            return data, detect_mime_by_bytes(data)
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError, binascii.Error, ValueError):
        return None


def extract_event_text(event: Any) -> str:
    text = getattr(event, "message_str", "") or getattr(getattr(event, "message_obj", None), "message_str", "")
    if text:
        return str(text).strip()
    message = getattr(getattr(event, "message_obj", None), "message", []) or []
    parts = []
    for comp in message:
        if type(comp).__name__ == "Plain":
            parts.append(str(getattr(comp, "text", "") or ""))
    if parts:
        return "".join(parts).strip()
    return str(getattr(event, "message_obj", "") or "").strip()


def extract_command_message(event: Any, command: Any, fallback: str = "") -> str:
    text = extract_event_text(event)
    if not text:
        return fallback.strip()
    commands = [command] if isinstance(command, str) else [str(item) for item in command if str(item).strip()]
    for item in commands:
        # Some adapters retain the bot @mention in message_str even though the
        # command dispatcher has already selected this handler.
        pattern = rf"^\s*(?:@\S+\s+)?[/!！.]?{re.escape(item)}(?:\s+([\s\S]*))?$"
        match = re.match(pattern, text)
        if match:
            return (match.group(1) or "").strip()
    return fallback.strip()


def extract_image_sources_from_event(event: Any, include_at_avatar: bool = False) -> List[str]:
    images: List[str] = []
    visited = set()

    def search(obj: Any) -> None:
        if obj is None or id(obj) in visited:
            return
        visited.add(id(obj))
        obj_type = type(obj).__name__

        if obj_type == "Image":
            # ``path`` is an empty declared field on AstrBot Image objects;
            # prefer a usable file/file URI before falling back to ``url``.
            candidates: List[str] = []
            for attr in ("path", "file", "file_path", "url"):
                try:
                    value = getattr(obj, attr, None)
                except Exception:
                    value = None
                text = str(value or "").strip()
                if text and text not in candidates:
                    candidates.append(text)
            local = [
                value
                for value in candidates
                if not value.lower().startswith(("http://", "https://"))
            ]
            if local:
                images.append(local[0])
            elif candidates:
                images.append(candidates[0])
            return

        if obj_type == "Plain":
            text = str(getattr(obj, "text", "") or "")
            images.extend(extract_image_urls(text))
            return

        if include_at_avatar and obj_type in {"At", "AtSomeone"}:
            qq = str(getattr(obj, "qq", getattr(obj, "id", "")) or "").strip()
            if qq and qq != "all":
                images.append(f"https://q4.qlogo.cn/headimg_dl?dst_uin={qq}&spec=640")
            return

        if isinstance(obj, str):
            images.extend(extract_image_urls(obj))
            return

        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                search(item)
            return

        attrs: List[str] = []
        if hasattr(obj, "__dict__"):
            attrs.extend(vars(obj).keys())
        if hasattr(obj, "__slots__"):
            attrs.extend(getattr(obj, "__slots__", []))
        blocked = {"context", "star", "bot", "provider", "session", "config", "plugin_config"}
        for key in set(attrs) - blocked:
            try:
                search(getattr(obj, key))
            except Exception:
                continue

    message_obj = getattr(event, "message_obj", None)
    search(message_obj)
    quote_obj = getattr(message_obj, "quote", None)
    if quote_obj:
        search(quote_obj)
    search(getattr(event, "message", None))
    search(getattr(event, "raw_message", None))

    return unique(images)


def event_user_id(event: Any) -> str:
    for method_name in ("get_sender_id", "get_user_id"):
        method = getattr(event, method_name, None)
        if callable(method):
            try:
                value = str(method() or "").strip()
                if value:
                    return value
            except Exception:
                pass
    message_obj = getattr(event, "message_obj", None)
    for obj in (event, message_obj):
        for attr in ("user_id", "sender_id", "sender", "qq"):
            value = getattr(obj, attr, None)
            if value:
                return str(value)
    return ""


def event_group_id(event: Any) -> str:
    message_obj = getattr(event, "message_obj", None)
    for obj in (event, message_obj):
        for attr in ("group_id", "group", "room_id", "channel_id"):
            value = getattr(obj, attr, None)
            if value:
                return str(value)
    for method_name in ("get_session_id",):
        method = getattr(event, method_name, None)
        if callable(method):
            try:
                text = str(method() or "")
                group_id = extract_group_id_from_text(text)
                if group_id:
                    return group_id
            except Exception:
                pass
    return ""


def extract_group_id_from_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = text.split(":", 2)
    if len(parts) == 3 and "group" in parts[1].lower():
        return parts[2].strip()
    if "group" not in text.lower() and "群" not in text:
        return ""
    labelled_match = re.search(r"(?:group_id|group|群)[=:_\-\s]+(\d+)", text, flags=re.I)
    if labelled_match:
        return labelled_match.group(1)
    matches = re.findall(r"\d+", text)
    return matches[0] if matches else ""
