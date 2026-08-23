# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project intends
to follow [Semantic Versioning](https://semver.org/) once a `1.0.0` is tagged —
before that, breaking changes can happen on any `0.x` release.

## [Unreleased]

### Added

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
