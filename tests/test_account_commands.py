"""``account add`` and ``account login``: present in the CLI, absent from MCP.

These tests exist because of a specific failure. The README opened with
``tg-ai account add`` and ``tg-ai account login``, three error messages inside
the code told people to run them, and neither command existed: the account
package had the login logic, nothing had ever registered it as an operation.
Every test file passed, because nothing asserted what the command tree actually
contains.

So the first two assertions here are about the surface a person meets, and the
rest are about the boundary that surface must not cross — signing an account in
is a terminal action, and publishing it as a tool would let a caller enrol an
account the allowlists were never written for.
"""

from __future__ import annotations

import click
import pytest

import telegram_ai_cli_mcp.ops  # noqa: F401  (registers every operation)
import telegram_ai_cli_mcp.ops.accounts as account_ops
from telegram_ai_cli_mcp.accounts.lock import SessionLock, SessionLocks
from telegram_ai_cli_mcp.accounts.login import LoginResult, login_and_register, session_file_lock
from telegram_ai_cli_mcp.accounts.models import AccountSource, AccountStatus
from telegram_ai_cli_mcp.accounts.paths import SessionPaths, session_lock_path, session_lock_paths
from telegram_ai_cli_mcp.cli import _attach
from telegram_ai_cli_mcp.config import PathsConfig, Settings
from telegram_ai_cli_mcp.context import OperationContext
from telegram_ai_cli_mcp.errors import AuthRequired, ErrorCode, InvalidInput, SessionLocked
from telegram_ai_cli_mcp.ops.accounts import (
    AddAccountInput,
    LoginInput,
    handle_account_add,
    handle_account_login,
)
from telegram_ai_cli_mcp.opspec import REGISTRY, Effect

ACCOUNT_OPERATIONS = ("account.add", "account.login")


def command_tree() -> click.Group:
    """A throwaway root, so the assertions don't depend on import order."""
    root = click.Group()
    _attach(root)
    return root


def context_over(tmp_path) -> OperationContext:
    """A real context on a temporary state directory — no network, no home dir."""
    settings = Settings(
        paths=PathsConfig(
            state=tmp_path / "state",
            sessions=tmp_path / "sessions",
            downloads=tmp_path / "downloads",
            audit_log=tmp_path / "state" / "audit.jsonl",
        )
    )
    return OperationContext.build(actor="cli", settings=settings)


# --- the surface a person meets --------------------------------------------


@pytest.mark.parametrize("name", ["add", "login"])
def test_the_documented_onboarding_commands_exist(name: str) -> None:
    account = command_tree().commands["account"]
    assert isinstance(account, click.Group)
    assert name in account.commands, f"README documents `tg-ai account {name}`"


def test_the_label_is_an_option_the_way_the_readme_spells_it() -> None:
    login = command_tree().commands["account"].commands["login"]
    assert "--label" in {opt for param in login.params for opt in param.opts}


# --- and the boundary it must not cross ------------------------------------


@pytest.mark.parametrize("name", ACCOUNT_OPERATIONS)
def test_account_administration_is_terminal_only(name: str) -> None:
    op = REGISTRY.by_name(name)
    assert op.effect is Effect.LOCAL_ADMIN
    assert op.mcp_tool is None
    assert op.plan_tool is None


def test_adding_these_commands_did_not_widen_the_mcp_surface() -> None:
    published = REGISTRY.mcp_tool_names()
    assert not [name for name in published if "account" in name or "login" in name]


# --- what the handlers actually do -----------------------------------------


async def test_add_registers_an_account_without_connecting(tmp_path) -> None:
    with context_over(tmp_path) as ctx:
        envelope = await handle_account_add(ctx, AddAccountInput(label="work", phone="+15551234"))

        assert envelope.ok
        assert envelope.data["label"] == "work"
        # Registered, not signed in: the session arrives with `account login`.
        assert envelope.data["status"] == str(AccountStatus.NEW)
        assert "account login" in envelope.data["next"]

        record = ctx.accounts.store.require("work")
        assert record.source is AccountSource.SESSION_FILE
        assert record.phone == "+15551234"


