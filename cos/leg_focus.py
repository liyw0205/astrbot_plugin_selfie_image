"""JSON-backed pose and scene selection for ``看看腿``."""

from __future__ import annotations

import random
import re
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..core.prompt_data import PromptDataError, read_nonempty_list, read_versioned_json

LEGWEAR_CATEGORIES = frozenset({
    "bare_legs", "sheer", "opaque", "fishnet", "knee_high", "ankle_sock", "thigh_high",
})
LEGWEAR_RISKS = frozenset({"normal", "high"})
LEGFOCUS_POSTURES = frozenset({
    "seated", "reclined", "lying", "side_lying", "prone", "kneeling", "standing",
})


DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "legs" / "compositions.json"
OUTFIT_DATA_PATH = DATA_PATH.with_name("outfits.json")
VIEW_DATA_PATH = DATA_PATH.with_name("views.json")
COMBO_DATA_PATH = DATA_PATH.with_name("combo_presets.json")


class LegFocusDataError(RuntimeError):
    """Raised when the leg-focus prompt pool is incomplete or incompatible."""


def _load_pool() -> Dict[str, Any]:
    try:
        payload = read_versioned_json(DATA_PATH, "看看腿提示词池")
        view_payload = read_versioned_json(VIEW_DATA_PATH, "看看腿机位池")
        scenes = read_nonempty_list(payload, "scenes", "看看腿场景池")
        poses = read_nonempty_list(payload, "poses", "看看腿姿势池")
        views = read_nonempty_list(view_payload, "views", "看看腿机位池")
        capture_styles = read_nonempty_list(payload, "capture_styles", "看看腿拍摄质感池")
        scene_states = read_nonempty_list(payload, "scene_states", "看看腿场景状态池")
    except PromptDataError as exc:
        raise LegFocusDataError(str(exc)) from exc
    scene_ids: set[str] = set()
    support_types: set[str] = set()
    for index, raw in enumerate(scenes):
        if not isinstance(raw, Mapping):
            raise LegFocusDataError(f"看看腿场景第 {index + 1} 项必须是对象")
        scene_id = str(raw.get("id") or "").strip()
        environment = raw.get("environment", "indoor")
        if not scene_id or scene_id in scene_ids:
            raise LegFocusDataError(f"看看腿场景 id 缺失或重复: {scene_id or index + 1}")
        if not isinstance(environment, str) or environment not in {"indoor", "outdoor"}:
            raise LegFocusDataError(f"看看腿场景环境无效: {scene_id}")
        if not str(raw.get("title") or "").strip() or not str(raw.get("prompt") or "").strip():
            raise LegFocusDataError(f"看看腿场景缺少标题或提示词: {scene_id}")
        cameras = raw.get("cameras", ["selfie", "third"])
        if (
            not isinstance(cameras, list) or not cameras
            or any(camera not in ("selfie", "third") for camera in cameras)
            or len(set(cameras)) != len(cameras)
        ):
            raise LegFocusDataError(f"看看腿场景的相机人称限制无效: {scene_id}")
        capture_modes = raw.get("capture_modes", ["direct"])
        if (
            not isinstance(capture_modes, list) or not capture_modes
            or any(mode not in {"direct", "mirror"} for mode in capture_modes)
            or len(set(capture_modes)) != len(capture_modes)
        ):
            raise LegFocusDataError(f"看看腿场景的拍摄方式无效: {scene_id}")
        supports = raw.get("supports")
        if not isinstance(supports, Mapping) or not supports or any(
            not isinstance(key, str) or not key or not isinstance(value, str) or not value.strip()
            for key, value in supports.items()
        ):
            raise LegFocusDataError(f"看看腿场景缺少有效支撑条件: {scene_id}")
        support_types.update(supports)
        scene_ids.add(scene_id)

    views_by_id: Dict[str, Any] = {}
    for raw in views:
        if not isinstance(raw, Mapping):
            raise LegFocusDataError("看看腿机位必须是对象")
        view_id = str(raw.get("id") or "").strip()
        supports = raw.get("support_types")
        framings = raw.get("framings")
        camera = raw.get("camera")
        capture_mode = raw.get("capture_mode", "direct")
        if (
            not re.fullmatch(r"[a-z_]+", view_id) or view_id in views_by_id
            or not str(raw.get("title") or "").strip() or not str(raw.get("prompt") or "").strip()
            or type(raw.get("weight")) is not int or raw["weight"] < 1
            or camera not in {"selfie", "third"}
            or capture_mode not in {"direct", "mirror"}
            or (capture_mode == "mirror" and camera != "selfie")
            or raw.get("direction") not in ({"front", "left", "right"} if camera == "selfie" else {"front", "oblique", "side"})
            or raw.get("elevation") not in ({"down", "level"} if camera == "selfie" else {"down", "up", "level", "top"})
            or not isinstance(supports, list) or not supports or not set(supports) <= support_types
            or not isinstance(framings, list) or not framings
            or not set(framings) <= ({"leg_detail"} if camera == "selfie" else {"leg_detail", "full_outfit"})
        ):
            raise LegFocusDataError(f"看看腿机位字段或兼容条件无效: {view_id}")
        angle = raw.get("angle")
        if (
            not isinstance(angle, Mapping)
            or type(angle.get("yaw")) not in (int, float)
            or not -180 <= angle["yaw"] <= 180
            or type(angle.get("pitch")) not in (int, float)
            or not -90 <= angle["pitch"] <= 90
            or angle.get("pitch_kind") not in {"down", "up", "level"}
            or type(angle.get("roll")) not in (int, float)
            or not -90 <= angle["roll"] <= 90
            or angle.get("camera_height") not in {
                "above_shoulder", "above_waist", "upper_body", "eye_level", "below_waist",
            }
            or angle.get("horizontal_position") not in {
                "front", "front_left", "front_right", "front_oblique", "left", "right", "side",
            }
            or any(
                not isinstance(angle.get(key), list)
                or len(angle[key]) != 2
                or any(type(value) not in (int, float) for value in angle[key])
                or angle[key][0] > angle[key][1]
                for key in ("yaw_range", "pitch_range")
            )
        ):
            raise LegFocusDataError(f"看看腿机位角度 angle 字段无效: {view_id}")
        selection_weight = raw.get("selection_weight", 1)
        if type(selection_weight) is not int or selection_weight < 1:
            raise LegFocusDataError(f"看看腿机位稀缺补偿权重无效: {view_id}/selection_weight")
        views_by_id[view_id] = raw

    pose_ids: set[str] = set()
    legacy_ids: set[str] = set()
    for index, raw in enumerate(poses):
        if not isinstance(raw, Mapping):
            raise LegFocusDataError(f"看看腿姿势第 {index + 1} 项必须是对象")
        pose_id = str(raw.get("id") or "").strip()
        if not pose_id or pose_id in pose_ids:
            raise LegFocusDataError(f"看看腿姿势 id 缺失或重复: {pose_id or index + 1}")
        camera_views = raw.get("camera_views")
        supports = raw.get("support_types")
        aliases = raw.get("legacy_ids")
        posture = raw.get("posture")
        pose_scenes = raw.get("scene_ids", [])
        disallowed_hosiery_kinds = raw.get("disallowed_hosiery_kinds", [])
        if (
            not str(raw.get("title") or "").strip()
            or not str(raw.get("prompt") or "").strip()
            or posture not in {"seated", "reclined", "lying", "side_lying", "prone", "kneeling", "standing"}
            or type(raw.get("weight")) is not int
            or int(raw.get("weight") or 0) < 1
            or not isinstance(raw.get("risk", "normal"), str)
            or raw.get("risk", "normal") not in {"normal", "high"}
            or not isinstance(camera_views, Mapping) or not camera_views
            or not set(camera_views) <= {"selfie", "third"}
            or raw.get("camera_preference", "") not in {"", "selfie", "third"}
            or any(
                not isinstance(refs, list) or not refs
                or any(not isinstance(ref, str) or not ref for ref in refs)
                or len(set(refs)) != len(refs)
                or not set(refs) <= views_by_id.keys()
                for refs in camera_views.values()
            )
            or not isinstance(supports, list) or not supports or not set(supports) <= support_types
            or not isinstance(pose_scenes, list) or not set(pose_scenes) <= scene_ids
            or not isinstance(aliases, list) or any(not isinstance(value, str) or not value for value in aliases)
            or not isinstance(disallowed_hosiery_kinds, list)
            or not set(disallowed_hosiery_kinds) <= {"tights", "thigh_high", "stirrup", "ankle"}
            or bool(set(aliases) & legacy_ids)
        ):
            raise LegFocusDataError(f"看看腿姿势字段不完整或引用无效: {pose_id}")
        for camera, view_refs in camera_views.items():
            for view_id in view_refs:
                if (
                    views_by_id[view_id]["camera"] != camera
                    or not set(supports) & set(views_by_id[view_id]["support_types"])
                ):
                    raise LegFocusDataError(f"看看腿姿势与机位没有兼容支撑: {pose_id}/{view_id}")
        crops = raw.get("crop_prompts")
        crop_keys = set(camera_views)
        capture_modes = {
            views_by_id[view_id].get("capture_mode", "direct")
            for refs in camera_views.values() for view_id in refs
        }
        if "mirror" in capture_modes:
            crop_keys.add("mirror")
        if not isinstance(crops, Mapping) or set(crops) != crop_keys or any(
            not isinstance(value, str) or not value.strip() for value in crops.values()
        ):
            raise LegFocusDataError(f"看看腿姿势缺少对应人称的裁切描述: {pose_id}/crop_prompts")
        hand_prompts = raw.get("hand_prompts", {})
        if not isinstance(hand_prompts, Mapping) or (hand_prompts and (
            set(hand_prompts) != capture_modes
            or any(not isinstance(value, str) or not value.strip() for value in hand_prompts.values())
        )):
            raise LegFocusDataError(f"看看腿姿势手部动作与拍摄方式不匹配: {pose_id}/hand_prompts")
        detail_variants = raw.get("detail_variants", [])
        detail_ids: set[str] = set()
        if not isinstance(detail_variants, list):
            raise LegFocusDataError(f"看看腿姿势细节变体必须是数组: {pose_id}/detail_variants")
        for variant in detail_variants:
            if not isinstance(variant, Mapping):
                raise LegFocusDataError(f"看看腿姿势细节变体必须是对象: {pose_id}/detail_variants")
            detail_id = str(variant.get("id") or "").strip()
            detail_cameras = variant.get("cameras", ["selfie", "third"])
            detail_modes = variant.get("capture_modes", ["direct", "mirror"])
            if (
                not re.fullmatch(r"[a-z_]+", detail_id) or detail_id in detail_ids
                or not str(variant.get("prompt") or "").strip()
                or type(variant.get("weight")) is not int or variant["weight"] < 1
                or not isinstance(variant.get("risk", "normal"), str)
                or variant.get("risk", "normal") not in {"normal", "high"}
                or not isinstance(detail_cameras, list) or not detail_cameras
                or any(camera not in {"selfie", "third"} for camera in detail_cameras)
                or len(set(detail_cameras)) != len(detail_cameras)
                or not isinstance(detail_modes, list) or not detail_modes
                or any(mode not in {"direct", "mirror"} for mode in detail_modes)
                or len(set(detail_modes)) != len(detail_modes)
            ):
                raise LegFocusDataError(f"看看腿姿势细节变体字段无效: {pose_id}/{detail_id or 'missing'}")
            detail_ids.add(detail_id)
        legacy_ids.update(aliases)
        pose_ids.add(pose_id)
    if legacy_ids & pose_ids:
        raise LegFocusDataError("看看腿旧姿势 id 与当前 id 冲突")

    for key in ("framing", "anatomy_rule", "hosiery_rule", "full_framing", "scene_rule"):
        if not str(payload.get(key) or "").strip():
            raise LegFocusDataError(f"看看腿提示词池缺少字段: {key}")
    if type(payload.get("proportion_guard", False)) is not bool:
        raise LegFocusDataError("看看腿 proportion_guard 必须是布尔值")
    if len(str(payload["anatomy_rule"])) > 60:
        raise LegFocusDataError("看看腿结构约束 anatomy_rule 不得超过 60 字")
    crop_modes = payload.get("crop_modes")
    if not isinstance(crop_modes, Mapping) or set(crop_modes) != {
        "leg_full", "shoulder_crop", "half_face", "full_face",
    } or any(not isinstance(value, str) or not value.strip() for value in crop_modes.values()):
        raise LegFocusDataError("看看腿 crop_modes 字段不完整或无效")
    for key, required in (
        ("camera_framings", {"selfie", "third", "mirror"}),
        ("reference_rules", {"zh", "en"}),
    ):
        values = payload.get(key)
        if not isinstance(values, Mapping) or set(values) != required or any(
            not isinstance(value, str) or not value.strip() for value in values.values()
        ):
            raise LegFocusDataError(f"看看腿提示词池字段不完整或无效: {key}")
    appearance_prompts = payload.get("appearance_prompts")
    if (
        not isinstance(appearance_prompts, Mapping)
        or set(appearance_prompts) != {"auto", "real", "anime"}
        or any(
            not isinstance(values, Mapping) or set(values) != {"zh", "en"}
            or any(not isinstance(value, str) or not value.strip() for value in values.values())
            for values in appearance_prompts.values()
        )
    ):
        raise LegFocusDataError("看看腿提示词池字段不完整或无效: appearance_prompts")
    scene_state_ids: set[str] = set()
    state_probability = payload.get("scene_state_probability", 0.3)
    if type(state_probability) not in (int, float) or not 0 <= state_probability <= 1:
        raise LegFocusDataError("看看腿场景状态触发概率无效: scene_state_probability")
    for index, state in enumerate(scene_states):
        if not isinstance(state, Mapping):
            raise LegFocusDataError(f"看看腿场景状态第 {index + 1} 项必须是对象")
        state_id = str(state.get("id") or "").strip()
        environments = state.get("environments")
        state_scene_ids = state.get("scene_ids")
        if (
            not re.fullmatch(r"[a-z_]+", state_id) or state_id in scene_state_ids
            or not str(state.get("title") or "").strip()
            or not str(state.get("prompt") or "").strip()
            or type(state.get("weight")) is not int or state["weight"] < 1
            or not isinstance(environments, list) or not environments
            or any(not isinstance(value, str) or value not in {"indoor", "outdoor"} for value in environments)
            or len(set(environments)) != len(environments)
            or not isinstance(state_scene_ids, list) or not state_scene_ids
            or any(not isinstance(scene_id, str) or not scene_id for scene_id in state_scene_ids)
            or ("*" in state_scene_ids and state_scene_ids != ["*"])
            or ("*" not in state_scene_ids and not set(state_scene_ids) <= scene_ids)
            or len(set(state_scene_ids)) != len(state_scene_ids)
        ):
            raise LegFocusDataError(f"看看腿场景状态字段无效: {state_id or index + 1}")
        scene_state_ids.add(state_id)

    capture_style_ids: set[str] = set()
    for index, style in enumerate(capture_styles):
        if not isinstance(style, Mapping):
            raise LegFocusDataError(f"看看腿拍摄质感第 {index + 1} 项必须是对象")
        style_id = str(style.get("id") or "").strip()
        modes = style.get("modes")
        style_scene_ids = style.get("scene_ids")
        environments = style.get("environments", ["indoor"])
        state_ids = style.get("state_ids", [])
        if (
            not re.fullmatch(r"[a-z_]+", style_id) or style_id in capture_style_ids
            or not str(style.get("title") or "").strip()
            or not str(style.get("prompt") or "").strip()
            or type(style.get("weight")) is not int or style["weight"] < 1
            or not isinstance(modes, list) or not modes
            or any(not isinstance(mode, str) or mode not in {"selfie", "third", "mirror"} for mode in modes)
            or len(set(modes)) != len(modes)
            or not isinstance(environments, list) or not environments
            or any(not isinstance(value, str) or value not in {"indoor", "outdoor"} for value in environments)
            or len(set(environments)) != len(environments)
            or not isinstance(style_scene_ids, list) or not style_scene_ids
            or any(not isinstance(scene_id, str) or not scene_id for scene_id in style_scene_ids)
            or ("*" in style_scene_ids and style_scene_ids != ["*"])
            or ("*" not in style_scene_ids and not set(style_scene_ids) <= scene_ids)
            or len(set(style_scene_ids)) != len(style_scene_ids)
            or not isinstance(state_ids, list)
            or any(not isinstance(state_id, str) or state_id not in scene_state_ids for state_id in state_ids)
            or len(set(state_ids)) != len(state_ids)
        ):
            raise LegFocusDataError(f"看看腿拍摄质感字段无效: {style_id or index + 1}")
        for state_id in state_ids:
            state = next(item for item in scene_states if item["id"] == state_id)
            style_scenes = scene_ids if "*" in style_scene_ids else set(style_scene_ids)
            state_scenes = scene_ids if "*" in state["scene_ids"] else set(state["scene_ids"])
            if not (set(environments) & set(state["environments"])) or not (style_scenes & state_scenes):
                raise LegFocusDataError(f"看看腿拍摄质感与场景状态不兼容: {style_id}/{state_id}")
        capture_style_ids.add(style_id)
    outdoor_scenes = [scene for scene in scenes if scene.get("environment", "indoor") == "outdoor"]
    for scene in outdoor_scenes:
        scene_id = str(scene["id"])
        states = [
            state for state in scene_states
            if "outdoor" in state["environments"]
            and ("*" in state["scene_ids"] or scene_id in state["scene_ids"])
        ]
        if not states:
            raise LegFocusDataError(f"室外场景缺少兼容状态: {scene_id}")
        camera_modes = {
            "mirror" if mode == "mirror" else camera
            for camera in scene.get("cameras", ["selfie", "third"])
            for mode in scene.get("capture_modes", ["direct"])
        }
        for state in states:
            for camera_mode in camera_modes:
                if not any(
                    "outdoor" in style.get("environments", ["indoor"])
                    and camera_mode in style["modes"]
                    and ("*" in style["scene_ids"] or scene_id in style["scene_ids"])
                    and (not style.get("state_ids") or state["id"] in style["state_ids"])
                    for style in capture_styles
                ):
                    raise LegFocusDataError(
                        f"室外场景缺少兼容拍摄质感: {scene_id}/{state['id']}/{camera_mode}"
                    )
    return {
        **payload,
        "views": views,
        "capture_styles": capture_styles,
        "scene_states": scene_states,
        "scene_state_probability": float(state_probability),
    }


