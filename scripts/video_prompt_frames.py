#!/usr/bin/env python3
"""Download parsed media, extract video frames or keep images, and infer a prompt.

The parser is loaded from the xiuxian entertainment module used by the user's
NoneBot installation.  This is intentionally a standalone operator script: it
does not change AstrBot configuration or generation records.

Examples:
  python scripts/video_prompt_frames.py "https://v.douyin.com/..."
  python scripts/video_prompt_frames.py "https://cdn.example/image.jpg"
  VISION_API_KEY=... python scripts/video_prompt_frames.py URL \
      --vision-base-url https://api.openai.com/v1 --vision-model gpt-4o
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_XIUXIAN_ROOT = Path(
    "/data/user/0/com.termux/files/home/nonebot_plugin_xiuxian_2_pmv/"
    "nonebot_plugin_xiuxian_2/xiuxian"
)
DEFAULT_OUTPUT = Path("./video_prompt_frames")
URL_RE = re.compile(r"https?://[^\s<>\"'，。！？、；：（）【】《》]+", re.I)
VIDEO_SUFFIXES = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".ts")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")


def _stub_nonebot() -> None:
    """Allow native.py to load without starting a NoneBot application."""
    if "nonebot.log" in sys.modules:
        return

    class Logger:
        def debug(self, *args: Any, **kwargs: Any) -> None:
            return None

        info = debug
        warning = debug
        error = debug
        exception = debug

    nonebot = types.ModuleType("nonebot")
    log = types.ModuleType("nonebot.log")
    log.logger = Logger()
    nonebot.log = log  # type: ignore[attr-defined]
    sys.modules["nonebot"] = nonebot
    sys.modules["nonebot.log"] = log


def _load_native(root: Path):
    """Load media_parser.native with a small package shim."""
    native_path = root / "xiuxian_entertainment" / "media_parser" / "native.py"
    if not native_path.is_file():
        raise FileNotFoundError(f"未找到媒体解析器: {native_path}")

    _stub_nonebot()
    package_root = root.parent
    package_paths = {
        "nonebot_plugin_xiuxian_2": package_root,
        "nonebot_plugin_xiuxian_2.xiuxian": root,
        "nonebot_plugin_xiuxian_2.xiuxian.xiuxian_utils": root / "xiuxian_utils",
        "nonebot_plugin_xiuxian_2.xiuxian.xiuxian_entertainment": root / "xiuxian_entertainment",
        "nonebot_plugin_xiuxian_2.xiuxian.xiuxian_entertainment.media_parser": root / "xiuxian_entertainment" / "media_parser",
    }
    for name, path in package_paths.items():
        module = sys.modules.get(name) or types.ModuleType(name)
        module.__path__ = [str(path)]  # type: ignore[attr-defined]
        module.__package__ = name
        sys.modules[name] = module

    config_name = "nonebot_plugin_xiuxian_2.xiuxian.xiuxian_config"
    if config_name not in sys.modules:
        config = types.ModuleType(config_name)

        class XiuConfig:
            custom_proxy_enabled = False
            custom_proxy = ""

        config.XiuConfig = XiuConfig  # type: ignore[attr-defined]
        sys.modules[config_name] = config

    module_name = "nonebot_plugin_xiuxian_2.xiuxian.xiuxian_entertainment.media_parser.native"
    spec = importlib.util.spec_from_file_location(module_name, native_path)
    if not spec or not spec.loader:
        raise ImportError(f"无法加载媒体解析器: {native_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _extract_urls(text: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for match in URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;!?)]}")
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _parse_media(text: str, root: Path) -> tuple[list[dict[str, Any]], str | None]:
    urls = _extract_urls(text)
    if not urls:
        return [], "输入中没有 http(s) 链接"
    try:
        native = _load_native(root)
        parsed = native.parse_text_native(text)
        if isinstance(parsed, list) and parsed:
            return [item for item in parsed if isinstance(item, dict)], None
        return [], "媒体解析器没有返回结果"
    except Exception as exc:
        # A direct mp4 URL remains usable if the optional platform parser cannot
        # import because the host installation lacks a NoneBot dependency.
        return [{"source_url": urls[0], "url": urls[0], "video_urls": [], "image_urls": []}], str(exc)


def _is_direct_video_url(url: str) -> bool:
    path = (urlparse(url).path or "").lower()
    return path.endswith(VIDEO_SUFFIXES)


def _is_direct_image_url(url: str) -> bool:
    path = (urlparse(url).path or "").lower()
    return path.endswith(IMAGE_SUFFIXES)


def _referer_for(url: str, fallback: str = "") -> str:
    if fallback:
        return fallback
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return ""


def _guess_mime(url: str, content_type: str = "", default: str = "application/octet-stream") -> str:
    """Prefer the server MIME, falling back to the URL suffix."""
    value = (content_type or "").split(";", 1)[0].strip().lower()
    if "/" in value and value != "application/octet-stream":
        return value
    guessed, _ = mimetypes.guess_type(urlparse(url).path)
    return guessed or default


def _download(
    url: str,
    dest: Path,
    *,
    referer: str,
    timeout: int,
    max_bytes: int,
    media_type: str = "video",
) -> str:
    """Download one media resource and return its MIME type."""
    label = "图片" if media_type == "image" else "视频"
    if url.startswith("file://"):
        source = Path(urlparse(url).path)
        if not source.is_file():
            raise FileNotFoundError(source)
        shutil.copyfile(source, dest)
        return _guess_mime(url)
    if not url.startswith(("http://", "https://")):
        source = Path(url)
        if source.is_file():
            shutil.copyfile(source, dest)
            return _guess_mime(url)
        raise ValueError(f"不支持的{label}地址: {url}")

    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("下载需要 requests，请先安装项目 requirements.txt") from exc

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
            "Mobile/15E148 Safari/604.1"
        ),
        "Accept": "image/*,*/*;q=0.8" if media_type == "image" else "video/*,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=True) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "")
                content_length = response.headers.get("Content-Length")
                if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                    raise RuntimeError(f"{label}超过 {max_bytes // (1024 * 1024)}MB")
                size = 0
                with part.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > max_bytes:
                            raise RuntimeError(f"{label}超过 {max_bytes // (1024 * 1024)}MB")
                        handle.write(chunk)
        if part.stat().st_size < 1024:
            raise RuntimeError(f"{label}下载内容过小")
        mime = _guess_mime(url, content_type)
        if media_type == "image" and not mime.startswith("image/"):
            raise RuntimeError(f"下载内容不是图片（Content-Type: {mime}）")
        part.replace(dest)
        return mime
    except Exception:
        part.unlink(missing_ok=True)
        raise


def _run_checked(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"缺少命令 {command[0]}，请安装 ffmpeg（含 ffprobe）") from exc
    except subprocess.CalledProcessError as exc:
        # Some Termux builds load its libplacebo before the Android libc++
        # implementation. Retry only ffmpeg tools with the compatible order.
        tool = Path(command[0]).name
        if tool in {"ffmpeg", "ffprobe"} and "cannot locate symbol" in (exc.stderr or ""):
            system_lib = Path("/system/lib64")
            if system_lib.is_dir():
                env = os.environ.copy()
                current = env.get("LD_LIBRARY_PATH", "")
                termux_lib = Path("/data/data/com.termux/files/usr/lib")
                search = [str(system_lib)]
                if termux_lib.is_dir():
                    search.append(str(termux_lib))
                if current:
                    search.append(current)
                env["LD_LIBRARY_PATH"] = os.pathsep.join(search)
                try:
                    return subprocess.run(command, check=True, capture_output=True, text=True, env=env)
                except FileNotFoundError as retry_exc:
                    raise RuntimeError(f"缺少命令 {command[0]}，请安装 ffmpeg（含 ffprobe）") from retry_exc
                except subprocess.CalledProcessError as retry_exc:
                    exc = retry_exc
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"命令失败: {command[0]}") from exc


def _video_duration(video: Path) -> float:
    result = _run_checked([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video),
    ])
    try:
        duration = float((result.stdout or "").strip())
    except ValueError:
        duration = 0.0
    if duration <= 0:
        raise RuntimeError("无法读取视频时长")
    return duration


def _extract_frames(video: Path, output: Path) -> tuple[float, list[dict[str, Any]]]:
    duration = _video_duration(video)
    output.mkdir(parents=True, exist_ok=True)
    frames: list[dict[str, Any]] = []
    for index, fraction in enumerate((0.15, 0.50, 0.85), start=1):
        timestamp = max(0.0, min(duration, duration * fraction))
        frame = output / f"frame_{index:02d}_{timestamp:.2f}s.jpg"
        _run_checked([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{timestamp:.3f}", "-i", str(video), "-frames:v", "1",
            "-q:v", "2", str(frame),
        ])
        if not frame.is_file() or frame.stat().st_size == 0:
            raise RuntimeError(f"抽帧失败: {frame.name}")
        frames.append({"index": index, "timestamp": round(timestamp, 3), "path": str(frame)})
    return duration, frames


def _vision_prompt(
    assets: list[dict[str, Any]],
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: int,
    media_type: str = "video",
) -> str:
    if not base_url or not api_key or not model:
        return ""
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("视觉反推需要 requests") from exc
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    if media_type == "video":
        instruction = (
            "请综合这段视频在三个时间点的画面，反推出可用于文生图/图生图的中文提示词。"
        )
    else:
        instruction = "请根据提供的图片反推出可用于文生图/图生图的中文提示词。"
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": instruction + (
            "只描述画面中能确认的主体、服装、姿态、动作、环境、构图、镜头、光线和风格；"
            "不猜测不可见内容，不添加画面中没有的限制；不要输出模式标签、JSON、Markdown 或负面提示词。"
            "只输出一段具体的造型与画面描述，后续会套用固定的自拍/COS构图格式。"
        ),
    }]
    for asset in assets:
        raw = Path(asset["path"]).read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        mime = str(asset.get("mime_type") or "image/jpeg")
        if not mime.startswith("image/"):
            mime = "image/jpeg"
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    response = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.2,
            "max_tokens": 1200,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    value = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    if isinstance(value, list):
        value = "".join(str(part.get("text") or "") for part in value if isinstance(part, dict))
    return str(value or "").strip()


def _clean_prompt_text(value: str) -> str:
    """Remove common LLM wrappers before embedding the description."""
    text = str(value or "").strip()
    fenced = re.match(r"^```(?:\w+)?\s*([\s\S]*?)\s*```$", text)
    if fenced:
        text = fenced.group(1).strip()
    text = re.sub(r"^(?:提示词|Prompt|prompt)\s*[:：]\s*", "", text)
    text = re.sub(r"^【(?:自拍|他拍)(?:\s*/\s*看看COS)?模式】\s*", "", text)
    return re.sub(r"\s+", " ", text).strip(" 。")


def _format_manual_prompt(prompt: str, *, media_type: str, prompt_format: str) -> str:
    """Wrap vision details in the same compact structure as `/看看COS`."""
    details = _clean_prompt_text(prompt)
    if not details or prompt_format == "plain":
        return details
    source = "三个视频画面" if media_type == "video" else "图片"
    return (
        "【自拍 / 看看COS模式】"
        "展示 AI 现在的样子，但本次强制换装为反推得到的整套 COS 造型。"
        "竖屏手机近景半身自拍：可对镜，但拍胸像到腰线，不要展会式全身棚拍；"
        "手机可出现在镜中；不要第一人称伸手自拍，不要手臂挡脸挡衣服。"
        "脸型五官保持当前 AI 形象，不要换成别人的脸；"
        "假发颜色、发型和发饰可按本次 COS 造型完整替换。"
        f"根据{source}确认的造型细节：{details}。"
        "服装颜色、层数、材质、配饰、鞋履等结构尽量齐全还原；"
        "构图完整带上腰线，画面干净得体。"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="解析媒体链接，下载视频抽取 3 帧或直接使用图片反推提示词")
    parser.add_argument("text", help="媒体分享链接、直链，或包含链接的文本")
    parser.add_argument("--output", "--out", default=str(DEFAULT_OUTPUT), help="输出目录")
    parser.add_argument("--xiuxian-root", default=os.getenv("XIUXIAN_ROOT", str(DEFAULT_XIUXIAN_ROOT)))
    parser.add_argument("--referer", default="", help="下载时的 Referer（不填则使用解析落地页/视频域名）")
    parser.add_argument("--timeout", type=int, default=90, help="解析/下载/视觉请求超时秒数")
    parser.add_argument("--max-mb", type=int, default=512, help="允许下载的最大媒体大小")
    parser.add_argument("--max-images", type=int, default=6, help="最多下载并发送给视觉模型的图片数")
    parser.add_argument("--use-ytdlp", action="store_true", help="无直链或下载失败时使用 yt-dlp")
    parser.add_argument("--vision-base-url", default=os.getenv("VISION_API_URL", ""), help="OpenAI 兼容 API 根地址")
    parser.add_argument("--vision-api-key", default=os.getenv("VISION_API_KEY", ""), help="视觉模型 API Key")
    parser.add_argument("--vision-model", default=os.getenv("VISION_MODEL", ""), help="视觉模型名称")
    parser.add_argument(
        "--prompt-format",
        choices=("cos", "plain"),
        default="cos",
        help="输出提示词格式：cos 为可直接手测的看看COS结构，plain 为视觉模型原始描述",
    )
    args = parser.parse_args()
    output = Path(args.output).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"input": args.text, "output_dir": str(output), "metas": []}

    metas, parser_error = _parse_media(args.text, Path(args.xiuxian_root).expanduser())
    result["parser_error"] = parser_error
    result["metas"] = [
        {key: meta.get(key) for key in ("platform", "title", "author", "source_url", "url", "error", "video_urls", "image_urls")}
        for meta in metas
    ]
    video_candidates: list[tuple[str, str]] = []
    image_candidates: list[tuple[str, str]] = []
    for meta in metas:
        referer = str(meta.get("url") or meta.get("source_url") or "")
        for url in meta.get("video_urls") or []:
            if isinstance(url, str) and url.startswith("http"):
                video_candidates.append((url, referer))
        for url in meta.get("image_urls") or []:
            if isinstance(url, str) and url.startswith("http"):
                image_candidates.append((url, referer))
        source = str(meta.get("source_url") or meta.get("url") or "")
        if source.startswith("http") and _is_direct_video_url(source):
            video_candidates.append((source, referer))
        if source.startswith("http") and _is_direct_image_url(source):
            image_candidates.append((source, referer))
    video_candidates = list(dict.fromkeys(video_candidates))
    image_candidates = list(dict.fromkeys(image_candidates))[: max(1, args.max_images)]
    video = output / "video.mp4"
    # Never reuse a video left by an earlier invocation when all new
    # candidates fail.
    video.unlink(missing_ok=True)
    download_error: str | None = None
    for url, parsed_referer in video_candidates:
        try:
            _download(
                url,
                video,
                referer=args.referer or _referer_for(parsed_referer or url),
                timeout=max(5, args.timeout),
                max_bytes=max(1, args.max_mb) * 1024 * 1024,
                media_type="video",
            )
            result["video_url"] = url
            result["video_path"] = str(video)
            break
        except Exception as exc:
            download_error = f"{type(exc).__name__}: {exc}"
    else:
        source_urls = _extract_urls(args.text)
        if args.use_ytdlp and source_urls:
            try:
                _run_checked(["yt-dlp", "--no-playlist", "-f", "best[ext=mp4]/best", "-o", str(video), source_urls[0]])
                result["video_url"] = source_urls[0]
                result["video_path"] = str(video)
            except Exception as exc:
                download_error = f"{type(exc).__name__}: {exc}"
    assets: list[dict[str, Any]] = []
    media_type = "video"
    if video.is_file():
        try:
            duration, frames = _extract_frames(video, output)
            result["media_type"] = "video"
            result["duration_seconds"] = round(duration, 3)
            result["frames"] = frames
            assets = frames
        except Exception as exc:
            download_error = f"视频处理失败: {type(exc).__name__}: {exc}"
            video.unlink(missing_ok=True)

    if not assets:
        media_type = "image"
        image_assets: list[dict[str, Any]] = []
        image_errors: list[str] = []
        for index, (url, parsed_referer) in enumerate(image_candidates, start=1):
            suffix = Path(urlparse(url).path).suffix.lower()
            if suffix not in IMAGE_SUFFIXES:
                suffix = ".img"
            dest = output / f"image_{index:02d}{suffix}"
            dest.unlink(missing_ok=True)
            try:
                mime = _download(
                    url,
                    dest,
                    referer=args.referer or _referer_for(parsed_referer or url),
                    timeout=max(5, args.timeout),
                    max_bytes=max(1, args.max_mb) * 1024 * 1024,
                    media_type="image",
                )
                image_assets.append({"index": index, "url": url, "path": str(dest), "mime_type": mime})
            except Exception as exc:
                image_errors.append(f"{url}: {type(exc).__name__}: {exc}")
                dest.unlink(missing_ok=True)
        if image_assets:
            result["media_type"] = "image"
            result["images"] = image_assets
            result["image_paths"] = [item["path"] for item in image_assets]
            result["image_urls"] = [item["url"] for item in image_assets]
            if len(image_assets) == 1:
                result["image_path"] = image_assets[0]["path"]
                result["image_url"] = image_assets[0]["url"]
                result["image_mime_type"] = image_assets[0]["mime_type"]
            assets = image_assets
        else:
            result["error"] = "媒体下载失败"
            result["download_error"] = "; ".join(image_errors) or download_error or "没有可用视频或图片直链"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1

    try:
        if args.vision_base_url and args.vision_api_key and args.vision_model:
            raw_prompt = _vision_prompt(
                assets,
                base_url=args.vision_base_url,
                api_key=args.vision_api_key,
                model=args.vision_model,
                timeout=max(5, args.timeout),
                media_type=media_type,
            )
            result["prompt_raw"] = raw_prompt
            result["prompt"] = _format_manual_prompt(
                raw_prompt,
                media_type=media_type,
                prompt_format=args.prompt_format,
            )
            result["prompt_format"] = args.prompt_format
            (output / "prompt.txt").write_text(result["prompt"], encoding="utf-8")
        else:
            result["prompt"] = None
            result["prompt_hint"] = "未配置 --vision-base-url/--vision-api-key/--vision-model，仅完成媒体下载与抽帧"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
