-- H6 (P1): Arti's in-character life between conversations: embroidery, tea
-- tastes and restoring an old recording. Declared fiction kept consistent, not
-- facts about the world. Global to the persona: no owner, chat or context
-- columns, and never any person's data. Real reading stays in arti_reading_notes.
CREATE TABLE IF NOT EXISTS arti_pursuits (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('embroidery','tea','restoration')),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','finished','abandoned')),
    state JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(state)='object'),
    progress REAL NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
    next_step_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    CHECK ((status IN ('finished','abandoned')) = (finished_at IS NOT NULL))
);
-- One live pursuit per kind; finished and abandoned rows stay as history.
CREATE UNIQUE INDEX IF NOT EXISTS idx_arti_pursuits_live ON arti_pursuits(kind) WHERE status IN ('active','paused');
CREATE INDEX IF NOT EXISTS idx_arti_pursuits_due ON arti_pursuits(next_step_at) WHERE status='active';

-- What happened, in her words. diff is the deterministic change; summary and
-- feeling are the only model-written text and are validated against it.
CREATE TABLE IF NOT EXISTS arti_pursuit_events (
    id BIGSERIAL PRIMARY KEY,
    pursuit_id BIGINT NOT NULL REFERENCES arti_pursuits(id),
    step_key TEXT NOT NULL UNIQUE CHECK (length(step_key) BETWEEN 1 AND 80),
    kind TEXT NOT NULL CHECK (kind IN ('started','step','stage','pattern_change','finished','verdict','verdict_change')),
    summary TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 240),
    feeling TEXT NOT NULL CHECK (length(feeling) BETWEEN 1 AND 40),
    diff JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(diff)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_arti_pursuit_events_recent ON arti_pursuit_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_arti_pursuit_events_pursuit ON arti_pursuit_events(pursuit_id, created_at DESC);

-- Her tastes with version history: a changed verdict supersedes the current
-- row and never overwrites it. basis/source name the real reading note.
CREATE TABLE IF NOT EXISTS arti_tastes (
    id BIGSERIAL PRIMARY KEY,
    item TEXT NOT NULL CHECK (length(item) BETWEEN 1 AND 60),
    category TEXT NOT NULL CHECK (length(category) BETWEEN 1 AND 40),
    verdict TEXT NOT NULL CHECK (verdict IN ('да','нет','спорно')),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 240),
    basis TEXT NOT NULL CHECK (length(basis) BETWEEN 1 AND 300),
    source TEXT NOT NULL CHECK (source ~ '^https?://' AND length(source) <= 2048),
    source_note_id BIGINT REFERENCES arti_reading_notes(id) ON DELETE SET NULL,
    event_id BIGINT REFERENCES arti_pursuit_events(id),
    version INTEGER NOT NULL CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    superseded_at TIMESTAMPTZ,
    UNIQUE (item, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_arti_tastes_current ON arti_tastes(item) WHERE superseded_at IS NULL;

-- Attempts, for the daily model budget and retries. Counts and categories only:
-- no model text, no provider payload. A dry run's model call is counted too.
CREATE TABLE IF NOT EXISTS arti_pursuit_runs (
    id BIGSERIAL PRIMARY KEY,
    pursuit_id BIGINT REFERENCES arti_pursuits(id),
    step_key TEXT CHECK (length(step_key) <= 80),
    outcome TEXT NOT NULL CHECK (outcome IN ('stored','invalid','failed','stale','dry_run')),
    error TEXT CHECK (length(error) <= 80),
    model_calls SMALLINT NOT NULL DEFAULT 0 CHECK (model_calls >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_arti_pursuit_runs_recent ON arti_pursuit_runs(created_at DESC);
