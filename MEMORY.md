# agents-matrix-connector memory

Lessons that outlive a session. Newest first.

## 2026-11-16 decisions

- Orphan approval answers (S-63 retro F2, 2026-11-16): `_answer_pending` finishes an answer whose
  target resolves to no decision ids (results `[]`) and replies "Nothing to answer here." instead
  of posting an empty batch (the taskboard rejects `items` with min_length=1, so it would retry
  forever). It never calls the taskboard for such an answer.
- Withheld attachment guard (T-420 final review): sync stores body and media_url of `not_allowed`
  rows, so "withheld" is a read-time property. `api.history.withheld_kind(reason)` is the one
  predicate; POST /chat/v1/attachments answers 409 `no_attachment` (same body as a row without
  file) and the fetcher refuses with `no_attachment` for any withheld row, so a denied sender's
  file is neither revealed nor delivered. Withheld rows also null `from` for all four kinds, as
  the spec says (the code had nulled it for denied only).
  The `from` filter (name or digest) never matches a withheld row of any kind (`db.history.page`
  takes the reasons from `withheld_kind`'s table), else filtering by a name would confirm the sender.

## 2026-11-15 decisions

Chat history and attachment fetch (T-420, release v0.16.0). Why each choice:

- Ordering: `s` (agent sends) and `o` (worker replies) rows sort by `created_at`, not by their
  delivery time, so a page cursor stays put while a send is delivered later; `m` rows sort by
  `origin_ts`.
- Ids: `o<n>` names a worker reply or notice that was sent without an agent send. They have no
  `sends` row, so they need their own prefix; `a<n>` names attachment requests.
- Crawl start token: never the live head. A crawl started from the head could turn a live event
  into a history row that is never dispatched; it starts just before the oldest stored sync row,
  else at the committed sync `next_batch`.
- Pacing: one page of at most 100 events per bot tick is the whole rate limit, so a crawl of many
  rooms never competes with sync, sends or dispatch for the homeserver.
- Edits for crawled rows: a page carries the edits that Synapse bundles in `unsigned`
  (`m.relations.m.replace`), so a crawled row is stored with its latest text and `edited` set
  without a second request per message.
- Redaction of a still-`new` row: bot sync moves it to `ignored` (reason `redacted`), so a message
  withdrawn before dispatch is never sent to an agent. The row is blanked, never deleted.
- Migration 0011 resets `sync_state.filter_id`: the bot then creates a new filter with
  `m.room.redaction`. This also makes the earlier `m.sticker` filter change effective, which
  stored filters had kept out.
- `not_allowed` counts as denied in history: the sender never passed the allow rules, so text,
  attachment and name are withheld (`withheld: denied`) like `denied:<kind>` rows.
- Anonymization applies to the admin view too: a superleader reading any room sees the pseudonym
  and no attachment name, so the admin route cannot be used to de-anonymize a room.
- The admin history view replaces the UI feed only on the admin router: the UI keeps its own
  `GET /matrix/api/v1/rooms/{room_id}/messages` shape, and `_ADMIN_ROUTES` filters by path and
  method, so the UI handler is registered only when `admin=False`.
- Reactions are not shown in the feed and edits and redactions are folded only for rows stored
  from this release on (no rewrite of older rows): both keep the migration forward-only.

## 2026-11-12 decisions

- Inbound media (T-155 step 2): the mention window is applied only by bot sync, on rows it
  inserted. An accepted mention promotes the same sender's media rows that are
  `ignored/not_mentioned` within `MATRIX_MEDIA_MENTION_WINDOW_SECONDS` around it, newest first,
  at most the room's remaining queue cap (`max_queued_per_room - count_new`), status-guarded
  (`status='ignored' AND reason='not_mentioned'` in the UPDATE itself); media after a mention is
  accepted when an accepted mention (`mentioned AND status <> 'ignored'`) of that sender is
  within the window before it. Why: Messenger sends each photo as its own event and only the
  text mentions the bot; a guarded update keeps one writer per transition, and the cap keeps a
  photo burst from bypassing `queue_full`.
- Video and audio are never downloaded (`unsupported_type` without a request): no orchestrator
  allowlist entry would accept them, so a download would only cost bandwidth.
- gif and webp are delivered in their own format, first frame (gate Q2 answer, overriding the
  plan's PNG/JPEG); this needs the orchestrator release that allows image/gif, image/webp and
  application/pdf deployed first, else the whole batch fails 422.
- Attachment names are always generated, `m<row id>.<ext>` from the sniffed type; the original
  file name is only an escaped prompt attribute and is never stored in `anonymize_names` rooms
  (sync drops it before insert), nor shown in `MessageView`.
- The worker records `media_status`/`media_reason` through `inbound.set_media_outcome`, guarded
  on `status='in-progress' AND run_id=<run>`, so a run that lost its rows cannot overwrite a
  later run's outcome. Media is attached again on a `_resume` retry (bytes are never stored).
- `m.sticker` joined the sync filter, the backfill filter and the message type check (F3):
  without it stickers never reach `parse_message`.

- Outbound agent files (T-155 step 3): the api downloads each file through the orchestrator
  at request time (gate Q4), outside any pool connection: the room checks, flag, names, path
  rules and a rate/room-bytes pre-check run on one connection, which is released before the
  downloads, and the locked transaction re-checks duplicate, rate, chunks and room bytes on a
  second one. Why: a download can take seconds for a 25 MB file and must not starve the pool
  (2026-10-28 one-connection rule). The pre-check runs only for sends with files, so a text
  duplicate is still answered at the rate limit.
- A file counts as one chunk toward `MATRIX_AGENT_ROOM_CHUNKS_PER_HOUR`, plus a new per-room
  `MATRIX_AGENT_ROOM_FILE_BYTES_PER_HOUR` (refused sends do not count). Chunks bound Matrix
  events, bytes bound bandwidth and Postgres growth from a looping agent.
- Bytes live in `send_files.data` until the send settles; the sender's sweep (`clear_settled`,
  every tick) nulls them for sent, failed and refused sends, so a crash between settle and clear
  still ends with no bytes.
- Upload once: the tracker stores `content_uri` right after the upload and sends the event with
  the deterministic `mxa-<send>-f<position>` txn id, so a crash after the upload reuses the uri
  and Synapse's txn dedup still holds.
- No captions: text chunks go first as `m.text`, then one event per file; the quoted reply
  relation is on the very first row (a file when there is no text). The bridge drops captions on
  audio and marks them unreliable.
- `agents_may_send_files` (gate Q3, default off) is checked by the api (403 files_not_allowed,
  before any download) and re-checked at the bot's claim (refused `files_not_allowed`); it is
  not re-checked per event at send time (room policy still is).
- `X-Orch-Agent` is read only by `POST /chat/v1/messages` (a route header, not the shared
  `caller` dependency), so the OpenAPI document shows it only where it matters; it is needed
  only with `files` (422 files_unavailable when missing or not a uuid).
- `text` keeps `min_length=1` (an empty string stays 422 invalid_request); a whitespace text
  with files is stored as NULL; neither text nor files is 422 invalid_text, checked in the
  handler (a pydantic model validator would answer invalid_request).
- File URLs never reach logs: httpx logs every request URL at INFO, and the download URL holds
  the workdir path, the upload URL the file name; `media.quiet_file_urls()` (installed by the
  agent app and the tracker) drops those records from the `httpx` and matrix loggers.
- Residual risk: a secret written into a workdir file can be sent, as it can be pasted into
  `text`. The flag (default off) is the control; the name deny list only catches accidents and
  the wrapper's workdir confinement is the hard boundary.
- Read receipt needs a wrapper session (T-167, finding I2 of T-138): `Session.is_up` in
  agents-common v0.16.0 now also requires `summary.ready_with_session > 0`, because a ready pod
  says nothing about a wrapper session and the connector said "seen" while no agent could take
  the message. `worker._delivered` is unchanged; a handoff into a ready pod without a wrapper
  session sends no receipt and the tracker sends it once the message is `sent`. Deploy order:
  agents-orchestrator with `ready_with_session` first, then this connector. Connector first is
  safe but late: `is_up` is always false, so receipts come only from the tracker.
- Attachments foundation (T-155): Pillow and pillow-heif are added because inbound photos must
  be re-encoded (EXIF stripped and orientation applied, long edge 2576, JPEG q85 stepping
  down) and iPhone HEIC must be readable; the agent's Read tool shows only jpeg/png/gif/webp.
- gif and webp are kept native (gate answer Q2: first frame, re-encoded in the same format,
  falling back to JPEG/PNG only when still over the cap); this overrides the conversion text in
  plan.md 4.2.
- Outbound caps are decimal bytes (8000000 per image, 25000000 per other file) so they stay
  under the bridge's "8 MB / 25 MB" whichever unit it uses.
- The pixel cap (`w * h`, default 40 million) is checked from the image header before any
  `load()`, and `Image.MAX_IMAGE_PIXELS` is set to it, so a tiny file declaring a huge canvas
  never allocates. Sniffed bytes decide type; sender mime, filename and extension never do.
- `outbound_mime` keeps a small extension table (docx, xlsx, pptx, zip, csv, md) ahead of
  `mimetypes`, whose result depends on the host's mime.types; an extension only ever yields a
  non-media mime.
- Animated WebP with metadata is sent unchanged by `strip_outbound_metadata` (re-encoding
  would drop the frames); GIF is never re-encoded outbound.

- Display-name body fallback removed (T-138, Q1 A): the bot ghost and the owner share the
  display name "Szymon Richert", so an owner tag woke the agent; mautrix-meta always sends
  `m.mentions` and pills, and Matrix spec v1.17 (MSC4210) dropped body matching. Mentions are
  now only the bot's id in `m.mentions.user_ids`, a matrix.to pill, or a room alias.
- A reply to the bot counts as a mention because bridgev2 puts the replied-to sender in
  `m.mentions.user_ids` (Q2); no reply parsing is added.
- `room_settings.agent_aliases` per room because Messenger nicknames are not bridged
  (`ChatMember.Nickname` unused). Whole word, casefold, Unicode `\w` boundary, optional leading
  `@`, max 10 aliases x 64 chars; none keeps and `[]` clears, like `anonymize_names`. Aliases
  never grant access, only wake an already allowed room.
- Read receipt once the hive has the message (T-138, Q5 B), replacing the 2026-10-26 "on
  handoff" rule: the 202 at handoff only means queued, and live run 636 showed "seen" ~95 s
  before the answer while the pod was still starting. At handoff the receipt is sent only when
  `get_session(...).is_up` (active, start finished, a ready agent); else the tracker sends it
  when the polled message is `sent` or has a turn. `sent` counts as received because the
  connector posts on the regular lane, which types into a busy agent mid-turn, and both
  orchestrator pick queries need a ready agent with a wrapper session. Never while the hive is
  starting, never waiting for the reply, never for a message that failed or was cancelled
  without a turn. `is_up` at handoff may mark read during a slash-command 409 requeue,
  accepted by the user. Once per run through an in-memory set in `receipts.ReadReceipts`
  shared by worker and tracker, marked before the send (a failed send is logged once, not
  retried) and forgotten when the tracker closes the run; a restart may resend one receipt per
  in-flight run, which the bridge ignores (not after LastRead). Typing is unchanged (Q6 No).
  agents-common v0.7.0 -> v0.14.0 for `Session.summary`/`is_up` (v0.8-v0.13 add nothing that
  breaks amc: project adoption only applies with `project=`, which amc never passes).

## 2026-11-11 decisions

- Per-room notes and skills (T-124): tables `room_notes` and `room_skills`, single writer api;
  the worker only reads them to build first prompts. Why: a room's context must outlive its
  sessions and bindings (a session is replaced, the room stays), and one writer keeps the
  one-writer-per-table rule.
- The room is never a request parameter: every notes and skills route resolves it from the
  caller session's active binding (`bindings.active_for_session`), so a session cannot reach
  another room's data and a rebound room keeps its notes (no FK to `rooms` either: notes
  outlive a pruned room row).
