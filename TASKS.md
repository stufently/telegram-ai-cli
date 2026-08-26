# Tasks

The repository-side v0.1 implementation is complete. Known product boundaries
and consciously accepted residual risks are recorded in
[`docs/product-boundaries.md`](docs/product-boundaries.md); shipped work is in
[`CHANGELOG.md`](CHANGELOG.md). They are not an implied backlog.

Everything below needs an external account, repository setting, or release
authority and cannot be completed by changing this checkout.

## Release owner

- [ ] Register the PyPI pending trusted publisher at
      <https://pypi.org/manage/account/publishing/> with project
      `telegram-ai-cli-mcp`, owner `stufently`, repository `telegram-ai-cli-mcp`,
      workflow `release.yml`, environment `pypi`.
- [ ] Create the GitHub environment `pypi`, preferably with a required reviewer.
- [ ] Bump `project.version`, land it on `main`, and push the matching first
      `v*` tag. The release workflow rejects a tag/version mismatch.
- [ ] Review and publish the draft GitHub release produced after PyPI succeeds.
- [ ] After the first package exists, change the plugin launcher to an
      install-free `uvx telegram-ai-cli-mcp …` form if that remains desirable.
- [ ] Publish the server to the chosen MCP registry under the same package name.

## Repository owner

- [ ] Upload the 1280×640 social preview image in GitHub Settings → General.
- [ ] Enable the About “Releases” and “Packages” links after the first release;
      leave Website blank until a documentation site exists.
- [ ] Archive or otherwise resolve the separate, dead
      `tdata-session-exporter` repository mentioned by the original design.
- [ ] After the first release, add this project to the related-project lists in
      `zabbix-ai-cli` and `yandex-mcp`.

## Optional live acceptance

A first live pass ran on 2026-08-24 against one authorised account: every read
operation, media download, local archive, the daemon, both MCP transports, and
one `message.send` through plan → apply → duplicate refusal. It is what found
the unregistered `chat topics`, the always-true `proxy` flag, the always-zero
fleet counters, the message-less flood-wait warning and the empty
`serverInfo.version` — all fixed in `CHANGELOG.md` under Unreleased. What it
could not cover needs more than one account or a chat of a kind that account
does not have:

- [ ] With test Telegram accounts, run one protected-content forward and confirm
      Telegram's live `CHAT_FORWARDS_RESTRICTED` response maps to
      `FORWARDS_RESTRICTED` as the deterministic unit test specifies.
- [ ] With a real forum, page `chat topics` using the returned three-part cursor
      and page scoped `mentions` with its returned offsets. The command exists
      again as of the fix above; only its paging is still unexercised live.

The monoforum path had its own live pass on 2026-08-25, against channels this
account neither runs nor had ever written to. `whois` returned the inbox and
its name for ten of the first twenty-five channels in the account's dialogs; a
`chat read` and a prepared `message.send` plan both resolved an inbox from a
separate process. It also corrected the reason the code exists: the id resolves
even without `GetFullChannel`, because Telethon falls back to
`channels.GetChannels` with `access_hash=0` and Telegram answers it — so the
call buys the name, the confirmation and the cached hash, not the addressing.
`CHANGELOG.md` and `docs/operations.md` say that now. What is still unexercised
there needs a second account:

- [ ] Apply a `message.send` plan into a monoforum inbox — the live pass
      stopped at the prepared plan, because applying it would have written to a
      stranger's channel.
