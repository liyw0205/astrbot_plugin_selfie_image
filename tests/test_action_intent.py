from unittest.mock import patch

from astrbot_plugin_selfie_image.features.action_intent import classify_action_camera_intent
from astrbot_plugin_selfie_image.features.persona import extra_overrides_context, has_explicit_activity_request
from astrbot_plugin_selfie_image.prompts.selfie_actions import (
    build_selfie_look_action,
    build_third_person_look_action,
)


def test_scene_words_do_not_mean_the_activity_is_happening() -> None:
    pool = classify_action_camera_intent("在游泳池拍照")
    clothing = classify_action_camera_intent("穿泳池图案的上衣")

    assert pool.explicit_scene
    assert not pool.is_activity
    assert not pool.requires_offscreen_camera
    assert not clothing.explicit_scene
    assert not has_explicit_activity_request("穿泳池图案的上衣")
    assert extra_overrides_context("在游泳池拍照")
    for scene in ("街上", "户外", "公园", "地铁", "摄影棚"):
        assert extra_overrides_context(scene)


def test_activity_classifier_distinguishes_motion_tasks_support_and_single_hand() -> None:
    cooking = classify_action_camera_intent("在厨房双手切菜做饭")
    support = classify_action_camera_intent("双手撑地做拉伸")
    pose = classify_action_camera_intent("坐地盘腿")
    cup = classify_action_camera_intent("单手端杯看书")

    assert cooking.is_task and cooking.requires_offscreen_camera
    assert support.needs_support and support.requires_offscreen_camera
    assert pose.needs_pose_framing and not pose.requires_offscreen_camera
    assert cup.hands_occupied == "one"
    assert not cup.requires_offscreen_camera
    assert classify_action_camera_intent("在公园散步").full_body_motion
    assert classify_action_camera_intent("瑜伽动作").full_body_motion
    assert classify_action_camera_intent("瑜伽教室拍照").explicit_scene
    assert not classify_action_camera_intent("瑜伽教室拍照").is_activity
    assert classify_action_camera_intent("准备晚餐").requires_offscreen_camera


def test_common_bimanual_tasks_and_motion_variants_use_offscreen_camera() -> None:
    for text in (
        "在厨房做晚饭", "双手切菜", "弹钢琴", "画画", "做手工", "打字", "修理自行车",
        "洗衣服", "烘焙", "普拉提", "跳操", "遛狗", "滑旱冰",
    ):
        intent = classify_action_camera_intent(text)
        assert intent.is_activity, text
        assert intent.requires_offscreen_camera, text


def test_one_hand_writing_task_keeps_a_handheld_camera_option() -> None:
    intent = classify_action_camera_intent("坐在书桌前写字")
    action = build_selfie_look_action("坐在书桌前写字")

    assert intent.is_activity
    assert intent.hands_occupied == "one"
    assert not intent.requires_offscreen_camera
    assert intent.needs_pose_framing
    assert "【shot:pose】" in action
    assert "手机和支架留在画面外" not in action


def test_negative_and_clothing_language_does_not_trigger_activity_camera() -> None:
    for text in ("不要做饭", "不需要拉伸", "穿瑜伽裤", "拉伸面料"):
        assert not classify_action_camera_intent(text).is_activity
        assert not has_explicit_activity_request(text)


def test_explicit_camera_intent_and_bimanual_conflict_are_classified() -> None:
    assert classify_action_camera_intent("对镜自拍").explicit_camera == "mirror"
    assert classify_action_camera_intent("手持手机自拍").explicit_camera == "handheld"
    assert classify_action_camera_intent("固定机位定时自拍").explicit_camera == "offscreen"
    conflicted = classify_action_camera_intent("对镜自拍，双手做饭")
    assert conflicted.requires_offscreen_camera
    assert "【shot:mirror_half】" in build_selfie_look_action("对镜自拍")
    assert "【shot:arm_half】" in build_selfie_look_action("手持手机自拍")
    assert "【shot:timer_half】" in build_selfie_look_action("固定机位定时自拍")


def test_conflicting_handheld_selfie_request_is_normalized_for_two_hand_task() -> None:
    action = build_selfie_look_action("手持手机自拍，同时双手做饭")

    assert "【shot:activity】" in action
    assert "手持手机自拍" not in action
    assert "自拍成片，同时双手做饭" in action
    assert "手机和支架留在画面外" in action


def test_natural_handheld_camera_phrasings_are_removed_when_both_hands_are_busy() -> None:
    for camera_request in ("拿手机自拍", "举着手机拍张照", "用前置摄像头自拍"):
        action = build_selfie_look_action(f"{camera_request}，同时双手切菜")
        assert "【shot:activity】" in action
        assert camera_request not in action
        assert "手机和支架留在画面外" in action


def test_movement_framing_takes_priority_over_a_half_body_hand_gesture() -> None:
    action = build_selfie_look_action("跳舞时双手比心")

    assert "【shot:activity】" in action
    assert "中景或更宽取景" in action
    assert "timer_half" not in action


def test_two_hand_task_and_support_use_offscreen_camera_and_action_framing() -> None:
    selfie = build_selfie_look_action("在厨房双手切菜做饭")
    third = build_third_person_look_action("俯卧撑")

    for action in (selfie, third):
        assert "【shot:activity】" in action
        assert "手臂自然伸向镜头" not in action
    assert "不用脸部或胸像近景截断动作" in selfie
    assert "手机和支架留在画面外" in selfie
    assert "支撑关系" in third


def test_low_pose_uses_pose_framing_instead_of_random_portrait_or_selfie_arm() -> None:
    action = build_selfie_look_action("坐地盘腿，双手放松")

    assert "【shot:pose】" in action
    assert "姿势主体自拍中近景" in action
    assert "胸像近景裁掉姿势" in action
    assert "近景胸像自拍" not in action


def test_natural_sitting_kneeling_lying_and_crouching_phrases_get_pose_framing() -> None:
    for text in (
        "坐在椅子上", "跪在地上", "趴在床上", "躺在沙发上", "蹲在地上",
    ):
        action = build_selfie_look_action(text)
        assert "【shot:pose】" in action, text
        assert "胸像近景裁掉姿势" in action, text


def test_single_hand_and_ordinary_selfie_keep_handheld_options() -> None:
    with patch(
        "astrbot_plugin_selfie_image.prompts.selfie_actions.random.choices",
        return_value=["arm_half"],
    ):
        ordinary = build_selfie_look_action()
        cup = build_selfie_look_action("单手端杯")

    assert "【shot:arm_half】" in ordinary
    assert "【shot:arm_half】" in cup
    assert "自拍臂中近景" in ordinary


def test_crop_waist_activity_does_not_fall_back_to_seated_crop() -> None:
    action = build_selfie_look_action("漏腰，在舞蹈室坐地拉伸")

    assert "【shot:crop_waist】" in action
    assert "画外固定机位活动自拍" in action
    assert "动作完整性优先" in action
    assert "放松地坐在沙发边" not in action
    assert "胸像自拍" not in action
