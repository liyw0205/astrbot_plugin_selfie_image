"""Embedded Flask Web UI for Selfie Image."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from ..core.utils import (
    generation_record_media_sources,
    redact_generation_record,
    redact_generation_record_for_detail,
    redact_sensitive_data,
    redact_sensitive_text,
)
from ..generation.generation_records import metric_window_seconds
from ..generation.generation_records import build_record_scope_stats
from .contracts import TASK_CANCEL_ROUTE_ALIASES


try:
    from flask import Flask, jsonify, request, send_file
    from werkzeug.serving import make_server
except Exception:  # pragma: no cover - handled at runtime in AstrBot env
    Flask = None  # type: ignore
    jsonify = None  # type: ignore
    request = None  # type: ignore
    send_file = None  # type: ignore
    make_server = None  # type: ignore


WEB_TASK_ID_RE = re.compile(r"^(?:web|web-studio|cmd)-\d{8,}-\d+$")
MAX_WEB_TASK_ID_LENGTH = 64
MAX_CACHE_IMAGE_PATH_LENGTH = 512
MAX_WEB_RECORD_ID_LENGTH = 128
MAX_RECORD_PAGE_LIMIT = 1000
MAX_ASSET_PAGE_LIMIT = 100
MAX_TASK_PAGE_LIMIT = 200
MAX_TASK_OFFSET = 1_000_000
PAGE_PREVIEW_MAX_BYTES = 64 * 1024 * 1024
_LOGO_SRC_PLACEHOLDER = "__SELFIE_LOGO_SRC__"
logger = logging.getLogger(__name__)
_INDEX_SOURCE_STATUS: dict[str, str] = {
    "source": "embedded",
    "path": "",
    "error": "",
}


def _bundled_logo_data_url() -> str:
    """Inline logo so Flask and AstrBot iframe both show the same brand mark."""
    path = os.path.join(Path(__file__).resolve().parents[1], "logo.png")
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        if not raw:
            return ""
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        return ""


def render_index_html(html: Optional[str] = None) -> str:
    if html is None:
        external_page = Path(__file__).resolve().parents[1] / "pages" / "dashboard" / "index.html"
        try:
            text = external_page.read_text(encoding="utf-8") if external_page.is_file() else INDEX_HTML
        except (OSError, UnicodeError) as exc:
            logger.warning("[SelfieImage] dashboard page render read failed; using selected fallback: %s", exc)
            text = INDEX_HTML
    else:
        text = str(html)
    logo = _bundled_logo_data_url()
    if logo:
        # Standalone Flask pages inline the logo; AstrBot-served plugin pages
        # keep the relative asset URL so its page token can be appended.
        return text.replace(_LOGO_SRC_PLACEHOLDER, logo).replace('src="logo.png"', f'src="{logo}"').replace('href="logo.png"', f'href="{logo}"')
    # Hide broken image slot when logo file is missing.
    return text.replace(f'src="{_LOGO_SRC_PLACEHOLDER}"', 'src="" style="display:none"')


EMBEDDED_INDEX_HTML = ""


def _load_external_index_html() -> str:
    """Use the maintained page file when available; retain the embedded page as fallback."""
    external_page = Path(__file__).resolve().parents[1] / "pages" / "dashboard" / "index.html"
    try:
        if external_page.is_file():
            text = external_page.read_text(encoding="utf-8")
            _INDEX_SOURCE_STATUS.update({"source": "external", "path": str(external_page), "error": ""})
            return text
        _INDEX_SOURCE_STATUS.update({"source": "embedded", "path": str(external_page), "error": "文件不存在"})
        logger.warning("[SelfieImage] dashboard page file is missing; using embedded fallback: %s", external_page)
    except (OSError, UnicodeError) as exc:
        _INDEX_SOURCE_STATUS.update({"source": "embedded", "path": str(external_page), "error": f"{type(exc).__name__}: {exc}"})
        logger.warning("[SelfieImage] dashboard page read failed; using embedded fallback %s: %s", external_page, exc)
    return GENERATED_INDEX_HTML


def dashboard_page_source_status() -> dict[str, str]:
    """Return the page source selected at import/startup time."""
    return dict(_INDEX_SOURCE_STATUS)


try:
    from .index_fallback import INDEX_HTML as GENERATED_INDEX_HTML
except Exception as exc:  # pragma: no cover - only broken package artifacts
    logger.error("[SelfieImage] generated dashboard fallback unavailable: %s", exc)
    GENERATED_INDEX_HTML = EMBEDDED_INDEX_HTML


INDEX_HTML = _load_external_index_html()


class _ServerThread(threading.Thread):
    def __init__(self, app: Any, host: str, port: int):
        super().__init__(daemon=True)
        self.server = make_server(host, port, app, threaded=True)
        self.context = app.app_context()
        self.context.push()

    def run(self) -> None:
        self.server.serve_forever()

    def shutdown(self) -> None:
        try:
            self.server.shutdown()
        finally:
            # ``shutdown`` stops serve_forever but does not release the
            # listening socket. Close it so an immediate plugin reload can
            # bind the same port again.
            self.server.server_close()


class FlaskWebServer:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.thread: Optional[_ServerThread] = None
        self.host = ""
        self.port = 0

    def start(self, host: str, port: int) -> None:
        if Flask is None or make_server is None:
            raise RuntimeError("Flask 未安装，请先安装 requirements.txt 中的 Flask/Werkzeug")
        if self.thread and self.host == host and self.port == port:
            return
        self.stop()
        app = self._create_app()
        self.thread = _ServerThread(app, host, port)
        self.host = host
        self.port = port
        self.thread.start()

    def stop(self) -> None:
        thread = self.thread
        if not thread:
            return
        thread.shutdown()
        thread.join(timeout=5)
        self.thread = None

    def _run_async(self, coro: Any, timeout: Optional[float] = None) -> Any:
        loop = getattr(self.plugin, "loop", None)
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout)
        return asyncio.run(coro)

    def _create_app(self) -> Any:
        app = Flask("astrbot_plugin_selfie_image")
        app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

        def ok(data: Any = None, **extra: Any) -> Any:
            payload = {"success": True, "data": data}
            payload.update(extra)
            return jsonify(payload)

        def fail(message: str, status: int = 400) -> Any:
            return jsonify({"success": False, "error": redact_sensitive_text(message)}), status

        @app.after_request
        def add_response_safety_headers(response: Any) -> Any:
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault("X-Frame-Options", "DENY")
            if str(request.path or "").startswith("/api/"):
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
            return response

        def json_object_payload() -> Any:
            payload = request.get_json(silent=True)
            if payload is None:
                raw_body = request.get_data(cache=True) or b""
                if raw_body.strip():
                    return None, fail("请求体必须是 JSON 对象")
                return {}, None
            if not isinstance(payload, dict):
                return None, fail("请求体必须是 JSON 对象")
            return payload, None

        def int_query_arg(name: str, default: int, minimum: int, maximum: int) -> tuple[Optional[int], Optional[Any]]:
            raw_value = str(request.args.get(name, "") or "").strip()
            if not raw_value:
                return default, None
            try:
                value = int(raw_value)
            except ValueError:
                return None, fail(f"{name} 必须是整数", 400)
            if value < minimum:
                return None, fail(f"{name} 不能小于 {minimum}", 400)
            return min(value, maximum), None

        def record_matches_query(record: Any, source: str, model: str, success: str, keyword: str, media_type: str = "") -> bool:
            if not isinstance(record, dict):
                return False
            if media_type and str(record.get("media_type") or "image").strip().lower() != media_type:
                return False
            if source:
                source_text = " ".join(
                    str(record.get(key) or "")
                    for key in ("source_label", "source", "group_id", "user_id")
                ).lower()
                if source not in source_text:
                    return False
            if model and model not in str(record.get("used_model") or "").lower():
                return False
            if success:
                expected = success in {"1", "true", "yes", "ok", "success", "succeeded", "成功"}
                if bool(record.get("success")) is not expected:
                    return False
            if keyword:
                text = " ".join(
                    str(record.get(key) or "")
                    for key in (
                        "source",
                        "source_label",
                        "used_model",
                        "error",
                        "failure_reason",
                        "original_prompt",
                        "request_prompt",
                        "group_id",
                        "user_id",
                    )
                ).lower()
                if keyword not in text:
                    return False
            return True

        def query_bool(value: Any) -> Optional[bool]:
            lowered = str(value or "").strip().lower()
            if not lowered:
                return None
            if lowered in {"1", "true", "yes", "on", "是", "开启"}:
                return True
            if lowered in {"0", "false", "no", "off", "否", "关闭"}:
                return False
            return None

        def filtered_record_payload(records: list[Any]) -> Any:
            source = str(request.args.get("source") or "").strip().lower()
            model = str(request.args.get("model") or "").strip().lower()
            media_type = str(request.args.get("media_type") or "").strip().lower()
            if media_type not in {"", "image", "video"}:
                return None, None, fail("media_type 必须是 image 或 video", 400)
            success = str(request.args.get("success") or "").strip().lower()
            favorite = query_bool(request.args.get("favorite"))
            pinned = query_bool(request.args.get("pinned"))
            tag = str(request.args.get("tag") or "").strip().lower()
            keyword = str(request.args.get("q") or request.args.get("keyword") or "").strip().lower()
            if success and success not in {"1", "0", "true", "false", "yes", "no", "ok", "success", "succeeded", "failed", "失败", "成功"}:
                return None, None, fail("success 必须是 true 或 false", 400)

            offset, error_response = int_query_arg("offset", 0, 0, 10000)
            if error_response:
                return None, None, error_response
            default_limit = min(MAX_RECORD_PAGE_LIMIT, len(records))
            limit, error_response = int_query_arg("limit", default_limit, 1, MAX_RECORD_PAGE_LIMIT)
            if error_response:
                return None, None, error_response

            filtered = [
                record
                for record in records
                if record_matches_query(record, source, model, success, keyword, media_type)
                and (favorite is None or bool(record.get("favorite")) is favorite)
                and (pinned is None or bool(record.get("pinned")) is pinned)
                and (not tag or tag in {str(item).strip().lower() for item in (record.get("tags") or [])})
            ]
            page = filtered[offset : offset + limit]
            meta = {
                "total": len(records),
                "filtered": len(filtered),
                "offset": offset,
                "limit": limit,
                "scope_stats": build_record_scope_stats(filtered),
            }
            return page, meta, None

        def token_candidates_from_request() -> list[str]:
            tokens: list[str] = []
            auth = str(request.headers.get("Authorization") or "")
            if auth.lower().startswith("bearer "):
                tokens.append(auth[7:].strip())
            tokens.extend(
                str(request.headers.get(name) or "").strip()
                for name in ("X-Selfie-Image-Token", "X-AICat-Token", "X-Token")
            )
            return [token for token in tokens if token]

        def check_auth() -> bool:
            configured = str(getattr(self.plugin.config, "web_token", "") or "").strip()
            if not configured:
                return True
            configured_bytes = configured.encode("utf-8")
            for token in token_candidates_from_request():
                try:
                    if hmac.compare_digest(token.encode("utf-8"), configured_bytes):
                        return True
                except Exception:
                    continue
            return False

        @app.route("/", methods=["GET"])
        @app.route("/index.html", methods=["GET"])
        def index() -> Any:
            return render_index_html()

        @app.route("/logo.png", methods=["GET"])
        def logo() -> Any:
            logo_path = Path(__file__).resolve().parents[1] / "logo.png"
            if not logo_path.is_file():
                return fail("Logo not found", 404)
            return send_file(logo_path, mimetype="image/png", max_age=3600)

        @app.route("/api/plugin/page/bridge-sdk.js", methods=["GET"])
        def bridge_sdk_fallback() -> Any:
            # AstrBot replaces this URL with its real bridge when embedded.
            # The standalone Flask page only needs a successful script load.
            return (
                "window.AstrBotPluginPage = window.AstrBotPluginPage || undefined;\n",
                200,
                {"Content-Type": "application/javascript; charset=utf-8", "Cache-Control": "no-store"},
            )

        @app.route("/api/health", methods=["GET"])
        def health() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            stats = getattr(self.plugin, "_cache_stats", None)
            cache_bytes, cache_count = stats() if callable(stats) else (self.plugin._cache_size_bytes(), 0)
            get_health = getattr(self.plugin, "get_channel_health", None)
            get_preview = getattr(self.plugin, "get_cache_cleanup_preview", None)
            return ok(
                {
                    "status": "ok",
                    "config_path": getattr(self.plugin, "config_path", ""),
                    "records_path": getattr(self.plugin, "records_path", ""),
                    "records_db_path": getattr(self.plugin, "records_db_path", ""),
                    "media_sources_dir": getattr(self.plugin, "media_sources_dir", ""),
                    "cache_dir": getattr(self.plugin, "generated_dir", ""),
                    "cache_size_mb": round(float(cache_bytes) / 1024 / 1024, 2),
                    "cache_file_count": cache_count,
                    "cache_limit_mb": getattr(self.plugin.config, "image_cache_limit_mb", 200),
                    "cache_limit_count": getattr(self.plugin.config, "image_cache_limit_count", 100),
                    "channel_health": get_health() if callable(get_health) else {},
                    "cache_cleanup_preview": get_preview() if callable(get_preview) else {},
                    "dashboard_page": dashboard_page_source_status(),
                }
            )

        @app.route("/api/metrics", methods=["GET"])
        def metrics() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            try:
                try:
                    window_seconds = metric_window_seconds(request.args.get("window"))
                except ValueError as exc:
                    return fail(str(exc), 400)
                if window_seconds is None:
                    metrics_data = self.plugin.get_generation_metrics()
                else:
                    metrics_data = self.plugin.get_generation_metrics(window_seconds=window_seconds)
                return ok(redact_sensitive_data(metrics_data))
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/tasks", methods=["GET"])
        def tasks() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            media_type = str(request.args.get("media_type") or "").strip().lower()
            if media_type not in {"", "image", "video"}:
                return fail("media_type 必须是 image 或 video", 400)
            include_finished = str(request.args.get("include_finished") or "").strip().lower() in {"1", "true", "yes", "on"}
            status_query = str(request.args.get("status") or "").strip().lower()
            valid_statuses = {
                "queued", "running", "succeeded", "partial_success", "failed",
                "delivery_failed", "cancelled", "expired",
            }
            if status_query:
                requested_statuses = {item.strip() for item in status_query.split(",") if item.strip()}
                if requested_statuses - valid_statuses:
                    return fail("status 包含不支持的任务状态", 400)
            raw_limit = str(request.args.get("limit") or "50").strip()
            try:
                limit = int(raw_limit)
            except ValueError:
                return fail("limit 必须是整数", 400)
            if limit < 1:
                return fail("limit 不能小于 1", 400)
            if limit > MAX_TASK_PAGE_LIMIT:
                return fail(f"limit 不能大于 {MAX_TASK_PAGE_LIMIT}", 400)
            raw_offset = str(request.args.get("offset") or "0").strip()
            try:
                offset = int(raw_offset)
            except ValueError:
                return fail("offset 必须是整数", 400)
            if offset < 0:
                return fail("offset 不能小于 0", 400)
            if offset > MAX_TASK_OFFSET:
                return fail(f"offset 不能大于 {MAX_TASK_OFFSET}", 400)
            def parse_timestamp(name: str, *, end_of_day: bool = False) -> tuple[Optional[float], Optional[Any]]:
                raw = str(request.args.get(name) or "").strip()
                if not raw:
                    return None, None
                try:
                    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
                        parsed = datetime.strptime(raw, "%Y-%m-%d")
                        if end_of_day:
                            parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
                        return time.mktime(parsed.timetuple()) + parsed.microsecond / 1_000_000, None
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        return time.mktime(parsed.timetuple()) + parsed.microsecond / 1_000_000, None
                    return parsed.astimezone(timezone.utc).timestamp(), None
                except (TypeError, ValueError):
                    return None, fail(f"{name} 必须是 YYYY-MM-DD 或 ISO 时间", 400)

            start_ts, parse_error = parse_timestamp("start_date")
            if parse_error:
                return parse_error
            end_ts, parse_error = parse_timestamp("end_date", end_of_day=True)
            if parse_error:
                return parse_error
            task_kwargs = {
                "include_finished": include_finished,
                "limit": limit,
                "offset": offset,
                "media_type": media_type,
            }
            optional_filters = {
                "source": str(request.args.get("source") or ""),
                "status": str(request.args.get("status") or ""),
                "model": str(request.args.get("model") or ""),
                "keyword": str(request.args.get("q") or request.args.get("keyword") or ""),
            }
            for key, value in optional_filters.items():
                if value.strip():
                    task_kwargs[key] = value
            if start_ts is not None:
                task_kwargs["start_ts"] = start_ts
            if end_ts is not None:
                task_kwargs["end_ts"] = end_ts
            try:
                data = self.plugin.list_web_tasks(**task_kwargs)
                return ok(redact_sensitive_data(data))
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/tasks/<task_id>", methods=["GET"])
        def task_detail(task_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            task_id_text = str(task_id or "").strip()
            if len(task_id_text) > MAX_WEB_TASK_ID_LENGTH or not WEB_TASK_ID_RE.fullmatch(task_id_text):
                return fail("非法任务 ID", 400)
            try:
                getter = getattr(self.plugin, "get_web_task_detail", None)
                data = getter(task_id_text) if callable(getter) else self.plugin.get_web_image_task(task_id_text)
                return ok(redact_sensitive_data(data))
            except Exception as exc:
                return fail(str(exc), 404)

        def task_ids_from_payload(payload: Any) -> tuple[Optional[list[str]], Optional[str]]:
            raw = (payload or {}).get("ids", (payload or {}).get("task_ids")) if isinstance(payload, dict) else None
            if not isinstance(raw, list):
                return None, "ids 必须是数组"
            ids = list(dict.fromkeys(str(item or "").strip() for item in raw if str(item or "").strip()))
            if not ids:
                return None, "至少选择一条任务"
            if len(ids) > 200:
                return None, "单次最多操作 200 条任务"
            if any(len(task_id) > MAX_WEB_TASK_ID_LENGTH or not WEB_TASK_ID_RE.fullmatch(task_id) for task_id in ids):
                return None, "包含非法任务 ID"
            return ids, None

        @app.route("/api/tasks/export", methods=["GET"])
        def tasks_export() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            raw_ids = str(request.args.get("ids") or "").strip()
            ids = [item.strip() for item in raw_ids.split(",") if item.strip()] if raw_ids else None
            if ids is not None and (
                len(ids) > 200
                or any(len(task_id) > MAX_WEB_TASK_ID_LENGTH or not WEB_TASK_ID_RE.fullmatch(task_id) for task_id in ids)
            ):
                return fail("包含非法任务 ID", 400)
            exporter = getattr(self.plugin, "export_web_tasks", None)
            if not callable(exporter):
                return fail("当前版本不支持任务导出", 501)
            try:
                return ok(exporter(ids))
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/tasks/delete", methods=["POST"])
        def tasks_delete() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            ids, validation_error = task_ids_from_payload(payload)
            if validation_error:
                return fail(validation_error, 400)
            deleter = getattr(self.plugin, "delete_web_tasks", None)
            if not callable(deleter):
                return fail("当前版本不支持任务删除", 501)
            try:
                return ok(deleter(ids or []), message="任务记录已删除")
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/tasks/retry", methods=["POST"])
        def tasks_retry() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            ids, validation_error = task_ids_from_payload(payload)
            if validation_error:
                return fail(validation_error, 400)
            retrier = getattr(self.plugin, "retry_web_tasks", None)
            if not callable(retrier):
                return fail("当前版本不支持任务重试", 501)
            try:
                feedback = str((payload or {}).get("feedback") or "").strip()[:2000]
                strategy = str((payload or {}).get("strategy") or (payload or {}).get("retry_strategy") or "full")
                try:
                    result = retrier(ids or [], feedback, strategy)
                except TypeError:
                    result = retrier(ids or [], feedback)
                return ok(result, message="已提交任务重试")
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/config", methods=["GET", "POST"])
        def config_route() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            if request.method == "GET":
                return ok(self.plugin.get_config_for_web())
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            if "config" in payload:
                if not isinstance(payload.get("config"), dict):
                    return fail("config 必须是 JSON 对象")
                patch = payload["config"]
            else:
                patch = payload
            try:
                data = self.plugin.update_config_from_web(patch)
                return ok(data)
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/selfie-reference", methods=["GET", "POST"])
        def selfie_reference() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            if request.method == "GET":
                return ok(self.plugin.get_selfie_reference_payload())
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                data = self.plugin.save_selfie_reference_from_web(payload)
                return ok(data, message="自拍参考图已保存")
            except Exception as exc:
                return fail(str(exc))

        @app.route("/api/selfie-reference/clear", methods=["POST"])
        def selfie_reference_clear() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            _, error_response = json_object_payload()
            if error_response:
                return error_response
            return ok(self.plugin.clear_selfie_reference_from_web(), message="自拍参考图已清除")

        @app.route("/api/selfie-appearance-type", methods=["POST"])
        def selfie_appearance_type() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                data = self.plugin.set_selfie_appearance_type_from_web(payload)
                return ok(data, message="形象类型已保存")
            except Exception as exc:
                return fail(str(exc))

        @app.route("/api/selfie-profile/refresh", methods=["POST"])
        def selfie_profile_refresh() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            _, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                data = self._run_async(self.plugin.refresh_selfie_profile_from_web(), timeout=20)
                return ok(data, message="今日自拍设定已刷新")
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/test-image-channel", methods=["POST"])
        def test_image_channel() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                data = self._run_async(self.plugin.web_test_image(payload), timeout=max(30, self.plugin.config.image_global_timeout + 30))
                return ok(redact_sensitive_data(data))
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/test-image-channel/tasks", methods=["POST"])
        def test_image_channel_task_start() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                data = self.plugin.start_web_image_task(payload)
                return ok(redact_sensitive_data(data))
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/test-image-channel/tasks/<task_id>", methods=["GET"])
        def test_image_channel_task_status(task_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            task_id_text = str(task_id or "").strip()
            if len(task_id_text) > MAX_WEB_TASK_ID_LENGTH or not WEB_TASK_ID_RE.fullmatch(task_id_text):
                return fail("非法任务 ID", 400)
            try:
                return ok(redact_sensitive_data(self.plugin.get_web_image_task(task_id_text)))
            except Exception as exc:
                return fail(str(exc), 404)

        def cancel_generation_task(task_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            task_id_text = str(task_id or "").strip()
            if len(task_id_text) > MAX_WEB_TASK_ID_LENGTH or not WEB_TASK_ID_RE.fullmatch(task_id_text):
                return fail("非法任务 ID", 400)
            try:
                task = self.plugin.get_web_image_task(task_id_text)
                if str(task.get("status") or "") not in {"queued", "running"}:
                    return fail("任务已经结束，不能取消", 409)
                message = self.plugin.cancel_image_task(task_id_text, is_admin=True)
                updated = self.plugin.get_web_image_task(task_id_text)
                if str(updated.get("status") or "") not in {"cancelled"} and not updated.get("cancel_requested"):
                    return fail("任务已经结束，不能取消", 409)
                return ok(redact_sensitive_data(updated), message=message)
            except PermissionError as exc:
                return fail(str(exc), 403)
            except Exception as exc:
                return fail(str(exc), 404)

        for route in TASK_CANCEL_ROUTE_ALIASES:
            app.add_url_rule(
                route.replace("{task_id}", "<task_id>"),
                endpoint="cancel_generation_task_" + route.split("/")[2].replace("-", "_"),
                view_func=cancel_generation_task,
                methods=["POST"],
            )

        @app.route("/api/test-video-channel/tasks", methods=["POST"])
        def test_video_channel_task_start() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                data = self.plugin.start_web_image_task({**payload, "media_type": "video"})
                return ok(redact_sensitive_data(data))
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/test-video-channel/tasks/<task_id>", methods=["GET"])
        def test_video_channel_task_status(task_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            task_id_text = str(task_id or "").strip()
            if len(task_id_text) > MAX_WEB_TASK_ID_LENGTH or not WEB_TASK_ID_RE.fullmatch(task_id_text):
                return fail("非法任务 ID", 400)
            try:
                return ok(redact_sensitive_data(self.plugin.get_web_image_task(task_id_text)))
            except Exception as exc:
                return fail(str(exc), 404)


        @app.route("/api/proxies", methods=["GET"])
        def proxies_list() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            try:
                return ok(self.plugin.list_proxies_for_web(mask_password=True))
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/proxies/test", methods=["POST"])
        def proxies_test() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                data = self._run_async(
                    self.plugin.test_proxy_connectivity(
                        proxy_id=str((payload or {}).get("id") or (payload or {}).get("proxy_id") or ""),
                        proxy=(payload or {}).get("proxy") if isinstance((payload or {}).get("proxy"), dict) else payload,
                    ),
                    timeout=30,
                )
                return ok(data)
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/proxies/quality-check", methods=["POST"])
        def proxies_quality() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                data = self._run_async(
                    self.plugin.test_proxy_quality(
                        proxy_id=str((payload or {}).get("id") or (payload or {}).get("proxy_id") or ""),
                        proxy=(payload or {}).get("proxy") if isinstance((payload or {}).get("proxy"), dict) else payload,
                    ),
                    timeout=90,
                )
                return ok(data)
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/refresh-image-models", methods=["POST"])
        def refresh_image_models() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                data = self._run_async(self.plugin.web_refresh_image_models(payload), timeout=30)
                return ok(data, count=len(data))
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/records", methods=["GET"])
        def records() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            data = [
                redact_generation_record(item)
                for item in self.plugin.get_recent_records(summary=True)
                if isinstance(item, dict)
            ]
            page, meta, error_response = filtered_record_payload(data)
            if error_response:
                return error_response
            return ok(page, **meta)

        @app.route("/api/records/<record_id>", methods=["GET"])
        def record_detail(record_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            record_id_text = str(record_id or "").strip()
            if not record_id_text or len(record_id_text) > MAX_WEB_RECORD_ID_LENGTH:
                return fail("非法记录 ID", 400)
            try:
                return ok(redact_generation_record_for_detail(self.plugin.get_record_for_web(record_id_text)))
            except Exception as exc:
                return fail(str(exc), 404)

        @app.route("/api/records/<record_id>/media-sources", methods=["GET"])
        def record_media_sources(record_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            record_id_text = str(record_id or "").strip()
            if not record_id_text or len(record_id_text) > MAX_WEB_RECORD_ID_LENGTH:
                return fail("非法记录 ID", 400)
            try:
                return ok(generation_record_media_sources(self.plugin.get_record_for_web(record_id_text)))
            except Exception as exc:
                return fail(str(exc), 404)

        @app.route("/api/records/<record_id>/metadata", methods=["POST"])
        @app.route("/api/records/<record_id>/asset", methods=["POST"])
        def record_metadata(record_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            record_id_text = str(record_id or "").strip()
            if not record_id_text or len(record_id_text) > MAX_WEB_RECORD_ID_LENGTH:
                return fail("非法记录 ID", 400)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.update_record_asset_metadata(record_id_text, payload or {}), message="资产标记已保存")
            except Exception as exc:
                return fail(str(exc), 404)

        @app.route("/api/records/<record_id>/reuse", methods=["GET", "POST"])
        def record_reuse(record_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            record_id_text = str(record_id or "").strip()
            if not record_id_text or len(record_id_text) > MAX_WEB_RECORD_ID_LENGTH:
                return fail("非法记录 ID", 400)
            try:
                return ok(self.plugin.get_record_reuse_payload(record_id_text))
            except Exception as exc:
                return fail(str(exc), 404)

        @app.route("/api/records/<record_id>/retry", methods=["POST"])
        def record_retry(record_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            record_id_text = str(record_id or "").strip()
            if not record_id_text or len(record_id_text) > MAX_WEB_RECORD_ID_LENGTH:
                return fail("非法记录 ID", 400)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            retry = getattr(self.plugin, "start_record_retry_task", None)
            if not callable(retry):
                return fail("当前版本不支持记录重试", 501)
            try:
                feedback = str((payload or {}).get("feedback") or "")
                strategy = str((payload or {}).get("strategy") or (payload or {}).get("retry_strategy") or "full")
                try:
                    task = retry(record_id_text, feedback, strategy)
                except TypeError:
                    # Keep compatibility with third-party/test plugin shims
                    # that still expose the original two-argument method.
                    task = retry(record_id_text, feedback)
                return ok(redact_sensitive_data(task), message="已提交重试任务")
            except ValueError as exc:
                return fail(str(exc), 400)
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/assets", methods=["GET"])
        def assets() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            favorite = query_bool(request.args.get("favorite"))
            pinned = query_bool(request.args.get("pinned"))
            try:
                media_type = str(request.args.get("media_type") or "").strip().lower()
                if media_type not in {"", "image", "video"}:
                    return fail("media_type 必须是 image 或 video", 400)
                raw_success = str(request.args.get("success") or "").strip().lower()
                success = query_bool(raw_success)
                if raw_success and success is None:
                    return fail("success 必须是 true 或 false", 400)
                raw_limit = str(request.args.get("limit") or "48").strip()
                raw_offset = str(request.args.get("offset") or "0").strip()
                try:
                    limit = int(raw_limit)
                    offset = int(raw_offset)
                except ValueError:
                    return fail("limit 和 offset 必须是整数", 400)
                if limit < 1:
                    return fail("limit 不能小于 1", 400)
                if limit > MAX_ASSET_PAGE_LIMIT:
                    return fail(f"limit 不能大于 {MAX_ASSET_PAGE_LIMIT}", 400)
                if offset < 0:
                    return fail("offset 不能小于 0", 400)
                sort = str(request.args.get("sort") or "recent").strip().lower()
                if sort not in {"recent", "oldest", "priority", "pinned", "favorite", "asc"}:
                    return fail("sort 必须是 recent、oldest、priority、pinned、favorite 或 asc", 400)
                query = getattr(self.plugin, "query_asset_records", None)
                if callable(query):
                    data, meta = query(
                        favorite=favorite,
                        pinned=pinned,
                        tag=str(request.args.get("tag") or ""),
                        source=str(request.args.get("source") or ""),
                        model=str(request.args.get("model") or ""),
                        media_type=media_type,
                        success=success,
                        keyword=str(request.args.get("q") or request.args.get("keyword") or ""),
                        start_time=str(request.args.get("start_time") or ""),
                        end_time=str(request.args.get("end_time") or ""),
                        offset=offset,
                        limit=limit,
                        sort=sort,
                    )
                    return ok(redact_sensitive_data(data), count=len(data), **meta)
                data = self.plugin.get_asset_records(
                    favorite=favorite,
                    pinned=pinned,
                    tag=str(request.args.get("tag") or ""),
                )
                return ok(redact_sensitive_data(data), count=len(data), total=len(data), filtered=len(data), offset=0, limit=len(data))
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/assets/tags", methods=["GET"])
        def assets_tags() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            try:
                getter = getattr(self.plugin, "list_asset_tags", None)
                if not callable(getter):
                    return fail("当前版本不支持资产标签列表", 501)
                return ok(getter())
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/assets/export", methods=["GET"])
        def assets_export() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            try:
                exporter = getattr(self.plugin, "export_asset_metadata", None)
                if not callable(exporter):
                    return fail("当前版本不支持资产导出", 501)
                raw_ids = str(request.args.get("ids") or "").strip()
                ids = [item.strip() for item in raw_ids.split(",") if item.strip()] if raw_ids else None
                if ids is not None and len(ids) > MAX_RECORD_PAGE_LIMIT:
                    return fail(f"单次最多导出 {MAX_RECORD_PAGE_LIMIT} 条资产", 400)
                return ok(exporter(ids))
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/assets/import", methods=["POST"])
        def assets_import() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                importer = getattr(self.plugin, "import_asset_metadata", None)
                if not callable(importer):
                    return fail("当前版本不支持资产导入", 501)
                return ok(importer(payload or {}), message="资产元数据已导入")
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/assets/metadata", methods=["POST"])
        def assets_metadata() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            ids = (payload or {}).get("ids", (payload or {}).get("record_ids"))
            if not isinstance(ids, list):
                return fail("ids 必须是数组", 400)
            try:
                return ok(self.plugin.update_records_asset_metadata(ids, payload or {}), message="批量资产标记已保存")
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/assets/delete", methods=["POST"])
        def assets_delete() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            ids = (payload or {}).get("ids", (payload or {}).get("record_ids"))
            if not isinstance(ids, list):
                return fail("ids 必须是数组", 400)
            try:
                return ok(self.plugin.delete_records(ids), message="资产已删除")
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/assets/<record_id>/studio", methods=["POST"])
        def asset_studio(record_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.studio_add_asset(record_id, payload or {}), message="资产已加入画布")
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/assets/studio", methods=["POST"])
        def assets_studio() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            ids = (payload or {}).get("ids", (payload or {}).get("record_ids"))
            if not isinstance(ids, list):
                return fail("ids 必须是数组", 400)
            try:
                return ok(self.plugin.studio_add_assets(ids, payload or {}), message="资产已加入画布")
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/records/clear", methods=["POST"])
        def records_clear() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            _, error_response = json_object_payload()
            if error_response:
                return error_response
            return ok({"deleted": self.plugin.clear_recent_records()})

        @app.route("/api/cache/cleanup", methods=["POST"])
        def cache_cleanup() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            cleanup = getattr(self.plugin, "cleanup_image_cache_from_web", None)
            if not callable(cleanup):
                return fail("当前版本不支持手动缓存清理", 501)
            raw_confirm = (payload or {}).get("confirm", False)
            confirm = (
                bool(raw_confirm)
                if not isinstance(raw_confirm, str)
                else str(raw_confirm).strip().lower() in {"1", "true", "yes", "on"}
            )
            token = str((payload or {}).get("plan_token") or "").strip()
            try:
                data = cleanup(confirm=confirm, plan_token=token)
                return ok(data, message="缓存清理完成" if confirm else "请确认缓存清理")
            except ValueError as exc:
                return fail(str(exc), 409)
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/cache-image", methods=["GET"])
        def cache_image() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            try:
                rel_path = str(request.args.get("path") or "")
                if len(rel_path) > MAX_CACHE_IMAGE_PATH_LENGTH:
                    return fail("图片路径过长", 400)
                info = self.plugin.get_cached_image_info(rel_path)
            except Exception as exc:
                return fail(str(exc), 400)
            if not info.get("exists"):
                return fail("图片已清理", 404)
            if not info.get("is_image") and not info.get("is_video"):
                return fail("缓存文件不是有效图片或视频", 400)
            return send_file(info["absolute_path"], mimetype=info.get("mime_type") or "application/octet-stream")


        @app.route("/api/cache-image-preview", methods=["GET"])
        def cache_image_preview() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            try:
                rel_path = str(request.args.get("path") or "")
                if len(rel_path) > MAX_CACHE_IMAGE_PATH_LENGTH:
                    return fail("缓存路径过长", 400)
                info = self.plugin.get_cached_image_info(rel_path)
            except Exception as exc:
                return fail(str(exc), 400)
            if not info.get("exists"):
                return fail("缓存文件已清理", 404)
            if not info.get("is_image") and not info.get("is_video"):
                return fail("缓存文件不是有效图片或视频", 400)
            path = str(info.get("absolute_path") or "")
            try:
                with open(path, "rb") as handle:
                    raw = handle.read(PAGE_PREVIEW_MAX_BYTES + 1)
            except Exception as exc:
                return fail(str(exc), 400)
            if len(raw) > PAGE_PREVIEW_MAX_BYTES:
                return fail("媒体文件过大，请改用下载查看", 413)
            mime = info.get("mime_type") or "application/octet-stream"
            return ok({
                "path": rel_path,
                "mime_type": mime,
                "size": len(raw),
                "data_url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}",
            })

        @app.route("/api/studio/sessions", methods=["GET", "POST"])
        def studio_sessions() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            if request.method == "GET":
                return ok(self.plugin.studio_list())
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.studio_create(payload or {}))
            except Exception as exc:
                return fail(str(exc))

        @app.route("/api/studio/sessions/<session_id>", methods=["GET", "POST"])
        def studio_session_detail(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            if request.method == "GET":
                try:
                    return ok(self.plugin.studio_get(session_id))
                except Exception as exc:
                    return fail(str(exc), 404)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.studio_update(session_id, payload or {}))
            except Exception as exc:
                return fail(str(exc))

        @app.route("/api/studio/sessions/<session_id>/delete", methods=["POST"])
        def studio_session_delete(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            _, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.studio_delete(session_id))
            except Exception as exc:
                return fail(str(exc), 404)

        @app.route("/api/studio/sessions/<session_id>/slots/<slot_id>", methods=["POST"])
        def studio_session_slot(session_id: str, slot_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.studio_set_slot(session_id, slot_id, payload or {}))
            except Exception as exc:
                return fail(str(exc))

        @app.route("/api/studio/sessions/<session_id>/slots", methods=["POST"])
        def studio_session_add_slot(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.studio_add_slot(session_id, payload or {}))
            except Exception as exc:
                return fail(str(exc))

        @app.route("/api/studio/sessions/<session_id>/reorder", methods=["POST"])
        def studio_session_reorder(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.studio_reorder(session_id, payload or {}))
            except Exception as exc:
                return fail(str(exc))

        @app.route("/api/studio/sessions/<session_id>/promote", methods=["POST"])
        def studio_session_promote(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.studio_promote(session_id, payload or {}))
            except Exception as exc:
                return fail(str(exc))

        @app.route("/api/studio/sessions/<session_id>/run", methods=["POST"])
        def studio_session_run(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(redact_sensitive_data(self.plugin.start_studio_run(session_id, payload or {})))
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/studio/tasks/<task_id>", methods=["GET"])
        def studio_task_status(task_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            task_id_text = str(task_id or "").strip()
            if len(task_id_text) > MAX_WEB_TASK_ID_LENGTH or not WEB_TASK_ID_RE.fullmatch(task_id_text):
                return fail("非法任务 ID", 400)
            try:
                return ok(redact_sensitive_data(self.plugin.get_web_image_task(task_id_text)))
            except Exception as exc:
                return fail(str(exc), 404)

        @app.route("/api/studio/gallery", methods=["GET"])
        def studio_gallery() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            try:
                limit = int(request.args.get("limit") or 24)
            except Exception:
                limit = 24
            try:
                return ok(self.plugin.studio_gallery_images(limit=limit))
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/prompt-presets", methods=["GET"])
        def prompt_presets() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            try:
                kind = request.args.get("kind") or request.args.get("media_type") or "image"
                data = self.plugin.list_prompt_presets_for_web(kind)
                status_getter = getattr(self.plugin, "get_prompt_preset_status_for_web", None)
                status = status_getter(kind) if callable(status_getter) else {"ok": True, "source": "legacy", "error": ""}
                return ok(data, count=len(data), load_status=status)
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/prompt-presets/manage", methods=["GET"])
        def prompt_presets_manage() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            try:
                kind = request.args.get("kind") or request.args.get("media_type") or "image"
                data = self.plugin.list_managed_prompt_presets_for_web(kind)
                status_getter = getattr(self.plugin, "get_prompt_preset_status_for_web", None)
                status = status_getter(kind) if callable(status_getter) else {"ok": True, "source": "legacy", "error": ""}
                return ok(data, load_status=status)
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/prompt-presets/manage/save", methods=["POST"])
        def prompt_preset_save() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.save_prompt_preset_from_web(payload or {}))
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/prompt-presets/manage/delete", methods=["POST"])
        def prompt_preset_delete() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            name = str((payload or {}).get("name") or "").strip()
            kind = str((payload or {}).get("kind") or (payload or {}).get("media_type") or "image")
            if not name:
                return fail("缺少预设名", 400)
            try:
                return ok(self.plugin.delete_prompt_preset_from_web(name, kind))
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/prompt-presets/manage/import", methods=["POST"])
        def prompt_presets_import() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.import_prompt_presets_from_web(payload or {}))
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/cos-look-sets", methods=["GET"])
        def cos_look_sets() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            try:
                return ok(self.plugin.list_cos_look_sets_for_web())
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/cos-pools", methods=["GET"])
        def cos_pools() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            try:
                return ok(self.plugin.list_cos_pools_for_web())
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/cos-pools/favorite", methods=["POST"])
        def cos_pool_favorite() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                look_id = str((payload or {}).get("id") or (payload or {}).get("look_id") or "").strip()
                enabled = (payload or {}).get("enabled", (payload or {}).get("favorite", True))
                if not isinstance(enabled, bool):
                    enabled = str(enabled).lower() in {"1", "true", "yes", "on", "是", "开启"}
                return ok(self.plugin.set_cos_favorite_for_web(look_id, enabled))
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/cos-pools/custom/save", methods=["POST"])
        def cos_custom_save() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.save_custom_cos_for_web(payload or {}))
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/cos-pools/custom/delete", methods=["POST"])
        def cos_custom_delete() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                look_id = str((payload or {}).get("id") or (payload or {}).get("look_id") or "").strip()
                return ok(self.plugin.delete_custom_cos_for_web(look_id))
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/cos-pools/export", methods=["GET"])
        def cos_pool_export() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            try:
                return ok(self.plugin.export_cos_pool_for_web())
            except Exception as exc:
                return fail(str(exc), 500)

        @app.route("/api/cos-pools/import", methods=["POST"])
        def cos_pool_import() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                return ok(self.plugin.import_cos_pool_for_web(payload or {}))
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/creative/template/render", methods=["POST"])
        def creative_template_render() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                prompt = str((payload or {}).get("prompt") or (payload or {}).get("template") or "")
                return ok(self.plugin.render_creative_prompt(prompt, payload or {}))
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/creative/variations", methods=["POST"])
        def creative_variations() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                prompt = str((payload or {}).get("prompt") or "")
                count = int((payload or {}).get("count") or 1)
                return ok({"variations": self.plugin.build_creative_variations(prompt, count, payload or {})})
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/creative/storyboard", methods=["POST"])
        def creative_storyboard() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            try:
                prompt = str((payload or {}).get("prompt") or (payload or {}).get("text") or "")
                return ok(self.plugin.parse_storyboard_for_web(prompt))
            except Exception as exc:
                return fail(str(exc), 400)

        @app.route("/api/records/compare", methods=["POST"])
        def records_compare() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            ids = (payload or {}).get("ids", (payload or {}).get("record_ids"))
            if not isinstance(ids, list):
                return fail("ids 必须是数组", 400)
            try:
                return ok(self.plugin.compare_records_for_web(ids, int((payload or {}).get("limit") or 8)))
            except Exception as exc:
                return fail(str(exc), 400)

        return app
