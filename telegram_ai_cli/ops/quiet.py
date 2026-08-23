"""Archiving and muting — the two remote writes nobody else can see.

Every other write in this project reaches somebody: a message arrives, a read
receipt appears, a member is added or removed. These two do not. Archiving
moves a chat between two lists *in this account's own client*; muting stops
notifications *on this account's own devices*. The other side is not blocked,
not left, not banned, receives everything it received before, and has no way to
tell that either happened.

That near-zero blast radius is exactly why the summaries below are wordier than
the actions deserve. The risk here is not the effect — it is a person reading
"mute" as something done *to* the other party, or filing it next to a ban.
Both are one word away in a review queue, and only the summary can tell them
apart, so both summaries say in plain words who can see the change and which
things it is not.

Two smaller decisions worth stating.

**A mute duration is stored, and the deadline is computed when the plan is
applied.** Telegram's field is an absolute ``mute_until``. Recording that
absolute second at planning time would mean a plan approved in the evening and
applied next morning muted the chat for the few minutes that were left of it,
rather than for the eight hours somebody read. So the plan carries the
*duration*, and ``mute_until`` turns it into a deadline against the clock of
the moment it actually happens.

**"For ever" is a sentinel, not a date.** Telegram's clients write
0x7FFFFFFF — the signed 32-bit maximum — for an indefinite mute, so a summary
that rendered it would announce a mute "until 19 January 2038". The summary
says "until it is unmuted" instead, because that is what it means.

Both operations are gated by ``Capability.SEND``: it is the rule that already
governs acting on a chat, and inventing a capability for a change only the
owner can observe would add a policy surface without adding a decision anybody
wants to take separately. The cost is noted in ``TASKS.md``: a chat you may
read but never write to cannot be muted through this tool.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, Field, model_validator

from ..errors import PlanPreconditionFailed
from ..opspec import REGISTRY, Effect, Operation
from ..plans import Plan
from ..render import humanize_duration
from ..safety import Capability
from ._common import require_peer
from .write import (
    WriteInput,
    describe,
    open_writer,
    peer_snapshot,
    require_planning_profile,
    resolve_peer,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import OperationContext

ARCHIVE_ACTION = "chat.archive"
MUTE_ACTION = "chat.mute"

#: Telegram's two folder ids. There is no enum for them on the wire: the
#: archive is folder 1 and the ordinary chat list is folder 0.
ARCHIVE_FOLDER_ID = 1
MAIN_FOLDER_ID = 0

#: What the official clients write for "muted until I say otherwise". Rendered
#: as a date it reads as January 2038, which is why it never is.
MUTE_FOREVER = 0x7FFFFFFF

#: A mute shorter than a minute is a mute nobody would notice happening, and a
#: mute longer than a year is indistinguishable from the indefinite one, which
#: already has its own spelling (omit the duration).
MIN_MUTE_SECONDS = 60
MAX_MUTE_SECONDS = 365 * 24 * 3600

#: The sentence that separates both of these from the operations they sit next
#: to in a review queue. Written once, so the two summaries cannot drift into
#: saying different things about the same property.
_INVISIBLE_TO_OTHERS = (
    "It is not a ban, a block or a leave: the other side is unaffected, keeps writing, "
    "and cannot tell."
)


class ArchiveChatInput(WriteInput):
    chat: int | str = Field(description="Chat id, @username, or t.me link.")
    archived: bool = Field(
        default=True,
        description="False moves the chat back out of Archived into the main list.",
    )


class MuteChatInput(WriteInput):
    chat: int | str = Field(description="Chat id, @username, or t.me link.")
    muted: bool = Field(default=True, description="False turns notifications back on.")
    duration_seconds: int | None = Field(
        default=None,
        ge=MIN_MUTE_SECONDS,
        le=MAX_MUTE_SECONDS,
        description=(
            "How long to stay muted, in seconds. Omit it to mute until somebody unmutes "
            "the chat by hand. Counted from the moment the plan is applied."
        ),
    )

    @model_validator(mode="after")
    def _a_duration_needs_a_mute(self) -> MuteChatInput:
        if self.duration_seconds is not None and not self.muted:
            raise ValueError(
                "duration_seconds only makes sense with muted=true; unmuting takes effect "
                "at once and has no length"
            )
        return self


def folder_id_for(params: ArchiveChatInput) -> int:
    """Which of Telegram's two folders the chat is being moved into."""
    return ARCHIVE_FOLDER_ID if params.archived else MAIN_FOLDER_ID


def mute_until(params: MuteChatInput, *, now: float | None = None) -> int:
    """The absolute second Telegram is told, computed against *this* clock.

    ``now`` is an argument rather than a call to ``time.time()`` inside so that
    the applier passes the moment it is actually applying, and so the
    arithmetic is testable without waiting for it.
    """
    if not params.muted:
        # Zero is how Telegram spells "not muted"; it is a real value, not an
        # absent one, so it has to be sent rather than omitted.
        return 0
    if params.duration_seconds is None:
        return MUTE_FOREVER
    return int(now if now is not None else time.time()) + params.duration_seconds


