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

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..links import format_message_link
from ..safety import PeerKind, PeerRef
from ._common import iso

#: Long bodies are cut here rather than by the reader. A model handed a
#: 40 000-character forwarded document loses the rest of the page it asked for.
MESSAGE_TEXT_LIMIT = 4000

#: Inbox previews are one line of context, not the message.
PREVIEW_LIMIT = 200


@dataclass(frozen=True, slots=True)
class ReadPointers:
    """Where a chat's two read pointers stand, as Telegram reports them.

    Read state is a property of the *dialog*, not of a message: Telegram tracks
    one id per direction and everything at or below it is read. So a caller
    that wants "has this been seen" needs the pointers alongside the messages,
    and a message serialized without them must say *unknown* rather than
    *unread* — the two look identical in a boolean and mean opposite things.
    """

    inbox_max_id: int | None = None
    """Highest id this account has read. Answers it for incoming messages."""

    outbox_max_id: int | None = None
    """Highest id the other side has read. Answers it for outgoing messages."""

    peer_receipts: bool = True
    """False where Telegram reports no reader pointer at all — a broadcast
    channel, or a dialog it declined to describe. The fields then stay null."""


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


#: The fields of a ``MessageMedia*`` that name something with a stable id, in
#: the order a person would call the attachment by. ``photo`` and ``video``
#: appear together on a live photo; ``alt_documents`` and ``video_cover`` hang
#: off a document; ``story`` carries its id on the media itself rather than on
#: an inner object, which is why plain ``id`` is in the list.
_MEDIA_PARTS = (
    "photo",
    "document",
    "video",
    "alt_documents",
    "video_cover",
    "webpage",
    "game",
    "poll",
    "story",
    "id",
)


def _ids_of(value: Any) -> list[str]:
    """The ids of one media field, whether it holds one object, several, or an id."""
    items = value if isinstance(value, list) else [value]
    found: list[str] = []
    for item in items:
        if item is None:
            continue
        ident = item if isinstance(item, int) else getattr(item, "id", None)
        if ident is not None:
            found.append(str(ident))
    return found


def media_fingerprint(message: Any) -> dict[str, Any] | None:
    """What is attached, identified — not merely "something is attached".

    A caption-less photo has an empty body, so the body digest a message
    snapshot records is identical for every such message. Editing a media
    message keeps its id, which means a plan reviewed against one photo could be
    applied to another and nothing in the snapshot would notice. Recording the
    id of the photo or document next to its type closes that.

    It lives here, beside :func:`media_summary`, because every operation that
    acts on somebody else's message needs it and the alternative is a second
    implementation that drifts from this one.

    Every identifiable part is recorded, not the first one found: an attachment
    is not always a single object. A live photo is a photo *and* a video, a
    document can carry a cover and alternative renditions, and taking whichever
    came first would call two attachments the same one whenever they agree
    about that part and differ about another.
    """
    media = getattr(message, "media", None)
    if media is None:
        return None
    parts = {name: ids for name in _MEDIA_PARTS if (ids := _ids_of(getattr(media, name, None)))}
    summary = media_summary(message) or {}
    primary = next((parts[name][0] for name in _MEDIA_PARTS if name in parts), None)
    return {
        "type": summary.get("type"),
        # The one a person reads in the plan preview. Comparison uses `parts`,
        # which is the whole of it.
        "id": primary,
        "parts": parts,
    }


def _clip(text: str | None, limit: int) -> tuple[str | None, bool]:
    if not text:
        return text or None, False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


#: `ReactionCustomEmoji` reads as one word once the prefix is gone.
_REACTION_KINDS = {"customemoji": "custom_emoji"}


def reaction_kind(reaction: Any) -> str:
    """Which of Telegram's reaction types this is.

    Read off the class name rather than by importing the four classes, for the
    same reason as everything else here: a type this version of Telethon does
    not know about degrades to its own name instead of raising.
    """
    if reaction is None:
        return "unknown"
    name = type(reaction).__name__.removeprefix("Reaction").lower() or "unknown"
    return _REACTION_KINDS.get(name, name)


