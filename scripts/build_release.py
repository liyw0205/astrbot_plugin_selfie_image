#!/usr/bin/env python3
"""Run release gates before writing a reviewed runtime ZIP to --output."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__:
    from .release_checks import (
        ROOT, ReleaseCheckError, build_artifact, release_files,
        validate_json_files, validate_versions,
    )
else:
    from release_checks import (
        ROOT, ReleaseCheckError, build_artifact, release_files,
        validate_json_files, validate_versions,
    )


def build_verified_release(
    output: Path, *, root: Path = ROOT, skip_tests: bool = False,
) -> dict:
    output = output.resolve()
    if output.exists():
        raise ReleaseCheckError(f"refusing to overwrite an existing release artifact: {output}")
    command = [sys.executable, "scripts/verify_release.py"]
    if skip_tests:
        # CI has already run the Python and Dashboard jobs.  Package mode
        # keeps only source/manifest checks here; build_artifact below is the
        # single final ZIP round-trip for this job.
        command.extend(["--layer", "package"])
    else:
        command.append("--require-node")
    subprocess.run(command, cwd=root, check=True)
    version = validate_versions(root)
    validate_json_files(root)
    names = release_files(root)
    with tempfile.TemporaryDirectory(prefix="selfie-release-build-") as directory:
        temporary = Path(directory) / "release.zip"
        report = build_artifact(root, temporary, names)
        output.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("rb") as source, output.open("xb") as target:
            shutil.copyfileobj(source, target)
    report["version"] = version
    report["output"] = str(output)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="destination ZIP; existing files are never overwritten")
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="skip pytest when an earlier CI job already passed the Python test layer",
    )
    args = parser.parse_args()
    try:
        report = build_verified_release(args.output, skip_tests=args.skip_tests)
    except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError, ReleaseCheckError) as exc:
        print(f"[release] {exc}", file=sys.stderr)
        return 1
    print(f"[release] artifact built: {json.dumps(report, ensure_ascii=False, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
