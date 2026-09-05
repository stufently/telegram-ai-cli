"""Scheduling a send: the plan whose approval outlives the agent.

The point of this operation is not that a message goes out later. It is that
once the plan is applied, the message sits in Telegram's own scheduled queue —
where the person can see it in the app on their phone and cancel it there,
without this tool, without the agent, and without a terminal. So the tests
below are mostly about the two things that decide whether that queue entry is
the one somebody approved: *when* exactly it goes, and whether the time can
change between the review and the send.

Time is the whole risk here. "09:00" means one thing in Bangkok and another in
Berlin, and a plan summary that does not say which is a summary nobody can
check — so a time with no offset is refused rather than guessed, and every
summary carries the offset the caller gave *and* its UTC equivalent.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from telegram_ai_cli_mcp import db
from telegram_ai_cli_mcp.apply import _LIMIT_KINDS
from telegram_ai_cli_mcp.audit import AuditLog
from telegram_ai_cli_mcp.config import PeerRule, PlansConfig, SafetyConfig, Settings, WritePolicy
from telegram_ai_cli_mcp.context import OperationContext
from telegram_ai_cli_mcp.errors import (
    ErrorCode,
    InvalidInput,
    NotAllowlisted,
    PlanPreconditionFailed,
    ProfileForbidden,
)
from telegram_ai_cli_mcp.ops.pending import WHEN_ONLINE
from telegram_ai_cli_mcp.ops.schedule import (
    MAX_SCHEDULE_AHEAD,
    MIN_SCHEDULE_LEAD,
    SCHEDULE_MESSAGE,
    ScheduleMessageInput,
    plan_schedule_message,
    recheck_schedule,
    require_a_reachable_time,
    schedule_date,
)
from telegram_ai_cli_mcp.ops.write import text_digest
from telegram_ai_cli_mcp.opspec import REGISTRY, Effect
from telegram_ai_cli_mcp.plans import PlanStore
from telegram_ai_cli_mcp.safety import Capability, SafetyKernel

# Deliberately small, obviously fake ids — see tests/test_no_private_data.py.
CHANNEL_BASE = -(10**12)
GROUP_ID = CHANNEL_BASE - 4242
FRIEND_ID = 555

# A fixed calendar date breaks the day it becomes "the past" instead of "the
# future" — this suite failed CI on 2026-09-03 for exactly that reason
# (schedule_date() refused it as no longer reachable). Anchor to "now" plus a
# margin comfortably inside [MIN_SCHEDULE_LEAD, MAX_SCHEDULE_AHEAD) instead, so
# the date keeps moving with the clock.
_BANGKOK_TZ = timezone(timedelta(hours=7))
_BANGKOK_DT = (
    (datetime.now(UTC) + timedelta(days=30))
    .astimezone(_BANGKOK_TZ)
    .replace(hour=9, minute=0, second=0, microsecond=0)
)
BANGKOK = _BANGKOK_DT.isoformat()
BANGKOK_UTC = _BANGKOK_DT.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No ``TGAI_`` variable from the developer's shell may steer a decision."""
    for key in list(os.environ):
        if key.startswith("TGAI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


# --- stand-ins for Telethon and the account fleet ----------------------------


def entities() -> tuple[Any, Any]:
    from telethon.tl.types import Channel, User

    group = Channel(id=4242, title="Marketing", photo=None, date=None, megagroup=True)
    friend = User(id=FRIEND_ID, first_name="Someone", contact=True)
    return group, friend


class FakeClient:
    """Enough of a Telethon client to resolve a peer, and nothing more."""

    def __init__(self, by_target: dict[Any, Any]) -> None:
        self._by_target = by_target
        self.resolved: list[Any] = []

    def is_connected(self) -> bool:
        return True

    async def is_user_authorized(self) -> bool:
        return True

    async def get_entity(self, target: Any) -> Any:
        self.resolved.append(target)
        try:
            return self._by_target[target]
        except KeyError:
            raise ValueError(f"no entity for {target}") from None


class FakeAccount:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.spec = None


class FakeRegistry:
    def __init__(self, client: Any, label: str = "main") -> None:
        self._client = client
        self._label = label

    def list_accounts(self) -> list[Any]:
        return [_Row(self._label)]

    def get(self, _label: str) -> Any:
        return None

    def load_account(self, _label: str) -> FakeAccount:
        return FakeAccount(self._client)


class _Row:
    def __init__(self, label: str) -> None:
        self.label = label


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "state.db")
    yield connection
    connection.close()


