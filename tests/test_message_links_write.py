"""Handing a message over the way a person actually has it: as a link.

Every Telegram client offers "copy link" and nothing else. Until now the four
marks understood one and `message.reply` / `edit` / `delete` did not — they
passed the whole string to Telethon, which resolved the chat and threw the
number away, so the operation acted on whatever the id argument defaulted to.

Two properties are worth a test rather than a reading of the code.

**The id is decided once and re-decided at apply time.** A link is not stored as
a number in the plan; verification re-derives it from the same argument, for the
same reason the peer is re-resolved rather than read back.

**Two ways of naming are not additive.** A link and an explicit id that disagree
are a caller error, and for `delete` a link alongside a list of ids is the same
error: picking either one silently acts on something nobody named.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from telethon.tl import types as tl

from telegram_ai_cli import db
from telegram_ai_cli.apply import _verify
from telegram_ai_cli.audit import AuditLog
from telegram_ai_cli.config import AuditConfig, PlansConfig, Settings
from telegram_ai_cli.context import OperationContext
from telegram_ai_cli.errors import InvalidInput
from telegram_ai_cli.ops.write import (
    DeleteMessageInput,
    EditMessageInput,
    ReplyMessageInput,
    plan_delete_message,
    plan_edit_message,
    plan_reply_message,
)
from telegram_ai_cli.opspec import REGISTRY
from telegram_ai_cli.plans import PlanStore
from telegram_ai_cli.safety import SafetyKernel

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

GROUP_ID = 1234567890
MARKED_GROUP_ID = -1001234567890
LINK = "https://t.me/marketing/412"


@dataclass
class FakeMessage:
    id: int = 412
    message: str | None = "quarterly numbers are in"
    date: datetime = NOW
    out: bool = True
    pinned: bool = False
    media: Any = None
    reactions: Any = None


class FakeClient:
    def __init__(self, entity: Any, messages: dict[int, Any]) -> None:
        self._entity = entity
        self._messages = messages
        self.touched: list[str] = []

    def is_connected(self) -> bool:
        return True

    async def is_user_authorized(self) -> bool:
        return True

    async def get_entity(self, target: Any) -> Any:
        self.touched.append(f"get_entity:{target}")
        return self._entity

    async def get_messages(self, _peer: Any, *, ids: list[int]) -> list[Any]:
        self.touched.append(f"get_messages:{ids}")
        return [self._messages.get(one) for one in ids]


@dataclass
class FakeOpened:
    client: FakeClient
    spec: Any = None


class FakeRegistry:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    def list_accounts(self) -> list[str]:
        return ["work"]

    def load_account(self, _label: str) -> FakeOpened:
        return FakeOpened(client=self._client)

    def get(self, _label: str) -> None:
        return None


def group() -> tl.Channel:
    return tl.Channel(
        id=GROUP_ID, title="Marketing", photo=None, date=None, megagroup=True, username="marketing"
    )


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "state.sqlite3")
    yield connection
    connection.close()


def build_ctx(conn: sqlite3.Connection, tmp_path: Path, client: FakeClient) -> OperationContext:
    targets = [MARKED_GROUP_ID]
    settings = Settings(
        profile="plan",
        plans={"encrypt_bodies": False},
        safety={
            "read": {"chats": {"allow": targets}, "dms": {"allow": targets}},
            "write": {"send": {"allow": targets}, "admin": {"allow": targets}},
        },
    )
    return OperationContext(
        settings=settings,
        safety=SafetyKernel(settings),
        plans=PlanStore(conn, PlansConfig(encrypt_bodies=False)),
        limits=None,  # type: ignore[arg-type]
        audit=AuditLog(tmp_path / "audit.jsonl", AuditConfig(enabled=False)),
        actor="cli",
        accounts=FakeRegistry(client),  # type: ignore[arg-type]
    )


def client_with_message() -> FakeClient:
    return FakeClient(group(), {412: FakeMessage()})


async def _plan_and_params(ctx: OperationContext, params: Any, planner: Any) -> tuple[Any, Any]:
    plan = await planner(ctx, params)
    op = REGISTRY.by_name(plan.operation)
    return ctx.plans.get(plan.plan_id), op.parse(plan.params)


# --- a link is enough ------------------------------------------------------


async def test_a_link_names_the_message_a_reply_answers(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    client = client_with_message()
    ctx = build_ctx(conn, tmp_path, client)

    plan = await plan_reply_message(ctx, ReplyMessageInput(chat=LINK, text="on it"))

    assert "to message 412" in plan.summary
    assert plan.preconditions["reply_to"]["id"] == 412


async def test_a_link_names_the_message_an_edit_rewrites(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    client = client_with_message()
    ctx = build_ctx(conn, tmp_path, client)

    plan = await plan_edit_message(ctx, EditMessageInput(chat=LINK, text="corrected"))

    assert "Edit message 412" in plan.summary
    assert plan.preconditions["message"]["id"] == 412


async def test_a_link_names_the_one_message_a_delete_removes(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    client = client_with_message()
    ctx = build_ctx(conn, tmp_path, client)

    plan = await plan_delete_message(ctx, DeleteMessageInput(chat=LINK))

    assert "Delete 1 message(s)" in plan.summary
    assert [snapshot["id"] for snapshot in plan.preconditions["messages"]] == [412]


# --- the id is re-derived, never read back ---------------------------------


async def test_the_applier_derives_the_id_from_the_link_again(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The plan stores the link, not a number. Verification decides again."""
    client = client_with_message()
    ctx = build_ctx(conn, tmp_path, client)
    plan, params = await _plan_and_params(
        ctx, ReplyMessageInput(chat=LINK, text="on it"), plan_reply_message
    )

    assert params.reply_to_message_id is None
    prepared = await _verify(ctx, client, plan, params)
    assert prepared.message_ids == [412]


