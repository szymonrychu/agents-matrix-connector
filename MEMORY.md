# agents-matrix-connector memory

Lessons that outlive a session. Newest first.

## 2026-10-25 decisions

- Bot user with an access token, not an appservice: an appservice needs homeserver
  registration (a registration file and a restart of Synapse) and the bridged chats are
  already visible to an ordinary user that is joined to them. A bot user needs only a token.
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

