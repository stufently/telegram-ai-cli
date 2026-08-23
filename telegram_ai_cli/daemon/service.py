"""What the daemon actually runs: registered operations, nothing else.

This is the module where the security property of the whole feature lives.
:func:`select_operation` takes a *name* and looks it up in the operation
registry. A name that is not there is `UNKNOWN_OPERATION`; there is no branch
that falls through to ``getattr`` on the client, and no argument anywhere in
the protocol that could carry one. An MTProto method name and an attribute path
are both simply names the registry does not have.

The account is opened once and pinned. :class:`PinnedRegistry` is what makes a
handler written against ``open_account`` reuse the daemon's single connected
client instead of opening a second one — it hands back a plain handle rather
than a :class:`~telegram_ai_cli.accounts.spec.LoadedClient`, precisely because
that class is an async context manager whose exit disconnects the client and
releases the auth key. Sharing means *not* doing that per request.

It also narrows ``list_accounts`` to the one account. That is deliberate: a
daemon serves one account, and a fleet-wide sweep routed into it must report
one account's answer honestly rather than silently omitting the rest under a
name that promises all of them. The client side only routes a request that
names an account, so the narrowing is never a surprise.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..context import OperationContext
from ..errors import AccountNotFound, ProfileForbidden
from ..opspec import REGISTRY, Effect, Operation

log = logging.getLogger(__name__)

#: Settings that describe *how* a caller reaches this tool rather than what it
#: may do. A daemon's own idle timeout and a client's HTTP port are allowed to
#: differ; nothing else is.
_TRANSPORT_SETTINGS = frozenset({"daemon", "http"})


def policy_fingerprint(settings: Settings) -> str:
    """Identify the policy a request would run under.

    The daemon loads its configuration once, at start-up, and then runs
    operations for whoever connects. Without this, a caller launched with a
    *narrower* configuration — `TGAI_PROFILE=readonly`, a tighter allowlist, a
    different config file — would silently get the daemon's wider one, which is
    the whole trust boundary going out through the transport. So the client
    sends the fingerprint of the policy it believes it is running under, the
    daemon refuses anything that does not match its own, and the caller falls
    back to opening the account itself under the configuration it actually has.

    Deliberately conservative: any difference at all is a mismatch, rather than
    an attempt to decide which differences are narrowing and which are widening.
    Getting that judgement wrong in the widening direction is the failure this
    exists to prevent.
    """
    payload = {
        key: value
        for key, value in settings.model_dump(mode="json").items()
        if key not in _TRANSPORT_SETTINGS
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8"))
    return digest.hexdigest()[:32]


def selectable_operations() -> tuple[Operation, ...]:
    """Every operation the daemon will run, which is the answer to "what can it do"."""
    return tuple(op for op in REGISTRY.all() if op.effect is not Effect.LOCAL_ADMIN)


def select_operation(name: str) -> Operation:
    """Resolve a registry name, or refuse.

    Two refusals, both narrower than they look. An unknown name is anything the
    registry does not hold — which includes every MTProto method and every
    attribute of the Telethon client, because none of those is a registered
    operation. A ``LOCAL_ADMIN`` operation is known but still refused: enrolling
    or signing in an account prompts a person, and a socket cannot be prompted.
    """
    op = REGISTRY.by_name(name)
    if op.effect is Effect.LOCAL_ADMIN:
        raise ProfileForbidden(
            f"{op.name} is a terminal-only command and is not served over the daemon socket",
            suggestion=f"Run `tg-ai {' '.join(op.cli)}` yourself.",
        )
    return op


@dataclass(slots=True)
class SharedAccount:
    """The daemon's one connected client, handed to an operation.

    Deliberately not a context manager: ``ops/_client.open_account`` enters one
    if it finds it, and exiting a :class:`LoadedClient` disconnects the client
    and gives the auth key back — which is exactly what must not happen between
    two requests sharing it.
    """

    label: str
    client: Any
    spec: Any = None


class PinnedRegistry:
    """The real registry, except that one account is already open."""

    def __init__(self, inner: Any, shared: SharedAccount) -> None:
        self._inner = inner
        self._shared = shared

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def list_accounts(self, **kwargs: Any) -> list[Any]:
        """Only the pinned account: a daemon speaks for one."""
        del kwargs
        listed = [
            account
            for account in self._inner.list_accounts()
            if getattr(account, "label", None) == self._shared.label
        ]
        return listed

    async def load_account(self, label: str, **kwargs: Any) -> SharedAccount:
        del kwargs
        if label != self._shared.label:
            raise AccountNotFound(
                f"this daemon serves account {self._shared.label!r}, not {label!r}",
                suggestion=f"Start a daemon for {label!r}, or omit the daemon for this call.",
            )
        return self._shared


class RegistrySession:
    """Owns the context and the connected client for one account."""

    def __init__(self, *, label: str, config_path: Path | None = None) -> None:
        self.label = label
        self.config_path = config_path
        self.policy: str | None = None
        self._ctx: OperationContext | None = None
        self._loaded: Any = None

    async def open(self) -> None:
        import telegram_ai_cli.ops  # noqa: F401  (registers every operation)

        ctx = OperationContext.build(actor="cli", config_path=self.config_path)
        try:
            registry = ctx.accounts
            if registry is None:  # pragma: no cover - build always sets one
                raise AccountNotFound("no accounts are configured")
            loaded = await registry.load_account(self.label)
            shared = SharedAccount(
                label=self.label, client=loaded.client, spec=getattr(loaded, "spec", None)
            )
            ctx.accounts = PinnedRegistry(registry, shared)
        except BaseException:
            ctx.close()
            raise
        self._ctx = ctx
        self._loaded = loaded
        self.policy = policy_fingerprint(ctx.settings)

    async def close(self) -> None:
        # The database handle is closed in `finally`: a disconnect that raises
        # must not strand an open SQLite connection for the life of the process.
        try:
            if self._loaded is not None:
                await self._loaded.close()
                self._loaded = None
        finally:
            if self._ctx is not None:
                self._ctx.close()
                self._ctx = None

    async def run(
        self,
        operation: str,
        params: dict[str, Any],
        *,
        actor: str,
        policy: str | None = None,
    ) -> dict[str, Any]:
        """Run one operation against the shared client and describe the result.

        Two shapes come back, and neither is a rendered envelope for a
        particular surface. A handler's envelope travels as its dictionary; a
        planner's plan travels as its fields, and the caller builds the envelope
        — so the CLI and the MCP server keep phrasing "what to do next" their
        own way instead of the daemon choosing for both.
        """
        # Checked first, before the operation is even resolved: if the caller
        # is not running under this daemon's configuration, nothing about the
        # request should be acted on at all.
        if policy != self.policy:
            # Refused before anything runs, so the caller may safely open the
            # account itself under the configuration it actually has.
            raise ProfileForbidden(
                "this daemon was started under a different configuration than the caller's",
                suggestion=(
                    "Restart the daemon under the same configuration, or run without one. "
                    "Reusing it would run the request under the daemon's policy, which may "
                    "be wider than the caller's."
                ),
                details={"policy_mismatch": True},
            )
        if self._ctx is None:  # pragma: no cover - serve() opens before dispatching
            raise ProfileForbidden("the daemon is not open")

        op = select_operation(operation)
        parsed = op.parse(params)
        # Runs are serialised by the daemon, so the actor recorded in the audit
        # log is the one this request arrived with, never a neighbour's.
        self._ctx.actor = actor  # type: ignore[assignment]

        if op.is_remote_write:
            plan = await op.planner(self._ctx, parsed)  # type: ignore[misc]
            return {
                "kind": "plan",
                "plan": {
                    "plan_id": plan.plan_id,
                    "operation": plan.operation,
                    "summary": plan.summary,
                    "state": str(plan.state),
                },
            }

        envelope = await op.handler(self._ctx, parsed)  # type: ignore[misc]
        return {"kind": "envelope", "envelope": envelope.to_dict()}
