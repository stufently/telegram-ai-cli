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

- [ ] With test Telegram accounts, run one protected-content forward and confirm
      Telegram's live `CHAT_FORWARDS_RESTRICTED` response maps to
      `FORWARDS_RESTRICTED` as the deterministic unit test specifies.
- [ ] With a real forum, page `chat topics` using the returned three-part cursor
      and page scoped `mentions` with its returned offsets.
