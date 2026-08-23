"""Remote writes — recorded as plans, never performed here.

Every operation in this module produces a :class:`~telegram_ai_cli.plans.Plan`
and nothing else. No message leaves, no chat is joined, no profile changes. The
effect happens later, in :mod:`telegram_ai_cli.apply`, when a person runs
``tg-ai plan apply``.

Three properties are worth stating up front, because each one exists to close a
specific hole rather than to be tidy.

**One plan tool per operation.** The obvious design is a single
``plan_create(operation, params)``. It fails in practice: an untyped ``params``
bag publishes no schema for the fields, so a model calling it invents argument
names that look plausible and are wrong. One typed tool per operation costs a
schema entry each and buys correct calls.

**Targets are resolved to numbers now, and the numbers are recorded.** A
username is a lease, not an identity: it can be released and taken by somebody
else between the moment a plan is written and the moment it is applied. So the
planner resolves the handle, checks policy against the numeric id, and writes a
snapshot of what it saw into the plan. :mod:`telegram_ai_cli.apply` resolves the
same handle again and refuses if the answer moved.

**The summary is the approval surface.** It is the text a human reads before
authorising the send, so it is built from sanitised pieces — chat titles,
usernames and message bodies are all written by strangers, and a terminal will
happily let a ``\\r`` in a message body redraw the line describing it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..errors import (
    InvalidInput,
    NotAllowlisted,
    PeerUnresolved,
    ProfileForbidden,
)
from ..opspec import REGISTRY, Effect, Operation
from ..outbox import file_preview, resolve_outbound
from ..plans import Plan
from ..render import quote_for_review, sanitize_line
from ..safety import REMOTE_WRITE_CAPABILITIES, Capability, PeerKind, PeerRef
from ._common import require_peer, telegram_errors
from ._serialize import peer_ref

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import OperationContext

#: Telegram's own ceiling for a text message. Rejecting oversize input here
#: turns a server-side error into a validation message the caller can act on,
#: and keeps a doomed plan out of the review queue.
MAX_MESSAGE_CHARS = 4096

#: A single plan may not fan out into an unbounded number of deletions or
#: forwards. Reviewing "delete these 4000 messages" is not review.
MAX_MESSAGE_IDS = 100

#: Telegram's ceiling for the caption under a file, a quarter of what a plain
#: message may hold. Refusing here turns a server-side rejection — arriving
#: after the whole file has been uploaded — into a validation message.
MAX_CAPTION_CHARS = 1024

#: ``t.me/+HASH`` and the legacy ``t.me/joinchat/HASH``. Matched here rather
#: than passed to Telethon verbatim so the hash can be recorded as the policy
#: subject — a private invite has no numeric id until you are inside.
_INVITE_LINK = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(?:joinchat/|\+)([A-Za-z0-9_-]{6,})/?$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# helpers shared with the applier
# ---------------------------------------------------------------------------


def text_digest(text: str) -> str:
    """Fingerprint of a body, so a plan can record *what* without storing it.

    The body itself lives in the plan's encrypted params. Preconditions are
    stored in the clear, and a second copy of every message somebody planned to
    send is exactly the archive this project tries not to create.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Resolved:
    """A target as it looked when the plan was written.

    ``access_hash`` travels with the reference because Telegram addresses a
    peer by the pair, not by the id alone. ``invite_hash`` is set only for a
    private invite that has not been accepted yet, where no id exists.
    """

    ref: PeerRef
    access_hash: int | None = None
    invite_hash: str | None = None
    #: The literal string or id the caller supplied. Re-resolved at apply time,
    #: because resolving the *recorded id* would prove nothing: the question is
    #: whether the handle still points where it pointed.
    target: str | int | None = None
    #: This invite files a request an admin must approve, rather than joining.
    #: Recorded so the applier can report "requested" instead of "joined".
    request_needed: bool = False
    #: A channel or supergroup rather than a basic group. Only moderation cares:
    #: bans, per-member rights and restrictions exist in the first and not in the
    #: second, and a preview that did not know which it was would promise
    #: something Telegram cannot do.
    is_channel: bool = False


def peer_snapshot(resolved: Resolved) -> dict[str, Any]:
    """The half of a plan's preconditions that describes a peer."""
    ref = resolved.ref
    snapshot: dict[str, Any] = {
        "peer_id": ref.peer_id,
        "kind": str(ref.kind),
        "username": ref.username.lower() if ref.username else None,
        # The title is a hint for a human reading the plan, not an identity —
        # anyone can rename a group. Hashed so a mismatch is still detectable
        # without keeping the string around.
        "title_sha256": text_digest(ref.title) if ref.title else None,
        "access_hash": str(resolved.access_hash) if resolved.access_hash is not None else None,
    }
    if resolved.invite_hash:
        snapshot["invite_hash"] = resolved.invite_hash
        snapshot["request_needed"] = resolved.request_needed
    if resolved.target is not None:
        snapshot["target"] = resolved.target
    return snapshot


def message_snapshot(message: Any) -> dict[str, Any]:
    """What a message looked like when the plan was written.

    Editing and deleting are addressed by id, and ids are reused by nobody —
    but the *content* behind an id changes. Recording the digest lets the
    applier refuse to delete a message that is no longer the one reviewed.
    """
    body = getattr(message, "message", None) or ""
    return {
        "id": int(message.id),
        "outgoing": bool(getattr(message, "out", False)),
        "body_sha256": text_digest(body),
        "body_len": len(body),
        "has_media": getattr(message, "media", None) is not None,
    }


def describe(resolved: Resolved) -> str:
    """One safe line naming a peer, for the text a human approves."""
    ref = resolved.ref
    parts: list[str] = []
    if ref.title:
        parts.append(f'"{sanitize_line(ref.title, limit=80)}"')
    if ref.username:
        parts.append(f"@{sanitize_line(ref.username, limit=64)}")
    if resolved.invite_hash and not ref.peer_id:
        parts.append("private invite (no id until joined)")
    else:
        parts.append(f"id={ref.peer_id}")
    parts.append(f"kind={ref.kind}")
    return " ".join(parts)


def repeat_notice(params: RepeatableWriteInput) -> str:
    """The line telling a reviewer that they are approving a repeat.

    Empty for an ordinary plan, so the notice only ever appears where it means
    something. Shouted, because it is the one fact separating this approval from
    the identical one somebody has already given: the applier refuses a repeat by
    default, and this plan has asked it not to.
    """
    if not params.allow_duplicate:
        return ""
    return (
        "\nDELIBERATE REPEAT: allow_duplicate is set on this plan. If these exact words "
        "have already gone to this chat, they will be sent AGAIN rather than refused."
    )


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


def require_planning_profile(ctx: OperationContext, capability: Capability, *, action: str) -> None:
    """Refuse a write-shaped request before it touches the network.

    The kernel checks this too, and the kernel remains the authority. The point
    of the early copy is that resolving a username is itself an observable act:
    in the ``readonly`` profile a ``plan_*`` call should not reach Telegram at
    all, not even to look somebody up.
    """
    if capability not in REMOTE_WRITE_CAPABILITIES:
        return
    if ctx.settings.profile == "plan":
        return
    reason = (
        f"profile {ctx.settings.profile!r} does not permit planning {capability}; "
        "switch to the 'plan' profile to prepare it for review"
    )
    ctx.audit.refusal(action=action, actor=ctx.actor, reason=reason)
    raise ProfileForbidden(reason)


