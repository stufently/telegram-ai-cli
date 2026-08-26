"""``mentions.list`` — where this account was actually called out.

The tests that matter here are not about shape. Two of them are properties:

**Nothing is acknowledged.** Telegram has one request that answers "which
mentions are unread" and another, one letter apart in Telethon's namespace,
that clears them. Calling the wrong one makes a badge vanish from the owner's
phone because an agent looked — an invisible, unrecoverable side effect. So the
fake client records every request class it is handed, and the test asserts on
the whole recording rather than on the answer.

**The floor holds before the fetch.** Service Notifications and unreadable
chats are dropped while the dialog list is walked, so the operation never
reaches the point of asking Telegram for their messages.

The fake client is deliberately dumb: it iterates canned dialogs and answers
canned pages. Everything it stands in for is either recorded or asserted on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from telethon.tl import types as tl
from telethon.tl.functions.messages import (
    GetUnreadMentionsRequest,
    GetUnreadReactionsRequest,
    ReadMentionsRequest,
    ReadReactionsRequest,
)

from telegram_ai_cli_mcp.audit import AuditLog
from telegram_ai_cli_mcp.config import AuditConfig, Settings
from telegram_ai_cli_mcp.context import OperationContext
from telegram_ai_cli_mcp.ops.inbox import InboxInput, _rank, _waiting
from telegram_ai_cli_mcp.ops.mentions import MentionsInput, handle_mentions
from telegram_ai_cli_mcp.safety import SafetyKernel

NOW = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)

#: Everything Telethon offers that would clear one of the two badges.
ACKNOWLEDGING = (ReadMentionsRequest, ReadReactionsRequest)


# --- stand-ins ---------------------------------------------------------------


@dataclass
class FakeReactionCount:
    reaction: Any
    count: int
    chosen_order: int | None = None


@dataclass
class FakePeerReaction:
    peer_id: Any
    reaction: Any
    date: datetime = NOW
    unread: bool = True


@dataclass
class FakeReactions:
    results: list[FakeReactionCount] = field(default_factory=list)
    recent_reactions: list[FakePeerReaction] | None = None
    can_see_list: bool = False


@dataclass
class FakeMessage:
    id: int = 10
    message: str | None = "hello"
    date: datetime = NOW
    out: bool = False
    sender_id: int | None = None
    sender: Any = None
    edit_date: datetime | None = None
    views: int | None = None
    pinned: bool = False
    media: Any = None
    reply_to: Any = None
    forward: Any = None
    reactions: FakeReactions | None = None


@dataclass
class FakeNotify:
    silent: bool = False
    mute_until: Any = None


@dataclass
class FakeRawDialog:
    notify_settings: FakeNotify = field(default_factory=FakeNotify)


@dataclass
class FakeDialog:
    entity: Any
    unread_count: int = 0
    unread_mentions_count: int = 0
    unread_reactions_count: int = 0
    pinned: bool = False
    archived: bool = False
    message: Any = None
    date: datetime = NOW
    dialog: FakeRawDialog = field(default_factory=FakeRawDialog)


@dataclass
class FakePage:
    """What ``messages.getUnreadMentions`` and its reaction twin return."""

    messages: list[Any] = field(default_factory=list)
    users: list[Any] = field(default_factory=list)
    chats: list[Any] = field(default_factory=list)
    count: int | None = None


class FakeClient:
    """Records every interaction; answers the two read-only requests.

    Both ways into a client are recorded, not just the raw calls: the dialog
    walk goes through ``iter_dialogs`` and everything else through ``__call__``,
    so ``touched`` is the complete list of what the operation asked this client
    to do. Requests are kept as objects rather than classes, because "which
    request" and "with which offsets" are both worth asserting.
    """

    def __init__(
        self,
        dialogs: list[FakeDialog],
        *,
        mentions: FakePage | None = None,
        reactions: FakePage | None = None,
    ) -> None:
        self._dialogs = dialogs
        self._mentions = mentions or FakePage()
        self._reactions = reactions or FakePage()
        self.requests: list[Any] = []
        self.touched: list[str] = []

    # -- what open_account needs -----------------------------------------
    def is_connected(self) -> bool:
        return True

    async def is_user_authorized(self) -> bool:
        return True

    # -- what the operation uses -----------------------------------------
    async def iter_dialogs(self, **_kwargs: Any) -> Any:
        self.touched.append("iter_dialogs")
        for dialog in self._dialogs:
            yield dialog

    async def get_entity(self, reference: Any) -> Any:
        del reference
        return self._dialogs[0].entity

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        self.touched.append(type(request).__name__)
        if isinstance(request, GetUnreadMentionsRequest):
            return self._mentions
        if isinstance(request, GetUnreadReactionsRequest):
            return self._reactions
        raise AssertionError(f"unexpected request: {type(request).__name__}")

    def issued(self) -> list[type]:
        return [type(request) for request in self.requests]

    # -- what it must never use ------------------------------------------
    async def send_read_acknowledge(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("a read operation acknowledged messages")


@dataclass
class FakeOpened:
    client: FakeClient
    spec: Any = None


class FakeRegistry:
    def __init__(self, clients: dict[str, FakeClient]) -> None:
        self._clients = clients

    def list_accounts(self) -> list[str]:
        return list(self._clients)

    def load_account(self, label: str) -> FakeOpened:
        return FakeOpened(client=self._clients[label])

    def get(self, label: str) -> None:
        return None


def build_ctx(tmp_path: Path, client: FakeClient, settings: Settings | None = None) -> Any:
    settings = settings or Settings()
    return OperationContext(
        settings=settings,
        safety=SafetyKernel(settings),
        plans=None,  # type: ignore[arg-type]
        limits=None,  # type: ignore[arg-type]
        audit=AuditLog(tmp_path / "audit.jsonl", AuditConfig(enabled=False)),
        actor="cli",
        accounts=FakeRegistry({"work": client}),  # type: ignore[arg-type]
    )


def group(peer_id: int = 1234567890, title: str = "Marketing") -> tl.Channel:
    return tl.Channel(
        id=peer_id, title=title, photo=None, date=None, megagroup=True, username="mkt"
    )


def user(peer_id: int, first_name: str, username: str | None = None) -> tl.User:
    return tl.User(id=peer_id, first_name=first_name, username=username)


def rows_of(envelope: Any) -> list[dict[str, Any]]:
    return list(envelope.data["chats"])


# --- the property this operation exists to keep -------------------------------


async def test_reading_mentions_acknowledges_nothing(tmp_path: Path) -> None:
    """The whole point: looking must not clear the badge on the owner's phone."""
    client = FakeClient(
        [FakeDialog(entity=group(), unread_count=4, unread_mentions_count=2)],
        mentions=FakePage(messages=[FakeMessage(id=41, sender_id=555)], users=[user(555, "Ann")]),
    )
    ctx = build_ctx(tmp_path, client)

    await handle_mentions(ctx, MentionsInput())

    assert GetUnreadMentionsRequest in client.issued(), "the unread page was never fetched"
    for forbidden in ACKNOWLEDGING:
        assert forbidden not in client.issued(), f"{forbidden.__name__} would clear the badge"
    # Everything the operation did to this client, not only the raw calls.
    assert client.touched == ["iter_dialogs", "GetUnreadMentionsRequest"]


