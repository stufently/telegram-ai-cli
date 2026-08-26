"""The local archive: a copy of named chats, on this machine, in SQLite.

Every live read is an RPC. That costs a round trip and an account's flood
budget, it cannot be given a regular expression — Telegram's search matches
text, not patterns — and it cannot answer a question about two accounts at once.
So a chat may be *copied* here on request and queried offline afterwards.

Three decisions are worth stating before the code, because each of them is the
kind of thing that looks like an implementation detail until it is wrong.

**Nothing fills itself.** There is no daemon, no background sweep and no
"archive everything". A chat lands here because somebody named it. An archive
that filled itself would turn a tool with an allowlist into a bulk collector of
private correspondence, and the size of what it held would be a function of
uptime rather than of anything a person decided.

**It is not encrypted, deliberately.** The same directory holds the Telethon
``.session`` files, and a session file *is* the account: whoever can read it can
read every message in Telegram, live, with no archive involved. Encrypting the
archive next to it would not raise the bar an attacker has to clear; it would
only make offline search and regular expressions impossible, which is the entire
reason the archive exists. What does the work instead is the same control as for
the session files: the file is created ``0600`` in a ``0700`` directory, and it
is in ``.gitignore``. Deletion is a first-class operation
(:meth:`ArchiveStore.forget`) rather than an instruction to run ``rm``.

**Recognisable secrets are masked on the way in.** :mod:`telegram_ai_cli_mcp.redact`
normally runs at the edge of a *result*, which is enough for a live read because
nothing is kept. An archive keeps it. A card number or a login code stored raw
would sit unencrypted on disk for as long as the archive is kept — strictly
worse than the live path — so the mask is applied before the row is written and
the raw value never lands. The cost is that a regular expression cannot match
what was masked, and that is the documented trade (`docs/operations.md`).

**Policy is not stored, it is re-asked.** The rows here carry a chat's id, kind
and username so that the read policy can be evaluated against the *current*
configuration every time the archive is read. A chat archived while it was
permitted and closed the next day must stop answering, and it cannot do that if
the decision were baked in at write time.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import InsecurePermissions
from .redact import redact
from .safety import PeerKind, PeerRef

#: One statement per concern, and no migration framework: the archive is a
#: cache of something Telegram still holds, so a schema that ever needs to
#: change can be rebuilt by re-syncing rather than migrated in place.
ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS archived_chats (
    account          TEXT NOT NULL,
    chat_id          INTEGER NOT NULL,
    kind             TEXT NOT NULL,
    username         TEXT,
    title            TEXT,
    -- The watermarks. `newest_message_id` is what a re-sync bounds its request
    -- below with, so already-stored messages are never fetched twice;
    -- `oldest_message_id` is where backfilling continues from.
    oldest_message_id INTEGER,
    newest_message_id INTEGER,
    -- An interrupted run for *new* messages. When the budget ran out before the
    -- walk down from the newest message reached `newest_message_id`, there is a
    -- hole: `pending_from_id` is where to carry on from and `pending_top_id` is
    -- the id that becomes the watermark once the hole closes. Without these the
    -- next run would restart at the top, re-fetch the same page forever and
    -- never join the two ends (raised by review, 2026-08-23).
    pending_from_id  INTEGER,
    pending_top_id   INTEGER,
    -- Whether backfilling reached the beginning of the history. A partial
    -- archive that claimed to be whole would answer "nobody said that" about a
    -- chat it has only the recent end of.
    complete         INTEGER NOT NULL DEFAULT 0,
    first_synced_at  REAL NOT NULL,
    last_synced_at   REAL NOT NULL,
    PRIMARY KEY (account, chat_id)
);

CREATE TABLE IF NOT EXISTS archived_messages (
    account         TEXT NOT NULL,
    chat_id         INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,
    date            REAL,
    sender_id       INTEGER,
    sender          TEXT,
    sender_username TEXT,
    outgoing        INTEGER NOT NULL DEFAULT 0,
    text            TEXT,
    text_truncated  INTEGER NOT NULL DEFAULT 0,
    reply_to_msg_id INTEGER,
    topic_id        INTEGER,
    -- Metadata only. Archiving never downloads a byte: that is `media fetch`,
    -- a different capability with a quota of its own.
    media_type      TEXT,
    PRIMARY KEY (account, chat_id, message_id),
    FOREIGN KEY (account, chat_id) REFERENCES archived_chats(account, chat_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS archived_messages_date_idx
    ON archived_messages(account, chat_id, date);
CREATE INDEX IF NOT EXISTS archived_messages_sender_idx
    ON archived_messages(account, sender_id);
"""


