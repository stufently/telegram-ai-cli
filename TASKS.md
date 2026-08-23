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

- [ ] **Nothing verifies the transcription image in CI.** `tests/test_transcribe.py`
      fakes the container at `subprocess.run`, which is what lets it assert the
      command (`--network none`, one read-only file, a non-root `--user`) and the
      image-absent path without a half-gigabyte model in the test job. What it
      cannot catch is `Dockerfile.transcribe` failing to build, or the entrypoint's
      exit codes drifting from the constants in `telegram_ai_cli/transcribe.py`.
      Both were verified by hand against the real image on 2026-08-23 (exit 3 for
      a missing model, exit 4 for audio over the ceiling, a clean transcript with
      the network off). A `container`-style CI job would pin them, but it downloads
      the model on every run, so it wants a cache decision first.

- [ ] **A cancelled transcription leaves its container running until it times
      out.** `asyncio.to_thread` cannot be cancelled: if the MCP call goes away
      mid-transcription, the worker thread keeps waiting on `subprocess.run` and
      the container keeps decoding. It is bounded — the thread's own timeout
      fires and removes the container by name — but the ceiling is
      `transcribe.timeout_seconds` rather than "immediately". Fixing it properly
      means `asyncio.create_subprocess_exec` and a cancellation handler, which is
      a different shape of code from every other operation here. Raised by review
      alongside the timeout-kills-only-the-client bug, which *was* fixed.

- [ ] **The in-container length check runs after the file is decoded.**
      faster-whisper reports a duration only once it has decoded, so
      `--memory`/`--memory-swap` are what actually bound a fabricated duration on
      a very long file: the container is killed rather than the host. A streaming
      decode capped at `max_audio_seconds` would check before allocating, which
      is the right answer if this ever needs to run without cgroup limits.

- [ ] **A transcript is not archived with its message.** `media transcribe`
      answers a question; it does not write the text anywhere. Transcribing the
      same voice message twice runs Whisper twice and leaves two copies of the
      audio under the download quota. Storing transcripts would mean deciding
      where (the archive? next to the artifact?), when they expire, and whether a
      chat removed from the allowlist stops answering from the store — the same
      questions the archive already answers, and worth answering the same way
      rather than a second way.

- [ ] **A chat photo can be replaced but not removed.** `chat.set_photo` takes a
      required `path`, and Telegram clears a photo with a distinct empty object
      rather than an absent file. Making `path` optional would weaken the one
      preview that matters here — "set the photo to *this file*" is reviewable,
      "set the photo to nothing in particular" is a different sentence — so
      clearing wants an operation of its own, which nobody has asked for yet.

- [ ] **The block list cannot be read.** `account.block` and `account.unblock`
      write it; `contacts.getBlocked` would read it, and there is no operation
      for that — so an agent cannot tell whether a person is already blocked, and
      an unblock of somebody who never was succeeds silently. It is a read of the
      account's own settings rather than of a chat, so it needs the decision
      `sessions` needed: which capability governs it, and whether it is on by
      default. Raised while implementing the writes, deliberately not answered in
      the same change.

- [ ] **The QR code's colours are a flag, not a detection.** `--invert` exists
      because a terminal cannot be asked what its background is. `COLORFGBG`,
      OSC 11 queries and `$TERM_PROGRAM` all half-answer it; none is reliable
      enough to flip a code somebody is pointing a camera at, so the default is
      documented instead. Worth revisiting only if the flag turns out to be the
      first thing every user has to discover.

- [ ] **A QR login can be scanned by the wrong account, and only says so
      afterwards.** The row's `user_id` and `phone` are corrected to whoever
      actually scanned (that is what `AccountStore.set_phone` is for), so the
      account inventory never *lies* — but nothing refuses the identity change
      at the time, because by the point it is knowable the session file is
      already authorised on disk. Refusing properly means comparing the previous
      `user_id` against the new one and, on a mismatch without `--replace`,
      undoing the login: logging the new session out, deleting it, and restoring
      the row. That is a rollback path with its own failure modes, not a
      condition, which is why it is a backlog item rather than an `if`.

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
      policy closes" is asserted. `tests/test_archive.py` is the fourth, and it
      *was* written from scratch — which is the evidence for the point below.
      Per-module fakes rather than a shared fixture; promoting one to
      `conftest.py` is what would let the cases above be written, and would stop
      a fifth from being written from scratch again.

