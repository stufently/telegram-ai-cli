"""Take one account row and hand back a connected, authorised client.

Everything that can go wrong with a session converges here, and three of those
paths are worth stating outright.

**The auth key is locked before anything touches it.** Two clients on one key
desynchronise the message sequence and Telegram may revoke the session. The lock
is keyed on the file's inode, so two rows pointing at the same ``.session``
through a link cannot both acquire it.

**A revoked session is terminal.** ``AuthKeyDuplicatedError`` and
``SessionRevokedError`` mean Telegram has thrown the key away. Reconnecting
cannot help, and a fleet that reconnects anyway becomes a retry storm; the
account is marked ``revoked``, and from then on it is not even dialled until a
human re-authorises it.

**One bad account never ends the run.** :func:`build_client` raises;
:func:`load_account` catches, records the verdict against that one label and
returns ``None``. Which of the two a caller wants depends on whether it is
serving one account or fifty.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import Settings
from ..errors import (
    AuthRequired,
    SessionLocked,
    SessionRevoked,
    TelegramAIError,
    TelegramError,
)
from ..secretbox import SecretBox
from .fs import harden_path
from .lock import SessionLock
from .models import AccountSource, AccountStatus
from .proxy import redact_proxy_url, redact_secrets
from .runtime import recoverable_errors, revoked_errors, telethon_options
from .sources import (
    USE_CURRENT_SESSION_WARNING,
    client_from_session_file,
    client_from_string,
    client_from_tdata,
)
from .spec import AccountSpec, LoadedClient, SessionFlag
from .store import AccountStore

if TYPE_CHECKING:  # pragma: no cover
    from opentele.tl import TelegramClient

log = logging.getLogger(__name__)

__all__ = [
    "USE_CURRENT_SESSION_WARNING",
    "AccountSpec",
    "LoadedClient",
    "SessionFlag",
    "build_client",
    "load_account",
    "recoverable_errors",
    "revoked_errors",
    "telethon_options",
]


async def build_client(
    spec: AccountSpec,
    *,
    sessions_dir: Path,
    settings: Settings | None = None,
    box: SecretBox | None = None,
    flag: SessionFlag = SessionFlag.CREATE_NEW,
    tdata_passcode: str | None = None,
    password: str | None = None,
    force_convert: bool = False,
    lock: bool = True,
    **telethon_kwargs: Any,
) -> LoadedClient:
    """Build, connect and verify the client for ``spec``.

    ``flag`` matters only the first time a ``tdata`` account is converted; the
    resulting ``.session`` is reused afterwards. Passing
    :attr:`SessionFlag.USE_CURRENT` logs the source Telegram Desktop out, so
    callers must surface :data:`USE_CURRENT_SESSION_WARNING` first.

    Raises :class:`~telegram_ai_cli.errors.SessionLocked` when the auth key is
    already in use, :class:`~telegram_ai_cli.errors.SessionRevoked` when
    Telegram has discarded it, and
    :class:`~telegram_ai_cli.errors.AuthRequired` when the session exists but no
    longer authorises.
    """
    paths = spec.paths(sessions_dir).prepare()
    proxy = spec.proxy()
    options = telethon_options(settings) | telethon_kwargs
    held = SessionLock(spec.lock_path(sessions_dir)).acquire() if lock else None
    client: TelegramClient | None = None
    try:
        if spec.source is AccountSource.TDATA:
            client = await client_from_tdata(
                spec,
                paths,
                proxy=proxy,
                flag=flag,
                passcode=tdata_passcode,
                password=password,
                force_convert=force_convert,
                box=box,
                telethon_kwargs=options,
            )
        elif spec.source is AccountSource.STRING_SESSION:
            client = client_from_string(spec, paths, proxy, box, options)
        else:
            client = client_from_session_file(spec, paths, proxy, box, options)

        await _connect(client, spec)
        await _require_authorized(client, spec)
    except BaseException:
        # opentele raises OpenTeleException, which derives from BaseException, so
        # a bare `except Exception` here would leak both the client and the lock.
        # The release sits in its own finally: _safe_disconnect swallows ordinary
        # errors but not a CancelledError raised *into* it, and a cleanup path
        # that skips the release strands the auth key for the life of the
        # process -- every later attempt then reports a holder that is gone.
        try:
            if client is not None:
                await _safe_disconnect(client, spec.label)
        finally:
            if held is not None:
                held.release()
        raise

    # Connecting creates the session file and its journal; both hold key material
    # and both were created under whatever umask happened to be in effect.
    if paths.session_file.exists():
        harden_path(paths.session_file)
    return LoadedClient(client=client, spec=spec, paths=paths, lock=held)


async def load_account(
    store: AccountStore,
    label: str,
    *,
    sessions_dir: Path,
    settings: Settings | None = None,
    box: SecretBox | None = None,
    flag: SessionFlag = SessionFlag.CREATE_NEW,
    tdata_passcode: str | None = None,
    password: str | None = None,
    mark_status: bool = True,
) -> LoadedClient | None:
    """Store-aware :func:`build_client`: records the verdict, raises nothing.

    Returns ``None`` for an account that failed, because losing one account out
    of a fleet must not end the run. A locked session deliberately leaves the
    status alone — it says nothing about the account, only about the moment.
    """
    record = store.get(label)
    if record is None:
        log.error("account %r is not registered", label)
        return None
    if record.status is AccountStatus.DISABLED:
        log.info("account %s is disabled; skipping", record.label)
        return None
    if record.status is AccountStatus.REVOKED:
        # The whole point of the status: no connection attempt at all.
        log.warning("account %s is revoked; re-authorise it before use", record.label)
        return None

    try:
        spec = AccountSpec.from_record(record, store)
    except TelegramAIError as exc:
        _fail(store, record.label, None, str(exc), mark_status)
        return None

    try:
        loaded = await build_client(
            spec,
            sessions_dir=sessions_dir,
            settings=settings,
            box=box,
            flag=flag,
            tdata_passcode=tdata_passcode,
            password=password,
        )
    except SessionLocked as exc:
        log.warning("account %s: %s", spec.label, exc.message)
        return None
    except SessionRevoked as exc:
        _revoke(store, spec, str(exc), mark_status)
        return None
    except TelegramAIError as exc:
        _fail(store, spec.label, spec, str(exc), mark_status)
        return None
    except recoverable_errors() as exc:  # unexpected, still only this account
        _fail(store, spec.label, spec, repr(exc), mark_status)
        log.exception("account %s failed to load", spec.label)
        return None

    try:
        me = await loaded.client.get_me()
    except revoked_errors() as exc:
        await loaded.close()
        _revoke(store, spec, repr(exc), mark_status)
        return None
    except recoverable_errors() as exc:
        await loaded.close()
        _fail(store, spec.label, spec, f"get_me failed: {exc!r}", mark_status)
        return None
    except BaseException:
        # Cancellation still has to give the auth key back.
        await loaded.close()
        raise

    if me is not None:
        loaded.user_id = int(me.id)
        store.set_user_id(spec.label, loaded.user_id)
    if mark_status:
        store.set_status(spec.label, AccountStatus.OK, None)
    log.info(
        "account %s connected (source=%s, user_id=%s, proxy=%s)",
        spec.label,
        spec.source,
        loaded.user_id,
        redact_proxy_url(spec.proxy_url),
    )
    return loaded


def _fail(
    store: AccountStore,
    label: str,
    spec: AccountSpec | None,
    message: str,
    mark_status: bool,
) -> None:
    """Record a per-account failure with the secrets stripped out.

    ``last_error`` is displayed verbatim wherever accounts are listed, and an
    exception repr easily carries a proxy password or a session string.
    """
    clean = redact_secrets(message, *(spec.secrets() if spec else ()))
    if mark_status:
        store.set_status(label, AccountStatus.AUTH_FAILED, clean)
    log.error("account %s failed: %s", label, clean)


def _revoke(store: AccountStore, spec: AccountSpec, message: str, mark_status: bool) -> None:
    clean = redact_secrets(message, *spec.secrets())
    if mark_status:
        store.set_status(spec.label, AccountStatus.REVOKED, clean)
    log.error("account %s was revoked by Telegram and will not be retried: %s", spec.label, clean)


def _revoked(spec: AccountSpec, exc: BaseException) -> SessionRevoked:
    return SessionRevoked(
        f"{spec.label}: Telegram discarded this session ({type(exc).__name__})",
        suggestion="Re-authorise the account; reconnecting cannot recover it.",
    )


def _unreachable(spec: AccountSpec, exc: OSError) -> TelegramError:
    return TelegramError(
        f"{spec.label}: cannot reach Telegram via {redact_proxy_url(spec.proxy_url)}: {exc}"
    )


async def _connect(client: TelegramClient, spec: AccountSpec) -> None:
    # UseCurrentSession hands back an unconnected client; CreateNewSession does not.
    if client.is_connected():
        return
    try:
        await client.connect()
    except revoked_errors() as exc:
        raise _revoked(spec, exc) from None
    except OSError as exc:
        raise _unreachable(spec, exc) from None


async def _require_authorized(client: TelegramClient, spec: AccountSpec) -> None:
    from telethon import errors

    try:
        authorized = await client.is_user_authorized()
    except revoked_errors() as exc:
        raise _revoked(spec, exc) from None
    except errors.UnauthorizedError as exc:
        raise AuthRequired(f"{spec.label}: session rejected by Telegram ({exc})") from None
    except OSError as exc:
        raise _unreachable(spec, exc) from None
    if not authorized:
        raise AuthRequired(
            f"{spec.label}: session exists but is not authorised (logged out or revoked)"
        )


async def _safe_disconnect(client: TelegramClient, label: str) -> None:
    try:
        result = client.disconnect()
        if asyncio.iscoroutine(result):
            await result
    except recoverable_errors() as exc:
        log.debug("disconnect of %s during cleanup failed: %s", label, exc)
