"""Carrying out a plan — the only code in the project that writes to Telegram.

There is no MCP tool that reaches this module, and the registry asserts as
much. A plan is written by whoever is driving the agent; applying it is a
command a person types. That boundary is documented honestly in ``SECURITY.md``
— an agent that also has a shell can type the command itself — but the audit
trail, the persistent limits and the human-readable summary all exist to make
the effect visible either way.

The order of operations below is the whole design, and every step is placed
where it is because the alternative fails in a specific way:

1. **Claim first.** ``pending → applying`` is one conditional UPDATE, so two
   processes racing on the same plan cannot both proceed. Reading the state and
   then writing it leaves a window, and losing that race sends a message twice.
2. **Re-check everything.** The policy check at planning time answered a
   question about the world as it was. Usernames get released and re-registered,
   messages get deleted, an allowlist gets edited. So the target is resolved
   again and compared against the snapshot in the plan.
3. **Refuse a duplicate.** The claim above stops one plan being applied twice;
   it says nothing about *two* plans carrying the same message, which is what a
   fresh session with no memory of the last one produces. The check sits here —
   after verification, because it needs the re-resolved peer id and the
   re-digested file, and before the reservation, because a refusal that never
   reached Telegram must not spend a slot meant for requests that did.
4. **Reserve the rate-limit slot before the network call.** Checking a counter
   and incrementing it after the ``await`` lets concurrent callers all read the
   same number and all proceed; a cap of five becomes eight.
5. **Write the audit attempt, and the ledger row, before the RPC.** A log
   written only after success loses exactly the case it exists for: the send
   that went out and then the process died. The ledger row is written there for
   the same reason and dropped again only where the slot is refunded.
6. **Then, and only then, the RPC.**

**A timeout after the request left is not a failure.** It is
``unknown_outcome``: the slot stays consumed, nothing is retried, and a person
has to look. Automatic retry here is how one message becomes two.

**Refunds are decided by exception class, never by message text.** Only errors
that cannot be raised after the request took effect give the slot back.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .envelope import Envelope, Meta
from .errors import (
    DuplicateOutbound,
    ForwardsRestricted,
    InvalidInput,
    PlanPreconditionFailed,
    PlanUnknownOutcome,
    PolicyError,
    PrivacyRestricted,
    TelegramAIError,
    TelegramError,
)
from .ledger import LEDGERED_OPERATIONS, LedgerEntry, fingerprint
from .limits import LimitKind, Reservation
from .opspec import REGISTRY
from .outbox import Delivery, OutboundFile, human_bytes, resolve_outbound
from .plans import Plan, PlanState
from .render import sanitize_line
from .safety import Capability

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pydantic import BaseModel

    from .context import OperationContext
    from .ops.write import Resolved

#: Ceiling on a single RPC. Past this the answer is unknown, not late: Telethon
#: has been told not to retry, so nothing further will arrive on its own.
RPC_TIMEOUT_SECONDS = 60

#: Operations whose "one RPC" is really a transfer, and therefore need their own
#: ceiling. Sixty seconds is generous for a request that carries a sentence and
#: far too little for one that carries a hundred megabytes — and the cost of
#: getting it wrong is not a retry but an ``unknown_outcome`` a person has to
#: resolve by hand, because a timeout mid-upload proves nothing about whether
#: the message appeared.
_UPLOAD_OPERATIONS = frozenset({"message.send_file", "chat.set_photo"})

#: Which budget each operation draws from, and therefore which ceiling refuses
#: it. Keyed by ``Operation.name`` so a new write cannot be added without
#: deciding what it costs.
_LIMIT_KINDS: dict[str, LimitKind] = {
    "message.send": LimitKind.SEND,
    "message.reply": LimitKind.SEND,
    "message.send_file": LimitKind.SEND,
    "message.edit": LimitKind.SEND,
    "message.delete": LimitKind.SEND,
    "message.forward": LimitKind.SEND,
    # A scheduled send is a send: it costs the same budget, at the moment the
    # queue entry is created rather than at the moment it goes out. Charging it
    # later is not an option — nothing of ours runs then.
    "message.schedule": LimitKind.SEND,
    # Marking read is visible to the other party, so it draws on the same
    # budget as speaking rather than being free.
    "chat.mark_read": LimitKind.SEND,
    # A reaction is speech: it appears under somebody's message and notifies
    # them. Same budget as a message, for the same reason.
    "message.react": LimitKind.SEND,
    "message.unreact": LimitKind.SEND,
    # Pinning changes what every member of the chat sees at the top of their
    # window, which is an administrative act rather than a remark.
    "message.pin": LimitKind.ADMIN,
    "message.unpin": LimitKind.ADMIN,
    "chat.join": LimitKind.JOIN,
    "chat.leave": LimitKind.JOIN,
    "chat.create": LimitKind.ADMIN,
    "chat.invite": LimitKind.ADMIN,
    "chat.promote": LimitKind.ADMIN,
    # Moderation draws on the same budget as any other admin act. A ceiling that
    # counted promotions but not bans would leave the destructive half of the
    # pair unlimited.
    "chat.ban": LimitKind.ADMIN,
    "chat.unban": LimitKind.ADMIN,
    "chat.kick": LimitKind.ADMIN,
    "chat.restrict": LimitKind.ADMIN,
    "chat.demote": LimitKind.ADMIN,
    # Nobody but the account owner can observe these, so they are not sends —
    # but they are still requests to Telegram, and an unbudgeted operation is a
    # loop nothing stops.
    "chat.archive": LimitKind.ADMIN,
    "chat.mute": LimitKind.ADMIN,
    "account.profile": LimitKind.ADMIN,
    # A chat's identity is an admin act on that chat, and the block list is an
    # admin act on a person; both draw on the budget that bounds how much of
    # either can happen in one window.
    "chat.set_title": LimitKind.ADMIN,
    "chat.set_about": LimitKind.ADMIN,
    "chat.set_photo": LimitKind.ADMIN,
    "account.block": LimitKind.ADMIN,
    "account.unblock": LimitKind.ADMIN,
}


@dataclass(slots=True)
class _Prepared:
    """Everything verification learned, handed to the RPC step."""

    limit_target: str
    peers: dict[str, Resolved] = field(default_factory=dict)
    messages: list[Any] = field(default_factory=list)
    #: The file an upload plan sends, re-resolved and re-digested here rather
    #: than trusted from the plan: the path is a name, and the bytes behind a
    #: name can change between review and apply.
    attachment: OutboundFile | None = None
    audit_peer_id: int | None = None
    audit_body: str | None = None
    warnings: list[str] = field(default_factory=list)
    #: The account's complete reaction list for the message afterwards, worked
    #: out during verification. Telegram's reaction call takes the whole list
    #: rather than a delta, and computing it after the audit record is written
    #: would put a refusable input error on the far side of the "the request may
    #: already have left" line. ``None`` means "not a reaction operation";
    #: ``[]`` means "remove them all", and the two are not the same thing.
    reactions: list[dict[str, Any]] | None = None
    #: Which message ids this plan actually addresses, decided during
    #: verification. They are not simply the arguments: a ``t.me/…/123`` link in
    #: the chat argument is what names the message, and the RPC step must send
    #: the id that was checked rather than re-derive one of its own.
    message_ids: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# exception classes, not messages
# ---------------------------------------------------------------------------


def _no_effect_error_classes() -> tuple[type[BaseException], ...]:
    """Errors that prove the request was rejected, so the slot may be refunded.

    Every entry is a *server-side refusal*: Telegram answered the request with
    "no". The answer is the evidence — the action did not happen, so the budget
    it reserved was not spent.

    Deliberately absent: ``RpcCallFailError``, ``RpcMcgetFailError``, plain
    ``RPCError`` and anything transport-shaped. Those can arrive after the
    server already applied the change, and refunding one of those is how a
    limit quietly stops limiting.
    """
    from telethon.errors import rpcerrorlist as rpc

    names = (
        # Rate-limited by Telegram: the request was counted and discarded.
        "FloodWaitError",
        "PeerFloodError",
        "SlowModeWaitError",
        # The account is not allowed to write here, so nothing was written.
        "ChatWriteForbiddenError",
        "ChatAdminRequiredError",
        "UserBannedInChannelError",
        "ChannelPrivateError",
        "ChatRestrictedError",
        # Content protection on the source chat. Enforced by the server, so the
        # forward simply did not happen — which is why it belongs here and not
        # in the ambiguous set: burning the rate-limit slot for a request
        # Telegram refused outright would spend budget on nothing.
        "ChatForwardsRestrictedError",
        # The target refused, by their own settings or their account state.
        "UserPrivacyRestrictedError",
        "UserNotMutualContactError",
        "InputUserDeactivatedError",
        "UserBlockedError",
        "UserIsBlockedError",
        # The request never described anything Telegram could act on, so there
        # was nothing for it to change.
        "MessageIdInvalidError",
        "MsgIdInvalidError",
        "MessageNotModifiedError",
        # Reacting and pinning add their own refusals. Each is Telegram saying
        # "no" to the request: the reaction is not one this chat permits, the
        # account cannot pay for a star reaction, too many were sent, the pin
        # state is already what was asked for, or pinning is not allowed here.
        # Without them any of these lands in `unknown_outcome`, which spends the
        # budget and asks a person to go and look at a chat nothing happened in.
        "ReactionInvalidError",
        "ReactionsTooManyError",
        "PremiumAccountRequiredError",
        "DocumentInvalidError",
        "ChatNotModifiedError",
        "PinRestrictedError",
        "BotOnesideNotAvailError",
        "MessageEmptyError",
        "MessageTooLongError",
        "PeerIdInvalidError",
        # A schedule Telegram will not take. Each of these is answered before
        # anything is queued: an unusable date, one past its horizon, a queue
        # that is full, or a peer whose online status is hidden — which is the
        # one thing "send when they are next online" cannot work without.
        "ScheduleDateInvalidError",
        "ScheduleDateTooLateError",
        "ScheduleTooMuchError",
        "ScheduleStatusPrivateError",
        "UsernameNotOccupiedError",
        "UsernameInvalidError",
        "InviteHashExpiredError",
        "InviteHashInvalidError",
        "ChannelsTooMuchError",
        "UserAlreadyParticipantError",
    )
    # Looked up by name: Telethon regenerates this module per layer, and a
    # constant that disappeared upstream must not take the applier down with an
    # AttributeError at import time.
    found = (getattr(rpc, name, None) for name in names)
    return tuple(dict.fromkeys(cls for cls in found if isinstance(cls, type)))


def _ambiguous_error_classes() -> tuple[type[BaseException], ...]:
    """Errors that say nothing about whether the change took effect.

    These land in ``unknown_outcome``: the slot stays consumed and nobody
    retries automatically. A server-side internal failure may have been raised
    after the message was already stored.
    """
    from telethon import errors
    from telethon.errors import rpcerrorlist as rpc

    classes: list[type[BaseException] | None] = [
        # asyncio.TimeoutError is TimeoutError on 3.11+; both are listed so the
        # intent survives a future divergence.
        asyncio.TimeoutError,
        TimeoutError,
        # ConnectionError is a subclass of OSError; the socket dying after the
        # request was written says nothing about what the server did with it.
        OSError,
        # The catch-all for Telegram's own errors. Anything not named in the
        # no-effect whitelist above is treated as ambiguous on purpose —
        # ``RpcCallFailError`` and friends can be raised after the change
        # landed, and guessing "it failed" is what produces duplicates.
        errors.RPCError,
        getattr(rpc, "TimeoutError", None),
    ]
    return tuple(dict.fromkeys(cls for cls in classes if isinstance(cls, type)))


# ---------------------------------------------------------------------------
# precondition comparison
# ---------------------------------------------------------------------------


def _check_peer(expected: dict[str, Any], actual: Resolved, *, what: str) -> list[str]:
    """Refuse if the handle now points somewhere else.

    The identity being compared is the numeric id, because that is the only
    part a stranger cannot acquire. A changed access hash is normal — Telegram
    re-issues them — so it produces a warning rather than a refusal.
    """
    warnings: list[str] = []
    expected_id = expected.get("peer_id")
    if expected_id != actual.ref.peer_id:
        raise PlanPreconditionFailed(
            f"{what} resolved to {actual.ref.peer_id} but the plan was written against "
            f"{expected_id}; the target changed since the plan was reviewed",
            suggestion="Reject this plan and create a new one against the current target.",
        )

    expected_name = expected.get("username")
    actual_name = actual.ref.username.lower() if actual.ref.username else None
    if expected_name != actual_name:
        raise PlanPreconditionFailed(
            f"{what} is now @{sanitize_line(str(actual_name), limit=64)} but the plan "
            f"recorded @{sanitize_line(str(expected_name), limit=64)}"
        )

    expected_invite = expected.get("invite_hash")
    if expected_invite and not actual.invite_hash:
        raise PlanPreconditionFailed(f"{what} no longer looks like the invite that was reviewed")
    if expected_invite and expected_id == 0 and actual.ref.peer_id != 0:
        raise PlanPreconditionFailed(
            f"{what}: this account is already a member; the plan described joining"
        )

    expected_hash = expected.get("access_hash")
    if (
        expected_hash is not None
        and actual.access_hash is not None
        and expected_hash != str(actual.access_hash)
    ):
        warnings.append(
            f"{what}: the access hash changed since planning (normal after a "
            "re-resolve; the numeric id still matches)"
        )
    return warnings


def _check_file(expected: dict[str, Any], actual: OutboundFile) -> list[str]:
    """Refuse if the bytes behind the path are not the ones that were reviewed.

    A plan records a *name*, and a name is not content. Between review and
    apply the file can be replaced, truncated or rewritten — by an editor, a
    sync client, or anybody who can write into the outbox — and the whole value
    of the approval is that what was read is what leaves. So the digest decides,
    and it is recomputed here rather than taken from the plan.

    The path is compared too, but only as a warning: the same bytes reached
    through a different name are the file that was approved.
    """
    warnings: list[str] = []
    if expected.get("sha256") != actual.sha256 or expected.get("size_bytes") != actual.size_bytes:
        raise PlanPreconditionFailed(
            f"the file at that path is not the one the plan was reviewed against: it is now "
            f"{actual.size_bytes} bytes ({human_bytes(actual.size_bytes)}) with digest "
            f"{actual.sha256}",
            suggestion="Reject this plan and create a new one for the file as it now stands.",
        )
    if expected.get("delivery") != str(actual.delivery):
        # Only reachable if the configuration changed the classification under a
        # pending plan. The reviewed line said "as a compressed photo" or "as a
        # document", and sending the other one is not what was approved.
        raise PlanPreconditionFailed(
            f"this file would now be sent {actual.delivery}, not "
            f"{expected.get('delivery')} as the plan recorded"
        )
    if expected.get("path") != str(actual.path):
        warnings.append(
            "the path resolves somewhere else than it did at planning time; the contents "
            "are identical, so the send is the one that was approved"
        )
    return warnings


def _check_messages(expected: list[dict[str, Any]], actual: list[Any]) -> None:
    """Refuse if the messages are not the ones that were reviewed."""
    from .ops.write import message_snapshot

    by_id = {int(snap["id"]): snap for snap in expected}
    if len(actual) != len(by_id):
        raise PlanPreconditionFailed(
            f"the plan referred to {len(by_id)} message(s); {len(actual)} are still there"
        )
    for message in actual:
        snap = by_id.get(int(message.id))
        if snap is None:
            raise PlanPreconditionFailed(f"message {message.id} was not part of this plan")
        current = message_snapshot(message)
        if current["body_sha256"] != snap["body_sha256"]:
            raise PlanPreconditionFailed(
                f"message {message.id} has been edited since the plan was reviewed"
            )
        if current["outgoing"] != snap["outgoing"]:
            raise PlanPreconditionFailed(
                f"message {message.id} no longer has the authorship the plan recorded"
            )


# ---------------------------------------------------------------------------
# verification: policy and preconditions, again
# ---------------------------------------------------------------------------


async def _verify(ctx: OperationContext, client: Any, plan: Plan, params: BaseModel) -> _Prepared:
    """Re-run every check the planner ran, against the world as it is now."""
    from .ops.write import (
        _fetch_messages,
        require_peer,
        require_planning_profile,
        resolve_join_target,
        resolve_message_ids,
        resolve_message_target,
        resolve_peer,
    )

    pre = plan.preconditions
    warnings: list[str] = []
    operation = plan.operation
    action = f"apply:{operation}"

    match operation:
        case "message.send" | "message.reply" | "chat.mark_read":
            require_planning_profile(ctx, Capability.SEND, action=action)
            answering: int | None = None
            if operation == "message.reply":
                # A reply may have been addressed by a link, which carries the
                # id of the message being answered; `resolve_message_target`
                # checks the peer itself, in the read side's order.
                chat, answering = await resolve_message_target(
                    ctx,
                    client,
                    chat=params.chat,  # type: ignore[attr-defined]
                    message_id=params.reply_to_message_id,  # type: ignore[attr-defined]
                    capability=Capability.SEND,
                    action=action,
                )
            else:
                chat = await resolve_peer(client, params.chat)  # type: ignore[attr-defined]
                require_peer(ctx, Capability.SEND, chat.ref, action=action)
            warnings += _check_peer(pre["peer"], chat, what="chat")
            prepared = _Prepared(
                limit_target=str(chat.ref.peer_id),
                peers={"chat": chat},
                audit_peer_id=chat.ref.peer_id,
                audit_body=getattr(params, "text", None),
                warnings=warnings,
                message_ids=[answering] if answering is not None else [],
            )
            if operation == "message.reply":
                original = await _fetch_messages(client, chat, [answering])
                _check_messages([pre["reply_to"]], original)
            return prepared

        case "message.schedule":
            from .ops.schedule import recheck_schedule

            require_planning_profile(ctx, Capability.SEND, action=action)
            chat = await resolve_peer(client, params.chat)  # type: ignore[attr-defined]
            require_peer(ctx, Capability.SEND, chat.ref, action=action)
            warnings += _check_peer(pre["peer"], chat, what="chat")
            # The reviewed *time*, not only the reviewed chat. Telegram sends a
            # scheduled message whose moment has already passed immediately, so
            # a plan applied too late would fire now rather than be late.
            recheck_schedule(pre, params)  # type: ignore[arg-type]
            return _Prepared(
                limit_target=str(chat.ref.peer_id),
                peers={"chat": chat},
                audit_peer_id=chat.ref.peer_id,
                audit_body=getattr(params, "text", None),
                warnings=warnings,
            )

        case "chat.archive" | "chat.mute":
            from .ops.quiet import recheck_archive, recheck_mute

            require_planning_profile(ctx, Capability.SEND, action=action)
            chat = await resolve_peer(client, params.chat)  # type: ignore[attr-defined]
            require_peer(ctx, Capability.SEND, chat.ref, action=action)
            warnings += _check_peer(pre["peer"], chat, what="chat")
            if operation == "chat.archive":
                recheck_archive(pre, params)  # type: ignore[arg-type]
            else:
                recheck_mute(pre, params)  # type: ignore[arg-type]
            return _Prepared(
                limit_target=str(chat.ref.peer_id),
                peers={"chat": chat},
                audit_peer_id=chat.ref.peer_id,
                warnings=warnings,
            )

        case "message.send_file":
            require_planning_profile(ctx, Capability.SEND, action=action)
            chat = await resolve_peer(client, params.chat)  # type: ignore[attr-defined]
            require_peer(ctx, Capability.SEND, chat.ref, action=action)
            warnings += _check_peer(pre["peer"], chat, what="chat")
            # Resolved again, not read from the plan: the outbox rule has to
            # hold at the moment of sending, and the configuration may have been
            # narrowed since — a path that was permitted when the plan was
            # written must not be sent from a directory that is no longer one.
            attachment = resolve_outbound(
                ctx.settings,
                params.path,  # type: ignore[attr-defined]
                as_document=params.as_document,  # type: ignore[attr-defined]
            )
            warnings += _check_file(pre["file"], attachment)
            reply_to = params.reply_to_message_id  # type: ignore[attr-defined]
            if reply_to is not None:
                original = await _fetch_messages(client, chat, [reply_to])
                _check_messages([pre["reply_to"]], original)
            return _Prepared(
                limit_target=str(chat.ref.peer_id),
                peers={"chat": chat},
                attachment=attachment,
                audit_peer_id=chat.ref.peer_id,
                audit_body=getattr(params, "caption", None) or None,
                warnings=warnings,
            )

        case "message.edit" | "message.delete":
            require_planning_profile(ctx, Capability.SEND, action=action)
            # Addressed from the arguments again, not from the plan: the chat may
            # have arrived as a link, and the id inside it is re-derived for the
            # same reason the peer is re-resolved.
            if operation == "message.edit":
                chat, chosen = await resolve_message_target(
                    ctx,
                    client,
                    chat=params.chat,  # type: ignore[attr-defined]
                    message_id=params.message_id,  # type: ignore[attr-defined]
                    capability=Capability.SEND,
                    action=action,
                )
                ids = [chosen]
            else:
                chat, ids = await resolve_message_ids(
                    ctx,
                    client,
                    chat=params.chat,  # type: ignore[attr-defined]
                    message_ids=params.message_ids,  # type: ignore[attr-defined]
                    capability=Capability.SEND,
                    action=action,
                )
            warnings += _check_peer(pre["peer"], chat, what="chat")
            messages = await _fetch_messages(client, chat, ids)
            expected = [pre["message"]] if operation == "message.edit" else pre["messages"]
            _check_messages(expected, messages)
            # Authorship is re-checked rather than trusted from the snapshot:
            # the snapshot says what was true, the message says what is.
            for message in messages:
                if not getattr(message, "out", False):
                    raise PlanPreconditionFailed(
                        f"message {message.id} was not sent by this account"
                    )
            return _Prepared(
                limit_target=str(chat.ref.peer_id),
                peers={"chat": chat},
                messages=messages,
                audit_peer_id=chat.ref.peer_id,
                audit_body=getattr(params, "text", None),
                warnings=warnings,
                message_ids=ids,
            )

        case "message.react" | "message.unreact" | "message.pin" | "message.unpin":
            return await _verify_mark(ctx, client, plan, params, warnings)

        case "message.forward":
            require_planning_profile(ctx, Capability.SEND, action=action)
            source = await resolve_peer(client, params.source_chat)  # type: ignore[attr-defined]
            destination = await resolve_peer(
                client,
                params.destination_chat,  # type: ignore[attr-defined]
            )
            require_peer(ctx, Capability.READ_CHAT, source.ref, action=action)
            require_peer(ctx, Capability.SEND, destination.ref, action=action)
            warnings += _check_peer(pre["source"], source, what="source chat")
            warnings += _check_peer(pre["destination"], destination, what="destination chat")
            messages = await _fetch_messages(
                client,
                source,
                list(params.message_ids),  # type: ignore[attr-defined]
            )
            _check_messages(pre["messages"], messages)
            return _Prepared(
                limit_target=str(destination.ref.peer_id),
                peers={"source": source, "destination": destination},
                messages=messages,
                audit_peer_id=destination.ref.peer_id,
                warnings=warnings,
            )

        case "chat.join":
            require_planning_profile(ctx, Capability.JOIN, action=action)
            target = await resolve_join_target(client, params.target)  # type: ignore[attr-defined]
            require_peer(ctx, Capability.JOIN, target.ref, action=action)
            warnings += _check_peer(pre["peer"], target, what="chat")
            return _Prepared(
                limit_target=str(target.invite_hash or target.ref.peer_id),
                peers={"chat": target},
                audit_peer_id=target.ref.peer_id or None,
                warnings=warnings,
            )

        case "chat.leave":
            require_planning_profile(ctx, Capability.JOIN, action=action)
            chat = await resolve_peer(client, params.chat)  # type: ignore[attr-defined]
            require_peer(ctx, Capability.JOIN, chat.ref, action=action)
            warnings += _check_peer(pre["peer"], chat, what="chat")
            return _Prepared(
                limit_target=str(chat.ref.peer_id),
                peers={"chat": chat},
                audit_peer_id=chat.ref.peer_id,
                warnings=warnings,
            )

        case "chat.create":
            require_planning_profile(ctx, Capability.ADMIN, action=action)
            ctx.safety.require_group_creation()
            peers: dict[str, Resolved] = {}
            if pre.get("kind", "supergroup") != params.kind:  # type: ignore[attr-defined]
                raise PlanPreconditionFailed(
                    "the kind of chat recorded in the plan differs from the one being "
                    "created; a supergroup and a channel are not the same approval"
                )
            for index, user in enumerate(params.users):  # type: ignore[attr-defined]
                resolved = await resolve_peer(client, user)
                require_peer(ctx, Capability.ADMIN, resolved.ref, action=action)
                warnings += _check_peer(pre["users"][index], resolved, what=f"member {index + 1}")
                peers[f"user:{index}"] = resolved
            return _Prepared(
                # A group that does not exist yet has no target, so the account
                # itself is the scope this draws against.
                limit_target=plan.account,
                peers=peers,
                warnings=warnings,
            )

        case (
            "chat.invite"
            | "chat.promote"
            | "chat.ban"
            | "chat.unban"
            | "chat.kick"
            | "chat.restrict"
            | "chat.demote"
        ):
            require_planning_profile(ctx, Capability.ADMIN, action=action)
            chat = await resolve_peer(client, params.chat)  # type: ignore[attr-defined]
            user = await resolve_peer(client, params.user)  # type: ignore[attr-defined]
            require_peer(ctx, Capability.ADMIN, chat.ref, action=action)
            require_peer(ctx, Capability.ADMIN, user.ref, action=action)
            warnings += _check_peer(pre["chat"], chat, what="chat")
            warnings += _check_peer(pre["user"], user, what="user")
            if operation == "chat.promote":
                asked = params.rights.model_dump()  # type: ignore[attr-defined]
                if pre.get("rights") != asked:
                    raise PlanPreconditionFailed(
                        "the rights recorded in the plan differ from the ones being applied"
                    )
            if operation == "chat.restrict":
                # Both halves of what was reviewed: which rights go, and for how
                # long. A duration silently widened between review and apply is
                # the same class of mistake as a changed right.
                taken = params.restrictions.model_dump()  # type: ignore[attr-defined]
                duration = params.duration_seconds  # type: ignore[attr-defined]
                if pre.get("restrictions") != taken or pre.get("duration_seconds") != duration:
                    raise PlanPreconditionFailed(
                        "the restrictions recorded in the plan differ from the ones being applied"
                    )
            return _Prepared(
                limit_target=str(chat.ref.peer_id),
                peers={"chat": chat, "user": user},
                audit_peer_id=chat.ref.peer_id,
                warnings=warnings,
            )

        case "account.profile":
            require_planning_profile(ctx, Capability.PROFILE, action=action)
            ctx.safety.require_profile_change()
            return _Prepared(limit_target=plan.account, warnings=warnings)

        case "account.block" | "account.unblock":
            from .ops.settings import require_person

            require_planning_profile(ctx, Capability.ADMIN, action=action)
            user = await resolve_peer(client, params.user)  # type: ignore[attr-defined]
            require_peer(ctx, Capability.ADMIN, user.ref, action=action)
            # Re-checked rather than trusted from the plan: the block list holds
            # people, and the id is resolved again here.
            require_person(user, action=action)
            warnings += _check_peer(pre["user"], user, what="user")
            return _Prepared(
                limit_target=str(user.ref.peer_id),
                peers={"user": user},
                audit_peer_id=user.ref.peer_id,
                warnings=warnings,
            )

        case "chat.set_title" | "chat.set_about" | "chat.set_photo":
            return await _verify_chat_setting(ctx, client, plan, params, warnings)

    raise InvalidInput(f"no applier for operation {sanitize_line(operation, limit=64)}")


async def _verify_chat_setting(
    ctx: OperationContext,
    client: Any,
    plan: Plan,
    params: BaseModel,
    warnings: list[str],
) -> _Prepared:
    """Re-check a chat identity change, including the value it overwrites.

    The peer check is the same one every admin operation runs. What is specific
    here is the *current* value: the summary a person approved quoted it, and
    Telegram keeps no copy once it is replaced — so a title, a description or a
    photo that moved between review and apply means the reviewed sentence no
    longer describes what would be lost, and the plan is refused rather than
    applied against a value nobody saw.
    """
    from .ops.settings import (
        current_about,
        current_photo_id,
        require_chat_peer,
        require_photo_file,
    )
    from .ops.write import require_peer, require_planning_profile, resolve_peer, text_digest

    pre = plan.preconditions
    operation = plan.operation
    action = f"apply:{operation}"

    require_planning_profile(ctx, Capability.ADMIN, action=action)
    chat = await resolve_peer(client, params.chat)  # type: ignore[attr-defined]
    require_peer(ctx, Capability.ADMIN, chat.ref, action=action)
    require_chat_peer(chat, action=action)
    warnings += _check_peer(pre["peer"], chat, what="chat")

    attachment: OutboundFile | None = None
    match operation:
        case "chat.set_title":
            if pre.get("current_title_sha256") != text_digest(chat.ref.title or ""):
                raise PlanPreconditionFailed(
                    "the chat has been renamed since the plan was reviewed; the title the "
                    "summary showed is not the one that would be overwritten",
                    suggestion="Reject this plan and create a new one against the current title.",
                )
        case "chat.set_about":
            if pre.get("current_about_sha256") != text_digest(await current_about(client, chat)):
                raise PlanPreconditionFailed(
                    "the chat's description has changed since the plan was reviewed"
                )
        case _:
            # Resolved again through the outbox rule, not read from the plan: a
            # path that was permitted when the plan was written must not be
            # published from a directory the configuration has since closed.
            attachment = resolve_outbound(ctx.settings, params.path)  # type: ignore[attr-defined]
            require_photo_file(attachment)
            warnings += _check_file(pre["file"], attachment)
            if pre.get("current_photo_id") != await current_photo_id(client, chat):
                raise PlanPreconditionFailed(
                    "the chat's photo has changed since the plan was reviewed; the one the "
                    "summary described is not the one that would be replaced",
                    suggestion="Reject this plan and create a new one against the current photo.",
                )

    return _Prepared(
        limit_target=str(chat.ref.peer_id),
        peers={"chat": chat},
        attachment=attachment,
        audit_peer_id=chat.ref.peer_id,
        warnings=warnings,
    )


async def _verify_mark(
    ctx: OperationContext,
    client: Any,
    plan: Plan,
    params: BaseModel,
    warnings: list[str],
) -> _Prepared:
    """Re-check a reaction or a pin against the world as it is now.

    Split out of :func:`_verify` because the four share every step, and because
    two of those steps have nothing to do with the peer or the message text:

    **The reactions this account had are compared.** Telegram's reaction call
    takes the account's complete list for a message, never a delta. A plan that
    said "add 🎉 alongside 👍" and is applied after 👍 was dropped from another
    device would silently send a list nobody reviewed — so a change in what this
    account had reacted with is a failed precondition, not a detail.

    **The pinned state is compared.** "Pin this" applied to something already
    pinned re-notifies the whole chat for nothing; "unpin this" applied to
    something already unpinned is an act on a state that no longer exists.

    The final reaction list is computed *here*, before the rate-limit slot and
    the audit record. An input error raised after those would land on the wrong
    side of the line where a request may already have left.
    """
    from .ops.marks import (
        chosen_reactions,
        final_reactions,
        media_fingerprint,
        remaining_reactions,
        resolve_message,
        same_reactions,
    )
    from .ops.write import require_planning_profile

    operation = plan.operation
    action = f"apply:{operation}"
    pre = plan.preconditions
    reacting = operation in {"message.react", "message.unreact"}
    capability = Capability.SEND if reacting else Capability.ADMIN

    require_planning_profile(ctx, capability, action=action)
    chat, message = await resolve_message(
        ctx,
        client,
        chat=params.chat,  # type: ignore[attr-defined]
        message_id=params.message_id,  # type: ignore[attr-defined]
        capability=capability,
        action=action,
    )
    warnings += _check_peer(pre["peer"], chat, what="chat")
    _check_messages([pre["message"]], [message])
    # The shared snapshot digests the *body*, which is empty for every
    # caption-less photo. These four act on other people's messages, where an
    # edit can swap the attachment and leave that digest untouched.
    if media_fingerprint(message) != pre.get("media"):
        raise PlanPreconditionFailed(
            f"the attachment on message {message.id} is not the one the plan was "
            "reviewed against; the message was edited since",
            suggestion="Reject this plan and create a new one against the current message.",
        )

    reactions: list[dict[str, Any]] | None = None
    if reacting:
        existing = chosen_reactions(message)
        if not same_reactions(existing, pre["existing"]):
            raise PlanPreconditionFailed(
                "this account's reactions on that message have changed since the plan "
                "was reviewed; the reaction call replaces the whole list, so applying "
                "it now would discard something nobody looked at",
                suggestion="Reject this plan and create a new one against the current state.",
            )
        wanted = _requested_reaction(params)
        try:
            if operation == "message.unreact":
                reactions = remaining_reactions(existing, wanted)
            elif wanted is None:  # pragma: no cover - the input model forbids it
                raise PlanPreconditionFailed("the plan names no reaction to add")
            else:
                reactions = final_reactions(
                    existing,
                    wanted,
                    keep_existing=params.keep_existing,  # type: ignore[attr-defined]
                )
        except InvalidInput as exc:
            # Reached only where the world moved in a way the comparison above
            # did not catch. Reported as a precondition failure rather than an
            # input error, so the plan is closed cleanly instead of surfacing as
            # a traceback from the verification path.
            raise PlanPreconditionFailed(exc.message) from exc
    else:
        expected_pinned = bool(pre["pinned"])
        if bool(getattr(message, "pinned", False)) != expected_pinned:
            state = "already pinned" if operation == "message.pin" else "no longer pinned"
            raise PlanPreconditionFailed(
                f"message {message.id} is {state}; the plan was written against the opposite state"
            )

    return _Prepared(
        limit_target=str(chat.ref.peer_id),
        peers={"chat": chat},
        messages=[message],
        audit_peer_id=chat.ref.peer_id,
        warnings=warnings,
        reactions=reactions,
    )


def _requested_reaction(params: BaseModel) -> dict[str, Any] | None:
    """The reaction named in a plan's params, or ``None`` for "all of them"."""
    from .ops.marks import requested_reaction

    emoji = getattr(params, "emoji", None)
    custom = getattr(params, "custom_emoji_id", None)
    if emoji is None and custom is None:
        return None
    return requested_reaction(emoji=emoji, custom_emoji_id=custom)


