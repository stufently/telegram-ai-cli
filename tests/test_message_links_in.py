"""Which links an operation may act on, and which it must refuse.

A link that parses is not the same thing as a link that addresses what it looks
like it addresses. Two shapes look valid and mean something else, and both would
otherwise send an operation at a message nobody named:

`t.me/someone/123` where `someone` is a person — the URL opens their profile and
addresses no message; and `t.me/channel/123?comment=456`, which addresses
message 456 in the channel's *discussion group*, a different chat entirely.

The guard runs after the policy check, never before: a malformed link is not a
reason to tell a caller anything about a chat they may not read.
"""

from __future__ import annotations

import pytest

from telegram_ai_cli_mcp.errors import ErrorCode, InvalidInput
from telegram_ai_cli_mcp.links import parse_telegram_link
from telegram_ai_cli_mcp.ops.chats import guard_message_link, message_id_from
from telegram_ai_cli_mcp.safety import PeerKind, PeerRef

GROUP = PeerRef(peer_id=-1001234567890, kind=PeerKind.GROUP, username=None, title="Marketing")
CHANNEL = PeerRef(peer_id=-1001234567890, kind=PeerKind.CHANNEL, username="publicchat")
USER = PeerRef(peer_id=111222333, kind=PeerKind.USER, username="someone", title="Someone")
SAVED = PeerRef(peer_id=111222333, kind=PeerKind.SELF, username="someone")


# --- a message link into a private chat ------------------------------------


@pytest.mark.parametrize("ref", [USER, SAVED])
def test_a_message_link_into_a_private_chat_is_refused(ref: PeerRef) -> None:
    """`t.me/someone/123` opens a profile; the 123 is not a message id there."""
    link = parse_telegram_link("https://t.me/someone/123")
    with pytest.raises(InvalidInput) as failure:
        guard_message_link(ref, link, what="chat.read")
    assert failure.value.code is ErrorCode.INVALID_INPUT
    assert "one-to-one" in failure.value.message


def test_a_chat_link_into_a_private_chat_is_fine() -> None:
    """Only the *message* number is the problem. The chat itself resolves."""
    guard_message_link(USER, parse_telegram_link("https://t.me/someone"), what="chat.read")


@pytest.mark.parametrize("ref", [GROUP, CHANNEL])
def test_a_message_link_into_a_group_or_channel_passes(ref: PeerRef) -> None:
    guard_message_link(ref, parse_telegram_link("https://t.me/publicchat/123"), what="chat.read")


def test_no_link_at_all_is_nothing_to_guard() -> None:
    guard_message_link(USER, None, what="chat.read")


# --- comment links ---------------------------------------------------------


def test_a_comment_link_is_refused_with_the_reason() -> None:
    link = parse_telegram_link("https://t.me/publicchat/123?comment=456")
    with pytest.raises(InvalidInput) as failure:
        guard_message_link(CHANNEL, link, what="message.reactions")
    assert "discussion" in failure.value.message


# --- choosing the message id -----------------------------------------------


def test_the_link_supplies_the_message_id_when_none_was_given() -> None:
    link = parse_telegram_link("https://t.me/publicchat/123")
    assert message_id_from(None, link, what="media.fetch") == 123


def test_an_explicit_id_is_used_when_there_is_no_link() -> None:
    assert message_id_from(7, None, what="media.fetch") == 7


def test_a_link_and_an_id_that_agree_are_accepted() -> None:
    link = parse_telegram_link("https://t.me/publicchat/123")
    assert message_id_from(123, link, what="media.fetch") == 123


def test_a_link_and_an_id_that_disagree_are_refused() -> None:
    """Preferring one silently means acting on a message nobody named."""
    link = parse_telegram_link("https://t.me/publicchat/123")
    with pytest.raises(InvalidInput, match="not both"):
        message_id_from(456, link, what="media.fetch")


def test_neither_a_link_nor_an_id_says_what_to_pass() -> None:
    with pytest.raises(InvalidInput, match="message_id"):
        message_id_from(None, None, what="media.fetch")
