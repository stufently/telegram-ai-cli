"""Reacting and pinning — the two planned writes that act on other people's messages.

Everything else under ``write.py`` either speaks as the account (send, reply) or
refuses to touch a message somebody else wrote (edit, delete). These four do the
opposite on purpose: a reaction is *for* other people's messages, and pinning
one is the whole point of pinning. That removes the ``_require_own_message``
guard, so the review text has to carry the weight instead — which is what most
of the assertions below are about.

Three properties are the reason this file exists.

**A no-op never reaches the review queue.** Reacting with the reaction this
account already left, removing one it never left, pinning what is already
pinned, unpinning what is not pinned — each is refused while the plan is being
written, not discovered at apply time. A plan that does nothing still costs a
person the read.

**The preview names the effect, not the verb.** Which emoji, on which message,
in which chat, quoting the message; and for a pin, whether everyone in the chat
gets a notification, plus whose message is being pinned or unpinned.

**The profile is checked before the network.** Under ``readonly`` these refuse
without resolving anything, because resolving a username is itself an
observable act.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from telethon.tl import types as tl

from telegram_ai_cli_mcp import db
from telegram_ai_cli_mcp.apply import _LIMIT_KINDS, _reaction_object, _verify_mark
from telegram_ai_cli_mcp.audit import AuditLog
from telegram_ai_cli_mcp.config import AuditConfig, PlansConfig, Settings
from telegram_ai_cli_mcp.context import OperationContext
from telegram_ai_cli_mcp.errors import (
    InvalidInput,
    NotAllowlisted,
    PlanPreconditionFailed,
    ProfileForbidden,
)
from telegram_ai_cli_mcp.ops.marks import (
    PinMessageInput,
    ReactMessageInput,
    UnpinMessageInput,
    UnreactMessageInput,
    chosen_reactions,
    describe_reaction,
    final_reactions,
    plan_pin_message,
    plan_react_message,
    plan_unpin_message,
    plan_unreact_message,
    remaining_reactions,
    requested_reaction,
    same_reactions,
)
from telegram_ai_cli_mcp.opspec import REGISTRY, Effect
from telegram_ai_cli_mcp.plans import PlanStore
from telegram_ai_cli_mcp.safety import SafetyKernel

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

GROUP_ID = 1234567890
MARKED_GROUP_ID = -1001234567890
USER_ID = 111222333

THUMB = {"kind": "emoji", "emoji": "\N{THUMBS UP SIGN}", "custom_emoji_id": None}
PARTY = {"kind": "emoji", "emoji": "\N{PARTY POPPER}", "custom_emoji_id": None}
CUSTOM = {"kind": "custom_emoji", "emoji": None, "custom_emoji_id": "5789012345678901234"}


# --- stand-ins ---------------------------------------------------------------


@dataclass
class ReactionEmoji:
    """Stands in for Telethon's own class.

    The name is load-bearing: ``_serialize.reaction_kind`` reads the class name
    rather than importing four classes, so a fake called anything else would
    serialize as a reaction type that does not exist.
    """

    emoticon: str


@dataclass
class FakeReactionCount:
    reaction: Any
    count: int = 1
    chosen_order: int | None = None


@dataclass
class FakeReactions:
    results: list[FakeReactionCount] = field(default_factory=list)
    recent_reactions: list[Any] | None = None
    can_see_list: bool = False


@dataclass
class FakeMessage:
    id: int = 412
    message: str | None = "quarterly numbers are in"
    date: datetime = NOW
    out: bool = False
    pinned: bool = False
    media: Any = None
    reactions: FakeReactions | None = None


class FakeClient:
    """Answers the two things a planner needs, and records what it was asked."""

    def __init__(self, entity: Any, messages: dict[int, Any] | None = None) -> None:
        self._entity = entity
        self._messages = messages or {}
        self.touched: list[str] = []

    def is_connected(self) -> bool:
        return True

    async def is_user_authorized(self) -> bool:
        return True

    async def get_entity(self, target: Any) -> Any:
        self.touched.append(f"get_entity:{target}")
        return self._entity

    async def get_messages(self, _peer: Any, *, ids: list[int]) -> list[Any]:
        self.touched.append(f"get_messages:{ids}")
        return [self._messages.get(one) for one in ids]


@dataclass
class FakeOpened:
    client: FakeClient
    spec: Any = None


class FakeRegistry:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    def list_accounts(self) -> list[str]:
        return ["work"]

    def load_account(self, _label: str) -> FakeOpened:
        return FakeOpened(client=self._client)

    def get(self, _label: str) -> None:
        return None


def settings_for(*, profile: str = "plan", allow: list[Any] | None = None) -> Settings:
    targets = [MARKED_GROUP_ID, USER_ID] if allow is None else allow
    return Settings(
        profile=profile,
        plans={"encrypt_bodies": False},
        safety={
            "read": {"chats": {"allow": targets}, "dms": {"allow": targets}},
            "write": {"send": {"allow": targets}, "admin": {"allow": targets}},
        },
    )


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "state.sqlite3")
    yield connection
    connection.close()


def build_ctx(
    conn: sqlite3.Connection,
    tmp_path: Path,
    client: FakeClient,
    settings: Settings | None = None,
) -> OperationContext:
    settings = settings or settings_for()
    return OperationContext(
        settings=settings,
        safety=SafetyKernel(settings),
        plans=PlanStore(conn, PlansConfig(encrypt_bodies=False)),
        limits=None,  # type: ignore[arg-type]
        audit=AuditLog(tmp_path / "audit.jsonl", AuditConfig(enabled=False)),
        actor="cli",
        accounts=FakeRegistry(client),  # type: ignore[arg-type]
    )


def group() -> tl.Channel:
    return tl.Channel(
        id=GROUP_ID, title="Marketing", photo=None, date=None, megagroup=True, username="marketing"
    )


def person() -> tl.User:
    return tl.User(id=USER_ID, first_name="Ann", username="ann")


def reacted(*emoji: str, chosen: str | None = None) -> FakeReactions:
    return FakeReactions(
        results=[
            FakeReactionCount(
                reaction=ReactionEmoji(emoticon=one),
                count=3,
                chosen_order=0 if one == chosen else None,
            )
            for one in emoji
        ]
    )


# --- the registry entries ----------------------------------------------------


NEW_OPERATIONS = ["message.react", "message.unreact", "message.pin", "message.unpin"]


@pytest.mark.parametrize("name", NEW_OPERATIONS)
def test_each_new_operation_is_a_planned_write_with_no_direct_tool(name: str) -> None:
    """The invariant that matters, asserted per operation rather than in bulk."""
    op = REGISTRY.by_name(name)
    assert op.effect is Effect.REMOTE_WRITE
    assert op.mcp_tool is None
    assert op.plan_tool is not None
    assert op.planner is not None and op.handler is None


@pytest.mark.parametrize("name", NEW_OPERATIONS)
def test_each_new_operation_draws_on_a_budget(name: str) -> None:
    """A write with no limit kind is refused at apply time; catch it here."""
    assert name in _LIMIT_KINDS


def test_no_remote_write_can_be_applied_without_a_budget() -> None:
    """The applier refuses an unbudgeted plan; that must not be how it is found."""
    unbudgeted = [
        op.name for op in REGISTRY.all() if op.is_remote_write and op.name not in _LIMIT_KINDS
    ]
    assert unbudgeted == []


# --- the pure part: what the final reaction list becomes ---------------------


def test_a_message_reports_only_the_reactions_this_account_left() -> None:
    message = FakeMessage(
        reactions=reacted("\N{THUMBS UP SIGN}", "\N{PARTY POPPER}", chosen="\N{PARTY POPPER}")
    )
    assert chosen_reactions(message) == [PARTY]


def test_a_message_with_no_reaction_block_reports_nothing() -> None:
    assert chosen_reactions(FakeMessage()) == []


def test_reacting_without_keeping_replaces_whatever_was_there() -> None:
    assert final_reactions([PARTY], THUMB, keep_existing=False) == [THUMB]


def test_reacting_and_keeping_adds_to_what_is_there() -> None:
    result = final_reactions([PARTY], THUMB, keep_existing=True)
    assert PARTY in result and THUMB in result and len(result) == 2


def test_reacting_with_the_reaction_already_left_is_refused() -> None:
    """A plan that changes nothing still costs a person the review."""
    with pytest.raises(InvalidInput, match="already"):
        final_reactions([THUMB], THUMB, keep_existing=False)
    with pytest.raises(InvalidInput, match="already"):
        final_reactions([THUMB], THUMB, keep_existing=True)


def test_a_custom_emoji_is_addressed_by_its_document_id() -> None:
    assert requested_reaction(emoji=None, custom_emoji_id="5789012345678901234") == CUSTOM
    assert "5789012345678901234" in describe_reaction(CUSTOM)


def test_an_emoji_is_described_with_its_codepoints() -> None:
    """A look-alike, or one the sanitizer stripped, must not read as the real one."""
    assert "U+1F44D" in describe_reaction(THUMB)


# --- reacting ----------------------------------------------------------------


async def test_a_reaction_plan_names_the_emoji_the_message_and_the_chat(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    client = FakeClient(group(), {412: FakeMessage()})
    ctx = build_ctx(conn, tmp_path, client)

    plan = await plan_react_message(
        ctx, ReactMessageInput(chat=MARKED_GROUP_ID, message_id=412, emoji="\N{THUMBS UP SIGN}")
    )

    assert plan.operation == "message.react"
    assert "\N{THUMBS UP SIGN}" in plan.summary
    assert "412" in plan.summary
    assert "Marketing" in plan.summary
    # The body of the message being reacted to is part of what is approved.
    assert "quarterly numbers are in" in plan.summary
    assert plan.preconditions["message"]["id"] == 412
    assert plan.preconditions["existing"] == []


async def test_a_reaction_can_be_addressed_by_a_message_link(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The number in the link is the whole reason it was pasted."""
    client = FakeClient(group(), {412: FakeMessage()})
    ctx = build_ctx(conn, tmp_path, client)

    plan = await plan_react_message(
        ctx, ReactMessageInput(chat="https://t.me/marketing/412", emoji="\N{THUMBS UP SIGN}")
    )

    assert plan.preconditions["message"]["id"] == 412
    assert "get_entity:marketing" in client.touched


