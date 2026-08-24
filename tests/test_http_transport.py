"""The HTTP transport: loopback only, bearer token mandatory, 401 says nothing.

Every test here is about a refusal, because that is the whole value of this
transport. An MCP server for a personal Telegram account listening on a
routable address, or on localhost with "no auth needed locally", is an account
handed to whoever else is on the machine or the network.

The ASGI app is driven directly rather than through a test client: `httpx` is
not a dependency of this project and adding one to assert a 401 would be a poor
trade. A scope, a receive and a send are the whole protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import pytest

from telegram_ai_cli import http_server
from telegram_ai_cli.config import HTTPConfig
from telegram_ai_cli.errors import ErrorCode, TelegramAIError

TOKEN = "0123456789abcdef0123456789abcdef"  # noqa: S105 - a fixture, not a credential


async def call_asgi(
    app: Any,
    *,
    path: str = "/mcp",
    method: str = "POST",
    headers: list[tuple[bytes, bytes]] | None = None,
    body_bytes: bytes = b"",
) -> tuple[int, dict[bytes, bytes], bytes]:
    """Drive one request through an ASGI app and collect the response."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 55555),
        "server": ("127.0.0.1", 8765),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    status = 0
    response_headers: dict[bytes, bytes] = {}
    body = b""

    async def send(message: dict[str, Any]) -> None:
        nonlocal status, response_headers, body
        if message["type"] == "http.response.start":
            status = message["status"]
            response_headers = dict(message.get("headers") or [])
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")

    await app(scope, receive, send)
    return status, response_headers, body


@contextlib.asynccontextmanager
async def asgi_lifespan(app: Any) -> Any:
    """Start and stop an ASGI app the way a server would.

    The MCP session manager owns a task group entered in the app's lifespan, so
    a request driven at an app that was never started fails inside the SDK. A
    dozen lines here beat adding an HTTP client dependency to get them.
    """
    inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def receive() -> dict[str, Any]:
        return await inbound.get()

    async def send(message: dict[str, Any]) -> None:
        await outbound.put(message)

    scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}
    task = asyncio.create_task(app(scope, receive, send))
    await inbound.put({"type": "lifespan.startup"})
    started = await asyncio.wait_for(outbound.get(), timeout=10)
    assert started["type"] == "lifespan.startup.complete", started
    try:
        yield
    finally:
        await inbound.put({"type": "lifespan.shutdown"})
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(outbound.get(), timeout=10)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=10)


class Reached(Exception):
    """Raised by the stand-in downstream app to prove auth let a call through."""


async def downstream(scope: Any, receive: Any, send: Any) -> None:
    del scope, receive
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"through"})


# --- the bind address -------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    # RFC 5737 documentation ranges rather than real-looking LAN addresses: the
    # repository refuses to carry either, and these say the same thing.
    [
        "0.0.0.0",  # noqa: S104 - the value under test is the one being refused
        "::",
        "192.0.2.10",
        "198.51.100.4",
        "203.0.113.7",
        "2001:db8::1",
        "",
    ],
)
def test_a_routable_bind_address_is_refused(host: str) -> None:
    with pytest.raises(TelegramAIError) as excinfo:
        http_server.require_loopback(host)
    assert excinfo.value.code is ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.53", "::1", "[::1]"])
def test_loopback_addresses_are_accepted(host: str) -> None:
    assert http_server.require_loopback(host)


def test_a_hostname_is_refused_even_when_it_resolves_to_loopback() -> None:
    """A name is resolved by something this process does not control."""
    with pytest.raises(TelegramAIError):
        http_server.require_loopback("localhost")


# --- the token --------------------------------------------------------------


def test_no_token_configured_refuses_to_start(monkeypatch) -> None:
    monkeypatch.delenv("TGAI_HTTP_TOKEN", raising=False)
    with pytest.raises(TelegramAIError) as excinfo:
        http_server.load_token(HTTPConfig())
    assert excinfo.value.code is ErrorCode.INVALID_INPUT
    assert "TGAI_HTTP_TOKEN" in excinfo.value.message


