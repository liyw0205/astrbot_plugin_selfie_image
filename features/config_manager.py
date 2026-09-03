"""Plugin configuration lifecycle and web management helpers."""

from __future__ import annotations

import asyncio
import copy
import os
import secrets
import shutil
from collections.abc import Mapping, MutableMapping
from typing import Any, Dict, List, Optional

try:
    from astrbot.api import logger
except ImportError:
    from astrbot.api.utils import logger

from ..core.constants import LEGACY_CONFIG_FILENAME, LEGACY_PLUGIN_NAME
from ..core.models import (
    AICatConfig,
    DEFAULT_CONFIG,
    deep_merge,
    normalize_config_tree,
    normalize_legacy_keys,
    strip_channel_timeouts,
)
from ..core.utils import load_json_file, redact_sensitive_data, save_json_file

WEB_STARTUP_CONFIG_KEYS = ("web", "webEnable", "webHost", "webPort", "webToken")
DEFAULT_WEB_TOKEN = str(DEFAULT_CONFIG["web"].get("token") or "changeme").strip().lower()


class ConfigurationMixin:
    def _migrate_legacy_data_dir(self, plugin_data_dir: str) -> None:
        if os.path.exists(self.data_dir):
            return
        legacy_dir = os.path.join(plugin_data_dir, LEGACY_PLUGIN_NAME)
        if not os.path.isdir(legacy_dir):
            return
        try:
            shutil.copytree(legacy_dir, self.data_dir)
            logger.info(f"[SelfieImage] 已迁移旧数据目录: {legacy_dir} -> {self.data_dir}")
        except Exception as exc:
            logger.warning(f"[SelfieImage] 迁移旧数据目录失败: {exc}", exc_info=True)

    def _migrate_legacy_config_file(self) -> None:
        legacy_path = os.path.join(self.data_dir, LEGACY_CONFIG_FILENAME)
        if os.path.exists(self.config_path) or not os.path.exists(legacy_path):
            return
        try:
            shutil.copy2(legacy_path, self.config_path)
            logger.info(f"[SelfieImage] 已迁移旧配置文件: {legacy_path} -> {self.config_path}")
        except Exception as exc:
            logger.warning(f"[SelfieImage] 迁移旧配置文件失败: {exc}", exc_info=True)

    def _config_object_to_dict(self, value: Any) -> Dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, Mapping):
            return {str(key): self._plain_config_value(item) for key, item in value.items()}
        for method_name in ("to_dict", "dict", "model_dump"):
            method = getattr(value, method_name, None)
            if not callable(method):
                continue
            try:
                converted = method()
                if isinstance(converted, Mapping):
                    return {str(key): self._plain_config_value(item) for key, item in converted.items()}
            except Exception:
                continue
        items = getattr(value, "items", None)
        if callable(items):
            try:
                return {str(key): self._plain_config_value(item) for key, item in items()}
            except Exception:
                pass
        keys = getattr(value, "keys", None)
        if callable(keys):
            try:
                return {str(key): self._plain_config_value(value[key]) for key in keys()}
            except Exception:
                pass
        return {}

    def _plain_config_value(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): self._plain_config_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._plain_config_value(item) for item in value]
        if isinstance(value, tuple):
            return [self._plain_config_value(item) for item in value]
        return copy.deepcopy(value)

    def _generate_web_token(self) -> str:
        return secrets.token_urlsafe(24)

    def _set_mapping_web_token(self, data: MutableMapping[str, Any], token: str) -> None:
        web = data.get("web")
        if isinstance(web, MutableMapping):
            web_value = web.get("value")
            if isinstance(web_value, MutableMapping):
                token_value = web_value.get("token")
                if isinstance(token_value, MutableMapping) and "value" in token_value:
                    token_value["value"] = token
                else:
                    web_value["token"] = token
            else:
                token_value = web.get("token")
                if isinstance(token_value, MutableMapping) and "value" in token_value:
                    token_value["value"] = token
                else:
                    web["token"] = token
        else:
            data["web"] = {"token": token}
        if "webToken" in data:
            legacy_token = data.get("webToken")
            if isinstance(legacy_token, MutableMapping) and "value" in legacy_token:
                legacy_token["value"] = token
            else:
                data["webToken"] = token

    def _try_persist_native_web_token(self, token: str) -> bool:
        persisted = False
        if self._native_config is not None:
            try:
                if isinstance(self._native_config, MutableMapping):
                    self._set_mapping_web_token(self._native_config, token)
                else:
                    native_config = self._config_object_to_dict(self._native_config)
                    self._set_mapping_web_token(native_config, token)
                    update = getattr(self._native_config, "update", None)
                    if callable(update):
                        update(native_config)
                    else:
                        for key, value in native_config.items():
                            self._native_config[key] = value
                save_config = getattr(self._native_config, "save_config", None)
                if callable(save_config):
                    save_config()
                persisted = True
            except Exception as exc:
                logger.warning(f"[SelfieImage] 随机 Web Token 写回 AstrBot 配置对象失败: {exc}", exc_info=True)

        if self._native_config_path:
            try:
                native_file_config = load_json_file(self._native_config_path)
                self._set_mapping_web_token(native_file_config, token)
                save_json_file(self._native_config_path, native_file_config)
                persisted = True
            except Exception as exc:
                logger.warning(f"[SelfieImage] 随机 Web Token 写回 AstrBot 配置文件失败: {exc}", exc_info=True)
        return persisted

    def _extract_native_key_config(self, native_config: Dict[str, Any]) -> Dict[str, Any]:
        normalized = normalize_legacy_keys(normalize_config_tree(copy.deepcopy(native_config or {})))
        web = normalized.get("web") if isinstance(normalized.get("web"), dict) else {}
        key_config = {"web": copy.deepcopy(DEFAULT_CONFIG["web"])}
        for key in ("enable", "host", "port", "token"):
            if key in web:
                key_config["web"][key] = web[key]
        if str(key_config["web"].get("token") or "").strip().lower() == DEFAULT_WEB_TOKEN:
            token = self._generate_web_token()
            key_config["web"]["token"] = token
            persisted = self._try_persist_native_web_token(token)
            suffix = "已写回 AstrBot 原生配置" if persisted else "未能自动写回配置，请手动保存"
            logger.warning(f"[SelfieImage] web.token 为默认 changeme，已自动生成随机 Web Token: {token}（{suffix}）")
        return key_config

    def _strip_web_startup_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        cleaned = copy.deepcopy(data if isinstance(data, dict) else {})
        for key in WEB_STARTUP_CONFIG_KEYS:
            cleaned.pop(key, None)
        return cleaned

    def _load_initial_config(self) -> Dict[str, Any]:
        persisted = self._strip_web_startup_config(load_json_file(self.config_path))
        source = strip_channel_timeouts(
            normalize_legacy_keys(normalize_config_tree(deep_merge(DEFAULT_CONFIG, persisted)))
        )
        source["web"] = copy.deepcopy(self.key_config["web"])
        return source

    def _persist_config(self) -> None:
        with self._config_lock:
            web_config = self._strip_web_startup_config(self.raw_config)
            save_json_file(self.config_path, web_config)

    def _apply_raw_config(self, raw: Dict[str, Any]) -> None:
        raw = self._strip_web_startup_config(raw)
        next_config = strip_channel_timeouts(
            normalize_legacy_keys(normalize_config_tree(deep_merge(DEFAULT_CONFIG, raw)))
        )
        next_config["web"] = copy.deepcopy(self.key_config["web"])
        self.raw_config = next_config
        self.config = AICatConfig.from_dict(self.raw_config)
        self._semaphore = asyncio.Semaphore(self.config.image_max_concurrent_tasks)
        self._image_batch_gate = asyncio.Semaphore(self.config.image_max_concurrent_tasks)
        self._selfie_batch_gate = self._image_batch_gate
        self._persist_config()

    def _start_web_server(self) -> None:
        if not self.config.web_enable:
            return
        try:
            self.web_server.start(self.config.web_host, self.config.web_port)
            logger.info(f"[SelfieImage] Flask Web 已启动: http://{self.config.web_host}:{self.config.web_port}")
        except Exception as exc:
            logger.error(f"[SelfieImage] Flask Web 启动失败: {exc}", exc_info=True)

    def get_config_for_web(self) -> Dict[str, Any]:
        return self._strip_web_startup_config(self.raw_config)

    def export_config_for_web(self) -> Dict[str, Any]:
        exported = redact_sensitive_data(self.get_config_for_web())
        if isinstance(exported, dict):
            exported["schema_version"] = int(exported.get("schema_version") or 2)
            exported["_export_note"] = "API key、Token、代理密码已脱敏；导入前请补回凭据。"
        return exported

    def preview_config_import(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        candidate = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        if not isinstance(candidate, dict):
            raise ValueError("配置必须是 JSON 对象")
        merged = deep_merge(self.raw_config, candidate)
        from ..core.models import preflight_config_channels
        report = preflight_config_channels(merged)
        return {"ok": bool(report.get("ok")), "schema_version": 2, "errors": report.get("errors", []), "config": redact_sensitive_data(candidate)}

    def import_config_from_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        candidate = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        if not isinstance(candidate, dict):
            raise ValueError("配置必须是 JSON 对象")
        before = copy.deepcopy(self.raw_config)
        try:
            preview = self.preview_config_import(candidate)
            if not preview["ok"]:
                raise RuntimeError("配置预检未通过")
            return self.update_config_from_web(candidate)
        except Exception:
            self._apply_raw_config(before)
            self._persist_config()
            raise

    def list_proxies_for_web(self, *, mask_password: bool = True) -> List[Dict[str, Any]]:
        from ..core.models import normalize_proxies_list, public_proxy_row
        rows = normalize_proxies_list((self.raw_config or {}).get("proxies") or [])
        return [public_proxy_row(row, mask_password=mask_password) for row in rows]

    def _find_proxy_row(self, proxy_id: str) -> Dict[str, Any]:
        from ..core.models import normalize_proxies_list
        pid = str(proxy_id or "").strip()
        if not pid:
            raise ValueError("缺少代理 id")
        for row in normalize_proxies_list((self.raw_config or {}).get("proxies") or []):
            if str(row.get("id") or "") == pid:
                return row
        raise ValueError("代理不存在")

    async def test_proxy_connectivity(self, proxy_id: str = "", proxy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from ..core.models import normalize_proxy_entry
        from ..core.proxy import probe_proxy_connectivity
        if proxy_id:
            row = self._find_proxy_row(proxy_id)
        else:
            row = normalize_proxy_entry(proxy or {})
            if not row:
                raise ValueError("代理参数无效")
        result = await probe_proxy_connectivity(str(row.get("url") or ""))
        result["proxy_id"] = str(row.get("id") or proxy_id or "")
        result["proxy_name"] = str(row.get("name") or "")
        return result

    async def test_proxy_quality(self, proxy_id: str = "", proxy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from ..core.models import normalize_proxy_entry
        from ..core.proxy import probe_proxy_quality
        if proxy_id:
            row = self._find_proxy_row(proxy_id)
        else:
            row = normalize_proxy_entry(proxy or {})
            if not row:
                raise ValueError("代理参数无效")
        result = await probe_proxy_quality(str(row.get("url") or ""))
        result["proxy_id"] = str(row.get("id") or proxy_id or "")
        result["proxy_name"] = str(row.get("name") or "")
        return result

    def update_config_from_web(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._config_lock:
            patch = self._strip_web_startup_config(patch)
            if isinstance(patch, dict) and isinstance(patch.get("proxies"), list):
                # Keep existing proxy passwords when UI sends blank / masked values.
                old_by_id = {
                    str(item.get("id") or ""): item
                    for item in (self.raw_config.get("proxies") or [])
                    if isinstance(item, dict) and item.get("id")
                }
                fixed = []
                for item in patch["proxies"]:
                    if not isinstance(item, dict):
                        continue
                    row = dict(item)
                    pid = str(row.get("id") or "").strip()
                    pwd = str(row.get("password") or "")
                    if pid and old_by_id.get(pid) and pwd in {"", "******", "[REDACTED]", "«redacted»"}:
                        old_pwd = str(old_by_id[pid].get("password") or "")
                        if old_pwd and old_pwd not in {"******", "[REDACTED]"}:
                            row["password"] = old_pwd
                    fixed.append(row)
                patch["proxies"] = fixed
            merged = deep_merge(self.raw_config, patch)
            from ..core.models import preflight_config_channels, sanitize_channels_for_save

            # Soft-fix empty-model channels before merge persist (auto-disable).
            sanitize_channels_for_save(merged)
            if isinstance(patch, dict):
                sanitize_channels_for_save(patch)
            report = preflight_config_channels(merged)
            channel_keys = (
                "image_channels",
                "audit_channels",
                "video_channels",
                "imageChannels",
                "auditChannels",
                "videoChannels",
            )
            if isinstance(patch, dict) and any(key in patch for key in channel_keys):
                if not report.get("ok"):
                    raise RuntimeError(report.get("message") or "渠道配置预检未通过")
                # Prefer sanitized tree from preflight when present.
                if isinstance(report.get("config"), dict):
                    for key in ("image_channels", "audit_channels", "video_channels"):
                        if key in report["config"]:
                            merged[key] = report["config"][key]
            self._apply_raw_config(merged)
            return self.get_config_for_web()
