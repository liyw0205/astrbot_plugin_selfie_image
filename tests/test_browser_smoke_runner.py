from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_browser_smokes.js"
EXPECTED_SUITES = [
    "mobile_smoke.js",
    "backup_smoke.js",
    "wardrobe_smoke.js",
    "dashboard_content_smoke.js",
    "identity_pool_browser_smoke.js",
]


def test_browser_smoke_runner_lists_existing_suites():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")

    result = subprocess.run(
        [node, str(RUNNER), "--list"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == EXPECTED_SUITES
    assert all((ROOT / "scripts" / name).is_file() for name in EXPECTED_SUITES)