async def test_a_delete_applies_to_the_id_the_link_named(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    client = client_with_message()
    ctx = build_ctx(conn, tmp_path, client)
    plan, params = await _plan_and_params(ctx, DeleteMessageInput(chat=LINK), plan_delete_message)

    assert params.message_ids is None
    prepared = await _verify(ctx, client, plan, params)
    assert prepared.message_ids == [412]


# --- two answers to "which message" ----------------------------------------


async def test_a_link_and_a_different_id_are_refused(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    ctx = build_ctx(conn, tmp_path, client_with_message())

    with pytest.raises(InvalidInput, match="pass one of them, not both"):
        await plan_edit_message(ctx, EditMessageInput(chat=LINK, message_id=87, text="x"))


async def test_the_same_id_twice_is_not_a_disagreement(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Saying it twice is redundant, not contradictory."""
    ctx = build_ctx(conn, tmp_path, client_with_message())

    plan = await plan_edit_message(ctx, EditMessageInput(chat=LINK, message_id=412, text="x"))
    assert "Edit message 412" in plan.summary


async def test_a_link_alongside_a_list_of_ids_is_refused(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    ctx = build_ctx(conn, tmp_path, client_with_message())

    with pytest.raises(InvalidInput, match="not both"):
        await plan_delete_message(ctx, DeleteMessageInput(chat=LINK, message_ids=[87, 88]))


async def test_a_link_and_a_list_naming_that_same_message_agree(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A list of one is not a second answer — it is the same answer twice.

    Refusing it would make `delete` stricter than `edit`, which accepts a scalar
    id that agrees with the link, over a disagreement that does not exist.
    """
    ctx = build_ctx(conn, tmp_path, client_with_message())

    plan = await plan_delete_message(ctx, DeleteMessageInput(chat=LINK, message_ids=[412]))

    assert [snapshot["id"] for snapshot in plan.preconditions["messages"]] == [412]


async def test_neither_a_link_nor_an_id_says_so(conn: sqlite3.Connection, tmp_path: Path) -> None:
    ctx = build_ctx(conn, tmp_path, client_with_message())

    with pytest.raises(InvalidInput, match="or a t.me link"):
        await plan_edit_message(ctx, EditMessageInput(chat=MARKED_GROUP_ID, text="x"))


# --- an id in a list is still a message id ---------------------------------


@pytest.mark.parametrize("bad", [0, -1, 2**31], ids=["zero", "negative", "over-32-bit"])
def test_an_unaddressable_id_in_a_list_is_refused(bad: int) -> None:
    """`Field(le=...)` on a `list[int]` bounds the list, not its elements.

    Without a constrained element type these reached Telethon, where packing a
    32-bit integer raises `struct.error` — a crash instead of a refusal.
    """
    with pytest.raises(ValidationError):
        DeleteMessageInput(chat=MARKED_GROUP_ID, message_ids=[bad])
