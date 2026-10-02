-- Revoke only the copies made by the publication, never independent originals
-- another participant later attached to the target project.
CREATE FUNCTION arti_publication_copy_cleanup() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ids TEXT[];
BEGIN
    IF NEW.status='deleted' AND OLD.status<>'deleted' THEN
        SELECT array_agg(s.target_asset_id) INTO ids FROM arti_project_publications p
            JOIN material_shares s ON s.request_id LIKE '%:'||p.id
            WHERE p.project_id=NEW.id OR p.target_project_id=NEW.id;
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
CREATE TRIGGER publication_copy_cleanup AFTER UPDATE OF status ON arti_projects FOR EACH ROW EXECUTE FUNCTION arti_publication_copy_cleanup();
