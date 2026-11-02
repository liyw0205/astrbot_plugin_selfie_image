from __future__ import annotations

import json
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_selfie_image.features.conversation_context import ConversationContextMixin


class _Context(ConversationContextMixin):
    def _bot_display_name(self) -> str:
        return "测试机器人"


def _context(tmp_path: Path) -> _Context:
    value = _Context()
    value.data_dir = str(tmp_path)
    value.raw_config = {"image": {"persist_context": True}}
    value._context_lock = threading.RLock()
    value._conversation_context = OrderedDict()
    value._context_max_messages = 40
    value._context_max_sessions = 100
    return value


def test_sync_provider_context_uses_official_history_without_media_urls(tmp_path: Path) -> None:
    context = _context(tmp_path)
    event = SimpleNamespace(unified_msg_origin="private:42")
    request = SimpleNamespace(
        session_id="private:42",
        contexts=[
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": "之前说过的话"},
            {"role": "assistant", "content": "我记住了"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "参考这张图"},
                    {"type": "image_url", "image_url": {"url": "https://secret.invalid/a.png"}},
                ],
            },
        ],
    )

    assert context._sync_provider_context(event, request) == 3
    view = context.get_conversation_context(event)
    assert [row["source"] for row in view["messages"]] == ["astrbot"] * 3
    assert view["messages"][-1]["image_count"] == 1
    saved = (tmp_path / "conversation_context.json").read_text(encoding="utf-8")
    assert "secret.invalid" not in saved
    assert json.loads(saved)["sessions"]["private:42"][-1]["image_count"] == 1

    request.contexts = [{"role": "user", "content": "重试后的新消息"}]
    assert context._sync_provider_context(event, request) == 1
    assert [row["content"] for row in context.get_conversation_context(event)["messages"]] == [
        "重试后的新消息"
    ]


def test_llm_response_context_filters_chunks_and_tool_rounds(tmp_path: Path) -> None:
    context = _context(tmp_path)
    event = SimpleNamespace(unified_msg_origin="group:7", message_obj=SimpleNamespace(message_id="m-1"))

    assert not context._record_llm_response_context(
        event, SimpleNamespace(role="assistant", completion_text="分片", is_chunk=True, id="chunk")
    )
    assert not context._record_llm_response_context(
        event,
        SimpleNamespace(
            role="assistant",
            completion_text="工具中间轮",
            tools_call_name=["generate_image"],
            tools_call_extra_content={},
            id="tool",
        ),
    )
    assert context._record_llm_response_context(
        event, SimpleNamespace(role="assistant", completion_text="正式回复", id="reply-1")
    )

    rows = context.get_conversation_context(event)["messages"]
    assert len(rows) == 1
    assert rows[0]["source"] == "llm_response"
    assert rows[0]["content"] == "正式回复"
