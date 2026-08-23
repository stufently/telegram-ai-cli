"""Marks left on a message somebody else wrote: reactions and pins.

Every other planned write in this project either speaks as the account (send,
reply) or refuses to touch a message it did not write (edit, delete). These
four are the opposite by design. A reaction is *for* other people's messages,
and pinning one is the entire point of pinning — so ``_require_own_message``,
the guard that keeps the message operations off other people's text, has no
place here.

That removes a safety net, and the review text is what replaces it. Three
things every summary in this module states, because without them "put a
reaction on a message" is not something anybody can approve:

**Exactly which mark, on exactly which message.** The emoji is named *and*
spelled out in codepoints — a look-alike, or one the terminal sanitizer had to
strip a joiner out of, must not read as the real one — and the message it lands
on is quoted, with a line saying whether this account or somebody else wrote it.

**Who finds out.** A pin is the loudest thing in this file: by default every
member of the chat gets a notification and the banner appears at the top of
their window. Unpinning is quiet but just as visible, and unpinning *somebody
else's* pin is an act against another person's decision. All of that is in the
summary rather than implied by the verb.

**What the mark replaces.** Telegram's reaction API takes the account's whole
list for that message, not a delta, so adding one silently discards the others
unless the caller asked to keep them. The plan records the reactions that were
there, the applier refuses if they moved, and the summary says which of the two
things is about to happen.

Addressing: a chat plus a ``message_id``, or a ``t.me/…/123`` permalink, which
is how a person actually hands a message over. The parsing is
:mod:`telegram_ai_cli.links` and the two guards are the read side's own, so a
link means the same thing to a plan as it does to a read.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, Field, field_validator, model_validator

from ..errors import InvalidInput
from ..links import TelegramLink, parse_telegram_link
from ..opspec import Operation
from ..plans import Plan
from ..render import quote_for_review
from ..safety import Capability, PeerRef
from ._common import require_peer
from ._serialize import media_summary, reactions_summary
from .chats import guard_message_link, message_id_from
from .write import (
    Resolved,
    WriteInput,
    _create,
    _fetch_message,
    _register,
    describe,
    message_snapshot,
    open_writer,
    peer_snapshot,
    require_planning_profile,
    resolve_peer,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import OperationContext

#: An emoji reaction is one grapheme in practice; the ceiling exists so that a
#: paragraph cannot be smuggled into the line a person approves.
MAX_EMOJI_CHARS = 16

#: A custom emoji is addressed by its document id — a 64-bit integer that
#: arrives as a string, because a JSON consumer parsing it as a double loses the
#: low bits, which are the identifier.
CUSTOM_EMOJI_ID = r"^[0-9]{1,20}$"

#: Telegram serialises a document id as a signed 64-bit integer. A larger number
#: matches the pattern above and then fails inside Telethon's own packing — by
#: which point the plan has been claimed, the rate-limit slot taken and the audit
#: attempt written, so the ceiling is enforced on the way in instead.
MAX_DOCUMENT_ID = 2**63 - 1

#: Message ids are 32-bit and sequential per chat; `links.py` refuses a larger
#: one in a permalink, and the explicit argument is held to the same bound so
#: the two ways of naming a message cannot disagree about what is addressable.
MAX_MESSAGE_ID = 2**31 - 1


# ---------------------------------------------------------------------------
# reactions as values
# ---------------------------------------------------------------------------
#
# A reaction is compared, recorded and re-checked, so it needs one canonical
# shape. It is the same shape the read side already publishes
# (`_serialize.reactions_summary`) minus the counts: the *kind* matters because
# Telegram has reaction types that carry no emoji at all, and collapsing them
# to their emoticon would make a paid star reaction and a blank one identical.


def reaction_of(row: dict[str, Any]) -> dict[str, Any]:
    """The identity half of a reaction row — what it *is*, not how many."""
    return {
        "kind": row["kind"],
        "emoji": row["emoji"],
        "custom_emoji_id": row["custom_emoji_id"],
    }


def _order(reaction: dict[str, Any]) -> tuple[str, str, str]:
    """A total order over reaction *values*, used only for comparing two sets.

    Never for deciding what to send: see :func:`chosen_reactions` for why the
    order a list goes out in is Telegram's, not this one's.
    """
    return (
        str(reaction["kind"]),
        str(reaction["emoji"] or ""),
        str(reaction["custom_emoji_id"] or ""),
    )


def same_reactions(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    """Whether two lists hold the same reactions, whatever order they are in.

    Drift is about *which* reactions this account has, not about their
    positions: Telegram re-orders ``results`` as counts move, and comparing the
    raw sequences would report a changed world every time somebody else reacted.
    """
    return sorted(left, key=_order) == sorted(right, key=_order)


def chosen_reactions(message: Any) -> list[dict[str, Any]]:
    """The reactions *this account* has on a message, in Telegram's own order.

    The order is not cosmetic. ``chosen_order`` is derived from the position a
    reaction held in the list that was sent, so re-sorting before sending
    silently re-orders the account's own reactions on that message.

    ``reactions_summary`` produces one row per entry of ``reactions.results``,
    in that order, which is what lets the order key be read off the matching
    entry — it is the one thing the row itself does not carry, and duplicating
    the rest of the serialisation to get it would be worse. Where the key is
    missing, position stands in for it.

    ``reactions_summary`` also distinguishes "no reactions block at all" from "a
    block with nobody in it"; that distinction does not survive here, and should
    not: both mean this account has left nothing.
    """
    rows = reactions_summary(message) or []
    results = list(getattr(getattr(message, "reactions", None), "results", None) or [])
    picked: list[tuple[int, dict[str, Any]]] = []
    for index, row in enumerate(rows):
        if not row["chosen"]:
            continue
        entry = results[index] if index < len(results) else None
        order = getattr(entry, "chosen_order", None)
        picked.append((int(order) if order is not None else index, reaction_of(row)))
    return [reaction for _, reaction in sorted(picked, key=lambda pair: pair[0])]


def media_fingerprint(message: Any) -> dict[str, Any] | None:
    """What is attached, identified — not merely "something is attached".

    A photo with no caption has an empty body, so the body digest the shared
    snapshot records is identical for every such message. Editing a media
    message keeps its id, which means a plan reviewed against one photo could be
    applied to another and nothing in the snapshot would notice. These four
    operations are the ones that act on other people's messages, so the id of
    the photo or document is recorded next to its type and compared again at
    apply time.
    """
    media = getattr(message, "media", None)
    if media is None:
        return None
    summary = media_summary(message) or {}
    inner = getattr(media, "photo", None) or getattr(media, "document", None)
    ident = getattr(inner, "id", None)
    return {
        "type": summary.get("type"),
        "id": str(ident) if ident is not None else None,
    }


def requested_reaction(*, emoji: str | None, custom_emoji_id: str | None) -> dict[str, Any]:
    """The reaction a caller asked for, in the shape everything else compares."""
    if custom_emoji_id is not None:
        return {"kind": "custom_emoji", "emoji": None, "custom_emoji_id": custom_emoji_id}
    return {"kind": "emoji", "emoji": emoji, "custom_emoji_id": None}


def describe_reaction(reaction: dict[str, Any]) -> str:
    """Name a reaction unambiguously for the text a person approves.

    The codepoints are spelled out next to the emoji on purpose. Two emoji can
    render identically in a terminal font, the sanitizer strips the joiners out
    of a multi-part sequence before it is printed, and "react with 👍" is a
    sentence somebody is about to authorise — so the summary carries the one
    form that cannot be misread.
    """
    if reaction["kind"] == "custom_emoji":
        return f"custom emoji {reaction['custom_emoji_id']}"
    emoji = reaction["emoji"] or ""
    if not emoji:
        # `ReactionPaid` and `ReactionEmpty` carry no emoticon at all. Rendering
        # them as a blank would make a star reaction indistinguishable from
        # nothing, in the one text somebody is about to authorise.
        return f"a {reaction['kind']} reaction, which carries no emoji"
    points = " ".join(f"U+{ord(ch):04X}" for ch in emoji)
    return f"{emoji} ({points})"


def describe_reactions(reactions: list[dict[str, Any]]) -> str:
    return ", ".join(describe_reaction(one) for one in reactions) or "(none)"


#: The reaction types this tool can send. Telegram also has ``ReactionPaid``
#: (star reactions, which cost money) and ``ReactionEmpty``, and neither can be
#: reproduced from a plan.
SENDABLE_KINDS = frozenset({"emoji", "custom_emoji"})


def guard_sendable(reactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Refuse to build a list this tool cannot actually send.

    Telegram's call replaces the account's whole list for a message, so keeping
    one reaction while adding another means re-sending the first — and a paid
    star reaction re-sent is a purchase, not a copy. Where the account has left
    something outside :data:`SENDABLE_KINDS`, the honest answer is that this
    operation cannot express the change, not a list with a hole in it.
    """
    unsendable = [one for one in reactions if one["kind"] not in SENDABLE_KINDS]
    if unsendable:
        raise InvalidInput(
            f"this account has left {describe_reactions(unsendable)} on that message. "
            "Telegram's reaction call replaces the whole list, so any change here would "
            "have to re-send that, which this tool will not do.",
            suggestion="Take that reaction off from a Telegram client first.",
        )
    return reactions


