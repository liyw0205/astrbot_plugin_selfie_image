"""Channel proxy parsing and aiohttp session selection."""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator, Dict, Optional
from urllib.parse import quote, unquote, urlparse

import aiohttp


LOCAL_IMAGE_WAIT_SECONDS = 180
# A successful image-generation response may point at a separate CDN.  Do not
# spend the whole generation budget waiting for that second request.
IMAGE_DOWNLOAD_WAIT_SECONDS = 30


def image_client_timeout(seconds: Optional[int] = None) -> aiohttp.ClientTimeout:
    """Bound one upstream image request without truncating its global budget."""
    requested = LOCAL_IMAGE_WAIT_SECONDS if seconds is None else int(seconds)
    total = max(20, requested)
    return aiohttp.ClientTimeout(total=total, connect=10, sock_connect=10, sock_read=total)


def image_download_timeout(seconds: Optional[int] = None) -> aiohttp.ClientTimeout:
    """Bound result-URL downloads independently from model generation."""
    requested = int(seconds or IMAGE_DOWNLOAD_WAIT_SECONDS)
    total = max(5, min(IMAGE_DOWNLOAD_WAIT_SECONDS, requested))
    return aiohttp.ClientTimeout(total=total, connect=min(10, total), sock_connect=min(10, total), sock_read=total)


@dataclass(frozen=True)
class ChannelProxy:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def has_auth(self) -> bool:
        return bool(self.username)

    @property
    def is_socks(self) -> bool:
        return self.scheme in {"socks5", "socks5h"}

    @property
    def url(self) -> str:
        auth = ""
        if self.has_auth:
            auth = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"{self.scheme}://{auth}{host}:{self.port}"


