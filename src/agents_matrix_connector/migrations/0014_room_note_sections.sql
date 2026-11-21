-- Room notes in five named sections (S-79, decision 6). Each room_notes body is kept verbatim in
-- room_notes_migration_log, then split on its "## <Title>" headings: text before the first known
-- heading and under any other heading (that heading line kept) goes to work-in-progress, a
-- repeated heading appends, bodies are trimmed and empty sections are not inserted. Caps are not
-- enforced here: a migrated section may start over its cap and must fit on its next write.

CREATE TABLE room_note_sections (
    room_id text NOT NULL,
    section text NOT NULL CHECK (section IN (
        'preferences', 'requests', 'work-in-progress', 'provided-information',
        'repeatable-workflows'
    )),
    body text NOT NULL,
    version integer NOT NULL CHECK (version >= 1),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by_slug text,
    PRIMARY KEY (room_id, section)
);

COMMENT ON TABLE room_note_sections IS 'writer: api';

CREATE TABLE room_notes_migration_log (
    room_id text PRIMARY KEY,
    body text NOT NULL,
    version integer NOT NULL,
    updated_at timestamptz,
    updated_by_slug text,
    migrated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE room_notes_migration_log IS
    'writer: api (filled once by migration 0014: the exact room_notes rows before the split)';

INSERT INTO room_notes_migration_log (room_id, body, version, updated_at, updated_by_slug)
SELECT room_id, body, version, updated_at, updated_by_slug FROM room_notes;

DO $$
DECLARE
    note record;
    line text;
    heading text[];
    known text;
    current_key text;
    acc jsonb;
    part record;
    trimmed text;
BEGIN
    FOR note IN SELECT room_id, body, updated_at, updated_by_slug FROM room_notes LOOP
        acc := '{}'::jsonb;
        current_key := 'work-in-progress';
        FOREACH line IN ARRAY regexp_split_to_array(note.body, E'\n') LOOP
            heading := regexp_match(line, '^##\s+(.*\S)\s*$');
            known := CASE lower(heading[1])
                WHEN 'preferences' THEN 'preferences'
                WHEN 'requests' THEN 'requests'
                WHEN 'work in progress' THEN 'work-in-progress'
                WHEN 'provided information' THEN 'provided-information'
                WHEN 'repeatable workflows' THEN 'repeatable-workflows'
            END;
            IF known IS NOT NULL THEN
                current_key := known;
            ELSE
                IF heading IS NOT NULL THEN
                    current_key := 'work-in-progress';
                END IF;
                acc := jsonb_set(
                    acc,
                    ARRAY[current_key],
                    to_jsonb(coalesce((acc ->> current_key) || E'\n', '') || line)
                );
            END IF;
        END LOOP;
        FOR part IN SELECT key, value FROM jsonb_each_text(acc) LOOP
            trimmed := btrim(part.value, E' \t\r\n');
            IF trimmed <> '' THEN
                INSERT INTO room_note_sections
                    (room_id, section, body, version, updated_at, updated_by_slug)
                VALUES (note.room_id, part.key, trimmed, 1, note.updated_at, note.updated_by_slug);
            END IF;
        END LOOP;
    END LOOP;
END
$$;

DROP TABLE room_notes;
