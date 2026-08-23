"""Sending a file as a plan operation: the wiring around `outbox`.

The path rule itself is tested in `test_outbox.py`. What is asserted here is
that the operation is declared the way every other remote write is — planned
over MCP, applied from a terminal — that its input can be expressed on both
surfaces, and that the applier re-checks the file rather than trusting the plan.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_ai_cli.apply import (
    _LIMIT_KINDS,
    _UPLOAD_OPERATIONS,
    RPC_TIMEOUT_SECONDS,
    _check_file,
    _execute,
    _Prepared,
    _rpc_timeout,
)
from telegram_ai_cli.cli import _options_for
from telegram_ai_cli.config import PathsConfig, Settings, UploadConfig
from telegram_ai_cli.errors import InvalidInput, PlanPreconditionFailed
from telegram_ai_cli.limits import LimitKind
from telegram_ai_cli.ops import write
from telegram_ai_cli.ops.write import Resolved
from telegram_ai_cli.opspec import REGISTRY, Effect
from telegram_ai_cli.outbox import resolve_outbound
from telegram_ai_cli.plans import Plan, PlanState
from telegram_ai_cli.safety import Capability, PeerKind, PeerRef

OPERATION = "message.send_file"


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("TGAI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def outbox(tmp_path: Path) -> Path:
    root = tmp_path / "outbox"
    root.mkdir()
    return root


def settings_for(outbox: Path, **upload: object) -> Settings:
    return Settings(
        paths=PathsConfig(uploads=outbox),
        upload=UploadConfig(**upload),  # type: ignore[arg-type]
    )


# --- how the operation is declared -----------------------------------------


def test_the_operation_is_planned_over_mcp_and_applied_from_a_terminal() -> None:
    """The property the whole project rests on, asserted for this operation."""
    op = REGISTRY.by_name(OPERATION)
    assert op.effect is Effect.REMOTE_WRITE
    assert op.mcp_tool is None
    assert op.plan_tool == "telegram_plan_send_file"
    assert op.capability is Capability.SEND
    assert op.planner is not None and op.handler is None
    assert REGISTRY.by_mcp_tool("telegram_plan_send_file").name == OPERATION
    assert REGISTRY.by_cli(("message", "send-file")).name == OPERATION


def test_it_is_listed_among_the_remote_writes() -> None:
    """The applier dispatches on the name; the list is what says it must."""
    assert write.SEND_FILE in write.WRITE_OPERATIONS


def test_it_draws_on_the_send_budget() -> None:
    """A write with no budget is refused at apply time, which is a dead plan."""
    assert _LIMIT_KINDS[OPERATION] is LimitKind.SEND


def test_the_generated_cli_can_express_every_argument() -> None:
    """Unlike `message delete`, this one is usable from a terminal.

    The CLI generator maps a list to a single value, so an operation with a
    list-valued argument can only be planned over MCP. Keeping every field here
    scalar is what avoids that — and one file per plan is the rule anyway.
    """
    flags = {opt for option in _options_for(write.SendFileInput) for opt in option.opts}
    assert {
        "--account",
        "--chat",
        "--path",
        "--caption",
        "--reply-to-message-id",
        "--as-document",
        "--silent",
    } <= flags


def test_the_published_schema_states_the_ceilings_and_the_path_rule() -> None:
    schema = REGISTRY.by_name(OPERATION).input_schema()
    assert schema["properties"]["caption"]["maxLength"] == write.MAX_CAPTION_CHARS
    assert "paths.uploads" in schema["properties"]["path"]["description"]
    assert sorted(schema["required"]) == ["chat", "path"]


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ({"chat": 1}, "path"),
        ({"path": "a.txt"}, "chat"),
        ({"chat": 1, "path": "a.txt", "caption": "x" * 1025}, "caption"),
        ({"chat": 1, "path": "a.txt", "reply_to_message_id": 0}, "reply_to_message_id"),
        ({"chat": 1, "path": "a.txt", "paths": ["a", "b"]}, "paths"),
    ],
)
def test_bad_input_is_refused_by_name(raw: dict[str, object], problem: str) -> None:
    with pytest.raises(InvalidInput, match=problem):
        REGISTRY.by_name(OPERATION).parse(raw)


# --- what the applier re-checks --------------------------------------------


def test_the_same_file_passes_its_own_precondition(outbox: Path) -> None:
    (outbox / "report.pdf").write_bytes(b"the reviewed bytes")
    settings = settings_for(outbox)
    planned = resolve_outbound(settings, "report.pdf")

    assert _check_file(planned.snapshot(), resolve_outbound(settings, "report.pdf")) == []


def test_a_file_swapped_after_review_is_refused(outbox: Path) -> None:
    """The attack a digest exists for: same name, different bytes."""
    path = outbox / "report.pdf"
    path.write_bytes(b"the reviewed bytes")
    settings = settings_for(outbox)
    planned = resolve_outbound(settings, "report.pdf").snapshot()

    path.write_bytes(b"something else entirely")
    with pytest.raises(PlanPreconditionFailed, match="reviewed"):
        _check_file(planned, resolve_outbound(settings, "report.pdf"))


def test_a_truncated_file_is_refused(outbox: Path) -> None:
    path = outbox / "report.pdf"
    path.write_bytes(b"the reviewed bytes")
    settings = settings_for(outbox)
    planned = resolve_outbound(settings, "report.pdf").snapshot()

    path.write_bytes(b"the reviewed")
    with pytest.raises(PlanPreconditionFailed):
        _check_file(planned, resolve_outbound(settings, "report.pdf"))


def test_the_same_bytes_under_another_name_are_a_warning_not_a_refusal(outbox: Path) -> None:
    """It is the file that was approved; the name it arrived under is not the point."""
    (outbox / "first.pdf").write_bytes(b"identical bytes")
    (outbox / "second.pdf").write_bytes(b"identical bytes")
    settings = settings_for(outbox)

    planned = resolve_outbound(settings, "first.pdf").snapshot()
    warnings = _check_file(planned, resolve_outbound(settings, "second.pdf"))
    assert warnings and "identical" in warnings[0]


def test_a_delivery_form_that_changed_under_the_plan_is_refused(outbox: Path) -> None:
    """The reviewed line said which form; the other form was not approved."""
    (outbox / "photo.jpg").write_bytes(b"pretend this is a jpeg")
    settings = settings_for(outbox)
    planned = resolve_outbound(settings, "photo.jpg").snapshot()

    forced = resolve_outbound(settings, "photo.jpg", as_document=True)
    with pytest.raises(PlanPreconditionFailed, match="document"):
        _check_file(planned, forced)


# --- the timeout ------------------------------------------------------------


def test_an_upload_gets_the_upload_ceiling_and_nothing_else_does(outbox: Path) -> None:
    """A transfer is not a request, and 60 seconds is not enough for one.

    Raising the ceiling globally would leave a stuck ordinary send sitting for
    minutes before anybody heard about it, so only this operation moves.
    """

    class _Ctx:
        settings = settings_for(outbox, timeout_seconds=600)

    assert OPERATION in _UPLOAD_OPERATIONS
    assert _rpc_timeout(_Ctx(), OPERATION) == 600  # type: ignore[arg-type]
    assert _rpc_timeout(_Ctx(), "message.send") == RPC_TIMEOUT_SECONDS  # type: ignore[arg-type]


# --- what actually reaches Telethon ----------------------------------------


class _FakeClient:
    """Records the one call `_execute` is allowed to make."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, object, dict[str, object]]] = []

    async def send_file(self, entity: object, file: object, **kwargs: object) -> object:
        self.calls.append((entity, file, kwargs))
        return SimpleNamespace(id=4242)


