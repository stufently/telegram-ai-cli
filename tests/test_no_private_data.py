"""Nothing from the author's own infrastructure may reach this public repository.

This test has two halves, and the split is deliberate.

The half that always runs looks for *shapes*: a number written in E.164 form, a
Telegram supergroup id, an IP address. Shapes need no secret list, so the check
works for a stranger cloning the repository, and it catches the common accident
— a real value pasted into an example.

The half that needs the author's own list is opt-in through
``TGAI_PRIVATE_DENYLIST``, a path to a file kept **outside** this repository.
The forbidden values are not written here, because writing them down is exactly
the leak the test exists to prevent. Failures name the file and the entry
number, never the value.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "node_modules",
    "htmlcov",
    "dist",
    "build",
    ".eggs",
}

SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
    ".pdf",
    ".zip",
    ".gz",
    ".xz",
    ".whl",
    ".so",
    ".session",
    ".sqlite3",
}

MAX_BYTES = 2 * 1024 * 1024

#: Written in E.164 form, which is how a real number gets pasted by accident.
PHONE = re.compile(r"\+\d{10,15}(?!\d)")

#: Telegram supergroup and channel ids: -100 followed by the internal id.
SUPERGROUP_ID = re.compile(r"(?<![\w-])-100\d{7,}")

IPV4 = re.compile(r"(?<![\d.\w])\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])")

#: The canonical placeholder used throughout the documentation. Visibly a
#: placeholder — the digits run 1234567890 — and it resolves to nothing.
DOCUMENTED_PLACEHOLDER_IDS = {"-1001234567890"}


def _is_documentation_ip(candidate: str) -> bool:
    """Loopback, unspecified and the RFC 5737 example ranges are not private data."""
    try:
        address = ipaddress.IPv4Address(candidate)
    except ValueError:
        # Four dotted numbers that are not an address — a version string.
        return True
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        return True
    documentation = (
        ipaddress.ip_network("192.0.2.0/24"),
        ipaddress.ip_network("198.51.100.0/24"),
        ipaddress.ip_network("203.0.113.0/24"),
    )
    return any(address in network for network in documentation)


def _repo_files() -> Iterator[Path]:
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(REPO_ROOT)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if path.stat().st_size > MAX_BYTES:
            continue
        yield path


def _text_files() -> Iterator[tuple[Path, str]]:
    for path in _repo_files():
        try:
            yield path, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: nothing to grep


def _location(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _line_at(text: str, offset: int) -> str:
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    return text[start:] if end == -1 else text[start:end]


def test_the_scan_actually_reads_the_repository() -> None:
    """A scan that silently walks nothing would pass forever."""
    scanned = {path.name for path, _ in _text_files()}
    assert "pyproject.toml" in scanned
    assert len(scanned) > 5


def test_no_phone_numbers() -> None:
    findings = [
        f"{path.relative_to(REPO_ROOT)}:{_location(text, match.start())}"
        for path, text in _text_files()
        for match in PHONE.finditer(text)
    ]
    assert not findings, f"E.164-shaped numbers found at: {findings}"


def test_no_real_looking_chat_ids() -> None:
    findings = [
        f"{path.relative_to(REPO_ROOT)}:{_location(text, match.start())}"
        for path, text in _text_files()
        for match in SUPERGROUP_ID.finditer(text)
        if match.group(0) not in DOCUMENTED_PLACEHOLDER_IDS
    ]
    assert not findings, f"supergroup-shaped ids found at: {findings}"


def test_no_host_addresses() -> None:
    findings = [
        f"{path.relative_to(REPO_ROOT)}:{_location(text, match.start())}"
        for path, text in _text_files()
        for match in IPV4.finditer(text)
        if not _is_documentation_ip(match.group(0))
        # A four-part version pin is four dotted numbers too.
        and "==" not in _line_at(text, match.start())
    ]
    assert not findings, f"IP addresses found at: {findings}"


def test_no_state_or_session_material_is_committed() -> None:
    """Sessions, keys and databases belong in the state directory, never here."""
    forbidden = [
        str(path.relative_to(REPO_ROOT))
        for path in REPO_ROOT.rglob("*")
        if path.is_file()
        and not any(part in SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts)
        and (
            path.name.endswith((".session", ".sqlite3", "secret.key", "audit.jsonl"))
            or path.name == ".env"
        )
    ]
    assert not forbidden, f"account or state material committed: {forbidden}"


def test_the_full_denylist_from_outside_the_repository() -> None:
    """The other half of the check, and the only one that can catch a name.

    Structural patterns cannot recognise an account label, a proxy host or a
    session name. That list lives outside this repository; point
    ``TGAI_PRIVATE_DENYLIST`` at it to run this check.
    """
    denylist_path = os.environ.get("TGAI_PRIVATE_DENYLIST")
    if not denylist_path:
        pytest.skip(
            "TGAI_PRIVATE_DENYLIST is not set, so only the structural checks ran. "
            "The full list of forbidden strings is kept outside this repository "
            "on purpose; set the variable to a file with one value per line to "
            "run the complete check."
        )

    source = Path(denylist_path)
    assert source.is_file(), f"TGAI_PRIVATE_DENYLIST points at {denylist_path}, which is not a file"
    assert REPO_ROOT not in source.resolve().parents, (
        "the denylist must live outside the repository it protects"
    )

    entries = [
        line.strip().lower()
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert entries, f"{denylist_path} is empty: nothing was checked"

    for path, text in _text_files():
        haystack = text.lower()
        for index, entry in enumerate(entries):
            if entry in haystack:
                # The value itself is never printed: that would put it in the
                # test output, the CI log, and eventually somewhere public.
                pytest.fail(f"denylist entry #{index} appears in {path.relative_to(REPO_ROOT)}")
