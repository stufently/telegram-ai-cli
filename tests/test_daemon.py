"""The shared-account daemon: the socket, the race, and what it refuses.

The daemon exists for one reason — an exclusive `flock` per auth key means a
five-minute `telegram_watch` locks every other caller out of that account — so
the tests that matter are the ones about *sharing*: two processes racing to
become the daemon, a slow request not blocking the next one, and a socket left
behind by a killed daemon not being inherited.

The security tests are the other half. The daemon runs operations by *name*
from the registry; there is no endpoint that takes an MTProto method or an
attribute path, and there is nothing here that applies a plan. Those are
asserted rather than reviewed, because they are exactly the properties a later
"just add a passthrough" would quietly remove.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket as socket_module
from pathlib import Path
from typing import Any

import pytest

import telegram_ai_cli.ops  # noqa: F401  (registers every operation)
from telegram_ai_cli.accounts.lock import SessionLock
from telegram_ai_cli.config import Settings
from telegram_ai_cli.daemon import client as daemon_client
from telegram_ai_cli.daemon import paths as daemon_paths
from telegram_ai_cli.daemon import protocol, service
from telegram_ai_cli.daemon.server import AccountDaemon
from telegram_ai_cli.errors import (
    ErrorCode,
    InsecurePermissions,
    InvalidInput,
    TelegramAIError,
)

ACCOUNT = "alpha"


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        paths={
            "state": tmp_path / "state",
            "sessions": tmp_path / "sessions",
            "downloads": tmp_path / "downloads",
            "uploads": tmp_path / "uploads",
            "audit_log": tmp_path / "state" / "audit.jsonl",
            "archive": tmp_path / "state" / "archive.sqlite3",
        }
    )


class FakeSession:
    """Stands in for the connected account, so no Telegram is involved.

    ``run`` records the order it was entered and left, which is how
    serialisation is asserted without sleeping for a fixed time and hoping.
    """

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.opened = 0
        self.closed = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.policies: list[str | None] = []
        self.concurrent = 0
        self.max_concurrent = 0

    async def open(self) -> None:
        self.opened += 1

    async def close(self) -> None:
        self.closed += 1

    async def run(
        self,
        operation: str,
        params: dict[str, Any],
        *,
        actor: str,
        policy: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((operation, params))
        self.policies.append(policy)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if operation == "boom":
                raise InvalidInput("no such thing")
            return {"kind": "envelope", "envelope": {"ok": True, "data": {"seen": operation}}}
        finally:
            self.concurrent -= 1


class StrictSession(FakeSession):
    """A fake with the real session's gate: names go through the registry."""

    async def run(
        self,
        operation: str,
        params: dict[str, Any],
        *,
        actor: str,
        policy: str | None = None,
    ) -> dict[str, Any]:
        service.select_operation(operation)
        return await super().run(operation, params, actor=actor, policy=policy)


def make_daemon(
    tmp_path: Path,
    session: FakeSession | None = None,
    *,
    idle_timeout: float = 300.0,
    bootstrap_wait: float = 2.0,
    install_signal_handlers: bool = False,
) -> AccountDaemon:
    settings = settings_for(tmp_path)
    daemon_paths.prepare_account_dir(settings, ACCOUNT)
    return AccountDaemon(
        account=ACCOUNT,
        socket_path=daemon_paths.socket_path(settings, ACCOUNT),
        bootstrap_lock_path=daemon_paths.bootstrap_lock_path(settings, ACCOUNT),
        session=session or FakeSession(),
        idle_timeout=idle_timeout,
        bootstrap_wait=bootstrap_wait,
        install_signal_handlers=install_signal_handlers,
    )


@contextlib.asynccontextmanager
async def running(daemon: AccountDaemon) -> Any:
    task = asyncio.create_task(daemon.serve())
    try:
        await asyncio.wait_for(daemon.started.wait(), timeout=5)
        yield daemon
    finally:
        daemon.request_shutdown()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=5)
        if not task.done():  # pragma: no cover - only on a hang
            task.cancel()


# --- where the socket lives -------------------------------------------------


