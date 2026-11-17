"""Web backup orchestration over the plugin's live stores and task state."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

from .archive_transfer import CHUNK_SIZE, MAX_JSON_ARCHIVE_BYTES


class BackupWebService:
    """Keep request decoding and restore coordination out of the plugin entrypoint."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    @staticmethod
    def decode_archive(payload: Any, label: str, *, max_bytes: int = MAX_JSON_ARCHIVE_BYTES) -> bytes:
        body = payload if isinstance(payload, Mapping) else {}
        raw = body.get("content_base64") or body.get("data") or body.get("content") or ""
        if not isinstance(raw, str):
            raise ValueError(f"{label}文件内容不是有效的 base64")
        encoded_limit = 4 * ((max_bytes + 2) // 3)
        if len(raw) > encoded_limit + 1024:
            raise ValueError(f"{label}超过 16MB，请使用分块上传")
        raw = raw.strip()
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        if not raw:
            raise ValueError(f"缺少{label}文件内容")
        if len(raw) > encoded_limit:
            raise ValueError(f"{label}超过 16MB，请使用分块上传")
        try:
            content = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError, base64.binascii.Error) as exc:
            raise ValueError(f"{label}文件内容不是有效的 base64") from exc
        if len(content) > max_bytes:
            raise ValueError(f"{label}超过 16MB，请使用分块上传")
        return content

    def options(self) -> dict[str, Any]:
        return {"items": self.plugin.backup_manager.options(), "max": 5, "restart_required": True}

    def list(self) -> dict[str, Any]:
        return self.plugin.backup_manager.list_backups()

    def create(self, payload: Any) -> dict[str, Any]:
        body = payload if isinstance(payload, Mapping) else {}
        selected = body.get("selected", body.get("items", []))
        if not isinstance(selected, list):
            raise ValueError("selected 必须是数组")
        return self.plugin.backup_manager.start_backup(selected)

    def task(self, task_id: str) -> dict[str, Any]:
        return self.plugin.backup_manager.task(task_id)

    def assert_restore_idle(self) -> None:
        plugin = self.plugin
        if getattr(plugin, "_terminating", False):
            raise ValueError("插件正在卸载，无法恢复备份")
        tasks = getattr(plugin, "_web_tasks", {})
        workers = getattr(plugin, "_runtime_generation_workers", {}).values()
        handles = getattr(plugin, "_runtime_generation_tasks", {}).values()
        commits = list(getattr(plugin, "_record_commit_tasks", set()))
        commits.extend(getattr(plugin, "_usage_commit_tasks", set()))
        commits.extend(future for bucket in getattr(plugin, "_pending_record_commits", {}).values() for future in bucket)
        commits.extend(future for bucket in getattr(plugin, "_pending_image_record_commits", {}).values() for future in bucket)
        if any(task.get("status") in {"queued", "running"} for task in tasks.values()) or any(
            not handle.done() for handle in [*workers, *handles, *commits]
        ):
            raise ValueError("请等待生成任务和数据写入完成后恢复备份")
        scheduler = plugin._image_scheduler_snapshot()
        if scheduler.get("active", 0) or scheduler.get("pending", 0):
            raise ValueError("请等待生成任务完成后恢复备份")

    def download(self, backup_id: str) -> dict[str, Any]:
        return self.plugin.backup_manager.download_backup(backup_id)

    def download_file(self, backup_id: str) -> dict[str, Any]:
        return self.plugin.backup_manager.download_file(backup_id)

    def download_chunk(self, backup_id: str, offset: int, length: int, revision: str) -> dict[str, Any]:
        return self.plugin.backup_manager.download_chunk(backup_id, offset, length, revision)

    def restore(self, backup_id: str, selected: Any = None) -> dict[str, Any]:
        plugin = self.plugin
        with plugin._web_task_lock:
            plugin._assert_backup_restore_idle()
            return plugin.backup_manager.start_restore(backup_id, selected)

    def delete(self, payload: Any) -> dict[str, Any]:
        body = payload if isinstance(payload, Mapping) else {}
        backup_id = str(body.get("id") or body.get("backup_id") or "").strip()
        if not backup_id:
            raise ValueError("缺少备份编号")
        return self.plugin.backup_manager.delete_backup(backup_id)

    def upload(self, payload: Any) -> dict[str, Any]:
        plugin = self.plugin
        body = payload if isinstance(payload, Mapping) else {}
        content = plugin._decode_web_archive(body, "备份")
        with plugin._web_task_lock:
            plugin._assert_backup_restore_idle()
            return plugin.backup_manager.upload_backup(str(body.get("filename") or "backup.zip"), content, body.get("selected"))

    def export_wardrobe(self) -> dict[str, Any]:
        return self.plugin.backup_manager.export_wardrobe()

    def import_wardrobe(self, payload: Any) -> dict[str, Any]:
        plugin = self.plugin
        content = plugin._decode_web_archive(payload, "衣橱归档")
        result = plugin.backup_manager.import_wardrobe(content)
        # Stores can be replaced by restore/reload; always read the live plugin.
        plugin.persona.wardrobe.load()
        return plugin.get_wardrobe_payload() | {"imported": True, "archive": result}

    def start_upload(self, payload: Any) -> dict[str, Any]:
        body = payload if isinstance(payload, Mapping) else {}
        return self.plugin.backup_manager.start_archive_upload(
            body.get("kind"), body.get("size"), str(body.get("filename") or "archive.zip"),
        )

    def append_upload(self, transfer_id: str, payload: Any, *, chunk_size: int = CHUNK_SIZE) -> dict[str, Any]:
        body = payload if isinstance(payload, Mapping) else {}
        raw = body.get("content_base64")
        if not isinstance(raw, str) or len(raw) > 4 * ((chunk_size + 2) // 3):
            raise ValueError("归档分块不得超过 1MB")
        try:
            data = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("归档分块不是有效的 base64") from exc
        return self.plugin.backup_manager.transfers.append_upload(
            transfer_id, body.get("offset"), data, body.get("crc32"),
        )

    def commit_upload(self, transfer_id: str, selected: Any = None) -> dict[str, Any]:
        plugin = self.plugin
        with plugin.backup_manager.transfers.consume_upload(transfer_id) as (path, info):
            if info["kind"] == "backup":
                with plugin._web_task_lock:
                    plugin._assert_backup_restore_idle()
                    return plugin.backup_manager.upload_backup_file(info["filename"], path, selected)
            result = plugin.backup_manager.import_wardrobe_file(path)
            plugin.persona.wardrobe.load()
            return plugin.get_wardrobe_payload() | {"imported": True, "archive": result}

    def discard_transfer(self, transfer_id: str) -> dict[str, Any]:
        return self.plugin.backup_manager.transfers.discard(transfer_id)

    def start_wardrobe_export(self) -> dict[str, Any]:
        return self.plugin.backup_manager.start_wardrobe_export()

    def download_export(self, transfer_id: str, offset: int, length: int,
                        revision: str, as_file: bool) -> dict[str, Any]:
        return self.plugin.backup_manager.transfers.download_export(
            transfer_id, offset=offset, length=length, revision=revision, as_file=as_file,
        )
