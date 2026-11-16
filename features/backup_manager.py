"""Online backup/restore manager for the dashboard.

The legacy offline backup script intentionally remains separate.  This module
handles small, user-selected online snapshots and never accepts arbitrary
filesystem paths from the browser.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from typing import Any, Callable, Iterable


BACKUP_FORMAT = "selfie-image-dashboard-backup"
BACKUP_VERSION = 1
MAX_BACKUPS = 5
MAX_UPLOAD_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_FILES = 10000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")


class BackupManagerError(ValueError):
    """A backup operation was rejected or could not be completed safely."""


class BackupManager:
    """Manage bounded, selectable snapshots under a plugin data directory."""

    OPTIONS = (
        {"id": "config", "label": "配置", "description": "插件配置文件", "paths": ("selfie_image_config.json", "config.json")},
        {"id": "persona", "label": "形象资料", "description": "形象图、辅助形象和日程资料", "paths": ("image_persona.json", "image-persona")},
        {"id": "wardrobe", "label": "衣橱", "description": "衣橱元数据与服装图片（必选）", "paths": ("wardrobe",)},
        {"id": "schedule", "label": "今日日程", "description": "日程状态与提示词池", "paths": ("daily_schedule.json",)},
        {"id": "presets", "label": "预设与 COS", "description": "图片预设、NAI 预设和自定义池", "paths": ("image_presets.json", "nai_artist_presets.json", "cos_pool.json")},
        {"id": "studio", "label": "画布与资产集合", "description": "无限画布、创作画布和资产集合", "paths": ("studio_sessions.json", "creative_canvas", "asset_collections.json")},
        {"id": "tasks", "label": "任务状态", "description": "任务队列和渠道健康记录", "paths": ("generation_tasks.json", "channel_health_history.json")},
        {"id": "usage", "label": "用量统计", "description": "配额和用量统计", "paths": ("usage_stats.json",)},
        {"id": "records", "label": "生成记录", "description": "生成记录 JSON/SQLite，数据量可能较大", "paths": ("generation_records.json", "generation_records.sqlite3")},
        {"id": "cache", "label": "图片缓存", "description": "生成图片缓存，通常不需要备份", "paths": ("image_cache",)},
        {"id": "media_sources", "label": "引用媒体", "description": "生成记录引用的原始媒体", "paths": ("media_sources",)},
    )
    _OPTION_MAP = {item["id"]: item for item in OPTIONS}

    def __init__(self, data_root: str | os.PathLike[str]):
        self.data_root = Path(data_root).expanduser().absolute().resolve()
        if not self.data_root.is_dir():
            self.data_root.mkdir(parents=True, exist_ok=True)
        self.backup_root = self.data_root / "backups"
        self.backup_root.mkdir(mode=0o700, exist_ok=True)
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="selfie-backup")
        self._tasks: dict[str, dict[str, Any]] = {}
        self._reserved_slots = 0

    def options(self) -> list[dict[str, Any]]:
        return [dict(item, required=item["id"] == "wardrobe") for item in self.OPTIONS]

    def _new_task(self, kind: str) -> dict[str, Any]:
        task_id = f"backup-{uuid.uuid4().hex[:20]}"
        task = {
            "task_id": task_id,
            "kind": kind,
            "status": "queued",
            "current": 0,
            "total": 1,
            "percent": 0,
            "stage": "queued",
            "message": "等待处理",
            "created_at": _now(),
        }
        with self._lock:
            self._tasks[task_id] = task
        return dict(task)

    def _update_task(self, task_id: str, **changes: Any) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            task.update(changes)
            total = max(1, int(task.get("total") or 1))
            current = max(0, min(total, int(task.get("current") or 0)))
            task["current"] = current
            task["percent"] = max(0, min(100, int(current * 100 / total)))

    def task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(str(task_id or ""))
            if not task:
                raise BackupManagerError("备份任务不存在")
            return dict(task)

    def mark_task_runtime_refreshed(self, task_id: str) -> bool:
        """Claim the one-time post-restore runtime refresh for a task."""
        with self._lock:
            task = self._tasks.get(str(task_id or ""))
            if not task or task.get("runtime_refreshed"):
                return False
            task["runtime_refreshed"] = True
            return True

    def _submit(self, task: dict[str, Any], worker: Callable[[], Any]) -> dict[str, Any]:
        def run() -> None:
            self._update_task(task["task_id"], status="running", stage="running", message="正在处理")
            try:
                result = worker()
                self._update_task(task["task_id"], status="completed", stage="completed", message="处理完成", current=1, total=1, result=result)
            except Exception as exc:  # pragma: no cover - exact errors are surfaced through task state
                self._update_task(task["task_id"], status="failed", stage="failed", message=str(exc), error=str(exc), current=1, total=1)
            finally:
                with self._lock:
                    self._reserved_slots = max(0, self._reserved_slots - (1 if task["kind"] in {"backup", "upload"} else 0))
        self._executor.submit(run)
        return dict(task)

    def _selected_options(self, selected: Any) -> list[dict[str, Any]]:
        values = selected if isinstance(selected, (list, tuple, set)) else []
        names = {str(value or "").strip() for value in values}
        names.discard("")
        unknown = names - set(self._OPTION_MAP)
        if unknown:
            raise BackupManagerError("包含不支持的备份数据项")
        if "wardrobe" not in names:
            raise BackupManagerError("衣橱是必要数据，必须选择")
        return [self._OPTION_MAP[name] for name in self._OPTION_MAP if name in names]

    def _roots(self, options: Iterable[dict[str, Any]]) -> list[tuple[str, Path, str]]:
        roots: list[tuple[str, Path, str]] = []
        for option in options:
            for raw in option["paths"]:
                relative = Path(raw)
                candidate = self.data_root / relative
                if candidate.is_symlink():
                    raise BackupManagerError(f"不支持符号链接：{raw}")
                source = candidate.resolve()
                if source == self.backup_root or self.backup_root in source.parents:
                    continue
                if not source.exists():
                    continue
                if not source.is_file() and not source.is_dir():
                    raise BackupManagerError(f"不支持的备份文件类型：{raw}")
                roots.append((raw, source, "dir" if source.is_dir() else "file"))
        return roots

    @staticmethod
    def _safe_relative(name: str) -> str:
        value = str(name or "")
        path = Path(value)
        if not value or path.is_absolute() or "\\" in value or any(part in {"", ".", ".."} for part in value.split("/")):
            raise BackupManagerError("备份包含非法路径")
        return value

    def _iter_files(self, roots: Iterable[tuple[str, Path, str]]) -> list[tuple[str, Path]]:
        entries: list[tuple[str, Path]] = []
        for raw, source, kind in roots:
            if kind == "file":
                entries.append((raw.replace(os.sep, "/"), source))
                continue
            for current, dirnames, filenames in os.walk(source, followlinks=False):
                dirnames.sort()
                filenames.sort()
                for directory in dirnames:
                    if (Path(current) / directory).is_symlink():
                        raise BackupManagerError(f"数据目录包含符号链接：{directory}")
                for name in filenames:
                    path = Path(current) / name
                    if path.is_symlink() or not path.is_file():
                        raise BackupManagerError(f"衣橱或数据目录包含不支持的文件：{path.name}")
                    entries.append((path.relative_to(self.data_root).as_posix(), path))
        return sorted(entries, key=lambda item: item[0])

    def _build_archive(self, destination: Path, selected: list[str], *, task_id: str | None = None, archive_kind: str = "backup") -> dict[str, Any]:
        options = self._selected_options(selected) if archive_kind == "backup" else [self._OPTION_MAP["wardrobe"]]
        roots = self._roots(options)
        files = self._iter_files(roots)
        total = max(1, len(files))
        if task_id:
            self._update_task(task_id, total=total, current=0, stage="archive", message="正在收集文件")
        manifest: dict[str, Any] = {
            "format": BACKUP_FORMAT,
            "version": BACKUP_VERSION,
            "kind": archive_kind,
            "created_at": _now(),
            "selected": list(selected),
            "roots": [raw for raw, _source, _kind in roots],
            "files": [],
        }
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as output:
            for index, (relative, source) in enumerate(files, 1):
                info = source.stat()
                digest = hashlib.sha256()
                with source.open("rb") as incoming:
                    data = incoming.read()
                digest.update(data)
                output.writestr("data/" + relative, data)
                manifest["files"].append({"path": relative, "size": len(data), "sha256": digest.hexdigest()})
                if task_id:
                    self._update_task(task_id, current=index, total=total, stage="archive", message=f"已处理 {index}/{total} 个文件")
                if info.st_size != len(data):
                    raise BackupManagerError(f"文件在备份过程中发生变化：{relative}")
            output.writestr("manifest.json", json.dumps(manifest, ensure_ascii=True, indent=2).encode("utf-8"))
        manifest["file_count"] = len(files)
        manifest["size"] = destination.stat().st_size
        return manifest

    def _backup_count(self) -> int:
        return sum(1 for path in self.backup_root.glob("*.zip") if path.is_file())

    def start_backup(self, selected: Any) -> dict[str, Any]:
        names = [str(value or "").strip() for value in (selected if isinstance(selected, list) else [])]
        self._selected_options(names)
        with self._lock:
            if self._backup_count() + self._reserved_slots >= MAX_BACKUPS:
                raise BackupManagerError(f"最多保留 {MAX_BACKUPS} 个备份，请先删除旧备份")
            self._reserved_slots += 1
        task = self._new_task("backup")
        backup_id = f"bkp_{uuid.uuid4().hex[:16]}"
        destination = self.backup_root / f"{backup_id}.zip"

        def worker() -> dict[str, Any]:
            try:
                manifest = self._build_archive(destination, names, task_id=task["task_id"])
                manifest.update({"id": backup_id, "filename": destination.name})
                self._rewrite_manifest(destination, manifest)
                return self._summary(manifest)
            except Exception:
                destination.unlink(missing_ok=True)
                raise

        return self._submit(task, worker)

    def _rewrite_manifest(self, archive: Path, manifest: dict[str, Any]) -> None:
        fd, name = tempfile.mkstemp(prefix="manifest-", suffix=".tmp", dir=str(self.backup_root))
        os.close(fd)
        temporary = Path(name)
        try:
            with zipfile.ZipFile(archive, "r") as incoming, zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as output:
                for item in incoming.infolist():
                    if item.filename == "manifest.json":
                        continue
                    output.writestr(item, incoming.read(item.filename))
                output.writestr("manifest.json", json.dumps(manifest, ensure_ascii=True, indent=2).encode("utf-8"))
            os.replace(temporary, archive)
        finally:
            temporary.unlink(missing_ok=True)

    def list_backups(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for archive in sorted(self.backup_root.glob("*.zip"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                manifest = self._read_manifest(archive)
                rows.append(self._summary(manifest, archive))
            except Exception:
                continue
        return {"items": rows[:MAX_BACKUPS], "count": len(rows[:MAX_BACKUPS]), "max": MAX_BACKUPS, "can_create": len(rows) < MAX_BACKUPS}

    def _summary(self, manifest: dict[str, Any], archive: Path | None = None) -> dict[str, Any]:
        archive = archive or self.backup_root / f"{manifest.get('id')}.zip"
        return {"id": str(manifest.get("id") or archive.stem), "filename": archive.name, "created_at": manifest.get("created_at", ""), "size": int(archive.stat().st_size) if archive.is_file() else int(manifest.get("size") or 0), "selected": list(manifest.get("selected") or []), "file_count": len(manifest.get("files") or []), "kind": manifest.get("kind", "backup")}

    def _read_manifest(self, archive: Path) -> dict[str, Any]:
        if not archive.is_file() or archive.parent.resolve() != self.backup_root.resolve():
            raise BackupManagerError("备份文件不存在")
        with zipfile.ZipFile(archive, "r") as incoming:
            try:
                manifest = json.loads(incoming.read("manifest.json").decode("utf-8"))
            except (KeyError, ValueError, UnicodeError, zipfile.BadZipFile) as exc:
                raise BackupManagerError("备份清单无效") from exc
            self._validate_archive(incoming, manifest)
            self._verify_archive_checksums(incoming, manifest)
        return manifest

    @staticmethod
    def _verify_archive_checksums(archive: zipfile.ZipFile, manifest: dict[str, Any]) -> None:
        for item in manifest.get("files") or []:
            data = archive.read("data/" + item["path"])
            if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise BackupManagerError(f"备份校验失败：{item['path']}")

    def _validate_archive(self, archive: zipfile.ZipFile, manifest: Any) -> dict[str, Any]:
        if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT or manifest.get("version") != BACKUP_VERSION:
            raise BackupManagerError("不支持的备份格式")
        kind = manifest.get("kind")
        if kind not in {"backup", "wardrobe-export"}:
            raise BackupManagerError("备份类型无效")
        selected = manifest.get("selected")
        if not isinstance(selected, list) or any(str(item) not in self._OPTION_MAP for item in selected):
            raise BackupManagerError("备份数据项无效")
        if kind == "backup" and "wardrobe" not in selected:
            raise BackupManagerError("备份缺少衣橱必要数据")
        if kind == "wardrobe-export" and selected != ["wardrobe"]:
            raise BackupManagerError("衣橱归档数据项无效")
        roots = manifest.get("roots")
        allowed_roots = {path for option in self.OPTIONS for path in option["paths"]}
        if not isinstance(roots, list) or any(str(root) not in allowed_roots for root in roots):
            raise BackupManagerError("备份根路径无效")
        files = manifest.get("files")
        if not isinstance(files, list) or len(files) > MAX_ARCHIVE_FILES:
            raise BackupManagerError("备份文件数量超限")
        listed: dict[str, dict[str, Any]] = {}
        total = 0
        for item in files:
            if not isinstance(item, dict):
                raise BackupManagerError("备份清单条目无效")
            relative = self._safe_relative(item.get("path"))
            if relative in listed or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or "")):
                raise BackupManagerError("备份校验信息无效")
            size = item.get("size")
            if type(size) is not int or size < 0:
                raise BackupManagerError("备份文件大小无效")
            total += size
            listed[relative] = item
        if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise BackupManagerError("备份解压大小超限")
        raw_names = archive.namelist()
        if len(raw_names) != len(set(raw_names)):
            raise BackupManagerError("备份包含重复文件")
        names = set(raw_names)
        expected = {"manifest.json", *("data/" + name for name in listed)}
        if names != expected:
            raise BackupManagerError("备份包含未声明的文件")
        root_values = [str(root) for root in roots]
        if any(not any(name == root or name.startswith(root.rstrip("/") + "/") for root in root_values) for name in listed):
            raise BackupManagerError("备份文件不属于声明的数据项")
        for info in archive.infolist():
            if info.filename == "manifest.json":
                continue
            if info.filename.startswith("/") or ".." in Path(info.filename).parts or info.is_dir():
                raise BackupManagerError("备份包含非法路径")
            if info.external_attr >> 16 & 0o170000 == 0o120000:
                raise BackupManagerError("备份不允许符号链接")
            relative = info.filename[5:]
            if relative not in listed:
                raise BackupManagerError("备份文件清单不一致")
        return manifest

    def _archive_path(self, backup_id: str) -> Path:
        value = str(backup_id or "").strip()
        if not _ID_RE.fullmatch(value):
            raise BackupManagerError("备份编号无效")
        path = (self.backup_root / f"{value}.zip").resolve()
        if path.parent != self.backup_root.resolve():
            raise BackupManagerError("备份路径无效")
        return path

    def start_restore(self, backup_id: str) -> dict[str, Any]:
        archive = self._archive_path(backup_id)
        self._read_manifest(archive)
        task = self._new_task("restore")

        def worker() -> dict[str, Any]:
            return self._restore_archive(archive, task["task_id"])

        return self._submit(task, worker)

    def _restore_archive(self, archive: Path, task_id: str | None = None) -> dict[str, Any]:
        with zipfile.ZipFile(archive, "r") as incoming:
            manifest = json.loads(incoming.read("manifest.json").decode("utf-8"))
            self._validate_archive(incoming, manifest)
            with tempfile.TemporaryDirectory(prefix="selfie-restore-") as temporary:
                staging = Path(temporary) / "data"
                staging.mkdir(mode=0o700)
                files = manifest["files"]
                total = max(1, len(files))
                if task_id:
                    self._update_task(task_id, total=total, current=0, stage="extract", message="正在校验备份")
                for index, item in enumerate(files, 1):
                    target = staging / item["path"]
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    data = incoming.read("data/" + item["path"])
                    if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                        raise BackupManagerError(f"备份校验失败：{item['path']}")
                    target.write_bytes(data)
                    if task_id:
                        self._update_task(task_id, total=total, current=index, stage="extract", message=f"已校验 {index}/{total} 个文件")
                roots = [str(value) for value in manifest.get("roots") or []]
                rollback = Path(temporary) / "rollback"
                replaced: list[tuple[Path, Path | None]] = []
                try:
                    for root in roots:
                        safe = self._safe_relative(root)
                        source = staging / safe
                        target = self.data_root / safe
                        if not source.exists():
                            continue
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        old = rollback / safe
                        old.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        old_path: Path | None = None
                        if target.exists() or target.is_symlink():
                            if target.is_symlink():
                                raise BackupManagerError(f"拒绝覆盖符号链接：{safe}")
                            os.replace(target, old)
                            old_path = old
                        os.replace(source, target)
                        replaced.append((target, old_path))
                except Exception:
                    for target, old in reversed(replaced):
                        if target.exists() or target.is_symlink():
                            if target.is_dir() and not target.is_symlink():
                                shutil.rmtree(target)
                            else:
                                target.unlink(missing_ok=True)
                        if old and old.exists():
                            os.replace(old, target)
                    raise
        return self._summary(manifest, archive)

    def delete_backup(self, backup_id: str) -> dict[str, Any]:
        archive = self._archive_path(backup_id)
        with self._lock:
            if not archive.is_file():
                raise BackupManagerError("备份不存在")
            archive.unlink()
        return self.list_backups()

    def download_backup(self, backup_id: str) -> dict[str, Any]:
        archive = self._archive_path(backup_id)
        self._read_manifest(archive)
        data = archive.read_bytes()
        return {"id": backup_id, "filename": archive.name, "content_base64": base64.b64encode(data).decode("ascii"), "size": len(data)}

    def upload_backup(self, filename: str, content: bytes) -> dict[str, Any]:
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise BackupManagerError(f"上传备份不得超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
        with tempfile.NamedTemporaryFile(prefix="selfie-upload-", suffix=".zip", delete=False) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        try:
            manifest = self._read_manifest_external(temporary_path)
            if manifest.get("kind") != "backup":
                raise BackupManagerError("上传文件不是完整备份")
            with self._lock:
                if self._backup_count() + self._reserved_slots >= MAX_BACKUPS:
                    raise BackupManagerError(f"最多保留 {MAX_BACKUPS} 个备份，请先删除旧备份")
                self._reserved_slots += 1
            backup_id = f"bkp_{uuid.uuid4().hex[:16]}"
            archive = self.backup_root / f"{backup_id}.zip"
            os.replace(temporary_path, archive)
            task = self._new_task("upload")

            def worker() -> dict[str, Any]:
                result = self._restore_archive(archive, task["task_id"])
                result["uploaded_filename"] = str(filename or "backup.zip")[:120]
                return result

            return self._submit(task, worker)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _read_manifest_external(self, path: Path) -> dict[str, Any]:
        with zipfile.ZipFile(path, "r") as incoming:
            manifest = json.loads(incoming.read("manifest.json").decode("utf-8"))
            self._validate_archive(incoming, manifest)
            return manifest

    def export_wardrobe(self) -> dict[str, Any]:
        with tempfile.NamedTemporaryFile(prefix="wardrobe-export-", suffix=".zip", delete=False) as temporary:
            path = Path(temporary.name)
        try:
            manifest = self._build_archive(path, ["wardrobe"], archive_kind="wardrobe-export")
            data = path.read_bytes()
            return {"filename": "selfie_image_wardrobe.zip", "content_base64": base64.b64encode(data).decode("ascii"), "size": len(data), "file_count": len(manifest.get("files") or [])}
        finally:
            path.unlink(missing_ok=True)

    def import_wardrobe(self, content: bytes) -> dict[str, Any]:
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise BackupManagerError("衣橱归档过大或为空")
        with tempfile.NamedTemporaryFile(prefix="wardrobe-import-", suffix=".zip", delete=False) as temporary:
            temporary.write(content)
            path = Path(temporary.name)
        try:
            manifest = self._read_manifest_external(path)
            if manifest.get("kind") != "wardrobe-export" or manifest.get("selected") != ["wardrobe"]:
                raise BackupManagerError("不是衣橱归档")
            result = self._restore_archive(path)
            return {"imported": True, **result}
        finally:
            path.unlink(missing_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
