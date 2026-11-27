"""Route aliases shared by the standalone and Dashboard Web adapters."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


WEB_TASK_ID_RE = re.compile(r"^(?:web|web-studio|cmd)-\d{8,}-\d+$")
MAX_WEB_TASK_ID_LENGTH = 64
MAX_WEB_RECORD_ID_LENGTH = 128


@dataclass(frozen=True)
class TaskRouteContract:
    endpoint: str
    flask_path: str
    dashboard_path: str
    methods: tuple[str, ...]
    flask_handler: str
    dashboard_handler: str
    description: str


# Both adapters bind this operation family from the same source. Their
# authentication and response envelopes remain adapter-owned.
TASK_ROUTE_CONTRACTS = (
    TaskRouteContract(
        "tasks", "/api/tasks", "tasks", ("GET",), "tasks", "page_tasks",
        "Selfie Image task queue",
    ),
    TaskRouteContract(
        "task_detail", "/api/tasks/<task_id>", "tasks/<task_id>", ("GET",),
        "task_detail", "page_task_detail", "Selfie Image task detail",
    ),
    TaskRouteContract(
        "tasks_export", "/api/tasks/export", "tasks/export", ("GET",),
        "tasks_export", "page_tasks_export", "Selfie Image export tasks",
    ),
    TaskRouteContract(
        "tasks_delete", "/api/tasks/delete", "tasks/delete", ("POST",),
        "tasks_delete", "page_tasks_delete", "Selfie Image delete tasks",
    ),
    TaskRouteContract(
        "tasks_retry", "/api/tasks/retry", "tasks/retry", ("POST",),
        "tasks_retry", "page_tasks_retry", "Selfie Image retry tasks",
    ),
    TaskRouteContract(
        "cancel_tasks", "/api/tasks/<task_id>/cancel", "tasks/<task_id>/cancel",
        ("POST",), "cancel_generation_task", "page_task_cancel", "Selfie Image cancel task",
    ),
    TaskRouteContract(
        "cancel_test_image_tasks", "/api/test-image-channel/tasks/<task_id>/cancel",
        "test-image-channel/tasks/<task_id>/cancel", ("POST",),
        "cancel_generation_task", "page_task_cancel", "Selfie Image cancel task",
    ),
    TaskRouteContract(
        "cancel_studio_tasks", "/api/studio/tasks/<task_id>/cancel",
        "studio/tasks/<task_id>/cancel", ("POST",),
        "cancel_generation_task", "page_task_cancel", "Selfie Image cancel task",
    ),
    TaskRouteContract(
        "studio_task_status", "/api/studio/tasks/<task_id>", "studio/tasks/<task_id>",
        ("GET",), "task_status", "page_studio_task", "Selfie Image studio task status",
    ),
)


@dataclass(frozen=True)
class RecordRouteContract:
    endpoint: str
    flask_path: str
    dashboard_path: str
    methods: tuple[str, ...]
    flask_handler: str
    dashboard_handler: str
    description: str


@dataclass(frozen=True)
class ConfigRouteContract:
    endpoint: str
    flask_path: str
    dashboard_path: str
    methods: tuple[str, ...]
    flask_handler: str
    dashboard_handler: str
    description: str


@dataclass(frozen=True)
class BackupRouteContract:
    endpoint: str
    flask_path: str
    dashboard_path: str
    methods: tuple[str, ...]
    flask_handler: str
    dashboard_handler: str
    description: str


@dataclass(frozen=True)
class ProxyRouteContract:
    endpoint: str
    flask_path: str
    dashboard_path: str
    methods: tuple[str, ...]
    flask_handler: str
    dashboard_handler: str
    description: str


@dataclass(frozen=True)
class AssetRouteContract:
    endpoint: str
    flask_path: str
    dashboard_path: str
    methods: tuple[str, ...]
    flask_handler: str
    dashboard_handler: str
    description: str


@dataclass(frozen=True)
class AssetCollectionRouteContract:
    endpoint: str
    flask_path: str
    dashboard_path: str
    methods: tuple[str, ...]
    flask_handler: str
    dashboard_handler: str
    description: str


@dataclass(frozen=True)
class CanvasRouteContract:
    endpoint: str
    flask_path: str
    dashboard_path: str
    methods: tuple[str, ...]
    flask_handler: str
    dashboard_handler: str
    description: str


@dataclass(frozen=True)
class PromptCatalogRouteContract:
    endpoint: str
    flask_path: str
    dashboard_path: str
    methods: tuple[str, ...]
    flask_handler: str
    dashboard_handler: str
    description: str


@dataclass(frozen=True)
class CacheRouteContract:
    endpoint: str
    flask_path: str
    dashboard_path: str
    methods: tuple[str, ...]
    flask_handler: str
    dashboard_handler: str
    description: str


@dataclass(frozen=True)
class HealthRouteContract:
    endpoint: str
    flask_path: str
    dashboard_path: str
    methods: tuple[str, ...]
    flask_handler: str
    dashboard_handler: str
    description: str


HEALTH_ROUTE_CONTRACTS = (
    HealthRouteContract(
        "health_history", "/api/health/history", "health/history", ("GET",),
        "health_history", "page_health_history", "Selfie Image channel health history",
    ),
)

# Kept as an empty import compatibility surface for older integrations. The
# conversation endpoints themselves were removed with the local context store.
CONTEXT_ROUTE_CONTRACTS: tuple[Any, ...] = ()


@dataclass(frozen=True)
class QQGroupRouteContract:
    endpoint: str
    flask_path: str
    dashboard_path: str
    methods: tuple[str, ...]
    flask_handler: str
    dashboard_handler: str
    description: str


QQ_GROUP_ROUTE_CONTRACTS = (
    QQGroupRouteContract(
        "qq_groups", "/api/qq-groups", "qq-groups", ("GET",),
        "qq_groups", "page_qq_groups", "Selfie Image QQ groups",
    ),
    QQGroupRouteContract(
        "qq_groups_config", "/api/qq-groups/config", "qq-groups/config", ("POST",),
        "qq_groups_config", "page_qq_groups_config", "Selfie Image QQ group config",
    ),
)


@dataclass(frozen=True)
class SelfieProfileRouteContract:
    endpoint: str
    flask_path: str
    dashboard_path: str
    methods: tuple[str, ...]
    flask_handler: str
    dashboard_handler: str
    description: str


SELFIE_PROFILE_ROUTE_CONTRACTS = (
    SelfieProfileRouteContract(
        "selfie_identity", "/api/selfie-identity", "selfie-identity", ("GET", "POST"),
        "selfie_identity", "page_selfie_identity", "Selfie Image identity pool mutation",
    ),
    SelfieProfileRouteContract(
        "selfie_identity_thumbnails", "/api/selfie-identity-thumbnails", "selfie-identity-thumbnails", ("GET",),
        "selfie_identity_thumbnails", "page_selfie_identity_thumbnails", "Selfie Image identity pool thumbnails",
    ),
    SelfieProfileRouteContract(
        "selfie_reference_get", "/api/selfie-reference", "selfie-reference", ("GET",),
        "selfie_reference_get", "page_selfie_reference_get", "Selfie Image get reference",
    ),
    SelfieProfileRouteContract(
        "selfie_reference_post", "/api/selfie-reference", "selfie-reference", ("POST",),
        "selfie_reference_post", "page_selfie_reference_post", "Selfie Image save reference",
    ),
    SelfieProfileRouteContract(
        "selfie_reference_clear", "/api/selfie-reference/clear", "selfie-reference/clear", ("POST",),
        "selfie_reference_clear", "page_selfie_reference_clear", "Selfie Image clear reference",
    ),
    SelfieProfileRouteContract(
        "selfie_appearance_type", "/api/selfie-appearance-type", "selfie-appearance-type", ("POST",),
        "selfie_appearance_type", "page_selfie_appearance_type", "Selfie Image appearance type",
    ),
    SelfieProfileRouteContract(
        "selfie_profile_refresh_local", "/api/selfie-profile/refresh-local",
        "selfie-profile/refresh-local", ("POST",), "selfie_profile_refresh_local",
        "page_selfie_profile_refresh_local", "Selfie Image local refresh profile",
    ),
    SelfieProfileRouteContract(
        "selfie_profile_ensure_today", "/api/selfie-profile/ensure-today",
        "selfie-profile/ensure-today", ("POST",), "selfie_profile_ensure_today",
        "page_selfie_profile_ensure_today", "Selfie Image ensure today's schedule",
    ),
    SelfieProfileRouteContract(
        "selfie_wardrobe_get", "/api/selfie-wardrobe", "selfie-wardrobe", ("GET",),
        "selfie_wardrobe_get", "page_selfie_wardrobe_get", "Selfie Image wardrobe list",
    ),
    SelfieProfileRouteContract(
        "selfie_wardrobe_add", "/api/selfie-wardrobe/add", "selfie-wardrobe/add", ("POST",),
        "selfie_wardrobe_add", "page_selfie_wardrobe_add", "Selfie Image add wardrobe item",
    ),
    SelfieProfileRouteContract(
        "selfie_wardrobe_update", "/api/selfie-wardrobe/update", "selfie-wardrobe/update", ("POST",),
        "selfie_wardrobe_update", "page_selfie_wardrobe_update", "Selfie Image update wardrobe item",
    ),
    SelfieProfileRouteContract(
        "selfie_wardrobe_delete", "/api/selfie-wardrobe/delete", "selfie-wardrobe/delete", ("POST",),
        "selfie_wardrobe_delete", "page_selfie_wardrobe_delete", "Selfie Image delete wardrobe item",
    ),
    SelfieProfileRouteContract(
        "selfie_wardrobe_toggle", "/api/selfie-wardrobe/toggle", "selfie-wardrobe/toggle", ("POST",),
        "selfie_wardrobe_toggle", "page_selfie_wardrobe_toggle", "Selfie Image toggle wardrobe item",
    ),
    SelfieProfileRouteContract(
        "selfie_wardrobe_export", "/api/selfie-wardrobe/export", "selfie-wardrobe/export", ("GET",),
        "selfie_wardrobe_export", "page_selfie_wardrobe_export", "Selfie Image export wardrobe",
    ),
    SelfieProfileRouteContract(
        "selfie_wardrobe_import", "/api/selfie-wardrobe/import", "selfie-wardrobe/import", ("POST",),
        "selfie_wardrobe_import", "page_selfie_wardrobe_import", "Selfie Image import wardrobe",
    ),
    SelfieProfileRouteContract(
        "selfie_wardrobe_settings", "/api/selfie-wardrobe/settings", "selfie-wardrobe/settings", ("POST",),
        "selfie_wardrobe_settings", "page_selfie_wardrobe_settings", "Selfie Image wardrobe rotation settings",
    ),
    SelfieProfileRouteContract(
        "selfie_wardrobe_select", "/api/selfie-wardrobe/select", "selfie-wardrobe/select", ("POST",),
        "selfie_wardrobe_select", "page_selfie_wardrobe_select", "Selfie Image select today's wardrobe item",
    ),
)


@dataclass(frozen=True)
class ImageChannelRouteContract:
    endpoint: str
    flask_path: str
    dashboard_path: str
    methods: tuple[str, ...]
    flask_handler: str
    dashboard_handler: str
    description: str


IMAGE_CHANNEL_ROUTE_CONTRACTS = (
    ImageChannelRouteContract(
        "test_image_channel", "/api/test-image-channel", "test-image-channel", ("POST",),
        "test_image_channel", "page_test_image_channel", "Selfie Image sync channel test",
    ),
    ImageChannelRouteContract(
        "test_image_channel_task_start", "/api/test-image-channel/tasks",
        "test-image-channel/tasks", ("POST",), "test_image_channel_task_start",
        "page_test_image_task_start", "Selfie Image start channel test task",
    ),
    ImageChannelRouteContract(
        "test_image_channel_task_status", "/api/test-image-channel/tasks/<task_id>",
        "test-image-channel/tasks/<task_id>", ("GET",),
        "test_image_channel_task_status", "page_test_image_task_status",
        "Selfie Image channel test task status",
    ),
    ImageChannelRouteContract(
        "refresh_image_models", "/api/refresh-image-models", "refresh-image-models",
        ("POST",), "refresh_image_models", "page_refresh_image_models",
        "Selfie Image refresh models",
    ),
)


CONFIG_ROUTE_CONTRACTS = (
    ConfigRouteContract(
        "config_get", "/api/config", "config", ("GET",), "config_get",
        "page_config_get", "Selfie Image get config",
    ),
    ConfigRouteContract(
        "config_update", "/api/config", "config", ("POST",), "config_update",
        "page_config_post", "Selfie Image save config",
    ),
    ConfigRouteContract(
        "config_export", "/api/config/export", "config/export", ("GET",),
        "config_export", "page_config_export", "Selfie Image export config",
    ),
    ConfigRouteContract(
        "config_import_preview", "/api/config/import/preview", "config/import/preview",
        ("POST",), "config_import_preview", "page_config_import_preview",
        "Selfie Image preview config import",
    ),
    ConfigRouteContract(
        "config_import", "/api/config/import", "config/import", ("POST",),
        "config_import", "page_config_import", "Selfie Image import config",
    ),
)


BACKUP_ROUTE_CONTRACTS = (
    BackupRouteContract(
        "backups_options", "/api/backups/options", "backups/options", ("GET",),
        "backups_options", "page_backups_options", "Selfie Image backup options",
    ),
    BackupRouteContract(
        "backups_list", "/api/backups", "backups", ("GET",),
        "backups_list", "page_backups_list", "Selfie Image backup list",
    ),
    BackupRouteContract(
        "backups_create", "/api/backups", "backups", ("POST",),
        "backups_create", "page_backups_create", "Selfie Image create backup",
    ),
    BackupRouteContract(
        "backups_task", "/api/backups/tasks/<task_id>", "backups/tasks/<task_id>", ("GET",),
        "backups_task", "page_backups_task", "Selfie Image backup task status",
    ),
    BackupRouteContract(
        "backups_download", "/api/backups/<backup_id>/download", "backups/<backup_id>/download", ("GET",),
        "backups_download", "page_backups_download", "Selfie Image download backup",
    ),
    BackupRouteContract(
        "backups_restore", "/api/backups/<backup_id>/restore", "backups/<backup_id>/restore", ("POST",),
        "backups_restore", "page_backups_restore", "Selfie Image restore backup",
    ),
    BackupRouteContract(
        "backups_delete", "/api/backups/delete", "backups/delete", ("POST",),
        "backups_delete", "page_backups_delete", "Selfie Image delete backup",
    ),
    BackupRouteContract(
        "backups_upload", "/api/backups/upload", "backups/upload", ("POST",),
        "backups_upload", "page_backups_upload", "Selfie Image upload backup",
    ),
    BackupRouteContract(
        "archive_upload_start", "/api/archive-transfers/uploads", "archive-transfers/uploads", ("POST",),
        "archive_upload_start", "page_archive_upload_start", "Selfie Image start archive upload",
    ),
    BackupRouteContract(
        "archive_upload_chunk", "/api/archive-transfers/<transfer_id>/chunk", "archive-transfers/<transfer_id>/chunk", ("POST",),
        "archive_upload_chunk", "page_archive_upload_chunk", "Selfie Image append archive chunk",
    ),
    BackupRouteContract(
        "archive_upload_commit", "/api/archive-transfers/<transfer_id>/commit", "archive-transfers/<transfer_id>/commit", ("POST",),
        "archive_upload_commit", "page_archive_upload_commit", "Selfie Image commit archive upload",
    ),
    BackupRouteContract(
        "archive_transfer_discard", "/api/archive-transfers/<transfer_id>/discard", "archive-transfers/<transfer_id>/discard", ("POST",),
        "archive_transfer_discard", "page_archive_transfer_discard", "Selfie Image discard archive transfer",
    ),
    BackupRouteContract(
        "archive_wardrobe_export", "/api/archive-transfers/wardrobe-export", "archive-transfers/wardrobe-export", ("POST",),
        "archive_wardrobe_export", "page_archive_wardrobe_export", "Selfie Image prepare wardrobe export",
    ),
    BackupRouteContract(
        "archive_export_download", "/api/archive-transfers/<transfer_id>/download", "archive-transfers/<transfer_id>/download", ("GET",),
        "archive_export_download", "page_archive_export_download", "Selfie Image download wardrobe export",
    ),
)


PROXY_ROUTE_CONTRACTS = (
    ProxyRouteContract(
        "proxies_list", "/api/proxies", "proxies", ("GET",), "proxies_list",
        "page_proxies_list", "Selfie Image get proxies",
    ),
    ProxyRouteContract(
        "proxies_test", "/api/proxies/test", "proxies/test", ("POST",),
        "proxies_test", "page_proxy_test", "Selfie Image test proxy",
    ),
    ProxyRouteContract(
        "proxies_quality", "/api/proxies/quality-check", "proxies/quality-check",
        ("POST",), "proxies_quality", "page_proxy_quality",
        "Selfie Image check proxy quality",
    ),
)


ASSET_ROUTE_CONTRACTS = (
    AssetRouteContract(
        "assets", "/api/assets", "assets", ("GET",), "assets",
        "page_assets", "Selfie Image asset library",
    ),
    AssetRouteContract(
        "assets_tags", "/api/assets/tags", "assets/tags", ("GET",),
        "assets_tags", "page_assets_tags", "Selfie Image asset tags",
    ),
    AssetRouteContract(
        "assets_export", "/api/assets/export", "assets/export", ("GET",),
        "assets_export", "page_assets_export", "Selfie Image asset metadata export",
    ),
    AssetRouteContract(
        "assets_import", "/api/assets/import", "assets/import", ("POST",),
        "assets_import", "page_assets_import", "Selfie Image asset metadata import",
    ),
    AssetRouteContract(
        "assets_metadata", "/api/assets/metadata", "assets/metadata", ("POST",),
        "assets_metadata", "page_assets_metadata", "Selfie Image batch asset metadata",
    ),
    AssetRouteContract(
        "assets_delete", "/api/assets/delete", "assets/delete", ("POST",),
        "assets_delete", "page_assets_delete", "Selfie Image batch asset delete",
    ),
    AssetRouteContract(
        "asset_studio", "/api/assets/<record_id>/studio", "assets/<record_id>/studio",
        ("POST",), "asset_studio", "page_asset_studio",
        "Selfie Image add asset to studio",
    ),
    AssetRouteContract(
        "assets_studio", "/api/assets/studio", "assets/studio", ("POST",),
        "assets_studio", "page_assets_studio", "Selfie Image batch add assets to studio",
    ),
)


ASSET_COLLECTION_ROUTE_CONTRACTS = (
    AssetCollectionRouteContract(
        "asset_collections", "/api/assets/collections", "assets/collections",
        ("GET",), "asset_collections", "page_asset_collections",
        "Selfie Image asset collections",
    ),
    AssetCollectionRouteContract(
        "asset_collection_create", "/api/assets/collections", "assets/collections",
        ("POST",), "asset_collection_create", "page_asset_collection_create",
        "Selfie Image create asset collection",
    ),
    AssetCollectionRouteContract(
        "asset_collection_restore", "/api/assets/collections/restore",
        "assets/collections/restore", ("POST",), "asset_collection_restore",
        "page_asset_collection_restore", "Selfie Image restore deleted asset collection",
    ),
    AssetCollectionRouteContract(
        "asset_collections_import", "/api/assets/collections/import",
        "assets/collections/import", ("POST",), "asset_collections_import",
        "page_asset_collections_import", "Selfie Image import asset collections",
    ),
    AssetCollectionRouteContract(
        "asset_collections_export", "/api/assets/collections/export",
        "assets/collections/export", ("GET",), "asset_collections_export",
        "page_asset_collections_export", "Selfie Image export asset collections",
    ),
    AssetCollectionRouteContract(
        "asset_collection_detail", "/api/assets/collections/<collection_id>",
        "assets/collections/<collection_id>", ("GET",), "asset_collection_detail",
        "page_asset_collection_detail", "Selfie Image asset collection detail",
    ),
    AssetCollectionRouteContract(
        "asset_collection_update", "/api/assets/collections/<collection_id>",
        "assets/collections/<collection_id>", ("POST",), "asset_collection_update",
        "page_asset_collection_update", "Selfie Image update asset collection",
    ),
    AssetCollectionRouteContract(
        "asset_collection_delete", "/api/assets/collections/<collection_id>/delete",
        "assets/collections/<collection_id>/delete", ("POST",), "asset_collection_delete",
        "page_asset_collection_delete", "Selfie Image delete asset collection",
    ),
    AssetCollectionRouteContract(
        "asset_collection_add", "/api/assets/collections/<collection_id>/add",
        "assets/collections/<collection_id>/add", ("POST",), "asset_collection_add",
        "page_asset_collection_add", "Selfie Image add assets to collection",
    ),
    AssetCollectionRouteContract(
        "asset_collection_remove", "/api/assets/collections/<collection_id>/remove",
        "assets/collections/<collection_id>/remove", ("POST",), "asset_collection_remove",
        "page_asset_collection_remove", "Selfie Image remove assets from collection",
    ),
    AssetCollectionRouteContract(
        "asset_collection_studio", "/api/assets/collections/<collection_id>/studio",
        "assets/collections/<collection_id>/studio", ("POST",), "asset_collection_studio",
        "page_asset_collection_studio", "Selfie Image send collection to studio",
    ),
)


CACHE_ROUTE_CONTRACTS = (
    CacheRouteContract(
        "cache_cleanup", "/api/cache/cleanup", "cache/cleanup", ("POST",),
        "cache_cleanup", "page_cache_cleanup", "Selfie Image cleanup cache",
    ),
    CacheRouteContract(
        "cache_image", "/api/cache-image", "cache-image", ("GET",),
        "cache_image", "page_cache_image_file", "Selfie Image cache image download",
    ),
    CacheRouteContract(
        "cache_image_thumbnail", "/api/cache-image-thumbnail", "cache-image-thumbnail",
        ("GET",), "cache_image_thumbnail", "page_cache_image_thumbnail",
        "Selfie Image cache image thumbnail",
    ),
    CacheRouteContract(
        "cache_image_preview", "/api/cache-image-preview", "cache-image-preview",
        ("GET",), "cache_image_preview", "page_cache_image_preview",
        "Selfie Image cache image preview",
    ),
)


CANVAS_ROUTE_CONTRACTS = (
    CanvasRouteContract(
        "studio_sessions_list", "/api/studio/sessions", "studio/sessions",
        ("GET",), "studio_sessions_list", "page_studio_list",
        "Selfie Image studio list",
    ),
    CanvasRouteContract(
        "studio_sessions_create", "/api/studio/sessions", "studio/sessions",
        ("POST",), "studio_sessions_create", "page_studio_create",
        "Selfie Image studio create",
    ),
    CanvasRouteContract(
        "studio_session_copy", "/api/studio/sessions/<session_id>/copy",
        "studio/sessions/<session_id>/copy", ("POST",), "studio_session_copy",
        "page_studio_copy", "Selfie Image copy studio session",
    ),
    CanvasRouteContract(
        "studio_session_get", "/api/studio/sessions/<session_id>",
        "studio/sessions/<session_id>", ("GET",), "studio_session_get",
        "page_studio_get", "Selfie Image studio get",
    ),
    CanvasRouteContract(
        "studio_session_update", "/api/studio/sessions/<session_id>",
        "studio/sessions/<session_id>", ("POST",), "studio_session_update",
        "page_studio_update", "Selfie Image studio update",
    ),
    CanvasRouteContract(
        "studio_session_canvas_get", "/api/studio/sessions/<session_id>/canvas",
        "studio/sessions/<session_id>/canvas", ("GET",), "studio_session_canvas_get",
        "page_studio_canvas_get", "Selfie Image studio canvas",
    ),
    CanvasRouteContract(
        "studio_session_canvas_update", "/api/studio/sessions/<session_id>/canvas",
        "studio/sessions/<session_id>/canvas", ("POST",), "studio_session_canvas_update",
        "page_studio_canvas_update", "Selfie Image update studio canvas",
    ),
    CanvasRouteContract(
        "studio_canvas_node_create", "/api/studio/sessions/<session_id>/canvas/nodes",
        "studio/sessions/<session_id>/canvas/nodes", ("POST",),
        "studio_canvas_node_create", "page_studio_canvas_node_create",
        "Selfie Image studio canvas node create",
    ),
    CanvasRouteContract(
        "studio_canvas_node_delete", "/api/studio/sessions/<session_id>/canvas/nodes/<node_id>",
        "studio/sessions/<session_id>/canvas/nodes/<node_id>", ("DELETE", "POST"),
        "studio_canvas_node_delete", "page_studio_canvas_node_delete",
        "Selfie Image studio canvas node delete",
    ),
    CanvasRouteContract(
        "studio_canvas_node_connect", "/api/studio/sessions/<session_id>/canvas/nodes/<node_id>/connect",
        "studio/sessions/<session_id>/canvas/nodes/<node_id>/connect", ("POST",),
        "studio_canvas_node_connect", "page_studio_canvas_node_connect",
        "Selfie Image studio canvas node connect",
    ),
    CanvasRouteContract(
        "studio_session_delete", "/api/studio/sessions/<session_id>/delete",
        "studio/sessions/<session_id>/delete", ("POST",), "studio_session_delete",
        "page_studio_delete", "Selfie Image studio delete",
    ),
    CanvasRouteContract(
        "studio_session_slot", "/api/studio/sessions/<session_id>/slots/<slot_id>",
        "studio/sessions/<session_id>/slots/<slot_id>", ("POST",), "studio_session_slot",
        "page_studio_set_slot", "Selfie Image studio slot",
    ),
    CanvasRouteContract(
        "studio_session_add_slot", "/api/studio/sessions/<session_id>/slots",
        "studio/sessions/<session_id>/slots", ("POST",), "studio_session_add_slot",
        "page_studio_add_slot", "Selfie Image studio add slot",
    ),
    CanvasRouteContract(
        "studio_session_reorder", "/api/studio/sessions/<session_id>/reorder",
        "studio/sessions/<session_id>/reorder", ("POST",), "studio_session_reorder",
        "page_studio_reorder", "Selfie Image studio reorder",
    ),
    CanvasRouteContract(
        "studio_session_promote", "/api/studio/sessions/<session_id>/promote",
        "studio/sessions/<session_id>/promote", ("POST",), "studio_session_promote",
        "page_studio_promote", "Selfie Image studio promote",
    ),
    CanvasRouteContract(
        "studio_session_run", "/api/studio/sessions/<session_id>/run",
        "studio/sessions/<session_id>/run", ("POST",), "studio_session_run",
        "page_studio_run", "Selfie Image studio run",
    ),
    CanvasRouteContract(
        "studio_gallery", "/api/studio/gallery", "studio/gallery", ("GET",),
        "studio_gallery", "page_studio_gallery", "Selfie Image studio gallery",
    ),
)


PROMPT_CATALOG_ROUTE_CONTRACTS = (
    PromptCatalogRouteContract(
        "prompt_presets", "/api/prompt-presets", "prompt-presets", ("GET",),
        "prompt_presets", "page_prompt_presets", "Selfie Image prompt presets",
    ),
    PromptCatalogRouteContract(
        "prompt_presets_manage", "/api/prompt-presets/manage", "prompt-presets/manage",
        ("GET",), "prompt_presets_manage", "page_prompt_presets_manage",
        "Selfie Image managed prompt presets",
    ),
    PromptCatalogRouteContract(
        "prompt_preset_save", "/api/prompt-presets/manage/save", "prompt-presets/manage/save",
        ("POST",), "prompt_preset_save", "page_prompt_preset_save",
        "Selfie Image save prompt preset",
    ),
    PromptCatalogRouteContract(
        "prompt_preset_delete", "/api/prompt-presets/manage/delete", "prompt-presets/manage/delete",
        ("POST",), "prompt_preset_delete", "page_prompt_preset_delete",
        "Selfie Image delete prompt preset",
    ),
    PromptCatalogRouteContract(
        "prompt_presets_import", "/api/prompt-presets/manage/import",
        "prompt-presets/manage/import", ("POST",), "prompt_presets_import",
        "page_prompt_presets_import", "Selfie Image import prompt presets",
    ),
    PromptCatalogRouteContract(
        "cos_look_sets", "/api/cos-look-sets", "cos-look-sets", ("GET",),
        "cos_look_sets", "page_cos_look_sets", "Selfie Image COS look sets",
    ),
    PromptCatalogRouteContract(
        "cos_pools", "/api/cos-pools", "cos-pools", ("GET",), "cos_pools",
        "page_cos_pools", "Selfie Image COS pools",
    ),
    PromptCatalogRouteContract(
        "cos_pool_favorite", "/api/cos-pools/favorite", "cos-pools/favorite",
        ("POST",), "cos_pool_favorite", "page_cos_pool_favorite",
        "Selfie Image favorite COS",
    ),
    PromptCatalogRouteContract(
        "cos_custom_save", "/api/cos-pools/custom/save", "cos-pools/custom/save",
        ("POST",), "cos_custom_save", "page_cos_custom_save",
        "Selfie Image save custom COS",
    ),
    PromptCatalogRouteContract(
        "cos_custom_delete", "/api/cos-pools/custom/delete", "cos-pools/custom/delete",
        ("POST",), "cos_custom_delete", "page_cos_custom_delete",
        "Selfie Image delete custom COS",
    ),
    PromptCatalogRouteContract(
        "cos_pool_export", "/api/cos-pools/export", "cos-pools/export", ("GET",),
        "cos_pool_export", "page_cos_pool_export", "Selfie Image export COS pools",
    ),
    PromptCatalogRouteContract(
        "cos_pool_import", "/api/cos-pools/import", "cos-pools/import", ("POST",),
        "cos_pool_import", "page_cos_pool_import", "Selfie Image import COS pools",
    ),
    PromptCatalogRouteContract(
        "creative_template_render", "/api/creative/template/render",
        "creative/template/render", ("POST",), "creative_template_render",
        "page_creative_template_render", "Selfie Image render prompt template",
    ),
    PromptCatalogRouteContract(
        "creative_variations", "/api/creative/variations", "creative/variations",
        ("POST",), "creative_variations", "page_creative_variations",
        "Selfie Image build prompt variations",
    ),
)


# Record routes share validation, filtering and redaction. The adapters retain
# their own authentication, JSON parsing and response envelope.
RECORD_ROUTE_CONTRACTS = (
    RecordRouteContract(
        "records", "/api/records", "records", ("GET",), "records", "page_records",
        "Selfie Image generation records",
    ),
    RecordRouteContract(
        "metrics", "/api/metrics", "metrics", ("GET",), "metrics", "page_metrics",
        "Selfie Image generation metrics",
    ),
    RecordRouteContract(
        "record_detail", "/api/records/<record_id>", "records/<record_id>", ("GET",),
        "record_detail", "page_record_detail", "Selfie Image record detail",
    ),
    RecordRouteContract(
        "record_media_sources", "/api/records/<record_id>/media-sources",
        "records/<record_id>/media-sources", ("GET",), "record_media_sources",
        "page_record_media_sources", "Selfie Image record media sources",
    ),
    RecordRouteContract(
        "record_metadata", "/api/records/<record_id>/metadata",
        "records/<record_id>/metadata", ("POST",), "record_metadata",
        "page_record_metadata", "Selfie Image record metadata",
    ),
    RecordRouteContract(
        "record_asset_metadata", "/api/records/<record_id>/asset",
        "records/<record_id>/asset", ("POST",), "record_metadata",
        "page_record_metadata", "Selfie Image record asset metadata",
    ),
    RecordRouteContract(
        "record_reuse", "/api/records/<record_id>/reuse", "records/<record_id>/reuse",
        ("GET", "POST"), "record_reuse", "page_record_reuse", "Selfie Image reuse record",
    ),
    RecordRouteContract(
        "record_retry", "/api/records/<record_id>/retry", "records/<record_id>/retry",
        ("POST",), "record_retry", "page_record_retry", "Selfie Image retry record generation",
    ),
    RecordRouteContract(
        "records_consistency", "/api/records/consistency", "records/consistency",
        ("GET",), "records_consistency", "page_storage_consistency",
        "Selfie Image storage consistency",
    ),
    RecordRouteContract(
        "records_consistency_repair", "/api/records/consistency/repair",
        "records/consistency/repair", ("POST",), "records_consistency_repair",
        "page_storage_consistency_repair", "Selfie Image repair storage consistency",
    ),
    RecordRouteContract(
        "records_compare", "/api/records/compare", "records/compare", ("POST",),
        "records_compare", "page_records_compare", "Selfie Image compare records",
    ),
    RecordRouteContract(
        "records_clear", "/api/records/clear", "records/clear", ("POST",),
        "records_clear", "page_records_clear", "Selfie Image clear records",
    ),
)

_TASK_CANCEL_CONTRACTS = tuple(
    route for route in TASK_ROUTE_CONTRACTS if route.dashboard_handler == "page_task_cancel"
)
TASK_CANCEL_ROUTE_ALIASES = tuple(
    route.flask_path.replace("<task_id>", "{task_id}") for route in _TASK_CANCEL_CONTRACTS
)
DASHBOARD_TASK_CANCEL_ROUTES = tuple(route.dashboard_path for route in _TASK_CANCEL_CONTRACTS)

# AstrBot has used both the historical bare plugin-page path and the explicit
# ``page/`` path. Both wrappers must register the same handler and semantics.
DASHBOARD_ROUTE_PREFIXES = ("", "page/")
