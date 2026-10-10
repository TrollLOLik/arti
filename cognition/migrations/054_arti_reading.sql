-- H4c (I7): what Arti read between conversations. Public web material and her
-- own short opinion; no private data. Sharing with a person is recorded by the
-- delivery outbox key, not here.
CREATE TABLE IF NOT EXISTS arti_reading_notes (
    id BIGSERIAL PRIMARY KEY,
    read_on DATE NOT NULL,
    topic TEXT NOT NULL CHECK (length(topic) BETWEEN 1 AND 80),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    url TEXT NOT NULL CHECK (url ~ '^https?://' AND length(url) <= 2048),
    gist TEXT NOT NULL CHECK (length(gist) BETWEEN 1 AND 600),
    opinion TEXT NOT NULL CHECK (length(opinion) BETWEEN 1 AND 600),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (read_on, topic)
);
CREATE INDEX IF NOT EXISTS idx_arti_reading_notes_recent ON arti_reading_notes(created_at DESC);