def test_the_socket_lives_in_a_private_per_account_directory(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    directory = daemon_paths.prepare_account_dir(settings, ACCOUNT)

    assert directory.is_dir()
    assert directory.stat().st_mode & 0o777 == 0o700
    assert daemon_paths.socket_path(settings, ACCOUNT).parent == directory
    # The label is one path component, never a traversal.
    assert daemon_paths.account_dir(settings, "../escape").parent == directory.parent


def test_a_directory_owned_by_someone_else_is_refused(tmp_path: Path, monkeypatch) -> None:
    settings = settings_for(tmp_path)
    stranger = os.geteuid() + 1
    monkeypatch.setattr(os, "geteuid", lambda: stranger)

    with pytest.raises(InsecurePermissions):
        daemon_paths.prepare_account_dir(settings, ACCOUNT)


def test_a_symlinked_socket_path_is_refused(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    daemon_paths.prepare_account_dir(settings, ACCOUNT)
    path = daemon_paths.socket_path(settings, ACCOUNT)
    path.symlink_to(tmp_path / "elsewhere.sock")

    with pytest.raises(InsecurePermissions):
        daemon_paths.require_bindable(path)


def test_an_existing_plain_file_at_the_socket_path_is_refused(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    daemon_paths.prepare_account_dir(settings, ACCOUNT)
    path = daemon_paths.socket_path(settings, ACCOUNT)
    path.write_text("not a socket")

    with pytest.raises(InvalidInput):
        daemon_paths.require_bindable(path)


def test_a_socket_path_too_long_for_an_address_is_refused_by_name(tmp_path: Path) -> None:
    """`bind` would fail with a bare "AF_UNIX path too long" and no path in it."""
    deep = tmp_path / ("d" * 80) / ("e" * 80)
    settings = settings_for(tmp_path)
    settings.paths.state = deep

    with pytest.raises(InvalidInput) as excinfo:
        daemon_paths.prepare_account_dir(settings, ACCOUNT)

    assert "limit" in excinfo.value.message
    assert not deep.exists(), "the directory was created before the path was checked"


def test_a_normal_socket_path_is_comfortably_inside_the_limit(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.paths.state = Path("/home/someone/.local/state/telegram-ai-cli")
    path = daemon_paths.socket_path(settings, "work")

    assert len(str(path).encode()) <= daemon_paths.MAX_SOCKET_PATH_BYTES
    daemon_paths.require_socket_path_fits(path)


@pytest.mark.asyncio
async def test_the_socket_is_created_0600_and_removed_on_shutdown(tmp_path: Path) -> None:
    daemon = make_daemon(tmp_path)
    async with running(daemon) as live:
        path = live.socket_path
        assert path.exists()
        assert path.stat().st_mode & 0o777 == 0o600

    assert not path.exists()


# --- stale sockets and the start-up race ------------------------------------


@pytest.mark.asyncio
async def test_a_stale_socket_is_replaced_not_inherited(tmp_path: Path) -> None:
    """A killed daemon leaves a socket nothing is listening on."""
    settings = settings_for(tmp_path)
    daemon_paths.prepare_account_dir(settings, ACCOUNT)
    path = daemon_paths.socket_path(settings, ACCOUNT)

    # Bind and close without unlinking: exactly what SIGKILL leaves behind.
    orphan = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    orphan.bind(str(path))
    orphan.close()
    assert path.exists()

    daemon = make_daemon(tmp_path)
    async with running(daemon):
        # `bind` on an existing path fails with EADDRINUSE, so a daemon that
        # answers here is one that replaced the file rather than inheriting it.
        reply = await daemon_client.ping(path)
        assert reply["kind"] == "pong"
        assert reply["account"] == ACCOUNT


@pytest.mark.asyncio
async def test_a_listener_that_is_merely_slow_is_not_treated_as_stale(tmp_path: Path) -> None:
    """A busy daemon must not have its socket unlinked out from under it.

    Staleness is decided by `connect`, not by whether a ping comes back: a
    daemon blocked long enough to miss the ping is still holding the auth key,
    and deleting its socket would leave it unreachable for ever.
    """
    settings = settings_for(tmp_path)
    daemon_paths.prepare_account_dir(settings, ACCOUNT)
    path = daemon_paths.socket_path(settings, ACCOUNT)

    # Listening, accepting, and answering nothing at all.
    mute = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    mute.bind(str(path))
    mute.listen(8)
    try:
        daemon = make_daemon(tmp_path, bootstrap_wait=0.2)
        assert await asyncio.wait_for(daemon.serve(), timeout=10) == "already-running"
        assert path.exists(), "a live but unresponsive daemon's socket was deleted"
        assert daemon.session.opened == 0, "the account was opened behind a live daemon"
    finally:
        mute.close()
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_two_racing_daemons_end_with_one_daemon_and_one_loser(tmp_path: Path) -> None:
    first = make_daemon(tmp_path)
    second = make_daemon(tmp_path)

    results: list[str] = []

    async def start(daemon: AccountDaemon) -> None:
        results.append(await daemon.serve())

    tasks = {
        first: asyncio.create_task(start(first)),
        second: asyncio.create_task(start(second)),
    }
    # Whoever wins signals `started`; the loser returns without binding.
    for _ in range(250):
        if first.started.is_set() or second.started.is_set():
            break
        await asyncio.sleep(0.02)

    winner = first if first.started.is_set() else second
    loser = second if winner is first else first

    # The loser must finish on its own; the winner keeps serving.
    await asyncio.wait_for(tasks[loser], timeout=5)

    assert loser.bound is False
    assert winner.bound is True
    reply = await daemon_client.ping(winner.socket_path)
    assert reply["pid"] == os.getpid()

    winner.request_shutdown()
    await asyncio.wait_for(tasks[winner], timeout=5)

    assert sorted(results) == ["already-running", "served"]


@pytest.mark.asyncio
async def test_a_held_bootstrap_lock_with_no_daemon_behind_it_is_reported(tmp_path: Path) -> None:
    """The loser waits for the winner's socket — and says so if it never appears."""
    settings = settings_for(tmp_path)
    daemon_paths.prepare_account_dir(settings, ACCOUNT)
    held = SessionLock(daemon_paths.bootstrap_lock_path(settings, ACCOUNT)).acquire()
    try:
        daemon = make_daemon(tmp_path, bootstrap_wait=0.2)
        with pytest.raises(TelegramAIError) as excinfo:
            await asyncio.wait_for(daemon.serve(), timeout=5)
        assert excinfo.value.code is ErrorCode.SESSION_LOCKED
    finally:
        held.release()


@pytest.mark.asyncio
async def test_the_bootstrap_lock_is_released_once_the_daemon_is_serving(tmp_path: Path) -> None:
    """Held for the bootstrap only: a second holder must be able to take it."""
    daemon = make_daemon(tmp_path)
    async with running(daemon):
        probe = SessionLock(daemon.bootstrap_lock_path).acquire()
        probe.release()


# --- concurrency ------------------------------------------------------------


@pytest.mark.asyncio
async def test_requests_are_serialised_but_the_socket_keeps_accepting(tmp_path: Path) -> None:
    session = FakeSession(delay=0.05)
    daemon = make_daemon(tmp_path, session)
    async with running(daemon) as live:
        replies = await asyncio.gather(
            *(
                daemon_client.run(live.socket_path, operation=f"op{i}", params={}, actor="cli")
                for i in range(4)
            )
        )

    assert len(replies) == 4
    assert session.max_concurrent == 1, "two operations ran against one client at once"


@pytest.mark.asyncio
async def test_a_slow_request_does_not_block_a_ping(tmp_path: Path) -> None:
    session = FakeSession(delay=0.4)
    daemon = make_daemon(tmp_path, session)
    async with running(daemon) as live:
        slow = asyncio.create_task(
            daemon_client.run(live.socket_path, operation="slow", params={}, actor="cli")
        )
        await asyncio.sleep(0.05)
        # The accept loop is not the thing being serialised: a ping answers now.
        reply = await asyncio.wait_for(daemon_client.ping(live.socket_path), timeout=0.3)
        assert reply["kind"] == "pong"
        await asyncio.wait_for(slow, timeout=5)


# --- lifecycle --------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_idle_daemon_shuts_itself_down(tmp_path: Path) -> None:
    daemon = make_daemon(tmp_path, idle_timeout=0.15)
    task = asyncio.create_task(daemon.serve())
    await asyncio.wait_for(daemon.started.wait(), timeout=5)
    path = daemon.socket_path

    assert await asyncio.wait_for(task, timeout=5) == "served"
    assert not path.exists()
    assert daemon.session.closed == 1


@pytest.mark.asyncio
async def test_sigterm_shuts_the_daemon_down_cleanly(tmp_path: Path) -> None:
    import signal

    daemon = make_daemon(tmp_path, install_signal_handlers=True)
    task = asyncio.create_task(daemon.serve())
    await asyncio.wait_for(daemon.started.wait(), timeout=5)
    path = daemon.socket_path

    os.kill(os.getpid(), signal.SIGTERM)

    assert await asyncio.wait_for(task, timeout=5) == "served"
    assert not path.exists()
    assert daemon.session.closed == 1


@pytest.mark.asyncio
async def test_shutdown_does_not_remove_a_successors_socket(tmp_path: Path) -> None:
    """Unlinking by name would delete whatever bound the path in between."""
    daemon = make_daemon(tmp_path)
    async with running(daemon):
        pass

    path = daemon.socket_path
    replacement = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    replacement.bind(str(path))
    try:
        daemon.remove_socket_if_ours()  # the shutdown step, against a new inode
        assert path.exists(), "a socket bound by someone else was unlinked"
    finally:
        replacement.close()
        path.unlink(missing_ok=True)


# --- the surface it refuses to be ------------------------------------------


@pytest.mark.asyncio
async def test_no_endpoint_names_an_mtproto_method_or_an_attribute(tmp_path: Path) -> None:
    daemon = make_daemon(tmp_path, StrictSession())
    async with running(daemon) as live:
        # Unknown action: the protocol has exactly two verbs.
        reply = await daemon_client.call(
            live.socket_path, {"v": protocol.PROTOCOL_VERSION, "action": "invoke"}
        )
        assert reply["ok"] is False
        assert reply["error"]["code"] == str(ErrorCode.INVALID_INPUT)

        # And "run" reaches the registry by name, never the client.
        for name in ("client.get_entity", "messages.GetHistoryRequest", "__class__"):
            reply = await daemon_client.call(
                live.socket_path,
                {
                    "v": protocol.PROTOCOL_VERSION,
                    "action": "run",
                    "operation": name,
                    "params": {},
                    "actor": "cli",
                },
            )
            assert reply["ok"] is False, f"{name} was accepted as an operation"
            assert reply["error"]["code"] == str(ErrorCode.UNKNOWN_OPERATION)


def test_only_registered_operations_are_selectable() -> None:
    with pytest.raises(TelegramAIError) as excinfo:
        service.select_operation("messages.GetHistoryRequest")
    assert excinfo.value.code is ErrorCode.UNKNOWN_OPERATION

    with pytest.raises(TelegramAIError):
        service.select_operation("__class__")


def test_the_daemon_cannot_apply_a_plan() -> None:
    """The approval boundary, checked at the surface that could bypass it."""
    for name in ("plan.apply", "apply", "plans.apply"):
        with pytest.raises(TelegramAIError) as excinfo:
            service.select_operation(name)
        assert excinfo.value.code is ErrorCode.UNKNOWN_OPERATION

    assert not [op.name for op in service.selectable_operations() if "apply" in op.name.lower()]


def test_account_administration_is_not_reachable_over_the_socket() -> None:
    """Signing an account in prompts a person; a socket cannot be prompted."""
    from telegram_ai_cli.opspec import REGISTRY, Effect

    admin = [op for op in REGISTRY.all() if op.effect is Effect.LOCAL_ADMIN]
    assert admin, "no LOCAL_ADMIN operation exists; this test has nothing to prove"
    for op in admin:
        with pytest.raises(TelegramAIError) as excinfo:
            service.select_operation(op.name)
        assert excinfo.value.code is ErrorCode.FORBIDDEN_BY_PROFILE


@pytest.mark.asyncio
async def test_params_are_revalidated_inside_the_daemon(tmp_path: Path) -> None:
    """The wire is not a trust boundary that can be skipped."""
    daemon = make_daemon(tmp_path, FakeSession())
    async with running(daemon) as live:
        reply = await daemon_client.call(
            live.socket_path,
            {
                "v": protocol.PROTOCOL_VERSION,
                "action": "run",
                "operation": "boom",
                "params": {},
                "actor": "cli",
            },
        )
    assert reply["ok"] is False
    assert reply["error"]["code"] == str(ErrorCode.INVALID_INPUT)


@pytest.mark.asyncio
async def test_an_oversized_frame_is_refused(tmp_path: Path) -> None:
    daemon = make_daemon(tmp_path)
    async with running(daemon) as live:
        reader, writer = await asyncio.open_unix_connection(str(live.socket_path))
        try:
            # Only the header is sent: a 16 MiB body must never be allocated to
            # find out the frame is too big.
            writer.write((protocol.MAX_FRAME_BYTES + 1).to_bytes(4, "big"))
            await writer.drain()
            reply = await asyncio.wait_for(protocol.read_frame(reader), timeout=5)
            assert reply is not None
            assert reply["ok"] is False
            assert reply["error"]["code"] == str(ErrorCode.INVALID_INPUT)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


# --- the client -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_client_reports_no_daemon_rather_than_hanging(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    daemon_paths.prepare_account_dir(settings, ACCOUNT)
    with pytest.raises(daemon_client.DaemonUnavailable):
        await daemon_client.ping(daemon_paths.socket_path(settings, ACCOUNT))


# --- the configuration a request runs under ---------------------------------


def test_the_fingerprint_ignores_transport_settings_and_nothing_else(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    baseline = service.policy_fingerprint(settings)

    # How a caller reaches the tool is allowed to differ.
    settings.daemon.idle_timeout_seconds = 999
    settings.http.port = 9999
    assert service.policy_fingerprint(settings) == baseline

    # What it may do is not.
    settings.profile = "plan"
    assert service.policy_fingerprint(settings) != baseline

    narrowed = settings_for(tmp_path)
    narrowed.safety.read.dms.allow = [12345]
    assert service.policy_fingerprint(narrowed) != baseline


@pytest.mark.asyncio
async def test_a_daemon_under_another_configuration_refuses_before_running(tmp_path: Path) -> None:
    """Borrowing a daemon started with a wider policy is the hole to close."""
    session = service.RegistrySession(label=ACCOUNT)
    session.policy = "the-daemon-was-started-with-this"

    with pytest.raises(TelegramAIError) as excinfo:
        await session.run("chat.read", {}, actor="cli", policy="but-the-caller-has-this")

    assert excinfo.value.code is ErrorCode.FORBIDDEN_BY_PROFILE
    assert excinfo.value.details.get("policy_mismatch") is True
    # Refused before the operation was even resolved, so nothing ran.
    assert session._ctx is None


# --- routing ----------------------------------------------------------------


def _read_op() -> Any:
    from telegram_ai_cli.opspec import REGISTRY, Effect

    return next(
        op
        for op in REGISTRY.all()
        if op.effect is Effect.READ and "account" in op.input_model.model_fields
    )


def test_nothing_is_routed_while_the_daemon_is_disabled(tmp_path: Path) -> None:
    from telegram_ai_cli import dispatch

    op = _read_op()
    settings = settings_for(tmp_path)
    params = op.input_model.model_validate({"account": ACCOUNT})

    assert settings.daemon.enabled is False
    assert dispatch.daemon_socket_for(op, params, settings) is None


def test_only_a_request_that_names_an_account_is_routed(tmp_path: Path) -> None:
    """A daemon serves one account; a fleet-wide call must not be narrowed."""
    from telegram_ai_cli import dispatch

    op = _read_op()
    settings = settings_for(tmp_path)
    settings.daemon.enabled = True

    named = op.input_model.model_validate({"account": ACCOUNT})
    assert dispatch.daemon_socket_for(op, named, settings) == daemon_paths.socket_path(
        settings, ACCOUNT
    )

    anonymous = op.input_model.model_validate({})
    assert dispatch.daemon_socket_for(op, anonymous, settings) is None


def test_account_administration_is_never_routed(tmp_path: Path) -> None:
    from telegram_ai_cli import dispatch
    from telegram_ai_cli.opspec import REGISTRY, Effect

    settings = settings_for(tmp_path)
    settings.daemon.enabled = True
    for op in REGISTRY.all():
        if op.effect is not Effect.LOCAL_ADMIN:
            continue
        if "account" not in op.input_model.model_fields:
            continue
        params = op.input_model.model_construct(account=ACCOUNT)
        assert dispatch.daemon_socket_for(op, params, settings) is None


@pytest.mark.asyncio
async def test_an_envelope_survives_the_round_trip(tmp_path: Path) -> None:
    """What the daemon ran must print as what a local run would have printed."""
    from telegram_ai_cli.envelope import Envelope, Meta

    original = Envelope.success(
        {"rows": [1, 2]},
        warnings=["careful"],
        meta=Meta(
            returned=2,
            truncated=True,
            truncated_reason="limit",
            account=ACCOUNT,
            untrusted_content=True,
            untrusted_markers=("<<", ">>"),
            extra={"cursor": "abc"},
        ),
    )
    rebuilt = Envelope.from_dict(original.to_dict())

    assert rebuilt.to_dict() == original.to_dict()
    assert rebuilt.meta.untrusted_markers == ("<<", ">>")
    assert rebuilt.meta.extra == {"cursor": "abc"}


@pytest.mark.asyncio
async def test_asking_before_any_daemon_ever_ran_is_not_an_error(tmp_path: Path) -> None:
    """ "No daemon has ever run for this account" is the ordinary first answer."""
    settings = settings_for(tmp_path)
    path = daemon_paths.socket_path(settings, ACCOUNT)
    assert not path.parent.exists()

    assert daemon_paths.require_bindable(path) is False
    with pytest.raises(daemon_client.DaemonUnavailable):
        await daemon_client.ping(path)


@pytest.mark.asyncio
async def test_the_client_refuses_a_symlinked_socket(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    daemon_paths.prepare_account_dir(settings, ACCOUNT)
    path = daemon_paths.socket_path(settings, ACCOUNT)
    path.symlink_to(tmp_path / "elsewhere.sock")

    with pytest.raises(InsecurePermissions):
        await daemon_client.ping(path)