def final_reactions(
    existing: list[dict[str, Any]],
    wanted: dict[str, Any],
    *,
    keep_existing: bool,
) -> list[dict[str, Any]]:
    """The whole list this account would have afterwards.

    Telegram's ``messages.sendReaction`` takes a list, not a delta: whatever is
    sent becomes the account's complete set of reactions on that message. So
    "add one more" has to be computed from what is already there, and "react
    with this" has to be explicit about discarding the rest.

    Reacting with a reaction that is already there is refused rather than sent
    as a no-op. A plan still costs a person the reading of it, and one that
    changes nothing is the worst kind to put in a review queue.
    """
    if wanted in existing:
        raise InvalidInput(
            f"this account has already reacted with {describe_reaction(wanted)}",
            suggestion="Remove it with `message unreact`, or react with something else.",
        )
    if not keep_existing:
        return [wanted]
    # Appended, never sorted: the new reaction is the newest, and Telegram reads
    # the position it is sent in as its `chosen_order`.
    return guard_sendable([*existing, wanted])


def remaining_reactions(
    existing: list[dict[str, Any]],
    unwanted: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """What is left after taking one reaction away, or all of them.

    ``None`` means "everything this account left". Removing a reaction that was
    never there is refused for the same reason as the no-op above: the plan
    would describe an effect that cannot happen.
    """
    if not existing:
        raise InvalidInput(
            "this account did not react to that message, so there is nothing to remove"
        )
    if unwanted is None:
        return []
    if unwanted not in existing:
        raise InvalidInput(
            f"this account did not react with {describe_reaction(unwanted)}; "
            f"it left {describe_reactions(existing)}"
        )
    return guard_sendable([one for one in existing if one != unwanted])


# ---------------------------------------------------------------------------
# addressing one message
# ---------------------------------------------------------------------------


def _link_in(chat: int | str) -> TelegramLink | None:
    return parse_telegram_link(chat) if isinstance(chat, str) else None


async def resolve_chat_argument(
    client: Any, chat: int | str
) -> tuple[Resolved, TelegramLink | None]:
    """Resolve a chat that may have arrived as a ``t.me`` link.

    The link is returned rather than consumed, because only the caller knows
    what the number in it means; here it is a message id, and dropping it would
    act on whatever ``message_id`` happened to default to.
    """
    link = _link_in(chat)
    return await resolve_peer(client, link.chat if link is not None else chat), link


async def resolve_message(
    ctx: OperationContext,
    client: Any,
    *,
    chat: int | str,
    message_id: int | None,
    capability: Capability,
    action: str,
) -> tuple[Resolved, Any]:
    """Resolve the chat, check the policy, then decide and fetch the message.

    The order is the point, and it is the read side's order. Policy first: a
    malformed link is not a reason to tell a caller anything about a chat they
    may not touch. The link guards second, because ``t.me/someone/123`` into a
    one-to-one conversation addresses no message at all and acting on message
    123 there would be acting on something nobody named.
    """
    target, link = await resolve_chat_argument(client, chat)
    require_peer(ctx, capability, target.ref, action=action)
    guard_message_link(target.ref, link, what=action)
    chosen = message_id_from(message_id, link, what=action)
    return target, await _fetch_message(client, target, chosen)


def authorship(message: Any) -> str:
    return "sent by this account" if getattr(message, "out", False) else "sent by somebody else"


def quote_message(message: Any) -> str:
    """The message being marked, as a reviewer needs to see it."""
    body = getattr(message, "message", None) or ""
    if body:
        return quote_for_review(body, limit=500)
    fingerprint = media_fingerprint(message)
    if fingerprint is not None:
        return f"(no text — a {fingerprint['type']} attachment, id {fingerprint['id']})"
    return "(no text)"


def message_block(message: Any) -> str:
    return f"--- the message ({authorship(message)}) ---\n{quote_message(message)}"


# ---------------------------------------------------------------------------
# input models
# ---------------------------------------------------------------------------


class MessageMarkInput(WriteInput):
    """One message, addressed the way a person hands one over.

    Singular on purpose. A plan that reacted to or pinned a batch would put one
    approval in front of an unbounded number of visible acts, which is the
    blast radius the review step exists to bound.
    """

    chat: int | str = Field(
        description=(
            "Chat id, @username, or a t.me link. A link that names the message supplies "
            "message_id on its own."
        )
    )
    message_id: int | None = Field(
        default=None,
        gt=0,
        le=MAX_MESSAGE_ID,
        description="Id of the message. Omit when the chat argument is a link to it.",
    )


class _ReactionChoice(BaseModel):
    """Mixin holding the two ways to name a reaction. Never registered alone."""

    emoji: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_EMOJI_CHARS,
        description="A standard emoji reaction, e.g. 👍. Not for custom emoji.",
    )
    custom_emoji_id: str | None = Field(
        default=None,
        pattern=CUSTOM_EMOJI_ID,
        description=(
            "Document id of a custom emoji, as a string. Only chats that permit custom "
            "emoji reactions accept one; elsewhere Telegram refuses the whole request."
        ),
    )

    @field_validator("custom_emoji_id")
    @classmethod
    def _fits_a_document_id(cls, value: str | None) -> str | None:
        """A number the pattern accepts but Telethon cannot pack is refused here.

        ``struct.pack('<q', …)`` raises during serialisation, which happens after
        the plan is claimed, the slot reserved and the audit attempt written —
        the one place an input error must not be discovered.
        """
        if value is None:
            return value
        if not 1 <= int(value) <= MAX_DOCUMENT_ID:
            raise ValueError(
                f"custom_emoji_id must be between 1 and {MAX_DOCUMENT_ID}; "
                "Telegram addresses a document by a signed 64-bit id"
            )
        return value

    @field_validator("emoji")
    @classmethod
    def _printable(cls, value: str | None) -> str | None:
        """Refuse an "emoji" that could redraw the line describing it.

        The plan summary is rendered to a terminal, and the renderer strips
        control characters — but stripping them here would leave the caller
        thinking they asked for something they did not. Refusing says so.
        """
        if value is None:
            return value
        if any(unicodedata.category(ch) in {"Cc", "Cs"} for ch in value):
            raise ValueError("an emoji cannot contain control characters")
        if value.strip() != value or any(ch.isspace() for ch in value):
            raise ValueError("an emoji cannot contain whitespace")
        return value


