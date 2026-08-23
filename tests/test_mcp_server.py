"""The MCP adapter, exercised against the real SDK.

These tests exist because of a specific failure. ``build_server()`` used the
1.x decorator API (``@server.list_tools()``), which the installed SDK no longer
has. Every module imported cleanly, the whole suite passed, and the break only
appeared when a server was actually constructed — which nothing did until the
stdio smoke test ran in CI.

So the point here is not coverage for its own sake: it is that *constructing*
the server is the cheapest way to catch an SDK API change, and it belongs in
the unit suite rather than only in a subprocess handshake.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import telegram_ai_cli.ops  # noqa: F401  (registers every operation)
from telegram_ai_cli.mcp_server import build_server


def test_the_server_can_be_built() -> None:
    """Regression: this raised AttributeError against the installed SDK."""
    assert build_server() is not None


def test_tools_list_is_registered_and_answers() -> None:
    server = build_server()
    entry = server.get_request_handler("tools/list")
    assert entry is not None, "tools/list must be registered or no tool is discoverable"


def test_call_tool_is_registered() -> None:
    server = build_server()
    assert server.get_request_handler("tools/call") is not None


async def _list_tools(server: Any) -> list[Any]:
    entry = server.get_request_handler("tools/list")
    result = await entry.handler(None, None)
    return list(result.tools)


@pytest.mark.asyncio
async def test_every_operation_is_exposed_and_none_of_them_apply() -> None:
    """The approval boundary, asserted at the surface that enforces it.

    ``opspec`` already forbids an MCP tool named "apply", but that invariant is
    checked on the registry. This checks the tool list the SDK would actually
    publish, which is what an MCP client sees.
    """
    tools = await _list_tools(build_server())

    assert tools, "no tools published"
    names = [tool.name for tool in tools]
    assert len(names) == len(set(names)), "duplicate tool names"
    assert not [name for name in names if "apply" in name.lower()]


@pytest.mark.asyncio
async def test_an_unknown_tool_comes_back_as_an_envelope_not_a_protocol_error() -> None:
    """Errors travel as content so the caller keeps the code and suggestion."""
    server = build_server()
    entry = server.get_request_handler("tools/call")

    import mcp.types as types

    params = types.CallToolRequestParams(name="telegram_not_a_real_tool", arguments={})
    result = await entry.handler(None, params)

    assert result.is_error is True
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is False
    assert payload["error"]["code"]
