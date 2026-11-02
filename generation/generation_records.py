"""Pure helpers for generation-record metrics and composition metadata."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional


METRIC_WINDOW_SECONDS = {
    "1h": 60 * 60,
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}

MODEL_STATUS_BUCKETS = 24


def _status_target_identity(target: Any) -> tuple[str, str, str, str]:
    """Return channel/model identity for a config target or serialized row."""
    if isinstance(target, Mapping):
        channel = str(target.get("channel") or target.get("channel_name") or "").strip()
        model = str(target.get("model") or "").strip()
        label = str(target.get("label") or "").strip()
        provider = str(target.get("provider_type") or "").strip()
    else:
        channel = str(getattr(target, "channel_name", "") or "").strip()
        model = str(getattr(target, "model", "") or "").strip()
        label = str(getattr(target, "label", "") or "").strip()
        provider = str(getattr(target, "provider_type", "") or "").strip()
    if label and (not channel or not model) and "/" in label:
        label_channel, label_model = label.split("/", 1)
        channel = channel or label_channel.strip()
        model = model or label_model.strip()
    channel = channel or "未命名渠道"
    model = model or "未标识模型"
    label = label or f"{channel}/{model}"
    return channel, model, label, provider


def _status_event_identity(event: Mapping[str, Any]) -> tuple[str, str, str, str]:
    channel, model, label, provider = _status_target_identity(event)
    # Older health history rows only stored the channel. Keep these rows
    # visible without pretending they belong to a newly configured model.
    if model == "未标识模型":
        label = f"{channel}/历史模型未标识"
        model = "历史模型未标识"
    return channel, model, label, provider


def build_model_status(
    events: Iterable[Mapping[str, Any]],
    *,
    enabled_targets: Iterable[Any] = (),
    runtime_health: Optional[Mapping[str, Mapping[str, Any]]] = None,
    window_seconds: int = 7 * 24 * 60 * 60,
    now: Optional[float] = None,
    bucket_count: int = MODEL_STATUS_BUCKETS,
) -> Dict[str, Any]:
    """Aggregate persisted model attempts into a compact monitoring payload.

    This deliberately consumes health events rather than issuing provider
    requests. It is safe to use from both a QQ command and the web monitor.
    """
    current = float(now if now is not None else time.time())
    safe_window = max(1, int(window_seconds or 1))
    safe_bucket_count = max(1, int(bucket_count or MODEL_STATUS_BUCKETS))
    cutoff = current - safe_window
    aggregates: Dict[tuple[str, str, str, str], Dict[str, Any]] = {}

    def ensure(identity: tuple[str, str, str, str]) -> Dict[str, Any]:
        row = aggregates.get(identity)
        if row is None:
            channel, model, label, provider = identity
            row = {
                "channel": channel,
                "model": model,
                "label": label,
                "provider_type": provider,
                "attempts": 0,
                "successes": 0,
                "failures": 0,
                "elapsed_seconds": 0.0,
                "elapsed_count": 0,
                "error_categories": {},
                "last_timestamp": 0.0,
                "last_success": None,
                "last_error_category": "",
                "history": [0] * safe_bucket_count,
                "_bucket_success": [0] * safe_bucket_count,
                "_bucket_failure": [0] * safe_bucket_count,
                "_events": [],
            }
            aggregates[identity] = row
        return row

    for raw in events or ():
        if not isinstance(raw, Mapping):
            continue
        media_type = str(raw.get("media_type") or "image").strip().lower()
        if media_type != "image":
            continue
        try:
            timestamp = float(raw.get("timestamp") or 0)
        except (TypeError, ValueError):
            timestamp = 0.0
        if timestamp < cutoff:
            continue
        identity = _status_event_identity(raw)
        row = ensure(identity)
        success = bool(raw.get("success"))
        row["attempts"] += 1
        row["successes"] += int(success)
        row["failures"] += int(not success)
        category = str(raw.get("error_category") or "unknown").strip() or "unknown"
        row["_events"].append((timestamp, success, category))
        if not success:
            categories = row["error_categories"]
            categories[category] = int(categories.get(category) or 0) + 1
        try:
            elapsed = max(0.0, float(raw.get("elapsed_seconds") or 0))
        except (TypeError, ValueError):
            elapsed = 0.0
        if elapsed > 0:
            row["elapsed_seconds"] += elapsed
            row["elapsed_count"] += 1
        if timestamp >= row["last_timestamp"]:
            row["last_timestamp"] = timestamp
        bucket = min(
            safe_bucket_count - 1,
            max(0, int((timestamp - cutoff) / safe_window * safe_bucket_count))
            if safe_window
            else safe_bucket_count - 1,
        )
        if success:
            row["_bucket_success"][bucket] += 1
        else:
            row["_bucket_failure"][bucket] += 1

    # Include configured models even when they have no events in this window.
    configured: Dict[tuple[str, str, str, str], None] = {}
    for target in enabled_targets or ():
        identity = _status_target_identity(target)
        configured[identity] = None
        ensure(identity)

    health_by_channel = runtime_health if isinstance(runtime_health, Mapping) else {}
    models: List[Dict[str, Any]] = []
    for identity, row in aggregates.items():
        attempts = int(row["attempts"])
        successes = int(row["successes"])
        failures = int(row["failures"])
        success_rate = successes / attempts if attempts else 0.0
        average_elapsed = row["elapsed_seconds"] / row["elapsed_count"] if row["elapsed_count"] else 0.0
        ordered_events = sorted(row["_events"], key=lambda item: item[0])
        if ordered_events:
            latest_timestamp, latest_success, latest_event_category = ordered_events[-1]
            row["last_timestamp"] = latest_timestamp
            row["last_success"] = latest_success
            row["last_error_category"] = "" if latest_success else latest_event_category
        consecutive_failures = 0
        for _, event_success, _ in reversed(ordered_events):
            if event_success:
                break
            consecutive_failures += 1
        latest_category = str(row.get("last_error_category") or "").strip().lower()
        channel_health = health_by_channel.get(row["channel"], {})
        if not isinstance(channel_health, Mapping):
            channel_health = {}
        # The runtime health cache predates model-level monitoring and is
        # channel-scoped. Use it only as a conservative cooldown hint; the
        # per-model failure streak above remains the source of truth.
        cooling_down = bool(channel_health.get("cooling_down")) and row.get("last_success") is False
        if attempts == 0:
            status = "未使用"
        elif latest_category in {"auth", "authentication", "unauthorized", "forbidden"} and row.get("last_success") is False:
            status = "验证失败"
        elif latest_category in {"rate_limit", "quota", "throttle"} and row.get("last_success") is False:
            status = "限流"
        elif failures == attempts:
            status = "断流"
        elif cooling_down or consecutive_failures >= 3 or success_rate < 0.7:
            status = "异常"
        elif success_rate < 0.9 or average_elapsed >= 8 or failures:
            status = "降级"
        else:
            status = "正常"
        history: List[str] = []
        for success_count, failure_count in zip(row["_bucket_success"], row["_bucket_failure"]):
            if not success_count and not failure_count:
                history.append("-")
            elif failure_count and not success_count:
                history.append("×")
            elif failure_count or average_elapsed >= 8:
                history.append("▲")
            else:
                history.append("■")
        category_counts = dict(sorted(row["error_categories"].items(), key=lambda item: (-item[1], item[0])))
        models.append(
            {
                "channel": row["channel"],
                "model": row["model"],
                "label": row["label"],
                "provider_type": row["provider_type"],
                "status": status,
                "attempts": attempts,
                "successes": successes,
                "failures": failures,
                "success_rate": round(success_rate, 4),
                "average_elapsed_seconds": round(average_elapsed, 2),
                "last_timestamp": row["last_timestamp"],
                "last_success": row["last_success"],
                "last_error_category": row["last_error_category"],
                "error_categories": category_counts,
                "consecutive_failures": consecutive_failures,
                "cooling_down": cooling_down,
                "history": "".join(history),
                "configured": identity in configured,
            }
        )
    models.sort(key=lambda item: (not item["configured"], item["channel"], item["model"]))
    if any(item["status"] in {"异常", "断流", "验证失败", "限流"} for item in models):
        overall_status = "异常"
    elif any(item["status"] == "降级" for item in models):
        overall_status = "降级"
    elif models and all(item["status"] == "未使用" for item in models):
        overall_status = "未使用"
    elif models:
        overall_status = "正常"
    else:
        overall_status = "无模型"
    alerts = []
    for item in models:
        if item["status"] not in {"正常", "未使用"}:
            alerts.append(f"{item['label']}：{item['status']}")
    return {
        "window_seconds": safe_window,
        "updated_at": current,
        "overall_status": overall_status,
        "models": models,
        "model_count": len(models),
        "channel_count": len({item["channel"] for item in models}),
        "available_count": sum(1 for item in models if item["status"] in {"正常", "降级"} and item["successes"] > 0),
        "alert_count": sum(1 for item in models if item["status"] not in {"正常", "未使用"}),
        "unused_count": sum(1 for item in models if item["status"] == "未使用"),
        "alerts": alerts,
    }


def metric_window_seconds(value: Any) -> Optional[int]:
    """Normalize a metrics window key; ``None`` means all retained records."""
    key = str(value or "all").strip().lower()
    if key in {"", "all", "全部"}:
        return None
    seconds = METRIC_WINDOW_SECONDS.get(key)
    if seconds is None:
        raise ValueError("window 必须是 all、1h、24h、7d 或 30d")
    return seconds


def _record_timestamp(record: Mapping[str, Any]) -> float:
    # ``time`` is the durable generation-record time. ``created_ts`` can be
    # filled by a bulk database rewrite, which gives many records the same
    # value and is therefore unsuitable for media age ordering.
    text = str(record.get("time") or "").strip()
    if text:
        try:
            return time.mktime(time.strptime(text[:19], "%Y-%m-%d %H:%M:%S"))
        except (TypeError, ValueError, OverflowError):
            pass
    for key in ("created_ts", "timestamp", "updated_ts"):
        try:
            value = float(record.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return 0.0


def _metric_recommendations(
    channels: Mapping[str, Mapping[str, Any]],
    error_categories: Mapping[str, int],
    quality: Mapping[str, Any],
    elapsed: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    recommendations: List[Dict[str, Any]] = []
    for channel, bucket in channels.items():
        attempts = int(bucket.get("attempts") or 0)
        success_rate = float(bucket.get("success_rate") or 0)
        if attempts >= 3 and success_rate < 0.8:
            recommendations.append(
                {
                    "code": "channel_low_success",
                    "severity": "warning",
                    "scope": "channel",
                    "target": channel,
                    "value": round(success_rate, 4),
                    "message": f"渠道 {channel} 成功率较低，建议检查上游状态或调整优先级",
                }
            )
    attempts_total = sum(int(bucket.get("attempts") or 0) for bucket in channels.values())
    rate_limit_failures = int(error_categories.get("rate_limit") or 0)
    if attempts_total >= 3 and rate_limit_failures >= 2 and rate_limit_failures / attempts_total >= 0.3:
        recommendations.append(
            {
                "code": "rate_limit_pressure",
                "severity": "warning",
                "scope": "system",
                "target": "rate_limit",
                "value": rate_limit_failures,
                "message": "近期限流错误偏多，建议降低并发或增加备用 Key/渠道",
            }
        )
    retry_rate = float(quality.get("retry_rate") or 0)
    if int(quality.get("records") or 0) >= 3 and retry_rate >= 0.5:
        recommendations.append(
            {
                "code": "retry_heavy",
                "severity": "info",
                "scope": "system",
                "target": "retries",
                "value": round(retry_rate, 4),
                "message": "过半生成记录发生过重试，建议检查模型优先级和失败原因分布",
            }
        )
    p95 = float(elapsed.get("p95") or 0)
    if int(quality.get("records") or 0) >= 3 and p95 >= 60:
        recommendations.append(
            {
                "code": "slow_generation",
                "severity": "info",
                "scope": "system",
                "target": "latency",
                "value": round(p95, 2),
                "message": "P95 生成耗时较高，建议检查排队等待、代理和上游响应时间",
            }
        )
    return recommendations


def build_generation_metrics(
    records: Iterable[Mapping[str, Any]],
    *,
    window_seconds: Optional[int] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Aggregate retained generation records without returning prompt content."""
    all_items = [dict(record) for record in records if isinstance(record, Mapping)]
    if window_seconds is not None:
        try:
            safe_window = max(1, int(window_seconds))
        except (TypeError, ValueError):
            safe_window = 1
        cutoff = float(now if now is not None else time.time()) - safe_window
        items = [record for record in all_items if _record_timestamp(record) >= cutoff]
    else:
        safe_window = None
        items = all_items
    status_counts: Dict[str, int] = {}
    category_counts: Dict[str, int] = {}
    model_counts: Dict[str, Dict[str, int]] = {}
    channel_counts: Dict[str, Dict[str, Any]] = {}
    elapsed_values: List[float] = []
    requested = succeeded = failed = 0
    retries_total = 0
    records_with_retries = 0
    retry_action_counts: Dict[str, int] = {}
    successful_records = 0
    partial_records = 0
    cancelled_records = 0
    fallback_total = 0

    for record in items:
        response = record.get("response_data") if isinstance(record.get("response_data"), Mapping) else {}
        status = str(
            record.get("status")
            or response.get("status")
            or ("succeeded" if record.get("success") else "failed")
        )
        status_counts[status] = status_counts.get(status, 0) + 1
        successful_records += int(status == "succeeded")
        partial_records += int(status == "partial_success")
        cancelled_records += int(status == "cancelled")
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
        model_bucket = model_counts.setdefault(model, {"records": 0, "success": 0, "failed": 0, "partial": 0})
        model_bucket["records"] += 1
        model_bucket["success"] += int(status == "succeeded")
        model_bucket["failed"] += int(status in {"failed", "partial_success"})
        model_bucket["partial"] += int(status == "partial_success")

        attempts = list(record.get("attempts") or response.get("attempts") or [])
        failed_attempts = [item for item in attempts if isinstance(item, Mapping) and not item.get("success")]
        try:
            retry_count = max(0, int(record.get("retry_count") or response.get("retry_count") or len(failed_attempts)))
        except (TypeError, ValueError):
            retry_count = len(failed_attempts)
        retries_total += retry_count
        records_with_retries += int(retry_count > 0)
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
            action = str(attempt.get("retry_action") or "").strip()
            if action:
                retry_action_counts[action] = retry_action_counts.get(action, 0) + 1

        for previous, current in zip(attempt_channels, attempt_channels[1:]):
            if previous != current:
                channel_counts[current]["fallbacks"] += 1
                fallback_total += 1

    elapsed_values.sort()

    def percentile(percent: float) -> float:
        if not elapsed_values:
            return 0.0
        index = min(len(elapsed_values) - 1, int(round((len(elapsed_values) - 1) * percent)))
        return round(elapsed_values[index], 2)

    elapsed_summary = {
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "max": round(max(elapsed_values), 2) if elapsed_values else 0.0,
    }
    for bucket in model_counts.values():
        records_count = int(bucket.get("records") or 0)
        bucket["success_rate"] = round(bucket["success"] / records_count, 4) if records_count else 0.0
        bucket["failure_rate"] = round(bucket["failed"] / records_count, 4) if records_count else 0.0
    total_records = len(items)
    total_attempts = sum(int(bucket.get("attempts") or 0) for bucket in channel_counts.values())
    quality = {
        "records": total_records,
        "record_success_rate": round(successful_records / total_records, 4) if total_records else 0.0,
        "image_success_rate": round(succeeded / requested, 4) if requested else 0.0,
        "partial_success_records": partial_records,
        "cancelled_records": cancelled_records,
        "retry_rate": round(records_with_retries / total_records, 4) if total_records else 0.0,
        "fallback_rate": round(fallback_total / total_attempts, 4) if total_attempts else 0.0,
        "average_elapsed_seconds": round(sum(elapsed_values) / len(elapsed_values), 2) if elapsed_values else 0.0,
    }
    return {
        "retained_records": total_records,
        "total_retained_records": len(all_items),
        "window_seconds": safe_window or 0,
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
        "elapsed_seconds": elapsed_summary,
        "quality": quality,
        "retries": {
            "total": retries_total,
            "records_with_retries": records_with_retries,
            "actions": retry_action_counts,
        },
        "recommendations": _metric_recommendations(channel_counts, category_counts, quality, elapsed_summary),
    }


def build_record_scope_stats(records: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Aggregate the exact filtered record scope used by a paged list."""
    items = [record for record in records if isinstance(record, Mapping)]
    success_count = sum(1 for record in items if bool(record.get("success")))
    failure_count = len(items) - success_count
    elapsed_values: List[float] = []
    for record in items:
        try:
            value = float(record.get("elapsed_seconds") or 0)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            elapsed_values.append(value)
    sample_count = len(items)
    return {
        "sample_count": sample_count,
        "success_count": success_count,
        "failure_count": failure_count,
        "success_rate": round(success_count / sample_count * 100, 2) if sample_count else None,
        "failure_rate": round(failure_count / sample_count * 100, 2) if sample_count else None,
        "average_elapsed_seconds": round(sum(elapsed_values) / len(elapsed_values), 3)
        if elapsed_values
        else None,
        "scope": "filtered",
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
