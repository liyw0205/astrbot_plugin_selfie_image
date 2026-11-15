"""Read-only checks for record, sidecar, and cache consistency.

The checker deliberately never mutates the filesystem.  A caller may use the
returned ``repairable`` entries to build an explicit, confirmed repair plan.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List

from ..core.utils import collect_record_cache_paths


def _safe_rel(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root)
    except (TypeError, ValueError):
        return str(path or "")


def _record_paths(record: Mapping[str, Any]) -> List[str]:
    return collect_record_cache_paths(dict(record))


def _sidecar_name(record_id: str) -> str:
    # GenerationStore uses safe IDs for ordinary records and hashed names for
    # unusual IDs.  For the consistency report the declared path is preferred.
    safe = str(record_id or "").strip()
    if safe and all(ch.isalnum() or ch in "_.-" for ch in safe) and len(safe) <= 128:
        return safe + ".json"
    return ""


def inspect_storage_consistency(
    records: Iterable[Mapping[str, Any]],
    *,
    cache_root: str,
    sidecar_root: str,
    protected_paths: Iterable[str] = (),
) -> Dict[str, Any]:
    """Inspect retained records, media sidecars, and cache files.

    ``records`` is treated as an untrusted snapshot.  Every issue is represented
    by a stable category and a bounded path/record identifier so it is suitable
    for an API response and logs.
    """
    cache_root = os.path.abspath(str(cache_root or "")) if cache_root else ""
    sidecar_root = os.path.abspath(str(sidecar_root or "")) if sidecar_root else ""
    rows = [row for row in records or () if isinstance(row, Mapping)]
    issues: List[Dict[str, Any]] = []
    scan_started = time.monotonic()
    scan_file_count = 0
    scan_skipped_count = 0
    referenced: set[str] = set()
    referenced_sidecars: set[str] = set()
    seen_paths: Dict[str, str] = {}
    missing_paths: set[tuple[str, str]] = set()

    def add_cache_reference(raw_path: Any, record_id: str = "", *, protected: bool = False) -> None:
        """Register a record/sidecar/protected path and verify its target."""
        if not cache_root:
            return
        try:
            raw = str(raw_path or "").strip()
            absolute = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(cache_root, raw))
            if absolute == cache_root or not absolute.startswith(cache_root + os.sep):
                raise ValueError("path escapes cache root")
        except (TypeError, ValueError):
            issue = {"kind": "invalid_path", "path": str(raw_path or ""), "repairable": False}
            if record_id:
                issue["record_id"] = record_id
            if protected:
                issue["protected"] = True
            issues.append(issue)
            return
        rel = _safe_rel(absolute, cache_root)
        referenced.add(rel)
        previous = seen_paths.get(rel)
        if previous and previous != record_id:
            issues.append({
                "kind": "duplicate_reference",
                "record_id": record_id,
                "other_record_id": previous,
                "path": rel,
                "repairable": False,
            })
        elif not previous:
            seen_paths[rel] = record_id
        missing_key = (record_id, rel)
        if not os.path.isfile(absolute) and missing_key not in missing_paths:
            missing_paths.add(missing_key)
            issue = {"kind": "missing_cache", "path": rel, "repairable": False}
            if record_id:
                issue["record_id"] = record_id
            if protected:
                issue["protected"] = True
            issues.append(issue)

    for raw_path in protected_paths:
        add_cache_reference(raw_path, protected=True)

    for record in rows:
        record_id = str(record.get("id") or "").strip()[:128]
        for raw_path in _record_paths(record):
            add_cache_reference(raw_path, record_id)

        declared = str(record.get("media_sources_file") or "").strip()
        response = record.get("response_data")
        response = response if isinstance(response, Mapping) else {}
        has_sources = bool(
            record.get("generated_image_sources")
            or response.get("generated_image_sources")
        )
        name = os.path.basename(declared) if declared else (_sidecar_name(record_id) if has_sources else "")
        if name and sidecar_root:
            sidecar_path = os.path.abspath(os.path.join(sidecar_root, name))
            if sidecar_path.startswith(sidecar_root + os.sep):
                referenced_sidecars.add(name)
                if not os.path.isfile(sidecar_path):
                    issues.append({"kind": "missing_sidecar", "record_id": record_id, "path": name, "repairable": True})
                else:
                    try:
                        with open(sidecar_path, "r", encoding="utf-8") as handle:
                            sidecar = json.load(handle)
                        if not isinstance(sidecar, Mapping):
                            raise ValueError("sidecar must be an object")
                        declared_record_id = str(sidecar.get("record_id") or "").strip()
                        if declared_record_id and declared_record_id != record_id:
                            issues.append({
                                "kind": "sidecar_record_mismatch",
                                "record_id": record_id,
                                "sidecar_record_id": declared_record_id[:128],
                                "path": name,
                                "repairable": False,
                            })
                        sources = sidecar.get("generated_image_sources")
                        if not isinstance(sources, list):
                            raise ValueError("generated_image_sources must be a list")
                        for source in sources:
                            if not isinstance(source, Mapping):
                                continue
                            if str(source.get("type") or "").strip().lower() == "cache_path":
                                add_cache_reference(source.get("value"), record_id)
                    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                        issues.append({"kind": "invalid_sidecar", "record_id": record_id, "path": name, "repairable": False})

    if cache_root and os.path.isdir(cache_root):
        for root, _, files in os.walk(cache_root):
            for filename in files:
                scan_file_count += 1
                absolute = os.path.abspath(os.path.join(root, filename))
                rel = _safe_rel(absolute, cache_root)
                if rel not in referenced:
                    issues.append({"kind": "orphan_cache", "path": rel, "repairable": True})
    if sidecar_root and os.path.isdir(sidecar_root):
        for filename in os.listdir(sidecar_root):
            if not filename.endswith(".json"):
                scan_skipped_count += 1
                continue
            if filename not in referenced_sidecars:
                issues.append({"kind": "orphan_sidecar", "path": filename, "repairable": True})

    counts: Dict[str, int] = {}
    for item in issues:
        kind = str(item.get("kind") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    repairable = sum(1 for item in issues if item.get("repairable"))
    return {
        "ok": not issues,
        "record_count": len(rows),
        "referenced_cache_count": len(referenced),
        "referenced_sidecar_count": len(referenced_sidecars),
        "issue_count": len(issues),
        "repairable_count": repairable,
        "counts": counts,
        "issues": issues[:1000],
        "read_only": True,
        "scan_file_count": scan_file_count,
        "scan_skipped_count": scan_skipped_count,
        "scan_elapsed_ms": round((time.monotonic() - scan_started) * 1000),
    }
