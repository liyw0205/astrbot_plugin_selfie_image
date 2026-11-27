"""Shared business rules for operations exposed by both Web adapters."""

from __future__ import annotations

import base64
import inspect
import os
from dataclasses import dataclass, field
from typing import Any

from ..core.utils import (
    generation_record_media_sources,
    redact_generation_record,
    redact_generation_record_for_detail,
    redact_sensitive_data,
)
from ..features.config_manager import ConfigImportValidationError, ConfigRevisionConflictError
from ..generation.generation_records import metric_window_seconds
from .contracts import MAX_WEB_RECORD_ID_LENGTH, MAX_WEB_TASK_ID_LENGTH, WEB_TASK_ID_RE
from .services import (
    WebContractError,
    config_patch_payload,
    filter_record_page,
    normalize_task_ids,
    parse_bounded_int,
    parse_query_bool,
    parse_task_query,
    query_value,
    render_image_thumbnail,
    validate_task_id,
)


MAX_CACHE_IMAGE_PATH_LENGTH = 512
PAGE_PREVIEW_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class TaskControllerResult:
    data: Any
    message: Any = None


@dataclass(frozen=True)
class RecordControllerResult:
    data: Any
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProxyControllerResult:
    data: Any
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AssetControllerResult:
    data: Any
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CanvasControllerResult:
    data: Any
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConfigControllerResult:
    data: Any
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CacheControllerResult:
    data: Any
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HealthControllerResult:
    data: Any


@dataclass(frozen=True)
class QQGroupControllerResult:
    data: Any


@dataclass(frozen=True)
class SelfieProfileControllerResult:
    data: Any
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ImageChannelControllerResult:
    data: Any
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CachedImage:
    path: str
    info: dict[str, Any]


class ConfigController:
    """Shared configuration operations for both Web adapters."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    @staticmethod
    def _patch(payload: Any) -> dict[str, Any]:
        candidate = config_patch_payload(payload if isinstance(payload, dict) else {})
        # ``revision`` is transport metadata, never a persisted config key.
        candidate = dict(candidate)
        candidate.pop("revision", None)
        return candidate

    def get(self) -> ConfigControllerResult:
        try:
            data = self.plugin.get_config_for_web()
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        revision = getattr(self.plugin, "get_config_revision", lambda: None)()
        extra = {"revision": int(revision)} if revision is not None else {}
        return ConfigControllerResult(data, extra)

    @staticmethod
    def _revision(payload: Any) -> int | None:
        if not isinstance(payload, dict) or payload.get("revision") is None:
            return None
        try:
            value = int(payload.get("revision"))
        except (TypeError, ValueError) as exc:
            raise WebContractError("revision 必须是整数") from exc
        if value < 1:
            raise WebContractError("revision 必须大于 0")
        return value

    def update(self, payload: Any) -> ConfigControllerResult:
        patch = self._patch(payload)
        expected_revision = self._revision(payload)
        try:
            updater = self.plugin.update_config_from_web
            parameters = inspect.signature(updater).parameters
            accepts_revision = "expected_revision" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            data = (
                updater(patch, expected_revision=expected_revision)
                if accepts_revision
                else updater(patch)
            )
        except ConfigRevisionConflictError as exc:
            raise WebContractError(str(exc), 409) from exc
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        revision = getattr(self.plugin, "get_config_revision", lambda: None)()
        extra = {"revision": int(revision)} if revision is not None else {}
        return ConfigControllerResult(data, extra)

    def export(self, mode: Any) -> ConfigControllerResult:
        raw = str(mode or "").strip().lower() in {"raw", "plain", "original"}
        try:
            data = self.plugin.export_config_for_web(raw=raw)
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return ConfigControllerResult(data)

    def preview_import(self, payload: Any) -> ConfigControllerResult:
        patch = self._patch(payload)
        try:
            data = self.plugin.preview_config_import(patch)
        except (ValueError, ConfigImportValidationError) as exc:
            raise WebContractError(str(exc), 400) from exc
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return ConfigControllerResult(data)

    def import_config(self, payload: Any) -> ConfigControllerResult:
        patch = self._patch(payload)
        expected_revision = self._revision(payload)
        try:
            importer = self.plugin.import_config_from_web
            parameters = inspect.signature(importer).parameters
            accepts_revision = "expected_revision" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            data = (
                importer(patch, expected_revision=expected_revision)
                if accepts_revision
                else importer(patch)
            )
        except ConfigRevisionConflictError as exc:
            raise WebContractError(str(exc), 409) from exc
        except (ValueError, ConfigImportValidationError) as exc:
            raise WebContractError(str(exc), 400) from exc
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        revision = getattr(self.plugin, "get_config_revision", lambda: None)()
        extra = {"message": "配置导入成功"}
        if revision is not None:
            extra["revision"] = int(revision)
        return ConfigControllerResult(data, extra)


class ProxyController:
    """Shared proxy listing and probe argument/error contracts."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def list(self) -> ProxyControllerResult:
        try:
            data = self.plugin.list_proxies_for_web(mask_password=True)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return ProxyControllerResult(data)

    @staticmethod
    def _probe_args(payload: Any) -> tuple[str, Any]:
        body = payload if isinstance(payload, dict) else {}
        proxy_id = str(body.get("id") or body.get("proxy_id") or "")
        proxy = body.get("proxy") if isinstance(body.get("proxy"), dict) else body
        return proxy_id, proxy

    async def test(self, payload: Any) -> ProxyControllerResult:
        proxy_id, proxy = self._probe_args(payload)
        try:
            data = await self.plugin.test_proxy_connectivity(
                proxy_id=proxy_id, proxy=proxy,
            )
        except ValueError as exc:
            raise WebContractError(str(exc), 400) from exc
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return ProxyControllerResult(data)

    async def quality(self, payload: Any) -> ProxyControllerResult:
        proxy_id, proxy = self._probe_args(payload)
        try:
            data = await self.plugin.test_proxy_quality(
                proxy_id=proxy_id, proxy=proxy,
            )
        except ValueError as exc:
            raise WebContractError(str(exc), 400) from exc
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return ProxyControllerResult(data)


