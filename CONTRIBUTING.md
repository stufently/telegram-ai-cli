# Contributing

Thanks for considering a contribution. This project touches a personal Telegram
account's live credentials, so a couple of things are stricter here than in a
typical CLI project — read [Security notes for contributors](#security-notes-for-contributors)
before you send a patch that touches `safety.py`, `plans.py`, `audit.py`,
`limits.py`, `redact.py`, `render.py` or account storage.

## Everything runs in Docker

Nothing in this repo should be installed on your host to work on it. Build and
run everything through the Makefile, which wraps Docker:

```bash
make test    # pytest, inside the container
make lint    # ruff check + ruff format --check, inside the container
```

If a target you need isn't there yet, add it to the `Makefile` rather than
running the underlying tool on the host — the point is that `make test` gives
the same result on every machine, including CI.

## Before you open a pull request

1. `make lint` and `make test` both pass.
2. New behaviour has a test. This project's safety guarantees — the hard
   denylist, fail-closed allow lists, the plan/apply split, persistent limits —
   are each backed by a specific test file (see `docs/superpowers/specs/2026-08-23-telegram-ai-cli-design.md`
   §10 for the list); a change that touches one of those areas without a
   matching test change will be asked to add one.
3. `CHANGELOG.md` has an entry under `[Unreleased]`, written as a sentence that
   explains *why* the change exists, not just what it touches.
4. No secrets, account labels, chat ids, phone numbers or other identifying
   material from a real Telegram account appear anywhere in the diff — commit
   message included. This is a public repository.

## Security-sensitive changes

Anything that changes `safety.py`, `plans.py`, `limits.py`, the hard denylist in
`config.py`, `audit.py`, `render.py`, `redact.py`, or account/session storage is
treated as security-sensitive and reviewed accordingly — expect more scrutiny
and more requested tests than for an equivalent change elsewhere. If you're
proposing a way to reach `777000` or Saved Messages under any configuration, or
a way to apply a plan from the MCP surface, that is very unlikely to be
accepted; see [SECURITY.md](SECURITY.md) for what's considered in scope as a
vulnerability versus a design trade-off.

If you've found an actual vulnerability rather than a feature idea, please
report it through [GitHub Security Advisories](https://github.com/stufently/telegram-ai-cli/security/advisories/new)
instead of a pull request or a public issue — see [SECURITY.md](SECURITY.md).

## Code style

- Ruff enforces both linting and formatting (`ruff check`, `ruff format --check`);
  run `make lint` before pushing rather than reformatting by hand.
- Type hints are expected throughout; the project targets Python 3.12+ syntax
  (`from __future__ import annotations`, `X | None`, `StrEnum`).
- Follow the tone already in the codebase: module and class docstrings here
  explain *why* a design decision was made, not just what the code does. A
  security-relevant choice without a one-line rationale is harder to review and
  harder to trust later.

## Project structure

See the architecture section of [`docs/superpowers/specs/2026-08-23-telegram-ai-cli-design.md`](docs/superpowers/specs/2026-08-23-telegram-ai-cli-design.md)
for how `opspec.py`, `ops/`, `cli.py` and `mcp_server.py` fit together — in
short, the CLI and the MCP server are both thin adapters over the same
operation registry, and a change to one surface without checking the other is
the most common way to break parity between them.

## License

By contributing, you agree that your contribution is licensed under the MIT
license that covers the rest of this project (see [LICENSE](LICENSE)).
