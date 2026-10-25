# agents-matrix-connector

Component `agents-matrix-connector` (python) of the claude-code-tatara project. Its own tests, `MEMORY.md` and `ROADMAP.md` live here.

Run `mise run test`, `mise run lint` and `mise run fmt` from this directory.

## Commands

- `mise run test` runs the suite; DB tests need Docker (testcontainers).
- `mise run lint` runs ruff check and format check; `mise run fmt` formats.

## Highest-risk surface

Untrusted Messenger input reaching agents (a chat message is prompt input to an agent that can
act), and the Matrix access token, which is the identity of `@hermes:szymonrichert.pl`.
Default deny, identifier-only rules and the token living only as a file in the bot pod guard
both; do not weaken them.
