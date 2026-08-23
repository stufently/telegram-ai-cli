"""QR login: the code is displayed, never typed — and never written down.

Three properties are worth a test rather than a review.

*The token is a credential.* ``tg://login?token=…`` is the whole of the login:
anything that imports it becomes the account. It therefore reaches the terminal
and nothing else — not the log, not the audit file, not the envelope a caller
reads. A rendered QR code is unreadable to `grep`, which is exactly why the
assertion has to be written against the *sources* the token could leak into.

*A code expires.* Telegram gives the token well under a minute, and a person
walking to fetch their phone will miss it. So the login regenerates and redraws
— a bounded number of times, and then says so, rather than looping forever in
front of somebody who has already walked away.

*Two-step verification is the same prompt as the phone flow.* A second password
prompt with its own attempt counting and its own scrubbing is a second thing to
get wrong; the QR path calls the one that already exists.
"""

from __future__ import annotations

import io
import logging

import click
import pytest
from telethon import errors

import telegram_ai_cli.ops  # noqa: F401  (registers every operation)
import telegram_ai_cli.ops.accounts as account_ops
from telegram_ai_cli.accounts import login as login_mod
from telegram_ai_cli.accounts.login import (
    DEFAULT_QR_REGENERATIONS,
    LoginResult,
    _sign_in_via_qr,
    qr_login,
    qr_login_and_register,
    show_qr,
)
from telegram_ai_cli.accounts.models import AccountSource, AccountStatus
from telegram_ai_cli.accounts.qr import render_qr
from telegram_ai_cli.cli import _attach
from telegram_ai_cli.config import PathsConfig, Settings
from telegram_ai_cli.context import OperationContext
from telegram_ai_cli.errors import AuthRequired, FloodWait, InvalidInput
from telegram_ai_cli.ops.accounts import QrLoginInput, handle_account_login_qr
from telegram_ai_cli.opspec import REGISTRY, Effect

# Invented credentials: the assertions below are that neither reaches a log, an
# audit record or an envelope, so they have to look like the real thing.
TOKEN = "SECRET-TOKEN-NOBODY-MAY-WRITE-DOWN"  # noqa: S105
TOKEN_URL = f"tg://login?token={TOKEN}"
PASSWORD = "correct horse battery staple"  # noqa: S105


# --- fakes ------------------------------------------------------------------


class FakeQrLogin:
    """Telethon's ``QRLogin``, reduced to what the login actually touches."""

    def __init__(self, outcomes: list[BaseException | None]) -> None:
        self._outcomes = list(outcomes)
        self.url = TOKEN_URL
        self.recreated = 0
        self.waited = 0

    async def wait(self, timeout: float | None = None) -> object:
        self.waited += 1
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if outcome is not None:
            raise outcome
        return object()

    async def recreate(self) -> None:
        self.recreated += 1
        self.url = f"{TOKEN_URL}-{self.recreated}"


class FakeClient:
    """Enough Telethon client for the login body: connect, sign in, get_me."""

    def __init__(
        self,
        qr: FakeQrLogin,
        *,
        session_file=None,
        authorized: bool = False,
        qr_error: BaseException | None = None,
    ) -> None:
        self.qr = qr
        self.session_file = session_file
        self.authorized = authorized
        self.qr_error = qr_error
        self.passwords: list[str] = []
        self.disconnected = False

    async def connect(self) -> None:
        if self.session_file is not None:
            # Telethon writes the DC auth key on connect, at whatever the umask
            # allowed; the login is what hardens it afterwards.
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            self.session_file.touch(mode=0o644)

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def qr_login(self) -> FakeQrLogin:
        if self.qr_error is not None:
            raise self.qr_error
        return self.qr

    async def sign_in(self, password: str | None = None) -> None:
        self.passwords.append(str(password))
        self.authorized = True

    async def get_me(self):
        return type("Me", (), {"id": 4242, "username": "someone", "phone": "15551234"})()

    async def disconnect(self) -> None:
        self.disconnected = True


def command_tree() -> click.Group:
    root = click.Group()
    _attach(root)
    return root


def context_over(tmp_path) -> OperationContext:
    settings = Settings(
        paths=PathsConfig(
            state=tmp_path / "state",
            sessions=tmp_path / "sessions",
            downloads=tmp_path / "downloads",
            audit_log=tmp_path / "state" / "audit.jsonl",
        )
    )
    return OperationContext.build(actor="cli", settings=settings)


