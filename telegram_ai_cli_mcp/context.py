"""What every operation is handed, and how it gets built.

One object carries the policy kernel, the plan store, the rate limiter, the
audit log, the outbound ledger and the account fleet. Operations receive it rather than reaching for
globals, which is what makes them testable without a Telegram account: a test
builds a context over a temporary directory and an in-memory database.

``actor`` records which surface invoked the operation — ``cli`` or ``mcp``. It
lands in the audit log, and it is the only thing distinguishing two otherwise
identical actions. When a log is read after something went wrong, "who asked
for this" is the first question.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self

from .audit import AuditLog
from .config import Settings, load_settings
from .db import connect
from .ledger import OutboundLedger
from .limits import LimitStore
from .plans import PlanStore
from .safety import SafetyKernel
from .secretbox import SecretBox, load_key

if TYPE_CHECKING:  # pragma: no cover
    from .accounts.registry import AccountRegistry

Actor = Literal["cli", "mcp"]


@dataclass(slots=True)
class OperationContext:
    settings: Settings
    safety: SafetyKernel
    plans: PlanStore
    limits: LimitStore
    audit: AuditLog
    actor: Actor
    accounts: AccountRegistry | None = None
    #: What has already been sent. Optional in the signature only so that a test
    #: can build a context by hand without naming it; ``__post_init__`` derives
    #: one from the connection whenever there is a database at all, and the
    #: applier refuses rather than proceeding if it ends up without one.
    ledger: OutboundLedger | None = None
    _conn: sqlite3.Connection | None = None

    def __post_init__(self) -> None:
        if self.ledger is None and self._conn is not None:
            self.ledger = OutboundLedger(self._conn, self.settings.ledger)

    @classmethod
    def build(
        cls,
        *,
        actor: Actor,
        config_path: Path | None = None,
        settings: Settings | None = None,
    ) -> Self:
        settings = settings or load_settings(config_path)

        state_dir = settings.paths.state
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        conn = connect(state_dir / "state.db")
        key = load_key(settings.secrets, state_dir=state_dir)
        box = SecretBox(key) if key else None

        # Imported here rather than at module scope: the registry pulls in the
        # whole accounts package, and context is imported by everything. A
        # local import keeps that edge from becoming a cycle as either side
        # grows. Leaving ``accounts`` unset made every operation fail with
        # ACCOUNT_NOT_FOUND, since nothing else ever populated it.
        from .accounts.registry import AccountRegistry

        return cls(
            settings=settings,
            safety=SafetyKernel(settings),
            plans=PlanStore(conn, settings.plans, box),
            limits=LimitStore(conn, settings.limits),
            audit=AuditLog(settings.paths.audit_log, settings.audit),
            ledger=OutboundLedger(conn, settings.ledger),
            actor=actor,
            accounts=AccountRegistry.from_connection(conn, settings, box),
            _conn=conn,
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
