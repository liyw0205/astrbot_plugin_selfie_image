from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astrbot_plugin_selfie_image.cos.cos_looks import (
    _COS_AUTO_REFERENCE_NEGATIVE_PROMPT,
    _COS_ANIME_NEGATIVE_PROMPT,
    _COS_COMMON_NEGATIVE_PROMPT,
    build_cos_look_action,
    build_cos_third_person_prompt,
    finalize_cos_prompt_for_request,
    get_cos_look_by_id,
    normalize_cos_prompt_negatives,
)
from astrbot_plugin_selfie_image.features.persona import (
    PersonaManager,
    appearance_type_output_instruction,
    extract_user_action_text,
)
from astrbot_plugin_selfie_image.prompts.selfie_actions import (
    build_selfie_look_action,
    build_group_selfie_action,
    build_third_person_look_action,
)
from astrbot_plugin_selfie_image.prompts.prompt_templates import (
    build_selfie_builtin_prompt,
    extract_user_prompt,
    resolve_appearance_type,
)


def test_explicit_request_medium_overrides_saved_setting():
    assert resolve_appearance_type("real", "这次请改成动漫风格") == "anime"
    assert resolve_appearance_type("anime", "这次请生成真人照片") == "real"
    assert resolve_appearance_type("anime", "不要动漫风格") == "real"
    assert resolve_appearance_type("anime", "不要生成动漫风格") == "real"
    assert resolve_appearance_type("anime", "自然自拍") == "anime"
    assert resolve_appearance_type("invalid", "自然自拍") == "auto"


@pytest.mark.parametrize("configured,user_text,expected", [
    ("auto", "更真实一点", "real"),
    ("auto", "更加真实", "real"),
    ("auto", "提高真实感", "real"),
    ("auto", "像真人一样", "real"),
    ("anime", "跟真人一样", "real"),
    ("auto", "写实", "real"),
    ("auto", "make it look like a real person", "real"),
    ("auto", "more realistic", "real"),
    ("auto", "more lifelike", "real"),
    ("anime", "更真实一点，但保留动漫风格", "anime"),
    ("real", "anime style, more realistic lighting", "anime"),
    ("auto", "不要更真实一点", "auto"),
    ("auto", "不需要提高真实感", "auto"),
    ("auto", "do not make more realistic", "auto"),
    ("real", "don't use anime style", "real"),
    ("anime", "not realistic photography", "anime"),
    ("auto", "动漫插画，不生成真人摄影", "anime"),
    ("anime", "不生成动漫插画", "real"),
    ("real", "不采用真人", "anime"),
    ("anime", "和真人合影", "anime"),
    ("anime", "和 真人 合影", "anime"),
    ("anime", "真人 COSER 的服装细节", "anime"),
])
def test_realism_requests_respect_negation_and_explicit_medium(configured, user_text, expected):
    assert resolve_appearance_type(configured, user_text) == expected


@pytest.mark.parametrize("medium", ["real", "anime"])
def test_group_medium_scaffolding_is_not_extracted_as_user_text(medium):
    action = build_group_selfie_action(appearance_type=medium)
    assert extract_user_prompt(action) == ""
    assert extract_user_action_text(action) == ""
    action = build_group_selfie_action("保留红色围巾", appearance_type=medium)
    assert extract_user_prompt(action) == "保留红色围巾"
    assert extract_user_action_text(action) == "保留红色围巾"
    legacy = (
        "合影 / 合照 / 同框。用户补充要求：保留红色围巾。"
        + appearance_type_output_instruction(medium)
        + " 【shot:group_portrait】"
    )
    assert extract_user_prompt(legacy) == "保留红色围巾"
    assert extract_user_action_text(legacy) == "保留红色围巾"
    explicit_style = appearance_type_output_instruction(medium)
    action = build_group_selfie_action(explicit_style, appearance_type=medium)
    assert extract_user_prompt(action) == explicit_style.strip(" 。")