def _load_outfit_pool() -> Dict[str, Any]:
    try:
        payload = read_versioned_json(OUTFIT_DATA_PATH, "看看腿服饰池")
        for key in ("outfits", "random_leg_details", "hosiery_styles", "leg_accessories"):
            records = read_nonempty_list(payload, key, "看看腿服饰池")
            seen: set[str] = set()
            for raw in records:
                if not isinstance(raw, Mapping):
                    raise LegFocusDataError(f"看看腿服饰池 {key} 必须是对象数组")
                item_id = str(raw.get("id") or "").strip()
                if (
                    not item_id or item_id in seen
                    or not str(raw.get("title") or "").strip()
                    or not str(raw.get("prompt") or "").strip()
                    or type(raw.get("weight")) is not int or raw["weight"] < 1
                    or type(raw.get("explicit_only", False)) is not bool
                ):
                    raise LegFocusDataError(f"看看腿服饰池字段不完整或重复: {key}/{item_id}")
                if raw.get("risk", "normal") not in LEGWEAR_RISKS:
                    raise LegFocusDataError(f"看看腿服饰池风险级别无效: {key}/{item_id}")
                category = raw.get("category")
                if key == "outfits":
                    if (
                        not isinstance(category, list) or not category
                        or not set(category) <= LEGWEAR_CATEGORIES
                    ):
                        raise LegFocusDataError(f"看看腿服饰类别无效: {key}/{item_id}")
                    compatible_postures = raw.get("compatible_postures")
                    if compatible_postures is not None and (
                        not isinstance(compatible_postures, list) or not compatible_postures
                        or any(posture not in LEGFOCUS_POSTURES for posture in compatible_postures)
                        or len(set(compatible_postures)) != len(compatible_postures)
                    ):
                        raise LegFocusDataError(f"看看腿服饰姿势兼容条件无效: {item_id}/compatible_postures")
                elif key != "leg_accessories" and category not in LEGWEAR_CATEGORIES:
                    raise LegFocusDataError(f"看看腿服饰类别无效: {key}/{item_id}")
                if key == "random_leg_details" and (
                    type(raw.get("category_weight", 1)) is not int
                    or raw.get("category_weight", 1) < 1
                ):
                    raise LegFocusDataError(f"看看腿服饰类别权重无效: {key}/{item_id}")
                seen.add(item_id)
    except PromptDataError as exc:
        raise LegFocusDataError(str(exc)) from exc
    for key, required in (
        ("preserve_prompts", {"custom", "reference", "custom_detail", "reference_detail", "requested_detail"}),
        ("wear_prompts", {"日常裤装", "用户指定", "光腿", "光腿神器", "黑丝", "白丝", "赤足", "连裤袜"}),
        ("wear_labels", {"日常裤装", "用户指定", "光腿", "光腿神器", "黑丝", "白丝", "赤足", "连裤袜"}),
    ):
        values = payload.get(key)
        if not isinstance(values, Mapping) or not all(str(values.get(name) or "").strip() for name in required):
            raise LegFocusDataError(f"看看腿服饰池缺少字段: {key}")
    pool_reference_prompts = payload.get("pool_reference_prompts")
    if not isinstance(pool_reference_prompts, Mapping) or any(
        not str(pool_reference_prompts.get(language) or "").strip() for language in ("zh", "en")
    ):
        raise LegFocusDataError("看看腿服饰池随机换装参考规则缺少中英文提示词")
    for detail in payload["random_leg_details"]:
        if detail.get("wear") not in {"黑丝", "白丝", "光腿", "赤足"}:
            raise LegFocusDataError(f"看看腿腿部细节无效: {detail['id']}")
    for style in payload["hosiery_styles"]:
        if (
            style.get("wear") not in {"黑丝", "白丝"}
            or not str(style.get("family") or "").strip()
            or style.get("kind") not in {"tights", "thigh_high", "stirrup", "ankle", "knee_high", "fishnet"}
            or type(style.get("explicit_only", False)) is not bool
            or not isinstance(style.get("aliases"), list)
            or not style["aliases"]
            or any(not isinstance(alias, str) or not alias.strip() for alias in style["aliases"])
        ):
            raise LegFocusDataError(f"看看腿袜装款式无效或需显式请求: {style['id']}")
    families = {style["family"] for style in payload["hosiery_styles"]}
    capture_profiles = payload.get("capture_profiles")
    if not isinstance(capture_profiles, Mapping) or not capture_profiles:
        raise LegFocusDataError("看看腿服饰池缺少有效 capture_profiles")
    for profile_id, profile in capture_profiles.items():
        if not isinstance(profile, Mapping) or not str(profile.get("coverage_prompt") or "").strip():
            raise LegFocusDataError(f"看看腿服饰拍摄规则无效: {profile_id}")
        for key, allowed_values in (
            ("third_directions", {"front", "oblique", "side"}),
            ("third_elevations", {"up", "down", "level", "top"}),
        ):
            values = profile.get(key)
            if (
                not isinstance(values, list) or not values
                or any(not isinstance(value, str) or value not in allowed_values for value in values)
                or len(set(values)) != len(values)
            ):
                raise LegFocusDataError(f"看看腿服饰拍摄规则无效: {profile_id}/{key}")
    for outfit in payload["outfits"]:
        if not isinstance(outfit.get("capture_profile"), str) or outfit["capture_profile"] not in capture_profiles:
            raise LegFocusDataError(f"看看腿服饰缺少有效拍摄规则: {outfit['id']}/capture_profile")
        environments = outfit.get("environments", ["indoor", "outdoor"])
        if (
            not isinstance(environments, list) or not environments
            or any(not isinstance(environment, str) or environment not in {"indoor", "outdoor"} for environment in environments)
            or len(set(environments)) != len(environments)
        ):
            raise LegFocusDataError(f"看看腿服饰适用环境无效: {outfit['id']}/environments")
        allowed = outfit.get("hosiery_families")
        if not isinstance(allowed, list) or not allowed or not set(allowed) <= families:
            raise LegFocusDataError(f"看看腿服饰兼容袜装无效: {outfit['id']}")
        for wear in ("黑丝", "白丝"):
            if not any(
                style["family"] in allowed and style["wear"] == wear and style["kind"] == "tights"
                for style in payload["hosiery_styles"]
            ):
                raise LegFocusDataError(f"看看腿服饰缺少兼容连裤袜: {outfit['id']}/{wear}")
    accessory_prompt_warnings = []
    for accessory in payload["leg_accessories"]:
        accessory.setdefault("bulk", "thin")
        for key in (
            "compatible_wear", "compatible_hosiery_families", "compatible_hosiery_kinds",
            "incompatible_outfit_ids", "blocked_by_terms", "postures", "aliases",
        ):
            if not isinstance(accessory.get(key), list) or any(not isinstance(value, str) for value in accessory[key]):
                raise LegFocusDataError(f"看看腿饰品兼容字段无效: {accessory['id']}/{key}")
        if not accessory["compatible_wear"] or not accessory["postures"]:
            raise LegFocusDataError(f"看看腿饰品兼容范围不能为空: {accessory['id']}")
        if any(wear not in {"黑丝", "白丝", "连裤袜", "光腿", "赤足"} for wear in accessory["compatible_wear"]):
            raise LegFocusDataError(f"看看腿饰品兼容服饰无效: {accessory['id']}")
        if (
            type(accessory.get("stackable", True)) is not bool
            or not isinstance(accessory.get("exclusive_group", ""), str)
            or not isinstance(accessory.get("accessory_type", ""), str)
            or accessory.get("bulk", "thin") not in {"thin", "medium", "heavy"}
        ):
            raise LegFocusDataError(f"看看腿饰品叠加规则无效: {accessory['id']}")
        prompt = str(accessory["prompt"])
        required_groups = (
            ("贴合", "紧贴"),
            ("窄", "纤细", "薄"),
            ("同色系", "同系", "低反光", "肤色", "袜色"),
            ("不勒", "不压迫", "不留痕", "不凸起"),
        )
        missing = [
            "/".join(group) for group in required_groups
            if not any(term in prompt for term in group)
        ]
        if missing:
            accessory_prompt_warnings.append(f"{accessory['id']} ({','.join(missing)})")
    if accessory_prompt_warnings:
        logging.getLogger(__name__).warning(
            "leg accessory source prompts rely on the generated constraint prefix: %s",
            "; ".join(accessory_prompt_warnings),
        )
    return payload


_POOL = _load_pool()
_OUTFITS = _load_outfit_pool()
LEGFOCUS_SCENE_POOL: List[Dict[str, Any]] = [dict(item) for item in _POOL["scenes"]]
LEGFOCUS_SCENE_BY_ID = {str(item["id"]): item for item in LEGFOCUS_SCENE_POOL}
LEGFOCUS_POSE_POOL: List[Dict[str, Any]] = [dict(item) for item in _POOL["poses"]]
LEGFOCUS_POSE_BY_ID = {str(item["id"]): item for item in LEGFOCUS_POSE_POOL}
LEGFOCUS_VIEW_POOL: List[Dict[str, Any]] = [dict(item) for item in _POOL["views"]]
LEGFOCUS_VIEW_BY_ID = {str(item["id"]): item for item in LEGFOCUS_VIEW_POOL}
LEGFOCUS_RANDOM_LEG_DETAILS: List[Dict[str, Any]] = [
    dict(item) for item in _OUTFITS["random_leg_details"]
]
LEGFOCUS_FRAMING = str(_POOL["framing"])
LEGFOCUS_ANATOMY_RULE = str(_POOL["anatomy_rule"])
LEGFOCUS_HOSIERY_RULE = str(_POOL["hosiery_rule"])
LEGFOCUS_CAMERA_FRAMINGS = dict(_POOL["camera_framings"])
LEGFOCUS_FULL_FRAMING = str(_POOL["full_framing"])
LEGFOCUS_CROP_MODES = {
    str(mode_id): str(prompt)
    for mode_id, prompt in _POOL["crop_modes"].items()
}
LEGFOCUS_REFERENCE_RULES = dict(_POOL["reference_rules"])
LEGFOCUS_SCENE_RULE = str(_POOL["scene_rule"])
LEGFOCUS_APPEARANCE_PROMPTS = _POOL["appearance_prompts"]
LEGFOCUS_CAPTURE_STYLE_POOL: List[Dict[str, Any]] = [
    dict(item) for item in _POOL["capture_styles"]
]
LEGFOCUS_SCENE_STATE_POOL: List[Dict[str, Any]] = [
    dict(item) for item in _POOL["scene_states"]
]
LEGFOCUS_SCENE_STATE_PROBABILITY = float(_POOL.get("scene_state_probability", 0.3))
LEGFOCUS_PRESERVE_PROMPTS = dict(_OUTFITS["preserve_prompts"])
LEGFOCUS_POOL_REFERENCE_PROMPTS = dict(_OUTFITS["pool_reference_prompts"])
LEGFOCUS_WEAR_PROMPTS = dict(_OUTFITS["wear_prompts"])
LEGFOCUS_WEAR_LABELS = dict(_OUTFITS["wear_labels"])
LEGFOCUS_HOSIERY_STYLES: List[Dict[str, Any]] = [dict(item) for item in _OUTFITS["hosiery_styles"]]
LEGFOCUS_ACCESSORIES: List[Dict[str, Any]] = [dict(item) for item in _OUTFITS["leg_accessories"]]
LEGFOCUS_ACCESSORY_BY_ID = {str(item["id"]): item for item in LEGFOCUS_ACCESSORIES}
LEGFOCUS_OUTFIT_CAPTURE_PROFILES = dict(_OUTFITS["capture_profiles"])


LEGFOCUS_POSE_IDS = frozenset(LEGFOCUS_POSE_BY_ID)
LEGFOCUS_EXPLICIT_ONLY_POSE_IDS = frozenset()
LEGFOCUS_SEATED_POSE_IDS = frozenset(
    item["id"] for item in LEGFOCUS_POSE_POOL if item.get("posture") == "seated"
)
LEGFOCUS_SCENE_TERMS = tuple(
    dict.fromkeys(
        str(term).strip()
        for scene in LEGFOCUS_SCENE_POOL
        for term in (scene.get("title"), *(scene.get("aliases") or []))
        if str(term or "").strip()
    )
)

# Persisted action tags remain readable while resolving to the current indoor pool.
LEGFOCUS_POSE_ALIASES = {
    "sit": "sofa_ankle_cross",
    "sit_crop": "sofa_ankle_cross",
    "kneel": "kneeling_sit_back",
    "kneel_crop": "kneeling_sit_back",
    "side_lie": "side_lying_offset_legs",
    "side_lie_crop": "side_lying_offset_legs",
    "hug_knee": "bed_one_knee_up",
    "hug_knee_crop": "bed_one_knee_up",
    "cross_leg": "chair_crossed_knee",
    "cross_leg_crop": "chair_crossed_knee",
    "windowsill": "gaming_chair_staggered",
    "windowsill_crop": "gaming_chair_staggered",
    "kneel_up": "kneeling_upright",
    "kneel_front": "kneeling_sit_back",
    "floor_fold": "rug_back_support",
    "one_knee_fix": "bed_one_knee_up",
    "reclined_knees_crop": "bed_side_recline",
    "floor_knees_up_crop": "rug_side_fold",
    "desk_sit_crop": "chair_crossed_knee",
    "bed_supine_crop": "supine_offset_legs",
    "sofa_front_crop": "seated_knees_together",
    "chair_side_crop": "chair_crossed_knee",
    "sofa_cross_crop": "sofa_side_fold",
    "floor_knees_crop": "rug_side_fold",
    "sofa_occlusion_crop": "sofa_forward_leg",
    "stool_edge_crop": "bed_edge_diagonal",
    "floor_side_kneel_crop": "kneeling_sit_back",
    "seat_knees_cross_crop": "sofa_ankle_cross",
    "floor_topdown_cross_crop": "rug_back_support",
    "first_person_topdown_legs_crop": "bed_edge_diagonal",
    "staggered_stair_step_crop": "bed_one_knee_up",
    "cross_step_snapshot_crop": "gaming_chair_staggered",
    "turn_and_settle_crop": "chair_crossed_knee",
    "bench_side_sit_crop": "gaming_chair_staggered",
    "toe_touch_step_crop": "bed_one_knee_up",
    "back_staggered_step_crop": "sofa_side_fold",
    "knees_forward_sit_crop": "edge_parallel_forward",
    "one_knee_bent_sit_crop": "bed_one_knee_up",
    "side_folded_sit_crop": "sofa_side_fold",
    "reclined_bent_knees_crop": "bed_side_recline",
    "one_leg_extended_sit_crop": "sofa_forward_leg",
    "heels_folded_sit_crop": "rug_side_fold",
    "edge_dangle_sit_crop": "edge_knees_together_dangle",
    "crossed_ankles_sit_crop": "sofa_ankle_cross",
    "crossed_knee_sit_crop": "chair_crossed_knee",
    "over_leg_sit_crop": "chair_crossed_knee",
    "supported_legs_forward_crop": "rug_back_support",
    "relaxed_knees_apart_sit_crop": "sofa_ankle_cross",
}
_DATA_POSE_ALIASES = {
    legacy_id: str(pose["id"])
    for pose in LEGFOCUS_POSE_POOL for legacy_id in pose["legacy_ids"]
}
LEGFOCUS_POSE_ALIASES = {
    alias: _DATA_POSE_ALIASES.get(pose_id, pose_id)
    for alias, pose_id in LEGFOCUS_POSE_ALIASES.items()
    if _DATA_POSE_ALIASES.get(pose_id, pose_id) in LEGFOCUS_POSE_BY_ID
}
LEGFOCUS_POSE_ALIASES.update(_DATA_POSE_ALIASES)

LEGFOCUS_CAMERA_WEIGHTS: Dict[str, Tuple[Tuple[str, int], ...]] = {
    str(item["id"]): tuple(
        (camera, 1) for camera in item["camera_views"]
    )
    for item in LEGFOCUS_POSE_POOL
}
for old_id, current_id in LEGFOCUS_POSE_ALIASES.items():
    LEGFOCUS_CAMERA_WEIGHTS[old_id] = LEGFOCUS_CAMERA_WEIGHTS[current_id]

LEGWEAR_PROMPTS = {
    str(key): str(value)
    for key, value in LEGFOCUS_WEAR_PROMPTS.items()
    if str(key).strip() and str(value).strip()
}
SAFE_LEGWEAR_LABELS = {
    str(key): str(value)
    for key, value in LEGFOCUS_WEAR_LABELS.items()
    if str(key).strip() and str(value).strip()
}
_DETAIL_TAGS = {str(item["id"]): str(item["wear"]) for item in LEGFOCUS_RANDOM_LEG_DETAILS}
_DETAIL_WEIGHTS = [
    _DETAIL_TAGS[str(item["id"])]
    for item in LEGFOCUS_RANDOM_LEG_DETAILS
    for _ in range(int(item["weight"]))
    if str(item["id"]) in _DETAIL_TAGS
]
LEGWEAR_BY_POSE = {
    pose_id: tuple((tag, sum(1 for value in _DETAIL_WEIGHTS if value == tag)) for tag in dict.fromkeys(_DETAIL_WEIGHTS))
    for pose_id in LEGFOCUS_POSE_IDS
}
for old_id, current_id in LEGFOCUS_POSE_ALIASES.items():
    LEGWEAR_BY_POSE[old_id] = LEGWEAR_BY_POSE[current_id]

LEGFOCUS_OUTFIT_POOL: List[Dict[str, Any]] = [dict(item) for item in _OUTFITS["outfits"]]
LEGFOCUS_OUTFIT_BY_ID = {str(item["id"]): item for item in LEGFOCUS_OUTFIT_POOL}
LEGFOCUS_RISKY_EXTRA_REPLACEMENTS = (
    ("掀衣摆", "整理衣摆"),
)

LEGWEAR_REQUEST_PATTERN = re.compile(
    r"(?:光腿神器|光腿|白丝|黑丝|丝袜|短袜|堆堆袜|过膝袜|长筒袜|踩脚袜|肉色丝袜|肉丝|连裤袜|长袜|赤足|裸足|光脚)"
)
LEGWEAR_STYLE_TERMS = tuple(
    dict.fromkeys(
        str(term).strip()
        for style in LEGFOCUS_HOSIERY_STYLES
        for term in (style["title"], *(style.get("aliases") or []))
        if str(term or "").strip()
    )
)
LEGWEAR_NEGATION_PATTERN = re.compile(
    r"(?:不穿|不要|不需要|无需|不必|不加|不搭配|不用|不带|没有|没穿)\s*(?:任何)?\s*"
    r"(?:袜子|袜装|袜|鞋履|鞋子|鞋|靴子|靴)"
    r"(?:\s*(?:和|与|或|、|及)\s*(?:袜子|袜装|袜|鞋履|鞋子|鞋|靴子|靴))*"
)
LEG_NEGATION_PREFIX = r"(?:不要|别|避免|禁止|不得|不需要|不想要|无需|不必|不穿|不加|不搭配|不用|不带|没有|没穿)"
LEG_DETAIL_REQUEST_PATTERN = re.compile(
    r"(?:下半身特写|腿部特写|下装特写|局部特写|腰部以下|只拍腿|只拍下装|不拍上半身|上半身不入镜|不露脸|不露头|近距离拍腿)"
)
LEG_FACE_REQUEST_PATTERN = re.compile(
    r"(?:完整面部|完整露脸|正脸入镜|半脸|侧脸|露脸|露头|带脸|看脸|面部入镜|表情|看向镜头|微笑|wink|眨眼)",
    re.I,
)
LEG_FULL_OUTFIT_REQUEST_PATTERN = re.compile(
    r"(?:全身(?!镜)|从头到脚|头部到鞋履|full[- ]?body)", re.I
)
LEGFOCUS_OUTFIT_REQUEST_PATTERN = re.compile(
    r"(?:裙|百褶|A字|裤|鞋|靴|平底|乐福|玛丽珍|帆布鞋|凉鞋|JK|洛丽塔|水手服|套装|T恤|衬衫|针织|卫衣|背心)", re.I
)
LEGFOCUS_COMPLETE_OUTFIT_REQUEST_PATTERN = re.compile(
    r"(?:身穿|穿着|换成|换上|改穿|连体(?:衣|服)|连衣裙|COS服|cosplay服装|制服|和服|旗袍|女仆装|礼服|参考图.{0,8}(?:服装|衣服|穿搭))",
    re.I,
)
LEG_FACE_ACTIONS: Dict[str, str] = {}


