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
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

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
    path_keys = ("request_image_paths", "generated_image_paths", "image_paths")

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
    candidates: List[Tuple[int, float, str]] = []
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
            candidates.append((priority, mtime, path))
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return [path for _, _, path in candidates]


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
        pattern = rf"^\s*[/!！.]?{re.escape(item)}(?:\s+([\s\S]*))?$"
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
            path = getattr(obj, "path", getattr(obj, "file", getattr(obj, "file_path", None)))
            url = getattr(obj, "url", None)
            value = path if path and not str(path).startswith(("http://", "https://")) else url or path
            if value:
                images.append(str(value))
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
