"""Directory containment, and the roots an MCP client sanctions.

Two things live here, and they are the same question asked twice: *is this path
inside a directory somebody nominated?* :mod:`telegram_ai_cli.outbox` asks it of
a file about to be sent, against directories the **operator** nominated in the
configuration. The MCP adapter asks it of the download directory, against
directories the **client** nominated over the protocol. One containment check
serves both, because two would be two chances to get symlinks wrong.

**Containment is decided on canonical paths.** ``realpath`` first, comparison
after: a prefix test on the string a caller typed passes ``root/../etc``, and it
passes a symlink that sits inside the root and points at ``/etc/shadow``. It
also passes ``/srv/data-evil`` for a root of ``/srv/data``, which is why the
test is on path components rather than on ``str.startswith``.

**Roots can only narrow.** ``media fetch`` already refuses to take a
destination from the caller — it writes into ``paths.downloads`` and returns an
opaque id. Roots are the client saying which directories it sanctions, and the
only thing this module does with them is refuse. Nothing is ever written
somewhere other than where the configuration says: a silent redirect would mean
the operator's `paths.downloads`, the quota walked against it and the artifact
ids handed out earlier all describe a different directory from the one in use.

**Each operation is checked against the path it actually writes.** Every
``LOCAL_WRITE`` declares one (``Operation.local_path``): a media fetch fills
``paths.downloads``, the archive operations write ``paths.archive``. Checking
all of them against a single directory was the first version of this, and it
was a ceiling applied to the wrong path two operations out of three.

**The two absences are different, and both are honoured.** A client that never
declared the roots capability cannot be asked, so nothing is constrained and
the configured directory stands — that is every client that does not implement
roots, and it must keep working. A client that declared the capability and
answered with an empty list *was* asked, and said none: an empty allow list
means nothing everywhere else in this project, and it means nothing here.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import mcp.types as types

from .errors import NotAllowlisted
from .render import sanitize_line


def resolved(path: Path | str) -> Path:
    """``realpath`` as a ``Path``: symlinks followed, ``..`` collapsed.

    Deliberately not strict about existence. The download directory is created
    by the first fetch, so a check that only worked on an existing directory
    would be absent exactly on first run — the one occasion nobody is watching
    for a refusal.
    """
    return Path(os.path.realpath(Path(path).expanduser()))


def is_within(candidate: Path, root: Path) -> bool:
    """Whether *candidate* is *root* itself, or lies under it.

    Both arguments must already have been through :func:`resolved`; this is the
    comparison, not the canonicalisation, and calling it on raw input is the
    mistake it exists to prevent.
    """
    return candidate == root or root in candidate.parents


def roots_from_uris(uris: Iterable[str]) -> tuple[Path, ...]:
    """Turn a client's advertised roots into absolute local directories.

    The protocol says a root is a ``file://`` URL. Anything else is dropped
    rather than guessed at — an ``https://`` root is not a directory on this
    machine, and a ``file://host/path`` one is not a directory on *this* one.
    Dropping every root a client sent is not the same as a client sending none:
    the result is an empty tuple, which is a refusal, and only ``None`` from
    :func:`advertised_roots` means "unconstrained".
    """
    paths: list[Path] = []
    for uri in uris:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            continue
        raw = unquote(parsed.path)
        # A percent-encoded NUL survives the URL parser and then raises out of
        # ``realpath`` — an unhandled ValueError where a refusal was expected.
        # Dropped like any other unusable root: if it was the only one, the
        # caller is left with an empty tuple, which refuses.
        if not raw or "\x00" in raw or not Path(raw).is_absolute():
            continue
        try:
            paths.append(resolved(raw))
        except (OSError, ValueError):
            continue
    # dict.fromkeys rather than a set: two clients advertising the same root
    # through different symlinks collapse to one, and the order stays the
    # client's, which is the order the refusal message lists them in.
    return tuple(dict.fromkeys(paths))


async def advertised_roots(session: Any) -> tuple[Path, ...] | None:
    """Ask the client which directories it sanctions.

    Returns ``None`` when there is nobody to ask — no session (the CLI, and any
    transport without a back-channel) or a client that never declared the roots
    capability. Returns a tuple otherwise, empty included: see the module
    docstring for why those two are not the same answer.

    A client that declared the capability and then failed to answer is refused
    rather than treated as unconstrained. It said it does roots; a transport
    error is not permission.
    """
    if session is None:
        return None
    roots_capability = types.ClientCapabilities(roots=types.RootsCapability())
    if not session.check_client_capability(roots_capability):
        return None

    with warnings.catch_warnings():
        # roots/list is deprecated in the 2026-07-28 revision (SEP-2577) and the
        # SDK warns on every call. It is still the only way a client says which
        # directories it sanctions, so the warning is suppressed at the call
        # rather than printed to an operator's stderr once per download.
        warnings.simplefilter("ignore")
        try:
            result = await session.list_roots()
        except Exception as exc:  # noqa: BLE001 - any failure is a failure to answer
            raise NotAllowlisted(
                "the client declared the roots capability but did not answer roots/list, "
                "so which directories it sanctions is unknown",
                suggestion=(
                    "Downloads are refused rather than written to an unsanctioned "
                    "directory. Fix the client, or run one that does not advertise roots."
                ),
            ) from exc

    return roots_from_uris(str(root.uri) for root in result.roots)


def require_within_roots(
    destination: Path,
    roots: Sequence[Path] | None,
    *,
    what: str,
) -> None:
    """Refuse a destination the client did not sanction.

    Both the destination and the roots are operator- or client-controlled
    paths, not text anybody wrote into a chat, so they are named in the
    message: ``Envelope.failure`` neither wraps nor defangs, and a refusal that
    will not say which path it refused sends whoever reads it to the wrong
    place. They still go through ``sanitize_line`` — the message is printed to
    a terminal.
    """
    if roots is None:
        return

    target = resolved(destination)
    shown = sanitize_line(str(target), limit=200)

    if not roots:
        raise NotAllowlisted(
            f"{what} would write to {shown}, and the client sanctioned no directory at "
            "all: it answered roots/list with an empty list",
            suggestion=(
                "Add that directory to the client's roots. Nothing is redirected "
                "automatically — a download that lands somewhere other than where it was "
                "configured to land is worse than one that is refused."
            ),
        )

    if any(is_within(target, root) for root in roots):
        return

    listed = ", ".join(sanitize_line(str(root), limit=200) for root in roots)
    raise NotAllowlisted(
        f"{what} would write to {shown}, which is outside every directory the client "
        f"sanctioned ({listed})",
        suggestion=(
            "Point paths.downloads inside one of those directories, or add the download "
            "directory to the client's roots. It is not redirected automatically: the "
            "quota, the artifact ids and the operator's own expectations all describe "
            "the configured directory."
        ),
    )


async def require_sanctioned_path(session: Any, destination: Path, *, what: str) -> None:
    """The whole check, in the order the adapter needs it: ask, then refuse.

    *destination* is the path the operation itself declares it writes into —
    ``paths.downloads`` for a media fetch, ``paths.archive`` for the archive —
    not a single directory assumed on behalf of every local write.
    """
    require_within_roots(destination, await advertised_roots(session), what=what)
