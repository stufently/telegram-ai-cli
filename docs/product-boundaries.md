# Product boundaries for v0.1

This file turns “maybe someday” notes into explicit decisions. The project is a
bounded personal-account CLI/MCP server, not a general Telegram client, a data
warehouse, or an unattended fleet scheduler. A future request may revisit any
boundary, but absence here is intentional rather than unfinished work.

## Local, operator-driven lifecycle

- The archive is filled, reconciled and erased only by explicit commands. It
  has no background sync, cross-account query, automatic retention, eviction,
  edit/delete reconciliation, or transcript cache. Operators bound collection
  by choosing chats and sync limits and remove it with `archive forget`.
- The uploads directory is operator-owned. The tool limits each file but does
  not delete or age files a person or another program put there. Likewise, the
  outbound ledger has a time window but no reporting UI or automatic disk
  maintenance command.
- Scheduled messages are cancelled in Telegram's own client, where the queue is
  visible even when this program is stopped. Chat photos can be replaced but
  clearing one is not disguised as an optional path.

## Deliberately narrow Telegram surface

- No album upload, custom thumbnail, forced voice/video-note mode, block-list
  browser, reaction roster expansion, moderation-state browser, mass history
  deletion, or monoforum per-user reply operation is exposed. These each widen
  either privacy reach or destructive scope and require a concrete product
  request before becoming plan operations.
- Of folder editing, only `folder add` exists. Removing a chat from a folder,
  renaming one, reordering them and deleting one all stay out for the reason
  that made `add` hard: `UpdateDialogFilter` replaces the whole folder, so
  every one of them would have to send back membership this program is not
  allowed to see.
- Reaction availability and per-chat reaction-count ceilings are left to
  Telegram's apply-time validation. Topic reads use chat-wide read state; the
  topic listing exposes Telegram's per-topic counters separately.
- Muting and archiving continue to use the existing `send` capability. A new
  account-settings capability would enlarge every configuration and migration
  for two owner-visible operations.
- `mcp.tools` remains an exact-name allowlist, not a pattern or tag policy
  language. Object-valued CLI arguments remain JSON rather than developing a
  second, model-specific option grammar.

## Concurrency and fleet behaviour

- One account daemon serialises operations over one Telethon client. A watch
  may therefore delay later calls; reads do not overtake it. The daemon never
  applies plans and does not hand an account to `plan apply`, preserving the
  terminal approval boundary.
- A watch names one account. Fleet reads that do not name an account open or
  fail each account honestly rather than silently narrowing to whichever daemon
  answered. There is no hidden fan-out scheduler.
- HTTP is loopback-only, bearer-authenticated, body-limited and rate-limited.
  Limits are process-wide safeguards rather than a multi-tenant identity or
  quota system.

## Accepted local-user residual risks

The threat model treats another process running as the same OS user as inside
the local trust boundary. Within that boundary:

- an upload is re-hashed immediately before sending, but the directory is not
  held by descriptor from review through Telegram's file read;
- MCP roots canonicalise paths but do not hold directory descriptors across a
  network round trip;
- two separately approved, identical plans can race between duplicate check and
  ledger settlement, and ledger/rate-limit refunds are ordered safe-side-first
  rather than committed in one cross-store transaction.

Closing those windows requires descriptor-based uploads, `openat` storage, or a
new shared transactional store. Those changes are disproportionate to an
attacker who already owns the same local account and can read the Telegram
session itself.

## Read scale and portability

- Search context is enriched only for a bounded number of single-chat hits.
  Folder filters remain on dialog-oriented listings rather than being copied
  onto every read that happens to walk chats.
- Archive regular-expression search uses a POSIX main-thread signal budget. On
  a non-main-thread embedding it still has row and pattern ceilings but no wall
  timer. Replacing it requires choosing and maintaining a bounded regex engine;
  the supported server/CLI execution paths keep handlers on the main loop.
- QR colour inversion remains explicit (`--invert`). Terminal background
  detection protocols are inconsistent enough that guessing wrong is worse
  than a documented flag.
- Transcription decodes inside a memory- and pid-limited container rather than
  adding a streaming pre-decoder to the host. The cgroup is the hard bound for
  malformed or very long audio before Whisper can verify duration.

## Approval boundary

Plan state and review remain terminal-only through `tg-ai plan list` and
`tg-ai plan show`. They are intentionally not MCP tools, and no daemon or HTTP
endpoint can apply a plan. The original design table has been updated to match
this decision.

Release recovery uses GitHub Actions' **Re-run failed jobs** for the existing
workflow run. A full rerun after PyPI accepted the immutable version is expected
to fail at publish rather than silently treating a duplicate upload as success.
