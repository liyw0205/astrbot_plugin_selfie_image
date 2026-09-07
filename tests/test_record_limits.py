from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_record_retention_and_web_page_limits_are_1000():
    store = (ROOT / "generation" / "generation_store.py").read_text(encoding="utf-8")
    database = (ROOT / "generation" / "record_database.py").read_text(encoding="utf-8")
    web = (ROOT / "webui" / "web.py").read_text(encoding="utf-8")
    assert "RECORD_KEEP_LIMIT = 1000" in store
    assert "def load_records(self, limit: int = 1000)" in database
    assert "MAX_RECORD_PAGE_LIMIT = 1000" in web
