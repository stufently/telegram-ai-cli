## What this changes

<!-- What does this PR do, and why? -->

## Related issue

Closes #

## Checklist

- [ ] `make lint` passes (ruff check + format)
- [ ] `make test` passes
- [ ] If a dependency version changed: `constraints.txt` and/or `pyproject.toml` floors updated together, with a real released version (not guessed from memory)
- [ ] If a new operation/tool was added: it exists identically on both CLI and MCP surfaces (see `test_parity.py`), and write operations go through a `telegram_plan_*` tool, never a direct send
- [ ] If safety-relevant code changed (`safety.py`, `plans.py`, `audit.py`, denylists, limits): explained below, not just in the diff
- [ ] No account labels, chat IDs, phone numbers, tokens, or other owner-specific data were added anywhere in the diff
- [ ] Docs updated if behavior changed (README / SECURITY.md / docs/)

## Safety-relevant changes (if any)

<!-- Explain the change to the threat model, not just what the code does. Leave blank if not applicable. -->
