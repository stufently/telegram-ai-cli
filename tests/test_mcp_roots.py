"""MCP roots as a ceiling on where downloaded media may land.

`media fetch` already refuses to take a destination from the caller: it writes
into `paths.downloads` and hands back an opaque id. Roots are the other
direction — the *client* saying which directories it sanctions — and they can
only narrow that: a configured download directory outside every advertised root
is refused, and nothing is ever quietly written somewhere else instead.

Two absences mean different things and both are tested, because getting them
the wrong way round breaks either every client that does not implement roots or
the whole point of the feature:

* **no roots capability** — the client cannot be asked, so nothing is
  constrained;
* **the capability, and an empty list** — the client was asked and answered
  "none", which is an answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import mcp.types as types
import pytest
import yaml

import telegram_ai_cli_mcp.ops  # noqa: F401  (registers every operation)
from telegram_ai_cli_mcp.errors import ErrorCode, NotAllowlisted
from telegram_ai_cli_mcp.mcp_server import build_server
from telegram_ai_cli_mcp.opspec import REGISTRY, Effect
from telegram_ai_cli_mcp.roots import (
    advertised_roots,
    is_within,
    require_within_roots,
    resolved,
    roots_from_uris,
)


@dataclass
class FakeSession:
    """Enough of `ServerSession` for the two questions this asks it."""

    #: `None` means the client never declared the roots capability.
    uris: list[str] | None
    failure: Exception | None = None
    asked: list[Any] = field(default_factory=list)

    def check_client_capability(self, capability: Any) -> bool:
        self.asked.append(capability)
        return self.uris is not None

    async def list_roots(self) -> Any:
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(roots=[SimpleNamespace(uri=uri) for uri in self.uris or []])


def as_uri(path: Path) -> str:
    return path.as_uri()


# -- containment, decided on canonical paths --------------------------------


def test_a_directory_is_within_itself_and_its_children(tmp_path: Path) -> None:
    root = resolved(tmp_path)

    assert is_within(root, root)
    assert is_within(resolved(tmp_path / "a" / "b"), root)


def test_traversal_does_not_stay_inside(tmp_path: Path) -> None:
    """`root/../elsewhere` is a string that starts with the root and is not in it."""
    root = resolved(tmp_path / "root")
    escape = resolved(tmp_path / "root" / ".." / "elsewhere")

    assert not is_within(escape, root)


def test_a_symlink_out_of_the_root_is_not_inside_it(tmp_path: Path) -> None:
    """The check a prefix comparison on raw strings gets wrong."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)

    assert not is_within(resolved(root / "link"), resolved(root))


def test_a_symlinked_root_is_resolved_before_it_is_compared(tmp_path: Path) -> None:
    """A client may well advertise a path that runs through a symlink."""
    real_root = tmp_path / "real"
    real_root.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_root)

    require_within_roots(real_root / "downloads", [resolved(link)], what="media.fetch")


def test_a_sibling_with_a_shared_prefix_is_not_inside(tmp_path: Path) -> None:
    """`/srv/data-evil` starts with `/srv/data` and is a different directory."""
    root = resolved(tmp_path / "data")
    sibling = resolved(tmp_path / "data-evil")

    assert not is_within(sibling, root)


# -- reading what the client advertised --------------------------------------


def test_only_file_urls_become_roots(tmp_path: Path) -> None:
    """The protocol says roots are `file://`; anything else is not a directory."""
    roots = roots_from_uris([as_uri(tmp_path), "https://example.invalid/x", "not a uri"])

    assert roots == (resolved(tmp_path),)


def test_a_percent_encoded_root_is_decoded(tmp_path: Path) -> None:
    directory = tmp_path / "with space"
    directory.mkdir()

    assert roots_from_uris([as_uri(directory)]) == (resolved(directory),)


def test_a_root_on_another_host_is_not_a_local_directory() -> None:
    assert roots_from_uris(["file://elsewhere/srv/data"]) == ()


def test_an_unusable_root_is_dropped_rather_than_raised(tmp_path: Path) -> None:
    """A percent-encoded NUL survives the URL parser and blows up `realpath`.

    Dropping it leaves the caller with the roots that *were* usable — and with
    an empty tuple, which refuses, if that was the only one. An uncaught
    ValueError here would be a traceback where a refusal was expected.
    """
    assert roots_from_uris(["file:///tmp/x%00y"]) == ()
    assert roots_from_uris(["file:///tmp/x%00y", as_uri(tmp_path)]) == (resolved(tmp_path),)


def test_a_relative_root_is_not_resolved_against_the_working_directory() -> None:
    assert roots_from_uris(["file:relative/path"]) == ()


async def test_a_client_without_the_capability_constrains_nothing() -> None:
    assert await advertised_roots(FakeSession(uris=None)) is None


async def test_no_session_at_all_constrains_nothing() -> None:
    """The CLI, and any transport with no back-channel, must keep working."""
    assert await advertised_roots(None) is None


async def test_an_empty_root_list_is_an_answer_not_a_silence() -> None:
    assert await advertised_roots(FakeSession(uris=[])) == ()


async def test_a_client_that_cannot_answer_is_refused_rather_than_ignored() -> None:
    """It declared the capability; a failed answer is not the same as no roots."""
    session = FakeSession(uris=[], failure=RuntimeError("no back-channel"))

    with pytest.raises(NotAllowlisted):
        await advertised_roots(session)


# -- the refusal itself ------------------------------------------------------


def test_no_roots_advertised_leaves_the_configured_directory_alone(tmp_path: Path) -> None:
    require_within_roots(tmp_path / "downloads", None, what="media.fetch")


