"""The only place that knows how an account becomes a connected client.

Everything else in :mod:`telegram_ai_cli.ops` asks for ``open_account(ctx,
label)`` and gets back a connected Telethon client. That indirection is not
ceremony: the account package owns session files, locking and the device
fingerprint, and those details are still settling. Confining the call to one
adapter means a change in its signature is a change in one function rather than
in six operations.

The lookup is written against the expected shape — ``list_accounts()``,
``get(label)`` and an async entry point returning something with ``.client``
and ``.spec`` — and falls back through the near neighbours of that shape rather
than failing on the first ``AttributeError``. Where nothing matches, the error
names the adapter, so the fix is obvious.

Reading never acknowledges messages. Nothing here enables Telethon's
``catch_up`` or update handling, because both make the client behave like a
running userbot rather than a query tool, and a userbot changes dialog state
just by being connected.
"""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ..context import OperationContext
from ..errors import (
    AccountDisabled,
    AccountNotFound,
    AuthRequired,
    InvalidInput,
)

#: Entry points tried, in order, to turn a label into a client.
_OPEN_METHODS = ("load_account", "build_client", "open", "connect")

#: Attribute names an account object may use for its label.
_LABEL_ATTRS = ("label", "name", "alias", "id")


@dataclass(slots=True)
class OpenAccount:
    """A connected account: the Telethon client plus how it is configured."""

    label: str
    client: Any
    spec: Any = None


def _registry(ctx: OperationContext) -> Any:
    registry = ctx.accounts
    if registry is None:
        raise AccountNotFound(
            "no accounts are configured",
            suggestion="Add an account with `tg-ai account add` before reading anything.",
        )
    return registry


def label_of(account: Any) -> str:
    """Best-effort label for an account object or a bare string."""
    if isinstance(account, str):
        return account
    for attr in _LABEL_ATTRS:
        value = getattr(account, attr, None)
        if isinstance(value, str) and value:
            return value
    return str(account)


def list_accounts(ctx: OperationContext) -> list[Any]:
    """Every registered account, in registration order."""
    registry = _registry(ctx)
    listing = registry.list_accounts()
    return list(listing)


def visible_labels(ctx: OperationContext) -> list[str]:
    """Labels this configuration permits touching at all."""
    return [
        label
        for label in (label_of(account) for account in list_accounts(ctx))
        if ctx.safety.account_allowed(label)
    ]


def resolve_label(ctx: OperationContext, label: str | None) -> str:
    """Pick the account to use, refusing to guess when it matters.

    With one permitted account the choice is unambiguous and demanding
    ``--account`` on every command would be noise. With several, picking one
    silently means an operation runs from an identity the caller did not
    choose — which, for a tool that reads private conversations, is the wrong
    kind of convenience.
    """
    permitted = visible_labels(ctx)
    if label is not None:
        if not ctx.safety.account_allowed(label):
            raise AccountDisabled(
                f"account {label!r} is excluded by safety.accounts",
                suggestion="Add it to safety.accounts.allow, or remove it from deny.",
            )
        known = {label_of(account) for account in list_accounts(ctx)}
        if label not in known:
            raise AccountNotFound(f"no account labelled {label!r}")
        return label

    if not permitted:
        raise AccountNotFound(
            "no usable account is configured",
            suggestion="Register one, or widen safety.accounts.allow.",
        )
    if len(permitted) > 1:
        raise InvalidInput(
            "several accounts are configured; say which one to use",
            details={"accounts": permitted},
        )
    return permitted[0]


async def _call_open(registry: Any, label: str) -> Any:
    """Invoke whichever opener the account package exposes."""
    for name in _OPEN_METHODS:
        method = getattr(registry, name, None)
        if method is None:
            continue
        result = method(label)
        if inspect.isawaitable(result):
            result = await result
        return result
    raise AccountNotFound(
        f"the account registry exposes none of {_OPEN_METHODS}; "
        "telegram_ai_cli/ops/_client.py is the single place to teach it the real name"
    )


@contextlib.asynccontextmanager
async def open_account(ctx: OperationContext, label: str | None) -> AsyncIterator[OpenAccount]:
    """Yield a connected, authorised client for ``label``.

    Connection state is left as it was found. An account package that keeps a
    long-lived client must not have it disconnected underneath it by a single
    read, and an adapter that opened the connection itself must not leave it
    open — either mistake shows up much later, as a session lock nobody can
    explain.
    """
    resolved = resolve_label(ctx, label)
    registry = _registry(ctx)

    async with contextlib.AsyncExitStack() as stack:
        opened = await _call_open(registry, resolved)

        # The opener may hand back an async context manager that owns the
        # session lock for the duration of the work.
        if hasattr(opened, "__aenter__"):
            opened = await stack.enter_async_context(opened)

        client = getattr(opened, "client", opened)
        spec = getattr(opened, "spec", None)
        if spec is None:
            spec = getattr(registry, "get", lambda _label: None)(resolved)

        connected_here = False
        if not _is_connected(client):
            await client.connect()
            connected_here = True
        if connected_here:
            stack.push_async_callback(_disconnect, client)

        if not await client.is_user_authorized():
            raise AuthRequired(
                f"account {resolved!r} is not signed in",
                suggestion=f"Run `tg-ai account login --label {resolved}`.",
            )

        yield OpenAccount(label=resolved, client=client, spec=spec)


def _is_connected(client: Any) -> bool:
    probe = getattr(client, "is_connected", None)
    if probe is None:
        return False
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001 - an unusable probe means "connect it"
        return False


async def _disconnect(client: Any) -> None:
    result = client.disconnect()
    if inspect.isawaitable(result):
        await result
