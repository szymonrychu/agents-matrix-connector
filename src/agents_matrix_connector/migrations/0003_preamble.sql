-- Editable first-prompt preamble (a Jinja template): NULL means "use the next level up", the
-- rule's, then the global one, then the built-in default. inbound.rule_id records which rule
-- allowed the message (no foreign key: a deleted rule just stops contributing its preamble).

ALTER TABLE settings ADD COLUMN preamble text NULL;
ALTER TABLE access_rules ADD COLUMN preamble text NULL;
ALTER TABLE inbound ADD COLUMN rule_id bigint NULL;
