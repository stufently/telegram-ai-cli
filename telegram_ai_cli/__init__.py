"""telegram-ai-cli — one codebase behind a CLI and an MCP server.

Nothing here imports Telethon at module scope. The CLI has to start, print help
and answer ``--version`` on a machine that has never seen a Telegram account,
and the test suite has to run without the network; both break if connecting to
Telegram is a side effect of importing the package.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0.dev0"
