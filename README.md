# agents-matrix-connector

Part of the claude-code-tatara project (python).

## What this is

A Python microservice that connects to the Matrix homeserver as a bot user, discovers
Messenger chats bridged by mautrix-meta, applies allow/deny rules and routes allowed chats
to agents-orchestrator sessions.

## Development

Run `pre-commit install` once per clone to enable the gitleaks hook.

- `mise run test` runs the test suite.
- `mise run lint` runs ruff check and format check.
- DB tests need Docker (testcontainers).

## Configuration

All variables use the `MATRIX_` prefix. Subcommands: migrate, api, bot.

| Variable | Default | Read by | Meaning |
|---|---|---|---|
| `MATRIX_DATABASE_URL` | none (required) | migrate, api, bot | Postgres URL of the connector's own database |
| `MATRIX_ORCHESTRATOR_URL` | none (required) | migrate, api, bot | agents-orchestrator base URL |
| `MATRIX_ORCHESTRATOR_TOKEN` | none | migrate, api, bot | orchestrator bearer token (required unless the file is set) |
| `MATRIX_ORCHESTRATOR_TOKEN_FILE` | empty | migrate, api, bot | file holding the orchestrator token; wins over the variable |
| `MATRIX_HTTP_HOST` | 0.0.0.0 | api | bind address |
| `MATRIX_HTTP_PORT` | 8000 | api | bind port |
| `MATRIX_LOG_LEVEL` | INFO | migrate, api, bot | log level |
| `MATRIX_SHUTDOWN_GRACE_SECONDS` | 20 | api, bot | graceful shutdown wait |
| `MATRIX_HOMESERVER_URL` | none (required) | migrate, api, bot | Matrix homeserver http(s) URL, trailing slash stripped |
| `MATRIX_USER_ID` | none (required) | migrate, api, bot | bot Matrix user id, `@name:server` |
| `MATRIX_DEVICE_ID` | agents-matrix-connector | bot | Matrix device id |
| `MATRIX_SELF_IDENTITIES` | empty | bot | extra Matrix user ids of the bot, comma separated |
| `MATRIX_BRIDGE_BOTS` | @facebookbot:szymonrichert.pl | bot | bridge bot user ids, comma separated |
| `MATRIX_PUPPET_PATTERN` | ^@facebook_(?P<id>[0-9]+): | bot | regex for bridged puppet user ids, needs a named group `id`; matched in full against the `@localpart:` of ids on the server of `MATRIX_USER_ID` |
| `MATRIX_DEFAULT_POLICY` | deny | bot | `deny` or `allow` for chats with no rule |
| `MATRIX_GROUP_TRIGGER` | mention | bot | `mention` or `all` in group chats |
| `MATRIX_MAX_NEW_SESSIONS` | 3 | bot | max new sessions started at once (>= 1) |
| `MATRIX_MAX_QUEUED_PER_ROOM` | 20 | bot | max queued messages per room (>= 1) |
| `MATRIX_MAX_INBOUND_CHARS` | 8000 | bot | max characters taken from one inbound message (>= 100) |
| `MATRIX_REPLY_CHUNK_CHARS` | 4000 | bot | reply split size (100..20000) |
| `MATRIX_RUN_TIMEOUT_SECONDS` | 1800 | bot | max wait for one agent run (>= 60) |
| `MATRIX_POLL_SECONDS` | 3 | bot | orchestrator poll interval (> 0) |
| `MATRIX_SYNC_TIMEOUT_SECONDS` | 30 | bot | Matrix long-poll timeout (1..120) |
| `MATRIX_RETENTION_DAYS` | 30 | bot | days messages are kept (>= 1) |
| `MATRIX_BACKFILL_MAX_EVENTS` | 500 | bot | max events read back per room when a sync timeline arrives limited (>= 1) |
| `MATRIX_API_ALLOWED_CIDRS` | 127.0.0.1/32 | api | client networks allowed to call the api, comma separated |
| `MATRIX_AGENT_PORT` | 8001 | api | bind port of the agent listener; must differ from `MATRIX_HTTP_PORT` |
| `MATRIX_AGENT_TOKEN_SHA256` | empty | api | sha256 hex (64 lowercase characters) of the orchestrator's agent bearer token; empty leaves the agent listener unbound |
| `MATRIX_AGENT_RATE_PER_MINUTE` | 20 | api | max agent sends (messages and reactions together) per session in the last 60 s (>= 1) |
| `MATRIX_AGENT_ROOM_CHUNKS_PER_HOUR` | 60 | api | max Matrix events (message chunks of `MATRIX_REPLY_CHUNK_CHARS`, one per reaction) that agent sends may put into one room in the last hour, all sessions together (>= 1) |
| `MATRIX_ACCESS_TOKEN` | none | bot | Matrix access token (required unless the file is set) |
| `MATRIX_ACCESS_TOKEN_FILE` | empty | bot | file holding the Matrix access token; wins over the variable, stripped |

