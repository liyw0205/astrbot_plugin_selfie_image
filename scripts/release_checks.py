"""Standard-library-only contracts and packaging helpers for release gates."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "astrbot_plugin_selfie_image"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 12 * 1024 * 1024
MAX_ARCHIVE_BYTES = 6 * 1024 * 1024
MODULE_DIRS = frozenset({"core", "cos", "features", "generation", "nai", "prompts", "studio", "tasks", "webui"})
DATA_DIRS = frozenset({"common", "cos", "group", "legs", "nai", "persona", "schedule", "studio"})
ROOT_FILES = frozenset({
    "__init__.py", "main.py", "metadata.yaml", "_conf_schema.json", "requirements.txt",
    "README.md", "CHANGELOG.md", "LICENSE", "LICENSE.md", "LICENSE.txt", "logo.png",
})
PAGE_FILES = frozenset({"pages/dashboard/index.html", "pages/dashboard/_page.json"})
CHANGELOG_ARCHIVES = frozenset({"data/CHANGELOG_1.7.md", "data/CHANGELOG_HISTORY.md"})
# Deliberately adopted runtime sources may be checked before their first commit.
# No other untracked file is eligible for a release archive.
APPROVED_RUNTIME_ADDITIONS = frozenset({
    "generation/record_metrics.py",
    "generation/web_test_workflow.py",
    "generation/counted_workflow.py",
    "generation/image_command_service.py",
    "generation/image_prompt_service.py",
    "generation/runtime_service.py",
    "generation/task_service.py",
    "generation/selfie_batch_workflow.py",
    "generation/selfie_workflow.py",
    "generation/help_poster.py",
    "core/async_compat.py",
    "core/persistent_store.py",
    "webui/controllers.py",
    "webui/prompt_controller.py",
    "features/backup_manager.py",
    "features/backup_web_service.py",
    "features/selfie_profile_web_service.py",
    "features/archive_transfer.py",
    "features/identity_pool.py",
    "data/persona/builtin_personas.json",
})
REQUIRED_FILES = frozenset({
    "__init__.py", "main.py", "metadata.yaml", "_conf_schema.json", "requirements.txt",
    "README.md", "CHANGELOG.md", "logo.png",
    "webui/index_fallback.py", "core/constants.py", "data/cos/looks/index.json",
    "data/common/selfie_snapshots.json", "data/group/compositions.json",
    "data/legs/compositions.json", "data/legs/outfits.json", "data/legs/views.json",
    "data/nai/artist_presets.json", "data/nai/reference.json", "data/schedule/prompt_pools.json",
    "data/studio/builtin_prompts.json",
    "features/identity_pool.py", "data/persona/builtin_personas.json",
    *(f"{name}/__init__.py" for name in MODULE_DIRS),
    *PAGE_FILES,
})
PERIODS = frozenset({"morning", "noon", "afternoon", "evening", "night", "late_night"})


class ReleaseCheckError(RuntimeError):
    """A release input or generated artifact violates a repository contract."""


def git_files(root: Path = ROOT) -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, stdout=subprocess.PIPE,
    )
    return {item for item in result.stdout.decode("utf-8").split("\0") if item}


def runtime_path(name: str) -> bool:
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in name:
        return False
    if any(part.startswith(".") or part in {"__pycache__", "research", "status-preview"} for part in path.parts):
        return False
    if name in ROOT_FILES or name in PAGE_FILES or name in CHANGELOG_ARCHIVES:
        return True
    if path.parts[0] in MODULE_DIRS:
        return path.suffix == ".py"
    if path.parts[0] == "data" and len(path.parts) >= 3 and path.parts[1] in DATA_DIRS:
        return path.suffix in {".json", ".md"}
    return path.parts[0] == "assets" and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".ttf", ".ttc", ".otf"}


def source_file(root: Path, name: str) -> Path:
    path = root / name
    if not runtime_path(name):
        raise ReleaseCheckError(f"file is outside the runtime release allowlist: {name}")
    if any((root / relative).is_symlink() for relative in (PurePosixPath(name), *PurePosixPath(name).parents)):
        raise ReleaseCheckError(f"release inputs must not be symlinks: {name}")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ReleaseCheckError(f"release input is missing or escapes the project: {name}")
    return path


def release_files(root: Path = ROOT, *, tracked: Iterable[str] | None = None) -> list[str]:
    candidates = set(git_files(root) if tracked is None else tracked)
    candidates.update(name for name in APPROVED_RUNTIME_ADDITIONS if (root / name).is_file())
    selected = sorted(name for name in candidates if runtime_path(name))
    missing = REQUIRED_FILES.difference(selected)
    if missing:
        raise ReleaseCheckError(f"required release files are missing: {', '.join(sorted(missing))}")
    for name in selected:
        source_file(root, name)
    validate_runtime_imports(root, selected)
    return selected


def validate_runtime_imports(root: Path, names: Iterable[str]) -> None:
    """Reject archives that silently omit a newly introduced local module."""

    included = set(names)

    def require_module(parts: tuple[str, ...], importer: str) -> None:
        module = PurePosixPath(*parts)
        choices = {f"{module}.py", str(module / "__init__.py")} if parts else {"__init__.py"}
        if not choices.intersection(included):
            raise ReleaseCheckError(f"{importer}: local import omitted from release manifest: {'.'.join(parts)}")

    for name in sorted(included):
        if not name.endswith(".py"):
            continue
        tree = ast.parse((root / name).read_text(encoding="utf-8"), filename=name)
        parent = PurePosixPath(name).parent.parts
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            if node.level > len(parent) + 1:
                raise ReleaseCheckError(f"{name}: relative import escapes the plugin package")
            base = parent[:len(parent) - node.level + 1]
            parts = base + tuple((node.module or "").split(".")) if node.module else base
            require_module(parts, name)
            if node.module is None:
                for alias in node.names:
                    candidate = PurePosixPath(*base, alias.name)
                    if (root / f"{candidate}.py").is_file() or (root / candidate / "__init__.py").is_file():
                        require_module(base + (alias.name,), name)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ReleaseCheckError(f"{label}: expected a nonempty object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseCheckError(f"{label}: expected a nonempty string")
    return value


def _list(value: Any, label: str, *, nonempty: bool = True) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ReleaseCheckError(f"{label}: expected {'a nonempty' if nonempty else 'an'} array")
    return value


def _rows(value: Any, label: str, fields: tuple[str, ...] = ("id", "title", "prompt")) -> list[dict[str, Any]]:
    seen: set[str] = set()
    rows = _list(value, label)
    for index, row in enumerate(rows):
        row_label = f"{label}[{index}]"
        _object(row, row_label)
        for field in fields:
            _text(row.get(field), f"{row_label}.{field}")
        identity = row[fields[0]]
        if identity in seen:
            raise ReleaseCheckError(f"{row_label}: duplicate {fields[0]} {identity}")
        seen.add(identity)
    return rows


def _version(data: dict[str, Any], key: str, name: str) -> None:
    if type(data.get(key)) is not int or data[key] != 1:
        raise ReleaseCheckError(f"{name}.{key}: expected supported version 1")


def validate_json_contract(name: str, data: Any) -> None:
    """Check required fields used by the current bundled-data readers."""

    if name not in {"_conf_schema.json", "pages/dashboard/_page.json"} and not name.startswith("data/"):
        return
    if name == "data/persona/builtin_personas.json":
        seen: set[str] = set()
        for index, row in enumerate(_list(data, name)):
            row = _object(row, f"{name}[{index}]")
            persona_name = _text(row.get("name"), f"{name}[{index}].name")
            _text(row.get("prompt"), f"{name}[{index}].prompt")
            if persona_name in seen:
                raise ReleaseCheckError(f"{name}[{index}]: duplicate persona name {persona_name}")
            seen.add(persona_name)
        return
    _object(data, name)
    if name == "_conf_schema.json":
        web = _object(data.get("web"), f"{name}.web")
        if web.get("type") != "object":
            raise ReleaseCheckError(f"{name}.web.type: expected object")
        items = _object(web.get("items"), f"{name}.web.items")
        for field, kind, value_type in (("enable", "bool", bool), ("host", "string", str), ("port", "int", int), ("token", "string", str)):
            item = _object(items.get(field), f"{name}.web.items.{field}")
            _text(item.get("description"), f"{name}.web.items.{field}.description")
            if item.get("type") != kind or type(item.get("default")) is not value_type:
                raise ReleaseCheckError(f"{name}.web.items.{field}: type/default mismatch")
        return
    if name == "pages/dashboard/_page.json":
        for field in ("title", "description"):
            localized = _object(data.get(field), f"{name}.{field}")
            for locale in ("zh-CN", "en-US"):
                _text(localized.get(locale), f"{name}.{field}.{locale}")
        return
    if not name.startswith("data/"):
        return
    _version(data, "schema_version" if name.startswith("data/cos/") else "version", name)
    if name == "data/cos/looks/index.json":
        for index, row in enumerate(_list(data.get("files"), f"{name}.files")):
            row = _object(row, f"{name}.files[{index}]")
            _text(row.get("cos_type"), f"{name}.files[{index}].cos_type")
            filename = _text(row.get("file"), f"{name}.files[{index}].file")
            if PurePosixPath(filename).name != filename or not filename.endswith(".json"):
                raise ReleaseCheckError(f"{name}: unsafe catalog filename {filename}")
            if type(row.get("count")) is not int or row["count"] < 1:
                raise ReleaseCheckError(f"{name}: invalid count for {filename}")
        for value in _list(data.get("order"), f"{name}.order"):
            _text(value, f"{name}.order")
    elif name.startswith("data/cos/looks/"):
        _text(data.get("cos_type"), f"{name}.cos_type")
        _rows(data.get("items"), f"{name}.items", ("id", "title", "prompt", "cos_type"))
    elif name == "data/cos/_legacy_groups.json":
        for key, values in _object(data.get("groups"), f"{name}.groups").items():
            for value in _list(values, f"{name}.groups.{key}"):
                _text(value, f"{name}.groups.{key}")
    elif name.startswith("data/cos/"):
        for key, row in _object(data.get("items"), f"{name}.items").items():
            row = _object(row, f"{name}.items.{key}")
            _text(row.get("title") or row.get("label"), f"{name}.items.{key}.title")
            for field in (("scene_id", "pose_id") if name.endswith("_combinations.json") else ("prompt",)):
                _text(row.get(field), f"{name}.items.{key}.{field}")
    elif name == "data/common/selfie_snapshots.json":
        for item in _list(data.get("items"), f"{name}.items"):
            _text(item, f"{name}.items")
    elif name in {"data/group/compositions.json", "data/studio/builtin_prompts.json"}:
        rows = _rows(data.get("items"), f"{name}.items")
        if name.startswith("data/studio/"):
            for row in rows:
                for item in _list(row.get("templates"), f"{name}.{row['id']}.templates", nonempty=False):
                    _text(item, f"{name}.{row['id']}.templates")
                if "global" in row and not isinstance(row["global"], bool):
                    raise ReleaseCheckError(f"{name}.{row['id']}.global: expected bool")
    elif name == "data/legs/views.json":
        _rows(data.get("views"), f"{name}.views", ("id", "title", "prompt", "camera", "direction", "elevation"))
    elif name == "data/legs/compositions.json":
        for field in ("framing", "anatomy_rule", "full_framing", "scene_rule"):
            _text(data.get(field), f"{name}.{field}")
        for field in ("camera_framings", "reference_rules", "appearance_prompts"):
            _object(data.get(field), f"{name}.{field}")
        for field in ("poses", "scenes"):
            _rows(data.get(field), f"{name}.{field}")
    elif name == "data/legs/outfits.json":
        for field in ("preserve_prompts", "pool_reference_prompts", "wear_prompts", "wear_labels", "capture_profiles"):
            _object(data.get(field), f"{name}.{field}")
        for field in ("random_leg_details", "outfits", "hosiery_styles"):
            _rows(data.get(field), f"{name}.{field}")
    elif name == "data/nai/artist_presets.json":
        for field in ("default_artist", "quality_prompt", "translation_template"):
            _text(data.get(field), f"{name}.{field}")
        _rows(data.get("presets"), f"{name}.presets", ("name", "prompt"))
    elif name == "data/nai/reference.json":
        _text(data.get("resolve_template"), f"{name}.resolve_template")
        for item in _list(data.get("control_tags"), f"{name}.control_tags"):
            _text(item, f"{name}.control_tags")
    elif name == "data/schedule/prompt_pools.json":
        daily = _object(data.get("daily"), f"{name}.daily")
        for field in ("scene_variants", "action_variants"):
            values = _object(daily.get(field), f"{name}.daily.{field}")
            if set(values) != PERIODS:
                raise ReleaseCheckError(f"{name}.daily.{field}: must cover all six periods")
            for period, items in values.items():
                for item in _list(items, f"{name}.daily.{field}.{period}"):
                    _text(item, f"{name}.daily.{field}.{period}")
        routes = _rows(daily.get("schedule_routes"), f"{name}.daily.schedule_routes", ("id", "theme"))
        for route in routes:
            events = _object(route.get("events"), f"{name}.{route['id']}.events")
            if set(events) != PERIODS:
                raise ReleaseCheckError(f"{name}.{route['id']}.events: must cover all six periods")
            for period, event in events.items():
                _object(event, f"{name}.{route['id']}.{period}")
                for field in ("status", "scene", "action"):
                    _text(event.get(field), f"{name}.{route['id']}.{period}.{field}")
        for field in ("extra_outfits", "mix_tops", "mix_bottoms", "one_piece_looks"):
            _list(daily.get(field), f"{name}.daily.{field}")
        for field in ("seed_choices", "fallback_outfits"):
            _object(daily.get(field), f"{name}.daily.{field}")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def validate_json_files(root: Path = ROOT, *, tracked: Iterable[str] | None = None) -> int:
    names = set(git_files(root) if tracked is None else tracked)
    payloads: dict[str, Any] = {}
    for name in sorted(item for item in names if item.endswith(".json")):
        try:
            data = json.loads((root / name).read_text(encoding="utf-8"), object_pairs_hook=_unique_keys, parse_constant=_invalid_constant)
            validate_json_contract(name, data)
        except (OSError, UnicodeError, ValueError, ReleaseCheckError) as exc:
            raise ReleaseCheckError(f"invalid JSON contract: {name}: {exc}") from exc
        payloads[name] = data
    index_name = "data/cos/looks/index.json"
    if index_name in payloads:
        look_ids: list[str] = []
        filenames: set[str] = set()
        for entry in payloads[index_name]["files"]:
            name = f"data/cos/looks/{entry['file']}"
            if name in filenames or name not in payloads:
                raise ReleaseCheckError(f"{index_name}: duplicate or untracked catalog file {name}")
            filenames.add(name)
            payload = payloads[name]
            rows = payload["items"]
            if len(rows) != entry["count"] or payload["cos_type"] != entry["cos_type"]:
                raise ReleaseCheckError(f"{index_name}: count/cos_type mismatch for {name}")
            if any(row["cos_type"] != payload["cos_type"] for row in rows):
                raise ReleaseCheckError(f"{name}: item cos_type differs from its catalog")
            look_ids.extend(row["id"] for row in rows)
        order = payloads[index_name]["order"]
        # The runtime accepts a partial legacy order and folds duplicate order
        # entries into a position map; only catalog identities must be unique.
        if len(set(look_ids)) != len(look_ids) or set(order).difference(look_ids):
            raise ReleaseCheckError(f"{index_name}: duplicate catalog ids or unknown ordered ids")
        for name, ids in payloads.get("data/cos/_legacy_groups.json", {}).get("groups", {}).items():
            if set(ids).difference(look_ids):
                raise ReleaseCheckError(f"legacy group {name}: unknown catalog id")
    return len(payloads)


def validate_versions(root: Path = ROOT) -> str:
    constants = ast.parse((root / "core/constants.py").read_text(encoding="utf-8"))
    values = [ast.literal_eval(node.value) for node in constants.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "PLUGIN_VERSION" for target in node.targets)]
    if len(values) != 1 or not isinstance(values[0], str) or not re.fullmatch(r"\d+\.\d+\.\d+", values[0]):
        raise ReleaseCheckError("core/constants.py: missing or ambiguous PLUGIN_VERSION")
    version = values[0]
    # These are deliberately anchored to current-version declarations, not
    # historical release mentions or examples elsewhere in the documents.
    patterns = {
        "metadata.yaml": r"^version:\s*['\"]?(\d+\.\d+\.\d+)['\"]?\s*$",
        "README.md": r"^- 当前稳定版：`(\d+\.\d+\.\d+)`\s*$",
        "CHANGELOG.md": r"^## (\d+\.\d+\.\d+) - \d{4}-\d{2}-\d{2}\s*$",
    }
    for name, pattern in patterns.items():
        matches = re.findall(pattern, (root / name).read_text(encoding="utf-8"), re.MULTILINE)
        if not matches or matches[0] != version or (name != "CHANGELOG.md" and len(matches) != 1):
            raise ReleaseCheckError(f"version mismatch: core/constants.py={version}, {name}={matches[:2]}")
    return version


def validate_tag_version(root: Path, tag: str) -> str:
    """Require a bare ``vMAJOR.MINOR.PATCH`` tag matching the release version."""

    match = re.fullmatch(r"v(\d+\.\d+\.\d+)", tag)
    if not match:
        raise ReleaseCheckError(
            f"release tag must match vMAJOR.MINOR.PATCH: {tag!r}"
        )
    version = validate_versions(root)
    tag_version = match.group(1)
    if tag_version != version:
        raise ReleaseCheckError(
            f"release tag/version mismatch: tag={tag!r}, plugin={version!r}"
        )
    return version


def build_artifact(root: Path, output: Path, names: Iterable[str]) -> dict[str, Any]:
    """Create and read back a deterministic ZIP from an already-approved list."""

    selected = list(names)
    if not selected or selected != sorted(set(selected)):
        raise ReleaseCheckError("artifact source list must be nonempty, sorted and unique")
    source: dict[str, bytes] = {}
    total_bytes = 0
    for name in selected:
        path = source_file(root, name)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ReleaseCheckError(f"release file exceeds {MAX_FILE_BYTES} bytes: {name} ({size})")
        total_bytes += size
        if total_bytes > MAX_UNCOMPRESSED_BYTES:
            raise ReleaseCheckError(f"release contents exceed {MAX_UNCOMPRESSED_BYTES} bytes")
        source[name] = path.read_bytes()
        if len(source[name]) != size:
            raise ReleaseCheckError(f"release source changed while reading: {name}")
    if output.exists():
        raise ReleaseCheckError(f"refusing to overwrite an existing release artifact: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in source.items():
            info = zipfile.ZipInfo(f"{PLUGIN_NAME}/{name}", date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    archive_bytes = output.stat().st_size
    if archive_bytes > MAX_ARCHIVE_BYTES:
        raise ReleaseCheckError(f"release ZIP exceeds {MAX_ARCHIVE_BYTES} bytes ({archive_bytes})")
    with zipfile.ZipFile(output) as archive:
        expected = [f"{PLUGIN_NAME}/{name}" for name in selected]
        if archive.namelist() != expected or archive.testzip() is not None:
            raise ReleaseCheckError("release ZIP content list or CRC differs from its source manifest")
        for name, content in source.items():
            if archive.read(f"{PLUGIN_NAME}/{name}") != content:
                raise ReleaseCheckError(f"release ZIP content differs from source: {name}")
    largest_name = max(selected, key=lambda name: len(source[name]))
    return {
        "files": len(selected), "uncompressed_bytes": total_bytes, "archive_bytes": archive_bytes,
        "largest_file": largest_name, "largest_file_bytes": len(source[largest_name]),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }
