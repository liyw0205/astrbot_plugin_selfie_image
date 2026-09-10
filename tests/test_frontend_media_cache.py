from pathlib import Path


PAGE = (Path(__file__).resolve().parents[1] / "pages" / "dashboard" / "index.html").read_text(encoding="utf-8")


def test_protected_media_uses_shared_cache_and_request_deduplication():
    assert "const PROTECTED_MEDIA_CACHE = new Map();" in PAGE
    assert "const PROTECTED_MEDIA_REQUESTS = new Map();" in PAGE
    assert "async function getProtectedMedia(path)" in PAGE
    assert PAGE.count("fetch(cacheImageUrl(key)") == 1
    assert "const pending = PROTECTED_MEDIA_REQUESTS.get(key);" in PAGE
    assert "return rememberProtectedMedia(key, entry);" in PAGE


def test_media_consumers_reuse_cached_response_for_detail_and_download():
    assert "const media = await getProtectedMedia(path);" in PAGE
    assert "const media = await getProtectedMedia(rel);" in PAGE
    assert "if (triggerProtectedDownload(media, name))" in PAGE
    assert "bridge.download('cache-image', { path: rel }, name)" in PAGE
    download_start = PAGE.index("async function downloadCachePath")
    download_body = PAGE[download_start:PAGE.index("    function copyIconSvg", download_start)]
    assert download_body.index("await getProtectedMedia(rel)") < download_body.index("bridge.download('cache-image'")


def test_media_cache_has_auth_and_deletion_invalidation_paths():
    assert "clearProtectedMediaCache();" in PAGE
    assert "ensureProtectedMediaScope();" in PAGE
    assert "images.concat(videos).forEach(invalidateProtectedMediaPath);" in PAGE


def test_asset_actions_are_delegated_and_media_loading_is_prioritized():
    assert "function setupAssetGridInteractions()" in PAGE
    assert 'data-asset-action="detail"' in PAGE
    assert 'data-asset-action="studio"' in PAGE
    assert "const PROTECTED_MEDIA_LOAD_CONCURRENCY = 2;" in PAGE
    assert "protectedMediaViewportPriority" in PAGE
    assert "正在加载记录详情" in PAGE


def test_record_source_buttons_copy_and_response_base64_opens():
    assert "copyMediaSourceValue" in PAGE
    assert 'title="复制原始来源"' in PAGE
    assert 'title="打开原始 Base64"' in PAGE
    assert "Base64（点击打开）" in PAGE


def test_dashboard_shortcuts_and_priority_pin_cover_all_model_kinds():
    for target in ("test", "studio", "selfie", "channels", "monitor"):
        assert f'data-nav-target="{target}"' in PAGE
    assert "function navigateToTab" in PAGE
    assert "main.app-shell > section" in PAGE
    assert "function movePriorityToTop" in PAGE
    assert "movePriorityToTop('${kind}', ${i})" in PAGE
    assert "for (const kind of ['image','audit','video'])" in PAGE
    assert "priorityList').value = (CONFIG.enabled_image_model_priority || []).join('\\n')" in PAGE
    assert "auditPriorityList').value = (CONFIG.enabled_audit_model_priority || []).join('\\n')" in PAGE
    assert "videoPriorityList').value = (CONFIG.enabled_video_model_priority || []).join('\\n')" in PAGE
