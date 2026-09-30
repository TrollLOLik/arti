"""Resumable fixed-snapshot import. Unknown history is quarantined explicitly."""
import time
from datetime import timezone,timedelta
from cognition.affect import initial_state,appraise
from cognition.memory_repository import MemoryRepository,key
from cognition.repositories import CognitiveRepository,ensure_schema
from cognition.serialization import dump
from cognition.types import ContextKey,CognitiveEvent,EvidenceRef,Origin,Perception,PERCEPTION_VERSION,MODEL_VERSION


class HistoricalMigration:
    def __init__(self,pool):
        self.pool = pool
        self.repository = CognitiveRepository(pool)
        self.memory = MemoryRepository(pool)

    async def run(self,batch_size=200,max_rows=0,stream='history-v1'):
        async with self.pool.acquire() as lease:
            locked = await lease.fetchval("SELECT pg_try_advisory_lock(hashtext($1)::bigint)",'arti:migration:'+stream)
            if not locked:
                raise RuntimeError('This migration stream is already running')
            try:
                return await self._run(batch_size,max_rows,stream)
            finally:
                await lease.execute("SELECT pg_advisory_unlock(hashtext($1)::bigint)",'arti:migration:'+stream)

    async def _run(self,batch_size,max_rows,stream):
        if not 1<=batch_size<=2000:
            raise ValueError('Invalid migration batch size')
        await ensure_schema(self.pool)
        started = time.perf_counter()
        async with self.pool.acquire() as conn:
            bound = await conn.fetchval('SELECT COALESCE(MAX(id),0) FROM memory_messages')
            await conn.execute('INSERT INTO cognitive_checkpoints(stream,snapshot_id) VALUES($1,$2) ON CONFLICT DO NOTHING',stream,bound)
            checkpoint = await conn.fetchrow('SELECT * FROM cognitive_checkpoints WHERE stream=$1',stream)
        bound,cursor = checkpoint['snapshot_id'],checkpoint['cursor_id']
        imported=quarantined=seen=0
        while cursor<bound:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch('SELECT * FROM memory_messages WHERE id>$1 AND id<=$2 ORDER BY id LIMIT $3',cursor,bound,batch_size)
            if not rows:
                break
            for row in rows:
                if max_rows and seen>=max_rows:
                    break
                async with self.pool.acquire() as conn:
                    mapped = await conn.fetchval("SELECT status FROM cognitive_legacy_map WHERE source_table='memory_messages' AND legacy_id=$1",row['id'])
                if not mapped:
                    mode = row['mode']
                    # Old RP has no reconstructible scene boundary. Keep it in a
                    # quarantine scene, never the newly active scene.
                    context = ContextKey('arti',row['chat_id'],mode,'legacy-unresolved' if mode=='rp' else '') if mode in ('default','rp') else None
                    reason = ('unknown_mode' if context is None else 'unknown_author' if row['role']=='user' and row['user_id'] is None else
                              'unconfirmed_delivery' if row['role']=='assistant' else 'legacy_derivative' if row['role']!='user' else '')
                    if not reason:
                        at = row['created_at'].replace(tzinfo=timezone.utc)
                        observed = checkpoint['started_at'] + timedelta(microseconds=row['id'])
                        source = f"legacy:memory_messages:{row['id']}"
                        ev = CognitiveEvent(source,context,EvidenceRef(source,source,Origin.USER,row['user_id']),min(at,observed),observed,row['message_text'],row['user_id'],event_kind='historical')
                        cid,eid = await self.repository.observe(ev)
                        # Import source-backed details, but NEVER invent a past
                        # emotional snapshot/appraisal from a compressed story.
                        p = Perception(ev.event_id,PERCEPTION_VERSION,())
                        # Historical interpretation is deliberately empty. Record
                        # its no-op effect without rewinding a currently live clock.
                        async with self.pool.acquire() as conn,conn.transaction():
                            rev = await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
                            await conn.execute('UPDATE cognitive_events SET perception=$2::jsonb WHERE id=$1',eid,dump(p))
                            await conn.execute('''INSERT INTO cognitive_effects(context_id,event_id,independent_group,model_version,applied_revision)
                                VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING''',cid,eid,source,MODEL_VERSION,rev)
                        aid = await self.memory.encode(cid,eid,p)
                        async with self.pool.acquire() as conn:
                            await conn.execute("INSERT INTO cognitive_legacy_map VALUES('memory_messages',$1,$2,$3,$4,'imported','timezone_assumed_utc') ON CONFLICT DO NOTHING",row['id'],cid,eid,aid)
                        imported += 1
                    else:
                        async with self.pool.acquire() as conn:
                            await conn.execute("INSERT INTO cognitive_legacy_map(source_table,legacy_id,status,reason) VALUES('memory_messages',$1,'quarantined',$2) ON CONFLICT DO NOTHING",row['id'],reason)
                        quarantined += 1
                cursor = row['id']
                seen += 1
                async with self.pool.acquire() as conn:
                    await conn.execute('UPDATE cognitive_checkpoints SET cursor_id=GREATEST(cursor_id,$2),updated_at=NOW() WHERE stream=$1',stream,cursor)
            if max_rows and seen>=max_rows:
                break
        async with self.pool.acquire() as conn:
            # Derivatives lacking complete raw lineage never become authoritative
            # facts or invented historical affect. Coverage remains observable.
            for table in ('memory_facts','memory_timelines','memory_user_profiles','memory_wiki_pages','memory_entities','memory_relations'):
                await conn.execute(f"INSERT INTO cognitive_legacy_map(source_table,legacy_id,status,reason) SELECT $1,id,'quarantined','legacy_unverified' FROM {table} ON CONFLICT DO NOTHING",table)
            if await conn.fetchval("SELECT to_regclass('memory_chunks')"):
                await conn.execute("INSERT INTO cognitive_legacy_map(source_table,legacy_id,status,reason) SELECT 'memory_chunks',id,'quarantined','legacy_unverified' FROM memory_chunks ON CONFLICT DO NOTHING")
            counts = await conn.fetch('SELECT status,COUNT(*) AS count FROM cognitive_legacy_map GROUP BY status')
            report = dict(snapshot_id=bound,cursor_id=cursor,rows_this_run=seen,imported_this_run=imported,
                          quarantined_this_run=quarantined,coverage={r['status']:r['count'] for r in counts},
                          complete=cursor>=bound,seconds=time.perf_counter()-started,model_version=MODEL_VERSION,
                          historical_emotions_inferred=0,timezone_policy='legacy naive timestamps assumed UTC; original retained')
            await conn.execute('UPDATE cognitive_checkpoints SET report=$2::jsonb WHERE stream=$1',stream,dump(report))
        return report


async def link_legacy_message(pool,mid,chat_id,owner,mode,source_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''SELECT e.id,e.context_id FROM cognitive_events e JOIN cognitive_contexts c ON c.id=e.context_id
            WHERE c.chat_id=$1 AND c.mode=$2 AND e.owner_id IS NOT DISTINCT FROM $3 AND e.suppressed_at IS NULL
            AND e.source_id=$4 ORDER BY e.id DESC LIMIT 1''',chat_id,mode,owner,source_id)
        if row:
            await conn.execute("INSERT INTO cognitive_legacy_map VALUES('memory_messages',$1,$2,$3,NULL,'imported','linked_live_source') ON CONFLICT DO NOTHING",mid,row['context_id'],row['id'])
