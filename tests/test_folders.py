"""Chat folders: a sorting a person already did, read back as a filter.

Two properties are worth a test rather than a reading of the code.

**A folder is not a permission.** Telegram's folders are authored by the user
and can name any chat the account can see — including the ones this tool
refuses to enumerate. So the filter has to run *after* the floor and the
enumeration switches, never instead of them, and the last section here drives
the real handlers to prove it rather than asserting it about a helper.

**The membership rule is Telegram's, not ours.** Whether a dialog belongs to a
folder is decided from flags and two peer lists, and every branch of that
decision is a case a person would otherwise have to re-derive from the API
documentation. It is a pure function over plain facts, so it is tested as one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from telegram_ai_cli.audit import AuditLog
from telegram_ai_cli.config import Settings
from telegram_ai_cli.context import OperationContext
from telegram_ai_cli.errors import InvalidInput, NotFound
from telegram_ai_cli.ops.chats import ChatsInput, handle_chats
from telegram_ai_cli.ops.folders import (
    DialogFacts,
    FolderFlags,
    FoldersInput,
    FolderView,
    folder_summary,
    handle_folders,
    input_peer_id,
    needs_self_id,
    parse_folders,
    resolve_folder,
)
from telegram_ai_cli.ops.inbox import InboxInput, handle_inbox
from telegram_ai_cli.safety import PeerKind, SafetyKernel
from telegram_ai_cli.untrusted import unwrap

# Deliberately small, obviously fake ids (see tests/test_no_private_data.py) —
# and the marked-id base is written as a power of ten rather than as the
# literal, which is shaped exactly like a real channel id.
CHANNEL_BASE = -(10**12)
GROUP_ID = CHANNEL_BASE - 4242
CHANNEL_ID = CHANNEL_BASE - 4343
FRIEND_ID = 555
SERVICE_ID = 777000
SELF_ID = 999


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """No ``TGAI_`` variable from the developer's shell may steer a decision."""
    for key in list(os.environ):
        if key.startswith("TGAI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


# --- stand-ins for what Telethon returns -----------------------------------


@dataclass
class FakeInputUser:
    user_id: int


@dataclass
class FakeInputChannel:
    channel_id: int


@dataclass
class FakeInputChat:
    chat_id: int


class FakeInputSelf:
    """``InputPeerSelf`` carries no id at all — the case that has no answer."""


@dataclass
class FakeTitle:
    """Newer layers wrap a folder title in ``TextWithEntities``."""

    text: str


@dataclass
class FakeFilter:
    id: int
    title: Any
    emoticon: str | None = None
    contacts: bool = False
    non_contacts: bool = False
    groups: bool = False
    broadcasts: bool = False
    bots: bool = False
    exclude_muted: bool = False
    exclude_read: bool = False
    exclude_archived: bool = False
    include_peers: list[Any] = field(default_factory=list)
    exclude_peers: list[Any] = field(default_factory=list)
    pinned_peers: list[Any] = field(default_factory=list)


@dataclass
class FakeChatlist:
    """``DialogFilterChatlist``: shareable, include-only, and flagless."""

    id: int
    title: Any
    emoticon: str | None = None
    include_peers: list[Any] = field(default_factory=list)
    pinned_peers: list[Any] = field(default_factory=list)


class FakeDefault:
    """``DialogFilterDefault`` — "All chats", which has no id and no rules."""


@dataclass
class FakeFilterList:
    """``messages.DialogFilters``, the newer envelope around the list."""

    filters: list[Any]


# --- parsing ----------------------------------------------------------------


def test_a_folder_is_read_with_its_name_emoji_and_rules() -> None:
    result = FakeFilterList(
        [
            FakeFilter(
                id=2,
                title=FakeTitle("Work"),
                emoticon="💼",
                groups=True,
                exclude_muted=True,
                include_peers=[FakeInputChannel(4242)],
                exclude_peers=[FakeInputUser(FRIEND_ID)],
                pinned_peers=[FakeInputChat(99)],
            )
        ]
    )
    (view,) = parse_folders(result)
    assert view.id == 2
    assert view.title == "Work"
    assert view.emoticon == "💼"
    assert view.flags.groups is True
    assert view.flags.exclude_muted is True
    assert view.flags.broadcasts is False
    assert view.include == frozenset({GROUP_ID})
    assert view.exclude == frozenset({FRIEND_ID})
    assert view.pinned == frozenset({-99})


def test_a_plain_list_of_filters_is_accepted_too() -> None:
    """Older Telethon returns the list itself, not an envelope around it."""
    views = parse_folders([FakeFilter(id=3, title="Reading", broadcasts=True)])
    assert [view.id for view in views] == [3]


def test_the_all_chats_pseudo_folder_is_not_a_folder() -> None:
    """``DialogFilterDefault`` is the absence of a folder; listing it as one
    would offer a filter that filters nothing."""
    assert parse_folders(FakeFilterList([FakeDefault()])) == []


def test_an_account_with_no_folders_parses_to_an_empty_list() -> None:
    assert parse_folders(FakeFilterList([])) == []
    assert parse_folders(None) == []


def test_a_shareable_folder_is_include_only() -> None:
    """A chatlist has no flags at all; treating a missing flag as ``False``
    is what keeps it from silently swallowing every group."""
    (view,) = parse_folders(FakeFilterList([FakeChatlist(id=9, title="Shared")]))
    assert view.shareable is True
    assert view.flags == FolderFlags()


def test_a_peer_id_matches_what_telethon_would_have_computed() -> None:
    """The marked-id arithmetic is duplicated here rather than imported, so
    the constant is pinned against the real implementation."""
    from telethon import utils
    from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser, PeerChannel

    assert input_peer_id(InputPeerChannel(channel_id=4242, access_hash=0)) == utils.get_peer_id(
        PeerChannel(4242)
    )
    assert input_peer_id(InputPeerChat(chat_id=99)) == -99
    assert input_peer_id(InputPeerUser(user_id=FRIEND_ID, access_hash=0)) == FRIEND_ID


def test_a_peer_with_no_id_of_its_own_has_no_answer() -> None:
    """``InputPeerSelf`` means "me", which is Saved Messages — closed in code."""
    assert input_peer_id(FakeInputSelf()) is None


# --- membership -------------------------------------------------------------


def folder(**overrides: Any) -> FolderView:
    base: dict[str, Any] = {
        "id": 1,
        "title": "Folder",
        "emoticon": None,
        "shareable": False,
        "flags": FolderFlags(),
        "include": frozenset(),
        "exclude": frozenset(),
        "pinned": frozenset(),
    }
    flags = overrides.pop("flags", {})
    if isinstance(flags, dict):
        flags = FolderFlags(**flags)
    return FolderView(**{**base, **overrides, "flags": flags})


def facts(**overrides: Any) -> DialogFacts:
    base: dict[str, Any] = {"chat_id": GROUP_ID, "kind": PeerKind.GROUP}
    return DialogFacts(**{**base, **overrides})


def test_a_named_chat_is_in_the_folder_whatever_the_flags_say() -> None:
    assert folder(include=frozenset({GROUP_ID})).contains(facts()) is True


def test_a_pinned_chat_is_in_the_folder() -> None:
    assert folder(pinned=frozenset({GROUP_ID})).contains(facts()) is True


def test_an_excluded_chat_wins_over_being_included() -> None:
    view = folder(include=frozenset({GROUP_ID}), exclude=frozenset({GROUP_ID}))
    assert view.contains(facts()) is False


def test_a_kind_flag_admits_every_chat_of_that_kind() -> None:
    groups = folder(flags={"groups": True})
    assert groups.contains(facts()) is True
    assert groups.contains(facts(chat_id=CHANNEL_ID, kind=PeerKind.CHANNEL)) is False
    assert folder(flags={"broadcasts": True}).contains(
        facts(chat_id=CHANNEL_ID, kind=PeerKind.CHANNEL)
    )


def test_a_bot_is_a_bot_before_it_is_a_contact() -> None:
    """Telegram counts bots separately; a bot in the address book must not
    arrive through the ``contacts`` flag."""
    bot = facts(chat_id=FRIEND_ID, kind=PeerKind.USER, bot=True, contact=True)
    assert folder(flags={"contacts": True}).contains(bot) is False
    assert folder(flags={"bots": True}).contains(bot) is True


def test_contacts_and_strangers_are_separate_flags() -> None:
    known = facts(chat_id=FRIEND_ID, kind=PeerKind.USER, contact=True)
    stranger = facts(chat_id=FRIEND_ID, kind=PeerKind.USER, contact=False)
    assert folder(flags={"contacts": True}).contains(known) is True
    assert folder(flags={"contacts": True}).contains(stranger) is False
    assert folder(flags={"non_contacts": True}).contains(stranger) is True


def test_the_exclude_flags_drop_a_chat_the_kind_flag_admitted() -> None:
    admitted = {"groups": True}
    assert folder(flags={**admitted, "exclude_muted": True}).contains(facts(muted=True)) is False
    assert folder(flags={**admitted, "exclude_read": True}).contains(facts()) is False
    assert folder(flags={**admitted, "exclude_read": True}).contains(facts(unread=2)) is True
    assert (
        folder(flags={**admitted, "exclude_archived": True}).contains(facts(archived=True)) is False
    )


def test_muting_a_group_does_not_mute_being_named_in_it() -> None:
    """``exclude_muted`` is narrower than its name: the official clients keep a
    muted chat that has an unread mention, because muting a group is a statement
    about its chatter, not about being addressed by name."""
    view = folder(flags={"groups": True, "exclude_muted": True})
    assert view.contains(facts(muted=True, unread=40)) is False
    assert view.contains(facts(muted=True, unread=40, mentions=1)) is True


def test_a_chat_marked_unread_by_hand_is_not_read() -> None:
    """Marking a chat unread by hand is the user's most deliberate statement
    that it is not finished; counting only messages drops exactly that chat."""
    view = folder(flags={"groups": True, "exclude_read": True})
    assert view.contains(facts()) is False
    assert view.contains(facts(unread_mark=True)) is True


def test_an_exclude_flag_does_not_drop_a_chat_the_user_named() -> None:
    """Naming a chat is the more specific statement of the two."""
    view = folder(flags={"groups": True, "exclude_muted": True}, include=frozenset({GROUP_ID}))
    assert view.contains(facts(muted=True)) is True


def test_a_shareable_folder_admits_nothing_it_was_not_given() -> None:
    view = folder(shareable=True, include=frozenset({CHANNEL_ID}))
    assert view.contains(facts()) is False
    assert view.contains(facts(chat_id=CHANNEL_ID, kind=PeerKind.CHANNEL)) is True


# --- resolving what the caller typed ----------------------------------------


def views() -> list[FolderView]:
    return [folder(id=2, title="Work"), folder(id=3, title="Work in progress")]


def test_a_folder_is_found_by_id() -> None:
    assert resolve_folder(views(), "3").id == 3


def test_a_folder_is_found_by_name_regardless_of_case() -> None:
    assert resolve_folder(views(), "work").id == 2


def test_an_exact_name_wins_over_a_longer_one_containing_it() -> None:
    """Otherwise naming a folder exactly would be ambiguous with its neighbour."""
    assert resolve_folder(views(), "Work").id == 2


def test_a_partial_name_still_finds_the_only_folder_it_fits() -> None:
    assert resolve_folder(views(), "in progress").id == 3


def test_a_name_that_matches_two_folders_is_refused_rather_than_guessed() -> None:
    pair = [folder(id=2, title="Work stuff"), folder(id=3, title="Work things")]
    with pytest.raises(InvalidInput) as excinfo:
        resolve_folder(pair, "work")
    assert "several" in excinfo.value.message
    assert excinfo.value.details == {"folder_ids": [2, 3]}


def test_a_folder_may_be_named_after_a_number() -> None:
    """A digit is tried as an id first, then as an exact name — Telegram lets a
    folder be called "2", and refusing to find it would be this tool's rule and
    not Telegram's."""
    numbered = [folder(id=7, title="2"), folder(id=8, title="Work 2024")]
    assert resolve_folder(numbered, "2").id == 7
    # The id still wins where there is one: it is the unambiguous form.
    assert resolve_folder([*numbered, folder(id=2, title="Archive")], "2").id == 2
    # And a number never matches a title by substring: "Work 2024" contains it.
    with pytest.raises(NotFound):
        resolve_folder([folder(id=8, title="Work 2024")], "2")


def test_a_number_that_is_neither_an_id_nor_a_name_is_not_found() -> None:
    with pytest.raises(NotFound):
        resolve_folder([folder(id=2, title="Work 2024")], "9")


def test_an_unknown_folder_is_a_not_found_that_names_no_folders() -> None:
    """The error text must not carry titles: it is written by a stranger's
    account and never passes the untrusted boundary a payload does."""
    with pytest.raises(NotFound) as excinfo:
        resolve_folder(views(), "Holidays")
    assert "Work" not in excinfo.value.message


def test_asking_for_a_folder_on_an_account_that_has_none_says_so() -> None:
    with pytest.raises(NotFound) as excinfo:
        resolve_folder([], "Work")
    assert "no chat folders" in excinfo.value.message


# --- the listing payload ----------------------------------------------------


def test_a_folder_hides_the_peers_the_floor_closes() -> None:
    view = folder(include=frozenset({GROUP_ID, SERVICE_ID}))
    row = folder_summary(view, include_private=True)
    assert row["include_peers"] == [GROUP_ID]
    assert row["hidden_peers"] == 1


def test_saved_messages_stays_hidden_when_a_folder_names_it_by_id() -> None:
    """A folder containing Saved Messages stores the account's *own user id* —
    an ordinary positive number. Without knowing which one it is, the floor
    cannot fire, which is why the id is passed in rather than inferred."""
    view = folder(include=frozenset({GROUP_ID, SELF_ID}))
    row = folder_summary(view, include_private=True, self_id=SELF_ID)
    assert row["include_peers"] == [GROUP_ID]
    assert row["hidden_peers"] == 1


def test_a_peer_with_no_id_of_its_own_is_counted_not_dropped() -> None:
    """``InputPeerSelf`` is Saved Messages under another spelling. Dropping it
    silently would make a hidden chat and an absent one look identical."""
    (view,) = parse_folders(
        FakeFilterList(
            [
                FakeFilter(
                    id=2,
                    title="Everything",
                    include_peers=[FakeInputSelf(), FakeInputChannel(4242)],
                )
            ]
        )
    )
    assert view.opaque_peers == 1
    row = folder_summary(view, include_private=True, self_id=SELF_ID)
    assert row["include_peers"] == [GROUP_ID]
    assert row["hidden_peers"] == 1


def test_knowing_who_the_account_is_costs_a_call_only_when_it_matters() -> None:
    """With DM enumeration off, every positive id is withheld anyway."""
    views = [folder(include=frozenset({GROUP_ID, SELF_ID}))]
    assert needs_self_id(views, include_private=False) is False
    assert needs_self_id(views, include_private=True) is True
    assert needs_self_id([folder(include=frozenset({GROUP_ID}))], include_private=True) is False


def test_a_folder_hides_private_peers_until_dm_enumeration_is_on() -> None:
    view = folder(include=frozenset({GROUP_ID, FRIEND_ID}))
    assert folder_summary(view, include_private=False)["include_peers"] == [GROUP_ID]
    assert folder_summary(view, include_private=True)["include_peers"] == [GROUP_ID, FRIEND_ID]


# --- driving the handlers ---------------------------------------------------


@dataclass
class FakeNotify:
    silent: bool = False
    mute_until: Any = None


@dataclass
class FakeRawDialog:
    notify_settings: Any = None


@dataclass
class FakeDialog:
    entity: Any
    unread_count: int = 0
    unread_mentions_count: int = 0
    pinned: bool = False
    archived: bool = False
    message: Any = None
    date: datetime = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    name: str | None = None
    dialog: Any = field(default_factory=FakeRawDialog)


class FakeClient:
    """Just enough client for a read handler: dialogs, folders, mute defaults."""

    def __init__(self, dialogs: list[Any], filters: Any, notify: Any = None) -> None:
        self._dialogs = dialogs
        self._filters = filters
        #: What `account.getNotifySettings` answers, by the switch asked about.
        #: An account that has changed nothing answers with settings that say
        #: nothing, which is what the empty default stands for here.
        self._notify: dict[str, Any] = notify or {}
        self.requests: list[Any] = []
        self.asked_who_i_am = 0

    def is_connected(self) -> bool:
        return True

    async def is_user_authorized(self) -> bool:
        return True

    async def get_me(self) -> Any:
        from telethon.tl.types import User

        self.asked_who_i_am += 1
        return User(id=SELF_ID, first_name="Me", is_self=True)

    def iter_dialogs(self, **_kwargs: Any) -> Any:
        async def stream() -> Any:
            for dialog in self._dialogs:
                yield dialog

        return stream()

    async def __call__(self, request: Any) -> Any:
        from telethon.tl import functions
        from telethon.tl import types as tl

        self.requests.append(request)
        if isinstance(request, functions.account.GetNotifySettingsRequest):
            switch = type(request.peer).__name__.removeprefix("InputNotify").lower()
            return self._notify.get(switch, tl.PeerNotifySettings())
        return self._filters


@dataclass
class FakeAccount:
    client: Any
    spec: Any = None


class FakeRegistry:
    def __init__(self, client: Any, label: str = "main") -> None:
        self._client = client
        self._label = label

    def list_accounts(self) -> list[Any]:
        return [FakeAccountRow(self._label)]

    def get(self, _label: str) -> Any:
        return None

    def load_account(self, _label: str) -> FakeAccount:
        return FakeAccount(client=self._client)


@dataclass
class FakeAccountRow:
    label: str


def telegram_entities() -> tuple[Any, Any, Any]:
    from telethon.tl.types import Channel, User

    group = Channel(id=4242, title="Marketing", photo=None, date=None, megagroup=True)
    friend = User(id=FRIEND_ID, first_name="Someone", contact=True)
    service = User(id=SERVICE_ID, first_name="Telegram")
    return group, friend, service


def context(tmp_path, client: Any, **overrides: Any) -> OperationContext:
    settings = Settings(**overrides)
    return OperationContext(
        settings=settings,
        safety=SafetyKernel(settings),
        plans=None,  # type: ignore[arg-type] - unused by read handlers
        limits=None,  # type: ignore[arg-type]
        audit=AuditLog(tmp_path / "audit.log", settings.audit),
        actor="cli",
        accounts=FakeRegistry(client),  # type: ignore[arg-type]
    )


def everything_folder() -> FakeFilterList:
    """One folder that names every chat below, including the closed ones."""
    return FakeFilterList(
        [
            FakeFilter(
                id=2,
                title="Everything",
                include_peers=[
                    FakeInputChannel(4242),
                    FakeInputUser(FRIEND_ID),
                    FakeInputUser(SERVICE_ID),
                    FakeInputUser(SELF_ID),
                ],
            )
        ]
    )


@pytest.mark.asyncio
async def test_a_folder_cannot_admit_a_chat_the_policy_closes(tmp_path) -> None:
    """The point of the whole feature: a folder is the user's sorting, not a
    permission. Service Notifications is closed in code, and private chats are
    out of enumeration until a switch says otherwise — a folder naming both
    changes neither answer."""
    group, friend, service = telegram_entities()
    client = FakeClient(
        [FakeDialog(entity=group), FakeDialog(entity=friend), FakeDialog(entity=service)],
        everything_folder(),
    )
    ctx = context(tmp_path, client)

    envelope = await handle_chats(ctx, ChatsInput(folder="Everything"))

    ids = [row["chat_id"] for row in envelope.data["chats"]]
    assert ids == [GROUP_ID]
    assert any("private chat" in warning for warning in envelope.warnings)


@pytest.mark.asyncio
async def test_a_folder_filters_the_listing_it_is_given(tmp_path) -> None:
    group, _friend, _service = telegram_entities()
    from telethon.tl.types import Channel

    other = Channel(id=4343, title="News", photo=None, date=None, broadcast=True)
    client = FakeClient(
        [FakeDialog(entity=group), FakeDialog(entity=other)],
        FakeFilterList([FakeFilter(id=2, title="Groups", groups=True)]),
    )
    ctx = context(tmp_path, client)

    envelope = await handle_chats(ctx, ChatsInput(folder="2"))

    assert [row["chat_id"] for row in envelope.data["chats"]] == [GROUP_ID]
    assert envelope.meta.extra["folder_id"] == 2


@pytest.mark.asyncio
async def test_without_a_folder_nothing_asks_telegram_for_one(tmp_path) -> None:
    """The extra round trip is paid for only when a folder was named."""
    group, _friend, _service = telegram_entities()
    client = FakeClient([FakeDialog(entity=group)], everything_folder())
    ctx = context(tmp_path, client)

    await handle_chats(ctx, ChatsInput())

    assert client.requests == []


@pytest.mark.asyncio
async def test_the_inbox_can_be_narrowed_to_a_folder(tmp_path) -> None:
    group, _friend, _service = telegram_entities()
    from telethon.tl.types import Channel

    other = Channel(id=4343, title="News", photo=None, date=None, broadcast=True)
    client = FakeClient(
        [
            FakeDialog(entity=group, unread_count=3),
            FakeDialog(entity=other, unread_count=7),
        ],
        FakeFilterList([FakeFilter(id=2, title="Groups", groups=True)]),
    )
    ctx = context(tmp_path, client)

    envelope = await handle_inbox(ctx, InboxInput(folder="Groups"))

    assert [row["chat_id"] for row in envelope.data["waiting"]] == [GROUP_ID]


# --- muted is a question about the account, not only about the chat ---------


def _muted_switch() -> Any:
    """A global switch someone turned on, in the shape Telegram sends it."""
    from telethon.tl import types as tl

    return tl.PeerNotifySettings(silent=True)


@pytest.mark.asyncio
async def test_a_globally_muted_kind_of_chat_stays_out_of_the_inbox(tmp_path) -> None:
    """One gesture mutes every group, and no group carries a setting of its own.

    Reading only per-chat settings answers "nothing is muted" here, which is the
    opposite of what the account's owner said.
    """
    group, _friend, _service = telegram_entities()
    client = FakeClient(
        [FakeDialog(entity=group, unread_count=3)],
        everything_folder(),
        notify={"chats": _muted_switch()},
    )
    ctx = context(tmp_path, client)

    envelope = await handle_inbox(ctx, InboxInput())

    assert envelope.data["waiting"] == []


@pytest.mark.asyncio
async def test_the_global_switch_applies_only_to_its_own_kind(tmp_path) -> None:
    """Muting every group says nothing about channels."""
    from telethon.tl.types import Channel

    group, _friend, _service = telegram_entities()
    news = Channel(id=4343, title="News", photo=None, date=None, broadcast=True)
    client = FakeClient(
        [FakeDialog(entity=group, unread_count=3), FakeDialog(entity=news, unread_count=7)],
        everything_folder(),
        notify={"chats": _muted_switch()},
    )
    ctx = context(tmp_path, client)

    envelope = await handle_inbox(ctx, InboxInput())

    from telethon import utils

    assert [row["chat_id"] for row in envelope.data["waiting"]] == [utils.get_peer_id(news)]


@pytest.mark.asyncio
async def test_a_chat_unmuted_on_purpose_beats_the_global_switch(tmp_path) -> None:
    """The per-chat setting is the more specific statement, either way it points."""
    from telethon.tl import types as tl

    group, _friend, _service = telegram_entities()
    client = FakeClient(
        [
            FakeDialog(
                entity=group,
                unread_count=3,
                dialog=FakeRawDialog(notify_settings=tl.PeerNotifySettings(silent=False)),
            )
        ],
        everything_folder(),
        notify={"chats": _muted_switch()},
    )
    ctx = context(tmp_path, client)

    envelope = await handle_inbox(ctx, InboxInput())

    assert [row["chat_id"] for row in envelope.data["waiting"]] == [GROUP_ID]


@pytest.mark.asyncio
async def test_an_expired_mute_is_not_a_mute(tmp_path) -> None:
    """Telegram stores a mute as an expiry, and a past one means audible again."""
    from telethon.tl import types as tl

    group, _friend, _service = telegram_entities()
    client = FakeClient(
        [
            FakeDialog(
                entity=group,
                unread_count=3,
                dialog=FakeRawDialog(
                    notify_settings=tl.PeerNotifySettings(
                        mute_until=datetime(2020, 1, 1, tzinfo=UTC)
                    )
                ),
            )
        ],
        everything_folder(),
    )
    ctx = context(tmp_path, client)

    envelope = await handle_inbox(ctx, InboxInput())

    assert [row["chat_id"] for row in envelope.data["waiting"]] == [GROUP_ID]


@pytest.mark.asyncio
async def test_a_far_future_mute_is_the_other_shape_of_the_same_switch(tmp_path) -> None:
    """Clients mute "forever" by writing an expiry far ahead, not only by flag.

    Both shapes are the account's global switch for groups, and the inbox has
    to read either one.
    """
    from telethon.tl import types as tl

    group, _friend, _service = telegram_entities()
    client = FakeClient(
        [FakeDialog(entity=group, unread_count=3)],
        everything_folder(),
        notify={"chats": tl.PeerNotifySettings(mute_until=datetime(2099, 1, 1, tzinfo=UTC))},
    )
    ctx = context(tmp_path, client)

    envelope = await handle_inbox(ctx, InboxInput())

    assert envelope.data["waiting"] == []


@pytest.mark.asyncio
async def test_asking_for_muted_chats_asks_telegram_nothing_extra(tmp_path) -> None:
    """Three requests for an answer nobody reads are three ways to fail."""
    group, _friend, _service = telegram_entities()
    client = FakeClient([FakeDialog(entity=group, unread_count=3)], everything_folder())
    ctx = context(tmp_path, client)

    await handle_inbox(ctx, InboxInput(include_muted=True))

    assert client.requests == []


@pytest.mark.asyncio
async def test_a_folder_that_excludes_muted_uses_the_global_switch(tmp_path) -> None:
    """The same inheritance, on the other listing and through a folder rule."""
    group, _friend, _service = telegram_entities()
    client = FakeClient(
        [FakeDialog(entity=group)],
        FakeFilterList([FakeFilter(id=2, title="Loud", groups=True, exclude_muted=True)]),
        notify={"chats": _muted_switch()},
    )
    ctx = context(tmp_path, client)

    envelope = await handle_chats(ctx, ChatsInput(folder="Loud"))

    assert envelope.data["chats"] == []


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({}, None),
        ({"silent": True}, True),
        ({"silent": False}, False),
        ({"mute_until": datetime(2099, 1, 1, tzinfo=UTC)}, True),
        ({"mute_until": datetime(2020, 1, 1, tzinfo=UTC)}, False),
        ({"silent": False, "mute_until": datetime(2099, 1, 1, tzinfo=UTC)}, True),
    ],
    ids=["says-nothing", "muted", "unmuted", "until-future", "until-past", "unmuted-but-until"],
)
def test_what_one_settings_object_states(settings: dict[str, Any], expected: bool | None) -> None:
    """The three answers, in the shape Telegram sends them.

    `None` is not `False`: it is the difference between "this chat is audible"
    and "this chat has not been given an opinion of its own".
    """
    from telethon.tl import types as tl

    from telegram_ai_cli.ops.folders import _notify_says_muted

    assert _notify_says_muted(tl.PeerNotifySettings(**settings)) is expected


