from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from astrbot_plugin_selfie_image.cos.cos_pool import CosPoolStore
from astrbot_plugin_selfie_image.cos.cos_looks import build_cos_look_action
from astrbot_plugin_selfie_image.core.providers import ImageReference
from astrbot_plugin_selfie_image.features.creative_features import (
    CreativeFeaturesMixin,
    apply_retry_strategy,
    build_prompt_variations,
    compare_generation_records,
    render_prompt_template,
)
from astrbot_plugin_selfie_image.prompts.command_parser import (
    extract_prompt_aspect_ratio,
    extract_template_options,
    parse_prompt_options,
)
from astrbot_plugin_selfie_image.features.reference_collector import CollectedReferences


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


def test_reference_selection_reports_cross_source_duplicates_after_deduplication() -> None:
    repeated = ImageReference(data=b"same-image", mime_type="image/png")
    unique = ImageReference(data=b"unique-image", mime_type="image/png")
    collected = CollectedReferences(message=[repeated], quote=[repeated], forward=[unique])

    assert len(collected.for_draw()) == 2
    summary = collected.selection_summary()
    assert summary["raw_selected_count"] == 3
    assert summary["selected_count"] == 2
    assert summary["duplicate_count"] == 1


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
    assert extract_prompt_aspect_ratio("画面比例9:19,超高清晰度") == "9:19"
    _, aspect, _ = parse_prompt_options(
        "画面比例9:19,超高清晰度",
        default_aspect_ratio="1:1",
    )
    assert aspect == "9:19"


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


def test_cos_pool_preserves_special_fixed_staging_contract() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = CosPoolStore(directory)
        saved = store.save_custom(
            {
                "id": "custom-special",
                "title": "自定义固定成片",
                "prompt": "完整提示词：窗边逆光、低角度侧后方、CCD颗粒和固定姿势。",
                "mode": "special",
                "special_camera": "first",
            }
        )
        assert saved["mode"] == "special"
        assert saved["special_camera"] == "first"
        restored = store.list_custom()[0]
        action = build_cos_look_action("", picker=lambda **_: restored)
        assert restored["prompt"] in action
        assert "【cos_mode:special】" in action
        assert "【cam:first】" in action
        assert "【cos_pose:" not in action
        assert "【cos_scene:" not in action
        assert "【cos_framing:" not in action


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
