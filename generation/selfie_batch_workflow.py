"""Prepare per-shot selfie actions before using the shared batch executor."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Tuple

from ..core.providers import ImageReference
from ..core.utils import event_user_id
from ..cos.cos_looks import keep_cos_outfit_requested
from ..cos.leg_focus import (
    has_leg_focus_contract,
    parse_requested_leg_camera,
    parse_requested_leg_pose,
    parse_requested_legwear,
)
from ..features.reference_collector import CosReferenceSelection
from ..prompts.prompt_templates import has_viewpoint_change_request


class SelfieBatchWorkflow:
    """Retain user choices while varying random framing across a single batch."""

    def __init__(
        self,
        plugin: Any,
        task_id: str,
        event: Any,
        action: str,
        extra_refs: List[ImageReference],
        source: str,
        requested_count: int,
        aspect: str,
        resolution: str,
        fail_label: str,
        rebuild_extra_request: str = "",
        rebuild_match_query: str = "",
        rebuild_special_preset_variants: Optional[List[str]] = None,
        cos_selection: Optional[CosReferenceSelection] = None,
        reference_selection: Optional[Mapping[str, Any]] = None,
        model_request: str = "",
    ) -> None:
        self.plugin = plugin
        self.task_id = task_id
        self.event = event
        self.action = action
        self.extra_refs = extra_refs
        self.source = source
        self.total = plugin._normalize_count(requested_count)
        self.aspect = aspect
        self.resolution = resolution
        self.fail_label = fail_label
        self.rebuild_extra_request = rebuild_extra_request
        self.rebuild_match_query = rebuild_match_query
        self.rebuild_special_preset_variants = rebuild_special_preset_variants
        self.cos_selection = cos_selection
        self.reference_selection = reference_selection
        self.model_request = model_request

    async def run(self) -> Dict[str, Any]:
        self._prepare_rebuild()
        self.round_actions = self._build_actions()
        return await self.plugin._run_counted_generation_shots(
            task_id=self.task_id,
            event=self.event,
            total=self.total,
            fail_label=self.fail_label,
            run_one=self._run_one,
            log_prefix="selfie batch",
        )

    def _prepare_rebuild(self) -> None:
        action, source = self.action, self.source
        self.rebuild_each = source in {
            "command-look-legs", "command-selfie", "command-look-you", "command-look-cos",
        } or (
            source == "command-group-selfie" and bool(self.rebuild_special_preset_variants)
        ) or (
            "看看腿" in str(action or "")
            or "【legs:outfit】" in str(action or "")
            or "看看COS" in str(action or "")
            or "【shot:" in str(action or "")
            or "【pose:" in str(action or "")
            or "【cos:" in str(action or "")
        )
        self.last_pose = ""
        self.last_shot = ""
        self.last_cos = ""
        self.last_cam = ""
        self.last_cos_pose = ""
        self.used_cos_poses: set[str] = set()
        self.used_cos_scenes: set[str] = set()
        self.used_cos_framings: set[str] = set()
        self.used_cos_compositions: set[str] = set()
        self.used_cos_cameras: set[str] = set()
        self.used_leg_combinations: set[Tuple[str, str]] = set()
        self.extra_keep = ""
        self.force_legwear = ""
        self.force_leg_pose = ""
        self.force_leg_camera = ""
        self.keep_cos_outfit = False
        if not self.rebuild_each:
            return
        # Keep the whole user supplement; punctuation inside preset text is
        # meaningful, while generated selection tags must not lock later shots.
        match = re.search(r"(?:用户补充要求优先|用户补充要求|用户指定内容|额外要求)[:：]\s*(.+)", str(action or ""), flags=re.S)
        if match:
            self.extra_keep = str(match.group(1) or "").strip()
            self.extra_keep = re.sub(r"\s*【(?:pose|shot|cos|cam|cos_mode|cos_profile|cos_eye|cos_pose|cos_scene|cos_style|cos_expression|cos_makeup|cos_framing|cos_composition|cos_view|legs|leg_view|leg_scene|leg_support|wear|outfit|frame):[a-z0-9_]+】\s*", " ", self.extra_keep)
            self.extra_keep = re.sub(r"\s*【(?:姿势池|场景池)·[^】]+】\s*", " ", self.extra_keep)
            self.extra_keep = re.sub(r"\s+", " ", self.extra_keep).strip(" 。")
        if self.rebuild_extra_request:
            self.extra_keep = str(self.rebuild_extra_request).strip()
        if source == "command-look-legs" or has_leg_focus_contract(action):
            self.force_legwear = parse_requested_legwear(self.extra_keep)
            self.force_leg_pose = parse_requested_leg_pose(self.extra_keep)
            self.force_leg_camera = parse_requested_leg_camera(self.extra_keep)
        pose = re.search(r"【pose:([a-z_]+)】", str(action or ""))
        if pose:
            self.last_pose = str(pose.group(1) or "")
        shot = re.search(r"【shot:([a-z_]+)】", str(action or ""))
        if shot:
            self.last_shot = str(shot.group(1) or "")
        cos = re.search(r"【cos:([a-z0-9_]+)】", str(action or ""))
        if cos:
            self.last_cos = str(cos.group(1) or "")
        camera = re.search(r"【cam:(selfie|first|third)】", str(action or ""))
        if camera:
            self.last_cam = str(camera.group(1) or "")
        cos_pose = re.search(r"【cos_pose:([a-z_]+)】", str(action or ""))
        if cos_pose:
            self.last_cos_pose = str(cos_pose.group(1) or "")
        self.keep_cos_outfit = source == "command-look-cos" and keep_cos_outfit_requested(self.extra_keep)

    def _build_actions(self) -> List[str]:
        actions = []
        for index in range(self.total):
            round_action = self.action
            if self.rebuild_each and self.total > 1:
                variant_extra = (
                    self.rebuild_special_preset_variants[index]
                    if self.rebuild_special_preset_variants and index < len(self.rebuild_special_preset_variants)
                    else self.extra_keep
                )
                round_action = self._rebuild_action(variant_extra)
            leg_pose = re.search(r"【pose:([a-z_]+)】", round_action)
            leg_view = re.search(r"【leg_view:([a-z_]+)】", round_action)
            if leg_pose and leg_view:
                self.used_leg_combinations.add((leg_pose.group(1), leg_view.group(1)))
            actions.append(round_action)
        return actions

    def _rebuild_action(self, variant_extra: str) -> str:
        plugin, action, source = self.plugin, self.action, self.source
        if source == "command-group-selfie" or (
            plugin._looks_like_group_selfie_intent(action) and not plugin._is_cos_action(action)
        ):
            round_action = plugin._build_group_selfie_action(
                variant_extra, bool(self.extra_refs), avoid_shot=self.last_shot,
            )
            shot = re.search(r"【shot:([a-z_]+)】", round_action)
            if shot:
                self.last_shot = str(shot.group(1) or self.last_shot)
        elif source == "command-look-legs" or "看看腿" in str(action or "") or "【legs:outfit】" in str(action or "") or "【pose:" in str(action or ""):
            round_action = plugin._build_leg_focus_action(
                variant_extra,
                bool(self.extra_refs),
                avoid_pose=self.last_pose,
                force_legwear=self.force_legwear,
                force_pose=self.force_leg_pose,
                force_camera=self.force_leg_camera,
                avoid_combinations=self.used_leg_combinations,
            )
            pose = re.search(r"【pose:([a-z_]+)】", round_action)
            if pose:
                self.last_pose = str(pose.group(1) or self.last_pose)
        elif source == "command-look-cos" or (source == "llm-generate-selfie" and plugin._is_cos_action(action)):
            round_action = plugin._build_cos_look_action(
                variant_extra,
                bool(self.extra_refs),
                avoid_id=self.last_cos,
                avoid_camera=self.last_cam,
                avoid_pose=self.last_cos_pose,
                avoid_poses=self.used_cos_poses,
                avoid_scenes=self.used_cos_scenes,
                avoid_framings=self.used_cos_framings,
                avoid_compositions=self.used_cos_compositions,
                avoid_cameras=self.used_cos_cameras,
                match_query=self.rebuild_match_query or self.extra_keep,
                force_id=self.last_cos if self.keep_cos_outfit else "",
                force_multi_view_collage="【cos_view:multi_view_collage】" in str(action or ""),
            )
            self._remember_cos_action(round_action)
        elif source == "command-look-you" or "看看你模式" in str(action or ""):
            round_action = plugin._build_third_person_look_action(
                variant_extra, bool(self.extra_refs), avoid_shot=self.last_shot,
            )
            shot = re.search(r"【shot:([a-z_]+)】", round_action)
            if shot:
                self.last_shot = str(shot.group(1) or self.last_shot)
        else:
            round_action = plugin._build_selfie_look_action(
                variant_extra, bool(self.extra_refs), avoid_shot=self.last_shot,
            )
            shot = re.search(r"【shot:([a-z_]+)】", round_action)
            if shot:
                self.last_shot = str(shot.group(1) or self.last_shot)
        return round_action

    def _remember_cos_action(self, action: str) -> None:
        cos = re.search(r"【cos:([a-z0-9_]+)】", action)
        if cos:
            self.last_cos = str(cos.group(1) or self.last_cos)
        camera = re.search(r"【cam:(selfie|first|third)】", action)
        if camera:
            self.last_cam = str(camera.group(1) or self.last_cam)
            self.used_cos_cameras.add(self.last_cam)
        pose = re.search(r"【cos_pose:([a-z_]+)】", action)
        if pose:
            self.last_cos_pose = str(pose.group(1) or self.last_cos_pose)
            self.used_cos_poses.add(self.last_cos_pose)
        scene = re.search(r"【cos_scene:([a-z_]+)】", action)
        if scene:
            self.used_cos_scenes.add(str(scene.group(1) or ""))
        framing = re.search(r"【cos_framing:([a-z0-9_]+)】", action)
        if framing:
            self.used_cos_framings.add(str(framing.group(1) or ""))
        composition = re.search(r"【cos_composition:([a-z0-9_]+)】", action)
        if composition:
            self.used_cos_compositions.add(str(composition.group(1) or ""))

    async def _run_one(self, index: int) -> Dict[str, Any]:
        plugin = self.plugin
        action = self.round_actions[index]
        if self.cos_selection is None:
            # Keep the legacy hook's positional signature when no COS selection
            # exists; older integrations provide that smaller call contract.
            prompt, refs, prompt_en_meta = await plugin._build_selfie_prompt_and_refs_for_event(
                self.event, action, self.extra_refs,
            )
        else:
            prompt, refs, prompt_en_meta = await plugin._build_selfie_prompt_and_refs_for_event(
                self.event, action, self.extra_refs, cos_selection=self.cos_selection,
            )
        reference_fidelity = "low" if refs and has_viewpoint_change_request(action) else ""
        return await plugin._run_image_generation(
            prompt,
            self.aspect,
            self.resolution,
            refs,
            targets=(
                plugin._prioritize_targets_by_model_request(plugin._resolve_generation_targets(self.event), self.model_request)
                if str(self.model_request or "").strip()
                else None
            ),
            source=self.source,
            audit_user_id=event_user_id(self.event),
            event=self.event,
            original_prompt=action,
            prompt_en_meta=prompt_en_meta,
            record_context={
                "task_id": self.task_id,
                "requested_count": self.total,
                "raw_reference_image_count": len(self.extra_refs),
                "reference_fidelity": reference_fidelity,
            },
            reference_selection=self.reference_selection or (self.cos_selection.summary() if self.cos_selection else None),
            reference_fidelity=reference_fidelity,
        )