class ClientSessionRegistry:
    """Reuse image HTTP transports while keeping proxy connectors isolated.

    HTTP proxy URLs are request-level aiohttp options, but they still get a
    separate session key here so connector pools are never shared across
    proxy identities. SOCKS URLs require a connector-level route and therefore
    always receive their own ``ProxyConnector`` session. The registry owns all
    sessions and is closed with the plugin lifecycle.
    """

    DEFAULT_IDLE_TIMEOUT_SECONDS = 15 * 60

    def __init__(self, idle_timeout_seconds: Optional[float] = DEFAULT_IDLE_TIMEOUT_SECONDS) -> None:
        self._sessions: Dict[tuple[str, str], aiohttp.ClientSession] = {}
        self._last_used: Dict[tuple[str, str], float] = {}
        self._closed = False
        self._idle_timeout_seconds = (
            None
            if idle_timeout_seconds is None
            else max(0.0, float(idle_timeout_seconds))
        )
        self._state_lock = asyncio.Lock()
        # Health is read by synchronous Flask handlers while aiohttp updates
        # counters on the event loop.  Keep the snapshot path independent of
        # the asyncio lock so cross-thread reads cannot observe partial state.
        self._health_lock = threading.RLock()
        self._stats: Dict[str, Any] = {
            "lookups": 0,
            "hits": 0,
            "misses": 0,
            "created": 0,
            "recycled": 0,
            "closed": 0,
            "close_failures": 0,
            "failures": 0,
            "failure_reasons": {},
            "requests": 0,
            "connection_created": 0,
            "connection_reused": 0,
            "request_failures": 0,
            "requests_by_transport": {"direct": 0, "http": 0, "socks": 0},
            "connection_created_by_transport": {"direct": 0, "http": 0, "socks": 0},
            "connection_reused_by_transport": {"direct": 0, "http": 0, "socks": 0},
            "request_failures_by_transport": {"direct": 0, "http": 0, "socks": 0},
        }

    @staticmethod
    def _connector_is_idle(session: aiohttp.ClientSession) -> bool:
        """Avoid reaping a session while aiohttp has an acquired connection."""
        connector = getattr(session, "connector", None)
        acquired = getattr(connector, "_acquired", None)
        # aiohttp connectors expose this private set across supported versions.
        # If a replacement connector does not, retain the session conservatively.
        return acquired is not None and not acquired

    async def _reap_idle_sessions(self, idle_timeout_seconds: Optional[float] = None) -> int:
        with self._health_lock:
            if self._closed:
                return 0
        timeout = self._idle_timeout_seconds if idle_timeout_seconds is None else max(0.0, float(idle_timeout_seconds))
        if timeout is None:
            return 0
        now = time.monotonic()
        stale = []
        with self._health_lock:
            for key, session in list(self._sessions.items()):
                last_used = self._last_used.get(key, now)
                if not session.closed and now - last_used >= timeout and self._connector_is_idle(session):
                    stale.append((key, session))
        for key, session in stale:
            with self._health_lock:
                if self._sessions.get(key) is not session:
                    continue
                self._sessions.pop(key, None)
                self._last_used.pop(key, None)
                self._stats["recycled"] += 1
                self._stats["closed"] += 1
            try:
                await session.close()
            except Exception as exc:
                with self._health_lock:
                    self._stats["close_failures"] += 1
                self._record_failure(exc)
        return len(stale)

    async def reap_idle_sessions(self, idle_timeout_seconds: Optional[float] = None) -> int:
        """Lazily close idle pools without interrupting active requests."""
        async with self._state_lock:
            return await self._reap_idle_sessions(idle_timeout_seconds)

    @staticmethod
    def _failure_reason(exc: BaseException) -> str:
        """Return a bounded category without retaining proxy or upstream details."""
        if isinstance(exc, ValueError):
            return "invalid_proxy"
        if isinstance(exc, ImportError):
            return "dependency_missing"
        if isinstance(exc, RuntimeError) and "registry is closed" in str(exc).lower():
            return "registry_closed"
        return type(exc).__name__

    def _record_failure(self, exc: BaseException) -> None:
        with self._health_lock:
            self._stats["failures"] += 1
            reasons = self._stats["failure_reasons"]
            reason = self._failure_reason(exc)
            # A malformed upstream error must not create an unbounded health map.
            if reason not in reasons and len(reasons) >= 16:
                reason = "OtherError"
                if reason not in reasons:
                    oldest = next(iter(reasons), None)
                    if oldest is not None:
                        del reasons[oldest]
            reasons[reason] = int(reasons.get(reason, 0)) + 1

    def _record_transport_metric(self, transport: str, metric: str) -> None:
        """Record a bounded request/connection event from aiohttp tracing."""
        if metric not in {
            "requests", "connection_created", "connection_reused", "request_failures",
        }:
            return
        transport_key = transport if transport in {"direct", "http", "socks"} else "direct"
        with self._health_lock:
            self._stats[metric] += 1
            by_transport = self._stats[f"{metric}_by_transport"]
            by_transport[transport_key] = int(by_transport.get(transport_key, 0)) + 1

    def _trace_config(self, transport: str) -> aiohttp.TraceConfig:
        """Build request tracing bound to one registry transport identity."""
        trace = aiohttp.TraceConfig()

        async def request_start(_session, _ctx, _params):
            self._record_transport_metric(transport, "requests")

        async def connection_created(_session, _ctx, _params):
            self._record_transport_metric(transport, "connection_created")

        async def connection_reused(_session, _ctx, _params):
            self._record_transport_metric(transport, "connection_reused")

        async def request_end(_session, _ctx, params):
            response = getattr(params, "response", None)
            try:
                failed = response is not None and int(getattr(response, "status", 0)) >= 400
            except (TypeError, ValueError):
                failed = False
            if failed:
                self._record_transport_metric(transport, "request_failures")

        async def request_exception(_session, _ctx, _params):
            self._record_transport_metric(transport, "request_failures")

        trace.on_request_start.append(request_start)
        trace.on_connection_create_end.append(connection_created)
        trace.on_connection_reuseconn.append(connection_reused)
        trace.on_request_end.append(request_end)
        trace.on_request_exception.append(request_exception)
        return trace

    @staticmethod
    def _connector_pool_counts(session: aiohttp.ClientSession) -> tuple[int, int]:
        """Read connector pool sizes without depending on one aiohttp version.

        ``_acquired`` and ``_conns`` are private aiohttp attributes, but their
        shape has remained stable across the supported releases.  Health
        reporting must degrade to zero when a replacement connector does not
        expose them; it must never affect request handling.
        """
        connector = getattr(session, "connector", None)
        if connector is None:
            return 0, 0
        try:
            active = len(getattr(connector, "_acquired", ()) or ())
        except (TypeError, AttributeError, RuntimeError):
            active = 0
        idle = 0
        pools = getattr(connector, "_conns", {}) or {}
        try:
            # Copy the values first: a response can return a connection to the
            # pool while a synchronous health endpoint is taking its snapshot.
            values = list(pools.values())
        except (AttributeError, RuntimeError):
            values = ()
        for entries in values:
            try:
                idle += len(entries)
            except (TypeError, AttributeError, RuntimeError):
                continue
        return int(active), int(idle)

    def get_stats(self) -> Dict[str, Any]:
        """Return bounded, credential-free session-pool health counters."""
        with self._health_lock:
            active_by_transport = {"direct": 0, "http": 0, "socks": 0}
            active_connections_by_transport = {"direct": 0, "http": 0, "socks": 0}
            idle_connections_by_transport = {"direct": 0, "http": 0, "socks": 0}
            for key, session in list(self._sessions.items()):
                if session.closed:
                    continue
                transport = key[0] if key and key[0] in active_by_transport else "direct"
                active_by_transport[transport] += 1
                active_connections, idle_connections = self._connector_pool_counts(session)
                active_connections_by_transport[transport] += active_connections
                idle_connections_by_transport[transport] += idle_connections
            active_sessions = sum(active_by_transport.values())
            active_connections = sum(active_connections_by_transport.values())
            idle_connections = sum(idle_connections_by_transport.values())
            return {
                "status": "closed" if self._closed else "ok",
                "registry_closed": self._closed,
                "idle_timeout_seconds": self._idle_timeout_seconds,
                "lookups": int(self._stats["lookups"]),
                "hits": int(self._stats["hits"]),
                "reuse_hits": int(self._stats["hits"]),
                "misses": int(self._stats["misses"]),
                "created": int(self._stats["created"]),
                "recycled": int(self._stats["recycled"]),
                "closed": int(self._stats["closed"]),
                "close_failures": int(self._stats["close_failures"]),
                "failures": int(self._stats["failures"]),
                "failure_reasons": dict(self._stats["failure_reasons"]),
                "active_sessions": active_sessions,
                "active_by_transport": active_by_transport,
                # Session count only describes registry ownership.  These pool
                # counters expose whether keep-alive sockets are actually active
                # or available for reuse.
                "active_connections": active_connections,
                "idle_connections": idle_connections,
                "pool_connections": active_connections + idle_connections,
                "active_connections_by_transport": active_connections_by_transport,
                "idle_connections_by_transport": idle_connections_by_transport,
                "requests": int(self._stats["requests"]),
                "connection_created": int(self._stats["connection_created"]),
                "connection_reused": int(self._stats["connection_reused"]),
                "request_failures": int(self._stats["request_failures"]),
                "requests_by_transport": dict(self._stats["requests_by_transport"]),
                "connection_created_by_transport": dict(self._stats["connection_created_by_transport"]),
                "connection_reused_by_transport": dict(self._stats["connection_reused_by_transport"]),
                "request_failures_by_transport": dict(self._stats["request_failures_by_transport"]),
            }

    @staticmethod
    def _session_key(proxy_value: str) -> tuple[str, str]:
        proxy = parse_channel_proxy(proxy_value)
        if not proxy:
            return ("direct", "")
        if proxy.is_socks:
            return ("socks", proxy.url)
        return ("http", proxy.url)

    async def get_session(
        self,
        proxy_value: str = "",
        timeout_seconds: Optional[int] = None,
    ) -> aiohttp.ClientSession:
        """Return a persistent session for one proxy identity.

        Session construction is intentionally synchronous from the event
        loop's perspective; aiohttp opens sockets lazily on the first
        request, so concurrent callers can safely share the resulting object.
        """
        with self._health_lock:
            self._stats["lookups"] += 1
        async with self._state_lock:
            with self._health_lock:
                if self._closed:
                    error = RuntimeError("HTTP session registry is closed")
                    self._record_failure(error)
                    raise error
            await self._reap_idle_sessions()
            try:
                key = self._session_key(proxy_value)
            except Exception as exc:
                with self._health_lock:
                    self._stats["misses"] += 1
                self._record_failure(exc)
                raise
            with self._health_lock:
                session = self._sessions.get(key)
                if session is not None and not session.closed:
                    self._stats["hits"] += 1
                    self._last_used[key] = time.monotonic()
                    return session
                self._stats["misses"] += 1
                if session is not None:
                    self._stats["recycled"] += 1
                    self._sessions.pop(key, None)
                    self._last_used.pop(key, None)

            try:
                proxy = parse_channel_proxy(proxy_value)
                kwargs: Dict[str, Any] = {
                    "trust_env": False,
                    "cookie_jar": aiohttp.DummyCookieJar(),
                    "trace_configs": [self._trace_config(key[0])],
                }
                if timeout_seconds is not None:
                    kwargs["timeout"] = image_client_timeout(timeout_seconds)
                if proxy and proxy.is_socks:
                    try:
                        from aiohttp_socks import ProxyConnector
                    except ImportError as exc:
                        raise RuntimeError("SOCKS5 代理需要安装 aiohttp-socks") from exc
                    kwargs["connector"] = ProxyConnector.from_url(proxy.url)
                session = aiohttp.ClientSession(**kwargs)
            except Exception as exc:
                self._record_failure(exc)
                raise
            with self._health_lock:
                self._sessions[key] = session
                self._last_used[key] = time.monotonic()
                self._stats["created"] += 1
            return session

    async def close(self) -> None:
        """Close every owned session and make the registry unusable."""
        async with self._state_lock:
            with self._health_lock:
                sessions = list(self._sessions.values())
                self._sessions.clear()
                self._last_used.clear()
                self._closed = True
                open_sessions = [session for session in sessions if not session.closed]
                self._stats["closed"] += len(open_sessions)
            if open_sessions:
                results = await asyncio.gather(
                    *(session.close() for session in open_sessions),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, BaseException):
                        with self._health_lock:
                            self._stats["close_failures"] += 1
                        self._record_failure(result)


