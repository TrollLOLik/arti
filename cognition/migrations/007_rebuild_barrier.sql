-- Multi-transaction reconstruction is a durable, recoverable maintenance phase.
ALTER TABLE cognitive_contexts ADD COLUMN IF NOT EXISTS rebuilding BOOLEAN NOT NULL DEFAULT FALSE;
