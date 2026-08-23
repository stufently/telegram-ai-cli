"""Turning Telethon objects into the flat dictionaries the contract promises.

Two reasons this is its own module rather than a method on each operation.

**Shape is a contract.** A message looks the same whether it arrived from a
chat read, a search or an inbox summary, so a caller writes one parser. When
each operation formats its own, the fields drift and the differences are only
discovered by whoever writes the second parser.

**Telethon types are not inspectable without Telethon.** Everything here reads
attributes rather than importing classes, so the module imports on a machine
with no Telethon installed, and the type it cannot recognise degrades to
``unknown`` instead of raising.
"""

from __future__ import annotations

from typing import Any

from ..safety import PeerKind, PeerRef
from ._common import iso

#: Long bodies are cut here rather than by the reader. A model handed a
#: 40 000-character forwarded document loses the rest of the page it asked for.
MESSAGE_TEXT_LIMIT = 4000

#: Inbox previews are one line of context, not the message.
PREVIEW_LIMIT = 200


def peer_kind(entity: Any) -> PeerKind:
    """Classify a peer by the attributes Telethon gives it.

    Attribute shape rather than class name: ``Channel`` covers both broadcast
    channels and supergroups, and the difference decides which read policy
    applies — a supergroup is a group, and treating it as a channel would let
    group rules apply to a broadcast or the reverse.
    """
    if entity is None:
        return PeerKind.UNKNOWN
    if getattr(entity, "is_self", False):
        return PeerKind.SELF
    channel_like = (
        getattr(entity, "broadcast", None) is not None
        or getattr(entity, "megagroup", None) is not None
    )
    if channel_like:
        if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
            return PeerKind.GROUP
        return PeerKind.CHANNEL if getattr(entity, "broadcast", False) else PeerKind.GROUP
    if hasattr(entity, "first_name") or hasattr(entity, "bot") or hasattr(entity, "phone"):
        return PeerKind.USER
    if hasattr(entity, "participants_count") or hasattr(entity, "title"):
        return PeerKind.GROUP
    return PeerKind.UNKNOWN


def marked_id(entity: Any) -> int:
    """The id in the form users see and write into their allowlists.

    Telethon's marked ids (``-100…`` for channels) are what the dialog list
    prints, so policy entries get copied from there. Deciding policy on the
    unmarked id would mean a configured chat silently fails to match.
    """
    from telethon import utils

    return int(utils.get_peer_id(entity))


def peer_ref(entity: Any) -> PeerRef:
    """The kernel's view of a peer: resolved id, kind, and its labels."""
    kind = peer_kind(entity)
    return PeerRef(
        peer_id=marked_id(entity),
        kind=kind,
        username=getattr(entity, "username", None),
        title=display_name(entity),
    )


def display_name(entity: Any) -> str | None:
    """Whatever a human would call this peer. Written by strangers."""
    if entity is None:
        return None
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    name = " ".join(part for part in parts if part)
    return name or None


def peer_summary(entity: Any) -> dict[str, Any]:
    """Identity of a peer, with no message content in it."""
    return {
        "id": marked_id(entity),
        "kind": str(peer_kind(entity)),
        "username": getattr(entity, "username", None),
        "title": display_name(entity),
        "bot": bool(getattr(entity, "bot", False)),
        "verified": bool(getattr(entity, "verified", False)),
        "scam": bool(getattr(entity, "scam", False)),
        "fake": bool(getattr(entity, "fake", False)),
        "deleted": bool(getattr(entity, "deleted", False)),
    }


def media_summary(message: Any) -> dict[str, Any] | None:
    """What is attached, without fetching a byte of it.

    Metadata only, on purpose: a chat read that downloaded attachments would
    spend the media quota on messages nobody asked to see, and it would do so
    under a capability the caller never granted.
    """
    media = getattr(message, "media", None)
    if media is None:
        return None
    handle = getattr(message, "file", None)
    return {
        "type": type(media).__name__.removeprefix("MessageMedia").lower() or "unknown",
        "mime_type": getattr(handle, "mime_type", None),
        "size": getattr(handle, "size", None),
        "name": getattr(handle, "name", None),
        "duration": getattr(handle, "duration", None),
        # Fetching it needs the media capability and a separate call, so the
        # caller is told the handle exists rather than being handed the file.
        "fetchable": True,
    }


def _clip(text: str | None, limit: int) -> tuple[str | None, bool]:
    if not text:
        return text or None, False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def message_summary(message: Any, *, text_limit: int = MESSAGE_TEXT_LIMIT) -> dict[str, Any]:
    """One message, flattened.

    ``text_truncated`` is per message and separate from the envelope's
    ``truncated``: the page may be complete while one body in it was cut.
    """
    text, clipped = _clip(getattr(message, "message", None), text_limit)
    sender = getattr(message, "sender", None)
    forward = getattr(message, "forward", None)

    summary: dict[str, Any] = {
        "id": getattr(message, "id", None),
        "date": iso(getattr(message, "date", None)),
        "outgoing": bool(getattr(message, "out", False)),
        "sender_id": getattr(message, "sender_id", None),
        "sender": display_name(sender),
        "sender_username": getattr(sender, "username", None),
        "text": text,
        "text_truncated": clipped,
        "reply_to_msg_id": getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
        "edited": iso(getattr(message, "edit_date", None)),
        "views": getattr(message, "views", None),
        "pinned": bool(getattr(message, "pinned", False)),
        "media": media_summary(message),
    }
    if forward is not None:
        summary["forwarded_from"] = display_name(getattr(forward, "sender", None)) or display_name(
            getattr(forward, "chat", None)
        )
    return summary


def preview_of(message: Any) -> str | None:
    """A single short line describing the last message in a dialog."""
    if message is None:
        return None
    text = getattr(message, "message", None)
    if text:
        clipped, _ = _clip(" ".join(text.split()), PREVIEW_LIMIT)
        return clipped
    media = media_summary(message)
    return f"[{media['type']}]" if media else None


def dialog_summary(dialog: Any) -> dict[str, Any]:
    """One row of a dialog listing: where it is and how much is waiting."""
    entity = getattr(dialog, "entity", None)
    return {
        "chat_id": marked_id(entity) if entity is not None else getattr(dialog, "id", None),
        "title": display_name(entity) or getattr(dialog, "name", None),
        "kind": str(peer_kind(entity)),
        "username": getattr(entity, "username", None),
        "unread": int(getattr(dialog, "unread_count", 0) or 0),
        "mentions": int(getattr(dialog, "unread_mentions_count", 0) or 0),
        "pinned": bool(getattr(dialog, "pinned", False)),
        "archived": bool(getattr(dialog, "archived", False)),
        "last_message_id": getattr(getattr(dialog, "message", None), "id", None),
        "last_message_at": iso(getattr(dialog, "date", None)),
    }


def participant_summary(user: Any) -> dict[str, Any]:
    """A chat member, plus the role Telethon attached to them if it did."""
    summary = peer_summary(user)
    participant = getattr(user, "participant", None)
    if participant is not None:
        role = type(participant).__name__
        summary["role"] = (
            role.removeprefix("ChannelParticipant").removeprefix("ChatParticipant").lower()
            or "member"
        )
        summary["admin"] = "admin" in role.lower() or "creator" in role.lower()
    return summary