class AssetController:
    """Shared asset library and metadata operations for both Web adapters."""

    PAGE_LIMIT = 100
    EXPORT_LIMIT = 1000
    OFFSET_LIMIT = 10000

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def list(self, params: Any) -> AssetControllerResult:
        favorite = parse_query_bool(query_value(params, "favorite"))
        pinned = parse_query_bool(query_value(params, "pinned"))
        media_type = query_value(params, "media_type").strip().lower()
        if media_type not in {"", "image"}:
            raise WebContractError("当前仅支持图片媒体")
        raw_success = query_value(params, "success").strip().lower()
        success = parse_query_bool(raw_success)
        if raw_success and success is None:
            raise WebContractError("success 必须是 true 或 false")
        offset = parse_bounded_int(params, "offset", 0, 0, self.OFFSET_LIMIT)
        limit = parse_bounded_int(params, "limit", 48, 1, self.PAGE_LIMIT)
        sort = query_value(params, "sort", "recent").strip().lower()
        if sort not in {"recent", "oldest", "priority", "pinned", "favorite", "asc"}:
            raise WebContractError(
                "sort 必须是 recent、oldest、priority、pinned、favorite 或 asc"
            )
        try:
            query = getattr(self.plugin, "query_asset_records", None)
            if callable(query):
                data, meta = query(
                    favorite=favorite,
                    pinned=pinned,
                    tag=query_value(params, "tag"),
                    source=query_value(params, "source"),
                    model=query_value(params, "model"),
                    media_type=media_type,
                    success=success,
                    keyword=query_value(params, "q") or query_value(params, "keyword"),
                    start_time=query_value(params, "start_time"),
                    end_time=query_value(params, "end_time"),
                    offset=offset,
                    limit=limit,
                    sort=sort,
                )
                return AssetControllerResult(
                    redact_sensitive_data(data), {"count": len(data), **meta}
                )
            data = self.plugin.get_asset_records(
                favorite=favorite,
                pinned=pinned,
                tag=query_value(params, "tag"),
            )
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return AssetControllerResult(
            redact_sensitive_data(data),
            {"count": len(data), "total": len(data), "filtered": len(data), "offset": 0, "limit": len(data)},
        )

    def tags(self) -> AssetControllerResult:
        try:
            getter = getattr(self.plugin, "list_asset_tags", None)
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        if not callable(getter):
            raise WebContractError("当前版本不支持资产标签列表", 501)
        try:
            return AssetControllerResult(getter())
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc

    def export(self, raw_ids: Any) -> AssetControllerResult:
        try:
            exporter = getattr(self.plugin, "export_asset_metadata", None)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        if not callable(exporter):
            raise WebContractError("当前版本不支持资产导出", 501)
        raw = str(raw_ids or "").strip()
        ids = [item.strip() for item in raw.split(",") if item.strip()] if raw else None
        if ids is not None and len(ids) > self.EXPORT_LIMIT:
            raise WebContractError(f"单次最多导出 {self.EXPORT_LIMIT} 条资产", 400)
        try:
            return AssetControllerResult(exporter(ids))
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc

    def import_metadata(self, payload: Any) -> AssetControllerResult:
        try:
            importer = getattr(self.plugin, "import_asset_metadata", None)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        if not callable(importer):
            raise WebContractError("当前版本不支持资产导入", 501)
        try:
            data = importer(payload if isinstance(payload, dict) else {})
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return AssetControllerResult(data, {"message": "资产元数据已导入"})

    @staticmethod
    def _ids(payload: Any) -> tuple[list[Any], dict[str, Any]]:
        body = payload if isinstance(payload, dict) else {}
        ids = body.get("ids", body.get("record_ids"))
        if not isinstance(ids, list):
            raise WebContractError("ids 必须是数组", 400)
        return ids, body

    def update_metadata(self, payload: Any) -> AssetControllerResult:
        ids, body = self._ids(payload)
        try:
            data = self.plugin.update_records_asset_metadata(ids, body)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return AssetControllerResult(data, {"message": "批量资产标记已保存"})

    def delete(self, payload: Any) -> AssetControllerResult:
        ids, _body = self._ids(payload)
        try:
            data = self.plugin.delete_records(ids)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return AssetControllerResult(data, {"message": "资产已删除"})

    def studio_one(self, record_id: Any, payload: Any) -> AssetControllerResult:
        try:
            data = self.plugin.studio_add_asset(
                str(record_id or ""), payload if isinstance(payload, dict) else {}
            )
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return AssetControllerResult(data, {"message": "资产已加入画布"})

    def studio_many(self, payload: Any) -> AssetControllerResult:
        ids, body = self._ids(payload)
        try:
            data = self.plugin.studio_add_assets(ids, body)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return AssetControllerResult(data, {"message": "资产已加入画布"})


