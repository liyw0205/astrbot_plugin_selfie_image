"""Embedded Flask Web UI for Selfie Image."""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import hmac
import json
from pathlib import Path
import threading
import time
from typing import Any, Optional
from astrbot.api import logger

from ..core.utils import (
    redact_sensitive_text,
)
from .contracts import (
    ASSET_COLLECTION_ROUTE_CONTRACTS,
    ASSET_ROUTE_CONTRACTS,
    CACHE_ROUTE_CONTRACTS,
    CANVAS_ROUTE_CONTRACTS,
    CONFIG_ROUTE_CONTRACTS,
    CONTEXT_ROUTE_CONTRACTS,
    HEALTH_ROUTE_CONTRACTS,
    IMAGE_CHANNEL_ROUTE_CONTRACTS,
    PROMPT_CATALOG_ROUTE_CONTRACTS,
    PROXY_ROUTE_CONTRACTS,
    QQ_GROUP_ROUTE_CONTRACTS,
    RECORD_ROUTE_CONTRACTS,
    SELFIE_PROFILE_ROUTE_CONTRACTS,
    TASK_ROUTE_CONTRACTS,
    WEB_TASK_ID_RE,
)
from .controllers import (
    AssetCollectionController,
    AssetController,
    AssetControllerResult,
    CacheController,
    CacheControllerResult,
    CanvasController,
    CanvasControllerResult,
    ConfigController,
    ConfigControllerResult,
    ContextController,
    ContextControllerResult,
    HealthController,
    HealthControllerResult,
    ImageChannelController,
    ImageChannelControllerResult,
    ProxyController,
    ProxyControllerResult,
    QQGroupController,
    QQGroupControllerResult,
    SelfieProfileController,
    SelfieProfileControllerResult,
    RecordController,
    RecordControllerResult,
    TaskController,
    TaskControllerResult,
)
from .prompt_controller import PromptCatalogController, PromptCatalogControllerResult
from .services import (
    WebContractError,
    build_health_payload,
    gallery_pagination,
    parse_bounded_int,
)


try:
    from flask import Flask, jsonify, request, send_file
    from werkzeug.serving import make_server
except Exception:  # pragma: no cover - handled at runtime in AstrBot env
    Flask = None  # type: ignore
    jsonify = None  # type: ignore
    request = None  # type: ignore
    send_file = None  # type: ignore
    make_server = None  # type: ignore