- Optimistic version, 0 = create; a stale version is 409 `version_conflict` and the body is
  unchanged. Two writers on one room (a retried PUT, two sessions over time) cannot silently
  overwrite each other; the loser re-reads and merges.
- Caps are our own connector caps counted in code points (`len(str)`), not a wrapper or hook
  limit: notes 8000, skill body 8000, 20 skills per room, description 1024 (below Claude Code's
  1,536 skill-listing truncation). The skill count cap is checked under a per-room advisory lock
  so concurrent creates cannot pass it.
- Bodies and descriptions are never logged or echoed in errors (log room id, name, version and
  char count only): they are readable data, but a room's notes can hold private chat content.
- One rule constant, `prompt.MEMORY_RULE`, is injected with the notes and the skill index into
  every first prompt (after the trust rule) and returned by the notes GET, so scheduled
  sessions that never get a chat batch first can read it there; `MATRIX_ROOM_MEMORY_RULE`
  overrides it. No preset carries the rule, so changing it needs no preset edit. Injected notes
  and skill names/descriptions are escaped like chat bodies and framed as facts, not
  instructions; the rule is the only imperative text.
- Skills materialize as `$CLAUDE_CONFIG_DIR/skills/<name>/SKILL.md` written by the agent, never
  in the working directory: the workdir is cached per preset and shared across rooms, so a skill
  there would leak into other rooms.

