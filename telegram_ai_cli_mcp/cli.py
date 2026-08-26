"""The command line, generated from the operation registry.

Click rather than Typer, deliberately. Typer derives a command from a
function's static signature, which is exactly what this file does not have:
commands are built at runtime from :class:`~telegram_ai_cli_mcp.opspec.Operation`
objects. Click exposes ``Command`` and ``Option`` as ordinary values, so the
registry can be turned into a command tree without fighting the framework.

The CLI holds one power the MCP server does not: ``tg-ai plan apply``. That
asymmetry is the approval design, and it is enforced in :mod:`opspec` as an
invariant rather than left as a convention here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, get_args, get_origin

import click
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from . import __version__, dispatch
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


class _IntOrText(click.ParamType):
    """A peer: an id, an ``@username`` or a ``t.me`` link.

    ``--chat 4242`` has to reach Telethon as the *integer* 4242. Handed over as
    text it is not an id at all: ``parse_phone`` accepts any run of digits, so
    the string takes the phone branch of ``_get_entity_from_string``, which
    walks the account's contact list looking for that number. Usually that ends
    in "cannot find any entity" — and occasionally, if the digits do match a
    contact, in a write addressed to the wrong person.

    The read path already draws this line, in `ops/chats.py:resolve_chat_ref`;
    every `int | str` field in the registry belongs to a *write*, whose
    `resolve_peer` hands the value straight to Telethon. Same rule, applied one
    step earlier so both surfaces agree. A username cannot be all digits and a
    phone number keeps its ``+``, so nothing else is caught by it.
    """

    name = "id|name"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        digits = text[1:] if text.startswith("-") else text
        return int(text) if digits.isdigit() else value


_INT_OR_TEXT = _IntOrText()


def _click_type(annotation: Any) -> Any:
    """Best-effort mapping. Anything exotic arrives as text and is validated
    by Pydantic afterwards, which is the component that actually knows the
    rules — Click only has to get the value off the command line."""
    if annotation in _SIMPLE_TYPES:
        return _SIMPLE_TYPES[annotation]
    if getattr(annotation, "__metadata__", None) is not None:  # Annotated[T, ...]
        # The constraints are Pydantic's to enforce; what Click needs is `T`,
        # which is the difference between `--message-ids 5` arriving as an int
        # and arriving as the string every Annotated field used to become.
        return _click_type(annotation.__origin__)
    origin = get_origin(annotation)
    if origin is not None:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _click_type(args[0])
        if set(args) == {int, str}:
            return _INT_OR_TEXT
    return str


def _without_none(annotation: Any) -> Any:
    """``X | None`` → ``X``; everything else unchanged.

    Only the optionality is stripped. ``list[int]`` keeps its list, which the
    old single-argument unwrap did not — that is how a repeatable field came
    out of the generator looking like a scalar.
    """
    args = get_args(annotation)
    if type(None) in args and len(rest := [a for a in args if a is not type(None)]) == 1:
        return rest[0]
    return annotation


class _JsonObject(click.ParamType):
    """A nested model, typed as JSON on the command line.

    Click's job stops at "get a mapping off the argv"; which keys are allowed
    and what they mean is Pydantic's, and routing the field errors through it
    keeps the terminal and the MCP tool refusing the same input for the same
    reason. What Click does add is the key list in ``--help``: an object
    argument whose fields are only documented elsewhere is an argument nobody
    can type.
    """

    name = "json"

    def __init__(self, model: type[BaseModel]) -> None:
        self.model = model

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        if isinstance(value, dict):  # a default, already in the right shape
            return value
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            self.fail(
                f"not valid JSON ({exc.msg}); expected an object like {self.example()}",
                param,
                ctx,
            )
        if not isinstance(parsed, dict):
            self.fail(f"expected a JSON object like {self.example()}", param, ctx)
        return parsed

    def example(self) -> str:
        first = next(iter(self.model.model_fields), "key")
        return json.dumps({first: True})

    def keys(self) -> str:
        return ", ".join(self.model.model_fields)


def _options_for(model: type[BaseModel]) -> list[click.Option]:
    options: list[click.Option] = []
    for name, field in model.model_fields.items():
        flag = "--" + name.replace("_", "-")
        annotation = field.annotation
        is_bool = annotation is bool
        required = field.is_required()
        inner = _without_none(annotation)
        help_text = field.description or ""

        multiple = get_origin(inner) in (list, set, tuple)
        param_type: Any
        if multiple:
            param_type = _click_type(get_args(inner)[0])
            help_text = (help_text + " Repeat the flag for each value.").strip()
        elif isinstance(inner, type) and issubclass(inner, BaseModel):
            json_type = _JsonObject(inner)
            param_type = json_type
            help_text = (
                f"{help_text} JSON object; keys: {json_type.keys()}. "
                f"Example: --{name.replace('_', '-')} '{json_type.example()}'"
            ).strip()
        else:
            param_type = None if is_bool else _click_type(annotation)

        # A field with a `default_factory` reports `PydanticUndefined` here,
        # and forwarding that sentinel as a value made the model reject its own
        # default. None is dropped before the model is built, which is the same
        # thing the factory would have produced.
        default = field.default
        if required or default is PydanticUndefined:
            default = None

        options.append(
            click.Option(
                [f"{flag}/--no-" + name.replace("_", "-")] if is_bool else [flag],
                required=required,
                default=default,
                multiple=multiple,
                help=help_text,
                type=param_type,
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
        # Sanitized here rather than trusted to have been sanitized wherever it
        # was raised. A refusal is the one line printed while somebody is deciding
        # what to do next, and an error that quotes a value — a path, a filename,
        # a name a stranger chose — is exactly where an escape sequence would
        # redraw it.
        click.secho(
            f"error [{envelope.error['code']}] {sanitize(str(envelope.error['message']))}",
            fg="red",
            err=True,
        )
        if suggestion := envelope.error.get("suggestion"):
            click.secho(f"  {sanitize(str(suggestion))}", fg="yellow", err=True)
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
    # those would override the model's own defaults with null. A repeatable
    # option is the same case wearing a different shape — an empty tuple, not
    # None — and forwarding *that* is what would turn "omit message_ids and let
    # the link name the message" into "delete an empty list of messages".
    raw: dict[str, Any] = {}
    for key, value in kwargs.items():
        if isinstance(value, tuple):  # only a repeatable option produces one
            if value:
                raw[key] = list(value)
        elif value is not None:
            raw[key] = value

    # Shared with the MCP server: parsing, the context, and the choice between
    # running here and running on the account's daemon all live in `dispatch`,
    # so the two surfaces cannot drift apart about any of them.
    envelope = dispatch.run(op, raw, actor="cli", config_path=ctx_obj["config"], next_key="next")

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
@click.option(
    "--http",
    "over_http",
    is_flag=True,
    help="Serve Streamable HTTP on a loopback address instead of stdio.",
)
@click.option("--host", default=None, help="Loopback address to bind (default: http.host).")
@click.option("--port", type=int, default=None, help="Port to bind (default: http.port).")
@click.pass_context
def mcp(ctx: click.Context, over_http: bool, host: str | None, port: int | None) -> None:
    """Serve the MCP protocol over stdio, or over loopback HTTP with --http.

    The HTTP transport refuses to start on anything but a loopback address and
    refuses to start without a bearer token in `$TGAI_HTTP_TOKEN`. Neither is a
    default that can be turned off.
    """
    if not over_http:
        if host is not None or port is not None:
            raise click.UsageError("--host and --port apply to --http only.")
        from .mcp_server import serve

        asyncio.run(serve(config_path=ctx.obj["config"]))
        return

    from .http_server import serve_http

    try:
        asyncio.run(serve_http(config_path=ctx.obj["config"], host=host, port=port))
    except TelegramAIError as exc:
        _emit(Envelope.failure(exc), as_json=ctx.obj["json"])
        raise SystemExit(1) from None


@cli.group("daemon")
def daemon_group() -> None:
    """Share one account's connection between several local callers."""


