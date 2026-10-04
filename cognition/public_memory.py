"""Source-backed recollection for one observed public audience.

This is deliberately separate from personal memory. It reads neither personal
traces nor beliefs, and never changes their owner filter. A public record keeps
its source owner; permission comes from an exact, still-readable observation in
the requested chat/topic/mode/scene, not from assigning the record to the asker.
"""
import asyncio
from dataclasses import asdict
from datetime import timedelta

from asyncpg import QueryCanceledError

from cognition.group_policy import PolicyRepository
from cognition.memory_repository import MemoryRepository, key
from cognition.repositories import SuppressedEvidence
from cognition.retrieval import RetrievalResult
from cognition.semantic import query_terms
from cognition.serialization import dump, load_event, object_value
from cognition.types import AudienceScope, ContextKey, MODEL_VERSION, Origin, utc


READ_BUDGET_SECONDS = 1.0
_STATEMENT_BUDGET = "SET LOCAL statement_timeout = '750ms'"


# Every dependency must independently be readable in the SAME public audience.
# Bot utterances are never retrieval results. If an observed human utterance
# depends on one, its full source chain must still be readable. UNION also
# terminates cycles instead of trusting a bounded-depth provenance shortcut.
_VISIBLE = """
WITH RECURSIVE visible AS (
    SELECT e.*,o.message_id,o.payload AS observation_payload
    FROM cognitive_events e JOIN group_observations o
      ON o.context_id=e.context_id AND o.event_id=e.id
    WHERE e.context_id=$1 AND e.suppressed_at IS NULL AND e.payload IS NOT NULL
      AND o.suppressed_at IS NULL AND o.payload IS NOT NULL
      AND e.id>$2 AND e.observed_at>=$3 AND o.observed_at>=$3
      AND e.observed_at<=$6 AND o.observed_at<=$6
      AND e.payload->'context'=$4::jsonb AND e.payload->'audience'=$5::jsonb
      AND e.payload->'evidence'->>'source_id'=e.source_id
      AND e.payload->'evidence'->>'origin'=e.origin
      AND e.payload->'evidence'->'owner_id'=coalesce(to_jsonb(e.owner_id),'null'::jsonb)
      AND o.owner_id IS NOT DISTINCT FROM e.owner_id
      AND o.payload->'owner_id'=coalesce(to_jsonb(e.owner_id),'null'::jsonb)
      AND o.payload->>'text'=left(e.payload->>'text',2500)
      AND ((e.origin='user' AND o.payload->>'sender_kind'='user'
            AND o.payload->>'is_bot'='false'
            AND e.payload->'actor_id'=to_jsonb(e.owner_id))
        OR (e.origin='delivered_action' AND o.payload->>'sender_kind'='bot'
            AND o.payload->>'is_bot'='true')
        OR (e.origin='system' AND e.owner_id IS NULL
            AND o.payload->>'sender_kind'='chat' AND o.payload->>'is_bot'='false'
            AND coalesce(o.payload->>'sender_ref','')<>''))
      AND NOT EXISTS (SELECT 1 FROM group_participant_settings p
          WHERE p.chat_id=$7 AND p.user_id=e.owner_id AND p.opt_out)
), dependencies(root_id,event_id) AS (
    SELECT id,id FROM visible
    UNION
    SELECT d.root_id,p.source_event_id FROM dependencies d
      JOIN cognitive_event_dependencies p ON p.context_id=$1 AND p.event_id=d.event_id
), permitted AS (
    SELECT v.* FROM visible v WHERE NOT EXISTS (
        SELECT 1 FROM dependencies d LEFT JOIN visible s ON s.id=d.event_id
        WHERE d.root_id=v.id AND s.id IS NULL)
)
"""