async def test_a_chat_with_nothing_unread_is_not_even_asked_about(tmp_path: Path) -> None:
    """No call at all where Telegram's counters say there is nothing to fetch."""
    client = FakeClient([FakeDialog(entity=group(), unread_count=120)])
    ctx = build_ctx(tmp_path, client)

    envelope = await handle_mentions(ctx, MentionsInput())

    assert client.issued() == []
    assert rows_of(envelope) == []


# --- what a mention and a reaction actually say -------------------------------


async def test_a_mention_names_who_called_and_which_message(tmp_path: Path) -> None:
    client = FakeClient(
        [FakeDialog(entity=group(), unread_count=9, unread_mentions_count=1)],
        mentions=FakePage(
            messages=[FakeMessage(id=41, message="@me can you look?", sender_id=555)],
            users=[user(555, "Ann", "ann")],
        ),
    )
    ctx = build_ctx(tmp_path, client)

    envelope = await handle_mentions(ctx, MentionsInput())

    row = rows_of(envelope)[0]
    assert row["unread_mentions"] == 1
    mention = row["mentions"][0]
    assert mention["id"] == 41
    assert "can you look?" in mention["text"]
    # The sender arrives with the raw page rather than attached to the message,
    # so a name here is the proof it was picked up from it.
    assert "Ann" in mention["sender"]
    assert mention["sender_username"] == "ann"
    assert mention["link"] == "https://t.me/mkt/41"