@daemon_group.command("serve")
@click.option("--account", required=True, help="Which account this daemon owns.")
@click.option(
    "--idle-timeout",
    type=float,
    default=None,
    help="Seconds of inactivity before it stops (default: daemon.idle_timeout_seconds).",
)
@click.pass_context
def daemon_serve(ctx: click.Context, account: str, idle_timeout: float | None) -> None:
    """Open one account and answer local callers over a Unix socket.

    Runs in the foreground and holds the account's auth key for as long as it
    lives, so stopping it (Ctrl-C, SIGTERM, or the idle timeout) is what gives
    the account back. Nothing supervises it and nothing starts it on demand: a
    client that finds no socket opens the account itself.
    """
    from .config import load_settings
    from .daemon import paths as daemon_paths
    from .daemon.server import AccountDaemon
    from .daemon.service import RegistrySession

    settings = load_settings(ctx.obj["config"])
    if not settings.daemon.enabled:
        # Serving without `daemon.enabled` is worse than pointless: this process
        # holds the auth key, and no client routes to it, so every other command
        # gets SESSION_LOCKED instead of the shared connection the daemon exists
        # to provide. Warned rather than refused — the setting is legitimately
        # supplied per call through TGAI_DAEMON__ENABLED.
        logging.getLogger(__name__).warning(
            "daemon.enabled is off, so no client will route to this daemon: it "
            "will hold %s's session and other commands will fail with "
            "SESSION_LOCKED. Set daemon.enabled (or TGAI_DAEMON__ENABLED=true) "
            "for the callers too.",
            # Sanitised: the label reaches the terminal before anything has
            # normalised it, and control characters in a log line can forge the
            # rest of the warning.
            sanitize_line(account),
        )
    try:
        daemon_paths.prepare_account_dir(settings, account)
        daemon = AccountDaemon(
            account=account,
            socket_path=daemon_paths.socket_path(settings, account),
            bootstrap_lock_path=daemon_paths.bootstrap_lock_path(settings, account),
            session=RegistrySession(label=account, config_path=ctx.obj["config"]),
            idle_timeout=(
                idle_timeout if idle_timeout is not None else settings.daemon.idle_timeout_seconds
            ),
            max_connections=settings.daemon.max_connections,
        )
        outcome = asyncio.run(daemon.serve())
    except TelegramAIError as exc:
        _emit(Envelope.failure(exc), as_json=ctx.obj["json"])
        raise SystemExit(1) from None
    _emit(Envelope.success({"account": account, "outcome": outcome}), as_json=ctx.obj["json"])


