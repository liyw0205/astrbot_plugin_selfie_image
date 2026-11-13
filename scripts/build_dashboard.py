"""Assemble editable Dashboard sources into the host's self-contained page."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "pages" / "dashboard" / "index.source.html"
TARGET = ROOT / "pages" / "dashboard" / "index.html"
INCLUDE_PATTERN = re.compile(
    r"^<!-- dashboard-source: ([a-z0-9_-]+\.(?:js|css)) -->\n",
    re.MULTILINE,
)


def render_dashboard_page(template: Path = TEMPLATE) -> str:
    template = Path(template)
    directory = (template.parent / "source").resolve()
    included: set[str] = set()

    def include(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in included:
            raise ValueError(f"Duplicate Dashboard source: {name}")
        included.add(name)
        path = (directory / name).resolve()
        if not path.is_relative_to(directory):
            raise ValueError(f"Dashboard source escapes source directory: {name}")
        content = path.read_text(encoding="utf-8")
        if content and not content.endswith("\n"):
            raise ValueError(f"Dashboard source must end with a newline: {name}")
        return content

    rendered = INCLUDE_PATTERN.sub(include, template.read_text(encoding="utf-8"))
    if "<!-- dashboard-source:" in rendered:
        raise ValueError("Invalid or unresolved Dashboard source directive")
    return rendered


def build_dashboard_page(*, check: bool = False) -> bool:
    rendered = render_dashboard_page()
    if check:
        return TARGET.is_file() and TARGET.read_text(encoding="utf-8") == rendered
    TARGET.write_text(rendered, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check without writing the generated page")
    args = parser.parse_args()
    try:
        if not build_dashboard_page(check=args.check):
            print("Dashboard index.html is out of date; run scripts/build_dashboard.py", file=sys.stderr)
            return 1
    except (OSError, ValueError) as exc:
        print(f"Dashboard build failed: {exc}", file=sys.stderr)
        return 1
    print("Dashboard source assembly matches" if args.check else "Dashboard page assembled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
