"""MCP over HTTP — loopback only, and never without a token.

stdio is the transport this project was built around: the client launches the
server, the pipe is the trust boundary, and there is nothing on a network to
find. HTTP is for the case stdio cannot serve — a client in a container, an
editor that only speaks URLs — and it drags the whole boundary along with it.

Two rules make that survivable, and both are enforced at start-up rather than
documented as advice.

**Loopback only.** ``0.0.0.0``, ``::`` and any routable address are refused
before a socket is opened. A hostname is refused too, however innocent: the
name is resolved by something outside this process, and "it points at 127.0.0.1
here" is a fact about one machine's `/etc/hosts` at one moment. This is a
server that reads a personal Telegram account; being reachable from the network
is not a configuration mistake to warn about.

**A bearer token, always.** There is no "no auth on localhost" mode, because
localhost is not a person: every other process and every other user on the
machine reaches it too. The token comes from an environment variable named in
the config — never from the config itself — and a missing one is a refusal to
start, not a warning and a server anyway.

The refusal itself says nothing. A wrong token and a missing token get byte-for-
byte the same 401, and the comparison is `hmac.compare_digest`, so neither the
body nor the timing tells a caller how close it got.

The transport is the MCP Python SDK's **Streamable HTTP**
(`mcp.server.streamable_http_manager.StreamableHTTPSessionManager`), which is
what the pinned SDK — mcp 2.0.0 — offers for HTTP; the older HTTP+SSE transport
is deprecated in the specification and is not used here. `starlette` and
`uvicorn` are already hard dependencies of that SDK, so this adds no dependency
of its own.

What this does *not* change: every tool is the same operation, the same policy
kernel decides, and there is still no tool that applies a plan. A transport
does not get to widen a surface.
"""

from __future__ import annotations

import contextlib
import hmac
import ipaddress
import logging
import math
import time
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .config import HTTPConfig, Settings, load_settings
from .errors import InvalidInput

log = logging.getLogger(__name__)

#: What an unauthenticated caller is told. Deliberately nothing.
_UNAUTHORIZED_BODY = b"Unauthorized"
_TOO_LARGE_BODY = b"Request body too large"
_RATE_LIMITED_BODY = b"Too Many Requests"


def require_loopback(host: str) -> str:
    """Return ``host`` if it is a loopback literal, or refuse.

    Fail-closed in both directions: an address that is not loopback is refused,
    and so is anything that is not an address at all. ``localhost`` looks safe
    and usually is, but it is a *name*, and what a name resolves to is decided
    by a resolver this process does not control.
    """
    text = (host or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]  # the bracketed form of an IPv6 literal in a URL
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        raise InvalidInput(
            f"http.host must be a loopback IP address, not {host!r}",
            suggestion=(
                "Use 127.0.0.1 or ::1. A hostname is refused because what it resolves to is "
                "not decided here, and this server reads a personal Telegram account."
            ),
        ) from None
    if not address.is_loopback:
        raise InvalidInput(
            f"http.host must be a loopback address; {host!r} is reachable from elsewhere",
            suggestion=(
                "Use 127.0.0.1 or ::1, and put a tunnel in front if a remote client needs it. "
                "Binding this to a routable address publishes the account."
            ),
        )
    return host


def load_token(config: HTTPConfig) -> str:
    """Read the bearer token, or refuse to start.

    Read at start-up rather than per request on purpose: a server that starts
    and only discovers at the first call that it has no token is a server that
    was already listening.
    """
    import os

    token = (os.environ.get(config.token_env) or "").strip()
    if not token:
        raise InvalidInput(
            f"the HTTP transport needs a bearer token in ${config.token_env}",
            suggestion=(
                f"Generate one and export it: {config.token_env}=$(python -c "
                "'import secrets; print(secrets.token_urlsafe(32))'). There is no "
                "unauthenticated mode, on localhost or anywhere else."
            ),
        )
    if len(token) < config.min_token_length:
        raise InvalidInput(
            f"${config.token_env} is shorter than {config.min_token_length} characters",
            suggestion="Use a random token, not a password you can remember.",
        )
    return token


def log_startup(host: str, port: int, path: str, token: str) -> None:
    """Say where it is listening and that auth is on — never what the token is.

    ``token`` is taken as an argument and deliberately unused: the signature is
    what stops a later "just log the first few characters for debugging", and a
    prefix is simply fewer characters left to guess.
    """
    del token
    log.info(
        "MCP HTTP transport listening on %s:%s%s (loopback only, bearer auth enabled)",
        host,
        port,
        path,
    )


class BearerAuth:
    """Pure-ASGI bearer check in front of everything, including 404s.

    In front of *everything* on purpose: mounted inside a router, an unknown
    path would answer 404 before auth ran, and the shape of a server is
    information too.

    Exactly one scope type is let past unchecked — ``lifespan`` — because it is
    the server starting the app rather than anybody's request. A ``websocket``
    scope is *not*: the transport does not use one, an anonymous client can open
    one anyway, and passing it inward reaches the SDK unauthenticated.
    """

    def __init__(self, app: Any, *, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind == "lifespan":
            await self._app(scope, receive, send)
            return
        if kind == "websocket":
            # Closed rather than passed on. Allow-list, not deny-list: a scope
            # type nobody thought about must not arrive inside authenticated.
            await send({"type": "websocket.close", "code": 1008})
            return
        if kind != "http":
            return
        if not self._authorized(scope):
            await self._refuse(send)
            return
        await self._app(scope, receive, send)

    def _authorized(self, scope: Any) -> bool:
        for name, value in scope.get("headers") or []:
            if name.lower() != b"authorization":
                continue
            scheme, _, presented = value.partition(b" ")
            if scheme.lower() != b"bearer":
                return False
            # compare_digest, not ==: an ordinary comparison returns as soon as
            # two bytes differ, and that timing is a byte-at-a-time oracle.
            return hmac.compare_digest(presented.decode("latin-1").strip(), self._token)
        return False

    async def _refuse(self, send: Any) -> None:
        # Identical for a missing, malformed and wrong token. Anything more
        # specific tells a caller which half of the guess was right.
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"www-authenticate", b"Bearer"),
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})


