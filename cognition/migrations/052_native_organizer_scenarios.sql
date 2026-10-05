-- Native organizer clarification survives restarts, but not reset or source erasure.
-- Item schema is initialized before cognition. Keep upgrades safe for existing DBs.
ALTER TABLE IF EXISTS arti_organizer_items ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 1;

-- Native reply snapshots may have no cognitive event (e.g. /todo-created
-- items). Revoke their exact ordinary transport jobs using content-free links.
CREATE OR REPLACE FUNCTION arti_organizer_invalidate_cognitive(request_ids TEXT[]) RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE
    outboxes BIGINT[];
    affected BIGINT[];
    delivered BIGINT[];
    legacy BIGINT[];
    context_row RECORD;
BEGIN
    -- Exact durable delivery namespaces, never a search for private title text.
    SELECT ARRAY_AGG(DISTINCT o.id) INTO outboxes FROM cognitive_outbox o
      JOIN cognitive_events cause ON cause.id=o.event_id AND cause.context_id=o.context_id
      JOIN cognitive_contexts c ON c.id=o.context_id
      JOIN arti_requests r ON r.id=ANY(request_ids) AND r.chat_id=c.chat_id
      WHERE c.persona_id='arti' AND c.chat_id>0 AND c.topic_id<0 AND c.mode='default' AND c.scene_id=''
        AND cause.owner_id=c.chat_id AND starts_with(o.delivery_key,cause.event_key || ':' || r.id || ':');
    IF outboxes IS NULL THEN RETURN; END IF;
    -- Taking the context fence before organizer/request rows is the global order.
    -- Callers already hold this fence; repeat it for standalone upgrade cleanup.
    PERFORM id FROM cognitive_contexts WHERE id IN
      (SELECT context_id FROM cognitive_outbox WHERE id=ANY(outboxes)) ORDER BY id FOR UPDATE;
    WITH RECURSIVE descendants(id) AS (
      SELECT e.id FROM cognitive_outbox o JOIN cognitive_contexts c ON c.id=o.context_id
        JOIN cognitive_events e ON e.context_id=o.context_id AND e.owner_id=c.chat_id
          AND e.source_id='telegram:' || c.chat_id::text || ':' || o.receipt_id::text || ':delivered_action'
        WHERE o.id=ANY(outboxes)
      UNION SELECT child.id FROM cognitive_event_dependencies d JOIN descendants parent ON parent.id=d.source_event_id
        JOIN cognitive_events child ON child.id=d.event_id JOIN cognitive_contexts c ON c.id=child.context_id
        WHERE child.owner_id=c.chat_id AND c.chat_id>0 AND c.mode='default' AND c.topic_id<0 AND c.scene_id=''
    ) SELECT COALESCE(ARRAY_AGG(id),ARRAY[]::BIGINT[]) INTO affected FROM descendants;
    SELECT COALESCE(ARRAY_AGG(id),ARRAY[]::BIGINT[]) INTO delivered FROM cognitive_events
      WHERE id=ANY(affected) AND origin='delivered_action';
    SELECT COALESCE(ARRAY_AGG(legacy_id),ARRAY[]::BIGINT[]) INTO legacy FROM cognitive_legacy_map
      WHERE source_table='memory_messages' AND event_id=ANY(delivered);
    -- Source history has a precise transported-event timestamp/text association.
    DELETE FROM chat_history h USING cognitive_events e,cognitive_contexts c
      WHERE e.id=ANY(delivered) AND c.id=e.context_id AND h.chat_id=c.chat_id AND h.user_name='Арти'
        AND h.timestamp=e.observed_at AT TIME ZONE 'UTC' AND h.message_text=e.payload->>'text';
    DELETE FROM memory_chunks WHERE message_ids && legacy;
    DELETE FROM memory_timelines WHERE source_message_ids && legacy;
    DELETE FROM memory_facts WHERE source_message_id=ANY(legacy);
    DELETE FROM memory_messages WHERE id=ANY(legacy);
    UPDATE cognitive_legacy_map SET status='suppressed' WHERE source_table='memory_messages' AND event_id=ANY(delivered);
    UPDATE cognitive_artifacts SET suppressed_at=NOW(),payload=NULL WHERE id IN
      (SELECT artifact_id FROM cognitive_provenance WHERE source_event_id=ANY(affected));
    DELETE FROM cognitive_effects WHERE event_id=ANY(affected);
    UPDATE cognitive_reappraisals SET perception=NULL WHERE cause_event_id=ANY(affected) OR support_event_id=ANY(affected);
    UPDATE cognitive_events SET perception=NULL WHERE id=ANY(affected) AND origin='user';
    UPDATE cognitive_events SET suppressed_at=NOW(),payload=NULL,perception=NULL,fingerprint=NULL
      WHERE id=ANY(delivered) AND suppressed_at IS NULL;
    UPDATE cognitive_jobs SET status=CASE WHEN event_id=ANY(delivered) THEN 'cancelled' ELSE 'pending' END,
      attempts=0,available_at=NOW(),lease_token=NULL,lease_until=NULL,last_error_code=NULL WHERE event_id=ANY(affected);
    -- A late receipt cannot resurrect a delivery after this exact outbox fence.
    UPDATE cognitive_outbox SET status='cancelled',payload=NULL,updated_at=NOW()
      WHERE id=ANY(outboxes) OR event_id=ANY(affected);
    FOR context_row IN SELECT DISTINCT c.id,c.chat_id FROM cognitive_contexts c
      JOIN cognitive_outbox o ON o.context_id=c.id WHERE o.id=ANY(outboxes)
    LOOP
      IF cardinality(delivered)>0 THEN
        UPDATE cognitive_contexts SET rebuilding=TRUE,suppression_epoch=suppression_epoch+1,
          worker_token=NULL,worker_lease_until=NULL WHERE id=context_row.id;
        -- The original list/query remains a live user event and safely anchors
        -- restart recovery. No invented user message or source is introduced.
        INSERT INTO cognitive_jobs(context_id,event_id,kind,status,last_error_code)
          SELECT context_row.id,MIN(o.event_id),'rebuild','pending','native_source_erased'
          FROM cognitive_outbox o JOIN cognitive_events e ON e.id=o.event_id
          WHERE o.id=ANY(outboxes) AND o.context_id=context_row.id AND e.suppressed_at IS NULL
          HAVING COUNT(*)>0
          ON CONFLICT(context_id,event_id,kind) DO UPDATE SET status='pending',attempts=0,
            lease_token=NULL,lease_until=NULL,available_at=NOW(),last_error_code='native_source_erased';
        -- These are legacy derived caches with incomplete source attribution.
        DELETE FROM memory_user_profiles WHERE chat_id=context_row.chat_id AND user_id=context_row.chat_id AND mode='default';
        DELETE FROM memory_wiki_pages WHERE chat_id=context_row.chat_id AND mode='default' AND NOT is_default;
        DELETE FROM memory_entities WHERE chat_id=context_row.chat_id;
      END IF;
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION arti_organizer_invalidate_requests(request_ids TEXT[], reason TEXT) RETURNS VOID
LANGUAGE plpgsql AS $$
BEGIN
    IF request_ids IS NULL OR array_length(request_ids,1) IS NULL THEN RETURN; END IF;
    PERFORM c.id FROM cognitive_contexts c WHERE c.persona_id='arti' AND c.chat_id>0 AND c.topic_id<0
      AND c.mode='default' AND c.scene_id='' AND c.chat_id IN
        (SELECT chat_id FROM arti_requests WHERE id=ANY(request_ids)) ORDER BY c.id FOR UPDATE;
    IF reason IN ('source_erased','organizer_upgrade') THEN
      PERFORM arti_organizer_invalidate_cognitive(request_ids);
    END IF;
    PERFORM id FROM arti_requests WHERE id=ANY(request_ids) ORDER BY seq FOR UPDATE;
    UPDATE arti_requests r SET
      state=CASE WHEN r.state IN ('queued','running','paused') THEN
        CASE WHEN EXISTS(SELECT 1 FROM arti_request_sends s WHERE s.request_id=r.id AND s.ordinal>0
          AND s.state IN ('sending','delivery_unknown')) THEN 'delivery_unknown' ELSE 'cancelled' END
        ELSE r.state END,
      payload='{}',checkpoints='{}',error_code=reason,token=NULL,lease_until=NULL,updated_at=NOW()
      WHERE r.id=ANY(request_ids);
    UPDATE arti_request_sends SET payload='{}',receipt=NULL,
      state=CASE WHEN state='sending' THEN 'delivery_unknown' WHEN state='prepared' THEN 'cancelled' ELSE state END,
      updated_at=NOW() WHERE request_id=ANY(request_ids);
    UPDATE arti_media_retained SET descriptor='{}',invalidated_at=COALESCE(invalidated_at,NOW())
      WHERE request_id=ANY(request_ids);
    UPDATE arti_request_resources SET state='cleanup_pending',updated_at=NOW()
      WHERE request_id=ANY(request_ids) AND state='bound';