def test_cos_negative_contract_matches_selected_medium():
    item = {
        "id": "medium_test",
        "title": "媒介测试 COS",
        "cos_type": "测试",
        "prompt": "严格换装为测试角色 COS：真人 COSER，红色外套和黑色短裙。",
    }

    auto_action = build_cos_look_action(
        "",
        camera="third",
        picker=lambda **_: item,
    )
    assert _COS_COMMON_NEGATIVE_PROMPT in auto_action
    assert "真人 COSER" in auto_action

    anime_action = build_cos_look_action(
        "",
        camera="third",
        appearance_type="anime",
        picker=lambda **_: item,
    )
    assert _COS_ANIME_NEGATIVE_PROMPT in anime_action
    assert _COS_COMMON_NEGATIVE_PROMPT not in anime_action
    assert "禁止动漫插画" not in anime_action
    assert "动漫风格 COS 人物" in anime_action
    assert normalize_cos_prompt_negatives(anime_action, "anime").count(
        _COS_ANIME_NEGATIVE_PROMPT
    ) == 1

    real_action = build_cos_look_action(
        "",
        camera="third",
        appearance_type="real",
        picker=lambda **_: item,
    )
    assert _COS_COMMON_NEGATIVE_PROMPT in real_action
    assert _COS_ANIME_NEGATIVE_PROMPT not in real_action


def test_auto_cos_with_reference_preserves_reference_medium():
    item = {
        "id": "auto_reference_medium_test",
        "title": "自动媒介参考测试",
        "cos_type": "测试",
        "prompt": "换装为测试角色 COS：白色外套和蓝色长裙。",
    }
    action = build_cos_look_action(
        "",
        has_refs=True,
        camera="third",
        appearance_type="auto",
        picker=lambda **_: item,
    )

    assert _COS_AUTO_REFERENCE_NEGATIVE_PROMPT in action
    assert _COS_COMMON_NEGATIVE_PROMPT not in action
    assert "禁止动漫插画" not in action
    assert "未提及或未核实的部件不自行添加" in action
    assert "开叉、荷叶边" not in action

    with tempfile.TemporaryDirectory() as tmp:
        persona = PersonaManager(tmp)
        prompt = persona.build_selfie_prompt(action, "bot", "tone", True, 0)
    assert "视觉媒介和人物比例以参考图一为唯一主形象依据" in prompt
    assert "禁止动漫插画" not in prompt


def test_request_negative_finalization_is_idempotent_and_keeps_special_prompt():
    look = get_cos_look_by_id("ark_ceobe_wilderness")
    action = build_cos_third_person_prompt(look["prompt"])
    real = finalize_cos_prompt_for_request(action, appearance_type="real")
    assert real.count(_COS_COMMON_NEGATIVE_PROMPT) == 1
    assert finalize_cos_prompt_for_request(real, appearance_type="real") == real
    anime = finalize_cos_prompt_for_request(real, appearance_type="anime")
    assert anime.count(_COS_ANIME_NEGATIVE_PROMPT) == 1
    assert "禁止动漫插画" not in anime
    auto = finalize_cos_prompt_for_request(anime, has_refs=True)
    assert "禁止真人照片质感" not in auto
    assert "禁止动漫插画" not in auto
    assert "不带武器" in auto
    for special in (
        get_cos_look_by_id("firefly_star_rail_sleeping_cabin")["prompt"],
        build_cos_third_person_prompt(get_cos_look_by_id("firefly_star_rail_sleeping_cabin")["prompt"]),
    ):
        assert finalize_cos_prompt_for_request(special, appearance_type="real") == special
    assert finalize_cos_prompt_for_request("街边合影") == "街边合影"


def test_cos_costume_hair_overrides_reference_bangs_and_color_consistently():
    item = {
        "id": "hair_override_test",
        "title": "金发假发测试",
        "cos_type": "测试",
        "prompt": "佩戴金色长假发，齐刘海，两侧低马尾以黑色缎带束起；穿白色长袖制服。",
    }
    action = build_cos_look_action(
        "",
        has_refs=True,
        camera="third",
        force_id="",
        picker=lambda **_: item,
    )
    with tempfile.TemporaryDirectory() as tmp:
        prompt = PersonaManager(tmp).build_selfie_prompt(
            action, "小助", "温柔", True, 0
        )

    assert "按指定内容替换参考图中对应的刘海、鬓发、分缝、长度或束发" in prompt
    assert "未写明的发型细节自然补全" in prompt
    assert "参考图原发完整收在假发内" in prompt
    assert "可见发际线、刘海和鬓发均属于同一顶假发" in prompt
    assert "不以是否出现“假发”字样为条件" in prompt
    assert "发际线、发根、内层、刘海、鬓发和发尾均为该色" in prompt
    assert "不在新发型下面叠留参考图原色刘海或鬓发" in prompt
    assert "局部异色仅用于对应发束或区域" in prompt
    assert "完全未指定发色时也沿用参考图发色" in prompt
    assert "发型发色来自参考图一" not in prompt