def test_an_empty_root_list_refuses_the_download(tmp_path: Path) -> None:
    with pytest.raises(NotAllowlisted) as caught:
        require_within_roots(tmp_path / "downloads", [], what="media.fetch")

    assert str(tmp_path / "downloads") in caught.value.message


def test_a_destination_outside_every_root_is_refused_and_named(tmp_path: Path) -> None:
    sanctioned = tmp_path / "sanctioned"
    sanctioned.mkdir()
    downloads = tmp_path / "elsewhere" / "downloads"

    with pytest.raises(NotAllowlisted) as caught:
        require_within_roots(downloads, [resolved(sanctioned)], what="media.fetch")

    # The offending path is named. It is operator-controlled configuration, not
    # anything a stranger wrote, so quoting it is safe.
    assert str(downloads) in caught.value.message
    assert str(sanctioned) in caught.value.message


def test_a_destination_inside_one_of_several_roots_is_allowed(tmp_path: Path) -> None:
    first = resolved(tmp_path / "one")
    second = resolved(tmp_path / "two")

    require_within_roots(tmp_path / "two" / "downloads", [first, second], what="media.fetch")


# -- through the server ------------------------------------------------------


def config(tmp_path: Path, downloads: Path, archive: Path | None = None) -> Path:
    path = tmp_path / "tgai.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "paths": {
                    "state": str(tmp_path / "state"),
                    "sessions": str(tmp_path / "sessions"),
                    "downloads": str(downloads),
                    "uploads": str(tmp_path / "uploads"),
                    "audit_log": str(tmp_path / "audit.jsonl"),
                    "archive": str(archive or tmp_path / "archive.sqlite3"),
                }
            }
        ),
        encoding="utf-8",
    )
    return path


async def call(server: Any, session: Any, name: str, **arguments: Any) -> dict[str, Any]:
    entry = server.get_request_handler("tools/call")
    params = types.CallToolRequestParams(name=name, arguments=arguments)
    result = await entry.handler(SimpleNamespace(session=session), params)
    return json.loads(result.content[0].text)


async def fetch(server: Any, session: Any) -> dict[str, Any]:
    return await call(server, session, "telegram_media_fetch", chat="-4242", message_id=1)


def test_every_local_write_declares_the_path_it_writes(tmp_path: Path) -> None:
    """The whole reason `Operation.local_path` exists.

    Three operations share `local_write` and they do not share a destination:
    checking all of them against `paths.downloads` refuses an archive write for
    a directory it never touches, and lets one through on the strength of a
    directory it never touches either.
    """
    from telegram_ai_cli_mcp.config import PathsConfig

    writes = [op for op in REGISTRY.all() if op.effect is Effect.LOCAL_WRITE]

    assert writes
    for op in writes:
        assert op.local_path in PathsConfig.model_fields, op.name
    assert {op.local_path for op in writes} == {"downloads", "archive"}


async def test_a_download_outside_the_clients_roots_never_reaches_telegram(
    tmp_path: Path,
) -> None:
    """Refused before the account is opened, and refused as an envelope."""
    sanctioned = tmp_path / "sanctioned"
    sanctioned.mkdir()
    downloads = tmp_path / "elsewhere"
    server = build_server(config_path=config(tmp_path, downloads))

    payload = await fetch(server, FakeSession(uris=[as_uri(sanctioned)]))

    assert payload["ok"] is False
    assert payload["error"]["code"] == ErrorCode.FORBIDDEN_BY_ALLOWLIST
    assert str(downloads) in payload["error"]["message"]
    # Nothing was created anywhere: a refusal is not a redirect.
    assert not downloads.exists()
    assert list(sanctioned.iterdir()) == []


async def test_a_client_that_sanctions_the_download_directory_gets_past_the_check(
    tmp_path: Path,
) -> None:
    """It then fails on the account, which is the next check, not this one."""
    downloads = tmp_path / "sanctioned" / "downloads"
    server = build_server(config_path=config(tmp_path, downloads))
    session = FakeSession(uris=[as_uri(tmp_path / "sanctioned")])

    payload = await fetch(server, session)

    # The client really was asked — otherwise this test would pass just as well
    # with the whole check removed.
    assert session.asked
    assert payload["ok"] is False
    assert payload["error"]["code"] == ErrorCode.ACCOUNT_NOT_FOUND


async def test_an_archive_write_is_judged_by_the_archive_path(tmp_path: Path) -> None:
    """Not by the download directory, which it never touches."""
    sanctioned = tmp_path / "sanctioned"
    sanctioned.mkdir()
    archive = tmp_path / "elsewhere" / "archive.sqlite3"
    # The download directory *is* sanctioned; only the archive is not. Checking
    # every local write against paths.downloads would let this through.
    server = build_server(config_path=config(tmp_path, sanctioned / "downloads", archive))

    payload = await call(
        server,
        FakeSession(uris=[as_uri(sanctioned)]),
        "telegram_archive_forget",
        chat_id=4242,
    )

    assert payload["error"]["code"] == ErrorCode.FORBIDDEN_BY_ALLOWLIST
    assert str(archive) in payload["error"]["message"]


async def test_a_sanctioned_archive_is_not_refused_for_the_download_directory(
    tmp_path: Path,
) -> None:
    """And the mirror image: an unsanctioned *download* path is not this op's business."""
    sanctioned = tmp_path / "sanctioned"
    sanctioned.mkdir()
    server = build_server(
        config_path=config(tmp_path, tmp_path / "elsewhere", sanctioned / "archive.sqlite3")
    )

    payload = await call(
        server,
        FakeSession(uris=[as_uri(sanctioned)]),
        "telegram_archive_forget",
        chat_id=4242,
    )

    assert payload["error"]["code"] != ErrorCode.FORBIDDEN_BY_ALLOWLIST
