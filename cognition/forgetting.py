"""Cross-store source erasure and conservative invalidation of legacy derivatives."""
from cognition.serialization import object_value
from cognition.types import Perception
from cognition.repositories import CognitiveRepository
from cognition.memory_repository import MemoryRepository


async def rebuild_allowed(pool,cid):
    async with pool.acquire() as conn:
        rows = await conn.fetch('''SELECT id,perception FROM cognitive_events WHERE context_id=$1 AND suppressed_at IS NULL
            AND perception IS NOT NULL ORDER BY observed_at,id''',cid)
        allowed_sources = set(await conn.fetchval('SELECT ARRAY_AGG(source_id) FROM cognitive_events WHERE context_id=$1 AND suppressed_at IS NULL',cid) or [])
    perceptions = []
    from dataclasses import replace
    for row in rows:
        p = Perception.from_dict(object_value(row['perception']))
        if p.situation:
            # Deleted causes cannot remain in a surviving explanation's registry.
            p = replace(p,situation=replace(p.situation,revisions=tuple(r for r in p.situation.revisions if r['source_id'] in allowed_sources)))
        perceptions.append((row['id'],p))
    await MemoryRepository(pool).rebuild(cid,perceptions)
    from cognition.reappraisal import ReappraisalRepository
    revisions = ReappraisalRepository(pool)
    for eid,p in perceptions:
        for revision in p.situation.revisions if p.situation else ():
            await revisions.reinterpret_trace(cid,revision['source_id'],eid,revision['interpretation'],revision['confidence'])
    async with pool.acquire() as conn,conn.transaction():
        await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
        owners = await conn.fetchval('SELECT ARRAY_AGG(DISTINCT owner_id) FROM cognitive_events WHERE context_id=$1 AND suppressed_at IS NULL AND perception IS NOT NULL',cid) or []
        for owner in owners:
            await revisions.rebuild_relationship_locked(conn,cid,owner)


async def forget_cognitive_sources(pool,cid,owner,sources):
    # A session lock serializes rebuilds while the persisted barrier keeps all
    # ordinary workers and deliveries out of partially reconstructed projections.
    async with pool.acquire() as lock:
        await lock.execute("SELECT pg_advisory_lock(hashtext('cognition-rebuild')::int,$1::int)",cid)
        try:
            context = await lock.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1',cid)
            if context:
                from materials.lifecycle import forget_sources
                from materials.types import context_identity
                identity = context_identity(context['persona_id'],context['chat_id'],context['topic_id'],context['mode'],context['scene_id'])
                await forget_sources(pool,identity,owner,sources)
            owned = await lock.fetchval('SELECT 1 FROM cognitive_events WHERE context_id=$1 AND owner_id=$2 AND source_id=ANY($3::text[])',cid,owner,list(set(sources)))
            if not owned:
                return dict(events=0,artifacts=0)
            from bot.request_store import RequestStore
            request_sources = await lock.fetch('SELECT id FROM cognitive_events WHERE context_id=$1 AND owner_id=$2 AND source_id=ANY($3::text[])', cid, owner, list(set(sources)))
            # Persist recovery before raising the barrier or erasing any source.
            # A process crash therefore cannot strand a half-built context.
            await schedule_rebuild(pool,cid,owner)
            async with lock.transaction():
                current = await lock.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
                if not current or not owned:
                    return dict(events=0,artifacts=0)
                await lock.execute('UPDATE cognitive_contexts SET rebuilding=TRUE,suppression_epoch=suppression_epoch+1,worker_token=NULL,worker_lease_until=NULL WHERE id=$1',cid)
                await RequestStore(None).erase_sources([row['id'] for row in request_sources], lock)
                await lock.execute("UPDATE cognitive_jobs SET status='pending',lease_token=NULL,lease_until=NULL,available_at=NOW() WHERE context_id=$1 AND status='running'",cid)
            try:
                total = dict(events=0,artifacts=0)
                repo = CognitiveRepository(pool)
                for source in set(sources):
                    result = await repo.forget(cid,source,owner)
                    for name in total:
                        total[name] += result[name]
                await finish_rebuild(pool,cid)
                return total
            except BaseException:
                await schedule_rebuild(pool,cid,owner)
                raise
        finally:
            await lock.execute("SELECT pg_advisory_unlock(hashtext('cognition-rebuild')::int,$1::int)",cid)


async def schedule_rebuild(pool,cid,owner):
    """A payload-free control observation can recover even an empty context."""
    from datetime import datetime,timezone
    from cognition.types import CognitiveEvent,ContextKey,EvidenceRef,Origin
    from cognition.jobs import JobQueue
    async with pool.acquire() as conn:
        ctx = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1',cid)
        source = f'control:rebuild:{cid}:{ctx["suppression_epoch"]}:{owner}'
        existing = await conn.fetchval('SELECT id FROM cognitive_events WHERE context_id=$1 AND event_key=$2',cid,source)
    if existing is None:
        at = datetime.now(timezone.utc)
        event = CognitiveEvent(source,ContextKey(ctx['persona_id'],ctx['chat_id'],ctx['mode'],ctx['scene_id'],ctx['topic_id']),
            EvidenceRef(source,source,Origin.SYSTEM,owner),at,at,'',None,event_kind='system')
        _,existing = await CognitiveRepository(pool).observe(event)
    jid = await JobQueue(pool).enqueue(cid,existing,'rebuild')
    async with pool.acquire() as conn:
        await conn.execute("UPDATE cognitive_jobs SET status='pending',attempts=0,lease_token=NULL,lease_until=NULL,available_at=NOW() WHERE id=$1",jid)
    return jid


