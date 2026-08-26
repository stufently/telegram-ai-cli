"""Shared plumbing for the reading operations.

Three concerns live here because every read operation has all three, and a copy
of each in six modules is six chances to forget one:

**The hard floor is re-stated, not re-decided.** :mod:`telegram_ai_cli_mcp.safety`
answers "may I read this peer" per capability, but enumeration has no peer
capability to consult — a dialog list is produced before anything has been
allowlisted. So the unconditional part of the kernel's floor (Service
Notifications, Saved Messages) is available here as a predicate that listing
code can apply per row. It duplicates a rule; it does not invent one.

**Redaction and the trust boundary happen once, at the point results are
assembled.** Doing both in a helper that also sets ``redacted`` and
``untrusted_content`` means a field added to a payload later is covered by
default instead of by memory. They answer different questions — redaction is
about privacy, the markers are about a model mistaking a stranger's sentence
for an instruction — and neither substitutes for the other, so both run.

**Sanitising is deliberately NOT done here.** Control-character stripping is a
rendering concern: the CLI applies it when printing and ``--raw`` opts out. If
operations sanitised, ``--raw`` could never show the original, and the flag
would be a lie.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..config import HARD_DENIED_PEERS, HARD_DENY_SAVED_MESSAGES, Settings
from ..context import OperationContext
from ..envelope import Envelope, Meta, TruncationReason
from ..errors import (
    Denylisted,
    FloodWait,
    PeerUnresolved,
    PolicyError,
    SessionRevoked,
    TelegramError,
)
from ..redact import redact_mapping
from ..safety import Capability, PeerKind, PeerRef
from ..untrusted import CLOSE_MARKER, OPEN_MARKER, wrap, wrap_untrusted

#: Hard ceiling on any page of Telegram objects, published in every schema that
#: has a ``limit``. A caller that wants more pages with ``before_id`` rather
#: than discovering the cap by having output silently cut.
MAX_PAGE = 500

#: How many dialogs a listing may walk while filtering. Enumeration is the
#: cheapest reconnaissance step there is, so it has a ceiling of its own that
#: is independent of how many rows the caller asked to see.
MAX_DIALOG_SCAN = 1000


class ReadInput(BaseModel):
    """Base for every read operation's input.

    ``extra="forbid"`` is the point of inheriting it. A misspelled argument that
    validates silently is worse over MCP than over a CLI: nobody sees the typo,
    the operation runs with a default, and the answer looks authoritative.
    """

    model_config = ConfigDict(extra="forbid")

    account: str | None = Field(
        default=None,
        description="Account label to use. Omit when exactly one account is configured.",
    )


def redaction_enabled(settings: Settings) -> bool:
    """Whether recognisable secrets are masked before results leave.

    On by default and, today, unconditionally: the configuration has no key to
    turn it off. It is read through one function so that adding such a key
    later is a one-line change rather than a search for every call site.
    """
    return bool(getattr(settings, "redact_output", True))


def hard_denied(peer: PeerRef) -> bool:
    """The part of the kernel's floor that applies with no capability in hand.

    Used by listings, which have to decide row by row before any capability is
    consulted. Kept in step with :class:`telegram_ai_cli_mcp.safety.SafetyKernel`
    by reading the same constants rather than by repeating the ids.
    """
    if peer.peer_id in HARD_DENIED_PEERS or peer.kind is PeerKind.SERVICE:
        return True
    return HARD_DENY_SAVED_MESSAGES and peer.kind is PeerKind.SELF


def require_peer(
    ctx: OperationContext,
    capability: Capability,
    peer: PeerRef,
    *,
    action: str,
) -> None:
    """Ask the kernel, and write down every refusal.

    A refusal is a security-relevant event: a run of them against one chat is
    what "somebody is probing" looks like, and it is invisible if only the
    successes reach the log.
    """
    try:
        ctx.safety.require(capability, peer)
    except PolicyError as exc:
        ctx.audit.refusal(action=action, actor=ctx.actor, reason=exc.message, peer_id=peer.peer_id)
        raise


def require_enumeration(ctx: OperationContext, *, private: bool, action: str) -> None:
    """Gate a listing, recording the refusal like any other."""
    try:
        ctx.safety.require_enumeration(private=private)
    except PolicyError as exc:
        ctx.audit.refusal(action=action, actor=ctx.actor, reason=exc.message)
        raise


def guard_hard_denied(ctx: OperationContext, peer: PeerRef, *, action: str) -> None:
    """Refuse a peer that no configuration may open, and say so in the log."""
    if not hard_denied(peer):
        return
    reason = (
        "this chat is closed in code: it carries login codes, second factors "
        "or personal storage, and no configuration can open it"
    )
    ctx.audit.refusal(action=action, actor=ctx.actor, reason=reason, peer_id=peer.peer_id)
    raise Denylisted(reason)


def telegram_result(
    ctx: OperationContext,
    data: Any,
    *,
    account: str | None = None,
    returned: int | None = None,
    total: int | None = None,
    truncated: bool = False,
    truncated_reason: TruncationReason | None = None,
    warnings: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> Envelope:
    """Wrap a payload that contains text written by strangers.

    Three things travel with it. ``untrusted_content`` tells a model that
    message bodies, titles and display names are data and not instructions, and
    the markers around those values inside ``data`` say *which spans* they are —
    the flag alone only says "somewhere in this document", which is not a
    boundary. ``truncated`` says the answer was cut — without it, a short list
    reads as "that is all there is", which is the failure mode that makes an
    agent conclude a chat is empty when it merely paged.

    Order matters: redaction runs first, and the markers go on last. A secret is
    masked whether or not it sits in a marked field, and nothing that runs after
    the wrapping can reintroduce a delimiter into the content it framed.
    """
    redacted = redaction_enabled(ctx.settings)
    body = redact_mapping(data) if redacted else data
    body = wrap_untrusted(body)
    safe_warnings = redact_mapping(warnings or []) if redacted else warnings or []
    # A warning is prose, so it cannot structurally identify which substring
    # came from Telegram. Frame the whole sentence: over-marking our connective
    # words is safe, while leaving an interpolated title bare is not.
    safe_warnings = [wrap(str(item)) for item in safe_warnings]
    safe_extra = redact_mapping(extra or {}) if redacted else extra or {}
    safe_extra = wrap_untrusted(safe_extra)
    meta = Meta(
        returned=returned,
        total=total,
        truncated=truncated,
        truncated_reason=truncated_reason if truncated else None,
        account=account,
        redacted=redacted,
        untrusted_content=True,
        untrusted_markers=(OPEN_MARKER, CLOSE_MARKER),
        extra=safe_extra,
    )
    return Envelope.success(body, warnings=safe_warnings, meta=meta)


def iso(value: datetime | None) -> str | None:
    """Timestamps as UTC ISO-8601, or nothing at all.

    Telethon hands back timezone-aware datetimes; rendering them with the local
    offset of whichever machine ran the query makes two logs of the same events
    disagree.
    """
    if value is None:
        return None
    return value.isoformat()


@contextlib.contextmanager
def telegram_errors(*, what: str) -> Iterator[None]:
    """Translate Telethon's exceptions into this project's taxonomy.

    Matching is on exception *class*, never on message text: Telegram's strings
    change between layers, and a caller branching on ``code`` must not have the
    meaning of a failure shift underneath it. Telethon is imported here rather
    than at module scope so that ``--help`` and the test suite work on a machine
    that has never installed it.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - re-raised, mapped or untouched
        from telethon import errors as tl_errors

        if isinstance(exc, tl_errors.FloodWaitError):
            raise FloodWait(int(getattr(exc, "seconds", 0))) from exc
        if isinstance(exc, _named(tl_errors, "AuthKeyDuplicatedError", "UserDeactivatedError")):
            raise SessionRevoked(
                f"{what}: Telegram invalidated this session; the account must sign in again"
            ) from exc
        if isinstance(
            exc,
            _named(
                tl_errors,
                "UsernameNotOccupiedError",
                "UsernameInvalidError",
                "InviteHashInvalidError",
                "InviteHashExpiredError",
            ),
        ):
            raise PeerUnresolved(f"{what}: no such user, chat or invite") from exc
        if isinstance(exc, _named(tl_errors, "ChannelPrivateError", "ChatAdminRequiredError")):
            raise PeerUnresolved(
                f"{what}: this account cannot see that chat",
                suggestion="Check that the account is a member and has the rights to read it.",
            ) from exc
        if isinstance(exc, tl_errors.RPCError):
            raise TelegramError(f"{what}: {type(exc).__name__}") from exc
        raise


def _named(module: Any, *names: str) -> tuple[type[BaseException], ...]:
    """Look error classes up by name instead of importing them.

    Telethon generates most of its error classes from the API layer, so the set
    is not identical across versions. A missing attribute here would raise
    ``AttributeError`` from inside an exception handler and replace a clean
    ``FLOOD_WAIT`` with a traceback about the error translator itself.
    """
    found = (getattr(module, name, None) for name in names)
    return tuple(cls for cls in found if isinstance(cls, type) and issubclass(cls, BaseException))
