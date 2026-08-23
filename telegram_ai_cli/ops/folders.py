"""``telegram_folders`` — the sorting the user already did, by hand.

Telegram's chat folders are the one grouping in this system that a *person*
authored. Someone sat down and decided that these chats are work, those are
family, and that the muted ones do not belong in either. That is a better
ordering than anything an agent can infer from titles and unread counts, and it
is already there — so it is read back rather than reinvented, and offered as a
filter on the two operations that list dialogs.

Three decisions are worth stating before the code.

**A folder is not a permission.** A folder is a list of chats a user wrote, and
it can name anything the account can see — Saved Messages, Service
Notifications, private conversations this configuration does not enumerate.
Filtering therefore runs *last*, over the rows a listing already decided it may
show; it can only ever remove rows, never add one. The same rule applies to the
folder listing itself: a peer id inside a folder is hidden when the floor or the
DM-enumeration switch would hide the chat.

**Membership is decided the way Telegram decides it, and it is decided here.**
There is no server call that answers "is this dialog in that folder": clients
apply the rules themselves. The rules are two peer lists plus a set of flags,
and getting one branch wrong means quietly returning the wrong chats — so the
whole decision is a pure function over plain facts, and every branch of it has a
test.

**Names are read by attribute, never by class.** A folder title is a plain
string in older layers and a ``TextWithEntities`` in newer ones, a shareable
folder (``DialogFilterChatlist``) carries no flags at all, and "All chats"
(``DialogFilterDefault``) carries nothing whatsoever. Reading attributes with
defaults means a layer this Telethon version does not know about degrades to a
folder with fewer rules rather than raising.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from ..context import OperationContext
from ..envelope import Envelope
from ..errors import InvalidInput, NotFound, TelegramError
from ..opspec import REGISTRY, Effect, Operation
from ..safety import Capability, PeerKind, PeerRef
from ._client import open_account
from ._common import (
    ReadInput,
    hard_denied,
    require_enumeration,
    telegram_errors,
    telegram_result,
)

#: How Telethon marks a channel id, restated rather than imported: the peer
#: lists inside a folder are ``InputPeer`` objects, and turning one into the id
#: a listing prints must produce exactly what ``utils.get_peer_id`` produces or
#: the two sides never match. Pinned against the real implementation by a test.
#: Written as a power of ten rather than as the literal, which is shaped exactly
#: like a real chat id and trips the repository's own private-data scan.
_CHANNEL_ID_BASE = -(10**12)


@dataclass(frozen=True, slots=True)
class FolderFlags:
    """The whole-category rules a folder can carry.

    Two groups with different meanings. The first five *admit* a category of
    chat; the last three *withhold* a chat that was admitted. All default to
    ``False``, which is what makes a shareable folder — which has none of them —
    safe to parse with the same code: it admits nothing it was not given.
    """

    contacts: bool = False
    non_contacts: bool = False
    groups: bool = False
    broadcasts: bool = False
    bots: bool = False
    exclude_muted: bool = False
    exclude_read: bool = False
    exclude_archived: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "contacts": self.contacts,
            "non_contacts": self.non_contacts,
            "groups": self.groups,
            "broadcasts": self.broadcasts,
            "bots": self.bots,
            "exclude_muted": self.exclude_muted,
            "exclude_read": self.exclude_read,
            "exclude_archived": self.exclude_archived,
        }


@dataclass(frozen=True, slots=True)
class DialogFacts:
    """Everything a folder rule can ask about one dialog, and nothing else.

    A plain struct rather than the Telethon dialog, so the membership rule is a
    pure function: it is the part most likely to be subtly wrong, and this is
    what makes every branch of it testable without a Telegram account.
    """

    chat_id: int
    kind: PeerKind
    bot: bool = False
    contact: bool = False
    archived: bool = False
    muted: bool = False
    unread: int = 0
    mentions: int = 0
    unread_mark: bool = False
    """The user pressed "mark as unread" — a statement, not a counter."""

    @property
    def waiting(self) -> bool:
        """Whether Telegram considers this chat unread at all.

        Three separate facts, and a folder's ``exclude_read`` has to honour all
        of them: an unread message, an unread mention, and the flag a person
        sets by hand. Counting only the first drops a chat the user themselves
        marked unread — which is the one they most deliberately said is not
        finished.
        """
        return self.unread > 0 or self.mentions > 0 or self.unread_mark


@dataclass(frozen=True, slots=True)
class FolderView:
    """One folder, as this tool describes it."""

    id: int
    title: str | None
    emoticon: str | None
    shareable: bool
    flags: FolderFlags
    include: frozenset[int]
    exclude: frozenset[int]
    pinned: frozenset[int]
    opaque_peers: int = 0
    """Peers this folder names that carry no id of their own — ``InputPeerSelf``
    and ``InputPeerEmpty``. Counted rather than dropped: a folder that names
    Saved Messages must report *something*, or a hidden chat and a chat that was
    never there look identical."""

    def contains(self, facts: DialogFacts) -> bool:
        """Whether this dialog belongs to the folder.

        Order is the rule, not an implementation detail:

        1. an excluded chat is out, whatever else says otherwise;
        2. a chat the user *named* is in — naming one chat is a more specific
           statement than any category flag, so ``exclude_muted`` does not
           withhold a muted chat that was pinned into the folder by hand;
        3. otherwise a category flag has to admit it, and then the three
           withholding flags get their say.

        Two of those three are narrower than their names suggest, and the
        official clients implement both exceptions: a muted chat with an unread
        *mention* is not withheld by ``exclude_muted`` — muting a group is a
        statement about its chatter, not about being addressed by name — and
        ``exclude_read`` honours the "mark as unread" flag as well as the
        counters.
        """
        if facts.chat_id in self.exclude:
            return False
        if facts.chat_id in self.include or facts.chat_id in self.pinned:
            return True
        if not self._kind_admitted(facts):
            return False
        if self.flags.exclude_muted and facts.muted and not facts.mentions:
            return False
        if self.flags.exclude_read and not facts.waiting:
            return False
        return not (self.flags.exclude_archived and facts.archived)

    def _kind_admitted(self, facts: DialogFacts) -> bool:
        if facts.kind is PeerKind.GROUP:
            return self.flags.groups
        if facts.kind is PeerKind.CHANNEL:
            return self.flags.broadcasts
        if facts.kind is PeerKind.USER:
            # Telegram counts bots as their own category, so a bot in the
            # address book must not arrive through the `contacts` flag.
            if facts.bot:
                return self.flags.bots
            return self.flags.contacts if facts.contact else self.flags.non_contacts
        # Saved Messages, Service Notifications and anything unrecognised. The
        # first two are closed in code anyway; the third has no rule to consult.
        return False


# --- reading what Telegram returned -----------------------------------------


def input_peer_id(peer: Any) -> int | None:
    """The marked id of a peer named inside a folder, or ``None``.

    ``None`` is not a failure and it is not nothing: ``InputPeerSelf`` names the
    account itself, which is Saved Messages — closed in code — and
    ``InputPeerEmpty`` names nothing at all. Neither can be turned into an id
    without knowing who this account is, so the caller counts them (see
    ``FolderView.opaque_peers``) rather than pretending they were not there.
    """
    user_id = getattr(peer, "user_id", None)
    if user_id is not None:
        return int(user_id)
    channel_id = getattr(peer, "channel_id", None)
    if channel_id is not None:
        return _CHANNEL_ID_BASE - int(channel_id)
    chat_id = getattr(peer, "chat_id", None)
    if chat_id is not None:
        return -int(chat_id)
    return None


def _peer_ids(peers: Any) -> tuple[frozenset[int], int]:
    """The ids of the peers named here, and how many carried no id."""
    ids: set[int] = set()
    opaque = 0
    for peer in peers or []:
        peer_id = input_peer_id(peer)
        if peer_id is None:
            opaque += 1
        else:
            ids.add(peer_id)
    return frozenset(ids), opaque


def _title_of(value: Any) -> str | None:
    """A folder title, which newer layers wrap in ``TextWithEntities``."""
    if value is None:
        return None
    text = getattr(value, "text", value)
    return str(text) if text else None


def parse_folders(result: Any) -> list[FolderView]:
    """Turn the ``GetDialogFilters`` answer into views.

    Accepts both shapes Telethon has returned: the bare list of filters, and the
    newer ``messages.DialogFilters`` envelope around it. "All chats" is skipped
    — it has no id and no rules, and offering it as a folder would offer a
    filter that filters nothing.
    """
    filters = getattr(result, "filters", result) or []
    views: list[FolderView] = []
    for item in filters:
        folder_id = getattr(item, "id", None)
        if folder_id is None:
            continue
        # A shareable folder carries no flags at all, which is exactly what
        # "admits nothing it was not given" needs the defaults to say.
        shareable = not hasattr(item, "contacts")
        views.append(
            FolderView(
                id=int(folder_id),
                title=_title_of(getattr(item, "title", None)),
                emoticon=getattr(item, "emoticon", None) or None,
                shareable=shareable,
                flags=FolderFlags(
                    contacts=bool(getattr(item, "contacts", False)),
                    non_contacts=bool(getattr(item, "non_contacts", False)),
                    groups=bool(getattr(item, "groups", False)),
                    broadcasts=bool(getattr(item, "broadcasts", False)),
                    bots=bool(getattr(item, "bots", False)),
                    exclude_muted=bool(getattr(item, "exclude_muted", False)),
                    exclude_read=bool(getattr(item, "exclude_read", False)),
                    exclude_archived=bool(getattr(item, "exclude_archived", False)),
                ),
                **_peer_lists(item),
            )
        )
    return views


def _peer_lists(item: Any) -> dict[str, Any]:
    """The three peer lists of one filter, plus the peers that had no id."""
    include, include_opaque = _peer_ids(getattr(item, "include_peers", None))
    exclude, exclude_opaque = _peer_ids(getattr(item, "exclude_peers", None))
    pinned, pinned_opaque = _peer_ids(getattr(item, "pinned_peers", None))
    return {
        "include": include,
        "exclude": exclude,
        "pinned": pinned,
        "opaque_peers": include_opaque + exclude_opaque + pinned_opaque,
    }


async def load_folders(client: Any, *, what: str) -> list[FolderView]:
    """Fetch this account's folders. One call, and it changes nothing."""
    from telethon.tl.functions.messages import GetDialogFiltersRequest

    with telegram_errors(what=what):
        result = await client(GetDialogFiltersRequest())
    return parse_folders(result)


