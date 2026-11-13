"""Configuration startup reuse must preserve the former three-parse result."""

from __future__ import annotations

import copy
import json
import threading
from itertools import count
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from astrbot_plugin_selfie_image.core import models
from astrbot_plugin_selfie_image.features import config_manager
from astrbot_plugin_selfie_image.features.config_manager import ConfigurationMixin


NATIVE_WEB = {"enable": False, "host": "127.0.0.2", "port": 12345, "token": "native-token"}


def _plugin(tmp_path):
    plugin = ConfigurationMixin()
    plugin.config_path = str(tmp_path / "config.json")
    plugin.key_config = {"web": copy.deepcopy(NATIVE_WEB)}
    plugin._config_lock = threading.RLock()
    return plugin


def _previous_initial_state(plugin):
    """Frozen startup pipeline before the shared parsed-state helper."""
    persisted = plugin._strip_web_startup_config(config_manager.load_json_file(plugin.config_path))
    normalized_persisted = models.normalize_legacy_keys(models.normalize_config_tree(persisted))
    permission_source = normalized_persisted.get("permission")
    permission_source = permission_source if isinstance(permission_source, dict) else {}
    legacy_usable_list = (
        bool(models.split_values(permission_source.get("usable_users")))
        and "usable_users_enabled" not in permission_source
    )
    source = models.strip_channel_timeouts(
        models.normalize_legacy_keys(
            models.normalize_config_tree(models.deep_merge(models.DEFAULT_CONFIG, normalized_persisted))
        )
    )
    if legacy_usable_list:
        source.setdefault("permission", {})["usable_users_enabled"] = True
    source.setdefault("image", {})["max_concurrent_tasks"] = models.AICatConfig.from_dict(source).image_max_concurrent_tasks
    source["web"] = copy.deepcopy(plugin.key_config["web"])
    config_manager._ensure_channel_ids(source)
    normalized = models.AICatConfig.from_dict(source)
    for key in ("proxies", "image_channels", "audit_channels"):
        source[key] = copy.deepcopy(normalized.raw[key])
    return source, models.AICatConfig.from_dict(source)


def _deterministic_ids():
    identities = count(1)
    return (
        patch.object(config_manager.secrets, "token_hex", side_effect=lambda size: f"{next(identities):0{size * 2}x}"),
        patch.object(models, "_new_proxy_id", return_value="px_migrated"),
    )


LEGACY = {
    "schema_version": 1,
    "web": {"enable": True, "host": "0.0.0.0", "port": 9999, "token": "persisted-token"},
    "webToken": "legacy-token",
    "botName": "retired-name",
    "permission": {"usable_users": "10001|10002"},
    "image": {"maxConcurrentTasks": "99"},
    "imageChannels": [
        {
            "id": "paint-id", "name": "paint", "providerType": "openai",
            "baseUrl": "https://image.example.test", "apiKey": "secret-one;secret-two",
            "apiKeys": ["******"], "model": "image-model", "timeout": 1,
            "proxy": "http://user:proxy-secret@proxy.example.test:8080",
        },
        {
            "id": "paint-id", "name": "copied", "providerType": "openai",
            "baseUrl": "https://copy.example.test", "apiKey": "copy-secret", "model": "copy-model", "timeout": 2,
        },
    ],
    "auditChannels": [{"name": "audit", "providerType": "openai_chat", "baseUrl": "https://audit.example.test", "apiKey": "audit-secret", "model": "audit-model"}],
    "groupChatConfigs": {"123": {"chat_type": "invalid", "model_priority_mode": "invalid"}},
    "videoChannels": [{"name": "retired"}],
}


INVALID = {
    "schema_version": "invalid",
    "image": {"max_concurrent_tasks": "invalid", "prompt_en_mode": "invalid"},
    "permission": {"usable_users": "10001", "usable_users_enabled": False},
    "image_channels": [
        {"id": "same-first", "name": "same", "model": "m1", "image_to_text_models": ["m1"], "prompt_en_models": ["m1"]},
        {"id": "same-last", "name": "same", "model": "m2", "image_to_text_models": ["m2"]},
        {"name": "invalid-provider", "provider_type": "invalid"},
    ],
    "proxies": [{"host": "", "port": "bad"}, "invalid"],
    "group_chat_configs": {"invalid-id": {"chat_type": "black"}, "456": {"chat_type": "invalid"}},
}


