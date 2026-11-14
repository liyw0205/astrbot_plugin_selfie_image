"""Quick-draw request preparation, sequential shots and result aggregation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.providers import ImageReference
from ..core.utils import (
    data_url_to_bytes,
    detect_mime_by_bytes,
    image_validation_limits,
    normalize_image_mime,
)
from ..cos.cos_looks import (
    build_cos_third_person_prompt,
    finalize_cos_prompt_for_request,
    looks_like_cos_prompt,
)
from ..features.creative_features import parse_variation_request
from ..prompts.prompt_composition import build_prompt_with_reference_instruction


class WebTestImageWorkflow:
    """Own mutable state for one request while sharing the plugin's generation path."""

    def __init__(
        self,
        plugin: Any,
        payload: Dict[str, Any],
        *,
        appearance_type_for_request: Callable[[Any, str], str],
    ) -> None:
        self.plugin = plugin
        self.payload = payload
        self.appearance_type_for_request = appearance_type_for_request
        self.all_request_paths: List[str] = []

    async def run(self) -> Dict[str, Any]:
        # Preserve the boundary: malformed request/template options fail before
        # the inspectable preflight record, just as at the public entrypoint.
        self._prepare_request()
        try:
            await self._prepare_generation()
            await self._run_shots()
            result = self._summarize_result()
        except Exception as exc:
            self._record_failure(str(exc))
            raise

        if result["success"]:
            result["status"] = "succeeded"
        else:
            result["status"] = "partial_success" if self.succeeded_count and self.failed_count else "failed"
            result["error"] = self.last_error or (
                "任务已取消"
                if self.task_id and self.plugin._task_cancel_requested(self.task_id)
                else "这次没顺好"
            )
        return result

    def _prepare_request(self) -> None:
        plugin, payload = self.plugin, self.payload
        # Queue links are transient; only their durable, public counterparts
        # belong in record_context and the request summary.
        self.task_id = str(payload.get("_task_id") or "").strip()
        self.trace_id = str(payload.get("_trace_id") or "").strip()
        if self.task_id and not self.trace_id:
            try:
                task_snapshot = plugin.get_web_image_task(self.task_id)
            except Exception:
                task_snapshot = None
            if isinstance(task_snapshot, Mapping):
                self.trace_id = str(task_snapshot.get("trace_id") or "").strip()
        self.channel_name = str(payload.get("channel") or "").strip()
        self.model_name = str(payload.get("model") or "").strip()
        self.raw_images = list(payload.get("images") or [])
        if payload.get("image"):
            self.raw_images.append(payload.get("image"))
        self.raw_prompt = str(payload.get("prompt") or "").strip() or "看着镜头自然自拍"
        template_values = payload.get("template_values") if isinstance(payload.get("template_values"), Mapping) else {}
        template_randomize = payload.get("template_randomize") in {True, 1, "1", "true", "yes", "on", "是", "开启"}
        template_result = plugin.render_creative_prompt(
            self.raw_prompt,
            {
                "template_values": template_values,
                "template_randomize": template_randomize,
                "template_pools": payload.get("template_pools"),
                "template_seed": payload.get("template_seed"),
            },
        )
        original_prompt = str(template_result.get("prompt") or self.raw_prompt).strip()
        configured_aspect = str(
            payload.get("aspect_ratio") or plugin.config.image_default_aspect_ratio or "9:16"
        ).strip() or "9:16"
        configured_resolution = str(
            payload.get("resolution") or plugin.config.image_default_resolution or "1K"
        ).strip() or "1K"
        # Prompt framing is applied after expansion so template supplements can
        # override the Web setting, including in COS requests.
        original_prompt, self.aspect, self.resolution = plugin._parse_prompt_options(
            original_prompt, configured_aspect, configured_resolution,
        )
        if looks_like_cos_prompt(original_prompt):
            original_prompt = build_cos_third_person_prompt(original_prompt)
        self.original_prompt, parsed_variation_enabled, parsed_variation_field = parse_variation_request(original_prompt)
        raw_variation_enabled = payload.get("variation_enabled")
        self.variation_enabled = (
            raw_variation_enabled is True
            or raw_variation_enabled in {1, "1", "true", "yes", "on", "是", "开启"}
            or parsed_variation_enabled
        )
        raw_variation_fields = payload.get("variation_fields") or payload.get("vary")
        if isinstance(raw_variation_fields, str):
            variation_fields = [item.strip() for item in re.split(r"[,，\s]+", raw_variation_fields) if item.strip()]
        elif isinstance(raw_variation_fields, (list, tuple, set)):
            variation_fields = [str(item).strip() for item in raw_variation_fields if str(item).strip()]
        else:
            variation_fields = []
        variation_field = parsed_variation_field or (variation_fields[0] if len(variation_fields) == 1 else "")
        normalize_web_count = getattr(plugin, "_normalize_web_image_count", None)
        if callable(normalize_web_count):
            self.requested_count = normalize_web_count(payload.get("count"))
        else:
            try:
                self.requested_count = max(1, min(100, int(payload.get("count") or 1)))
            except (TypeError, ValueError):
                self.requested_count = 1
        self.variation_fields = (
            variation_fields or ([variation_field] if variation_field else ["pose", "scene", "shot"])
        ) if self.variation_enabled else []
        prompt_enhance_raw = payload.get("prompt_enhance", True)
        self.prompt_enhance = not (
            prompt_enhance_raw is False
            or str(prompt_enhance_raw).strip().lower() in {"false", "0", "no", "off", "关闭", "否"}
        )
        self.request_summary = {
            "original_prompt": self.original_prompt,
            "channel": self.channel_name,
            "model": self.model_name,
            "aspect_ratio": self.aspect,
            "resolution": self.resolution,
            "prompt_enhance": self.prompt_enhance,
            "use_selfie_reference": bool(payload.get("use_selfie_reference")),
            "raw_reference_image_count": len(self.raw_images),
            "variation_enabled": self.variation_enabled,
            "variation_fields": self.variation_fields,
            "template": {
                "raw_prompt": self.raw_prompt,
                "values": {str(key): str(value) for key, value in template_values.items()},
                "randomize_missing": template_randomize,
                "randomized": template_result.get("randomized") or {},
                "unresolved": template_result.get("unresolved") or [],
            },
            "requested_count": self.requested_count,
            "count": self.requested_count,
        }
        if self.trace_id:
            self.request_summary["trace_id"] = self.trace_id
        self.retry_record_id = str(payload.get("_retry_record_id") or payload.get("retry_record_id") or "").strip()
        if self.retry_record_id:
            self.request_summary["retry_record_id"] = self.retry_record_id[:128]

    async def _prepare_generation(self) -> None:
        plugin = self.plugin
        plugin._validate_web_test_selection(self.payload)
        self.target = plugin._find_web_retry_target(self.payload, media_type="image")
        if not self.target:
            raise RuntimeError("未找到指定生图模型")

        max_bytes = plugin.config.image_max_image_size_mb * 1024 * 1024
        max_dimension, max_pixels, max_frames = image_validation_limits(plugin.config)
        self.extra_refs: List[ImageReference] = []
        for raw in self.raw_images:
            data, mime = data_url_to_bytes(
                str(raw or ""),
                max_dimension=max_dimension,
                max_pixels=max_pixels,
                max_frames=max_frames,
            )
            if not data:
                continue
            if len(data) > max_bytes:
                raise RuntimeError(f"参考图过大，最大允许 {plugin.config.image_max_image_size_mb}MB")
            self.extra_refs.append(ImageReference(data=data, mime_type=normalize_image_mime(mime or detect_mime_by_bytes(data))))

        self.original_prompt = finalize_cos_prompt_for_request(
            self.original_prompt,
            appearance_type=self.appearance_type_for_request(getattr(plugin, "persona", None), self.raw_prompt),
            has_refs=bool(self.extra_refs) or bool(self.payload.get("use_selfie_reference")),
        )
        self.request_summary["original_prompt"] = self.original_prompt
        self.variation_rows = []
        if self.variation_enabled:
            self.variation_rows = plugin.build_creative_variations(
                self.original_prompt,
                self.requested_count,
                {"variation_fields": self.variation_fields},
            )
        self.prompt, self.refs, self.prompt_en_meta = await self._compose_prompt(self.original_prompt)

    async def _compose_prompt(
        self, action_text: str,
    ) -> Tuple[str, List[ImageReference], Optional[Dict[str, Any]]]:
        plugin = self.plugin
        if not self.prompt_enhance:
            selected_refs = list(self.extra_refs)
            if self.payload.get("use_selfie_reference"):
                persona_ref = plugin._persona_identity_reference()
                if not persona_ref:
                    raise RuntimeError("当前未设置 AI 自拍形象参考图，且未启用 logo 回退；请先上传形象图、开启「无形象图用 logo」，或取消使用自拍形象参考图")
                selected_refs.insert(0, persona_ref)
            return action_text, selected_refs, None
        if self.payload.get("use_selfie_reference"):
            prepared, selected_refs, translation_meta = await plugin._build_selfie_prompt_and_refs_for_event(
                None, action_text, self.extra_refs,
            )
            if not selected_refs:
                raise RuntimeError("当前未设置 AI 自拍形象参考图，请先上传形象图，或取消使用自拍形象参考图")
            return prepared, selected_refs, translation_meta
        user_prompt = action_text
        translation_meta = {"enabled": False, "applied": False, "scope": "user_text_only"}
        if plugin._prompt_en_needed(user_prompt, media="image"):
            translated, translation_meta = await plugin._translate_prompt_to_english(
                user_prompt, media="image", event=None,
            )
            if translation_meta.get("applied") and translated:
                user_prompt = translated
        prepared = build_prompt_with_reference_instruction(
            user_prompt,
            self.extra_refs,
            language="en" if plugin.config.image_enable_image_prompt_en else "zh",
        )
        return prepared, self.extra_refs, translation_meta

    async def _run_shots(self) -> None:
        plugin = self.plugin
        self.all_paths: List[str] = []
        self.all_attempts: List[Dict[str, Any]] = []
        self.last_result: Dict[str, Any] = {}
        self.last_error = ""
        self.used_model = ""
        self.final_prompt = self.prompt
        self.total_elapsed = 0.0
        self.succeeded_count = 0
        self.failed_count = 0
        self.completed_count = 0
        for index in range(self.requested_count):
            # Cancellation must not discard a completed shot or start another.
            if self.task_id and plugin._task_cancel_requested(self.task_id):
                break
            shot_prompt, shot_refs, shot_translation_meta = self.prompt, self.refs, self.prompt_en_meta
            if self.variation_rows:
                variant_action = str(
                    self.variation_rows[min(index, len(self.variation_rows) - 1)].get("prompt") or self.original_prompt
                )
                shot_prompt, shot_refs, shot_translation_meta = await self._compose_prompt(variant_action)
            one = await plugin._run_image_generation(
                prompt=shot_prompt,
                aspect_ratio=self.aspect,
                resolution=self.resolution,
                refs=shot_refs,
                targets=[self.target],
                source="web-test",
                original_prompt=shot_prompt,
                event=None,
                max_attempts=1,
                allow_compat_retry=False,
                prompt_en_meta=shot_translation_meta,
                record_context={
                    key: value
                    for key, value in {
                        "retry_record_id": self.retry_record_id,
                        "task_id": self.task_id,
                        "trace_id": self.trace_id,
                        "requested_count": self.requested_count,
                        "raw_reference_image_count": len(self.raw_images),
                    }.items()
                    if value
                } or None,
            )
            self.last_result = one if isinstance(one, dict) else {}
            try:
                self.total_elapsed += float(self.last_result.get("elapsed_seconds") or 0)
            except (TypeError, ValueError):
                pass
            self.used_model = str(self.last_result.get("used_model") or self.used_model)
            self.final_prompt = str(self.last_result.get("final_prompt") or self.final_prompt)
            self.all_paths.extend(
                str(path).strip()
                for path in (
                    self.last_result.get("image_paths") or self.last_result.get("generated_image_paths") or []
                )
                if str(path).strip()
            )
            self.all_request_paths.extend(
                str(path).strip()
                for path in (self.last_result.get("request_image_paths") or [])
                if str(path).strip()
            )
            raw_attempts = self.last_result.get("attempts")
            if isinstance(raw_attempts, list):
                self.all_attempts.extend(item for item in raw_attempts if isinstance(item, dict))
            self.completed_count += 1
            if self.last_result.get("success"):
                self.succeeded_count += 1
            else:
                self.failed_count += 1
                self.last_error = str(self.last_result.get("error") or "这次没顺好")
            if self.task_id:
                plugin._set_web_image_task(
                    self.task_id,
                    requested_count=self.requested_count,
                    completed_count=self.completed_count,
                    succeeded_count=self.succeeded_count,
                    failed_count=self.failed_count,
                    progress_percent=int(round(self.completed_count * 100 / max(1, self.requested_count))),
                    current_index=index + 1,
                )
            # Channel tests stop on failure to avoid repeatedly hitting a
            # failing endpoint, while successful shots continue until count.
            if not self.last_result.get("success"):
                break

    def _summarize_result(self) -> Dict[str, Any]:
        request_data = dict(self.last_result.get("request_data") or self.request_summary)
        request_data.update(self.request_summary)
        request_data.update({
            "original_prompt": self.original_prompt,
            "requested_count": self.requested_count,
            "count": self.requested_count,
        })
        success = self.succeeded_count == self.requested_count and self.failed_count == 0
        response_data = dict(self.last_result.get("response_data") or {})
        response_data.update({
            "success": success,
            "count": len(self.all_paths),
            "requested_count": self.requested_count,
            "completed_count": self.completed_count,
            "succeeded_count": self.succeeded_count,
            "failed_count": self.failed_count,
            "generated_image_paths": self.all_paths,
            "attempts": self.all_attempts,
        })
        if self.trace_id:
            response_data["trace_id"] = self.trace_id
        return {
            "success": success,
            "used_model": self.used_model,
            "elapsed_seconds": round(self.total_elapsed, 2),
            "reference_images": len(self.refs),
            "original_prompt": self.original_prompt,
            "final_prompt": self.final_prompt,
            "request_data": request_data,
            "response_data": response_data,
            "request_image_paths": self.all_request_paths,
            "generated_image_paths": self.all_paths,
            "attempts": self.all_attempts,
            "requested_count": self.requested_count,
            "completed_count": self.completed_count,
            "succeeded_count": self.succeeded_count,
            "failed_count": self.failed_count,
            "generation_success": bool(self.all_paths) or self.succeeded_count > 0,
        }

    def _record_failure(self, error: str) -> None:
        response_data = {"success": False, "stage": "web_test_preflight", "error": error}
        if self.trace_id:
            response_data["trace_id"] = self.trace_id
        self.plugin._record_task({
            **self.plugin._source_context(None, "web-test"),
            "source_label": "Web/快速试画",
            "success": False,
            "generation_success": False,
            "delivery_success": None,
            "error": error,
            "prompt": self.original_prompt,
            "original_prompt": self.original_prompt,
            "request_prompt": self.original_prompt,
            "final_prompt": self.original_prompt,
            "used_model": self.model_name,
            "elapsed_seconds": 0,
            "reference_images": len(self.raw_images),
            "request_data": self.request_summary,
            "response_data": response_data,
            "request_image_paths": self.all_request_paths,
            "generated_image_paths": [],
            "retry_record_id": self.retry_record_id,
            "task_id": self.task_id,
            "trace_id": self.trace_id,
        })
