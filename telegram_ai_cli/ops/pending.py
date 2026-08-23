"""Messages that exist but have not been sent: drafts and scheduled sends.

Both answer a question the message operations cannot: *what is about to go out,
or what did I start and abandon?* Neither is visible in a chat's history —
Telegram keeps them in separate places — so a tool that reads history alone
reports a chat as quiet while a message sits in it waiting for six o'clock.

**They are read-only, and that is a decision rather than an omission.** Clearing
a draft or cancelling a scheduled send would be a remote write, and this project
records those as plans a person applies. Neither exists here in any form: the
handlers below fetch, and the calls that would change either state appear
nowhere in this module, by name or otherwise.

**A draft is the most private thing in an account.** It is what somebody started
writing and decided not to send, so the listing is gated exactly like a dialog
listing — enumeration first, then the hard floor per row, then the read policy
of the chat the draft belongs to. Saved Messages and Telegram Service
Notifications are not merely refused, they are not counted: a "1 draft
withheld" tally would still say a draft exists there, which is the fact worth
hiding.

**Scheduled messages are per chat, because Telegram has no global list.** Drafts
do have one (a single call returns every draft), and scheduled sends do not —
they are fetched from one chat's scheduled history at a time. So the two
operations take different arguments, and the asymmetry is Telegram's rather
than a design choice worth hiding behind an optional parameter that silently
returns nothing.
"""

from __future__ import annotations

from enum import StrEnum, auto
from typing import Any

from pydantic import Field

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
    iso,
    require_enumeration,
    require_peer,
    telegram_errors,
    telegram_result,
)
from ._serialize import MESSAGE_TEXT_LIMIT, message_summary, peer_ref, peer_summary
from .chats import guard_message_link, resolve_chat_ref

#: Telegram's sentinel for "send this the moment they come online". It is a
#: real ``schedule_date`` on the wire, and rendered as a date it reads as 19
#: January 2038 — a lie about a message that may go out in a minute.
WHEN_ONLINE = 0x7FFFFFFE


class Visibility(StrEnum):
    """Why one draft is in the listing and another is not.

    Four outcomes rather than a boolean, because the caller has to report three
    of them differently: a chat closed in code is never mentioned at all, a
    private chat left out by ``include_private`` is counted as hidden, and a
    chat the read policy refuses is counted as withheld. Collapsing them makes
    a short list unexplainable.
    """

    SHOW = auto()
    #: The hard floor. Not listed, not counted, not hinted at.
    CLOSED = auto()
    #: A one-to-one conversation, and the caller did not ask for those.
    PRIVATE = auto()
    #: The read policy refuses this chat. Counted, so the list is explicable.
    WITHHELD = auto()


class DraftsInput(ReadInput):
    limit: int = Field(default=50, ge=1, le=MAX_PAGE, description="Drafts to return.")
    include_private: bool = Field(
        default=False,
        description="Include drafts in one-to-one conversations "
        "(requires safety.read.enumerate_dms and the chat's own DM allowlist entry).",
    )


class ScheduledInput(ReadInput):
    chat: str = Field(description="Chat id, @username, or t.me link.")
    limit: int = Field(default=50, ge=1, le=MAX_PAGE, description="Messages to return.")


def draft_visibility(kernel: SafetyKernel, chat: PeerRef, *, include_private: bool) -> Visibility:
    """Decide one row, in the order the kernel documents.

    Pure, and separate from the fetch, because this is the part worth testing
    exhaustively: a listing walks every dialog the account has, so a mistake
    here leaks whatever the account happens to contain rather than one chat the
    caller named.
    """
    if hard_denied(chat):
        return Visibility.CLOSED
    if chat.is_private and not include_private:
        return Visibility.PRIVATE
    # `check` rather than `require`: a listing filters, and the kernel remaps a
    # private peer onto the DM policy on its own, so an unlisted DM is refused
    # here even when `include_private` was asked for.
    if not kernel.check(Capability.READ_CHAT, chat).allowed:
        return Visibility.WITHHELD
    return Visibility.SHOW


