"""Turning each kind of stored material into a Telethon client.

Three sources, three shapes of risk.

``tdata`` is converted exactly once and the resulting ``.session`` is reused
forever. That is not an optimisation: every conversion with ``CreateNewSession``
performs a QR login, which registers another device on the account and is rate
limited.

``session_file`` and ``string_session`` are already auth keys. Nothing here
creates them — they are opened, and the only decisions left are whether the file
is really ours (not a symlink someone swapped in) and which fingerprint to
present.

Telethon and opentele are imported inside the functions. The package has to be
importable, and the CLI has to print help, on a machine with neither installed.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from ..errors import AccountNotFound, InvalidInput, TelegramAIError
from ..secretbox import SecretBox
from .api_profile import resolve_api
from .fs import harden_path, read_private_text, require_no_symlink
from .paths import SessionPaths
from .proxy import mask_secret
from .spec import SessionFlag

if TYPE_CHECKING:  # pragma: no cover
    from opentele.api import APIData
    from opentele.tl import TelegramClient

    from .spec import AccountSpec

log = logging.getLogger(__name__)

#: Shown before anything switches to ``UseCurrentSession``.
USE_CURRENT_SESSION_WARNING: Final = (
    "UseCurrentSession reuses the SAME 256-byte auth key that lives in tdata: the "
    "original Telegram Desktop installation gets logged out and that folder must "
    "never be opened again afterwards. CreateNewSession (the default) authorises a "
    "separate session by QR login and leaves the desktop client signed in -- it only "
    "adds one device to the account's session list."
)

_MIN_INLINE_STRING_LEN: Final = 40


async def client_from_tdata(
    spec: AccountSpec,
    paths: SessionPaths,
    *,
    proxy: dict[str, Any] | None,
    flag: SessionFlag,
    passcode: str | None,
    password: str | None,
    force_convert: bool,
    box: SecretBox | None,
    telethon_kwargs: dict[str, Any],
) -> TelegramClient:
    """Convert a Telegram Desktop folder once, then reuse the session."""
    api = _api_for(spec, paths, box)
    if paths.session_file.exists() and not force_convert:
        require_no_symlink(paths.session_file)
        return new_client(str(paths.session_file), api, proxy, telethon_kwargs)

    tdata_dir = Path(spec.session_path) if spec.session_path else paths.tdata_dir
    require_no_symlink(tdata_dir)
    if not tdata_dir.is_dir():
        raise AccountNotFound(f"tdata folder for {spec.label} not found at {tdata_dir}")

    if flag is SessionFlag.USE_CURRENT:
        log.warning("account %s: %s", spec.label, USE_CURRENT_SESSION_WARNING)

    from opentele.api import CreateNewSession, UseCurrentSession
    from opentele.td import TDesktop

    def _load() -> TDesktop:
        desk = TDesktop(api=api, passcode=passcode)
        desk.LoadTData(str(tdata_dir))
        return desk

    try:
        # LoadTData is blocking file I/O plus AES over the whole folder; on the
        # event loop it would stall every other account for its duration.
        desktop = await asyncio.to_thread(_load)
    except BaseException as exc:  # OpenTeleException does not derive from Exception
        raise InvalidInput(
            f"cannot read tdata for {spec.label} from {tdata_dir}: {exc!r}"
        ) from None

    if not desktop.isLoaded() or desktop.accountsCount == 0:
        raise InvalidInput(f"tdata for {spec.label} holds no account")

    opentele_flag = UseCurrentSession if flag is SessionFlag.USE_CURRENT else CreateNewSession
    try:
        client: TelegramClient = await desktop.ToTelethon(
            session=str(paths.session_file),
            flag=opentele_flag,
            api=api,
            password=password,
            proxy=proxy,
            **telethon_kwargs,
        )
    except BaseException as exc:
        raise TelegramAIError(f"tdata conversion failed for {spec.label}: {exc!r}") from None
    harden_path(paths.session_file)
    return client


def client_from_session_file(
    spec: AccountSpec,
    paths: SessionPaths,
    proxy: dict[str, Any] | None,
    box: SecretBox | None,
    telethon_kwargs: dict[str, Any],
) -> TelegramClient:
    session = Path(spec.session_path) if spec.session_path else paths.session_file
    if not session.exists():
        raise AccountNotFound(f"session file for {spec.label} not found at {session}")
    # Telethon opens this path itself, so O_NOFOLLOW is not available at the
    # call that matters. Refusing a link here is the only chance to notice.
    require_no_symlink(session)
    harden_path(session)
    api = _api_for(spec, paths, box)
    # Full path on purpose: Telethon's SQLiteSession appends ".session" only when
    # it is missing, so handing it the stem would break a file named otherwise.
    return new_client(str(session), api, proxy, telethon_kwargs)


def client_from_string(
    spec: AccountSpec,
    paths: SessionPaths,
    proxy: dict[str, Any] | None,
    box: SecretBox | None,
    telethon_kwargs: dict[str, Any],
) -> TelegramClient:
    from telethon.sessions import StringSession

    raw = read_session_string(spec, paths)
    api = _api_for(spec, paths, box)
    try:
        session = StringSession(raw)
    except Exception as exc:  # noqa: BLE001 - any parse failure means the same
        raise InvalidInput(
            f"session string for {spec.label} is not a valid StringSession "
            f"({type(exc).__name__}); value={mask_secret(raw)}"
        ) from None
    return new_client(session, api, proxy, telethon_kwargs)


def read_session_string(spec: AccountSpec, paths: SessionPaths) -> str:
    """Read the session string from its ``0600`` file, never following a link.

    A raw string stored directly in ``session_path`` is tolerated for
    hand-edited databases — it is still the auth key, so it is never logged.
    """
    candidate = spec.session_path or str(paths.string_file)
    path = Path(candidate)
    for target in (path, paths.string_file):
        if target.exists():
            require_no_symlink(target)
            harden_path(target)
            return read_private_text(target).strip()
    if candidate and "/" not in candidate and len(candidate) > _MIN_INLINE_STRING_LEN:
        return candidate.strip()
    raise AccountNotFound(f"no session string on disk for {spec.label}")


def new_client(
    session: Any,
    api: APIData,
    proxy: dict[str, Any] | None,
    telethon_kwargs: dict[str, Any],
) -> TelegramClient:
    from opentele.tl import TelegramClient

    return TelegramClient(session, api=api, proxy=proxy, **telethon_kwargs)


def _api_for(spec: AccountSpec, paths: SessionPaths, box: SecretBox | None) -> APIData:
    return resolve_api(
        label=paths.label,
        api_file=paths.api_file,
        api_id=spec.api_id,
        api_hash=spec.api_hash,
        box=box,
    )
