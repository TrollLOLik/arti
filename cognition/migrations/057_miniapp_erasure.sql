-- Projection erasure follows the existing context -> conversation lock order.
CREATE FUNCTION app_scrub_projection(p_context BIGINT, p_event BIGINT DEFAULT NULL) RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE c UUID; e UUID; s BIGINT; r BIGINT; jobs TEXT[];
BEGIN
  FOR c IN SELECT DISTINCT conversation_id FROM app_messages
    WHERE cognitive_context_id=p_context AND (p_event IS NULL OR cognitive_event_id=p_event OR cognitive_event_id IN (SELECT event_id FROM cognitive_event_dependencies WHERE context_id=p_context AND source_event_id=p_event))
    UNION SELECT b.conversation_id FROM app_context_bindings b JOIN cognitive_contexts ctx
      ON ctx.chat_id=b.telegram_chat_id AND ctx.persona_id='arti' AND ctx.topic_id=-1 AND ctx.mode='default' AND ctx.scene_id=''
      WHERE ctx.id=p_context AND p_event IS NULL
    ORDER BY conversation_id LOOP
    PERFORM id FROM app_conversations WHERE id=c FOR UPDATE;
    SELECT array_agg(DISTINCT request_id) INTO jobs FROM app_messages WHERE conversation_id=c
      AND request_id IS NOT NULL AND (p_event IS NULL OR cognitive_event_id=p_event
        OR cognitive_event_id IN (SELECT event_id FROM cognitive_event_dependencies WHERE context_id=p_context AND source_event_id=p_event));
    IF to_regclass('arti_requests') IS NOT NULL THEN
      PERFORM arti_organizer_invalidate_requests(jobs,'source_erased');
    END IF;
    UPDATE app_deliveries d SET state=CASE WHEN d.state='sending' THEN 'delivery_unknown' ELSE 'cancelled' END,
        payload=NULL,token=NULL,updated_at=now()
      FROM app_messages m WHERE m.id=d.message_id AND m.conversation_id=c
        AND (p_event IS NULL OR m.cognitive_event_id=p_event OR m.cognitive_event_id IN (SELECT event_id FROM cognitive_event_dependencies WHERE context_id=p_context AND source_event_id=p_event)) AND d.state IN ('prepared','sending');
    UPDATE app_messages SET text='[Удалено]',suppressed_at=COALESCE(suppressed_at,now())
      WHERE conversation_id=c AND (p_event IS NULL OR cognitive_event_id=p_event OR cognitive_event_id IN (SELECT event_id FROM cognitive_event_dependencies WHERE context_id=p_context AND source_event_id=p_event));
    UPDATE app_conversations SET history_revision=history_revision+1,event_seq=event_seq+1
      WHERE id=c RETURNING event_seq,access_revision INTO s,r;
    e:=gen_random_uuid();
    INSERT INTO app_events(id,conversation_id,seq,kind,access_revision) VALUES(e,c,s,'history.reset',r);
    INSERT INTO app_outbox(event_id) VALUES(e);
  END LOOP;
END $$;
CREATE FUNCTION app_event_erased() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.suppressed_at IS NULL AND NEW.suppressed_at IS NOT NULL THEN
    PERFORM app_scrub_projection(NEW.context_id,NEW.id);
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER app_cognitive_erased AFTER UPDATE OF suppressed_at ON cognitive_events
  FOR EACH ROW EXECUTE FUNCTION app_event_erased();
CREATE FUNCTION app_context_reset() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  -- An explicit reset is a new generation even when the transcript is empty.
  PERFORM app_scrub_projection(NEW.id);
  RETURN NEW;
END $$;
CREATE TRIGGER app_cognitive_reset AFTER UPDATE OF history_after_event_id ON cognitive_contexts
  FOR EACH ROW EXECUTE FUNCTION app_context_reset();
