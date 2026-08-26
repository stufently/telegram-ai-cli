"""``telegram_sessions`` — every device this account is signed in on.

The threat model already reasons about a session being killed from a phone
(:doc:`../../README` on ``AuthKeyDuplicatedError``), and about a ``.session``
file being equivalent to holding the account. Both arguments assume somebody
can *see* the list of authorisations. Until this operation existed, nothing in
the tool could, and the answer to "is there a login here I do not recognise"
had to come from the Telegram app.

**Reading only, and terminating is not a gap to be filled later.** Ending a
session is the natural next tool to reach for; it is deliberately absent, in
this project and in this file, and the absence is asserted by
``tests/test_sessions.py`` across the whole registry rather than promised here.
A read tool that can log a device out is a read tool that can log *the owner's
phone* out, and there would be no plan step in the way.

**What a row may carry is a privacy decision, and it follows this project's own
rules rather than Telegram's.** :mod:`telegram_ai_cli_mcp.redact` masks values that
are recognisable by shape, on the grounds that a payload leaves this process and
does not come back; ``tests/test_no_private_data.py`` treats an IP address as
private data by that same standard. So:

* **The address is cut to its network** — the first two octets of an IPv4
  address, the first three hextets of an IPv6 one — and the host never leaves.
  That is the part that answers the question actually being asked ("is this
  session on the same network as my others, or on another continent"), and it
  is the part that stays true when the payload is pasted into a log, a ticket
  or another model's context. A full address would identify a home connection
  precisely; it would also collide with the redaction rules on the way out, and
  be delivered as ``[redacted:phone]`` — accurate about the danger, useless as
  an answer.
* **Country and region stay whole.** They are coarse by construction and they
  are the fields that make a rogue session obvious at a glance.
* **The authorisation hash is dropped entirely.** It is the handle the
  terminating call would take, no Telegram client accepts it from a person, and
  publishing an identifier whose only use is the operation this project refuses
  to have is an invitation to add that operation.

This is owner data, not a stranger's, and that is *why* it is trimmed rather
than why it would be safe to print: the owner is the one person who cannot be
warned by their own tool that their address ended up somewhere else.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from ..context import OperationContext
from ..envelope import Envelope
from ..errors import PolicyError
from ..opspec import REGISTRY, Effect, Operation
from ..safety import Capability
from ._client import open_account
from ._common import ReadInput, iso, telegram_errors, telegram_result

#: How much of an address survives. A /16 for IPv4 names the provider and the
#: rough locality; a /48 for IPv6 names the site rather than the machine, which
#: is what the trailing 64 bits identify.
_IPV4_KEPT_OCTETS = 2
_IPV6_PREFIX_BITS = 48


class SessionsInput(ReadInput):
    """Nothing but ``account``: the subject of this operation is the account itself."""


def ip_prefix(value: Any) -> str | None:
    """The network an address sits in, or nothing at all.

    Returns ``None`` for anything that does not parse, rather than echoing the
    string back. A malformed value is the one case where "pass it through
    unchanged" would hand over the full address it was supposed to trim.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None

    if address.version == 4:
        octets = address.exploded.split(".")[:_IPV4_KEPT_OCTETS]
        # Rendered with `x` rather than as `a.b.0.0/16`: a dotted quad of digits
        # is exactly the shape the phone rule in `redact` masks, and a masked
        # network is no answer at all.
        return ".".join(octets + ["x"] * (4 - _IPV4_KEPT_OCTETS))

    # Built by the address library rather than by splitting the text on ":".
    # `::` stands for a *variable* number of zero groups, so taking the first
    # three fields of `2001:db8::1` yields `2001:db8:::` — malformed, and on
    # `fe80::1` it would carry a host group through (raised by review).
    network = ipaddress.ip_network(f"{address}/{_IPV6_PREFIX_BITS}", strict=False)
    return network.network_address.compressed


