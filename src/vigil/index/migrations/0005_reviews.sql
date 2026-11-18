-- Session reviews (`vigil.review`): the numbers vigil computed from the transcript, and what
-- Claude suggested after reading a non-persisted fork of the session. A review goes when its
-- session leaves the index.
--   numbers      JSON (`vigil.review.numbers.numbers_to_dict`)
--   suggestions  JSON list of `vigil.review.schema.Suggestion` fields; NULL until done
--   advice       JSON list of `vigil.review.schema.Advice` fields; NULL until done
--   error        why it failed, or a note on a review that finished (e.g. over the budget)

CREATE TABLE reviews (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    machine        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    finished_at    TEXT,
    claude_version TEXT,
    model          TEXT,
    cost_usd       REAL,
    status         TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    numbers        TEXT NOT NULL,
    suggestions    TEXT,
    advice         TEXT,
    error          TEXT
);

CREATE INDEX reviews_session ON reviews (session_id);
