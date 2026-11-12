-- Per-room names people use to call the agent (matched as whole words in the message body).
ALTER TABLE room_settings ADD COLUMN agent_aliases text[] NOT NULL DEFAULT '{}';
