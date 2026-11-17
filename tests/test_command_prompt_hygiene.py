"""Capture final requests through command parsing and the real batch runner."""

import asyncio
import json
import os
import random
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_selfie_image.core.models import AICatConfig
from astrbot_plugin_selfie_image.core.providers import ImageReference
from astrbot_plugin_selfie_image.features.persona import PersonaManager
from astrbot_plugin_selfie_image.main import SelfieImagePlugin
from astrbot_plugin_selfie_image.prompts.preset import ImagePresetManager


COMMANDS = {
    "cos_third": ("cmd_look_cos", "/看看COS 2 洛琪希·奶油睡衣 他拍 自然站姿 摄影棚"),
    "cos_selfie": ("cmd_look_cos", "/看看COS 2 洛琪希·奶油睡衣 自拍 自然站姿 摄影棚"),
    "cos_first": ("cmd_look_cos", "/看看COS 2 洛琪希·奶油睡衣 第一视角 自然站姿 摄影棚"),
    "cos_rear": ("cmd_look_cos", "/看看COS 2 洛琪希·奶油睡衣 他拍 背面发型近景"),
    "cos_back": ("cmd_look_cos", "/看看COS 2 希露菲叶特 他拍 背身"),
    "cos_blindfold": ("cmd_look_cos", "/看看COS 2 美杜莎·黑紫短裙基础灵基 他拍 自然站姿 面部特写"),
    "cos_closed": ("cmd_look_cos", "/看看COS 2 洛琪希·奶油睡衣 他拍 安静低眸 温柔"),
    "cos_expression": ("cmd_look_cos", "/看看COS 2 洛琪希·奶油睡衣 他拍 自然站姿 温柔 自然妆"),
    "cos_makeup_expression": ("cmd_look_cos", "/看看COS 2 洛琪希·奶油睡衣 他拍 自然站姿 平静 温柔妆"),
    "selfie": ("cmd_selfie", "/自拍 2 手持红色雨伞"),
    "selfie_back": ("cmd_selfie", "/自拍 2 背身坐在床边整理发丝"),
    "selfie_heart": ("cmd_selfie", "/自拍 2 双手比心"),
    "selfie_crop": ("cmd_selfie", "/自拍 2 漏腰"),
    "third": ("cmd_look_you", "/看看你 2 手持红色雨伞"),
    "third_back": ("cmd_look_you", "/看看你 2 背面抬手整理发丝"),
    "legs": ("cmd_look_legs", "/看看腿 2 黑丝"),
    "group": ("cmd_group_selfie", "/合影 2 和一位成年女性在咖啡馆"),
}


