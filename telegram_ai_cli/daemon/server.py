"""One process owning one account's client, so several callers can share it.

An auth key admits exactly one connected client — two desynchronise the message
sequence and Telegram may revoke the session — and
:mod:`telegram_ai_cli.accounts.lock` enforces that with an exclusive ``flock``
held for as long as a client is open. The cost lands on the caller: a
``telegram_watch`` holds the key for up to five minutes, and everything else
aimed at that account fails immediately with ``SESSION_LOCKED`` rather than
waiting its turn.

This daemon changes who holds the key, not how many hold it. One process opens
the account, keeps it, and answers *named operations* over a Unix socket. Local
callers then queue behind each other instead of being refused.

Four things it deliberately is not.

**It is not a trust boundary that can be skipped.** Every request is
revalidated and runs through the same registry, the same policy kernel and the
same audit log as a direct call. The socket replaces the transport, nothing
else.

**It is not an RPC surface.** ``run`` takes an operation name from the registry.
There is no endpoint that names an MTProto method or an attribute of the
client, because either would hand a caller the account with the policy removed.

**It is not a supervisor.** Nothing restarts it, nothing spawns it on demand:
a person starts it, and a client that finds no socket falls back to opening the
account itself. Idle timeout and SIGTERM are the whole lifecycle.

**It is not a queue with a policy.** Requests are served in the order the lock
grants them. Priorities, cancellation and back-pressure classes are the shape
this would grow into, and none of them has been asked for.

The concurrency model is two layers. *Accepting and framing* are fully
concurrent — one task per connection — so a slow operation never stops the
socket answering the next caller. *Executing* is serialised by a single
``asyncio.Lock``, because the Telethon client underneath is one connection and
issuing overlapping requests on it is the thing the lock on disk exists to
prevent. A slow request therefore delays later requests and blocks nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any, Protocol

from ..accounts.lock import SessionLock
from ..errors import (
    InvalidInput,
    ProfileForbidden,
    SessionLocked,
    TelegramAIError,
)
from . import paths as daemon_paths
from .protocol import (
    PROTOCOL_VERSION,
    error_response,
    ok_response,
    read_frame,
    write_frame,
)

log = logging.getLogger(__name__)

#: How long a connected peer has to send its request before it is dropped. A
#: client that connects and says nothing would otherwise hold a task for ever.
_REQUEST_HEADER_TIMEOUT = 30.0

#: Ceiling on waiting for in-flight work at shutdown. Past it the operation is
#: abandoned rather than the shutdown being: a daemon that cannot be stopped is
#: worse than an operation that has to be retried.
_DRAIN_TIMEOUT = 30.0


class DaemonSession(Protocol):
    """What the daemon owns for the life of the process.

    Kept behind a protocol so the socket, the race and the lifecycle can be
    tested without a Telegram account; the real one is
    :class:`telegram_ai_cli.daemon.service.RegistrySession`.
    """

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def run(
        self,
        operation: str,
        params: dict[str, Any],
        *,
        actor: str,
        policy: str | None = None,
    ) -> dict[str, Any]: ...


class AccountDaemon:
    """Serves one account over one Unix socket."""

    def __init__(
        self,
        *,
        account: str,
        socket_path: Path,
        bootstrap_lock_path: Path,
        session: DaemonSession,
        idle_timeout: float = 300.0,
        max_connections: int = 64,
        bootstrap_wait: float = 5.0,
        install_signal_handlers: bool = True,
    ) -> None:
        self.account = account
        self.socket_path = Path(socket_path)
        self.bootstrap_lock_path = Path(bootstrap_lock_path)
        self.session = session
        self.idle_timeout = float(idle_timeout)
        self.max_connections = int(max_connections)
        self.bootstrap_wait = float(bootstrap_wait)
        self.install_signal_handlers = install_signal_handlers

        self.started = asyncio.Event()
        self.bound = False

        self._stop = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._inflight = 0
        self._last_activity = time.monotonic()
        self._socket_claimed = False
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()

    # -- lifecycle ---------------------------------------------------------

    async def serve(self) -> str:
        """Become the daemon, or report that one is already running.

        Returns ``"served"`` after a full life (bind, serve, shut down) and
        ``"already-running"`` when another process got there first. Losing the
        race is not an error: the caller's goal — a daemon for this account —
        is satisfied either way.
        """
        outcome = await self._bootstrap()
        if outcome == "already-running":
            return outcome

        try:
            await self._serve_until_stopped()
        finally:
            await self._shutdown()
        return "served"

    def request_shutdown(self) -> None:
        """Stop accepting and wind down. Safe to call more than once."""
        self._stop.set()

    async def _bootstrap(self) -> str:
        """Claim the socket under a lock held only for the claim itself.

        Two processes starting at the same moment must end with one daemon and
        one client, never with two clients on one auth key. The `flock` makes
        the claim atomic; it is released as soon as the socket is bound and the
        account is open, because the thing that keeps a *second* daemon from
        connecting after that is the live socket, not the lock.
        """
        try:
            bootstrap = SessionLock(self.bootstrap_lock_path).acquire()
        except SessionLocked:
            # Somebody else is mid-claim. Their socket is about to appear; wait
            # for it rather than racing them for a lock we do not need.
            if await self._wait_for_peer(self.bootstrap_wait):
                log.info("account %s: a daemon is already running", self.account)
                return "already-running"
            raise

        try:
            if daemon_paths.require_bindable(self.socket_path):
                if await self._peer_listening():
                    log.info("account %s: a daemon is already running", self.account)
                    return "already-running"
                # A socket nobody is listening on: what SIGKILL leaves behind.
                # Replaced, not inherited — binding onto it would fail, and
                # trusting it would send every client into a black hole.
                log.warning("account %s: replacing a stale socket", self.account)
                self.socket_path.unlink(missing_ok=True)

            # The run lock is taken before the listener exists, so a client that
            # connects while the account is still being opened waits instead of
            # meeting a half-built daemon.
            async with self._run_lock:
                self._server = await asyncio.start_unix_server(
                    self._on_connection, path=str(self.socket_path)
                )
                os.chmod(self.socket_path, 0o600)
                self._socket_claimed = True
                self.bound = True
                try:
                    await self.session.open()
                except BaseException:
                    await self._unbind()
                    self.remove_socket_if_ours()
                    raise
        finally:
            bootstrap.release()

        self._install_signals()
        self._last_activity = time.monotonic()
        self.started.set()
        log.info(
            "account %s: daemon listening on %s (idle timeout %.0fs)",
            self.account,
            self.socket_path,
            self.idle_timeout,
        )
        return "served"

    async def _serve_until_stopped(self) -> None:
        watchdog = asyncio.create_task(self._watch_idle())
        try:
            await self._stop.wait()
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog

    async def _watch_idle(self) -> None:
        if self.idle_timeout <= 0:
            await asyncio.Event().wait()  # pragma: no cover - "never idle out"
        tick = min(1.0, max(self.idle_timeout / 4, 0.02))
        while not self._stop.is_set():
            await asyncio.sleep(tick)
            if self._inflight:
                continue
            if time.monotonic() - self._last_activity >= self.idle_timeout:
                log.info("account %s: idle for %.0fs; stopping", self.account, self.idle_timeout)
                self._stop.set()
                return

    async def _shutdown(self) -> None:
        self._remove_signals()
        # Unlink *before* closing the listener. While the socket is still bound
        # nobody else can have taken the path, so removing it cannot remove a
        # successor's; the other order leaves a window in which it can.
        self.remove_socket_if_ours()
        await self._unbind()
        await self._drain()
        with contextlib.suppress(Exception):
            await self.session.close()

    async def _unbind(self) -> None:
        if self._server is None:
            return
        self._server.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()
        self._server = None
        self.bound = False

    async def _drain(self) -> None:
        """Let in-flight requests finish, then make sure they really have.

        The cancelled tasks are awaited rather than merely cancelled: the next
        step disconnects the shared Telethon client, and a request still
        unwinding on it would be issuing calls against a client being torn down.
        """
        if not self._connections:
            return
        pending = list(self._connections)
        _, still_running = await asyncio.wait(pending, timeout=_DRAIN_TIMEOUT)
        if not still_running:  # pragma: no branch - the common case
            return
        for task in still_running:  # pragma: no cover - only on a stuck request
            task.cancel()
        await asyncio.gather(*still_running, return_exceptions=True)  # pragma: no cover

    def remove_socket_if_ours(self) -> None:
        """Unlink the socket, but only if it is still the one we bound.

        Ownership is tracked as a flag rather than re-derived from the file,
        because a file has no identity that survives this. ``st_ino`` is reused
        — the next socket bound at the same path routinely gets the number the
        previous one had — and ``st_ctime_ns`` is no better, since the kernel
        caches the current time to about a timer tick, so two sockets created a
        fraction of a millisecond apart carry the same one. Both were tried;
        both called a stranger's socket our own.

        What makes the flag sufficient is the order in :meth:`_shutdown`: the
        unlink happens while the listener is still bound, so the path cannot
        have been claimed by anyone else in between. Afterwards the flag is
        clear and this is a no-op, which is exactly the case that matters — a
        successor daemon binding as this one finishes stopping.
        """
        if not self._socket_claimed:
            return
        self._socket_claimed = False
        with contextlib.suppress(OSError):
            self.socket_path.unlink()

    def _install_signals(self) -> None:
        if not self.install_signal_handlers:
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_shutdown)
            except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover
                # Not the main thread, or a platform without them. The idle
                # timeout is still a way out, so this is a note, not a refusal.
                log.warning("account %s: cannot install a handler for %s", self.account, sig)

    def _remove_signals(self) -> None:
        if not self.install_signal_handlers:
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(sig)

    # -- peers -------------------------------------------------------------

    async def _peer_listening(self) -> bool:
        """Is anything listening on the socket that is already there?

        The question is answered by ``connect`` alone, and deliberately not by
        whether a ping comes back. A daemon that is alive but slow to answer —
        a synchronous handler holding the event loop, a machine under load —
        would fail a ping and be declared stale, and the caller would then
        *unlink its socket*, leaving a live daemon holding the auth key that no
        client can reach any more. On a Unix socket a successful connect means
        a listener exists; a refused one (``ECONNREFUSED``) means the file
        outlived its process, which is exactly what stale means.
        """
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)), timeout=2.0
            )
        except (TimeoutError, OSError):
            return False
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True

    async def _wait_for_peer(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.socket_path.exists() and await self._peer_listening():
                return True
            await asyncio.sleep(0.05)
        return False

    # -- requests ----------------------------------------------------------

    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if len(self._connections) >= self.max_connections:
            with contextlib.suppress(Exception):
                await write_frame(
                    writer,
                    error_response(
                        SessionLocked(
                            f"account {self.account}: daemon connection limit reached",
                            suggestion="Retry after another local caller disconnects.",
                            retry_after=1,
                        )
                    ),
                )
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        if task is not None:
            self._connections.add(task)
        try:
            request = await asyncio.wait_for(read_frame(reader), timeout=_REQUEST_HEADER_TIMEOUT)
            if request is None:
                return
            response = await self._answer(request)
            try:
                await write_frame(writer, response)
            except TelegramAIError as exc:
                # A refusal about the *answer* — it did not fit in a frame.
                await write_frame(writer, error_response(exc))
        except TelegramAIError as exc:
            with contextlib.suppress(Exception):
                await write_frame(writer, error_response(exc))
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:
            log.exception("account %s: connection failed", self.account)
        finally:
            if task is not None:
                self._connections.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _answer(self, request: dict[str, Any]) -> dict[str, Any]:
        """Turn a request into a response, refusals included.

        An unexpected exception becomes a generic refusal rather than its own
        repr: a Telethon or opentele traceback carries proxy credentials and
        session material, and this one is about to be written to a socket.
        """
        try:
            return await self._dispatch(request)
        except TelegramAIError as exc:
            return error_response(exc)
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:
            log.exception("account %s: operation failed", self.account)
            return error_response(
                TelegramAIError(
                    "the daemon could not run this operation",
                    suggestion=(
                        "See the daemon's own log. The detail is withheld here because an "
                        "exception from this layer can carry account material."
                    ),
                )
            )

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        version = request.get("v")
        if version != PROTOCOL_VERSION:
            raise InvalidInput(
                f"unsupported protocol version {version!r}",
                suggestion=f"This daemon speaks version {PROTOCOL_VERSION}.",
            )

        action = request.get("action")
        if action == "ping":
            return ok_response(
                {
                    "kind": "pong",
                    "account": self.account,
                    "pid": os.getpid(),
                    "idle_timeout": self.idle_timeout,
                    "max_connections": self.max_connections,
                }
            )
        if action != "run":
            # Two verbs, both named here. Anything else — including a method to
            # invoke or an attribute to read — is not a thing this speaks.
            raise InvalidInput(
                f"unknown action {action!r}",
                suggestion="The daemon understands 'ping' and 'run' only.",
            )

        operation = request.get("operation")
        params = request.get("params", {})
        actor = request.get("actor", "mcp")
        policy = request.get("policy")
        if policy is not None and not isinstance(policy, str):
            raise InvalidInput("'policy' must be a string")
        if not isinstance(operation, str) or not operation:
            raise InvalidInput("'operation' must be the name of a registered operation")
        if not isinstance(params, dict):
            raise InvalidInput("'params' must be an object")
        if actor not in {"cli", "mcp"}:
            raise ProfileForbidden(f"unknown actor {actor!r}")

        self._inflight += 1
        try:
            async with self._run_lock:
                body = await self.session.run(operation, params, actor=actor, policy=policy)
        finally:
            self._inflight -= 1
            self._last_activity = time.monotonic()
        return ok_response(body)
