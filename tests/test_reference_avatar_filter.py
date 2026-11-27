from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_selfie_image.core.utils import (
    REFERENCE_IMAGE_COMMANDS,
    extract_command_message,
    extract_command_mention_ids,
    extract_image_sources_from_event,
)
from astrbot_plugin_selfie_image.features.reference_collector import ReferenceCollector


class At:
    def __init__(self, qq: str, avatar: str = "") -> None:
        self.qq = qq
        self.avatar = avatar


class Image:
    def __init__(self, url: str) -> None:
        self.url = url
        self.path = ""


def event_with_message(*components):
    return SimpleNamespace(
        message_obj=SimpleNamespace(message=list(components), quote=None),
        message=None,
        raw_message=None,
    )


def test_regular_reference_collection_filters_bot_and_triggering_user_at_avatars():
    event = event_with_message(At("bot-qq"), At("sender-qq"))
    collector = ReferenceCollector(
        max_bytes=1024 * 1024,
        bot_ids=["bot-qq"],
        at_avatar_filter_ids=["sender-qq"],
        include_at_avatar=True,
    )

    buckets = collector.collect_source_buckets(event)

    assert buckets["at_avatar"] == []


def test_group_selfie_can_keep_bot_and_sender_at_avatars():
    event = event_with_message(At("bot-qq"), At("sender-qq"))
    collector = ReferenceCollector(
        max_bytes=1024 * 1024,
        bot_ids=["bot-qq"],
        at_avatar_filter_ids=["sender-qq"],
        include_at_avatar=True,
        filter_at_avatars=False,
    )

    buckets = collector.collect_source_buckets(event)

    assert buckets["at_avatar"] == [
        "https://q4.qlogo.cn/headimg_dl?dst_uin=bot-qq&spec=640",
        "https://q4.qlogo.cn/headimg_dl?dst_uin=sender-qq&spec=640",
    ]


def test_regular_collection_keeps_other_users_with_similar_ids():
    event = event_with_message(At("prefix-bot-qq-suffix"), At("another-sender-qq"))
    collector = ReferenceCollector(
        max_bytes=1024 * 1024,
        bot_ids=["bot-qq"],
        at_avatar_filter_ids=["sender-qq"],
        include_at_avatar=True,
    )

    buckets = collector.collect_source_buckets(event)

    assert buckets["at_avatar"] == [
        "https://q4.qlogo.cn/headimg_dl?dst_uin=prefix-bot-qq-suffix&spec=640",
        "https://q4.qlogo.cn/headimg_dl?dst_uin=another-sender-qq&spec=640",
    ]


def test_avatar_filter_does_not_remove_an_explicit_image_with_same_qq_url():
    explicit = "https://q4.qlogo.cn/headimg_dl?dst_uin=sender-qq&spec=640"
    event = event_with_message(At("sender-qq"), Image(explicit))
    collector = ReferenceCollector(
        max_bytes=1024 * 1024,
        at_avatar_filter_ids=["sender-qq"],
        include_at_avatar=True,
    )

    buckets = collector.collect_source_buckets(event)

    assert buckets["at_avatar"] == []
    assert buckets["message"] == [explicit]


def test_at_component_fields_are_not_scanned_when_avatar_collection_is_disabled():
    avatar = "https://q4.qlogo.cn/headimg_dl?dst_uin=sender-qq&spec=640"
    event = event_with_message(At("sender-qq", avatar=avatar))
    collector = ReferenceCollector(max_bytes=1024 * 1024, include_at_avatar=False)

    buckets = collector.collect_source_buckets(event)

    assert all(not values for values in buckets.values())


class Plain:
    def __init__(self, text: str) -> None:
        self.text = text


def official_event(*components, raw_mapping=False, mentions=None):
    author = {
        "id": "sender-platform-id",
        "member_openid": "sender-openid",
        "avatar": "https://q.qlogo.cn/qqapp/app-id/sender-openid/640",
    }
    if mentions is None:
        mentions = [{
            "id": "bot-openid", "is_you": True,
            "avatar": "https://q.qlogo.cn/qqapp/app-id/bot-openid/640",
        }]
    raw_data = {"author": author, "mentions": mentions, "attachments": []}
    raw_message = raw_data if raw_mapping else SimpleNamespace(
        author=SimpleNamespace(**author),
        mentions=[SimpleNamespace(**item) for item in mentions],
        attachments=[], raw_data=raw_data,
    )
    event = event_with_message(At("bot-openid"), Plain("看看COS 修女服 3"), *components)
    event.message_obj.self_id = "bot-openid"
    event.message_obj.sender = SimpleNamespace(user_id="sender-openid")
    event.message_obj.raw_message = raw_message
    event.get_sender_id = lambda: "sender-openid"
    return event