## 2026-11-07 decisions

- 409 `room_bound` names the occupier (`error.occupier`, the bound session id) so the
  orchestrator can relay a cron session's post through it. Only the orchestrator reads it (it is
  the sole `/chat/v1` caller) and it never passes it to an agent. Only for a room flagged
  `agents_may_initiate` with a session bound (the caller could have started a chat there); never
  for a reservation (no session yet) or an unflagged room. This amends the 2026-10-28
  anti-probing rule for exactly this case; reply-masking (`bad_reference`) still wins first.

## 2026-11-04 decisions

- Name anonymization (T-43, user corrections m345 and m346): a per-room `anonymize_names` flag
  (`room_settings`, migration 0006, default off), settable from the UI and by superleaders
  through the admin settings route. Pseudonym `user-<sha>` / `admin-<sha>` is the first 12 hex
  of a plain sha256 of the mxid, no HMAC and no secret (the user asked for exactly that);
  `admin-` for ids in `MATRIX_ANONYMIZE_ADMINS` (mxids or numeric Facebook ids, never display
  names, so nobody renames into the label). Only identity fields are pseudonymised (prompt
  `from`, empty `facebook_id`, room name, preamble `sender`/`room_name`, `/chat/v1/rooms`
  names); bodies are never rewritten and there is no real-name check: the user wants real names
  usable and only the pseudonyms kept out of the chat. A pseudonym in outbound text is
  rejected, never redacted: the API answers 422 `pseudonym_disclosed` naming the tokens; the
  agent send route enforces it (the tracker no longer posts final text, see 2026-11-17).
  Matching folds NFKC/casefold and drops invisible characters so width or zero-width
  tricks do not slip a token past. Admin relays and the UI keep real names.
- Room ids: the `room` rule format accepts `!opaque:server` and the server-less `!opaque` of
  Matrix room version 12 (`!` + 43-char url-safe base64 of the create event hash). Why: the
  allow route returned 500 for a v12 room because validation required `:server`.
- matrix-integration (T-25): superleader agents allow rooms and open chats. Decisions are the
  user's answers in the workbench `docs/plans/matrix-integration/plan.md` (Q1-Q6).
- Invites are approved, not auto-joined (Q3). `SyncLoop._join_invites` joins an invite only when
  the room has an `allow/room` rule, the bot opened it itself (an `open` room request in
  `working`/`done` with that `room_id`, e.g. the portal `create_dm` returned) or it is the
  bridge management room (`auto_join`, read once from the provisioning `whoami` at start; a
  failure logs a warning and the bot starts without it). Why: the user wants to approve
  invitations, and joining a stranger's portal already marks the message request seen on the
  bridge side. A sender rule cannot approve an invite (the inviter is the bridge bot), so a new
  portal of an allowed person waits for an allow call. The e2e `Stack.portal` helper approves
  its portals as bot-opened (an inserted `open`/`done` row) so they join without being allowed.
- `room_requests` is a queue with two transitioners: the api inserts rows and moves
  `pending_confirmation` to `confirmed`/`rejected`; the bot moves `resolving` to
  `pending_confirmation`/`candidates`/`failed` and `confirmed` to `working` to `done`/`failed`.
  Why not the bot alone: confirmation is the user's answer, which only the orchestrator can
  read (taskboard), and the bot has no orchestrator-side identity for that. The confirm route is
  internal (`/chat/v1/internal`, never relayed) and only accepts the decision id the
  orchestrator attached, so an agent cannot confirm its own request, even with a decision it
  asked itself. The rows double as the audit trail and are never pruned (low volume, capped by
  `MATRIX_ROOM_REQUESTS_PER_HOUR`).
- The allow route (`POST /rooms/{room_id}/allow`) writes settings, the `allow/room` rule and an
  `accept` row (status `confirmed` directly: the allow call itself is the approval) in one
  transaction under a per-room advisory lock. The bot sends
  `com.beeper.accept_message_request` once the room is joined; an accept for a room that is not
  joined yet goes back to `confirmed` each tick. Allowing a room the bot opened stays a separate
  allow call, so `room_settings` and `access_rules` keep the api as their only writer.
