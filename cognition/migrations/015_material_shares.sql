CREATE TABLE material_shares (
    target_asset_id TEXT PRIMARY KEY REFERENCES material_assets(id),
    source_asset_id TEXT NOT NULL REFERENCES material_assets(id),
    source_version INTEGER NOT NULL,authorized_by BIGINT NOT NULL,
    request_id TEXT NOT NULL,destination_scope TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),CHECK(target_asset_id<>source_asset_id)
);
CREATE INDEX material_share_source ON material_shares(source_asset_id);
CREATE FUNCTION arti_material_share_revoke() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ids TEXT[];
BEGIN
    IF (NEW.erased_at IS NOT NULL AND OLD.erased_at IS NULL) OR NEW.current_version<>OLD.current_version THEN
        WITH RECURSIVE copies(id) AS (
            SELECT target_asset_id FROM material_shares WHERE source_asset_id=NEW.id
            UNION SELECT s.target_asset_id FROM material_shares s JOIN copies c ON s.source_asset_id=c.id)
        SELECT array_agg(id) INTO ids FROM copies;
        IF ids IS NOT NULL THEN
            UPDATE material_assets SET erased_at=NOW(),generation=generation+1,filename='' WHERE id=ANY(ids) AND erased_at IS NULL;
            UPDATE material_extractions SET payload=NULL WHERE asset_id=ANY(ids);
            UPDATE material_derivatives SET payload=NULL,invalidated_at=NOW() WHERE id IN
                (SELECT derivative_id FROM material_dependencies WHERE asset_id=ANY(ids));
            UPDATE material_asset_versions SET blob_id=NULL WHERE asset_id=ANY(ids);
            INSERT INTO material_cognitive_cleanup(asset_id) SELECT id FROM material_assets WHERE id=ANY(ids) AND owner_id IS NOT NULL ON CONFLICT DO NOTHING;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER material_share_revoke AFTER UPDATE OF erased_at,current_version ON material_assets
FOR EACH ROW EXECUTE FUNCTION arti_material_share_revoke();
