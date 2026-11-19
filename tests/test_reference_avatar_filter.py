from __future__ import annotations

from types import SimpleNamespace

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
