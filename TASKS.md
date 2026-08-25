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
      `telegram-ai-cli`, owner `stufently`, repository `telegram-ai-cli`,
      workflow `release.yml`, environment `pypi`.
- [ ] Create the GitHub environment `pypi`, preferably with a required reviewer.
- [ ] Bump `project.version`, land it on `main`, and push the matching first
      `v*` tag. The release workflow rejects a tag/version mismatch.
- [ ] Review and publish the draft GitHub release produced after PyPI succeeds.
- [ ] After the first package exists, change the plugin launcher to an
      install-free `uvx telegram-ai-cli …` form if that remains desirable.
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
- [ ] Against a channel this account neither runs nor has ever written to, run
      `whois` and then send to the `linked_monoforum_id` it returns. The unit
      tests pin that `GetFullChannel` is issued, and Telethon's own
      `process_entities` stores what it returns — but only a live account can
      show that the id then resolves for an inbox that was never in its
      dialogs, which is the whole point of the call.
