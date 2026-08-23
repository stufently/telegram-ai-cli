"""Choosing a file to send — the download rule, pointed the other way.

:mod:`telegram_ai_cli.ops.media` refuses to let a caller choose where a
downloaded file *lands*, because a caller-chosen destination is a write of
arbitrary bytes to an arbitrary path. Sending has the same shape reversed: a
caller that names any path on the host has a read of arbitrary bytes with a
delivery mechanism attached — ``~/.ssh/id_ed25519``, the ``.session`` file that
*is* the account, the plan database, someone's tax return — and the destination
is a chat other people read. Nothing about "it is only a chat tool" contains
that.

So the rule is deliberately symmetric with the download rule: **the file must
already be inside a directory the operator nominated.** ``paths.uploads`` is
that directory. The download root joins it only when
``upload.allow_downloads_dir`` says so, and it is off by default — a file this
tool fetched is a file a *stranger* chose, and re-posting one into another chat
should be a decision an operator takes once in the configuration rather than
one a tool call takes on its own.

Three details carry the weight.

**Containment is decided after symlinks are resolved.** A prefix check on the
string the caller typed passes a link that sits inside the root and points at
``/etc/shadow``. The path is realpathed first, and the result has to be under a
root.

**A relative name is read from the outbox, never from the process's working
directory.** The cwd is whatever happened to launch the server; treating it as
an input would make the same argument mean different files in different
sessions.

**Size and type are answered from a descriptor, not from a name.** The file is
opened once, with ``O_NOFOLLOW`` and ``O_NONBLOCK``, and everything after that
comes from ``fstat`` on that descriptor: a size checked by uploading until it
fails costs the transfer and fails halfway, and a name checked with ``stat()``
before a separate ``open()`` can be a FIFO by the time it is opened — which
would block for ever, before any timeout is armed.

The outbox must not be writable by other users, because the entire rule rests
on the files in it having been put there by whoever configured this.

What this does *not* claim: someone who can already write inside the outbox can
swap the file between the digest and the upload. That is not the hole being
closed here — the point is that a *caller* cannot name a path outside it — and
the plan records a sha256 the applier recomputes, so a swap outside that window
is caught rather than sent. A hard link into the outbox is the same story: it
takes write access to the directory, and anyone with that could copy the file
in instead. The refusal that matters is the one aimed at the tool call.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import stat
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path

from .config import TELEGRAM_MAX_UPLOAD_BYTES, Settings
from .errors import (
    ArtifactTooLarge,
    InsecurePermissions,
    InvalidInput,
    NotAllowlisted,
    NotFound,
)
from .render import sanitize_line
from .roots import is_within, resolved

#: Read in this many bytes at a time when digesting. Large enough not to make a
#: syscall per line, small enough not to hold a 100 MiB file in memory.
_CHUNK = 1024 * 1024

#: Characters a file name may not contain. NUL cannot reach a syscall at all,
#: and a newline would break the audit record and the review line into two —
#: both of which a person reads as though each line were one fact.
_FORBIDDEN_IN_PATH = ("\x00", "\n", "\r")


class Delivery(StrEnum):
    """How Telegram will present the file, which is not a cosmetic choice.

    A photo sent as a photo is re-encoded and the original file is gone; sent
    as a document the bytes arrive intact but nothing shows a preview. The
    person approving the plan is entitled to know which of the two happens.
    """

    PHOTO = auto()
    VIDEO = auto()
    VOICE = auto()
    AUDIO = auto()
    DOCUMENT = auto()


#: Extension groups, matched to what Telethon actually does rather than to what
#: "an image" means in ordinary speech — a preview derived from anything else
#: would describe a send that does not happen.
#:
#: ``telethon.utils.is_image`` recognises **png and jpeg only**; every other
#: picture format (``.webp``, ``.bmp``, ``.heic``) is uploaded as a document
#: with its bytes intact. Listing those as photos here would put "Telegram
#: re-encodes it" in front of a person for a send that re-encodes nothing.
_PHOTO_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})
_VOICE_SUFFIXES = frozenset({".ogg", ".oga", ".opus"})
_AUDIO_SUFFIXES = frozenset({".mp3", ".m4a", ".flac", ".wav", ".aac"})


@dataclass(frozen=True, slots=True)
class OutboundFile:
    """A file that has been proven sendable, described well enough to approve."""

    path: Path
    """Absolute, symlinks resolved, known to be under one of the roots."""

    name: str
    """Base name, with control characters stripped: it is about to be printed."""

    size_bytes: int
    sha256: str
    mime: str
    delivery: Delivery
    forced_document: bool
    root: Path

    @property
    def as_document(self) -> bool:
        """Whether the applier passes ``force_document`` to Telethon."""
        return self.delivery is Delivery.DOCUMENT

    def snapshot(self) -> dict[str, object]:
        """The half of a plan's preconditions that describes the file.

        The digest is what makes "the file that was reviewed is the file that
        was sent" checkable at apply time. Size travels with it because a
        truncated file with a colliding prefix is not a thing worth arguing
        about, but a cheap second condition is free.
        """
        return {
            "name": self.name,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "delivery": str(self.delivery),
            "path": str(self.path),
        }


def upload_roots(settings: Settings) -> tuple[Path, ...]:
    """Directories a file may be sent from, in the order they are searched.

    A relative root is refused rather than resolved. ``Path("")`` is ``Path(".")``
    and ``realpath(".")`` is the process's working directory — so an outbox
    configured as an empty string would silently make *wherever this was
    launched from* the allowlist, which for a shell started in ``$HOME`` means
    ``.ssh``. The configuration refuses that too; this is the second lock, for
    a ``Settings`` built some other way.
    """
    configured = [("paths.uploads", settings.paths.uploads)]
    if settings.upload.allow_downloads_dir:
        configured.append(("paths.downloads", settings.paths.downloads))

    roots: list[Path] = []
    for key, value in configured:
        expanded = value.expanduser()
        if not expanded.is_absolute():
            raise InvalidInput(
                f"{key} must be an absolute path, not {sanitize_line(str(value), limit=200)!r}",
                suggestion=(
                    "A relative outbox resolves against whatever directory this process was "
                    "started in, which is not a permission anybody granted."
                ),
            )
        roots.append(resolved(expanded))
    return tuple(roots)


def _require_private_root(root: Path) -> None:
    """Refuse an outbox other users can write into.

    The whole rule rests on one assumption: what is in this directory was put
    there by whoever configured the tool. A group- or world-writable outbox
    breaks it — anybody on the machine could drop a file in and let the next
    approved plan carry it out — and it also widens the window between the
    digest and the upload from "someone with write access" to "anybody".

    Not fixed automatically: this is a directory a person owns and keeps files
    in, and quietly re-permissioning it is a bigger liberty than refusing.
    """
    mode = root.stat().st_mode
    if mode & 0o022:
        raise InsecurePermissions(
            f"{sanitize_line(str(root), limit=200)} is writable by other users "
            f"(mode {stat.filemode(mode)})",
            suggestion=f"chmod go-w {root} — a file this tool sends must be one you put there.",
        )


def human_bytes(value: int) -> str:
    """A size a person reads at a glance, next to the exact count elsewhere."""
    if value < 1024:
        return f"{value} B"
    size = float(value)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        size /= 1024
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
    raise AssertionError("unreachable")  # pragma: no cover


def classify(name: str, *, as_document: bool) -> Delivery:
    """Which form the file goes in, from its extension.

    Extension rather than content sniffing on purpose: it is what Telethon
    itself keys on, so a preview derived from anything else would describe a
    send that does not happen.
    """
    if as_document:
        return Delivery.DOCUMENT
    suffix = Path(name).suffix.lower()
    if suffix in _PHOTO_SUFFIXES:
        return Delivery.PHOTO
    if suffix in _VIDEO_SUFFIXES:
        return Delivery.VIDEO
    if suffix in _VOICE_SUFFIXES:
        return Delivery.VOICE
    if suffix in _AUDIO_SUFFIXES:
        return Delivery.AUDIO
    return Delivery.DOCUMENT


def describe_delivery(file: OutboundFile) -> str:
    """One sentence saying what arrives at the other end.

    Written for the person approving the plan, so each branch says what happens
    to the *bytes* — that is the part that cannot be undone once it is sent.
    """
    match file.delivery:
        case Delivery.PHOTO:
            # The only form that loses the file. Everything else below is
            # uploaded byte for byte and differs only in how it is presented.
            return (
                "as a compressed photo — Telegram re-encodes the image, so the original "
                "file is not what arrives; set as_document to send the bytes unchanged"
            )
        case Delivery.VIDEO:
            return (
                "as a video — the bytes are uploaded unchanged, and it arrives playable "
                "in the chat rather than as a file to download; set as_document for a "
                "plain file"
            )
        case Delivery.VOICE:
            return (
                "as a voice message — the bytes are uploaded unchanged, and it plays as a "
                "waveform rather than arriving as a file; set as_document for a plain file"
            )
        case Delivery.AUDIO:
            return (
                "as an audio track — the bytes are uploaded unchanged, and it plays in the "
                "music player rather than arriving as a file; set as_document for a plain file"
            )
        case _:
            if file.forced_document:
                return "as a document, because as_document was set — the bytes arrive unchanged"
            return (
                "as a document — Telegram has no compressed form for this type, so the "
                "bytes arrive unchanged"
            )


def file_preview(file: OutboundFile) -> str:
    """The block a person reads before approving a send.

    Everything here is either caller-supplied or derived locally, and it is all
    sanitised anyway: a file name is text somebody chose, and a terminal will
    happily let an escape sequence in one redraw the line above it.
    """
    return "\n".join(
        (
            "--- file ---",
            f"  name:     {sanitize_line(file.name, limit=200)}",
            f"  size:     {human_bytes(file.size_bytes)} ({file.size_bytes} bytes)",
            f"  type:     {file.mime}",
            f"  sha256:   {file.sha256}",
            f"  from:     {sanitize_line(str(file.root), limit=200)}",
            f"  sent:     {describe_delivery(file)}",
        )
    )


def _measure(settings: Settings, path: Path, *, shown: str) -> tuple[int, str]:
    """Open the file once, and answer size and digest from that descriptor.

    Three flags, each closing something a ``stat()``-then-``open()`` pair leaves
    open:

    ``O_NOFOLLOW`` — the path was realpathed, so its last component is not a
    symlink *now*; without the flag one substituted in between would be
    followed.

    ``O_NONBLOCK`` — opening a FIFO for reading blocks until a writer appears,
    for ever. A named pipe put where a regular file was is otherwise a way to
    hang the process before any timeout is armed. The flag is meaningless for
    regular files, which is what this is allowed to be.

    ``fstat`` on the descriptor rather than ``stat`` on the name — the type and
    the size then describe the object being read, not whatever the name pointed
    at a moment ago.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise InvalidInput(
                f"{shown} is not a regular file",
                suggestion="One file per plan: a directory, pipe, socket or device is not one.",
            )
        size = info.st_size
        if size == 0:
            raise InvalidInput(f"{shown} is empty; Telegram refuses a zero-byte file")

        ceiling = min(settings.upload.max_file_bytes, TELEGRAM_MAX_UPLOAD_BYTES)
        if size > ceiling:
            # Refused before a byte is read, let alone uploaded: the point of a
            # ceiling is not to discover the size the expensive way.
            raise ArtifactTooLarge(
                f"{shown} is {size} bytes ({human_bytes(size)}), over the "
                f"{ceiling}-byte ({human_bytes(ceiling)}) ceiling",
                suggestion=(
                    "Raise upload.max_file_bytes if the file is genuinely wanted — up to "
                    f"{TELEGRAM_MAX_UPLOAD_BYTES} bytes, which is Telegram's own limit."
                ),
            )

        digest = hashlib.sha256()
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return size, digest.hexdigest()


