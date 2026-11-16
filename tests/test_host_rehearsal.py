"""Offline contracts for the parameterized AstrBot host rehearsal runner."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from astrbot_plugin_selfie_image.scripts.host_rehearsal import (
    REQUIRED_PHASES,
    RehearsalError,
    run_rehearsal,
    validate_config,
)


ROOT = Path(__file__).resolve().parents[1]


def _server_command(port: int) -> list[str]:
    source = textwrap.dedent(
        """
        import http.server
        import sys

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'ok')

            def log_message(self, *args):
                pass

        http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
        """
    )
    return [sys.executable, "-c", source, str(port)]


def _config(tmp_path: Path, port: int) -> dict:
    success = [sys.executable, "-c", "pass"]
    return {
        "data_root": str(tmp_path / "isolated-data"),
        "report_path": str(tmp_path / "rehearsal.json"),
        "working_directory": str(ROOT),
        "start_command": _server_command(port),
        "stop_command": success,
        "health_url": f"http://127.0.0.1:{port}/health",
        "health_timeout_seconds": 5,
        "health_interval_seconds": 0.05,
        "astrbot_version": "4.13.0",
        "plugin_version": "1.7.55",
        "target_version": "1.7.56",
        "rollback_version": "1.7.55",
        "channel": "offline-test",
        "account": "account-secret-must-not-be-written",
        "proxy": {"mode": "none"},
        "steps": {
            phase: {"command": success, "timeout_seconds": 5}
            for phase in REQUIRED_PHASES
        },
    }


def test_config_requires_isolated_host_contract(tmp_path: Path) -> None:
    with pytest.raises(RehearsalError, match="missing required field 'data_root'"):
        validate_config({"steps": {}}, config_path=tmp_path / "config.json")

    config = _config(tmp_path, 38127)
    del config["steps"]["rollback"]
    with pytest.raises(RehearsalError, match="missing required phase.*rollback"):
        validate_config(config, config_path=tmp_path / "config.json")


def test_full_rehearsal_is_health_checked_and_redacted(tmp_path: Path) -> None:
    config = validate_config(_config(tmp_path, 38128), config_path=tmp_path / "config.json")
    report = run_rehearsal(config)

    assert report["status"] == "passed"
    assert [item["name"] for item in report["phases"]] == ["startup", *REQUIRED_PHASES]
    assert len(report["health"]) == len(REQUIRED_PHASES) + 1
    assert report["data_cleanup"] == "removed"
    assert not Path(report["data_root"]).exists()
    assert "account-secret" not in (config["report_path"]).read_text(encoding="utf-8")
    assert report["metadata"]["account_fingerprint"]
    assert report["shutdown"]["exit_code"] == 0


def test_failed_phase_still_stops_host_and_cleans_data(tmp_path: Path) -> None:
    raw = _config(tmp_path, 38129)
    raw["steps"]["cancel"] = {
        "command": [sys.executable, "-c", "raise SystemExit(7)"],
        "timeout_seconds": 5,
    }
    config = validate_config(raw, config_path=tmp_path / "config.json")
    report = run_rehearsal(config)

    assert report["status"] == "failed"
    assert report["phases"][-1]["name"] == "cancel"
    assert report["phases"][-1]["exit_code"] == 7
    assert report["shutdown"]["exit_code"] == 0
    assert report["data_cleanup"] == "removed"
    assert not Path(report["data_root"]).exists()
