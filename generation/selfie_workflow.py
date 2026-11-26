"""Command parsing and task orchestration for selfie generation workflows."""

from __future__ import annotations

from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple

from ..cos.cos_looks import (
    cos_query_has_series_constraint,
    format_cos_look_list,
    match_cos_look_sets,
    parse_cos_look_list_request,
)
from ..cos.leg_focus import LegFocusDataError
from ..core.providers import ImageReference
from ..core.utils import extract_command_message, extract_image_sources_from_event
from ..features.reference_collector import (
    CosReferenceSelection,
    select_cos_references,
    select_group_references,
    select_wardrobe_selfie_references,
)
from ..studio.studio import prompt_preset_group


class SelfieWorkflowService:
    """Own shared selfie command orchestration while plugin hooks stay stable."""

    def __init__(
        self,
        plugin: Any,
        command_message_parser: Optional[Callable[..., str]] = None,
        image_source_extractor: Optional[Callable[..., List[str]]] = None,
    ) -> None:
        self.plugin = plugin
        self.extract_command_message = command_message_parser or extract_command_message
        self.extract_image_sources = image_source_extractor or extract_image_sources_from_event

    def is_cos_action(self, action: str) -> bool:
        return self.plugin.persona.analyze_selfie_intent(str(action or "")).is_cos_look

    def select_group_references(
        self, message_refs: List[ImageReference]
    ) -> CosReferenceSelection:
        plugin = self.plugin
        return select_group_references(
            plugin._persona_identity_reference(allow_logo=False),
            message_refs,
        )

    def select_cos_references(
        self, action: str, message_refs: List[ImageReference]
    ) -> CosReferenceSelection:
        plugin = self.plugin
        persona_ref = plugin._persona_identity_reference(allow_logo=False)
        return select_cos_references(
            persona_ref,
            message_refs,
            action=action,
        )

    def select_selfie_references(
        self, action: str, message_refs: List[ImageReference]
    ) -> Optional[CosReferenceSelection]:
        plugin = self.plugin
        intent = plugin.persona.analyze_selfie_intent(str(action or ""))
        compact = "".join(str(action or "").split()).lower()
        look_cos = bool(intent.is_cos_look and "看看cos" in compact)
        if not look_cos and not intent.is_legs_only and not intent.is_group_photo:
            if intent.is_cos_look:
                return plugin._select_cos_references(action, message_refs)
            if message_refs or intent.change_clothes:
                return None
            wardrobe_item = plugin.persona.current_wardrobe_item()
            wardrobe_ref = plugin.persona.current_wardrobe_reference()
            if wardrobe_item and wardrobe_ref:
                return select_wardrobe_selfie_references(
                    plugin._persona_identity_reference(allow_logo=True),
                    wardrobe_ref,
                    wardrobe_item_id=wardrobe_item.id,
                    wardrobe_item_name=wardrobe_item.name,
                )
            return None

        is_cos = bool(intent.is_cos_look)
        return select_cos_references(
            plugin._persona_identity_reference(allow_logo=not is_cos),
            message_refs,
            action=action,
            prefer_message_identity=True,
        )

    def selfie_reference_images(
        self,
        action: str,
        message_refs: List[ImageReference],
        selection: Optional[CosReferenceSelection] = None,
    ) -> List[ImageReference]:
        plugin = self.plugin
        if selection is not None:
            return list(selection.refs)
        identity = plugin._persona_identity_reference(allow_logo=not plugin._is_cos_action(action))
        refs = ([identity] if identity else []) + list(message_refs)
        persona = getattr(plugin, "persona", None)
        intent = persona.analyze_selfie_intent(action) if persona is not None else None
        if (
            not message_refs and persona is not None and intent is not None
            and not intent.is_cos_look and not intent.is_legs_only and not intent.is_group_photo
            and not intent.change_clothes
        ):
            wardrobe_ref = persona.current_wardrobe_reference()
            if wardrobe_ref is not None:
                refs.append(wardrobe_ref)
        return refs

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
            raw_request = self.extract_command_message(event, command_name, include_quote_text=True)
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
            raw_request = self.extract_command_message(event, command_name, fallback, include_quote_text=True)
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
        suppress_cos_pose: bool = False,
        filter_at_avatars: bool = True,
    ) -> AsyncGenerator[Any, None]:
        plugin = self.plugin
        plugin._claim_media_command_event(event)
        message = (
            message_override.strip()
            if message_override
            else self.extract_command_message(event, command_name, fallback, include_quote_text=True)
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
            filter_at_avatars=filter_at_avatars,
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
        mode_getter = getattr(plugin.persona, "get_identity_mode", None)
        persona_mode = callable(mode_getter) and mode_getter() == "persona"
        if not plugin.persona.has_reference_image() and not persona_mode:
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
                suppress_cos_pose=suppress_cos_pose,
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
                "use_selfie_reference": True,
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
        raw_message = self.extract_command_message(
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
        has_refs = bool(self.extract_image_sources(event))
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

    async def look_legs(self, event: Any, *fallback_parts: str) -> AsyncGenerator[Any, None]:
        """Parse the leg-focus command before using the shared selfie handler."""
        plugin = self.plugin
        fallback_args = " ".join(item for item in fallback_parts if item).strip()
        raw_message = self.extract_command_message(
            event, "看看腿", fallback_args, include_quote_text=True
        )
        raw_extra, requested_count = plugin._extract_command_count(raw_message, allow_trailing=True)
        special_variants, preset_aspect, preset_resolution, preset_name = (
            plugin._expand_dynamic_preset_variants(raw_extra, requested_count)
        )
        if special_variants:
            expanded_extra = special_variants[0]
        else:
            expanded_extra, preset_aspect, preset_resolution, preset_name = plugin._expand_user_text_with_preset(raw_extra)
        has_refs = bool(self.extract_image_sources(event))
        try:
            fallback = plugin._build_leg_focus_action(expanded_extra, has_refs)
        except LegFocusDataError as exc:
            yield event.plain_result(str(exc))
            return
        async for item in plugin._handle_selfie_command(
            event=event,
            command_name="看看腿",
            fallback=fallback,
            default_action=plugin._build_leg_focus_action("", False),
            default_action_with_refs=plugin._build_leg_focus_action("", True),
            progress_label="自拍",
            source="command-look-legs",
            fail_label=plugin._natural_fail_fallback("legs"),
            message_override=fallback,
            requested_count_override=requested_count,
            preset_aspect=preset_aspect,
            preset_resolution=preset_resolution,
            preset_name=preset_name,
            rebuild_extra_request=expanded_extra,
            rebuild_special_preset_variants=special_variants,
        ):
            yield item

    async def look_cos(self, event: Any, *fallback_parts: str) -> AsyncGenerator[Any, None]:
        """Resolve COS set constraints and dynamic presets before dispatch."""
        plugin = self.plugin
        fallback_args = " ".join(item for item in fallback_parts if item).strip()
        raw_message = self.extract_command_message(
            event, "看看COS", fallback_args, include_quote_text=True
        )
        if not raw_message:
            raw_message = self.extract_command_message(
                event, "看看cos", fallback_args, include_quote_text=True
            )
        list_page = parse_cos_look_list_request(raw_message)
        if list_page is not None:
            yield event.plain_result(format_cos_look_list(page=list_page))
            return
        raw_extra, requested_count = plugin._extract_command_count(raw_message, allow_attached=True)
        if cos_query_has_series_constraint(raw_extra) and not match_cos_look_sets(raw_extra):
            yield event.plain_result("未找到匹配的 COS 作品系列或套装，请检查作品名称后重试。")
            return
        special_preset_variants, preset_aspect, preset_resolution, preset_name = (
            plugin._expand_cos_special_preset_variants(raw_extra, requested_count)
        )
        if special_preset_variants:
            expanded_extra = special_preset_variants[0]
        else:
            expanded_extra, preset_aspect, preset_resolution, preset_name = (
                plugin._expand_cos_user_text_with_preset(raw_extra)
            )
        has_refs = bool(self.extract_image_sources(event))
        suppress_cos_pose = (
            preset_name == "动作预设" or prompt_preset_group(preset_name) == "action"
        )
        fallback = plugin._build_cos_look_action(
            expanded_extra, has_refs, match_query=raw_extra,
            suppress_cos_pose=suppress_cos_pose,
        )
        async for item in plugin._handle_selfie_command(
            event=event,
            command_name="看看COS",
            fallback=fallback,
            default_action=plugin._build_cos_look_action("", False),
            default_action_with_refs=plugin._build_cos_look_action("", True),
            progress_label="自拍",
            source="command-look-cos",
            fail_label=plugin._natural_fail_fallback("selfie"),
            message_override=fallback,
            allow_context_fallback=False,
            requested_count_override=requested_count,
            preset_aspect=preset_aspect,
            preset_resolution=preset_resolution,
            preset_name=preset_name,
            rebuild_extra_request=expanded_extra,
            rebuild_match_query=raw_extra,
            rebuild_special_preset_variants=special_preset_variants,
            suppress_cos_pose=suppress_cos_pose,
        ):
            yield item

    async def look_you(self, event: Any, *fallback_parts: str) -> AsyncGenerator[Any, None]:
        """Prepare the third-person selfie command before shared dispatch."""
        plugin = self.plugin
        fallback_args = " ".join(item for item in fallback_parts if item).strip()
        raw_message = self.extract_command_message(
            event, "看看你", fallback_args, include_quote_text=True
        )
        raw_extra, requested_count = plugin._extract_command_count(raw_message, allow_trailing=True)
        special_variants, preset_aspect, preset_resolution, preset_name = (
            plugin._expand_dynamic_preset_variants(raw_extra, requested_count)
        )
        if special_variants:
            expanded_extra = special_variants[0]
        else:
            expanded_extra, preset_aspect, preset_resolution, preset_name = plugin._expand_user_text_with_preset(raw_extra)
        has_refs = bool(self.extract_image_sources(event))
        await plugin._ensure_daily_selfie_schedule(expanded_extra or "看看你", has_refs)
        fallback = plugin._build_third_person_look_action(expanded_extra, has_refs)
        async for item in plugin._handle_selfie_command(
            event=event,
            command_name="看看你",
            fallback=fallback,
            default_action=plugin._build_third_person_look_action("", False),
            default_action_with_refs=plugin._build_third_person_look_action("", True),
            progress_label="自拍",
            source="command-look-you",
            fail_label=plugin._natural_fail_fallback("selfie"),
            message_override=fallback,
            requested_count_override=requested_count,
            preset_aspect=preset_aspect,
            preset_resolution=preset_resolution,
            preset_name=preset_name,
            rebuild_extra_request=expanded_extra,
            rebuild_special_preset_variants=special_variants,
        ):
            yield item

    async def group_selfie(self, event: Any, *fallback_parts: str) -> AsyncGenerator[Any, None]:
        """Prepare group-photo references and dispatch the shared selfie handler."""
        plugin = self.plugin
        fallback = " ".join(item for item in fallback_parts if item).strip()
        raw_message = self.extract_command_message(
            event, ("合影", "合照"), fallback, include_quote_text=True
        )
        raw_message, requested_count = plugin._extract_command_count(raw_message, allow_trailing=True)
        special_variants, preset_aspect, preset_resolution, preset_name = (
            plugin._expand_dynamic_preset_variants(raw_message, requested_count)
        )
        if special_variants:
            expanded_message = special_variants[0]
        else:
            expanded_message, preset_aspect, preset_resolution, preset_name = plugin._expand_user_text_with_preset(raw_message)
        action = plugin._build_group_selfie_action(
            expanded_message,
            bool(self.extract_image_sources(event, include_at_avatar=True)),
        )
        async for item in plugin._handle_selfie_command(
            event=event,
            command_name=("合影", "合照"),
            fallback=fallback,
            default_action=plugin._build_group_selfie_action("", False),
            default_action_with_refs=plugin._build_group_selfie_action("", True),
            progress_label="合影",
            source="command-group-selfie",
            fail_label=plugin._natural_fail_fallback("group"),
            message_override=action,
            include_at_avatar=True,
            filter_at_avatars=False,
            requested_count_override=requested_count,
            preset_aspect=preset_aspect,
            preset_resolution=preset_resolution,
            preset_name=preset_name,
            rebuild_extra_request=expanded_message,
            rebuild_special_preset_variants=special_variants,
        ):
            yield item