def draft_summary(draft: Any, *, chat: PeerRef) -> dict[str, Any]:
    """One unsent draft, flattened.

    ``chat`` is a :class:`~telegram_ai_cli.safety.PeerRef` rather than the
    Telethon entity for the same reason ``message_summary`` takes one: the
    caller has already resolved it to decide policy, and a plain dataclass keeps
    this function testable on a machine without Telethon.
    """
    raw = getattr(draft, "raw_text", None) or getattr(draft, "text", None) or ""
    text = str(raw)
    return {
        "chat": {
            "id": chat.peer_id,
            "kind": str(chat.kind),
            "username": chat.username,
            "title": chat.title,
        },
        "text": text[:MESSAGE_TEXT_LIMIT] or None,
        "text_truncated": len(text) > MESSAGE_TEXT_LIMIT,
        # When the draft was last touched — which is how "abandoned in March"
        # is told apart from "typing right now".
        "saved_at": iso(getattr(draft, "date", None)),
        "reply_to_msg_id": getattr(draft, "reply_to_msg_id", None),
        "link_preview": bool(getattr(draft, "link_preview", False)),
    }


def scheduled_summary(message: Any, *, chat: PeerRef) -> dict[str, Any]:
    """One scheduled message, in the same shape as any other message.

    Two fields are added and one is removed on purpose. ``scheduled_for`` names
    what Telegram stores in ``date`` for a scheduled message — the *intended*
    send time, not a send that happened — and ``send_when_online`` covers the
    sentinel, which is a date only in the sense that any large integer is.

    The permalink goes: a scheduled message's id belongs to a sequence of its
    own, so a ``t.me`` link built from it addresses a *different* message in
    the same chat. Producing that is worse than producing nothing.
    """
    row = message_summary(message, chat=chat)
    date = getattr(message, "date", None)
    when_online = date is not None and int(date.timestamp()) == WHEN_ONLINE

    row["scheduled_for"] = None if when_online else row["date"]
    row["send_when_online"] = bool(when_online)
    row["link"] = None
    return row


async def handle_drafts(ctx: OperationContext, params: DraftsInput) -> Envelope:
    require_enumeration(ctx, private=params.include_private, action="drafts.list")

    rows: list[dict[str, Any]] = []
    hidden = 0
    withheld = 0

    async with open_account(ctx, params.account) as account:
        with telegram_errors(what="drafts.list"):
            # One call returns every draft the account has; nothing is opened
            # and nothing is marked read by asking for them.
            async for item in account.client.iter_drafts():
                entity = getattr(item, "entity", None)
                if entity is None:
                    # No entity means no id, and no id means no policy decision.
                    # Treated exactly like a chat closed in code — dropped, and
                    # not counted either: nothing here can show that an
                    # unidentified peer is *not* Saved Messages, so a tally
                    # would say a draft exists there (raised by review). The
                    # branch is defensive: Telethon builds a draft from the
                    # peers the same response carried, so it does not arise in
                    # practice.
                    continue

                ref = peer_ref(entity)
                verdict = draft_visibility(ctx.safety, ref, include_private=params.include_private)
                if verdict is Visibility.CLOSED:
                    continue
                if verdict is Visibility.PRIVATE:
                    hidden += 1
                    continue
                if verdict is Visibility.WITHHELD:
                    withheld += 1
                    continue

                row = draft_summary(item, chat=ref)
                if row["text"] is None:
                    # Telegram keeps an empty draft object around after one is
                    # cleared. It is not a draft; listing it would report work
                    # that does not exist.
                    continue
                rows.append(row)

    # Newest first, like every other listing in this project.
    rows.sort(key=lambda row: row["saved_at"] or "", reverse=True)
    shown = rows[: params.limit]

    warnings: list[str] = []
    if hidden:
        warnings.append(
            f"{hidden} draft(s) in private chats omitted; "
            "pass include_private and allowlist the chat to see them"
        )
    if withheld:
        ctx.audit.refusal(
            action="drafts.list",
            actor=ctx.actor,
            reason=f"{withheld} draft(s) withheld by the read policy",
        )
        warnings.append(
            f"{withheld} draft(s) withheld: their chats are not readable under the current policy"
        )

    return telegram_result(
        ctx,
        {"drafts": shown},
        account=account.label,
        returned=len(shown),
        total=len(rows),
        truncated=len(shown) < len(rows),
        truncated_reason="limit",
        warnings=warnings,
    )


