"""The local archive: filling it on request, searching it offline, erasing it.

Every live read costs a Telegram round trip, and a round trip cannot be given a
regular expression. So a chat can be copied to disk *on request* and queried
there instead. Four properties are what make that safe rather than merely
convenient, and each of them is a way the feature goes wrong quietly:

**The allowlist is re-applied on the way out.** An archive is a snapshot of a
decision that was true when it was taken. A chat archived while it was permitted
and closed the next day would keep answering questions forever if policy were
only consulted at write time — the copy on disk would outlive the permission
that produced it. So every read re-checks, per chat, against the *current*
configuration.

**Erasing is an operation, not a suggestion.** The archive is personal data on
somebody's disk. If the only way to remove it were `rm`, the tool would have
shipped a data-retention problem. And it must keep working for a chat the policy
now closes: "you may no longer read it, and you may no longer delete it" is the
worst of both answers.

**Archived text is still a stranger's text.** It crossed the trust boundary once
on the way in; it has to cross it again on the way out, because what a model
reads is the tool result, not the disk.

**The answer says where it came from and how old it is.** An archive result that
looked like a live one is an agent reporting last week's state as today's.
"""

from __future__ import annotations

import os
import secrets
import signal
import sqlite3
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from telegram_ai_cli import db
from telegram_ai_cli.audit import AuditLog
from telegram_ai_cli.config import Settings
from telegram_ai_cli.context import OperationContext
from telegram_ai_cli.errors import (
    Denylisted,
    InsecurePermissions,
    InvalidInput,
    NotAllowlisted,
)
from telegram_ai_cli.limits import LimitStore
from telegram_ai_cli.ops import archive as archive_ops
from telegram_ai_cli.ops.archive import (
    ARCHIVE_PAGE,
    ArchiveForgetInput,
    ArchiveSearchInput,
    ArchiveStatusInput,
    ArchiveSyncInput,
    handle_archive_forget,
    handle_archive_search,
    handle_archive_status,
    handle_archive_sync,
)
from telegram_ai_cli.opspec import REGISTRY, Effect
from telegram_ai_cli.plans import PlanStore
from telegram_ai_cli.safety import SafetyKernel
from telegram_ai_cli.secretbox import SecretBox
from telegram_ai_cli.untrusted import CLOSE_MARKER, OPEN_MARKER

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

# --- a Telegram that is entirely local --------------------------------------


class FakeTotalList(list):
    """Telethon hands back a list that also knows the total. So does this."""

    def __init__(self, items: list[Any], total: int) -> None:
        super().__init__(items)
        self.total = total


@dataclass(slots=True)
class FakeMessage:
    """Only the attributes the serializer actually reads."""

    id: int
    message: str
    date: datetime = BASE
    out: bool = False
    sender_id: int = 4242
    media: Any = None
    file: Any = None