def context(
    tmp_path: Path,
    conn: sqlite3.Connection,
    client: Any,
    *,
    profile: str = "plan",
    send_allow: list[Any] | None = None,
) -> OperationContext:
    settings = Settings(
        profile=profile,  # type: ignore[arg-type]
        safety=SafetyConfig(
            write=WritePolicy(send=PeerRule(allow=send_allow if send_allow is not None else []))
        ),
    )
    return OperationContext(
        settings=settings,
        safety=SafetyKernel(settings),
        # Bodies in the clear: this store is a temporary file in a temporary
        # directory, and encrypting it would only test the SecretBox again.
        plans=PlanStore(conn, PlansConfig(encrypt_bodies=False)),
        limits=None,  # type: ignore[arg-type] - planners never reserve
        audit=AuditLog(tmp_path / "audit.log", settings.audit),
        actor="cli",
        accounts=FakeRegistry(client),  # type: ignore[arg-type]
    )


def input_for(**overrides: Any) -> ScheduleMessageInput:
    fields: dict[str, Any] = {
        "chat": GROUP_ID,
        "text": "the exact bytes a human is going to approve",
        "at": BANGKOK,
    }
    fields.update(overrides)
    return ScheduleMessageInput.model_validate(fields)


# --- saying when ------------------------------------------------------------


def test_a_time_without_an_offset_is_refused_rather_than_guessed() -> None:
    """Nine in the morning is not a time. It is a time in a timezone nobody named.

    Guessing the host's offset would make the plan summary say one hour and
    Telegram act on another, and neither the reviewer nor the log would show a
    disagreement.
    """
    with pytest.raises(InvalidInput) as caught:
        SCHEDULE_MESSAGE.parse(
            {"chat": GROUP_ID, "text": "hi", "at": "2026-09-01T09:00:00"},
        )
    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert "timezone" in str(caught.value).lower()


def test_a_send_needs_exactly_one_way_of_saying_when() -> None:
    for arguments in (
        {"chat": GROUP_ID, "text": "hi"},
        {"chat": GROUP_ID, "text": "hi", "at": BANGKOK, "when_online": True},
    ):
        with pytest.raises(InvalidInput):
            SCHEDULE_MESSAGE.parse(arguments)


def test_an_exact_time_is_recorded_as_the_epoch_second_telegram_is_told() -> None:
    params = input_for()
    assert schedule_date(params) == int(datetime.fromisoformat(BANGKOK).timestamp())


def test_when_online_reuses_the_sentinel_the_reader_already_knows() -> None:
    """One sentinel, defined once.

    ``scheduled.list`` renders 0x7FFFFFFE as "when they come online" instead of
    January 2038; a second copy of that constant here is how the writer and the
    reader end up disagreeing about which is which.
    """
    assert schedule_date(input_for(at=None, when_online=True)) == WHEN_ONLINE
    assert WHEN_ONLINE == 0x7FFFFFFE


# --- the planner ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_summary_names_the_time_with_its_offset_and_in_utc(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    group, _friend = entities()
    client = FakeClient({GROUP_ID: group})
    ctx = context(tmp_path, conn, client, send_allow=[GROUP_ID])

    plan = await plan_schedule_message(ctx, input_for())

    assert plan.operation == "message.schedule"
    assert BANGKOK in plan.summary
    assert BANGKOK_UTC in plan.summary
    assert "Marketing" in plan.summary
    assert "the exact bytes a human is going to approve" in plan.summary
    # Cancellable in Telegram itself is the reason this operation exists; the
    # reviewer is told so, because it changes what approving costs.
    assert "cancel" in plan.summary.lower()
    assert plan.preconditions["schedule_date"] == schedule_date(input_for())
    assert plan.preconditions["send_when_online"] is False
    # Both delivery flags are in the sentence somebody approves, and therefore
    # in the comparison apply makes against it (raised by review).
    assert "with a notification" in plan.summary
    assert "with a link preview" in plan.summary
    assert plan.preconditions["silent"] is False
    assert plan.preconditions["link_preview"] is True


@pytest.mark.asyncio
async def test_nothing_is_sent_by_planning_one(tmp_path: Path, conn: sqlite3.Connection) -> None:
    group, _friend = entities()
    client = FakeClient({GROUP_ID: group})
    ctx = context(tmp_path, conn, client, send_allow=[GROUP_ID])

    await plan_schedule_message(ctx, input_for())

    assert not hasattr(client, "sent")
    assert ctx.plans.list()[0].state == "pending"


