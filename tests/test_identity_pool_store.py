from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_selfie_image.features.persona import PersonaManager
from astrbot_plugin_selfie_image.core.models import AICatConfig
from astrbot_plugin_selfie_image.main import SelfieImagePlugin


PNG = b"\x89PNG\r\n\x1a\nidentity"


def test_persona_pool_defaults_and_image_slots(tmp_path):
    manager = PersonaManager(tmp_path)
    assert manager.get_active_persona()["name"] == "小玉"
    assert [item["name"] for item in manager.get()["persona_pool"]] == [
        "小玉", "祈枝枝o.o", "奶包菜", "燕子回来了", "蛋姐",
    ]
    assert manager.get_identity_mode() == "image"

    manager.save_reference_image(PNG, "image/png")
    manager.save_reference_image(PNG + b"2", "image/png")
    assert [item["slot"] for item in manager.list_image_pool()] == [1, 2]
    manager.remove_reference_image(1)
    manager.save_reference_image(PNG + b"3", "image/png")
    assert [item["slot"] for item in manager.list_image_pool()] == [1, 2]
    assert manager.data["active_image_slot"] == 1

    payload = manager.identity_pool_payload()
    assert all("path" not in item for item in payload["image_pool"])
    assert all("path" not in item for item in payload["persona_pool"])


def test_existing_persona_pool_receives_new_builtins_once(tmp_path):
    old_state = {
        "identity_mode": "persona",
        "persona_pool": [{
            "slot": 1,
            "name": "小玉",
            "prompt": "用户已自定义的小玉描述",
            "created_at": "2026-01-01T00:00:00",
        }],
        "active_persona_slot": 1,
        "image_pool": [],
    }
    (tmp_path / "image_persona.json").write_text(json.dumps(old_state), encoding="utf-8")

    manager = PersonaManager(tmp_path)
    assert [item["name"] for item in manager.get()["persona_pool"]] == [
        "小玉", "祈枝枝o.o", "奶包菜", "燕子回来了", "蛋姐",
    ]
    assert manager.get()["persona_pool"][0]["prompt"] == "用户已自定义的小玉描述"
    assert manager.get_active_persona()["slot"] == 1
    assert json.loads((tmp_path / "image_persona.json").read_text(encoding="utf-8"))[
        "builtin_persona_catalog_version"
    ] == 2

    manager.remove_persona(2)
    reloaded = PersonaManager(tmp_path)
    assert "祈枝枝o.o" not in {item["name"] for item in reloaded.get()["persona_pool"]}
    assert len(reloaded.get()["persona_pool"]) == 4


def test_explicitly_empty_legacy_persona_pool_stays_empty(tmp_path):
    (tmp_path / "image_persona.json").write_text(
        json.dumps({"persona_pool": [], "image_pool": []}), encoding="utf-8",
    )
    manager = PersonaManager(tmp_path)
    assert manager.get()["persona_pool"] == []
    assert json.loads((tmp_path / "image_persona.json").read_text(encoding="utf-8"))[
        "builtin_persona_catalog_version"
    ] == 2


def test_persona_mode_hides_image_without_deleting_it(tmp_path):
    manager = PersonaManager(tmp_path)
    manager.save_reference_image(PNG, "image/png")
    path = manager.data["ref_image_path"]
    manager.set_identity_mode("persona")
    assert manager.get_reference_path() == ""
    assert manager.get_reference_image() is None
    assert not manager.has_reference_image()
    assert manager.data["ref_image_path"] == path
    assert manager.get_active_persona()["name"] == "小玉"


def test_persona_validation_and_explicit_empty_pool(tmp_path):
    manager = PersonaManager(tmp_path)
    manager.add_persona("测试", "成年女性角色")
    with pytest.raises(ValueError):
        manager.update_persona(1, name=" ")
    with pytest.raises(ValueError):
        manager.update_persona(1, prompt="x" * 6001)
    with pytest.raises(ValueError):
        manager.select_persona(True)

    state = manager.get()
    state["persona_pool"] = []
    (tmp_path / "image_persona.json").write_text(json.dumps(state), encoding="utf-8")
    restored = PersonaManager(tmp_path)
    assert restored.get_active_persona() is None


