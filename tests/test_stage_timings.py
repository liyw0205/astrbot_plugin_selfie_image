"""Stage samples must reflect observed work without changing task outcomes."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_selfie_image.core.constants import GENERATION_TIMING_STAGES
from astrbot_plugin_selfie_image.core.models import AICatConfig
from astrbot_plugin_selfie_image.core import provider_parser
from astrbot_plugin_selfie_image.core.providers import (
    BaseImageAdapter,
    ImageGenerateRequest,
    ImageGenerateResult,
    ImageReference,
)
from astrbot_plugin_selfie_image.core.utils import compact_generation_record
from astrbot_plugin_selfie_image.features import reference_collector
from astrbot_plugin_selfie_image.generation import image_workflow
from astrbot_plugin_selfie_image.generation.generation_records import build_generation_metrics


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 128


@pytest.fixture
def workflow(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(image_workflow, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    records = []
    target = SimpleNamespace(label="test/model", channel_name="test", model="model", extra={"image_to_text_enabled": True})
    plugin = SimpleNamespace(
        config=AICatConfig.from_dict({"image": {"enable_prompt_audit": True, "enable_output_audit": True}}),
        _select_healthy_generation_targets=lambda targets: (targets, []),
        _source_context=lambda *_args: {},
        _record_task=records.append,
        _set_generation_stage=Mock(),
        _save_reference_images_to_cache=lambda refs: ["request.png"] if refs else [],
        _composition_metadata=lambda *_args: {},
        _cleanup_image_cache_if_needed=lambda *_args: {},
        _prompt_en_needed=lambda *_args, **_kwargs: False,
        _is_audit_exempt=Mock(return_value=False),
        _save_cache_image=lambda *_args: "output.png",
        _cache_absolute_path=lambda path: path,
    )

    async def describe(*_args, **_kwargs):
        clock[0] += 2
        return "a reference description", {"applied": True}

    async def audit_prompt(*_args, **_kwargs):
        clock[0] += 3
        return True, ""

    async def audit_output(*_args, **_kwargs):
        clock[0] += 4
        return True, ""

    async def generate(*_args, **_kwargs):
        clock[0] += 7
        return ImageGenerateResult(images=[PNG_BYTES], used_model="test/model")

    plugin._describe_reference_images_for_generation = AsyncMock(side_effect=describe)
    plugin._audit_prompt = AsyncMock(side_effect=audit_prompt)
    plugin._audit_output_images = AsyncMock(side_effect=audit_output)
    generate_mock = AsyncMock(side_effect=generate)

    def run(**kwargs):
        options = dict(
            prompt="a portrait", aspect_ratio="1:1", resolution="1K",
            refs=[ImageReference(PNG_BYTES)], targets=[target],
            reference_selection={"download_elapsed_seconds": 1.25},
            generate_images=generate_mock,
        )
        options.update(kwargs)
        return asyncio.run(image_workflow.run_image_generation_workflow(plugin, **options))

    return SimpleNamespace(plugin=plugin, records=records, clock=clock, run=run, generate=generate_mock)


def test_successful_workflow_keeps_stage_and_generation_durations_separate(workflow):
    result = workflow.run()
    assert result["success"] is True
    assert result["elapsed_seconds"] == 7
    assert workflow.records[0]["stage_timings"] == {
        "reference_fetch": 1.25, "image_to_text": 2.0, "prompt_audit": 3.0, "output_audit": 4.0,
        "image_validation": 0.0, "image_cache_write": 0.0, "image_digest": 0.0,
    }


def test_workflow_emits_bounded_stage_and_channel_attempt_events(workflow, monkeypatch):
    events = []
    monkeypatch.setattr(image_workflow.logger, "info", lambda message: events.append(message))
    workflow.generate.side_effect = None
    workflow.generate.return_value = ImageGenerateResult(
        images=[PNG_BYTES],
        used_model="model-v1",
        attempts=[
            {
                "attempt": 1,
                "channel": "primary",
                "provider_type": "openai",
                "model": "model-v1",
                "success": False,
                "error": "api_key=private-channel-key prompt=private prompt",
                "error_category": "rate_limit",
                "http_status": 429,
                "elapsed_seconds": 1.25,
                "key_index": 1,
                "retry_count": 0,
                "retry_action": "next_key",
                "retryable": True,
                "stage_timings": {"result_url_download": 0.5, "secret": 99},
            },
            {
                "attempt": 1,
                "channel": "primary",
                "provider_type": "openai",
                "model": "model-v1",
                "success": True,
                "elapsed_seconds": 2.5,
                "key_index": 2,
                "retry_count": 1,
                "retry_action": "stop",
                "stage_timings": {"result_url_download": 0.25, "image_base64_decode": 0.1},
            },
        ],
    )

    result = workflow.run(
        record_context={"task_id": "web-12345678-1", "trace_id": "gen-12345678"},
    )

    assert result["success"] is True
    parsed = [json.loads(message.removeprefix("[SelfieImage] ")) for message in events]
    stage_event = next(event for event in parsed if event["event"] == "generation.stages")
    attempt_events = [event for event in parsed if event["event"] == "generation.channel_attempt"]
    assert stage_event["task_id"] == "web-12345678-1"
    assert stage_event["trace_id"] == "gen-12345678"
    assert stage_event["stage_timings"]["prompt_audit"] == 3.0
    assert stage_event["stage_timings"]["provider_attempts"] == 3.75
    assert stage_event["stage_timings"]["result_url_download"] == 0.75
    assert len(attempt_events) == 2
    assert [(event["success"], event["retry_action"]) for event in attempt_events] == [
        (False, "next_key"),
        (True, "stop"),
    ]
    assert all(event["trace_id"] == "gen-12345678" for event in attempt_events)
    assert attempt_events[0]["stage_timings"] == {"result_url_download": 0.5}
    serialized = json.dumps(parsed)
    assert "private-channel-key" not in serialized
    assert "private prompt" not in serialized
    assert '"secret"' not in serialized
    assert all("error" not in event for event in parsed)


def test_adapter_reports_response_decode_and_result_download_timings(monkeypatch):
    async def fetch(_session, _url, _timeout, **_kwargs):
        return PNG_BYTES

    monkeypatch.setattr(provider_parser, "fetch_generated_image_url", fetch)
    adapter = BaseImageAdapter(
        SimpleNamespace(extra={}, proxy="", timeout=1),
        object(),
    )
    request = ImageGenerateRequest(prompt="test")

    url_result = asyncio.run(adapter.result_from_response(
        {"data": [{"url": "https://cdn.example.test/image.png"}]}, request, ""
    ))
    encoded = base64.b64encode(PNG_BYTES).decode("ascii")
    base64_result = asyncio.run(adapter.result_from_response(
        {"data": [{"b64_json": encoded}]}, request, ""
    ))

    assert url_result.images == [PNG_BYTES]
    assert url_result.stage_timings["provider_response_parse"] >= 0
    assert url_result.stage_timings["result_url_download"] >= 0
    assert base64_result.images == [PNG_BYTES]
    assert base64_result.stage_timings["provider_response_parse"] >= 0
    assert base64_result.stage_timings["image_base64_decode"] >= 0


def test_openai_fast_path_reports_base64_decode_timing(monkeypatch):
    from astrbot_plugin_selfie_image.core.providers import OpenAIImageAdapter

    encoded = base64.b64encode(PNG_BYTES).decode("ascii")
    original_decode = provider_parser.b64_to_bytes

    def slow_decode(value):
        import time

        time.sleep(0.02)
        return original_decode(value)

    monkeypatch.setattr(provider_parser, "b64_to_bytes", slow_decode)
    adapter = OpenAIImageAdapter(
        SimpleNamespace(extra={}, proxy="", timeout=1), object()
    )
    result = asyncio.run(adapter._result_from_openai_images_data(
        {"data": [{"b64_json": encoded}]}, ImageGenerateRequest(prompt="test"), ""
    ))

    assert result.images == [PNG_BYTES]
    assert result.stage_timings["image_base64_decode"] >= 0.02
    assert result.stage_timings["image_response_extract"] >= result.stage_timings["image_base64_decode"]


def test_result_url_downloads_are_bounded_ordered_and_keep_failure_diagnostics(monkeypatch):
    active = 0
    max_active = 0

    async def fetch(_session, url, _timeout, *, diagnostics, **_kwargs):
        nonlocal active, max_active
        index = int(url.rsplit("/", 1)[-1].split(".", 1)[0])
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep((5 - index) * 0.002)
            if index in {1, 3}:
                diagnostics.append(f"failed-{index}")
                return None
            return b"\x89PNG\r\n\x1a\n" + str(index).encode() + b"0" * 128
        finally:
            active -= 1

    monkeypatch.setattr(provider_parser, "fetch_generated_image_url", fetch)
    payload = {"data": [{"url": f"https://cdn.example.test/{index}.png"} for index in range(5)]}
    collected = provider_parser.collect_images_from_unknown(payload)
    expected_indexes = [
        int(url.rsplit("/", 1)[-1].split(".", 1)[0])
        for url in collected["urls"]
        if int(url.rsplit("/", 1)[-1].split(".", 1)[0]) not in {1, 3}
    ]
    diagnostics = []
    timings = {}
    images = asyncio.run(provider_parser.images_from_response_unknown(
        object(), payload, timeout=1, download_diagnostics=diagnostics,
        collected=collected, stage_timings=timings,
    ))

    assert [int(image[8:9]) for image in images] == expected_indexes
    expected_failures = [
        f"failed-{url.rsplit('/', 1)[-1].split('.', 1)[0]}"
        for url in collected["urls"]
        if int(url.rsplit("/", 1)[-1].split(".", 1)[0]) in {1, 3}
    ]
    assert diagnostics == expected_failures
    assert max_active == 3
    assert timings["result_url_download"] >= 0


def test_result_url_download_cancels_batch_siblings_on_unexpected_error(monkeypatch):
    cancelled = []

    async def fetch(_session, url, _timeout, **_kwargs):
        if url.endswith("/1.png"):
            await asyncio.sleep(0.001)
            raise RuntimeError("broken downloader")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(url)
            raise

    monkeypatch.setattr(provider_parser, "fetch_generated_image_url", fetch)
    payload = {"data": [{"url": f"https://cdn.example.test/{index}.png"} for index in range(3)]}

    with pytest.raises(RuntimeError, match="broken downloader"):
        asyncio.run(provider_parser.images_from_response_unknown(object(), payload, timeout=1))

    assert len(cancelled) == 2


def test_result_url_download_waits_for_inflight_fetches_on_cancellation(monkeypatch):
    started = asyncio.Event()
    started_count = 0
    cancelled = []

    async def fetch(_session, url, _timeout, **_kwargs):
        nonlocal started_count
        started_count += 1
        if started_count == 3:
            started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(url)
            raise

    monkeypatch.setattr(provider_parser, "fetch_generated_image_url", fetch)
    payload = {"data": [{"url": f"https://cdn.example.test/{index}.png"} for index in range(3)]}

    async def run():
        task = asyncio.create_task(provider_parser.images_from_response_unknown(
            object(), payload, timeout=1
        ))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert started_count == 3
    assert len(cancelled) == 3


def test_reference_downloads_are_bounded_ordered_and_keep_failure_reasons(monkeypatch):
    active = 0
    max_active = 0

    async def fetch(source, _session, *, failure_reasons, **_kwargs):
        nonlocal active, max_active
        index = int(source.rsplit("-", 1)[-1])
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep((5 - index) * 0.002)
            if index in {1, 3}:
                failure_reasons.append(f"failed-{index}")
                return None
            return b"\x89PNG\r\n\x1a\n" + str(index).encode() + b"0" * 128, "image/png"
        finally:
            active -= 1

    monkeypatch.setattr(reference_collector, "fetch_image_source", fetch)
    failures = []
    refs, failed = asyncio.run(reference_collector.download_sources_as_references(
        [f"reference-{index}" for index in range(5)],
        object(),
        max_bytes=1024,
        failure_reasons=failures,
    ))

    assert [ref.data[8:9] for ref in refs] == [b"0", b"2", b"4"]
    assert [ref.source_url for ref in refs] == ["reference-0", "reference-2", "reference-4"]
    assert failed == 2
    assert failures == ["failed-1", "failed-3"]
    assert max_active == 3


def test_structured_log_failure_does_not_change_generation_result(workflow, monkeypatch):
    def fail_log(_message):
        raise RuntimeError("log transport unavailable")

    monkeypatch.setattr(image_workflow.logger, "info", fail_log)

    result = workflow.run()

    assert result["success"] is True
    assert len(workflow.records) == 1


def test_failed_stage_event_logs_only_failure_classification(workflow, monkeypatch):
    events = []
    monkeypatch.setattr(image_workflow.logger, "info", lambda message: events.append(message))
    workflow.generate.side_effect = None
    workflow.generate.return_value = ImageGenerateResult(
        error="HTTP 503 api_key=private-upstream-key prompt=private request",
    )

    result = workflow.run(
        record_context={"task_id": "web-12345678-1", "trace_id": "gen-12345678"},
    )

    assert result["success"] is False
    stage_event = next(
        json.loads(message.removeprefix("[SelfieImage] "))
        for message in events
        if '"event":"generation.stages"' in message
    )
    assert stage_event["success"] is False
    assert stage_event["failure_stage"] == "generate"
    assert stage_event["error_category"] == "server"
    assert "error" not in stage_event
    assert "private-upstream-key" not in json.dumps(stage_event)
    assert "private request" not in json.dumps(stage_event)


@pytest.mark.parametrize("failure", ["image_to_text", "prompt_audit", "generate", "output_audit"])
def test_failed_workflow_retains_only_stages_that_ran(workflow, failure):
    if failure == "image_to_text":
        async def fail_describe(*_args, **_kwargs):
            workflow.clock[0] += 2
            raise RuntimeError("description failed")
        workflow.plugin._describe_reference_images_for_generation.side_effect = fail_describe
    elif failure == "generate":
        workflow.generate.side_effect = None
        workflow.generate.return_value = ImageGenerateResult(error="upstream failed")
    else:
        hook = workflow.plugin._audit_prompt if failure == "prompt_audit" else workflow.plugin._audit_output_images
        async def reject(*_args, **_kwargs):
            workflow.clock[0] += 3 if failure == "prompt_audit" else 4
            return False, "blocked"
        hook.side_effect = reject

    result = workflow.run()
    assert result["success"] is False
    expected = {"reference_fetch": 1.25, "image_to_text": 2.0}
    if failure != "image_to_text":
        expected["prompt_audit"] = 3.0
    if failure == "output_audit":
        expected.update({"image_validation": 0.0, "image_cache_write": 0.0, "image_digest": 0.0})
        expected["output_audit"] = 4.0
    assert workflow.records[0]["stage_timings"] == expected


@pytest.mark.parametrize("exempt", [False, True])
def test_disabled_or_exempt_audit_does_not_add_zero_samples(workflow, exempt):
    workflow.plugin._is_audit_exempt.return_value = exempt
    workflow.plugin.config.image_enable_prompt_audit = exempt
    workflow.plugin.config.image_enable_output_audit = exempt
    assert workflow.run(refs=[], reference_selection=None)["success"] is True
    assert "prompt_audit" not in workflow.records[0]["stage_timings"]
    assert "output_audit" not in workflow.records[0]["stage_timings"]
    assert "image_validation" in workflow.records[0]["stage_timings"]


def test_explicit_reference_roles_receive_collection_latency_once(workflow):
    workflow.plugin._consume_reference_selection = Mock(side_effect=[
        {"download_elapsed_seconds": 1.5, "roles": {"message": 1}}, {},
    ])
    selection = {"roles": {"persona": 1}, "selected_count": 1}
    for _ in range(2):
        assert workflow.run(event=object(), reference_selection=selection)["success"] is True
    assert workflow.records[0]["stage_timings"]["reference_fetch"] == 1.5
    assert "reference_fetch" not in workflow.records[1]["stage_timings"]
    assert workflow.records[0]["request_data"]["reference_selection"]["roles"] == {"persona": 1}
    assert selection == {"roles": {"persona": 1}, "selected_count": 1}


def test_reference_collection_measures_success_and_failure_without_empty_role_overhead(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(reference_collector, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def fetch(source, _session, **_kwargs):
        clock[0] += 2 if source == "good" else 5
        return (PNG_BYTES, "image/png") if source == "good" else None

    monkeypatch.setattr(reference_collector, "fetch_image_source", fetch)
    collector = reference_collector.ReferenceCollector(max_bytes=1024)
    collector.collect_source_buckets = Mock(return_value={"message": ["good"], "quote": ["bad"]})
    collected = asyncio.run(collector.collect(object(), object()))
    assert collected.source_count == 2
    assert collected.failed_count == 1
    assert len(collected.message) == 1
    assert collected.selection_summary()["download_elapsed_seconds"] == 7.0

    collector.collect_source_buckets.return_value = {}
    empty = asyncio.run(collector.collect(object(), object()))
    assert "download_elapsed_seconds" not in empty.selection_summary()
    legacy = reference_collector.CollectedReferences(source_count=1)
    assert "download_elapsed_seconds" not in legacy.selection_summary()


def test_stage_metrics_use_windowed_observed_samples_and_drop_nonfinite_values():
    records = [
        {"created_ts": 95, "stage_timings": {"image_to_text": 2, "reference_fetch": 0}},
        {"created_ts": 96, "stage_timings": {"image_to_text": 4, "reference_fetch": 1}},
        {"created_ts": 97},
        {"created_ts": 1, "stage_timings": {"image_to_text": 999}},
        {"created_ts": 98, "stage_timings": {"image_to_text": float("nan"), "prompt_audit": True}},
        {"created_ts": 99, "stage_timings": {"output_audit": float("inf"), "delivery_receipt": -1, "unknown": 1}},
        {"created_ts": 99, "stage_timings": [1, 2]},
    ]
    metrics = build_generation_metrics(records, window_seconds=10, now=100)
    stages = metrics["stage_timings"]
    assert set(stages) == set(GENERATION_TIMING_STAGES)
    assert stages["image_to_text"] == {"samples": 2, "p50": 2.0, "p95": 4.0, "max": 4.0}
    assert stages["reference_fetch"] == {"samples": 2, "p50": 0.0, "p95": 1.0, "max": 1.0}
    for stage in ("prompt_audit", "output_audit", "delivery_receipt"):
        assert stages[stage] == {"samples": 0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    assert build_generation_metrics([])["stage_timings"] == {
        stage: {"samples": 0, "p50": 0.0, "p95": 0.0, "max": 0.0} for stage in GENERATION_TIMING_STAGES
    }


def test_compaction_bounds_and_whitelists_stage_metadata():
    record = {"stage_timings": {
        "reference_fetch": 1.23456, "image_to_text": 999999,
        "prompt_audit": float("nan"), "output_audit": True, "delivery_receipt": -2,
        "secret-url": "https://example.test/private", "unknown": 2,
    }}
    compacted = compact_generation_record(record)
    assert compacted["stage_timings"] == {"reference_fetch": 1.235, "image_to_text": 86400.0}
    assert build_generation_metrics([compacted])["stage_timings"]["image_to_text"]["max"] == 86400.0
    assert "secret-url" in record["stage_timings"]


@pytest.mark.parametrize("value", [None, [], "invalid", {}, {"prompt_audit": float("inf")}, {"prompt_audit": 10 ** 1000}])
def test_compaction_drops_invalid_stage_payloads(value):
    assert "stage_timings" not in compact_generation_record({"stage_timings": value})
