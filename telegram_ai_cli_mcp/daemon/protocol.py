"""The wire between a local client and its account daemon.

Four bytes of big-endian length, then that many bytes of UTF-8 JSON. Not
newline-delimited JSON, for two reasons: ``StreamReader.readline`` buffers until
it sees the delimiter, so a peer that never sends one grows the buffer until the
64 KiB default limit turns a malformed frame into an exception halfway through
parsing — and a chat read is routinely larger than that limit anyway. A declared
length is refused *before* a byte of the body is read.

The vocabulary is deliberately tiny. There are two verbs, ``ping`` and ``run``,
and ``run`` carries an **operation name from the registry** — never a method to
call, never an attribute to reach. That is the property the whole design rests
on: a daemon that could be asked for ``client.<anything>`` would hand a caller
the account with none of the policy in front of it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Final

from ..errors import InvalidInput

PROTOCOL_VERSION: Final = 1

#: Refused before the body is read. Generous, because a permitted `chat read`
#: of 500 messages is a legitimately large answer; bounded, because the length
#: arrives from the peer and an unbounded one is an allocation it chooses.
MAX_FRAME_BYTES: Final = 16 * 1024 * 1024

_LENGTH_BYTES: Final = 4


class FrameTooLarge(InvalidInput):
    """The declared length is over the ceiling; nothing was read."""


async def write_frame(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise FrameTooLarge(
            f"response is {len(body)} bytes, over the {MAX_FRAME_BYTES}-byte frame ceiling",
            suggestion="Ask for fewer rows: every read operation takes a limit.",
        )
    writer.write(len(body).to_bytes(_LENGTH_BYTES, "big") + body)
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read one frame, or ``None`` when the peer closed cleanly."""
    try:
        header = await reader.readexactly(_LENGTH_BYTES)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None  # the peer closed between frames, which is not an error
        raise InvalidInput("truncated frame header") from None
    size = int.from_bytes(header, "big")
    if size == 0:
        raise InvalidInput("empty frame")
    if size > MAX_FRAME_BYTES:
        raise FrameTooLarge(f"frame declares {size} bytes, over the {MAX_FRAME_BYTES}-byte ceiling")
    try:
        body = await reader.readexactly(size)
    except asyncio.IncompleteReadError:
        raise InvalidInput("truncated frame body") from None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidInput(f"frame is not valid JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise InvalidInput("a frame must be a JSON object")
    return payload


def error_response(exc: Any) -> dict[str, Any]:
    """A refusal in the shape the client turns straight into an envelope."""
    return {"v": PROTOCOL_VERSION, "ok": False, "error": exc.to_dict()}


def ok_response(body: dict[str, Any]) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "ok": True, **body}