class RequestGuard:
    """Bound authenticated HTTP request size and arrival rate before the SDK."""

    def __init__(self, app: Any, *, max_body_bytes: int, requests_per_minute: int) -> None:
        self._app = app
        self._max_body_bytes = int(max_body_bytes)
        self._requests_per_minute = int(requests_per_minute)
        self._arrivals: deque[float] = deque()
        self._rate_lock: Any = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        allowed, retry_after = await self._accept_arrival()
        if not allowed:
            await _plain_response(
                send,
                status=429,
                body=_RATE_LIMITED_BODY,
                headers=[(b"retry-after", str(retry_after).encode())],
            )
            return

        declared = self._content_length(scope)
        if declared is not None and declared > self._max_body_bytes:
            await _plain_response(send, status=413, body=_TOO_LARGE_BODY)
            return

        if scope.get("method", "GET").upper() not in {"POST", "PUT", "PATCH"}:
            await self._app(scope, receive, send)
            return

        buffered: list[dict[str, Any]] = []
        size = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message.get("type") != "http.request":
                break
            size += len(message.get("body", b""))
            if size > self._max_body_bytes:
                await _plain_response(send, status=413, body=_TOO_LARGE_BODY)
                return
            if not message.get("more_body", False):
                break

        async def replay() -> dict[str, Any]:
            if buffered:
                return buffered.pop(0)
            return await receive()

        await self._app(scope, replay, send)

    async def _accept_arrival(self) -> tuple[bool, int]:
        import asyncio

        if self._rate_lock is None:
            self._rate_lock = asyncio.Lock()
        async with self._rate_lock:
            now = time.monotonic()
            cutoff = now - 60.0
            while self._arrivals and self._arrivals[0] <= cutoff:
                self._arrivals.popleft()
            if len(self._arrivals) >= self._requests_per_minute:
                retry = max(1, math.ceil(self._arrivals[0] + 60.0 - now))
                return False, retry
            self._arrivals.append(now)
            return True, 0

    def _content_length(self, scope: Any) -> int | None:
        for name, value in scope.get("headers") or []:
            if name.lower() != b"content-length":
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            return max(0, parsed)
        return None


def build_http_app(
    *,
    config: HTTPConfig | None = None,
    config_path: Path | None = None,
) -> BearerAuth:
    """Assemble the ASGI app, refusing before anything is bound.

    Both refusals happen here rather than in :func:`serve_http` so that they are
    reachable from a test without opening a socket — and so that a caller
    embedding this app cannot skip them.
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    import telegram_ai_cli_mcp.ops  # noqa: F401  (registers every operation)

    from .mcp_server import build_server

    config = config or HTTPConfig()
    require_loopback(config.host)
    token = load_token(config)

    manager = StreamableHTTPSessionManager(
        build_server(config_path=config_path),
        # Without a timeout the manager keeps a transport and a task per session
        # for the life of the process, and a session is created by any accepted
        # request — including ones that go on to fail. An abandoned client
        # therefore leaks one until a restart.
        session_idle_timeout=config.session_idle_timeout_seconds,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # The manager owns a task group for its sessions and can only be
        # entered once; the app's lifespan is exactly that scope.
        async with manager.run():
            yield

    endpoint = config.path.rstrip("/") or "/"

    async def handle(scope: Any, receive: Any, send: Any) -> None:
        # Matched here rather than with `Mount(config.path)`: a Mount answers
        # the un-slashed form with a 307 to the slashed one, and a client that
        # POSTs its `initialize` to the documented URL would be redirected
        # rather than served.
        if (scope.get("path") or "/").rstrip("/") != endpoint.rstrip("/"):
            await _not_found(send)
            return
        await manager.handle_request(scope, receive, send)

    inner = Starlette(routes=[Mount("", app=handle)], lifespan=lifespan)
    guarded = RequestGuard(
        inner,
        max_body_bytes=config.max_request_body_bytes,
        requests_per_minute=config.requests_per_minute,
    )
    return BearerAuth(guarded, token=token)


async def _plain_response(
    send: Any,
    *,
    status: int,
    body: bytes,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    response_headers = list(headers or [])
    response_headers.extend(
        [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ]
    )
    await send({"type": "http.response.start", "status": status, "headers": response_headers})
    await send({"type": "http.response.body", "body": body})


async def _not_found(send: Any) -> None:
    body = b"Not Found"
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def serve_http(
    *,
    settings: Settings | None = None,
    config_path: Path | None = None,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Run the MCP server over Streamable HTTP on a loopback address."""
    import uvicorn

    settings = settings or load_settings(config_path)
    config = settings.http
    if host is not None:
        config = config.model_copy(update={"host": host})
    if port is not None:
        config = config.model_copy(update={"port": port})

    app = build_http_app(config=config, config_path=config_path)
    log_startup(config.host, config.port, config.path, load_token(config))

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            log_level="info",
            # Access logs on a token-authenticated endpoint would record the
            # path and the outcome; the header is never in them, and this keeps
            # the volume down on an SSE stream that is one long request.
            access_log=False,
        )
    )
    await server.serve()