class AssetCollectionController:
    """Shared asset collection operations for both Web adapters."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def list(self) -> AssetControllerResult:
        try:
            return AssetControllerResult(self.plugin.list_asset_collections())
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc

    def create(self, payload: Any) -> AssetControllerResult:
        body = payload if isinstance(payload, dict) else {}
        try:
            data = self.plugin.create_asset_collection(body.get("name", ""), body)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return AssetControllerResult(data, {"message": "资产集合已创建"})

    def export(self) -> AssetControllerResult:
        try:
            data = self.plugin.export_asset_collections()
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return AssetControllerResult(data)

    def import_data(self, payload: Any) -> AssetControllerResult:
        body = payload if isinstance(payload, dict) else {}
        preview = (
            str(body.get("mode") or "").lower() == "preview"
            or bool(body.get("preview"))
        )
        try:
            data = self.plugin.import_asset_collections(body, preview=preview)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        message = "导入预览完成" if preview else "资产集合已导入"
        return AssetControllerResult(data, {"message": message})

    def detail(self, collection_id: Any) -> AssetControllerResult:
        try:
            data = self.plugin.get_asset_collection(str(collection_id or ""))
        except Exception as exc:
            raise WebContractError(str(exc), 404) from exc
        return AssetControllerResult(data)

    def update(self, collection_id: Any, payload: Any) -> AssetControllerResult:
        body = payload if isinstance(payload, dict) else {}
        try:
            data = self.plugin.update_asset_collection(
                str(collection_id or ""), body
            )
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return AssetControllerResult(data, {"message": "资产集合已更新"})

    def delete(self, collection_id: Any) -> AssetControllerResult:
        try:
            data = self.plugin.delete_asset_collection(str(collection_id or ""))
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return AssetControllerResult(data, {"message": "资产集合已删除"})

    def restore(self, payload: Any) -> AssetControllerResult:
        body = payload if isinstance(payload, dict) else {}
        token = str(body.get("token") or body.get("undo_token") or "").strip()
        if not token:
            raise WebContractError("token 不能为空", 400)
        try:
            data = self.plugin.restore_asset_collection(token)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return AssetControllerResult(data, {"message": "资产集合已恢复"})

    def update_records(
        self, collection_id: Any, payload: Any, *, remove: bool
    ) -> AssetControllerResult:
        body = payload if isinstance(payload, dict) else {}
        try:
            data = self.plugin.update_asset_collection_records(
                str(collection_id or ""), body, remove=remove
            )
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return AssetControllerResult(data, {"message": "集合内容已更新"})

    def studio(self, collection_id: Any, payload: Any) -> AssetControllerResult:
        body = payload if isinstance(payload, dict) else {}
        try:
            data = self.plugin.asset_collection_studio(
                str(collection_id or ""), body
            )
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return AssetControllerResult(data, {"message": "集合已加入画布"})


class CanvasController:
    """Shared Studio and legacy creation-canvas session operations."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def _call(
        self, method: str, *args: Any, status: int = 400, **kwargs: Any
    ) -> CanvasControllerResult:
        try:
            data = getattr(self.plugin, method)(*args, **kwargs)
        except Exception as exc:
            raise WebContractError(str(exc), status) from exc
        return CanvasControllerResult(data)

    def studio_list(self, *, include_metadata: bool = False) -> CanvasControllerResult:
        return self._call("studio_list", include_metadata=include_metadata, status=500)

    def studio_create(self, payload: Any) -> CanvasControllerResult:
        return self._call("studio_create", payload)

    def studio_copy(self, session_id: Any, payload: Any) -> CanvasControllerResult:
        try:
            copier = getattr(self.plugin, "studio_copy", None)
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        if not callable(copier):
            raise WebContractError("当前版本不支持复制画布会话", 501)
        try:
            data = copier(str(session_id or ""), payload)
        except ValueError as exc:
            raise WebContractError(str(exc), 409) from exc
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return CanvasControllerResult(data, {"message": "画布会话已复制"})

    def studio_get(self, session_id: Any) -> CanvasControllerResult:
        return self._call("studio_get", str(session_id or ""), status=404)

    def studio_update(self, session_id: Any, payload: Any) -> CanvasControllerResult:
        return self._call("studio_update", str(session_id or ""), payload)

    def studio_canvas(self, session_id: Any) -> CanvasControllerResult:
        return self._call("studio_canvas", str(session_id or ""), status=404)

    def studio_canvas_update(self, session_id: Any, payload: Any) -> CanvasControllerResult:
        return self._call("studio_canvas_update", str(session_id or ""), payload)

    def studio_canvas_node_create(self, session_id: Any, payload: Any) -> CanvasControllerResult:
        return self._call("studio_canvas_node_create", str(session_id or ""), payload)

    def studio_canvas_node_delete(
        self, session_id: Any, node_id: Any
    ) -> CanvasControllerResult:
        return self._call(
            "studio_canvas_node_delete", str(session_id or ""), str(node_id or ""), status=404
        )

    def studio_canvas_node_connect(
        self, session_id: Any, node_id: Any, payload: Any
    ) -> CanvasControllerResult:
        return self._call(
            "studio_canvas_node_connect", str(session_id or ""), str(node_id or ""), payload
        )

    def studio_delete(self, session_id: Any) -> CanvasControllerResult:
        return self._call("studio_delete", str(session_id or ""), status=404)

    def studio_set_slot(
        self, session_id: Any, slot_id: Any, payload: Any
    ) -> CanvasControllerResult:
        return self._call(
            "studio_set_slot", str(session_id or ""), str(slot_id or ""), payload
        )

    def studio_add_slot(self, session_id: Any, payload: Any) -> CanvasControllerResult:
        return self._call("studio_add_slot", str(session_id or ""), payload)

    def studio_reorder(self, session_id: Any, payload: Any) -> CanvasControllerResult:
        return self._call("studio_reorder", str(session_id or ""), payload)

    def studio_promote(self, session_id: Any, payload: Any) -> CanvasControllerResult:
        return self._call("studio_promote", str(session_id or ""), payload)

    def studio_run(self, session_id: Any, payload: Any) -> CanvasControllerResult:
        result = self._call(
            "start_studio_run", str(session_id or ""), payload, status=500
        )
        return CanvasControllerResult(redact_sensitive_data(result.data))


    def gallery(self, limit: Any, offset: Any) -> CanvasControllerResult:
        return self._call(
            "studio_gallery_images", limit=limit, offset=offset, status=400
        )