def resolve_outbound(
    settings: Settings,
    raw_path: str,
    *,
    as_document: bool = False,
) -> OutboundFile:
    """Turn a caller's path into a file that is provably allowed to leave.

    Raises rather than returns a verdict: every failure here is a refusal a
    caller has to act on, and each one names what to do about it.
    """
    text = raw_path.strip()
    if not text:
        raise InvalidInput("no file was named")
    if any(bad in text for bad in _FORBIDDEN_IN_PATH):
        raise InvalidInput(
            "a file path may not contain a newline or a null byte",
            suggestion="Rename the file, or send it from a path without control characters.",
        )

    roots = upload_roots(settings)
    if not any(root.is_dir() for root in roots):
        # Said plainly, because the alternative message is "no such file", which
        # sends whoever configured this looking for the wrong problem.
        listed = ", ".join(sanitize_line(str(root), limit=200) for root in roots)
        raise NotFound(
            f"there is no directory to send files from: paths.uploads ({listed}) does not exist",
            suggestion="Create it, and put the files this account may send inside it.",
        )

    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        # Relative to the outbox, never to the process's working directory.
        candidate = roots[0] / candidate

    # One containment check, shared with the MCP roots ceiling: canonicalise,
    # then compare on path components. See `telegram_ai_cli.roots`.
    real = resolved(candidate)
    root = next((r for r in roots if is_within(real, r)), None)
    if root is None:
        listed = ", ".join(sanitize_line(str(r), limit=200) for r in roots)
        raise NotAllowlisted(
            f"{sanitize_line(text, limit=200)} is not inside a directory files may be "
            f"sent from ({listed})",
            suggestion=(
                "Put the file under paths.uploads, or add the download directory with "
                "upload.allow_downloads_dir. A path outside those is refused so that a "
                "tool call cannot post an arbitrary file from this machine."
            ),
        )

    shown = sanitize_line(text, limit=200)
    _require_private_root(root)

    if not real.exists():
        raise NotFound(
            f"no such file: {shown}",
            suggestion=f"Files are sent from {sanitize_line(str(root), limit=200)}.",
        )
    if real.is_dir():
        # Answered by name before opening, only because "is not a regular file"
        # is a poorer message for the commonest mistake than naming it.
        raise InvalidInput(
            f"{shown} is a directory, not a regular file",
            suggestion="One file per plan; a directory cannot be sent.",
        )

    size, sha256 = _measure(settings, real, shown=shown)
    mime = mimetypes.guess_type(real.name)[0] or "application/octet-stream"
    return OutboundFile(
        path=real,
        name=sanitize_line(real.name, limit=200),
        size_bytes=size,
        sha256=sha256,
        mime=mime,
        delivery=classify(real.name, as_document=as_document),
        forced_document=as_document,
        root=root,
    )
