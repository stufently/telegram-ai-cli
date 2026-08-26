"""Error taxonomy with stable, machine-readable codes.

Two audiences read these. A human sees ``message`` and ``suggestion``; a program
(or a model) branches on ``code`` and ``retryable``. That means the codes are
part of the public contract: rename one and you have made a breaking change, so
they live here as an explicit enum rather than as string literals scattered
through call sites.

``retryable`` answers exactly one question — may the caller send the identical
request again? A refusal by policy is not retryable no matter how many times it
is attempted; a flood wait is, once the wait has elapsed.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable identifiers. Values are part of the public JSON contract."""

    # --- caller mistakes -------------------------------------------------
    INVALID_INPUT = "INVALID_INPUT"
    UNKNOWN_OPERATION = "UNKNOWN_OPERATION"
    NOT_FOUND = "NOT_FOUND"

    # --- policy ----------------------------------------------------------
    FORBIDDEN_BY_PROFILE = "FORBIDDEN_BY_PROFILE"
    FORBIDDEN_BY_ALLOWLIST = "FORBIDDEN_BY_ALLOWLIST"
    FORBIDDEN_BY_DENYLIST = "FORBIDDEN_BY_DENYLIST"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    DUPLICATE_OUTBOUND = "DUPLICATE_OUTBOUND"

    # --- plans -----------------------------------------------------------
    PLAN_NOT_FOUND = "PLAN_NOT_FOUND"
    PLAN_NOT_PENDING = "PLAN_NOT_PENDING"
    PLAN_EXPIRED = "PLAN_EXPIRED"
    PLAN_PRECONDITION_FAILED = "PLAN_PRECONDITION_FAILED"
    PLAN_UNKNOWN_OUTCOME = "PLAN_UNKNOWN_OUTCOME"

    # --- accounts and sessions -------------------------------------------
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    SESSION_LOCKED = "SESSION_LOCKED"
    SESSION_REVOKED = "SESSION_REVOKED"
    AUTH_REQUIRED = "AUTH_REQUIRED"

    # --- storage ----------------------------------------------------------
    INSECURE_PERMISSIONS = "INSECURE_PERMISSIONS"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    ARTIFACT_TOO_LARGE = "ARTIFACT_TOO_LARGE"

    # --- optional local components ----------------------------------------
    TRANSCRIBER_UNAVAILABLE = "TRANSCRIBER_UNAVAILABLE"
    TRANSCRIPTION_FAILED = "TRANSCRIPTION_FAILED"

    # --- upstream ---------------------------------------------------------
    FLOOD_WAIT = "FLOOD_WAIT"
    FORWARDS_RESTRICTED = "FORWARDS_RESTRICTED"
    PEER_UNRESOLVED = "PEER_UNRESOLVED"
    PRIVACY_RESTRICTED = "PRIVACY_RESTRICTED"
    TELEGRAM_ERROR = "TELEGRAM_ERROR"

    # --- catch-all --------------------------------------------------------
    INTERNAL = "INTERNAL"


class TelegramAIError(Exception):
    """Base for every expected failure.

    Expected is the operative word: these carry a code and are rendered as a
    clean one-line error. Anything that escapes as a different exception type is
    a bug in this project and is allowed to produce a traceback, because hiding
    it would only make it harder to fix.
    """

    code: ErrorCode = ErrorCode.INTERNAL
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        suggestion: str | None = None,
        retry_after: int | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.suggestion = suggestion
        self.retry_after = retry_after
        self.details = details or {}

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": str(self.code),
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.suggestion:
            payload["suggestion"] = self.suggestion
        if self.retry_after is not None:
            payload["retry_after"] = self.retry_after
        if self.details:
            payload["details"] = self.details
        return payload


# --- caller mistakes -------------------------------------------------------


class InvalidInput(TelegramAIError):
    code = ErrorCode.INVALID_INPUT


class UnknownOperation(TelegramAIError):
    code = ErrorCode.UNKNOWN_OPERATION


class NotFound(TelegramAIError):
    code = ErrorCode.NOT_FOUND


# --- policy ----------------------------------------------------------------


class PolicyError(TelegramAIError):
    """Refused on purpose.

    Never retryable. A caller that retries a policy refusal is either confused
    or being driven by something that wants the refusal to wear down — neither
    is a reason to let the second attempt through.
    """

    code = ErrorCode.FORBIDDEN_BY_PROFILE
    retryable = False


class ProfileForbidden(PolicyError):
    code = ErrorCode.FORBIDDEN_BY_PROFILE


class NotAllowlisted(PolicyError):
    code = ErrorCode.FORBIDDEN_BY_ALLOWLIST


class Denylisted(PolicyError):
    code = ErrorCode.FORBIDDEN_BY_DENYLIST


class LimitExceeded(PolicyError):
    code = ErrorCode.LIMIT_EXCEEDED


class DuplicateOutbound(PolicyError):
    """This exact thing has already gone to this peer, recently.

    Refused rather than skipped. A caller that cannot tell "sent" from "quietly
    did nothing" is worse off than one that gets an error, because it will
    report success for a message the other person never received — or, worse,
    stop looking for the one they received twice. The way to send it again is to
    say so on the plan, where the person approving it can read that it is a
    deliberate repeat.
    """

    code = ErrorCode.DUPLICATE_OUTBOUND


