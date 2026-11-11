-- Per-room skills kept across sessions: a named, described body the room's own session saves
-- through the agent api and writes out as a SKILL.md. No foreign key to rooms: skills outlive a
-- pruned room row.

CREATE TABLE IF NOT EXISTS room_skills (
    room_id text NOT NULL,
    name text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
    description text NOT NULL CHECK (char_length(description) BETWEEN 1 AND 1024),
    body text NOT NULL,
    version integer NOT NULL CHECK (version >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by_slug text,
    PRIMARY KEY (room_id, name)
);

COMMENT ON TABLE room_skills IS 'writer: api';
