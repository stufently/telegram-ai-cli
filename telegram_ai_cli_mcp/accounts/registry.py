"""Account bookkeeping: the database row and the files, kept in step.

Everything an operator does to the fleet — importing a Telegram Desktop folder
or a whole backup tree, adopting a ``.session`` or a session string, listing,
enabling, disabling, removing — goes through here, so the row and the material
under ``sessions/`` can never drift apart.

Three invariants this module owns.

*The label is one safe path component*, because it becomes
``sessions/<label>.session``.

*Imported material is copied and hardened*, not referenced where the operator
left it. A row pointing into a Downloads folder is a session that disappears
when someone tidies up.

*Destructive changes hold the account's session lock.* Overwriting or deleting
files under a running client corrupts the session it is using, so an import over
a connected account is refused rather than raced.

Views returned from here carry no ``api_hash`` and no proxy password: they exist
to be printed.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self

from ..config import Settings
from ..errors import (
    AccountDisabled,
    InvalidInput,
    SessionLocked,
    SessionRevoked,
    TelegramAIError,
)
from ..secretbox import SecretBox
from .bulk import ImportResult, import_session_files, import_tdata_batch
from .discovery import discover_tdata_dirs, is_sqlite, label_for_tdata, looks_like_tdata
from .fs import (
    copy_private_file,
    ensure_private_dir,
    harden_path,
    require_no_symlink,
    write_private_text,
)
from .loader import build_client
from .lock import SessionLocks
from .models import AccountSource, AccountStatus
from .paths import SessionPaths, sanitize_label, session_lock_paths
from .proxy import redact_secrets, validate_proxy_url
from .spec import AccountSpec, LoadedClient, SessionFlag
from .store import AccountRecord, AccountStore
from .views import MIN_STRING_SESSION_LEN, AccountView

log = logging.getLogger(__name__)

__all__ = [
    "AccountRegistry",
    "AccountView",
    "ImportResult",
    "discover_tdata_dirs",
]


class AccountRegistry:
    """Create, inspect and retire accounts."""

    def __init__(
        self,
        store: AccountStore,
        sessions_dir: Path,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.sessions_dir = Path(sessions_dir)
        self.settings = settings

    @classmethod
    def from_connection(
        cls,
        conn: sqlite3.Connection,
        settings: Settings,
        box: SecretBox | None = None,
    ) -> Self:
        """Build a registry over an already-open state database.

        Deliberately not called ``open``: the operations layer probes a registry
        for an ``open(label)`` entry point, and a classmethod with a different
        signature answering that probe would fail somewhere far from here.
        """
        return cls(AccountStore(conn, box), settings.paths.sessions, settings)

    # -- reading -----------------------------------------------------------

    def paths_for(self, label: str) -> SessionPaths:
        return SessionPaths(self.sessions_dir, label)

    def list_accounts(self, *, only_enabled: bool = False) -> list[AccountView]:
        return [
            AccountView.from_record(record, self.store)
            for record in self.store.list(only_enabled=only_enabled)
        ]

    def get(self, label: str) -> AccountView | None:
        record = self.store.get(label)
        return AccountView.from_record(record, self.store) if record else None

    def require(self, label: str) -> AccountView:
        return AccountView.from_record(self.store.require(label), self.store)

    def exists(self, label: str) -> bool:
        return self.store.exists(label)

    # -- connecting --------------------------------------------------------

    async def load_account(
        self,
        label: str,
        *,
        flag: SessionFlag = SessionFlag.CREATE_NEW,
        tdata_passcode: str | None = None,
        password: str | None = None,
        mark_status: bool = True,
    ) -> LoadedClient:
        """Connect one account, or raise saying why it cannot be connected.

        This is the single-account entry point, and it raises: a command told to
        use *this* account must not quietly proceed without one. The fleet-wide
        counterpart is :func:`telegram_ai_cli_mcp.accounts.loader.load_account`,
        which records the same verdicts and returns ``None`` instead, because
        losing one account out of fifty must not end the run.

        The returned :class:`~telegram_ai_cli_mcp.accounts.spec.LoadedClient` is an
        async context manager holding the lock on the auth key. Close it, or the
        key stays claimed for the life of the process.
        """
        record = self.store.require(label)
        if record.status is AccountStatus.DISABLED:
            raise AccountDisabled(
                f"account {record.label!r} is disabled",
                suggestion=f"Run `tg-ai account enable {record.label}` first.",
            )
        if record.status is AccountStatus.REVOKED:
            raise SessionRevoked(
                f"account {record.label!r} was revoked by Telegram",
                suggestion=f"Re-authorise it with `tg-ai account login --label {record.label}`.",
            )

        spec = AccountSpec.from_record(record, self.store)
        try:
            loaded = await build_client(
                spec,
                sessions_dir=self.sessions_dir,
                settings=self.settings,
                box=self.store.box,
                flag=flag,
                tdata_passcode=tdata_passcode,
                password=password,
            )
        except SessionLocked:
            # Says nothing about the account, only about this moment: recording
            # it as a failure would leave a stale verdict behind.
            raise
        except SessionRevoked as exc:
            self._record_failure(spec, AccountStatus.REVOKED, str(exc), mark_status)
            raise
        except TelegramAIError as exc:
            self._record_failure(spec, AccountStatus.AUTH_FAILED, str(exc), mark_status)
            raise

        if mark_status:
            self.store.set_status(spec.label, AccountStatus.OK, None)
        return loaded

    def _record_failure(
        self, spec: AccountSpec, status: AccountStatus, message: str, mark_status: bool
    ) -> None:
        if not mark_status:
            return
        # last_error is displayed verbatim, and an exception string easily
        # carries a proxy password or an api_hash.
        self.store.set_status(spec.label, status, redact_secrets(message, *spec.secrets()))

    # -- importing ---------------------------------------------------------

    def register_phone_login(
        self,
        label: str,
        *,
        phone: str | None = None,
        api_id: int | None = None,
        api_hash: str | None = None,
        proxy_url: str | None = None,
        replace: bool = False,
    ) -> AccountView:
        """Register an account that a phone login will authorise later.

        Nothing is imported and nothing connects: the row points at the
        ``.session`` the login will create. It still runs under the account's
        session lock, like every other change that can replace a row — writing
        over one while a client is connected underneath it is how a session
        file ends up corrupted by its own reader.
        """
        name = self._claim_label(label, replace=replace)
        paths = self.paths_for(name).prepare()
        proxy_url = validate_proxy_url(proxy_url)

        with self._offline(name):
            self.store.upsert(
                name,
                AccountSource.SESSION_FILE,
                session_path=str(paths.session_file),
                phone=phone,
                api_id=api_id,
                api_hash=api_hash,
                proxy_url=proxy_url,
                # replace=replace, not True: _claim_label has already refused a
                # duplicate, and re-checking inside the transaction closes the gap
                # between that check and this write.
                replace=replace,
            )
        log.info("registered account %s for phone login", name)
        return self.require(name)

    def import_tdata(
        self,
        path: Path | str,
        *,
        label: str | None = None,
        proxy_url: str | None = None,
        api_id: int | None = None,
        api_hash: str | None = None,
        copy: bool = True,
        replace: bool = False,
    ) -> AccountView:
        """Register one Telegram Desktop folder.

        No conversion happens here. Converting costs a QR login and another
        device on the account, so it is deferred to the first connection.
        """
        src = require_no_symlink(Path(path).expanduser())
        if not src.is_dir():
            raise InvalidInput(f"{src} is not a directory")
        if not looks_like_tdata(src) and looks_like_tdata(src / "tdata"):
            src = src / "tdata"
        if not looks_like_tdata(src):
            raise InvalidInput(f"{src} does not look like a tdata folder (no key_data file inside)")

        name = self._claim_label(label or label_for_tdata(src), replace=replace)
        paths = self.paths_for(name).prepare()
        proxy_url = validate_proxy_url(proxy_url)

        with self._offline(name):
            target = src
            if copy:
                target = paths.tdata_dir
                ensure_private_dir(target.parent)
                if target.exists():
                    shutil.rmtree(target)
                # copytree, not move: the operator's backup must survive, and a
                # half-imported account is recoverable only if the source does.
                # symlinks=True copies links as links -- following them would
                # pull in, and later chmod, files from outside the backup.
                shutil.copytree(src, target, symlinks=True)
                harden_path(target)

            self.store.upsert(
                name,
                AccountSource.TDATA,
                session_path=str(target),
                api_id=api_id,
                api_hash=api_hash,
                proxy_url=proxy_url,
                # replace=replace, not True: _claim_label has already refused a
                # duplicate, and re-checking inside the transaction closes the gap
                # between that check and this write.
                replace=replace,
            )
        log.info("imported tdata account %s (copied=%s)", name, copy)
        return self.require(name)

    def import_tdata_batch(self, root: Path | str, **kwargs: Any) -> list[ImportResult]:
        """Import every tdata folder under ``root``; see :mod:`.bulk`."""
        return import_tdata_batch(self, root, **kwargs)

    def import_session_file(
        self,
        path: Path | str,
        *,
        label: str | None = None,
        api_id: int | None = None,
        api_hash: str | None = None,
        proxy_url: str | None = None,
        copy: bool = True,
        replace: bool = False,
    ) -> AccountView:
        """Adopt an existing Telethon ``.session``.

        ``api_id``/``api_hash`` should be the credentials that *created* the
        session. Presenting a different application to an existing auth key is
        one of the signals Telegram treats as a stolen session.
        """
        src = require_no_symlink(Path(path).expanduser())
        if not src.is_file():
            raise InvalidInput(f"{src} is not a file")
        if not is_sqlite(src):
            raise InvalidInput(f"{src} is not a Telethon .session (SQLite) file")

        name = self._claim_label(label or src.stem, replace=replace)
        paths = self.paths_for(name).prepare()
        proxy_url = validate_proxy_url(proxy_url)

        with self._offline(name):
            target = src
            if copy:
                target = paths.session_file
                if target.resolve() != src.resolve():
                    copy_private_file(src, target)
            harden_path(target)
            self.store.upsert(
                name,
                AccountSource.SESSION_FILE,
                session_path=str(target),
                api_id=api_id,
                api_hash=api_hash,
                proxy_url=proxy_url,
                # replace=replace, not True: _claim_label has already refused a
                # duplicate, and re-checking inside the transaction closes the gap
                # between that check and this write.
                replace=replace,
            )
        log.info("imported session file account %s", name)
        return self.require(name)

    def import_session_files(
        self, paths: Iterable[Path | str], **kwargs: Any
    ) -> list[ImportResult]:
        """Adopt several ``.session`` files; see :mod:`.bulk`."""
        return import_session_files(self, paths, **kwargs)

    def import_string_session(
        self,
        session_string: str,
        *,
        label: str,
        api_id: int | None = None,
        api_hash: str | None = None,
        proxy_url: str | None = None,
        replace: bool = False,
    ) -> AccountView:
        """Store a Telethon session string in a ``0600`` file.

        The string *is* the auth key, so it goes to a file rather than into the
        database: a file is easy to keep out of backups and log dumps, a TEXT
        column in a shared SQLite is not.
        """
        raw = (session_string or "").strip()
        if len(raw) < MIN_STRING_SESSION_LEN or any(ch.isspace() for ch in raw):
            # The value itself must not reach the message.
            raise InvalidInput(
                f"session string for {label!r} is malformed (length {len(raw)}, expected a "
                f"single token of {MIN_STRING_SESSION_LEN}+ characters)"
            )

        name = self._claim_label(label, replace=replace)
        paths = self.paths_for(name).prepare()
        proxy_url = validate_proxy_url(proxy_url)

        with self._offline(name):
            write_private_text(paths.string_file, raw)
            self.store.upsert(
                name,
                AccountSource.STRING_SESSION,
                session_path=str(paths.string_file),
                api_id=api_id,
                api_hash=api_hash,
                proxy_url=proxy_url,
                # replace=replace, not True: _claim_label has already refused a
                # duplicate, and re-checking inside the transaction closes the gap
                # between that check and this write.
                replace=replace,
            )
        log.info("imported string-session account %s", name)
        return self.require(name)

    # -- lifecycle ---------------------------------------------------------

    def enable(self, label: str) -> AccountView:
        """Re-arm an account. Status goes back to ``new`` so the loader retries it."""
        record = self.store.require(label)
        self.store.set_status(record.label, AccountStatus.NEW, None)
        return self.require(record.label)

    def disable(self, label: str) -> AccountView:
        record = self.store.require(label)
        self.store.set_status(record.label, AccountStatus.DISABLED, None)
        return self.require(record.label)

    def set_proxy(self, label: str, proxy_url: str | None) -> AccountView:
        """Attach a proxy, validated before it can break the next start-up.

        An empty value clears it. That is a deliberate downgrade — the account
        then egresses from the host address — so it is logged rather than silent.
        """
        record = self.store.require(label)
        validated = validate_proxy_url(proxy_url)
        if validated is None:
            log.warning(
                "account %s will connect without a proxy; a fleet sharing one "
                "address is how every account in it gets banned together",
                record.label,
            )
        self.store.set_proxy(record.label, validated)
        return self.require(record.label)

    def remove(self, label: str, *, delete_files: bool = True) -> bool:
        """Delete the row and, by default, everything the account owns on disk.

        Only paths inside ``sessions_dir`` are touched. With ``copy=False`` the
        row may still point at the operator's original tdata folder, and
        deleting that would destroy the only copy of an account.
        """
        record = self.store.get(label)
        if record is None:
            return False
        with self._offline(record.label, record=record):
            self.store.delete(record.label)
            if delete_files:
                self._purge_files(record)
        log.info("removed account %s (files=%s)", record.label, delete_files)
        return True

    # -- internals ---------------------------------------------------------

    @contextmanager
    def _offline(
        self, label: str, record: AccountRecord | None = None
    ) -> Iterator[SessionLocks | None]:
        """Hold the account's session lock across a destructive change.

        Overwriting or deleting files under a running client corrupts the
        session it is using, so this takes the same lock the loader takes and
        refuses rather than races.
        """
        record = record if record is not None else self.store.get(label)
        if record is None:
            # Nothing registered yet: no client can hold this key, and the lock
            # path is not knowable from a label alone for every source.
            yield None
            return
        paths = session_lock_paths(str(record.source), record.session_path, self.paths_for(label))
        try:
            lock = SessionLocks(paths).acquire()
        except SessionLocked as exc:
            raise SessionLocked(
                f"account {label!r} is currently connected; stop it first",
                details=exc.details,
            ) from None
        try:
            yield lock
        finally:
            lock.release()

    def _purge_files(self, record: AccountRecord) -> None:
        candidates = list(self.paths_for(record.label).owned_paths())
        if record.session_path:
            candidates.append(Path(record.session_path))
        for path in candidates:
            if not self._is_managed(path) or not path.exists():
                continue
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as exc:
                # A leftover file is a mess, not a breach: the row is already
                # gone and the material is still 0600. Say so and continue.
                log.warning("cannot remove %s: %s", path, exc)

    def _is_managed(self, path: Path) -> bool:
        """True only for paths we created under ``sessions_dir``."""
        try:
            return Path(path).resolve().is_relative_to(self.sessions_dir.resolve())
        except OSError:
            return False

    def _claim_label(self, label: str, *, replace: bool) -> str:
        name = sanitize_label(label)
        if not replace and self.store.exists(name):
            raise InvalidInput(
                f"account {name!r} already exists",
                suggestion="Pass --replace to overwrite it.",
            )
        return name
