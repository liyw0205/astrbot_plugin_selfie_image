"""Regression checks on the assembled prompts without calling an image model."""

import asyncio
import random
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from astrbot_plugin_selfie_image.cos.cos_looks import build_cos_look_action
from astrbot_plugin_selfie_image.cos.leg_focus import (
    LEGFOCUS_HOSIERY_STYLES,
    LEGFOCUS_CAMERA_FRAMINGS,
    LEGFOCUS_VIEW_BY_ID,
    LegFocusDataError,
    build_leg_focus_action,
)
from astrbot_plugin_selfie_image.core.providers import ImageReference
from astrbot_plugin_selfie_image.core.models import AICatConfig
from astrbot_plugin_selfie_image.features.persona import PersonaManager
from astrbot_plugin_selfie_image.main import SelfieImagePlugin
from astrbot_plugin_selfie_image.prompts.prompt_templates import (
    build_selfie_builtin_prompt,
    extract_generated_action_contract,
    extract_user_prompt,
)
from astrbot_plugin_selfie_image.prompts.selfie_actions import (
    build_group_selfie_action,
    build_selfie_look_action,
    build_third_person_look_action,
)


def plugin_for_prompts(tmp_path, english=False):
    plugin = object.__new__(SelfieImagePlugin)
    plugin.persona = PersonaManager(tmp_path)
    plugin.config = SimpleNamespace(image_max_batch_count=10)
    plugin._ensure_daily_selfie_schedule = AsyncMock()
    plugin._persona_identity_reference = lambda **_kw: ImageReference(data=b"identity", mime_type="image/png")
    plugin._persona_auxiliary_references = lambda action="": []
    plugin._prompt_en_needed = lambda *_a, **_kw: english
    # Exercise the supported translation-failure fallback without a network LLM.
    plugin._translate_prompt_to_english = AsyncMock(side_effect=lambda text, **_kw: (text, {"applied": False}))
    return plugin


@pytest.mark.parametrize("english,translation_applied", [(False, False), (True, False), (True, True)])
def test_all_look_modes_keep_the_selected_contract_in_final_request(tmp_path, english, translation_applied):
    plugin = plugin_for_prompts(tmp_path, english)
    if translation_applied:
        plugin._translate_prompt_to_english = AsyncMock(
            side_effect=lambda text, **_kw: (text, {"applied": True})
        )
    random.seed(5)
    actions = [
        build_selfie_look_action("侧身"),
        build_selfie_look_action("穿黑丝"),
        build_selfie_look_action("测试角色 COS"),
        build_selfie_look_action("露腰", has_refs=True),
        build_third_person_look_action("背对镜头"),
        build_leg_focus_action("不要第三人称，第一人称 不要沙发 不要脚踝交叠 白色短袜"),
        build_group_selfie_action("三人合影，穿黑丝"),
        build_cos_look_action("侧身"),
    ]
    for action in actions:
        prompt, refs, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, []))
        contract = extract_generated_action_contract(action)
        assert prompt.count(contract) == 1
        assert len(refs) == 1
        supplement = re.search(r"(?:用户补充要求优先|用户补充要求|用户指定内容)[:：]", action)
        if supplement:
            assert extract_user_prompt(action) in prompt
            assert prompt.count("User request:") <= 1
        if "【legs:outfit】" not in action:
            assert "【legs:outfit】" not in prompt


@pytest.mark.parametrize("query", [
    "不要第三人称，第一人称", "非自拍", "notselfie", "不要沙发",
    "不要脚踝交叠", "不要袜子", "不穿鞋和袜子", "不要黑丝", "白色短袜", "白色堆堆袜",
    "双手撑床", "单手撑地", "第三人称 低机位平拍", "第三人称 斜侧拍",
])
def test_leg_constraints_survive_final_persona_and_reference_selection(tmp_path, query):
    plugin = plugin_for_prompts(tmp_path)
    for seed in range(12):
        random.seed(seed)
        action = build_leg_focus_action(query)
        prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, []))
        assert query in prompt
        assert prompt.count(f"用户补充要求优先：{query}。") == 1
        if query.startswith("不要第三人称"):
            assert "【cam:selfie】" in prompt
        if query in {"非自拍", "notselfie"}:
            assert "【cam:third】" in prompt
        if query == "不要沙发":
            assert "【leg_scene:sofa】" not in prompt
        if query == "不要脚踝交叠":
            assert "【pose:seated_ankle_cross】" not in prompt
        if query in {"不要袜子", "不穿鞋和袜子", "不要黑丝"}:
            assert "【wear:black】" not in prompt
        if query in {"不要袜子", "不穿鞋和袜子"}:
            assert "【wear:white】" not in prompt
        if query == "双手撑床":
            assert "【leg_scene:bed_surface】" in prompt
        if query == "单手撑地":
            assert re.search(r"【leg_scene:(?:bedroom_rug|living_room_rug)】", prompt)
        if query == "白色堆堆袜":
            assert not any(style["prompt"] in prompt for style in LEGFOCUS_HOSIERY_STYLES)
            assert "不额外随机其他袜装或赤足" in prompt
        if query == "第三人称 斜侧拍":
            view_id = re.search(r"【leg_view:([a-z_]+)】", prompt).group(1)
            assert LEGFOCUS_VIEW_BY_ID[view_id]["direction"] == "oblique"