class ReactMessageInput(MessageMarkInput, _ReactionChoice):
    keep_existing: bool = Field(
        default=False,
        description=(
            "Keep the reactions this account already left on that message and add this "
            "one alongside them. Only chats that allow several reactions per person "
            "accept it. The default replaces them."
        ),
    )
    big: bool = Field(
        default=False,
        description="Send it as the bigger, longer animation everyone in the chat sees.",
    )

    @model_validator(mode="after")
    def _one_reaction(self) -> ReactMessageInput:
        if (self.emoji is None) == (self.custom_emoji_id is None):
            raise ValueError("name exactly one of emoji or custom_emoji_id")
        return self


class UnreactMessageInput(MessageMarkInput, _ReactionChoice):
    @model_validator(mode="after")
    def _at_most_one(self) -> UnreactMessageInput:
        if self.emoji is not None and self.custom_emoji_id is not None:
            raise ValueError("name exactly one of emoji or custom_emoji_id, or neither")
        return self


class PinMessageInput(MessageMarkInput):
    silent: bool = Field(
        default=False,
        description=(
            "Pin without notifying the members. The banner still appears for everyone; "
            "only the notification is suppressed."
        ),
    )
    both_sides: bool = Field(
        default=False,
        description=(
            "In a one-to-one conversation, pin for the other person too. The default "
            "pins only on this account's side. Meaningless anywhere else, and refused "
            "there rather than ignored."
        ),
    )


