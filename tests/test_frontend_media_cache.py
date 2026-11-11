from pathlib import Path


PAGE = (Path(__file__).resolve().parents[1] / "pages" / "dashboard" / "index.html").read_text(encoding="utf-8")


def test_protected_media_uses_shared_cache_and_request_deduplication():
    assert "const PROTECTED_MEDIA_CACHE = new Map();" in PAGE
    assert "const PROTECTED_MEDIA_REQUESTS = new Map();" in PAGE
    assert "async function getProtectedMedia(path)" in PAGE
    assert PAGE.count("fetch(cacheImagePreviewUrl(key)") == 1
    assert "preview.available === false" in PAGE
    assert "const pending = PROTECTED_MEDIA_REQUESTS.get(key);" in PAGE
    assert "return rememberProtectedMedia(key, entry);" in PAGE
    assert "function cacheImageThumbnailUrl(path)" in PAGE
    assert "async function getProtectedMediaVariant(path, variant = 'full')" in PAGE
    assert "cacheImageThumbnailUrl(mediaPath)" in PAGE


def test_media_consumers_reuse_cached_response_for_detail_and_download():
    assert "const media = await getProtectedMedia(path);" in PAGE
    assert "const media = await getProtectedMedia(rel);" in PAGE
    assert "if (triggerProtectedDownload(media, name))" in PAGE
    assert "bridge.download('cache-image', { path: rel }, name)" in PAGE
    assert "async function downloadStandaloneCacheFile(path, name)" in PAGE
    download_start = PAGE.index("async function downloadCachePath")
    download_body = PAGE[download_start:PAGE.index("    function copyIconSvg", download_start)]
    assert download_body.index("await getProtectedMedia(rel)") < download_body.index("bridge.download('cache-image'")


def test_media_cache_has_auth_and_deletion_invalidation_paths():
    assert "clearProtectedMediaCache();" in PAGE
    assert "ensureProtectedMediaScope();" in PAGE
    assert "images.forEach(invalidateProtectedMediaPath);" in PAGE


def test_gallery_picker_uses_one_long_lived_shared_cache_for_both_canvases():
    assert "const GALLERY_PICKER_CACHE_MAX_ENTRIES = 100;" in PAGE
    assert "const GALLERY_PICKER_CACHE = {items: [], loaded: false, request: null, version: 0};" in PAGE
    assert "async function loadGalleryPickerItems()" in PAGE
    assert PAGE.count("studioApi('/api/studio/gallery?limit=' + GALLERY_PICKER_CACHE_MAX_ENTRIES)") == 1
    assert "loadCreativeCanvasGalleryItems" not in PAGE
    assert "CREATIVE_CANVAS_GALLERY_CACHE_TTL_MS" not in PAGE


def test_gallery_and_catalog_pickers_use_server_pages_and_support_search():
    assert "const PICKER_PAGE_SIZE = 30;" in PAGE
    assert "function renderGalleryPage(panel, items, onPick)" in PAGE
    assert "data-gallery-more" in PAGE
    assert "data-prompt-search" in PAGE
    assert "data-cos-search" in PAGE
    assert "async function fetchCatalogPage(kind, query, page, force = false)" in PAGE
    assert "limit: String(PICKER_PAGE_SIZE), offset: String(offset), search: normalizedQuery" in PAGE
    assert "data-picker-page=\"1\"" in PAGE
    assert "id=\"presetManagerPager\"" in PAGE
    assert "id=\"presetCosManagerPager\"" in PAGE
    assert 'id="assetStudioPresetSearch"' in PAGE
    assert "await fetchCatalogPage('image', search, ASSET_STUDIO_PRESET_PAGE)" in PAGE
    assert "api('/api/prompt-presets?kind=" not in PAGE
    assert "api('/api/cos-pools')" not in PAGE


