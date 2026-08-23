# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project intends
to follow [Semantic Versioning](https://semver.org/) once a `1.0.0` is tagged —
before that, breaking changes can happen on any `0.x` release.

## [Unreleased]

### Added

- `telegram_ai_cli.untrusted` — an explicit instruction boundary in tool output.
  Values a person outside this system wrote (message body, media caption, display
  name, chat title, inbox preview, forwarded-from name, profile text, admin rank)
  are delimited with `⟦untrusted⟧ … ⟦/untrusted⟧` at the point results are
  assembled, and the delimiters are published in `meta.untrusted_markers`.
  `meta.untrusted_content` said a *response* contained stranger-written text; it
  never said which spans, so a message body arrived indistinguishable from this
  project's own fields. **A sender cannot close the wrapper:** `⟦` and `⟧` are
  replaced with `[` and `]` inside wrapped content unconditionally, so no
  spelling of a forged marker in a message body can end the frame — structural,
  rather than a pattern match that has to enumerate every casing. Ids, dates,
  counts, links and `username` are never wrapped, so existing parsers keep
  working, and `untrusted.unwrap()` is the supported way back to a raw value.
  Strings that are *not* wrapped are still defanged, because a name-based
  allowlist is a promise the names are complete and they were not — a document's
  `mime_type` is typed by the uploader and carried a forged marker straight
  through on the first pass. It is now wrapped like anything else a person
  types, and `render.sanitize` defangs the delimiters on the terminal-facing
  paths (plan summaries, warnings, table cells) that get no wrapper of their own.
  `telegram_plan_*` results are inside the boundary too: their `summary` quotes
  chat titles and message bodies, and it is built outside `telegram_result`.
- `telegram_ai_cli.links` — `t.me` links parsed and produced. Public
  (`t.me/name/123`), private (`t.me/c/<internal>/<id>`) and forum-topic
  (`…/<topic>/<id>`, `?thread=`) forms, as a pure function with no network and no
  Telethon. Telegram's own deep links (`t.me/share`, `t.me/login/…`,
  `t.me/proxy`, …) are declined rather than read as a chat named `share`, and a
  path longer than Telegram produces is declined rather than truncated.