def test_english_cos_builtin_has_matching_hair_override_contract():
    item = {
        "id": "english_hair_override_test",
        "title": "Silver Wig Test",
        "cos_type": "Test",
        "prompt": "Wear a silver long wig with straight bangs and a red hair bow.",
    }
    action = build_cos_look_action(
        "",
        has_refs=True,
        camera="third",
        picker=lambda **_: item,
    )
    prompt = build_selfie_builtin_prompt(
        action,
        language="en",
        has_reference_image=True,
        appearance_type="auto",
    )

    assert "Replace only the hairstyle features that are specified" in prompt
    assert "tuck all reference hair inside it" in prompt
    assert "the visible hairline, bangs, and side locks belong to the same wig" in prompt
    assert "whether or not the word wig appears" in prompt
    assert "hairline, roots, inner layers, bangs, side locks, and ends" in prompt
    assert "do not layer new hair over bangs or side locks in the reference's original color" in prompt
    assert "Local accents apply only to the corresponding strands or regions" in prompt
    assert "when no color is specified at all" in prompt
    assert "do not invent unlisted hair details from the character name" in prompt


def test_persona_selfie_prompt_finishes_with_selected_medium_contract():
    with tempfile.TemporaryDirectory() as tmp:
        persona = PersonaManager(tmp)
        persona.set_appearance_type("anime")
        prompt = persona.build_selfie_prompt("自拍", "小助", "温柔", True, 0)

    assert prompt.endswith(
        "最终画面统一为 2D 柔光京阿尼风格动漫插画；自拍、手机、照片、摄影和 COS 仅控制构图与内容，不生成真人摄影或混合画风。"
    )
    assert "动漫自拍画面" in prompt
    assert "形象类型：动漫" in prompt


def test_english_builtin_does_not_use_reference_medium_for_anime_setting():
    prompt = build_selfie_builtin_prompt(
        "自拍",
        language="en",
        has_reference_image=True,
        appearance_type="anime",
    )

    assert prompt.startswith("Create one cohesive soft Kyoto Animation-like anime illustration")
    assert "selected appearance type controls the rendered visual medium" in prompt
    assert "never render photorealistically" in prompt
    assert "primary identity and visual-medium anchor" not in prompt

    override = build_selfie_builtin_prompt(
        "自拍，动漫风格",
        language="en",
        appearance_type="real",
    )
    assert override.startswith("Create one cohesive soft Kyoto Animation-like anime illustration")


def test_cos_prompt_uses_explicit_request_medium_override():
    with tempfile.TemporaryDirectory() as tmp:
        persona = PersonaManager(tmp)
        persona.set_appearance_type("real")
        action = build_cos_look_action(
            "这次改成动漫风格",
            camera="third",
            appearance_type=resolve_appearance_type("real", "这次改成动漫风格"),
            picker=lambda **_: {
                "id": "medium_override",
                "title": "媒介覆盖测试",
                "cos_type": "测试",
                "prompt": "测试角色 COS：真人 COSER，白色外套。",
            },
        )
        prompt = persona.build_selfie_prompt(action, "小助", "温柔", True, 0)

    assert _COS_ANIME_NEGATIVE_PROMPT in action
    assert "真人 COSER" not in action
    assert prompt.endswith(
        "最终画面统一为 2D 柔光京阿尼风格动漫插画；自拍、手机、照片、摄影和 COS 仅控制构图与内容，不生成真人摄影或混合画风。"
    )


