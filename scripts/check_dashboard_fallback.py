"""Fail when the generated Flask fallback diverges from the canonical page."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "pages" / "dashboard" / "index.html"
FALLBACK = ROOT / "webui" / "index_fallback.py"


def _load_fallback() -> str:
    spec = importlib.util.spec_from_file_location("selfie_dashboard_fallback", FALLBACK)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法读取回退产物：{FALLBACK}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(getattr(module, "INDEX_HTML", ""))


def main() -> int:
    source = SOURCE.read_text(encoding="utf-8")
    fallback = _load_fallback()
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    fallback_hash = hashlib.sha256(fallback.encode("utf-8")).hexdigest()
    if source_hash != fallback_hash:
        print(f"dashboard fallback out of date: source={source_hash} fallback={fallback_hash}")
        return 1
    print(f"dashboard fallback matches {source_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
