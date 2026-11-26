"""Image model target matching and priority helpers."""

from __future__ import annotations

import re
from typing import Iterable, List, Optional

from ..core.models import ImageModelTarget


NAI_MODEL_UNAVAILABLE = "当前没启用 NAI 模型，请管理员在渠道里启用 NovelAI / nai-diffusion 模型"
NAI_LLM_REQUEST_DISALLOWED = "LLM 生图不能指定或调用 NAI，请通过受白名单保护的 /nai 或 /nai5 指令使用。"
_NAI_MODEL_ID = re.compile(r"(?<![a-z0-9])nai-diffusion(?:-[a-z0-9]+(?:\.[a-z0-9]+)*)*")


def _nai_request_alias(raw: str) -> str:
    text = str(raw or "").strip().lower()
    if re.search(r"(?<![a-z0-9])nai[\s_-]?5(?![a-z0-9])", text):
        return "nai5_alias"
    if re.search(r"(?<![a-z0-9])nai[\s_-]?4(?![a-z0-9])", text):
        return "nai4_alias"
    if re.search(r"(?<![a-z0-9])novel[\s_-]*ai(?![a-z0-9])", text):
        return "nai_alias"
    if re.search(r"(?<![a-z0-9])nai(?![a-z0-9]|[-_]diffusion)", text):
        return "nai_alias"
    return ""


def is_nai_model_request(raw: str) -> bool:
    text = str(raw or "").strip().lower()
    return bool(
        _NAI_MODEL_ID.search(text) or _nai_request_alias(text)
    )


def is_nai_target(target: ImageModelTarget) -> bool:
    provider = str(getattr(target, "provider_type", "") or "").strip().lower().replace("-", "_")
    model = str(getattr(target, "model", "") or "").strip().lower()
    return provider == "novelai" or model.startswith("nai-diffusion")


def route_model_request(
    raw: str,
    targets: Iterable[ImageModelTarget],
) -> tuple[List[ImageModelTarget], str]:
    """Resolve a model hint; explicit NAI requests never fall back to other providers."""
    text = str(raw or "").strip().strip("\"'“”")
    items = list(targets)
    if not text:
        return items, "default"

    lowered = text.lower()
    nai_model = _NAI_MODEL_ID.search(lowered)
    alias = "" if nai_model else _nai_request_alias(lowered)
    if alias:
        nai_targets = [target for target in items if is_nai_target(target)]
        if alias in {"nai4_alias", "nai5_alias"}:
            family = alias[3]
            nai_targets = [
                target
                for target in nai_targets
                if re.match(rf"^nai-diffusion-{family}(?![0-9])", target.model.lower())
            ]
        return nai_targets, alias

    if nai_model:
        model_id = nai_model.group()
        matches = [target for target in items if target.model.lower() == model_id]
        # Preserve a specific channel preference without retrying a different
        # NAI version when the user named a concrete model.
        preferred = next(
            (target for target in matches if lowered in {target.label.lower(), f"{target.channel_name}:{target.model}".lower()}),
            None,
        )
        if preferred is not None:
            matches = [preferred] + [target for target in matches if target.label != preferred.label]
        return matches, "exact" if matches else "unmatched"

    def prioritize(target: ImageModelTarget) -> List[ImageModelTarget]:
        return [target] + [item for item in items if item.label != target.label]

    # Exact label / channel/model / bare model id first.
    for target in items:
        if lowered in {
            target.label.lower(),
            f"{target.channel_name}/{target.model}".lower(),
            f"{target.channel_name}:{target.model}".lower(),
            target.model.lower(),
        }:
            return prioritize(target), "exact"
    # Unique substring hits on the model id, then on the channel name.  A
    # request like "gpt" must not pick arbitrarily between gpt-image-2 and
    # gpt-image-1, so ambiguous hits fall back to default routing.
    for attribute, mode in (("model", "model_substring"), ("channel_name", "channel_substring")):
        hits = [
            target
            for target in items
            if lowered in str(getattr(target, attribute) or "").lower()
        ]
        unique_models = {target.model for target in hits}
        if len(hits) == 1 or (attribute == "model" and len(unique_models) == 1):
            return prioritize(hits[0]), mode
    return items, "unmatched"


def match_model_request_with_mode(
    raw: str,
    targets: Iterable[ImageModelTarget],
) -> tuple[Optional[ImageModelTarget], str]:
    routed, mode = route_model_request(raw, targets)
    return (routed[0] if routed and mode not in {"default", "unmatched"} else None), mode


def find_model_target(
    targets: Iterable[ImageModelTarget],
    channel_name: str = "",
    model: str = "",
) -> Optional[ImageModelTarget]:
    items = list(targets)
    if not channel_name and not model:
        return items[0] if items else None
    for target in items:
        if channel_name and target.channel_name != channel_name:
            continue
        if model and target.model != model:
            continue
        return target
    if channel_name and not model:
        return next(
            (target for target in items if target.channel_name == channel_name),
            None,
        )
    return None


def available_model_labels(targets: Iterable[ImageModelTarget]) -> List[str]:
    labels: List[str] = []
    seen = set()
    for target in targets:
        if target.label in seen:
            continue
        seen.add(target.label)
        labels.append(target.label)
    return labels


def match_model_label(raw: str, labels: Iterable[str]) -> Optional[str]:
    text = str(raw or "").strip()
    items = list(labels)
    if not text or not items:
        return None
    if text.isdigit():
        index = int(text) - 1
        return items[index] if 0 <= index < len(items) else None
    for label in items:
        if text == label or text == label.replace("/", ":"):
            return label
    for label in items:
        channel, _, model = label.partition("/")
        if text == model or text == f"{channel}:{model}":
            return label
    hits = [label for label in items if text in label or label.endswith(f"/{text}")]
    return hits[0] if len(hits) == 1 else None


def prioritize_model_target(
    targets: Iterable[ImageModelTarget], override: str = ""
) -> List[ImageModelTarget]:
    items = list(targets)
    selected = str(override or "").strip()
    if not selected or not items:
        return items
    preferred = next((target for target in items if target.label == selected), None)
    if preferred is None and "/" in selected:
        channel, _, model = selected.partition("/")
        preferred = next(
            (
                target
                for target in items
                if target.channel_name == channel and target.model == model
            ),
            None,
        )
    if preferred is None:
        return items
    return [preferred] + [target for target in items if target.label != preferred.label]


def match_model_request(
    raw: str,
    targets: Iterable[ImageModelTarget],
) -> Optional[ImageModelTarget]:
    """Fuzzy-match an LLM model hint to an enabled target."""
    return match_model_request_with_mode(raw, targets)[0]