- Provisioning (Q6): mautrix-meta serves `/_matrix/provision` on its appservice port 29328 (not
  29319); the bot calls it with Hermes' own Matrix token and `?user_id=` (relies on
  `provisioning.allow_matrix_auth`, upstream default true). Empty
  `MATRIX_BRIDGE_PROVISIONING_URL` fails Facebook targets with `bridge_unconfigured`. The bridge
  creates portal rooms itself, so Facebook opens never createRoom+invite a ghost.
- A Matrix open that created its room but failed to update `m.direct` is still `done`: a retry
  would create a second room, and only the DM marker is lost. Rows a stopped bot left
  `working` are requeued on the first tick (the bot is a singleton, so nothing else holds them).
- Vanity `facebook.com/<username>` links answer 422 `vanity_link_unresolvable` (the bridge
  only takes numeric ids); names resolve locally (exact, case-insensitive display name) first,
  then through the bridge `search_users` (exact name matches preferred, else all results). Not
  exactly one match is `candidates`/`failed`, never an open.

## 2026-11-01 decisions

- Usage quota holds (epic claude-usage-monitor E4, agents-common v0.5.0 to v0.7.0; v0.6.0 only
  added `service_gate`, nothing to adapt here). The orchestrator now refuses a new session with
  409 `usage_quota` when the Claude plan usage is at its limit. The worker catches
  `UsageQuotaRefused` (before `OrchestratorError`) in `_dispatch` only: a follow run posts into an
  existing session and never creates one. In one transaction the run fails `usage_quota`, its rows
  return to `new`, the reservation is released (`run_failed`) and `room_holds` gets
  `until = exc.retry_at(now)` (reset plus 60 s, at least a minute away, 300 s without a reset).
  Why a hold and not just requeue: without it the room would be retried every tick, hitting the
  orchestrator and the usage gate several times a second for every held chat. Why per room and not
  global: the connector does not know the gate state, and the next room's first run is refused
  once and held the same way. `rooms_ready(conn, now)` skips a room whose hold `until > now`
  (the hold ends exactly at `until`); a second refusal upserts a later `until`. `room_holds` has
  one writer (the worker), one row per room, and is never pruned (a stale row is inert and the
  count is bounded by the number of rooms).
- `_fail_and_requeue` is the one place that fails a run and returns its rows to `new`, shared by
  `_session_gone` and `_quota_hold`; the two differ only in how the binding is released (the
  session's binding vs the first run's reservation). Dispatch of an unposted run through `_resume`
  can be refused too and is held the same way.

## 2026-10-31 decisions

- Superleader admin surface (M1): the connector's token-gated agent listener (`MATRIX_AGENT_PORT`,
  `AgentGate`) now also serves the five routes the orchestrator relay (O9) needs, under
  `/chat/v1/admin`, reusing the UI handlers (`build_admin_router` filters the rooms and rules
  routers): `GET /chat/v1/admin/rooms`, `PUT /chat/v1/admin/rooms/{room_id}/settings`,
  `GET /chat/v1/admin/rules`, `POST /chat/v1/admin/rules`, `DELETE /chat/v1/admin/rules/{rule_id}`.
  Bodies and errors equal the UI routes (`/matrix/api/v1`). No `X-Orch-Session` needed. Before
  this the UI routes sat only behind the CIDR allowlist, unreachable with the bearer. Rule PATCH,
  settings, users and room detail are deliberately not exposed. `create_agent_app` now takes the
  orchestrator client (the room settings route validates `preset_id`).

