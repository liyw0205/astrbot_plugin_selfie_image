"""Read-only checks for record, sidecar, and cache consistency.

The checker deliberately never mutates the filesystem.  A caller may use the
returned ``repairable`` entries to build an explicit, confirmed repair plan.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List


def _safe_rel(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root)
    except (TypeError, ValueError):
        return str(path or "")


def _record_paths(record: Mapping[str, Any]) -> List[str]:
    response = record.get("response_data")
    response = response if isinstance(response, Mapping) else {}
    paths: List[str] = []
    # Images and videos share the generated cache root.  Keep both fields in
    # the reference set so a retained video is not reported as an orphan.
    for key in ("generated_image_paths", "generated_video_paths"):
        values = record.get(key)
        if not isinstance(values, list) or not values:
            values = response.get(key)
        if not isinstance(values, list):
            values = []
        paths.extend(str(item or "").strip() for item in values if str(item or "").strip())
    return paths


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
    referenced: set[str] = set()
    referenced_sidecars: set[str] = set()
    seen_paths: Dict[str, str] = {}

    for record in rows:
        record_id = str(record.get("id") or "").strip()[:128]
        for raw_path in _record_paths(record):
            try:
                absolute = raw_path if os.path.isabs(raw_path) else os.path.join(cache_root, raw_path)
                absolute = os.path.abspath(absolute)
                if cache_root and not absolute.startswith(cache_root + os.sep):
                    issues.append({"kind": "invalid_path", "record_id": record_id, "path": raw_path, "repairable": False})
                    continue
            except (TypeError, ValueError):
                issues.append({"kind": "invalid_path", "record_id": record_id, "path": raw_path, "repairable": False})
                continue
            rel = _safe_rel(absolute, cache_root) if cache_root else raw_path
            referenced.add(rel)
            previous = seen_paths.get(rel)
            if previous and previous != record_id:
                issues.append({"kind": "duplicate_reference", "record_id": record_id, "other_record_id": previous, "path": rel, "repairable": False})
            else:
                seen_paths[rel] = record_id
            if not os.path.isfile(absolute):
                issues.append({"kind": "missing_cache", "record_id": record_id, "path": rel, "repairable": False})

        declared = str(record.get("media_sources_file") or "").strip()
        response = record.get("response_data")
        response = response if isinstance(response, Mapping) else {}
        has_sources = bool(
            record.get("generated_image_sources")
            or record.get("video_source")
            or response.get("generated_image_sources")
            or response.get("video_source")
            or record.get("video_url")
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
                    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                        issues.append({"kind": "invalid_sidecar", "record_id": record_id, "path": name, "repairable": False})

    if cache_root and os.path.isdir(cache_root):
        for root, _, files in os.walk(cache_root):
            for filename in files:
                absolute = os.path.abspath(os.path.join(root, filename))
                rel = _safe_rel(absolute, cache_root)
                if rel not in referenced:
                    issues.append({"kind": "orphan_cache", "path": rel, "repairable": True})
    if sidecar_root and os.path.isdir(sidecar_root):
        for filename in os.listdir(sidecar_root):
            if not filename.endswith(".json"):
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
    }
