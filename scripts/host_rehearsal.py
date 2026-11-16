#!/usr/bin/env python3
"""Run a parameterized, isolated AstrBot host rehearsal.

The runner deliberately records phase outcomes and timings only.  It never
persists command output, media, credentials, or full upstream responses.
Commands are argv arrays (not shell strings) and may use the documented
``{data_root}``, ``{report_path}``, ``{phase}``, ``{target_version}``,
``{rollback_version}``, and ``{channel}`` placeholders.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from string import Formatter
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
REQUIRED_PHASES = ("upgrade", "generate", "failure", "cancel", "restart", "rollback")
ALLOWED_PLACEHOLDERS = frozenset(
    {"data_root", "report_path", "phase", "target_version", "rollback_version", "channel"}
)
MAX_OUTPUT_SAMPLE_BYTES = 8192
DEFAULT_COMMAND_TIMEOUT = 300.0
DEFAULT_HEALTH_TIMEOUT = 90.0
DEFAULT_HEALTH_INTERVAL = 1.0


class RehearsalError(ValueError):
    """The rehearsal configuration or execution is unsafe or incomplete."""


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RehearsalError(f"{label}: expected a non-empty string")
    return value.strip()


def _command(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise RehearsalError(f"{label}: expected a non-empty argv array")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise RehearsalError(f"{label}[{index}]: expected a non-empty string")
        result.append(item)
    return result


def _bounded_number(value: Any, label: str, *, default: float, maximum: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RehearsalError(f"{label}: expected a number")
    number = float(value)
    if number <= 0 or number > maximum:
        raise RehearsalError(f"{label}: expected a value in (0, {maximum}]")
    return number


def _validate_placeholders(command: Sequence[str], label: str) -> None:
    formatter = Formatter()
    for item in command:
        try:
            fields = [field for _, field, _, _ in formatter.parse(item) if field]
        except ValueError as error:
            raise RehearsalError(f"{label}: malformed command placeholder") from error
        for field in fields:
            name = field.split(".", 1)[0].split("[", 1)[0]
            if name not in ALLOWED_PLACEHOLDERS:
                raise RehearsalError(f"{label}: unsupported placeholder {{{field}}}")


def _safe_url(value: Any, label: str) -> str:
    url = _text(value, label)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RehearsalError(f"{label}: expected an http(s) URL")
    if parsed.username or parsed.password:
        raise RehearsalError(f"{label}: URL credentials must be supplied through the environment")
    return url


def _empty_or_new_directory(value: Any, label: str) -> Path:
    path = Path(_text(value, label)).expanduser().absolute()
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise RehearsalError(f"{label}: must be a real directory path")
    if path.exists() and any(path.iterdir()):
        raise RehearsalError(f"{label}: must be new or empty; existing data is never removed")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def validate_config(payload: Any, *, config_path: Path | None = None) -> dict[str, Any]:
    """Validate and normalize the host contract before any process starts."""

    if not isinstance(payload, dict):
        raise RehearsalError("config: expected a JSON object")
    required = (
        "data_root", "working_directory", "start_command", "stop_command", "health_url",
        "astrbot_version", "plugin_version", "target_version", "rollback_version",
        "channel", "account", "proxy", "steps",
    )
    for name in required:
        if name not in payload:
            raise RehearsalError(f"config: missing required field {name!r}")

    data_root = _empty_or_new_directory(payload["data_root"], "data_root")
    data_root_preexisting = data_root.exists()
    working_directory = Path(_text(payload["working_directory"], "working_directory")).expanduser().absolute()
    if working_directory.is_symlink() or not working_directory.is_dir():
        raise RehearsalError("working_directory: must be an existing directory")

    start_command = _command(payload["start_command"], "start_command")
    stop_command = _command(payload["stop_command"], "stop_command")
    _validate_placeholders(start_command, "start_command")
    _validate_placeholders(stop_command, "stop_command")

    health_url = _safe_url(payload["health_url"], "health_url")
    proxy = payload["proxy"]
    if not isinstance(proxy, dict):
        raise RehearsalError("proxy: expected an object")
    proxy_mode = _text(proxy.get("mode"), "proxy.mode").lower()
    if proxy_mode not in {"none", "http", "socks"}:
        raise RehearsalError("proxy.mode: expected none, http, or socks")
    proxy_url_env = ""
    if proxy_mode != "none":
        proxy_url_env = _text(proxy.get("url_env"), "proxy.url_env")
        if not proxy_url_env.isidentifier():
            raise RehearsalError("proxy.url_env: expected an environment variable name")
        if not os.environ.get(proxy_url_env):
            raise RehearsalError(f"proxy.url_env: environment variable {proxy_url_env!r} is not set")

    steps = payload["steps"]
    if not isinstance(steps, dict):
        raise RehearsalError("steps: expected an object")
    missing_phases = [phase for phase in REQUIRED_PHASES if phase not in steps]
    if missing_phases:
        raise RehearsalError(f"steps: missing required phase(s): {', '.join(missing_phases)}")
    normalized_steps: dict[str, dict[str, Any]] = {}
    for phase in REQUIRED_PHASES:
        item = steps[phase]
        if not isinstance(item, dict):
            raise RehearsalError(f"steps.{phase}: expected an object")
        command = _command(item.get("command"), f"steps.{phase}.command")
        _validate_placeholders(command, f"steps.{phase}.command")
        expected = item.get("expected_exit_codes", [0])
        if not isinstance(expected, list) or not expected:
            raise RehearsalError(f"steps.{phase}.expected_exit_codes: expected a non-empty array")
        if any(isinstance(code, bool) or not isinstance(code, int) for code in expected):
            raise RehearsalError(f"steps.{phase}.expected_exit_codes: expected integer exit codes")
        normalized_steps[phase] = {
            "command": command,
            "expected_exit_codes": sorted(set(expected)),
            "timeout": _bounded_number(
                item.get("timeout_seconds"), f"steps.{phase}.timeout_seconds",
                default=DEFAULT_COMMAND_TIMEOUT, maximum=3600.0,
            ),
        }

    report_path = payload.get("report_path")
    if report_path is None:
        suffix = ".host-rehearsal.json"
        report = (config_path or Path.cwd() / "host-rehearsal.json").with_suffix(suffix)
    else:
        report = Path(_text(report_path, "report_path")).expanduser().absolute()
    if report.resolve().parent == data_root.resolve() or data_root.resolve() in report.resolve().parents:
        raise RehearsalError("report_path: must be outside data_root")
    if report.exists():
        raise RehearsalError(f"report_path: refusing to overwrite existing file {report}")

    return {
        "schema_version": SCHEMA_VERSION,
        "data_root": data_root,
        "data_root_preexisting": data_root_preexisting,
        "working_directory": working_directory,
        "start_command": start_command,
        "stop_command": stop_command,
        "health_url": health_url,
        "astrbot_version": _text(payload["astrbot_version"], "astrbot_version"),
        "plugin_version": _text(payload["plugin_version"], "plugin_version"),
        "target_version": _text(payload["target_version"], "target_version"),
        "rollback_version": _text(payload["rollback_version"], "rollback_version"),
        "channel": _text(payload["channel"], "channel"),
        "account": _text(payload["account"], "account"),
        "proxy_mode": proxy_mode,
        "proxy_url_env": proxy_url_env,
        "steps": normalized_steps,
        "report_path": report,
        "health_timeout": _bounded_number(
            payload.get("health_timeout_seconds"), "health_timeout_seconds",
            default=DEFAULT_HEALTH_TIMEOUT, maximum=900.0,
        ),
        "health_interval": _bounded_number(
            payload.get("health_interval_seconds"), "health_interval_seconds",
            default=DEFAULT_HEALTH_INTERVAL, maximum=30.0,
        ),
    }


def load_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise RehearsalError(f"cannot read config {path}: {error}") from error
    return validate_config(payload, config_path=path)


def _render_command(command: Sequence[str], values: Mapping[str, Any]) -> list[str]:
    try:
        return [item.format(**values) for item in command]
    except (KeyError, ValueError) as error:
        raise RehearsalError(f"cannot render command: {error}") from error


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _account_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _error_category(text: str, *, timed_out: bool = False, return_code: int | None = None) -> str:
    if timed_out:
        return "timeout"
    lower = text.lower()
    if any(item in lower for item in ("proxy", "socks", "connect tunnel")):
        return "proxy"
    if any(item in lower for item in ("401", "403", "unauthor", "forbidden", "token", "api key")):
        return "auth"
    if any(item in lower for item in ("timeout", "timed out", "deadline")):
        return "timeout"
    if any(item in lower for item in ("connection", "network", "refused", "dns", "unreachable")):
        return "network"
    if return_code is not None:
        return "command"
    return "unknown"


def _run_command(
    command: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float,
    capture: bool = True,
) -> dict[str, Any]:
    started = time.monotonic()
    output = tempfile.TemporaryFile() if capture else None
    error_output = tempfile.TemporaryFile() if capture else None
    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    try:
        process = subprocess.Popen(
            list(command), cwd=str(cwd), env=dict(env),
            stdout=output if output is not None else subprocess.DEVNULL,
            stderr=error_output if error_output is not None else subprocess.DEVNULL,
        )
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            return_code = process.wait(timeout=10)
        sample = b""
        if error_output is not None:
            error_output.seek(0)
            sample = error_output.read(MAX_OUTPUT_SAMPLE_BYTES)
        if not sample and output is not None:
            output.seek(0)
            sample = output.read(MAX_OUTPUT_SAMPLE_BYTES)
        try:
            sample_text = sample.decode("utf-8", errors="replace")
        except Exception:
            sample_text = ""
        return {
            "exit_code": return_code,
            "timed_out": timed_out,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "error_category": "" if return_code == 0 and not timed_out else _error_category(
                sample_text, timed_out=timed_out, return_code=return_code,
            ),
        }
    except OSError as error:
        return {
            "exit_code": None,
            "timed_out": False,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "error_category": "command",
        }
    finally:
        if output is not None:
            output.close()
        if error_output is not None:
            error_output.close()


def _health_check(url: str, *, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(Request(url, headers={"Accept": "application/json"}), timeout=min(timeout, 10.0)) as response:
            status = int(response.status)
            response.read(512)
        return {
            "status": "passed" if 200 <= status < 300 else "failed",
            "http_status": status,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "error_category": "" if 200 <= status < 300 else "health",
        }
    except HTTPError as error:
        return {
            "status": "failed", "http_status": int(error.code),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "error_category": "health",
        }
    except (URLError, OSError, TimeoutError) as error:
        return {
            "status": "failed", "http_status": None,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "error_category": _error_category(str(error)),
        }


def _wait_for_health(url: str, *, timeout: float, interval: float, process: subprocess.Popen[bytes] | None) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {"status": "failed", "http_status": None, "error_category": "health"}
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            last["error_category"] = "host_exit"
            break
        last = _health_check(url, timeout=min(interval + 2.0, 10.0))
        if last["status"] == "passed":
            return last
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    last["status"] = "failed"
    last["duration_ms"] = int(timeout * 1000)
    return last


def _terminate_process(process: subprocess.Popen[bytes] | None) -> bool:
    if process is None or process.poll() is not None:
        return True
    try:
        process.terminate()
        process.wait(timeout=10)
        return True
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return False
        return True
    except OSError:
        return False


def run_rehearsal(config: Mapping[str, Any], *, keep_data: bool = False) -> dict[str, Any]:
    """Run every required phase and return a credential-free summary."""

    data_root = Path(config["data_root"])
    report_path = Path(config["report_path"])
    data_root.mkdir(parents=True, exist_ok=True)
    values = {
        "data_root": str(data_root),
        "report_path": str(report_path),
        "phase": "",
        "target_version": config["target_version"],
        "rollback_version": config["rollback_version"],
        "channel": config["channel"],
    }
    env = dict(os.environ)
    env.update({
        "SELFIE_REHEARSAL_DATA_ROOT": str(data_root),
        "SELFIE_REHEARSAL_REPORT": str(report_path),
        "SELFIE_REHEARSAL_ASTRBOT_VERSION": str(config["astrbot_version"]),
        "SELFIE_REHEARSAL_PLUGIN_VERSION": str(config["plugin_version"]),
        "SELFIE_REHEARSAL_TARGET_VERSION": str(config["target_version"]),
        "SELFIE_REHEARSAL_ROLLBACK_VERSION": str(config["rollback_version"]),
        "SELFIE_REHEARSAL_CHANNEL": str(config["channel"]),
        "SELFIE_REHEARSAL_ACCOUNT": str(config["account"]),
        "SELFIE_REHEARSAL_PROXY_MODE": str(config["proxy_mode"]),
    })
    if config["proxy_url_env"]:
        env["SELFIE_REHEARSAL_PROXY_URL"] = os.environ[config["proxy_url_env"]]

    started_at = _now()
    started = time.monotonic()
    phases: list[dict[str, Any]] = []
    health: list[dict[str, Any]] = []
    process: subprocess.Popen[bytes] | None = None
    stop_result: dict[str, Any] | None = None
    status = "passed"
    startup_started = time.monotonic()
    startup: dict[str, Any] = {
        "exit_code": None,
        "timed_out": False,
        "duration_ms": 0,
        "error_category": "",
    }
    try:
        # Keep the foreground host process so health checks and shutdown can
        # observe the same process that was actually started.
        process = subprocess.Popen(
            _render_command(config["start_command"], values),
            cwd=str(config["working_directory"]), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        startup["duration_ms"] = int((time.monotonic() - startup_started) * 1000)
        if process.poll() is not None:
            startup["exit_code"] = process.returncode
            startup["error_category"] = "host_exit"
            status = "failed"
    except OSError as error:
        startup["duration_ms"] = int((time.monotonic() - startup_started) * 1000)
        startup["error_category"] = "command"
        status = "failed"
    phases.append({"name": "startup", "status": "passed" if status == "passed" else "failed", **startup})

    if status == "passed":
        startup_health = _wait_for_health(
            config["health_url"], timeout=config["health_timeout"],
            interval=config["health_interval"], process=process,
        )
        startup_health["stage"] = "startup"
        health.append(startup_health)
        if startup_health["status"] != "passed":
            status = "failed"

    try:
        if status == "passed":
            for phase in REQUIRED_PHASES:
                values["phase"] = phase
                result = _run_command(
                    _render_command(config["steps"][phase]["command"], values),
                    cwd=Path(config["working_directory"]), env=env,
                    timeout=config["steps"][phase]["timeout"], capture=True,
                )
                passed = result["exit_code"] in config["steps"][phase]["expected_exit_codes"] and not result["timed_out"]
                phases.append({"name": phase, "status": "passed" if passed else "failed", **result})
                if not passed:
                    status = "failed"
                    break
                if phase == "restart":
                    # A restart command may replace the original foreground
                    # process.  From this point the health endpoint, not the
                    # stale Popen handle, is the source of truth.
                    process = None
                check = _wait_for_health(
                    config["health_url"], timeout=config["health_timeout"],
                    interval=config["health_interval"], process=process,
                )
                check["stage"] = phase
                health.append(check)
                if check["status"] != "passed":
                    status = "failed"
                    break
    finally:
        values["phase"] = "shutdown"
        stop_result = _run_command(
            _render_command(config["stop_command"], values),
            cwd=Path(config["working_directory"]), env=env,
            timeout=60.0, capture=True,
        )
        if stop_result["exit_code"] != 0 or stop_result["timed_out"]:
            status = "failed"
        if not _terminate_process(process):
            status = "failed"

    finished_at = _now()
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "metadata": {
            "astrbot_version": config["astrbot_version"],
            "plugin_version": config["plugin_version"],
            "target_version": config["target_version"],
            "rollback_version": config["rollback_version"],
            "channel": config["channel"],
            "account_fingerprint": _account_fingerprint(config["account"]),
            "proxy_mode": config["proxy_mode"],
        },
        "data_root": str(data_root),
        "kept_data": bool(keep_data),
        "health": health,
        "phases": phases,
        "shutdown": stop_result,
    }
    if not keep_data:
        try:
            if config["data_root_preexisting"]:
                report["data_cleanup"] = "preserved_existing_empty"
            else:
                shutil.rmtree(data_root)
                report["data_cleanup"] = "removed"
        except OSError:
            report["data_cleanup"] = "failed"
            report["status"] = "failed"
    else:
        report["data_cleanup"] = "kept"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="JSON rehearsal contract")
    parser.add_argument("--keep-data", action="store_true", help="retain the isolated data root for diagnosis")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        report = run_rehearsal(config, keep_data=args.keep_data)
    except RehearsalError as error:
        print(f"[host-rehearsal] {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"[host-rehearsal] execution failed: {error}", file=sys.stderr)
        return 1
    print(
        f"[host-rehearsal] {report['status']}: phases={len(report['phases'])} "
        f"health_checks={len(report['health'])} report={config['report_path']}"
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
