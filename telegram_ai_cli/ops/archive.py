"""The archive operations: fill it, search it offline, describe it, erase it.

Why an archive exists at all: a live search is an RPC. It spends the account's
flood budget, it can only match *text* — Telegram has no regular expressions —
and it answers about one account per call. Copying a named chat to disk makes
all three of those someone else's problem, at the cost of the copy being a
snapshot rather than the truth.

Four operations, and the interesting decisions are about which of them is which
kind of thing.

**`archive.sync` and `archive.forget` are `local_write`, not `read`.** They
write this machine's disk. `media.fetch` set the precedent and the reasoning is
the same: something that consumes a shared resource should not be classified
with the operations that consume nothing. Calling either of them `read` would
be the quiet lie — a caller reading the effect table would believe a tool that
creates a durable copy of somebody's private messages was as consequence-free as
listing chats. Neither is a `remote_write`: nothing they do is visible from
Telegram's side, nobody is messaged, no state on the account changes. Planning a
local file write for human approval would put a confirmation step in front of
the wrong risk while leaving `media.fetch` — which writes far more bytes —
without one.

**The read policy is applied on the read, not remembered from the write.** An
allowlist changes. A chat archived yesterday and closed today must stop
answering, so every path out of the archive rebuilds a `PeerRef` from the stored
identity and asks the kernel again. The hard floor is checked twice for the same
reason: on the way in, so Service Notifications and Saved Messages never land on
disk, and on the way out, so a database copied from somewhere else cannot
smuggle them back.

**`archive.forget` is deliberately *not* allowlist-gated.** A chat that has just
been removed from the allowlist is precisely the one whose copy on disk ought to
go. Refusing to delete it because it may no longer be read would strand personal
data with no way to remove it through the tool — the one failure mode a delete
operation exists to prevent. It touches nothing outside this machine, and the
worst a hostile caller achieves is deleting a cache that `archive.sync` rebuilds.
"""

from __future__ import annotations

import re
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import Field, field_validator

from ..archive import ArchiveStore, StoredMessage, epoch_of, iso_of, open_archive
from ..context import OperationContext
from ..envelope import Envelope
from ..errors import Denylisted, InvalidInput
from ..opspec import REGISTRY, Effect, Operation
from ..safety import Capability, PeerRef
from ._client import open_account, resolve_label
from ._common import (
    ReadInput,
    guard_hard_denied,
    hard_denied,
    require_enumeration,
    require_peer,
    telegram_errors,
    telegram_result,
)
from ._serialize import (
    MESSAGE_TEXT_LIMIT,
    media_summary,
    message_link,
    peer_ref,
    peer_summary,
)
from .chats import guard_message_link, resolve_chat_ref

#: Messages fetched per Telegram request while archiving. Telethon pages
#: history at 100 internally; asking for a round multiple of that keeps one
#: request per page rather than one and a fifth.
ARCHIVE_PAGE = 200

#: How many messages one ``archive.sync`` call may fetch. A ceiling rather than
#: "until it finishes" because a chat with 300 000 messages would otherwise hold
#: the account's session lock — and its flood budget — for as long as it took.
#: The call reports where it stopped and repeating it continues from there.
ARCHIVE_MAX_SYNC = 5000

#: How many stored rows one search may run its text predicate over. Bounds the
#: work of a catastrophically backtracking regular expression, which Python
#: cannot interrupt by timeout.
ARCHIVE_MAX_SCAN = 50_000

#: A pattern longer than this is not a query anyone typed.
MAX_PATTERN = 500

#: Wall-clock ceiling on the matching phase of one regular-expression search.
#: The row and pattern ceilings bound how much is matched, not how long a single
#: match takes — and `re` has no timeout of its own. See :func:`_time_budget`
#: for what this can and cannot enforce.
SEARCH_TIME_BUDGET_SEC = 10.0

