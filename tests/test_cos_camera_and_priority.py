import asyncio
import re
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from astrbot_plugin_selfie_image.cos.cos_looks import (
    COS_FRAMING_CLASSES,
    COS_EXPLICIT_ONLY_FRAMING_IDS,
    COS_LOOK_SETS,
    COS_RANDOM_POSE_CLASSES,
    COS_RANDOM_SCENE_CLASSES,
    COS_RANDOM_COMPOSITION_CLASSES,
    COS_POSE_CAMERA_COMPATIBILITY,
    COS_POSE_FRAMING_COMPATIBILITY,
    COS_FRAMING_SCENE_COMPATIBILITY,
    COS_FRAMING_CAMERA_COMPATIBILITY,
    audit_cos_look_catalog,
    build_cos_look_action,
    build_cos_third_person_prompt,
    cos_prompt_has_framing,
    cos_look_compatibility,
    match_cos_look_sets,
    normalize_cos_prompt_negatives,
    pick_cos_framing,
    pick_cos_camera,
    pick_cos_variation,
)
from astrbot_plugin_selfie_image.core.providers import ImageReference
from astrbot_plugin_selfie_image.features.reference_collector import (
    select_cos_references,
    select_group_references,
)
from astrbot_plugin_selfie_image.features.persona import (
    PersonaManager,
    appearance_type_instruction,
    group_style_lines,
)
from astrbot_plugin_selfie_image.prompts.selfie_actions import (
    build_group_selfie_action,
    build_selfie_look_action,
    build_third_person_look_action,
    looks_like_selfie_intent,
)
from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt
from astrbot_plugin_selfie_image.studio.studio import build_studio_action, empty_session


def test_default_cos_pool_actions_randomize_camera_and_keep_contracts_clean():
    for item in COS_LOOK_SETS:
        action = build_cos_look_action("", picker=lambda item=item, **_: item)
        camera = re.search(r"【cam:(selfie|first|third)】", action).group(1)
        assert camera in {"selfie", "first", "third"}
        assert re.search(r"【cos_pose:[a-z0-9_]+】", action)
        assert re.search(r"【cos_scene:[a-z0-9_]+】", action)
        assert re.search(r"【cos_framing:[a-z0-9_]+】", action)
        assert "本次随机场景优先于套装正文" in action
        assert "本次随机姿势优先于套装正文" in action
        if camera == "third":
            assert "【他拍 / 看看COS模式】" in action
            assert "自拍" not in action
            assert "镜前" not in action
            assert "镜子" not in action
            assert "对镜取景" not in action
            assert "两条手臂和两只手" in action
        elif camera == "first":
            assert "【第一视角 / 看看COS模式】" in action
            assert "手持设备" in action
            assert "自拍" not in action
        else:
            assert "【自拍 / 看看COS模式】" in action


def test_cos_catalog_audit_allows_costume_features_and_rejects_identity_locks():
    assert audit_cos_look_catalog(COS_LOOK_SETS) == []

    safe = [
        {
            "id": "safe-costume",
            "prompt": "银白色假发、蓝色瞳色、黑色眼罩和轻度角色妆容，服装细节清晰。",
        }
    ]
    assert audit_cos_look_catalog(safe) == []

    risky = [
        {
            "id": "risky-identity",
            "prompt": "固定脸型为瓜子脸，固定五官比例和固定肤色，严格按角色原画的脸生成。",
        }
    ]
    findings = audit_cos_look_catalog(risky)
    assert {finding["id"] for finding in findings} == {"risky-identity"}
    assert {finding["risk"] for finding in findings} == {
        "face_shape",
        "facial_proportions",
        "skin_tone",
        "character_face",
    }


def test_cos_negative_normalization_deduplicates_shared_semantics():
    prompt = (
        "严格换装为测试COS：真实布料。"
        "不要额外人物、文字、字幕和水印。"
        "禁止动漫插画、游戏立绘、3D渲染、塑料皮肤、超现实发光和水印。"
        "不要第二个人、可读文字、视频字幕或品牌水印。"
    )
    normalized = normalize_cos_prompt_negatives(prompt)

    assert normalized.count("额外人物") == 1
    assert normalized.count("文字") == 1
    assert normalized.count("字幕") == 1
    assert normalized.count("水印") == 1
    assert normalized.count("动漫插画") == 1
    assert normalized.count("游戏立绘") == 1
    assert normalized.count("3D渲染") == 1
    assert normalized.count("塑料皮肤") == 1
    assert normalized.count("超现实发光") == 1


def test_corrected_cos_identity_details_are_present_in_final_catalog():
    prompts = {item["id"]: item["prompt"] for item in COS_LOOK_SETS}
    assert "紫色眼睛" in prompts["azur_lane_enterprise"]
    assert "银白色长发假发" in prompts["azur_lane_belfast"]
    assert "紫色眼睛" in prompts["azur_lane_belfast"]
    assert "飞鸟马时" in prompts["blue_archive_toki"]
    assert "金色长发" in prompts["blue_archive_toki"]
    assert "黑色长发" not in prompts["blue_archive_toki"]
    assert "蔚蓝档案" in prompts["shiroko_black_white_tracksuit"]
    assert "蔚蓝档案" in prompts["ibuki_red_white_sportswear"]
    assert "白色长发" in prompts["blue_archive_hina"]
    assert "紫色眼睛" in prompts["blue_archive_hina"]
    assert "粉色长发" in prompts["blue_archive_aru"]
    assert "红色短发" in prompts["blue_archive_junko"]
    assert "黑色短发" in prompts["blue_archive_tsubaki"]
    assert "金色齐肩双马尾" in prompts["blue_archive_chinatsu"]
    assert "深棕色短发" in prompts["blue_archive_izuna"]
    assert "黑色及膝长假发" in prompts["baizhi_research_white_blue"]
    assert "白色长假发" in prompts["camellya_red_rose"]
    assert "银白色长卷发" in prompts["carlotta_frost_portrait"]
    assert "深棕色长假发" in prompts["wuthering_zhezhi_ink"]
    assert "深红色长发" in prompts["mushoku_tensei_aisha"]
    assert "绿色眼睛" in prompts["mushoku_tensei_aisha"]
    assert "洛可可" in prompts["wuthering_roccia_stage"]
    assert "釉瑚" in prompts["wuthering_youhu_antique"]


def test_sun_shangxiang_view_is_explicit_only_not_a_random_framing_option():
    sun = next(item for item in COS_LOOK_SETS if item["id"] == "sun_shangxiang_neon_green")
    assert "横屏低机位构图" not in sun["prompt"]
    assert "颈部以下拍到膝上" not in sun["prompt"]
    assert "头部自然位于画面外" not in sun["prompt"]
    framing = COS_FRAMING_CLASSES["low_angle_neck_to_knee"]
    assert framing["title"] == "横屏低机位颈下至膝上"
    assert "颈部以下拍到膝上" in framing["prompt"]
    assert "头部自然位于画面外" in framing["prompt"]
    assert "low_angle_neck_to_knee" in COS_EXPLICIT_ONLY_FRAMING_IDS
    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=lambda values: values[0],
    ):
        selected = pick_cos_framing(extra_request="COS 换装：自然站立")
    assert selected["view_id"] != "low_angle_neck_to_knee"
    explicit = pick_cos_framing(extra_request="横屏低机位颈下至膝上构图")
    assert explicit["explicit"]
    assert explicit["view_id"] == "user"


def test_cos_reference_selection_keeps_persona_first_without_identity_intent():
    persona = ImageReference(data=b"persona", mime_type="image/png")
    attached = ImageReference(data=b"attached", mime_type="image/png")

    selected = select_cos_references(persona, [attached], action="换成这套服装")

    assert selected.identity_source == "persona"
    assert selected.identity_ref is persona
    assert selected.refs == [persona, attached]
    assert selected.identity_reference_count == 1
    assert selected.extra_reference_image_count == 1


