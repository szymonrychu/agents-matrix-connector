# agents-matrix-connector memory

Lessons that outlive a session. Newest first.

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