@pytest.mark.parametrize("raw_mapping", [False, True])
@pytest.mark.parametrize("include_at_avatar", [False, True])
def test_official_author_and_bot_mention_metadata_are_not_message_images(raw_mapping, include_at_avatar):
    event = official_event(raw_mapping=raw_mapping)
    collector = ReferenceCollector(
        max_bytes=1024 * 1024,
        bot_ids=["bot-openid"],
        at_avatar_filter_ids=["sender-openid"],
        include_at_avatar=include_at_avatar,
    )

    buckets = collector.collect_source_buckets(event)

    assert all(not values for values in buckets.values())
    assert extract_image_sources_from_event(event, include_at_avatar=False) == []


@pytest.mark.parametrize("raw_mapping", [False, True])
@pytest.mark.parametrize("filter_at_avatars", [False, True])
def test_official_mentions_keep_provenance_and_use_real_avatar_urls(raw_mapping, filter_at_avatars):
    mentions = [
        {"id": "bot-openid", "is_you": True, "avatar": "https://cdn.example/bot.png"},
        {"id": "sender-platform-id", "avatar": "https://cdn.example/sender.png"},
        {"id": "other-openid", "avatar": "https://cdn.example/other.png"},
    ]
    event = official_event(
        At("sender-platform-id"), At("other-openid"),
        raw_mapping=raw_mapping, mentions=mentions,
    )
    collector = ReferenceCollector(
        max_bytes=1024 * 1024,
        bot_ids=["bot-openid"],
        at_avatar_filter_ids=["sender-openid"],
        include_at_avatar=True,
        filter_at_avatars=filter_at_avatars,
    )

    buckets = collector.collect_source_buckets(event)

    assert buckets["message"] == []
    assert set(buckets["at_avatar"]) == {
        "https://cdn.example/sender.png", "https://cdn.example/other.png",
    }


def test_official_metadata_filter_preserves_explicit_and_quoted_images():
    avatar = "https://q.qlogo.cn/qqapp/app-id/sender-openid/640"
    event = official_event(Image(avatar), Image("https://cdn.example/attachment.png"))
    event.message_obj.quote = SimpleNamespace(message=[Image("https://cdn.example/quoted.png")])
    collector = ReferenceCollector(
        max_bytes=1024 * 1024,
        bot_ids=["bot-openid"],
        at_avatar_filter_ids=["sender-openid"],
        include_at_avatar=True,
    )

    buckets = collector.collect_source_buckets(event)

    assert set(buckets["message"]) == {avatar, "https://cdn.example/attachment.png"}
    assert buckets["quote"] == ["https://cdn.example/quoted.png"]
    assert buckets["at_avatar"] == []


def test_official_c2c_placeholder_at_is_filtered():
    event = event_with_message(At("qq_official"))
    event.message_obj.self_id = "unknown_selfid"
    collector = ReferenceCollector(
        max_bytes=1024 * 1024,
        bot_ids=["unknown_selfid"],
        include_at_avatar=True,
    )

    assert collector.collect_source_buckets(event)["at_avatar"] == []


