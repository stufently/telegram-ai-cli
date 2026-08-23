"""``telegram_message_reactions`` — what a single message collected.

A reaction is the cheapest signal a chat produces. "Did anyone acknowledge the
announcement" is answered by a count, not by reading two hundred replies, and
that answer costs one call instead of a page of history in the context window.
Chat reads carry the same counts per message; this operation exists for the
case where the message is already known — usually because somebody pasted its
link — and the whole history around it is not wanted.

Two things it deliberately does not do.

**It does not ask who reacted.** ``messages.getMessageReactionsList`` exists and
would name every person; Telegram gates that by the reader's own privacy
settings, and pulling the list turns a count into a roster of individuals.
Whatever Telegram already attached to the message (``recent_reactions``, a
handful of peer ids on small chats) is reported as-is, because it arrived with
the message and fetching it again would be the extra step. Nothing else is
requested, and when Telegram says the full list is unavailable the payload says
so rather than leaving a silent gap.

**It does not react.** Adding a reaction is a remote write, and remote writes
are planned and applied by a person. Effect is ``READ``, and the capability is
the same ``read_chat`` that reading the message itself needs — a reaction count
is chat content, and no cheaper door into a chat exists here than the one the
policy already guards.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ..context import OperationContext
from ..envelope import Envelope
from ..errors import NotFound
from ..opspec import REGISTRY, Effect, Operation
from ..safety import Capability
from ._client import open_account
from ._common import (
    ReadInput,
    guard_hard_denied,
    require_peer,
    telegram_errors,
    telegram_result,
)
from ._serialize import (
    message_link,
    peer_ref,
    peer_summary,
    reactions_summary,
    recent_reactors,
    topic_id_of,
)
from .chats import guard_message_link, message_id_from, resolve_chat_ref

#: Why the roster may be missing, stated in the payload instead of omitted.
_LIST_UNAVAILABLE = (
    "Telegram only reveals who reacted where the chat and the reader's privacy "
    "settings permit it; this operation reports the names it was given with the "
    "message and never requests the full list"
)


class MessageReactionsInput(ReadInput):
    chat: str = Field(
        description=(
            "Chat id, @username, or t.me link. A link that names the message "
            "supplies message_id on its own."
        )
    )
    message_id: int | None = Field(
        default=None,
        ge=1,
        description="Id of the message. Omit when the chat argument is a link to it.",
    )


async def handle_message_reactions(
    ctx: OperationContext, params: MessageReactionsInput
) -> Envelope:
    async with open_account(ctx, params.account) as account:
        entity, link = await resolve_chat_ref(account.client, params.chat, what="message.reactions")
        ref = peer_ref(entity)
        guard_hard_denied(ctx, ref, action="message.reactions")
        require_peer(ctx, Capability.READ_CHAT, ref, action="message.reactions")
        guard_message_link(ref, link, what="message.reactions")

        message_id = message_id_from(params.message_id, link, what="message.reactions")

        with telegram_errors(what="message.reactions"):
            message = await account.client.get_messages(entity, ids=message_id)
        if message is None:
            raise NotFound(f"message {message_id} is not in that chat")

        rows = reactions_summary(message)
        reactions = getattr(message, "reactions", None)
        can_see_list = bool(getattr(reactions, "can_see_list", False))
        data: dict[str, Any] = {
            "chat": peer_summary(entity),
            "message_id": message_id,
            "link": message_link(ref, message_id, topic_id_of(message)),
            "reactions": rows,
            "total": sum(row["count"] for row in rows) if rows else 0,
            "reactor_list": {
                "available": can_see_list,
                "reason": None if can_see_list else _LIST_UNAVAILABLE,
            },
            "recent": recent_reactors(reactions),
        }

    return telegram_result(
        ctx,
        data,
        account=account.label,
        returned=len(rows or []),
        total=len(rows or []),
    )


MESSAGE_REACTIONS = REGISTRY.register(
    Operation(
        name="message.reactions",
        cli=("message", "reactions"),
        mcp_tool="telegram_message_reactions",
        summary="Reaction counts on one message.",
        description=(
            "Returns the per-emoji counts on a single message, plus whichever recent "
            "reactors Telegram already attached to it. The full list of who reacted is "
            "never requested — where it is unavailable the payload says so. Reading "
            "reactions marks nothing as seen and adds no reaction."
        ),
        input_model=MessageReactionsInput,
        effect=Effect.READ,
        capability=Capability.READ_CHAT,
        handler=handle_message_reactions,  # type: ignore[arg-type]
        tags=("read", "messages"),
    )
)