#: Said in the payload as well as in `meta`, because a warning is what a person
#: skimming CLI output actually sees.
STALE_WARNING = (
    "these results come from the local archive, not from Telegram: anything said "
    "since the last sync is not in them. Run `archive sync` to bring it up to date"
)


class ArchiveSyncInput(ReadInput):
    chat: str = Field(
        description=(
            "Chat id, @username, or t.me link. Only this chat is archived — there is "
            "no bulk or background sync, by design."
        )
    )
    limit: int = Field(
        default=1000,
        ge=1,
        le=ARCHIVE_MAX_SYNC,
        description=(
            "Ceiling on messages fetched in this call. New messages are taken first, "
            "then older ones are backfilled. Repeat the call to continue; `complete` "
            "says whether the beginning of the history has been reached."
        ),
    )


class ArchiveSearchInput(ReadInput):
    query: str = Field(min_length=1, description="Substring, or a regular expression.")
    chat: str | None = Field(
        default=None,
        description=(
            "Restrict to one archived chat. Omit to search every archived chat this "
            "policy still permits."
        ),
    )
    regex: bool = Field(
        default=False,
        description=(
            "Treat `query` as a Python regular expression instead of a substring. "
            "This is what a live search cannot do: Telegram matches text, not patterns."
        ),
    )
    ignore_case: bool = Field(default=True, description="Match without regard to case.")
    sender: int | None = Field(default=None, description="Only messages from this sender id.")
    since: str | None = Field(
        default=None, description="Only messages at or after this ISO-8601 timestamp."
    )
    until: str | None = Field(
        default=None, description="Only messages at or before this ISO-8601 timestamp."
    )
    limit: int = Field(default=50, ge=1, le=500, description="Matching messages to return.")

    @field_validator("query")
    @classmethod
    def _bounded(cls, value: str) -> str:
        if len(value) > MAX_PATTERN:
            raise ValueError(f"query is longer than {MAX_PATTERN} characters")
        return value


class ArchiveStatusInput(ReadInput):
    """No arguments beyond the account: it describes what is on this disk."""


class ArchiveForgetInput(ReadInput):
    chat_id: int = Field(
        description=(
            "Marked chat id to erase from the archive, as `archive status` reports it. "
            "An id and not a @username or a link: deleting local data must not depend "
            "on Telegram resolving anything, or a chat that has since been left could "
            "never be erased."
        )
    )


# --- filling it -------------------------------------------------------------


def _stored(message: Any) -> StoredMessage:
    """One Telethon message, flattened into the columns the archive keeps.

    A subset of :func:`_serialize.message_summary` on purpose. Reactions, views,
    pin state and read pointers are all *live* properties that change after a
    message is sent; storing them would mean an archive that confidently reports
    last week's reaction counts. What is kept is what does not move: who said
    what, when, in reply to what.
    """
    text = getattr(message, "message", None) or None
    truncated = bool(text and len(text) > MESSAGE_TEXT_LIMIT)
    media = media_summary(message)
    return StoredMessage(
        message_id=int(getattr(message, "id", 0)),
        date=epoch_of(getattr(message, "date", None)),
        sender_id=getattr(message, "sender_id", None),
        sender=_display_of(message),
        sender_username=getattr(getattr(message, "sender", None), "username", None),
        outgoing=bool(getattr(message, "out", False)),
        text=text[:MESSAGE_TEXT_LIMIT] if text else None,
        text_truncated=truncated,
        reply_to_msg_id=getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
        topic_id=_topic_of(message),
        media_type=media["type"] if media else None,
    )


def _display_of(message: Any) -> str | None:
    from ._serialize import display_name

    return display_name(getattr(message, "sender", None))


def _topic_of(message: Any) -> int | None:
    from ._serialize import topic_id_of

    return topic_id_of(message)


async def _fetch(client: Any, entity: Any, *, limit: int, max_id: int, min_id: int) -> list[Any]:
    with telegram_errors(what="archive.sync"):
        page = await client.get_messages(entity, limit=limit, max_id=max_id, min_id=min_id)
    return list(page)


