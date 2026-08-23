"""The ledger of what has already been sent.

The audit log says what happened. It does not stop anything happening twice —
and the failure this guards against is not a race but an *amnesia*: an agent in
a new session, with no memory of the last one, re-sends a message it already
sent. The person on the other end sees the same words twice and cannot tell
which run produced them.

So the properties under test are the ones that survive a process boundary. A
fingerprint that two different processes compute identically. A row that
outlives the object that wrote it. A refusal that says *when* the identical
thing went out and *which plan* did it, because "duplicate" without those two
facts is not something a person can act on. And an explicit way to repeat on
purpose, because people do repeat messages.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_ai_cli import db
from telegram_ai_cli.apply import _outbound_fingerprint, _refuse_duplicate, apply_plan
from telegram_ai_cli.audit import AuditLog
from telegram_ai_cli.config import LedgerConfig, PlansConfig, Settings
from telegram_ai_cli.context import OperationContext
from telegram_ai_cli.errors import DuplicateOutbound, ErrorCode
from telegram_ai_cli.ledger import (
    LEDGERED_OPERATIONS,
    OutboundLedger,
    fingerprint,
    normalise_body,
)
from telegram_ai_cli.limits import LimitStore
from telegram_ai_cli.ops import write
from telegram_ai_cli.ops.write import SendMessageInput
from telegram_ai_cli.plans import PlanState, PlanStore
from telegram_ai_cli.safety import SafetyKernel

CHANNEL_BASE = -(10**12)
GROUP_ID = CHANNEL_BASE - 4242
OTHER_GROUP_ID = CHANNEL_BASE - 9999
TITLE = "Marketing"


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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


def a_fingerprint(**overrides: Any) -> str:
    base: dict[str, Any] = {
        "account": "work",
        "operation": "message.send",
        "peer_id": GROUP_ID,
        "body": "on my way",
    }
    return fingerprint(**{**base, **overrides})


# --- the fingerprint --------------------------------------------------------


def test_the_same_message_to_the_same_peer_fingerprints_the_same() -> None:
    """The whole point: two processes, no shared memory, one answer."""
    assert a_fingerprint() == a_fingerprint()


@pytest.mark.parametrize(
    "override",
    [
        {"peer_id": OTHER_GROUP_ID},
        {"account": "other"},
        {"operation": "message.reply"},
        {"body": "on my way!"},
    ],
    ids=["peer", "account", "operation", "body"],
)
def test_a_change_in_any_of_the_four_makes_a_different_action(override: dict[str, Any]) -> None:
    """Each part is in the fingerprint because changing it changes what the
    person on the other end receives, or who sent it to them."""
    assert a_fingerprint(**override) != a_fingerprint()


def test_the_peer_is_the_numeric_id_rather_than_the_handle() -> None:
    """A handle can change hands; the id is what the applier re-checks. There
    is deliberately no way to fingerprint against a username."""
    with pytest.raises(TypeError):
        fingerprint(account="work", operation="message.send", peer_id="@marketing")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    ["on my way", "on my way\n", "  on my way  ", "on  my   way", "on\tmy way  \n\n"],
    ids=["plain", "trailing-newline", "padded", "doubled-space", "tab-and-trailing"],
)
def test_cosmetic_whitespace_is_not_a_different_message(text: str) -> None:
    """A re-run that emits the same text with a trailing newline is the same
    message; refusing to see that is how the check gets bypassed by accident."""
    assert a_fingerprint(body=text) == a_fingerprint(body="on my way")


def test_a_change_of_case_is_a_change_of_message() -> None:
    """Deliberately not case-folded. The ledger catches *re-runs*, which emit
    byte-identical text; deciding that "OK" and "ok" are one message is a
    judgement about somebody's writing that this tool has no business making."""
    assert a_fingerprint(body="OK") != a_fingerprint(body="ok")


def test_a_line_break_is_a_change_of_message() -> None:
    """Telegram renders it. Collapsing "a\\nb" into "a b" would call two things
    that look different on the screen one thing."""
    assert a_fingerprint(body="a\nb") != a_fingerprint(body="a b")


def test_whitespace_inside_a_line_is_still_cosmetic() -> None:
    assert a_fingerprint(body="a  b\n  c  ") == a_fingerprint(body="a b\nc")


def test_an_empty_body_and_a_missing_one_are_the_same_thing() -> None:
    assert a_fingerprint(body=None) == a_fingerprint(body="")


def test_two_files_with_one_caption_are_told_apart_by_their_digest() -> None:
    """`resolve_outbound` already computes this sha256, and it is reused rather
    than hashing the bytes a second way."""
    one = a_fingerprint(operation="message.send_file", body="here", file_sha256="a" * 64)
    two = a_fingerprint(operation="message.send_file", body="here", file_sha256="b" * 64)
    assert one != two


