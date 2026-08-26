"""Event-driven waiting: one wake-up per burst, a ceiling, and a hard filter.

Every test here runs on a **fake clock over a scripted source**. Not a
convenience: a debounce test written with real `sleep` asserts on the scheduler
rather than on the rule, and it fails on a loaded CI runner for reasons that
have nothing to do with the code. `collect_burst` takes its clock and its
source as arguments precisely so this file can advance time in one step and
assert exact numbers.

Three properties are worth the file on their own:

**Four fast replies are one answer.** The whole point of the operation is that
an agent is woken once and reads the burst, instead of four wake-ups each
paying for the system prompt again.

**The ceiling is absolute.** A stream that never goes quiet must still return,
or an MCP client waits forever on a call that has no reason to end.

**A refused peer leaves no trace.** Not merely absent from the payload: it must
not start a burst, must not extend one, and must not turn a silent wait into a
"something happened" answer.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import ValidationError

from telegram_ai_cli_mcp.audit import AuditLog
from telegram_ai_cli_mcp.config import Settings
from telegram_ai_cli_mcp.context import OperationContext
from telegram_ai_cli_mcp.errors import InvalidInput
from telegram_ai_cli_mcp.ops.watch import (
    MAX_DEBOUNCE_SECONDS,
    MAX_WATCH_SECONDS,
    WatchInput,
    admits,
    collect_burst,
    filtering_source,
    handle_watch,
)
from telegram_ai_cli_mcp.opspec import REGISTRY, Effect
from telegram_ai_cli_mcp.safety import Capability, PeerKind, PeerRef, SafetyKernel

# Deliberately small, obviously fake ids — see tests/test_no_private_data.py.
GROUP = PeerRef(peer_id=-4242, kind=PeerKind.GROUP, title="A group")
OTHER_GROUP = PeerRef(peer_id=-4343, kind=PeerKind.GROUP, title="Another group")
USER = PeerRef(peer_id=555, kind=PeerKind.USER, username="Someone")
SAVED = PeerRef(peer_id=666, kind=PeerKind.SELF, title="Saved Messages")
SERVICE = PeerRef(peer_id=777000, kind=PeerKind.SERVICE, title="Telegram")


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """No ``TGAI_`` variable from the developer's shell may steer a decision."""
    for key in list(os.environ):
        if key.startswith("TGAI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


class Clock:
    """Monotonic time the test moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Script:
    """A source of scripted arrivals. Nothing sleeps; the clock jumps.

    ``await source(window)`` behaves exactly like the real queue reader: it
    returns the next arrival if one falls inside ``window``, otherwise it
    consumes the whole window and returns ``None``.
    """

    def __init__(self, clock: Clock, arrivals: list[tuple[float, Any]]) -> None:
        self.clock = clock
        self.pending = sorted(arrivals, key=lambda item: item[0])
        self.windows: list[float] = []

    async def __call__(self, window: float) -> Any | None:
        self.windows.append(window)
        horizon = self.clock.now + window
        if self.pending and self.pending[0][0] <= horizon:
            at, payload = self.pending.pop(0)
            self.clock.now = max(self.clock.now, at)
            return payload
        self.clock.now = horizon
        return None


def burst(
    arrivals: list[tuple[float, Any]],
    *,
    timeout: float = 60.0,
    debounce: float = 2.0,
    limit: int = 50,
) -> tuple[list[Any], float, str, Script]:
    clock = Clock()
    source = Script(clock, arrivals)
    events, waited, reason = _run(
        collect_burst(
            source,
            timeout=timeout,
            debounce=debounce,
            limit=limit,
            monotonic=clock,
        )
    )
    return events, waited, reason, source


def _run(coro):
    return asyncio.run(coro)


# --- the debounce ----------------------------------------------------------


def test_four_fast_replies_wake_the_agent_once_and_come_back_together() -> None:
    """The requirement the operation exists for.

    Four replies inside the debounce window are one answer, not four. The
    window restarts on each arrival, so the burst ends when the chat goes
    quiet rather than a fixed interval after the first message.
    """
    events, waited, reason, source = burst(
        [(1.0, "a"), (1.2, "b"), (1.4, "c"), (1.6, "d")], debounce=2.0
    )

    assert events == ["a", "b", "c", "d"]
    assert reason == "quiet"
    # 1.6 (last arrival) + 2.0 (the window it did not use) — the debounce was
    # restarted by each reply rather than counted from the first.
    assert waited == pytest.approx(3.6)
    # Five reads: four that returned a message and one that timed out. One
    # call returns the whole burst.
    assert len(source.windows) == 5


def test_the_debounce_window_never_outlives_the_overall_timeout() -> None:
    """A late arrival must not extend the wait past the ceiling."""
    events, waited, reason, _ = burst([(4.5, "a")], timeout=5.0, debounce=30.0)

    assert events == ["a"]
    assert waited == pytest.approx(5.0)
    assert reason == "timeout"


def test_zero_debounce_returns_the_first_arrival_alone() -> None:
    events, _, reason, _ = burst([(1.0, "a"), (1.1, "b")], debounce=0.0)

    assert events == ["a"]
    assert reason == "quiet"


# --- the ceiling -----------------------------------------------------------


def test_silence_returns_an_empty_result_and_says_how_long_it_waited() -> None:
    """Not an error. "Nothing happened for 30 seconds" is an answer."""
    events, waited, reason, _ = burst([], timeout=30.0)

    assert events == []
    assert reason == "timeout"
    assert waited == pytest.approx(30.0)


def test_a_stream_that_never_goes_quiet_still_returns_at_the_ceiling() -> None:
    """Otherwise an MCP client hangs on a call with no reason to end."""
    arrivals = [(0.5 * (n + 1), n) for n in range(200)]
    events, waited, reason, _ = burst(arrivals, timeout=5.0, debounce=2.0, limit=500)

    assert waited == pytest.approx(5.0)
    assert reason == "timeout"
    assert events, "a busy chat should still hand back what arrived"


def test_the_limit_ends_the_burst_and_says_so() -> None:
    arrivals = [(0.1 * (n + 1), n) for n in range(10)]
    events, _, reason, _ = burst(arrivals, limit=3)

    assert events == [0, 1, 2]
    assert reason == "limit"


# --- the filter ------------------------------------------------------------


def filtered_burst(
    arrivals: list[tuple[float, Any]],
    marker: str,
    *,
    timeout: float = 60.0,
    debounce: float = 2.0,
) -> tuple[list[Any], float, str]:
    clock = Clock()
    raw = Script(clock, arrivals)

    async def screen(item: Any) -> Any | None:
        return item if item == marker else None

    return _run(
        collect_burst(
            filtering_source(raw, screen, monotonic=clock),
            timeout=timeout,
            debounce=debounce,
            limit=50,
            monotonic=clock,
        )
    )


def test_a_refused_message_neither_starts_a_burst_nor_extends_one() -> None:
    events, waited, reason = filtered_burst(
        [(1.0, "denied"), (2.0, "denied"), (3.0, "allowed")], "allowed"
    )

    assert events == ["allowed"]
    assert reason == "quiet"
    # The burst began at 3.0, not at 1.0: had the refused messages counted,
    # the wait would have ended at 3.0 with three events in it.
    assert waited == pytest.approx(5.0)


def test_traffic_in_refused_chats_reads_as_silence() -> None:
    """The strict form of the rule: not even the *fact* of an event escapes."""
    arrivals = [(float(n + 1), "denied") for n in range(5)]
    events, waited, reason = filtered_burst(arrivals, "allowed", timeout=10.0)

    assert events == []
    assert reason == "timeout"
    assert waited == pytest.approx(10.0)


# --- what may be watched at all --------------------------------------------


def kernel(**overrides) -> SafetyKernel:
    return SafetyKernel(Settings(**overrides))


@pytest.mark.parametrize("peer", [SERVICE, SAVED])
def test_the_hard_floor_applies_to_watching(peer: PeerRef) -> None:
    """Service Notifications and Saved Messages, even when named explicitly."""
    assert admits(kernel(), peer, allowed={peer.peer_id}) is False


def test_a_group_is_watchable_by_default() -> None:
    assert admits(kernel(), GROUP, allowed=None) is True


def test_a_private_chat_is_not_watchable_until_it_is_allowlisted() -> None:
    """The same fail-closed rule reading a DM obeys — watching is reading."""
    assert admits(kernel(), USER, allowed=None) is False
    assert admits(kernel(safety={"read": {"dms": {"allow": [555]}}}), USER, allowed=None) is True


def test_a_denied_group_is_not_watchable() -> None:
    denied = kernel(safety={"read": {"chats": {"deny": [-4242]}}})
    assert admits(denied, GROUP, allowed=None) is False
    assert admits(denied, OTHER_GROUP, allowed=None) is True


def test_naming_chats_narrows_but_never_widens() -> None:
    """An explicit list is an intersection with the policy, not a substitute."""
    assert admits(kernel(), OTHER_GROUP, allowed={GROUP.peer_id}) is False
    assert admits(kernel(), GROUP, allowed={GROUP.peer_id}) is True
    # Naming a private chat does not grant it.
    assert admits(kernel(), USER, allowed={USER.peer_id}) is False


# --- the handler, against a fake client ------------------------------------
#
# The pure core above proves the *rule*. These prove the wiring: that the
# subscription exists before anything else awaits, that it is taken down again,
# and that the ceiling covers the setup rather than only the waiting.


@dataclass
class FakeMessage:
    id: int
    message: str
    out: bool = False
    date: Any = None


@dataclass
class FakeEvent:
    chat: Any
    message: Any


@dataclass
class FakeAccount:
    client: Any


class FakeAccountRow:
    def __init__(self, label: str) -> None:
        self.label = label


class FakeRegistry:
    def __init__(self, client: Any, label: str = "main") -> None:
        self._client = client
        self._label = label

    def list_accounts(self) -> list[Any]:
        return [FakeAccountRow(self._label)]

    def get(self, _label: str) -> Any:
        return None

    def load_account(self, _label: str) -> FakeAccount:
        return FakeAccount(client=self._client)


class FakeClient:
    """Enough client to drive the handler: entities, and update dispatch.

    ``log`` records the order of the calls that matter, which is how "the
    subscription is in place before a chat is resolved" is asserted rather than
    read off the source.
    """

    def __init__(
        self,
        entities: dict[str, Any] | None = None,
        *,
        on_subscribe: list[Any] | None = None,
        on_resolve: Any = None,
    ) -> None:
        self.log: list[str] = []
        self.handlers: list[Any] = []
        self._entities = entities or {}
        self._on_subscribe = on_subscribe or []
        self._on_resolve = on_resolve

    def is_connected(self) -> bool:
        return True

    async def is_user_authorized(self) -> bool:
        return True

    def add_event_handler(self, callback: Any, event: Any = None) -> None:
        self.log.append("add_event_handler")
        self.handlers.append((callback, event))
        for pending in self._on_subscribe:
            # Telethon dispatches from its own task; so does this. The
            # collector's first await is what lets them run.
            asyncio.get_running_loop().create_task(callback(pending))

    def remove_event_handler(self, callback: Any, event: Any = None) -> None:
        self.log.append("remove_event_handler")
        self.handlers = [entry for entry in self.handlers if entry[0] is not callback]

    async def get_entity(self, reference: Any) -> Any:
        self.log.append("get_entity")
        if self._on_resolve is not None:
            await self._on_resolve(self)
        return self._entities[str(reference)]

    async def deliver(self, event: Any) -> None:
        for callback, _filter in self.handlers:
            await callback(event)


def telegram_group(number: int) -> Any:
    from telethon.tl.types import Channel

    return Channel(id=number, title="A group", photo=None, date=None, megagroup=True)


def telegram_user(number: int) -> Any:
    from telethon.tl.types import User

    return User(id=number, first_name="Someone")


def context(tmp_path, client: Any, **overrides: Any) -> OperationContext:
    settings = Settings(**overrides)
    return OperationContext(
        settings=settings,
        safety=SafetyKernel(settings),
        plans=None,  # type: ignore[arg-type] - unused by a read handler
        limits=None,  # type: ignore[arg-type]
        audit=AuditLog(tmp_path / "audit.log", settings.audit),
        actor="cli",
        accounts=FakeRegistry(client),  # type: ignore[arg-type]
    )


def watched(envelope) -> list[Any]:
    return envelope.data["events"]


async def test_the_subscription_is_in_place_before_a_chat_is_resolved(tmp_path) -> None:
    """Resolving a chat is a round trip, and a message can land inside it.

    Registering afterwards means that message is dispatched with nobody
    listening — and the caller then waits out the whole timeout for something
    that had already arrived.
    """
    group = telegram_group(4242)
    arrived = FakeEvent(chat=group, message=FakeMessage(id=7, message="during resolution"))

    async def deliver_midway(client: FakeClient) -> None:
        await client.deliver(arrived)

    client = FakeClient({"@team": group}, on_resolve=deliver_midway)
    envelope = await handle_watch(
        context(tmp_path, client),
        WatchInput(chats=["@team"], timeout_sec=5.0, debounce_sec=0.0),
    )

    assert client.log[:2] == ["add_event_handler", "get_entity"]
    assert [row["message"]["id"] for row in watched(envelope)] == [7]
    assert client.log[-1] == "remove_event_handler"
    assert client.handlers == []


async def test_the_subscription_is_removed_even_when_the_wait_fails(tmp_path) -> None:
    """A handler left behind outlives the operation and keeps filling a queue."""
    client = FakeClient({})  # resolving "@missing" raises KeyError

    with pytest.raises(KeyError):
        await handle_watch(
            context(tmp_path, client),
            WatchInput(chats=["@missing"], timeout_sec=5.0),
        )

    assert client.log.count("remove_event_handler") == 1
    assert client.handlers == []


async def test_a_message_from_a_refused_chat_is_not_reported(tmp_path) -> None:
    """The policy runs over live events, not only over named chats."""
    private = FakeEvent(chat=telegram_user(555), message=FakeMessage(id=1, message="private"))
    group = FakeEvent(chat=telegram_group(4242), message=FakeMessage(id=2, message="group"))

    client = FakeClient(on_subscribe=[private, group])
    envelope = await handle_watch(
        context(tmp_path, client),
        WatchInput(timeout_sec=5.0, debounce_sec=0.0),
    )

    assert [row["message"]["id"] for row in watched(envelope)] == [2]
    assert envelope.data["watched"]["scope"] == "permitted"


async def test_the_ceiling_covers_the_setup_not_only_the_waiting(tmp_path, monkeypatch) -> None:
    """`timeout_sec` bounds the *call*, which is what an MCP client cannot abandon.

    Connecting and resolving named chats are round trips. A ceiling measured
    from the first read would let a slow setup push the call past it, and the
    number would then bound the waiting rather than the call.
    """
    clock = Clock()

    class FakeTime:
        monotonic = staticmethod(clock)

    monkeypatch.setattr("telegram_ai_cli_mcp.ops.watch.time", FakeTime)

    group = telegram_group(4242)
    arrived = FakeEvent(chat=group, message=FakeMessage(id=9, message="too late"))

    async def slow_resolution(client: FakeClient) -> None:
        clock.now += 4.0
        await client.deliver(arrived)

    client = FakeClient({"@team": group}, on_resolve=slow_resolution)
    envelope = await handle_watch(
        context(tmp_path, client),
        WatchInput(chats=["@team"], timeout_sec=3.0),
    )

    assert watched(envelope) == []
    assert envelope.data["stopped_because"] == "timeout"
    assert envelope.data["waited_sec"] == pytest.approx(4.0)


# --- the published contract ------------------------------------------------


def test_the_wait_is_bounded_by_the_schema_itself() -> None:
    """A caller cannot ask for an unbounded wait; the cap is published."""
    schema = WatchInput.model_json_schema()
    assert schema["properties"]["timeout_sec"]["maximum"] == MAX_WATCH_SECONDS
    assert schema["properties"]["debounce_sec"]["maximum"] == MAX_DEBOUNCE_SECONDS


def test_an_over_long_wait_is_refused_rather_than_clamped() -> None:
    op = REGISTRY.by_name("watch.wait")
    with pytest.raises(InvalidInput, match="timeout_sec"):
        op.parse({"timeout_sec": MAX_WATCH_SECONDS + 1})


def test_a_comma_separated_chat_list_is_accepted_for_the_terminal() -> None:
    """The form the docs taught, in both shapes it can now arrive in.

    `--chats @a --chats @b` reaches the model as a list, so the comma form now
    arrives as a one-element list rather than as a bare string — and it has to
    keep splitting there, or repetition would have broken the older form.
    """
    assert WatchInput.model_validate({"chats": "-4242, @example"}).chats == ["-4242", "@example"]
    assert WatchInput.model_validate({"chats": ["-4242, @example"]}).chats == ["-4242", "@example"]
    assert WatchInput.model_validate({"chats": ["-4242", "@example"]}).chats == [
        "-4242",
        "@example",
    ]
    assert WatchInput.model_validate({"chats": ["-4242"]}).chats == ["-4242"]
    assert WatchInput.model_validate({}).chats is None


@pytest.mark.parametrize("given", ["", " ", ",", [], [""], [",", " "]])
def test_a_chats_value_that_names_nothing_is_refused(given: Any) -> None:
    """An empty narrowing must not widen.

    `chats=None` means every chat the policy permits, so reading an empty value
    as an omission would take a scope somebody typed wrong and replace it with
    the largest one this account allows. The policy still applies either way —
    this is about honouring what was asked for, not about what is reachable.
    """
    with pytest.raises(ValidationError, match="names no chat"):
        WatchInput.model_validate({"chats": given})


def test_omitting_chats_still_means_every_permitted_chat() -> None:
    """The distinction the refusal above depends on: absent is not empty."""
    assert WatchInput.model_validate({}).chats is None
    assert WatchInput.model_validate({"chats": None}).chats is None


def test_watching_is_a_read_that_declares_its_capability() -> None:
    op = REGISTRY.by_name("watch.wait")

    assert op.effect is Effect.READ
    assert op.capability is Capability.READ_CHAT
    assert op.mcp_tool == "telegram_watch"
    assert op.plan_tool is None