@pytest.mark.parametrize(
    ("until", "expected"),
    [(0, False), (1600000000, False), (4102444800, True)],
    ids=["epoch", "long-past", "far-future"],
)
def test_an_expiry_that_arrives_as_a_number_is_still_an_expiry(until: int, expected: bool) -> None:
    """Telethon hands over a datetime. Another library, or a fixture, may not —
    and `bool(seconds)` would call every long-expired mute a live one."""
    from telegram_ai_cli.ops.folders import _notify_says_muted

    class RawSettings:
        silent = None
        mute_until = until

    assert _notify_says_muted(RawSettings()) is expected


@pytest.mark.asyncio
async def test_listing_folders_withholds_the_chats_the_floor_closes(tmp_path) -> None:
    """Even with DM enumeration on, a folder cannot name Saved Messages or
    Service Notifications back to the caller — it reports a count instead."""
    client = FakeClient([], everything_folder())
    ctx = context(tmp_path, client, safety={"read": {"enumerate_dms": True}})

    envelope = await handle_folders(ctx, FoldersInput(include_private=True))

    (row,) = envelope.data["folders"]
    assert row["include_peers"] == [GROUP_ID, FRIEND_ID]
    assert row["hidden_peers"] == 2
    # Saved Messages is only recognisable once the account's own id is known.
    assert client.asked_who_i_am == 1


