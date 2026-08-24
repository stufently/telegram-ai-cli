# Threat model

This is the fuller version of the reasoning summarized in
[`SECURITY.md`](../SECURITY.md#design-notes). It exists so a reader deciding
whether to trust this tool with their own Telegram account doesn't have to
reverse-engineer the reasoning from the source.

## Scope and core assumption

**Content read from Telegram is written by an adversary, not just by the
account owner's contacts.** A message body, a chat title, a group description,
a display name, a bio — every one of them is a string an attacker can shape
freely, simply by sending it. This project treats every such string as data,
never as an instruction, and marks it as such in the JSON contract
(`meta.untrusted_content`, see [`envelope.py`](../telegram_ai_cli/envelope.py)).
Every place that reasoning could plausibly fail is called out below by name.

The rest of this document assumes the reader has read the README's
[Safety](../README.md#safety) section already; it does not repeat the
plan-and-apply mechanics, only the threats and how each one is or isn't
handled.

## Assets, ranked by what losing them costs

1. **The MTProto auth key** (the Telethon `.session` file). Whoever holds it
   controls the Telegram account — reads everything it can read, sends
   anything as it — until the session is revoked from a device. This is the
   single highest-value asset in the whole system.
2. **The Telegram account itself**, independent of the key: even without
   stealing the session file, an attacker who can drive this tool's write
   operations can send, join, leave, invite and promote *as* the account
   owner, with all the social and reputational consequences that implies.
3. **`api_hash`, proxy credentials, `TGAI_SECRET_KEY`.** Individually less
   catastrophic than the session file, but `api_hash` paired with the account's
   phone number is enough to attempt a fresh login, and the secret key
   protects everything encrypted with it.
4. **The content of Telegram conversations** the account can read — including
   ones outside the read allowlist, from the perspective of "what would be bad
   if this tool leaked it," even though the allowlist means most of it is
   never fetched in the first place.
5. **The plan database and audit log.** Lower stakes than the above, but a
   plan can contain an unsent message body (encrypted at rest, see
   [`secretbox.py`](../telegram_ai_cli/secretbox.py)), and the audit log is the
   record everything else in this document leans on for detectability.

## Actors

- **Telegram-side adversary.** Anyone who can get a message, a chat title, a
  bio, a group name or an invite link in front of this tool. This includes
  people the account owner has never interacted with, if `read_allow` is
  configured broadly enough to see them (groups and channels are open by
  default — see the README's [Configure](../README.md#configure) section).
- **A prompt injection riding in Telegram content.** A special case of the
  above, worth naming separately: text engineered to look like an instruction
  to whatever AI agent is reading this tool's output, not to a human. This is
  the primary reason `untrusted_content` exists and the primary reason no read
  tool executes anything found in message text. The flag alone only says a
  response contains such text; the value itself is delimited with
  `⟦untrusted⟧ … ⟦/untrusted⟧`, and the delimiter characters cannot survive
  inside the content they frame, so a sender cannot end the wrapper early and
  continue as though the tool were speaking. See
  [the trust boundary](operations.md#the-trust-boundary-in-tool-output).
- **A compromised or malicious MCP client with no shell.** A client that
  speaks MCP but cannot run arbitrary commands on the host — most IDE
  integrations, Claude Desktop as shipped. Its ceiling is exactly the MCP tool
  surface: the read tools and the plan tools, and no apply tool. The count is
  deliberately not repeated here — it moves, and a stale number in a threat
  model reads as a smaller blast radius than the one that ships. See
  [MCP tools in the README](../README.md#mcp-tools).
- **An AI agent that *does* have shell access** (Claude Code, Codex, and
  comparable coding agents), whether acting on the owner's genuine intent or
  having been steered by injected content. This is the actor the rest of this
  document spends the most words on, because it is the one existing controls
  do the least against.
- **A co-resident process running as the same OS user.** Can read
  `~/.config/telegram-ai-cli/`, `~/.local/state/telegram-ai-cli/`, and any
  environment variable the `tg-ai` process was started with — including
  `TGAI_SECRET_KEY` if it was passed that way rather than through a key file.
- **A network attacker between this tool and Telegram**, or a Telegram-side
  compromise. Out of scope for this project specifically (Telethon and MTProto
  own that boundary), noted here only for completeness.

## Threats and current handling

### T1 — An agent (steered by injected content, or just wrong) tries to act on a chat outside its remit

**Mitigated.** The capability matrix in [`safety.py`](../telegram_ai_cli/safety.py)
is checked for every read and every plan creation, before anything reaches
Telegram. Direct messages are closed until explicitly allowlisted; every write
capability is fail-closed the same way. `777000` and Saved Messages are
excluded as constants, ahead of any allow/deny list — see
[`config.py`](../telegram_ai_cli/config.py). None of this can be reconfigured
away by anything arriving through a tool call, because the check runs against
the loaded `Settings` object, not against text in the request.

### T2 — A username-based plan gets applied against a different account than the one it was written for

**Partially mitigated — the error exists, the check that raises it doesn't yet.**
Policy and targets are meant to be resolved to a numeric peer id at plan time
and stored in `Plan.preconditions` ([`plans.py`](../telegram_ai_cli/plans.py));
`PlanPreconditionFailed` in [`errors.py`](../telegram_ai_cli/errors.py) exists
precisely for "the world moved between planning and applying," and
`plan show` already surfaces `preconditions` to a reviewer. What's missing is
the module that actually re-verifies the id against Telegram before calling
the send RPC — `apply.py`, which `cli.py`'s `plan apply` command already
imports but which doesn't exist yet. Tracked in [`TASKS.md`](../TASKS.md).

### T3 — A plan gets applied twice, or two concurrent applies race

**Mitigated.** `PlanStore.claim()` in [`plans.py`](../telegram_ai_cli/plans.py)
implements the `pending → applying` transition as a conditional `UPDATE`
inside a `BEGIN IMMEDIATE` transaction (see
[`db.immediate()`](../telegram_ai_cli/db.py)), the same pattern already used
for rate-limit reservations in [`limits.py`](../telegram_ai_cli/limits.py).
Two processes racing to claim the same `plan_id` cannot both win — only the
caller whose `UPDATE` actually changed a row proceeds.

### T4 — A timeout after the RPC left causes a retry, sending a message twice

**Mitigated.** `PlanUnknownOutcome` in [`errors.py`](../telegram_ai_cli/errors.py)
is deliberately not retryable, and Telethon's own automatic flood-wait retry
is disabled (`telethon_flood_sleep_threshold: int = 0` in
[`config.py`](../telegram_ai_cli/config.py)) so nothing beneath this project
resends on its own either. An `unknown_outcome` plan is left for a human to
resolve by hand.

### T5 — A rate limit gets reset by restarting the process

**Mitigated.** Limits are counted from rows in SQLite
([`limits.py`](../telegram_ai_cli/limits.py)), not from an in-memory counter,
and a slot is reserved *before* the network call under the same
`BEGIN IMMEDIATE` pattern, so a burst of concurrent callers can't all read the
same pre-increment count and all proceed.

### T6 — A malicious chat title, message body or plan summary manipulates what a human sees when reviewing a plan

**Mitigated.** Everything Telegram-authored is passed through
[`render.py`](../telegram_ai_cli/render.py) before it reaches a terminal — ANSI
and OSC escape sequences, carriage returns, control characters and
bidirectional-override characters are all stripped. This is the control the
entire human-approval story depends on: if a chat title could redraw the
terminal, "the human read the plan and approved it" would stop meaning
anything. `--raw` exists only in the CLI for a person who explicitly wants the
unfiltered text, and is never exposed to an MCP client.

### T7 — Sensitive values leak through logs, errors, or tool output

**Partially mitigated.** [`redact.py`](../telegram_ai_cli/redact.py) masks
values recognizable by shape (phone numbers, card numbers, seed phrases, TON
and EVM addresses, login codes, API-token-shaped strings) in any structure
before it leaves the process. This is explicitly a second line of defence —
see the module's own docstring — because it cannot make a conversation
non-personal: names, handles and the sentences themselves still carry
identity. The read allowlist is what actually limits what gets fetched in the
first place; redaction only limits the damage of what does.

### T8 — A copy of the state database, a backup, or a stray file leaks `api_hash`, proxy credentials, or plan bodies

**Mitigated for the database; not for the `.session` file itself.**
`api_hash`, proxy credentials and plan bodies are AES-256-GCM encrypted with a
key held outside the database
([`secretbox.py`](../telegram_ai_cli/secretbox.py)) — a leaked database or
backup is useless without the key. The Telethon `.session` file, however,
*is* the auth key, in the clear, by the nature of MTProto session storage;
`0600` permissions protect it from other users on the same host, not from a
disk-level backup that copies file contents regardless of mode. If backups of
the state directory are in scope for a deployment, encrypt the backup or the
volume, not just rely on file permissions.

### T9 — A reader who can already run as the same OS user

**Explicitly not mitigated, and not claimed to be.** Anyone who can execute
code as the user running `tg-ai` can read `TGAI_SECRET_KEY` from the
environment the same way this process does, read the key file, read the
session file, and — if they can also reach a terminal attached to this
process — approve their own plans. This project's threat model starts at "an
adversary controls Telegram-side content and possibly an AI agent's actions";
it does not attempt to defend against an adversary who already has arbitrary
code execution as the same OS user, because nothing at the application layer
can meaningfully defend against that.

### T10 — An MCP client with shell access runs `tg-ai plan apply` itself

**Named, not mitigated — this is the important one.** The plan-and-apply split
exists specifically because a confirmation an agent can send over MCP is a
confirmation a prompt injection reaching that agent can send too. But that
argument only holds while the *transport* is the boundary — an MCP tool call
versus a terminal command a human types. When the MCP client is Claude Code,
Codex, or any comparable agent that also has its own shell, there is no tool
call involved in running `tg-ai plan apply <id>`: it's the identical shell
command a human would type, issued by whichever process happens to hold that
terminal. Nothing in this project — and nothing achievable at this project's
layer — can distinguish "the person typed this" from "the agent typed this"
once they share a terminal, because the OS presents both as the same session
owner running the same binary.

For this class of client, the actual security properties this project offers
are:

- **Detectability, not prevention.** Every attempt is logged before the RPC
  and every outcome after (`audit.py`), so an unauthorized apply is visible in
  the log even though it wasn't stopped.
- **A cap on damage per unit time.** Persistent rate limits mean a compromised
  or misled agent can do only so much before hitting a ceiling that does not
  reset on restart (`limits.py`).
- **An unconfigurable floor.** `777000` and Saved Messages are unreachable
  regardless of who or what is driving `tg-ai`, because that check does not
  distinguish callers at all.
- **Legible plans.** Because a plan is sanitized before display
  (`render.py`), if a human *does* look at `tg-ai plan show <id>` before an
  agent applies it, what they see is trustworthy — the failure mode this
  project accepts is an agent applying without a human looking, not a human
  being shown a falsified plan.

None of that adds up to "an agent cannot send a message on its own" for a
shell-capable client. It adds up to "if it does, there will be a record, a
ceiling, and a small number of chats it structurally cannot reach." Anyone
deploying this tool with a shell-capable MCP client should read that as: **you
are trusting the agent**, in the same way you would trust a human assistant
with terminal access to your account, and this project's controls are the
audit trail and blast-radius limits you'd want on that assistant regardless of
whether they're a person or a model.

## A real approval boundary, and why v0.1 doesn't have one

A boundary that actually holds against T10 needs the "apply" step to require
something the agent's own execution context structurally cannot produce. Two
designs get there; neither is in v0.1, and both are real scope, not a small
addition:

**A separate OS principal.** `tg-ai plan apply` runs as a different user than
the one the agent's shell runs as — a dedicated account, invoked through
`sudo` with a password prompt the agent cannot answer, or a Unix domain socket
whose permissions only that second user can write to. This is a strong
boundary because it relies on kernel-enforced access control, not on
convention, but it means the person has to explicitly switch context (or keep
a second terminal open as the other user) to approve anything — a real
workflow cost, and one that has to be designed so it doesn't just get
automated away by the same agent with `sudo` rights.

**A TOTP-gated apply.** `tg-ai plan apply <id> --code <6-digit-TOTP>`, checked
against a secret provisioned once into an authenticator app on a device the
agent has no access to. Weaker than a separate OS principal against an
attacker who already has code execution in the approving human's session (a
TOTP code typed once can be relayed by anything watching that session), but
meaningfully stronger against the case this project actually worries about
most: an agent that can run shell commands but has no channel to a phone in
someone's pocket.

Both require care that's easy to get wrong — a `sudo` rule broad enough to be
useful is often broad enough to be automatable, and a TOTP check implemented
as "ask a tool for the code" defeats its own purpose if the agent is the one
asking. Getting either right is future work, tracked as a design question
rather than an item in [`TASKS.md`](../TASKS.md) until there's an actual
decision to implement.

## Out of scope

- Vulnerabilities in Telegram, Telethon, or `opentele-ng` themselves.
- An account owner deliberately widening their own configuration (adding
  chats to an allowlist, switching to the `plan` profile). That's the
  configuration working as designed, not a bypass.
- Telegram limiting or banning the account for activity that is genuinely
  abusive at the account level, independent of whether this tool or a person
  typing by hand sent the requests.
- Physical or OS-level compromise of the host the account material lives on.
  Encrypt the disk or the backup; this project protects the database's
  contents from a *copy*, not the host from an attacker who already owns it.
