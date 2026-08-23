"""Two kinds of message that were never sent: drafts and scheduled sends.

Both are read-only here, and the tests below are mostly about what the
operations *refuse* to do. A draft is the most private thing in a Telegram
account — it is what someone started writing and decided not to send — so the
hard floor and the read policy are checked per row, exactly as they are for a
dialog listing, and a listing that quietly included Saved Messages would defeat
both.

Everything here runs without Telethon: the payload builders take plain objects
and a resolved :class:`~telegram_ai_cli.safety.PeerRef`, which is the same
choice ``_serialize.message_summary`` makes and for the same reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_ai_cli.config import Settings
from telegram_ai_cli.ops.pending import (
    DRAFTS,
    SCHEDULED,
    WHEN_ONLINE,
    Visibility,
    draft_summary,
    draft_visibility,
    scheduled_summary,
)
from telegram_ai_cli.opspec import Effect
from telegram_ai_cli.safety import Capability, PeerKind, PeerRef, SafetyKernel
from telegram_ai_cli.untrusted import OPEN_MARKER, wrap_untrusted

GROUP = PeerRef(peer_id=-1001234567890, kind=PeerKind.GROUP, title="Team")
USER = PeerRef(peer_id=4242, kind=PeerKind.USER, username="someone", title="Someone")
SAVED = PeerRef(peer_id=99, kind=PeerKind.SELF, title="Saved Messages")
SERVICE = PeerRef(peer_id=777000, kind=PeerKind.SERVICE, title="Telegram")

SAVED_AT = datetime(2026, 8, 23, 9, 30, tzinfo=UTC)


def draft(text: str = "half a sentence", **overrides) -> SimpleNamespace:
    fields = {
        "raw_text": text,
        "text": text,
        "date": SAVED_AT,
        "reply_to_msg_id": None,
        "link_preview": True,
    }
    return SimpleNamespace(**{**fields, **overrides})


def scheduled(when: datetime | None = None, **overrides) -> SimpleNamespace:
    fields = {
        "id": 17,
        "date": when or datetime(2026, 9, 1, 6, 0, tzinfo=UTC),
        "message": "the announcement",
        "out": True,
        "sender_id": 1,
        "sender": None,
        "reply_to": None,
        "edit_date": None,
        "media": None,
        "reactions": None,
        "views": None,
        "pinned": False,
        "forward": None,
    }
    return SimpleNamespace(**{**fields, **overrides})


def kernel(**safety) -> SafetyKernel:
    return SafetyKernel(Settings(safety=safety) if safety else Settings())


def most_permissive() -> SafetyKernel:
    """Everything an operator could switch on, switched on."""
    targets = [SAVED.peer_id, SERVICE.peer_id, "telegram", "saved messages"]
    return SafetyKernel(
        Settings(
            profile="plan",
            safety={
                "read": {
                    "chats": {"allow": targets},
                    "dms": {"allow": targets},
                    "allow_dialog_enumeration": True,
                    "enumerate_dms": True,
                }
            },
        )
    )


# --- drafts -----------------------------------------------------------------


def test_a_draft_is_reported_with_the_chat_it_was_written_in() -> None:
    row = draft_summary(draft(), chat=GROUP)

    assert row["text"] == "half a sentence"
    assert row["text_truncated"] is False
    assert row["chat"]["id"] == GROUP.peer_id
    assert row["chat"]["title"] == "Team"
    assert row["saved_at"] == SAVED_AT.isoformat()


def test_a_long_draft_says_that_it_was_cut() -> None:
    row = draft_summary(draft("x" * 9000), chat=GROUP)

    assert row["text_truncated"] is True
    assert len(row["text"]) < 9000


def test_a_reply_draft_keeps_what_it_is_replying_to() -> None:
    row = draft_summary(draft(reply_to_msg_id=88), chat=GROUP)
    assert row["reply_to_msg_id"] == 88


def test_draft_text_crosses_the_trust_boundary_like_any_other_body() -> None:
    wrapped = wrap_untrusted(draft_summary(draft(), chat=GROUP))

    assert wrapped["text"].startswith(OPEN_MARKER)
    assert wrapped["chat"]["title"].startswith(OPEN_MARKER)


@pytest.mark.parametrize("closed", [SAVED, SERVICE], ids=lambda p: str(p.kind))
def test_no_configuration_lists_a_draft_from_a_closed_chat(closed: PeerRef) -> None:
    """The floor, applied per row — invariant 3, in the listing that would leak it.

    Saved Messages is where a person drafts the things they would never send,
    and 777000 carries login codes. Neither is enumerated, not even as a count:
    a withheld tally would still say a draft exists there.
    """
    verdict = draft_visibility(most_permissive(), closed, include_private=True)
    assert verdict is Visibility.CLOSED


def test_a_group_draft_is_visible_with_no_configuration_at_all() -> None:
    assert draft_visibility(kernel(), GROUP, include_private=False) is Visibility.SHOW


def test_a_private_draft_needs_both_switches() -> None:
    """Enumeration first, then the DM allowlist — an empty one still means none."""
    assert draft_visibility(kernel(), USER, include_private=False) is Visibility.PRIVATE
    assert draft_visibility(kernel(), USER, include_private=True) is Visibility.WITHHELD

    allowed = kernel(read={"dms": {"allow": [USER.peer_id]}, "enumerate_dms": True})
    assert draft_visibility(allowed, USER, include_private=True) is Visibility.SHOW


def test_a_denied_group_draft_is_withheld_not_shown() -> None:
    denied = kernel(read={"chats": {"deny": [GROUP.peer_id]}})
    assert draft_visibility(denied, GROUP, include_private=False) is Visibility.WITHHELD


# --- scheduled --------------------------------------------------------------


def test_a_scheduled_message_says_when_it_will_be_sent() -> None:
    row = scheduled_summary(scheduled(), chat=GROUP)

    assert row["scheduled_for"] == "2026-09-01T06:00:00+00:00"
    assert row["send_when_online"] is False
    assert row["text"] == "the announcement"


def test_send_when_online_is_not_a_date_in_2038() -> None:
    """Telegram encodes "when they come online" as a sentinel timestamp.

    Rendered as a date it reads as 19 January 2038, which is a lie about a
    message that will be sent the moment the other person appears.
    """
    sentinel = datetime.fromtimestamp(WHEN_ONLINE, tz=UTC)
    row = scheduled_summary(scheduled(sentinel), chat=USER)

    assert row["send_when_online"] is True
    assert row["scheduled_for"] is None


def test_a_scheduled_message_gets_no_permalink() -> None:
    """Its id belongs to a separate sequence, so a t.me link would address
    some other message in the same chat."""
    row = scheduled_summary(scheduled(), chat=GROUP)
    assert row["link"] is None


# --- both, on every surface --------------------------------------------------


@pytest.mark.parametrize("op", [DRAFTS, SCHEDULED], ids=lambda o: o.name)
def test_neither_operation_can_change_anything(op) -> None:
    assert op.effect is Effect.READ
    assert op.is_remote_write is False
    assert op.plan_tool is None
    assert op.planner is None
    assert op.mcp_tool and op.mcp_tool.startswith("telegram_")


def test_the_declared_capabilities_match_what_is_read() -> None:
    assert DRAFTS.capability is Capability.ENUMERATE
    assert SCHEDULED.capability is Capability.READ_CHAT


def test_the_module_names_no_request_that_would_change_a_draft_or_a_send() -> None:
    """Deleting a draft or firing a scheduled message is one call away.

    Named here so the refusal is a property of the file rather than a promise
    in its docstring: none of these appears in the module, by name or
    otherwise.
    """
    source = Path(SCHEDULED.handler.__module__.replace(".", "/") + ".py")
    text = (Path(__file__).resolve().parents[1] / source).read_text(encoding="utf-8")

    for forbidden in (
        "SaveDraft",
        "ClearAllDrafts",
        "DeleteScheduledMessages",
        "SendScheduledMessages",
    ):
        assert forbidden not in text
