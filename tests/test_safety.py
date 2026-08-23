"""The policy kernel, exercised as a matrix rather than by example.

Every assertion here is about a decision a person would otherwise have to
re-derive by reading ``safety.py``: which empty list means "everything", which
means "nothing", and what a profile is allowed to unlock. The kernel touches no
network and no account, so these run everywhere and are cheap enough to be
exhaustive — which is the point, because a permission bug is invisible until it
is exploited.
"""

from __future__ import annotations

import os

import pytest

from telegram_ai_cli.config import Settings, load_settings
from telegram_ai_cli.errors import (
    Denylisted,
    ErrorCode,
    NotAllowlisted,
    PolicyError,
    ProfileForbidden,
)
from telegram_ai_cli.safety import (
    REMOTE_WRITE_CAPABILITIES,
    Capability,
    PeerKind,
    PeerRef,
    SafetyKernel,
)

# Deliberately small, obviously fake ids: nothing here should resemble a real
# chat, and the repository scan in test_no_private_data.py stays clean.
GROUP = PeerRef(peer_id=-4242, kind=PeerKind.GROUP, title="A group")
OTHER_GROUP = PeerRef(peer_id=-4343, kind=PeerKind.GROUP, title="Another group")
CHANNEL = PeerRef(peer_id=-4444, kind=PeerKind.CHANNEL, title="A channel")
USER = PeerRef(peer_id=555, kind=PeerKind.USER, username="Someone")


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """No ``TGAI_`` variable from the developer's shell may steer a decision.

    ``Settings`` is a ``BaseSettings``: an exported override would silently
    become part of the fixture and the test would assert the wrong thing.
    """
    for key in list(os.environ):
        if key.startswith("TGAI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def kernel(**overrides) -> SafetyKernel:
    return SafetyKernel(Settings(**overrides))


# --- defaults --------------------------------------------------------------


def test_default_profile_is_readonly() -> None:
    assert Settings().profile == "readonly"


def test_write_allow_lists_start_empty() -> None:
    """Fail-closed is a property of the defaults, not only of the kernel."""
    write = Settings().safety.write
    assert write.send.allow == []
    assert write.admin.allow == []
    assert write.join.allow == []
    assert write.profile_enabled is False


def test_state_paths_follow_xdg_state_home(tmp_path) -> None:
    """Nothing is written to the real home directory, in tests or in use."""
    paths = Settings().paths
    root = tmp_path / "state" / "telegram-ai-cli"
    assert paths.state == root
    assert paths.sessions == root / "sessions"
    assert paths.audit_log == root / "audit.jsonl"


# --- reading ---------------------------------------------------------------


@pytest.mark.parametrize("peer", [GROUP, CHANNEL])
def test_empty_allow_list_permits_reading_groups_and_channels(peer: PeerRef) -> None:
    """A reader that can see nothing until configured gets configured carelessly."""
    assert kernel().check(Capability.READ_CHAT, peer).allowed is True


def test_empty_allow_list_forbids_reading_private_chats() -> None:
    """The owner's decision: private correspondence is opt-in, one chat at a time."""
    decision = kernel().check(Capability.READ_DM, USER)
    assert decision.allowed is False
    assert "allow list is empty" in decision.reason


def test_private_chat_is_readable_once_listed() -> None:
    allowed = kernel(safety={"read": {"dms": {"allow": [USER.peer_id]}}})
    assert allowed.check(Capability.READ_DM, USER).allowed is True
    stranger = PeerRef(peer_id=556, kind=PeerKind.USER)
    assert allowed.check(Capability.READ_DM, stranger).allowed is False


def test_username_entries_match_case_insensitively_and_without_the_at_sign() -> None:
    by_handle = kernel(safety={"read": {"dms": {"allow": ["@SOMEONE"]}}})
    assert by_handle.check(Capability.READ_DM, USER).allowed is True


@pytest.mark.parametrize("capability", [Capability.READ_MEMBERS, Capability.READ_MEDIA])
def test_private_chat_rules_apply_to_members_and_media(capability: Capability) -> None:
    """Otherwise the members or media tool is the way around the DM allowlist.

    The peer is on the ``members``/``media`` allowlist and still refused,
    because what decides is the kind of chat, not the tool that asked.
    """
    permissive = kernel(
        safety={
            "read": {
                "members": {"allow": [USER.peer_id]},
                "media": {"allow": [USER.peer_id]},
            }
        }
    )
    decision = permissive.check(capability, USER)
    assert decision.allowed is False
    assert str(Capability.READ_DM) in decision.reason


@pytest.mark.parametrize("capability", [Capability.READ_MEMBERS, Capability.READ_MEDIA])
def test_group_members_and_media_stay_readable_by_default(capability: Capability) -> None:
    assert kernel().check(capability, GROUP).allowed is True


def test_saved_messages_kind_counts_as_private() -> None:
    assert PeerRef(peer_id=1, kind=PeerKind.SELF).is_private is True
    assert PeerRef(peer_id=1, kind=PeerKind.SERVICE).is_private is True
    assert PeerRef(peer_id=1, kind=PeerKind.GROUP).is_private is False


# --- deny beats allow ------------------------------------------------------


def test_deny_overrides_allow_for_reads() -> None:
    both = kernel(safety={"read": {"chats": {"allow": [GROUP.peer_id], "deny": [GROUP.peer_id]}}})
    decision = both.check(Capability.READ_CHAT, GROUP)
    assert decision.allowed is False
    assert "deny list" in decision.reason


def test_deny_overrides_allow_for_writes() -> None:
    both = kernel(
        profile="plan",
        safety={"write": {"send": {"allow": [GROUP.peer_id], "deny": [GROUP.peer_id]}}},
    )
    assert both.check(Capability.SEND, GROUP).allowed is False


def test_deny_by_username_also_wins() -> None:
    denied = kernel(safety={"read": {"dms": {"allow": [USER.peer_id], "deny": ["someone"]}}})
    assert denied.check(Capability.READ_DM, USER).allowed is False


# --- profiles --------------------------------------------------------------


@pytest.mark.parametrize("capability", sorted(REMOTE_WRITE_CAPABILITIES))
def test_readonly_profile_forbids_every_remote_write(capability: Capability) -> None:
    """`readonly` is the default, and it never reaches Telegram with a change."""
    permissive = kernel(
        safety={
            "write": {
                "send": {"allow": [GROUP.peer_id]},
                "admin": {"allow": [GROUP.peer_id]},
                "join": {"allow": [GROUP.peer_id]},
                "profile_enabled": True,
            }
        }
    )
    decision = permissive.check(capability, GROUP)
    assert decision.allowed is False
    assert "readonly" in decision.reason


@pytest.mark.parametrize("capability", sorted(REMOTE_WRITE_CAPABILITIES))
def test_plan_profile_still_needs_an_allow_list(capability: Capability) -> None:
    decision = kernel(profile="plan").check(capability, GROUP)
    assert decision.allowed is False
    # Refused by the empty allow list, not by the profile: `plan` unlocks the
    # class of action, it does not name a single chat.
    assert "allow list is empty" in decision.reason


@pytest.mark.parametrize(
    ("capability", "section"),
    [
        (Capability.SEND, "send"),
        (Capability.ADMIN, "admin"),
        (Capability.JOIN, "join"),
    ],
)
def test_plan_profile_permits_planning_for_a_listed_chat(
    capability: Capability, section: str
) -> None:
    planner = kernel(profile="plan", safety={"write": {section: {"allow": [GROUP.peer_id]}}})
    assert planner.check(capability, GROUP).allowed is True
    assert planner.check(capability, OTHER_GROUP).allowed is False


def test_readonly_profile_does_not_restrict_reading() -> None:
    assert kernel().check(Capability.READ_CHAT, GROUP).allowed is True


# --- refusals are typed ----------------------------------------------------


def test_require_raises_the_error_that_matches_the_reason() -> None:
    with pytest.raises(ProfileForbidden) as profile:
        kernel().require(Capability.SEND, GROUP)
    assert profile.value.code is ErrorCode.FORBIDDEN_BY_PROFILE

    with pytest.raises(NotAllowlisted) as allowlist:
        kernel().require(Capability.READ_DM, USER)
    assert allowlist.value.code is ErrorCode.FORBIDDEN_BY_ALLOWLIST

    denied = kernel(safety={"read": {"chats": {"deny": [GROUP.peer_id]}}})
    with pytest.raises(Denylisted) as denylist:
        denied.require(Capability.READ_CHAT, GROUP)
    assert denylist.value.code is ErrorCode.FORBIDDEN_BY_DENYLIST


def test_policy_refusals_are_never_retryable() -> None:
    """Retrying a refusal is either confusion or an attempt to wear it down."""
    for error in (ProfileForbidden("x"), NotAllowlisted("x"), Denylisted("x")):
        assert isinstance(error, PolicyError)
        assert error.retryable is False
        assert error.to_dict()["retryable"] is False


def test_require_is_silent_when_permitted() -> None:
    assert kernel().require(Capability.READ_CHAT, GROUP) is None


# --- capabilities without a peer -------------------------------------------


def test_enumeration_hides_private_chats_unless_asked_for() -> None:
    default = kernel()
    assert default.require_enumeration(private=False) is None
    with pytest.raises(NotAllowlisted, match="enumerate_dms"):
        default.require_enumeration(private=True)

    with_dms = kernel(safety={"read": {"enumerate_dms": True}})
    assert with_dms.require_enumeration(private=True) is None


def test_enumeration_can_be_switched_off_entirely() -> None:
    off = kernel(safety={"read": {"allow_dialog_enumeration": False}})
    with pytest.raises(NotAllowlisted):
        off.require_enumeration(private=False)


def test_profile_change_needs_both_the_plan_profile_and_the_switch() -> None:
    with pytest.raises(ProfileForbidden):
        kernel().require_profile_change()
    with pytest.raises(NotAllowlisted):
        kernel(profile="plan").require_profile_change()
    enabled = kernel(profile="plan", safety={"write": {"profile_enabled": True}})
    assert enabled.require_profile_change() is None


def test_group_creation_needs_the_plan_profile() -> None:
    with pytest.raises(ProfileForbidden):
        kernel().require_group_creation()
    assert kernel(profile="plan").require_group_creation() is None


def test_account_allow_and_deny() -> None:
    assert kernel().account_allowed("work") is True
    denied = kernel(safety={"accounts": {"deny": ["work"]}})
    assert denied.account_allowed("WORK") is False
    listed = kernel(safety={"accounts": {"allow": ["other"]}})
    assert listed.account_allowed("work") is False
    assert listed.account_allowed("Other") is True


# --- configuration loading -------------------------------------------------


def test_yaml_overrides_defaults_and_tolerates_empty_sections(tmp_path) -> None:
    """A section with every key commented out parses as ``None``.

    That is the normal shape of an example config; it must fall back to the
    defaults rather than fail validation.
    """
    config = tmp_path / "tgai.yaml"
    config.write_text(
        "profile: plan\nsafety:\ndownload:\n",
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.profile == "plan"
    assert settings.safety.read.dms.allow == []
    assert settings.download.max_file_bytes > 0


def test_missing_config_file_is_not_an_error(tmp_path) -> None:
    settings = load_settings(tmp_path / "absent.yaml")
    assert settings.profile == "readonly"
