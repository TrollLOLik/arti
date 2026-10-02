CREATE TABLE material_block_index (
    realm TEXT NOT NULL,
    asset_id TEXT NOT NULL REFERENCES material_assets(id),
    asset_version INTEGER NOT NULL,
    extraction_id TEXT NOT NULL REFERENCES material_extractions(id),
    block_id TEXT NOT NULL,
    locator JSONB NOT NULL,
    observation_id TEXT REFERENCES material_derivatives(id),
    observation_key TEXT NOT NULL DEFAULT '',
    segment_id TEXT,
    kind TEXT NOT NULL,
    role TEXT NOT NULL,
    quality TEXT NOT NULL,
    text TEXT NOT NULL,
    terms TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple',text)) STORED,
    PRIMARY KEY(extraction_id,block_id,observation_key)
);
CREATE INDEX material_index_search ON material_block_index USING GIN(terms);
CREATE INDEX material_index_realm ON material_block_index(realm,asset_id,asset_version);
-- Copies of source text are physically removed in the same erasure/version txn.
CREATE FUNCTION arti_material_index_cleanup() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.erased_at IS NOT NULL OR NEW.current_version<>OLD.current_version THEN
        DELETE FROM material_block_index WHERE asset_id=NEW.id;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER material_index_cleanup AFTER UPDATE OF erased_at,current_version ON material_assets
FOR EACH ROW EXECUTE FUNCTION arti_material_index_cleanup();
CREATE FUNCTION arti_transcript_index_cleanup() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM material_block_index WHERE realm=NEW.realm AND asset_id=NEW.asset_id AND observation_id IS NOT NULL;
    RETURN NEW;
END;
$$;
CREATE TRIGGER transcript_index_cleanup AFTER UPDATE OF observation_id ON material_observation_heads
FOR EACH ROW EXECUTE FUNCTION arti_transcript_index_cleanup();
