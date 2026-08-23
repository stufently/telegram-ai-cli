"""``telegram_media_fetch`` — download one message's attachment, safely.

This is the only read-side operation that writes to the machine, and the whole
design of it comes from one refusal: **the caller does not choose the path.**

A destination parameter here would be a file-write primitive with a Telegram
front end. Whoever controls the tool call controls the path, and the path could
be the config file, the plan database, the audit log, a shell profile, or the
session file that *is* the account. That is not a theoretical escalation — the
attachment content is attacker-chosen too, so it is a write of arbitrary bytes
to an arbitrary location. There is no flag for it and no "trusted caller" mode.

What happens instead: the file lands in the configured download directory under
a generated name, created with ``O_CREAT|O_EXCL|O_NOFOLLOW`` at ``0600`` so it
cannot land on an existing file or be redirected through a symlink someone
planted. Size, time and total-quota ceilings all apply while the bytes arrive,
not just from the size Telegram advertised. The caller gets an opaque
``artifact_id`` back.

Effect is ``LOCAL_WRITE`` rather than ``READ`` for the same reason: it consumes
disk, and something that consumes a shared resource should not be classified
with the operations that consume nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
from pathlib import Path
from typing import Any

from pydantic import Field

from ..context import OperationContext
from ..envelope import Envelope
from ..errors import (
    ArtifactTooLarge,
    InsecurePermissions,
    NotFound,
    QuotaExceeded,
    TelegramError,
)
from ..opspec import REGISTRY, Effect, Operation
from ..safety import Capability
from ._client import open_account
from ._common import (
    ReadInput,
    guard_hard_denied,
    require_peer,
    telegram_errors,
    telegram_result,
)
from ._serialize import media_summary, peer_ref, peer_summary
from .chats import resolve_chat

#: Extensions are taken from Telegram-supplied metadata, so they are treated as
#: hostile input: a short, conservative shape or nothing at all.
_SAFE_EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,8}$")


class MediaFetchInput(ReadInput):
    """Note what is *absent*: there is no destination parameter, by design."""

    chat: str = Field(description="Chat id, @username, or t.me link.")
    message_id: int = Field(ge=1, description="Id of the message whose attachment to fetch.")


class _CappedWriter:
    """A file object that refuses to grow past its ceiling.

    The advertised size is checked first, but it is metadata from the other
    side and may be absent or wrong. Counting the bytes as they land is what
    actually enforces the limit, and raising mid-stream aborts the transfer
    instead of discovering the overrun once the disk is full.
    """

    def __init__(self, handle: Any, *, max_bytes: int, quota_bytes: int) -> None:
        self._handle = handle
        self._max = max_bytes
        self._quota = quota_bytes
        self._digest = hashlib.sha256()
        self.written = 0

    def write(self, chunk: bytes) -> int:
        self.written += len(chunk)
        if self.written > self._max:
            raise ArtifactTooLarge(
                f"attachment exceeds download.max_file_bytes ({self._max} bytes)",
                suggestion="Raise download.max_file_bytes if this file is genuinely wanted.",
            )
        if self.written > self._quota:
            raise QuotaExceeded(
                "the download directory would exceed download.total_quota_bytes",
                suggestion="Clear old artifacts, or raise download.total_quota_bytes.",
            )
        self._digest.update(chunk)
        return self._handle.write(chunk)

    def flush(self) -> None:
        self._handle.flush()

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()


def _directory_usage(root: Path) -> int:
    """Bytes already held in the download directory.

    Walked each time rather than tracked in a counter: the directory is a
    normal place a person deletes files from, and a stored total would drift
    into refusing downloads because of files that no longer exist.
    """
    total = 0
    if not root.exists():
        return 0
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            total += path.stat().st_size
    return total


def _extension(message: Any) -> str:
    handle = getattr(message, "file", None)
    candidate = getattr(handle, "ext", None) or ""
    return candidate if _SAFE_EXTENSION.match(candidate) else ".bin"


def _create_artifact(root: Path, name: str) -> int:
    """Create the destination file, or fail — never open an existing one.

    ``O_EXCL`` means a name collision is an error rather than an overwrite, and
    ``O_NOFOLLOW`` means a symlink placed at that name is an error rather than
    a write to wherever it points. ``0600`` is set at creation, so there is no
    window in which the file exists and is readable by others.
    """
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # ``mode`` only applies when mkdir actually creates the directory, and a
    # download root that already exists may be group-readable from before. The
    # narrowing is a refusal if it fails: continuing would leave attachments
    # readable by other users and the run looking successful.
    if root.stat().st_mode & 0o077:
        try:
            root.chmod(0o700)
        except OSError as exc:
            raise InsecurePermissions(
                f"cannot narrow {root} to 0700: {exc}",
                suggestion="Point paths.downloads at a directory this user owns.",
            ) from None
    return os.open(
        root / name,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
        0o600,
    )


async def handle_media_fetch(ctx: OperationContext, params: MediaFetchInput) -> Envelope:
    download = ctx.settings.download
    root = ctx.settings.paths.downloads

    async with open_account(ctx, params.account) as account:
        entity = await resolve_chat(account.client, params.chat, what="media.fetch")
        ref = peer_ref(entity)
        guard_hard_denied(ctx, ref, action="media.fetch")
        require_peer(ctx, Capability.READ_MEDIA, ref, action="media.fetch")

        with telegram_errors(what="media.fetch"):
            message = await account.client.get_messages(entity, ids=params.message_id)
        if message is None or getattr(message, "media", None) is None:
            raise NotFound(f"message {params.message_id} in that chat has no attachment to fetch")

        advertised = getattr(getattr(message, "file", None), "size", None)
        if advertised and advertised > download.max_file_bytes:
            raise ArtifactTooLarge(
                f"attachment is {advertised} bytes, over download.max_file_bytes "
                f"({download.max_file_bytes})"
            )

        remaining = download.total_quota_bytes - _directory_usage(root)
        if remaining <= 0 or (advertised and advertised > remaining):
            raise QuotaExceeded(
                "the download directory is at download.total_quota_bytes",
                suggestion="Clear old artifacts, or raise download.total_quota_bytes.",
            )

        artifact_id = secrets.token_hex(16)
        path = root / f"{artifact_id}{_extension(message)}"
        fd = _create_artifact(root, path.name)

        event = ctx.audit.attempt(
            action="media.fetch",
            account=account.label,
            actor=ctx.actor,
            peer_id=ref.peer_id,
            extra={"message_id": params.message_id, "artifact_id": artifact_id},
        )

        try:
            with os.fdopen(fd, "wb") as raw:
                writer = _CappedWriter(
                    raw, max_bytes=download.max_file_bytes, quota_bytes=remaining
                )
                try:
                    with telegram_errors(what="media.fetch"):
                        await asyncio.wait_for(
                            account.client.download_media(message, file=writer),
                            timeout=download.timeout_seconds,
                        )
                except TimeoutError as exc:
                    # Retryable on purpose: a slow transfer is not a refusal,
                    # and the partial file is removed below either way.
                    raise TelegramError(
                        f"download exceeded download.timeout_seconds ({download.timeout_seconds}s)",
                        retry_after=download.timeout_seconds,
                    ) from exc
                raw.flush()
                os.fsync(raw.fileno())
        except BaseException as exc:
            # A partial artifact is worse than none: it looks like a complete
            # file, counts against the quota, and would be read as truth.
            path.unlink(missing_ok=True)
            ctx.audit.outcome(
                event, status="failed", error_code=type(exc).__name__, detail=str(exc)[:200]
            )
            raise

        ctx.audit.outcome(event, status="applied", detail=f"{writer.written} bytes")

    data: dict[str, Any] = {
        "artifact_id": artifact_id,
        "bytes": writer.written,
        "sha256": writer.sha256,
        "chat": peer_summary(entity),
        "message_id": params.message_id,
        "media": media_summary(message),
    }
    if ctx.actor == "cli":
        # The person at the terminal owns this directory and needs to open the
        # file; a tool caller gets the opaque id, because a path it did not
        # choose is not a path it needs to know.
        data["path"] = str(path)

    return telegram_result(ctx, data, account=account.label, returned=1, total=1)


MEDIA_FETCH = REGISTRY.register(
    Operation(
        name="media.fetch",
        cli=("media", "fetch"),
        mcp_tool="telegram_media_fetch",
        summary="Download one message's attachment into the managed download directory.",
        description=(
            "Writes to a server-chosen path under a generated name and returns an opaque "
            "artifact_id. There is no destination parameter: a caller-chosen path would "
            "let this tool overwrite configuration, plans or the session file itself. "
            "Size, timeout and total quota all apply."
        ),
        input_model=MediaFetchInput,
        effect=Effect.LOCAL_WRITE,
        capability=Capability.READ_MEDIA,
        handler=handle_media_fetch,  # type: ignore[arg-type]
        tags=("read", "media"),
    )
)