def test_legacy_single_image_migrates_to_slot_one(tmp_path):
    image_dir = tmp_path / "image-persona"
    image_dir.mkdir()
    path = image_dir / "legacy.png"
    path.write_bytes(PNG)
    (tmp_path / "image_persona.json").write_text(
        json.dumps({"ref_image_path": str(path), "ref_mime_type": "image/png"}),
        encoding="utf-8",
    )
    manager = PersonaManager(tmp_path)
    assert manager.data["active_image_slot"] == 1
    assert manager.list_image_pool()[0]["slot"] == 1
    assert manager.get_reference_image()["data"] == PNG


def test_full_image_pool_retains_numbers_and_reuses_three_after_reload(tmp_path):
    manager = PersonaManager(tmp_path)
    for slot in range(1, 11):
        manager.save_reference_image(PNG + bytes([slot]))
    before = manager.get()
    with pytest.raises(ValueError, match="最多 10"):
        manager.save_reference_image(PNG)
    assert manager.get() == before
    manager.remove_reference_image(3)
    assert [entry["slot"] for entry in manager.list_image_pool()] == [1, 2, 4, 5, 6, 7, 8, 9, 10]
    manager = PersonaManager(tmp_path)
    manager.save_reference_image(PNG + b"replacement")
    assert manager.get()["active_image_slot"] == 3
    assert manager.get_reference_image()["data"] == PNG + b"replacement"
    manager.select_reference_image(9)
    assert manager.get_reference_image()["data"] == PNG + bytes([9])


def test_persona_capacity_reuses_holes_and_empty_pool_stays_empty(tmp_path):
    manager = PersonaManager(tmp_path)
    while len(manager.get()["persona_pool"]) < 10:
        count = len(manager.get()["persona_pool"])
        manager.add_persona(f"person-{count}", f"description-{count}")
    with pytest.raises(ValueError, match="最多 10"):
        manager.add_persona("overflow", "description")
    manager.select_persona(8)
    manager.remove_persona(3)
    manager.add_persona("replacement", "replacement-description")
    assert manager.get_active_persona()["slot"] == 8
    assert next(item for item in manager.get()["persona_pool"] if item["slot"] == 3)["name"] == "replacement"
    for slot in range(1, 11):
        manager.remove_persona(slot)
    manager = PersonaManager(tmp_path)
    assert manager.get()["persona_pool"] == []
    manager.add_persona("first", "first-description")
    assert manager.get_active_persona()["slot"] == 1


def test_failed_writes_preserve_image_files_and_pool_state(tmp_path, monkeypatch):
    manager = PersonaManager(tmp_path)
    manager.save_reference_image(PNG)
    before = manager.get()
    original_file = Path(manager.get_reference_path())
    persisted = Path(manager.file_path).read_bytes()

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(manager, "save", fail)
    for action in (
        lambda: manager.save_reference_image(PNG + b"new"),
        lambda: manager.remove_reference_image(1),
        lambda: manager.set_identity_mode("persona"),
        lambda: manager.add_persona("new", "new-description"),
        lambda: manager.remove_persona(1),
        lambda: manager.update_persona(1, name="changed"),
    ):
        with pytest.raises(OSError, match="disk full"):
            action()
        assert manager.get() == before
        assert original_file.read_bytes() == PNG
        assert list(Path(manager.image_dir).iterdir()) == [original_file]
        assert Path(manager.file_path).read_bytes() == persisted


def test_missing_image_reserves_its_slot(tmp_path):
    manager = PersonaManager(tmp_path)
    manager.save_reference_image(PNG)
    Path(manager.get_reference_path()).unlink()
    manager = PersonaManager(tmp_path)
    assert manager.identity_pool_payload()["image_pool"][0]["available"] is False
    manager.save_reference_image(PNG + b"second")
    assert manager.get()["active_image_slot"] == 2


def _command(plugin, handler, text):
    async def run():
        event = SimpleNamespace(message_str=text, plain_result=lambda value: value)
        return [item async for item in getattr(plugin, handler)(event)]

    return asyncio.run(run())


def _plugin(tmp_path):
    plugin = object.__new__(SelfieImagePlugin)
    plugin.persona = PersonaManager(tmp_path)
    plugin.config = AICatConfig.from_dict({"image": {"use_logo_when_no_persona": False}})
    return plugin


