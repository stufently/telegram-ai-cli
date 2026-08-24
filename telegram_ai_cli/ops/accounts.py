"""``account add`` / ``account login`` / ``account login-qr`` — putting an
account into the fleet.

Every other operation starts from an account that already exists; these are how
one comes to exist. They are the first commands the README tells a person to
run, and the ones every "no usable account is configured" suggestion points
back at.

**None is an MCP tool, and that is enforced rather than remembered.** All are
:attr:`~telegram_ai_cli.opspec.Effect.LOCAL_ADMIN`, which the registry's
invariants refuse to publish as a tool. Two reasons, and either alone would be
enough: signing in puts something in front of a human — the code Telegram just
sent to their phone, or a QR code only they can hold a phone up to — which no
tool call can answer; and enrolling an account is the one action that widens the
fleet the rest of the policy is written against — a caller who can add an
account can add one whose chats no allowlist was ever written for.

The QR login has a third reason of its own. Its token *is* the login: whatever
imports ``tg://login?token=…`` becomes the account, with no code and no
password. A tool that could ask for one would be handing a caller a credential
across the boundary the whole design exists to hold, which is why the URL is
drawn on the terminal and appears in no result, no log and no audit record.

Credentials stay off the command line, with one caveat stated rather than
hidden. There is no ``--api-hash`` flag: a value in ``argv`` is visible in
``ps`` to every user on the host and lands in shell history. Application
credentials come from the account's own row, from
``TGAI_API_ID``/``TGAI_API_HASH`` (or the YAML equivalents), or they are
generated per label and frozen — see
:mod:`telegram_ai_cli.accounts.api_profile`. The two-step verification password
is never accepted from anywhere but an interactive prompt, which is
:mod:`telegram_ai_cli.accounts.login`'s own rule.

The caveat is ``--proxy``: a proxy URL may carry ``user:password``, and that
one *does* reach ``argv``. It is redacted everywhere it is logged or printed and
encrypted at rest, but a credentialed proxy passed on the command line is still
in shell history — set it once, from a shell that does not record the line, or
use a proxy that does not need a password.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..accounts.login import (
    login_and_register,
    normalize_phone,
    qr_login_and_register,
    require_display_terminal,
    show_qr,
)
from ..accounts.models import AccountSource
from ..accounts.spec import AccountSpec
from ..accounts.views import AccountView
from ..context import OperationContext
from ..envelope import Envelope
from ..errors import AccountNotFound, InvalidInput, TelegramAIError
from ..opspec import REGISTRY, Effect, Operation


class AccountInput(BaseModel):
    """Shared shape: every account command names one label.

    ``extra="forbid"`` for the same reason the read operations use it — a
    misspelled argument that validates silently runs with a default and looks
    authoritative.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(description="Name this account is referred to by, e.g. `work`.")


class AddAccountInput(AccountInput):
    phone: str | None = Field(
        default=None,
        description="Phone number in E.164 form, remembered for `account login`.",
    )
    proxy: str | None = Field(
        default=None,
        description="Proxy URL the account connects through, e.g. socks5://host:1080.",
    )
    tdata: str | None = Field(
        default=None,
        description="Import a Telegram Desktop tdata folder instead of logging in.",
    )
    session_file: str | None = Field(
        default=None,
        description="Adopt an existing Telethon .session file instead of logging in.",
    )
    replace: bool = Field(
        default=False,
        description="Overwrite an account already registered under this label.",
    )


class LoginInput(AccountInput):
    phone: str | None = Field(
        default=None,
        description="Phone number in E.164 form. Omit to reuse the registered one.",
    )
    proxy: str | None = Field(
        default=None,
        description="Proxy URL to sign in through. Omit to reuse the registered one.",
    )
    replace: bool = Field(
        default=False,
        description="Sign in even if this label is registered from tdata or a session file.",
    )