def describe_mute(params: MuteChatInput) -> str:
    """The middle of the sentence a reviewer approves."""
    if not params.muted:
        return "Unmute"
    if params.duration_seconds is None:
        return "Mute indefinitely"
    # Every unit, not the usual two: this number *is* the decision, and the
    # two-unit form truncates — 25h59m would read as "1 day 1 hour".
    return f"Mute for {humanize_duration(params.duration_seconds, max_units=4)}"


def recheck_archive(preconditions: dict[str, Any], params: ArchiveChatInput) -> None:
    """Refuse if the direction being applied is not the reviewed one.

    The parameters are encrypted at rest and the preconditions are not, so this
    comparison is what makes an altered params blob fail closed rather than be
    applied as though a person had read it.
    """
    if preconditions.get("archived") != params.archived:
        raise PlanPreconditionFailed(
            "the archive direction being applied is not the one recorded when the plan "
            "was reviewed",
            suggestion="Reject this plan and create a new one.",
        )


def recheck_mute(preconditions: dict[str, Any], params: MuteChatInput) -> None:
    """Refuse if either half of the reviewed mute has changed.

    Both halves matter: a mute silently widened from eight hours to a year is
    the same class of mistake as one that was never approved at all.
    """
    if (
        preconditions.get("muted") != params.muted
        or preconditions.get("duration_seconds") != params.duration_seconds
    ):
        raise PlanPreconditionFailed(
            "the mute being applied is not the one recorded when the plan was reviewed",
            suggestion="Reject this plan and create a new one.",
        )


async def plan_archive_chat(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(ArchiveChatInput, params)
    require_planning_profile(ctx, Capability.SEND, action=ARCHIVE_ACTION)
    async with open_writer(ctx, p.account) as (label, client):
        target = await resolve_peer(client, p.chat)
    require_peer(ctx, Capability.SEND, target.ref, action=ARCHIVE_ACTION)

    movement = (
        "out of the main chat list into Archived"
        if p.archived
        else "back out of Archived into the main chat list"
    )
    summary = (
        f"Move {describe(target)} {movement}, as {label}.\n"
        f"  Only you see this: it changes this account's own chat list and nothing else. "
        f"{_INVISIBLE_TO_OTHERS}"
    )
    return ctx.plans.create(
        operation=ARCHIVE_ACTION,
        account=label,
        params=p.model_dump(mode="json"),
        preconditions={"peer": peer_snapshot(target), "archived": p.archived},
        summary=summary,
    )


async def plan_mute_chat(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(MuteChatInput, params)
    require_planning_profile(ctx, Capability.SEND, action=MUTE_ACTION)
    async with open_writer(ctx, p.account) as (label, client):
        target = await resolve_peer(client, p.chat)
    require_peer(ctx, Capability.SEND, target.ref, action=MUTE_ACTION)

    if not p.muted:
        tail = "notifications from it resume on this account's devices"
    elif p.duration_seconds is None:
        tail = "notifications stay off until the chat is unmuted by hand"
    else:
        tail = "notifications stay off for that long, counted from the moment this plan is applied"
    summary = (
        f"{describe_mute(p)} {describe(target)}, as {label} — {tail}.\n"
        f"  Only you see this: muting changes notifications on this account's devices and "
        f"nothing else. {_INVISIBLE_TO_OTHERS}"
    )
    return ctx.plans.create(
        operation=MUTE_ACTION,
        account=label,
        params=p.model_dump(mode="json"),
        preconditions={
            "peer": peer_snapshot(target),
            "muted": p.muted,
            # The duration, not the deadline: see the module docstring.
            "duration_seconds": p.duration_seconds,
        },
        summary=summary,
    )


ARCHIVE_CHAT = REGISTRY.register(
    Operation(
        name=ARCHIVE_ACTION,
        cli=("chat", "archive"),
        plan_tool="telegram_plan_archive_chat",
        summary="Plan moving a chat into the Archived list, or back out of it.",
        description=(
            "Changes this account's own chat list and nothing else: the other side is not "
            "affected, still receives everything, and cannot tell. Not a ban, a block or a "
            "leave. Pass archived=false to move the chat back into the main list."
        ),
        input_model=ArchiveChatInput,
        effect=Effect.REMOTE_WRITE,
        mcp_tool=None,
        capability=Capability.SEND,
        planner=plan_archive_chat,
        tags=("write", "plan"),
    )
)

MUTE_CHAT = REGISTRY.register(
    Operation(
        name=MUTE_ACTION,
        cli=("chat", "mute"),
        plan_tool="telegram_plan_mute_chat",
        summary="Plan muting a chat for a while or indefinitely, or unmuting it.",
        description=(
            "Silences notifications on this account's own devices. Messages keep arriving "
            "and the other side cannot tell — this is not a ban, a block or a leave. Give "
            "duration_seconds for a bounded mute (counted from when the plan is applied), "
            "omit it to mute until somebody unmutes by hand, or pass muted=false to turn "
            "notifications back on."
        ),
        input_model=MuteChatInput,
        effect=Effect.REMOTE_WRITE,
        mcp_tool=None,
        capability=Capability.SEND,
        planner=plan_mute_chat,
        tags=("write", "plan"),
    )
)
