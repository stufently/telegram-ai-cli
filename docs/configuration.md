# Configuration reference

Everything on this page is defined in
[`config.py`](../telegram_ai_cli/config.py) and read exactly once per process
into a `Settings` object. Policy decisions are made against *that* object, never
against text arriving through a tool call — which is why nothing in a message,
a chat title or a tool argument can widen what follows.

Two conventions run through the whole file, and they explain most of the
defaults:

**An empty `allow` list means "nothing", not "everything"** — for every write
capability and for reading private chats. The opposite convention is common, and
it is how a tool ends up permitting the one thing nobody meant to permit: the
list was empty because it had not been filled in yet.

**Groups and channels read freely; direct messages do not.** A tool that can
read nothing until it is configured cannot even list chats to discover their
ids, so it is useless on first run and gets configured carelessly. Private
correspondence is the part worth gating, and it is gated.

## Where settings come from

1. `~/.config/telegram-ai-cli/tgai.yaml` — or wherever `TGAI_CONFIG` points, or
   `tg-ai --config <path>`.
2. `TGAI_`-prefixed environment variables, which **win** over the file.

Nesting in an environment variable is a double underscore, so
`safety.write.send.allow` is `TGAI_SAFETY__WRITE__SEND__ALLOW`.

> **Lists and objects in environment variables are JSON**, because that is how
> `pydantic-settings` parses a complex type:
> `TGAI_SAFETY__WRITE__SEND__ALLOW='["123456789","-1001234567890"]'`.
> A bare comma-separated string is a validation error, not a two-element list.

A YAML section whose keys are all commented out parses as `null`; those are
dropped rather than failing validation, so a mostly-commented example config
falls back to defaults. See [`.env.example`](../.env.example) for the
environment form of the common settings.

## What cannot be configured

```python
HARD_DENIED_PEERS: frozenset[int] = frozenset({777000})
HARD_DENY_SAVED_MESSAGES = True
```

`777000` is Telegram Service Notifications, where login codes and 2FA resets
arrive; reading it hands over the account. Saved Messages is where people keep
passwords and documents precisely because it feels private.

Both are constants in `config.py`, not defaults: there is **no settings key for
either**, so no YAML value, no environment variable and no text arriving through
a tool can reopen them. The check runs ahead of every allow and deny list in
[`safety.py`](../telegram_ai_cli/safety.py), and a test asserts that `Settings`
has no attribute that would let it be overridden
([`tests/test_denylist.py`](../tests/test_denylist.py)).

A profile named `full` — direct send with no plan step — does not exist either.
It was considered and rejected; see the [threat model](threat-model.md).

**Neither is the trust boundary in tool output.** Every value a person outside
this system wrote — message body, media caption, display name, chat title — is
delimited with `⟦untrusted⟧ … ⟦/untrusted⟧` before a result leaves
([`untrusted.py`](../telegram_ai_cli/untrusted.py)), and there is no key to turn
that off. A switch here would be a switch for making injected text
indistinguishable from this tool's own fields again, which is the failure the
markers exist to prevent. The same is true of redaction: `redact_output` is read
through one function so a key *could* be added later, but none exists today.

