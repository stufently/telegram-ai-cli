"""The tool-visibility gate: what this server is allowed to publish at all.

The gate exists for one property that no per-peer rule can provide: a prompt
injection cannot invoke a tool it never saw in the tool list. It is a second,
coarser layer *in front of* the permission matrix, never a replacement for it —
so the tests below assert both halves of that:

* it narrows and only narrows (a name outside what the registry publishes is a
  configuration error, not a way to conjure a tool);
* it filters the call path as well as the list, because a filter that only
  hides is cosmetic — the tool name is a string, and a caller that guesses one
  would otherwise reach a tool the operator removed from the surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import mcp.types as types
import pytest
import yaml

import telegram_ai_cli.ops  # noqa: F401  (registers every operation)
from telegram_ai_cli.errors import ErrorCode, InvalidInput
from telegram_ai_cli.mcp_server import build_server, published_tool_names
from telegram_ai_cli.opspec import REGISTRY


def config(tmp_path: Path, **body: Any) -> Path:
    path = tmp_path / "tgai.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


async def tool_names(server: Any) -> list[str]:
    entry = server.get_request_handler("tools/list")
    result = await entry.handler(None, None)
    return [tool.name for tool in result.tools]


async def call(server: Any, name: str, **arguments: Any) -> dict[str, Any]:
    entry = server.get_request_handler("tools/call")
    params = types.CallToolRequestParams(name=name, arguments=arguments)
    result = await entry.handler(None, params)
    return json.loads(result.content[0].text)


# -- the default: off -------------------------------------------------------


async def test_with_no_gate_configured_every_tool_is_published(tmp_path: Path) -> None:
    """Unset means unchanged. The gate is opt-in, deliberately."""
    names = await tool_names(build_server(config_path=config(tmp_path)))

    assert sorted(names) == sorted(REGISTRY.mcp_tool_names())


def test_an_unset_gate_is_not_the_same_object_as_an_empty_one() -> None:
    assert published_tool_names(None) is None
    assert published_tool_names([]) == frozenset()


# -- narrowing ---------------------------------------------------------------


async def test_the_gate_publishes_only_the_tools_it_names(tmp_path: Path) -> None:
    server = build_server(
        config_path=config(tmp_path, mcp={"tools": ["telegram_chats", "telegram_whois"]})
    )

    assert sorted(await tool_names(server)) == ["telegram_chats", "telegram_whois"]


async def test_an_empty_gate_publishes_nothing(tmp_path: Path) -> None:
    """Empty means nothing here, as it does for every other allow list."""
    server = build_server(config_path=config(tmp_path, mcp={"tools": []}))

    assert await tool_names(server) == []


def test_the_gate_can_only_ever_narrow() -> None:
    """Whatever it names, the result is a subset of what the registry publishes."""
    every = REGISTRY.mcp_tool_names()

    assert published_tool_names(list(every)) == frozenset(every)
    assert published_tool_names(["telegram_chats"]) <= frozenset(every)


# -- a typo is loud ----------------------------------------------------------


def test_an_unknown_tool_name_is_refused_at_startup() -> None:
    with pytest.raises(InvalidInput) as caught:
        published_tool_names(["telegram_chats", "telegram_chatz", "telegram_nope"])

    # Every unknown entry is named, not just the first: an operator fixing a
    # list one error at a time restarts once per typo.
    assert "telegram_chatz" in caught.value.message
    assert "telegram_nope" in caught.value.message
    assert "telegram_chats" not in caught.value.message


def test_a_bad_gate_stops_the_server_from_being_built(tmp_path: Path) -> None:
    """Loud at startup rather than a warning: a warning leaves the typo in place."""
    with pytest.raises(InvalidInput):
        build_server(config_path=config(tmp_path, mcp={"tools": ["telegram_not_a_tool"]}))


def test_naming_a_write_operation_is_a_configuration_error() -> None:
    """A remote write has no direct tool, and the gate cannot invent one.

    `message.send` is planned over MCP and applied from a terminal, so
    `telegram_message_send` is not a published name — asking for it is a typo,
    and answering it would be the gate widening rather than narrowing.
    """
    with pytest.raises(InvalidInput):
        published_tool_names(["telegram_message_send"])

    assert "telegram_plan_send_message" in REGISTRY.mcp_tool_names()


# -- the call path -----------------------------------------------------------


async def test_a_hidden_tool_is_refused_when_it_is_called_by_name(tmp_path: Path) -> None:
    """A filter that only hides is cosmetic: the name is guessable."""
    server = build_server(config_path=config(tmp_path, mcp={"tools": ["telegram_chats"]}))

    payload = await call(server, "telegram_chat_read", chat="-4242")

    assert payload["ok"] is False
    assert payload["error"]["code"] == ErrorCode.FORBIDDEN_BY_ALLOWLIST
    assert payload["error"]["retryable"] is False


async def test_a_hidden_plan_tool_is_refused_too(tmp_path: Path) -> None:
    server = build_server(config_path=config(tmp_path, mcp={"tools": ["telegram_chats"]}))

    payload = await call(server, "telegram_plan_send_message", chat="-4242", text="hi")

    assert payload["ok"] is False
    assert payload["error"]["code"] == ErrorCode.FORBIDDEN_BY_ALLOWLIST


async def test_the_gate_is_reached_only_after_the_registry_matched_the_name(
    tmp_path: Path,
) -> None:
    """Which is what makes the name quoted in the gate's refusal safe to quote.

    A failure payload is redacted and defanged, but a refusal still must not
    repeat caller-supplied text as project-authored prose. An unknown name never
    reaches the gate — it is
    `UNKNOWN_OPERATION` from the registry first, so by the time the gate speaks,
    the string it names is one of this project's own constants.

    (That earlier message *does* echo the name it was given, safely inside the
    failure boundary; the point asserted here is the gate's ordering.)
    """
    server = build_server(config_path=config(tmp_path, mcp={"tools": []}))

    payload = await call(server, "telegram_not_a_real_tool")

    assert payload["error"]["code"] == ErrorCode.UNKNOWN_OPERATION
