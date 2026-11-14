#!/usr/bin/env python3
"""Run the repository's release gates in one reproducible command."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def run_gate(label: str, command: list[str]) -> None:
    print(f"[release] {label}: {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def run_python_gates(python: str, *, include_tests: bool) -> None:
    """Run gates that exercise Python code and the offline test suite."""

    if include_tests:
        run_gate("tests", [python, "-m", "pytest", "-q"])
    run_gate(
        "compileall",
        [
            python,
            "-m",
            "compileall",
            "-q",
            "core",
            "features",
            "generation",
            "prompts",
            "cos",
            "nai",
            "studio",
            "tasks",
            "webui",
            "main.py",
        ],
    )
    run_gate("noop check", [python, "scripts/check_noop_implementations.py"])


def run_frontend_gates(python: str, *, require_node: bool = False) -> None:
    """Verify assembled Dashboard artifacts and optional JavaScript syntax."""

    run_gate("dashboard build", [python, "scripts/build_dashboard.py", "--check"])
    run_gate("dashboard fallback", [python, "scripts/check_dashboard_fallback.py"])
    javascript_command = [python, "scripts/check_dashboard_javascript.py"]
    if require_node:
        javascript_command.append("--require-node")
    run_gate("dashboard JavaScript", javascript_command)


def run_release_asset_gates(python: str) -> None:
    run_gate("version, bundled data and artifact", [python, "scripts/check_release.py"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-tests", action="store_true", help="skip pytest for fast local checks")
    parser.add_argument(
        "--layer",
        choices=("all", "python", "frontend", "static"),
        default="all",
        help=(
            "run one release-gate layer; all keeps the historical full check, "
            "static skips pytest while retaining every artifact/data gate"
        ),
    )
    parser.add_argument(
        "--require-node",
        action="store_true",
        help="require Node.js for the frontend layer instead of allowing a skip",
    )
    args = parser.parse_args()

    python = sys.executable
    if args.layer == "all":
        run_python_gates(python, include_tests=not args.skip_tests)
        run_frontend_gates(python, require_node=args.require_node)
        run_release_asset_gates(python)
        run_gate("diff check", ["git", "diff", "--check"])
    elif args.layer == "python":
        run_python_gates(python, include_tests=not args.skip_tests)
    elif args.layer == "frontend":
        run_frontend_gates(python, require_node=args.require_node)
    else:  # static
        run_python_gates(python, include_tests=False)
        run_frontend_gates(python, require_node=args.require_node)
        run_release_asset_gates(python)
        run_gate("diff check", ["git", "diff", "--check"])
    print("[release] all gates passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"[release] failed with exit code {exc.returncode}: {exc.cmd}", file=sys.stderr)
        raise SystemExit(exc.returncode or 1)
    except RuntimeError as exc:
        print(f"[release] {exc}", file=sys.stderr)
        raise SystemExit(1)
