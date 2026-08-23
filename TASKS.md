# Tasks

Backlog only — open and future work, not a progress log. What has already
shipped is in [`CHANGELOG.md`](CHANGELOG.md); the design these items implement
is
[`docs/superpowers/specs/2026-08-23-telegram-ai-cli-design.md`](docs/superpowers/specs/2026-08-23-telegram-ai-cli-design.md).

## Distribution surface (Claude Code plugin, skills)

- [ ] `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`,
      `plugin.mcp.json` — so the repo can be added as a Claude Code plugin the
      way `yandex-mcp` and `zabbix-ai-cli` are.
- [ ] `.claude/skills/<skill>/SKILL.md` — at least one Claude Code skill now
      that the tool surface is stable enough to write one against.

## Known gaps

- [ ] **No handler-level tests.** Everything under `ops/` is tested through its
      pure parts (`_serialize`, `links`, `untrusted`, the policy kernel); no test
      drives a handler with a fake Telethon client. Raised by review: the cases
      worth covering that way are policy-checked-before-fetch, a DM refusal, the
      link/id conflict, and that `chat.read` sends `GetPeerDialogsRequest`
      specifically rather than anything that acknowledges a message. Needs a
      small fake-client fixture first.

- [ ] **Plan operations do not accept a `t.me` message link.** Reads resolve one
      through `chats.resolve_chat_ref` and keep its message number; `write.py`
      still hands the raw string to `client.get_entity`, so a link is either
      resolved as a chat by Telethon or refused outright — either way the number
      is lost. `message.reply` / `message.edit` / `message.delete` are the ones
      where a pasted link is the natural input, and they should take the message
      id from it the way `media.fetch` and `message.reactions` now do.
- [ ] **`chat.read` does not filter by forum topic.** A topic link is parsed and
      reported (`meta.topic_id`, plus a warning), but the page still covers the
      whole chat. Telethon can filter with `reply_to=<topic>`; the question is
      whether that becomes an argument of its own rather than a side effect of
      the link's shape.

## Known gaps in the CLI surface

- [ ] **List- and object-valued arguments have no CLI form.** `_options_for` in
      `cli.py` maps a `list[int]` to a single `int` (no `multiple=True`) and a
      nested model to a string, so `message delete` / `message forward`
      (`message_ids`), `chat create` (`users`) and `chat promote` (`rights`)
      cannot be planned from the terminal at all — only through their MCP
      tools. Either teach the generator repeated options and a JSON form, or
      say so in `--help` rather than only in `docs/operations.md`.
- [ ] **`login_and_register` writes the account row before it takes the session
      lock** (`accounts/login.py`), so a login that then fails on
      `SessionLocked` has already changed `phone`, `source`, `session_path` and
      `status` on a row another process is using. `account add` now registers
      under the lock (`AccountRegistry.register_phone_login`); the login path
      still needs the same treatment, ideally as one registry method that holds
      the old auth key's lock across the whole sequence and restores the row on
      failure.
- [ ] **An already-authorised session accepts a new `--phone` without checking
      it.** `interactive_login` is idempotent and returns early, but the row was
      written before that — so the number is stored as though a login had
      verified it.

## Decisions

- [ ] Decide on PyPI and MCP-registry publication (explicitly deferred by the
      owner in the design, §12.5 — a name claimed there is claimed forever).
- [ ] Decide what to do about `telegram_plan_status(plan_id)` and
      `telegram_plan_list()`. The design (§5) lists both as tools; the code has
      neither, and `tg-ai plan list` / `plan show` cover the same ground from
      the terminal. Either implement them as read tools or strike them from the
      design — the README no longer claims they exist.
- [ ] Archive or otherwise resolve the dead `tdata-session-exporter` repo
      mentioned in the design (§2) as a stray duplicate — tracked here because
      it's a decision about a *different* repository, not something this one's
      code can fix.

## For the owner, on GitHub (not fixable from a commit)

From [`docs/seo-geo-checklist.md`](docs/seo-geo-checklist.md), where the
rationale for each lives:

- [ ] **About → Website** — leave blank until there is a documentation site;
      don't link a placeholder.
- [ ] **About → Releases / Packages checkboxes** — tick only once there is
      something behind them.
- [ ] **Social preview image** (Settings → General), 1280×640.
- [ ] **When PyPI publication happens:** re-check `keywords`, `description` and
      `[project.urls]` in `pyproject.toml`, and register the project on an MCP
      registry under the same name and description.
- [ ] **After the first tagged release:** add this repo to the "Related
      projects" lists in `zabbix-ai-cli` and `yandex-mcp`, which this README
      already links back to.
