from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_selfie_image.core.models import AICatConfig
from astrbot_plugin_selfie_image.features.access_policy import access_status, permission_denied_message
from astrbot_plugin_selfie_image.features.qq_group_cache import QQGroupCache
from astrbot_plugin_selfie_image.webui.web import FlaskWebServer


def test_group_chat_config_normalizes_supported_values():
    config = AICatConfig.from_dict({
        "group_chat_configs": {
            "123": {"chat_type": "white", "model_priority_mode": "fixed", "fixed_model": "paint/m1"},
            "not-a-group": {"chat_type": "black"},
            "456": {"chat_type": "invalid", "model_priority_mode": "invalid"},
        }
    })
    assert config.group_chat_configs == {
        "123": {"chat_type": "white", "model_priority_mode": "fixed", "fixed_model": "paint/m1"},
        "456": {"chat_type": "none", "model_priority_mode": "default", "fixed_model": ""},
    }


def test_group_chat_access_modes_blacklist_and_whitelist():
    groups = {
        "100": {"chat_type": "black"},
        "200": {"chat_type": "white"},
    }
    blocked = access_status(
        user_id="u1", group_id="100", blocked_users=[], usable_users=[],
        whitelist_users=[], whitelist_groups=[], group_chat_configs=groups,
    )
    assert not blocked["allowed"]
    assert permission_denied_message(blocked) == "当前群聊已被加入黑名单，无法使用生图功能。"
    allowed = access_status(
        user_id="u1", group_id="200", blocked_users=[], usable_users=[],
        whitelist_users=[], whitelist_groups=[], group_chat_configs=groups,
    )
    assert allowed["allowed"] and allowed["unlimited"] and allowed["whitelist"]
    legacy = access_status(
        user_id="u1", group_id="300", blocked_users=[], usable_users=[],
        whitelist_users=[], whitelist_groups=["300"],
    )
    explicit_none = access_status(
        user_id="u1", group_id="300", blocked_users=[], usable_users=[],
        whitelist_users=[], whitelist_groups=["300"], group_chat_configs={"300": {"chat_type": "none"}},
    )
    assert legacy["unlimited"]
    assert not explicit_none["unlimited"]


def test_group_model_modes_use_enabled_models_and_fixed_selection():
    config = AICatConfig.from_dict({
        "image_channels": [{
            "name": "paint", "provider_type": "openai", "base_url": "https://image.example",
            "enabled_models": ["m1", "m2"], "enabled": True,
        }],
        "enabled_image_model_priority": ["paint/m2"],
    })
    assert [target.label for target in config.get_image_targets_for_mode("sequence", respect_global_priority=False)] == ["paint/m1", "paint/m2"]
    assert [target.label for target in config.get_image_targets_for_mode("fixed", "paint/m1", respect_global_priority=False)] == ["paint/m1"]
    assert config.get_image_targets_for_mode("fixed", "missing", respect_global_priority=False) == []
    with patch("random.choice", side_effect=lambda values: values[-1]):
        assert [target.label for target in config.get_image_targets_for_mode("random", respect_global_priority=False)] == ["paint/m2"]


def test_qq_group_cache_uses_onebot_action_and_merges_saved_groups():
    class Client:
        async def call_action(self, action):
            assert action == "get_group_list"
            return {"data": [{"group_id": 12, "group_name": "开发群"}]}

    platform = SimpleNamespace(
        meta=lambda: SimpleNamespace(id="qq-main", name="aiocqhttp"),
        get_client=lambda: Client(),
    )
    plugin = SimpleNamespace(context=SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[platform])))
    result = asyncio.run(QQGroupCache(plugin).list_groups({"34": {}}))
    assert result["available"]
    assert [(row["group_id"], row["group_name"]) for row in result["groups"]] == [("12", "开发群"), ("34", "群 34")]


def test_flask_group_api_reads_and_saves_group_config():
    class Plugin:
        config = SimpleNamespace(web_token="")

        async def list_qq_groups_for_web(self, *, force=False):
            return {"groups": [{"group_id": "12"}], "models": ["paint/m1"], "available": True}

        def update_group_chat_configs_from_web(self, payload):
            self.saved = payload
            return {"group_chat_configs": {"12": payload["groups"][0]["config"]}}

    client = FlaskWebServer(Plugin())._create_app().test_client()
    response = client.get("/api/qq-groups")
    assert response.status_code == 200
    assert response.get_json()["data"]["models"] == ["paint/m1"]
    response = client.post("/api/qq-groups/config", json={"groups": [{
        "group_id": "12",
        "config": {"chat_type": "black", "model_priority_mode": "fixed", "fixed_model": "paint/m1"},
    }]})
    assert response.status_code == 200
    assert response.get_json()["data"]["group_chat_configs"]["12"]["chat_type"] == "black"
