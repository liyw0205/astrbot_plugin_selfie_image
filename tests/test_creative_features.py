from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from astrbot_plugin_selfie_image.cos.cos_pool import CosPoolStore
from astrbot_plugin_selfie_image.features.creative_features import (
    CreativeFeaturesMixin,
    apply_retry_strategy,
    build_prompt_variations,
    compare_generation_records,
    normalize_storyboard_payload,
    parse_video_storyboard,
    render_prompt_template,
)
from astrbot_plugin_selfie_image.prompts.command_parser import (
    extract_prompt_aspect_ratio,
    extract_template_options,
    parse_prompt_options,
)
from astrbot_plugin_selfie_image.prompts.preset import VideoPresetManager
from astrbot_plugin_selfie_image.studio.studio import default_video_preset_seed


def test_template_variables_support_chinese_aliases_and_explicit_values() -> None:
    result = render_prompt_template(
        "{角色}穿着{服饰}，位于{场景|室内}",
        {"role": "宁红夜", "outfit": "红色旗袍"},
    )
    assert result["prompt"] == "宁红夜穿着红色旗袍，位于室内"
    assert not result["unresolved"]


def test_variations_keep_fixed_values_and_change_requested_fields() -> None:
    rows = build_prompt_variations(
        "{角色}在{场景}，{姿势}",
        4,
        fixed_values={"角色": "角色A"},
        vary=["scene", "pose"],
        seed="stable",
    )
    assert len(rows) == 4
    assert all("角色A" in row["prompt"] for row in rows)
    assert all("scene" in row["values"] and "pose" in row["values"] for row in rows)
    assert [row["prompt"] for row in rows] == [row["prompt"] for row in build_prompt_variations("{角色}在{场景}，{姿势}", 4, fixed_values={"角色": "角色A"}, vary=["scene", "pose"], seed="stable")]


def test_storyboard_parses_lines_and_durations() -> None:
    parsed = parse_video_storyboard("镜头1：推近角色，时长2秒\n镜头2：转身看向窗外")
    assert parsed["enabled"] is True
    assert parsed["shots"][0]["duration"] == 2
    assert parsed["shots"][1]["description"] == "转身看向窗外"
    assert "镜头1" in parsed["prompt"]

    parenthesized = parse_video_storyboard("镜头1：推近角色（2秒）")
    assert parenthesized["shots"][0]["duration"] == 2
    assert parenthesized["shots"][0]["description"] == "推近角色"


def test_storyboard_normalizes_edited_dashboard_rows() -> None:
    parsed = normalize_storyboard_payload(
        {"shots": [{"description": "镜头推进", "duration": "2"}, {"prompt": "转身", "duration": 0}]}
    )
    assert parsed["enabled"] is True
    assert parsed["shots"][0]["duration"] == 2
    assert parsed["shots"][1]["index"] == 2
    assert "镜头2" in parsed["prompt"]


