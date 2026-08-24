"""The ``accounts`` table, and the encryption boundary around it.

This is the only place that reads or writes account rows. Two things it owns:

**Secrets are encrypted going in and revealed only on demand.** ``api_hash`` and
``proxy_url`` pass through :class:`~telegram_ai_cli.secretbox.SecretBox` on
write. On read they stay as stored — a listing has no business decrypting a
proxy password — and :meth:`AccountStore.reveal` is the deliberate step a caller
takes when it genuinely needs the plaintext.

**Ciphertext is never handed on as if it were plaintext.** Without a key,
``reveal`` refuses. Passing ``enc:v1:...`` to Telethon as an ``api_hash`` would
fail at the far end of a connection attempt with an error that says nothing
about a missing key, and the operator would go looking at Telegram.

The schema itself lives in :mod:`telegram_ai_cli.db` so that one file describes
the whole database.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..db import immediate
from ..errors import AccountNotFound, InvalidInput, TelegramAIError
from ..secretbox import SecretBox, is_encrypted
from .models import AccountSource, AccountStatus
from .paths import sanitize_label


@dataclass(frozen=True, slots=True)
class AccountRecord:
    """One row, with secrets exactly as they are stored.

    ``api_hash`` and ``proxy_url`` may be ciphertext. That is intentional: a
    record can be listed, counted and rendered without a key anywhere near it.
    """

    label: str
    source: AccountSource
    session_path: str | None = None
    phone: str | None = None
    api_id: int | None = None
    api_hash: str | None = None
    proxy_url: str | None = None
    status: AccountStatus = AccountStatus.NEW
    user_id: int | None = None
    last_error: str | None = None
    created_at: float = 0.0

    def __repr__(self) -> str:
        # The generated repr would print api_hash into any traceback that
        # happens to carry a record.
        return (
            f"AccountRecord(label={self.label!r}, source={self.source}, "
            f"status={self.status}, user_id={self.user_id})"
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> AccountRecord:
        data = dict(row)
        raw_source = str(data.get("source") or "")
        try:
            source = AccountSource(raw_source)
        except ValueError:
            raise InvalidInput(
                f"account {data.get('label')!r} has unknown source {raw_source!r}"
            ) from None
        try:
            status = AccountStatus(str(data.get("status") or AccountStatus.NEW))
        except ValueError:
            # An unreadable status must not make the account unlistable; treat
            # it as "never connected" and let the next run overwrite it.
            status = AccountStatus.NEW
        return cls(
            label=str(data["label"]),
            source=source,
            session_path=data.get("session_path"),
            phone=data.get("phone"),
            api_id=int(data["api_id"]) if data.get("api_id") is not None else None,
            api_hash=data.get("api_hash"),
            proxy_url=data.get("proxy_url"),
            status=status,
            user_id=int(data["user_id"]) if data.get("user_id") is not None else None,
            last_error=data.get("last_error"),
            created_at=float(data.get("created_at") or 0.0),
        )


class AccountStore:
    """CRUD over ``accounts``. No filesystem, no network."""

    def __init__(self, conn: sqlite3.Connection, box: SecretBox | None = None) -> None:
        self._conn = conn
        self._box = box

    # -- secrets -----------------------------------------------------------

    @property
    def box(self) -> SecretBox | None:
        """The key holder, for code that must encrypt something outside a row.

        The device fingerprint is the case: it lives in a file next to the
        session, not in this table, and it carries an ``api_hash``.
        """
        return self._box

    def seal(self, value: str | None) -> str | None:
        """Encrypt a value for storage, when a key is configured."""
        if value is None or value == "":
            return None
        if self._box is None or is_encrypted(value):
            return value
        return self._box.encrypt(value)

    def reveal(self, value: str | None) -> str | None:
        """Decrypt a stored value, or refuse if the key is missing.

        Returning ciphertext here would push the failure to Telegram, where it
        surfaces as a rejected connection instead of as "the key is not set".
        """
        if value is None or value == "":
            return None
        if not is_encrypted(value):
            return value
        if self._box is None:
            raise TelegramAIError(
                "this account's secrets are encrypted but no key is configured",
                suggestion="Set TGAI_SECRET_KEY to the key these rows were written with.",
            )
        return self._box.decrypt(value)

    # -- reading -----------------------------------------------------------

    def get(self, label: str) -> AccountRecord | None:
        row = self._conn.execute(
            "SELECT * FROM accounts WHERE label = ?", (sanitize_label(label),)
        ).fetchone()
        return AccountRecord.from_row(row) if row else None

    def require(self, label: str) -> AccountRecord:
        record = self.get(label)
        if record is None:
            raise AccountNotFound(f"no account labelled {sanitize_label(label)!r}")
        return record

    def exists(self, label: str) -> bool:
        return self.get(label) is not None

    def list(self, *, only_enabled: bool = False) -> list[AccountRecord]:
        sql = "SELECT * FROM accounts"
        params: tuple[Any, ...] = ()
        if only_enabled:
            sql += " WHERE status != ?"
            params = (str(AccountStatus.DISABLED),)
        sql += " ORDER BY label"
        return [AccountRecord.from_row(row) for row in self._conn.execute(sql, params)]

    # -- writing -----------------------------------------------------------

    def upsert(
        self,
        label: str,
        source: AccountSource,
        *,
        session_path: str | None = None,
        phone: str | None = None,
        api_id: int | None = None,
        api_hash: str | None = None,
        proxy_url: str | None = None,
        replace: bool = False,
    ) -> AccountRecord:
        """Register or re-register an account.

        A re-import resets ``status`` to ``new``: the material on disk just
        changed, so any previous verdict about it describes a different session.
        """
        name = sanitize_label(label)
        with immediate(self._conn) as conn:
            existing = conn.execute(
                "SELECT label FROM accounts WHERE label = ?", (name,)
            ).fetchone()
            if existing and not replace:
                raise InvalidInput(
                    f"account {name!r} already exists",
                    suggestion="Pass replace=True (or --replace) to overwrite it.",
                )
            conn.execute(
                """
                INSERT INTO accounts
                    (label, source, session_path, phone, api_id, api_hash, proxy_url,
                     status, user_id, last_error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                ON CONFLICT(label) DO UPDATE SET
                    source = excluded.source,
                    session_path = excluded.session_path,
                    phone = excluded.phone,
                    api_id = excluded.api_id,
                    api_hash = excluded.api_hash,
                    proxy_url = excluded.proxy_url,
                    status = excluded.status,
                    user_id = NULL,
                    last_error = NULL
                """,
                (
                    name,
                    str(source),
                    session_path,
                    phone,
                    api_id,
                    self.seal(api_hash),
                    self.seal(proxy_url),
                    str(AccountStatus.NEW),
                    time.time(),
                ),
            )
        return self.require(name)

    def restore(self, record: AccountRecord) -> AccountRecord:
        """Put a previously read row back byte-for-byte.

        Login replacement is staged under the session lock.  If authorisation
        fails, the old registration must come back with its original encrypted
        values, status and creation time; feeding those values through
        :meth:`upsert` would both re-encrypt ciphertext and reset identity
        fields.  This deliberately accepts only an :class:`AccountRecord`
        returned by this store.
        """
        name = sanitize_label(record.label)
        with immediate(self._conn) as conn:
            conn.execute(
                """
                INSERT INTO accounts
                    (label, source, session_path, phone, api_id, api_hash, proxy_url,
                     status, user_id, last_error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(label) DO UPDATE SET
                    source = excluded.source,
                    session_path = excluded.session_path,
                    phone = excluded.phone,
                    api_id = excluded.api_id,
                    api_hash = excluded.api_hash,
                    proxy_url = excluded.proxy_url,
                    status = excluded.status,
                    user_id = excluded.user_id,
                    last_error = excluded.last_error,
                    created_at = excluded.created_at
                """,
                (
                    name,
                    str(record.source),
                    record.session_path,
                    record.phone,
                    record.api_id,
                    record.api_hash,
                    record.proxy_url,
                    str(record.status),
                    record.user_id,
                    record.last_error,
                    record.created_at,
                ),
            )
        return self.require(name)

    def set_status(self, label: str, status: AccountStatus, last_error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE accounts SET status = ?, last_error = ? WHERE label = ?",
            (str(status), last_error, sanitize_label(label)),
        )

    def set_user_id(self, label: str, user_id: int | None) -> None:
        self._conn.execute(
            "UPDATE accounts SET user_id = ? WHERE label = ?",
            (user_id, sanitize_label(label)),
        )

    def set_phone(self, label: str, phone: str | None) -> None:
        """Record the number the session actually belongs to, or clear it.

        Written after a login rather than before one, which is the only moment
        the number is a fact instead of an assumption: a QR code can be scanned
        by whichever account the person was signed in as, and a row whose phone
        and whose session name two different accounts is worse than a row with
        no phone at all.
        """
        self._conn.execute(
            "UPDATE accounts SET phone = ? WHERE label = ?",
            (phone, sanitize_label(label)),
        )

    def set_proxy(self, label: str, proxy_url: str | None) -> None:
        self._conn.execute(
            "UPDATE accounts SET proxy_url = ? WHERE label = ?",
            (self.seal(proxy_url), sanitize_label(label)),
        )

    def set_session_path(self, label: str, session_path: str) -> None:
        self._conn.execute(
            "UPDATE accounts SET session_path = ? WHERE label = ?",
            (session_path, sanitize_label(label)),
        )

    def delete(self, label: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM accounts WHERE label = ?", (sanitize_label(label),)
        )
        return cursor.rowcount > 0
