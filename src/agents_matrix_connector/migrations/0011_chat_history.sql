-- Chat history: inbound reply, source, edit and redaction fields, the attachment request
-- queue, the one-time history crawl queue and the ordering indexes of the history feed.
-- The sync filter gains m.room.redaction, so the stored filter id is cleared: the bot creates
-- a fresh filter on its next start (a stored one would keep the old event types forever).

ALTER TABLE inbound
    ADD COLUMN reply_to_event_id text NULL,
    ADD COLUMN source            text NOT NULL DEFAULT 'sync'
                                     CHECK (source IN ('sync', 'history')),
    ADD COLUMN edited_at         timestamptz NULL,
    ADD COLUMN redacted          boolean NOT NULL DEFAULT false;

COMMENT ON TABLE inbound IS
    'queue: inserted by bot sync and bot history crawler, edits and redactions by bot sync, status by bot worker';

CREATE INDEX inbound_room_at_idx ON inbound (room_id, (COALESCE(origin_ts, received_at)), id);
CREATE INDEX sends_room_created_idx ON sends (room_id, created_at, id);
CREATE INDEX outbound_room_created_idx ON outbound (room_id, created_at, id);

CREATE TABLE attachment_requests (
    id         bigserial PRIMARY KEY,
    session_id uuid NOT NULL,
    slug       text NULL,
    room_id    text NOT NULL,
    inbound_id bigint NOT NULL,
    status     text NOT NULL CHECK (status IN ('queued', 'fetching', 'sent', 'failed', 'refused')),
    reason     text NULL,
    message_id text NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    claimed_at timestamptz NULL,
    done_at    timestamptz NULL
);
CREATE INDEX attachment_requests_queued_idx ON attachment_requests (id) WHERE status = 'queued';
CREATE INDEX attachment_requests_room_created_idx ON attachment_requests (room_id, created_at);
CREATE UNIQUE INDEX attachment_requests_inflight_idx ON attachment_requests (inbound_id)
    WHERE status IN ('queued', 'fetching');
COMMENT ON TABLE attachment_requests IS
    'queue: inserted by api, transitions by bot attachment fetcher';

CREATE TABLE history_crawls (
    id         bigserial PRIMARY KEY,
    room_id    text NOT NULL UNIQUE,
    status     text NOT NULL CHECK (status IN ('queued', 'crawling', 'done', 'failed')),
    reason     text NULL,
    token      text NULL,
    events     integer NOT NULL DEFAULT 0,
    inserted   integer NOT NULL DEFAULT 0,
    not_before timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    done_at    timestamptz NULL
);
COMMENT ON TABLE history_crawls IS 'queue: inserted and transitioned by bot history crawler';

UPDATE sync_state SET filter_id = NULL;
