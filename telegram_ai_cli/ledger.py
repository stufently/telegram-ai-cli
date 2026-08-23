"""A record of what has already been sent, so it is not sent again.

:mod:`telegram_ai_cli.audit` writes down what happened. Nothing in it stops the
same thing happening twice — and the failure this module exists for is not a
race but an *amnesia*. An agent in a new session, with no memory of the last
one, plans and applies a message it already sent. The person on the other end
sees the same words twice and has no way to tell which run produced them; the
audit log holds both, which is a record of the mistake rather than a defence
against it.

So a fingerprint of every outbound action is written to the same SQLite file
that holds the plans and the rate-limit history, and the applier consults it
before it consults anything else. Three decisions carry the weight.

**What goes into the fingerprint is what the recipient sees.** Who sent it (the
account), what kind of act it was (the operation), who received it (the numeric
peer id — a handle can change hands, and the applier re-checks the id for the
same reason), the words after cosmetic whitespace is normalised away, the sha256
of any attachment — the one :func:`telegram_ai_cli.outbox.resolve_outbound`
already computed, rather than a second digest of the same bytes — and the
choices that change how any of it renders: which message a reply quotes, whether
a link expands into a preview, whether a file arrives as a compressed photo or as
the original document, under what name, and whether a forward carries its
author's name. Two sends that differ in one of those are two different things on
the screen, so they are two different actions here.

The one thing left out is *notification*: ``silent`` decides whether a phone
makes a sound, not what the message says, and a person who resent the same words
without the chime has still sent them twice. Nothing is lost by excluding it,
because the duplicate this catches is a re-run, and a re-run does not flip flags.

**Case is left alone.** A re-run emits byte-identical text; deciding that "OK"
and "ok" are the same message is a judgement about somebody's writing, and every
false refusal teaches whoever hits it to set the repeat flag by reflex — which
is how the check stops working.

**The window is short by design.** The duplicate this catches is a *re-run*, not
a recurrence. A message on a daily rhythm is a legitimate repeat and must never
be caught; six hours cannot reach one even with hours of drift, while still
covering a restarted process, a retried script and a fresh session.

What this does not claim: it is not an idempotency framework. Two processes
applying two *different* plans with the same fingerprint at the same instant can
both pass the check, because the check and the record sit either side of the RPC
rather than inside one transaction. The same plan applied twice is already
impossible — :meth:`telegram_ai_cli.plans.PlanStore.claim` is one conditional
UPDATE — and the gap that remains is the one an approving human is standing in.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

from .config import LedgerConfig
from .db import immediate

#: Bumping this invalidates every stored fingerprint at once, which is the point:
#: if what goes into the digest changes, old rows describe a different question
#: and must stop matching rather than match wrongly.
FINGERPRINT_VERSION = 1

#: The operations that put new, visible words into somebody's chat — the only
#: ones where a repeat is a thing a person cannot unsee. Reacting, pinning,
#: joining and marking read are either idempotent at Telegram's end or invisible
#: to anybody but the account owner, and putting them under a duplicate check
#: would buy nothing and refuse a lot.
LEDGERED_OPERATIONS = frozenset(
    {
        "message.send",
        "message.reply",
        "message.send_file",
        "message.forward",
    }
)


def normalise_body(text: str | None) -> str:
    """The words, with cosmetic whitespace taken out of the comparison.

    NFC first, because two encodings of the same accented character are the same
    character. Then, *within each line*, runs of spaces and tabs collapse to one
    and the ends are trimmed; blank lines at either end go. A re-run that emits
    the same text with a trailing newline is the same message, and a check that
    could not see that would be bypassed by accident rather than on purpose.

    Line breaks survive, because Telegram renders them: "a\nb" and "a b" are two
    different messages on the screen, and calling them one would be this module
    deciding it knows better than the person who wrote them.
    """
    if not text:
        return ""
    lines = [" ".join(line.split()) for line in unicodedata.normalize("NFC", text).splitlines()]
    return "\n".join(lines).strip("\n")


def fingerprint(
    *,
    account: str,
    operation: str,
    peer_id: int,
    body: str | None = None,
    file_sha256: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Digest the identity of one outbound action.

    ``peer_id`` is required to be an integer rather than accepting a handle:
    ``@someone`` can change hands between one send and the next, and a ledger
    keyed on the handle would call two different people one recipient.
    """
    if not isinstance(peer_id, int) or isinstance(peer_id, bool):
        raise TypeError("peer_id must be the numeric peer id, not a handle")
    payload = {
        "v": FINGERPRINT_VERSION,
        "account": account,
        "operation": operation,
        "peer_id": peer_id,
        "body": normalise_body(body),
        "file_sha256": file_sha256,
        "extra": extra or {},
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """A row written before the request left. Keep it, then forget it or not."""

    row_id: int
    digest: str


@dataclass(frozen=True, slots=True)
class PriorSend:
    """An identical action that already went out, described well enough to act on."""

    plan_id: str
    sent_at: float

    def age_seconds(self, *, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.sent_at)


class OutboundLedger:
    def __init__(self, conn: sqlite3.Connection, config: LedgerConfig) -> None:
        self._conn = conn
        self._config = config

    @property
    def window_seconds(self) -> int:
        return self._config.window_seconds

    def find_recent(self, digest: str, *, now: float | None = None) -> PriorSend | None:
        """The most recent identical action inside the window, if there is one.

        The most recent rather than the first: a repeat that somebody approved
        on purpose is still the last time these words went out, and naming an
        older one would send a person looking at the wrong plan.
        """
        window = self._config.window_seconds
        if window <= 0:
            # Configured off. Said here rather than by never writing rows, so
            # that turning it back on works on the history already collected.
            return None
        moment = now if now is not None else time.time()
        row = self._conn.execute(
            "SELECT plan_id, sent_at FROM outbound_ledger "
            "WHERE fingerprint = ? AND sent_at >= ? ORDER BY sent_at DESC LIMIT 1",
            (digest, moment - window),
        ).fetchone()
        if row is None:
            return None
        return PriorSend(plan_id=row["plan_id"], sent_at=float(row["sent_at"]))

    def record(
        self,
        *,
        digest: str,
        account: str,
        operation: str,
        peer_id: int | None,
        plan_id: str,
    ) -> LedgerEntry:
        """Write the row, before the request leaves.

        Pessimistic on purpose, and for the same reason the audit attempt is
        written first: a row that exists for a send that never happened costs
        one refusal a person can override explicitly, while a missing row for a
        send that did happen costs the duplicate this module exists to prevent.
        There is no unique constraint on the fingerprint — a deliberate repeat is
        a second send, and the one after it still has to be caught.
        """
        now = time.time()
        with immediate(self._conn):
            cursor = self._conn.execute(
                "INSERT INTO outbound_ledger "
                "(fingerprint, account, operation, peer_id, plan_id, sent_at) "
                "VALUES (?,?,?,?,?,?)",
                (digest, account, operation, peer_id, plan_id, now),
            )
        return LedgerEntry(row_id=int(cursor.lastrowid or 0), digest=digest)

    def settle(self, entry: LedgerEntry) -> None:
        """Move the row's timestamp to the moment the send actually completed.

        The row is written before the request leaves, so until this is called
        ``sent_at`` is when the attempt *started*. For a sentence that is the
        same instant; for a hundred-megabyte upload it is minutes earlier, and a
        window measured from the start would expire before the file had finished
        arriving. Not called on an unknown outcome: the moment such a send
        reached Telegram, if it did, is exactly what nobody knows, and the
        earlier timestamp is the conservative one to keep.
        """
        with immediate(self._conn):
            self._conn.execute(
                "UPDATE outbound_ledger SET sent_at = ? WHERE id = ?",
                (time.time(), entry.row_id),
            )

    def forget(self, entry: LedgerEntry) -> None:
        """Drop a row for an action Telegram provably refused.

        Called in exactly the places the rate-limit slot is released, and for
        exactly the same reason: those are the error classes that cannot be
        raised after the request took effect. An unknown outcome keeps its row,
        because a send nobody can prove did not happen is one a later identical
        plan should still be stopped by.
        """
        with immediate(self._conn):
            self._conn.execute("DELETE FROM outbound_ledger WHERE id = ?", (entry.row_id,))

    def prune(self, *, older_than_seconds: int | None = None) -> int:
        """Drop rows the window can no longer see. Housekeeping only."""
        horizon = time.time() - (older_than_seconds or self._config.window_seconds)
        with immediate(self._conn):
            cursor = self._conn.execute("DELETE FROM outbound_ledger WHERE sent_at < ?", (horizon,))
        return cursor.rowcount or 0
