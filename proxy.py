"""Channel proxy parsing and aiohttp session selection."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator, Dict, Optional
from urllib.parse import quote, unquote, urlparse

import aiohttp


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
async def channel_client_session(proxy_value: str, fallback: aiohttp.ClientSession) -> AsyncIterator[aiohttp.ClientSession]:
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
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
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

    results = []
    ok_count = 0
    for item in PROXY_QUALITY_TARGETS:
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
            if entry["success"]:
                ok_count += 1
        except asyncio.TimeoutError:
            entry["latency_ms"] = round((time.monotonic() - started) * 1000)
            entry["message"] = "超时"
            entry["http_status"] = None
        except Exception as exc:
            entry["latency_ms"] = round((time.monotonic() - started) * 1000)
            entry["message"] = str(exc)[:120]
            entry["http_status"] = None
        results.append(entry)

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