# ---------------------------------------------------------------------------
# the one place the write path touches the account fleet
# ---------------------------------------------------------------------------


@asynccontextmanager
async def open_writer(ctx: OperationContext, account: str | None) -> AsyncIterator[tuple[str, Any]]:
    """Borrow a connected client for a write, and say which account it is.

    Account selection, session locking and connection lifetime belong to
    :func:`telegram_ai_cli.ops._client.open_account`, which the reads already
    use; going through it means a plan and a read cannot disagree about which
    account "the account" is. This wrapper adds the one thing specific to
    writing: Telethon's automatic retries are switched off.
    """
    from ._client import open_account

    async with open_account(ctx, account) as opened:
        _disable_client_retries(ctx, opened.client)
        yield opened.label, opened.client


def _disable_client_retries(ctx: OperationContext, client: Any) -> None:
    """Take the library's automatic second attempt away.

    ``flood_sleep_threshold`` makes Telethon sleep through a flood wait and try
    again; ``request_retries`` makes it re-issue a request that did not answer.
    Both are reasonable defaults for a chat client and wrong for this one: the
    whole plan/apply design exists so that a second attempt is a human
    decision, and a library that quietly re-sends turns one message into two.
    ``_request_retries`` is private in Telethon, so it is set defensively
    rather than assumed to exist.
    """
    settings = ctx.settings
    if hasattr(client, "flood_sleep_threshold"):
        client.flood_sleep_threshold = settings.telethon_flood_sleep_threshold
    if hasattr(client, "_request_retries"):
        client._request_retries = settings.telethon_request_retries


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def reference(entity: Any, *, target: str | int | None = None) -> Resolved:
    """Turn a Telethon entity into the project's own peer reference.

    The classification is the reads' own :func:`_serialize.peer_ref`, so a
    supergroup is the same kind of thing to a plan as it is to a chat listing.
    Two rules deciding what a peer *is* would eventually disagree, and the
    disagreement would show up as a policy hole.
    """
    return Resolved(
        ref=peer_ref(entity),
        access_hash=getattr(entity, "access_hash", None),
        target=target,
        # Supergroup or basic group — a distinction the read policy deliberately
        # does not make (both are `group`) and moderation cannot avoid: a basic
        # group keeps no ban list and no per-member rights. Captured here, while
        # the entity is still in hand, so a plan can say which one it is on the
        # screen a person approves rather than discovering it at apply time.
        is_channel=hasattr(entity, "megagroup") or hasattr(entity, "broadcast"),
    )


async def resolve_peer(client: Any, target: str | int) -> Resolved:
    """Resolve a handle, id or link to a numeric peer.

    Deliberately not tolerant: an unresolvable target is an error, never a
    guess. Guessing here would mean planning an action against a chat nobody
    named.
    """
    with telegram_errors(what=f"resolving {sanitize_line(str(target), limit=80)}"):
        try:
            entity = await client.get_entity(target)
        except (ValueError, TypeError) as exc:
            raise PeerUnresolved(
                f"could not resolve {sanitize_line(str(target), limit=80)}: {exc}",
                suggestion=(
                    "Check the username or id; 'tg-ai whois' resolves without side effects."
                ),
            ) from None
    return reference(entity, target=target)


def invite_hash_of(target: str) -> str | None:
    """Extract the hash of a private invite, if that is what this is."""
    match = _INVITE_LINK.match(target.strip())
    return match.group(1) if match else None


async def resolve_join_target(client: Any, target: str) -> Resolved:
    """Resolve what a join would act on, without joining.

    A public ``@name`` resolves to a numeric peer like anything else. A private
    invite does not: until the invite is accepted there is no id to check a
    policy against. For that case the *invite hash* becomes the policy subject
    — which is honest, because the hash is exactly the thing the operator was
    handed and chose to allow. ``CheckChatInviteRequest`` is a read; importing
    the invite is the write, and that only happens at apply time.
    """
    from telethon.tl import functions

    hash_ = invite_hash_of(target)
    if hash_ is None:
        return await resolve_peer(client, target)

    try:
        info = await client(functions.messages.CheckChatInviteRequest(hash=hash_))
    except (ValueError, TypeError) as exc:
        raise PeerUnresolved(f"invite could not be checked: {exc}") from None

    chat = getattr(info, "chat", None)
    if chat is not None:
        # ChatInviteAlready / ChatInvitePeek: we can see the real chat, so the
        # numeric id is available and policy is checked against it.
        resolved = reference(chat, target=target)
        return Resolved(
            ref=resolved.ref,
            access_hash=resolved.access_hash,
            invite_hash=hash_,
            target=target,
        )

    title = getattr(info, "title", None)
    is_channel = bool(getattr(info, "channel", False)) and not getattr(info, "megagroup", False)
    ref = PeerRef(
        peer_id=0,
        kind=PeerKind.CHANNEL if is_channel else PeerKind.GROUP,
        # The hash stands in for a username so that the existing allow/deny
        # matcher can express "this invite is permitted" without inventing a
        # parallel rule type.
        username=hash_,
        title=str(title) if title else None,
    )
    return Resolved(
        ref=ref,
        invite_hash=hash_,
        target=target,
        # Applying such a plan files a join request an admin has to approve;
        # the applier reports "requested", not "joined".
        request_needed=bool(getattr(info, "request_needed", False)),
    )


# ---------------------------------------------------------------------------
# input models
# ---------------------------------------------------------------------------


class WriteInput(BaseModel):
    """Fields every planned write shares.

    ``extra="forbid"`` is load-bearing rather than pedantic. A caller that
    invents ``message`` where the field is ``text`` should be told so, not have
    the invented field silently dropped and an empty plan created.
    """

    model_config = ConfigDict(extra="forbid")

    account: str | None = Field(
        default=None,
        description="Account label to act as. Omit only when exactly one account is configured.",
    )


class RepeatableWriteInput(WriteInput):
    """A write that puts new words in a chat, and can therefore be a duplicate.

    ``allow_duplicate`` is the only way to send the same thing to the same peer
    twice inside the ledger's window, and it is a field on the plan rather than a
    flag on ``plan apply`` on purpose: the person approving is entitled to see
    that they are approving a repeat, and a switch typed at apply time is one
    nobody reviewed. It is never the default — a repeat is normally a re-run
    somebody forgot about — and it is deliberately not part of the fingerprint,
    so an approved repeat does not make every later copy invisible.
    """

    allow_duplicate: bool = Field(
        default=False,
        description=(
            "Send this even though an identical message has already gone to this chat "
            "recently. Off by default: the applier refuses a repeat, because the usual "
            "cause is a re-run nobody meant. Setting it says the repeat is intended, and "
            "the approval preview says so in as many words."
        ),
    )


