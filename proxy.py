"""Channel proxy parsing and aiohttp session selection."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import AsyncIterator, Optional
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
