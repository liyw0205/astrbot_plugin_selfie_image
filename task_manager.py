"""Persistent web generation task lifecycle mixin."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any, Dict, Optional

from .utils import (
    load_json_file,
    redact_sensitive_data,
    redact_sensitive_text,
    save_json_file,
)


class WebTaskMixin:
    def _web_task_timestamp(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    def _load_web_tasks(self) -> Dict[str, Dict[str, Any]]:
        data = load_json_file(self.tasks_path)
        if not isinstance(data, dict):
            return {}
        raw_tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
        tasks: Dict[str, Dict[str, Any]] = {}
        expired_on_start = False
        for task_id, raw in raw_tasks.items():
            if not isinstance(raw, dict):
                continue
            task = copy.deepcopy(raw)
            task["task_id"] = str(task.get("task_id") or task_id)
            if task.get("status") in {"queued", "running"}:
                task["status"] = "expired"
                task["success"] = False
                task["error"] = "插件重启后未恢复该任务，请重新提交"
                task["finished_ts"] = time.time()
                task["finished_at"] = self._web_task_timestamp()
                expired_on_start = True
            tasks[task["task_id"]] = task
        if expired_on_start:
            save_json_file(self.tasks_path, {"tasks": tasks})
        return tasks

    def _persist_web_tasks_locked(self) -> None:
        path = str(getattr(self, "tasks_path", "") or "").strip()
        if path:
            save_json_file(path, {"tasks": self._web_tasks})

    def _request_fingerprint(
        self, payload: Mapping[str, Any], owner_session: str = ""
    ) -> str:
        """Build a short-lived dedupe key without persisting request contents."""
        image_values = list(payload.get("images") or [])
        if payload.get("image"):
            image_values.append(payload.get("image"))
        image_hashes = [
            hashlib.sha256(str(value or "").encode("utf-8", "ignore")).hexdigest()[:24]
            for value in image_values
        ]
        fields = {
            "owner_session": str(owner_session or ""),
            "media_type": str(payload.get("media_type") or "image").strip().lower(),
            "prompt": str(payload.get("prompt") or payload.get("original_prompt") or "").strip(),
            "channel": str(payload.get("channel") or "").strip(),
            "model": str(payload.get("model") or "").strip(),
            "aspect_ratio": str(payload.get("aspect_ratio") or "").strip(),
            "resolution": str(payload.get("resolution") or "").strip(),
            "duration": str(payload.get("duration") or "").strip(),
            "count": str(payload.get("count") or 1).strip(),
            "prompt_enhance": str(payload.get("prompt_enhance") or "").strip().lower(),
            "image_hashes": image_hashes,
        }
        encoded = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8", "ignore")).hexdigest()[:24]

    def _find_recent_duplicate_task_locked(
        self, fingerprint: str, *, now: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        if not fingerprint:
            return None
        current = float(now or time.time())
        for task in self._web_tasks.values():
            if not isinstance(task, dict) or task.get("request_fingerprint") != fingerprint:
                continue
            if task.get("status") not in {"queued", "running"}:
                continue
            if current - float(task.get("created_ts") or 0) <= 120:
                return copy.deepcopy(task)
        return None

    def _summarize_web_test_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_images = list(payload.get("images") or [])
        if payload.get("image"):
            raw_images.append(payload.get("image"))
        media_type = str(payload.get("media_type") or "image").strip().lower()
        if media_type == "video":
            return {
                "media_type": "video",
                "original_prompt": str(payload.get("prompt") or "").strip()
                or "一段自然流畅的短视频",
                "channel": str(payload.get("channel") or "").strip(),
                "model": str(payload.get("model") or "").strip(),
                "aspect_ratio": str(payload.get("aspect_ratio") or "16:9"),
                "duration": int(
                    payload.get("duration") or self.config.video_default_duration or 5
                ),
                "raw_reference_image_count": len(raw_images),
            }
        prompt_enhance_raw = payload.get("prompt_enhance", True)
        prompt_enhance = not (
            prompt_enhance_raw is False
            or str(prompt_enhance_raw).strip().lower()
            in {"false", "0", "no", "off", "关闭", "否"}
        )
        return {
            "original_prompt": str(payload.get("prompt") or "").strip()
            or "看着镜头自然自拍",
            "channel": str(payload.get("channel") or "").strip(),
            "model": str(payload.get("model") or "").strip(),
            "aspect_ratio": str(
                payload.get("aspect_ratio")
                or self.config.image_default_aspect_ratio
                or "9:16"
            ),
            "resolution": str(
                payload.get("resolution") or self.config.image_default_resolution or "1K"
            ),
            "prompt_enhance": prompt_enhance,
            "use_selfie_reference": bool(payload.get("use_selfie_reference")),
            "raw_reference_image_count": len(raw_images),
        }

    def _prune_web_tasks_locked(self) -> None:
        if len(self._web_tasks) <= 50:
            return
        finished = [
            (float(task.get("updated_ts") or 0), task_id)
            for task_id, task in self._web_tasks.items()
            if task.get("status")
            in {"succeeded", "partial_success", "failed", "cancelled", "expired"}
        ]
        finished.sort(key=lambda item: item[0])
        while len(self._web_tasks) > 50 and finished:
            _, task_id = finished.pop(0)
            self._web_tasks.pop(task_id, None)

    def _set_web_image_task(self, task_id: str, **fields: Any) -> None:
        with self._web_task_lock:
            task = self._web_tasks.get(task_id)
            if not task:
                return
            now = time.time()
            task.update(fields)
            task["updated_ts"] = now
            task["updated_at"] = self._web_task_timestamp()
            self._prune_web_tasks_locked()
            self._persist_web_tasks_locked()

    def get_web_image_task(self, task_id: str) -> Dict[str, Any]:
        with self._web_task_lock:
            task = self._web_tasks.get(str(task_id or "").strip())
            if not task:
                raise ValueError("任务不存在或已清理")
            data = copy.deepcopy(task)
        if data.get("status") in {"queued", "running"}:
            started = float(data.get("started_ts") or data.get("created_ts") or time.time())
            data["running_seconds"] = round(max(0.0, time.time() - started), 2)
        return redact_sensitive_data(data)

    def start_web_image_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise RuntimeError("请求体必须是 JSON 对象")
        loop = getattr(self, "loop", None)
        if loop is None or not loop.is_running():
            raise RuntimeError("AstrBot 事件循环未就绪，无法启动后台生图任务")
        payload_copy = copy.deepcopy(payload)
        media_type = str(payload_copy.get("media_type") or "image").strip().lower()
        if media_type not in {"image", "video"}:
            raise RuntimeError("media_type 必须是 image 或 video")
        self._validate_web_test_selection(payload_copy)
        force_regenerate = bool(
            payload_copy.get("force_regenerate") or payload_copy.get("force")
        )
        fingerprint = self._request_fingerprint(payload_copy, "web")
        with self._web_task_lock:
            if not force_regenerate:
                duplicate = self._find_recent_duplicate_task_locked(fingerprint)
                if duplicate:
                    duplicate["deduplicated"] = True
                    return redact_sensitive_data(duplicate)
            self._web_task_seq += 1
            task_id = f"web-{int(time.time() * 1000)}-{self._web_task_seq}"
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
                "request_data": self._summarize_web_test_payload(payload_copy),
                "result": None,
                "source": "web-video-test" if media_type == "video" else "web-test",
                "owner_session": "web",
                "cancel_requested": False,
                "request_fingerprint": fingerprint,
                "deduplicated": False,
            }
            self._prune_web_tasks_locked()
            self._persist_web_tasks_locked()
        asyncio.run_coroutine_threadsafe(
            self._run_web_image_task(task_id, payload_copy), loop
        )
        return self.get_web_image_task(task_id)

    async def _run_web_image_task(self, task_id: str, payload: Dict[str, Any]) -> None:
        self._set_web_image_task(
            task_id,
            status="running",
            started_ts=time.time(),
            started_at=self._web_task_timestamp(),
        )
        try:
            if self._task_cancel_requested(task_id):
                raise RuntimeError("任务已取消")
            media_type = str(payload.get("media_type") or "image").strip().lower()
            result = await (
                self.web_test_video(payload)
                if media_type == "video"
                else self.web_test_image(payload)
            )
            result = self._normalize_generation_result(result, payload.get("count") or 1)
            result = redact_sensitive_data(result)
            if self._task_cancel_requested(task_id):
                self._set_web_image_task(
                    task_id,
                    status="cancelled",
                    success=False,
                    error="任务已取消",
                    result={"success": False, "error": "任务已取消"},
                    finished_ts=time.time(),
                    finished_at=self._web_task_timestamp(),
                )
                return
            success = bool(result.get("success"))
            error = (
                ""
                if success
                else redact_sensitive_text(str(result.get("error") or "这次没顺好"))
            )
            self._set_web_image_task(
                task_id,
                status=str(result.get("status") or ("succeeded" if success else "failed")),
                success=success,
                error=error,
                requested_count=result.get("requested_count", 1),
                succeeded_count=result.get("succeeded_count", 0),
                failed_count=result.get("failed_count", 0),
                result=result,
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
        except Exception as exc:
            error = redact_sensitive_text(str(exc))
            cancelled = "取消" in error
            self._set_web_image_task(
                task_id,
                status="cancelled" if cancelled else "failed",
                success=False,
                error=error,
                result={"success": False, "error": error},
                finished_ts=time.time(),
                finished_at=self._web_task_timestamp(),
            )