def test_official_is_you_filters_bot_alias_before_converting_to_avatar_url():
    event = official_event(mentions=[{
        "id": "bot-openid", "is_you": True,
        "avatar": "https://cdn.example/profile.png",
    }])
    collector = ReferenceCollector(max_bytes=1024 * 1024, include_at_avatar=True)

    buckets = collector.collect_source_buckets(event)

    assert all(not values for values in buckets.values())


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("identity", [False, True])
def test_cos_command_prefers_explicit_attachment_then_selected_identity(tmp_path, monkeypatch, attached, identity):
    from astrbot_plugin_selfie_image.core.models import AICatConfig
    from astrbot_plugin_selfie_image.core.providers import ImageReference
    from astrbot_plugin_selfie_image.features import reference_collector
    from astrbot_plugin_selfie_image.features.persona import PersonaManager
    from astrbot_plugin_selfie_image.main import SelfieImagePlugin
    from astrbot_plugin_selfie_image.prompts.preset import ImagePresetManager

    plugin = object.__new__(SelfieImagePlugin)
    plugin.context = SimpleNamespace()
    plugin.config = AICatConfig.from_dict({})
    plugin.persona = PersonaManager(tmp_path)
    plugin.presets = ImagePresetManager(tmp_path)
    persona_ref = ImageReference(b"selected-identity") if identity else None
    plugin._persona_identity_reference = lambda **_kwargs: persona_ref
    plugin._quota_error_message = lambda *_args: ""
    plugin._rate_limit_error_message = lambda *_args: ""
    plugin._recent_context_image_sources_async = AsyncMock(return_value=[])
    plugin._build_contextual_progress_text = AsyncMock(return_value="progress")
    plugin._image_batch_queue_expected = lambda *_args: False
    plugin._background_selfie_batches = AsyncMock(return_value={})

    @asynccontextmanager
    async def reference_session():
        yield object()

    plugin._reference_client_session = reference_session
    captured = {}
    plugin.start_command_image_task = lambda *_args, **kwargs: captured.update(kwargs)
    downloads = []

    async def download(sources, *_args, **_kwargs):
        downloads.extend(sources)
        return [ImageReference(source.encode(), source_url=source) for source in sources], 0

    monkeypatch.setattr(reference_collector, "download_sources_as_references", download)
    attachment_url = "https://cdn.example/request-image.png"
    event = official_event(*([Image(attachment_url)] if attached else []))
    event.message_str = "/看看COS 修女服 1"
    event.message_obj.message_str = event.message_str
    event.plain_result = lambda value: value

    async def scenario():
        replies = [reply async for reply in plugin.cmd_look_cos(event)]
        assert replies
        await captured["runner"]("cos-filter-probe")

    asyncio.run(scenario())

    assert downloads == ([attachment_url] if attached else [])
    expected = [attachment_url.encode()] if attached else ([b"selected-identity"] if identity else [])
    assert [ref.data for ref in captured["reference_images"]] == expected
    selection = plugin._background_selfie_batches.call_args.kwargs["cos_selection"]
    assert [ref.data for ref in selection.refs] == expected
    assert selection.identity_source == ("message" if attached else "persona" if identity else "none")


@pytest.mark.parametrize("command", REFERENCE_IMAGE_COMMANDS)
@pytest.mark.parametrize("filter_at_avatars", [False, True])
@pytest.mark.parametrize("mention_bot_again", [False, True])
def test_command_mentions_use_position_instead_of_account_filter(command, filter_at_avatars, mention_bot_again):
    explicit = ["other-qq", "sender-qq"] + (["bot-qq"] if mention_bot_again else [])
    event = event_with_message(
        At("bot-qq"), At("before-command"), Plain(f" /{command} "),
        *(At(identity) for identity in explicit),
    )
    # message_str may drop all At components; the ordered chain still has them.
    event.message_str = f"/{command}"
    collector = ReferenceCollector(
        max_bytes=1024 * 1024, include_at_avatar=True,
        bot_ids=["bot-qq"], at_avatar_filter_ids=["sender-qq"],
        filter_at_avatars=filter_at_avatars,
    )
    expected = [f"https://q4.qlogo.cn/headimg_dl?dst_uin={identity}&spec=640" for identity in explicit]

    assert extract_command_mention_ids(event) == explicit
    assert collector.collect_source_buckets(event)["at_avatar"] == expected
    assert extract_image_sources_from_event(event, include_at_avatar=True) == expected


@pytest.mark.parametrize("markup", [
    "[CQ:at,qq={identity}]", "[At:{identity}]", "<@!{identity}>",
    "<@{identity}>", "<at:{identity}>", "@Bot({identity})", "@{identity}",
    '<qqbot-at-user id="{identity}" />',
])
@pytest.mark.parametrize("command", ["合影", "看看COS", "生图", "nai5"])
def test_protocol_mention_markup_keeps_the_second_bot_occurrence(markup, command):
    bot, other = "12345", "67890"
    before = markup.format(identity=bot)
    after = f"{markup.format(identity=other)} {markup.format(identity=bot)}"
    event = event_with_message()
    event.message_str = f"{before} /{command} {after}"

    assert extract_command_message(event, command) == after
    assert extract_command_mention_ids(event) == [other, bot]
    sources = ReferenceCollector(max_bytes=1024, include_at_avatar=True, bot_ids=[bot]).collect_source_buckets(event)
    assert sources["at_avatar"] == [
        f"https://q4.qlogo.cn/headimg_dl?dst_uin={other}&spec=640",
        f"https://q4.qlogo.cn/headimg_dl?dst_uin={bot}&spec=640",
    ]


