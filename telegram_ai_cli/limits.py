"""Rolling rate limits that survive a restart.

An in-memory counter is not a limit. Restart the process and the budget is
fresh, so anything that can restart the process — including whatever talked the
agent into sending in the first place — can lift the ceiling. These counters
live in SQLite.

The reservation is taken *before* the network call and released only when the
call demonstrably had no effect. That ordering is not fussiness: checking first
and incrementing after the ``await`` leaves a window in which concurrent callers
all read the same count and all proceed, which is how a cap of five turns into
eight. Ask this project's sibling how it found that out.

A reservation that is never resolved stays counted. That is the safe direction:
an unknown outcome should consume budget, because the alternative is a failure
mode that refunds attempts nobody can prove did not happen.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum, auto

from .config import LimitsConfig
from .db import immediate
from .errors import LimitExceeded


class LimitKind(StrEnum):
    SEND = auto()
    JOIN = auto()
    ADMIN = auto()


@dataclass(frozen=True, slots=True)
class Reservation:
    """A consumed slot. Hold it, then either commit or release it."""

    event_id: int
    kind: LimitKind
    account: str
    target: str


class LimitStore:
    def __init__(self, conn: sqlite3.Connection, config: LimitsConfig) -> None:
        self._conn = conn
        self._config = config

    def _ceilings(self, kind: LimitKind) -> list[tuple[str, int, dict[str, str]]]:
        """Every ceiling this kind of action is subject to.

        Returned as (label, ceiling, scope) so a refusal can say which one was
        hit. "Rate limited" without naming the scope leaves the caller unable
        to tell whether to wait, use another account, or pick another target.
        """
        cfg = self._config
        match kind:
            case LimitKind.SEND:
                return [
                    ("per-account", cfg.sends_per_account, {"account": ""}),
                    ("per-target", cfg.sends_per_target, {"account": "", "target": ""}),
                    ("fleet-wide", cfg.sends_per_fleet, {}),
                ]
            case LimitKind.JOIN:
                return [("per-account", cfg.joins_per_account, {"account": ""})]
            case LimitKind.ADMIN:
                return [("per-account", cfg.admin_ops_per_account, {"account": ""})]

    def _count(
        self,
        kind: LimitKind,
        since: float,
        *,
        account: str | None,
        target: str | None,
    ) -> int:
        sql = "SELECT COUNT(*) FROM limit_events WHERE kind = ? AND created_at >= ?"
        params: list[object] = [str(kind), since]
        if account is not None:
            sql += " AND account = ?"
            params.append(account)
        if target is not None:
            sql += " AND target = ?"
            params.append(target)
        return int(self._conn.execute(sql, params).fetchone()[0])

    def reserve(self, kind: LimitKind, *, account: str, target: str) -> Reservation:
        """Take a slot, or refuse. Never blocks waiting for one to free up."""
        now = time.time()
        since = now - self._config.window_seconds

        with immediate(self._conn):
            for label, ceiling, scope in self._ceilings(kind):
                if ceiling <= 0:
                    raise LimitExceeded(
                        f"{kind} is disabled: the {label} ceiling is set to zero",
                        suggestion="Raise the limit in the configuration to permit this.",
                    )
                used = self._count(
                    kind,
                    since,
                    account=account if "account" in scope else None,
                    target=target if "target" in scope else None,
                )
                if used >= ceiling:
                    raise LimitExceeded(
                        f"{kind}: {label} limit of {ceiling} reached "
                        f"in the last {self._config.window_seconds}s",
                        retry_after=self._config.window_seconds,
                        details={"scope": label, "used": used, "ceiling": ceiling},
                    )

            cursor = self._conn.execute(
                "INSERT INTO limit_events (kind, account, target, created_at, committed) "
                "VALUES (?, ?, ?, ?, 0)",
                (str(kind), account, target, now),
            )
            event_id = int(cursor.lastrowid or 0)

        return Reservation(event_id=event_id, kind=kind, account=account, target=target)

    def commit(self, reservation: Reservation) -> None:
        """Mark the slot as definitely used. The row stays either way."""
        with immediate(self._conn):
            self._conn.execute(
                "UPDATE limit_events SET committed = 1 WHERE id = ?", (reservation.event_id,)
            )

    def release(self, reservation: Reservation) -> None:
        """Give the slot back.

        Only for calls that provably did not reach Telegram — a refusal by
        local policy, or an exception from a whitelist of error *classes*
        that cannot be raised after the request left. Matching on message text
        instead of class is how a refund gets handed out for a send that
        actually happened.
        """
        with immediate(self._conn):
            self._conn.execute(
                "DELETE FROM limit_events WHERE id = ? AND committed = 0",
                (reservation.event_id,),
            )

    def prune(self, *, older_than_seconds: int | None = None) -> int:
        """Drop history outside the window. Purely housekeeping."""
        horizon = time.time() - (older_than_seconds or self._config.window_seconds * 24)
        with immediate(self._conn):
            cursor = self._conn.execute("DELETE FROM limit_events WHERE created_at < ?", (horizon,))
        return cursor.rowcount or 0
