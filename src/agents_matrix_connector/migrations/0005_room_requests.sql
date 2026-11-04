-- Room requests: an agent asks to open a DM, invite a user or accept a Messenger message
-- request. The api inserts rows and moves pending_confirmation to confirmed or rejected (only
-- through the internal confirm route, with the taskboard decision the orchestrator attached);
-- the bot moves resolving to pending_confirmation, candidates or failed, and confirmed to
-- working to done or failed. Every change is a status-guarded update. Rows are the audit trail.

CREATE TABLE room_requests (
    id             bigserial PRIMARY KEY,
    action         text NOT NULL CHECK (action IN ('open', 'invite', 'accept')),
    target_kind    text NULL CHECK (target_kind IN ('matrix', 'facebook', 'name')),
    target         text NULL,
    display_name   text NULL,
    room_id        text NULL,
    candidates     jsonb NULL,
    task_key       text NULL,
    decision_id    bigint NULL,
    requested_by   uuid NULL,
    requested_slug text NULL,
    status         text NOT NULL CHECK (status IN
                       ('resolving', 'candidates', 'pending_confirmation', 'confirmed',
                        'working', 'done', 'failed', 'rejected')),
    reason         text NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    confirmed_at   timestamptz NULL,
    done_at        timestamptz NULL
);
CREATE INDEX room_requests_work_idx ON room_requests (id) WHERE status IN ('resolving', 'confirmed');
CREATE INDEX room_requests_room_idx ON room_requests (room_id);
CREATE INDEX room_requests_created_idx ON room_requests (created_at);

COMMENT ON TABLE room_requests IS
    'queue: inserted by api; api confirms or rejects, bot resolves and executes';