async def test_add_refuses_two_sources_at_once(tmp_path) -> None:
    with context_over(tmp_path) as ctx, pytest.raises(InvalidInput) as failure:
        await handle_account_add(
            ctx,
            AddAccountInput(
                label="work",
                tdata=str(tmp_path / "tdata"),
                session_file=str(tmp_path / "work.session"),
            ),
        )
    assert failure.value.code is ErrorCode.INVALID_INPUT


async def test_login_without_a_phone_says_so_before_any_network_call(tmp_path) -> None:
    with context_over(tmp_path) as ctx:
        await handle_account_add(ctx, AddAccountInput(label="work"))

        with pytest.raises(InvalidInput, match="phone"):
            await handle_account_login(ctx, LoginInput(label="work"))


async def test_add_refuses_a_phone_it_would_never_verify(tmp_path) -> None:
    """Imported material is already authorised; no login follows it.

    Storing a phone number there would record it as though a login had checked
    it, which is exactly the kind of unverified fact a later reader trusts.
    """
    with context_over(tmp_path) as ctx, pytest.raises(InvalidInput, match="login flow"):
        await handle_account_add(
            ctx,
            AddAccountInput(
                label="work", phone="+15551234", session_file=str(tmp_path / "work.session")
            ),
        )


async def test_login_reuses_what_the_row_already_knows(tmp_path, monkeypatch) -> None:
    """Re-registering with blank fields must not drop the account's proxy.

    Signing in from the host address after having connected through a proxy is
    the location jump that gets a fresh session killed, so an omitted `--proxy`
    means "keep it", never "clear it".
    """
    captured: dict[str, object] = {}

    async def fake_login(store, **kwargs):
        captured.update(kwargs)
        return LoginResult(
            label=str(kwargs["label"]),
            phone=str(kwargs["phone"]),
            session_path=tmp_path / "sessions" / "work.session",
            user_id=4242,
            username="someone",
        )

    monkeypatch.setattr(account_ops, "login_and_register", fake_login)

    with context_over(tmp_path) as ctx:
        await handle_account_add(
            ctx,
            AddAccountInput(label="work", phone="+15551234", proxy="socks5://proxy.example:1080"),
        )
        envelope = await handle_account_login(ctx, LoginInput(label="work"))

    assert envelope.ok
    assert captured["phone"] == "+15551234"
    assert captured["proxy_url"] == "socks5://proxy.example:1080"
    # The row `account add` wrote is not an obstacle to the login that follows.
    assert captured["replace"] is True


async def test_an_authorised_session_refuses_a_different_phone_before_rewriting(
    tmp_path,
) -> None:
    """An idempotent login must not turn an unverified argument into account data."""
    with context_over(tmp_path) as ctx:
        paths = ctx.accounts.paths_for("work")
        record = ctx.accounts.store.upsert(
            "work",
            AccountSource.SESSION_FILE,
            session_path=str(paths.session_file),
            phone="+15551234",
        )
        ctx.accounts.store.set_user_id(record.label, 4242)
        ctx.accounts.store.set_status(record.label, AccountStatus.OK, None)

        with pytest.raises(InvalidInput, match="different phone"):
            await login_and_register(
                ctx.accounts.store,
                label="work",
                phone="+15559999",
                sessions_dir=ctx.accounts.sessions_dir,
                replace=True,
            )

        after = ctx.accounts.store.require("work")

    assert after.phone == "+15551234"
    assert after.user_id == 4242
    assert after.status is AccountStatus.OK


async def test_a_discovered_phone_mismatch_records_the_session_identity(
    tmp_path, monkeypatch
) -> None:
    """A refused argument must not leave its false value in an otherwise valid row."""
    from telegram_ai_cli_mcp.accounts import login as login_mod

    async def authorised(**kwargs):
        return LoginResult(
            label=str(kwargs["label"]),
            phone="+15551234",
            session_path=tmp_path / "sessions" / "work.session",
            user_id=4242,
        )

    monkeypatch.setattr(login_mod, "interactive_login", authorised)

    with context_over(tmp_path) as ctx:
        with pytest.raises(InvalidInput, match="different phone"):
            await login_mod.login_and_register(
                ctx.accounts.store,
                label="work",
                phone="+15559999",
                sessions_dir=ctx.accounts.sessions_dir,
            )

        record = ctx.accounts.store.require("work")

    assert record.phone == "+15551234"
    assert record.user_id == 4242
    assert record.status is AccountStatus.OK