def test_a_token_too_short_to_be_one_is_refused(monkeypatch) -> None:
    monkeypatch.setenv("TGAI_HTTP_TOKEN", "hunter2")
    with pytest.raises(TelegramAIError):
        http_server.load_token(HTTPConfig())


def test_a_configured_token_is_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("TGAI_HTTP_TOKEN", TOKEN)
    assert http_server.load_token(HTTPConfig()) == TOKEN


def test_the_token_never_reaches_a_log_line(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="telegram_ai_cli.http_server"):
        http_server.log_startup("127.0.0.1", 8765, "/mcp", TOKEN)

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "127.0.0.1:8765" in text
    assert "bearer" in text.lower()
    assert TOKEN not in text
    # Not even a prefix: four characters of a token is four fewer to guess.
    assert TOKEN[:4] not in text


# --- what the middleware does -----------------------------------------------


@pytest.mark.asyncio
async def test_a_request_without_a_token_gets_a_plain_401() -> None:
    app = http_server.BearerAuth(downstream, token=TOKEN)
    status, headers, body = await call_asgi(app)

    assert status == 401
    assert headers[b"www-authenticate"] == b"Bearer"
    assert body == b"Unauthorized"


@pytest.mark.asyncio
async def test_a_wrong_token_is_refused_and_told_nothing() -> None:
    app = http_server.BearerAuth(downstream, token=TOKEN)
    wrong = await call_asgi(app, headers=[(b"authorization", b"Bearer " + b"z" * 32)])
    missing = await call_asgi(app)

    # Byte for byte the same answer: length, prefix and validity all unsaid.
    assert wrong == missing


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        b"Basic " + TOKEN.encode(),
        TOKEN.encode(),
        b"Bearer",
        b"Bearer  ",
        b"bearer\t" + TOKEN.encode(),
    ],
)
async def test_a_malformed_authorization_header_is_refused(header: bytes) -> None:
    app = http_server.BearerAuth(downstream, token=TOKEN)
    status, _, _ = await call_asgi(app, headers=[(b"authorization", header)])
    assert status == 401


@pytest.mark.asyncio
async def test_the_right_token_reaches_the_server() -> None:
    app = http_server.BearerAuth(downstream, token=TOKEN)
    status, _, body = await call_asgi(app, headers=[(b"authorization", f"Bearer {TOKEN}".encode())])

    assert status == 200
    assert body == b"through"


@pytest.mark.asyncio
async def test_the_scheme_is_matched_case_insensitively() -> None:
    """RFC 9110 says the scheme is case-insensitive; the token is not."""
    app = http_server.BearerAuth(downstream, token=TOKEN)
    status, _, _ = await call_asgi(app, headers=[(b"authorization", f"bearer {TOKEN}".encode())])
    assert status == 200


@pytest.mark.asyncio
async def test_authenticated_request_body_is_capped_before_downstream() -> None:
    reached = False

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    app = http_server.BearerAuth(
        http_server.RequestGuard(inner, max_body_bytes=8, requests_per_minute=10),
        token=TOKEN,
    )
    status, _, body = await call_asgi(
        app,
        headers=[(b"authorization", f"Bearer {TOKEN}".encode())],
        body_bytes=b"123456789",
    )

    assert status == 413
    assert body == b"Request body too large"
    assert reached is False


@pytest.mark.asyncio
async def test_authenticated_http_arrivals_have_a_process_wide_rate_limit() -> None:
    app = http_server.BearerAuth(
        http_server.RequestGuard(downstream, max_body_bytes=1024, requests_per_minute=2),
        token=TOKEN,
    )
    auth = [(b"authorization", f"Bearer {TOKEN}".encode())]

    assert (await call_asgi(app, headers=auth))[0] == 200
    assert (await call_asgi(app, headers=auth))[0] == 200
    status, headers, body = await call_asgi(app, headers=auth)

    assert status == 429
    assert int(headers[b"retry-after"]) >= 1
    assert body == b"Too Many Requests"


