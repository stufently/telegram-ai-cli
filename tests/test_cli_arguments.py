"""What the terminal can express, and what reaches the model when it does.

Every command here is generated from a Pydantic model at import time, so an
argument shape the generator cannot express is not a missing feature in one
command — it is a whole class of commands that exist in `--help`, accept a
value, and mean something else by it. That is how `chat restrict` came to be
listed, documented and unusable: `--restrictions` took a string, the model
wanted an object, and the only sign was a validation error about a field the
person had in fact supplied.

These tests assert on the dictionary handed to `dispatch.run`, because that is
the boundary where the terminal stops and the shared operation begins — the MCP
server hands over the same dictionary, and the whole point of generating the
CLI is that the two cannot disagree about what an argument means.
"""

from __future__ import annotations

from typing import Any, get_args, get_origin

import click
import pytest
from click.testing import CliRunner
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

import telegram_ai_cli.ops  # noqa: F401  (registers every operation)
from telegram_ai_cli import cli as cli_module
from telegram_ai_cli.envelope import Envelope
from telegram_ai_cli.opspec import REGISTRY


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Run commands with the operation itself stubbed out.

    The subject is the arguments, not the account: stopping at `dispatch.run`
    keeps the test on the parsing boundary and away from a Telegram session.
    """
    seen: list[dict[str, Any]] = []

    def fake_run(op: Any, raw: dict[str, Any], **kwargs: Any) -> Envelope:
        seen.append(raw)
        return Envelope.success({"ok": True})

    monkeypatch.setattr(cli_module.dispatch, "run", fake_run)
    return seen


@pytest.fixture
def run() -> Any:
    root = click.Group(context_settings={"help_option_names": ["-h", "--help"]})
    root.params = list(cli_module.cli.params)
    root.callback = cli_module.cli.callback
    cli_module._attach(root)
    runner = CliRunner()

    def invoke(*args: str) -> Any:
        return runner.invoke(root, list(args), catch_exceptions=False)

    return invoke


# --- repeated options ------------------------------------------------------


def test_a_repeated_option_becomes_a_list(run: Any, captured: list[dict[str, Any]]) -> None:
    """Two ids, two flags, one list — and integers, not the strings an
    `Annotated[int, ...]` field used to be reduced to."""
    result = run(
        "message",
        "forward",
        "--source-chat",
        "@a",
        "--destination-chat",
        "@b",
        "--message-ids",
        "5",
        "--message-ids",
        "6",
    )

    assert result.exit_code == 0, result.output
    assert captured[0]["message_ids"] == [5, 6]


def test_a_single_repetition_is_still_a_list(run: Any, captured: list[dict[str, Any]]) -> None:
    """The shape must not depend on how many times the flag appeared."""
    run(
        "message",
        "forward",
        "--source-chat",
        "@a",
        "--destination-chat",
        "@b",
        "--message-ids",
        "5",
    )

    assert captured[0]["message_ids"] == [5]


def test_an_omitted_repeatable_option_is_not_an_empty_list(
    run: Any, captured: list[dict[str, Any]]
) -> None:
    """Click answers an unused repeatable option with `()`, not `None`.

    Forwarding that would say "delete this empty list of messages" where the
    caller meant "the link names the message", and the model's own default —
    the thing that distinguishes those two — would never be consulted.
    """
    run("message", "delete", "--chat", "https://t.me/example/412")

    assert "message_ids" not in captured[0]
    assert captured[0]["chat"] == "https://t.me/example/412"


def test_a_field_with_a_default_factory_is_left_to_its_factory(
    run: Any, captured: list[dict[str, Any]]
) -> None:
    """`users` reports `PydanticUndefined` as its default, not `[]`.

    Passing that sentinel through as a value made `chat create` reject its own
    default — the command was unusable even without the argument it could not
    express.
    """
    result = run("chat", "create", "--title", "Marketing")

    assert result.exit_code == 0, result.output
    assert "users" not in captured[0]


def test_a_repeated_option_of_strings_survives_as_typed(
    run: Any, captured: list[dict[str, Any]]
) -> None:
    result = run("watch", "--chats", "@one", "--chats", "@two")

    assert result.exit_code == 0, result.output
    assert captured[0]["chats"] == ["@one", "@two"]


def test_the_comma_form_still_reaches_the_model_as_one_string(
    run: Any, captured: list[dict[str, Any]]
) -> None:
    """`watch` grew a comma-splitting validator to work around this gap.

    It stays, so the form people already type keeps working — but it is the
    model's convenience now, not the CLI's only route, and the CLI must hand it
    over untouched rather than pre-splitting on its own.
    """
    result = run("watch", "--chats", "@one,@two")

    assert result.exit_code == 0, result.output
    assert captured[0]["chats"] == ["@one,@two"]


# --- a peer is an id or a name, and the difference matters ------------------


def test_a_numeric_peer_reaches_telethon_as_an_id(run: Any, captured: list[dict[str, Any]]) -> None:
    """`int | str` collapsed to `str`, and a numeric string is not an id.

    Telethon's `parse_phone` accepts any run of digits, so `--chat 4242` as
    text takes the *phone* branch and searches the account's contacts. Usually
    that ends in "cannot find any entity"; if the digits happen to match a
    contact's number, it ends in a write to the wrong person.
    """
    run("message", "send", "--chat", "-1001234567890", "--text", "hello")

    assert captured[0]["chat"] == -1001234567890


def test_a_named_peer_is_left_alone(run: Any, captured: list[dict[str, Any]]) -> None:
    """A username cannot be all digits and a phone keeps its `+`, which is what
    makes the rule unambiguous rather than a guess.

    The `+` case is deliberately too short to be a real number: this repository
    refuses to hold anything E.164-shaped (`test_no_private_data`), and what is
    being tested is the leading `+`, not the digits after it.
    """
    for given in ("@marketing", "https://t.me/example/412", "+1234567", "-"):
        captured.clear()
        run("message", "send", "--chat", given, "--text", "hello")
        assert captured[0]["chat"] == given


def test_a_numeric_member_in_a_list_is_an_id_too(run: Any, captured: list[dict[str, Any]]) -> None:
    """The same rule inside a repeatable option, which is where it was found."""
    run("chat", "create", "--title", "Marketing", "--users", "555", "--users", "@sam")

    assert captured[0]["users"] == [555, "@sam"]


def test_every_peer_field_in_the_registry_reads_an_id_as_an_id() -> None:
    fields = [
        (op, name)
        for op in REGISTRY.all()
        for name, field in op.input_model.model_fields.items()
        if set(a for a in get_args(cli_module._without_none(field.annotation)) if a is not None)
        == {int, str}
    ]
    assert fields, "no `int | str` fields found — the check would pass vacuously"

    for op, name in fields:
        option = next(o for o in cli_module._options_for(op.input_model) if o.name == name)
        assert isinstance(option.type, cli_module._IntOrText), (
            f"{' '.join(op.cli)} --{name} reads a numeric id as a phone number"
        )


# --- object-valued options -------------------------------------------------


def test_an_object_argument_is_parsed_from_json(run: Any, captured: list[dict[str, Any]]) -> None:
    result = run(
        "chat",
        "restrict",
        "--chat",
        "@group",
        "--user",
        "@sam",
        "--restrictions",
        '{"send_messages": true, "embed_links": true}',
    )

    assert result.exit_code == 0, result.output
    assert captured[0]["restrictions"] == {"send_messages": True, "embed_links": True}


def test_malformed_json_is_refused_before_the_operation_runs(
    run: Any, captured: list[dict[str, Any]]
) -> None:
    """A usage error, naming the shape — not a validation error about a field
    the caller did supply."""
    result = run(
        "chat", "promote", "--chat", "@group", "--user", "@sam", "--rights", "pin_messages"
    )

    assert result.exit_code == 2
    assert "not valid JSON" in result.output
    assert captured == []


def test_a_json_array_is_not_an_object(run: Any, captured: list[dict[str, Any]]) -> None:
    """`[...]` parses, so only a type check catches it."""
    result = run(
        "chat", "promote", "--chat", "@group", "--user", "@sam", "--rights", '["pin_messages"]'
    )

    assert result.exit_code == 2
    assert "expected a JSON object" in result.output
    assert captured == []


def test_the_help_names_the_keys_the_object_accepts(run: Any) -> None:
    """An object argument documented only in `docs/operations.md` is one a
    person at a terminal cannot type."""
    result = run("chat", "promote", "-h")

    assert "JSON object; keys:" in result.output
    for key in ("delete_messages", "ban_users", "add_admins"):
        assert key in result.output


# --- the generator, over the whole registry --------------------------------


def _fields_of_shape(kind: str) -> list[tuple[Any, str]]:
    found = []
    for op in REGISTRY.all():
        for name, field in op.input_model.model_fields.items():
            inner = cli_module._without_none(field.annotation)
            is_list = get_origin(inner) in (list, set, tuple)
            is_model = isinstance(inner, type) and issubclass(inner, BaseModel)
            if (kind == "list" and is_list) or (kind == "model" and is_model):
                found.append((op, name))
    return found


def test_every_list_field_in_the_registry_is_repeatable() -> None:
    """The guard against the next one.

    This gap was not one command's oversight; it was the generator quietly
    mapping `list[int]` to `int` for whichever commands happened to have such a
    field. A new one added tomorrow inherits the fix only if something checks.
    """
    fields = _fields_of_shape("list")
    assert fields, "no list-valued fields found — the check would pass vacuously"

    for op, name in fields:
        option = next(o for o in cli_module._options_for(op.input_model) if o.name == name)
        assert option.multiple, f"{' '.join(op.cli)} --{name} takes one value, not a list"


def test_every_nested_model_field_in_the_registry_takes_json() -> None:
    fields = _fields_of_shape("model")
    assert fields, "no object-valued fields found — the check would pass vacuously"

    for op, name in fields:
        option = next(o for o in cli_module._options_for(op.input_model) if o.name == name)
        assert isinstance(option.type, cli_module._JsonObject), (
            f"{' '.join(op.cli)} --{name} takes a bare string, not an object"
        )


def test_no_option_default_is_a_pydantic_sentinel() -> None:
    """`PydanticUndefined` is not a value, and Click cannot tell."""
    for op in REGISTRY.all():
        for option in cli_module._options_for(op.input_model):
            assert option.default is not PydanticUndefined, (
                f"{' '.join(op.cli)} --{option.name} defaults to a sentinel"
            )


def test_annotated_integer_fields_are_typed_as_integers() -> None:
    """`Annotated[int, Field(le=...)]` is how a bounded id is spelled here, and
    the unwrap that used to miss it turned every one of them into text."""
    from telegram_ai_cli.ops.write import MessageId

    assert cli_module._click_type(MessageId) is int
    assert cli_module._click_type(get_args(list[MessageId])[0]) is int


# --- and the model on the other side of the boundary ------------------------


def test_what_the_terminal_produces_is_what_the_model_accepts() -> None:
    """The two halves, joined.

    Click parses `--restrictions` into a dictionary and the model turns that
    dictionary into a `Restrictions`. Each half has its own test above and in
    `test_moderation.py`; neither one notices if the shapes stop meeting.
    """
    op = REGISTRY.by_name("chat.restrict")
    parsed = op.parse({"chat": "@group", "user": "@sam", "restrictions": {"send_messages": True}})

    assert parsed.restrictions.send_messages is True
    assert parsed.restrictions.pin_messages is False
