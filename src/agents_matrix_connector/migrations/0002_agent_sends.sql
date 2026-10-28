-- Agent sends: the sends queue, the outbound columns for agent messages and reactions, the
-- per-room agents_may_initiate flag and the hard 1:1 room-session binding (room_bindings).
-- sends is a queue table whose rows change status only through status-guarded UPDATEs.

CREATE TABLE sends (
    id           bigserial PRIMARY KEY,
    session_id   uuid NOT NULL,
    slug         text NULL,
    kind         text NOT NULL CHECK (kind IN ('message', 'reaction')),
    room_id      text NOT NULL,
    text         text NULL,
    key          text NULL,
    reply_to     text NULL,
    ref_event_id text NULL,
    event_id     text NULL,
    status       text NOT NULL CHECK (status IN
                     ('queued', 'sending', 'sent', 'failed', 'refused')),
    reason       text NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    claimed_at   timestamptz NULL,
    sent_at      timestamptz NULL
);
CREATE INDEX sends_queued_idx ON sends (id) WHERE status = 'queued';
CREATE INDEX sends_session_created_idx ON sends (session_id, created_at);
CREATE INDEX sends_room_status_idx ON sends (room_id, status);

ALTER TABLE outbound
    ADD COLUMN send_id    bigint NULL,
    ADD COLUMN event_type text NOT NULL DEFAULT 'm.room.message',
    ADD COLUMN relates_to jsonb NULL,
    DROP CONSTRAINT outbound_kind_check,
    ADD CONSTRAINT outbound_kind_check CHECK (kind IN ('reply', 'notice', 'agent', 'reaction'));

ALTER TABLE room_settings ADD COLUMN agents_may_initiate boolean NOT NULL DEFAULT false;

ALTER TABLE runs
    ALTER COLUMN preset_id DROP NOT NULL,
    ADD CONSTRAINT runs_first_preset_check CHECK (kind <> 'first' OR preset_id IS NOT NULL);
CREATE INDEX runs_session_room_idx ON runs (session_id, room_id);

-- A binding is active while released_at is NULL: at most one per room and one per session.
CREATE TABLE room_bindings (
    id             bigserial PRIMARY KEY,
    room_id        text NOT NULL,
    session_id     uuid NULL,
    slug           text NULL,
    origin         text NOT NULL CHECK (origin IN ('inbound', 'agent')),
    run_id         bigint NULL,
    send_id        bigint NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    released_at    timestamptz NULL,
    release_reason text NULL,
    CONSTRAINT room_bindings_inbound_check CHECK (origin <> 'inbound' OR run_id IS NOT NULL),
    CONSTRAINT room_bindings_agent_check
        CHECK (origin <> 'agent' OR (session_id IS NOT NULL AND send_id IS NOT NULL))
);
CREATE UNIQUE INDEX room_bindings_active_room_idx ON room_bindings (room_id)
    WHERE released_at IS NULL;
CREATE UNIQUE INDEX room_bindings_active_session_idx ON room_bindings (session_id)
    WHERE released_at IS NULL AND session_id IS NOT NULL;

-- Backfill: existing chats keep their session. A room whose newest run is an in-progress first
-- run without a session is reserved for that run; otherwise the room binds the session of its
-- newest run that has one, and a session newest in several rooms binds only the room of its
-- newest run. The slug is the session's, the slug of the first run that created it (a follow
-- run's slug names the run, not the session); NULL when that run is gone.
WITH newest AS (
    SELECT DISTINCT ON (room_id) room_id, id, kind, status, session_id
    FROM runs
    ORDER BY room_id, id DESC
),
reserved AS (
    SELECT room_id, id AS run_id
    FROM newest
    WHERE kind = 'first' AND status = 'in-progress' AND session_id IS NULL
),
room_session AS (
    SELECT DISTINCT ON (room_id) room_id, id AS run_id, session_id
    FROM runs
    WHERE session_id IS NOT NULL AND room_id NOT IN (SELECT room_id FROM reserved)
    ORDER BY room_id, id DESC
),
session_room AS (
    SELECT DISTINCT ON (session_id) room_id, run_id, session_id
    FROM room_session
    ORDER BY session_id, run_id DESC
),
session_slug AS (
    SELECT DISTINCT ON (session_id) session_id, slug
    FROM runs
    WHERE kind = 'first' AND session_id IS NOT NULL
    ORDER BY session_id, id
)
INSERT INTO room_bindings (room_id, session_id, slug, origin, run_id)
SELECT room_id, NULL, NULL, 'inbound', run_id FROM reserved
UNION ALL
SELECT b.room_id, b.session_id, s.slug, 'inbound', b.run_id
FROM session_room b LEFT JOIN session_slug s USING (session_id);

COMMENT ON TABLE sends IS 'queue: inserted by api, transitions by bot worker';
COMMENT ON TABLE room_bindings IS 'writer: bot worker';
