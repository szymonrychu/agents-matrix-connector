# agents-matrix-connector memory

Lessons that outlive a session. Newest first.

## 2026-11-12 decisions

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
  tracker fails the run `pseudonym_disclosed` before `_send_match` (so it is never matched as
  already sent), posts nothing to the room (no generic notice, the agent is expected to answer
  itself) and posts a message into the run's session through the orchestrator naming the tokens
  and asking for a rephrased `orchestrator_chat_send`; that post is best effort and logged on
  failure. Matching folds NFKC/casefold and drops invisible characters so width or zero-width
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
