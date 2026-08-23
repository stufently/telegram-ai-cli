"""Where a setting comes from when two sources disagree.

The README, `.env.example` and `docs/configuration.md` all promise the same
thing: the YAML file provides the base, `TGAI_`-prefixed environment variables
win. That promise was false for a while — `load_settings` passes the file in as
init keyword arguments, and pydantic-settings ranks those above the environment
unless a class says otherwise.

The widening direction is the harmless one. The direction these tests exist for
is narrowing: an operator, a container or a systemd unit must be able to take
away what a config file on disk grants, and `TGAI_PROFILE=readonly` over
`profile: plan` is exactly that case.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from telegram_ai_cli.config import Settings, load_settings


@pytest.fixture(autouse=True)
def _no_inherited_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TGAI_ variable in the developer's shell must not steer these tests."""
    for name in ("TGAI_PROFILE", "TGAI_SAFETY__WRITE__SEND__ALLOW", "TGAI_CONFIG"):
        monkeypatch.delenv(name, raising=False)


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tgai.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_file_wins_over_the_built_in_default(tmp_path: Path) -> None:
    assert Settings().profile == "readonly"
    assert load_settings(write_config(tmp_path, "profile: plan\n")).profile == "plan"


def test_the_environment_wins_over_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TGAI_PROFILE", "plan")
    assert load_settings(write_config(tmp_path, "profile: readonly\n")).profile == "plan"


def test_the_environment_can_narrow_what_the_file_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direction that is a security property rather than a convenience."""
    monkeypatch.setenv("TGAI_PROFILE", "readonly")
    assert load_settings(write_config(tmp_path, "profile: plan\n")).profile == "readonly"


def test_one_environment_override_does_not_discard_the_rest_of_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sources are merged, not swapped.

    An environment variable naming one allowlist must not silently empty the
    others — that would turn a narrowing intent into a much wider change than
    the operator asked for.
    """
    path = write_config(
        tmp_path,
        "profile: plan\n"
        "safety:\n"
        "  read:\n"
        "    dms:\n"
        "      allow: [123456789]\n"
        "  write:\n"
        "    send:\n"
        "      allow: [123456789]\n",
    )
    monkeypatch.setenv("TGAI_SAFETY__WRITE__SEND__ALLOW", '["987654321"]')

    settings = load_settings(path)

    # A JSON list from the environment keeps its strings; the file's YAML
    # integers stay integers. Both are fine — the kernel compares allowlist
    # entries as normalised strings, which is what lets a rule name a
    # `@username` as readily as a numeric id.
    assert settings.safety.write.send.allow == ["987654321"]
    assert settings.safety.read.dms.allow == [123456789]
    assert settings.profile == "plan"


def test_a_section_commented_out_falls_back_to_defaults(tmp_path: Path) -> None:
    """The normal shape of an example config: a key with nothing under it."""
    settings = load_settings(write_config(tmp_path, "profile: plan\nlimits:\n"))
    assert settings.profile == "plan"
    assert settings.limits.sends_per_account == 30
