"""Framework conversation adapter tests."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_selfie_image.features.conversation_context import ConversationContextMixin


class _Conversation:
    def __init__(self, history: str):
        self.history = history


class _Conversations:
    async def get_curr_conversation_id(self, umo: str) -> str:
        return f"cid:{umo}"

    async def get_conversation(self, umo: str, cid: str) -> _Conversation:
        assert cid == f"cid:{umo}"
        return _Conversation(
            json.dumps(
                [
                    {"role": "system", "content": "ignored"},
                    {"role": "user", "content": "之前说过的话"},
                    {"role": "assistant", "content": "我记住了"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "参考这张图"},
                            {"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}},
                        ],
                    },
                ],
                ensure_ascii=False,
            )
        )


class _Context(ConversationContextMixin):
    def _bot_display_name(self) -> str:
        return "测试机器人"


def test_framework_history_is_read_without_local_storage(tmp_path: Path) -> None:
    context = _Context()
    context.data_dir = str(tmp_path)
    context.context = SimpleNamespace(conversations=_Conversations())
    event = SimpleNamespace(unified_msg_origin="private:42")

    rows = asyncio.run(context._framework_context_records(event))

    assert [row["content"] for row in rows] == ["之前说过的话", "我记住了", "参考这张图"]
    assert rows[-1]["image_count"] == 1
    assert rows[-1]["image_sources"] == ["https://example.invalid/a.png"]
    assert not (tmp_path / "conversation_context.json").exists()
    assert not hasattr(context, "_conversation_context")


def test_framework_history_failures_are_safe() -> None:
    context = _Context()
    context.context = SimpleNamespace(conversations=None)
    event = SimpleNamespace(unified_msg_origin="private:42")

    assert asyncio.run(context._framework_context_records(event)) == []
    assert context._recent_context_records(event) == []
    assert context._recent_context_image_sources(event) == []
