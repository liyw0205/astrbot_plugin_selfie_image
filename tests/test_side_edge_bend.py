from astrbot_plugin_selfie_image.cos.cos_looks import (
    COS_RANDOM_POSE_CLASSES,
    build_cos_look_action,
    parse_requested_cos_camera,
    pick_cos_variation,
)
from astrbot_plugin_selfie_image.cos.cos_looks_loader import (
    COS_JSON_POSES,
    validate_json_catalog,
)


def test_side_edge_bend_requires_two_hands_on_one_edge_and_sideward_gaze():
    required = (
        "双手必须同时扶住同一条窄侧边",
        "两只手都在肩部至头部附近接触并支撑同一边缘",
        "双臂共同承担身体前倾的支撑力",
        "视线看向支撑边",
        "可只见侧脸",
    )
    json_prompt = COS_JSON_POSES["side_edge_bend"]["prompt"]
    python_prompt = COS_RANDOM_POSE_CLASSES["side_edge_bend"]["prompt"]
    for marker in required:
        assert marker in json_prompt
        assert marker in python_prompt
    for weak_clause in ("另一只手可", "整理腰侧衣料", "轻触边缘"):
        assert weak_clause not in json_prompt
        assert weak_clause not in python_prompt

    assert validate_json_catalog() == []
    for camera in ("first", "third"):
        variation = pick_cos_variation(
            "roxy_cream",
            extra_request="扶着侧边弯腰",
            camera=camera,
        )
        assert variation["pose_id"] == "side_edge_bend"
        assert variation["pose_id"] not in {"wall_lean_curve", "rear_wall_curve"}
        assert all(marker in variation["prompt"] for marker in required)

        action = build_cos_look_action(
            "扶着侧边弯腰",
            force_id="roxy_cream",
            camera=camera,
        )
        assert "【cos_pose:side_edge_bend】" in action
        assert "双手必须同时扶住同一条窄侧边" in action
        assert "另一只手可" not in action
        assert "头部靠近并转向侧边" in action
        assert "不要求露出正脸" in action
        assert "未明确要求遮脸时保持面部" not in action
        assert "保持面部、发型、服装和配饰清晰可见" not in action
        if camera == "third":
            assert "手机、镜子或拍摄设备" in action
        else:
            assert "不自动出现手持设备" in action


def test_camera_direction_does_not_force_selfie_or_pull_gaze_to_camera():
    assert parse_requested_cos_camera("人物正对镜头摆拍") == ""
    assert parse_requested_cos_camera("不要正对镜头，视线看向侧边") == ""
    assert parse_requested_cos_camera("人物侧身，不正对镜头摆拍") == ""
    assert parse_requested_cos_camera("对镜自拍") == "selfie"
    assert parse_requested_cos_camera("镜子前拍照") == "selfie"

    request = "双手扶住同一条窄侧边，从髋部前倾，身体侧面朝向镜头，不是正对镜头摆拍，视线看向侧边"
    action = build_cos_look_action(request, force_id="roxy_cream")
    assert "【cos_pose:side_edge_bend】" in action
    assert "【cam:selfie】" not in action
    assert "不为了露脸回头或扭颈" in action


def test_explicit_selfie_keeps_side_edge_support_hands_and_sideward_gaze():
    action = build_cos_look_action(
        "对镜自拍，双手扶住同一条窄侧边，从髋部前倾，身体侧面朝向镜头",
        force_id="roxy_cream",
        camera="selfie",
    )
    assert "【cam:selfie】" in action
    assert "【cos_pose:side_edge_bend】" in action
    assert "侧边支撑前倾优先于通用自拍手势" in action
    assert "头部和视线朝向支撑边，不看镜头" in action
