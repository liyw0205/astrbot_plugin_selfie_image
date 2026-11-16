"""Command entry workflows for regular text-to-image and image-to-image requests."""

from __future__ import annotations

from typing import Any, AsyncGenerator, Dict, List, Optional

from ..core.utils import extract_command_message


class ImageCommandService:
    """Run image commands while leaving AstrBot registration on the plugin."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    async def draw(self, event: Any, *fallback_parts: str) -> AsyncGenerator[Any, None]:
        """Run the flexible /画 command, including attached reference images."""
        plugin = self.plugin
        plugin._claim_media_command_event(event)
        fallback = " ".join(item for item in fallback_parts if item).strip()
        message = extract_command_message(event, ("画", "生图"), fallback, include_quote_text=True)
        message, requested_count = plugin._extract_command_count(message, allow_trailing=True)
        message, template_values, template_randomize, _ = plugin._render_command_template(message)
        error = plugin._quota_error_message(event, requested_count) or plugin._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        special_variants, preset_aspect, preset_resolution, preset_name = (
            plugin._expand_dynamic_preset_variants(message, requested_count)
        )
        if special_variants:
            prompt = special_variants[0]
            aspect = preset_aspect
            resolution = preset_resolution
        else:
            prompt, aspect, resolution, preset_name = plugin._expand_user_text_with_preset(message)
        render_payload = {"template_values": template_values, "template_randomize": template_randomize}
        rendered = plugin.render_creative_prompt(prompt, render_payload)
        prompt = str(rendered.get("prompt") or prompt).strip()
        rendered_special_variants: Optional[List[str]] = None
        if special_variants:
            rendered_special_variants = [prompt]
            for variant in special_variants[1:]:
                variant_rendered = plugin.render_creative_prompt(variant, render_payload)
                rendered_special_variants.append(
                    str(variant_rendered.get("prompt") or variant).strip()
                )

        # General image commands may use explicit/contextual references, never
        # the persona image reserved for the dedicated selfie workflow.
        refs, source_count, failed_count = await plugin._event_reference_images_with_stats(
            event,
            include_at_avatar=True,
            context_hint=prompt,
            allow_context_fallback=True,
            include_persona=False,
        )
        if not refs and source_count and failed_count:
            yield event.plain_result(plugin._reference_failure_message(event))
            return
        if not prompt and refs:
            prompt = "根据参考图生成一张自然、清晰、符合原图语义的图片。"
        if not prompt:
            yield event.plain_result("请输入提示词或附带参考图。")
            return

        progress = await plugin._build_contextual_progress_text(event, "image", prompt, requested_count)

        async def runner(task_id: str) -> Dict[str, Any]:
            return await plugin._background_draw_batches(
                task_id,
                event,
                prompt,
                aspect,
                resolution,
                refs,
                "command-draw",
                requested_count,
                passthrough=True,
                special_preset_variants=rendered_special_variants,
            )

        plugin.start_command_image_task(
            event,
            source="command-draw",
            summary={
                "original_prompt": prompt,
                "aspect_ratio": aspect,
                "resolution": resolution,
                "requested_count": requested_count,
                "reference_image_count": len(refs),
                "special_preset_variants": rendered_special_variants,
                "template_values": template_values,
                "template_randomize": template_randomize,
                "template_unresolved": rendered.get("unresolved") or [],
                "preset_name": preset_name,
            },
            runner=runner,
            reference_images=refs,
        )
        yield event.plain_result(progress)

    async def raw_text_to_image(self, event: Any, *fallback_parts: str) -> AsyncGenerator[Any, None]:
        """Run /文生图 without adding persona or reference images."""
        plugin = self.plugin
        plugin._claim_media_command_event(event)
        fallback = " ".join(item for item in fallback_parts if item).strip()
        message = extract_command_message(event, "文生图", fallback, include_quote_text=True)
        message, requested_count = plugin._extract_command_count(message, allow_trailing=True)
        message, template_values, template_randomize, _ = plugin._render_command_template(message)
        error = plugin._quota_error_message(event, requested_count) or plugin._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        special_variants, preset_aspect, preset_resolution, preset_name = (
            plugin._expand_dynamic_preset_variants(message, requested_count)
        )
        if special_variants:
            prompt = special_variants[0]
            aspect = preset_aspect
            resolution = preset_resolution
        else:
            prompt, aspect, resolution, preset_name = plugin._expand_user_text_with_preset(message)
        render_payload = {"template_values": template_values, "template_randomize": template_randomize}
        rendered = plugin.render_creative_prompt(prompt, render_payload)
        prompt = str(rendered.get("prompt") or prompt).strip()
        rendered_special_variants: Optional[List[str]] = None
        if special_variants:
            rendered_special_variants = [prompt]
            for variant in special_variants[1:]:
                variant_rendered = plugin.render_creative_prompt(variant, render_payload)
                rendered_special_variants.append(
                    str(variant_rendered.get("prompt") or variant).strip()
                )
        if not prompt:
            yield event.plain_result("请输入文生图提示词。")
            return

        progress = await plugin._build_contextual_progress_text(event, "image", prompt, requested_count)

        async def runner(task_id: str) -> Dict[str, Any]:
            return await plugin._background_draw_batches(
                task_id,
                event,
                prompt,
                aspect,
                resolution,
                [],
                "command-raw-text-to-image",
                requested_count,
                passthrough=True,
                special_preset_variants=rendered_special_variants,
            )

        plugin.start_command_image_task(
            event,
            source="command-raw-text-to-image",
            summary={
                "original_prompt": prompt,
                "aspect_ratio": aspect,
                "resolution": resolution,
                "requested_count": requested_count,
                "template_values": template_values,
                "template_randomize": template_randomize,
                "template_unresolved": rendered.get("unresolved") or [],
                "preset_name": preset_name,
                "special_preset_variants": rendered_special_variants,
            },
            runner=runner,
        )
        yield event.plain_result(progress)

    async def raw_image_to_image(self, event: Any, *fallback_parts: str) -> AsyncGenerator[Any, None]:
        """Run /图生图 using required event or quoted reference images."""
        plugin = self.plugin
        plugin._claim_media_command_event(event)
        fallback = " ".join(item for item in fallback_parts if item).strip()
        message = extract_command_message(event, "图生图", fallback, include_quote_text=True)
        message, requested_count = plugin._extract_command_count(message, allow_trailing=True)
        error = plugin._quota_error_message(event, requested_count) or plugin._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        special_variants, preset_aspect, preset_resolution, preset_name = (
            plugin._expand_dynamic_preset_variants(message, requested_count)
        )
        if special_variants:
            prompt = special_variants[0]
            aspect = preset_aspect
            resolution = preset_resolution
        else:
            prompt, aspect, resolution, preset_name = plugin._expand_user_text_with_preset(message)
        refs, source_count, failed_count = await plugin._event_reference_images_with_stats(
            event,
            include_at_avatar=True,
            context_hint=prompt,
            allow_context_fallback=True,
        )
        if not refs:
            if source_count and failed_count:
                yield event.plain_result(plugin._reference_failure_message(event))
                return
            yield event.plain_result("请附带、引用图片，或艾特要作为参考的对象。")
            return
        if not prompt:
            yield event.plain_result("请输入图生图提示词。")
            return

        progress = await plugin._build_contextual_progress_text(event, "image", prompt, requested_count)

        async def runner(task_id: str) -> Dict[str, Any]:
            return await plugin._background_draw_batches(
                task_id,
                event,
                prompt,
                aspect,
                resolution,
                refs,
                "command-raw-image-to-image",
                requested_count,
                passthrough=True,
                special_preset_variants=special_variants,
            )

        plugin.start_command_image_task(
            event,
            source="command-raw-image-to-image",
            summary={
                "original_prompt": prompt,
                "aspect_ratio": aspect,
                "resolution": resolution,
                "requested_count": requested_count,
                "reference_image_count": len(refs),
                "special_preset_variants": special_variants,
                "preset_name": preset_name,
            },
            runner=runner,
            reference_images=refs,
        )
        yield event.plain_result(progress)
