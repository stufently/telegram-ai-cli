"""The operation registry — one definition, three surfaces.

Every capability of this tool is declared once, here, as an :class:`Operation`.
The CLI renders it into a command, the MCP server renders it into a tool, and
``tg-ai schema`` prints its JSON Schema. None of the three holds logic of its
own. The moment one of them grows a special case, the surfaces can disagree
about what a parameter means, and the one nobody exercises is the one that
turns out to be wrong.

Input is a Pydantic model rather than a bespoke parameter type. A hand-rolled
descriptor would be a fourth type system in a project that already has three,
and it would have to reimplement validation that Pydantic performs correctly.
The model is the source of the JSON Schema, the Click options, and the
validation itself.

⚠️ The MCP low-level server *publishes* input schemas; it does not enforce
them. Tool annotations such as ``readOnlyHint`` are advisory metadata for the
client, not a boundary. Both facts mean the adapter has to call
``model_validate`` itself — see :meth:`Operation.parse`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from .config import PathsConfig
from .errors import InvalidInput, UnknownOperation
from .safety import Capability

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .context import OperationContext
    from .envelope import Envelope
    from .plans import Plan


class Effect(StrEnum):
    """What an operation changes, which decides how it may be invoked."""

    #: Reads Telegram. Runs immediately on any surface.
    READ = auto()
    #: Writes to this machine only (downloading media). Runs immediately, but
    #: never at a caller-chosen path — see the media operation for why.
    LOCAL_WRITE = auto()
    #: Administers this installation's own account inventory: registering an
    #: account, signing one in. Terminal only — never published as an MCP tool,
    #: because it prompts a person for a login code and because the ability to
    #: enrol an account is the ability to widen everything else.
    LOCAL_ADMIN = auto()
    #: Changes something on Telegram. Never runs from a tool call: it is
    #: planned, and applied by a person running the CLI.
    REMOTE_WRITE = auto()


Handler = Callable[["OperationContext", BaseModel], Awaitable["Envelope"]]
Planner = Callable[["OperationContext", BaseModel], Awaitable["Plan"]]


@dataclass(frozen=True, slots=True)
class Operation:
    name: str
    """Canonical dotted id, e.g. ``chat.read``. Stable; part of the contract."""

    cli: tuple[str, ...]
    """Path in the CLI command tree, e.g. ``("chat", "read")``."""

    summary: str
    input_model: type[BaseModel]
    effect: Effect

    description: str = ""

    mcp_tool: str | None = None
    """Tool name for READ and LOCAL_WRITE.

    Must be ``None`` for REMOTE_WRITE (planned over MCP, applied in a terminal)
    and for LOCAL_ADMIN (terminal only, never reachable from a tool call).
    """

    plan_tool: str | None = None
    """Tool name that *prepares* a REMOTE_WRITE, e.g. ``telegram_plan_send_message``."""

    capability: Capability | None = None
    """Which policy this consults. ``None`` only for operations with no peer."""

    local_path: str | None = None
    """Which ``paths.*`` setting a ``LOCAL_WRITE`` writes into, e.g. ``"downloads"``.

    Required for ``LOCAL_WRITE`` and forbidden for anything else, both checked
    in :meth:`Registry.check_invariants`. It exists because "this operation
    writes to the machine" is not enough to say **where**: ``media.fetch``
    fills ``paths.downloads`` and the archive operations write
    ``paths.archive``, and an MCP client's roots have to be checked against the
    path the operation actually uses. Deriving it from the effect alone was the
    bug that made this a field — one destination checked on behalf of three
    operations is a check that is wrong twice.
    """

    handler: Handler | None = None
    planner: Planner | None = None

    destructive: bool = False
    """Whether running this destroys something that was there before.

    Advisory metadata for MCP clients (``destructiveHint``), not a boundary —
    but a *wrong* hint is worse than none, because a client may auto-approve
    what it is told is harmless. Only true for operations that remove data,
    which today means erasing a chat from the local archive; every remote write
    is planned and approved by a person, so the hint on a plan tool describes
    the planning, which destroys nothing.
    """

    idempotent: bool | None = None
    """Whether running it twice differs from running it once.

    ``None`` means "derive it from the effect" — a read is idempotent, anything
    else is assumed not to be. Set explicitly where that guess is wrong: a
    delete is idempotent without being a read.
    """

    tags: tuple[str, ...] = field(default_factory=tuple)

    # -- derived ----------------------------------------------------------

    @property
    def is_remote_write(self) -> bool:
        return self.effect is Effect.REMOTE_WRITE

    def input_schema(self) -> dict[str, Any]:
        """JSON Schema published to MCP clients and by ``tg-ai schema``."""
        return self.input_model.model_json_schema()

    def parse(self, raw: dict[str, Any]) -> BaseModel:
        """Validate caller input, raising the shared error type.

        Called on every surface, so a malformed argument produces the same
        message and the same error code whether it arrived from a shell or
        from a tool call. That parity is asserted by the test suite; without
        it, "works in the CLI" stops implying "works over MCP".
        """
        try:
            return self.input_model.model_validate(raw)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
                for err in exc.errors()
            )
            raise InvalidInput(f"invalid arguments for {self.name}: {problems}") from exc


class Registry:
    """Every operation, plus the invariants that keep the surfaces honest."""

    def __init__(self) -> None:
        self._ops: dict[str, Operation] = {}

    def register(self, op: Operation) -> Operation:
        if op.name in self._ops:
            raise ValueError(f"duplicate operation name: {op.name}")
        self._ops[op.name] = op
        return op

    def all(self) -> tuple[Operation, ...]:
        return tuple(self._ops.values())

    def by_name(self, name: str) -> Operation:
        try:
            return self._ops[name]
        except KeyError:
            raise UnknownOperation(f"no such operation: {name}") from None

    def by_mcp_tool(self, tool: str) -> Operation:
        for op in self._ops.values():
            if tool in {op.mcp_tool, op.plan_tool} and tool is not None:
                return op
        raise UnknownOperation(f"no such tool: {tool}")

    def by_cli(self, path: tuple[str, ...]) -> Operation:
        for op in self._ops.values():
            if op.cli == path:
                return op
        raise UnknownOperation(f"no such command: {' '.join(path)}")

    # -- surfaces ----------------------------------------------------------

    def read_tools(self) -> tuple[Operation, ...]:
        return tuple(op for op in self._ops.values() if op.mcp_tool)

    def plan_tools(self) -> tuple[Operation, ...]:
        return tuple(op for op in self._ops.values() if op.plan_tool)

    def mcp_tool_names(self) -> tuple[str, ...]:
        names = [op.mcp_tool for op in self._ops.values() if op.mcp_tool]
        names += [op.plan_tool for op in self._ops.values() if op.plan_tool]
        return tuple(sorted(n for n in names if n))

    # -- invariants --------------------------------------------------------

    def check_invariants(self) -> None:
        """Run at import and asserted by the test suite.

        These are the properties that a reviewer would otherwise have to
        re-derive by reading every operation. Checking them at startup means a
        mistake shows up the first time anything runs, not the first time
        somebody exercises the one path that breaks.
        """
        seen_cli: set[tuple[str, ...]] = set()
        seen_tools: set[str] = set()

        for op in self._ops.values():
            if op.cli in seen_cli:
                raise ValueError(f"duplicate CLI path: {' '.join(op.cli)}")
            seen_cli.add(op.cli)

            for tool in (op.mcp_tool, op.plan_tool):
                if tool is None:
                    continue
                if tool in seen_tools:
                    raise ValueError(f"duplicate MCP tool name: {tool}")
                seen_tools.add(tool)

            if bool(op.handler) == bool(op.planner):
                raise ValueError(
                    f"{op.name}: exactly one of handler/planner must be set "
                    "(a read that also plans, or an operation that does neither, "
                    "is a definition error)"
                )

            if op.effect is Effect.LOCAL_WRITE:
                if op.local_path is None:
                    raise ValueError(
                        f"{op.name}: a local write must name the paths.* setting it writes "
                        "into. It is what an MCP client's advertised roots are checked "
                        "against, and an operation that names nothing would be checked "
                        "against some other operation's directory."
                    )
                if op.local_path not in PathsConfig.model_fields:
                    raise ValueError(f"{op.name}: paths.{op.local_path} is not a configured path")
            elif op.local_path is not None:
                raise ValueError(
                    f"{op.name}: only a local write may name a paths.* setting; nothing "
                    "else writes to this machine"
                )

            if op.is_remote_write:
                if op.planner is None:
                    raise ValueError(f"{op.name}: remote writes need a planner")
                if op.mcp_tool is not None:
                    raise ValueError(
                        f"{op.name}: a remote write must not have a direct MCP tool. "
                        "Writes are planned over MCP and applied from the terminal."
                    )
                if op.plan_tool is None:
                    raise ValueError(f"{op.name}: remote writes need a plan tool")
            else:
                if op.handler is None:
                    raise ValueError(f"{op.name}: reads need a handler")
                if op.plan_tool is not None:
                    raise ValueError(f"{op.name}: only remote writes may have a plan tool")
                if op.effect is Effect.LOCAL_ADMIN and op.mcp_tool is not None:
                    raise ValueError(
                        f"{op.name}: account administration must not be published as an MCP "
                        "tool. Enrolling or signing in an account is done by a person at a "
                        "terminal; a tool for it would let a caller widen the fleet the rest "
                        "of the policy is written against."
                    )

        # The single most important property in the project, checked as a
        # property rather than trusted as a convention: no tool applies a plan.
        for tool in seen_tools:
            if "apply" in tool.lower():
                raise ValueError(
                    f"{tool}: no MCP tool may apply a plan. Applying is a terminal-only "
                    "command; exposing it as a tool would defeat the approval design."
                )


#: The process-wide registry. Operation modules register into it on import.
REGISTRY = Registry()
