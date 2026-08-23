"""Fixtures every handler test shares. The stand-ins themselves are in `fakes`."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeRegistry

from telegram_ai_cli.audit import AuditLog
from telegram_ai_cli.config import Settings
from telegram_ai_cli.context import OperationContext
from telegram_ai_cli.safety import SafetyKernel


@pytest.fixture
def make_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build an :class:`OperationContext` around a fake client.

    Settings arrive as keyword overrides so a test states only the policy it is
    about — ``make_context(client, safety={"read": {"enumerate_dms": True}})``
    reads as the sentence it is testing.

    Every ``TGAI_*`` variable is cleared first. The environment outranks
    constructor arguments in :class:`Settings`, so without this a developer who
    exports a deny list to try something out gets failures in tests that never
    mention one — and the failure names the assertion, not the export.
    """
    for name in [key for key in os.environ if key.startswith("TGAI_")]:
        monkeypatch.delenv(name, raising=False)

    def build(client: Any, **overrides: Any) -> OperationContext:
        settings = Settings(**overrides)
        return OperationContext(
            settings=settings,
            safety=SafetyKernel(settings),
            plans=None,  # type: ignore[arg-type] - read handlers never touch it
            limits=None,  # type: ignore[arg-type]
            audit=AuditLog(tmp_path / "audit.log", settings.audit),
            actor="cli",
            accounts=FakeRegistry(client),  # type: ignore[arg-type]
        )

    return build
