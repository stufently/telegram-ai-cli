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
    """Tool name for READ and LOCAL_WRITE. Must be ``None`` for REMOTE_WRITE."""

    plan_tool: str | None = None
    """Tool name that *prepares* a REMOTE_WRITE, e.g. ``telegram_plan_send_message``."""

    capability: Capability | None = None
    """Which policy this consults. ``None`` only for operations with no peer."""

    handler: Handler | None = None
    planner: Planner | None = None

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
