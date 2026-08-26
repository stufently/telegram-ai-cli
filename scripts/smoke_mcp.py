#!/usr/bin/env python3
"""Smoke-test the MCP server end to end over stdio.

Importing telegram_ai_cli_mcp.mcp_server does NOT prove the server works: the
module can import cleanly and the process can still hang on startup, never
answer a request, or crash right after accepting the first line. A CI step
that only imports the module (or launches it in the background and moves on)
stays green even when the transport is broken -- the process never exits on
its own, so "it didn't crash yet" is not a pass.

This script speaks the real protocol: it launches `tg-ai mcp`, performs the
mandatory MCP handshake (initialize -> notifications/initialized), asks
tools/list, and requires a well-formed answer within a timeout. Any deviation
-- the binary missing, the handshake stalling, a malformed response -- is a
hard failure with a diagnostic message and a non-zero exit code. Do not soften
this into a pass; a red `smoke` job here is an honest signal that the MCP
server isn't wired up yet, not a bug in the check.

Env overrides (seconds): SMOKE_STARTUP_TIMEOUT, SMOKE_REQUEST_TIMEOUT.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
from typing import Any, NoReturn

STARTUP_TIMEOUT = float(os.environ.get("SMOKE_STARTUP_TIMEOUT", "15"))
REQUEST_TIMEOUT = float(os.environ.get("SMOKE_REQUEST_TIMEOUT", "15"))

MCP_PROTOCOL_VERSION = "2025-06-18"

#: How much of one JSON-RPC line this script is willing to buffer. See the
#: comment beside `create_subprocess_exec`: the default 64 KiB is smaller than
#: a `tools/list` answer once the registry passes about fifty tools.
STDOUT_LIMIT = 8 * 1024 * 1024


def _fail(message: str) -> NoReturn:
    print(f"SMOKE FAIL: {message}", file=sys.stderr)
    sys.exit(1)


async def _write_line(stream: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    stream.write((json.dumps(payload) + "\n").encode("utf-8"))
    await stream.drain()


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        proc.kill()
        await proc.wait()


async def _fail_with_stderr(proc: asyncio.subprocess.Process, message: str) -> NoReturn:
    """Grab whatever the child already wrote to stderr before killing it --
    the raw traceback from the child is the whole point of a smoke test."""
    stderr_tail = b""
    if proc.stderr is not None:
        # Best-effort: we are already failing, and the diagnostic is a bonus.
        with contextlib.suppress(Exception):
            stderr_tail = await asyncio.wait_for(proc.stderr.read(8192), timeout=2)
    await _kill(proc)
    if stderr_tail:
        print("--- child stderr ---", file=sys.stderr)
        print(stderr_tail.decode("utf-8", errors="replace"), file=sys.stderr)
    _fail(message)


async def _read_response(
    proc: asyncio.subprocess.Process, timeout: float, expected_id: int, what: str
) -> dict[str, Any]:
    # A real check, not an assert: asserts vanish under `python -O`, and a
    # smoke test that silently skips its own precondition is the failure mode
    # this script exists to prevent.
    if proc.stdout is None:
        _fail("child process was started without a stdout pipe")
    try:
        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
    except TimeoutError:
        await _fail_with_stderr(proc, f"timed out after {timeout:.0f}s waiting for {what}")
    if not raw:
        await _fail_with_stderr(proc, f"server closed stdout before sending {what}")
    line = raw.decode("utf-8", errors="replace").strip()
    if not line:
        await _fail_with_stderr(proc, f"got an empty line while waiting for {what}")
    try:
        message: dict[str, Any] = json.loads(line)
    except json.JSONDecodeError as exc:
        await _fail_with_stderr(proc, f"{what} was not valid JSON ({exc}); raw line: {line!r}")
    if message.get("id") != expected_id or "result" not in message:
        await _fail_with_stderr(proc, f"{what} was not a well-formed result: {message!r}")
    return message


async def main() -> None:
    binary = shutil.which("tg-ai")
    if binary is None:
        _fail(
            "`tg-ai` is not on PATH. Either the package is not installed in "
            "this environment, or the `tg-ai` console script does not exist "
            "yet (telegram_ai_cli_mcp.cli:main)."
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "mcp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # One JSON-RPC message per line, and `tools/list` is one line
            # carrying the description and the full input schema of every
            # published tool. asyncio's default StreamReader limit is 64 KiB,
            # past which `readline` raises "Separator is found, but chunk is
            # longer than limit" — which reads like a protocol fault and is in
            # fact only this script's buffer. The suite crossed 64 KiB at around
            # fifty tools; the ceiling here is set well clear of the next fifty.
            limit=STDOUT_LIMIT,
        )
    except OSError as exc:
        _fail(f"could not launch `{binary} mcp`: {exc}")

    if proc.stdin is None:
        await _fail_with_stderr(proc, "child process was started without a stdin pipe")

    try:
        await _write_line(
            proc.stdin,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "telegram-ai-cli-mcp-smoke-test", "version": "0"},
                },
            },
        )
        await _read_response(proc, STARTUP_TIMEOUT, expected_id=1, what="the initialize response")

        # Required by the MCP spec before any further request is valid.
        await _write_line(
            proc.stdin,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )

        await _write_line(
            proc.stdin,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        list_response = await _read_response(
            proc, REQUEST_TIMEOUT, expected_id=2, what="the tools/list response"
        )

        tools = list_response["result"].get("tools")
        if not isinstance(tools, list):
            await _fail_with_stderr(
                proc, f"tools/list result had no 'tools' array: {list_response!r}"
            )

        names = sorted(t.get("name", "?") for t in tools if isinstance(t, dict))
        print(f"SMOKE OK: tools/list returned {len(tools)} tool(s): {', '.join(names) or '(none)'}")
    finally:
        await _kill(proc)


if __name__ == "__main__":
    asyncio.run(main())