async def test_a_reaction_names_the_person_the_emoji_and_the_message(tmp_path: Path) -> None:
    reaction = tl.ReactionEmoji(emoticon="🔥")
    client = FakeClient(
        [FakeDialog(entity=group(), unread_reactions_count=1)],
        reactions=FakePage(
            messages=[
                FakeMessage(
                    id=77,
                    message="shipped it",
                    out=True,
                    reactions=FakeReactions(
                        results=[FakeReactionCount(reaction, count=1)],
                        recent_reactions=[FakePeerReaction(tl.PeerUser(555), reaction)],
                    ),
                )
            ],
            users=[user(555, "Ann", "ann")],
        ),
    )
    ctx = build_ctx(tmp_path, client)

    envelope = await handle_mentions(ctx, MentionsInput())

    row = rows_of(envelope)[0]
    assert row["unread_reactions"] == 1
    hit = row["reactions"][0]
    assert hit["id"] == 77, "a reaction is reported against the message it landed on"
    assert "shipped it" in hit["text"]
    assert hit["reactors"][0]["emoji"] == "🔥"
    assert hit["reactors"][0]["peer_id"] == 555
    assert "Ann" in hit["reactors"][0]["name"]


async def test_a_reaction_already_seen_is_not_reported_as_unread(tmp_path: Path) -> None:
    """Telegram flags each reactor; only the unread ones are new information."""
    reaction = tl.ReactionEmoji(emoticon="👍")
    client = FakeClient(
        [FakeDialog(entity=group(), unread_reactions_count=1)],
        reactions=FakePage(
            messages=[
                FakeMessage(
                    id=77,
                    out=True,
                    reactions=FakeReactions(
                        results=[FakeReactionCount(reaction, count=2)],
                        recent_reactions=[
                            FakePeerReaction(tl.PeerUser(555), reaction, unread=False),
                            FakePeerReaction(tl.PeerUser(666), reaction, unread=True),
                        ],
                    ),
                )
            ],
            users=[user(555, "Ann"), user(666, "Bob")],
        ),
    )
    ctx = build_ctx(tmp_path, client)

    envelope = await handle_mentions(ctx, MentionsInput())

    reactors = rows_of(envelope)[0]["reactions"][0]["reactors"]
    assert [r["peer_id"] for r in reactors] == [666]


# --- the floor ----------------------------------------------------------------


async def test_service_notifications_are_never_fetched(tmp_path: Path) -> None:
    """777000 carries login codes; it is closed in code, badge or no badge."""
    settings = Settings()
    # Every switch a configuration has, turned all the way on: the point is
    # that this chat is refused by the floor, not by a policy that could differ.
    settings.safety.read.enumerate_dms = True
    settings.safety.read.dms.allow = [777000]
    client = FakeClient(
        [FakeDialog(entity=user(777000, "Telegram"), unread_count=1, unread_mentions_count=1)]
    )
    ctx = build_ctx(tmp_path, client, settings)

    envelope = await handle_mentions(ctx, MentionsInput(include_private=True))

    assert client.issued() == []
    assert rows_of(envelope) == []


async def test_a_private_chat_stays_out_until_enumeration_permits_it(tmp_path: Path) -> None:
    client = FakeClient(
        [FakeDialog(entity=user(4242, "Ann"), unread_count=1, unread_mentions_count=1)]
    )
    ctx = build_ctx(tmp_path, client)

    envelope = await handle_mentions(ctx, MentionsInput())

    assert client.issued() == []
    assert rows_of(envelope) == []
    assert any("private" in warning for warning in envelope.warnings)


async def test_a_dm_is_still_judged_by_the_dm_allowlist(tmp_path: Path) -> None:
    """Enumeration is not a read: an empty ``dms`` allowlist still means none."""
    settings = Settings()
    settings.safety.read.enumerate_dms = True
    client = FakeClient(
        [FakeDialog(entity=user(4242, "Ann"), unread_count=1, unread_mentions_count=1)]
    )
    ctx = build_ctx(tmp_path, client, settings)

    envelope = await handle_mentions(ctx, MentionsInput(include_private=True))

    assert client.issued() == [], "a chat outside the read policy is never fetched"
    assert rows_of(envelope) == []
    assert any("withheld" in warning for warning in envelope.warnings)


