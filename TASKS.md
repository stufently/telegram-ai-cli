# Tasks

Backlog only — open and future work, not a progress log. See
[`docs/superpowers/specs/2026-08-23-telegram-ai-cli-design.md`](docs/superpowers/specs/2026-08-23-telegram-ai-cli-design.md)
for the full design these items implement, and `CHANGELOG.md` for what has
already shipped.

## Core

The safety kernel (`config.py`, `safety.py`, `redact.py`, `render.py`,
`audit.py`, `limits.py`, `secretbox.py`, `db.py`, `errors.py`, `envelope.py`)
landed first, per the design's ordering (§11) — read/write operations are
written against it, not the other way around. Since then, `plans.py`
(the plan store and state machine), `opspec.py` (the operation registry and
its startup invariants — see the "no tool may apply a plan" check inside
`Registry.check_invariants`), `context.py` (`OperationContext`, what every
operation is handed), `cli.py` (Click, plus the standing `plan
list/show/apply/reject/schema/mcp` commands) and `mcp_server.py` (the stdio
adapter) have all landed too. `accounts/` also exists in full (`models.py`,
`store.py`, `loader.py`, `lock.py`, `api_profile.py`, `fs.py`, `proxy.py`,
`sources.py`, `paths.py`) — the port from `telegram-save-private-photo-video`
described in the design's §2/§6.8/§6.9 is done, not just planned.

What's still missing from core:

- [ ] `apply.py` — `cli.py`'s `plan apply` command already imports
      `from .apply import apply_plan` lazily; the module itself doesn't exist
      yet. This is the one function that actually re-verifies preconditions
      and calls Telethon to carry out a plan — see §6.3's "all checks repeat
      at apply, not only at plan creation."
- [ ] `ops/accounts.py`, `ops/chats.py`, `ops/messages.py`, `ops/contacts.py`,
      `ops/admin.py` — the actual read handlers and write planners. Only the
      shared plumbing exists so far: `ops/_common.py` (the `ReadInput` base,
      redaction-at-assembly, the Telethon-exception-to-`TelegramAIError`
      translator) and `ops/_client.py` (the one place that turns an account
      label into a connected Telethon client). Until these land, `tg-ai` has
      no read commands and no write commands at all — only `plan
      list/show/apply/reject`, `schema` and `mcp` exist as CLI commands today,
      and `tools/list` over MCP returns an empty tool set.

## Tests

None of the twelve test files listed in the design (§10) exist yet. In
particular, before any operation code lands:

- [ ] `test_safety.py`, `test_denylist.py` — lock down the policy kernel's
      current behaviour with tests, since it's the piece every later operation
      depends on.
- [ ] `test_limits.py` — persistence across a simulated restart, reservation
      before the network call, release only on the whitelisted exception
      classes.
- [ ] `test_audit.py`, `test_redact.py`, `test_render.py` — the two-phase
      write, the PII patterns, and the ANSI/OSC/bidi stripping, respectively.

Then, once the corresponding code exists:

- [ ] `test_plans.py`, `test_media_fetch.py`, `test_parity.py`,
      `test_accounts.py`, `test_no_mark_read.py`, `test_no_private_data.py`.

## CI, packaging and distribution

`Dockerfile` (multi-stage, non-root runtime user), `Dockerfile.test`,
`.dockerignore`, `Makefile` (`make build`, `make test`, `make lint`, `make
fmt`, `make shell`), `.github/pull_request_template.md` and
`scripts/smoke_mcp.py` (a real stdio MCP handshake + `tools/list` check, not
just an import check) already exist.

- [ ] `.github/workflows/ci.yml` — wire up `make lint`, `make test` and
      `scripts/smoke_mcp.py` across the 3.12/3.13/3.14 matrix, plus the
      container build check. The pieces it needs to call already exist; the
      workflow file itself does not yet.
- [ ] `.github/workflows/release.yml` — tag-triggered only; third-party actions
      pinned by commit SHA, not by a mutable tag.
- [ ] `.github/ISSUE_TEMPLATE/*`.
- [ ] Decide on PyPI and MCP-registry publication (explicitly deferred by the
      owner in the design, §12.5 — a name claimed there is claimed forever).

## Distribution surface (Claude Code plugin, skills)

- [ ] `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`,
      `plugin.mcp.json` — so the repo can be added as a Claude Code plugin the
      way `yandex-mcp` and `zabbix-ai-cli` are.
- [ ] `.claude/skills/<skill>/SKILL.md` — at least one Claude Code skill once
      there is a stable enough tool surface to write one against.

## Documentation

- [ ] `docs/operations.md` — one page per operation (read and plan), its
      inputs, its JSON Schema, and worked examples — referenced from the
      README's command tables but not yet written.
- [ ] `docs/configuration.md` — the full `tgai.yaml` reference; the README's
      Configure section shows the common cases only.
- [ ] Fill in the GitHub-side items in
      [`docs/seo-geo-checklist.md`](docs/seo-geo-checklist.md) (topics, About,
      social preview) — these can't be done from inside the repository.

## Housekeeping

- [ ] Once `opspec.py` exists, add a startup-invariant test asserting the
      claim this README already makes: no MCP tool applies a plan, `handler`
      and `planner` are mutually exclusive per operation, and CLI/MCP report
      identical validation errors for the same bad input (design §4, "parity
      tests").
- [ ] Archive or otherwise resolve the dead `tdata-session-exporter` repo
      mentioned in the design (§2) as a stray duplicate — tracked here because
      it's a decision about a *different* repository, not something this one's
      code can fix.
