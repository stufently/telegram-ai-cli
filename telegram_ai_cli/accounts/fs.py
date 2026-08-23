"""Filesystem primitives for material that *is* the account.

A Telethon ``.session`` holds a 256-byte auth key. Whoever reads that file owns
the account: no password, no second factor, nothing else is needed. So every
helper here is written for the case where the file must never be readable by
anyone but this user, not even for the instant between ``open()`` and
``chmod()``.

Three rules, each of which exists because the obvious alternative fails quietly.

**Permission failures are refusals, not warnings.** Logging "cannot chmod" and
carrying on leaves the key world-readable *and* the run successful, which is
precisely the outcome the check was added to prevent.

**Reads never follow symlinks.** Hardening deliberately skips links so that a
``chmod`` cannot be aimed at an arbitrary file elsewhere on the host — but that
only protects the mode bits. The later *read* is what hands over the contents,
so it opens with ``O_NOFOLLOW`` and fails on a link rather than dereferencing
one someone else planted.

**Writes are atomic and private from birth.** Create at ``0600`` with
``O_EXCL``, fsync, rename into place. A partially written session file that a
reader picks up is a corrupt session; a briefly world-readable one is a leak.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Final

from ..errors import InsecurePermissions, InvalidInput

DIR_MODE: Final = 0o700
FILE_MODE: Final = 0o600

#: Any bit here means somebody other than the owner can reach the file.
_OTHERS: Final = 0o077

_COPY_CHUNK: Final = 1024 * 256


def chmod_or_fail(path: Path, mode: int) -> None:
    """Set ``mode``, or refuse to continue.

    The failure that matters is a read-only bind mount holding session files
    someone else can read. Continuing there means running the fleet out of
    material we have no way to protect.
    """
    try:
        path.chmod(mode)
    except OSError as exc:
        raise InsecurePermissions(
            f"cannot set mode {mode:o} on {path}: {exc}",
            suggestion="Move the sessions directory somewhere this user owns, then retry.",
        ) from None


def require_private(path: Path) -> None:
    """Refuse account material that group or others can read."""
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        raise InsecurePermissions(f"cannot stat {path}: {exc}") from None
    if mode & _OTHERS:
        raise InsecurePermissions(
            f"{path} is mode {mode:o}; account material must not be readable by anyone else",
            suggestion=f"chmod 600 {path}",
        )


def require_no_symlink(path: Path) -> Path:
    """Reject a symlinked artefact before anything opens it.

    Telethon and opentele are handed a *path* and open it themselves, so
    ``O_NOFOLLOW`` is not available at that call site. Checking here is the only
    place where a link swapped in under the sessions directory can be caught.
    """
    if path.is_symlink():
        raise InsecurePermissions(
            f"{path} is a symlink; account material must be a real file or directory",
            suggestion="Import the target itself instead of a link to it.",
        )
    return path


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` and its parents as ``0700``."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InvalidInput(f"cannot create {path}: {exc}") from None
    chmod_or_fail(path, DIR_MODE)
    return path


def harden_path(path: Path) -> None:
    """Force ``0600`` on files and ``0700`` on directories, recursively.

    Symlinks *inside* a tree are skipped rather than followed: a backup may
    legitimately contain links, and ``chmod`` through one would change the mode
    of whatever it points at, anywhere on the host. The top of the tree is not
    allowed to be a link at all — that is the artefact we are about to use.
    """
    if not path.exists():
        return
    require_no_symlink(path)
    if not path.is_dir():
        chmod_or_fail(path, FILE_MODE)
        return
    chmod_or_fail(path, DIR_MODE)
    for child in path.rglob("*"):
        if child.is_symlink():
            continue
        chmod_or_fail(child, DIR_MODE if child.is_dir() else FILE_MODE)


def write_private_text(path: Path, text: str) -> Path:
    """Write ``text`` so the file is never readable by anyone else, even briefly."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    chmod_or_fail(path, FILE_MODE)
    return path


def read_private_text(path: Path) -> str:
    """Read a secret file without following a symlink and without widening it.

    ``O_NOFOLLOW`` is the point: hardening refuses to chmod through a link, but
    the code that later reads the profile or the session string would happily
    dereference one. Then the mode check runs on the file we actually opened.
    """
    fd = _open_nofollow(path)
    try:
        stat = os.fstat(fd)
        if stat.st_mode & _OTHERS:
            raise InsecurePermissions(
                f"{path} is mode {stat.st_mode & 0o777:o}; refusing to read account material "
                "that others can read too",
                suggestion=f"chmod 600 {path}",
            )
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1  # ownership moved to the file object
            return handle.read()
    finally:
        if fd >= 0:
            os.close(fd)


def copy_private_file(src: Path, dst: Path) -> Path:
    """Copy a secret file, creating the destination ``0600`` from the start."""
    ensure_private_dir(dst.parent)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    out_fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
    in_fd = -1
    try:
        in_fd = _open_nofollow(src)
        with os.fdopen(out_fd, "wb") as out, os.fdopen(in_fd, "rb") as inp:
            out_fd = in_fd = -1
            while chunk := inp.read(_COPY_CHUNK):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        for fd in (out_fd, in_fd):
            if fd >= 0:
                os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, dst)
    chmod_or_fail(dst, FILE_MODE)
    return dst


def _open_nofollow(path: Path) -> int:
    """``open(2)`` with ``O_NOFOLLOW``; a symlink becomes a refusal, not a read.

    ``ELOOP`` is the errno Linux reports for exactly that case, and it is worth
    translating: the raw message ("Too many levels of symbolic links") reads as
    a broken path rather than as the security decision it is.
    """
    try:
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise InsecurePermissions(
                f"{path} is a symlink; refusing to read account material through one"
            ) from None
        raise
