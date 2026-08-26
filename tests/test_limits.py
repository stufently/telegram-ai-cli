"""Rate limits that a restart does not reset.

An in-memory counter is not a limit: whatever talked the agent into sending can
also restart the process, and the budget comes back full. So the property under
test is not "does it count" but "does the count survive the object that made
it", and the slot must be taken before the network call rather than after.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from telegram_ai_cli_mcp import db
from telegram_ai_cli_mcp.config import LimitsConfig
from telegram_ai_cli_mcp.errors import ErrorCode, LimitExceeded
from telegram_ai_cli_mcp.limits import LimitKind, LimitStore


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for key in list(os.environ):
        if key.startswith("TGAI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "state.sqlite3"


@pytest.fixture
def conn(state_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(state_path)
    yield connection
    connection.close()


def store(connection: sqlite3.Connection, **overrides) -> LimitStore:
    # Merge into the defaults rather than passing both: spreading overrides
    # alongside the explicit keywords made overriding any named ceiling a
    # TypeError, so the tests that tune one could never run.
    defaults = {
        "sends_per_account": 3,
        "sends_per_target": 2,
        "sends_per_fleet": 10,
        "joins_per_account": 1,
        "admin_ops_per_account": 1,
    }
    return LimitStore(connection, LimitsConfig(**(defaults | overrides)))


def age_every_event(connection: sqlite3.Connection, *, seconds: float) -> None:
    """Pretend time passed, without waiting for it or patching the clock."""
    connection.execute("UPDATE limit_events SET created_at = created_at - ?", (seconds,))


# --- reserving -------------------------------------------------------------


def test_reserving_returns_a_slot_that_names_what_it_covers(conn) -> None:
    reservation = store(conn).reserve(LimitKind.SEND, account="work", target="-4242")
    assert reservation.event_id > 0
    assert reservation.kind is LimitKind.SEND
    assert reservation.account == "work"
    assert reservation.target == "-4242"


def test_the_ceiling_refuses_rather_than_waiting(conn) -> None:
    limits = store(conn)
    limits.reserve(LimitKind.SEND, account="work", target="-4242")
    limits.reserve(LimitKind.SEND, account="work", target="-4242")
    with pytest.raises(LimitExceeded) as refusal:
        limits.reserve(LimitKind.SEND, account="work", target="-4242")
    assert refusal.value.code is ErrorCode.LIMIT_EXCEEDED
    assert refusal.value.retryable is False
    assert refusal.value.retry_after == LimitsConfig().window_seconds


def test_a_refusal_names_the_scope_that_was_hit(conn) -> None:
    """ "Rate limited" alone leaves the caller unable to choose what to change."""
    limits = store(conn)
    limits.reserve(LimitKind.SEND, account="work", target="-4242")
    limits.reserve(LimitKind.SEND, account="work", target="-4242")
    with pytest.raises(LimitExceeded) as refusal:
        limits.reserve(LimitKind.SEND, account="work", target="-4242")
    assert refusal.value.details["scope"] == "per-target"
    assert refusal.value.details["ceiling"] == 2

    # The per-target budget is spent, the per-account one is not.
    assert limits.reserve(LimitKind.SEND, account="work", target="-4343")


def test_the_fleet_wide_ceiling_spans_accounts(conn) -> None:
    limits = store(conn, sends_per_fleet=1)
    limits.reserve(LimitKind.SEND, account="one", target="-4242")
    with pytest.raises(LimitExceeded) as refusal:
        limits.reserve(LimitKind.SEND, account="two", target="-4343")
    assert refusal.value.details["scope"] == "fleet-wide"


def test_a_ceiling_of_zero_reads_as_disabled(conn) -> None:
    with pytest.raises(LimitExceeded, match="disabled"):
        store(conn, sends_per_account=0).reserve(LimitKind.SEND, account="work", target="-4242")


@pytest.mark.parametrize("kind", [LimitKind.JOIN, LimitKind.ADMIN], ids=str)
def test_each_kind_has_its_own_budget(conn, kind: LimitKind) -> None:
    limits = store(conn)
    limits.reserve(kind, account="work", target="-4242")
    with pytest.raises(LimitExceeded):
        limits.reserve(kind, account="work", target="-4343")
    # Spending the join budget must not spend the send budget.
    assert limits.reserve(LimitKind.SEND, account="work", target="-4242")


# --- releasing and committing ---------------------------------------------


def test_release_gives_the_slot_back(conn) -> None:
    limits = store(conn)
    first = limits.reserve(LimitKind.SEND, account="work", target="-4242")
    limits.reserve(LimitKind.SEND, account="work", target="-4242")
    limits.release(first)
    assert limits.reserve(LimitKind.SEND, account="work", target="-4242")


def test_a_committed_slot_is_never_given_back(conn) -> None:
    """Refunding a send that actually happened is how a cap stops being one."""
    limits = store(conn)
    first = limits.reserve(LimitKind.SEND, account="work", target="-4242")
    limits.reserve(LimitKind.SEND, account="work", target="-4242")
    limits.commit(first)
    limits.release(first)
    with pytest.raises(LimitExceeded):
        limits.reserve(LimitKind.SEND, account="work", target="-4242")


def test_an_unresolved_reservation_keeps_consuming_budget(conn) -> None:
    """Neither committed nor released: the safe direction is "counted"."""
    limits = store(conn)
    limits.reserve(LimitKind.SEND, account="work", target="-4242")
    limits.reserve(LimitKind.SEND, account="work", target="-4242")
    with pytest.raises(LimitExceeded):
        limits.reserve(LimitKind.SEND, account="work", target="-4242")


# --- persistence -----------------------------------------------------------


def test_the_count_survives_a_new_store_over_the_same_database(state_path: Path) -> None:
    first = db.connect(state_path)
    store(first).reserve(LimitKind.SEND, account="work", target="-4242")
    store(first).reserve(LimitKind.SEND, account="work", target="-4242")
    first.close()

    # A fresh process would do exactly this: reopen the file and carry on.
    second = db.connect(state_path)
    try:
        with pytest.raises(LimitExceeded):
            store(second).reserve(LimitKind.SEND, account="work", target="-4242")
    finally:
        second.close()


def test_the_database_file_is_private(state_path: Path) -> None:
    connection = db.connect(state_path)
    try:
        assert state_path.stat().st_mode & 0o777 == 0o600
    finally:
        connection.close()


# --- the rolling window ----------------------------------------------------


def test_events_outside_the_window_stop_counting(conn) -> None:
    limits = store(conn)
    limits.reserve(LimitKind.SEND, account="work", target="-4242")
    limits.reserve(LimitKind.SEND, account="work", target="-4242")
    with pytest.raises(LimitExceeded):
        limits.reserve(LimitKind.SEND, account="work", target="-4242")

    age_every_event(conn, seconds=LimitsConfig().window_seconds + 60)
    assert limits.reserve(LimitKind.SEND, account="work", target="-4242")


def test_pruning_drops_only_old_history(conn) -> None:
    limits = store(conn)
    limits.reserve(LimitKind.SEND, account="work", target="-4242")
    assert limits.prune() == 0

    age_every_event(conn, seconds=LimitsConfig().window_seconds * 25)
    assert limits.prune() == 1
    assert limits.reserve(LimitKind.SEND, account="work", target="-4242")