def reactions_summary(message: Any) -> list[dict[str, Any]] | None:
    """Who reacted with what, counted — or ``None`` if Telegram said nothing.

    The distinction is load-bearing. ``[]`` means the message carries a
    reactions block and nobody has reacted; ``None`` means there is no block at
    all, which is what an old message or a chat with reactions disabled looks
    like. Collapsing the two into an empty list turns "not applicable" into
    "nobody cared".
    """
    reactions = getattr(message, "reactions", None)
    if reactions is None:
        return None
    results = getattr(reactions, "results", None)
    if results is None:
        return None

    rows: list[dict[str, Any]] = []
    for item in results:
        reaction = getattr(item, "reaction", None)
        custom = getattr(reaction, "document_id", None)
        rows.append(
            {
                # Telegram has four reaction types and two of them carry no
                # emoji at all (`ReactionPaid`, `ReactionEmpty`). Without this,
                # a paid star reaction and a blank one serialize identically.
                "kind": reaction_kind(reaction),
                "emoji": getattr(reaction, "emoticon", None),
                # A document id is 64-bit, and a JSON consumer parsing it as a
                # double loses the low bits — which is the whole identifier.
                "custom_emoji_id": str(custom) if custom is not None else None,
                "count": int(getattr(item, "count", 0) or 0),
                # Telegram records the *order* this account picked a reaction
                # in; presence, not position, is what "did I react" means.
                "chosen": getattr(item, "chosen_order", None) is not None,
            }
        )
    return rows


def recent_reactors(reactions: Any, *, unread_only: bool = False) -> list[dict[str, Any]] | None:
    """The handful of reactors Telegram attaches to the message itself.

    Never a request of its own: ``messages.getMessageReactionsList`` would name
    every person who reacted, gated by their own privacy settings, and pulling
    it turns a count into a roster. What is reported here arrived *with* the
    message, so reporting it costs nothing and reveals nothing extra.

    ``unread_only`` keeps the ones Telegram still flags as new. A page of unread
    reactions carries the older reactors on the same message too, and treating
    those as fresh would re-announce a reaction the owner has already seen.
    """
    recent = getattr(reactions, "recent_reactions", None)
    if not recent:
        return None
    rows: list[dict[str, Any]] = []
    for item in recent:
        if unread_only and not getattr(item, "unread", False):
            continue
        reaction = getattr(item, "reaction", None)
        custom = getattr(reaction, "document_id", None)
        peer = getattr(item, "peer_id", None)
        try:
            peer_id = marked_id(peer) if peer is not None else None
        except Exception:  # noqa: BLE001 - an unknown peer shape is not fatal
            peer_id = None
        rows.append(
            {
                "peer_id": peer_id,
                "kind": reaction_kind(reaction),
                "emoji": getattr(reaction, "emoticon", None),
                "custom_emoji_id": str(custom) if custom is not None else None,
                "date": iso(getattr(item, "date", None)),
            }
        )
    return rows


def topic_id_of(message: Any) -> int | None:
    """The forum topic a message belongs to, if it is in a forum at all.

    Telegram models a topic as a reply to the message that opens it, so the
    same field carries two different meanings and only ``forum_topic`` tells
    them apart. ``reply_to_top_id`` is the topic when the message also replies
    to something inside it; otherwise the topic *is* the reply target.
    """
    reply_to = getattr(message, "reply_to", None)
    if reply_to is None or not getattr(reply_to, "forum_topic", False):
        return None
    return getattr(reply_to, "reply_to_top_id", None) or getattr(reply_to, "reply_to_msg_id", None)


def is_forum(entity: Any) -> bool:
    """Whether this chat keeps its messages in topics rather than one history.

    Read off the flag Telegram sets on the channel, not guessed from the
    presence of topic ids on messages: a forum with every message in the
    General topic looks exactly like an ordinary supergroup from the messages
    alone, and a chat that had topics turned off keeps the old ones on its
    history.
    """
    return bool(getattr(entity, "forum", False))


def topic_summary(topic: Any, *, chat: PeerRef | None = None) -> dict[str, Any]:
    """One forum topic, flattened.

    Two shapes arrive in the same list. ``ForumTopic`` is the topic itself;
    ``ForumTopicDeleted`` carries an id and nothing else, and it is serialized
    into the same keys rather than dropped — a caller that remembered the id
    has to learn that it is gone, and a missing row looks like a page boundary.
    Its counters come back ``null`` instead of ``0``, because "no unread
    messages" and "no topic" are different statements.

    The draft Telegram attaches to a topic is deliberately not serialized: it
    is text this account typed and never sent, and a listing is not the place
    to disclose it.
    """
    topic_id = getattr(topic, "id", None)
    # Attribute shape rather than a class name, like everything else here: a
    # deleted topic is the one that cannot say what it was called.
    deleted = not hasattr(topic, "title")
    emoji_id = getattr(topic, "icon_emoji_id", None)
    return {
        "id": topic_id,
        "title": getattr(topic, "title", None),
        "deleted": deleted,
        "created_at": iso(getattr(topic, "date", None)),
        # Topics are opened by a message, and that message's id *is* the topic
        # id — which is what makes `reply_to=<topic>` a history filter.
        "top_message_id": getattr(topic, "top_message", None),
        "unread": _optional_int(getattr(topic, "unread_count", None)),
        "mentions": _optional_int(getattr(topic, "unread_mentions_count", None)),
        "unread_reactions": _optional_int(getattr(topic, "unread_reactions_count", None)),
        "read_inbox_max_id": getattr(topic, "read_inbox_max_id", None),
        "closed": bool(getattr(topic, "closed", False)),
        "hidden": bool(getattr(topic, "hidden", False)),
        "pinned": bool(getattr(topic, "pinned", False)),
        "mine": bool(getattr(topic, "my", False)),
        "icon_color": getattr(topic, "icon_color", None),
        # A custom emoji id is 64-bit; a JSON consumer parsing it as a double
        # loses the low bits, which is the whole identifier.
        "icon_emoji_id": str(emoji_id) if emoji_id is not None else None,
        "link": message_link(chat, topic_id),
    }