# --- plans -----------------------------------------------------------------


class PlanNotFound(TelegramAIError):
    code = ErrorCode.PLAN_NOT_FOUND


class PlanNotPending(TelegramAIError):
    code = ErrorCode.PLAN_NOT_PENDING


class PlanExpired(TelegramAIError):
    code = ErrorCode.PLAN_EXPIRED


class PlanPreconditionFailed(TelegramAIError):
    """The world moved between planning and applying.

    The classic case is a username that resolved to one account when the plan
    was written and to another by the time it ran. Applying anyway would send
    the message to whoever holds the handle now.
    """

    code = ErrorCode.PLAN_PRECONDITION_FAILED


class PlanUnknownOutcome(TelegramAIError):
    """The request went out; whether it landed is unknown.

    Deliberately not retryable. A timeout after the RPC left is not evidence of
    failure, and an automatic retry here is how one message becomes two.
    """

    code = ErrorCode.PLAN_UNKNOWN_OUTCOME
    retryable = False


# --- accounts --------------------------------------------------------------


class AccountNotFound(TelegramAIError):
    code = ErrorCode.ACCOUNT_NOT_FOUND


class AccountDisabled(TelegramAIError):
    code = ErrorCode.ACCOUNT_DISABLED


class SessionLocked(TelegramAIError):
    """Another process holds this account's auth key.

    Retryable: the holder normally lets go. Two clients on one auth key is not
    a lock we may skip — Telegram treats the collision as a stolen session.
    """

    code = ErrorCode.SESSION_LOCKED
    retryable = True


class SessionRevoked(TelegramAIError):
    code = ErrorCode.SESSION_REVOKED


class AuthRequired(TelegramAIError):
    code = ErrorCode.AUTH_REQUIRED


# --- storage ---------------------------------------------------------------


class InsecurePermissions(TelegramAIError):
    """Refused to use account material we cannot protect.

    Fail closed. A warning here would be worthless: the file stays readable and
    the run continues, which is exactly the outcome the check exists to stop.
    """

    code = ErrorCode.INSECURE_PERMISSIONS


class QuotaExceeded(TelegramAIError):
    code = ErrorCode.QUOTA_EXCEEDED


class ArtifactTooLarge(TelegramAIError):
    code = ErrorCode.ARTIFACT_TOO_LARGE


# --- optional local components ---------------------------------------------


class TranscriberUnavailable(TelegramAIError):
    """The optional transcription image (or its model) is not on this machine.

    Its own code rather than ``NOT_FOUND`` because it says something different:
    nothing is missing from *Telegram*, an optional local component was never
    installed. Not retryable — the identical request will keep failing until a
    person builds the image — and the suggestion carries the command that fixes
    it, because "transcription is unavailable" with no next step is how an
    optional feature becomes an unexplained dead end.
    """

    code = ErrorCode.TRANSCRIBER_UNAVAILABLE
    retryable = False


class TranscriptionFailed(TelegramAIError):
    """The transcriber ran and did not produce a transcript.

    Retryable: the usual causes are a timeout and a container that lost its
    scheduling slot, both of which a second attempt can survive. Deliberately
    carries none of the container's own output — see the class docstring in
    ``telegram_ai_cli_mcp/transcribe.py`` for why that text goes to the audit log
    instead of into a message a model reads.
    """

    code = ErrorCode.TRANSCRIPTION_FAILED
    retryable = True


# --- upstream --------------------------------------------------------------


class FloodWait(TelegramAIError):
    code = ErrorCode.FLOOD_WAIT
    retryable = True

    def __init__(self, seconds: int, **kwargs: object) -> None:
        super().__init__(
            f"Telegram asked us to wait {seconds}s before trying again",
            suggestion="Wait out the interval, or route the work through another account.",
            retry_after=seconds,
            **kwargs,  # type: ignore[arg-type]
        )


class ForwardsRestricted(TelegramAIError):
    """The source chat has content protection on, and Telegram enforces it.

    ``CHAT_FORWARDS_RESTRICTED`` is a server-side refusal: no client bypasses it,
    and this one does not try. Given its own code because the alternative — a
    generic Telegram error carrying a Telethon class name — tells whoever reads
    it nothing about whether to retry, change something, or stop.

    Not retryable, and nothing happened, so the rate-limit slot goes back.
    """

    code = ErrorCode.FORWARDS_RESTRICTED
    retryable = False


class PeerUnresolved(TelegramAIError):
    code = ErrorCode.PEER_UNRESOLVED


class PrivacyRestricted(TelegramAIError):
    """Telegram declined on the target's privacy settings.

    Not an error to retry: it reports a choice the other person made. Telegram
    also reports it without raising — an invite comes back listing the people it
    did not add — so this gets constructed from a successful-looking response.
    """

    code = ErrorCode.PRIVACY_RESTRICTED


class TelegramError(TelegramAIError):
    code = ErrorCode.TELEGRAM_ERROR
    retryable = True