# --- choosing one -----------------------------------------------------------


def resolve_folder(views: list[FolderView], token: str) -> FolderView:
    """Find the folder the caller meant, refusing to guess between two.

    A folder id is stable and a name is not, so both are accepted — a person
    types the name they see, and a script pins the id. What no error here does
    is quote a title back: those are written on someone's own account, and an
    error message is the one path out of this tool that does not pass through
    the untrusted-content boundary a payload does.
    """
    text = (token or "").strip()
    if not text:
        raise InvalidInput("folder: pass a folder id, or the name shown in Telegram")
    if not views:
        raise NotFound(
            "folder: this account has no chat folders",
            suggestion="Folders are created in Telegram itself; there are none to filter by.",
        )

    numeric = text.lstrip("-").isdigit()
    if numeric:
        wanted = int(text)
        for view in views:
            if view.id == wanted:
                return view

    needle = text.casefold()
    exact = [view for view in views if (view.title or "").casefold() == needle]
    if numeric:
        # A digit that matched no id may still be a folder *named* "2" —
        # Telegram accepts any title. Only an exact title, though: letting a
        # number match a substring would quietly pick "Work 2024".
        matches = exact
    else:
        # An exact name wins over a longer one containing it, or naming a folder
        # precisely would be ambiguous with its own neighbour.
        matches = exact or [view for view in views if needle in (view.title or "").casefold()]

    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise NotFound(
            "folder: this account has no folder with that id or name",
            suggestion=f"This account has {len(views)} folder(s); `folders` lists them.",
        )
    raise InvalidInput(
        "folder: that name matches several folders; pass the id instead",
        details={"folder_ids": sorted(view.id for view in matches)},
    )


