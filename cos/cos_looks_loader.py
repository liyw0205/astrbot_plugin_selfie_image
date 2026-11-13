"""Load the JSON-backed COS catalog and reusable image vocabularies."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Tuple


COS_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "cos"
LOOKS_DIR = COS_DATA_DIR / "looks"


class CosLooksDataError(RuntimeError):
    """Raised when the JSON catalog cannot satisfy the image COS contract."""


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CosLooksDataError(f"无法读取 COS JSON: {path}") from exc
    if not isinstance(value, dict):
        raise CosLooksDataError(f"COS JSON 顶层必须是对象: {path}")
    return value


def _load_catalog() -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    index = _read_json(LOOKS_DIR / "index.json")
    records: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    for entry in index.get("files") or []:
        if not isinstance(entry, Mapping):
            continue
        filename = str(entry.get("file") or "").strip()
        if not filename or filename.startswith("_"):
            continue
        payload = _read_json(LOOKS_DIR / filename)
        for raw in payload.get("items") or []:
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            look_id = str(item.get("id") or "").strip()
            if not look_id:
                raise CosLooksDataError(f"COS 条目缺少 id: {filename}")
            if look_id in by_id:
                raise CosLooksDataError(f"COS 条目 id 重复: {look_id}")
            item["id"] = look_id
            item["title"] = str(item.get("title") or "").strip()
            item["cos_type"] = str(item.get("cos_type") or "").strip()
            item["prompt"] = str(item.get("prompt") or "").strip()
            # Drop retired per-look staging fields at the data boundary so old
            # custom files cannot reintroduce bespoke combinations.
            for key in (
                "video", "mode", "camera", "orientation", "angle",
                "allow_explicit_override", "pose_id", "scene_id", "framing_id",
                "pose_ids", "scene_ids", "style_ids", "expression_ids",
                "framing_ids", "makeup_ids", "compositions",
            ):
                item.pop(key, None)
            by_id[look_id] = item
            records.append(item)

    order = [str(value).strip() for value in index.get("order") or [] if str(value).strip()]
    if order:
        order_index = {look_id: position for position, look_id in enumerate(order)}
        records.sort(key=lambda item: order_index.get(str(item["id"]), len(order_index)))
    return records, by_id


def _load_vocab(filename: str) -> Dict[str, Dict[str, Any]]:
    payload = _read_json(COS_DATA_DIR / filename)
    if payload.get("schema_version") != 1:
        raise CosLooksDataError(f"COS 词库缺少受支持的 schema_version: {filename}")
    items = payload.get("items") or {}
    if not isinstance(items, Mapping):
        raise CosLooksDataError(f"COS 词库必须是对象: {filename}")
    return {
        str(key): dict(value)
        for key, value in items.items()
        if isinstance(value, Mapping)
    }


def _load_object(filename: str, key: str) -> Dict[str, Any]:
    value = _read_json(COS_DATA_DIR / filename).get(key)
    if not isinstance(value, Mapping):
        raise CosLooksDataError(f"COS JSON 配置必须是对象: {filename}/{key}")
    return dict(value)


def _load_terms(filename: str, key: str) -> Tuple[str, ...]:
    values = _read_json(COS_DATA_DIR / filename).get(key) or []
    if not isinstance(values, list):
        raise CosLooksDataError(f"COS JSON 请求词必须是数组: {filename}/{key}")
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _load_alias_map(filename: str, key: str) -> Dict[str, List[str]]:
    values = _read_json(COS_DATA_DIR / filename).get(key) or {}
    if not isinstance(values, Mapping):
        raise CosLooksDataError(f"COS JSON 请求别名必须是对象: {filename}/{key}")
    return {
        str(item_id): list(dict.fromkeys(str(alias).strip() for alias in aliases if str(alias).strip()))
        for item_id, aliases in values.items()
        if isinstance(aliases, list)
    }


def _load_legacy_groups() -> Dict[str, List[str]]:
    payload = _read_json(COS_DATA_DIR / "_legacy_groups.json")
    groups = payload.get("groups") or {}
    if not isinstance(groups, Mapping):
        raise CosLooksDataError("legacy COS 分组必须是对象")
    return {
        str(name): [str(value).strip() for value in values if str(value).strip()]
        for name, values in groups.items()
        if isinstance(values, list)
    }


COS_JSON_LOOK_SETS, COS_JSON_LOOKS_BY_ID = _load_catalog()
COS_JSON_STYLES = _load_vocab("_styles.json")
COS_JSON_EXPRESSIONS = _load_vocab("_expressions.json")
COS_JSON_POSES = _load_vocab("_poses.json")
COS_JSON_SCENES = _load_vocab("_scenes.json")
COS_JSON_FRAMINGS = _load_vocab("_framings.json")
COS_JSON_REAR_HAIR_REQUEST_TERMS = tuple(
    str(value).strip()
    for value in (COS_JSON_FRAMINGS.get("rear_hair_close", {}).get("request_aliases") or [])
    if str(value).strip()
)
COS_JSON_MAKEUPS = _load_vocab("_makeups.json")
COS_JSON_LIGHTINGS = _load_vocab("_lightings.json")
COS_JSON_LIGHTING_RULES = _load_object("_lightings.json", "rules")
COS_JSON_CONTACT_SHADOW_PROMPTS = _load_object("_lightings.json", "contact_shadow_prompts")
COS_JSON_LIGHTING_REQUEST_TERMS = _load_terms("_lightings.json", "request_terms")
COS_JSON_COMPOSITIONS = _load_vocab("_compositions.json")
COS_JSON_MULTI_VIEW_COLLAGE = _load_object("_compositions.json", "multi_view")
COS_JSON_COMBINATIONS = _load_vocab("_combinations.json")
COS_JSON_POSE_REQUEST_ALIASES = _load_alias_map("_poses.json", "request_aliases")
COS_JSON_POSE_CLASSIFICATION_TERMS = _load_alias_map("_poses.json", "classification_terms")
COS_JSON_POSE_REQUEST_SYNTAX_TERMS = _load_alias_map("_poses.json", "request_syntax_terms")
COS_JSON_POSE_CUSTOM_REQUEST_TERMS = _load_terms("_poses.json", "custom_request_terms")
COS_JSON_SCENE_REQUEST_TERMS = _load_terms("_scenes.json", "request_terms")
COS_JSON_SCENE_REQUEST_SYNTAX_TERMS = _load_alias_map("_scenes.json", "request_syntax_terms")
COS_JSON_LOOK_THEME_MARKERS = _load_alias_map("_scenes.json", "look_theme_markers")
COS_JSON_LOOK_THEME_CONFLICTS = _load_alias_map("_scenes.json", "look_theme_conflicts")
COS_JSON_FACE_RISK_PROMPTS = _read_json(COS_DATA_DIR / "_scenes.json").get("face_risk_prompts") or {}
COS_JSON_STYLE_REQUEST_TERMS = _load_terms("_styles.json", "request_terms")
COS_JSON_FRAMING_REQUEST_TERMS = _load_terms("_framings.json", "request_terms")
COS_JSON_FRAMING_OPTION_TERMS = _load_terms("_framings.json", "option_terms")
COS_JSON_FRAMING_OPTION_SUFFIX_TERMS = _load_terms("_framings.json", "option_suffix_terms")
COS_JSON_CAMERA_REQUEST_TERMS = _load_alias_map("_framings.json", "camera_request_terms")
COS_JSON_COMPOSITION_REQUEST_TERMS = _load_terms("_compositions.json", "request_terms")
COS_JSON_MULTI_VIEW_COLLAGE_TERMS = _load_terms("_compositions.json", "multi_view_request_terms")
COS_JSON_LEGACY_GROUPS = _load_legacy_groups()

for pose_id, aliases in COS_JSON_POSE_REQUEST_ALIASES.items():
    if pose_id in COS_JSON_POSES:
        COS_JSON_POSES[pose_id]["aliases"] = aliases


def get_json_cos_look(look_id: str) -> Dict[str, Any]:
    """Return a defensive copy of one JSON-backed outfit."""
    item = COS_JSON_LOOKS_BY_ID.get(str(look_id or "").strip())
    return dict(item) if item else {}


def get_json_legacy_group(group_name: str) -> List[Dict[str, Any]]:
    """Return defensive copies for one former Python-owned COS group."""
    name = str(group_name or "").strip()
    look_ids = COS_JSON_LEGACY_GROUPS.get(name)
    if look_ids is None:
        return []
    missing = [look_id for look_id in look_ids if look_id not in COS_JSON_LOOKS_BY_ID]
    if missing:
        raise CosLooksDataError(f"legacy COS 分组包含未知 id: {name}: {','.join(missing[:8])}")
    return [dict(COS_JSON_LOOKS_BY_ID[look_id]) for look_id in look_ids]


def validate_json_catalog() -> List[str]:
    """Return human-readable image COS catalog issues."""
    issues: List[str] = []
    for item in COS_JSON_LOOK_SETS:
        look_id = str(item.get("id") or "")
        if not item.get("title") or not item.get("cos_type") or not item.get("prompt"):
            issues.append(f"{look_id}: missing title/cos_type/prompt")
    for group_name, look_ids in COS_JSON_LEGACY_GROUPS.items():
        missing = [look_id for look_id in look_ids if look_id not in COS_JSON_LOOKS_BY_ID]
        if missing:
            issues.append(f"legacy group {group_name} missing {','.join(missing[:8])}")
    for name, vocab in (
        ("poses", COS_JSON_POSES),
        ("scenes", COS_JSON_SCENES),
        ("styles", COS_JSON_STYLES),
        ("expressions", COS_JSON_EXPRESSIONS),
        ("framings", COS_JSON_FRAMINGS),
        ("makeups", COS_JSON_MAKEUPS),
        ("lightings", COS_JSON_LIGHTINGS),
    ):
        for item_id, item in vocab.items():
            if not str(item.get("title") or item.get("label") or "").strip() or not str(item.get("prompt") or "").strip():
                issues.append(f"{name}/{item_id}: missing title/prompt")
    pose_ids = set(COS_JSON_POSES)
    scene_ids = set(COS_JSON_SCENES)
    known_framing_ids = set(COS_JSON_FRAMINGS)
    expression_ids = set(COS_JSON_EXPRESSIONS)
    makeup_ids = set(COS_JSON_MAKEUPS)
    camera_ids = {"selfie", "first", "third"}
    required_lighting_rules = {
        "source_consistency", "user_lighting", "user_face", "subject_clarity",
        "face_visible", "face_optional", "contact", "collage",
    }
    if set(COS_JSON_LIGHTING_RULES) != required_lighting_rules or any(
        not isinstance(value, str) or not value.strip()
        for value in COS_JSON_LIGHTING_RULES.values()
    ):
        issues.append("lighting rules are incomplete or invalid")
    if not COS_JSON_LIGHTING_REQUEST_TERMS:
        issues.append("lighting request terms must not be empty")
    if "default" not in COS_JSON_CONTACT_SHADOW_PROMPTS or any(
        not isinstance(value, str) or not value.strip()
        for value in COS_JSON_CONTACT_SHADOW_PROMPTS.values()
    ):
        issues.append("contact shadow prompts are incomplete or invalid")
    for lighting_id, lighting in COS_JSON_LIGHTINGS.items():
        aliases = lighting.get("aliases")
        if not isinstance(aliases, list) or not aliases or any(
            not isinstance(value, str) or not value.strip() for value in aliases
        ):
            issues.append(f"lighting {lighting_id}: missing valid aliases")
    for expression_id, expression in COS_JSON_EXPRESSIONS.items():
        if set(expression) != {"label", "prompt", "aliases"}:
            issues.append(f"expression {expression_id}: expected label/prompt/aliases")
        aliases = expression.get("aliases")
        if not isinstance(aliases, list) or not aliases or any(not isinstance(alias, str) or not alias.strip() for alias in aliases):
            issues.append(f"expression {expression_id}: aliases must be a non-empty string list")
    unknown_pose_aliases = set(COS_JSON_POSE_REQUEST_ALIASES) - pose_ids
    if unknown_pose_aliases:
        issues.append("pose request aliases reference unknown ids: " + ",".join(sorted(unknown_pose_aliases)[:8]))
    missing_pose_aliases = pose_ids - set(COS_JSON_POSE_REQUEST_ALIASES)
    if missing_pose_aliases:
        issues.append("poses missing request aliases: " + ",".join(sorted(missing_pose_aliases)[:8]))
    required_pose_term_groups = {
        "edge", "bend", "back", "side", "orientation_back", "orientation_side",
        "orientation_front", "orientation_strip",
    }
    if set(COS_JSON_POSE_CLASSIFICATION_TERMS) != required_pose_term_groups:
        issues.append("pose classification term groups do not match the schema")
    if set(COS_JSON_POSE_REQUEST_SYNTAX_TERMS) != {"labels", "assignments"}:
        issues.append("pose request syntax term groups do not match the schema")
    elif not all(COS_JSON_POSE_REQUEST_SYNTAX_TERMS.values()):
        issues.append("pose request syntax terms must not be empty")
    if set(COS_JSON_SCENE_REQUEST_SYNTAX_TERMS) != {
        "labels", "assignments", "freeform_leads", "freeform_locations",
    }:
        issues.append("scene request syntax term groups do not match the schema")
    elif not all(COS_JSON_SCENE_REQUEST_SYNTAX_TERMS.values()):
        issues.append("scene request syntax terms must not be empty")
    if not COS_JSON_SCENE_REQUEST_TERMS or not COS_JSON_STYLE_REQUEST_TERMS:
        issues.append("scene/style request terms must not be empty")
    if not COS_JSON_FRAMING_REQUEST_TERMS or not COS_JSON_FRAMING_OPTION_TERMS:
        issues.append("framing request/option terms must not be empty")
    if not COS_JSON_FRAMING_OPTION_SUFFIX_TERMS:
        issues.append("framing option suffix terms must not be empty")
    if set(COS_JSON_CAMERA_REQUEST_TERMS) != {"selfie", "mirror_selfie", "first", "third"}:
        issues.append("camera request term groups do not match the schema")
    elif (
        not all(COS_JSON_CAMERA_REQUEST_TERMS.values())
        or not set(COS_JSON_CAMERA_REQUEST_TERMS["mirror_selfie"]) <= set(COS_JSON_CAMERA_REQUEST_TERMS["selfie"])
    ):
        issues.append("camera request terms must be non-empty and mirror selfie terms must be selfie aliases")
    if not COS_JSON_MULTI_VIEW_COLLAGE_TERMS or not COS_JSON_POSE_CUSTOM_REQUEST_TERMS:
        issues.append("multi-view and custom pose request terms must not be empty")
    collage_id = str(COS_JSON_MULTI_VIEW_COLLAGE.get("id") or "").strip()
    collage_count = COS_JSON_MULTI_VIEW_COLLAGE.get("panel_count")
    collage_labels = COS_JSON_MULTI_VIEW_COLLAGE.get("panel_labels")
    if (
        not collage_id
        or not COS_JSON_MULTI_VIEW_COLLAGE.get("title")
        or not COS_JSON_MULTI_VIEW_COLLAGE.get("prompt")
        or not isinstance(collage_count, int)
        or collage_count < 1
        or not isinstance(collage_labels, list)
        or len(collage_labels) != collage_count
        or any(not str(label).strip() for label in collage_labels)
    ):
        issues.append("multi-view collage metadata is incomplete or inconsistent")
    if set(COS_JSON_LOOK_THEME_CONFLICTS) - set(COS_JSON_LOOK_THEME_MARKERS):
        issues.append("scene look theme conflicts reference unknown themes")
    if not isinstance(COS_JSON_FACE_RISK_PROMPTS, Mapping) or any(
        not str(prompt).strip() for prompt in COS_JSON_FACE_RISK_PROMPTS.values()
    ):
        issues.append("scene face risk prompts must map tags to non-empty prompts")
    for scene_id, scene in COS_JSON_SCENES.items():
        unknown_risks = [value for value in scene.get("face_risk_tags") or [] if value not in COS_JSON_FACE_RISK_PROMPTS]
        if unknown_risks:
            issues.append(f"scene {scene_id}: face risk prompts missing {','.join(unknown_risks[:8])}")
        missing = [value for value in scene.get("pose_ids") or [] if value not in pose_ids]
        if missing:
            issues.append(f"scene {scene_id}: poses missing {','.join(missing[:8])}")
        style_ids = scene.get("style_ids") or []
        missing_styles = [value for value in style_ids if value not in COS_JSON_STYLES]
        if missing_styles:
            issues.append(f"scene {scene_id}: styles missing {','.join(missing_styles[:8])}")
        scene_makeups = scene.get("makeup_ids") or []
        missing_makeups = [value for value in scene_makeups if value not in makeup_ids]
        if missing_makeups:
            issues.append(f"scene {scene_id}: makeups missing {','.join(missing_makeups[:8])}")
        if not scene.get("prompt") or not scene.get("pose_ids") or not style_ids or not scene_makeups:
            issues.append(f"scene {scene_id}: missing prompt/pose/style/makeup options")
        lighting_ids = scene.get("lighting_ids")
        if not isinstance(lighting_ids, list) or not lighting_ids or any(
            not isinstance(value, str) or value not in COS_JSON_LIGHTINGS for value in lighting_ids
        ):
            issues.append(f"scene {scene_id}: missing valid lighting_ids")
        style_weights = scene.get("style_weights") or {}
        if not isinstance(style_weights, Mapping) or any(
            key not in style_ids or not isinstance(value, int) or isinstance(value, bool) or value < 1
            for key, value in style_weights.items()
        ):
            issues.append(f"scene {scene_id}: invalid style_weights")
    for pose_id, pose in COS_JSON_POSES.items():
        shadow_id = pose.get("contact_shadow_id", "default")
        if not isinstance(shadow_id, str) or shadow_id not in COS_JSON_CONTACT_SHADOW_PROMPTS:
            issues.append(f"pose {pose_id}: unknown contact_shadow_id")
        missing = [value for value in pose.get("incompatible_expression_ids") or [] if value not in COS_JSON_EXPRESSIONS]
        if missing:
            issues.append(f"pose {pose_id}: expressions missing {','.join(missing[:8])}")
        pose_expressions = pose.get("expression_ids") or []
        missing_expressions = [value for value in pose_expressions if value not in expression_ids]
        if missing_expressions:
            issues.append(f"pose {pose_id}: compatible expressions missing {','.join(missing_expressions[:8])}")
        if set(pose_expressions) & set(pose.get("incompatible_expression_ids") or []):
            issues.append(f"pose {pose_id}: expression_ids include incompatible expressions")
        for field in ("camera_ids", "random_camera_ids"):
            invalid = [value for value in pose.get(field) or [] if value not in camera_ids]
            if invalid:
                issues.append(f"pose {pose_id}: invalid {field} {','.join(invalid[:8])}")
        if not pose_expressions or not pose.get("random_camera_ids"):
            issues.append(f"pose {pose_id}: missing expression/camera options")
        pose_framings = pose.get("framing_ids") or []
        if not isinstance(pose_framings, list):
            issues.append(f"pose {pose_id}: framing_ids must be a list")
            continue
        missing_framings = [value for value in pose_framings if value not in known_framing_ids]
        if missing_framings:
            issues.append(f"pose {pose_id}: framings missing {','.join(missing_framings[:8])}")
        if not pose_framings and not pose.get("explicit_only"):
            issues.append(f"pose {pose_id}: no compatible framings")
    for framing_id, framing in COS_JSON_FRAMINGS.items():
        invalid_list = False
        for field in ("random_camera_ids", "camera_ids", "scene_ids"):
            if field in framing and (
                not isinstance(framing[field], list)
                or any(not isinstance(value, str) or not value.strip() for value in framing[field])
            ):
                issues.append(f"framing {framing_id}: {field} must be a list")
                invalid_list = True
        if invalid_list:
            continue
        if framing.get("camera_ids") and not set(framing.get("random_camera_ids") or []) <= set(framing["camera_ids"]):
            issues.append(f"framing {framing_id}: random cameras must be allowed by camera_ids")
        missing_scenes = [value for value in framing.get("scene_ids") or [] if value not in scene_ids]
        if missing_scenes:
            issues.append(f"framing {framing_id}: scenes missing {','.join(missing_scenes[:8])}")
        for field in ("camera_ids", "random_camera_ids"):
            invalid = [value for value in framing.get(field) or [] if value not in camera_ids]
            if invalid:
                issues.append(f"framing {framing_id}: invalid {field} {','.join(invalid[:8])}")
        if "random_camera_ids" not in framing:
            issues.append(f"framing {framing_id}: missing random_camera_ids")
        aliases = framing.get("request_aliases")
        if aliases is not None and (
            not isinstance(aliases, list)
            or any(not isinstance(value, str) or not value.strip() for value in aliases)
        ):
            issues.append(f"framing {framing_id}: request_aliases must contain non-empty strings")
    if not COS_JSON_REAR_HAIR_REQUEST_TERMS:
        issues.append("rear_hair_close: request_aliases must not be empty")
    for composition_id, composition in COS_JSON_COMPOSITIONS.items():
        missing_scenes = [value for value in composition.get("scenes") or [] if value not in scene_ids]
        missing_poses = [value for value in composition.get("poses") or [] if value not in pose_ids]
        invalid_cameras = [value for value in composition.get("cameras") or [] if value not in camera_ids]
        if missing_scenes:
            issues.append(f"composition {composition_id}: scenes missing {','.join(missing_scenes[:8])}")
        if missing_poses:
            issues.append(f"composition {composition_id}: poses missing {','.join(missing_poses[:8])}")
        if invalid_cameras:
            issues.append(f"composition {composition_id}: cameras invalid {','.join(invalid_cameras[:8])}")
    for combination_id, combination in COS_JSON_COMBINATIONS.items():
        scene_id = str(combination.get("scene_id") or "")
        pose_id = str(combination.get("pose_id") or "")
        scene = COS_JSON_SCENES.get(scene_id) or {}
        if scene_id not in scene_ids:
            issues.append(f"combination {combination_id}: scene missing {scene_id}")
        if pose_id not in pose_ids:
            issues.append(f"combination {combination_id}: pose missing {pose_id}")
        elif pose_id not in (scene.get("pose_ids") or []):
            issues.append(f"combination {combination_id}: pose {pose_id} incompatible with scene {scene_id}")
    return issues


_JSON_CATALOG_ISSUES = validate_json_catalog()
if _JSON_CATALOG_ISSUES:
    raise CosLooksDataError("COS JSON catalog 校验失败: " + "; ".join(_JSON_CATALOG_ISSUES[:8]))
