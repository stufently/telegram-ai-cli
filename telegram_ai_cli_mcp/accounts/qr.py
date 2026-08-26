"""Drawing a ``tg://login`` URL as a QR code made of terminal blocks.

One function, deliberately: the login flow decides *when* a code is shown, this
decides only what it looks like. Keeping it pure is what lets the shape of the
output be asserted by a test without a Telegram client anywhere near it.

Two rows of modules share one line of text (``▀``/``▄``/``█``), because a code
drawn one module per line is twice as tall as most terminals and scrolls out of
view while the camera is still focusing.

**Which way round?** A scanner expects dark modules on a light background, and a
terminal has no idea what colour its own background is. The default draws dark
modules as solid blocks, which is correct on a light background;
:paramref:`invert` swaps them for the dark themes most terminals ship with
today. Neither guess is universally right, which is why the caller can flip it
and why the raw URL is always printed alongside — a URL can be pasted into
Telegram Desktop when no rendering works at all.

The URL contains the login token, and the token *is* the login: whatever imports
it becomes the account. Nothing here logs, stores or returns it in any form but
the drawing itself — see :mod:`telegram_ai_cli_mcp.accounts.login` for the rest of
that rule.
"""

from __future__ import annotations

import io
from typing import Final

#: Quiet zone, in modules. The specification asks for four; two is the smallest
#: margin phone scanners reliably cope with and keeps a version-4 code inside an
#: 80-column terminal.
DEFAULT_BORDER: Final = 2


def render_qr(url: str, *, invert: bool = False, border: int = DEFAULT_BORDER) -> str:
    """Return ``url`` drawn as a block-character QR code, newline separated.

    ``invert`` swaps solid blocks and gaps, for a terminal with a dark
    background. Raises :class:`ValueError` on an empty URL rather than drawing
    an empty code that a person would stand there scanning.
    """
    text = str(url or "").strip()
    if not text:
        raise ValueError("nothing to encode: the login URL is empty")

    import qrcode

    code = qrcode.QRCode(
        # Error correction L, not the library default M: the payload is a
        # ~60-character URL shown on a screen a hand's width from the camera,
        # not printed on a box, and every level up adds modules — which on a
        # terminal means lines that wrap and a code that cannot be scanned at
        # all.
        error_correction=qrcode.ERROR_CORRECT_L,
        border=border,
    )
    code.add_data(text)
    code.make(fit=True)

    out = io.StringIO()
    # tty=False on purpose: colour escapes would be baked into a string this
    # function's caller may not be sending to a terminal at all.
    code.print_ascii(out=out, tty=False, invert=invert)
    return out.getvalue().rstrip("\n")
