"""The account's own sessions — the list the threat model talks about.

The README reasons at length about a session being revoked from a phone, and
until now there was nothing in the tool that could *look* at the list. This is
that listing, and it is read-only in the strong sense: there is no operation
anywhere in this project that ends a session, and the tests below assert that
as a property rather than trusting the docstring.

The second half is about what a row may carry. A session names the address the
account was used from, and the repository's own privacy scan
(`tests/test_no_private_data.py`) treats an IP address as private data by
shape. So the host part never leaves — only the network it sits in, which is
what answers "is this session where my others are".
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_ai_cli_mcp.config import Settings
from telegram_ai_cli_mcp.errors import NotAllowlisted
from telegram_ai_cli_mcp.ops.sessions import SESSIONS, ip_prefix, session_summary
from telegram_ai_cli_mcp.opspec import REGISTRY, Effect
from telegram_ai_cli_mcp.safety import Capability, SafetyKernel
from telegram_ai_cli_mcp.untrusted import OPEN_MARKER, wrap_untrusted

CREATED = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)
ACTIVE = datetime(2026, 8, 23, 11, 15, tzinfo=UTC)

#: 0x7FFFFFFFFFFFFFFF-ish: whatever it is, it is the handle a terminating call
#: would take, and it must not appear in the output.
AUTH_HASH = 8123456789012345678


def authorization(**overrides) -> SimpleNamespace:
    fields = {
        "hash": AUTH_HASH,
        "device_model": "Pixel 9",
        "platform": "Android",
        "system_version": "SDK 35",
        "api_id": 6,
        "app_name": "Telegram Android",
        "app_version": "11.2.0",
        "date_created": CREATED,
        "date_active": ACTIVE,
        "ip": "198.51.100.34",
        "country": "Thailand",
        "region": "Bangkok",
        "current": False,
        "official_app": True,
        "password_pending": False,
        "encrypted_requests_disabled": False,
        "call_requests_disabled": False,
        "unconfirmed": False,
    }
    return SimpleNamespace(**{**fields, **overrides})


# --- what a row says ---------------------------------------------------------


def test_a_row_answers_which_device_from_where_and_when() -> None:
    row = session_summary(authorization())

    assert row["device"] == "Pixel 9"
    assert row["app"] == "Telegram Android"
    assert row["country"] == "Thailand"
    assert row["created"] == CREATED.isoformat()
    assert row["last_active"] == ACTIVE.isoformat()
    assert row["current"] is False


def test_the_current_session_is_marked_as_such() -> None:
    assert session_summary(authorization(current=True))["current"] is True


def test_a_session_telegram_has_not_confirmed_is_flagged() -> None:
    """The one field that says "somebody just logged in and it may not be you"."""
    assert session_summary(authorization(unconfirmed=True))["unconfirmed"] is True


def test_device_and_app_names_cross_the_trust_boundary() -> None:
    """Whoever signed in chose these strings — a custom client sets them freely."""
    wrapped = wrap_untrusted(session_summary(authorization()))

    assert wrapped["device"].startswith(OPEN_MARKER)
    assert wrapped["app"].startswith(OPEN_MARKER)


# --- what a row must never say ----------------------------------------------


def test_the_host_part_of_the_address_never_leaves() -> None:
    row = session_summary(authorization())

    assert row["ip_prefix"] == "198.51.x.x"
    assert "198.51.100.34" not in repr(row)
    assert "ip" not in row


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("198.51.100.34", "198.51.x.x"),
        ("203.0.113.7", "203.0.x.x"),
        ("2001:db8:85a3:1234::8a2e", "2001:db8:85a3::"),
        # `::` stands for a variable number of zero groups, so slicing the text
        # on ":" would produce `2001:db8:::` here and carry a host group through
        # on `fe80::1`. Raised by review; the address library builds it instead.
        ("2001:db8::1", "2001:db8::"),
        ("fe80::1", "fe80::"),
        ("::1", "::"),
        ("  198.51.100.34  ", "198.51.x.x"),
    ],
)
def test_only_the_network_survives(address: str, expected: str) -> None:
    assert ip_prefix(address) == expected


@pytest.mark.parametrize("value", ["", None, "not-an-address", "198.51.100.999"])
def test_an_address_that_does_not_parse_is_dropped_rather_than_echoed(value) -> None:
    """Echoing the raw string on a parse failure is how the full value gets out."""
    assert ip_prefix(value) is None


def test_the_termination_handle_is_not_in_the_payload() -> None:
    """`hash` is the argument the operation this project does not have would take.

    Printing it would be useless to a person — no client accepts it — and an
    invitation to add the call that does.
    """
    row = session_summary(authorization())

    assert "hash" not in row
    assert AUTH_HASH not in row.values()
    assert str(AUTH_HASH) not in repr(row)


# --- read-only, as a property ------------------------------------------------


def test_the_operation_reads_and_nothing_else() -> None:
    assert SESSIONS.effect is Effect.READ
    assert SESSIONS.plan_tool is None
    assert SESSIONS.planner is None
    assert SESSIONS.capability is Capability.READ_SESSIONS


def test_no_operation_anywhere_ends_a_session() -> None:
    """Terminating a session is out of scope for this project, deliberately.

    It is the natural next tool to reach for, which is exactly why the absence
    is asserted across the whole registry rather than left to review.
    """
    for op in REGISTRY.all():
        names = " ".join(filter(None, [op.name, op.mcp_tool, op.plan_tool, " ".join(op.cli)]))
        assert "terminate" not in names.lower()
        assert "revoke" not in names.lower()


def test_no_module_names_a_call_that_would_end_one() -> None:
    """The whole package, not just this operation's file.

    Scanning only `sessions.py` would miss the call appearing in some other
    handler later, which is exactly how a decision like this erodes (raised by
    review).
    """
    package = Path(__file__).resolve().parents[1] / "telegram_ai_cli_mcp"
    sources = sorted(package.rglob("*.py"))
    assert len(sources) > 10, "the scan walked nothing"

    for path in sources:
        text = path.read_text(encoding="utf-8")
        for forbidden in ("ResetAuthorization", "ResetWebAuthorization", "TerminateSession"):
            assert forbidden not in text, f"{path.name} names {forbidden}"


# --- the switch --------------------------------------------------------------


def test_reading_sessions_is_on_by_default_and_can_be_turned_off() -> None:
    assert SafetyKernel(Settings()).require_sessions() is None

    disabled = SafetyKernel(Settings(safety={"read": {"sessions": False}}))
    with pytest.raises(NotAllowlisted, match="sessions"):
        disabled.require_sessions()
