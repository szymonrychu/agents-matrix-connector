-- Approvals: the connector's own record of approval messages it posted, the digests that
-- group them and the answers it read from the approvals room. The connector is the only writer.

CREATE TABLE approval_digests (
    id           bigserial PRIMARY KEY,
    room_id      text NOT NULL,
    event_id     text NOT NULL UNIQUE,
    decision_ids bigint[] NOT NULL CHECK (cardinality(decision_ids) BETWEEN 2 AND 50),
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE approval_messages (
    decision_id bigint PRIMARY KEY,
    room_id     text NOT NULL,
    event_id    text NOT NULL UNIQUE,
    digest_id   bigint NULL REFERENCES approval_digests (id),
    status      text NOT NULL CHECK (status IN ('posted', 'answered', 'closed')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE approval_answers (
    event_id        text PRIMARY KEY,
    room_id         text NOT NULL,
    sender          text NOT NULL,
    target_event_id text NOT NULL,
    verdict         text NOT NULL CHECK (verdict IN ('go', 'no_go')),
    status          text NOT NULL CHECK (status IN ('pending', 'done')),
    results         jsonb NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    done_at         timestamptz NULL
);
CREATE INDEX approval_answers_pending_idx ON approval_answers (created_at) WHERE status = 'pending';

COMMENT ON TABLE approval_digests IS 'writer: bot approvals';
COMMENT ON TABLE approval_messages IS 'writer: bot approvals';
COMMENT ON TABLE approval_answers IS 'queue: inserted and transitioned by bot approvals';