@pytest.mark.asyncio
async def test_tokens_are_compared_in_constant_time(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    real = http_server.hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(http_server.hmac, "compare_digest", spy)
    app = http_server.BearerAuth(downstream, token=TOKEN)
    await call_asgi(app, headers=[(b"authorization", f"Bearer {TOKEN}".encode())])

    assert calls, "the token was compared with something other than compare_digest"


@pytest.mark.asyncio
async def test_lifespan_is_passed_through_untouched() -> None:
    """Lifespan is the server starting the app, not anybody's request."""
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        del receive, send
        seen.append(scope["type"])

    app = http_server.BearerAuth(inner, token=TOKEN)
    await app({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


@pytest.mark.asyncio
async def test_a_websocket_is_closed_rather_than_passed_inward() -> None:
    """An anonymous client can open one; it must not reach the SDK."""
    reached: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        del receive, send
        reached.append(scope["type"])

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    app = http_server.BearerAuth(inner, token=TOKEN)
    await app({"type": "websocket", "path": "/mcp", "headers": []}, None, send)

    assert reached == []
    assert sent == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.asyncio
async def test_an_unknown_scope_type_is_dropped_rather_than_forwarded() -> None:
    """An allow-list: a scope nobody thought about must not arrive authenticated."""
    reached: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        del receive, send
        reached.append(scope["type"])

    app = http_server.BearerAuth(inner, token=TOKEN)
    await app({"type": "something.new"}, None, None)
    assert reached == []


def test_http_sessions_are_given_an_idle_timeout(monkeypatch) -> None:
    """The SDK's default is "never", which leaks a task per abandoned client."""
    monkeypatch.setenv("TGAI_HTTP_TOKEN", TOKEN)
    captured: dict[str, Any] = {}

    import mcp.server.streamable_http_manager as manager_module

    real = manager_module.StreamableHTTPSessionManager.__init__

    def spy(self: Any, *args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        real(self, *args, **kwargs)

    monkeypatch.setattr(manager_module.StreamableHTTPSessionManager, "__init__", spy)
    http_server.build_http_app(config=HTTPConfig())

    assert captured.get("session_idle_timeout") == HTTPConfig().session_idle_timeout_seconds


# --- assembling the app -----------------------------------------------------


def test_building_the_app_refuses_a_routable_host(monkeypatch) -> None:
    monkeypatch.setenv("TGAI_HTTP_TOKEN", TOKEN)
    with pytest.raises(TelegramAIError):
        http_server.build_http_app(config=HTTPConfig(host="0.0.0.0"))  # noqa: S104


def test_building_the_app_refuses_a_missing_token(monkeypatch) -> None:
    monkeypatch.delenv("TGAI_HTTP_TOKEN", raising=False)
    with pytest.raises(TelegramAIError):
        http_server.build_http_app(config=HTTPConfig())


def test_the_app_is_built_with_auth_in_front(monkeypatch) -> None:
    monkeypatch.setenv("TGAI_HTTP_TOKEN", TOKEN)
    app = http_server.build_http_app(config=HTTPConfig())
    assert isinstance(app, http_server.BearerAuth)


@pytest.mark.asyncio
async def test_the_assembled_app_refuses_an_anonymous_request(monkeypatch) -> None:
    """End to end: no token, no MCP session, whatever the path."""
    monkeypatch.setenv("TGAI_HTTP_TOKEN", TOKEN)
    app = http_server.build_http_app(config=HTTPConfig())

    for path in ("/mcp", "/", "/anything"):
        status, _, _ = await call_asgi(app, path=path)
        assert status == 401


@pytest.mark.asyncio
async def test_the_documented_url_is_served_rather_than_redirected(monkeypatch) -> None:
    """A `Mount` answers the un-slashed form with a 307, which POSTs badly."""
    monkeypatch.setenv("TGAI_HTTP_TOKEN", TOKEN)
    app = http_server.build_http_app(config=HTTPConfig())
    authorized = [(b"authorization", f"Bearer {TOKEN}".encode())]

    async with asgi_lifespan(app):
        status, _, _ = await call_asgi(app, path="/mcp", headers=authorized)
        assert status not in (301, 302, 307, 308), "the endpoint redirected instead of answering"
        assert status != 404

        missing, _, _ = await call_asgi(app, path="/not-the-endpoint", headers=authorized)
        assert missing == 404