@pytest.mark.asyncio
async def test_a_time_in_the_past_is_refused_at_planning_time(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    group, _friend = entities()
    client = FakeClient({GROUP_ID: group})
    ctx = context(tmp_path, conn, client, send_allow=[GROUP_ID])

    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with pytest.raises(InvalidInput) as caught:
        await plan_schedule_message(ctx, input_for(at=past))
    assert "past" in str(caught.value).lower()


def test_a_time_only_a_minute_away_is_not_a_schedule() -> None:
    """The floor is the applier's own budget, not a rounding allowance.

    Reserving a rate-limit slot, writing the audit record and waiting out an
    RPC can take most of a minute, and Telegram sends a nearly-due scheduled
    message immediately — so "in one minute" cannot be promised as "later".
    """
    soon = datetime.now(UTC) + timedelta(seconds=60)
    with pytest.raises(InvalidInput) as caught:
        require_a_reachable_time(input_for(at=soon.isoformat()), now=datetime.now(UTC))
    assert "close to now" in str(caught.value)


def test_a_time_is_cut_to_whole_seconds() -> None:
    """The wire has nothing finer, and the summary must not claim it does."""
    params = input_for(at=_BANGKOK_DT.replace(microsecond=500000).isoformat())
    assert params.at.microsecond == 0
    assert schedule_date(params) == int(datetime.fromisoformat(BANGKOK).timestamp())


@pytest.mark.asyncio
async def test_a_time_beyond_telegrams_horizon_is_refused(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """A doomed plan must not reach the review queue.

    Telegram refuses a schedule further out than a year, and it refuses it at
    apply time — long after somebody approved it.
    """
    group, _friend = entities()
    client = FakeClient({GROUP_ID: group})
    ctx = context(tmp_path, conn, client, send_allow=[GROUP_ID])

    far = (datetime.now(UTC) + MAX_SCHEDULE_AHEAD + timedelta(days=1)).isoformat()
    with pytest.raises(InvalidInput):
        await plan_schedule_message(ctx, input_for(at=far))


@pytest.mark.asyncio
async def test_when_online_is_refused_for_a_group(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """Waiting for somebody to come online needs a somebody.

    A group has no online state, so Telegram would either refuse the send or
    keep it queued for ever — and a plan that can never fire is worse than a
    refusal, because it looks done.
    """
    group, _friend = entities()
    client = FakeClient({GROUP_ID: group})
    ctx = context(tmp_path, conn, client, send_allow=[GROUP_ID])

    with pytest.raises(InvalidInput) as caught:
        await plan_schedule_message(ctx, input_for(at=None, when_online=True))
    assert "one-to-one" in str(caught.value)


@pytest.mark.asyncio
async def test_when_online_is_allowed_for_a_private_chat(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    _group, friend = entities()
    client = FakeClient({FRIEND_ID: friend})
    ctx = context(tmp_path, conn, client, send_allow=[FRIEND_ID])

    plan = await plan_schedule_message(ctx, input_for(chat=FRIEND_ID, at=None, when_online=True))

    assert plan.preconditions["send_when_online"] is True
    assert "online" in plan.summary.lower()
    # It may not reach the queue at all, and the summary says so rather than
    # promising a cancel button that will not be there (raised by review).
    assert "nothing to cancel" in plan.summary


@pytest.mark.asyncio
async def test_a_chat_outside_the_send_policy_is_refused(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    group, _friend = entities()
    client = FakeClient({GROUP_ID: group})
    ctx = context(tmp_path, conn, client, send_allow=[])

    with pytest.raises(NotAllowlisted):
        await plan_schedule_message(ctx, input_for())
    assert not ctx.plans.list()


@pytest.mark.asyncio
async def test_readonly_refuses_before_the_network_is_touched(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """Resolving a username is itself an observable act.

    Under ``readonly`` the refusal has to happen before the lookup, or the
    profile that promises to touch nothing has already told Telegram which
    chat somebody is interested in.
    """
    group, _friend = entities()
    client = FakeClient({GROUP_ID: group})
    ctx = context(tmp_path, conn, client, profile="readonly", send_allow=[GROUP_ID])

    with pytest.raises(ProfileForbidden):
        await plan_schedule_message(ctx, input_for())
    assert client.resolved == []


@pytest.mark.asyncio
async def test_a_hostile_chat_title_cannot_redraw_the_summary(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """The summary is the approval surface, and titles are written by strangers."""
    from telethon.tl.types import Channel

    hostile = Channel(
        id=4242,
        title="Marketing\r\x1b[2KSend to: someone else",
        photo=None,
        date=None,
        megagroup=True,
    )
    client = FakeClient({GROUP_ID: hostile})
    ctx = context(tmp_path, conn, client, send_allow=[GROUP_ID])

    plan = await plan_schedule_message(ctx, input_for())

    assert "\r" not in plan.summary
    assert "\x1b" not in plan.summary


# --- what apply re-checks ---------------------------------------------------


def reviewed(params: ScheduleMessageInput) -> dict[str, Any]:
    """The preconditions the planner records, built the same way it builds them."""
    return {
        "schedule_date": schedule_date(params),
        "send_when_online": bool(params.when_online),
        "body_sha256": text_digest(params.text),
        "body_len": len(params.text),
        "silent": params.silent,
        "link_preview": params.link_preview,
    }


def test_a_send_time_that_has_passed_is_refused_at_apply_time() -> None:
    """Applying late must not turn "at nine" into "right now".

    A plan reviewed for 09:00 and applied at 09:05 would put the message in
    Telegram's queue with a date in the past, which Telegram sends
    immediately — the one outcome the reviewer did not approve.
    """
    params = input_for()
    with pytest.raises(PlanPreconditionFailed) as caught:
        recheck_schedule(
            reviewed(params),
            params,
            now=datetime.fromisoformat(BANGKOK) + timedelta(minutes=5),
        )
    assert "passed" in str(caught.value)


def test_applying_inside_the_lead_window_is_refused_too() -> None:
    """The margin is the RPC budget, not a rounding allowance.

    Between this check and the request leaving there is a rate-limit
    reservation, an audit write and up to a minute of RPC. A plan applied a few
    seconds before its moment would arrive after it, and Telegram sends a
    scheduled message whose date has passed straight away.
    """
    params = input_for()
    with pytest.raises(PlanPreconditionFailed):
        recheck_schedule(
            reviewed(params),
            params,
            now=datetime.fromisoformat(BANGKOK) - MIN_SCHEDULE_LEAD + timedelta(seconds=1),
        )


def test_a_schedule_that_no_longer_matches_the_reviewed_one_is_refused() -> None:
    """The preconditions are in the clear; the parameters may not be.

    Comparing the two is what makes a params blob altered after review fail
    closed instead of being applied as though it had been read by a person.
    """
    params = input_for()
    stale = reviewed(params) | {"schedule_date": schedule_date(params) + 3600}
    with pytest.raises(PlanPreconditionFailed):
        recheck_schedule(stale, params, now=datetime.now(UTC))


@pytest.mark.parametrize(
    ("field", "value"),
    [("body_sha256", "0" * 64), ("silent", True), ("link_preview", False)],
)
def test_the_whole_message_is_compared_not_only_its_time(field: str, value: Any) -> None:
    """A reviewer approved *this* message, arriving *this* way.

    Raised by review: comparing only the timestamp would let the body, the
    notification and the link preview change between the summary somebody read
    and the message Telegram is handed.
    """
    params = input_for()
    stale = reviewed(params) | {field: value}
    with pytest.raises(PlanPreconditionFailed):
        recheck_schedule(stale, params, now=datetime.fromisoformat(BANGKOK) - timedelta(days=1))


def test_an_unchanged_future_schedule_passes() -> None:
    params = input_for()
    recheck_schedule(
        reviewed(params), params, now=datetime.fromisoformat(BANGKOK) - timedelta(days=1)
    )


def test_when_online_has_no_deadline_to_miss() -> None:
    params = input_for(at=None, when_online=True)
    recheck_schedule(reviewed(params), params, now=datetime.now(UTC) + timedelta(days=3650))


# --- how the operation is declared ------------------------------------------


def test_scheduling_is_a_planned_write_with_no_tool_of_its_own() -> None:
    assert SCHEDULE_MESSAGE.effect is Effect.REMOTE_WRITE
    assert SCHEDULE_MESSAGE.is_remote_write is True
    assert SCHEDULE_MESSAGE.mcp_tool is None
    assert SCHEDULE_MESSAGE.plan_tool == "telegram_plan_schedule_message"
    assert SCHEDULE_MESSAGE.capability is Capability.SEND
    assert SCHEDULE_MESSAGE.cli == ("message", "schedule")


def test_every_remote_write_has_a_budget_to_draw_on() -> None:
    """A write with no limit kind is planned happily and refused on apply.

    ``apply_plan`` looks the operation up in ``_LIMIT_KINDS`` and raises when it
    is missing — after the plan was written, reviewed and approved. Checking it
    from the registry means a new write cannot be added without deciding what
    it costs, in whichever module it is declared.
    """
    missing = [
        op.name for op in REGISTRY.all() if op.is_remote_write and op.name not in _LIMIT_KINDS
    ]
    assert missing == []


# --- the request Telegram is actually handed ---------------------------------


class RecordingClient(FakeClient):
    """A client that records the send rather than performing one."""

    def __init__(self, by_target: dict[Any, Any], *, message_id: int | None = 4242) -> None:
        super().__init__(by_target)
        self.sends: list[dict[str, Any]] = []
        self._message_id = message_id

    async def send_message(self, entity: Any, text: str, **kwargs: Any) -> Any:
        self.sends.append({"entity": entity, "text": text, **kwargs})
        if self._message_id is None:
            return None
        return type("FakeSent", (), {"id": self._message_id})()


def prepared_for(peer_id: int) -> Any:
    from telegram_ai_cli_mcp.apply import _Prepared
    from telegram_ai_cli_mcp.ops.write import Resolved
    from telegram_ai_cli_mcp.safety import PeerKind, PeerRef

    resolved = Resolved(ref=PeerRef(peer_id=peer_id, kind=PeerKind.GROUP, title="Marketing"))
    return _Prepared(limit_target=str(peer_id), peers={"chat": resolved})


def plan_for(params: ScheduleMessageInput) -> Any:
    from telegram_ai_cli_mcp.plans import Plan, PlanState

    return Plan(
        plan_id="0" * 32,
        operation="message.schedule",
        account="main",
        params=params.model_dump(mode="json"),
        preconditions={},
        summary="",
        state=PlanState.PENDING,
        created_at=0.0,
        expires_at=0.0,
    )


@pytest.mark.asyncio
async def test_the_send_carries_the_schedule_and_both_delivery_flags() -> None:
    """The one place the plan turns into a request; asserted on the call itself."""
    from telegram_ai_cli_mcp.apply import _execute

    params = input_for(silent=True, link_preview=False)
    client = RecordingClient({})
    outcome, warnings = await _execute(client, plan_for(params), params, prepared_for(GROUP_ID))

    call = client.sends[0]
    assert call["entity"] == GROUP_ID
    assert call["schedule"] == schedule_date(params)
    assert call["silent"] is True
    assert call["link_preview"] is False
    assert outcome == {
        "scheduled": True,
        "message_id": 4242,
        "send_when_online": False,
        "scheduled_for": params.at.isoformat(),
    }
    assert warnings == []


@pytest.mark.asyncio
async def test_when_online_warns_that_there_may_be_nothing_to_cancel() -> None:
    """Telegram sends at once to somebody who is already online.

    Raised by review: the queue is the reason this operation exists, and this
    is the one mode that can skip it. Saying so at apply time is the difference
    between "check the chat" and "it is waiting, you can still cancel it".
    """
    from telegram_ai_cli_mcp.apply import _execute

    params = input_for(at=None, when_online=True)
    client = RecordingClient({})
    outcome, warnings = await _execute(client, plan_for(params), params, prepared_for(GROUP_ID))

    assert client.sends[0]["schedule"] == WHEN_ONLINE
    assert outcome["send_when_online"] is True
    assert outcome["scheduled_for"] is None
    assert any("already online" in w for w in warnings)


@pytest.mark.asyncio
async def test_a_send_that_answers_without_an_id_is_reported_not_raised() -> None:
    """The queue entry exists either way.

    Raising here would file a schedule that succeeded as an unknown outcome,
    and an unknown outcome is the state a person has to investigate by hand.
    """
    from telegram_ai_cli_mcp.apply import _execute

    params = input_for()
    client = RecordingClient({}, message_id=None)
    outcome, _warnings = await _execute(client, plan_for(params), params, prepared_for(GROUP_ID))

    assert outcome["scheduled"] is True
    assert outcome["message_id"] is None
