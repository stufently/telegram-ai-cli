"""Interactive login, for the CLI — by phone code, or by scanning a QR code.

Produces exactly what the loader expects from a ``session_file`` account: a
``0600`` ``.session`` in the sessions directory plus a frozen device fingerprint
beside it. Both flows share one body (:func:`_authorise`) so that "signed in"
means the same file, the same permissions and the same lock either way; they
differ only in how the account proves who it is.

The two-step verification password is read with :func:`getpass.getpass` and is
never accepted from ``argv`` or the environment. A command line is visible in
``ps`` output to every user on the host and lands in shell history; an
environment variable is readable from ``/proc`` for the life of the process.
Neither the password nor the login code is logged, at any level.

**The QR login token is a credential of the same rank.** ``tg://login?token=…``
*is* the login: anything that imports it becomes the account, with no code and
no password. So it goes to the terminal and nowhere else — not to the log at any
level, not to the audit file, not into an error message. That is why the URL
reaches a display callback rather than a logger, and why nothing here returns
it to a caller.

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
import sys
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
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
#: Handed the ``tg://login`` URL to put in front of a person. It must not log,
#: store or forward it — see the module docstring.
QrDisplay = Callable[[str], None]

_PHONE_RE = re.compile(r"^\+?\d{6,20}$")

DEFAULT_CODE_ATTEMPTS: Final = 3
DEFAULT_PASSWORD_ATTEMPTS: Final = 3
#: How many times an unscanned QR code is replaced before the login gives up.
#: Telegram expires a token in well under a minute, and a person who has walked
#: off to find their phone should get a few codes rather than one — but a login
#: that redraws forever in front of an empty chair holds the account's session
#: lock while it does it.
DEFAULT_QR_REGENERATIONS: Final = 4


@dataclass(frozen=True, slots=True)
class LoginResult:
    label: str
    phone: str
    session_path: Path
    user_id: int
    username: str | None = None


@dataclass(frozen=True, slots=True)
class _Authorised:
    """What both flows learn once the client is signed in."""

    paths: SessionPaths
    user_id: int
    username: str | None
    phone: str
    proxy_url: str | None


def session_file_lock(paths: SessionPaths) -> Path:
    """The lock guarding the ``.session`` a login writes.

    One expression, used by the connection and by the caller that has to take
    the lock before it — two copies would be two locks the day either one of
    them changed, and two locks over one auth key is the collision this whole
    mechanism exists to refuse.
    """
    return session_lock_path(str(AccountSource.SESSION_FILE), str(paths.session_file), paths)


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
    lock: SessionLock | None = None,
) -> LoginResult:
    """Sign one phone number in and leave an authorised session on disk.

    Idempotent: an already authorised ``<label>.session`` is reported as-is
    rather than triggering another code request, which Telegram rate limits
    aggressively.
    """
    phone = normalize_phone(phone)

    async def sign_in(client: TelegramClient, name: str) -> None:
        await _sign_in(
            client,
            phone=phone,
            label=name,
            code_cb=code_cb or prompt_code,
            password_cb=password_cb,
            code_attempts=code_attempts,
            password_attempts=password_attempts,
        )

    done = await _authorise(
        label=label,
        sessions_dir=sessions_dir,
        api_id=api_id,
        api_hash=api_hash,
        proxy_url=proxy_url,
        settings=settings,
        box=box,
        sign_in=sign_in,
        lock=lock,
    )
    log.info(
        "account %s logged in: user_id=%s phone=%s proxy=%s",
        done.paths.label,
        done.user_id,
        mask_secret(phone, keep=3),
        redact_proxy_url(done.proxy_url),
    )
    return LoginResult(
        label=done.paths.label,
        phone=phone,
        session_path=done.paths.session_file,
        user_id=done.user_id,
        username=done.username,
    )


async def qr_login(
    *,
    label: str,
    sessions_dir: Path,
    api_id: int | None = None,
    api_hash: str | None = None,
    proxy_url: str | None = None,
    settings: Settings | None = None,
    box: SecretBox | None = None,
    display: QrDisplay | None = None,
    password_cb: PasswordCallback | None = None,
    password_attempts: int = DEFAULT_PASSWORD_ATTEMPTS,
    regenerations: int = DEFAULT_QR_REGENERATIONS,
    lock: SessionLock | None = None,
) -> LoginResult:
    """Sign in by showing a QR code that an authorised Telegram app scans.

    No phone number is needed and nothing is typed: the code carries a one-shot
    login token, and the app that scans it authorises this session. Everything
    afterwards — the ``0600`` session file, the frozen fingerprint, the lock —
    is identical to :func:`interactive_login`, because it is the same code path.

    Idempotent in the same way: an already authorised session is reported as-is
    rather than asking Telegram for a token nobody needs.
    """

    async def sign_in(client: TelegramClient, name: str) -> None:
        await _sign_in_via_qr(
            client,
            label=name,
            display=display or show_qr,
            password_cb=password_cb,
            password_attempts=password_attempts,
            regenerations=regenerations,
        )

    done = await _authorise(
        label=label,
        sessions_dir=sessions_dir,
        api_id=api_id,
        api_hash=api_hash,
        proxy_url=proxy_url,
        settings=settings,
        box=box,
        sign_in=sign_in,
        lock=lock,
    )
    log.info(
        "account %s logged in by QR: user_id=%s phone=%s proxy=%s",
        done.paths.label,
        done.user_id,
        mask_secret(done.phone, keep=3),
        redact_proxy_url(done.proxy_url),
    )
    return LoginResult(
        label=done.paths.label,
        phone=done.phone,
        session_path=done.paths.session_file,
        user_id=done.user_id,
        username=done.username,
    )


async def _authorise(
    *,
    label: str,
    sessions_dir: Path,
    api_id: int | None,
    api_hash: str | None,
    proxy_url: str | None,
    settings: Settings | None,
    box: SecretBox | None,
    sign_in: Callable[[TelegramClient, str], Awaitable[None]],
    lock: SessionLock | None = None,
) -> _Authorised:
    """Connect under the account's lock, sign in if needed, harden what is left.

    Both login flows run through here, which is the point: the session file, its
    permissions, the lock and the "already authorised, do nothing" shortcut are
    written once and cannot drift apart between the two.

    ``lock`` is for a caller that had to take this account's lock *before*
    calling — :func:`login_and_register` does, so that the ``accounts`` row is
    written under the same lock the connection will run under. Passing it is not
    an optimisation: taking it twice would fail, because ``flock`` is held by the
    open file description rather than by the process.
    """
    from .loader import telethon_options

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
    client: TelegramClient | None = None
    # A lock supplied by the caller is already held over this same auth key, and
    # `flock` belongs to the open file description: acquiring it a second time
    # fails even inside one process. So the caller's lock is used as-is, and
    # released by whoever took it.
    borrowed = lock is not None
    lock = lock if lock is not None else SessionLock(session_file_lock(paths)).acquire()
    try:
        # Inside the try: a failure while constructing the client must still hand
        # the auth key back, or the next attempt reports a holder that is gone.
        client = new_client(str(paths.session_file), api, proxy, telethon_options(settings))
        await client.connect()
        if not await client.is_user_authorized():
            await sign_in(client, paths.label)
        me = await client.get_me()
        if me is None:
            raise AuthRequired(f"{paths.label}: signed in but Telegram returned no user")
        user_id = int(me.id)
        username = getattr(me, "username", None)
        phone = _phone_of(me)
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
            try:
                harden_path(paths.session_file)
            finally:
                # Also in a finally: hardening touches the filesystem and can
                # fail, and an auth key held by a lock nobody will release is
                # an account that stays unusable until the process exits.
                if not borrowed:
                    lock.release()

    return _Authorised(
        paths=paths,
        user_id=user_id,
        username=username,
        phone=phone,
        proxy_url=proxy_url,
    )


def _phone_of(me: Any) -> str:
    """The account's own number in E.164 form, or ``""`` if Telegram withheld it.

    Only the QR flow needs this — a phone login already knows the number it was
    given. It is never logged unmasked and never printed.
    """
    raw = str(getattr(me, "phone", "") or "").strip()
    if not raw:
        return ""
    return raw if raw.startswith("+") else f"+{raw}"


@contextmanager
def _account_offline(store: AccountStore, paths: SessionPaths) -> Iterator[SessionLock]:
    """Hold this account's auth key before the row naming it is rewritten.

    ``upsert`` moves ``phone``, ``source``, ``session_path`` and ``status`` —
    the four columns the loader reads to decide what to connect. Writing them
    while another process has the account connected repoints a row underneath a
    live client, and the failure that follows (``SessionLocked``, raised a
    moment later by the connection this login was about to make) leaves the
    damage behind and adds ``auth_failed`` to it. Taking the lock first turns
    that into a refusal with nothing written, which is what
    :meth:`AccountRegistry.register_phone_login` already does for the row it
    writes.

    Usually one lock: a login that is *not* repointing the label finds the row
    already naming this same ``.session``. With ``--replace`` it can be two —
    the material the row names now, and the session file about to be written —
    and both have to be still before either is touched. The second is yielded,
    because it is the one the connection itself will run under.
    """
    session_lock = session_file_lock(paths)
    record = store.get(paths.label)
    also: SessionLock | None = None
    if record is not None:
        current = session_lock_path(str(record.source), record.session_path, paths)
        if current != session_lock:
            also = SessionLock(current).acquire()
    try:
        held = SessionLock(session_lock).acquire()
    except BaseException:
        if also is not None:
            also.release()
        raise
    try:
        yield held
    finally:
        try:
            held.release()
        finally:
            if also is not None:
                also.release()


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
    file nobody knows about. It is written *under the account's lock* for the
    other half of that: an interruption is one thing, and rewriting a row a
    running client is reading from is another. See :func:`_account_offline`.

    A failed login still leaves the row, marked ``auth_failed``. Restoring what
    was there before would undo, silently, the repointing the operator asked
    for and leave no record that it was attempted; the recorded error is that
    record.
    """
    paths = SessionPaths(Path(sessions_dir), label)
    with _account_offline(store, paths) as lock:
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
                lock=lock,
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


