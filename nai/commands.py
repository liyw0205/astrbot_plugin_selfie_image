"""NAI command handling with explicit model routing and shared task delivery."""

from __future__ import annotations

import os
import re
from dataclasses import replace

from astrbot.api import logger

from ..core.providers import novelai_model_family
from ..core.utils import event_user_id, extract_command_message, load_json_file, save_json_file
from ..prompts.prompt_translation import parse_prompt_en_response
from .prompts import compose_nai_prompt, extract_artist_request, select_nai_targets


class NaiCommandMixin:
    def _nai_session_artist(self, event, value=None):
        path = os.path.join(self.data_dir, "nai_artists_by_session.json")
        key = self._session_key(event)
        with self._session_model_lock:
            selections = load_json_file(path)
            if not isinstance(selections, dict):
                selections = {}
            if value is None:
                return str(selections.get(key) or "")
            if value:
                selections[key] = str(value)
            else:
                selections.pop(key, None)
            save_json_file(path, selections)
        return str(value or "")

    async def _handle_nai_command(self, event, fallback: str, *, v5_only: bool = False):
        self._claim_media_command_event(event)
        command = "nai5" if v5_only else "nai"
        message = extract_command_message(event, (command, command.upper()), fallback, include_quote_text=True)
        message, requested_count = self._extract_command_count(message, allow_trailing=True)
        error = self._quota_error_message(event, requested_count) or self._rate_limit_error_message(event)
        if error:
            yield event.plain_result(error)
            return
        manager = self.nai_artist_presets
        manager.load()
        if message in {"画师", "画师串", "画师列表"}:
            names = "、".join(name for name, _ in manager.list()) or "暂无"
            yield event.plain_result(f"NAI 画师串：{names}\n用法：/{command} [画师名] 提示词；仅发送画师名可设为本会话默认。")
            return
        if message in {"默认", "恢复默认", "重置画师预设"}:
            self._nai_session_artist(event, "")
            yield event.plain_result("已恢复本会话默认 NAI 画师串。")
            return
        try:
            prompt, artist_name = extract_artist_request(message, manager)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        prompt, aspect, resolution = self._parse_prompt_options(prompt)
        refs, source_count, failed_count = await self._event_reference_images_with_stats(
            event,
            include_at_avatar=True,
            context_hint=prompt,
            allow_context_fallback=True,
            include_persona=False,
        )
        if not refs and source_count and failed_count:
            yield event.plain_result(self._reference_failure_message(event))
            return
        if artist_name and not prompt and not refs:
            self._nai_session_artist(event, artist_name)
            yield event.plain_result(f"已将本会话 NAI 画师串设为：{artist_name}")
            return
        targets = select_nai_targets(self.config.get_enabled_image_targets(), v5_only=v5_only)
        if not targets:
            model_label = "NAI5" if v5_only else "NAI4 / NAI5"
            yield event.plain_result(f"没有已启用的 {model_label} 模型，请在渠道中启用对应模型，无需加入模型优先级。")
            return
        if not prompt and not refs:
            yield event.plain_result(f"请输入提示词或附带参考图，例如：/{command} [柔和色彩] 樱花树下的少女")
            return
        if not prompt:
            prompt = str(manager.settings["reference_prompt"])
        selected_artist = artist_name or self._nai_session_artist(event)
        selected_artist, artist = manager.find_artist(selected_artist)
        if not artist:
            selected_artist, artist = manager.find_artist(manager.settings.get("default_artist", ""))
        quality = str(manager.settings.get("quality_prompt") or "")
        preview_prompt = compose_nai_prompt(prompt, artist, quality)
        # Keep artist weights intact and pass references directly to NAI.
        targets = [replace(target, extra={
            **target.extra, "prompt_en_enabled": False, "image_to_text_enabled": False,
        }) for target in targets]
        source = "command-nai5" if v5_only else "command-nai"

        async def runner(task_id):
            user_prompt = prompt
            translation = {"enabled": False, "applied": False, "scope": "nai_user_tags"}
            if novelai_model_family(targets[0].model) != "nai5" and re.search(r"[\u3400-\u9fff]", prompt):
                template = str(manager.settings.get("translation_template") or "")
                if template:
                    try:
                        translated = await self._call_text_llm(
                            event, template.replace("{prompt}", prompt), timeout=45, use_persona=False,
                        )
                        translated = parse_prompt_en_response(translated)
                    except Exception:
                        logger.warning("[SelfieImage/NAI] tag translation failed; using original description")
                        translated = ""
                    translation.update({"enabled": True, "applied": bool(translated)})
                    if translated:
                        user_prompt = translated
            final_prompt = compose_nai_prompt(user_prompt, artist, quality)

            async def run_one(_index):
                return await self._run_image_generation(
                    final_prompt, aspect, resolution, refs,
                    targets=targets, source=source,
                    audit_user_id=event_user_id(event), event=event,
                    original_prompt=message, prompt_en_meta=translation,
                    record_context={"task_id": task_id, "requested_count": requested_count},
                )

            return await self._run_counted_generation_shots(
                task_id=task_id, event=event, total=requested_count,
                fail_label="NAI 生图失败", run_one=run_one, log_prefix="nai batch",
            )

        self.start_command_image_task(
            event, source=source,
            summary={
                "original_prompt": message, "final_prompt": preview_prompt,
                "aspect_ratio": aspect, "resolution": resolution,
                "requested_count": requested_count, "reference_image_count": len(refs),
                "nai_artist": selected_artist, "models": [target.label for target in targets],
            },
            runner=runner,
        )
        progress = f"NAI 正在生成 {requested_count} 张图片，模型：{targets[0].model}"
        if selected_artist:
            progress += f"，画师串：{selected_artist}"
        self._record_bot_text_context(event, progress)
        yield event.plain_result(progress + "。")