class FakeClient:
    """History in a list, with Telethon's `max_id`/`min_id` paging reproduced.

    Both bounds are exclusive, which is exactly what makes them a cursor: the
    archive walks down from the newest message with `max_id` and stops at the
    watermark with `min_id`. A fake that treated either as inclusive would
    certify an importer that loses or repeats a message at every page boundary.
    """

    def __init__(self, entity: Any, messages: list[FakeMessage]) -> None:
        self.entity = entity
        self.messages = sorted(messages, key=lambda m: m.id)
        self.calls: list[dict[str, Any]] = []
        self.downloads = 0

    def is_connected(self) -> bool:
        return True

    async def is_user_authorized(self) -> bool:
        return True

    async def get_entity(self, reference: Any) -> Any:
        return self.entity

    async def download_media(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        self.downloads += 1
        raise AssertionError("archiving must never fetch an attachment")

    async def get_messages(
        self,
        entity: Any,
        *,
        limit: int | None = None,
        search: str | None = None,
        max_id: int = 0,
        min_id: int = 0,
    ) -> FakeTotalList:
        self.calls.append({"limit": limit, "search": search, "max_id": max_id, "min_id": min_id})
        pool = list(self.messages)
        if max_id:
            pool = [m for m in pool if m.id < max_id]
        if min_id:
            pool = [m for m in pool if m.id > min_id]
        pool.reverse()
        return FakeTotalList(pool[: limit or len(pool)], total=len(pool))


@dataclass(slots=True)
class FakeRegistry:
    client: FakeClient
    labels: list[str] = field(default_factory=lambda: ["main"])

    def list_accounts(self) -> list[str]:
        return list(self.labels)

    def get(self, label: str) -> None:
        return None

    async def load_account(self, label: str) -> Any:
        return self


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("TGAI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "state.sqlite3")
    yield connection
    connection.close()


def group_entity(peer_id: int = 555, title: str = "Release room") -> Any:
    """A real Telethon type: ``marked_id`` calls ``utils.get_peer_id``."""
    from telethon.tl.types import Chat

    return Chat(
        id=peer_id,
        title=title,
        photo=None,
        participants_count=3,
        date=BASE,
        version=1,
    )


def user_entity(peer_id: int = 909) -> Any:
    from telethon.tl.types import User

    return User(id=peer_id, first_name="Dana", username="dana")


def service_entity() -> Any:
    from telethon.tl.types import User

    return User(id=777000, first_name="Telegram")


def build_context(
    conn: sqlite3.Connection,
    tmp_path: Path,
    client: FakeClient,
    *,
    settings: Settings | None = None,
) -> OperationContext:
    settings = settings or Settings()
    return OperationContext(
        settings=settings,
        safety=SafetyKernel(settings),
        plans=PlanStore(conn, settings.plans, SecretBox(secrets.token_bytes(32))),
        limits=LimitStore(conn, settings.limits),
        audit=AuditLog(tmp_path / "audit.jsonl", settings.audit),
        actor="cli",
        accounts=FakeRegistry(client),
    )


def history(count: int = 10, *, word: str = "deploy") -> list[FakeMessage]:
    """Ids 1..count. The word appears in 4 and 6; 6 is from another sender."""
    bodies = {4: f"the {word} went out at noon", 6: f"{word} again after the fix"}
    return [
        FakeMessage(
            id=i,
            message=bodies.get(i, f"message {i}"),
            date=BASE + timedelta(hours=i),
            sender_id=7 if i == 6 else 4242,
        )
        for i in range(1, count + 1)
    ]


async def sync(conn: sqlite3.Connection, tmp_path: Path, client: FakeClient, **kwargs: Any) -> Any:
    ctx = build_context(conn, tmp_path, client)
    return await handle_archive_sync(ctx, ArchiveSyncInput(**kwargs))


async def search(
    conn: sqlite3.Connection,
    tmp_path: Path,
    client: FakeClient,
    *,
    settings: Settings | None = None,
    **kwargs: Any,
) -> Any:
    ctx = build_context(conn, tmp_path, client, settings=settings)
    return await handle_archive_search(ctx, ArchiveSearchInput(**kwargs))


async def status(
    conn: sqlite3.Connection,
    tmp_path: Path,
    client: FakeClient,
    *,
    settings: Settings | None = None,
    **kwargs: Any,
) -> Any:
    ctx = build_context(conn, tmp_path, client, settings=settings)
    return await handle_archive_status(ctx, ArchiveStatusInput(**kwargs))


async def forget(
    conn: sqlite3.Connection,
    tmp_path: Path,
    client: FakeClient,
    *,
    settings: Settings | None = None,
    **kwargs: Any,
) -> Any:
    ctx = build_context(conn, tmp_path, client, settings=settings)
    return await handle_archive_forget(ctx, ArchiveForgetInput(**kwargs))


def ids_of(envelope: Any) -> list[int]:
    return [row["id"] for row in envelope.data["messages"]]


def closed_group(peer_id: int = -555) -> Settings:
    """The same configuration, with one group taken away."""
    return Settings(safety={"read": {"chats": {"deny": [peer_id]}}})


# --- filling it -------------------------------------------------------------


async def test_sync_copies_a_chat_and_reports_its_watermarks(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    client = FakeClient(group_entity(), history())

    envelope = await sync(conn, tmp_path, client, chat="-555")

    assert envelope.data["stored"] == 10
    assert envelope.data["oldest_message_id"] == 1
    assert envelope.data["newest_message_id"] == 10
    assert envelope.data["complete"] is True
    assert envelope.data["chat"]["id"] == -555


async def test_a_second_sync_adds_only_what_is_new(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The watermark is the point: re-archiving must not re-download."""
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    client.messages.extend(
        FakeMessage(id=i, message=f"message {i}", date=BASE + timedelta(hours=i)) for i in (11, 12)
    )
    client.calls.clear()

    envelope = await sync(conn, tmp_path, client, chat="-555")

    assert envelope.data["stored"] == 2
    assert envelope.data["newest_message_id"] == 12
    # Every request for new messages is bounded below by the watermark, so the
    # ten already on disk are never asked for again.
    assert all(call["min_id"] == 10 for call in client.calls)


async def test_a_sync_with_nothing_new_stores_nothing(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    envelope = await sync(conn, tmp_path, client, chat="-555")

    assert envelope.data["stored"] == 0
    assert envelope.data["newest_message_id"] == 10


async def test_a_long_history_is_archived_across_calls(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A budget bounds one call; repeating it continues where it stopped."""
    total = ARCHIVE_PAGE * 3
    client = FakeClient(group_entity(), history(total))

    first = await sync(conn, tmp_path, client, chat="-555", limit=ARCHIVE_PAGE)
    assert first.data["stored"] == ARCHIVE_PAGE
    assert first.data["complete"] is False
    assert first.meta.truncated is True

    second = await sync(conn, tmp_path, client, chat="-555", limit=ARCHIVE_PAGE * 2)
    assert second.data["stored"] == ARCHIVE_PAGE * 2
    assert second.data["complete"] is True
    assert second.data["oldest_message_id"] == 1


async def test_more_new_messages_than_one_budget_still_converge(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The bug this test exists for: a hole that never closes.

    An interrupted run for *new* messages used to restart at the newest message
    every time, so a chat that gained more messages than one budget could fetch
    re-downloaded the same page forever and never joined the two ends. The
    resume cursor is what fixes it, and the watermark must not move until the
    hole is actually closed.
    """
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    client.messages.extend(
        FakeMessage(id=i, message=f"message {i}", date=BASE + timedelta(hours=i))
        for i in range(11, 16)
    )

    first = await sync(conn, tmp_path, client, chat="-555", limit=2)
    assert first.data["contiguous"] is False
    assert first.data["newest_message_id"] == 10  # not moved across the hole
    assert first.meta.truncated is True

    second = await sync(conn, tmp_path, client, chat="-555", limit=2)
    assert second.data["stored"] == 2
    assert second.data["contiguous"] is False

    third = await sync(conn, tmp_path, client, chat="-555", limit=2)
    assert third.data["contiguous"] is True
    assert third.data["newest_message_id"] == 15
    assert third.data["messages"] == 15

    found = await search(conn, tmp_path, client, query="message 1", chat="-555")
    assert set(ids_of(found)) >= {11, 12, 13, 14, 15}


async def test_a_gap_is_reported_by_search_not_hidden(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A hole answers "nobody said that" as confidently as a whole archive."""
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")
    client.messages.extend(
        FakeMessage(id=i, message=f"message {i}", date=BASE + timedelta(hours=i))
        for i in range(11, 16)
    )
    await sync(conn, tmp_path, client, chat="-555", limit=2)

    envelope = await search(conn, tmp_path, client, query="message", chat="-555")

    assert any("gap" in warning for warning in envelope.warnings)


async def test_sync_refuses_a_chat_closed_in_code(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """The hard floor applies to archiving exactly as it does to reading."""
    client = FakeClient(service_entity(), history())

    with pytest.raises(Denylisted):
        await sync(conn, tmp_path, client, chat="777000")


async def test_sync_refuses_a_private_chat_that_is_not_allowlisted(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    client = FakeClient(user_entity(), history())

    with pytest.raises(NotAllowlisted):
        await sync(conn, tmp_path, client, chat="909")


async def test_sync_checks_policy_before_it_fetches(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A refused chat costs no Telegram call at all."""
    client = FakeClient(user_entity(), history())

    with pytest.raises(NotAllowlisted):
        await sync(conn, tmp_path, client, chat="909")

    assert client.calls == []


async def test_sync_never_fetches_an_attachment(conn: sqlite3.Connection, tmp_path: Path) -> None:
    client = FakeClient(group_entity(), history())

    await sync(conn, tmp_path, client, chat="-555")

    assert client.downloads == 0


async def test_the_archive_file_is_private(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Unencrypted on disk by decision, so the permission bits are the control."""
    client = FakeClient(group_entity(), history())

    await sync(conn, tmp_path, client, chat="-555")

    path = Settings().paths.archive
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_an_archive_left_readable_is_narrowed_on_every_open(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Not only at creation.

    A file left `0644` by an earlier version, a restore from a backup or a
    careless `chmod -R` all produce a world-readable copy of somebody's private
    messages that a create-time-only check would never notice.
    """
    path = Settings().paths.archive
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o644)
    path.chmod(0o644)

    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_the_archive_refuses_to_be_a_symlink(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Otherwise the archive writes wherever somebody pointed the name."""
    path = Settings().paths.archive
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(tmp_path / "elsewhere.sqlite3")

    client = FakeClient(group_entity(), history())
    with pytest.raises(InsecurePermissions):
        await sync(conn, tmp_path, client, chat="-555")


async def test_recognisable_secrets_never_reach_the_disk(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Redaction is applied on the way in, not only on the way out.

    A live read never persists anything, so masking at the edge is enough there.
    An archive is a copy that outlives the request, and storing the raw value
    would leave the one thing redaction exists to contain sitting unencrypted in
    a file for as long as the archive is kept.
    """
    secretive = [FakeMessage(id=1, message="card 4111 1111 1111 1111 please", date=BASE)]
    client = FakeClient(group_entity(), secretive)

    await sync(conn, tmp_path, client, chat="-555")

    raw = Settings().paths.archive.read_bytes()
    assert b"4111 1111 1111 1111" not in raw
    assert b"[redacted:card]" in raw


# --- searching it -----------------------------------------------------------


async def test_search_finds_a_substring_offline(conn: sqlite3.Connection, tmp_path: Path) -> None:
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")
    client.calls.clear()

    envelope = await search(conn, tmp_path, client, query="deploy", chat="-555")

    assert ids_of(envelope) == [6, 4]
    # The whole point: not one Telegram request was made.
    assert client.calls == []


async def test_search_supports_a_regular_expression(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The capability a live search cannot have: Telegram matches text, not patterns."""
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    envelope = await search(
        conn, tmp_path, client, query=r"deploy (again|went)", chat="-555", regex=True
    )

    assert ids_of(envelope) == [6, 4]


async def test_a_broken_regular_expression_is_reported_as_input(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    with pytest.raises(InvalidInput):
        await search(conn, tmp_path, client, query="deploy(", chat="-555", regex=True)


async def test_search_filters_by_sender_and_by_date(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    by_sender = await search(conn, tmp_path, client, query="deploy", chat="-555", sender=7)
    assert ids_of(by_sender) == [6]

    cutoff = (BASE + timedelta(hours=5)).isoformat()
    since = await search(conn, tmp_path, client, query="deploy", chat="-555", since=cutoff)
    assert ids_of(since) == [6]

    until = await search(conn, tmp_path, client, query="deploy", chat="-555", until=cutoff)
    assert ids_of(until) == [4]


async def test_search_says_the_answer_came_from_the_archive_and_when(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Otherwise an agent reports a stale answer as the current state."""
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    envelope = await search(conn, tmp_path, client, query="deploy", chat="-555")

    assert envelope.meta.extra["source"] == "archive"
    assert envelope.meta.extra["synced_at"]
    assert any("archive" in warning for warning in envelope.warnings)


async def test_archived_text_crosses_the_trust_boundary_on_the_way_out(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """It is a stranger's sentence whether it came from the wire or from disk."""
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    envelope = await search(conn, tmp_path, client, query="deploy", chat="-555")

    for row in envelope.data["messages"]:
        assert row["text"].startswith(OPEN_MARKER)
        assert row["text"].endswith(CLOSE_MARKER)


async def test_a_chat_closed_after_it_was_archived_is_not_returned(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The policy is re-applied on the read, not remembered from the write.

    This is the property that makes an archive safe to keep at all: the copy on
    disk must not outlive the permission that produced it.
    """
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    with pytest.raises((NotAllowlisted, Denylisted)):
        await search(conn, tmp_path, client, query="deploy", chat="-555", settings=closed_group())


async def test_an_unscoped_search_withholds_chats_the_policy_now_closes(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """And says how many, rather than returning a short list with no reason."""
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    envelope = await search(conn, tmp_path, client, query="deploy", settings=closed_group())

    assert envelope.data["messages"] == []
    assert envelope.meta.extra["withheld_chats"] == 1
    assert any("withheld" in warning for warning in envelope.warnings)


def _corrupt_kind(kind: str = "supergroup") -> None:
    """Rewrite a stored chat kind to something this build does not know.

    Only a database written by something other than this project can look like
    this, which is exactly the case the archive doc promises to survive.
    """
    from telegram_ai_cli.archive import connect_archive

    conn = connect_archive(Settings().paths.archive)
    conn.execute("UPDATE archived_chats SET kind = ?", (kind,))
    conn.close()


async def test_a_chat_kind_this_build_cannot_parse_is_withheld(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Fail-closed, because the alternative admits a private chat.

    An unrecognised kind mapped to `unknown` is *not private*, so a private
    conversation in a foreign database would be judged under the group rule —
    which permits by default.
    """
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")
    _corrupt_kind()

    envelope = await search(conn, tmp_path, client, query="deploy")

    assert envelope.data["messages"] == []
    assert envelope.meta.extra["withheld_chats"] == 1


async def test_a_chat_kind_this_build_cannot_parse_is_refused_when_named(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")
    _corrupt_kind()

    with pytest.raises(Denylisted):
        await search(conn, tmp_path, client, query="deploy", chat="-555")


async def test_policy_on_an_archived_chat_is_decided_by_id_not_a_stored_handle(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A username is a copy taken at sync time, and handles are reassignable.

    Allowlisting `@release` must not admit an archived chat merely because that
    chat *used to* answer to the name. Fail-closed: the id is what counts.
    """
    from telethon.tl.types import Channel

    entity = Channel(
        id=777,
        title="Release room",
        photo=None,
        date=BASE,
        megagroup=True,
        username="release",
    )
    client = FakeClient(entity, history())
    await sync(conn, tmp_path, client, chat="-100777")

    by_name = Settings(safety={"read": {"chats": {"allow": ["@release"]}}})
    envelope = await search(conn, tmp_path, client, query="deploy", settings=by_name)

    assert envelope.data["messages"] == []
    assert envelope.meta.extra["withheld_chats"] == 1


async def test_a_runaway_pattern_is_stopped_and_blamed_on_the_pattern(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row and length ceilings bound how much is matched, not how long.

    `re` has no timeout, so an unattended server would hold the call open for as
    long as a catastrophically backtracking pattern cared to run.
    """
    if not hasattr(signal, "SIGALRM"):  # pragma: no cover - POSIX only
        pytest.skip("no SIGALRM on this platform")

    evil = [FakeMessage(id=1, message="a" * 60 + "!", date=BASE)]
    client = FakeClient(group_entity(), evil)
    await sync(conn, tmp_path, client, chat="-555")

    monkeypatch.setattr(archive_ops, "SEARCH_TIME_BUDGET_SEC", 1.0)

    with pytest.raises(InvalidInput, match="took longer"):
        await search(conn, tmp_path, client, query=r"(a+)+$", chat="-555", regex=True)


async def test_the_timer_is_disarmed_afterwards(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """An SIGALRM left armed would fire into unrelated code later."""
    if not hasattr(signal, "SIGALRM"):  # pragma: no cover - POSIX only
        pytest.skip("no SIGALRM on this platform")

    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    await search(conn, tmp_path, client, query="deploy", chat="-555", regex=True)

    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


async def test_an_unscoped_search_needs_enumeration(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Sweeping every archived chat reveals which ones exist, like a dialog list."""
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    settings = Settings(safety={"read": {"allow_dialog_enumeration": False}})
    with pytest.raises(NotAllowlisted):
        await search(conn, tmp_path, client, query="deploy", settings=settings)


async def test_searching_an_unarchived_chat_says_so(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """ "Nothing matched" and "nothing was ever archived" are different answers."""
    client = FakeClient(group_entity(), history())

    envelope = await search(conn, tmp_path, client, query="deploy", chat="-555")

    assert envelope.data["messages"] == []
    assert any("not archived" in warning for warning in envelope.warnings)


# --- knowing what is in it --------------------------------------------------


async def test_status_reports_each_chat_with_counts_and_a_sync_time(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    envelope = await status(conn, tmp_path, client)

    row = envelope.data["chats"][0]
    assert row["chat_id"] == -555
    assert row["messages"] == 10
    assert row["oldest_message_id"] == 1
    assert row["newest_message_id"] == 10
    assert row["last_synced_at"]
    assert row["complete"] is True


async def test_status_hides_a_chat_the_policy_now_closes(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    envelope = await status(conn, tmp_path, client, settings=closed_group())

    assert envelope.data["chats"] == []
    assert envelope.meta.extra["withheld_chats"] == 1


# --- erasing it -------------------------------------------------------------


async def test_forget_removes_the_chat_and_every_message_in_it(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    envelope = await forget(conn, tmp_path, client, chat_id=-555)

    assert envelope.data["forgotten"] is True
    assert envelope.data["messages"] == 10
    assert (await status(conn, tmp_path, client)).data["chats"] == []


async def test_forget_still_works_for_a_chat_the_policy_now_closes(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Erasability is not gated on readability.

    A chat removed from the allowlist is exactly the one whose copy on disk
    ought to go. Refusing to delete it because it may no longer be read would
    strand personal data with no way to remove it through the tool.
    """
    client = FakeClient(group_entity(), history())
    await sync(conn, tmp_path, client, chat="-555")

    envelope = await forget(conn, tmp_path, client, chat_id=-555, settings=closed_group())

    assert envelope.data["forgotten"] is True


async def test_forgetting_a_chat_that_was_never_archived_is_not_an_error(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Deletion is idempotent; a second call must not fail a cleanup script."""
    client = FakeClient(group_entity(), history())

    envelope = await forget(conn, tmp_path, client, chat_id=-555)

    assert envelope.data["forgotten"] is False
    assert envelope.data["messages"] == 0


# --- how the operations are classified --------------------------------------


def test_writing_to_the_archive_is_declared_a_local_write() -> None:
    """It writes this machine's disk. Calling it a read would hide that."""
    assert REGISTRY.by_name("archive.sync").effect is Effect.LOCAL_WRITE
    assert REGISTRY.by_name("archive.forget").effect is Effect.LOCAL_WRITE


def test_reading_the_archive_is_declared_a_read() -> None:
    assert REGISTRY.by_name("archive.search").effect is Effect.READ
    assert REGISTRY.by_name("archive.status").effect is Effect.READ


def test_erasing_is_published_as_destructive_and_idempotent() -> None:
    """The hints are advisory, which is why a wrong one is worse than none.

    A client that auto-approves what it was told is harmless would act on the
    lie; and a delete that claimed not to be idempotent invites a caller to
    avoid a retry that is perfectly safe.
    """
    from telegram_ai_cli.mcp_server import _tool_for

    forget = _tool_for(REGISTRY.by_name("archive.forget"), plan=False)
    assert forget.annotations.destructive_hint is True
    assert forget.annotations.idempotent_hint is True

    sync_tool = _tool_for(REGISTRY.by_name("archive.sync"), plan=False)
    assert sync_tool.annotations.destructive_hint is False


def test_every_archive_operation_is_reachable_as_a_tool() -> None:
    """None of them is a remote write, so none of them may be plan-only."""
    for name in ("archive.sync", "archive.search", "archive.status", "archive.forget"):
        op = REGISTRY.by_name(name)
        assert op.mcp_tool is not None
        assert op.plan_tool is None
        assert op.is_remote_write is False
