"""The operation registry and the invariants that keep the surfaces honest.

One definition feeds the CLI, the MCP server and ``tg-ai schema``. The startup
invariants are what stop the three from drifting apart, and one of them is the
single most important property in the project: **no MCP tool applies a plan**.
It is checked here as a property rather than trusted as a convention, because a
convention is what a plausible-looking pull request walks straight through.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from telegram_ai_cli.errors import ErrorCode, InvalidInput, UnknownOperation
from telegram_ai_cli.opspec import REGISTRY, Effect, Operation, Registry
from telegram_ai_cli.safety import Capability


class ReadInput(BaseModel):
    chat: int
    limit: int = Field(default=20, ge=1, le=500)


class SendInput(BaseModel):
    chat: int
    text: str


async def handler(ctx, params):  # pragma: no cover - never invoked here
    raise AssertionError("the registry must not run handlers")


async def planner(ctx, params):  # pragma: no cover - never invoked here
    raise AssertionError("the registry must not run planners")


def read_op(**overrides) -> Operation:
    defaults = {
        "name": "chat.read",
        "cli": ("chat", "read"),
        "summary": "Read messages from a chat",
        "input_model": ReadInput,
        "effect": Effect.READ,
        "mcp_tool": "telegram_read_chat",
        "capability": Capability.READ_CHAT,
        "handler": handler,
    }
    return Operation(**{**defaults, **overrides})


def write_op(**overrides) -> Operation:
    defaults = {
        "name": "message.send",
        "cli": ("message", "send"),
        "summary": "Plan a message",
        "input_model": SendInput,
        "effect": Effect.REMOTE_WRITE,
        "plan_tool": "telegram_plan_send_message",
        "capability": Capability.SEND,
        "planner": planner,
    }
    return Operation(**{**defaults, **overrides})


class LabelInput(BaseModel):
    label: str


def admin_op(**overrides) -> Operation:
    defaults = {
        "name": "account.login",
        "cli": ("account", "login"),
        "summary": "Sign an account in",
        "input_model": LabelInput,
        "effect": Effect.LOCAL_ADMIN,
        "capability": None,
        "handler": handler,
    }
    return Operation(**{**defaults, **overrides})


def registry_of(*ops: Operation) -> Registry:
    registry = Registry()
    for op in ops:
        registry.register(op)
    return registry


# --- lookup ----------------------------------------------------------------


def test_a_registered_operation_is_findable_on_every_surface() -> None:
    registry = registry_of(read_op(), write_op())

    assert registry.by_name("chat.read").name == "chat.read"
    assert registry.by_cli(("message", "send")).name == "message.send"
    assert registry.by_mcp_tool("telegram_read_chat").name == "chat.read"
    assert registry.by_mcp_tool("telegram_plan_send_message").name == "message.send"


@pytest.mark.parametrize(
    "lookup",
    [
        lambda r: r.by_name("nope"),
        lambda r: r.by_cli(("nope",)),
        lambda r: r.by_mcp_tool("telegram_nope"),
    ],
)
def test_unknown_lookups_raise_the_shared_error(lookup) -> None:
    registry = registry_of(read_op())
    with pytest.raises(UnknownOperation) as failure:
        lookup(registry)
    assert failure.value.code is ErrorCode.UNKNOWN_OPERATION


def test_the_surfaces_are_separate_lists() -> None:
    registry = registry_of(read_op(), write_op())

    assert [op.name for op in registry.read_tools()] == ["chat.read"]
    assert [op.name for op in registry.plan_tools()] == ["message.send"]
    assert registry.mcp_tool_names() == ("telegram_plan_send_message", "telegram_read_chat")
    assert len(registry.all()) == 2


def test_a_duplicate_name_is_refused_at_registration() -> None:
    registry = registry_of(read_op())
    with pytest.raises(ValueError, match="duplicate operation name"):
        registry.register(read_op(cli=("other",), mcp_tool="telegram_other"))


# --- invariants ------------------------------------------------------------


def test_a_well_formed_registry_passes() -> None:
    assert registry_of(read_op(), write_op()).check_invariants() is None


def test_duplicate_cli_paths_are_caught() -> None:
    registry = registry_of(
        read_op(),
        read_op(name="chat.read2", mcp_tool="telegram_read_chat2"),
    )
    with pytest.raises(ValueError, match="duplicate CLI path"):
        registry.check_invariants()


def test_duplicate_tool_names_are_caught() -> None:
    registry = registry_of(
        read_op(),
        read_op(name="chat.read2", cli=("chat", "read2")),
    )
    with pytest.raises(ValueError, match="duplicate MCP tool name"):
        registry.check_invariants()


def test_a_plan_tool_may_not_collide_with_a_read_tool() -> None:
    registry = registry_of(
        read_op(mcp_tool="telegram_plan_send_message"),
        write_op(),
    )
    with pytest.raises(ValueError, match="duplicate MCP tool name"):
        registry.check_invariants()


def test_an_operation_needs_exactly_one_of_handler_and_planner() -> None:
    both = registry_of(read_op(planner=planner))
    with pytest.raises(ValueError, match="exactly one of handler/planner"):
        both.check_invariants()

    neither = registry_of(read_op(handler=None))
    with pytest.raises(ValueError, match="exactly one of handler/planner"):
        neither.check_invariants()


def test_a_remote_write_must_be_planned_not_handled() -> None:
    registry = registry_of(write_op(planner=None, handler=handler))
    with pytest.raises(ValueError, match="remote write"):
        registry.check_invariants()


def test_a_remote_write_must_not_have_a_direct_tool() -> None:
    """This is the boundary: writes are planned over MCP, applied in a terminal."""
    registry = registry_of(write_op(mcp_tool="telegram_send_message"))
    with pytest.raises(ValueError, match="must not have a direct MCP tool"):
        registry.check_invariants()


def test_a_remote_write_must_expose_a_plan_tool() -> None:
    registry = registry_of(write_op(plan_tool=None))
    with pytest.raises(ValueError, match="plan tool"):
        registry.check_invariants()


def test_a_read_may_not_have_a_plan_tool() -> None:
    registry = registry_of(read_op(plan_tool="telegram_plan_read"))
    with pytest.raises(ValueError, match="only remote writes may have a plan tool"):
        registry.check_invariants()


@pytest.mark.parametrize(
    "tool_name",
    [
        "telegram_apply_plan",
        "telegram_plan_apply",
        "telegram_Apply_Plan",
        "plan_apply_message",
    ],
)
def test_no_tool_may_apply_a_plan(tool_name: str) -> None:
    """The single most important property in the project.

    Exposing "apply" as a tool would mean the confirmation and the request come
    down the same channel, which is no confirmation at all.
    """
    registry = registry_of(write_op(plan_tool=tool_name))
    with pytest.raises(ValueError, match="no MCP tool may apply a plan"):
        registry.check_invariants()


def test_the_ban_on_apply_covers_read_tools_too() -> None:
    registry = registry_of(read_op(mcp_tool="telegram_apply_something"))
    with pytest.raises(ValueError, match="no MCP tool may apply a plan"):
        registry.check_invariants()


def test_account_administration_may_not_be_published_as_a_tool() -> None:
    """Enrolling or signing in an account is a terminal action, not a tool call.

    It prompts a human for the code Telegram just sent, and it widens the very
    fleet the rest of the policy is written against — a caller able to add an
    account can add one whose chats no allowlist was ever written for.
    """
    registry = registry_of(admin_op(mcp_tool="telegram_account_login"))
    with pytest.raises(ValueError, match="must not be published as an MCP tool"):
        registry.check_invariants()


def test_account_administration_is_well_formed_without_any_tool() -> None:
    registry = registry_of(admin_op())
    assert registry.check_invariants() is None
    assert registry.mcp_tool_names() == ()


def test_the_process_wide_registry_is_consistent() -> None:
    """Whatever the operation modules have registered by import time."""
    assert REGISTRY.check_invariants() is None


# --- parsing ---------------------------------------------------------------


def test_parse_returns_a_validated_model_with_defaults() -> None:
    parsed = read_op().parse({"chat": -4242})
    assert parsed.model_dump() == {"chat": -4242, "limit": 20}


def test_a_missing_argument_is_reported_by_name() -> None:
    with pytest.raises(InvalidInput) as failure:
        read_op().parse({})
    assert failure.value.code is ErrorCode.INVALID_INPUT
    assert "chat.read" in failure.value.message
    assert "chat" in failure.value.message


def test_a_wrongly_typed_argument_is_reported_by_name() -> None:
    with pytest.raises(InvalidInput) as failure:
        read_op().parse({"chat": "not-a-number"})
    assert "chat" in failure.value.message


def test_a_bound_violation_is_reported() -> None:
    with pytest.raises(InvalidInput, match="limit"):
        read_op().parse({"chat": 1, "limit": 10_000})


def test_several_problems_are_reported_together() -> None:
    with pytest.raises(InvalidInput) as failure:
        read_op().parse({"limit": "x"})
    assert "chat" in failure.value.message
    assert "limit" in failure.value.message


def test_the_published_schema_describes_the_input() -> None:
    schema = read_op().input_schema()
    assert sorted(schema["properties"]) == ["chat", "limit"]
    assert schema["required"] == ["chat"]
    # Limits are published so a caller can page instead of guessing.
    assert schema["properties"]["limit"]["maximum"] == 500


def test_is_remote_write_matches_the_effect() -> None:
    assert write_op().is_remote_write is True
    assert read_op().is_remote_write is False
    assert read_op(effect=Effect.LOCAL_WRITE).is_remote_write is False


def test_no_cli_path_is_a_prefix_of_another() -> None:
    """A command whose name another command extends stops being a command.

    Click cannot have `tg-ai folders` be both a command and the group holding
    `tg-ai folders add`: declaring the second silently replaces the first, and
    the listing that README documents answers "Usage: … COMMAND" instead. The
    registry's duplicate-path check does not see this — the two tuples differ —
    which is how it reached a real account before anyone noticed. Hence the
    project's convention: `chats` lists, `chat …` acts.
    """
    paths = {op.cli for op in REGISTRY.all()}
    swallowed = sorted(
        " ".join(path)
        for path in paths
        if any(other[: len(path)] == path and len(other) > len(path) for other in paths)
    )

    assert not swallowed, f"CLI commands shadowed by a group of the same name: {swallowed}"


def test_every_operation_module_is_wired_into_the_package() -> None:
    """A module the package never imports has no command and no tool.

    ``ops/__init__`` lists its modules by hand, on purpose, and that list is the
    only thing that registers them. Nothing else notices when a module is left
    out: its own tests import it directly, which both exercises the handler and
    registers the operation as a side effect, so the suite stays green while the
    command does not exist. ``ops/topics.py`` shipped that way — complete, tested
    and unreachable, with ``tg-ai chat topics`` answering "No such command" and
    the README documenting it anyway. Hence a check against the directory rather
    than against anything a test has already imported.
    """
    import ast
    from pathlib import Path

    import telegram_ai_cli.ops as ops_package

    directory = Path(ops_package.__file__).parent
    on_disk = {
        path.stem
        for path in directory.glob("*.py")
        if not path.stem.startswith("_")  # private helpers carry no operations
    }

    # Read the import statement itself rather than `__all__` or `sys.modules`.
    # `__all__` is documentation and can list a module the import block does not
    # actually pull in; `sys.modules` is worse, because any other test that
    # imported the module directly has already put it there — which is the very
    # blindness this check exists to remove.
    source = ast.parse((directory / "__init__.py").read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(source)
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None
        for alias in node.names
    }

    assert on_disk <= imported, (
        f"operation modules not imported by ops/__init__: {sorted(on_disk - imported)}"
    )
    assert on_disk == set(ops_package.__all__) - {"REGISTRY"}, (
        "ops.__all__ has drifted from the modules on disk: "
        f"{sorted(set(ops_package.__all__) - {'REGISTRY'} ^ on_disk)}"
    )
