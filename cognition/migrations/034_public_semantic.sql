-- Locale-independent Russian orthographic casing. Each substitution keeps one
-- character, so source offsets stay exact even on a C-locale PostgreSQL server.
-- English ASCII casing is already handled by PostgreSQL's Russian dictionary.
CREATE FUNCTION arti_memory_search_text(value TEXT)
RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT translate(value,'АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ','абвгдеёжзийклмнопрстуфхцчшщъыьэюя')
$$;

-- Public vectors never share storage or ownership rules with private traces.
-- Text stays in the source ledger. Progress records complete sequential coverage,
-- rather than interpreting the first committed vector as completion.
CREATE TABLE cognitive_public_semantic_progress (
    generation BIGSERIAL NOT NULL,
    context_id BIGINT NOT NULL,
    event_id BIGINT NOT NULL,
    embedding_model TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    projection_epoch BIGINT NOT NULL,
    total_chunks INTEGER NOT NULL CHECK(total_chunks >= 0),
    next_chunk INTEGER NOT NULL DEFAULT 0 CHECK(next_chunk >= 0 AND next_chunk <= total_chunks),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(context_id,event_id,embedding_model),
    FOREIGN KEY(context_id,event_id) REFERENCES cognitive_events(context_id,id) ON DELETE CASCADE
);
CREATE TABLE cognitive_public_semantic_vectors (
    context_id BIGINT NOT NULL,
    event_id BIGINT NOT NULL,
    embedding_model TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    projection_epoch BIGINT NOT NULL,
    chunk_index INTEGER NOT NULL CHECK(chunk_index >= 0),
    chunk_start INTEGER NOT NULL CHECK(chunk_start >= 0),
    chunk_end INTEGER NOT NULL CHECK(chunk_end > chunk_start),
    vector DOUBLE PRECISION[] NOT NULL CHECK(array_length(vector,1)=384),
    PRIMARY KEY(context_id,event_id,embedding_model,chunk_index),
    FOREIGN KEY(context_id,event_id,embedding_model)
        REFERENCES cognitive_public_semantic_progress(context_id,event_id,embedding_model) ON DELETE CASCADE
);
CREATE TABLE cognitive_public_semantic_work (
    context_id BIGINT PRIMARY KEY REFERENCES cognitive_contexts(id) ON DELETE CASCADE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- Erasing or correcting any source also invalidates every dependent public
-- source. Recursive UNION terminates cycles. The ledger's final validation is
-- still mandatory; invalidation alone is not an authorization boundary.
CREATE FUNCTION arti_invalidate_public_semantic_source(cid BIGINT,eid BIGINT)
RETURNS VOID LANGUAGE SQL AS $$
    WITH RECURSIVE affected(id) AS (
        SELECT eid
        UNION
        SELECT d.event_id FROM cognitive_event_dependencies d JOIN affected a
          ON a.id=d.source_event_id WHERE d.context_id=cid
    )
    DELETE FROM cognitive_public_semantic_progress
      WHERE context_id=cid AND event_id IN (SELECT id FROM affected)
$$;
CREATE FUNCTION arti_public_semantic_event_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        PERFORM arti_invalidate_public_semantic_source(OLD.context_id,OLD.id);
        RETURN OLD;
    END IF;
    IF NEW.payload IS DISTINCT FROM OLD.payload OR NEW.suppressed_at IS DISTINCT FROM OLD.suppressed_at
       OR NEW.owner_id IS DISTINCT FROM OLD.owner_id OR NEW.source_id IS DISTINCT FROM OLD.source_id
       OR NEW.event_key IS DISTINCT FROM OLD.event_key
       OR NEW.origin IS DISTINCT FROM OLD.origin OR NEW.observed_at IS DISTINCT FROM OLD.observed_at
       OR NEW.occurred_at IS DISTINCT FROM OLD.occurred_at THEN
        PERFORM arti_invalidate_public_semantic_source(OLD.context_id,OLD.id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER public_semantic_event_cleanup BEFORE UPDATE OR DELETE ON cognitive_events
FOR EACH ROW EXECUTE FUNCTION arti_public_semantic_event_cleanup();
CREATE FUNCTION arti_public_semantic_observation_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        PERFORM arti_invalidate_public_semantic_source(OLD.context_id,OLD.event_id);
        RETURN OLD;
    END IF;
    IF NEW IS DISTINCT FROM OLD THEN
        PERFORM arti_invalidate_public_semantic_source(OLD.context_id,OLD.event_id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER public_semantic_observation_cleanup BEFORE UPDATE OR DELETE ON group_observations
FOR EACH ROW EXECUTE FUNCTION arti_public_semantic_observation_cleanup();
CREATE FUNCTION arti_public_semantic_dependency_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP<>'INSERT' THEN
        PERFORM arti_invalidate_public_semantic_source(OLD.context_id,OLD.event_id);
    END IF;
    IF TG_OP<>'DELETE' THEN
        PERFORM arti_invalidate_public_semantic_source(NEW.context_id,NEW.event_id);
        RETURN NEW;
    END IF;
    RETURN OLD;
END $$;
CREATE TRIGGER public_semantic_dependency_cleanup BEFORE INSERT OR UPDATE OR DELETE ON cognitive_event_dependencies
FOR EACH ROW EXECUTE FUNCTION arti_public_semantic_dependency_cleanup();
CREATE FUNCTION arti_public_semantic_context_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.suppression_epoch IS DISTINCT FROM OLD.suppression_epoch
       OR NEW.history_after_event_id IS DISTINCT FROM OLD.history_after_event_id
       OR NEW.authority IS DISTINCT FROM OLD.authority OR NEW.rebuilding IS DISTINCT FROM OLD.rebuilding
       OR NEW.model_version IS DISTINCT FROM OLD.model_version
       OR NEW.persona_id IS DISTINCT FROM OLD.persona_id OR NEW.chat_id IS DISTINCT FROM OLD.chat_id
       OR NEW.mode IS DISTINCT FROM OLD.mode OR NEW.scene_id IS DISTINCT FROM OLD.scene_id
       OR NEW.topic_id IS DISTINCT FROM OLD.topic_id THEN
        DELETE FROM cognitive_public_semantic_progress WHERE context_id=OLD.id;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER public_semantic_context_cleanup AFTER UPDATE ON cognitive_contexts
FOR EACH ROW EXECUTE FUNCTION arti_public_semantic_context_cleanup();
CREATE FUNCTION arti_public_semantic_optout_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.opt_out THEN
        WITH RECURSIVE affected(context_id,event_id) AS (
            SELECT e.context_id,e.id FROM cognitive_events e JOIN cognitive_contexts c ON c.id=e.context_id
              WHERE c.chat_id=NEW.chat_id AND e.owner_id=NEW.user_id
            UNION
            SELECT d.context_id,d.event_id FROM cognitive_event_dependencies d JOIN affected a
              ON d.context_id=a.context_id AND d.source_event_id=a.event_id
        )
        DELETE FROM cognitive_public_semantic_progress p USING affected a
          WHERE p.context_id=a.context_id AND p.event_id=a.event_id;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER public_semantic_optout_cleanup AFTER INSERT OR UPDATE ON group_participant_settings
FOR EACH ROW EXECUTE FUNCTION arti_public_semantic_optout_cleanup();
CREATE FUNCTION arti_public_semantic_policy_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    -- A policy edit is uncommon; conservative invalidation prevents stale
    -- retained vectors while the worker resumes the newly permitted sources.
    IF TG_OP='INSERT' OR NEW.payload IS DISTINCT FROM OLD.payload THEN
        IF TG_TABLE_NAME='group_topic_settings' THEN
            DELETE FROM cognitive_public_semantic_progress WHERE context_id IN
              (SELECT id FROM cognitive_contexts WHERE chat_id=NEW.chat_id AND topic_id=NEW.topic_id);
        ELSE
            DELETE FROM cognitive_public_semantic_progress WHERE context_id IN
              (SELECT id FROM cognitive_contexts WHERE chat_id=NEW.chat_id);
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER public_semantic_chat_policy_cleanup AFTER INSERT OR UPDATE ON group_chat_settings
FOR EACH ROW EXECUTE FUNCTION arti_public_semantic_policy_cleanup();
CREATE TRIGGER public_semantic_topic_policy_cleanup AFTER INSERT OR UPDATE ON group_topic_settings
FOR EACH ROW EXECUTE FUNCTION arti_public_semantic_policy_cleanup();
CREATE FUNCTION arti_public_semantic_scene_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM cognitive_public_semantic_progress WHERE context_id IN
      (SELECT id FROM cognitive_contexts WHERE chat_id=OLD.chat_id AND topic_id=OLD.topic_id
        AND mode='rp' AND scene_id=OLD.scene_id);
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER public_semantic_scene_cleanup BEFORE UPDATE OR DELETE ON cognitive_scenes
FOR EACH ROW EXECUTE FUNCTION arti_public_semantic_scene_cleanup();

CREATE FUNCTION arti_public_semantic_response_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.enabled IS FALSE THEN
        DELETE FROM cognitive_public_semantic_progress WHERE context_id IN
          (SELECT id FROM cognitive_contexts WHERE chat_id=NEW.chat_id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER public_semantic_response_cleanup AFTER INSERT OR UPDATE ON response_status
FOR EACH ROW EXECUTE FUNCTION arti_public_semantic_response_cleanup();
