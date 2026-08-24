"""``telegram_fleet`` — which accounts exist and which of them can be used.

This is the operation people run first and the one they run when something is
wrong, so it answers the awkward questions directly: signed in or not, locked by
another process or free, permitted by the safety configuration or excluded from
it. An account that is registered but unusable is worse than one that is
missing, because every other operation fails with a different symptom.

It does not touch the network by default. Session state is on disk, and a fleet
listing that connected five accounts to answer "what do I have" would burn rate
limit and wake sessions to produce a table. ``probe`` asks for the round trip
explicitly, and reports per account what came back.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ..context import OperationContext
from ..envelope import Envelope
from ..errors import AuthRequired, SessionRevoked
from ..opspec import REGISTRY, Effect, Operation
from ._client import label_of, list_accounts, open_account
from ._common import ReadInput, telegram_errors, telegram_result

#: Attributes an account object may use to say "signed in".
_AUTH_ATTRS = ("authorized", "is_authorized", "logged_in")

#: …and to say "another process holds the auth key".
_LOCK_ATTRS = ("locked", "is_locked", "in_use")


class FleetInput(ReadInput):
    """Input for the fleet listing.

    ``account`` is inherited and narrows the listing to one label rather than
    selecting which account performs the work — the only read where it means
    that, because the fleet *is* the subject.
    """

    include_excluded: bool = Field(
        default=False,
        description="Also list accounts that safety.accounts excludes from use.",
    )
    probe: bool = Field(
        default=False,
        description="Connect to each account to confirm its session is still valid.",
    )


def _flag(account: Any, names: tuple[str, ...]) -> bool | None:
    """Read the first attribute that exists, without asserting which one does.

    Returning ``None`` for "the account package does not track this" is the
    point: printing ``false`` for an unknown lock state would tell an operator
    the account is free when nothing checked.
    """
    for name in names:
        value = getattr(account, name, None)
        if value is None:
            continue
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001 - a probe that fails is unknown, not false
                return None
        return bool(value)
    return None


def _describe(ctx: OperationContext, account: Any) -> dict[str, Any]:
    label = label_of(account)
    return {
        "label": label,
        "status": str(getattr(account, "status", None) or "unknown"),
        "authorized": _flag(account, _AUTH_ATTRS),
        "locked": _flag(account, _LOCK_ATTRS),
        "allowed": ctx.safety.account_allowed(label),
        "user_id": getattr(account, "user_id", None),
        "username": getattr(account, "username", None),
        # ``has_proxy``, not ``proxy``: the latter is the *display* string, and
        # its no-proxy value is the placeholder "<none>" rather than empty — so
        # bool() on it reported every account as proxied, including the ones the
        # same run warns about for having no proxy at all.
        "proxy": bool(getattr(account, "has_proxy", False)),
        "last_error": getattr(account, "last_error", None),
    }


async def _probe(ctx: OperationContext, row: dict[str, Any], warnings: list[str]) -> None:
    """Confirm one account's session by asking Telegram who it is.

    A failure here is recorded on the row and continues to the next account: a
    fleet report that stops at the first revoked session hides the state of
    every account after it, which is exactly when the report is needed.
    """
    label = row["label"]
    try:
        async with open_account(ctx, label) as account:
            with telegram_errors(what=f"fleet probe {label}"):
                me = await account.client.get_me()
            row["authorized"] = me is not None
            row["user_id"] = getattr(me, "id", row.get("user_id"))
            row["username"] = getattr(me, "username", row.get("username"))
            row["probe"] = "ok"
    except Exception as exc:  # noqa: BLE001 - reported per account, never fatal
        row["probe"] = "failed"
        # `False` is a claim about the account; most failures here are claims
        # about the attempt. A held session lock, a flood wait or a dead proxy
        # says nothing about whether the account is signed in, and answering
        # "authorized: false" sends an operator looking for a login that is not
        # missing. Only a session Telegram itself rejected earns the false.
        row["authorized"] = False if isinstance(exc, (AuthRequired, SessionRevoked)) else None
        row["last_error"] = f"{type(exc).__name__}: {exc}"
        warnings.append(f"{label}: {type(exc).__name__}")


async def handle_fleet(ctx: OperationContext, params: FleetInput) -> Envelope:
    accounts = list_accounts(ctx)
    warnings: list[str] = []

    rows = [_describe(ctx, account) for account in accounts]
    if params.account is not None:
        rows = [row for row in rows if row["label"] == params.account]
    total = len(rows)
    if not params.include_excluded:
        rows = [row for row in rows if row["allowed"]]
    excluded = total - len(rows)
    if excluded:
        # Said out loud rather than left to the row count: an account missing
        # from this table because of policy looks identical to one that was
        # never registered.
        warnings.append(
            f"{excluded} account(s) hidden by safety.accounts; pass include_excluded to list them"
        )

    if params.probe:
        for row in rows:
            if row["allowed"]:
                await _probe(ctx, row, warnings)

    # `authorized` and `locked` are tri-state on the row — `_flag` returns None
    # for "nothing checked" precisely so an operator is not told an account is
    # signed out when nobody asked. Summing them flattens that None to zero and
    # puts the lie back: without `probe` nothing sets `authorized`, and `locked`
    # is never set at all, so both counters read a healthy fleet as "0 of N".
    # The unknowns are therefore counted separately rather than absorbed.
    counts = {
        "registered": total,
        "usable": sum(1 for row in rows if row["allowed"]),
        "authorized": sum(1 for row in rows if row["authorized"] is True),
        "authorized_unknown": sum(1 for row in rows if row["authorized"] is None),
        "locked": sum(1 for row in rows if row["locked"] is True),
        "locked_unknown": sum(1 for row in rows if row["locked"] is None),
    }

    return telegram_result(
        ctx,
        {"accounts": rows, "counts": counts},
        returned=len(rows),
        total=total,
        warnings=warnings,
        extra={"excluded": excluded} if excluded else None,
    )


FLEET = REGISTRY.register(
    Operation(
        name="fleet.list",
        cli=("fleet",),
        mcp_tool="telegram_fleet",
        summary="List configured accounts and whether each one is usable.",
        description=(
            "Reports every registered account with its sign-in state, lock state and "
            "whether the safety configuration permits using it. Local only unless "
            "`probe` is set."
        ),
        input_model=FleetInput,
        effect=Effect.READ,
        capability=None,  # No peer is touched; account access is gated by safety.accounts.
        handler=handle_fleet,  # type: ignore[arg-type]
        tags=("read", "accounts"),
    )
)
