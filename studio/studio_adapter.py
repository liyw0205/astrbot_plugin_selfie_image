"""Plugin adapter methods for the server-side studio canvas."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
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
from ..generation.generation_results import build_task_terminal_state


logger = logging.getLogger(__name__)


class StudioMixin:
    def _resolve_studio_refs(
        self,
        session: Dict[str, Any],
        parent_node_id: str,
        *,
        persona_ref: Optional[Dict[str, Any]],
    ) -> tuple[List[tuple[bytes, str]], List[str]]:
        """Resolve the image input for one node generation.

        The root node may use configured slots and the persona fallback. Every
        child node must use its connected parent's generated image instead;
        silently falling back to the persona would break the visual lineage.
        """
        parent_id = str(parent_node_id or "").strip()
        if parent_id:
            canvas = session.get("canvas") if isinstance(session.get("canvas"), dict) else {}
            parent = next(
                (node for node in canvas.get("nodes") or [] if isinstance(node, dict) and str(node.get("id") or "") == parent_id),
                None,
            )
            result = parent.get("result") if isinstance(parent, dict) and isinstance(parent.get("result"), dict) else {}
            path = str(result.get("media_path") or result.get("thumbnail_path") or "").strip()
            if not path:
                raise RuntimeError("上一节点还没有生成图片，无法继续生成")
            loaded = self._load_cache_image_bytes(path)
            if not loaded or not loaded[0]:
                raise RuntimeError("上一节点图片已失效，请先重画上一节点")
            data, mime = loaded
            return [(data, mime or "image/png")], []
        return resolve_slot_refs_for_run(
            session,
            persona_ref=persona_ref,
            load_path_bytes=self._load_cache_image_bytes,
        )

    async def _ensure_studio_failure_record(
        self,
        task_id: str,
        session_id: str,
        *,
        error: str,
        cancelled: bool = False,
        session: Optional[Dict[str, Any]] = None,
        action: str = "",
        prompt: str = "",
        mode: str = "",
        aspect_ratio: str = "",
        resolution: str = "",
        count: int = 1,
        used_slots: Optional[List[str]] = None,
        source_asset_ids: Optional[List[str]] = None,
        reference_image_count: int = 0,
        stage: str = "preflight",
        completed_count: int = 0,
    ) -> None:
        """Persist one inspectable row when Studio fails before generation.

        Normal generation failures are already recorded by
        ``_run_image_generation``.  Waiting first and checking the task links
        keeps this fallback idempotent when an exception races an async record
        commit or occurs after an earlier shot in a batch.
        """
        wait_commits = getattr(self, "_wait_for_record_commits", None)
        if callable(wait_commits):
            await wait_commits(task_id)
        try:
            current = self.get_web_image_task(task_id)
        except Exception:
            current = {}
        linked = current.get("record_ids") if isinstance(current, dict) else []
        if not isinstance(linked, list):
            linked = []
        result = current.get("result") if isinstance(current, dict) else {}
        result_linked = result.get("record_ids") if isinstance(result, dict) else []
        if not isinstance(result_linked, list):
            result_linked = []
        linked_ids = {
            str(item or "").strip()
            for item in [*linked, *result_linked]
            if str(item or "").strip()
        }
        linked_count = len(linked_ids)
        try:
            completed_count = max(0, int(completed_count or 0))
        except (TypeError, ValueError):
            completed_count = 0
        # A task may already have records for earlier shots.  Only suppress
        # the fallback when the current shot has also produced a row; this
        # preserves a failure/cancel row for a later shot that crashed before
        # ``_run_image_generation`` could persist anything.
        if linked_count > completed_count or (linked_count and completed_count == 0):
            return

        request = current.get("request_data") if isinstance(current, dict) else {}
        request = dict(request) if isinstance(request, dict) else {}
        session = session if isinstance(session, dict) else {}
        session_title = str(session.get("title") or "画布").strip() or "画布"
        graph = session.get("graph") if isinstance(session.get("graph"), dict) else {}
        template = str(session.get("template") or graph.get("template") or "").strip()
        action = str(action or request.get("original_prompt") or request.get("prompt") or "").strip()
        prompt = str(prompt or request.get("request_prompt") or action).strip()
        used_slots = [str(item).strip() for item in (used_slots or []) if str(item).strip()][:24]
        source_asset_ids = [str(item).strip() for item in (source_asset_ids or []) if str(item).strip()][:24]
        try:
            reference_image_count = max(0, int(reference_image_count or 0))
        except (TypeError, ValueError):
            reference_image_count = 0
        try:
            count = max(1, min(4, int(count or 1)))
        except (TypeError, ValueError):
            count = 1
        request_data = {
            **request,
            "session_id": session_id,
            "kind": "studio",
            "mode": str(mode or graph.get("mode") or "group").strip().lower() or "group",
            "studio_template": template,
            "studio_session_title": session_title,
            "original_prompt": action,
            "prompt": action,
            "request_prompt": prompt,
            "aspect_ratio": str(aspect_ratio or graph.get("aspect_ratio") or "9:16"),
            "resolution": str(resolution or graph.get("resolution") or "1K"),
            "count": count,
            "requested_count": count,
            "used_slots": used_slots,
            "source_asset_ids": source_asset_ids,
            "reference_image_count": reference_image_count,
            "stage": str(stage or "preflight"),
        }
        response_data = {
            "success": False,
            "stage": str(stage or "preflight"),
            "error": str(error or ("任务已取消" if cancelled else "生成失败")),
            "cancelled": bool(cancelled),
        }
        record = {
            "source": "studio-run",
            "source_label": f"Web/{session_title}",
            "media_type": "image",
            "success": False,
            "generation_success": False,
            "delivery_success": None,
            "status": "cancelled" if cancelled else "failed",
            "cancelled": bool(cancelled),
            "error": response_data["error"],
            "prompt": prompt,
            "original_prompt": action,
            "request_prompt": prompt,
            "final_prompt": prompt,
            "used_model": "",
            "elapsed_seconds": 0,
            "reference_images": reference_image_count,
            "request_data": redact_sensitive_data(request_data),
            "response_data": redact_sensitive_data(response_data),
            "request_image_paths": [],
            "generated_image_paths": [],
            "attempts": [],
            "retry_count": 0,
            "retry_exhausted": False,
            "task_id": task_id,
            "studio_session_id": session_id,
            "studio_task_id": task_id,
            "studio_template": template,
            "studio_session_title": session_title,
            "studio_source_asset_ids": source_asset_ids,
        }
        try:
            self._record_task(record)
        except Exception as exc:
            logger.warning(f"[SelfieImage] Studio 失败记录落库失败: {exc}")
            return
        if callable(wait_commits):
            await wait_commits(task_id)

    # --- Studio / 画布 ---
    def _canvas_store(self, namespace: str = "studio"):
        return self.creative_canvas if str(namespace or "").strip().lower() == "creative" else self.studio

    def studio_list(self, store=None, *, include_metadata: bool = True) -> Dict[str, Any]:
        """Return canvas summaries without forcing the large picker catalogs.

        The embedded dashboard loads prompt presets and COS pools on demand.
        Keeping those catalogs out of the session-list response avoids sending
        hundreds of kilobytes every time a canvas tab is opened.
        """
        canvas_store = store or self.studio
        result = {
            "sessions": canvas_store.list_sessions(),
            "storage_status": canvas_store.storage_status(),
            "builtin_prompts": BUILTIN_PROMPTS,
            "templates": list_studio_templates(),
        }
        if not include_metadata:
            return result
        # Keep the richer payload for direct/plugin callers that still request
        # all picker data in one response.
        self.presets.load()
        result.update({
            "prompt_presets": self.list_prompt_presets_for_web(),
            "prompt_preset_status": self.get_prompt_preset_status_for_web("image"),
            "cos_look_sets": self.list_cos_look_sets_for_web(),
        })
        return result

    def get_prompt_preset_status_for_web(self, kind: str = "image") -> Dict[str, Any]:
        """Expose load failures separately from an intentionally empty list."""
        manager = (
            getattr(self, "video_presets", None)
            if str(kind or "").strip().lower() == "video"
            else getattr(self, "presets", None)
        )
        if manager is None:
            return {"ok": False, "source": "manager", "error": "预设管理器不可用"}
        getter = getattr(manager, "get_load_status", None)
        return getter() if callable(getter) else {"ok": True, "source": "legacy", "error": ""}

    def list_prompt_presets_for_web(self, kind: str = "image") -> List[Dict[str, Any]]:
        """Return the picker presets for one media kind."""
        kind = "video" if str(kind or "").strip().lower() == "video" else "image"
        if kind == "video":
            manager = getattr(self, "video_presets", None)
            if manager is not None:
                manager.load()
                return manager.list_public()
            return []
        merged: Dict[str, Dict[str, Any]] = {}
        for item in global_prompt_presets():
            name = str(item.get("name") or item.get("title") or "").strip()
            if not name:
                continue
            try:
                if self.presets.is_builtin_deleted(name):
                    continue
            except Exception as exc:
                logger.warning("[SelfieImage] failed to inspect deleted preset %s: %s", name, exc)
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
        except Exception as exc:
            logger.exception("[SelfieImage] failed to merge persisted image presets")
            if not merged:
                raise RuntimeError(f"预设列表读取失败：{type(exc).__name__}") from exc
        rows = list(merged.values())
        rows.sort(key=lambda r: str(r.get("name") or ""))
        return rows

    def list_managed_prompt_presets_for_web(self, kind: str = "image") -> List[Dict[str, str]]:
        """Return the persisted preset set used by the preset management page."""
        manager = (getattr(self, "video_presets", self.presets)
                   if str(kind or "").strip().lower() == "video" else self.presets)
        manager.load()
        return manager.list_management()

    def save_prompt_preset_from_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(payload or {})
        manager = (getattr(self, "video_presets", self.presets)
                   if str(payload.get("kind") or payload.get("media_type") or "").strip().lower() == "video" else self.presets)
        manager.load()
        ok, message = manager.save_management(payload)
        if not ok:
            raise ValueError(message)
        return {"message": message, "kind": "video" if manager is getattr(self, "video_presets", None) else "image", "presets": manager.list_management()}

    def delete_prompt_preset_from_web(self, name: str, kind: str = "image") -> Dict[str, Any]:
        manager = (getattr(self, "video_presets", self.presets)
                   if str(kind or "").strip().lower() == "video" else self.presets)
        manager.load()
        ok, message = manager.remove(name)
        if not ok:
            raise ValueError(message)
        return {"message": message, "kind": "video" if manager is getattr(self, "video_presets", None) else "image", "presets": manager.list_management()}

    def import_prompt_presets_from_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(payload or {})
        manager = (getattr(self, "video_presets", self.presets)
                   if str(payload.get("kind") or payload.get("media_type") or "").strip().lower() == "video" else self.presets)
        manager.load()
        source = payload.get("presets") if isinstance(payload, dict) else None
        if source is None and isinstance(payload, dict):
            source = payload.get("items")
        imported, message = manager.import_management(source)
        return {"message": message, "imported": imported, "kind": "video" if manager is getattr(self, "video_presets", None) else "image", "presets": manager.list_management()}

    def list_cos_look_sets_for_web(self) -> List[Dict[str, Any]]:
        """Expose the command COS pool to the canvas and quick-test pickers."""
        return list_cos_look_sets()

    def studio_get(self, session_id: str, store=None) -> Dict[str, Any]:
        return (store or self.studio).get(session_id)

    def studio_canvas(self, session_id: str) -> Dict[str, Any]:
        """Return the drawable relationship graph for one Studio session."""
        return self.studio.canvas_graph(session_id)

    def studio_canvas_update(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("canvas 必须是 JSON 对象")
        return self.studio.update_canvas(session_id, payload)

    def studio_canvas_node_create(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("节点参数必须是 JSON 对象")
        return self.studio.add_canvas_node(session_id, payload)

    def studio_canvas_node_delete(self, session_id: str, node_id: str) -> Dict[str, Any]:
        return self.studio.delete_canvas_node(session_id, node_id)

    def studio_canvas_node_connect(self, session_id: str, node_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("连接参数必须是 JSON 对象")
        parent_id = str(payload.get("parent_id") or "").strip()
        if not parent_id:
            raise ValueError("请选择要连接的前置节点")
        return self.studio.connect_canvas_node(session_id, node_id, parent_id)

    def studio_create(self, payload: Optional[Dict[str, Any]] = None, store=None) -> Dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        canvas_store = store or self.studio
        title = str(payload.get("title") or "").strip()
        template = str(payload.get("template") or payload.get("template_id") or "").strip()
        use_group = payload.get("use_group_template", None)
        if isinstance(use_group, str):
            use_group = use_group.strip().lower() not in {"0", "false", "no", "off", "否"}
        tid = normalize_template_id(template, use_group_template=use_group if template == "" else None)
        session = canvas_store.create(title, template=tid, use_group_template=use_group if not template else None)
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
                    session = canvas_store.set_slot_image(
                        session["id"],
                        identity["id"],
                        image_path=rel,
                        source="persona",
                        mime=str(ref.get("mime_type") or "image/png"),
                    )
        return session

    def studio_copy(self, session_id: str, payload: Optional[Dict[str, Any]] = None, store=None) -> Dict[str, Any]:
        """Copy a canvas session, validating referenced media one slot at a time."""
        payload = payload if isinstance(payload, dict) else {}
        canvas_store = store or self.studio
        source = canvas_store.get(session_id)
        last_run = source.get("last_run") if isinstance(source.get("last_run"), dict) else {}
        if str(last_run.get("status") or "").strip().lower() in {"queued", "running"}:
            raise ValueError("画布任务正在运行，暂时不能复制")
        valid: List[str] = []
        skipped: List[Dict[str, Any]] = []
        for slot in source.get("slots") or []:
            if not isinstance(slot, dict):
                continue
            sid = str(slot.get("id") or "").strip()
            path = str(slot.get("image_path") or "").strip()
            if not path:
                valid.append(sid)
                continue
            info = self.get_cached_image_info(path)
            if info.get("exists") and info.get("is_image") is not False:
                valid.append(sid)
            else:
                skipped.append({"slot_id": sid, "label": str(slot.get("label") or ""), "path": path, "error": "媒体不存在或不可解析"})
        title = str(payload.get("title") or "").strip()[:80]
        result = canvas_store.copy_session(session_id, title=title, valid_slot_ids=valid)
        result["source_session_id"] = str(session_id)
        result["skipped_slots"] = skipped
        return result

    def studio_delete(self, session_id: str, store=None) -> Dict[str, Any]:
        (store or self.studio).delete(session_id)
        return {"deleted": True, "id": session_id}

    def studio_update(self, session_id: str, payload: Dict[str, Any], store=None) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        patch = payload.get("graph") if isinstance(payload.get("graph"), dict) else payload
        if "title" in payload and "title" not in patch:
            patch = dict(patch)
            patch["title"] = payload.get("title")
        return (store or self.studio).update_graph(session_id, patch)

    def studio_set_slot(self, session_id: str, slot_id: str, payload: Dict[str, Any], store=None) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        canvas_store = store or self.studio
        if payload.get("clear"):
            return canvas_store.clear_slot(session_id, slot_id)
        # from existing cache path
        from_path = str(payload.get("image_path") or payload.get("path") or "").strip()
        if from_path:
            info = self.get_cached_image_info(from_path)
            if not info.get("exists") or info.get("is_image") is False:
                raise ValueError("图片不存在或不是有效图片")
            return canvas_store.set_slot_image(
                session_id,
                slot_id,
                image_path=from_path,
                source=str(payload.get("source") or "record"),
                mime=str(info.get("mime_type") or ""),
                label=str(payload.get("label") or ""),
                source_record_id=str(payload.get("source_record_id") or "").strip(),
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
        return canvas_store.set_slot_image(
            session_id,
            slot_id,
            image_path=rel,
            source=str(payload.get("source") or "upload"),
            mime=mime,
            label=str(payload.get("label") or ""),
            source_record_id=str(payload.get("source_record_id") or "").strip(),
        )

    def studio_add_slot(self, session_id: str, payload: Optional[Dict[str, Any]] = None, store=None) -> Dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        return (store or self.studio).add_slot(
            session_id,
            role=str(payload.get("role") or "extra"),
            label=str(payload.get("label") or ""),
        )

    def studio_reorder(self, session_id: str, payload: Dict[str, Any], store=None) -> Dict[str, Any]:
        order = payload.get("order") or payload.get("input_order") or []
        if not isinstance(order, list):
            raise ValueError("order 必须是数组")
        return (store or self.studio).reorder_slots(session_id, order)

    def studio_promote(self, session_id: str, payload: Dict[str, Any], store=None) -> Dict[str, Any]:
        result_id = str(payload.get("result_id") or "").strip()
        if not result_id:
            raise ValueError("需要 result_id")
        role = str(payload.get("role") or "").strip()
        slot_id = str(payload.get("slot_id") or "").strip()
        if role:
            return (store or self.studio).promote_result_to_role(
                session_id,
                result_id,
                role,
                create_if_missing=payload.get("create_if_missing", True) is not False,
            )
        if not slot_id:
            raise ValueError("需要 slot_id 或 role")
        return (store or self.studio).promote_result_to_slot(session_id, result_id, slot_id)

    # Legacy creation canvas API. These wrappers intentionally use a second
    # store and namespace so its sessions cannot be opened by the infinite
    # relationship canvas.
    def creative_canvas_list(self, *, include_metadata: bool = True) -> Dict[str, Any]:
        return self.studio_list(self.creative_canvas, include_metadata=include_metadata)

    def creative_canvas_get(self, session_id: str) -> Dict[str, Any]:
        return self.studio_get(session_id, self.creative_canvas)

    def creative_canvas_create(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.studio_create(payload, self.creative_canvas)

    def creative_canvas_update(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.studio_update(session_id, payload, self.creative_canvas)

    def creative_canvas_delete(self, session_id: str) -> Dict[str, Any]:
        return self.studio_delete(session_id, self.creative_canvas)

    def creative_canvas_set_slot(self, session_id: str, slot_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.studio_set_slot(session_id, slot_id, payload, self.creative_canvas)

    def creative_canvas_add_slot(self, session_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.studio_add_slot(session_id, payload, self.creative_canvas)

    def creative_canvas_reorder(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.studio_reorder(session_id, payload, self.creative_canvas)

    def creative_canvas_promote(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.studio_promote(session_id, payload, self.creative_canvas)

    def start_creative_canvas_run(self, session_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.start_studio_run(
            session_id,
            payload,
            store=self.creative_canvas,
            canvas_namespace="creative",
        )

    def studio_gallery_images(self, limit: int = 24) -> Dict[str, Any]:
        """Recent successful generated images from records for 画布「从记录选图」."""
        try:
            limit_n = max(1, min(100, int(limit or 24)))
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

    def studio_add_asset(self, record_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Put a generated image asset into a canvas, optionally appending a preset."""
        payload = payload if isinstance(payload, dict) else {}
        record_id = str(record_id or "").strip()
        record = self.get_record_for_web(record_id)
        if str(record.get("media_type") or "image").strip().lower() == "video":
            raise ValueError("视频资产不能作为图片参考加入画布")
        response = record.get("response_data") if isinstance(record.get("response_data"), dict) else {}
        paths = list(record.get("generated_image_paths") or response.get("generated_image_paths") or [])
        image_path = str(paths[0] if paths else "").strip()
        if not image_path:
            raise ValueError("该资产没有可用的图片缓存")
        info = self.get_cached_image_info(image_path)
        if not info.get("exists") or info.get("is_image") is False:
            raise ValueError("资产缓存不存在或不是有效图片")

        preset_name = str(payload.get("preset_name") or payload.get("preset") or "").strip()
        preset_prompt = ""
        if preset_name:
            presets = self.list_prompt_presets_for_web("image")
            preset = next((item for item in presets if str(item.get("name") or item.get("title") or "").strip() == preset_name), None)
            if not preset:
                raise ValueError("预设不存在或已删除")
            preset_prompt = str(preset.get("prompt") or "").strip()

        session_id = str(payload.get("session_id") or "").strip()
        created = False
        if session_id:
            session = self.studio.get(session_id)
        else:
            title = str(payload.get("title") or "").strip() or "资产重做"
            session = self.studio_create({"title": title, "template": str(payload.get("template") or "i2i")})
            session_id = str(session.get("id") or "")
            created = True
        slots = [item for item in (session.get("slots") or []) if isinstance(item, dict)]
        slot_id = str(payload.get("slot_id") or "").strip()
        if slot_id and not any(str(item.get("id") or "") == slot_id for item in slots):
            raise ValueError("指定槽位不存在")
        if not slot_id:
            preferred = {"base", "identity", "extra", "style", "detail"}
            target = next((item for item in slots if str(item.get("role") or "") in preferred and not str(item.get("image_path") or "").strip()), None)
            if target is None:
                target = next((item for item in slots if not str(item.get("image_path") or "").strip()), None)
            if target is None:
                session = self.studio_add_slot(session_id, {"role": "extra", "label": "资产参考"})
                target = next(item for item in session.get("slots") or [] if str(item.get("role") or "") == "extra" and not str(item.get("image_path") or "").strip())
            slot_id = str(target.get("id") or "")
        label = str(payload.get("label") or "").strip() or str(image_path.rsplit("/", 1)[-1])[:40]
        session = self.studio.set_slot_image(
            session_id,
            slot_id,
            image_path=image_path,
            source="asset",
            mime=str(info.get("mime_type") or "image/png"),
            label=label,
            source_record_id=record_id,
        )

        applied_preset = ""
        if preset_name:
            graph = session.get("graph") if isinstance(session.get("graph"), dict) else {}
            current_prompt = str(graph.get("prompt") or "").strip()
            if preset_prompt and preset_prompt not in current_prompt:
                combined = "\n\n".join(item for item in (current_prompt, preset_prompt) if item)
                session = self.studio.update_graph(session_id, {"prompt": combined})
            applied_preset = preset_name
        return {
            "session": session,
            "session_id": session_id,
            "slot_id": slot_id,
            "record_id": record_id,
            "created": created,
            "preset_name": applied_preset,
        }

    def studio_add_assets(self, record_ids: Any, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Add several image assets to one canvas without losing per-item errors."""
        payload = payload if isinstance(payload, dict) else {}
        if isinstance(record_ids, (str, bytes)):
            values = [record_ids]
        elif isinstance(record_ids, (list, tuple, set)):
            values = list(record_ids)
        else:
            values = []
        ids = list(dict.fromkeys(str(item or "").strip() for item in values if str(item or "").strip()))
        if not ids:
            raise ValueError("至少选择一项资产")
        if len(ids) > 12:
            raise ValueError("单次最多加入 12 项图片资产")
        common = {
            key: payload[key]
            for key in ("session_id", "slot_id", "preset_name", "preset", "title", "template", "label")
            if key in payload
        }
        added: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        session_id = str(common.get("session_id") or "").strip()
        for record_id in ids:
            item_payload = dict(common)
            if session_id:
                item_payload["session_id"] = session_id
            # A slot is selected automatically for batch input; a caller-provided
            # slot is meaningful only for a single asset.
            if len(ids) > 1:
                item_payload.pop("slot_id", None)
            try:
                result = self.studio_add_asset(record_id, item_payload)
                session_id = str(result.get("session_id") or session_id).strip()
                added.append({
                    "record_id": str(record_id),
                    "slot_id": result.get("slot_id") or "",
                    "preset_name": result.get("preset_name") or "",
                })
            except Exception as exc:
                errors.append({"record_id": str(record_id), "error": redact_sensitive_text(str(exc))})
        if not added:
            raise ValueError(errors[0]["error"] if errors else "没有可加入画布的图片资产")
        session = self.studio.get(session_id) if session_id else None
        return {
            "session": session,
            "session_id": session_id,
            "added": added,
            "errors": errors,
            "added_count": len(added),
            "error_count": len(errors),
        }

    def start_studio_run(
        self,
        session_id: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        store=None,
        canvas_namespace: str = "studio",
    ) -> Dict[str, Any]:
        """Queue a studio generation using current session slots + graph."""
        payload = payload if isinstance(payload, dict) else {}
        canvas_store = store or self.studio
        session = canvas_store.get(session_id)
        parent_node_id = str(payload.get("parent_node_id") or "").strip()
        target_node_id = str(payload.get("target_node_id") or "").strip()
        canvas_snapshot = canvas_store.canvas_graph(session_id)
        canvas_nodes = canvas_snapshot.get("nodes") or []
        nodes_by_id = {str(node.get("id")): node for node in canvas_nodes if isinstance(node, dict)}
        valid_node_ids = set(nodes_by_id)
        if target_node_id and target_node_id not in valid_node_ids:
            raise ValueError("目标节点不存在")
        if parent_node_id:
            if parent_node_id not in valid_node_ids:
                raise ValueError("父节点不存在")
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
            session = canvas_store.update_graph(session_id, payload["graph"])
            canvas_snapshot = canvas_store.canvas_graph(session_id)
            canvas_nodes = canvas_snapshot.get("nodes") or []
            nodes_by_id = {str(node.get("id")): node for node in canvas_nodes if isinstance(node, dict)}

        config_node = nodes_by_id.get(target_node_id or parent_node_id or str(canvas_snapshot.get("root_node_id") or "")) or {}
        node_params = payload.get("node_params") if isinstance(payload.get("node_params"), dict) else config_node.get("params")
        node_params = dict(node_params) if isinstance(node_params, dict) else {}
        template_id = normalize_template_id(
            str(payload.get("template_id") or config_node.get("template_id") or session.get("template") or "")
        )
        template_meta = next(
            (item for item in list_studio_templates() if str(item.get("id") or "") == template_id),
            {},
        )
        effective_session = deepcopy(session)
        effective_graph = dict(effective_session.get("graph") or {})
        effective_graph.update(node_params)
        effective_graph["mode"] = str(template_meta.get("mode") or effective_graph.get("mode") or "group")
        effective_graph["aspect_ratio"] = str(node_params.get("aspect_ratio") or effective_graph.get("aspect_ratio") or "自动")
        effective_graph["resolution"] = str(node_params.get("resolution") or effective_graph.get("resolution") or "1K")
        try:
            effective_graph["count"] = max(1, min(4, int(node_params.get("count") or effective_graph.get("count") or 1)))
        except (TypeError, ValueError):
            effective_graph["count"] = 1
        effective_session["template"] = template_id
        effective_session["graph"] = effective_graph
        loop = getattr(self, "loop", None)
        if loop is None or not loop.is_running():
            raise RuntimeError("AstrBot 事件循环未就绪，无法启动画布生成")

        graph = effective_session.get("graph") or {}
        action = build_studio_action(effective_session)
        aspect = str(graph.get("aspect_ratio") or self.config.image_default_aspect_ratio or "9:16")
        resolution = str(graph.get("resolution") or self.config.image_default_resolution or "1K")
        try:
            count = max(1, min(4, int(graph.get("count") or 1)))
        except Exception:
            count = 1
        mode = str(graph.get("mode") or "group")
        persona_ref = self.persona.get_reference_image() if not parent_node_id and graph.get("use_persona_identity", True) else None
        raw_refs, used_slots = self._resolve_studio_refs(
            effective_session,
            parent_node_id,
            persona_ref=persona_ref,
        )
        if mode in {"group", "selfie", "i2i"} and not raw_refs:
            raise RuntimeError("请至少放一张参考图，或先设置形象参考图")
        source_asset_ids = [
            str(slot.get("source_record_id") or "").strip()
            for slot in (effective_session.get("slots") or [])
            if isinstance(slot, dict)
            and str(slot.get("id") or "") in set(used_slots)
            and str(slot.get("source_record_id") or "").strip()
        ][:24]

        summary = {
            "session_id": session_id,
            "studio_session_title": str(session.get("title") or "画布").strip() or "画布",
            "mode": mode,
            "prompt": action,
            "aspect_ratio": aspect,
            "resolution": resolution,
            "count": count,
            "used_slots": used_slots,
            "source_asset_ids": source_asset_ids,
            "kind": "studio",
            "parent_node_id": parent_node_id or str((canvas_snapshot.get("root_node_id") or "")),
            "target_node_id": target_node_id,
            "reference_node_id": parent_node_id,
            "canvas_namespace": str(canvas_namespace or "studio"),
            "canvas_mode": "creative" if str(canvas_namespace or "").strip().lower() == "creative" else "relationship",
            "template": template_id,
            "graph_params": {
                "prompt": str(graph.get("prompt") or ""),
                "mode": mode,
                "aspect_ratio": aspect,
                "resolution": resolution,
                "count": count,
                "use_persona_identity": bool(graph.get("use_persona_identity", True)),
            },
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
                "canvas_namespace": str(canvas_namespace or "studio"),
                **self._task_runtime_defaults(),
                **self._task_progress_defaults(count),
                "generation_stage": "preflight",
                "generation_stage_label": "准备画布任务",
            }
            self._prune_web_tasks_locked()
            self._persist_web_tasks_locked()

        canvas_store.attach_run_start(session_id, task_id, summary)
        runtime_future = asyncio.run_coroutine_threadsafe(
            self._run_studio_task(task_id, session_id, canvas_namespace=str(canvas_namespace or "studio")),
            loop,
        )
        runtime_tasks = getattr(self, "_runtime_generation_tasks", None)
        if runtime_tasks is None:
            runtime_tasks = {}
            self._runtime_generation_tasks = runtime_tasks
        runtime_tasks[task_id] = runtime_future
        runtime_future.add_done_callback(
            lambda _task, tid=task_id: getattr(self, "_runtime_generation_tasks", {}).pop(tid, None)
        )
        return self.get_web_image_task(task_id)

    async def _run_studio_task(self, task_id: str, session_id: str, *, canvas_namespace: str = "studio") -> None:
        canvas_store = self._canvas_store(canvas_namespace)
        self._set_web_image_task(
            task_id,
            status="running",
            started_ts=time.time(),
            started_at=self._web_task_timestamp(),
            generation_started_ts=time.time(),
            queue_waiting=False,
            queue_position=0,
            generation_stage="preflight",
            generation_stage_label="准备画布任务",
        )
        session: Dict[str, Any] = {}
        action = ""
        prompt = ""
        mode = ""
        aspect = str(getattr(self.config, "image_default_aspect_ratio", "9:16") or "9:16")
        resolution = str(getattr(self.config, "image_default_resolution", "1K") or "1K")
        count = 1
        used_slots: List[str] = []
        source_asset_ids: List[str] = []
        refs: List[ImageReference] = []
        failure_stage = "preflight"
        completed_count = 0
        try:
            if self._task_cancel_requested(task_id):
                raise RuntimeError("任务已取消")
            session = canvas_store.get(session_id)
            last_run = session.get("last_run") if isinstance(session.get("last_run"), dict) else {}
            summary = last_run.get("summary") if isinstance(last_run.get("summary"), dict) else {}
            target_node_id = str(summary.get("target_node_id") or "").strip()
            canvas = session.get("canvas") if isinstance(session.get("canvas"), dict) else {}
            target_node = next(
                (node for node in canvas.get("nodes") or [] if str(node.get("id") or "") == target_node_id),
                None,
            )
            template_id = normalize_template_id(
                str(summary.get("template") or (target_node or {}).get("template_id") or session.get("template") or "")
            )
            template_meta = next(
                (item for item in list_studio_templates() if str(item.get("id") or "") == template_id),
                {},
            )
            node_params = summary.get("graph_params") if isinstance(summary.get("graph_params"), dict) else (target_node or {}).get("params")
            node_params = dict(node_params) if isinstance(node_params, dict) else {}
            session = deepcopy(session)
            graph_override = dict(session.get("graph") or {})
            graph_override.update(node_params)
            graph_override["mode"] = str(template_meta.get("mode") or graph_override.get("mode") or "group")
            session["template"] = template_id
            session["graph"] = graph_override
            graph = session.get("graph") or {}
            action = build_studio_action(session)
            aspect = str(graph.get("aspect_ratio") or self.config.image_default_aspect_ratio or "9:16")
            resolution = str(graph.get("resolution") or self.config.image_default_resolution or "1K")
            try:
                count = max(1, min(4, int(graph.get("count") or 1)))
            except Exception:
                count = 1
            mode = str(graph.get("mode") or "group").strip().lower() or "group"
            # ``parent_node_id`` describes graph insertion; the explicit
            # reference id stays empty for the root even when it is rerun.
            if "reference_node_id" in summary:
                parent_node_id = str(summary.get("reference_node_id") or "").strip()
            else:
                # Compatibility with tasks persisted before reference_node_id
                # was introduced. A root rerun stored itself as parent, while
                # a child generation stored its actual preceding node.
                legacy_parent = str(summary.get("parent_node_id") or "").strip()
                parent_node_id = legacy_parent if legacy_parent and legacy_parent != target_node_id else ""
            persona_ref = self.persona.get_reference_image() if not parent_node_id and graph.get("use_persona_identity", True) else None
            raw_refs, used_slots = self._resolve_studio_refs(
                session,
                parent_node_id,
                persona_ref=persona_ref,
            )
            refs = [ImageReference(data=data, mime_type=mime) for data, mime in raw_refs]
            source_asset_ids = [
                str(slot.get("source_record_id") or "").strip()
                for slot in (session.get("slots") or [])
                if isinstance(slot, dict)
                and str(slot.get("id") or "") in set(used_slots)
                and str(slot.get("source_record_id") or "").strip()
            ][:24]
            if mode in {"group", "selfie", "i2i"} and not refs:
                raise RuntimeError("请至少放一张参考图，或先设置形象参考图")

            failure_stage = "prompt_translation"
            self._set_web_image_task(
                task_id,
                generation_stage="prompt_translation",
                generation_stage_label="处理画布提示词",
            )
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
                    from ..prompts.prompt_templates import (
                        append_daily_context_to_english_prompt,
                        build_selfie_builtin_prompt,
                        extract_user_prompt,
                    )

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
                        source_prompt = prompt
                        prompt = build_selfie_builtin_prompt(
                            action,
                            language="en",
                            has_reference_image=has_identity,
                            extra_reference_count=extra_count,
                            appearance_type=self.persona.get_appearance_type(),
                            user_text=translated_user,
                        )
                        prompt = append_daily_context_to_english_prompt(prompt, source_prompt)
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

            failure_stage = "generating"

            # Keep the canvas request inspectable while it is running.  The
            # initial task summary only contains the user's action; recording
            # the effective prompt here lets the task center show both sides
            # of the prompt transformation before the first result arrives.
            studio_request_data = {
                "session_id": session_id,
                "studio_session_title": str(session.get("title") or "画布").strip() or "画布",
                "kind": "studio",
                "mode": mode,
                "original_prompt": action,
                "prompt": action,
                "request_prompt": prompt,
                "aspect_ratio": aspect,
                "resolution": resolution,
                "count": count,
                "requested_count": count,
                "used_slots": used_slots,
                "source_asset_ids": source_asset_ids,
            }
            self._set_web_image_task(
                task_id,
                request_data=redact_sensitive_data(studio_request_data),
            )

            all_paths: List[str] = []
            last_error = ""
            used_model = ""
            last_result: Dict[str, Any] = {}
            all_attempts: List[Dict[str, Any]] = []
            succeeded_count = 0
            failed_count = 0
            completed_count = 0
            self._set_web_image_task(
                task_id,
                generation_stage="generating",
                generation_stage_label="生成画布结果",
            )
            for index in range(max(1, count)):
                if self._task_cancel_requested(task_id):
                    # A completed shot is evidence even when cancellation was
                    # requested before the next batch item started. Let the
                    # terminal classifier publish a partial result instead of
                    # throwing away the paths already produced.
                    if all_paths:
                        break
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
                    record_context={
                        "task_id": task_id,
                        "studio_session_id": session_id,
                        "studio_session_title": str(session.get("title") or "画布").strip() or "画布",
                        "studio_task_id": task_id,
                        "studio_template": template_id,
                        "studio_source_asset_ids": source_asset_ids,
                    },
                )
                last_result = result if isinstance(result, dict) else {}
                raw_attempts = last_result.get("attempts")
                if isinstance(raw_attempts, list):
                    all_attempts.extend(
                        item for item in raw_attempts if isinstance(item, dict)
                    )
                if not last_result.get("success"):
                    last_error = str(last_result.get("error") or "生成失败")
                    failed_count += 1
                    completed_count += 1
                    self._set_web_image_task(
                        task_id,
                        completed_count=completed_count,
                        succeeded_count=succeeded_count,
                        failed_count=failed_count,
                        progress_percent=int(round(completed_count * 100 / max(1, count))),
                        current_index=index + 1,
                    )
                    break
                succeeded_count += 1
                completed_count += 1
                used_model = str(last_result.get("used_model") or used_model)
                for path in last_result.get("image_paths") or last_result.get("generated_image_paths") or []:
                    text = str(path or "").strip()
                    if text:
                        all_paths.append(text)
                self._set_web_image_task(
                    task_id,
                    completed_count=completed_count,
                    succeeded_count=succeeded_count,
                    failed_count=failed_count,
                    progress_percent=int(round(completed_count * 100 / max(1, count))),
                    current_index=index + 1,
                )

            cancel_requested = self._task_cancel_requested(task_id)
            terminal = build_task_terminal_state(
                {
                    "success": bool(all_paths) and not last_error,
                    "error": last_error,
                    "status": (
                        "partial_success"
                        if failed_count and succeeded_count
                        else "failed"
                        if last_error
                        else ""
                    ),
                    "files": all_paths,
                    "image_paths": all_paths,
                    "requested_count": count,
                    "completed_count": completed_count,
                    "succeeded_count": succeeded_count,
                    "failed_count": failed_count,
                },
                requested_count=count,
                cancel_requested=cancel_requested,
            )
            if cancel_requested and not terminal["cancelled_result"]:
                # A late result won the race; cancellation is no longer an
                # active request and must not remain on the terminal snapshot.
                self._set_web_image_task(task_id, cancel_requested=False)
            success = terminal["success"]
            error = terminal["error"]
            terminal_stage = terminal["terminal_stage"]
            terminal_stage_label = (
                "已取消"
                if terminal_stage == "cancelled"
                else "部分完成"
                if terminal["partial_success"]
                else "已完成"
                if terminal_stage == "complete"
                else "已失败"
            )
            wait_commits = getattr(self, "_wait_for_record_commits", None)
            if callable(wait_commits):
                await wait_commits(task_id)
            canvas_store.attach_run_finish(
                session_id,
                task_id,
                success=success,
                error=error,
                result_paths=all_paths,
                used_model=used_model,
                source_asset_ids=source_asset_ids,
                status=terminal["terminal_status"],
            )
            request_data = dict(studio_request_data)
            if isinstance(last_result.get("request_data"), dict):
                request_data.update(last_result["request_data"])
            # The generation helper may translate or otherwise normalize the
            # prompt per channel, so prefer its final value when available.
            original_prompt = str(last_result.get("original_prompt") or action)
            final_prompt = str(last_result.get("final_prompt") or prompt)
            request_data.update(
                {
                    "original_prompt": original_prompt,
                    "prompt": original_prompt,
                    "request_prompt": str(
                        last_result.get("request_prompt")
                        or request_data.get("request_prompt")
                        or final_prompt
                    ),
                }
            )
            result_payload = {
                "success": success,
                "error": error,
                "status": terminal["terminal_status"],
                "cancelled": terminal["cancelled"],
                "generation_success": terminal["generation_success"],
                "delivery_failed": terminal["delivery_failed"],
                "delivery_unknown": terminal["delivery_unknown"],
                "image_paths": all_paths,
                "generated_image_paths": all_paths,
                "original_prompt": original_prompt,
                "final_prompt": final_prompt,
                "request_prompt": request_data.get("request_prompt") or final_prompt,
                "request_data": redact_sensitive_data(request_data),
                "attempts": all_attempts,
                "used_model": used_model,
                "session_id": session_id,
                "elapsed_seconds": last_result.get("elapsed_seconds"),
                "requested_count": count,
                "completed_count": completed_count,
                "succeeded_count": succeeded_count,
                "failed_count": failed_count,
            }
            self._set_web_image_task(
                task_id,
                status=terminal["terminal_status"],
                success=success,
                generation_stage=terminal_stage,
                generation_stage_label=terminal_stage_label,
                error=error,
                requested_count=count,
                completed_count=completed_count,
                succeeded_count=succeeded_count,
                failed_count=failed_count,
                progress_percent=int(round(completed_count * 100 / max(1, count))),
                current_index=completed_count,
                result=redact_sensitive_data(result_payload),
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
        except asyncio.CancelledError:
            error = "任务已取消"
            await self._ensure_studio_failure_record(
                task_id,
                session_id,
                error=error,
                cancelled=True,
                session=session,
                action=action,
                prompt=prompt,
                mode=mode,
                aspect_ratio=aspect,
                resolution=resolution,
                count=count,
                used_slots=used_slots,
                source_asset_ids=source_asset_ids,
                reference_image_count=len(refs),
                stage="cancelled",
                completed_count=completed_count,
            )
            wait_commits = getattr(self, "_wait_for_record_commits", None)
            if callable(wait_commits):
                await wait_commits(task_id)
            try:
                canvas_store.attach_run_finish(session_id, task_id, success=False, error=error, result_paths=[], status="cancelled")
            except Exception:
                pass
            self._set_web_image_task(
                task_id,
                status="cancelled",
                success=False,
                generation_stage="cancelled",
                generation_stage_label="已取消",
                error=error,
                result={"success": False, "error": error, "cancelled": True, "session_id": session_id},
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
            return
        except Exception as exc:
            error = redact_sensitive_text(str(exc))
            cancelled = "取消" in error
            await self._ensure_studio_failure_record(
                task_id,
                session_id,
                error=error,
                cancelled=cancelled,
                session=session,
                action=action,
                prompt=prompt,
                mode=mode,
                aspect_ratio=aspect,
                resolution=resolution,
                count=count,
                used_slots=used_slots,
                source_asset_ids=source_asset_ids,
                reference_image_count=len(refs),
                stage="cancelled" if cancelled else failure_stage,
                completed_count=completed_count,
            )
            wait_commits = getattr(self, "_wait_for_record_commits", None)
            if callable(wait_commits):
                await wait_commits(task_id)
            try:
                canvas_store.attach_run_finish(session_id, task_id, success=False, error=error, result_paths=[], status="cancelled" if cancelled else "failed")
            except Exception:
                pass
            self._set_web_image_task(
                task_id,
                status="cancelled" if cancelled else "failed",
                success=False,
                generation_stage="cancelled" if cancelled else "failed",
                generation_stage_label="已取消" if cancelled else "已失败",
                error=error,
                result={"success": False, "error": error, "session_id": session_id},
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
