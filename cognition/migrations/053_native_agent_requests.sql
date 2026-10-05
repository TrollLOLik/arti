-- Original source selection survives ordinary request retries and task replanning.
-- Request text and source IDs live only in an erasable, source-bound derivative.
CREATE TABLE arti_native_agent_requests (
 id TEXT PRIMARY KEY,
 realm TEXT NOT NULL,
 owner_id BIGINT NOT NULL,
 project_id TEXT NOT NULL REFERENCES arti_projects(id),
 access_generation BIGINT NOT NULL,
 binding_id TEXT NOT NULL REFERENCES material_derivatives(id),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE arti_tasks ADD COLUMN native_request_id TEXT REFERENCES arti_native_agent_requests(id);
ALTER TABLE arti_tasks ADD COLUMN native_origin_plan_id TEXT REFERENCES material_derivatives(id);
CREATE FUNCTION native_agent_identity_immutable() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
 IF NEW IS DISTINCT FROM OLD THEN
  RAISE EXCEPTION 'native_agent_identity_immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER native_agent_identity_immutable BEFORE UPDATE ON arti_native_agent_requests
 FOR EACH ROW EXECUTE FUNCTION native_agent_identity_immutable();
CREATE FUNCTION native_agent_task_binding_immutable() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.native_request_id IS DISTINCT FROM OLD.native_request_id OR NEW.native_origin_plan_id IS DISTINCT FROM OLD.native_origin_plan_id THEN
  RAISE EXCEPTION 'native_agent_task_binding_immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER native_agent_task_binding_immutable BEFORE UPDATE ON arti_tasks
 FOR EACH ROW EXECUTE FUNCTION native_agent_task_binding_immutable();

-- A project deletion also erases the immutable native request/original plan.
CREATE FUNCTION native_agent_project_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.status='deleted' AND OLD.status<>'deleted' THEN
  UPDATE material_derivatives SET payload=NULL,invalidated_at=NOW() WHERE id IN (
   SELECT binding_id FROM arti_native_agent_requests WHERE project_id=NEW.id
   UNION SELECT native_origin_plan_id FROM arti_tasks WHERE project_id=NEW.id
  );
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER native_agent_project_cleanup AFTER UPDATE ON arti_projects
 FOR EACH ROW EXECUTE FUNCTION native_agent_project_cleanup();
