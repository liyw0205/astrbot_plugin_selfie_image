#!/usr/bin/env python3
"""Validate bundled JSON, current versions and a temporary runtime release ZIP."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__:
    from .release_checks import (
        ROOT, MAX_ARCHIVE_BYTES, MAX_FILE_BYTES, MAX_UNCOMPRESSED_BYTES,
        ReleaseCheckError, build_artifact, git_files, release_files,
        validate_json_files, validate_versions,
    )
else:
    from release_checks import (
        ROOT, MAX_ARCHIVE_BYTES, MAX_FILE_BYTES, MAX_UNCOMPRESSED_BYTES,
        ReleaseCheckError, build_artifact, git_files, release_files,
        validate_json_files, validate_versions,
    )


def main() -> int:
    try:
        tracked = git_files(ROOT)
        version = validate_versions(ROOT)
        print(f"[release] plugin version: {version}", flush=True)
        checked = validate_json_files(ROOT, tracked=tracked)
        print(f"[release] JSON parse and required fields: {checked} files", flush=True)
        names = release_files(ROOT, tracked=tracked)
        with tempfile.TemporaryDirectory(prefix="selfie-release-check-") as directory:
            report = build_artifact(ROOT, Path(directory) / "release.zip", names)
        report["version"] = version
        report["limits"] = {
            "file_bytes": MAX_FILE_BYTES,
            "uncompressed_bytes": MAX_UNCOMPRESSED_BYTES,
            "archive_bytes": MAX_ARCHIVE_BYTES,
        }
        print(f"[release] artifact checked: {json.dumps(report, ensure_ascii=False, sort_keys=True)}")
        return 0
    except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError, ReleaseCheckError) as exc:
        print(f"[release] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
