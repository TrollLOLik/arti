CREATE TABLE arti_workflow_objects (
 id TEXT PRIMARY KEY,realm TEXT NOT NULL,owner_id BIGINT NOT NULL,project_id TEXT NOT NULL REFERENCES arti_projects(id),
 kind TEXT NOT NULL,revision BIGINT NOT NULL DEFAULT 1,head TEXT NOT NULL REFERENCES material_derivatives(id),
 accepted TEXT REFERENCES material_derivatives(id),status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE arti_workflow_versions (
 object_id TEXT REFERENCES arti_workflow_objects(id),revision BIGINT,head TEXT REFERENCES material_derivatives(id),
 author_id BIGINT NOT NULL,PRIMARY KEY(object_id,revision)
);
CREATE TABLE arti_subscription_runs (
 subscription_id TEXT REFERENCES arti_workflow_objects(id),revision BIGINT,occurrence TIMESTAMPTZ,
 task_id TEXT REFERENCES arti_tasks(id),status TEXT NOT NULL DEFAULT 'pending',fingerprint TEXT,delivery_key TEXT,
 PRIMARY KEY(subscription_id,revision,occurrence)
);
CREATE TABLE arti_subscription_cursor (
 subscription_id TEXT PRIMARY KEY REFERENCES arti_workflow_objects(id),revision BIGINT,next_at TIMESTAMPTZ NOT NULL,
 fingerprint TEXT,last_run TIMESTAMPTZ,paused_reason TEXT
);
CREATE FUNCTION workflow_project_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.status='deleted' AND OLD.status<>'deleted' THEN
  UPDATE arti_workflow_objects SET status='deleted' WHERE project_id=NEW.id;
  UPDATE material_derivatives SET payload=NULL,invalidated_at=NOW() WHERE id IN(
   SELECT v.head FROM arti_workflow_versions v JOIN arti_workflow_objects o ON v.object_id=o.id WHERE o.project_id=NEW.id);
  UPDATE arti_subscription_cursor SET fingerprint=NULL,paused_reason='project_deleted' WHERE subscription_id IN(SELECT id FROM arti_workflow_objects WHERE project_id=NEW.id);
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER workflow_project_cleanup AFTER UPDATE ON arti_projects FOR EACH ROW EXECUTE FUNCTION workflow_project_cleanup();
CREATE FUNCTION workflow_source_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.invalidated_at IS NOT NULL OR NEW.payload IS NULL THEN
  UPDATE arti_workflow_objects SET status='paused' WHERE head=NEW.id AND status='active';
  UPDATE arti_subscription_cursor SET fingerprint=NULL,paused_reason='source_unavailable' WHERE subscription_id IN(SELECT id FROM arti_workflow_objects WHERE head=NEW.id);
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER workflow_source_cleanup AFTER UPDATE ON material_derivatives FOR EACH ROW EXECUTE FUNCTION workflow_source_cleanup();
