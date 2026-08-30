"""Pure helpers for generation-record metrics and composition metadata."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List


def build_generation_metrics(records: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Aggregate retained generation records without returning prompt content."""
    items = [dict(record) for record in records if isinstance(record, Mapping)]
    status_counts: Dict[str, int] = {}
    category_counts: Dict[str, int] = {}
    model_counts: Dict[str, Dict[str, int]] = {}
    channel_counts: Dict[str, Dict[str, Any]] = {}
    elapsed_values: List[float] = []
    requested = succeeded = failed = 0

    for record in items:
        response = record.get("response_data") if isinstance(record.get("response_data"), Mapping) else {}
        status = str(
            record.get("status")
            or response.get("status")
            or ("succeeded" if record.get("success") else "failed")
        )
        status_counts[status] = status_counts.get(status, 0) + 1
        try:
            requested += max(
                1,
                int(
                    record.get("requested_count")
                    or response.get("requested_count")
                    or record.get("count")
                    or 1
                ),
            )
            succeeded += max(
                0,
                int(
                    record.get("succeeded_count")
                    or response.get("succeeded_count")
                    or (record.get("count") if record.get("success") else 0)
                    or 0
                ),
            )
            failed += max(
                0,
                int(
                    record.get("failed_count")
                    or response.get("failed_count")
                    or (0 if record.get("success") else 1)
                ),
            )
        except (TypeError, ValueError):
            pass

        try:
            elapsed = float(record.get("elapsed_seconds") or response.get("elapsed_seconds") or 0)
            if elapsed > 0:
                elapsed_values.append(elapsed)
        except (TypeError, ValueError):
            pass

        model = str(record.get("used_model") or response.get("used_model") or "未知").strip() or "未知"
        model_bucket = model_counts.setdefault(model, {"records": 0, "success": 0, "failed": 0})
        model_bucket["records"] += 1
        model_bucket["success"] += int(status == "succeeded")
        model_bucket["failed"] += int(status in {"failed", "partial_success"})

        attempts = list(record.get("attempts") or response.get("attempts") or [])
        attempt_channels: List[str] = []
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                continue
            channel = str(
                attempt.get("channel") or attempt.get("provider_type") or "unknown"
            ).strip() or "unknown"
            attempt_channels.append(channel)
            channel_bucket = channel_counts.setdefault(
                channel,
                {
                    "attempts": 0,
                    "success": 0,
                    "failed": 0,
                    "elapsed_seconds": 0.0,
                    "error_categories": {},
                    "fallbacks": 0,
                },
            )
            channel_bucket["attempts"] += 1
            success_attempt = bool(attempt.get("success"))
            channel_bucket["success"] += int(success_attempt)
            channel_bucket["failed"] += int(not success_attempt)
            try:
                channel_bucket["elapsed_seconds"] += max(
                    0.0, float(attempt.get("elapsed_seconds") or 0)
                )
            except (TypeError, ValueError):
                pass
            if not success_attempt:
                category = str(attempt.get("error_category") or "unknown")
                category_counts[category] = category_counts.get(category, 0) + 1
                categories = channel_bucket["error_categories"]
                categories[category] = categories.get(category, 0) + 1

        for previous, current in zip(attempt_channels, attempt_channels[1:]):
            if previous != current:
                channel_counts[current]["fallbacks"] += 1

    elapsed_values.sort()

    def percentile(percent: float) -> float:
        if not elapsed_values:
            return 0.0
        index = min(len(elapsed_values) - 1, int(round((len(elapsed_values) - 1) * percent)))
        return round(elapsed_values[index], 2)

    return {
        "retained_records": len(items),
        "requested_images": requested,
        "succeeded_images": succeeded,
        "failed_images": failed,
        "status_counts": status_counts,
        "error_categories": category_counts,
        "models": model_counts,
        "channels": {
            channel: {
                **values,
                "elapsed_seconds": round(float(values["elapsed_seconds"]), 2),
                "success_rate": round(values["success"] / values["attempts"], 4)
                if values["attempts"]
                else 0.0,
            }
            for channel, values in channel_counts.items()
        },
        "elapsed_seconds": {
            "p50": percentile(0.50),
            "p95": percentile(0.95),
            "max": round(max(elapsed_values), 2) if elapsed_values else 0.0,
        },
    }


def composition_metadata(
    prompt: str,
    source: str,
    aspect_ratio: str,
    resolution: str,
    reference_count: int,
) -> Dict[str, Any]:
    """Classify a prompt for monitoring without retaining its raw text."""
    text = str(prompt or "").strip()
    lowered = text.lower()
    if "看看腿" in text or "look_legs" in lowered:
        strategy = "look_legs"
    elif "全身" in text or "full body" in lowered:
        strategy = "full_body"
    elif "半身" in text or "portrait" in lowered:
        strategy = "half_body"
    else:
        strategy = "selfie_default" if "selfie" in str(source or "").lower() else "custom"
    prompt_hash = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16] if text else ""
    return {
        "strategy": strategy,
        "prompt_hash": prompt_hash,
        "aspect_ratio": str(aspect_ratio or "自动"),
        "resolution": str(resolution or "1K"),
        "reference_image_count": max(0, int(reference_count or 0)),
    }