async def _walk(
    client: Any,
    entity: Any,
    store: ArchiveStore,
    account: str,
    chat_id: int,
    *,
    start_at: int,
    floor: int,
    budget: int,
) -> tuple[int, int | None, int | None, bool]:
    """Page backwards from ``start_at`` down to ``floor``, storing as it goes.

    Both bounds are Telegram's own exclusive cursors, which is what makes this
    resumable: ``max_id`` walks down from the newest message, ``min_id`` stops
    at the watermark, and neither ever re-requests a message already on disk.

    Returns ``(stored, lowest_id, highest_id, reached_the_floor)``. The last
    value is the one that matters to the caller: a walk that merely ran out of
    budget has left a *gap* between what it stored and what was already on disk,
    and a watermark advanced across a gap would mean those messages are never
    fetched again.
    """
    stored = 0
    lowest: int | None = None
    highest: int | None = None
    cursor = start_at
    reached = False

    while budget > 0:
        want = min(ARCHIVE_PAGE, budget)
        page = await _fetch(client, entity, limit=want, max_id=cursor, min_id=floor)
        if not page:
            reached = True
            break
        rows = [_stored(message) for message in page if getattr(message, "id", None)]
        stored += store.store_messages(account, chat_id, rows)
        ids = [row.message_id for row in rows]
        lowest = min(ids) if lowest is None else min(lowest, *ids)
        highest = max(ids) if highest is None else max(highest, *ids)
        budget -= len(page)
        # Exclusive again: the next page starts strictly below what was just read.
        cursor = min(ids)
        if len(page) < want:
            # Telegram had fewer messages than were asked for, so there are none
            # left between the cursor and the floor.
            reached = True
            break
        if cursor <= floor + 1:
            # And nothing can lie in a gap of zero ids.
            reached = True
            break

    return stored, lowest, highest, reached