def test_builtin_dance_video_presets_include_motion_audio_and_duration() -> None:
    seed = default_video_preset_seed()

    xiaoban = seed["小半"]
    assert xiaoban["duration"] == 12
    assert "左右点胯2次" in xiaoban["prompt"]
    assert "baby, i am worth it" in xiaoban["prompt"]
    assert "必须输出可听见的BGM" in xiaoban["prompt"]

    jiatong = seed["嘉桐摇"]
    assert jiatong["duration"] == 10
    assert "BPM为115" in jiatong["prompt"]
    assert "双手做可爱的猫爪姿势" in jiatong["prompt"]
    assert "必须输出可听见的BGM" in jiatong["prompt"]

    sequence = seed["兰花指卡点舞"]
    assert sequence["duration"] == 20
    assert "严格按1至19的原顺序逐拍完成" in sequence["prompt"]
    assert "连续完成3次波浪手" in sequence["prompt"]
    assert "双手向左右交替推送5次" in sequence["prompt"]
    assert "第5个八拍" in sequence["prompt"]
    assert "必须输出可听见的BGM" in sequence["prompt"]

    revenge = seed["复仇摇"]
    assert revenge["duration"] == 16
    assert "镜头随律动轻微晃动" in revenge["prompt"]
    assert "短促人声采样片段" in revenge["prompt"]
    assert "不要生成琵琶、古筝或其他古风乐器音效" in revenge["prompt"]

    revenge_two = seed["复仇摇2"]
    assert revenge_two["duration"] == 16
    assert "镜头全程跟随女孩跳‘复仇摇’" in revenge_two["prompt"]
    assert "发丝具有真实惯性但全程不遮脸" in revenge_two["prompt"]
    assert "必须输出可听见的BGM" in revenge_two["prompt"]

    fish = seed["鱼块摇"]
    assert fish["duration"] == 13
    assert "生成13.6秒9:16竖屏高清真人舞蹈视频" in fish["prompt"]
    assert "整体是魔性、带感、卡点精准的鱼块摇风格" in fish["prompt"]
    assert "《Blow (鱼块摇)》DJ电子鼓点版" in fish["prompt"]
    assert "动作必须按以下顺序完整执行，不循环、不跳过" in fish["prompt"]
    assert "1，右手放胯部，坐胯" in fish["prompt"]
    assert "2，双手交替点点点，同时双手绕腕扭胯" in fish["prompt"]
    assert "7，左手锤右肩3次" in fish["prompt"]
    assert "14，双手举起卖萌，收尾定格" in fish["prompt"]
    assert "所有动作必须卡在《Blow (鱼块摇)》DJ电子鼓点的重拍上" in fish["prompt"]
    assert "弹琵琶仅表现手部拨弦手势" in fish["prompt"]
    assert "禁止古风乐器独奏" in fish["prompt"]
    assert "禁止古风乐器音效" in fish["prompt"]
    assert "必须输出可听见的BGM" in fish["prompt"]

    transition = seed["动作转场"]
    assert transition["duration"] == 8
    assert "动作1：人物保持跪姿" in transition["prompt"]
    assert "动作2：切到下一个动作" in transition["prompt"]
    assert "动作4：切到下一个动作" in transition["prompt"]
    assert "不能跳帧、残影、动作叠加" in transition["prompt"]
    assert "必须输出可听见的BGM" in transition["prompt"]

    transition_two = seed["动作转场2"]
    assert transition_two["duration"] == 6
    assert "三个动作按顺序呈现" in transition_two["prompt"]
    assert "短暂黑屏切镜" in transition_two["prompt"]
    assert "人物侧身面对镜头" in transition_two["prompt"]
    assert "人物正面面对镜头" in transition_two["prompt"]
    assert "人物背对镜头" in transition_two["prompt"]
    assert "必须输出可听见的BGM" in transition_two["prompt"]

    with tempfile.TemporaryDirectory() as directory:
        manager = VideoPresetManager(directory)
        assert manager.has_preset("小半")
        assert manager.has_preset("嘉桐摇")
        assert manager.has_preset("兰花指卡点舞")
        assert manager.has_preset("复仇摇")
        assert manager.has_preset("复仇摇2")
        assert manager.has_preset("鱼块摇")
        assert manager.has_preset("动作转场")
        assert manager.has_preset("动作转场2")
        assert manager.resolve("小半")["duration"] == 12
        assert manager.resolve("嘉桐摇")["duration"] == 10
        assert manager.resolve("兰花指卡点舞")["duration"] == 20
        assert manager.resolve("复仇摇")["duration"] == 16
        assert manager.resolve("复仇摇2")["duration"] == 16
        assert manager.resolve("鱼块摇")["duration"] == 13
        assert manager.resolve("动作转场")["duration"] == 8
        assert manager.resolve("动作转场2")["duration"] == 6


def test_command_template_options_can_be_mixed_with_prompt() -> None:
    cleaned, values, randomize = extract_template_options(
        "{角色}在{场景} --场景=庭院廊下 --角色 \"宁红夜\" --template-random"
    )
    assert cleaned == "{角色}在{场景}"
    assert values == {"scene": "庭院廊下", "role": "宁红夜"}
    assert randomize is True


def test_unknown_command_options_are_preserved() -> None:
    cleaned, values, randomize = extract_template_options("猫 --ar 9:16 --unknown value")
    assert cleaned == "猫 --ar 9:16 --unknown value"
    assert values == {}
    assert randomize is False


def test_prompt_aspect_ratio_prefers_user_supplement_over_cos_template() -> None:
    text = "严格真人COS，竖屏9:16全身。用户补充要求优先：改为横屏16:9构图。"
    assert extract_prompt_aspect_ratio(text) == "16:9"
    cleaned, aspect, _ = parse_prompt_options(text, default_aspect_ratio="1:1")
    assert aspect == "16:9"
    assert "竖屏9:16" in cleaned


