"""The policy kernel: who may touch which peer, for what.

Three lists — read, write, admin — turned out not to be enough. ``forward``
touches a source and a destination and needs both checked. ``edit`` and
``delete`` depend on the chat, the message and who wrote it. ``create_group``
has no chat id at all when it is planned. ``set_profile`` is scoped to an
account rather than a chat. So permission is expressed per capability, and each
capability names the rule it consults.

Every decision runs in this order, and the order is the point:

1. the hard denylist, which no configuration can override;
2. the profile, which decides whether the class of action exists at all;
3. the capability's own ``deny`` list;
4. the capability's ``allow`` list, read fail-closed except where the config
   deliberately says otherwise (groups may be read by default; private chats
   may not).

The kernel answers questions. It does not perform actions and it does not talk
to Telegram, which is what makes it cheap to test exhaustively — and this is
the one module where exhaustive tests are worth the effort.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto

from .config import HARD_DENIED_PEERS, HARD_DENY_SAVED_MESSAGES, PeerRule, Settings
from .errors import Denylisted, NotAllowlisted, ProfileForbidden


class Capability(StrEnum):
    READ_CHAT = auto()
    READ_DM = auto()
    READ_MEMBERS = auto()
    READ_MEDIA = auto()
    ENUMERATE = auto()
    SEND = auto()
    ADMIN = auto()
    JOIN = auto()
    PROFILE = auto()


#: Capabilities that change something outside this machine.
REMOTE_WRITE_CAPABILITIES = frozenset(
    {Capability.SEND, Capability.ADMIN, Capability.JOIN, Capability.PROFILE}
)


class PeerKind(StrEnum):
    USER = auto()
    GROUP = auto()
    CHANNEL = auto()
    SELF = auto()
    SERVICE = auto()
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class PeerRef:
    """A resolved peer.

    Resolved is deliberate: policy is never decided on a username. Handles are
    reassignable, so a rule written against ``@someone`` would follow whoever
    holds the name today. A username may match an allowlist entry, but only
    after it has been turned into a numeric id that gets recorded and checked
    again at apply time.
    """

    peer_id: int
    kind: PeerKind = PeerKind.UNKNOWN
    username: str | None = None
    title: str | None = None

    @property
    def is_private(self) -> bool:
        return self.kind in {PeerKind.USER, PeerKind.SELF, PeerKind.SERVICE}


def _normalize(entry: int | str) -> str:
    text = str(entry).strip().lower()
    return text.removeprefix("@")


def _matches(rule_entries: list[int | str], peer: PeerRef) -> bool:
    if not rule_entries:
        return False
    candidates = {str(peer.peer_id)}
    if peer.username:
        candidates.add(peer.username.lower())
    return any(_normalize(entry) in candidates for entry in rule_entries)


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str = ""


class SafetyKernel:
    """Answers "may I?" for one configuration."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # -- hard, unconfigurable floor ---------------------------------------

    def _hard_denied(self, peer: PeerRef) -> str | None:
        if peer.peer_id in HARD_DENIED_PEERS or peer.kind is PeerKind.SERVICE:
            return (
                "Telegram Service Notifications carries login codes and 2FA resets. "
                "This chat is closed in code and cannot be opened by configuration."
            )
        if HARD_DENY_SAVED_MESSAGES and peer.kind is PeerKind.SELF:
            return (
                "Saved Messages is where people keep passwords and documents. "
                "This chat is closed in code and cannot be opened by configuration."
            )
        return None

    # -- profile -----------------------------------------------------------

    def _profile_allows(self, capability: Capability) -> bool:
        if capability in REMOTE_WRITE_CAPABILITIES:
            # Even in `plan`, a remote write is only ever planned here. Applying
            # is a separate command; no profile turns this into a direct send.
            return self._settings.profile == "plan"
        return True

    # -- per-capability rules ---------------------------------------------

    def _rule_for(self, capability: Capability) -> tuple[PeerRule, bool]:
        """Return the rule and whether an empty ``allow`` means "everything"."""
        safety = self._settings.safety
        match capability:
            case Capability.READ_CHAT:
                return safety.read.chats, True
            case Capability.READ_DM:
                # The one place where empty means none: private conversations
                # are not readable until they are named.
                return safety.read.dms, False
            case Capability.READ_MEMBERS:
                return safety.read.members, True
            case Capability.READ_MEDIA:
                return safety.read.media, True
            case Capability.SEND:
                return safety.write.send, False
            case Capability.ADMIN:
                return safety.write.admin, False
            case Capability.JOIN:
                return safety.write.join, False
            case _:
                return PeerRule(), False

    def check(self, capability: Capability, peer: PeerRef) -> Decision:
        """The whole decision, in the documented order."""
        if (reason := self._hard_denied(peer)) is not None:
            return Decision(False, reason)

        if not self._profile_allows(capability):
            return Decision(
                False,
                f"profile {self._settings.profile!r} does not permit {capability}; "
                "switch to 'plan' to prepare it for review",
            )

        # A private chat is judged as a private chat regardless of which read
        # tool asked, so a members or media lookup cannot be the way in.
        if peer.is_private and capability in {
            Capability.READ_CHAT,
            Capability.READ_MEMBERS,
            Capability.READ_MEDIA,
        }:
            capability = Capability.READ_DM

        rule, empty_allows_all = self._rule_for(capability)

        if _matches(rule.deny, peer):
            return Decision(False, f"{capability}: chat is on the deny list")

        if not rule.allow:
            if empty_allows_all:
                return Decision(True)
            return Decision(
                False,
                f"{capability}: allow list is empty, which means nothing is permitted. "
                "Add the chat to the configuration to permit it.",
            )

        if _matches(rule.allow, peer):
            return Decision(True)
        return Decision(False, f"{capability}: chat is not on the allow list")

    def require(self, capability: Capability, peer: PeerRef) -> None:
        """Raise the right refusal, or return quietly."""
        decision = self.check(capability, peer)
        if decision.allowed:
            return
        if "deny list" in decision.reason or "closed in code" in decision.reason:
            raise Denylisted(decision.reason)
        if "profile" in decision.reason:
            raise ProfileForbidden(decision.reason)
        raise NotAllowlisted(decision.reason)

    # -- capabilities without a peer ---------------------------------------

    def require_profile_change(self) -> None:
        if not self._profile_allows(Capability.PROFILE):
            raise ProfileForbidden("profile changes require the 'plan' profile")
        if not self._settings.safety.write.profile_enabled:
            raise NotAllowlisted(
                "profile changes are disabled; set safety.write.profile_enabled to allow them"
            )

    def require_group_creation(self) -> None:
        if not self._profile_allows(Capability.ADMIN):
            raise ProfileForbidden("creating a group requires the 'plan' profile")

    def require_enumeration(self, *, private: bool) -> None:
        read = self._settings.safety.read
        if not read.allow_dialog_enumeration:
            raise NotAllowlisted("dialog enumeration is disabled in the configuration")
        if private and not read.enumerate_dms:
            raise NotAllowlisted(
                "private chats are excluded from enumeration; "
                "set safety.read.enumerate_dms to include them"
            )

    def account_allowed(self, label: str) -> bool:
        rule = self._settings.safety.accounts
        entries = {_normalize(entry) for entry in rule.deny}
        if label.lower() in entries:
            return False
        if not rule.allow:
            return True
        return label.lower() in {_normalize(entry) for entry in rule.allow}
