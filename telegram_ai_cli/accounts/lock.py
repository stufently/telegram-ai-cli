"""One auth key, one connected client — enforced with ``flock``.

A Telegram session is a single 256-byte auth key. Two connections sharing it
desynchronise the message sequence, and Telegram may respond by revoking the
session outright. That is unrecoverable without the phone, so the collision is
refused rather than survived.

``flock`` is the right primitive here because the lock belongs to the *open file
description*: a second ``acquire`` fails even inside the same process, which is
the case that actually happens — the CLI and the MCP server both reaching for
the same account. It is also released by the kernel when the process dies, so a
crash does not strand an account forever the way a lock row in a database would.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterable
from pathlib import Path

from ..errors import SessionLocked
from .fs import FILE_MODE, ensure_private_dir


class SessionLock:
    """Exclusive advisory lock over one auth key."""

    __slots__ = ("_fd", "path")

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> SessionLock:
        ensure_private_dir(self.path.parent)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = _read_holder(fd)
            os.close(fd)
            raise SessionLocked(
                f"this account's session is already in use{holder}",
                suggestion="Stop the other process, or wait for it to finish.",
                details={"lock": str(self.path)},
            ) from None
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.fsync(fd)
        except BaseException:
            # The lock is ours from the moment flock returned. Failing to record
            # the pid must not strand it: an fd nobody can reach still holds the
            # lock until the process exits.
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> SessionLock:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


class SessionLocks:
    """Acquire every identity of one auth key as one indivisible guard.

    A session always has a stable canonical-path identity and, once its file
    exists, an inode identity as well.  Acquiring both prevents the creation
    transition from changing locks while retaining protection for hard links.
    """

    __slots__ = ("_locks", "paths")

    def __init__(self, paths: Iterable[Path]) -> None:
        unique = tuple(dict.fromkeys(Path(path) for path in paths))
        if not unique:
            raise ValueError("at least one session lock path is required")
        self.paths = unique
        self._locks: list[SessionLock] = []

    def acquire(self) -> SessionLocks:
        try:
            for path in self.paths:
                self._locks.append(SessionLock(path).acquire())
        except BaseException:
            self.release()
            raise
        return self

    def release(self) -> None:
        while self._locks:
            self._locks.pop().release()

    @property
    def held(self) -> bool:
        return bool(self._locks)

    def __enter__(self) -> SessionLocks:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _read_holder(fd: int) -> str:
    """Name the blocking process, when it managed to write its pid."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        pid = os.read(fd, 32).decode(errors="replace").strip()
    except OSError:
        return ""
    return f" (pid {pid})" if pid.isdigit() else ""