def _optional_int(value: Any) -> int | None:
    """A counter Telegram reported, or nothing — never a fabricated zero."""
    return None if value is None else int(value)


def message_link(chat: PeerRef | None, message_id: Any, topic_id: int | None = None) -> str | None:
    """The permanent address of a message, or ``None`` where none exists.

    A one-to-one conversation is the case worth spelling out: the other party
    has a username, and ``t.me/<their handle>/55`` is a well-formed URL that
    opens their profile and addresses no message at all. Producing it would be
    worse than producing nothing, so a private peer gets no link even though
    the ingredients for one are right there.
    """
    if chat is None or chat.is_private or not isinstance(message_id, int):
        return None
    return format_message_link(
        username=chat.username,
        chat_id=chat.peer_id,
        message_id=message_id,
        topic_id=topic_id,
    )


def _read_flags(message: Any, read: ReadPointers | None) -> tuple[bool | None, bool | None]:
    """``(read_by_me, read_by_peer)``, with ``None`` meaning "not known".

    Only one of the two can ever be known for a given message: a message this
    account sent is read by the other side, and one it received is read by this
    account. The direction that does not apply stays null rather than false.
    """
    if read is None:
        return None, None
    message_id = getattr(message, "id", None)
    if not isinstance(message_id, int):
        return None, None

    if getattr(message, "out", False):
        if not read.peer_receipts or read.outbox_max_id is None:
            return None, None
        return None, message_id <= read.outbox_max_id
    if read.inbox_max_id is None:
        return None, None
    return message_id <= read.inbox_max_id, None


def message_summary(
    message: Any,
    *,
    text_limit: int = MESSAGE_TEXT_LIMIT,
    chat: PeerRef | None = None,
    read: ReadPointers | None = None,
    senders: Mapping[int, Any] | None = None,
) -> dict[str, Any]:
    """One message, flattened.

    ``text_truncated`` is per message and separate from the envelope's
    ``truncated``: the page may be complete while one body in it was cut.

    ``chat`` is a :class:`~telegram_ai_cli.safety.PeerRef` rather than the
    Telethon entity because the only thing a permalink needs is the resolved id
    and the username, both of which the caller has already computed — and a
    plain dataclass keeps this function testable without Telethon.

    ``senders`` covers messages that did not come from Telethon's high-level
    helpers. A raw API response carries its people in a separate ``users`` list
    and never attaches them to the messages, so ``message.sender`` is ``None``
    and the row would name nobody — which for a mention is the one field that
    matters. Keyed by the same marked id ``sender_id`` reports.
    """
    text, clipped = _clip(getattr(message, "message", None), text_limit)
    sender = getattr(message, "sender", None)
    if sender is None and senders:
        sender = senders.get(getattr(message, "sender_id", None))
    forward = getattr(message, "forward", None)
    message_id = getattr(message, "id", None)
    topic_id = topic_id_of(message)
    read_by_me, read_by_peer = _read_flags(message, read)

    summary: dict[str, Any] = {
        "id": message_id,
        "date": iso(getattr(message, "date", None)),
        "outgoing": bool(getattr(message, "out", False)),
        "sender_id": getattr(message, "sender_id", None),
        "sender": display_name(sender),
        "sender_username": getattr(sender, "username", None),
        "text": text,
        "text_truncated": clipped,
        "reply_to_msg_id": getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
        "topic_id": topic_id,
        "edited": iso(getattr(message, "edit_date", None)),
        "views": getattr(message, "views", None),
        "pinned": bool(getattr(message, "pinned", False)),
        "reactions": reactions_summary(message),
        # A permanent address for the message, so a person can open what the
        # agent is talking about. Null where Telegram has no such address.
        "link": message_link(chat, message_id, topic_id),
        "read_by_me": read_by_me,
        "read_by_peer": read_by_peer,
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
        # Unread *reactions on this account's own messages* — Telegram counts
        # them separately from mentions, and they are the other half of "where
        # was I actually addressed". Ranking on `unread` alone buries both.
        "reactions": int(getattr(dialog, "unread_reactions_count", 0) or 0),
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
