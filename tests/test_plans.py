"""The plan store: an intention recorded now, carried out by a person later.

Three properties carry the design. A plan sends nothing. Two applications of one
plan cannot both win, because a message sent twice is the failure this project
is built to avoid. And a message body waiting for review is ciphertext on disk,
because the review queue would otherwise be a second copy of everyone's mail.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from telegram_ai_cli_mcp import db
from telegram_ai_cli_mcp.config import PlansConfig
from telegram_ai_cli_mcp.errors import (
    ErrorCode,
    InvalidInput,
    LimitExceeded,
    PlanExpired,
    PlanNotFound,
    PlanNotPending,
    TelegramAIError,
)
from telegram_ai_cli_mcp.plans import TERMINAL_STATES, Plan, PlanState, PlanStore
from telegram_ai_cli_mcp.secretbox import PREFIX, SecretBox

BODY = "the exact bytes a human is going to approve"


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


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(secrets.token_bytes(32))


@pytest.fixture
def store(conn: sqlite3.Connection, box: SecretBox) -> PlanStore:
    return PlanStore(conn, PlansConfig(), box)


def make(store: PlanStore, **overrides) -> Plan:
    params = {"peer_id": -4242, "text": BODY}
    params.update(overrides.pop("params", {}))
    return store.create(
        operation=overrides.pop("operation", "message.send"),
        account=overrides.pop("account", "work"),
        params=params,
        preconditions=overrides.pop("preconditions", {"peer_id": -4242, "username": "team"}),
        summary=overrides.pop("summary", "send a message to A group"),
    )


def force_expiry(conn: sqlite3.Connection, plan_id: str) -> None:
    """Move the deadline into the past instead of sleeping through a TTL."""
    conn.execute("UPDATE plans SET expires_at = ? WHERE plan_id = ?", (time.time() - 1, plan_id))


# --- creating --------------------------------------------------------------


def test_creating_a_plan_records_it_as_pending_and_sends_nothing(store: PlanStore) -> None:
    plan = make(store)
    assert plan.state is PlanState.PENDING
    assert plan.applied_at is None
    assert plan.outcome is None
    assert plan.params["text"] == BODY
    # Everything needed to review it later, without asking Telegram again.
    assert store.get(plan.plan_id).params == plan.params
    assert store.get(plan.plan_id).preconditions == plan.preconditions


def test_plan_ids_are_unguessable(store: PlanStore) -> None:
    """Sequential ids would let a caller reference a plan it never saw."""
    ids = {make(store).plan_id for _ in range(5)}
    assert len(ids) == 5
    assert all(len(plan_id) == 32 for plan_id in ids)


def test_expiry_is_set_from_the_configured_ttl(conn, box: SecretBox) -> None:
    store = PlanStore(conn, PlansConfig(ttl_seconds=600), box)
    plan = make(store)
    assert 0 < plan.expires_at - plan.created_at <= 600


def test_pending_plans_are_capped(conn, box: SecretBox) -> None:
    store = PlanStore(conn, PlansConfig(max_pending=2), box)
    make(store)
    make(store)
    with pytest.raises(LimitExceeded) as refusal:
        make(store)
    assert refusal.value.code is ErrorCode.LIMIT_EXCEEDED

    # Resolving one frees the quota again.
    store.reject(store.list(state=PlanState.PENDING)[0].plan_id)
    assert make(store)


def test_an_expired_plan_does_not_hold_a_slot_of_the_quota(conn, box: SecretBox) -> None:
    """A plan past its TTL can never be applied, so it must not block new ones.

    Counting it as pending made the ceiling reachable by plans that were
    already dead, and only an unrelated claim() or expire_stale() call would
    ever release the slot.
    """
    store = PlanStore(conn, PlansConfig(max_pending=1), box)
    stale = make(store)
    conn.execute(
        "UPDATE plans SET expires_at = ? WHERE plan_id = ?",
        (time.time() - 1, stale.plan_id),
    )

    assert make(store)
    assert store.get(stale.plan_id).state is PlanState.EXPIRED


def test_encryption_without_a_key_is_refused_rather_than_silently_skipped(conn) -> None:
    """Fail closed: storing the body in the clear is what the setting forbids."""
    with pytest.raises(InvalidInput):
        PlanStore(conn, PlansConfig(encrypt_bodies=True), None)


def test_unknown_plan_ids_are_reported_as_such(store: PlanStore) -> None:
    with pytest.raises(PlanNotFound):
        store.get("0" * 32)
    with pytest.raises(PlanNotFound):
        store.claim("0" * 32)


# --- claiming --------------------------------------------------------------


def test_claim_moves_pending_to_applying(store: PlanStore) -> None:
    plan = make(store)
    claimed = store.claim(plan.plan_id)
    assert claimed.state is PlanState.APPLYING
    assert claimed.plan_id == plan.plan_id
    assert claimed.params == plan.params


def test_a_second_claim_of_the_same_plan_is_refused(store: PlanStore) -> None:
    """The whole defence against applying one plan twice."""
    plan = make(store)
    store.claim(plan.plan_id)
    with pytest.raises(PlanNotPending) as refusal:
        store.claim(plan.plan_id)
    assert refusal.value.code is ErrorCode.PLAN_NOT_PENDING
    assert store.get(plan.plan_id).state is PlanState.APPLYING


@pytest.mark.parametrize(
    "state", [PlanState.APPLIED, PlanState.FAILED, PlanState.REJECTED, PlanState.UNKNOWN_OUTCOME]
)
def test_a_finished_plan_cannot_be_claimed_again(store: PlanStore, state: PlanState) -> None:
    plan = make(store)
    store.claim(plan.plan_id)
    store.finish(plan.plan_id, state=state)
    with pytest.raises(PlanNotPending):
        store.claim(plan.plan_id)


def test_an_expired_plan_is_refused_and_marked_expired(store: PlanStore, conn) -> None:
    """Refusing is half of it: the plan must not stay in the pending queue.

    A plan that reports itself expired on every claim and still sits in
    ``plan list`` as pending is a plan someone keeps trying to apply.
    """
    plan = make(store)
    force_expiry(conn, plan.plan_id)

    with pytest.raises(PlanExpired) as refusal:
        store.claim(plan.plan_id)
    assert refusal.value.code is ErrorCode.PLAN_EXPIRED
    assert store.get(plan.plan_id).state is PlanState.EXPIRED


def test_housekeeping_expires_stale_plans(store: PlanStore, conn) -> None:
    fresh = make(store)
    stale = make(store)
    force_expiry(conn, stale.plan_id)

    assert store.expire_stale() == 1
    assert store.get(stale.plan_id).state is PlanState.EXPIRED
    assert store.get(fresh.plan_id).state is PlanState.PENDING


def test_is_expired_reflects_the_deadline(store: PlanStore, conn) -> None:
    plan = make(store)
    assert plan.is_expired is False
    force_expiry(conn, plan.plan_id)
    assert store.get(plan.plan_id).is_expired is True


# --- finishing and rejecting ----------------------------------------------


def test_finish_records_the_outcome(store: PlanStore) -> None:
    plan = make(store)
    store.claim(plan.plan_id)
    store.finish(plan.plan_id, state=PlanState.APPLIED, outcome={"message_id": 7})

    finished = store.get(plan.plan_id)
    assert finished.state is PlanState.APPLIED
    assert finished.outcome == {"message_id": 7}
    assert finished.applied_at is not None


@pytest.mark.parametrize("state", [PlanState.PENDING, PlanState.APPLYING])
def test_finishing_in_a_non_terminal_state_is_a_programming_error(
    store: PlanStore, state: PlanState
) -> None:
    plan = make(store)
    with pytest.raises(ValueError, match="terminal"):
        store.finish(plan.plan_id, state=state)


def test_unknown_outcome_is_terminal(store: PlanStore) -> None:
    """A timeout after the RPC left is not "did not send".

    It has to be a resting place a person resolves, because an automatic retry
    here is how one message becomes two.
    """
    assert PlanState.UNKNOWN_OUTCOME in TERMINAL_STATES
    plan = make(store)
    store.claim(plan.plan_id)
    store.finish(
        plan.plan_id, state=PlanState.UNKNOWN_OUTCOME, outcome={"detail": "timeout after send"}
    )
    stuck = store.get(plan.plan_id)
    assert stuck.state is PlanState.UNKNOWN_OUTCOME
    with pytest.raises(PlanNotPending):
        store.claim(plan.plan_id)


def test_reject_only_applies_to_pending_plans(store: PlanStore) -> None:
    plan = make(store)
    store.reject(plan.plan_id)
    assert store.get(plan.plan_id).state is PlanState.REJECTED

    with pytest.raises(PlanNotPending):
        store.reject(plan.plan_id)

    claimed = make(store)
    store.claim(claimed.plan_id)
    with pytest.raises(PlanNotPending):
        store.reject(claimed.plan_id)


def test_rejecting_an_unknown_plan_is_refused(store: PlanStore) -> None:
    with pytest.raises(PlanNotPending):
        store.reject("0" * 32)


# --- listing ---------------------------------------------------------------


def test_listing_filters_by_state_and_honours_the_limit(store: PlanStore) -> None:
    first = make(store, summary="first")
    second = make(store, summary="second")
    store.reject(first.plan_id)

    pending = store.list(state=PlanState.PENDING)
    assert [plan.plan_id for plan in pending] == [second.plan_id]
    assert {plan.plan_id for plan in store.list()} == {first.plan_id, second.plan_id}
    assert len(store.list(limit=1)) == 1


# --- storage ---------------------------------------------------------------


def test_the_body_is_encrypted_at_rest(store: PlanStore, conn) -> None:
    """The database is a queue of unsent messages. A stray copy must be useless."""
    plan = make(store)
    raw = conn.execute(
        "SELECT params_json FROM plans WHERE plan_id = ?", (plan.plan_id,)
    ).fetchone()[0]

    assert BODY.encode("utf-8") not in bytes(raw)
    assert bytes(raw).startswith(PREFIX.encode("ascii"))
    # And it is still readable through the store, with the right key.
    assert store.get(plan.plan_id).params["text"] == BODY


def test_a_plan_written_with_another_key_is_not_silently_readable(conn, box: SecretBox) -> None:
    plan = make(PlanStore(conn, PlansConfig(), box))
    other = PlanStore(conn, PlansConfig(), SecretBox(secrets.token_bytes(32)))
    with pytest.raises(TelegramAIError) as failure:
        other.get(plan.plan_id)
    assert "decrypt" in str(failure.value)


def test_plans_survive_reopening_the_database(state_path: Path, box: SecretBox) -> None:
    first = db.connect(state_path)
    plan = make(PlanStore(first, PlansConfig(), box))
    first.close()

    second = db.connect(state_path)
    try:
        reloaded = PlanStore(second, PlansConfig(), box).get(plan.plan_id)
        assert reloaded.state is PlanState.PENDING
        assert reloaded.params["text"] == BODY
    finally:
        second.close()