async def finish_rebuild(pool,cid):
    await rebuild_allowed(pool,cid)
    async with pool.acquire() as conn:
        owners = await conn.fetchval('SELECT ARRAY_AGG(DISTINCT owner_id) FROM cognitive_events WHERE context_id=$1 AND suppressed_at IS NOT NULL AND owner_id IS NOT NULL',cid) or []
    for owner in owners:
        await erase_linked_legacy(pool,cid,owner)
    async with pool.acquire() as conn,conn.transaction():
        await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
        await conn.execute('UPDATE cognitive_contexts SET rebuilding=FALSE WHERE id=$1',cid)
        await conn.execute("UPDATE cognitive_jobs SET status='done',last_error_code=NULL WHERE context_id=$1 AND kind='rebuild' AND status='pending'",cid)


async def recover_rebuild(pool,cid):
    async with pool.acquire() as lock:
        await lock.execute("SELECT pg_advisory_lock(hashtext('cognition-rebuild')::int,$1::int)",cid)
        try:
            if await lock.fetchval('SELECT rebuilding FROM cognitive_contexts WHERE id=$1',cid):
                await finish_rebuild(pool,cid)
        finally:
            await lock.execute("SELECT pg_advisory_unlock(hashtext('cognition-rebuild')::int,$1::int)",cid)


async def rebuild_context(pool,cid,owner):
    """Invalidate source-less legacy views without losing permitted new memory."""
    async with pool.acquire() as lock:
        await lock.execute("SELECT pg_advisory_lock(hashtext('cognition-rebuild')::int,$1::int)",cid)
        try:
            await schedule_rebuild(pool,cid,owner)
            async with lock.transaction():
                await lock.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
                await lock.execute('UPDATE cognitive_contexts SET rebuilding=TRUE,suppression_epoch=suppression_epoch+1,worker_token=NULL,worker_lease_until=NULL WHERE id=$1',cid)
                await lock.execute("UPDATE cognitive_jobs SET status='pending',lease_token=NULL,lease_until=NULL,available_at=NOW() WHERE context_id=$1 AND status='running'",cid)
            await finish_rebuild(pool,cid)
        finally:
            await lock.execute("SELECT pg_advisory_unlock(hashtext('cognition-rebuild')::int,$1::int)",cid)


async def erase_linked_legacy(pool,cid,owner):
    async with pool.acquire() as conn,conn.transaction():
        context = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
        ids = await conn.fetchval('''SELECT ARRAY_AGG(m.legacy_id) FROM cognitive_legacy_map m JOIN cognitive_events e ON e.id=m.event_id
            WHERE m.source_table='memory_messages' AND e.context_id=$1 AND e.owner_id=$2 AND e.suppressed_at IS NOT NULL''',cid,owner) or []
        if ids:
            if await conn.fetchval("SELECT to_regclass('memory_chunks')"):
                await conn.execute('DELETE FROM memory_chunks WHERE chat_id=$1 AND message_ids && $2::bigint[]',context['chat_id'],ids)
            await conn.execute('DELETE FROM memory_timelines WHERE chat_id=$1 AND source_message_ids && $2::bigint[]',context['chat_id'],ids)
            await conn.execute('DELETE FROM memory_facts WHERE chat_id=$1 AND source_message_id=ANY($2::bigint[])',context['chat_id'],ids)
            await conn.execute('DELETE FROM memory_messages WHERE chat_id=$1 AND user_id=$2 AND id=ANY($3::bigint[])',context['chat_id'],owner,ids)
            await conn.execute("UPDATE cognitive_legacy_map SET status='suppressed' WHERE source_table='memory_messages' AND legacy_id=ANY($1::bigint[])",ids)
        # These legacy views have incomplete lineage and are never trusted by the
        # active core; drop them so fallback/archive tools cannot expose a copy.
        await conn.execute('DELETE FROM memory_user_profiles WHERE chat_id=$1 AND user_id=$2 AND mode=$3',context['chat_id'],owner,context['mode'])
        await conn.execute('DELETE FROM memory_wiki_pages WHERE chat_id=$1 AND mode=$2 AND NOT is_default',context['chat_id'],context['mode'])
        await conn.execute('DELETE FROM memory_entities WHERE chat_id=$1',context['chat_id'])
        history = 'chat_history_rp' if context['mode']=='rp' else 'chat_history'
        await conn.execute(f'DELETE FROM {history} WHERE chat_id=$1',context['chat_id'])
    invalidate_chat_caches(context['chat_id'])