@pytest.mark.parametrize("raw_mapping", [False, True])
@pytest.mark.parametrize("mention_bot_again", [False, True])
def test_official_raw_content_restores_explicit_bot_removed_by_adapter(raw_mapping, mention_bot_again):
    event = official_event(raw_mapping=raw_mapping, mentions=[
        {"id": "bot-openid", "is_you": True, "avatar": "https://cdn.example/bot.png"},
        {"id": "other-openid", "avatar": "https://cdn.example/other.png"},
        {"id": "unmentioned", "avatar": "https://cdn.example/unmentioned.png"},
    ])
    content = "<@!bot-openid> /合影 <@!other-openid>"
    if mention_bot_again:
        content += " <@!bot-openid>"
    raw = event.message_obj.raw_message
    if raw_mapping:
        raw["content"] = content
    else:
        raw.content = content
        raw.raw_data["content"] = content
    # Reproduce the live official adapter, which strips every @bot occurrence.
    event.message_obj.message = [At("bot-openid"), Plain("合影 <@!other-openid>")]
    event.message_str = "合影 <@!other-openid>"
    collector = ReferenceCollector(max_bytes=1024, include_at_avatar=True, bot_ids=["bot-openid"])

    expected = ["https://cdn.example/other.png"]
    if mention_bot_again:
        expected.append("https://cdn.example/bot.png")
    assert collector.collect_source_buckets(event)["at_avatar"] == expected


def test_onebot_raw_chain_and_dict_mentions_preserve_position():
    event = event_with_message(At("12345"), Plain("合影"))
    event.message_obj.raw_message = {"message": [
        {"type": "at", "data": {"qq": "12345"}},
        {"type": "text", "data": {"text": " 合影 "}},
        {"type": "at", "data": {"qq": "67890"}},
        {"type": "at", "data": {"qq": "12345"}},
    ]}

    assert extract_command_mention_ids(event) == ["67890", "12345"]
    sources = ReferenceCollector(max_bytes=1024, include_at_avatar=True, bot_ids=["12345"]).collect_source_buckets(event)
    assert sources["at_avatar"] == [
        "https://q4.qlogo.cn/headimg_dl?dst_uin=67890&spec=640",
        "https://q4.qlogo.cn/headimg_dl?dst_uin=12345&spec=640",
    ]


@pytest.mark.parametrize("raw_type", ["text", "chain"])
def test_direct_raw_message_preserves_mentions_removed_from_normalized_chain(raw_type):
    event = event_with_message(At("12345"), Plain("合影"))
    raw = "[CQ:at,qq=12345] 合影 [CQ:at,qq=67890] [CQ:at,qq=12345]"
    event.raw_message = raw if raw_type == "text" else [
        {"type": "at", "data": {"qq": "12345"}},
        {"type": "text", "data": {"text": " 合影 "}},
        {"type": "at", "data": {"qq": "67890"}},
        {"type": "at", "data": {"qq": "12345"}},
    ]
    assert extract_command_mention_ids(event) == ["67890", "12345"]


def test_quoted_mentions_do_not_add_or_override_current_command_avatars():
    event = event_with_message(At("bot"), Plain(" 合影 "), At("other", avatar="https://cdn.example/current.png"))
    event.message_obj.quote = SimpleNamespace(message=[
        At("quoted"), At("other", avatar="https://cdn.example/quoted-avatar.png"),
        Image("https://cdn.example/quoted-image.png"),
    ])
    sources = ReferenceCollector(max_bytes=1024, include_at_avatar=True).collect_source_buckets(event)

    assert sources["at_avatar"] == ["https://cdn.example/current.png"]
    assert sources["quote"] == ["https://cdn.example/quoted-image.png"]


@pytest.mark.parametrize("command", ["生图加黑", "生图加白", "生图释放"])
@pytest.mark.parametrize("dict_segments", [False, True])
def test_access_commands_accept_explicit_bot_after_command_and_ignore_prefix_mentions(command, dict_segments):
    from astrbot_plugin_selfie_image.main import SelfieImagePlugin

    event = event_with_message(At("12345"), At("before"), Plain(f" {command} "), At("12345"))
    event.get_self_id = lambda: "12345"
    if dict_segments:
        event.message_obj.message = [
            {"type": "at", "data": {"qq": "12345"}},
            {"type": "at", "data": {"qq": "before"}},
            {"type": "text", "data": {"text": f" {command} "}},
            {"type": "at", "data": {"qq": "12345"}},
        ]
    assert SelfieImagePlugin._parse_access_user_command(event, "测试") == ("12345", "测试")


