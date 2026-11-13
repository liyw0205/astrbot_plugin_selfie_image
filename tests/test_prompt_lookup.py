"""Prompt lookup across transport conversion, restart and concurrent delivery."""

import asyncio
import copy
import hashlib
import json
import random
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image, ImageDraw

from astrbot_plugin_selfie_image.core.providers import ImageReference
from astrbot_plugin_selfie_image.core.utils import save_json_file, summarize_record_for_list
from astrbot_plugin_selfie_image.generation.prompt_lookup import (
    CODE_PATTERN, fingerprint_distance, image_fingerprint, new_image_code,
)
from astrbot_plugin_selfie_image.main import SelfieImagePlugin
from astrbot_plugin_selfie_image.webui.services import record_matches_query


class Chain(list):
    def message(self, text):
        self.append(text)
        return self


class Event:
    user_id = "u1"
    group_id = "g1"
    role = "member"
    message_str = ""
    unified_msg_origin = "qq:group:g1"

    def __init__(self):
        self.sent = []

    def plain_result(self, text):
        return text

    def chain_result(self, components):
        return Chain(components)

    async def send(self, message):
        self.sent.append(message)
        return {"data": {"message_id": 700 + len(self.sent)}}


@pytest.fixture
def plugin(tmp_path):
    obj = object.__new__(SelfieImagePlugin)
    obj.data_dir = str(tmp_path)
    obj.generated_dir = str(tmp_path / "images")
    Path(obj.generated_dir).mkdir()
    obj.records_path = str(tmp_path / "records.json")
    obj.records_db_path = str(tmp_path / "records.sqlite3")
    obj._records_lock = threading.RLock()
    obj._records = []
    obj._record_seq = 0
    obj._send_failures = {}
    obj._send_failures_lock = threading.RLock()
    obj.config = SimpleNamespace(image_record_keep_limit=1000)
    obj._permission_denied_message = lambda _event: ""
    obj._record_bot_image_context = lambda *_args: None
    obj._create_image_component = lambda path: ("image", path)
    obj._event_reference_images = AsyncMock(return_value=[])
    obj._reverse_image_prompt_with_llm = AsyncMock(return_value="reversed prompt")
    return obj


def sample_image(seed=1):
    rng = random.Random(seed)
    image = Image.new("RGB", (240, 400), (210, 210, 220))
    draw = ImageDraw.Draw(image)
    for _ in range(24):
        x, y = rng.randrange(200), rng.randrange(360)
        draw.rectangle((x, y, x + 35, y + 35), fill=tuple(rng.randrange(256) for _ in range(3)))
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def compress(data):
    output = BytesIO()
    with Image.open(BytesIO(data)) as image:
        image.resize((120, 200)).save(output, "JPEG", quality=70)
    return output.getvalue()


def commit(plugin, data=None, **extra):
    data = data or sample_image()
    filename = f"generated_{len(plugin._records)}.png"
    (Path(plugin.generated_dir) / filename).write_bytes(data)
    payload = {
        "success": True, "generated_image_paths": [filename],
        "md5": hashlib.md5(data).hexdigest(), "user_id": "u1", "group_id": "g1",
        "lookup_scope": Event.unified_msg_origin, "original_prompt": "【pose:seated】【cam:third】 input",
        "request_prompt": "before translation", "request_data": {"request_prompt_en": "final actual prompt"},
    }
    payload.update(extra)
    plugin._commit_generation_records(payload)
    return copy.deepcopy(plugin._records[0])


def test_codes_have_letters_and_digits_and_retry_collisions():
    codes = {new_image_code(set()) for _ in range(200)}
    assert len(codes) == 200
    assert all(CODE_PATTERN.fullmatch(code) for code in codes)
    with patch("astrbot_plugin_selfie_image.generation.prompt_lookup.secrets.choice", side_effect=list("ABC234DEF567")):
        assert new_image_code({"ABC234"}) == "DEF567"


def test_legacy_codes_backfill_once_without_reordering_database(plugin):
    database = plugin._record_database()
    database.upsert_record({"id": "old", "request_prompt": "old"}, sequence=7)
    database.upsert_record({"id": "new", "request_prompt": "new"}, sequence=20)
    first = plugin._load_records()
    second = plugin._load_records()
    assert [(r["id"], r["image_code"]) for r in first] == [(r["id"], r["image_code"]) for r in second]
    assert [r["id"] for r in second] == ["new", "old"]
    assert database.max_sequence() == 20