DUPLICATE_PROXIES = {
    "proxies": [
        {"id": "px_one", "url": "http://user:one@proxy-one.example.test:8080"},
        {"id": "px_two", "url": "http://user:two@proxy-two.example.test:8080"},
        {"id": "px_disabled", "url": "http://disabled.example.test:8080", "enabled": False},
    ],
    "image_channels": [
        {"name": "same", "model": "m1", "proxy_id": "px_one"},
        {"name": "same", "model": "m2", "proxy_id": "px_disabled"},
        {"name": "legacy-same", "model": "m1", "proxy": "http://user:one@proxy-one.example.test:8080"},
        {"name": "legacy-same", "model": "m2"},
    ],
    "audit_channels": [
        {"name": "same", "model": "m1", "proxy": "http://user:one@proxy-one.example.test:8080"},
        {"name": "same", "model": "m2", "proxy": "http://user:two@proxy-two.example.test:8080"},
    ],
}


@pytest.mark.parametrize(
    "content,expected_parses",
    [(None, 1), ("", 1), ("{}", 1), ("[]", 1), ("{invalid", 1), (json.dumps(LEGACY), 1), (json.dumps(INVALID), 2), (json.dumps(DUPLICATE_PROXIES), 2)],
    ids=["absent", "empty-file", "empty-config", "non-object", "invalid-json", "legacy", "invalid-values", "duplicate-proxies"],
)
def test_initial_state_reuses_parse_and_matches_previous_final_state(tmp_path, content, expected_parses):
    plugin = _plugin(tmp_path)
    path = tmp_path / "config.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    before = path.read_bytes() if path.exists() else None
    native_before = copy.deepcopy(plugin.key_config)

    channel_ids, proxy_ids = _deterministic_ids()
    with channel_ids, proxy_ids, patch.object(models.AICatConfig, "from_dict", wraps=models.AICatConfig.from_dict) as previous_parse:
        expected_raw, expected_config = _previous_initial_state(plugin)
        assert previous_parse.call_count == 3

    channel_ids, proxy_ids = _deterministic_ids()
    with channel_ids, proxy_ids, patch.object(models.AICatConfig, "from_dict", wraps=models.AICatConfig.from_dict) as current_parse:
        actual_raw, actual_config = plugin._load_initial_config_state()
        assert current_parse.call_count == expected_parses

    assert actual_raw == expected_raw
    assert actual_config == expected_config
    assert actual_raw["web"] == NATIVE_WEB
    assert actual_raw["web"] is not plugin.key_config["web"]
    assert actual_config.raw["web"] is not actual_raw["web"]
    assert plugin.key_config == native_before
    assert (path.read_bytes() if path.exists() else None) == before
    assert actual_config.raw["image"]["max_concurrent_tasks"] == actual_config.image_max_concurrent_tasks


@pytest.mark.parametrize("value", [1, "1", 3, "5", 0, 6, -1, "1.0", "2x", None])
def test_initial_state_preserves_concurrency_normalization(tmp_path, value):
    plugin = _plugin(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"image": {"max_concurrent_tasks": value}}), encoding="utf-8")
    raw, config = plugin._load_initial_config_state()
    expected = models.normalize_concurrent_count(value)
    assert raw["image"]["max_concurrent_tasks"] == expected
    assert config.raw["image"]["max_concurrent_tasks"] == expected
    assert config.image_max_concurrent_tasks == expected


def test_legacy_loader_wrapper_returns_dict_without_extra_parse(tmp_path):
    plugin = _plugin(tmp_path)
    with patch.object(models.AICatConfig, "from_dict", wraps=models.AICatConfig.from_dict) as parse:
        raw = plugin._load_initial_config()
    assert isinstance(raw, dict)
    assert parse.call_count == 1


