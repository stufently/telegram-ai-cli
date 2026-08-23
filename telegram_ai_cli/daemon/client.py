"""Talking to an account daemon, and knowing when there isn't one.

One rule decides everything here: **the fallback to opening the account
directly happens only when the connection was never established.** Once a
request has left, a failure is reported as a failure. The reason is planning: a
plan operation that timed out mid-flight may already have recorded a plan, and
retrying it locally would record a second one — the same duplicate-send
reasoning that makes `PlanUnknownOutcome` deliberately not retryable.

So :class:`DaemonUnavailable` means "no daemon answered", and it is the only
condition a caller may treat as "carry on without one".
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from ..errors import TelegramAIError
from . import paths as daemon_paths
from .protocol import PROTOCOL_VERSION, read_frame, write_frame

#: Connecting to a socket on the same machine is instant or it is not happening.
DEFAULT_CONNECT_TIMEOUT = 2.0

#: Generous: an operation the daemon is running is an operation this caller
#: would otherwise be running itself, with no timeout at all.
DEFAULT_REQUEST_TIMEOUT = 900.0


class DaemonUnavailable(Exception):
    """No daemon answered on this socket. The caller may proceed alone."""


async def call(
    socket_path: Path,
    payload: dict[str, Any],
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
) -> dict[str, Any]:
    """Send one request and read one reply.

    Raises :class:`DaemonUnavailable` when nothing is listening, and
    :class:`~telegram_ai_cli.errors.TelegramAIError` when the exchange itself
    broke after the connection was made.
    """
    path = Path(socket_path)
    # Refuse to speak through a symlink even as a client: whoever planted it
    # chooses the process on the other end.
    if not _connectable(path):
        raise DaemonUnavailable(f"no daemon socket at {path}")

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)), timeout=connect_timeout
        )
    except (TimeoutError, OSError) as exc:
        raise DaemonUnavailable(f"no daemon answered on {path}: {exc}") from None

    try:
        await write_frame(writer, payload)
        reply = await asyncio.wait_for(read_frame(reader), timeout=request_timeout)
    except TimeoutError:
        raise TelegramAIError(
            f"the daemon for this account did not answer within {request_timeout:.0f}s",
            suggestion="Check the daemon's log; the request may still be running.",
        ) from None
    except (OSError, asyncio.IncompleteReadError) as exc:
        raise TelegramAIError(f"the connection to the account daemon broke: {exc}") from None
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    if reply is None:
        raise TelegramAIError("the account daemon closed the connection without answering")
    return reply


async def ping(socket_path: Path, **kwargs: Any) -> dict[str, Any]:
    """Who is serving this socket. Raises if the answer is a refusal."""
    return _unwrap(await call(socket_path, {"v": PROTOCOL_VERSION, "action": "ping"}, **kwargs))


async def run(
    socket_path: Path,
    *,
    operation: str,
    params: dict[str, Any],
    actor: str,
    policy: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run one registered operation on the daemon's client.

    ``operation`` is a registry name. There is no argument here that could
    carry a method to call or an attribute to read, and the daemon would refuse
    one anyway — see :mod:`telegram_ai_cli.daemon.service`.

    ``policy`` is the fingerprint of the configuration this caller believes it
    is running under. A daemon started under a different one refuses before
    running anything, so the caller can fall back to opening the account itself
    rather than silently borrowing a wider policy.
    """
    return _unwrap(
        await call(
            socket_path,
            {
                "v": PROTOCOL_VERSION,
                "action": "run",
                "operation": operation,
                "params": params,
                "actor": actor,
                "policy": policy,
            },
            **kwargs,
        )
    )


def _connectable(path: Path) -> bool:
    """True when a socket we own is sitting there; refuse anything odd."""
    try:
        return daemon_paths.require_bindable(path)
    except FileNotFoundError:  # pragma: no cover - require_bindable handles it
        return False


def _unwrap(reply: dict[str, Any]) -> dict[str, Any]:
    if reply.get("ok"):
        return reply
    error = reply.get("error") or {}
    raise DaemonRefusal(error)


class DaemonRefusal(TelegramAIError):
    """A refusal the daemon produced, carried back with its code intact.

    The daemon has already built the error payload the CLI and the MCP server
    print; re-deriving one from a string here would lose the code, the
    suggestion and ``retryable``.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(
            str(payload.get("message") or "the account daemon refused this operation"),
            suggestion=payload.get("suggestion"),
            retry_after=payload.get("retry_after"),
            details=payload.get("details"),
        )
        self.payload = dict(payload)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)
