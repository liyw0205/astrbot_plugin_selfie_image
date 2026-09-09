from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from astrbot_plugin_selfie_image.cos.cos_pool import CosPoolStore
from astrbot_plugin_selfie_image.features.creative_features import (
    apply_retry_strategy,
    build_prompt_variations,
    compare_generation_records,
    parse_video_storyboard,
    render_prompt_template,
)


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


def test_retry_strategies_are_bounded_and_preserve_prompt() -> None:
    source = {"prompt": "原提示词", "channel": "bad", "model": "bad-model", "resolution": "4K"}
    lowered = apply_retry_strategy(source, "lower_resolution")
    assert lowered["prompt"] == source["prompt"]
    assert lowered["resolution"] == "2K"
    assert source["resolution"] == "4K"
    model_only = apply_retry_strategy(source, "model_only")
    assert model_only["channel"] == "" and model_only["model"] == ""


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
