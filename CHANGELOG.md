# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project intends
to follow [Semantic Versioning](https://semver.org/) once a `1.0.0` is tagged —
before that, breaking changes can happen on any `0.x` release.

## [Unreleased]

### Added

- `tg-ai account block` / `telegram_plan_block_user` and `tg-ai account unblock` /
  `telegram_plan_unblock_user` — stop one person from writing to or calling this
  account, and let them back. **This is a setting of the account, not a chat
  ban**, and the plan summary says so in the sentence a reviewer reads: nobody is
  removed from any chat, no ban is created or lifted, and the effect exists only
  between that person and this account. The two are one word apart in a tool
  listing, and approving the wrong one is not something reading the other
  afterwards undoes. The person is judged against `safety.write.admin` — the list
  that already says which people this tool may act on — and against the hard
  denylist first, so Service Notifications stays unreachable. A peer that is not
  a person is refused, at planning time and again at apply time, rather than
  quietly doing something else.
- `tg-ai chat create --kind channel` / `telegram_plan_create_group` — the same
  operation now creates a broadcast channel as well as a supergroup, and says
  which in the preview along with what it means (only admins post, everyone else
  reads). Neither kind gets a public link: a chat created here has no username
  until somebody gives it one. A channel is refused any initial `users` — an
  audience assembled by the same approval that created the broadcast is an
  audience nobody chose, and `chat.invite` admits people one approval at a time.
  The kind is a precondition, compared again at apply time.
- `tg-ai chat set-title` / `telegram_plan_set_chat_title` and `tg-ai chat set-about`
  / `telegram_plan_set_chat_about` — rename a chat or change its description. Both
  previews quote the **current** value next to the replacement, because Telegram
  keeps no copy of either: once applied, the old one exists nowhere but the plan.
  Both record the current value's digest and refuse at apply time if it moved, so
  a plan approved against one title cannot overwrite another. `set_about` spends
  an extra request fetching the description no entity carries, rather than
  showing an empty "current" it never checked; a change that would alter nothing
  is refused, and the refusal names the chat by id because an error envelope is
  outside the wrapper that marks stranger-written text as data.
- `tg-ai chat set-photo` / `telegram_plan_set_chat_photo` — replace a chat's photo
  with an image from the outbox. The file is chosen by `outbox.resolve_outbound`,
  the same rule `message send-file` uses rather than a second copy of it: two
  answers to "which local file may leave this machine" is how the weaker one
  becomes the hole. On top of that rule, the file must be a format Telegram
  compresses (JPEG or PNG, decided by the same `classify` that decides how a send
  is presented), and the plan records the id of the photo being replaced — a
  photo cannot be quoted in a preview the way a title can, so the one going out
  is named by digest and the one being replaced by id, and a *different* photo
  appearing between review and apply is refused. A chat photo draws on
  `upload.timeout_seconds` rather than the 60-second per-RPC ceiling, because it
  is a transfer.
- All five refuse a peer of the wrong shape before anything is planned: a chat id
  is not a person to block, and a private conversation has no title, description
  or photo of its own. Without the check the plan would be approved and then fail
  inside Telethon at apply time — the one moment nothing can be done about it.
- **`mcp.tools` — a tool-visibility gate on the MCP surface.** An operator-set list
  of the tool names this server may publish (`TGAI_MCP__TOOLS='["telegram_chats"]'`),
  for one property no per-peer rule provides: *a prompt injection cannot invoke a
  tool it never saw in the tool list.* It is a coarse second layer in front of the
  permission matrix, never a replacement — the profile, the capability rules and the
  hard denylist all still run underneath.
  **Unset publishes every tool, exactly as before**: the gate is opt-in, because the
  fail-closed reading that protects an empty `allow` elsewhere would here make a
  fresh install publish nothing and get a list pasted in without thought. Set to an
  empty list it publishes nothing, which *has* been thought about and reads like
  every other empty allow list.
  It filters the **call path as well as the list** — a tool name is a guessable
  string, and a filter that only hides is cosmetic — refusing a hidden tool with
  `FORBIDDEN_BY_ALLOWLIST`. It can only narrow: the configured names are intersected
  with what the registry already publishes, so nothing here can conjure a tool, least
  of all one that applies a plan. **An unknown name refuses to start**, naming every
  unknown entry at once, the same fail-loud rule as a relative `paths.uploads`: a
  silent drop would turn a typo into a tool missing for a reason nobody can see, and
  a warning on the stderr of a stdio server is a line the client swallows.
- **MCP roots as a ceiling on where a call may write.** When a client advertises
  roots — the directories it sanctions — an operation that writes to this machine is
  refused if the path *it* writes is outside every one of them, before the account is
  opened and before anything is created, naming the path refused. It is **not
  redirected** to a directory the client would accept: the quota is walked against the
  configured directory and that is where the operator looks for files, so a silent
  move would make both wrong at once. **Each operation is judged by its own
  destination**, now declared as `Operation.local_path` and required of every
  `local_write` by the registry's invariants: `media fetch` writes
  `paths.downloads`, `archive sync` and `archive forget` write `paths.archive`.
  Checking all three against one directory — the first cut of this, caught in
  review — refuses an archive write over a download directory it never touches, and
  lets one through on the strength of a directory it never touches either. The two
  absences are different and both are honoured — a
  client that never declared the capability cannot be asked and constrains nothing
  (every client without roots keeps working), while a client that declared it and
  answered with an empty list was asked and sanctions none. One that declared it and
  then fails to answer is refused: it said it does roots, and a transport error is
  not permission. Containment is decided on canonical paths — `realpath` first,
  comparison on path components after — so a symlink out of a root, a `..` in the
  middle, and `/srv/data-evil` against a root of `/srv/data` are all refused. A root
  that is not a usable local directory — another scheme, another host, a relative
  path, an embedded NUL that would otherwise raise out of `realpath` — is dropped,
  and dropping the last one leaves the empty-list refusal rather than a traceback.
  It is the same containment check `message send-file` applies to the outbox, now
  shared in `telegram_ai_cli/roots.py` rather than written twice.
- **A release pipeline: a `v*` tag publishes to PyPI, with no token stored
  anywhere.** `.github/workflows/release.yml` runs the full CI matrix, builds
  an sdist and a wheel, uploads them over **Trusted Publishing** (OIDC) and
  drafts a GitHub release carrying the same artifacts and their SHA-256 sums.
  Nothing else can trigger it; an ordinary push to `main` publishes nothing.
  **No PyPI API token exists.** GitHub mints a short-lived token identifying
  this workflow file, in this repository, in the `pypi` environment, and PyPI
  exchanges it for a one-off upload credential — so there is no secret to leak
  or rotate, and the job fails closed until the pending publisher is registered
  on PyPI's side. The workflow file name and the environment name are part of
  that trust relationship rather than cosmetic, and both are commented as such.
  **The tag is the version, and the workflow refuses to build if it is not.**
  setuptools reads the version from `pyproject.toml` and PyPI reads it from the
  package metadata; neither consults the tag. So a `v0.2.0` tag on a tree that
  says `0.1.0` would publish `0.1.0` under a release everybody reads as
  `0.2.0` — permanently, because a version on PyPI can never be replaced or
  re-uploaded. It is compared before a single artifact is built.
  **The archives are checked against the working tree, not spot-checked.**
  Every `.py` file under `telegram_ai_cli/` must be present *and non-empty* in
  both the wheel and the sdist. This repository has shipped an empty package
  twice over — once from an unanchored `accounts/` in `.gitignore`, once from a
  shared build directory — and both times it built, installed and looked
  entirely successful. `twine check --strict` then validates the metadata PyPI
  is about to read, so a bad long-description type is a refusal here rather
  than a rejection after the version number is spent.
  **The CI matrix is called, not copied.** `ci.yml` gained a `workflow_call`
  trigger and the release job `uses:` it, so a tag runs the same lint, the same
  three Python versions, the same MCP stdio smoke test and the same container
  check that a pull request does. A second copy of those steps would drift, and
  the copy that drifts is the one nobody looks at until a release publishes
  what the tests it did not run would have caught.
  `build`, `twine` and `wheel` are pinned in `constraints.txt` like everything
  else: the one run that produces an artifact which can never be replaced is the
  worst place for "whatever pip resolved today". The build step exports
  `PIP_CONSTRAINT` as well as passing `--constraint`, because `python -m build`
  resolves `[build-system] requires` inside a *fresh isolated environment* where
  nothing the outer `pip install` did is visible — without it the pinned
  setuptools applied to the wrong environment and the actual build used whatever
  PyPI served that minute (raised by review). Archive names are parsed with
  `packaging.utils` rather than string-matched, since a back end writes the PEP
  440 *normalized* version into a file name and `1.0-rc1` becomes `1.0rc1` —
  a raw comparison would have rejected a perfectly good release (also review).
  README gains a **Releasing** section with the three things the repository
  owner has to do by hand.
- `telegram_ai_cli/py.typed`. `Typing :: Typed` has been in the classifiers all
  along; without the PEP 561 marker beside the package, mypy and pyright ignore
  every annotation in it and the classifier is simply untrue. The release
  workflow checks the marker is in the wheel, because it is a zero-byte file
  that nothing else would notice the absence of.

- `tg-ai message schedule` / `telegram_plan_schedule_message` — plan a message for a
  given time, or for the moment the other person is next online. The point is not
  that it goes out later: once the plan is applied the message sits in **Telegram's
  own scheduled queue**, where the owner sees it in the app and can cancel it there —
  from a phone, with no agent running and no terminal open. `at` is ISO-8601 and must
  carry an explicit UTC offset; a naive time is refused rather than guessed, and the
  summary prints the offset form, the UTC equivalent and the distance from now,
  because a line that says "09:00" without saying whose is one nobody can check. At
  least two minutes and at most a year ahead — the floor is the applier's own budget,
  since Telegram sends a nearly-due schedule immediately. `when_online` is Telegram's
  own mode and needs a one-to-one chat, reusing the sentinel `telegram_scheduled`
  already reads back rather than a second copy of it; if the recipient is online
  already it goes out at once instead of waiting in the queue, which the summary says
  and the apply step warns about. A plan whose moment has passed is **refused** at
  apply time rather than sent late, and what apply compares against the plan is the
  whole message — time, body digest, `silent`, `link_preview` — not only its clock.
- `tg-ai chat archive` / `telegram_plan_archive_chat` and `tg-ai chat mute` /
  `telegram_plan_mute_chat` — move a chat into Archived or back out of it, and mute it
  for a while, indefinitely, or not at all. These are the two writes nobody else can
  observe: they change this account's own chat list and its own notifications, and the
  other side is not blocked, left or banned, keeps writing, and cannot tell. Both plan
  summaries say that in words, because the risk here is not the effect but a reviewer
  filing "mute" next to "ban". A mute records its *duration* and computes the deadline
  when the plan is applied, so one approved in the evening and applied next morning
  still mutes for the eight hours that were reviewed; "for ever" stays a sentinel
  rather than being rendered as a date in 2038.

- **Reactions and pins** — `telegram_plan_react_message`,
  `telegram_plan_unreact_message`, `telegram_plan_pin_message`,
  `telegram_plan_unpin_message` (`tg-ai message react` / `unreact` / `pin` /
  `unpin`). Reading reactions has been possible since `telegram_message_reactions`;
  leaving one was not, and neither was the single most visible act a member of a
  chat can perform short of speaking — putting a message at the top of everyone's
  window. All four are `remote_write`: planned, reviewed, and applied from a
  terminal, with no direct MCP tool, which the registry's invariants enforce.
  **These are the first writes that act on other people's messages.** `edit` and
  `delete` refuse a message this account did not send; a reaction is *for*
  somebody else's message and pinning one is the whole point of pinning, so that
  guard cannot apply and the preview carries the weight instead. It names the
  emoji *and* spells out its codepoints — two emoji can render identically, and
  the terminal sanitizer strips joiners out of a multi-part sequence — quotes the
  message, and says whether this account or somebody else wrote it.
  **A pin says who finds out.** By default every member gets a notification and
  the banner appears at the top of their window, and the summary says so;
  `silent` suppresses the notification, not the banner, and the summary says
  that too. In a one-to-one chat a pin lands on this side only unless
  `both_sides` is set — the flag is refused rather than ignored anywhere it
  would not mean that. Unpinning is never one-sided: a banner left at the top of
  the other person's window is not what "unpinned" says.
  **Telegram's reaction call takes the account's whole list, not a delta**, so
  reacting replaces whatever this account had reacted with unless `keep_existing`
  is set, the summary says which of the two is happening, and the plan records
  what was there. The applier refuses if that moved — otherwise a plan reviewed
  against one set and applied against another would silently discard a reaction
  nobody looked at. `add_to_recent` is off: reordering the "recently used" row on
  the owner's own phone is an invisible side effect of an act approved for a
  different reason. And where the account has left a paid (star) reaction, any
  change that would have to re-send it is refused: re-sending one is a purchase,
  not a copy.
  **A no-op never reaches the review queue.** Reacting with the reaction already
  there, removing one this account never left, pinning what is already pinned,
  unpinning what is not pinned — each is refused while the plan is written. A
  plan still costs a person the reading of it.
  **A caption-less attachment is identified, not just counted.** The shared
  message snapshot digests the *body*, which is empty for every photo posted
  without a caption — so an edit that swaps the picture leaves it unchanged, and
  these four are exactly the operations that act on messages somebody else can
  still edit. The plan records the attachment's type and id, quotes them in the
  preview, and the applier refuses if they moved.
  **The refusals these two calls have of their own are treated as refusals**:
  `REACTION_INVALID`, `REACTIONS_TOO_MANY`, `PREMIUM_ACCOUNT_REQUIRED`,
  `CHAT_NOT_MODIFIED`, `PIN_RESTRICTED` and their neighbours join the whitelist
  of errors that prove nothing happened, so they give the rate-limit slot back
  instead of landing in `unknown_outcome` and sending somebody to look at a chat
  where nothing occurred.
  **They take a `t.me` link.** `chat` accepts a permalink and takes the message
  number from it, through the same parser and the same two guards the reads use
  (`links.parse_telegram_link`, `chats.guard_message_link`), so a link into a
  one-to-one conversation and a `?comment=` link are refused here exactly as they
  are there. Reacting is judged by the `send` policy and pinning by `admin`,
  because a pin changes what every member of the chat sees.
  `telegram_ai_cli/ops/marks.py`, `tests/test_reactions_pins.py`.

- **QR login — `tg-ai account login-qr`.** A second way to authorise an account,
  alongside the phone code that stays the default: the command draws a QR code
  in the terminal, an app that is already signed in scans it (Settings → Devices
  → Link Desktop Device), and nothing is typed anywhere. No phone number is
  needed, so a label that was never registered is enrolled by this command
  alone.
  **The login token is treated as the credential it is.** `tg://login?token=…`
  *is* the login — whatever imports it becomes the account, with no code and no
  password — so it goes to the terminal and nowhere else: not the log at any
  level, not the audit record, not the result envelope. It reaches a display
  callback rather than a logger, which is what the test asserts. Specifically
  **not stdout**: `tg-ai --json account login-qr > out.json` would otherwise
  write a live login token into a file (and emit invalid JSON while doing it).
  The code is drawn on the controlling terminal — `/dev/tty`, or `stderr` when
  that is itself a terminal — and if there is neither, the command refuses
  *before* asking Telegram for a token, because a credential that has been
  minted and cannot be shown is one that existed for nothing. The raw URL is
  printed under the code on purpose, as the fallback for terminals that cannot
  draw block characters.
  **Expiry is the ordinary case, and it is bounded.** Telegram expires the token
  in well under a minute, which a person walking off to fetch their phone will
  miss, so the code is regenerated and redrawn up to four times — and then the
  command says so rather than redrawing forever while holding the account's
  session lock. Expiry has two shapes and both are handled: Telethon's
  `TimeoutError` when nobody scanned, and Telegram's own `AUTH_TOKEN_EXPIRED`
  when a scan lands just after the deadline. A flood wait asking for a token
  becomes this project's `FloodWait` rather than escaping as a raw Telethon
  error past the `except TelegramAIError` that records the account's status.
  **The row records the account that actually signed in.** A QR code can be
  scanned by whichever account the person happened to be signed in as, so the
  number stored afterwards is that account's own (new `AccountStore.set_phone`),
  not the one the row carried in. A row whose phone names one account and whose
  session names another is worse than a row with no phone: it is the number a
  later `account login` would send a code to.
  **Two-step verification is the phone flow's own prompt**, called rather than
  copied, and both flows now run through one body: the `0600` session file, the
  frozen fingerprint, the lock and the "already authorised, do nothing"
  shortcut are written once and cannot drift apart between the two.
  **It is `local_admin` like the other account commands** — terminal only, and
  the registry refuses to publish it as an MCP tool. A third reason on top of
  the existing two: a tool that could ask for a login token would be handing a
  caller the account itself across the boundary the design exists to hold.
  Adds one dependency, `qrcode` (pure Python, no transitive dependency on this
  platform) — a QR encoder is not something to hand-roll into a project that
  holds session keys. `--invert` swaps the blocks for a dark-background
  terminal, which is a guess no program can make on the user's behalf.

- **Moderation, and an undo for every rights change** — `telegram_plan_ban_user`,
  `telegram_plan_unban_user`, `telegram_plan_kick_user`,
  `telegram_plan_restrict_user`, `telegram_plan_demote_admin` (`tg-ai chat ban`
  / `unban` / `kick` / `restrict` / `demote`). The project could hand somebody
  admin rights and had no way to take any back, which meant an agent could
  produce a state it was unable to reverse. Now `chat.ban` is paired with
  `chat.unban`, `chat.promote` with `chat.demote`, and `chat.restrict` either
  expires by itself or is lifted by the same unban — Telegram keeps a ban and a
  restriction in one `ChatBannedRights`, so one request clears both.
  **They are `remote_write` like every other effect**, which means each one is
  planned, reviewed and applied from a terminal; none of them has a direct MCP
  tool, and the registry's invariants refuse to publish one.
  **One member per plan.** Every input takes a single `user`, never a list:
  banning six people behind one approval is the blast radius the confirmation
  step exists to bound.
  **The preview is the point.** It names the chat and the person by title,
  `@username` and numeric id, lists each right a restriction takes away in
  words, says how long it lasts — and for the two the person on the receiving
  end cannot reverse, a ban and a kick, it says so in as many words.
  `duration_seconds` is a duration rather than a date, counted from the moment
  the plan is applied, because a plan can wait in the review queue for hours;
  it is refused outside 60 seconds … 365 days — inside the window Telegram
  silently reads as permanent, by a margin, because the deadline is computed
  here and read by a server one round trip and one clock away.
  **What a basic group cannot do is refused rather than faked**: it keeps no ban
  list and no per-member rights, so `chat.unban` and `chat.restrict` are refused
  while the plan is written and again at apply time, and `chat.ban` says on the
  approval screen that it can only remove the person there. A kick in a
  supergroup is a ban immediately lifted — the only way Telegram offers — with
  both requests issued explicitly; a failure of the *second* one is reported as
  an unknown outcome saying the person is **banned, not kicked**, with the
  rate-limit slot kept. Left to the general error handling a flood wait there
  counts as "no effect", which would refund the budget and close the plan as
  failed while the ban stood (raised by review).
  `tests/test_moderation.py` drives the planners over a fake client and the
  applier's own RPC step over another, which is how "planning issues no
  request", "one flag takes GIFs and inline results with it" and "a restriction
  is not a ban" are asserted rather than reviewed.

- **A local archive, and offline search over it** — `telegram_archive_sync`,
  `telegram_archive_search`, `telegram_archive_status`,
  `telegram_archive_forget` (`tg-ai archive …`). Until now every search was an
  RPC: it spent the account's flood budget, it could only match *text* —
  Telegram has no regular expressions — and it answered about one account per
  call. A named chat can now be copied into a SQLite file on this machine and
  queried there instead, with regular expressions, sender and date filters, and
  no Telegram request at all.
  **Nothing fills itself.** One chat per `archive sync`, named explicitly; there
  is no daemon, no background sweep and no key that turns on bulk collection. An
  archive that filled itself would turn a tool with an allowlist into a bulk
  collector of private correspondence, sized by uptime rather than by anything a
  person decided.
  **Re-running does not re-download.** Two watermarks per chat: a repeat call
  fetches what is new (bounding the request below with `newest_message_id`), then
  backfills older messages downward until the per-call budget runs out. Three
  separate fields report where it got to, because they are three questions:
  `contiguous` (no hole in the middle), `reaches_first_message` (the backfill got
  to the beginning) and `complete` (both).
  **An interrupted run leaves a resumable hole, not a permanent one.** A
  watermark is never advanced across a gap — moving it would leave the messages
  inside unreachable forever — and a resume cursor is stored so the next call
  carries on from where it stopped. Raised by review (2026-08-23): without that
  cursor, a chat that gained more messages than one budget could fetch
  re-downloaded the same page on every call and never joined the two ends,
  however often it was run. `archive search` warns when a chat it covered has a
  gap, because a hole answers "nobody said that" as confidently as a whole
  archive.
  **The read policy is applied on the *read*, not remembered from the write.**
  An archive is a snapshot of a decision that was true when it was taken, so
  every path out of it rebuilds the chat's `PeerRef` from the stored identity and
  asks the kernel again against today's configuration: a chat archived while it
  was permitted and closed since is withheld and *counted*, never quietly
  missing. The hard floor is checked twice — on the way in, so Service
  Notifications and Saved Messages never land on disk, and on the way out, so a
  database copied in from elsewhere cannot smuggle them back. Two narrowings
  keep that fail-closed (raised by review): a stored chat kind this build cannot
  parse is refused rather than treated as `unknown` — which is *not private*, so
  a private conversation in a foreign database would otherwise be judged under
  the group rule — and policy is decided on the numeric id alone, never on the
  `username` copied at sync time, since handles are reassignable.
  `tests/test_archive.py` drives the real handlers over a local fake client to
  assert both directions, plus the watermark, the trust boundary and the file
  mode.
  **Effect `local_write`, not `read`, for sync and forget.** They write a durable
  copy of somebody's private messages to this machine's disk; classifying that
  with the operations that consume nothing would be the quiet lie. Equally not
  `remote_write` — nothing they do is visible from Telegram's side — so the
  invariant that no MCP tool applies a plan is untouched. `media_fetch` set the
  precedent.
  **Not encrypted, deliberately, and the archive is masked on the way in.** The
  same state directory holds the `.session` files, and a session file *is* the
  account: encrypting the archive next to it would not raise the bar an attacker
  must clear, while it would make offline search and regular expressions
  impossible. `0600` in a `0700` directory and `.gitignore` do the work instead.
  Because the file persists what a live read never keeps, `redact()` runs on
  message text and sender names *before* the row is written — the trade being
  that a pattern cannot match what was masked.
  **Erasing is a first-class operation and is *not* allowlist-gated.**
  `archive forget <chat_id>` removes a chat and every message of it, and works
  for a chat the policy has since closed — that chat is precisely the one whose
  copy ought to go, and refusing would strand personal data with no way to
  remove it through the tool. Idempotent, and it takes an id rather than a
  username so that a chat since left can still be erased.
  The file mode is checked and narrowed on **every** open, not only at creation
  — a file left `0644` by an earlier version, a restore from a backup or a
  careless `chmod -R` is exactly what a create-time-only check never notices —
  and a symlink or non-regular file at that path is refused (raised by review).
  New setting `paths.archive` (default `…/telegram-ai-cli/archive.sqlite3`), a
  separate database from `state.db` so that erasing every archived message
  cannot take the account registry, the pending plans and the rate-limit history
  with it. Bounds: 5000 messages per sync call, 50 000 rows scanned per search
  (streamed off the cursor, not materialised) and a 500-character query ceiling
  — plus a **10-second `SIGALRM` budget on the matching phase of a regex
  search**, because those ceilings bound how much is matched and not how long a
  single match takes, and `re` has no timeout of its own. POSIX and main-thread
  only; elsewhere the search runs untimed rather than pretending.
- `Operation` gained `destructive` and `idempotent`, and the MCP adapter reads
  them instead of publishing a blanket `destructiveHint: false` /
  `idempotentHint: <is a read>` for every tool. `telegram_archive_forget` is the
  first to need them, and it is exactly the case where the guess was wrong in
  both directions. The hints are advisory, which is why a wrong one is worse
  than none: a client that auto-approves what it was told is harmless acts on
  the lie.

- `telegram_plan_send_file` / `tg-ai message send-file` — the first plan
  operation that sends *bytes*: one local file to one chat, with an optional
  caption and an optional reply, as compressed media or as an untouched
  document. Like every remote write it is planned over MCP and applied from a
  terminal; unlike the others, what it puts at risk is the host rather than the
  chat.
  **A caller names a file, never a path.** `media fetch` refuses to let a caller
  choose where a download lands; sending is that problem reversed and worse — a
  free choice of path would be a read of arbitrary bytes with a delivery
  mechanism attached (`~/.ssh/id_ed25519`, the `.session` file that *is* the
  account, `state.db` with its queue of unsent messages) into a chat other
  people read. So a file is sent from `paths.uploads` and nowhere else: a
  relative name is read from that directory rather than from whatever working
  directory launched the server, an absolute path is accepted only if it is
  inside it, and containment is decided *after* symlinks are resolved — a link
  in the outbox pointing at `/etc/shadow` has an innocent name and hostile
  bytes, and a prefix check on the string would pass it. The download directory
  is not an outbox by default (`upload.allow_downloads_dir`): a fetched file is
  one a stranger chose, and re-posting it into another chat should be an
  operator's decision rather than a tool call's.
  **The preview says what actually arrives.** The summary a person approves
  carries the name, the size in both human and exact form, the MIME type, the
  SHA-256, the directory it came from, and the delivery form — because "send
  photo.jpg" hides the one distinction that cannot be undone: a photo sent as a
  photo is re-encoded and the original file is not what lands, while the same
  file sent with `as_document` arrives byte for byte. The forms follow what
  Telethon actually does rather than what "an image" usually means — PNG and
  JPEG become photos and every other picture format goes as a document, because
  `utils.is_image` recognises no others — so the line a person approves
  describes the send that happens. `as_document` overrides all of it and is the
  only guarantee available about the bytes, since `force_document` is the one
  instruction Telethon always obeys.
  **The outbox is trusted, so it has to be trustworthy.** `paths.uploads` must
  be an absolute path — `Path("")` is `Path(".")`, and a blank one would quietly
  make the process's working directory the allowlist — and only its owner may
  write into it, since the rule rests on the files in it having been put there
  by the operator: a world-writable outbox is refused, and a group-writable one
  has the group write bit removed first (see **Fixed**, below). The file is
  opened once with `O_NOFOLLOW | O_NONBLOCK` and its type, size and digest all
  come from that descriptor: a name checked with `stat()` and opened afterwards
  can be a FIFO by the time it is opened, which blocks for ever before any
  timeout exists.
  **The size ceiling comes from `stat()`, not from a failed upload.**
  `upload.max_file_bytes` (100 MiB, itself capped at Telegram's 2 GiB) is
  checked when the plan is written and again before the transfer starts, so an
  oversize file is a refusal naming both numbers rather than a transfer that
  dies partway. Empty files, directories and devices are refused as input
  mistakes. The plan records the file's digest and the applier recomputes it: a
  file edited or swapped between review and apply fails the precondition
  instead of being uploaded, and the same bytes under another name are a
  warning rather than a refusal.
  **Uploading gets its own timeout** (`upload.timeout_seconds`, 300s) in place
  of the applier's 60-second per-RPC ceiling, which is generous for a request
  carrying a sentence and useless for one carrying a hundred megabytes. Getting
  that wrong is not a retry: a timeout partway through an upload is
  `unknown_outcome`, which costs a person a look at the chat.
- `telegram_folders` / `tg-ai folders` — the account's chat folders, which are
  the one grouping in this system a *person* authored: id, name, emoji, the
  chats each folder names or excludes, and the category flags it carries
  (`contacts`, `non_contacts`, `groups`, `broadcasts`, `bots`) or withholds by
  (`exclude_muted`, `exclude_read`, `exclude_archived`). `telegram_chats` and
  `telegram_inbox` take a matching `folder` argument — an id, or the name shown
  in the app — so an agent can sort by the sorting the user already did instead
  of guessing from titles and unread counts.
  **A folder is not a permission.** It is a list a user wrote and can name any
  chat the account can see, so the filter runs *last*, over rows the policy
  already allowed: it can only remove rows, never add one. A folder that names
  Service Notifications, Saved Messages, or a private chat this configuration
  does not enumerate changes none of those answers, and the folder listing
  itself reports such chats as a `hidden_peers` count rather than an id.
  `tests/test_folders.py` drives the real handlers to prove it.
  Saved Messages needed one more turn of the screw: a folder that contains it
  stores the account's *own user id*, an ordinary positive number that no rule
  can recognise without knowing whose account this is. When DM enumeration is on
  and a folder names any private chat, `get_me` supplies it and the id is
  withheld like any other closed chat; with enumeration off — the default — every
  positive id is withheld anyway and the call is not made. An `InputPeerSelf`,
  which carries no id at all, is counted rather than dropped.
  Membership is decided client-side (there is no request that answers "is this
  dialog in that folder"), so the whole rule is a pure function over plain facts
  with a test per branch: `exclude_peers` beats everything, a chat the user
  named survives `exclude_muted`, a bot arrives through `bots` and not through
  `contacts`, and a shareable `DialogFilterChatlist` — which carries no flags at
  all — admits nothing it was not given. Two withholding flags are narrower than
  their names, as in the official clients: `exclude_muted` keeps a muted chat
  with an unread *mention*, and `exclude_read` honours "mark as unread".
  "All chats" (`DialogFilterDefault`) is skipped rather than offered as a filter
  that filters nothing, and an account with no folders gets an empty list plus a
  warning saying so, never an error.
- `telegram_mentions` / `tg-ai mentions` — unread **mentions** and unread
  **reactions**, which Telegram counts separately from plain unread and which
  are the only two counters that mean *somebody addressed this account*. One row
  per chat across every permitted account: who mentioned or replied and what
  they said, and who reacted to this account's own messages with which emoji
  (`reactors`: `peer_id`, `name`, `kind`, `emoji`, `custom_emoji_id`, `date`).
  Only the reactors Telegram still flags as unread are listed — a page of unread
  reactions carries the older ones on the same message too, and reporting those
  would re-announce a reaction already seen.
  **Reading acknowledges nothing.** Telethon puts `GetUnreadMentionsRequest` one
  letter from `ReadMentionsRequest` (and the same for reactions): the first
  reports, the second clears the badge on every device the owner has. Only the
  `Get` pair is issued, and `tests/test_mentions.py` asserts on the whole list of
  requests the operation made rather than on its answer — an agent that looked
  and made a badge vanish from somebody's phone has caused an invisible,
  unrecoverable side effect.
  Enumeration gets the counters; `read_chat`/`read_dm` is still required per chat
  before its messages are fetched, checked before the request so a refused chat
  costs no call and is reported as withheld. Chats whose counters are zero are
  never asked about, and the candidates are ranked and cut to `limit` before any
  page is requested. Private chats are dropped *before* their counters are read,
  so the omitted tally cannot say how many conversations somebody is waiting in;
  stopping at the dialog-scan ceiling is a warning and `meta.truncated`, not a
  short list that looks complete; and a named `account` that fails is an error
  rather than an empty answer, since "no mentions" and "nobody looked" must not
  serialize identically.
- `telegram_inbox` ranks on unread reactions too: mentions first, then unread
  reactions, then volume, then longest waiting. A 👍 on this account's own message is a
  response *to it*; two hundred unread messages in a group are a busy group.
  Dialog rows (`telegram_chats`, `telegram_inbox`) carry the third count as
  `reactions`, and the inbox `totals` sum it. `mentions_only` still means
  mentions strictly — widening it silently would make the flag mean something
  else.

- `telegram_sessions` / `tg-ai account sessions` — the devices and applications
  this account is signed in on: device, app, `api_id`, country, region, first
  sign-in, last activity, whether it is the current session, and whether
  Telegram has seen it confirmed. The README has always reasoned about revoked
  sessions; nothing here could *look* at the list. **Read-only in the strong
  sense: no operation in this project ends a session**, deliberately — a read
  tool that can log a device out can log the owner's own phone out with no plan
  step in the way — and `tests/test_sessions.py` asserts that across the whole
  registry rather than leaving it to review. **The IP address is cut to its
  network** (`198.51.x.x`, `2001:db8:85a3::`) and the authorisation hash is not
  returned at all: the hash is the handle a terminating call would take and no
  client accepts it from a person, while a full address identifies a home
  connection precisely — this is the owner's own data, which is the reason to
  trim it rather than a reason to print it, because tool output travels into
  logs, tickets and other models' contexts. Device and app strings are wrapped
  as untrusted text: whatever client signed in chose them. Gated by
  `safety.read.sessions` (new, on by default) through the new
  `read_sessions` capability, which is account-scoped and names no peer.
- `telegram_drafts` / `tg-ai drafts` — text that was started and never sent,
  newest first, with the chat each draft belongs to. Filtered row by row in the
  kernel's own order (hard floor, then `include_private`, then the read policy
  of the draft's own chat), because a listing walks every dialog the account
  has rather than one chat the caller named. **A draft in Saved Messages or
  Service Notifications is neither listed nor counted** — a "1 withheld" tally
  would still say one exists there. Drafts withheld by *policy* are counted, in
  `warnings`, so a short list is explicable. Nothing clears a draft.
- `telegram_scheduled` / `tg-ai scheduled` — one chat's queue of messages
  waiting to be sent, soonest first. Rows are the usual message shape plus
  `scheduled_for` (the intended send time, which Telegram stores in `date`) and
  `send_when_online` for the sentinel timestamp that otherwise renders as 19
  January 2038; `link` is always `null`, because a scheduled message's id
  belongs to a separate sequence and a `t.me` link built from it would address a
  different message in the same chat. One chat at a time, because Telegram
  publishes no global list — the asymmetry with drafts is stated rather than
  hidden behind an argument that would silently return nothing. Cancelling one,
  or sending it early, is not part of this project.

- `telegram_watch` / `tg-ai watch` — event-driven waiting, so an agent stops
  learning that a message arrived by asking `telegram_inbox` again. It registers
  a Telethon update handler and blocks until something lands in a chat the
  policy permits. **A burst is one answer:** the first message opens a debounce
  window (`debounce_sec`, default 2s) that every further message restarts, so
  four fast replies wake the caller once and come back together — polling costs
  a turn, and the system prompt with it, whether or not anything happened, and
  waking once per reply would re-create that cost inside the tool meant to
  remove it. **The wait always ends:** `timeout_sec` is capped in the published
  schema at 300s, because an MCP client cannot abandon a call it is waiting on;
  the ceiling is absolute and covers connecting, resolving named chats and
  resolving an event's chat, since a bound measured from the first read would
  bound the waiting rather than the call. Returning with no events at the
  ceiling is a result (`events: []`, `stopped_because: "timeout"`,
  `waited_sec`), not an error. The subscription is opened *before* the named
  chats are resolved — a message arriving during that round trip would
  otherwise be dispatched with nobody listening — and its queue is bounded at
  1000, because nothing here can slow a flood down. **A refused chat
  leaves no trace:** the policy filter runs *before* the debounce logic, so a
  message from a peer the configuration does not permit neither starts a burst
  nor extends one — and unlike `telegram_search` this operation deliberately
  does not report how many events it withheld, since a count of activity in
  chats the caller may not read says a specific conversation was busy at a
  specific second. Naming chats narrows the watch and never widens it. ⚠️ It
  holds that account's session lock (`accounts/lock.py`) for the whole wait, so
  nothing else can use the same account meanwhile — see `docs/operations.md`
  for what that means in practice and why the ceiling is five minutes.
- `telegram_ai_cli.untrusted` — an explicit instruction boundary in tool output.
  Values a person outside this system wrote (message body, media caption, display
  name, chat title, inbox preview, forwarded-from name, profile text, admin rank)
  are delimited with `⟦untrusted⟧ … ⟦/untrusted⟧` at the point results are
  assembled, and the delimiters are published in `meta.untrusted_markers`.
  `meta.untrusted_content` said a *response* contained stranger-written text; it
  never said which spans, so a message body arrived indistinguishable from this
  project's own fields. **A sender cannot close the wrapper:** `⟦` and `⟧` are
  replaced with `[` and `]` inside wrapped content unconditionally, so no
  spelling of a forged marker in a message body can end the frame — structural,
  rather than a pattern match that has to enumerate every casing. Ids, dates,
  counts, links and `username` are never wrapped, so existing parsers keep
  working, and `untrusted.unwrap()` is the supported way back to a raw value.
  Strings that are *not* wrapped are still defanged, because a name-based
  allowlist is a promise the names are complete and they were not — a document's
  `mime_type` is typed by the uploader and carried a forged marker straight
  through on the first pass. It is now wrapped like anything else a person
  types, and `render.sanitize` defangs the delimiters on the terminal-facing
  paths (plan summaries, warnings, table cells) that get no wrapper of their own.
  `telegram_plan_*` results are inside the boundary too: their `summary` quotes
  chat titles and message bodies, and it is built outside `telegram_result`.
- `telegram_ai_cli.links` — `t.me` links parsed and produced. Public
  (`t.me/name/123`), private (`t.me/c/<internal>/<id>`) and forum-topic
  (`…/<topic>/<id>`, `?thread=`) forms, as a pure function with no network and no
  Telethon. Telegram's own deep links (`t.me/share`, `t.me/login/…`,
  `t.me/proxy`, …) are declined rather than read as a chat named `share`, and a
  path longer than Telegram produces is declined rather than truncated.
- Two link shapes are refused instead of interpreted, after the policy check:
  a message link into a one-to-one chat (`t.me/someone/123` opens a profile and
  addresses no message, so the number is not a message id there) and a comment
  link (`?comment=` addresses the channel's discussion group, a different chat).
- Message permalinks in output (`link` on every serialized message, and on the
  reactions payload). `null` where Telegram has no such address — a one-to-one
  chat or a basic group — rather than a well-formed URL that opens a profile and
  addresses no message.
- Reactions: per-emoji counts (`reactions`) on every serialized message,
  distinguishing `null` (no reaction block at all) from `[]` (nobody reacted),
  plus a `telegram_message_reactions` read tool / `tg-ai message reactions` for
  one message — counts, total, permalink and whichever recent reactors Telegram
  already attached. The full list of *who* reacted is never requested, and where
  it is unavailable the payload says so. Each row carries a `kind`
  (`emoji`/`custom_emoji`/`paid`/`empty`): two of Telegram's four reaction types
  carry no emoji, and without it a paid star reaction serialized as a blank one.
- Read state on `chat read`: `data.read_state` (the dialog's read pointers, from
  a call that acknowledges nothing) and `read_by_me` / `read_by_peer` per
  message. Outside a one-to-one chat Telegram tracks reading per member behind a
  separate privacy-controlled request this tool does not make — reported as
  `peer_receipts: false` with a reason, with the per-message field left `null`
  rather than `false`. Skippable with `include_read_state: false`.
- Forum topics, on both sides of the read. `telegram_chat_topics` / `tg-ai chat
  topics` lists a forum supergroup's threads — id, title, icon (colour and the
  custom-emoji id, as a string because it is 64-bit), creation date, per-topic
  unread and mention counts, closed/hidden/pinned, and a permalink — and
  `chat read` takes `topic_id` (or a topic link) to page one of them, sending
  `messages.getReplies` through Telethon's `reply_to` instead of a flat history
  fetch. A flat read of a forum is not a partial answer but a wrong one: two
  unrelated threads arrive interleaved as a single dialogue, in an order nobody
  ever saw. **The previous "the link names topic N; this page is not filtered
  to it" warning is gone** — the page is filtered now. A `ForumTopicDeleted`
  row keeps the shape with `deleted: true` and `null` counters rather than
  vanishing, because a missing row looks like the end of a page, and the draft
  Telegram attaches to a topic is deliberately not serialized. Refusals rather
  than empty pages: `chat topics` on a chat that is not a forum says so
  (`NOT_FOUND`, with "read it with `chat read`"), `topic_id` on one is
  `INVALID_INPUT`, and so is `topic_id` together with `search` — Telegram's
  replies call carries no query, and Telethon prefers it over the search
  branch, so the two together would drop one of them in silence. Reading a
  forum *without* a topic stays allowed and gains a warning instead: it is
  several conversations interleaved into one list, and every row already says
  which topic it came from. A topic page still reports the *chat's* read
  pointers, since a forum has one dialog for all its topics; that too is said
  in a warning rather than left to be misread. All of these refusals name the
  chat by id and never by its title — an error is assembled outside
  `telegram_result`, and `Envelope.failure` neither wraps nor defangs, so a
  quoted title would leave as unmarked stranger-written text.
- `topic_id` on serialized messages, so a forum message says which topic it is in.
- Project scaffolding: MIT license, `pyproject.toml` targeting Python 3.12+ with
  dependency floors (`telethon>=1.44,<2`, `click>=8.2,<9`, `pydantic>=2.11,<3`,
  `mcp>=2.0,<3`, and the rest), and `constraints.txt` pinning the exact versions
  the Docker image and CI build against.
- `telegram_ai_cli.errors` — the stable error taxonomy every surface returns.
  Codes are an explicit `StrEnum` rather than string literals scattered through
  the codebase, because a caller (human or model) branches on `code` and
  `retryable`, and renaming a code silently would be a breaking change to a
  contract nobody agreed to break.
- `telegram_ai_cli.envelope` — the one JSON response shape shared by the CLI and
  the MCP server, with `meta.truncated`/`truncated_reason` for anything cut for
  size and `meta.untrusted_content` marking any payload carrying text that came
  from Telegram, so a model reading it knows to treat it as data, not instruction.
- `telegram_ai_cli.config` — YAML configuration overlaid by `TGAI_`-prefixed
  environment variables (via `pydantic-settings`), with the safety, limits,
  plans, download, audit and secrets sections all typed and validated.
- `telegram_ai_cli.safety` — the capability-matrix policy kernel: read/write
  permission is decided per capability (`READ_CHAT`, `READ_DM`, `SEND`, `ADMIN`,
  `JOIN`, `PROFILE`, …) rather than from three generic allow/deny lists, because
  operations like `forward` (source *and* destination) or `create_group`
  (no chat id exists yet when it's planned) don't fit a flat model.
- `telegram_ai_cli.redact` — pattern-based masking for phone numbers, emails,
  card numbers (Luhn-checked), BIP39 seed phrases, TON and EVM addresses,
  Telegram login codes, and API-token-shaped strings, applied to any structure
  before it leaves the process.
- `telegram_ai_cli.render` — terminal-output sanitization: ANSI/OSC escape
  sequences, carriage returns, control characters and bidirectional-override
  characters are stripped from any Telegram-authored text before it is shown to
  a human, so what a person approves in `tg-ai plan show` is what will actually
  be sent.
- `telegram_ai_cli.audit` — a two-phase, append-only JSON-lines audit log
  (`attempt` before an RPC leaves, `outcome` after), file-locked and `fsync`'d
  per write, with control characters escaped on the way in so a logged value
  can't forge a second record.
- `telegram_ai_cli.limits` — persistent, SQLite-backed rate limiting (per
  account, per target, and fleet-wide), with the slot reserved before the
  network call and released only on a whitelisted class of exception that
  proves the call had no effect.
- `telegram_ai_cli.secretbox` — AES-256-GCM encryption at rest for `api_hash`,
  proxy credentials and plan bodies, keyed by an externally-held
  `TGAI_SECRET_KEY` (or a generated, `0600` key file) rather than anything
  stored alongside the ciphertext.
- `telegram_ai_cli.db` — the shared SQLite connection (plans and rate-limit
  history in one file), opened in WAL mode with `BEGIN IMMEDIATE` available for
  every check-then-write sequence, so two processes can't both pass a check and
  only then discover they disagree.
- `telegram_ai_cli.plans` — the plan store and state machine
  (`pending → applying → applied | failed | unknown_outcome`, plus
  `rejected`/`expired`), with `plan_id` as 128 bits from a CSPRNG, a
  `max_pending` quota, TTL-based expiry, and the `pending → applying` claim
  implemented as a conditional `UPDATE` inside a `BEGIN IMMEDIATE`
  transaction so two processes cannot both apply the same plan.
- `telegram_ai_cli.opspec` — the operation registry: one `Operation` per
  capability, holding its CLI path, its MCP tool name(s), its Pydantic input
  model, and either a `handler` (reads) or a `planner` (writes) — never both.
  `Registry.check_invariants()` runs at import and asserts, as code rather
  than convention, that no MCP tool name contains "apply."
- `telegram_ai_cli.context` — `OperationContext`, the one object every
  operation receives instead of reaching for globals (the safety kernel, the
  plan store, the rate limiter, the audit log, the account fleet), tagged with
  which surface (`cli` or `mcp`) invoked it.
- `telegram_ai_cli.cli` — the Click command line, built by walking the
  operation registry rather than hand-writing a command per operation; `plan
  list`/`show`/`apply`/`reject`, `schema` and `mcp` exist today as the
  registry-independent commands, ahead of any read or write operation.
- `telegram_ai_cli.mcp_server` — the stdio MCP adapter: it validates every
  call with `Operation.parse` (the SDK publishes schemas but does not enforce
  them), and returns errors as tool content rather than protocol failures so
  an MCP caller sees the same envelope the CLI prints.
- `telegram_ai_cli.accounts` — session, proxy and device-fingerprint storage,
  ported (not copied) from `telegram-save-private-photo-video`'s
  `tgsave/accounts/` behind this project's own storage layer, with the
  hardening fixes the design called for: `api_hash` actually encrypted,
  a failed `chmod` treated as a fatal error rather than logged and ignored,
  `O_NOFOLLOW` on every read of a profile/session/tdata path, and the lock key
  computed from `st_dev`+`st_ino` rather than a string path.
- `Dockerfile` (multi-stage; the runtime image runs as a non-root user whose
  UID/GID default to the host's own, so bind-mounted account material stays
  owned by whoever runs the container), `Dockerfile.test`, `.dockerignore`,
  and a `Makefile` (`build`, `test`, `lint`, `fmt`, `shell`) so nothing has to
  be installed on a contributor's host.
- `scripts/smoke_mcp.py` — a real MCP handshake over stdio
  (`initialize` → `notifications/initialized` → `tools/list`) rather than a
  bare import check, because the server can import cleanly and still hang,
  never respond, or exit wrong.
- `README.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `TASKS.md`, `docs/threat-model.md`, `llms.txt`, `CITATION.cff`,
  `docs/seo-geo-checklist.md` and `.env.example`.
- `tg-ai account add` and `tg-ai account login` — the onboarding the README has
  always documented and three of the code's own error messages pointed at, now
  actually registered as operations (`telegram_ai_cli/ops/accounts.py`). `add`
  registers an account without touching the network, or adopts already-authorised
  material with `--tdata` / `--session-file`; `login` runs the interactive phone
  login that `accounts/login.py` had implemented all along, reusing whatever the
  account's row already knows (phone, proxy, application credentials) so that a
  re-registration cannot silently drop the proxy an account was signed in through.
- `Effect.LOCAL_ADMIN`, a fourth effect class for operations that administer this
  installation's own account inventory. `Registry.check_invariants()` refuses to
  publish one as an MCP tool, so the two account commands are terminal-only as a
  checked property rather than as an omission somebody could "fix" later: signing
  in prompts a person for the code Telegram sent to their phone, and enrolling an
  account widens the very fleet every allowlist is written against.
- **`telegram_search` can bring back the messages around each match** —
  `context: N` (`--context N`), capped at 5 either side. One matching line rarely
  says what a conversation was about, and the only way to find out was a second,
  paged read of the whole chat. The neighbours arrive in the same `messages`
  list, ordered newest first like history, each carrying `match: false` — a
  neighbour counted as a hit would inflate every number a caller derives from the
  result. `match` is now on *every* search row, with or without context, so a
  caller writes one parser. Bounded three ways: the radius is capped in the
  schema, only the first 10 matches are enriched (each costs two more history
  calls, and Telethon puts a flood wait at about ten in quick succession — a
  warning names how many matches were left plain), and overlapping windows
  collapse so a message next to two hits is returned once. `meta` reports
  `matches`, `context_messages` and `context_radius`, and `truncated` is computed
  from matches rather than rows — from the match total Telegram reports where it
  reports one, which also stops a complete last page of exactly `limit` matches
  from being called truncated. A global search **refuses** `context` instead of
  ignoring it: its hits can be in chats the caller never named, so context there
  would be a separate page per chat.
- `docs/operations.md` and `docs/configuration.md` — the per-operation reference
  (arguments, defaults, effect and the policy each one consults) and the full
  `tgai.yaml` / `TGAI_*` reference, including the part that cannot be configured
  at all.

### Changed

- **The README no longer claims this tool handles `noforwards`.** It did not, and
  nothing in the repository ever implemented it — the line described a capability
  that existed only in the documentation. What is true is now written down in
  both places: **downloading** protected media is not blocked here and never was
  (Telegram asks *clients* to prevent saving it, which the official apps honour
  and a raw MTProto library does not; Telethon saves the file like any other and
  no guard was added); **forwarding** is refused by Telegram itself with
  `CHAT_FORWARDS_RESTRICTED`, which no client bypasses; and posting the bytes as
  a fresh copy is `media fetch` plus `message send-file`, two plans approved
  separately, with deliberately no one-step feature packaging them. The failure
  is now legible rather than a raw Telethon class name:
  `ChatForwardsRestrictedError` is classified as a server refusal that had no
  effect — so the rate-limit slot goes back, as with every other "Telegram said
  no" — and is reported as `FORWARDS_RESTRICTED`, saying content protection is
  the reason and naming the source chat by its numeric id, never by its title.
- **A `t.me` link keeps its message number.** `chat` arguments used to resolve a
  link as a chat and drop everything else, turning "look at this message" into
  "look at this chat" silently. `chat read` now anchors its page at the message
  the link names (reported as `meta.anchor_message_id`), `media fetch` and
  `message reactions` take the message id from the link when it is not given
  explicitly, and a link plus a conflicting explicit id is refused rather than
  resolved by preference. A topic link reports `meta.topic_id` and warns that the
  page is not filtered to it; a search scoped by a message link warns that it
  covers the whole chat.
- `telegram_media_fetch`'s `message_id` is now optional — required unless the
  `chat` argument is a link that names the message.
- The MCP server's instructions describe the untrusted markers, so a client is
  told what the delimiters mean instead of inferring it.

### Fixed

- **One pinned action SHA was not a commit.** `release.yml` pinned
  `softprops/action-gh-release` to `fe965f7a…`, labelled `v3.0.2` and listed as
  verified. That object does exist — it is the **annotated tag object** for
  v3.0.2 — but `uses: owner/repo@<sha>` resolves commits, not tag objects, so
  every run would have died on "unable to resolve action". The commit v3.0.2
  points *at* is `3d0d9888cb7fd7b750713d6e236d1fcb99157228`. Every pin in both
  workflows was re-resolved through the GitHub API — `git/ref/tags/<tag>`, then
  `git/tags/<sha>` where the ref yields a tag object rather than a commit — and
  the other five were correct. Two lessons, both in the convention rather than
  the file: a `# vX.Y.Z` comment beside a SHA is a claim nothing checks, so it
  has to come out of the API rather than off a keyboard; and reading that API
  means dereferencing the tag, since the first answer for an annotated tag is
  the wrong kind of object and looks entirely plausible.
- **The outbox worked only for people whose umask was `022`.**
  `_require_private_root` refused any `paths.uploads` with a group *or* other
  write bit — and `umask 002`, which is the **default** wherever *user private
  groups* are in use (Ubuntu out of the box, Debian and the RHEL family through
  `USERGROUPS_ENAB`, each user getting a single-member group), makes every
  directory you create `0775`. So `message send-file` failed with `INSECURE_PERMISSIONS`
  on a directory the user had just made for it, over a "group" that had nobody
  else in it. CI never saw this because GitHub runners use `umask 022`; the
  local suite showed it as 43 failures in `tests/test_outbox.py` and
  `tests/test_send_file.py` that appeared only on a developer's own machine.
  The two write bits are now treated as the different problems they are:
  world-writable is still **refused** (no default umask produces it, so it is a
  deliberate `chmod` and not this tool's to overrule), while group-writable is
  **repaired** with `chmod g-w` and then used — narrow-or-refuse, exactly as the
  download root and the archive already handle their directories, with a failed
  `chmod` still fatal. Only the write bit moves; read and execute are left as
  found. Judging the group instead ("is anybody else in it?") was rejected as
  unanswerable: `grp.getgrgid().gr_mem` lists only *supplementary* members, so a
  group whose members all hold it as their primary gid reads back empty, and a
  check that cannot tell safe from unsafe must not claim it can.
  The mode is **read back** after the `chmod` rather than inferred from the call
  returning: on a mount whose permissions are fixed by its mount options — many
  FUSE and SMB mounts, anything with `mode=` or `dmask=` — `chmod` succeeds and
  changes nothing, and taking success as proof would have made this the worst
  version of the check, strict-looking and open (raised by review).
- A promotion that granted `manage_topics` now actually grants it. The right was
  accepted by `AdminRights`, printed in the plan summary and then dropped when
  the applier built `ChatAdminRights` — so the plan a person approved and the
  request Telegram received disagreed about it, silently and in the direction
  that looks like it worked (`apply.py`, found while adding the demotion that
  reverses the same call).
- `TGAI_`-prefixed environment variables actually override the YAML file, as the
  README, `.env.example` and the new configuration reference all say they do.
  `load_settings` passes the file in as init keyword arguments, and
  pydantic-settings ranks those *above* the environment by default — so
  `TGAI_PROFILE=readonly` could not take away a `profile: plan` written on disk.
  `Settings.settings_customise_sources` now puts the environment first, and
  `tests/test_config.py` covers both directions plus the merge (one override
  must not empty the allowlists it does not name).
- Registering an account holds that account's session lock
  (`AccountRegistry.register_phone_login`), like every other change that can
  replace a row. Writing over a registration while a client is connected
  underneath it corrupts the session file that client is using.
- Error suggestions name a command that exists and can be typed as written:
  `tg-ai account login --label <name>`, matching the option the generated CLI
  actually takes (`ops/_client.py`, `accounts/registry.py`). The positional form
  they used before would have failed with "unexpected extra argument" even once
  the command existed.
- README command names now match the CLI: `tg-ai chats` (not `chat list`),
  `tg-ai fleet` (not `fleet status`), `chat promote` (not `admin promote`); and
  the MCP tool table no longer lists `telegram_plan_status` and
  `telegram_plan_list`, which the registry does not publish — `tg-ai plan list`
  and `plan show` are terminal commands, on the same side of the line as
  `plan apply`.
- The MCP adapter is built against the installed SDK's constructor-handler API
  (`Server(..., on_list_tools=..., on_call_tool=...)`) rather than the 1.x
  decorator form, which the low-level `Server` no longer provides. Nothing
  imported wrongly, so the whole suite passed while any attempt to actually
  serve MCP died with `AttributeError`; `tests/test_mcp_server.py` now builds a
  server and exercises both handlers, so an SDK API change fails in the unit
  suite rather than only in the stdio smoke test.
- The runtime image builds the real package in a directory the dependency-only
  placeholder never touched. Sharing one left setuptools' `build/lib/` holding
  an empty `__init__.py` stamped at build time, and `build_py` copies a source
  file only when it is newer than its destination — so the real `__init__.py`
  lost that comparison and never entered the wheel. The image built and ran as
  non-root, then failed on first import with a missing `__version__`.

### Security

- `777000` (Telegram Service Notifications, where login codes and 2FA resets
  arrive) and Saved Messages are excluded as constants in `config.py`, checked
  before any allow/deny list in `safety.py` — no configuration value can reopen
  either.
- Every write list (`send`, `admin`, `join`) and the direct-message read list
  are fail-closed by default: an empty `allow` means nothing is permitted for
  that capability, not everything. Reading groups and channels is the one
  intentional exception, open by default so the tool is usable before it has
  been configured.
- Rate limits persist across a process restart by design — an in-memory counter
  would let anything able to restart the process (including whatever talked an
  agent into sending in the first place) lift the ceiling for free.
- The `.gitignore` inherited from the standard GitHub Python template did not
  cover `sessions/`, `accounts/`, `tdata*/`, `*.session*`, `*.api.json` or
  `*.string` — a real gap for a project that holds live MTProto auth keys. These
  patterns, plus the local state directory, the audit log and `secret.key`, are
  now excluded explicitly — and anchored to the repository root with a leading
  slash, so that an unanchored `accounts/` cannot also match the
  `telegram_ai_cli/accounts/` source package (which had silently excluded the
  whole module from both git and the linter).
- `plans.encrypt_bodies` fails closed: constructing a `PlanStore` with body
  encryption enabled but no key available is refused, rather than quietly
  writing the plan body to the database in plaintext.
- Redaction is applied to email addresses of any domain depth, before the
  card and phone rules — an address is masked whole rather than having a
  numeric local part rewritten as a phone number inside an intact domain.