def test_look_reference_selection_uses_only_user_images_when_present():
    persona = ImageReference(data=b"persona", mime_type="image/png")
    persona_extra = ImageReference(data=b"persona-extra", mime_type="image/png")
    attached = ImageReference(data=b"attached", mime_type="image/png")
    quoted = ImageReference(data=b"quoted", mime_type="image/jpeg")

    selected = select_cos_references(
        persona,
        [attached, quoted],
        action="看看COS",
        persona_extra_refs=[persona_extra],
        prefer_message_identity=True,
    )
    fallback = select_cos_references(
        persona,
        [],
        action="看看COS",
        persona_extra_refs=[persona_extra],
        prefer_message_identity=True,
    )

    assert selected.identity_source == "message"
    assert selected.identity_ref is attached
    assert selected.refs == [attached, quoted]
    assert selected.summary()["used_persona"] is False
    assert selected.summary()["roles"] == {"message": 2}
    assert fallback.identity_source == "persona"
    assert fallback.refs == [persona, persona_extra]


def test_look_commands_share_reference_preference_but_other_cos_keeps_legacy_order():
    from astrbot_plugin_selfie_image.main import SelfieImagePlugin

    persona = ImageReference(data=b"persona", mime_type="image/png")
    persona_extra = ImageReference(data=b"persona-extra", mime_type="image/png")
    attached = ImageReference(data=b"attached", mime_type="image/png")

    with tempfile.TemporaryDirectory() as tmp:
        plugin = object.__new__(SelfieImagePlugin)
        plugin.persona = PersonaManager(tmp)
        plugin._persona_identity_reference = lambda **_: persona
        plugin._persona_auxiliary_references = lambda _action: [persona_extra]

        legs = plugin._select_selfie_references("看看腿", [attached])
        cos = plugin._select_selfie_references("看看COS 洛琪希", [attached])
        legacy_cos = plugin._select_selfie_references("COS换装", [attached])
        legs_without_image = plugin._select_selfie_references("看看腿", [])

    assert legs is not None and legs.refs == [attached]
    assert cos is not None and cos.refs == [attached]
    assert legacy_cos is not None and legacy_cos.refs == [persona, persona_extra, attached]
    assert legs_without_image is not None
    assert legs_without_image.refs == [persona, persona_extra]


def test_group_reference_selection_keeps_persona_identity_and_attached_image_as_extra():
    persona = ImageReference(data=b"persona", mime_type="image/jpeg")
    anime = ImageReference(data=b"anime", mime_type="image/png")

    selected = select_group_references(persona, [anime])

    assert selected.identity_source == "persona"
    assert selected.identity_ref is persona
    assert selected.refs == [persona, anime]
    assert selected.summary()["used_persona"] is True
    assert selected.summary()["roles"] == {"persona": 1, "message": 1}


def test_group_reference_selection_does_not_promote_attached_image_without_persona():
    anime = ImageReference(data=b"anime", mime_type="image/png")

    selected = select_group_references(None, [anime])

    assert selected.identity_source == "none"
    assert selected.identity_ref is None
    assert selected.refs == [anime]
    assert selected.identity_reference_count == 0



def test_auto_group_media_policy_never_lets_extra_reference_choose_the_main_medium():
    instruction = appearance_type_instruction("auto", has_reference_image=True)
    style_lines = group_style_lines("auto")

    assert "参考图一" in instruction
    assert "不得改变主角媒介" in instruction
    assert all("由模型根据主角形象与参考图自行判断" not in line for line in style_lines)
    assert any("主形象参考图" in line for line in style_lines)
    assert any("额外参考图" in line and "媒介" in line for line in style_lines)


def test_auto_english_group_prompt_uses_primary_reference_as_media_authority():
    prompt = build_selfie_builtin_prompt(
        "合影 / 合照 / 同框",
        language="en",
        has_reference_image=True,
        extra_reference_count=1,
        appearance_type="auto",
    )

    assert "primary identity reference" in prompt
    assert "must not override the primary subject's visual medium" in prompt
    assert "visually consistent" not in prompt


def test_cos_reference_selection_promotes_attached_face_when_explicitly_requested():
    persona = ImageReference(data=b"persona", mime_type="image/png")
    attached = ImageReference(data=b"attached", mime_type="image/png")

    selected = select_cos_references(
        persona,
        [attached],
        action="以附图人物脸为准，按这套服装生成",
    )

    assert selected.identity_source == "message"
    assert selected.identity_ref is attached
    assert selected.refs == [attached]
    assert selected.extra_reference_image_count == 0


def test_cos_reference_selection_uses_first_message_image_without_persona():
    first = ImageReference(data=b"first", mime_type="image/png")
    second = ImageReference(data=b"second", mime_type="image/png")

    selected = select_cos_references(None, [first, second], action="看看COS")

    assert selected.identity_source == "message"
    assert selected.identity_ref is first
    assert selected.refs == [first, second]
    assert selected.identity_reference_count == 1
    assert selected.extra_reference_image_count == 1


def test_cos_reference_selection_dry_run_matrix_has_stable_identity_and_prompt_contract():
    from astrbot_plugin_selfie_image.features.persona import PersonaManager
    from astrbot_plugin_selfie_image.prompts.prompt_templates import (
        COS_IDENTITY_CONTRACT_ZH,
        build_selfie_builtin_prompt,
    )

    persona = ImageReference(data=b"persona", mime_type="image/png")
    attached = ImageReference(data=b"attached", mime_type="image/png")
    duplicate = ImageReference(data=b"attached", mime_type="image/jpeg")
    action = build_cos_look_action(
        "",
        picker=lambda **_: {
            "id": "dry_run_cos",
            "title": "dry-run 测试套装",
            "prompt": "严格换装为测试 COS：银白色假发、蓝色眼妆、黑色眼罩，人物自然站立。",
        },
        camera="first",
    )

    cases = [
        ("persona", persona, [], [persona]),
        ("persona", persona, [attached], [persona, attached]),
        ("message", persona, [attached], [attached]),
        ("message", None, [attached, duplicate], [attached]),
        ("none", None, [], []),
    ]
    for source, persona_ref, message_refs, expected_refs in cases:
        selected = select_cos_references(
            persona_ref,
            message_refs,
            action="以附图人物脸为准" if source == "message" and persona_ref else action,
        )
        assert selected.identity_source == source
        assert selected.refs == expected_refs
        assert selected.summary()["identity_reference_source"] == source

        with tempfile.TemporaryDirectory() as tmp:
            prompt = PersonaManager(tmp).build_selfie_prompt(
                action,
                "小助",
                "温柔",
                bool(selected.identity_ref),
                selected.extra_reference_image_count,
            )
        if source == "none":
            assert COS_IDENTITY_CONTRACT_ZH not in prompt
        else:
            assert prompt.count(COS_IDENTITY_CONTRACT_ZH) == 1
        english = build_selfie_builtin_prompt(
            action,
            language="en",
            has_reference_image=bool(selected.identity_ref),
            extra_reference_count=selected.extra_reference_image_count,
            appearance_type="real",
        )
        assert english.count(action) == 1


def test_cos_reference_selection_does_not_use_logo_or_other_fallback_when_empty():
    selected = select_cos_references(None, [], action="看看COS")

    assert selected.identity_source == "none"
    assert selected.identity_ref is None
    assert selected.refs == []
    assert selected.identity_reference_count == 0
    assert selected.raw_total_count == 0


