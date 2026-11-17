"""Exercise outcome/timing contracts through real runners and public projections."""

import asyncio
import json
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_selfie_image.core.models import AICatConfig
from astrbot_plugin_selfie_image.core.utils import summarize_record_for_list
from astrbot_plugin_selfie_image.features.reference_media import ImageDeliveryOutcome
from astrbot_plugin_selfie_image.main import SelfieImagePlugin
from astrbot_plugin_selfie_image.studio.studio_adapter import StudioMixin
from astrbot_plugin_selfie_image.tasks.task_manager import WebTaskMixin
from astrbot_plugin_selfie_image.tasks.task_views import format_task_detail_text, format_task_list_text


@pytest.mark.parametrize("finish", ["stop", "cancel", "complete"])
@pytest.mark.parametrize("delivery", ["sent", "failed", "unknown"])
def test_batch_early_terminal_paths_preserve_transport_and_model(finish, delivery):
    plugin = object.__new__(SelfieImagePlugin)
    plugin.config = AICatConfig.from_dict({})
    plugin._image_scheduler = object()
    plugin._image_inflight_limit = lambda: 1
    plugin._web_task_lock = threading.RLock()
    plugin._web_tasks = {"task": {"requested_count": 2}}
    cancelled = [False]
    plugin._task_cancel_requested = lambda _tid: cancelled[0]
    plugin._set_web_image_task = Mock()
    plugin._record_generated_images_async = AsyncMock()
    outcome = {"sent": ImageDeliveryOutcome(1), "failed": ImageDeliveryOutcome(0, failed=1),
               "unknown": ImageDeliveryOutcome(0, unknown=1)}[delivery]
    plugin._send_generated_images = AsyncMock(return_value=outcome)
    plugin._set_task_notification_status = Mock()
    plugin._wait_for_record_commits = AsyncMock()
    plugin._update_generation_records_delivery = Mock()
    plugin._update_generation_records_delivery_timing = Mock()
    plugin._build_success_text = Mock(return_value="")
    plugin._batch_success_text = Mock(return_value="")
    plugin._batch_failure_policy = lambda: ("stop", 0)
    plugin._batch_shot_fail_text = Mock(return_value="failed")
    plugin._friendly_user_error_message = lambda error, _label: error
    plugin._send_task_notification = AsyncMock()

    async def run_one(index):
        if index:
            if finish == "cancel":
                cancelled[0] = True
            return {"success": False, "error": "second shot failed"}
        return {"success": True, "files": ["generated.png"], "used_model": "channel/model",
                "elapsed_seconds": 3.25, "request_data": {"model": "model"}}

    total = 1 if finish == "complete" else 2
    result = asyncio.run(plugin._run_counted_generation_shots(
        task_id="task", event=object(), total=total, fail_label="failed", run_one=run_one, log_prefix="audit",
    ))
    assert result["files"] == ["generated.png"]
    assert result["generation_success"] is True
    assert result["delivery_success"] is {"sent": True, "failed": False, "unknown": None}[delivery]
    assert bool(result["delivery_unknown"]) is (delivery == "unknown")
    assert bool(result["delivery_failed"]) is (delivery == "failed")
    assert result["used_model"] == "channel/model"
    assert result["elapsed_seconds"] == 3.25
    assert result["request_data"]["model"] == "model"