def parse_channel_proxy(value: str) -> Optional[ChannelProxy]:
    """Parse an HTTP or SOCKS5 channel proxy without accepting partial auth."""
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError("代理协议必须是 http、https、socks5 或 socks5h")
    if not parsed.hostname:
        raise ValueError("代理 URL 必须包含主机")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理端口必须是数字") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValueError("代理 URL 必须包含 1 到 65535 的端口")
    username = unquote(parsed.username) if parsed.username is not None else ""
    password = unquote(parsed.password) if parsed.password is not None else ""
    if bool(username) != bool(password):
        raise ValueError("代理用户名和密码必须同时填写")
    return ChannelProxy(scheme=scheme, host=parsed.hostname, port=port, username=username, password=password)


def http_proxy_url(value: str) -> Optional[str]:
    proxy = parse_channel_proxy(value)
    return proxy.url if proxy and not proxy.is_socks else None


def target_session_proxy(target):
    """Return a target without proxy= for SOCKS connector requests."""
    proxy = parse_channel_proxy(str(getattr(target, "proxy", "") or ""))
    if not proxy or not proxy.is_socks:
        return target
    extra = dict(getattr(target, "extra", {}) or {})
    extra["_socks_proxy"] = proxy.url
    return replace(target, proxy="", extra=extra)