def test_cos_reference_selection_deduplicates_without_replacing_identity_first():
    attached = ImageReference(data=b"attached", mime_type="image/png")
    duplicate = ImageReference(data=b"attached", mime_type="image/jpeg")
    extra = ImageReference(data=b"extra", mime_type="image/png")

    selected = select_cos_references(None, [attached, duplicate, extra], action="看看COS")

    assert selected.identity_ref is attached
    assert selected.refs == [attached, extra]
    assert selected.raw_total_count == 3
    assert selected.deduplicated_total_count == 2
    assert selected.duplicate_count == 1


def test_cos_camera_parser_and_avoid_support_first_person():
    from astrbot_plugin_selfie_image.cos.cos_looks import parse_requested_cos_camera, pick_cos_camera

    assert parse_requested_cos_camera("第一视角") == "first"
    assert parse_requested_cos_camera("第一人称视角") == "first"
    assert parse_requested_cos_camera("自拍视角") == "selfie"
    assert pick_cos_camera(camera="first") == "first"
    assert pick_cos_camera(avoid="third") in {"first", "selfie"}


def test_studio_cos_prompt_uses_the_same_third_person_contract():
    session = empty_session("COS", template="clothes")
    session["graph"]["prompt"] = COS_LOOK_SETS[0]["prompt"]
    action = build_studio_action(session)
    assert "【cos:web】" in action
    assert "【cam:third】" in action
    assert "【他拍 / 看看COS模式】" in action
    assert "自拍" not in action
    assert "两条手臂和两只手" in action


def test_raw_web_cos_prompt_can_be_wrapped_without_selfie_semantics():
    action = build_cos_third_person_prompt("严格换装为角色 COS：人物在全身镜前自拍")
    assert "【cam:third】" in action
    assert "自拍" not in action
    assert "全身镜" not in action


def test_unspecified_cos_framing_picks_one_concrete_view():
    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=lambda values: values[0],
    ):
        selected = pick_cos_framing(extra_request="COS 换装：自然站立")
    assert selected["view_id"]
    assert selected["prompt"] in {item["prompt"] for item in COS_FRAMING_CLASSES.values()}
    assert not cos_prompt_has_framing("COS 换装：自然站立")
    assert cos_prompt_has_framing("COS 换装：竖屏三分之四侧身全身构图")
    assert pick_cos_framing(extra_request="COS 换装：竖屏三分之四侧身全身构图")["prompt"] == ""


def test_outfit_framing_words_do_not_suppress_random_view():
    def choose_framing(values):
        return "low_angle_hero" if "low_angle_hero" in values else values[0]

    with patch("astrbot_plugin_selfie_image.cos.cos_looks.random.random", return_value=0.25), patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=choose_framing,
    ):
        selected = pick_cos_framing(
            outfit="竖屏9:16，半身到全身构图，人物对镜站立",
            extra_request="COS 换装：自然站立",
        )
    assert selected["view_id"] == "low_angle_hero"
    assert selected["prompt"] == COS_FRAMING_CLASSES["low_angle_hero"]["prompt"]
    assert selected["outfit_text"] == "竖屏9:16，半身到全身构图，人物对镜站立"


def test_every_cos_look_uses_scene_pose_and_framing_compatibility():
    assert COS_RANDOM_POSE_CLASSES.keys() >= {"kneeling_carpet"}
    assert len(COS_RANDOM_POSE_CLASSES) >= 12
    assert len(COS_RANDOM_SCENE_CLASSES) >= 32
    assert COS_RANDOM_SCENE_CLASSES["bedroom_carpet"]["pose_ids"]
    assert "kneeling_carpet" in COS_RANDOM_SCENE_CLASSES["bedroom_carpet"]["pose_ids"]
    kneeling_scenes = {
        scene_id
        for scene_id, scene in COS_RANDOM_SCENE_CLASSES.items()
        if "kneeling_carpet" in scene["pose_ids"]
    }
    assert kneeling_scenes == {"bedroom_carpet", "living_room_rug"}
    for item in COS_LOOK_SETS:
        profile = cos_look_compatibility(item["id"])
        assert profile["variation_enabled"], item["id"]
        assert profile["scene_ids"] == list(COS_RANDOM_SCENE_CLASSES)
        assert profile["pose_ids"] == list(COS_RANDOM_POSE_CLASSES)
        assert profile["scene_pose_compatibility"] == {
            scene_id: list(dict.fromkeys([*scene["pose_ids"], "side_profile"]))
            for scene_id, scene in COS_RANDOM_SCENE_CLASSES.items()
        }
        assert profile["pose_framing_compatibility"] == {
            pose_id: list(framings)
            for pose_id, framings in COS_POSE_FRAMING_COMPATIBILITY.items()
        }
        assert profile["pose_camera_compatibility"] == {
            pose_id: list(cameras)
            for pose_id, cameras in COS_POSE_CAMERA_COMPATIBILITY.items()
        }
        assert profile["framing_scene_compatibility"] == {
            framing_id: list(scenes)
            for framing_id, scenes in COS_FRAMING_SCENE_COMPATIBILITY.items()
        }
        assert profile["framing_camera_compatibility"] == {
            framing_id: list(cameras)
            for framing_id, cameras in COS_FRAMING_CAMERA_COMPATIBILITY.items()
        }
        assert set(profile["composition_constraints"]) == set(COS_RANDOM_COMPOSITION_CLASSES)
    for scene in COS_RANDOM_SCENE_CLASSES.values():
        assert scene["pose_ids"]
        assert set(scene["pose_ids"]) <= COS_RANDOM_POSE_CLASSES.keys()

    assert "one_knee_crouch" not in COS_RANDOM_SCENE_CLASSES["rainy_neon_street"]["pose_ids"]
    assert "one_knee_crouch" not in COS_RANDOM_SCENE_CLASSES["snowy_courtyard"]["pose_ids"]
    assert "low_angle_neck_to_knee" in COS_EXPLICIT_ONLY_FRAMING_IDS

    expected_back_scenes = {
        "garden_walkway", "bamboo_garden", "cherry_blossom_path",
        "seaside_boardwalk", "rooftop_sunset", "old_town_lane",
        "rainy_neon_street", "festival_lantern", "stone_staircase",
        "snowy_courtyard", "old_city_rooftop", "aquarium_tunnel",
        "greenhouse_storm", "skybridge_after_rain", "last_train_platform",
        "theater_stage_wing", "planetarium_dome", "sea_cliff_lighthouse",
        "spiral_archive", "red_rock_canyon", "cinema_projection_room",
        "snow_observation_corridor", "modern_art_gallery",
    }
    actual_back_scenes = {
        scene_id
        for scene_id, compatible_poses in profile["scene_pose_compatibility"].items()
        if "back_to_camera" in compatible_poses
    }
    assert actual_back_scenes == expected_back_scenes
    assert set(COS_POSE_FRAMING_COMPATIBILITY) == set(COS_RANDOM_POSE_CLASSES)
    for pose_id, framing_ids in COS_POSE_FRAMING_COMPATIBILITY.items():
        assert framing_ids
        assert set(framing_ids) <= COS_FRAMING_CLASSES.keys(), pose_id

    new_scene_ids = {
        "aquarium_tunnel", "greenhouse_storm", "skybridge_after_rain",
        "last_train_platform", "theater_stage_wing", "planetarium_dome",
        "sea_cliff_lighthouse", "spiral_archive", "red_rock_canyon",
        "cinema_projection_room", "snow_observation_corridor",
        "modern_art_gallery",
    }
    for scene_id in new_scene_ids:
        scene = COS_RANDOM_SCENE_CLASSES[scene_id]
        assert scene["style_prompt"]
        assert scene["pose_ids"]
        assert set(scene["pose_ids"]) <= COS_RANDOM_POSE_CLASSES.keys()

    for _ in range(100):
        variation = pick_cos_variation("roxy_cream")
        assert variation["pose_id"] in profile["scene_pose_compatibility"][variation["scene_id"]]
        framing = pick_cos_framing(pose_id=variation["pose_id"])
        assert framing["view_id"] in profile["pose_framing_compatibility"][variation["pose_id"]]
        camera = pick_cos_camera(pose_id=variation["pose_id"])
        assert camera in profile["pose_camera_compatibility"].get(
            variation["pose_id"], ("third", "first", "selfie")
        )

    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=lambda values: values[0],
    ):
        selected = pick_cos_variation("roxy_cream", extra_request="跪坐")
    assert selected["pose_id"] == "kneeling_carpet"
    assert selected["scene_id"] in {
        "bedroom_carpet", "living_room_rug"
    }
    assert "随机场景" in selected["prompt"]
    assert "随机姿势" not in selected["prompt"]


