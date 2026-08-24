"""Putting a chat into one of the account's chat folders.

Separate from `folders.py`, which only reads. Not a stylistic split: `chats.py`
imports `folders.py`, and the write helpers import `write.py`, which imports
`chats.py` — keeping the planner here is what stops that from becoming a cycle.

**Telegram has no "add one chat to a folder" call.** `UpdateDialogFilter`
replaces the whole filter, so adding one chat means sending back every chat the
folder already had. A folder on a real account names private conversations this
configuration may not enumerate, and `folders` deliberately does not print them.
Rebuilding a filter from what this tool can see would therefore delete exactly
the chats the read policy hid — the safety boundary would have become a
data-loss bug, and the deletion would be invisible in both the plan and the
result.

So nothing here rebuilds. The applier fetches the raw filter object Telegram
just sent, appends one peer to it, and hands the same object back. What it
cannot see, it also cannot drop. `test_the_filter_that_is_edited_is_the_object
_telegram_sent` pins that by identity rather than equality, because an
equal-looking copy is precisely the mistake being guarded against.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, Field

from ..errors import InvalidInput, NotFound, PlanPreconditionFailed
from ..opspec import REGISTRY, Effect, Operation
from ..plans import Plan
from ..safety import Capability
from ._common import require_peer
from .folders import input_peer_id, load_folders, resolve_folder
from .write import (
    WriteInput,
    describe,
    open_writer,
    peer_snapshot,
    require_planning_profile,
    resolve_chat_argument,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import OperationContext

FOLDER_ADD_ACTION = "folders.add"


class FolderAddInput(WriteInput):
    folder: str = Field(
        description="Folder id, or the name shown in Telegram — the same token `folders` prints."
    )
    chat: int | str = Field(description="Chat id, @username, or t.me link.")


def recheck_folder(preconditions: dict[str, Any], view: Any, peer_id: int) -> None:
    """Refuse if the folder is not the reviewed one, or no longer accepts this chat.

    Three ways a folder can drift between review and apply, all of them things a
    person does in Telegram meanwhile: the *name* that found it now points at a
    different folder, the folder was turned into a shareable one whose members
    follow an invite link, or the chat was added to its exclusions.

    All three are checked here, in verification, rather than left to the applier.
    Everything before the RPC step has to stay refusable: a refusal raised later
    has already reserved a rate-limit slot and written an audit *attempt* for
    something that never happened.
    """
    if preconditions.get("folder_id") != view.id:
        raise PlanPreconditionFailed(
            "the folder being changed is not the one recorded when the plan was reviewed",
            suggestion="Reject this plan and create a new one.",
        )
    if view.shareable:
        raise PlanPreconditionFailed(
            "this folder has become a shareable folder since the plan was reviewed, and "
            "its members now follow its invite link rather than a list",
            suggestion="Reject this plan; the folder is edited in Telegram itself.",
        )
    if peer_id in view.exclude:
        raise PlanPreconditionFailed(
            "this chat has been added to the folder's exclude list since the plan was "
            "reviewed, and including a chat it excludes would leave the folder "
            "contradicting itself",
            suggestion="Reject this plan, or remove the exclusion in Telegram first.",
        )


def raw_filter_for(result: Any, folder_id: int) -> Any:
    """The untouched filter object for one folder, straight off the wire."""
    for item in getattr(result, "filters", result) or []:
        if getattr(item, "id", None) == folder_id:
            return item
    raise NotFound(
        "folder: this account no longer has a folder with that id",
        suggestion="Folders are edited in Telegram itself; `folders` lists what is there now.",
    )


def add_peer_to_filter(item: Any, peer: Any, peer_id: int) -> bool:
    """Append one peer to a filter's include list, in place.

    Returns whether anything changed. The filter is mutated rather than rebuilt
    for the reason in the module docstring: its other peers must survive being
    invisible to this configuration.
    """
    # A shareable folder is recognised the way `parse_folders` recognises it —
    # `DialogFilterChatlist` carries no category flags at all. Not by a missing
    # `include_peers`: it *has* one, so testing for that would have been a guard
    # that never fires on the real type.
    if not hasattr(item, "contacts"):
        raise InvalidInput(
            "folder: this folder cannot be edited here — it is a shareable folder, "
            "whose members are managed by its invite link in Telegram itself"
        )
    include = getattr(item, "include_peers", None)
    if include is None:
        raise InvalidInput("folder: this folder has no list of chats to add to")
    # Exclusion is checked first because exclusion *wins*: `FolderView.contains`
    # drops an excluded chat before it looks at anything else. A peer sitting in
    # both lists is already a contradiction, and answering "already in the
    # folder" would report it as membership it does not have.
    for existing in getattr(item, "exclude_peers", None) or []:
        if input_peer_id(existing) == peer_id:
            raise InvalidInput(
                "folder: this chat is on the folder's exclude list, and including a chat "
                "it excludes would leave the folder contradicting itself",
                suggestion="Remove it from the exclusion in Telegram first.",
            )
    # Pinned counts as being in the folder, and the plan says so. Without this
    # the summary would promise "already there, nothing changes" while the
    # applier appended the peer a second time.
    for existing in list(include) + list(getattr(item, "pinned_peers", None) or []):
        if input_peer_id(existing) == peer_id:
            return False
    include.append(peer)
    return True


async def plan_folder_add(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(FolderAddInput, params)
    require_planning_profile(ctx, Capability.SEND, action=FOLDER_ADD_ACTION)
    async with open_writer(ctx, p.account) as (label, client):
        # `resolve_chat_argument`, not `resolve_peer`: the field accepts a t.me
        # link, and Telethon cannot resolve one that names a message. The message
        # part is discarded here on purpose — a folder holds chats, not messages.
        target, _link = await resolve_chat_argument(client, p.chat)
        # Before the folders are fetched, not after. Resolving the chat is
        # unavoidable — the policy is written in terms of the peer, and the peer
        # is not known until then — but everything after it is avoidable, and a
        # chat this configuration may not touch should not cost a second request
        # on the account. It also decides which refusal a caller sees: a denied
        # peer is answered as denied, rather than as "no such folder".
        require_peer(ctx, Capability.SEND, target.ref, action=FOLDER_ADD_ACTION)
        view = resolve_folder(await load_folders(client, what=FOLDER_ADD_ACTION), str(p.folder))

    if view.shareable:
        raise InvalidInput(
            "folder: this is a shareable folder, whose members follow its invite link; "
            "it cannot be edited here"
        )
    # Refused here as well as in `add_peer_to_filter`, and that duplication is
    # the point: a plan that cannot be applied should never reach the review
    # queue, and the applier's copy is what holds when the exclusion is added
    # after the plan was written.
    if target.ref.peer_id in view.exclude:
        raise InvalidInput(
            "folder: this chat is on the folder's exclude list, and including a chat "
            "it excludes would leave the folder contradicting itself",
            suggestion="Remove it from the exclusion in Telegram first.",
        )
    already = target.ref.peer_id in view.include or target.ref.peer_id in view.pinned
    named = len(view.include) + len(view.pinned) + view.opaque_peers

    noop = ""
    if already:
        noop = "\n  This chat is already in the folder; applying would change nothing."
    summary = (
        f"Put {describe(target)} into folder {view.id}, as {label}.\n"
        f"  The folder currently names {named} chat(s); all of them stay, including any "
        f"this configuration may not list. Nothing is removed.\n"
        f"  Only you see this: folders are this account's own sorting. No chat is joined, "
        f"left, muted or archived, and nobody is notified." + noop
    )
    return ctx.plans.create(
        operation=FOLDER_ADD_ACTION,
        account=label,
        params=p.model_dump(mode="json"),
        # The id, not the name that found it: see `recheck_folder`.
        preconditions={"peer": peer_snapshot(target), "folder_id": view.id},
        summary=summary,
    )


FOLDER_ADD = REGISTRY.register(
    Operation(
        name=FOLDER_ADD_ACTION,
        # `folder`, not `folders`: the plural is already the listing command, and
        # a group of the same name would replace it. The repository's convention
        # throughout — `chats` lists, `chat …` acts.
        cli=("folder", "add"),
        plan_tool="telegram_plan_folder_add",
        summary="Plan putting one chat into one of the account's chat folders.",
        description=(
            "Adds a chat to a folder's own list of named chats. Telegram replaces the "
            "whole folder when it is edited, so everything already in it is preserved "
            "verbatim — including chats this configuration is not allowed to list. "
            "Folders are the account's own sorting: nothing is joined, left, muted or "
            "archived, and the chats involved are not notified. Shareable folders, whose "
            "members follow an invite link, are refused."
        ),
        input_model=FolderAddInput,
        effect=Effect.REMOTE_WRITE,
        mcp_tool=None,
        capability=Capability.SEND,
        planner=plan_folder_add,
        tags=("write", "plan", "chats"),
    )
)
