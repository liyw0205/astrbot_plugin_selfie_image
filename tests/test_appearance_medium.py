from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astrbot_plugin_selfie_image.cos.cos_looks import (
    _COS_ANIME_NEGATIVE_PROMPT,
    _COS_COMMON_NEGATIVE_PROMPT,
    build_cos_look_action,
    normalize_cos_prompt_negatives,
)
from astrbot_plugin_selfie_image.features.persona import PersonaManager
from astrbot_plugin_selfie_image.prompts.selfie_actions import (
    build_selfie_look_action,
    build_third_person_look_action,
)
from astrbot_plugin_selfie_image.prompts.prompt_templates import (
    build_selfie_builtin_prompt,
    resolve_appearance_type,
)


def test_explicit_request_medium_overrides_saved_setting():
    assert resolve_appearance_type("real", "这次请改成动漫风格") == "anime"
    assert resolve_appearance_type("anime", "这次请生成真人照片") == "real"
    assert resolve_appearance_type("anime", "不要动漫风格") == "real"
    assert resolve_appearance_type("anime", "不要生成动漫风格") == "real"
    assert resolve_appearance_type("anime", "自然自拍") == "anime"
    assert resolve_appearance_type("invalid", "自然自拍") == "auto"


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
