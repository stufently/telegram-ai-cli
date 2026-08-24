"""What a handler does, as opposed to what its pure parts return.

The policy kernel, the serializers and the link parser each have their own
tests, and every one of them passes on a handler that calls them in the wrong
order — or does not call them at all. These tests drive whole handlers against
the shared fake in `fakes.py`, and they assert on two things a return value
cannot show:

**When the refusal happens.** A chat this account may not read must be refused
*before* Telegram is asked for its messages. Both orders return the same error;
only one of them keeps the request from being made, and the request is what
leaves a trace on somebody's account.

**Which request was issued.** Telegram has one call that describes a dialog and
several that mark it read. Choosing the wrong one makes a badge disappear from
the owner's phone because an agent looked at a chat — invisible from the answer,
unrecoverable afterwards, and visible here as the class that was sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fakes import NOW, FakeClient, FakeDialog, FakeMessage
from telethon import utils
from telethon.tl import types as tl
from telethon.tl.functions.messages import GetPeerDialogsRequest

from telegram_ai_cli.errors import InvalidInput, NotAllowlisted
from telegram_ai_cli.ops.chats import ChatReadInput, handle_chat_read
from telegram_ai_cli.ops.inbox import InboxInput, handle_inbox
from telegram_ai_cli.ops.pending import DraftsInput, handle_drafts

GROUP_ID = 4242
OTHER_GROUP_ID = 4343
FRIEND_ID = 5151
OTHER_FRIEND_ID = 5252

#: Computed rather than written out: the marked form of a channel id is what
#: `utils.get_peer_id` produces, and a literal here would both restate that
#: rule and read like a real chat id to the scan that keeps them out of this
#: repository.
MARKED_GROUP_ID = utils.get_peer_id(tl.PeerChannel(GROUP_ID))


def group(chat_id: int = GROUP_ID, title: str = "Marketing") -> tl.Channel:
    return tl.Channel(id=chat_id, title=title, photo=None, date=None, megagroup=True)


def friend(user_id: int = FRIEND_ID) -> tl.User:
    return tl.User(id=user_id, first_name="Sam")


def a_dialog_answer() -> Any:
    """What `GetPeerDialogs` returns: a dialog, and nothing acknowledged."""
    return tl.messages.PeerDialogs(
        dialogs=[
            tl.Dialog(
                peer=tl.PeerChannel(GROUP_ID),
                top_message=10,
                read_inbox_max_id=10,
                read_outbox_max_id=10,
                unread_count=0,
                unread_mentions_count=0,
                unread_reactions_count=0,
                unread_poll_votes_count=0,
                notify_settings=tl.PeerNotifySettings(),
            )
        ],
        messages=[],
        chats=[],
        users=[],
        state=tl.updates.State(pts=1, qts=1, date=NOW, seq=1, unread_count=0),
    )


def readable(chat_id: int) -> dict[str, Any]:
    return {"read": {"chats": {"allow": [chat_id]}}}


# --- the refusal comes before the request -----------------------------------


@pytest.mark.asyncio
async def test_an_unreadable_chat_is_refused_without_asking_telegram_for_it(
    make_context: Any,
) -> None:
    """Resolving a chat is unavoidable; reading it is not.

    A refusal issued after the fetch returns the same error to the caller and
    leaves a different trace on the account.
    """
    client = FakeClient(entity=group(), messages=[FakeMessage()])
    ctx = make_context(client, **{"safety": readable(OTHER_GROUP_ID)})

    with pytest.raises(NotAllowlisted):
        await handle_chat_read(ctx, ChatReadInput(chat=str(MARKED_GROUP_ID)))

    assert client.requests == []
    assert [name for name, _, _ in client.calls] == ["get_entity"]


@pytest.mark.asyncio
async def test_a_private_chat_is_refused_by_the_dm_policy_it_is_remapped_onto(
    make_context: Any,
) -> None:
    """A one-to-one conversation is judged by the DM list, which is empty here.

    Allowing every group says nothing about a person's private messages, and an
    empty DM allowlist means none — not all.
    """
    client = FakeClient(entity=friend(), messages=[FakeMessage()])
    ctx = make_context(client, **{"safety": {"read": {"chats": {"allow": []}}}})

    with pytest.raises(NotAllowlisted):
        await handle_chat_read(ctx, ChatReadInput(chat=str(FRIEND_ID)))

    assert client.requests == []
    assert [name for name, _, _ in client.calls] == ["get_entity"]


# --- which request a read issues --------------------------------------------


@pytest.mark.asyncio
async def test_reading_a_chat_describes_the_dialog_and_acknowledges_nothing(
    make_context: Any,
) -> None:
    """`GetPeerDialogs` reports where the read pointers stand; it moves none.

    The assertion is on the whole recording rather than on the answer: the
    property is that nothing *else* was sent — `ReadHistory` and `ReadMentions`
    sit one letter away in the same namespace and each clears a badge on the
    owner's phone — and an answer cannot show that.
    """
    client = FakeClient(
        entity=group(),
        messages=[FakeMessage(id=10, message="quarterly numbers are in")],
        answers={GetPeerDialogsRequest: a_dialog_answer()},
    )
    ctx = make_context(client, **{"safety": readable(MARKED_GROUP_ID)})

    envelope = await handle_chat_read(
        ctx, ChatReadInput(chat=str(MARKED_GROUP_ID), include_read_state=True)
    )

    assert client.issued() == [GetPeerDialogsRequest]
    assert envelope.data["read_state"]["known"] is True


@pytest.mark.asyncio
async def test_not_asking_for_the_read_state_asks_telegram_nothing(make_context: Any) -> None:
    """The default page is history alone, and history alone is one call."""
    client = FakeClient(entity=group(), messages=[FakeMessage()])
    ctx = make_context(client, **{"safety": readable(MARKED_GROUP_ID)})

    envelope = await handle_chat_read(
        ctx, ChatReadInput(chat=str(MARKED_GROUP_ID), include_read_state=False)
    )

    assert client.requests == []
    assert envelope.data["read_state"]["known"] is False


@pytest.mark.asyncio
async def test_a_link_anchors_the_page_at_the_message_it_names(make_context: Any) -> None:
    """The cursor is exclusive, so anchoring *at* a message is its id plus one.

    Asserted on the arguments the handler passed rather than on the rows: this
    fake returns what it was given whatever the cursor says, which is exactly
    why a dropped cursor has to be caught here.
    """
    client = FakeClient(entity=group(), messages=[FakeMessage()])
    ctx = make_context(client, **{"safety": readable(MARKED_GROUP_ID)})

    await handle_chat_read(
        ctx,
        ChatReadInput(chat="https://t.me/c/4242/900", include_read_state=False, limit=20),
    )

    (name, _, kwargs) = client.calls[-1]
    assert name == "get_messages"
    assert kwargs["max_id"] == 901
    assert kwargs["limit"] == 20


@pytest.mark.asyncio
async def test_a_link_and_before_id_both_position_the_page(make_context: Any) -> None:
    """Two answers to "where does this page start" cannot both be honoured.

    Silently preferring either one returns a page nobody asked for, and it
    looks exactly like the page they did ask for.
    """
    client = FakeClient(entity=group(), messages=[FakeMessage()])
    ctx = make_context(client, **{"safety": readable(MARKED_GROUP_ID)})

    with pytest.raises(InvalidInput, match="pass one of them"):
        await handle_chat_read(
            ctx,
            ChatReadInput(chat="https://t.me/c/4242/900", before_id=500),
        )

    assert client.requests == []
    assert [name for name, _, _ in client.calls] == ["get_entity"]


# --- a listing routes each verdict to its own bucket -------------------------


@dataclass
class FakeDraft:
    entity: Any
    text: str = "half-written"
    date: Any = NOW
    reply_to_msg_id: int | None = None
    link_preview: bool = False


@pytest.mark.asyncio
async def test_each_draft_lands_in_the_bucket_its_verdict_names(make_context: Any) -> None:
    """Shown, hidden and withheld are three different things.

    They are decided by a pure function that has its own exhaustive tests — and
    swapping two of the branches that act on its verdict would list a private
    draft while every one of those tests still passed. The counts are reported
    separately for the same reason: "nothing here" and "something here you may
    not see" are not the same answer.
    """
    client = FakeClient(
        drafts=[
            FakeDraft(entity=group(), text="ship it"),
            FakeDraft(entity=friend(), text="see you at six"),
            FakeDraft(entity=friend(OTHER_FRIEND_ID), text="running late"),
            FakeDraft(entity=group(OTHER_GROUP_ID, "Legal"), text="not for you"),
        ],
    )
    ctx = make_context(client, **{"safety": readable(MARKED_GROUP_ID)})

    envelope = await handle_drafts(ctx, DraftsInput())

    assert [row["chat"]["id"] for row in envelope.data["drafts"]] == [MARKED_GROUP_ID]
    # Counted, not merely mentioned: two private and one unlisted group. Equal
    # counts would let the two branches be swapped and still pass, which is the
    # mistake this test exists to catch.
    assert any("2 draft(s) in private chats omitted" in one for one in envelope.warnings)
    assert any("1 draft(s) withheld" in one for one in envelope.warnings)
    printed = str(envelope.data)
    assert "see you at six" not in printed
    assert "not for you" not in printed


@pytest.mark.asyncio
async def test_the_inbox_counts_an_unreadable_chat_without_quoting_it(
    make_context: Any,
) -> None:
    """Enumeration says a conversation exists; it does not say what is in it.

    The inbox row carries a preview, and a preview is the last message's text —
    so a listing that enumerates direct messages would otherwise hand back a
    line out of every one of them, including the chats the DM allowlist refuses.
    `telegram_drafts` and `telegram_mentions` both ask before quoting; this is
    the same question, asked at the point the text is attached.
    """
    client = FakeClient(
        dialogs=[
            FakeDialog(
                entity=group(),
                unread_count=3,
                message=FakeMessage(message="quarterly numbers are in"),
            ),
            FakeDialog(
                entity=friend(),
                unread_count=1,
                message=FakeMessage(message="the door code is on the fridge"),
            ),
        ],
    )
    ctx = make_context(
        client,
        **{"safety": {"read": {"enumerate_dms": True}}},
    )

    envelope = await handle_inbox(ctx, InboxInput(include_private=True, include_muted=True))

    rows = {row["chat_id"]: row for row in envelope.data["waiting"]}
    # Both are listed: enumeration was permitted, and withholding the row would
    # hide that the conversation is waiting at all.
    assert set(rows) == {MARKED_GROUP_ID, FRIEND_ID}
    # `in`, not equality: a preview travels inside the untrusted-content
    # markers, which is what makes it quotable at all.
    assert "quarterly numbers are in" in rows[MARKED_GROUP_ID]["preview"]
    assert rows[FRIEND_ID]["preview"] is None
    assert "door code" not in str(envelope.data)
    assert any("1 preview(s) withheld" in one for one in envelope.warnings)


@pytest.mark.asyncio
async def test_a_cleared_draft_is_not_a_draft(make_context: Any) -> None:
    """Telegram keeps the object around after the text is deleted."""
    client = FakeClient(drafts=[FakeDraft(entity=group(), text="")])
    ctx = make_context(client, **{"safety": readable(MARKED_GROUP_ID)})

    envelope = await handle_drafts(ctx, DraftsInput())

    assert envelope.data["drafts"] == []
    assert envelope.warnings == []
