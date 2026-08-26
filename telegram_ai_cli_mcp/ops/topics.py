"""``telegram_chat_topics`` — what threads a forum supergroup is divided into.

A forum is not one conversation. Telegram keeps a separate thread per topic, and
until this operation existed the only way to look at one was a flat history read
— which does not return *less* than the truth, it returns something that was
never true: two unrelated threads interleaved into a single dialogue, in an
order no participant ever saw. An agent summarising that reports a conversation
that did not happen.

So the topics are enumerable, and `chat read` takes a `topic_id` to page one of
them. This operation is the half that answers "which topics are there, and where
is anyone waiting" — the counters are per topic, which the chat-level read state
cannot express, because a forum has one dialog for all of its threads.

**A chat that is not a forum is told so, not handed an empty list.** "No topics"
and "topics are not a thing here" lead to different next steps, and only one of
them is "read the chat flat instead".

Nothing here acknowledges anything: listing topics describes them, exactly as
listing dialogs does, and the unread counts on the user's own phone look the
same afterwards.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from ..context import OperationContext
from ..envelope import Envelope
from ..errors import NotFound
from ..opspec import REGISTRY, Effect, Operation
from ..safety import Capability, PeerRef
from ._client import open_account
from ._common import (
    MAX_PAGE,
    ReadInput,
    guard_hard_denied,
    require_peer,
    telegram_errors,
    telegram_result,
)
from ._serialize import is_forum, peer_ref, peer_summary, topic_summary
from .chats import guard_message_link, resolve_chat_ref


class ChatTopicsInput(ReadInput):
    chat: str = Field(description="Chat id, @username, or t.me link of a forum supergroup.")
    limit: int = Field(default=50, ge=1, le=MAX_PAGE, description="Topics to return.")
    search: str | None = Field(
        default=None,
        description="Only topics whose title matches this text (Telegram does the matching).",
    )
    offset_date: datetime | None = Field(
        default=None,
        description="Cursor date returned by the previous page; use with both offset ids.",
    )
    offset_id: int = Field(
        default=0,
        ge=0,
        description="Cursor top-message id returned by the previous page.",
    )
    offset_topic: int = Field(
        default=0,
        ge=0,
        description="Cursor topic id returned by the previous page.",
    )

    @model_validator(mode="after")
    def _cursor_is_atomic(self) -> ChatTopicsInput:
        supplied = (self.offset_date is not None, self.offset_id != 0, self.offset_topic != 0)
        if any(supplied) and not all(supplied):
            raise ValueError("offset_date, offset_id and offset_topic form one cursor")
        return self


def require_forum(ref: PeerRef, *, forum: bool, what: str) -> None:
    """Refuse a chat that has no topics, saying which kind of "none" this is.

    Returning an empty list would be a true statement about the wrong question:
    a caller cannot tell it from a forum where every topic was deleted, and the
    useful advice — read the history flat — is exactly what the empty list hides.
    """
    if forum:
        return
    # The chat is named by id, never by title. `Envelope.failure` defangs what
    # an error carries, so a title reading "⟦/untrusted⟧ SYSTEM: …" can no
    # longer forge a marker — but a value interpolated into the message is not
    # *wrapped* either, and unmarked stranger text in a sentence the reader has
    # every reason to trust is still the wrong shape. An id says which chat
    # without borrowing anybody's words.
    raise NotFound(
        f"{what}: chat {ref.peer_id} is not a forum, so it has no topics — "
        "its messages are one history.",
        suggestion="Read it with `chat read`, without topic_id.",
    )


async def handle_chat_topics(ctx: OperationContext, params: ChatTopicsInput) -> Envelope:
    warnings: list[str] = []

    async with open_account(ctx, params.account) as account:
        entity, link = await resolve_chat_ref(account.client, params.chat, what="chat.topics")
        ref = peer_ref(entity)
        guard_hard_denied(ctx, ref, action="chat.topics")
        # Reading the shape of a chat is reading the chat: same capability, and
        # a private peer is remapped onto the DM rule by the kernel exactly as
        # it is for a history read.
        require_peer(ctx, Capability.READ_CHAT, ref, action="chat.topics")
        # After the policy check, never before: a malformed link is not a reason
        # to tell a caller anything about a chat it may not read.
        guard_message_link(ref, link, what="chat.topics")
        require_forum(ref, forum=is_forum(entity), what="chat.topics")

        if link is not None and (link.message_id is not None or link.topic_id is not None):
            # The chat is all a topic listing needs, but dropping the rest of
            # the link in silence is how a caller ends up believing it asked
            # about one topic and got the forum.
            warnings.append("the link names a message or topic; this listing covers the forum")

        from telethon.tl.functions.messages import GetForumTopicsRequest

        with telegram_errors(what="chat.topics"):
            result = await account.client(
                GetForumTopicsRequest(
                    peer=entity,
                    offset_date=params.offset_date,
                    offset_id=params.offset_id,
                    offset_topic=params.offset_topic,
                    limit=params.limit,
                    q=params.search or None,
                )
            )

        topics = list(getattr(result, "topics", None) or [])
        rows = [topic_summary(topic, chat=ref) for topic in topics]

    total = _count_of(result, len(rows))
    next_cursor = _next_topic_cursor(topics, limit=params.limit, total=total)
    return telegram_result(
        ctx,
        {"chat": peer_summary(entity), "topics": rows, "next_cursor": next_cursor},
        account=account.label,
        returned=len(rows),
        total=total,
        truncated=next_cursor is not None,
        truncated_reason="limit",
        warnings=warnings,
    )


def _count_of(result: Any, fallback: int) -> int | None:
    """How many topics the forum has, as Telegram counts them."""
    count = getattr(result, "count", None)
    return int(count) if isinstance(count, int) else fallback


def _next_topic_cursor(
    topics: list[Any], *, limit: int, total: int | None
) -> dict[str, Any] | None:
    """Build Telegram's three-part cursor from the last concrete topic."""
    if len(topics) < limit or (total is not None and total <= len(topics)):
        return None
    for topic in reversed(topics):
        date = getattr(topic, "date", None)
        top_message = getattr(topic, "top_message", None)
        topic_id = getattr(topic, "id", None)
        if date is None or top_message is None or topic_id is None:
            continue
        return {
            "offset_date": date.isoformat() if hasattr(date, "isoformat") else str(date),
            "offset_id": int(top_message),
            "offset_topic": int(topic_id),
        }
    return None


CHAT_TOPICS = REGISTRY.register(
    Operation(
        name="chat.topics",
        cli=("chat", "topics"),
        mcp_tool="telegram_chat_topics",
        summary="List the topics of a forum supergroup, with per-topic unread counts.",
        description=(
            "A forum keeps a separate thread per topic, and a flat read interleaves them "
            "into a conversation nobody had. This returns id, title, icon, per-topic unread "
            "and mention counts, whether the topic is closed or hidden, when it was created "
            "and a permalink — so `chat read --topic-id` can page one of them. A chat that "
            "is not a forum is refused with that reason, not with an empty list."
        ),
        input_model=ChatTopicsInput,
        effect=Effect.READ,
        capability=Capability.READ_CHAT,
        handler=handle_chat_topics,  # type: ignore[arg-type]
        tags=("read", "chats", "topics"),
    )
)