class RecordController:
    """Shared record reads and single-record actions for both Web adapters."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    @staticmethod
    def _record_id(record_id: Any) -> str:
        value = str(record_id or "").strip()
        if not value or len(value) > MAX_WEB_RECORD_ID_LENGTH:
            raise WebContractError("非法记录 ID")
        return value

    def list(self, params: Any) -> RecordControllerResult:
        try:
            records = [
                redact_generation_record(item)
                for item in self.plugin.get_recent_records(summary=True)
                if isinstance(item, dict)
            ]
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        page, meta = filter_record_page(records, params)
        return RecordControllerResult(page, meta)

    def metrics(self, window: Any) -> RecordControllerResult:
        try:
            window_seconds = metric_window_seconds(window)
        except ValueError as exc:
            raise WebContractError(str(exc), 400) from exc
        try:
            if window_seconds is None:
                data = self.plugin.get_generation_metrics()
            else:
                data = self.plugin.get_generation_metrics(window_seconds=window_seconds)
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return RecordControllerResult(redact_sensitive_data(data))

    def detail(self, record_id: Any, params: Any = None) -> RecordControllerResult:
        target = self._record_id(record_id)
        params = params or {}
        view = query_value(params, "view")
        field = query_value(params, "field")
        if view not in {"", "summary"}:
            raise WebContractError("未知记录视图")
        if field and field not in {
            "original_prompt", "request_prompt", "final_prompt", "request_prompt_en",
            "request_data", "response_data",
        }:
            raise WebContractError("未知记录字段")
        try:
            options = {"view": view, "field": field} if view or field else {}
            data = redact_generation_record_for_detail(self.plugin.get_record_for_web(target, **options))
        except Exception as exc:
            raise WebContractError(str(exc), 404) from exc
        return RecordControllerResult(data)

    def media_sources(self, record_id: Any) -> RecordControllerResult:
        target = self._record_id(record_id)
        try:
            data = generation_record_media_sources(self.plugin.get_record_for_web(target))
        except Exception as exc:
            raise WebContractError(str(exc), 404) from exc
        return RecordControllerResult(data)

    def update_metadata(self, record_id: Any, payload: Any) -> RecordControllerResult:
        target = self._record_id(record_id)
        try:
            data = self.plugin.update_record_asset_metadata(target, payload or {})
        except Exception as exc:
            raise WebContractError(str(exc), 404) from exc
        return RecordControllerResult(data, {"message": "资产标记已保存"})

    def reuse(self, record_id: Any) -> RecordControllerResult:
        target = self._record_id(record_id)
        try:
            data = self.plugin.get_record_reuse_payload(target)
        except Exception as exc:
            raise WebContractError(str(exc), 404) from exc
        return RecordControllerResult(redact_sensitive_data(data))

    def retry(self, record_id: Any, payload: Any) -> RecordControllerResult:
        target = self._record_id(record_id)
        try:
            retry = getattr(self.plugin, "start_record_retry_task", None)
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        if not callable(retry):
            raise WebContractError("当前版本不支持记录重试", 501)
        body = payload if isinstance(payload, dict) else {}
        feedback = str(body.get("feedback") or "")
        strategy = str(body.get("strategy") or body.get("retry_strategy") or "full")
        try:
            try:
                task = retry(target, feedback, strategy)
            except TypeError:
                task = retry(target, feedback)
        except ValueError as exc:
            raise WebContractError(str(exc), 400) from exc
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return RecordControllerResult(
            redact_sensitive_data(task), {"message": "已提交重试任务"}
        )

    def consistency(self) -> RecordControllerResult:
        try:
            checker = getattr(self.plugin, "inspect_storage_consistency", None)
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        if not callable(checker):
            raise WebContractError("当前版本不支持存储一致性检查", 501)
        try:
            data = checker()
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return RecordControllerResult(redact_sensitive_data(data))

    def repair_consistency(self, payload: Any) -> RecordControllerResult:
        body = payload if isinstance(payload, dict) else {}
        raw_confirm = body.get("confirm", False)
        confirm = (
            raw_confirm
            if isinstance(raw_confirm, bool)
            else str(raw_confirm).strip().lower() in {"1", "true", "yes", "on"}
        )
        try:
            repair = getattr(self.plugin, "repair_storage_consistency", None)
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        if not callable(repair):
            raise WebContractError("当前版本不支持存储一致性修复", 501)
        try:
            data = repair(confirm=confirm, kinds=body.get("kinds"))
        except Exception as exc:
            raise WebContractError(str(exc), 409 if confirm else 400) from exc
        message = "一致性修复完成" if confirm else "已生成修复预览"
        return RecordControllerResult(redact_sensitive_data(data), {"message": message})

    def compare(self, payload: Any) -> RecordControllerResult:
        body = payload if isinstance(payload, dict) else {}
        ids = body.get("ids", body.get("record_ids"))
        if not isinstance(ids, list):
            raise WebContractError("ids 必须是数组", 400)
        try:
            limit = int(body.get("limit") or 8)
        except (TypeError, ValueError) as exc:
            raise WebContractError(str(exc), 400) from exc
        try:
            comparer = getattr(self.plugin, "compare_records_for_web", None)
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        if not callable(comparer):
            raise WebContractError("当前版本不支持记录对比", 501)
        try:
            data = comparer(ids, limit)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return RecordControllerResult(data)

    def clear(self) -> RecordControllerResult:
        try:
            clearer = getattr(self.plugin, "clear_recent_records", None)
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        if not callable(clearer):
            raise WebContractError("当前版本不支持清空记录", 501)
        try:
            deleted = clearer()
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return RecordControllerResult({"deleted": deleted})


class TaskController:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def list_tasks(self, params: Any) -> TaskControllerResult:
        task_kwargs = parse_task_query(params)
        try:
            data = self.plugin.list_web_tasks(**task_kwargs)
        except WebContractError:
            raise
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return TaskControllerResult(redact_sensitive_data(data))

    def detail(self, task_id: Any) -> TaskControllerResult:
        task_id_text = validate_task_id(task_id, WEB_TASK_ID_RE, max_length=MAX_WEB_TASK_ID_LENGTH)
        try:
            getter = getattr(self.plugin, "get_web_task_detail", None)
            data = getter(task_id_text) if callable(getter) else self.plugin.get_web_image_task(task_id_text)
        except Exception as exc:
            raise WebContractError(str(exc), 404) from exc
        return TaskControllerResult(redact_sensitive_data(data))

    def status(self, task_id: Any) -> TaskControllerResult:
        task_id_text = validate_task_id(task_id, WEB_TASK_ID_RE, max_length=MAX_WEB_TASK_ID_LENGTH)
        try:
            data = self.plugin.get_web_image_task(task_id_text)
        except Exception as exc:
            raise WebContractError(str(exc), 404) from exc
        return TaskControllerResult(redact_sensitive_data(data))

    def export(self, raw_ids: Any = "") -> TaskControllerResult:
        raw = str(raw_ids or "").strip()
        ids = [item.strip() for item in raw.split(",") if item.strip()] if raw else None
        if ids is not None:
            if len(ids) > 200:
                raise WebContractError("包含非法任务 ID")
            try:
                for task_id in ids:
                    validate_task_id(task_id, WEB_TASK_ID_RE, max_length=MAX_WEB_TASK_ID_LENGTH)
            except WebContractError as exc:
                raise WebContractError("包含非法任务 ID", exc.status_code) from exc
        try:
            exporter = getattr(self.plugin, "export_web_tasks", None)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        if not callable(exporter):
            raise WebContractError("当前版本不支持任务导出", 501)
        try:
            data = exporter(ids)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return TaskControllerResult(redact_sensitive_data(data))

    def delete(self, payload: Any) -> TaskControllerResult:
        ids, validation_error = normalize_task_ids(
            payload, WEB_TASK_ID_RE, max_length=MAX_WEB_TASK_ID_LENGTH
        )
        if validation_error:
            raise WebContractError(validation_error)
        try:
            deleter = getattr(self.plugin, "delete_web_tasks", None)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        if not callable(deleter):
            raise WebContractError("当前版本不支持任务删除", 501)
        try:
            data = deleter(ids or [])
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return TaskControllerResult(redact_sensitive_data(data), "任务记录已删除")

    def retry(self, payload: Any) -> TaskControllerResult:
        ids, validation_error = normalize_task_ids(
            payload, WEB_TASK_ID_RE, max_length=MAX_WEB_TASK_ID_LENGTH
        )
        if validation_error:
            raise WebContractError(validation_error)
        body = payload if isinstance(payload, dict) else {}
        feedback = str(body.get("feedback") or "").strip()[:2000]
        strategy = str(body.get("strategy") or body.get("retry_strategy") or "full")
        scope_supplied = "scope" in body or "retry_scope" in body
        scope = str(body.get("scope") or body.get("retry_scope") or "all")
        try:
            retrier = getattr(self.plugin, "retry_web_tasks", None)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        if not callable(retrier):
            raise WebContractError("当前版本不支持任务重试", 501)
        try:
            if scope_supplied:
                try:
                    data = retrier(ids or [], feedback, strategy, scope)
                except TypeError:
                    try:
                        data = retrier(ids or [], feedback, strategy)
                    except TypeError:
                        data = retrier(ids or [], feedback)
            else:
                try:
                    data = retrier(ids or [], feedback, strategy)
                except TypeError:
                    data = retrier(ids or [], feedback)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return TaskControllerResult(redact_sensitive_data(data), "已提交任务重试")

    def cancel(self, task_id: Any) -> TaskControllerResult:
        task_id_text = validate_task_id(task_id, WEB_TASK_ID_RE, max_length=MAX_WEB_TASK_ID_LENGTH)
        try:
            task = self.plugin.get_web_image_task(task_id_text)
            is_active = str(task.get("status") or "") in {"queued", "running"}
        except Exception as exc:
            raise WebContractError(str(exc), 404) from exc
        if not is_active:
            raise WebContractError("任务已经结束，不能取消", 409)

        try:
            message = self.plugin.cancel_image_task(task_id_text, is_admin=True)
            updated = self.plugin.get_web_image_task(task_id_text)
            is_cancelled = str(updated.get("status") or "") == "cancelled" or bool(updated.get("cancel_requested"))
        except PermissionError as exc:
            raise WebContractError(str(exc), 403) from exc
        except Exception as exc:
            raise WebContractError(str(exc), 404) from exc
        if not is_cancelled:
            raise WebContractError("任务已经结束，不能取消", 409)
        return TaskControllerResult(redact_sensitive_data(updated), redact_sensitive_data(message))


class CacheController:
    """Shared cache cleanup and image validation for both Web adapters."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def cleanup(self, payload: Any) -> CacheControllerResult:
        body = payload if isinstance(payload, dict) else {}
        cleanup = getattr(self.plugin, "cleanup_image_cache_from_web", None)
        if not callable(cleanup):
            raise WebContractError("当前版本不支持手动缓存清理", 501)
        raw_confirm = body.get("confirm", False)
        confirm = (
            bool(raw_confirm)
            if not isinstance(raw_confirm, str)
            else raw_confirm.strip().lower() in {"1", "true", "yes", "on"}
        )
        token = str(body.get("plan_token") or "").strip()
        try:
            data = cleanup(confirm=confirm, plan_token=token)
        except ValueError as exc:
            raise WebContractError(str(exc), 409) from exc
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        message = "缓存清理完成" if confirm else "请确认缓存清理"
        return CacheControllerResult(data, {"message": message})

    def image_info(self, path: Any) -> CachedImage:
        rel_path = str(path or "")
        if len(rel_path) > MAX_CACHE_IMAGE_PATH_LENGTH:
            raise WebContractError("图片路径过长", 400)
        try:
            info = self.plugin.get_cached_image_info(rel_path)
            if not isinstance(info, dict):
                raise TypeError("缓存图片信息格式无效")
            if not info.get("exists"):
                raise WebContractError("图片已清理", 404)
            if not info.get("is_image"):
                raise WebContractError("缓存文件不是有效图片", 400)
        except WebContractError:
            raise
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return CachedImage(rel_path, info)

    def preview(
        self, path: Any, *, fallback_mime: str = "application/octet-stream",
    ) -> CacheControllerResult:
        rel_path = str(path or "")
        try:
            cached = self.image_info(rel_path)
        except WebContractError as exc:
            if exc.status_code != 404:
                raise
            message = "图片已清理"
            return CacheControllerResult(
                {"path": rel_path, "available": False, "message": message},
                {"message": message},
            )

        try:
            with open(str(cached.info.get("absolute_path") or ""), "rb") as handle:
                raw = handle.read(PAGE_PREVIEW_MAX_BYTES + 1)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        if len(raw) > PAGE_PREVIEW_MAX_BYTES:
            raise WebContractError("图片过大，请改用下载查看", 413)
        mime = str(cached.info.get("mime_type") or fallback_mime)
        return CacheControllerResult({
            "path": cached.path,
            "mime_type": mime,
            "size": len(raw),
            "data_url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}",
        })

    def thumbnail(self, path: Any) -> CacheControllerResult:
        cached = self.image_info(path)
        try:
            raw = render_image_thumbnail(str(cached.info.get("absolute_path") or ""))
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return CacheControllerResult({
            "path": cached.path,
            "mime_type": "image/jpeg",
            "size": len(raw),
            "data_url": f"data:image/jpeg;base64,{base64.b64encode(raw).decode('ascii')}",
        })