class PublicMemoryRepository:
    def __init__(self, pool, semantic=None):
        self.pool = pool
        self.semantic = semantic
        self.policies = PolicyRepository(pool)
        self.records = MemoryRepository(pool)

    async def _scope(self, conn, cid, context, at, requester, expected_epoch, *, lock=True):
        """Run under the context lock, including final prompt/delivery checks."""
        if not isinstance(context, ContextKey) or context.topic_id < 0:
            return None
        row = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1'+(' FOR UPDATE' if lock else ''),cid)
        if (not row or tuple(row[k] for k in ('persona_id','chat_id','mode','scene_id','topic_id')) != context.identity()
                or row['authority'] != 'active' or row['rebuilding'] or row['model_version'] != MODEL_VERSION):
            return None
        if expected_epoch is not None and row['suppression_epoch'] != expected_epoch:
            raise SuppressedEvidence()
        if context.mode == 'rp':
            scene = await conn.fetchval('SELECT scene_id FROM cognitive_scenes WHERE chat_id=$1 AND topic_id=$2',context.chat_id,context.topic_id)
            if scene != context.scene_id:
                return None
        policy,_ = await self.policies.get(context.chat_id,context.topic_id,connection=conn)
        # Visibility and quiet-hours govern initiative, not access to an actual
        # observed utterance. A partial-visibility chat can recall only what it
        # has really observed, never messages supplied by a private projection.
        if policy.disabled:
            return None
        if requester is not None and await conn.fetchval(
                'SELECT opt_out FROM group_participant_settings WHERE chat_id=$1 AND user_id=$2',context.chat_id,requester):
            return None
        if await conn.fetchval('SELECT enabled FROM response_status WHERE chat_id=$1',context.chat_id) is False:
            return None
        audience = AudienceScope('topic' if context.topic_id > 0 else 'group',context.chat_id,context.topic_id)
        args = (cid,row['history_after_event_id'],at-timedelta(days=policy.retention_days),
                dump(context),dump(audience),at,context.chat_id)
        return row,args

    @staticmethod
    def _source(row, context):
        """Validate wire data instead of promoting legacy/ambiguous ownership."""
        try:
            event = load_event(row['payload'])
            observation = object_value(row['observation_payload'])
            if (event.context != context or not event.audience.permits(context.chat_id,context.topic_id)
                    or event.evidence.owner_id != row['owner_id'] or event.evidence.source_id != row['source_id']
                    or event.evidence.origin.value != row['origin']
                    or event.event_id != row['event_key']
                    or event.observed_at != row['observed_at'] or event.occurred_at != row['occurred_at']
                    or observation.get('message_id') != row['message_id']):
                return None
            return event,observation
        except (KeyError,TypeError,ValueError,AttributeError):
            return None

    @staticmethod
    def _modality(row, event):
        if event.evidence.origin == Origin.DELIVERED_ACTION:
            return 'delivered_action'
        try:
            perception = object_value(row['perception']) if row['perception'] else {}
            situation = perception.get('situation') or {}
            if perception.get('event_id') == event.event_id and situation.get('modality') in ('quoted','reported','hypothetical','interaction'):
                return situation['modality']
        except (AttributeError,TypeError,ValueError):
            pass
        return 'source_utterance'

    @classmethod
    def _record(cls, row, event, observation, words, epoch, *, window=None):
        # Keep exact wording, including quotation/report framing. A substring
        # is explicitly an excerpt, never a new statement attributed to asker.
        if window is None:
            positions = [event.text.casefold().find(word) for word in words]
            position = min((p for p in positions if p >= 0),default=0)
            start = max(0,position-256)
            end = min(len(event.text),start+640)
        else:
            start,end = window[:2]
        modality = cls._modality(row,event)
        author = 'arti' if event.evidence.origin == Origin.DELIVERED_ACTION else event.actor_id
        return dict(source_id=event.evidence.source_id,event_id=row['id'],owner_id=row['owner_id'],
                    author_id=author,sender_kind=observation['sender_kind'],sender_ref=observation.get('sender_ref'),
                    audience=asdict(event.audience),scope=asdict(event.context),visibility='observed_public_only',
                    message_id=row['message_id'],observed_at=event.observed_at.isoformat(),
                    occurred_at=event.occurred_at.isoformat(),record_start=start,record_end=end,
                    source_record_verified=True,projection_epoch=epoch,
                    source_prefix=event.text[:256] if start else '',
                    evidence_status='source_record',status='source_utterance',
                    details=[dict(text=event.text[start:end],kind='wording',confidence=.5,verbatim_verified=True)],
                    time_precision='source_record',time_basis='source_event',modality=modality,interpretation='',version=1,
                    familiarity=.5,uncertainty=True)

    async def retrieve(self, cid, context, query, at, cycle_key, *, requester=None,
                       expected_epoch=None, limit=8, exclude_event_ids=()):
        """Hybrid retrieval from the permitted ledger, never private projections.

        Optional query encoding gets its own short budget before the locked
        lexical read. Semantic unavailability/timeouts preserve lexical results.
        Counts report current audience coverage, not an assertion of no evidence.
        """
        semantic_status = 'unavailable'
        vector = None
        if self.semantic is not None and query_terms(query) and limit>0:
            vector,semantic_status = await self.semantic.encode_query(query)
        try:
            async with asyncio.timeout(READ_BUDGET_SECONDS):
                return await self._retrieve(cid,context,query,at,cycle_key,requester=requester,
                    expected_epoch=expected_epoch,limit=limit,exclude_event_ids=exclude_event_ids,
                    vector=vector,semantic_status=semantic_status)
        except (TimeoutError,QueryCanceledError):
            return RetrievalResult(diagnostics=dict(status='timeout',semantic_status=semantic_status,
                lexical_status='timeout',scope='public'))

    async def _retrieve(self, cid, context, query, at, cycle_key, *, requester,
                        expected_epoch, limit, exclude_event_ids, vector=None, semantic_status='unavailable'):
        from cognition.source_chunks import lexical_windows, deduplicate_windows
        at = utc(at)
        words = sorted(query_terms(query))[:64]
        if not words or limit <= 0:
            return RetrievalResult(diagnostics=dict(status='complete',semantic_status='complete',
                lexical_status='complete',scope='public'))
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute(_STATEMENT_BUDGET)
            if isinstance(context,ContextKey):
                await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',context.chat_id)
            scope = await self._scope(conn,cid,context,at,requester,expected_epoch)
            if scope is None:
                return RetrievalResult(diagnostics=dict(status='complete',semantic_status='complete',
                    lexical_status='complete',scope='public',total_sources=0,indexed_sources=0,
                    total_chunks=0,indexed_chunks=0,remaining_chunks=0))
            ctx,args = scope
            tsquery = ' OR '.join(words)
            rows = await conn.fetch(_VISIBLE+"""
                SELECT p.*,ts_rank(to_tsvector('russian',arti_memory_search_text(p.payload->>'text')),websearch_to_tsquery('russian',arti_memory_search_text($8))) AS score,
                    to_tsvector('russian',arti_memory_search_text(p.payload->>'text')) @@ websearch_to_tsquery('russian',arti_memory_search_text($11)) AS full_query_match
                FROM permitted p
                WHERE p.origin IN ('user','system')
                  AND to_tsvector('russian',arti_memory_search_text(p.payload->>'text')) @@ websearch_to_tsquery('russian',arti_memory_search_text($8))
                  AND NOT (p.id=ANY($9::bigint[]))
                ORDER BY full_query_match DESC,score DESC,p.observed_at DESC,p.id DESC LIMIT $10
                """,*args,tsquery,list(exclude_event_ids),min(128,max(16,int(limit)*4)),' '.join(words))
            # Savepoints bound optional semantic SQL without poisoning the
            # lexical transaction if vector ranking or coverage times out.
            semantic_rows = []
            diagnostics = dict(status=semantic_status,semantic_status=semantic_status,
                lexical_status='complete',scope='public')
            from cognition.public_semantic import PublicSemanticIndex
            diagnostic_index = self.semantic or PublicSemanticIndex(self.pool,None)
            if diagnostic_index is not None:
                try:
                    async with conn.transaction():
                        await conn.execute("SET LOCAL statement_timeout = '120ms'")
                        async with asyncio.timeout(.15):
                            diagnostics.update(await diagnostic_index.diagnostics_locked(conn,context,scope))
                            if vector is not None:
                                semantic_rows = await self.semantic.search_locked(conn,cid,context,scope,vector,
                                    limit=128,exclude_event_ids=exclude_event_ids)
                        await conn.execute(_STATEMENT_BUDGET)
                except (TimeoutError,QueryCanceledError):
                    semantic_status = 'timeout'
                except Exception:
                    semantic_status = 'unavailable'
                if semantic_status!='complete':
                    diagnostics.update(status=semantic_status,semantic_status=semantic_status)
            # Reciprocal ranks preserve lexical precision while allowing a
            # semantic-only public paraphrase into the same bounded shortlist.
            candidates = []
            by_source = {}
            for rank,row in enumerate(rows,1):
                source = self._source(row,context)
                if source is None:
                    continue
                windows = await lexical_windows(conn,source[0].text,tsquery,width=640,limit=3)
                by_source[row['id']] = dict(row=row,source=source,windows=[],score=1/(60+rank),
                    full_query_match=bool(row['full_query_match']))
                for window in windows:
                    by_source[row['id']]['windows'].append((1/(60+rank),window))
            source_ranks = {}
            for row in semantic_rows:
                source = self._source(row,context)
                if source is None:
                    continue
                start,end = row['chunk_start'],row['chunk_end']
                if not (type(start) is int and type(end) is int and 0<=start<end<=len(source[0].text)):
                    continue
                rank = source_ranks.setdefault(row['id'],len(source_ranks)+1)
                item = by_source.setdefault(row['id'],dict(row=row,source=source,windows=[],score=0.))
                if not item.get('semantic_ranked'):
                    item['score'] += 1/(60+rank)
                    item['semantic_ranked'] = True
                item['windows'].append((float(row['semantic_score'])/(60+rank),
                    (start,end,source[0].text[start:end])))
            for item in by_source.values():
                # Keep distinct passages; the fixed semantic geometry has a
                # small boundary overlap, which is not a duplicate passage.
                selected = deduplicate_windows(item['source'][0].text,
                    [window for _,window in item['windows']],limit=3)
                for index,window in enumerate(selected):
                    candidates.append((item['score'],index,item,window))
            # Full-query lexical specificity precedes broad topical similarity.
            # This is PostgreSQL lexeme matching (including inflection), never
            # guessed names or aliases: 'Alice budget' must outrank 'Bob budget'.
            candidates.sort(key=lambda x:(-int(x[2].get('full_query_match',False)),
                -x[0],x[1],-x[2]['row']['id'],x[3][0]))
            result = []
            for score,_,item,window in candidates:
                row,source = item['row'],item['source']
                record = self._record(row,*source,words,ctx['suppression_epoch'],window=window)
                identity = key('public_record',row['id'],ctx['suppression_epoch'],record['record_start'],record['record_end'])
                old = await conn.fetchrow('SELECT suppressed_at FROM cognitive_artifacts WHERE context_id=$1 AND artifact_key=$2',cid,identity)
                if old and old['suppressed_at'] is not None:
                    continue
                aid = await self.records._put(conn,cid,'public_record',identity,record,row['owner_id'],[row['id']])
                result.append(dict(artifact_id=aid,score=score,**record))
                if len(result)>=min(16,int(limit)):
                    break
            ids = [r['artifact_id'] for r in result]
            for stage in ('candidate','recalled'):
                await self._log(conn,cid,requester,cycle_key,stage,ids,at)
            return RetrievalResult(result,diagnostics=diagnostics)

    async def _validate_locked(self, conn, cid, context, artifact_ids, at, requester, expected_epoch):
        scope = await self._scope(conn,cid,context,at,requester,expected_epoch)
        if scope is None or not artifact_ids:
            return [],[]
        ctx,args = scope
        rows = await conn.fetch(_VISIBLE+"""
            SELECT p.*,a.id AS artifact_id,a.payload AS record,
                ARRAY(SELECT d.event_id FROM dependencies d WHERE d.root_id=p.id ORDER BY d.event_id) AS source_ids
            FROM cognitive_artifacts a JOIN cognitive_provenance v
              ON v.context_id=a.context_id AND v.artifact_id=a.id
            JOIN permitted p ON p.id=v.source_event_id
            WHERE a.context_id=$1 AND a.id=ANY($8::bigint[]) AND a.kind='public_record'
              AND p.origin IN ('user','system')
              AND a.model_version=$9 AND a.projection_epoch=$10
              AND a.suppressed_at IS NULL AND a.payload IS NOT NULL
              AND a.owner_id IS NOT DISTINCT FROM p.owner_id
              AND (SELECT count(*) FROM cognitive_provenance z WHERE z.context_id=$1 AND z.artifact_id=a.id)=1
            """,*args,sorted(set(artifact_ids)),MODEL_VERSION,ctx['suppression_epoch'])
        allowed,sources = [],set()
        for row in rows:
            source = self._source(row,context)
            if source is None:
                continue
            event,observation = source
            record = object_value(row['record'])
            try:
                start,end = record['record_start'],record['record_end']
                if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(event.text)
                        or record['event_id'] != row['id'] or record['source_id'] != event.evidence.source_id
                        or record['scope'] != asdict(context) or record['audience'] != asdict(event.audience)
                        or record['owner_id'] != row['owner_id'] or record['projection_epoch'] != ctx['suppression_epoch']
                        or record['author_id'] != ('arti' if event.evidence.origin == Origin.DELIVERED_ACTION else event.actor_id)
                        or record['sender_kind'] != observation['sender_kind'] or record['sender_ref'] != observation.get('sender_ref')
                        or record['message_id'] != row['message_id'] or record['visibility'] != 'observed_public_only'
                        or record['modality'] != self._modality(row,event)
                        or record['time_precision'] != 'source_record' or record['time_basis'] != 'source_event'
                        or record['evidence_status'] != 'source_record' or record['status'] != 'source_utterance'
                        or record['source_prefix'] != (event.text[:256] if start else '')
                        or record['observed_at'] != event.observed_at.isoformat()
                        or record['occurred_at'] != event.occurred_at.isoformat()
                        or record['details'] != [dict(text=event.text[start:end],kind='wording',confidence=.5,verbatim_verified=True)]):
                    continue
            except (KeyError,TypeError,ValueError):
                continue
            allowed.append(row['artifact_id'])
            sources.update(row['source_ids'])
        return sorted(set(allowed)),sorted(sources)

    async def validate(self, cid, context, artifact_ids, at, *, requester=None,
                       expected_epoch=None, connection=None):
        """Return (allowed artifact IDs, supporting event IDs), never text.

        A caller supplying a connection must own its transaction. This permits a
        final recheck under the same lock as prompt inclusion or delivery. An
        epoch mismatch raises; any other access loss removes the affected IDs.
        Budget exhaustion fails closed. With an external connection this error
        must propagate out of the caller's transaction so it rolls back too.
        """
        try:
            async with asyncio.timeout(READ_BUDGET_SECONDS):
                return await self._validate(cid,context,artifact_ids,at,requester=requester,
                    expected_epoch=expected_epoch,connection=connection)
        except (TimeoutError,QueryCanceledError) as exc:
            raise SuppressedEvidence() from exc

    async def _validate(self, cid, context, artifact_ids, at, *, requester,
                        expected_epoch, connection):
        at = utc(at)
        if connection is not None:
            return await self._validate_locked(connection,cid,context,artifact_ids,at,requester,expected_epoch)
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute(_STATEMENT_BUDGET)
            if isinstance(context,ContextKey):
                await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',context.chat_id)
            return await self._validate_locked(conn,cid,context,artifact_ids,at,requester,expected_epoch)

    @staticmethod
    async def _log(conn, cid, requester, cycle_key, stage, ids, at):
        return await conn.fetchval('''INSERT INTO cognitive_retrievals(context_id,cycle_key,owner_id,stage,artifact_ids,created_at)
            VALUES($1,$2,$3,$4,$5::bigint[],$6) ON CONFLICT(context_id,cycle_key,stage) DO NOTHING RETURNING id''',
            cid,cycle_key,requester,stage,ids,at)

    async def record_retrieval(self, cid, context, requester, cycle_key, stage, ids, at, *, expected_epoch=None):
        """Public-only inclusion logging; private artifacts cannot enter here."""
        try:
            async with asyncio.timeout(READ_BUDGET_SECONDS):
                return await self._record_retrieval(cid,context,requester,cycle_key,stage,ids,at,expected_epoch=expected_epoch)
        except (TimeoutError,QueryCanceledError) as exc:
            raise SuppressedEvidence() from exc

    async def _record_retrieval(self, cid, context, requester, cycle_key, stage, ids, at, *, expected_epoch):
        if stage not in ('candidate','recalled','archive_checked','included','expressed'):
            raise ValueError('Invalid retrieval stage')
        at = utc(at)
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute(_STATEMENT_BUDGET)
            if isinstance(context,ContextKey):
                await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',context.chat_id)
            allowed,sources = await self._validate_locked(conn,cid,context,ids,at,requester,expected_epoch)
            # Dropping rows after the prompt was assembled would still let a
            # provider see revoked text. Inclusion must abort, not just log less.
            if set(allowed) != set(ids):
                raise SuppressedEvidence()
            await self._log(conn,cid,requester,cycle_key,stage,allowed,at)
            return allowed,sources
