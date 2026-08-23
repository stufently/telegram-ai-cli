"""Append-only record of everything this tool did to the outside world.

Written in two phases. ``attempt`` goes to disk *before* the RPC leaves;
``outcome`` follows once the result is known. A log written only after success
loses exactly the case it exists for — the send that went out and then the
process died, the timeout that may or may not have delivered. One line per
event, both sharing an id, so a reader can find attempts that never got an
outcome and treat them as unresolved rather than as absent.

Every value is passed through :func:`telegram_ai_cli.render.sanitize_line`
first. A log is something a human greps and a program parses; message text that
can inject newlines or terminal escapes can forge records in both.

Message bodies are recorded as a hash and a length by default. An audit trail
that reproduces every conversation is a second copy of the thing being
protected, and it usually ends up with weaker permissions than the first.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from .config import AuditConfig
from .render import sanitize_line


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: Path, config: AuditConfig) -> None:
        self._path = path
        self._config = config

    # -- writing -----------------------------------------------------------

    def _write(self, record: dict[str, Any]) -> None:
        if not self._config.enabled:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._rotate_if_needed()

        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        # O_APPEND alone orders writes within one file, but the lock is what
        # keeps two processes from interleaving partial lines, and fsync is
        # what makes the attempt record survive the crash it is there to
        # describe.
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _rotate_if_needed(self) -> None:
        try:
            size = self._path.stat().st_size
        except FileNotFoundError:
            return
        if size < self._config.rotate_bytes:
            return
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        self._path.replace(self._path.with_suffix(f".{stamp}.jsonl"))

    # -- the two phases ----------------------------------------------------

    def attempt(
        self,
        *,
        action: str,
        account: str,
        actor: str,
        peer_id: int | None = None,
        plan_id: str | None = None,
        body: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Record an intent about to be carried out. Returns the event id."""
        event_id = secrets.token_hex(8)
        record: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": "attempt",
            "event_id": event_id,
            "action": sanitize_line(action, limit=64),
            "account": sanitize_line(account, limit=64),
            "actor": sanitize_line(actor, limit=32),
        }
        if peer_id is not None:
            record["peer_id"] = peer_id
        if plan_id:
            record["plan_id"] = sanitize_line(plan_id, limit=64)
        if body is not None:
            record["body_sha256"] = _digest(body)
            record["body_len"] = len(body)
            if self._config.include_bodies:
                record["body"] = sanitize_line(body, limit=4000)
        if extra:
            record["extra"] = {k: sanitize_line(str(v), limit=200) for k, v in extra.items()}
        self._write(record)
        return event_id

    def outcome(
        self,
        event_id: str,
        *,
        status: str,
        message_id: int | None = None,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Close an attempt.

        ``status`` is one of ``applied``, ``failed`` or ``unknown``. The third
        is not a failure: it means the request left and the answer never came
        back, which a reader must be able to tell apart from a clean refusal.
        """
        record: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": "outcome",
            "event_id": sanitize_line(event_id, limit=64),
            "status": sanitize_line(status, limit=32),
        }
        if message_id is not None:
            record["message_id"] = message_id
        if error_code:
            record["error_code"] = sanitize_line(error_code, limit=64)
        if detail:
            record["detail"] = sanitize_line(detail, limit=500)
        self._write(record)

    def refusal(self, *, action: str, actor: str, reason: str, peer_id: int | None = None) -> None:
        """Record something the policy kernel declined.

        Refusals belong in the same log as actions. A run of them against the
        same chat is the signal that someone is probing, and it is invisible if
        only successes are written down.
        """
        record: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": "refused",
            "action": sanitize_line(action, limit=64),
            "actor": sanitize_line(actor, limit=32),
            "reason": sanitize_line(reason, limit=300),
        }
        if peer_id is not None:
            record["peer_id"] = peer_id
        self._write(record)