@pytest.mark.asyncio
async def test_listing_folders_does_not_ask_who_the_account_is_by_default(tmp_path) -> None:
    client = FakeClient([], everything_folder())
    ctx = context(tmp_path, client)

    envelope = await handle_folders(ctx, FoldersInput())

    (row,) = envelope.data["folders"]
    assert row["include_peers"] == [GROUP_ID]
    assert client.asked_who_i_am == 0


@pytest.mark.asyncio
async def test_an_account_with_no_folders_gets_an_empty_list_not_an_error(tmp_path) -> None:
    client = FakeClient([], FakeFilterList([]))
    ctx = context(tmp_path, client)

    envelope = await handle_folders(ctx, FoldersInput())

    assert envelope.ok is True
    assert envelope.data["folders"] == []
    assert any("no chat folders" in warning for warning in envelope.warnings)


@pytest.mark.asyncio
async def test_listing_folders_reports_their_names_as_untrusted(tmp_path) -> None:
    """A folder name is typed by a person; it crosses the same boundary a
    message body does."""
    client = FakeClient([], everything_folder())
    ctx = context(tmp_path, client)

    envelope = await handle_folders(ctx, FoldersInput())

    (row,) = envelope.data["folders"]
    assert unwrap(row["title"]) == "Everything"
    assert row["title"] != "Everything"
    assert envelope.meta.untrusted_content is True