async def test_a_private_supergroup_link_addresses_the_same_message(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """`t.me/c/<internal>/<id>` is the only address a private supergroup has."""
    client = FakeClient(group(), {412: FakeMessage()})
    ctx = build_ctx(conn, tmp_path, client)

    plan = await plan_pin_message(ctx, PinMessageInput(chat="https://t.me/c/1234567890/412"))

    assert plan.preconditions["message"]["id"] == 412
    assert f"get_entity:{MARKED_GROUP_ID}" in client.touched


async def test_a_link_and_a_message_id_that_disagree_are_refused(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    client = FakeClient(group(), {412: FakeMessage()})
    ctx = build_ctx(conn, tmp_path, client)

    with pytest.raises(InvalidInput, match="not both"):
        await plan_react_message(
            ctx,
            ReactMessageInput(
                chat="https://t.me/marketing/412", message_id=999, emoji="\N{THUMBS UP SIGN}"
            ),
        )


async def test_the_review_text_says_which_reaction_is_being_replaced(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    message = FakeMessage(reactions=reacted("\N{PARTY POPPER}", chosen="\N{PARTY POPPER}"))
    client = FakeClient(group(), {412: message})
    ctx = build_ctx(conn, tmp_path, client)

    plan = await plan_react_message(
        ctx, ReactMessageInput(chat=MARKED_GROUP_ID, message_id=412, emoji="\N{THUMBS UP SIGN}")
    )

    assert "replac" in plan.summary.lower()
    assert plan.preconditions["existing"] == [PARTY]


async def test_reacting_twice_with_the_same_emoji_never_becomes_a_plan(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    message = FakeMessage(reactions=reacted("\N{THUMBS UP SIGN}", chosen="\N{THUMBS UP SIGN}"))
    ctx = build_ctx(conn, tmp_path, FakeClient(group(), {412: message}))

    with pytest.raises(InvalidInput, match="already"):
        await plan_react_message(
            ctx, ReactMessageInput(chat=MARKED_GROUP_ID, message_id=412, emoji="\N{THUMBS UP SIGN}")
        )


async def test_a_reaction_needs_exactly_one_of_emoji_and_custom_emoji_id() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ReactMessageInput(chat=1, message_id=1)
    with pytest.raises(ValueError, match="exactly one"):
        ReactMessageInput(chat=1, message_id=1, emoji="\N{THUMBS UP SIGN}", custom_emoji_id="12")


def test_an_emoji_carrying_control_characters_is_refused() -> None:
    """The summary is rendered to a terminal; a `\\r` in it redraws the line."""
    with pytest.raises(ValueError, match="control"):
        ReactMessageInput(chat=1, message_id=1, emoji="\N{THUMBS UP SIGN}\r\x1b[2K")


def test_a_custom_emoji_id_that_is_not_a_document_id_is_refused() -> None:
    with pytest.raises(ValueError):
        ReactMessageInput(chat=1, message_id=1, custom_emoji_id="not-an-id")


# --- removing a reaction -----------------------------------------------------


async def test_removing_a_reaction_this_account_never_left_is_refused(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    message = FakeMessage(reactions=reacted("\N{THUMBS UP SIGN}"))
    ctx = build_ctx(conn, tmp_path, FakeClient(group(), {412: message}))

    with pytest.raises(InvalidInput, match="did not react"):
        await plan_unreact_message(
            ctx,
            UnreactMessageInput(chat=MARKED_GROUP_ID, message_id=412, emoji="\N{THUMBS UP SIGN}"),
        )


async def test_removing_one_reaction_leaves_the_others(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    message = FakeMessage(
        reactions=FakeReactions(
            results=[
                FakeReactionCount(ReactionEmoji("\N{THUMBS UP SIGN}"), 2, chosen_order=0),
                FakeReactionCount(ReactionEmoji("\N{PARTY POPPER}"), 5, chosen_order=1),
            ]
        )
    )
    ctx = build_ctx(conn, tmp_path, FakeClient(group(), {412: message}))

    plan = await plan_unreact_message(
        ctx,
        UnreactMessageInput(chat=MARKED_GROUP_ID, message_id=412, emoji="\N{THUMBS UP SIGN}"),
    )

    assert plan.preconditions["remaining"] == [PARTY]
    assert "\N{THUMBS UP SIGN}" in plan.summary


async def test_removing_every_reaction_is_what_naming_none_means(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    message = FakeMessage(reactions=reacted("\N{THUMBS UP SIGN}", chosen="\N{THUMBS UP SIGN}"))
    ctx = build_ctx(conn, tmp_path, FakeClient(group(), {412: message}))

    plan = await plan_unreact_message(
        ctx, UnreactMessageInput(chat=MARKED_GROUP_ID, message_id=412)
    )

    assert plan.preconditions["remaining"] == []
    assert "every reaction" in plan.summary


async def test_removing_a_reaction_from_a_message_with_none_is_refused(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    ctx = build_ctx(conn, tmp_path, FakeClient(group(), {412: FakeMessage()}))

    with pytest.raises(InvalidInput, match="did not react"):
        await plan_unreact_message(ctx, UnreactMessageInput(chat=MARKED_GROUP_ID, message_id=412))


# --- pinning -----------------------------------------------------------------


async def test_a_pin_plan_says_that_everyone_is_notified(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    ctx = build_ctx(conn, tmp_path, FakeClient(group(), {412: FakeMessage()}))

    plan = await plan_pin_message(ctx, PinMessageInput(chat=MARKED_GROUP_ID, message_id=412))

    assert "notif" in plan.summary.lower()
    assert "quarterly numbers are in" in plan.summary
    assert plan.preconditions["silent"] is False


async def test_a_silent_pin_plan_says_that_nobody_is_notified(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    ctx = build_ctx(conn, tmp_path, FakeClient(group(), {412: FakeMessage()}))

    plan = await plan_pin_message(
        ctx, PinMessageInput(chat=MARKED_GROUP_ID, message_id=412, silent=True)
    )

    assert "no notification" in plan.summary.lower()
    assert plan.preconditions["silent"] is True


async def test_the_pin_preview_says_whose_message_it_is(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Pinning somebody else's message is a different act from pinning your own."""
    ctx = build_ctx(conn, tmp_path, FakeClient(group(), {412: FakeMessage(out=False)}))
    theirs = await plan_pin_message(ctx, PinMessageInput(chat=MARKED_GROUP_ID, message_id=412))
    assert "somebody else" in theirs.summary

    ctx = build_ctx(conn, tmp_path, FakeClient(group(), {413: FakeMessage(id=413, out=True)}))
    mine = await plan_pin_message(ctx, PinMessageInput(chat=MARKED_GROUP_ID, message_id=413))
    assert "this account" in mine.summary


async def test_pinning_what_is_already_pinned_is_refused(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    ctx = build_ctx(conn, tmp_path, FakeClient(group(), {412: FakeMessage(pinned=True)}))

    with pytest.raises(InvalidInput, match="already pinned"):
        await plan_pin_message(ctx, PinMessageInput(chat=MARKED_GROUP_ID, message_id=412))


async def test_pinning_for_both_sides_is_meaningless_outside_a_one_to_one_chat(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    ctx = build_ctx(conn, tmp_path, FakeClient(group(), {412: FakeMessage()}))

    with pytest.raises(InvalidInput, match="one-to-one"):
        await plan_pin_message(
            ctx, PinMessageInput(chat=MARKED_GROUP_ID, message_id=412, both_sides=True)
        )


async def test_a_private_pin_defaults_to_this_side_only_and_says_so(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    ctx = build_ctx(conn, tmp_path, FakeClient(person(), {412: FakeMessage()}))

    mine = await plan_pin_message(ctx, PinMessageInput(chat=USER_ID, message_id=412))
    assert "only on this side" in mine.summary
    assert mine.preconditions["both_sides"] is False

    ctx = build_ctx(conn, tmp_path, FakeClient(person(), {413: FakeMessage(id=413)}))
    shared = await plan_pin_message(
        ctx, PinMessageInput(chat=USER_ID, message_id=413, both_sides=True)
    )
    assert "for both sides" in shared.summary


# --- unpinning ---------------------------------------------------------------


async def test_unpinning_says_whose_pin_is_being_removed(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    ctx = build_ctx(conn, tmp_path, FakeClient(group(), {412: FakeMessage(pinned=True)}))

    plan = await plan_unpin_message(ctx, UnpinMessageInput(chat=MARKED_GROUP_ID, message_id=412))

    assert plan.operation == "message.unpin"
    assert "somebody else" in plan.summary
    assert "quarterly numbers are in" in plan.summary


async def test_unpinning_what_is_not_pinned_is_refused(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    ctx = build_ctx(conn, tmp_path, FakeClient(group(), {412: FakeMessage(pinned=False)}))

    with pytest.raises(InvalidInput, match="not pinned"):
        await plan_unpin_message(ctx, UnpinMessageInput(chat=MARKED_GROUP_ID, message_id=412))


# --- the policy, before anything is resolved ---------------------------------


@pytest.mark.parametrize(
    ("planner", "params"),
    [
        (plan_react_message, {"emoji": "\N{THUMBS UP SIGN}"}),
        (plan_unreact_message, {"emoji": "\N{THUMBS UP SIGN}"}),
        (plan_pin_message, {}),
        (plan_unpin_message, {}),
    ],
    ids=["react", "unreact", "pin", "unpin"],
)
async def test_the_readonly_profile_refuses_before_touching_telegram(
    conn: sqlite3.Connection, tmp_path: Path, planner: Any, params: dict[str, Any]
) -> None:
    """Resolving a username is itself an observable act."""
    client = FakeClient(group(), {412: FakeMessage(pinned=True)})
    ctx = build_ctx(conn, tmp_path, client, settings_for(profile="readonly"))

    op = {
        plan_react_message: ReactMessageInput,
        plan_unreact_message: UnreactMessageInput,
        plan_pin_message: PinMessageInput,
        plan_unpin_message: UnpinMessageInput,
    }[planner]

    with pytest.raises(ProfileForbidden):
        await planner(ctx, op(chat=MARKED_GROUP_ID, message_id=412, **params))

    assert client.touched == [], "a refused plan still went looking for the chat"


async def test_a_chat_outside_the_allow_list_is_refused(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    ctx = build_ctx(
        conn, tmp_path, FakeClient(group(), {412: FakeMessage()}), settings_for(allow=[])
    )

    with pytest.raises(NotAllowlisted):
        await plan_react_message(
            ctx, ReactMessageInput(chat=MARKED_GROUP_ID, message_id=412, emoji="\N{THUMBS UP SIGN}")
        )


# --- the applier, re-checking what the reviewer saw --------------------------
#
# `messages.sendReaction` takes the account's whole list for a message, not a
# delta. So a plan reviewed against one set of reactions and applied against
# another would send a list nobody looked at — silently dropping a reaction, or
# re-adding one that was taken off from a phone in the meantime.


async def _plan_and_params(ctx: OperationContext, params: Any, planner: Any) -> tuple[Any, Any]:
    plan = await planner(ctx, params)
    op = REGISTRY.by_name(plan.operation)
    return ctx.plans.get(plan.plan_id), op.parse(plan.params)


async def test_the_applier_refuses_a_reaction_plan_whose_ground_moved(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    message = FakeMessage(reactions=reacted("\N{PARTY POPPER}", chosen="\N{PARTY POPPER}"))
    client = FakeClient(group(), {412: message})
    ctx = build_ctx(conn, tmp_path, client)
    plan, params = await _plan_and_params(
        ctx,
        ReactMessageInput(
            chat=MARKED_GROUP_ID,
            message_id=412,
            emoji="\N{THUMBS UP SIGN}",
            keep_existing=True,
        ),
        plan_react_message,
    )

    # The same message, but this account's 🎉 was taken off from another device.
    message.reactions = reacted("\N{PARTY POPPER}")

    with pytest.raises(PlanPreconditionFailed, match="have changed"):
        await _verify_mark(ctx, client, plan, params, [])


async def test_the_applier_sends_the_whole_list_the_reviewer_approved(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    message = FakeMessage(reactions=reacted("\N{PARTY POPPER}", chosen="\N{PARTY POPPER}"))
    client = FakeClient(group(), {412: message})
    ctx = build_ctx(conn, tmp_path, client)
    plan, params = await _plan_and_params(
        ctx,
        ReactMessageInput(
            chat=MARKED_GROUP_ID,
            message_id=412,
            emoji="\N{THUMBS UP SIGN}",
            keep_existing=True,
        ),
        plan_react_message,
    )

    prepared = await _verify_mark(ctx, client, plan, params, [])

    assert prepared.reactions == [PARTY, THUMB]
    assert prepared.limit_target == str(MARKED_GROUP_ID)


async def test_the_applier_refuses_a_pin_that_somebody_else_already_made(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    message = FakeMessage()
    client = FakeClient(group(), {412: message})
    ctx = build_ctx(conn, tmp_path, client)
    plan, params = await _plan_and_params(
        ctx, PinMessageInput(chat=MARKED_GROUP_ID, message_id=412), plan_pin_message
    )

    message.pinned = True

    with pytest.raises(PlanPreconditionFailed, match="already pinned"):
        await _verify_mark(ctx, client, plan, params, [])


def test_a_reaction_becomes_the_telethon_object_it_names() -> None:
    """A 64-bit document id must survive the round trip as an integer, exactly."""
    assert _reaction_object(THUMB) == tl.ReactionEmoji(emoticon="\N{THUMBS UP SIGN}")
    assert _reaction_object(CUSTOM) == tl.ReactionCustomEmoji(document_id=5789012345678901234)


# --- reaction types this tool cannot send back ------------------------------


PAID = {"kind": "paid", "emoji": None, "custom_emoji_id": None}


def test_a_reaction_with_no_emoji_is_named_by_its_kind() -> None:
    """A star reaction rendered as a blank reads as nothing at all."""
    assert "paid" in describe_reaction(PAID)


def test_keeping_a_paid_reaction_would_mean_re_sending_it_so_it_is_refused() -> None:
    """The call replaces the whole list, and re-sending a star reaction is a purchase."""
    with pytest.raises(InvalidInput, match="replaces the whole list"):
        final_reactions([PAID], THUMB, keep_existing=True)


def test_replacing_a_paid_reaction_is_allowed_because_nothing_is_re_sent() -> None:
    assert final_reactions([PAID], THUMB, keep_existing=False) == [THUMB]


def test_removing_one_reaction_while_a_paid_one_stays_is_refused() -> None:
    with pytest.raises(InvalidInput, match="replaces the whole list"):
        remaining_reactions([PAID, THUMB], THUMB)


def test_removing_every_reaction_never_re_sends_anything() -> None:
    assert remaining_reactions([PAID, THUMB], None) == []


# --- order, bounds and attachments ------------------------------------------


def test_the_account_s_own_reaction_order_is_telegram_s_not_this_tool_s() -> None:
    """`chosen_order` is derived from the position a reaction was sent in.

    The two emoji here sort the other way round by codepoint, so a list that
    came back in the right order and a list that was re-sorted are different
    lists — which is the only way to tell them apart.
    """
    message = FakeMessage(
        reactions=FakeReactions(
            results=[
                FakeReactionCount(ReactionEmoji("\N{PARTY POPPER}"), 9, chosen_order=1),
                FakeReactionCount(ReactionEmoji("\N{THUMBS UP SIGN}"), 2, chosen_order=0),
            ]
        )
    )
    assert chosen_reactions(message) == [THUMB, PARTY]


def test_a_kept_reaction_stays_first_and_the_new_one_goes_last() -> None:
    assert final_reactions([PARTY], THUMB, keep_existing=True) == [PARTY, THUMB]


def test_drift_is_about_which_reactions_not_their_order() -> None:
    """Telegram re-orders `results` as counts move; that is not the world moving."""
    assert same_reactions([PARTY, THUMB], [THUMB, PARTY]) is True
    assert same_reactions([PARTY], [THUMB]) is False


def test_a_document_id_too_large_for_telegram_is_refused_on_the_way_in() -> None:
    """`struct.pack('<q', …)` fails after the slot and the audit record exist."""
    with pytest.raises(ValueError, match="signed 64-bit"):
        ReactMessageInput(chat=1, message_id=1, custom_emoji_id=str(2**63))
    assert ReactMessageInput(chat=1, message_id=1, custom_emoji_id=str(2**63 - 1))


def test_a_message_id_past_telegram_s_range_is_refused() -> None:
    with pytest.raises(ValueError):
        ReactMessageInput(chat=1, message_id=2**31, emoji="\N{THUMBS UP SIGN}")


@dataclass
class FakePhoto:
    id: int = 90001


@dataclass
class FakeMediaPhoto:
    photo: FakePhoto = field(default_factory=FakePhoto)


async def test_a_swapped_attachment_is_caught_even_though_the_caption_never_changed(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A caption-less photo has an empty body, so the body digest cannot see this."""
    message = FakeMessage(message="", media=FakeMediaPhoto())
    client = FakeClient(group(), {412: message})
    ctx = build_ctx(conn, tmp_path, client)
    plan, params = await _plan_and_params(
        ctx, PinMessageInput(chat=MARKED_GROUP_ID, message_id=412), plan_pin_message
    )
    # In the shared message snapshot, so that every operation naming a message
    # gets it — not in a key only the four marks write.
    assert plan.preconditions["message"]["media"] == {
        "type": "fakemediaphoto",
        "id": "90001",
        "parts": {"photo": ["90001"]},
    }
    # The reviewer was told which attachment it is, not merely that there is one.
    assert "90001" in plan.summary

    message.media = FakeMediaPhoto(photo=FakePhoto(id=90002))

    with pytest.raises(PlanPreconditionFailed, match="attachment"):
        await _verify_mark(ctx, client, plan, params, [])
