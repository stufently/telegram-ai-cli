"""``telegram_mentions`` — where this account was actually called out.

"What needs an answer" and "where is there a lot of unread" are different
questions, and the second one drowns the first. Telegram knows the difference:
alongside the plain unread counter it keeps two more per dialog — unread
*mentions* (someone typed the handle, or replied to a message of this account's)
and unread *reactions* (someone reacted to something this account wrote). Both
are a person addressing this account by name; a hundred unread messages in a
busy group is not. ``telegram_inbox`` ranks on those counters. This operation
returns what is behind them: which messages, from whom, with what emoji.

Three decisions worth stating.

**Nothing is acknowledged, and that is the whole risk here.** Telethon's
namespace puts ``GetUnreadMentionsRequest`` one letter from
``ReadMentionsRequest``, and the same for reactions. The first asks which
mentions are unread; the second clears them — on every device the owner has.
An agent that "just looked" and made a badge disappear from somebody's phone
has caused an invisible, unrecoverable side effect, so only the two ``Get``
requests appear below and ``tests/test_mentions.py`` asserts on the *whole*
list of requests the operation issued, not just on its answer.

**The counters decide what is fetched.** A chat whose counters are zero is
never asked about, so a sweep across a fleet costs one dialog listing per
account plus one page per chat that genuinely has something. The chats are
ranked and cut to ``limit`` *before* any page is requested — otherwise a
five-hundred-dialog account would issue five hundred requests to fill a list of
twenty.

**Enumeration is not a read.** Walking the dialog list needs ``enumerate``;
showing the messages inside a chat needs the same ``read_chat`` (or ``read_dm``)
that reading it any other way needs. A chat the policy will not open is counted
as withheld and never fetched, which is the same bargain ``telegram_search``
makes for a global search: a stated omission beats an unexplained short list.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from pydantic import Field

from ..context import OperationContext
from ..envelope import Envelope
from ..opspec import REGISTRY, Effect, Operation
from ..safety import Capability, PeerRef
from ._client import open_account, visible_labels
from ._common import (
    MAX_DIALOG_SCAN,
    MAX_PAGE,
    ReadInput,
    hard_denied,
    require_enumeration,
    telegram_errors,
    telegram_result,
)
from ._serialize import (
    display_name,
    marked_id,
    message_summary,
    peer_ref,
    peer_summary,
    recent_reactors,
)

#: Said in the payload rather than left as an empty list. Telegram attaches the
#: reactors to the message only where the chat is small enough; elsewhere the
#: count is all there is, and the roster is a separate privacy-gated request
#: this tool never makes.
NO_REACTORS = (
    "Telegram did not name anyone with this reaction; the full list of who "
    "reacted is a separate privacy-gated request this tool never makes"
)


class MentionsInput(ReadInput):
    """``account`` narrows the sweep to one label; by default it covers all."""

    limit: int = Field(
        default=20, ge=1, le=MAX_PAGE, description="Conversations to fetch and return."
    )
    per_chat: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Messages to fetch per chat, for each of mentions and reactions.",
    )
    include_mentions: bool = Field(default=True, description="Include unread mentions and replies.")
    include_reactions: bool = Field(
        default=True,
        description="Include unread reactions to this account's own messages.",
    )
    include_private: bool = Field(
        default=False,
        description="Include one-to-one conversations (requires safety.read.enumerate_dms).",
    )


class _Candidate:
    """One chat that has something unread addressed to this account."""

    __slots__ = ("entity", "label", "last_at", "mentions", "reactions", "ref")

    def __init__(
        self,
        *,
        label: str,
        entity: Any,
        ref: PeerRef,
        mentions: int,
        reactions: int,
        last_at: Any,
    ) -> None:
        self.label = label
        self.entity = entity
        self.ref = ref
        self.mentions = mentions
        self.reactions = reactions
        self.last_at = last_at

    @property
    def rank(self) -> tuple[int, int]:
        return (-self.mentions, -self.reactions)


def _sender_index(result: Any) -> dict[int, Any]:
    """People and chats that came with a raw page, keyed by marked id.

    A raw API response keeps its entities in ``users``/``chats`` and never
    attaches them to the messages, so without this the one field a mention is
    read for — who called — comes back null.
    """
    index: dict[int, Any] = {}
    for entity in list(getattr(result, "users", None) or []) + list(
        getattr(result, "chats", None) or []
    ):
        # An entity shape this Telethon cannot turn into an id costs one name
        # in the output; raising here would cost the whole page.
        with contextlib.suppress(Exception):
            index[marked_id(entity)] = entity
    return index


async def _unread_page(client: Any, request: Any) -> tuple[list[Any], dict[int, Any]]:
    """Run one ``Get…`` request and unpack it. Nothing here acknowledges."""
    with telegram_errors(what="mentions.list"):
        result = await client(request)
    messages = list(getattr(result, "messages", None) or [])
    return messages, _sender_index(result)


async def _mentions_of(client: Any, entity: Any, limit: int) -> tuple[list[Any], dict[int, Any]]:
    # `messages.getUnreadMentions` — reports, never acknowledges. Its sibling
    # `ReadMentionsRequest` is what clears the badge, and it is not used here.
    from telethon.tl.functions.messages import GetUnreadMentionsRequest

    return await _unread_page(
        client,
        GetUnreadMentionsRequest(
            peer=entity, offset_id=0, add_offset=0, limit=limit, max_id=0, min_id=0
        ),
    )


async def _reactions_of(client: Any, entity: Any, limit: int) -> tuple[list[Any], dict[int, Any]]:
    # `messages.getUnreadReactions`, with the same relationship to
    # `ReadReactionsRequest`: this one reports, that one clears.
    from telethon.tl.functions.messages import GetUnreadReactionsRequest

    return await _unread_page(
        client,
        GetUnreadReactionsRequest(
            peer=entity, offset_id=0, add_offset=0, limit=limit, max_id=0, min_id=0
        ),
    )


def _reactor_rows(message: Any, senders: dict[int, Any]) -> list[dict[str, Any]]:
    """Who reacted, with a name where the page happened to carry the person.

    Only the reactors Telegram still flags as unread: a page of unread
    reactions carries the older ones on the same message too, and re-announcing
    a reaction the owner has already seen is exactly the noise this operation
    exists to cut.
    """
    rows = recent_reactors(getattr(message, "reactions", None), unread_only=True) or []
    for row in rows:
        row["name"] = display_name(senders.get(row.get("peer_id")))
    return rows


def _candidates_of(
    ctx: OperationContext, label: str, dialogs: list[Any], params: MentionsInput
) -> tuple[list[_Candidate], int, int]:
    """Which of one account's dialogs are worth a page, and what was left out.

    Returns the chats to fetch, how many private ones were hidden, and how many
    the read policy withheld. Pure decision-making: no request is issued here,
    which is what makes "a refused chat costs no call" true by construction.

    The order of the checks is the point. A private chat is dropped *before* its
    counters are read, so the hidden tally is "how many private conversations
    exist" — the same thing ``telegram_inbox`` already discloses — and not "how
    many private conversations have somebody waiting in them right now", which
    is a fact about activity that an account with ``enumerate_dms`` off has not
    agreed to reveal.
    """
    candidates: list[_Candidate] = []
    hidden = 0
    withheld = 0

    for dialog in dialogs:
        entity = getattr(dialog, "entity", None)
        if entity is None:
            continue

        ref = peer_ref(entity)
        if hard_denied(ref):
            continue
        if ref.is_private and not params.include_private:
            hidden += 1
            continue

        mentions = int(getattr(dialog, "unread_mentions_count", 0) or 0)
        reactions = int(getattr(dialog, "unread_reactions_count", 0) or 0)
        if not params.include_mentions:
            mentions = 0
        if not params.include_reactions:
            reactions = 0
        if mentions == 0 and reactions == 0:
            continue

        # Reading the messages inside a chat is a read, whatever the dialog
        # list already revealed about it. Enumeration got us the counter; it
        # does not get us the content.
        if not ctx.safety.check(Capability.READ_CHAT, ref).allowed:
            withheld += 1
            continue

        candidates.append(
            _Candidate(
                label=label,
                entity=entity,
                ref=ref,
                mentions=mentions,
                reactions=reactions,
                last_at=getattr(dialog, "date", None),
            )
        )

    return candidates, hidden, withheld


async def _row_for(client: Any, candidate: _Candidate, params: MentionsInput) -> dict[str, Any]:
    """One chat's unread mentions and reactions, as the payload row."""
    mentions: list[dict[str, Any]] = []
    reactions: list[dict[str, Any]] = []

    if candidate.mentions:
        messages, senders = await _mentions_of(client, candidate.entity, params.per_chat)
        mentions = [
            message_summary(message, chat=candidate.ref, senders=senders) for message in messages
        ]

    if candidate.reactions:
        messages, senders = await _reactions_of(client, candidate.entity, params.per_chat)
        for message in messages:
            row = message_summary(message, chat=candidate.ref, senders=senders)
            reactors = _reactor_rows(message, senders)
            row["reactors"] = reactors
            if not reactors:
                row["reactors_reason"] = NO_REACTORS
            reactions.append(row)

    return {
        "account": candidate.label,
        "chat": peer_summary(candidate.entity),
        "unread_mentions": candidate.mentions,
        "unread_reactions": candidate.reactions,
        "mentions": mentions,
        "reactions": reactions,
    }