@pytest.mark.parametrize("query", ["赤足", "白色短袜", "白色堆堆袜", "不要袜子", "不要鞋子"])
def test_leg_reference_detail_override_does_not_also_preserve_old_socks(tmp_path, query):
    plugin = plugin_for_prompts(tmp_path)
    action = build_leg_focus_action(query, has_refs=True)
    prompt, refs, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(
        None, action, [ImageReference(data=b"external", mime_type="image/png")]
    ))
    assert refs[0].data == b"external"
    assert "袜鞋与配饰，只调整姿势和构图" not in prompt
    assert "其余服饰沿用原图" in prompt
    assert prompt.count(query) >= 1


@pytest.mark.parametrize("query", ["第三人称 第一人称", "侧拍 正前方", "第一人称 全身"])
def test_incompatible_leg_requests_are_rejected_before_prompt_generation(query):
    with pytest.raises(LegFocusDataError):
        build_leg_focus_action(query)


def test_group_leg_description_is_not_routed_into_leg_mode(tmp_path):
    plugin = plugin_for_prompts(tmp_path)
    random.seed(5)
    action = build_group_selfie_action("多人合影 黑丝")
    intent = plugin.persona.analyze_selfie_intent(action)
    assert intent.is_group_photo and not intent.is_legs_only
    for language in ("zh", "en"):
        prompt = build_selfie_builtin_prompt(action, language=language)
        assert "【legs:outfit】" not in prompt


def test_close_selfie_has_one_framing_and_crop_waist_keeps_reference_context(tmp_path):
    plugin = plugin_for_prompts(tmp_path)
    with patch("astrbot_plugin_selfie_image.prompts.selfie_actions.random.choices", return_value=["close_portrait"]):
        action = build_selfie_look_action()
    prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, []))
    assert "近景胸像" in prompt
    assert "画面至少到腰线" not in prompt
    cropped = build_selfie_look_action("露腰", has_refs=True)
    assert "不另加沙发或床" in cropped
    assert "放松地坐着或靠在深色沙发/床边" not in cropped


def test_reference_description_uses_locked_contract_scope(tmp_path):
    plugin = plugin_for_prompts(tmp_path)
    plugin.config = SimpleNamespace(image_ocr_model="")
    plugin._call_text_llm = AsyncMock(return_value="相同人物，长发。")
    contract = build_leg_focus_action("赤足", has_refs=True)
    _, meta = asyncio.run(plugin._describe_reference_images_for_generation(
        object(), [b"external"], prompt_contract=contract
    ))
    instruction = plugin._call_text_llm.call_args.args[1]
    assert contract in instruction
    assert "不要描述原图的姿势" in instruction
    assert "未被本次要求替换的衣物" in instruction
    assert meta["scope"] == "contract_identity"


def test_batch_keeps_user_supplement_without_locking_random_legwear(tmp_path):
    plugin = plugin_for_prompts(tmp_path)
    plugin._run_image_generation = AsyncMock(return_value={"success": True})

    async def run_shots(**kwargs):
        return [await kwargs["run_one"](index) for index in range(kwargs["total"])]

    plugin._run_counted_generation_shots = run_shots
    for source, action in (
        ("command-look-legs", build_leg_focus_action()),
        ("command-group-selfie", build_group_selfie_action("三人合影，手持同一束花")),
    ):
        with patch.object(plugin, "_build_leg_focus_action", wraps=plugin._build_leg_focus_action) as build_legs:
            asyncio.run(plugin._run_selfie_batches_unlocked(
                "", None, action, [], source, 2, "9:16", "1K", "失败"
            ))
            if source == "command-look-legs":
                assert all(not call.kwargs["force_legwear"] for call in build_legs.call_args_list)
            else:
                for call in plugin._run_image_generation.call_args_list[-2:]:
                    assert "三人合影，手持同一束花" in call.args[0]


