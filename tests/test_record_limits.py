from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_record_retention_and_web_page_limits_are_1000():
    store = (ROOT / "generation" / "generation_store.py").read_text(encoding="utf-8")
    database = (ROOT / "generation" / "record_database.py").read_text(encoding="utf-8")
    web = (ROOT / "webui" / "web.py").read_text(encoding="utf-8")
    assert "RECORD_KEEP_LIMIT = 1000" in store
    assert "def load_records(self, limit: int = 1000)" in database
    assert "MAX_RECORD_PAGE_LIMIT = 1000" in web


def test_asset_query_scan_limit_is_explicit_and_matches_record_retention():
    store = (ROOT / "generation" / "generation_store.py").read_text(encoding="utf-8")
    assert "ASSET_QUERY_SCAN_LIMIT = RECORD_KEEP_LIMIT" in store


def test_asset_query_does_not_scan_beyond_retained_record_window():
    from astrbot_plugin_selfie_image.generation.generation_store import GenerationStoreMixin

    rows = [
        {
            "id": f"asset-{index}",
            "time": f"2026-09-01 00:{index % 60:02d}:00",
            "source": "group",
            "used_model": "model-a",
            "media_type": "image",
            "success": True,
            "tags": ["portrait"],
            "original_prompt": "portrait",
        }
        for index in range(1200)
    ]
    store = object.__new__(GenerationStoreMixin)
    store.get_recent_records = lambda summary=False: rows

    page, meta = store.query_asset_records(limit=2000)
    assert len(page) == 1000
    assert meta["total"] == 1000
    assert meta["filtered"] == 1000


def test_web_entrypoints_share_asset_redaction_and_compatibility_status_contract():
    flask = (ROOT / "webui" / "web.py").read_text(encoding="utf-8")
    dashboard = (ROOT / "webui" / "dashboard_api.py").read_text(encoding="utf-8")
    assert flask.count("return ok(redact_sensitive_data(data), count=len(data)") == 2
    assert dashboard.count("return self._ok(redact_sensitive_data(data), count=len(data)") == 2
    assert 'return self._fail("当前版本不支持资产导出", 501)' in dashboard
    assert 'return fail("当前版本不支持资产导出", 501)' in flask
    health_block = dashboard.split("async def page_health", 1)[1].split("async def page_proxies_list", 1)[0]
    assert '"source": "dashboard"' not in health_block
