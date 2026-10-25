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
| `MATRIX_PUPPET_PATTERN` | ^@facebook_(?P<id>[0-9]+): | bot | regex for bridged puppet user ids, needs a named group `id` |
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
`effective_trigger`, `session` (`id`, `slug`, `status`, or null) and `queued` (messages waiting); 422 `invalid_request`.

    curl -s 'http://localhost:8000/matrix/api/v1/rooms?kind=dm&access=allowed'

### `GET /matrix/api/v1/rooms/{room_id}`

One room with its `members` (`user_id`, `display_name`, `facebook_id`, `membership`, `access`). 200; 404 `not_found`.

    curl -s 'http://localhost:8000/matrix/api/v1/rooms/!abc:server'

### `PUT /matrix/api/v1/rooms/{room_id}/settings`

Body `{"preset_id": uuid|null, "trigger": "all"|"mention"|null}`: the room's preset and
trigger override (null clears). 200 room; 404 `not_found`; 422 `unknown_preset` or
`invalid_request`; 503 `unavailable`; 502 `orchestrator_error`.

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
