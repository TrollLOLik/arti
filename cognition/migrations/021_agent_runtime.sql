CREATE TABLE arti_tasks (
 id TEXT PRIMARY KEY,realm TEXT NOT NULL,scope JSONB NOT NULL,owner_id BIGINT NOT NULL,
 project_id TEXT NOT NULL REFERENCES arti_projects(id),access_generation BIGINT NOT NULL,
 plan_id TEXT REFERENCES material_derivatives(id),status TEXT NOT NULL DEFAULT 'queued',revision BIGINT NOT NULL DEFAULT 1,
 fence BIGINT NOT NULL DEFAULT 0,lease_token TEXT,lease_until TIMESTAMPTZ,
 max_calls INTEGER NOT NULL,max_cost NUMERIC NOT NULL,max_bytes BIGINT NOT NULL,deadline TIMESTAMPTZ NOT NULL,
 used_calls INTEGER NOT NULL DEFAULT 0,used_cost NUMERIC NOT NULL DEFAULT 0,used_bytes BIGINT NOT NULL DEFAULT 0,
 diagnostics TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX arti_task_claim ON arti_tasks(status,lease_until);
CREATE TABLE arti_task_calls (
 task_id TEXT REFERENCES arti_tasks(id),step_id TEXT,attempt INTEGER,fence BIGINT NOT NULL,
 tool TEXT NOT NULL,version TEXT NOT NULL,input_digest TEXT NOT NULL,effect TEXT NOT NULL,
 status TEXT NOT NULL,output_id TEXT REFERENCES material_derivatives(id),receipt JSONB,
 reserved_cost NUMERIC NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(task_id,step_id,attempt)
);
CREATE TABLE arti_capability_grants (
 id TEXT PRIMARY KEY,realm TEXT NOT NULL,owner_id BIGINT NOT NULL,scope_key TEXT NOT NULL,
 tools TEXT[] NOT NULL,resources TEXT[] NOT NULL,audience TEXT NOT NULL,recipient TEXT NOT NULL,
 content_digest TEXT NOT NULL,max_cost NUMERIC NOT NULL,expires_at TIMESTAMPTZ NOT NULL,revoked_at TIMESTAMPTZ,
 request_id TEXT NOT NULL,UNIQUE(realm,owner_id,request_id,content_digest)
);
CREATE TABLE arti_work_actions (
 id TEXT PRIMARY KEY,realm TEXT NOT NULL,owner_id BIGINT NOT NULL,scope_key TEXT NOT NULL,
 artifact_id TEXT REFERENCES arti_artifacts(id),revision BIGINT NOT NULL,action TEXT NOT NULL,
 expires_at TIMESTAMPTZ NOT NULL,consumed_at TIMESTAMPTZ
);
CREATE TABLE arti_work_delivery (
 delivery_key TEXT PRIMARY KEY,realm TEXT NOT NULL,project_id TEXT REFERENCES arti_projects(id),
 target_id TEXT NOT NULL,target_revision BIGINT NOT NULL,status TEXT NOT NULL,receipt BIGINT,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE FUNCTION agent_source_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.invalidated_at IS NOT NULL OR NEW.payload IS NULL THEN
  UPDATE arti_tasks SET status='cancelled',revision=revision+1,lease_token=NULL,lease_until=NULL,diagnostics='source_unavailable'
   WHERE plan_id=NEW.id AND status NOT IN ('cancelled','succeeded','failed');
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER agent_source_cleanup AFTER UPDATE ON material_derivatives FOR EACH ROW EXECUTE FUNCTION agent_source_cleanup();
CREATE FUNCTION agent_project_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.status='deleted' AND OLD.status<>'deleted' THEN
  UPDATE arti_tasks SET status='cancelled',revision=revision+1,lease_token=NULL,lease_until=NULL,diagnostics='project_deleted' WHERE project_id=NEW.id;
  UPDATE material_derivatives SET payload=NULL,invalidated_at=NOW() WHERE id IN (
   SELECT r.derivative_id FROM arti_artifact_revisions r JOIN arti_artifacts a ON a.id=r.artifact_id WHERE a.project_id=NEW.id
   UNION SELECT plan_id FROM arti_tasks WHERE project_id=NEW.id
   UNION SELECT c.output_id FROM arti_task_calls c JOIN arti_tasks t ON c.task_id=t.id WHERE t.project_id=NEW.id
  );
  UPDATE arti_task_calls SET receipt=NULL WHERE task_id IN(SELECT id FROM arti_tasks WHERE project_id=NEW.id);
  UPDATE arti_work_delivery SET status='cancelled',receipt=NULL WHERE project_id=NEW.id;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER agent_project_cleanup AFTER UPDATE ON arti_projects FOR EACH ROW EXECUTE FUNCTION agent_project_cleanup();