async def capture_command(tmp_path, mode, medium, language, message_refs=False):
    plugin = object.__new__(SelfieImagePlugin)
    plugin.persona = PersonaManager(tmp_path)
    plugin.persona.set_appearance_type(medium)
    plugin.config = AICatConfig.from_dict({})
    plugin.presets = ImagePresetManager(tmp_path)
    plugin._ensure_daily_selfie_schedule = AsyncMock()
    plugin._persona_identity_reference = lambda **_kw: ImageReference(data=b"identity", mime_type="image/png")
    plugin._persona_auxiliary_references = lambda *_a: []
    plugin._prompt_en_needed = lambda *_a, **_kw: language != "zh"
    plugin._translate_prompt_to_english = AsyncMock(
        side_effect=lambda text, **_kw: (text, {"applied": language == "en"})
    )
    plugin._quota_error_message = lambda *_a: ""
    plugin._rate_limit_error_message = lambda *_a: ""
    plugin._event_reference_images = AsyncMock(return_value=[
        ImageReference(data=f"external-{index}".encode(), mime_type="image/png")
        for index in range(int(message_refs))
    ])
    plugin._build_contextual_progress_text = AsyncMock(return_value="progress")
    plugin._record_bot_text_context = lambda *_a: None
    plugin._image_batch_queue_expected = lambda *_a: False
    plugin._run_image_generation = AsyncMock(return_value={"success": True})
    captured = {}
    plugin.start_command_image_task = lambda *_a, **kw: captured.update(kw) or "task"

    async def run_shots(**kwargs):
        return [await kwargs["run_one"](index) for index in range(kwargs["total"])]

    plugin._run_counted_generation_shots = run_shots
    method, message = COMMANDS[mode]
    event = SimpleNamespace(message_str=message, plain_result=lambda text: text)
    state = random.getstate()
    try:
        random.seed(17)
        results = [result async for result in getattr(plugin, method)(event)]
        assert results and "runner" in captured, results
        await captured["runner"]("")
    finally:
        random.setstate(state)
    calls = plugin._run_image_generation.call_args_list
    assert len(calls) == 2
    records = [{
        "command": message,
        "medium": medium,
        "translation": language,
        "message_refs": bool(message_refs),
        "message_reference_count": int(message_refs),
        "prompt": call.args[0],
        "action": call.kwargs["original_prompt"],
    } for call in calls]
    audit_dir = os.environ.get("SELFIE_PROMPT_AUDIT_DIR")
    if audit_dir:
        target = Path(audit_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{mode}-{medium}-{language}-{int(message_refs)}.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    return records


@pytest.mark.parametrize("mode", COMMANDS)
@pytest.mark.parametrize("medium", ["真人", "动漫", "自动"])
@pytest.mark.parametrize("language", ["zh", "en", "fallback"])
@pytest.mark.parametrize("message_refs", [False, True, 2])
def test_command_final_prompts_have_one_owner_per_rule(tmp_path, mode, medium, language, message_refs):
    records = asyncio.run(capture_command(tmp_path, mode, medium, language, message_refs))
    for record in records:
        prompt = record["prompt"]
        assert len(re.findall(r"磨皮|skin smoothing", prompt)) <= int(medium != "动漫")
        assert "即使套装正文只写长发" not in prompt
        assert "Complete unspecified hairstyle details naturally" not in prompt
        assert "本次未指定视角，随机采用" not in prompt
        assert "按用户指定的镜头构图执行" not in prompt
        assert "镜头视角优先于参考图投影" not in prompt or not mode.startswith("cos")
        assert prompt.count("本次兼容神态：") <= 1
        assert prompt.count("本次兼容妆造：") <= 1
        assert "眼神柔软，保留鼻梁" not in prompt
        if mode.startswith("cos") or mode in {"selfie", "selfie_back", "selfie_heart", "selfie_crop", "third", "third_back"}:
            assert "表情眼神按本次场景重画" not in prompt
            assert "除非用户要求遮脸，否则主角面部清晰自然" not in prompt
        if mode in {"cos_rear", "cos_back"}:
            assert "本次兼容神态：" not in prompt
            assert "本次兼容妆造：" not in prompt
        if mode == "cos_rear":
            assert prompt.count("不回头、不回眸、不扭颈") == 1
        if mode == "cos_back":
            assert "长发自然垂落背部" not in prompt
        if mode == "cos_blindfold":
            assert "【cos_eye:full_cover】" in record["action"]
            expression = prompt.split("本次兼容神态：", 1)[1].split("本次兼容妆造：", 1)[0]
            makeup = prompt.split("本次兼容妆造：", 1)[1].split("用户补充", 1)[0].split("【cos:", 1)[0]
            assert not re.search(r"眼神|目光|视线|眼睑|眼角|眉|睫毛|眼线|眼影|眼妆", expression + makeup)
        if mode == "cos_closed":
            expression = prompt.split("本次兼容神态：", 1)[1].split("本次兼容妆造：", 1)[0]
            assert not re.search(r"眼神|目光|视线|抬眼|看向镜头", expression)
        if mode == "cos_expression":
            assert "【cos_expression:gentle】" in record["action"]
            assert "【cos_makeup:natural_clean】" in record["action"]
            assert "用户补充要求优先：温柔" not in prompt
        if mode == "cos_makeup_expression":
            assert "【cos_expression:calm】" in record["action"]
            assert "【cos_makeup:soft_feminine】" in record["action"]
            assert "用户补充要求优先：平静" not in prompt
            assert "神态温柔放松" not in prompt
        if mode == "selfie_back":
            assert "在用户指定的床沿或座位边自然坐下" in prompt
        if mode == "third_back":
            assert "一只手自然整理发丝" in prompt
        if "雨伞" in record["command"]:
            assert prompt.count("手持红色雨伞") == 1