class QrLoginInput(AccountInput):
    """No phone number: the point of the flow is that nothing is typed."""

    proxy: str | None = Field(
        default=None,
        description="Proxy URL to sign in through. Omit to reuse the registered one.",
    )
    replace: bool = Field(
        default=False,
        description="Sign in even if this label is registered from tdata or a session file.",
    )
    invert: bool = Field(
        default=False,
        description="Swap the QR code's blocks and gaps, for a terminal with a dark background.",
    )


def _registry(ctx: OperationContext) -> Any:
    registry = ctx.accounts
    if registry is None:  # pragma: no cover - OperationContext.build always sets it
        raise AccountNotFound("the account registry is not available in this context")
    return registry


def _fallback_api(ctx: OperationContext) -> tuple[int | None, str | None]:
    """Application credentials from the configuration, if any were given.

    These are the only place ``Settings.api_id``/``api_hash`` are consulted: a
    connection reads them from the account's row or from the fingerprint frozen
    beside its session, so this is where a configured pair enters that row.
    Absent, a plausible Telegram Desktop fingerprint is generated per label.
    """
    return ctx.settings.api_id, ctx.settings.api_hash


def _describe(view: AccountView) -> dict[str, Any]:
    """The parts of an account safe to print.

    :class:`~telegram_ai_cli.accounts.views.AccountView` is already free of
    ``api_hash`` and of proxy passwords; the phone number is dropped here on top
    of that, because an account listing has no reason to echo it back.
    """
    return {
        "label": view.label,
        "source": view.source,
        "status": view.status,
        "user_id": view.user_id,
        "has_api_hash": view.has_api_hash,
        "has_proxy": view.has_proxy,
        "session_path": view.session_path,
    }


async def handle_account_add(ctx: OperationContext, params: AddAccountInput) -> Envelope:
    """Register an account. Nothing connects to Telegram here.

    Three ways in, and they are deliberately separate commands' worth of
    behaviour behind one: a fresh phone login (the row is written now, the
    session arrives with ``account login``), a Telegram Desktop ``tdata``
    folder, or an existing ``.session`` file. The last two are authorised
    already and need no login at all.
    """
    registry = _registry(ctx)
    if params.tdata and params.session_file:
        raise InvalidInput(
            "an account comes from one source: pass --tdata or --session-file, not both"
        )
    if params.phone and (params.tdata or params.session_file):
        # Refused rather than ignored: imported material is already authorised,
        # so a phone passed here would be stored as though it had been verified
        # by a login that never runs.
        raise InvalidInput(
            "--phone belongs to the login flow; imported material is already authorised",
            suggestion="Drop --phone, or register the account without --tdata/--session-file.",
        )

    api_id, api_hash = _fallback_api(ctx)
    if params.tdata:
        source = "tdata"
    elif params.session_file:
        source = "session_file"
    else:
        source = "phone"
    event = ctx.audit.attempt(
        action="account.add",
        account=params.label,
        actor=ctx.actor,
        extra={"source": source},
    )
    try:
        if params.tdata:
            view = registry.import_tdata(
                params.tdata,
                label=params.label,
                proxy_url=params.proxy,
                api_id=api_id,
                api_hash=api_hash,
                replace=params.replace,
            )
        elif params.session_file:
            view = registry.import_session_file(
                params.session_file,
                label=params.label,
                proxy_url=params.proxy,
                api_id=api_id,
                api_hash=api_hash,
                replace=params.replace,
            )
        else:
            view = registry.register_phone_login(
                params.label,
                phone=normalize_phone(params.phone) if params.phone else None,
                api_id=api_id,
                api_hash=api_hash,
                proxy_url=params.proxy,
                replace=params.replace,
            )
    except TelegramAIError as exc:
        ctx.audit.outcome(event, status="failed", error_code=str(exc.code))
        raise
    ctx.audit.outcome(event, status="applied")

    payload = _describe(view)
    payload["next"] = (
        f"tg-ai account login --label {view.label}"
        if source == "phone"
        else f"tg-ai fleet --account {view.label}"
    )
    return Envelope.success(payload)