- [ ] **The `noforwards` classification is not exercised against a live chat.**
      `ChatForwardsRestrictedError` is in the applier's no-effect set and is
      translated into `FORWARDS_RESTRICTED` with a message naming the source chat
      by id; the test constructs the exception directly, because there is no
      fixture that drives `apply_plan` against a fake Telethon raising from
      `forward_messages`. The same missing fixture is the one the handler-level
      gap above asks for.

- [ ] **The regex time budget is POSIX- and main-thread-only.** `archive search`
      caps a pattern at 10 seconds with `SIGALRM`, which is the only
      interruption the standard library offers without a second process — and
      `signal.signal` can only be called from the main thread, so an MCP server
      that ever dispatches handlers off it loses the timer silently (the code
      degrades to no timer rather than a fake one, and says so in
      `docs/operations.md`). A real fix is a matching engine with a guaranteed
      bound: the `regex` package's `timeout=`, or RE2. Both are a dependency,
      which is why neither was added here without asking.

- [ ] **The archive is per account, and nothing correlates across accounts.**
      `archive search` takes one `account` like every other operation, so
      "where did this phrase appear across the whole fleet" still means one call
      per account and joining the answers by hand. The rows are keyed
      `(account, chat_id, message_id)` and a cross-account query is one `IN`
      clause away — what is undecided is the surface: a `fleet` flag on
      `archive search` (which then has to say *which* account each hit came
      from, and re-check each one's policy separately), or a distinct operation.
      Deliberately out of scope for the archive work itself; correlation was
      named as a thing to stop and ask about rather than build.

- [ ] **The archive has no retention policy and no size ceiling.** `archive
      sync` grows the file for as long as it is called, and the only way back is
      `archive forget` on a named chat. There is no "drop messages older than N
      days", no per-chat message cap and no equivalent of
      `download.total_quota_bytes`. For a handful of chats this is fine; for a
      standing habit of archiving everything permitted, the file is unbounded.
      A quota would need a policy for what to evict — oldest messages, or whole
      chats least recently searched — and neither is obviously right.

- [ ] **A message deleted or edited on Telegram is not reconciled.** Sync is
      forward-only: it fetches ids above the watermark and backfills below the
      oldest one, so a message deleted after it was archived stays in the
      archive, and one edited outside both ranges keeps its old text. Detecting
      either means re-reading a range already stored and diffing it, which is
      the cost the watermark exists to avoid. Worth doing only as an explicit
      `archive resync --chat` that a person asks for.

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

- [ ] **A watch still occupies its account for the whole wait, daemon or not.**
      The immediate refusal is fixed — with `daemon.enabled` and `tg-ai daemon
      serve --account <label>`, callers that name the account queue behind the
      watch instead of getting `SESSION_LOCKED` (see
      `docs/operations.md#the-account-daemon`). What is *not* fixed is the
      occupancy: the daemon serialises requests, because the Telethon client
      underneath is one connection, so a caller behind a 300-second watch waits
      up to 300 seconds. Making a read overtake a watch means either interrupting
      the watch's own `client.run_until_disconnected`-style wait or issuing
      concurrent requests on one client — the second is what the lock exists to
      prevent, and the first needs the watch to be cancellable, which it is not.
      The five-minute ceiling is still the mitigation.
- [ ] **Nothing routes a fleet-wide call through the daemons.** `dispatch`
      routes a request only when it *names* an account, so `telegram_inbox` with
      no `account` opens every permitted account itself and hits `SESSION_LOCKED`
      on any that a daemon holds. Narrowing it silently to the one daemon's
      account would be worse — an answer covering one account under a name that
      promises all of them — so the honest fix is a fan-out that asks each
      account's daemon in turn and falls back per account, which is a second
      scheduling policy nobody has asked for yet.
- [ ] **The daemon accepts unbounded concurrent connections.** Each connection
      is one task and one request, and a peer that connects without sending is
      dropped after 30 seconds — but nothing caps how many may be open at once.
      The socket is `0600` in a `0700` directory, so the only caller who can do
      this is the user who owns the account already; a semaphore and a "busy"
      response would be the fix if that ever stops being true.
- [ ] **`tg-ai plan apply` cannot use a daemon.** Applying is deliberately not a
      registered operation, so it is not something the socket can run — and it
      opens the account itself, which means applying a plan while a daemon holds
      that account fails with `SESSION_LOCKED`. Stop the daemon, or wait for the
      idle timeout. Giving the socket an apply endpoint is exactly the shortcut
      the approval design exists to forbid, so the fix, if one is wanted, is a
      way for the daemon to *hand the account back* rather than a new endpoint.
- [ ] **A watch does not cover the fleet.** `telegram_inbox` sweeps every
      permitted account; `telegram_watch` uses exactly one, because watching *n*
      accounts would hold *n* session locks for the duration. Waiting on several
      accounts at once needs the occupancy question above answered first — a
      daemon per account makes it possible to *reach* them, not to wait on them
      concurrently.
- [ ] **The HTTP transport has no rate limit and no request-size ceiling of its
      own.** It relies on the SDK's `max_request_body_size` (4 MiB by default)
      and on the bearer token being secret. Fine for a loopback port that only
      this user's processes reach; worth revisiting if it is ever put behind a
      reverse proxy, together with whether the token should be rotatable without
      a restart.
- [ ] **Most plan operations still do not accept a `t.me` message link.** The
      four marks (`message.react` / `unreact` / `pin` / `unpin`) do:
      `ops/marks.resolve_message` parses the link with
      `links.parse_telegram_link`, checks the policy, then applies the reads'
      own `chats.guard_message_link` and `chats.message_id_from`. Everything in
      `write.py` still hands the raw string to `client.get_entity`, so a link is
      either resolved as a chat by Telethon or refused outright — either way the
      number is lost. `message.reply` / `message.edit` / `message.delete` are
      where a pasted link is the natural input, and the helper to give them is
      already written and tested; what is missing is deciding whether it moves
      into `write.py` or `marks.py` stays the one module that owns
      message-addressed writes.
- [ ] **A reaction plan cannot say "keep the others" safely in every chat.**
      `message.react --keep-existing` computes the account's whole new list and
      sends it, because Telegram's call takes a list rather than a delta — but
      whether a chat allows more than one reaction per person is
      `reactions_uniq_max` on the chat's full info, which is a second request
      nobody makes. So the plan promises something a chat may refuse, and the
      refusal only shows up at apply time. Fetching the ceiling at plan time
      would let the summary say "this chat allows one reaction, so it will
      replace" with certainty rather than in the conditional.
- [ ] **A plan's message snapshot cannot see a swapped attachment.**
      `write.message_snapshot` records the digest of the message *body*, which
      is empty for every photo posted without a caption — so editing a media
      message to carry a different photo leaves the snapshot identical and
      `apply._check_messages` passes. `ops/marks.media_fingerprint` closes it for
      reactions and pins by recording the attachment's type and id in their own
      preconditions; `message.forward` and `message.delete` still have the gap,
      and the fix belongs in the shared snapshot rather than in a third copy.
      (Raised by review, 2026-08-23.)
- [ ] **Which reactions a chat permits is never checked.** A chat can restrict
      reactions to a named set, and a custom emoji reaction is a paid feature the
      account may not have. Both surface as an apply-time refusal
      (`ReactionInvalidError`) rather than as something the plan could have
      known; `messages.getAvailableReactions` and the chat's own
      `available_reactions` are what would answer it.
- [ ] **A refusal is defanged but never redacted.** `Envelope.failure` now puts
      the error payload through the trust boundary, but redaction is the other
      half of `telegram_result` and it did not come along: `redact_mapping` is
      driven by settings, and the envelope is built in places that hold no
      `OperationContext`. So a secret shaped value that reaches an error message
      — an argument echoed back, a filename — is printed as typed. Either the
      envelope learns the setting, or errors are redacted where they are raised;
      picking one is a design decision, not a flag.
- [ ] **A list of strings under a human-authored key is defanged, not
      delimited.** `wrap_untrusted` wraps a *string* under a name in
      `UNTRUSTED_FIELDS`; a list under the same name has its elements
      neutralised like any other string and comes back unmarked. No serializer
      produces one today, which is the only reason it is not a live gap — the
      field list is matched by name precisely so that a serializer added later
      is covered by default, and this is the shape that would slip through
      (raised by review, 2026-08-23).
- [ ] **The top-level CLI net cannot honour `--json`.** An error raised outside
      a command — before the root context exists — is printed by the net in
      `main()`, which now builds an envelope and renders it sanitized, but has
      no way to know the caller asked for JSON. A script piping `--json` gets a
      human line on stderr for that one class of failure. Fixing it means
      parsing the flag before click does, which is its own small parser.
- [ ] **`warnings` and `meta.extra` are outside the boundary.** Both are
      assembled by this project, so today neither carries stranger text, and
      both are handed to `Envelope.success` unwalked. The wrapping pass cannot
      simply be extended to cover them: `data` has already been through it, and
      a second pass would defang the markers the first one wrote. The warning
      that interpolates a chat title is the one that breaks this, and nothing
      stops it being written.

- [ ] **Sending a file covers one file, and only the forms an extension
      names.** `message.send_file` takes a single path on purpose — a list
      would let one approval upload a directory — so there is no album, and
      Telegram's grouped-media send is not reachable at all. The delivery form
      is read from the extension (`as_document` overrides it), which leaves
      three things unexpressible: a video note (the round one), a voice message
      made from a file Telegram would otherwise treat as music, and a thumbnail
      for a document. Each is one more boolean on an input model that is
      already at six fields, and none of them has been asked for yet; the
      question is whether they become flags or a single `send_as` enum.
- [ ] **The outbox has no quota and nothing prunes it.** `paths.downloads` is
      bounded by `download.total_quota_bytes` because this tool fills it;
      `paths.uploads` is filled by a person, so a per-file ceiling is all
      `upload.max_file_bytes` gives. If the directory becomes a place agents
      leave generated files, it needs the same running total — and a decision
      about who deletes from it.
- [ ] **A file plan pins bytes, not a path.** The applier recomputes the
      SHA-256 and refuses a file that changed, which closes the swap between
      review and apply. It does not close the window between that check and the
      upload itself: someone who can already write inside the outbox can
      substitute the file in between. Closing it properly means holding an open
      descriptor from the check through the send — Telethon's `send_file` takes
      a file object, but then the name has to be supplied as a
      `DocumentAttributeFilename` and the type detection it does from the path
      is lost, which is a bigger change than the residual risk warrants today.
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

- [ ] **A ban leaves the banned person's messages in place.** Telegram can
      delete a member's whole history along with the ban
      (`channels.deleteParticipantHistory`), which is what a moderator usually
      wants for a spammer. It is deliberately not wired up: it deletes other
      people's messages, which `message.delete` refuses to do for the same
      reason, and a plan whose summary said "ban" while silently erasing a
      hundred messages would be the opposite of a reviewable preview. If it is
      added it needs to be its own flag, spelled out in the summary with the
      number of messages it would remove.

- [ ] **Nothing reports the moderation state a plan is about to change.**
      `chat.members` lists participants and admins, but there is no way to ask
      "who is banned here", "what is this person already restricted from" or
      "when does their restriction expire" — so an unban is planned against a
      ban nobody verified exists, and a restriction cannot be diffed against the
      one already in force. Telegram answers all three with
      `channels.getParticipant` and `ChannelParticipantsBanned`; the question is
      whether that belongs in `chat.members` as a filter or in a read of its own.

- [ ] **Muting and archiving are gated by the *send* allowlist.** `chat.mute` and
      `chat.archive` declare `Capability.SEND`, because that is the rule which
      already governs acting on a chat and a capability of their own would be a
      policy surface nobody asked for. The cost is a real case: a noisy chat this
      configuration may read but never write to cannot be muted through the tool.
      Whether changes only the account owner can observe deserve a write capability
      of their own is a decision nobody has taken.

- [ ] **A scheduled message can be created here but not cancelled here.**
      `message.schedule` puts one in Telegram's queue and `scheduled.list` reads the
      queue back; nothing deletes an entry or fires one early, and `ops/pending.py`
      names the two requests it refuses to make. The asymmetry is deliberate — the
      queue is visible in the app, where cancelling is one tap and needs no agent,
      which is most of the reason the operation exists — but it does mean a wrong
      schedule applied from a headless machine has to be undone on a phone.

- [ ] **The tool gate lists names, and nothing else.** `mcp.tools` is a flat
      allow list of exact tool names: no deny form, no patterns, no "every read
      tool" or "everything tagged `archive`". Each of those is a small language,
      and a small language for permissions is the thing this project has
      repeatedly refused to grow — but the cost is real. An operator who wants
      the twenty read tools and none of the plan tools writes twenty names, and
      writes them again when one is renamed. The `tags` already on every
      `Operation` are the obvious raw material if this is ever taken further,
      and taking it further should be a decision rather than a convenience.

- [ ] **Client roots do not constrain the outbox.** Roots are applied to what an
      MCP call *writes* (`paths.downloads`, `paths.archive`) and not to
      `paths.uploads`, where an applied plan *reads* from, so a client that sanctions one
      directory cannot thereby stop a file being sent out of another. The reason
      is structural rather than an oversight: a plan is applied from a terminal
      by `tg-ai plan apply`, where there is no MCP session and no roots to ask
      about, so the check could only run at *plan* time and would be absent at
      the moment the bytes actually leave. A ceiling that holds in one of the two
      places is worse than an operator knowing it holds in neither — the outbox
      rule in `outbox.py` is the one doing that work. Revisit only if plan
      preconditions grow a way to record "the client sanctioned this path", which
      the applier could then re-check.

- [ ] **The roots check is a check on a path, not a lock on a directory.** It
      canonicalises `paths.downloads` (or `paths.archive`) and compares, then the
      operation opens the account, spends a network round trip and only then
      writes — so someone who can replace that directory with a symlink *on this
      host* in between still redirects the write, and `O_NOFOLLOW` in
      `ops/media.py` only covers the last path component. Closing it properly
      means holding an open directory descriptor across the check and writing
      through `openat`, which is a change to how every local write opens its
      file rather than to roots. It does not widen anything roots were supposed
      to close: an attacker with write access to the download directory's parent
      is already inside the local-user boundary this project draws in
      `docs/threat-model.md`. Raised by review.

- [ ] **Roots are re-read on every call, and a change to them is not noticed
      between calls.** Each `local_write` asks the client afresh (one `roots/list`
      round trip per download, which is cheap and always current), but nothing
      subscribes to `notifications/roots/list_changed`, and the published *tool
      list* never changes in response to roots — a client that revokes the root
      containing `paths.downloads` keeps seeing `telegram_media_fetch` in its tool
      list and finds out on the call. Hiding the tool instead would mean the list
      a client cached and the list that is callable could disagree, which is the
      worse failure of the two.

- [ ] **The outbound ledger has no command of its own.** `ledger.prune()` exists
      and nothing calls it, exactly as `limits.prune()` does; rows accumulate at
      one per applied message and are read back only through the window, so this
      is a disk-space question rather than a correctness one. What is missing for
      a person is a way to *look*: "has this already gone out", and "what did this
      account send in the last six hours", are both one `SELECT` away and have no
      surface. A read operation would have to decide what it may show — the
      fingerprint is a digest, but the peer id and the timestamps are not — and
      that decision has not been taken.

- [ ] **Two different plans carrying the same message can still both pass the
      duplicate check** if they are applied in the same instant. The check
      (`_refuse_duplicate`) and the record (`ledger.record`) sit either side of
      the RPC rather than inside one transaction, which is what keeps the refusal
      before the rate-limit reservation and the row before the request leaves.
      Applying *one* plan twice is already impossible — the plan store's claim is
      a conditional UPDATE — and the gap that remains is one an approving human
      stands in, so it was left open rather than closed with a lock that would
      have to be held across a network call. Closing it properly means reserving
      the fingerprint at check time and settling it like a rate-limit slot.

- [ ] **Refunding a rate-limit slot and dropping a ledger row are two
      transactions.** Both settle the same question — did the request take
      effect? — but they live in different modules, and welding them into one
      `BEGIN IMMEDIATE` would mean one store reaching into the other's
      connection. The order was chosen instead: the ledger row goes first, so a
      crash in between leaves an over-counted rate limit (one send of budget)
      rather than a phantom duplicate (a refusal for something that never
      happened). Closing it properly means a settlement transaction both stores
      can join.

- [ ] **Only the four message-producing operations are ledgered.**
      `message.send`, `message.reply`, `message.send_file`, `message.forward`.
      The rest are either idempotent at Telegram's end (reacting, joining) or
      invisible to anybody but the account owner (archive, mute), so a duplicate
      check would refuse a lot and prevent nothing. `chat.create` is the one that
      could be argued: applying two identical plans makes two groups with the same
      title, and nobody has decided whether that is a mistake or a Tuesday.

## Known gaps in the CLI surface

- [ ] **List- and object-valued arguments have no CLI form.** `_options_for` in
      `cli.py` maps a `list[int]` to a single `int` (no `multiple=True`) and a
      nested model to a string, so `message delete` / `message forward`
      (`message_ids`), `chat create` (`users`), `chat promote` (`rights`) and
      `chat restrict` (`restrictions`) cannot be planned from the terminal at
      all — only through their MCP tools. Either teach the generator repeated
      options and a JSON form, or say so in `--help` rather than only in
      `docs/operations.md`. The moderation operations make this sharper: `chat
      ban`, `chat unban`, `chat kick` and `chat demote` *are* usable from a
      terminal, so `chat restrict` is now the one moderation action a person
      cannot plan without an MCP client.
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

- [ ] **A release that uploads to PyPI and then fails to draft cannot be
      recovered by re-running the whole workflow.** `github-release` needs
      `publish-pypi`, and `publish-pypi` sets `skip-existing: false` on purpose —
      so a full re-run stops at the duplicate upload and the draft is never
      created. "Re-run failed jobs" does the right thing (the publish succeeded,
      so only the draft job re-runs), which is why this is a rough edge rather
      than a hole; the sharp fix is a recovery path that compares the PyPI
      digests before deciding a re-upload is a duplicate, and that is a real
      design question rather than a flag. Raised by review, 2026-08-23.

## Decisions

- [ ] Decide on **MCP-registry** publication (explicitly deferred by the owner
      in the design, §12.5 — a name claimed there is claimed forever). The PyPI
      half of that decision is taken: the pipeline exists and publishes from a
      `v*` tag, and what remains is the owner's manual setup, listed under "For
      the owner, on GitHub" below.
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
- [ ] **Register the PyPI pending publisher.**
      [pypi.org/manage/account/publishing](https://pypi.org/manage/account/publishing/)
      → *Add a new pending publisher*, with exactly: project `telegram-ai-cli`,
      owner `stufently`, repository `telegram-ai-cli`, workflow `release.yml`,
      environment `pypi`. "Pending" is the form for a project that does not
      exist yet; the first upload creates it. Until this is done the publish job
      fails closed. Full context: README → Releasing.
- [ ] **Create the `pypi` GitHub environment** (Settings → Environments), named
      exactly that — the name is half of the trust relationship above. Worth a
      required reviewer on it: that gate is the last point at which a release
      can be stopped after a tag is pushed.
- [ ] **Then push the first `v*` tag** — after bumping `version` in
      `pyproject.toml` and landing it on `main`. The workflow refuses to build
      if the tag and `pyproject.toml` disagree.
- [ ] **When PyPI publication happens:** re-check `keywords`, `description` and
      `[project.urls]` in `pyproject.toml`, and register the project on an MCP
      registry under the same name and description.
- [ ] **After the first tagged release:** add this repo to the "Related
      projects" lists in `zabbix-ai-cli` and `yandex-mcp`, which this README
      already links back to.
