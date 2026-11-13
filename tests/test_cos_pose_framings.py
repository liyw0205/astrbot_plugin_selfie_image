import re
from unittest.mock import patch

from astrbot_plugin_selfie_image.cos import cos_looks
from astrbot_plugin_selfie_image.cos.cos_looks import (
    COS_COMBINATION_PRESETS,
    COS_FRAMING_CLASSES,
    COS_POSE_REQUEST_ALIASES,
    COS_POSE_FRAMING_COMPATIBILITY,
    COS_RANDOM_POSE_CLASSES,
    COS_RANDOM_SCENE_CLASSES,
    COS_RANDOM_COMPOSITION_CLASSES,
    COS_SCENE_FACE_RISK_TAGS,
    build_cos_look_action,
    build_cos_third_person_prompt,
    parse_requested_cos_camera,
    pick_cos_framing,
    pick_cos_variation,
    requests_cos_multi_view_collage,
)
from astrbot_plugin_selfie_image.cos.cos_looks_loader import (
    COS_DATA_DIR,
    LOOKS_DIR,
    COS_JSON_CAMERA_REQUEST_TERMS,
    COS_JSON_COMBINATIONS,
    COS_JSON_COMPOSITIONS,
    COS_JSON_FACE_RISK_PROMPTS,
    COS_JSON_EXPRESSIONS,
    COS_JSON_FRAMINGS,
    COS_JSON_FRAMING_OPTION_TERMS,
    COS_JSON_FRAMING_REQUEST_TERMS,
    COS_JSON_FRAMING_OPTION_SUFFIX_TERMS,
    COS_JSON_MAKEUPS,
    COS_JSON_MULTI_VIEW_COLLAGE,
    COS_JSON_MULTI_VIEW_COLLAGE_TERMS,
    COS_JSON_POSES,
    COS_JSON_POSE_CLASSIFICATION_TERMS,
    COS_JSON_POSE_CUSTOM_REQUEST_TERMS,
    COS_JSON_POSE_REQUEST_SYNTAX_TERMS,
    COS_JSON_LOOK_THEME_MARKERS,
    COS_JSON_SCENES,
    COS_JSON_SCENE_REQUEST_SYNTAX_TERMS,
    COS_JSON_STYLES,
    validate_json_catalog,
)


def test_pose_framing_compatibility_is_owned_by_json_catalog():
    assert validate_json_catalog() == []
    assert set(COS_POSE_FRAMING_COMPATIBILITY) == set(COS_RANDOM_POSE_CLASSES)
    for pose_id, pose in COS_JSON_POSES.items():
        framing_ids = pose["framing_ids"]
        assert tuple(framing_ids) == COS_POSE_FRAMING_COMPATIBILITY[pose_id]
        assert framing_ids
        assert set(framing_ids) <= COS_JSON_FRAMINGS.keys()


def test_close_framings_only_pair_with_compatible_pose_directions():
    compatible_poses = {
        "face_close_portrait": {
            "front_hand_near_face", "half_turn", "side_profile", "quiet_low_gaze",
        },
        "bust_close_portrait": {
            "front_sleeve_display", "front_hand_near_face", "quiet_low_gaze",
            "hands_gathered_at_sternum",
        },
        "side_profile_close": {"half_turn", "side_profile"},
        "rear_hair_close": {"over_shoulder"},
    }
    for framing_id, pose_ids in compatible_poses.items():
        framing = COS_JSON_FRAMINGS[framing_id]
        assert set(framing["random_camera_ids"]) <= set(framing["camera_ids"])
        for pose_id in pose_ids:
            assert framing_id in COS_JSON_POSES[pose_id]["framing_ids"]
            for camera in set(framing["random_camera_ids"]) & set(COS_JSON_POSES[pose_id]["random_camera_ids"]):
                selected = pick_cos_framing(
                    pose_id=pose_id,
                    camera=camera,
                    allowed_framings=[framing_id],
                    forced_framing_id=framing_id,
                    extra_request="背面发型近景" if framing_id == "rear_hair_close" else "",
                )
                assert selected["view_id"] == framing_id

    assert "face_close_portrait" not in COS_JSON_POSES["back_to_camera"]["framing_ids"]
    assert "rear_hair_close" not in COS_JSON_POSES["back_to_camera"]["framing_ids"]
    assert "rear_hair_close" not in COS_JSON_POSES["side_profile"]["framing_ids"]
    assert "rear_hair_close" in COS_JSON_POSES["over_shoulder"]["framing_ids"]
    assert "发型特写" in COS_JSON_FRAMING_REQUEST_TERMS
    assert "侧颜近景" in COS_JSON_FRAMING_OPTION_TERMS
    assert cos_looks.cos_prompt_has_framing("看看COS，面部特写")