class UnpinMessageInput(MessageMarkInput):
    """Unpinning takes no `silent`: Telegram never announces one."""


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------


def guard_both_sides(ref: PeerRef, both_sides: bool, *, what: str) -> None:
    """`both_sides` is a fact about one-to-one chats and nothing else.

    Accepting it in a group and quietly dropping it would let a plan promise
    something the apply cannot do — and this is the one flag whose whole purpose
    is to say who else sees the pin.
    """
    if both_sides and not ref.is_private:
        raise InvalidInput(
            f"{what}: both_sides only means something in a one-to-one conversation; "
            "in a group or channel a pin is always for everyone",
        )


def guard_pin_state(message: Any, *, pinned: bool, what: str) -> None:
    """Refuse a pin that changes nothing, and an unpin of what is not pinned.

    Both are no-ops that read like work. Telegram would accept the first and
    re-notify the whole chat for nothing; the second it would accept silently.
    Neither belongs in a queue somebody has to read through.
    """
    is_pinned = bool(getattr(message, "pinned", False))
    if pinned and is_pinned:
        raise InvalidInput(
            f"{what}: message {getattr(message, 'id', '?')} is already pinned in that chat"
        )
    if not pinned and not is_pinned:
        raise InvalidInput(
            f"{what}: message {getattr(message, 'id', '?')} is not pinned, so there is "
            "nothing to unpin"
        )


