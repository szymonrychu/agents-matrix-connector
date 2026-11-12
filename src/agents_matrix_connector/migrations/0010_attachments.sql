-- Attachments: inbound media columns, the per-room agents_may_send_files switch, the
-- send_files queue (one row per file of an agent send) and outbound.file_position.
-- sends.text is already nullable (0002), so a files-only send needs no sends change.

ALTER TABLE inbound
    ADD COLUMN media_kind   text NULL CHECK (media_kind IN
                                ('image', 'video', 'audio', 'file', 'sticker')),
    ADD COLUMN media_url    text NULL,
    ADD COLUMN media_mime   text NULL,
    ADD COLUMN media_size   bigint NULL,
    ADD COLUMN media_name   text NULL,
    ADD COLUMN media_w      integer NULL,
    ADD COLUMN media_h      integer NULL,
    ADD COLUMN media_status text NULL CHECK (media_status IN ('attached', 'not_attached')),
    ADD COLUMN media_reason text NULL;

ALTER TABLE room_settings ADD COLUMN agents_may_send_files boolean NOT NULL DEFAULT false;

-- Files of an agent send: the api inserts rows with the send (bytes in data), the bot uploads
-- each file (content_uri) and clears data once the send settles.
CREATE TABLE send_files (
    send_id     bigint NOT NULL REFERENCES sends (id) ON DELETE CASCADE,
    position    smallint NOT NULL,
    path        text NOT NULL,
    name        text NOT NULL,
    mime        text NOT NULL,
    msgtype     text NOT NULL CHECK (msgtype IN ('m.image', 'm.video', 'm.audio', 'm.file')),
    size        bigint NOT NULL,
    sha256      text NOT NULL,
    w           integer NULL,
    h           integer NULL,
    data        bytea NULL,
    content_uri text NULL,
    PRIMARY KEY (send_id, position)
);
COMMENT ON TABLE send_files IS 'queue: inserted by api with its send, upload and clear by bot';

ALTER TABLE outbound ADD COLUMN file_position smallint NULL;
