# agents-matrix-connector memory

Lessons that outlive a session. Newest first.

## 2026-10-28 decisions

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

