ALTER TABLE cognitive_artifacts ADD COLUMN IF NOT EXISTS projection_epoch BIGINT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_cognitive_artifacts_epoch ON cognitive_artifacts(context_id,projection_epoch,kind);