@daemon_group.command("status")
@click.option("--account", required=True, help="Which account to ask about.")
@click.pass_context
def daemon_status(ctx: click.Context, account: str) -> None:
    """Say whether a daemon is answering for this account."""
    from .config import load_settings
    from .daemon import client as daemon_client
    from .daemon import paths as daemon_paths

    settings = load_settings(ctx.obj["config"])
    path = daemon_paths.socket_path(settings, account)
    try:
        reply = asyncio.run(daemon_client.ping(path))
    except daemon_client.DaemonUnavailable:
        payload: dict[str, Any] = {"account": account, "running": False, "socket": str(path)}
    except TelegramAIError as exc:
        _emit(Envelope.failure(exc), as_json=ctx.obj["json"])
        raise SystemExit(1) from None
    else:
        payload = {
            "account": account,
            "running": True,
            "socket": str(path),
            "pid": reply.get("pid"),
            "idle_timeout": reply.get("idle_timeout"),
        }
    _emit(Envelope.success(payload), as_json=ctx.obj["json"])


def main() -> None:
    import telegram_ai_cli_mcp.ops  # noqa: F401  (registers every operation)

    _attach(cli)
    try:
        cli(standalone_mode=True)
    except TelegramAIError as exc:  # pragma: no cover - top-level safety net
        # The net can run before Click has created the root context. Inspecting
        # only the exact, argument-free root flag keeps the machine-readable
        # contract without trying to duplicate Click's parser here.
        _emit(Envelope.failure(exc), as_json=_argv_requests_json(sys.argv[1:]))
        sys.exit(1)


def _argv_requests_json(argv: list[str]) -> bool:
    """Recognise the root ``--json`` flag without interpreting the command."""
    for argument in argv:
        if argument == "--":
            break
        if argument == "--json":
            return True
    return False


if __name__ == "__main__":  # pragma: no cover
    main()