class FakeTerminal(io.StringIO):
    """A stand-in for ``/dev/tty``, which a test process does not have.

    ``close`` is a no-op so the buffer survives being written and closed the way
    the real handle is.
    """

    def close(self) -> None:
        pass


@pytest.fixture
def terminal(monkeypatch) -> FakeTerminal:
    """Give the login somewhere to draw. Without it, it refuses — by design."""
    fake = FakeTerminal()
    monkeypatch.setattr(login_mod, "_terminal_writer", lambda: fake)
    return fake


def register_as_logged_in(store, label: str, session_path, user_id: int) -> None:
    """What a real login leaves behind, so the handler has a row to describe."""
    store.upsert(
        label,
        AccountSource.SESSION_FILE,
        session_path=str(session_path),
        replace=True,
    )
    store.set_user_id(label, user_id)
    store.set_status(label, AccountStatus.OK, None)


# --- the surface a person meets --------------------------------------------


def test_the_qr_login_command_exists() -> None:
    account = command_tree().commands["account"]
    assert "login-qr" in account.commands
    login_qr = account.commands["login-qr"]
    assert "--label" in {opt for param in login_qr.params for opt in param.opts}


def test_the_phone_login_is_untouched_and_still_the_default() -> None:
    """QR is opt-in: it is a command of its own, not a mode of the old one."""
    login = command_tree().commands["account"].commands["login"]
    flags = {opt for param in login.params for opt in param.opts}
    assert "--phone" in flags
    assert not [flag for flag in flags if "qr" in flag]


# --- and the boundary it must not cross ------------------------------------


def test_qr_login_is_terminal_only() -> None:
    op = REGISTRY.by_name("account.login_qr")
    assert op.effect is Effect.LOCAL_ADMIN
    assert op.mcp_tool is None
    assert op.plan_tool is None


def test_qr_login_did_not_widen_the_mcp_surface() -> None:
    published = REGISTRY.mcp_tool_names()
    assert not [name for name in published if "qr" in name or "login" in name]


# --- rendering --------------------------------------------------------------


def test_the_code_renders_as_a_rectangular_block_of_text() -> None:
    drawn = render_qr(TOKEN_URL).splitlines()
    assert len(drawn) > 8
    assert len({len(line) for line in drawn}) == 1, "every row must be the same width"
    assert set("".join(drawn)) - {" "}, "a blank block is not a QR code"


def test_inverting_swaps_the_blocks_for_dark_terminals() -> None:
    assert render_qr(TOKEN_URL, invert=True) != render_qr(TOKEN_URL, invert=False)


def test_the_rendered_code_never_spells_the_token_out() -> None:
    """It is a picture of the token, not a copy of it."""
    assert TOKEN not in render_qr(TOKEN_URL)


# --- the lifecycle ----------------------------------------------------------


async def test_the_url_is_shown_to_the_person_and_logged_nowhere(caplog) -> None:
    shown: list[str] = []
    qr = FakeQrLogin([None])
    client = FakeClient(qr)

    with caplog.at_level(logging.DEBUG):
        await _sign_in_via_qr(client, label="work", password_cb=None, display=shown.append)

    assert shown == [TOKEN_URL], "the raw URL is the fallback for terminals without blocks"
    assert TOKEN not in caplog.text
    assert "tg://login" not in caplog.text


async def test_an_expired_code_is_redrawn_a_bounded_number_of_times() -> None:
    shown: list[str] = []
    # Every wait times out: the person never scans.
    qr = FakeQrLogin([TimeoutError()] * 10)
    client = FakeClient(qr)

    with pytest.raises(AuthRequired, match="expired"):
        await _sign_in_via_qr(
            client, label="work", password_cb=None, display=shown.append, regenerations=2
        )

    assert qr.recreated == 2, "bounded: two new codes, then it gives up"
    assert len(shown) == 3, "the first code plus each regenerated one"
    assert shown[0] == TOKEN_URL and shown[1] != shown[2]


async def test_a_code_scanned_after_one_expiry_still_signs_in() -> None:
    qr = FakeQrLogin([TimeoutError(), None])
    client = FakeClient(qr)

    await _sign_in_via_qr(client, label="work", password_cb=None, display=lambda url: None)

    assert qr.recreated == 1
    assert qr.waited == 2


