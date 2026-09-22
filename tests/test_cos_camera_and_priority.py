import re
import tempfile
from unittest.mock import patch
from pathlib import Path

from astrbot_plugin_selfie_image.cos.cos_looks import (
    COS_FRAMING_CLASSES,
    COS_LOOK_SETS,
    audit_cos_look_catalog,
    build_cos_look_action,
    build_cos_third_person_prompt,
    cos_prompt_has_framing,
    match_cos_look_sets,
    normalize_cos_prompt_negatives,
    pick_cos_framing,
    pick_cos_variation,
)
from astrbot_plugin_selfie_image.core.providers import ImageReference
from astrbot_plugin_selfie_image.features.reference_collector import (
    select_cos_references,
    select_group_references,
)
from astrbot_plugin_selfie_image.features.persona import (
    appearance_type_instruction,
    group_style_lines,
)
from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt
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


def test_cos_catalog_audit_allows_costume_features_and_rejects_identity_locks():
    assert audit_cos_look_catalog(COS_LOOK_SETS) == []

    safe = [
        {
            "id": "safe-costume",
            "prompt": "银白色假发、蓝色瞳色、黑色眼罩和轻度角色妆容，服装细节清晰。",
        }
    ]
    assert audit_cos_look_catalog(safe) == []

    risky = [
        {
            "id": "risky-identity",
            "prompt": "固定脸型为瓜子脸，固定五官比例和固定肤色，严格按角色原画的脸生成。",
        }
    ]
    findings = audit_cos_look_catalog(risky)
    assert {finding["id"] for finding in findings} == {"risky-identity"}
    assert {finding["risk"] for finding in findings} == {
        "face_shape",
        "facial_proportions",
        "skin_tone",
        "character_face",
    }


def test_cos_negative_normalization_deduplicates_shared_semantics():
    prompt = (
        "严格换装为测试COS：真实布料。"
        "不要额外人物、文字、字幕和水印。"
        "禁止动漫插画、游戏立绘、3D渲染、塑料皮肤、超现实发光和水印。"
        "不要第二个人、可读文字、视频字幕或品牌水印。"
    )
    normalized = normalize_cos_prompt_negatives(prompt)

    assert normalized.count("额外人物") == 1
    assert normalized.count("文字") == 1
    assert normalized.count("字幕") == 1
    assert normalized.count("水印") == 1
    assert normalized.count("动漫插画") == 1
    assert normalized.count("游戏立绘") == 1
    assert normalized.count("3D渲染") == 1
    assert normalized.count("塑料皮肤") == 1
    assert normalized.count("超现实发光") == 1


def test_corrected_cos_identity_details_are_present_in_final_catalog():
    prompts = {item["id"]: item["prompt"] for item in COS_LOOK_SETS}
    assert "紫色眼睛" in prompts["azur_lane_enterprise"]
    assert "银白色长发" in prompts["azur_lane_belfast"]
    assert "紫色眼睛" in prompts["azur_lane_belfast"]
    assert "飞鸟马时" in prompts["blue_archive_toki"]
    assert "金色长发" in prompts["blue_archive_toki"]
    assert "黑色长发" not in prompts["blue_archive_toki"]
    assert "蔚蓝档案" in prompts["shiroko_black_white_tracksuit"]
    assert "蔚蓝档案" in prompts["ibuki_red_white_sportswear"]
    assert "白色长发" in prompts["blue_archive_hina"]
    assert "紫色眼睛" in prompts["blue_archive_hina"]
    assert "粉色长发" in prompts["blue_archive_aru"]
    assert "红色短发" in prompts["blue_archive_junko"]
    assert "黑色短发" in prompts["blue_archive_tsubaki"]
    assert "金色齐肩双马尾" in prompts["blue_archive_chinatsu"]
    assert "深棕色短发" in prompts["blue_archive_izuna"]
    assert "黑色及膝长发" in prompts["baizhi_research_white_blue"]
    assert "白色长发" in prompts["camellya_red_rose"]
    assert "银白色长卷发" in prompts["carlotta_frost_portrait"]
    assert "深棕色长发" in prompts["wuthering_zhezhi_ink"]
    assert "深红色长发" in prompts["mushoku_tensei_aisha"]
    assert "绿色眼睛" in prompts["mushoku_tensei_aisha"]
    assert "洛可可" in prompts["wuthering_roccia_stage"]
    assert "釉瑚" in prompts["wuthering_youhu_antique"]


