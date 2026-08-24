"""The message shape a caller parses, built from what Telethon hands over.

`_serialize` reads attributes rather than importing Telethon classes, which is
what makes it testable with plain stand-ins: if a field can be produced from a
duck-typed object here, it can be produced from the real one, and a Telethon
version that stops providing an attribute degrades to `None` instead of raising.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from telegram_ai_cli.ops._serialize import (
    ReadPointers,
    message_summary,
    peer_summary,
    reactions_summary,
)
from telegram_ai_cli.safety import PeerKind, PeerRef

GROUP = PeerRef(peer_id=-1001234567890, kind=PeerKind.GROUP, username=None, title="Marketing")
PUBLIC = PeerRef(peer_id=-1001234567890, kind=PeerKind.CHANNEL, username="publicchat", title="News")
DM = PeerRef(peer_id=111222333, kind=PeerKind.USER, username="someone", title="Someone")


class ReactionEmoji:
    """Named as Telethon names it: `reaction_kind` reads the class name."""

    def __init__(self, emoticon: str) -> None:
        self.emoticon = emoticon


class ReactionCustomEmoji:
    def __init__(self, document_id: int) -> None:
        self.document_id = document_id


class ReactionPaid:
    """Carries no emoji at all — the case a bare `emoji: null` cannot express."""


@dataclass
class FakeReactionCount:
    reaction: Any
    count: int
    chosen_order: int | None = None


@dataclass
class FakeReactions:
    results: list[FakeReactionCount] = field(default_factory=list)
    can_see_list: bool = False
    recent_reactions: list[Any] | None = None


@dataclass
class FakeReplyTo:
    reply_to_msg_id: int | None = None
    reply_to_top_id: int | None = None
    forum_topic: bool = False


@dataclass
class FakeMessage:
    id: int = 10
    message: str | None = "hello"
    date: datetime = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    out: bool = False
    sender_id: int | None = 111222333
    sender: Any = None
    edit_date: datetime | None = None
    views: int | None = None
    pinned: bool = False
    media: Any = None
    reply_to: FakeReplyTo | None = None
    forward: Any = None
    reactions: FakeReactions | None = None


# --- reactions -------------------------------------------------------------


def test_a_channel_surfaces_its_linked_direct_messages_chat_as_a_usable_id() -> None:
    from telethon import utils
    from telethon.tl.types import Channel, PeerChannel

    channel = Channel(
        id=42,
        title="News",
        photo=None,
        date=None,
        broadcast=True,
        megagroup=False,
        linked_monoforum_id=84,
    )

    assert peer_summary(channel)["linked_monoforum_id"] == utils.get_peer_id(PeerChannel(84))


def test_media_without_an_id_is_fingerprinted_by_its_content() -> None:
    from telegram_ai_cli.ops._serialize import media_fingerprint

    @dataclass
    class Location:
        latitude: float
        longitude: float

    first = FakeMessage(media=Location(1.25, 2.5))
    same = FakeMessage(media=Location(1.25, 2.5))
    other = FakeMessage(media=Location(9.0, 2.5))

    assert media_fingerprint(first) == media_fingerprint(same)
    assert media_fingerprint(first) != media_fingerprint(other)
    assert "opaque_sha256" in media_fingerprint(first)["parts"]


def test_a_message_without_reactions_reports_none_rather_than_an_empty_list() -> None:
    """`None` means "Telegram said nothing"; `[]` would mean "nobody reacted"."""
    assert message_summary(FakeMessage())["reactions"] is None


def test_reactions_are_counted_per_emoji() -> None:
    message = FakeMessage(
        reactions=FakeReactions(
            results=[
                FakeReactionCount(ReactionEmoji("👍"), count=3),
                FakeReactionCount(ReactionEmoji("🔥"), count=1, chosen_order=0),
            ]
        )
    )
    assert message_summary(message)["reactions"] == [
        {"kind": "emoji", "emoji": "👍", "custom_emoji_id": None, "count": 3, "chosen": False},
        {"kind": "emoji", "emoji": "🔥", "custom_emoji_id": None, "count": 1, "chosen": True},
    ]


def test_a_custom_emoji_reaction_reports_its_id_as_a_string() -> None:
    """A document id is 64-bit; JSON consumers in JavaScript lose the low bits."""
    message = FakeMessage(
        reactions=FakeReactions(results=[FakeReactionCount(ReactionCustomEmoji(5), count=2)])
    )
    row = message_summary(message)["reactions"][0]
    assert row["kind"] == "custom_emoji"
    assert row["custom_emoji_id"] == "5"
    assert row["emoji"] is None


def test_a_reaction_with_no_emoji_is_still_identified() -> None:
    """A paid star reaction and a blank one are not the same fact."""
    message = FakeMessage(reactions=FakeReactions(results=[FakeReactionCount(ReactionPaid(), 7)]))
    row = message_summary(message)["reactions"][0]
    assert row == {
        "kind": "paid",
        "emoji": None,
        "custom_emoji_id": None,
        "count": 7,
        "chosen": False,
    }


def test_reactions_summary_survives_an_object_it_does_not_recognise() -> None:
    assert reactions_summary(object()) is None


# --- permalinks ------------------------------------------------------------


def test_a_message_link_is_returned_for_a_private_supergroup() -> None:
    assert message_summary(FakeMessage(id=55), chat=GROUP)["link"] == (
        "https://t.me/c/1234567890/55"
    )


def test_a_public_chat_uses_its_username() -> None:
    assert message_summary(FakeMessage(id=55), chat=PUBLIC)["link"] == "https://t.me/publicchat/55"


def test_a_direct_message_has_no_permalink() -> None:
    """Honest `null` rather than a link that resolves to nothing."""
    assert message_summary(FakeMessage(id=55), chat=DM)["link"] is None


def test_without_a_chat_there_is_no_link_to_build() -> None:
    assert message_summary(FakeMessage(id=55))["link"] is None


def test_a_topic_message_carries_the_topic_in_its_link_and_in_a_field() -> None:
    message = FakeMessage(
        id=55, reply_to=FakeReplyTo(reply_to_msg_id=40, reply_to_top_id=12, forum_topic=True)
    )
    row = message_summary(message, chat=GROUP)
    assert row["topic_id"] == 12
    assert row["link"] == "https://t.me/c/1234567890/12/55"
    # The reply target is still the message replied to, not the topic.
    assert row["reply_to_msg_id"] == 40


def test_a_reply_outside_a_forum_is_not_a_topic() -> None:
    row = message_summary(FakeMessage(reply_to=FakeReplyTo(reply_to_msg_id=40)), chat=GROUP)
    assert row["topic_id"] is None


# --- pinned and read state -------------------------------------------------


def test_pinned_is_reported_per_message() -> None:
    assert message_summary(FakeMessage(pinned=True))["pinned"] is True
    assert message_summary(FakeMessage())["pinned"] is False


def test_without_read_pointers_both_read_fields_are_unknown() -> None:
    """Unknown is `null`. `false` would claim the message is unread."""
    row = message_summary(FakeMessage())
    assert row["read_by_me"] is None
    assert row["read_by_peer"] is None


def test_an_incoming_message_is_read_by_me_up_to_the_inbox_pointer() -> None:
    pointers = ReadPointers(inbox_max_id=10, outbox_max_id=0, peer_receipts=True)
    assert message_summary(FakeMessage(id=10), read=pointers)["read_by_me"] is True
    assert message_summary(FakeMessage(id=11), read=pointers)["read_by_me"] is False
    # It is not mine to be read by the other side.
    assert message_summary(FakeMessage(id=10), read=pointers)["read_by_peer"] is None


def test_an_outgoing_message_is_read_by_the_peer_up_to_the_outbox_pointer() -> None:
    pointers = ReadPointers(inbox_max_id=0, outbox_max_id=20, peer_receipts=True)
    assert message_summary(FakeMessage(id=20, out=True), read=pointers)["read_by_peer"] is True
    assert message_summary(FakeMessage(id=21, out=True), read=pointers)["read_by_peer"] is False
    assert message_summary(FakeMessage(id=20, out=True), read=pointers)["read_by_me"] is None


def test_where_telegram_reports_no_receipts_the_field_stays_unknown() -> None:
    """A broadcast channel has no reader pointer: say so rather than guess."""
    pointers = ReadPointers(inbox_max_id=99, outbox_max_id=99, peer_receipts=False)
    assert message_summary(FakeMessage(id=1, out=True), read=pointers)["read_by_peer"] is None
