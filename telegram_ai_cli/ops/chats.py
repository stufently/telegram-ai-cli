"""Chat operations: what dialogs exist, what was said, and who is in them.

Three things worth stating before the code, because each one is a decision
rather than an implementation detail.

**Nothing here acknowledges a message.** Reading is expected to be invisible:
an operator who lists a chat's history must not thereby clear the unread badge
on a colleague's phone, and an agent must never be able to hide the fact that
something arrived. Every call below fetches history, and none of the fetching
APIs used report anything back to Telegram about what was seen.

**Enumeration is gated separately from reading.** Listing dialogs reveals which
conversations exist, which is reconnaissance even when none of them is opened.
It has its own switch, and private chats stay out of it until a second switch
is turned on.

**A private chat is judged as a private chat.** The kernel remaps a chat read
of a one-to-one conversation onto the DM policy, so ``chat read`` cannot be the
way around an empty ``dms`` allowlist. The same is true of the member listing.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ..context import OperationContext
from ..envelope import Envelope
from ..errors import NotFound
from ..opspec import REGISTRY, Effect, Operation
from ..safety import Capability, PeerKind
from ._client import open_account
from ._common import (
    MAX_DIALOG_SCAN,
    MAX_PAGE,
    ReadInput,
    guard_hard_denied,
    hard_denied,
    require_enumeration,
    require_peer,
    telegram_errors,
    telegram_result,
)
from ._serialize import (
    dialog_summary,
    message_summary,
    participant_summary,
    peer_ref,
    peer_summary,
)


class ChatsInput(ReadInput):
    limit: int = Field(default=50, ge=1, le=MAX_PAGE, description="Rows to return.")
    search: str | None = Field(
        default=None,
        description="Case-insensitive substring of the chat title or @username.",
    )
    include_private: bool = Field(
        default=False,
        description="Include one-to-one conversations (requires safety.read.enumerate_dms).",
    )
    archived: bool = Field(default=False, description="List the archive instead of the main list.")


class ChatReadInput(ReadInput):
    chat: str = Field(description="Chat id, @username, or t.me link.")
    limit: int = Field(default=30, ge=1, le=MAX_PAGE, description="Messages to return.")
    before_id: int | None = Field(
        default=None,
        ge=1,
        description="Return messages older than this id — the way to page backwards.",
    )
    search: str | None = Field(default=None, description="Only messages containing this text.")


class ChatMembersInput(ReadInput):
    chat: str = Field(description="Chat id, @username, or t.me link.")
    limit: int = Field(default=100, ge=1, le=MAX_PAGE, description="Members to return.")
    search: str | None = Field(default=None, description="Filter members by name or @username.")
    admins_only: bool = Field(default=False, description="Return only administrators.")


async def resolve_chat(client: Any, chat: str, *, what: str) -> Any:
    """Turn what the caller typed into a resolved Telegram entity.

    Policy is never decided on the string. A username is reassignable, so a
    rule written against one would follow whoever holds the handle today; the
    entity carries the numeric id that the kernel actually checks.
    """
    reference: str | int = chat
    text = chat.strip()
    if text.lstrip("-").isdigit():
        reference = int(text)
    with telegram_errors(what=what):
        entity = await client.get_entity(reference)
    if entity is None:
        raise NotFound(f"{what}: {chat!r} did not resolve to a chat")
    return entity


# --- chats ------------------------------------------------------------------


async def handle_chats(ctx: OperationContext, params: ChatsInput) -> Envelope:
    require_enumeration(ctx, private=params.include_private, action="chats.list")

    needle = params.search.lower().strip() if params.search else None
    rows: list[dict[str, Any]] = []
    scanned = 0
    matched = 0
    hidden = 0

    async with open_account(ctx, params.account) as account:
        with telegram_errors(what="chats.list"):
            async for dialog in account.client.iter_dialogs(
                archived=params.archived,
                # Telethon can be asked to ignore the pinned/folder ordering
                # rules; leaving them alone keeps the list in the order the
                # user sees on their own device.
                ignore_migrated=True,
            ):
                scanned += 1
                entity = getattr(dialog, "entity", None)
                if entity is None:
                    continue

                ref = peer_ref(entity)
                # The hard floor has no capability to consult during a listing,
                # so it is applied per row. These chats are never enumerated,
                # not even as a title.
                if hard_denied(ref):
                    continue
                if ref.is_private and not params.include_private:
                    hidden += 1
                    continue

                row = dialog_summary(dialog)
                if needle:
                    haystack = f"{row.get('title') or ''} {row.get('username') or ''}".lower()
                    if needle not in haystack:
                        continue

                matched += 1
                if len(rows) < params.limit:
                    rows.append(row)
                if scanned >= MAX_DIALOG_SCAN:
                    break

    warnings: list[str] = []
    if hidden:
        warnings.append(f"{hidden} private chat(s) omitted; enumeration of direct messages is off")
    truncated = matched > len(rows) or scanned >= MAX_DIALOG_SCAN
    return telegram_result(
        ctx,
        {"chats": rows},
        account=account.label,
        returned=len(rows),
        total=matched,
        truncated=truncated,
        truncated_reason="limit",
        warnings=warnings,
        extra={"scanned": scanned} if scanned >= MAX_DIALOG_SCAN else None,
    )


# --- chat read --------------------------------------------------------------


async def handle_chat_read(ctx: OperationContext, params: ChatReadInput) -> Envelope:
    async with open_account(ctx, params.account) as account:
        entity = await resolve_chat(account.client, params.chat, what="chat.read")
        ref = peer_ref(entity)
        guard_hard_denied(ctx, ref, action="chat.read")
        require_peer(ctx, Capability.READ_CHAT, ref, action="chat.read")

        with telegram_errors(what="chat.read"):
            # `max_id` is exclusive, which is what makes it a cursor: pass the
            # oldest id from the previous page to continue backwards without
            # re-reading or skipping a message.
            messages = await account.client.get_messages(
                entity,
                limit=params.limit,
                max_id=params.before_id or 0,
                search=params.search,
            )

        rows = [message_summary(message) for message in messages]

    total = getattr(messages, "total", None)
    oldest = rows[-1]["id"] if rows else None
    return telegram_result(
        ctx,
        {
            "chat": peer_summary(entity),
            "messages": rows,
            # Handed back explicitly so paging does not depend on the caller
            # noticing that the list is ordered newest first.
            "next_before_id": oldest,
        },
        account=account.label,
        returned=len(rows),
        total=total,
        truncated=bool(total and total > len(rows)),
        truncated_reason="limit",
    )


# --- chat members -----------------------------------------------------------


async def handle_chat_members(ctx: OperationContext, params: ChatMembersInput) -> Envelope:
    async with open_account(ctx, params.account) as account:
        entity = await resolve_chat(account.client, params.chat, what="chat.members")
        ref = peer_ref(entity)
        guard_hard_denied(ctx, ref, action="chat.members")
        if ref.kind is PeerKind.USER:
            raise NotFound("chat.members: a one-to-one conversation has no member list")
        require_peer(ctx, Capability.READ_MEMBERS, ref, action="chat.members")

        kwargs: dict[str, Any] = {"limit": params.limit, "search": params.search}
        if params.admins_only:
            from telethon.tl.types import ChannelParticipantsAdmins

            # `filter` and `search` are mutually exclusive in the API; the
            # admin filter wins, and the search is applied afterwards.
            kwargs["filter"] = ChannelParticipantsAdmins()
            kwargs["search"] = None

        with telegram_errors(what="chat.members"):
            participants = await account.client.get_participants(entity, **kwargs)

        rows = [participant_summary(user) for user in participants]

    if params.admins_only and params.search:
        needle = params.search.lower()
        rows = [
            row
            for row in rows
            if needle in f"{row.get('title') or ''} {row.get('username') or ''}".lower()
        ]

    total = getattr(participants, "total", None)
    return telegram_result(
        ctx,
        {"chat": peer_summary(entity), "members": rows},
        account=account.label,
        returned=len(rows),
        total=total,
        truncated=bool(total and total > len(rows)),
        truncated_reason="limit",
    )


CHATS = REGISTRY.register(
    Operation(
        name="chats.list",
        cli=("chats",),
        mcp_tool="telegram_chats",
        summary="List dialogs, or find a chat_id by title.",
        description=(
            "Returns the account's dialogs with unread counts, so a title can be turned "
            "into the chat_id every other operation needs. Private conversations are "
            "excluded unless safety.read.enumerate_dms permits them."
        ),
        input_model=ChatsInput,
        effect=Effect.READ,
        capability=Capability.ENUMERATE,
        handler=handle_chats,  # type: ignore[arg-type]
        tags=("read", "chats"),
    )
)

CHAT_READ = REGISTRY.register(
    Operation(
        name="chat.read",
        cli=("chat", "read"),
        mcp_tool="telegram_chat_read",
        summary="Read a chat's history, newest first.",
        description=(
            "Fetches up to 500 messages with attachment metadata. Page backwards with "
            "before_id. Reading never marks anything as seen."
        ),
        input_model=ChatReadInput,
        effect=Effect.READ,
        capability=Capability.READ_CHAT,
        handler=handle_chat_read,  # type: ignore[arg-type]
        tags=("read", "messages"),
    )
)

CHAT_MEMBERS = REGISTRY.register(
    Operation(
        name="chat.members",
        cli=("chat", "members"),
        mcp_tool="telegram_chat_members",
        summary="List the members of a group or channel, with their roles.",
        input_model=ChatMembersInput,
        effect=Effect.READ,
        capability=Capability.READ_MEMBERS,
        handler=handle_chat_members,  # type: ignore[arg-type]
        tags=("read", "members"),
    )
)