@pytest.mark.parametrize("finish", ["success", "partial", "failed", "late_cancel", "preflight_failure", "preflight_cancel"])
def test_studio_terminal_task_and_records_share_web_availability_contract(finish):
    stub = object.__new__(StudioMixin)
    state = {"task_id": "studio-task", "status": "queued", "request_data": {"kind": "studio"},
             "cancel_requested": False, "record_ids": [], "result": None}
    events = []
    session = {"id": "studio", "template": "t2i", "graph": {"mode": "t2i", "prompt": "portrait",
               "count": 2 if finish == "partial" else 1, "use_persona_identity": False}, "slots": []}
    stub.config = SimpleNamespace(image_default_aspect_ratio="9:16", image_default_resolution="1K",
                                  image_enable_image_prompt_en=False)
    stub.studio = SimpleNamespace(get=lambda _id: session, attach_run_finish=lambda *_a, **_kw: None)
    stub._record_task = Mock()
    if finish.startswith("preflight"):
        stub.studio.get = Mock(side_effect=RuntimeError("任务已取消" if finish.endswith("cancel") else "missing canvas"))
    stub.persona = SimpleNamespace(get_reference_image=lambda: None)
    stub._task_cancel_requested = lambda _tid: state["cancel_requested"]
    stub._web_task_timestamp = lambda: "now"
    stub._set_web_image_task = lambda _tid, **fields: state.update(fields)
    stub.get_web_image_task = lambda _tid: dict(state)
    stub._load_cache_image_bytes = lambda _path: None
    stub._prompt_en_needed = lambda *_a, **_kw: False

    async def commit(_tid):
        events.append("commit")

    async def mark(_tid, **kwargs):
        events.append(("available", kwargs))

    stub._wait_for_record_commits = commit
    stub._mark_task_records_delivery = mark
    generated = [False]

    async def generate(**_kwargs):
        if finish == "failed" or generated[0]:
            return {"success": False, "error": "provider failed"}
        generated[0] = True
        if finish == "late_cancel":
            state["cancel_requested"] = True
        return {"success": True, "image_paths": ["generated.png"], "files": ["generated.png"],
                "used_model": "channel/model", "elapsed_seconds": 2.5}

    stub._run_image_generation = generate
    asyncio.run(stub._run_studio_task("studio-task", "studio"))
    has_files = finish in {"success", "partial", "late_cancel"}
    assert state["generation_success"] is has_files
    assert state["delivery_success"] is (True if has_files else None)
    assert state["result"]["delivery_success"] is (True if has_files else None)
    if has_files:
        assert events[-2:] == ["commit", ("available", {"delivered": True, "paths": ["generated.png"]})]
    else:
        assert all(event == "commit" for event in events)


def task_probe(record):
    probe = object.__new__(WebTaskMixin)
    probe._web_task_lock = threading.RLock()
    probe._records_lock = threading.RLock()
    probe._persist_web_tasks_locked = lambda: None
    probe._records = [record | {"id": "record", "task_id": "task"}]
    probe._web_tasks = {"task": {"task_id": "task", "trace_id": "trace-audit", "status": "running",
                               "record_ids": ["record"], "result": {}}}
    return probe


@pytest.mark.parametrize("generation,delivery,expected", [
    (False, None, "not_sent"), (True, None, "pending"), (True, True, "sent"), (True, False, "failed"),
])
def test_image_projection_does_not_invent_unknown_receipts(generation, delivery, expected):
    detail = task_probe({"success": generation, "generation_success": generation,
                         "delivery_success": delivery}).get_web_task_detail("task")
    row = detail["image_results"][0]
    assert row["delivery_state"] == expected
    assert row["delivery_unknown"] is False


def test_image_projection_preserves_only_bounded_observed_stage_times():
    record = {"success": True, "delivery_success": True, "queue_wait_seconds": 1.5,
              "elapsed_seconds": float("nan"), "stage_timings": {"image_to_text": 2.5,
              "prompt_audit": True, "output_audit": float("inf"), "delivery_receipt": 0,
              "secret": "https://private.test/api_key=not-public"}}
    probe = task_probe(record)
    row = probe.get_web_task_detail("task")["image_results"][0]
    assert row["stage_timings"] == {"image_to_text": 2.5, "delivery_receipt": 0}
    assert row["queue_wait_seconds"] == 1.5
    assert row["elapsed_seconds"] is None
    assert "secret" not in str(row)
    assert "image_results" not in probe.list_web_tasks()["tasks"][0]


@pytest.mark.parametrize("value,expected", [(True, None), (-1, None), (float("inf"), None),
                                            (float("nan"), None), (90000, 86400), (0, 0)])
def test_image_projection_rejects_invalid_duration_samples(value, expected):
    row = task_probe({"success": True, "elapsed_seconds": value, "queue_wait_seconds": value,
                      "stage_timings": {"prompt_audit": value}}).get_web_task_detail("task")["image_results"][0]
    assert row["elapsed_seconds"] == expected
    assert row["queue_wait_seconds"] == expected
    assert row["stage_timings"] == ({} if expected is None else {"prompt_audit": expected})


