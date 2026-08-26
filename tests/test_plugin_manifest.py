"""The plugin manifests and skills, checked against the code they describe.

A skill is prose that an agent acts on, and prose does not fail to compile. The
failure mode is silent and specific: a tool gets renamed, the skill keeps naming
the old one, and the agent that trusted it calls something that does not exist —
in somebody's Telegram account, mid-task. The manifests have the same problem
one level up: a `plugin.json` pointing at a file that was moved installs a
plugin with no server in it.

So these tests read what the manifests and skills actually say and compare it to
the registry and the filesystem. Nothing here checks that the advice is *good* —
only that every name in it is real.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

import telegram_ai_cli_mcp.ops  # noqa: F401  (registers every operation)
from telegram_ai_cli_mcp.opspec import REGISTRY

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / ".claude-plugin"
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"

#: Every `telegram_…` word a skill can say. Deliberately greedy: a tool name
#: this project does not publish is exactly what the test is looking for.
TOOL_MENTION = re.compile(r"`(telegram_[a-z_]+)`")


def skill_files() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def published_names() -> set[str]:
    names = {op.mcp_tool for op in REGISTRY.all() if op.mcp_tool}
    names |= {op.plan_tool for op in REGISTRY.all() if op.plan_tool}
    return names


# --- the manifests ----------------------------------------------------------


def test_the_plugin_manifest_is_json_and_points_at_files_that_exist() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text())

    assert manifest["name"] == "telegram-ai-cli-mcp"
    expected_repository = "https://github.com/stufently/telegram-ai-cli-mcp"
    assert manifest["homepage"] == expected_repository
    assert manifest["repository"] == expected_repository
    for key in ("mcpServers", "skills"):
        # `removeprefix`, not `lstrip`: `lstrip("./")` strips *characters*, so it
        # eats the leading dot of `./.claude/skills/` and looks for a directory
        # that was never there.
        target = REPO_ROOT / str(manifest[key]).removeprefix("./")
        assert target.exists(), f"plugin.json {key} points at {manifest[key]}, which is missing"


def test_the_marketplace_entry_describes_this_plugin() -> None:
    marketplace = json.loads((PLUGIN_DIR / "marketplace.json").read_text())
    plugin = json.loads((PLUGIN_DIR / "plugin.json").read_text())

    entries = {entry["name"]: entry for entry in marketplace["plugins"]}
    assert plugin["name"] in entries, "the marketplace lists no entry for this plugin"

    entry = entries[plugin["name"]]
    # The two files are read by different things and drift apart quietly; the
    # description is the one a person sees before installing.
    assert entry["description"] == plugin["description"]
    assert entry["repository"] == plugin["repository"]


def test_the_plugin_version_tracks_the_package_version() -> None:
    """Two version numbers in one repository drift, and this pair drifts
    silently: nothing installs the plugin from the package, so a release can
    bump `pyproject.toml` and leave the marketplace advertising the old one.

    They cannot be string-equal — a plugin version is semver and a Python
    version carries `.dev0` — so the release segment must match and the
    prerelease state must agree. The second half is the one with teeth: an
    installed plugin is only refreshed when this string changes, so publishing
    a pre-release as a bare `0.1.0` means the eventual real `0.1.0` is not a
    change, and everyone who installed early stays on the pre-release forever.
    """
    plugin = json.loads((PLUGIN_DIR / "plugin.json").read_text())
    package = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]

    release = re.match(r"^\d+\.\d+\.\d+", package)
    assert release, f"pyproject version {package!r} has no release segment to compare"
    assert plugin["version"].startswith(release.group(0)), (
        f"plugin.json says {plugin['version']}, pyproject.toml says {package}"
    )

    prerelease = package != release.group(0)
    assert prerelease == ("-" in plugin["version"]), (
        f"pyproject.toml says {package} and plugin.json says {plugin['version']}: "
        "one of them claims a release the other does not"
    )


def test_the_marketplace_is_named_the_way_the_readme_tells_people_to_install_it() -> None:
    """The marketplace name is the install address, and it is global to the
    machine: two marketplaces with one name means the second replaces the
    first. Renaming it and leaving the README on the old address leaves a
    command that installs nothing.
    """
    marketplace = json.loads((PLUGIN_DIR / "marketplace.json").read_text())
    plugin = json.loads((PLUGIN_DIR / "plugin.json").read_text())
    readme = (REPO_ROOT / "README.md").read_text()

    # Whole lines, not a substring: one marketplace name is a prefix of the
    # other here, so `... in readme` would keep passing after a rename back.
    wanted = f"claude plugin install {plugin['name']}@{marketplace['name']}"
    assert wanted in [line.strip() for line in readme.splitlines()], (
        f"the README does not tell anyone to run `{wanted}`"
    )


def test_the_server_is_launched_by_the_command_this_package_installs() -> None:
    """`plugin.mcp.json` names an executable; `pyproject.toml` creates it.

    Rename the console script and the plugin still installs, still publishes a
    server, and fails at the first tool call with "command not found".
    """
    mcp = json.loads((REPO_ROOT / "plugin.mcp.json").read_text())
    server = mcp["mcpServers"]["telegram"]

    scripts = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert server["command"] in scripts, (
        f"plugin.mcp.json runs {server['command']!r}, which this package does not install"
    )
    assert server["args"] == ["mcp"]


# --- the skills -------------------------------------------------------------


def test_there_is_at_least_one_skill() -> None:
    assert skill_files(), "the plugin declares a skills directory with nothing in it"


@pytest.mark.parametrize("skill", skill_files(), ids=lambda p: p.parent.name)
def test_a_skill_has_the_frontmatter_that_makes_it_findable(skill: Path) -> None:
    """Without `name` and `description` a skill is never selected, and nothing
    says so — it simply never fires."""
    text = skill.read_text()
    assert text.startswith("---\n"), f"{skill.parent.name} has no frontmatter"
    front = text.split("---", 2)[1]

    assert re.search(rf"^name:\s*{re.escape(skill.parent.name)}\s*$", front, re.M), (
        f"{skill.parent.name}: the `name` must match the directory"
    )
    described = re.search(r"^description:\s*(\S.*)$", front, re.M)
    assert described, f"{skill.parent.name}: no description"
    assert len(described.group(1)) > 40, (
        f"{skill.parent.name}: the description is what selects the skill; say when to use it"
    )


@pytest.mark.parametrize("skill", skill_files(), ids=lambda p: p.parent.name)
def test_every_tool_a_skill_names_is_one_this_server_publishes(skill: Path) -> None:
    published = published_names()
    mentioned = set(TOOL_MENTION.findall(skill.read_text()))

    assert mentioned, f"{skill.parent.name} names no tools at all"
    unknown = sorted(mentioned - published)
    assert not unknown, f"{skill.parent.name} names tools that do not exist: {unknown}"


def test_the_write_skill_names_every_plan_tool() -> None:
    """Its table is the one an agent reads to find out what it may prepare.

    A plan tool missing from it is an action the agent will report as impossible
    while it sits in the registry.
    """
    text = (SKILLS_DIR / "telegram-write-by-plan" / "SKILL.md").read_text()
    mentioned = set(TOOL_MENTION.findall(text))
    plan_tools = {op.plan_tool for op in REGISTRY.all() if op.plan_tool}

    missing = sorted(plan_tools - mentioned)
    assert not missing, f"the write skill does not mention: {missing}"


def test_no_skill_promises_a_tool_that_applies_a_plan() -> None:
    """The invariant the whole approval design rests on, asserted where it is
    most likely to be quietly contradicted: in prose."""
    for skill in skill_files():
        text = skill.read_text().lower()
        assert "telegram_apply" not in text
        assert "telegram_plan_apply" not in text
