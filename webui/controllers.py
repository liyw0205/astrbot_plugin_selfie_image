"""Shared business rules for operations exposed by both Web adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.utils import (
    generation_record_media_sources,
    redact_generation_record,
    redact_generation_record_for_detail,
    redact_sensitive_data,
)
from ..features.config_manager import ConfigImportValidationError
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
    validate_task_id,
)


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


class ConfigController:
    """Shared configuration operations for both Web adapters."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    @staticmethod
    def _patch(payload: Any) -> dict[str, Any]:
        return config_patch_payload(payload if isinstance(payload, dict) else {})

    def get(self) -> ConfigControllerResult:
        try:
            data = self.plugin.get_config_for_web()
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return ConfigControllerResult(data)

    def update(self, payload: Any) -> ConfigControllerResult:
        patch = self._patch(payload)
        try:
            data = self.plugin.update_config_from_web(patch)
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return ConfigControllerResult(data)

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
        try:
            data = self.plugin.import_config_from_web(patch)
        except (ValueError, ConfigImportValidationError) as exc:
            raise WebContractError(str(exc), 400) from exc
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return ConfigControllerResult(data, {"message": "配置导入成功"})


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

    def creative_canvas_list(
        self, *, include_metadata: bool = False
    ) -> CanvasControllerResult:
        return self._call(
            "creative_canvas_list", include_metadata=include_metadata, status=500
        )

    def creative_canvas_create(self, payload: Any) -> CanvasControllerResult:
        return self._call("creative_canvas_create", payload)

    def creative_canvas_get(self, session_id: Any) -> CanvasControllerResult:
        return self._call("creative_canvas_get", str(session_id or ""), status=404)

    def creative_canvas_update(
        self, session_id: Any, payload: Any
    ) -> CanvasControllerResult:
        return self._call("creative_canvas_update", str(session_id or ""), payload)

    def creative_canvas_delete(self, session_id: Any) -> CanvasControllerResult:
        return self._call("creative_canvas_delete", str(session_id or ""), status=404)

    def creative_canvas_set_slot(
        self, session_id: Any, slot_id: Any, payload: Any
    ) -> CanvasControllerResult:
        return self._call(
            "creative_canvas_set_slot", str(session_id or ""), str(slot_id or ""), payload
        )

    def creative_canvas_add_slot(
        self, session_id: Any, payload: Any
    ) -> CanvasControllerResult:
        return self._call("creative_canvas_add_slot", str(session_id or ""), payload)

    def creative_canvas_reorder(
        self, session_id: Any, payload: Any
    ) -> CanvasControllerResult:
        return self._call("creative_canvas_reorder", str(session_id or ""), payload)

    def creative_canvas_promote(
        self, session_id: Any, payload: Any
    ) -> CanvasControllerResult:
        return self._call("creative_canvas_promote", str(session_id or ""), payload)

    def creative_canvas_run(
        self, session_id: Any, payload: Any
    ) -> CanvasControllerResult:
        result = self._call(
            "start_creative_canvas_run", str(session_id or ""), payload, status=500
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

    def detail(self, record_id: Any) -> RecordControllerResult:
        target = self._record_id(record_id)
        try:
            data = redact_generation_record_for_detail(self.plugin.get_record_for_web(target))
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
        try:
            retrier = getattr(self.plugin, "retry_web_tasks", None)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        if not callable(retrier):
            raise WebContractError("当前版本不支持任务重试", 501)
        try:
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
