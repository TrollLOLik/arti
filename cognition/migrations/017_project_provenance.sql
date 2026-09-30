-- Source-derived reasons/previews must participate in the physical erase fence.
CREATE FUNCTION arti_project_source_cleanup() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.erased_at IS NOT NULL AND OLD.erased_at IS NULL) OR NEW.current_version<>OLD.current_version THEN
        UPDATE arti_project_result_candidates SET reason=NULL WHERE derivative_id IN
            (SELECT derivative_id FROM material_dependencies WHERE asset_id=NEW.id);
        UPDATE arti_project_revisions SET payload=NULL WHERE payload->>'derivative_id' IN
            (SELECT derivative_id FROM material_dependencies WHERE asset_id=NEW.id);
        UPDATE arti_project_publications SET payload=NULL,status='revoked' WHERE EXISTS
            (SELECT 1 FROM jsonb_array_elements(payload->'inputs') input WHERE input->>'id'=NEW.id);
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER project_source_cleanup AFTER UPDATE OF erased_at,current_version ON material_assets
FOR EACH ROW EXECUTE FUNCTION arti_project_source_cleanup();