def test_selfie_action_builders_do_not_force_a_medium_in_auto_mode():
    builders = (
        lambda medium, request="": build_selfie_look_action(
            request,
            appearance_type=medium,
            scene_light_provider=lambda _kind: (["窗边"], ["窗光"]),
        ),
        lambda medium, request="": build_third_person_look_action(
            request,
            appearance_type=medium,
            scene_light_provider=lambda _kind: (["窗边"], ["窗光"]),
        ),
        lambda medium, request="": build_selfie_look_action(
            "漏腰" if not request else request,
            appearance_type=medium,
            scene_light_provider=lambda _kind: (["窗边"], ["窗光"]),
        ),
    )

    for build in builders:
        automatic = build("auto")
        realistic = build("real")
        anime = build("anime")
        assert "真人摄影质感" not in automatic
        assert "柔光京阿尼风格动漫插画" not in automatic
        assert "真人摄影质感" in realistic
        assert "真人摄影质感" not in anime
        assert "柔光京阿尼风格动漫插画" in anime
        assert "真实皮肤" not in anime

    overridden = build_selfie_look_action(
        "这次改成动漫风格",
        appearance_type="real",
        scene_light_provider=lambda _kind: (["窗边"], ["窗光"]),
    )
    assert "柔光京阿尼风格动漫插画" in overridden
    assert "真人摄影质感" not in overridden


def test_command_llm_selfie_and_llm_cos_keep_selected_medium_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        persona = PersonaManager(tmp)
        persona.set_appearance_type("anime")

        command_action = build_selfie_look_action(
            appearance_type=persona.get_appearance_type(),
            scene_light_provider=lambda _kind: (["窗边"], ["窗光"]),
        )
        command_prompt = persona.build_selfie_prompt(command_action, "小助", "温柔", True, 0)
        assert "形象类型：动漫" in command_prompt
        assert command_prompt.endswith(
            "最终画面统一为 2D 柔光京阿尼风格动漫插画；自拍、手机、照片、摄影和 COS 仅控制构图与内容，不生成真人摄影或混合画风。"
        )

    llm_selfie = build_selfie_builtin_prompt(
        "LLM 自拍：这次改成动漫风格",
        language="en",
        appearance_type="real",
    )
    assert llm_selfie.startswith("Create one cohesive soft Kyoto Animation-like anime illustration")
    assert "Final visual medium: a cohesive 2D anime illustration" in llm_selfie

    with tempfile.TemporaryDirectory() as tmp:
        llm_persona = PersonaManager(tmp)
        llm_persona.set_appearance_type("real")
        raw_llm_cos_zh = llm_persona.build_selfie_prompt(
            "LLM 自拍看看COS：洛琪希，这次动漫风格",
            "小助",
            "温柔",
            True,
            0,
        )
    assert "形象类型：动漫" in raw_llm_cos_zh
    assert raw_llm_cos_zh.endswith(
        "最终画面统一为 2D 柔光京阿尼风格动漫插画；自拍、手机、照片、摄影和 COS 仅控制构图与内容，不生成真人摄影或混合画风。"
    )

    cos_action = build_cos_look_action(
        "这次改成动漫风格",
        camera="third",
        appearance_type="anime",
        picker=lambda **_: {
            "id": "llm_cos_medium",
            "title": "LLM COS 媒介测试",
            "cos_type": "测试",
            "prompt": "严格换装为测试角色 COS：红色外套。",
        },
    )
    llm_cos = build_selfie_builtin_prompt(
        cos_action,
        language="en",
        appearance_type="real",
    )
    assert llm_cos.startswith("Create one cohesive soft Kyoto Animation-like anime cosplay illustration")
    assert "Final visual medium: a cohesive 2D anime illustration" in llm_cos
    assert "Final visual medium: a cohesive realistic human photograph" not in llm_cos

    raw_llm_cos = build_selfie_builtin_prompt(
        "LLM 自拍看看COS：洛琪希，这次动漫风格",
        language="en",
        appearance_type="real",
    )
    assert raw_llm_cos.startswith("Create one cohesive soft Kyoto Animation-like anime cosplay illustration")
    assert "Final visual medium: a cohesive 2D anime illustration" in raw_llm_cos
