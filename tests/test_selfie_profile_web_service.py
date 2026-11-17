"""Selfie profile hooks against isolated, persistent persona and wardrobe stores."""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from astrbot_plugin_selfie_image.core.models import AICatConfig
from astrbot_plugin_selfie_image.features.persona import PersonaManager
from astrbot_plugin_selfie_image.main import SelfieImagePlugin


def _png(size=(2, 2), color="red"):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def _payload(data, field="image"):
    return {field: "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")}


@pytest.fixture
def plugin(tmp_path):
    instance = object.__new__(SelfieImagePlugin)
    instance.persona = PersonaManager(str(tmp_path))
    instance.config = AICatConfig.from_dict({"web": {"enable": False}})
    return instance


@pytest.mark.parametrize("field", ["image", "data"])
def test_reference_upload_type_alias_and_clear_persist(plugin, tmp_path, field):
    initial = plugin.get_selfie_reference_payload()
    assert initial["has_image"] is False
    assert "image" not in initial
    data = _png()
    result = plugin.save_selfie_reference_from_web(_payload(data, field) | {"type": "真人"})
    assert result["has_image"] is True
    assert result["appearance_type"] == "real"
    assert result["appearance_type_label"] == "真人"
    assert result["image"] == "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    assert result["ref_mime_type"] == "image/png"
    plugin.persona = PersonaManager(str(tmp_path))
    assert plugin.get_selfie_reference_payload() == result
    changed = plugin.set_selfie_appearance_type_from_web({"appearance_type": "动漫"})
    assert changed["appearance_type"] == "anime"
    assert plugin.persona.get_reference_image()["data"] == data
    image_path = plugin.persona.get_reference_path()
    cleared = plugin.clear_selfie_reference_from_web()
    assert cleared["has_image"] is False
    assert "image" not in cleared
    assert cleared["appearance_type"] == "anime"
    assert not Path(image_path).exists()
    assert PersonaManager(str(tmp_path)).get_reference_image() is None


@pytest.mark.parametrize("hook", ["save_selfie_reference_from_web", "save_wardrobe_item_from_web"])
@pytest.mark.parametrize("invalid", ["missing", "non_image", "bytes", "dimension", "pixels", "frames"])
def test_image_limits_fail_before_mutating_stores(plugin, hook, invalid):
    plugin.persona.save_reference_image(_png(color="blue"))
    plugin.get_selfie_reference_payload()
    plugin.config = AICatConfig.from_dict({"web": {"enable": False}})
    before = plugin.persona.get()
    if invalid == "missing":
        body, message = {}, "缺少 image"
    elif invalid == "non_image":
        body, message = _payload(b"not an image"), "上传图片为空"
    elif invalid == "bytes":
        plugin.config.image_max_image_size_mb = 1
        body, message = _payload(_png() + b"x" * (1024 * 1024)), "图片过大"
    elif invalid == "dimension":
        plugin.config.image_max_image_dimension = 256
        body, message = _payload(_png((257, 1))), "尺寸/动画帧数超限"
    elif invalid == "pixels":
        plugin.config.image_max_image_pixels = 1024 * 1024
        body, message = _payload(_png((1025, 1025))), "尺寸/动画帧数超限"
    else:
        plugin.config.image_max_animation_frames = 1
        output = BytesIO()
        Image.new("RGB", (2, 2), "red").save(
            output, format="GIF", save_all=True,
            append_images=[Image.new("RGB", (2, 2), "blue")],
        )
        body, message = _payload(output.getvalue()), "尺寸/动画帧数超限"
    body["appearance_type"] = "anime"
    with pytest.raises(ValueError, match=message):
        result = getattr(plugin, hook)(body)
        if hook == "save_wardrobe_item_from_web":
            asyncio.run(result)
    assert plugin.persona.get() == before
    assert plugin.persona.wardrobe.list_items() == []


def test_wardrobe_hook_aliases_reconcile_schedule_and_reload(plugin, tmp_path):
    first = asyncio.run(plugin.save_wardrobe_item_from_web(_payload(_png(), "data") | {
        "title": "第一套", "description": "衬衫", "tags": "春季,上衣",
    }))
    item_id = first["items"][0]["id"]
    assert first["current_item_id"] == item_id
    assert plugin.persona.current_wardrobe_reference().data == _png()
    updated = plugin.update_wardrobe_item_from_web({
        "item_id": item_id, "name": "新名称", "description": "更新描述", "tags": ["夏季"],
    })
    assert updated["items"][0]["name"] == "新名称"
    assert updated["items"][0]["description"] == "更新描述"
    assert updated["items"][0]["tags"] == ["夏季"]
    assert plugin.set_wardrobe_settings_from_web({"selection_mode": "sequential"})["selection_mode"] == "sequential"
    for enabled in ("off", "启用"):
        toggled = asyncio.run(plugin.toggle_wardrobe_item_from_web({"item_id": item_id, "enabled": enabled}))
        assert bool(toggled["current_item_id"]) is (enabled == "启用")
        assert bool(plugin.persona.get_daily_schedule().wardrobe_item_id) is (enabled == "启用")
    selected = asyncio.run(plugin.select_wardrobe_today_from_web({"id": " " + item_id + " "}))
    assert selected["wardrobe"]["manual_today"] is True
    assert selected["daily_schedule"]["schedule"]["wardrobe_item_id"] == item_id
    plugin.persona = PersonaManager(str(tmp_path))
    assert plugin.get_wardrobe_payload()["manual_today"] is True
    deleted = asyncio.run(plugin.delete_wardrobe_item_from_web({"item_id": item_id}))
    assert deleted["items"] == []
    assert plugin.persona.current_wardrobe_reference() is None
    assert not plugin.persona.get_daily_schedule().wardrobe_item_id