def test_cos_reference_selection_keeps_persona_first_without_identity_intent():
    persona = ImageReference(data=b"persona", mime_type="image/png")
    attached = ImageReference(data=b"attached", mime_type="image/png")

    selected = select_cos_references(persona, [attached], action="换成这套服装")

    assert selected.identity_source == "persona"
    assert selected.identity_ref is persona
    assert selected.refs == [persona, attached]
    assert selected.identity_reference_count == 1
    assert selected.extra_reference_image_count == 1


def test_group_reference_selection_keeps_persona_identity_and_attached_image_as_extra():
    persona = ImageReference(data=b"persona", mime_type="image/jpeg")
    anime = ImageReference(data=b"anime", mime_type="image/png")

    selected = select_group_references(persona, [anime])

    assert selected.identity_source == "persona"
    assert selected.identity_ref is persona
    assert selected.refs == [persona, anime]
    assert selected.summary()["used_persona"] is True
    assert selected.summary()["roles"] == {"persona": 1, "message": 1}


def test_group_reference_selection_does_not_promote_attached_image_without_persona():
    anime = ImageReference(data=b"anime", mime_type="image/png")

    selected = select_group_references(None, [anime])

    assert selected.identity_source == "none"
    assert selected.identity_ref is None
    assert selected.refs == [anime]
    assert selected.identity_reference_count == 0



def test_auto_group_media_policy_never_lets_extra_reference_choose_the_main_medium():
    instruction = appearance_type_instruction("auto", has_reference_image=True)
    style_lines = group_style_lines("auto")

    assert "参考图一" in instruction
    assert "不得改变主角媒介" in instruction
    assert all("由模型根据主角形象与参考图自行判断" not in line for line in style_lines)
    assert any("主形象参考图" in line for line in style_lines)
    assert any("额外参考图" in line and "媒介" in line for line in style_lines)


def test_auto_english_group_prompt_uses_primary_reference_as_media_authority():
    prompt = build_selfie_builtin_prompt(
        "合影 / 合照 / 同框",
        language="en",
        has_reference_image=True,
        extra_reference_count=1,
        appearance_type="auto",
    )

    assert "primary identity reference" in prompt
    assert "must not override the primary subject's visual medium" in prompt
    assert "visually consistent" not in prompt


def test_cos_reference_selection_promotes_attached_face_when_explicitly_requested():
    persona = ImageReference(data=b"persona", mime_type="image/png")
    attached = ImageReference(data=b"attached", mime_type="image/png")

    selected = select_cos_references(
        persona,
        [attached],
        action="以附图人物脸为准，按这套服装生成",
    )

    assert selected.identity_source == "message"
    assert selected.identity_ref is attached
    assert selected.refs == [attached]
    assert selected.extra_reference_image_count == 0


def test_cos_reference_selection_uses_first_message_image_without_persona():
    first = ImageReference(data=b"first", mime_type="image/png")
    second = ImageReference(data=b"second", mime_type="image/png")

    selected = select_cos_references(None, [first, second], action="看看COS")

    assert selected.identity_source == "message"
    assert selected.identity_ref is first
    assert selected.refs == [first, second]
    assert selected.identity_reference_count == 1
    assert selected.extra_reference_image_count == 1


def test_cos_reference_selection_dry_run_matrix_has_stable_identity_and_prompt_contract():
    from astrbot_plugin_selfie_image.features.persona import PersonaManager
    from astrbot_plugin_selfie_image.prompts.prompt_templates import (
        COS_IDENTITY_CONTRACT_ZH,
        build_selfie_builtin_prompt,
    )

    persona = ImageReference(data=b"persona", mime_type="image/png")
    attached = ImageReference(data=b"attached", mime_type="image/png")
    duplicate = ImageReference(data=b"attached", mime_type="image/jpeg")
    action = build_cos_look_action(
        "",
        picker=lambda **_: {
            "id": "dry_run_cos",
            "title": "dry-run 测试套装",
            "prompt": "严格换装为测试 COS：银白色假发、蓝色眼妆、黑色眼罩，人物自然站立。",
        },
        camera="first",
    )

    cases = [
        ("persona", persona, [], [persona]),
        ("persona", persona, [attached], [persona, attached]),
        ("message", persona, [attached], [attached]),
        ("message", None, [attached, duplicate], [attached]),
        ("none", None, [], []),
    ]
    for source, persona_ref, message_refs, expected_refs in cases:
        selected = select_cos_references(
            persona_ref,
            message_refs,
            action="以附图人物脸为准" if source == "message" and persona_ref else action,
        )
        assert selected.identity_source == source
        assert selected.refs == expected_refs
        assert selected.summary()["identity_reference_source"] == source

        with tempfile.TemporaryDirectory() as tmp:
            prompt = PersonaManager(tmp).build_selfie_prompt(
                action,
                "小助",
                "温柔",
                bool(selected.identity_ref),
                selected.extra_reference_image_count,
            )
        if source == "none":
            assert COS_IDENTITY_CONTRACT_ZH not in prompt
        else:
            assert prompt.count(COS_IDENTITY_CONTRACT_ZH) == 1
        english = build_selfie_builtin_prompt(
            action,
            language="en",
            has_reference_image=bool(selected.identity_ref),
            extra_reference_count=selected.extra_reference_image_count,
            appearance_type="real",
        )
        assert english.count(action) == 1


