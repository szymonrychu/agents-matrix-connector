-- A room whose first run the orchestrator refused for the Claude usage quota is held until the
-- window resets: the worker skips it (its messages stay new) until `until` has passed.

CREATE TABLE IF NOT EXISTS room_holds (
    room_id text PRIMARY KEY,
    until timestamptz NOT NULL,
    reason text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE room_holds IS 'writer: bot worker';