async def test_a_denied_group_is_counted_not_shown(tmp_path: Path) -> None:
    settings = Settings()
    settings.safety.read.chats.deny = [-1001234567890]
    client = FakeClient([FakeDialog(entity=group(), unread_mentions_count=3)])
    ctx = build_ctx(tmp_path, client, settings)

    envelope = await handle_mentions(ctx, MentionsInput())

    assert client.issued() == []
    assert rows_of(envelope) == []
    assert any("withheld" in warning for warning in envelope.warnings)


# --- narrowing ----------------------------------------------------------------


async def test_reactions_can_be_left_out(tmp_path: Path) -> None:
    client = FakeClient(
        [FakeDialog(entity=group(), unread_mentions_count=1, unread_reactions_count=1)],
        mentions=FakePage(messages=[FakeMessage(id=41)]),
        reactions=FakePage(messages=[FakeMessage(id=77, out=True)]),
    )
    ctx = build_ctx(tmp_path, client)

    envelope = await handle_mentions(ctx, MentionsInput(include_reactions=False))

    assert GetUnreadReactionsRequest not in client.issued()
    assert rows_of(envelope)[0]["reactions"] == []


async def test_one_chat_is_read_without_enumerating_dialogs_and_can_page(tmp_path: Path) -> None:
    page = FakePage(
        messages=[FakeMessage(id=80), FakeMessage(id=70)],
        count=5,
    )
    client = FakeClient([FakeDialog(entity=group())], mentions=page)
    ctx = build_ctx(tmp_path, client)

    envelope = await handle_mentions(
        ctx,
        MentionsInput(
            account="work",
            chat="1234567890",
            per_chat=2,
            offset_id=90,
            include_reactions=False,
        ),
    )

    request = client.requests[0]
    assert client.touched == ["GetUnreadMentionsRequest"]
    assert request.offset_id == 90
    assert rows_of(envelope)[0]["next_offset_id"]["mentions"] == 70
    assert envelope.meta.truncated is True


async def test_one_forum_topic_is_sent_as_top_msg_id(tmp_path: Path) -> None:
    client = FakeClient(
        [FakeDialog(entity=group())],
        mentions=FakePage(messages=[FakeMessage(id=80)], count=1),
    )
    ctx = build_ctx(tmp_path, client)

    await handle_mentions(
        ctx,
        MentionsInput(
            account="work",
            chat="1234567890",
            topic_id=44,
            include_reactions=False,
        ),
    )

    assert client.requests[0].top_msg_id == 44


async def test_the_busiest_chats_come_first_and_the_rest_are_reported(tmp_path: Path) -> None:
    dialogs = [
        FakeDialog(entity=group(1, "quiet"), unread_mentions_count=1),
        FakeDialog(entity=group(2, "loud"), unread_mentions_count=9),
    ]
    client = FakeClient(dialogs, mentions=FakePage(messages=[FakeMessage(id=41)]))
    ctx = build_ctx(tmp_path, client)

    envelope = await handle_mentions(ctx, MentionsInput(limit=1))

    rows = rows_of(envelope)
    assert len(rows) == 1
    assert "loud" in rows[0]["chat"]["title"]
    assert envelope.meta.truncated is True
    assert envelope.meta.total == 2


async def test_one_broken_account_does_not_blank_the_fleet(tmp_path: Path) -> None:
    good = FakeClient(
        [FakeDialog(entity=group(), unread_mentions_count=1)],
        mentions=FakePage(messages=[FakeMessage(id=41)]),
    )
    ctx = build_ctx(tmp_path, good)
    ctx.accounts = FakeRegistry({"broken": None, "work": good})  # type: ignore[arg-type]

    envelope = await handle_mentions(ctx, MentionsInput())

    assert len(rows_of(envelope)) == 1
    assert any("broken" in warning for warning in envelope.warnings)


async def test_the_unread_page_is_asked_for_from_the_top(tmp_path: Path) -> None:
    """The offsets matter: a stray one would page past the newest mentions."""
    client = FakeClient(
        [FakeDialog(entity=group(), unread_mentions_count=2)],
        mentions=FakePage(messages=[FakeMessage(id=41)]),
    )
    ctx = build_ctx(tmp_path, client)

    await handle_mentions(ctx, MentionsInput(per_chat=7))

    request = client.requests[0]
    assert (request.offset_id, request.add_offset, request.max_id, request.min_id) == (0, 0, 0, 0)
    assert request.limit == 7


