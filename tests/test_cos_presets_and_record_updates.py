import asyncio
import json
from threading import RLock
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from astrbot_plugin_selfie_image.cos.cos_looks import build_cos_look_action, get_cos_look_by_id
from astrbot_plugin_selfie_image.generation.generation_store import GenerationStoreMixin
from astrbot_plugin_selfie_image.generation.selfie_batch_workflow import SelfieBatchWorkflow
from astrbot_plugin_selfie_image.generation.selfie_workflow import SelfieWorkflowService
from astrbot_plugin_selfie_image.studio.studio import action_prompt_presets, upper_prompt_presets
from astrbot_plugin_selfie_image.webui.controllers import RecordController
from astrbot_plugin_selfie_image.webui.services import WebContractError, filter_record_page


@pytest.mark.parametrize("preset_name,suppressed", [
    ("动作预设", True), (action_prompt_presets()[0]["title"], True),
    ("上身预设", False), (upper_prompt_presets()[0]["title"], False), ("", False),
])
def test_cos_command_only_action_presets_take_over_pose(preset_name, suppressed):
    captured = {}

    async def handle(**kwargs):
        captured.update(kwargs)
        if False:
            yield None

    look = get_cos_look_by_id("roxy_cream")
    plugin = SimpleNamespace(
        _extract_command_count=lambda *a, **kw: ("fixture", 5),
        _expand_cos_special_preset_variants=lambda *a: ([], "", "", ""),
        _expand_cos_user_text_with_preset=lambda *a: ("a wave followed by a turn", "", "", preset_name),
        _build_cos_look_action=lambda *a, **kw: build_cos_look_action(*a, **kw, picker=lambda **_: look),
        _natural_fail_fallback=lambda *a: "failed",
        _handle_selfie_command=handle,
    )
    service = SelfieWorkflowService(plugin, lambda *a, **kw: "fixture", lambda *a, **kw: [])

    async def run():
        return [item async for item in service.look_cos(object())]

    asyncio.run(run())
    assert captured["suppress_cos_pose"] is suppressed
    assert ("【cos_pose:" not in captured["fallback"]) is suppressed
    assert ("本次随机姿势优先" not in captured["fallback"]) is suppressed
    assert "a wave followed by a turn" in captured["fallback"]


@pytest.mark.parametrize("explicit", [False, True])
def test_cos_batch_does_not_inherit_a_random_collage(explicit):
    look = get_cos_look_by_id("roxy_cream")
    plugin = SimpleNamespace(
        _normalize_count=int,
        _looks_like_group_selfie_intent=lambda _: False,
        _build_cos_look_action=lambda *a, **kw: build_cos_look_action(
            *a, **kw, camera="third", picker=lambda **_: look,
        ),
    )
    workflow = SelfieBatchWorkflow(
        plugin, "task", object(), "【cos:roxy_cream】 【cos_view:multi_view_collage】", [],
        "command-look-cos", 5, "9:16", "1K", "failed",
        rebuild_extra_request="多视角拼贴" if explicit else "",
    )
    workflow._prepare_rebuild()

    def choose(values):
        return False if isinstance(values[0], bool) else values[0]

    with patch("astrbot_plugin_selfie_image.cos.cos_looks.random.choice", side_effect=choose):
        actions = workflow._build_actions()
    assert len(actions) == 5
    assert all(("【cos_view:multi_view_collage】" in action) is explicit for action in actions)


def test_record_delta_sends_only_changed_rows_and_preserves_page_order():
    old = [{"id": str(i), "success": True, "source": "fixture"} for i in range(4)]
    _, initial = filter_record_page(old, {"limit": "3"})
    params = {"limit": "3", "known_records": json.dumps(initial["record_revisions"])}
    assert filter_record_page(old, params)[0] == []
    current = [{"id": "new", "success": True, "source": "fixture"}, dict(old[0], delivery_success=True), *old[1:]]
    delta, meta = filter_record_page(current, params)
    assert [row["id"] for row in delta] == ["new", "0"]
    assert meta["page_ids"] == ["new", "0", "1"]
    assert meta["total"] == 5
    assert "2" not in meta["record_revisions"]
    _, deleted = filter_record_page([old[1]], params)
    assert deleted["page_ids"] == ["1"]


def test_record_delta_obeys_filters_and_rejects_invalid_manifests():
    rows = [{"id": "a", "success": True}, {"id": "b", "success": False}]
    page, meta = filter_record_page(rows, {"success": "false", "known_records": "{}"})
    assert page == [rows[1]]
    assert meta["filtered"] == 1
    for manifest in ("[]", "bad", '"value"'):
        with pytest.raises(WebContractError):
            filter_record_page(rows, {"known_records": manifest})


def test_record_summary_defers_heavy_fields_and_keeps_field_redaction():
    store = GenerationStoreMixin()
    store._records_lock = RLock()
    heavy = "large payload " * 100_000
    store._records = [{
        "id": "r", "success": True, "original_prompt": heavy, "final_prompt": "final text",
        "media_sources_file": "fixture.json", "generated_image_paths": ["result.png"],
        "request_data": {"api_key": "private", "aspect_ratio": "9:16", "request_prompt_en": "translated",
                         "prompt_en": {"applied": True, "original_prompt": heavy}},
        "response_data": {"raw_body": heavy, "token": "private"},
    }]

    def sidecar(_):
        raise AssertionError("summary and field reads must not load the media sidecar")

    store._attach_media_sidecar = sidecar
    controller = RecordController(store)
    summary = controller.detail("r", {"view": "summary"}).data
    assert len(json.dumps(summary)) < 3000
    assert summary["generated_image_paths"] == ["result.png"]
    assert summary["request_data"]["aspect_ratio"] == "9:16"
    assert "original_prompt" not in summary
    assert controller.detail("r", {"field": "original_prompt"}).data == {"original_prompt": heavy}
    assert controller.detail("r", {"field": "final_prompt"}).data == {"final_prompt": "final text"}
    assert controller.detail("r", {"field": "request_prompt_en"}).data == {"request_prompt_en": "translated"}
    request = controller.detail("r", {"field": "request_data"}).data
    assert request["request_data"]["api_key"] == "[REDACTED]"
    with pytest.raises(WebContractError):
        controller.detail("r", {"field": "media_sources_file"})