END $$;

CREATE OR REPLACE FUNCTION arti_organizer_cancel_dialogue(native_owner BIGINT) RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE request_ids TEXT[];
BEGIN
    IF native_owner IS NULL OR native_owner<=0 OR to_regclass('arti_organizer_turns') IS NULL THEN RETURN; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('organizer:' || native_owner::text,0));
    INSERT INTO arti_organizer_dialogue_state(owner_id,generation) VALUES(native_owner,1)
      ON CONFLICT(owner_id) DO UPDATE SET generation=arti_organizer_dialogue_state.generation+1;
    WITH cancelled AS (
      UPDATE arti_organizer_turns SET state='cancelled',outcome=NULL
        WHERE owner_id=native_owner AND state='pending' RETURNING source_key
    ) SELECT ARRAY_AGG(DISTINCT l.request_id) INTO request_ids FROM arti_organizer_request_links l
      JOIN cancelled c ON c.source_key=l.source_key WHERE l.owner_id=native_owner;
    PERFORM arti_organizer_invalidate_requests(request_ids,'organizer_reset');
    DELETE FROM arti_organizer_pending WHERE owner_id=native_owner;
    DELETE FROM arti_organizer_reply_refs WHERE owner_id=native_owner;
END $$;

CREATE OR REPLACE FUNCTION arti_organizer_erase_native_source(native_owner BIGINT, native_source TEXT) RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE
    base_source TEXT;
    roots TEXT[];
    items TEXT[];
    request_ids TEXT[];
