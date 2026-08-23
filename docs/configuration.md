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
| `paths.uploads` | `…/telegram-ai-cli/uploads` | The only directory a file may leave from — `message send-file` sends from it, `chat set-photo` publishes from it |
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

## `ledger`

What has already been sent, so that it is not sent again. One row per applied
outbound message in `state.db`, keyed by a fingerprint of what the recipient
sees — the account, the operation, the numeric peer id, the words after
cosmetic whitespace inside each line is normalised away (line breaks survive:
Telegram renders them), the sha256 of any attachment, and the choices that
change how it all renders: which message a reply quotes, whether a link expands
into a preview, whether a file arrives as a compressed photo or as the original
document and under what name, and whether a forward keeps its author's name.
`silent` is left out on purpose — it decides whether a phone makes a sound, not
what the message says.

| Key | Default | Meaning |
| --- | --- | --- |
| `ledger.window_seconds` | `21600` (6 hours, min 0) | How long an identical send to the same peer is refused as a duplicate. `0` turns the check off |

The failure this exists for is not a race — one plan cannot be applied twice, the
plan store's atomic claim sees to that — but an *amnesia*: an agent in a new
session, with no memory of the last one, plans and applies a message it already
sent, and the person on the other end sees the same words twice with no way to
tell which run produced them. A duplicate is **refused**, never silently skipped:
a caller that cannot tell "sent" from "quietly didn't" reports success for a
message nobody received.

Six hours is chosen deliberately. The duplicate worth catching is a *re-run* — a
restarted process, a retried script, a fresh session — and those happen within
hours; a message on a daily rhythm is a legitimate repeat and must never be
caught, which a six-hour window cannot reach even with hours of drift. A longer
window trades a rare catch for routine false refusals, and every false refusal
teaches whoever hits it to set `allow_duplicate` by reflex, which is how the
check stops working.

Repeating on purpose is a field on the plan, not a flag on `plan apply`:
`allow_duplicate` (`--allow-duplicate`) on `message.send`, `message.reply`,
`message.send_file` and `message.forward`. It is never the default, it prints
`DELIBERATE REPEAT` in the approval preview so whoever approves knows what they
are approving, and it is not part of the fingerprint — approving one repeat does
not make every later copy invisible.

## `download`

| Key | Default | Meaning |
| --- | --- | --- |
| `download.max_file_bytes` | `104857600` (100 MiB) | Largest attachment `media fetch` will write |
| `download.total_quota_bytes` | `5368709120` (5 GiB) | Running total across the download directory |
| `download.timeout_seconds` | `120` | Per-download ceiling |