def test_rear_hair_close_request_selects_side_back_pose_and_matching_crop():
    action = build_cos_look_action(
        "背面发型近景",
        force_id="roxy_cream",
        camera="third",
    )

    framing_prompt = COS_JSON_FRAMINGS["rear_hair_close"]["prompt"]
    assert "【cos_pose:over_shoulder】" in action
    assert "【cos_framing:rear_hair_close】" in action
    assert framing_prompt in action
    assert "人物以背后三分之四角度朝向镜头斜后方" in action
    assert "展示后背剪裁、腰胯曲线" not in action
    assert "本次构图优先保证支撑关系" not in action


def test_selfie_rear_hair_close_uses_side_back_head_crop_in_final_prompt(tmp_path):
    from astrbot_plugin_selfie_image.features.persona import PersonaManager
    from astrbot_plugin_selfie_image.prompts.selfie_actions import build_selfie_look_action

    action = build_selfie_look_action("背面发型近景", appearance_type="real")
    prompt = PersonaManager(str(tmp_path)).build_selfie_prompt(
        action,
        bot_name="",
        personality="",
        has_reference_image=False,
        extra_reference_count=0,
    )

    assert "【shot:back_three_quarter】" in action
    assert "背后三分之四侧后方头肩近景" in action
    assert "画面到肩胛上缘为止" in action
    assert "不回头、不回眸、不扭颈" in action
    assert "背后三分之四侧后方头肩近景" in prompt
    assert "头部到腰线入镜" not in prompt
    assert "镜头同时看到后背和一侧肩胯" not in prompt


def test_shared_cos_vocabularies_are_separate_from_look_records():
    shared_files = (
        "_scenes.json",
        "_poses.json",
        "_expressions.json",
        "_framings.json",
        "_styles.json",
        "_makeups.json",
        "_lightings.json",
        "_compositions.json",
        "_combinations.json",
        "_legacy_groups.json",
    )

    assert (LOOKS_DIR / "index.json").is_file()
    for filename in shared_files:
        assert (COS_DATA_DIR / filename).is_file()
        assert not (LOOKS_DIR / filename).exists()


def test_runtime_framing_copy_uses_catalog_titles_and_prompts():
    assert COS_RANDOM_POSE_CLASSES == COS_JSON_POSES
    assert COS_RANDOM_SCENE_CLASSES == COS_JSON_SCENES
    assert COS_FRAMING_CLASSES == COS_JSON_FRAMINGS
    for framing_id, framing in COS_JSON_FRAMINGS.items():
        assert COS_FRAMING_CLASSES[framing_id]["title"] == framing["title"]
        assert COS_FRAMING_CLASSES[framing_id]["prompt"] == framing["prompt"]


def test_shared_staging_metadata_and_curated_pairs_are_json_owned():
    assert COS_POSE_REQUEST_ALIASES == {
        pose_id: tuple(aliases)
        for pose_id, aliases in cos_looks.COS_JSON_POSE_REQUEST_ALIASES.items()
    }
    assert COS_RANDOM_COMPOSITION_CLASSES == COS_JSON_COMPOSITIONS
    assert COS_COMBINATION_PRESETS == COS_JSON_COMBINATIONS
    assert cos_looks.COS_LOOK_THEME_MARKERS == {
        theme_id: tuple(terms)
        for theme_id, terms in COS_JSON_LOOK_THEME_MARKERS.items()
    }
    assert set(COS_JSON_POSE_CLASSIFICATION_TERMS) == {
        "edge", "bend", "back", "side", "orientation_back", "orientation_side",
        "orientation_front", "orientation_strip",
    }
    assert COS_SCENE_FACE_RISK_TAGS == {
        scene_id: frozenset(scene["face_risk_tags"])
        for scene_id, scene in COS_JSON_SCENES.items()
        if scene.get("face_risk_tags")
    }
    assert cos_looks.COS_FACE_RISK_PROMPTS == COS_JSON_FACE_RISK_PROMPTS
    assert all(
        set(scene.get("face_risk_tags") or ()) <= COS_JSON_FACE_RISK_PROMPTS.keys()
        for scene in COS_JSON_SCENES.values()
    )
    used_poses = {
        pose_id
        for scene in COS_JSON_SCENES.values()
        for pose_id in scene["pose_ids"]
    }
    assert set(COS_JSON_POSES) - used_poses == {"veranda_side_profile"}
    assert COS_JSON_POSES["veranda_side_profile"]["explicit_only"] is True
    assert set(COS_JSON_MAKEUPS) == {
        makeup_id for scene in COS_JSON_SCENES.values() for makeup_id in scene["makeup_ids"]
    }
    for combo in COS_COMBINATION_PRESETS.values():
        assert combo["pose_id"] in COS_JSON_SCENES[combo["scene_id"]]["pose_ids"]
        assert pick_cos_framing(
            pose_id=combo["pose_id"],
            scene_id=combo["scene_id"],
            camera="third",
        )["prompt"]