async def test_a_failed_replacement_restores_the_old_row_and_session(tmp_path, monkeypatch) -> None:
    """Replacement is published only after authorisation succeeds."""
    from telegram_ai_cli_mcp.accounts import login as login_mod

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    session = sessions / "work.session"
    session.write_bytes(b"known-good-session")
    session.chmod(0o600)

    async def rejected(**kwargs):
        session.write_bytes(b"partial-new-session")
        raise AuthRequired("Telegram rejected the replacement")

    monkeypatch.setattr(login_mod, "interactive_login", rejected)

    with context_over(tmp_path) as ctx:
        store = ctx.accounts.store
        old = store.upsert(
            "work",
            AccountSource.SESSION_FILE,
            session_path=str(session),
            phone="+15551234",
            api_id=1234,
            api_hash="old-secret",
        )
        store.set_user_id(old.label, 4242)
        store.set_status(old.label, AccountStatus.OK, None)
        before = store.require("work")

        with pytest.raises(AuthRequired, match="rejected"):
            await login_mod.login_and_register(
                store,
                label="work",
                phone="+15551234",
                sessions_dir=sessions,
                api_id=9999,
                api_hash="new-secret",
                replace=True,
            )

        after = store.require("work")

    assert after == before
    assert session.read_bytes() == b"known-good-session"


async def test_login_will_not_quietly_replace_an_imported_account(tmp_path) -> None:
    """A tdata or session-file account is already authorised.

    Signing in by phone over the top of one rewrites the row to point at a new
    session file, which is a different account's worth of material — so it is
    refused unless it is asked for explicitly.
    """
    with context_over(tmp_path) as ctx:
        ctx.accounts.store.upsert(
            "legacy",
            AccountSource.TDATA,
            session_path=str(tmp_path / "sessions" / "tdata" / "legacy"),
        )

        with pytest.raises(InvalidInput, match="replace"):
            await handle_account_login(ctx, LoginInput(label="legacy", phone="+15551234"))


# --- a login does not rewrite a row another process is using -----------------


def test_the_primary_lock_does_not_change_when_the_session_is_created(tmp_path) -> None:
    """The first connection must not move from a path lock to an inode lock."""
    paths = SessionPaths(tmp_path / "sessions", "work")
    before = session_file_lock(paths)

    paths.sessions_dir.mkdir(parents=True)
    paths.session_file.write_bytes(b"session")

    assert session_file_lock(paths) == before
    assert session_lock_paths("session_file", str(paths.session_file), paths)[0] == before