- The hard-coded preamble is now the Jinja template `DEFAULT_PREAMBLE` (user decision). Lookup
  order: the allowing rule's `access_rules.preamble`, then `settings.preamble`, then the default.
  Variables: `room_name`, `room_id`, `sender`, `rule_name` (the rule's note), `now`. Why a
  nullable column per level: NULL means "inherit", so clearing an override is one update.
- `inbound.rule_id` (written by sync, no foreign key) records the deciding rule, because the
  worker renders later and re-evaluating the policy there would need the sync loop's room
  context. A deleted rule simply stops contributing its preamble.
- A stored template that fails to render at send time falls back to the default with a warning
  instead of failing the run: a bad edit must not stall a chat. Writes are validated (syntax
  and variable names, 422 `invalid_template`), so this only guards drift.
- agents-common `render` runs in a child process, so the worker calls it through
  `asyncio.to_thread`. `PUT /settings` now keeps a field that is left out (it used to require
  `default_preset_id`).

## 2026-10-28 decisions

- Release job (2026-10-28, epic semver-image-tags-at-the-end D1-D8): every green main build ends
  with `release` (needs every other job, `contents: write`, concurrency group `release`, full
  history and tags). Why: Harbor retention pruned the deployed sha7 tags within hours (incident
  2026-10-28); semver tags survive it. `scripts/next_version.py` and `tests/test_next_version.py`
  are byte-identical copies of agents-orchestrator's: never edit them here, change them there and
  copy. Rules: `!` or a BREAKING CHANGE footer is major (also on 0.x), `feat` minor, else patch,
  over every commit since the highest reachable `vX.Y.Z` (not first-parent); first release exactly
  0.1.0; nothing printed when HEAD is already contained in a release tag, because concurrency
  serializes release jobs but not their order. Retag with `regctl image copy` (manifest only)
  first, git tag (lightweight, checkout token, never `secrets.GITHUB_TOKEN`, so the secret set
  stays the two Harbor secrets) last, so a failed run leaves no tag and a re-run repeats the
  idempotent copies. This repo never had a `v*` trigger or `chart-publish-tag`;
  `test_no_tag_trigger_or_tag_publish_job` pins that.
- One pool connection per request, binding kept after a later delivery (2026-10-29, epic
  matrix-connector-agent-initi, epic review Consider 1 and 2):
  - A `/chat/v1` request holds at most one pool connection at a time. `queue` and `list_rooms`
    used to read the room through `directory.get_room(pool)` while the route held a connection,
    so as many simultaneous requests as the pool size (10, shared with the UI api) each held
    one and waited for a second until the pool timeout, then all answered 503. The routes now
    release their connection before `queue` runs, `queue` reads the room first and then takes
    its own connection, and `list_rooms` reads the directory before taking one. The room read
    and the checks are no longer one snapshot, which is fine: the room checks are advisory and
    the bot's claim re-checks them. Do not call a pool-taking helper inside `pool.connection()`.
  - `sender.release_initiation` replaces the plain `bindings.release_for_send` for a send that
    ended `failed` or `refused` with no sent row: it keeps the binding when a later send
    (higher id) of the same session reached the same room (an outbound row `sent`). Why: with
    m1 and m2 queued before the claim, m1 binds the room and m2 is "already"; if m1 failed and m2
    landed, releasing the binding made the user's reply to m2 start a new `mx-` session instead
    of reaching the agent that wrote to them. A later send that has not been delivered yet does
    not count, so a binding with nothing delivered is still released.

- Agent outbound limits (2026-10-28, epic matrix-connector-agent-initi, security review MEDIUM 2
  and LOW 2/3; outbound messages reach a real Messenger user, so abuse limits must hold):
  - The duplicate lookup, the per-session rate count (`MATRIX_AGENT_RATE_PER_MINUTE`, 20 per 60 s)
    and the new per-room cap now run in one transaction under `pg_advisory_xact_lock` on the
    session id and then on the room id (always in that order, so no lock cycle), before the
    insert. Why: counting and inserting in separate statements let parallel requests exceed the
    limit. The room-check (b) stays advisory; the bot's claim re-checks it.
  - New `MATRIX_AGENT_ROOM_CHUNKS_PER_HOUR`, default 60 chunks per hour (>= 1): at most that many per room
    in the last hour, summed over all sessions. A message counts `len(chunks.split(text,
    MATRIX_REPLY_CHUNK_CHARS))` (one 16000-character send is 4 chunks at 4000), a reaction 1,
    `refused` sends 0. Why chunks and not sends: the rate limit alone let a session push about 80
    Messenger messages a minute (20 sends of 4 chunks). 60 per hour is one a minute on average,
    conservative for a person's DM. Refusal is 429 `room_chunk_limit`, distinct from
    `rate_limited`; a send that would pass the cap is refused whole.
  - `X-Orch-Slug` is validated with the orchestrator's session slug rules (DNS-1123 label, at
    most 40 characters, not `orchestrator` or `orchestrator-*`) and answers 400 `invalid_slug`;
    absent or empty stays NULL. Why: it was stored unvalidated and unbounded. The session
    check (401) still comes first.
  - A reply or reaction to an `m<n>` whose room check fails now answers 422 `bad_reference`, the
    same as an unknown id, unless that room is the session's own bound room. Why: 409
    `room_bound` for a room bound elsewhere versus 422 for an unknown id let a session probe
    which ids exist in other rooms. In its own room the specific refusal (for example 403
    `room_not_allowed` after the room turned denied) is kept, because the session already knows
    that room. Replies to `s<n>` keep their codes: those are the session's own sends.

- A whitespace-only message text is refused at the api (2026-10-28, epic
  matrix-connector-agent-initi, found by mxc-bot-sends-delivery): `POST /chat/v1/messages`
  answers 422 `invalid_text` and queues nothing when the text has no non-whitespace character
  (`str.strip()` is empty, the same test `chunks.split` uses to return no chunk). Why: the
  sender fails such a row as `empty_text`, so the agent would get 202 for a message that can
  never be delivered. The sender keeps its `empty_text` failure as a defence for rows queued
  before this change. An empty text stays the pydantic length check (422 `invalid_request`).

- One session per room and one room per session (2026-10-27, epic matrix-connector-agent-initi):
  hard 1:1 binding in `room_bindings`, released only when the session is deleted; it amends the
  2026-10-25 "One agent session per room". Why: the user's final-gate decision (2026-10-27,
  "hard 1:1 relationship between chat and session"), which replaced last-speaker routing: an
  answer to an agent-initiated or follow-up message must reach the session that asked, and a
  room shared by several sessions would let a reply land in the wrong one. The binding decides
  before any preset check, so a bound session takes the batch without a preset.
- A refused session check (an orchestrator error other than not found) keeps the binding and
  the rows `new`. A hard binding never moves on an ambiguous answer; only not found or desired
  `deleting` releases it.
- The binding sweep runs at most once per 60 s, the first one 60 s after the worker starts, and
  releases only when it checked every bound session (an unavailable orchestrator ends it with
  nothing released). A room's next batch releases at once anyway, so the sweep only bounds the
  delay for quiet rooms, and starting one interval late keeps a restart from re-checking every
  session at once.
- A follow run without a preset on its session or room stores NULL `runs.preset_id` (CHECK keeps
  it NOT NULL for first runs); `deliver` never reads it, so an explicit "no preset" beats a
  sentinel uuid.
- A follow run into a session with no other run in the room gets the PREAMBLE, decided in
  `Worker._deliver` so the `_resume` retry keeps it: such a session (one that initiated the chat)
  never saw the untrusted-input framing.

- Migration 0002 backfill: a binding's slug comes from the session's first run (NULL if that
  run was pruned), never from a follow run. The first run is the one that created the
  session, so it is the only run whose slug names the binding's origin; a follow run may
  carry another agent's slug and would mislabel it.
- `/chat/v1` routes accept a missing `X-Orch-Slug` and store it as NULL (`sends.slug` is
  nullable). The session id is the identity of the caller, so the slug is informational and
  rejecting its absence would only break callers that have none.