def test_back_three_quarter_pose_does_not_request_a_lookback():
    pose = COS_JSON_POSES["over_shoulder"]

    assert pose["title"] == "背后三分之四展示"
    assert pose["framing_ids"] == ["rear_three_quarter", "rear_low_medium", "rear_hair_close"]
    assert "肩线、胯部与头部朝向保持一致" in pose["prompt"]
    assert "不回头、不回眸、不扭颈" in pose["prompt"]
    assert "不要求露出侧脸或正脸" in pose["prompt"]
    assert "body_curve" in pose["traits"]
    assert "rear_lookback" not in COS_JSON_FRAMINGS

    look = next(item for item in cos_looks.COS_LOOK_SETS if item["id"] == "roxy_cream")
    action = build_cos_look_action(
        "背身回眸",
        camera="third",
        has_refs=True,
        picker=lambda **_: look,
    )
    assert "【cos_pose:over_shoulder】" in action
    assert "【cos_framing:rear_lookback】" not in action
    assert "面部自然朝向画外" in action
    assert "不为了露脸回头、回眸或扭颈" in action


def test_cos_multi_view_collage_defaults_to_portrait_ratio():
    look = next(item for item in cos_looks.COS_LOOK_SETS if item["id"] == "roxy_cream")
    action = build_cos_look_action(
        "多视角拼贴",
        picker=lambda **_: look,
    )

    from astrbot_plugin_selfie_image.prompts.command_parser import extract_prompt_aspect_ratio

    assert "9:16竖幅画布" in action
    assert extract_prompt_aspect_ratio(action) == "9:16"
    assert "中央竖幅主画格（大图）" in action


def test_camera_and_request_vocabulary_is_json_owned():
    assert cos_looks.COS_SELFIE_REQUEST_TERMS == tuple(COS_JSON_CAMERA_REQUEST_TERMS["selfie"])
    assert cos_looks.COS_MULTI_VIEW_COLLAGE_TERMS == COS_JSON_MULTI_VIEW_COLLAGE_TERMS
    assert COS_JSON_CAMERA_REQUEST_TERMS["mirror_selfie"]
    assert COS_JSON_FRAMING_OPTION_SUFFIX_TERMS
    assert COS_JSON_POSE_CUSTOM_REQUEST_TERMS
    assert set(COS_JSON_POSE_REQUEST_SYNTAX_TERMS) == {"labels", "assignments"}
    assert set(COS_JSON_SCENE_REQUEST_SYNTAX_TERMS) == {
        "labels", "assignments", "freeform_leads", "freeform_locations",
    }
    assert cos_looks.COS_MULTI_VIEW_COLLAGE_ID == COS_JSON_MULTI_VIEW_COLLAGE["id"]
    assert cos_looks.COS_MULTI_VIEW_COLLAGE_PANEL_COUNT == COS_JSON_MULTI_VIEW_COLLAGE["panel_count"]
    assert len(cos_looks.COS_MULTI_VIEW_COLLAGE_LABELS) == COS_JSON_MULTI_VIEW_COLLAGE["panel_count"]
    assert COS_JSON_POSES["back_to_camera"]["random_weight"] == 5
    assert COS_JSON_POSES["over_shoulder"]["random_weight"] == 6

    assert parse_requested_cos_camera("第一人称视角") == "first"
    assert parse_requested_cos_camera("朋友帮忙拍摄") == "third"
    assert parse_requested_cos_camera("对镜自拍") == "selfie"
    assert requests_cos_multi_view_collage("多视角拼贴") is True
    assert cos_looks._requested_cos_pose("用户要求交叉双臂") == "user"
    assert cos_looks._requested_cos_pose("不要交叉双臂") == ""


