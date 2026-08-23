"""``telegram_inbox`` — the one question an assistant actually gets asked.

"What needs an answer right now?" is not a message list. Answering it with raw
history means fetching thousands of messages, spending the context window on
them, and leaving the ranking to whoever reads the output. So this operation
returns a compact summary: one row per waiting conversation, ordered by how
much it looks like someone is waiting, across every permitted account.

The counts come from the dialog list itself. Telegram already tracks unread
totals and unread mentions per dialog — mentions being the field that also
covers replies to this account — so the whole answer is one cheap call per
account rather than a history fetch per chat. Nothing is downloaded, nothing is
opened, and nothing is acknowledged: the badge on the user's own phone looks
exactly the same afterwards.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from ..context import OperationContext
from ..envelope import Envelope
from ..opspec import REGISTRY, Effect, Operation
from ..safety import Capability
from ._client import open_account, visible_labels
from ._common import (
    MAX_DIALOG_SCAN,
    MAX_PAGE,
    ReadInput,
    hard_denied,
    require_enumeration,
    telegram_errors,
    telegram_result,
)
from ._serialize import dialog_summary, peer_ref, preview_of


class InboxInput(ReadInput):
    """``account`` narrows the sweep to one label; by default it covers all."""

    limit: int = Field(default=25, ge=1, le=MAX_PAGE, description="Conversations to return.")
    include_private: bool = Field(
        default=False,
        description="Include one-to-one conversations (requires safety.read.enumerate_dms).",
    )
    mentions_only: bool = Field(
        default=False,
        description="Only conversations where this account was mentioned or replied to.",
    )
    include_muted: bool = Field(default=False, description="Include chats the user has muted.")


def _waiting(row: dict[str, Any], *, mentions_only: bool) -> bool:
    if mentions_only:
        return row["mentions"] > 0
    return row["unread"] > 0 or row["mentions"] > 0


def _rank(row: dict[str, Any]) -> tuple[int, int, str]:
    """Mentions first, then volume, then recency.

    A direct mention is a person addressing this account; a hundred unread
    messages in a busy group usually is not. Sorting purely by unread count
    buries the one message that was actually for you.
    """
    return (-row["mentions"], -row["unread"], row.get("last_message_at") or "")


async def _sweep_account(
    ctx: OperationContext,
    label: str,
    params: InboxInput,
) -> tuple[list[dict[str, Any]], int]:
    """Collect the waiting conversations of one account."""
    rows: list[dict[str, Any]] = []
    hidden = 0

    async with open_account(ctx, label) as account:
        with telegram_errors(what=f"inbox {label}"):
            scanned = 0
            async for dialog in account.client.iter_dialogs(ignore_migrated=True):
                scanned += 1
                if scanned > MAX_DIALOG_SCAN:
                    break
                entity = getattr(dialog, "entity", None)
                if entity is None:
                    continue

                ref = peer_ref(entity)
                if hard_denied(ref):
                    continue
                if ref.is_private and not params.include_private:
                    hidden += 1
                    continue
                if not params.include_muted and _is_muted(dialog):
                    continue

                row = dialog_summary(dialog)
                if not _waiting(row, mentions_only=params.mentions_only):
                    continue

                row["account"] = label
                row["preview"] = preview_of(getattr(dialog, "message", None))
                row["last_from_me"] = bool(getattr(getattr(dialog, "message", None), "out", False))
                rows.append(row)

    return rows, hidden


def _is_muted(dialog: Any) -> bool:
    """Whether the user silenced this chat on their own devices.

    Muting is the user's own statement that this conversation is not urgent, so
    honouring it is what keeps the summary short enough to be worth reading.
    """
    raw = getattr(dialog, "dialog", None)
    notify = getattr(raw, "notify_settings", None)
    if notify is None:
        return False
    if getattr(notify, "silent", False):
        return True
    until = getattr(notify, "mute_until", None)
    if isinstance(until, datetime):
        # Telegram stores a mute as an expiry, and a past expiry means the
        # chat is audible again. Treating any value as "muted" would hide
        # conversations that stopped being muted months ago.
        return until > datetime.now(tz=until.tzinfo or UTC)
    return bool(until)


async def handle_inbox(ctx: OperationContext, params: InboxInput) -> Envelope:
    require_enumeration(ctx, private=params.include_private, action="inbox")

    labels = [params.account] if params.account else visible_labels(ctx)
    warnings: list[str] = []
    collected: list[dict[str, Any]] = []
    hidden_total = 0

    for label in labels:
        try:
            rows, hidden = await _sweep_account(ctx, label, params)
        except Exception as exc:  # noqa: BLE001 - one bad account must not blank the fleet
            warnings.append(f"{label}: {type(exc).__name__}")
            continue
        collected.extend(rows)
        hidden_total += hidden

    collected.sort(key=_rank)
    shown = collected[: params.limit]

    if hidden_total:
        warnings.append(
            f"{hidden_total} private chat(s) omitted; enumeration of direct messages is off"
        )

    totals = {
        "conversations": len(collected),
        "unread": sum(row["unread"] for row in collected),
        "mentions": sum(row["mentions"] for row in collected),
        "accounts": len(labels),
    }

    return telegram_result(
        ctx,
        {"waiting": shown, "totals": totals},
        returned=len(shown),
        total=len(collected),
        truncated=len(shown) < len(collected),
        truncated_reason="limit",
        warnings=warnings,
    )


INBOX = REGISTRY.register(
    Operation(
        name="inbox.list",
        cli=("inbox",),
        mcp_tool="telegram_inbox",
        summary="What is waiting for a reply right now, across every account.",
        description=(
            "A compact, ranked summary of conversations with unread messages or "
            "mentions — one row per chat, not raw history. Mentions rank above volume. "
            "Reading the inbox never marks anything as seen."
        ),
        input_model=InboxInput,
        effect=Effect.READ,
        capability=Capability.ENUMERATE,
        handler=handle_inbox,  # type: ignore[arg-type]
        tags=("read", "triage"),
    )
)