def test_cos_reference_selection_does_not_use_logo_or_other_fallback_when_empty():
    selected = select_cos_references(None, [], action="看看COS")

    assert selected.identity_source == "none"
    assert selected.identity_ref is None
    assert selected.refs == []
    assert selected.identity_reference_count == 0
    assert selected.raw_total_count == 0


def test_cos_reference_selection_deduplicates_without_replacing_identity_first():
    attached = ImageReference(data=b"attached", mime_type="image/png")
    duplicate = ImageReference(data=b"attached", mime_type="image/jpeg")
    extra = ImageReference(data=b"extra", mime_type="image/png")

    selected = select_cos_references(None, [attached, duplicate, extra], action="看看COS")

    assert selected.identity_ref is attached
    assert selected.refs == [attached, extra]
    assert selected.raw_total_count == 3
    assert selected.deduplicated_total_count == 2
    assert selected.duplicate_count == 1


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
    with patch("astrbot_plugin_selfie_image.cos.cos_looks.random.random", return_value=0.25):
        selected = pick_cos_framing(extra_request="COS 换装：自然站立")
    assert selected["view_id"]
    assert selected["prompt"] in {item["prompt"] for item in COS_FRAMING_CLASSES.values()}
    assert not cos_prompt_has_framing("COS 换装：自然站立")
    assert cos_prompt_has_framing("COS 换装：竖屏三分之四侧身全身构图")
    assert pick_cos_framing(extra_request="COS 换装：竖屏三分之四侧身全身构图")["prompt"] == ""


def test_outfit_framing_words_do_not_suppress_random_view():
    with patch("astrbot_plugin_selfie_image.cos.cos_looks.random.random", return_value=0.25), patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        return_value="low_angle_full_2",
    ):
        selected = pick_cos_framing(
            outfit="竖屏9:16，半身到全身构图，人物对镜站立",
            extra_request="COS 换装：自然站立",
        )
    assert selected["view_id"] == "low_angle_full_2"
    assert selected["prompt"] == COS_FRAMING_CLASSES["low_angle_full_2"]["prompt"]
    assert selected["outfit_text"] == "竖屏9:16，半身到全身构图，人物对镜站立"


def test_random_cos_pose_is_optional_and_scene_is_never_injected():
    from unittest.mock import patch

    with patch("astrbot_plugin_selfie_image.cos.cos_looks.random.random", return_value=0.25):
        selected = pick_cos_variation("blue_archive_haruka")
    assert selected["pose_id"]
    assert selected["scene_id"] == ""
    assert "随机姿势" in selected["prompt"]

    with patch("astrbot_plugin_selfie_image.cos.cos_looks.random.random", return_value=0.75):
        selected = pick_cos_variation("blue_archive_haruka")
    assert selected == {"pose_id": "", "scene_id": "", "prompt": ""}


def test_random_cos_framing_is_optional_when_prompt_has_no_view():
    from unittest.mock import patch

    with patch("astrbot_plugin_selfie_image.cos.cos_looks.random.random", return_value=0.25), patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=lambda values: values[0],
    ):
        selected = pick_cos_framing(extra_request="COS 换装：自然站立")
    assert selected["view_id"]

    with patch("astrbot_plugin_selfie_image.cos.cos_looks.random.random", return_value=0.75):
        selected = pick_cos_framing(extra_request="COS 换装：自然站立")
    assert selected["view_id"] == ""
    assert selected["prompt"] == ""


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


def test_cos_identity_contract_is_emitted_once_in_chinese_prompt():
    from astrbot_plugin_selfie_image.features.persona import PersonaManager
    from astrbot_plugin_selfie_image.prompts.prompt_templates import COS_IDENTITY_CONTRACT_ZH

    item = {
        "id": "canonical_identity_zh",
        "title": "身份合同中文测试",
        "prompt": "严格换装为测试 COS：银白色假发、白色短装、人物自然站立。",
    }
    action = build_cos_look_action("", picker=lambda **_: item, camera="first")

    with tempfile.TemporaryDirectory() as tmp:
        prompt = PersonaManager(tmp).build_selfie_prompt(action, "小助", "温柔", True, 0)

    assert prompt.count(COS_IDENTITY_CONTRACT_ZH) == 1
    assert "保持参考图中同一人的脸型" not in action
    assert "本次套装：身份合同中文测试。" in prompt