- `/chat/v1/rooms` sorts names by code point with unnamed rooms last. The order is
  deterministic, so an agent that lists rooms twice sees the same list.
- Unknown references, another session's sends and another room's ids share one
  `bad_reference` message. Distinct messages would let an agent probe which ids exist for
  other sessions.
- Agent room policy tie-breaks (design D4 was silent): several denied members resolve to
  the lowest rule id; several allowed members resolve rule-backed allow before default
  allow, then lowest rule id. Both are deterministic and make a stable, explainable
  decision (an explicit rule outranks the default).

## 2026-10-26 decisions

- Read receipts (user decision 2026-10-26, "On handoff"): the bot sends one public `m.read`
  receipt for the newest event of a batch in `Worker._delivered`, next to the typing
  indicator, so "seen" means the agent has the message. Best-effort like typing, and only for
  handed-off rows, so a denied stranger never sees "seen" from the bot's Facebook account.
  Amended 2026-11-12 (T-138): sent when the hive has the message, not at handoff.

## 2026-10-25 decisions

- Bot user with an access token, not an appservice: an appservice needs a registration
  file in Synapse's app_service_config_files plus a Synapse restart in namespace messaging,
  and an appservice user would still have to be logged in to mautrix-meta to get Facebook
  portals, so it buys nothing. A bot user needs only a token.
- Reuse Hermes' Matrix user `@hermes:szymonrichert.pl` and its Facebook account with Hermes
  scaled to 0 (user decision 2026-10-25). The one allowed chat is Hermes' owner DM and home
  channel `!iLWrAwaTMYnINJsyNv:szymonrichert.pl`, which exists only for that Facebook login,
  so a new account would not reach it. Hermes must stay at 0 while the connector runs.
- No E2EE: Hermes runs with E2EE off, the bridge rooms are unencrypted, and the connector
  ignores encrypted rooms (`room_encrypted`).
- Rules are identifier-only with default deny. The webhook service's RE2 rule engine was not
  reused: matching free text such as display names would let a user rename into access.
- One agent session per room, reused while it is running; a deleted session is replaced by a
  new one on the next message (`session_gone`). Amended 2026-10-27 (see 2026-10-28 decisions):
  one session per room and one room per session (hard 1:1 binding in `room_bindings`), released
  only when the session is deleted.