def test_canvas_state_has_request_trace_viewport_fit_and_mobile_node_drawer():
    assert "const REQUEST_TRACE = [];" in PAGE
    assert "function requestTraceSnapshot()" in PAGE
    assert "function studioFitViewport(force = false)" in PAGE
    assert "selfieStudioSelected:" in PAGE
    assert "const searchable = `${studioNodeTitle(node)}" in PAGE
    assert "mobile-node-list-open" in PAGE
    assert "studioStopPoll();" in PAGE


def test_desktop_node_panel_has_an_independent_vertical_scroll_region():
    assert ".studio-graph-sidebar, .studio-node-panel { min-width:0; min-height:0;" in PAGE
    assert ".studio-node-panel-body { flex:1 1 auto; min-height:0; overflow-y:auto;" in PAGE
    assert "overscroll-behavior:contain;" in PAGE


def test_preset_manager_supports_fuzzy_name_and_prompt_search_for_all_kinds():
    assert 'id="presetManagerSearch"' in PAGE
    assert 'id="presetCosManagerSearch"' in PAGE
    assert "const PRESET_MANAGER_QUERIES = {image: '', cos: '', nai: ''};" in PAGE
    assert "search:query" in PAGE
    assert "item.name || item.title || ''}\\n${item.prompt || ''}" in PAGE
    assert "item.title || item.id || ''}\\n${item.prompt || ''}" in PAGE
    assert "renderManagedPromptPresets();" in PAGE
    assert "renderManagedCosPool();" in PAGE


def test_creative_canvas_hides_rerun_until_a_successful_result():
    assert "const hasSuccess = String(last.status || '').toLowerCase() === 'succeeded'" in PAGE
    assert "$('creativeCanvasRerunBtn').hidden = !hasSuccess;" in PAGE
    assert "保存后生成" in PAGE


def test_asset_actions_are_delegated_and_media_loading_is_prioritized():
    assert "function setupAssetGridInteractions()" in PAGE
    assert 'data-asset-action="detail"' in PAGE
    assert 'data-asset-action="studio"' in PAGE
    assert "const PROTECTED_MEDIA_LOAD_CONCURRENCY = 2;" in PAGE
    assert "protectedMediaViewportPriority" in PAGE
    assert "正在加载记录详情" in PAGE
    assert 'data-cache-variant="thumbnail"' in PAGE
    assert "加载缩略图" in PAGE


def test_record_and_asset_pagers_show_totals_and_ignore_stale_requests():
    assert "第 ${ASSET_PAGE}/${totalPages} 页" in PAGE
    assert "第 ${MONITOR_PAGE}/${totalPages} 页" in PAGE
    assert "let ASSET_LOAD_SEQ = 0;" in PAGE
    assert "let RECORD_LOAD_SEQ = 0;" in PAGE
    assert "if (requestSeq !== ASSET_LOAD_SEQ) return;" in PAGE
    assert "if (requestSeq !== RECORD_LOAD_SEQ) return;" in PAGE
    assert "function setPagerLoading(id, loading)" in PAGE


def test_record_and_asset_pages_cache_metadata_and_prefetch_neighbors():
    assert "const RECORD_PAGE_CACHE = new Map();" in PAGE
    assert "const ASSET_PAGE_CACHE = new Map();" in PAGE
    assert "const PAGE_CACHE_TTL_MS = 30000;" in PAGE
    assert "function usablePageCache(cache, key)" in PAGE
    assert "function requestCachedPage(path, requests, force = false)" in PAGE
    assert "function prefetchPages(path, page, totalPages" in PAGE
    assert "function observeProtectedMedia(element, path)" in PAGE
    assert "new IntersectionObserver" in PAGE
    assert "prefetchPages(requestPath, MONITOR_PAGE" in PAGE
    assert "prefetchPages(requestPath, ASSET_PAGE" in PAGE
    assert "function clearRecordPageCache()" in PAGE
    assert "function clearAssetPageCache()" in PAGE
    assert "let RECORD_PAGE_CACHE_VERSION = 0;" in PAGE
    assert "let ASSET_PAGE_CACHE_VERSION = 0;" in PAGE
    assert "if (showRefreshToast) RECORD_PAGE_CACHE.delete(requestPath);" in PAGE
    assert "if (showRefreshToast) ASSET_PAGE_CACHE.delete(requestPath);" in PAGE
    assert "pruneProtectedMediaLoadQueue();" in PAGE


