from pathlib import Path

from astrbot_plugin_selfie_image.cos.cos_looks import (
    COS_LOOK_SETS,
    build_cos_look_action,
    build_cos_third_person_prompt,
)
from astrbot_plugin_selfie_image.studio.studio import build_studio_action, empty_session


def test_default_cos_pool_actions_are_third_person_and_camera_clean():
    for item in COS_LOOK_SETS:
        action = build_cos_look_action("", picker=lambda item=item, **_: item)
        assert "【cam:third】" in action
        assert "【cam:selfie】" not in action
        assert "自拍" not in action
        assert "镜前" not in action
        assert "镜子" not in action
        assert "对镜" not in action
        assert "两条手臂和两只手" in action


def test_studio_cos_prompt_uses_the_same_third_person_contract():
    session = empty_session("COS", template="clothes")
    session["graph"]["prompt"] = COS_LOOK_SETS[0]["prompt"]
    action = build_studio_action(session)
    assert "【cos:web】" in action
    assert "【cam:third】" in action
    assert "【他拍 / 看看COS模式】" in action
    assert "自拍" not in action
    assert "两条手臂和两只手" in action


def test_raw_web_cos_prompt_can_be_wrapped_without_selfie_semantics():
    action = build_cos_third_person_prompt("严格换装为角色 COS：人物在全身镜前自拍")
    assert "【cam:third】" in action
    assert "自拍" not in action
    assert "全身镜" not in action


def test_priority_picker_filters_already_selected_models():
    page = (Path(__file__).resolve().parents[1] / "pages/dashboard/index.html").read_text(
        encoding="utf-8"
    )
    assert "function refreshPriorityPicker(kind = 'image')" in page
    assert "!selected.some(item => priorityEntryMatchesLabel(item, label))" in page
    assert "暂无可加入模型" in page
    assert "refreshPriorityPicker(kind);" in page


def test_preset_manager_lists_are_switchable_sibling_views():
    page = (Path(__file__).resolve().parents[1] / "pages/dashboard/index.html").read_text(
        encoding="utf-8"
    )
    assert 'id="presetManagerViews"' in page
    assert 'id="presetPromptManager"' in page
    assert 'id="presetCosManager" class="preset-manager-view" hidden' in page
    assert ".preset-manager-view[hidden] { display: none !important; }" in page
    assert "promptManager.style.display = isCos ? 'none' : '';" in page
    assert "cosManager.style.display = isCos ? '' : 'none';" in page
    assert 'id="presetKindCos"' in page
