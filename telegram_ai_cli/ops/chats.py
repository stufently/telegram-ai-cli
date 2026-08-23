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

**A link keeps its number.** A ``t.me`` URL used to be resolved as a chat and
nothing else, so the message id in ``t.me/example/4231`` was dropped on the
floor — which turns "look at this message" into "look at this chat", silently
and at the wrong message. Resolution now returns what the link addressed, and
the operations that act on a single message accept it in place of an id.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ..context import OperationContext
from ..envelope import Envelope
from ..errors import InvalidInput, NotFound
from ..links import TelegramLink, parse_telegram_link
from ..opspec import REGISTRY, Effect, Operation
from ..safety import Capability, PeerKind, PeerRef
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
    ReadPointers,
    dialog_summary,
    message_summary,
    participant_summary,
    peer_ref,
    peer_summary,
)

#: Said out loud instead of leaving `read_by_peer` null with no explanation.
NO_PEER_RECEIPTS = (
    "Telegram maintains a read pointer for the other side only in one-to-one "
    "chats; elsewhere it is per reader, behind a separate privacy-controlled "
    "request this tool does not make"
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
    chat: str = Field(
        description=(
            "Chat id, @username, or t.me link. A link to a specific message "
            "(t.me/name/123, t.me/c/123/456, or a topic link) starts the page at "
            "that message instead of at the newest one."
        )
    )
    limit: int = Field(default=30, ge=1, le=MAX_PAGE, description="Messages to return.")
    before_id: int | None = Field(
        default=None,
        ge=1,
        description="Return messages older than this id — the way to page backwards.",
    )
    search: str | None = Field(default=None, description="Only messages containing this text.")
    include_read_state: bool = Field(
        default=True,
        description=(
            "Report which messages have been read, using the chat's read pointers. "
            "Costs one extra call and marks nothing as read; turn it off to skip it."
        ),
    )


class ChatMembersInput(ReadInput):
    chat: str = Field(description="Chat id, @username, or t.me link.")
    limit: int = Field(default=100, ge=1, le=MAX_PAGE, description="Members to return.")
    search: str | None = Field(default=None, description="Filter members by name or @username.")
    admins_only: bool = Field(default=False, description="Return only administrators.")


async def resolve_chat_ref(client: Any, chat: str, *, what: str) -> tuple[Any, TelegramLink | None]:
    """Resolve what the caller typed, keeping whatever else the link said.

    Policy is never decided on the string. A username is reassignable, so a
    rule written against one would follow whoever holds the handle today; the
    entity carries the numeric id that the kernel actually checks.

    The link comes back alongside the entity rather than being consumed here,
    because only the calling operation knows what to do with a message id: one
    that acts on a single message uses it, one that reads history anchors on
    it, and neither may quietly ignore it.
    """
    text = (chat or "").strip()
    link = parse_telegram_link(text)

    reference: str | int
    if link is not None:
        # `t.me/c/<internal>` is a chat with no public handle, so the numeric
        # form is the only thing that can resolve it.
        reference = link.chat
    elif text.lstrip("-").isdigit():
        reference = int(text)
    else:
        reference = chat

    with telegram_errors(what=what):
        entity = await client.get_entity(reference)
    if entity is None:
        raise NotFound(f"{what}: {chat!r} did not resolve to a chat")
    return entity, link


async def resolve_chat(client: Any, chat: str, *, what: str) -> Any:
    """The entity alone, for operations that address a chat and not a message."""
    entity, _ = await resolve_chat_ref(client, chat, what=what)
    return entity


def guard_message_link(ref: PeerRef, link: TelegramLink | None, *, what: str) -> None:
    """Refuse a link whose message number does not mean what it looks like.

    Two shapes, both of which would otherwise act on a message nobody named:

    **A message link into a private chat.** ``t.me/someone/123`` is a
    well-formed URL, and if ``someone`` is a person rather than a chat it opens
    their profile — it addresses no message. Treating the ``123`` as a message
    id would read message 123 of that conversation, which the link never
    pointed at. The output side already refuses to *produce* such a link
    (see ``_serialize.message_link``); this is the same rule on the way in.

    **A comment link.** ``t.me/channel/123?comment=456`` addresses message 456
    in the channel's *discussion group* — a different chat from the one in the
    path. Resolving the discussion peer is not implemented, and silently
    acting on channel post 123 instead is the wrong message in the wrong chat.
    """
    if link is None:
        return
    if link.comment_id is not None:
        raise InvalidInput(
            f"{what}: a ?comment= link points at a message in the channel's discussion "
            "group, which this tool does not resolve. Open the comment in Telegram and "
            "copy its own link, which names that group directly.",
        )
    if link.message_id is not None and ref.is_private:
        raise InvalidInput(
            f"{what}: {link.chat!r} is a one-to-one conversation, and a t.me link cannot "
            f"address a message in one — the {link.message_id} in that link is not a "
            "message id there. Pass the chat and message_id separately if that is what "
            "was meant.",
        )


def message_id_from(explicit: int | None, link: TelegramLink | None, *, what: str) -> int:
    """Decide which message an operation acts on, refusing to guess.

    A link and an explicit id that disagree is a caller error worth reporting:
    silently preferring one of them means acting on a message the caller did
    not name, which for a fetch is a wasted call and for anything else is worse.
    """
    from_link = link.message_id if link is not None else None
    if explicit is not None and from_link is not None and explicit != from_link:
        raise InvalidInput(
            f"{what}: the link names message {from_link} but message_id says {explicit}; "
            "pass one of them, not both"
        )
    chosen = explicit if explicit is not None else from_link
    if chosen is None:
        raise InvalidInput(f"{what}: pass message_id, or a t.me link that names the message itself")
    return chosen


async def read_state_of(
    client: Any, entity: Any, ref: PeerRef, *, what: str
) -> tuple[ReadPointers, dict[str, Any]]:
    """Where this chat's read pointers stand — and nothing is marked read.

    ``GetPeerDialogs`` describes a dialog; it does not acknowledge anything, so
    the badge on the user's own phone looks the same afterwards. That property
    is the reason this is the call used rather than anything in the "open the
    chat" family.

    A failure degrades to ``known: false`` with the reason attached instead of
    failing the read: the history is the answer, and the read state is context.
    """
    from telethon.tl.functions.messages import GetPeerDialogsRequest

    try:
        with telegram_errors(what=what):
            result = await client(GetPeerDialogsRequest(peers=[entity]))
    except Exception as exc:  # noqa: BLE001 - context, never the answer itself
        return ReadPointers(peer_receipts=False), {
            "known": False,
            "reason": f"Telegram did not report this chat's read state ({type(exc).__name__})",
        }

    dialogs = getattr(result, "dialogs", None) or []
    if not dialogs:
        return ReadPointers(peer_receipts=False), {
            "known": False,
            "reason": "Telegram returned no dialog state for this chat",
        }

    dialog = dialogs[0]
    # Only a one-to-one chat has a single other side whose pointer means
    # anything. In a group "read by the peer" is a question about each member.
    receipts = ref.kind is PeerKind.USER
    pointers = ReadPointers(
        inbox_max_id=getattr(dialog, "read_inbox_max_id", None),
        outbox_max_id=getattr(dialog, "read_outbox_max_id", None),
        peer_receipts=receipts,
    )
    state: dict[str, Any] = {
        "known": True,
        "read_inbox_max_id": pointers.inbox_max_id,
        "read_outbox_max_id": pointers.outbox_max_id if receipts else None,
        "unread_count": getattr(dialog, "unread_count", None),
        "unread_mentions": getattr(dialog, "unread_mentions_count", None),
        "peer_receipts": receipts,
    }
    if not receipts:
        state["reason"] = NO_PEER_RECEIPTS
    return pointers, state


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
    warnings: list[str] = []
    extra: dict[str, Any] = {}

    async with open_account(ctx, params.account) as account:
        entity, link = await resolve_chat_ref(account.client, params.chat, what="chat.read")
        ref = peer_ref(entity)
        guard_hard_denied(ctx, ref, action="chat.read")
        require_peer(ctx, Capability.READ_CHAT, ref, action="chat.read")
        # After the policy check, never before: a malformed link is not a
        # reason to tell a caller anything about a chat it may not read.
        guard_message_link(ref, link, what="chat.read")

        anchor = link.message_id if link is not None else None
        if anchor is not None and params.before_id is not None:
            raise InvalidInput(
                f"chat.read: the link names message {anchor} and before_id says "
                f"{params.before_id}; both position the page, so pass one of them"
            )
        if anchor is not None:
            extra["anchor_message_id"] = anchor
        if link is not None and link.topic_id is not None:
            # Not applied as a filter: the page still covers the whole chat, and
            # a caller expecting only that topic deserves to be told.
            extra["topic_id"] = link.topic_id
            warnings.append(
                f"the link names topic {link.topic_id}; this page is not filtered to it"
            )

        pointers = ReadPointers(peer_receipts=False)
        read_state: dict[str, Any] = {
            "known": False,
            "reason": "not requested (include_read_state was false)",
        }
        if params.include_read_state:
            pointers, read_state = await read_state_of(
                account.client, entity, ref, what="chat.read"
            )

        with telegram_errors(what="chat.read"):
            # `max_id` is exclusive, which is what makes it a cursor: pass the
            # oldest id from the previous page to continue backwards without
            # re-reading or skipping a message. A message link anchors the page
            # *at* the message it names, so its id is offset by one.
            max_id = params.before_id or (anchor + 1 if anchor is not None else 0)
            messages = await account.client.get_messages(
                entity,
                limit=params.limit,
                max_id=max_id,
                search=params.search,
            )

        rows = [message_summary(message, chat=ref, read=pointers) for message in messages]

    total = getattr(messages, "total", None)
    oldest = rows[-1]["id"] if rows else None
    return telegram_result(
        ctx,
        {
            "chat": peer_summary(entity),
            "messages": rows,
            "read_state": read_state,
            # Handed back explicitly so paging does not depend on the caller
            # noticing that the list is ordered newest first.
            "next_before_id": oldest,
        },
        account=account.label,
        returned=len(rows),
        total=total,
        truncated=bool(total and total > len(rows)),
        truncated_reason="limit",
        warnings=warnings,
        extra=extra or None,
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
            "Fetches up to 500 messages with attachment metadata, reaction counts, a "
            "permalink per message and, where Telegram reports it, whether each one has "
            "been read. Page backwards with before_id; a t.me link to a message starts "
            "the page at that message. Reading never marks anything as seen."
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
