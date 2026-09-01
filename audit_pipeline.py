"""Prompt auditing, image lookup, and prompt translation mixin."""

from __future__ import annotations

import hashlib
from io import BytesIO
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

import aiohttp

try:
    from astrbot.api.event import AstrMessageEvent
except ImportError:
    from astrbot.api.event import AstrMessageEvent

from .models import DEFAULT_CONFIG, ImageModelTarget
from .prompt_translation import parse_prompt_en_response
from .providers import normalize_image_base_url
from .utils import (
    bytes_to_data_url,
    detect_mime_by_bytes,
    parse_audit_response_text,
    redact_sensitive_text,
)


class AuditMixin:
    def _parse_audit_response(self, text: str) -> Tuple[bool, str]:
        return parse_audit_response_text(text)

    def _find_audit_target(self, label: str) -> Optional[ImageModelTarget]:
        value = str(label or "").strip()
        if not value:
            return None
        targets = self.config.get_audit_targets()
        if "/" in value:
            channel_name, model = value.split("/", 1)
            channel_name = channel_name.strip()
            model = model.strip()
            for target in targets:
                if target.channel_name == channel_name and target.model == model:
                    return target
            return None
        for target in targets:
            if target.model == value:
                return target
        return None

    def _record_image_md5(self, record: Mapping[str, Any]) -> str:
        """Return the MD5 of cached image bytes, with legacy metadata fallback."""
        value = str(record.get("md5") or "").strip().lower()
        paths = record.get("generated_image_paths")
        if isinstance(paths, list):
            for path in paths:
                try:
                    loaded = self._load_cache_image_bytes(str(path or ""))
                    if loaded:
                        # The file bytes are authoritative; stale metadata cannot create a match.
                        return hashlib.md5(loaded[0]).hexdigest()
                except Exception:
                    continue
        return value if re.fullmatch(r"[0-9a-f]{32}", value) else ""

    def _image_md5_variants(self, data: bytes) -> List[str]:
        """Return direct and AstrBot JPEG-normalized MD5s for image bytes.

        AstrBot converts quoted non-JPEG images to RGB JPEG (quality 95,
        subsampling 0) before plugin handlers run.  Keep the original cache
        digest authoritative, but also recognize that deterministic transport
        representation when QQ does not expose the original image source.
        """
        if not data:
            return []
        direct = hashlib.md5(data).hexdigest()
        variants = [direct]
        try:
            from PIL import Image as PILImage

            with PILImage.open(BytesIO(data)) as opened:
                image_format = str(opened.format or "").upper()
                image_has_alpha = opened.mode in {"RGBA", "LA"} or (
                    opened.mode == "P" and "transparency" in opened.info
                )
                image_is_animated = bool(
                    getattr(opened, "is_animated", False)
                    or getattr(opened, "n_frames", 1) > 1
                )
                if image_format == "JPEG" or image_has_alpha or image_is_animated:
                    return variants
                converted = opened.convert("RGB")
                try:
                    output = BytesIO()
                    converted.save(output, "JPEG", quality=95, subsampling=0)
                    normalized = hashlib.md5(output.getvalue()).hexdigest()
                finally:
                    converted.close()
            if normalized not in variants:
                variants.append(normalized)
        except Exception:
            # Pillow is supplied by AstrBot, but direct MD5 lookup remains
            # available in minimal/older installations without it.
            pass
        return variants

    def _find_generation_record_by_md5(self, md5: str) -> Optional[Dict[str, Any]]:
        wanted = str(md5 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", wanted):
            return None
        with self._records_lock:
            records = [dict(item) for item in self._records if isinstance(item, dict)]
        for record in records:
            if not record.get("generated_image_paths"):
                continue
            if str(record.get("media_type") or "image").lower() == "video":
                continue
            if self._record_image_md5(record) == wanted:
                record["md5"] = wanted
                return record
            # Legacy batch rows may contain several cached paths but no per-image MD5.
            for path in record.get("generated_image_paths") or []:
                try:
                    loaded = self._load_cache_image_bytes(str(path or ""))
                    if loaded and wanted in self._image_md5_variants(loaded[0]):
                        record["md5"] = wanted
                        return record
                except Exception:
                    continue
        return None

    async def _reverse_image_prompt_with_llm(
        self,
        event: Optional[AstrMessageEvent],
        image: bytes,
    ) -> str:
        """Use the current AstrBot chat LLM to reconstruct a prompt from an image."""
        if event is None:
            raise RuntimeError("当前会话不可用，无法调用 LLM 反推提示词。")
        instruct = (
            "请根据这张图片反推出一个适合图像生成模型使用的中文提示词。"
            "只输出提示词正文，不要解释、不要 Markdown、不要猜测图片来源。"
            "尽量描述主体、构图、视角、姿势、服装、场景、光线、风格和画面比例；"
            "看不清或无法确定的内容不要编造。"
        )
        result = await self._call_text_llm(event, instruct, timeout=30, images=[image])
        cleaned = str(result or "").strip()
        fenced = re.match(r"^```(?:\w+)?\s*([\s\S]*?)\s*```$", cleaned)
        if fenced:
            cleaned = fenced.group(1).strip()
        return cleaned[:6000]

    async def _describe_reference_images_for_generation(
        self,
        event: Optional[AstrMessageEvent],
        images: List[bytes],
    ) -> Tuple[str, Dict[str, Any]]:
        """Describe reference images for a text-only generation request.

        A configured auxiliary target is preferred.  When it is left empty,
        use the current AstrBot LLM, which is the same fallback used by the
        other text features.  Requests without an event still use AstrBot's
        currently selected provider when the runtime context exposes one.
        """
        if not images:
            return "", {"enabled": True, "applied": False, "reason": "no_images"}
        instruct = (
            "请分析参考图片，并输出一段可直接用于图像生成的中文画面描述。"
            "描述主体、外观、服装、姿势、构图、场景、光线和艺术风格；"
            "只输出描述正文，不要解释、不要 Markdown、不要猜测图片来源，"
            "看不清或无法确定的内容不要编造。"
        )
        configured = str(getattr(self.config, "image_ocr_model", "") or "").strip()
        target = self._find_audit_target(configured) if configured else None
        model = ""
        text = ""
        if target is not None:
            text = await self._audit_chat_via_target(target, instruct, images=images)
            model = target.label
        elif event is not None or getattr(self, "context", None) is not None:
            text = await self._call_text_llm(event, instruct, timeout=30, images=images)
            model = "astrbot"
        else:
            raise RuntimeError("已启用图转文，但未配置图转文模型；当前请求没有可用的 LLM 会话。")
        cleaned = str(text or "").strip()
        fenced = re.match(r"^```(?:\w+)?\s*([\s\S]*?)\s*```$", cleaned)
        if fenced:
            cleaned = fenced.group(1).strip()
        cleaned = cleaned[:6000]
        if not cleaned:
            raise RuntimeError("图转文模型没有返回有效描述。")
        return cleaned, {
            "enabled": True,
            "applied": True,
            "model": model,
            "image_count": len(images),
            "description": cleaned,
        }

    async def _audit_chat_via_target(self, target: ImageModelTarget, text: str, images: Optional[List[bytes]] = None) -> str:
        images = images or []
        provider_type = str(target.provider_type or "").lower()
        timeout = aiohttp.ClientTimeout(total=max(10, int(target.timeout or self.config.image_global_timeout or 180)))
        proxy = str(target.proxy or "").strip() or None
        async with aiohttp.ClientSession(trust_env=False) as session:
            if provider_type == "gemini":
                base = normalize_image_base_url(target.base_url) or "https://generativelanguage.googleapis.com"
                base = re.sub(r"/v1beta(?:/.*)?$", "", base.rstrip("/"), flags=re.I)
                model_path = target.model if target.model.startswith("models/") else f"models/{target.model}"
                url = f"{base}/v1beta/{model_path}:generateContent"
                parts: List[Dict[str, Any]] = [{"text": text}]
                for image in images:
                    parts.append({"inline_data": {"mime_type": detect_mime_by_bytes(image), "data": bytes_to_data_url(image, detect_mime_by_bytes(image)).split(",", 1)[-1]}})
                headers = {"Content-Type": "application/json", "Accept": "application/json"}
                if target.api_key:
                    headers["x-goog-api-key"] = target.api_key
                async with session.post(url, json={"contents": [{"parts": parts}]}, headers=headers, timeout=timeout, proxy=proxy) as response:
                    if response.status >= 400:
                        raise RuntimeError(f"审核接口失败: HTTP {response.status} {redact_sensitive_text(await response.text())[:200]}")
                    data = await response.json(content_type=None)
                texts: List[str] = []
                for candidate in data.get("candidates", []) if isinstance(data, dict) else []:
                    content = candidate.get("content") if isinstance(candidate, dict) else {}
                    for part in content.get("parts", []) if isinstance(content, dict) else []:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            texts.append(part["text"])
                return "\n".join(texts).strip()

            base = normalize_image_base_url(target.base_url) or "https://api.openai.com"
            url = f"{base}/v1/chat/completions"
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if target.api_key:
                headers["Authorization"] = f"Bearer {target.api_key}"
            content: Any = [{"type": "text", "text": text}] if images else text
            if images:
                for image in images:
                    content.append({"type": "image_url", "image_url": {"url": bytes_to_data_url(image, detect_mime_by_bytes(image))}})
            payload = {"model": target.model, "messages": [{"role": "user", "content": content}], "stream": False}
            async with session.post(url, json=payload, headers=headers, timeout=timeout, proxy=proxy) as response:
                if response.status >= 400:
                    raise RuntimeError(f"审核接口失败: HTTP {response.status} {redact_sensitive_text(await response.text())[:200]}")
                data = await response.json(content_type=None)
            if isinstance(data, dict):
                choices = data.get("choices")
                if isinstance(choices, list) and choices:
                    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
                    if isinstance(message, dict):
                        content = message.get("content")
                        if isinstance(content, str):
                            return content.strip()
                        if isinstance(content, list):
                            parts = [str(item.get("text") or "") for item in content if isinstance(item, dict)]
                            return "\n".join(part for part in parts if part).strip()
            return ""

    async def _audit_prompt_via_astrbot(
        self,
        event: Optional[AstrMessageEvent],
        text: str,
        images: Optional[List[bytes]] = None,
    ) -> str:
        """Call the currently selected AstrBot LLM for auxiliary work."""
        caller = getattr(self, "_call_text_llm", None)
        if callable(caller):
            return str(
                await caller(event, text, timeout=30, images=images)
            ).strip()
        return ""

    async def _audit_prompt(self, prompt: str, user_id: str = "", event: Optional[AstrMessageEvent] = None) -> Tuple[bool, str]:
        error = self._validate_prompt(prompt, user_id, event)
        if error:
            return False, error
        if self._is_audit_exempt(event, user_id):
            return True, ""
        if not self.config.image_enable_prompt_audit:
            return True, ""

        audit_prompt = self.config.image_prompt_audit_template.replace("{prompt}", str(prompt or ""))
        try:
            target = self._find_audit_target(self.config.image_prompt_audit_model)
            if target:
                text = await self._audit_chat_via_target(target, audit_prompt)
            else:
                text = await self._audit_prompt_via_astrbot(event, audit_prompt)
        except Exception as exc:
            return False, str(exc)
        return self._parse_audit_response(text)

    async def _audit_output_images(self, files: List[str], user_id: str = "", prompt: str = "", event: Optional[AstrMessageEvent] = None) -> Tuple[bool, str]:
        if self._is_audit_exempt(event, user_id):
            return True, ""
        if not self.config.image_enable_output_audit:
            return True, ""
        if not files:
            return False, "没有待审核图片"

        target = self._find_audit_target(self.config.image_output_audit_model)
        images: List[bytes] = []
        for file_path in files:
            with open(file_path, "rb") as handle:
                images.append(handle.read())
        audit_prompt = self.config.image_output_audit_template.replace("{prompt}", str(prompt or ""))
        try:
            if target is not None:
                text = await self._audit_chat_via_target(target, audit_prompt, images=images)
            else:
                text = await self._audit_prompt_via_astrbot(event, audit_prompt, images=images)
        except Exception as exc:
            return False, str(exc)
        return self._parse_audit_response(text)


    def _prompt_en_needed(self, text: str, *, media: str = "image") -> bool:
        """Whether prompt EN translation is enabled and applicable for this text."""
        if media == "video":
            if not bool(getattr(self.config, "image_enable_video_prompt_en", False)):
                return False
        else:
            if not bool(getattr(self.config, "image_enable_image_prompt_en", False)):
                return False
        mode = str(getattr(self.config, "image_prompt_en_mode", "if_cjk") or "if_cjk").strip().lower()
        if mode == "always":
            return True
        # if_cjk (default): only when CJK present
        return bool(re.search(r"[\u3400-\u9fff\uf900-\ufaff]", str(text or "")))

    async def _translate_prompt_to_english(
        self,
        prompt: str,
        *,
        media: str = "image",
        event: Optional[AstrMessageEvent] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Translate generation prompt to English via audit-channel chat model.

        Returns (translated_or_original, meta). Fail-open: on error keep original.
        """
        raw = str(prompt or "").strip()
        meta: Dict[str, Any] = {"enabled": True, "applied": False, "media": media}
        if not raw:
            return raw, meta
        if not self._prompt_en_needed(raw, media=media):
            meta["skipped"] = "not_needed"
            return raw, meta
        template = (
            getattr(self.config, "image_video_prompt_en_template", "")
            if media == "video"
            else getattr(self.config, "image_image_prompt_en_template", "")
        )
        template = str(template or "").strip()
        if not template or "{prompt}" not in template:
            from .models import DEFAULT_CONFIG
            template = str(
                DEFAULT_CONFIG["image"]["video_prompt_en_template"]
                if media == "video"
                else DEFAULT_CONFIG["image"]["image_prompt_en_template"]
            )
        instruct = template.replace("{prompt}", raw)
        model_label = str(getattr(self.config, "image_prompt_en_model", "") or "").strip()
        # Prefer dedicated EN model, else prompt-audit model, else first audit target.
        try:
            target = self._find_audit_target(model_label) if model_label else None
            if target is None and self.config.image_prompt_audit_model:
                target = self._find_audit_target(self.config.image_prompt_audit_model)
            if target is None:
                targets = self.config.get_audit_targets()
                target = targets[0] if targets else None
            text = ""
            if target:
                text = await self._audit_chat_via_target(target, instruct)
                meta["model"] = target.label
            else:
                text = await self._audit_prompt_via_astrbot(event, instruct)
                meta["model"] = "astrbot"
            cleaned = parse_prompt_en_response(text)
            if not cleaned:
                meta["error"] = "translate_parse_failed"
                meta["raw_preview"] = redact_sensitive_text(str(text or ""))[:180]
                return raw, meta  # fail-open: keep original prompt
            meta["applied"] = True
            meta["original_len"] = len(raw)
            meta["translated_len"] = len(cleaned)
            meta["format"] = "json"
            return cleaned, meta
        except Exception as exc:
            meta["error"] = redact_sensitive_text(str(exc))[:200]
            return raw, meta  # fail-open
