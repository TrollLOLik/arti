-- Content-free durable limits. Delivery attempts (including unknown outcomes)
-- consume a slot; neither provider abstention nor silence is a successful action.
CREATE TABLE cognitive_initiative_calls (
 token TEXT PRIMARY KEY,
 context_id BIGINT NOT NULL REFERENCES cognitive_contexts(id),
 owner_id BIGINT,
 kind TEXT NOT NULL,
 started_at TIMESTAMPTZ NOT NULL,
 lease_until TIMESTAMPTZ NOT NULL,
 completed_at TIMESTAMPTZ
);
CREATE INDEX cognitive_initiative_calls_window ON cognitive_initiative_calls(started_at DESC,context_id,kind);
CREATE INDEX cognitive_initiative_calls_active ON cognitive_initiative_calls(lease_until) WHERE completed_at IS NULL;
CREATE TABLE cognitive_initiative_charges (
 context_id BIGINT NOT NULL REFERENCES cognitive_contexts(id),
 delivery_key TEXT NOT NULL,
 owner_id BIGINT,
 charged_at TIMESTAMPTZ NOT NULL,
 PRIMARY KEY(context_id,delivery_key)
);
CREATE INDEX cognitive_initiative_charges_owner ON cognitive_initiative_charges(owner_id,charged_at DESC);
CREATE INDEX cognitive_initiative_charges_context ON cognitive_initiative_charges(context_id,charged_at DESC);
-- Preserve recent delivery pressure at upgrade using existing scoped metadata.
INSERT INTO cognitive_initiative_charges(context_id,delivery_key,owner_id,charged_at)
 SELECT o.context_id,o.delivery_key,e.owner_id,coalesce(g.charged_at,o.created_at)
 FROM group_candidates g JOIN cognitive_outbox o ON o.id=g.outbox_id
 JOIN cognitive_events e ON e.id=o.event_id AND e.context_id=o.context_id
 WHERE g.kind!='reminder' AND o.status IN ('sending','delivered','delivery_unknown')
 AND coalesce(g.charged_at,o.created_at)>NOW()-INTERVAL '1 day'
 ON CONFLICT DO NOTHING;
INSERT INTO cognitive_initiative_charges(context_id,delivery_key,owner_id,charged_at)
 SELECT o.context_id,o.delivery_key,a.owner_id,o.created_at
 FROM cognitive_artifacts a JOIN cognitive_contexts c ON c.id=a.context_id
 JOIN cognitive_outbox o ON o.context_id=a.context_id
   AND starts_with(o.delivery_key,(a.payload->>'delivery_key')||':')
 WHERE a.kind='intention' AND a.payload->>'status'='open' AND c.chat_id>0 AND c.topic_id<0
 AND o.status IN ('sending','delivered','delivery_unknown') AND o.created_at>NOW()-INTERVAL '1 day'
 ON CONFLICT DO NOTHING;
