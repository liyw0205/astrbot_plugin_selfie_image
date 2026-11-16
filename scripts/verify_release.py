#!/usr/bin/env python3
"""Run the repository's release gates in one reproducible command."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]

# The full suite runs once on the canonical CI interpreter. Other Python
# versions still exercise the release contracts and compatibility-sensitive
# runtime paths without repeating the slowest integration cases.
PYTHON_COMPATIBILITY_TESTS = (
    "tests/test_model_request.py",
    "tests/test_image_limits.py",
    "tests/test_http_sessions.py",
    "tests/test_record_database_materialized.py",
    "tests/test_generation_runtime_service.py",
    "tests/test_generation_task_service.py",
    "tests/test_task_snapshot_budget.py",
    "tests/test_reference_cache_index.py",
)

# These checks validate the release manifest and generated artifact rather
# than Python-version compatibility. They run in the canonical release job.
PYTHON_STATIC_GATE_TESTS = frozenset({"tests/test_release_gates.py"})


def validate_python_compatibility_tests(
    tests: tuple[str, ...] | None = None,
    *,
    root: Path = ROOT,
) -> tuple[str, ...]:
    """Validate the explicit cross-version pytest manifest before execution."""

    manifest = PYTHON_COMPATIBILITY_TESTS if tests is None else tests
    seen: set[str] = set()
    duplicate: list[str] = []
    missing: list[str] = []
    static_gates: list[str] = []
    normalized: list[str] = []
    root = root.resolve()
    for value in manifest:
        relative = Path(str(value))
        name = relative.as_posix()
        if name in seen:
            duplicate.append(name)
        else:
            seen.add(name)
        normalized.append(name)
        if name in PYTHON_STATIC_GATE_TESTS:
            static_gates.append(name)
        candidate = (root / relative).resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not candidate.is_relative_to(root)
            or not candidate.is_file()
        ):
            missing.append(name)

    problems: list[str] = []
    if missing:
        problems.append("missing paths: " + ", ".join(sorted(set(missing))))
    if duplicate:
        problems.append("duplicate paths: " + ", ".join(sorted(set(duplicate))))
    if static_gates:
        problems.append(
            "static release-gate tests are not compatibility tests: "
            + ", ".join(sorted(set(static_gates)))
        )
    if problems:
        raise RuntimeError("invalid Python compatibility test manifest; " + "; ".join(problems))
    return tuple(normalized)


def run_gate(label: str, command: list[str]) -> None:
    print(f"[release] {label}: {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def run_python_gates(
    python: str, *, include_tests: bool, test_scope: str = "full",
    include_static: bool = True,
) -> None:
    """Run gates that exercise Python code and the offline test suite."""

    compatibility_tests = validate_python_compatibility_tests()
    if include_tests:
        command = [python, "-m", "pytest", "-q"]
        if test_scope == "compatibility":
            command.extend(compatibility_tests)
        elif test_scope != "full":
            raise ValueError(f"unsupported Python test scope: {test_scope}")
        run_gate("tests", command)
    if include_static:
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
        run_gate("prompt-pool compatibility", [python, "scripts/check_prompt_pools.py"])


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


def run_package_gates(python: str) -> None:
    """Validate the package inputs without rebuilding a throwaway archive.

    ``build_release.py`` performs the final archive round-trip immediately
    after this gate.  Rebuilding the same temporary ZIP here only duplicated
    work in the CI package job.
    """

    run_gate(
        "version, bundled data and manifest",
        [python, "scripts/check_release.py", "--skip-artifact"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-tests", action="store_true", help="skip pytest for fast local checks")
    parser.add_argument(
        "--layer",
        choices=("all", "python", "frontend", "static", "package"),
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
    parser.add_argument(
        "--test-scope",
        choices=("full", "compatibility"),
        default="full",
        help="pytest scope for the Python layer; compatibility skips repeated slow integration cases",
    )
    parser.add_argument(
        "--skip-static",
        action="store_true",
        help="skip compileall/noop checks when a separate release job already covers them",
    )
    args = parser.parse_args()

    python = sys.executable
    if args.layer == "all":
        run_python_gates(
            python,
            include_tests=not args.skip_tests,
            test_scope=args.test_scope,
            include_static=not args.skip_static,
        )
        run_frontend_gates(python, require_node=args.require_node)
        run_release_asset_gates(python)
        run_gate("diff check", ["git", "diff", "--check", "HEAD"])
    elif args.layer == "python":
        run_python_gates(
            python,
            include_tests=not args.skip_tests,
            test_scope=args.test_scope,
            include_static=not args.skip_static,
        )
    elif args.layer == "frontend":
        run_frontend_gates(python, require_node=args.require_node)
    elif args.layer == "static":
        run_python_gates(python, include_tests=False, include_static=not args.skip_static)
        run_frontend_gates(python, require_node=args.require_node)
        run_release_asset_gates(python)
        run_gate("diff check", ["git", "diff", "--check", "HEAD"])
    else:  # package
        run_package_gates(python)
        run_gate("diff check", ["git", "diff", "--check", "HEAD"])
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