class SendMessageInput(RepeatableWriteInput):
    chat: int | str = Field(description="Chat id, @username, or phone-free user id.")
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    silent: bool = Field(default=False, description="Deliver without a notification sound.")
    link_preview: bool = True


class ReplyMessageInput(RepeatableWriteInput):
    chat: int | str
    reply_to_message_id: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    silent: bool = False
    link_preview: bool = True


class SendFileInput(RepeatableWriteInput):
    """One file, one chat.

    A list of paths would let one approval upload a directory, and the review
    step exists to bound exactly that. The path is not a free choice either:
    :mod:`telegram_ai_cli.outbox` resolves it inside the configured outbox and
    refuses anything outside, so this operation cannot be turned into "post
    ``~/.ssh/id_ed25519`` into a group chat".
    """

    chat: int | str = Field(description="Chat id, @username, or user id.")
    path: str = Field(
        min_length=1,
        description=(
            "The file to send: a name relative to paths.uploads, or an absolute path "
            "inside it. Anything resolving outside the permitted directories is refused, "
            "symlinks included. One file per plan."
        ),
    )
    caption: str = Field(
        default="",
        max_length=MAX_CAPTION_CHARS,
        description="Text shown with the file. Telegram allows 1024 characters here.",
    )
    reply_to_message_id: int | None = Field(
        default=None,
        gt=0,
        description="Send it as a reply to this message.",
    )
    as_document: bool = Field(
        default=False,
        description=(
            "Send the bytes unchanged, as a document, whatever the file's type — rather "
            "than letting Telegram re-encode an image or video into its compressed form."
        ),
    )
    silent: bool = Field(default=False, description="Deliver without a notification sound.")


class EditMessageInput(WriteInput):
    chat: int | str
    message_id: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class DeleteMessageInput(WriteInput):
    chat: int | str
    message_ids: list[int] = Field(min_length=1, max_length=MAX_MESSAGE_IDS)
    revoke: bool = Field(
        default=True,
        description="Delete for everyone, not only for this account.",
    )


class ForwardMessageInput(RepeatableWriteInput):
    source_chat: int | str
    message_ids: list[int] = Field(min_length=1, max_length=MAX_MESSAGE_IDS)
    destination_chat: int | str
    silent: bool = False
    drop_author: bool = Field(
        default=False,
        description="Forward without attribution to the original sender.",
    )


class MarkReadInput(WriteInput):
    chat: int | str
    max_message_id: int | None = Field(
        default=None,
        gt=0,
        description="Mark everything up to this id. Omit to mark the whole chat read.",
    )


class JoinChatInput(WriteInput):
    target: str = Field(
        description="@username of a public chat, or a t.me/+HASH invite link.",
    )


class LeaveChatInput(WriteInput):
    chat: int | str


class CreateGroupInput(WriteInput):
    title: str = Field(min_length=1, max_length=128)
    about: str = Field(default="", max_length=255)
    kind: Literal["supergroup", "channel"] = Field(
        default="supergroup",
        description=(
            "supergroup: everybody admitted can post. channel: a broadcast, where only "
            "admins post and everyone else reads. Neither gets a public link."
        ),
    )
    users: list[int | str] = Field(
        default_factory=list,
        max_length=50,
        description="Initial members. Each is checked against the admin policy.",
    )

    @model_validator(mode="after")
    def _a_channel_starts_empty(self) -> CreateGroupInput:
        """A broadcast created with an audience already in it is an audience
        nobody chose to have — and unlike a group, they cannot answer back.
        Admitting people is ``chat.invite``, with an approval of its own."""
        if self.kind == "channel" and self.users:
            raise ValueError(
                "a channel is created empty; plan chat.invite for each person afterwards"
            )
        return self


class InviteUserInput(WriteInput):
    chat: int | str
    user: int | str


class AdminRights(BaseModel):
    """The rights a promotion grants.

    Every flag defaults to off, so a promotion grants exactly what was asked
    for. ``anonymous`` is absent on purpose: an admin action nobody can
    attribute defeats the audit log this project keeps.
    """

    model_config = ConfigDict(extra="forbid")

    change_info: bool = False
    delete_messages: bool = False
    ban_users: bool = False
    invite_users: bool = False
    pin_messages: bool = False
    manage_call: bool = False
    manage_topics: bool = False
    add_admins: bool = Field(
        default=False,
        description="Lets the promoted account promote others. Grant deliberately.",
    )

    @model_validator(mode="after")
    def _at_least_one(self) -> AdminRights:
        if not any(self.model_dump().values()):
            raise ValueError("a promotion must grant at least one right")
        return self


class PromoteAdminInput(WriteInput):
    chat: int | str
    user: int | str
    rights: AdminRights
    rank: str = Field(default="", max_length=16, description="Custom admin title.")


class ModerationInput(WriteInput):
    """One chat, one person.

    A list of members here would let a single approval remove a dozen of them,
    which is precisely the blast radius the review step exists to bound. The
    field is singular in every moderation operation, and it stays that way.
    """

    chat: int | str = Field(description="Chat id or @username of the group being moderated.")
    user: int | str = Field(description="Id or @username of the single member acted on.")


class BanUserInput(ModerationInput):
    user: int | str = Field(
        description="The member to remove and block from returning. One person per plan."
    )


class UnbanUserInput(ModerationInput):
    user: int | str = Field(
        description=(
            "The member whose ban and restrictions are lifted. This does not add them back."
        )
    )


class KickUserInput(ModerationInput):
    user: int | str = Field(
        description="The member to remove without banning them. One person per plan."
    )


class DemoteAdminInput(ModerationInput):
    user: int | str = Field(description="The admin whose rights are taken away.")


class Restrictions(BaseModel):
    """What a restricted member may no longer do.

    Every field is a *prohibition* — ``send_messages=True`` means they may no
    longer send messages — because that is how Telegram models it, and a layer
    that inverted the sense would make the preview and the RPC disagree about
    which way round a flag reads. All default to off, so a restriction takes
    away exactly what was asked for.

    ``view_messages`` is deliberately absent: taking that away *is* a ban, and
    a ban is its own operation with its own irreversibility warning rather than
    a flag hidden inside a rights object.
    """

    model_config = ConfigDict(extra="forbid")

    send_messages: bool = False
    send_media: bool = Field(
        default=False,
        description="Photos, video, files — and GIFs, games and inline results with them.",
    )
    send_stickers: bool = False
    send_polls: bool = False
    embed_links: bool = False
    invite_users: bool = False
    pin_messages: bool = False
    change_info: bool = False

    @model_validator(mode="after")
    def _at_least_one(self) -> Restrictions:
        if not any(self.model_dump().values()):
            raise ValueError("a restriction must take at least one right away")
        return self


#: How each prohibition is named in the text a person approves. "send_media:
#: true" is a field; "may no longer send media" is a sentence somebody can act
#: on, and the compound flags say what they actually cover.
RESTRICTION_LABELS: dict[str, str] = {
    "send_messages": "send messages",
    "send_media": "send media (photos, video, files, GIFs, games, inline results)",
    "send_stickers": "send stickers",
    "send_polls": "create polls",
    "embed_links": "post links that show a preview",
    "invite_users": "add other members",
    "pin_messages": "pin messages",
    "change_info": "change the chat's name, photo or description",
}