async def handle_scheduled(ctx: OperationContext, params: ScheduledInput) -> Envelope:
    warnings: list[str] = []

    async with open_account(ctx, params.account) as account:
        entity, link = await resolve_chat_ref(account.client, params.chat, what="scheduled.list")
        ref = peer_ref(entity)
        guard_hard_denied(ctx, ref, action="scheduled.list")
        require_peer(ctx, Capability.READ_CHAT, ref, action="scheduled.list")
        # After the policy check, never before: a malformed link is not a reason
        # to tell a caller anything about a chat it may not read.
        guard_message_link(ref, link, what="scheduled.list")
        if link is not None and link.message_id is not None:
            warnings.append(
                f"the link names message {link.message_id}; scheduled messages have ids "
                "of their own, so the whole scheduled queue of that chat is returned"
            )

        with telegram_errors(what="scheduled.list"):
            # Telethon turns `scheduled=True` into a request that reads the
            # scheduled history. Nothing here sends one early or removes one.
            messages = await account.client.get_messages(entity, limit=params.limit, scheduled=True)

        rows = [scheduled_summary(message, chat=ref) for message in messages]

    # Soonest first, with the "when they come online" ones last: they have no
    # time to sort by, and they are the ones with no deadline attached.
    rows.sort(key=lambda row: (row["send_when_online"], row["scheduled_for"] or ""))

    # Telethon reports how many the chat has; `len(rows)` is how many were
    # returned. Reporting the second as both, and calling the page truncated
    # whenever it filled `limit`, produced `returned == total` with
    # `truncated: true` — a queue of exactly `limit` messages read as cut when
    # it was complete (raised by review).
    total = getattr(messages, "total", None)
    return telegram_result(
        ctx,
        {"chat": peer_summary(entity), "scheduled": rows},
        account=account.label,
        returned=len(rows),
        total=total,
        truncated=bool(total and total > len(rows)),
        truncated_reason="limit",
        warnings=warnings,
    )


DRAFTS = REGISTRY.register(
    Operation(
        name="drafts.list",
        cli=("drafts",),
        mcp_tool="telegram_drafts",
        summary="Unsent drafts, across every chat this account may be read for.",
        description=(
            "What was started and never sent, newest first, with the chat each draft "
            "belongs to. Gated like a dialog listing: enumeration, then the hard floor "
            "per row, then the read policy of the draft's own chat — a draft in Saved "
            "Messages or Service Notifications is never listed or counted. Reading a "
            "draft does not clear it; nothing here can."
        ),
        input_model=DraftsInput,
        effect=Effect.READ,
        capability=Capability.ENUMERATE,
        handler=handle_drafts,  # type: ignore[arg-type]
        tags=("read", "messages"),
    )
)

SCHEDULED = REGISTRY.register(
    Operation(
        name="scheduled.list",
        cli=("scheduled",),
        mcp_tool="telegram_scheduled",
        summary="Messages queued to be sent to one chat later.",
        description=(
            "The chat's scheduled queue, soonest first, with the time each message is "
            "due — or a flag where Telegram will send it as soon as the other person "
            "comes online. One chat at a time, because Telegram keeps no global list. "
            "Cancelling or sending one early is not part of this project."
        ),
        input_model=ScheduledInput,
        effect=Effect.READ,
        capability=Capability.READ_CHAT,
        handler=handle_scheduled,  # type: ignore[arg-type]
        tags=("read", "messages"),
    )
)