# ---------------------------------------------------------------------------
# the RPCs
# ---------------------------------------------------------------------------


async def _execute(
    client: Any, plan: Plan, params: BaseModel, prepared: _Prepared
) -> tuple[dict[str, Any], list[str]]:
    """Perform the one remote action this plan describes."""
    from telethon import errors
    from telethon.tl import functions, types

    warnings: list[str] = []
    operation = plan.operation

    match operation:
        case "message.send":
            message = await client.send_message(
                prepared.peers["chat"].ref.peer_id,
                params.text,  # type: ignore[attr-defined]
                silent=params.silent,  # type: ignore[attr-defined]
                link_preview=params.link_preview,  # type: ignore[attr-defined]
            )
            return {"message_id": int(message.id)}, warnings

        case "message.reply":
            message = await client.send_message(
                prepared.peers["chat"].ref.peer_id,
                params.text,  # type: ignore[attr-defined]
                # The id verification settled on, not the argument: the chat may
                # have been a link, in which case the argument is None.
                reply_to=prepared.message_ids[0],
                silent=params.silent,  # type: ignore[attr-defined]
                link_preview=params.link_preview,  # type: ignore[attr-defined]
            )
            return {"message_id": int(message.id)}, warnings

        case "message.send_file":
            attachment = prepared.attachment
            if attachment is None:  # pragma: no cover - _verify always sets it
                raise InvalidInput("the file to send was never resolved; refusing to upload")
            message = await client.send_file(
                prepared.peers["chat"].ref.peer_id,
                str(attachment.path),
                caption=params.caption or None,  # type: ignore[attr-defined]
                # The one instruction Telethon always obeys, and the only reason
                # the summary can promise the bytes arrive untouched.
                force_document=attachment.as_document,
                voice_note=attachment.delivery is Delivery.VOICE,
                reply_to=params.reply_to_message_id,  # type: ignore[attr-defined]
                silent=params.silent,  # type: ignore[attr-defined]
            )
            return {
                "message_id": int(message.id),
                "file": attachment.name,
                "bytes": attachment.size_bytes,
                "sha256": attachment.sha256,
                "delivery": str(attachment.delivery),
            }, warnings

        case "message.edit":
            asked_id = prepared.message_ids[0]
            message = await client.edit_message(
                prepared.peers["chat"].ref.peer_id,
                asked_id,
                params.text,  # type: ignore[attr-defined]
            )
            return {"message_id": int(getattr(message, "id", None) or asked_id)}, warnings

        case "message.delete":
            await client.delete_messages(
                prepared.peers["chat"].ref.peer_id,
                list(prepared.message_ids),
                revoke=params.revoke,  # type: ignore[attr-defined]
            )
            return {"deleted": len(prepared.message_ids)}, warnings

        case "message.forward":
            sent = await client.forward_messages(
                prepared.peers["destination"].ref.peer_id,
                list(params.message_ids),  # type: ignore[attr-defined]
                prepared.peers["source"].ref.peer_id,
                silent=params.silent,  # type: ignore[attr-defined]
                drop_author=params.drop_author,  # type: ignore[attr-defined]
            )
            sent_list = sent if isinstance(sent, list) else [sent]
            return {"message_ids": [int(m.id) for m in sent_list if m is not None]}, warnings

        case "message.schedule":
            from .ops.schedule import schedule_date

            scheduled = await client.send_message(
                prepared.peers["chat"].ref.peer_id,
                params.text,  # type: ignore[attr-defined]
                # One integer, computed by the same function the planner and the
                # preconditions used — including the sentinel that means "when
                # they are next online" rather than a date in 2038.
                schedule=schedule_date(params),  # type: ignore[arg-type]
                silent=params.silent,  # type: ignore[attr-defined]
                link_preview=params.link_preview,  # type: ignore[attr-defined]
            )
            # A scheduled send answers with an id from the chat's *scheduled*
            # sequence, and older layers may answer with nothing this library
            # can turn into a message. The queue entry exists either way, so a
            # missing id is reported as missing rather than raised — raising
            # here would file a successful schedule as an unknown outcome.
            scheduled_id = getattr(scheduled, "id", None)
            at = params.at  # type: ignore[attr-defined]
            if params.when_online:  # type: ignore[attr-defined]
                # The queue is what makes this operation worth having, and this
                # is the one mode that may skip it: Telegram sends immediately
                # to somebody who is already online, leaving nothing to cancel.
                warnings.append(
                    "sent in 'when online' mode: if the recipient was already online it has "
                    "gone out now rather than waiting in the scheduled queue"
                )
            return {
                "scheduled": True,
                "message_id": int(scheduled_id) if scheduled_id is not None else None,
                "send_when_online": bool(params.when_online),  # type: ignore[attr-defined]
                "scheduled_for": None if at is None else at.isoformat(),
            }, warnings

        case "chat.archive":
            from .ops.quiet import folder_id_for

            peer = await client.get_input_entity(prepared.peers["chat"].ref.peer_id)
            folder_id = folder_id_for(params)  # type: ignore[arg-type]
            await client(
                functions.folders.EditPeerFoldersRequest(
                    folder_peers=[types.InputFolderPeer(peer=peer, folder_id=folder_id)]
                )
            )
            return {"archived": bool(params.archived)}, warnings  # type: ignore[attr-defined]

        case "chat.mute":
            from .ops.quiet import mute_until

            peer = await client.get_input_entity(prepared.peers["chat"].ref.peer_id)
            # Computed here, against the clock of the moment this is applied: the
            # plan carries a duration, not a deadline.
            until = mute_until(params)  # type: ignore[arg-type]
            await client(
                functions.account.UpdateNotifySettingsRequest(
                    peer=types.InputNotifyPeer(peer=peer),
                    settings=types.InputPeerNotifySettings(mute_until=until),
                )
            )
            return {"muted": bool(params.muted), "mute_until": until or None}, warnings  # type: ignore[attr-defined]

        case "chat.mark_read":
            peer = await client.get_input_entity(prepared.peers["chat"].ref.peer_id)
            max_id = params.max_message_id or 0  # type: ignore[attr-defined]
            # The raw requests are used rather than the convenience wrapper so
            # that acknowledging a chat is always an explicit, planned act and
            # never something a read path can reach by accident.
            if isinstance(peer, types.InputPeerChannel):
                await client(functions.channels.ReadHistoryRequest(channel=peer, max_id=max_id))
            else:
                await client(functions.messages.ReadHistoryRequest(peer=peer, max_id=max_id))
            return {"marked_read_up_to": max_id or "latest"}, warnings

        case "message.react" | "message.unreact":
            peer = await client.get_input_entity(prepared.peers["chat"].ref.peer_id)
            message = prepared.messages[0]
            wanted = list(prepared.reactions or [])
            await client(
                functions.messages.SendReactionRequest(
                    peer=peer,
                    msg_id=int(message.id),
                    # The account's whole list, because that is what Telegram's
                    # call means. An empty one takes every reaction off.
                    reaction=[_reaction_object(one) for one in wanted],
                    big=bool(getattr(params, "big", False)),
                    # Off deliberately. Adding to the owner's "recently used"
                    # row reorders a control on their own phone, which is an
                    # invisible side effect of an action they approved for a
                    # different reason.
                    add_to_recent=False,
                )
            )
            return {
                "message_id": int(message.id),
                "reactions": wanted,
            }, warnings

        case "message.pin" | "message.unpin":
            peer = await client.get_input_entity(prepared.peers["chat"].ref.peer_id)
            message = prepared.messages[0]
            unpin = operation == "message.unpin"
            private = prepared.peers["chat"].ref.is_private
            # `pm_oneside` exists only for one-to-one chats, where it means
            # "leave the other person's window alone". Pinning defaults to that;
            # unpinning never uses it, because a pin removed on one side only is
            # a banner still sitting at the top of somebody else's chat, which is
            # not what the plan said would happen.
            oneside = private and not unpin and not bool(getattr(params, "both_sides", False))
            await client(
                functions.messages.UpdatePinnedMessageRequest(
                    peer=peer,
                    id=int(message.id),
                    # Telegram never announces an unpin, so the flag is only
                    # meaningful on the way in.
                    silent=bool(getattr(params, "silent", False)) or unpin,
                    unpin=unpin,
                    pm_oneside=oneside,
                )
            )
            return {"message_id": int(message.id), "pinned": not unpin}, warnings

        case "chat.join":
            target = prepared.peers["chat"]
            if target.invite_hash and target.ref.peer_id == 0:
                try:
                    await client(
                        functions.messages.ImportChatInviteRequest(hash=target.invite_hash)
                    )
                except errors.InviteRequestSentError:
                    # An effect did happen: a join request is now pending with
                    # the chat's admins. Reporting this as a failure would be
                    # wrong in both directions — nothing to retry, and the
                    # request is real.
                    warnings.append("a join request was filed and awaits admin approval")
                    return {"joined": False, "requested": True}, warnings
                return {"joined": True}, warnings
            channel = await client.get_input_entity(target.ref.peer_id)
            await client(functions.channels.JoinChannelRequest(channel=channel))
            return {"joined": True}, warnings

        case "chat.leave":
            peer = await client.get_input_entity(prepared.peers["chat"].ref.peer_id)
            if isinstance(peer, types.InputPeerChannel):
                await client(functions.channels.LeaveChannelRequest(channel=peer))
            else:
                await client(
                    functions.messages.DeleteChatUserRequest(
                        chat_id=peer.chat_id, user_id=types.InputUserSelf()
                    )
                )
            return {"left": True}, warnings

        case "chat.create":
            result = await client(
                functions.channels.CreateChannelRequest(
                    title=params.title,  # type: ignore[attr-defined]
                    about=params.about,  # type: ignore[attr-defined]
                    # Exactly one of the two, from the kind the plan recorded and
                    # verification compared. Neither flag makes it public: a chat
                    # is findable only once somebody gives it a username.
                    megagroup=params.kind == "supergroup",  # type: ignore[attr-defined]
                    broadcast=params.kind == "channel",  # type: ignore[attr-defined]
                )
            )
            created = result.chats[0]
            from .ops.write import reference

            chat_ref = reference(created)
            outcome: dict[str, Any] = {"chat_id": chat_ref.ref.peer_id, "created": True}
            # Sorted numerically, not lexically: "user:10" must not come before
            # "user:2", or the invite order stops matching the reviewed plan.
            keys = sorted(
                (key for key in prepared.peers if key.startswith("user:")),
                key=lambda key: int(key.split(":", 1)[1]),
            )
            invitees = [prepared.peers[key] for key in keys]
            if invitees:
                channel = await client.get_input_entity(chat_ref.ref.peer_id)
                users = [await client.get_input_entity(r.ref.peer_id) for r in invitees]
                invite_result = await client(
                    functions.channels.InviteToChannelRequest(channel=channel, users=users)
                )
                missing = _missing_invitees(invite_result)
                outcome["invited"] = len(users) - len(missing)
                if missing:
                    # The group exists, so the plan did what it said. The people
                    # who were not added are reported rather than swallowed —
                    # "created" must not be read as "everyone is in it".
                    outcome["not_invited"] = missing
                    warnings.append(
                        f"{len(missing)} user(s) were not added: their privacy settings "
                        "do not allow being added to groups"
                    )
            return outcome, warnings

        case "chat.invite":
            chat_peer = await client.get_input_entity(prepared.peers["chat"].ref.peer_id)
            user_peer = await client.get_input_entity(prepared.peers["user"].ref.peer_id)
            if isinstance(chat_peer, types.InputPeerChannel):
                result = await client(
                    functions.channels.InviteToChannelRequest(channel=chat_peer, users=[user_peer])
                )
            else:
                result = await client(
                    functions.messages.AddChatUserRequest(
                        chat_id=chat_peer.chat_id, user_id=user_peer, fwd_limit=0
                    )
                )
            missing = _missing_invitees(result)
            if missing:
                # Telegram reports this refusal inside a *successful* response,
                # not as an exception. Reading only the status would report a
                # success where nobody was invited.
                raise PrivacyRestricted(
                    "Telegram did not add the user: their privacy settings do not allow "
                    "being added to chats",
                    suggestion="Send them an invite link instead.",
                    details={"missing_invitees": missing},
                )
            return {"invited": True}, warnings

        case "chat.promote":
            chat_peer = await client.get_input_entity(prepared.peers["chat"].ref.peer_id)
            user_peer = await client.get_input_entity(prepared.peers["user"].ref.peer_id)
            rights = params.rights  # type: ignore[attr-defined]
            if isinstance(chat_peer, types.InputPeerChannel):
                await client(
                    functions.channels.EditAdminRequest(
                        channel=chat_peer,
                        user_id=user_peer,
                        admin_rights=types.ChatAdminRights(
                            change_info=rights.change_info,
                            delete_messages=rights.delete_messages,
                            ban_users=rights.ban_users,
                            invite_users=rights.invite_users,
                            pin_messages=rights.pin_messages,
                            add_admins=rights.add_admins,
                            manage_call=rights.manage_call,
                            # Was accepted in the input model and then dropped
                            # here, so a promotion that granted it silently did
                            # not — the plan said one thing and Telegram was
                            # told another.
                            manage_topics=rights.manage_topics,
                            # Never anonymous: an admin action nobody can
                            # attribute defeats the log this project keeps.
                            anonymous=False,
                        ),
                        rank=params.rank,  # type: ignore[attr-defined]
                    )
                )
            else:
                await client(
                    functions.messages.EditChatAdminRequest(
                        chat_id=chat_peer.chat_id, user_id=user_peer, is_admin=True
                    )
                )
                warnings.append(
                    "this is a basic group, where admin rights are all-or-nothing; "
                    "the individual rights in the plan could not be applied separately"
                )
            return {"promoted": True}, warnings

        case "chat.ban":
            chat_peer, user_peer = await _member_peers(client, prepared)
            if isinstance(chat_peer, types.InputPeerChannel):
                await client(
                    functions.channels.EditBannedRequest(
                        channel=chat_peer,
                        participant=user_peer,
                        # No until_date: a ban with no end is what the plan said,
                        # and Telegram reads a missing date as "forever".
                        banned_rights=types.ChatBannedRights(until_date=None, view_messages=True),
                    )
                )
                return {"banned": True}, warnings
            await client(
                functions.messages.DeleteChatUserRequest(
                    chat_id=chat_peer.chat_id, user_id=user_peer
                )
            )
            # A basic group keeps no ban list at all. Reporting "banned" here
            # would tell a person the door is locked when any member can open it.
            warnings.append(
                "this is a basic group, which keeps no ban list: the person was removed, "
                "and any member can add them back"
            )
            return {"banned": False, "removed": True}, warnings

        case "chat.unban":
            chat_peer, user_peer = await _member_peers(client, prepared)
            if not isinstance(chat_peer, types.InputPeerChannel):
                # Refused before any request leaves, so the applier's own
                # bookkeeping treats it as the refusal it is rather than as an
                # attempt whose outcome nobody knows.
                raise PlanPreconditionFailed(
                    "this is a basic group: it keeps no ban list, so there is nothing to lift",
                    suggestion="Invite the person back instead, or convert the group first.",
                )
            await client(
                functions.channels.EditBannedRequest(
                    channel=chat_peer,
                    participant=user_peer,
                    # Every flag off: the one request that clears a ban also
                    # clears every restriction, because Telegram keeps them in
                    # the same object.
                    banned_rights=types.ChatBannedRights(until_date=None),
                )
            )
            return {"unbanned": True}, warnings

        case "chat.kick":
            chat_peer, user_peer = await _member_peers(client, prepared)
            if isinstance(chat_peer, types.InputPeerChannel):
                # A supergroup has no "remove without banning": the documented
                # way is to ban and immediately lift it. The second request is
                # the whole difference between a kick and a ban, so it is issued
                # here explicitly — if it fails, the person is left banned and
                # the failure says so rather than reporting a kick.
                await client(
                    functions.channels.EditBannedRequest(
                        channel=chat_peer,
                        participant=user_peer,
                        banned_rights=types.ChatBannedRights(until_date=None, view_messages=True),
                    )
                )
                try:
                    await client(
                        functions.channels.EditBannedRequest(
                            channel=chat_peer,
                            participant=user_peer,
                            banned_rights=types.ChatBannedRights(until_date=None),
                        )
                    )
                except Exception as exc:
                    # The half-done state this operation is built out of, and the
                    # one case the applier's error taxonomy would otherwise get
                    # backwards: a FloodWaitError here is in the "no effect"
                    # whitelist, so the plan would be closed as failed, the slot
                    # refunded — and the person would still be banned. Raised as
                    # an unknown outcome instead, which keeps the budget spent and
                    # tells a person exactly what to go and look at.
                    raise PlanUnknownOutcome(
                        "the ban went through but lifting it did not: the person is "
                        f"BANNED, not kicked ({type(exc).__name__}: "
                        f"{sanitize_line(str(exc), limit=200)})",
                        suggestion=(
                            "Check the chat, then plan chat.unban for the same person "
                            "to finish what this kick started."
                        ),
                    ) from exc
                return {"removed": True}, warnings
            await client(
                functions.messages.DeleteChatUserRequest(
                    chat_id=chat_peer.chat_id, user_id=user_peer
                )
            )
            return {"removed": True}, warnings

        case "chat.restrict":
            chat_peer, user_peer = await _member_peers(client, prepared)
            if not isinstance(chat_peer, types.InputPeerChannel):
                raise PlanPreconditionFailed(
                    "this is a basic group: Telegram keeps no per-member rights there",
                    suggestion="Plan chat.kick instead, or convert the group to a supergroup.",
                )
            taken = params.restrictions  # type: ignore[attr-defined]
            duration = params.duration_seconds  # type: ignore[attr-defined]
            # Dated here, not at planning time: a plan can wait in the review
            # queue for hours, and an absolute date written then would already
            # be in the past by the time somebody approves it.
            until = datetime.now(UTC) + timedelta(seconds=duration) if duration else None
            await client(
                functions.channels.EditBannedRequest(
                    channel=chat_peer,
                    participant=user_peer,
                    banned_rights=types.ChatBannedRights(
                        until_date=until,
                        # Emphatically not a ban: they stay and keep reading.
                        view_messages=False,
                        send_messages=taken.send_messages,
                        # One asked-for flag, four of Telegram's: "no media"
                        # that still allowed GIFs, games and inline results
                        # would be a preview nobody could rely on.
                        send_media=taken.send_media,
                        send_gifs=taken.send_media,
                        send_games=taken.send_media,
                        send_inline=taken.send_media,
                        send_stickers=taken.send_stickers,
                        send_polls=taken.send_polls,
                        embed_links=taken.embed_links,
                        invite_users=taken.invite_users,
                        pin_messages=taken.pin_messages,
                        change_info=taken.change_info,
                    ),
                )
            )
            return {
                "restricted": True,
                "until": until.isoformat() if until else None,
            }, warnings

        case "chat.demote":
            chat_peer, user_peer = await _member_peers(client, prepared)
            if isinstance(chat_peer, types.InputPeerChannel):
                await client(
                    functions.channels.EditAdminRequest(
                        channel=chat_peer,
                        user_id=user_peer,
                        # Every right off is how Telegram spells "not an admin".
                        admin_rights=types.ChatAdminRights(
                            change_info=False,
                            post_messages=False,
                            edit_messages=False,
                            delete_messages=False,
                            ban_users=False,
                            invite_users=False,
                            pin_messages=False,
                            add_admins=False,
                            manage_call=False,
                            manage_topics=False,
                            anonymous=False,
                        ),
                        # Clearing the rank as well: a custom title left behind
                        # still says "moderator" next to somebody who is not one.
                        rank="",
                    )
                )
                return {"demoted": True}, warnings
            await client(
                functions.messages.EditChatAdminRequest(
                    chat_id=chat_peer.chat_id, user_id=user_peer, is_admin=False
                )
            )
            return {"demoted": True}, warnings

        case "account.profile":
            fields = {
                name: value
                for name, value in (
                    ("first_name", params.first_name),  # type: ignore[attr-defined]
                    ("last_name", params.last_name),  # type: ignore[attr-defined]
                    ("about", params.about),  # type: ignore[attr-defined]
                )
                if value is not None
            }
            await client(functions.account.UpdateProfileRequest(**fields))
            return {"changed": sorted(fields)}, warnings

        case "account.block" | "account.unblock":
            user_peer = await client.get_input_entity(prepared.peers["user"].ref.peer_id)
            blocking = operation == "account.block"
            request = (
                functions.contacts.BlockRequest if blocking else functions.contacts.UnblockRequest
            )
            await client(request(id=user_peer))
            # Reported as the state the account is now in rather than as
            # "unblocked: true", so both operations answer the same question.
            return {"blocked": blocking}, warnings

        case "chat.set_title":
            peer = await client.get_input_entity(prepared.peers["chat"].ref.peer_id)
            title = params.title  # type: ignore[attr-defined]
            if isinstance(peer, types.InputPeerChannel):
                await client(functions.channels.EditTitleRequest(channel=peer, title=title))
            else:
                await client(
                    functions.messages.EditChatTitleRequest(chat_id=peer.chat_id, title=title)
                )
            return {"title": "changed"}, warnings

        case "chat.set_about":
            peer = await client.get_input_entity(prepared.peers["chat"].ref.peer_id)
            # One request for both kinds of chat: unlike a title or a photo,
            # Telegram keeps the description on the peer rather than on the
            # channel object.
            await client(
                functions.messages.EditChatAboutRequest(
                    peer=peer,
                    about=params.about,  # type: ignore[attr-defined]
                )
            )
            return {"about": "changed"}, warnings

        case "chat.set_photo":
            image = prepared.attachment
            if image is None:  # pragma: no cover - verification always sets it
                raise InvalidInput("the image was never resolved; refusing to upload")
            peer = await client.get_input_entity(prepared.peers["chat"].ref.peer_id)
            # Uploaded here rather than during verification: an upload is
            # already an effect on Telegram's servers, and everything before
            # this line has to stay refusable.
            uploaded = await client.upload_file(str(image.path))
            photo = types.InputChatUploadedPhoto(file=uploaded)
            if isinstance(peer, types.InputPeerChannel):
                await client(functions.channels.EditPhotoRequest(channel=peer, photo=photo))
            else:
                await client(
                    functions.messages.EditChatPhotoRequest(chat_id=peer.chat_id, photo=photo)
                )
            return {"photo": "changed", "sha256": image.sha256, "file": image.name}, warnings

    raise InvalidInput(f"no applier for operation {sanitize_line(operation, limit=64)}")