def test_generic_cos_prompt_uses_shared_json_staging_vocabularies():
    with patch.object(cos_looks.random, "choice", side_effect=lambda values: values[0]):
        prompt = build_cos_third_person_prompt("COS 换装：穿蓝色礼服")

    pose_id = re.search(r"【cos_pose:([a-z0-9_]+)】", prompt).group(1)
    scene_id = re.search(r"【cos_scene:([a-z0-9_]+)】", prompt).group(1)
    style_id = re.search(r"【cos_style:([a-z0-9_]+)】", prompt).group(1)
    expression_id = re.search(r"【cos_expression:([a-z0-9_]+)】", prompt).group(1)
    makeup_id = re.search(r"【cos_makeup:([a-z0-9_]+)】", prompt).group(1)
    framing_id = re.search(r"【cos_framing:([a-z0-9_]+)】", prompt).group(1)

    assert COS_JSON_POSES[pose_id]["prompt"] in prompt
    assert COS_JSON_SCENES[scene_id]["prompt"] in prompt
    assert COS_JSON_STYLES[style_id]["prompt"] in prompt
    assert COS_JSON_EXPRESSIONS[expression_id]["prompt"] in prompt
    assert COS_JSON_MAKEUPS[makeup_id]["prompt"] in prompt
    assert COS_JSON_FRAMINGS[framing_id]["prompt"] in prompt


def test_generic_cos_back_curve_does_not_force_face_or_gaze_to_camera():
    prompt = build_cos_third_person_prompt("COS 换装，背身扶墙微弯，不看镜头")

    assert "【cos_pose:rear_wall_curve】" in prompt
    assert "可只见侧脸或不露脸" in prompt
    assert "不得为了露脸回头、扭颈或改变肩胯方向" in prompt
    assert "不看镜头" in prompt
    assert "目光稳定地看向镜头" not in prompt


def test_front_pose_variants_are_reachable_with_compatible_scene_and_framing():
    front_poses = (
        "front_seated_lean", "front_raised_arm", "front_waist_shift", "front_hand_near_face",
    )
    for pose_id in front_poses:
        pose = COS_JSON_POSES[pose_id]
        assert pose["orientation"] == "front"
        assert pose["random_weight"] > 0
        assert "third" in pose["random_camera_ids"]
        assert pose_id in COS_JSON_SCENES["photo_studio"]["pose_ids"]
        action = build_cos_look_action(
            "摄影棚 " + COS_POSE_REQUEST_ALIASES[pose_id][0] + " 慵懒诱惑",
            force_id="roxy_cream",
            camera="third",
        )
        assert f"【cos_pose:{pose_id}】" in action
        assert "【cos_scene:photo_studio】" in action
        assert "【cos_expression:sultry】" in action
        assert pose["prompt"] in action
        framing_id = re.search(r"【cos_framing:([a-z0-9_]+)】", action).group(1)
        assert framing_id in pose["framing_ids"]
        assert COS_JSON_FRAMINGS[framing_id]["prompt"] in action
    assert "side_profile" not in COS_JSON_POSES["kneeling_carpet"]["framing_ids"]


def test_short_video_reference_poses_survive_the_final_cos_prompt(tmp_path):
    from astrbot_plugin_selfie_image.features.persona import PersonaManager

    cases = (
        ("front_lean_v_sign", "正面前倾比V", "摄影棚"),
        ("kneeling_side_raised_arm", "侧向单膝跪姿抬臂", "摄影棚"),
        ("front_arms_raised_arch", "正面双臂抬起轻仰", "摄影棚"),
    )

    for pose_id, alias, scene_alias in cases:
        pose = COS_JSON_POSES[pose_id]
        assert pose["framing_ids"]
        assert pose_id in COS_JSON_SCENES["photo_studio"]["pose_ids"]
        action = build_cos_look_action(
            f"{scene_alias} {alias}",
            force_id="roxy_cream",
            camera="third",
        )

        assert f"【cos_pose:{pose_id}】" in action
        assert "【cos_scene:photo_studio】" in action
        assert action.count(pose["prompt"]) == 1
        framing_id = re.search(r"【cos_framing:([a-z0-9_]+)】", action).group(1)
        assert framing_id in pose["framing_ids"]
        assert COS_JSON_FRAMINGS[framing_id]["prompt"] in action
        if pose.get("face_visibility") == "optional":
            assert "面部可被手臂或发丝自然局部遮挡" in action
            assert "保持面部、发型、服装和配饰清晰可见" not in action

        final_prompt = PersonaManager(str(tmp_path)).build_selfie_prompt(
            action,
            bot_name="",
            personality="",
            has_reference_image=True,
            extra_reference_count=1,
        )
        assert final_prompt.count(pose["prompt"]) == 1
        if pose.get("face_visibility") == "optional":
            assert "姿势优先，面部可被手臂或发丝自然局部遮挡，不为露脸改变动作。" in final_prompt
            assert "除非用户要求遮脸，否则主角面部清晰自然" not in final_prompt


