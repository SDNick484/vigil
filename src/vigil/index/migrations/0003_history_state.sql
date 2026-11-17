-- Where shell history import stopped in each history file, so a later import reads only what was
-- appended. Same shape as file_state, which does this for transcripts.

CREATE TABLE history_state (
    path     TEXT PRIMARY KEY,
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    offset   INTEGER NOT NULL
);
