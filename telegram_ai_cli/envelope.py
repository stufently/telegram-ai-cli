"""The single response shape shared by the CLI and the MCP server.

Both surfaces return the same JSON. That is not tidiness — it is what lets the
MCP server stay a thin adapter. The moment the two render results differently,
there are two implementations to keep correct, and the second one is the one
nobody tests.

Two fields in ``meta`` carry weight beyond bookkeeping:

``truncated``
    Set whenever output was cut. Limits are published in each operation's JSON
    Schema, so a caller can ask for the right page instead of guessing why the
    answer looks short. Silent truncation reads as "that is all there is".

``untrusted_content``
    Set on anything containing text that came from Telegram. Message bodies,
    chat titles and display names are written by strangers, and a model reading
    this output must treat them as data. The flag is how the envelope says so
    in-band, rather than relying on the reader to remember.

``untrusted_markers``
    The flag says the response contains such text; it does not say *where*.
    These are the delimiters the human-authored values inside ``data`` are
    wrapped in (see :mod:`telegram_ai_cli.untrusted`), published so that a
    parser can strip them deterministically instead of hard-coding a literal
    that may change.

**A refusal is inside that boundary too.** A result is assembled by
``ops._common.telegram_result``, which redacts and then wraps; an error is
assembled from an exception and goes nowhere near it. That left ``error`` the
one field in the response where a stranger's text could arrive carrying its own
delimiters — in the part a reader trusts most, because it is normally this
project speaking. So :meth:`Envelope.failure` walks the error payload through
the same pass: every string is defanged, and a value under a human-authored
field name (a chat title in ``details``) is delimited like any other.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal, Self

from .errors import TelegramAIError
from .untrusted import CLOSE_MARKER, OPEN_MARKER, has_untrusted_field, wrap_untrusted

TruncationReason = Literal["limit", "budget", "quota", "size"]


@dataclass(slots=True)
class Meta:
    """Everything about the answer that is not the answer itself."""

    returned: int | None = None
    total: int | None = None
    truncated: bool = False
    truncated_reason: TruncationReason | None = None
    account: str | None = None
    redacted: bool = False
    untrusted_content: bool = False
    untrusted_markers: tuple[str, str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.returned is not None:
            payload["returned"] = self.returned
        if self.total is not None:
            payload["total"] = self.total
        if self.truncated:
            # Only meaningful alongside the flag, and misleading without it.
            payload["truncated"] = True
            if self.truncated_reason:
                payload["truncated_reason"] = self.truncated_reason
        if self.account is not None:
            payload["account"] = self.account
        if self.redacted:
            payload["redacted"] = True
        if self.untrusted_content:
            payload["untrusted_content"] = True
            if self.untrusted_markers:
                # Only alongside the flag: markers announced on a payload that
                # carries none would send a parser looking for them.
                payload["untrusted_markers"] = {
                    "open": self.untrusted_markers[0],
                    "close": self.untrusted_markers[1],
                }
        payload.update(self.extra)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> Meta:
        """Rebuild meta that has been through JSON.

        Needed because a result may now arrive from the account daemon rather
        than from a handler in this process. Anything not a declared field goes
        back into ``extra``, so a round trip neither invents fields nor drops
        the ones an operation added.
        """
        data = dict(payload or {})
        markers = data.pop("untrusted_markers", None)
        known = {
            name: data.pop(name)
            for name in ("returned", "total", "truncated", "truncated_reason", "account")
            if name in data
        }
        redacted = bool(data.pop("redacted", False))
        untrusted = bool(data.pop("untrusted_content", False))
        return cls(
            **known,
            redacted=redacted,
            untrusted_content=untrusted,
            untrusted_markers=(
                (markers["open"], markers["close"]) if isinstance(markers, dict) else None
            ),
            extra=data,
        )


@dataclass(slots=True)
class Envelope:
    """A result, or a refusal, in the one shape every caller can parse."""

    ok: bool
    data: Any = None
    warnings: list[str] = field(default_factory=list)
    meta: Meta = field(default_factory=Meta)
    error: dict[str, Any] | None = None

    @classmethod
    def success(
        cls,
        data: Any,
        *,
        warnings: list[str] | None = None,
        meta: Meta | None = None,
    ) -> Self:
        return cls(ok=True, data=data, warnings=warnings or [], meta=meta or Meta())

    @classmethod
    def failure(cls, exc: TelegramAIError, *, meta: Meta | None = None) -> Self:
        """Build a refusal, with the error payload inside the trust boundary.

        The message itself is *not* wrapped: this project composed that
        sentence, and delimiting it would claim a stranger wrote our own words.
        Defanging it is what matters — it makes any text quoted inside it
        incapable of forging a marker.

        The flag is set only when something was actually delimited — not merely
        when the pass changed the payload. Those are different: a message whose
        text contained a forged delimiter is defanged and carries no markers
        afterwards, so announcing them there would send a parser looking for
        delimiters that were never written, which is the same lie in the other
        direction as omitting the flag when they were.
        """
        raw = exc.to_dict()
        error = wrap_untrusted(raw)
        meta = meta or Meta()
        if has_untrusted_field(raw):
            meta = replace(
                meta,
                untrusted_content=True,
                untrusted_markers=(OPEN_MARKER, CLOSE_MARKER),
            )
        return cls(ok=False, data=None, meta=meta, error=error)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Self:
        """The inverse of :meth:`to_dict`, for a result that arrived over a socket.

        Used by the account-daemon client: the daemon ran the operation and
        assembled the envelope, and this rebuilds it so both surfaces print the
        same object whether the work happened here or one process away.
        """
        meta = Meta.from_dict(payload.get("meta"))
        if payload.get("ok"):
            return cls(
                ok=True,
                data=payload.get("data"),
                warnings=list(payload.get("warnings") or []),
                meta=meta,
            )
        return cls(ok=False, data=None, meta=meta, error=dict(payload.get("error") or {}))

    def to_dict(self) -> dict[str, Any]:
        if self.ok:
            payload: dict[str, Any] = {"ok": True, "data": self.data}
            if self.warnings:
                payload["warnings"] = self.warnings
        else:
            payload = {"ok": False, "error": self.error}
        meta = self.meta.to_dict()
        if meta:
            payload["meta"] = meta
        return payload

    @property
    def exit_code(self) -> int:
        """What the CLI should exit with.

        Zero only on success. A caller piping ``--json`` into another program
        must be able to branch on the exit status without parsing the body.
        """
        return 0 if self.ok else 1
