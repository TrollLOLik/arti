-- Historic scopes remain unknown; no guessed topic or public promotion.
ALTER TABLE cognitive_contexts ADD COLUMN IF NOT EXISTS topic_id BIGINT NOT NULL DEFAULT -1;
ALTER TABLE cognitive_contexts DROP CONSTRAINT IF EXISTS cognitive_contexts_persona_id_chat_id_mode_scene_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS cognitive_context_topic_identity ON cognitive_contexts(persona_id,chat_id,mode,scene_id,topic_id);
ALTER TABLE cognitive_scenes ADD COLUMN IF NOT EXISTS topic_id BIGINT NOT NULL DEFAULT -1;
ALTER TABLE cognitive_scenes DROP CONSTRAINT IF EXISTS cognitive_scenes_pkey;
ALTER TABLE cognitive_scenes ADD PRIMARY KEY(chat_id,topic_id);
CREATE TABLE group_chat_settings (
 chat_id BIGINT PRIMARY KEY, payload JSONB NOT NULL, revision BIGINT NOT NULL DEFAULT 1, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE group_topic_settings (
 chat_id BIGINT NOT NULL, topic_id BIGINT NOT NULL, payload JSONB NOT NULL, revision BIGINT NOT NULL DEFAULT 1,
 PRIMARY KEY(chat_id,topic_id)
);
CREATE TABLE group_participant_settings (
 chat_id BIGINT NOT NULL, user_id BIGINT NOT NULL, opt_out BOOLEAN NOT NULL DEFAULT FALSE,
 PRIMARY KEY(chat_id,user_id)
);
CREATE TABLE group_observations (
 id BIGSERIAL PRIMARY KEY, context_id BIGINT NOT NULL REFERENCES cognitive_contexts(id),
 event_id BIGINT NOT NULL REFERENCES cognitive_events(id), message_id BIGINT NOT NULL,
 owner_id BIGINT, payload JSONB, observed_at TIMESTAMPTZ NOT NULL, edited_at TIMESTAMPTZ,
 suppressed_at TIMESTAMPTZ, UNIQUE(context_id,message_id), UNIQUE(context_id,id),
 FOREIGN KEY(context_id,event_id) REFERENCES cognitive_events(context_id,id)
);
CREATE INDEX group_observations_recent ON group_observations(context_id,observed_at DESC) WHERE payload IS NOT NULL AND suppressed_at IS NULL;
CREATE TABLE group_candidates (
 id BIGSERIAL PRIMARY KEY, context_id BIGINT NOT NULL REFERENCES cognitive_contexts(id),
 candidate_key TEXT NOT NULL, kind TEXT NOT NULL, source_ids BIGINT[] NOT NULL,
 payload JSONB, status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMPTZ NOT NULL,
 due_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
 charged_at TIMESTAMPTZ,
 attempts INTEGER NOT NULL DEFAULT 0, policy_revision TEXT, lease_token TEXT, outbox_id BIGINT REFERENCES cognitive_outbox(id),
 UNIQUE(context_id,candidate_key), CHECK(status IN ('pending','deferred','claimed','shadow','abstained','cancelled','delivered','delivery_unknown'))
);
CREATE INDEX group_candidates_due ON group_candidates(status,due_at);
CREATE INDEX group_candidates_budget ON group_candidates(charged_at DESC,context_id) WHERE status IN ('claimed','delivered','delivery_unknown','shadow');
CREATE TABLE group_action_leases (
 context_id BIGINT PRIMARY KEY REFERENCES cognitive_contexts(id), token TEXT, fence BIGINT NOT NULL DEFAULT 0, lease_until TIMESTAMPTZ
);
CREATE TABLE group_decisions (
 id BIGSERIAL PRIMARY KEY, context_id BIGINT NOT NULL REFERENCES cognitive_contexts(id), candidate_id BIGINT REFERENCES group_candidates(id),
 action TEXT NOT NULL, reason TEXT NOT NULL, score DOUBLE PRECISION, policy_revision TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE group_feedback (
 context_id BIGINT NOT NULL REFERENCES cognitive_contexts(id), message_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
 signal DOUBLE PRECISION NOT NULL CHECK(signal BETWEEN -1 AND 1), created_at TIMESTAMPTZ NOT NULL,
 source_ids BIGINT[] NOT NULL, PRIMARY KEY(context_id,message_id,user_id)
);
CREATE TABLE group_topic_runtime (
 context_id BIGINT PRIMARY KEY REFERENCES cognitive_contexts(id), revision BIGINT NOT NULL DEFAULT 0,
 closed BOOLEAN NOT NULL DEFAULT FALSE, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