async def test_hiding_private_chats_does_not_report_how_many_are_active(
    tmp_path: Path,
) -> None:
    """The tally is "how many private chats", never "how many are waiting".

    Counting only the ones with unread mentions would answer a question the
    configuration declined to answer: somebody is talking to this account right
    now, in exactly two conversations you may not see.
    """
    client = FakeClient(
        [
            FakeDialog(entity=user(4242, "Ann"), unread_mentions_count=3),
            FakeDialog(entity=user(4343, "Bob")),
        ]
    )
    ctx = build_ctx(tmp_path, client)

    envelope = await handle_mentions(ctx, MentionsInput())

    assert any("2 private chat(s) omitted" in warning for warning in envelope.warnings)


async def test_stopping_at_the_dialog_ceiling_is_reported(tmp_path: Path) -> None:
    """A short list that looks complete is the failure mode worth a warning."""
    from telegram_ai_cli_mcp.ops._common import MAX_DIALOG_SCAN

    dialogs = [
        FakeDialog(entity=group(peer_id=1000 + n, title=f"g{n}")) for n in range(MAX_DIALOG_SCAN)
    ]
    dialogs.append(FakeDialog(entity=group(peer_id=9999, title="the one"), unread_mentions_count=1))
    client = FakeClient(dialogs)
    ctx = build_ctx(tmp_path, client)

    envelope = await handle_mentions(ctx, MentionsInput())

    assert envelope.meta.truncated is True
    assert any("stopped at" in warning for warning in envelope.warnings)


async def test_a_named_account_that_fails_is_an_error_not_an_empty_answer(
    tmp_path: Path,
) -> None:
    """ "No mentions" and "nobody looked" must not serialize identically."""
    from telegram_ai_cli_mcp.errors import TelegramAIError

    ctx = build_ctx(tmp_path, FakeClient([]))

    with pytest.raises(TelegramAIError):
        await handle_mentions(ctx, MentionsInput(account="missing"))


async def test_the_totals_count_the_chats_past_the_cut_too(tmp_path: Path) -> None:
    """Otherwise the total agrees with the list and disagrees with reality."""
    dialogs = [
        FakeDialog(entity=group(1, "quiet"), unread_mentions_count=2),
        FakeDialog(entity=group(2, "loud"), unread_mentions_count=9),
    ]
    client = FakeClient(dialogs, mentions=FakePage(messages=[FakeMessage(id=41)]))
    ctx = build_ctx(tmp_path, client)

    envelope = await handle_mentions(ctx, MentionsInput(limit=1))

    assert envelope.data["totals"]["chats"] == 2
    assert envelope.data["totals"]["mentions"] == 11


# --- the signal has to reach the inbox ----------------------------------------


def test_a_dialog_row_carries_the_unread_reaction_count() -> None:
    """The count the ranking reads has to be on the row in the first place."""
    from telegram_ai_cli_mcp.ops._serialize import dialog_summary

    dialog = FakeDialog(entity=group(), unread_mentions_count=1, unread_reactions_count=2)

    row = dialog_summary(dialog)
    assert row["mentions"] == 1
    assert row["reactions"] == 2


def test_the_inbox_counts_an_unread_reaction_as_something_waiting() -> None:
    row = {"unread": 0, "mentions": 0, "reactions": 1}
    assert _waiting(row, mentions_only=False) is True


def test_the_inbox_ranks_a_reaction_above_bulk_unread() -> None:
    """Someone reacting to your message is about you; a busy group is not."""
    reacted = {"unread": 1, "mentions": 0, "reactions": 1, "last_message_at": ""}
    noisy = {"unread": 200, "mentions": 0, "reactions": 0, "last_message_at": ""}
    assert sorted([noisy, reacted], key=_rank)[0] is reacted


def test_the_inbox_still_ranks_a_mention_above_a_reaction() -> None:
    mentioned = {"unread": 1, "mentions": 1, "reactions": 0, "last_message_at": ""}
    reacted = {"unread": 1, "mentions": 0, "reactions": 5, "last_message_at": ""}
    assert sorted([reacted, mentioned], key=_rank)[0] is mentioned


def test_mentions_only_still_means_mentions() -> None:
    """Narrowing to mentions must not quietly start meaning "and reactions"."""
    assert _waiting({"unread": 0, "mentions": 0, "reactions": 3}, mentions_only=True) is False


def test_the_inbox_input_still_forbids_unknown_arguments() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic's own error type
        InboxInput(mentions_onlyy=True)  # type: ignore[call-arg]
