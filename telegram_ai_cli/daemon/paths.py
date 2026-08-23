"""Where an account's daemon socket lives, and what may be bound there.

The rules are the ones :mod:`telegram_ai_cli.accounts.fs` already states for
session material, applied to a listening socket: the directory is ``0700``, it
is owned by this user, and nothing is followed through a symlink. They are
reused rather than restated — a second, slightly different path rule is how one
of them ends up weaker than the other without anyone noticing.

Why ownership matters here and not only permissions: whoever owns the
*directory* can rename or replace what is inside it, so a socket in a directory
belonging to someone else is a socket they choose the identity of, while every
local client reaching it believes it is talking to this tool.

The socket path is derived from the label, never from a caller's string:
``sanitize_label`` reduces it to one path component, so ``../../tmp`` cannot
name a directory outside the daemon root.
"""

from __future__ import annotations

import stat
from pathlib import Path

from ..accounts.fs import ensure_private_dir, require_owned
from ..accounts.paths import sanitize_label
from ..config import Settings
from ..errors import InsecurePermissions, InvalidInput

#: Directory under `paths.state` holding one subdirectory per account.
DAEMON_DIR_NAME = "daemon"

#: The socket, and the lock held only while a daemon is claiming the socket.
#: Short on purpose — see :data:`MAX_SOCKET_PATH_BYTES`; the directory is
#: already named `daemon/<label>/`, so a longer file name says nothing more.
SOCKET_NAME = "sock"
BOOTSTRAP_LOCK_NAME = "bootstrap.lock"

#: A Unix socket address is a fixed-size field, not a string: ``sun_path`` is
#: 108 bytes on Linux and 104 on the BSDs, including the terminating NUL. A path
#: over the limit fails inside ``bind`` with a bare ``OSError: AF_UNIX path too
#: long``, so it is measured here instead and refused by name. 103 is the BSD
#: figure less the NUL, which is the safe number to hold everywhere.
MAX_SOCKET_PATH_BYTES = 103


def daemon_root(settings: Settings) -> Path:
    return Path(settings.paths.state) / DAEMON_DIR_NAME


def account_dir(settings: Settings, label: str) -> Path:
    """The per-account state directory. ``label`` is reduced to one component."""
    return daemon_root(settings) / sanitize_label(label)


def socket_path(settings: Settings, label: str) -> Path:
    return account_dir(settings, label) / SOCKET_NAME


def bootstrap_lock_path(settings: Settings, label: str) -> Path:
    return account_dir(settings, label) / BOOTSTRAP_LOCK_NAME


def require_socket_path_fits(path: Path) -> Path:
    """Refuse a socket path longer than an address can hold.

    Checked before the directory is created rather than discovered inside
    ``bind``: the kernel's message is ``AF_UNIX path too long`` with no mention
    of which path or what the limit is, and the fix — a shorter label, or a
    shallower ``paths.state`` — is not guessable from it.
    """
    encoded = len(str(path).encode("utf-8"))
    if encoded > MAX_SOCKET_PATH_BYTES:
        raise InvalidInput(
            f"the daemon socket path is {encoded} bytes, over the {MAX_SOCKET_PATH_BYTES}-byte "
            "limit a Unix socket address can hold",
            suggestion=(
                "Use a shorter account label, or point paths.state at a shallower directory. "
                "The rest of the tool works normally without a daemon."
            ),
            details={"path": str(path), "limit_bytes": MAX_SOCKET_PATH_BYTES},
        )
    return path


def prepare_account_dir(settings: Settings, label: str) -> Path:
    """Create the per-account directory ``0700``, and prove it is ours.

    Both roots are checked, not only the leaf: a daemon root somebody else owns
    lets them swap the account directory underneath us.
    """
    directory = account_dir(settings, label)
    require_socket_path_fits(directory / SOCKET_NAME)
    ensure_private_dir(directory)
    require_owned(directory.parent)
    require_owned(directory)
    return directory


def require_bindable(path: Path) -> bool:
    """Say whether a socket file is already sitting at ``path``, or refuse.

    Returns ``True`` when something that *is* a socket is there — which may be
    a live daemon or the remains of a killed one; telling those apart takes a
    connection attempt, which is the caller's next step. Returns ``False`` when
    the path is free.

    A symlink and a non-socket file are both refusals rather than something to
    clean up: unlinking either would delete a file this tool did not create, at
    a path somebody else chose.

    A path that is simply not there — including one whose directory does not
    exist yet — is ``False`` and not a refusal. "No daemon has ever run for this
    account" is the ordinary first answer, and a client asking about one must
    get "there isn't one", not a permissions error about a missing directory.
    """
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise InsecurePermissions(f"cannot stat {path}: {exc}") from None

    if stat.S_ISLNK(info.st_mode):
        raise InsecurePermissions(
            f"{path} is a symlink; a daemon socket must be a real socket",
            suggestion="Remove the link. This tool will not bind through one.",
        )
    if not stat.S_ISSOCK(info.st_mode):
        raise InvalidInput(
            f"{path} exists and is not a socket",
            suggestion="Move or delete it; the daemon will not overwrite it.",
        )
    # Only once something is actually there: a socket we own inside a directory
    # somebody else owns is still a socket they can replace.
    require_owned(path.parent)
    require_owned(path)
    return True