def test_reference_pose_views_are_close_and_camera_compatible():
    assert "high_angle_close_three_quarter" in COS_JSON_POSES["front_lean_v_sign"]["framing_ids"]
    assert "low_angle_close_three_quarter" in COS_JSON_POSES["kneeling_side_raised_arm"]["framing_ids"]
    assert "low_angle_close_three_quarter" in COS_JSON_POSES["front_arms_raised_arch"]["framing_ids"]

    for framing_id in (
        "high_angle_close_three_quarter",
        "low_angle_close_three_quarter",
    ):
        framing = COS_JSON_FRAMINGS[framing_id]
        assert framing["random_camera_ids"] == ["third", "first", "selfie"]
        assert "远景" in framing["prompt"]

    for pose_id in ("front_lean_v_sign", "kneeling_side_raised_arm", "front_arms_raised_arch"):
        for camera in ("third", "first", "selfie"):
            selected = pick_cos_framing(
                pose_id=pose_id,
                scene_id="photo_studio",
                camera=camera,
            )
            assert selected["view_id"] in COS_JSON_POSES[pose_id]["framing_ids"]
            assert camera in COS_JSON_FRAMINGS[selected["view_id"]]["random_camera_ids"]


def test_seductive_expressions_replace_old_entries_without_changing_head_direction():
    assert not {"shy", "mysterious"} & COS_JSON_EXPRESSIONS.keys()
    for expression_id in ("flirtatious", "sultry"):
        expression = COS_JSON_EXPRESSIONS[expression_id]
        assert "含蓄" not in expression["prompt"]
        assert "神秘" not in expression["prompt"]
        for pose_id in ("side_edge_bend", "rear_edge_curve", "over_shoulder"):
            selected_id, prompt = cos_looks._pick_scene_expression(pose_id, expression["label"])
            assert selected_id == expression_id
            assert prompt == expression["prompt"]
            assert not any(term in prompt for term in ("镜头", "回头", "侧脸", "扭颈", "歪头"))


def test_expression_catalog_has_one_schema_and_no_direction_controls():
    for expression in COS_JSON_EXPRESSIONS.values():
        assert set(expression) == {"label", "prompt", "aliases"}
        assert not any(term in expression["prompt"] for term in (
            "镜头", "回头", "扭颈", "歪头", "远处", "低垂", "肩颈", "头部",
        ))


def test_front_actions_use_multiple_independent_framings():
    assert "front_seated_medium" not in COS_JSON_FRAMINGS
    assert "front_raised_arm_medium" not in COS_JSON_FRAMINGS
    for pose_id in ("front_seated_lean", "front_raised_arm", "front_waist_shift", "front_hand_near_face"):
        framing_ids = COS_JSON_POSES[pose_id]["framing_ids"]
        assert "three_quarter_front" in framing_ids
        assert "high_angle_three_quarter" in framing_ids
        for framing_id in framing_ids:
            scene_ids = COS_JSON_FRAMINGS[framing_id].get("scene_ids") or ["photo_studio"]
            scene_id = next(
                scene_id for scene_id in scene_ids
                if pose_id in COS_JSON_SCENES[scene_id]["pose_ids"]
            )
            selected = pick_cos_framing(
                pose_id=pose_id, camera="third", scene_id=scene_id, forced_framing_id=framing_id,
            )
            assert selected["view_id"] == framing_id


def test_two_handed_pose_does_not_delegate_phone_compatibility_to_the_model():
    action = build_cos_look_action(
        "正面坐姿轻前倾 自拍 手机遮脸", force_id="roxy_cream", has_refs=True,
    )
    assert "【cos_pose:front_seated_lean】" in action
    assert "双手严格保持已选姿势" in action
    assert "不添加手持手机" in action
    assert "一只手握持唯一一部普通手机" not in action
    assert "若套装姿势" not in action