# ---------------------------------------------------------------------------
# planners
# ---------------------------------------------------------------------------


async def plan_react_message(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(ReactMessageInput, params)
    require_planning_profile(ctx, Capability.SEND, action="message.react")
    wanted = requested_reaction(emoji=p.emoji, custom_emoji_id=p.custom_emoji_id)

    async with open_writer(ctx, p.account) as (label, client):
        target, message = await resolve_message(
            ctx,
            client,
            chat=p.chat,
            message_id=p.message_id,
            capability=Capability.SEND,
            action="message.react",
        )

    existing = chosen_reactions(message)
    # Computing the result is also what proves the plan would do something: it
    # refuses a reaction that is already there, and one this tool cannot re-send.
    final = final_reactions(existing, wanted, keep_existing=p.keep_existing)

    if not existing:
        change = "This account has left no other reaction on that message."
    elif p.keep_existing:
        change = (
            f"It is added alongside {describe_reactions(existing)}, leaving this account "
            f"with {describe_reactions(final)} there. Only a chat that allows several "
            "reactions per person accepts this; where it does not, Telegram refuses the "
            "whole request and nothing changes."
        )
    else:
        change = f"It replaces the reaction this account left: {describe_reactions(existing)}."

    loudness = (
        "It plays as the bigger, longer animation for everyone in the chat."
        if p.big
        else "The reaction is visible to everyone in the chat and notifies the author."
    )
    summary = (
        f"React with {describe_reaction(wanted)} as {label} to message {message.id} "
        f"in {describe(target)}\n"
        f"{message_block(message)}\n"
        f"{loudness}\n{change}"
    )
    return await _create(
        ctx,
        operation="message.react",
        account=label,
        params=p,
        preconditions={
            "peer": peer_snapshot(target),
            "message": message_snapshot(message),
            "media": media_fingerprint(message),
            # What this account had reacted with when the plan was written. The
            # applier refuses if it moved, because the reaction API takes the
            # whole list and a stale one would silently discard something.
            "existing": existing,
        },
        summary=summary,
    )


async def plan_unreact_message(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(UnreactMessageInput, params)
    require_planning_profile(ctx, Capability.SEND, action="message.unreact")
    unwanted = (
        requested_reaction(emoji=p.emoji, custom_emoji_id=p.custom_emoji_id)
        if (p.emoji is not None or p.custom_emoji_id is not None)
        else None
    )

    async with open_writer(ctx, p.account) as (label, client):
        target, message = await resolve_message(
            ctx,
            client,
            chat=p.chat,
            message_id=p.message_id,
            capability=Capability.SEND,
            action="message.unreact",
        )

    existing = chosen_reactions(message)
    remaining = remaining_reactions(existing, unwanted)

    taken = (
        f"every reaction this account left ({describe_reactions(existing)})"
        if unwanted is None
        else describe_reaction(unwanted)
    )
    summary = (
        f"Remove {taken} as {label} from message {message.id} in {describe(target)}\n"
        f"{message_block(message)}\n"
        f"This account is left with {describe_reactions(remaining)} on that message. "
        "The count the author sees drops; nobody is notified."
    )
    return await _create(
        ctx,
        operation="message.unreact",
        account=label,
        params=p,
        preconditions={
            "peer": peer_snapshot(target),
            "message": message_snapshot(message),
            "media": media_fingerprint(message),
            "existing": existing,
            "remaining": remaining,
        },
        summary=summary,
    )


async def plan_pin_message(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(PinMessageInput, params)
    require_planning_profile(ctx, Capability.ADMIN, action="message.pin")

    async with open_writer(ctx, p.account) as (label, client):
        target, message = await resolve_message(
            ctx,
            client,
            chat=p.chat,
            message_id=p.message_id,
            capability=Capability.ADMIN,
            action="message.pin",
        )
    guard_both_sides(target.ref, p.both_sides, what="message.pin")
    guard_pin_state(message, pinned=True, what="message.pin")

    if target.ref.is_private:
        reach = (
            "It is pinned for both sides: the banner appears at the top of the other "
            "person's conversation too."
            if p.both_sides
            else "It is pinned only on this side; the other person's conversation is unchanged."
        )
    else:
        reach = "The banner appears at the top of the chat for every member."
    noise = (
        "No notification is sent."
        if p.silent
        else "Every member of the chat gets a pin notification."
    )
    summary = (
        f"Pin message {message.id} as {label} in {describe(target)}\n"
        f"{message_block(message)}\n"
        f"{noise} {reach}"
    )
    return await _create(
        ctx,
        operation="message.pin",
        account=label,
        params=p,
        preconditions={
            "peer": peer_snapshot(target),
            "message": message_snapshot(message),
            "media": media_fingerprint(message),
            # Recorded so the applier can tell "still unpinned" from "somebody
            # pinned it in the meantime", and so `plan show` states the reach of
            # the act without re-deriving it from the flags.
            "pinned": False,
            "silent": p.silent,
            "both_sides": p.both_sides,
        },
        summary=summary,
    )


async def plan_unpin_message(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(UnpinMessageInput, params)
    require_planning_profile(ctx, Capability.ADMIN, action="message.unpin")

    async with open_writer(ctx, p.account) as (label, client):
        target, message = await resolve_message(
            ctx,
            client,
            chat=p.chat,
            message_id=p.message_id,
            capability=Capability.ADMIN,
            action="message.unpin",
        )
    guard_pin_state(message, pinned=False, what="message.unpin")

    whose = (
        "This undoes somebody else's decision to pin it"
        if not getattr(message, "out", False)
        else "This undoes a pin of this account's own message"
    )
    reach = (
        # Unpinning is never one-sided: a banner left at the top of the other
        # person's window is not what "unpinned" says.
        "It is unpinned for both sides of the conversation."
        if target.ref.is_private
        else "The banner disappears for every member of the chat."
    )
    summary = (
        f"Unpin message {message.id} as {label} in {describe(target)}\n"
        f"{message_block(message)}\n"
        f"{reach} {whose}; Telegram does not announce an unpin, and does not say who did it."
    )
    return await _create(
        ctx,
        operation="message.unpin",
        account=label,
        params=p,
        preconditions={
            "peer": peer_snapshot(target),
            "message": message_snapshot(message),
            "media": media_fingerprint(message),
            "pinned": True,
        },
        summary=summary,
    )


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
#
# `_register` is write.py's, deliberately: it is the one place that spells out
# why a remote write has no direct MCP tool, and a second copy of that reasoning
# is a second place for it to rot.

REACT_MESSAGE: Operation = _register(
    name="message.react",
    cli=("message", "react"),
    plan_tool="telegram_plan_react_message",
    summary="Plan putting a reaction on a message.",
    description=(
        "Records the intent to react. Nothing is sent until a person applies the plan. "
        "The reaction is visible to everyone in the chat and notifies the author. "
        "Telegram takes the account's whole reaction list for a message, so this "
        "replaces whatever it had reacted with unless keep_existing is set — and "
        "keeping several works only in chats that allow several."
    ),
    input_model=ReactMessageInput,
    capability=Capability.SEND,
    planner=plan_react_message,
)

UNREACT_MESSAGE: Operation = _register(
    name="message.unreact",
    cli=("message", "unreact"),
    plan_tool="telegram_plan_unreact_message",
    summary="Plan taking this account's reaction off a message.",
    description=(
        "Removes one reaction this account left, or all of them when neither emoji nor "
        "custom_emoji_id is named. Removing one it never left is refused rather than "
        "planned as a no-op."
    ),
    input_model=UnreactMessageInput,
    capability=Capability.SEND,
    planner=plan_unreact_message,
)

PIN_MESSAGE: Operation = _register(
    name="message.pin",
    cli=("message", "pin"),
    plan_tool="telegram_plan_pin_message",
    summary="Plan pinning a message in a chat.",
    description=(
        "Pinning is the loudest thing this tool does short of sending: by default every "
        "member gets a notification and the banner appears at the top of their window. "
        "`silent` suppresses the notification, not the banner. In a one-to-one chat it "
        "pins on this side only unless both_sides is set. Judged by the admin policy, "
        "because it changes what everyone in the chat sees."
    ),
    input_model=PinMessageInput,
    capability=Capability.ADMIN,
    planner=plan_pin_message,
)

UNPIN_MESSAGE: Operation = _register(
    name="message.unpin",
    cli=("message", "unpin"),
    plan_tool="telegram_plan_unpin_message",
    summary="Plan unpinning a message in a chat.",
    description=(
        "Removes the pinned banner for everyone who could see it, including when it was "
        "somebody else who pinned it — which the plan summary says explicitly. Telegram "
        "does not announce an unpin."
    ),
    input_model=UnpinMessageInput,
    capability=Capability.ADMIN,
    planner=plan_unpin_message,
)

#: The marks, in registration order. Kept next to the operations for the same
#: reason `write.WRITE_OPERATIONS` is: the applier dispatches on names, and a
#: list nobody maintains is worse than no list.
MARK_OPERATIONS: tuple[Operation, ...] = (
    REACT_MESSAGE,
    UNREACT_MESSAGE,
    PIN_MESSAGE,
    UNPIN_MESSAGE,
)