#: Telegram reads a ban shorter than 30 seconds or longer than 366 days as
#: permanent. Both ends are refused rather than accepted and silently widened:
#: a plan that says "for 5 seconds" and produces a permanent restriction is a
#: lie in the one text a person approves.
#:
#: The accepted range sits *inside* Telegram's by a margin on purpose. The
#: deadline is computed here and evaluated by a server on the far side of a
#: network round trip and a clock that is not this one — a window of exactly 30
#: seconds can arrive as 29 and become permanent, which is the failure this
#: check exists to prevent (raised by review, 2026-08-23).
MIN_RESTRICTION_SECONDS = 60
MAX_RESTRICTION_SECONDS = 365 * 24 * 60 * 60


class RestrictUserInput(ModerationInput):
    user: int | str = Field(description="The member whose rights are taken away.")
    restrictions: Restrictions = Field(
        description="Which rights to take away. At least one must be set."
    )
    duration_seconds: int = Field(
        default=0,
        description=(
            "How long the restriction lasts, counted from the moment the plan is applied. "
            f"0 means until an admin lifts it; otherwise between {MIN_RESTRICTION_SECONDS} "
            f"seconds and {MAX_RESTRICTION_SECONDS} (365 days)."
        ),
    )

    @model_validator(mode="after")
    def _duration_is_honest(self) -> RestrictUserInput:
        if self.duration_seconds == 0:
            return self
        if not MIN_RESTRICTION_SECONDS <= self.duration_seconds <= MAX_RESTRICTION_SECONDS:
            raise ValueError(
                "duration_seconds must be 0 (until lifted) or between "
                f"{MIN_RESTRICTION_SECONDS} and {MAX_RESTRICTION_SECONDS}; Telegram reads "
                "anything outside that range as permanent"
            )
        return self


class SetProfileInput(WriteInput):
    first_name: str | None = Field(default=None, max_length=64)
    last_name: str | None = Field(default=None, max_length=64)
    about: str | None = Field(default=None, max_length=70)

    @model_validator(mode="after")
    def _something_to_change(self) -> SetProfileInput:
        if self.first_name is None and self.last_name is None and self.about is None:
            raise ValueError("set at least one of first_name, last_name, about")
        return self


# ---------------------------------------------------------------------------
# planners
# ---------------------------------------------------------------------------


async def _create(
    ctx: OperationContext,
    *,
    operation: str,
    account: str,
    params: BaseModel,
    preconditions: dict[str, Any],
    summary: str,
) -> Plan:
    return ctx.plans.create(
        operation=operation,
        account=account,
        params=params.model_dump(mode="json"),
        preconditions=preconditions,
        summary=summary,
    )