async def handle_archive_sync(ctx: OperationContext, params: ArchiveSyncInput) -> Envelope:
    warnings: list[str] = []

    async with open_account(ctx, params.account) as account:
        entity, link = await resolve_chat_ref(account.client, params.chat, what="archive.sync")
        ref = peer_ref(entity)
        # Both gates before a single message is fetched: a refused chat must
        # cost no round trip and leave nothing on disk.
        guard_hard_denied(ctx, ref, action="archive.sync")
        require_peer(ctx, Capability.READ_CHAT, ref, action="archive.sync")
        guard_message_link(ref, link, what="archive.sync")
        if link is not None and link.message_id is not None:
            warnings.append(f"the link names message {link.message_id}; the whole chat is archived")

        event = ctx.audit.attempt(
            action="archive.sync",
            account=account.label,
            actor=ctx.actor,
            peer_id=ref.peer_id,
            extra={"limit": params.limit},
        )

        try:
            with open_archive(ctx.settings) as store:
                previous = store.register_chat(account.label, ref)
                budget = params.limit
                stored = 0
                oldest = previous.oldest_message_id if previous else None
                newest = previous.newest_message_id if previous else None
                complete = bool(previous.complete) if previous else False
                pending_from = previous.pending_from_id if previous else None
                pending_top = previous.pending_top_id if previous else None

                # New first. A caller that runs out of budget on a long backfill
                # still gets today's messages, which is what it usually wanted.
                if newest is not None:
                    # Resume an interrupted run where it stopped, not at the top.
                    # Restarting at the newest message would re-fetch the same
                    # page on every call and never join the two ends — a chat
                    # with more new messages than one budget would then never
                    # finish, however often the command was run.
                    fresh, low, high, reached = await _walk(
                        account.client,
                        entity,
                        store,
                        account.label,
                        ref.peer_id,
                        start_at=pending_from or 0,
                        floor=newest,
                        budget=budget,
                    )
                    stored += fresh
                    budget -= fresh
                    if high is not None:
                        pending_top = high if pending_top is None else max(pending_top, high)
                    if reached:
                        # The hole is closed: everything from the old watermark
                        # up to the highest id seen across the whole run is now
                        # on disk, so the mark may move to it.
                        if pending_top is not None:
                            newest = max(newest, pending_top)
                        pending_from = pending_top = None
                    elif low is not None:
                        # Still a hole. Remember both ends of it; the mark stays
                        # put, because moving it across a gap would make the
                        # messages inside unreachable forever.
                        pending_from = low

                # Then backwards, from wherever the archive currently stops.
                if budget > 0 and not complete:
                    old, low, high, beginning = await _walk(
                        account.client,
                        entity,
                        store,
                        account.label,
                        ref.peer_id,
                        start_at=oldest or 0,
                        floor=0,
                        budget=budget,
                    )
                    stored += old
                    if low is not None:
                        oldest = low if oldest is None else min(oldest, low)
                    if high is not None:
                        newest = high if newest is None else max(newest, high)
                    complete = beginning

                store.set_watermarks(
                    account.label,
                    ref.peer_id,
                    oldest=oldest,
                    newest=newest,
                    complete=complete,
                    pending_from=pending_from,
                    pending_top=pending_top,
                )
                total = store.count(account.label, ref.peer_id)
        except BaseException as exc:
            ctx.audit.outcome(
                event, status="failed", error_code=type(exc).__name__, detail=str(exc)[:200]
            )
            raise

        ctx.audit.outcome(event, status="applied", detail=f"{stored} message(s)")

    contiguous = pending_from is None
    if not contiguous:
        # Said separately from the backfill, because it is a different hole and
        # it is the one that makes "up to date" untrue: there are newer messages
        # on disk with a gap under them.
        warnings.append(
            f"more messages arrived than one call may fetch: ids between "
            f"{newest} and {pending_from} are not archived yet. Run `archive sync` "
            "again (a larger limit closes it in one go)"
        )
    if not complete:
        warnings.append(
            "the beginning of this chat has not been reached; run `archive sync` again "
            "to continue backfilling"
        )

    return telegram_result(
        ctx,
        {
            "chat": peer_summary(entity),
            "stored": stored,
            "messages": total,
            "oldest_message_id": oldest,
            "newest_message_id": newest,
            # Whole means both: no hole in the middle, and back to the first
            # message. Reporting the backfill alone would call an archive with a
            # gap in it complete.
            "complete": complete and contiguous,
            "reaches_first_message": complete,
            "contiguous": contiguous,
        },
        account=account.label,
        returned=stored,
        total=total,
        truncated=not (complete and contiguous),
        truncated_reason="budget",
        warnings=warnings,
        extra={"source": "archive"},
    )


# --- reading it -------------------------------------------------------------


@contextmanager
def _time_budget(seconds: float, *, regex: bool) -> Iterator[None]:
    """Stop a search that has run away, where the platform allows it.

    Row and pattern-length ceilings bound how *much* is matched; they do not
    bound how long a single match takes. `re` has no timeout, and a
    catastrophically backtracking pattern can spend minutes on one 4000-character
    message — so an unattended MCP server would sit there holding the call open
    (raised by review, 2026-08-23).

    ``SIGALRM`` is the only interruption the standard library offers without a
    second process, and it comes with two honest limits: it exists on POSIX
    only, and it can be installed only from the main thread. Where either is
    untrue this degrades to no timer at all rather than to a fake one, and the
    residual risk is written down in `docs/operations.md` instead of being
    papered over. Substring matching is linear and never given a timer.

    An existing handler is restored, and the timer is always cancelled: leaving
    an armed ``SIGALRM`` behind would fire it into unrelated code later.
    """
    if not regex or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _expired(_signum: int, _frame: Any) -> None:
        raise _SearchTimeout

    try:
        previous = signal.signal(signal.SIGALRM, _expired)
    except ValueError:
        # Not the main thread. No timer is available; say nothing and run.
        yield
        return
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class _SearchTimeout(Exception):
    """Internal: the pattern outran :data:`SEARCH_TIME_BUDGET_SEC`."""


