"""Archiving and muting: the two writes nobody else can see.

Everything else in `write.py` is visible to somebody — a message arrives, a
read receipt appears, a member is added. These two change only this account's
own copy of Telegram: the chat moves in *this* list, notifications stop on
*these* devices. The other side is not blocked, not left, not banned, and
cannot tell.

That is the whole reason the tests below spend most of their attention on the
summary rather than on the RPC. The blast radius is nearly zero, so the risk is
not the effect — it is a reviewer who reads "mute" as something that reaches the
other person, or reads it as a ban. The summary has to say which, in words, or
the approval is not informed.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from telegram_ai_cli_mcp import db
from telegram_ai_cli_mcp.apply import _LIMIT_KINDS
from telegram_ai_cli_mcp.audit import AuditLog
from telegram_ai_cli_mcp.config import PeerRule, PlansConfig, SafetyConfig, Settings, WritePolicy
from telegram_ai_cli_mcp.context import OperationContext
from telegram_ai_cli_mcp.errors import (
    InvalidInput,
    NotAllowlisted,
    PlanPreconditionFailed,
    ProfileForbidden,
)
from telegram_ai_cli_mcp.ops.quiet import (
    ARCHIVE_CHAT,
    ARCHIVE_FOLDER_ID,
    MAIN_FOLDER_ID,
    MUTE_CHAT,
    MUTE_FOREVER,
    ArchiveChatInput,
    MuteChatInput,
    folder_id_for,
    mute_until,
    plan_archive_chat,
    plan_mute_chat,
    recheck_archive,
    recheck_mute,
)
from telegram_ai_cli_mcp.opspec import Effect
from telegram_ai_cli_mcp.plans import PlanStore
from telegram_ai_cli_mcp.safety import Capability, SafetyKernel

CHANNEL_BASE = -(10**12)
GROUP_ID = CHANNEL_BASE - 4242

HOUR = 3600


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("TGAI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


# --- stand-ins ---------------------------------------------------------------


def group_entity(title: str = "Marketing") -> Any:
    from telethon.tl.types import Channel

    return Channel(id=4242, title=title, photo=None, date=None, megagroup=True)


class FakeClient:
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
        plans=PlanStore(conn, PlansConfig(encrypt_bodies=False)),
        limits=None,  # type: ignore[arg-type]
        audit=AuditLog(tmp_path / "audit.log", settings.audit),
        actor="cli",
        accounts=FakeRegistry(client),  # type: ignore[arg-type]
    )


# --- what Telegram is actually told ------------------------------------------


def test_archiving_moves_a_chat_between_two_folder_ids() -> None:
    assert folder_id_for(ArchiveChatInput(chat=GROUP_ID)) == ARCHIVE_FOLDER_ID
    assert folder_id_for(ArchiveChatInput(chat=GROUP_ID, archived=False)) == MAIN_FOLDER_ID
    assert (ARCHIVE_FOLDER_ID, MAIN_FOLDER_ID) == (1, 0)


def test_unmuting_is_a_mute_until_of_zero() -> None:
    assert mute_until(MuteChatInput(chat=GROUP_ID, muted=False), now=1_000_000) == 0


def test_an_indefinite_mute_is_the_far_future_telegram_uses() -> None:
    assert mute_until(MuteChatInput(chat=GROUP_ID), now=1_000_000) == MUTE_FOREVER
    # A signed 32-bit maximum, which is what the clients write for "for ever".
    assert MUTE_FOREVER == 0x7FFFFFFF


def test_a_bounded_mute_counts_from_when_it_is_applied_not_planned() -> None:
    """The plan records a duration; the deadline is computed at apply time.

    Recording an absolute deadline instead would mean a plan approved in the
    evening and applied in the morning muted the chat for a few minutes rather
    than the eight hours somebody read.
    """
    params = MuteChatInput(chat=GROUP_ID, duration_seconds=8 * HOUR)
    assert mute_until(params, now=1_000_000) == 1_000_000 + 8 * HOUR
    assert mute_until(params, now=2_000_000) == 2_000_000 + 8 * HOUR


def test_a_duration_makes_no_sense_when_unmuting() -> None:
    with pytest.raises(InvalidInput) as caught:
        MUTE_CHAT.parse({"chat": GROUP_ID, "muted": False, "duration_seconds": HOUR})
    assert "duration_seconds" in str(caught.value)


def test_an_absurd_duration_is_refused() -> None:
    for seconds in (5, 400 * 24 * HOUR):
        with pytest.raises(InvalidInput):
            MUTE_CHAT.parse({"chat": GROUP_ID, "duration_seconds": seconds})


# --- the summaries, which are the point --------------------------------------


@pytest.mark.asyncio
async def test_the_archive_summary_says_which_way_and_who_can_see_it(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    client = FakeClient({GROUP_ID: group_entity()})
    ctx = context(tmp_path, conn, client, send_allow=[GROUP_ID])

    plan = await plan_archive_chat(ctx, ArchiveChatInput(chat=GROUP_ID))

    assert plan.operation == "chat.archive"
    assert "Marketing" in plan.summary
    assert "Archived" in plan.summary
    # The line that separates this from a ban, a block or a leave.
    assert "only" in plan.summary.lower()
    assert "ban" in plan.summary.lower()
    assert plan.preconditions["archived"] is True


@pytest.mark.asyncio
async def test_unarchiving_reads_as_the_other_direction(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    client = FakeClient({GROUP_ID: group_entity()})
    ctx = context(tmp_path, conn, client, send_allow=[GROUP_ID])

    plan = await plan_archive_chat(ctx, ArchiveChatInput(chat=GROUP_ID, archived=False))

    assert "back" in plan.summary.lower()
    assert plan.preconditions["archived"] is False


@pytest.mark.asyncio
async def test_a_bounded_mute_states_the_duration_in_words(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """8 hours, not 28800. A reviewer cannot judge a number of seconds."""
    client = FakeClient({GROUP_ID: group_entity()})
    ctx = context(tmp_path, conn, client, send_allow=[GROUP_ID])

    plan = await plan_mute_chat(ctx, MuteChatInput(chat=GROUP_ID, duration_seconds=8 * HOUR))

    assert "8 hours" in plan.summary
    assert "only" in plan.summary.lower()
    assert plan.preconditions["duration_seconds"] == 8 * HOUR
    assert plan.preconditions["muted"] is True


@pytest.mark.asyncio
async def test_a_duration_is_stated_in_full_not_rounded_down(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """Raised by review: two units truncate, and this number is the decision.

    25h59m59s rendered as "1 day 1 hour" understates a mute by nearly an hour
    on the one line somebody reads before approving it.
    """
    client = FakeClient({GROUP_ID: group_entity()})
    ctx = context(tmp_path, conn, client, send_allow=[GROUP_ID])

    plan = await plan_mute_chat(ctx, MuteChatInput(chat=GROUP_ID, duration_seconds=93_599))

    assert "1 day 1 hour 59 minutes 59 seconds" in plan.summary


@pytest.mark.asyncio
async def test_an_indefinite_mute_says_so_rather_than_naming_a_date(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """A mute "until 2038" is a date nobody means; "until you unmute it" is the truth."""
    client = FakeClient({GROUP_ID: group_entity()})
    ctx = context(tmp_path, conn, client, send_allow=[GROUP_ID])

    plan = await plan_mute_chat(ctx, MuteChatInput(chat=GROUP_ID))

    assert "2038" not in plan.summary
    assert "unmute" in plan.summary.lower()
    assert plan.preconditions["duration_seconds"] is None


@pytest.mark.asyncio
async def test_unmuting_says_notifications_come_back(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    client = FakeClient({GROUP_ID: group_entity()})
    ctx = context(tmp_path, conn, client, send_allow=[GROUP_ID])

    plan = await plan_mute_chat(ctx, MuteChatInput(chat=GROUP_ID, muted=False))

    assert "Unmute" in plan.summary
    assert plan.preconditions["muted"] is False


@pytest.mark.asyncio
async def test_a_hostile_chat_title_cannot_redraw_either_summary(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    client = FakeClient({GROUP_ID: group_entity("Marketing\r\x1b[2Kbanned everyone")})
    ctx = context(tmp_path, conn, client, send_allow=[GROUP_ID])

    for plan in (
        await plan_archive_chat(ctx, ArchiveChatInput(chat=GROUP_ID)),
        await plan_mute_chat(ctx, MuteChatInput(chat=GROUP_ID)),
    ):
        assert "\r" not in plan.summary
        assert "\x1b" not in plan.summary


# --- policy -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readonly_refuses_before_the_network_is_touched(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    client = FakeClient({GROUP_ID: group_entity()})
    ctx = context(tmp_path, conn, client, profile="readonly", send_allow=[GROUP_ID])

    for planner, params in (
        (plan_archive_chat, ArchiveChatInput(chat=GROUP_ID)),
        (plan_mute_chat, MuteChatInput(chat=GROUP_ID)),
    ):
        with pytest.raises(ProfileForbidden):
            await planner(ctx, params)
    assert client.resolved == []


@pytest.mark.asyncio
async def test_a_chat_outside_the_policy_is_refused(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    client = FakeClient({GROUP_ID: group_entity()})
    ctx = context(tmp_path, conn, client, send_allow=[])

    with pytest.raises(NotAllowlisted):
        await plan_mute_chat(ctx, MuteChatInput(chat=GROUP_ID))
    assert not ctx.plans.list()


# --- what apply re-checks -----------------------------------------------------


def test_a_mute_that_no_longer_matches_the_reviewed_one_is_refused() -> None:
    params = MuteChatInput(chat=GROUP_ID, duration_seconds=8 * HOUR)
    for stale in (
        {"muted": True, "duration_seconds": 24 * HOUR},
        {"muted": False, "duration_seconds": 8 * HOUR},
        {"muted": True, "duration_seconds": None},
    ):
        with pytest.raises(PlanPreconditionFailed):
            recheck_mute(stale, params)
    recheck_mute({"muted": True, "duration_seconds": 8 * HOUR}, params)


def test_an_archive_direction_that_flipped_since_review_is_refused() -> None:
    params = ArchiveChatInput(chat=GROUP_ID)
    with pytest.raises(PlanPreconditionFailed):
        recheck_archive({"archived": False}, params)
    recheck_archive({"archived": True}, params)


def test_the_deadline_is_still_in_the_future_when_it_is_applied() -> None:
    """Sanity on the arithmetic itself, at a real clock rather than a fixture."""
    params = MuteChatInput(chat=GROUP_ID, duration_seconds=HOUR)
    assert mute_until(params, now=time.time()) > time.time()


# --- how the operations are declared -----------------------------------------


@pytest.mark.parametrize("op", [ARCHIVE_CHAT, MUTE_CHAT], ids=lambda o: o.name)
def test_both_are_planned_writes_with_no_tool_of_their_own(op: Any) -> None:
    assert op.effect is Effect.REMOTE_WRITE
    assert op.mcp_tool is None
    assert op.plan_tool and op.plan_tool.startswith("telegram_plan_")
    assert op.capability is Capability.SEND
    assert op.name in _LIMIT_KINDS


# --- the requests Telegram is actually handed --------------------------------


class RecordingClient(FakeClient):
    """Records the TL requests instead of sending them."""

    def __init__(self, by_target: dict[Any, Any]) -> None:
        super().__init__(by_target)
        self.requests: list[Any] = []

    async def get_input_entity(self, peer: Any) -> Any:
        from telethon.tl.types import InputPeerChannel

        return InputPeerChannel(channel_id=4242, access_hash=0)

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        return None


def prepared_for(peer_id: int) -> Any:
    from telegram_ai_cli_mcp.apply import _Prepared
    from telegram_ai_cli_mcp.ops.write import Resolved
    from telegram_ai_cli_mcp.safety import PeerKind, PeerRef

    resolved = Resolved(ref=PeerRef(peer_id=peer_id, kind=PeerKind.GROUP, title="Marketing"))
    return _Prepared(limit_target=str(peer_id), peers={"chat": resolved})


def plan_for(operation: str, params: Any) -> Any:
    from telegram_ai_cli_mcp.plans import Plan, PlanState

    return Plan(
        plan_id="0" * 32,
        operation=operation,
        account="main",
        params=params.model_dump(mode="json"),
        preconditions={},
        summary="",
        state=PlanState.PENDING,
        created_at=0.0,
        expires_at=0.0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("archived", "folder"), [(True, ARCHIVE_FOLDER_ID), (False, MAIN_FOLDER_ID)]
)
async def test_archiving_moves_the_peer_into_the_folder_it_said(
    archived: bool, folder: int
) -> None:
    from telegram_ai_cli_mcp.apply import _execute

    params = ArchiveChatInput(chat=GROUP_ID, archived=archived)
    client = RecordingClient({})
    outcome, _warnings = await _execute(
        client, plan_for("chat.archive", params), params, prepared_for(GROUP_ID)
    )

    request = client.requests[0]
    assert request.folder_peers[0].folder_id == folder
    assert outcome == {"archived": archived}


@pytest.mark.asyncio
async def test_muting_sends_a_deadline_and_unmuting_sends_zero() -> None:
    """Zero is a value Telegram reads as "not muted", not an absent field."""
    from telegram_ai_cli_mcp.apply import _execute

    client = RecordingClient({})
    muting = MuteChatInput(chat=GROUP_ID, duration_seconds=HOUR)
    outcome, _warnings = await _execute(
        client, plan_for("chat.mute", muting), muting, prepared_for(GROUP_ID)
    )
    assert client.requests[0].settings.mute_until > time.time()
    assert outcome["muted"] is True

    unmuting = MuteChatInput(chat=GROUP_ID, muted=False)
    outcome, _warnings = await _execute(
        client, plan_for("chat.mute", unmuting), unmuting, prepared_for(GROUP_ID)
    )
    assert client.requests[1].settings.mute_until == 0
    assert outcome == {"muted": False, "mute_until": None}


@pytest.mark.asyncio
async def test_an_indefinite_mute_sends_the_sentinel() -> None:
    from telegram_ai_cli_mcp.apply import _execute

    params = MuteChatInput(chat=GROUP_ID)
    client = RecordingClient({})
    outcome, _warnings = await _execute(
        client, plan_for("chat.mute", params), params, prepared_for(GROUP_ID)
    )

    assert client.requests[0].settings.mute_until == MUTE_FOREVER
    assert outcome["mute_until"] == MUTE_FOREVER