async def qr_login_and_register(
    store: AccountStore,
    *,
    label: str,
    sessions_dir: Path,
    phone: str | None = None,
    api_id: int | None = None,
    api_hash: str | None = None,
    proxy_url: str | None = None,
    settings: Settings | None = None,
    box: SecretBox | None = None,
    display: QrDisplay | None = None,
    password_cb: PasswordCallback | None = None,
    replace: bool = False,
) -> LoginResult:
    """:func:`qr_login` plus the ``accounts`` row — the same row, written the
    same way and at the same moment as :func:`login_and_register` writes it.

    ``phone`` is whatever the row already knew, and it is only what the row
    carries *while the login runs*: a QR code can be scanned by any account the
    person happens to be signed in as, so once one has, the number stored is the
    number of whoever actually signed in — read from ``get_me`` — and not the
    one that was there before. Keeping the old value would leave a row whose
    phone belongs to one account and whose session belongs to another, and it is
    that row a later ``account login`` would send a code to.
    """
    paths = SessionPaths(Path(sessions_dir), label)
    with _account_offline(store, paths) as lock:
        record = store.upsert(
            paths.label,
            AccountSource.SESSION_FILE,
            session_path=str(paths.session_file),
            phone=normalize_phone(phone) if phone else None,
            api_id=api_id,
            api_hash=api_hash,
            proxy_url=validate_proxy_url(proxy_url),
            replace=replace,
        )
        try:
            result = await qr_login(
                label=record.label,
                sessions_dir=sessions_dir,
                api_id=api_id,
                api_hash=api_hash,
                proxy_url=proxy_url,
                settings=settings,
                box=box,
                display=display,
                password_cb=password_cb,
                lock=lock,
            )
        except TelegramAIError as exc:
            # Redacted for the same reason the phone flow redacts it: last_error is
            # displayed verbatim, and it outlives the terminal it was raised in.
            clean = redact_secrets(str(exc), api_hash, proxy_password(proxy_url))
            store.set_status(record.label, AccountStatus.AUTH_FAILED, clean)
            raise

        store.set_user_id(record.label, result.user_id)
        if result.phone != (record.phone or ""):
            # Whoever scanned the code is the account now on this row. An unknown
            # number clears the column rather than leaving the previous one: an
            # empty phone is a gap, a stale one is a wrong answer that a later
            # `account login` would act on.
            store.set_phone(record.label, result.phone or None)
        store.set_status(record.label, AccountStatus.OK, None)
    return result


