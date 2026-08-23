"""Sending later — the one plan whose approval outlives this tool.

Every other write in this project is a moment: a person approves it, it
happens, and whether it was the right call is now history. A scheduled send is
different, and the difference is the reason it exists. Once the plan is
applied, the message sits in **Telegram's own scheduled queue**, where it shows
up in the app on the person's phone with a cancel button next to it. They can
change their mind an hour later, from a device that has never heard of this
tool, with no agent running and no terminal open. That is a stronger form of
control than a plan ever gives, and it is worth an operation of its own.

Three decisions carry the module.

**A time without an offset is refused, never guessed.** "09:00" is not a time;
it is a time in a timezone nobody named. Reading it as the host's local zone
would make the summary say one hour and Telegram act on another, and a plan
summary that cannot be checked is not an approval surface. So the field is a
Pydantic ``AwareDatetime``: a naive value fails validation with a message
saying exactly that, and the summary prints both the offset the caller gave and
the UTC equivalent, so a reviewer in a third timezone can still tell what will
happen.

**"When they come online" is Telegram's own mode, and it reuses Telegram's own
sentinel.** ``scheduled.list`` already knows 0x7FFFFFFE means "as soon as they
appear" rather than January 2038; that constant is imported from there rather
than copied, because two copies are how a reader and a writer end up
disagreeing about which of them is a date.

**The deadline is re-checked at apply time, and a missed one is a refusal.**
Telegram sends a scheduled message whose date has passed immediately. So a plan
reviewed for nine o'clock and applied at five past would fire *now* — the one
outcome nobody approved. ``recheck_schedule`` refuses instead, and the person
re-plans against a time that still exists.

Registered here rather than in ``write.py`` for the same reason ``pending.py``
is not part of ``messages.py``: it is a coherent piece with its own vocabulary.
The registry, not any one module, is the list of what exists — which is why the
test suite derives "every remote write has a budget" from ``REGISTRY``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from ..errors import InvalidInput, PlanPreconditionFailed
from ..opspec import REGISTRY, Effect, Operation
from ..plans import Plan
from ..render import humanize_duration, quote_for_review
from ..safety import Capability
from ._common import require_peer
from .pending import WHEN_ONLINE
from .write import (
    MAX_MESSAGE_CHARS,
    WriteInput,
    describe,
    open_writer,
    peer_snapshot,
    require_planning_profile,
    resolve_peer,
    text_digest,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import OperationContext

ACTION = "message.schedule"

#: Telegram refuses a schedule further out than about a year — and it refuses
#: it at *send* time, long after somebody approved the plan. Rejecting it here
#: keeps a doomed plan out of the review queue instead of turning it into a
#: failure nobody can act on.
MAX_SCHEDULE_AHEAD = timedelta(days=365)

#: How close to the send time a plan may still be applied, and why it is minutes
#: rather than seconds. Telegram treats a schedule that is nearly due as "send
#: now", and the request does not reach it instantly: the applier reserves a
#: rate-limit slot, writes an audit record and then allows the RPC up to
#: ``apply.RPC_TIMEOUT_SECONDS`` (60s). A margin smaller than that budget means
#: a plan approved as "later" could arrive as "now" — which is the one thing the
#: review step exists to keep apart. Two minutes covers the whole path with
#: slack, at the cost of not being able to schedule something a minute out.
MIN_SCHEDULE_LEAD = timedelta(seconds=120)


class ScheduleMessageInput(WriteInput):
    chat: int | str = Field(description="Chat id, @username, or phone-free user id.")
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    at: AwareDatetime | None = Field(
        default=None,
        description=(
            "When to send it: ISO-8601 with an explicit UTC offset, e.g. "
            "2026-09-01T09:00:00+07:00. A time without an offset is refused rather than "
            "guessed; an epoch second is accepted too, being unambiguous by construction. "
            "At least two minutes and at most a year ahead; seconds are the resolution, "
            "and anything finer is dropped."
        ),
    )
    when_online: bool = Field(
        default=False,
        description=(
            "Send as soon as the other person is next online — Telegram's own mode, "
            "one-to-one chats only. There is no fixed time, and if they are already "
            "online it goes out at once rather than waiting in the queue."
        ),
    )
    silent: bool = Field(default=False, description="Deliver without a notification sound.")
    link_preview: bool = True

    @model_validator(mode="after")
    def _to_whole_seconds(self) -> ScheduleMessageInput:
        """Drop anything finer than a second, because the wire has nothing finer.

        ``schedule_date`` is an integer. Keeping the microseconds would print a
        time in the summary that is not the time Telegram is given — a small lie
        on the one surface that exists to be exact.
        """
        if self.at is not None and self.at.microsecond:
            object.__setattr__(self, "at", self.at.replace(microsecond=0))
        return self

    @model_validator(mode="after")
    def _exactly_one_way_of_saying_when(self) -> ScheduleMessageInput:
        # `when_online` and "an exact time" are the two modes Telegram has, and
        # they are mutually exclusive on the wire: the sentinel *is* the date
        # field. Accepting both would silently drop one of them.
        if self.when_online == (self.at is not None):
            raise ValueError(
                "say when exactly once: either 'at' (a time with a UTC offset) "
                "or 'when_online', not both and not neither"
            )
        return self


def schedule_date(params: ScheduleMessageInput) -> int:
    """The single integer Telegram is told, as a plain epoch second.

    One function, used by the planner, by the preconditions and by the applier,
    so those three cannot disagree about what was scheduled.
    """
    if params.when_online:
        return WHEN_ONLINE
    at = cast(datetime, params.at)
    return int(at.timestamp())


def describe_when(params: ScheduleMessageInput, *, now: datetime) -> str:
    """The line a reviewer checks the clock against.

    Both forms are printed. The offset the caller gave is what they meant; the
    UTC form is what everybody else can compare against; the relative distance
    is what catches a year or a date typed wrong, which no amount of formatting
    of an absolute timestamp ever does.
    """
    if params.when_online:
        return (
            "as soon as the other person is next online — Telegram picks the moment, so "
            "there is no fixed time, and if they are online already it goes out at once"
        )
    at = cast(datetime, params.at)
    # "about", because the relative form truncates: the two absolute forms next
    # to it are the exact ones, and this is only here to catch a wrong date.
    return (
        f"{at.isoformat()} "
        f"({at.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}, "
        f"in about {humanize_duration((at - now).total_seconds())})"
    )


def require_a_reachable_time(params: ScheduleMessageInput, *, now: datetime) -> None:
    """Refuse a time Telegram would not accept, before anything is recorded."""
    if params.when_online:
        return
    at = cast(datetime, params.at)
    if at <= now + MIN_SCHEDULE_LEAD:
        raise InvalidInput(
            f"{at.isoformat()} is in the past or too close to now; a scheduled send has "
            f"to be at least {int(MIN_SCHEDULE_LEAD.total_seconds())}s away",
            suggestion="Send it now with message.send, or pick a later time.",
        )
    if at > now + MAX_SCHEDULE_AHEAD:
        raise InvalidInput(
            f"{at.isoformat()} is more than {MAX_SCHEDULE_AHEAD.days} days away; "
            "Telegram will not hold a scheduled message that long",
            suggestion="Pick a time within a year.",
        )


def recheck_schedule(
    preconditions: dict[str, Any],
    params: ScheduleMessageInput,
    *,
    now: datetime | None = None,
) -> None:
    """Everything apply must confirm about *what* and *when*, before it sends.

    Two different failures, deliberately reported differently. A mismatch
    against the recorded parameters means they are no longer the ones a person
    read — plan bodies are encrypted only when ``plans.encrypt_bodies`` is on,
    while the preconditions are always in the clear, so comparing the two is
    what makes an altered blob fail closed. A deadline that has simply passed is
    not tampering at all; it is a plan that waited too long, and the answer is
    to re-plan it.

    Everything the send is made of is covered, not only the time: the body's
    digest, and the two flags that decide how it arrives. A reviewer approved a
    message that would be silent, or that would show a link preview, and a plan
    that changed either of those between review and apply is not the one they
    read (raised by review).
    """
    now = now or datetime.now(UTC)

    recorded = (
        preconditions.get("schedule_date"),
        preconditions.get("body_sha256"),
        preconditions.get("silent"),
        preconditions.get("link_preview"),
    )
    applying = (
        schedule_date(params),
        text_digest(params.text),
        params.silent,
        params.link_preview,
    )
    if recorded != applying:
        raise PlanPreconditionFailed(
            "the message being applied is not the one recorded when the plan was reviewed",
            suggestion="Reject this plan and create a new one.",
        )

    if params.when_online:
        # No deadline to miss: Telegram holds it until the other side appears.
        return

    at = cast(datetime, params.at)
    if at <= now + MIN_SCHEDULE_LEAD:
        raise PlanPreconditionFailed(
            f"the time this was scheduled for ({at.isoformat()}) has passed; applying it now "
            "would send the message immediately rather than at the reviewed time",
            suggestion="Reject this plan and schedule it again for a time that is still ahead.",
        )


async def plan_schedule_message(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(ScheduleMessageInput, params)
    # Before the network: under `readonly` even resolving the chat would tell
    # Telegram which conversation somebody is interested in.
    require_planning_profile(ctx, Capability.SEND, action=ACTION)
    now = datetime.now(UTC)
    require_a_reachable_time(p, now=now)

    async with open_writer(ctx, p.account) as (label, client):
        target = await resolve_peer(client, p.chat)
    require_peer(ctx, Capability.SEND, target.ref, action=ACTION)

    if p.when_online and not target.ref.is_private:
        # A group has no online state. Telegram would either refuse the send or
        # hold it for ever, and a plan that can never fire is worse than a
        # refusal because it looks done.
        raise InvalidInput(
            "'when_online' needs a one-to-one chat: a group or channel has no online state",
            suggestion="Give an exact time in 'at' instead.",
        )

    # The queue is the point of the operation, but only the timed mode is
    # guaranteed to reach it: "when they are next online" can go out at once,
    # and a summary promising a cancel button that will not be there is worse
    # than no promise at all (raised by review).
    queue = (
        "  It waits in Telegram's own scheduled queue until then: visible in the app, "
        "and cancellable there without this tool.\n"
        if not p.when_online
        else "  If they are offline it waits in Telegram's own scheduled queue, visible and "
        "cancellable in the app; if they are already online there is nothing to cancel.\n"
    )
    delivery = "silently" if p.silent else "with a notification"
    preview = "with" if p.link_preview else "without"
    summary = (
        f"Schedule a message as {label} to {describe(target)}\n"
        f"  send: {describe_when(p, now=now)}\n"
        f"{queue}"
        f"  It will arrive {delivery}, {preview} a link preview.\n"
        f"--- message ({len(p.text)} chars) ---\n"
        f"{quote_for_review(p.text)}"
    )
    return ctx.plans.create(
        operation=ACTION,
        account=label,
        params=p.model_dump(mode="json"),
        preconditions={
            "peer": peer_snapshot(target),
            "schedule_date": schedule_date(p),
            "send_when_online": bool(p.when_online),
            "body_sha256": text_digest(p.text),
            "body_len": len(p.text),
            # Both flags are part of what was approved: "silently" and "with a
            # preview" are in the summary, so they are in the comparison too.
            "silent": p.silent,
            "link_preview": p.link_preview,
        },
        summary=summary,
    )


SCHEDULE_MESSAGE = REGISTRY.register(
    Operation(
        name=ACTION,
        cli=("message", "schedule"),
        plan_tool="telegram_plan_schedule_message",
        summary="Plan a message to be sent at a given time, or when the recipient is next online.",
        description=(
            "Records the intent to schedule. Nothing is queued until a person applies the "
            "plan; after that the message waits in Telegram's own scheduled queue, where it "
            "is visible in the app and can be cancelled there without this tool. Give 'at' "
            "as ISO-8601 with an explicit UTC offset — a time without one is refused rather "
            "than guessed — at least two minutes and at most a year ahead. The alternative "
            "is 'when_online' for a one-to-one chat, which goes out the moment they appear "
            "and immediately if they are online already."
        ),
        input_model=ScheduleMessageInput,
        effect=Effect.REMOTE_WRITE,
        mcp_tool=None,
        capability=Capability.SEND,
        planner=plan_schedule_message,
        tags=("write", "plan"),
    )
)
