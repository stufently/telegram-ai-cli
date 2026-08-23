"""The vocabulary of the account fleet: where a session came from, and how it is.

Both enums are string-valued because they are stored in SQLite and printed in
listings; an integer would save nothing and turn every hand-run query into a
lookup exercise.

``revoked`` is separate from ``auth_failed`` for one reason: it is terminal.
``auth_failed`` invites another attempt — the proxy was down, the key file was
missing. A revoked session will never authorise again no matter how many times
it is tried, and reconnecting to one in a loop is how a fleet turns into a
retry storm against Telegram.
"""

from __future__ import annotations

from enum import StrEnum


class AccountSource(StrEnum):
    """Where the auth key came from."""

    TDATA = "tdata"
    """A Telegram Desktop data folder, converted once into a ``.session``."""

    SESSION_FILE = "session_file"
    """A Telethon SQLite ``.session`` — also what interactive login produces."""

    STRING_SESSION = "string_session"
    """A Telethon ``StringSession``, kept in a ``0600`` file next to the rest."""


class AccountStatus(StrEnum):
    NEW = "new"
    """Registered, never yet connected."""

    OK = "ok"
    """Connected and authorised at least once."""

    AUTH_FAILED = "auth_failed"
    """Something went wrong that may not go wrong again. Retryable."""

    REVOKED = "revoked"
    """Telegram killed the session. Terminal: do not reconnect."""

    DISABLED = "disabled"
    """Switched off by the operator. Never selected for work."""


#: Statuses an account may still be used from. Anything else is skipped when
#: work is handed out, which is what keeps a dead session from being retried on
#: every single operation.
USABLE_STATUSES = frozenset({AccountStatus.NEW, AccountStatus.OK, AccountStatus.AUTH_FAILED})