MAX_RECORD_PAGE_LIMIT = 1000
MAX_ASSET_PAGE_LIMIT = 100
MAX_TASK_PAGE_LIMIT = 200
MAX_TASK_OFFSET = 1_000_000
_INDEX_SOURCE_STATUS: dict[str, str] = {
    "source": "embedded",
    "path": "",
    "error": "",
}


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
    # Keep the logo as a cacheable asset instead of copying hundreds of KB into
    # every HTML response. The Flask route below serves it with a one-hour cache.
    return text.replace("__SELFIE_LOGO_SRC__", "logo.png")


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
            try:
                return future.result(timeout)
            except FutureTimeoutError:
                future.cancel()
                raise
        return asyncio.run(coro)

    def _create_app(self) -> Any:
        app = Flask("astrbot_plugin_selfie_image")
        app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
        asset_collection_controller = AssetCollectionController(self.plugin)
        asset_controller = AssetController(self.plugin)
        canvas_controller = CanvasController(self.plugin)
        config_controller = ConfigController(self.plugin)
        cache_controller = CacheController(self.plugin)
        proxy_controller = ProxyController(self.plugin)
        task_controller = TaskController(self.plugin)
        record_controller = RecordController(self.plugin)
        health_controller = HealthController(self.plugin)
        context_controller = ContextController(self.plugin)
        qq_group_controller = QQGroupController(self.plugin)
        selfie_profile_controller = SelfieProfileController(self.plugin)
        image_channel_controller = ImageChannelController(self.plugin)
        prompt_catalog_controller = PromptCatalogController(self.plugin)
        auth_failure_lock = threading.RLock()
        auth_failures: dict[str, dict[str, float]] = {}
        auth_failure_limit = 8
        auth_failure_window = 60.0
        auth_block_seconds = 60.0

        def ok(data: Any = None, **extra: Any) -> Any:
            payload = {"success": True, "data": data}
            payload.update(extra)
            return jsonify(payload)

        def fail(message: str, status: int = 400) -> Any:
            rate_limited = bool(getattr(request, "environ", {}).get("_selfie_auth_rate_limited"))
            if rate_limited and status == 401:
                response = jsonify({"success": False, "error": "请求过于频繁，请稍后重试"})
                response.headers["Retry-After"] = str(int(auth_block_seconds))
                return response, 429
            return jsonify({"success": False, "error": redact_sensitive_text(message)}), status

        def task_result(call: Any, *, include_message: bool = False) -> Any:
            try:
                result: TaskControllerResult = call()
            except WebContractError as exc:
                return fail(str(exc), exc.status_code)
            extra = {"message": result.message} if include_message else {}
            return ok(result.data, **extra)

        def config_result(call: Any) -> Any:
            try:
                result: ConfigControllerResult = call()
            except WebContractError as exc:
                return fail(str(exc), exc.status_code)
            except Exception as exc:
                return fail(str(exc), 500)
            return ok(result.data, **result.extra)

        def proxy_result(call: Any) -> Any:
            try:
                result: ProxyControllerResult = call()
            except WebContractError as exc:
                return fail(str(exc), exc.status_code)
            except Exception as exc:
                return fail(str(exc), 500)
            return ok(result.data, **result.extra)

        def asset_result(call: Any) -> Any:
            try:
                result: AssetControllerResult = call()
            except WebContractError as exc:
                return fail(str(exc), exc.status_code)
            except Exception as exc:
                return fail(str(exc), 500)
            return ok(result.data, **result.extra)

        def canvas_result(call: Any) -> Any:
            try:
                result: CanvasControllerResult = call()
            except WebContractError as exc:
                return fail(str(exc), exc.status_code)
            except Exception as exc:
                return fail(str(exc), 500)
            return ok(result.data, **result.extra)

        def prompt_catalog_result(
            call: Any, *, omit_extras: tuple[str, ...] = ("message",)
        ) -> Any:
            try:
                result: PromptCatalogControllerResult = call()
            except WebContractError as exc:
                return fail(str(exc), exc.status_code)
            except Exception as exc:
                return fail(str(exc), 500)
            extra = {
                key: value for key, value in result.extra.items()
                if key not in omit_extras
            }
            return ok(result.data, **extra)

        def cache_result(call: Any) -> Any:
            try:
                result: CacheControllerResult = call()
            except WebContractError as exc:
                return fail(str(exc), exc.status_code)
            except Exception as exc:
                return fail(str(exc), 500)
            return ok(result.data, **result.extra)

        def health_result(call: Any) -> Any:
            try:
                result: HealthControllerResult = call()
            except WebContractError as exc:
                return fail(str(exc), exc.status_code)
            except Exception as exc:
                return fail(str(exc), 500)
            return ok(result.data)

        def context_result(call: Any) -> Any:
            try:
                result: ContextControllerResult = call()
            except WebContractError as exc:
                return fail(str(exc), exc.status_code)
            except Exception as exc:
                return fail(str(exc), 500)
            return ok(result.data, **result.extra)

        def qq_group_result(call: Any) -> Any:
            try:
                result: QQGroupControllerResult = call()
            except WebContractError as exc:
                return fail(str(exc), exc.status_code)
            except Exception as exc:
                return fail(str(exc), 500)
            return ok(result.data)

        def selfie_profile_result(call: Any) -> Any:
            try:
                result: SelfieProfileControllerResult = call()
            except WebContractError as exc:
                return fail(str(exc), exc.status_code)
            except Exception as exc:
                return fail(str(exc), 500)
            return ok(result.data, **result.extra)

        def image_channel_result(call: Any) -> Any:
            try:
                result: ImageChannelControllerResult = call()
            except WebContractError as exc:
                return fail(str(exc), exc.status_code)
            except Exception as exc:
                return fail(str(exc), 500)
            return ok(result.data, **result.extra)

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

        def generation_task_status(task_id: str) -> Any:
            return task_result(lambda: task_controller.status(task_id))

        def studio_task_status(task_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return generation_task_status(task_id)

        def gallery_payload() -> Any:
            limit, offset = gallery_pagination(
                request.args.get("limit"), request.args.get("offset")
            )
            return canvas_result(lambda: canvas_controller.gallery(limit, offset))

        def record_result(call: Any) -> Any:
            try:
                result: RecordControllerResult = call()
            except WebContractError as exc:
                return fail(str(exc), exc.status_code)
            except Exception as exc:
                return fail(str(exc), 500)
            return ok(result.data, **result.extra)

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
                        source = str(request.remote_addr or "unknown")[:128]
                        with auth_failure_lock:
                            auth_failures.pop(source, None)
                        return True
                except Exception:
                    continue
            source = str(request.remote_addr or "unknown")[:128]
            now = time.monotonic()
            with auth_failure_lock:
                state = auth_failures.get(source)
                if state and float(state.get("blocked_until") or 0.0) > now:
                    request.environ["_selfie_auth_rate_limited"] = True
                    return False
                if not state or now - float(state["first_ts"]) >= auth_failure_window:
                    state = {"first_ts": now, "count": 0.0, "blocked_until": 0.0}
                    auth_failures[source] = state
                state["count"] = float(state.get("count") or 0.0) + 1.0
                if state["count"] >= auth_failure_limit:
                    state["blocked_until"] = now + auth_block_seconds
                    request.environ["_selfie_auth_rate_limited"] = True
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
            return ok(build_health_payload(self.plugin, dashboard_page_source_status()))

        def health_history() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return health_result(lambda: health_controller.history(request.args))

        health_handlers = {"health_history": health_history}
        for route in HEALTH_ROUTE_CONTRACTS:
            app.add_url_rule(
                route.flask_path,
                endpoint=route.endpoint,
                view_func=health_handlers[route.flask_handler],
                methods=list(route.methods),
            )

        def context_get() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return context_result(context_controller.get)

        def context_clear() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            _, error_response = json_object_payload()
            if error_response:
                return error_response
            return context_result(context_controller.clear)

        context_handlers = {
            "context_get": context_get,
            "context_clear": context_clear,
        }
        for route in CONTEXT_ROUTE_CONTRACTS:
            app.add_url_rule(
                route.flask_path,
                endpoint=route.endpoint,
                view_func=context_handlers[route.flask_handler],
                methods=list(route.methods),
            )

        def tasks() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return task_result(lambda: task_controller.list_tasks(request.args))

        def task_detail(task_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return task_result(lambda: task_controller.detail(task_id))

        def tasks_export() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return task_result(lambda: task_controller.export(request.args.get("ids")))

        def tasks_delete() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return task_result(lambda: task_controller.delete(payload), include_message=True)

        def tasks_retry() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return task_result(lambda: task_controller.retry(payload), include_message=True)

        def config_get() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return config_result(lambda: config_controller.get())

        def config_update() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return config_result(lambda: config_controller.update(payload))

        def config_export() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return config_result(
                lambda: config_controller.export(request.args.get("mode"))
            )

        def config_import_preview() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return config_result(
                lambda: config_controller.preview_import(payload)
            )

        def config_import() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return config_result(lambda: config_controller.import_config(payload))

        def proxies_list() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return proxy_result(lambda: proxy_controller.list())

        def proxies_test() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return proxy_result(
                lambda: self._run_async(proxy_controller.test(payload), timeout=30)
            )

        def proxies_quality() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return proxy_result(
                lambda: self._run_async(proxy_controller.quality(payload), timeout=90)
            )

        def assets() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return asset_result(lambda: asset_controller.list(request.args))

        def assets_tags() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return asset_result(lambda: asset_controller.tags())

        def assets_export() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return asset_result(
                lambda: asset_controller.export(request.args.get("ids"))
            )

        def assets_import() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return asset_result(lambda: asset_controller.import_metadata(payload))

        def assets_metadata() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return asset_result(lambda: asset_controller.update_metadata(payload))

        def assets_delete() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return asset_result(lambda: asset_controller.delete(payload))

        def asset_studio(record_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return asset_result(
                lambda: asset_controller.studio_one(record_id, payload)
            )

        def assets_studio() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return asset_result(lambda: asset_controller.studio_many(payload))

        def qq_groups() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return qq_group_result(
                lambda: self._run_async(qq_group_controller.list(request.args), timeout=20)
            )

        def qq_groups_config() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return qq_group_result(lambda: qq_group_controller.update(payload))

        qq_group_handlers = {
            "qq_groups": qq_groups,
            "qq_groups_config": qq_groups_config,
        }
        for route in QQ_GROUP_ROUTE_CONTRACTS:
            app.add_url_rule(
                route.flask_path,
                endpoint=route.endpoint,
                view_func=qq_group_handlers[route.flask_handler],
                methods=list(route.methods),
            )

        def selfie_reference_get() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return selfie_profile_result(selfie_profile_controller.get_reference)

        def selfie_reference_post() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return selfie_profile_result(
                lambda: selfie_profile_controller.save_reference(payload)
            )

        def selfie_reference_clear() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            _, error_response = json_object_payload()
            if error_response:
                return error_response
            return selfie_profile_result(selfie_profile_controller.clear_reference)

        def selfie_appearance_type() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return selfie_profile_result(
                lambda: selfie_profile_controller.set_appearance_type(payload)
            )

        def selfie_profile_refresh_local() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            _, error_response = json_object_payload()
            if error_response:
                return error_response
            return selfie_profile_result(lambda: self._run_async(
                selfie_profile_controller.refresh_local(), timeout=20,
            ))

        def selfie_wardrobe_get() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return selfie_profile_result(selfie_profile_controller.wardrobe_get)

        def selfie_wardrobe_mutation(method: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return selfie_profile_result(
                lambda: getattr(selfie_profile_controller, method)(payload)
            )

        selfie_profile_handlers = {
            "selfie_reference_get": selfie_reference_get,
            "selfie_reference_post": selfie_reference_post,
            "selfie_reference_clear": selfie_reference_clear,
            "selfie_appearance_type": selfie_appearance_type,
            "selfie_profile_refresh_local": selfie_profile_refresh_local,
            "selfie_wardrobe_get": selfie_wardrobe_get,
            "selfie_wardrobe_add": lambda: selfie_wardrobe_mutation("wardrobe_add"),
            "selfie_wardrobe_update": lambda: selfie_wardrobe_mutation("wardrobe_update"),
            "selfie_wardrobe_delete": lambda: selfie_wardrobe_mutation("wardrobe_delete"),
            "selfie_wardrobe_toggle": lambda: selfie_wardrobe_mutation("wardrobe_toggle"),
        }
        for route in SELFIE_PROFILE_ROUTE_CONTRACTS:
            app.add_url_rule(
                route.flask_path,
                endpoint=route.endpoint,
                view_func=selfie_profile_handlers[route.flask_handler],
                methods=list(route.methods),
            )

        def test_image_channel() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            timeout = max(30, self.plugin.config.image_global_timeout + 30)
            return image_channel_result(lambda: self._run_async(
                image_channel_controller.test(payload), timeout=timeout,
            ))

        def test_image_channel_task_start() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return image_channel_result(
                lambda: image_channel_controller.start_task(payload)
            )

        def test_image_channel_task_status(task_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return generation_task_status(task_id)

        def refresh_image_models() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return image_channel_result(lambda: self._run_async(
                image_channel_controller.refresh_models(payload), timeout=30,
            ))

        image_channel_handlers = {
            "test_image_channel": test_image_channel,
            "test_image_channel_task_start": test_image_channel_task_start,
            "test_image_channel_task_status": test_image_channel_task_status,
            "refresh_image_models": refresh_image_models,
        }
        for route in IMAGE_CHANNEL_ROUTE_CONTRACTS:
            app.add_url_rule(
                route.flask_path,
                endpoint=route.endpoint,
                view_func=image_channel_handlers[route.flask_handler],
                methods=list(route.methods),
            )

        def cancel_generation_task(task_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return task_result(lambda: task_controller.cancel(task_id), include_message=True)

        def records() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return record_result(lambda: record_controller.list(request.args))

        def metrics() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return record_result(lambda: record_controller.metrics(request.args.get("window")))

        def record_detail(record_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return record_result(lambda: record_controller.detail(record_id))

        def record_media_sources(record_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return record_result(lambda: record_controller.media_sources(record_id))

        def record_metadata(record_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return record_result(
                lambda: record_controller.update_metadata(record_id, payload)
            )

        def record_reuse(record_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return record_result(lambda: record_controller.reuse(record_id))

        def record_retry(record_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return record_result(
                lambda: record_controller.retry(record_id, payload)
            )

        def records_consistency() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return record_result(lambda: record_controller.consistency())

        def records_consistency_repair() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return record_result(
                lambda: record_controller.repair_consistency(payload)
            )

        def records_compare() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return record_result(lambda: record_controller.compare(payload))

        def records_clear() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            _, error_response = json_object_payload()
            if error_response:
                return error_response
            return record_result(lambda: record_controller.clear())

        task_handlers = {
            "tasks": tasks,
            "task_detail": task_detail,
            "tasks_export": tasks_export,
            "tasks_delete": tasks_delete,
            "tasks_retry": tasks_retry,
            "cancel_generation_task": cancel_generation_task,
            "task_status": studio_task_status,
        }
        for route in TASK_ROUTE_CONTRACTS:
            app.add_url_rule(
                route.flask_path,
                endpoint=route.endpoint,
                view_func=task_handlers[route.flask_handler],
                methods=list(route.methods),
            )

        config_handlers = {
            "config_get": config_get,
            "config_update": config_update,
            "config_export": config_export,
            "config_import_preview": config_import_preview,
            "config_import": config_import,
        }
        for route in CONFIG_ROUTE_CONTRACTS:
            app.add_url_rule(
                route.flask_path,
                endpoint=route.endpoint,
                view_func=config_handlers[route.flask_handler],
                methods=list(route.methods),
            )

        proxy_handlers = {
            "proxies_list": proxies_list,
            "proxies_test": proxies_test,
            "proxies_quality": proxies_quality,
        }
        for route in PROXY_ROUTE_CONTRACTS:
            app.add_url_rule(
                route.flask_path,
                endpoint=route.endpoint,
                view_func=proxy_handlers[route.flask_handler],
                methods=list(route.methods),
            )

        asset_handlers = {
            "assets": assets,
            "assets_tags": assets_tags,
            "assets_export": assets_export,
            "assets_import": assets_import,
            "assets_metadata": assets_metadata,
            "assets_delete": assets_delete,
            "asset_studio": asset_studio,
            "assets_studio": assets_studio,
        }
        for route in ASSET_ROUTE_CONTRACTS:
            app.add_url_rule(
                route.flask_path,
                endpoint=route.endpoint,
                view_func=asset_handlers[route.flask_handler],
                methods=list(route.methods),
            )

        record_handlers = {
            "records": records,
            "metrics": metrics,
            "record_detail": record_detail,
            "record_media_sources": record_media_sources,
            "record_metadata": record_metadata,
            "record_reuse": record_reuse,
            "record_retry": record_retry,
            "records_consistency": records_consistency,
            "records_consistency_repair": records_consistency_repair,
            "records_compare": records_compare,
            "records_clear": records_clear,
        }
        for route in RECORD_ROUTE_CONTRACTS:
            app.add_url_rule(
                route.flask_path,
                endpoint=route.endpoint,
                view_func=record_handlers[route.flask_handler],
                methods=list(route.methods),
            )

        def asset_collections() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return asset_result(lambda: asset_collection_controller.list())

        def asset_collection_create() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return asset_result(
                lambda: asset_collection_controller.create(payload)
            )

        def asset_collections_export() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return asset_result(lambda: asset_collection_controller.export())

        def asset_collections_import() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return asset_result(
                lambda: asset_collection_controller.import_data(payload)
            )

        def asset_collection_detail(collection_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return asset_result(
                lambda: asset_collection_controller.detail(collection_id)
            )

        def asset_collection_update(collection_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return asset_result(
                lambda: asset_collection_controller.update(collection_id, payload)
            )

        def asset_collection_delete(collection_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return asset_result(lambda: asset_collection_controller.delete(collection_id))

        def asset_collection_restore() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return asset_result(
                lambda: asset_collection_controller.restore(payload)
            )

        def asset_collection_studio(collection_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return asset_result(
                lambda: asset_collection_controller.studio(collection_id, payload)
            )

        def asset_collection_add(collection_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return asset_result(
                lambda: asset_collection_controller.update_records(
                    collection_id, payload, remove=False
                )
            )

        def asset_collection_remove(collection_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return asset_result(
                lambda: asset_collection_controller.update_records(
                    collection_id, payload, remove=True
                )
            )

        asset_collection_handlers = {
            "asset_collections": asset_collections,
            "asset_collection_create": asset_collection_create,
            "asset_collections_export": asset_collections_export,
            "asset_collections_import": asset_collections_import,
            "asset_collection_detail": asset_collection_detail,
            "asset_collection_update": asset_collection_update,
            "asset_collection_delete": asset_collection_delete,
            "asset_collection_restore": asset_collection_restore,
            "asset_collection_add": asset_collection_add,
            "asset_collection_remove": asset_collection_remove,
            "asset_collection_studio": asset_collection_studio,
        }
        for route in ASSET_COLLECTION_ROUTE_CONTRACTS:
            app.add_url_rule(
                route.flask_path,
                endpoint=route.endpoint,
                view_func=asset_collection_handlers[route.flask_handler],
                methods=list(route.methods),
            )

        def cache_cleanup() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return cache_result(lambda: cache_controller.cleanup(payload))

        def cache_image() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            try:
                cached = cache_controller.image_info(request.args.get("path"))
            except WebContractError as exc:
                return fail(str(exc), exc.status_code)
            return send_file(
                cached.info["absolute_path"],
                mimetype=cached.info.get("mime_type") or "application/octet-stream",
            )

        def cache_image_thumbnail() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return cache_result(
                lambda: cache_controller.thumbnail(request.args.get("path"))
            )

        def cache_image_preview() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return cache_result(lambda: cache_controller.preview(
                request.args.get("path"), fallback_mime="application/octet-stream",
            ))

        cache_handlers = {
            "cache_cleanup": cache_cleanup,
            "cache_image": cache_image,
            "cache_image_thumbnail": cache_image_thumbnail,
            "cache_image_preview": cache_image_preview,
        }
        for route in CACHE_ROUTE_CONTRACTS:
            app.add_url_rule(
                route.flask_path,
                endpoint=route.endpoint,
                view_func=cache_handlers[route.flask_handler],
                methods=list(route.methods),
            )

        def studio_sessions_list() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            include_metadata = str(request.args.get("metadata") or "").strip().lower() in {"1", "true", "yes", "all"}
            return canvas_result(
                lambda: canvas_controller.studio_list(include_metadata=include_metadata)
            )

        def studio_sessions_create() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(lambda: canvas_controller.studio_create(payload))

        def studio_session_copy(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.studio_copy(session_id, payload)
            )

        def studio_session_get(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return canvas_result(lambda: canvas_controller.studio_get(session_id))

        def studio_session_update(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.studio_update(session_id, payload)
            )

        def studio_session_canvas_get(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return canvas_result(lambda: canvas_controller.studio_canvas(session_id))

        def studio_session_canvas_update(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.studio_canvas_update(session_id, payload)
            )

        def studio_canvas_node_create(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.studio_canvas_node_create(session_id, payload)
            )

        def studio_canvas_node_delete(session_id: str, node_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return canvas_result(
                lambda: canvas_controller.studio_canvas_node_delete(session_id, node_id)
            )

        def studio_canvas_node_connect(session_id: str, node_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.studio_canvas_node_connect(
                    session_id, node_id, payload
                )
            )

        def studio_session_delete(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            _, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(lambda: canvas_controller.studio_delete(session_id))

        def studio_session_slot(session_id: str, slot_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.studio_set_slot(session_id, slot_id, payload)
            )

        def studio_session_add_slot(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.studio_add_slot(session_id, payload)
            )

        def studio_session_reorder(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.studio_reorder(session_id, payload)
            )

        def studio_session_promote(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.studio_promote(session_id, payload)
            )

        def studio_session_run(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.studio_run(session_id, payload)
            )

        def studio_gallery() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return gallery_payload()

        def creative_canvas_sessions_list() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            include_metadata = str(request.args.get("metadata") or "").strip().lower() in {"1", "true", "yes", "all"}
            return canvas_result(
                lambda: canvas_controller.creative_canvas_list(
                    include_metadata=include_metadata
                )
            )

        def creative_canvas_sessions_create() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.creative_canvas_create(payload)
            )

        def creative_canvas_session_get(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return canvas_result(
                lambda: canvas_controller.creative_canvas_get(session_id)
            )

        def creative_canvas_session_update(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.creative_canvas_update(session_id, payload)
            )

        def creative_canvas_session_delete(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            _, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.creative_canvas_delete(session_id)
            )

        def creative_canvas_slot(session_id: str, slot_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.creative_canvas_set_slot(
                    session_id, slot_id, payload
                )
            )

        def creative_canvas_add_slot(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.creative_canvas_add_slot(session_id, payload)
            )

        def creative_canvas_reorder(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.creative_canvas_reorder(session_id, payload)
            )

        def creative_canvas_promote(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.creative_canvas_promote(session_id, payload)
            )

        def creative_canvas_run(session_id: str) -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return canvas_result(
                lambda: canvas_controller.creative_canvas_run(session_id, payload)
            )

        def creative_canvas_gallery() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return gallery_payload()

        canvas_handlers = {
            "studio_sessions_list": studio_sessions_list,
            "studio_sessions_create": studio_sessions_create,
            "studio_session_copy": studio_session_copy,
            "studio_session_get": studio_session_get,
            "studio_session_update": studio_session_update,
            "studio_session_canvas_get": studio_session_canvas_get,
            "studio_session_canvas_update": studio_session_canvas_update,
            "studio_canvas_node_create": studio_canvas_node_create,
            "studio_canvas_node_delete": studio_canvas_node_delete,
            "studio_canvas_node_connect": studio_canvas_node_connect,
            "studio_session_delete": studio_session_delete,
            "studio_session_slot": studio_session_slot,
            "studio_session_add_slot": studio_session_add_slot,
            "studio_session_reorder": studio_session_reorder,
            "studio_session_promote": studio_session_promote,
            "studio_session_run": studio_session_run,
            "creative_canvas_sessions_list": creative_canvas_sessions_list,
            "creative_canvas_sessions_create": creative_canvas_sessions_create,
            "creative_canvas_session_get": creative_canvas_session_get,
            "creative_canvas_session_update": creative_canvas_session_update,
            "creative_canvas_session_delete": creative_canvas_session_delete,
            "creative_canvas_session_slot": creative_canvas_slot,
            "creative_canvas_session_add_slot": creative_canvas_add_slot,
            "creative_canvas_session_reorder": creative_canvas_reorder,
            "creative_canvas_session_promote": creative_canvas_promote,
            "creative_canvas_session_run": creative_canvas_run,
            "studio_gallery": studio_gallery,
            "creative_canvas_gallery": creative_canvas_gallery,
        }
        for route in CANVAS_ROUTE_CONTRACTS:
            app.add_url_rule(
                route.flask_path,
                endpoint=route.endpoint,
                view_func=canvas_handlers[route.flask_handler],
                methods=list(route.methods),
            )

        def prompt_presets() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return prompt_catalog_result(
                lambda: prompt_catalog_controller.list_presets(request.args)
            )

        def prompt_presets_manage() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return prompt_catalog_result(
                lambda: prompt_catalog_controller.list_presets(request.args, managed=True)
            )

        def prompt_preset_save() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return prompt_catalog_result(
                lambda: prompt_catalog_controller.save_preset(payload)
            )

        def prompt_preset_delete() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return prompt_catalog_result(
                lambda: prompt_catalog_controller.delete_preset(payload)
            )

        def prompt_presets_import() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return prompt_catalog_result(
                lambda: prompt_catalog_controller.import_presets(payload)
            )

        def cos_look_sets() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return prompt_catalog_result(
                prompt_catalog_controller.list_cos_look_sets,
                omit_extras=("message", "count"),
            )

        def cos_pools() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return prompt_catalog_result(
                lambda: prompt_catalog_controller.list_cos_pools(request.args)
            )

        def cos_pool_favorite() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return prompt_catalog_result(
                lambda: prompt_catalog_controller.favorite_cos(payload)
            )

        def cos_custom_save() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return prompt_catalog_result(
                lambda: prompt_catalog_controller.save_custom_cos(payload)
            )

        def cos_custom_delete() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return prompt_catalog_result(
                lambda: prompt_catalog_controller.delete_custom_cos(payload)
            )

        def cos_pool_export() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            return prompt_catalog_result(prompt_catalog_controller.export_cos)

        def cos_pool_import() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return prompt_catalog_result(
                lambda: prompt_catalog_controller.import_cos(payload)
            )

        def creative_template_render() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return prompt_catalog_result(
                lambda: prompt_catalog_controller.render_template(payload)
            )

        def creative_variations() -> Any:
            if not check_auth():
                return fail("Unauthorized: Token 不正确", 401)
            payload, error_response = json_object_payload()
            if error_response:
                return error_response
            return prompt_catalog_result(
                lambda: prompt_catalog_controller.variations(payload)
            )

        prompt_handlers = {
            "prompt_presets": prompt_presets,
            "prompt_presets_manage": prompt_presets_manage,
            "prompt_preset_save": prompt_preset_save,
            "prompt_preset_delete": prompt_preset_delete,
            "prompt_presets_import": prompt_presets_import,
            "cos_look_sets": cos_look_sets,
            "cos_pools": cos_pools,
            "cos_pool_favorite": cos_pool_favorite,
            "cos_custom_save": cos_custom_save,
            "cos_custom_delete": cos_custom_delete,
            "cos_pool_export": cos_pool_export,
            "cos_pool_import": cos_pool_import,
            "creative_template_render": creative_template_render,
            "creative_variations": creative_variations,
        }
        for route in PROMPT_CATALOG_ROUTE_CONTRACTS:
            app.add_url_rule(
                route.flask_path,
                endpoint=route.endpoint,
                view_func=prompt_handlers[route.flask_handler],
                methods=list(route.methods),
            )

        return app
