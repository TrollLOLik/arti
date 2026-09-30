CREATE TABLE arti_artifacts (
 id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES arti_projects(id),revision BIGINT NOT NULL DEFAULT 1,
 head TEXT NOT NULL REFERENCES material_derivatives(id),accepted TEXT REFERENCES material_derivatives(id)
);
CREATE TABLE arti_artifact_revisions (
 artifact_id TEXT REFERENCES arti_artifacts(id),revision BIGINT,derivative_id TEXT REFERENCES material_derivatives(id),
 author_id BIGINT NOT NULL,status TEXT NOT NULL CHECK(status IN ('proposed','accepted','rejected')),reason TEXT,
 PRIMARY KEY(artifact_id,revision)
);
CREATE FUNCTION artifact_source_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.payload IS NULL OR NEW.invalidated_at IS NOT NULL THEN
  UPDATE arti_artifact_revisions SET reason=NULL WHERE derivative_id=NEW.id;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER artifact_source_cleanup AFTER UPDATE ON material_derivatives FOR EACH ROW EXECUTE FUNCTION artifact_source_cleanup();
