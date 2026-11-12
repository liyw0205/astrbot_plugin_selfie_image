"""Regression tests for the second repository audit's six confirmed issues."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from astrbot_plugin_selfie_image.core.models import AICatConfig, normalize_proxy_entry, public_proxy_row
from astrbot_plugin_selfie_image.core.providers import ImageReference
from astrbot_plugin_selfie_image.features.config_manager import (
    ConfigurationMixin,
    _mask_web_channel_credentials,
    _restore_web_channel_credentials,
    _restore_web_proxy_credentials,
)
from astrbot_plugin_selfie_image.features.creative_features import CreativeFeaturesMixin
from astrbot_plugin_selfie_image.features.reference_media import ImageDeliveryOutcome
from astrbot_plugin_selfie_image.generation.storage_consistency import inspect_storage_consistency
from astrbot_plugin_selfie_image.studio.studio import StudioStore
from astrbot_plugin_selfie_image.tests.test_repository_fixes import runtime_plugin
from astrbot_plugin_selfie_image.tests.test_storage import TestStorage as StorageFixture
from astrbot_plugin_selfie_image.webui.dashboard_api import SelfieImageDashboardAPI
from astrbot_plugin_selfie_image.webui.web import FlaskWebServer


def png(color="red"):
    output = BytesIO()
    Image.new("RGB", (2, 2), color).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize("key", ["request_image_paths", "generated_image_paths", "image_paths"])
@pytest.mark.parametrize("nested", [False, True])
def test_consistency_recognizes_all_record_media(tmp_path, key, nested):
    media = tmp_path / "media.png"
    media.write_bytes(png())
    record = {"id": "record", key: [media.name]}
    if nested:
        record = {"id": "record", "request_data": {key: [media.name]}}
    report = inspect_storage_consistency([record], cache_root=str(tmp_path), sidecar_root="")
    assert not report["issues"]


def test_consistency_repair_preserves_records_canvases_and_pending_media(tmp_path):
    store = StorageFixture()._plugin(str(tmp_path))
    request = store._save_reference_images_to_cache([ImageReference(png())])[0]
    store._commit_generation_record({"id": "kept", "favorite": True, "request_image_paths": [request]})
    pending = store._save_reference_images_to_cache([ImageReference(png("blue"))])[0]
    protected = [request, pending]
    for name in ("studio", "creative_canvas"):
        canvas = StudioStore(str(tmp_path / name))
        setattr(store, name, canvas)
        path = name + ".png"
        (Path(store.generated_dir) / path).write_bytes(png())
        session = canvas.create(name)
        canvas.attach_run_finish(session["id"], "old", success=True, result_paths=[path])
        protected.append(path)
    orphan = Path(store.generated_dir) / "orphan.png"
    orphan.write_bytes(png())
    preview = store.repair_storage_consistency()
    assert [item["path"] for item in preview["candidates"]] == [orphan.name]
    repaired = store.repair_storage_consistency(confirm=True)
    assert repaired["deleted"] == [{"kind": "orphan_cache", "path": orphan.name}]
    assert all((Path(store.generated_dir) / path).is_file() for path in protected)


@pytest.mark.parametrize("protection", ["record", "pending", "studio"])
def test_consistency_rechecks_references_after_preview(tmp_path, monkeypatch, protection):
    store = StorageFixture()._plugin(str(tmp_path))
    media = Path(store.generated_dir) / "newly-used.png"
    media.write_bytes(png())
    inspect = store.inspect_storage_consistency
    first = True

    def stale_report():
        nonlocal first
        report = inspect()
        if first:
            first = False
            if protection == "record":
                store._commit_generation_record({"id": "new", "generated_image_paths": [media.name]})
            elif protection == "pending":
                store._register_inflight_media([media.name])
            else:
                store.studio = StudioStore(str(tmp_path))
                session = store.studio.create("new")
                store.studio.attach_run_finish(session["id"], "new", success=True, result_paths=[media.name])
        return report

    monkeypatch.setattr(store, "inspect_storage_consistency", stale_report)
    result = store.repair_storage_consistency(confirm=True)
    assert not result["deleted"]
    assert media.is_file()


@pytest.mark.parametrize("writer", ["generated", "reference"])
def test_consistency_repair_cannot_delete_media_between_write_and_reservation(tmp_path, monkeypatch, writer):
    store = StorageFixture()._plugin(str(tmp_path))
    written = threading.Event()
    release = threading.Event()
    repairing = threading.Event()
    register = store._register_inflight_media
    inspect = store.inspect_storage_consistency

    def paused_registration(paths):
        written.set()
        assert release.wait(5)
        return register(paths)

    def inspecting():
        repairing.set()
        return inspect()

    monkeypatch.setattr(store, "_register_inflight_media", paused_registration)
    monkeypatch.setattr(store, "inspect_storage_consistency", inspecting)
    with ThreadPoolExecutor(max_workers=2) as pool:
        save = pool.submit(
            store._save_cache_image if writer == "generated" else store._save_or_reuse_reference_image,
            *([png(), "generated"] if writer == "generated" else [ImageReference(png())]),
        )
        try:
            assert written.wait(5)
            repair = pool.submit(store.repair_storage_consistency, confirm=True)
            assert repairing.wait(5)
            assert not repair.done()
        finally:
            release.set()
        path = save.result(timeout=5)
        assert not repair.result(timeout=5)["deleted"]
        assert (Path(store.generated_dir) / path).is_file()


def test_consistency_repair_stops_if_canvas_references_cannot_be_read(tmp_path):
    store = StorageFixture()._plugin(str(tmp_path))
    media = Path(store.generated_dir) / "canvas.png"
    media.write_bytes(png())

    def broken():
        raise OSError("canvas storage unavailable")

    store.studio = SimpleNamespace(referenced_cache_paths=broken)
    with pytest.raises(OSError, match="canvas storage unavailable"):
        store.repair_storage_consistency(confirm=True)
    assert media.is_file()


@pytest.mark.parametrize("kind", ["image_channels", "audit_channels"])
@pytest.mark.parametrize("credential", [{}, {"api_key": ""}, {"api_key": "******"}, {"api_keys": ["******"]}])
def test_new_channel_never_inherits_credentials_by_position(kind, credential):
    current = {kind: [{"id": "old", "name": "old", "api_key": "old-secret"}]}
    patch = {kind: [{"id": "new", "name": "new", **credential}]}
    result = _restore_web_channel_credentials(patch, current)
    assert "old-secret" not in json.dumps(result)


@pytest.mark.parametrize("password", [None, "", "******"])
def test_new_proxy_never_inherits_credentials_by_position(password):
    patch = {"proxies": [{"id": "new", "password": password}]}
    current = {"proxies": [{"id": "old", "password": "old-secret"}]}
    assert "old-secret" not in json.dumps(_restore_web_proxy_credentials(patch, current))


def test_channel_ids_preserve_credentials_through_rename_and_reorder():
    current = {"image_channels": [
        {"id": "one", "name": "A", "api_key": "secret-A"},
        {"id": "two", "name": "B", "api_key": "secret-B"},
    ]}
    patch = {"image_channels": [
        {"id": "two", "name": "renamed-B", "api_key": "******"},
        {"id": "one", "name": "renamed-A", "api_key": "******"},
    ]}
    rows = _restore_web_channel_credentials(patch, current)["image_channels"]
    assert [row["api_key"] for row in rows] == ["secret-B", "secret-A"]


@pytest.mark.parametrize("change", [
    {"base_url": "https://new.example.test"}, {"baseUrl": "https://new.example.test"},
    {"provider_type": "novelai"}, {"providerType": "novelai"}, {"api_type": "novelai"},
])
def test_legacy_name_match_cannot_restore_credentials_to_changed_endpoint(change):
    old = {"id": "old", "name": "same", "base_url": "https://old.example.test", "provider_type": "openai", "api_key": "old-secret"}
    result = _restore_web_channel_credentials(
        {"image_channels": [{"name": "same", "api_key": "******", **change}]}, {"image_channels": [old]},
    )
    assert "old-secret" not in json.dumps(result)


def test_legacy_name_match_requires_unique_name_and_preserves_unchanged_row():
    old = {"id": "old", "name": "same", "base_url": "https://old.example.test", "api_key": "old-secret"}
    patch = {"image_channels": [{"name": "same", "api_key": "******"}]}
    restored = _restore_web_channel_credentials(patch, {"image_channels": [old]})["image_channels"][0]
    assert restored["id"] == "old" and restored["api_key"] == "old-secret"
    assert "old-secret" not in json.dumps(_restore_web_channel_credentials(patch, {"image_channels": [old, dict(old, id="other")]}))


def test_existing_proxy_password_survives_rename_and_reorder_but_typed_password_wins():
    old = {"proxies": [{"id": "one", "password": "secret-one"}, {"id": "two", "password": "secret-two"}]}
    patch = {"proxies": [{"id": "two", "name": "renamed", "password": "******"}, {"id": "one", "password": "new-secret"}]}
    assert [row["password"] for row in _restore_web_proxy_credentials(patch, old)["proxies"]] == ["secret-two", "new-secret"]


def config_plugin(tmp_path):
    plugin = ConfigurationMixin()
    plugin._config_lock = threading.RLock()
    plugin.config_path = str(tmp_path / "config.json")
    plugin.key_config = {"web": {"enable": False, "token": "audit-token"}}
    plugin.raw_config = {"image_channels": [{
        "name": "legacy", "base_url": "https://relay.example.test",
        "api_key": "legacy-secret", "model": "gpt-image-1", "provider_type": "openai",
    }]}
    plugin._apply_raw_config(plugin.raw_config)
    return plugin


def test_legacy_channel_gains_persisted_id_and_renames_without_losing_key(tmp_path):
    plugin = config_plugin(tmp_path)
    public = plugin.get_config_for_web()
    identity = public["image_channels"][0].get("id")
    assert identity
    public["image_channels"][0]["name"] = "renamed"
    saved = plugin.update_config_from_web(public)
    assert saved["image_channels"][0]["id"] == identity
    assert plugin.config.image_channels[0].api_key == "legacy-secret"
    assert json.loads(Path(plugin.config_path).read_text())["image_channels"][0]["id"] == identity


def test_duplicated_channel_gets_new_identity_but_keeps_explicit_source_credentials(tmp_path):
    plugin = config_plugin(tmp_path)
    public = plugin.get_config_for_web()
    duplicate = copy.deepcopy(public["image_channels"][0])
    duplicate["name"] = "legacy-copy"
    public["image_channels"].append(duplicate)
    saved = plugin.update_config_from_web(public)
    ids = [row.get("id") for row in saved["image_channels"]]
    assert all(ids) and len(set(ids)) == 2
    assert [row.api_key for row in plugin.config.image_channels] == ["legacy-secret"] * 2


@pytest.mark.parametrize("password", ["plain-secret", "pass@word:/?#", "with space"])
def test_public_proxy_masks_url_credentials_without_mutating_runtime(password):
    row = normalize_proxy_entry({"id": "proxy", "host": "proxy.example.test", "port": 8080,
                                 "username": "user", "password": password})
    public = public_proxy_row(row)
    assert public["password"] == "******"
    assert "url" not in public
    assert row["password"] == password
    assert public_proxy_row(row, mask_password=False)["url"] == row["url"]


def test_config_masks_structured_and_legacy_proxy_urls_and_roundtrips(tmp_path):
    plugin = config_plugin(tmp_path)
    proxy = normalize_proxy_entry({"id": "proxy", "host": "proxy.example.test", "port": 8080,
                                   "username": "user", "password": "proxy-secret"})
    plugin.raw_config["proxies"] = [proxy]
    plugin.raw_config["image_channels"][0]["proxy"] = proxy["url"]
    public = plugin.get_config_for_web()
    assert "proxy-secret" not in json.dumps(public)
    plugin.update_config_from_web(public)
    assert plugin.config.proxies[0]["password"] == "proxy-secret"
    assert plugin.config.image_channels[0].proxy == proxy["url"]


def test_masking_preserves_legacy_proxy_fields_and_explicit_password_presence():
    row = {"id": "legacy", "url": "http://proxy.example.test:8080", "password": "proxy-secret", "compatibility_field": True}
    public = _mask_web_channel_credentials({"proxies": [row]})["proxies"][0]
    assert public["password"] == "******" and "url" not in public
    assert public["host"] == "proxy.example.test" and public["compatibility_field"] is True
    assert row["password"] == "proxy-secret"


def test_url_only_legacy_proxy_export_and_import_keep_runtime_credentials(tmp_path):
    plugin = config_plugin(tmp_path)
    plugin._apply_raw_config({**plugin.raw_config, "proxies": [{"id": "legacy", "url": "http://user:proxy-secret@proxy.example.test:8080"}]})
    exported = plugin.export_config_for_web()
    assert "proxy-secret" not in json.dumps(exported)
    assert "url" not in exported["proxies"][0]
    assert plugin.preview_config_import(exported)["ok"]
    plugin.import_config_from_web(exported)
    assert plugin.config.proxies[0]["password"] == "proxy-secret"


def test_proxy_list_http_response_does_not_expose_password(tmp_path):
    plugin = config_plugin(tmp_path)
    plugin.raw_config["proxies"] = [normalize_proxy_entry({
        "id": "p", "host": "proxy.example.test", "port": 8080,
        "username": "user", "password": "proxy-secret",
    })]
    response = FlaskWebServer(plugin)._create_app().test_client().get(
        "/api/proxies", headers={"Authorization": "Bearer audit-token"})
    assert response.status_code == 200
    assert "proxy-secret" not in response.get_data(as_text=True)


def test_dashboard_variations_matches_flask(monkeypatch):
    plugin = CreativeFeaturesMixin()
    plugin.config = SimpleNamespace(web_token="audit-token")
    payload = {"prompt": "portrait", "count": 3, "variation_seed": "fixed"}
    response = FlaskWebServer(plugin)._create_app().test_client().post(
        "/api/creative/variations", headers={"Authorization": "Bearer audit-token"}, json=payload)
    expected = response.get_json()
    api = SelfieImageDashboardAPI(plugin)

    async def get_payload():
        return payload, None

    monkeypatch.setattr(api, "_json_object_payload", get_payload)
    monkeypatch.setattr(api, "_ok", lambda data: {"success": True, "data": data})
    monkeypatch.setattr(api, "_fail", lambda error, status=400: {"success": False, "error": error})
    assert asyncio.run(api.page_creative_variations()) == expected


def retry_plugin(tmp_path, count=1):
    plugin = runtime_plugin(tmp_path)
    plugin.__dict__.update(StorageFixture()._plugin(str(tmp_path)).__dict__)
    plugin._send_failures_lock = threading.RLock()
    plugin._send_failures = {}
    plugin._session_key = lambda event: "audit-scope"
    plugin._context_session_key = lambda event: "audit-scope"
    plugin._record_bot_image_context = lambda *_: None
    plugin._create_image_component = lambda path: path
    plugin._web_tasks["audit-task"] = {
        "task_id": "audit-task", "status": "delivery_failed", "success": False,
        "generation_success": True, "delivery_success": False, "delivery_failed": True,
        "requested_count": count, "succeeded_count": count, "failed_count": 0,
        "result": {"generation_success": True, "delivery_success": False, "delivery_failed": True,
                   "requested_count": count, "succeeded_count": count, "files": []},
    }
    for index in range(count):
        path = f"result-{index}.png"
        (Path(plugin.generated_dir) / path).write_bytes(png())
        plugin._commit_generation_record({
            "id": f"record-{index}", "task_id": "audit-task", "success": False,
            "generation_success": True, "delivery_success": False, "delivery_failed": True,
            "error": "original send failed", "delivery_error": "original send failed",
            "generated_image_paths": [path],
            "response_data": {"success": False, "delivery_success": False, "delivery_failed": True,
                              "error": "original send failed", "delivery_error": "original send failed"},
        })
        plugin._send_failures.setdefault("audit-scope", []).append(path)
        plugin._web_tasks["audit-task"]["result"]["files"].append(path)
    return plugin


class SentEvent:
    def chain_result(self, chain):
        return chain

    async def send(self, chain):
        return {"message_id": "new-receipt"}


def test_successful_redelivery_updates_record_database_and_task_snapshot(tmp_path):
    plugin = retry_plugin(tmp_path)
    result = asyncio.run(plugin.retry_failed_images(SentEvent()))
    assert result["sent"] == 1
    record = plugin._record_database().load_records()[0]
    assert record["delivery_success"] is True
    assert record["delivery_failed"] is False
    assert record["success"] is True and record["status"] == "succeeded"
    assert record["response_data"]["delivery_success"] is True
    assert record["delivery_messages"]
    assert "original send failed" in json.dumps(record["delivery_history"])
    task = json.loads(Path(plugin.tasks_path).read_text())["tasks"]["audit-task"]
    assert task["status"] == "succeeded" and task["delivery_success"] is True


@pytest.mark.parametrize("legacy", ["task", "record"])
def test_redelivery_recovers_legacy_delivery_failure_without_redundant_flags(tmp_path, legacy):
    plugin = retry_plugin(tmp_path)
    if legacy == "task":
        task = plugin._web_tasks["audit-task"]
        task.pop("delivery_failed")
        task["error"] = "original send failed"
        task["result"].update(status="delivery_failed", error="original send failed")
        task["result"].pop("delivery_failed")
    else:
        record = plugin._records[0]
        record.pop("delivery_failed")
        record.pop("delivery_error")
        record["response_data"].pop("delivery_failed")
        record["response_data"].pop("delivery_error")
        record["response_data"]["status"] = "delivery_failed"
    asyncio.run(plugin.retry_failed_images(SentEvent()))
    record = plugin._record_database().load_records()[0]
    assert "original send failed" in json.dumps(record["delivery_history"])
    assert not record.get("error") and not record["response_data"].get("error")
    assert plugin._web_tasks["audit-task"]["status"] == "succeeded"
    assert not plugin._web_tasks["audit-task"].get("error")


def test_redelivery_preserves_new_failures_added_while_sending(tmp_path):
    async def run():
        plugin = retry_plugin(tmp_path)
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def send(event, files, **kwargs):
            entered.set()
            await finish.wait()
            return ImageDeliveryOutcome(1)

        plugin._send_generated_images = send
        retry = asyncio.create_task(plugin.retry_failed_images(SentEvent()))
        await entered.wait()
        plugin._send_failures.setdefault("audit-scope", []).append("new-failure.png")
        finish.set()
        await retry
        assert plugin._send_failures["audit-scope"] == ["new-failure.png"]

    asyncio.run(run())


def test_concurrent_redelivery_does_not_send_same_image_twice(tmp_path):
    async def run():
        plugin = retry_plugin(tmp_path)
        entered = asyncio.Event()
        finish = asyncio.Event()
        sends = []

        async def send(event, files, **kwargs):
            sends.extend(files)
            entered.set()
            await finish.wait()
            return ImageDeliveryOutcome(1)

        plugin._send_generated_images = send
        first = asyncio.create_task(plugin.retry_failed_images(SentEvent()))
        await entered.wait()
        second = asyncio.create_task(plugin.retry_failed_images(SentEvent()))
        await asyncio.sleep(0)
        finish.set()
        await asyncio.gather(first, second)
        assert len(sends) == 1

    asyncio.run(run())


def test_cancelled_redelivery_preserves_unsent_queue(tmp_path):
    async def run():
        plugin = retry_plugin(tmp_path, 2)
        entered = asyncio.Event()

        async def send(event, files, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        plugin._send_generated_images = send
        retry = asyncio.create_task(plugin.retry_failed_images(SentEvent()))
        await entered.wait()
        retry.cancel()
        await asyncio.gather(retry, return_exceptions=True)
        assert plugin._send_failures["audit-scope"] == ["result-0.png", "result-1.png"]

    asyncio.run(run())


def test_partial_redelivery_does_not_promote_a_batch_with_other_failed_images(tmp_path):
    plugin = retry_plugin(tmp_path, 2)

    class MixedEvent(SentEvent):
        async def send(self, chain):
            if chain[0].endswith("result-1.png"):
                raise RuntimeError("adapter rejected image")
            return await super().send(chain)

    result = asyncio.run(plugin.retry_failed_images(MixedEvent()))
    assert result["sent"] == 1 and result["remaining"] == 1
    assert plugin._send_failures["audit-scope"] == ["result-1.png"]
    assert plugin._web_tasks["audit-task"]["status"] == "delivery_failed"
    assert plugin._web_tasks["audit-task"]["delivery_failed_count"] == 1
    again = asyncio.run(plugin.retry_failed_images(SentEvent()))
    assert again["sent"] == 1 and again["remaining"] == 0
    assert plugin._web_tasks["audit-task"]["status"] == "succeeded"
    assert plugin._active_image_retries == {}


def test_ambiguous_redelivery_is_not_automatically_queued_again(tmp_path):
    plugin = retry_plugin(tmp_path)

    class TimedOutEvent(SentEvent):
        async def send(self, chain):
            raise TimeoutError("receipt timed out")

    result = asyncio.run(plugin.retry_failed_images(TimedOutEvent()))
    assert result["unknown"] == 1 and result["remaining"] == 0
    assert plugin._send_failures == {}
    record = plugin._record_database().load_records()[0]
    assert record["delivery_unknown"] is True and record["delivery_success"] is None
    assert record["delivery_failed"] is False
    assert plugin._web_tasks["audit-task"]["user_notification_status"] == "unknown"
    assert plugin._web_tasks["audit-task"]["delivery_success"] is None


@pytest.mark.parametrize("ambiguous", [False, True])
def test_cancellation_after_transport_outcome_waits_for_persistence_without_requeue(tmp_path, monkeypatch, ambiguous):
    async def run():
        plugin = retry_plugin(tmp_path, 2)
        entered = threading.Event()
        finish = threading.Event()
        update = plugin._update_generation_records_delivery

        def persist(*args, **kwargs):
            entered.set()
            assert finish.wait(5)
            return update(*args, **kwargs)

        class OutcomeEvent(SentEvent):
            async def send(self, chain):
                if ambiguous:
                    raise TimeoutError("receipt timed out")
                return await super().send(chain)

        monkeypatch.setattr(plugin, "_update_generation_records_delivery", persist)
        retry = asyncio.create_task(plugin.retry_failed_images(OutcomeEvent()))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            retry.cancel()
            await asyncio.sleep(0)
            assert not retry.done()
            plugin._send_failures.setdefault("audit-scope", []).append("new-failure.png")
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await retry
        assert plugin._send_failures["audit-scope"] == ["result-1.png", "new-failure.png"]
        assert plugin._active_image_retries == {}
        record = next(r for r in plugin._record_database().load_records() if r["id"] == "record-0")
        assert record["delivery_success"] is (None if ambiguous else True)

    asyncio.run(run())


def test_redelivery_drops_missing_files_and_invalid_paths_without_sending_them(tmp_path):
    plugin = retry_plugin(tmp_path)
    plugin._send_failures["audit-scope"] = ["missing.png", "../outside.png", "result-0.png", "result-0.png"]
    result = asyncio.run(plugin.retry_failed_images(SentEvent()))
    assert result["sent"] == 1 and result["missing"] == 2 and result["remaining"] == 0


def test_redelivery_exception_restores_unsent_items_and_new_failures(tmp_path):
    async def run():
        plugin = retry_plugin(tmp_path, 2)

        async def broken(*args, **kwargs):
            plugin._send_failures.setdefault("audit-scope", []).append("new-failure.png")
            raise RuntimeError("unexpected adapter error")

        plugin._send_generated_images = broken
        with pytest.raises(RuntimeError, match="unexpected adapter error"):
            await plugin.retry_failed_images(SentEvent())
        assert plugin._send_failures["audit-scope"] == ["result-0.png", "result-1.png", "new-failure.png"]
        assert plugin._active_image_retries == {}

    asyncio.run(run())


def test_redelivery_updates_only_the_matching_session_record_when_a_path_is_shared(tmp_path):
    plugin = retry_plugin(tmp_path)
    plugin._prompt_record_patch("record-0", {"lookup_scope": "audit-scope"})
    plugin._commit_generation_record({
        "id": "other", "task_id": "other-task", "lookup_scope": "other-scope",
        "success": False, "generation_success": True, "delivery_success": False,
        "delivery_failed": True, "generated_image_paths": ["result-0.png"],
    })
    asyncio.run(plugin.retry_failed_images(SentEvent()))
    records = {r["id"]: r for r in plugin._record_database().load_records()}
    assert records["record-0"]["delivery_success"] is True
    assert records["other"]["delivery_success"] is False and not records["other"].get("delivery_messages")


def test_redelivery_does_not_guess_between_ambiguous_legacy_records(tmp_path):
    plugin = retry_plugin(tmp_path)
    plugin._commit_generation_record({
        "id": "other", "task_id": "other-task", "success": False,
        "generation_success": True, "delivery_success": False, "delivery_failed": True,
        "generated_image_paths": ["result-0.png"],
    })
    assert asyncio.run(plugin.retry_failed_images(SentEvent()))["sent"] == 1
    assert all(r["delivery_success"] is False for r in plugin._record_database().load_records())


def test_redelivery_of_legacy_multi_image_record_requires_all_images_to_succeed(tmp_path):
    plugin = retry_plugin(tmp_path, 2)
    plugin._records = [dict(plugin._records[-1], generated_image_paths=["result-0.png", "result-1.png"])]
    plugin._web_tasks["audit-task"]["record_ids"] = [plugin._records[0]["id"]]
    plugin._send_failures["audit-scope"] = ["result-0.png"]
    asyncio.run(plugin.retry_failed_images(SentEvent()))
    assert plugin._records[0]["delivery_success"] is False
    assert plugin._web_tasks["audit-task"]["delivery_success"] is False
    plugin._send_failures["audit-scope"] = ["result-1.png"]
    asyncio.run(plugin.retry_failed_images(SentEvent()))
    assert plugin._records[0]["delivery_success"] is True
    assert plugin._web_tasks["audit-task"]["delivery_success"] is True


def test_redelivery_cannot_claim_full_delivery_when_task_records_are_missing(tmp_path):
    plugin = retry_plugin(tmp_path, 2)
    plugin._records = [r for r in plugin._records if r["id"] == "record-0"]
    plugin._send_failures["audit-scope"] = ["result-0.png"]
    asyncio.run(plugin.retry_failed_images(SentEvent()))
    assert plugin._web_tasks["audit-task"]["delivery_success"] is False


def test_redelivery_preserves_partial_generation_counts(tmp_path):
    plugin = retry_plugin(tmp_path)
    plugin._web_tasks["audit-task"].update(requested_count=2, failed_count=1)
    plugin._web_tasks["audit-task"]["result"].update(requested_count=2, failed_count=1)
    asyncio.run(plugin.retry_failed_images(SentEvent()))
    task = plugin._web_tasks["audit-task"]
    assert task["status"] == "partial_success" and task["success"] is False
    assert task["delivery_success"] is True and task["result"]["failed_count"] == 1


def test_delivery_failure_history_is_bounded_and_success_clears_response_error(tmp_path):
    plugin = retry_plugin(tmp_path)
    for index in range(40):
        plugin._update_generation_records_delivery("audit-task", record_id="record-0", delivered=False, error=f"failure-{index}")
    plugin._update_generation_records_delivery("audit-task", delivered=True)
    record = plugin._record_database().load_records()[0]
    assert len(record["delivery_history"]) == 32
    assert record["success"] is True and record["response_data"]["success"] is True
    assert not record.get("delivery_error") and not record["response_data"].get("delivery_error")


@pytest.mark.parametrize("result", [
    {"sent": 0, "remaining": 1, "busy": True}, {"sent": 0, "remaining": 2},
    {"sent": 0, "remaining": 0, "unknown": 2}, {"sent": 1, "remaining": 0, "unknown": 2},
])
def test_retry_command_distinguishes_busy_failed_and_unknown_outcomes(tmp_path, result):
    plugin = retry_plugin(tmp_path)
    plugin._permission_denied_message = lambda event: ""
    event = SimpleNamespace(plain_result=lambda text: text)

    async def reply(outcome):
        async def retry(_event):
            return outcome

        plugin.retry_failed_images = retry
        return [text async for text in plugin.cmd_image_retry_failed(event)][0]

    idle = asyncio.run(reply({"sent": 0, "remaining": 0}))
    text = asyncio.run(reply(result))
    assert text and text != idle
    if result.get("unknown"):
        assert "2" in text


def test_dashboard_channel_normalizer_preserves_id_and_new_channels_have_distinct_ids():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for dashboard JavaScript verification")
    page = (Path(__file__).resolve().parents[1] / "pages/dashboard/index.html").read_text()
    normalizer = page.split("    function normalizeChannel(ch) {", 1)[1].split("    function normalizeChannels", 1)[0]
    factories = page.split("    function newChannel() {", 1)[1].split("    function applyProviderDefaults", 1)[0]
    extra = re.search(r"    function newChannelId\(\) \{[\s\S]*?\n    \}", page)
    script = """
