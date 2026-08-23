"""Two chats that no configuration may open.

``777000`` is Telegram Service Notifications: login codes and 2FA resets arrive
there, so reading it hands over the account. Saved Messages is where people keep
passwords and documents precisely because it feels private.

The test that matters is not "are they refused by default" — it is "are they
still refused after someone has tried to permit them". Everything below builds
the most permissive configuration the schema accepts, puts both peers on every
allow list, and expects a refusal anyway.
"""

from __future__ import annotations

import os

import pytest

from telegram_ai_cli.config import HARD_DENIED_PEERS, Settings
from telegram_ai_cli.errors import Denylisted, ErrorCode
from telegram_ai_cli.safety import Capability, PeerKind, PeerRef, SafetyKernel

SERVICE_ID = 777000

SERVICE = PeerRef(peer_id=SERVICE_ID, kind=PeerKind.SERVICE, title="Telegram")
#: The same peer as an ordinary user account — the id alone must be enough.
SERVICE_AS_USER = PeerRef(peer_id=SERVICE_ID, kind=PeerKind.USER, username="Telegram")
#: And with nothing resolved at all, which is how an unknown id arrives.
SERVICE_UNRESOLVED = PeerRef(peer_id=SERVICE_ID)
SAVED_MESSAGES = PeerRef(peer_id=1234, kind=PeerKind.SELF, title="Saved Messages")

CLOSED_PEERS = [SERVICE, SERVICE_AS_USER, SERVICE_UNRESOLVED, SAVED_MESSAGES]

EVERY_CAPABILITY = sorted(Capability)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for key in list(os.environ):
        if key.startswith("TGAI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def most_permissive_kernel() -> SafetyKernel:
    """Everything a determined operator could switch on, switched on."""
    targets = [SERVICE_ID, "telegram", SAVED_MESSAGES.peer_id, "saved messages"]
    return SafetyKernel(
        Settings(
            profile="plan",
            safety={
                "read": {
                    "chats": {"allow": targets},
                    "dms": {"allow": targets},
                    "members": {"allow": targets},
                    "media": {"allow": targets},
                    "allow_dialog_enumeration": True,
                    "enumerate_dms": True,
                },
                "write": {
                    "send": {"allow": targets},
                    "admin": {"allow": targets},
                    "join": {"allow": targets},
                    "profile_enabled": True,
                },
            },
        )
    )


@pytest.mark.parametrize("peer", CLOSED_PEERS, ids=lambda p: f"{p.kind}-{p.peer_id}")
@pytest.mark.parametrize("capability", EVERY_CAPABILITY, ids=str)
def test_no_configuration_opens_the_closed_chats(peer: PeerRef, capability: Capability) -> None:
    decision = most_permissive_kernel().check(capability, peer)
    assert decision.allowed is False
    assert "cannot be opened by configuration" in decision.reason


@pytest.mark.parametrize("peer", CLOSED_PEERS, ids=lambda p: f"{p.kind}-{p.peer_id}")
def test_refusal_is_a_denylist_error(peer: PeerRef) -> None:
    with pytest.raises(Denylisted) as refusal:
        most_permissive_kernel().require(Capability.READ_CHAT, peer)
    assert refusal.value.code is ErrorCode.FORBIDDEN_BY_DENYLIST
    assert refusal.value.retryable is False


def test_the_hard_denial_outranks_the_profile() -> None:
    """A read refusal here must not be reported as "switch profiles"."""
    readonly = SafetyKernel(Settings())
    decision = readonly.check(Capability.SEND, SERVICE)
    assert decision.allowed is False
    assert "cannot be opened by configuration" in decision.reason


def test_environment_variables_cannot_reopen_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """`TGAI_`-prefixed overrides reach every other setting. Not these."""
    monkeypatch.setenv("TGAI_HARD_DENIED_PEERS", "[]")
    monkeypatch.setenv("TGAI_HARD_DENY_SAVED_MESSAGES", "false")
    kernel = SafetyKernel(Settings())
    assert kernel.check(Capability.READ_CHAT, SERVICE).allowed is False
    assert kernel.check(Capability.READ_CHAT, SAVED_MESSAGES).allowed is False


def test_the_closed_list_is_a_constant_not_a_setting() -> None:
    assert isinstance(HARD_DENIED_PEERS, frozenset)
    assert SERVICE_ID in HARD_DENIED_PEERS
    assert not hasattr(Settings(), "hard_denied_peers")
    with pytest.raises(AttributeError):
        HARD_DENIED_PEERS.add(1)  # type: ignore[attr-defined]


def test_an_ordinary_chat_with_a_similar_id_is_unaffected() -> None:
    """The floor is exact ids, not a prefix or a range."""
    neighbour = PeerRef(peer_id=SERVICE_ID + 1, kind=PeerKind.GROUP)
    assert SafetyKernel(Settings()).check(Capability.READ_CHAT, neighbour).allowed is True