@asynccontextmanager
async def channel_client_session(
    proxy_value: str,
    fallback: aiohttp.ClientSession,
    timeout_seconds: Optional[int] = None,
) -> AsyncIterator[aiohttp.ClientSession]:
    """Use a dedicated SOCKS connector only when the channel requires one."""
    proxy = parse_channel_proxy(proxy_value)
    if not proxy or not proxy.is_socks:
        yield fallback
        return
    try:
        from aiohttp_socks import ProxyConnector
    except ImportError as exc:
        raise RuntimeError("SOCKS5 代理需要安装 aiohttp-socks") from exc
    connector = ProxyConnector.from_url(proxy.url)
    async with aiohttp.ClientSession(
        connector=connector,
        trust_env=False,
        timeout=image_client_timeout(timeout_seconds),
    ) as session:
        yield session


def build_proxy_url(
    protocol: str,
    host: str,
    port: int,
    username: str = "",
    password: str = "",
) -> str:
    """Build a channel proxy URL from protocol/host/port/auth fields."""
    scheme = str(protocol or "http").strip().lower()
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError("代理协议必须是 http、https、socks5 或 socks5h")
    host_text = str(host or "").strip()
    if not host_text:
        raise ValueError("代理主机不能为空")
    try:
        port_num = int(port)
    except Exception as exc:
        raise ValueError("代理端口必须是数字") from exc
    if not 1 <= port_num <= 65535:
        raise ValueError("代理端口必须在 1–65535")
    user = str(username or "").strip()
    pwd = str(password or "").strip()
    if bool(user) != bool(pwd):
        raise ValueError("代理用户名和密码必须同时填写")
    return ChannelProxy(scheme=scheme, host=host_text, port=port_num, username=user, password=pwd).url


