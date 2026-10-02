-- Additive schema. Apply on a disposable DB first; legacy data is never guessed.
CREATE TABLE IF NOT EXISTS cognitive_contexts (
    id BIGSERIAL PRIMARY KEY,
    persona_id TEXT NOT NULL,
    chat_id BIGINT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('default','rp')),
    scene_id TEXT NOT NULL DEFAULT '',
    model_version TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
    state JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (mode != 'rp' OR scene_id != ''),
    UNIQUE (persona_id, chat_id, mode, scene_id)
);
CREATE TABLE IF NOT EXISTS cognitive_events (
    id BIGSERIAL PRIMARY KEY,
    context_id BIGINT NOT NULL REFERENCES cognitive_contexts(id),
    event_key TEXT NOT NULL,
    source_id TEXT NOT NULL,
    independent_group TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('user','delivered_action','system','recall','replay')),
    owner_id BIGINT,
    occurred_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    payload JSONB,
    fingerprint TEXT,
    perception JSONB,
    suppressed_at TIMESTAMPTZ,
    CHECK (observed_at >= occurred_at),
    UNIQUE (context_id, event_key),
    UNIQUE (context_id, source_id, origin),
    UNIQUE (context_id, id)
);
CREATE INDEX IF NOT EXISTS idx_cognitive_events_rebuild ON cognitive_events(context_id, observed_at, id)
    WHERE suppressed_at IS NULL;
CREATE TABLE IF NOT EXISTS cognitive_effects (
    context_id BIGINT NOT NULL,
    event_id BIGINT NOT NULL,
    independent_group TEXT NOT NULL,
    model_version TEXT NOT NULL,
    applied_revision BIGINT NOT NULL,
    PRIMARY KEY (context_id, independent_group, model_version),
    FOREIGN KEY (context_id, event_id) REFERENCES cognitive_events(context_id, id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS cognitive_artifacts (
    id BIGSERIAL PRIMARY KEY,
    context_id BIGINT NOT NULL REFERENCES cognitive_contexts(id),
    owner_id BIGINT,
    kind TEXT NOT NULL,
    model_version TEXT NOT NULL,
    payload JSONB,
    suppressed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (context_id, id)
);
-- Flattened raw-source provenance: every derivative inherits all parent sources.
CREATE TABLE IF NOT EXISTS cognitive_provenance (
    context_id BIGINT NOT NULL,
    artifact_id BIGINT NOT NULL,
    source_event_id BIGINT NOT NULL,
    PRIMARY KEY (context_id, artifact_id, source_event_id),
    FOREIGN KEY (context_id, artifact_id) REFERENCES cognitive_artifacts(context_id, id) ON DELETE CASCADE,
    FOREIGN KEY (context_id, source_event_id) REFERENCES cognitive_events(context_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cognitive_provenance_source ON cognitive_provenance(context_id, source_event_id);
CREATE TABLE IF NOT EXISTS cognitive_jobs (
    id BIGSERIAL PRIMARY KEY,
    context_id BIGINT NOT NULL,
    event_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('interpret','encode','replay','rebuild')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','dead','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_until TIMESTAMPTZ,
    lease_token TEXT,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (context_id, event_id) REFERENCES cognitive_events(context_id, id) ON DELETE CASCADE,
    UNIQUE (context_id, event_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_cognitive_jobs_ready ON cognitive_jobs(status, available_at, lease_until);
ALTER TABLE cognitive_contexts ADD COLUMN IF NOT EXISTS worker_lease_until TIMESTAMPTZ;
ALTER TABLE cognitive_contexts ADD COLUMN IF NOT EXISTS worker_token TEXT;
