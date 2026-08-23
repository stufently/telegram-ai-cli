"""Interactive phone login, for the CLI.

Produces exactly what the loader expects from a ``session_file`` account: a
``0600`` ``.session`` in the sessions directory plus a frozen device fingerprint
beside it.

The two-step verification password is read with :func:`getpass.getpass` and is
never accepted from ``argv`` or the environment. A command line is visible in
``ps`` output to every user on the host and lands in shell history; an
environment variable is readable from ``/proc`` for the life of the process.
Neither the password nor the login code is logged, at any level.

The login runs through the same proxy the account will use afterwards. Signing
in from the host address and then connecting through a proxy is precisely the
location jump that gets a fresh session killed.
"""

from __future__ import annotations

import asyncio
import getpass
import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from ..config import Settings
from ..errors import AuthRequired, FloodWait, InvalidInput, TelegramAIError
from ..secretbox import SecretBox
from .api_profile import resolve_api
from .fs import harden_path
from .lock import SessionLock
from .models import AccountSource, AccountStatus
from .paths import SessionPaths, session_lock_path
from .proxy import (
    mask_secret,
    parse_proxy_url,
    proxy_password,
    redact_proxy_url,
    redact_secrets,
    validate_proxy_url,
)
from .sources import new_client
from .store import AccountStore

if TYPE_CHECKING:  # pragma: no cover
    from opentele.tl import TelegramClient

log = logging.getLogger(__name__)

#: Sync or async, both are accepted, so a GUI or a bot can supply the code.
CodeCallback = Callable[[str], str | Awaitable[str]]
PasswordCallback = Callable[[str], str | Awaitable[str]]

_PHONE_RE = re.compile(r"^\+?\d{6,20}$")

DEFAULT_CODE_ATTEMPTS: Final = 3
DEFAULT_PASSWORD_ATTEMPTS: Final = 3


@dataclass(frozen=True, slots=True)
class LoginResult:
    label: str
    phone: str
    session_path: Path
    user_id: int
    username: str | None = None


async def prompt_code(prompt: str) -> str:
    """Read the login code from the terminal without blocking the event loop."""
    return await asyncio.to_thread(_blocking_input, prompt)


async def prompt_password(prompt: str) -> str:
    """Read the 2FA password with echo off, off the event loop. Never from argv."""
    return await asyncio.to_thread(getpass.getpass, prompt)


