"""The audit log, which is the insurance the approval boundary rests on.

Two phases, because a log written only after success loses exactly the case it
exists for: the send that went out and then the process died. ``attempt`` is on
disk before the RPC leaves, ``outcome`` follows, and both carry the same id so
an attempt with no outcome reads as unresolved rather than as absent.

The log is also a file a human greps and a program parses, which makes it a
forgery target: message text that can inject a newline can write records of its
own. Every value goes through ``sanitize_line`` on the way in.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from telegram_ai_cli_mcp.audit import AuditLog
from telegram_ai_cli_mcp.config import AuditConfig

BODY = "hello, this is the message body"


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for key in list(os.environ):
        if key.startswith("TGAI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "audit.jsonl"


def read_records(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line]


# --- the two phases --------------------------------------------------------


def test_attempt_is_written_before_anything_happens_and_returns_its_id(log_path: Path) -> None:
    log = AuditLog(log_path, AuditConfig())
    event_id = log.attempt(
        action="message.send", account="work", actor="cli", peer_id=-4242, plan_id="p" * 32
    )

    assert event_id
    records = read_records(log_path)
    assert len(records) == 1
    assert records[0]["event"] == "attempt"
    assert records[0]["event_id"] == event_id
    assert records[0]["action"] == "message.send"
    assert records[0]["peer_id"] == -4242
    assert records[0]["ts"].endswith("Z")


def test_outcome_closes_the_attempt_with_the_same_id(log_path: Path) -> None:
    log = AuditLog(log_path, AuditConfig())
    event_id = log.attempt(action="message.send", account="work", actor="cli")
    log.outcome(event_id, status="applied", message_id=7)

    attempt, outcome = read_records(log_path)
    assert attempt["event_id"] == outcome["event_id"] == event_id
    assert outcome["event"] == "outcome"
    assert outcome["status"] == "applied"
    assert outcome["message_id"] == 7


def test_every_attempt_gets_a_distinct_id(log_path: Path) -> None:
    log = AuditLog(log_path, AuditConfig())
    ids = {log.attempt(action="a", account="work", actor="cli") for _ in range(5)}
    assert len(ids) == 5


def test_an_attempt_without_an_outcome_stays_visible_as_unresolved(log_path: Path) -> None:
    """This is the shape of a crash mid-send, and it must be readable as one."""
    log = AuditLog(log_path, AuditConfig())
    first = log.attempt(action="message.send", account="work", actor="cli")
    second = log.attempt(action="message.send", account="work", actor="cli")
    log.outcome(second, status="applied")

    records = read_records(log_path)
    closed = {r["event_id"] for r in records if r["event"] == "outcome"}
    assert first not in closed
    assert second in closed


@pytest.mark.parametrize("status", ["applied", "failed", "unknown"])
def test_an_unknown_outcome_is_recorded_as_itself(log_path: Path, status: str) -> None:
    """ "Unknown" is not a failure: a reader must be able to tell them apart."""
    log = AuditLog(log_path, AuditConfig())
    event_id = log.attempt(action="message.send", account="work", actor="cli")
    log.outcome(event_id, status=status, error_code="FLOOD_WAIT", detail="no answer")

    outcome = read_records(log_path)[1]
    assert outcome["status"] == status
    assert outcome["error_code"] == "FLOOD_WAIT"


# --- bodies ----------------------------------------------------------------


def test_the_body_is_recorded_as_a_hash_and_a_length(log_path: Path) -> None:
    """An audit trail that mirrors every conversation is a second archive."""
    log = AuditLog(log_path, AuditConfig())
    log.attempt(action="message.send", account="work", actor="cli", body=BODY)

    record = read_records(log_path)[0]
    assert "body" not in record
    assert BODY not in log_path.read_text(encoding="utf-8")
    assert record["body_len"] == len(BODY)
    assert len(record["body_sha256"]) == 64


def test_the_same_body_hashes_the_same_way(log_path: Path) -> None:
    """The hash has to be worth something: it is how two sends are compared."""
    log = AuditLog(log_path, AuditConfig())
    log.attempt(action="a", account="work", actor="cli", body=BODY)
    log.attempt(action="a", account="work", actor="cli", body=BODY)
    log.attempt(action="a", account="work", actor="cli", body=BODY + "!")

    first, second, third = read_records(log_path)
    assert first["body_sha256"] == second["body_sha256"]
    assert third["body_sha256"] != first["body_sha256"]


def test_bodies_are_written_only_when_explicitly_asked_for(log_path: Path) -> None:
    log = AuditLog(log_path, AuditConfig(include_bodies=True))
    log.attempt(action="message.send", account="work", actor="cli", body=BODY)

    record = read_records(log_path)[0]
    assert record["body"] == BODY
    assert record["body_sha256"]


# --- forgery ---------------------------------------------------------------


def test_a_newline_in_a_value_cannot_forge_a_record(log_path: Path) -> None:
    forged = 'x\n{"event": "outcome", "status": "applied"}'
    log = AuditLog(log_path, AuditConfig())
    event_id = log.attempt(
        action="message.send", account="work", actor="cli", extra={"note": forged}
    )
    log.outcome(event_id, status="failed", detail=forged)

    records = read_records(log_path)
    assert len(records) == 2
    assert [r["event"] for r in records] == ["attempt", "outcome"]
    assert "\n" not in records[0]["extra"]["note"]
    assert "\n" not in records[1]["detail"]


def test_escape_sequences_never_reach_the_file(log_path: Path) -> None:
    """A record is read in a terminal too; it must not be able to redraw it."""
    log = AuditLog(log_path, AuditConfig(include_bodies=True))
    log.attempt(
        action="message.send\x1b[2K",
        account="work\r",
        actor="cli",
        body="body\x1b[31mred",
        extra={"peer": "title\x1b]0;pwned\x07"},
    )

    text = log_path.read_text(encoding="utf-8")
    assert "\x1b" not in text
    assert "\r" not in text
    assert "[2K" not in text


def test_long_values_are_truncated_rather_than_stored_whole(log_path: Path) -> None:
    log = AuditLog(log_path, AuditConfig())
    event_id = log.attempt(action="a" * 500, account="work", actor="cli")
    log.outcome(event_id, status="failed", detail="d" * 2000)

    attempt, outcome = read_records(log_path)
    assert len(attempt["action"]) == 64
    assert len(outcome["detail"]) == 500


# --- refusals --------------------------------------------------------------


def test_refusals_are_recorded_in_the_same_log(log_path: Path) -> None:
    """A run of refusals against one chat is what probing looks like."""
    log = AuditLog(log_path, AuditConfig())
    log.refusal(
        action="message.send",
        actor="mcp",
        reason="send: chat is not on the allow list",
        peer_id=-4242,
    )

    record = read_records(log_path)[0]
    assert record["event"] == "refused"
    assert record["actor"] == "mcp"
    assert record["peer_id"] == -4242
    assert "allow list" in record["reason"]


# --- the file itself -------------------------------------------------------


def test_the_log_is_created_private(log_path: Path) -> None:
    log = AuditLog(log_path, AuditConfig())
    log.attempt(action="a", account="work", actor="cli")

    assert log_path.stat().st_mode & 0o777 == 0o600
    assert log_path.parent.stat().st_mode & 0o077 == 0


def test_records_are_appended_not_overwritten(log_path: Path) -> None:
    AuditLog(log_path, AuditConfig()).attempt(action="a", account="work", actor="cli")
    # A second process would construct its own AuditLog over the same path.
    AuditLog(log_path, AuditConfig()).attempt(action="b", account="work", actor="cli")

    assert [r["action"] for r in read_records(log_path)] == ["a", "b"]


def test_disabling_the_log_writes_nothing(log_path: Path) -> None:
    log = AuditLog(log_path, AuditConfig(enabled=False))
    event_id = log.attempt(action="a", account="work", actor="cli")
    log.outcome(event_id, status="applied")

    assert event_id
    assert not log_path.exists()


def test_the_log_rotates_instead_of_growing_without_end(log_path: Path) -> None:
    log = AuditLog(log_path, AuditConfig(rotate_bytes=1024))
    for _ in range(20):
        log.attempt(action="message.send", account="work", actor="cli", body=BODY)

    rotated = sorted(log_path.parent.glob("audit.*.jsonl"))
    assert rotated, "the log grew past rotate_bytes without rotating"
    # The live file holds only what was written since the last rotation, and
    # what was moved aside is still valid JSON lines.
    assert len(read_records(log_path)) < 20
    assert read_records(rotated[-1])