def test_hard_links_share_the_additional_inode_lock(tmp_path) -> None:
    """Stable path locking must not lose the existing hard-link protection."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    first = sessions / "first.session"
    second = sessions / "second.session"
    first.write_bytes(b"session")
    second.hardlink_to(first)
    first_paths = session_lock_paths("session_file", str(first), SessionPaths(sessions, "first"))
    second_paths = session_lock_paths("session_file", str(second), SessionPaths(sessions, "second"))

    assert first_paths[0] != second_paths[0]
    assert first_paths[1] == second_paths[1]

    holder = SessionLocks(first_paths).acquire()
    try:
        with pytest.raises(SessionLocked):
            SessionLocks(second_paths).acquire()
    finally:
        holder.release()


async def test_a_login_on_a_connected_account_changes_nothing(tmp_path) -> None:
    """The row moves under the same lock the connection will run under.

    `upsert` rewrites `phone`, `source`, `session_path` and `status` — the four
    columns the loader reads to decide what to connect. Written before the lock
    was taken, they landed on a row a live client was reading from, and the
    `SessionLocked` that the login then raised a moment later reported a failure
    whose damage was already done: the account was repointed *and* marked
    `auth_failed` while it was still running fine somewhere else.

    Nothing here is mocked away, because the property is about the order of two
    real side effects. The lock stands in for the other process.
    """
    with context_over(tmp_path) as ctx:
        registry = ctx.accounts
        await handle_account_add(
            ctx,
            AddAccountInput(label="work", phone="+15551234", proxy="socks5://proxy.example:1080"),
        )
        before = registry.store.get("work")
        assert before is not None

        paths = registry.paths_for("work")
        holder = SessionLock(session_file_lock(paths)).acquire()
        try:
            with pytest.raises(SessionLocked):
                await login_and_register(
                    registry.store,
                    label="work",
                    phone="+15559999",
                    sessions_dir=registry.sessions_dir,
                    replace=True,
                )
        finally:
            holder.release()

        after = registry.store.get("work")

    assert after is not None
    assert after.phone == before.phone, "a refused login rewrote the number"
    assert after.status == before.status, "a refused login marked a running account failed"
    assert after.last_error == before.last_error
    assert after.source == before.source
    assert after.session_path == before.session_path


async def test_the_login_itself_still_takes_the_lock_when_nobody_handed_it_one(
    tmp_path, monkeypatch
) -> None:
    """`_authorise` borrows a caller's lock; on its own it must still take one.

    The two paths differ by one branch, and the one that matters least — a
    direct `interactive_login`, which the tests and any future caller use — is
    the one with no caller holding anything.
    """
    from telegram_ai_cli_mcp.accounts import login as login_mod

    paths = SessionPaths(tmp_path / "sessions", "solo")
    holder = SessionLock(session_file_lock(paths)).acquire()
    try:
        with pytest.raises(SessionLocked):
            await login_mod.interactive_login(
                label="solo",
                phone="+15551234",
                sessions_dir=tmp_path / "sessions",
            )
    finally:
        holder.release()


async def test_a_replacing_login_also_holds_the_material_it_is_replacing(tmp_path) -> None:
    """With `--replace` the two locks can be two different auth keys.

    The row names a `.session` the operator adopted from somewhere else
    (`account add --session-file …` without copying); the login is about to
    write this label's own. Those are separate files with separate locks, so
    holding only the one about to be written would leave the *running* account
    free to be repointed underneath itself — the whole failure, merely moved.

    Not every source splits like this: a `tdata` row locks its *converted*
    `.session`, which is this label's own file, so there both locks are one.
    The premise is asserted below rather than assumed, because a test whose two
    locks are secretly the same proves nothing about holding two.
    """
    with context_over(tmp_path) as ctx:
        registry = ctx.accounts
        store = registry.store
        adopted = tmp_path / "elsewhere" / "legacy.session"
        adopted.parent.mkdir(parents=True)
        adopted.write_bytes(b"")
        store.upsert(
            "legacy",
            AccountSource.SESSION_FILE,
            session_path=str(adopted),
            phone="+15551234",
        )
        before = store.get("legacy")
        assert before is not None

        paths = registry.paths_for("legacy")
        current = session_lock_path(str(before.source), before.session_path, paths)
        assert current != session_file_lock(paths), (
            "the two locks must differ, or this proves nothing"
        )

        holder = SessionLock(current).acquire()
        try:
            with pytest.raises(SessionLocked):
                await login_and_register(
                    store,
                    label="legacy",
                    phone="+15559999",
                    sessions_dir=registry.sessions_dir,
                    replace=True,
                )
        finally:
            holder.release()

        after = store.get("legacy")

    assert after is not None
    assert after.source == before.source, "a refused login repointed a running account"
    assert after.session_path == before.session_path
    assert after.phone == before.phone
    assert after.status == before.status


async def test_a_qr_login_on_a_connected_account_changes_nothing(tmp_path) -> None:
    """The QR flow writes the same row the same way, so it takes the same lock.

    Its docstring says as much; before this it was true of the write and not of
    the lock, in both flows.
    """
    from telegram_ai_cli_mcp.accounts.login import qr_login_and_register

    with context_over(tmp_path) as ctx:
        registry = ctx.accounts
        await handle_account_add(ctx, AddAccountInput(label="work", phone="+15551234"))
        before = registry.store.get("work")
        assert before is not None

        holder = SessionLock(session_file_lock(registry.paths_for("work"))).acquire()
        try:
            with pytest.raises(SessionLocked):
                await qr_login_and_register(
                    registry.store,
                    label="work",
                    sessions_dir=registry.sessions_dir,
                    phone="+15559999",
                    replace=True,
                )
        finally:
            holder.release()

        after = registry.store.get("work")

    assert after is not None
    assert after.phone == before.phone
    assert after.status == before.status