def test_pose_and_scene_detection_preserves_explicit_user_staging():
    kneeling = pick_cos_variation("roxy_cream", extra_request="跪坐在地毯上")
    assert kneeling["pose_id"] == "kneeling_carpet"
    assert kneeling["scene_id"] == "user"
    assert "随机场景" not in kneeling["prompt"]
    assert "随机姿势" not in kneeling["prompt"]

    ordinary = pick_cos_variation("roxy_cream", extra_request="在镜头前保持自然，服装细节清晰")
    assert ordinary["scene_id"] != "user"
    assert "随机场景" in ordinary["prompt"]

    custom_scene = pick_cos_variation("roxy_cream", extra_request="夜景")
    assert custom_scene["scene_id"] == "user"
    assert "随机场景" not in custom_scene["prompt"]

    custom_pose = pick_cos_variation("roxy_cream", extra_request="双手托脸")
    assert custom_pose["pose_id"] == "user"
    assert "随机姿势" not in custom_pose["prompt"]

    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=lambda values: values[0],
    ):
        negative_orientation = pick_cos_variation("roxy_cream", extra_request="不要背对镜头")
        negative_scene = pick_cos_variation("roxy_cream", extra_request="不要在天台拍")
    assert negative_orientation["pose_id"] != "back_to_camera"
    assert negative_scene["scene_id"] != "old_city_rooftop"

    requested_back = pick_cos_variation("roxy_cream", extra_request="背影")
    assert requested_back["pose_id"] == "back_to_camera"
    assert requested_back["scene_id"] in {
        "garden_walkway", "bamboo_garden", "cherry_blossom_path",
        "seaside_boardwalk", "rooftop_sunset", "old_town_lane",
        "rainy_neon_street", "festival_lantern", "stone_staircase",
        "snowy_courtyard", "old_city_rooftop", "aquarium_tunnel",
        "greenhouse_storm", "skybridge_after_rain", "last_train_platform",
        "theater_stage_wing", "planetarium_dome", "sea_cliff_lighthouse",
        "spiral_archive", "red_rock_canyon", "cinema_projection_room",
        "snow_observation_corridor", "modern_art_gallery",
    }


def test_random_scene_is_uniformly_selected_before_compatible_pose():
    def choose_final_scene_and_back_pose(values):
        if "back_to_camera" in values:
            return "back_to_camera"
        return values[-1]

    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=choose_final_scene_and_back_pose,
    ):
        selected = pick_cos_variation("custom_cos_id")
    final_scene_id = list(COS_RANDOM_SCENE_CLASSES)[-1]
    assert selected["scene_id"] == final_scene_id
    assert selected["pose_id"] == "back_to_camera"
    assert selected["pose_id"] != "over_shoulder"


def test_back_and_side_cos_staging_never_randomly_forces_a_lookback():
    assert COS_RANDOM_POSE_CLASSES["over_shoulder"]["explicit_only"] is True
    assert "over_shoulder" not in cos_look_compatibility("test")["recommended_pose_ids"]
    def choose_back_pose(values):
        if "back_to_camera" in values:
            return "back_to_camera"
        return values[-1]

    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=choose_back_pose,
    ):
        selected = pick_cos_variation("test")
    assert selected["pose_id"] == "back_to_camera"
    assert "不回头、不回眸、不扭转颈部" in selected["prompt"]

    back_framing = pick_cos_framing(pose_id="back_to_camera")
    assert back_framing["view_id"] in {
        "rear_three_quarter", "rear_low_medium", "rear_low_hero", "environmental", "eye_level_full",
    }
    side_framing = pick_cos_framing(pose_id="side_profile")
    assert side_framing["view_id"] in {"side_profile", "environmental", "eye_level_full", "low_angle_full", "low_angle_hero"}

    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=lambda values: values[-1],
    ):
        exhausted_back_framing = pick_cos_framing(
            pose_id="back_to_camera",
            avoid_framings=COS_FRAMING_CLASSES,
        )
    assert exhausted_back_framing["view_id"] != "rear_lookback"

    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        return_value="rear_lookback",
    ):
        requested_lookback = pick_cos_framing(pose_id="over_shoulder")
    assert requested_lookback["view_id"] == "rear_lookback"

    assert pick_cos_camera(pose_id="over_shoulder") == "third"
    assert pick_cos_camera(camera="selfie", pose_id="over_shoulder") == "selfie"
    assert pick_cos_camera(pose_id="back_to_camera") in {"third", "first"}
    assert pick_cos_camera(camera="selfie", pose_id="back_to_camera") == "selfie"


def test_back_view_can_use_a_controlled_low_angle_only_in_third_person():
    def choose_low_rear_framing(values):
        return "rear_low_hero" if "rear_low_hero" in values else values[0]

    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=choose_low_rear_framing,
    ):
        selected = pick_cos_framing(pose_id="back_to_camera", camera="third")

    assert selected["view_id"] == "rear_low_hero"
    assert "轻微仰拍" in selected["prompt"]
    assert "肩线、胯部和头部朝向一致" in selected["prompt"]
    assert "不回头、不回眸、不扭转颈部" in selected["prompt"]
    assert "不使用超广角" in selected["prompt"]

    for _ in range(20):
        first_person = pick_cos_framing(pose_id="back_to_camera", camera="first")
        assert first_person["view_id"] in {
            "rear_three_quarter", "rear_low_medium", "eye_level_full",
        }
        assert first_person["view_id"] != "rear_low_hero"
        assert first_person["view_id"] != "environmental"


def test_first_person_walking_pose_uses_observer_wording():
    def choose_walking_pose(values):
        return "walking_turn" if "walking_turn" in values else values[0]

    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=choose_walking_pose,
    ):
        selected = pick_cos_variation(
            "test",
            extra_request="第一视角 老旧楼群天台",
            camera="first",
        )

    assert selected["pose_id"] == "walking_turn"
    assert "主角自然经过观察者的视线前方" in selected["prompt"]
    assert "身体自然侧向镜头" not in selected["prompt"]


def test_face_cropping_framing_is_explicit_only():
    random_framing = pick_cos_framing(pose_id="standing_detail")
    assert random_framing["view_id"] != "low_angle_neck_to_knee"
    assert random_framing["view_id"] != "low_angle_full_2"

    explicit_framing = pick_cos_framing(
        extra_request="颈下至膝上构图",
        pose_id="standing_detail",
    )
    assert explicit_framing["explicit"]
    assert explicit_framing["view_id"] == "user"