async def test_a_token_expired_answer_counts_as_an_expiry_too() -> None:
    """The other shape of the same thing: a scan that lands after the deadline.

    Telethon raises ``TimeoutError`` when nobody scanned; Telegram answers
    ``AUTH_TOKEN_EXPIRED`` when somebody scanned a moment too late. Both mean
    "show another code", and only one of them is a ``TimeoutError``.
    """
    qr = FakeQrLogin([errors.AuthTokenExpiredError(request=None), None])
    client = FakeClient(qr)

    await _sign_in_via_qr(client, label="work", password_cb=None, display=lambda url: None)

    assert qr.recreated == 1
    assert qr.waited == 2


async def test_a_flood_wait_asking_for_a_code_becomes_this_project_s_error() -> None:
    """Requesting a token is an RPC: a raw Telethon error here would escape the
    caller's `except TelegramAIError` and leave the account row half-written."""
    client = FakeClient(FakeQrLogin([]), qr_error=errors.FloodWaitError(request=None, capture=42))

    with pytest.raises(FloodWait) as failure:
        await _sign_in_via_qr(client, label="work", password_cb=None, display=lambda url: None)

    assert "42" in str(failure.value)


async def test_two_step_verification_reuses_the_phone_flow_prompt(caplog) -> None:
    """Same prompt, same attempt counting, same scrubbing — not a second copy."""
    asked: list[str] = []

    async def password_cb(prompt: str) -> str:
        asked.append(prompt)
        return PASSWORD

    qr = FakeQrLogin([errors.SessionPasswordNeededError(request=None)])
    client = FakeClient(qr)

    with caplog.at_level(logging.DEBUG):
        await _sign_in_via_qr(
            client, label="work", password_cb=password_cb, display=lambda url: None
        )

    assert client.passwords == [PASSWORD]
    assert asked == ["[work] two-step verification password: "]
    assert PASSWORD not in caplog.text


async def test_the_default_regeneration_budget_is_bounded() -> None:
    assert 0 < DEFAULT_QR_REGENERATIONS <= 10


def test_the_default_display_writes_the_code_and_the_url_to_the_terminal(terminal, capsys) -> None:
    """The fallback matters: a terminal that cannot draw blocks still gets a link.

    And it goes to the *terminal*, not to stdout: `tg-ai --json … > out.json`
    would otherwise write a live login token into a file.
    """
    show_qr(TOKEN_URL)

    drawn = terminal.getvalue()
    assert TOKEN_URL in drawn
    assert render_qr(TOKEN_URL) in drawn
    captured = capsys.readouterr()
    assert TOKEN not in captured.out and TOKEN not in captured.err


def test_a_qr_login_refuses_when_there_is_nowhere_private_to_draw(monkeypatch) -> None:
    """Fail-closed, and before a token exists — not after minting one."""
    monkeypatch.setattr(login_mod, "_terminal_writer", lambda: None)
    with pytest.raises(InvalidInput, match="terminal"):
        login_mod.require_display_terminal()


async def test_the_handler_checks_for_a_terminal_before_asking_for_a_token(
    tmp_path, monkeypatch
) -> None:
    called = False

    async def fake_login(store, **kwargs):  # pragma: no cover - must not run
        nonlocal called
        called = True
        raise AssertionError("a token must not be requested with nowhere to show it")

    monkeypatch.setattr(login_mod, "_terminal_writer", lambda: None)
    monkeypatch.setattr(account_ops, "qr_login_and_register", fake_login)

    with context_over(tmp_path) as ctx, pytest.raises(InvalidInput, match="terminal"):
        await handle_account_login_qr(ctx, QrLoginInput(label="work"))

    assert not called


# --- persistence ------------------------------------------------------------


async def test_the_session_is_hardened_exactly_like_the_phone_flow(tmp_path, monkeypatch) -> None:
    sessions = tmp_path / "sessions"
    client = FakeClient(FakeQrLogin([None]), session_file=sessions / "work.session")
    monkeypatch.setattr(login_mod, "new_client", lambda *args, **kwargs: client)

    result = await qr_login(label="work", sessions_dir=sessions, display=lambda url: None)

    assert isinstance(result, LoginResult)
    assert result.user_id == 4242
    assert result.username == "someone"
    assert (sessions / "work.session").stat().st_mode & 0o777 == 0o600
    assert client.disconnected


