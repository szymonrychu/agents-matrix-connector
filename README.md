# agents-matrix-connector

Part of the claude-code-tatara project (python).

## What this is

A Python microservice that connects to the Matrix homeserver as a bot user, discovers
Messenger chats bridged by mautrix-meta, applies allow/deny rules and routes allowed chats
to agents-orchestrator sessions.

## Development

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
- `forbidden`: an outbound reply the homeserver refused (a failed outbound row)
- `denied: <kind>` and `not_allowed` also appear on ignored messages

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

All rules, `{"allow": [...], "deny": [...]}`; each has `id`, `list`, `kind`, `value`, `note`
and `created_at`. 200.

    curl -s http://localhost:8000/matrix/api/v1/rules

### `POST /matrix/api/v1/rules`

Body `{"list": "allow"|"deny", "kind": "user"|"facebook"|"room"|"alias"|"display_name",
"value": "...", "note": "..."|null}` (`display_name` is deny only; see `rule_kinds` in
`/meta`). 201 rule; 409 `rule_exists`; 422 `invalid_rule` or `invalid_request`.

    curl -s -X POST -H 'content-type: application/json' \
      -d '{"list": "allow", "kind": "facebook", "value": "100012345"}' \
      http://localhost:8000/matrix/api/v1/rules

### `DELETE /matrix/api/v1/rules/{rule_id}`

204 on success; 404 `not_found`; 422 `invalid_request` for a non-integer id.

    curl -s -X DELETE http://localhost:8000/matrix/api/v1/rules/3

### `GET /matrix/api/v1/settings`

`{"default_preset_id": uuid|null}`, the preset used by rooms without their own. 200.

    curl -s http://localhost:8000/matrix/api/v1/settings

### `PUT /matrix/api/v1/settings`

Body `{"default_preset_id": uuid|null}`. 200; 422 `unknown_preset` or `invalid_request`; 503
`unavailable`; 502 `orchestrator_error`.

    curl -s -X PUT -H 'content-type: application/json' \
      -d '{"default_preset_id": null}' http://localhost:8000/matrix/api/v1/settings

### `GET /matrix/api/v1/messages`

Inbound messages across all rooms, newest first. Query: `status` (`new`, `in-progress`,
`handled`, `ignored`, `failed`), `room_id`, `limit` (1..200, default 50), `before_id`
(`in-<n>` only). 200; 422 `invalid_request`.

    curl -s 'http://localhost:8000/matrix/api/v1/messages?status=failed'

## Agent API

The agent listener of the `api` subcommand, called by agents-orchestrator on behalf of a session
leader (the `orchestrator_chat_*` tools). With `MATRIX_AGENT_TOKEN_SHA256` set, the `api`
subcommand also binds `MATRIX_AGENT_PORT`, which serves only `/chat/v1/*`, `/healthz` and
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
<slug>` (stored with the send; may be absent). The agent's own token never reaches the
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
body (`422 invalid_request`, `422 invalid_key`), references (`422 bad_reference`), room choice
(`422 room_required`), (a) (`403 room_not_allowed`), (b) (`409 room_bound`, `403
room_not_authorised`, `409 session_bound`, in that order), duplicate (`202` with the existing
send), rate (`429 rate_limited`). The (b) check, the duplicate lookup and the rate count are
advisory: they are not atomic with the insert, and the bot's claim re-checks (b), refusing a send
that lost a race with reason `room_bound`, `session_bound` or `room_not_authorised`.

Duplicate rule: a request identical to a send of the same session, room and kind, with the same
`text` (message) or `key` (reaction) and the same `reply_to` (a reaction's `to`), compared
exactly, whose status is `queued`, `sending` or `sent` and that was created in the last 60 s,
answers `202` with the existing send's id and current status and stores nothing, so a retry after
a timeout never posts twice. A `failed` or `refused` send is not a duplicate. A duplicate is not
counted by the rate limit and is answered even at the limit.

Rate: at most `MATRIX_AGENT_RATE_PER_MINUTE` sends (messages and reactions together, of any
status) per session in the last 60 s.

Errors are `{"error": {"code": "...", "message": "..."}}`. Codes:

| Code | Status | Meaning |
|---|---|---|
| `unauthorized` | 401 | the bearer token is missing or wrong, or `X-Orch-Session` is not a uuid |
| `not_found` | 404 | the id is not one of this session's sends, or the path does not exist |
| `invalid_request` | 422 | a body field failed validation: `text` empty or over 16000 characters, a wrong type, an unknown field |
| `invalid_key` | 422 | the reaction `key` is not 1 to 16 code points, or has ASCII, whitespace or control characters |
| `bad_reference` | 422 | `reply_to` or `to` is not an `m<n>` or `s<n>` this session can reference: unknown, another session's send, a send not yet `sent`, a reaction, of another room than the given `room`, or a raw event id |
| `room_required` | 422 | no `room`, no `reply_to`, and the session is bound to no room |
| `room_not_allowed` | 403 | the room is unknown or not policy-allowed for agent sends (both answer the same) |
| `room_bound` | 409 | the room is bound to (or reserved by) another session |
| `room_not_authorised` | 403 | the room is unbound and not flagged `agents_may_initiate` |
| `session_bound` | 409 | the room is unbound and flagged, but the session is already bound to another room |
| `rate_limited` | 429 | the session reached `MATRIX_AGENT_RATE_PER_MINUTE` sends in the last 60 s |
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
422 `invalid_request`, `bad_reference` or `room_required`; 403 `room_not_allowed` or
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
(`PUT /matrix/api/v1/settings`); an allowed room with no preset is ignored with `no_preset`.

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