def test_random_cos_composition_respects_pose_scene_and_camera_constraints():
    profile = cos_look_compatibility("test")
    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=lambda values: values[0],
    ):
        for scene_id, pose_ids in profile["scene_pose_compatibility"].items():
            for pose_id in pose_ids:
                if COS_RANDOM_POSE_CLASSES[pose_id].get("explicit_only"):
                    continue
                for camera in ("third", "first", "selfie"):
                    selected = pick_cos_framing(
                        pose_id=pose_id,
                        scene_id=scene_id,
                        camera=camera,
                    )
                    view_id = selected["view_id"]
                    if view_id not in {"unspecified", "user"}:
                        assert view_id in COS_POSE_FRAMING_COMPATIBILITY[pose_id]
                        assert scene_id in COS_FRAMING_SCENE_COMPATIBILITY.get(view_id, (scene_id,))
                        assert camera in COS_FRAMING_CAMERA_COMPATIBILITY.get(view_id, (camera,))
                    composition = selected.get("composition") or {}
                    if composition:
                        constraints = profile["composition_constraints"][composition["composition_id"]]
                        assert camera in constraints["cameras"]
                        assert not constraints.get("poses") or pose_id in constraints["poses"]
                        assert not constraints.get("scenes") or scene_id in constraints["scenes"]

    unknown_pose = pick_cos_framing(pose_id="user", scene_id="photo_studio", camera="third")
    assert unknown_pose["view_id"] == "unspecified"
    assert unknown_pose["composition"] == {}


def test_explicit_cos_camera_filters_only_random_pose_choices():
    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=lambda values: values[0],
    ):
        selfie_variation = pick_cos_variation("roxy_cream", camera="selfie")
        selfie_action = build_cos_look_action(
            "自拍",
            camera="selfie",
            picker=lambda **_: next(item for item in COS_LOOK_SETS if item["id"] == "roxy_cream"),
        )

    assert selfie_variation["pose_id"] not in {"back_to_camera", "over_shoulder"}
    assert selfie_variation["pose_id"] in cos_look_compatibility("test")["scene_pose_compatibility"][selfie_variation["scene_id"]]
    assert "【cam:selfie】" in selfie_action
    assert "【cos_pose:back_to_camera】" not in selfie_action

    explicit_back = pick_cos_variation("roxy_cream", extra_request="背影", camera="selfie")
    assert explicit_back["pose_id"] == "back_to_camera"


def test_random_cos_action_appends_an_independent_compatible_composition():
    item = next(item for item in COS_LOOK_SETS if item["id"] == "roxy_cream")
    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=lambda values: values[0],
    ):
        action = build_cos_look_action(
            "自然站立",
            camera="third",
            picker=lambda **_: item,
        )
    match = re.search(r"【cos_composition:([a-z0-9_]+)】", action)
    assert match
    composition = COS_RANDOM_COMPOSITION_CLASSES[match.group(1)]
    assert "附加构图强化：" + composition["prompt"] in action


def test_composition_avoidance_keeps_only_compatible_candidates():
    from astrbot_plugin_selfie_image.cos.cos_looks import _pick_cos_composition

    first = _pick_cos_composition(
        pose_id="standing_detail",
        scene_id="old_town_lane",
        camera="third",
    )
    second = _pick_cos_composition(
        pose_id="standing_detail",
        scene_id="old_town_lane",
        camera="third",
        avoid={first["composition_id"]},
    )
    assert second["composition_id"] != first["composition_id"]
    constraints = cos_look_compatibility("test")["composition_constraints"][second["composition_id"]]
    assert "third" in constraints["cameras"]
    assert "old_town_lane" in constraints.get("scenes", ("old_town_lane",))
    assert "standing_detail" in constraints.get("poses", ("standing_detail",))

    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=lambda values: values[-1],
    ):
        exhausted = _pick_cos_composition(
            pose_id="standing_detail",
            scene_id="old_town_lane",
            camera="third",
            avoid=COS_RANDOM_COMPOSITION_CLASSES,
        )
    assert exhausted["composition_id"] in {
        "asymmetric_negative_space",
        "telephoto_compression",
        "architectural_frame",
        "architectural_leading_lines",
        "layered_foreground",
    }


def test_new_scene_uses_its_bound_style_unless_the_user_supplies_one():
    scene = COS_RANDOM_SCENE_CLASSES["aquarium_tunnel"]
    selected = pick_cos_variation(
        "custom_cos_id",
        extra_request="水族馆隧道",
    )
    assert selected["scene_id"] == "aquarium_tunnel"
    assert scene["prompt"] in selected["prompt"]
    assert scene["style_prompt"] in selected["prompt"]

    explicit_style = pick_cos_variation(
        "custom_cos_id",
        extra_request="水族馆隧道，使用胶片质感",
    )
    assert explicit_style["scene_id"] == "aquarium_tunnel"
    assert scene["style_prompt"] not in explicit_style["prompt"]


def test_explicit_back_view_adapts_across_selfie_group_and_cos_routes():
    selfie = build_selfie_look_action("背对镜头，卧室")
    third_person = build_third_person_look_action("背对镜头，卧室")
    group = build_group_selfie_action("背对镜头合影")
    assert "【shot:back_view】" in selfie
    assert "定时拍摄" in selfie
    assert "不回头、不回眸、不扭颈" in selfie
    assert "【shot:back_view】" in third_person
    assert "【shot:back_view】" in group
    assert "天台" not in selfie + third_person + group
    assert "场景倾向：" not in selfie
    assert "不增删人物" in group

    with tempfile.TemporaryDirectory() as tmp:
        persona = PersonaManager(tmp)
        selfie_prompt = persona.build_selfie_prompt(selfie, "小助", "温柔", False, 0)
        group_prompt = persona.build_selfie_prompt(group, "小助", "温柔", False, 0)
    assert "不要求露出正脸" in selfie_prompt
    assert "不强迫回头" in group_prompt
    assert "本次合影采用背面构图" in group_prompt
    assert "天台背面构图" not in group_prompt


def test_group_random_staging_uses_environment_when_selected():
    with patch(
        "astrbot_plugin_selfie_image.prompts.selfie_actions.random.choices",
        return_value=["group_environment"],
    ):
        action = build_group_selfie_action("和我合影")

    assert "【shot:group_environment】" in action
    assert "中远景环境合影构图" in action
    assert "所有人保持中远景可辨识" in action
    assert "背面构图" not in action
    assert "竖屏手机近景半身" not in action


def test_group_random_staging_can_choose_a_back_view():
    with patch(
        "astrbot_plugin_selfie_image.prompts.selfie_actions.random.choices",
        return_value=["back_view"],
    ):
        action = build_group_selfie_action("和我合影")

    assert "【shot:back_view】" in action
    assert "本次合影采用背面构图" in action
    assert "竖屏中远景" in action
    assert "不强迫回头" in action


def test_selfie_scene_and_light_overrides_are_independent():
    provider = lambda _kind: (["随机咖啡馆"], ["随机暖灯"])
    bedroom = build_selfie_look_action(
        "在卧室拍摄",
        scene_light_provider=provider,
    )
    cold_light = build_third_person_look_action(
        "冷光",
        scene_light_provider=provider,
    )
    group = build_group_selfie_action("冷光合影")

    assert "场景倾向：" not in bedroom
    assert "光线：随机暖灯" in bedroom
    assert "场景倾向：随机咖啡馆" in cold_light
    assert "光线：" not in cold_light
    assert "窗光或暖灯" not in cold_light
    assert "窗光或暖灯" not in group

    with patch(
        "astrbot_plugin_selfie_image.prompts.selfie_actions.random.choices",
        return_value=["arm_half"],
    ):
        negative_scene = build_selfie_look_action(
            "不要在卧室拍",
            scene_light_provider=provider,
        )
    assert "场景倾向：随机咖啡馆" in negative_scene


