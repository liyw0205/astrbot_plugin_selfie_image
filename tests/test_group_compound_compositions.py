"""Check complete group staging through selection, batching and validation."""

import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from astrbot_plugin_selfie_image.core.prompt_data import PromptDataError
from astrbot_plugin_selfie_image.generation.selfie_batch_workflow import SelfieBatchWorkflow
from astrbot_plugin_selfie_image.prompts import group_prompt_pool
from astrbot_plugin_selfie_image.prompts.group_prompt_pool import (
    GROUP_COMPOSITIONS,
    GROUP_COMPOSITIONS_BY_ID,
)
from astrbot_plugin_selfie_image.prompts.selfie_actions import build_group_selfie_action


COMPOUND_IDS = {
    "studio_topdown_cluster_group",
    "living_rug_layered_recline_group",
    "window_seated_standing_group",
    "lantern_corridor_staggered_group",
    "petal_hot_spring_waterline_group",
}


@pytest.mark.parametrize("query", [
    "", "合影", "三人合照", "5人同框", "多人合影 随机", "双人合影 动漫",
    "合影 场景随机，姿势随机", "真人合照 随便",
])
def test_unspecified_group_staging_can_draw_each_compound(query):
    for item_id in COMPOUND_IDS:
        with patch(
            "astrbot_plugin_selfie_image.prompts.selfie_actions.random.choices",
            return_value=[item_id],
        ) as choose:
            action = build_group_selfie_action(query, has_refs=True)

        population = choose.call_args.args[0]
        assert COMPOUND_IDS <= set(population)
        assert choose.call_args.kwargs["weights"] == [
            GROUP_COMPOSITIONS_BY_ID[key]["weight"] for key in population
        ]
        assert GROUP_COMPOSITIONS_BY_ID[item_id]["prompt"] in action
        assert f"【shot:{item_id}】" in action
        assert "窗光或暖灯" not in action
        assert "竖屏手机近景半身" not in action
        assert "不使用远景" not in action
        assert "同框人物的正面、侧面或背侧朝向自然错开" not in action


@pytest.mark.parametrize("query", [
    "海边合影", "三人合影，手持同一束花", "合影 正上方顶拍",
    "合影 不要温泉", "合影 横屏", "合照 夜晚蓝光",
])
def test_explicit_group_staging_is_not_replaced_by_a_random_compound(query):
    with patch(
        "astrbot_plugin_selfie_image.prompts.selfie_actions.random.choices",
        side_effect=lambda population, **_kw: [population[0]],
    ) as choose:
        action = build_group_selfie_action(query)

    assert set(choose.call_args.args[0]) == {"group_portrait", "group_environment"}
    assert f"用户补充要求：{query}" in action
    assert not any(GROUP_COMPOSITIONS_BY_ID[key]["prompt"] in action for key in COMPOUND_IDS)


@pytest.mark.parametrize("item_id", sorted(COMPOUND_IDS))
def test_group_random_draw_avoids_the_previous_compound(item_id):
    with patch(
        "astrbot_plugin_selfie_image.prompts.selfie_actions.random.choices",
        side_effect=lambda population, **_kw: [population[0]],
    ) as choose:
        action = build_group_selfie_action("多人合影", avoid_shot=item_id)

    assert item_id not in choose.call_args.args[0]
    assert COMPOUND_IDS - {item_id} <= set(choose.call_args.args[0])
    assert f"【shot:{item_id}】" not in action


def test_group_batch_redraws_compounds_without_copying_generated_staging():
    plugin = SimpleNamespace(
        _normalize_count=int,
        _build_group_selfie_action=build_group_selfie_action,
    )
    with patch(
        "astrbot_plugin_selfie_image.prompts.selfie_actions.random.choices",
        return_value=["studio_topdown_cluster_group"],
    ):
        initial = build_group_selfie_action("三人合影 随机")
    workflow = SelfieBatchWorkflow(
        plugin, "group-test", None, initial, [], "command-group-selfie",
        4, "9:16", "1K", "failed", rebuild_extra_request="三人合影 随机",
    )
    workflow._prepare_rebuild()
    remaining = iter(sorted(COMPOUND_IDS - {"studio_topdown_cluster_group"}))

    def choose_next(population, **_kw):
        selected = next(remaining)
        assert selected in population
        assert workflow.last_shot not in population
        return [selected]

    with patch(
        "astrbot_plugin_selfie_image.prompts.selfie_actions.random.choices",
        side_effect=choose_next,
    ):
        actions = workflow._build_actions()

    ids = [re.search(r"【shot:([a-z_]+)】", action).group(1) for action in actions]
    assert len(set(ids)) == 4
    for item_id, action in zip(ids, actions):
        assert "用户补充要求：三人合影 随机" in action
        assert GROUP_COMPOSITIONS_BY_ID[item_id]["prompt"] in action
        assert sum(GROUP_COMPOSITIONS_BY_ID[key]["prompt"] in action for key in COMPOUND_IDS) == 1


def test_group_loader_preserves_legacy_defaults():
    assert len(GROUP_COMPOSITIONS) == 14
    assert {item["id"] for item in GROUP_COMPOSITIONS if item["compound"]} == COMPOUND_IDS
    assert not GROUP_COMPOSITIONS_BY_ID["group_portrait"]["compound"]


def test_group_loader_rejects_a_text_boolean_for_compound_flag():
    item = dict(GROUP_COMPOSITIONS_BY_ID["studio_topdown_cluster_group"], compound="false")
    with patch.object(group_prompt_pool, "read_versioned_json", return_value={"version": 1, "items": [item]}):
        with pytest.raises(PromptDataError, match="复合组合标记必须为布尔值"):
            group_prompt_pool._load_group_compositions()
