-- Rebuildable, public-only semantic hypotheses. No private traces or memories.
CREATE TABLE group_understanding_state (
 context_id BIGINT PRIMARY KEY REFERENCES cognitive_contexts(id) ON DELETE CASCADE,
 generation BIGSERIAL NOT NULL,
 cursor_event_id BIGINT NOT NULL DEFAULT 0,
 pending_cursor BIGINT NOT NULL DEFAULT 0,
 coverage_start_event_id BIGINT NOT NULL DEFAULT 0,
 lineage_truncated BOOLEAN NOT NULL DEFAULT FALSE,
 schema_version TEXT NOT NULL,
 projection_epoch BIGINT NOT NULL,
 policy_revision TEXT NOT NULL,
 payload JSONB,
 input_event_ids BIGINT[] NOT NULL DEFAULT '{}',
 group_revision BIGINT NOT NULL DEFAULT 0,
 committed_at TIMESTAMPTZ,
 lease_token TEXT,
 lease_until TIMESTAMPTZ
);
-- This is complete cumulative consulted AND anchor-selection lineage, not just
-- the subset the model chose to cite. A later call never silently drops it.
CREATE TABLE group_understanding_dependencies (
 context_id BIGINT NOT NULL REFERENCES group_understanding_state(context_id) ON DELETE CASCADE,
 event_id BIGINT NOT NULL,
 source_id TEXT NOT NULL,
 source_hash TEXT NOT NULL,
 PRIMARY KEY(context_id,event_id),
 FOREIGN KEY(context_id,event_id) REFERENCES cognitive_events(context_id,id) ON DELETE CASCADE
);
-- Unusable sources are durable coverage gaps, never model evidence. Their
-- raw hash permits conservative replay after edits without pinning successors.
CREATE TABLE group_understanding_skips (
 context_id BIGINT NOT NULL REFERENCES group_understanding_state(context_id) ON DELETE CASCADE,
 event_id BIGINT NOT NULL,
 source_hash TEXT NOT NULL,
 reason TEXT NOT NULL CHECK(reason IN ('invalid_wire','lineage_limit')),
 PRIMARY KEY(context_id,event_id),
 FOREIGN KEY(context_id,event_id) REFERENCES cognitive_events(context_id,id) ON DELETE CASCADE
);
CREATE TABLE group_understanding_work (
 context_id BIGINT PRIMARY KEY REFERENCES cognitive_contexts(id) ON DELETE CASCADE,
 attempted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION arti_erase_group_understanding(cid BIGINT) RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    -- Fence direct SQL mutation against a late provider commit as well.
    PERFORM 1 FROM cognitive_contexts WHERE id=cid FOR UPDATE;
    DELETE FROM group_understanding_state WHERE context_id=cid;
    IF FOUND THEN
        UPDATE group_topic_runtime SET revision=revision+1,updated_at=clock_timestamp() WHERE context_id=cid;
    END IF;
END $$;
CREATE FUNCTION arti_invalidate_group_understanding_source(cid BIGINT,eid BIGINT)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1 FROM cognitive_contexts WHERE id=cid FOR UPDATE;
    IF EXISTS (
        WITH RECURSIVE affected(id) AS (
            SELECT eid UNION
            SELECT d.event_id FROM cognitive_event_dependencies d JOIN affected a
              ON a.id=d.source_event_id WHERE d.context_id=cid
        )
        SELECT 1 FROM group_understanding_dependencies d WHERE d.context_id=cid
          AND d.event_id IN (SELECT id FROM affected)
        UNION ALL
        SELECT 1 FROM group_understanding_skips s WHERE s.context_id=cid
          AND s.event_id IN (SELECT id FROM affected)
    ) OR EXISTS (SELECT 1 FROM group_understanding_state WHERE context_id=cid
                 AND eid<=greatest(cursor_event_id,pending_cursor)) THEN
        PERFORM arti_erase_group_understanding(cid);
    END IF;
END $$;
CREATE FUNCTION arti_group_understanding_event_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        PERFORM arti_invalidate_group_understanding_source(OLD.context_id,OLD.id);
        RETURN OLD;
    END IF;
    IF NEW.payload IS DISTINCT FROM OLD.payload OR NEW.suppressed_at IS DISTINCT FROM OLD.suppressed_at
       OR NEW.owner_id IS DISTINCT FROM OLD.owner_id OR NEW.source_id IS DISTINCT FROM OLD.source_id
       OR NEW.context_id IS DISTINCT FROM OLD.context_id OR NEW.event_key IS DISTINCT FROM OLD.event_key
       OR NEW.origin IS DISTINCT FROM OLD.origin OR NEW.observed_at IS DISTINCT FROM OLD.observed_at
       OR NEW.occurred_at IS DISTINCT FROM OLD.occurred_at THEN
        PERFORM arti_invalidate_group_understanding_source(OLD.context_id,OLD.id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER group_understanding_event_cleanup BEFORE UPDATE OR DELETE ON cognitive_events
FOR EACH ROW EXECUTE FUNCTION arti_group_understanding_event_cleanup();
CREATE FUNCTION arti_group_understanding_observation_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP<>'INSERT' AND (TG_OP='DELETE' OR NEW IS DISTINCT FROM OLD) THEN
        PERFORM arti_invalidate_group_understanding_source(OLD.context_id,OLD.event_id);
    END IF;
    IF TG_OP<>'DELETE' THEN
        -- An observation may arrive after its event was registered. If that
        -- event is behind the pending/committed prefix, replay the prefix.
        IF TG_OP='INSERT' OR NEW.event_id IS DISTINCT FROM OLD.event_id OR NEW.context_id IS DISTINCT FROM OLD.context_id THEN
            PERFORM arti_invalidate_group_understanding_source(NEW.context_id,NEW.event_id);
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END $$;
CREATE TRIGGER group_understanding_observation_cleanup BEFORE INSERT OR UPDATE OR DELETE ON group_observations
FOR EACH ROW EXECUTE FUNCTION arti_group_understanding_observation_cleanup();
CREATE FUNCTION arti_group_understanding_dependency_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP<>'INSERT' THEN
        PERFORM arti_invalidate_group_understanding_source(OLD.context_id,OLD.event_id);
    END IF;
    IF TG_OP<>'DELETE' THEN
        PERFORM arti_invalidate_group_understanding_source(NEW.context_id,NEW.event_id);
        RETURN NEW;
    END IF;
    RETURN OLD;
END $$;
CREATE TRIGGER group_understanding_dependency_cleanup BEFORE INSERT OR UPDATE OR DELETE ON cognitive_event_dependencies
FOR EACH ROW EXECUTE FUNCTION arti_group_understanding_dependency_cleanup();
CREATE FUNCTION arti_group_understanding_context_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.suppression_epoch IS DISTINCT FROM OLD.suppression_epoch
       OR NEW.history_after_event_id IS DISTINCT FROM OLD.history_after_event_id
       OR NEW.authority IS DISTINCT FROM OLD.authority OR NEW.rebuilding IS DISTINCT FROM OLD.rebuilding
       OR NEW.model_version IS DISTINCT FROM OLD.model_version
       OR NEW.persona_id IS DISTINCT FROM OLD.persona_id OR NEW.chat_id IS DISTINCT FROM OLD.chat_id
       OR NEW.mode IS DISTINCT FROM OLD.mode OR NEW.scene_id IS DISTINCT FROM OLD.scene_id
       OR NEW.topic_id IS DISTINCT FROM OLD.topic_id THEN
        PERFORM arti_erase_group_understanding(OLD.id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER group_understanding_context_cleanup AFTER UPDATE ON cognitive_contexts
FOR EACH ROW EXECUTE FUNCTION arti_group_understanding_context_cleanup();
CREATE FUNCTION arti_group_understanding_optout_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE cid BIGINT;
BEGIN
    IF NEW.opt_out THEN
        -- Also fence an in-flight extraction whose new inputs are already
        -- recorded. Recursively derived sources are checked by the helper.
        FOR cid IN SELECT DISTINCT c.id FROM cognitive_contexts c JOIN cognitive_events e ON e.context_id=c.id
                   WHERE c.chat_id=NEW.chat_id AND e.owner_id=NEW.user_id ORDER BY c.id LOOP
            PERFORM arti_erase_group_understanding(cid);
        END LOOP;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER group_understanding_optout_cleanup AFTER INSERT OR UPDATE ON group_participant_settings
FOR EACH ROW EXECUTE FUNCTION arti_group_understanding_optout_cleanup();
CREATE FUNCTION arti_group_understanding_policy_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE cid BIGINT; chat BIGINT; topic BIGINT;
BEGIN
    chat := CASE WHEN TG_OP='DELETE' THEN OLD.chat_id ELSE NEW.chat_id END;
    IF TG_TABLE_NAME='group_topic_settings' THEN
        topic := CASE WHEN TG_OP='DELETE' THEN OLD.topic_id ELSE NEW.topic_id END;
    END IF;
    FOR cid IN SELECT id FROM cognitive_contexts WHERE chat_id=chat AND (topic IS NULL OR topic_id=topic) ORDER BY id LOOP
        PERFORM arti_erase_group_understanding(cid);
    END LOOP;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER group_understanding_chat_policy_cleanup AFTER INSERT OR UPDATE OR DELETE ON group_chat_settings
FOR EACH ROW EXECUTE FUNCTION arti_group_understanding_policy_cleanup();
CREATE TRIGGER group_understanding_topic_policy_cleanup AFTER INSERT OR UPDATE OR DELETE ON group_topic_settings
FOR EACH ROW EXECUTE FUNCTION arti_group_understanding_policy_cleanup();
CREATE FUNCTION arti_group_understanding_scene_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE cid BIGINT;
BEGIN
    FOR cid IN SELECT id FROM cognitive_contexts WHERE chat_id=OLD.chat_id AND topic_id=OLD.topic_id
               AND mode='rp' AND scene_id=OLD.scene_id ORDER BY id LOOP
        PERFORM arti_erase_group_understanding(cid);
    END LOOP;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER group_understanding_scene_cleanup BEFORE UPDATE OR DELETE ON cognitive_scenes
FOR EACH ROW EXECUTE FUNCTION arti_group_understanding_scene_cleanup();
CREATE FUNCTION arti_group_understanding_response_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE cid BIGINT;
BEGIN
    IF NEW.enabled IS FALSE THEN
        FOR cid IN SELECT id FROM cognitive_contexts WHERE chat_id=NEW.chat_id ORDER BY id LOOP
            PERFORM arti_erase_group_understanding(cid);
        END LOOP;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER group_understanding_response_cleanup AFTER INSERT OR UPDATE ON response_status
FOR EACH ROW EXECUTE FUNCTION arti_group_understanding_response_cleanup();