def test_image_commands_switch_remove_and_refill_same_slot(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    for slot in range(1, 4):
        plugin.persona.save_reference_image(PNG + bytes([slot]), "image/png")
    assert "已切换到形象图 2" in _command(plugin, "cmd_persona_set", "/形象设置 2")[0]
    assert plugin.persona.get_reference_image()["data"] == PNG + bytes([2])
    _command(plugin, "cmd_persona_remove_slot", "/形象移除 2")
    assert [item["slot"] for item in plugin.persona.list_image_pool()] == [1, 3]

    async def fetch(*args, **kwargs):
        return PNG + b"replacement", "image/png"

    monkeypatch.setattr("astrbot_plugin_selfie_image.main.fetch_image_source", fetch)
    reply = _command(plugin, "cmd_persona_set", "/形象设置 https://example.test/new.png")[0]
    assert "编号 2" in reply
    assert plugin.persona.get()["active_image_slot"] == 2
    assert plugin.persona.get_reference_image()["data"] == PNG + b"replacement"
    assert [item["slot"] for item in PersonaManager(tmp_path).list_image_pool()] == [1, 2, 3]


def test_identity_pool_switch_command_selects_mode_without_changing_pool_contents(tmp_path):
    plugin = _plugin(tmp_path)
    plugin.persona.save_reference_image(PNG, "image/png")
    plugin.persona.select_persona(1)

    reply = _command(plugin, "cmd_identity_pool_switch", "/形象切换 人设池")[0]
    assert "已切换到形象人设池" in reply
    assert plugin.persona.get_identity_mode() == "persona"

    reply = _command(plugin, "cmd_identity_pool_switch", "/形象切换 形象池")[0]
    assert "已切换到形象图池" in reply
    assert plugin.persona.get_identity_mode() == "image"
    assert len(plugin.persona.list_image_pool()) == 1
    assert plugin.persona.get_active_persona()["slot"] == 1

    reply = _command(plugin, "cmd_identity_pool_switch", "/形象切换 未知")[0]
    assert "用法：/形象切换 人设池 或 /形象切换 形象池" in reply
    assert plugin.persona.get_identity_mode() == "image"


def test_text_persona_commands_select_remove_and_refill_hole(tmp_path):
    plugin = _plugin(tmp_path)
    first_custom_slot = next(
        slot for slot in range(1, 11)
        if slot not in {item["slot"] for item in plugin.persona.get()["persona_pool"]}
    )
    reply = _command(plugin, "cmd_persona_pool_add", "/形象人设添加 测试形象 成年女性，黑色长直发")[0]
    assert f"编号 {first_custom_slot}" in reply
    _command(plugin, "cmd_persona_pool_add", "/形象人设添加 名称=第三形象 描述=成年女性，浅棕卷发")
    second_custom_slot = first_custom_slot + 1
    _command(plugin, "cmd_persona_pool_select", f"/形象人设设置 {second_custom_slot}")
    assert plugin.persona.get_active_persona()["name"] == "第三形象"
    _command(plugin, "cmd_persona_pool_remove", f"/形象人设移除 {first_custom_slot}")
    reply = _command(plugin, "cmd_persona_pool_add", "/形象人设添加 替补形象 成年女性，齐肩短发")[0]
    assert f"编号 {first_custom_slot}" in reply
    assert plugin.persona.get_active_persona()["slot"] == second_custom_slot
    before = plugin.persona.get()
    assert "用法" in _command(plugin, "cmd_persona_pool_remove", "/形象人设移除 11")[0]
    assert plugin.persona.get() == before


def test_selfie_cos_and_group_only_use_selected_pool_image(tmp_path):
    plugin = _plugin(tmp_path)
    for slot in range(1, 4):
        plugin.persona.save_reference_image(PNG + bytes([slot]), "image/png")
    plugin.persona.select_reference_image(2)
    for action in ("自拍", "COS换装", "看看COS", "看看腿", "合影"):
        selection = plugin._select_selfie_references(action, [])
        refs = plugin._selfie_reference_images(action, [], selection)
        assert [ref.data for ref in refs] == [PNG + bytes([2])]
    assert [ref.data for ref in plugin._select_group_references([]).refs] == [PNG + bytes([2])]
    plugin.persona.set_identity_mode("persona")
    assert plugin._selfie_reference_images("自拍", []) == []
    assert plugin._select_group_references([]).refs == []


def test_all_pool_images_remain_stable_references_when_mode_or_selection_changes(tmp_path):
    plugin = _plugin(tmp_path)
    for slot in range(1, 4):
        plugin.persona.save_reference_image(PNG + bytes([slot]), "image/png")
    paths = {item["path"] for item in plugin.persona.list_image_pool()}
    plugin.persona.select_reference_image(1)
    assert plugin._stable_reference_paths() == paths
    plugin.persona.set_identity_mode("persona")
    assert plugin._stable_reference_paths() == paths
