import re
import tempfile
from unittest.mock import patch
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


def test_default_cos_pool_actions_randomize_camera_and_keep_contracts_clean():
    for item in COS_LOOK_SETS:
        action = build_cos_look_action("", picker=lambda item=item, **_: item)
        camera = re.search(r"【cam:(selfie|first|third)】", action).group(1)
        assert camera in {"selfie", "first", "third"}
        if camera == "third":
            assert "【他拍 / 看看COS模式】" in action
            assert "自拍" not in action
            assert "镜前" not in action
            assert "镜子" not in action
            assert "对镜" not in action
            assert "两条手臂和两只手" in action
        elif camera == "first":
            assert "【第一视角 / 看看COS模式】" in action
            assert "手持设备" in action
            assert "自拍" not in action
        else:
            assert "【自拍 / 看看COS模式】" in action


def test_cos_camera_parser_and_avoid_support_first_person():
    from astrbot_plugin_selfie_image.cos.cos_looks import parse_requested_cos_camera, pick_cos_camera

    assert parse_requested_cos_camera("第一视角") == "first"
    assert parse_requested_cos_camera("第一人称视角") == "first"
    assert parse_requested_cos_camera("自拍视角") == "selfie"
    assert pick_cos_camera(camera="first") == "first"
    assert pick_cos_camera(avoid="third") in {"first", "selfie"}


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
    assert "正面" in selected["prompt"] or "三分之四侧身" in selected["prompt"]
    assert "近景" in selected["prompt"] or "全身" in selected["prompt"]
    resolved = selected["extra_request_text"]
    assert "正面或三分之四侧身" not in resolved
    assert "近景至全身" not in resolved


def test_cos_framing_alternatives_return_one_complete_framing_contract():
    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=lambda values: values[0],
    ):
        selected = pick_cos_framing(
            extra_request="人物正面或三分之四侧身站立，近景至全身构图"
        )

    assert selected["randomized_from_options"] is True
    assert selected["prompt"] == "正面站立；近景构图"
    assert "或" not in selected["extra_request_text"]
    assert "至" not in selected["extra_request_text"]
    assert "三分之四" not in selected["prompt"]
    assert "全身" not in selected["prompt"]
    assert selected["extra_request_text"]


def test_cos_english_builtin_keeps_the_complete_action_once():
    item = {
        "id": "english_cos_details",
        "title": "英文套装细节测试",
        "prompt": "严格换装为测试 COS：银白色长发，白色蓬袖短装，人物站在暖色室内，一手轻扶腰间配饰。",
    }
    action = build_cos_look_action(
        "",
        picker=lambda **_: item,
        camera="first",
    )
    from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt

    prompt = build_selfie_builtin_prompt(
        action,
        language="en",
        has_reference_image=True,
        appearance_type="real",
    )

    assert prompt.count(action) == 1
    assert "银白色长发" in prompt
    assert "白色蓬袖短装" in prompt
    assert "【cam:first】" in prompt


def test_cos_english_builtin_uses_translated_action_contract_once():
    action = "【第一视角 / 看看COS模式】严格换装为测试 COS：银白色长发。 【cos:translated】 【cam:first】"
    translated_action = "Create the locked test COS outfit: silver-white long hair. [cos:translated] [cam:first]"
    from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt

    prompt = build_selfie_builtin_prompt(
        action,
        language="en",
        action_content=translated_action,
        has_reference_image=True,
        appearance_type="real",
    )

    assert prompt.count(translated_action) == 1
    assert translated_action in prompt
    assert "银白色长发" not in prompt


def test_cos_english_builtin_does_not_repeat_user_supplement():
    action = (
        "【自拍 / 看看COS模式】严格换装为测试 COS：银白色长发。"
        " 用户补充要求优先：站在窗边。 【cos:translated】 【cam:selfie】"
    )
    translated_action = (
        "Create the locked test COS outfit: silver-white long hair."
        " User supplement: stand by a window. [cos:translated] [cam:selfie]"
    )
    from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt

    prompt = build_selfie_builtin_prompt(
        action,
        language="en",
        action_content=translated_action,
        user_text="stand by a window",
        has_reference_image=True,
        appearance_type="real",
    )

    assert prompt.count("stand by a window") == 1


