"""Local semantic windows for actually observed, currently permitted public sources.

This index contains no private traces or beliefs. Every read and both sides of
encoding use the public repository's audience and recursive provenance fence.
Only hashes, exact offsets, normalized vectors and resumable progress persist.
"""
import asyncio
from datetime import datetime, timezone

from asyncpg import QueryCanceledError

from cognition.semantic import VERSION, validated_vector
from cognition.types import ContextKey, MODEL_VERSION, utc


PUBLIC_VERSION = VERSION + ':observed-public-v1'
MAX_BATCH_CHUNKS = 32
BACKFILL_BUDGET_SECONDS = 12
# Keep this in sync with source_chunk_count's 640-character/100-overlap geometry.
_CHUNK_COUNT = "CASE WHEN length(p.payload->>'text')=0 THEN 0 ELSE (greatest(1,length(p.payload->>'text')-100)+539)/540 END"
_HASH = "md5(p.payload::text || p.observation_payload::text)"
_VALID_CHUNKS = """(SELECT count(*) FROM cognitive_public_semantic_vectors v
    WHERE v.context_id=q.context_id AND v.event_id=q.event_id AND v.embedding_model=q.embedding_model
      AND v.source_hash=q.source_hash AND v.projection_epoch=q.projection_epoch
      AND v.chunk_index<q.next_chunk AND v.chunk_start=v.chunk_index*540
      AND v.chunk_end=least(length(p.payload->>'text'),v.chunk_start+640))"""