class HealthController:
    """Shared channel health history query for both Web adapters."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def history(self, params: Any) -> HealthControllerResult:
        getter = getattr(self.plugin, "get_channel_health_history", None)
        if not callable(getter):
            raise WebContractError("当前版本不支持渠道健康历史", 501)
        window = parse_bounded_int(params, "window", 86400, 0, 31 * 86400)
        media_type = query_value(params, "media_type")
        export = query_value(params, "export").strip().lower() in {"1", "true", "yes", "on"}
        try:
            try:
                data = getter(window_seconds=window, media_type=media_type, export=export)
            except TypeError:
                data = getter(window_seconds=window, media_type=media_type)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return HealthControllerResult(redact_sensitive_data(data))


class QQGroupController:
    """Shared QQ group operations for both Web adapters."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    async def list(self, params: Any) -> QQGroupControllerResult:
        getter = getattr(self.plugin, "list_qq_groups_for_web", None)
        if not callable(getter):
            raise WebContractError("当前版本不支持 QQ 群列表", 501)
        refresh = query_value(params, "refresh").strip().lower() in {"1", "true", "yes", "on"}
        try:
            data = await getter(force=refresh)
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return QQGroupControllerResult(redact_sensitive_data(data))

    def update(self, payload: Any) -> QQGroupControllerResult:
        updater = getattr(self.plugin, "update_group_chat_configs_from_web", None)
        if not callable(updater):
            raise WebContractError("当前版本不支持 QQ 群配置", 501)
        try:
            data = updater(payload)
        except ConfigRevisionConflictError as exc:
            raise WebContractError(str(exc), 409) from exc
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return QQGroupControllerResult(redact_sensitive_data(data))