async def _member_peers(client: Any, prepared: _Prepared) -> tuple[Any, Any]:
    """The two input peers every moderation RPC addresses.

    Telegram takes a peer plus its access hash, not a bare id, and the pair is
    fetched again here rather than carried from planning time: hashes are
    re-issued, and a stale one fails the request rather than acting on the
    wrong person.
    """
    chat_peer = await client.get_input_entity(prepared.peers["chat"].ref.peer_id)
    user_peer = await client.get_input_entity(prepared.peers["user"].ref.peer_id)
    return chat_peer, user_peer


def _reaction_object(reaction: dict[str, Any]) -> Any:
    """Turn this project's reaction shape back into Telethon's.

    The two forms are kept apart on purpose. A plan stores something a person
    can read in ``plan show`` and something the applier can compare for drift; a
    Telethon constructor is neither. The custom-emoji id travels as a string all
    the way to here for the same reason it is published as one — it is 64-bit,
    and anything that round-trips it through a float loses the identifier.
    """
    from telethon.tl import types

    if reaction.get("kind") == "custom_emoji":
        return types.ReactionCustomEmoji(document_id=int(reaction["custom_emoji_id"]))
    if reaction.get("kind") != "emoji" or not reaction.get("emoji"):
        # Unreachable: `marks.guard_sendable` refuses such a list while the plan
        # is written and again during verification. Kept because the alternative
        # is constructing a `ReactionEmoji(emoticon="None")` and sending it.
        raise InvalidInput(f"cannot send a reaction of kind {reaction.get('kind')!r}")
    return types.ReactionEmoji(emoticon=str(reaction["emoji"]))


