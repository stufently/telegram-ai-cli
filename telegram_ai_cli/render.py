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

One more class of character is defanged here for a different reason: the
delimiters :mod:`telegram_ai_cli.untrusted` uses to frame stranger-written
text. Anything rendered through this module — a plan summary, a warning, a
table cell — gets no wrapper of its own, so without this a forged
``⟦/untrusted⟧`` inside a chat title would reach a reader looking exactly like
the real frame closing.
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

#: The delimiters :mod:`telegram_ai_cli.untrusted` frames stranger-written text
#: with. They are defanged here too, so that a forged marker cannot survive on
#: the paths that render text for a terminal — a plan summary, a warning, a
#: table cell — where no wrapper is applied and nothing else would strip it.
_MARKER_DELIMITERS = str.maketrans({"⟦": "[", "⟧": "]"})

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
    text = text.translate(_MARKER_DELIMITERS)

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


#: Largest first. How many of them are spoken is the caller's decision, and it
#: is not cosmetic: two units read better ("in about 3 days 4 hours") but round
#: *down*, so 25h59m becomes "1 day 1 hour". Where the number is the thing being
#: approved — how long a chat stays muted — every non-zero unit is spoken.
_DURATION_UNITS: tuple[tuple[int, str], ...] = (
    (86400, "day"),
    (3600, "hour"),
    (60, "minute"),
    (1, "second"),
)


def humanize_duration(seconds: float, *, max_units: int = 2) -> str:
    """A span of time as a person would say it, for a plan summary.

    ``28800`` is not something a reviewer can judge — "8 hours" is. This lives
    next to the other approval-surface rendering because that is the only place
    it is used: a duration in machine form belongs in the plan's parameters,
    and a duration in words belongs in the sentence somebody approves.

    ``max_units`` truncates rather than rounds, which is why a caller whose
    duration *is* the decision passes ``max_units=4`` and says the whole thing.
    """
    remaining = int(seconds)
    if remaining <= 0:
        return "no time at all"

    parts: list[str] = []
    for size, name in _DURATION_UNITS:
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{count} {name}{'s' if count != 1 else ''}")
        if len(parts) == max_units:
            break
    return " ".join(parts)


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
