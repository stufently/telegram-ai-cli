"""Make attacker-controlled text safe to print in a terminal.

The whole approval design rests on one assumption: when a human runs
``tg-ai plan show``, what appears on screen is what will be sent. Terminals
break that assumption for free. A message body carrying ``\\r`` or a CSI
sequence can erase the line it was printed on and draw a different one, so the
plan a person reads and the plan they approve stop being the same plan.

Nothing here tries to be clever about which sequences are dangerous. Escape
handling differs across terminal emulators, and a filter that models one of
them is a filter that another one walks through. Control characters are removed
outright; the only ones that survive are the newline and the tab, because a
message that legitimately spans lines should still read like one.

Bidirectional overrides get the same treatment. They do not move the cursor,
they reverse the visual order of a run of text, which is enough to make a
domain or a phone number render as something other than what it is.
"""

from __future__ import annotations

import re
import unicodedata

#: Kept because real messages contain them and they cannot reposition output.
_ALLOWED_CONTROLS = {"\n", "\t"}

#: Explicit direction controls (LRO/RLO/PDF/LRI/RLI/FSI/PDI and friends).
_BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩‎‏")

#: Zero-width characters, used to hide text rather than to move it.
_INVISIBLE = frozenset("​‌‍⁠﻿")

_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_OTHER = re.compile(r"\x1b[@-Z\\-_]")


def sanitize(text: str, *, replacement: str = "") -> str:
    """Strip everything that could make output lie about itself.

    Applied to every piece of Telegram-authored text before it reaches a
    terminal: message bodies, chat titles, display names, usernames, and the
    rendered description of a plan.
    """
    if not text:
        return text

    # Escape sequences first: removing the ESC alone would leave the payload
    # ("[2K") as visible garbage instead of dropping the whole sequence.
    text = _ANSI_OSC.sub(replacement, text)
    text = _ANSI_CSI.sub(replacement, text)
    text = _ANSI_OTHER.sub(replacement, text)

    out: list[str] = []
    for ch in text:
        if ch in _ALLOWED_CONTROLS:
            out.append(ch)
            continue
        if ch in _BIDI_CONTROLS or ch in _INVISIBLE:
            out.append(replacement)
            continue
        # Cc is C0/C1 controls, Cf is format characters, Cs is lone surrogates.
        if unicodedata.category(ch) in {"Cc", "Cf", "Cs"}:
            out.append(replacement)
            continue
        out.append(ch)
    return "".join(out)


def sanitize_line(text: str, *, limit: int | None = None) -> str:
    """Collapse to a single safe line, for tables and log records.

    Newlines survive :func:`sanitize` on purpose, but a value dropped into a
    table cell must not be able to add rows to it.
    """
    flat = " ".join(sanitize(text).split())
    if limit is not None and len(flat) > limit:
        return flat[: max(0, limit - 1)] + "…"
    return flat


def quote_for_review(text: str, *, limit: int = 4000) -> str:
    """Render a message body for a human who is about to approve sending it.

    Truncation is stated rather than silent: someone approving a send needs to
    know they are looking at part of it.
    """
    cleaned = sanitize(text)
    if len(cleaned) <= limit:
        return cleaned
    omitted = len(cleaned) - limit
    return f"{cleaned[:limit]}\n[… {omitted} more characters not shown]"
