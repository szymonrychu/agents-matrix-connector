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
