"""Quoted-text-as-prompt coverage.

A quoted image already acts as a reference.  These tests pin the matching
behaviour for quoted *text*: a bare ``/画`` should use the quoted body, while
task/model/preset management commands must keep ignoring it.
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astrbot_plugin_selfie_image.core.utils import (  # noqa: E402
    QUOTE_TEXT_MAX_CHARS,
    extract_command_message,
    extract_quote_text,
)


class _Plain:
    def __init__(self, text: str) -> None:
        self.text = text


class _Image:
    """Stands in for an image component: no usable text."""


class _Reply:
    def __init__(self, chain, message_str: str = "") -> None:
        self.chain = chain
        self.message_str = message_str
        # Mirrors AstrBot's deprecated field so tests can prove it is ignored.
        self.text = ""


def _event(message_str: str, message=None, quote=None, raw_message=None):
    message_obj = SimpleNamespace(
        message_str=message_str,
        message=message if message is not None else [_Plain(message_str)],
        quote=quote,
        raw_message=raw_message,
    )
    return SimpleNamespace(message_str=message_str, message_obj=message_obj)


class QuoteTextExtractionTest(unittest.TestCase):
    def test_reads_text_from_reply_chain(self) -> None:
        event = _event("/画", quote=_Reply([_Plain("白色长袖衬衫配藏蓝百褶裙")]))
        self.assertEqual(extract_quote_text(event), "白色长袖衬衫配藏蓝百褶裙")

    def test_reads_astrbot_reply_message_str(self) -> None:
        """AstrBot's Reply exposes the parsed body in ``message_str``."""
        event = _event("/画", quote=_Reply([], message_str="坐在窗边看雨"))
        self.assertEqual(extract_quote_text(event), "坐在窗边看雨")

    def test_deprecated_reply_text_field_does_not_shadow_body(self) -> None:
        reply = _Reply([_Plain("海边黄昏")], message_str="海边黄昏")
        event = _event("/画", quote=reply)
        self.assertEqual(extract_quote_text(event), "海边黄昏")

    def test_reads_onebot_raw_message_dict(self) -> None:
        raw = {
            "type": "reply",
            "data": {"id": "42"},
            "message": [{"type": "text", "data": {"text": "坐在窗边看雨"}}],
        }
        event = _event("/画", quote=None, raw_message={"quote": raw})
        self.assertEqual(extract_quote_text(event), "坐在窗边看雨")

    def test_skips_media_components(self) -> None:
        event = _event("/画", quote=_Reply([_Image(), _Plain("露肩白礼服"), _Image()]))
        self.assertEqual(extract_quote_text(event), "露肩白礼服")

    def test_strips_cq_codes_and_placeholder_tokens(self) -> None:
        quote = _Reply(
            [_Plain("[CQ:image,file=a.jpg]"), _Plain("露肩白礼服"), _Plain("图片")]
        )
        event = _event("/画", quote=quote)
        self.assertEqual(extract_quote_text(event), "露肩白礼服")

    def test_keeps_sentence_that_merely_starts_with_media_word(self) -> None:
        event = _event("/画", quote=_Reply([_Plain("图片里的女孩穿白色连衣裙")]))
        self.assertEqual(extract_quote_text(event), "图片里的女孩穿白色连衣裙")

    def test_collapses_multiline_body_into_one_line(self) -> None:
        event = _event("/画", quote=_Reply([_Plain("第一行\n第二行")]))
        self.assertEqual(extract_quote_text(event), "第一行 第二行")

    def test_truncates_overlong_body(self) -> None:
        event = _event("/画", quote=_Reply([_Plain("云" * 9000)]))
        self.assertEqual(len(extract_quote_text(event)), QUOTE_TEXT_MAX_CHARS)

    def test_returns_empty_without_quote(self) -> None:
        self.assertEqual(extract_quote_text(_event("/画")), "")
        self.assertEqual(extract_quote_text(None), "")


class QuoteTextAsCommandArgumentTest(unittest.TestCase):
    def test_bare_generation_command_uses_quoted_text(self) -> None:
        event = _event("/画", quote=_Reply([_Plain("海边黄昏的白色长裙")]))
        self.assertEqual(
            extract_command_message(event, ("画", "生图"), include_quote_text=True),
            "海边黄昏的白色长裙",
        )

    def test_inline_text_wins_over_quoted_text(self) -> None:
        event = _event("/画 雨夜霓虹", quote=_Reply([_Plain("海边黄昏")]))
        self.assertEqual(
            extract_command_message(event, ("画", "生图"), include_quote_text=True),
            "雨夜霓虹",
        )

    def test_image_only_quote_yields_no_prompt(self) -> None:
        event = _event("/画", quote=_Reply([_Image()]))
        self.assertEqual(
            extract_command_message(event, ("画", "生图"), include_quote_text=True), ""
        )

    def test_quote_is_ignored_by_default(self) -> None:
        """Task / model / preset commands must never read a quoted body."""
        event = _event("/生图取消", quote=_Reply([_Plain("3")]))
        self.assertEqual(extract_command_message(event, "生图取消"), "")

    def test_fallback_still_used_when_nothing_available(self) -> None:
        event = _event("/画")
        self.assertEqual(
            extract_command_message(event, ("画", "生图"), "默认提示词"), "默认提示词"
        )


if __name__ == "__main__":
    unittest.main()
