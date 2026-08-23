"""`t.me` links, parsed and produced — the address of a single message.

A permalink is the natural way a person hands a message to an agent, and the
natural way an agent hands one back. Both directions were missing something.

**Inbound**, a link was only ever resolved as a *chat*: ``t.me/example/4231``
was handed to Telethon, which produced the chat and dropped the ``4231``. The
number is the whole reason the link was pasted, so losing it silently means the
operation runs against the wrong thing — the newest message in the chat rather
than the one that was pointed at.

**Outbound**, nothing addressed a message at all. A result saying "message 4231
in chat -100…" is not something a person can open.

Three shapes exist and all three matter:

``t.me/<username>/<id>``
    A public chat.
``t.me/c/<internal>/<id>``
    A private supergroup or channel. ``<internal>`` is the marked id with its
    ``-100`` prefix removed, which is why parsing has to put the prefix back
    rather than treating the number as a chat id.
``t.me/<chat>/<topic>/<id>``
    A forum topic. The middle number is the *topic*, and reading it as the
    message id addresses the wrong message — usually the first one in the
    thread, which is exactly the kind of near-miss nobody notices.

Everything here is pure: no network, no Telethon, no configuration. The number
in a link decides which message an operation touches, so that decision is
testable exhaustively and offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

#: Telegram's own hosts. `telegram.dog` is a long-standing alias.
_HOSTS = frozenset({"t.me", "telegram.me", "telegram.dog", "www.t.me", "www.telegram.me"})

#: Message ids are sequential per chat and nowhere near this large; a number
#: past it is a typo or a probe, and believing it would send an operation at a
#: message that cannot exist.
_MAX_MESSAGE_ID = 2**31 - 1

#: Invite links are a different thing entirely — they name a chat nobody can
#: address by username, and describing one is `whois`'s job, not this module's.
_INVITE_PREFIXES = ("+", "joinchat")

#: First path segments Telegram reserves for its own deep links. None of them is
#: a chat, so reading `t.me/share/123` as "message 123 in the chat @share" would
#: send a lookup at a handle nobody can hold — and `t.me/login/12345` would go
#: looking for a chat named after a login code. Declined outright instead.
_RESERVED_PATHS = frozenset(
    {
        "addemoji",
        "addlist",
        "addstickers",
        "addtheme",
        "bg",
        "boost",
        "confirmphone",
        "contact",
        "giftcode",
        "invoice",
        "iv",
        "login",
        "proxy",
        "setlanguage",
        "share",
        "socks",
    }
)

_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


@dataclass(frozen=True, slots=True)
class TelegramLink:
    """What a `t.me` URL actually addresses."""

    chat: str | int
    """A username, or the marked chat id for a `/c/` link — never a raw path."""

    message_id: int | None = None
    topic_id: int | None = None
    private: bool = False
    """True for `/c/` links: the chat has no public address."""

    comment_id: int | None = None
    """Set by `?comment=<id>`, which addresses a message in the *discussion
    group* attached to a channel — a different chat from the one in the path.
    Recorded rather than resolved, so a caller can refuse the link instead of
    silently acting on the channel post it hangs off."""


def _marked(internal: str) -> int:
    """`/c/1234567890` and `-1001234567890` are the same chat."""
    return int(f"-100{internal}")


def _message_id(value: str) -> int | None:
    if not value.isdigit():
        return None
    number = int(value)
    if number < 1 or number > _MAX_MESSAGE_ID:
        return None
    return number


def parse_telegram_link(text: str) -> TelegramLink | None:
    """Read a `t.me` URL, or decline.

    Returning ``None`` rather than raising is deliberate: callers accept an id,
    a ``@username`` *or* a link in the same argument, so "not a link" is an
    ordinary outcome that falls through to the next resolution strategy, not an
    error to report to the user.
    """
    candidate = (text or "").strip()
    if not candidate:
        return None
    if "//" not in candidate:
        # urlsplit needs a scheme to populate `netloc`; without one the whole
        # string lands in `path` and every host check silently passes.
        candidate = f"https://{candidate}"

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or parts.netloc.lower() not in _HOSTS:
        return None

    segments = [segment for segment in parts.path.split("/") if segment]
    if not segments or segments[0].startswith(_INVITE_PREFIXES):
        return None

    # `?thread=` is how the web client addresses a topic or a comment thread
    # when the path does not carry it.
    query = parse_qs(parts.query)
    thread = query.get("thread", [None])[0]
    topic_id = _message_id(thread) if thread else None
    comment = query.get("comment", [None])[0]
    comment_id = _message_id(comment) if comment else None

    private = segments[0] == "c"
    if private:
        segments = segments[1:]
        if not segments or not segments[0].isdigit():
            return None
        chat: str | int = _marked(segments[0])
    else:
        # `t.me/s/<name>` is the web preview of a channel, not a chat of its own.
        if segments[0] == "s" and len(segments) > 1:
            segments = segments[1:]
        if segments[0].lower() in _RESERVED_PATHS:
            return None
        if not _USERNAME.match(segments[0]):
            return None
        chat = segments[0]

    rest = segments[1:]
    if not rest:
        return TelegramLink(chat=chat, topic_id=topic_id, private=private, comment_id=comment_id)

    # Telegram produces at most `<chat>/<topic>/<message>`. A longer path is
    # something this parser does not understand, and ignoring the tail would
    # mean confidently addressing a message from a link whose shape says
    # otherwise.
    if len(rest) > 2:
        return None

    if len(rest) == 2:
        # Two numbers: the first is the topic. Reading them the other way round
        # addresses the first message of the thread instead of the intended one.
        topic_id = _message_id(rest[0]) or topic_id
        message_id = _message_id(rest[1])
    else:
        message_id = _message_id(rest[0])

    if message_id is None:
        return None
    return TelegramLink(
        chat=chat,
        message_id=message_id,
        topic_id=topic_id,
        private=private,
        comment_id=comment_id,
    )


def format_message_link(
    *,
    username: str | None,
    chat_id: int | None,
    message_id: int,
    topic_id: int | None = None,
) -> str | None:
    """Build the permalink for one message, or return ``None`` honestly.

    Not every message has an address. A one-to-one conversation and a basic
    (non-super) group have no `t.me` form at all, and inventing one — or
    printing a link that opens the wrong thing — is worse than saying there
    isn't one.
    """
    if username:
        base = f"https://t.me/{username}"
    elif chat_id is not None and str(chat_id).startswith("-100"):
        base = f"https://t.me/c/{str(chat_id)[4:]}"
    else:
        return None
    if topic_id:
        return f"{base}/{topic_id}/{message_id}"
    return f"{base}/{message_id}"
