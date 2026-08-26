"""Search with context: the messages around a hit, marked as not being hits.

A match on its own rarely says what the conversation was about, and the only
way to find out used to be a second, paged read of the whole chat. So the
search may bring back the neighbours — but they are a different kind of row
than a match, and the output has to keep saying which is which.

Three properties are worth a test each, because each one is a way the feature
goes wrong quietly:

**Off by default.** Context costs two extra Telegram calls per hit. A default
that fetched them would make every existing search slower for callers who never
asked.

**Overlaps collapse.** Two hits three messages apart share neighbours. Emitting
them twice would make an agent believe the same sentence was said twice.

**Context is still a stranger's text.** It goes through the same trust boundary
as a match; a neighbour that arrived unwrapped would be the one span in the
payload a model could mistake for an instruction.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from telegram_ai_cli_mcp import db
from telegram_ai_cli_mcp.audit import AuditLog
from telegram_ai_cli_mcp.config import Settings
from telegram_ai_cli_mcp.context import OperationContext
from telegram_ai_cli_mcp.errors import InvalidInput
from telegram_ai_cli_mcp.limits import LimitStore
from telegram_ai_cli_mcp.ops.search import (
    MAX_CONTEXT,
    MAX_CONTEXT_HITS,
    SearchInput,
    handle_search,
)
from telegram_ai_cli_mcp.plans import PlanStore
from telegram_ai_cli_mcp.safety import SafetyKernel
from telegram_ai_cli_mcp.secretbox import SecretBox
from telegram_ai_cli_mcp.untrusted import CLOSE_MARKER, OPEN_MARKER

# --- a Telegram that is entirely local --------------------------------------


class FakeTotalList(list):
    """Telethon hands back a list that also knows how many matched in all.

    The count is what tells a complete last page from a cut one, so a fake that
    returned a plain list would leave the truncation rule untested.
    """

    def __init__(self, items: list[Any], total: int) -> None:
        super().__init__(items)
        self.total = total


@dataclass(slots=True)
class FakeMessage:
    """Only the attributes ``_serialize.message_summary`` actually reads."""

    id: int
    message: str
    date: datetime = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    out: bool = False
    sender_id: int = 4242


class FakeClient:
    """History in a list, with Telethon's paging semantics reproduced.

    ``offset_id`` is exclusive in both directions and ``reverse`` flips which
    direction it means — that is the whole reason the context fetch can ask for
    the *nearest* neighbours rather than the newest ones in the chat, so a fake
    that ignored it would certify the wrong call.
    """

    def __init__(self, entity: Any, messages: list[FakeMessage]) -> None:
        self.entity = entity
        self.messages = sorted(messages, key=lambda m: m.id)
        self.calls: list[dict[str, Any]] = []

    def is_connected(self) -> bool:
        return True

    async def is_user_authorized(self) -> bool:
        return True

    async def get_entity(self, reference: Any) -> Any:
        return self.entity

    async def get_messages(
        self,
        entity: Any,
        *,
        limit: int | None = None,
        search: str | None = None,
        max_id: int = 0,
        min_id: int = 0,
        offset_id: int = 0,
        reverse: bool = False,
    ) -> FakeTotalList:
        self.calls.append(
            {
                "limit": limit,
                "search": search,
                "max_id": max_id,
                "min_id": min_id,
                "offset_id": offset_id,
                "reverse": reverse,
            }
        )
        pool = list(self.messages)
        if search:
            pool = [m for m in pool if search.lower() in m.message.lower()]
        if max_id:
            pool = [m for m in pool if m.id < max_id]
        if min_id:
            pool = [m for m in pool if m.id > min_id]
        if reverse:
            if offset_id:
                pool = [m for m in pool if m.id > offset_id]
        else:
            if offset_id:
                pool = [m for m in pool if m.id < offset_id]
            pool.reverse()
        return FakeTotalList(pool[: limit or len(pool)], total=len(pool))


class FakeRegistry:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    def list_accounts(self) -> list[str]:
        return ["main"]

    def get(self, label: str) -> None:
        return None

    async def load_account(self, label: str) -> Any:
        return self

    @property
    def client(self) -> FakeClient:
        return self._client


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("TGAI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "state.sqlite3")
    yield connection
    connection.close()


def group_entity() -> Any:
    """A real Telethon type, because ``marked_id`` calls ``utils.get_peer_id``."""
    from telethon.tl.types import Chat

    return Chat(
        id=555,
        title="Release room",
        photo=None,
        participants_count=3,
        date=datetime(2026, 8, 1, tzinfo=UTC),
        version=1,
    )


def build_context(conn: sqlite3.Connection, tmp_path: Path, client: FakeClient) -> OperationContext:
    settings = Settings()
    return OperationContext(
        settings=settings,
        safety=SafetyKernel(settings),
        # A read never touches the plan store; it is built only because the
        # context object holds one, and encrypted bodies fail closed without
        # a key.
        plans=PlanStore(conn, settings.plans, SecretBox(secrets.token_bytes(32))),
        limits=LimitStore(conn, settings.limits),
        audit=AuditLog(tmp_path / "audit.jsonl", settings.audit),
        actor="cli",
        accounts=FakeRegistry(client),
    )


def chat_history() -> list[FakeMessage]:
    """Ten messages; ids 1–10. The word "deploy" is in 4 and 6 only."""
    bodies = {
        4: "the deploy went out at noon",
        6: "deploy again after the fix",
    }
    return [FakeMessage(id=i, message=bodies.get(i, f"message {i}")) for i in range(1, 11)]


async def run_search(
    conn: sqlite3.Connection, tmp_path: Path, client: FakeClient, **kwargs: Any
) -> Any:
    ctx = build_context(conn, tmp_path, client)
    return await handle_search(ctx, SearchInput(**kwargs))


def ids_of(envelope: Any) -> list[int]:
    return [row["id"] for row in envelope.data["messages"]]


def matches_of(envelope: Any) -> list[int]:
    return [row["id"] for row in envelope.data["messages"] if row["match"]]


# --- the tests --------------------------------------------------------------


async def test_context_is_off_by_default(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """No argument, no extra calls, and every row is a match."""
    client = FakeClient(group_entity(), chat_history())

    envelope = await run_search(conn, tmp_path, client, query="deploy", chat="555")

    assert ids_of(envelope) == [6, 4]
    assert all(row["match"] for row in envelope.data["messages"])
    assert len(client.calls) == 1


async def test_context_returns_neighbours_marked_as_context(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """One message either side, in one list, newest first, hits flagged."""
    client = FakeClient(group_entity(), chat_history())

    envelope = await run_search(conn, tmp_path, client, query="deploy", chat="555", context=1)

    assert ids_of(envelope) == [7, 6, 5, 4, 3]
    assert matches_of(envelope) == [6, 4]
    assert envelope.meta.extra["matches"] == 2
    assert envelope.meta.extra["context_messages"] == 3


async def test_overlapping_context_is_collapsed(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Message 5 sits between both hits and appears exactly once."""
    client = FakeClient(group_entity(), chat_history())

    envelope = await run_search(conn, tmp_path, client, query="deploy", chat="555", context=2)

    ids = ids_of(envelope)
    assert ids == sorted(ids, reverse=True)
    assert len(ids) == len(set(ids))
    assert ids == [8, 7, 6, 5, 4, 3, 2]