def test_json_migration_reserves_existing_codes_before_backfilling(plugin):
    save_json_file(plugin.records_path, {"records": [{"id": "missing"}, {"id": "existing", "image_code": "ABC234"}]})
    with patch("astrbot_plugin_selfie_image.generation.prompt_lookup.secrets.choice", side_effect=list("ABC234DEF567")):
        rows = plugin._load_records()
    assert [r["image_code"] for r in rows] == ["DEF567", "ABC234"]
    assert [r["image_code"] for r in plugin._load_records()] == ["DEF567", "ABC234"]


def test_batch_and_concurrent_commits_allocate_independent_codes(plugin):
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(plugin._commit_generation_records, [
            {"success": True, "generated_image_paths": [f"{i}-a.png", f"{i}-b.png"]} for i in range(8)
        ]))
    assert len(plugin._records) == 16
    assert len({r["image_code"] for r in plugin._records}) == 16
    assert len({r["id"] for r in plugin._records}) == 16


def test_qq_compression_matches_but_similar_candidates_require_confirmation(plugin):
    data = sample_image()
    row = commit(plugin, data)
    refs = [ImageReference(data=compress(data), mime_type="image/jpeg")]
    assert hashlib.md5(refs[0].data).hexdigest() != row["md5"]
    matched, method, candidates = plugin._lookup_prompt_image(Event(), refs, [])
    assert matched["id"] == row["id"]
    assert method == "图片相似匹配"
    assert not candidates
    commit(plugin, data, original_prompt="a different prompt")
    matched, _, candidates = plugin._lookup_prompt_image(Event(), refs, [])
    assert matched is None
    assert len(candidates) == 2


def test_lazy_old_fingerprints_and_jpeg_variants_remain_compatible(plugin):
    data = sample_image()
    row = commit(plugin, data)
    plugin._prompt_record_patch(row["id"], {"image_fingerprint": {}})
    refs = [ImageReference(data=compress(data), mime_type="image/jpeg")]
    matched, _, _ = plugin._lookup_prompt_image(Event(), refs, [])
    assert matched["id"] == row["id"]
    assert plugin._record_database().load_records()[0]["image_fingerprint"]


def test_permission_scope_and_recent_do_not_leak_other_users_or_web(plugin):
    first = commit(plugin)
    commit(plugin, user_id="u2")
    commit(plugin, lookup_scope="qq:group:g2")
    commit(plugin, user_id="", group_id="", lookup_scope="")
    assert plugin._recent_prompt_record(Event())["id"] == first["id"]
    assert len(plugin._prompt_records_for_event(Event())) == 1
    admin = Event()
    admin.role = "admin"
    assert len(plugin._prompt_records_for_event(admin)) == 4
    assert plugin._recent_prompt_record(admin)["id"] == first["id"]


def test_delivery_receipt_quote_and_confirmed_alias_survive_restart_and_delete(plugin):
    data = sample_image()
    row = commit(plugin, data)
    plugin._remember_prompt_delivery(Event(), row, None)
    assert not plugin._records[0].get("delivery_messages")
    plugin._remember_prompt_delivery(Event(), row, {"data": {"message_id": 123}})
    ref = ImageReference(data=compress(data), mime_type="image/jpeg")
    record, method, _ = plugin._lookup_prompt_image(Event(), [ref], ["123"])
    assert method == "引用消息"
    assert record["image_code"] == row["image_code"]
    plugin._records = plugin._load_records()
    assert plugin._lookup_prompt_image(Event(), [ref], [])[1] == "图片校验"
    plugin.delete_records([row["id"]])
    assert plugin._prompt_record_by_code(Event(), row["image_code"]) is None
    assert plugin._lookup_prompt_image(Event(), [ref], ["123"])[0] is None