def archive_path(settings: Settings) -> Path:
    return settings.paths.archive


def _narrow(path: Path, mode: int, *, what: str) -> None:
    """Take a path down to ``mode``, or refuse to go on.

    A failed ``chmod`` is fatal rather than warned about. A warning would leave
    other people's messages readable and the run looking successful, which is
    exactly the outcome the check exists to prevent.
    """
    if path.stat().st_mode & ~mode & 0o777:
        try:
            path.chmod(mode)
        except OSError as exc:
            raise InsecurePermissions(
                f"cannot narrow {what} {path} to {mode:#o}: {exc}",
                suggestion="Point paths.archive at a file this user owns.",
            ) from None


def _prepare(path: Path) -> None:
    """Make the archive file exist, private, and be a file.

    Checked on **every** open, not only on creation. An archive left ``0644`` by
    an earlier version, a restore from a backup, or a careless ``chmod -R`` all
    produce a readable copy of somebody's private messages that a
    create-time-only check would never notice — which is precisely the bug this
    replaced (raised by review, 2026-08-23).

    ``O_CREAT|O_EXCL|O_NOFOLLOW`` at ``0600`` rather than letting sqlite3 create
    the file: the mode is then set at creation, so there is no window in which
    the database exists and is world-readable, and a symlink planted at that
    name is an error rather than a write to wherever it points. SQLite gives the
    ``-wal`` and ``-shm`` sidecars the mode of the main file, so narrowing it
    first is what keeps those private too.
    """
    root = path.parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # ``mode`` above applies only when mkdir actually creates the directory.
    _narrow(root, 0o700, what="the state directory")

    if path.is_symlink():
        raise InsecurePermissions(
            f"{path} is a symlink; the archive must be a regular file",
            suggestion="Remove it, or point paths.archive somewhere this user owns.",
        )
    if path.exists():
        if not path.is_file():
            raise InsecurePermissions(f"{path} exists and is not a regular file")
        _narrow(path, 0o600, what="the archive")
        return
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600))
    except FileExistsError:
        # Another process got there first; judge what is now on disk.
        _narrow(path, 0o600, what="the archive")


def connect_archive(path: Path) -> sqlite3.Connection:
    """Open the archive, private before anything is written into it.

    A *separate* database from :func:`telegram_ai_cli_mcp.db.connect` on purpose:
    erasing every archived message must not take the account registry, the
    pending plans and the rate-limit history with it.
    """
    _prepare(path)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(ARCHIVE_SCHEMA)
    _add_missing_columns(conn)
    return conn


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Bring a file written by an older build up to the current columns.

    Two idempotent statements, and deliberately **not** a migration framework:
    the archive is a cache of something Telegram still holds, so a schema that
    ever changes incompatibly is answered by deleting the file and re-syncing.
    What this covers is the narrow case of a column added to a table that
    ``CREATE TABLE IF NOT EXISTS`` will not touch.
    """
    for column in ("pending_from_id INTEGER", "pending_top_id INTEGER"):
        with suppress(sqlite3.OperationalError):  # already there
            conn.execute(f"ALTER TABLE archived_chats ADD COLUMN {column}")  # noqa: S608


def iso_of(epoch: float | None) -> str | None:
    """A stored timestamp as UTC ISO-8601, matching what a live read prints."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def epoch_of(value: datetime | None) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        # A naive datetime is ambiguous, and guessing the local zone would make
        # two machines disagree about when the same message was sent.
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


@dataclass(frozen=True, slots=True)
class ArchivedChat:
    """One archived chat, and everything needed to re-judge it.

    ``peer`` is rebuilt from the stored identity rather than remembered as a
    verdict: the policy must be evaluated against today's configuration, not
    against the one that was in force when the sync ran.
    """

    account: str
    chat_id: int
    kind: str
    username: str | None
    title: str | None
    oldest_message_id: int | None
    newest_message_id: int | None
    complete: bool
    first_synced_at: float
    last_synced_at: float
    pending_from_id: int | None = None
    pending_top_id: int | None = None
    messages: int = 0

    @property
    def contiguous(self) -> bool:
        """Whether the stored range has no hole in it.

        A chat with an interrupted run for new messages *does* hold today's
        messages and is missing some in the middle, which is a different thing
        from an unfinished backfill and has to be reported separately.
        """
        return self.pending_from_id is None

    @property
    def whole(self) -> bool:
        """Contiguous *and* back to the first message ever sent."""
        return self.complete and self.contiguous

    @property
    def peer(self) -> PeerRef | None:
        """The kernel's view of this chat, or ``None`` if it cannot be judged.

        Two deliberate narrowings, both raised by review (2026-08-23), both in
        the fail-closed direction:

        **A ``kind`` this build does not recognise yields ``None``.** Every row
        this project writes carries a valid one, so an unparseable value means
        the file was written by something else — and mapping it to ``UNKNOWN``
        would make it *not private*, which is how a foreign database gets a
        private conversation read under the group rule.

        **The stored ``username`` is left out of the judgement.** Handles are
        reassignable and this one is a copy taken at sync time, so matching an
        allowlist entry against it could admit a chat because somebody *used to*
        hold the name in the entry. Policy on an archived chat is decided by its
        numeric id alone; the username is still reported, as a label.
        """
        try:
            kind = PeerKind(self.kind)
        except ValueError:
            return None
        return PeerRef(peer_id=self.chat_id, kind=kind, username=None, title=self.title)

    def to_row(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "kind": self.kind,
            "username": self.username,
            "title": self.title,
            "messages": self.messages,
            "oldest_message_id": self.oldest_message_id,
            "newest_message_id": self.newest_message_id,
            "complete": self.whole,
            "reaches_first_message": self.complete,
            "contiguous": self.contiguous,
            "first_synced_at": iso_of(self.first_synced_at),
            "last_synced_at": iso_of(self.last_synced_at),
        }


