"""``telegram_whois`` — who or what is behind a handle, an id or an invite.

This is the operation that makes every allowlist entry possible: policy is
written against numeric ids, and a person has a ``@name`` or a link. It returns
identity only — never message content — which is why it is not gated behind the
chat read policy. Refusing to resolve an id would make the safe configuration
impossible to write.

🚨 **Checking an invite must not join anything.** Telegram exposes two calls
for invite links that look interchangeable in a client library: one describes
the chat, the other walks through the door. Only the describing call is used
here, and joining is a write that goes through a plan like every other write.
A "look it up" tool that quietly joins a group announces the account's presence
to everyone in it, and there is no undo for that.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import Field

from ..context import OperationContext
from ..envelope import Envelope
from ..errors import FloodWait, InvalidInput, PolicyError, SessionRevoked, TelegramAIError
from ..opspec import REGISTRY, Effect, Operation
from ._client import open_account
from ._common import (
    ReadInput,
    guard_hard_denied,
    require_enumeration,
    telegram_errors,
    telegram_result,
)
from ._serialize import display_name, peer_kind, peer_ref, peer_summary

#: ``t.me/+hash``, ``t.me/joinchat/hash`` and their telegram.me spellings.
_INVITE = re.compile(
    r"(?:https?://)?(?:t(?:elegram)?\.me|telegram\.dog)/(?:joinchat/|\+)([\w-]{8,64})/?$",
    re.IGNORECASE,
)

#: A public link, which is just a username wearing a URL.
_PUBLIC_LINK = re.compile(
    r"(?:https?://)?(?:t(?:elegram)?\.me|telegram\.dog)/([A-Za-z][\w\d_]{3,31})/?$",
    re.IGNORECASE,
)

#: How many shared groups to list. Enough to answer "do we overlap", not a map
#: of the person's memberships.
_COMMON_CHATS = 20


class WhoisInput(ReadInput):
    target: str = Field(
        min_length=1,
        description="@username, numeric id, t.me link, or an invite link.",
    )
    common_chats: bool = Field(
        default=True,
        description="For users, also list groups this account shares with them.",
    )


def classify(target: str) -> tuple[str, str]:
    """Decide what kind of reference this is before touching the network.

    Done with a regular expression rather than by trying calls until one
    succeeds: "try to join and see" is precisely the behaviour this operation
    must not have.
    """
    text = target.strip()
    if match := _INVITE.match(text):
        return "invite", match.group(1)
    if match := _PUBLIC_LINK.match(text):
        return "username", match.group(1)
    if text.lstrip("-").isdigit():
        return "id", text
    if text.startswith("@"):
        return "username", text[1:]
    if re.fullmatch(r"[A-Za-z][\w\d_]{3,31}", text):
        return "username", text
    raise InvalidInput(f"whois: {target!r} is not a username, id or Telegram link")


async def _describe_invite(ctx: OperationContext, account: Any, invite_hash: str) -> dict[str, Any]:
    """Describe a private chat from its invite, without entering it."""
    from telethon.tl.functions.messages import CheckChatInviteRequest

    with telegram_errors(what="whois invite"):
        # CheckChatInvite only, and it is the only invite request this module
        # imports — the sibling request that joins by invite hash appears
        # nowhere in the read path, by name or otherwise.
        result = await account.client(CheckChatInviteRequest(invite_hash))

    chat = getattr(result, "chat", None)
    if chat is not None:
        ref = peer_ref(chat)
        guard_hard_denied(ctx, ref, action="whois")
        return {
            "kind": "invite",
            "already_member": True,
            "chat": peer_summary(chat),
        }

    return {
        "kind": "invite",
        "already_member": False,
        "chat": {
            "title": getattr(result, "title", None),
            "kind": "channel" if getattr(result, "broadcast", False) else "group",
            "participants": getattr(result, "participants_count", None),
            "requires_approval": bool(getattr(result, "request_needed", False)),
            "public": bool(getattr(result, "public", False)),
        },
        # Stated in the payload so a reader cannot mistake a description for
        # membership: nothing was joined by asking.
        "joined": False,
    }


async def _monoforum_inbox(
    account: Any, entity: Any, inbox_id: int, warnings: list[str]
) -> str | None:
    """Confirm a channel's Direct Messages inbox, name it, and cache its hash.

    ``linked_monoforum_id`` arrives on the channel entity itself, while the
    inbox is a *separate* channel with its own access hash — which the entity
    does not carry, and only ``GetFullChannel``'s ``chats`` does. Telethon
    writes every entity of a response into the session, so asking here is what
    puts the real hash where later resolutions find it.

    **What this is not.** It is not what makes the id resolvable: Telethon
    falls back to ``channels.GetChannels`` with ``access_hash=0`` for a peer it
    does not know, and Telegram answers that for a monoforum — checked live
    against three channels this account had never written to. So the id worked
    before this call existed. What the call buys is the inbox's name, an answer
    that came from Telegram rather than from a field on another object, and a
    hash that spares every later resolution that fallback round-trip. It also
    covers the case the fallback does not: an id it refuses is indistinguishable
    from one that does not exist, and the full channel still carries the hash.

    A failure costs those things, not the answer: the identity is already
    correct, and the caller is told the inbox is unconfirmed. Two failures are
    re-raised instead, because neither is about this channel: a flood wait says
    everything after this is rate limited and carries the interval Telegram
    named, and a revoked session says the account is signed out — an answer
    that came back ``ok`` would have hidden both.

    **What the cache is worth lasts one process.** A file-backed session keeps
    what it stored; an account configured as a string session gets a fresh
    in-memory store on every open, since ``StringSession.save()`` writes the DC
    and auth key only. There a later command resolves through the fallback
    again rather than from the session.
    """
    from telethon.tl.functions.channels import GetFullChannelRequest

    try:
        with telegram_errors(what="whois monoforum"):
            full = await account.client(GetFullChannelRequest(channel=entity))
    except (FloodWait, SessionRevoked):
        raise
    except TelegramAIError as exc:
        warnings.append(f"direct messages inbox not confirmed: {exc.message}")
        return None

    for chat in getattr(full, "chats", None) or []:
        if peer_ref(chat).peer_id != inbox_id:
            continue
        # Arriving in the response is not the same as being usable. Telethon
        # stores an entity only when it carries a real access hash and is not a
        # `min` object, and it does that silently — so the addressing claim is
        # made by asking for the input peer, which is exactly what a send would
        # ask for, rather than by assuming the store accepted what it was given.
        try:
            await account.client.get_input_entity(inbox_id)
        except (ValueError, TypeError):
            warnings.append(
                "direct messages inbox is not addressable from this account; "
                "sending to linked_monoforum_id will not resolve"
            )
            return None
        return display_name(chat)

    # The id is still returned: it is what the channel itself reported. What is
    # gone is the assurance that it can be addressed, and saying so beats a
    # caller discovering it as an unresolvable peer at apply time.
    warnings.append(
        "Telegram did not return the direct messages inbox; sending to "
        "linked_monoforum_id may not resolve"
    )
    return None


async def _common_chats(
    ctx: OperationContext, account: Any, entity: Any, warnings: list[str]
) -> list[dict[str, Any]] | None:
    """Groups shared with a user, when enumeration is permitted.

    A refusal here downgrades the field to ``null`` instead of failing the whole
    lookup: the identity answer is still useful, and the caller is told why the
    rest is missing.
    """
    try:
        require_enumeration(ctx, private=False, action="whois.common_chats")
    except PolicyError as exc:
        warnings.append(f"shared groups omitted: {exc.message}")
        return None

    from telethon.tl.functions.messages import GetCommonChatsRequest

    with telegram_errors(what="whois common chats"):
        # Telethon casts the entity to an InputUser while resolving the
        # request, so the already-resolved object is handed over as-is.
        result = await account.client(
            GetCommonChatsRequest(user_id=entity, max_id=0, limit=_COMMON_CHATS)
        )

    chats = getattr(result, "chats", []) or []
    return [
        {
            "chat_id": peer_ref(chat).peer_id,
            "title": display_name(chat),
            "kind": str(peer_kind(chat)),
        }
        for chat in chats
    ]


async def handle_whois(ctx: OperationContext, params: WhoisInput) -> Envelope:
    reference, value = classify(params.target)
    warnings: list[str] = []

    async with open_account(ctx, params.account) as account:
        if reference == "invite":
            data = await _describe_invite(ctx, account, value)
            return telegram_result(ctx, data, account=account.label, warnings=warnings)

        with telegram_errors(what="whois"):
            entity = await account.client.get_entity(int(value) if reference == "id" else value)

        ref = peer_ref(entity)
        # Identity lookups are not gated by the chat read policy — an id has to
        # be resolvable before it can be allowlisted — but the hard floor still
        # applies, and it is not a policy anything may argue with.
        guard_hard_denied(ctx, ref, action="whois")

        data = {"kind": reference, **peer_summary(entity)}
        data["premium"] = bool(getattr(entity, "premium", False))
        data["restricted"] = bool(getattr(entity, "restricted", False))
        data["participants"] = getattr(entity, "participants_count", None)

        inbox_id = data.get("linked_monoforum_id")
        if inbox_id is not None:
            data["linked_monoforum_title"] = await _monoforum_inbox(
                account, entity, int(inbox_id), warnings
            )

        if params.common_chats and ref.is_private and not data["bot"]:
            data["common_chats"] = await _common_chats(ctx, account, entity, warnings)

    return telegram_result(ctx, data, account=account.label, warnings=warnings)


WHOIS = REGISTRY.register(
    Operation(
        name="contacts.whois",
        cli=("whois",),
        mcp_tool="telegram_whois",
        summary="Resolve a @username, id or invite link to who or what it is.",
        description=(
            "Returns identity only: id, kind, handle, bot flag, and shared groups for "
            "users. An invite link is described with CheckChatInvite and is never "
            "joined — joining is a write and goes through a plan."
        ),
        input_model=WhoisInput,
        effect=Effect.READ,
        capability=None,  # Identity, not chat content; the hard floor still applies.
        handler=handle_whois,  # type: ignore[arg-type]
        tags=("read", "contacts"),
    )
)
