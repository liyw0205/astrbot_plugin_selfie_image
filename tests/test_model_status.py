from astrbot_plugin_selfie_image.generation.generation_records import build_model_status


def test_model_status_aggregates_models_and_keeps_unused_targets() -> None:
    now = 1_000_000.0
    events = [
        {
            "timestamp": now - 10,
            "channel": "主渠道",
            "model": "image-a",
            "label": "主渠道/image-a",
            "success": True,
            "elapsed_seconds": 2.0,
            "media_type": "image",
        },
        {
            "timestamp": now - 20,
            "channel": "主渠道",
            "model": "image-a",
            "label": "主渠道/image-a",
            "success": False,
            "elapsed_seconds": 9.0,
            "error_category": "timeout",
            "media_type": "image",
        },
        {
            "timestamp": now - 30,
            "channel": "备用渠道",
            "model": "image-b",
            "label": "备用渠道/image-b",
            "success": False,
            "error_category": "auth",
            "media_type": "image",
        },
        {
            "timestamp": now - 5,
            "channel": "视频渠道",
            "model": "video-a",
            "label": "视频渠道/video-a",
            "success": False,
            "error_category": "server",
            "media_type": "video",
        },
    ]
    status = build_model_status(
        events,
        enabled_targets=[
            {"channel": "主渠道", "model": "image-a", "label": "主渠道/image-a"},
            {"channel": "新渠道", "model": "image-c", "label": "新渠道/image-c"},
        ],
        now=now,
        window_seconds=86400,
    )

    rows = {row["label"]: row for row in status["models"]}
    assert rows["主渠道/image-a"]["attempts"] == 2
    assert rows["主渠道/image-a"]["successes"] == 1
    assert rows["主渠道/image-a"]["status"] == "异常"
    assert rows["备用渠道/image-b"]["status"] == "验证失败"
    assert rows["新渠道/image-c"]["status"] == "未使用"
    assert "视频渠道/video-a" not in rows
    assert status["overall_status"] == "异常"
    assert status["model_count"] == 3


def test_model_status_uses_latest_event_for_recent_state_and_old_rows_are_visible() -> None:
    now = 2_000_000.0
    status = build_model_status(
        [
            {
                "timestamp": now - 5,
                "channel": "旧渠道",
                "success": False,
                "error_category": "server",
            },
            {
                "timestamp": now - 10,
                "channel": "新渠道",
                "model": "model-a",
                "label": "新渠道/model-a",
                "success": False,
                "error_category": "timeout",
            },
            {
                "timestamp": now - 1,
                "channel": "新渠道",
                "model": "model-a",
                "label": "新渠道/model-a",
                "success": True,
                "elapsed_seconds": 1.5,
            },
            *[
                {
                    "timestamp": now - 20 - index,
                    "channel": "新渠道",
                    "model": "model-a",
                    "label": "新渠道/model-a",
                    "success": True,
                    "elapsed_seconds": 1.5,
                }
                for index in range(8)
            ],
        ],
        now=now,
        window_seconds=3600,
    )
    rows = {row["label"]: row for row in status["models"]}
    assert rows["旧渠道/历史模型未标识"]["status"] == "断流"
    assert rows["新渠道/model-a"]["status"] == "降级"
    assert rows["新渠道/model-a"]["last_success"] is True
    assert rows["新渠道/model-a"]["last_error_category"] == ""


def test_model_status_report_renders_a_png(tmp_path) -> None:
    import pytest
    from PIL import Image

    from astrbot_plugin_selfie_image.main import SelfieImagePlugin

    if SelfieImagePlugin._status_report_font(14) is None:
        pytest.skip("A Chinese TrueType font is not installed in this test environment")

    plugin = SelfieImagePlugin.__new__(SelfieImagePlugin)
    plugin.data_dir = str(tmp_path)
    report = plugin._render_model_status_image(
        {
            "window_seconds": 86400,
            "updated_at": 1_000_000,
            "overall_status": "正常",
            "model_count": 1,
            "channel_count": 1,
            "available_count": 1,
            "alert_count": 0,
            "unused_count": 0,
            "models": [
                {
                    "label": "主渠道/image-a",
                    "status": "正常",
                    "success_rate": 1.0,
                    "successes": 2,
                    "attempts": 2,
                    "average_elapsed_seconds": 1.25,
                    "last_success": True,
                    "history": "■" * 24,
                    "error_categories": {},
                }
            ],
            "alerts": [],
        }
    )
    assert report is not None
    with Image.open(report) as image:
        assert image.format == "PNG"
        assert image.width == 1080
        assert image.height > 500


def test_model_status_font_uses_windows_chinese_fonts(monkeypatch) -> None:
    import astrbot_plugin_selfie_image.main as main_module

    class Mask:
        size = (1, 1)

        def __init__(self, value: str) -> None:
            self.value = value

        def getbbox(self):
            return (0, 0, 1, 1)

        def __bytes__(self):
            return self.value.encode("utf-8")

    class Font:
        def getmask(self, value: str):
            return Mask(value)

    class FontLoader:
        @staticmethod
        def truetype(path: str, size: int):
            assert path.replace("\\", "/").lower().endswith("fonts/msyh.ttc")
            return Font()

    monkeypatch.setenv("WINDIR", "C:\\Windows")
    monkeypatch.delenv("ASTRBOT_STATUS_FONT", raising=False)
    monkeypatch.setattr(
        main_module.os.path,
        "isfile",
        lambda path: path.replace("\\", "/").lower().endswith("fonts/msyh.ttc"),
    )
    monkeypatch.setattr(main_module, "ImageFont", FontLoader)

    assert isinstance(main_module.SelfieImagePlugin._status_report_font(20), Font)


def test_model_status_font_rejects_missing_glyph_boxes(monkeypatch) -> None:
    import astrbot_plugin_selfie_image.main as main_module

    class MissingGlyphMask:
        size = (1, 1)

        def getbbox(self):
            return (0, 0, 1, 1)

        def __bytes__(self):
            return b"same tofu box"

    class Font:
        def getmask(self, _value: str):
            return MissingGlyphMask()

    class FontLoader:
        @staticmethod
        def truetype(_path: str, _size: int):
            return Font()

    monkeypatch.setenv("WINDIR", "C:\\Windows")
    monkeypatch.delenv("ASTRBOT_STATUS_FONT", raising=False)
    monkeypatch.setattr(
        main_module.os.path,
        "isfile",
        lambda path: path.replace("\\", "/").lower().endswith("fonts/msyh.ttc"),
    )
    monkeypatch.setattr(main_module, "ImageFont", FontLoader)

    assert main_module.SelfieImagePlugin._status_report_font(20) is None
