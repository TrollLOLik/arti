-- Extend the existing erasure function to authored Mini App sources.
CREATE OR REPLACE FUNCTION arti_organizer_erase_native_source(native_owner BIGINT, native_source TEXT) RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE
    base_source TEXT;
    roots TEXT[];
    items TEXT[];
    request_ids TEXT[];
BEGIN
    IF native_owner IS NULL OR native_owner<=0 OR to_regclass('arti_organizer_turns') IS NULL THEN RETURN; END IF;
    IF native_source !~ ('^telegram:' || native_owner::text || ':[1-9][0-9]*(:user)?$') AND native_source !~ '^miniapp:[0-9a-f-]{36}:[0-9a-f-]{36}(:user)?$' THEN RETURN; END IF;
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
