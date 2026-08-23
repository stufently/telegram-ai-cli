# SEO / GEO checklist — GitHub UI steps

Everything that can live in the repository (`README.md`, `llms.txt`,
`CITATION.cff`, `pyproject.toml` keywords, badges) is already there. The items
below can only be set through GitHub's web UI or API — they are not files a
commit can carry — so they need a person with admin rights on the repository
to go through them once, and to revisit them if the pitch changes.

Rationale for why this matters at all: the design brief for this project
(`docs/superpowers/specs/2026-08-23-telegram-ai-cli-design.md`, §7) treats
findability by AI answer engines (GEO — generative engine optimization) as at
least as important as classic search ranking, on the premise that someone
looking for "an MCP server for Telegram" is more likely to ask an LLM than to
run a Google search.

## Repository settings → Topics

Go to the repo's **Settings → General**, or the gear icon next to **About** on
the repo home page, and add topics. Aim for 15–20, the same range used on
`zabbix-ai-cli` and `yandex-mcp`. The design brief's list:

```
mcp
mcp-server
model-context-protocol
claude-code
claude
cli
ai-agents
ai-tools
llm-tools
telegram
mtproto
telethon
userbot
python
automation
```

- [x] Add the topics above (or the current equivalent list, if the pitch has
      moved on). *(Done 2026-08-23 via `gh repo edit --add-topic`; all 15
      applied. This needs no UI visit — `gh` reaches the same settings.)*

## About panel

Click the gear icon next to **About** on the repo home page and fill in:

- [x] **Description** — one line, matching the README's title tagline:
      "CLI and MCP server for AI agents to read and act on a personal Telegram
      account over MTProto, with plan-and-apply approval."
      *(Done 2026-08-23 via `gh repo edit --description`.)*
- [ ] **Website** — leave blank unless/until there's a documentation site;
      don't link a placeholder.
- [ ] Check the **Releases** and **Packages** boxes only once there's
      something behind them — an empty Releases tab linked from About looks
      abandoned, not active.

## Social preview image

**Settings → General → Social preview.** GitHub renders this image whenever
the repo URL is shared on Twitter/X, Slack, LinkedIn, etc. — including, for
some crawlers, in AI-generated summaries of the link. Upload one at
1280×640px.

- [ ] Generate and upload a social preview image once the project has a
      visual identity worth putting on one (a plain text card with the repo
      name is an acceptable placeholder, but note it here as a placeholder so
      it gets revisited).

## PyPI metadata (when publication happens)

The pipeline is in place — a `v*` tag publishes over Trusted Publishing (see
the README's Releasing section) — but nothing is claimed on PyPI until the
repository owner registers the pending publisher, because a name on a public
registry is claimed permanently. When that happens:

- [ ] Confirm `keywords`, `description` and `[project.urls]` in
      `pyproject.toml` are still accurate (they already carry the same terms
      as the GitHub topics above, so PyPI's own search picks them up too).
- [ ] Register the project on [MCP registries](https://github.com/modelcontextprotocol)
      /catalogs once one is settled on, using the same name and description.

## Cross-linking with sibling projects

- [ ] Once this repo has its first tagged release, add it to the "Related
      projects" list (or equivalent) in `zabbix-ai-cli`'s and `yandex-mcp`'s
      READMEs, the way this project's own README already links back to them.
      A small linked family of same-author MCP tools is a stronger signal,
      for both search and an LLM's sense of provenance, than any one of them
      alone.

## Recheck cadence

None of the above is "set once and forget" — topics and the About description
should be revisited whenever the pitch in the README's first paragraph
changes materially, since that paragraph is the source of truth both a human
skimmer and an LLM citation will draw from.