def _predicate(params: ArchiveSearchInput) -> Any:
    """One matcher for both modes, built once rather than per row."""
    if params.regex:
        try:
            pattern = re.compile(params.query, re.IGNORECASE if params.ignore_case else 0)
        except re.error as exc:
            raise InvalidInput(
                f"archive.search: {params.query!r} is not a valid regular expression: {exc}",
                suggestion="Escape the pattern, or drop regex to search for it literally.",
            ) from None
        return lambda text: bool(pattern.search(text))

    needle = params.query.lower() if params.ignore_case else params.query
    if params.ignore_case:
        return lambda text: needle in text.lower()
    return lambda text: needle in text


def _timestamp(value: str | None, *, field: str) -> float | None:
    """An ISO-8601 bound, refusing to guess at anything else."""
    if value is None:
        return None
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidInput(
            f"archive.search: {field} must be an ISO-8601 timestamp, got {value!r}",
            suggestion="For example 2026-08-01T00:00:00Z, or 2026-08-01.",
        ) from None
    return epoch_of(parsed)


def readable(ctx: OperationContext, chat: Any) -> bool:
    """Whether today's policy permits this archived chat. Fail-closed.

    ``chat.peer`` is ``None`` when the stored ``kind`` is not one this build
    recognises, which can only happen in a database this project did not write.
    That is a refusal, not a default: mapping it to "unknown kind" would make it
    *not private*, and a private conversation would then be judged by the group
    rule, which permits by default.
    """
    peer: PeerRef | None = chat.peer
    if peer is None:
        return False
    if hard_denied(peer):
        return False
    return ctx.safety.check(Capability.READ_CHAT, peer).allowed


def _visible(ctx: OperationContext, chats: list[Any]) -> tuple[list[Any], int]:
    """The archived chats today's policy still permits, and how many it hid.

    The count matters as much as the list. A short answer with no explanation
    reads as "nothing was archived", which is a different statement from "what
    was archived may no longer be read".
    """
    permitted = [chat for chat in chats if readable(ctx, chat)]
    return permitted, len(chats) - len(permitted)


def _message_row(row: Any, chat: Any) -> dict[str, Any]:
    """An archived message in the shape a live read returns.

    Deliberately the same keys, so a caller writes one parser for both sources
    and reads `meta.source` to learn which it got. Fields the archive does not
    keep are ``null`` rather than absent — a missing key reads as a different
    shape, a null reads as "not recorded".
    """
    peer: PeerRef | None = chat.peer
    return {
        "id": row["message_id"],
        "date": iso_of(row["date"]),
        "outgoing": bool(row["outgoing"]),
        "sender_id": row["sender_id"],
        "sender": row["sender"],
        "sender_username": row["sender_username"],
        "text": row["text"],
        "text_truncated": bool(row["text_truncated"]),
        "reply_to_msg_id": row["reply_to_msg_id"],
        "topic_id": row["topic_id"],
        "media": {"type": row["media_type"], "fetchable": True} if row["media_type"] else None,
        "link": message_link(peer, row["message_id"], row["topic_id"]),
        "match": True,
        # Per row, not only in `meta`: an unscoped search mixes chats whose
        # syncs are days apart, and one timestamp in the envelope would be a
        # claim about all of them.
        "archived_at": iso_of(chat.last_synced_at),
    }


