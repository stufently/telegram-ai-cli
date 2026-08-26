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
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from telegram_ai_cli_mcp import db
from telegram_ai_cli_mcp.audit import AuditLog
from telegram_ai_cli_mcp.config import PlansConfig, Settings
from telegram_ai_cli_mcp.context import OperationContext
from telegram_ai_cli_mcp.errors import (
    InvalidInput,
    NotAllowlisted,
    NotFound,
    PlanPreconditionFailed,
    ProfileForbidden,
)
from telegram_ai_cli_mcp.ops.chats import ChatsInput, handle_chats
from telegram_ai_cli_mcp.ops.folder_write import (
    FolderAddInput,
    add_peer_to_filter,
    plan_folder_add,
    raw_filter_for,
    recheck_folder,
)
from telegram_ai_cli_mcp.ops.folders import (
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
from telegram_ai_cli_mcp.ops.inbox import InboxInput, handle_inbox
from telegram_ai_cli_mcp.plans import PlanStore
from telegram_ai_cli_mcp.safety import PeerKind, SafetyKernel
from telegram_ai_cli_mcp.untrusted import unwrap

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


# --- putting a chat into a folder -------------------------------------------
#
# Telegram replaces the whole filter on every edit, so the property under test
# is not "the peer arrived" but "nothing else left". A folder on a real account
# names private chats the read policy hides, and a rebuild-from-what-we-can-see
# would silently drop exactly those.

#: Computed, never written out: the marked form of a channel id is shaped
#: exactly like a real supergroup id, and the repository's own scan for private
#: data rejects that literal on sight (tests/test_no_private_data.py).
NEW_CHANNEL = FakeInputChannel(channel_id=42)
NEW_CHANNEL_ID = input_peer_id(NEW_CHANNEL)


def test_the_filter_that_is_edited_is_the_object_telegram_sent() -> None:
    """Identity, not equality: a copy could be built from the sanitized view."""
    wanted = FakeFilter(id=2, title=FakeTitle("постоянные"))
    result = FakeFilterList(filters=[FakeDefault(), FakeFilter(id=1, title="other"), wanted])

    assert raw_filter_for(result, 2) is wanted


def test_a_folder_that_disappeared_between_review_and_apply_is_not_found() -> None:
    with pytest.raises(NotFound):
        raw_filter_for(FakeFilterList(filters=[FakeFilter(id=1, title="other")]), 2)


def test_adding_a_chat_keeps_every_peer_the_folder_already_named() -> None:
    """Including the ones this configuration would refuse to name back."""
    private = FakeInputUser(user_id=777)
    opaque = FakeInputSelf()
    item = FakeFilter(id=2, title="постоянные", include_peers=[private, opaque])

    assert add_peer_to_filter(item, NEW_CHANNEL, NEW_CHANNEL_ID) is True

    assert item.include_peers[0] is private
    assert item.include_peers[1] is opaque
    assert len(item.include_peers) == 3


def test_adding_a_chat_the_folder_already_has_changes_nothing() -> None:
    item = FakeFilter(id=2, title="постоянные", include_peers=[FakeInputChannel(channel_id=42)])

    assert add_peer_to_filter(item, NEW_CHANNEL, NEW_CHANNEL_ID) is False
    assert len(item.include_peers) == 1


def test_a_chat_the_folder_excludes_is_refused_rather_than_contradicted() -> None:
    item = FakeFilter(id=2, title="постоянные", exclude_peers=[FakeInputChannel(channel_id=42)])

    with pytest.raises(InvalidInput):
        add_peer_to_filter(item, NEW_CHANNEL, NEW_CHANNEL_ID)

    assert item.include_peers == []


def test_a_folder_that_still_matches_the_plan_passes_verification() -> None:
    recheck_folder({"folder_id": 2}, folder(id=2), NEW_CHANNEL_ID)


@pytest.mark.parametrize(
    "drift",
    [
        pytest.param({"id": 3}, id="the name now points at another folder"),
        pytest.param({"id": 2, "shareable": True}, id="it became a shareable folder"),
        pytest.param(
            {"id": 2, "exclude": frozenset({NEW_CHANNEL_ID})},
            id="the chat has since been excluded",
        ),
    ],
)
def test_a_folder_that_drifted_since_review_fails_the_precondition(
    drift: dict[str, Any],
) -> None:
    """Refused in verification, not in the applier.

    All three are things a person does in Telegram between review and apply. A
    refusal raised later has already reserved a rate-limit slot and written an
    audit *attempt* for something that never happened.
    """
    with pytest.raises(PlanPreconditionFailed):
        recheck_folder({"folder_id": 2}, folder(**drift), NEW_CHANNEL_ID)


def test_a_chat_only_pinned_in_the_folder_is_already_in_it() -> None:
    """Otherwise the plan says "nothing changes" and the applier appends a duplicate."""
    item = FakeFilter(id=2, title="постоянные", pinned_peers=[FakeInputChannel(channel_id=42)])

    assert add_peer_to_filter(item, NEW_CHANNEL, NEW_CHANNEL_ID) is False
    assert item.include_peers == []


def test_a_shareable_folder_is_refused_because_its_members_follow_a_link() -> None:
    """`DialogFilterChatlist` *has* an `include_peers`; what it lacks is the flags.

    Recognising it by a missing peer list would be a guard that never fires on
    the real type — the shape that reaches this function in production.
    """
    item = FakeChatlist(id=9, title="shared")
    assert hasattr(item, "include_peers")

    with pytest.raises(InvalidInput):
        add_peer_to_filter(item, NEW_CHANNEL, NEW_CHANNEL_ID)

    assert item.include_peers == []


# --- the planner, run rather than reasoned about -----------------------------
#
# Everything above is a pure helper, and the planner is what calls them in
# order. Nothing had ever *run* `plan_folder_add`: its first version named
# `resolve_chat_argument` while importing `resolve_peer`, which is a `NameError`
# on every possible call — a whole operation that could not execute once, with
# the full suite green. Only the linter noticed.
#
# So these drive the planner itself. The helpers are still tested above,
# because a test that only reaches them through the planner cannot say which of
# the two is wrong.
#
# The client and context builders live further down, beside the ones the read
# handlers use: the folders are the same stand-ins either way, and a second set
# would drift from the first.


@pytest.mark.asyncio
async def test_the_plan_pins_the_folder_by_id_and_promises_to_remove_nothing(
    planning_context: Any,
) -> None:
    """The summary is what a person approves, so it states the count and the id.

    The count includes the peer this configuration would refuse to name back —
    `InputPeerSelf` here — because "2 chats stay" is the promise being made, and
    a count of only the visible ones would understate what an edit puts at risk.
    """
    client = planning_client(
        FakeFilterList(
            [
                FakeFilter(
                    id=2,
                    title="постоянные",
                    include_peers=[FakeInputUser(FRIEND_ID), FakeInputSelf()],
                )
            ]
        )
    )
    ctx = planning_context(client)

    plan = await plan_folder_add(ctx, FolderAddInput(folder="постоянные", chat=GROUP_ID))

    assert plan.operation == "folders.add"
    assert "Marketing" in plan.summary
    assert "names 2 chat(s)" in plan.summary
    assert "Nothing is removed" in plan.summary
    # The id, not the name that found it: a name is re-typed by a person and can
    # point at another folder by the time the plan is applied.
    assert plan.preconditions["folder_id"] == 2
    assert plan.preconditions["peer"]["peer_id"] == GROUP_ID


@pytest.mark.asyncio
async def test_a_chat_the_folder_already_names_is_planned_as_changing_nothing(
    planning_context: Any,
) -> None:
    """Pinned counts as being in the folder, and the summary has to say so.

    Otherwise the plan promises a change and the applier answers "already
    there", which is the one sentence a reviewer cannot check afterwards.
    """
    client = planning_client(
        FakeFilterList(
            [FakeFilter(id=2, title="постоянные", pinned_peers=[FakeInputChannel(4242)])]
        )
    )
    ctx = planning_context(client)

    plan = await plan_folder_add(ctx, FolderAddInput(folder="2", chat=GROUP_ID))

    assert "already in the folder" in plan.summary


@pytest.mark.asyncio
async def test_planning_into_a_shareable_folder_is_refused_before_a_plan_exists(
    planning_context: Any,
) -> None:
    """A folder whose members follow an invite link is not edited from here.

    Refused in the planner and not only in the applier: a plan nobody can apply
    should never reach the review queue in the first place.
    """
    client = planning_client(FakeFilterList([FakeChatlist(id=9, title="shared")]))
    ctx = planning_context(client)

    with pytest.raises(InvalidInput):
        await plan_folder_add(ctx, FolderAddInput(folder="9", chat=GROUP_ID))

    assert not ctx.plans.list()


@pytest.mark.asyncio
async def test_planning_a_chat_the_folder_excludes_is_refused_rather_than_contradicted(
    planning_context: Any,
) -> None:
    """Including a chat the folder excludes would leave it contradicting itself."""
    client = planning_client(
        FakeFilterList(
            [FakeFilter(id=2, title="постоянные", exclude_peers=[FakeInputChannel(4242)])]
        )
    )
    ctx = planning_context(client)

    with pytest.raises(InvalidInput):
        await plan_folder_add(ctx, FolderAddInput(folder="2", chat=GROUP_ID))

    assert not ctx.plans.list()


@pytest.mark.asyncio
async def test_a_chat_outside_the_write_policy_is_refused(planning_context: Any) -> None:
    """A folder is the account's own sorting, and it is still a write.

    The chat has to be one this configuration may act on, or a folder becomes a
    way to touch a chat the policy closed.
    """
    client = planning_client(FakeFilterList([FakeFilter(id=2, title="постоянные")]))
    ctx = planning_context(client, send_allow=[])

    with pytest.raises(NotAllowlisted):
        await plan_folder_add(ctx, FolderAddInput(folder="2", chat=GROUP_ID))

    assert not ctx.plans.list()
    # And refused before the folders were fetched. Resolving the chat is
    # unavoidable; asking the account for anything else is not, and a folder
    # that does not exist must not answer "no such folder" to a caller who was
    # never allowed to touch the chat in the first place.
    assert client.requests == []


@pytest.mark.asyncio
async def test_a_plan_written_from_a_link_is_verified_against_the_same_chat(
    planning_context: Any,
) -> None:
    """The planner understands `t.me/…/412`; verification has to understand it too.

    Asserted on the argument handed to Telethon, not on the answer, because
    this fake resolves whatever it is given and the real client does not: it
    cannot resolve a link that names a *message*, and `folder add` documents a
    link as one of the things its `chat` accepts. Verifying with the plain
    resolver therefore refused every plan written from a link — at apply time,
    after a person had already approved it.
    """
    from telegram_ai_cli_mcp.apply import _verify

    link = "https://t.me/marketing/412"
    client = planning_client(FakeFilterList([FakeFilter(id=2, title="постоянные")]))
    ctx = planning_context(client)

    plan = await plan_folder_add(ctx, FolderAddInput(folder="2", chat=link))
    await _verify(ctx, client, plan, FolderAddInput(folder="2", chat=link))

    planned, verified = client.resolved
    assert verified == planned
    # The message part is dropped by both, on purpose: a folder holds chats.
    assert "412" not in str(planned)


@pytest.mark.asyncio
async def test_readonly_refuses_to_plan_before_telegram_is_touched(planning_context: Any) -> None:
    """Resolving a username is itself an observable act on the account."""
    client = planning_client(FakeFilterList([FakeFilter(id=2, title="постоянные")]))
    ctx = planning_context(client, profile="readonly")

    with pytest.raises(ProfileForbidden):
        await plan_folder_add(ctx, FolderAddInput(folder="2", chat=GROUP_ID))

    assert client.resolved == []
    assert client.requests == []


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
    """Just enough client for a read handler: dialogs, folders, mute defaults.

    ``entity`` is what planning adds: a planner resolves the chat it is given
    before it looks at any folder. It stays optional and unset by default, so a
    read handler that starts resolving entities fails here rather than being
    answered by something irrelevant.
    """

    def __init__(
        self, dialogs: list[Any], filters: Any, notify: Any = None, entity: Any = None
    ) -> None:
        self._dialogs = dialogs
        self._filters = filters
        #: What `account.getNotifySettings` answers, by the switch asked about.
        #: An account that has changed nothing answers with settings that say
        #: nothing, which is what the empty default stands for here.
        self._notify: dict[str, Any] = notify or {}
        self._entity = entity
        self.requests: list[Any] = []
        self.resolved: list[Any] = []
        self.asked_who_i_am = 0

    def is_connected(self) -> bool:
        return True

    async def is_user_authorized(self) -> bool:
        return True

    async def get_entity(self, target: Any) -> Any:
        self.resolved.append(target)
        if self._entity is None:
            # What Telethon raises for a handle it cannot resolve, which is what
            # `resolve_peer` turns into a `PeerUnresolved`. Returning a stand-in
            # instead would let a planner act on a chat nobody named.
            raise ValueError(f"no entity was canned for {target!r}")
        return self._entity

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


def planning_client(filters: Any) -> FakeClient:
    """A client for the write path: one resolvable chat and this account's folders.

    No dialogs at all — planning never enumerates them, and an empty list is the
    assertion that it does not start.
    """
    group, _friend, _service = telegram_entities()
    return FakeClient([], filters, entity=group)


@pytest.fixture
def planning_context(tmp_path) -> Iterator[Any]:
    """Build a context that can *store* a plan, around a fake client.

    `context` above hands the read handlers ``plans=None``, which is all a
    listing needs. A planner's entire output is a stored plan, so this one owns
    a real store — and closes its connection on the way out rather than leaving
    it to the garbage collector.

    ``send_allow`` defaults to the one group these tests plan against: a folder
    is the account's own sorting, but putting a chat into one is still a write,
    and the write policy is what says which chats may be touched.
    """
    connection = db.connect(tmp_path / "plans.db")

    def build(
        client: Any,
        *,
        profile: str = "plan",
        send_allow: list[Any] | None = None,
    ) -> OperationContext:
        settings = Settings(
            profile=profile,  # type: ignore[arg-type]
            plans={"encrypt_bodies": False},
            safety={"write": {"send": {"allow": [GROUP_ID] if send_allow is None else send_allow}}},
        )
        return OperationContext(
            settings=settings,
            safety=SafetyKernel(settings),
            plans=PlanStore(connection, PlansConfig(encrypt_bodies=False)),
            limits=None,  # type: ignore[arg-type]
            audit=AuditLog(tmp_path / "audit.log", settings.audit),
            actor="cli",
            accounts=FakeRegistry(client),  # type: ignore[arg-type]
        )

    yield build
    connection.close()


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

    from telegram_ai_cli_mcp.ops.folders import _notify_says_muted

    assert _notify_says_muted(tl.PeerNotifySettings(**settings)) is expected


@pytest.mark.parametrize(
    ("until", "expected"),
    [(0, False), (1600000000, False), (4102444800, True)],
    ids=["epoch", "long-past", "far-future"],
)
def test_an_expiry_that_arrives_as_a_number_is_still_an_expiry(until: int, expected: bool) -> None:
    """Telethon hands over a datetime. Another library, or a fixture, may not —
    and `bool(seconds)` would call every long-expired mute a live one."""
    from telegram_ai_cli_mcp.ops.folders import _notify_says_muted

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