@dataclass(frozen=True, slots=True)
class _Sweep:
    """What one account's pass produced, including what it could not show."""

    rows: list[dict[str, Any]]
    found: int
    """Chats with something unread for this account — including past the cut."""
    mentions: int
    reactions: int
    hidden: int
    withheld: int
    scan_capped: bool
    """The dialog walk stopped at the ceiling, so there may be more behind it."""


async def _sweep_account(ctx: OperationContext, label: str, params: MentionsInput) -> _Sweep:
    """One account, one connection: list the dialogs, then page the top ones.

    The chats are ranked and cut to ``limit`` *before* any page is requested.
    An account with five hundred dialogs would otherwise issue five hundred
    requests to fill a list of twenty, and the ones past the cut would be paid
    for and thrown away.
    """
    async with open_account(ctx, label) as account:
        with telegram_errors(what=f"mentions {label}"):
            dialogs: list[Any] = []
            async for dialog in account.client.iter_dialogs(ignore_migrated=True):
                dialogs.append(dialog)
                if len(dialogs) >= MAX_DIALOG_SCAN:
                    break

        candidates, hidden, withheld = _candidates_of(ctx, label, dialogs, params)
        candidates.sort(key=lambda candidate: candidate.rank)

        rows = [
            await _row_for(account.client, candidate, params)
            for candidate in candidates[: params.limit]
        ]

    return _Sweep(
        rows=rows,
        found=len(candidates),
        # Summed over every candidate, not over the rows that survived the cut:
        # a total that counted only what is displayed would agree with the list
        # and disagree with reality, which is the one way a total can mislead.
        mentions=sum(candidate.mentions for candidate in candidates),
        reactions=sum(candidate.reactions for candidate in candidates),
        hidden=hidden,
        withheld=withheld,
        scan_capped=len(dialogs) >= MAX_DIALOG_SCAN,
    )


