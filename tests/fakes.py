"""Driving a handler against a Telegram that is not there.

Everything under ``ops/`` ends in a Telethon call, and until now every test that
wanted to drive a whole handler built its own stand-in for one. Four modules
did; the fifth was written from scratch rather than borrowing, which is how a
shared fixture earns its place — not to save typing, but because a fake nobody
shares drifts from the client it imitates, one module at a time.

Two properties are the reason this exists rather than a bag of dataclasses.

**An unexpected request is a failure, not an answer.** A fake that returns the
same object for every request lets a new call slip through a green suite: it is
issued, it is answered by something irrelevant, and nothing asserts either way.
That happened here. :class:`FakeClient` answers what it was told to answer and
raises on anything else, so a handler that starts asking Telegram something new
says so the first time it runs.

**Every call is recorded, with its arguments.** Some properties are about what
was *not* sent — that reading a chat never acknowledges its messages, that a
refusal happens before the fetch rather than after it. Those are assertions
about the recording, which no assertion about the answer can replace. The
convenience methods record their keyword arguments too, so a paging cursor that
stops being passed is visible rather than merely harmless here.

What this fake does **not** do is reproduce paging: ``get_messages`` returns
what it was given, whatever ``max_id`` and ``limit`` say. A test about paging
belongs against a fake that implements it — ``test_search_context`` and
``test_archive`` each have one — or against an assertion on the recorded
arguments.

It lives beside the tests rather than inside ``conftest.py`` because these are
classes a test imports, not fixtures pytest injects; ``conftest`` holds the one
fixture, and importing a conftest by name is a habit that breaks under
``--import-mode=importlib``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: One fixed moment, so a summary never differs between two runs.
NOW = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)


@dataclass
class FakeMessage:
    """A message as the handlers read one: by attribute, with defaults."""

    id: int = 10
    message: str | None = "hello"
    date: datetime = NOW
    out: bool = False
    sender_id: int | None = None
    sender: Any = None
    edit_date: datetime | None = None
    views: int | None = None
    pinned: bool = False
    media: Any = None
    reply_to: Any = None
    forward: Any = None
    reactions: Any = None


@dataclass
class FakeRawDialog:
    """The ``Dialog`` Telethon wraps — where the notification settings live."""

    notify_settings: Any = None
    unread_mark: bool = False


@dataclass
class FakeDialog:
    entity: Any
    unread_count: int = 0
    unread_mentions_count: int = 0
    unread_reactions_count: int = 0
    pinned: bool = False
    archived: bool = False
    message: Any = None
    date: datetime = NOW
    dialog: FakeRawDialog = field(default_factory=FakeRawDialog)


class _Page(list):  # type: ignore[type-arg]
    """What a history page is: a list that also knows how many there were.

    Telethon returns a ``TotalList``, and handlers read ``.total`` off it to say
    whether a page was truncated. A plain list answers ``None`` there, which
    reads as "not truncated" — true here, and the reason the attribute exists
    rather than being left to `getattr`.
    """

    def __init__(self, items: list[Any], total: int | None = None) -> None:
        super().__init__(items)
        self.total = len(items) if total is None else total


class UnexpectedRequest(BaseException):
    """A handler asked Telegram something this test never said it would.

    Derived from :class:`BaseException` on purpose. Handlers catch broad
    ``except Exception`` in places where a failed call is context rather than
    the answer — `chat.read` degrades an unreadable read state to
    ``known: false`` that way — and an ``AssertionError`` raised here would be
    swallowed by exactly that branch, turning "this test never canned an answer"
    into a green run with a quietly wrong result.
    """


class FakeClient:
    """A Telethon client that answers only what it was told to answer.

    ``answers`` maps a request class to what calling it returns; anything else
    raises :class:`UnexpectedRequest` naming the class. Passing a bare object
    instead of a mapping is deliberately not supported — that is the shape that
    silently answers everything.
    """

    def __init__(
        self,
        *,
        entity: Any = None,
        entities: dict[Any, Any] | None = None,
        dialogs: list[Any] | None = None,
        messages: list[Any] | None = None,
        drafts: list[Any] | None = None,
        participants: list[Any] | None = None,
        answers: dict[type, Any] | None = None,
        me: Any = None,
    ) -> None:
        self._entity = entity
        self._entities = entities or {}
        self._dialogs = dialogs or []
        self._messages = messages or []
        self._drafts = drafts or []
        self._participants = participants or []
        self._answers = answers or {}
        self._me = me
        #: Every request object handed to `__call__`, in order.
        self.requests: list[Any] = []
        #: Every call that went through a Telethon convenience method instead,
        #: as `("name", target, kwargs)` — those issue requests too, and their
        #: arguments are where a dropped paging cursor shows up.
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    # --- connection ---------------------------------------------------------

    def is_connected(self) -> bool:
        return True

    async def is_user_authorized(self) -> bool:
        return True

    async def get_me(self) -> Any:
        self.calls.append(("get_me", None, {}))
        return self._me

    # --- reading ------------------------------------------------------------

    async def get_entity(self, target: Any) -> Any:
        self.calls.append(("get_entity", target, {}))
        if self._entities:
            try:
                found = self._entities.get(target)
            except TypeError:  # unhashable target, e.g. an InputPeer
                found = None
            if found is not None:
                return found
        if self._entity is None:
            raise UnexpectedRequest(f"no entity was canned for {target!r}")
        return self._entity

    async def get_messages(self, target: Any, **kwargs: Any) -> Any:
        """Telethon's two shapes, kept apart.

        A scalar ``ids`` returns one message or ``None``; a list returns a list
        with a hole where a message is missing. Returning a list for both would
        let a handler index into something Telethon would never hand it.
        """
        self.calls.append(("get_messages", target, kwargs))
        ids = kwargs.get("ids")
        by_id = {message.id: message for message in self._messages}
        if isinstance(ids, int):
            return by_id.get(ids)
        if ids is not None:
            return [by_id.get(one) for one in ids]
        return _Page(self._messages)

    def iter_dialogs(self, **kwargs: Any) -> Any:
        self.calls.append(("iter_dialogs", None, kwargs))

        async def stream() -> Any:
            for dialog in self._dialogs:
                yield dialog

        return stream()

    def iter_drafts(self, **kwargs: Any) -> Any:
        self.calls.append(("iter_drafts", None, kwargs))

        async def stream() -> Any:
            for draft in self._drafts:
                yield draft

        return stream()

    async def get_participants(self, target: Any, **kwargs: Any) -> Any:
        self.calls.append(("get_participants", target, kwargs))
        return _Page(self._participants)

    # --- raw requests -------------------------------------------------------

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        for kind, answer in self._answers.items():
            if isinstance(request, kind):
                return answer
        raise UnexpectedRequest(
            f"{type(request).__name__} was issued, and this test canned no answer for it"
        )

    # --- assertions the tests share -----------------------------------------

    def issued(self) -> list[type]:
        """The class of every raw request, which is what properties assert on."""
        return [type(request) for request in self.requests]


@dataclass
class FakeAccount:
    """What the registry hands back: a client and the spec it came from."""

    client: Any
    spec: Any = None


class FakeRegistry:
    def __init__(self, client: Any, label: str = "main") -> None:
        self._client = client
        self._label = label

    def list_accounts(self) -> list[str]:
        return [self._label]

    def load_account(self, _label: str | None = None) -> FakeAccount:
        return FakeAccount(client=self._client)

    def get(self, _label: str) -> None:
        return None