def test_side_pose_with_extra_reference_does_not_force_a_visible_face(tmp_path):
    from astrbot_plugin_selfie_image.features.persona import PersonaManager

    action = build_cos_look_action(
        "背身扶墙微弯", force_id="roxy_cream", camera="third", has_refs=True,
    )
    prompt = PersonaManager(str(tmp_path)).build_selfie_prompt(
        action, bot_name="", personality="", has_reference_image=True, extra_reference_count=1,
    )
    assert "【cos_pose:rear_wall_curve】" in prompt
    assert "若当前构图允许面部入镜" in prompt
    assert "除非用户要求遮脸，否则主角面部清晰自然" not in prompt


def test_group_prompt_opening_does_not_call_a_third_person_composition_a_selfie(tmp_path):
    from astrbot_plugin_selfie_image.features.persona import PersonaManager
    from astrbot_plugin_selfie_image.prompts.selfie_actions import build_group_selfie_action

    action = build_group_selfie_action("背面合影")
    prompt = PersonaManager(str(tmp_path)).build_selfie_prompt(
        action, bot_name="", personality="", has_reference_image=True, extra_reference_count=1,
    )
    assert prompt.startswith("这是合影照片。")


def test_explicit_style_selects_a_compatible_scene_style_from_json():
    with patch.object(cos_looks.random, "choice", side_effect=lambda values: values[0]):
        variation = pick_cos_variation("roxy_cream", extra_request="复古 DV", camera="third")

    assert variation["style_id"] == "retro_ccd_static"
    assert variation["style_id"] in COS_JSON_SCENES[variation["scene_id"]]["style_ids"]
    assert COS_JSON_STYLES[variation["style_id"]]["prompt"] in variation["prompt"]


def test_side_edge_bend_can_randomize_compatible_camera_views():
    framing_ids = COS_POSE_FRAMING_COMPATIBILITY["side_edge_bend"]
    selected = {
        pick_cos_framing(pose_id="side_edge_bend", camera="third")["view_id"]
        for _ in range(80)
    }

    assert len(selected) > 1
    assert selected <= set(framing_ids)


def test_curated_combination_keeps_scene_and_pose_but_not_a_fixed_framing():
    assert COS_COMBINATION_PRESETS
    assert all("framing_id" not in preset for preset in COS_COMBINATION_PRESETS.values())

    with patch.object(cos_looks.random, "random", return_value=0.0):
        variation = pick_cos_variation(
            "roxy_cream",
            camera="third",
            use_combinations=True,
        )

    assert variation["combination_id"] in COS_COMBINATION_PRESETS
    assert variation["scene_id"] == COS_COMBINATION_PRESETS[variation["combination_id"]]["scene_id"]
    assert variation["pose_id"] == COS_COMBINATION_PRESETS[variation["combination_id"]]["pose_id"]
    assert "framing_id" not in variation


def test_build_action_does_not_pass_combination_framing_as_forced_view():
    original_picker = cos_looks.pick_cos_framing
    with (
        patch.object(cos_looks.random, "random", return_value=0.0),
        patch.object(cos_looks.random, "choice", side_effect=lambda values: values[0]),
        patch.object(cos_looks, "pick_cos_framing", wraps=original_picker) as framing_picker,
    ):
        action = build_cos_look_action(
            force_id="roxy_cream",
            camera="third",
        )

    assert action
    assert framing_picker.call_args is not None
    assert "forced_framing_id" not in framing_picker.call_args.kwargs
    pose_id = re.search(r"【cos_pose:([a-z0-9_]+)】", action).group(1)
    framing_id = re.search(r"【cos_framing:([a-z0-9_]+)】", action).group(1)
    assert framing_id in COS_POSE_FRAMING_COMPATIBILITY[pose_id]


def test_cos_action_uses_json_expression_and_makeup_pools():
    action = build_cos_look_action(
        "温柔，自然妆",
        force_id="roxy_cream",
        camera="third",
    )

    expression_prompt = COS_JSON_EXPRESSIONS["gentle"]["prompt"]
    makeup_prompt = COS_JSON_MAKEUPS["natural_clean"]["prompt"]
    assert expression_prompt in action
    assert makeup_prompt in action
    assert "【cos_expression:gentle】" in action
    assert "【cos_makeup:natural_clean】" in action
