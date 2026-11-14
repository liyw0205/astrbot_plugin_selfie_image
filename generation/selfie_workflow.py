"""Command parsing and task orchestration for selfie generation workflows."""

from __future__ import annotations

from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from ..core.utils import extract_command_message, extract_image_sources_from_event


class SelfieWorkflowService:
    """Own shared selfie command orchestration while plugin hooks stay stable."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def _resolve_action_options(
        self,
        event: Any,
        command_name: Any,
        fallback: str,
        message: str,
        source: str,
        preset_aspect: str,
        preset_resolution: str,
        preset_name: str,
        rebuild_extra_request: str,
    ) -> Tuple[str, str, str]:
        plugin = self.plugin
        if source == "command-look-cos":
            action, _, _ = plugin._parse_prompt_options(message)
            _, aspect, resolution = plugin._parse_prompt_options(
                rebuild_extra_request, preset_aspect, preset_resolution,
            )
            raw_request = extract_command_message(event, command_name, include_quote_text=True)
            _, aspect, resolution = plugin._parse_prompt_options(raw_request, aspect, resolution)
        elif any(str(value or "").strip() for value in (preset_name, preset_aspect, preset_resolution)):
            default_aspect = str(plugin.config.image_default_aspect_ratio or "9:16").strip() or "9:16"
            default_resolution = str(plugin.config.image_default_resolution or "1K").strip() or "1K"
            action, aspect, resolution = plugin._parse_prompt_options(
                message,
                str(preset_aspect or "").strip() or default_aspect,
                str(preset_resolution or "").strip() or default_resolution,
            )
            if preset_aspect and preset_aspect != default_aspect:
                aspect = preset_aspect
            raw_request = extract_command_message(event, command_name, fallback, include_quote_text=True)
            _, aspect, resolution = plugin._parse_prompt_options(raw_request, aspect, resolution)
        else:
            action, aspect, resolution, _, _ = plugin._resolve_image_preset(message)
        return action, aspect, resolution

    async def handle_command(
        self,
        event: Any,
        command_name: Any,
        fallback: str,
        default_action: str,
        default_action_with_refs: str,
        progress_label: str,
        source: str,
        fail_label: str,
        message_override: str = "",
        include_at_avatar: bool = False,
        allow_context_fallback: bool = True,
        requested_count_override: int = 0,
        preset_aspect: str = "",
        preset_resolution: str = "",
        preset_name: str = "",
        rebuild_extra_request: str = "",
        rebuild_match_query: str = "",
        rebuild_special_preset_variants: Optional[List[str]] = None,
    ) -> AsyncGenerator[Any, None]:
        plugin = self.plugin
        plugin._claim_media_command_event(event)
        message = (
            message_override.strip()
            if message_override
            else extract_command_message(event, command_name, fallback, include_quote_text=True)
        )
        if requested_count_override > 0:
            requested_count = plugin._normalize_count(requested_count_override)
        else:
            message, requested_count = plugin._extract_command_count(message)

        error = plugin._quota_error_message(event, requested_count) or plugin._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return

        action, aspect, resolution = self._resolve_action_options(
            event,
            command_name,
            fallback,
            message,
            source,
            preset_aspect,
            preset_resolution,
            preset_name,
            rebuild_extra_request,
        )
        extra_refs = await plugin._event_reference_images(
            event,
            include_at_avatar=include_at_avatar,
            context_hint=action,
            allow_context_fallback=allow_context_fallback,
        )
        if not action:
            action = default_action_with_refs if extra_refs else default_action
        cos_selection = plugin._select_selfie_references(action, extra_refs)
        group_selection = (
            plugin._select_group_references(extra_refs)
            if progress_label == "合影" and not plugin._is_cos_action(action)
            else None
        )
        selection = group_selection or cos_selection
        hints: List[str] = []
        if not plugin.persona.has_reference_image():
            if bool(getattr(plugin.config, "image_use_logo_when_no_persona", True)):
                hints.append("当前还没有设置 AI 形象参考图，将用插件 logo 作为形象回退；关闭「无形象图用 logo」后改为仅按人设生成。")
            else:
                hints.append("当前还没有设置 AI 形象参考图，会按人设与今日设定生成主角。")
        if progress_label == "合影" and not extra_refs:
            hints.append("没有读取到合影对象参考图，会按文字要求生成同框对象。")
        progress = await plugin._build_contextual_progress_text(event, "selfie", action, requested_count)
        if hints:
            progress += "\n" + "\n".join(hints)
        queue_notified = plugin._image_batch_queue_expected(requested_count)
        if queue_notified:
            progress = "当前生图并发已满，本次先排队，空出槽位后继续。\n" + progress
        plugin._record_bot_text_context(event, progress)

        async def runner(task_id: str) -> Dict[str, Any]:
            return await plugin._background_selfie_batches(
                task_id,
                event,
                action,
                extra_refs,
                source,
                requested_count,
                aspect,
                resolution,
                fail_label,
                queue_notified=queue_notified,
                rebuild_extra_request=rebuild_extra_request,
                rebuild_match_query=rebuild_match_query,
                rebuild_special_preset_variants=rebuild_special_preset_variants,
                cos_selection=selection,
                reference_selection=selection.summary() if selection else None,
            )

        plugin.start_command_image_task(
            event,
            source=source,
            summary={
                "original_prompt": action,
                "aspect_ratio": aspect,
                "resolution": resolution,
                "requested_count": requested_count,
                "kind": progress_label,
                "reference_image_count": len(extra_refs),
                "special_preset_variants": rebuild_special_preset_variants,
                "preset_name": str(preset_name or "").strip(),
            },
            runner=runner,
            reference_images=plugin._selfie_reference_images(action, extra_refs, selection),
        )
        yield event.plain_result(progress)

    async def selfie(self, event: Any, *fallback_parts: str) -> AsyncGenerator[Any, None]:
        """Prepare and dispatch the /自拍 command without changing its plugin hook."""
        plugin = self.plugin
        fallback_args = " ".join(item for item in fallback_parts if item).strip()
        raw_message = extract_command_message(
            event, ("自拍", "看看"), fallback_args, include_quote_text=True
        )
        raw_extra, requested_count = plugin._extract_command_count(raw_message, allow_trailing=True)
        special_variants, preset_aspect, preset_resolution, preset_name = (
            plugin._expand_dynamic_preset_variants(raw_extra, requested_count)
        )
        if special_variants:
            expanded_extra = special_variants[0]
        else:
            expanded_extra, preset_aspect, preset_resolution, preset_name = plugin._expand_user_text_with_preset(raw_extra)
        has_refs = bool(extract_image_sources_from_event(event))
        await plugin._ensure_daily_selfie_schedule(expanded_extra or "自拍", has_refs)
        base_action = plugin._build_selfie_request_action(expanded_extra, has_refs)
        async for item in plugin._handle_selfie_command(
            event=event,
            command_name=("自拍", "看看"),
            fallback=base_action,
            default_action=plugin._build_selfie_request_action("", False),
            default_action_with_refs=plugin._build_selfie_request_action("", True),
            progress_label="自拍",
            source="command-selfie",
            fail_label=plugin._natural_fail_fallback("selfie"),
            message_override=base_action,
            requested_count_override=requested_count,
            preset_aspect=preset_aspect,
            preset_resolution=preset_resolution,
            preset_name=preset_name,
            rebuild_extra_request=expanded_extra,
            rebuild_special_preset_variants=special_variants,
        ):
            yield item