def test_catalogs_prefetch_three_forward_pages_without_loading_media():
    assert "const PAGE_PREFETCH_AHEAD = 3;" in PAGE
    assert "function prefetchCatalogPages(kind, query, page, total)" in PAGE
    assert "function prefetchManagedCatalogPages(kind, query, page, total)" in PAGE
    assert "Array.from({length: PAGE_PREFETCH_AHEAD}, (_, index) => page + index + 1)" in PAGE
    prefetch = PAGE[PAGE.index("function prefetchCatalogPages"):PAGE.index("function fetchManagedCatalogPage")]
    assert "loadProtectedImages" not in prefetch


def test_protected_media_has_three_day_sliding_persistent_cache_and_retry_ui():
    assert "PROTECTED_MEDIA_PERSIST_CACHE_NAME" in PAGE
    assert "PROTECTED_MEDIA_PERSIST_TTL_MS = 3 * 24 * 60 * 60 * 1000" in PAGE
    assert "async function readPersistentProtectedMedia(key, scope)" in PAGE
    assert "touchPersistentProtectedMedia(key, scope)" in PAGE
    assert "async function deletePersistentProtectedMedia(key)" in PAGE
    assert "data-media-retry" in PAGE
    assert "function bindProtectedMediaRetry(root = document)" in PAGE


def test_record_source_buttons_copy_and_response_base64_opens():
    assert "copyMediaSourceValue" in PAGE
    assert 'title="复制原始来源"' in PAGE
    assert 'title="打开原始 Base64"' in PAGE
    assert "Base64（点击打开）" in PAGE


def test_record_persona_reference_is_loaded_after_markup_without_inline_html_data():
    assert "function loadPersonaReferenceThumbs(root = document)" in PAGE
    assert "SELFIE_REFERENCE_DATA_URL = data.has_image" in PAGE
    assert "img.src = src;" in PAGE
    assert "loadPersonaReferenceThumbs($('recordDetailBody'))" in PAGE
    assert 'src="${escapeHtml(src)}" alt="形象图"' not in PAGE


def test_selfie_controls_only_offer_local_daily_outfit_refresh():
    controls = PAGE[PAGE.index('<input id="selfieFile"'):PAGE.index('<div id="selfieStatus"', PAGE.index('<input id="selfieFile"'))]
    assert controls.index('onclick="refreshSelfie(true)"') < controls.index('onclick="clearSelfie()"')
    assert '今日穿搭' not in controls
    assert 'onclick="refreshDailySelfieLocal()"' in controls
    assert 'class="actions selfie-reference-actions"' in controls
    assert 'onclick="refreshDailySelfieLlm()"' not in PAGE
    assert "async function refreshDailySelfieLlm()" not in PAGE
    assert "async function refreshDailySelfieLocal()" in PAGE
    assert "'/api/selfie-profile/refresh-local'" in PAGE
    assert '今日穿搭与日程' in PAGE


def test_dashboard_health_and_metrics_are_grouped_for_scanning():
    assert 'class="health-header"' in PAGE
    assert 'id="health" class="health-grid"' in PAGE
    assert 'class="health-item-label"' in PAGE
    assert 'id="monitorMetricButtons" class="metrics-launcher-grid"' in PAGE
    assert 'data-metrics-category="${category.key}"' in PAGE
    assert 'id="metricsModal" class="modal-mask"' in PAGE
    assert "function openMetricsCategory(key)" in PAGE
    assert 'class="metrics-kpi-grid"' in PAGE
    assert 'class="metrics-split"' in PAGE
    assert '错误类型' in PAGE
    assert '@media (max-width: 640px)' in PAGE
    assert 'id="monitorMetrics"' not in PAGE


