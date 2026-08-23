"""Running one operation — in this process, or on the account's daemon.

The CLI and the MCP server used to hold the same twelve lines each: parse,
build a context, call the handler or the planner, wrap a refusal in an
envelope. They now share them, which is what keeps a change in *how* an
operation is run from having to be made twice and being made once.

The daemon is the reason this exists as a module rather than a helper. When
`daemon.enabled` is on and the request names an account with a daemon
listening, the work happens over there, on the client that daemon already has
open; otherwise it happens here exactly as before. Two rules govern that
choice:

**Only a request that names an account is routed.** A daemon serves one
account, so a fleet-wide call with no ``account`` would come back covering one
of them under a name that promises all. Those run locally.

**The fallback happens only if no daemon answered.** Once a request has left,
a failure is a failure. Retrying a planner locally after a mid-flight timeout
is how one plan becomes two, which is the same reasoning that makes
`PlanUnknownOutcome` deliberately not retryable.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .config import Settings, load_settings
from .context import Actor, OperationContext
from .daemon import client as daemon_client
from .daemon import paths as daemon_paths
from .daemon.service import policy_fingerprint
from .envelope import Envelope, Meta
from .errors import TelegramAIError
from .opspec import Effect, Operation
from .render import sanitize
from .untrusted import CLOSE_MARKER, OPEN_MARKER, wrap

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlanRecord:
    """A plan as both surfaces need it, before either has phrased it."""

    plan_id: str
    operation: str
    summary: str
    state: str


def plan_envelope(plan: PlanRecord, *, next_key: str, next_step: str) -> Envelope:
    """The plan result, marked as carrying somebody else's words.

    A summary quotes the title of the destination chat and the body being
    edited. It is assembled here rather than by `telegram_result`, so the
    boundary is applied explicitly — without it a hostile chat title would
    reach the model unmarked through the one tool family that is *about* to act
    on it.
    """
    return Envelope.success(
        {
            "plan_id": plan.plan_id,
            "operation": plan.operation,
            "summary": wrap(sanitize(plan.summary)),
            "state": plan.state,
            next_key: next_step,
        },
        meta=Meta(untrusted_content=True, untrusted_markers=(OPEN_MARKER, CLOSE_MARKER)),
    )


async def execute(
    op: Operation,
    raw: dict[str, Any],
    *,
    actor: Actor,
    config_path: Path | None = None,
    settings: Settings | None = None,
) -> Envelope | PlanRecord:
    """Validate and run ``op``, returning its envelope or the plan it recorded.

    Raises :class:`~telegram_ai_cli.errors.TelegramAIError` for every expected
    failure, so both surfaces render a refusal the same way.
    """
    params = op.parse(raw)
    settings = settings or load_settings(config_path)

    routed = await _via_daemon(op, params, actor=actor, settings=settings)
    if routed is not None:
        return routed
    return await _locally(op, params, actor=actor, config_path=config_path)


def run(
    op: Operation,
    raw: dict[str, Any],
    *,
    actor: Actor,
    config_path: Path | None = None,
    next_key: str,
    next_step: str = "tg-ai plan apply {plan_id}",
) -> Envelope:
    """:func:`execute` for a synchronous caller, rendered into one envelope."""
    try:
        outcome = asyncio.run(execute(op, raw, actor=actor, config_path=config_path))
    except TelegramAIError as exc:
        return Envelope.failure(exc)
    return render(outcome, next_key=next_key, next_step=next_step)


def render(
    outcome: Envelope | PlanRecord,
    *,
    next_key: str,
    next_step: str = "tg-ai plan apply {plan_id}",
) -> Envelope:
    if isinstance(outcome, Envelope):
        return outcome
    return plan_envelope(
        outcome, next_key=next_key, next_step=next_step.format(plan_id=outcome.plan_id)
    )


# --- the two ways of running it --------------------------------------------


async def _locally(
    op: Operation,
    params: BaseModel,
    *,
    actor: Actor,
    config_path: Path | None,
) -> Envelope | PlanRecord:
    with OperationContext.build(actor=actor, config_path=config_path) as ctx:
        if op.is_remote_write:
            plan = await op.planner(ctx, params)  # type: ignore[misc]
            return PlanRecord(
                plan_id=plan.plan_id,
                operation=plan.operation,
                summary=plan.summary,
                state=str(plan.state),
            )
        return await op.handler(ctx, params)  # type: ignore[misc]


def daemon_socket_for(op: Operation, params: BaseModel, settings: Settings) -> Path | None:
    """The socket this request would be routed to, if any.

    ``None`` covers every reason not to route: the feature is off, the
    operation is terminal-only, or the request did not name an account.
    """
    if not settings.daemon.enabled:
        return None
    if op.effect is Effect.LOCAL_ADMIN:
        # Prompts a person. A socket cannot be prompted, and the daemon refuses
        # these anyway; not routing them keeps the refusal local and legible.
        return None
    label = getattr(params, "account", None)
    if not isinstance(label, str) or not label:
        return None
    return daemon_paths.socket_path(settings, label)


async def _via_daemon(
    op: Operation,
    params: BaseModel,
    *,
    actor: Actor,
    settings: Settings,
) -> Envelope | PlanRecord | None:
    socket_path = daemon_socket_for(op, params, settings)
    if socket_path is None:
        return None

    try:
        body = await daemon_client.run(
            socket_path,
            operation=op.name,
            params=params.model_dump(mode="json"),
            actor=actor,
            policy=policy_fingerprint(settings),
            connect_timeout=settings.daemon.connect_timeout_seconds,
            request_timeout=settings.daemon.request_timeout_seconds,
        )
    except daemon_client.DaemonUnavailable as exc:
        # Nothing answered, so nothing ran. Doing the work here is safe.
        log.debug("no daemon for %s: %s", op.name, exc)
        return None
    except daemon_client.DaemonRefusal as exc:
        if not _is_policy_mismatch(exc):
            raise
        # The daemon refused *before* running anything, so this is still the
        # "nothing happened" case: run it here, under this caller's own policy,
        # which is the whole point of refusing.
        log.warning(
            "the daemon for %r runs a different configuration; running %s locally instead",
            getattr(params, "account", None),
            op.name,
        )
        return None

    kind = body.get("kind")
    if kind == "envelope":
        return Envelope.from_dict(body.get("envelope") or {})
    if kind == "plan":
        plan = body.get("plan") or {}
        return PlanRecord(
            plan_id=str(plan.get("plan_id")),
            operation=str(plan.get("operation")),
            summary=str(plan.get("summary", "")),
            state=str(plan.get("state", "")),
        )
    raise TelegramAIError(f"the account daemon answered with an unknown result kind {kind!r}")


def _is_policy_mismatch(exc: daemon_client.DaemonRefusal) -> bool:
    details = exc.payload.get("details")
    return bool(isinstance(details, dict) and details.get("policy_mismatch"))