class SelfieProfileController:
    """Shared selfie-reference and profile operations for both Web adapters."""

    ASYNC_WARDROBE_MUTATIONS = frozenset({"wardrobe_add", "wardrobe_delete", "wardrobe_toggle", "wardrobe_select"})

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def _call(self, hook_name: str, *args: Any, error_status: int = 500) -> Any:
        hook = getattr(self.plugin, hook_name, None)
        if not callable(hook):
            raise WebContractError("当前版本不支持自拍资料操作", 501)
        try:
            return hook(*args)
        except Exception as exc:
            raise WebContractError(str(exc), error_status) from exc

    @staticmethod
    def _result(data: Any, message: str | None = None) -> SelfieProfileControllerResult:
        extra = {"message": message} if message else {}
        return SelfieProfileControllerResult(redact_sensitive_data(data), extra)

    def get_reference(self, query: Any = None) -> SelfieProfileControllerResult:
        if (query or {}).get("view") == "original":
            slot = parse_bounded_int(query or {}, "slot", default=0, minimum=1, maximum=10)
            persona = getattr(self.plugin, "persona", None)
            list_images = getattr(persona, "list_image_pool", None)
            is_managed = getattr(persona, "_managed_image_path", None)
            if not callable(list_images) or not callable(is_managed):
                raise WebContractError("当前版本不支持形象图预览", 501)
            item = next((item for item in list_images() if item.get("slot") == slot), None)
            path = str((item or {}).get("path") or "")
            if not path or not is_managed(path) or not os.path.isfile(path):
                raise WebContractError("形象图不存在", 404)
            from ..core.utils import detect_mime_by_bytes

            with open(path, "rb") as stream:
                raw = stream.read(PAGE_PREVIEW_MAX_BYTES + 1)
            if len(raw) > PAGE_PREVIEW_MAX_BYTES:
                raise WebContractError("形象图超过预览大小上限", 413)
            mime = detect_mime_by_bytes(raw) or "image/png"
            return SelfieProfileControllerResult({
                "has_image": True, "image": f"data:{mime};base64," + base64.b64encode(raw).decode("ascii"),
            })
        if (query or {}).get("view") == "preview":
            persona = getattr(self.plugin, "persona", None)
            path_reader = getattr(persona, "get_reference_path", None)
            if callable(path_reader):
                path = path_reader()
                if not path:
                    return SelfieProfileControllerResult({"has_image": False})
                raw = render_image_thumbnail(path, max_dimension=768)
                return SelfieProfileControllerResult({
                    "has_image": True,
                    "image": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
                })
        return self._result(self._call("get_selfie_reference_payload"))

    def get_identity(self, query: Any = None) -> SelfieProfileControllerResult:
        if (query or {}).get("view") == "summary" and callable(getattr(self.plugin, "get_selfie_identity_payload", None)):
            return self._result(self._call("get_selfie_identity_payload"))
        return self.get_reference()

    def save_reference(self, payload: Any) -> SelfieProfileControllerResult:
        data = self._call("save_selfie_reference_from_web", payload, error_status=400)
        return self._result(data, "自拍参考图已保存")

    def clear_reference(self) -> SelfieProfileControllerResult:
        data = self._call("clear_selfie_reference_from_web")
        return self._result(data, "自拍参考图已清除")

    def set_appearance_type(self, payload: Any) -> SelfieProfileControllerResult:
        data = self._call("set_selfie_appearance_type_from_web", payload, error_status=400)
        return self._result(data, "形象类型已保存")

    def identity_mutation(self, payload: Any) -> SelfieProfileControllerResult:
        data = self._call("update_selfie_identity_from_web", payload, error_status=400)
        return self._result(data, "形象池已更新")

    def identity_thumbnails(self) -> SelfieProfileControllerResult:
        persona = getattr(self.plugin, "persona", None)
        list_images = getattr(persona, "list_image_pool", None)
        is_managed = getattr(persona, "_managed_image_path", None)
        if not callable(list_images) or not callable(is_managed):
            raise WebContractError("当前版本不支持形象图缩略图", 501)
        items = []
        for item in list_images():
            try:
                slot = int(item.get("slot"))
                path = str(item.get("path") or "")
                if not path or not is_managed(path) or not os.path.isfile(path):
                    continue
                raw = render_image_thumbnail(path, max_dimension=128)
            except (OSError, TypeError, ValueError):
                continue
            items.append({
                "slot": slot,
                "data_url": f"data:image/jpeg;base64,{base64.b64encode(raw).decode('ascii')}",
            })
        # Only generated JPEG bytes and slot numbers are returned. Text redaction
        # can mistake compressed pixels for credentials and corrupt the Base64.
        return SelfieProfileControllerResult({"items": items})

    async def ensure_daily_schedule(self) -> SelfieProfileControllerResult:
        hook = getattr(self.plugin, "ensure_selfie_daily_schedule_from_web", None)
        if not callable(hook):
            raise WebContractError("当前版本不支持自拍资料操作", 501)
        try:
            data = await hook()
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return self._result(data, "今日日程已就绪")

    async def refresh_local(self) -> SelfieProfileControllerResult:
        hook = getattr(self.plugin, "refresh_selfie_profile_local_from_web", None)
        if not callable(hook):
            raise WebContractError("当前版本不支持自拍资料操作", 501)
        try:
            data = await hook()
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return self._result(data, "今日日程已重新生成")

    def wardrobe_get(self) -> SelfieProfileControllerResult:
        return self._result(self._call("get_wardrobe_payload"))

    async def _wardrobe_change(self, hook_name: str, payload: Any, message: str) -> SelfieProfileControllerResult:
        data = self._call(hook_name, payload, error_status=400)
        try:
            if inspect.isawaitable(data):
                data = await data
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return self._result(data, message)

    async def wardrobe_add(self, payload: Any) -> SelfieProfileControllerResult:
        return await self._wardrobe_change("save_wardrobe_item_from_web", payload, "衣橱服装已添加")

    def wardrobe_update(self, payload: Any) -> SelfieProfileControllerResult:
        data = self._call("update_wardrobe_item_from_web", payload, error_status=400)
        return self._result(data, "衣橱服装已更新")

    async def wardrobe_delete(self, payload: Any) -> SelfieProfileControllerResult:
        return await self._wardrobe_change("delete_wardrobe_item_from_web", payload, "衣橱服装已删除")

    async def wardrobe_toggle(self, payload: Any) -> SelfieProfileControllerResult:
        return await self._wardrobe_change("toggle_wardrobe_item_from_web", payload, "衣橱服装状态已更新")

    def wardrobe_settings(self, payload: Any) -> SelfieProfileControllerResult:
        data = self._call("set_wardrobe_settings_from_web", payload, error_status=400)
        return self._result(data, "衣橱轮换方式已保存")

    async def wardrobe_select(self, payload: Any) -> SelfieProfileControllerResult:
        return await self._wardrobe_change("select_wardrobe_today_from_web", payload, "今日衣橱服装已指定")

    def wardrobe_export(self) -> SelfieProfileControllerResult:
        return self._result(self._call("export_wardrobe_from_web"), "衣橱已导出")

    def wardrobe_import(self, payload: Any) -> SelfieProfileControllerResult:
        data = self._call("import_wardrobe_from_web", payload, error_status=400)
        return self._result(data, "衣橱已导入")