def _missing_invitees(result: Any) -> list[int]:
    """Pull the people Telegram declined to add out of a successful response.

    Newer layers answer an invite with ``messages.InvitedUsers``, which carries
    a ``missing_invitees`` list next to the updates. Older ones answer with
    plain ``Updates`` and no such field, in which case there is nothing to
    report.
    """
    missing = getattr(result, "missing_invitees", None) or []
    ids: list[int] = []
    for entry in missing:
        user_id = getattr(entry, "user_id", None)
        if user_id is not None:
            ids.append(int(user_id))
    return ids


# ---------------------------------------------------------------------------
# has this already gone out?
# ---------------------------------------------------------------------------


def _outbound_fingerprint(plan: Any, params: BaseModel, prepared: _Prepared | Any) -> str | None:
    """Identify this action the way the ledger identifies it, or decline to.

    ``None`` means "not an operation the ledger covers" — reacting twice or
    joining twice is idempotent at Telegram's end, and putting those under a
    duplicate check would refuse a lot and prevent nothing.

    Everything fed in comes from :func:`_verify`, not from the plan: the peer id
    was re-resolved a moment ago, and the file digest was recomputed from the
    bytes rather than read out of the preconditions. ``allow_duplicate`` is
    deliberately absent — a repeat somebody approved is still these words going
    to this person, and folding the flag in would make every send after an
    approved repeat invisible to the check.
    """
    if plan.operation not in LEDGERED_OPERATIONS:
        return None
    peer_id = prepared.audit_peer_id
    if peer_id is None:  # pragma: no cover - every ledgered operation sets it
        return None

    extra: dict[str, Any] = {}
    # Taken from verification for `message.reply`, where the argument may be
    # None because a link named the message instead. The same reply addressed
    # both ways is the same remark under the same message, and a fingerprint
    # that read the raw argument would call them two different things.
    reply_to = (
        (prepared.message_ids[0] if prepared.message_ids else None)
        if plan.operation == "message.reply"
        else getattr(params, "reply_to_message_id", None)
    )
    if reply_to is not None:
        # A reply quotes something. The same words under two different messages
        # read as two different remarks, and only one of them may be a mistake.
        extra["reply_to_message_id"] = int(reply_to)
    preview = getattr(params, "link_preview", None)
    if preview is not None:
        # A link that expands into a card and the same link that does not are
        # different objects in the chat, whatever the characters say.
        extra["link_preview"] = bool(preview)
    if plan.operation == "message.forward":
        # A forward carries no body of its own: what identifies it is which
        # messages, from where — and whether the original author's name travels
        # with them, which is the difference between a quotation and an
        # anonymous one.
        extra["source_peer_id"] = plan.preconditions.get("source", {}).get("peer_id")
        extra["message_ids"] = sorted(int(one) for one in params.message_ids)  # type: ignore[attr-defined]
        extra["drop_author"] = bool(getattr(params, "drop_author", False))

    attachment = getattr(prepared, "attachment", None)
    if attachment is not None:
        # The same bytes are not the same arrival. A JPEG sent as a photo is
        # re-encoded and shows a preview; sent as a document it arrives intact,
        # under a file name people read. Both are in the plan summary somebody
        # approved, so both belong here.
        extra["delivery"] = str(attachment.delivery)
        extra["file_name"] = attachment.name
    # `silent` is deliberately absent: it decides whether a phone makes a sound,
    # not what the message says, and the same words sent twice are twice either
    # way.
    return fingerprint(
        account=plan.account,
        operation=plan.operation,
        peer_id=int(peer_id),
        body=prepared.audit_body,
        file_sha256=attachment.sha256 if attachment is not None else None,
        extra=extra,
    )


