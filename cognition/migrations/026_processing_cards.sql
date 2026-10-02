CREATE TABLE arti_processing_cards (
 task_id TEXT PRIMARY KEY REFERENCES arti_tasks(id),realm TEXT NOT NULL,owner_id BIGINT NOT NULL,
 scope_key TEXT NOT NULL,message_id BIGINT,content_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN('active','unknown','gone','revoked')),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE FUNCTION processing_card_source_cleanup() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.invalidated_at IS NOT NULL OR NEW.payload IS NULL THEN
  UPDATE arti_processing_cards SET status='revoked',message_id=NULL
    WHERE task_id IN(SELECT id FROM arti_tasks WHERE plan_id=NEW.id);
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER processing_card_source_cleanup AFTER UPDATE ON material_derivatives
 FOR EACH ROW EXECUTE FUNCTION processing_card_source_cleanup();