def test_studio_cos_english_path_keeps_translated_action_contract():
    source = Path(__file__).resolve().parents[1] / "studio" / "studio_adapter.py"
    text = source.read_text(encoding="utf-8")
    block = text.split("if mode in {\"group\", \"selfie\"}:", 1)[1].split(
        'failure_stage = "generating"', 1
    )[0]

    assert "extract_generated_action_contract" in block
    assert "translated_action" in block
    assert "action_content=translated_action" in block


def test_compact_record_derives_legacy_cos_view_from_cam():
    from astrbot_plugin_selfie_image.core.utils import compact_generation_record

    compact = compact_generation_record(
        {
            "success": True,
            "original_prompt": "【cos:test】 【cam:first】",
            "request_data": {"request_prompt": "【cos:test】 【cam:first】"},
        }
    )

    assert compact["cos_view"] == "first"
    assert compact["request_data"]["cos_view"] == "first"


def test_generated_cos_action_is_not_appended_again_as_user_request():
    from astrbot_plugin_selfie_image.features.persona import PersonaManager

    item = {
        "id": "repro_duplicate_prompt",
        "title": "重复拼接测试",
        "prompt": "严格换装为测试 COS：人物自然站立，正面或三分之四侧身，近景至全身构图。",
    }
    action = build_cos_look_action(
        "",
        picker=lambda **_: item,
        camera="first",
    )
    with tempfile.TemporaryDirectory() as tmp:
        prompt = PersonaManager(tmp).build_selfie_prompt(action, "小助", "温柔", True, 0)

    assert prompt.count(action) == 1
    assert prompt.count("【COS换装第一视角模式】") == 1
    assert "用户要求：按以上已锁定的 COS 套装、相机、构图、姿势和场景规则生成。" in prompt
    assert "第三人称摄影画面" not in prompt
    assert "摄影师在画面外" not in prompt


def test_generated_cos_action_exposes_one_camera_tag_and_one_concrete_framing():
    item = {
        "id": "repro_single_view",
        "title": "单一视角测试",
        "prompt": "严格换装为测试 COS：人物自然站立。",
    }
    action = build_cos_look_action(
        "",
        picker=lambda **_: item,
        camera="first",
    )

    assert re.findall(r"【(?:cam|cos_view):[^】]+】", action) == ["【cam:first】"]
    assert action.count("本次未指定视角，随机采用") == 1
    assert "视角按本套 COS 套装描述执行" not in action


def test_first_person_cos_action_is_not_treated_as_freeform_user_text():
    from astrbot_plugin_selfie_image.features.persona import PersonaManager, extract_user_action_text
    from astrbot_plugin_selfie_image.prompts.prompt_templates import extract_user_prompt

    item = {
        "id": "repro_first_person_marker",
        "title": "第一视角标记测试",
        "prompt": "严格换装为测试 COS：人物自然站立。",
    }
    action = build_cos_look_action(
        "",
        picker=lambda **_: item,
        camera="first",
    )

    assert extract_user_action_text(action) == ""
    assert extract_user_prompt(action) == ""


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


def test_selfie_command_cos_request_uses_cos_builder_once():
    from astrbot_plugin_selfie_image.main import SelfieImagePlugin

    plugin = object.__new__(SelfieImagePlugin)
    action = plugin._build_selfie_request_action(
        "严格换装为测试角色 COS，人物站在花园中。",
        False,
    )

    assert "【COS换装" not in action
    assert "【cos:" in action
    assert len(re.findall(r"【cam:(?:selfie|first|third)】", action)) == 1
    assert "【自拍 / 看看模式】" not in action
    assert action.count("用户补充要求优先：") == 1


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
