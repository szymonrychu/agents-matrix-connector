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