def _refuse_duplicate(
    ctx: OperationContext, plan: Any, params: BaseModel, prepared: _Prepared | Any
) -> str | None:
    """Stop this send if the identical one already went out. Returns the digest.

    Refused, never skipped. A caller told "done" for something that was quietly
    not done reports success for a message nobody received; a caller told "no"
    can look, and the message says what to look at — when the identical action
    was applied, and which plan did it.

    The chat is named by its numeric id and nothing else. A chat title is
    written by whoever runs the chat, and although :meth:`Envelope.failure`
    now defangs an error payload, it does not mark a value interpolated into
    the sentence this project composed — so the id is what belongs there.
    """
    digest = _outbound_fingerprint(plan, params, prepared)
    if digest is None:
        return None
    if ctx.ledger is None:
        # Fail closed. A context with no ledger is a broken installation, not a
        # permission to skip the check — and every production path builds one.
        raise InvalidInput(
            "no outbound ledger is available, so this send cannot be checked for "
            "duplicates; refusing to apply it"
        )
    if getattr(params, "allow_duplicate", False):
        return digest

    prior = ctx.ledger.find_recent(digest)
    if prior is None:
        return digest

    minutes = prior.age_seconds() / 60
    ago = f"{minutes:.0f} minutes ago" if minutes < 90 else f"{minutes / 60:.1f} hours ago"
    when = datetime.fromtimestamp(prior.sent_at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    raise DuplicateOutbound(
        # "went out" rather than "was applied": a plan whose outcome was never
        # established keeps its row on purpose, and telling somebody it
        # succeeded would be claiming to know the one thing nobody does.
        f"this identical {plan.operation} to peer {prepared.audit_peer_id} already went out "
        f"{ago} ({when}) as plan {sanitize_line(prior.plan_id, limit=64)}; "
        "refusing to do it a second time",
        suggestion=(
            "Check the chat first. If the repeat is intended, plan it again with "
            "allow_duplicate set — the approval preview then says it is a deliberate "
            "repeat, so whoever approves it knows what they are approving."
        ),
        details={"previous_plan_id": prior.plan_id, "previous_sent_at": prior.sent_at},
    )


# ---------------------------------------------------------------------------
# the applier
# ---------------------------------------------------------------------------


async def apply_plan(ctx: OperationContext, plan_id: str) -> Envelope:
    """Apply one plan. The only path in this project that changes Telegram."""
    # Importing the write operations registers them; the plan names one of them
    # and the registry is what turns that name back into a validated input.
    from .ops.write import open_writer

    # 1. Claim first: pending → applying is one conditional UPDATE, so a plan
    # already claimed elsewhere is refused here rather than applied twice.
    try:
        plan = ctx.plans.claim(plan_id)
    except TelegramAIError as exc:
        # Not found, not pending, expired. Nothing was claimed, so there is
        # nothing to close — the refusal is the whole answer.
        return Envelope.failure(exc, meta=Meta(extra={"plan_id": plan_id}))

    meta = Meta(account=plan.account, extra={"plan_id": plan.plan_id})
    reservation: Reservation | None = None
    ledger_entry: LedgerEntry | None = None
    event_id: str | None = None
    started_rpc = False
    #: The RPC returned. Kept apart from ``started_rpc`` because the window
    #: after it is not harmless: recording the outcome can itself fail — a full
    #: disk, a locked database — and the send has already happened. Without this
    #: such a failure looked like "nothing left the machine", refunded the slot
    #: and dropped the ledger row, so the next identical plan went out for real.
    rpc_completed = False

    try:
        operation = REGISTRY.by_name(plan.operation)
        params = operation.parse(plan.params)
        limit_kind = _LIMIT_KINDS.get(plan.operation)
        if limit_kind is None:
            raise InvalidInput(
                f"operation {sanitize_line(plan.operation, limit=64)} has no limit budget; "
                "refusing to apply it"
            )

        async with open_writer(ctx, plan.account) as (label, client):
            # 2. Everything the planner checked, checked again.
            prepared = await _verify(ctx, client, plan, params)

            # 3. Has this exact thing already gone to this peer? Asked here
            # because it needs what verification just re-resolved, and asked
            # before the reservation because a refusal spends no budget.
            digest = _refuse_duplicate(ctx, plan, params, prepared)

            # 4. The slot is taken before the network call, never after.
            reservation = ctx.limits.reserve(
                limit_kind, account=label, target=prepared.limit_target
            )

            # 5. The attempt is on disk before the request leaves.
            event_id = ctx.audit.attempt(
                action=plan.operation,
                account=label,
                actor=ctx.actor,
                peer_id=prepared.audit_peer_id,
                plan_id=plan.plan_id,
                body=prepared.audit_body,
                extra={"limit_target": prepared.limit_target},
            )
            if digest is not None and ctx.ledger is not None:
                # Written before the RPC for the reason the audit attempt is:
                # a row for a send that did not happen costs one refusal a
                # person can override on purpose, while a missing row for a send
                # that did happen costs the duplicate this exists to prevent.
                ledger_entry = ctx.ledger.record(
                    digest=digest,
                    account=label,
                    operation=plan.operation,
                    peer_id=prepared.audit_peer_id,
                    plan_id=plan.plan_id,
                )

            # 6. The effect.
            started_rpc = True
            outcome, rpc_warnings = await asyncio.wait_for(
                _execute(client, plan, params, prepared),
                timeout=_rpc_timeout(ctx, plan.operation),
            )
            started_rpc = False
            rpc_completed = True

        # 7. Record the result, then 8. settle the reservation.
        ctx.audit.outcome(event_id, status="applied", message_id=outcome.get("message_id"))
        ctx.plans.finish(plan.plan_id, state=PlanState.APPLIED, outcome=outcome)
        ctx.limits.commit(reservation)
        if ledger_entry is not None and ctx.ledger is not None:
            # The row was dated when the attempt began. Now that the send has
            # demonstrably finished, the window counts from the moment it
            # actually went out — which for a long upload is minutes later.
            ctx.ledger.settle(ledger_entry)

        return Envelope.success(
            {
                "plan_id": plan.plan_id,
                "operation": plan.operation,
                "account": plan.account,
                "state": str(PlanState.APPLIED),
                "outcome": outcome,
            },
            warnings=prepared.warnings + rpc_warnings,
            meta=meta,
        )

    except (PolicyError, PlanPreconditionFailed) as exc:
        # Refused before anything happened, or refused because the world moved.
        # Either way the action did not occur, so the slot goes back.
        ctx.audit.refusal(action=plan.operation, actor=ctx.actor, reason=exc.message)
        if event_id is not None:
            ctx.audit.outcome(
                event_id, status="failed", error_code=str(exc.code), detail=exc.message
            )
        ctx.plans.finish(plan.plan_id, state=PlanState.FAILED, outcome={"error": exc.to_dict()})
        _forget_ledger(ctx, ledger_entry)
        if reservation is not None:
            ctx.limits.release(reservation)
        return Envelope.failure(exc, meta=meta)

    except PrivacyRestricted as exc:
        # The request was made and Telegram acted on it — it simply did not do
        # what was asked. The attempt is spent, so the slot is committed.
        if event_id is not None:
            ctx.audit.outcome(
                event_id, status="failed", error_code=str(exc.code), detail=exc.message
            )
        ctx.plans.finish(plan.plan_id, state=PlanState.FAILED, outcome={"error": exc.to_dict()})
        if reservation is not None:
            ctx.limits.commit(reservation)
        return Envelope.failure(exc, meta=meta)

    except asyncio.CancelledError:
        # Interrupted mid-flight. Whether the request landed is unknowable from
        # here, so it is recorded as unknown and the cancellation continues to
        # propagate — swallowing it would leave the event loop in a lie.
        _settle_unknown(ctx, plan, event_id, reservation, "the apply was cancelled mid-flight")
        raise

    except Exception as exc:
        if isinstance(exc, _no_effect_error_classes()):
            detail = f"{type(exc).__name__}: {exc}"
            wrapped: TelegramAIError = _as_project_error(exc, detail, plan)
            if event_id is not None:
                ctx.audit.outcome(
                    event_id, status="failed", error_code=str(wrapped.code), detail=detail
                )
            ctx.plans.finish(
                plan.plan_id, state=PlanState.FAILED, outcome={"error": wrapped.to_dict()}
            )
            # The ledger row goes first. Neither store can be settled in the
            # other's transaction without welding the two modules together, so
            # the order decides what a crash in between leaves behind: this way
            # an over-counted rate limit, which costs one send of budget, rather
            # than a phantom ledger row, which refuses a send that never happened.
            _forget_ledger(ctx, ledger_entry)
            if reservation is not None:
                # Safe: every class in that whitelist is Telegram answering
                # "no". The refusal itself is the proof nothing happened.
                ctx.limits.release(reservation)
            return Envelope.failure(wrapped, meta=meta)

        if started_rpc or rpc_completed or isinstance(exc, _ambiguous_error_classes()):
            unknown = _settle_unknown(
                ctx, plan, event_id, reservation, f"{type(exc).__name__}: {exc}"
            )
            return Envelope.failure(unknown, meta=meta)

        # A bug on the verification path: nothing was sent, so the plan is
        # closed and the exception is allowed to surface as a traceback.
        if event_id is not None:
            ctx.audit.outcome(event_id, status="failed", detail=f"{type(exc).__name__}: {exc}")
        ctx.plans.finish(
            plan.plan_id, state=PlanState.FAILED, outcome={"error": {"internal": str(exc)}}
        )
        _forget_ledger(ctx, ledger_entry)
        if reservation is not None:
            ctx.limits.release(reservation)
        raise

    except BaseException as exc:
        # KeyboardInterrupt and SystemExit. Not swallowed — but a claimed plan
        # must not be left wedged in `applying`, where nothing can ever pick it
        # up again.
        detail = f"{type(exc).__name__}: {exc}"
        if started_rpc or rpc_completed:
            _settle_unknown(ctx, plan, event_id, reservation, detail)
        else:
            if event_id is not None:
                ctx.audit.outcome(event_id, status="failed", detail=detail)
            ctx.plans.finish(
                plan.plan_id, state=PlanState.FAILED, outcome={"error": {"internal": detail}}
            )
            _forget_ledger(ctx, ledger_entry)
            if reservation is not None:
                ctx.limits.release(reservation)
        raise


def _forget_ledger(ctx: OperationContext, entry: LedgerEntry | None) -> None:
    """Drop the ledger row, in every place the rate-limit slot is given back.

    The two go together on purpose: both are settled by the same question — did
    the request take effect? An unknown outcome keeps its row for the same
    reason it keeps its slot, because a send nobody can prove did not happen is
    one a later identical plan should still be stopped by.
    """
    if entry is not None and ctx.ledger is not None:
        ctx.ledger.forget(entry)


def _rpc_timeout(ctx: OperationContext, operation: str) -> int:
    """How long this one operation may take before its outcome is unknown.

    Sending a file is the only write here that is a transfer rather than a
    request, so it is the only one whose ceiling is a configuration value. The
    default applies to everything else, unchanged: raising it globally would
    make a stuck ordinary send sit for minutes before anybody heard about it.
    """
    if operation in _UPLOAD_OPERATIONS:
        return ctx.settings.upload.timeout_seconds
    return RPC_TIMEOUT_SECONDS


def _settle_unknown(
    ctx: OperationContext,
    plan: Plan,
    event_id: str | None,
    reservation: Reservation | None,
    detail: str,
) -> PlanUnknownOutcome:
    """Close a plan whose effect cannot be determined.

    The reservation is *not* released. An attempt that may have taken effect
    has to keep costing budget, because the alternative refunds sends nobody
    can prove did not happen.
    """
    if event_id is not None:
        ctx.audit.outcome(event_id, status="unknown", detail=detail)
    ctx.plans.finish(
        plan.plan_id,
        state=PlanState.UNKNOWN_OUTCOME,
        outcome={"unknown": True, "detail": sanitize_line(detail, limit=500)},
    )
    if reservation is not None:
        ctx.limits.commit(reservation)
    return PlanUnknownOutcome(
        f"plan {plan.plan_id} left the machine and the outcome is unknown: "
        f"{sanitize_line(detail, limit=200)}",
        suggestion=(
            "Check the chat before doing anything else. This is not retried "
            "automatically: a repeat after a send that succeeded is a duplicate."
        ),
        details={"plan_id": plan.plan_id, "operation": plan.operation},
    )


def _as_project_error(exc: BaseException, detail: str, plan: Any = None) -> TelegramAIError:
    """Translate a Telethon refusal into this project's error taxonomy.

    ``plan`` is consulted only to name the chat a refusal is about, and only by
    its numeric id: the preconditions hold one that verification re-checked a
    moment earlier, and a chat *title* is text somebody else wrote.
    """
    from telethon import errors

    restricted = getattr(errors.rpcerrorlist, "ChatForwardsRestrictedError", None)
    if restricted is not None and isinstance(exc, restricted):
        source = None
        if plan is not None:
            source = (getattr(plan, "preconditions", None) or {}).get("source", {}).get("peer_id")
        named = f"chat {source}" if source is not None else "the source chat"
        return ForwardsRestricted(
            f"Telegram refused the forward: {named} has content protection turned on "
            "(noforwards). The server enforces that, so no client can forward out of "
            "it and nothing was sent",
            suggestion=(
                "Do not retry: the answer will not change. Downloading from such a chat "
                "is not blocked — media fetch works normally — so posting the bytes as a "
                "fresh copy is media fetch followed by message send-file, which is a new "
                "message of your own rather than a forward, and needs its own approval."
            ),
        )
    if isinstance(exc, errors.FloodWaitError):
        from .errors import FloodWait

        return FloodWait(int(getattr(exc, "seconds", 0)))
    if isinstance(exc, errors.rpcerrorlist.UserPrivacyRestrictedError):
        return PrivacyRestricted(detail)
    error = TelegramError(sanitize_line(detail, limit=300))
    # Retrying is the caller's decision, and for a rejected write it is almost
    # never the right one; the flag says "the request may be re-issued", not
    # "re-issue it".
    error.retryable = False
    return error