async def test_registration_records_the_account_the_way_a_phone_login_does(
    tmp_path, monkeypatch
) -> None:
    sessions = tmp_path / "sessions"

    async def fake_qr_login(**kwargs):
        return LoginResult(
            label=str(kwargs["label"]),
            phone="+15551234",
            session_path=sessions / "work.session",
            user_id=4242,
            username="someone",
        )

    monkeypatch.setattr(login_mod, "qr_login", fake_qr_login)

    with context_over(tmp_path) as ctx:
        result = await qr_login_and_register(
            ctx.accounts.store, label="work", sessions_dir=sessions
        )
        record = ctx.accounts.store.require("work")

    assert result.user_id == 4242
    assert record.source is AccountSource.SESSION_FILE
    assert record.status is AccountStatus.OK
    assert record.user_id == 4242
    assert record.phone == "+15551234", "the number the session actually belongs to"


async def test_the_row_takes_the_number_of_whoever_scanned_the_code(tmp_path, monkeypatch) -> None:
    """A QR code can be scanned by a different account than the row expected.

    Carrying the old number over would leave a row whose phone names one account
    and whose session names another — and it is that number a later
    `account login` would send a code to.
    """
    sessions = tmp_path / "sessions"

    async def fake_qr_login(**kwargs):
        return LoginResult(
            label=str(kwargs["label"]),
            phone="+15559999",  # somebody else's phone scanned the code
            session_path=sessions / "work.session",
            user_id=999,
            username=None,
        )

    monkeypatch.setattr(login_mod, "qr_login", fake_qr_login)

    with context_over(tmp_path) as ctx:
        store = ctx.accounts.store
        store.upsert("work", AccountSource.SESSION_FILE, phone="+15551234")
        await qr_login_and_register(
            store, label="work", sessions_dir=sessions, phone="+15551234", replace=True
        )
        record = store.require("work")

    assert record.phone == "+15559999"
    assert record.user_id == 999


# --- the handler ------------------------------------------------------------


async def test_the_handler_reports_the_account_without_leaking_the_token(
    tmp_path, monkeypatch, caplog, terminal
) -> None:
    captured: dict[str, object] = {}

    async def fake_login(store, **kwargs):
        captured.update(kwargs)
        session_path = tmp_path / "sessions" / "work.session"
        register_as_logged_in(store, str(kwargs["label"]), session_path, 4242)
        return LoginResult(
            label=str(kwargs["label"]),
            phone="+15551234",
            session_path=session_path,
            user_id=4242,
            username="someone",
        )

    monkeypatch.setattr(account_ops, "qr_login_and_register", fake_login)

    with context_over(tmp_path) as ctx, caplog.at_level(logging.DEBUG):
        envelope = await handle_account_login_qr(ctx, QrLoginInput(label="work"))
        audit_log = ctx.settings.paths.audit_log

    assert envelope.ok
    assert envelope.data["label"] == "work"
    assert envelope.data["user_id"] == 4242
    payload = str(envelope.to_dict())
    assert "tg://" not in payload and TOKEN not in payload
    assert "tg://" not in caplog.text
    recorded = audit_log.read_text(encoding="utf-8")
    assert "account.login_qr" in recorded, "the attempt/outcome pair is written like any other"
    assert "tg://" not in recorded and TOKEN not in recorded


async def test_a_qr_login_will_not_quietly_replace_an_imported_account(tmp_path, terminal) -> None:
    """Same guard as the phone login: imported material is already authorised."""
    with context_over(tmp_path) as ctx:
        ctx.accounts.store.upsert(
            "legacy",
            AccountSource.TDATA,
            session_path=str(tmp_path / "sessions" / "tdata" / "legacy"),
        )
        with pytest.raises(InvalidInput, match="replace"):
            await handle_account_login_qr(ctx, QrLoginInput(label="legacy"))


async def test_a_qr_login_needs_no_phone_number(tmp_path, monkeypatch, terminal) -> None:
    """The point of the flow: nothing is typed, so nothing has to be known."""

    async def fake_login(store, **kwargs):
        session_path = tmp_path / "sessions" / "fresh.session"
        register_as_logged_in(store, str(kwargs["label"]), session_path, 7)
        return LoginResult(
            label=str(kwargs["label"]),
            phone="",
            session_path=session_path,
            user_id=7,
            username=None,
        )

    monkeypatch.setattr(account_ops, "qr_login_and_register", fake_login)

    with context_over(tmp_path) as ctx:
        envelope = await handle_account_login_qr(ctx, QrLoginInput(label="fresh"))

    assert envelope.ok
    assert envelope.data["user_id"] == 7