class BackupController:
    """Shared selectable backup operations for both web adapters."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def _call(self, name: str, *args: Any, status: int = 500) -> Any:
        hook = getattr(self.plugin, name, None)
        if not callable(hook):
            raise WebContractError("当前版本不支持备份管理", 501)
        try:
            return hook(*args)
        except WebContractError:
            raise
        except Exception as exc:
            raise WebContractError(str(exc), status) from exc

    def options(self) -> dict[str, Any]:
        return self._call("backup_options_from_web")

    def list(self) -> dict[str, Any]:
        return self._call("list_backups_from_web")

    def create(self, payload: Any) -> dict[str, Any]:
        return self._call("create_backup_from_web", payload, status=400)

    def task(self, task_id: Any) -> dict[str, Any]:
        return self._call("backup_task_from_web", str(task_id or ""), status=404)

    def download(self, backup_id: Any, params: Any = None) -> dict[str, Any]:
        params = params or {}
        mode = query_value(params, "format")
        if mode == "chunk":
            offset = parse_bounded_int(params, "offset", 0, 0, 128 * 1024 * 1024)
            length = parse_bounded_int(params, "length", 1024 * 1024, 1, 1024 * 1024)
            return self._call("download_backup_chunk_from_web", str(backup_id or ""), offset, length,
                              query_value(params, "revision"), status=400)
        if mode == "file":
            return self._call("download_backup_file_from_web", str(backup_id or ""), status=404)
        if mode:
            raise WebContractError("备份下载格式无效", 400)
        return self._call("download_backup_from_web", str(backup_id or ""), status=404)

    @staticmethod
    def _restore_payload(payload: Any) -> dict[str, Any]:
        body = payload if isinstance(payload, dict) else {}
        if set(body) - {"selected"} or ("selected" in body and (
            not isinstance(body["selected"], list) or not body["selected"]
            or any(not isinstance(item, str) for item in body["selected"])
        )):
            raise WebContractError("请选择有效的恢复数据项", 400)
        return body

    def restore(self, backup_id: Any, payload: Any = None) -> dict[str, Any]:
        body = self._restore_payload(payload)
        args = (body["selected"],) if "selected" in body else ()
        return self._call("restore_backup_from_web", str(backup_id or ""), *args, status=400)

    def delete(self, payload: Any) -> dict[str, Any]:
        body = payload if isinstance(payload, dict) else {}
        return self._call("delete_backup_from_web", body, status=400)

    def upload(self, payload: Any) -> dict[str, Any]:
        return self._call("upload_backup_from_web", payload, status=400)

    def transfer(self, operation: str, transfer_id: str = "", payload: Any = None) -> dict[str, Any]:
        body = payload if isinstance(payload, dict) else {}
        allowed = {"start": {"kind", "size", "filename"},
                   "append": {"offset", "content_base64", "crc32"},
                   "commit": {"selected"}}.get(operation, set())
        if set(body) - allowed:
            raise WebContractError("归档请求包含未知字段", 400)
        if operation == "commit":
            self._restore_payload(body)
        if operation == "start" and (
            not isinstance(body.get("filename", ""), str) or len(body.get("filename", "")) > 255
            or not isinstance(body.get("kind"), str) or body.get("kind") not in {"backup", "wardrobe"}
        ):
            raise WebContractError("无效的归档类型或文件名", 400)
        hooks = {
            "start": ("start_archive_upload_from_web", (body,)),
            "append": ("append_archive_upload_from_web", (transfer_id, body)),
            "commit": ("commit_archive_upload_from_web", (transfer_id, body["selected"]) if "selected" in body else (transfer_id,)),
            "discard": ("discard_archive_transfer_from_web", (transfer_id,)),
            "export": ("start_wardrobe_export_from_web", ()),
        }
        name, args = hooks[operation]
        return self._call(name, *args, status=400)

    def export_download(self, transfer_id: str, params: Any) -> dict[str, Any]:
        mode = query_value(params, "format", "chunk")
        if mode not in {"chunk", "file"}:
            raise WebContractError("归档下载格式无效", 400)
        offset = parse_bounded_int(params, "offset", 0, 0, 128 * 1024 * 1024)
        length = parse_bounded_int(params, "length", 1024 * 1024, 1, 1024 * 1024)
        return self._call("download_archive_export_from_web", transfer_id, offset, length,
                          query_value(params, "revision"), mode == "file", status=400)


class ImageChannelController:
    """Shared channel-test and model-refresh operations for both Web adapters."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    async def test(self, payload: Any) -> ImageChannelControllerResult:
        hook = getattr(self.plugin, "web_test_image", None)
        if not callable(hook):
            raise WebContractError("当前版本不支持渠道测试", 501)
        try:
            data = await hook(payload)
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return ImageChannelControllerResult(redact_sensitive_data(data))

    def start_task(self, payload: Any) -> ImageChannelControllerResult:
        hook = getattr(self.plugin, "start_web_image_task", None)
        if not callable(hook):
            raise WebContractError("当前版本不支持渠道测试任务", 501)
        try:
            data = hook(payload)
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return ImageChannelControllerResult(redact_sensitive_data(data))

    async def refresh_models(self, payload: Any) -> ImageChannelControllerResult:
        hook = getattr(self.plugin, "web_refresh_image_models", None)
        if not callable(hook):
            raise WebContractError("当前版本不支持刷新图像模型", 501)
        try:
            data = await hook(payload)
            count = len(data)
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return ImageChannelControllerResult(redact_sensitive_data(data), {"count": count})