def test_batch_delivery_receipts_persist_once_and_ignore_duplicates(plugin, monkeypatch):
    row = commit(plugin, generated_image_paths=["one.png", "two.png"])
    database = plugin._record_database()
    writes = []
    upsert = database.upsert_record

    def counted_upsert(record, **kwargs):
        writes.append(record["id"])
        return upsert(record, **kwargs)

    monkeypatch.setattr(database, "upsert_record", counted_upsert)
    deliveries = [
        (row, {"message_id": "701"}),
        (row, {"data": {"message_id": "702"}}),
    ]
    plugin._remember_prompt_deliveries(Event(), deliveries)

    assert writes == [row["id"]]
    assert plugin._records[0]["delivery_messages"] == [
        {"scope": Event.unified_msg_origin, "message_id": "701"},
        {"scope": Event.unified_msg_origin, "message_id": "702"},
    ]
    plugin._remember_prompt_deliveries(Event(), deliveries)
    assert writes == [row["id"]]


def test_quote_label_works_without_adapter_receipt(plugin):
    row = commit(plugin)
    event = Event()
    event.message_obj = SimpleNamespace(quote={"message": [{"type": "text", "data": {"text": "生图编号：" + row["image_code"]}}]})
    matched, method, _ = plugin._lookup_prompt_image(event, [], [])
    assert matched["id"] == row["id"]
    assert method == "引用消息编号"


def test_onebot_get_msg_recovers_label_and_links_reply_id(plugin):
    row = commit(plugin)
    event = Event()
    event.message_obj = SimpleNamespace(message=[{"type": "reply", "data": {"id": 42}}])
    event.bot = SimpleNamespace(call_action=AsyncMock(return_value={"data": {"message": [
        {"type": "text", "data": {"text": "生图编号：" + row["image_code"]}},
    ]}}))

    async def run():
        return [value async for value in plugin.cmd_view_prompt(event)]

    results = asyncio.run(run())
    assert len(results) == 2 and results[1] == "final actual prompt"
    assert "引用消息" in results[0]
    assert plugin._records[0]["delivery_messages"][0]["message_id"] == "42"


def test_candidate_confirmation_learns_only_for_current_user_and_expires(plugin):
    data = sample_image()
    row = commit(plugin, data)
    commit(plugin, data)
    refs = [ImageReference(data=compress(data), mime_type="image/jpeg")]
    _, _, candidates = plugin._lookup_prompt_image(Event(), refs, [])
    plugin._remember_prompt_candidates(Event(), refs, candidates)
    other = Event()
    other.user_id = "u2"
    plugin._confirm_prompt_candidate(other, row)
    assert not plugin._records[1].get("image_alias_md5s")
    plugin._confirm_prompt_candidate(Event(), row)
    assert plugin._lookup_prompt_image(Event(), refs, [])[0]["id"] == row["id"]
    plugin._remember_prompt_candidates(Event(), refs, candidates)
    for pending in plugin._prompt_confirmations.values():
        pending["created"] -= 601
    plugin._prompt_record_patch(row["id"], {"image_alias_md5s": []})
    plugin._confirm_prompt_candidate(Event(), row)
    assert plugin._lookup_prompt_image(Event(), refs, [])[0] is None


def test_invalid_and_flat_fingerprints_cannot_auto_match(plugin):
    assert image_fingerprint(b"bad bytes") == {}
    assert fingerprint_distance({"gray": []}, {}) is None
    output = BytesIO()
    Image.new("RGB", (240, 400), "white").save(output, "PNG")
    row = commit(plugin, output.getvalue())
    refs = [ImageReference(data=compress(output.getvalue()), mime_type="image/jpeg")]
    matched, _, candidates = plugin._lookup_prompt_image(Event(), refs, [])
    assert matched is None
    assert candidates[0]["id"] == row["id"]


def test_view_by_code_sends_metadata_then_exact_final_prompt_without_llm(plugin):
    row = commit(plugin)
    plugin._event_reference_images.side_effect = AssertionError("code lookup must not download images")

    async def run():
        return [value async for value in plugin.cmd_view_prompt(Event(), row["image_code"].lower())]

    results = asyncio.run(run())
    assert len(results) == 2
    assert row["image_code"] in results[0]
    assert results[1] == "final actual prompt"
    assert plugin._records[0]["prompt_selection"] == {"pose": "seated", "cam": "third"}
    plugin._reverse_image_prompt_with_llm.assert_not_awaited()


