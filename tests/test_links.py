"""`t.me` links, in both directions.

A permalink is how a person hands a message to an agent ("look at this") and how
an agent hands one back ("here is what I found"). Both directions have to work
on the same three shapes Telegram actually produces: a public chat, a private
one (`/c/<internal id>`), and a forum topic, where the middle number is the
topic and the last one is the message.

Parsing is a pure function on purpose — no network, no Telethon — because the
number in a link decides which message an operation touches, and that decision
must be testable exhaustively.
"""

from __future__ import annotations

import pytest

from telegram_ai_cli_mcp.links import format_message_link, parse_telegram_link

# --- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "https://t.me/publicchat/123",
        "http://t.me/publicchat/123",
        "t.me/publicchat/123",
        "https://telegram.me/publicchat/123",
        "https://t.me/publicchat/123/",
        "https://t.me/publicchat/123?single",
    ],
)
def test_a_public_message_link_keeps_its_number(text: str) -> None:
    """The number is the point: resolving the link as a chat loses it."""
    link = parse_telegram_link(text)
    assert link is not None
    assert link.chat == "publicchat"
    assert link.message_id == 123
    assert link.topic_id is None
    assert link.private is False


def test_a_public_chat_link_has_no_message() -> None:
    link = parse_telegram_link("https://t.me/publicchat")
    assert link is not None
    assert link.chat == "publicchat"
    assert link.message_id is None


def test_a_private_link_becomes_the_marked_chat_id() -> None:
    """`/c/<internal>` is the same chat as `-100<internal>` everywhere else."""
    link = parse_telegram_link("https://t.me/c/1234567890/55")
    assert link is not None
    assert link.chat == -1001234567890
    assert link.message_id == 55
    assert link.private is True


def test_a_private_chat_link_without_a_message() -> None:
    link = parse_telegram_link("https://t.me/c/1234567890")
    assert link is not None
    assert link.chat == -1001234567890
    assert link.message_id is None


@pytest.mark.parametrize(
    ("text", "chat", "topic", "message"),
    [
        ("https://t.me/c/1234567890/12/55", -1001234567890, 12, 55),
        ("https://t.me/mygroup/12/55", "mygroup", 12, 55),
        ("https://t.me/mygroup/55?thread=12", "mygroup", 12, 55),
        ("https://t.me/c/1234567890/55?thread=12", -1001234567890, 12, 55),
    ],
)
def test_a_topic_link_separates_the_topic_from_the_message(
    text: str, chat: str | int, topic: int, message: int
) -> None:
    """In a forum the middle number is the topic, not the message."""
    link = parse_telegram_link(text)
    assert link is not None
    assert (link.chat, link.topic_id, link.message_id) == (chat, topic, message)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "not a link",
        "@publicchat",
        "-1001234567890",
        "https://example.invalid/publicchat/1",
        "https://t.me/+AAAAAAAAAAAAAAAAAAAAAA",  # an invite: whois describes it
        "https://t.me/joinchat/AAAAAAAAAAAAAAAAAAAAAA",
        "https://t.me/c/notanumber/5",
        # Telegram's own deep links: none of these names a chat, and
        # `t.me/login/12345` would send a lookup after a login code.
        "https://t.me/share/123",
        "https://t.me/login/12345",
        "https://t.me/addlist/AAAAAAAA",
        "https://t.me/proxy/1",
        "https://t.me/CONTACT/9",
    ],
)
def test_what_is_not_a_message_link_is_declined(text: str) -> None:
    """Declining is not failing: the caller falls back to its own resolution."""
    assert parse_telegram_link(text) is None


def test_an_absurd_message_number_is_declined_rather_than_believed() -> None:
    assert parse_telegram_link("https://t.me/publicchat/999999999999") is None


def test_a_path_longer_than_telegram_produces_is_declined() -> None:
    """Ignoring the tail would mean addressing a message the link does not name."""
    assert parse_telegram_link("https://t.me/publicchat/12/34/56") is None
    assert parse_telegram_link("https://t.me/c/1234567890/12/34/56") is None


def test_a_comment_link_is_recorded_rather_than_resolved() -> None:
    """`?comment=` addresses the channel's discussion group, a different chat.

    Reading it as "message 123 in @publicchat" would act on the post the
    comment hangs off — the wrong message, in the wrong chat. The caller
    refuses it; the parser makes that possible by keeping the number.
    """
    link = parse_telegram_link("https://t.me/publicchat/123?comment=456")
    assert link is not None
    assert link.comment_id == 456
    assert link.message_id == 123


# --- formatting ------------------------------------------------------------


def test_a_public_chat_gets_a_username_permalink() -> None:
    assert format_message_link(username="publicchat", chat_id=-1001234567890, message_id=7) == (
        "https://t.me/publicchat/7"
    )


def test_a_private_supergroup_gets_a_c_permalink() -> None:
    assert format_message_link(username=None, chat_id=-1001234567890, message_id=7) == (
        "https://t.me/c/1234567890/7"
    )


def test_a_topic_message_carries_its_topic() -> None:
    assert format_message_link(username=None, chat_id=-1001234567890, message_id=7, topic_id=3) == (
        "https://t.me/c/1234567890/3/7"
    )


@pytest.mark.parametrize("chat_id", [111222333, -4242, None])
def test_a_chat_with_no_permalink_says_so_instead_of_inventing_one(chat_id: int | None) -> None:
    """One-to-one chats and basic groups have no public message address."""
    assert format_message_link(username=None, chat_id=chat_id, message_id=7) is None


def test_the_two_directions_agree() -> None:
    """Anything this project prints must parse back to what it came from."""
    printed = format_message_link(username=None, chat_id=-1001234567890, message_id=7, topic_id=3)
    assert printed is not None
    link = parse_telegram_link(printed)
    assert link is not None
    assert (link.chat, link.topic_id, link.message_id) == (-1001234567890, 3, 7)
