#!/usr/bin/env python3
"""Fail when production modules contain an unapproved no-op function."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from astrbot_plugin_selfie_image.core.noop_check import find_unapproved_noop_functions


def main() -> int:
    findings = find_unapproved_noop_functions(ROOT)
    for item in findings:
        print(f"{item.path}:{item.line}: unapproved no-op function {item.function}()")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())