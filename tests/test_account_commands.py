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

import telegram_ai_cli.ops  # noqa: F401  (registers every operation)
import telegram_ai_cli.ops.accounts as account_ops
from telegram_ai_cli.accounts.login import LoginResult
from telegram_ai_cli.accounts.models import AccountSource, AccountStatus
from telegram_ai_cli.cli import _attach
from telegram_ai_cli.config import PathsConfig, Settings
from telegram_ai_cli.context import OperationContext
from telegram_ai_cli.errors import ErrorCode, InvalidInput
from telegram_ai_cli.ops.accounts import (
    AddAccountInput,
    LoginInput,
    handle_account_add,
    handle_account_login,
)
from telegram_ai_cli.opspec import REGISTRY, Effect

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
