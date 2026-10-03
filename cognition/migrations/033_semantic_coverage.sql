-- Semantic vectors are a rebuildable cache. v2 sampled long sources, so none of
-- its rows can certify v4 contiguous, token-complete coverage.
DELETE FROM cognitive_semantic_vectors;
CREATE TABLE cognitive_semantic_progress (
    context_id BIGINT NOT NULL,
    artifact_id BIGINT NOT NULL,
    embedding_model TEXT NOT NULL,
    source_event_id BIGINT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    projection_epoch BIGINT NOT NULL,
    generation BIGSERIAL NOT NULL,
    next_chunk INTEGER NOT NULL DEFAULT 0 CHECK(next_chunk>=0),
    total_chunks INTEGER NOT NULL CHECK(total_chunks>0),
    last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(context_id,artifact_id,embedding_model),
    CHECK(next_chunk<=total_chunks),
    FOREIGN KEY(context_id,artifact_id) REFERENCES cognitive_artifacts(context_id,id) ON DELETE CASCADE,
    FOREIGN KEY(context_id,source_event_id) REFERENCES cognitive_events(context_id,id) ON DELETE CASCADE
);
CREATE INDEX idx_semantic_progress_fair ON cognitive_semantic_progress(embedding_model,last_attempt_at,artifact_id);
ALTER TABLE cognitive_semantic_vectors ADD COLUMN source_fingerprint TEXT NOT NULL;
ALTER TABLE cognitive_semantic_vectors ADD COLUMN projection_epoch BIGINT NOT NULL;
ALTER TABLE cognitive_semantic_vectors ADD COLUMN chunk_index INTEGER NOT NULL CHECK(chunk_index>=0);
ALTER TABLE cognitive_semantic_vectors ADD CONSTRAINT semantic_vector_progress_fk
    FOREIGN KEY(context_id,artifact_id,embedding_model)
    REFERENCES cognitive_semantic_progress(context_id,artifact_id,embedding_model) ON DELETE CASCADE;
CREATE UNIQUE INDEX idx_semantic_vector_ordinal
    ON cognitive_semantic_vectors(context_id,artifact_id,embedding_model,chunk_index);

CREATE FUNCTION arti_semantic_fingerprint(artifact JSONB,source JSONB)
RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT md5(jsonb_build_array(artifact->>'gist',artifact->>'interpretation',
        artifact->>'event_id',artifact->>'source_id',source)::text)
$$;
CREATE FUNCTION arti_private_semantic_chunks(source TEXT)
RETURNS INTEGER LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT CASE WHEN length(source)=0 THEN 0 WHEN length(source)<=420 THEN 1
        ELSE (greatest(1,length(source)-100)+539)/540 END
$$;

-- Rehearsal changes accessibility, not semantic content; retain its cursor.
CREATE OR REPLACE FUNCTION arti_invalidate_semantic_vector() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.suppressed_at IS NOT NULL OR NEW.payload IS NULL
       OR NEW.context_id IS DISTINCT FROM OLD.context_id
       OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
       OR NEW.kind IS DISTINCT FROM OLD.kind
       OR NEW.model_version IS DISTINCT FROM OLD.model_version
       OR NEW.projection_epoch IS DISTINCT FROM OLD.projection_epoch
       OR (NEW.payload->>'gist') IS DISTINCT FROM (OLD.payload->>'gist')
       OR (NEW.payload->>'interpretation') IS DISTINCT FROM (OLD.payload->>'interpretation')
       OR (NEW.payload->>'event_id') IS DISTINCT FROM (OLD.payload->>'event_id')
       OR (NEW.payload->>'source_id') IS DISTINCT FROM (OLD.payload->>'source_id') THEN
        DELETE FROM cognitive_semantic_progress WHERE context_id=OLD.context_id AND artifact_id=OLD.id;
        DELETE FROM cognitive_semantic_vectors WHERE context_id=OLD.context_id AND artifact_id=OLD.id;
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION arti_invalidate_private_semantic_source(cid BIGINT,eid BIGINT)
RETURNS VOID LANGUAGE SQL AS $$
    WITH RECURSIVE affected(id) AS (
        SELECT eid
        UNION
        SELECT d.event_id FROM cognitive_event_dependencies d JOIN affected prior ON prior.id=d.source_event_id
          WHERE d.context_id=cid
    )
    DELETE FROM cognitive_semantic_progress s WHERE s.context_id=cid
      AND (s.source_event_id IN (SELECT id FROM affected) OR EXISTS(
        SELECT 1 FROM cognitive_provenance p WHERE p.context_id=s.context_id AND p.artifact_id=s.artifact_id
          AND p.source_event_id IN (SELECT id FROM affected)))
$$;

CREATE FUNCTION arti_semantic_source_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR NEW.payload IS DISTINCT FROM OLD.payload
       OR NEW.suppressed_at IS DISTINCT FROM OLD.suppressed_at
       OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
       OR NEW.source_id IS DISTINCT FROM OLD.source_id
       OR NEW.context_id IS DISTINCT FROM OLD.context_id
       OR NEW.origin IS DISTINCT FROM OLD.origin THEN
        -- Serialize late commits even for direct SQL mutation/erasure.
        PERFORM 1 FROM cognitive_contexts WHERE id=OLD.context_id FOR UPDATE;
        PERFORM arti_invalidate_private_semantic_source(OLD.context_id,OLD.id);
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER cognitive_semantic_source_erasure BEFORE UPDATE OR DELETE ON cognitive_events
FOR EACH ROW EXECUTE FUNCTION arti_semantic_source_changed();

CREATE FUNCTION arti_semantic_dependency_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP!='INSERT' THEN
        DELETE FROM cognitive_semantic_progress WHERE context_id=OLD.context_id AND artifact_id=OLD.artifact_id;
    END IF;
    IF TG_OP!='DELETE' THEN
        DELETE FROM cognitive_semantic_progress WHERE context_id=NEW.context_id AND artifact_id=NEW.artifact_id;
        RETURN NEW;
    END IF;
    RETURN OLD;
END $$;
CREATE TRIGGER cognitive_semantic_dependency_erasure AFTER INSERT OR UPDATE OR DELETE ON cognitive_provenance
FOR EACH ROW EXECUTE FUNCTION arti_semantic_dependency_changed();

CREATE FUNCTION arti_semantic_context_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.suppression_epoch IS DISTINCT FROM OLD.suppression_epoch
       OR NEW.rebuilding IS DISTINCT FROM OLD.rebuilding
       OR NEW.authority IS DISTINCT FROM OLD.authority
       OR NEW.model_version IS DISTINCT FROM OLD.model_version THEN
        DELETE FROM cognitive_semantic_progress WHERE context_id=NEW.id;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER cognitive_semantic_context_erasure AFTER UPDATE ON cognitive_contexts
FOR EACH ROW EXECUTE FUNCTION arti_semantic_context_changed();

CREATE FUNCTION arti_private_semantic_event_dependency_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP!='INSERT' THEN
        PERFORM arti_invalidate_private_semantic_source(OLD.context_id,OLD.event_id);
    END IF;
    IF TG_OP!='DELETE' THEN
        PERFORM arti_invalidate_private_semantic_source(NEW.context_id,NEW.event_id);
        RETURN NEW;
    END IF;
    RETURN OLD;
END $$;
CREATE TRIGGER cognitive_private_semantic_event_dependency_erasure
BEFORE INSERT OR UPDATE OR DELETE ON cognitive_event_dependencies
FOR EACH ROW EXECUTE FUNCTION arti_private_semantic_event_dependency_changed();