def test_the_same_file_sent_twice_is_one_fingerprint() -> None:
    args = {"operation": "message.send_file", "body": "", "file_sha256": "c" * 64}
    assert a_fingerprint(**args) == a_fingerprint(**args)


def test_a_reply_to_a_different_message_is_a_different_action() -> None:
    one = a_fingerprint(operation="message.reply", extra={"reply_to_message_id": 5})
    two = a_fingerprint(operation="message.reply", extra={"reply_to_message_id": 9})
    assert one != two


@pytest.mark.parametrize(
    ("one", "two"),
    [
        ({"link_preview": True}, {"link_preview": False}),
        ({"drop_author": True}, {"drop_author": False}),
        ({"delivery": "photo"}, {"delivery": "document"}),
        ({"file_name": "a.jpg"}, {"file_name": "b.jpg"}),
    ],
    ids=["link-preview", "drop-author", "delivery", "file-name"],
)
def test_a_choice_that_changes_how_it_renders_is_a_different_action(
    one: dict[str, Any], two: dict[str, Any]
) -> None:
    """The same bytes are not the same arrival: a JPEG sent as a compressed
    photo and the same JPEG sent as a document are two different objects in the
    chat, and both descriptions were in the summary somebody approved."""
    assert a_fingerprint(extra=one) != a_fingerprint(extra=two)


def test_normalising_a_body_leaves_the_words_alone() -> None:
    assert normalise_body("  hello   world \n") == "hello world"
    assert normalise_body(None) == ""


def test_only_the_operations_that_put_new_words_in_a_chat_are_ledgered() -> None:
    """Scope is deliberately narrow. Reacting twice, pinning twice or joining
    twice is either idempotent at Telegram's end or invisible to anybody else;
    a *message* arriving twice is the thing a person cannot unsee."""
    assert (
        frozenset({"message.send", "message.reply", "message.send_file", "message.forward"})
        == LEDGERED_OPERATIONS
    )


# --- the store --------------------------------------------------------------


def ledger(conn: sqlite3.Connection, **config: Any) -> OutboundLedger:
    return OutboundLedger(conn, LedgerConfig(**config))


def record(store: OutboundLedger, digest: str, *, plan_id: str = "plan-1") -> Any:
    return store.record(
        digest=digest,
        account="work",
        operation="message.send",
        peer_id=GROUP_ID,
        plan_id=plan_id,
    )


def test_nothing_is_a_duplicate_of_an_empty_ledger(conn: sqlite3.Connection) -> None:
    assert ledger(conn).find_recent(a_fingerprint()) is None


def test_a_recorded_send_is_found_again_with_its_plan_and_its_time(
    conn: sqlite3.Connection,
) -> None:
    store = ledger(conn)
    record(store, a_fingerprint(), plan_id="4f0f8a2b")

    prior = store.find_recent(a_fingerprint())

    assert prior is not None
    assert prior.plan_id == "4f0f8a2b"
    assert prior.sent_at == pytest.approx(time.time(), abs=10)


def test_a_send_outside_the_window_is_not_a_duplicate(conn: sqlite3.Connection) -> None:
    """Identical messages a month apart are two messages, not one repeated."""
    store = ledger(conn, window_seconds=3600)
    record(store, a_fingerprint())
    conn.execute("UPDATE outbound_ledger SET sent_at = sent_at - ?", (7200,))

    assert store.find_recent(a_fingerprint()) is None


def test_a_window_of_zero_turns_the_check_off(conn: sqlite3.Connection) -> None:
    """One knob, and the way to disable it is to say so rather than to delete
    rows behind the check's back."""
    store = ledger(conn, window_seconds=0)
    record(store, a_fingerprint())

    assert store.find_recent(a_fingerprint()) is None


def test_a_row_survives_the_object_that_wrote_it(state_path: Path) -> None:
    """A second process must see the first one's sends; that is the entire
    reason this is a file rather than a set in memory."""
    first = db.connect(state_path)
    record(ledger(first), a_fingerprint())
    first.close()

    second = db.connect(state_path)
    try:
        assert ledger(second).find_recent(a_fingerprint()) is not None
    finally:
        second.close()


def test_two_connections_can_both_write(state_path: Path) -> None:
    """Concurrent processes share one file; the writes must not collide."""
    one, two = db.connect(state_path), db.connect(state_path)
    try:
        record(ledger(one), a_fingerprint(body="one"), plan_id="p1")
        record(ledger(two), a_fingerprint(body="two"), plan_id="p2")
        assert ledger(one).find_recent(a_fingerprint(body="two")) is not None
        assert ledger(two).find_recent(a_fingerprint(body="one")) is not None
    finally:
        one.close()
        two.close()