async def test_context_never_reports_a_neighbour_as_a_match(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A neighbour that happens to be fetched twice is still not a hit."""
    client = FakeClient(group_entity(), chat_history())

    envelope = await run_search(conn, tmp_path, client, query="deploy", chat="555", context=2)

    by_id = {row["id"]: row for row in envelope.data["messages"]}
    assert by_id[5]["match"] is False
    assert by_id[4]["match"] is True
    assert envelope.meta.returned == len(by_id)


async def test_context_messages_cross_the_trust_boundary(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A neighbour is somebody else's sentence and is wrapped like one."""
    client = FakeClient(group_entity(), chat_history())

    envelope = await run_search(conn, tmp_path, client, query="deploy", chat="555", context=1)

    context_rows = [row for row in envelope.data["messages"] if not row["match"]]
    assert context_rows
    for row in context_rows:
        assert row["text"].startswith(OPEN_MARKER)
        assert row["text"].endswith(CLOSE_MARKER)


async def test_context_asks_for_the_nearest_neighbours(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Both directions are anchored on the hit, and the query is not reapplied.

    Asking with ``min_id`` alone would return the newest messages in the chat
    rather than the ones next to the match; carrying ``search`` over would
    return more hits instead of context.
    """
    client = FakeClient(group_entity(), chat_history())

    await run_search(conn, tmp_path, client, query="deploy", chat="555", context=1)

    context_calls = client.calls[1:]
    assert context_calls
    assert all(call["search"] is None for call in context_calls)
    assert all(call["offset_id"] in {4, 6} for call in context_calls)
    assert {call["reverse"] for call in context_calls} == {True, False}


async def test_context_has_a_ceiling(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """The radius is capped in the schema, where a caller can read it."""
    with pytest.raises(ValidationError):
        SearchInput(query="deploy", chat="555", context=MAX_CONTEXT + 1)

    assert SearchInput(query="deploy", chat="555", context=MAX_CONTEXT).context == MAX_CONTEXT


async def test_context_stops_after_a_bounded_number_of_hits(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Fifty hits must not become a download of the whole chat."""
    total = MAX_CONTEXT_HITS * 3
    history = [FakeMessage(id=i, message="deploy") for i in range(1, total + 1)]
    client = FakeClient(group_entity(), history)

    envelope = await run_search(
        conn, tmp_path, client, query="deploy", chat="555", context=1, limit=total
    )

    assert len(client.calls) == 1 + 2 * MAX_CONTEXT_HITS
    assert any(str(MAX_CONTEXT_HITS) in warning for warning in envelope.warnings)


async def test_context_rows_do_not_make_a_page_look_truncated(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Two hits, five rows, and nothing was cut."""
    client = FakeClient(group_entity(), chat_history())

    envelope = await run_search(
        conn, tmp_path, client, query="deploy", chat="555", context=1, limit=2
    )

    assert len(envelope.data["messages"]) == 5
    assert envelope.meta.extra["matches"] == 2
    assert envelope.meta.truncated is False


async def test_truncation_follows_the_match_count_telegram_reported(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A page as long as the limit is not evidence there is more."""
    history = [FakeMessage(id=i, message="deploy") for i in range(1, 4)]
    client = FakeClient(group_entity(), history)

    cut = await run_search(conn, tmp_path, client, query="deploy", chat="555", limit=2)
    assert cut.meta.truncated is True

    whole = await run_search(conn, tmp_path, client, query="deploy", chat="555", limit=3)
    assert whole.meta.truncated is False


async def test_global_search_refuses_context(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Each hit would live in a chat of its own; that is a different read."""
    client = FakeClient(group_entity(), chat_history())

    with pytest.raises(InvalidInput):
        await run_search(conn, tmp_path, client, query="deploy", context=1)
