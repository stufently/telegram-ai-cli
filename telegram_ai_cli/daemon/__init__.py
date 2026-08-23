"""A shared local daemon so several callers can use one account at once.

Opt-in and off by default. Without it, nothing changes: every caller opens the
account itself and holds the auth key for the duration, which is correct and is
also why a five-minute `telegram_watch` locks everything else out.

See :mod:`telegram_ai_cli.daemon.server` for the concurrency model and
:mod:`telegram_ai_cli.daemon.service` for why the socket cannot be asked for an
arbitrary MTProto method.
"""

from __future__ import annotations

from .client import DaemonRefusal, DaemonUnavailable
from .server import AccountDaemon, DaemonSession
from .service import PinnedRegistry, RegistrySession, SharedAccount, select_operation

__all__ = [
    "AccountDaemon",
    "DaemonRefusal",
    "DaemonSession",
    "DaemonUnavailable",
    "PinnedRegistry",
    "RegistrySession",
    "SharedAccount",
    "select_operation",
]
