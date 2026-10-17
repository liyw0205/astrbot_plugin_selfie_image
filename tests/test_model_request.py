"""Tests for LLM-requested model routing (match_model_request)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astrbot_plugin_selfie_image.core.models import ImageModelTarget
from astrbot_plugin_selfie_image.features.model_selection import match_model_request


def make_target(channel: str, model: str) -> ImageModelTarget:
    return ImageModelTarget(
        channel_name=channel,
        provider_type="openai",
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