There is no setting for *where* a caller may write, because there is no such
parameter — see [`media fetch`](operations.md#telegram_media_fetch--tg-ai-media-fetch).

## `upload`

| Key | Default | Meaning |
| --- | --- | --- |
| `upload.max_file_bytes` | `104857600` (100 MiB) | Largest file `message send-file` will send. Capped at `2147483648` (2 GiB), Telegram's own ceiling |
| `upload.allow_downloads_dir` | `false` | Whether `paths.downloads` may also be sent *from* |
| `upload.timeout_seconds` | `300` (min 1) | Ceiling on the transfer itself, replacing the applier's 60-second per-RPC one |

There is no setting for *which paths* a caller may send, because the answer is
fixed: files come from `paths.uploads`, symlinks are resolved before the
directory is checked, and a relative name is read from the outbox rather than
from the process's working directory. `allow_downloads_dir` is the one widening
available, and it is off because a downloaded file is one a stranger chose —
re-posting it into another chat should be an operator's decision taken here, not
a tool call's. The reasoning in full is in
[Sending a file](operations.md#sending-a-file-where-the-bytes-may-come-from).

A file over `upload.max_file_bytes` is refused from the descriptor it is hashed
on — at planning time and again at apply time — rather than discovered partway
through a transfer. Configuring more than Telegram accepts is refused as a
validation error, because it could only move the failure later.

Two requirements on the directory itself, both fail-closed:

- **`paths.uploads` must be absolute.** It is not merely a location, it is the
  allowlist deciding which files may leave; a relative value resolves against
  whatever directory this process was started in, and `Path("")` is `Path(".")`.
  A relative one is rejected when the settings are built.
- **Only its owner may write into it.** Nothing here creates the outbox for
  you, and a `0777` one would mean "whatever anybody on this machine dropped
  in" rather than "what the operator put there". The two write bits are handled
  differently, on purpose:
  - **World-writable (`o+w`) is refused** with `INSECURE_PERMISSIONS` and the
    `chmod` that fixes it. No default umask produces it, so it is a deliberate
    setting and this tool does not overrule those.
  - **Group-writable (`g+w`) is repaired**: the group write bit is removed and
    the send goes ahead. `umask 002` — the default wherever *user private
    groups* are in use, which is Ubuntu out of the box and Debian and the RHEL
    family through `USERGROUPS_ENAB`, and which gives each user a single-member
    group — makes every directory you create `0775`, so refusing that made the
    outbox unusable out of the box for most Linux users over a "group" with
    nobody else in it. Asking whether the
    group is safe cannot be answered honestly from a process (`gr_mem` lists
    only supplementary members, so a group whose members all have it as their
    *primary* gid reads back empty), so the bit is removed rather than judged.
    Only the write bit changes; group and other read/execute are left alone. If
    the `chmod` fails — an outbox this user does not own — the refusal stands.

  If you deliberately share an outbox with a group, this tool is not the way to
  do it: point `paths.uploads` at a directory this user owns and copy files in.

## `mcp`

| Key | Default | Meaning |
| --- | --- | --- |
| `mcp.tools` | unset | Which tool names the MCP server publishes. Unset = all of them |

```yaml
mcp:
  tools:
    - telegram_chats
    - telegram_chat_read
    - telegram_search
```

`TGAI_MCP__TOOLS='["telegram_chats","telegram_chat_read"]'` — a JSON list, like
every other list-valued setting.

**A tool that is never published cannot be invoked by a prompt injection.** That
is the whole claim, and it is worth being precise about what it does *not* say:
this is a second, coarser layer in front of the per-peer rules, not a
replacement for them. It narrows which of this server's tools exist as far as
the client is concerned; the profile, the capability matrix and the hard
denylist all still run underneath, and the gate can never reach past them.

Three behaviours follow, and each is a deliberate answer to a question that has
a defensible opposite:

- **Unset means every tool**, exactly as before this setting existed. The gate
  is opt-in. The empty-`allow`-means-nothing convention elsewhere in this file
  protects against a list that was empty because nobody filled it in; here the
  same default would make a fresh install publish nothing at all, and the first
  thing anybody did about that would be to paste a list they had not thought
  about.
- **`mcp.tools: []` publishes nothing.** Set to an empty list, it *has* been
  thought about, and it reads like every other empty allow list here.
- **An unknown name refuses to start.** `tg-ai mcp` exits with
  `error [INVALID_INPUT] mcp.tools names 1 tool(s) this server does not
  publish: telegram_chatz`, naming every unknown entry at once. A silent drop
  would turn a typo into a tool that is missing for a reason nobody can see, and
  a warning on the stderr of a stdio server is a line the client swallows. It is
  the same fail-loud rule as a relative `paths.uploads`.

The gate filters the **call path** as well as the tool list. A hidden tool
invoked by name is refused with `FORBIDDEN_BY_ALLOWLIST`; a filter that only
hides is cosmetic, since a tool name is a guessable string.

`tg-ai schema` prints every publishable name. Note that a write operation has no
direct tool — it appears as `telegram_plan_*` — so naming `telegram_message_send`
here is a configuration error, not a way to obtain one.

### Client roots and where an MCP call writes

If the MCP client advertises [roots](https://modelcontextprotocol.io/) — the
directories it sanctions — an operation that writes to this machine is refused
when the path *it* writes is outside every one of them, naming the path that was
refused. Nothing is redirected: a download that lands somewhere other than where
it was configured to land would leave the quota, the artifact ids and the
operator's own expectations describing a different directory.

**Each operation is judged by its own destination**, which it declares
(`Operation.local_path`, checked at import for every `local_write`):

| Operation | Path checked |
| --- | --- |
| `media fetch` | `paths.downloads` |
| `archive sync`, `archive forget` | `paths.archive` |

That distinction is not cosmetic. All three are `local_write`, and checking
them against one directory would refuse an archive write over a download
directory it never touches — and let one through on the strength of a directory
it never touches either.

There is no setting for this. Roots arrive over the protocol, they can only
narrow what the configuration already permits, and the two absences mean
different things:

| The client | Means | Effect |
| --- | --- | --- |
| never declared the roots capability | there is nobody to ask | unconstrained; the configured paths stand |
| declared it and answered with an empty list | it was asked, and sanctions none | every local write refused |
| declared it and failed to answer | unknown | refused — it said it does roots, and a transport error is not permission |

A root that is not a usable local directory — a non-`file://` scheme, another
host, a relative path, an embedded NUL — is dropped rather than guessed at. If
that leaves no roots, the result is the empty-list row above: a refusal.

Containment is decided on canonical paths — `realpath` first, comparison after —
so a symlink out of a root and a `..` in the middle of a path are both refused,
and `/srv/data-evil` is not inside `/srv/data`. It is the same check
`message send-file` applies to the outbox, shared rather than written twice
([`roots.py`](../telegram_ai_cli/roots.py)).

The check runs before the account is opened, and it is a check on a path, not a
lock on a directory: someone who can already replace `paths.downloads` with a
symlink on this host between the check and the write can still redirect it, the
same as they could before roots existed. That is the local-user trust boundary
the [threat model](threat-model.md) draws, not something roots move.

## `transcribe`

Local speech-to-text for voice messages and audio files. **Optional in the
strongest sense**: it lives in a second Docker image that `make build` does not
build, the published package gains no dependency from it, and an installation
that never runs `make transcribe-image` sees no change in behaviour beyond one
extra tool that refuses with an explanation.

| Key | Default | Meaning |
| --- | --- | --- |
| `transcribe.image` | `telegram-ai-cli-transcribe:latest` | The optional image to run |
| `transcribe.model_cache` | `<state>/whisper-models` | Where the model weights live, mounted into the container read-only |
| `transcribe.max_audio_seconds` | `600` (10 min) | Longest audio accepted, checked twice |
| `transcribe.timeout_seconds` | `900` (15 min) | Wall clock for the whole container |
| `transcribe.docker_binary` | `docker` | The client to invoke |
| `transcribe.run_as` | *this process's `uid:gid`* | `--user` for the container. Root is refused |

**There is no API key, no endpoint and no fallback**, because there is no remote
service. A voice message is somebody's actual voice; the decision was that it
never leaves the host, and it is enforced rather than promised — the
transcribing container runs with `--network none`. A process with no network
interface cannot upload anything, whatever a future dependency of it decides to
do.

**The model is not configurable.** It is baked into the image at `small`, which
is clearly better than `base` on Russian and still around half a gigabyte. A
setting here would mean a cache of several models, a rule for choosing between
them and something to keep them current — a subsystem, in place of a feature
that shells out to one container.

**Two commands, once, per host:**

```bash
make transcribe-image   # build the optional image
make transcribe-model   # download the weights into transcribe.model_cache
```

The download is a separate step *because* transcription has no network. It is
the only invocation of that image that ever gets one. Until both have run,
`media transcribe` refuses with `TRANSCRIBER_UNAVAILABLE` and names the command
that is missing — the image, or the model.

**`max_audio_seconds` is checked twice, and the first check is not trusted.**
Telegram reports a duration with the attachment, so the host refuses on it
before spending the transfer; but that figure is metadata the *uploader*
supplied. The container measures the file it actually decodes and refuses again,
which is the check that holds when the declared duration is a lie. Ten minutes
covers a voice message a person recorded by hand; past that it is a recording,
and transcribing recordings is a batch job rather than a chat read.

**The second check is the one that holds, and it needs a memory limit to be
safe.** Whisper decodes the whole file before it can report a duration, so the
in-container check runs *after* the allocation it bounds — and
`download.max_file_bytes` permits 100 MiB of opus, which is many hours and
several gigabytes once decoded. The container therefore runs with
`--memory`/`--memory-swap` (2 GiB) and a `--pids-limit`, so a fabricated
duration on a long file is an exit status rather than an out-of-memory host.
Neither is a setting: they bound a fixed workload with a fixed model.

**`timeout_seconds` is deliberately much larger than a request usually takes.**
The worst case it has to survive is `max_audio_seconds` of audio on a loaded CPU
at real time, plus a cold container start and a model load. A ceiling tight
enough to catch a hung container would also kill legitimate work.

A timeout **removes the container**, rather than only killing the `docker run`
client: `--rm` disposes of a container that has exited, and a timed-out one has
not — left alone it keeps exactly the CPU and memory the timeout existed to
reclaim. The run is named for that reason.

**`run_as` cannot reintroduce root.** `0` as either half is refused, as is
anything that is not numeric `uid:gid`; an empty value means "unset" and falls
back to this process's own ids. The check is on the value that reaches `--user`,
not on the default, because putting it in the default branch is what let a
config key turn the guarantee off.

**`model_cache` must already exist** when a transcription runs, and a missing one
is refused by name. `docker -v` *creates* a missing absolute source as an empty
directory instead of failing, which would put a directory on disk that the
operation never declared and then fail obscurely for want of a model.

**What the container is given.** One file, bind-mounted read-only at a fixed
path — not the download directory, which holds every attachment this account
ever fetched — plus the model cache, mounted separately and **also read-only**,
because the weights are put there by `make transcribe-model` and the
transcribing container has no reason to write anything at all. That is why
`media.transcribe` declares `paths.downloads` as its only local destination. It
runs as this process's own `uid:gid`, never as root: a container writing to a
bind mount as root leaves files behind the host user cannot delete, which is
exactly the trap `make transcribe-model` — which *does* write — has to avoid.

`transcribe.model_cache` is deliberately **not** a `paths.*` entry: nothing in
the tool writes it, so it is not one of the destinations an MCP client's roots
are checked against.

The transcript is somebody else's sentence and crosses the same trust boundary
as a message body — see
[`media transcribe`](operations.md#telegram_media_transcribe--tg-ai-media-transcribe).

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
