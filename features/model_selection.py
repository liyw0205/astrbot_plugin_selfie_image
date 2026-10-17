"""Image/video model target matching and priority helpers."""

from __future__ import annotations

from typing import Iterable, List, Optional

from ..core.models import ImageModelTarget


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
    """Fuzzy-match a free-text model request to one enabled target.

    LLM tool calls pass whatever the user said ("用gpt画一只猫"), so the text
    may be a bare model id, a channel name, or a full ``channel/model`` label.
    The request must only *prefer* a model: anything that cannot be matched
    with confidence keeps the default routing instead of failing the request.
    """
    text = str(raw or "").strip().strip("\"'“”")
    items = list(targets)
    if not text or not items:
        return None
    lowered = text.lower()
    # Exact label / channel/model / bare model id first.
    for target in items:
        if lowered in {
            target.label.lower(),
            f"{target.channel_name}/{target.model}".lower(),
            f"{target.channel_name}:{target.model}".lower(),
            target.model.lower(),
        }:
            return target
    # Unique substring hits on the model id, then on the channel name.  A
    # request like "gpt" must not pick arbitrarily between gpt-image-2 and
    # gpt-image-1, so ambiguous hits fall back to default routing.
    for attribute in ("model", "channel_name"):
        hits = [
            target
            for target in items
            if lowered in str(getattr(target, attribute) or "").lower()
        ]
        unique_models = {target.model for target in hits}
        if len(hits) == 1 or (attribute == "model" and len(unique_models) == 1):
            return hits[0]
    return None