- 2026-11-04: Admins and the owner are absolutely trusted (owner: "admin is admin and I'm the
  owner"). The connector sets `role="admin"` on `<message>` for senders in
  `MATRIX_ANONYMIZE_ADMINS` and the first prompt always carries `ADMIN_TRUST_RULE` after the
  preamble (not part of the editable template, so custom preambles cannot drop it). Identity is
  only connector-set attributes; bodies are escaped, so claims in content never grant trust.
- 2026-11-12: forwarded chat batches are posted `from_user=True` (T-137): `Worker._dispatch` and `Worker._deliver` pass it to agents-common, so the orchestrator stamps `last_user_message_at` and the web pins the superleader the user last talked to across Matrix and web. Every sender the room rules allow counts as the user (the rules already decide who may reach an agent). Tracker notes (`tracker._tell`, failure and rejection text) are system text and stay unflagged. Needs agents-orchestrator 7.16.0 deployed first (PostMessage forbids unknown fields) and agents-common v0.15.0.

- 2026-11-15 (T-420 chat-history, migration 0011): inbound gains reply_to_event_id, source
  (sync|history), edited_at, redacted; attachment_requests and history_crawls queues added;
  sync_state.filter_id is cleared so the bot creates a new filter (with m.room.redaction, Task
  2). Edits and redactions fold into the original row (no rewrite of rows stored earlier);
  redaction blanks text and media and ignores a still-queued row (reason redacted). History
  rows are always status ignored (reason history), so they never dispatch. The REDACTED and
  HISTORY reason constants live in db/inbound.py, the table owner.
- 2026-11-15 (T-420 Task 2): the sync loop verifies a stored filter id against SYNC_FILTER
  (GET filter) before reuse and creates a new one when it differs or is gone, because an old pod
  during a rolling deploy can re-save a filter without m.room.redaction (A21). Backfill no longer
  stops at an empty chunk that still has an `end` token (Synapse returns those when a page holds
  only filtered events); an empty page counts as one event toward the cap so the loop stays
  bounded. Edits are applied right after the original's insert (bundled edits) or as m.replace
  events through inbound.apply_edit (same sender only); redactions blank the row, and a message
  the server already redacted is stored blank as ignored/redacted.
- 2026-11-15 (T-420 Task 3): the history feed is one UNION ALL statement (db/history.py) with
  keyset paging on (at, kind, id); s and o rows sort by created_at so a cursor stays put while a
  send is delivered. One SQL fragment (_REPLY_TO) resolves an event id to m/s/o/unknown for both
  inbound replies and worker replies. `history.parse_ref` is the m/s/o id parser for the feed
  (the send routes use it too). `status` is null on m
  items: the inbound queue status is internal; s and o items carry their delivery status. In an
  anonymized room a `from` filter that is not a pseudonym returns an empty page rather than an
  error, so a real name is never resolved or confirmed.

- 2026-11-15 (T-420, task 4): `GET /chat/v1/history` (agent listener, room from the binding) and
  `GET /chat/v1/admin/rooms/{room_id}/messages` (admin router only, read only). The UI
  `room_messages` handler is now registered only when `admin=False`, because `_ADMIN_ROUTES`
  filters by path and method and would otherwise also expose the UI shape on the admin path.
  A malformed id is `bad_reference` like an unknown or foreign one (A14); `before` with `after`
  is `invalid_request`.

## 2026-11-15 Attachment requests (T-420, Task 5)
- `db.history.parse_ref` is the one m/s/o id parser; `sends.parse_ref` is gone and `sends.resolve`
  rejects `o` ids like any unusable reference (uniform 422 bad_reference).
- `POST /chat/v1/attachments` takes the per-room advisory lock `attachments:<room>` for the
  rate-limit count and the insert, so two requests cannot both pass the 10-per-600-s count. The
  in-flight dedupe is the partial unique index on `inbound_id`, not a read-then-write.
- The status path parameter is `request_id`; any id that is not the caller's `a<n>` is the same
  404 text (never echoed). `INTERRUPTED` lives in `db/attachment_requests.py`.
- Settled requests are pruned daily with the other tables (retention_days).

## Attachment fetcher (2026-11-15, T-420)

- `attachment_fetcher.AttachmentFetcher` is the fifth bot tick (after room_requests): one queued
  request per tick, claimed in its own committed transaction; the binding and row are re-checked
  on one connection, then no connection is held during the download and the post. The file is
  posted with `post_message` without `from_user`: it is system delivery, not a user message, so
  the text tells the agent to reach the chat with a `<chat-send>` envelope (2026-11-17).
- The sender role (`role="admin"`) is computed like the worker does, from `anonymize.is_admin`;
  anonymized rooms show the pseudonym and never the original file name.
- Only fixed reasons and ids are logged (`attachment a<id>: <status> (<reason>)`).

## History crawl (2026-11-15, T-420, Task 8)

- `history_crawl.HistoryCrawler` is the sixth bot tick (after the attachment fetcher): one
  `/messages` page of at most 100 events per tick, up to `MATRIX_HISTORY_CRAWL_MAX_EVENTS`
  (default 2000, 0 disables) per room, one crawl per room ever (`history_crawls.room_id` unique,
  queued from active `room_bindings`). The crawler is the table's only writer.
- The start token is never the live edge: just before the room's oldest stored sync row
  (`/context` start), else the committed sync `next_batch` (everything older was already offered
  to sync). With no sync position yet the crawl stays queued. A live event therefore can never
  become a history row instead of being dispatched.
- History rows go through `SyncLoop.intake_history` / `_apply_event(history=True)`: always
  `ignored`, reason `history` or the policy refusal, `source='history'`, no mention, trigger,
  queue cap or media promotion. History mode uses its own room-context cache so the concurrent
  sync loop's per-response cache is never shared.
- An empty page with `end` counts as one event toward the cap, so a crawl always ends.
- A 429's `retry_after` is capped at the crawler-only `MAX_CRAWL_RETRY_SECONDS` (600 s) before the
  crawl is deferred (not_before is scheduled, never slept); without one the crawl waits `DEFAULT_RETRY_SECONDS` (60 s).
- A non-Matrix exception on a crawl ends it as `failed` with reason `internal_error` (2026-11-15):
  otherwise the same oldest crawl would come back every tick and block every other room. Only the
  exception type name and crawl id are logged; a failure while settling never masks it.
- `retry_after_seconds` accepts only ASCII digits in `Retry-After` (superscript two passes `isdigit()`
  but `float` raises).

## Approvals foundation (2026-11-16, S-63, Task 17)

- Migration is `0012_approvals.sql` (0011 was taken by chat history). Tables `approval_digests`,
  `approval_messages`, `approval_answers`: the connector is their only writer; answers are a
  pending/done queue. `test_migration_0011_resets_sync_filter` now indexes `MIGRATIONS[-2]`
  because it targets the 0011 migration, no longer the last one.
- `BotSettings.approvals_room` empty turns approvals off; when set, the taskboard url and token,
  and at least one approver are required, and approvers must not overlap the bot's own
  identities or bridge bots (a bridged puppet or the bot itself must never answer).
- `approvals.py` verdict parsers are exact-match only (whole text in a small set): anything
  ambiguous is not a verdict and stays chat. The delegate client always sends `source: "matrix"`.

## Approval answers from Matrix (2026-11-16, S-63, Task 19)

- Sync intercepts approval answers in `_apply_joined` before message intake, never in
  `_apply_event`: the pre-join history crawl also goes through `_apply_event`, and an old
  reaction must never answer an approval. Only events recorded as answers are skipped; an
  ambiguous reply falls through and reaches the bound session as chat.
- `m.reaction` joins the sync filter and the gap backfill (`BACKFILL_FILTER`,
  `_messages_oldest_first`), so a reaction during a limited sync is not lost. It is not a
  room-info type, so it never triggers a state fetch.
- A digest target accepts only `digest_verdict` text ("all go"); a plain "go" on a digest is
  chat. Answers are sent before new approvals are posted in each tick; a retryable delegate
  error (no response, 5xx, 401, 403, 408, 429) stops the tick and leaves the row pending; any other
  4xx finishes it with `[{"error": status}]` so one bad row cannot block the queue, and posts
  "Could not answer D-<id>: <status>." as a reply to the approver's event (2026-11-16, review fix).
- The delegate client refuses an `items` that is not a list of objects (non-retryable
  `TaskboardError`); `_normalise` strips trailing spaces with the punctuation ("go ." is go).

## Approvals chart, e2e and the approvals room (2026-11-16, S-63, Task 20)

- `approvals.room`, `approvals.approverIds` and `approvals.taskboard` (url, token or tokenSecret)
  render MATRIX_APPROVALS_ROOM, MATRIX_APPROVER_IDS and MATRIX_TASKBOARD_URL into the ConfigMap and
  MATRIX_TASKBOARD_TOKEN as a secretKeyRef in the bot container only (the api and migrate never
  hold a delegate token). With a room set, a missing approver list, url, token source or both
  token sources fail the render.
- Approvers must be disjoint from the bot identities and bridge bots (settings check): a
  double puppet could otherwise forge a reaction as an approver, and an agent that can make the
  bot or a puppet speak would approve its own request.
- The answer inbox is the connector's `approval_answers` table (pending/done): the sync loop only
  records, the bot step sends to the taskboard, so a taskboard outage retries instead of losing
  an answer. A digest is bound to its own decision ids, so "all go" never answers a later approval.
- Reaction keys are normalised before matching because the emoji keys the bridge carries are
  undocumented.
- Verified: the answer intercept (`_approval_answer` in `_apply_joined`) runs for every joined
  room before rules and classification are consulted, so the approvals room needs no allow rule and
  no bridge state. The e2e proves it with a bare room and no rule. A non-answer message in that
  room still takes the normal intake path (default deny drops it).
- `FakeHomeserver.events(room_id)` returns the room's full events, so a test can react to an event
  the bot posted (event ids are positional, not predictable).

## Admin room notes by room id (2026-11-16, S-68 step 17, T-525)

- `GET`/`PUT /chat/v1/admin/rooms/{room_id}/notes` reuse the notes body, cap, version guard and
  `RoomNotes` shape of the bound-session routes (shared module-level helpers), but take the room
  from the path and need no binding: cron sessions are room-unbound. An unknown room is 404.
- The writer name is the optional `X-Orch-Slug` header (the admin routes otherwise ignore session
  headers); the audit line is `admin notes update session=<slug> room=<id>` and never carries the
  body. The skill store stays in this release.

## Envelope replies, no final text (2026-11-17, S-70 step 6, T-563)

- Decision 793: the connector never posts a turn's final text. A completed turn closes the run
  `handled` (reason None) with no outbound rows; failure paths (`turn_failed`, timeout, message
  failed or cancelled) still post `NOTICE`. Why: posting final text was fail-open and leaked
  operator recaps, "no visible output" emoji and foreign-turn answers into the room (T-556).
  Removed: `_disclosure`, `_send_match`, `_tell`, `EMPTY_REPLY`, `ALREADY_SENT`,
  `sends.message_sends_since`, the run reasons `pseudonym_disclosed` (tracker side only; the API
  still answers 422) and the session "not sent" message.
- Prompt: `REPLY_RULE` is fixed text after `ADMIN_TRUST_RULE` on every first prompt (a custom
  preamble cannot drop it); `FOLLOW_REMINDER` follows "New messages in the chat:" in every follow
  batch. No prompt text names `orchestrator_chat_send/react/status`.
- `api/agent.py` is unchanged: the orchestrator's envelope worker calls it with the same policy,
  caps and pseudonym check.

## Room skill store removed (2026-11-17, S-68 step 24, T-532)

- The `/chat/v1/skills` routes, `db/skills.py`, the `room_skills` table (migration
  `0013_drop_room_skills.sql`, `DROP TABLE IF EXISTS`), the skill settings
  (`MATRIX_ROOM_SKILL_MAX_CHARS`, `MATRIX_ROOM_SKILLS_MAX` and their chart values), the
  `<room-skills>` index in the first prompt and the "write SKILL.md yourself" text are gone. Why:
  skills now live in each session's skills directory and ride on the session backups
  (`orchestrator_backup_save`), so every session of the project gets them (T-505).
- `MEMORY_RULE` keeps the notes text and gains "Reusable workflows are written as skills in your
  skills directory; call orchestrator_backup_save afterwards so every session of the project gets
  them." The old "tell the user you saved it" sentence is dropped (F8). `memory_block` takes only
  the rule and the notes.
- Tests deleted as no longer accurate (the feature is removed): `tests/test_skills_repo.py`, the
  skill route tests in `test_api_agent.py`, the skill cases in settings, chart, readme, prompt and
  worker tests. Merge only after the room skills migration (T-530) is verified: rooms with stored
  skills lose them at deploy.

## Room notes in named sections (2026-11-21, S-79 step 5, T-634)

- Section model: five fixed sections, one constant table `db/notes.SECTIONS` (key, title,
  inject): preferences, requests, work-in-progress are injected into the first prompt;
  provided-information and repeatable-workflows are not (they can be large, e.g. decklists) and
  appear there only as an index line `<Title> - <N> chars, read with
  orchestrator_chat_notes_get(section="<key>")`. Why: first prompts stay small while bulk data
  stays reachable on demand.
- Caps (decision 9): `MATRIX_ROOM_NOTES_MAX_CHARS` is now the room total (default 20000),
  `MATRIX_ROOM_NOTES_SECTION_MAX_CHARS` one section (default 4000); a settings validator refuses
  a section cap above the total. 413 `section_too_large` / `notes_too_large`.
- `put_section` takes `pg_advisory_xact_lock` keyed on sha256("room_note_sections:<room>") before
  summing the other sections, so two concurrent writes to different sections cannot together pass
  the total (the per-row version guard alone would not catch that).
- Migration 0014 copies every `room_notes` row verbatim into `room_notes_migration_log` (the
  exact pre-migration body, for recovery), splits bodies on `## <Title>` headings in PL/pgSQL
  (unknown headings and leading text go to work-in-progress), then drops `room_notes`. Caps are
  not enforced on migrated data: an over-cap section is readable and must fit on its next write.
- Decision 7: the whole-body `PUT /chat/v1/notes` and the admin whole PUT are removed with no
  backward compatibility; `NotesIn` and the top-level `version`/`updated_at` are gone.
- GET keeps `body` (the non-empty sections rendered `## <Title>` plus body in fixed order) so the
  orchestrator handoff package, which reads `body`, needs no change.

- 2026-11-22 (S-83 step 18): the entries above naming `orchestrator_backup_save` are superseded; that tool was removed and `MEMORY_RULE` now names `orchestrator_volume_save`.
- 2026-11-23 (S-83 final review): `MEMORY_RULE` now tells the agent how orchestrator_volume_save works in v0.62.0: it takes the volume name (POST /agent/v1/volumes/{name}/save), works only for a volume attached with `backup_restore` (else 403 backup_not_enabled, 404 for an unmounted name, 409 volume_busy) and `orchestrator_volumes_status` lists the session's volumes. The old wording named no volume, so calls failed with 422/403.
