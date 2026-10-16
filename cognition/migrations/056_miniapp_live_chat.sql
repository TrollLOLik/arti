ALTER TABLE app_conversations ADD COLUMN history_revision BIGINT NOT NULL DEFAULT 1;
ALTER TABLE app_messages ADD COLUMN cognitive_context_id BIGINT;
ALTER TABLE app_messages ADD COLUMN cognitive_event_id BIGINT;
ALTER TABLE app_messages ADD COLUMN request_id TEXT;
ALTER TABLE app_messages ADD COLUMN source_channel TEXT NOT NULL DEFAULT 'miniapp'
    CHECK (source_channel IN ('miniapp','telegram'));
CREATE UNIQUE INDEX app_source_event ON app_messages(cognitive_event_id) WHERE cognitive_event_id IS NOT NULL;
ALTER TABLE app_events DROP CONSTRAINT app_events_kind_check;
ALTER TABLE app_events ADD CONSTRAINT app_events_kind_check CHECK (kind IN ('message.created','membership.changed','history.reset'));
CREATE TABLE app_publications (
    request_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>0),
    message_id UUID NOT NULL REFERENCES app_messages(id), body_hash TEXT NOT NULL,
    PRIMARY KEY(request_id,ordinal)
);
ALTER TABLE app_deliveries ADD COLUMN payload TEXT;
ALTER TABLE app_deliveries ADD COLUMN token TEXT;
ALTER TABLE app_deliveries ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE app_deliveries ADD COLUMN available_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX app_mirror_pending ON app_deliveries(available_at) WHERE target='telegram' AND state='prepared';
-- Native rooms have a typed UUID address; never allocate a fictitious Telegram ID.
-- Inference remains gated until room policy/membership handling in MA06.
CREATE TABLE cognitive_native_contexts (
    id UUID PRIMARY KEY REFERENCES app_conversations(id), persona_id TEXT NOT NULL DEFAULT 'arti',
    mode TEXT NOT NULL DEFAULT 'default' CHECK(mode='default'), state JSONB NOT NULL DEFAULT '{}'
);
