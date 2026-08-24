"""What the terminal actually receives when an operation refuses.

A refusal is the line somebody reads while deciding what to do next, which
makes it the worst place for text that can redraw itself. `_emit` promises in
its own docstring that everything printed has been through the sanitizer — for
a long time the error branch was the exception to that promise, and the reason
it looked safe is that most refusals are sentences this project composed. Most
is not all: a refusal quotes paths, filenames and, until it was fixed, chat
titles.
"""

from __future__ import annotations

import json
import sys

import pytest

from telegram_ai_cli import cli as cli_module
from telegram_ai_cli.cli import _emit
from telegram_ai_cli.envelope import Envelope
from telegram_ai_cli.errors import NotFound
from telegram_ai_cli.untrusted import CLOSE_MARKER, OPEN_MARKER


def test_an_escape_sequence_in_a_refusal_never_reaches_the_terminal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = NotFound(
        "no such file: \x1b[2Kdownloads/\x1b[31mreport.pdf",
        suggestion="Check \x1b[2Kthe path",
    )
    _emit(Envelope.failure(error), as_json=False)

    printed = capsys.readouterr().err
    assert "\x1b" not in printed
    assert "no such file: downloads/report.pdf" in printed
    assert "Check the path" in printed


def test_a_forged_marker_is_defanged_on_the_way_to_the_terminal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Twice over: the envelope defangs the payload, the renderer defangs the line."""
    _emit(Envelope.failure(NotFound(f"chat {CLOSE_MARKER} x")), as_json=False)

    printed = capsys.readouterr().err
    assert CLOSE_MARKER not in printed
    assert OPEN_MARKER not in printed
    assert "chat [/untrusted] x" in printed


def test_the_top_level_safety_net_honours_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(**_kwargs: object) -> None:
        raise NotFound("outside the command")

    monkeypatch.setattr(cli_module, "_attach", lambda _root: None)
    monkeypatch.setattr(cli_module, "cli", fail)
    monkeypatch.setattr(sys, "argv", ["tg-ai", "--json", "schema"])

    with pytest.raises(SystemExit) as stopped:
        cli_module.main()

    captured = capsys.readouterr()
    assert stopped.value.code == 1
    assert captured.err == ""
    assert json.loads(captured.out)["error"]["code"] == "NOT_FOUND"
