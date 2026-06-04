"""Shared utility helpers."""

from __future__ import annotations

import base64
import asyncio
import binascii
import hashlib
import json
import mimetypes
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
}


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
    if len(b) >= 4 and b[:4] == b"RIFF":
        return "image/webp"
    if len(b) >= 2 and b[:2] == b"BM":
        return "image/bmp"
    return "image/png"


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
    return "png"


def guess_image_content_type(source: str, fallback: str = "image/png") -> str:
    text = str(source or "")
    lowered = text.lower()
    if lowered.startswith("data:"):
        header = text.split(",", 1)[0]
        media_type = header[5:].split(";", 1)[0].strip()
        if media_type.startswith("image/"):
            return normalize_image_mime(media_type)
    guessed = mimetypes.guess_type(text)[0] or ""
    if guessed.startswith("image/"):
        return normalize_image_mime(guessed)
    if lowered.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lowered.endswith(".webp"):
        return "image/webp"
    if lowered.endswith(".gif"):
        return "image/gif"
    if lowered.endswith(".bmp"):
        return "image/bmp"
    return fallback


def data_url_to_bytes(input_text: str) -> Tuple[bytes, str]:
    text = str(input_text or "").strip()
    if not text:
        return b"", "image/png"

    match = re.match(r"^data:([^;,]+);base64,([\s\S]+)$", text, flags=re.I)
    if match:
        mime = normalize_image_mime(match.group(1))
        return base64.b64decode(match.group(2), validate=False), mime

    prefix = "base64://"
    if text.startswith(prefix):
        data = base64.b64decode(text[len(prefix) :], validate=False)
        return data, detect_mime_by_bytes(data)

    try:
        data = base64.b64decode(text, validate=False)
        return data, detect_mime_by_bytes(data)
    except binascii.Error:
        return b"", "image/png"


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


def looks_like_image_url(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if value.startswith(("data:image/", "base64://")):
        return True
    lowered = value.lower()
    return lowered.startswith(("http://", "https://")) and (
        any(ext in lowered.split("?", 1)[0] for ext in IMAGE_EXTENSIONS)
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


def extract_image_urls(text: str) -> List[str]:
    raw = decode_html_entities(text)
    result: List[str] = []

    for match in re.finditer(r"https?://[^\s\"'<>，。！？、；：)\]}]+", raw, flags=re.I):
        url = match.group(0).strip().rstrip("，。！？、；：)]}>")
        if looks_like_image_url(url):
            result.append(url)

    for match in re.finditer(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=_-]+", raw):
        result.append(match.group(0))

    for match in re.finditer(r"base64://[A-Za-z0-9+/=_-]+", raw):
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
        if text.startswith(("data:image/", "base64://")):
            data, mime = data_url_to_bytes(text)
            if data and len(data) <= max_bytes:
                return data, mime
            return None

        if os.path.exists(text) and os.path.isfile(text):
            if os.path.getsize(text) > max_bytes:
                return None
            with open(text, "rb") as file:
                data = file.read()
            return data, detect_mime_by_bytes(data)

        if not text.lower().startswith(("http://", "https://")):
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
            content_length = response.headers.get("content-length", "")
            if content_length and int(content_length) > max_bytes:
                return None
            chunks: List[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
            data = b"".join(chunks)
            header_mime = normalize_image_mime(response.headers.get("content-type", ""), "")
            return data, header_mime or detect_mime_by_bytes(data)
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
