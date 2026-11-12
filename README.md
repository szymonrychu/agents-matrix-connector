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
| `MATRIX_INBOUND_MEDIA_MAX_BYTES` | 26214400 | bot | max bytes downloaded for one inbound attachment (>= 1) |
| `MATRIX_INBOUND_IMAGE_MAX_EDGE` | 2576 | bot | long edge in pixels inbound images are downscaled to (>= 1) |
| `MATRIX_INBOUND_IMAGE_MAX_PIXELS` | 40000000 | bot | max width times height of an inbound image; larger is refused before decoding (>= 1) |
| `MATRIX_INBOUND_ATTACH_MAX_BYTES` | 6291456 | bot | max bytes of one prepared attachment handed to the agent (>= 1) |
| `MATRIX_INBOUND_ATTACH_MAX_TOTAL_BYTES` | 8388608 | bot | max bytes of all attachments of one message handed to the agent (>= 1) |
| `MATRIX_INBOUND_ATTACH_MAX_COUNT` | 8 | bot | max attachments of one message handed to the agent (>= 1) |
| `MATRIX_MEDIA_MENTION_WINDOW_SECONDS` | 120 | bot | same-sender media sent this many seconds before or after a mention joins that message (>= 1) |
| `MATRIX_AGENT_FILES_PER_SEND` | 4 | api | max files in one agent send (>= 1) |
| `MATRIX_AGENT_IMAGE_MAX_BYTES` | 8000000 | api | max bytes of one outbound image, decimal to stay under the bridge's 8 MB (>= 1) |
| `MATRIX_AGENT_FILE_MAX_BYTES` | 25000000 | api | max bytes of one other outbound file, decimal to stay under the bridge's 25 MB (>= 1) |
| `MATRIX_AGENT_ROOM_FILE_BYTES_PER_HOUR` | 209715200 | api | max file bytes agent sends may put into one room in the last hour, all sessions together (>= 1) |
| `MATRIX_ANONYMIZE_ADMINS` | empty | api, bot | Matrix user ids or numeric Facebook ids, comma separated, shown as `admin-<sha>` instead of `user-<sha>` in rooms with `anonymize_names` |
| `MATRIX_ROOM_REQUESTS_PER_HOUR` | 10 | api | max open and invite room requests created in the last hour, all sessions together (>= 1); more answer 429 `rate_limited` |
| `MATRIX_ROOM_NOTES_MAX_CHARS` | 8000 | api | max characters of one room's notes (100 to 32000); a longer PUT is 413 `notes_too_large` |
| `MATRIX_ROOM_SKILL_MAX_CHARS` | 8000 | api | max characters of one skill body (100 to 32000); longer is 413 `skill_too_large` |
| `MATRIX_ROOM_SKILLS_MAX` | 20 | api | max skills per room (1 to 100); another create is 409 `skill_limit` |
| `MATRIX_ROOM_MEMORY_RULE` | empty | worker and api | replaces the built-in room memory rule in first prompts and the notes GET; empty keeps the built-in |
| `MATRIX_ACCESS_TOKEN` | none | bot | Matrix access token (required unless the file is set) |
| `MATRIX_ACCESS_TOKEN_FILE` | empty | bot | file holding the Matrix access token; wins over the variable, stripped |
| `MATRIX_BRIDGE_PROVISIONING_URL` | empty | bot | mautrix-meta provisioning base, e.g. `http://mautrix-meta.messaging.svc.cluster.local:29328/_matrix/provision`, called with the bot's own Matrix token; empty fails Facebook targets with `bridge_unconfigured` |

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