## API

The UI API of the `api` subcommand. Base path `/matrix/api/v1`; the OpenAPI document is
committed as `openapi.json` (regenerate with
`uv run python -m agents_matrix_connector.api.openapi > openapi.json`) and served at
`/matrix/api/v1/openapi.json`. `/healthz` and `/readyz` are unauthenticated probes.

Auth: the service sits behind SSO at the ingress (oauth2-proxy). It also accepts only direct
socket peers inside `MATRIX_API_ALLOWED_CIDRS` (X-Forwarded-For is never read); any other
source gets `403 forbidden`. Probes are exempt. In a cluster without the ingress, reach it
with `kubectl port-forward` from an allowed source.

Errors are `{"error": {"code": "...", "message": "..."}}`. Codes:

| Code | Status | Meaning |
|---|---|---|
| `invalid_request` | 422 | a query, path or body field failed validation |
| `invalid_rule` | 422 | the rule `list`, `kind` or `value` is not valid |
| `rule_exists` | 409 | the same rule is already present |
| `unknown_preset` | 422 | the preset id does not exist in the orchestrator |
| `not_found` | 404 | the room, rule or path does not exist |
| `forbidden` | 403 | the caller's source address is not allowed |

A database outage answers `503 unavailable`, an unreachable orchestrator `503 unavailable`
and an orchestrator refusal `502 orchestrator_error`.

Compatibility is additive only: fields and routes may be added, never renamed, retyped or
removed within `v1`.

Access state of a room or user: `state` is `allowed`, `denied` or `default` (no rule
matched, the default policy applies), with a `reason` and the matching `rule_id`. Reasons:

- `room_infrastructure`: a space or other non-chat room, never routed
- `room_encrypted`: an end-to-end encrypted room, never routed
- `denied: <kind>`: a deny rule of that kind matched (`denied: user`, `denied: room`, ...)
- `not_allowed`: no allow rule matched and the policy requires one

Message statuses are `new`, `in-progress`, `handled`, `ignored` and `failed`, with a `reason`
when not plain handled:

- `not_mentioned`: ignored, a group message that did not mention the bot
- `queue_full`: ignored, the room already has `MATRIX_MAX_QUEUED_PER_ROOM` queued messages
- `unsupported_content`: ignored, not a text message
- `no_preset`: ignored, the room has no preset and there is no default preset
- `preset_not_found`: failed, the preset no longer exists in the orchestrator
- `session_gone`: failed, the room's session was deleted; the next message starts a new one
- `turn_failed`: failed, the agent turn failed
- `message_failed`: failed, the orchestrator message failed
- `message_cancelled`: failed, the orchestrator message was cancelled
- `timeout`: failed, no result within `MATRIX_RUN_TIMEOUT_SECONDS`
- `empty_reply`: handled, the agent answered with no text
- `already_sent`: handled, the turn's final answer equals (stripped) a message the agent sent
  with the send tool during the run, so it is not posted a second time
- `forbidden`: an outbound reply the homeserver refused (a failed outbound row)
- `denied: <kind>` and `not_allowed` also appear on ignored messages

Only a delivered send suppresses the final answer: when a turn completes, the bot compares its
final answer, stripped, with the stripped text of every message send of the same session
created at or after the run started. A matching `sent` send closes the run `already_sent` and
posts nothing. A matching send still `queued` or `sending` keeps the run in progress (the room's
next batch waits) until it settles, bounded by `MATRIX_RUN_TIMEOUT_SECONDS` from the run's
start; when the timeout passes first the final answer is posted. A matching send that ended
`failed` or `refused` does not count, so the final answer is posted. Every other final answer
is posted as usual.

### `GET /matrix/api/v1/meta`

Bot user, homeserver, default policy, group trigger, rule kinds per list and sync health.

    curl -s http://localhost:8000/matrix/api/v1/meta

