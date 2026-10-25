-- agents-matrix-connector schema. Every table has exactly one writer (see COMMENT ON TABLE);
-- inbound is a queue table whose rows change status only through status-guarded UPDATEs.

CREATE TABLE sync_state (
    id           smallint PRIMARY KEY CHECK (id = 1),
    next_batch   text NULL,
    filter_id    text NULL,
    last_sync_at timestamptz NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);
INSERT INTO sync_state (id) VALUES (1);

CREATE TABLE rooms (
    room_id         text PRIMARY KEY,
    kind            text NULL CHECK (kind IN ('dm', 'group', 'matrix', 'infrastructure')),
    name            text NULL,
    alias           text NULL,
    encrypted       boolean NOT NULL DEFAULT false,
    message_request boolean NOT NULL DEFAULT false,
    membership      text NOT NULL CHECK (membership IN ('invite', 'join', 'leave')),
    inviter         text NULL,
    member_count    int NOT NULL DEFAULT 0,
    discovered_at   timestamptz NOT NULL DEFAULT now(),
    last_event_at   timestamptz NULL,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    user_id       text PRIMARY KEY,
    display_name  text NULL,
    facebook_id   text NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX users_facebook_id_idx ON users (facebook_id);

CREATE TABLE room_members (
    room_id      text NOT NULL REFERENCES rooms (room_id) ON DELETE CASCADE,
    user_id      text NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    membership   text NOT NULL,
    display_name text NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (room_id, user_id)
);

CREATE TABLE access_rules (
    id         bigserial PRIMARY KEY,
    list       text NOT NULL CHECK (list IN ('allow', 'deny')),
    kind       text NOT NULL CHECK (kind IN ('user', 'facebook', 'room', 'alias', 'display_name')),
    value      text NOT NULL CHECK (length(value) BETWEEN 1 AND 255),
    note       text NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (list, kind, value),
    CHECK (NOT (list = 'allow' AND kind = 'display_name'))
);

CREATE TABLE room_settings (
    room_id    text PRIMARY KEY,
    preset_id  uuid NULL,
    trigger    text NULL CHECK (trigger IN ('all', 'mention')),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE settings (
    id                smallint PRIMARY KEY CHECK (id = 1),
    default_preset_id uuid NULL,
    updated_at        timestamptz NOT NULL DEFAULT now()
);
INSERT INTO settings (id) VALUES (1);

CREATE TABLE inbound (
    id          bigserial PRIMARY KEY,
    event_id    text NOT NULL UNIQUE,
    room_id     text NOT NULL,
    sender      text NOT NULL,
    sender_name text NULL,
    body        text NULL,
    mentioned   boolean NOT NULL DEFAULT false,
    origin_ts   timestamptz NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    status      text NOT NULL CHECK (status IN
                    ('new', 'in-progress', 'handled', 'ignored', 'failed')),
    reason      text NULL,
    run_id      bigint NULL
);
CREATE INDEX inbound_new_idx ON inbound (room_id, id) WHERE status = 'new';
CREATE INDEX inbound_room_idx ON inbound (room_id, id);

CREATE TABLE runs (
    id          bigserial PRIMARY KEY,
    room_id     text NOT NULL,
    preset_id   uuid NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('first', 'follow')),
    slug        text NULL,
    session_id  uuid NULL,
    message_id  text NULL,
    status      text NOT NULL CHECK (status IN ('in-progress', 'handled', 'failed')),
    reason      text NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz NULL
);
CREATE INDEX runs_in_progress_idx ON runs (id) WHERE status = 'in-progress';
CREATE INDEX runs_room_idx ON runs (room_id, id);

CREATE TABLE outbound (
    id         bigserial PRIMARY KEY,
    room_id    text NOT NULL,
    run_id     bigint NULL,
    kind       text NOT NULL CHECK (kind IN ('reply', 'notice')),
    body       text NOT NULL,
    txn_id     text NOT NULL UNIQUE,
    event_id   text NULL,
    status     text NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
    reason     text NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at    timestamptz NULL
);
CREATE INDEX outbound_pending_idx ON outbound (id) WHERE status = 'pending';

COMMENT ON TABLE sync_state IS 'writer: bot sync';
COMMENT ON TABLE rooms IS 'writer: bot sync';
COMMENT ON TABLE users IS 'writer: bot sync';
COMMENT ON TABLE room_members IS 'writer: bot sync';
COMMENT ON TABLE access_rules IS 'writer: api';
COMMENT ON TABLE room_settings IS 'writer: api';
COMMENT ON TABLE settings IS 'writer: api';
COMMENT ON TABLE inbound IS 'queue: inserted by bot sync, transitions by bot worker';
COMMENT ON TABLE runs IS 'writer: bot worker';
COMMENT ON TABLE outbound IS 'writer: bot worker';
