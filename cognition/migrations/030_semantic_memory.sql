CREATE TABLE cognitive_semantic_vectors (
    context_id BIGINT NOT NULL,
    artifact_id BIGINT NOT NULL,
    embedding_model TEXT NOT NULL,
    vector DOUBLE PRECISION[] NOT NULL CHECK(array_length(vector,1)=384),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(context_id,artifact_id,embedding_model),
    FOREIGN KEY(context_id,artifact_id) REFERENCES cognitive_artifacts(context_id,id) ON DELETE CASCADE
);
CREATE FUNCTION arti_semantic_dot(a DOUBLE PRECISION[],b DOUBLE PRECISION[])
RETURNS DOUBLE PRECISION LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT SUM(x*y) FROM unnest(a,b) AS v(x,y)
$$;
CREATE FUNCTION arti_invalidate_semantic_vector() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.suppressed_at IS NOT NULL OR NEW.payload IS NULL
       OR (NEW.payload->>'gist') IS DISTINCT FROM (OLD.payload->>'gist')
       OR (NEW.payload->>'interpretation') IS DISTINCT FROM (OLD.payload->>'interpretation') THEN
        DELETE FROM cognitive_semantic_vectors WHERE context_id=NEW.context_id AND artifact_id=NEW.id;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER cognitive_semantic_erasure AFTER UPDATE ON cognitive_artifacts
FOR EACH ROW EXECUTE FUNCTION arti_invalidate_semantic_vector();
