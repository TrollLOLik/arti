-- Explicit Telegram rate-limit rejection is safe to retry after this instant.
-- This is transport scheduling only; it does not alter cognitive algorithms.
ALTER TABLE cognitive_outbox ADD COLUMN IF NOT EXISTS retry_at TIMESTAMPTZ;