### `GET /matrix/api/v1/rooms`

Discovered rooms. Query: `kind` (`dm`, `group`, `matrix`, `infrastructure`), `access`
(`allowed`, `denied`, `default`). 200 list of rooms with `access`, `preset_id`, `trigger`,
`effective_trigger`, `agents_may_initiate` (false when the room has no settings), `session`
(the latest run: `id`, `slug`, `status`, or null), `binding` (the room's active session binding:
`session_id`, `slug`, `origin` `inbound` or `agent`, `since`; `session_id` null while a first run
reserves the room; null when the room is unbound) and `queued` (messages waiting); 422
`invalid_request`. The binding is read only: no route writes, releases or moves it.

    curl -s 'http://localhost:8000/matrix/api/v1/rooms?kind=dm&access=allowed'

### `GET /matrix/api/v1/rooms/{room_id}`

One room with its `members` (`user_id`, `display_name`, `facebook_id`, `membership`, `access`). 200; 404 `not_found`.

    curl -s 'http://localhost:8000/matrix/api/v1/rooms/!abc:server'

### `PUT /matrix/api/v1/rooms/{room_id}/settings`

Body `{"preset_id": uuid|null, "trigger": "all"|"mention"|null, "agents_may_initiate":
true|false|null}`: the room's preset and trigger override (null or omitted clears) and whether
agents may start a chat in the room while it is unbound (`agents_may_initiate`, off by default).
An omitted or null `agents_may_initiate` keeps the stored value (false for a room without
settings), so a save of the preset or trigger never grants or revokes it. 200 room; 404
`not_found`; 422 `unknown_preset` or `invalid_request`; 503 `unavailable`; 502
`orchestrator_error`.

    curl -s -X PUT -H 'content-type: application/json' -d '{"trigger": "all"}' \
      'http://localhost:8000/matrix/api/v1/rooms/!abc:server/settings'

### `GET /matrix/api/v1/rooms/{room_id}/messages`

The room's merged inbound and outbound feed, newest first. Query: `limit` (1..200, default
50), `before_id` (a message id like `in-12` or `out-3`, from the previous page). 200; 404
`not_found`; 422 `invalid_request`.

    curl -s 'http://localhost:8000/matrix/api/v1/rooms/!abc:server/messages?limit=20'

### `GET /matrix/api/v1/users`

Discovered users with their rooms and access. Query: `q` (text filter), `limit` (1..500,
default 100). 200; 422 `invalid_request`.

    curl -s 'http://localhost:8000/matrix/api/v1/users?q=alice'

### `GET /matrix/api/v1/rules`

All rules, `{"allow": [...], "deny": [...]}`; each has `id`, `list`, `kind`, `value`, `note`,
`preamble` (a Jinja template or null) and `created_at`. 200.

    curl -s http://localhost:8000/matrix/api/v1/rules

### `POST /matrix/api/v1/rules`

Body `{"list": "allow"|"deny", "kind": "user"|"facebook"|"room"|"alias"|"display_name",
"value": "...", "note": "..."|null, "preamble": "..."|null}` (`display_name` is deny only; see
`rule_kinds` in `/meta`). 201 rule; 409 `rule_exists`; 422 `invalid_rule`, `invalid_template` or
`invalid_request`.

    curl -s -X POST -H 'content-type: application/json' \
      -d '{"list": "allow", "kind": "facebook", "value": "100012345"}' \
      http://localhost:8000/matrix/api/v1/rules

### `PATCH /matrix/api/v1/rules/{rule_id}`

Body `{"preamble": "..."|null}`: the first-prompt preamble for chats this rule allows (null
clears it; the global preamble, then the built-in default, apply). 200 rule; 404 `not_found`;
422 `invalid_template` (syntax error or a variable outside `room_name`, `room_id`, `sender`,
`rule_name`, `now`).

    curl -s -X PATCH -H 'content-type: application/json' \
      -d '{"preamble": "Chat with {{ sender }}"}' http://localhost:8000/matrix/api/v1/rules/3

### `DELETE /matrix/api/v1/rules/{rule_id}`

204 on success; 404 `not_found`; 422 `invalid_request` for a non-integer id.

    curl -s -X DELETE http://localhost:8000/matrix/api/v1/rules/3

### `GET /matrix/api/v1/settings`