async def folder_for(client: Any, token: str, *, what: str) -> FolderView:
    """The folder a listing was asked to narrow itself to."""
    return resolve_folder(await load_folders(client, what=what), token)


# --- facts about a dialog ---------------------------------------------------


def dialog_is_muted(dialog: Any) -> bool:
    """Whether the user silenced this chat on their own devices.

    Muting is the user's own statement that a conversation is not urgent, which
    is why both the inbox and the ``exclude_muted`` folder flag ask it. Telegram
    stores a mute as an *expiry*, and a past expiry means the chat is audible
    again — treating any value as "muted" hides conversations that stopped being
    muted months ago.
    """
    raw = getattr(dialog, "dialog", None)
    notify = getattr(raw, "notify_settings", None)
    if notify is None:
        return False
    if getattr(notify, "silent", False):
        return True
    until = getattr(notify, "mute_until", None)
    if isinstance(until, datetime):
        return until > datetime.now(tz=until.tzinfo or UTC)
    return bool(until)


def facts_of(dialog: Any, entity: Any, ref: PeerRef) -> DialogFacts:
    """Everything the folder rules may ask about this dialog."""
    raw = getattr(dialog, "dialog", None)
    return DialogFacts(
        chat_id=ref.peer_id,
        kind=ref.kind,
        bot=bool(getattr(entity, "bot", False)),
        contact=bool(getattr(entity, "contact", False)),
        archived=bool(getattr(dialog, "archived", False)),
        muted=dialog_is_muted(dialog),
        unread=int(getattr(dialog, "unread_count", 0) or 0),
        mentions=int(getattr(dialog, "unread_mentions_count", 0) or 0),
        # Telethon's Dialog does not surface "marked unread"; the raw dialog it
        # wraps does. Read through both, so a client version that promotes the
        # field still answers.
        unread_mark=bool(
            getattr(dialog, "unread_mark", False) or getattr(raw, "unread_mark", False)
        ),
    )


