"""What the loader works with: one account's decrypted details, and the result.

:class:`AccountSpec` is the row after decryption — the only object in this
package that holds an ``api_hash`` and a proxy password in the clear. Its
``__repr__`` is hand-written for exactly that reason: the generated one would
print both into any traceback that happened to carry a spec, and tracebacks end
up in logs and in ``last_error``.

:class:`LoadedClient` pairs the connected client with the lock on its auth key,
so releasing the key is impossible to forget — closing the client is what
releases it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

from .lock import SessionLocks
from .models import AccountSource
from .paths import SessionPaths, session_lock_paths
from .proxy import (
    NO_PROXY_WARNING,
    mask_secret,
    parse_proxy_url,
    proxy_password,
    redact_proxy_url,
)
from .runtime import recoverable_errors
from .store import AccountRecord, AccountStore

if TYPE_CHECKING:  # pragma: no cover
    from opentele.tl import TelegramClient

log = logging.getLogger(__name__)


class SessionFlag(StrEnum):
    """Which opentele login flag to use when converting ``tdata``."""

    CREATE_NEW = "create_new_session"
    """QR-login a new session. Safe: the desktop client stays signed in."""

    USE_CURRENT = "use_current_session"
    """Take over the existing auth key. Logs the original desktop client out."""


@dataclass(frozen=True, slots=True)
class AccountSpec:
    """What the loader needs from an account row, with secrets in the clear.

    ``session_path`` means something different per source: the tdata folder, the
    ``.session`` file, or the file holding the session string.
    """

    label: str
    source: AccountSource
    session_path: str | None = None
    phone: str | None = None
    api_id: int | None = None
    api_hash: str | None = None
    proxy_url: str | None = None

    def __repr__(self) -> str:
        return (
            f"AccountSpec(label={self.label!r}, source={self.source}, "
            f"api_id={self.api_id}, api_hash={mask_secret(self.api_hash)}, "
            f"proxy={redact_proxy_url(self.proxy_url)})"
        )

    @classmethod
    def from_record(cls, record: AccountRecord, store: AccountStore) -> AccountSpec:
        """Decrypt the stored secrets, refusing ciphertext without a key."""
        return cls(
            label=record.label,
            source=record.source,
            session_path=record.session_path,
            phone=record.phone,
            api_id=record.api_id,
            api_hash=store.reveal(record.api_hash),
            proxy_url=store.reveal(record.proxy_url),
        )

    def paths(self, sessions_dir: Path) -> SessionPaths:
        return SessionPaths(Path(sessions_dir), self.label)

    def lock_paths(self, sessions_dir: Path) -> tuple[Path, ...]:
        return session_lock_paths(str(self.source), self.session_path, self.paths(sessions_dir))

    def secrets(self) -> tuple[str | None, ...]:
        """Literal values that must never reach a log line or ``last_error``."""
        return (self.api_hash, proxy_password(self.proxy_url))

    def proxy(self) -> dict[str, Any] | None:
        proxy = parse_proxy_url(self.proxy_url)
        if proxy is None:
            log.warning(NO_PROXY_WARNING, self.label)
        return proxy


@dataclass(slots=True)
class LoadedClient:
    """A connected client plus the lock keeping its auth key exclusive."""

    client: TelegramClient
    spec: AccountSpec
    paths: SessionPaths
    lock: SessionLocks | None = None
    user_id: int | None = None

    async def close(self) -> None:
        """Disconnect, then release the auth key for the next process."""
        try:
            result = self.client.disconnect()
            if asyncio.iscoroutine(result):
                await result
        except recoverable_errors() as exc:  # must not strand the lock
            log.warning("disconnect of %s failed: %s", self.spec.label, exc)
        finally:
            if self.lock is not None:
                self.lock.release()
                self.lock = None

    async def __aenter__(self) -> LoadedClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