def leg_focus_appearance_prompt(appearance_type: str, language: str = "zh", existing_text: str = "") -> str:
    prompts = LEGFOCUS_APPEARANCE_PROMPTS.get(appearance_type, LEGFOCUS_APPEARANCE_PROMPTS["auto"])
    prompt = prompts["en" if language == "en" else "zh"]
    return "" if prompt in existing_text else prompt


def parse_requested_leg_face(text: str) -> bool:
    """Only explicit face requests opt out of the default faceless crop."""
    raw = str(text or "")
    if re.search(r"(?:不露脸|不露头|不拍脸|只拍腿|只拍下装|上半身不入镜)", raw):
        return False
    return any(
        not re.search(r"(?:不要|不需要|无需|不必|不想|别|不|避免|禁止)(?:再|去|用|使用)*$", raw[max(0, match.start() - 12):match.start()])
        for match in LEG_FACE_REQUEST_PATTERN.finditer(raw)
    )


def has_leg_face_context(text: str) -> bool:
    return "【legs_face:context】" in str(text or "")


def is_leg_calf_crop_action(text: str) -> bool:
    raw = str(text or "")
    frame = re.search(r"【frame:(full_outfit|leg_detail)】", raw)
    if frame:
        return frame.group(1) == "leg_detail"
    if LEG_DETAIL_REQUEST_PATTERN.search(raw):
        return True
    pose = re.search(r"【pose:([a-z_]+)】", raw)
    return bool(pose)


CALF_CROP_POSES = frozenset(
    set(LEGFOCUS_POSE_IDS) | set(LEGFOCUS_POSE_ALIASES) | {"sit", "sit_crop"}
)