const assert = require('node:assert/strict');
const uniq = x => x;
const modelId = x => x;
const normalizeProviderType = x => x;
const collectModelImageToTextModels = () => [];
const collectModelPromptEnModels = () => [];
const collectModelDownloadProxyIds = () => ({});
const collectModelProviderTypes = () => ({});
const compactModelPromptEnModels = x => x;
const compactModelProviderTypes = x => x;
const applyProviderDefaults = x => x;
""" + (extra.group(0) if extra else "") + "\nfunction normalizeChannel(ch) {" + normalizer + "\nfunction newChannel() {" + factories + """
assert.equal(normalizeChannel({id: 'stable', name: 'renamed'}).id, 'stable');
const one = newChannel(), two = newChannel(), audit = newAuditChannel();
assert.ok(one.id && two.id && audit.id);
assert.equal(new Set([one.id, two.id, audit.id]).size, 3);
"""
    subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)


def test_dashboard_proxy_edit_keeps_masked_password_and_existing_identity():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for dashboard JavaScript verification")
    page = (Path(__file__).resolve().parents[1] / "pages/dashboard/index.html").read_text()
    modal = page.split("    function newProxyDraft() {", 1)[1].split("    function proxyStatusCodeClass", 1)[0]
    script = """
const assert = require('node:assert/strict');
let CONFIG = {proxies: [{id: 'stable-proxy', name: 'old', protocol: 'http', host: 'proxy.example.test', port: 8080, username: 'user', password: '******', has_auth: true}]};
const fields = {};
const $ = id => fields[id] ||= {value: '', checked: true, classList: {add() {}, remove() {}}};
const ensureConfig = () => {};
const renderProxyList = () => {};
const fillChannelProxyOptions = () => {};
let persisted = false;
const persistConfig = async () => {
  persisted = true;
  assert.equal(CONFIG.proxies[0].id, 'stable-proxy');
  assert.equal(CONFIG.proxies[0].password, '******');
  assert.equal(CONFIG.proxies[0].name, 'renamed');
  return true;
};
""" + "\nfunction newProxyDraft() {" + modal + """
openProxyModal(0);
$('proxyName').value = 'renamed';
saveProxyModal().then(() => assert.equal(persisted, true)).catch(error => { throw error; });
"""
    subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
