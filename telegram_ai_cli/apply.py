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
3. **Reserve the rate-limit slot before the network call.** Checking a counter
   and incrementing it after the ``await`` lets concurrent callers all read the
   same number and all proceed; a cap of five becomes eight.
4. **Write the audit attempt before the RPC.** A log written only after success
   loses exactly the case it exists for: the send that went out and then the
   process died.
5. **Then, and only then, the RPC.**

**A timeout after the request left is not a failure.** It is
``unknown_outcome``: the slot stays consumed, nothing is retried, and a person
has to look. Automatic retry here is how one message becomes two.

**Refunds are decided by exception class, never by message text.** Only errors
that cannot be raised after the request took effect give the slot back.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .envelope import Envelope, Meta
from .errors import (
    InvalidInput,
    PlanPreconditionFailed,
    PlanUnknownOutcome,
    PolicyError,
    PrivacyRestricted,
    TelegramAIError,
    TelegramError,
)
from .limits import LimitKind, Reservation
from .opspec import REGISTRY
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

#: Which budget each operation draws from, and therefore which ceiling refuses
#: it. Keyed by ``Operation.name`` so a new write cannot be added without
#: deciding what it costs.
_LIMIT_KINDS: dict[str, LimitKind] = {
    "message.send": LimitKind.SEND,
    "message.reply": LimitKind.SEND,
    "message.edit": LimitKind.SEND,
    "message.delete": LimitKind.SEND,
    "message.forward": LimitKind.SEND,
    # Marking read is visible to the other party, so it draws on the same
    # budget as speaking rather than being free.
    "chat.mark_read": LimitKind.SEND,
    "chat.join": LimitKind.JOIN,
    "chat.leave": LimitKind.JOIN,
    "chat.create": LimitKind.ADMIN,
    "chat.invite": LimitKind.ADMIN,
    "chat.promote": LimitKind.ADMIN,
    "account.profile": LimitKind.ADMIN,
}