- Two link shapes are refused instead of interpreted, after the policy check:
  a message link into a one-to-one chat (`t.me/someone/123` opens a profile and
  addresses no message, so the number is not a message id there) and a comment
  link (`?comment=` addresses the channel's discussion group, a different chat).
- Message permalinks in output (`link` on every serialized message, and on the
  reactions payload). `null` where Telegram has no such address — a one-to-one
  chat or a basic group — rather than a well-formed URL that opens a profile and
  addresses no message.
- Reactions: per-emoji counts (`reactions`) on every serialized message,
  distinguishing `null` (no reaction block at all) from `[]` (nobody reacted),
  plus a `telegram_message_reactions` read tool / `tg-ai message reactions` for
  one message — counts, total, permalink and whichever recent reactors Telegram
  already attached. The full list of *who* reacted is never requested, and where
  it is unavailable the payload says so. Each row carries a `kind`
  (`emoji`/`custom_emoji`/`paid`/`empty`): two of Telegram's four reaction types
  carry no emoji, and without it a paid star reaction serialized as a blank one.
- Read state on `chat read`: `data.read_state` (the dialog's read pointers, from
  a call that acknowledges nothing) and `read_by_me` / `read_by_peer` per
  message. Outside a one-to-one chat Telegram tracks reading per member behind a
  separate privacy-controlled request this tool does not make — reported as
  `peer_receipts: false` with a reason, with the per-message field left `null`
  rather than `false`. Skippable with `include_read_state: false`.
- `topic_id` on serialized messages, so a forum message says which topic it is in.
- Project scaffolding: MIT license, `pyproject.toml` targeting Python 3.12+ with
  dependency floors (`telethon>=1.44,<2`, `click>=8.2,<9`, `pydantic>=2.11,<3`,
  `mcp>=2.0,<3`, and the rest), and `constraints.txt` pinning the exact versions
  the Docker image and CI build against.
- `telegram_ai_cli.errors` — the stable error taxonomy every surface returns.
  Codes are an explicit `StrEnum` rather than string literals scattered through
  the codebase, because a caller (human or model) branches on `code` and
  `retryable`, and renaming a code silently would be a breaking change to a
  contract nobody agreed to break.
- `telegram_ai_cli.envelope` — the one JSON response shape shared by the CLI and
  the MCP server, with `meta.truncated`/`truncated_reason` for anything cut for
  size and `meta.untrusted_content` marking any payload carrying text that came
  from Telegram, so a model reading it knows to treat it as data, not instruction.
- `telegram_ai_cli.config` — YAML configuration overlaid by `TGAI_`-prefixed
  environment variables (via `pydantic-settings`), with the safety, limits,
  plans, download, audit and secrets sections all typed and validated.
- `telegram_ai_cli.safety` — the capability-matrix policy kernel: read/write
  permission is decided per capability (`READ_CHAT`, `READ_DM`, `SEND`, `ADMIN`,
  `JOIN`, `PROFILE`, …) rather than from three generic allow/deny lists, because
  operations like `forward` (source *and* destination) or `create_group`
  (no chat id exists yet when it's planned) don't fit a flat model.
- `telegram_ai_cli.redact` — pattern-based masking for phone numbers, emails,
  card numbers (Luhn-checked), BIP39 seed phrases, TON and EVM addresses,
  Telegram login codes, and API-token-shaped strings, applied to any structure
  before it leaves the process.
- `telegram_ai_cli.render` — terminal-output sanitization: ANSI/OSC escape
  sequences, carriage returns, control characters and bidirectional-override
  characters are stripped from any Telegram-authored text before it is shown to
  a human, so what a person approves in `tg-ai plan show` is what will actually
  be sent.
- `telegram_ai_cli.audit` — a two-phase, append-only JSON-lines audit log
  (`attempt` before an RPC leaves, `outcome` after), file-locked and `fsync`'d
  per write, with control characters escaped on the way in so a logged value
  can't forge a second record.
- `telegram_ai_cli.limits` — persistent, SQLite-backed rate limiting (per
  account, per target, and fleet-wide), with the slot reserved before the
  network call and released only on a whitelisted class of exception that
  proves the call had no effect.
- `telegram_ai_cli.secretbox` — AES-256-GCM encryption at rest for `api_hash`,
  proxy credentials and plan bodies, keyed by an externally-held
  `TGAI_SECRET_KEY` (or a generated, `0600` key file) rather than anything
  stored alongside the ciphertext.
- `telegram_ai_cli.db` — the shared SQLite connection (plans and rate-limit
  history in one file), opened in WAL mode with `BEGIN IMMEDIATE` available for
  every check-then-write sequence, so two processes can't both pass a check and
  only then discover they disagree.
- `telegram_ai_cli.plans` — the plan store and state machine
  (`pending → applying → applied | failed | unknown_outcome`, plus
  `rejected`/`expired`), with `plan_id` as 128 bits from a CSPRNG, a
  `max_pending` quota, TTL-based expiry, and the `pending → applying` claim
  implemented as a conditional `UPDATE` inside a `BEGIN IMMEDIATE`
  transaction so two processes cannot both apply the same plan.
- `telegram_ai_cli.opspec` — the operation registry: one `Operation` per
  capability, holding its CLI path, its MCP tool name(s), its Pydantic input
  model, and either a `handler` (reads) or a `planner` (writes) — never both.
  `Registry.check_invariants()` runs at import and asserts, as code rather
  than convention, that no MCP tool name contains "apply."
- `telegram_ai_cli.context` — `OperationContext`, the one object every
  operation receives instead of reaching for globals (the safety kernel, the
  plan store, the rate limiter, the audit log, the account fleet), tagged with
  which surface (`cli` or `mcp`) invoked it.
- `telegram_ai_cli.cli` — the Click command line, built by walking the
  operation registry rather than hand-writing a command per operation; `plan
  list`/`show`/`apply`/`reject`, `schema` and `mcp` exist today as the
  registry-independent commands, ahead of any read or write operation.
- `telegram_ai_cli.mcp_server` — the stdio MCP adapter: it validates every
  call with `Operation.parse` (the SDK publishes schemas but does not enforce
  them), and returns errors as tool content rather than protocol failures so
  an MCP caller sees the same envelope the CLI prints.
- `telegram_ai_cli.accounts` — session, proxy and device-fingerprint storage,
  ported (not copied) from `telegram-save-private-photo-video`'s
  `tgsave/accounts/` behind this project's own storage layer, with the
  hardening fixes the design called for: `api_hash` actually encrypted,
  a failed `chmod` treated as a fatal error rather than logged and ignored,
  `O_NOFOLLOW` on every read of a profile/session/tdata path, and the lock key
  computed from `st_dev`+`st_ino` rather than a string path.
- `Dockerfile` (multi-stage; the runtime image runs as a non-root user whose
  UID/GID default to the host's own, so bind-mounted account material stays
  owned by whoever runs the container), `Dockerfile.test`, `.dockerignore`,
  and a `Makefile` (`build`, `test`, `lint`, `fmt`, `shell`) so nothing has to
  be installed on a contributor's host.
- `scripts/smoke_mcp.py` — a real MCP handshake over stdio
  (`initialize` → `notifications/initialized` → `tools/list`) rather than a
  bare import check, because the server can import cleanly and still hang,
  never respond, or exit wrong.
- `README.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `TASKS.md`, `docs/threat-model.md`, `llms.txt`, `CITATION.cff`,
  `docs/seo-geo-checklist.md` and `.env.example`.
- `tg-ai account add` and `tg-ai account login` — the onboarding the README has
  always documented and three of the code's own error messages pointed at, now
  actually registered as operations (`telegram_ai_cli/ops/accounts.py`). `add`
  registers an account without touching the network, or adopts already-authorised
  material with `--tdata` / `--session-file`; `login` runs the interactive phone
  login that `accounts/login.py` had implemented all along, reusing whatever the
  account's row already knows (phone, proxy, application credentials) so that a
  re-registration cannot silently drop the proxy an account was signed in through.
- `Effect.LOCAL_ADMIN`, a fourth effect class for operations that administer this
  installation's own account inventory. `Registry.check_invariants()` refuses to
  publish one as an MCP tool, so the two account commands are terminal-only as a
  checked property rather than as an omission somebody could "fix" later: signing
  in prompts a person for the code Telegram sent to their phone, and enrolling an
  account widens the very fleet every allowlist is written against.
- `docs/operations.md` and `docs/configuration.md` — the per-operation reference
  (arguments, defaults, effect and the policy each one consults) and the full
  `tgai.yaml` / `TGAI_*` reference, including the part that cannot be configured
  at all.

### Changed

- **A `t.me` link keeps its message number.** `chat` arguments used to resolve a
  link as a chat and drop everything else, turning "look at this message" into
  "look at this chat" silently. `chat read` now anchors its page at the message
  the link names (reported as `meta.anchor_message_id`), `media fetch` and
  `message reactions` take the message id from the link when it is not given
  explicitly, and a link plus a conflicting explicit id is refused rather than
  resolved by preference. A topic link reports `meta.topic_id` and warns that the
  page is not filtered to it; a search scoped by a message link warns that it
  covers the whole chat.
- `telegram_media_fetch`'s `message_id` is now optional — required unless the
  `chat` argument is a link that names the message.
- The MCP server's instructions describe the untrusted markers, so a client is
  told what the delimiters mean instead of inferring it.

### Fixed

- `TGAI_`-prefixed environment variables actually override the YAML file, as the
  README, `.env.example` and the new configuration reference all say they do.
  `load_settings` passes the file in as init keyword arguments, and
  pydantic-settings ranks those *above* the environment by default — so
  `TGAI_PROFILE=readonly` could not take away a `profile: plan` written on disk.
  `Settings.settings_customise_sources` now puts the environment first, and
  `tests/test_config.py` covers both directions plus the merge (one override
  must not empty the allowlists it does not name).
- Registering an account holds that account's session lock
  (`AccountRegistry.register_phone_login`), like every other change that can
  replace a row. Writing over a registration while a client is connected
  underneath it corrupts the session file that client is using.
- Error suggestions name a command that exists and can be typed as written:
  `tg-ai account login --label <name>`, matching the option the generated CLI
  actually takes (`ops/_client.py`, `accounts/registry.py`). The positional form
  they used before would have failed with "unexpected extra argument" even once
  the command existed.
- README command names now match the CLI: `tg-ai chats` (not `chat list`),
  `tg-ai fleet` (not `fleet status`), `chat promote` (not `admin promote`); and
  the MCP tool table no longer lists `telegram_plan_status` and
  `telegram_plan_list`, which the registry does not publish — `tg-ai plan list`
  and `plan show` are terminal commands, on the same side of the line as
  `plan apply`.
- The MCP adapter is built against the installed SDK's constructor-handler API
  (`Server(..., on_list_tools=..., on_call_tool=...)`) rather than the 1.x
  decorator form, which the low-level `Server` no longer provides. Nothing
  imported wrongly, so the whole suite passed while any attempt to actually
  serve MCP died with `AttributeError`; `tests/test_mcp_server.py` now builds a
  server and exercises both handlers, so an SDK API change fails in the unit
  suite rather than only in the stdio smoke test.
- The runtime image builds the real package in a directory the dependency-only
  placeholder never touched. Sharing one left setuptools' `build/lib/` holding
  an empty `__init__.py` stamped at build time, and `build_py` copies a source
  file only when it is newer than its destination — so the real `__init__.py`
  lost that comparison and never entered the wheel. The image built and ran as
  non-root, then failed on first import with a missing `__version__`.

### Security

- `777000` (Telegram Service Notifications, where login codes and 2FA resets
  arrive) and Saved Messages are excluded as constants in `config.py`, checked
  before any allow/deny list in `safety.py` — no configuration value can reopen
  either.
- Every write list (`send`, `admin`, `join`) and the direct-message read list
  are fail-closed by default: an empty `allow` means nothing is permitted for
  that capability, not everything. Reading groups and channels is the one
  intentional exception, open by default so the tool is usable before it has
  been configured.
- Rate limits persist across a process restart by design — an in-memory counter
  would let anything able to restart the process (including whatever talked an
  agent into sending in the first place) lift the ceiling for free.
- The `.gitignore` inherited from the standard GitHub Python template did not
  cover `sessions/`, `accounts/`, `tdata*/`, `*.session*`, `*.api.json` or
  `*.string` — a real gap for a project that holds live MTProto auth keys. These
  patterns, plus the local state directory, the audit log and `secret.key`, are
  now excluded explicitly — and anchored to the repository root with a leading
  slash, so that an unanchored `accounts/` cannot also match the
  `telegram_ai_cli/accounts/` source package (which had silently excluded the
  whole module from both git and the linter).
- `plans.encrypt_bodies` fails closed: constructing a `PlanStore` with body
  encryption enabled but no key available is refused, rather than quietly
  writing the plan body to the database in plaintext.
- Redaction is applied to email addresses of any domain depth, before the
  card and phone rules — an address is masked whole rather than having a
  numeric local part rewritten as a phone number inside an intact domain.