def test_unknown_image_does_not_silently_reverse_or_use_recent(plugin):
    commit(plugin)
    plugin._event_reference_images.return_value = [ImageReference(data=sample_image(99), mime_type="image/png")]

    async def run():
        return [value async for value in plugin.cmd_view_prompt(Event())]

    result = asyncio.run(run())
    assert len(result) == 1 and "未找到" in result[0]
    plugin._reverse_image_prompt_with_llm.assert_not_awaited()


def test_reverse_codes_are_separate_persistent_and_reusable(plugin):
    plugin._event_reference_images.return_value = [ImageReference(data=sample_image(), mime_type="image/png")]

    async def run(code=""):
        return [value async for value in plugin.cmd_reverse_image_prompt(Event(), code)]

    results = asyncio.run(run())
    assert len(results) == 2 and results[1] == "reversed prompt"
    assert "非原始提示词" in results[0]
    code = re.search(r"[A-Z0-9]{6}", results[0])[0]
    assert plugin._records == []
    del plugin._prompt_reverse_records
    assert asyncio.run(run(code)) == results
    assert plugin._reverse_image_prompt_with_llm.await_count == 1


@pytest.mark.parametrize("show_generation_info", [False, True])
@pytest.mark.parametrize("source", ["command-draw", "llm-generate-image", "llm-generate-selfie"])
def test_send_awaits_current_image_commit_and_records_message_id(plugin, show_generation_info, source):
    event = Event()
    plugin.config.image_show_generation_info = show_generation_info
    data = sample_image()
    path = Path(plugin.generated_dir) / "delayed.png"
    path.write_bytes(data)
    entered = threading.Event()
    release = threading.Event()
    original_commit = plugin._commit_generation_records

    def delayed(payload):
        entered.set()
        assert release.wait(5)
        original_commit(payload)

    plugin._commit_generation_records = delayed

    async def run():
        plugin._record_task({
            "success": True, "task_id": "delayed", "generated_image_paths": [path.name],
            "source": source, "user_id": event.user_id, "group_id": event.group_id,
            "lookup_scope": event.unified_msg_origin,
        })
        assert await asyncio.to_thread(entered.wait, 3)
        await plugin._wait_for_record_commits("delayed", timeout=0.01)
        assert all(not task.cancelled() for task in plugin._pending_record_commits["delayed"])
        sending = asyncio.create_task(plugin._send_generated_images(event, [str(path)]))
        await asyncio.sleep(0)
        assert not event.sent
        release.set()
        assert await sending == 1

    asyncio.run(run())
    record = plugin._records[0]
    expected_message = [("image", str(path))]
    if show_generation_info:
        expected_message.append("\n生图编号：" + record["image_code"])
    assert event.sent == [expected_message]
    assert plugin._records[0]["delivery_messages"][0]["message_id"] == "701"
    matched, method, candidates = plugin._lookup_prompt_image(event, [], ["701"])
    assert matched["image_code"] == record["image_code"]
    assert method == "引用消息" and not candidates
    assert not plugin._pending_image_record_commits


def test_code_in_list_and_both_search_paths(plugin):
    row = commit(plugin)
    assert summarize_record_for_list(row)["image_code"] == row["image_code"]
    assert record_matches_query(row, "", "", "", row["image_code"].lower())
    rows, _ = plugin.query_asset_records(keyword=row["image_code"].lower())
    assert [r["id"] for r in rows] == [row["id"]]


def test_diagnostics_copy_omits_request_secrets_and_javascript_is_valid(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for dashboard JavaScript verification")
    page = (Path(__file__).resolve().parents[1] / "pages/dashboard/index.html").read_text()
    for script in re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", page):
        if script.strip():
            subprocess.run([node, "--check"], input=script, text=True, capture_output=True, check=True)
    function = page.split("    function recordDiagnosticsText(r) {", 1)[1].split("    async function copyRecordDiagnostics", 1)[0]
    result = subprocess.run([node, "-e", "function recordDiagnosticsText(r) {" + function + "\nprocess.stdout.write(recordDiagnosticsText(" + json.dumps({
        "image_code": "ABC234", "final_prompt": "actual prompt", "original_prompt": "input",
        "prompt_selection": {"cos_pose": "pose1"}, "request_data": {"api_key": "SECRET_KEY", "headers": {"Authorization": "SECRET_AUTH"}},
    }) + "));"], text=True, capture_output=True, check=True).stdout
    assert "ABC234" in result and "actual prompt" in result and "pose1" in result
    assert "SECRET" not in result
