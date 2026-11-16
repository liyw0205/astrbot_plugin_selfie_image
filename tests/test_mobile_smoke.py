"""Contract checks for the optional Chromium viewport smoke test."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mobile_smoke.js"


def test_mobile_smoke_script_covers_declared_viewports_and_pages() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "{name: 'small', width: 320, height: 568}" in source
    assert "{name: 'large', width: 390, height: 844}" in source
    assert "status-preview" not in source
    assert "dashboard unauthenticated" in source
    assert "does not certify a real device" in source
    assert "scrollWidth > metrics.clientWidth + 1" in source
    assert "Page.captureScreenshot" in source


@pytest.mark.skipif(
    os.environ.get("RUN_MOBILE_SMOKE") != "1" or shutil.which("node") is None,
    reason="set RUN_MOBILE_SMOKE=1 with Node.js to run the Chromium smoke test",
)
def test_mobile_smoke_browser() -> None:
    result = subprocess.run(
        ["node", str(SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, f"mobile smoke failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
