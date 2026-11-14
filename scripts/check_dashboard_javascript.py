#!/usr/bin/env python3
"""Check Dashboard JavaScript syntax when Node.js is available.

The Dashboard is shipped as a self-contained HTML page.  Its editable
JavaScript lives in ``pages/dashboard/source`` and is assembled into the
generated page, so both representations need a syntax check.

Node.js is an optional local dependency.  A missing executable is reported as
a skip and succeeds; CI installs Node explicitly so this path is exercised in
the release workflow.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "pages" / "dashboard" / "source"
TARGET = ROOT / "pages" / "dashboard" / "index.html"
SCRIPT_PATTERN = re.compile(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", re.IGNORECASE)


def _run_check(node: str, *, path: Path | None = None, source: str | None = None) -> None:
    command = [node, "--check"]
    if path is not None:
        command.append(str(path))
    completed = subprocess.run(
        command,
        cwd=ROOT,
        input=source,
        text=True,
        capture_output=True,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        label = str(path) if path is not None else "generated Dashboard inline script"
        raise RuntimeError(f"JavaScript syntax error in {label}: {detail}")


def check_dashboard_javascript(*, require_node: bool = False) -> int:
    node = shutil.which("node")
    if not node:
        if require_node:
            raise RuntimeError("node is required for the Dashboard JavaScript syntax check")
        print("[dashboard] node not found; JavaScript syntax check skipped")
        return 0

    source_files = sorted(SOURCE_DIR.glob("*.js"))
    for path in source_files:
        _run_check(node, path=path)

    if not TARGET.is_file():
        raise RuntimeError(f"generated Dashboard page is missing: {TARGET}")
    page = TARGET.read_text(encoding="utf-8")
    inline_scripts = [item for item in SCRIPT_PATTERN.findall(page) if item.strip()]
    for source in inline_scripts:
        _run_check(node, source=source)

    print(
        f"[dashboard] JavaScript syntax ok: {len(source_files)} source files, "
        f"{len(inline_scripts)} generated scripts"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-node",
        action="store_true",
        help="fail when Node.js is unavailable instead of recording a skip",
    )
    args = parser.parse_args()
    try:
        return check_dashboard_javascript(require_node=args.require_node)
    except (OSError, UnicodeError, RuntimeError) as exc:
        print(f"[dashboard] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