async def handle_archive_search(ctx: OperationContext, params: ArchiveSearchInput) -> Envelope:
    warnings: list[str] = []
    matches = _predicate(params)
    since = _timestamp(params.since, field="since")
    until = _timestamp(params.until, field="until")

    label = resolve_label(ctx, params.account)
    scoped = params.chat is not None

    with open_archive(ctx.settings) as store:
        if scoped:
            # Resolved locally: the chat argument names something already on
            # this disk, and going to Telegram to turn "-555" into an entity
            # would make an offline search need the network after all.
            chat_id = _archived_chat_id(store, label, params.chat or "")
            chat = store.chat(label, chat_id) if chat_id is not None else None
            if chat is None:
                return telegram_result(
                    ctx,
                    {"messages": []},
                    account=label,
                    returned=0,
                    total=0,
                    warnings=[
                        f"chat {params.chat} is not archived for account {label}; "
                        "run `archive sync` on it first"
                    ],
                    extra={"source": "archive", "synced_at": None},
                )
            peer = chat.peer
            if peer is None:
                # Written by something that is not this project. Refused rather
                # than judged under a guessed kind — see `ArchivedChat.peer`.
                ctx.audit.refusal(
                    action="archive.search",
                    actor=ctx.actor,
                    reason="archived chat has an unrecognised kind and cannot be judged",
                    peer_id=chat.chat_id,
                )
                raise Denylisted(
                    f"archived chat {chat.chat_id} records a chat kind this build does not "
                    "recognise, so the read policy cannot be applied to it",
                    suggestion=f"Remove it with `archive forget {chat.chat_id}` and re-sync.",
                )
            guard_hard_denied(ctx, peer, action="archive.search")
            require_peer(ctx, Capability.READ_CHAT, peer, action="archive.search")
            visible, withheld = [chat], 0
        else:
            # Sweeping every archived chat reveals which conversations were
            # copied here, which is the same disclosure as a dialog listing.
            require_enumeration(ctx, private=False, action="archive.search")
            visible, withheld = _visible(ctx, store.chats(label))

        by_id = {chat.chat_id: chat for chat in visible}
        rows: list[dict[str, Any]] = []
        scanned = 0
        try:
            with _time_budget(SEARCH_TIME_BUDGET_SEC, regex=params.regex):
                for row in store.candidates(
                    label,
                    chat_ids=list(by_id),
                    sender_id=params.sender,
                    since=since,
                    until=until,
                    scan=ARCHIVE_MAX_SCAN,
                ):
                    scanned += 1
                    text = row["text"]
                    if not text or not matches(text):
                        continue
                    chat = by_id[row["chat_id"]]
                    entry = _message_row(row, chat)
                    if not scoped:
                        entry["chat"] = _chat_identity(chat)
                    rows.append(entry)
                    if len(rows) >= params.limit:
                        break
        except _SearchTimeout:
            # Reported as bad input rather than a server fault: the pattern is
            # the thing that has to change, and a partial answer would look like
            # a complete one.
            raise InvalidInput(
                f"archive.search: the pattern took longer than "
                f"{SEARCH_TIME_BUDGET_SEC:g}s and was stopped after {scanned} message(s)",
                suggestion=(
                    "Nested quantifiers such as (a+)+ backtrack exponentially. "
                    "Simplify the pattern, or narrow the search with chat, sender "
                    "or a date range."
                ),
            ) from None

        synced = [chat.last_synced_at for chat in visible]
        gapped = [chat.chat_id for chat in visible if not chat.contiguous]

    if gapped:
        # A hole is worse than staleness and reads identically in the output: a
        # search over a chat missing a block in the middle answers "nobody said
        # that" with the same confidence as one over a whole chat.
        warnings.append(
            f"{len(gapped)} chat(s) searched have a gap in their archive "
            f"(ids {', '.join(str(c) for c in gapped[:5])}): a sync was interrupted "
            "and the messages in the gap are not here. Run `archive sync` on them"
        )
    if withheld:
        ctx.audit.refusal(
            action="archive.search",
            actor=ctx.actor,
            reason=f"{withheld} archived chat(s) withheld by the read policy",
        )
        warnings.append(
            f"{withheld} archived chat(s) withheld: they are not readable under the "
            "current policy, even though they were archived when it permitted them"
        )
    warnings.append(STALE_WARNING)
    if scanned >= ARCHIVE_MAX_SCAN:
        warnings.append(
            f"only the {ARCHIVE_MAX_SCAN} most recent archived messages were examined; "
            "narrow the search with chat, sender or a date range"
        )

    data: dict[str, Any] = {"messages": rows}
    if scoped and visible:
        data["chat"] = _chat_identity(visible[0])

    return telegram_result(
        ctx,
        data,
        account=label,
        returned=len(rows),
        truncated=len(rows) >= params.limit or scanned >= ARCHIVE_MAX_SCAN,
        truncated_reason="limit",
        warnings=warnings,
        extra={
            # Never omitted, and never true of a live read: an archive answer
            # that looked like a live one is last week's state reported as now.
            "source": "archive",
            # The *oldest* sync among the chats searched, because that is the
            # honest freshness of the answer as a whole.
            "synced_at": iso_of(min(synced)) if synced else None,
            "chats_searched": len(visible),
            "withheld_chats": withheld,
        },
    )


