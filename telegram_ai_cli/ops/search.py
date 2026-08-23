"""``telegram_search`` — find messages, in one chat or across the account.

Two modes, and the difference matters for policy.

Scoped to a chat, the search is an ordinary read: the chat is resolved, the
read policy is consulted for that peer, and the results come back like any
history page.

Unscoped, the search runs across everything the account can see, which means
results arrive from chats that were never named. Those are filtered afterwards
against the same read policy, peer by peer, and the count of what was withheld
is reported. Filtering after the fact is not a weaker check — it is the only
possible order, because Telegram decides which chats to search — but it does
mean a global search reveals *how many* results policy hid, and that is the
honest trade: an unexplained short list is worse than a stated one.

**A match can bring its neighbours.** One line out of a conversation rarely
says what the conversation was about, and the fallback — paging the chat again
around every hit — costs a second operation and a lot of output. ``context``
asks for the messages either side of each match. They come back in the same
list, ordered like history, with ``match: false`` on them: a neighbour that
looked like a hit would inflate every count a caller makes from the result.

Three things keep that from becoming a chat download. The radius is capped
(:data:`MAX_CONTEXT`), only the first :data:`MAX_CONTEXT_HITS` matches are
given context and the rest are said so in a warning, and overlapping windows
collapse — a message next to two hits is fetched twice and emitted once.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ..context import OperationContext
from ..envelope import Envelope
from ..errors import InvalidInput
from ..opspec import REGISTRY, Effect, Operation
from ..safety import Capability
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
from .chats import guard_message_link, resolve_chat_ref

#: How far either side of a match ``context`` may reach. Small on purpose: the
#: neighbours are there to explain a hit, and a caller that wants the thread
#: has ``chat read``, which pages properly and costs one call per page.
MAX_CONTEXT = 5

#: How many matches may carry context in one search. Every enriched hit costs
#: two more history calls, so an unbounded version of this turns a 500-hit
#: search into a thousand requests and a copy of the chat. Ten rather than a
#: rounder number because Telethon's own documentation puts a flood wait at
#: roughly ten history requests in quick succession, and nothing here paces
#: them: a search that trips one fails as a whole, having done the work twice.
MAX_CONTEXT_HITS = 10


class SearchInput(ReadInput):
    query: str = Field(min_length=1, description="Text to look for.")
    chat: str | None = Field(
        default=None,
        description="Restrict to one chat. Omit to search everything this account can see.",
    )
    limit: int = Field(
        default=30,
        ge=1,
        le=MAX_PAGE,
        description=(
            "Matching messages to return. `context` adds rows on top of this, so "
            "`meta.returned` can exceed it; `meta.matches` is what this caps."
        ),
    )
    before_id: int | None = Field(
        default=None,
        ge=1,
        description="Only messages older than this id (single-chat searches only).",
    )
    context: int = Field(
        default=0,
        ge=0,
        le=MAX_CONTEXT,
        description=(
            "Messages to include either side of each match, marked `match: false` "
            "so they are not counted as hits. Single-chat searches only; the first "
            f"{MAX_CONTEXT_HITS} matches get context and a warning names the rest. "
            "0, the default, returns matches alone."
        ),
    )


def _row(message: Any, ref: Any, *, match: bool) -> dict[str, Any]:
    """One message, flagged as a hit or as the context around one.

    The flag is set on every search result, with or without ``context``, so a
    caller writes one parser rather than branching on whether it asked for
    neighbours.
    """
    summary = message_summary(message, chat=ref)
    summary["match"] = match
    return summary


async def _context_rows(
    client: Any,
    entity: Any,
    ref: Any,
    hits: list[Any],
    radius: int,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """The messages around each hit, each one emitted at most once.

    Both directions are anchored on the hit with ``offset_id``, which Telegram
    treats as exclusive; ``reverse`` is what makes the second call mean "the
    next few *after* this message" rather than "the newest few in the chat",
    and the difference is the whole feature — the latter returns the end of the
    conversation for every match in it.

    ``seen`` starts as the hit ids so a match is never re-emitted as its own
    neighbour's context, and grows as rows are added so two hits that share a
    window produce one row, not two.
    """
    enriched = hits[:MAX_CONTEXT_HITS]
    if len(hits) > len(enriched):
        warnings.append(
            f"context was added to the first {MAX_CONTEXT_HITS} of {len(hits)} matches; "
            "narrow the search or lower limit to get it for the rest"
        )

    seen = {getattr(hit, "id", None) for hit in hits}
    rows: list[dict[str, Any]] = []

    with telegram_errors(what="search"):
        for hit in enriched:
            hit_id = getattr(hit, "id", None)
            if not isinstance(hit_id, int):
                continue
            older = await client.get_messages(entity, limit=radius, offset_id=hit_id)
            newer = await client.get_messages(entity, limit=radius, offset_id=hit_id, reverse=True)
            for message in (*older, *newer):
                message_id = getattr(message, "id", None)
                if message_id in seen:
                    continue
                seen.add(message_id)
                rows.append(_row(message, ref, match=False))
    return rows


async def _search_one_chat(
    ctx: OperationContext, account: Any, params: SearchInput, warnings: list[str]
) -> tuple[dict[str, Any], list[dict[str, Any]], int | None, int]:
    entity, link = await resolve_chat_ref(account.client, params.chat or "", what="search")
    ref = peer_ref(entity)
    guard_hard_denied(ctx, ref, action="search")
    require_peer(ctx, Capability.READ_CHAT, ref, action="search")
    guard_message_link(ref, link, what="search")

    if link is not None and link.message_id is not None:
        # The chat is what a search needs, but dropping the rest of the link in
        # silence is how a caller ends up believing it searched one message.
        warnings.append(
            f"the link names message {link.message_id}; the search covers the whole chat"
        )

    with telegram_errors(what="search"):
        messages = await account.client.get_messages(
            entity,
            limit=params.limit,
            search=params.query,
            max_id=params.before_id or 0,
        )
    hits = list(messages)
    rows = [_row(message, ref, match=True) for message in hits]
    if params.context and hits:
        rows.extend(
            await _context_rows(account.client, entity, ref, hits, params.context, warnings)
        )
        # History order, newest first, so a hit and the lines around it read as
        # the conversation did. Appending context after the matches instead
        # would leave the caller to reassemble the thread by id anyway.
        rows.sort(key=lambda row: row["id"] or 0, reverse=True)
    return peer_summary(entity), rows, getattr(messages, "total", None), len(hits)


async def _search_everywhere(
    ctx: OperationContext, account: Any, params: SearchInput
) -> tuple[list[dict[str, Any]], int]:
    """Global search, with every result re-checked against the read policy."""
    require_enumeration(ctx, private=False, action="search")

    rows: list[dict[str, Any]] = []
    withheld = 0

    with telegram_errors(what="search"):
        # Telethon treats `entity=None` as a global search. It is iterated
        # rather than fetched in one call so that the scan stops as soon as
        # `limit` permitted results have been collected — policy filtering
        # happens here, so "how many did Telegram return" is not the same
        # number as "how many may be shown".
        async for message in account.client.iter_messages(
            None, search=params.query, limit=params.limit * 4
        ):
            chat = getattr(message, "chat", None)
            if chat is None:
                withheld += 1
                continue
            ref = peer_ref(chat)
            if hard_denied(ref) or not ctx.safety.check(Capability.READ_CHAT, ref).allowed:
                withheld += 1
                continue

            # A global search returns messages from chats the caller never
            # named, so the permalink is the only cheap way back to any of them.
            row = _row(message, ref, match=True)
            row["chat"] = peer_summary(chat)
            rows.append(row)
            if len(rows) >= params.limit:
                break

    if withheld:
        ctx.audit.refusal(
            action="search",
            actor=ctx.actor,
            reason=f"{withheld} global search result(s) withheld by the read policy",
        )
    return rows, withheld


async def handle_search(ctx: OperationContext, params: SearchInput) -> Envelope:
    warnings: list[str] = []

    if params.context and not params.chat:
        # Every hit of a global search can be in a different chat, so context
        # would mean paging each of them — a read of chats the caller never
        # named, at a cost that grows with the number of results. Refusing is
        # better than quietly returning matches with no context: a caller that
        # asked for it would believe the neighbours were simply absent.
        raise InvalidInput(
            "search: context needs a chat. A global search returns matches from chats "
            "it never named, and reading around each of them is a separate page per chat",
            suggestion="Pass chat=… to search one chat with context, or drop context.",
        )

    async with open_account(ctx, params.account) as account:
        if params.chat:
            chat, rows, total, matched = await _search_one_chat(ctx, account, params, warnings)
            data: dict[str, Any] = {"chat": chat, "messages": rows}
        else:
            rows, withheld = await _search_everywhere(ctx, account, params)
            total = None
            matched = len(rows)
            data = {"messages": rows}
            if withheld:
                warnings.append(
                    f"{withheld} result(s) withheld: their chats are not readable "
                    "under the current policy"
                )

    # Truncation is a statement about *matches*: context rows are extra output,
    # and counting them would report a page as full because its hits brought
    # neighbours along. Where Telegram reported how many messages match in
    # total, that is the answer; the page-is-full guess is for when it did not,
    # and using it anyway calls a complete result truncated whenever the last
    # page happens to be exactly `limit` long.
    truncated = total > matched if total is not None else matched >= params.limit

    extra = (
        {
            "matches": matched,
            "context_messages": len(rows) - matched,
            "context_radius": params.context,
        }
        if params.context
        else None
    )

    return telegram_result(
        ctx,
        data,
        account=account.label,
        returned=len(rows),
        total=total,
        truncated=truncated,
        truncated_reason="limit",
        warnings=warnings,
        extra=extra,
    )


SEARCH = REGISTRY.register(
    Operation(
        name="search.messages",
        cli=("search",),
        mcp_tool="telegram_search",
        summary="Search messages in one chat, or across everything the account sees.",
        description=(
            "Scoped to a chat, the read policy is checked once for that chat, and "
            "`context` may bring back the messages either side of each match, marked "
            "`match: false`. Unscoped, "
            "every result is checked against the policy for its own chat and the number "
            "withheld is reported."
        ),
        input_model=SearchInput,
        effect=Effect.READ,
        capability=Capability.READ_CHAT,
        handler=handle_search,  # type: ignore[arg-type]
        tags=("read", "messages"),
    )
)
