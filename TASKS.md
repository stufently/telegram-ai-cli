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
      small fake-client fixture first. `drafts.list` adds one more: its
      per-row filter is tested as a pure function (`draft_visibility`), but
      nothing yet proves the *handler* routes each verdict to the right
      bucket — a swap of the "hidden" and "withheld" branches would list a
      private draft and still pass. `tests/test_search_context.py` now drives
      `handle_search` against a local fake (history in a list, Telethon's
      `offset_id`/`reverse` paging reproduced), and `tests/test_mentions.py`
      drives `handle_mentions` against another one that records every request
      class it is handed — which is how "policy is checked before the fetch" and
      "no acknowledging request is ever issued" are asserted rather than
      reviewed. `tests/test_folders.py` adds a third — a fake registry and
      client driving `handle_chats`, `handle_inbox` and `handle_folders` over
      real Telethon entities, which is how "a folder cannot admit a chat the
      policy closes" is asserted. Three per-module fakes rather than a shared
      fixture; promoting one to `conftest.py` is what would let the cases above
      be written, and would stop the fourth from being written from scratch.

- [ ] **"Muted" is read per chat, never from the account's defaults.**
      `folders.dialog_is_muted` (formerly `inbox._is_muted`) reports muted only
      where the dialog carries its own `notify_settings`; Telegram's model is
      that an absent peer override means the *global* default applies, which
      `account.getNotifySettings` would supply. So an account that muted all
      groups globally has its inbox and every `exclude_muted` folder answer as
      though nothing were muted. Pre-dates the folder work (raised by review,
      2026-08-23); fixing it means one more call per sweep, cached per account.

- [ ] **A folder narrows only the two dialog listings.** `chats.list` and
      `inbox.list` take `folder`; `search`, `mentions` and `drafts` walk chats
      too and do not. The filter itself is reusable (`folders.facts_of` plus
      `FolderView.contains`) — what is missing is deciding whether every
      chat-walking read should carry the argument, or whether that many
      near-identical arguments is worse than one documented asymmetry. Editing a
      folder is a remote write and deliberately has no plan operation yet.

- [ ] **`mentions.list` cannot be narrowed to one chat, and does not page.**
      It sweeps the dialog list and reports the top `limit` chats; there is no
      `chat` argument for "what did I miss in *this* one", and no cursor for the
      mentions past `per_chat` in a chat that has hundreds. Telegram's
      `getUnreadMentions` takes `offset_id`/`max_id`, so paging is a cursor
      argument away; the question is whether a chat-scoped variant belongs there
      or in `chat.read`.

- [ ] **A forum topic is not a filter on `mentions.list`.** Both unread requests
      accept `top_msg_id`, so "mentions in this topic" is one argument — but the
      same open question as `chat.read`'s topic gap applies (see above): whether
      that becomes an argument of its own or is derived from a link's shape.

- [ ] **`reactors` names only whoever came with the page.** Telegram attaches
      recent reactors to the message on small chats and nothing at all on large
      ones, and the roster request (`messages.getMessageReactionsList`) is
      deliberately never made — so a reaction from a big group reports the emoji
      with `peer_id: null` and a `reactors_reason`. Whether that request should
      ever be available, gated by a capability of its own, is a decision nobody
      has taken.

- [ ] **Search context is single-chat only, and costs two calls per hit.**
      `context` is refused on a global search, because each hit can be in a chat
      the caller never named and paging around it is a separate read per chat.
      The per-hit cost is why only the first 10 matches are enriched. Both
      ceilings would move if context were fetched as one ranged read per cluster
      of nearby hits instead of two calls per hit — worth doing only if the
      argument turns out to be used with large `limit`s.

- [ ] **A watch monopolises its account's session for the whole wait.**
      `telegram_watch` keeps a client open for up to 300s, and `accounts/lock.py`
      holds an exclusive `flock` per auth key for as long as a client is open —
      so anything else reaching for that account meanwhile fails immediately with
      `SESSION_LOCKED`. Every operation takes the same lock; a watch is the first
      one that holds it for minutes rather than milliseconds, which is why the
      ceiling is five minutes. Documented in `docs/operations.md`. Fixing it
      properly means either a multiplexing client shared across operations, or a
      way to hand the connection over — both bigger than this operation, and
      neither is safe to fake: two connections on one auth key can get the
      session revoked.
- [ ] **A watch does not cover the fleet.** `telegram_inbox` sweeps every
      permitted account; `telegram_watch` uses exactly one, because watching *n*
      accounts would hold *n* session locks for the duration. Waiting on several
      accounts at once needs the lock question above answered first.
- [ ] **Plan operations do not accept a `t.me` message link.** Reads resolve one
      through `chats.resolve_chat_ref` and keep its message number; `write.py`
      still hands the raw string to `client.get_entity`, so a link is either
      resolved as a chat by Telethon or refused outright — either way the number
      is lost. `message.reply` / `message.edit` / `message.delete` are the ones
      where a pasted link is the natural input, and they should take the message
      id from it the way `media.fetch` and `message.reactions` now do.
- [ ] **`Envelope.failure` is outside the trust boundary.** `telegram_result`
      wraps and defangs a *successful* payload; an error is assembled from an
      exception and neither wrapped nor defanged, and `meta.untrusted_content`
      is not set on it. So any refusal that quotes Telegram-authored text — a
      chat title, a message body — hands the reader an unmarked stranger's
      sentence in the one field it has most reason to trust. The forum
      operations avoid it by naming chats by id (raised by review, 2026-08-23);
      the general fix is to defang `error.message` / `error.suggestion` where
      the envelope is built, so it stops depending on every call site
      remembering.

- [ ] **Topic listings serve one page.** `chat.topics` sends
      `messages.getForumTopics` with a zeroed cursor, so a forum with more
      topics than `limit` reports `truncated` and has no way to fetch the rest.
      Telegram's cursor there is a triple (`offset_date`, `offset_id`,
      `offset_topic`) taken from the last row, which is three arguments for a
      case that starts mattering past 500 topics — deferred rather than
      designed hastily.

- [ ] **A topic's own read pointers are not used.** `chat read --topic-id`
      reports `read_state` from the chat's dialog, because a forum has one
      dialog for all of its topics, and warns that it is chat-wide.
      `ForumTopic` carries `read_inbox_max_id` and the unread counts per topic
      (`chat topics` returns them), so the per-message `read_by_me` flags of a
      topic page could be derived from the topic instead of the chat — at the
      cost of one extra `messages.getForumTopicsByID` call.

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