BEGIN
    IF native_owner IS NULL OR native_owner<=0 OR to_regclass('arti_organizer_turns') IS NULL THEN RETURN; END IF;
    IF native_source !~ ('^telegram:' || native_owner::text || ':[1-9][0-9]*(:user)?$') THEN RETURN; END IF;
    base_source=regexp_replace(native_source,':user$','');
    PERFORM id FROM cognitive_contexts WHERE persona_id='arti' AND chat_id=native_owner AND topic_id<0
      AND mode='default' AND scene_id='' ORDER BY id FOR UPDATE;
    PERFORM pg_advisory_xact_lock(hashtextextended('organizer:' || native_owner::text,0));
    INSERT INTO arti_organizer_dialogue_state(owner_id,generation) VALUES(native_owner,1)
      ON CONFLICT(owner_id) DO UPDATE SET generation=arti_organizer_dialogue_state.generation+1;
    SELECT ARRAY_AGG(DISTINCT source) INTO roots FROM (
      SELECT base_source AS source
      UNION SELECT root_source_key FROM arti_organizer_turns WHERE owner_id=native_owner AND source_key=base_source
      UNION SELECT regexp_replace(root_source_key,':user$','') FROM arti_organizer_source_routes
        WHERE owner_id=native_owner AND source_key=base_source || ':user'
    ) r;
    SELECT COALESCE(ARRAY_AGG(id),ARRAY[]::TEXT[]) INTO items FROM arti_organizer_items
      WHERE owner_id=native_owner AND (source_key=ANY(roots) OR id IN
        (SELECT item_id FROM arti_organizer_actions WHERE owner_id=native_owner AND source_key=ANY(roots)));
    SELECT roots || COALESCE(ARRAY_AGG(source_key),ARRAY[]::TEXT[]) INTO roots FROM arti_organizer_pending
      WHERE owner_id=native_owner AND (payload->>'item_id'=ANY(items) OR EXISTS
        (SELECT 1 FROM jsonb_array_elements(COALESCE(payload->'candidates','[]'::jsonb)) candidate
          WHERE candidate->>'id'=ANY(items)));
    -- Scrub any pending edit of an erased object as well as its creation dialogue.
    INSERT INTO arti_organizer_erased_sources(owner_id,source_key)
      SELECT native_owner,unnest(roots)
      UNION SELECT native_owner,source_key FROM arti_organizer_items WHERE owner_id=native_owner AND id=ANY(items)
      UNION SELECT native_owner,source_key FROM arti_organizer_actions WHERE owner_id=native_owner AND item_id=ANY(items)
      UNION SELECT native_owner,source_key FROM arti_organizer_turns WHERE owner_id=native_owner AND (root_source_key=ANY(roots) OR item_id=ANY(items))
      UNION SELECT native_owner,source_key FROM arti_organizer_pending WHERE owner_id=native_owner AND payload->>'item_id'=ANY(items)
      ON CONFLICT DO NOTHING;
    WITH erased AS (UPDATE arti_organizer_turns SET state='erased',outcome=NULL,item_id=NULL
      WHERE owner_id=native_owner AND (root_source_key=ANY(roots) OR item_id=ANY(items) OR EXISTS
        (SELECT 1 FROM jsonb_array_elements(COALESCE(outcome->'items','[]'::jsonb)) ref WHERE ref->>'id'=ANY(items)) OR source_key IN
        (SELECT source_key FROM arti_organizer_erased_sources WHERE owner_id=native_owner)) RETURNING source_key
    ) SELECT ARRAY_AGG(DISTINCT l.request_id) INTO request_ids FROM arti_organizer_request_links l
      JOIN erased e ON e.source_key=l.source_key WHERE l.owner_id=native_owner;
    PERFORM arti_organizer_invalidate_requests(request_ids,'source_erased');
    DELETE FROM arti_organizer_pending WHERE owner_id=native_owner AND (source_key=ANY(roots) OR payload->>'item_id'=ANY(items));
    DELETE FROM arti_organizer_reply_refs WHERE owner_id=native_owner AND item_id=ANY(items);
    DELETE FROM arti_organizer_items WHERE owner_id=native_owner AND id=ANY(items);
