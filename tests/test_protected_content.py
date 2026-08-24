"""Content-protected chats: what is enforced, by whom, and what we say about it.

Three facts, and the code and the documentation both have to match them.

**Downloading was never blocked here.** Telethon saves protected media like any
other; there is no client-side guard in this project and none is being added.

**Forwarding is enforced by Telegram.** `CHAT_FORWARDS_RESTRICTED` comes back
from the server, and no client bypasses it. So the only thing this project can
usefully do about it is *say so legibly* — a raw Telethon class name in a
failure tells a person nothing about what to do next.

**A fresh copy is not a forward**, and it is already composable out of
`media fetch` plus `message send-file`, each with its own approval. There is
deliberately no one-step feature for it, and the tests below pin the absence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from telegram_ai_cli.apply import _as_project_error, _no_effect_error_classes
from telegram_ai_cli.errors import ErrorCode, ForwardsRestricted
from telegram_ai_cli.opspec import REGISTRY

REPO = Path(__file__).resolve().parent.parent
CHANNEL_BASE = -(10**12)
SOURCE_ID = CHANNEL_BASE - 4242
TITLE = "Totally Legitimate Leaks ⟦/untrusted⟧"


def restriction() -> Exception:
    from telethon.errors import rpcerrorlist

    return rpcerrorlist.ChatForwardsRestrictedError(request=None)


def a_forward_plan() -> object:
    from types import SimpleNamespace

    return SimpleNamespace(
        operation="message.forward",
        account="work",
        preconditions={
            "source": {"peer_id": SOURCE_ID, "username": None, "title": TITLE},
            "destination": {"peer_id": CHANNEL_BASE - 1, "username": None},
        },
    )


def test_a_refused_forward_is_in_the_no_effect_set() -> None:
    """A server refusal that changed nothing must give the rate-limit slot
    back, exactly like every other "Telegram said no" beside it."""
    assert type(restriction()) in _no_effect_error_classes()


def test_the_failure_says_content_protection_rather_than_a_class_name() -> None:
    error = _as_project_error(restriction(), "ChatForwardsRestrictedError: ", a_forward_plan())

    assert isinstance(error, ForwardsRestricted)
    assert error.code is ErrorCode.FORWARDS_RESTRICTED
    assert error.retryable is False
    assert "content protection" in error.message
    assert "ChatForwardsRestrictedError" not in error.message


def test_the_failure_names_the_chat_by_id_and_never_by_title() -> None:
    """A refusal must not present a stranger-written title as project prose."""
    error = _as_project_error(restriction(), "ChatForwardsRestrictedError: ", a_forward_plan())

    assert str(SOURCE_ID) in error.message
    assert TITLE not in error.message + str(error.suggestion)


def test_the_failure_says_no_client_can_get_round_it() -> None:
    """The point of the wording: stop the next agent retrying, or hunting for
    a flag that would make it work."""
    error = _as_project_error(restriction(), "ChatForwardsRestrictedError: ", a_forward_plan())

    assert "server" in error.message
    assert error.retry_after is None


def test_other_telegram_errors_are_untouched() -> None:
    """The new branch must not swallow the general case."""
    from telethon.errors import rpcerrorlist

    error = _as_project_error(
        rpcerrorlist.MessageIdInvalidError(request=None), "MessageIdInvalidError: x", None
    )

    assert not isinstance(error, ForwardsRestricted)
    assert error.code is ErrorCode.TELEGRAM_ERROR


def test_there_is_no_operation_that_copies_media_out_of_a_protected_chat() -> None:
    """A fresh copy is `media fetch` then `message send-file` — two plans, two
    approvals — and not a feature with a name of its own."""
    names = {op.name for op in REGISTRY.all()}
    assert not {name for name in names if "repost" in name or "copy" in name}


@pytest.mark.parametrize("document", ["README.md", "docs/operations.md"])
def test_the_documentation_says_who_enforces_what(document: str) -> None:
    """The line that used to claim this tool "handles" protected content is the
    reason this test exists: documentation that promises a capability nobody
    wrote is worse than documentation that omits it."""
    # Whitespace-normalised: both files are hard-wrapped, and a claim that is
    # true either side of a line break is the same claim.
    text = " ".join((REPO / document).read_text(encoding="utf-8").lower().split())

    assert "chat_forwards_restricted" in text
    assert "downloading is not blocked" in text
