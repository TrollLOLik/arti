CREATE TABLE arti_project_publications (
    id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES arti_projects(id),
    realm TEXT NOT NULL,author_id BIGINT NOT NULL,payload JSONB,
    target_project_id TEXT REFERENCES arti_projects(id),status TEXT NOT NULL DEFAULT 'prepared',
    expires_at TIMESTAMPTZ NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE arti_project_result_candidates (
    project_id TEXT NOT NULL REFERENCES arti_projects(id),result_key TEXT NOT NULL,
    derivative_id TEXT NOT NULL REFERENCES material_derivatives(id),
    actor_id BIGINT NOT NULL,status TEXT NOT NULL CHECK(status IN ('proposed','accepted','rejected')),
    reason TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(project_id,result_key,derivative_id)
);
CREATE FUNCTION arti_project_payload_cleanup() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE targets TEXT[];
BEGIN
    IF NEW.status='deleted' AND OLD.status<>'deleted' THEN
        SELECT array_agg(target_project_id) INTO targets FROM arti_project_publications WHERE project_id=NEW.id AND target_project_id IS NOT NULL;
        UPDATE arti_project_publications SET payload=NULL,status='revoked' WHERE project_id=NEW.id OR target_project_id=NEW.id;
        UPDATE arti_project_revisions SET payload=NULL WHERE project_id=NEW.id;
        UPDATE arti_project_result_candidates SET reason=NULL WHERE project_id=NEW.id;
        UPDATE arti_projects SET status='deleted',payload=NULL,access_generation=access_generation+1 WHERE id=ANY(targets) AND status<>'deleted';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER project_payload_cleanup AFTER UPDATE OF status ON arti_projects FOR EACH ROW EXECUTE FUNCTION arti_project_payload_cleanup();
