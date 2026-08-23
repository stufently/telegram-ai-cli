"""One SQLite file holds plans and rate-limit history.

WAL so a reader never blocks the writer, and ``BEGIN IMMEDIATE`` for anything
that reads a value and then writes based on it. SQLite's default deferred
transaction takes the write lock late, which means two processes can both pass
a check and only then discover they disagree — precisely the race a limit or a
plan state machine must not have.

``PRAGMA foreign_keys`` and ``synchronous=FULL`` are set per connection because
SQLite scopes them that way; setting them once at creation would silently not
apply to the next process to open the file.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id        TEXT PRIMARY KEY,
    operation      TEXT NOT NULL,
    account        TEXT NOT NULL,
    params_json    BLOB NOT NULL,
    preconditions  TEXT NOT NULL,
    summary        TEXT NOT NULL,
    state          TEXT NOT NULL,
    created_at     REAL NOT NULL,
    expires_at     REAL NOT NULL,
    applied_at     REAL,
    outcome_json   TEXT
);
CREATE INDEX IF NOT EXISTS plans_state_idx ON plans(state, expires_at);

-- The fleet. The label is the primary key rather than a surrogate id because it
-- is what every command, plan and audit line names; a second identifier would
-- only create a way for the two to disagree.
--
-- api_hash and proxy_url are written through SecretBox (``enc:v1:`` prefix) when
-- a key is configured. session_path holds a path, never the session material
-- itself: an auth key belongs in a 0600 file, not in a TEXT column that any
-- backup or ``.dump`` carries off wholesale.
CREATE TABLE IF NOT EXISTS accounts (
    label        TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    session_path TEXT,
    phone        TEXT,
    api_id       INTEGER,
    api_hash     TEXT,
    proxy_url    TEXT,
    status       TEXT NOT NULL DEFAULT 'new',
    user_id      INTEGER,
    last_error   TEXT,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS accounts_status_idx ON accounts(status);

CREATE TABLE IF NOT EXISTS limit_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    account    TEXT NOT NULL,
    target     TEXT NOT NULL,
    created_at REAL NOT NULL,
    committed  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS limit_events_idx ON limit_events(kind, created_at);
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open the state database, creating it with private permissions.

    The file carries unsent message bodies and a record of who was contacted;
    it is created before use so that it never exists briefly as world-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    existed = path.exists()
    conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
    if not existed:
        path.chmod(0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A transaction that takes the write lock up front.

    Use this for every check-then-write sequence. Without it, two callers read
    the same state, both decide they may proceed, and the second write wins
    without either of them noticing.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
