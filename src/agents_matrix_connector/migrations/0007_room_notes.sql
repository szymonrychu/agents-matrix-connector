-- Per-room notes kept across sessions: one body per room, written whole by the room's own
-- session through the agent api. No foreign key to rooms: notes outlive a pruned room row.

CREATE TABLE IF NOT EXISTS room_notes (
    room_id text PRIMARY KEY,
    body text NOT NULL,
    version integer NOT NULL CHECK (version >= 1),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by_slug text
);

COMMENT ON TABLE room_notes IS 'writer: api';