# Channel-type default bases for quality checks (no API key required).
PROXY_QUALITY_TARGETS = [
    {"id": "openai", "label": "OpenAI", "url": "https://api.openai.com"},
    {"id": "gemini", "label": "Gemini", "url": "https://generativelanguage.googleapis.com"},
    {"id": "grok", "label": "Grok", "url": "https://api.x.ai"},
    {"id": "agnes", "label": "Agnes", "url": "https://apihub.agnes-ai.com"},
    {"id": "gitee", "label": "Gitee AI", "url": "https://ai.gitee.com"},
    {"id": "novelai", "label": "NovelAI", "url": "https://image.novelai.net"},
]


def _format_location(city: str = "", region: str = "", country: str = "", country_code: str = "") -> str:
    parts = [str(x or "").strip() for x in (city, region, country)]
    parts = [p for p in parts if p]
    text = " · ".join(parts)
    code = str(country_code or "").strip().upper()
    if code and code not in text:
        text = f"{text} ({code})" if text else code
    return text


def _parse_ip_geo_payload(data: Any) -> Dict[str, str]:
    if not isinstance(data, dict):
        return {}
    # ipapi.co style
    if data.get("ip") or data.get("country_name") or data.get("country_code"):
        if data.get("error"):
            return {}
        return {
            "ip_address": str(data.get("ip") or "").strip(),
            "city": str(data.get("city") or "").strip(),
            "region": str(data.get("region") or data.get("region_code") or "").strip(),
            "country": str(data.get("country_name") or data.get("country") or "").strip(),
            "country_code": str(data.get("country_code") or "").strip().upper(),
            "org": str(data.get("org") or data.get("asn") or "").strip(),
        }
    # ip-api.com style
    if str(data.get("status") or "").lower() == "success" or data.get("query"):
        if str(data.get("status") or "").lower() == "fail":
            return {}
        return {
            "ip_address": str(data.get("query") or data.get("ip") or "").strip(),
            "city": str(data.get("city") or "").strip(),
            "region": str(data.get("regionName") or data.get("region") or "").strip(),
            "country": str(data.get("country") or "").strip(),
            "country_code": str(data.get("countryCode") or "").strip().upper(),
            "org": str(data.get("isp") or data.get("org") or "").strip(),
        }
    return {}