END $$;

CREATE OR REPLACE FUNCTION arti_organizer_event_erased() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.suppressed_at IS NOT NULL AND OLD.suppressed_at IS NULL AND EXISTS
      (SELECT 1 FROM cognitive_contexts WHERE id=NEW.context_id AND persona_id='arti'
        AND chat_id=NEW.owner_id AND topic_id<0 AND mode='default' AND scene_id='') THEN
        PERFORM arti_organizer_erase_native_source(NEW.owner_id,NEW.source_id);
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS arti_organizer_event_erased ON cognitive_events;
CREATE TRIGGER arti_organizer_event_erased AFTER UPDATE OF suppressed_at ON cognitive_events
FOR EACH ROW EXECUTE FUNCTION arti_organizer_event_erased();

CREATE OR REPLACE FUNCTION arti_organizer_history_reset() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.chat_id>0 AND NEW.persona_id='arti' AND NEW.topic_id<0 AND NEW.mode='default' AND NEW.scene_id='' THEN
        PERFORM arti_organizer_cancel_dialogue(NEW.chat_id);
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS arti_organizer_history_reset ON cognitive_contexts;
-- Deliberately fires on an explicit reset even if the history cursor is unchanged.
CREATE TRIGGER arti_organizer_history_reset AFTER UPDATE OF history_after_event_id ON cognitive_contexts
FOR EACH ROW EXECUTE FUNCTION arti_organizer_history_reset();

CREATE OR REPLACE FUNCTION arti_organizer_source_tombstoned() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.scope_key=encode(sha256(convert_to('["arti",' || NEW.owner_id::text || ',-1,"default",""]','UTF8')),'hex') THEN
        PERFORM arti_organizer_erase_native_source(NEW.owner_id,NEW.source_id);
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS arti_organizer_source_tombstoned ON material_source_tombstones;
CREATE TRIGGER arti_organizer_source_tombstoned AFTER INSERT ON material_source_tombstones
FOR EACH ROW EXECUTE FUNCTION arti_organizer_source_tombstoned();

-- Upgrade old installations too: previously forgotten sources must not leave a
-- native notification waiting to send merely because its erasure predates us.
DO $$
DECLARE old_source RECORD;
BEGIN
    FOR old_source IN
      SELECT e.owner_id,e.source_id FROM cognitive_events e JOIN cognitive_contexts c ON c.id=e.context_id
        WHERE e.suppressed_at IS NOT NULL AND e.owner_id>0 AND c.persona_id='arti'
          AND c.chat_id=e.owner_id AND c.topic_id<0 AND c.mode='default' AND c.scene_id=''
      UNION
      SELECT owner_id,source_id FROM material_source_tombstones
        WHERE owner_id>0 AND scope_key=encode(sha256(convert_to('["arti",' || owner_id::text || ',-1,"default",""]','UTF8')),'hex')
    LOOP
      PERFORM arti_organizer_erase_native_source(old_source.owner_id,old_source.source_id);
    END LOOP;
END $$;

-- Old code did not persist displayed-item dependencies. Its native checkpoints
-- cannot safely be attributed to one source after an upgrade. Invalidate only
-- these explicitly tagged, unlinked private native replies, not ordinary work
-- or the underlying saved tasks/reminders.
DO $$
DECLARE legacy_requests TEXT[];
BEGIN
    IF to_regclass('arti_requests') IS NOT NULL AND to_regclass('arti_organizer_request_links') IS NOT NULL THEN
      SELECT ARRAY_AGG(r.id) INTO legacy_requests FROM arti_requests r
        WHERE r.kind='text' AND r.chat_id>0 AND r.topic_id<0 AND r.state IN ('queued','running','paused')
          AND r.checkpoints ? 'organizer_result'
          AND NOT EXISTS(SELECT 1 FROM arti_organizer_request_links l WHERE l.request_id=r.id);
      PERFORM arti_organizer_invalidate_requests(legacy_requests,'organizer_upgrade');
    END IF;
END $$;