def test_default_selfies_get_one_candid_finish_and_full_mirror_shows_the_outfit():
    with (
        patch(
            "astrbot_plugin_selfie_image.prompts.selfie_actions.random.choices",
            return_value=["arm_half"],
        ),
        patch(
            "astrbot_plugin_selfie_image.prompts.selfie_actions.random.choice",
            side_effect=lambda items: items[0],
        ),
    ):
        default_selfie = build_selfie_look_action()
        explicit_selfie = build_selfie_look_action("窗边自拍，轻微仰拍")

    assert "手机随手拍质感：" in default_selfie
    assert "场景和镜头不得改变、增添或删减今日穿搭" in default_selfie
    assert "手机随手拍质感：" not in explicit_selfie

    with (
        patch(
            "astrbot_plugin_selfie_image.prompts.selfie_actions.random.choices",
            return_value=["mirror_full"],
        ),
        patch(
            "astrbot_plugin_selfie_image.prompts.selfie_actions.random.choice",
            side_effect=lambda items: items[0],
        ),
    ):
        mirror_selfie = build_selfie_look_action(
            scene_light_provider=lambda _kind: (["镜前整洁台面"], ["室内白光"])
        )

    assert "【shot:mirror_full】" in mirror_selfie
    assert "头部、上衣、下装和鞋履完整入镜" in mirror_selfie
    assert "人物与倒影比例一致" in mirror_selfie
    with tempfile.TemporaryDirectory() as tmp:
        assert not PersonaManager(tmp).analyze_selfie_intent(mirror_selfie).is_legs_only


def test_llm_selfie_intents_route_to_their_mode_builders_and_cos_rebuilds():
    from astrbot_plugin_selfie_image.main import SelfieImagePlugin

    with tempfile.TemporaryDirectory() as tmp:
        plugin = object.__new__(SelfieImagePlugin)
        plugin.persona = PersonaManager(tmp)
        built = []

        def marker(kind, tag):
            def build(request, has_refs, **kwargs):
                built.append((kind, request, has_refs, kwargs))
                return f"{kind}:{request} {tag}"

            return build

        plugin._build_cos_look_action = marker("cos", "【cos:test】")
        plugin._normalize_selfie_action = lambda request, _refs: f"legs:{request} 【legs:outfit】"
        plugin._build_group_selfie_action = marker("group", "【shot:group_portrait】")
        plugin._build_third_person_look_action = marker("third", "【shot:half_front】")
        plugin._build_selfie_request_action = marker("selfie", "【shot:arm_half】")

        cos_action, _, cos_query = plugin._prepare_llm_selfie_action("【LLM 自拍】看看COS：洛琪希", True)
        legs_action, legs_extra, _ = plugin._prepare_llm_selfie_action("【LLM 自拍】看看腿 白丝", False)
        group_action, _, _ = plugin._prepare_llm_selfie_action("【LLM 自拍】和我合影", False)
        third_action, _, _ = plugin._prepare_llm_selfie_action("【LLM 自拍】看看你现在的样子", False)
        selfie_action, _, _ = plugin._prepare_llm_selfie_action("【LLM 自拍】白裙自拍", False)

        assert cos_action.startswith("cos:看看COS：洛琪希")
        assert cos_query == "看看COS：洛琪希"
        assert legs_action.startswith("legs:")
        assert legs_extra == "【LLM 自拍】看看腿 白丝"
        assert group_action.startswith("group:")
        assert third_action.startswith("third:")
        assert selfie_action.startswith("selfie:")
        assert [entry[0] for entry in built] == ["cos", "group", "third", "selfie"]
        assert built[0][3]["match_query"] == cos_query

        rebuilt_cos_calls = []
        plugin._build_cos_look_action = lambda request="", has_refs=False, **kwargs: (
            rebuilt_cos_calls.append((request, has_refs, kwargs))
            or f"COS rebuilt {request} 【cos:test】 【cam:selfie】"
        )
        plugin._normalize_count = lambda count: int(count or 1)
        plugin._build_group_selfie_action = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("COS batch entered group builder")
        )
        plugin._build_leg_focus_action = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("COS batch entered legs builder")
        )
        plugin._build_third_person_look_action = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("COS batch entered third-person builder")
        )
        plugin._build_selfie_look_action = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("COS batch fell back to selfie builder")
        )

        async def build_prompt(_event, action, _refs, **_kwargs):
            return action, [], {}

        async def generate(*_args, **_kwargs):
            return {"success": True, "files": []}

        async def run_shots(*, total, run_one, **_kwargs):
            for index in range(total):
                await run_one(index)
            return {"success": True, "files": []}

        plugin._build_selfie_prompt_and_refs_for_event = build_prompt
        plugin._run_image_generation = generate
        plugin._run_counted_generation_shots = run_shots

        async def run_cos_batch():
            return await SelfieImagePlugin._run_selfie_batches_unlocked(
                plugin,
                "test-cos-llm",
                SimpleNamespace(),
                cos_action,
                [],
                "llm-generate-selfie",
                2,
                "9:16",
                "1K",
                "failed",
                rebuild_match_query=cos_query,
            )

        result = asyncio.run(run_cos_batch())
        assert result["success"]
        assert len(rebuilt_cos_calls) == 2
        assert all(call[2]["match_query"] == cos_query for call in rebuilt_cos_calls)


def test_llm_image_detection_sends_explicit_look_modes_to_selfie_flow():
    for prompt in ("看看COS洛琪希", "看看腿白丝", "看看你现在的样子", "帮我自拍"):
        assert looks_like_selfie_intent(prompt), prompt
    assert not looks_like_selfie_intent("看看这张城市夜景照片")


def test_random_cos_scene_bundles_include_a_distinct_visual_treatment():
    japanese_room = pick_cos_variation("test", extra_request="和风室内")
    assert japanese_room["scene_id"] == "japanese_ink_room"
    assert "古早DV录像静帧" in japanese_room["prompt"]
    assert "冷蓝色偏" in japanese_room["prompt"]

    rooftop = pick_cos_variation("test", extra_request="老旧楼群天台")
    assert rooftop["scene_id"] == "old_city_rooftop"
    assert "暖色低饱和胶片快照" in rooftop["prompt"]
    assert "密密层叠的老旧楼群" in rooftop["prompt"]
    assert "浅景深" not in rooftop["prompt"]
    assert "近景至中景" not in rooftop["prompt"]
    assert "低机位" not in rooftop["prompt"]


def test_cos_back_view_does_not_require_a_visible_face_in_third_person_mode():
    item = next(item for item in COS_LOOK_SETS if item["id"] == "roxy_cream")
    action = build_cos_look_action(
        "背对镜头",
        camera="third",
        picker=lambda **_: item,
    )
    assert "【cos_pose:back_to_camera】" in action
    assert "背面他拍" in action
    assert "不要求露出面部" in action
    assert "未明确要求遮脸时保持面部和服装清晰可见" not in action

    with tempfile.TemporaryDirectory() as tmp:
        persona = PersonaManager(tmp)
        prompt = persona.build_selfie_prompt(action, "小助", "温柔", True, 1)
    assert "若当前构图允许面部入镜" in prompt
    assert "除非用户要求遮脸，否则主角面部清晰自然" not in prompt


def test_explicit_cos_scene_and_pose_are_not_overridden():
    item = next(item for item in COS_LOOK_SETS if item["id"] == "roxy_cream")
    action = build_cos_look_action(
        "卧室地毯上跪坐",
        camera="third",
        picker=lambda **_: item,
    )
    assert "【cos_scene:bedroom_carpet】" in action
    assert "【cos_pose:kneeling_carpet】" in action
    assert "按用户指定的镜头构图执行" not in action
    assert "用户补充要求优先：卧室地毯上跪坐" in action


def test_back_look_pose_uses_a_downcast_profile_instead_of_a_full_turn():
    item = next(item for item in COS_LOOK_SETS if item["id"] == "roxy_cream")
    action = build_cos_look_action(
        "背身回眸",
        camera="third",
        has_refs=True,
        picker=lambda **_: item,
    )
    assert "【cos_pose:over_shoulder】" in action
    assert "【cos_framing:rear_lookback】" in action
    assert "低垂看向身侧或地面" in action
    assert "不直视镜头、不转成正脸" in action

    with tempfile.TemporaryDirectory() as tmp:
        persona = PersonaManager(tmp)
        prompt = persona.build_selfie_prompt(action, "小助", "温柔", True, 1)
    assert "若当前构图允许面部入镜" in prompt
    assert "除非用户要求遮脸，否则主角面部清晰自然" not in prompt