async def forget_legacy_fact(pool,chat_id,owner,fact_id):
    """Legacy data with unknown lineage is invalidated, never silently retained.

    Exact-source derivatives are erased. Unscoped summaries/profiles/graph and
    caches are discarded for the affected chat; raw evidence of other owners is
    retained. Canon seeded as global default is not user-derived.
    """
    from database.models import MemoryFact
    from cognition.repositories import ensure_schema
    await ensure_schema(pool)
    async with pool.acquire() as conn,conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',chat_id)
        fact = await conn.fetchrow('SELECT * FROM memory_facts WHERE id=$1 AND chat_id=$2 AND (user_id=$3 OR user_id IS NULL) FOR UPDATE',fact_id,chat_id,owner)
        if not fact:
            return False
        mode = fact['mode']
        sid = fact['source_message_id']
        # Summaries may have been extracted from several original messages. All
        # raw owned messages participating in this lineage are removed together.
        messages = await conn.fetch('''SELECT id FROM memory_messages WHERE chat_id=$1 AND mode=$2 AND user_id=$3
            AND (id=$4 OR message_text ILIKE '%' || $5 || '%')''',chat_id,mode,owner,sid,fact['fact_text'])
        ids = [r['id'] for r in messages]
        metadata = object_value(fact['metadata']) or {}
        ids.extend(int(x) for x in metadata.get('source_message_ids',[]) if type(x) is int)
        # Unknown legacy lineage requires invalidation of ALL derivative readers
        # in this scope. Source-less raw text of other owners is never deleted.
        await conn.execute('DELETE FROM memory_chunks WHERE chat_id=$1 AND mode=$2 AND (user_id=$3 OR user_id IS NULL OR message_ids && $4::bigint[])',chat_id,mode,owner,ids)
        await conn.execute('DELETE FROM memory_timelines WHERE chat_id=$1 AND mode=$2 AND (user_id=$3 OR user_id IS NULL OR source_message_ids && $4::bigint[])',chat_id,mode,owner,ids)
        await conn.execute('DELETE FROM memory_user_profiles WHERE chat_id=$1 AND mode=$2',chat_id,mode)
        await conn.execute('DELETE FROM memory_entities WHERE chat_id=$1',chat_id)
        await conn.execute('DELETE FROM memory_wiki_pages WHERE chat_id=$1 AND mode=$2 AND NOT is_default',chat_id,mode)
        await conn.execute('DELETE FROM user_events WHERE chat_id=$1',chat_id)
        await conn.execute('DELETE FROM chat_emotional_states WHERE chat_id=$1',chat_id)
        await conn.execute('DELETE FROM legacy_emotional_effects WHERE chat_id=$1',chat_id)
        # Rolling histories do not contain ownership/source IDs. Clear the small
        # chat cache rather than claim an unknowable exact-source deletion.
        history = 'chat_history_rp' if mode=='rp' else 'chat_history'
        await conn.execute(f'DELETE FROM {history} WHERE chat_id=$1',chat_id)
        await conn.execute('DELETE FROM memory_facts WHERE chat_id=$1 AND mode=$2 AND (id=$3 OR source_message_id=ANY($4::bigint[]))',chat_id,mode,fact_id,ids)
        await conn.execute('DELETE FROM memory_messages WHERE chat_id=$1 AND user_id=$2 AND id=ANY($3::bigint[])',chat_id,owner,ids)
        mapped = await conn.fetch('''SELECT e.context_id,e.source_id FROM cognitive_legacy_map m JOIN cognitive_events e ON e.id=m.event_id
            WHERE m.source_table='memory_messages' AND m.legacy_id=ANY($1::bigint[]) AND e.owner_id=$2''',ids,owner)
        contexts = await conn.fetch('SELECT id FROM cognitive_contexts WHERE chat_id=$1 AND mode=$2',chat_id,mode)
        # Epoch changes fence in-flight generation before any success is reported.
        await conn.execute('UPDATE cognitive_contexts SET suppression_epoch=suppression_epoch+1 WHERE chat_id=$1 AND mode=$2',chat_id,mode)
        await conn.execute("UPDATE cognitive_outbox SET status='cancelled',payload=NULL WHERE context_id=ANY($1::bigint[]) AND status='prepared'",[c['id'] for c in contexts])
    for source in mapped:
        await forget_cognitive_sources(pool,source['context_id'],owner,[source['source_id']])
    for context in contexts:
        if context['id'] not in {s['context_id'] for s in mapped}:
            await rebuild_context(pool,context['id'],owner)
    invalidate_chat_caches(chat_id)
    return True


def invalidate_chat_caches(chat_id):
    from utils import chat_history
    for name in ('_context_cache','_recent_messages_cache','_dialog_history_cache',
                 '_context_cache_rp','_recent_messages_cache_rp','_dialog_history_cache_rp'):
        getattr(chat_history,name,{}).pop(chat_id,None)
