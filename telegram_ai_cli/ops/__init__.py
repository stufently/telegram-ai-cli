"""Every operation, registered by importing it.

Importing this package is what populates
:data:`telegram_ai_cli.opspec.REGISTRY`, so the CLI, the MCP server and
``tg-ai schema`` all import it before they build their surfaces. A module that
is not listed here has no CLI command and no tool, however complete its code is
— which is why the list is explicit rather than a directory scan: a file that
fails to import should break the build loudly, not disappear from the tool list
quietly.

The invariants run at the bottom, at import time. They are cheap, and the
alternative is discovering a duplicate tool name or a write operation with a
direct MCP tool the first time somebody exercises that one path.

Nothing here imports Telethon. Operations import it inside their handlers, so
``--help``, schema generation and the test suite work on a machine that has
never had it installed.
"""

from __future__ import annotations

from ..opspec import REGISTRY
from . import accounts, chats, contacts, fleet, inbox, media, search, write

__all__ = [
    "REGISTRY",
    "accounts",
    "chats",
    "contacts",
    "fleet",
    "inbox",
    "media",
    "search",
    "write",
]

REGISTRY.check_invariants()