def test_a_deliberate_repeat_leaves_a_second_row(conn: sqlite3.Connection) -> None:
    """The fingerprint is not a unique key: a repeat that was approved on
    purpose is still a send, and the next accidental one must be caught."""
    store = ledger(conn)
    record(store, a_fingerprint(), plan_id="p1")
    record(store, a_fingerprint(), plan_id="p2")

    prior = store.find_recent(a_fingerprint())

    assert prior is not None
    assert prior.plan_id == "p2", "the most recent send is the one worth naming"


def test_settling_dates_the_row_from_when_the_send_finished(conn: sqlite3.Connection) -> None:
    """The row is written before the request leaves, so until the send completes
    its timestamp is when the attempt *started*. For a long upload those are
    minutes apart, and a window counted from the start would expire before the
    file had finished arriving."""
    store = ledger(conn)
    entry = record(store, a_fingerprint())
    conn.execute("UPDATE outbound_ledger SET sent_at = sent_at - ?", (600,))

    store.settle(entry)

    prior = store.find_recent(a_fingerprint())
    assert prior is not None
    assert prior.age_seconds() < 10


def test_forgetting_a_row_makes_it_no_longer_a_duplicate(conn: sqlite3.Connection) -> None:
    """Written before the request leaves, dropped only where the rate-limit
    slot is dropped: on an error class that proves Telegram refused it."""
    store = ledger(conn)
    entry = record(store, a_fingerprint())

    store.forget(entry)

    assert store.find_recent(a_fingerprint()) is None


def test_forgetting_one_row_leaves_the_others(conn: sqlite3.Connection) -> None:
    store = ledger(conn)
    kept = record(store, a_fingerprint(), plan_id="p1")
    dropped = record(store, a_fingerprint(body="other"), plan_id="p2")

    store.forget(dropped)

    assert store.find_recent(a_fingerprint()) is not None
    assert store.find_recent(a_fingerprint(body="other")) is None
    assert kept.row_id != dropped.row_id


def test_prune_drops_what_the_window_can_no_longer_see(conn: sqlite3.Connection) -> None:
    store = ledger(conn, window_seconds=3600)
    record(store, a_fingerprint())
    # Parameterised, not inlined: a `10_000_000` literal in SQL needs SQLite
    # 3.46, and the oldest interpreter this project supports ships an older one.
    conn.execute("UPDATE outbound_ledger SET sent_at = sent_at - ?", (10_000_000,))

    assert store.prune() == 1
    assert conn.execute("SELECT COUNT(*) FROM outbound_ledger").fetchone()[0] == 0


# --- configuration ----------------------------------------------------------


def test_the_window_defaults_to_six_hours() -> None:
    """Long enough to span a restart, a retried script or a fresh agent
    session; short enough that a daily message is never caught by accident."""
    assert Settings().ledger.window_seconds == 6 * 60 * 60


def test_the_window_is_configurable_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TGAI_LEDGER__WINDOW_SECONDS", "60")
    assert Settings().ledger.window_seconds == 60


def test_a_negative_window_is_refused() -> None:
    with pytest.raises(ValueError):
        LedgerConfig(window_seconds=-1)


def test_every_built_context_has_a_ledger(tmp_path: Path) -> None:
    """The check is not optional in production, so the thing it consults is
    not optional either."""
    settings = Settings(paths={"state": tmp_path / "state"})  # type: ignore[arg-type]
    with OperationContext.build(actor="cli", settings=settings) as ctx:
        assert ctx.ledger is not None


# --- the applier ------------------------------------------------------------


def entities() -> Any:
    from telethon.tl.types import Channel

    return Channel(id=4242, title=TITLE, photo=None, date=None, megagroup=True)


