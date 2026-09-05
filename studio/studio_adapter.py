"""Plugin adapter methods for the server-side studio canvas."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from ..cos.cos_looks import list_cos_look_sets
from ..prompts.prompt_composition import build_prompt_with_reference_instruction
from ..core.providers import ImageReference
from .studio import (
    BUILTIN_PROMPTS,
    build_studio_action,
    global_prompt_presets,
    list_studio_templates,
    normalize_template_id,
    resolve_slot_refs_for_run,
)
from ..core.utils import (
    data_url_to_bytes,
    detect_mime_by_bytes,
    normalize_image_mime,
    redact_sensitive_data,
    redact_sensitive_text,
)


class StudioMixin:
    # --- Studio / 画布 ---
    def studio_list(self) -> Dict[str, Any]:
        # Ensure default presets are seeded for picker / QQ /预设
        try:
            self.presets.load()
        except Exception:
            pass
        return {
            "sessions": self.studio.list_sessions(),
            "builtin_prompts": BUILTIN_PROMPTS,
            "templates": list_studio_templates(),
            "prompt_presets": self.list_prompt_presets_for_web(),
            "cos_look_sets": self.list_cos_look_sets_for_web(),
        }

    def list_prompt_presets_for_web(self) -> List[Dict[str, Any]]:
        """Merged builtin global + user image_presets for 画布/试画 picker."""
        merged: Dict[str, Dict[str, Any]] = {}
        for item in global_prompt_presets():
            name = str(item.get("name") or item.get("title") or "").strip()
            if not name:
                continue
            try:
                if self.presets.is_builtin_deleted(name):
                    continue
            except Exception:
                pass
            merged[name] = dict(item)
            merged[name]["name"] = name
            merged[name]["title"] = name
        try:
            for item in self.presets.list_public():
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                # user file wins on same name (may already include seeded builtins)
                row = dict(item)
                row["name"] = name
                row["title"] = name
                if name in merged and row.get("source") == "user":
                    row["source"] = "preset"
                merged[name] = row
        except Exception:
            pass
        rows = list(merged.values())
        rows.sort(key=lambda r: str(r.get("name") or ""))
        return rows

    def list_managed_prompt_presets_for_web(self) -> List[Dict[str, str]]:
        """Return the persisted preset set used by the preset management page."""
        self.presets.load()
        return self.presets.list_management()

    def save_prompt_preset_from_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.presets.load()
        ok, message = self.presets.save_management(payload or {})
        if not ok:
            raise ValueError(message)
        return {"message": message, "presets": self.presets.list_management()}

    def delete_prompt_preset_from_web(self, name: str) -> Dict[str, Any]:
        self.presets.load()
        ok, message = self.presets.remove(name)
        if not ok:
            raise ValueError(message)
        return {"message": message, "presets": self.presets.list_management()}

    def import_prompt_presets_from_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.presets.load()
        source = payload.get("presets") if isinstance(payload, dict) else None
        if source is None and isinstance(payload, dict):
            source = payload.get("items")
        imported, message = self.presets.import_management(source)
        return {"message": message, "imported": imported, "presets": self.presets.list_management()}

    def list_cos_look_sets_for_web(self) -> List[Dict[str, str]]:
        """Expose the command COS pool to the canvas and quick-test pickers."""
        return list_cos_look_sets()

    def studio_get(self, session_id: str) -> Dict[str, Any]:
        return self.studio.get(session_id)

    def studio_create(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        title = str(payload.get("title") or "").strip()
        template = str(payload.get("template") or payload.get("template_id") or "").strip()
        use_group = payload.get("use_group_template", None)
        if isinstance(use_group, str):
            use_group = use_group.strip().lower() not in {"0", "false", "no", "off", "否"}
        tid = normalize_template_id(template, use_group_template=use_group if template == "" else None)
        session = self.studio.create(title, template=tid, use_group_template=use_group if not template else None)
        # Prefill identity/base from persona when template wants it
        graph = session.get("graph") or {}
        if graph.get("use_persona_identity") and self.persona.has_reference_image():
            ref = self.persona.get_reference_image()
            if ref and ref.get("data"):
                rel = self._save_cache_image(ref["data"], "studio", ref.get("mime_type") or "image/png")
                identity = next(
                    (
                        s
                        for s in session.get("slots") or []
                        if s.get("role") in {"identity", "base"}
                    ),
                    None,
                )
                if identity:
                    session = self.studio.set_slot_image(
                        session["id"],
                        identity["id"],
                        image_path=rel,
                        source="persona",
                        mime=str(ref.get("mime_type") or "image/png"),
                    )
        return session

    def studio_delete(self, session_id: str) -> Dict[str, Any]:
        self.studio.delete(session_id)
        return {"deleted": True, "id": session_id}

    def studio_update(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        patch = payload.get("graph") if isinstance(payload.get("graph"), dict) else payload
        if "title" in payload and "title" not in patch:
            patch = dict(patch)
            patch["title"] = payload.get("title")
        return self.studio.update_graph(session_id, patch)

    def studio_set_slot(self, session_id: str, slot_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        if payload.get("clear"):
            return self.studio.clear_slot(session_id, slot_id)
        # from existing cache path
        from_path = str(payload.get("image_path") or payload.get("path") or "").strip()
        if from_path:
            info = self.get_cached_image_info(from_path)
            if not info.get("exists") or info.get("is_image") is False:
                raise ValueError("图片不存在或不是有效图片")
            return self.studio.set_slot_image(
                session_id,
                slot_id,
                image_path=from_path,
                source=str(payload.get("source") or "record"),
                mime=str(info.get("mime_type") or ""),
                label=str(payload.get("label") or ""),
            )
        raw = payload.get("image") or payload.get("data_url") or ""
        data, mime = data_url_to_bytes(str(raw or ""))
        if not data:
            raise ValueError("请提供图片 data_url 或 image_path")
        max_bytes = self.config.image_max_image_size_mb * 1024 * 1024
        if len(data) > max_bytes:
            raise ValueError(f"参考图过大，最大允许 {self.config.image_max_image_size_mb}MB")
        mime = normalize_image_mime(mime or detect_mime_by_bytes(data))
        rel = self._save_cache_image(data, "studio", mime)
        return self.studio.set_slot_image(
            session_id,
            slot_id,
            image_path=rel,
            source=str(payload.get("source") or "upload"),
            mime=mime,
            label=str(payload.get("label") or ""),
        )

    def studio_add_slot(self, session_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        return self.studio.add_slot(
            session_id,
            role=str(payload.get("role") or "extra"),
            label=str(payload.get("label") or ""),
        )

    def studio_reorder(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        order = payload.get("order") or payload.get("input_order") or []
        if not isinstance(order, list):
            raise ValueError("order 必须是数组")
        return self.studio.reorder_slots(session_id, order)

    def studio_promote(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        result_id = str(payload.get("result_id") or "").strip()
        if not result_id:
            raise ValueError("需要 result_id")
        role = str(payload.get("role") or "").strip()
        slot_id = str(payload.get("slot_id") or "").strip()
        if role:
            return self.studio.promote_result_to_role(
                session_id,
                result_id,
                role,
                create_if_missing=payload.get("create_if_missing", True) is not False,
            )
        if not slot_id:
            raise ValueError("需要 slot_id 或 role")
        return self.studio.promote_result_to_slot(session_id, result_id, slot_id)

    def studio_gallery_images(self, limit: int = 24) -> Dict[str, Any]:
        """Recent successful generated images from records for 画布「从记录选图」."""
        try:
            limit_n = max(1, min(48, int(limit or 24)))
        except Exception:
            limit_n = 24
        items: List[Dict[str, Any]] = []
        seen = set()
        for record in self.get_recent_records():
            if not record.get("success"):
                continue
            resp = record.get("response_data") if isinstance(record.get("response_data"), dict) else {}
            req = record.get("request_data") if isinstance(record.get("request_data"), dict) else {}
            paths = list(resp.get("generated_image_paths") or resp.get("image_paths") or [])
            if not paths:
                # some older shapes
                paths = list(record.get("generated_image_paths") or [])
            for path in paths:
                text = str(path or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                info = self.get_cached_image_info(text)
                if not info.get("exists"):
                    continue
                items.append(
                    {
                        "path": text,
                        "record_id": record.get("id"),
                        "created_at": record.get("created_at") or record.get("time") or "",
                        "model": resp.get("model") or req.get("model") or "",
                        "prompt": str(req.get("original_prompt") or req.get("prompt") or "")[:80],
                        "source": record.get("source") or "",
                    }
                )
                if len(items) >= limit_n:
                    return {"items": items, "count": len(items)}
        return {"items": items, "count": len(items)}

    def start_studio_run(self, session_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Queue a studio generation using current session slots + graph."""
        payload = payload if isinstance(payload, dict) else {}
        session = self.studio.get(session_id)
        # Prevent double-submit while last run still running
        last = session.get("last_run") if isinstance(session.get("last_run"), dict) else {}
        if str(last.get("status") or "") == "running":
            task_id = str(last.get("task_id") or "").strip()
            if task_id:
                try:
                    existing = self.get_web_image_task(task_id)
                    st = str(existing.get("status") or "")
                    if st in {"queued", "running"}:
                        raise RuntimeError("当前画布正在生成，请稍候或等完成后再点")
                except ValueError:
                    pass
                except RuntimeError:
                    raise
        if isinstance(payload.get("graph"), dict):
            session = self.studio.update_graph(session_id, payload["graph"])
        loop = getattr(self, "loop", None)
        if loop is None or not loop.is_running():
            raise RuntimeError("AstrBot 事件循环未就绪，无法启动画布生成")

        graph = session.get("graph") or {}
        action = build_studio_action(session)
        aspect = str(graph.get("aspect_ratio") or self.config.image_default_aspect_ratio or "9:16")
        resolution = str(graph.get("resolution") or self.config.image_default_resolution or "1K")
        try:
            count = max(1, min(4, int(graph.get("count") or 1)))
        except Exception:
            count = 1
        mode = str(graph.get("mode") or "group")
        persona_ref = self.persona.get_reference_image() if graph.get("use_persona_identity", True) else None
        raw_refs, used_slots = resolve_slot_refs_for_run(
            session,
            persona_ref=persona_ref,
            load_path_bytes=self._load_cache_image_bytes,
        )
        if mode in {"group", "selfie", "i2i"} and not raw_refs:
            raise RuntimeError("请至少放一张参考图，或先设置形象参考图")

        summary = {
            "session_id": session_id,
            "mode": mode,
            "prompt": action,
            "aspect_ratio": aspect,
            "resolution": resolution,
            "count": count,
            "used_slots": used_slots,
            "kind": "studio",
        }
        with self._web_task_lock:
            self._web_task_seq += 1
            task_id = f"web-studio-{int(time.time() * 1000)}-{self._web_task_seq}"
            now = time.time()
            self._web_tasks[task_id] = {
                "task_id": task_id,
                "status": "queued",
                "success": None,
                "error": "",
                "created_ts": now,
                "updated_ts": now,
                "created_at": self._web_task_timestamp(),
                "updated_at": self._web_task_timestamp(),
                "request_data": redact_sensitive_data(dict(summary)),
                "result": None,
                "source": "studio-run",
                "owner_session": "web",
                "cancel_requested": False,
                "studio_session_id": session_id,
            }
            self._prune_web_tasks_locked()
            self._persist_web_tasks_locked()

        self.studio.attach_run_start(session_id, task_id, summary)
        asyncio.run_coroutine_threadsafe(self._run_studio_task(task_id, session_id), loop)
        return self.get_web_image_task(task_id)

    async def _run_studio_task(self, task_id: str, session_id: str) -> None:
        self._set_web_image_task(task_id, status="running", started_ts=time.time(), started_at=self._web_task_timestamp())
        try:
            if self._task_cancel_requested(task_id):
                raise RuntimeError("任务已取消")
            session = self.studio.get(session_id)
            graph = session.get("graph") or {}
            action = build_studio_action(session)
            aspect = str(graph.get("aspect_ratio") or self.config.image_default_aspect_ratio or "9:16")
            resolution = str(graph.get("resolution") or self.config.image_default_resolution or "1K")
            try:
                count = max(1, min(4, int(graph.get("count") or 1)))
            except Exception:
                count = 1
            mode = str(graph.get("mode") or "group").strip().lower() or "group"
            persona_ref = self.persona.get_reference_image() if graph.get("use_persona_identity", True) else None
            raw_refs, _ = resolve_slot_refs_for_run(
                session,
                persona_ref=persona_ref,
                load_path_bytes=self._load_cache_image_bytes,
            )
            refs = [ImageReference(data=data, mime_type=mime) for data, mime in raw_refs]
            if mode in {"group", "selfie", "i2i"} and not refs:
                raise RuntimeError("请至少放一张参考图，或先设置形象参考图")

            if mode in {"group", "selfie"}:
                if mode == "selfie":
                    action = self._normalize_selfie_action(action, bool(refs))
                await self.persona.ensure_daily_selfie_profile(action)
                # refs already include identity first when available; do not re-prepend persona
                has_identity = bool(refs)
                extra_count = max(0, len(refs) - 1) if has_identity else len(refs)
                prompt = self.persona.build_selfie_prompt(
                    action=action,
                    bot_name=self.config.bot_name,
                    personality=self.config.personality,
                    has_reference_image=has_identity,
                    extra_reference_count=extra_count,
                )
                prompt_en_meta: Dict[str, Any] = {"enabled": False, "applied": False, "scope": "user_text_only"}
                if self._prompt_en_needed(action, media="image"):
                    from ..prompts.prompt_templates import build_selfie_builtin_prompt, extract_user_prompt

                    user_text = extract_user_prompt(action)
                    translated_user = ""
                    if user_text:
                        translated_user, prompt_en_meta = await self._translate_prompt_to_english(
                            user_text, media="image", event=None
                        )
                        if not prompt_en_meta.get("applied"):
                            translated_user = ""
                    else:
                        prompt_en_meta.update({"enabled": True, "applied": True, "scope": "builtin_only"})
                    if prompt_en_meta.get("applied"):
                        prompt = build_selfie_builtin_prompt(
                            action,
                            language="en",
                            has_reference_image=has_identity,
                            extra_reference_count=extra_count,
                            appearance_type=self.persona.get_appearance_type(),
                            user_text=translated_user,
                        )
            else:
                user_prompt = action
                prompt_en_meta = {"enabled": False, "applied": False, "scope": "user_text_only"}
                if self._prompt_en_needed(user_prompt, media="image"):
                    translated, prompt_en_meta = await self._translate_prompt_to_english(
                        user_prompt, media="image", event=None
                    )
                    if prompt_en_meta.get("applied") and translated:
                        user_prompt = translated
                prompt = build_prompt_with_reference_instruction(
                    user_prompt,
                    refs,
                    language="en" if self.config.image_enable_image_prompt_en else "zh",
                )

            all_paths: List[str] = []
            last_error = ""
            used_model = ""
            last_result: Dict[str, Any] = {}
            for _ in range(max(1, count)):
                if self._task_cancel_requested(task_id):
                    raise RuntimeError("任务已取消")
                result = await self._run_image_generation(
                    prompt=prompt,
                    aspect_ratio=aspect,
                    resolution=resolution,
                    refs=refs,
                    source="studio-run",
                    original_prompt=action,
                    event=None,
                    prompt_en_meta=prompt_en_meta,
                )
                last_result = result if isinstance(result, dict) else {}
                if not last_result.get("success"):
                    last_error = str(last_result.get("error") or "生成失败")
                    break
                used_model = str(last_result.get("used_model") or used_model)
                for path in last_result.get("image_paths") or last_result.get("generated_image_paths") or []:
                    text = str(path or "").strip()
                    if text:
                        all_paths.append(text)

            success = bool(all_paths) and not last_error
            error = "" if success else (last_error or "生成失败")
            self.studio.attach_run_finish(
                session_id,
                task_id,
                success=success,
                error=error,
                result_paths=all_paths,
                used_model=used_model,
            )
            result_payload = {
                "success": success,
                "error": error,
                "image_paths": all_paths,
                "generated_image_paths": all_paths,
                "used_model": used_model,
                "session_id": session_id,
                "elapsed_seconds": last_result.get("elapsed_seconds"),
            }
            self._set_web_image_task(
                task_id,
                status="succeeded" if success else "failed",
                success=success,
                error=error,
                result=redact_sensitive_data(result_payload),
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
        except Exception as exc:
            error = redact_sensitive_text(str(exc))
            try:
                self.studio.attach_run_finish(session_id, task_id, success=False, error=error, result_paths=[])
            except Exception:
                pass
            cancelled = "取消" in error
            self._set_web_image_task(
                task_id,
                status="cancelled" if cancelled else "failed",
                success=False,
                error=error,
                result={"success": False, "error": error, "session_id": session_id},
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
