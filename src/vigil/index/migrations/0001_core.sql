-- Core index: sessions, their models and features, scan state, vigil-only metadata, launches.
-- Timestamps are ISO 8601 strings in UTC with microseconds ("2026-11-17T15:07:10.230000+00:00"),
-- so they sort and compare as text.

CREATE TABLE sessions (
    session_id          TEXT PRIMARY KEY,
    cwd                 TEXT,
    git_branch          TEXT,
    version             TEXT,
    title               TEXT,
    first_prompt        TEXT,
    last_user           TEXT,
    last_assistant      TEXT,
    started_at          TEXT,
    last_activity_at    TEXT,
    message_count       INTEGER NOT NULL DEFAULT 0,
    previous_session_id TEXT,
    machine             TEXT NOT NULL,
    project_key         TEXT NOT NULL,
    project_label       TEXT NOT NULL,
    chain_id            TEXT NOT NULL,
    transcript_path     TEXT NOT NULL
);

CREATE INDEX sessions_last_activity ON sessions (last_activity_at);
CREATE INDEX sessions_project ON sessions (project_key);
CREATE INDEX sessions_chain ON sessions (chain_id);
CREATE INDEX sessions_transcript ON sessions (transcript_path);

CREATE TABLE session_models (
    session_id TEXT NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    PRIMARY KEY (session_id, model)
);

CREATE TABLE session_features (
    session_id TEXT NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL,
    count      INTEGER NOT NULL,
    PRIMARY KEY (session_id, kind, name)
);

CREATE TABLE file_state (
    path     TEXT PRIMARY KEY,
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    offset   INTEGER NOT NULL
);

-- Not a foreign key: metadata outlives a session row that is removed and indexed again.
CREATE TABLE session_meta (
    session_id TEXT PRIMARY KEY,
    label      TEXT,
    pinned     INTEGER NOT NULL DEFAULT 0,
    archived   INTEGER NOT NULL DEFAULT 0,
    note       TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE launches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('vigil', 'shell')),
    subcommand TEXT,
    flags      TEXT NOT NULL, -- JSON list of flag names, never values
    session_id TEXT
);

CREATE INDEX launches_at ON launches (at);