`{"default_preset_id": uuid|null, "preamble": string|null, "default_preamble": string}`: the
preset used by rooms without their own, the global preamble template (null means the built-in
`default_preamble`). 200.

    curl -s http://localhost:8000/matrix/api/v1/settings

### `PUT /matrix/api/v1/settings`

Body `{"default_preset_id": uuid|null, "preamble": string|null}`; a field left out keeps its
stored value. 200; 422 `unknown_preset`, `invalid_template` or `invalid_request`; 503
`unavailable`; 502 `orchestrator_error`.

    curl -s -X PUT -H 'content-type: application/json' \
      -d '{"default_preset_id": null}' http://localhost:8000/matrix/api/v1/settings

### `POST /matrix/api/v1/templates/preview`

Body `{"template": "...", "context": {...}|null}`; the sample context (`room_name`, `room_id`,
`sender`, `rule_name`, `now`) is overlaid with `context`. 200 `{"rendered": string|null,
"error": {"line", "message"}|null, "variables": [...]}`; template errors are data, not HTTP
errors. 422 `invalid_request`.

    curl -s -X POST -H 'content-type: application/json' \
      -d '{"template": "Chat {{ room_name }}"}' \
      http://localhost:8000/matrix/api/v1/templates/preview

### `GET /matrix/api/v1/messages`

Inbound messages across all rooms, newest first. Query: `status` (`new`, `in-progress`,
`handled`, `ignored`, `failed`), `room_id`, `limit` (1..200, default 50), `before_id`
(`in-<n>` only). 200; 422 `invalid_request`.

    curl -s 'http://localhost:8000/matrix/api/v1/messages?status=failed'

## Agent API

The agent listener of the `api` subcommand, called by agents-orchestrator on behalf of a session
leader (the `orchestrator_chat_*` tools). With `MATRIX_AGENT_TOKEN_SHA256` set, the `api`
subcommand also binds `MATRIX_AGENT_PORT`, which serves only `/chat/v1/*` (the chat routes and the five `/chat/v1/admin` routes), `/healthz` and
`/readyz`. Base path `/chat/v1`; the OpenAPI document is committed as `openapi-agent.json`
(regenerate with `uv run python -m agents_matrix_connector.api.openapi --agent >
openapi-agent.json`) and served at `/chat/v1/openapi.json`.

Auth: every path but the probes needs `Authorization: Bearer <token>`, where the sha256 hex of
the token equals `MATRIX_AGENT_TOKEN_SHA256` (constant-time compare); anything else gets `401
unauthorized` with `WWW-Authenticate: Bearer`. The token is the only credential: no source
allowlist applies to this port, so the orchestrator pod reaches it directly. Paths outside
`/chat/v1` get `404 not_found`. The token and its hash never reach a log line or an error body.
The UI app on `MATRIX_HTTP_PORT` serves no `/chat/v1` path. Every `/chat/v1` call also carries
`X-Orch-Session: <session uuid>` (missing or malformed: `401 unauthorized`) and `X-Orch-Slug:
<slug>` (stored with the send; may be absent, else the orchestrator's session slug rules: a
DNS-1123 label of at most 40 characters, not `orchestrator` or `orchestrator-*`, or `400
invalid_slug`). The agent's own token never reaches the
connector.

Message ids: `m<n>` is a chat message (the `id` the prompt shows on each `<message>`), `s<n>`
a send of this session. Raw Matrix event ids are never accepted.

Room rule (design D4): a session may post to a room only when both hold:

- (a) the room is policy-allowed for agent sends (see "Let agents start chats in a room"):
  checked when the api accepts the request, when the bot claims the send and again before
  each of its Matrix events is sent;
- (b) the room is bound to the session, or it is unbound, flagged `agents_may_initiate` and the
  session is bound to no room (an initiation).

Binding rule: a chat room and a session are bound 1:1. A room is bound to at most one session
and a session to at most one room, through `room_bindings`, whose only writer is the bot. The api
never writes a binding: an initiation is accepted unbound, and the bot's claim of the send binds
the room to the session. A room reserved by another session's first run in progress counts as
bound to another session. The binding is released only when the session is deleted (or when the
send that created it reached nobody); turning `agents_may_initiate` off does not release it.