@dataclass(slots=True)
class _Prepared:
    """Everything verification learned, handed to the RPC step."""

    limit_target: str
    peers: dict[str, Resolved] = field(default_factory=dict)
    messages: list[Any] = field(default_factory=list)
    audit_peer_id: int | None = None
    audit_body: str | None = None
    warnings: list[str] = field(default_factory=list)


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
        # The target refused, by their own settings or their account state.
        "UserPrivacyRestrictedError",
        "UserNotMutualContactError",
        "InputUserDeactivatedError",
        "UserBlockedError",
        "UserIsBlockedError",
        # The request never described anything Telegram could act on, so there
        # was nothing for it to change.
        "MessageIdInvalidError",
        "MessageNotModifiedError",
        "MessageEmptyError",
        "MessageTooLongError",
        "PeerIdInvalidError",
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
        resolve_peer,
    )

    pre = plan.preconditions
    warnings: list[str] = []
    operation = plan.operation
    action = f"apply:{operation}"

    match operation:
        case "message.send" | "message.reply" | "chat.mark_read":
            require_planning_profile(ctx, Capability.SEND, action=action)
            chat = await resolve_peer(client, params.chat)  # type: ignore[attr-defined]
            require_peer(ctx, Capability.SEND, chat.ref, action=action)
            warnings += _check_peer(pre["peer"], chat, what="chat")
            prepared = _Prepared(
                limit_target=str(chat.ref.peer_id),
                peers={"chat": chat},
                audit_peer_id=chat.ref.peer_id,
                audit_body=getattr(params, "text", None),
                warnings=warnings,
            )
            if operation == "message.reply":
                original = await _fetch_messages(
                    client,
                    chat,
                    [params.reply_to_message_id],  # type: ignore[attr-defined]
                )
                _check_messages([pre["reply_to"]], original)
            return prepared

        case "message.edit" | "message.delete":
            require_planning_profile(ctx, Capability.SEND, action=action)
            chat = await resolve_peer(client, params.chat)  # type: ignore[attr-defined]
            require_peer(ctx, Capability.SEND, chat.ref, action=action)
            warnings += _check_peer(pre["peer"], chat, what="chat")
            ids = (
                [params.message_id]  # type: ignore[attr-defined]
                if operation == "message.edit"
                else list(params.message_ids)  # type: ignore[attr-defined]
            )
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
            )

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

        case "chat.invite" | "chat.promote":
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

    raise InvalidInput(f"no applier for operation {sanitize_line(operation, limit=64)}")


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
                reply_to=params.reply_to_message_id,  # type: ignore[attr-defined]
                silent=params.silent,  # type: ignore[attr-defined]
                link_preview=params.link_preview,  # type: ignore[attr-defined]
            )
            return {"message_id": int(message.id)}, warnings

        case "message.edit":
            message = await client.edit_message(
                prepared.peers["chat"].ref.peer_id,
                params.message_id,  # type: ignore[attr-defined]
                params.text,  # type: ignore[attr-defined]
            )
            asked_id = params.message_id  # type: ignore[attr-defined]
            return {"message_id": int(getattr(message, "id", None) or asked_id)}, warnings

        case "message.delete":
            await client.delete_messages(
                prepared.peers["chat"].ref.peer_id,
                list(params.message_ids),  # type: ignore[attr-defined]
                revoke=params.revoke,  # type: ignore[attr-defined]
            )
            return {"deleted": len(params.message_ids)}, warnings  # type: ignore[attr-defined]

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
                    megagroup=True,
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

    raise InvalidInput(f"no applier for operation {sanitize_line(operation, limit=64)}")


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
    event_id: str | None = None
    started_rpc = False

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

            # 3. The slot is taken before the network call, never after.
            reservation = ctx.limits.reserve(
                limit_kind, account=label, target=prepared.limit_target
            )

            # 4. The attempt is on disk before the request leaves.
            event_id = ctx.audit.attempt(
                action=plan.operation,
                account=label,
                actor=ctx.actor,
                peer_id=prepared.audit_peer_id,
                plan_id=plan.plan_id,
                body=prepared.audit_body,
                extra={"limit_target": prepared.limit_target},
            )

            # 5. The effect.
            started_rpc = True
            outcome, rpc_warnings = await asyncio.wait_for(
                _execute(client, plan, params, prepared), timeout=RPC_TIMEOUT_SECONDS
            )
            started_rpc = False

        # 6. Record the result, then 7. settle the reservation.
        ctx.audit.outcome(event_id, status="applied", message_id=outcome.get("message_id"))
        ctx.plans.finish(plan.plan_id, state=PlanState.APPLIED, outcome=outcome)
        ctx.limits.commit(reservation)

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
            wrapped: TelegramAIError = _as_project_error(exc, detail)
            if event_id is not None:
                ctx.audit.outcome(
                    event_id, status="failed", error_code=str(wrapped.code), detail=detail
                )
            ctx.plans.finish(
                plan.plan_id, state=PlanState.FAILED, outcome={"error": wrapped.to_dict()}
            )
            if reservation is not None:
                # Safe: every class in that whitelist is Telegram answering
                # "no". The refusal itself is the proof nothing happened.
                ctx.limits.release(reservation)
            return Envelope.failure(wrapped, meta=meta)

        if started_rpc or isinstance(exc, _ambiguous_error_classes()):
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
        if reservation is not None:
            ctx.limits.release(reservation)
        raise

    except BaseException as exc:
        # KeyboardInterrupt and SystemExit. Not swallowed — but a claimed plan
        # must not be left wedged in `applying`, where nothing can ever pick it
        # up again.
        detail = f"{type(exc).__name__}: {exc}"
        if started_rpc:
            _settle_unknown(ctx, plan, event_id, reservation, detail)
        else:
            if event_id is not None:
                ctx.audit.outcome(event_id, status="failed", detail=detail)
            ctx.plans.finish(
                plan.plan_id, state=PlanState.FAILED, outcome={"error": {"internal": detail}}
            )
            if reservation is not None:
                ctx.limits.release(reservation)
        raise


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


def _as_project_error(exc: BaseException, detail: str) -> TelegramAIError:
    """Translate a Telethon refusal into this project's error taxonomy."""
    from telethon import errors

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