The markers are published in `meta.untrusted_markers` on every response that
carries them, so a program parsing the payload strips them from a value it was
told about rather than from a literal it hard-coded. Details, including why a
sender cannot forge them, are in
[Operations → The trust boundary](operations.md#the-trust-boundary-in-tool-output).

## `profile`

| Value | What it permits |
| --- | --- |
| `readonly` (default) | Read operations only. Every `plan_*` operation refuses with `FORBIDDEN_BY_PROFILE`. |
| `plan` | Read, plus *creating* plans. Applying is still only `tg-ai plan apply`. |

```yaml
profile: plan
```

`TGAI_PROFILE=plan`. Least privilege is the default deliberately: an MCP client
that only needs to read never needs the ability to write a plan.

## Application credentials

```yaml
api_id: 0
api_hash: "…"
```

Fallback app credentials from <https://my.telegram.org>, used when an account has
no fingerprint of its own. They are consulted at exactly one point — when an
account is registered (`tg-ai account add`) or signed in (`tg-ai account login`)
— and are copied into that account's row, encrypted at rest. Afterwards the
account uses the device fingerprint frozen in `<label>.api.json` beside its
session, which always wins.

**There is no `--api-hash` command-line flag on purpose.** A secret in `argv` is
visible in `ps` to every user on the host and lands in shell history, so the
credential arrives through the environment or the config file instead. The same
rule is why the two-step verification password is only ever read from an
interactive prompt — see
[`accounts/login.py`](../telegram_ai_cli/accounts/login.py).

A frozen fingerprint matters beyond convenience: a device fingerprint that
changes between restarts looks exactly like a session someone stole and is
replaying from their own machine, which is a good way to have it killed.

## `paths`

| Key | Default | Holds |
| --- | --- | --- |
| `paths.sessions` | `$XDG_STATE_HOME/telegram-ai-cli/sessions` (else `~/.local/state/…`) | `.session` files, device fingerprints, session locks |
| `paths.state` | `$XDG_STATE_HOME/telegram-ai-cli` | `state.db` (accounts, plans, rate limits) and `secret.key` |
| `paths.downloads` | `…/telegram-ai-cli/downloads` | Everything `media fetch` writes |
| `paths.audit_log` | `…/telegram-ai-cli/audit.jsonl` | The append-only audit log |
| `paths.archive` | `…/telegram-ai-cli/archive.sqlite3` | The local message archive — only what `archive sync` was told to copy |

Directories are created `0700` and account material `0600`. A failed `chmod` is
fatal rather than logged and ignored: a warning would leave the file readable
and the run continuing, which is the outcome the check exists to stop.

### The archive file, and why it is not encrypted

`paths.archive` is a SQLite file created `0600` inside the `0700` state
directory, and it holds message text — other people's — for whichever chats
`archive sync` was explicitly told to copy. Nothing fills it in the background;
there is no key that turns on bulk collection.

The mode is checked and narrowed on **every** open, not only at creation: a file
left `0644` by an earlier version, a restore from a backup or a careless
`chmod -R` all produce a readable copy of somebody's private messages that a
create-time-only check would never notice. A path that is a symlink, or exists
and is not a regular file, is refused outright. The `-wal` and `-shm` sidecars
inherit the mode of the main file, which is why it is narrowed before SQLite
opens it.

It is **deliberately not encrypted**, and the reasoning is worth stating rather
than leaving to be re-derived. The same state tree holds the Telethon `.session`
files, and a session file *is* the account: whoever can read one can read every
message in Telegram, live, with no archive in the picture. So encrypting the
archive would not raise the bar an attacker has to clear — it would only make
offline search and regular expressions impossible, which is the entire reason
the archive exists. What protects it is the same thing that protects the session
files next to it: the permission bits, the directory mode, and `.gitignore`.

Two consequences follow, and both are load-bearing:

- **Recognisable secrets are masked before they are written**, not only on the
  way out. A live read never persists anything, so masking at the edge of a
  result is enough there; the archive keeps what it is given, so a raw card
  number or login code would sit unencrypted on disk for as long as the archive
  is kept. `redact()` therefore runs on message text and sender names on the way
  in. The cost is that a regular expression cannot match what was masked.
- **Erasing is an operation.** `archive forget <chat_id>` removes a chat and
  every message of it, and it works even for a chat the read policy has since
  closed — see [`operations.md`](operations.md#telegram_archive_forget--tg-ai-archive-forget).
  It is a separate database from `state.db` precisely so that erasing every
  archived message cannot take the account registry, the pending plans and the
  rate-limit history with it.

The read allowlist still applies to everything in it, and it is applied **on the
read**: a chat archived while it was permitted stops answering the moment the
configuration stops permitting it.

## `safety`

The kernel decides in this order, and the order is the point:

1. the hard denylist above, which no configuration overrides;
2. the profile, which decides whether the class of action exists at all;
3. the capability's own `deny` list — `deny` always wins;
4. the capability's `allow` list, read fail-closed except where the table below
   says otherwise.

Every rule is an `allow`/`deny` pair of chat ids or `@username`s. A username may
match, but only after the peer has been resolved to a numeric id — handles are
reassignable, and a rule written against `@someone` would otherwise follow
whoever holds the name today.

### Which rule each capability consults

| Capability | Rule | Empty `allow` means | Used by |
| --- | --- | --- | --- |
| `read_chat` | `safety.read.chats` | **everything not denied** | `chat read`, `search` |
| `read_dm` | `safety.read.dms` | **nothing** | any read whose peer is a private chat |
| `read_members` | `safety.read.members` | everything not denied | `chat members` |
| `read_media` | `safety.read.media` | everything not denied | `media fetch` |
| `read_sessions` | `safety.read.sessions` | — (a switch, on by default) | `account sessions` |
| `send` | `safety.write.send` | **nothing** | send, reply, edit, delete, forward, mark-read |
| `join` | `safety.write.join` | **nothing** | join, leave |
| `admin` | `safety.write.admin` | **nothing** | create, invite, promote |
| `profile` | `safety.write.profile_enabled` | disabled | `account profile` |
| — | `safety.accounts` | all registered accounts | which accounts are reachable at all |

**A private chat is judged as a private chat regardless of which read operation
asked.** `read_chat`, `read_members` and `read_media` all fall back to `read_dm`
when the peer is a user, so a members or media lookup cannot be the way into a
conversation the `dms` list does not name.

### Enumeration

```yaml
safety:
  read:
    allow_dialog_enumeration: true   # default
    enumerate_dms: false             # default
```

Listing dialogs is how a reader finds what exists — and the cheapest possible
reconnaissance step. It is on by default because a tool that cannot list chats
cannot discover the ids everything else needs, but private conversations stay
out of the listing until `enumerate_dms` says otherwise, and they stay
unreadable until `safety.read.dms.allow` names them. Enumeration also has a
ceiling of its own (`MAX_DIALOG_SCAN`, 1000 dialogs walked) independent of the
caller's `limit`.

### A worked example

```yaml
safety:
  read:
    chats:
      deny: ["@somenoisychannel"]   # everything else stays readable
    dms:
      allow: [123456789]            # exactly one person's DMs are readable
    enumerate_dms: false
  write:
    send:
      allow: [123456789, -1001234567890]   # that person, and one group
    admin: { allow: [] }            # nothing may be created, invited or promoted
    join: { allow: [] }             # nothing may be joined or left
    profile_enabled: false
  accounts:
    allow: [work]                   # `personal` is registered but unreachable
```

## `limits`

Rolling windows, counted from rows in SQLite rather than from an in-memory
counter, with the slot reserved *before* the network call.

| Key | Default | Meaning |
| --- | --- | --- |
| `limits.window_seconds` | `3600` (min 60) | Width of the rolling window |
| `limits.sends_per_account` | `30` | Sends per account per window |
| `limits.sends_per_target` | `10` | Sends to one chat per window |
| `limits.sends_per_fleet` | `60` | Sends across every account per window |
| `limits.joins_per_account` | `3` | Joins per account per window |
| `limits.admin_ops_per_account` | `10` | Create/invite/promote per account per window |

Persistence is the design, not an implementation detail: an in-memory counter
would let anything able to restart the process — including whatever talked an
agent into sending in the first place — lift the ceiling for free. Joins are
capped hardest because mass joining is one of the behaviours Telegram itself
treats as abuse.

## `plans`

| Key | Default | Meaning |
| --- | --- | --- |
| `plans.ttl_seconds` | `86400` (min 60) | How long a pending plan stays applicable |
| `plans.max_pending` | `50` (min 1) | How many plans may await a decision at once |
| `plans.encrypt_bodies` | `true` | Encrypt message bodies held in the plan database |

`encrypt_bodies` **fails closed**: constructing the plan store with encryption
enabled and no key available is refused rather than quietly writing plan bodies
to the database in plaintext.

## `download`

| Key | Default | Meaning |
| --- | --- | --- |
| `download.max_file_bytes` | `104857600` (100 MiB) | Largest attachment `media fetch` will write |
| `download.total_quota_bytes` | `5368709120` (5 GiB) | Running total across the download directory |
| `download.timeout_seconds` | `120` | Per-download ceiling |

There is no setting for *where* a caller may write, because there is no such
parameter — see [`media fetch`](operations.md#telegram_media_fetch--tg-ai-media-fetch).

## `audit`

| Key | Default | Meaning |
| --- | --- | --- |
| `audit.enabled` | `true` | Write the log at all |
| `audit.include_bodies` | `false` | Store message text in full, not just its SHA-256 and length |
| `audit.rotate_bytes` | `67108864` (64 MiB) | Rotate the log past this size |

`include_bodies` is off by default because an audit trail that mirrors every
conversation is a second archive of the thing this project is trying to protect.
The log records that a message was sent, its digest and its length — enough to
prove what happened without becoming a copy of it.

## `secrets`

| Key | Default | Meaning |
| --- | --- | --- |
| `secrets.enabled` | `true` | Encrypt `api_hash`, proxy credentials and plan bodies at rest |
| `secrets.key_env` | `TGAI_SECRET_KEY` | Variable holding a base64 32-byte key |
| `secrets.key_file` | `paths.state/secret.key` | Where the key is read from when the variable is unset |
| `secrets.auto_create_key` | `true` | Generate a `0600` key file on first run if none exists |

The key is never a configuration *value* — only the name of the variable or the
path of the file that holds it. Set `auto_create_key: false` in a deployment
that manages keys elsewhere: a silently generated key is one nobody backs up,
and losing it makes every encrypted row unreadable.

**What this protects, stated honestly:** a copy of the state database or a stray
backup. It does not protect against a reader who can already run as this user —
they read the key the same way this process does. And the Telethon `.session`
file *is* the auth key in the clear, by the nature of MTProto session storage;
`0600` protects it from other users on the host, not from a disk-level backup.

## Telethon behaviour

| Key | Default | Meaning |
| --- | --- | --- |
| `telethon_flood_sleep_threshold` | `0` | Telethon's own sleep-and-retry on `FloodWaitError`, disabled |
| `telethon_request_retries` | `1` | One attempt; retries are driven by this project |

Both defaults exist for the same reason. Telethon sleeps through a flood wait
and retries the failed call automatically, which is exactly wrong for a
plan-and-apply tool: an automatic retry after a timeout is how one intended send
becomes two. A `FLOOD_WAIT` is returned to the caller with `retry_after` instead,
and an outcome that is genuinely unknown is left for a person to resolve.

## Proxies

Proxy configuration is **per account**, not a global setting: it is given when
the account is registered or signed in (`tg-ai account add --proxy …`,
`tg-ai account login --proxy …`) and stored, encrypted, in that account's row.
An account with no proxy egresses from the host's own address, shared with every
other account there — which is logged as a warning, because a fleet sharing one
address is how every account in it gets limited together.