def test_daily_schedule_details_use_labeled_wrapping_fields():
    assert 'function renderSelfieDailySchedule(payload)' in PAGE
    assert "renderField('场景', event.scene)" in PAGE
    assert "renderField('动作', event.action)" in PAGE
    assert "renderField('穿搭', outfit)" in PAGE
    assert 'class="daily-event-fields"' in PAGE
    assert 'class="daily-current-event"' in PAGE
    assert '<details class="daily-schedule-details">' in PAGE


def test_dashboard_action_feedback_uses_toasts_without_empty_bottom_status_bar():
    assert 'id="baseStatus"' not in PAGE
    assert '.status:empty { display: none; }' in PAGE
    assert "function setMultiStatus(text, type = 'info')" in PAGE
    assert 'if (text) showToast(text, type);' in PAGE
    assert 'white-space: normal; overflow-wrap: anywhere; font-size: 12px;' in PAGE


def test_task_queue_defers_request_image_loading_until_detail_modal():
    queue_start = PAGE.index("$('taskQueueTable').innerHTML")
    queue_end = PAGE.index("        $('taskQueueTable').querySelectorAll", queue_start)
    queue_render = PAGE[queue_start:queue_end]
    assert '<th>请求图</th>' not in queue_render
    assert 'taskRequestImageCell' not in queue_render
    assert "loadProtectedImages($('taskQueueTable'))" not in queue_render
    detail_start = PAGE.index('async function openTaskDetail')
    detail_end = PAGE.index('    async function loadTasksNow', detail_start)
    detail = PAGE[detail_start:detail_end]
    assert "const requestImagePaths = Array.isArray(task.request_image_paths)" in detail
    assert "imageThumbs(requestImagePaths, '请求图')" in detail
    assert "function taskUsesPersonaReference(task)" in PAGE
    assert 'personaReferenceThumb()' in detail
    assert 'loadProtectedImages(body);' in detail
    assert 'loadPersonaReferenceThumbs(body);' in detail


def test_dashboard_shortcuts_and_priority_pin_cover_all_model_kinds():
    for target in ("test", "studio", "selfie", "channels", "monitor"):
        assert f'data-nav-target="{target}"' in PAGE
    assert "function navigateToTab" in PAGE
    assert "main.app-shell > section" in PAGE
    assert "function movePriorityToTop" in PAGE
    assert "movePriorityToTop('${kind}', ${i})" in PAGE
    assert "for (const kind of ['image','audit'])" in PAGE
    assert "priorityList').value = (CONFIG.enabled_image_model_priority || []).join('\\n')" in PAGE
    assert "auditPriorityList').value = (CONFIG.enabled_audit_model_priority || []).join('\\n')" in PAGE


def test_model_priority_refresh_uses_one_snapshot_and_keeps_selection():
    refresh_start = PAGE.index("function refreshModelSelectors()")
    refresh_end = PAGE.index("function refreshTestModels()", refresh_start)
    refresh = PAGE[refresh_start:refresh_end]
    assert refresh.count("collectChannels()") == 1
    assert "prunePriorityList('image', labels)" in refresh
    assert "prunePriorityList('audit', audits)" in refresh
    assert "values.includes(previous) ? previous : fallback" in refresh
    provider_start = PAGE.index("function setModelProviderType(")
    provider_end = PAGE.index("function addEnabledModel(", provider_start)
    assert "refreshModelSelectors()" not in PAGE[provider_start:provider_end]


def test_priority_autosave_does_not_rebuild_channel_cards_on_success():
    save_start = PAGE.index("async function persistChannelListSnapshot(")
    save_end = PAGE.index("async function scheduleChannelListAutoSave(", save_start)
    save_body = PAGE[save_start:save_end]
    success_body = save_body.split("} catch (e)", 1)[0]
    assert "renderAllChannelLists()" not in success_body
    assert "refreshModelSelectors();" in success_body
