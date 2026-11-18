"""Offline contracts for the parameterized AstrBot host rehearsal runner."""

from __future__ import annotations

import json
import socket
import sys
import textwrap
from pathlib import Path

import pytest

from astrbot_plugin_selfie_image.scripts import host_rehearsal
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
        from pathlib import Path

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'ok')

            def log_message(self, *args):
                pass

        if not Path(sys.argv[2]).is_dir():
            raise SystemExit(4)
        http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
        """
    )
    return [sys.executable, "-c", source, str(port), "{data_root}"]


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


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


@pytest.mark.parametrize("start_command", [
    [sys.executable, "-c", "pass"],
    [sys.executable, "-c", "pass", "{{data_root}}"],
    [sys.executable, "-c", "pass", "{data_root!r}"],
])
def test_start_command_must_pass_bare_data_root(tmp_path: Path, start_command: list[str]) -> None:
    config = _config(tmp_path, 38127)
    config["start_command"] = start_command

    with pytest.raises(RehearsalError, match=r"start_command: must pass the bare \{data_root\}"):
        validate_config(config, config_path=tmp_path / "config.json")


def test_validate_only_does_not_create_data_or_run_host(tmp_path: Path, monkeypatch, capsys) -> None:
    raw = _config(tmp_path, 38130)
    config_path = tmp_path / "host-rehearsal.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(
        host_rehearsal, "run_rehearsal",
        lambda *_args, **_kwargs: pytest.fail("validate-only must not start the rehearsal"),
    )

    assert host_rehearsal.main(["--config", str(config_path), "--validate-only"]) == 0

    output = capsys.readouterr().out
    assert "configuration valid" in output
    assert "no directories or processes created" in output
    assert not Path(raw["data_root"]).exists()
    assert not Path(raw["report_path"]).exists()


def test_invalid_config_does_not_create_data_root_parents(tmp_path: Path) -> None:
    raw = _config(tmp_path, 38127)
    parent = tmp_path / "created-during-validation"
    raw["data_root"] = str(parent / "isolated-data")
    del raw["steps"]["rollback"]

    with pytest.raises(RehearsalError, match="missing required phase.*rollback"):
        validate_config(raw, config_path=tmp_path / "config.json")

    assert not parent.exists()


def test_rehearsal_round_trips_unicode_deep_data_root(tmp_path: Path) -> None:
    # This stays below the per-component limit while exercising a path longer
    # than the legacy Windows MAX_PATH boundary and preserving UTF-8 names.
    data_root = tmp_path
    for index in range(18):
        data_root /= f"中文数据目录-{index:02d}-" + ("x" * 12)
    data_root /= "隔离宿主数据-" + ("y" * 48)
    raw = _config(tmp_path, _free_port())
    raw["data_root"] = str(data_root)
    config = validate_config(raw, config_path=tmp_path / "config.json")

    report = run_rehearsal(config)

    assert report["status"] == "passed"
    assert report["data_cleanup"] == "removed"
    assert report["data_cleanup_attempts"] == 1
    assert not data_root.exists()


def test_prepare_rejects_unwritable_data_root_before_starting_host(tmp_path: Path, monkeypatch) -> None:
    raw = _config(tmp_path, _free_port())
    config = validate_config(raw, config_path=tmp_path / "config.json")
    launched: list[bool] = []
    monkeypatch.setattr(
        host_rehearsal.subprocess,
        "Popen",
        lambda *_args, **_kwargs: launched.append(True),
    )

    def reject_probe(*_args, **_kwargs):
        raise PermissionError("simulated read-only data root")

    monkeypatch.setattr(host_rehearsal.tempfile, "mkstemp", reject_probe)
    with pytest.raises(RehearsalError, match="data_root: directory is not writable"):
        run_rehearsal(config)

    assert launched == []
    assert not Path(raw["data_root"]).exists()


def test_prepare_rechecks_root_after_write_probe(tmp_path: Path, monkeypatch) -> None:
    raw = _config(tmp_path, _free_port())
    config = validate_config(raw, config_path=tmp_path / "config.json")
    launched: list[bool] = []
    monkeypatch.setattr(
        host_rehearsal.subprocess,
        "Popen",
        lambda *_args, **_kwargs: launched.append(True),
    )

    def replace_after_probe(path: Path) -> None:
        original = path.with_name("runner-created-original")
        path.rename(original)
        path.mkdir()

    monkeypatch.setattr(host_rehearsal, "_probe_data_root_writable", replace_after_probe)
    with pytest.raises(RehearsalError, match="data_root: changed after validation"):
        run_rehearsal(config)

    assert launched == []
    assert Path(raw["data_root"]).is_dir()
    assert (Path(raw["data_root"]).parent / "runner-created-original").is_dir()


def test_cleanup_retries_transient_windows_file_lock(tmp_path: Path, monkeypatch) -> None:
    config = validate_config(_config(tmp_path, _free_port()), config_path=tmp_path / "config.json")
    remove = host_rehearsal.shutil.rmtree
    attempts = 0

    def locked_once(path, *args, **kwargs):
        nonlocal attempts
        if Path(path) == Path(config["data_root"]) and attempts < 2:
            attempts += 1
            raise PermissionError("simulated Windows sharing violation")
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(host_rehearsal.shutil, "rmtree", locked_once)
    report = run_rehearsal(config)

    assert report["status"] == "passed"
    assert report["data_cleanup"] == "removed"
    assert report["data_cleanup_attempts"] == 3
    assert report["data_cleanup_error"] == "PermissionError"
    assert not Path(config["data_root"]).exists()


def test_cleanup_preserves_data_on_permanent_windows_file_lock(tmp_path: Path, monkeypatch) -> None:
    config = validate_config(_config(tmp_path, _free_port()), config_path=tmp_path / "config.json")
    remove = host_rehearsal.shutil.rmtree

    def permanently_locked(path, *args, **kwargs):
        if Path(path) == Path(config["data_root"]):
            raise PermissionError("simulated Windows sharing violation")
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(host_rehearsal.shutil, "rmtree", permanently_locked)
    report = run_rehearsal(config)

    assert report["status"] == "failed"
    assert report["data_cleanup"] == "failed"
    assert report["data_cleanup_attempts"] == host_rehearsal.DATA_CLEANUP_RETRIES
    assert report["data_cleanup_error"] == "PermissionError"
    assert Path(config["data_root"]).is_dir()
    remove(config["data_root"])


@pytest.mark.parametrize("preexisting", [False, True])
def test_rehearsal_rechecks_data_root_before_starting_host(
    tmp_path: Path, monkeypatch, preexisting: bool,
) -> None:
    raw = _config(tmp_path, 38128)
    data_root = Path(raw["data_root"])
    if preexisting:
        data_root.mkdir()
    config = validate_config(raw, config_path=tmp_path / "config.json")
    data_root.mkdir(exist_ok=preexisting)
    sentinel = data_root / "keep-me.txt"
    sentinel.write_text("created after preflight", encoding="utf-8")

    launched: list[bool] = []

    class DummyProcess:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    def popen(*_args, **_kwargs):
        launched.append(True)
        return DummyProcess()

    monkeypatch.setattr(host_rehearsal.subprocess, "Popen", popen)
    monkeypatch.setattr(host_rehearsal, "_run_command", lambda *_args, **_kwargs: {
        "exit_code": 0, "timed_out": False, "duration_ms": 0, "error_category": "",
    })
    monkeypatch.setattr(host_rehearsal, "_wait_for_health", lambda *_args, **_kwargs: {
        "status": "passed", "http_status": 200, "duration_ms": 0, "error_category": "",
    })

    with pytest.raises(RehearsalError, match="changed after validation"):
        run_rehearsal(config)

    assert launched == []
    assert sentinel.read_text(encoding="utf-8") == "created after preflight"


def test_cleanup_preserves_replaced_data_root(tmp_path: Path, monkeypatch) -> None:
    config = validate_config(_config(tmp_path, 38129), config_path=tmp_path / "config.json")

    class DummyProcess:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    monkeypatch.setattr(host_rehearsal.subprocess, "Popen", lambda *_args, **_kwargs: DummyProcess())
    monkeypatch.setattr(host_rehearsal, "_run_command", lambda *_args, **_kwargs: {
        "exit_code": 0, "timed_out": False, "duration_ms": 0, "error_category": "",
    })
    changed = False

    def replace_data_root(*_args, **_kwargs):
        nonlocal changed
        if not changed:
            root = Path(config["data_root"])
            root.rename(root.with_name("runner-created-original"))
            root.mkdir()
            (root / "operator-data.txt").write_text("preserve", encoding="utf-8")
            changed = True
        return {"status": "passed", "http_status": 200, "duration_ms": 0, "error_category": ""}

    monkeypatch.setattr(host_rehearsal, "_wait_for_health", replace_data_root)
    report = run_rehearsal(config)

    assert report["status"] == "failed"
    assert report["data_cleanup"] == "preserved_changed"
    assert (Path(config["data_root"]) / "operator-data.txt").read_text(encoding="utf-8") == "preserve"


def test_full_rehearsal_is_health_checked_and_redacted(tmp_path: Path) -> None:
    config = validate_config(_config(tmp_path, _free_port()), config_path=tmp_path / "config.json")
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
    raw = _config(tmp_path, _free_port())
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