@pytest.mark.parametrize("markup", ['<@!12345>', '<qqbot-at-user id="12345" />'])
def test_official_access_mention_does_not_leak_markup_into_reason(markup):
    from astrbot_plugin_selfie_image.main import SelfieImagePlugin

    event = event_with_message(At("12345"), Plain(" 生图加黑 "), At("12345"))
    event.message_str = f"{markup} 生图加黑 {markup} 测试"
    event.get_self_id = lambda: "12345"
    payload = extract_command_message(event, "生图加黑")
    assert SelfieImagePlugin._parse_access_user_command(event, payload) == ("12345", "测试")


def test_many_wake_mentions_do_not_hide_the_command():
    event = event_with_message()
    event.message_str = " ".join(["@12345"] * 300) + " /合影 @67890"
    assert extract_command_mention_ids(event) == ["67890"]
    assert extract_command_message(event, "合影") == "@67890"


@pytest.mark.parametrize("handler,command", [
    ("cmd_group_selfie", "合影"), ("cmd_group_selfie", "合照"),
    ("cmd_selfie", "自拍"), ("cmd_look_cos", "看看COS"),
    ("cmd_look_legs", "看看腿"), ("cmd_look_you", "看看你"),
])
@pytest.mark.parametrize("mention_bot_again", [False, True])
def test_selfie_commands_download_only_mentions_after_command(tmp_path, monkeypatch, handler, command, mention_bot_again):
    from astrbot_plugin_selfie_image.core.models import AICatConfig
    from astrbot_plugin_selfie_image.core.providers import ImageReference
    from astrbot_plugin_selfie_image.features import reference_collector
    from astrbot_plugin_selfie_image.features.persona import PersonaManager
    from astrbot_plugin_selfie_image.main import SelfieImagePlugin
    from astrbot_plugin_selfie_image.prompts.preset import ImagePresetManager

    plugin = object.__new__(SelfieImagePlugin)
    plugin.context = SimpleNamespace()
    plugin.config = AICatConfig.from_dict({})
    plugin.persona = PersonaManager(tmp_path)
    plugin.presets = ImagePresetManager(tmp_path)
    plugin._persona_identity_reference = lambda **_kwargs: ImageReference(b"selected-identity")
    plugin._quota_error_message = lambda *_args: ""
    plugin._rate_limit_error_message = lambda *_args: ""
    plugin._recent_context_image_sources_async = AsyncMock(return_value=[])
    plugin._build_contextual_progress_text = AsyncMock(return_value="progress")
    plugin._ensure_daily_selfie_schedule = AsyncMock()
    plugin._image_batch_queue_expected = lambda *_args: False
    plugin._background_selfie_batches = AsyncMock(return_value={})

    @asynccontextmanager
    async def reference_session():
        yield object()

    plugin._reference_client_session = reference_session
    captured = {}
    plugin.start_command_image_task = lambda *_args, **kwargs: captured.update(kwargs)
    downloads = []

    async def download(sources, *_args, **_kwargs):
        downloads.extend(sources)
        return [ImageReference(source.encode(), source_url=source) for source in sources], 0

    monkeypatch.setattr(reference_collector, "download_sources_as_references", download)
    after = [At("other-qq")] + ([At("bot-qq")] if mention_bot_again else [])
    event = event_with_message(At("bot-qq"), Plain(f" {command} "), *after)
    event.message_obj.self_id = "bot-qq"
    event.message_str = f"/{command}"
    event.get_sender_id = lambda: "sender-qq"
    event.plain_result = lambda value: value

    async def scenario():
        assert [reply async for reply in getattr(plugin, handler)(event)]
        await captured["runner"]("mention-probe")

    asyncio.run(scenario())

    explicit_ids = ["other-qq"] + (["bot-qq"] if mention_bot_again else [])
    expected = [f"https://q4.qlogo.cn/headimg_dl?dst_uin={identity}&spec=640" for identity in explicit_ids]
    assert downloads == expected
    assert [ref.data for ref in plugin._background_selfie_batches.call_args.args[3]] == [url.encode() for url in expected]
    if command in {"合影", "合照"}:
        assert [ref.data for ref in captured["reference_images"]] == [b"selected-identity", *[url.encode() for url in expected]]
