ALTER TABLE arti_work_actions ADD COLUMN task_id TEXT REFERENCES arti_tasks(id);
ALTER TABLE arti_work_actions ADD CONSTRAINT arti_action_target CHECK((artifact_id IS NOT NULL)::int+(task_id IS NOT NULL)::int=1);
