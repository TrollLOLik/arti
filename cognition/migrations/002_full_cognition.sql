ALTER TABLE cognitive_contexts ADD COLUMN IF NOT EXISTS suppression_epoch BIGINT NOT NULL DEFAULT 0;
ALTER TABLE cognitive_contexts ADD COLUMN IF NOT EXISTS authority TEXT NOT NULL DEFAULT 'shadow'
    CHECK (authority IN ('shadow','active','legacy'));
ALTER TABLE cognitive_contexts ADD COLUMN IF NOT EXISTS projection_revision BIGINT NOT NULL DEFAULT 0;
ALTER TABLE cognitive_artifacts ADD COLUMN IF NOT EXISTS artifact_key TEXT;
ALTER TABLE cognitive_artifacts ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 1;
CREATE UNIQUE INDEX IF NOT EXISTS idx_cognitive_artifact_key
    ON cognitive_artifacts(context_id,artifact_key) WHERE artifact_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cognitive_artifact_scope
    ON cognitive_artifacts(context_id,kind,owner_id,model_version) WHERE suppressed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_cognitive_artifact_lexical
    ON cognitive_artifacts USING GIN(to_tsvector('russian',coalesce(payload->>'gist','')))
    WHERE suppressed_at IS NULL AND kind='trace';
CREATE TABLE IF NOT EXISTS cognitive_projection_effects (
    context_id BIGINT NOT NULL REFERENCES cognitive_contexts(id),
    event_id BIGINT NOT NULL,
    phase TEXT NOT NULL,
    model_version TEXT NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(context_id,event_id,phase,model_version),
    FOREIGN KEY(context_id,event_id) REFERENCES cognitive_events(context_id,id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS cognitive_artifact_parents (
    context_id BIGINT NOT NULL, child_id BIGINT NOT NULL, parent_id BIGINT NOT NULL,
    PRIMARY KEY(context_id,child_id,parent_id),
    FOREIGN KEY(context_id,child_id) REFERENCES cognitive_artifacts(context_id,id) ON DELETE CASCADE,
    FOREIGN KEY(context_id,parent_id) REFERENCES cognitive_artifacts(context_id,id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS cognitive_memory_links (
    context_id BIGINT NOT NULL, source_id BIGINT NOT NULL, target_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('episode','topic','temporal','semantic','revision')),
    weight DOUBLE PRECISION NOT NULL CHECK(weight>=0 AND weight<=1),
    PRIMARY KEY(context_id,source_id,target_id,kind),
    FOREIGN KEY(context_id,source_id) REFERENCES cognitive_artifacts(context_id,id) ON DELETE CASCADE,
    FOREIGN KEY(context_id,target_id) REFERENCES cognitive_artifacts(context_id,id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS cognitive_embeddings (
    context_id BIGINT NOT NULL, artifact_id BIGINT NOT NULL, embedding_model TEXT NOT NULL,
    vector DOUBLE PRECISION[] NOT NULL, model_version TEXT NOT NULL,
    PRIMARY KEY(context_id,artifact_id,embedding_model,model_version),
    FOREIGN KEY(context_id,artifact_id) REFERENCES cognitive_artifacts(context_id,id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS cognitive_retrievals (
    id BIGSERIAL PRIMARY KEY, context_id BIGINT NOT NULL REFERENCES cognitive_contexts(id),
    cycle_key TEXT NOT NULL, owner_id BIGINT, stage TEXT NOT NULL
       CHECK(stage IN ('candidate','recalled','archive_checked','included','expressed')),
    artifact_ids BIGINT[] NOT NULL, created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(context_id,cycle_key,stage)
);
CREATE TABLE IF NOT EXISTS cognitive_reappraisals (
    context_id BIGINT NOT NULL, cause_event_id BIGINT NOT NULL, support_event_id BIGINT NOT NULL,
    perception JSONB, created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(context_id,cause_event_id,support_event_id),
    FOREIGN KEY(context_id,cause_event_id) REFERENCES cognitive_events(context_id,id),
    FOREIGN KEY(context_id,support_event_id) REFERENCES cognitive_events(context_id,id)
);
CREATE TABLE IF NOT EXISTS cognitive_outbox (
    id BIGSERIAL PRIMARY KEY, context_id BIGINT NOT NULL, event_id BIGINT NOT NULL,
    delivery_key TEXT NOT NULL, channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'prepared' CHECK(status IN ('prepared','sending','delivered','delivery_unknown','cancelled')),
    payload JSONB, receipt_id BIGINT, suppression_epoch BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(context_id,delivery_key),
    FOREIGN KEY(context_id,event_id) REFERENCES cognitive_events(context_id,id)
);
CREATE TABLE IF NOT EXISTS cognitive_checkpoints (
    stream TEXT PRIMARY KEY, snapshot_id BIGINT NOT NULL DEFAULT 0,
    cursor_id BIGINT NOT NULL DEFAULT 0, report JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS cognitive_legacy_map (
    source_table TEXT NOT NULL, legacy_id BIGINT NOT NULL, context_id BIGINT,
    event_id BIGINT, artifact_id BIGINT, status TEXT NOT NULL CHECK(status IN ('imported','quarantined','suppressed')),
    reason TEXT NOT NULL DEFAULT '', PRIMARY KEY(source_table,legacy_id)
);
CREATE TABLE IF NOT EXISTS cognitive_scenes (
    chat_id BIGINT PRIMARY KEY, scene_id TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS memory_processing_checkpoints (
    chat_id BIGINT NOT NULL, mode TEXT NOT NULL, phase TEXT NOT NULL, last_id BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY(chat_id,mode,phase)
);
ALTER TABLE IF EXISTS memory_chunks ADD COLUMN IF NOT EXISTS embedding_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE IF EXISTS memory_chunks ADD COLUMN IF NOT EXISTS embedding_error_code TEXT;
