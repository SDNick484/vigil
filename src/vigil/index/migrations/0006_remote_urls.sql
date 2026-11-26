-- Every Remote Control link a session had (each valid `bridge_status` record of its transcript),
-- as a JSON array, oldest first, so a link from earlier in the session still identifies it.

ALTER TABLE sessions ADD COLUMN remote_urls TEXT NOT NULL DEFAULT '[]';

-- Transcripts indexed so far were not read for every link: forget how far each was read, so the
-- next scan parses them again from the start (the facts it derives are otherwise unchanged).
DELETE FROM file_state;
