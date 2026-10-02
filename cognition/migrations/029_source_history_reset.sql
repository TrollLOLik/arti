-- Clear recent dialogue without erasing autobiographical sources or emotions.
ALTER TABLE cognitive_contexts ADD COLUMN IF NOT EXISTS history_after_event_id BIGINT NOT NULL DEFAULT 0;