def _plan_for(params: write.SendFileInput) -> Plan:
    return Plan(
        plan_id="0" * 32,
        operation=OPERATION,
        account="work",
        params=params.model_dump(mode="json"),
        preconditions={},
        summary="",
        state=PlanState.APPLYING,
        created_at=0.0,
        expires_at=0.0,
    )


async def _send(outbox: Path, name: str, content: bytes = b"bytes", **overrides: object):
    (outbox / name).write_bytes(content)
    params = write.SendFileInput(chat=-1001, path=name, **overrides)  # type: ignore[arg-type]
    resolved = resolve_outbound(
        settings_for(outbox), name, as_document=bool(overrides.get("as_document", False))
    )
    prepared = _Prepared(
        limit_target="-1001",
        peers={"chat": Resolved(ref=PeerRef(peer_id=-1001, kind=PeerKind.GROUP))},
        attachment=resolved,
    )
    client = _FakeClient()
    outcome, warnings = await _execute(client, _plan_for(params), params, prepared)
    return client.calls[0], outcome, warnings, resolved


async def test_a_document_is_sent_with_force_document(outbox: Path) -> None:
    """The flag the preview's promise rests on, asserted on the actual call."""
    (entity, file, kwargs), outcome, _, resolved = await _send(outbox, "report.pdf")

    assert entity == -1001
    assert file == str(resolved.path)
    assert kwargs["force_document"] is True
    assert kwargs["voice_note"] is False
    # An empty caption is None, not "": Telethon would otherwise attach one.
    assert kwargs["caption"] is None
    assert kwargs["reply_to"] is None
    assert kwargs["silent"] is False
    assert outcome == {
        "message_id": 4242,
        "file": "report.pdf",
        "bytes": resolved.size_bytes,
        "sha256": resolved.sha256,
        "delivery": "document",
    }


async def test_a_jpeg_goes_as_a_photo_exactly_as_the_preview_said(outbox: Path) -> None:
    """`force_document` false is what makes Telethon compress it — as previewed."""
    (_, _, kwargs), outcome, _, _ = await _send(outbox, "holiday.jpg")
    assert kwargs["force_document"] is False
    assert outcome["delivery"] == "photo"


async def test_as_document_overrides_the_photo_form_on_the_call(outbox: Path) -> None:
    (_, _, kwargs), outcome, _, _ = await _send(outbox, "holiday.jpg", as_document=True)
    assert kwargs["force_document"] is True
    assert outcome["delivery"] == "document"


async def test_a_voice_file_is_sent_as_a_voice_note(outbox: Path) -> None:
    (_, _, kwargs), outcome, _, _ = await _send(outbox, "memo.ogg")
    assert kwargs["voice_note"] is True
    assert kwargs["force_document"] is False
    assert outcome["delivery"] == "voice"


async def test_caption_reply_and_silence_are_passed_through(outbox: Path) -> None:
    (_, _, kwargs), _, _, _ = await _send(
        outbox,
        "report.pdf",
        caption="have a look",
        reply_to_message_id=77,
        silent=True,
    )
    assert kwargs["caption"] == "have a look"
    assert kwargs["reply_to"] == 77
    assert kwargs["silent"] is True


async def test_an_unresolved_attachment_refuses_rather_than_uploading_nothing(
    outbox: Path,
) -> None:
    """Belt: `_verify` always sets it, and a missing one must not reach Telethon."""
    params = write.SendFileInput(chat=-1001, path="report.pdf")
    prepared = _Prepared(
        limit_target="-1001",
        peers={"chat": Resolved(ref=PeerRef(peer_id=-1001, kind=PeerKind.GROUP))},
    )
    with pytest.raises(InvalidInput):
        await _execute(_FakeClient(), _plan_for(params), params, prepared)
