"""Generation orchestration with retry and fallback."""

from __future__ import annotations

import asyncio
import re
import time
from typing import List

import aiohttp

from .models import ImageModelTarget
from .providers import ImageGenerateRequest, ImageGenerateResult, create_adapter


IMAGE_RETRY_ATTEMPTS = 3


def should_retry(error_text: str) -> bool:
    return bool(
        re.search(
            r"fetch failed|ECONN|ETIMEDOUT|EPIPE|UND_|aborted|ECONNABORTED|socket|network|timeout|超时|HTTP 5\d\d",
            str(error_text or ""),
            flags=re.I,
        )
    )


async def generate_image_with_fallback(
    targets: List[ImageModelTarget],
    req: ImageGenerateRequest,
    session: aiohttp.ClientSession,
) -> ImageGenerateResult:
    if not targets:
        return ImageGenerateResult(error="未配置生图模型")

    global_timeout = max(10, int(targets[0].timeout or 180))
    deadline = time.monotonic() + global_timeout
    last_error = "未配置生图模型"

    for attempt in range(1, IMAGE_RETRY_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ImageGenerateResult(error=f"生图全局超时（{global_timeout}秒），最后错误: {last_error}")

        target = targets[(attempt - 1) % len(targets)]
        label = target.label
        adapter = create_adapter(target, session)

        try:
            result = await asyncio.wait_for(adapter.generate(req), timeout=max(1, min(target.timeout, int(remaining))))
            if result.images and not result.error:
                result.used_model = label
                return result
            last_error = f"{label}: {result.error or '生成失败'}"
            if not should_retry(last_error):
                return ImageGenerateResult(error=last_error, used_model=label)
        except asyncio.TimeoutError:
            last_error = f"{label}: 请求超时"
        except Exception as exc:
            last_error = f"{label}: {exc}"
            if not should_retry(last_error):
                return ImageGenerateResult(error=last_error, used_model=label)

        if attempt < IMAGE_RETRY_ATTEMPTS:
            wait_seconds = attempt
            if deadline - time.monotonic() <= wait_seconds:
                return ImageGenerateResult(error=f"生图全局超时（{global_timeout}秒），最后错误: {last_error}")
            await asyncio.sleep(wait_seconds)

    return ImageGenerateResult(error=last_error)

