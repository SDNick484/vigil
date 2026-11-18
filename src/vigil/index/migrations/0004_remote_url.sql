-- The Claude Remote Control link of each session (newest `bridge_status` record in its
-- transcript) and when that record was written, so a link left over from an earlier run of the
-- session can be told apart from the current one.

ALTER TABLE sessions ADD COLUMN remote_url TEXT;
ALTER TABLE sessions ADD COLUMN remote_url_at TEXT;

-- Transcripts indexed so far were not read for the link: forget how far each was read, so the
-- next scan parses them again from the start (the facts it derives are otherwise unchanged).
DELETE FROM file_state;
