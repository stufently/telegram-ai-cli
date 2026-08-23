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
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ..context import OperationContext
from ..envelope import Envelope
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


class SearchInput(ReadInput):
    query: str = Field(min_length=1, description="Text to look for.")
    chat: str | None = Field(
        default=None,
        description="Restrict to one chat. Omit to search everything this account can see.",
    )
    limit: int = Field(default=30, ge=1, le=MAX_PAGE, description="Messages to return.")
    before_id: int | None = Field(
        default=None,
        ge=1,
        description="Only messages older than this id (single-chat searches only).",
    )


async def _search_one_chat(
    ctx: OperationContext, account: Any, params: SearchInput, warnings: list[str]
) -> tuple[dict[str, Any], list[dict[str, Any]], int | None]:
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
    rows = [message_summary(message, chat=ref) for message in messages]
    return peer_summary(entity), rows, getattr(messages, "total", None)


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
            row = message_summary(message, chat=ref)
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

    async with open_account(ctx, params.account) as account:
        if params.chat:
            chat, rows, total = await _search_one_chat(ctx, account, params, warnings)
            data: dict[str, Any] = {"chat": chat, "messages": rows}
        else:
            rows, withheld = await _search_everywhere(ctx, account, params)
            total = None
            data = {"messages": rows}
            if withheld:
                warnings.append(
                    f"{withheld} result(s) withheld: their chats are not readable "
                    "under the current policy"
                )

    return telegram_result(
        ctx,
        data,
        account=account.label,
        returned=len(rows),
        total=total,
        truncated=bool(total and total > len(rows)) or len(rows) >= params.limit,
        truncated_reason="limit",
        warnings=warnings,
    )


SEARCH = REGISTRY.register(
    Operation(
        name="search.messages",
        cli=("search",),
        mcp_tool="telegram_search",
        summary="Search messages in one chat, or across everything the account sees.",
        description=(
            "Scoped to a chat, the read policy is checked once for that chat. Unscoped, "
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