# --- the listing ------------------------------------------------------------


def _peer_visible(peer_id: int, *, include_private: bool, self_id: int | None) -> bool:
    """Whether a chat named inside a folder may be named back to the caller.

    The kind is derived from the id itself: Telegram's marked form is negative
    for groups and channels and positive for users, which is all this needs to
    apply the same two rules a dialog listing applies to a row.

    ``self_id`` is what makes Saved Messages recognisable here. A folder that
    contains it stores the account's *own user id* — an ordinary positive
    number, indistinguishable from a friend's without knowing who this account
    is. Left unknown, the hard floor cannot fire for it, so the id is passed in
    rather than inferred.
    """
    private = peer_id > 0
    if self_id is not None and peer_id == self_id:
        kind = PeerKind.SELF
    else:
        kind = PeerKind.USER if private else PeerKind.GROUP
    ref = PeerRef(peer_id=peer_id, kind=kind)
    if hard_denied(ref):
        return False
    return include_private or not private


def needs_self_id(views: list[FolderView], *, include_private: bool) -> bool:
    """Whether the floor cannot be applied without knowing who this account is.

    Only when private chats are being named at all: with ``include_private``
    off, every positive id — Saved Messages among them — is withheld anyway, so
    the extra round trip buys nothing.
    """
    if not include_private:
        return False
    return any(
        peer_id > 0 for view in views for peer_id in (view.include | view.exclude | view.pinned)
    )


async def self_id_of(client: Any, *, what: str) -> int:
    """This account's own user id, for recognising Saved Messages.

    Deliberately not defensive: if Telegram will not say who this account is,
    the floor cannot be applied to the ids in a folder, and a listing that
    proceeded anyway would be guessing about the one chat that must never be
    enumerated.
    """
    with telegram_errors(what=what):
        me = await client.get_me()
    peer_id = int(getattr(me, "id", 0) or 0)
    if not peer_id:
        raise TelegramError(f"{what}: Telegram did not report this account's own id")
    return peer_id


