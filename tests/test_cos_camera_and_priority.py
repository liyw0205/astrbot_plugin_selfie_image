from pathlib import Path

from astrbot_plugin_selfie_image.cos.cos_looks import (
    COS_FRAMING_CLASSES,
    COS_LOOK_SETS,
    build_cos_look_action,
    build_cos_third_person_prompt,
    cos_prompt_has_framing,
    match_cos_look_sets,
    pick_cos_framing,
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


def test_unspecified_cos_framing_picks_one_concrete_view():
    selected = pick_cos_framing(extra_request="COS 换装：自然站立")
    assert selected["view_id"]
    assert selected["prompt"] in {item["prompt"] for item in COS_FRAMING_CLASSES.values()}
    assert not cos_prompt_has_framing("COS 换装：自然站立")
    assert cos_prompt_has_framing("COS 换装：竖屏三分之四侧身全身构图")
    assert pick_cos_framing(extra_request="COS 换装：竖屏三分之四侧身全身构图")["prompt"] == ""


def test_cos_framing_alternatives_are_resolved_before_prompt_generation():
    selected = pick_cos_framing(
        extra_request="COS 换装：正面或三分之四侧身站立，近景至全身构图"
    )
    assert selected["randomized_from_options"] is True
    assert selected["prompt"] in {"正面", "三分之四侧身", "近景", "全身"}
    resolved = selected["extra_request_text"]
    assert "正面或三分之四侧身" not in resolved
    assert "近景至全身" not in resolved


def test_cos_action_replaces_outfit_view_alternatives():
    item = {
        "id": "framing_test",
        "title": "视角测试",
        "prompt": "严格换装为测试COS：人物正面或三分之四侧身站立，采用近景至全身构图。",
    }
    action = build_cos_look_action("", picker=lambda **_: item)
    assert "本次从已有视角选项中随机确定为" in action
    assert "正面或三分之四侧身" not in action
    assert "近景至全身" not in action


def test_private_hoshino_and_sunna_cos_outfits_are_registered():
    looks = {item["id"]: item for item in COS_LOOK_SETS}
    hoshino = looks["blue_archive_hoshino_private_gym"]
    sunna = looks["zzz_sunna_private_sportswear"]

    assert hoshino["cos_type"] == "蔚蓝档案"
    assert "私设体操服" in hoshino["title"]
    assert "黑白亮青绿拼色宽松运动夹克" in hoshino["prompt"]
    assert "黑色贴身运动短裤" in hoshino["prompt"]
    assert sunna["cos_type"] == "绝区零"
    assert "妄想天使千夏（Sunna）" in sunna["prompt"]
    assert "浅粉色修身无袖背心" in sunna["prompt"]
    assert "黑色宽松运动短裤" in sunna["prompt"]

    assert {item["id"] for item in match_cos_look_sets("星野")} == {
        "blue_archive_hoshino",
        "blue_archive_hoshino_private_gym",
    }
    assert {item["id"] for item in match_cos_look_sets("千夏")} == {
        "blue_archive_chinatsu",
        "zzz_sunna_private_sportswear",
    }
    assert [item["id"] for item in match_cos_look_sets("蔚蓝档案 千夏")] == [
        "blue_archive_chinatsu"
    ]
    assert [item["id"] for item in match_cos_look_sets("绝区零 千夏")] == [
        "zzz_sunna_private_sportswear"
    ]


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
