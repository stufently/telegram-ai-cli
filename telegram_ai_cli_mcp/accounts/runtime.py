"""Facts about Telethon and opentele that everything connecting needs.

All three functions look up their answer at call time instead of at import time.
That is deliberate: the package must import on a machine with neither library
installed, so a module-level ``from telethon import errors`` would break the CLI
for someone who only wanted ``--help``.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings


def recoverable_errors() -> tuple[type[BaseException], ...]:
    """Exception classes that may be swallowed for a single account.

    ``OpenTeleException`` derives from ``BaseException``, not ``Exception``, so
    a plain ``except Exception`` lets one corrupt tdata folder take down a run
    over fifty accounts. Listing it explicitly keeps ``CancelledError``,
    ``KeyboardInterrupt`` and ``SystemExit`` propagating, which a blanket
    ``except BaseException`` would not.
    """
    try:
        from opentele.exception import OpenTeleException
    except ImportError:  # opentele is optional for anything that never connects
        return (Exception,)
    return (Exception, OpenTeleException)


def revoked_errors() -> tuple[type[BaseException], ...]:
    """Telethon errors that mean the auth key is gone for good.

    Looked up by name rather than imported directly: the set of error classes
    Telethon exports shifts between releases, and an account fleet must not fail
    to start because one of them was renamed. An empty tuple in an ``except``
    clause simply never matches, which is the right behaviour when Telethon is
    not installed at all.
    """
    try:
        from telethon import errors
    except ImportError:  # pragma: no cover - only when Telethon is absent
        return ()
    return tuple(
        error
        for error in (
            getattr(errors, "AuthKeyDuplicatedError", None),
            getattr(errors, "SessionRevokedError", None),
            getattr(errors, "AuthKeyUnregisteredError", None),
        )
        if error is not None
    )


def telethon_options(settings: Settings | None = None) -> dict[str, Any]:
    """Client kwargs that disable Telethon's own retrying.

    ``flood_sleep_threshold=0`` stops it from sleeping out a flood wait and
    retrying by itself; ``request_retries=1`` stops it from resending a request
    whose outcome we have not classified yet. Both would otherwise deliver a
    message this project decided not to resend — the retry policy belongs to the
    plan machinery, not to the transport.

    They are settings rather than constants because a deployment that genuinely
    wants Telethon's behaviour should have to say so out loud.
    """
    settings = settings or Settings()
    return {
        "flood_sleep_threshold": settings.telethon_flood_sleep_threshold,
        "request_retries": settings.telethon_request_retries,
    }
