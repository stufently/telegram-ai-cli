"""Remote writes that change a *setting* rather than send a message.

Three groups live here, and they are together because a reviewer reads all three
the same way — "what is this, exactly, and what does it replace" — rather than
because Telegram groups them:

**The block list.** ``account.block`` is a setting of this account: the person
can no longer write to it, call it, or see when it was last online. Nobody is
removed from any chat and nothing is banned anywhere. It is one character away
from ``chat.ban`` in a tool listing and a completely different act, so the
preview says which it is, in as many words.

**A chat's identity.** Its title, its description and its photo. Every member
sees the change, and Telegram keeps no copy of what was there before — so the
preview quotes the current value next to the replacement, and the applier
refuses if somebody changed it in the meantime. A preview that showed only the
new value would be asking for approval of a deletion nobody could see.

**A photo is a file on this machine**, which makes it the one settings write
that reads the local filesystem — and it reuses
:func:`telegram_ai_cli.outbox.resolve_outbound` rather than deciding for itself
which files are publishable. That rule already answers the question (containment
after symlinks are resolved, a descriptor rather than a name, an outbox no other
user may write into), and two answers to it in one codebase is how the weaker
one becomes the hole.

They live outside :mod:`telegram_ai_cli.ops.write` because that module is
already the message and moderation surface at some length; every shared helper
— the profile check, peer resolution, the plan snapshot — is imported from it,
so there is one definition of each and not two.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, Field

from ..errors import InvalidInput
from ..opspec import REGISTRY, Effect, Operation
from ..outbox import Delivery, OutboundFile, file_preview, resolve_outbound
from ..plans import Plan
from ..render import quote_for_review
from ..safety import Capability, PeerKind
from ._common import require_peer
from .write import (
    Resolved,
    WriteInput,
    describe,
    open_writer,
    peer_snapshot,
    require_planning_profile,
    resolve_peer,
    text_digest,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import OperationContext

#: Telegram's own ceilings. Refusing here turns a server-side error into a
#: validation message, and keeps a doomed plan out of the review queue.
MAX_CHAT_TITLE_CHARS = 128
MAX_CHAT_ABOUT_CHARS = 255

#: A chat photo has to be one of the two formats Telegram compresses. The check
#: is ``outbox.classify``'s, not a second opinion: a file it would send as a
#: document is a file Telegram will not take as an avatar, and refusing here
#: says so before a plan is written.
PHOTO_DELIVERY = Delivery.PHOTO


# ---------------------------------------------------------------------------
# reading what is there now
# ---------------------------------------------------------------------------


async def current_about(client: Any, target: Resolved) -> str:
    """The chat's description, which no entity carries.

    Telegram puts it on the *full* chat, one request away, and there are two
    requests depending on what kind of chat this is. It is fetched rather than
    left blank because a preview that showed an empty "current" would be
    telling a reviewer that nothing is being overwritten.
    """
    from telethon.tl import functions, types

    peer = await client.get_input_entity(target.ref.peer_id)
    if isinstance(peer, types.InputPeerChannel):
        full = await client(functions.channels.GetFullChannelRequest(channel=peer))
    else:
        full = await client(functions.messages.GetFullChatRequest(chat_id=peer.chat_id))
    return str(getattr(full.full_chat, "about", None) or "")


async def current_photo_id(client: Any, target: Resolved) -> int | None:
    """The id of the photo the chat shows today, or ``None`` for no photo.

    A photo cannot be quoted in a preview the way a title can, so this is the
    nearest thing to showing the old value: the reviewer is told whether one is
    being replaced, and the id is recorded so the applier can refuse when a
    *different* photo has appeared since. Telegram's "no photo" is a distinct
    empty object rather than a missing field, and it carries no id — which is
    exactly the distinction wanted here.
    """
    entity = await client.get_entity(target.ref.peer_id)
    photo_id = getattr(getattr(entity, "photo", None), "photo_id", None)
    return int(photo_id) if photo_id is not None else None


def shown(value: str, *, limit: int = 300) -> str:
    """One reviewable rendering of a value that may not be there at all."""
    return quote_for_review(value, limit=limit) if value else "(empty)"


def describe_photo(photo_id: int | None) -> str:
    """How the photo being replaced is named on the approval screen."""
    return f"id={photo_id}" if photo_id is not None else "(none — the chat has no photo)"


def require_photo_file(file: OutboundFile) -> None:
    """Refuse a file Telegram would not accept as an avatar.

    Decided by ``outbox.classify``, the same function that decides how
    ``message.send_file`` presents a file, so "this is a photo" means one thing
    in this codebase rather than two.
    """
    if file.delivery is PHOTO_DELIVERY:
        return
    raise InvalidInput(
        f"{file.name} would be sent as a {file.delivery}, and a chat photo has to be a "
        "JPEG or a PNG",
        suggestion="Convert the image before planning; Telegram takes no other format here.",
    )


# ---------------------------------------------------------------------------
# input models
# ---------------------------------------------------------------------------


class BlockUserInput(WriteInput):
    user: int | str = Field(
        description=(
            "Id or @username of the single person to block. This is a setting of this "
            "account, not a chat moderation action."
        )
    )


class UnblockUserInput(WriteInput):
    user: int | str = Field(description="Id or @username of the single person to unblock.")


class ChatSettingInput(WriteInput):
    """One chat, one setting.

    A list of chats here would let a single approval rename a dozen of them,
    which is the blast radius the review step exists to bound.
    """

    chat: int | str = Field(description="Chat id or @username of the chat being changed.")


class SetChatTitleInput(ChatSettingInput):
    title: str = Field(min_length=1, max_length=MAX_CHAT_TITLE_CHARS)


class SetChatAboutInput(ChatSettingInput):
    about: str = Field(
        max_length=MAX_CHAT_ABOUT_CHARS,
        description="The new description. An empty string clears it, and the plan says so.",
    )


class SetChatPhotoInput(ChatSettingInput):
    path: str = Field(
        description=(
            "JPEG or PNG to publish, named relative to paths.uploads. A path outside the "
            "outbox is refused by the same rule `message send-file` uses: the caller does "
            "not choose which of this machine's files reaches a chat."
        )
    )


# ---------------------------------------------------------------------------
# planners
# ---------------------------------------------------------------------------


async def _create(
    ctx: OperationContext,
    *,
    operation: str,
    account: str,
    params: BaseModel,
    preconditions: dict[str, Any],
    summary: str,
) -> Plan:
    return ctx.plans.create(
        operation=operation,
        account=account,
        params=params.model_dump(mode="json"),
        preconditions=preconditions,
        summary=summary,
    )


async def _block_target(ctx: OperationContext, params: Any, *, action: str) -> tuple[str, Resolved]:
    """Resolve the person, and refuse anything that is not one.

    The capability is ``admin`` — the same list that names who this tool may act
    *on* — because blocking is an act against a person, and a rule that let any
    resolvable handle be blocked would make "which people may this tool touch"
    unanswerable from the configuration.
    """
    require_planning_profile(ctx, Capability.ADMIN, action=action)
    async with open_writer(ctx, params.account) as (label, client):
        user = await resolve_peer(client, params.user)
    require_peer(ctx, Capability.ADMIN, user.ref, action=action)
    require_person(user, action=action)
    return label, user


async def plan_block_user(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(BlockUserInput, params)
    label, user = await _block_target(ctx, p, action="account.block")

    summary = (
        f"Block {describe(user)} on account {label}\n"
        "  effect:  they can no longer message or call this account, and stop seeing "
        "when it was last online\n"
        "  scope:   this is a setting of THIS ACCOUNT — not a chat ban. Nobody is removed "
        "from any group, and they keep every chat they are in\n"
        "  reversible: plan `account unblock` for the same person"
    )
    return await _create(
        ctx,
        operation="account.block",
        account=label,
        params=p,
        preconditions={"user": peer_snapshot(user)},
        summary=summary,
    )


async def plan_unblock_user(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(UnblockUserInput, params)
    label, user = await _block_target(ctx, p, action="account.unblock")

    summary = (
        f"Unblock {describe(user)} on account {label}\n"
        "  effect:  they may message and call this account again\n"
        "  scope:   this is a setting of THIS ACCOUNT — it lifts no ban in any chat"
    )
    return await _create(
        ctx,
        operation="account.unblock",
        account=label,
        params=p,
        preconditions={"user": peer_snapshot(user)},
        summary=summary,
    )


#: What a chat setting can be applied to. A private conversation has no title,
#: no description and no photo of its own — the "title" of one is the other
#: person's name, and Telegram has no request that changes it.
CHAT_KINDS = frozenset({PeerKind.GROUP, PeerKind.CHANNEL})


def require_person(user: Resolved, *, action: str) -> None:
    """Refuse anything that is not a person for the block list.

    A chat id here is somebody reaching for ``chat.ban`` and getting a different
    effect, and Telegram would take the request. Named by id rather than by
    title for the same reason as everywhere else in this module: an error
    envelope is outside the wrapper that marks stranger-written text as data.
    """
    if user.ref.kind is PeerKind.USER:
        return
    raise InvalidInput(
        f"peer {user.ref.peer_id} is a {user.ref.kind}, and the block list holds people",
        suggestion=(
            "To remove somebody from a chat, plan chat.ban or chat.kick instead — "
            f"{action} is a setting of this account and touches no chat."
        ),
    )


def require_chat_peer(chat: Resolved, *, action: str) -> None:
    """Refuse a peer that has no identity of its own to change.

    Without this a user id plans happily and fails at apply time with an
    ``AttributeError`` from inside Telethon — an approved plan that was never
    applicable, discovered at the one moment nothing can be done about it. The
    peer is named by id: this message is assembled into an error envelope, which
    is outside the wrapper that marks stranger-written text as data.
    """
    if chat.ref.kind in CHAT_KINDS:
        return
    raise InvalidInput(
        f"peer {chat.ref.peer_id} is a {chat.ref.kind}, and {action} changes a group or a channel",
        suggestion=(
            "A private conversation has no title, description or photo of its own. "
            "Use account.profile to change how this account itself appears."
        ),
    )


async def _chat_target(ctx: OperationContext, params: Any, *, action: str) -> tuple[str, Resolved]:
    require_planning_profile(ctx, Capability.ADMIN, action=action)
    async with open_writer(ctx, params.account) as (label, client):
        chat = await resolve_peer(client, params.chat)
        require_peer(ctx, Capability.ADMIN, chat.ref, action=action)
        require_chat_peer(chat, action=action)
    return label, chat


#: The sentence every identity change carries. It is not decoration: the old
#: value exists nowhere once the change lands, so the plan is the last copy of
#: it, and a reviewer has to know that before approving.
_VISIBLE_AND_UNRECORDED = (
    "  Every member sees this, and Telegram keeps no copy of the previous value — "
    "after this is applied, the current one survives only in this plan."
)


async def plan_set_chat_title(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(SetChatTitleInput, params)
    label, chat = await _chat_target(ctx, p, action="chat.set_title")

    current = chat.ref.title or ""
    if current == p.title:
        # Named by id, never by title: this message is assembled into an error
        # envelope, which is outside the wrapper that marks stranger-written
        # text as data. Quoting the title here would hand it over unmarked.
        raise InvalidInput(f"chat {chat.ref.peer_id} already has that title; nothing would change")

    summary = (
        f"Rename {describe(chat)} as {label}\n"
        f"--- current title ---\n{shown(current, limit=200)}\n"
        f"--- new title ({len(p.title)} chars) ---\n{quote_for_review(p.title, limit=200)}\n"
        f"{_VISIBLE_AND_UNRECORDED}"
    )
    return await _create(
        ctx,
        operation="chat.set_title",
        account=label,
        params=p,
        preconditions={
            "peer": peer_snapshot(chat),
            # Stored separately from the peer snapshot's `title_sha256`, which is
            # a hint the applier only warns about: here the current title is the
            # thing being destroyed, so a change to it is a refusal.
            "current_title_sha256": text_digest(current),
        },
        summary=summary,
    )


async def plan_set_chat_about(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(SetChatAboutInput, params)
    require_planning_profile(ctx, Capability.ADMIN, action="chat.set_about")
    async with open_writer(ctx, p.account) as (label, client):
        chat = await resolve_peer(client, p.chat)
        require_peer(ctx, Capability.ADMIN, chat.ref, action="chat.set_about")
        require_chat_peer(chat, action="chat.set_about")
        current = await current_about(client, chat)

    if current == p.about:
        raise InvalidInput(
            f"chat {chat.ref.peer_id} already has that description; nothing would change"
        )

    replacement = (
        f"--- new description ({len(p.about)} chars) ---\n{quote_for_review(p.about, limit=300)}"
        if p.about
        else "--- new description ---\n(cleared: the chat will have no description)"
    )
    summary = (
        f"Change the description of {describe(chat)} as {label}\n"
        f"--- current description ---\n{shown(current)}\n"
        f"{replacement}\n"
        f"{_VISIBLE_AND_UNRECORDED}"
    )
    return await _create(
        ctx,
        operation="chat.set_about",
        account=label,
        params=p,
        preconditions={
            "peer": peer_snapshot(chat),
            "current_about_sha256": text_digest(current),
        },
        summary=summary,
    )


async def plan_set_chat_photo(ctx: OperationContext, params: BaseModel) -> Plan:
    p = cast(SetChatPhotoInput, params)
    # The file is checked before the network is touched: a path outside the
    # outbox is the caller's mistake, and resolving a chat to report it would be
    # an observable act performed for nothing.
    image = resolve_outbound(ctx.settings, p.path)
    require_photo_file(image)
    require_planning_profile(ctx, Capability.ADMIN, action="chat.set_photo")
    async with open_writer(ctx, p.account) as (label, client):
        chat = await resolve_peer(client, p.chat)
        require_peer(ctx, Capability.ADMIN, chat.ref, action="chat.set_photo")
        require_chat_peer(chat, action="chat.set_photo")
        replaced = await current_photo_id(client, chat)

    summary = (
        f"Set the photo of {describe(chat)} as {label}\n"
        f"{file_preview(image)}\n"
        f"--- current photo ---\n  {describe_photo(replaced)}\n"
        "  A photo cannot be quoted the way an old title can, which is why the one being "
        "published is described down to its digest and the one being replaced by its id.\n"
        "  Every member sees this, and Telegram keeps no copy of the photo it replaces."
    )
    return await _create(
        ctx,
        operation="chat.set_photo",
        account=label,
        params=p,
        preconditions={
            "peer": peer_snapshot(chat),
            "file": image.snapshot(),
            # Compared again at apply time: a *different* photo appearing between
            # review and apply means the preview described a replacement that is
            # no longer the one that would happen.
            "current_photo_id": replaced,
        },
        summary=summary,
    )


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def _register(
    *,
    name: str,
    cli: tuple[str, ...],
    plan_tool: str,
    summary: str,
    input_model: type[BaseModel],
    planner: Any,
    description: str = "",
) -> Operation:
    """Declare one settings write.

    ``mcp_tool`` is ``None`` for every one of them, and the registry's
    invariants enforce it: a remote write is planned over MCP and applied from a
    terminal.
    """
    return REGISTRY.register(
        Operation(
            name=name,
            cli=cli,
            summary=summary,
            description=description,
            input_model=input_model,
            effect=Effect.REMOTE_WRITE,
            mcp_tool=None,
            plan_tool=plan_tool,
            capability=Capability.ADMIN,
            planner=planner,
            tags=("write", "plan", "settings"),
        )
    )


BLOCK_USER = _register(
    name="account.block",
    cli=("account", "block"),
    plan_tool="telegram_plan_block_user",
    summary="Plan blocking one person from contacting this account.",
    description=(
        "A setting of this account, not a chat moderation action: nobody is removed from "
        "any chat. The preview says so, because the two are easy to confuse."
    ),
    input_model=BlockUserInput,
    planner=plan_block_user,
)

UNBLOCK_USER = _register(
    name="account.unblock",
    cli=("account", "unblock"),
    plan_tool="telegram_plan_unblock_user",
    summary="Plan letting one person contact this account again.",
    description="The undo for account.block. It lifts no ban in any chat.",
    input_model=UnblockUserInput,
    planner=plan_unblock_user,
)

SET_CHAT_TITLE = _register(
    name="chat.set_title",
    cli=("chat", "set-title"),
    plan_tool="telegram_plan_set_chat_title",
    summary="Plan renaming a chat.",
    description=(
        "The preview quotes the current title next to the new one: Telegram keeps no copy "
        "of the old name, so the plan is the last place it exists."
    ),
    input_model=SetChatTitleInput,
    planner=plan_set_chat_title,
)

SET_CHAT_ABOUT = _register(
    name="chat.set_about",
    cli=("chat", "set-about"),
    plan_tool="telegram_plan_set_chat_about",
    summary="Plan changing a chat's description.",
    description=(
        "The current description is fetched so the preview can show what is being "
        "overwritten. An empty string clears it, and the plan says so."
    ),
    input_model=SetChatAboutInput,
    planner=plan_set_chat_about,
)

SET_CHAT_PHOTO = _register(
    name="chat.set_photo",
    cli=("chat", "set-photo"),
    plan_tool="telegram_plan_set_chat_photo",
    summary="Plan replacing a chat's photo with a local image.",
    description=(
        "The image comes from `paths.uploads` under the same rule `message send-file` "
        "uses, and its bytes are fingerprinted at planning time and re-read at apply "
        "time — what was reviewed is what is published."
    ),
    input_model=SetChatPhotoInput,
    planner=plan_set_chat_photo,
)

#: Every settings write, in registration order. The applier dispatches on
#: ``Operation.name``, so this is also the list of names it must cover.
SETTINGS_OPERATIONS: tuple[Operation, ...] = (
    BLOCK_USER,
    UNBLOCK_USER,
    SET_CHAT_TITLE,
    SET_CHAT_ABOUT,
    SET_CHAT_PHOTO,
)