def test_legacy_multimage_receipts_override_record_aggregate():
    rows = task_probe({
        "success": False, "generation_success": True, "delivery_success": False, "delivery_failed": True,
        "generated_image_paths": ["sent.png", "failed.png", "unknown.png"],
        "image_delivery": {"sent.png": "sent", "failed.png": "failed", "unknown.png": "unknown"},
    }).get_web_task_detail("task")["image_results"]
    assert [(row["delivery_state"], row["delivery_success"], row["delivery_failed"], row["delivery_unknown"])
            for row in rows] == [("sent", True, False, False), ("failed", False, True, False),
                                 ("unknown", None, False, True)]


def test_record_list_and_command_views_keep_unknown_separate_from_failure():
    record = {"success": True, "generation_success": True, "delivery_success": None, "delivery_unknown": True}
    assert summarize_record_for_list(record)["delivery_unknown"] is True
    task = record | {"task_id": "task", "status": "succeeded", "result": record}
    assert "发送状态未知" in format_task_detail_text(task)
    assert "发送状态未知" in format_task_list_text([task], include_finished=True)


@pytest.mark.parametrize("source", ["record", "response"])
@pytest.mark.parametrize("success", [True, False])
def test_failed_only_retry_skips_unknown_receipts_but_allows_explicit_retry(source, success):
    record = {"success": success, "generation_success": True, "delivery_success": None, "delivery_unknown": True}
    if source == "response":
        record = {"success": success, "response_data": record}
    probe = task_probe(record)
    probe._web_tasks["task"]["status"] = "succeeded"
    probe.get_record_for_web = lambda _rid: record
    probe.start_record_retry_task = Mock(return_value={"task_id": "retried"})
    result = probe.retry_web_tasks(["task"], scope="failed")
    assert result["submitted_count"] == 0
    probe.start_record_retry_task.assert_not_called()
    result = probe.retry_web_tasks(["task"], scope="all")
    assert result["submitted_count"] == 1
    probe.start_record_retry_task.assert_called_once()


def test_dashboard_delivery_views_do_not_relabel_unknown_as_timeout():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for dashboard JavaScript verification")
    root = Path(__file__).resolve().parents[1]

    def function(source: str, name: str, next_marker: str) -> str:
        start = source.index(f"    function {name}(")
        end = source.index(next_marker, start)
        return source[start:end]

    monitoring = (root / "pages/dashboard/source/monitoring.js").read_text(encoding="utf-8")
    details = (root / "pages/dashboard/source/record_details.js").read_text(encoding="utf-8")
    assets = (root / "pages/dashboard/source/records_assets.js").read_text(encoding="utf-8")
    functions = [
        function(monitoring, "taskQueueDeliveryLabel", "    function taskImageDeliveryLabel"),
        function(details, "recordDiagnosticsText", "    async function copyRecordDiagnostics"),
        function(assets, "recordLifecycleStatus", "    function recordDetailValue"),
    ]
    expression = """
globalThis.result = {
  queueUnknown: taskQueueDeliveryLabel({delivery_unknown:true}),
  queuePending: taskQueueDeliveryLabel({generation_success:true}),
  diagnostics: recordDiagnosticsText({success:true,generation_success:true,delivery_unknown:true}),
  record: recordLifecycleStatus({success:true,generation_success:true,delivery_unknown:true})
};
"""
    script = (
        "const vm=require('node:vm');const context={};"
        f"vm.runInNewContext({json.dumps(chr(10).join(functions) + expression)},context);"
        "process.stdout.write(JSON.stringify(context.result));"
    )
    result = json.loads(subprocess.run([node, "-e", script], text=True, capture_output=True, check=True).stdout)
    assert result["queueUnknown"] == "发送状态未知"
    assert result["queuePending"] == "待确认"
    assert "发送状态未知" in result["diagnostics"]
    assert "QQ 回执超时" not in result["diagnostics"]
    assert result["record"] == {"label": "回执未知", "tone": "warning"}