def _archived_chat_id(store: ArchiveStore, account: str, chat: str) -> int | None:
    """Turn what the caller typed into an archived chat id, without the network.

    An id is taken literally; a ``@username`` is matched against the ones stored
    at sync time. A username can be reassigned, so this is a convenience for
    finding a row and never a permission — the row it finds is judged by its
    numeric id like everything else.
    """
    text = (chat or "").strip()
    if text.lstrip("-").isdigit():
        return int(text)
    needle = text.removeprefix("@").lower()
    for row in store.chats(account):
        if (row.username or "").lower() == needle:
            return row.chat_id
    return None


def _chat_identity(chat: Any) -> dict[str, Any]:
    return {
        "id": chat.chat_id,
        "kind": chat.kind,
        "username": chat.username,
        "title": chat.title,
        "archived_at": iso_of(chat.last_synced_at),
        "complete": chat.complete,
    }


async def handle_archive_status(ctx: OperationContext, params: ArchiveStatusInput) -> Envelope:
    label = resolve_label(ctx, params.account)
    # Listing what has been archived says which conversations exist on this
    # disk, so it is gated like any other enumeration.
    require_enumeration(ctx, private=False, action="archive.status")

    with open_archive(ctx.settings) as store:
        visible, withheld = _visible(ctx, store.chats(label))
        rows = [chat.to_row() for chat in visible]
        total = sum(row["messages"] for row in rows)

    warnings: list[str] = []
    if withheld:
        ctx.audit.refusal(
            action="archive.status",
            actor=ctx.actor,
            reason=f"{withheld} archived chat(s) withheld by the read policy",
        )
        warnings.append(
            f"{withheld} archived chat(s) withheld by the current read policy. They are "
            "still on disk: `archive forget <chat_id>` removes them"
        )
    if any(not row["complete"] for row in rows):
        warnings.append(
            "some chats are archived only from a recent message backwards; `complete` "
            "says which, and an incomplete archive cannot answer 'nobody ever said that'"
        )

    return telegram_result(
        ctx,
        {"chats": rows, "messages": total},
        account=label,
        returned=len(rows),
        total=len(rows),
        warnings=warnings,
        extra={"source": "archive", "withheld_chats": withheld},
    )


# --- erasing it -------------------------------------------------------------


async def handle_archive_forget(ctx: OperationContext, params: ArchiveForgetInput) -> Envelope:
    label = resolve_label(ctx, params.account)

    event = ctx.audit.attempt(
        action="archive.forget",
        account=label,
        actor=ctx.actor,
        peer_id=params.chat_id,
    )
    try:
        with open_archive(ctx.settings) as store:
            existed, removed = store.forget(label, params.chat_id)
    except BaseException as exc:
        ctx.audit.outcome(
            event, status="failed", error_code=type(exc).__name__, detail=str(exc)[:200]
        )
        raise
    ctx.audit.outcome(event, status="applied", detail=f"{removed} message(s)")

    warnings = (
        []
        if existed
        else [f"chat {params.chat_id} was not in the archive for account {label}; nothing to do"]
    )
    return telegram_result(
        ctx,
        {"chat_id": params.chat_id, "forgotten": existed, "messages": removed},
        account=label,
        returned=removed,
        warnings=warnings,
        extra={"source": "archive"},
    )


