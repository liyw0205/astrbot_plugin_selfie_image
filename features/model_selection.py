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