class FakeClient:
    """Resolves one group and answers a send with a message id."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def get_entity(self, target: Any) -> Any:
        if target in (GROUP_ID, "marketing"):
            return entities()
        raise ValueError(f"no entity for {target!r}")

    async def get_input_entity(self, peer_id: Any) -> Any:
        from telethon.tl.types import InputPeerChannel

        return InputPeerChannel(channel_id=4242, access_hash=1)

    async def send_message(self, peer: Any, text: str, **kwargs: Any) -> Any:
        self.sent.append((peer, text))
        return SimpleNamespace(id=100 + len(self.sent))

    async def __call__(self, request: Any) -> Any:
        return SimpleNamespace(updates=[], users=[], chats=[])


@dataclass
class FakePlan:
    operation: str
    account: str = "work"
    plan_id: str = "fake"
    preconditions: dict[str, Any] | None = None


def context(tmp_path: Path, **overrides: Any) -> OperationContext:
    settings = Settings(
        profile="plan",
        safety={"write": {"send": {"allow": [GROUP_ID]}}},  # type: ignore[arg-type]
        **overrides,
    )
    conn = db.connect(tmp_path / "state.db")
    return OperationContext(
        settings=settings,
        safety=SafetyKernel(settings),
        plans=PlanStore(conn, PlansConfig(encrypt_bodies=False)),
        limits=LimitStore(conn, settings.limits),
        audit=AuditLog(tmp_path / "audit.jsonl", settings.audit),
        actor="cli",
        _conn=conn,
    )


@pytest.fixture(autouse=True)
def _no_real_account(monkeypatch: pytest.MonkeyPatch) -> Any:
    client = FakeClient()

    @asynccontextmanager
    async def writer(ctx: Any, account: str | None):  # noqa: ANN401
        yield "work", client

    monkeypatch.setattr(write, "open_writer", writer)
    return client


@pytest.fixture
def client(_no_real_account: FakeClient) -> FakeClient:
    return _no_real_account


async def plan_a_message(ctx: OperationContext, text: str = "on my way", **extra: Any) -> Any:
    return await write.plan_send_message(ctx, SendMessageInput(chat=GROUP_ID, text=text, **extra))


async def test_the_second_identical_send_is_refused(tmp_path: Path, client: FakeClient) -> None:
    """The failure this exists for, end to end: two plans, one text, one peer.

    The first is applied. The second is refused rather than skipped — a caller
    that cannot tell "sent" from "silently didn't" is worse than one that errors.
    """
    ctx = context(tmp_path)
    first = await plan_a_message(ctx)
    second = await plan_a_message(ctx)

    assert (await apply_plan(ctx, first.plan_id)).ok
    result = await apply_plan(ctx, second.plan_id)

    assert not result.ok
    assert result.error is not None
    assert result.error["code"] == str(ErrorCode.DUPLICATE_OUTBOUND)
    assert client.sent == [(GROUP_ID, "on my way")], "the second send must not leave"


async def test_the_refusal_names_when_it_happened_and_which_plan_did_it(
    tmp_path: Path, client: FakeClient
) -> None:
    ctx = context(tmp_path)
    first = await plan_a_message(ctx)
    second = await plan_a_message(ctx)
    await apply_plan(ctx, first.plan_id)

    result = await apply_plan(ctx, second.plan_id)

    assert result.error is not None
    message = str(result.error["message"])
    assert first.plan_id in message
    assert "ago" in message
    assert str(GROUP_ID) in message


async def test_the_refusal_quotes_no_chat_title(tmp_path: Path, client: FakeClient) -> None:
    """`Envelope.failure` neither wraps nor defangs untrusted text, so the chat
    is named by the one part of it a stranger cannot choose."""
    ctx = context(tmp_path)
    first = await plan_a_message(ctx)
    second = await plan_a_message(ctx)
    await apply_plan(ctx, first.plan_id)

    result = await apply_plan(ctx, second.plan_id)

    assert result.error is not None
    rendered = str(result.error)
    assert TITLE not in rendered


async def test_a_duplicate_does_not_burn_a_rate_limit_slot(
    tmp_path: Path, client: FakeClient
) -> None:
    """The check sits before the reservation: a refusal that never reached
    Telegram must not spend a budget meant for requests that did."""
    ctx = context(tmp_path)
    first = await plan_a_message(ctx)
    second = await plan_a_message(ctx)
    await apply_plan(ctx, first.plan_id)
    before = ctx._conn.execute("SELECT COUNT(*) FROM limit_events").fetchone()[0]  # type: ignore[union-attr]

    await apply_plan(ctx, second.plan_id)

    after = ctx._conn.execute("SELECT COUNT(*) FROM limit_events").fetchone()[0]  # type: ignore[union-attr]
    assert (before, after) == (1, 1)


async def test_a_deliberate_repeat_is_allowed_through(tmp_path: Path, client: FakeClient) -> None:
    """People do repeat messages; the way to do it is explicit, never a default."""
    ctx = context(tmp_path)
    first = await plan_a_message(ctx)
    second = await plan_a_message(ctx, allow_duplicate=True)
    await apply_plan(ctx, first.plan_id)

    result = await apply_plan(ctx, second.plan_id)

    assert result.ok
    assert len(client.sent) == 2


async def test_a_deliberate_repeat_is_still_recorded(tmp_path: Path, client: FakeClient) -> None:
    """Approving one repeat does not approve the next one."""
    ctx = context(tmp_path)
    plans = [
        await plan_a_message(ctx),
        await plan_a_message(ctx, allow_duplicate=True),
        await plan_a_message(ctx),
    ]
    await apply_plan(ctx, plans[0].plan_id)
    await apply_plan(ctx, plans[1].plan_id)

    assert not (await apply_plan(ctx, plans[2].plan_id)).ok


async def test_the_repeat_flag_says_so_in_the_approval_preview(tmp_path: Path) -> None:
    """A human approving a repeat has to be told it is one."""
    ctx = context(tmp_path)
    plain = await plan_a_message(ctx)
    repeat = await plan_a_message(ctx, allow_duplicate=True)

    assert "repeat" not in plain.summary.lower()
    assert "REPEAT" in repeat.summary


async def test_a_bookkeeping_failure_after_the_send_keeps_the_row(
    tmp_path: Path, client: FakeClient
) -> None:
    """The window after the RPC returns is not harmless. Recording the outcome
    can itself fail — a full disk, a locked database — and the message has
    already gone. Treating that as "nothing left the machine" refunded the slot
    and dropped the row, and the next identical plan went out for real.
    """
    ctx = context(tmp_path)
    first = await plan_a_message(ctx)
    second = await plan_a_message(ctx)
    finish = ctx.plans.finish

    def once(plan_id: str, *, state: Any, outcome: Any = None) -> None:
        if state is PlanState.APPLIED:
            raise sqlite3.OperationalError("database is locked")
        finish(plan_id, state=state, outcome=outcome)

    ctx.plans.finish = once  # type: ignore[method-assign]
    result = await apply_plan(ctx, first.plan_id)
    ctx.plans.finish = finish  # type: ignore[method-assign]

    assert not result.ok, "the caller is told the outcome is unresolved"
    assert len(client.sent) == 1
    assert not (await apply_plan(ctx, second.plan_id)).ok, "the second copy must still be refused"
    assert len(client.sent) == 1


def test_the_repeat_flag_is_not_part_of_the_fingerprint() -> None:
    """Otherwise a repeat, once approved, would never be seen again."""
    plan = FakePlan("message.send")
    prepared = SimpleNamespace(audit_peer_id=GROUP_ID, audit_body="hi", attachment=None)
    plain = SendMessageInput(chat=GROUP_ID, text="hi")
    repeat = SendMessageInput(chat=GROUP_ID, text="hi", allow_duplicate=True)

    assert _outbound_fingerprint(plan, plain, prepared) == _outbound_fingerprint(
        plan, repeat, prepared
    )


def test_an_operation_outside_the_scope_is_never_fingerprinted() -> None:
    plan = FakePlan("chat.join")
    prepared = SimpleNamespace(audit_peer_id=GROUP_ID, audit_body=None, attachment=None)

    assert _outbound_fingerprint(plan, SimpleNamespace(), prepared) is None


def test_refusing_a_duplicate_is_a_policy_refusal(tmp_path: Path) -> None:
    """It is raised where every other refusal is raised, so the applier's own
    bookkeeping closes the plan and gives back the slot without a special case."""
    ctx = context(tmp_path)
    assert ctx.ledger is not None
    plan = FakePlan("message.send")
    params = SendMessageInput(chat=GROUP_ID, text="hi")
    prepared = SimpleNamespace(audit_peer_id=GROUP_ID, audit_body="hi", attachment=None)
    digest = _outbound_fingerprint(plan, params, prepared)
    assert digest is not None
    record(ctx.ledger, digest, plan_id="earlier")

    with pytest.raises(DuplicateOutbound) as refusal:
        _refuse_duplicate(ctx, plan, params, prepared)

    assert refusal.value.code is ErrorCode.DUPLICATE_OUTBOUND
    assert refusal.value.retryable is False
    assert "allow_duplicate" in str(refusal.value.suggestion)


def test_a_context_without_a_database_refuses_rather_than_waving_it_through(
    tmp_path: Path,
) -> None:
    """Fail closed. A missing ledger is a broken installation, not permission."""
    from telegram_ai_cli.errors import InvalidInput

    ctx = context(tmp_path)
    ctx.ledger = None
    plan = FakePlan("message.send")
    prepared = SimpleNamespace(audit_peer_id=GROUP_ID, audit_body="hi", attachment=None)

    with pytest.raises(InvalidInput):
        _refuse_duplicate(ctx, plan, SendMessageInput(chat=GROUP_ID, text="hi"), prepared)