def session_summary(authorization: Any) -> dict[str, Any]:
    """One authorisation, flattened — minus the parts that must not travel.

    Attributes are read with ``getattr`` rather than off a known class, so a
    field Telegram adds in a later layer degrades to ``None`` instead of raising
    on a machine running an older Telethon.
    """
    return {
        # The first question anybody asks of this list.
        "current": bool(getattr(authorization, "current", False)),
        "device": getattr(authorization, "device_model", None),
        "platform": getattr(authorization, "platform", None),
        "system_version": getattr(authorization, "system_version", None),
        "app": getattr(authorization, "app_name", None),
        "app_version": getattr(authorization, "app_version", None),
        # Which application credentials it was signed in with; a session created
        # by a script rather than by an official client shows up here.
        "api_id": getattr(authorization, "api_id", None),
        "official_app": bool(getattr(authorization, "official_app", False)),
        "country": getattr(authorization, "country", None),
        "region": getattr(authorization, "region", None),
        "ip_prefix": ip_prefix(getattr(authorization, "ip", None)),
        "created": iso(getattr(authorization, "date_created", None)),
        "last_active": iso(getattr(authorization, "date_active", None)),
        # Telegram marks a login it has not seen confirmed. It is the field that
        # says "somebody signed in and it may not have been you".
        "unconfirmed": bool(getattr(authorization, "unconfirmed", False)),
        "password_pending": bool(getattr(authorization, "password_pending", False)),
        "calls_disabled": bool(getattr(authorization, "call_requests_disabled", False)),
        "secret_chats_disabled": bool(getattr(authorization, "encrypted_requests_disabled", False)),
    }


def _require_sessions(ctx: OperationContext, *, action: str) -> None:
    """Ask the kernel, and write down a refusal like any other."""
    try:
        ctx.safety.require_sessions()
    except PolicyError as exc:
        ctx.audit.refusal(action=action, actor=ctx.actor, reason=exc.message)
        raise


def _order(rows: list[dict[str, Any]]) -> None:
    """This session first, then the most recently used.

    Two stable passes rather than one composite key: the second sort preserves
    the order the first established, and inverting an ISO timestamp to make a
    single ascending key is the kind of cleverness that is wrong once and then
    wrong forever.
    """
    rows.sort(key=lambda row: row["last_active"] or "", reverse=True)
    rows.sort(key=lambda row: 0 if row["current"] else 1)


async def handle_sessions(ctx: OperationContext, params: SessionsInput) -> Envelope:
    _require_sessions(ctx, action="account.sessions")

    async with open_account(ctx, params.account) as account:
        from telethon.tl.functions.account import GetAuthorizationsRequest

        with telegram_errors(what="account.sessions"):
            result = await account.client(GetAuthorizationsRequest())

    rows = [session_summary(item) for item in getattr(result, "authorizations", None) or []]
    _order(rows)

    warnings: list[str] = []
    unconfirmed = sum(1 for row in rows if row["unconfirmed"])
    if unconfirmed:
        warnings.append(
            f"{unconfirmed} session(s) Telegram has not seen confirmed — "
            "check them in the Telegram app before anything else"
        )

    return telegram_result(
        ctx,
        {
            "sessions": rows,
            # Telegram's own inactivity setting: sessions unused for this many
            # days are dropped by the server. Part of the answer to "how did
            # that old device disappear".
            "auto_terminate_after_days": getattr(result, "authorization_ttl_days", None),
            # Said in the payload rather than only in the docs, because the
            # obvious next request is "then close that one".
            "terminate_from": "Telegram app → Settings → Devices. This tool cannot end a session.",
        },
        account=account.label,
        returned=len(rows),
        total=len(rows),
        warnings=warnings,
    )


SESSIONS = REGISTRY.register(
    Operation(
        name="account.sessions",
        cli=("account", "sessions"),
        mcp_tool="telegram_sessions",
        summary="Which devices and apps this account is signed in on.",
        description=(
            "Device, application, coarse location, first sign-in and last activity for "
            "every authorisation, current one first. The address is cut to its network "
            "and the authorisation hash is not returned at all: nothing in this project "
            "ends a session, and that identifier has no other use."
        ),
        input_model=SessionsInput,
        effect=Effect.READ,
        capability=Capability.READ_SESSIONS,
        handler=handle_sessions,  # type: ignore[arg-type]
        tags=("read", "accounts", "security"),
    )
)