def require_display_terminal() -> None:
    """Refuse before a token exists if there is nowhere private to draw it.

    Called *before* the login asks Telegram for a token, not at display time: a
    credential that has been minted and cannot be shown is a credential that
    existed for no reason. Fail-closed, because the alternative is writing a
    live login token into whatever the caller redirected the output to.
    """
    stream = _terminal_writer()
    if stream is None:
        raise InvalidInput(
            "a QR login has to draw the code on a terminal, and this process has none",
            suggestion="Run it from a terminal, or use `tg-ai account login --phone …` instead.",
        )
    if stream is not sys.stderr:
        stream.close()


def _terminal_writer() -> Any | None:
    """The person's terminal, or ``None`` if this process is not attached to one.

    Deliberately *not* ``stdout``: stdout may be a file, a pipe or a caller
    parsing JSON, and the login token must reach none of those. This is the same
    answer :func:`getpass.getpass` gives to the same question — the controlling
    terminal if there is one, ``stderr`` if it is itself a terminal, and
    otherwise nothing at all.
    """
    try:
        return open("/dev/tty", "w", encoding="utf-8")  # noqa: SIM115 - closed by the caller
    except OSError:
        pass
    try:
        return sys.stderr if sys.stderr is not None and sys.stderr.isatty() else None
    except (AttributeError, ValueError):  # pragma: no cover - a closed stderr
        return None


