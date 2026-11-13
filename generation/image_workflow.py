"""Single-shot image generation, auditing and durable record workflow."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from typing import Any, Callable, Dict, List, Optional

from ..core.models import ImageModelTarget
from ..core.providers import ImageGenerateRequest, ImageReference
from ..core.utils import detect_mime_by_bytes, redact_sensitive_text


async def run_image_generation_workflow(
    self,
    prompt: str,
    aspect_ratio: str,
    resolution: str,
    refs: List[ImageReference],
    targets: Optional[List[ImageModelTarget]] = None,
    source: str = "command",
    audit_user_id: str = "",
    event: Optional[Any] = None,
    original_prompt: str = "",
    max_attempts: Optional[int] = None,
    allow_compat_retry: bool = True,
    prompt_en_meta: Optional[Dict[str, Any]] = None,
    record_context: Optional[Mapping[str, Any]] = None,
    reference_selection: Optional[Mapping[str, Any]] = None,
    reference_fidelity: str = "",
    *,
    generate_images: Callable[..., Any],
) -> Dict[str, Any]:
    # ``targets=[]`` is an intentional explicit selection (for example a
    # disabled channel test); do not silently replace it with all configured
    # channels via truthiness.
    candidate_targets = (
        self._resolve_generation_targets(event) if targets is None else list(targets)
    )
    if reference_selection is None and event is not None:
        consume_selection = getattr(self, "_consume_reference_selection", None)
        if callable(consume_selection):
            reference_selection = consume_selection(event)
    selected_targets, cooldown_attempts = self._select_healthy_generation_targets(
        candidate_targets
    )
    request_prompt = str(prompt or "")
    plain_request_prompt = request_prompt
    original_prompt = str(original_prompt or request_prompt)
    reference_fidelity = (
        "low"
        if refs and str(reference_fidelity or "").strip().lower() == "low"
        else ""
    )
    stable_reference_checker = getattr(self, "_is_stable_reference", None)
    persona_reference_count = sum(
        1
        for ref in refs
        if callable(stable_reference_checker) and stable_reference_checker(ref)
    )
    # Leg-focus actions are normalized into a neutral outfit prompt before generation.
    # Audit that effective prompt so command labels do not create false positives.
    is_leg_focus_request = (
        source == "command-look-legs"
        or "【legs:outfit】" in original_prompt
        or "下半身穿搭" in original_prompt
    )
    audit_prompt_text = request_prompt if is_leg_focus_request else (original_prompt or request_prompt)
    source_meta = self._source_context(event, source, audit_user_id)
    is_studio_run = source == "studio-run"
    if is_studio_run and isinstance(record_context, Mapping):
        studio_title = str(record_context.get("studio_session_title") or "画布").strip() or "画布"
        source_meta["source_label"] = f"Web/{studio_title}"
    elif source == "web-test":
        source_meta["source_label"] = "Web/快速试画"
    selection_metadata: Dict[str, Any] = {}
    if isinstance(reference_selection, Mapping):
        selection_roles = reference_selection.get("roles") if isinstance(reference_selection.get("roles"), Mapping) else {}
        normalized_selection_roles = {}
        for key, value in selection_roles.items():
            try:
                count = int(value or 0)
            except (TypeError, ValueError):
                count = 0
            normalized_selection_roles[str(key)] = max(0, count)
        try:
            selected_count = max(0, int(reference_selection.get("selected_count") or len(refs)))
        except (TypeError, ValueError):
            selected_count = len(refs)
        try:
            failed_count = max(0, int(reference_selection.get("failed_count") or 0))
        except (TypeError, ValueError):
            failed_count = 0
        try:
            raw_count = max(0, int(reference_selection.get("raw_reference_image_count_total") or reference_selection.get("raw_selected_count") or selected_count))
        except (TypeError, ValueError):
            raw_count = selected_count
        try:
            deduplicated_count = max(0, int(reference_selection.get("deduplicated_reference_image_count_total") or selected_count))
        except (TypeError, ValueError):
            deduplicated_count = selected_count
        deduplicated_count = min(deduplicated_count, raw_count) if raw_count else 0
        selection_metadata = {
            "identity_reference_source": str(reference_selection.get("identity_reference_source") or "none"),
            "identity_reference_count": max(0, int(reference_selection.get("identity_reference_count") or 0)),
            "extra_reference_image_count": max(0, int(reference_selection.get("extra_reference_image_count") or max(0, selected_count - 1))),
            "raw_reference_image_count_total": raw_count,
            "deduplicated_reference_image_count_total": deduplicated_count,
            "duplicate_reference_image_count_total": max(0, raw_count - deduplicated_count),
            "roles": dict(normalized_selection_roles),
        }
        source_meta.update(selection_metadata)

    if persona_reference_count:
        source_meta["persona_reference_count"] = persona_reference_count
    selection_roles = (
        reference_selection.get("roles")
        if isinstance(reference_selection, Mapping)
        and isinstance(reference_selection.get("roles"), Mapping)
        else {}
    )
    uses_persona_reference = bool(
        persona_reference_count
        or (
            isinstance(reference_selection, Mapping)
            and (
                reference_selection.get("used_persona")
                or selection_roles.get("persona")
                or selection_roles.get("persona_extra")
            )
        )
    )

    def add_selection_metadata(request: Dict[str, Any]) -> Dict[str, Any]:
        if not selection_metadata or not isinstance(reference_selection, Mapping):
            return request
        request.update(selection_metadata)
        request["reference_selection"] = {
            "roles": dict(selection_metadata.get("roles") or {}),
            "selected_count": selection_metadata["deduplicated_reference_image_count_total"],
            "failed_count": max(0, int(reference_selection.get("failed_count") or 0)),
            "used_persona": bool(reference_selection.get("used_persona")),
            "used_context_fallback": bool(reference_selection.get("used_context_fallback")),
        }
        return request

    self._set_generation_stage(
        record_context,
        "preflight",
        "准备画布任务" if is_studio_run else "准备图片请求",
    )
    # Workflow links are intentionally small and non-sensitive. Keeping
    # them at the record top level makes detail/list consumers independent
    # from the request-data compaction policy.
    if isinstance(record_context, Mapping):
        for key in (
            "studio_session_id",
            "studio_task_id",
            "studio_session_title",
            "studio_template",
            "studio_source_asset_ids",
            "retry_record_id",
            "task_id",
            "requested_count",
            "raw_reference_image_count",
        ):
            value = record_context.get(key)
            if isinstance(value, (str, int, float, bool)):
                source_meta[key] = value
            elif isinstance(value, list):
                source_meta[key] = [str(item).strip() for item in value if str(item).strip()][:24]
    request_image_paths = self._save_reference_images_to_cache(refs)
    task_id = str(record_context.get("task_id") or "").strip() if isinstance(record_context, Mapping) else ""
    if task_id:
        task_setter = getattr(self, "_set_web_image_task", None)
        if callable(task_setter):
            task_setter(task_id, request_image_paths=list(request_image_paths))
    self._set_generation_stage(
        record_context,
        "prompt_translation",
        "处理画布提示词" if is_studio_run else "处理图片提示词",
    )
    image_to_text_targets = [
        target
        for target in selected_targets
        if bool((getattr(target, "extra", {}) or {}).get("image_to_text_enabled"))
    ]
    image_to_text_meta: Dict[str, Any] = {
        "enabled": bool(refs and image_to_text_targets),
        "applied": False,
        "target_count": len(image_to_text_targets),
    }
    if refs and image_to_text_targets:
        try:
            from ..prompts.prompt_templates import explicit_selfie_action_mode

            locked_mode = explicit_selfie_action_mode(request_prompt) or explicit_selfie_action_mode(original_prompt)
            describe_options = {"prompt_contract": request_prompt} if locked_mode else {}
            reference_description, image_to_text_meta = await self._describe_reference_images_for_generation(
                event, [ref.data for ref in refs if ref and ref.data], **describe_options
            )
            # Keep the user's instruction intact while making the visual
            # details available to models that receive a text-only request.
            if is_leg_focus_request:
                description_line = (
                    "参考图内容描述（仅用于身份及合同要求保留的服饰；不得覆盖已锁定的腿部机位、姿势、场景、裁切或袜装）："
                    f"{reference_description}"
                )
            elif locked_mode == "cos":
                description_line = (
                    "参考图内容描述（仅用于辅助识别身份；不得覆盖已锁定的 COS 服饰、姿势、场景、机位或构图）："
                    f"{reference_description}"
                )
            elif locked_mode:
                description_line = (
                    "参考图内容描述（仅补充身份及合同要求保留的服饰；机位、姿势、视线、场景和构图服从本次合同）："
                    f"{reference_description}"
                )
            else:
                description_line = f"参考图内容描述：{reference_description}"
            request_prompt = "\n\n".join(
                part for part in (request_prompt.strip(), description_line) if part
            )
            audit_prompt_text = request_prompt
        except Exception as exc:
            error = f"图转文失败：{redact_sensitive_text(str(exc))}"
            image_to_text_meta = {
                **image_to_text_meta,
                "applied": False,
                "error": redact_sensitive_text(str(exc)),
            }
            plain_targets = [target for target in selected_targets if target not in image_to_text_targets]
            if plain_targets:
                # A failed helper must not block fallback models that still
                # support ordinary image-to-image requests.
                selected_targets = plain_targets
                image_to_text_targets = []
            else:
                response_data = {"success": False, "stage": "image_to_text", "error": error}
                request_data = {
                    "original_prompt": original_prompt,
                    "request_prompt": request_prompt,
                    "audit_prompt": audit_prompt_text,
                    "aspect_ratio": aspect_ratio,
                    "resolution": resolution,
                    "reference_image_count": len(refs),
                    "persona_reference_count": persona_reference_count,
                    "use_selfie_reference": uses_persona_reference,
                    "request_image_paths": request_image_paths,
                    "targets": [redact_sensitive_text(target.label) for target in selected_targets],
                    "image_to_text": image_to_text_meta,
                }
                add_selection_metadata(request_data)
                self._record_task(
                    {
                        **source_meta,
                        "success": False,
                        "generation_success": False,
                        "delivery_success": None,
                        "error": error,
                        "prompt": request_prompt,
                        "original_prompt": original_prompt,
                        "request_prompt": request_prompt,
                        "final_prompt": request_prompt,
                        "used_model": "",
                        "elapsed_seconds": 0,
                        "reference_images": len(refs),
                        "request_data": request_data,
                        "response_data": response_data,
                        "request_image_paths": request_image_paths,
                        "generated_image_paths": [],
                    }
                )
                return {
                    "success": False,
                    "error": error,
                    "original_prompt": original_prompt,
                    "final_prompt": request_prompt,
                    "request_data": request_data,
                    "response_data": response_data,
                }
    request_data = {
        "original_prompt": original_prompt,
        "request_prompt": request_prompt,
        "audit_prompt": audit_prompt_text,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "reference_image_count": len(refs),
        "persona_reference_count": persona_reference_count,
        "use_selfie_reference": uses_persona_reference,
        "request_image_paths": request_image_paths,
        "targets": [redact_sensitive_text(target.label) for target in selected_targets],
        "image_to_text": image_to_text_meta,
    }
    if isinstance(record_context, Mapping) and isinstance(record_context.get("nai_reference"), Mapping):
        request_data["nai_reference"] = dict(record_context["nai_reference"])
    if reference_fidelity:
        request_data["reference_fidelity"] = reference_fidelity
    add_selection_metadata(request_data)
    if cooldown_attempts:
        request_data["cooldown_skipped_channels"] = cooldown_attempts
    if isinstance(record_context, Mapping):
        for key in (
            "studio_session_id",
            "studio_task_id",
            "studio_session_title",
            "studio_template",
            "studio_source_asset_ids",
            "retry_record_id",
            "task_id",
            "requested_count",
            "raw_reference_image_count",
        ):
            value = record_context.get(key)
            if isinstance(value, (str, int, float, bool)):
                request_data[key] = value
            elif isinstance(value, list):
                request_data[key] = [str(item).strip() for item in value if str(item).strip()][:24]
    if prompt_en_meta:
        request_data["prompt_en"] = dict(prompt_en_meta)
        if prompt_en_meta.get("applied"):
            request_data["request_prompt_en"] = request_prompt
    request_data["composition"] = self._composition_metadata(
        request_prompt, source, aspect_ratio, resolution, len(refs)
    )
    request_cleanup = self._cleanup_image_cache_if_needed(request_image_paths)
    if request_cleanup.get("deleted"):
        request_data["cache_cleanup_before_generation"] = request_cleanup

    audit_ok, audit_reason = await self._audit_prompt(audit_prompt_text, audit_user_id, event)
    if not audit_ok:
        response_data = {"success": False, "stage": "prompt_audit", "error": f"提示词审核未通过：{audit_reason}"}
        self._record_task(
            {
                **source_meta,
                "success": False,
                "generation_success": False,
                "delivery_success": None,
                "error": response_data["error"],
                "prompt": request_prompt,
                "original_prompt": original_prompt,
                "request_prompt": request_prompt,
                "final_prompt": request_prompt,
                "used_model": "",
                "elapsed_seconds": 0,
                "reference_images": len(refs),
                "request_data": request_data,
                "response_data": response_data,
                "retry_count": 0,
                "retry_exhausted": False,
                "request_image_paths": request_image_paths,
                "generated_image_paths": [],
            }
        )
        return {
            "success": False,
            "error": f"提示词审核未通过：{audit_reason}",
            "original_prompt": original_prompt,
            "final_prompt": request_prompt,
            "request_data": request_data,
        }

    # Optional: translate final image prompt to English for models weak on Chinese.
    if (
        (prompt_en_meta is None or image_to_text_meta.get("applied"))
        and self._prompt_en_needed(request_prompt, media="image")
    ):
        translated, en_meta = await self._translate_prompt_to_english(
            request_prompt, media="image", event=event
        )
        request_data["prompt_en"] = en_meta
        if en_meta.get("applied") and translated:
            request_prompt = translated
            request_data["request_prompt"] = request_prompt
            request_data["request_prompt_en"] = translated

    if not selected_targets:
        response_data = {
            "success": False,
            "stage": "select_model",
            "error": "当前没有可用的出图模型，请先在管理页启用渠道和模型。",
        }
        self._record_task(
            {
                **source_meta,
                "success": False,
                "generation_success": False,
                "delivery_success": None,
                "error": response_data["error"],
                "prompt": request_prompt,
                "original_prompt": original_prompt,
                "request_prompt": request_prompt,
                "final_prompt": request_prompt,
                "used_model": "",
                "elapsed_seconds": 0,
                "reference_images": len(refs),
                "request_data": request_data,
                "response_data": response_data,
                "retry_count": 0,
                "retry_exhausted": False,
                "request_image_paths": request_image_paths,
                "generated_image_paths": [],
            }
        )
        return {
            "success": False,
            "error": response_data["error"],
            "original_prompt": original_prompt,
            "final_prompt": request_prompt,
            "request_data": request_data,
            "response_data": response_data,
        }

    request = ImageGenerateRequest(
        prompt=request_prompt,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        images=refs,
        allow_compat_retry=allow_compat_retry,
        max_image_bytes=self.config.image_max_image_size_mb * 1024 * 1024,
        reference_fidelity=reference_fidelity,
    )

    # A model-level translation opt-in is evaluated after target selection
    # so it cannot affect unrelated fallback models. The global switch was
    # handled above and remains authoritative for every image target.
    model_prompt_overrides: Dict[str, str] = {}
    model_prompt_meta: Dict[str, Dict[str, Any]] = {}
    if not bool(getattr(self.config, "image_enable_image_prompt_en", False)):
        for target in selected_targets:
            if not self._prompt_en_needed(request_prompt, media="image", target=target):
                continue
            translated, en_meta = await self._translate_prompt_to_english(
                request_prompt,
                media="image",
                event=event,
                target=target,
            )
            model_prompt_meta[target.label] = dict(en_meta)
            if en_meta.get("applied") and translated:
                model_prompt_overrides[target.label] = translated
    if model_prompt_meta:
        request_data["prompt_en_by_model"] = {
            label: dict(meta) for label, meta in model_prompt_meta.items()
        }

    def request_for_target(target: ImageModelTarget) -> ImageGenerateRequest:
        use_text_description = bool(
            (getattr(target, "extra", {}) or {}).get("image_to_text_enabled") and refs
        )
        target_prompt = request_prompt if (
            use_text_description
            or bool(getattr(self.config, "image_enable_image_prompt_en", False))
        ) else plain_request_prompt
        if target.label in model_prompt_overrides:
            target_prompt = model_prompt_overrides[target.label]
        return ImageGenerateRequest(
            prompt=target_prompt,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            images=[] if use_text_description else refs,
            allow_compat_retry=allow_compat_retry,
            max_image_bytes=self.config.image_max_image_size_mb * 1024 * 1024,
            reference_fidelity=reference_fidelity if not use_text_description else "",
        )

    started = time.monotonic()
    self._set_generation_stage(
        record_context,
        "queue",
        "等待画布并发槽位" if is_studio_run else "等待图片并发槽位",
    )
    self._set_generation_stage(
        record_context,
        "generating",
        "生成画布结果" if is_studio_run else "生成图片结果",
    )
    result = await generate_images(
        selected_targets,
        request,
        None,
        max_attempts=max_attempts,
        global_timeout=self.config.image_global_timeout,
        request_factory=request_for_target,
    )
    if cooldown_attempts:
        result.attempts = [*cooldown_attempts, *(result.attempts or [])]
    elapsed = time.monotonic() - started
    record_channel_health = getattr(self, "_record_channel_health", None)
    if callable(record_channel_health):
        record_channel_health(result.attempts)

    if not result.error and result.images:
        used_target = next(
            (
                target
                for target in selected_targets
                if redact_sensitive_text(target.label) == result.used_model
            ),
            None,
        )
        used_image_to_text = bool(
            used_target
            and refs
            and (getattr(used_target, "extra", {}) or {}).get("image_to_text_enabled")
        )
        request_data["image_to_text"]["used_by_model"] = used_image_to_text
        if not used_image_to_text:
            request_prompt = plain_request_prompt
            request_data["request_prompt"] = plain_request_prompt
            request_data["composition"] = self._composition_metadata(
                plain_request_prompt, source, aspect_ratio, resolution, len(refs)
            )

    if result.error or not result.images:
        response_data = {
            "success": False,
            "stage": "generate",
            "error": result.error or "未生成任何图片",
            "used_model": result.used_model,
            "elapsed_seconds": round(elapsed, 2),
            "generated_image_sources": result.source_media,
            "attempts": result.attempts,
            "retry_count": int(getattr(result, "retry_count", 0) or 0),
            "retry_exhausted": bool(getattr(result, "retry_exhausted", False)),
        }
        self._record_task(
            {
                **source_meta,
                "success": False,
                "generation_success": False,
                "delivery_success": None,
                "error": response_data["error"],
                "prompt": request_prompt,
                "original_prompt": original_prompt,
                "request_prompt": request_prompt,
                "final_prompt": request_prompt,
                "used_model": result.used_model,
                "elapsed_seconds": round(elapsed, 2),
                "reference_images": len(refs),
                "request_data": request_data,
                "response_data": response_data,
                "retry_count": int(getattr(result, "retry_count", 0) or 0),
                "retry_exhausted": bool(getattr(result, "retry_exhausted", False)),
                "request_image_paths": request_image_paths,
                "generated_image_paths": [],
                "generated_image_sources": result.source_media,
            }
        )
        return {
            "success": False,
            "error": result.error or "未生成任何图片",
            "original_prompt": original_prompt,
            "final_prompt": request_prompt,
            "elapsed_seconds": elapsed,
            "used_model": result.used_model,
            "request_data": request_data,
            "response_data": response_data,
            "request_image_paths": request_image_paths,
            "attempts": result.attempts,
            "retry_count": int(getattr(result, "retry_count", 0) or 0),
            "retry_exhausted": bool(getattr(result, "retry_exhausted", False)),
        }

    generated_images = [image for image in result.images if image]
    generated_image_paths = [
        self._save_cache_image(image, "generated", detect_mime_by_bytes(image))
        for image in generated_images
    ]
    generated_image_md5s = [hashlib.md5(image).hexdigest() for image in generated_images]
    files = [self._cache_absolute_path(path) for path in generated_image_paths]
    self._set_generation_stage(
        record_context,
        "output_audit",
        "检查画布结果" if is_studio_run else "检查图片结果",
    )
    output_ok, output_reason = await self._audit_output_images(files, audit_user_id, prompt, event=event)
    if not output_ok:
        response_data = {
            "success": False,
            "stage": "output_audit",
            "error": f"图片内容审核未通过：{output_reason}",
            "used_model": result.used_model,
            "elapsed_seconds": round(elapsed, 2),
            "generated_image_paths": generated_image_paths,
            "generated_image_sources": result.source_media,
            "blocked_images_retained": True,
            "attempts": result.attempts,
            "retry_count": int(getattr(result, "retry_count", 0) or 0),
            "retry_exhausted": bool(getattr(result, "retry_exhausted", False)),
        }
        self._record_task(
            {
                **source_meta,
                "success": False,
                "generation_success": False,
                "delivery_success": None,
                "error": response_data["error"],
                "prompt": request_prompt,
                "original_prompt": original_prompt,
                "request_prompt": request_prompt,
                "final_prompt": request_prompt,
                "used_model": result.used_model,
                "elapsed_seconds": round(elapsed, 2),
                "reference_images": len(refs),
                "request_data": request_data,
                "response_data": response_data,
                "retry_count": int(getattr(result, "retry_count", 0) or 0),
                "retry_exhausted": bool(getattr(result, "retry_exhausted", False)),
                "request_image_paths": request_image_paths,
                "generated_image_paths": generated_image_paths,
                "generated_image_sources": result.source_media,
                "md5s": generated_image_md5s,
            }
        )
        return {
            "success": False,
            "error": f"图片内容审核未通过：{output_reason}",
            "original_prompt": original_prompt,
            "final_prompt": request_prompt,
            "elapsed_seconds": elapsed,
            "used_model": result.used_model,
            "image_paths": generated_image_paths,
            "attempts": result.attempts,
            "retry_count": int(getattr(result, "retry_count", 0) or 0),
            "retry_exhausted": bool(getattr(result, "retry_exhausted", False)),
        }

    cleanup = self._cleanup_image_cache_if_needed([*request_image_paths, *generated_image_paths])
    response_data = {
        "success": True,
        "used_model": result.used_model,
        "elapsed_seconds": round(elapsed, 2),
        "count": len(files),
        "generated_image_paths": generated_image_paths,
        "generated_image_sources": result.source_media,
        "cache_cleanup": cleanup,
        "attempts": result.attempts,
        "retry_count": int(getattr(result, "retry_count", 0) or 0),
        "retry_exhausted": bool(getattr(result, "retry_exhausted", False)),
    }
    self._record_task(
        {
            **source_meta,
            "success": True,
            "generation_success": True,
            "delivery_success": None,
            "prompt": request_prompt,
            "original_prompt": original_prompt,
            "request_prompt": request_prompt,
            "final_prompt": request_prompt,
            "used_model": result.used_model,
            "elapsed_seconds": round(elapsed, 2),
            "reference_images": len(refs),
            "count": len(files),
            "request_data": request_data,
            "response_data": response_data,
            "retry_count": int(getattr(result, "retry_count", 0) or 0),
            "retry_exhausted": bool(getattr(result, "retry_exhausted", False)),
            "request_image_paths": request_image_paths,
            "generated_image_paths": generated_image_paths,
            "generated_image_sources": result.source_media,
            "md5s": generated_image_md5s,
        }
    )
    return {
        "success": True,
        "original_prompt": original_prompt,
        "final_prompt": request_prompt,
        "files": files,
        "image_paths": generated_image_paths,
        "elapsed_seconds": elapsed,
        "used_model": result.used_model,
        "reference_images": len(refs),
        "request_data": request_data,
        "response_data": response_data,
        "request_image_paths": request_image_paths,
        "attempts": result.attempts,
        "retry_count": int(getattr(result, "retry_count", 0) or 0),
        "retry_exhausted": bool(getattr(result, "retry_exhausted", False)),
        "generated_image_sources": result.source_media,
    }