def _chat_from(row: sqlite3.Row, *, messages: int = 0) -> ArchivedChat:
    return ArchivedChat(
        account=row["account"],
        chat_id=row["chat_id"],
        kind=row["kind"],
        username=row["username"],
        title=row["title"],
        oldest_message_id=row["oldest_message_id"],
        newest_message_id=row["newest_message_id"],
        complete=bool(row["complete"]),
        first_synced_at=row["first_synced_at"],
        last_synced_at=row["last_synced_at"],
        pending_from_id=row["pending_from_id"],
        pending_top_id=row["pending_top_id"],
        messages=messages,
    )


@dataclass(slots=True)
class StoredMessage:
    """One row on its way to disk. Assembled by the operation, masked here."""

    message_id: int
    date: float | None = None
    sender_id: int | None = None
    sender: str | None = None
    sender_username: str | None = None
    outgoing: bool = False
    text: str | None = None
    text_truncated: bool = False
    reply_to_msg_id: int | None = None
    topic_id: int | None = None
    media_type: str | None = None


class ArchiveStore:
    """Everything that touches the archive database, in one place."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- writing -----------------------------------------------------------

    def register_chat(self, account: str, peer: PeerRef) -> ArchivedChat | None:
        """Create the chat row if it is new, refresh its labels if it is not.

        Returns what was on disk *before* this call, which is what the caller
        needs to know where to resume from — ``None`` means nothing was.
        """
        existing = self.chat(account, peer.peer_id)
        now = time.time()
        title = redact(peer.title) if peer.title else None
        if existing is None:
            self._conn.execute(
                "INSERT INTO archived_chats (account, chat_id, kind, username, title, "
                "first_synced_at, last_synced_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (account, peer.peer_id, str(peer.kind), peer.username, title, now, now),
            )
            return None
        self._conn.execute(
            "UPDATE archived_chats SET kind = ?, username = ?, title = ? "
            "WHERE account = ? AND chat_id = ?",
            (str(peer.kind), peer.username, title, account, peer.peer_id),
        )
        return existing

    def store_messages(self, account: str, chat_id: int, messages: list[StoredMessage]) -> int:
        """Write a page, masking every human-authored string on the way in.

        ``INSERT OR REPLACE`` rather than ``INSERT``: a message edited between
        two syncs should end up with its current text, and a re-run that
        overlaps a page boundary must not fail on a primary key.
        """
        if not messages:
            return 0
        self._conn.executemany(
            "INSERT OR REPLACE INTO archived_messages ("
            "account, chat_id, message_id, date, sender_id, sender, sender_username, "
            "outgoing, text, text_truncated, reply_to_msg_id, topic_id, media_type"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    account,
                    chat_id,
                    item.message_id,
                    item.date,
                    item.sender_id,
                    redact(item.sender) if item.sender else None,
                    item.sender_username,
                    int(item.outgoing),
                    redact(item.text) if item.text else None,
                    int(item.text_truncated),
                    item.reply_to_msg_id,
                    item.topic_id,
                    item.media_type,
                )
                for item in messages
            ],
        )
        return len(messages)

    def set_watermarks(
        self,
        account: str,
        chat_id: int,
        *,
        oldest: int | None,
        newest: int | None,
        complete: bool,
        pending_from: int | None = None,
        pending_top: int | None = None,
    ) -> None:
        """Record where this chat's archive now stops, in both directions.

        ``pending_from``/``pending_top`` are written unconditionally, including
        as ``NULL``: clearing them is how a closed hole stops being a hole, and
        an update that only ever set them would leave a chat permanently marked
        as interrupted.
        """
        self._conn.execute(
            "UPDATE archived_chats SET oldest_message_id = ?, newest_message_id = ?, "
            "complete = ?, pending_from_id = ?, pending_top_id = ?, last_synced_at = ? "
            "WHERE account = ? AND chat_id = ?",
            (
                oldest,
                newest,
                int(complete),
                pending_from,
                pending_top,
                time.time(),
                account,
                chat_id,
            ),
        )

    def forget(self, account: str, chat_id: int) -> tuple[bool, int]:
        """Erase one chat. Returns ``(was_archived, messages_removed)``.

        Idempotent by design: a cleanup that fails the second time it runs is a
        cleanup nobody automates.
        """
        removed = self._conn.execute(
            "DELETE FROM archived_messages WHERE account = ? AND chat_id = ?",
            (account, chat_id),
        ).rowcount
        existed = self._conn.execute(
            "DELETE FROM archived_chats WHERE account = ? AND chat_id = ?",
            (account, chat_id),
        ).rowcount
        return bool(existed), max(removed, 0)

    # -- reading -----------------------------------------------------------

    def chat(self, account: str, chat_id: int) -> ArchivedChat | None:
        row = self._conn.execute(
            "SELECT * FROM archived_chats WHERE account = ? AND chat_id = ?",
            (account, chat_id),
        ).fetchone()
        if row is None:
            return None
        return _chat_from(row, messages=self.count(account, chat_id))

    def chats(self, account: str) -> list[ArchivedChat]:
        rows = self._conn.execute(
            "SELECT c.*, ("
            "  SELECT COUNT(*) FROM archived_messages m"
            "   WHERE m.account = c.account AND m.chat_id = c.chat_id"
            ") AS messages FROM archived_chats c WHERE c.account = ? "
            "ORDER BY c.last_synced_at DESC",
            (account,),
        ).fetchall()
        return [_chat_from(row, messages=row["messages"]) for row in rows]

    def count(self, account: str, chat_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM archived_messages WHERE account = ? AND chat_id = ?",
            (account, chat_id),
        ).fetchone()
        return int(row["n"]) if row else 0

    def candidates(
        self,
        account: str,
        *,
        chat_ids: list[int],
        sender_id: int | None = None,
        since: float | None = None,
        until: float | None = None,
        scan: int,
    ) -> Iterator[sqlite3.Row]:
        """Rows the structured filters admit, newest first, bounded by ``scan``.

        Text matching is *not* done here. A regular expression cannot be pushed
        into SQLite, and having two matching paths — one in SQL for substrings,
        one in Python for patterns — is how a filter comes to mean two slightly
        different things depending on a flag. So SQL narrows on the parts it can
        index (chat, sender, date) and one Python predicate decides the rest.

        ``scan`` is a ceiling on how many rows that predicate is run over. Rows
        are **streamed** off the cursor rather than materialised: fifty thousand
        message bodies is hundreds of megabytes, and a search that stops at the
        first fifty matches should not have paid for all of them first.
        """
        if not chat_ids:
            return
        clauses = ["account = ?", f"chat_id IN ({','.join('?' for _ in chat_ids)})"]
        args: list[Any] = [account, *chat_ids]
        if sender_id is not None:
            clauses.append("sender_id = ?")
            args.append(sender_id)
        if since is not None:
            clauses.append("date >= ?")
            args.append(since)
        if until is not None:
            clauses.append("date <= ?")
            args.append(until)
        args.append(scan)
        # Every fragment joined here is a literal written above, and the only
        # variable part is *how many* `?` placeholders the `IN` list needs —
        # which sqlite3 cannot express as one bound parameter. No caller value
        # reaches the string; all of them travel in `args`.
        where = " AND ".join(clauses)
        order = "ORDER BY date DESC, message_id DESC LIMIT ?"
        sql = f"SELECT * FROM archived_messages WHERE {where} {order}"  # noqa: S608
        cursor = self._conn.execute(sql, args)
        try:
            yield from cursor
        finally:
            cursor.close()


@contextmanager
def open_archive(settings: Settings) -> Iterator[ArchiveStore]:
    """The archive for this configuration, closed again afterwards.

    Opened per operation rather than held on the context: the archive is used by
    four operations out of thirty, and creating the file (and its WAL sidecars)
    on every run of an unrelated command would put an empty database of other
    people's messages on the disk of somebody who never asked for one.
    """
    with closing(connect_archive(archive_path(settings))) as conn:
        yield ArchiveStore(conn)
