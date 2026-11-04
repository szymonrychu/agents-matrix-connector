-- Per-room name anonymization: the chat agent sees user-<sha>/admin-<sha> instead of the
-- people's names and ids, and outbound text containing a pseudonym is rejected.

ALTER TABLE room_settings ADD COLUMN anonymize_names boolean NOT NULL DEFAULT false;
