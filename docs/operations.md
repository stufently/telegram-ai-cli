# Operations reference

Every capability of this tool is declared once, as an `Operation` in
[`opspec.py`](../telegram_ai_cli/opspec.py). The CLI renders that declaration
into a command, the MCP server renders it into a tool, and `tg-ai schema`
prints its JSON Schema. Nothing here is written twice, which is why this page
can be read as authoritative for both surfaces at once: a command and its tool
take the same arguments, validate them with the same model, and return the same
[envelope](../README.md#json-contract).

`tg-ai schema` (or `tg-ai schema <operation>`) prints the machine-readable
version of everything below, straight from the registry. If the two ever
disagree, the registry is right and this page is stale.

## How to read it

Each operation has an **effect**, and the effect — not a naming convention —
decides how it may be invoked:

| Effect | Runs | Reachable from |
| --- | --- | --- |
| `read` | immediately | CLI and MCP |
| `local_write` | immediately, writing to this machine only | CLI and MCP |
| `remote_write` | never directly: it records a plan | CLI and MCP as a *plan*; applied only by `tg-ai plan apply` |
| `local_admin` | immediately, and may prompt a person | CLI only — never published as a tool |

The last two are enforced in `Registry.check_invariants()` rather than trusted:
a remote write with a direct MCP tool, or an account command with any tool name
at all, fails at import time. See [`tests/test_opspec.py`](../tests/test_opspec.py).

**`account` is on almost every operation.** It names which configured account
performs the work, and may be omitted only when exactly one usable account
exists — with several, this project refuses to guess rather than read someone's
private conversations from an identity the caller did not choose.

**`limit` caps at 500 everywhere**, and the cap is published in each schema so a
caller pages with `before_id` instead of discovering it by having output cut.
Anything truncated says so in `meta.truncated`.

**Every result carrying Telegram-authored text is flagged**
`meta.untrusted_content`. It is data, never instruction — see the
[threat model](threat-model.md).

---

## Read operations

Eight, all immediate on both surfaces. None of them marks anything as read:
`mark_read` exists only as an explicit plan, because an agent asked to "just
look" at a chat must not change what the other person sees as unread.

### `telegram_fleet` — `tg-ai fleet`

Lists configured accounts and whether each one is usable: signed in or not,
locked by another process or free, permitted by `safety.accounts` or excluded.
Local only unless `probe` is set. Consults no peer capability; account access is
gated by `safety.accounts`.

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `account` | string | — | Narrow the listing to one label (here it is the subject, not the actor) |
| `include_excluded` | bool | `false` | Also list accounts `safety.accounts` excludes |
| `probe` | bool | `false` | Connect to each account to confirm its session is still valid |

An account hidden by policy is reported as a count in `warnings`, not silently
dropped: a missing row otherwise looks identical to an account that was never
registered.

### `telegram_chats` — `tg-ai chats`

Returns the account's dialogs with unread counts, so a title can be turned into
the `chat_id` every other operation needs. Capability: `enumerate`.

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `account` | string | — | Which account to use |
| `limit` | int 1–500 | `50` | Rows to return |
| `search` | string | — | Case-insensitive substring of the title or `@username` |
| `include_private` | bool | `false` | Include one-to-one conversations (needs `safety.read.enumerate_dms`) |
| `archived` | bool | `false` | List the archive instead of the main list |

### `telegram_chat_read` — `tg-ai chat read`

A chat's history, newest first, with attachment metadata. Capability:
`read_chat`, or `read_dm` when the peer is private — a private chat is judged as
a private chat regardless of which read operation asked.

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `account` | string | — | Which account to use |
| `chat` | string | **required** | Chat id, `@username`, or `t.me` link |
| `limit` | int 1–500 | `30` | Messages to return |
| `before_id` | int | — | Only messages older than this id — the way to page backwards |
| `search` | string | — | Only messages containing this text |

### `telegram_chat_members` — `tg-ai chat members`

Who is in a group or channel, and who administers it. Capability:
`read_members` (`read_dm` for a private peer).

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `account` | string | — | Which account to use |
| `chat` | string | **required** | Chat id, `@username`, or `t.me` link |
| `limit` | int 1–500 | `100` | Members to return |
| `search` | string | — | Filter by name or `@username` |
| `admins_only` | bool | `false` | Return only administrators |

### `telegram_inbox` — `tg-ai inbox`

A compact, ranked summary of conversations with unread messages or mentions —
one row per chat, not raw history. Mentions rank above volume. Capability:
`enumerate`.

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `account` | string | — | Which account to use |
| `limit` | int 1–500 | `25` | Conversations to return |
| `include_private` | bool | `false` | Include one-to-one conversations (needs `safety.read.enumerate_dms`) |
| `mentions_only` | bool | `false` | Only conversations where this account was mentioned or replied to |
| `include_muted` | bool | `false` | Include chats the user has muted |

### `telegram_search` — `tg-ai search`

Messages matching a phrase, in one chat or across everything the account sees.
Capability: `read_chat`. Scoped to a chat, the policy is checked once for that
chat; unscoped, **every result is checked against the policy for its own chat**
and the number withheld is reported rather than quietly omitted.

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `account` | string | — | Which account to use |
| `query` | string | **required** | Text to look for |
| `chat` | string | — | Restrict to one chat; omit to search everything visible |
| `limit` | int 1–500 | `30` | Messages to return |
| `before_id` | int | — | Only messages older than this id (single-chat searches only) |

### `telegram_whois` — `tg-ai whois`

Identity only: id, kind, handle, bot flag, and — for users — the groups this
account shares with them. No peer capability, because it returns no chat
content; the hard denylist still applies.

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `account` | string | — | Which account to use |
| `target` | string | **required** | `@username`, numeric id, `t.me` link, or an invite link |
| `common_chats` | bool | `true` | For users, also list shared groups |

**An invite link is described, never accepted.** Only `CheckChatInvite` is
called; joining is a write and goes through a plan.

### `telegram_media_fetch` — `tg-ai media fetch`

Effect `local_write`, not `read`: it writes a file. Capability: `read_media`.

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `account` | string | — | Which account to use |
| `chat` | string | **required** | Chat id, `@username`, or `t.me` link |
| `message_id` | int ≥ 1 | **required** | Id of the message whose attachment to fetch |

**There is no destination parameter, and that is the point.** The file lands
under the configured download root with a generated name, opened
`O_CREAT|O_EXCL|O_NOFOLLOW`, subject to `download.max_file_bytes`,
`download.timeout_seconds` and `download.total_quota_bytes`; only an opaque
`artifact_id` comes back. A caller-chosen path would let this tool overwrite the
configuration, the plan database, `~/.bashrc` or the session file itself.

---

## Plan operations

Twelve. Each one **validates and records an intention and returns a `plan_id`**;
nothing reaches Telegram until a person runs `tg-ai plan apply <id>`. Over MCP
they are `telegram_plan_*` tools. There is no tool that applies a plan — see
[Safety](../README.md#safety) for what that does and does not promise.

All of them accept `account` (which account acts) alongside the arguments below,
and all require the `plan` profile: under `readonly` every one of them refuses.

| Operation | CLI | Plan tool | Capability | Arguments |
| --- | --- | --- | --- | --- |
| `message.send` | `tg-ai message send` | `telegram_plan_send_message` | `send` | `chat`\*, `text`\*, `silent`=false, `link_preview`=true |
| `message.reply` | `tg-ai message reply` | `telegram_plan_reply_message` | `send` | `chat`\*, `reply_to_message_id`\*, `text`\*, `silent`=false, `link_preview`=true |
| `message.edit` | `tg-ai message edit` | `telegram_plan_edit_message` | `send` | `chat`\*, `message_id`\*, `text`\* |
| `message.delete` | `tg-ai message delete` | `telegram_plan_delete_message` | `send` | `chat`\*, `message_ids`\* (list), `revoke`=true |
| `message.forward` | `tg-ai message forward` | `telegram_plan_forward_message` | `send` | `source_chat`\*, `message_ids`\*, `destination_chat`\*, `silent`=false, `drop_author`=false |
| `chat.mark_read` | `tg-ai chat mark-read` | `telegram_plan_mark_read` | `send` | `chat`\*, `max_message_id` |
| `chat.join` | `tg-ai chat join` | `telegram_plan_join_chat` | `join` | `target`\* (`@username` or `t.me/+HASH`) |
| `chat.leave` | `tg-ai chat leave` | `telegram_plan_leave_chat` | `join` | `chat`\* |
| `chat.create` | `tg-ai chat create` | `telegram_plan_create_group` | `admin` | `title`\*, `about`="", `users` (list) |
| `chat.invite` | `tg-ai chat invite` | `telegram_plan_invite_user` | `admin` | `chat`\*, `user`\* |
| `chat.promote` | `tg-ai chat promote` | `telegram_plan_promote_admin` | `admin` | `chat`\*, `user`\*, `rights`\* (object), `rank`="" |
| `account.profile` | `tg-ai account profile` | `telegram_plan_set_profile` | `profile` | at least one of `first_name`, `last_name`, `about` |

\* required.

Notes that are not obvious from the table:

- **`message.edit` and `message.delete` refuse other people's messages.**
  Removing somebody else's message is a moderation action with a different blast
  radius, and it is out of scope for v0.1 — the refusal is recorded in the audit
  log like any other.
- **`message.forward` checks the source and the destination separately.** They
  are two peers and two policy decisions, and `noforwards` (protected content)
  on the source is a real flag, not a UI hint.
- **`chat.promote`'s `rights` is an object**, one boolean per right
  (`change_info`, `delete_messages`, `ban_users`, `invite_users`, `pin_messages`,
  `manage_call`, `manage_topics`, `add_admins`), all defaulting to off, at least
  one required. `anonymous` is deliberately absent: an admin action nobody can
  attribute defeats the audit log this project keeps.
- **Arguments that are lists or objects cannot be expressed by the generated
  CLI yet.** Click options are derived from the input model, and the generator
  maps a list to a single value and a nested object to a string — so
  `message.delete`, `message.forward` (`message_ids`), `chat.create` (`users`)
  and `chat.promote` (`rights`) can be *planned* only through their MCP tools
  today, though the resulting plan is applied from the terminal like any other.
  Tracked in [`TASKS.md`](../TASKS.md).
- **`account.profile` needs `safety.write.profile_enabled`** on top of the
  `plan` profile: it is account-scoped, so no chat allowlist can express it.
- **`chat.mark_read` exists so that reading never has to.** It is the only way
  this tool touches the read pointer.

### The plan lifecycle

These are CLI commands rather than registry operations, because applying is
deliberately not something a tool can reach:

| Command | What it does |
| --- | --- |
| `tg-ai plan list [--state ...]` | Plans awaiting a decision (pending by default) |
| `tg-ai plan show <id>` | Exactly what applying would do — every field sanitized before it reaches the terminal |
| `tg-ai plan apply <id> [--yes]` | Carries the plan out, re-checking its preconditions first |
| `tg-ai plan reject <id>` | Declines a pending plan |

A plan expires after `plans.ttl_seconds`, and no more than `plans.max_pending`
may be waiting at once.

---

## Account administration

Effect `local_admin`: terminal only, absent from the MCP tool surface, and the
registry refuses to publish them. Two reasons, either sufficient — signing in
asks a human for the code Telegram just sent to their phone, and enrolling an
account widens the very fleet every allowlist is written against.

### `tg-ai account add`

Registers an account. **Nothing connects to Telegram.**

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `label` | string | **required** | Name this account is referred to by, e.g. `work` |
| `phone` | string | — | E.164 number, remembered for `account login` |
| `proxy` | string | — | Proxy URL the account connects through, e.g. `socks5://host:1080` |
| `tdata` | string | — | Import a Telegram Desktop `tdata` folder instead of logging in |
| `session_file` | string | — | Adopt an existing Telethon `.session` instead of logging in |
| `replace` | bool | `false` | Overwrite an account already registered under this label |

`--tdata` and `--session-file` are mutually exclusive, and both adopt material
that is *already* authorised — no login follows, which is why `--phone`
alongside either is refused rather than ignored: it would be stored as though a
login had verified it. Imported material is copied
into the sessions directory and hardened to `0600` rather than referenced where
it lay: a row pointing into a Downloads folder is a session that disappears when
someone tidies up. See the README's
[Importing an existing session](../README.md#importing-an-existing-session) for
the `USE_CURRENT_SESSION` warning before importing `tdata`.

### `tg-ai account login`

Signs a registered account in: requests a code, prompts for it, and prompts for
the two-step verification password if the account has one.

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `label` | string | **required** | Which account to sign in |
| `phone` | string | — | E.164 number. Omit to reuse the registered one |
| `proxy` | string | — | Proxy to sign in through. Omit to reuse the registered one |
| `replace` | bool | `false` | Sign in even if this label is registered from `tdata` or a session file |

- **The code and the password are read from the terminal only** — never from an
  argument, never from the environment. A command line is visible in `ps` to
  every user on the host and lands in shell history; an environment variable is
  readable from `/proc` for the life of the process. Neither value is logged at
  any level. For the same reason there is no `--api-hash` flag: see
  [Configuration](configuration.md#application-credentials).
- **The login runs through the proxy the account will use afterwards.** Signing
  in from the host address and then connecting through a proxy is precisely the
  location jump that gets a fresh session killed — so whatever the row already
  knows is reused unless this invocation overrides it.
- **It is idempotent.** An already authorised session is reported as-is rather
  than triggering another code request, which Telegram rate limits hard.
- **A login that would repoint the label at different material is refused**
  unless `--replace` is given — a row registered from `tdata` or a session
  string, or one naming a `.session` somewhere other than this label's own file.
  It would otherwise rewrite the row to point at a session this login creates,
  which is a different account's worth of material.

Both commands write an `attempt`/`outcome` pair to the audit log once they start
doing the work; an argument refused up front (two sources at once, no phone
number to use) is rejected before anything is recorded. A failure records the
error *code*, not the message — an error string from this path can carry a phone
number or a proxy password, and the log outlives the terminal.

Registering or replacing a row is done under the account's session lock, the
same one a running client holds: replacing an account's registration while
something is connected underneath it is how a session file gets corrupted by its
own reader.