ARCHIVE_SYNC = REGISTRY.register(
    Operation(
        name="archive.sync",
        cli=("archive", "sync"),
        mcp_tool="telegram_archive_sync",
        summary="Copy one chat into the local archive, continuing where it left off.",
        description=(
            "Effect local_write: it writes a SQLite file on this machine, never anything "
            "on Telegram. One chat per call, named explicitly — there is no background "
            "sync and nothing is archived that was not asked for. A repeat call fetches "
            "only what is new (the stored watermark bounds the request) and then "
            "backfills older messages until the per-call budget runs out; `complete` "
            "says whether the beginning of the history has been reached. Attachments are "
            "never downloaded, only their metadata. The read policy is checked before "
            "the first request, and again on every read of the archive afterwards."
        ),
        input_model=ArchiveSyncInput,
        effect=Effect.LOCAL_WRITE,
        capability=Capability.READ_CHAT,
        handler=handle_archive_sync,  # type: ignore[arg-type]
        tags=("archive", "local"),
    )
)

ARCHIVE_SEARCH = REGISTRY.register(
    Operation(
        name="archive.search",
        cli=("archive", "search"),
        mcp_tool="telegram_archive_search",
        summary="Search the local archive offline, by substring or regular expression.",
        description=(
            "Costs no Telegram request at all, which is what makes regular expressions, "
            "sender filters and date ranges possible — Telegram's own search matches "
            "text only. Every result is checked against the *current* read policy for "
            "its own chat, so a chat archived while it was permitted and closed since is "
            "withheld and counted. The answer always carries `meta.source: archive` and "
            "the timestamp of the oldest sync it covers: an archive result mistaken for "
            "a live one is stale state reported as current."
        ),
        input_model=ArchiveSearchInput,
        effect=Effect.READ,
        capability=Capability.READ_CHAT,
        handler=handle_archive_search,  # type: ignore[arg-type]
        tags=("archive", "read"),
    )
)

ARCHIVE_STATUS = REGISTRY.register(
    Operation(
        name="archive.status",
        cli=("archive", "status"),
        mcp_tool="telegram_archive_status",
        summary="What is in the local archive: chats, message counts, sync times.",
        description=(
            "One row per archived chat with its message count, id watermarks, whether "
            "the backfill reached the beginning, and when it was last synchronised. "
            "Filtered by the current read policy like every other read; chats it "
            "withholds are counted, and `archive forget` is how they leave the disk."
        ),
        input_model=ArchiveStatusInput,
        effect=Effect.READ,
        capability=Capability.READ_CHAT,
        handler=handle_archive_status,  # type: ignore[arg-type]
        tags=("archive", "read"),
    )
)

ARCHIVE_FORGET = REGISTRY.register(
    Operation(
        name="archive.forget",
        cli=("archive", "forget"),
        mcp_tool="telegram_archive_forget",
        summary="Erase one chat, and every message of it, from the local archive.",
        description=(
            "Effect local_write. Deliberately not gated on the read allowlist: a chat "
            "just removed from it is precisely the one whose copy on disk ought to go, "
            "and refusing would strand personal data with no way to remove it. "
            "Idempotent — forgetting a chat that was never archived reports "
            "`forgotten: false` rather than failing."
        ),
        input_model=ArchiveForgetInput,
        effect=Effect.LOCAL_WRITE,
        # The one operation in the project that removes data, and the one whose
        # second run is identical to its first. Both are published to MCP
        # clients, which otherwise inherit the blanket "not destructive, not
        # idempotent" guess (raised by review, 2026-08-23).
        destructive=True,
        idempotent=True,
        # No peer capability on purpose: deletion of local data is not gated on
        # permission to read the thing being deleted. See the module docstring.
        capability=None,
        handler=handle_archive_forget,  # type: ignore[arg-type]
        tags=("archive", "local"),
    )
)