def _refuse_to_repoint(registry: Any, existing: Any, *, replace: bool, kind: str) -> None:
    """Stop a login from silently pointing a label at different material.

    Two ways that happens: the row names another kind of source (tdata, a
    session string), or it names a ``.session`` adopted from somewhere else —
    ``account add --session-file …`` with copying turned off leaves the row
    pointing at the operator's own file, and a login would write a new one over
    the label instead.

    The message names the label and the source, never a path. Failure payloads
    are redacted and defanged, but a local path is still needless disclosure in
    project-authored prose.
    """
    own_session = str(registry.paths_for(existing.label).session_file)
    elsewhere = (existing.session_path or own_session) != own_session
    if (existing.source is not AccountSource.SESSION_FILE or elsewhere) and not replace:
        raise InvalidInput(
            f"account {existing.label!r} is already registered from "
            f"{existing.source}; {kind.lower()} would replace that "
            "registration with a new session",
            suggestion="Pass --replace to sign in anyway, or use a different label.",
        )


async def handle_account_login(ctx: OperationContext, params: LoginInput) -> Envelope:
    """Sign an account in, prompting for the code Telegram sends.

    Idempotent by way of :func:`~telegram_ai_cli.accounts.login.interactive_login`:
    an already authorised session is reported as-is rather than triggering
    another code request, which Telegram rate limits hard.

    Whatever the row already knows — phone, proxy, application credentials — is
    reused unless this invocation overrides it. Re-registering with the fields
    left blank would otherwise silently drop the proxy an account was signed in
    through, and connecting from a different address is how a fresh session
    gets killed.
    """
    registry = _registry(ctx)
    existing = registry.store.get(params.label)

    api_id, api_hash = _fallback_api(ctx)
    phone = params.phone
    proxy_url = params.proxy

    if existing is not None:
        _refuse_to_repoint(registry, existing, replace=params.replace, kind="A phone login")
        spec = AccountSpec.from_record(existing, registry.store)
        phone = phone or existing.phone
        proxy_url = proxy_url or spec.proxy_url
        api_id = spec.api_id or api_id
        api_hash = spec.api_hash or api_hash

    if not phone:
        raise InvalidInput(
            f"no phone number is registered for account {params.label!r}",
            suggestion="Pass --phone with the number in E.164 form: a leading + and digits.",
        )

    event = ctx.audit.attempt(action="account.login", account=params.label, actor=ctx.actor)
    try:
        result = await login_and_register(
            registry.store,
            label=params.label,
            phone=phone,
            sessions_dir=registry.sessions_dir,
            api_id=api_id,
            api_hash=api_hash,
            proxy_url=proxy_url,
            settings=ctx.settings,
            box=registry.store.box,
            # An existing row is not an error here: `account add` writes one on
            # purpose, and refusing to overwrite it would make the two-step
            # onboarding the README documents impossible.
            replace=params.replace or existing is not None,
        )
    except TelegramAIError as exc:
        # The code, not the message: an error string from this path can carry a
        # phone number or a proxy password, and the audit log is a file that
        # outlives the terminal it was printed to.
        ctx.audit.outcome(event, status="failed", error_code=str(exc.code))
        raise
    ctx.audit.outcome(event, status="applied")

    payload = _describe(registry.require(result.label))
    payload["username"] = result.username
    payload["next"] = f"tg-ai fleet --account {result.label}"
    return Envelope.success(payload)