async def _request_via_proxy(
    proxy: ChannelProxy,
    method: str,
    url: str,
    *,
    timeout: float,
    headers: Optional[Dict[str, str]] = None,
) -> tuple[int, str, bytes]:
    """Return status, text, raw body through the proxy."""
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    hdrs = headers or {}
    if proxy.is_socks:
        try:
            from aiohttp_socks import ProxyConnector
        except ImportError as exc:
            raise RuntimeError("SOCKS5 代理需要安装 aiohttp-socks") from exc
        connector = ProxyConnector.from_url(proxy.url)
        async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
            async with session.request(method, url, timeout=client_timeout, headers=hdrs, allow_redirects=True) as resp:
                raw = await resp.read()
                try:
                    text = raw.decode(resp.charset or "utf-8", errors="replace")
                except Exception:
                    text = raw.decode("utf-8", errors="replace")
                return int(resp.status), text, raw
    async with aiohttp.ClientSession(trust_env=False) as session:
        async with session.request(
            method,
            url,
            proxy=proxy.url,
            timeout=client_timeout,
            headers=hdrs,
            allow_redirects=True,
        ) as resp:
            raw = await resp.read()
            try:
                text = raw.decode(resp.charset or "utf-8", errors="replace")
            except Exception:
                text = raw.decode("utf-8", errors="replace")
            return int(resp.status), text, raw


async def _lookup_exit_ip_geo(proxy: ChannelProxy, timeout: float = 10.0) -> Dict[str, Any]:
    """Resolve exit IP + geo via public endpoints (no key)."""
    import json as _json

    attempts = [
        ("https://ipapi.co/json/", None),
        ("http://ip-api.com/json/?fields=status,message,country,countryCode,region,regionName,city,query,isp,org", None),
        ("https://api.ipify.org?format=json", "ip_only"),
    ]
    last_error = ""
    ip_only = ""
    for url, mode in attempts:
        try:
            status, text, _raw = await _request_via_proxy(proxy, "GET", url, timeout=timeout)
            if status >= 400:
                last_error = f"HTTP {status}"
                continue
            try:
                data = _json.loads(text)
            except Exception:
                data = {"ip": text.strip()} if mode == "ip_only" else {}
            if mode == "ip_only":
                ip_only = str((data or {}).get("ip") or text.strip())[:64]
                continue
            geo = _parse_ip_geo_payload(data)
            if geo.get("ip_address"):
                geo["http_status"] = status
                geo["location"] = _format_location(
                    geo.get("city", ""),
                    geo.get("region", ""),
                    geo.get("country", ""),
                    geo.get("country_code", ""),
                )
                return geo
            if geo.get("city") or geo.get("country"):
                if not geo.get("ip_address") and ip_only:
                    geo["ip_address"] = ip_only
                geo["http_status"] = status
                geo["location"] = _format_location(
                    geo.get("city", ""),
                    geo.get("region", ""),
                    geo.get("country", ""),
                    geo.get("country_code", ""),
                )
                return geo
        except Exception as exc:
            last_error = str(exc)[:120]
            continue
    if ip_only:
        return {
            "ip_address": ip_only,
            "city": "",
            "region": "",
            "country": "",
            "country_code": "",
            "org": "",
            "location": "",
            "http_status": 200,
            "message": last_error or "",
        }
    return {"message": last_error or "无法解析出口 IP"}


