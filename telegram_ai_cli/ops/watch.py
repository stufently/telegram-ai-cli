"""``telegram_watch`` — wait for the next message instead of asking again.

Polling `telegram_inbox` is how an agent currently learns that something
arrived, and it is expensive in the one resource that matters: every poll is a
turn, and every turn pays for the system prompt again whether or not anything
happened. Worse, the answer is stale by construction — the interval between
polls is the latency floor.

This operation waits instead. It registers a Telethon update handler, blocks
until something a permitted chat produces, and hands the burst back in one
answer.

Three decisions shape it, and each one is a rule rather than a tuning knob.

**A burst is one answer.** Somebody typing four short replies is a single event
as far as an agent is concerned; waking four times to read one thought is the
polling cost re-created inside the operation that was supposed to remove it. So
the first message opens a debounce window, every further message restarts it,
and the result comes back when the chat goes quiet. The window is a parameter;
that it exists is not.

**The wait always ends.** `timeout_sec` is capped in the schema at
:data:`MAX_WATCH_SECONDS`, so no argument produces an unbounded call. An MCP
client has no way to abandon a tool call it is waiting on, so an operation that
could block forever is a hung session, not a slow one. The ceiling is absolute
and measured from the top of the handler: connecting the account, resolving
named chats and resolving an event's chat are all network round trips, and a
ceiling that started at the first read would bound the *waiting* rather than
the call. Returning empty at it is a *result* — `stopped_because: "timeout"`
with `waited_sec` — and not an error: "nothing happened for a minute" is
exactly what the caller asked about.

**A refused chat leaves no trace.** The policy filter runs *before* the
debounce logic, so a message from a peer the configuration does not permit does
not start a burst, does not extend one, and does not turn a silent minute into
a "something happened" answer. Every other read reports how many rows it
withheld; this one deliberately does not, because a count of events in chats
the caller may not read is itself the leak — it says a specific conversation
was active at a specific second.

⚠️ **This holds the account's session lock for the whole wait.** One auth key
allows one connection (`accounts/lock.py`), so nothing else can use the same
account until the wait returns — which is why the ceiling is five minutes and
not an hour. See `docs/operations.md`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal

from pydantic import Field, field_validator

from ..context import OperationContext
from ..envelope import Envelope
from ..opspec import REGISTRY, Effect, Operation
from ..safety import Capability, PeerRef, SafetyKernel
from ._client import open_account
from ._common import (
    MAX_PAGE,
    ReadInput,
    guard_hard_denied,
    hard_denied,
    require_enumeration,
    require_peer,
    telegram_errors,
    telegram_result,
)
from ._serialize import message_summary, peer_ref, peer_summary
from .chats import resolve_chat

#: The hard ceiling on a single wait, published in the schema. Five minutes is
#: long enough to be worth the call and short enough that the session lock this
#: holds does not read as a hung account to whoever tries the CLI meanwhile.
MAX_WATCH_SECONDS = 300.0
DEFAULT_WATCH_SECONDS = 60.0

#: How long the chat must stay quiet before the burst is considered finished.
MAX_DEBOUNCE_SECONDS = 30.0
DEFAULT_DEBOUNCE_SECONDS = 2.0

#: How many chats may be named. A longer list is a dialog sweep wearing a
#: filter, and every entry costs a `get_entity` round trip before the wait
#: even starts.
MAX_WATCHED_CHATS = 20

#: How many undelivered updates the subscription will hold. Nothing here can
#: slow a flood down, so the alternative to a bound is holding every message a
#: hostile chat cares to send; overflow drops the newest rather than evicting a
#: burst that is already being collected.
QUEUE_MAXSIZE = 1000

#: Why the wait ended. Published in the payload so a caller can tell "the chat
#: went quiet" from "I ran out of time", which look identical in a message list.
StopReason = Literal["quiet", "timeout", "limit"]

#: A source: ``await source(window)`` returns the next item, or ``None`` when
#: ``window`` seconds pass without one.
Source = Callable[[float], Awaitable[Any | None]]

#: A screen: returns the value to report, or ``None`` to drop the item — a
#: dropped item does not start a burst, does not extend one, and leaves no
#: trace in the result.
Screen = Callable[[Any], Awaitable[Any | None]]


class WatchInput(ReadInput):
    chats: list[str] | None = Field(
        default=None,
        max_length=MAX_WATCHED_CHATS,
        description=(
            "Chat ids, @usernames or t.me links to watch. Omit to watch every chat "
            "the policy already permits reading. Accepts a comma-separated string."
        ),
    )
    timeout_sec: float = Field(
        default=DEFAULT_WATCH_SECONDS,
        gt=0,
        le=MAX_WATCH_SECONDS,
        description=(
            "Longest this call may block. Returning with no events at the end of it "
            "is a result, not an error."
        ),
    )
    debounce_sec: float = Field(
        default=DEFAULT_DEBOUNCE_SECONDS,
        ge=0,
        le=MAX_DEBOUNCE_SECONDS,
        description=(
            "How long the chat must stay quiet before the burst is handed back. "
            "Each new message restarts the window; 0 returns the first message alone."
        ),
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=MAX_PAGE,
        description="Most messages to collect before returning even if the chat is still busy.",
    )

    @field_validator("chats", mode="before")
    @classmethod
    def _accept_a_comma_separated_string(cls, value: Any) -> Any:
        """The CLI generator has no repeated-option form for a list field.

        `cli.py` maps `list[str]` to a single string option (a known gap in
        `TASKS.md`), so without this the operation would be reachable over MCP
        and unusable from a terminal. Splitting here keeps both surfaces on the
        same model instead of teaching one of them a special case.
        """
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",") if part.strip()]
            return parts or None
        return value


# --- the core, with time and events injected --------------------------------


def admits(safety: SafetyKernel, peer: PeerRef, *, allowed: set[int] | None) -> bool:
    """Whether an event from ``peer`` may be reported at all.

    Three gates in the order the rest of the project uses. ``allowed`` is the
    caller's own narrowing — an intersection with the policy, never a
    substitute for it, so naming a chat that the configuration refuses does not
    open it. The kernel remaps a private peer onto ``read_dm`` itself, which is
    what stops a watch from becoming the cheap way into a DM that
    ``chat read`` would refuse.

    Returns a boolean rather than raising: this runs per event on a stream, and
    a refusal here is not a caller error worth failing the whole wait over.
    """
    if hard_denied(peer):
        return False
    if allowed is not None and peer.peer_id not in allowed:
        return False
    return safety.check(Capability.READ_CHAT, peer).allowed


def filtering_source(source: Source, screen: Screen, *, monotonic: Callable[[], float]) -> Source:
    """Wrap ``source`` so refused items never reach the debounce logic.

    Screening *inside* the window rather than after it is the whole point: an
    item that is dropped must not consume the caller's wait either, so the
    remaining time is recomputed and the read is retried until the window is
    genuinely spent.
    """

    async def next_event(window: float) -> Any | None:
        deadline = monotonic() + window
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return None
            item = await source(remaining)
            if item is None:
                return None
            kept = await screen(item)
            if kept is not None:
                return kept

    return next_event


async def collect_burst(
    source: Source,
    *,
    timeout: float,
    debounce: float,
    limit: int,
    monotonic: Callable[[], float],
) -> tuple[list[Any], float, StopReason]:
    """Wait for a burst and return it whole: ``(events, waited, reason)``.

    The clock and the source are arguments so that the rule can be tested
    without sleeping. A debounce test written against real time asserts on the
    scheduler, not on the behaviour, and it fails on a loaded machine for
    reasons that have nothing to do with this function.
    """
    start = monotonic()
    deadline = start + timeout
    events: list[Any] = []
    reason: StopReason = "timeout"

    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            reason = "timeout"
            break

        # Before the first message the whole budget is available; after it, the
        # debounce window — but never past the ceiling, or a chat that keeps
        # talking would extend the call indefinitely.
        window = remaining if not events else min(remaining, debounce)
        # Which of the two bounded this wait is decided *before* it, not by
        # reading the clock afterwards: a debounce window that expired well
        # inside the budget still resumes after the deadline if the event loop
        # was busy, and the answer would then blame the ceiling for a chat that
        # simply went quiet.
        bounded_by_debounce = bool(events) and debounce < remaining
        event = await source(window)

        if event is None:
            reason = "quiet" if bounded_by_debounce else "timeout"
            break

        events.append(event)
        if len(events) >= limit:
            reason = "limit"
            break

    return events, monotonic() - start, reason


# --- the Telegram side ------------------------------------------------------


def _queue_source(queue: asyncio.Queue[Any]) -> Source:
    """Read one item from ``queue``, giving up after ``window`` seconds."""

    async def source(window: float) -> Any | None:
        if window <= 0:
            # `wait_for(..., timeout=0)` cancels the getter before it can look;
            # an item already sitting in the queue would be missed.
            try:
                return queue.get_nowait()
            except asyncio.QueueEmpty:
                return None
        try:
            return await asyncio.wait_for(queue.get(), timeout=window)
        except TimeoutError:
            return None

    return source


@contextlib.asynccontextmanager
async def incoming_messages(client: Any, *, maxsize: int = QUEUE_MAXSIZE) -> AsyncIterator[Source]:
    """Subscribe to incoming messages for the life of the block.

    Entered *before* anything else that can await, and that ordering is the
    point: resolving a named chat costs a network round trip, and a message
    arriving during it would otherwise be dispatched with no handler listening —
    the caller would then wait a full minute for the message that had already
    come and gone.

    The queue is bounded. Nothing here can slow a flood down (the handler is a
    callback Telethon awaits), so the only way an unbounded queue ends is with
    the process holding every message a hostile chat cares to send. Overflow
    drops the newest rather than evicting what is already waiting, so a burst
    that is already being collected is not corrupted by one that follows it.
    """
    from telethon import events as tl_events

    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)

    async def on_new_message(event: Any) -> None:
        # Overflow is deliberately silent. The alternative is reporting how
        # many events were dropped, and the drops are dominated by chats the
        # policy refuses — a count of those is the leak this operation avoids.
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(event)

    # `incoming=True`: a message this account sent from another device is not
    # something an agent was asked to wait for, and echoing it back would make
    # the tool's own reply-plan traffic look like an event.
    handler_filter = tl_events.NewMessage(incoming=True)
    client.add_event_handler(on_new_message, handler_filter)
    try:
        yield _queue_source(queue)
    finally:
        client.remove_event_handler(on_new_message, handler_filter)


def _screen_for(ctx: OperationContext, allowed: set[int] | None, *, deadline: float) -> Screen:
    """Turn a Telethon event into a reportable row, or drop it.

    ``deadline`` is absolute and shared with the collector. Screening is the one
    step here that can touch the network — an event may arrive without its chat
    attached, and resolving it is a round trip — so it is bounded by the same
    ceiling as the wait itself. Without that, `timeout_sec` would bound only the
    waiting and not the call, which is the property an MCP client depends on.
    """

    async def screen(event: Any) -> Any | None:
        chat = getattr(event, "chat", None)
        if chat is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                with telegram_errors(what="watch.wait"):
                    chat = await asyncio.wait_for(event.get_chat(), timeout=remaining)
            except Exception:  # noqa: BLE001 - an unidentifiable peer is dropped
                # Fail closed. A chat that cannot be resolved cannot be checked
                # against the policy, and reporting it anyway would be reporting
                # a message from a peer nobody authorised.
                return None
        if chat is None:
            return None

        ref = peer_ref(chat)
        if not admits(ctx.safety, ref, allowed=allowed):
            return None

        return {
            "chat": peer_summary(chat),
            "message": message_summary(getattr(event, "message", None), chat=ref),
        }

    return screen


async def handle_watch(ctx: OperationContext, params: WatchInput) -> Envelope:
    if not params.chats:
        # Watching everything is a sweep across the account's dialogs, and it
        # reveals which conversations are active — the same reconnaissance
        # `chats` and `inbox` are gated on. Naming chats does not need it: the
        # caller already knows they exist.
        require_enumeration(ctx, private=False, action="watch.wait")

    # The clock starts here, not at the first `await source(...)`. Connecting
    # the account and resolving up to twenty named chats are network round
    # trips, and a ceiling that excluded them would bound the waiting rather
    # than the call — which is not what an MCP client, unable to abandon a call
    # it is waiting on, needs the number to mean.
    started = time.monotonic()
    deadline = started + params.timeout_sec

    async with (
        open_account(ctx, params.account) as account,
        incoming_messages(account.client) as source,
    ):
        allowed: set[int] | None = None
        watched: list[dict[str, Any]] = []

        if params.chats:
            allowed = set()
            for target in params.chats:
                entity = await resolve_chat(account.client, target, what="watch.wait")
                ref = peer_ref(entity)
                guard_hard_denied(ctx, ref, action="watch.wait")
                # A named chat is checked loudly, once, up front: silently
                # watching nothing because a chat was not allowlisted looks
                # exactly like a quiet chat, and the caller would never learn
                # the difference.
                require_peer(ctx, Capability.READ_CHAT, ref, action="watch.wait")
                allowed.add(ref.peer_id)
                watched.append(peer_summary(entity))

        events, _, reason = await collect_burst(
            filtering_source(
                source,
                _screen_for(ctx, allowed, deadline=deadline),
                monotonic=time.monotonic,
            ),
            timeout=max(0.0, deadline - time.monotonic()),
            debounce=params.debounce_sec,
            limit=params.limit,
            monotonic=time.monotonic,
        )

    # What the caller actually spent, setup included — not just the part spent
    # inside the collector.
    waited = time.monotonic() - started

    warnings: list[str] = []
    if reason == "timeout" and events:
        warnings.append(
            "the wait hit timeout_sec while messages were still arriving; more may be waiting"
        )

    return telegram_result(
        ctx,
        {
            "watched": {
                "scope": "named" if params.chats else "permitted",
                "chats": watched or None,
            },
            "events": events,
            "waited_sec": round(waited, 3),
            "timeout_sec": params.timeout_sec,
            "stopped_because": reason,
        },
        account=account.label,
        returned=len(events),
        total=len(events),
        truncated=reason == "limit",
        truncated_reason="limit",
        warnings=warnings,
    )


WATCH = REGISTRY.register(
    Operation(
        name="watch.wait",
        cli=("watch",),
        mcp_tool="telegram_watch",
        summary="Wait for the next incoming message, instead of polling for it.",
        description=(
            "Blocks until a message arrives in a permitted chat, then returns the whole "
            "burst in one answer: replies that land within debounce_sec of each other "
            "wake the caller once, not once each. Always returns within timeout_sec "
            "(at most 300s); an empty result at the ceiling is an answer, not an error, "
            "and says how long it waited. Messages from chats the policy refuses are "
            "not reported at all — not even as the fact that something happened. "
            "Reading never marks anything as seen. Note that the account's session is "
            "held for the duration of the wait, so nothing else can use it meanwhile."
        ),
        input_model=WatchInput,
        effect=Effect.READ,
        capability=Capability.READ_CHAT,
        handler=handle_watch,  # type: ignore[arg-type]
        tags=("read", "triage"),
    )
)