def _has_positive_term(text: str, term: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    start = 0
    while True:
        index = compact.find(term, start)
        if index < 0:
            return False
        prefix = compact[max(0, index - 12):index]
        if not re.search(
            r"(?:不要|不在|不去|不选|不需要|不想要|无需|不必|不穿|不加|不搭配|不用|不带|没有|没穿|避免|禁止|别)"
            r"(?:再|去|在|用|穿|选择|采用|使用)*$",
            prefix,
        ):
            return True
        start = index + len(term)


def _has_negative_term(text: str, term: str) -> bool:
    """Return whether a term is explicitly negated in the user request."""
    compact = re.sub(r"\s+", "", str(text or ""))
    needle = re.sub(r"\s+", "", str(term or ""))
    if not needle:
        return False
    for match in re.finditer(re.escape(needle), compact, re.I):
        prefix = compact[max(0, match.start() - 12):match.start()]
        if re.search(LEG_NEGATION_PREFIX + r"(?:再|去|在|用|穿|选择|采用|使用)*$", prefix, re.I):
            return True
    return False


def _constraint_kinds(text: str, categories: Dict[str, tuple[str, ...]]) -> Tuple[set[str], set[str]]:
    """Classify longest phrases once, so '非自拍' does not also mean '自拍'."""
    raw = re.sub(r"\s+", "", str(text or "")).lower()
    terms = {term.lower(): kind for kind, aliases in categories.items() for term in aliases}
    pattern = "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
    positive: set[str] = set()
    negative: set[str] = set()
    for match in re.finditer(pattern, raw):
        prefix = raw[max(0, match.start() - 12):match.start()]
        negated = re.search(LEG_NEGATION_PREFIX + r"(?:再|去|在|用|穿|选择|采用|使用)*$", prefix)
        target = negative if negated else positive
        target.add(terms[match.group()])
    return positive, negative


def _preserve_unhandled_legwear(text: str) -> str:
    """Keep unsupported sock requests intact instead of reducing them to a color."""
    raw = str(text or "")
    supported = [term for term in LEGWEAR_STYLE_TERMS if term in raw]
    for term in sorted(supported, key=len, reverse=True):
        raw = raw.replace(term, " ")
    unknown = [
        match.group(0)
        for match in re.finditer(r"(?:[黑白粉灰蓝红紫肤肉乳][色白]?|纯色|条纹|蕾丝)?(?:短袜|堆堆袜|长筒袜|过膝袜|踩脚袜|长袜)", raw)
        if _has_positive_term(raw, match.group(0))
    ]
    return "、".join(dict.fromkeys(unknown))


def parse_requested_leg_scene(text: str) -> str:
    raw = re.sub(r"场景不变|背景不变|保持场景|保持背景", "", str(text or ""))
    matches: List[Tuple[int, str]] = []
    for scene in LEGFOCUS_SCENE_POOL:
        terms = (str(scene["id"]), str(scene["title"]), *(scene.get("aliases") or []))
        matching_lengths = [
            len(str(term)) for term in terms
            if str(term).strip() and _has_positive_term(raw, str(term))
        ]
        if matching_lengths:
            matches.append((max(matching_lengths), str(scene["id"])))
    if not matches:
        return ""
    longest = max(length for length, _ in matches)
    return " ".join(dict.fromkeys(scene_id for length, scene_id in matches if length == longest))


def parse_excluded_leg_scenes(text: str) -> set[str]:
    raw = str(text or "")
    excluded = set()
    for scene in LEGFOCUS_SCENE_POOL:
        terms = (str(scene["id"]), str(scene["title"]), *(scene.get("aliases") or []))
        if any(_has_negative_term(raw, term) for term in terms if str(term).strip()):
            excluded.add(str(scene["id"]))
    return excluded


def _resolve_pose_id(pose_id: str) -> str:
    value = str(pose_id or "").strip()
    if value in LEGFOCUS_POSE_BY_ID:
        return value
    return LEGFOCUS_POSE_ALIASES.get(value, "")


def _ranges_overlap(first: Any, second: Any) -> bool:
    return first[0] <= second[1] and second[0] <= first[1]


def _view_matches_angle(view: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    angle = view.get("angle") or {}
    for key in ("yaw_range", "pitch_range"):
        requested = filters.get(key)
        if key == "yaw_range" and filters.get("yaw_ranges"):
            view_range = angle.get(key, (angle.get("yaw"),) * 2)
            if not any(_ranges_overlap(view_range, candidate) for candidate in filters["yaw_ranges"]):
                return False
            continue
        if requested and not _ranges_overlap(angle.get(key, (angle.get(key[:-6]),) * 2), requested):
            return False
    requested_height = filters.get("camera_height")
    if not requested_height:
        return True
    height_aliases = {
        "above_shoulder": {"above_shoulder", "above_waist", "upper_body"},
        "above_waist": {"above_waist", "above_shoulder", "upper_body"},
    }
    return angle.get("camera_height") in height_aliases.get(
        requested_height, {requested_height},
    )


def _view_angle_prompt(view: Mapping[str, Any]) -> str:
    angle = view["angle"]
    positions = {
        "front": "人物正前方",
        "front_left": "人物左前方",
        "front_right": "人物右前方",
        "front_oblique": "人物斜前方",
        "left": "人物左侧",
        "right": "人物右侧",
        "side": "人物侧方",
    }
    heights = {
        "above_shoulder": "高于人物肩线",
        "above_waist": "人物腰胯以上",
        "upper_body": "人物上身附近",
        "eye_level": "人物视线高度",
        "below_waist": "人物腰胯以下",
    }
    pitch = angle["pitch"]
    yaw = angle["yaw"]
    horizontal = f"水平偏转约{yaw:+g}度" if yaw else "水平正向取景"
    height = heights[angle["camera_height"]]
    position = positions[angle["horizontal_position"]]
    roll = float(angle["roll"])
    roll_line = f"画面横滚角{roll:g}度，边线保持水平" if roll == 0 else f"画面横滚约{roll:g}度"
    if view["camera"] == "third":
        if pitch > 0:
            vertical = (
                f"机位位于{height}，镜头向下俯视约{pitch:g}度，明确斜俯取景；"
                "俯视角度高于膝部，不采用平视角度。"
            )
        elif pitch < 0:
            vertical = f"机位位于{height}，镜头向上轻仰约{abs(pitch):g}度取景。"
        else:
            vertical = "机位与人物肩线同高，镜头保持水平，俯仰角0度，正平视取景。"
        return (
            f"第三人称画外相机位于{position}，偏航角{yaw:+g}度（{horizontal}）；{vertical}"
            f"{roll_line}；普通手机焦段、竖幅近距离拍摄，不用超广角。"
        )
    if pitch > 0:
        vertical = f"镜头向下倾斜约{pitch}度取景"
    elif pitch < 0:
        vertical = f"镜头向上倾斜约{abs(pitch)}度取景"
    else:
        vertical = "镜头保持水平取景"
    return (
        f"第一人称自拍机位由人物手持手机拍摄，手臂自然抬起并维持手机与镜头的拍摄关系；"
        f"机位位于{position}，偏航角{yaw:+g}度（{horizontal}）；{height}，{vertical}；"
        f"俯仰角{pitch:+g}度，{roll_line}。"
    )


def _pose_support_constraints(
    pose: Mapping[str, Any], support_type: str,
) -> List[str]:
    posture = str(pose.get("posture") or "")
    if posture == "seated":
        seat = "座沿" if support_type in {"seat_edge", "raised_edge", "wide_edge"} else "座面"
        anchor = f"臀部与骨盆稳定接触本张姿势支撑中的{seat}，臀部不悬空"
        load = "重心落在臀部与承托面的接触区域，双腿重量由髋部沿膝踝方向传向已选落点"
    elif posture == "reclined":
        anchor = "上背与骨盆分别接触本张姿势支撑中的靠背和承托面，身体重量落在连续支撑区域"
        load = "重心由上背、骨盆和大腿与支撑面接触的一侧承接，双腿沿支撑方向自然延伸"
    elif posture in {"lying", "side_lying", "prone"}:
        if posture == "side_lying":
            anchor = "下侧躯干、骨盆及下侧腿贴合本张姿势支撑中的软面，上侧腿按姿势获得明确承托"
            load = "重心落向下侧躯干与骨盆接触的支撑面一侧，上侧腿重量由其接触点承接"
        elif posture == "prone":
            anchor = "腹侧躯干、骨盆和大腿前侧贴合本张姿势支撑中的软面"
            load = "重心落在腹侧躯干与骨盆接触的支撑面一侧，双腿由髋部向后顺势延伸"
        else:
            anchor = "背侧躯干、骨盆和大腿后侧贴合本张姿势支撑中的软面"
            load = "重心落在背侧躯干与骨盆接触的支撑面一侧，双腿沿承托面自然放置"
    elif posture == "kneeling":
        if support_type == "flat_ground":
            anchor = "左膝与右脚掌直接落在同一块平整硬质地面上，无垫子、无台阶、无高差"
            load = "重心稳定落在左膝与右脚掌形成的支撑区域，髋部不悬空，手部只作自然辅助支撑"
        else:
            anchor = "双膝或姿势指定的承重点落在同一连续支撑面上，不混用不同高度的软垫与地面"
            load = "重心落在本张姿势指定的膝部、脚掌或臀部接触区域，腿部沿支撑方向传递重量"
    else:
        return []
    joints = (
        "左腿从髋部连续连接到跪地膝和向后的小腿，右腿从髋部连续连接到弯曲膝和前踩脚掌；"
        "左右关节朝向清楚，腿段不翻折"
        if posture == "kneeling" and support_type == "flat_ground"
        else "左右腿各自保持同侧髋、膝、踝连续衔接，关节朝向和腿段层次遵循已选姿势"
    )
    return [f"承托位置硬约束：{anchor}。", f"关节链硬约束：{joints}。", f"重力方向硬约束：{load}。"]


def _pose_requires_shoulder_crop(pose: Mapping[str, Any], framing_key: str) -> bool:
    text = " ".join((
        str(pose.get("prompt") or ""),
        str((pose.get("crop_prompts") or {}).get(framing_key) or ""),
    ))
    return bool(re.search(
        r"(?:頭部|头部|头脸|面部).{0,10}(?:画外|不入镜|画面外)|"
        r"(?:上缘|画面上缘).{0,10}(?:肩线|肩部)|不露脸|只拍肩以下|肩线以下裁切",
        text,
    ))


def _clean_leg_view_prompt(prompt: str, view: Mapping[str, Any], crop_mode: str) -> str:
    value = str(prompt or "")
    angle = view.get("angle") or {}
    pitch = float(angle.get("pitch") or 0)
    clauses = re.split(r"(?<=[。；])", value)
    if pitch > 0:
        clauses = [clause for clause in clauses if not re.search(
            r"(?:平视|平拍|与膝部?[^。；]{0,6}等高|与肩线同高|同高平视|正面平拍)", clause,
        )]
    elif pitch < 0:
        clauses = [clause for clause in clauses if not re.search(
            r"(?:向下倾斜|俯视取景|俯拍取景|俯拍|俯视|平视取景|平拍|正面平拍)", clause,
        )]
    if str(angle.get("camera_height") or "").startswith("above_"):
        clauses = [clause for clause in clauses if not re.search(
            r"(?:全身入镜|头部到足部完整入镜|从头到脚完整入镜)", clause,
        )]
    if crop_mode == "shoulder_crop":
        clauses = [clause for clause in clauses if not re.search(
            r"(?:头脸可自然入画|头部与面部可自然入画|允许头脸入画|允许面部入画|面部自然入镜|自然侧脸入镜|完整面部清晰进入画面)",
            clause,
        )]
    value = re.sub(r"\s+", " ", "".join(clauses)).strip(" ；")
    conflicts = []
    if pitch > 0 and re.search(r"(?:平视|平拍|与膝.{0,6}等高|与肩线同高|正面平拍)", value):
        conflicts.append("down-angle/level")
    if pitch < 0 and re.search(r"(?:俯视|俯拍|平视|平拍|正面平拍)", value):
        conflicts.append("up-angle/down-angle")
    if str(angle.get("camera_height") or "").startswith("above_") and re.search(
        r"(?:全身入镜|头部到足部完整入镜|从头到脚完整入镜)", value,
    ):
        conflicts.append("high-camera/full-body")
    if crop_mode == "shoulder_crop" and re.search(
        r"(?:头脸可自然入画|头部与面部可自然入画|允许头脸入画|允许面部入画|面部自然入镜|自然侧脸入镜|完整面部清晰进入画面)",
        value,
    ):
        conflicts.append("shoulder-crop/face")
    if conflicts:
        logging.getLogger(__name__).warning(
            "leg view prompt conflict view=%s conflicts=%s prompt=%r",
            view.get("id"), ",".join(conflicts), value[:240],
        )
    return value


def _remove_selected_accessory_blocks(prompt: str, accessories: List[Dict[str, Any]]) -> str:
    value = str(prompt or "")
    selected_terms = tuple(
        str(term) for item in accessories for term in item.get("blocked_by_terms", ())
    )
    if not selected_terms:
        return value
    pattern = re.compile(r"(?:不加|不添加|不增加|不出现|不要|禁止|不需要|不带)[^，,。；]*[，,]?")
    value = pattern.sub(
        lambda match: "" if any(term in match.group(0) for term in selected_terms) else match.group(0),
        value,
    )
    return re.sub(r"\s+", " ", value)


def compatible_leg_compositions(
    pose: Dict[str, Any], *, camera: str = "", scene_ids: str = "",
    view_id: str = "", framing: str = "leg_detail", angles: Dict[str, Any] | None = None,
    allow_explicit: bool = False,
) -> List[Tuple[Dict[str, Any], str, Dict[str, Any]]]:
    if allow_explicit:
        compatible = compatible_leg_compositions(
            pose,
            camera=camera,
            scene_ids=scene_ids,
            view_id=view_id,
            framing=framing,
            angles=angles,
        )
        if compatible:
            return compatible
    requested_scenes = set(scene_ids.split())
    filters = angles or {}
    combinations = []
    for scene in LEGFOCUS_SCENE_POOL:
        if requested_scenes and scene["id"] not in requested_scenes:
            continue
        if pose.get("scene_ids") and scene["id"] not in pose["scene_ids"]:
            continue
        if scene["id"] in filters.get("excluded_scenes", ()):
            continue
        # Selection uses the first compatible support for a given scene/view.
        selected_views: set[str] = set()
        for support in pose["support_types"]:
            if support not in scene["supports"]:
                continue
            if allow_explicit:
                candidate_views = [
                    view for view in LEGFOCUS_VIEW_POOL
                    if (not view_id or view["id"] == view_id)
                    and (not camera or view["camera"] == camera)
                ]
            else:
                candidate_views = [
                    LEGFOCUS_VIEW_BY_ID[candidate_id]
                    for camera_kind, camera_view_ids in pose["camera_views"].items()
                    if not camera or camera_kind == camera
                    for candidate_id in camera_view_ids
                ]
            for view in candidate_views:
                candidate_id = view["id"]
                if (
                    candidate_id in selected_views
                    or (view_id and candidate_id != view_id)
                    or (camera and view["camera"] != camera)
                    or view["camera"] not in scene.get("cameras", ("selfie", "third"))
                    or view.get("capture_mode", "direct") not in scene.get("capture_modes", ("direct",))
                    or framing not in view["framings"]
                    or (
                        framing == "full_outfit"
                        and view["angle"].get("camera_height") in {"above_shoulder", "above_waist", "upper_body"}
                    )
                    or (not allow_explicit and support not in view["support_types"])
                    or (filters.get("direction") and view["direction"] != filters["direction"])
                    or (filters.get("elevations") and view["elevation"] not in filters["elevations"])
                    or not _view_matches_angle(view, filters)
                    or view["direction"] in filters.get("excluded_directions", ())
                    or view["elevation"] in filters.get("excluded_elevations", ())
                    or view["camera"] in filters.get("excluded_cameras", ())
                ):
                    continue
                combinations.append((scene, support, view))
                selected_views.add(candidate_id)
    return combinations


def _load_combo_presets() -> List[Dict[str, Any]]:
    try:
        payload = read_versioned_json(COMBO_DATA_PATH, "看看腿组合预设池")
        records = read_nonempty_list(payload, "combos", "看看腿组合预设池")
    except PromptDataError as exc:
        raise LegFocusDataError(str(exc)) from exc
    seen: set[str] = set()
    result: List[Dict[str, Any]] = []
    for raw in records:
        if not isinstance(raw, Mapping):
            raise LegFocusDataError("看看腿组合预设必须是对象")
        item = dict(raw)
        combo_id = str(item.get("id") or "").strip()
        pose = LEGFOCUS_POSE_BY_ID.get(str(item.get("pose") or ""))
        view = LEGFOCUS_VIEW_BY_ID.get(str(item.get("view") or ""))
        accessory_ids = item.get("accessory_ids")
        angle = item.get("angle")
        if (
            not re.fullmatch(r"combo_[0-9]{2}", combo_id) or combo_id in seen
            or not str(item.get("title") or "").strip()
            or not isinstance(item.get("aliases"), list)
            or any(not isinstance(alias, str) or not alias.strip() for alias in item["aliases"])
            or not pose or not view or view["camera"] != "third"
            or item.get("crop_mode") not in LEGFOCUS_CROP_MODES
            or item.get("face_mode") not in {"none", "half", "full"}
            or item.get("category") not in LEGWEAR_CATEGORIES
            or item.get("wear") not in {"黑丝", "白丝", "光腿", "赤足"}
            or not isinstance(accessory_ids, list)
            or any(accessory_id not in LEGFOCUS_ACCESSORY_BY_ID for accessory_id in accessory_ids)
            or len(accessory_ids) > 2
            or len(set(accessory_ids)) != len(accessory_ids)
            or item.get("support") not in pose.get("support_types", ())
            or type(item.get("weight")) is not int or item["weight"] < 1
            or item.get("risk") not in {"low", "high"}
            or not isinstance(angle, Mapping)
            or type(angle.get("yaw")) not in (int, float)
            or type(angle.get("pitch")) not in (int, float)
        ):
            raise LegFocusDataError(f"看看腿组合预设字段无效: {combo_id or 'missing'}")
        if item["category"] not in {detail["category"] for detail in LEGFOCUS_RANDOM_LEG_DETAILS}:
            raise LegFocusDataError(f"看看腿组合预设服饰类别无效: {combo_id}")
        if item["wear"] not in LEGWEAR_PROMPTS:
            raise LegFocusDataError(f"看看腿组合预设袜装无效: {combo_id}")
        if item["face_mode"] == "none" and item["crop_mode"] in {"half_face", "full_face"}:
            raise LegFocusDataError(f"看看腿组合预设裁切与露脸设置冲突: {combo_id}")
        if item["face_mode"] == "half" and item["crop_mode"] == "full_face":
            raise LegFocusDataError(f"看看腿组合预设裁切与露脸设置冲突: {combo_id}")
        if item["face_mode"] == "full" and item["crop_mode"] == "half_face":
            raise LegFocusDataError(f"看看腿组合预设裁切与露脸设置冲突: {combo_id}")
        if item["crop_mode"] == "shoulder_crop" and item["face_mode"] != "none":
            raise LegFocusDataError(f"看看腿肩线裁切组合必须不露脸: {combo_id}")
        view_angle = view["angle"]
        if (
            not view_angle["yaw_range"][0] <= angle["yaw"] <= view_angle["yaw_range"][1]
            or not view_angle["pitch_range"][0] <= angle["pitch"] <= view_angle["pitch_range"][1]
        ):
            raise LegFocusDataError(f"看看腿组合预设角度不兼容: {combo_id}")
        framing = "full_outfit" if item["face_mode"] in {"half", "full"} else "leg_detail"
        compatible = compatible_leg_compositions(
            pose,
            camera="third",
            view_id=view["id"],
            framing=framing,
        )
        if not any(support == item["support"] for _, support, _ in compatible):
            raise LegFocusDataError(f"看看腿组合预设没有兼容支撑: {combo_id}")
        if any(
            item["wear"] not in accessory["compatible_wear"]
            or (accessory.get("postures") and pose["posture"] not in accessory["postures"])
            for accessory in (LEGFOCUS_ACCESSORY_BY_ID[aid] for aid in accessory_ids)
        ):
            raise LegFocusDataError(f"看看腿组合预设饰品与动作不兼容: {combo_id}")
        seen.add(combo_id)
        result.append(item)
    return result


LEGFOCUS_COMBO_POOL: List[Dict[str, Any]] = _load_combo_presets()
LEGFOCUS_COMBO_BY_ID = {str(item["id"]): item for item in LEGFOCUS_COMBO_POOL}


def parse_requested_leg_combo(text: str) -> str:
    raw = str(text or "")
    match = re.search(r"\b(combo_[0-9]{2})\b", raw, re.I)
    if match and match.group(1).lower() in LEGFOCUS_COMBO_BY_ID:
        return match.group(1).lower()
    matches = [
        (len(alias), str(combo["id"]))
        for combo in LEGFOCUS_COMBO_POOL
        for alias in (combo["title"], *combo["aliases"])
        if _has_positive_term(raw, str(alias))
    ]
    return max(matches, key=lambda item: item[0])[1] if matches else ""


def _matching_leg_combos(
    text: str,
    *,
    allow_face_combo: bool = False,
    force_pose: str = "",
    force_view: str = "",
    force_camera: str = "",
    force_framing: str = "",
    force_crop_mode: str = "",
    force_face_mode: str = "",
    force_legwear: str = "",
    force_legwear_category: str = "",
    preferred_scene: str = "",
    named_combo: bool = False,
) -> List[Dict[str, Any]]:
    requested_pose = _resolve_pose_id(force_pose) or _resolve_pose_id(parse_requested_leg_pose(text))
    requested_view = force_view or parse_requested_leg_view(text)
    requested_camera = force_camera if force_camera in {"selfie", "third"} else parse_requested_leg_camera(text)
    requested_crop = force_crop_mode or parse_requested_leg_crop_mode(text)
    requested_full_frame = force_framing == "full_outfit" or bool(
        LEG_FULL_OUTFIT_REQUEST_PATTERN.search(str(text or ""))
        and not re.search(
            r"(?:不|不要|无需|不需要)\s*(?:全身(?!镜)|从头到脚)", str(text or "")
        )
    )
    requested_face = str(force_face_mode or "").strip().lower()
    if not requested_face:
        face_tag = re.search(r"【face_mode:(none|half|full)】", str(text or ""), re.I)
        requested_face = face_tag.group(1).lower() if face_tag else ""
    if not requested_face and LEG_DETAIL_REQUEST_PATTERN.search(text):
        requested_face = "none"
    requested_wear = force_legwear or parse_requested_legwear(text)
    excluded_wears = parse_excluded_legwear(text)
    requested_style = _requested_hosiery_style(text)
    requested_accessories = {
        str(item["id"])
        for item in LEGFOCUS_ACCESSORIES
        if _has_positive_term(text, str(item["id"]))
        or any(_has_positive_term(text, str(alias)) for alias in item.get("aliases", ()))
    }
    requested_scene = parse_requested_leg_scene(text) or parse_requested_leg_scene(preferred_scene)
    excluded_scenes = parse_excluded_leg_scenes(text)
    angles = parse_requested_leg_angles(text)
    postures = _allowed_leg_postures(text)
    explicit_face = (
        parse_requested_leg_face(text)
        or requested_face in {"half", "full"}
        or requested_crop in {"half_face", "full_face"}
    )
    result = []
    for combo in LEGFOCUS_COMBO_POOL:
        pose = LEGFOCUS_POSE_BY_ID[combo["pose"]]
        view = LEGFOCUS_VIEW_BY_ID[combo["view"]]
        effective_view = {**view, "angle": combo.get("angle") or view["angle"]}
        framing = "full_outfit" if combo["face_mode"] in {"half", "full"} else "leg_detail"
        if (
            (requested_pose and requested_pose != combo["pose"])
            or (requested_view and requested_view != combo["view"])
            or (requested_camera and requested_camera != view["camera"])
            or (force_framing and force_framing != framing)
            or (requested_full_frame and framing != "full_outfit")
            or (requested_crop and requested_crop != combo["crop_mode"])
            or (requested_face and requested_face != combo["face_mode"])
            or (explicit_face and combo["face_mode"] == "none")
            or (not explicit_face and not allow_face_combo and combo["face_mode"] == "full")
            or (not named_combo and (
                (requested_wear and requested_wear != combo["wear"])
                or combo["wear"] in excluded_wears
                or (requested_style and requested_style.get("category") != combo["category"])
                or (force_legwear_category and force_legwear_category != combo["category"])
                or (requested_accessories and requested_accessories != set(combo["accessory_ids"]))
            ))
            or (pose["posture"] not in postures)
            or not _view_matches_angle(effective_view, angles)
            or view["direction"] in angles.get("excluded_directions", ())
            or view["elevation"] in angles.get("excluded_elevations", ())
            or view["camera"] in angles.get("excluded_cameras", ())
        ):
            continue
        if not named_combo and requested_style and any(
            (LEGFOCUS_ACCESSORY_BY_ID[accessory_id].get("compatible_hosiery_families")
             and requested_style.get("family") not in LEGFOCUS_ACCESSORY_BY_ID[accessory_id]["compatible_hosiery_families"])
            or (LEGFOCUS_ACCESSORY_BY_ID[accessory_id].get("compatible_hosiery_kinds")
                and requested_style.get("kind") not in LEGFOCUS_ACCESSORY_BY_ID[accessory_id]["compatible_hosiery_kinds"])
            for accessory_id in combo["accessory_ids"]
        ):
            continue
        compositions = compatible_leg_compositions(
            pose,
            camera=view["camera"],
            scene_ids=requested_scene,
            view_id=view["id"],
            framing=framing,
            angles={key: value for key, value in angles.items()
                    if key not in {"yaw_range", "yaw_ranges", "pitch_range", "camera_height"}},
        )
        if not any(
            support == combo["support"]
            and scene["id"] not in excluded_scenes
            for scene, support, _ in compositions
        ):
            continue
        result.append(combo)
    return result


def _weighted_pose_choice(pool: List[Dict[str, Any]]) -> Dict[str, Any]:
    weights = [
        max(0.01, float(item.get("_selection_weight", item["weight"])))
        if "_selection_weight" in item
        else int(item["weight"]) * (0.15 if item.get("risk") == "high" else 1)
        for item in pool
    ]
    return dict(random.choices(pool, weights=weights, k=1)[0])


def _weighted_pose_detail_choice(
    pose: Dict[str, Any], *, camera: str, capture_mode: str,
    avoid_pose_details: set[Tuple[str, str]] | None = None,
) -> Dict[str, Any]:
    variants = [
        item for item in pose.get("detail_variants", [])
        if camera in item.get("cameras", ("selfie", "third"))
        and capture_mode in item.get("capture_modes", ("direct", "mirror"))
    ]
    if not variants:
        return {}
    pose_id = str(pose["id"])
    unused = [
        item for item in variants
        if (pose_id, str(item["id"])) not in (avoid_pose_details or set())
    ]
    choices = unused or variants
    weights = [
        int(item["weight"]) * (0.15 if item.get("risk") == "high" else 1)
        for item in choices
    ]
    return dict(random.choices(choices, weights=weights, k=1)[0])


def _compatible_capture_styles(
    scene_id: str, camera_mode: str, environment: str, state_id: str = "",
) -> List[Dict[str, Any]]:
    return [
        item for item in LEGFOCUS_CAPTURE_STYLE_POOL
        if camera_mode in item["modes"]
        and environment in item.get("environments", ["indoor"])
        and ("*" in item["scene_ids"] or scene_id in item["scene_ids"])
        and (not item.get("state_ids") or state_id in item["state_ids"])
    ]


def _weighted_capture_style_choice(
    scene_id: str, camera_mode: str, environment: str = "indoor", state_id: str = "",
) -> Dict[str, Any]:
    pool = _compatible_capture_styles(scene_id, camera_mode, environment, state_id)
    if not pool:
        raise LegFocusDataError(
            f"看看腿场景没有匹配的拍摄质感: {scene_id}/{environment}/{camera_mode}/{state_id}"
        )
    return dict(random.choices(pool, weights=[int(item["weight"]) for item in pool], k=1)[0])


def _weighted_scene_state_choice(scene_id: str, environment: str) -> Dict[str, Any]:
    pool = [
        item for item in LEGFOCUS_SCENE_STATE_POOL
        if environment in item["environments"]
        and ("*" in item["scene_ids"] or scene_id in item["scene_ids"])
    ]
    if not pool or random.random() >= LEGFOCUS_SCENE_STATE_PROBABILITY:
        return {}
    return dict(random.choices(pool, weights=[int(item["weight"]) for item in pool], k=1)[0])


def pick_leg_focus_pose(
    *,
    avoid_id: str = "",
    preferred_scene: str = "",
    camera_kind: str = "",
    include_face: bool = False,
) -> Dict[str, Any]:
    del include_face
    scene_hint = parse_requested_leg_scene(preferred_scene) or str(preferred_scene or "")
    avoid_id = _resolve_pose_id(avoid_id) or str(avoid_id or "")
    pool = [
        item for item in LEGFOCUS_POSE_POOL
        if item["id"] not in LEGFOCUS_EXPLICIT_ONLY_POSE_IDS
        if compatible_leg_compositions(item, camera=camera_kind, scene_ids=scene_hint)
    ]
    if not pool:
        raise LegFocusDataError("看看腿没有兼容当前场景与相机的姿势")
    pool = [item for item in pool if item["id"] != avoid_id] or pool
    return _weighted_pose_choice(pool)


def _leg_outfit_allows_view(outfit: Dict[str, Any], view: Dict[str, Any]) -> bool:
    if view["camera"] != "third":
        return True
    profile = LEGFOCUS_OUTFIT_CAPTURE_PROFILES[outfit["capture_profile"]]
    return (
        view["direction"] in profile["third_directions"]
        and view["elevation"] in profile["third_elevations"]
    )


def _leg_outfit_allows_environment(outfit: Dict[str, Any], environment: str) -> bool:
    return environment in outfit.get("environments", ("indoor", "outdoor"))


def _leg_outfit_allows_posture(outfit: Dict[str, Any], posture: str) -> bool:
    compatible = outfit.get("compatible_postures")
    if compatible is not None:
        return posture in compatible
    return posture != "kneeling"


def _leg_outfit_allows_composition(
    outfit: Dict[str, Any], composition: Tuple[Dict[str, Any], str, Dict[str, Any]],
) -> bool:
    scene, _, view = composition
    return (
        _leg_outfit_allows_environment(outfit, str(scene.get("environment", "indoor")))
        and _leg_outfit_allows_view(outfit, view)
    )


def _compatible_leg_outfits(
    *, hosiery_family: str = "",
    category: str = "",
    posture: str = "",
    compositions: List[Tuple[Dict[str, Any], str, Dict[str, Any]]] | None = None,
) -> List[Dict[str, Any]]:
    return [
        item for item in LEGFOCUS_OUTFIT_POOL
        if (not hosiery_family or hosiery_family in item["hosiery_families"])
        and (not category or category in item.get("category", LEGWEAR_CATEGORIES))
        and (not posture or _leg_outfit_allows_posture(item, posture))
        and (compositions is None or any(_leg_outfit_allows_composition(item, composition) for composition in compositions))
    ]


def pick_leg_focus_outfit(
    *, avoid_id: str = "", hosiery_family: str = "",
    category: str = "",
    posture: str = "",
    compositions: List[Tuple[Dict[str, Any], str, Dict[str, Any]]] | None = None,
) -> Dict[str, Any]:
    compatible = _compatible_leg_outfits(
        hosiery_family=hosiery_family, category=category, posture=posture, compositions=compositions,
    )
    pool = [
        item for item in compatible
        if item["id"] != avoid_id
    ] or compatible
    if not pool:
        raise LegFocusDataError("看看腿服饰池没有兼容当前姿势、视角与袜装的搭配")
    weights = [int(item["weight"]) * (0.15 if item.get("risk") == "high" else 1.0) for item in pool]
    return dict(random.choices(pool, weights=weights, k=1)[0])


def get_leg_focus_outfit(outfit_id: str) -> Dict[str, Any]:
    return dict(LEGFOCUS_OUTFIT_BY_ID.get(str(outfit_id or ""), {}))


def _requested_hosiery_style(text: str) -> Dict[str, Any]:
    matches = [
        (len(alias), style)
        for style in LEGFOCUS_HOSIERY_STYLES
        for alias in (style["title"], *style["aliases"])
        if _has_positive_term(text, alias)
    ]
    return dict(max(matches, key=lambda item: item[0])[1]) if matches else {}


def _hosiery_allowed(style: Dict[str, Any], text: str) -> bool:
    kind_terms = {
        "tights": ("连裤袜",), "thigh_high": ("过膝袜", "长筒袜", "长袜"),
        "stirrup": ("踩脚袜",), "ankle": ("短袜", "踝袜"),
        "knee_high": ("膝下袜", "中筒袜"), "fishnet": ("网袜", "网眼袜"),
    }
    return not any(
        _has_negative_term(text, term)
        for term in (style["title"], *style["aliases"], *kind_terms[style["kind"]])
    )


def _pick_hosiery_style(
    wear: str, outfit: Dict[str, Any], text: str = "", *,
    category: str = "", pose: Dict[str, Any] | None = None,
    excluded_kinds: set[str] | None = None,
) -> Dict[str, Any]:
    excluded_kinds = excluded_kinds or set()
    pool = [
        style for style in LEGFOCUS_HOSIERY_STYLES
        if style["family"] in outfit["hosiery_families"]
        and (style["wear"] == wear or (wear == "连裤袜" and style["kind"] == "tights"))
        and (not category or style.get("category") == category)
        and style["kind"] not in excluded_kinds
        and not style.get("explicit_only")
        and _hosiery_allowed(style, text)
    ]
    if not pool:
        return {}
    posture = str((pose or {}).get("posture") or "")
    pose_id = str((pose or {}).get("id") or "")
    relaxed_sofa_side_sit = pose_id == "mirror_side_sit_shins_overlap"
    weights = []
    for style in pool:
        weight = float(style["weight"])
        if style.get("risk") == "high":
            weight *= 0.1
        if (
            style.get("category") in {"thigh_high", "knee_high"}
            and posture in {"seated", "reclined", "lying", "side_lying", "prone"}
            and not relaxed_sofa_side_sit
        ):
            weight *= 0.35
        if style.get("kind") == "ankle" and posture == "kneeling":
            weight *= 0.15
        weights.append(max(0.01, weight))
    return dict(random.choices(pool, weights=weights, k=1)[0])


def _leg_accessory_prompt(
    item: Mapping[str, Any], wear: str, style: Mapping[str, Any],
) -> str:
    material = "袜料" if style else "皮肤"
    color_source = "袜色" if style else "肤色"
    title = str(item.get("title") or "腿部饰品")
    prefix = (
        f"薄款{title}，紧贴{material}，宽度不超过约两指、厚度低且不凸起；"
        f"边缘平整无卷边，皮肤保持本人自然肤色，颜色与{color_source}同色系、低反光；"
        "不勒入皮肤、不压迫、不留痕。"
    )
    if item.get("accessory_type") == "thigh_scarf":
        prefix += "丝巾只在大腿中段轻绕一圈，留出自然垂落的短尾，贴肤不堆叠，禁止打结团块和厚重褶皱。"
    return prefix + str(item.get("prompt") or "")


def _pick_leg_accessory(
    wear: str,
    outfit: Dict[str, Any],
    style: Dict[str, Any],
    pose: Dict[str, Any],
    text: str = "",
    *, force_ids: List[str] | None = None, require_one: bool = False,
) -> List[Dict[str, Any]]:
    outfit_id = str(outfit.get("id") or "")
    posture = str(pose.get("posture") or "")
    outfit_prompt = " ".join((str(outfit.get("prompt") or ""), str(style.get("prompt") or "")))
    forced_ids = set(force_ids or ())
    explicit = [
        item for item in LEGFOCUS_ACCESSORIES
        if str(item["id"]) in forced_ids
        or _has_positive_term(text, str(item["id"]))
        or any(_has_positive_term(text, str(alias)) for alias in item.get("aliases", []))
    ]
    pool = []
    for item in LEGFOCUS_ACCESSORIES:
        if item["compatible_wear"] and wear not in item["compatible_wear"]:
            continue
        if outfit_id in item.get("incompatible_outfit_ids", []):
            continue
        if item.get("postures") and posture not in item["postures"]:
            continue
        if style:
            if item.get("compatible_hosiery_families") and style.get("family") not in item["compatible_hosiery_families"]:
                continue
            if item.get("compatible_hosiery_kinds") and style.get("kind") not in item["compatible_hosiery_kinds"]:
                continue
        bulk = str(item.get("bulk") or "thin")
        style_category = str(style.get("category") or ("bare_legs" if wear in {"光腿", "光腿神器", "赤足"} else ""))
        if bulk == "heavy" and style_category not in {"opaque", "thigh_high"}:
            continue
        if style_category == "sheer" and bulk != "thin":
            continue
        if not explicit and item.get("risk") == "high":
            continue
        blocked_terms = item.get("blocked_by_terms", [])
        blocked = any(
            re.search(
                rf"(?:不加|不添加|不增加|不出现|不要|禁止|不需要|不带)[^。；，,]{{0,8}}{re.escape(term)}",
            outfit_prompt if item not in explicit else str(outfit.get("prompt") or ""),
            )
            for term in blocked_terms
        )
        if blocked:
            continue
        pool.append(item)
    if explicit:
        pool = [item for item in pool if item in explicit]
        if not pool:
            return []
    elif not require_one and random.random() < 0.2:
        return []
    if not pool:
        return []

    def choose(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        weights = [
            int(item["weight"])
            * (0.15 if item.get("risk") == "high" else 1)
            for item in candidates
        ]
        return dict(random.choices(candidates, weights=weights, k=1)[0])

    if explicit:
        selected = []
        groups = set()
        for item in pool:
            group = str(item.get("exclusive_group") or "")
            if selected and (
                len(selected) >= 2
                or not item.get("stackable", False)
                or any(not selected_item.get("stackable", False) for selected_item in selected)
                or (group and group in groups)
            ):
                continue
            selected.append(item)
            if group:
                groups.add(group)
        if selected:
            return selected
    return [choose(pool)]


def has_explicit_leg_outfit(text: str) -> bool:
    cleaned = LEGWEAR_NEGATION_PATTERN.sub(" ", str(text or ""))
    for style in LEGFOCUS_HOSIERY_STYLES:
        for term in (style["title"], *style["aliases"]):
            cleaned = cleaned.replace(term, " ")
    cleaned = LEGWEAR_REQUEST_PATTERN.sub(" ", cleaned)
    cleaned = re.sub(r"(?:身穿|穿着|换成|换上|改穿)\s*$", " ", cleaned)
    cleaned = re.sub(r"(?:参考图中的)?鞋袜搭配|鞋袜", " ", cleaned)
    return bool(
        LEGFOCUS_OUTFIT_REQUEST_PATTERN.search(cleaned)
        or LEGFOCUS_COMPLETE_OUTFIT_REQUEST_PATTERN.search(cleaned)
    )


def parse_requested_leg_camera(text: str) -> str:
    positive, negative = _constraint_kinds(text, LEG_CAMERA_TERMS)
    if len(positive) > 1 or positive & negative:
        raise LegFocusDataError("看看腿指定了冲突的相机人称，请选择第一人称或第三人称中的一个")
    return next(iter(positive), "")


LEG_CAMERA_TERMS = {
    "third": ("第三人称", "第三人称视角", "他拍", "别人拍", "朋友拍", "摄影师拍", "路人视角", "非自拍", "notselfie", "thirdperson"),
    "selfie": ("第一人称", "第一人称视角", "第一视角", "主观视角", "自视", "本人视角", "手机自拍", "自拍", "自己拍", "selfie"),
}


def parse_excluded_leg_cameras(text: str) -> set[str]:
    return _constraint_kinds(text, LEG_CAMERA_TERMS)[1]


LEG_POSTURE_TERMS = {
    "standing": ("站姿", "站立", "站着"),
    "seated": ("坐姿", "坐着", "侧坐"),
    "kneeling": ("跪姿", "跪坐", "跪着", "跪立"),
    "lying": ("仰卧", "仰躺", "平躺", "平卧"),
    "side_lying": ("侧躺", "侧卧"),
    "prone": ("俯卧", "趴着", "趴姿", "趴卧"),
    "reclined": ("半躺", "半卧", "半靠"),
    "any_lying": ("躺姿", "躺着", "卧姿"),
}


def _leg_pose_mentions(text: str) -> List[Tuple[int, str, str, bool]]:
    # Consume complete pose names first so a negated pose cannot expose a
    # shorter positive alias or a category word inside the same phrase.
    terms = {
        term: ("posture", kind)
        for kind, aliases in LEG_POSTURE_TERMS.items() for term in aliases
    }
    for pose in LEGFOCUS_POSE_POOL:
        for term in (pose["id"], pose["title"], *pose["legacy_ids"], *pose["aliases"]):
            terms.setdefault(re.sub(r"\s+", "", term).lower(), ("pose", pose["id"]))
    raw = re.sub(r"\s+", "", str(text or "")).lower()
    pattern = "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
    specific_pose_terms = [term for term, (kind, _) in terms.items() if kind == "pose"]
    matches = []
    for match in re.finditer(pattern, raw):
        prefix = raw[max(0, match.start() - 16):match.start()]
        kind, value = terms[match.group()]
        phrase_negated = bool(re.search(
            LEG_NEGATION_PREFIX + r"[^，,。；;！？!?]{0,12}$", prefix,
        ))
        if kind == "posture" and phrase_negated and any(
            raw.startswith(term, match.end()) for term in specific_pose_terms
        ):
            continue
        direct_negated = bool(re.search(
            LEG_NEGATION_PREFIX + r"(?:再|去|在|用|选择|采用|使用)*$", prefix,
        ))
        negated = phrase_negated if kind == "pose" else direct_negated
        matches.append((len(match.group()), kind, value, negated))
    return matches


def parse_requested_leg_pose(text: str) -> str:
    matches = [
        (length, value) for length, kind, value, negated in _leg_pose_mentions(text)
        if kind == "pose" and not negated
    ]
    return max(matches, key=lambda item: item[0])[1] if matches else ""


def parse_excluded_leg_poses(text: str) -> set[str]:
    return {
        value for _, kind, value, negated in _leg_pose_mentions(text)
        if kind == "pose" and negated
    }


def _allowed_leg_postures(text: str) -> set[str]:
    allowed = set(LEG_POSTURE_TERMS) - {"any_lying"}
    excluded = set()
    for _, kind, value, negated in _leg_pose_mentions(text):
        if kind != "posture":
            continue
        postures = {"lying", "side_lying", "prone", "reclined"} if value == "any_lying" else {value}
        if negated:
            excluded.update(postures)
        else:
            allowed.intersection_update(postures)
    allowed.difference_update(excluded)
    if not allowed:
        raise LegFocusDataError("看看腿的姿势类别要求冲突，请选择兼容的类别或调整排除条件")
    return allowed


def parse_requested_leg_view(text: str) -> str:
    raw = str(text or "")
    tag = re.search(r"【leg_view:([a-z_]+)】", raw)
    if tag and tag.group(1) in LEGFOCUS_VIEW_BY_ID:
        return tag.group(1)
    if re.search(r"(?:镜前|落地镜|对镜).{0,12}(?:轻俯拍|微俯拍|向下倾)", raw):
        if re.search(r"右|偏右", raw):
            return "self_mirror_right_gentle_down"
        if re.search(r"左|偏左", raw):
            return "self_mirror_left_gentle_down"
    matches = [
        (len(term), view["id"]) for view in LEGFOCUS_VIEW_POOL
        for term in (view["id"], view["title"]) if _has_positive_term(raw, term)
    ]
    return max(matches, key=lambda item: item[0])[1] if matches else ""


def parse_requested_leg_crop_mode(text: str) -> str:
    raw = str(text or "")
    tag = re.search(r"【crop_mode:(leg_full|shoulder_crop|half_face|full_face)】", raw, re.I)
    if tag:
        return tag.group(1).lower()
    if any(_has_positive_term(raw, term) for term in ("到肩不露脸", "肩下裁切", "只拍肩以下", "肩线以下裁切")):
        return "shoulder_crop"
    if any(_has_positive_term(raw, term) for term in ("完整面部", "正脸入镜", "完整露脸", "全脸入镜")):
        return "full_face"
    if any(_has_positive_term(raw, term) for term in ("半脸", "侧脸")):
        return "half_face"
    if parse_requested_leg_face(raw):
        return "half_face"
    return ""


def parse_leg_angle_tokens(text: str) -> Dict[str, Any]:
    raw = str(text or "")
    result: Dict[str, Any] = {}
    if any(_has_positive_term(raw, term) for term in ("陡俯", "陡俯拍", "较陡俯拍", "大角度俯拍")):
        result["pitch_range"] = (50, 60)
        result["pitch"] = 55
    elif any(_has_positive_term(raw, term) for term in ("斜俯", "斜俯拍", "斜向下", "斜向俯拍")):
        result["pitch_range"] = (30, 40)
        result["pitch"] = 35
    elif any(_has_positive_term(raw, term) for term in ("轻俯", "轻俯拍", "微俯", "微俯拍")):
        result["pitch_range"] = (10, 20)
        result["pitch"] = 15
    elif any(_has_positive_term(raw, term) for term in ("仰拍", "仰视", "向上拍")):
        result["pitch_range"] = (-25, -15)
        result["pitch"] = -20
        result["elevations"] = {"up"}
    elif any(_has_positive_term(raw, term) for term in ("平视", "平拍")):
        result["pitch_range"] = (-5, 5)
        result["pitch"] = 0
        result["elevations"] = {"level"}

    if any(_has_positive_term(raw, term) for term in ("右前", "右前方", "右前斜")):
        result["yaw_range"] = (30, 60)
        result["yaw"] = 45
    elif any(_has_positive_term(raw, term) for term in ("左前", "左前方", "左前斜")):
        result["yaw_range"] = (-60, -30)
        result["yaw"] = -45
    elif _has_positive_term(raw, "斜侧拍"):
        result["yaw_ranges"] = ((-60, -30), (30, 60))
        result["yaw_values"] = (-45, 45)
    elif any(_has_positive_term(raw, term) for term in ("左正侧", "左侧面", "左侧拍")):
        result["yaw_range"] = (-100, -80)
        result["yaw"] = -90
    elif any(_has_positive_term(raw, term) for term in ("右正侧", "右侧面", "右侧拍")):
        result["yaw_range"] = (80, 100)
        result["yaw"] = 90
    elif any(_has_positive_term(raw, term) for term in ("正侧", "正側", "侧拍", "侧面")):
        result["yaw_ranges"] = ((-100, -80), (80, 100))
        result["yaw_values"] = (-90, 90)
    elif _has_positive_term(raw, "左侧"):
        result["yaw_range"] = (-100, -80)
        result["yaw"] = -90
    elif _has_positive_term(raw, "右侧"):
        result["yaw_range"] = (80, 100)
        result["yaw"] = 90
    elif any(_has_positive_term(raw, term) for term in ("正前", "正面拍", "正拍")):
        result["yaw_range"] = (-10, 10)
        result["yaw"] = 0
    if any(_has_positive_term(raw, term) for term in ("高于肩线", "高过肩线", "肩线以上")):
        result["camera_height"] = "above_shoulder"
    return result


def parse_requested_leg_angles(text: str) -> Dict[str, Any]:
    raw = str(text or "").lower()
    compact = re.sub(r"\s+", "", raw)
    if any(_has_positive_term(raw, term) for term in ("背拍", "背身", "背面", "侧后方", "后方拍")):
        raise LegFocusDataError("看看腿不支持背面或侧后方机位，请使用正前方、斜前方或侧面机位")
    filters: Dict[str, Any] = {
        "excluded_directions": set(), "excluded_elevations": set(),
    }
    left_front = any(_has_positive_term(raw, term) for term in ("左前斜", "左前方", "左前")) or (
        "斜前" in compact and _has_positive_term(raw, "偏左")
    )
    right_front = any(_has_positive_term(raw, term) for term in ("右前斜", "右前方", "右前")) or (
        "斜前" in compact and _has_positive_term(raw, "偏右")
    )
    if left_front and right_front:
        raise LegFocusDataError("看看腿同时指定了左右两个镜头角度，请选择左前或右前中的一个")
    direction_terms = dict((
        ("oblique", ("斜前方", "侧前方", "斜侧拍")),
        ("side", ("正侧", "侧拍", "侧面", "侧方")),
        ("front", ("正前", "正前方", "正面拍", "正拍")),
    ))
    if left_front or right_front:
        direction_kinds, excluded_directions = _constraint_kinds(raw, direction_terms)
        direction_kinds.add("oblique")
        yaw_match = re.search(
            r"(?:(?:左前|右前)(?:方|斜)?(?:约|大约)?(\d{1,3})(?:度|°)|(\d{1,3})(?:度|°)(?:左前|右前)(?:方|斜)?)",
            compact,
        )
        target_yaw = int(next(value for value in yaw_match.groups() if value)) if yaw_match else 45
        yaw = -target_yaw if left_front else target_yaw
        filters["yaw_range"] = (max(-180, yaw - 5), min(180, yaw + 5))
    else:
        direction_kinds, excluded_directions = _constraint_kinds(raw, {
            **direction_terms,
            "left": ("偏左", "左侧", "左"),
            "right": ("偏右", "右侧", "右"),
        })
    filters["excluded_directions"] = excluded_directions
    if len(direction_kinds) > 1:
        raise LegFocusDataError("看看腿同时指定了不兼容的镜头方向，请选择正前方、斜前方或侧面中的一个")
    if direction_kinds:
        filters["direction"] = next(iter(direction_kinds))
    level_requested = any(_has_positive_term(raw, term) for term in ("平拍", "平视"))
    explicit_up = any(_has_positive_term(raw, term) for term in ("仰拍", "仰视", "向上拍"))
    explicit_down = any(_has_positive_term(raw, term) for term in ("俯拍", "俯视", "向下拍", "向下看"))
    # “低机位/高机位平拍” describes camera height, not a tilted lens.
    if level_requested and not (explicit_up or explicit_down):
        filters["elevations"] = {"level"}
    elif level_requested and (explicit_up or explicit_down):
        raise LegFocusDataError("看看腿同时指定了不兼容的俯仰角度，请选择仰拍、俯拍或平拍中的一个")
    pitch_terms = r"(?:俯拍|俯视|下俯|向下拍|向下看|仰拍|仰视|向上拍)"
    exact_pitch_match = re.search(
        pitch_terms + r"(?:约|大约|接近)?(\d{1,2})(?:度|°)?", compact,
    )
    if exact_pitch_match:
        pitch_value = exact_pitch_match.group(1)
    else:
        exact_pitch_match = re.search(
            r"(\d{1,2})(?:度|°)" + pitch_terms, compact,
        )
        pitch_value = exact_pitch_match.group(1) if exact_pitch_match else ""
    if exact_pitch_match:
        pitch = int(pitch_value)
        if explicit_up and not explicit_down:
            pitch = -pitch
        elif explicit_down and not explicit_up:
            pitch = abs(pitch)
        filters["pitch_range"] = (max(-90, pitch - 5), min(90, pitch + 5))
    elif any(_has_positive_term(raw, term) for term in ("较陡俯拍", "陡俯拍", "陡俯", "大角度俯拍")):
        filters["pitch_range"] = (50, 60)
    elif any(_has_positive_term(raw, term) for term in ("斜俯", "斜向下", "斜向俯拍")):
        filters["pitch_range"] = (30, 40)
    elif any(_has_positive_term(raw, term) for term in ("轻俯", "微俯", "轻微俯拍")):
        filters["pitch_range"] = (10, 20)
    if any(_has_positive_term(raw, term) for term in ("高于肩线", "高过肩线", "肩线以上", "高于肩部", "头顶前上方")):
        filters["camera_height"] = "above_shoulder"
    for values, terms in (
        ({"top"}, ("正上方", "顶拍", "topdown", "top-down")),
        ({"down"}, ("俯拍", "俯视", "向下拍", "向下看")),
        ({"up"}, ("仰拍", "低机位", "向上拍")),
        ({"level"}, ("平拍", "平视")),
    ):
        if level_requested and values == {"level"}:
            continue
        positive = any(_has_positive_term(raw, term) for term in terms)
        if values == {"up"} and level_requested and not explicit_up:
            positive = False
        if positive:
            selected = filters.get("elevations", values) & values
            if not selected:
                raise LegFocusDataError("看看腿同时指定了不兼容的俯仰角度，请选择一个角度")
            filters["elevations"] = selected
        elif any(_has_negative_term(raw, term) for term in terms):
            filters["excluded_elevations"].update(values)
            if values == {"down"}:
                filters["excluded_elevations"].add("top")
    token_angles = parse_leg_angle_tokens(raw)
    if "yaw" in token_angles and "yaw_range" not in filters:
        target_yaw = float(token_angles["yaw"])
        filters["yaw_range"] = (max(-180, target_yaw - 5), min(180, target_yaw + 5))
    if "yaw_values" in token_angles:
        filters["yaw_ranges"] = tuple(
            (max(-180, float(value) - 5), min(180, float(value) + 5))
            for value in token_angles["yaw_values"]
        )
    for key, value in token_angles.items():
        if key not in {"yaw", "yaw_values", "yaw_range", "yaw_ranges"}:
            filters.setdefault(key, value)
    return filters


def parse_excluded_legwear(text: str) -> set[str]:
    raw = str(text or "")
    excluded: set[str] = set()
    no_socks = any(
        re.search(r"袜", match.group()) for match in LEGWEAR_NEGATION_PATTERN.finditer(raw)
    )
    if no_socks or any(_has_negative_term(raw, term) for term in ("袜子", "袜装", "袜", "丝袜")):
        excluded.update({"黑丝", "白丝", "连裤袜"})
    if any(_has_negative_term(raw, term) for term in ("黑丝", "黑色丝袜", "黑色袜装")):
        excluded.add("黑丝")
    if any(_has_negative_term(raw, term) for term in ("白丝", "白色丝袜", "白色袜装")):
        excluded.add("白丝")
    if any(_has_negative_term(raw, term) for term in ("光腿", "光腿神器")):
        excluded.update({"光腿", "光腿神器", "赤足"})
    if any(_has_negative_term(raw, term) for term in ("赤足", "裸足", "光脚")):
        excluded.add("赤足")
    return excluded


def parse_requested_legwear(text: str) -> str:
    raw = str(text or "")
    safe_tag = re.search(r"【wear:(daily|custom|white|black|bare|barefoot|pantyhose)】", raw)
    if safe_tag:
        return {
            "daily": "日常裤装",
            "custom": "用户指定",
            "white": "白丝",
            "black": "黑丝",
            "bare": "光腿神器",
            "barefoot": "赤足",
            "pantyhose": "连裤袜",
        }[safe_tag.group(1)]
    requested_style = _requested_hosiery_style(raw)
    if requested_style:
        return str(requested_style["wear"])
    explicit_values = ("白丝", "黑丝", "光腿神器", "光腿", "赤足", "连裤袜", "丝袜", "光脚", "裸足")
    for value in explicit_values:
        if _has_positive_term(LEGWEAR_NEGATION_PATTERN.sub(" ", raw), value):
            if value in {"光脚", "裸足"}:
                return "赤足"
            if value == "丝袜":
                return "连裤袜"
            if value == "光腿神器":
                return "光腿"
            return value
    if re.search(r"(?:长裤|牛仔裤|西裤|休闲裤|阔腿裤)", raw):
        return "日常裤装"
    return ""


def _weighted_detail_choice(
    excluded_wears: set[str] | None = None,
    *, category: str = "", allowed_categories: set[str] | None = None,
) -> Dict[str, Any]:
    excluded = set(excluded_wears or ())
    pool = [
        item for item in LEGFOCUS_RANDOM_LEG_DETAILS
        if str(item.get("wear") or "") not in excluded
        and not item.get("explicit_only", False)
        and (not category or item.get("category") == category)
        and (allowed_categories is None or item.get("category") in allowed_categories)
    ]
    if not pool:
        raise LegFocusDataError("看看腿没有满足分类与排除条件的腿部细节，请放宽袜装要求")
    if category:
        return dict(random.choices(pool, weights=[int(item["weight"]) for item in pool], k=1)[0])
    category_weights = {
        str(item["category"]): int(item.get("category_weight", 1))
        for item in pool
    }
    available_categories = list(category_weights)
    selected_category = random.choices(
        available_categories,
        weights=[category_weights[value] for value in available_categories],
        k=1,
    )[0]
    category_pool = [item for item in pool if item["category"] == selected_category]
    return dict(random.choices(category_pool, weights=[int(item["weight"]) for item in category_pool], k=1)[0])


def _clean_extra_request(extra_request: str) -> str:
    extra = re.sub(r"/?看看腿", " ", str(extra_request or ""), flags=re.I)
    extra = re.sub(r"【(?:legs|legs_face|leg_view|leg_scene|leg_support|leg_capture_style|leg_state|face_mode|crop_mode|leg_expression|cam|wear|pose|pose_detail|outfit|frame|legwear_category|hosiery|leg_accessory):[a-z0-9_|,\u4e00-\u9fff]+】", " ", extra, flags=re.I)
    extra = re.sub(r"【leg_view_title:[^】]+】", " ", extra, flags=re.I)
    extra = re.sub(r"【(?:combo|angle):[^】]+】", " ", extra, flags=re.I)
    # Keep the original constraint once; deleting fragments loses negation,
    # sock lengths, or the support required by a free-form pose.
    for risky_text, neutral_text in LEGFOCUS_RISKY_EXTRA_REPLACEMENTS:
        extra = extra.replace(risky_text, neutral_text)
    extra = re.sub(r"\s+", " ", extra).strip(" 。、，")
    return re.sub(r"\s*([，,])\s*", r"\1", extra)


def build_leg_focus_action(
    extra_request: str = "",
    has_refs: bool = False,
    *,
    avoid_pose: str = "",
    force_legwear: str = "",
    force_legwear_category: str = "",
    force_face_mode: str = "",
    force_crop_mode: str = "",
    force_pose: str = "",
    force_camera: str = "",
    force_framing: str = "",
    force_view: str = "",
    force_pose_detail: str = "",
    force_outfit: str = "",
    avoid_combinations: set[Tuple[str, str]] | None = None,
    avoid_combos: set[str] | None = None,
    prefer_combo: bool = False,
    avoid_pose_details: set[Tuple[str, str]] | None = None,
    preferred_scene: str = "",
) -> str:
    raw_extra = str(extra_request or "")
    requested_combo_id = parse_requested_leg_combo(raw_extra)
    combo: Dict[str, Any] = {}
    if requested_combo_id:
        matches = _matching_leg_combos(
            raw_extra,
            allow_face_combo=True,
            force_pose=force_pose,
            force_view=force_view,
            force_camera=force_camera,
            force_framing=force_framing,
            force_crop_mode=force_crop_mode,
            force_face_mode=force_face_mode,
            force_legwear=force_legwear,
            force_legwear_category=force_legwear_category,
            preferred_scene=preferred_scene,
            named_combo=True,
        )
        combo = LEGFOCUS_COMBO_BY_ID[requested_combo_id]
        if combo not in matches:
            raise LegFocusDataError("指定的动作与机位组合和本次姿势或拍摄要求不兼容")
    elif (
        not has_refs and not force_outfit and not force_pose
        and not parse_requested_leg_pose(raw_extra)
        and not has_explicit_leg_outfit(raw_extra)
        and not _preserve_unhandled_legwear(raw_extra)
        and (prefer_combo or random.random() < 0.4)
    ):
        matches = _matching_leg_combos(
            raw_extra,
            allow_face_combo=bool(
                LEG_FULL_OUTFIT_REQUEST_PATTERN.search(raw_extra)
                and not re.search(r"(?:不|不要|无需|不需要)\s*(?:全身(?!镜)|从头到脚)", raw_extra)
            ),
            force_pose=force_pose,
            force_view=force_view,
            force_camera=force_camera,
            force_framing=force_framing,
            force_crop_mode=force_crop_mode or parse_requested_leg_crop_mode(raw_extra),
            force_face_mode=force_face_mode,
            force_legwear=force_legwear,
            force_legwear_category=force_legwear_category,
            preferred_scene=preferred_scene,
        )
        excluded_poses = parse_excluded_leg_poses(raw_extra)
        matches = [
            item for item in matches
            if item["pose"] not in excluded_poses
            and item["id"] not in (avoid_combos or set())
            and (item["pose"], item["view"]) not in (avoid_combinations or set())
        ]
        if avoid_pose:
            avoid_combo_matches = [item for item in matches if item["pose"] != _resolve_pose_id(avoid_pose)]
            matches = avoid_combo_matches or matches
        if matches:
            face_requested = bool(
                parse_requested_leg_face(raw_extra) or force_face_mode in {"half", "full"}
                or force_crop_mode in {"half_face", "full_face"} or force_framing == "full_outfit"
                or LEG_FULL_OUTFIT_REQUEST_PATTERN.search(raw_extra)
            )
            combo = random.choices(matches, weights=[
                item["weight"]
                * (0.15 if item["face_mode"] == "half" and not face_requested else 1)
                * (0.15 if item["risk"] == "high" else 1)
                for item in matches
            ], k=1)[0]
    requested_view = str(force_view or parse_requested_leg_view(raw_extra) or combo.get("view") or "")
    if requested_view and requested_view not in LEGFOCUS_VIEW_BY_ID:
        candidates = "、".join(str(view["title"]) for view in LEGFOCUS_VIEW_POOL[:8])
        raise LegFocusDataError(f"指定机位无法识别。可选机位示例：{candidates}")
    angles = parse_requested_leg_angles(raw_extra)
    requested_camera = str(force_camera or "").strip()
    if requested_camera not in {"selfie", "third"}:
        requested_camera = parse_requested_leg_camera(raw_extra) or (
            str(LEGFOCUS_VIEW_BY_ID[combo["view"]]["camera"]) if combo else ""
        )
    explicit_face_or_full_request = bool(
        force_face_mode in {"half", "full"} or parse_requested_leg_face(raw_extra)
        or (force_crop_mode or parse_requested_leg_crop_mode(raw_extra)) in {"half_face", "full_face"}
        or force_framing == "full_outfit"
        or LEG_FULL_OUTFIT_REQUEST_PATTERN.search(raw_extra)
    )
    include_face = parse_requested_leg_face(raw_extra)
    crop_mode_tag = re.search(r"【crop_mode:(leg_full|shoulder_crop|half_face|full_face)】", raw_extra, re.I)
    crop_mode = str(force_crop_mode or (crop_mode_tag.group(1) if crop_mode_tag else "")).strip().lower()
    if crop_mode and crop_mode not in LEGFOCUS_CROP_MODES:
        raise LegFocusDataError("看看腿构图模式无法识别；可选范围包括腿部主体、肩线下裁切、半脸和完整面部")
    if not crop_mode:
        crop_mode = parse_requested_leg_crop_mode(raw_extra) or str(combo.get("crop_mode") or "leg_full")
    face_mode_tag = re.search(r"【face_mode:(none|half|full)】", raw_extra, re.I)
    face_mode = str(force_face_mode or (face_mode_tag.group(1) if face_mode_tag else "")).strip().lower()
    explicit_face_mode = bool(force_face_mode or face_mode_tag or combo.get("face_mode") in {"half", "full"})
    requested_crop_mode = parse_requested_leg_crop_mode(raw_extra)
    if face_mode not in {"none", "half", "full"}:
        if re.search(r"(?:不露脸|不露头|不拍脸|只拍腿|只拍下装|上半身不入镜)", raw_extra):
            face_mode = "none"
        elif re.search(r"(?:完整露脸|正脸入镜|完整面部|全身(?!镜)|从头到脚)", raw_extra):
            face_mode = "full"
        elif re.search(r"(?:半脸|侧脸)", raw_extra):
            face_mode = "half"
        else:
            wants_full_frame = force_framing == "full_outfit" or bool(LEG_FULL_OUTFIT_REQUEST_PATTERN.search(raw_extra))
            face_mode = str(combo.get("face_mode") or ("half" if wants_full_frame else "none"))
            if face_mode not in {"none", "half", "full"}:
                face_mode = "none"
    if explicit_face_mode:
        include_face = face_mode in {"half", "full"}
    if crop_mode == "shoulder_crop":
        face_mode = "none"
        include_face = False
    elif crop_mode in {"half_face", "full_face"}:
        face_mode = "half" if crop_mode == "half_face" else "full"
        include_face = True
        explicit_face_mode = True
    framing = str(force_framing or "").strip()
    if crop_mode == "shoulder_crop":
        framing = "leg_detail"
    elif framing not in {"full_outfit", "leg_detail"}:
        explicit_full = bool(LEG_FULL_OUTFIT_REQUEST_PATTERN.search(raw_extra)) and not bool(
            re.search(r"(?:不|不要|无需|不需要)\s*(?:全身(?!镜)|从头到脚)", raw_extra)
        )
        framing = "full_outfit" if explicit_full or include_face else "leg_detail"
    explicit_face_request = include_face or explicit_face_mode or framing == "full_outfit"
    visible_face_requested = explicit_face_request and face_mode in {"half", "full"}
    if include_face and framing == "leg_detail":
        raise LegFocusDataError("看看腿的腿部局部构图不能同时露脸，请选择全身构图或取消露脸要求")
    requested_scene = parse_requested_leg_scene(raw_extra)
    excluded_scenes = parse_excluded_leg_scenes(raw_extra)
    explicit_scene = bool(requested_scene)
    if not requested_scene and preferred_scene:
        requested_scene = parse_requested_leg_scene(preferred_scene)
    if (
        requested_camera == "third"
        and not combo
        and not requested_crop_mode
        and not force_crop_mode
        and not explicit_face_mode
        and not include_face
        and not LEG_FULL_OUTFIT_REQUEST_PATTERN.search(raw_extra)
        and not force_framing
    ):
        crop_mode = "shoulder_crop"
        face_mode = "none"
        include_face = False
    # A self-view camera cannot include its owner's head-to-toe body.
    if framing == "full_outfit":
        if requested_camera == "selfie":
            raise LegFocusDataError("看看腿的第一人称自视不能拍到本人从头到脚，请选择第三人称全身或第一人称腿部构图")
        requested_camera = "third"
    excluded_cameras = parse_excluded_leg_cameras(raw_extra)
    if requested_camera and requested_camera in excluded_cameras:
        raise LegFocusDataError("看看腿指定的相机人称同时被排除，请选择第一人称或第三人称中的一个")
    forced_pose_id = _resolve_pose_id(force_pose)
    if force_pose and not forced_pose_id:
        options = "、".join(str(item["title"]) for item in LEGFOCUS_POSE_POOL[:8])
        raise LegFocusDataError(f"指定姿势无法识别。可选姿势示例：{options}")
    requested_pose = forced_pose_id or _resolve_pose_id(parse_requested_leg_pose(raw_extra)) or str(combo.get("pose") or "")
    if requested_pose and LEGFOCUS_POSE_BY_ID[requested_pose]["posture"] == "kneeling" and explicit_face_or_full_request:
        raise LegFocusDataError("跪姿使用腿部局部构图，不支持露脸或全身入镜要求")
    allowed_postures = _allowed_leg_postures(raw_extra)
    excluded_poses = parse_excluded_leg_poses(raw_extra)
    if requested_pose and requested_pose in excluded_poses:
        raise LegFocusDataError("看看腿指定的姿势同时被排除，请选择其他姿势")
    avoid_id = _resolve_pose_id(avoid_pose) or str(avoid_pose or "")
    requested_style = _requested_hosiery_style(raw_extra)
    forced_outfit = get_leg_focus_outfit(force_outfit) if force_outfit else {}
    if force_outfit and not forced_outfit:
        raise LegFocusDataError(f"看看腿服饰不存在: {force_outfit}")
    if forced_outfit and requested_pose and not _leg_outfit_allows_posture(
        forced_outfit, str(LEGFOCUS_POSE_BY_ID[requested_pose]["posture"]),
    ):
        raise LegFocusDataError("所选服饰与当前姿势不兼容，请更换服饰或姿势")
    explicit_outfit = has_explicit_leg_outfit(raw_extra)
    outfit_source = "pool" if forced_outfit else "custom" if explicit_outfit else "reference" if has_refs else "pool"
    requested_family = str(requested_style.get("family") or "")
    requested_wear_hint = str(force_legwear or parse_requested_legwear(raw_extra) or "")
    excluded_wears = parse_excluded_legwear(raw_extra)
    override_combo_wear = bool(combo) and (
        bool(requested_style or _preserve_unhandled_legwear(raw_extra))
        or (requested_wear_hint and requested_wear_hint != combo["wear"])
        or combo["wear"] in excluded_wears
        or (force_legwear_category and force_legwear_category != combo["category"])
    )
    use_combo_wear = bool(combo) and not override_combo_wear
    category_filter = str(
        force_legwear_category or (combo["category"] if use_combo_wear and outfit_source == "pool" else "")
    ).strip()
    if category_filter and category_filter not in LEGWEAR_CATEGORIES:
        raise LegFocusDataError(f"看看腿服饰类别不存在: {category_filter}")
    outfit_candidates = [forced_outfit] if forced_outfit else _compatible_leg_outfits(
        hosiery_family=requested_family, category=category_filter,
    )
    if forced_outfit and requested_family and requested_family not in forced_outfit["hosiery_families"]:
        raise LegFocusDataError("看看腿指定的服饰与袜装不兼容，请调整其中一项")
    requested_style_kind = str(requested_style.get("kind") or "")
    if requested_pose and requested_style_kind in set(
        LEGFOCUS_POSE_BY_ID[requested_pose].get("disallowed_hosiery_kinds") or ()
    ):
        raise LegFocusDataError(
            f"看看腿姿势“{LEGFOCUS_POSE_BY_ID[requested_pose]['title']}”与指定袜型不兼容，请更换姿势或袜型"
        )

    pose_options = (
        [LEGFOCUS_POSE_BY_ID[requested_pose]]
        if requested_pose else [pose for pose in LEGFOCUS_POSE_POOL if pose["id"] not in LEGFOCUS_EXPLICIT_ONLY_POSE_IDS]
    )
    selection_angles = {
        **angles,
        "excluded_scenes": excluded_scenes,
        "excluded_cameras": excluded_cameras,
    }
    if combo:
        selection_angles = {
            key: value for key, value in selection_angles.items()
            if key not in {"yaw_range", "yaw_ranges", "pitch_range", "camera_height"}
        }
    explicit_view_constraints = bool(
        requested_view or requested_camera or any(
            key in angles for key in ("direction", "elevations", "yaw_range", "pitch_range", "camera_height")
        )
    )
    allow_explicit_combination = bool(requested_pose and explicit_view_constraints)

    def candidates(scene_hint: str) -> Dict[str, List[Tuple[Dict[str, Any], str, Dict[str, Any]]]]:
        filtered_scene_hint = " ".join(
            scene_id for scene_id in scene_hint.split() if scene_id not in excluded_scenes
        )
        if scene_hint.strip() and not filtered_scene_hint:
            return {}
        result = {}
        for pose in pose_options:
            if pose["id"] in excluded_poses or pose["posture"] not in allowed_postures:
                continue
            pose_outfits = [
                outfit for outfit in outfit_candidates
                if _leg_outfit_allows_posture(outfit, str(pose["posture"]))
            ]
            if visible_face_requested and pose["posture"] in {"lying", "side_lying", "prone"}:
                continue
            combinations = compatible_leg_compositions(
                pose, camera=requested_camera, scene_ids=filtered_scene_hint,
                view_id=requested_view, framing=framing, angles=selection_angles,
                allow_explicit=allow_explicit_combination,
            )
            if combo:
                combinations = [item for item in combinations if item[1] == combo["support"]]
            if visible_face_requested:
                combinations = [
                    item for item in combinations
                    if item[2].get("elevation") not in {"down", "top"}
                ]
            if outfit_source == "pool":
                combinations = [
                    item for item in combinations
                    if any(_leg_outfit_allows_composition(outfit, item) for outfit in pose_outfits)
                ]
            if combinations:
                result[pose["id"]] = combinations
        return result

    eligible = candidates(requested_scene)
    if not eligible and requested_scene and not explicit_scene:
        eligible = candidates("")
    if not eligible:
        if visible_face_requested:
            raise LegFocusDataError("看看腿当前姿势或俯拍机位与露脸不兼容，请改用站姿、坐姿或平视/仰拍机位")
        diagnostic_scene_hint = " ".join(
            scene_id for scene_id in requested_scene.split() if scene_id not in excluded_scenes
        )
        available_view_ids: set[str] = set()
        diagnostic_poses = [LEGFOCUS_POSE_BY_ID[requested_pose]] if requested_pose else pose_options
        for candidate_pose in diagnostic_poses:
            for candidate_combination in compatible_leg_compositions(
                candidate_pose,
                camera=requested_camera,
                scene_ids=diagnostic_scene_hint,
                framing=framing,
                angles=selection_angles,
                allow_explicit=allow_explicit_combination,
            ):
                candidate_view = candidate_combination[2]
                if requested_camera and candidate_view["camera"] != requested_camera:
                    continue
                if outfit_source == "pool" and not any(
                    _leg_outfit_allows_composition(outfit, candidate_combination)
                    for outfit in outfit_candidates
                    if _leg_outfit_allows_posture(outfit, candidate_pose["posture"])
                ):
                    continue
                available_view_ids.add(str(candidate_view["id"]))
        available_pose_ids: set[str] = set()
        for candidate_pose in LEGFOCUS_POSE_POOL:
            if candidate_pose["id"] in excluded_poses or candidate_pose["posture"] not in allowed_postures:
                continue
            if visible_face_requested and candidate_pose["posture"] in {"lying", "side_lying", "prone"}:
                continue
            matches = compatible_leg_compositions(
                candidate_pose,
                camera=requested_camera,
                scene_ids=diagnostic_scene_hint,
                view_id=requested_view,
                framing=framing,
                angles=selection_angles,
                allow_explicit=allow_explicit_combination,
            )
            if matches and (
                outfit_source != "pool"
                or any(
                    _leg_outfit_allows_posture(outfit, candidate_pose["posture"])
                    and _leg_outfit_allows_composition(outfit, item)
                    for outfit in outfit_candidates for item in matches
                )
            ):
                available_pose_ids.add(str(candidate_pose["id"]))
        view_titles_text = "、".join(
            LEGFOCUS_VIEW_BY_ID[view_id]["title"] for view_id in sorted(available_view_ids)[:8]
        ) or "无"
        pose_titles_text = "、".join(
            LEGFOCUS_POSE_BY_ID[pose_id]["title"] for pose_id in sorted(available_pose_ids)[:8]
        ) or "无"
        raise LegFocusDataError(
            "看看腿指定的姿势、场景、服饰或视角不兼容；"
            f"当前姿势可用机位: [{view_titles_text}]；当前机位可用姿势: [{pose_titles_text}]；"
            "兼容检查包含 camera、scene/support、framing、angle 与服饰条件。"
            "不会强行改变支撑动作或相机人称"
        )
    if requested_style_kind and not requested_pose:
        eligible = {
            pose_id: combinations for pose_id, combinations in eligible.items()
            if requested_style_kind not in set(
                LEGFOCUS_POSE_BY_ID[pose_id].get("disallowed_hosiery_kinds") or ()
            )
        }
        if not eligible:
            raise LegFocusDataError("看看腿没有兼容本次指定袜型的姿势，请更换袜型或指定姿势")
    detail_id = str(force_pose_detail or "").strip()
    if detail_id:
        filtered = {}
        for pose_id, combinations in eligible.items():
            detail = next((
                item for item in LEGFOCUS_POSE_BY_ID[pose_id].get("detail_variants", [])
                if str(item["id"]) == detail_id
            ), {})
            compatible = [
                item for item in combinations
                if detail
                and item[2]["camera"] in detail.get("cameras", ("selfie", "third"))
                and item[2].get("capture_mode", "direct")
                in detail.get("capture_modes", ("direct", "mirror"))
            ]
            if compatible:
                filtered[pose_id] = compatible
        eligible = filtered
        if not eligible:
            raise LegFocusDataError(f"看看腿姿势细节不存在或与机位不兼容: {requested_pose or '随机姿势'}/{detail_id}")
    unused = {} if requested_combo_id else {
        pose_id: options for pose_id, combinations in eligible.items()
        if (options := [item for item in combinations if (pose_id, item[2]["id"]) not in (avoid_combinations or set())])
    }
    eligible = unused or eligible
    pose_choices = [LEGFOCUS_POSE_BY_ID[pose_id] for pose_id in eligible]
    if not requested_pose:
        pose_choices = [pose for pose in pose_choices if pose["id"] != avoid_id] or pose_choices
    view_pose_counts: Dict[str, int] = {}
    for pose_id, pose_combinations in eligible.items():
        for view_id in {item[2]["id"] for item in pose_combinations}:
            view_pose_counts[view_id] = view_pose_counts.get(view_id, 0) + 1
    weighted_pose_choices = []
    for candidate_pose in pose_choices:
        rare_views = {
            item[2]["id"] for item in eligible[candidate_pose["id"]]
        }
        scarcity = sum(1 / (view_pose_counts[view_id] ** 0.5) for view_id in rare_views) / max(1, len(rare_views))
        risk_factor = 0.15 if candidate_pose.get("risk") == "high" and not requested_pose else 1.0
        weighted_pose_choices.append({
            **candidate_pose,
            "_selection_weight": int(candidate_pose["weight"]) * risk_factor * (1 + 2 * scarcity),
        })
    pose = _weighted_pose_choice(weighted_pose_choices)
    pose.pop("_selection_weight", None)
    pose_id = pose["id"]
    combinations = eligible[pose_id]
    forced_detail: Dict[str, Any] = {}
    if detail_id:
        forced_detail = next((
            item for item in pose.get("detail_variants", [])
            if str(item["id"]) == detail_id
        ), {})
        if not forced_detail:
            raise LegFocusDataError(f"看看腿姿势细节不存在或与机位不兼容: {pose_id}/{detail_id}")
        combinations = [
            item for item in combinations
            if item[2]["camera"] in forced_detail.get("cameras", ("selfie", "third"))
            and item[2].get("capture_mode", "direct")
            in forced_detail.get("capture_modes", ("direct", "mirror"))
        ]
        if not combinations:
            raise LegFocusDataError(f"看看腿姿势细节不存在或与机位不兼容: {pose_id}/{detail_id}")
    outfit: Dict[str, Any] = {}
    if outfit_source == "pool":
        outfit = forced_outfit or pick_leg_focus_outfit(
            hosiery_family=requested_family, category=category_filter,
            posture=str(pose["posture"]), compositions=combinations,
        )
        if not _leg_outfit_allows_posture(outfit, str(pose["posture"])):
            raise LegFocusDataError("所选服饰与当前姿势不兼容，请更换服饰或姿势")
        combinations = [item for item in combinations if _leg_outfit_allows_composition(outfit, item)]
        if not combinations:
            raise LegFocusDataError("看看腿选定的服饰与姿势机位不兼容，请调整服饰或视角")
    camera_kinds = list(dict.fromkeys(item[2]["camera"] for item in combinations))
    camera_kind = (
        str(pose.get("camera_preference"))
        if not requested_camera and pose.get("camera_preference") in camera_kinds
        else random.choice(camera_kinds)
    )
    combinations = [item for item in combinations if item[2]["camera"] == camera_kind]
    if (
        camera_kind == "third"
        and not combo
        and not (requested_crop_mode or force_crop_mode)
        and not explicit_face_mode
        and not include_face
        and not explicit_face_or_full_request
    ):
        crop_mode = "shoulder_crop"
        face_mode = "none"
        include_face = False
        framing = "leg_detail"
    view_ids = list(dict.fromkeys(item[2]["id"] for item in combinations))
    eligible_pose_counts: Dict[str, int] = {}
    for eligible_pose_id, pose_combinations in eligible.items():
        if eligible_pose_id == pose_id:
            continue
        if any(item[2]["camera"] == camera_kind for item in pose_combinations):
            for view_id in {item[2]["id"] for item in pose_combinations if item[2]["camera"] == camera_kind}:
                eligible_pose_counts[view_id] = eligible_pose_counts.get(view_id, 0) + 1
    selected_view_counts = {
        view_id: eligible_pose_counts.get(view_id, 0) + 1 for view_id in view_ids
    }
    prefer_third_oblique_down = bool(
        camera_kind == "third"
        and not requested_view
        and not combo
    )
    view = random.choices(
        [LEGFOCUS_VIEW_BY_ID[view_id] for view_id in view_ids],
        weights=[
            LEGFOCUS_VIEW_BY_ID[view_id]["weight"]
            * LEGFOCUS_VIEW_BY_ID[view_id].get("selection_weight", 1)
            / (selected_view_counts[view_id] ** 0.5)
            * (
                4 if prefer_third_oblique_down
                and 25 <= LEGFOCUS_VIEW_BY_ID[view_id]["angle"].get("pitch", 0) <= 30
                else 1
            )
            for view_id in view_ids
        ], k=1,
    )[0]
    if combo:
        view = {**view, "angle": {**view["angle"], **(combo.get("angle") or {})}}
    combinations = [item for item in combinations if item[2]["id"] == view["id"]]
    if pose["posture"] in {"lying", "side_lying", "prone"} or view.get("elevation") in {"down", "top"}:
        face_mode = "none"
        include_face = False
    else:
        include_face = visible_face_requested
        if not include_face:
            face_mode = "none"
    if face_mode == "none" and framing == "full_outfit":
        framing = "leg_detail"
    scene_ids = list(dict.fromkeys(item[0]["id"] for item in combinations))
    scene = LEGFOCUS_SCENE_BY_ID[random.choice(scene_ids)]
    support = next(item[1] for item in combinations if item[0]["id"] == scene["id"])
    environment = str(scene.get("environment", "indoor"))
    capture_mode = str(view.get("capture_mode", "direct"))
    camera_mode = "mirror" if capture_mode == "mirror" else camera_kind
    framing_key = "mirror" if capture_mode == "mirror" else camera_kind
    pose_requests_shoulder_crop = _pose_requires_shoulder_crop(pose, framing_key)
    default_kneeling_crop = pose.get("posture") == "kneeling" and not explicit_face_or_full_request
    if pose_requests_shoulder_crop or default_kneeling_crop:
        if crop_mode != "shoulder_crop" and (
            pose_requests_shoulder_crop or combo or requested_crop_mode or force_crop_mode or force_framing
        ):
            prompt_fragment = " ".join((
                str(pose.get("prompt") or ""),
                str((pose.get("crop_prompts") or {}).get(framing_key) or ""),
            ))[:240]
            logging.getLogger(__name__).warning(
                "leg pose/crop conflict pose=%s crop_mode=%s prompt=%r",
                pose.get("id"), crop_mode, prompt_fragment,
            )
        crop_mode = "shoulder_crop"
        face_mode = "none"
        include_face = False
        framing = "leg_detail"
    scene_state = _weighted_scene_state_choice(str(scene["id"]), environment) if environment == "outdoor" else {}
    capture_style = _weighted_capture_style_choice(
        str(scene["id"]), camera_mode, environment, str(scene_state.get("id") or ""),
    )
    view_prompt = _clean_leg_view_prompt(str(view["prompt"]), view, crop_mode)
    view_title = str(view["title"])
    if allow_explicit_combination and not requested_view:
        declared_combinations = compatible_leg_compositions(
            pose,
            camera=camera_kind,
            scene_ids=str(scene["id"]),
            view_id=str(view["id"]),
            framing=framing,
            angles=selection_angles,
        )
        if not any(item[1] == support and item[2]["id"] == view["id"] for item in declared_combinations):
            view_prompt = "相机按用户指定的镜头角度取景，人物与实际支撑面保持自然连贯的透视关系。"
            view_title = "用户指定角度"
    if detail_id:
        pose_detail = forced_detail
    else:
        pose_detail = _weighted_pose_detail_choice(
            pose, camera=camera_kind, capture_mode=capture_mode,
            avoid_pose_details=avoid_pose_details,
        )

    requested_wear = str(force_legwear or "").strip()
    if requested_wear not in LEGWEAR_PROMPTS:
        requested_wear = parse_requested_legwear(raw_extra)
    unhandled_wear = _preserve_unhandled_legwear(raw_extra)
    if unhandled_wear and requested_wear == "用户指定":
        requested_wear = ""
    if not requested_wear and use_combo_wear and outfit_source == "pool":
        requested_wear = combo["wear"]
    if requested_wear and requested_wear in excluded_wears:
        raise LegFocusDataError("看看腿的袜装要求同时被排除，请只保留一个袜装条件")
    if requested_style and requested_style.get("wear") not in {requested_wear, ""}:
        requested_style = {}
    detail: Dict[str, Any] = {}
    style: Dict[str, Any] = {}
    if requested_wear:
        wear = requested_wear
        if wear in {"日常裤装", "用户指定"}:
            wear = "用户指定"
    elif outfit_source != "pool" or unhandled_wear:
        wear = "用户指定"
    else:
        allowed_categories = set(outfit.get("category") or LEGWEAR_CATEGORIES)
        detail = _weighted_detail_choice(
            excluded_wears,
            category=category_filter,
            allowed_categories=allowed_categories,
        )
        wear = str(detail.get("wear") or _DETAIL_TAGS.get(str(detail.get("id") or "")) or "用户指定")
    category = str(
        requested_style.get("category")
        or (combo["category"] if use_combo_wear and outfit_source == "pool" else "")
        or detail.get("category")
        or ("bare_legs" if wear in {"光腿", "光腿神器"} else "sheer" if wear in {"黑丝", "白丝", "连裤袜"} else "")
    )
    if outfit_source == "pool":
        style = requested_style or ({} if unhandled_wear else _pick_hosiery_style(
            wear, outfit, raw_extra, category=category, pose=pose,
            excluded_kinds=set(pose.get("disallowed_hosiery_kinds") or ()),
        ))
        if style and not _hosiery_allowed(style, raw_extra):
            raise LegFocusDataError("看看腿指定的袜装款式同时被排除，请调整袜装要求")
        if wear in {"黑丝", "白丝", "连裤袜"} and not style and not unhandled_wear:
            raise LegFocusDataError("看看腿没有符合本次要求的袜装款式，请放宽袜型限制")

    if outfit_source == "pool":
        profile = LEGFOCUS_OUTFIT_CAPTURE_PROFILES[outfit["capture_profile"]]
        outfit_line = str(outfit["prompt"]) + str(profile["coverage_prompt"])
        if unhandled_wear:
            outfit_line += f"袜装只按用户明确要求呈现：{unhandled_wear}；不额外随机其他袜装或赤足。"
        elif style:
            outfit_line += str(style["prompt"])
        elif requested_wear:
            outfit_line += LEGWEAR_PROMPTS[requested_wear]
        elif detail:
            outfit_line += str(detail["prompt"])
        explicit_accessory = any(
            _has_positive_term(raw_extra, str(term))
            for item in LEGFOCUS_ACCESSORIES for term in (item["id"], *item.get("aliases", ()))
        )
        accessory_wear = "光腿" if wear == "光腿神器" else wear
        if use_combo_wear and not explicit_accessory:
            defaults = list(combo["accessory_ids"])
            accessories = _pick_leg_accessory(accessory_wear, outfit, style, pose, force_ids=defaults) if defaults else []
            if defaults and not accessories:
                accessories = _pick_leg_accessory(accessory_wear, outfit, style, pose, raw_extra)
        else:
            accessories = _pick_leg_accessory(
                accessory_wear, outfit, style, pose, raw_extra,
                require_one=bool(combo and override_combo_wear),
            )
        if accessories:
            outfit_line += "".join(
                _leg_accessory_prompt(accessory, wear, style) for accessory in accessories
            )
            outfit_line = _remove_selected_accessory_blocks(outfit_line, accessories)
    else:
        accessories = []
        detail_override = bool(requested_wear or unhandled_wear or excluded_wears or LEGWEAR_NEGATION_PATTERN.search(raw_extra))
        preserve_line = LEGFOCUS_PRESERVE_PROMPTS[
            f"{outfit_source}_detail" if detail_override else outfit_source
        ]
        outfit_line = preserve_line
        if requested_wear:
            detail_prompt = str(requested_style.get("prompt") or LEGWEAR_PROMPTS[requested_wear])
            outfit_line += LEGFOCUS_PRESERVE_PROMPTS["requested_detail"].format(detail=detail_prompt)
        elif unhandled_wear:
            outfit_line += LEGFOCUS_PRESERVE_PROMPTS["requested_detail"].format(detail=unhandled_wear) + "。"
    if re.search(r"(?:不穿|不要|不需要|无需|不加|不用|不带)\s*(?:鞋履|鞋子|鞋|靴子|靴)", raw_extra):
        outfit_line += "不添加鞋履；袜装或赤足按本次已锁定的腿部细节执行。"

    # Each layer owns one constraint: crop, camera, action, setting, or clothing.
    pose_crop_prompt = (
        pose.get("crop_prompts", {}).get(framing_key)
        or pose.get("crop_prompts", {}).get(camera_kind)
        or next(iter(pose.get("crop_prompts", {}).values()), "")
    )
    framing_lines = [LEGFOCUS_CROP_MODES[crop_mode]]
    if framing == "full_outfit":
        framing_lines.append(LEGFOCUS_FULL_FRAMING)
    else:
        framing_lines.extend((
            LEGFOCUS_FRAMING,
            LEGFOCUS_CAMERA_FRAMINGS[framing_key],
            str(pose_crop_prompt),
        ))
        if _POOL.get("proportion_guard"):
            framing_lines.append(
                "即便画面只出现大腿到脚，也按完整身材比例绘制；不得为了填满画幅而延长腿部或缩小躯干。"
            )
    face_lines: List[str] = []
    expression_id = ""
    if not include_face:
        if crop_mode == "shoulder_crop":
            face_lines.append("头部与面部完全不进入画面，姿势与机位自然优先。")
        else:
            face_lines.append("头部与面部保持画外；取景上缘服从当前腿部主体构图，不额外强制停在肩线。")
    else:
        face_crop = (
            "完整面部清晰进入画面，额头、双眼、鼻部和下巴均自然可见；"
            if face_mode == "full"
            else "仅呈现自然侧脸或半脸，画面在脸部形成自然裁切；"
        )
        face_lines.append(
            face_crop + "脸部特征、发型、妆容与形象参考图一致，肤色与可见手部、腿部统一。"
        )
        try:
            from .cos_looks import _pick_scene_expression

            expression_id, expression_prompt = _pick_scene_expression(pose_id, raw_extra)
        except Exception:
            expression_prompt = ""
        if expression_prompt:
            face_lines.append("本次神态按 COS 表情模板执行：" + expression_prompt)
    skin_tone_line = (
        "人物皮肤保持其本人自然肤色，脸部、手部、腿部肤色一致；白色只属于袜料纤维，不覆盖皮肤。"
        if wear == "白丝" else
        "人物皮肤保持其本人自然肤色，脸部、手部、腿部肤色一致；袜装颜色只属于织物，不覆盖皮肤。"
        if wear in {"黑丝", "连裤袜"} else
        "人物皮肤保持其本人自然肤色，脸部、手部、腿部肤色一致。"
    )
    base = "\n".join(line for line in (
        LEGFOCUS_ANATOMY_RULE,
        *framing_lines,
        *face_lines,
        skin_tone_line,
        "" if category == "bare_legs" else LEGFOCUS_HOSIERY_RULE,
        _view_angle_prompt(view),
        f"本次机位（{view_title}）：{view_prompt}",
        str(pose["prompt"]),
        str(pose_detail.get("prompt") or ""),
        str(pose.get("hand_prompts", {}).get(capture_mode, "")),
        LEGFOCUS_SCENE_RULE,
        f"本张{'室外' if environment == 'outdoor' else '室内'}场景：{scene['prompt']}",
        f"本张姿势支撑：{scene['supports'][support]}。",
        *_pose_support_constraints(pose, support),
        f"本张状态（{scene_state['title']}）：{scene_state['prompt']}" if scene_state else "",
        f"本张拍摄质感：{capture_style['prompt']}",
        outfit_line,
    ) if line)
    extra = _clean_extra_request(raw_extra)
    if extra:
        base += f"\n用户补充要求优先：{extra}。"

    wear_tag = {
        "日常裤装": "daily",
        "用户指定": "custom",
        "白丝": "white",
        "黑丝": "black",
        "光腿": "bare",
        "光腿神器": "bare",
        "赤足": "barefoot",
        "连裤袜": "pantyhose",
    }.get(wear, "custom")
    return (
        base
        + f" 【legs:outfit】 【frame:{framing if framing == 'full_outfit' else 'leg_detail'}】"
        + (" 【legs_face:context】" if include_face else "")
        + f" 【face_mode:{face_mode}】"
        + f"【姿势池·{pose['title']}】【场景池·{scene['title']}】"
        + f" 【leg_view_title:{view_title}】 【leg_scene:{scene['id']}】 【leg_support:{support}】"
        + f" 【leg_capture_style:{capture_style['id']}】"
        + (f" 【leg_state:{scene_state['id']}】" if scene_state else "")
        + f" 【cam:{camera_kind}】 【wear:{wear_tag}】"
        + (f" 【pose_detail:{pose_detail['id']}】" if pose_detail else "")
        + (f" 【leg_expression:{expression_id}】" if expression_id else "")
        + f" 【outfit:{outfit.get('id') or outfit_source}】"
        + f" 【legwear_category:{category or 'custom'}】"
        + f" 【hosiery:{style.get('id') or 'none'}】"
        + " 【leg_accessory:"
        + (
            ",".join(
                f"{item['id']}|{item.get('bulk', 'thin')}|贴合" for item in accessories
            ) if accessories else "none"
        )
        + "】"
        + f" 【combo:{combo.get('id') or 'none'}】"
        + f" 【leg_view:{view['id']}】 【pose:{pose_id}】 【crop_mode:{crop_mode}】"
        + f" 【angle:pitch={view['angle']['pitch']},yaw={view['angle']['yaw']}】"
    )


def has_leg_focus_contract(text: str) -> bool:
    raw = str(text or "")
    if not (
        "【legs:outfit】" in raw
        and re.search(r"【cam:(?:selfie|third)】", raw)
        and re.search(r"【wear:(?:daily|custom|white|black|bare|barefoot|pantyhose)】", raw)
        and re.search(r"【frame:(?:full_outfit|leg_detail)】", raw)
        and re.search(r"【crop_mode:(?:leg_full|shoulder_crop|half_face|full_face)】", raw)
        and re.search(r"【leg_accessory:(?:none|[a-z_,|\u4e00-\u9fff]+)】", raw)
        and re.search(r"【pose:[a-z_]+】", raw)
        and re.search(r"【outfit:[a-z_]+】", raw)
        and re.search(r"【leg_view:[a-z_]+】", raw)
    ):
        return False
    pose_id = _resolve_pose_id(re.search(r"【pose:([a-z_]+)】", raw).group(1))
    if not pose_id:
        return False
    pose = LEGFOCUS_POSE_BY_ID[pose_id]
    crop_mode = re.search(r"【crop_mode:(leg_full|shoulder_crop|half_face|full_face)】", raw).group(1)
    frame = re.search(r"【frame:(full_outfit|leg_detail)】", raw).group(1)
    if frame == "full_outfit" and crop_mode == "shoulder_crop":
        return False
    if pose["posture"] == "kneeling" and crop_mode == "leg_full":
        return False
    detail_match = re.search(r"【pose_detail:([a-z_]+)】", raw)
    if detail_match:
        detail = next((
            item for item in pose.get("detail_variants", [])
            if str(item["id"]) == detail_match.group(1)
        ), {})
        if not detail:
            return False
    scene = re.search(r"【leg_scene:([a-z_]+)】", raw)
    support = re.search(r"【leg_support:([a-z_]+)】", raw)
    if not pose_id or not scene or not support:
        return False
    outfit_id = re.search(r"【outfit:([a-z_]+)】", raw).group(1)
    outfit = get_leg_focus_outfit(outfit_id)
    if not outfit and outfit_id not in {"reference", "custom"}:
        return False
    camera = re.search(r"【cam:(selfie|third)】", raw).group(1)
    view_id = re.search(r"【leg_view:([a-z_]+)】", raw).group(1)
    view = LEGFOCUS_VIEW_BY_ID.get(view_id, {})
    framing_key = "mirror" if view.get("capture_mode") == "mirror" else camera
    if _pose_requires_shoulder_crop(pose, framing_key) and crop_mode != "shoulder_crop":
        return False
    if detail_match and (
        camera not in detail.get("cameras", ("selfie", "third"))
        or view.get("capture_mode", "direct") not in detail.get("capture_modes", ("direct", "mirror"))
    ):
        return False
    combinations = compatible_leg_compositions(
        pose,
        camera=camera,
        view_id=view_id,
        scene_ids=scene.group(1),
        framing=re.search(r"【frame:(full_outfit|leg_detail)】", raw).group(1),
    )
    selected_support = support.group(1)
    strict_match = any(
        selected_support == support.group(1)
        and (not outfit or _leg_outfit_allows_composition(outfit, (scene_obj, selected_support, view)))
        for scene_obj, selected_support, view in combinations
    )
    if strict_match:
        return True
    return any(
        selected_support == support.group(1)
        and (not outfit or _leg_outfit_allows_composition(outfit, (scene_obj, selected_support, view)))
        for scene_obj, selected_support, view in compatible_leg_compositions(
            pose,
            camera=camera,
            view_id=view_id,
            scene_ids=scene.group(1),
            framing=re.search(r"【frame:(full_outfit|leg_detail)】", raw).group(1),
            allow_explicit=True,
        )
    )


def ensure_leg_focus_action(text: str, has_refs: bool = False) -> str:
    raw = str(text or "").strip()
    if not raw or has_leg_focus_contract(raw):
        return raw
    pose_match = re.search(r"【pose:([a-z_]+)】", raw)
    camera_match = re.search(r"【cam:(selfie|third)】", raw)
    wear_match = re.search(r"【wear:(daily|custom|white|black|bare|barefoot|pantyhose)】", raw)
    frame_match = re.search(r"【frame:(full_outfit|leg_detail)】", raw)
    scene_match = re.search(r"【leg_scene:([a-z_]+)】", raw)
    outfit_match = re.search(r"【outfit:([a-z_]+)】", raw)
    detail_match = re.search(r"【pose_detail:([a-z_]+)】", raw)
    outfit = get_leg_focus_outfit(outfit_match.group(1)) if outfit_match else {}
    extra = raw
    if "【legs:outfit】" in raw:
        supplement = re.search(r"用户补充要求优先[:：](.*?)(?=【legs:outfit】|$)", raw, re.S)
        extra = supplement.group(1).strip() if supplement else ""
    extra = re.sub(r"【(?:legs|legs_face|leg_view|leg_scene|leg_support|leg_capture_style|leg_state|face_mode|crop_mode|leg_expression|cam|wear|pose|pose_detail|outfit|frame|legwear_category|hosiery|leg_accessory):[a-z0-9_|,\u4e00-\u9fff]+】", " ", extra, flags=re.I)
    extra = re.sub(r"【leg_view_title:[^】]+】", " ", extra, flags=re.I)
    extra = re.sub(r"【(?:combo|angle):[^】]+】", " ", extra, flags=re.I)
    extra = re.sub(r"/?看看腿", " ", extra, flags=re.I)
    pose_id = _resolve_pose_id(str(pose_match.group(1) if pose_match else ""))
    force_detail = str(detail_match.group(1) if detail_match else "")
    if force_detail and force_detail not in {
        str(item["id"]) for item in LEGFOCUS_POSE_BY_ID.get(pose_id, {}).get("detail_variants", [])
    }:
        force_detail = ""
    camera = str(camera_match.group(1) if camera_match else "")
    # An obsolete generated camera tag must not override the current pose rules.
    if "【legs:outfit】" in raw and pose_id and camera:
        selected_scene = str(scene_match.group(1) if scene_match else "")
        selected_view = re.search(r"【leg_view:([a-z_]+)】", raw)
        if not compatible_leg_compositions(
            LEGFOCUS_POSE_BY_ID[pose_id], camera=camera, scene_ids=selected_scene,
            view_id=selected_view.group(1) if selected_view else "",
            framing=str(frame_match.group(1) if frame_match else "leg_detail"),
        ):
            camera = ""
    return build_leg_focus_action(
        extra,
        has_refs,
        force_pose=pose_id,
        force_pose_detail=force_detail,
        force_camera=camera,
        force_outfit=str(outfit.get("id") or ""),
        force_legwear=parse_requested_legwear(str(wear_match.group(0) if wear_match else "")),
        force_framing=str(frame_match.group(1) if frame_match else ""),
        preferred_scene=str(scene_match.group(1) if scene_match else ""),
    )


def validate_leg_focus_catalog() -> List[str]:
    """Return deep compatibility and prompt-contract issues for the leg pool.

    The loader validates individual records, while this pass validates the
    cross-product that random selection can actually traverse. It deliberately
    stays data-only so release checks can run without an AstrBot instance.
    """
    issues: List[str] = []

    def duplicate_prompt_issues(items: List[Dict[str, Any]], category: str) -> None:
        by_prompt: Dict[str, List[str]] = {}
        for item in items:
            prompt = re.sub(r"\s+", "", str(item.get("prompt") or "")).strip("。；;，,")
            item_id = str(item.get("id") or "<missing>")
            if prompt:
                by_prompt.setdefault(prompt, []).append(item_id)
        for prompt, item_ids in by_prompt.items():
            if len(item_ids) > 1:
                issues.append(f"{category}: duplicate prompt ids {','.join(item_ids[:8])}")

    def has_unnegated_term(text: str, term: str) -> bool:
        for match in re.finditer(re.escape(term), text):
            prefix = text[max(0, match.start() - 14):match.start()]
            if not re.search(
                r"(?:不|避免|禁止|不能|不得|不让|不画成|不变成|不呈现)"
                r"[^。；，,]{0,12}$",
                prefix,
            ):
                return True
        return False

    # A pose, every declared view, and every scene must have a reachable
    # support/camera path. This catches dead ends hidden by valid references.
    for pose in LEGFOCUS_POSE_POOL:
        pose_id = str(pose["id"])
        all_combinations = compatible_leg_compositions(pose)
        if not all_combinations:
            issues.append(f"pose {pose_id}: no compatible scene/camera/view path")
        for camera, view_ids in pose["camera_views"].items():
            camera_combinations = compatible_leg_compositions(pose, camera=camera)
            if not camera_combinations:
                issues.append(f"pose {pose_id}: camera {camera} has no compatible path")
            for view_id in view_ids:
                if not compatible_leg_compositions(pose, camera=camera, view_id=view_id):
                    issues.append(f"pose {pose_id}: view {view_id} has no compatible scene/support")

    for scene in LEGFOCUS_SCENE_POOL:
        if not any(
            compatible_leg_compositions(pose, scene_ids=scene["id"])
            for pose in LEGFOCUS_POSE_POOL
        ):
            issues.append(f"scene {scene['id']}: no compatible pose/camera/view path")

    for view in LEGFOCUS_VIEW_POOL:
        if not any(
            compatible_leg_compositions(pose, view_id=view["id"])
            for pose in LEGFOCUS_POSE_POOL
        ):
            issues.append(f"view {view['id']}: no compatible pose/scene/support path")

    # Every random wear and pose must retain at least one outfit/style/view
    # combination after capture and hosiery restrictions are applied.
    random_styles = [
        style for style in LEGFOCUS_HOSIERY_STYLES
        if style["kind"] == "tights" and not style.get("explicit_only", False)
    ]
    for pose in LEGFOCUS_POSE_POOL:
        combinations = compatible_leg_compositions(pose)
        for wear in ("黑丝", "白丝"):
            if not any(
                style["wear"] == wear
                and style["family"] in outfit["hosiery_families"]
                and style["kind"] not in set(pose.get("disallowed_hosiery_kinds") or ())
                and _leg_outfit_allows_composition(outfit, composition)
                for composition in combinations
                for outfit in LEGFOCUS_OUTFIT_POOL
                for style in random_styles
            ):
                issues.append(f"pose {pose['id']}: no random {wear} outfit/style/view path")

    all_compositions = [
        composition
        for pose in LEGFOCUS_POSE_POOL
        for composition in compatible_leg_compositions(pose)
    ]
    for outfit in LEGFOCUS_OUTFIT_POOL:
        outfit_id = str(outfit["id"])
        for environment in outfit.get("environments", ("indoor", "outdoor")):
            if not any(
                _leg_outfit_allows_composition(outfit, composition)
                for composition in all_compositions
                if str(composition[0].get("environment", "indoor")) == environment
            ):
                issues.append(f"outfit {outfit_id}: no compatible {environment} scene/view path")
        if re.search(r"(?:袜装|连裤袜|丝袜|过膝袜|赤足|裸足|光脚)", str(outfit["prompt"])):
            issues.append(f"outfit {outfit_id}: clothing prompt must not choose hosiery or bare feet")
        for wear in ("黑丝", "白丝"):
            if not any(
                style["wear"] == wear
                and style["kind"] == "tights"
                and not style.get("explicit_only", False)
                and style["family"] in outfit["hosiery_families"]
                for style in LEGFOCUS_HOSIERY_STYLES
            ):
                issues.append(f"outfit {outfit_id}: no random {wear} tights style")

    for profile_id, profile in LEGFOCUS_OUTFIT_CAPTURE_PROFILES.items():
        if not any(
            view["camera"] == "third"
            and view["direction"] in profile["third_directions"]
            and view["elevation"] in profile["third_elevations"]
            for view in LEGFOCUS_VIEW_POOL
        ):
            issues.append(f"capture profile {profile_id}: no usable third-person view")

    families = {str(style["family"]) for style in LEGFOCUS_HOSIERY_STYLES}
    referenced_families = {
        family for outfit in LEGFOCUS_OUTFIT_POOL for family in outfit["hosiery_families"]
    }
    for family in sorted(families - referenced_families):
        issues.append(f"hosiery family {family}: not referenced by any outfit")
    for style in LEGFOCUS_HOSIERY_STYLES:
        kind = str(style["kind"])
        prompt = str(style["prompt"])
        style_id = str(style["id"])
        if kind == "tights":
            if "连裤袜" not in prompt or not re.search(r"(?:纤维|织物|袜尖|袜跟|脚背)", prompt):
                issues.append(f"hosiery {style_id}: tights prompt lacks continuous fabric/foot detail")
            if any(has_unnegated_term(prompt, term) for term in ("赤足", "裸足", "光脚")):
                issues.append(f"hosiery {style_id}: tights prompt conflicts with bare-foot state")
        elif kind == "thigh_high" and not re.search(r"(?:过膝|长筒|丝袜)", prompt):
            issues.append(f"hosiery {style_id}: thigh-high prompt lacks length detail")
        elif kind == "knee_high" and not re.search(r"(?:膝下|膝部)", prompt):
            issues.append(f"hosiery {style_id}: knee-high prompt lacks length detail")
        elif kind == "fishnet" and "网袜" not in prompt:
            issues.append(f"hosiery {style_id}: fishnet prompt lacks mesh detail")
        elif kind == "stirrup" and not all(term in prompt for term in ("踩脚", "脚弓")):
            issues.append(f"hosiery {style_id}: stirrup prompt lacks foot-strap detail")
        elif kind == "ankle" and not re.search(r"(?:短袜|踝袜)", prompt):
            issues.append(f"hosiery {style_id}: ankle prompt lacks ankle-length detail")
        if style.get("category") not in LEGWEAR_CATEGORIES:
            issues.append(f"hosiery {style_id}: invalid random category")

    for detail in LEGFOCUS_RANDOM_LEG_DETAILS:
        wear = str(detail["wear"])
        prompt = str(detail["prompt"])
        detail_id = str(detail["id"])
        if wear == "光腿" and re.search(r"袜|织物|纤维|白丝|冷调|丝袜", prompt):
            issues.append(f"leg detail {detail_id}: bare-leg prompt contains hosiery language")
        if wear == "赤足" and not all(term in prompt for term in ("赤足", "不添加袜装", "鞋履")):
            issues.append(f"leg detail {detail_id}: barefoot prompt lacks sock/shoe/foot contract")
        if wear in {"黑丝", "白丝"} and any(
            has_unnegated_term(prompt, term) for term in ("赤足", "裸足", "光脚")
        ):
            issues.append(f"leg detail {detail_id}: hosiery detail conflicts with bare-foot state")
        if wear == "赤足" and not detail.get("explicit_only", False):
            issues.append(f"leg detail {detail_id}: barefoot detail must be explicit_only")

    framing_contracts = {
        "selfie": ("本人看向自己双腿", ("第三人称画外相机",)),
        "third": ("第三人称画外相机", ("本人看向自己双腿", "镜面中同一人的穿搭倒影")),
        "mirror": ("镜中同一人的腿部穿搭与姿势是视觉主体", ("第三人称画外相机", "本人看向自己双腿")),
    }
    for camera, (required, forbidden) in framing_contracts.items():
        prompt = str(LEGFOCUS_CAMERA_FRAMINGS.get(camera) or "")
        if required not in prompt:
            issues.append(f"camera framing {camera}: missing person contract")
        for term in forbidden:
            if term in prompt:
                issues.append(f"camera framing {camera}: conflicting person term {term}")

    duplicate_prompt_issues(LEGFOCUS_POSE_POOL, "poses")
    duplicate_prompt_issues(LEGFOCUS_SCENE_POOL, "scenes")
    duplicate_prompt_issues(LEGFOCUS_VIEW_POOL, "views")
    duplicate_prompt_issues(LEGFOCUS_OUTFIT_POOL, "outfits")
    duplicate_prompt_issues(LEGFOCUS_HOSIERY_STYLES, "hosiery")
    duplicate_prompt_issues(LEGFOCUS_RANDOM_LEG_DETAILS, "leg-details")
    return issues


_LEGFOCUS_CATALOG_ISSUES = validate_leg_focus_catalog()
if _LEGFOCUS_CATALOG_ISSUES:
    raise LegFocusDataError(
        "看看腿提示词池深检失败: " + "; ".join(_LEGFOCUS_CATALOG_ISSUES[:12])
    )