def test_local_refresh_keeps_pin_and_returns_detached_profile(plugin):
    added = asyncio.run(plugin.save_wardrobe_item_from_web(_payload(_png())))
    item_id = added["items"][0]["id"]
    asyncio.run(plugin.select_wardrobe_today_from_web({"id": item_id}))
    profiles = []
    refresh = plugin.persona.refresh_daily_selfie_profile_local

    async def capture_profile():
        profile = await refresh()
        profiles.append(profile)
        return profile

    plugin.persona.refresh_daily_selfie_profile_local = capture_profile
    refreshed = asyncio.run(plugin.refresh_selfie_profile_local_from_web())
    assert refreshed["wardrobe"]["manual_today"] is True
    assert refreshed["wardrobe"]["current_item_id"] == item_id
    assert refreshed["daily_schedule"]["schedule"]["wardrobe_item_id"] == item_id
    profile = profiles[0]
    assert refreshed["updated_at"] == profile.updated_at
    assert refreshed["source"] == profile.source
    period = next(iter(profile.schedule_by_period))
    expected_node = dict(profile.schedule_by_period[period])
    refreshed["schedule_by_period"][period].clear()
    assert profile.schedule_by_period[period] == expected_node
    refreshed["schedule_by_period"][period] = expected_node
    for name in ("outfit_by_period", "scene_by_period", "action_by_period", "schedule_by_period"):
        assert refreshed[name] == getattr(profile, name)
        refreshed[name].clear()
        assert getattr(profile, name)
    refreshed["outfit_pool"].clear()
    assert profile.outfit_pool


def test_legacy_host_without_cache_attribute_keeps_projection_hooks(plugin):
    class Host:
        __slots__ = ("persona", "config", "calls")
        save_selfie_reference_from_web = SelfieImagePlugin.save_selfie_reference_from_web
        clear_selfie_reference_from_web = SelfieImagePlugin.clear_selfie_reference_from_web
        set_selfie_appearance_type_from_web = SelfieImagePlugin.set_selfie_appearance_type_from_web
        get_wardrobe_payload = SelfieImagePlugin.get_wardrobe_payload
        save_wardrobe_item_from_web = SelfieImagePlugin.save_wardrobe_item_from_web

        def get_selfie_reference_payload(self):
            self.calls += 1
            return {"custom_projection": self.persona.get_appearance_type()}

    host = Host()
    host.persona, host.config, host.calls = plugin.persona, plugin.config, 0
    assert host.save_selfie_reference_from_web(_payload(_png())) == {"custom_projection": "auto"}
    assert host.set_selfie_appearance_type_from_web({"type": "动漫"}) == {"custom_projection": "anime"}
    assert host.clear_selfie_reference_from_web() == {"custom_projection": "anime"}
    assert host.calls == 3
    assert asyncio.run(host.save_wardrobe_item_from_web(_payload(_png())))["current_item_id"]
    assert not hasattr(host, "_selfie_profile_web_service")


@pytest.mark.parametrize("kind", ["reference", "wardrobe"])
def test_cached_service_only_changes_the_replacement_persona(plugin, tmp_path, kind):
    old = plugin.persona
    old.save_reference_image(_png(color="blue"))
    asyncio.run(plugin.save_wardrobe_item_from_web(_payload(_png(color="blue"))))
    plugin.get_selfie_reference_payload()
    service = plugin._selfie_profile_web_service
    old_files = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    }
    replacement_root = tmp_path / "replacement"
    plugin.persona = PersonaManager(str(replacement_root))
    if kind == "reference":
        result = plugin.save_selfie_reference_from_web(_payload(_png()) | {"type": "真人"})
        assert result["appearance_type"] == "real"
        assert result["wardrobe"]["items"] == []
        assert plugin.persona.get_reference_image()["data"] == _png()
        assert plugin.set_selfie_appearance_type_from_web({"type": "动漫"})["appearance_type"] == "anime"
        assert plugin.clear_selfie_reference_from_web()["has_image"] is False
    else:
        result = asyncio.run(plugin.save_wardrobe_item_from_web(_payload(_png()) | {"name": "替换后"}))
        item_id = result["items"][0]["id"]
        assert plugin.persona.current_wardrobe_reference().data == _png()
        assert plugin.update_wardrobe_item_from_web({"id": item_id, "name": "新服装"})["current_item_name"] == "新服装"
        assert plugin.set_wardrobe_settings_from_web({"selection_mode": "sequential"})["selection_mode"] == "sequential"
        selected = asyncio.run(plugin.select_wardrobe_today_from_web({"id": item_id}))
        assert selected["wardrobe"]["manual_today"] is True
        refreshed = asyncio.run(plugin.refresh_selfie_profile_local_from_web())
        assert refreshed["wardrobe"]["current_item_id"] == item_id
        toggled = asyncio.run(plugin.toggle_wardrobe_item_from_web({"id": item_id, "enabled": False}))
        assert toggled["items"][0]["enabled"] is False
        assert asyncio.run(plugin.delete_wardrobe_item_from_web({"id": item_id}))["items"] == []
    assert plugin._selfie_profile_web_service is service
    assert old.get_reference_image()["data"] == _png(color="blue")
    assert old.wardrobe.list_items()[0].name != "新服装"
    assert {name: (tmp_path / name).read_bytes() for name in old_files} == old_files
    assert {
        path.relative_to(tmp_path) for path in tmp_path.rglob("*")
        if path.is_file() and replacement_root not in path.parents
    } == old_files.keys()


def test_reference_projection_keeps_wardrobe_override(plugin):
    plugin.get_wardrobe_payload = lambda: {"custom_wardrobe": True}
    assert plugin.get_selfie_reference_payload()["wardrobe"] == {"custom_wardrobe": True}