Checks run in this order and a refusal queues nothing: `X-Orch-Session` (`401 unauthorized`),
`X-Orch-Slug` (`400 invalid_slug`), body (`422 invalid_request`, `422 invalid_key`, `422 invalid_text`), references (`422 bad_reference`), room choice
(`422 room_required`), (a) (`403 room_not_allowed`), (b) (`409 room_bound`, `403
room_not_authorised`, `409 session_bound`, in that order), duplicate (`202` with the existing
send), rate (`429 rate_limited`), room chunk cap (`429 room_chunk_limit`). The (b) check is
advisory: the bot's claim re-checks it, refusing a send that lost a race with reason
`room_bound`, `session_bound` or `room_not_authorised`. The duplicate lookup, the rate count and
the room chunk count run under transaction-scoped advisory locks on the session and on the room,
so they are atomic with the insert and parallel requests cannot exceed either limit.

A reference (`reply_to` or `to`) to an `m<n>` of a room that is not the session's own bound room
whose room check fails answers `422 bad_reference`, the same as an unknown id, so a session
cannot probe which ids exist in other rooms. In the session's own room the specific code applies.

Duplicate rule: a request identical to a send of the same session, room and kind, with the same
`text` (message) or `key` (reaction) and the same `reply_to` (a reaction's `to`), compared
exactly, whose status is `queued`, `sending` or `sent` and that was created in the last 60 s,
answers `202` with the existing send's id and current status and stores nothing, so a retry after
a timeout never posts twice. A `failed` or `refused` send is not a duplicate. A duplicate is not
counted by the rate limit and is answered even at the limit.

Rate: at most `MATRIX_AGENT_RATE_PER_MINUTE` sends (messages and reactions together, of any
status) per session in the last 60 s.

Room chunk cap: at most `MATRIX_AGENT_ROOM_CHUNKS_PER_HOUR` Matrix events per room in the last
hour, summed over every session that sent there. A message counts one per chunk (its text split
at `MATRIX_REPLY_CHUNK_CHARS`, so one 16000-character message is 4 chunks at the default), a
reaction one; `refused` sends do not count. A send that would pass the cap is refused whole.

Errors are `{"error": {"code": "...", "message": "..."}}`. Codes:

| Code | Status | Meaning |
|---|---|---|
| `unauthorized` | 401 | the bearer token is missing or wrong, or `X-Orch-Session` is not a uuid |
| `invalid_slug` | 400 | `X-Orch-Slug` is sent but is not a valid session slug |
| `not_found` | 404 | the id is not one of this session's sends, or the path does not exist |
| `invalid_request` | 422 | a body field failed validation: `text` empty or over 16000 characters, a wrong type, an unknown field |
| `invalid_text` | 422 | the message `text` has no non-whitespace character, so it could never be delivered |
| `invalid_key` | 422 | the reaction `key` is not 1 to 16 code points, or has ASCII, whitespace or control characters |
| `bad_reference` | 422 | `reply_to` or `to` is not an `m<n>` or `s<n>` this session can reference: unknown, another session's send, a send not yet `sent`, a reaction, of another room than the given `room`, an `m<n>` whose room is refused to a session that is not bound to it, or a raw event id |
| `room_required` | 422 | no `room`, no `reply_to`, and the session is bound to no room |
| `room_not_allowed` | 403 | the room is unknown or not policy-allowed for agent sends (both answer the same) |
| `room_bound` | 409 | the room is bound to (or reserved by) another session |
| `room_not_authorised` | 403 | the room is unbound and not flagged `agents_may_initiate` |
| `session_bound` | 409 | the room is unbound and flagged, but the session is already bound to another room |
| `rate_limited` | 429 | the session reached `MATRIX_AGENT_RATE_PER_MINUTE` sends in the last 60 s |
| `room_chunk_limit` | 429 | the send would take the room past `MATRIX_AGENT_ROOM_CHUNKS_PER_HOUR` chunks in the last hour |
| `unavailable` | 503 | the database is unavailable |

Send statuses: `queued` (accepted, waiting for the bot), `sending` (claimed by the bot, being
delivered), `sent` (every Matrix event delivered), `failed` (delivery failed), `refused` (the bot's
claim or send-time check refused it). Reasons:

- `room_bound`, `session_bound`, `room_not_authorised`: refused, (b) failed at the bot's claim
- `room_infrastructure`, `room_encrypted`, `denied: <kind>`, `not_allowed`: refused, (a)
  failed at the claim or before a Matrix event was sent
- `forbidden`: failed, the homeserver refused the event; another failure carries the
  homeserver's errcode, or `send_failed`

### `GET /chat/v1/rooms`

The rooms this session may post to, exactly those passing (a) and (b): its bound room (`bound`
true) when it passes (a), else, while the session is bound to no room, every unbound flagged
room passing (a). The bound room comes first, then by `name` (code point order, rooms without a
name last), then `room_id`. 200 `{"rooms": [{"room_id", "name", "kind", "bound",
"agents_may_initiate"}]}`; 401 `unauthorized`.

    curl -s -H "Authorization: Bearer $TOKEN" -H "X-Orch-Session: $SESSION" \
      http://localhost:8001/chat/v1/rooms

### `POST /chat/v1/messages`

Body `{"text": str, "room": str|null, "reply_to": "m<n>"|"s<n>"|null}`: `text` 1 to 16000
characters, stored unchanged and split into `MATRIX_REPLY_CHUNK_CHARS` chunks by the bot.
`room` is a Matrix room id; omitted, the room of `reply_to`, else the session's bound room. A
`reply_to` sends a quoted reply to that message. 202 `{"id": "s<n>", "status": "queued"}` (a
duplicate: the existing id and its status `queued`, `sending` or `sent`); 401 `unauthorized`;
422 `invalid_request`, `invalid_text`, `bad_reference` or `room_required`; 403 `room_not_allowed` or
`room_not_authorised`; 409 `room_bound` or `session_bound`; 429 `rate_limited`; 503
`unavailable`.

    curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "X-Orch-Session: $SESSION" \
      -H 'content-type: application/json' -d '{"text": "Build finished", "reply_to": "m42"}' \
      http://localhost:8001/chat/v1/messages

### `POST /chat/v1/reactions`

Body `{"to": "m<n>"|"s<n>", "key": str}`: react to that message with an emoji `key`; the
reaction goes to the message's room, which passes (a) and (b) like a given room. 202 `{"id":
"s<n>", "status": "queued"}` (a duplicate: as for messages); 401 `unauthorized`; 422
`invalid_request`, `invalid_key` or `bad_reference`; 403 `room_not_allowed` or
`room_not_authorised`; 409 `room_bound` or `session_bound`; 429 `rate_limited`; 503
`unavailable`.

    curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "X-Orch-Session: $SESSION" \
      -H 'content-type: application/json' -d '{"to": "m42", "key": "\ud83d\udc4d"}' \
      http://localhost:8001/chat/v1/reactions

### `GET /chat/v1/messages/{id}`

One of this session's sends by `s<n>`: 200 `{"id", "kind" ("message" or "reaction"), "status",
"reason", "room_id", "created_at", "sent_at"}`; 404 `not_found` for any other id, another
session's send included; 401 `unauthorized`.

    curl -s -H "Authorization: Bearer $TOKEN" -H "X-Orch-Session: $SESSION" \
      http://localhost:8001/chat/v1/messages/s7

### Admin routes (`/chat/v1/admin`)

The same handlers as the UI app, behind the same bearer, for the orchestrator's superleader
relay. They take no `X-Orch-Session`. Request and response bodies, status codes and error codes
are those of the UI routes of the same name.

### `GET /chat/v1/admin/rooms`

As `GET /matrix/api/v1/rooms`.

### `PUT /chat/v1/admin/rooms/{room_id}/settings`

As `PUT /matrix/api/v1/rooms/{room_id}/settings`.

### `GET /chat/v1/admin/rules`

As `GET /matrix/api/v1/rules`.

### `POST /chat/v1/admin/rules`

As `POST /matrix/api/v1/rules`.

### `DELETE /chat/v1/admin/rules/{rule_id}`

As `DELETE /matrix/api/v1/rules/{rule_id}`.

## Operations

### Bot identity (taken over from Hermes)

The connector runs as the Matrix user `@hermes:szymonrichert.pl`, device `hermes-agent`
(`MATRIX_USER_ID`, `MATRIX_DEVICE_ID`). That user is already logged in to mautrix-meta
through the bridge bot `@facebookbot:szymonrichert.pl` with Hermes' Facebook account
`61594633049360`, so no new account and no new Facebook login are needed. The bridged self
ghost `@facebook_61594633049360:szymonrichert.pl` must be listed in `MATRIX_SELF_IDENTITIES`
so the bot never answers its own Facebook messages.

The access token already exists in Secret `hermes-agent-matrix` (key `MATRIX_ACCESS_TOKEN`,
namespace `ai`). Copy it into the agents-helmfile SOPS secrets file through the bootstrap
script, which pipes values between processes and never writes them to disk unencrypted. The
chart mounts it as a file into the bot pod only (`MATRIX_ACCESS_TOKEN_FILE`). Never paste
the token into a README, a chat, a values file in clear or a commit.

Hermes is scaled to 0 in the infrastructure helmfile and must stay at 0 while the connector
runs: two clients on one account would both answer every message.

### First chat

The default policy is deny, so a newly discovered chat is stored as a room but not routed; its
(truncated) message bodies are kept so you can judge the pending chat. The only allow rule at first start is the room
`!iLWrAwaTMYnINJsyNv:szymonrichert.pl` (Hermes' owner DM and home channel, which exists only
for that Facebook login). To allow another chat, open it on the Chats page of the UI and
allow it, or call the API:

    curl -s -X POST -H 'content-type: application/json' \
      -d '{"list": "allow", "kind": "room", "value": "!iLWrAwaTMYnINJsyNv:szymonrichert.pl"}' \
      http://localhost:8000/matrix/api/v1/rules

Allow by `facebook` (the numeric Facebook id) or `user` to cover every chat of one person, or
by `room` for one chat. A deny rule always wins over an allow rule. Set a preset per room
(`PUT /matrix/api/v1/rooms/{room_id}/settings`) or one default preset
(`PUT /matrix/api/v1/settings`); an allowed unbound room with no preset is ignored with
`no_preset` (see "One session per room, one room per session").

### One session per room, one room per session

A chat room and a session are bound one to one (`room_bindings`): a room has at most one bound
session and a session at most one bound room. A room gets bound in one of two ways:

- an inbound chat in an unbound room starts a new `mx-<run id>` session from the room's preset
  (else the default preset) under `MATRIX_MAX_NEW_SESSIONS`; the run reserves the room when it
  claims the messages, so no agent can initiate there meanwhile, and the new session fills the
  binding. A first run that ends without a session (for example `preset_not_found`) releases
  the reservation (`run_failed`);
- an agent initiates in an unbound room flagged `agents_may_initiate` (see "Let agents start
  chats in a room"); its first send binds the room to its session, and a send that reached
  nobody releases it again (`send_failed`).

The user's messages in a bound room always go to the bound session, whatever it is (an `mx-`
chat session, a hive leader, a webhook or schedule session), and never to any other session; a
preset is needed only to start a new session in an unbound room. A session that has no other run
in the room gets the untrusted-input preamble with its first batch. A busy session (for example a
hive leader in a long turn) gets the batch queued behind its current turn; the bot follows that
message until the session replies or the run timeout (`MATRIX_RUN_TIMEOUT_SECONDS`, default 1800
s) passes, and meanwhile the room's later messages stay `new` and go in the next batch.

Only deleting the session releases the binding (`session_gone`): once the session is deleted
(not found, or being deleted), the bot releases the binding at the room's next message, and
otherwise within about a minute through a sweep of the bound sessions. The room is then unbound:
the next message starts a new `mx-` session (a preset is needed), and an agent may initiate
there if the room is flagged. Turning `agents_may_initiate` off or adding a deny rule does not
release the binding (a deny rule still stops every agent send and every inbound message). A
session check the orchestrator refuses keeps the binding and the messages `new`; an unavailable
orchestrator is retried on the next tick.

### Let agents start chats in a room

Agents may post only to policy-allowed rooms that are bound to their session, or that are
unbound and flagged `agents_may_initiate` (off by default). The flag lets an agent start a chat
only while the room is unbound; its first send binds the room to its session. Only the owner DM
`!iLWrAwaTMYnINJsyNv:szymonrichert.pl` gets the flag, set once after deploy, through the
agents-web toggle once deployed, or through a port-forward to the api container (bypassing its
SSO sidecar):

    kubectl -n agents port-forward deploy/agents-matrix-connector-api 8000:8000 &
    curl -s -X PUT -H 'content-type: application/json' -d '{"agents_may_initiate": true}' \
      'http://localhost:8000/matrix/api/v1/rooms/!iLWrAwaTMYnINJsyNv:szymonrichert.pl/settings'

The answer shows `agents_may_initiate` true and the room's `binding`. Send
`{"agents_may_initiate": false}` the same way to turn it off; turning it off does not release a
binding.

A room is policy-allowed for agent sends through the same rules as inbound chats, read for
a message with no sender: a room or alias allow rule allows it, or one current member that the
rules allow does (a `facebook` or `user` allow rule, or the default `allow` policy). Any denied
current member denies the room, as does a room or alias deny rule; an infrastructure or
encrypted room is never allowed. Current members are those who have joined, leaving out the bot
user, its self identities and the bridge bots. A room with no such member and no room or alias
allow rule is `not_allowed`.

### Read receipts

The bot marks a message read (a public Matrix `m.read` receipt on the newest message of the
batch, which mautrix-meta bridges to Messenger as "seen") when it hands it to the agent, in the
same step that starts the typing indicator. It never sends a receipt in chats it does not
route: denied chats, chats without a preset, and messages that were ignored or refused. A
failed receipt is logged and never costs the agent its reply.

### Handing the identity back to Hermes

1. Scale the connector (bot and api) to 0.
2. Revert the Hermes scale-down in the infrastructure helmfile and apply it.

Never run both at once.

### Token rotation

1. Log the `hermes-agent` device out of the homeserver.
2. Log in again as `@hermes:szymonrichert.pl` and obtain a new access token.
3. Update Secret `hermes-agent-matrix` (namespace `ai`) with it.
4. Re-run the agents-helmfile bootstrap so the SOPS file carries the new token, then apply
   the release so the bot pod restarts with it.

## Release

Every green push to `main` ends with the `release` job in `.github/workflows/ci.yaml`. It needs
every other job of the run, so only a fully green build is released:

- `scripts/next_version.py` computes the version from the conventional commits since the last
  `vX.Y.Z` git tag (every commit, branch commits behind merge commits included): a `type!:`
  subject or a `BREAKING CHANGE:` footer bumps major (also on 0.x), `feat` bumps minor, anything
  else bumps patch. With no `vX.Y.Z` tag yet the first release is `0.1.0`.
- The job copies the run's image
  `harbor.szymonrichert.pl/containers/agents-matrix-connector:<sha7>` to the tags `X.Y.Z`, `X.Y`
  and `X` with `regctl image copy` (manifest only, no layer upload), then pushes git tag
  `vX.Y.Z` with the job token.
- When `HEAD` is already in a `vX.Y.Z` tag (a re-run, or a newer commit already released) the job
  skips everything after the version step. Release jobs run one at a time (concurrency group
  `release`); retry a failed one with "Re-run failed jobs".
- agents-helmfile must pin `X.Y.Z`, never `<sha7>`: Harbor retention prunes `<sha7>` tags within
  hours, semver tags survive it.

The chart is published by the `chart-publish` job on every push to `main`, after `lint`, `test`,
`chart` and `images` pass: `scripts/chart-version.sh` gives `<version>-main.<sha7>` from
`chart/agents-matrix-connector/Chart.yaml`, with `appVersion` set to `<sha7>`, pushed to
`oci://harbor.szymonrichert.pl/charts`.