def show_qr(url: str, *, invert: bool = False) -> None:
    """Default display: the code on the terminal, with the raw URL under it.

    The URL is printed deliberately. A terminal that cannot draw block
    characters, an SSH session through something that mangles them, a screen
    reader — in all of those the drawing is useless and the link still works
    when opened on a device already signed in. It is printed to the terminal,
    never to stdout, never logged: see the module docstring for why those
    distinctions are the whole point.
    """
    from .qr import render_qr

    stream = _terminal_writer()
    if stream is None:  # pragma: no cover - the caller checks this first
        raise InvalidInput("there is no terminal to draw the QR code on")
    try:
        stream.write(render_qr(url, invert=invert))
        stream.write(
            "\n\nScan it from an app that is already signed in: "
            "Settings → Devices → Link Desktop Device.\n"
            "If the code will not render, open this link on that device instead:\n"
            f"{url}\n\n"
        )
        stream.flush()
    finally:
        if stream is not sys.stderr:
            stream.close()


@contextmanager
def _as_flood_wait(errors: Any) -> Iterator[None]:
    """Turn Telethon's flood wait into this project's, wherever it comes from.

    ``errors`` is passed in rather than imported here so the module keeps its
    "Telethon is imported inside the function that needs it" shape.
    """
    try:
        yield
    except errors.FloodWaitError as exc:
        raise FloodWait(exc.seconds) from None


async def _sign_in_via_qr(
    client: TelegramClient,
    *,
    label: str,
    display: QrDisplay,
    password_cb: PasswordCallback | None = None,
    password_attempts: int = DEFAULT_PASSWORD_ATTEMPTS,
    regenerations: int = DEFAULT_QR_REGENERATIONS,
) -> None:
    """Show a login QR code until it is scanned, it expires too often, or 2FA.

    Expiry has two shapes and both mean the same thing. Telethon's ``wait()``
    raises :class:`TimeoutError` when the token's own deadline passes with
    nobody scanning; Telegram answers ``AUTH_TOKEN_EXPIRED`` when a scan lands
    just after it — the race between a camera and a clock a person cannot avoid.
    Neither is an error to report: the token is regenerated and redrawn, a
    bounded number of times, so an abandoned login ends rather than holding the
    account's session lock indefinitely.

    Two-step verification arrives as ``SessionPasswordNeededError`` after the
    scan, and is handed to the same password prompt the phone login uses.
    """
    from telethon import errors

    # Requesting a token is an RPC like any other, so a flood wait here has to
    # become this project's error rather than escaping as a raw Telethon one —
    # the caller records `auth_failed` on a `TelegramAIError` and nothing else.
    with _as_flood_wait(errors):
        qr = await client.qr_login()

    for redraw in range(regenerations + 1):
        # Straight to the display callback, never through the log: this string
        # is the credential.
        display(qr.url)
        try:
            await qr.wait()
            return
        except errors.SessionPasswordNeededError:
            await _sign_in_with_password(
                client, label=label, password_cb=password_cb, attempts=password_attempts
            )
            return
        except errors.FloodWaitError as exc:
            raise FloodWait(exc.seconds) from None
        except (TimeoutError, errors.AuthTokenExpiredError):
            if redraw >= regenerations:
                break
            log.info(
                "account %s: the QR code expired unscanned, showing another (%d/%d)",
                label,
                redraw + 1,
                regenerations,
            )
            with _as_flood_wait(errors):
                await qr.recreate()

    raise AuthRequired(
        f"{label}: the QR code expired {regenerations + 1} times without being "
        "scanned; start the login again"
    )


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
