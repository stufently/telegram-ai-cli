"""Where an account's files live, and how its auth key is named for locking.

Two questions are answered here.

*Which files belong to this label?* :class:`SessionPaths` is the single answer,
so nothing else has to know that a session string lands in ``<label>.string``
or that the device fingerprint is ``<label>.api.json``.

*Which lock protects this auth key?* Not the label — the key. Locking by label
was the original defect: a ``.session`` reachable under two names (a symlink, a
hard link, or simply two rows pointing at the same file) produced two different
lock files, both acquirable, and two clients on one auth key is the collision
Telegram treats as a stolen session. The lock is therefore keyed on the
artefact's identity — ``st_dev`` plus ``st_ino`` — which no alias can change.
Before the artefact exists there is no key yet, so the fallback key is the
*canonical* path, with symlinks already resolved.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..errors import InvalidInput
from .fs import ensure_private_dir

MAX_LABEL_LEN: Final = 64

_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_LABEL_RUNS = re.compile(r"_{2,}")

#: All lock files live together, because their names are derived from inode
#: identity rather than from anything a human would recognise.
LOCK_DIR_NAME: Final = ".locks"


def sanitize_label(label: str) -> str:
    """Reduce a user-supplied label to one safe filesystem component.

    The label becomes ``sessions/<label>.session``, so separators, ``..`` and a
    leading dot that would hide the file are stripped rather than rejected, and
    the result is re-checked against :attr:`pathlib.Path.name`.
    """
    cleaned = _LABEL_UNSAFE.sub("_", (label or "").strip())
    cleaned = _LABEL_RUNS.sub("_", cleaned).strip("._-")
    if len(cleaned) > MAX_LABEL_LEN:
        cleaned = cleaned[:MAX_LABEL_LEN].rstrip("._-")
    if not cleaned or cleaned in {".", ".."}:
        raise InvalidInput(
            f"label {label!r} contains no usable characters",
            suggestion="Use letters, digits, dot, dash or underscore.",
        )
    if cleaned != Path(cleaned).name or os.sep in cleaned or "/" in cleaned:
        raise InvalidInput(f"label {label!r} is not a single path component")
    return cleaned


@dataclass(frozen=True, slots=True)
class SessionPaths:
    """Every on-disk artefact belonging to one account label."""

    sessions_dir: Path
    label: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "sessions_dir", Path(self.sessions_dir))
        object.__setattr__(self, "label", sanitize_label(self.label))

    @property
    def session_file(self) -> Path:
        return self.sessions_dir / f"{self.label}.session"

    @property
    def api_file(self) -> Path:
        """Frozen device fingerprint; see :mod:`telegram_ai_cli.accounts.api_profile`."""
        return self.sessions_dir / f"{self.label}.api.json"

    @property
    def string_file(self) -> Path:
        return self.sessions_dir / f"{self.label}.string"

    @property
    def tdata_dir(self) -> Path:
        return self.sessions_dir / "tdata" / self.label

    @property
    def lock_dir(self) -> Path:
        return self.sessions_dir / LOCK_DIR_NAME

    def prepare(self) -> SessionPaths:
        ensure_private_dir(self.sessions_dir)
        return self

    def owned_paths(self) -> tuple[Path, ...]:
        """Artefacts safe to delete when the account is removed.

        Lock files are deliberately absent. Deleting one while a process holds
        the ``flock`` frees only the *name*; the next acquirer creates a fresh
        inode and wins a second lock on the very key the first one is using.
        """
        session = self.session_file
        return (
            session,
            session.with_name(session.name + "-journal"),
            session.with_name(session.name + "-wal"),
            session.with_name(session.name + "-shm"),
            self.api_file,
            self.string_file,
            self.tdata_dir,
        )


def auth_key_id(target: Path) -> str:
    """Stable path identity for the artefact holding one auth key.

    This identity must not change when Telethon creates the session file.  An
    inode-only identity does exactly that: the first login locks ``path-*``,
    ``connect()`` creates the file, and a second process then locks ``ino-*``
    unopposed.  The canonical-path lock is therefore always held.  Existing
    files also get an inode lock from :func:`auth_key_ids`, preserving the
    hard-link collision protection.
    """
    real = Path(os.path.realpath(target))
    digest = hashlib.sha256(str(real).encode("utf-8")).hexdigest()
    return f"path-{digest[:32]}"


def auth_key_ids(target: Path) -> tuple[str, ...]:
    """Every identity that must be locked for ``target``.

    The path identity closes the absent-to-created transition.  The additional
    inode identity makes two different hard-link paths converge on the same
    lock.  Ordering path first is deliberate and shared by every caller.
    """
    real = Path(os.path.realpath(target))
    identities = [auth_key_id(real)]
    try:
        stat = os.stat(real)
    except OSError:
        return tuple(identities)
    identities.append(f"ino-{stat.st_dev:x}-{stat.st_ino:x}")
    return tuple(identities)


def lock_target(source: str, session_path: str | None, paths: SessionPaths) -> Path:
    """The artefact whose auth key the lock protects.

    For ``tdata`` that is the converted ``.session``, not the folder: the folder
    is only ever read, and only until the first conversion produces the session
    that every later start reuses.
    """
    if source == "tdata":
        return paths.session_file
    if source == "string_session":
        target = Path(session_path) if session_path else paths.string_file
        if not target.is_absolute() and not target.exists():
            return paths.string_file  # a raw string in the column, not a path
        return target
    return Path(session_path) if session_path else paths.session_file


def session_lock_path(source: str, session_path: str | None, paths: SessionPaths) -> Path:
    """Stable primary lock path guarding this account's auth key."""
    target = lock_target(source, session_path, paths)
    return paths.lock_dir / f"{auth_key_id(target)}.lock"


def session_lock_paths(
    source: str, session_path: str | None, paths: SessionPaths
) -> tuple[Path, ...]:
    """All lock paths guarding an auth key, including its inode when present."""
    target = lock_target(source, session_path, paths)
    return tuple(paths.lock_dir / f"{identity}.lock" for identity in auth_key_ids(target))
