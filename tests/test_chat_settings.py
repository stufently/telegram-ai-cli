"""Settings-shaped writes: the block list, a new chat, and a chat's identity.

These operations differ from the message writes in what a reviewer has to be
told, and each test here pins one of those differences rather than the plumbing
they share with everything else in ``ops/write.py``.

**A block is a setting of this account, not a moderation action.** It is the
one operation whose name looks like ``chat.ban`` and means something else
entirely: nobody is removed from any chat, and the effect exists only between
the blocked person and this account. If the preview cannot be told apart from a
ban's preview, the wrong one gets approved — so the summary says which it is,
and a test asserts it does.

**Creating a chat says which kind and admits nobody.** ``kind`` decides whether
subscribers may speak at all, and a channel created with members already in it
is an audience nobody chose to have. Both are properties of the preview and of
the request that is eventually issued.

**Renaming shows what is being lost.** Telegram keeps no copy of a chat's
previous title, description or photo, and every member sees the new one. A
preview that showed only the replacement would be asking somebody to approve a
deletion they cannot see, so the old value is quoted next to the new one and
re-checked before the change is applied.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_ai_cli import db
from telegram_ai_cli.apply import (
    _LIMIT_KINDS,
    _UPLOAD_OPERATIONS,
    _execute,
    _Prepared,
    _verify,
)
from telegram_ai_cli.audit import AuditLog
from telegram_ai_cli.config import Settings
from telegram_ai_cli.context import OperationContext
from telegram_ai_cli.errors import (
    Denylisted,
    InvalidInput,
    NotAllowlisted,
    PlanPreconditionFailed,
    ProfileForbidden,
)
from telegram_ai_cli.limits import LimitKind, LimitStore
from telegram_ai_cli.ops import settings as settings_ops
from telegram_ai_cli.ops import write
from telegram_ai_cli.ops.settings import (
    BLOCK_USER,
    SETTINGS_OPERATIONS,
    BlockUserInput,
    SetChatAboutInput,
    SetChatPhotoInput,
    SetChatTitleInput,
    UnblockUserInput,
)
from telegram_ai_cli.ops.write import CreateGroupInput
from telegram_ai_cli.opspec import REGISTRY, Effect
from telegram_ai_cli.plans import PlanState, PlanStore
from telegram_ai_cli.safety import Capability, PeerKind, PeerRef, SafetyKernel
from telegram_ai_cli.secretbox import SecretBox
from telegram_ai_cli.untrusted import CLOSE_MARKER, OPEN_MARKER

# Obviously fake ids, written the way tests/test_no_private_data.py wants them.
CHANNEL_BASE = -(10**12)
GROUP_ID = CHANNEL_BASE - 4242
BASIC_GROUP_ID = -4343
MEMBER_ID = 555
#: Telegram Service Notifications. Closed in code, and the block list is one
#: more door it must not be reachable through.
SERVICE_ID = 777000

CURRENT_TITLE = "Marketing"
CURRENT_ABOUT = "Where the campaign lives"
#: The photo the fake chat shows, so a preview can name what is replaced.
PHOTO_ID = 909090

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """No ``TGAI_`` variable from the developer's shell may steer a decision."""
    for key in list(os.environ):
        if key.startswith("TGAI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


# --- stand-ins for Telethon --------------------------------------------------


def entities(photo_id: int | None = PHOTO_ID) -> tuple[Any, Any, Any, Any]:
    from telethon.tl.types import Channel, Chat, ChatPhoto, ChatPhotoEmpty, User

    photo = (
        ChatPhoto(photo_id=photo_id, dc_id=2, stripped_thumb=None)
        if photo_id is not None
        else ChatPhotoEmpty()
    )
    supergroup = Channel(
        id=4242, title=CURRENT_TITLE, photo=photo, date=None, megagroup=True, username="marketing"
    )
    basic = Chat(
        id=4343, title=CURRENT_TITLE, photo=None, participants_count=3, date=None, version=1
    )
    member = User(id=MEMBER_ID, first_name="Someone", username="someone")
    service = User(id=SERVICE_ID, first_name="Telegram")
    return supergroup, basic, member, service


class FakeClient:
    """Resolves the peers a settings plan names, and records every RPC."""

    def __init__(
        self,
        *,
        basic_group: bool = False,
        about: str = CURRENT_ABOUT,
        photo_id: int | None = PHOTO_ID,
    ) -> None:
        from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser

        supergroup, basic, member, service = entities(photo_id)
        chat_id = BASIC_GROUP_ID if basic_group else GROUP_ID
        self._entities: dict[Any, Any] = {
            chat_id: basic if basic_group else supergroup,
            MEMBER_ID: member,
            "someone": member,
            SERVICE_ID: service,
        }
        self._inputs: dict[int, Any] = {
            chat_id: (
                InputPeerChat(chat_id=4343)
                if basic_group
                else InputPeerChannel(channel_id=4242, access_hash=1)
            ),
            MEMBER_ID: InputPeerUser(user_id=MEMBER_ID, access_hash=2),
            SERVICE_ID: InputPeerUser(user_id=SERVICE_ID, access_hash=3),
        }
        self.chat_id = chat_id
        self.about = about
        self.photo_id = photo_id
        self.resolved: list[Any] = []
        self.requests: list[Any] = []
        self.uploaded: list[Any] = []

    async def get_entity(self, target: Any) -> Any:
        self.resolved.append(target)
        try:
            entity = self._entities[target]
        except KeyError:
            raise ValueError(f"no entity for {target!r}") from None
        if target == self.chat_id:
            # Rebuilt from the current photo id: a test changes it to stand for
            # somebody replacing the photo between review and apply.
            entity = entities(self.photo_id)[1 if self.chat_id == BASIC_GROUP_ID else 0]
        return entity

    async def get_input_entity(self, peer_id: Any) -> Any:
        return self._inputs[peer_id]

    async def upload_file(self, path: Any) -> Any:
        self.uploaded.append(Path(path))
        return SimpleNamespace(name="uploaded")

    async def __call__(self, request: Any) -> Any:
        from telethon.tl import functions

        self.requests.append(request)
        if isinstance(
            request,
            functions.channels.GetFullChannelRequest | functions.messages.GetFullChatRequest,
        ):
            return SimpleNamespace(full_chat=SimpleNamespace(about=self.about))
        chats = [self._entities[self.chat_id]]
        return SimpleNamespace(updates=[], users=[], chats=chats)


class FakePlan:
    """Only ``operation`` and ``preconditions`` are read by the two steps."""

    def __init__(
        self, operation: str, *, preconditions: dict[str, Any] | None = None, account: str = "work"
    ) -> None:
        self.operation = operation
        self.account = account
        self.preconditions = preconditions or {}


def context(tmp_path, client: FakeClient, **overrides: Any) -> OperationContext:
    """A real plan store and audit log over a temporary directory."""
    targets = [client.chat_id, MEMBER_ID, SERVICE_ID]
    paths = overrides.pop("paths", {"uploads": str(tmp_path / "uploads")})
    conf = Settings(
        profile=overrides.pop("profile", "plan"),
        safety=overrides.pop("safety", {"write": {"admin": {"allow": targets}}}),
        paths=paths,
        **overrides,
    )
    conn = db.connect(tmp_path / "state.db")
    return OperationContext(
        settings=conf,
        safety=SafetyKernel(conf),
        plans=PlanStore(conn, conf.plans, SecretBox(secrets.token_bytes(32))),
        limits=LimitStore(conn, conf.limits),
        audit=AuditLog(tmp_path / "audit.log", conf.audit),
        actor="cli",
        _conn=conn,
    )


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture(autouse=True)
def _no_real_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every planner borrows a client through ``open_writer``; here it is fake."""

    @asynccontextmanager
    async def writer(ctx: Any, account: str | None):  # noqa: ANN401
        yield "work", writer.client  # type: ignore[attr-defined]

    monkeypatch.setattr(write, "open_writer", writer)
    monkeypatch.setattr(settings_ops, "open_writer", writer)
    _no_real_account.writer = writer  # type: ignore[attr-defined]


def use(client: FakeClient) -> None:
    """Point the patched ``open_writer`` at this client."""
    _no_real_account.writer.client = client  # type: ignore[attr-defined]


def an_image(tmp_path, name: str = "logo.png", data: bytes = PNG) -> Path:
    """A file in the outbox. 0755 because `outbox` refuses a root other users
    can write into, which is part of what makes the rule worth sharing."""
    root = tmp_path / "uploads"
    root.mkdir(exist_ok=True)
    root.chmod(0o755)
    path = root / name
    path.write_bytes(data)
    return path


# --- the shape of the operations --------------------------------------------


@pytest.mark.parametrize("op", SETTINGS_OPERATIONS, ids=lambda o: o.name)
def test_no_settings_operation_can_act_on_its_own(op) -> None:
    """The invariant the whole project rests on, checked per operation."""
    assert op.effect is Effect.REMOTE_WRITE
    assert op.mcp_tool is None, "a remote write must not be reachable as a tool call"
    assert op.plan_tool and op.plan_tool.startswith("telegram_plan_")
    assert "apply" not in op.plan_tool.lower()
    assert op.planner is not None
    assert op.handler is None
    assert op.capability is Capability.ADMIN


def test_the_registry_still_holds_with_the_new_operations() -> None:
    assert REGISTRY.check_invariants() is None


def test_every_remote_write_in_the_registry_has_a_budget() -> None:
    """The applier refuses an operation with no budget, so a missing entry
    would turn a reviewed plan into a runtime failure at apply time."""
    planned = {op.name for op in REGISTRY.all() if op.is_remote_write}
    assert planned <= set(_LIMIT_KINDS)


@pytest.mark.parametrize("op", SETTINGS_OPERATIONS, ids=lambda o: o.name)
def test_every_settings_operation_draws_on_the_admin_budget(op) -> None:
    assert _LIMIT_KINDS[op.name] is LimitKind.ADMIN


def test_the_input_models_reject_an_invented_argument() -> None:
    with pytest.raises(InvalidInput):
        BLOCK_USER.parse({"user": MEMBER_ID, "reason": "spam"})


@pytest.mark.parametrize("model", [BlockUserInput, UnblockUserInput], ids=lambda m: m.__name__)
def test_one_plan_blocks_one_person(model) -> None:
    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic's own error
        model(user=[MEMBER_ID, 556])


# --- blocking ----------------------------------------------------------------


async def test_a_block_names_the_person_and_says_it_is_not_a_chat_ban(
    tmp_path, client: FakeClient
) -> None:
    """The one preview that could be mistaken for `chat.ban`'s. If a reviewer
    cannot tell them apart, the wrong one gets approved."""
    use(client)
    ctx = context(tmp_path, client)

    plan = await settings_ops.plan_block_user(ctx, BlockUserInput(user=MEMBER_ID))

    assert plan.operation == "account.block"
    assert plan.state is PlanState.PENDING
    assert "Someone" in plan.summary and "@someone" in plan.summary
    assert str(MEMBER_ID) in plan.summary
    assert "not a chat ban" in plan.summary.lower()
    assert "account unblock" in plan.summary
    # Planning is not doing: not a single request left.
    assert client.requests == []


async def test_an_unblock_says_what_comes_back(tmp_path, client: FakeClient) -> None:
    use(client)
    plan = await settings_ops.plan_unblock_user(
        context(tmp_path, client), UnblockUserInput(user=MEMBER_ID)
    )

    assert plan.operation == "account.unblock"
    assert "Someone" in plan.summary
    assert client.requests == []


async def test_the_service_account_cannot_be_blocked(tmp_path, client: FakeClient) -> None:
    """The hard denylist runs before any allow list, and the block list is one
    more door leading to the chat that carries login codes."""
    use(client)
    ctx = context(tmp_path, client)

    with pytest.raises(Denylisted):
        await settings_ops.plan_block_user(ctx, BlockUserInput(user=SERVICE_ID))
    assert ctx.plans.list() == []


async def test_blocking_a_group_is_refused(tmp_path, client: FakeClient) -> None:
    """`account.block` acts on a person. A chat id here would be somebody
    reaching for `chat.ban` and getting a different effect."""
    use(client)

    with pytest.raises(InvalidInput):
        await settings_ops.plan_block_user(context(tmp_path, client), BlockUserInput(user=GROUP_ID))


async def test_a_person_outside_the_admin_allowlist_cannot_be_blocked(
    tmp_path, client: FakeClient
) -> None:
    use(client)
    ctx = context(tmp_path, client, safety={"write": {"admin": {"allow": [GROUP_ID]}}})

    with pytest.raises(NotAllowlisted):
        await settings_ops.plan_block_user(ctx, BlockUserInput(user=MEMBER_ID))
    assert ctx.plans.list() == []


async def test_the_readonly_profile_refuses_before_telegram_is_touched(
    tmp_path, client: FakeClient
) -> None:
    """Resolving a handle is itself observable, so a readonly profile must not
    reach Telegram even to look somebody up."""
    use(client)
    ctx = context(tmp_path, client, profile="readonly")

    with pytest.raises(ProfileForbidden):
        await settings_ops.plan_block_user(ctx, BlockUserInput(user=MEMBER_ID))
    assert client.resolved == []


async def test_applying_a_block_issues_one_block_request(client: FakeClient) -> None:
    from telethon.tl import functions

    prepared = _Prepared(
        limit_target=str(MEMBER_ID),
        peers={"user": write.Resolved(ref=PeerRef(peer_id=MEMBER_ID, kind=PeerKind.USER))},
    )
    outcome, _ = await _execute(
        client, FakePlan("account.block"), BlockUserInput(user=MEMBER_ID), prepared
    )

    assert outcome["blocked"] is True
    assert len(client.requests) == 1
    assert isinstance(client.requests[0], functions.contacts.BlockRequest)


async def test_applying_an_unblock_issues_one_unblock_request(client: FakeClient) -> None:
    from telethon.tl import functions

    prepared = _Prepared(
        limit_target=str(MEMBER_ID),
        peers={"user": write.Resolved(ref=PeerRef(peer_id=MEMBER_ID, kind=PeerKind.USER))},
    )
    outcome, _ = await _execute(
        client, FakePlan("account.unblock"), UnblockUserInput(user=MEMBER_ID), prepared
    )

    assert outcome["blocked"] is False
    assert isinstance(client.requests[0], functions.contacts.UnblockRequest)


async def test_applying_a_block_re_checks_the_person(tmp_path, client: FakeClient) -> None:
    """The handle is resolved again and compared: a username released and taken
    by somebody else between review and apply must not be blocked in their
    place."""
    use(client)
    ctx = context(tmp_path, client)
    params = BlockUserInput(user="someone")
    plan = await settings_ops.plan_block_user(ctx, params)

    prepared = await _verify(
        ctx, client, FakePlan("account.block", preconditions=plan.preconditions), params
    )
    assert prepared.peers["user"].ref.peer_id == MEMBER_ID

    moved = FakePlan("account.block", preconditions={"user": dict(plan.preconditions["user"])})
    moved.preconditions["user"]["peer_id"] = 556
    with pytest.raises(PlanPreconditionFailed):
        await _verify(ctx, client, moved, params)


# --- creating a channel ------------------------------------------------------


async def test_creating_a_channel_says_which_kind_it_is_and_who_may_speak(
    tmp_path, client: FakeClient
) -> None:
    use(client)
    plan = await write.plan_create_group(
        context(tmp_path, client), CreateGroupInput(title="Announcements", kind="channel")
    )

    assert plan.operation == "chat.create"
    assert "channel" in plan.summary.lower()
    assert "only admins" in plan.summary.lower()
    assert "private" in plan.summary.lower()
    assert plan.preconditions["kind"] == "channel"
    assert client.requests == []


async def test_a_supergroup_is_still_the_default(tmp_path, client: FakeClient) -> None:
    use(client)
    plan = await write.plan_create_group(
        context(tmp_path, client), CreateGroupInput(title="Marketing")
    )
    assert plan.preconditions["kind"] == "supergroup"
    assert "supergroup" in plan.summary.lower()


def test_a_channel_may_not_be_created_with_members_in_it() -> None:
    """An audience nobody chose to have. Inviting is its own operation, with
    its own approval."""
    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic's own error
        CreateGroupInput(title="Announcements", kind="channel", users=[MEMBER_ID])


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic's own error
        CreateGroupInput(title="Announcements", kind="broadcast-list")


async def test_applying_a_channel_creation_asks_for_a_broadcast_channel(
    client: FakeClient,
) -> None:
    outcome, _ = await _execute(
        client,
        FakePlan("chat.create"),
        CreateGroupInput(title="Announcements", kind="channel"),
        _Prepared(limit_target="work"),
    )

    assert outcome["created"] is True
    request = client.requests[0]
    assert request.broadcast is True
    assert not request.megagroup


async def test_applying_a_supergroup_creation_still_asks_for_a_megagroup(
    client: FakeClient,
) -> None:
    await _execute(
        client,
        FakePlan("chat.create"),
        CreateGroupInput(title="Marketing"),
        _Prepared(limit_target="work"),
    )
    request = client.requests[0]
    assert request.megagroup is True
    assert not request.broadcast


async def test_a_creation_whose_kind_moved_after_review_is_refused(
    tmp_path, client: FakeClient
) -> None:
    """The kind decides whether members may speak at all. A plan row edited
    between review and apply must not be applied against the summary that was
    actually read."""
    use(client)
    ctx = context(tmp_path, client)
    params = CreateGroupInput(title="Announcements", kind="channel")
    plan = await write.plan_create_group(ctx, params)
    tampered = FakePlan("chat.create", preconditions=dict(plan.preconditions))
    tampered.preconditions["kind"] = "supergroup"

    with pytest.raises(PlanPreconditionFailed):
        await _verify(ctx, client, tampered, params)


# --- renaming, describing, re-photographing ----------------------------------


async def test_a_rename_shows_the_old_title_next_to_the_new_one(
    tmp_path, client: FakeClient
) -> None:
    use(client)
    plan = await settings_ops.plan_set_chat_title(
        context(tmp_path, client), SetChatTitleInput(chat=GROUP_ID, title="Growth")
    )

    assert plan.operation == "chat.set_title"
    assert CURRENT_TITLE in plan.summary, "the value being lost has to be visible"
    assert "Growth" in plan.summary
    assert "every member" in plan.summary.lower()
    assert "keeps no copy" in plan.summary.lower()
    assert (
        plan.preconditions["current_title_sha256"]
        == hashlib.sha256(CURRENT_TITLE.encode()).hexdigest()
    )


async def test_a_rename_to_the_same_title_is_refused_without_quoting_it(
    tmp_path, client: FakeClient
) -> None:
    """A chat title is written by strangers, and `Envelope.failure` neither
    wraps nor defangs the text it carries — so a refusal names the chat by id."""
    use(client)

    with pytest.raises(InvalidInput) as caught:
        await settings_ops.plan_set_chat_title(
            context(tmp_path, client), SetChatTitleInput(chat=GROUP_ID, title=CURRENT_TITLE)
        )

    assert CURRENT_TITLE not in caught.value.message
    assert str(GROUP_ID) in caught.value.message


async def test_a_new_description_is_shown_against_the_current_one(
    tmp_path, client: FakeClient
) -> None:
    use(client)
    plan = await settings_ops.plan_set_chat_about(
        context(tmp_path, client), SetChatAboutInput(chat=GROUP_ID, about="Now with numbers")
    )

    assert plan.operation == "chat.set_about"
    assert CURRENT_ABOUT in plan.summary
    assert "Now with numbers" in plan.summary
    assert (
        plan.preconditions["current_about_sha256"]
        == hashlib.sha256(CURRENT_ABOUT.encode()).hexdigest()
    )


async def test_clearing_a_description_says_so_rather_than_showing_nothing(
    tmp_path, client: FakeClient
) -> None:
    use(client)
    plan = await settings_ops.plan_set_chat_about(
        context(tmp_path, client), SetChatAboutInput(chat=GROUP_ID, about="")
    )
    assert "cleared" in plan.summary.lower()
    assert CURRENT_ABOUT in plan.summary


async def test_a_description_is_read_from_the_full_chat_not_guessed(
    tmp_path, client: FakeClient
) -> None:
    """Telegram does not put a description on the entity, so the planner has to
    ask for it — a preview that showed an empty "current" would be lying."""
    from telethon.tl import functions

    use(client)
    await settings_ops.plan_set_chat_about(
        context(tmp_path, client), SetChatAboutInput(chat=GROUP_ID, about="Now with numbers")
    )
    assert any(isinstance(r, functions.channels.GetFullChannelRequest) for r in client.requests)


async def test_a_basic_group_description_comes_from_its_own_request(tmp_path) -> None:
    from telethon.tl import functions

    basic = FakeClient(basic_group=True)
    use(basic)
    await settings_ops.plan_set_chat_about(
        context(tmp_path, basic), SetChatAboutInput(chat=BASIC_GROUP_ID, about="Newer")
    )
    assert any(isinstance(r, functions.messages.GetFullChatRequest) for r in basic.requests)


async def test_applying_a_rename_whose_title_moved_is_refused(tmp_path, client: FakeClient) -> None:
    """Somebody renamed the chat between review and apply: the old value in the
    summary is no longer the value that would be overwritten."""
    use(client)
    ctx = context(tmp_path, client)
    params = SetChatTitleInput(chat=GROUP_ID, title="Growth")
    plan = await settings_ops.plan_set_chat_title(ctx, params)
    tampered = FakePlan("chat.set_title", preconditions=dict(plan.preconditions))
    tampered.preconditions["current_title_sha256"] = hashlib.sha256(b"Something else").hexdigest()

    with pytest.raises(PlanPreconditionFailed):
        await _verify(ctx, client, tampered, params)


async def test_applying_a_description_change_whose_current_value_moved_is_refused(
    tmp_path, client: FakeClient
) -> None:
    use(client)
    ctx = context(tmp_path, client)
    params = SetChatAboutInput(chat=GROUP_ID, about="Now with numbers")
    plan = await settings_ops.plan_set_chat_about(ctx, params)

    client.about = "Somebody else got there first"
    stale = FakePlan("chat.set_about", preconditions=plan.preconditions)
    with pytest.raises(PlanPreconditionFailed):
        await _verify(ctx, client, stale, params)


async def test_applying_a_rename_asks_telegram_to_rename_the_chat(client: FakeClient) -> None:
    from telethon.tl import functions

    prepared = _Prepared(
        limit_target=str(GROUP_ID),
        peers={"chat": write.Resolved(ref=PeerRef(peer_id=GROUP_ID, kind=PeerKind.GROUP))},
    )
    outcome, _ = await _execute(
        client,
        FakePlan("chat.set_title"),
        SetChatTitleInput(chat=GROUP_ID, title="Growth"),
        prepared,
    )

    assert outcome["title"] == "changed"
    assert isinstance(client.requests[0], functions.channels.EditTitleRequest)
    assert client.requests[0].title == "Growth"


async def test_applying_a_rename_in_a_basic_group_uses_the_other_request() -> None:
    from telethon.tl import functions

    basic = FakeClient(basic_group=True)
    prepared = _Prepared(
        limit_target=str(BASIC_GROUP_ID),
        peers={"chat": write.Resolved(ref=PeerRef(peer_id=BASIC_GROUP_ID, kind=PeerKind.GROUP))},
    )
    await _execute(
        basic,
        FakePlan("chat.set_title"),
        SetChatTitleInput(chat=BASIC_GROUP_ID, title="Growth"),
        prepared,
    )
    assert isinstance(basic.requests[0], functions.messages.EditChatTitleRequest)


async def test_applying_a_description_change_issues_one_request(client: FakeClient) -> None:
    from telethon.tl import functions

    prepared = _Prepared(
        limit_target=str(GROUP_ID),
        peers={"chat": write.Resolved(ref=PeerRef(peer_id=GROUP_ID, kind=PeerKind.GROUP))},
    )
    outcome, _ = await _execute(
        client,
        FakePlan("chat.set_about"),
        SetChatAboutInput(chat=GROUP_ID, about="Now with numbers"),
        prepared,
    )

    assert outcome["about"] == "changed"
    assert isinstance(client.requests[0], functions.messages.EditChatAboutRequest)


# --- the photo, which is a file on this machine ------------------------------
#
# The rule about *which* file is `outbox.resolve_outbound`, shared with
# `message.send_file` and tested there. What is asserted here is that this
# operation uses it — a second, weaker copy of that rule is the failure mode
# worth a test — and the two things it adds: a chat photo must be a format
# Telegram compresses, and the photo being replaced is named in the preview.


async def test_a_photo_plan_names_the_file_and_what_it_replaces(
    tmp_path, client: FakeClient
) -> None:
    use(client)
    path = an_image(tmp_path)

    plan = await settings_ops.plan_set_chat_photo(
        context(tmp_path, client), SetChatPhotoInput(chat=GROUP_ID, path=str(path))
    )

    assert plan.operation == "chat.set_photo"
    assert "logo.png" in plan.summary
    assert "every member" in plan.summary.lower()
    assert f"id={PHOTO_ID}" in plan.summary, "the photo being replaced has to be named"
    assert plan.preconditions["file"]["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert plan.preconditions["file"]["size_bytes"] == len(PNG)
    assert plan.preconditions["current_photo_id"] == PHOTO_ID
    assert client.requests == []


async def test_a_chat_with_no_photo_says_so_rather_than_showing_an_id(
    tmp_path, client: FakeClient
) -> None:
    bare = FakeClient(photo_id=None)
    use(bare)
    an_image(tmp_path)

    plan = await settings_ops.plan_set_chat_photo(
        context(tmp_path, bare), SetChatPhotoInput(chat=GROUP_ID, path="logo.png")
    )
    assert "no photo" in plan.summary
    assert plan.preconditions["current_photo_id"] is None


async def test_the_outbox_rule_is_the_shared_one_and_not_a_second_copy(
    tmp_path, client: FakeClient
) -> None:
    """A path outside `paths.uploads` is refused, and it is refused by
    `outbox.resolve_outbound` — the same function `message.send_file` uses.
    Two rules for "which local file may leave" is how the weaker one becomes
    the hole, so this asserts the call rather than re-testing the rule."""
    use(client)
    an_image(tmp_path)
    (tmp_path / "escape.png").write_bytes(PNG)
    seen: list[str] = []
    real = settings_ops.resolve_outbound

    def spy(settings, raw_path, **kwargs):
        seen.append(raw_path)
        return real(settings, raw_path, **kwargs)

    settings_ops.resolve_outbound = spy  # type: ignore[assignment]
    try:
        with pytest.raises(NotAllowlisted):
            await settings_ops.plan_set_chat_photo(
                context(tmp_path, client),
                SetChatPhotoInput(chat=GROUP_ID, path=str(tmp_path / "escape.png")),
            )
    finally:
        settings_ops.resolve_outbound = real  # type: ignore[assignment]

    assert seen == [str(tmp_path / "escape.png")]


async def test_a_photo_may_be_named_relative_to_the_uploads_directory(
    tmp_path, client: FakeClient
) -> None:
    use(client)
    an_image(tmp_path)

    plan = await settings_ops.plan_set_chat_photo(
        context(tmp_path, client), SetChatPhotoInput(chat=GROUP_ID, path="logo.png")
    )
    assert plan.preconditions["file"]["sha256"] == hashlib.sha256(PNG).hexdigest()


async def test_a_file_telegram_would_not_take_as_an_avatar_is_refused(
    tmp_path, client: FakeClient
) -> None:
    """`outbox.classify` decides, so "this is a photo" means the same thing here
    as it does in a send. A document would be accepted by the outbox rule and
    refused by Telegram, at apply time, on an approved plan."""
    use(client)
    an_image(tmp_path, name="notes.pdf")

    with pytest.raises(InvalidInput) as caught:
        await settings_ops.plan_set_chat_photo(
            context(tmp_path, client), SetChatPhotoInput(chat=GROUP_ID, path="notes.pdf")
        )
    assert "JPEG or a PNG" in caught.value.message


async def test_applying_a_photo_whose_bytes_changed_is_refused(
    tmp_path, client: FakeClient
) -> None:
    """The file is re-read at apply time. What was reviewed is what is
    published, or nothing is."""
    use(client)
    ctx = context(tmp_path, client)
    path = an_image(tmp_path)
    params = SetChatPhotoInput(chat=GROUP_ID, path=str(path))
    plan = await settings_ops.plan_set_chat_photo(ctx, params)

    path.write_bytes(PNG + b"different")

    stale = FakePlan("chat.set_photo", preconditions=plan.preconditions)
    with pytest.raises(PlanPreconditionFailed):
        await _verify(ctx, client, stale, params)


async def test_applying_a_photo_that_now_replaces_a_different_one_is_refused(
    tmp_path, client: FakeClient
) -> None:
    """Somebody changed the chat photo between review and apply: the preview
    named the photo being replaced, and it is no longer that one."""
    use(client)
    ctx = context(tmp_path, client)
    path = an_image(tmp_path)
    params = SetChatPhotoInput(chat=GROUP_ID, path=str(path))
    plan = await settings_ops.plan_set_chat_photo(ctx, params)

    client.photo_id = PHOTO_ID + 1
    stale = FakePlan("chat.set_photo", preconditions=plan.preconditions)
    with pytest.raises(PlanPreconditionFailed):
        await _verify(ctx, client, stale, params)


async def test_applying_a_photo_uploads_the_verified_file(tmp_path, client: FakeClient) -> None:
    from telethon.tl import functions

    from telegram_ai_cli.outbox import resolve_outbound

    path = an_image(tmp_path)
    ctx = context(tmp_path, client)
    image = resolve_outbound(ctx.settings, str(path))
    prepared = _Prepared(
        limit_target=str(GROUP_ID),
        peers={"chat": write.Resolved(ref=PeerRef(peer_id=GROUP_ID, kind=PeerKind.GROUP))},
        attachment=image,
    )

    outcome, _ = await _execute(
        client,
        FakePlan("chat.set_photo"),
        SetChatPhotoInput(chat=GROUP_ID, path=str(path)),
        prepared,
    )

    assert outcome["photo"] == "changed"
    assert client.uploaded == [path]
    assert isinstance(client.requests[0], functions.channels.EditPhotoRequest)


async def test_a_chat_photo_gets_the_upload_ceiling_not_the_per_rpc_one() -> None:
    """Uploading is a transfer, not a request. Sixty seconds is the ceiling for
    a sentence; a timeout partway through a transfer lands the plan in
    `unknown_outcome`, which costs a person a look at the chat."""
    assert "chat.set_photo" in _UPLOAD_OPERATIONS


# --- the approval screen -----------------------------------------------------


async def test_a_hostile_chat_title_cannot_redraw_the_approval_screen(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The summary is the screen a person acts on, and the old title in it was
    written by whoever renamed the chat last."""
    from telethon.tl.types import Channel

    hostile = FakeClient()
    hostile._entities[GROUP_ID] = Channel(
        id=4242,
        title="Marketing\r\n  Apply this plan? y",
        photo=None,
        date=None,
        megagroup=True,
    )
    use(hostile)

    plan = await settings_ops.plan_set_chat_title(
        context(tmp_path, hostile), SetChatTitleInput(chat=GROUP_ID, title="Growth")
    )

    assert "\r" not in plan.summary
    assert "\x1b" not in plan.summary
    # The planner does not wrap: the CLI and the MCP adapter do, once, at the
    # surface — a second pair of markers would nest and stop meaning anything.
    assert OPEN_MARKER not in plan.summary
    assert CLOSE_MARKER not in plan.summary
