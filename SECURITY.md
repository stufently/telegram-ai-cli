# Security policy

## Reporting a vulnerability

Report privately through [GitHub Security Advisories](https://github.com/stufently/telegram-ai-cli-mcp/security/advisories/new) on this repository. Please do not open a public issue for a vulnerability, and please do not report it by email — advisories are the only channel this project monitors for security reports.

Include what you did, what happened, and what you expected. A proof of concept helps but is not required.

You can expect an acknowledgement within a week and an assessment within two. Fixes for confirmed issues are released as soon as they are ready, and the advisory credits the reporter unless asked otherwise.

## Scope

In scope:

- Any way to reach `777000` (Telegram Service Notifications) or Saved Messages through this tool, by any configuration or any input. Both are meant to be closed in code, unconditionally — see [Design notes](#design-notes).
- Any way for a caller reachable only through MCP (no shell) to apply a plan — send, edit, delete, forward, join, leave, invite, promote, or change a profile — without going through `tg-ai plan apply` at a terminal.
- A caller reading or acting on a chat that is not on the relevant allowlist, or on the deny list.
- A rate or send limit that resets on a process restart, rather than persisting.
- Any path by which the plaintext MTProto auth key, `api_hash`, a proxy credential, or `TGAI_SECRET_KEY` reaches stdout, stderr, a log line, an error message, an audit entry, or an MCP client response.
- Text from a Telegram message, chat title or display name that changes the behaviour of this program, rather than being treated as inert data — including anything that alters what appears on a terminal reviewing a plan (see [`render.py`](telegram_ai_cli_mcp/render.py)).
- Escaping the session, download, plan-database or config directories through a caller-supplied label, path or filename.
- A plan being applied twice, or applied against a target that changed between planning and applying (a username re-pointed at a different account, for example).

Out of scope:

- Vulnerabilities in Telegram itself, Telethon, or `opentele-ng`. Report those upstream.
- The consequences of an MCP client that has its own shell (Claude Code, Codex, and comparable coding agents) running `tg-ai plan apply` on its own. That is a documented, named limit of this design, not a bypass — see [Safety in the README](README.md#safety) and [the threat model](docs/threat-model.md).
- The consequences of an account owner deliberately loosening the safety configuration (adding a chat to `safety.write.send.allow`, switching to the `plan` profile, and so on). The config is the operator's decision to make.
- Telegram limiting or banning the automated account for activity that is genuinely abusive at the account level (mass joins, high-volume sends, member scraping), independent of this tool.
- A person approving a plan whose contents they did not read carefully. The plan-and-apply step exists so a person decides; it does not decide correctly for them.

## Design notes

The threat model assumes the content this program reads from Telegram — message bodies, chat titles, usernames, group descriptions — is written by an adversary, not just by the account owner's contacts. Nothing read from Telegram is ever treated as an instruction to this program or to the model reading its output; the `untrusted_content` flag in the JSON envelope exists to say so in-band (see [JSON contract in the README](README.md#json-contract)).

Specific mitigations, and where they live:

- **A hard denylist, not a default.** `777000` and Saved Messages are excluded in [`config.py`](telegram_ai_cli_mcp/config.py) as constants, checked first in [`safety.py`](telegram_ai_cli_mcp/safety.py) before any allowlist is consulted. No YAML value and no environment variable can reopen them.
- **Fail-closed allow lists.** An empty `allow` list means nothing is permitted for that capability — reading a direct message, sending, joining, admin actions — except for reading groups and channels, which is open by default so the tool is usable before it has been configured at all. See the module docstrings in [`config.py`](telegram_ai_cli_mcp/config.py) and [`safety.py`](telegram_ai_cli_mcp/safety.py).
- **Plan, then apply — and the boundary that gives, named honestly.** Every write goes through a saved plan first; the MCP surface has no tool that applies one. That is a real boundary for an MCP client with no shell of its own. It is not a boundary at all against a client that also has shell access, because a confirmation an agent can send is a confirmation a prompt injection reaching that agent can send too, and there is no way to tell, at the process level, whether `tg-ai plan apply` was typed by the person or by the agent sharing their terminal. For that class of client, the safety net is an audit trail and persistent limits, not an unbreakable gate — see [`docs/threat-model.md`](docs/threat-model.md) for the fuller discussion and for what a real gate (a separate OS principal, or a TOTP-guarded apply) would need, which v0.1 does not implement.
- **Preconditions are re-checked at apply time, not just at plan time.** A username is resolved to a numeric peer id once and stored in the plan's `preconditions` ([`plans.py`](telegram_ai_cli_mcp/plans.py)); the module that applies a plan is required to re-verify that id (not the handle) before calling Telethon, so a target cannot be swapped between the two steps. That module (`apply.py`) doesn't exist yet — tracked in [TASKS.md](TASKS.md).
- **Persistent, reserved-before-the-call rate limits.** Limits live in SQLite, not memory, so restarting the process does not refresh the budget; a slot is reserved before the network call and released only on a whitelisted class of exception that proves nothing happened — never by matching on error text ([`limits.py`](telegram_ai_cli_mcp/limits.py)).
- **Terminal output is sanitized before a human approves anything.** ANSI/OSC escape sequences, carriage returns, control characters and bidirectional-override characters are stripped from anything that came from Telegram before it is shown in `tg-ai plan show` or anywhere else — otherwise the text a person is approving and the text that renders on their screen could be two different things ([`render.py`](telegram_ai_cli_mcp/render.py)). `--raw` exists only in the CLI, for a person who explicitly wants the unfiltered bytes, and is not exposed over MCP.
- **Two-phase audit log.** An `attempt` record is written before the RPC leaves and an `outcome` record after, sharing an id, so a crash between the two is visible as an unresolved attempt rather than silence ([`audit.py`](telegram_ai_cli_mcp/audit.py)). Message bodies are recorded as a hash and a length by default, not stored in full — an audit trail that mirrors every conversation is a second copy of the thing being protected.
- **Encryption at rest for what has to sit in a database.** `api_hash`, proxy credentials and (when the `plans` config asks for it) plan message bodies are AES-256-GCM encrypted with a key held outside the database (`TGAI_SECRET_KEY` or a generated key file) — see [`secretbox.py`](telegram_ai_cli_mcp/secretbox.py). This protects a copy of the database or a stray backup; it does not protect against a reader who can already run as the same user, since they can read the key the same way this process does.
- **PII redaction is a second line of defence, not the control.** [`redact.py`](telegram_ai_cli_mcp/redact.py) recognizes values by shape — phone numbers, card numbers, email addresses, seed phrases, TON and EVM addresses, Telegram's own login codes — and masks them in output. It cannot make a conversation non-personal: names, usernames and the sentences themselves still carry identity. The control that actually limits exposure is the read allowlist, not this module.

## Supported versions

The latest release. Security fixes are not backported to earlier tags before 1.0.