async def probe_proxy_connectivity(proxy_value: str, timeout: float = 12.0) -> dict:
    """Connectivity test: exit IP + geography via proxy."""
    import asyncio
    import time

    raw = str(proxy_value or "").strip()
    if not raw:
        return {"success": False, "message": "未配置代理"}
    try:
        proxy = parse_channel_proxy(raw)
    except ValueError as exc:
        return {"success": False, "message": str(exc)}
    assert proxy is not None
    started = time.monotonic()
    try:
        geo = await _lookup_exit_ip_geo(proxy, timeout=timeout)
        latency = round((time.monotonic() - started) * 1000)
        ip = str(geo.get("ip_address") or "").strip()
        if not ip:
            return {
                "success": False,
                "message": str(geo.get("message") or "连通失败"),
                "latency_ms": latency,
                "http_status": geo.get("http_status"),
            }
        location = str(geo.get("location") or "").strip()
        org = str(geo.get("org") or "").strip()
        msg_parts = ["连通正常"]
        if location:
            msg_parts.append(location)
        if org:
            msg_parts.append(org)
        return {
            "success": True,
            "message": " · ".join(msg_parts),
            "latency_ms": latency,
            "http_status": int(geo.get("http_status") or 200),
            "ip_address": ip,
            "city": str(geo.get("city") or ""),
            "region": str(geo.get("region") or ""),
            "country": str(geo.get("country") or ""),
            "country_code": str(geo.get("country_code") or ""),
            "org": org,
            "location": location,
        }
    except asyncio.TimeoutError:
        return {
            "success": False,
            "message": f"连通超时（{int(timeout)}s）",
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        return {
            "success": False,
            "message": str(exc)[:200],
            "latency_ms": round((time.monotonic() - started) * 1000),
        }


async def probe_proxy_quality(proxy_value: str, timeout_each: float = 10.0) -> dict:
    """Quality test against channel-type default base URLs; keep real HTTP status codes."""
    import asyncio
    import time

    raw = str(proxy_value or "").strip()
    if not raw:
        return {"success": False, "message": "未配置代理", "results": []}
    try:
        proxy = parse_channel_proxy(raw)
    except ValueError as exc:
        return {"success": False, "message": str(exc), "results": []}
    assert proxy is not None

    # Exit IP once for the summary card.
    exit_info: Dict[str, Any] = {}
    try:
        exit_info = await _lookup_exit_ip_geo(proxy, timeout=min(timeout_each, 8.0))
    except Exception:
        exit_info = {}

    async def check_target(item: Dict[str, str]) -> Dict[str, Any]:
        target_url = str(item["url"]).rstrip("/") + "/"
        started = time.monotonic()
        entry: Dict[str, Any] = {
            "id": item["id"],
            "label": item["label"],
            "url": item["url"],
            "success": False,
            "reachable": False,
            "latency_ms": None,
            "http_status": None,
            "message": "",
        }
        try:
            status, _text, _raw = await _request_via_proxy(
                proxy,
                "GET",
                target_url,
                timeout=timeout_each,
                headers={"User-Agent": "SelfieImageProxyProbe/1.0"},
            )
            entry["latency_ms"] = round((time.monotonic() - started) * 1000)
            entry["http_status"] = int(status)
            entry["reachable"] = True
            # Any HTTP response means the proxy tunnel reached the host.
            # 401/403/404 still count as reachable; 5xx counts but marked weaker.
            entry["success"] = 100 <= int(status) < 600
            entry["message"] = f"HTTP {status}"
            return entry
        except asyncio.TimeoutError:
            entry["latency_ms"] = round((time.monotonic() - started) * 1000)
            entry["message"] = "超时"
            entry["http_status"] = None
        except Exception as exc:
            entry["latency_ms"] = round((time.monotonic() - started) * 1000)
            entry["message"] = str(exc)[:120]
            entry["http_status"] = None
        return entry

    results = list(await asyncio.gather(*(check_target(item) for item in PROXY_QUALITY_TARGETS)))
    ok_count = sum(1 for entry in results if entry.get("success"))

    total = len(results) or 1
    score = round(ok_count * 100 / total)
    if score >= 80:
        grade = "A"
    elif score >= 60:
        grade = "B"
    elif score >= 40:
        grade = "C"
    else:
        grade = "D"

    location = str(exit_info.get("location") or "").strip()
    return {
        "success": ok_count > 0,
        "message": f"质量检测完成：{ok_count}/{len(results)} 可达",
        "quality_score": score,
        "quality_grade": grade,
        "ok_count": ok_count,
        "total": len(results),
        "ip_address": str(exit_info.get("ip_address") or ""),
        "city": str(exit_info.get("city") or ""),
        "region": str(exit_info.get("region") or ""),
        "country": str(exit_info.get("country") or ""),
        "country_code": str(exit_info.get("country_code") or ""),
        "org": str(exit_info.get("org") or ""),
        "location": location,
        "results": results,
    }