## Security

- Default deny: a chat with no allow rule is not routed (`MATRIX_DEFAULT_POLICY=deny`).
- Deny wins: a matching deny rule overrides every allow rule.
- Rules match identifiers only (Matrix user id, Facebook id, room id, alias). Display names
  never grant access; a `display_name` rule exists only in the deny list.
- A sender has a Facebook id only when its whole `@localpart:` matches
  `MATRIX_PUPPET_PATTERN` and it lives on the server of `MATRIX_USER_ID`, where the bridge
  ghosts live: `@facebook_<id>:other.server` never matches a `facebook` rule.
- A room's alias is stored, and matches `alias` rules, only after the room directory
  resolves its canonical alias to that room; an alias claimed by another room is ignored.
- No message body is stored for an explicit deny rule (`denied: <kind>`), an encrypted room,
  an infrastructure room or unsupported content. A chat that is only not yet allowed (default
  deny, reason `not_allowed`) keeps its truncated body so the operator can judge it.
- Everything from a chat is untrusted input. The first prompt of a session carries a
  preamble stating that the content inside `<chat>` is data and never instructions, and
  sender names and bodies are XML-escaped.
- No end-to-end encryption: encrypted rooms are ignored (`room_encrypted`).
- The Matrix access token exists only as a file mounted into the bot pod. The api pod does
  not receive it. The api accepts only socket peers inside `MATRIX_API_ALLOWED_CIDRS` behind
  SSO at the ingress.