class PublicSemanticIndex:
    def __init__(self, pool, encoder, clock=None):
        self.pool = pool
        self.encoder = encoder
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _repository(self):
        # Avoid a module cycle; the authoritative public permission code stays
        # in one place and is shared with lexical retrieval/final validation.
        from cognition.public_memory import PublicMemoryRepository
        return PublicMemoryRepository(self.pool)

    @staticmethod
    def _context(row):
        return ContextKey(*(row[k] for k in ('persona_id','chat_id','mode','scene_id','topic_id')))

    async def backfill(self, limit=4):
        """Bound optional background work, preserving atomic checkpoints."""
        try:
            async with asyncio.timeout(BACKFILL_BUDGET_SECONDS):
                return await self._backfill(limit)
        except (TimeoutError,QueryCanceledError):
            return 0

    async def _backfill(self, limit=4):
        """Commit at most 32 sequential windows, then fairly resume next call.

        Context scheduling and source progress survive restart. Encoding holds
        no database locks. A delayed result may commit only against a freshly
        authorized, identical source and its unchanged next-window checkpoint.
        """
        from cognition.public_memory import _VISIBLE
        from cognition.source_chunks import source_chunks
        limit = max(0,min(16,int(limit)))
        if not limit:
            return 0
        repository = self._repository()
        # Exclude tuple-locked audiences before LIMIT. Otherwise a first-time
        # scheduler FK check can block before recording its fair-rotation mark.
        # These candidate locks live only for this short selection transaction.
        async with self.pool.acquire(timeout=1) as conn,conn.transaction():
            contexts = await conn.fetch('''SELECT c.* FROM cognitive_contexts c
                LEFT JOIN cognitive_public_semantic_work w ON w.context_id=c.id
                WHERE c.topic_id>=0 AND c.authority='active' AND NOT c.rebuilding AND c.model_version=$1
                  AND EXISTS(SELECT 1 FROM group_observations o WHERE o.context_id=c.id)
                ORDER BY coalesce(w.updated_at,'-infinity'::timestamptz),c.id LIMIT $2
                FOR NO KEY UPDATE OF c SKIP LOCKED''',
                MODEL_VERSION,min(64,max(8,limit*4)),timeout=1)
        prepared = []
        chunk_budget = max(1,min(MAX_BATCH_CHUNKS,int(getattr(self.encoder,'recommended_batch_size',MAX_BATCH_CHUNKS))))
        # One source receives at most an equal share in a pass; a huge source
        # cannot consume every batch before short sources get a turn.
        per_source = max(1,chunk_budget//limit)
        prepare_until = asyncio.get_running_loop().time()+1.5
        for context_row in contexts:
            if (len(prepared)>=limit or chunk_budget<=0
                    or asyncio.get_running_loop().time()>=prepare_until):
                break
            context = self._context(context_row)
            cid = context_row['id']
            try:
                # Persist the scheduling attempt before waiting on a context.
                # One busy audience must not pin the worker ahead of all others.
                async with self.pool.acquire(timeout=1) as scheduler:
                    await scheduler.execute('''INSERT INTO cognitive_public_semantic_work(context_id) VALUES($1)
                        ON CONFLICT(context_id) DO UPDATE SET updated_at=clock_timestamp()''',cid,timeout=1)
                async with self.pool.acquire(timeout=1) as conn,conn.transaction():
                    await conn.execute("SET LOCAL statement_timeout = '1500ms'")
                    await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',context.chat_id)
                    scope = await repository._scope(conn,cid,context,utc(self.clock()),None,None)
                    if scope is None:
                        await conn.execute('DELETE FROM cognitive_public_semantic_progress WHERE context_id=$1',cid)
                        continue
                    ctx,args = scope
                    # This also removes expired, revoked-dependency and obsolete
                    # model/epoch rows that may have become stale without a write.
                    await conn.execute(_VISIBLE+f'''
                        DELETE FROM cognitive_public_semantic_progress q WHERE q.context_id=$1
                        AND (q.embedding_model<>$8 OR q.projection_epoch<>$9 OR NOT EXISTS(
                          SELECT 1 FROM permitted p WHERE p.id=q.event_id AND p.origin IN ('user','system')
                            AND q.source_hash={_HASH} AND q.total_chunks={_CHUNK_COUNT}
                            AND q.next_chunk={_VALID_CHUNKS}))''',*args,PUBLIC_VERSION,ctx['suppression_epoch'])
                    rows = await conn.fetch(_VISIBLE+f'''
                        SELECT p.*, {_HASH} AS source_hash, {_CHUNK_COUNT} AS total_chunks,
                               coalesce(q.next_chunk,0) AS next_chunk
                        FROM permitted p LEFT JOIN cognitive_public_semantic_progress q
                          ON q.context_id=p.context_id AND q.event_id=p.id AND q.embedding_model=$8
                        WHERE p.origin IN ('user','system') AND length(p.payload->>'text')>0
                          AND (q.event_id IS NULL OR q.next_chunk<q.total_chunks)
                        ORDER BY coalesce(q.updated_at,p.observed_at),p.id LIMIT $9''',
                        *args,PUBLIC_VERSION,limit-len(prepared))
                    for row in rows:
                        # Even a malformed legacy row gets a scheduling checkpoint,
                        # so it cannot starve later valid sources in this audience.
                        generation = await conn.fetchval('''INSERT INTO cognitive_public_semantic_progress
                            (context_id,event_id,embedding_model,source_hash,projection_epoch,total_chunks,next_chunk)
                            VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(context_id,event_id,embedding_model)
                            DO UPDATE SET updated_at=clock_timestamp() RETURNING generation''',cid,row['id'],PUBLIC_VERSION,
                            row['source_hash'],ctx['suppression_epoch'],row['total_chunks'],row['next_chunk'])
                        source = repository._source(row,context)
                        if source is None:
                            continue
                        event,_ = source
                        count = min(per_source,chunk_budget)
                        chunks = source_chunks(event.text,start_index=row['next_chunk'],limit=count)
                        if not chunks:
                            continue
                        prepared.append((dict(row,generation=generation),context,ctx['suppression_epoch'],chunks))
                        chunk_budget -= len(chunks)
                        if chunk_budget<=0:
                            break
            except (TimeoutError,QueryCanceledError):
                continue
        if not prepared:
            return 0
        texts = [text for _,_,_,chunks in prepared for _,_,text in chunks]
        vectors = await self.encoder.encode(texts,timeout=5)
        if vectors is None:
            return 0
        if len(vectors)!=len(texts):
            raise ValueError('public_semantic_batch_size')
        vectors = [validated_vector(v) for v in vectors]
        offset,count = 0,0
        for row,context,epoch,chunks in prepared:
            chunk_vectors = vectors[offset:offset+len(chunks)]
            offset += len(chunks)
            cid = row['context_id']
            async with self.pool.acquire(timeout=1) as conn,conn.transaction():
                await conn.execute("SET LOCAL statement_timeout = '1500ms'")
                await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',context.chat_id)
                scope = await repository._scope(conn,cid,context,utc(self.clock()),None,None)
                if scope is None or scope[0]['suppression_epoch']!=epoch:
                    continue
                _,args = scope
                current = await conn.fetchrow(_VISIBLE+f'''
                    SELECT p.* FROM permitted p JOIN cognitive_public_semantic_progress q
                      ON q.context_id=p.context_id AND q.event_id=p.id AND q.embedding_model=$8
                    WHERE p.id=$9 AND p.origin IN ('user','system') AND {_HASH}=$10
                      AND q.source_hash=$10 AND q.projection_epoch=$11 AND q.next_chunk=$12
                      AND q.total_chunks=$13 AND q.generation=$14 FOR UPDATE OF q''',*args,PUBLIC_VERSION,row['id'],
                    row['source_hash'],epoch,row['next_chunk'],row['total_chunks'],row['generation'])
                if current is None or repository._source(current,context) is None:
                    continue
                for index,((start,end,_),vector) in enumerate(zip(chunks,chunk_vectors),row['next_chunk']):
                    await conn.execute('''INSERT INTO cognitive_public_semantic_vectors
                        (context_id,event_id,embedding_model,source_hash,projection_epoch,chunk_index,chunk_start,chunk_end,vector)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::double precision[]) ON CONFLICT DO NOTHING''',
                        cid,row['id'],PUBLIC_VERSION,row['source_hash'],epoch,index,start,end,vector)
                await conn.execute('''UPDATE cognitive_public_semantic_progress SET next_chunk=$4,updated_at=clock_timestamp()
                    WHERE context_id=$1 AND event_id=$2 AND embedding_model=$3''',
                    cid,row['id'],PUBLIC_VERSION,row['next_chunk']+len(chunks))
                count += 1
        return count

    async def encode_query(self, query):
        """Use a shorter optional budget so lexical results remain available."""
        try:
            async with asyncio.timeout(.25):
                vectors = await self.encoder.encode([str(query)[:3200]],timeout=.2)
            if vectors is None:
                status = getattr(self.encoder,'last_status','unavailable')
                return None,status if status in ('unavailable','timeout') else 'unavailable'
            return validated_vector(vectors[0]),'complete'
        except TimeoutError:
            return None,'timeout'
        except Exception:
            return None,'unavailable'

    async def search_locked(self, conn, cid, context, scope, vector, *, limit=48, exclude_event_ids=()):
        """The caller already holds the chat/context lock and current _scope."""
        from cognition.public_memory import _VISIBLE
        ctx,args = scope
        return await conn.fetch(_VISIBLE+f""",
            public_sources AS MATERIALIZED (
                SELECT p.*,{_HASH} AS public_source_hash,length(p.payload->>'text') AS source_length
                FROM permitted p WHERE p.origin IN ('user','system') AND NOT(p.id=ANY($11::bigint[]))
            ), scored AS MATERIALIZED (
                SELECT p.id,v.chunk_start,v.chunk_end,
                  arti_semantic_dot(v.vector,$8::double precision[]) AS semantic_score
                FROM public_sources p JOIN cognitive_public_semantic_progress q
                  ON q.context_id=p.context_id AND q.event_id=p.id AND q.embedding_model=$9
                  AND q.projection_epoch=$10 AND q.source_hash=p.public_source_hash
                JOIN cognitive_public_semantic_vectors v
                  ON v.context_id=p.context_id AND v.event_id=p.id AND v.embedding_model=q.embedding_model
                WHERE v.projection_epoch=$10 AND v.source_hash=p.public_source_hash
                  AND v.chunk_index<q.next_chunk
                  AND v.chunk_start=v.chunk_index*540
                  AND v.chunk_end=least(p.source_length,v.chunk_start+640)
            ), ranked AS (
                SELECT *,row_number() OVER(PARTITION BY id ORDER BY semantic_score DESC,chunk_start) AS window_rank
                FROM scored WHERE semantic_score>=.42
            ), selected AS (
                SELECT * FROM ranked WHERE window_rank<=12
                ORDER BY semantic_score DESC,id DESC,chunk_start LIMIT $12
            )
            SELECT p.*,s.chunk_start,s.chunk_end,s.semantic_score FROM selected s JOIN public_sources p ON p.id=s.id
            ORDER BY s.semantic_score DESC,p.id DESC,s.chunk_start""",
            *args,vector,PUBLIC_VERSION,ctx['suppression_epoch'],list(exclude_event_ids),min(128,max(0,int(limit))))

    async def diagnostics_locked(self, conn, context, scope):
        """Counts reveal only sources permitted in this exact public audience."""
        from cognition.public_memory import _VISIBLE
        ctx,args = scope
        valid_chunks = _VALID_CHUNKS.replace("length(p.payload->>'text')",'p.source_length')
        row = await conn.fetchrow(_VISIBLE+f""",
            public_sources AS MATERIALIZED (
                SELECT p.*,{_HASH} AS public_source_hash,length(p.payload->>'text') AS source_length,
                  {_CHUNK_COUNT} AS total_chunks
                FROM permitted p WHERE p.origin IN ('user','system') AND length(p.payload->>'text')>0
            ), coverage AS MATERIALIZED (
                SELECT p.total_chunks,{valid_chunks} AS indexed_chunks
                FROM public_sources p LEFT JOIN cognitive_public_semantic_progress q
                  ON q.context_id=p.context_id AND q.event_id=p.id AND q.embedding_model=$8
                  AND q.projection_epoch=$9 AND q.source_hash=p.public_source_hash
            )
            SELECT count(*) AS total_sources,
              count(*) FILTER(WHERE indexed_chunks>=total_chunks) AS indexed_sources,
              coalesce(sum(indexed_chunks),0) AS indexed_chunks,
              coalesce(sum(total_chunks),0) AS total_chunks FROM coverage""",
            *args,PUBLIC_VERSION,ctx['suppression_epoch'])
        result = {k:int(row[k]) for k in ('total_sources','indexed_sources','indexed_chunks','total_chunks')}
        result['remaining_chunks'] = max(0,result['total_chunks']-result['indexed_chunks'])
        result['status'] = result['semantic_status'] = 'incomplete' if result['remaining_chunks'] else 'complete'
        result['scope'] = 'public'
        return result
