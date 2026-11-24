"""Tests for LLM-requested model routing (match_model_request)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astrbot_plugin_selfie_image.core.models import ImageModelTarget
from astrbot_plugin_selfie_image.features.model_selection import (
    NAI_MODEL_UNAVAILABLE,
    is_nai_model_request,
    match_model_request,
    match_model_request_with_mode,
)


def make_target(channel: str, model: str) -> ImageModelTarget:
    return ImageModelTarget(
        channel_name=channel,
        provider_type="openai",
        base_url="https://example.invalid/v1",
        api_key="key",
        model=model,
        timeout=120,
    )


def make_nai_target(channel: str, model: str, provider_type: str = "novelai") -> ImageModelTarget:
    return ImageModelTarget(
        channel_name=channel,
        provider_type=provider_type,
        base_url="https://example.invalid/v1",
        api_key="key",
        model=model,
        timeout=120,
    )


TARGETS = [
    make_target("章鱼", "gpt-image-2"),
    make_target("pai3", "gpt-image-2"),
    make_target("即梦", "jimeng-3.1"),
    make_target("doubao", "seedream-4.0"),
]


def test_exact_model_id_prefers_first_enabled_channel() -> None:
    matched = match_model_request("gpt-image-2", TARGETS)
    assert matched is TARGETS[0]


def test_exact_label_and_channel_model_forms() -> None:
    assert match_model_request("即梦/jimeng-3.1", TARGETS) is TARGETS[2]
    assert match_model_request("doubao:seedream-4.0", TARGETS) is TARGETS[3]


def test_ambiguous_keyword_keeps_default_routing() -> None:
    # "gpt" hits two channels carrying the same model id; the first enabled
    # target wins deterministically instead of failing the request.
    matched = match_model_request("gpt", TARGETS)
    assert matched is not None
    assert matched.model == "gpt-image-2"


def test_unique_substring_matches_model() -> None:
    assert match_model_request("seedream", TARGETS) is TARGETS[3]
    assert match_model_request("jimeng", TARGETS) is TARGETS[2]


def test_channel_name_match() -> None:
    assert match_model_request("章鱼", TARGETS) is TARGETS[0]


def test_unknown_keyword_returns_none() -> None:
    assert match_model_request("flux-pro", TARGETS) is None
    assert match_model_request("", TARGETS) is None


def test_match_is_case_insensitive() -> None:
    assert match_model_request("GPT-Image-2", TARGETS) is TARGETS[0]
    assert match_model_request("Seedream-4.0", TARGETS) is TARGETS[3]


@pytest.mark.parametrize("alias", ["nai", "NAI", "novelai", "novel ai", "用nai画"])
def test_nai_alias_only_matches_nai_targets_in_enabled_order(alias: str) -> None:
    nai4 = make_nai_target("nai4", "nai-diffusion-4-5-full")
    nai5 = make_nai_target("nai5", "nai-diffusion-5-full")
    targets = [TARGETS[0], nai4, nai5]

    matched, mode = match_model_request_with_mode(alias, targets)

    assert matched is nai4
    assert mode == "nai_alias"
    assert match_model_request(alias, [TARGETS[0]]) is None
    assert is_nai_model_request(alias)


def test_nai4_and_nai5_aliases_match_only_their_model_family() -> None:
    nai4 = make_nai_target("nai4", "nai-diffusion-4-5-full")
    nai5 = make_nai_target("nai5", "nai-diffusion-5-full")
    targets = [nai5, TARGETS[0], nai4]

    assert match_model_request("nai4", targets) is nai4
    assert match_model_request("nai5", targets) is nai5
    nai40 = make_nai_target("nai40", "nai-diffusion-40-full")
    assert match_model_request("nai4", [nai40]) is None


def test_nai_model_prefix_is_recognized_even_without_enabled_match() -> None:
    assert is_nai_model_request("nai-diffusion-5-full")
    assert match_model_request("nai-diffusion-5-full", [TARGETS[0]]) is None


def test_nai_alias_does_not_match_unrelated_words() -> None:
    assert not is_nai_model_request("banana")


def test_explicit_nai_request_fails_closed_when_no_nai_target_is_enabled() -> None:
    from astrbot_plugin_selfie_image.main import SelfieImagePlugin

    plugin = object.__new__(SelfieImagePlugin)
    plugin.config = SimpleNamespace(
        group_chat_configs={},
        get_group_image_targets=lambda *_args: [TARGETS[0]],
    )
    plugin._get_session_model_override = lambda _event: ""

    assert plugin._nai_model_unavailable(None, "nai")
    failure = plugin._tool_soft_fail(NAI_MODEL_UNAVAILABLE)
    assert NAI_MODEL_UNAVAILABLE in failure
    assert "不要改用其他模型重试" in failure


def test_explicit_nai_route_keeps_only_enabled_nai_models_in_priority_order() -> None:
    from astrbot_plugin_selfie_image.main import SelfieImagePlugin

    nai4 = make_nai_target("nai4", "nai-diffusion-4-5-full")
    nai5 = make_nai_target("nai5", "nai-diffusion-5-full")
    plugin = object.__new__(SelfieImagePlugin)
    plugin.config = SimpleNamespace(
        group_chat_configs={},
        get_group_image_targets=lambda *_args: [TARGETS[0], nai5, nai4],
    )
    plugin._get_session_model_override = lambda _event: ""

    routed = plugin._prioritize_targets_by_model_request(
        plugin._resolve_generation_targets(), "nai"
    )

    assert routed == [nai5, nai4]
    assert not plugin._nai_model_unavailable(None, "nai")