def test_initial_state_preserves_identity_credentials_and_permission_migration(tmp_path):
    plugin = _plugin(tmp_path)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(LEGACY), encoding="utf-8")
    plugin.raw_config, plugin.config = plugin._load_initial_config_state()
    original_raw = copy.deepcopy(plugin.raw_config)

    assert plugin.config.usable_users_enabled is True
    assert plugin.config.usable_users == ["10001", "10002"]
    assert plugin.config.image_max_concurrent_tasks == 5
    assert plugin.raw_config["image_channels"][0]["id"] == "paint-id"
    assert plugin.raw_config["image_channels"][1]["id"] != "paint-id"
    assert all("timeout" not in row for row in plugin.raw_config["image_channels"])
    assert plugin.config.image_channels[0].resolved_api_keys() == ["secret-one", "secret-two"]
    assert plugin.config.proxies[0]["password"] == "proxy-secret"

    exported = plugin.get_config_for_web()
    assert "web" not in exported
    assert exported["image_channels"][0]["apiKey"] == "******"
    exported["image_channels"].reverse()
    scheduler = Mock()
    plugin._image_scheduler = scheduler
    with patch.object(models.AICatConfig, "from_dict", wraps=models.AICatConfig.from_dict) as parse:
        plugin.update_config_from_web({**exported, "image": {"max_concurrent_tasks": "3"}, "web": {"token": "untrusted"}})
    assert parse.call_count == 1
    assert plugin._image_scheduler is scheduler
    scheduler.set_limit.assert_called_once_with(3)
    assert plugin.config.web_token == "native-token"
    by_name = {row["name"]: row for row in plugin.raw_config["image_channels"]}
    assert by_name["paint"]["id"] == original_raw["image_channels"][0]["id"]
    assert by_name["copied"]["id"] == original_raw["image_channels"][1]["id"]
    by_name = {channel.name: channel for channel in plugin.config.image_channels}
    assert by_name["paint"].resolved_api_keys() == ["secret-one", "secret-two"]
    assert by_name["copied"].resolved_api_keys() == ["copy-secret"]
    assert plugin.config.proxies[0]["password"] == "proxy-secret"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert "web" not in persisted
    assert persisted["image"]["max_concurrent_tasks"] == 3
    assert persisted["image_channels"][0]["id"] == original_raw["image_channels"][1]["id"]
    assert persisted["proxies"][0]["password"] == "proxy-secret"


@pytest.mark.parametrize("content,expected_parses", [
    (None, 1),
    ("{invalid", 1),
    (json.dumps({"schema_version": 2, "image": {"max_concurrent_tasks": "3"}}), 1),
    (json.dumps(LEGACY), 2),
], ids=["absent", "invalid-json", "current", "schema-migration"])
def test_plugin_initialization_reuses_loaded_state(tmp_path, monkeypatch, content, expected_parses):
    from astrbot_plugin_selfie_image import main

    path = tmp_path / "plugin_data" / main.PLUGIN_NAME / main.PLUGIN_CONFIG_FILENAME
    if content is not None:
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(main, "get_astrbot_data_path", lambda: str(tmp_path))
    with patch.object(models.AICatConfig, "from_dict", wraps=models.AICatConfig.from_dict) as parse:
        plugin = main.SelfieImagePlugin(SimpleNamespace(), {"web": copy.deepcopy(NATIVE_WEB)})
    assert parse.call_count == expected_parses
    assert plugin.config.web_token == "native-token"
    assert plugin.config.raw["image"]["max_concurrent_tasks"] == plugin.config.image_max_concurrent_tasks
    assert plugin._image_scheduler._limit == plugin.config.image_max_concurrent_tasks
    if expected_parses == 2:
        assert path.with_suffix(path.suffix + ".bak").read_text(encoding="utf-8") == content
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted["schema_version"] == 2
        assert "web" not in persisted
    elif content is None:
        assert not path.exists()
    else:
        assert path.read_text(encoding="utf-8") == content