@pytest.mark.parametrize("namespace", ["studio", "creative"])
@pytest.mark.parametrize("english,translation_applied", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("mode,query", [
    ("group", "三人侧身合影 黑丝"),
    ("selfie", "背对镜头"),
    ("selfie", "看看腿 第一人称 白色短袜"),
])
def test_canvas_final_requests_use_the_same_mode_contracts(tmp_path, namespace, english, translation_applied, mode, query):
    plugin = plugin_for_prompts(tmp_path, english)
    if translation_applied:
        plugin._translate_prompt_to_english = AsyncMock(side_effect=lambda text, **_kw: (text, {"applied": True}))
    plugin.config = AICatConfig.from_dict({})
    plugin.persona.set_appearance_type("anime")
    plugin.persona.ensure_daily_selfie_profile = AsyncMock()
    session = {
        "id": "canvas", "template": "group" if mode == "group" else "selfie",
        "graph": {"mode": mode, "prompt": query, "count": 1},
        "slots": [{"id": "ref", "image_path": "reference.png"}],
    }
    store = SimpleNamespace(get=lambda _: session, attach_run_finish=lambda *_a, **_kw: None)
    plugin._canvas_store = lambda selected: store if selected == namespace else None
    plugin._resolve_studio_refs = lambda *_a, **_kw: ([(b"external", "image/png")], ["ref"])
    plugin._task_cancel_requested = lambda _: False
    plugin._web_task_timestamp = lambda: "now"
    state = {}
    plugin._set_web_image_task = lambda _, **fields: state.update(fields)
    plugin.get_web_image_task = lambda _: state
    plugin._wait_for_record_commits = AsyncMock()
    plugin._record_task = lambda _: None
    plugin._run_image_generation = AsyncMock(return_value={"success": True, "image_paths": ["generated.png"]})
    asyncio.run(plugin._run_studio_task("task", "canvas", canvas_namespace=namespace))
    assert state["status"] == "succeeded", state.get("error")
    request = plugin._run_image_generation.call_args.kwargs
    action = request["original_prompt"]
    assert request["prompt"].count(extract_generated_action_contract(action)) == 1
    if mode == "group":
        assert plugin.persona.analyze_selfie_intent(action).is_group_photo
        assert "【legs:outfit】" not in request["prompt"]
        assert "柔光京阿尼" in request["prompt"]
    elif "看看腿" in query:
        assert LEGFOCUS_CAMERA_FRAMINGS["selfie"] in request["prompt"]
        assert "白色短袜" in request["prompt"]
    else:
        assert "主角背对镜头" in request["prompt"]


def test_llm_group_contract_is_not_wrapped_again(tmp_path):
    plugin = plugin_for_prompts(tmp_path)
    action = build_group_selfie_action("背对镜头合影")
    prepared, _, _ = plugin._prepare_llm_selfie_action(action, False)
    assert prepared == action


@pytest.mark.parametrize("builder", [build_selfie_look_action, build_third_person_look_action, build_group_selfie_action])
def test_explicit_gaze_is_not_overridden_by_camera_defaults(tmp_path, builder):
    plugin = plugin_for_prompts(tmp_path)
    for seed in range(12):
        random.seed(seed)
        action = builder("不看镜头，目光看向侧边")
        prompt, _, _ = asyncio.run(plugin._build_selfie_prompt_and_refs_for_event(None, action, []))
        assert "不看镜头，目光看向侧边" in prompt
        for forced in ("必须看向镜头", "眼神对焦镜头", "优先与镜头有眼神交流", "正对镜头时自然看向镜头", "对镜头轻轻点头笑"):
            assert forced not in prompt


def test_quick_draw_variants_are_composed_before_sending(tmp_path):
    plugin = plugin_for_prompts(tmp_path)
    plugin.config = AICatConfig.from_dict({})
    plugin._validate_web_test_selection = lambda _: None
    plugin._find_web_retry_target = lambda *_a, **_kw: SimpleNamespace(label="test")
    plugin._record_task = lambda _: None
    plugin._run_image_generation = AsyncMock(return_value={"success": True, "image_paths": ["generated.png"]})
    plugin.build_creative_variations = lambda *_a, **_kw: [
        {"prompt": "自然自拍，窗边"}, {"prompt": "自然自拍，书桌边"},
    ]
    result = asyncio.run(plugin.web_test_image({
        "prompt": "自然自拍", "use_selfie_reference": True,
        "variation_enabled": True, "count": 2,
    }))
    assert result["success"], result.get("error")
    for call, scene in zip(plugin._run_image_generation.call_args_list, ("窗边", "书桌边")):
        assert scene in call.kwargs["prompt"]
        assert "参考图一是主角身份锚点" in call.kwargs["prompt"]
        assert call.kwargs["refs"][0].data == b"identity"


@pytest.mark.parametrize("refs", [[], [ImageReference(data=b"external", mime_type="image/png")]])
def test_plain_drawing_requests_preserve_the_user_prompt(tmp_path, refs):
    plugin = plugin_for_prompts(tmp_path)
    plugin._run_image_generation = AsyncMock(return_value={"success": True})
    raw = "正面站姿，摄影者留在画外；只按参考服装，不额外换装。"
    asyncio.run(plugin._draw_passthrough_once(None, raw, "9:16", "1K", refs, "command-draw"))
    call = plugin._run_image_generation.call_args
    assert call.args[0] == raw
    assert call.args[3] == refs
