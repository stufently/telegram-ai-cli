"""The command line, generated from the operation registry.

Click rather than Typer, deliberately. Typer derives a command from a
function's static signature, which is exactly what this file does not have:
commands are built at runtime from :class:`~telegram_ai_cli.opspec.Operation`
objects. Click exposes ``Command`` and ``Option`` as ordinary values, so the
registry can be turned into a command tree without fighting the framework.

The CLI holds one power the MCP server does not: ``tg-ai plan apply``. That
asymmetry is the approval design, and it is enforced in :mod:`opspec` as an
invariant rather than left as a convention here.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, get_args, get_origin

import click
from pydantic import BaseModel

from . import __version__
from .context import OperationContext
from .envelope import Envelope
from .errors import TelegramAIError
from .opspec import REGISTRY, Effect, Operation
from .plans import PlanState
from .render import sanitize, sanitize_line

# --------------------------------------------------------------------------
# Turning a Pydantic model into Click options
# --------------------------------------------------------------------------

_SIMPLE_TYPES: dict[Any, Any] = {str: str, int: int, float: float, bool: bool}


def _click_type(annotation: Any) -> Any:
    """Best-effort mapping. Anything exotic arrives as text and is validated
    by Pydantic afterwards, which is the component that actually knows the
    rules — Click only has to get the value off the command line."""
    if annotation in _SIMPLE_TYPES:
        return _SIMPLE_TYPES[annotation]
    origin = get_origin(annotation)
    if origin is not None:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _click_type(args[0])
    return str


def _options_for(model: type[BaseModel]) -> list[click.Option]:
    options: list[click.Option] = []
    for name, field in model.model_fields.items():
        flag = "--" + name.replace("_", "-")
        annotation = field.annotation
        is_bool = annotation is bool
        required = field.is_required()
        options.append(
            click.Option(
                [f"{flag}/--no-" + name.replace("_", "-")] if is_bool else [flag],
                required=required,
                default=None if required else field.default,
                help=field.description or "",
                type=None if is_bool else _click_type(annotation),
            )
        )
    return options


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def _emit(envelope: Envelope, *, as_json: bool) -> None:
    """Render a result. Everything printed has already passed through the
    sanitizer, because message text reaching a terminal unfiltered can redraw
    the very lines a person is about to act on."""
    if as_json:
        click.echo(json.dumps(envelope.to_dict(), ensure_ascii=False, indent=2, default=str))
        return

    if not envelope.ok and envelope.error:
        click.secho(
            f"error [{envelope.error['code']}] {envelope.error['message']}",
            fg="red",
            err=True,
        )
        if suggestion := envelope.error.get("suggestion"):
            click.secho(f"  {suggestion}", fg="yellow", err=True)
        return

    for warning in envelope.warnings:
        click.secho(f"warning: {sanitize_line(warning)}", fg="yellow", err=True)

    data = envelope.data
    if isinstance(data, str):
        click.echo(sanitize(data))
    else:
        click.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))

    if envelope.meta.truncated:
        click.secho(
            f"note: output truncated ({envelope.meta.truncated_reason}); "
            "narrow the query or raise the limit",
            fg="yellow",
            err=True,
        )


def _run_operation(op: Operation, ctx_obj: dict[str, Any], **kwargs: Any) -> None:
    as_json = ctx_obj["json"]
    # Click hands back None for every option the user did not pass; forwarding
    # those would override the model's own defaults with null.
    raw = {k: v for k, v in kwargs.items() if v is not None}

    try:
        params = op.parse(raw)
        with OperationContext.build(actor="cli", config_path=ctx_obj["config"]) as ctx:
            if op.effect is Effect.REMOTE_WRITE:
                plan = asyncio.run(op.planner(ctx, params))  # type: ignore[misc]
                envelope = Envelope.success(
                    {
                        "plan_id": plan.plan_id,
                        "operation": plan.operation,
                        "summary": sanitize(plan.summary),
                        "state": str(plan.state),
                        "next": f"tg-ai plan apply {plan.plan_id}",
                    }
                )
            else:
                envelope = asyncio.run(op.handler(ctx, params))  # type: ignore[misc]
    except TelegramAIError as exc:
        envelope = Envelope.failure(exc)

    _emit(envelope, as_json=as_json)
    if not envelope.ok:
        raise SystemExit(envelope.exit_code)


def _build_command(op: Operation) -> click.Command:
    def callback(**kwargs: Any) -> None:
        ctx_obj = click.get_current_context().find_root().obj
        _run_operation(op, ctx_obj, **kwargs)

    help_text = op.description or op.summary
    if op.effect is Effect.REMOTE_WRITE:
        help_text += (
            "\n\nPrepares a plan and prints its id. Nothing is sent until you run "
            "`tg-ai plan apply <id>`."
        )
    elif op.effect is Effect.LOCAL_ADMIN:
        help_text += (
            "\n\nA terminal-only command: it may prompt, and it is deliberately absent "
            "from the MCP tool surface."
        )
    return click.Command(
        name=op.cli[-1],
        callback=callback,
        params=_options_for(op.input_model),
        help=help_text,
        short_help=op.summary,
    )


def _attach(root: click.Group) -> None:
    """Hang every registered operation off the root group."""
    groups: dict[tuple[str, ...], click.Group] = {}

    for op in sorted(REGISTRY.all(), key=lambda o: o.cli):
        parent: click.Group = root
        for depth in range(len(op.cli) - 1):
            path = op.cli[: depth + 1]
            if path not in groups:
                group = click.Group(name=path[-1], help=f"{path[-1]} commands")
                parent.add_command(group)
                groups[path] = group
            parent = groups[path]
        parent.add_command(_build_command(op))


# --------------------------------------------------------------------------
# Root
# --------------------------------------------------------------------------


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="tg-ai")
@click.option(
    "--config",
    "-c",
    type=click.Path(path_type=Path),
    envvar="TGAI_CONFIG",
    help="Path to tgai.yaml.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the raw JSON envelope.")
@click.pass_context
def cli(ctx: click.Context, config: Path | None, as_json: bool) -> None:
    """AI-first CLI and MCP server for Telegram user accounts over MTProto."""
    ctx.obj = {"config": config, "json": as_json}


@cli.group("plan")
def plan_group() -> None:
    """Review and apply prepared writes."""


@plan_group.command("list")
@click.option("--state", type=click.Choice([str(s) for s in PlanState]), default=None)
@click.pass_context
def plan_list(ctx: click.Context, state: str | None) -> None:
    """Show plans awaiting a decision."""
    with OperationContext.build(actor="cli", config_path=ctx.obj["config"]) as octx:
        octx.plans.expire_stale()
        plans = octx.plans.list(state=PlanState(state) if state else PlanState.PENDING)
        rows = [
            {
                "plan_id": p.plan_id,
                "operation": p.operation,
                "account": p.account,
                "state": str(p.state),
                "summary": sanitize_line(p.summary, limit=90),
            }
            for p in plans
        ]
    _emit(Envelope.success(rows), as_json=ctx.obj["json"])


@plan_group.command("show")
@click.argument("plan_id")
@click.pass_context
def plan_show(ctx: click.Context, plan_id: str) -> None:
    """Print exactly what applying this plan would do.

    This is the screen the whole approval design rests on, so every field is
    sanitized before it is printed.
    """
    try:
        with OperationContext.build(actor="cli", config_path=ctx.obj["config"]) as octx:
            plan = octx.plans.get(plan_id)
            payload = {
                "plan_id": plan.plan_id,
                "operation": plan.operation,
                "account": plan.account,
                "state": str(plan.state),
                "summary": sanitize(plan.summary),
                "params": {k: sanitize(str(v)) for k, v in plan.params.items()},
                "preconditions": plan.preconditions,
            }
    except TelegramAIError as exc:
        _emit(Envelope.failure(exc), as_json=ctx.obj["json"])
        raise SystemExit(1) from None
    _emit(Envelope.success(payload), as_json=ctx.obj["json"])


@plan_group.command("apply")
@click.argument("plan_id")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def plan_apply(ctx: click.Context, plan_id: str, yes: bool) -> None:
    """Carry out a plan. The only path to sending anything.

    Deliberately absent from the MCP surface: a confirmation an agent can send
    is a confirmation a prompt injection can send.
    """
    from .apply import apply_plan

    try:
        with OperationContext.build(actor="cli", config_path=ctx.obj["config"]) as octx:
            plan = octx.plans.get(plan_id)
            if not yes:
                click.echo(sanitize(plan.summary))
                click.confirm("Apply this plan?", abort=True)
            envelope = asyncio.run(apply_plan(octx, plan_id))
    except TelegramAIError as exc:
        envelope = Envelope.failure(exc)

    _emit(envelope, as_json=ctx.obj["json"])
    if not envelope.ok:
        raise SystemExit(envelope.exit_code)


@plan_group.command("reject")
@click.argument("plan_id")
@click.pass_context
def plan_reject(ctx: click.Context, plan_id: str) -> None:
    """Decline a pending plan."""
    try:
        with OperationContext.build(actor="cli", config_path=ctx.obj["config"]) as octx:
            octx.plans.reject(plan_id)
    except TelegramAIError as exc:
        _emit(Envelope.failure(exc), as_json=ctx.obj["json"])
        raise SystemExit(1) from None
    _emit(Envelope.success({"plan_id": plan_id, "state": "rejected"}), as_json=ctx.obj["json"])


@cli.command("schema")
@click.argument("operation", required=False)
def schema(operation: str | None) -> None:
    """Print the JSON Schema published for an operation, or for all of them."""
    if operation:
        op = REGISTRY.by_name(operation)
        click.echo(json.dumps(op.input_schema(), ensure_ascii=False, indent=2))
        return
    payload = {
        op.name: {
            "cli": " ".join(op.cli),
            "mcp_tool": op.mcp_tool,
            "plan_tool": op.plan_tool,
            "effect": str(op.effect),
            "input_schema": op.input_schema(),
        }
        for op in REGISTRY.all()
    }
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@cli.command("mcp")
@click.pass_context
def mcp(ctx: click.Context) -> None:
    """Serve the MCP protocol over stdio."""
    from .mcp_server import serve

    asyncio.run(serve(config_path=ctx.obj["config"]))


def main() -> None:
    import telegram_ai_cli.ops  # noqa: F401  (registers every operation)

    _attach(cli)
    try:
        cli(standalone_mode=True)
    except TelegramAIError as exc:  # pragma: no cover - top-level safety net
        click.secho(f"error [{exc.code}] {exc.message}", fg="red", err=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