def test_back_look_orientation_does_not_count_as_a_complete_framing_request():
    selected = pick_cos_framing(extra_request="背身回眸", pose_id="over_shoulder")
    assert selected["view_id"] == "rear_lookback"


def test_random_cos_framing_is_always_selected_when_unspecified():
    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=lambda values: values[0],
    ):
        selected = pick_cos_framing(extra_request="COS 换装：自然站立")
    assert selected["view_id"]
    explicit = pick_cos_framing(extra_request="COS 换装：竖屏全身构图")
    assert explicit["explicit"] is True
    assert explicit["view_id"] == "user"


def test_cos_framing_alternatives_are_resolved_before_prompt_generation():
    selected = pick_cos_framing(
        extra_request="COS 换装：正面或三分之四侧身站立，近景至全身构图"
    )
    assert selected["randomized_from_options"] is True
    assert "正面" in selected["prompt"] or "三分之四侧身" in selected["prompt"]
    assert "近景" in selected["prompt"] or "全身" in selected["prompt"]
    resolved = selected["extra_request_text"]
    assert "正面或三分之四侧身" not in resolved
    assert "近景至全身" not in resolved


def test_cos_framing_alternatives_return_one_complete_framing_contract():
    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=lambda values: values[0],
    ):
        selected = pick_cos_framing(
            extra_request="人物正面或三分之四侧身站立，近景至全身构图"
        )

    assert selected["randomized_from_options"] is True
    assert selected["prompt"] == "正面站立；近景构图"
    assert "或" not in selected["extra_request_text"]
    assert "至" not in selected["extra_request_text"]
    assert "三分之四" not in selected["prompt"]
    assert "全身" not in selected["prompt"]
    assert selected["extra_request_text"]


def test_framing_option_combinations_can_be_avoided_between_batch_shots():
    request = "人物正面或三分之四侧身站立，近景至全身构图"
    with patch(
        "astrbot_plugin_selfie_image.cos.cos_looks.random.choice",
        side_effect=lambda values: values[0],
    ):
        first = pick_cos_framing(extra_request=request)
        second = pick_cos_framing(
            extra_request=request,
            avoid_framings={first["view_id"]},
        )
    assert first["view_id"] == "option_0_0"
    assert second["view_id"] != first["view_id"]
    assert second["prompt"] != first["prompt"]


def test_cos_pool_batch_can_rotate_pose_scene_framing_and_camera():
    used_poses = set()
    used_scenes = set()
    used_framings = set()
    used_cameras = set()
    actions = []
    for _ in range(3):
        action = build_cos_look_action(
            "洛琪希",
            avoid_poses=used_poses,
            avoid_scenes=used_scenes,
            avoid_framings=used_framings,
            avoid_cameras=used_cameras,
            match_query="洛琪希",
        )
        actions.append(action)
        for tag, values in (
            ("cos_pose", used_poses),
            ("cos_scene", used_scenes),
            ("cos_framing", used_framings),
            ("cam", used_cameras),
        ):
            match = re.search(rf"【{tag}:([a-z0-9_]+)】", action)
            assert match, (tag, action)
            values.add(match.group(1))

    for tag in ("cos_pose", "cos_scene", "cos_framing", "cam"):
        values = [re.search(rf"【{tag}:([a-z0-9_]+)】", action).group(1) for action in actions]
        assert len(set(values)) == 3, (tag, values)


def test_cos_english_builtin_keeps_the_complete_action_once():
    item = {
        "id": "english_cos_details",
        "title": "英文套装细节测试",
        "prompt": "严格换装为测试 COS：银白色长发，白色蓬袖短装，人物站在暖色室内，一手轻扶腰间配饰。",
    }
    action = build_cos_look_action(
        "",
        picker=lambda **_: item,
        camera="first",
    )
    from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt

    prompt = build_selfie_builtin_prompt(
        action,
        language="en",
        has_reference_image=True,
        appearance_type="real",
    )

    assert prompt.count(action) == 1
    assert "银白色长发" in prompt
    assert "白色蓬袖短装" in prompt
    assert "【cam:first】" in prompt


def test_cos_identity_contract_is_emitted_once_in_chinese_prompt():
    from astrbot_plugin_selfie_image.features.persona import PersonaManager
    from astrbot_plugin_selfie_image.prompts.prompt_templates import COS_IDENTITY_CONTRACT_ZH

    item = {
        "id": "canonical_identity_zh",
        "title": "身份合同中文测试",
        "prompt": "严格换装为测试 COS：银白色假发、白色短装、人物自然站立。",
    }
    action = build_cos_look_action("", picker=lambda **_: item, camera="first")

    with tempfile.TemporaryDirectory() as tmp:
        prompt = PersonaManager(tmp).build_selfie_prompt(action, "小助", "温柔", True, 0)

    assert prompt.count(COS_IDENTITY_CONTRACT_ZH) == 1
    assert "保持参考图中同一人的脸型" not in action
    assert "本次套装：身份合同中文测试。" in prompt


def test_cos_identity_contract_is_emitted_once_in_english_prompt():
    from astrbot_plugin_selfie_image.prompts.prompt_templates import (
        COS_IDENTITY_CONTRACT_EN,
        build_selfie_builtin_prompt,
    )

    item = {
        "id": "canonical_identity_en",
        "title": "身份合同英文测试",
        "prompt": "严格换装为测试 COS：银白色假发、白色短装、人物自然站立。",
    }
    action = build_cos_look_action("", picker=lambda **_: item, camera="first")
    prompt = build_selfie_builtin_prompt(
        action,
        language="en",
        has_reference_image=True,
        appearance_type="real",
    )

    assert prompt.count(COS_IDENTITY_CONTRACT_EN) == 1
    assert prompt.count(action) == 1
    assert "Preserve the reference face outline" not in action


def test_cos_english_builtin_uses_translated_action_contract_once():
    action = "【第一视角 / 看看COS模式】严格换装为测试 COS：银白色长发。 【cos:translated】 【cam:first】"
    translated_action = "Create the locked test COS outfit: silver-white long hair. [cos:translated] [cam:first]"
    from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt

    prompt = build_selfie_builtin_prompt(
        action,
        language="en",
        action_content=translated_action,
        has_reference_image=True,
        appearance_type="real",
    )

    assert prompt.count(translated_action) == 1
    assert translated_action in prompt
    assert "银白色长发" not in prompt


def test_cos_english_builtin_does_not_repeat_user_supplement():
    action = (
        "【自拍 / 看看COS模式】严格换装为测试 COS：银白色长发。"
        " 用户补充要求优先：站在窗边。 【cos:translated】 【cam:selfie】"
    )
    translated_action = (
        "Create the locked test COS outfit: silver-white long hair."
        " User supplement: stand by a window. [cos:translated] [cam:selfie]"
    )
    from astrbot_plugin_selfie_image.prompts.prompt_templates import build_selfie_builtin_prompt

    prompt = build_selfie_builtin_prompt(
        action,
        language="en",
        action_content=translated_action,
        user_text="stand by a window",
        has_reference_image=True,
        appearance_type="real",
    )

    assert prompt.count("stand by a window") == 1


def test_studio_cos_english_path_keeps_translated_action_contract():
    source = Path(__file__).resolve().parents[1] / "studio" / "studio_adapter.py"
    text = source.read_text(encoding="utf-8")
    block = text.split("if mode in {\"group\", \"selfie\"}:", 1)[1].split(
        'failure_stage = "generating"', 1
    )[0]

    assert "extract_generated_action_contract" in block
    assert "translated_action" in block
    assert "action_content=translated_action" in block