def _blocking_input(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


async def _ask(callback: Callable[[str], Any], prompt: str) -> str:
    result = callback(prompt)
    if inspect.isawaitable(result):
        result = await result
    text = str(result or "").strip()
    if not text:
        raise AuthRequired("empty input; login aborted")
    return text


async def interactive_login(
    *,
    label: str,
    phone: str,
    sessions_dir: Path,
    api_id: int | None = None,
    api_hash: str | None = None,
    proxy_url: str | None = None,
    settings: Settings | None = None,
    box: SecretBox | None = None,
    code_cb: CodeCallback | None = None,
    password_cb: PasswordCallback | None = None,
    code_attempts: int = DEFAULT_CODE_ATTEMPTS,
    password_attempts: int = DEFAULT_PASSWORD_ATTEMPTS,
) -> LoginResult:
    """Sign one phone number in and leave an authorised session on disk.

    Idempotent: an already authorised ``<label>.session`` is reported as-is
    rather than triggering another code request, which Telegram rate limits
    aggressively.
    """
    from .loader import telethon_options

    phone = normalize_phone(phone)
    paths = SessionPaths(Path(sessions_dir), label).prepare()
    proxy_url = validate_proxy_url(proxy_url)
    proxy = parse_proxy_url(proxy_url)
    if proxy is None:
        log.warning(
            "logging %s in without a proxy: the account is bound to this host's "
            "egress address, shared with every other account here",
            paths.label,
        )

    api = resolve_api(
        label=paths.label, api_file=paths.api_file, api_id=api_id, api_hash=api_hash, box=box
    )
    lock_path = session_lock_path(str(AccountSource.SESSION_FILE), str(paths.session_file), paths)
    client: TelegramClient | None = None
    lock = SessionLock(lock_path).acquire()
    try:
        # Inside the try: a failure while constructing the client must still hand
        # the auth key back, or the next attempt reports a holder that is gone.
        client = new_client(str(paths.session_file), api, proxy, telethon_options(settings))
        await client.connect()
        if not await client.is_user_authorized():
            await _sign_in(
                client,
                phone=phone,
                label=paths.label,
                code_cb=code_cb or prompt_code,
                password_cb=password_cb,
                code_attempts=code_attempts,
                password_attempts=password_attempts,
            )
        me = await client.get_me()
        if me is None:
            raise AuthRequired(f"{paths.label}: signed in but Telegram returned no user")
        user_id = int(me.id)
        username = getattr(me, "username", None)
    finally:
        try:
            result = client.disconnect() if client is not None else None
            if inspect.isawaitable(result):
                await result
        finally:
            # In the finally, not after the block: connect() has already written
            # a DC auth key into the .session, so a login abandoned at the code
            # prompt still leaves key material on disk at whatever the umask
            # allowed. Hardening only the happy path leaves exactly the files
            # nobody comes back to clean up.
            harden_path(paths.session_file)
            lock.release()

    log.info(
        "account %s logged in: user_id=%s phone=%s proxy=%s",
        paths.label,
        user_id,
        mask_secret(phone, keep=3),
        redact_proxy_url(proxy_url),
    )
    return LoginResult(
        label=paths.label,
        phone=phone,
        session_path=paths.session_file,
        user_id=user_id,
        username=username,
    )


async def login_and_register(
    store: AccountStore,
    *,
    label: str,
    phone: str,
    sessions_dir: Path,
    api_id: int | None = None,
    api_hash: str | None = None,
    proxy_url: str | None = None,
    settings: Settings | None = None,
    box: SecretBox | None = None,
    code_cb: CodeCallback | None = None,
    password_cb: PasswordCallback | None = None,
    replace: bool = False,
) -> LoginResult:
    """:func:`interactive_login` plus the ``accounts`` row.

    The row is written before the network call, so a login interrupted halfway
    leaves a visible account with a recorded error rather than an orphan session
    file nobody knows about.
    """
    paths = SessionPaths(Path(sessions_dir), label)
    record = store.upsert(
        paths.label,
        AccountSource.SESSION_FILE,
        session_path=str(paths.session_file),
        phone=normalize_phone(phone),
        api_id=api_id,
        api_hash=api_hash,
        proxy_url=validate_proxy_url(proxy_url),
        replace=replace,
    )
    try:
        result = await interactive_login(
            label=record.label,
            phone=phone,
            sessions_dir=sessions_dir,
            api_id=api_id,
            api_hash=api_hash,
            proxy_url=proxy_url,
            settings=settings,
            box=box,
            code_cb=code_cb,
            password_cb=password_cb,
        )
    except TelegramAIError as exc:
        # last_error is displayed verbatim, so it gets the same redaction the
        # loader applies. The messages raised here are careful today; the
        # invariant must not depend on that staying true for every future
        # Telethon error string.
        clean = redact_secrets(str(exc), api_hash, proxy_password(proxy_url))
        store.set_status(record.label, AccountStatus.AUTH_FAILED, clean)
        raise

    store.set_user_id(record.label, result.user_id)
    store.set_status(record.label, AccountStatus.OK, None)
    return result


async def _sign_in(
    client: TelegramClient,
    *,
    phone: str,
    label: str,
    code_cb: CodeCallback,
    password_cb: PasswordCallback | None,
    code_attempts: int,
    password_attempts: int,
) -> None:
    from telethon import errors

    try:
        sent = await client.send_code_request(phone)
    except errors.FloodWaitError as exc:
        raise FloodWait(exc.seconds) from None
    except errors.PhoneNumberBannedError:
        raise AuthRequired(f"{label}: this phone number is banned by Telegram") from None
    except (errors.PhoneNumberInvalidError, errors.PhoneNumberUnoccupiedError) as exc:
        raise InvalidInput(f"{label}: phone number rejected ({type(exc).__name__})") from None

    for attempt in range(1, code_attempts + 1):
        code = await _ask(code_cb, f"[{label}] code sent to {phone}, enter it: ")
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
            return
        except errors.SessionPasswordNeededError:
            await _sign_in_with_password(
                client, label=label, password_cb=password_cb, attempts=password_attempts
            )
            return
        except errors.PhoneCodeInvalidError:
            log.warning("account %s: wrong code (attempt %d/%d)", label, attempt, code_attempts)
        except errors.PhoneCodeExpiredError:
            raise AuthRequired(f"{label}: the code expired; start the login again") from None
        except errors.FloodWaitError as exc:
            raise FloodWait(exc.seconds) from None

    raise AuthRequired(f"{label}: wrong code {code_attempts} times, giving up")


async def _sign_in_with_password(
    client: TelegramClient,
    *,
    label: str,
    password_cb: PasswordCallback | None,
    attempts: int,
) -> None:
    from telethon import errors

    if password_cb is None:
        password_cb = prompt_password
    for attempt in range(1, attempts + 1):
        password = await _ask(password_cb, f"[{label}] two-step verification password: ")
        try:
            await client.sign_in(password=password)
            return
        except errors.PasswordHashInvalidError:
            log.warning("account %s: wrong 2FA password (attempt %d/%d)", label, attempt, attempts)
        except errors.FloodWaitError as exc:
            raise FloodWait(exc.seconds) from None
        finally:
            # Drop the plaintext as early as possible: it must not survive in a
            # frame that a traceback could render.
            password = ""

    raise AuthRequired(f"{label}: wrong 2FA password {attempts} times, giving up")


def normalize_phone(phone: str) -> str:
    cleaned = re.sub(r"[\s()\-]", "", str(phone or ""))
    if not _PHONE_RE.match(cleaned):
        raise InvalidInput(f"{phone!r} does not look like a phone number in E.164 form")
    return cleaned