async def handle_mentions(ctx: OperationContext, params: MentionsInput) -> Envelope:
    require_enumeration(ctx, private=params.include_private, action="mentions.list")

    labels = [params.account] if params.account else visible_labels(ctx)
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    found = mentions = reactions = hidden = withheld = 0
    scan_capped = False

    for label in labels:
        try:
            sweep = await _sweep_account(ctx, label, params)
        except Exception as exc:  # noqa: BLE001 - one bad account must not blank the fleet
            # Only when the caller left the choice open. An account named
            # explicitly and then not read is a failed request, and answering
            # it with `ok: true` and an empty list says the opposite: that
            # there are no mentions, rather than that nobody looked.
            if params.account is not None:
                raise
            warnings.append(f"{label}: {type(exc).__name__}")
            continue
        rows.extend(sweep.rows)
        found += sweep.found
        mentions += sweep.mentions
        reactions += sweep.reactions
        hidden += sweep.hidden
        withheld += sweep.withheld
        scan_capped = scan_capped or sweep.scan_capped

    rows.sort(key=lambda row: (-row["unread_mentions"], -row["unread_reactions"]))
    rows = rows[: params.limit]

    if hidden:
        warnings.append(f"{hidden} private chat(s) omitted; enumeration of direct messages is off")
    if withheld:
        warnings.append(
            f"{withheld} chat(s) withheld: they have unread mentions or reactions, "
            "but the read policy does not permit opening them"
        )
        ctx.audit.refusal(
            action="mentions.list",
            actor=ctx.actor,
            reason=f"{withheld} chat(s) withheld by the read policy",
        )
    if scan_capped:
        # Said out loud, because the alternative is a short list that looks
        # complete: a mention sitting in dialog 1001 is invisible, and without
        # this the answer would claim nothing was cut.
        warnings.append(
            f"the dialog walk stopped at {MAX_DIALOG_SCAN} conversations; "
            "chats further down the list were not examined"
        )

    totals = {
        "chats": found,
        "mentions": mentions,
        "reactions": reactions,
        "accounts": len(labels),
    }

    return telegram_result(
        ctx,
        {"chats": rows, "totals": totals},
        returned=len(rows),
        total=found,
        # The scan ceiling truncates as surely as the row limit does, and the
        # envelope has one vocabulary for both.
        truncated=len(rows) < found or scan_capped,
        truncated_reason="limit",
        warnings=warnings,
    )


MENTIONS = REGISTRY.register(
    Operation(
        name="mentions.list",
        cli=("mentions",),
        mcp_tool="telegram_mentions",
        summary="Unread mentions and unread reactions — where this account was called out.",
        description=(
            "The messages behind telegram_inbox's mention and reaction counters: who "
            "mentioned or replied to this account and what they said, and who reacted to "
            "its own messages with which emoji. Telegram counts these separately from "
            "plain unread, which is what makes them a signal rather than volume. Reading "
            "them marks nothing as seen — the badge on the owner's own phone is "
            "unchanged."
        ),
        input_model=MentionsInput,
        effect=Effect.READ,
        capability=Capability.ENUMERATE,
        handler=handle_mentions,  # type: ignore[arg-type]
        tags=("read", "triage"),
    )
)