def test_compact_record_derives_legacy_cos_view_from_cam():
    from astrbot_plugin_selfie_image.core.utils import compact_generation_record

    compact = compact_generation_record(
        {
            "success": True,
            "original_prompt": "【cos:test】 【cam:first】",
            "request_data": {"request_prompt": "【cos:test】 【cam:first】"},
        }
    )

    assert compact["cos_view"] == "first"
    assert compact["request_data"]["cos_view"] == "first"


def test_generated_cos_action_is_not_appended_again_as_user_request():
    from astrbot_plugin_selfie_image.features.persona import PersonaManager

    item = {
        "id": "repro_duplicate_prompt",
        "title": "重复拼接测试",
        "prompt": "严格换装为测试 COS：人物自然站立，正面或三分之四侧身，近景至全身构图。",
    }
    action = build_cos_look_action(
        "",
        picker=lambda **_: item,
        camera="first",
    )
    with tempfile.TemporaryDirectory() as tmp:
        prompt = PersonaManager(tmp).build_selfie_prompt(action, "小助", "温柔", True, 0)

    assert prompt.count(action) == 1
    assert prompt.count("【COS换装第一视角模式】") == 1
    assert "用户要求：按以上已锁定的 COS 套装、相机、构图、姿势和场景规则生成。" in prompt
    assert "第三人称摄影画面" not in prompt
    assert "摄影师在画面外" not in prompt


def test_generated_cos_action_exposes_one_camera_tag_and_one_concrete_framing():
    item = {
        "id": "repro_single_view",
        "title": "单一视角测试",
        "prompt": "严格换装为测试 COS：人物自然站立。",
    }
    with patch("astrbot_plugin_selfie_image.cos.cos_looks.random.random", return_value=0.25):
        action = build_cos_look_action(
            "",
            picker=lambda **_: item,
            camera="first",
        )

    assert re.findall(r"【(?:cam|cos_view):[^】]+】", action) == ["【cam:first】"]
    assert action.count("本次未指定视角，随机采用") == 1
    assert "视角按本套 COS 套装描述执行" not in action


def test_first_person_cos_action_is_not_treated_as_freeform_user_text():
    from astrbot_plugin_selfie_image.features.persona import PersonaManager, extract_user_action_text
    from astrbot_plugin_selfie_image.prompts.prompt_templates import extract_user_prompt

    item = {
        "id": "repro_first_person_marker",
        "title": "第一视角标记测试",
        "prompt": "严格换装为测试 COS：人物自然站立。",
    }
    action = build_cos_look_action(
        "",
        picker=lambda **_: item,
        camera="first",
    )

    assert extract_user_action_text(action) == ""
    assert extract_user_prompt(action) == ""


def test_cos_action_replaces_outfit_view_alternatives():
    item = {
        "id": "framing_test",
        "title": "视角测试",
        "prompt": "严格换装为测试COS：人物正面或三分之四侧身站立，采用近景至全身构图。",
    }
    with patch("astrbot_plugin_selfie_image.cos.cos_looks.random.random", return_value=0.25):
        action = build_cos_look_action("", picker=lambda **_: item)
    assert "本次未指定视角，随机采用" in action
    assert "该随机机位优先于套装正文中的通用景别和机位描述" in action
    assert "正面或三分之四侧身" in action
    assert "近景至全身" in action


def test_selfie_command_cos_request_does_not_use_cos_pool():
    from astrbot_plugin_selfie_image.main import SelfieImagePlugin

    plugin = object.__new__(SelfieImagePlugin)
    action = plugin._build_selfie_request_action(
        "严格换装为测试角色 COS，人物站在花园中。",
        False,
    )

    assert "【COS换装" not in action
    assert "【cos:" not in action
    assert "【自拍 / 看看模式】" in action
    assert len(re.findall(r"【shot:[a-z_]+】", action)) == 1
    assert action.count("用户补充要求优先：") == 1


def test_private_hoshino_and_sunna_cos_outfits_are_registered():
    looks = {item["id"]: item for item in COS_LOOK_SETS}
    hoshino = looks["blue_archive_hoshino_private_gym"]
    hoshino_beach = looks["blue_archive_hoshino_private_beach"]
    sunna = looks["zzz_sunna_private_sportswear"]

    assert hoshino["cos_type"] == "蔚蓝档案"
    assert "私设体操服" in hoshino["title"]
    assert "黑白亮青绿拼色宽松运动夹克" in hoshino["prompt"]
    assert "黑色贴身运动短裤" in hoshino["prompt"]
    assert hoshino_beach["cos_type"] == "蔚蓝档案"
    assert "海边荷叶边泳装" in hoshino_beach["title"]
    assert "白色高腰多层荷叶边短裙" in hoshino_beach["prompt"]
    assert "安全裤" not in hoshino_beach["prompt"]
    assert "内衬" not in hoshino_beach["prompt"]
    assert sunna["cos_type"] == "绝区零"
    assert "妄想天使千夏（Sunna）" in sunna["prompt"]
    assert "浅粉色修身无袖背心" in sunna["prompt"]
    assert "黑色宽松运动短裤" in sunna["prompt"]

    assert {item["id"] for item in match_cos_look_sets("星野")} == {
        "blue_archive_hoshino",
        "blue_archive_hoshino_private_gym",
        "blue_archive_hoshino_white_shirt_dress",
    }
    assert [item["id"] for item in match_cos_look_sets("星野海边")] == [
        "blue_archive_hoshino_private_beach"
    ]
    assert [item["id"] for item in match_cos_look_sets("星野泳装")] == [
        "blue_archive_hoshino_private_beach"
    ]
    assert {item["id"] for item in match_cos_look_sets("千夏")} == {
        "blue_archive_chinatsu",
        "zzz_sunna_private_sportswear",
    }
    assert [item["id"] for item in match_cos_look_sets("蔚蓝档案 千夏")] == [
        "blue_archive_chinatsu"
    ]
    assert [item["id"] for item in match_cos_look_sets("绝区零 千夏")] == [
        "zzz_sunna_private_sportswear"
    ]


def test_original_blue_stage_cos_outfit_is_registered():
    looks = {item["id"]: item for item in COS_LOOK_SETS}
    stage = looks["original_blue_stage_opera"]
    assert stage["title"] == "原创COS·蓝色戏曲舞台服"
    assert stage["cos_type"] == "原创COS"
    assert "原创国风戏曲舞台服" in stage["prompt"]
    assert "蓝黑色古风帽冠" in stage["prompt"]
    assert "红色长流苏" in stage["prompt"]
    assert [item["id"] for item in match_cos_look_sets("蓝色戏曲舞台服")] == [
        "original_blue_stage_opera"
    ]


def test_priority_picker_filters_already_selected_models():
    page = (Path(__file__).resolve().parents[1] / "pages/dashboard/index.html").read_text(
        encoding="utf-8"
    )
    assert "function refreshPriorityPicker(kind = 'image')" in page
    assert "!selected.some(item => priorityEntryMatchesLabel(item, label))" in page
    assert "暂无可加入模型" in page
    assert "refreshPriorityPicker(kind);" in page


def test_preset_manager_lists_are_switchable_sibling_views():
    page = (Path(__file__).resolve().parents[1] / "pages/dashboard/index.html").read_text(
        encoding="utf-8"
    )
    assert 'id="presetManagerViews"' in page
    assert 'id="presetPromptManager"' in page
    assert 'id="presetCosManager" class="preset-manager-view" hidden' in page
    assert ".preset-manager-view[hidden] { display: none !important; }" in page
    assert "promptManager.style.display = isCos ? 'none' : '';" in page
    assert "cosManager.style.display = isCos ? '' : 'none';" in page
    assert 'id="presetKindCos"' in page