def test_cos_identity_contract_is_emitted_once_in_english_prompt():
    from astrbot_plugin_selfie_image.prompts.prompt_templates import (
        COS_IDENTITY_CONTRACT_EN,
        build_selfie_builtin_prompt,
    )

    item = {
        "id": "canonical_identity_en",
        "title": "身份合同英文测试",
        "prompt": "严格换装为测试 COS：银白色假发、白色短装、人物自然站立。",
    }
    action = build_cos_look_action("", picker=lambda **_: item, camera="first")
    prompt = build_selfie_builtin_prompt(
        action,
        language="en",
        has_reference_image=True,
        appearance_type="real",
    )

    assert prompt.count(COS_IDENTITY_CONTRACT_EN) == 1
    assert prompt.count(action) == 1
    assert "Preserve the reference face outline" not in action


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
    with patch("astrbot_plugin_selfie_image.cos.cos_looks.random.random", return_value=0.25):
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
    with patch("astrbot_plugin_selfie_image.cos.cos_looks.random.random", return_value=0.25):
        action = build_cos_look_action("", picker=lambda **_: item)
    assert "本次未指定视角，随机采用" in action
    assert "该随机机位优先于套装正文中的通用景别和机位描述" in action
    assert "正面或三分之四侧身" in action
    assert "近景至全身" in action


def test_selfie_command_cos_request_does_not_use_cos_pool():
    from astrbot_plugin_selfie_image.main import SelfieImagePlugin

    plugin = object.__new__(SelfieImagePlugin)
    action = plugin._build_selfie_request_action(
        "严格换装为测试角色 COS，人物站在花园中。",
        False,
    )

    assert "【COS换装" not in action
    assert "【cos:" not in action
    assert "【自拍 / 看看模式】" in action
    assert len(re.findall(r"【shot:[a-z_]+】", action)) == 1
    assert action.count("用户补充要求优先：") == 1


def test_private_hoshino_and_sunna_cos_outfits_are_registered():
    looks = {item["id"]: item for item in COS_LOOK_SETS}
    hoshino = looks["blue_archive_hoshino_private_gym"]
    hoshino_beach = looks["blue_archive_hoshino_private_beach"]
    sunna = looks["zzz_sunna_private_sportswear"]

    assert hoshino["cos_type"] == "蔚蓝档案"
    assert "私设体操服" in hoshino["title"]
    assert "黑白亮青绿拼色宽松运动夹克" in hoshino["prompt"]
    assert "黑色贴身运动短裤" in hoshino["prompt"]
    assert hoshino_beach["cos_type"] == "蔚蓝档案"
    assert "海边荷叶边泳装" in hoshino_beach["title"]
    assert "白色高腰多层荷叶边短裙" in hoshino_beach["prompt"]
    assert "安全裤" not in hoshino_beach["prompt"]
    assert "内衬" not in hoshino_beach["prompt"]
    assert sunna["cos_type"] == "绝区零"
    assert "妄想天使千夏（Sunna）" in sunna["prompt"]
    assert "浅粉色修身无袖背心" in sunna["prompt"]
    assert "黑色宽松运动短裤" in sunna["prompt"]

    assert {item["id"] for item in match_cos_look_sets("星野")} == {
        "blue_archive_hoshino",
        "blue_archive_hoshino_private_gym",
    }
    assert [item["id"] for item in match_cos_look_sets("星野海边")] == [
        "blue_archive_hoshino_private_beach"
    ]
    assert [item["id"] for item in match_cos_look_sets("星野泳装")] == [
        "blue_archive_hoshino_private_beach"
    ]
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


def test_original_blue_stage_cos_outfit_is_registered():
    looks = {item["id"]: item for item in COS_LOOK_SETS}
    stage = looks["original_blue_stage_opera"]
    assert stage["title"] == "原创COS·蓝色戏曲舞台服"
    assert stage["cos_type"] == "原创COS"
    assert "原创国风戏曲舞台服" in stage["prompt"]
    assert "蓝黑色古风帽冠" in stage["prompt"]
    assert "红色长流苏" in stage["prompt"]
    assert [item["id"] for item in match_cos_look_sets("蓝色戏曲舞台服")] == [
        "original_blue_stage_opera"
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