def test_prompt_aspect_ratio_supports_orientation_and_pixel_dimensions() -> None:
    assert extract_prompt_aspect_ratio("方图正面构图") == "1:1"
    assert extract_prompt_aspect_ratio("尺寸 1024x1536") == "2:3"
    assert extract_prompt_aspect_ratio("不要横屏，使用竖屏") == "9:16"


def test_explicit_ar_option_beats_natural_prompt_ratio() -> None:
    _, aspect, _ = parse_prompt_options(
        "猫 --ar 1:1 横屏16:9",
        default_aspect_ratio="9:16",
    )
    assert aspect == "1:1"


def test_retry_strategies_are_bounded_and_preserve_prompt() -> None:
    source = {"prompt": "原提示词", "channel": "bad", "model": "bad-model", "resolution": "4K"}
    lowered = apply_retry_strategy(source, "lower_resolution")
    assert lowered["prompt"] == source["prompt"]
    assert lowered["resolution"] == "2K"
    assert source["resolution"] == "4K"
    model_only = apply_retry_strategy(source, "model_only")
    assert model_only["channel"] == "bad" and model_only["model"] == ""
    channel_only = apply_retry_strategy(source, "channel_only")
    assert channel_only["channel"] == "" and channel_only["model"] == "bad-model"


def test_cos_pool_round_trip_and_custom_validation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = CosPoolStore(directory)
        store.set_favorite("builtin-a", True)
        store.save_custom({"id": "custom-a", "title": "自定义", "prompt": "白色礼服"})
        assert store.is_favorite("builtin-a")
        assert store.list_custom()[0]["id"] == "custom-a"
        exported = store.export_data()
        restored = CosPoolStore(str(Path(directory) / "other"))
        restored.import_data(exported)
        assert restored.list_favorites() == ["builtin-a"]
        assert restored.list_custom()[0]["title"] == "自定义"
        with pytest.raises(ValueError):
            store.save_custom({"id": "missing-prompt", "title": "无效"})


def test_cos_pool_drops_legacy_builtin_id_duplicates() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / CosPoolStore.FILENAME
        path.write_text(
            json.dumps(
                {
                    "favorites": [],
                    "custom": [
                        {
                            "id": "nahida_floating_dream",
                            "title": "纳西妲·白草净华",
                            "prompt": "旧的部分字段",
                        },
                        {
                            "id": "custom-genshin",
                            "title": "自定义原神套装",
                            "prompt": "完整自定义服饰",
                            "cos_type": "原神",
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        store = CosPoolStore(directory)
        assert [item["id"] for item in store.list_custom()] == ["custom-genshin"]
        assert store.list_custom()[0]["cos_type"] == "原神"
        with pytest.raises(ValueError, match="不能使用内置套装 ID"):
            store.save_custom(
                {
                    "id": "nahida_floating_dream",
                    "title": "纳西妲·白草净华",
                    "prompt": "再次覆盖",
                }
            )


def test_cos_pool_web_contract_deduplicates_builtin_and_custom_ids() -> None:
    class Pool:
        def list_favorites(self):
            return []

        def list_custom(self):
            return [
                {
                    "id": "nahida_floating_dream",
                    "title": "纳西妲·白草净华",
                    "prompt": "旧的部分字段",
                    "source": "custom",
                }
            ]

    plugin = CreativeFeaturesMixin()
    plugin.cos_pool = Pool()
    payload = plugin.list_cos_pools_for_web()
    combined = list(payload["builtin"]) + list(payload["custom"])
    nahida = [item for item in combined if item.get("id") == "nahida_floating_dream"]
    assert len(nahida) == 1
    assert nahida[0]["source"] == "builtin"


def test_record_comparison_is_redacted_to_public_fields() -> None:
    compared = compare_generation_records(
        [
            {"id": "a", "success": True, "prompt": "同一提示", "used_model": "model-a", "request_data": {"api_key": "secret"}},
            {"id": "b", "success": False, "prompt": "同一提示", "used_model": "model-b"},
        ]
    )
    assert compared["count"] == 2
    assert compared["records"][0]["model"] == "model-a"
    assert "api_key" not in compared["records"][0]


def test_record_comparison_requires_two_distinct_records() -> None:
    with pytest.raises(ValueError):
        from astrbot_plugin_selfie_image.features.creative_features import CreativeFeaturesMixin

        CreativeFeaturesMixin.compare_records_for_web(object(), ["only-one"])