async def plan_send_message(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(SendMessageInput, params)
    require_planning_profile(ctx, Capability.SEND, action="message.send")
    async with open_writer(ctx, p.account) as (label, client):
        target = await resolve_peer(client, p.chat)
    require_peer(ctx, Capability.SEND, target.ref, action="message.send")

    summary = (
        f"Send a message as {label} to {describe(target)}\n"
        f"--- message ({len(p.text)} chars) ---\n"
        f"{quote_for_review(p.text)}"
    ) + repeat_notice(p)
    return await _create(
        ctx,
        operation="message.send",
        account=label,
        params=p,
        preconditions={
            "peer": peer_snapshot(target),
            "body_sha256": text_digest(p.text),
            "body_len": len(p.text),
        },
        summary=summary,
    )


async def plan_reply_message(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(ReplyMessageInput, params)
    require_planning_profile(ctx, Capability.SEND, action="message.reply")
    async with open_writer(ctx, p.account) as (label, client):
        target = await resolve_peer(client, p.chat)
        require_peer(ctx, Capability.SEND, target.ref, action="message.reply")
        # The message being answered is part of what a reviewer is approving:
        # "reply to #412" means nothing without knowing what #412 says.
        original = await _fetch_message(client, target, p.reply_to_message_id)

    quoted = quote_for_review(getattr(original, "message", None) or "", limit=500)
    summary = (
        f"Reply as {label} in {describe(target)} to message "
        f"{p.reply_to_message_id}\n"
        f"--- in reply to ---\n{quoted}\n"
        f"--- message ({len(p.text)} chars) ---\n{quote_for_review(p.text)}"
    ) + repeat_notice(p)
    return await _create(
        ctx,
        operation="message.reply",
        account=label,
        params=p,
        preconditions={
            "peer": peer_snapshot(target),
            "reply_to": message_snapshot(original),
            "body_sha256": text_digest(p.text),
            "body_len": len(p.text),
        },
        summary=summary,
    )


async def plan_send_file(ctx: OperationContext, params: BaseModel) -> Plan:
    """Plan an upload, and describe it well enough to be approved.

    The file is resolved *before* the account is opened. Refusing a path costs
    nothing then — no connection, no lookup of the destination chat — and
    reading the file's own metadata is not an act anyone else can observe.

    What goes into the summary is the whole point of the operation: which file,
    how big, of what type, and **which form it arrives in**. "Send photo.jpg"
    hides the difference between a re-encoded picture and the original file,
    and only one of those two can be undone by sending it again.
    """
    p = cast(SendFileInput, params)
    require_planning_profile(ctx, Capability.SEND, action="message.send_file")
    file = resolve_outbound(ctx.settings, p.path, as_document=p.as_document)

    async with open_writer(ctx, p.account) as (label, client):
        target = await resolve_peer(client, p.chat)
        require_peer(ctx, Capability.SEND, target.ref, action="message.send_file")
        original = (
            await _fetch_message(client, target, p.reply_to_message_id)
            if p.reply_to_message_id is not None
            else None
        )

    caption = (
        f"--- caption ({len(p.caption)} chars) ---\n{quote_for_review(p.caption)}"
        if p.caption
        else "--- caption ---\n  (none)"
    )
    parts = [
        f"Send a file as {label} to {describe(target)}",
        file_preview(file),
        caption,
    ]
    if original is not None:
        quoted = quote_for_review(getattr(original, "message", None) or "", limit=500)
        parts.append(f"--- in reply to message {p.reply_to_message_id} ---\n{quoted}")
    if p.silent:
        parts.append("Delivered silently: no notification sound.")
    if notice := repeat_notice(p):
        parts.append(notice.strip())

    return await _create(
        ctx,
        operation="message.send_file",
        account=label,
        params=p,
        preconditions={
            "peer": peer_snapshot(target),
            # The digest is what makes "the file reviewed is the file sent"
            # checkable later: the path stays the same while the bytes behind it
            # need not, and swapping them is the cheap attack on a queue of
            # approved uploads.
            "file": file.snapshot(),
            "caption_sha256": text_digest(p.caption) if p.caption else None,
            "caption_len": len(p.caption),
            "reply_to": message_snapshot(original) if original is not None else None,
        },
        summary="\n".join(parts),
    )


async def plan_edit_message(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(EditMessageInput, params)
    require_planning_profile(ctx, Capability.SEND, action="message.edit")
    async with open_writer(ctx, p.account) as (label, client):
        target = await resolve_peer(client, p.chat)
        require_peer(ctx, Capability.SEND, target.ref, action="message.edit")
        original = await _fetch_message(client, target, p.message_id)
    _require_own_message(ctx, original, action="message.edit", peer=target.ref)

    before = quote_for_review(getattr(original, "message", None) or "", limit=1000)
    summary = (
        f"Edit message {p.message_id} as {label} in {describe(target)}\n"
        f"--- current text ---\n{before}\n"
        f"--- replacement ({len(p.text)} chars) ---\n{quote_for_review(p.text)}"
    )
    return await _create(
        ctx,
        operation="message.edit",
        account=label,
        params=p,
        preconditions={
            "peer": peer_snapshot(target),
            "message": message_snapshot(original),
            "body_sha256": text_digest(p.text),
            "body_len": len(p.text),
        },
        summary=summary,
    )


async def plan_delete_message(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(DeleteMessageInput, params)
    require_planning_profile(ctx, Capability.SEND, action="message.delete")
    async with open_writer(ctx, p.account) as (label, client):
        target = await resolve_peer(client, p.chat)
        require_peer(ctx, Capability.SEND, target.ref, action="message.delete")
        originals = await _fetch_messages(client, target, p.message_ids)
    for message in originals:
        _require_own_message(ctx, message, action="message.delete", peer=target.ref)

    listed = "\n".join(
        f"  {m.id}: {quote_for_review(getattr(m, 'message', None) or '', limit=120)}"
        for m in originals
    )
    scope = "for everyone" if p.revoke else "for this account only"
    summary = (
        f"Delete {len(originals)} message(s) {scope} as {label} in {describe(target)}\n"
        f"--- messages ---\n{listed}"
    )
    return await _create(
        ctx,
        operation="message.delete",
        account=label,
        params=p,
        preconditions={
            "peer": peer_snapshot(target),
            "messages": [message_snapshot(m) for m in originals],
        },
        summary=summary,
    )


async def plan_forward_message(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(ForwardMessageInput, params)
    require_planning_profile(ctx, Capability.SEND, action="message.forward")
    async with open_writer(ctx, p.account) as (label, client):
        source = await resolve_peer(client, p.source_chat)
        destination = await resolve_peer(client, p.destination_chat)
        # Both ends are checked. Forwarding reads one chat and writes another,
        # so a rule that only guards the destination would let any readable
        # chat be copied anywhere the account may post — and one that only
        # guards the source would let a permitted chat be republished to a
        # forbidden one.
        require_peer(ctx, Capability.READ_CHAT, source.ref, action="message.forward")
        require_peer(ctx, Capability.SEND, destination.ref, action="message.forward")
        originals = await _fetch_messages(client, source, p.message_ids)

    listed = "\n".join(
        f"  {m.id}: {quote_for_review(getattr(m, 'message', None) or '', limit=120)}"
        for m in originals
    )
    attribution = "without attribution" if p.drop_author else "with attribution"
    summary = (
        f"Forward {len(originals)} message(s) {attribution} as {label}\n"
        f"  from {describe(source)}\n"
        f"  to   {describe(destination)}\n"
        f"--- messages ---\n{listed}"
    ) + repeat_notice(p)
    return await _create(
        ctx,
        operation="message.forward",
        account=label,
        params=p,
        preconditions={
            "source": peer_snapshot(source),
            "destination": peer_snapshot(destination),
            "messages": [message_snapshot(m) for m in originals],
        },
        summary=summary,
    )


async def plan_mark_read(ctx: OperationContext, params: BaseModel) -> Plan:
    """Mark a chat read — deliberately, never as a side effect.

    Reading a chat through this tool never acknowledges it: the read
    operations are forbidden from doing so in code. Marking read is visible to
    the other party ("they have seen it"), so it is its own remote write, gets
    its own plan, and shows up in the audit log like every other effect.
    """
    p = cast(MarkReadInput, params)
    require_planning_profile(ctx, Capability.SEND, action="chat.mark_read")
    async with open_writer(ctx, p.account) as (label, client):
        target = await resolve_peer(client, p.chat)
    require_peer(ctx, Capability.SEND, target.ref, action="chat.mark_read")

    upto = f"up to message {p.max_message_id}" if p.max_message_id else "entirely"
    summary = (
        f"Mark {describe(target)} read {upto} as {label}. The other side will see the read receipt."
    )
    return await _create(
        ctx,
        operation="chat.mark_read",
        account=label,
        params=p,
        preconditions={"peer": peer_snapshot(target)},
        summary=summary,
    )


async def plan_join_chat(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(JoinChatInput, params)
    require_planning_profile(ctx, Capability.JOIN, action="chat.join")
    async with open_writer(ctx, p.account) as (label, client):
        target = await resolve_join_target(client, p.target)
    require_peer(ctx, Capability.JOIN, target.ref, action="chat.join")

    how = "private invite" if target.invite_hash else "public username"
    summary = f"Join {describe(target)} as {label} via {how}."
    return await _create(
        ctx,
        operation="chat.join",
        account=label,
        params=p,
        preconditions={"peer": peer_snapshot(target)},
        summary=summary,
    )


async def plan_leave_chat(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(LeaveChatInput, params)
    require_planning_profile(ctx, Capability.JOIN, action="chat.leave")
    async with open_writer(ctx, p.account) as (label, client):
        target = await resolve_peer(client, p.chat)
    # Membership in both directions is governed by the same rule: an account
    # that may not join a chat may not be walked out of one either, since
    # leaving a private group can be irreversible.
    require_peer(ctx, Capability.JOIN, target.ref, action="chat.leave")

    summary = (
        f"Leave {describe(target)} as {label}. Re-joining a private chat needs a fresh invite."
    )
    return await _create(
        ctx,
        operation="chat.leave",
        account=label,
        params=p,
        preconditions={"peer": peer_snapshot(target)},
        summary=summary,
    )


async def plan_create_group(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(CreateGroupInput, params)
    require_planning_profile(ctx, Capability.ADMIN, action="chat.create")
    ctx.safety.require_group_creation()

    invitees: list[Resolved] = []
    async with open_writer(ctx, p.account) as (label, client):
        for user in p.users:
            resolved = await resolve_peer(client, user)
            # Adding somebody to a group acts on that person, so each invitee
            # is judged individually — the group does not exist yet and cannot
            # carry the permission.
            require_peer(ctx, Capability.ADMIN, resolved.ref, action="chat.create")
            invitees.append(resolved)

    listed = "\n".join(f"  {describe(r)}" for r in invitees) or "  (nobody)"
    # Which kind is not a detail: it decides whether the people who end up there
    # can speak at all, and the two are one word apart in a tool call.
    kind_line = (
        "a broadcast channel: only admins post, everyone else reads"
        if p.kind == "channel"
        else "a supergroup: everybody admitted can post"
    )
    summary = (
        f'Create {p.kind} "{sanitize_line(p.title, limit=80)}" as {label}\n'
        f"  kind:    {kind_line}\n"
        "  reach:   private — it gets no public link and no username; "
        "somebody has to give it one later for it to be findable\n"
        f"--- description ({len(p.about)} chars) ---\n{quote_for_review(p.about, limit=255)}\n"
        f"--- initial members ---\n{listed}"
    )
    return await _create(
        ctx,
        operation="chat.create",
        account=label,
        params=p,
        preconditions={
            # No chat id exists yet, so the only snapshot worth taking is of
            # the people who would be pulled in.
            "title_sha256": text_digest(p.title),
            # Compared again at apply time, like a promotion's rights: a plan
            # row edited between review and apply must not turn the supergroup
            # somebody approved into a channel nobody may answer in.
            "kind": p.kind,
            "users": [peer_snapshot(r) for r in invitees],
        },
        summary=summary,
    )


async def plan_invite_user(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(InviteUserInput, params)
    require_planning_profile(ctx, Capability.ADMIN, action="chat.invite")
    async with open_writer(ctx, p.account) as (label, client):
        chat = await resolve_peer(client, p.chat)
        user = await resolve_peer(client, p.user)
    require_peer(ctx, Capability.ADMIN, chat.ref, action="chat.invite")
    require_peer(ctx, Capability.ADMIN, user.ref, action="chat.invite")

    summary = f"Invite {describe(user)} into {describe(chat)} as {label}."
    return await _create(
        ctx,
        operation="chat.invite",
        account=label,
        params=p,
        preconditions={"chat": peer_snapshot(chat), "user": peer_snapshot(user)},
        summary=summary,
    )


async def plan_promote_admin(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(PromoteAdminInput, params)
    require_planning_profile(ctx, Capability.ADMIN, action="chat.promote")
    async with open_writer(ctx, p.account) as (label, client):
        chat = await resolve_peer(client, p.chat)
        user = await resolve_peer(client, p.user)
    require_peer(ctx, Capability.ADMIN, chat.ref, action="chat.promote")
    require_peer(ctx, Capability.ADMIN, user.ref, action="chat.promote")

    granted = ", ".join(name for name, on in p.rights.model_dump().items() if on)
    summary = (
        f"Promote {describe(user)} in {describe(chat)} as {label}\n"
        f"  rights: {granted}\n"
        f"  rank:   {sanitize_line(p.rank, limit=16) or '(none)'}"
    )
    return await _create(
        ctx,
        operation="chat.promote",
        account=label,
        params=p,
        preconditions={
            "chat": peer_snapshot(chat),
            "user": peer_snapshot(user),
            "rights": p.rights.model_dump(),
        },
        summary=summary,
    )


# ---------------------------------------------------------------------------
# moderation: the operations that take something away
# ---------------------------------------------------------------------------
#
# The project could grant admin rights long before it could take any back,
# which meant an agent could produce a state it was unable to reverse. These
# five close that: ban has unban, restrict expires or is lifted by the same
# unban, promote has demote, and kick is removal that was never a ban.
#
# Two things are said in every summary they build. *Who exactly* — a name, a
# username and a numeric id for both the chat and the person, because "restrict
# a user" is not a sentence anybody can approve. And, for the two that the
# person on the receiving end cannot undo, that they cannot undo it.


async def _moderation_target(
    ctx: OperationContext, params: Any, *, action: str
) -> tuple[str, Resolved, Resolved]:
    """Resolve the chat and the member, checking both against the admin policy.

    Both peers are judged, not just the chat: a rule that only guarded the
    group would let any chat this account administers be used to act on
    somebody who was never named in the configuration.
    """
    require_planning_profile(ctx, Capability.ADMIN, action=action)
    async with open_writer(ctx, params.account) as (label, client):
        chat = await resolve_peer(client, params.chat)
        user = await resolve_peer(client, params.user)
    require_peer(ctx, Capability.ADMIN, chat.ref, action=action)
    require_peer(ctx, Capability.ADMIN, user.ref, action=action)
    return label, chat, user


def duration_phrase(seconds: int) -> str:
    """A window a person reads at a glance, not a number of seconds."""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [
        f"{value}{unit}"
        for value, unit in ((days, "d"), (hours, "h"), (minutes, "m"), (secs, "s"))
        if value
    ]
    return " ".join(parts) or "0s"


def _require_ban_list(chat: Resolved, *, what: str) -> None:
    """Refuse, while the plan is being written, what a basic group cannot do.

    A basic group has no ban list and no per-member rights. Discovering that at
    apply time would mean a person had already approved a summary describing
    something Telegram was never going to do, so the type of the chat is checked
    here — where the answer changes what gets written down rather than only what
    happens.
    """
    if chat.is_channel:
        return
    # By id, not by `describe`: that line quotes the chat title, which is
    # written by whoever runs the chat. In a plan summary it is wrapped and
    # announced as untrusted; in an error message it would be neither, because
    # the sentence around it is this project's own. Same rule as `ops/topics.py`.
    raise InvalidInput(
        f"chat {chat.ref.peer_id} is a basic group: Telegram keeps no ban list and no "
        f"per-member rights there, so {what} does not exist for it",
        suggestion="Plan chat.kick instead, or convert the group to a supergroup first.",
    )


async def plan_ban_user(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(BanUserInput, params)
    label, chat, user = await _moderation_target(ctx, p, action="chat.ban")

    if chat.is_channel:
        effect = (
            "  effect:  they are removed from the chat and cannot come back, "
            "not through a link and not through a public username\n"
        )
    else:
        # Said here rather than as a warning after the fact: a summary that
        # promised a ban and produced a removal would have been approved on a
        # description of something that did not happen.
        effect = (
            "  effect:  THIS IS A BASIC GROUP, which keeps no ban list — applying this "
            "REMOVES them, and any member can add them straight back. Convert the group "
            "to a supergroup first if you need the ban to hold.\n"
        )
    summary = (
        f"Ban {describe(user)} from {describe(chat)} as {label}\n"
        f"{effect}"
        "  IRREVERSIBLE FOR THEM: the banned person cannot undo this. "
        "Only an admin can, by planning `chat unban` for the same person.\n"
        "  their existing messages are left in place"
    )
    return await _create(
        ctx,
        operation="chat.ban",
        account=label,
        params=p,
        preconditions={"chat": peer_snapshot(chat), "user": peer_snapshot(user)},
        summary=summary,
    )


async def plan_unban_user(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(UnbanUserInput, params)
    label, chat, user = await _moderation_target(ctx, p, action="chat.unban")
    _require_ban_list(chat, what="lifting a ban")

    summary = (
        f"Lift the ban and every restriction on {describe(user)} in {describe(chat)} as {label}\n"
        "  effect:  they may join again and speak again\n"
        "  note:    this does not add them back — they have to rejoin, or be invited"
    )
    return await _create(
        ctx,
        operation="chat.unban",
        account=label,
        params=p,
        preconditions={"chat": peer_snapshot(chat), "user": peer_snapshot(user)},
        summary=summary,
    )


async def plan_kick_user(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(KickUserInput, params)
    label, chat, user = await _moderation_target(ctx, p, action="chat.kick")

    summary = (
        f"Remove {describe(user)} from {describe(chat)} as {label}, without banning them\n"
        "  effect:  they leave the chat; nothing stops them returning through a "
        "public username or a fresh invite\n"
        "  IRREVERSIBLE FOR THEM: they cannot undo the removal — getting back in needs "
        "the chat to be public, or somebody to invite them"
    )
    return await _create(
        ctx,
        operation="chat.kick",
        account=label,
        params=p,
        preconditions={"chat": peer_snapshot(chat), "user": peer_snapshot(user)},
        summary=summary,
    )


async def plan_restrict_user(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(RestrictUserInput, params)
    label, chat, user = await _moderation_target(ctx, p, action="chat.restrict")
    _require_ban_list(chat, what="restricting one member")

    taken = "\n".join(
        f"    - {RESTRICTION_LABELS[name]}"
        for name, on in p.restrictions.model_dump().items()
        if on
    )
    window = (
        f"{duration_phrase(p.duration_seconds)}, counted from the moment the plan is applied"
        if p.duration_seconds
        else "until an admin lifts it (no expiry)"
    )
    summary = (
        f"Restrict {describe(user)} in {describe(chat)} as {label}\n"
        f"  they may no longer:\n{taken}\n"
        f"  duration: {window}\n"
        "  they stay in the chat and keep reading it; plan `chat unban` for the same "
        "person to lift this early"
    )
    return await _create(
        ctx,
        operation="chat.restrict",
        account=label,
        params=p,
        preconditions={
            "chat": peer_snapshot(chat),
            "user": peer_snapshot(user),
            # Compared again at apply time, like a promotion's rights: a plan
            # row edited between review and apply must not be applied against
            # the summary somebody actually read.
            "restrictions": p.restrictions.model_dump(),
            "duration_seconds": p.duration_seconds,
        },
        summary=summary,
    )


async def plan_demote_admin(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(DemoteAdminInput, params)
    label, chat, user = await _moderation_target(ctx, p, action="chat.demote")

    summary = (
        f"Take every admin right away from {describe(user)} in {describe(chat)} as {label}\n"
        "  effect:  they keep their membership and lose all admin powers, "
        "including their custom title\n"
        "  reversible: plan `chat promote` for the same person to grant rights again\n"
        "  note:    Telegram only lets an account demote admins it promoted itself"
    )
    return await _create(
        ctx,
        operation="chat.demote",
        account=label,
        params=p,
        preconditions={"chat": peer_snapshot(chat), "user": peer_snapshot(user)},
        summary=summary,
    )


async def plan_set_profile(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(SetProfileInput, params)
    require_planning_profile(ctx, Capability.PROFILE, action="account.profile")
    # Profile changes are scoped to an account, not to a chat, so there is no
    # peer for the allowlist to judge — the kernel answers this one directly.
    ctx.safety.require_profile_change()

    # Opened for its side effects only: it validates the label against
    # safety.accounts and proves the account is usable before a plan is
    # recorded against it. There is no peer to resolve for a profile change.
    async with open_writer(ctx, p.account) as (label, _client):
        pass

    changes = "\n".join(
        f"  {field}: {quote_for_review(value, limit=200)}"
        for field, value in (
            ("first_name", p.first_name),
            ("last_name", p.last_name),
            ("about", p.about),
        )
        if value is not None
    )
    summary = f"Change the visible profile of account {label}\n{changes}"
    return await _create(
        ctx,
        operation="account.profile",
        account=label,
        params=p,
        preconditions={
            field: text_digest(value)
            for field, value in (
                ("first_name_sha256", p.first_name),
                ("last_name_sha256", p.last_name),
                ("about_sha256", p.about),
            )
            if value is not None
        },
        summary=summary,
    )


# ---------------------------------------------------------------------------
# message lookups shared by planner and applier
# ---------------------------------------------------------------------------


async def _fetch_message(client: Any, target: Resolved, message_id: int) -> Any:
    messages = await _fetch_messages(client, target, [message_id])
    return messages[0]


async def _fetch_messages(client: Any, target: Resolved, ids: list[int]) -> list[Any]:
    """Load the messages a plan refers to, refusing when one is missing.

    A missing message is not a detail to skip over: it means the plan describes
    something that is no longer there, and applying it would act on a different
    set than the one reviewed.
    """
    fetched = await client.get_messages(target.ref.peer_id, ids=list(ids))
    found = [m for m in fetched if m is not None]
    if len(found) != len(ids):
        missing = sorted(set(ids) - {int(m.id) for m in found})
        # The chat is named by id, not by `describe`. A title is written by
        # strangers; `Envelope.failure` defangs an error payload, but a value
        # interpolated into a sentence this project composed is not wrapped —
        # it would read as our own words in the field a reader trusts most.
        raise InvalidInput(
            f"message(s) {missing} not found in chat {target.ref.peer_id}",
            suggestion="Check the ids; deleted messages cannot be edited or forwarded.",
        )
    return found


def _require_own_message(
    ctx: OperationContext, message: Any, *, action: str, peer: PeerRef
) -> None:
    """Only this account's own messages may be edited or deleted.

    Removing somebody else's message is a moderation action with a different
    blast radius and a different audience, and it is out of scope for v0.1.
    Refusing here means a plan cannot quietly become one.
    """
    if getattr(message, "out", False):
        return
    reason = (
        f"message {getattr(message, 'id', '?')} was not sent by this account; "
        "editing or deleting other people's messages is not supported"
    )
    ctx.audit.refusal(action=action, actor=ctx.actor, reason=reason, peer_id=peer.peer_id)
    raise NotAllowlisted(reason)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def _register(
    *,
    name: str,
    cli: tuple[str, ...],
    plan_tool: str,
    summary: str,
    input_model: type[BaseModel],
    capability: Capability,
    planner: Any,
    description: str = "",
) -> Operation:
    """Declare one remote write.

    ``mcp_tool`` is ``None`` for all of them, and the registry's invariants
    enforce it: a remote write is planned over MCP and applied from a terminal.
    A tool that performed the write directly would make the review step
    optional, which is the same as not having one.
    """
    return REGISTRY.register(
        Operation(
            name=name,
            cli=cli,
            summary=summary,
            description=description,
            input_model=input_model,
            effect=Effect.REMOTE_WRITE,
            mcp_tool=None,
            plan_tool=plan_tool,
            capability=capability,
            planner=planner,
            tags=("write", "plan"),
        )
    )


SEND_MESSAGE = _register(
    name="message.send",
    cli=("message", "send"),
    plan_tool="telegram_plan_send_message",
    summary="Plan a new message to a chat.",
    description="Records the intent to send. Nothing is sent until a person applies the plan.",
    input_model=SendMessageInput,
    capability=Capability.SEND,
    planner=plan_send_message,
)

REPLY_MESSAGE = _register(
    name="message.reply",
    cli=("message", "reply"),
    plan_tool="telegram_plan_reply_message",
    summary="Plan a reply to a specific message.",
    input_model=ReplyMessageInput,
    capability=Capability.SEND,
    planner=plan_reply_message,
)

SEND_FILE = _register(
    name="message.send_file",
    cli=("message", "send-file"),
    plan_tool="telegram_plan_send_file",
    summary="Plan sending one local file to a chat.",
    description=(
        "Records the intent to upload a file. Nothing is sent until a person applies the "
        "plan. The file must already be inside the configured outbox directory "
        "(paths.uploads): a caller-chosen path anywhere on the host would make this a way "
        "to post private keys, session files or anyone's documents into a chat. The plan's "
        "summary names the file, its size, its type and the form it arrives in — compressed "
        "media or an untouched document."
    ),
    input_model=SendFileInput,
    capability=Capability.SEND,
    planner=plan_send_file,
)

EDIT_MESSAGE = _register(
    name="message.edit",
    cli=("message", "edit"),
    plan_tool="telegram_plan_edit_message",
    summary="Plan an edit of a message this account sent.",
    input_model=EditMessageInput,
    capability=Capability.SEND,
    planner=plan_edit_message,
)

DELETE_MESSAGE = _register(
    name="message.delete",
    cli=("message", "delete"),
    plan_tool="telegram_plan_delete_message",
    summary="Plan the deletion of messages this account sent.",
    input_model=DeleteMessageInput,
    capability=Capability.SEND,
    planner=plan_delete_message,
)

FORWARD_MESSAGE = _register(
    name="message.forward",
    cli=("message", "forward"),
    plan_tool="telegram_plan_forward_message",
    summary="Plan forwarding messages from one chat to another.",
    description="Checks the source and the destination separately.",
    input_model=ForwardMessageInput,
    capability=Capability.SEND,
    planner=plan_forward_message,
)

MARK_READ = _register(
    name="chat.mark_read",
    cli=("chat", "mark-read"),
    plan_tool="telegram_plan_mark_read",
    summary="Plan marking a chat as read.",
    description="Reading a chat never does this implicitly; it is always an explicit plan.",
    input_model=MarkReadInput,
    capability=Capability.SEND,
    planner=plan_mark_read,
)

JOIN_CHAT = _register(
    name="chat.join",
    cli=("chat", "join"),
    plan_tool="telegram_plan_join_chat",
    summary="Plan joining a public chat or accepting an invite.",
    input_model=JoinChatInput,
    capability=Capability.JOIN,
    planner=plan_join_chat,
)

LEAVE_CHAT = _register(
    name="chat.leave",
    cli=("chat", "leave"),
    plan_tool="telegram_plan_leave_chat",
    summary="Plan leaving a chat.",
    input_model=LeaveChatInput,
    capability=Capability.JOIN,
    planner=plan_leave_chat,
)

CREATE_GROUP = _register(
    name="chat.create",
    cli=("chat", "create"),
    plan_tool="telegram_plan_create_group",
    summary="Plan the creation of a supergroup or a broadcast channel.",
    description=(
        "`kind` decides which: a supergroup everybody admitted may post in, or a channel "
        "only admins post in. Neither gets a public link, and a channel is created empty."
    ),
    input_model=CreateGroupInput,
    capability=Capability.ADMIN,
    planner=plan_create_group,
)

INVITE_USER = _register(
    name="chat.invite",
    cli=("chat", "invite"),
    plan_tool="telegram_plan_invite_user",
    summary="Plan adding a user to a chat.",
    input_model=InviteUserInput,
    capability=Capability.ADMIN,
    planner=plan_invite_user,
)

PROMOTE_ADMIN = _register(
    name="chat.promote",
    cli=("chat", "promote"),
    plan_tool="telegram_plan_promote_admin",
    summary="Plan granting admin rights to a user in a chat.",
    input_model=PromoteAdminInput,
    capability=Capability.ADMIN,
    planner=plan_promote_admin,
)

BAN_USER = _register(
    name="chat.ban",
    cli=("chat", "ban"),
    plan_tool="telegram_plan_ban_user",
    summary="Plan banning one member from a chat.",
    description=(
        "Removes the person and blocks them from returning. The plan says so in as many "
        "words: the person banned cannot undo it, only an admin can, through chat.unban."
    ),
    input_model=BanUserInput,
    capability=Capability.ADMIN,
    planner=plan_ban_user,
)

UNBAN_USER = _register(
    name="chat.unban",
    cli=("chat", "unban"),
    plan_tool="telegram_plan_unban_user",
    summary="Plan lifting a ban, and every restriction, on one member.",
    description=(
        "The undo for both chat.ban and chat.restrict — Telegram stores them in one set of "
        "rights. It does not add the person back; they rejoin or are invited."
    ),
    input_model=UnbanUserInput,
    capability=Capability.ADMIN,
    planner=plan_unban_user,
)

KICK_USER = _register(
    name="chat.kick",
    cli=("chat", "kick"),
    plan_tool="telegram_plan_kick_user",
    summary="Plan removing one member without banning them.",
    input_model=KickUserInput,
    capability=Capability.ADMIN,
    planner=plan_kick_user,
)

RESTRICT_USER = _register(
    name="chat.restrict",
    cli=("chat", "restrict"),
    plan_tool="telegram_plan_restrict_user",
    summary="Plan taking specific rights away from one member, with a duration.",
    description=(
        "Each right is named in the plan, and so is how long it lasts — a preview that said "
        "only 'restrict a user' would not be something a person could decide on."
    ),
    input_model=RestrictUserInput,
    capability=Capability.ADMIN,
    planner=plan_restrict_user,
)

DEMOTE_ADMIN = _register(
    name="chat.demote",
    cli=("chat", "demote"),
    plan_tool="telegram_plan_demote_admin",
    summary="Plan taking every admin right away from one member.",
    description="The undo for chat.promote. Membership is untouched; the rights and rank go.",
    input_model=DemoteAdminInput,
    capability=Capability.ADMIN,
    planner=plan_demote_admin,
)

SET_PROFILE = _register(
    name="account.profile",
    cli=("account", "profile"),
    plan_tool="telegram_plan_set_profile",
    summary="Plan a change to this account's visible profile.",
    input_model=SetProfileInput,
    capability=Capability.PROFILE,
    planner=plan_set_profile,
)

#: Every remote write *declared in this module*, in registration order. The
#: settings writes live in :mod:`telegram_ai_cli.ops.settings` and have a list
#: of their own; the applier dispatches on ``Operation.name`` and must cover
#: both, which is asserted against the registry rather than against either list.
WRITE_OPERATIONS: tuple[Operation, ...] = (
    SEND_MESSAGE,
    REPLY_MESSAGE,
    SEND_FILE,
    EDIT_MESSAGE,
    DELETE_MESSAGE,
    FORWARD_MESSAGE,
    MARK_READ,
    JOIN_CHAT,
    LEAVE_CHAT,
    CREATE_GROUP,
    INVITE_USER,
    PROMOTE_ADMIN,
    BAN_USER,
    UNBAN_USER,
    KICK_USER,
    RESTRICT_USER,
    DEMOTE_ADMIN,
    SET_PROFILE,
)
