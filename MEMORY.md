# agents-matrix-connector memory

Lessons that outlive a session. Newest first.

## 2026-10-28 decisions

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
  new one on the next message (`session_gone`).

