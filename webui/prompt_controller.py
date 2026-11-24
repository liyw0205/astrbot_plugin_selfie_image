"""Shared prompt and COS catalog operations for both Web adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .services import (
    CATALOG_PAGE_SIZE,
    MAX_CATALOG_PAGE_SIZE,
    WebContractError,
    catalog_pagination_requested,
    flatten_cos_pool_rows,
    paginate_catalog_rows,
    parse_bounded_int,
    query_value,
)


@dataclass(frozen=True)
class PromptCatalogControllerResult:
    data: Any
    extra: dict[str, Any] = field(default_factory=dict)


class PromptCatalogController:
    """Shared prompt preset, COS catalog and creative prompt operations."""

    _MANAGED_IMAGE_FILTERS = frozenset(
        {"custom", "builtin", "ordinary", "special", "action", "upper", "lower"}
    )

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    @staticmethod
    def _body(payload: Any) -> dict[str, Any]:
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _params(params: Any) -> Mapping[str, Any]:
        return params if isinstance(params, Mapping) else {}

    def list_presets(
        self, params: Any, *, managed: bool = False
    ) -> PromptCatalogControllerResult:
        query = self._params(params)
        kind = query_value(query, "kind") or query_value(query, "media_type") or "image"
        try:
            if managed:
                data = self.plugin.list_managed_prompt_presets_for_web(kind)
            else:
                data = self.plugin.list_prompt_presets_for_web(kind)
            status_getter = getattr(self.plugin, "get_prompt_preset_status_for_web", None)
            status = (
                status_getter(kind)
                if callable(status_getter)
                else {"ok": True, "source": "legacy", "error": ""}
            )
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc

        extra: dict[str, Any] = {"load_status": status}
        rows = data
        preset_filter = (
            query_value(query, "preset_filter").strip().casefold()
            if managed and kind != "nai"
            else ""
        )
        if preset_filter in self._MANAGED_IMAGE_FILTERS:
            rows = [
                row
                for row in (data if isinstance(data, (list, tuple)) else [])
                if self._managed_image_preset_matches(row, preset_filter)
            ]
        if catalog_pagination_requested(query):
            limit = parse_bounded_int(
                query, "limit", CATALOG_PAGE_SIZE, 1, MAX_CATALOG_PAGE_SIZE
            )
            offset = parse_bounded_int(query, "offset", 0, 0, 1_000_000)
            try:
                page = paginate_catalog_rows(
                    rows,
                    offset=offset,
                    limit=limit,
                    query=query_value(query, "search"),
                    fields=("name", "title", "prompt", "description"),
                )
            except Exception as exc:
                raise WebContractError(str(exc), 500) from exc
            page["load_status"] = status
            if isinstance(status, Mapping) and "revision" in status:
                page["revision"] = status["revision"]
            extra["count"] = page["total"]
            return PromptCatalogControllerResult(page, extra)

        if not managed:
            try:
                extra["count"] = len(rows)
            except Exception as exc:
                raise WebContractError(str(exc), 500) from exc
        return PromptCatalogControllerResult(rows, extra)

    @staticmethod
    def _managed_image_preset_matches(row: Any, preset_filter: str) -> bool:
        if not isinstance(row, Mapping):
            return False
        group = str(row.get("preset_group") or row.get("special_group") or "").strip().casefold()
        if preset_filter == "custom":
            return str(row.get("source") or "").strip().casefold() != "builtin"
        if preset_filter == "builtin":
            return str(row.get("source") or "").strip().casefold() == "builtin"
        if preset_filter == "ordinary":
            return not group
        if preset_filter == "special":
            return bool(group)
        return group == preset_filter

    def save_preset(self, payload: Any) -> PromptCatalogControllerResult:
        try:
            data = self.plugin.save_prompt_preset_from_web(self._body(payload))
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return PromptCatalogControllerResult(data)

    def delete_preset(self, payload: Any) -> PromptCatalogControllerResult:
        body = self._body(payload)
        name = str(body.get("name") or "").strip()
        if not name:
            raise WebContractError("缺少预设名", 400)
        kind = str(body.get("kind") or body.get("media_type") or "image")
        try:
            revision = body.get("revision")
            if revision in (None, ""):
                data = self.plugin.delete_prompt_preset_from_web(name, kind)
            else:
                data = self.plugin.delete_prompt_preset_from_web(
                    name, kind, expected_revision=int(revision)
                )
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return PromptCatalogControllerResult(data)

    def import_presets(self, payload: Any) -> PromptCatalogControllerResult:
        try:
            data = self.plugin.import_prompt_presets_from_web(self._body(payload))
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return PromptCatalogControllerResult(data)

    def list_cos_look_sets(self) -> PromptCatalogControllerResult:
        try:
            data = self.plugin.list_cos_look_sets_for_web()
            count = len(data)
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return PromptCatalogControllerResult(data, {"count": count})

    def list_cos_pools(self, params: Any) -> PromptCatalogControllerResult:
        query = self._params(params)
        try:
            data = self.plugin.list_cos_pools_for_web()
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        if not catalog_pagination_requested(query):
            return PromptCatalogControllerResult(data)

        limit = parse_bounded_int(
            query, "limit", CATALOG_PAGE_SIZE, 1, MAX_CATALOG_PAGE_SIZE
        )
        offset = parse_bounded_int(query, "offset", 0, 0, 1_000_000)
        try:
            rows = flatten_cos_pool_rows(data)
            series_options = sorted(
                {
                    value
                    for row in rows
                    if (value := str(row.get("cos_type") or row.get("series") or "").strip())
                },
                key=str.casefold,
            )
            selected_series = str(query_value(query, "series") or "").strip().casefold()
            if selected_series:
                rows = [
                    row
                    for row in rows
                    if str(row.get("cos_type") or row.get("series") or "").strip().casefold()
                    == selected_series
                ]
            page = paginate_catalog_rows(
                rows,
                offset=offset,
                limit=limit,
                query=query_value(query, "search"),
                fields=("title", "id", "prompt", "tags", "cos_type", "series"),
            )
            page["series_options"] = series_options
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        if isinstance(data, Mapping):
            page["revision"] = data.get("revision", 1)
        return PromptCatalogControllerResult(page, {"count": page["total"]})

    def favorite_cos(self, payload: Any) -> PromptCatalogControllerResult:
        body = self._body(payload)
        look_id = str(body.get("id") or body.get("look_id") or "").strip()
        enabled = body.get("enabled", body.get("favorite", True))
        if not isinstance(enabled, bool):
            enabled = str(enabled).lower() in {"1", "true", "yes", "on", "是", "开启"}
        try:
            revision = body.get("revision")
            if revision in (None, ""):
                data = self.plugin.set_cos_favorite_for_web(look_id, enabled)
            else:
                data = self.plugin.set_cos_favorite_for_web(
                    look_id, enabled, expected_revision=int(revision)
                )
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return PromptCatalogControllerResult(data)

    def save_custom_cos(self, payload: Any) -> PromptCatalogControllerResult:
        try:
            data = self.plugin.save_custom_cos_for_web(self._body(payload))
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return PromptCatalogControllerResult(data, {"message": "自定义 COS 已保存"})

    def delete_custom_cos(self, payload: Any) -> PromptCatalogControllerResult:
        body = self._body(payload)
        look_id = str(body.get("id") or body.get("look_id") or "").strip()
        try:
            revision = body.get("revision")
            if revision in (None, ""):
                data = self.plugin.delete_custom_cos_for_web(look_id)
            else:
                data = self.plugin.delete_custom_cos_for_web(
                    look_id, expected_revision=int(revision)
                )
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return PromptCatalogControllerResult(data, {"message": "自定义 COS 已删除"})

    def export_cos(self) -> PromptCatalogControllerResult:
        try:
            data = self.plugin.export_cos_pool_for_web()
        except Exception as exc:
            raise WebContractError(str(exc), 500) from exc
        return PromptCatalogControllerResult(data)

    def import_cos(self, payload: Any) -> PromptCatalogControllerResult:
        try:
            data = self.plugin.import_cos_pool_for_web(self._body(payload))
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return PromptCatalogControllerResult(data, {"message": "COS 池已导入"})

    def render_template(self, payload: Any) -> PromptCatalogControllerResult:
        body = self._body(payload)
        prompt = str(body.get("prompt") or body.get("template") or "")
        try:
            data = self.plugin.render_creative_prompt(prompt, body)
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return PromptCatalogControllerResult(data)

    def variations(self, payload: Any) -> PromptCatalogControllerResult:
        body = self._body(payload)
        prompt = str(body.get("prompt") or "")
        try:
            count = int(body.get("count") or 1)
            data = {"variations": self.plugin.build_creative_variations(prompt, count, body)}
        except Exception as exc:
            raise WebContractError(str(exc), 400) from exc
        return PromptCatalogControllerResult(data)
