"""Runtime QQ/OneBot group discovery for the dashboard."""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    logger = None

try:  # pragma: no cover - optional AstrBot adapter
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
except Exception:  # pragma: no cover
    AiocqhttpAdapter = None


def _platforms(context: Any) -> list[Any]:
    manager = getattr(context, "platform_manager", None)
    if manager is None:
        return []
    instances = getattr(manager, "platform_insts", None)
    if instances is None and callable(getattr(manager, "get_insts", None)):
        try:
            instances = manager.get_insts()
        except Exception:
            instances = []
    return list(instances or [])


def _platform_id(platform: Any) -> tuple[str, str]:
    try:
        meta = platform.meta()
        return str(getattr(meta, "id", "") or "").strip(), str(getattr(meta, "name", "") or "").lower()
    except Exception:
        return str(getattr(platform, "id", "") or "").strip(), str(getattr(platform, "name", "") or "").lower()


def _client(platform: Any) -> Any:
    try:
        if callable(getattr(platform, "get_client", None)):
            return platform.get_client()
        return getattr(platform, "bot", None)
    except Exception:
        return None


def _is_qq_platform(platform: Any, platform_name: str) -> bool:
    if AiocqhttpAdapter is not None and isinstance(platform, AiocqhttpAdapter):
        return True
    return not platform_name or any(marker in platform_name for marker in ("qq", "onebot", "aiocqhttp"))


class QQGroupCache:
    def __init__(self, plugin: Any, ttl_seconds: int = 300) -> None:
        self.plugin = plugin
        self.ttl_seconds = max(30, int(ttl_seconds))
        self._lock = asyncio.Lock()
        self._groups: list[dict[str, Any]] = []
        self._refreshed_at = 0.0
        self._available = False
        self._message = "QQ 平台不可用"

    async def list_groups(self, configured: dict[str, Any] | None = None, *, force: bool = False) -> dict[str, Any]:
        if force or time.time() - self._refreshed_at >= self.ttl_seconds:
            await self._refresh(force=force)
        rows = {str(row.get("group_id")): copy.deepcopy(row) for row in self._groups if row.get("group_id")}
        for group_id in (configured or {}):
            group_id = str(group_id).strip()
            if group_id.isdigit() and group_id not in rows:
                rows[group_id] = {"group_id": group_id, "group_name": f"群 {group_id}", "source": "configured"}
        groups = sorted(rows.values(), key=lambda row: (not str(row.get("group_id", "")).isdigit(), int(row["group_id"]) if str(row.get("group_id", "")).isdigit() else 0))
        return {"groups": groups, "available": self._available, "message": self._message}

    async def _refresh(self, *, force: bool = False) -> None:
        async with self._lock:
            if not force and time.time() - self._refreshed_at < self.ttl_seconds:
                return
            found: dict[str, dict[str, Any]] = {}
            called = False
            for platform in _platforms(getattr(self.plugin, "context", None)):
                platform_id, platform_name = _platform_id(platform)
                if not platform_id or not _is_qq_platform(platform, platform_name):
                    continue
                client = _client(platform)
                if client is None or not callable(getattr(client, "call_action", None)):
                    continue
                try:
                    result = await client.call_action("get_group_list")
                    called = True
                except Exception as exc:
                    if logger:
                        logger.warning("[SelfieImage] 获取 QQ 群列表失败: %s", exc)
                    continue
                data = result.get("data") if isinstance(result, dict) else result
                for raw in data if isinstance(data, list) else []:
                    if not isinstance(raw, dict):
                        continue
                    group_id = str(raw.get("group_id") or "").strip()
                    if group_id and group_id not in found:
                        found[group_id] = {
                            "group_id": group_id,
                            "group_name": str(raw.get("group_name") or f"群 {group_id}").strip(),
                            "member_count": raw.get("member_count", 0),
                            "source": "live",
                            "platform_id": platform_id,
                        }
            if called:
                self._groups = list(found.values())
                self._available = True
                self._message = ""
            else:
                self._available = False
                self._groups = [
                    {**row, "source": "cached"}
                    for row in self._groups
                ]
                self._message = "未找到可调用的 QQ/OneBot 平台"
                if self._groups:
                    self._message += "；当前显示上次读取的群列表"
            self._refreshed_at = time.time()
