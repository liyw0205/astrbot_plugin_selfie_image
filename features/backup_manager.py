"""Online backup/restore manager for the dashboard.

The legacy offline backup script intentionally remains separate.  This module
handles user-selected online snapshots and startup restores without accepting
arbitrary filesystem paths from the browser.
"""

from __future__ import annotations

import base64
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from typing import Any, Callable, Iterable

from ..core.constants import PLUGIN_CONFIG_FILENAME
from .archive_transfer import ArchiveTransferStore, MAX_JSON_ARCHIVE_BYTES


BACKUP_FORMAT = "selfie-image-dashboard-backup"
BACKUP_VERSION = 1
MAX_BACKUPS = 5
MAX_UPLOAD_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_FILES = 10000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_ACTIVE_TASKS = 2
MAX_TASK_HISTORY = 100
TASK_HISTORY_TTL = 3600
CHUNK_SIZE = 1024 * 1024
MAX_JSON_DOWNLOAD_BYTES = 16 * 1024 * 1024
_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")
_DIRECTORY_ROOTS = frozenset({"image-persona", "wardrobe", "creative_canvas", "image_cache", "media_sources"})


class BackupManagerError(ValueError):
    """A backup operation was rejected or could not be completed safely."""


class BackupManager:
    """Manage bounded, selectable snapshots under a plugin data directory."""

    # Keep this list aligned with the stores created by ``main.py``.  The
    # flags describe storage semantics, rather than special-casing an option
    # ID in validation or the dashboard:
    #   required       must be present in a complete plugin backup;
    #   default_selected is selected for a normal user-created backup.
    OPTIONS = (
        {
            "id": "config",
            "label": "配置",
            "description": "插件运行所需的完整业务配置",
            "reason": "核心配置，恢复插件行为必须保留",
            "paths": (PLUGIN_CONFIG_FILENAME,),
            "required": True,
            "default_selected": True,
        },
        {
            "id": "persona",
            "label": "形象资料",
            "description": "主形象、辅助形象和形象元数据",
            "reason": "用户资料，缺失时仍可启动但会丢失形象",
            "paths": ("image_persona.json", "image-persona"),
            "required": False,
            "default_selected": True,
        },
        {
            "id": "wardrobe",
            "label": "衣橱",
            "description": "衣橱元数据与服装图片",
            "reason": "可选的服装参考资料，可单独导入导出",
            "paths": ("wardrobe",),
            "required": False,
            "default_selected": True,
        },
        {
            "id": "schedule",
            "label": "今日日程",
            "description": "日程状态与当天提示词池",
            "reason": "运行状态，可在缺失时重新生成",
            "paths": ("daily_schedule.json",),
            "required": False,
            "default_selected": True,
        },
        {
            "id": "presets",
            "label": "预设与 COS",
            "description": "图片预设、NAI 预设、会话画师和自定义 COS 池",
            "reason": "用户维护的创作资料",
            "paths": (
                "image_presets.json",
                "nai_artist_presets.json",
                "nai_artists_by_session.json",
                "cos_pools.json",
            ),
            "required": False,
            "default_selected": True,
        },
        {
            "id": "studio",
            "label": "画布与资产集合",
            "description": "关系画布、创作画布和资产集合",
            "reason": "用户维护的创作资料",
            "paths": ("studio_sessions.json", "creative_canvas", "asset_collections.json"),
            "required": False,
            "default_selected": True,
        },
        {
            "id": "tasks",
            "label": "任务状态",
            "description": "任务快照、渠道健康和请求媒体日志",
            "reason": "可恢复的运行状态，体积小但不是启动必需",
            "paths": (
                "generation_tasks.json",
                "channel_health_history.json",
                "pending_request_media.json",
            ),
            "required": False,
            "default_selected": False,
        },
        {
            "id": "usage",
            "label": "用量统计",
            "description": "每日额度和用量统计",
            "reason": "可重建的统计状态",
            "paths": ("usage_stats.json",),
            "required": False,
            "default_selected": False,
        },
        {
            "id": "records",
            "label": "生成记录",
            "description": "生成记录 JSON/SQLite，数据量可能较大",
            "reason": "长期历史，可按需备份",
            "paths": (
                "generation_records.json",
                "generation_records.json.bak",
                "generation_records.sqlite3",
            ),
            "required": False,
            "default_selected": False,
        },
        {
            "id": "cache",
            "label": "图片缓存",
            "description": "生成图片缓存，通常可重新生成",
            "reason": "派生数据，默认不备份以节省空间",
            "paths": ("image_cache",),
            "required": False,
            "default_selected": False,
        },
        {
            "id": "media_sources",
            "label": "引用媒体",
            "description": "生成记录引用的原始媒体",
            "reason": "记录关联的可选媒体，体积可能较大",
            "paths": ("media_sources",),
            "required": False,
            "default_selected": False,
        },
    )
    _OPTION_MAP = {item["id"]: item for item in OPTIONS}

    def __init__(self, data_root: str | os.PathLike[str], *, defer_restore: bool = False):
        self.data_root = Path(data_root).expanduser().absolute().resolve()
        if not self.data_root.is_dir():
            self.data_root.mkdir(parents=True, exist_ok=True)
        self.backup_root = self.data_root / "backups"
        if self.backup_root.is_symlink():
            raise BackupManagerError("备份目录不能是符号链接")
        self.backup_root.mkdir(mode=0o700, exist_ok=True)
        self._lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="selfie-backup")
        self._tasks: dict[str, dict[str, Any]] = {}
        self._futures: dict[str, Future] = {}
        self._reserved_slots = 0
        self._closed = False
        self._defer_restore = defer_restore
        self._pending_restore = self.backup_root / "pending-restore"
        self._restore_pending = self._pending_restore.exists()
        self._transaction = self.backup_root / ".restore-transaction"
        self._manifest_cache: dict[Path, tuple[tuple[int, ...], dict[str, Any]]] = {}
        self.transfers = ArchiveTransferStore(self.backup_root / ".transfers")

    @property
    def restore_pending(self) -> bool:
        with self._lock:
            return self._restore_pending

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise BackupManagerError("备份管理器已关闭")
        if self._restore_pending:
            raise BackupManagerError("备份恢复已准备，请重启插件后继续操作")

    def close(self, *, wait: bool = True) -> None:
        """Reject submissions, cancel queued work and drain running file writes."""
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=True)
        if wait:
            with self._operation_lock:
                pass
        self.transfers.close()

    def _prune_tasks_locked(self) -> None:
        now = time.time()
        finished = sorted(
            ((key, task) for key, task in self._tasks.items() if task.get("finished_ts")),
            key=lambda item: item[1]["finished_ts"],
            reverse=True,
        )
        for index, (key, task) in enumerate(finished):
            if index >= MAX_TASK_HISTORY or now - task["finished_ts"] > TASK_HISTORY_TTL:
                self._tasks.pop(key, None)

    def options(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.OPTIONS]

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
            self._prune_tasks_locked()
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

    def _submit(self, task: dict[str, Any], worker: Callable[[], Any]) -> dict[str, Any]:
        def run() -> None:
            self._update_task(task["task_id"], status="running", stage="running", message="正在处理")
            try:
                with self._operation_lock:
                    result = worker()
                message = "恢复已准备，请重启插件后生效" if result.get("restart_required") else "处理完成"
                self._update_task(task["task_id"], status="completed", stage="completed", message=message, current=1, total=1, result=result, finished_ts=time.time())
            except Exception as exc:  # pragma: no cover - exact errors are surfaced through task state
                self._update_task(task["task_id"], status="failed", stage="failed", message=str(exc), error=str(exc), current=1, total=1, finished_ts=time.time())

        def finished(future: Future) -> None:
            with self._lock:
                self._futures.pop(task["task_id"], None)
                if future.cancelled():
                    self._update_task(task["task_id"], status="cancelled", stage="cancelled", message="插件卸载，备份任务已取消", finished_ts=time.time())
                self._reserved_slots = max(0, self._reserved_slots - (1 if task["kind"] in {"backup", "upload"} else 0))
                if task["kind"] in {"restore", "upload"}:
                    self._restore_pending = self._pending_restore.exists()
                self._prune_tasks_locked()

        with self._lock:
            try:
                if self._closed:
                    raise BackupManagerError("备份管理器已关闭")
                if self._restore_pending and task["kind"] not in {"restore", "upload"}:
                    raise BackupManagerError("备份恢复已准备，请重启插件后继续操作")
                if len(self._futures) >= MAX_ACTIVE_TASKS:
                    raise BackupManagerError("备份任务繁忙，请稍后重试")
                future = self._executor.submit(run)
            except Exception:
                self._tasks.pop(task["task_id"], None)
                self._reserved_slots = max(0, self._reserved_slots - (1 if task["kind"] in {"backup", "upload"} else 0))
                if task["kind"] in {"restore", "upload"}:
                    self._restore_pending = self._pending_restore.exists()
                raise
            self._futures[task["task_id"]] = future
            future.add_done_callback(finished)
        return dict(task)

    def _selected_options(self, selected: Any) -> list[dict[str, Any]]:
        values = selected if isinstance(selected, (list, tuple, set)) else []
        names = {str(value or "").strip() for value in values}
        names.discard("")
        unknown = names - set(self._OPTION_MAP)
        if unknown:
            raise BackupManagerError("包含不支持的备份数据项")
        required = {
            item["id"] for item in self.OPTIONS if item.get("required") is True
        }
        missing = required - names
        if missing:
            labels = [
                str(self._OPTION_MAP[item].get("label") or item)
                for item in self._OPTION_MAP
                if item in missing
            ]
            raise BackupManagerError(f"必须选择核心数据：{'、'.join(labels)}")
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

        def fail(error: OSError) -> None:
            raise error

        for raw, source, kind in roots:
            if kind == "file":
                entries.append((raw.replace(os.sep, "/"), source))
                continue
            for current, dirnames, filenames in os.walk(source, followlinks=False, onerror=fail):
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
                    if len(entries) > MAX_ARCHIVE_FILES:
                        raise BackupManagerError("备份文件数量超限")
        if len(entries) > MAX_ARCHIVE_FILES:
            raise BackupManagerError("备份文件数量超限")
        return sorted(entries, key=lambda item: item[0])

    def _build_archive(self, destination: Path, selected: list[str], *, task_id: str | None = None, archive_kind: str = "backup") -> dict[str, Any]:
        options = self._selected_options(selected) if archive_kind == "backup" else [self._OPTION_MAP["wardrobe"]]
        roots = self._roots(options)
        files = self._iter_files(roots)
        if sum(source.stat().st_size for _name, source in files) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise BackupManagerError("备份解压大小超限")
        total = max(1, len(files))
        if task_id:
            self._update_task(task_id, total=total, current=0, stage="archive", message="正在收集文件")
        manifest: dict[str, Any] = {
            "format": BACKUP_FORMAT,
            "version": BACKUP_VERSION,
            "kind": archive_kind,
            "id": destination.stem,
            "filename": destination.name,
            "created_at": _now(),
            "selected": list(selected),
            "roots": [raw for raw, _source, _kind in roots],
            "files": [],
        }
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        uncompressed_size = 0
        with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=self.backup_root) as temporary, zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as output:
            for index, (relative, source) in enumerate(files, 1):
                if relative == "generation_records.sqlite3":
                    snapshot = Path(temporary) / source.name
                    # A raw main-file copy omits committed transactions in WAL.
                    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as incoming:
                        with closing(sqlite3.connect(snapshot)) as database:
                            incoming.backup(database)
                            database.execute("PRAGMA journal_mode=DELETE")
                            if database.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                                raise BackupManagerError("记录数据库快照校验失败")
                    source = snapshot
                info = source.stat()
                digest = hashlib.sha256()
                size = 0
                with source.open("rb") as incoming, output.open("data/" + relative, "w", force_zip64=True) as entry:
                    for chunk in iter(lambda: incoming.read(CHUNK_SIZE), b""):
                        size += len(chunk)
                        uncompressed_size += len(chunk)
                        if uncompressed_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                            raise BackupManagerError("备份解压大小超限")
                        digest.update(chunk)
                        entry.write(chunk)
                manifest["files"].append({"path": relative, "size": size, "sha256": digest.hexdigest()})
                if task_id:
                    self._update_task(task_id, current=index, total=total, stage="archive", message=f"已处理 {index}/{total} 个文件")
                if info.st_size != size or self._stat_key(info) != self._stat_key(source.stat()):
                    raise BackupManagerError(f"文件在备份过程中发生变化：{relative}")
            manifest_data = json.dumps(manifest, ensure_ascii=True, indent=2).encode("utf-8")
            if len(manifest_data) > MAX_MANIFEST_BYTES:
                raise BackupManagerError("备份清单过大")
            output.writestr("manifest.json", manifest_data)
        manifest["file_count"] = len(files)
        manifest["size"] = destination.stat().st_size
        if manifest["size"] > MAX_UPLOAD_BYTES:
            raise BackupManagerError("备份压缩大小超过上传上限，请减少所选数据")
        return manifest

    def _backup_count(self) -> int:
        return sum(1 for path in self.backup_root.glob("*.zip") if path.is_file())

    def start_backup(self, selected: Any) -> dict[str, Any]:
        names = [str(value or "").strip() for value in (selected if isinstance(selected, list) else [])]
        self._selected_options(names)
        with self._lock:
            self._ensure_open_locked()
            if self._backup_count() + self._reserved_slots >= MAX_BACKUPS:
                raise BackupManagerError(f"最多保留 {MAX_BACKUPS} 个备份，请先删除旧备份")
            self._reserved_slots += 1
        task = self._new_task("backup")
        backup_id = f"bkp_{uuid.uuid4().hex[:16]}"
        destination = self.backup_root / f"{backup_id}.zip"

        def worker() -> dict[str, Any]:
            with tempfile.TemporaryDirectory(prefix=".backup-", dir=self.backup_root) as temporary:
                staged = Path(temporary) / destination.name
                manifest = self._build_archive(staged, names, task_id=task["task_id"])
                with staged.open("rb") as incoming:
                    os.fsync(incoming.fileno())
                os.replace(staged, destination)
                self._sync_directory(self.backup_root)
            return self._summary(manifest, destination)

        return self._submit(task, worker)

    def list_backups(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for archive in sorted(self.backup_root.glob("*.zip"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                manifest = self._read_manifest(archive)
                rows.append(self._summary(manifest, archive))
            except Exception:
                continue
        count = self._backup_count()
        return {"items": rows[:MAX_BACKUPS], "count": len(rows[:MAX_BACKUPS]), "max": MAX_BACKUPS, "can_create": count < MAX_BACKUPS and not self.restore_pending, "restore_pending": self.restore_pending, "invalid_count": max(0, count - len(rows))}

    def _summary(self, manifest: dict[str, Any], archive: Path | None = None) -> dict[str, Any]:
        archive = archive or self.backup_root / f"{manifest.get('id')}.zip"
        return {"id": archive.stem, "filename": archive.name, "created_at": manifest.get("created_at", ""), "size": int(archive.stat().st_size) if archive.is_file() else int(manifest.get("size") or 0), "selected": list(manifest.get("selected") or []), "file_count": len(manifest.get("files") or []), "kind": manifest.get("kind", "backup")}

    @staticmethod
    def _stat_key(info: os.stat_result) -> tuple[int, ...]:
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _read_manifest(self, archive: Path, *, verify: bool = True) -> dict[str, Any]:
        if not archive.is_file() or archive.parent.resolve() != self.backup_root.resolve():
            raise BackupManagerError("备份文件不存在")
        signature = self._stat_key(archive.stat())
        with self._lock:
            cached = self._manifest_cache.get(archive)
            if cached and cached[0] == signature:
                return cached[1]
        with zipfile.ZipFile(archive, "r") as incoming:
            manifest = self._read_archive_manifest(incoming)
            if verify:
                self._verify_archive_checksums(incoming, manifest)
        if verify and signature == self._stat_key(archive.stat()):
            with self._lock:
                if len(self._manifest_cache) >= MAX_BACKUPS:
                    self._manifest_cache.clear()
                self._manifest_cache[archive] = (signature, manifest)
        return manifest

    def _read_archive_manifest(self, archive: zipfile.ZipFile) -> dict[str, Any]:
        try:
            if archive.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
                raise BackupManagerError("备份清单过大")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except (KeyError, ValueError, UnicodeError, zipfile.BadZipFile) as exc:
            raise BackupManagerError("备份清单无效") from exc
        return self._validate_archive(archive, manifest)

    @staticmethod
    def _verify_archive_checksums(archive: zipfile.ZipFile, manifest: dict[str, Any]) -> None:
        for item in manifest.get("files") or []:
            digest = hashlib.sha256()
            size = 0
            with archive.open("data/" + item["path"]) as incoming:
                for chunk in iter(lambda: incoming.read(CHUNK_SIZE), b""):
                    size += len(chunk)
                    digest.update(chunk)
            if size != item["size"] or digest.hexdigest() != item["sha256"]:
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
        if kind == "backup":
            required = {
                item["id"] for item in self.OPTIONS if item.get("required") is True
            }
            missing = required - {str(item) for item in selected}
            if missing:
                raise BackupManagerError("备份缺少核心必选数据")
        if kind == "wardrobe-export" and selected != ["wardrobe"]:
            raise BackupManagerError("衣橱归档数据项无效")
        roots = manifest.get("roots")
        allowed_roots = {path for name in selected for path in self._OPTION_MAP[str(name)]["paths"]}
        if not isinstance(roots, list) or any(not isinstance(root, str) or root not in allowed_roots for root in roots) or len(roots) != len(set(roots)):
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
        if any(not any(
            (root not in _DIRECTORY_ROOTS and name == root)
            or (root in _DIRECTORY_ROOTS and name.startswith(root + "/"))
            for root in root_values
        ) for name in listed):
            raise BackupManagerError("备份文件不属于声明的数据项")
        for info in archive.infolist():
            if info.filename == "manifest.json":
                continue
            if info.filename.startswith("/") or ".." in Path(info.filename).parts or info.is_dir():
                raise BackupManagerError("备份包含非法路径")
            if info.external_attr >> 16 & 0o170000 == 0o120000:
                raise BackupManagerError("备份不允许符号链接")
            relative = info.filename[5:]
            if relative not in listed or info.file_size != listed[relative]["size"]:
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

    def _restore_selection(self, manifest: dict[str, Any], selected: Any) -> list[str]:
        available = manifest["selected"]
        if selected is None:
            return list(available)
        if not isinstance(selected, list) or not selected or any(
            not isinstance(item, str) or item not in self._OPTION_MAP for item in selected
        ):
            raise BackupManagerError("请选择有效的恢复数据项")
        names = [name for name in available if name in selected]
        if not names:
            raise BackupManagerError("备份中不包含所选恢复数据项")
        return names

    def start_restore(self, backup_id: str, selected: Any = None) -> dict[str, Any]:
        archive = self._archive_path(backup_id)
        manifest = self._read_manifest(archive, verify=False)
        selected = self._restore_selection(manifest, selected)
        with self._lock:
            self._ensure_open_locked()
            if self._futures:
                raise BackupManagerError("请等待当前备份任务完成后恢复")
            self._restore_pending = True
            task = self._new_task("restore")

        def worker() -> dict[str, Any]:
            return self._prepare_restore(archive, task["task_id"], selected) if self._defer_restore else self._restore_archive(archive, task["task_id"], selected)

        return self._submit(task, worker)

    def _prepare_restore(self, archive: Path, task_id: str, selected: Any = None) -> dict[str, Any]:
        """Publish a verified restore request; live stores remain untouched."""
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=self.backup_root) as temporary:
            staged = Path(temporary) / "archive.zip"
            with archive.open("rb") as incoming, staged.open("wb") as output:
                shutil.copyfileobj(incoming, output, CHUNK_SIZE)
                output.flush()
                os.fsync(output.fileno())
            with zipfile.ZipFile(staged) as incoming:
                manifest = self._read_archive_manifest(incoming)
                self._verify_archive_checksums(incoming, manifest)
            selected = self._restore_selection(manifest, selected)
            with (Path(temporary) / "selection.json").open("w", encoding="utf-8") as output:
                json.dump(selected, output)
                output.flush()
                os.fsync(output.fileno())
            self._sync_directory(Path(temporary))
            self._update_task(task_id, stage="prepare", message="正在准备重启恢复")
            os.replace(temporary, self._pending_restore)
            self._sync_directory(self.backup_root)
        return {**self._summary(manifest, archive), "restore_selected": selected, "restart_required": True}

    def apply_pending_restore(self) -> bool:
        """Run only during startup, before constructing any durable stores."""
        with self._operation_lock:
            self._recover_restore_transaction()
            if self._pending_restore.is_symlink():
                raise BackupManagerError("待恢复目录不能是符号链接")
            if not self._pending_restore.exists():
                return False
            archive = self._pending_restore / "archive.zip"
            if archive.is_symlink() or not archive.is_file():
                raise BackupManagerError("待恢复归档缺失或不是普通文件，已保留现场")
            selection_path = self._pending_restore / "selection.json"
            if selection_path.is_symlink():
                raise BackupManagerError("恢复选项不能是符号链接")
            selected = json.loads(selection_path.read_text(encoding="utf-8")) if selection_path.exists() else None
            self._restore_archive(archive, selected=selected)
            shutil.rmtree(self._pending_restore)
            self._sync_directory(self.backup_root)
            with self._lock:
                self._restore_pending = False
            return True

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def _write_restore_journal(self, journal: dict[str, Any]) -> None:
        temporary = self._transaction / "journal.tmp"
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(journal, output, ensure_ascii=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self._transaction / "journal.json")
        self._sync_directory(self._transaction)

    def _recover_restore_transaction(self) -> None:
        """An incomplete publication rolls back before any plugin store opens."""
        if not self._transaction.exists():
            return
        if self._transaction.is_symlink():
            raise BackupManagerError("恢复事务目录不能是符号链接")
        journal_path = self._transaction / "journal.json"
        if journal_path.exists():
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            roots = journal.get("roots")
            allowed = {path for option in self.OPTIONS for path in option["paths"]}
            allowed.update({"generation_records.sqlite3-wal", "generation_records.sqlite3-shm"})
            if journal.get("state") not in {"prepared", "committed"} or not isinstance(roots, list) or any(
                not isinstance(item, dict) or item.get("path") not in allowed or type(item.get("existed")) is not bool
                for item in roots
            ):
                raise BackupManagerError("恢复事务日志无效，已保留现场")
            if journal["state"] == "prepared":
                for item in reversed(roots):
                    target = self.data_root / item["path"]
                    old = self._transaction / "rollback" / item["path"]
                    if old.exists():
                        self._remove_path(target)
                        os.replace(old, target)
                        self._sync_directory(self.data_root)
                    elif not item["existed"] and not (self._transaction / "data" / item["path"]).exists():
                        self._remove_path(target)
                        self._sync_directory(self.data_root)
        shutil.rmtree(self._transaction)
        self._sync_directory(self.backup_root)

    def _restore_archive(self, archive: Path, task_id: str | None = None, selected: Any = None) -> dict[str, Any]:
        with self._operation_lock, zipfile.ZipFile(archive, "r") as incoming:
            manifest = self._read_archive_manifest(incoming)
            selected = self._restore_selection(manifest, selected)
            selected_roots = {root for name in selected for root in self._OPTION_MAP[name]["paths"]}
            self._recover_restore_transaction()
            self._transaction.mkdir(mode=0o700)
            self._sync_directory(self.backup_root)
            try:
                staging = self._transaction / "data"
                staging.mkdir(mode=0o700)
                for root in manifest["roots"]:
                    if root in _DIRECTORY_ROOTS:
                        (staging / root).mkdir(mode=0o700)
                files = manifest["files"]
                total = max(1, len(files))
                if task_id:
                    self._update_task(task_id, total=total, current=0, stage="extract", message="正在校验备份")
                for index, item in enumerate(files, 1):
                    target = staging / item["path"]
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    size = 0
                    with incoming.open("data/" + item["path"]) as source, target.open("wb") as output:
                        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                            size += len(chunk)
                            digest.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if size != item["size"] or digest.hexdigest() != item["sha256"]:
                        raise BackupManagerError(f"备份校验失败：{item['path']}")
                    if task_id:
                        self._update_task(task_id, total=total, current=index, stage="extract", message=f"已校验 {index}/{total} 个文件")
                roots = [root for root in manifest["roots"] if root in selected_roots and (staging / root).exists()]
                if "generation_records.sqlite3" in roots:
                    roots.extend(["generation_records.sqlite3-wal", "generation_records.sqlite3-shm"])
                journal = {"state": "prepared", "roots": []}
                rollback = self._transaction / "rollback"
                rollback.mkdir(mode=0o700)
                for root in roots:
                    target = self.data_root / root
                    if target.is_symlink():
                        raise BackupManagerError(f"拒绝覆盖符号链接：{root}")
                    journal["roots"].append({"path": root, "existed": target.exists()})
                for current, _dirs, _files in os.walk(staging, topdown=False):
                    self._sync_directory(Path(current))
                self._write_restore_journal(journal)
                for root in roots:
                    target = self.data_root / root
                    source = staging / root
                    old = rollback / root
                    if target.exists():
                        os.replace(target, old)
                        self._sync_directory(rollback)
                        self._sync_directory(self.data_root)
                    if source.exists():
                        os.replace(source, target)
                        self._sync_directory(staging)
                        self._sync_directory(self.data_root)
                journal["state"] = "committed"
                self._write_restore_journal(journal)
            except Exception:
                # Keep the journal and originals if rollback itself fails.
                self._recover_restore_transaction()
                raise
            self._recover_restore_transaction()
        return {**self._summary(manifest, archive), "restore_selected": selected}

    def delete_backup(self, backup_id: str) -> dict[str, Any]:
        archive = self._archive_path(backup_id)
        with self._lock:
            self._ensure_open_locked()
            if not archive.is_file():
                raise BackupManagerError("备份不存在")
            archive.unlink()
        return self.list_backups()

    def download_backup(self, backup_id: str) -> dict[str, Any]:
        info = self.download_file(backup_id)
        archive = Path(info["path"])
        if info["size"] > MAX_JSON_DOWNLOAD_BYTES:
            raise BackupManagerError("备份过大，请使用文件下载或分块下载")
        data = archive.read_bytes()
        return {"id": backup_id, "filename": archive.name, "content_base64": base64.b64encode(data).decode("ascii"), "size": len(data)}

    def download_file(self, backup_id: str) -> dict[str, Any]:
        archive = self._archive_path(backup_id)
        self._read_manifest(archive)
        return {"path": str(archive), "filename": archive.name, "size": archive.stat().st_size}

    def download_chunk(self, backup_id: str, offset: int, length: int, revision: str = "") -> dict[str, Any]:
        if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= CHUNK_SIZE:
            raise BackupManagerError("备份下载范围无效")
        info = self.download_file(backup_id)
        with Path(info["path"]).open("rb") as incoming:
            stat = os.fstat(incoming.fileno())
            current_revision = hashlib.sha256(repr(self._stat_key(stat)).encode("ascii")).hexdigest()
            if revision and revision != current_revision:
                raise BackupManagerError("备份文件已变化，请重新下载")
            if offset > stat.st_size:
                raise BackupManagerError("备份下载偏移超限")
            incoming.seek(offset)
            data = incoming.read(length)
        return {"id": backup_id, "filename": info["filename"], "size": stat.st_size,
                "offset": offset, "next_offset": offset + len(data), "eof": offset + len(data) >= stat.st_size,
                "revision": current_revision, "content_base64": base64.b64encode(data).decode("ascii")}

    def upload_backup(self, filename: str, content: bytes, selected: Any = None) -> dict[str, Any]:
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise BackupManagerError(f"上传备份不得超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
        with self._lock:
            self._ensure_open_locked()
        with tempfile.NamedTemporaryFile(prefix=".upload-", suffix=".tmp", dir=self.backup_root, delete=False) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        try:
            return self.upload_backup_file(filename, temporary_path, selected)
        finally:
            temporary_path.unlink(missing_ok=True)

    def upload_backup_file(self, filename: str, temporary_path: Path, selected: Any = None) -> dict[str, Any]:
        """Consume a private staging file only after validating the full archive."""
        if not 0 < temporary_path.stat().st_size <= MAX_UPLOAD_BYTES:
            raise BackupManagerError("上传备份过大或为空")
        manifest = self._read_manifest_external(temporary_path)
        if manifest.get("kind") != "backup":
            raise BackupManagerError("上传文件不是完整备份")
        selected = self._restore_selection(manifest, selected)
        with self._lock:
            self._ensure_open_locked()
            if self._futures:
                raise BackupManagerError("请等待当前备份任务完成后恢复")
            if self._backup_count() + self._reserved_slots >= MAX_BACKUPS:
                raise BackupManagerError(f"最多保留 {MAX_BACKUPS} 个备份，请先删除旧备份")
            self._reserved_slots += 1
            archive = self.backup_root / f"bkp_{uuid.uuid4().hex[:16]}.zip"
            try:
                os.replace(temporary_path, archive)
            except Exception:
                self._reserved_slots -= 1
                raise
            self._restore_pending = True
            task = self._new_task("upload")

        def worker() -> dict[str, Any]:
            result = self._prepare_restore(archive, task["task_id"], selected) if self._defer_restore else self._restore_archive(archive, task["task_id"], selected)
            result["uploaded_filename"] = str(filename or "backup.zip")[:120]
            return result

        return self._submit(task, worker)

    def _read_manifest_external(self, path: Path) -> dict[str, Any]:
        with zipfile.ZipFile(path, "r") as incoming:
            return self._read_archive_manifest(incoming)

    def export_wardrobe(self) -> dict[str, Any]:
        with self._operation_lock:
            with self._lock:
                self._ensure_open_locked()
            with tempfile.TemporaryDirectory(prefix=".wardrobe-export-", dir=self.backup_root) as temporary:
                path = Path(temporary) / "selfie_image_wardrobe.zip"
                manifest = self._build_archive(path, ["wardrobe"], archive_kind="wardrobe-export")
                if path.stat().st_size > MAX_JSON_ARCHIVE_BYTES:
                    raise BackupManagerError("衣橱归档过大，请使用分块导出")
                data = path.read_bytes()
                return {"filename": path.name, "content_base64": base64.b64encode(data).decode("ascii"), "size": len(data), "file_count": len(manifest.get("files") or [])}

    def import_wardrobe(self, content: bytes) -> dict[str, Any]:
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise BackupManagerError("衣橱归档过大或为空")
        with self._operation_lock:
            with self._lock:
                self._ensure_open_locked()
            with tempfile.TemporaryDirectory(prefix=".wardrobe-import-", dir=self.backup_root) as temporary:
                path = Path(temporary) / "selfie_image_wardrobe.zip"
                path.write_bytes(content)
                return self.import_wardrobe_file(path)

    def import_wardrobe_file(self, path: Path) -> dict[str, Any]:
        with self._operation_lock:
            with self._lock:
                self._ensure_open_locked()
            if not 0 < path.stat().st_size <= MAX_UPLOAD_BYTES:
                raise BackupManagerError("衣橱归档过大或为空")
            manifest = self._read_manifest_external(path)
            if manifest.get("kind") != "wardrobe-export" or manifest.get("selected") != ["wardrobe"]:
                raise BackupManagerError("不是衣橱归档")
            result = self._restore_archive(path)
            return {"imported": True, **result}

    def start_wardrobe_export(self) -> dict[str, Any]:
        def build(path: Path) -> None:
            with self._operation_lock:
                with self._lock:
                    self._ensure_open_locked()
                self._build_archive(path, ["wardrobe"], archive_kind="wardrobe-export")

        return self.transfers.create_export(build)

    def start_archive_upload(self, kind: str, size: int, filename: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_open_locked()
        return self.transfers.start_upload(kind, size, filename)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