- `not_mentioned`: ignored, a group message that did not mention the bot (a tag of the bot, a
  reply to its message or one of the room's `agent_aliases`, see "Mentions")
- `queue_full`: ignored, the room already has `MATRIX_MAX_QUEUED_PER_ROOM` queued messages
- `unsupported_content`: ignored, neither a text nor a media message (for example a location,
  or media with an encrypted `file` instead of a plain `url`)
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
- `pseudonym_disclosed`: failed, in a room with `anonymize_names` the turn's final answer
  contained a pseudonym (`user-<sha>`, `admin-<sha>`): nothing is posted to the room and the
  run's session is told why, so the agent can rephrase and send with `orchestrator_chat_send`
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
`effective_trigger`, `agents_may_initiate`, `anonymize_names`, `agents_may_send_files` (false
when the room has no settings) and `agent_aliases` (`[]` without settings), `session`
(the latest run: `id`, `slug`, `status`, or null), `binding` (the room's active session binding:
`session_id`, `slug`, `origin` `inbound` or `agent`, `since`; `session_id` null while a first run
reserves the room; null when the room is unbound), `queued` (messages waiting),
`effective_preset_id` (the room's preset, else the default preset) and `pending`: `allowed`
(the room's access state is allowed), `invited` (membership `invite`: the bot has not joined),
`inviter`, `message_request` (a Messenger message request), `denied_count` and
`last_denied_at` (messages the policy refused, `not_allowed` or `denied: <kind>`) and
`denied_senders` (`user_id`, `display_name`, `facebook_id`; never message bodies); 422
`invalid_request`. The binding is read only: no route writes, releases or moves it.

    curl -s 'http://localhost:8000/matrix/api/v1/rooms?kind=dm&access=allowed'

### `GET /matrix/api/v1/rooms/{room_id}`

One room (the fields of the list, `pending` included) with its `members` (`user_id`,
`display_name`, `facebook_id`, `membership`, `access`). 200; 404 `not_found`.

    curl -s 'http://localhost:8000/matrix/api/v1/rooms/!abc:server'

### `PUT /matrix/api/v1/rooms/{room_id}/settings`

Body `{"preset_id": uuid|null, "trigger": "all"|"mention"|null, "agents_may_initiate":
true|false|null, "anonymize_names": true|false|null, "agent_aliases": [string]|null,
"agents_may_send_files": true|false|null}`: the room's preset and trigger override
(null or omitted clears), whether agents may start a chat in the room while it is unbound
(`agents_may_initiate`, off by default), whether the chat agent sees pseudonyms instead of
the people's names (`anonymize_names`, off by default, see "Name anonymization") and whether
agents may send workdir files to the room (`agents_may_send_files`, off by default, see "Agent
files"). An omitted or null flag keeps its stored value (false for a room without settings), so a
save of the preset or trigger never changes any of them. `agent_aliases` (at most 10 names of up to 64 characters, see
"Mentions") are cleaned (trimmed, one leading `@` dropped, spaces collapsed, case-insensitive
duplicates dropped); null or omitted keeps the stored list, `[]` clears it, an invalid one is 422
`invalid_request`. 200 room; 404
`not_found`; 422 `unknown_preset` or `invalid_request`; 503 `unavailable`; 502
`orchestrator_error`.

    curl -s -X PUT -H 'content-type: application/json' -d '{"trigger": "all"}' \
      'http://localhost:8000/matrix/api/v1/rooms/!abc:server/settings'

### `POST /matrix/api/v1/rooms/{room_id}/allow`

Allow and configure a room in one call. Body as `PUT .../settings` (`preset_id`, `trigger`
stored as sent, `agents_may_initiate` null keeps the stored flag, `agent_aliases` null keeps the stored list).
One transaction stores the
settings and an `allow/room` rule for the room id (an existing one is reused, so a second call
adds nothing) and, when the room is a Messenger message request or still at membership
`invite`, queues an `accept` room request (status `confirmed`) unless one is open. The bot joins
an invited room on its next sync because the rule now exists, then accepts the message request
(`com.beeper.accept_message_request`). The rule note is `allowed by agent <slug>` with an
`X-Orch-Slug` header, else `allowed via api`. 200: the room as `GET /rooms/{room_id}` plus
`rule_id` and `accept_request_id` (null when nothing was queued); 404 `not_found`; 422
`unknown_preset`; 503 `unavailable`; 502 `orchestrator_error`.

    curl -s -X POST 'http://localhost:8000/matrix/api/v1/rooms/!abc:server/allow' \
      -H 'Content-Type: application/json' -d '{"trigger": "all"}'

### `GET /matrix/api/v1/rooms/{room_id}/messages`

The room's merged inbound and outbound feed, newest first. Query: `limit` (1..200, default
50), `before_id` (a message id like `in-12` or `out-3`, from the previous page). 200; 404
`not_found`; 422 `invalid_request`. An inbound message with media carries `media_kind`,
`media_mime` and `media_size` (declared by the sender), `media_status` (`attached`,
`not_attached`, or null before the worker handled it) and `media_reason` (see "Inbound photos
and files"); the media url and the original file name are never shown.

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
invalid_slug`). A message with `files` also needs `X-Orch-Agent: <agent uuid>`, the calling
agent whose workdir holds the files (missing or malformed: `422 files_unavailable`; ignored
without files). The agent's own token never reaches the
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
room_not_authorised`, `409 session_bound`, in that order), then for a message with `files`: the
room's `agents_may_send_files` (`403 files_not_allowed`), pseudonyms in `text` and file names
(`422 pseudonym_disclosed`), path rules (`422 invalid_file_path`), a pre-check of rate and room
bytes (`429 rate_limited`, `429 room_file_bytes_limit`), the download of each file (`422
files_unavailable`, `404 file_not_found`, `413 file_too_large`, `503 unavailable`); then
duplicate (`202` with the existing send), rate (`429 rate_limited`), room chunk cap (`429
room_chunk_limit`), room file bytes (`429 room_file_bytes_limit`). The (b) check is
advisory: the bot's claim re-checks it, refusing a send that lost a race with reason
`room_bound`, `session_bound` or `room_not_authorised`. The duplicate lookup, the rate count and
the room chunk count run under transaction-scoped advisory locks on the session and on the room,
so they are atomic with the insert and parallel requests cannot exceed either limit.

A reference (`reply_to` or `to`) to an `m<n>` of a room that is not the session's own bound room
whose room check fails answers `422 bad_reference`, the same as an unknown id, so a session
cannot probe which ids exist in other rooms. In the session's own room the specific code applies.

Duplicate rule: a request identical to a send of the same session, room and kind, with the same
`text` (message) or `key` (reaction), the same `reply_to` (a reaction's `to`) and the same
files (the ordered list of their sha256, so the same path with new content is a new send),
compared exactly, whose status is `queued`, `sending` or `sent` and that was created in the last 60 s,
answers `202` with the existing send's id and current status and stores nothing, so a retry after
a timeout never posts twice. A `failed` or `refused` send is not a duplicate. A duplicate is not
counted by the rate limit and is answered even at the limit.

Rate: at most `MATRIX_AGENT_RATE_PER_MINUTE` sends (messages and reactions together, of any
status) per session in the last 60 s.

Room chunk cap: at most `MATRIX_AGENT_ROOM_CHUNKS_PER_HOUR` Matrix events per room in the last
hour, summed over every session that sent there. A message counts one per chunk (its text split
at `MATRIX_REPLY_CHUNK_CHARS`, so one 16000-character message is 4 chunks at the default) plus
one per file, a reaction one; `refused` sends do not count. A send that would pass the cap is
refused whole.

Agent files: a message may carry `files`, at most `MATRIX_AGENT_FILES_PER_SEND` (4; the schema
caps the list at 4) relative paths in the calling agent's workdir, only in a room flagged
`agents_may_send_files` (off by default, set with the room settings route or the agents-web
switch). Path rules: relative, at most 512 characters, no NUL, no empty, `.` or `..` component,
no component starting with `.` except a leading `.attachments/` (files the agent received), and
no `.env`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*` or `credentials*` name; a refusal names
the index (`files[<i>]`), never the path. The api downloads each file through the orchestrator
at request time, holding no database connection, types it from its bytes (`m.image` for jpeg,
png, gif and webp, `m.video`, `m.audio`, else `m.file`), caps an image at
`MATRIX_AGENT_IMAGE_MAX_BYTES` (8000000) and any other file at `MATRIX_AGENT_FILE_MAX_BYTES`
(25000000), strips image metadata (EXIF, XMP, text chunks), and stores the bytes with the send
until it settles. Each file is one Matrix event after the text (no captions), named after the
path's basename (`[A-Za-z0-9._-]`, at most 128 characters); the quoted reply relation stays on
the first event. Room file bytes: at most `MATRIX_AGENT_ROOM_FILE_BYTES_PER_HOUR` per room in the
last hour, all sessions together, `refused` sends not counted. The bot re-checks the flag when it
claims the send (`refused`, `files_not_allowed`), uploads each file once (a retry reuses the
stored content uri), fails a file over the homeserver's `m.upload.size`
(`too_large_for_homeserver`) and clears the stored bytes once the send is `sent`, `failed` or
`refused`. One INFO line per accepted file names the session, send id, position, size, mime and
the first 12 hex of its sha256; never the path or the bytes.

Errors are `{"error": {"code": "...", "message": "..."}}`. Codes:

| Code | Status | Meaning |
|---|---|---|
| `unauthorized` | 401 | the bearer token is missing or wrong, or `X-Orch-Session` is not a uuid |
| `invalid_slug` | 400 | `X-Orch-Slug` is sent but is not a valid session slug |
| `not_found` | 404 | the id is not one of this session's sends, or the path does not exist |
| `invalid_request` | 422 | a body field failed validation: `text` empty or over 16000 characters, a wrong type, an unknown field |
| `invalid_text` | 422 | the message has no `files` and its `text` is missing or has no non-whitespace character, so it could never be delivered |
| `invalid_key` | 422 | the reaction `key` is not 1 to 16 code points, or has ASCII, whitespace or control characters |
| `bad_reference` | 422 | `reply_to` or `to` is not an `m<n>` or `s<n>` this session can reference: unknown, another session's send, a send not yet `sent`, a reaction, of another room than the given `room`, an `m<n>` whose room is refused to a session that is not bound to it, or a raw event id |
| `room_required` | 422 | no `room`, no `reply_to`, and the session is bound to no room |
| `room_not_allowed` | 403 | the room is unknown or not policy-allowed for agent sends (both answer the same) |
| `room_bound` | 409 | the room is bound to (or reserved by) another session; a room flagged `agents_may_initiate` with a session bound also carries `error.occupier`, that session's id (never for a reservation or an unflagged room), read by the orchestrator, which never passes it to an agent |
| `room_not_authorised` | 403 | the room is unbound and not flagged `agents_may_initiate` |
| `session_bound` | 409 | the room is unbound and flagged, but the session is already bound to another room |
| `rate_limited` | 429 | the session reached `MATRIX_AGENT_RATE_PER_MINUTE` sends in the last 60 s |
| `room_chunk_limit` | 429 | the send would take the room past `MATRIX_AGENT_ROOM_CHUNKS_PER_HOUR` chunks in the last hour |
| `files_not_allowed` | 403 | the message has `files` and the room is not flagged `agents_may_send_files` |
| `invalid_file_path` | 422 | a `files` entry breaks the path rules; the message names the index only |
| `files_unavailable` | 422 | `files` without a valid `X-Orch-Agent`, or the orchestrator refused the download (the agent is not usable), or an image could not be read |
| `file_not_found` | 404 | a `files` entry does not exist in the agent's workdir |
| `file_too_large` | 413 | a file is over `MATRIX_AGENT_IMAGE_MAX_BYTES` (images) or `MATRIX_AGENT_FILE_MAX_BYTES` (other files) |
| `room_file_bytes_limit` | 429 | the send would take the room past `MATRIX_AGENT_ROOM_FILE_BYTES_PER_HOUR` file bytes in the last hour |
| `pseudonym_disclosed` | 422 | the room has `anonymize_names` and the message `text` or a file name contains a pseudonym (`user-<sha>`, `admin-<sha>`); the message names the tokens found, refer to the person differently |
| `room_unbound` | 409 | the notes or skills routes need the session to be bound to a chat room, and it is bound to none |
| `version_conflict` | 409 | `expected_version` is not the stored version: re-read and merge, then write again |
| `skill_not_found` | 404 | the room has no skill of that name (also a PUT with `expected_version` above 0 on a missing one) |
| `skill_too_large` | 413 | the skill `body` is over `MATRIX_ROOM_SKILL_MAX_CHARS` characters (counted in code points) |
| `skill_limit` | 409 | creating the skill would pass `MATRIX_ROOM_SKILLS_MAX` skills in the room: delete one first |
| `notes_too_large` | 413 | the notes `body` is over `MATRIX_ROOM_NOTES_MAX_CHARS` characters (counted in code points) |
| `unavailable` | 503 | the database (or, for `files`, the orchestrator) is unavailable |

Send statuses: `queued` (accepted, waiting for the bot), `sending` (claimed by the bot, being
delivered), `sent` (every Matrix event delivered), `failed` (delivery failed), `refused` (the bot's
claim or send-time check refused it). Reasons:

- `room_bound`, `session_bound`, `room_not_authorised`: refused, (b) failed at the bot's claim
- `room_infrastructure`, `room_encrypted`, `denied: <kind>`, `not_allowed`: refused, (a)
  failed at the claim or before a Matrix event was sent
- `files_not_allowed`: refused, the room's `agents_may_send_files` was off when the bot claimed
  a send with files
- `too_large_for_homeserver`: failed, a file is over the homeserver's `m.upload.size`
- `file_unavailable`: failed, a file's bytes were gone before it was uploaded
- `forbidden`: failed, the homeserver refused the event or upload; another failure carries the
  homeserver's errcode, or `send_failed` (`upload_failed` for an upload without one)

### `GET /chat/v1/rooms`

The rooms this session may post to, exactly those passing (a) and (b): its bound room (`bound`
true) when it passes (a), else, while the session is bound to no room, every unbound flagged
room passing (a). The bound room comes first, then by `name` (code point order, rooms without a
name last), then `room_id`. A room with `anonymize_names` is named with its people's
pseudonyms (a DM after the other person, a group with member names replaced). 200
`{"rooms": [{"room_id", "name", "kind", "bound", "agents_may_initiate",
"agents_may_send_files"}]}`; 401 `unauthorized`.

    curl -s -H "Authorization: Bearer $TOKEN" -H "X-Orch-Session: $SESSION" \
      http://localhost:8001/chat/v1/rooms

### `POST /chat/v1/messages`

Body `{"text": str|null, "files": [str], "room": str|null, "reply_to": "m<n>"|"s<n>"|null}`:
`text` 1 to 16000 characters, stored unchanged and split into `MATRIX_REPLY_CHUNK_CHARS` chunks
by the bot; optional when `files` is given (see "Agent files"; needs `X-Orch-Agent`).
`room` is a Matrix room id; omitted, the room of `reply_to`, else the session's bound room. A
`reply_to` sends a quoted reply to that message. 202 `{"id": "s<n>", "status": "queued"}` (a
duplicate: the existing id and its status `queued`, `sending` or `sent`); 401 `unauthorized`;
422 `invalid_request`, `invalid_text`, `bad_reference`, `room_required` or `pseudonym_disclosed`
(checked after the room), `invalid_file_path` or `files_unavailable`; 403 `room_not_allowed`,
`room_not_authorised` or `files_not_allowed`; 404 `file_not_found`; 409 `room_bound` or
`session_bound`; 413 `file_too_large`; 429 `rate_limited`, `room_chunk_limit` or
`room_file_bytes_limit`; 503 `unavailable`.

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
"reason", "room_id", "created_at", "sent_at", "files"}`, `files` a list of `{"name", "mime",
"size", "status"}` in order (status `queued`, `uploaded`, `sent` or `failed`; empty without
files); 404 `not_found` for any other id, another
session's send included; 401 `unauthorized`.

    curl -s -H "Authorization: Bearer $TOKEN" -H "X-Orch-Session: $SESSION" \
      http://localhost:8001/chat/v1/messages/s7

### `GET /chat/v1/notes`

The notes of the room this session is bound to; the room is never a parameter, it is the
session's active binding, so a session reaches only its own room's notes and a rebound room
keeps them. 200 `{"room_id", "body", "version", "updated_at", "chars", "max_chars", "rule"}`
(no notes yet: `body` empty, `version` 0, `updated_at` null); `rule` is the room memory rule
(`MATRIX_ROOM_MEMORY_RULE`, else the built-in). 401 `unauthorized`; 409 `room_unbound`; 503
`unavailable`.

    curl -s -H "Authorization: Bearer $TOKEN" -H "X-Orch-Session: $SESSION" \
      http://localhost:8001/chat/v1/notes

### `PUT /chat/v1/notes`

Replace the whole notes body of the session's bound room. Body `{"body": str,
"expected_version": int}`: `expected_version` is the version read (0 when none exists yet); an
empty `body` forgets the notes. The body is capped at `MATRIX_ROOM_NOTES_MAX_CHARS` characters
(code points). 200 the same shape as the GET with the new version; 401 `unauthorized`; 409
`room_unbound` or `version_conflict` (the stored body is unchanged); 413 `notes_too_large`; 422
`invalid_request` (a missing or negative `expected_version`, an unknown field such as `room`);
503 `unavailable`. Notes are never written to a log.

    curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "X-Orch-Session: $SESSION" \
      -H 'content-type: application/json' \
      -d '{"body": "## Requests\n- example", "expected_version": 0}' \
      http://localhost:8001/chat/v1/notes

### `GET /chat/v1/skills`

The skills of the session's bound room, without bodies. 200 `{"room_id", "skills": [{"name",
"description", "version", "updated_at", "chars"}], "max_skills", "max_chars"}` ordered by name;
401 `unauthorized`; 409 `room_unbound`; 503 `unavailable`.

    curl -s -H "Authorization: Bearer $TOKEN" -H "X-Orch-Session: $SESSION" \
      http://localhost:8001/chat/v1/skills

### `GET /chat/v1/skills/{name}`

One skill with its body: 200 `{"room_id", "name", "description", "body", "version",
"updated_at", "chars", "max_chars"}`. `name` is 1 to 64 characters of lowercase letters, digits
and hyphens, starting with a letter or digit (a valid SKILL.md directory name), else 422
`invalid_request`. 404 `skill_not_found`; 401 `unauthorized`; 409 `room_unbound`; 503
`unavailable`.

    curl -s -H "Authorization: Bearer $TOKEN" -H "X-Orch-Session: $SESSION" \
      http://localhost:8001/chat/v1/skills/deck-tune

### `PUT /chat/v1/skills/{name}`

Create or replace a skill of the session's bound room. Body `{"description": str, "body": str,
"expected_version": int}`: `description` 1 to 1024 characters (when to use the skill), `body`
at least 1 character and at most `MATRIX_ROOM_SKILL_MAX_CHARS` code points, `expected_version`
0 to create or the version read to replace. 200 the same shape as the GET; 401 `unauthorized`;
404 `skill_not_found` (`expected_version` above 0 and no such skill); 409 `room_unbound`,
`version_conflict` or `skill_limit` (more than `MATRIX_ROOM_SKILLS_MAX` skills, checked under a
per-room lock so concurrent creates cannot pass it; replacing an existing skill is always
allowed); 413 `skill_too_large`; 422 `invalid_request`; 503 `unavailable`. Descriptions and
bodies are never written to a log.

    curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "X-Orch-Session: $SESSION" \
      -H 'content-type: application/json' \
      -d '{"description": "when to use it", "body": "1. step", "expected_version": 0}' \
      http://localhost:8001/chat/v1/skills/deck-tune

### `DELETE /chat/v1/skills/{name}`

Delete a skill of the session's bound room: 200 `{"room_id", "name", "deleted": true}`; 404
`skill_not_found`; 401 `unauthorized`; 409 `room_unbound`; 422 `invalid_request`; 503
`unavailable`.

    curl -s -X DELETE -H "Authorization: Bearer $TOKEN" -H "X-Orch-Session: $SESSION" \
      http://localhost:8001/chat/v1/skills/deck-tune

### Admin routes (`/chat/v1/admin`)

The same handlers as the UI app, behind the same bearer, for the orchestrator's superleader
relay. They take no `X-Orch-Session`. Request and response bodies, status codes and error codes
are those of the UI routes of the same name.

### `GET /chat/v1/admin/rooms`

As `GET /matrix/api/v1/rooms`.

### `GET /chat/v1/admin/rooms/{room_id}`

As `GET /matrix/api/v1/rooms/{room_id}`.

### `POST /chat/v1/admin/rooms/{room_id}/allow`

As `POST /matrix/api/v1/rooms/{room_id}/allow`.

### `PUT /chat/v1/admin/rooms/{room_id}/settings`

As `PUT /matrix/api/v1/rooms/{room_id}/settings` without `agents_may_send_files`: only the
web listener may set that flag, so an agent body carrying it is 422 `invalid_request` and a save
leaves the stored value unchanged.

### `GET /chat/v1/admin/users`

As `GET /matrix/api/v1/users`.

### `GET /chat/v1/admin/rules`

As `GET /matrix/api/v1/rules`.

### `POST /chat/v1/admin/rules`

As `POST /matrix/api/v1/rules`.

### `DELETE /chat/v1/admin/rules/{rule_id}`

As `DELETE /matrix/api/v1/rules/{rule_id}`.

### Room requests

An agent asks to open a chat (`open`) or invite a user into a joined Matrix room (`invite`); the
bot executes a request only after the user said yes on a taskboard decision. The orchestrator
enforces that: it asks the decision as the calling session, attaches it through the internal
decision route and records the answer through the internal confirm route, the only path to
`confirmed`. The api inserts and confirms rows; the bot resolves names and executes. Statuses:

| Status | Meaning |
|---|---|
| `resolving` | a name with no local match; the bot searches the bridge (`search_users`) |
| `candidates` | the name matched several people (`candidates`: `user_id`, `display_name`, `facebook_id`); re-ask with one id. Terminal |
| `pending_confirmation` | resolved to one id; waits for the user's decision |
| `confirmed` | the user said yes; the bot executes it next |
| `working` | the bot is executing it |
| `done` | executed; `room_id` is the room (`reason` `join_pending` when the bot still has to join, `not_a_message_request` for an accept with nothing to accept) |
| `failed` | `no_match`, `interrupted` (a Matrix-id open lost mid-call, never rerun), `bridge_unconfigured`, `bridge_error: <errcode>` or `matrix_error: <errcode>` |
| `rejected` | the user said no (`user_declined`) |

Execution: a Facebook target opens a DM through the mautrix-meta provisioning API (`create_dm`,
the bot then joins the portal room); a Matrix target gets a new direct room (`createRoom` with
`is_direct`, recorded in `m.direct`); an invite is a Matrix `/invite`; an `accept` (queued by
the allow route) sends `com.beeper.accept_message_request` once the room is joined. A new room
is not allowed by any of this: allow it with the allow route.

### `POST /chat/v1/admin/room-requests`

Body: `target` (1 to 500 characters: a Matrix user id, a bridge ghost `@facebook_<id>:<server>`,
a numeric Facebook id, a `facebook.com/profile.php?id=<n>` link on `facebook.com`, `www.`,
`m.`, `mobile.`, `web.` or `fb.com`, or a display name), `action` (`open`, default, or
`invite`), `room_id` (invite only: a joined Matrix room), `task_key` (`T-<n>`, the taskboard
task the decision is asked on). Optional `X-Orch-Session` and `X-Orch-Slug` are stored for the
audit trail. 201: the request (`id`, `action`, `status`, `target_kind`, `target`,
`display_name`, `room_id`, `candidates`, `task_key`, `decision_id`, `reason`, `created_at`,
`updated_at`). An id or link is `pending_confirmation`; a name with one local match too (its
Facebook id, else its Matrix id); several local matches `candidates`; none `resolving`. 422
`vanity_link_unresolvable` (a `facebook.com/<username>` link: pass the name or a profile.php
link), 422 `invalid_target` (empty, the bot, a self identity, a bridge bot, a foreign link),
422 `invite_not_possible`, 422 `invalid_request`, 429 `rate_limited` (more than
`MATRIX_ROOM_REQUESTS_PER_HOUR` open and invite requests in the last hour). No row is written
on a 4xx.

### `GET /chat/v1/admin/room-requests/{request_id}`

The request as above; 404 `not_found`.

### Internal routes (`/chat/v1/internal`)

For the orchestrator only, behind the same bearer, never relayed to agents.

### `PUT /chat/v1/internal/room-requests/{request_id}/decision`

Body `task_key`, `decision_id`: attach the taskboard decision, once, to a
`pending_confirmation` request. 200 the request; 404 `not_found`; 409 `not_pending`.

### `POST /chat/v1/internal/room-requests/{request_id}/confirm`

Body `decision_id`, `approved`: with the attached decision id, `confirmed` (approved) or
`rejected` (`user_declined`). 200 the request; 404 `not_found`; 409 `not_pending` (another
status, no decision attached, or a different decision id).

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

Invites are approved, not auto-joined: the bot joins a Matrix invite only when the room has an
`allow/room` rule (the allow route adds it), when the bot opened the room itself (an `open`
room request, such as the portal `create_dm` returned) or when it is the bridge management room
(from the provisioning `whoami`, read once at start when `MATRIX_BRIDGE_PROVISIONING_URL` is
set). Every other invite stays at membership `invite` and is listed as pending (`pending.invited`,
`pending.inviter`) until it is allowed; the next sync then joins it. A sender rule cannot apply
to an invite (its inviter is the bridge bot), so a new portal of an already allowed person also
waits for an allow call. Rooms already joined are unaffected.

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

### Rooms held for the Claude usage quota

When the orchestrator refuses a new session because the Claude plan usage is at its limit (409
`usage_quota`), the first run fails with `usage_quota`, its messages go back to `new`, the room's
reservation is released (`run_failed`) and the room is held (`room_holds`, written only by the
worker) until the window resets plus a 60 s margin (never earlier than a minute from now; five
minutes when the orchestrator names no reset). The worker skips a held room, so no session is
asked for, while the room's later messages stay `new`; when the hold has passed, one new first run
carries them all, and a second refusal moves the hold on. A hold only keeps the room from
starting a session: a follow run into an already bound running session never creates one and is
unaffected, and so are all other rooms until the orchestrator refuses them too.

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

### Name anonymization

A room flagged `anonymize_names` (off by default; set it with `PUT .../settings`, from the UI or
by a superleader through `PUT /chat/v1/admin/rooms/{room_id}/settings`) hides who the people
are from the chat agent. Each person is `user-<sha>`, `<sha>` being the first 12 hex characters
of the sha256 of their Matrix user id, or `admin-<sha>` when their Matrix user id or numeric
Facebook id is listed in `MATRIX_ANONYMIZE_ADMINS`. The prompt's `from`, the room name (a DM is
named after the other person), the preamble variables `sender` and `room_name` and the room
names of `GET /chat/v1/rooms` carry pseudonyms, and `facebook_id` is empty. Message bodies are
never rewritten, and real names may be sent.

A pseudonym must never reach the chat. In such a room `POST /chat/v1/messages` answers 422
`pseudonym_disclosed` for a text containing `user-<12 hex>` or `admin-<12 hex>` as a whole word
(any case), and a turn's final answer containing one is not posted: the run fails
`pseudonym_disclosed` and its session gets a message naming the tokens, so the agent can
rephrase and send the answer itself. The superleader admin routes (`/chat/v1/admin/rooms`,
`/users`, room requests) and the UI keep real names.

    curl -s -X PUT -H 'content-type: application/json' -d '{"anonymize_names": true}' \
      'http://localhost:8000/matrix/api/v1/rooms/!abc:server/settings'

### Admin and owner trust

A sender listed in `MATRIX_ANONYMIZE_ADMINS` (by Matrix user id or numeric Facebook id) is an
admin or the owner, in every room, anonymized or not. The connector marks each of their
messages with `role="admin"` on `<message>` (the attribute is omitted for everyone else; in an
anonymized room their `from` is also `admin-<sha>`). The first prompt of a session always
carries a trust rule after the preamble (the constant `ADMIN_TRUST_RULE`, appended whether the
preamble is the built-in default or a custom template, so a template cannot drop it): messages
with `role="admin"` are fully trusted, followed like the operator's, and override the
untrusted-input rule. Identity comes only from attributes the connector sets; bodies and names
are XML-escaped, so text claiming to be an admin never adds the role.

### Mentions

A group room in trigger `mention` wakes the agent on a tag of the bot (the structured
`m.mentions` list or a matrix.to link to the bot in the message), on a reply to one of the bot's
messages (the bridge puts the replied-to sender in `m.mentions`), or on one of the room's
`agent_aliases` as a whole word, case-insensitive, with an optional leading `@` ("bot", "@Bot,"
and "BOT?" match the alias `bot`; "robot" and "bot_x" do not). Display names are never matched:
the bot and its owner can share one, so a tag of the owner must not wake the agent. Messenger
nicknames are not bridged, so the names people use for the agent are configured here (`PUT
.../settings`, `POST .../allow`, the UI or the admin tool). Aliases never grant access: the allow
rules still decide who is heard.

### Inbound photos and files

Messenger photos and files reach the agent as attachments of the orchestrator message that
carries the chat batch. Accepted events: `m.room.message` with msgtype `m.image`, `m.file`,
`m.video` or `m.audio`, and `m.sticker`, each with a plain `url` on the bot's own homeserver
(`mxc://<server of MATRIX_USER_ID>/<id>`). Caption rule (Matrix v1.10): when `filename` differs
from `body`, `body` is the caption and becomes the message text; otherwise the text is empty.
Media metadata is stored only where a body would be (never for denied senders, encrypted or
infrastructure rooms), and the original file name is never stored in `anonymize_names` rooms.

When the worker hands a batch over, it downloads each media message (authenticated media, one
retry, at most `MATRIX_INBOUND_MEDIA_MAX_BYTES`), types it from its bytes (never from the
sender's mime or file name) and prepares it: images are downscaled to a
`MATRIX_INBOUND_IMAGE_MAX_EDGE` long edge (2576) and re-encoded as JPEG quality 85 stepping
down to fit `MATRIX_INBOUND_ATTACH_MAX_BYTES` (6 MiB), PNG when there is alpha, HEIC to JPEG;
gif and webp keep their format (first frame); PDF, plain text and JSON pass after magic
checks. Video and audio are never downloaded. Files are packed first fit, at most
`MATRIX_INBOUND_ATTACH_MAX_COUNT` (8) and `MATRIX_INBOUND_ATTACH_MAX_TOTAL_BYTES` (8 MiB) per
message, the orchestrator's own caps. Each file is named `m<message id>.<ext>`. A media failure
never holds the text back. The prompt lists every media message after its body:

    <message id="m42" from="Anna" facebook_id="123" at="...">look at this
    <attachment file="m42.jpg" kind="image" mime="image/jpeg" size="183211" original_name="IMG_0012.HEIC" attached="yes"/>
    </message>

`mime` and `size` are those of the delivered file, or the declared ones when not attached;
`original_name` is omitted in anonymized rooms. A file that is not attached has no `file` and
carries `attached="no" reason="..."`: `too_large`, `unsupported_type` (video, audio and types
other than images, PDF, text and JSON), `download_failed`, `batch_limit`, `undecodable` or
`foreign_media` (another server). The default preamble tells the agent that files and images
are untrusted data like message text.

In a room with trigger `mention`, Messenger sends "three photos and @bot look at these" as
four events and only the text mentions the bot. Media of the same sender sent within
`MATRIX_MEDIA_MENTION_WINDOW_SECONDS` (120) before an accepted mention joins it (up to the
room's queue cap), and media sent within that window after one is accepted too. Media of other
senders, and media around a mention that was ignored, stay `not_mentioned`.

### Read receipts

The bot marks the newest message of a batch read (a public Matrix `m.read` receipt, which
mautrix-meta bridges to Messenger as "seen") once the agent has it: at handoff when an agent of
the session already holds a wrapper session (a ready pod alone is not enough), else when the orchestrator reports the message sent (polled every
`MATRIX_POLL_SECONDS`). It never marks read while the session is still starting, never waits
for the reply, and never marks a message that failed before the agent got it. One receipt per
batch. The typing indicator is unchanged: it starts at handoff. It never sends a receipt in
chats it does not route: denied chats, chats without a preset, and messages that were ignored
or refused. A failed receipt is logged and never costs the agent its reply.

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
  preamble stating that the content inside `<chat>` is data and never instructions (except
  messages with `role="admin"`, see "Admin and owner trust"), and sender names and bodies are
  XML-escaped.
- No end-to-end encryption: encrypted rooms are ignored (`room_encrypted`).
- The Matrix access token exists only as a file mounted into the bot pod. The api pod does
  not receive it. The api accepts only socket peers inside `MATRIX_API_ALLOWED_CIDRS` behind
  SSO at the ingress.