def folder_summary(
    view: FolderView, *, include_private: bool, self_id: int | None = None
) -> dict[str, Any]:
    """One folder as a row, with the peers this configuration may not name
    removed and counted rather than silently dropped."""
    lists: dict[str, list[int]] = {}
    hidden = view.opaque_peers
    for key, ids in (
        ("include_peers", view.include),
        ("exclude_peers", view.exclude),
        ("pinned_peers", view.pinned),
    ):
        visible = [
            peer_id
            for peer_id in sorted(ids)
            if _peer_visible(peer_id, include_private=include_private, self_id=self_id)
        ]
        hidden += len(ids) - len(visible)
        lists[key] = visible

    return {
        "folder_id": view.id,
        "title": view.title,
        "emoticon": view.emoticon,
        "shareable": view.shareable,
        "flags": view.flags.as_dict(),
        **lists,
        "hidden_peers": hidden,
    }


class FoldersInput(ReadInput):
    include_private: bool = Field(
        default=False,
        description=(
            "Also name the one-to-one conversations a folder contains "
            "(requires safety.read.enumerate_dms)."
        ),
    )


async def handle_folders(ctx: OperationContext, params: FoldersInput) -> Envelope:
    require_enumeration(ctx, private=params.include_private, action="folders.list")

    async with open_account(ctx, params.account) as account:
        views = await load_folders(account.client, what="folders.list")
        self_id = (
            await self_id_of(account.client, what="folders.list")
            if needs_self_id(views, include_private=params.include_private)
            else None
        )

    rows = [
        folder_summary(view, include_private=params.include_private, self_id=self_id)
        for view in views
    ]
    warnings: list[str] = []
    if not views:
        # An empty answer is a fact about the account, not a failure. Said out
        # loud, because "no folders" and "folders were filtered away" look
        # identical in an empty list.
        warnings.append(
            "this account has no chat folders; Telegram creates none by default, "
            "so there is nothing here to filter a listing by"
        )
    hidden = sum(row["hidden_peers"] for row in rows)
    if hidden:
        warnings.append(
            f"{hidden} chat(s) named inside these folders are not listed: they are "
            "closed in code (Saved Messages, Service Notifications), or private "
            "and enumeration of direct messages is off"
        )

    return telegram_result(
        ctx,
        {"folders": rows},
        account=account.label,
        returned=len(rows),
        total=len(rows),
        warnings=warnings,
    )


FOLDERS = REGISTRY.register(
    Operation(
        name="folders.list",
        cli=("folders",),
        mcp_tool="telegram_folders",
        summary="List the account's chat folders and the rules behind them.",
        description=(
            "Returns each folder's id, name, emoji and rules: which chats it names, "
            "which it excludes, and which whole categories it admits (contacts, "
            "non-contacts, groups, channels, bots) or withholds (muted, read, "
            "archived). The ids are what `chats` and `inbox` take as `folder`. A "
            "folder is the user's own sorting, not a permission: chats this "
            "configuration may not enumerate stay hidden inside it."
        ),
        input_model=FoldersInput,
        effect=Effect.READ,
        capability=Capability.ENUMERATE,
        handler=handle_folders,  # type: ignore[arg-type]
        tags=("read", "chats"),
    )
)

#: Re-exported so the two listing operations declare the same argument with the
#: same words. A folder filter that means one thing in `chats` and another in
#: `inbox` is the kind of drift the single registry exists to prevent.
FOLDER_FIELD_DESCRIPTION = (
    "Only chats in this Telegram folder — its id, or the name shown in the app. "
    "Filters the listing; it never widens it."
)

__all__ = [
    "FOLDERS",
    "FOLDER_FIELD_DESCRIPTION",
    "DialogFacts",
    "FolderFlags",
    "FolderView",
    "FoldersInput",
    "dialog_is_muted",
    "facts_of",
    "folder_for",
    "folder_summary",
    "handle_folders",
    "input_peer_id",
    "load_folders",
    "needs_self_id",
    "parse_folders",
    "resolve_folder",
    "self_id_of",
]