async def handle_account_login_qr(ctx: OperationContext, params: QrLoginInput) -> Envelope:
    """Sign an account in by QR code: it is displayed, scanned, and that is all.

    No phone number is asked for or needed, so this also enrols a label that was
    never registered — the row is written by the same call the phone login uses,
    at the same point, and everything it leaves on disk is identical.

    The ``tg://login`` URL goes to the terminal and nowhere else. It is not in
    the envelope this returns, not in the audit record, and not in the log: an
    audit file outlives the terminal it was written from, and this particular
    string is enough on its own to take the account over. That is also why the
    terminal is required *before* anything is requested: a token that has been
    minted and cannot be shown is a live credential created for nothing, and
    stdout — which may be a file, a pipe, or a caller reading `--json` — is not
    somewhere it may be written.
    """
    require_display_terminal()
    registry = _registry(ctx)
    existing = registry.store.get(params.label)

    api_id, api_hash = _fallback_api(ctx)
    proxy_url = params.proxy
    phone = None

    if existing is not None:
        _refuse_to_repoint(registry, existing, replace=params.replace, kind="A QR login")
        spec = AccountSpec.from_record(existing, registry.store)
        proxy_url = proxy_url or spec.proxy_url
        api_id = spec.api_id or api_id
        api_hash = spec.api_hash or api_hash
        # Carried through rather than re-verified: a QR login never checks a
        # number, so it must not blank one an earlier login did check.
        phone = existing.phone

    event = ctx.audit.attempt(action="account.login_qr", account=params.label, actor=ctx.actor)
    try:
        result = await qr_login_and_register(
            registry.store,
            label=params.label,
            sessions_dir=registry.sessions_dir,
            phone=phone,
            api_id=api_id,
            api_hash=api_hash,
            proxy_url=proxy_url,
            settings=ctx.settings,
            box=registry.store.box,
            display=partial(show_qr, invert=params.invert),
            replace=params.replace or existing is not None,
            # Row replacement is needed for the normal add-then-login flow;
            # identity replacement still requires the operator's explicit flag.
            allow_identity_change=params.replace,
        )
    except TelegramAIError as exc:
        # The code, not the message, exactly as the phone login records it.
        ctx.audit.outcome(event, status="failed", error_code=str(exc.code))
        raise
    ctx.audit.outcome(event, status="applied")

    payload = _describe(registry.require(result.label))
    payload["username"] = result.username
    payload["next"] = f"tg-ai fleet --account {result.label}"
    return Envelope.success(payload)


ADD_ACCOUNT = REGISTRY.register(
    Operation(
        name="account.add",
        cli=("account", "add"),
        summary="Register an account with this installation.",
        description=(
            "Writes the account's row and prepares its directory. Nothing connects to "
            "Telegram: a phone account is signed in afterwards with `tg-ai account login`, "
            "while `--tdata` and `--session-file` adopt material that is already authorised."
        ),
        input_model=AddAccountInput,
        effect=Effect.LOCAL_ADMIN,
        capability=None,  # No peer, and no profile gate: see the module docstring.
        handler=handle_account_add,  # type: ignore[arg-type]
        tags=("accounts", "admin"),
    )
)

LOGIN_ACCOUNT = REGISTRY.register(
    Operation(
        name="account.login",
        cli=("account", "login"),
        summary="Sign a registered account in, interactively.",
        description=(
            "Requests a login code, prompts for it, and prompts for the two-step "
            "verification password if the account has one. Both are read from the "
            "terminal and never from arguments or the environment."
        ),
        input_model=LoginInput,
        effect=Effect.LOCAL_ADMIN,
        capability=None,
        handler=handle_account_login,  # type: ignore[arg-type]
        tags=("accounts", "admin"),
    )
)

LOGIN_ACCOUNT_QR = REGISTRY.register(
    Operation(
        name="account.login_qr",
        cli=("account", "login-qr"),
        summary="Sign a registered account in by scanning a QR code.",
        description=(
            "Draws a login QR code in the terminal and waits for an already signed-in "
            "Telegram app to scan it — Settings → Devices → Link Desktop Device. No "
            "phone number and no code are typed; an expired code is redrawn a few "
            "times, and a two-step verification password is prompted for exactly as "
            "the phone login prompts for it. The login token is shown and never "
            "logged, recorded or returned."
        ),
        input_model=QrLoginInput,
        effect=Effect.LOCAL_ADMIN,
        capability=None,
        handler=handle_account_login_qr,  # type: ignore[arg-type]
        tags=("accounts", "admin"),
    )
)
