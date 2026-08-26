"""Plans: an intention recorded now, carried out by a person later.

A plan is what a tool call produces instead of an effect. It holds the resolved
target, the exact bytes that would be sent, and the preconditions that were
true when it was written. Nothing here talks to Telegram.

Two details carry most of the weight.

**The claim is atomic.** ``pending → applying`` is a conditional UPDATE, so two
processes racing to apply the same plan cannot both win. Reading the state and
then writing it would leave a window where both see ``pending``, and the cost of
losing that race is a message sent twice.

**Timeouts are not failures.** If the request left and the answer never came
back, the plan lands in ``unknown_outcome`` and stays there. Marking it failed
invites a retry, and a retry after a send that actually succeeded is how one
message becomes two. A human has to look.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Any

from .config import PlansConfig
from .db import immediate
from .errors import (
    InvalidInput,
    LimitExceeded,
    PlanExpired,
    PlanNotFound,
    PlanNotPending,
)
from .secretbox import SecretBox


class PlanState(StrEnum):
    PENDING = auto()
    APPLYING = auto()
    APPLIED = auto()
    FAILED = auto()
    #: The request left; the outcome is genuinely unknown. Terminal until a
    #: person resolves it. Never retried automatically.
    UNKNOWN_OUTCOME = auto()
    REJECTED = auto()
    EXPIRED = auto()


TERMINAL_STATES = frozenset(
    {
        PlanState.APPLIED,
        PlanState.FAILED,
        PlanState.UNKNOWN_OUTCOME,
        PlanState.REJECTED,
        PlanState.EXPIRED,
    }
)


@dataclass(frozen=True, slots=True)
class Plan:
    plan_id: str
    operation: str
    account: str
    params: dict[str, Any]
    preconditions: dict[str, Any]
    summary: str
    state: PlanState
    created_at: float
    expires_at: float
    applied_at: float | None = None
    outcome: dict[str, Any] | None = None

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at


class PlanStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: PlansConfig,
        box: SecretBox | None = None,
    ) -> None:
        if config.encrypt_bodies and box is None:
            # Fail closed. Silently storing the body in the clear because no
            # key was supplied is the one outcome the setting exists to
            # prevent, and nothing downstream can tell the two apart.
            raise InvalidInput(
                "plans.encrypt_bodies is on but no encryption key is available",
                suggestion=(
                    "Set TGAI_SECRET_KEY (or let the tool generate a key file), "
                    "or turn plans.encrypt_bodies off."
                ),
            )
        self._conn = conn
        self._config = config
        self._box = box

    # -- encoding ----------------------------------------------------------

    def _encode(self, params: dict[str, Any]) -> bytes:
        blob = json.dumps(params, ensure_ascii=False, sort_keys=True)
        if self._config.encrypt_bodies and self._box is not None:
            blob = self._box.encrypt(blob)
        return blob.encode("utf-8")

    def _decode(self, blob: bytes) -> dict[str, Any]:
        text = blob.decode("utf-8")
        if self._box is not None:
            text = self._box.decrypt(text)
        return json.loads(text)

    def _row_to_plan(self, row: sqlite3.Row) -> Plan:
        return Plan(
            plan_id=row["plan_id"],
            operation=row["operation"],
            account=row["account"],
            params=self._decode(row["params_json"]),
            preconditions=json.loads(row["preconditions"]),
            summary=row["summary"],
            state=PlanState(row["state"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            applied_at=row["applied_at"],
            outcome=json.loads(row["outcome_json"]) if row["outcome_json"] else None,
        )

    # -- writing -----------------------------------------------------------

    def create(
        self,
        *,
        operation: str,
        account: str,
        params: dict[str, Any],
        preconditions: dict[str, Any],
        summary: str,
    ) -> Plan:
        """Record an intention. Sends nothing."""
        now = time.time()
        # 128 bits from a CSPRNG. Sequential ids would let a caller reference a
        # plan it never saw, and guessing one is the first step to applying it.
        plan_id = secrets.token_hex(16)
        expires_at = now + self._config.ttl_seconds

        with immediate(self._conn):
            # Retire plans past their TTL before counting. Without this a plan
            # that expired but was never claimed keeps occupying a slot of the
            # quota, so the ceiling is reached by plans that can no longer be
            # applied at all — and only an unrelated claim() or expire_stale()
            # would ever free it.
            self._conn.execute(
                "UPDATE plans SET state = ? WHERE state = ? AND expires_at <= ?",
                (str(PlanState.EXPIRED), str(PlanState.PENDING), now),
            )
            pending = self._conn.execute(
                "SELECT COUNT(*) FROM plans WHERE state = ?", (str(PlanState.PENDING),)
            ).fetchone()[0]
            if pending >= self._config.max_pending:
                raise LimitExceeded(
                    f"{pending} plans are already awaiting review "
                    f"(limit {self._config.max_pending})",
                    suggestion="Apply or reject the pending plans: tg-ai plan list",
                )
            self._conn.execute(
                "INSERT INTO plans (plan_id, operation, account, params_json, preconditions,"
                " summary, state, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    plan_id,
                    operation,
                    account,
                    self._encode(params),
                    json.dumps(preconditions, ensure_ascii=False, sort_keys=True),
                    summary,
                    str(PlanState.PENDING),
                    now,
                    expires_at,
                ),
            )

        return Plan(
            plan_id=plan_id,
            operation=operation,
            account=account,
            params=params,
            preconditions=preconditions,
            summary=summary,
            state=PlanState.PENDING,
            created_at=now,
            expires_at=expires_at,
        )

    def claim(self, plan_id: str) -> Plan:
        """Move ``pending → applying`` atomically, or refuse.

        The conditional UPDATE is the whole mechanism: only the caller whose
        write actually changed a row may proceed.
        """
        # Raising out of ``immediate()`` rolls the transaction back, so the
        # expiry has to be recorded and reported in that order: mark inside the
        # transaction, leave it normally so the mark commits, raise afterwards.
        # Raising in place left the plan pending for ever, still holding a slot
        # of the pending quota.
        expired = False
        with immediate(self._conn):
            # Read the clock after the write lock is held. BEGIN IMMEDIATE can
            # wait behind another writer, and a timestamp taken before that
            # wait lets a plan whose TTL ran out *during* the wait still be
            # claimed and applied.
            now = time.time()
            row = self._conn.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
            if row is None:
                raise PlanNotFound(f"no plan with id {plan_id}")

            state = PlanState(row["state"])
            if state is not PlanState.PENDING:
                raise PlanNotPending(
                    f"plan {plan_id} is {state}, not pending",
                    suggestion="Only a pending plan can be applied.",
                )
            if now >= row["expires_at"]:
                self._conn.execute(
                    "UPDATE plans SET state = ? WHERE plan_id = ? AND state = ?",
                    (str(PlanState.EXPIRED), plan_id, str(PlanState.PENDING)),
                )
                expired = True
            else:
                cursor = self._conn.execute(
                    "UPDATE plans SET state = ? WHERE plan_id = ? AND state = ?",
                    (str(PlanState.APPLYING), plan_id, str(PlanState.PENDING)),
                )
                if cursor.rowcount != 1:
                    raise PlanNotPending(f"plan {plan_id} was claimed by someone else")
                row = self._conn.execute(
                    "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
                ).fetchone()

        if expired:
            raise PlanExpired(f"plan {plan_id} expired; re-create it if it is still wanted")
        return self._row_to_plan(row)

    def finish(
        self,
        plan_id: str,
        *,
        state: PlanState,
        outcome: dict[str, Any] | None = None,
    ) -> None:
        """Close a claimed plan with its result."""
        if state not in TERMINAL_STATES:
            raise ValueError(f"{state} is not a terminal state")
        with immediate(self._conn):
            self._conn.execute(
                "UPDATE plans SET state = ?, applied_at = ?, outcome_json = ? WHERE plan_id = ?",
                (
                    str(state),
                    time.time(),
                    json.dumps(outcome, ensure_ascii=False) if outcome else None,
                    plan_id,
                ),
            )

    def reject(self, plan_id: str) -> None:
        """Decline a plan. Only a pending plan can be rejected."""
        with immediate(self._conn):
            cursor = self._conn.execute(
                "UPDATE plans SET state = ? WHERE plan_id = ? AND state = ?",
                (str(PlanState.REJECTED), plan_id, str(PlanState.PENDING)),
            )
            if cursor.rowcount != 1:
                raise PlanNotPending(f"plan {plan_id} is not pending")

    def expire_stale(self) -> int:
        """Mark plans past their TTL. Housekeeping; ``claim`` also checks."""
        with immediate(self._conn):
            cursor = self._conn.execute(
                "UPDATE plans SET state = ? WHERE state = ? AND expires_at <= ?",
                (str(PlanState.EXPIRED), str(PlanState.PENDING), time.time()),
            )
        return cursor.rowcount or 0

    # -- reading -----------------------------------------------------------

    def get(self, plan_id: str) -> Plan:
        row = self._conn.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
        if row is None:
            raise PlanNotFound(f"no plan with id {plan_id}")
        return self._row_to_plan(row)

    def list(self, *, state: PlanState | None = None, limit: int = 100) -> list[Plan]:
        if state is None:
            rows = self._conn.execute(
                "SELECT * FROM plans ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM plans WHERE state = ? ORDER BY created_at DESC LIMIT ?",
                (str(state), limit),
            ).fetchall()
        return [self._row_to_plan(row) for row in rows]
