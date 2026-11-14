"""Bounded process-local latency telemetry for record submissions."""

from __future__ import annotations

from collections import deque
import threading
import time
from collections.abc import Mapping
from typing import Any, Dict

from ..core.utils import redact_sensitive_text


class RecordCommitMetrics:
    SAMPLE_LIMIT = 256

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._samples: deque[tuple[bool, float, float, float]] = deque(maxlen=self.SAMPLE_LIMIT)
        self._success_count = 0
        self._failure_count = 0
        self._last_success: bool | None = None
        self._last_completed_at = ""
        self._last_failure_at = ""
        self._last_failure_reason = ""
        self._last_failure_type = ""

    def observe(self, submitted: float, started: float, finished: float, error: Exception | None = None) -> None:
        success = error is None
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        reason = redact_sensitive_text(str(error))[:320] if error is not None else ""
        with self._lock:
            self._samples.append((
                success,
                max(0.0, started - submitted) * 1000,
                max(0.0, finished - started) * 1000,
                max(0.0, finished - submitted) * 1000,
            ))
            self._success_count += int(success)
            self._failure_count += int(not success)
            self._last_success = success
            self._last_completed_at = timestamp
            if error is not None:
                self._last_failure_at = timestamp
                self._last_failure_reason = reason
                self._last_failure_type = type(error).__name__

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            samples = list(self._samples)
            snapshot = {
                "scope": "process",
                "sample_limit": self.SAMPLE_LIMIT,
                "completed_count": self._success_count + self._failure_count,
                "success_count": self._success_count,
                "failure_count": self._failure_count,
                "status": "idle" if self._last_success is None else "ok" if self._last_success else "error",
                "last_completed_at": self._last_completed_at,
                "last_failure_at": self._last_failure_at,
                "last_failure_reason": self._last_failure_reason,
                "last_failure_type": self._last_failure_type,
            }
        for success, key in ((True, "success_latency_ms"), (False, "failure_latency_ms")):
            selected = [row for row in samples if row[0] is success]
            summary = {}
            for column, name in enumerate(("queue_wait", "write", "total"), start=1):
                values = sorted(row[column] for row in selected)
                summary[name] = {
                    "samples": len(values),
                    "p50": round(values[int(round((len(values) - 1) * 0.50))], 3) if values else 0.0,
                    "p95": round(values[int(round((len(values) - 1) * 0.95))], 3) if values else 0.0,
                    "max": round(values[-1], 3) if values else 0.0,
                }
            snapshot[key] = summary
        return snapshot


class CacheMetrics:
    """Bounded process-local hit and cleanup telemetry without path data."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reference_lookups = 0
        self._reference_hits = 0
        self._reference_misses = 0
        self._cleanup_scans = 0
        self._cleanup_runs = 0
        self._deleted_files = 0
        self._deleted_bytes = 0
        self._delete_failures = 0
        self._last_scan: Dict[str, Any] = {}
        self._last_cleanup: Dict[str, Any] = {}
        self._orphan_growth_baseline: int | None = None
        self._orphan_growth_scan_count = 0

    def reference_lookup(self, *, hit: bool) -> None:
        with self._lock:
            self._reference_lookups += 1
            self._reference_hits += int(hit)
            self._reference_misses += int(not hit)

    def cleanup_scan(self, values: Mapping[str, Any]) -> None:
        scan = dict(values)
        scan["scanned_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with self._lock:
            self._cleanup_scans += 1
            try:
                orphan_count = max(0, int(scan.get("orphan_count") or 0))
            except (TypeError, ValueError, OverflowError):
                orphan_count = None
            if orphan_count is not None:
                if self._orphan_growth_baseline is None:
                    self._orphan_growth_baseline = orphan_count
                    self._orphan_growth_scan_count = 1
                else:
                    self._orphan_growth_scan_count += 1
            self._last_scan = scan

    def cleanup_result(self, values: Mapping[str, Any]) -> None:
        result = dict(values)
        failed = max(0, int(result.get("failed_delete_count") or 0))
        deleted = max(0, int(result.get("deleted_count") or 0))
        deleted_bytes = max(0, int(result.get("deleted_bytes") or 0))
        with self._lock:
            self._cleanup_runs += 1
            self._deleted_files += deleted
            self._deleted_bytes += deleted_bytes
            self._delete_failures += failed
            if deleted:
                self._orphan_growth_baseline = None
                self._orphan_growth_scan_count = 0
            self._last_cleanup = {
                "completed_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "deleted_count": deleted,
                "deleted_bytes": deleted_bytes,
                "failed_delete_count": failed,
            }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            lookups = self._reference_lookups
            hits = self._reference_hits
            orphan_count = self._last_scan.get("orphan_count")
            growth_available = self._orphan_growth_scan_count >= 2 and orphan_count is not None
            orphan_growth = {
                "available": growth_available,
                "baseline_orphan_count": self._orphan_growth_baseline,
                "latest_orphan_count": orphan_count,
                "increase": max(0, int(orphan_count) - int(self._orphan_growth_baseline or 0))
                if growth_available
                else 0,
                "scans_since_baseline": self._orphan_growth_scan_count,
            }
            return {
                "scope": "process",
                "reference_cache": {
                    "lookups": lookups,
                    "hits": hits,
                    "misses": self._reference_misses,
                    "hit_rate": round(hits / lookups, 4) if lookups else None,
                },
                "cleanup": {
                    "scans": self._cleanup_scans,
                    "runs": self._cleanup_runs,
                    "deleted_files": self._deleted_files,
                    "deleted_bytes": self._deleted_bytes,
                    "failed_delete_count": self._delete_failures,
                    "last_scan": dict(self._last_scan),
                    "last_run": dict(self._last_cleanup),
                    "orphan_growth": orphan_growth,
                },
            }
