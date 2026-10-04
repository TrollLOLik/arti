"""Transactional typed projections over the raw-source provenance substrate.

All private text is in suppressible artifact payloads. Indexes/links never bypass
artifact/source permission checks. Encoding and consolidation commit together.
"""
import asyncio
import hashlib
import math
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta

from cognition.memory_dynamics import (tokens,lexical_vector,cosine,encode_details,
                                      reconstruct,reactivate,detail_state)
from cognition.relationships import relationship_transition,initial_relationship
from cognition.serialization import dump,object_value,load_event,load_state
from cognition.situations import Situation,goal_transition
from cognition.types import MODEL_VERSION,Origin,utc
from cognition.repositories import SuppressedEvidence,StaleRevision
from cognition.semantic import query_terms,trace_names,retrieval_signals
from cognition.retrieval import RetrievalResult
from asyncpg import QueryCanceledError

EMBEDDING_VERSION = 'arti-subword-192-v1'


def key(kind,*parts):
    return kind + ':' + hashlib.sha256(dump(list(parts)).encode()).hexdigest()[:40]


class MemoryRepository:
    def __init__(self,pool,semantic=None):
        self.pool = pool
        self.semantic = semantic

    async def _put(self,conn,cid,kind,identity,payload,owner,sources,parents=()):
        sources = set(sources)
        existing = await conn.fetchrow('SELECT id,owner_id,suppressed_at FROM cognitive_artifacts WHERE context_id=$1 AND artifact_key=$2',cid,identity)
        if existing and existing['owner_id']!=owner:
            raise ValueError('Artifact identity cannot change owner')
        for parent in parents:
            if existing and existing['suppressed_at'] is None and parent==existing['id']:
                # Existing flattened dependencies are already durable. Re-reading
                # and reinserting the full lifetime history on each contact is
                # quadratic and adds no new provenance.
                continue
            valid = await conn.fetchval('SELECT 1 FROM cognitive_artifacts WHERE context_id=$1 AND id=$2 AND suppressed_at IS NULL',cid,parent)
            if not valid:
                raise SuppressedEvidence()
            sources.update(await conn.fetchval('SELECT ARRAY_AGG(source_event_id) FROM cognitive_provenance WHERE context_id=$1 AND artifact_id=$2',cid,parent) or [])
        rows = await conn.fetch('SELECT id,owner_id FROM cognitive_events WHERE context_id=$1 AND id=ANY($2::bigint[]) AND suppressed_at IS NULL',cid,sorted(sources))
        if not sources or {r['id'] for r in rows} != sources:
            raise SuppressedEvidence()
        if any(r['owner_id'] != owner for r in rows):
            raise ValueError('Personal projections cannot change source ownership')
        if existing and existing['suppressed_at'] is not None:
            await conn.execute('DELETE FROM cognitive_provenance WHERE context_id=$1 AND artifact_id=$2',cid,existing['id'])
            await conn.execute('DELETE FROM cognitive_artifact_parents WHERE context_id=$1 AND child_id=$2',cid,existing['id'])
        aid = await conn.fetchval('''
            INSERT INTO cognitive_artifacts(context_id,owner_id,kind,artifact_key,model_version,payload,projection_epoch)
            VALUES($1,$2,$3,$4,$5,$6::jsonb,(SELECT suppression_epoch FROM cognitive_contexts WHERE id=$1))
            ON CONFLICT(context_id,artifact_key) WHERE artifact_key IS NOT NULL DO UPDATE
            SET payload=EXCLUDED.payload,model_version=EXCLUDED.model_version,projection_epoch=EXCLUDED.projection_epoch,suppressed_at=NULL,revision=cognitive_artifacts.revision+1
            RETURNING id
        ''',cid,owner,kind,identity,MODEL_VERSION,dump(payload))
        # Each mutable projection retains all independent source dependencies.
        await conn.executemany('INSERT INTO cognitive_provenance(context_id,artifact_id,source_event_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING',[(cid,aid,s) for s in sorted(sources)])
        for parent in parents:
            await conn.execute('INSERT INTO cognitive_artifact_parents VALUES($1,$2,$3) ON CONFLICT DO NOTHING',cid,aid,parent)
        return aid

    async def _get(self,conn,cid,identity):
        row = await conn.fetchrow('SELECT * FROM cognitive_artifacts WHERE context_id=$1 AND artifact_key=$2 AND suppressed_at IS NULL AND projection_epoch=(SELECT suppression_epoch FROM cognitive_contexts WHERE id=$1)',cid,identity)
        return (dict(row),object_value(row['payload'])) if row else (None,None)

    async def encode(self,cid,eid,perception,before=None,after=None,*,worker_token=None,expected_epoch=None):
        async with self.pool.acquire() as conn,conn.transaction():
            context = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
            if expected_epoch is not None and context['suppression_epoch']!=expected_epoch:
                raise SuppressedEvidence()
            if worker_token is not None:
                valid = await conn.fetchval("SELECT 1 FROM cognitive_jobs WHERE context_id=$1 AND event_id=$2 AND status='running' AND lease_token=$3 AND lease_until>NOW()",cid,eid,worker_token)
                if not valid or context['worker_token']!=worker_token:
                    raise StaleRevision()
            row = await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1 AND id=$2 AND suppressed_at IS NULL',cid,eid)
            if not row:
                raise SuppressedEvidence()
            done = await conn.fetchval('''SELECT 1 FROM cognitive_projection_effects f JOIN cognitive_events e ON e.id=f.event_id
                WHERE f.context_id=$1 AND e.independent_group=$2 AND f.phase='encode' AND f.model_version=$3''',cid,row['independent_group'],MODEL_VERSION)
            if done:
                return None
            event = load_event(row['payload'])
            owner = row['owner_id']
            s = perception.situation or Situation()
            # Snapshots are a personal projection of this cause, never the
            # shared chat accumulator containing another participant's causes.
            from cognition.affect import initial_state,appraise,affect,EMOTIONS
            state_key = key('personal_state',owner)
            state_row,personal = await self._get(conn,cid,state_key)
            cause_before = load_state(personal['state']) if personal else initial_state(event.context,event.observed_at)
            cause_after = appraise(cause_before,event,perception)
            from dataclasses import replace
            cause_after = replace(cause_after,applied_groups=frozenset())
            await self._put(conn,cid,'personal_state',state_key,dict(state=object_value(dump(cause_after))),owner,[eid],[state_row['id']] if state_row else [])
            if perception.situation:
                eligible = await conn.fetch('SELECT source_id FROM cognitive_events WHERE context_id=$1 AND owner_id IS NOT DISTINCT FROM $2 AND suppressed_at IS NULL',cid,owner)
                s = Situation.from_dict(object_value(dump(s)),event,{r['source_id'] for r in eligible})
            if event.evidence.origin not in (Origin.USER,Origin.DELIVERED_ACTION):
                return None
            salience = min(1.,sum(e.intensity for e in after.episodes if e.cause_id==event.event_id)) if after else 0.
            details = encode_details(event,s,affect(after)['attention'] if after else 1.,salience)
            topic = s.topic or 'unspecified'
            last = await conn.fetchrow('''SELECT * FROM cognitive_artifacts WHERE context_id=$1 AND owner_id IS NOT DISTINCT FROM $2
                AND kind='episode' AND suppressed_at IS NULL ORDER BY id DESC LIMIT 1''',cid,owner)
            prior = object_value(last['payload']) if last else None
            gap = (event.observed_at-datetime.fromisoformat(prior['last_at'])).total_seconds() if prior else math.inf
            boundary = ('start' if not prior else ('time_gap' if gap>1800 else ('topic' if prior['topic']!=topic else 'continuation')))
            episode_key = prior['key'] if boundary=='continuation' else key('episode',event.event_id)
            episode = prior if boundary=='continuation' else dict(key=episode_key,topic=topic,started_at=event.observed_at.isoformat(),
                                                                  events=[],boundary=boundary,participants=[owner],parent_period=event.observed_at.strftime('%Y-%m'))
            episode = {**episode,'last_at':event.observed_at.isoformat(),'events':episode['events']+[eid]}
            episode_id = await self._put(conn,cid,'episode',episode_key,episode,owner,[eid],([last['id']] if boundary=='continuation' else []))
            trace = dict(source_id=event.evidence.source_id,event_id=eid,author_id='arti' if event.evidence.origin==Origin.DELIVERED_ACTION else event.actor_id,
                         audience=asdict(event.audience),group=row['independent_group'],episode_id=episode_id,
                         gist=' '.join(d['text'] for d in details),topic=topic,details=details,observed_at=event.observed_at.isoformat(),
                         occurred_at=event.occurred_at.isoformat(),modality='delivered_action' if event.evidence.origin==Origin.DELIVERED_ACTION else s.modality,
                         salience=salience,version=1,interpretation='',replay_count=0,last_replay=None,
                         emotional_valence=sum(EMOTIONS[e.emotion][0]*e.intensity for e in cause_after.episodes if e.cause_id==event.event_id),
                         before=affect(cause_before),after=affect(cause_after))
            trace_id = await self._put(conn,cid,'trace',key('trace',event.event_id),trace,owner,[eid])
            await conn.execute('INSERT INTO cognitive_embeddings VALUES($1,$2,$3,$4::double precision[],$5) ON CONFLICT DO NOTHING',cid,trace_id,EMBEDDING_VERSION,lexical_vector(trace['gist']),MODEL_VERSION)
            if last:
                earlier = await conn.fetchrow('''SELECT id,payload FROM cognitive_artifacts WHERE context_id=$1 AND owner_id IS NOT DISTINCT FROM $2
                    AND kind='trace' AND id<$3 AND suppressed_at IS NULL ORDER BY id DESC LIMIT 1''',cid,owner,trace_id)
                if earlier:
                    p = object_value(earlier['payload'])
                    link_kind = 'topic' if p['topic']==topic else 'temporal'
                    await conn.execute('INSERT INTO cognitive_memory_links VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING',cid,earlier['id'],trace_id,link_kind,.65 if link_kind=='topic' else .25)
            if event.evidence.origin==Origin.USER:
                rkey = key('relationship',owner)
                rr,relationship = await self._get(conn,cid,rkey)
                relationship = relationship_transition(relationship,event,s)
                await self._put(conn,cid,'relationship',rkey,relationship,owner,[eid],([rr['id']] if rr else []))
                gkey = key('goals',owner)
                gr,goals = await self._get(conn,cid,gkey)
                await self._put(conn,cid,'goals',gkey,goal_transition(goals or {},s,event),owner,[eid],([gr['id']] if gr else []))
                await self._learn_beliefs(conn,cid,eid,event,s)
                await self._intentions(conn,cid,eid,event,s)
            elif event.evidence.origin==Origin.DELIVERED_ACTION:
                await self._intentions(conn,cid,eid,event,s)
            period_key = key('autobiography',owner,event.observed_at.strftime('%Y-%m'))
            ar,period = await self._get(conn,cid,period_key)
            period = period or dict(period=event.observed_at.strftime('%Y-%m'),projects={},episode_ids=[],delivered_actions=[],rituals={})
            period['projects'][topic] = period['projects'].get(topic,0)+1
            period['episode_ids'] = list(dict.fromkeys(period['episode_ids']+[episode_id]))
            if event.evidence.origin==Origin.DELIVERED_ACTION:
                period['delivered_actions'].append(eid)
            else:
                period['rituals'][topic] = period['projects'][topic] >= 3
            await self._put(conn,cid,'autobiography',period_key,period,owner,[eid],([ar['id']] if ar else []))
            if s.kind in ('loss','threat','conflict') or s.outcome=='pending':
                await self._put(conn,cid,'concern',key('concern',event.evidence.source_id),
                                dict(source_id=event.evidence.source_id,topic=topic,status='open',kind=s.kind,
                                     since=event.observed_at.isoformat(),confidence=max((a.confidence for a in perception.appraisals),default=.5)),owner,[eid])
            await conn.execute("INSERT INTO cognitive_projection_effects(context_id,event_id,phase,model_version) VALUES($1,$2,'encode',$3)",cid,eid,MODEL_VERSION)
            await conn.execute('UPDATE cognitive_contexts SET projection_revision=projection_revision+1 WHERE id=$1',cid)
            return trace_id

    async def _learn_beliefs(self,conn,cid,eid,event,s):
        if s.modality in ('quoted','hypothetical'):
            return
        for proposal in s.beliefs:
            identity = key('belief',event.evidence.owner_id,proposal['subject'],proposal['predicate'],proposal['condition'])
            oldrow,old = await self._get(conn,cid,identity)
            span = s.spans[proposal['span']]
            support = key('assertion',event.actor_id,proposal['value'],proposal['condition'])
            changed = old is not None and old['value']!=proposal['value']
            if changed:
                historical = {**old,'status':'superseded','valid_until':event.occurred_at.isoformat(),
                              'superseded_by':event.evidence.source_id,'superseded_at':event.observed_at.isoformat()}
                await self._put(conn,cid,'belief_version',key('belief_version',oldrow['id'],oldrow['revision']),historical,
                                event.evidence.owner_id,[],[oldrow['id']])
            confidence = min(proposal['confidence'],.65 if proposal['assertion']=='inferred' else .95)
            value = dict(subject=proposal['subject'],predicate=proposal['predicate'],value=proposal['value'],
                         condition=proposal['condition'],assertion=proposal['assertion'],confidence=confidence,
                         source_id=event.evidence.source_id,source_span=asdict(span),status='current',
                         observed_at=event.observed_at.isoformat(),occurred_at=event.occurred_at.isoformat(),
                         time_basis='source_event',supersedes=([old['source_id']] if changed else old.get('supersedes',[]) if old else []),
                         valid_from=event.occurred_at.isoformat(),valid_until=None,
                         support_groups=sorted(set((old['support_groups'] if old and not changed else [])+[support])),
                         version=(old.get('version',1)+1 if changed else old.get('version',1)) if old else 1)
            # A repeated declaration is one assertion group, not corroboration.
            if old and not changed:
                value['confidence'] = max(old['confidence'],confidence)
                value['valid_from'] = old['valid_from']
            await self._put(conn,cid,'belief',identity,value,event.evidence.owner_id,[eid],([oldrow['id']] if oldrow and not changed else []))

    async def _intentions(self,conn,cid,eid,event,s):
        if s.modality in ('quoted','hypothetical'):
            return
        from cognition.private_followup import materially_revised
        for item in s.intentions:
            stated_deadline = item.get('deadline')
            actor = item.get('actor','arti' if event.evidence.origin==Origin.DELIVERED_ACTION else event.actor_id)
            identity = key('intention',event.evidence.owner_id,actor,item['key'])
            previous,old = await self._get(conn,cid,identity)
            if (old and old.get('status')==item['status'] and item['status'] in ('open','reminder')
                    and item.get('deadline') is None and old.get('deadline')):
                # Omission in a paraphrase is not an explicit reschedule or a
                # revocation of the previously source-backed deadline.
                item = {**item,'deadline':old['deadline']}
            revised = old is not None and materially_revised(old,item)
            revision = (old.get('goal_revision',1) if old else 1) + int(revised)
            # A paraphrase, new confidence or a repeated cue is the same goal.
            # Operationally changed/reopened goals get a new durable send slot;
            # the artifact's ordinary projection revision is not a send identity.
            delivery_key = (key('intention_delivery',identity,revision,event.evidence.source_id)
                            if revised else old.get('delivery_key',identity) if old else identity)
            value = {**item,'source_id':event.evidence.source_id,'actor_id':actor,
                     'source_span':asdict(s.spans[item['span']]),'topic':s.topic,
                     'created_at':old['created_at'] if old and not revised else event.observed_at.isoformat(),
                     'updated_at':event.observed_at.isoformat(),'goal_revision':revision,
                     'closed_at':event.observed_at.isoformat() if item['status'] in ('fulfilled','cancelled') else None,
                     'delivery_key':delivery_key,'delivered':False if old is None or revised else old.get('delivered',False)}
            if old and not revised and old.get('delivered_at'):
                value['delivered_at'] = old['delivered_at']
            if item['status']=='reminder':
                if event.evidence.origin==Origin.USER and s.kind=='request' and stated_deadline is not None:
                    value['reminder_request_source_id'] = event.evidence.source_id
                elif old and old.get('status')=='reminder' and not revised:
                    # An unchanged reminder retains its exact authorization,
                    # independently of a later neutral descriptive update.
                    # Reopening/rescheduling must establish a fresh request.
                    value['reminder_request_source_id'] = old.get('reminder_request_source_id') or old.get('source_id')
            await self._put(conn,cid,'intention',identity,value,event.evidence.owner_id,[eid],([previous['id']] if previous else []))

    async def artifacts(self,cid,owner,kind=None,limit=200,query=None):
        search = ' OR '.join(sorted(tokens(query))) if query else None
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''SELECT a.* FROM cognitive_artifacts a WHERE context_id=$1
                AND NOT (SELECT rebuilding FROM cognitive_contexts WHERE id=$1)
                AND (owner_id IS NULL OR owner_id=$2) AND suppressed_at IS NULL AND payload IS NOT NULL
                AND (kind!='trace' OR owner_id IS NOT DISTINCT FROM $2)
                AND model_version=$3 AND ($4::text IS NULL OR kind=$4)
                AND projection_epoch=(SELECT suppression_epoch FROM cognitive_contexts WHERE id=$1)
                AND NOT EXISTS (WITH RECURSIVE dependencies(id) AS (
                    SELECT p.source_event_id FROM cognitive_provenance p WHERE p.context_id=a.context_id AND p.artifact_id=a.id
                    UNION SELECT d.source_event_id FROM cognitive_event_dependencies d JOIN dependencies previous ON previous.id=d.event_id
                        WHERE d.context_id=a.context_id)
                    SELECT 1 FROM dependencies d LEFT JOIN cognitive_events e ON e.context_id=a.context_id AND e.id=d.id
                    WHERE e.id IS NULL OR e.suppressed_at IS NOT NULL OR e.payload IS NULL OR e.owner_id IS DISTINCT FROM a.owner_id)
                AND (a.kind!='trace' OR EXISTS(SELECT 1 FROM cognitive_events e WHERE e.context_id=a.context_id
                    AND e.id::text=a.payload->>'event_id' AND e.source_id=a.payload->>'source_id'
                    AND e.owner_id IS NOT DISTINCT FROM a.owner_id AND e.suppressed_at IS NULL AND e.payload IS NOT NULL))
                ORDER BY CASE WHEN $6::text IS NULL THEN 0 ELSE
                    ts_rank(to_tsvector('russian',arti_memory_search_text(coalesce(payload->>'gist',''))),websearch_to_tsquery('russian',arti_memory_search_text($6)))
                    + CASE WHEN payload->>'gist' ILIKE '%' || $6 || '%' THEN 1 ELSE 0 END END DESC,a.id DESC
                LIMIT $5''',cid,owner,MODEL_VERSION,kind,min(2000,limit),search)
        return [{**dict(r),'payload':object_value(r['payload'])} for r in rows]

    async def beliefs_for_query(self,cid,owner,query,recollections=(),limit=16):
        """Select current claims by query AND recalled historical source lineage.

        Corrections such as "Actually, Tomsk" inherit relevance from the exact
        belief key of the recalled old assertion, not from a guessed predicate.
        Historic utterances remain available with explicit supersession metadata.
        """
        sources = [r['source_id'] for r in recollections]
        search = ' OR '.join(sorted(query_terms(query)))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""WITH allowed AS (
                SELECT a.* FROM cognitive_artifacts a JOIN cognitive_contexts c ON c.id=a.context_id
                WHERE a.context_id=$1 AND a.owner_id IS NOT DISTINCT FROM $2
                AND a.kind IN ('belief','belief_version') AND a.model_version=$3
                AND a.suppressed_at IS NULL AND a.payload IS NOT NULL
                AND NOT c.rebuilding AND a.projection_epoch=c.suppression_epoch
                AND EXISTS(SELECT 1 FROM cognitive_provenance p WHERE p.artifact_id=a.id)
                AND NOT EXISTS(WITH RECURSIVE dependencies(id) AS (
                    SELECT p.source_event_id FROM cognitive_provenance p WHERE p.context_id=a.context_id AND p.artifact_id=a.id
                    UNION SELECT d.source_event_id FROM cognitive_event_dependencies d JOIN dependencies previous ON previous.id=d.event_id
                        WHERE d.context_id=a.context_id)
                    SELECT 1 FROM dependencies d LEFT JOIN cognitive_events e ON e.context_id=a.context_id AND e.id=d.id
                    WHERE e.id IS NULL OR e.suppressed_at IS NOT NULL OR e.payload IS NULL OR e.owner_id IS DISTINCT FROM a.owner_id))
                SELECT a.*, CASE WHEN a.payload->>'source_id'=ANY($4::text[]) OR EXISTS(
                    SELECT 1 FROM allowed h WHERE h.kind='belief_version' AND h.payload->>'source_id'=ANY($4::text[])
                    AND h.payload->'subject'=a.payload->'subject' AND h.payload->>'predicate'=a.payload->>'predicate'
                    AND h.payload->>'condition'=a.payload->>'condition') THEN 1.0 ELSE 0.0 END
                    + ts_rank(to_tsvector('russian',arti_memory_search_text(concat_ws(' ',a.payload->>'predicate',a.payload->>'value',
                        a.payload->>'condition',a.payload->'source_span'->>'text'))),websearch_to_tsquery('russian',arti_memory_search_text($5))) AS query_rank
                FROM allowed a WHERE a.kind='belief' AND a.payload->>'status'='current'
                ORDER BY query_rank DESC,a.id DESC LIMIT $6""",cid,owner,MODEL_VERSION,sources,search,min(64,limit))
            selected = [{**dict(r),'payload':object_value(r['payload'])} for r in rows if r['query_rank']>0]
            history = await conn.fetch("""SELECT a.* FROM cognitive_artifacts a JOIN cognitive_contexts c ON c.id=a.context_id
                WHERE a.context_id=$1 AND a.owner_id IS NOT DISTINCT FROM $2 AND a.kind='belief_version'
                AND a.model_version=$3 AND a.suppressed_at IS NULL AND a.payload IS NOT NULL
                AND NOT c.rebuilding AND a.projection_epoch=c.suppression_epoch
                AND a.payload->>'source_id'=ANY($4::text[])
                AND NOT EXISTS(WITH RECURSIVE dependencies(id) AS (
                    SELECT p.source_event_id FROM cognitive_provenance p WHERE p.context_id=a.context_id AND p.artifact_id=a.id
                    UNION SELECT d.source_event_id FROM cognitive_event_dependencies d JOIN dependencies previous ON previous.id=d.event_id
                        WHERE d.context_id=a.context_id)
                    SELECT 1 FROM dependencies d LEFT JOIN cognitive_events e ON e.context_id=a.context_id AND e.id=d.id
                    WHERE e.id IS NULL OR e.suppressed_at IS NOT NULL OR e.payload IS NULL OR e.owner_id IS DISTINCT FROM a.owner_id)
                ORDER BY a.id DESC LIMIT 128""",cid,owner,MODEL_VERSION,sources)
        for recollection in recollections:
            updates = []
            for row in history:
                old = object_value(row['payload'])
                if old['source_id']!=recollection['source_id']:
                    continue
                current = next((r for r in selected if all(r['payload'].get(k)==old.get(k)
                    for k in ('subject','predicate','condition'))),None)
                updates.append(dict(subject=old['subject'],predicate=old['predicate'],condition=old['condition'],
                    status=old['status'],valid_from=old.get('valid_from'),valid_until=old.get('valid_until'),
                    superseded_by=current['payload']['source_id'] if current else old.get('superseded_by')))
            if updates:
                recollection['belief_history'] = updates
        return selected

    async def candidates(self,cid,owner,query,limit=512):
        from cognition.source_chunks import lexical_windows,chunk_trace
        diagnostics = dict(status='unavailable',semantic_status='unavailable',lexical_status='complete',scope='private')
        rows = []
        try:
            async with asyncio.timeout(.5):
                rows = await self.artifacts(cid,owner,'trace',limit=limit,query=query)
        except (TimeoutError,QueryCanceledError):
            diagnostics['lexical_status'] = 'timeout'
        # Full-source FTS remains useful while background semantic coverage is
        # incomplete. Excerpt positions use the SAME PostgreSQL stemmer, never
        # an exact substring that can silently return an unrelated source prefix.
        search = ' OR '.join(sorted(query_terms(query)))
        if search:
            try:
                async with asyncio.timeout(.5),self.pool.acquire() as conn,conn.transaction():
                    await conn.execute("SET LOCAL statement_timeout = '450ms'")
                    sources = await conn.fetch("""SELECT a.*,e.payload AS source_payload FROM cognitive_artifacts a
                        JOIN cognitive_contexts c ON c.id=a.context_id
                        JOIN cognitive_events e ON e.context_id=a.context_id AND e.id=(a.payload->>'event_id')::bigint
                        WHERE a.context_id=$1 AND a.owner_id IS NOT DISTINCT FROM $2 AND e.owner_id IS NOT DISTINCT FROM $2
                        AND a.kind='trace' AND a.model_version=$3 AND a.suppressed_at IS NULL AND a.payload IS NOT NULL
                        AND e.suppressed_at IS NULL AND e.payload IS NOT NULL AND e.source_id=a.payload->>'source_id'
                        AND c.authority='active' AND NOT c.rebuilding AND a.projection_epoch=c.suppression_epoch
                        AND length(e.payload->>'text')>420
                        AND to_tsvector('russian',arti_memory_search_text(e.payload->>'text')) @@ websearch_to_tsquery('russian',arti_memory_search_text($4))
                        AND NOT EXISTS(WITH RECURSIVE dependencies(id) AS (
                            SELECT p.source_event_id FROM cognitive_provenance p WHERE p.context_id=a.context_id AND p.artifact_id=a.id
                            UNION SELECT d.source_event_id FROM cognitive_event_dependencies d JOIN dependencies previous ON previous.id=d.event_id
                                WHERE d.context_id=a.context_id)
                            SELECT 1 FROM dependencies d LEFT JOIN cognitive_events z ON z.context_id=a.context_id AND z.id=d.id
                            WHERE z.id IS NULL OR z.suppressed_at IS NOT NULL OR z.payload IS NULL OR z.owner_id IS DISTINCT FROM a.owner_id)
                        ORDER BY ts_rank(to_tsvector('russian',arti_memory_search_text(e.payload->>'text')),websearch_to_tsquery('russian',arti_memory_search_text($4))) DESC,a.id DESC LIMIT 32""",cid,owner,MODEL_VERSION,search)
                    for row in sources:
                        trace = object_value(row['payload'])
                        event = object_value(row['source_payload'])
                        source = event['text']
                        # A name by itself cannot make an unrelated event relevant.
                        event_words = query_terms(query)-trace_names(trace)
                        windows = await lexical_windows(conn,source,' OR '.join(sorted(event_words)),limit=3)
                        if not windows:
                            continue
                        rows = [r for r in rows if r['id']!=row['id']]
                        for start,end,_ in windows:
                            item={k:v for k,v in dict(row).items() if k!='source_payload'}
                            item['payload']=chunk_trace(trace,source,start,end)
                            item['payload']['author_id']='arti' if event['evidence']['origin']=='delivered_action' else event.get('actor_id')
                            item['payload']['audience']=event.get('audience')
                            item['lexical_match']=True
                            rows.append(item)
            except (TimeoutError,QueryCanceledError):
                diagnostics['lexical_status']='timeout'
        if self.semantic is not None:
            semantic = await self.semantic.search(cid,owner,query)
            diagnostics.update(getattr(semantic,'diagnostics',{}))
            rows.extend(semantic)
        if diagnostics['lexical_status']=='timeout':
            diagnostics['status']='timeout'
        # Keep independent matching passages. Merge only exact duplicates, then
        # drop overlapping lower-ranked windows and cap each source at three.
        chunked = {r['id'] for r in rows if r['payload'].get('record_start') is not None}
        rows = [r for r in rows if r['id'] not in chunked or r['payload'].get('record_start') is not None]
        merged = {}
        for row in rows:
            p=row['payload']; identity=(row['id'],p.get('record_start'),p.get('record_end'))
            if identity in merged:
                old=merged[identity]
                row={**old,**row,'semantic_score':max(old.get('semantic_score',0.),row.get('semantic_score',0.)),
                     'lexical_match':old.get('lexical_match',False) or row.get('lexical_match',False)}
            merged[identity]=row
        words=query_terms(query)
        ranked=sorted(merged.values(),key=lambda r:-(
            .55*len(words & query_terms(r['payload']['gist']))/max(1,len(words))
            +.45*r.get('semantic_score',0.)+.25*bool(r.get('lexical_match'))))
        selected=[]; by_source={}
        for row in ranked:
            previous=by_source.setdefault(row['id'],[])
            start,end=row['payload'].get('record_start'),row['payload'].get('record_end')
            if previous and (start is None or any(a is None or max(0,min(end,b)-max(start,a))>min(end-start,b-a)*.35 for a,b in previous)):
                continue
            if len(previous)>=3:
                continue
            previous.append((start,end)); selected.append(row)
        return RetrievalResult(selected,diagnostics=diagnostics)

    async def open_intentions(self,cid,owner,query='',limit=16):
        search = ' OR '.join(sorted(tokens(query)))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""SELECT * FROM cognitive_artifacts WHERE context_id=$1 AND owner_id=$2
                AND NOT (SELECT rebuilding FROM cognitive_contexts WHERE id=$1)
                AND kind='intention' AND suppressed_at IS NULL AND payload->>'status' IN ('open','reminder')
                AND model_version=$3 AND projection_epoch=(SELECT suppression_epoch FROM cognitive_contexts WHERE id=$1)
                ORDER BY ts_rank(to_tsvector('russian',arti_memory_search_text(payload->>'description')),websearch_to_tsquery('russian',arti_memory_search_text($4))) DESC,id DESC LIMIT $5""",cid,owner,MODEL_VERSION,search,limit)
        return [{**dict(r),'payload':object_value(r['payload'])} for r in rows]

    async def relationship(self,cid,owner):
        async with self.pool.acquire() as conn:
            _,value = await self._get(conn,cid,key('relationship',owner))
        return value or initial_relationship()

    async def retrieve(self,cid,owner,query,at,cycle_key,*,mood=0.,archive=False,limit=8):
        try:
            async with asyncio.timeout(4.):
                return await self._retrieve(cid,owner,query,at,cycle_key,mood=mood,archive=archive,limit=limit)
        except (TimeoutError,QueryCanceledError):
            return RetrievalResult(diagnostics=dict(status='timeout',lexical_status='timeout',semantic_status='timeout',scope='private'))

    async def _retrieve(self,cid,owner,query,at,cycle_key,*,mood=0.,archive=False,limit=8):
        at = utc(at)
        if archive:
            return await self.archive_lookup(cid,owner,query,at,cycle_key,limit)
        words = tokens(query)
        qvector = lexical_vector(query)
        traces = await self.candidates(cid,owner,query)
        topics = Counter(r['payload']['topic'] for r in {r['id']:r for r in traces}.values())
        requested_names=query_terms(query) & set().union(*(trace_names(r['payload']) for r in traces))
        candidates = {}
        async with self.pool.acquire() as conn:
            # Vector model/version are part of every query and persisted key.
            rows = await conn.fetch('''SELECT artifact_id,vector FROM cognitive_embeddings WHERE context_id=$1
                AND embedding_model=$2 AND model_version=$3 AND artifact_id=ANY($4::bigint[])''',cid,EMBEDDING_VERSION,MODEL_VERSION,[r['id'] for r in traces])
            vectors = {r['artifact_id']:r['vector'] for r in rows}
            links = await conn.fetch('SELECT * FROM cognitive_memory_links WHERE context_id=$1 AND source_id=ANY($2::bigint[]) AND target_id=ANY($2::bigint[])',cid,[r['id'] for r in traces])
        for row in traces:
            t = row['payload']
            signals=retrieval_signals(query,t,requested_names=requested_names,semantic_score=row.get('semantic_score',0.))
            if row.get('lexical_match'):
                names=trace_names(t)
                if not (requested_names and names and not names & requested_names):
                    signals['eligible']=True
                    signals['lexical']=max(signals['lexical'],.5)
            overlap=signals['lexical']
            vector = max(0.,cosine(qvector,vectors.get(row['id'],[])))
            semantic = row.get('semantic_score',0.)
            cue = max(overlap,semantic,signals['topic'],signals['action'])
            competition = (topics[t['topic']]-1)/12
            recollection = reconstruct(t,at,cue,competition,archive)
            recollection.update({k:t[k] for k in ('record_start','record_end','source_chunk','evidence_status','author_id','audience') if k in t})
            if t.get('source_prefix') and recollection['details']:
                prefix=t['source_prefix']
                visible={(d['kind'],d['text']) for d in recollection['details']}
                for d in t['details']:
                    if d['kind'] not in ('gist','wording') and (d['kind'],d['text']) not in visible:
                        prefix=prefix.replace(d['text'],'[деталь не вспоминается]')
                for d in t.get('_source_prefix_details',()):
                    state=detail_state(d,datetime.fromisoformat(t['observed_at']),at,competition,cue)
                    if state['fidelity']<.5 or state['accessibility']<.28:
                        prefix=prefix.replace(d['text'],'[деталь не вспоминается]')
                recollection['source_prefix']=prefix
            accessible = max((d['accessibility'] for d in recollection['details']),default=.05)
            # Mood bias is small; no candidate is removed by its emotional sign.
            bias = max(-.05,min(.05,mood*t.get('emotional_valence',0.)*.05))
            score = (max(.55*overlap+.25*vector,.55*semantic+.20*overlap)
                     +.12*signals['topic']+.12*signals['action']+.25*signals['name']+.15*accessible+bias)
            candidates[(row['id'],t.get('record_start'),t.get('record_end'))] = dict(row=row,score=score,cue=cue,recollection=recollection,eligible=signals['eligible'])
        # Spreading activation is source-level: several excerpts do not become
        # independent corroborating memories or multiply a link's weight.
        activation = {}
        for item in candidates.values():
            aid=item['row']['id']
            activation[aid]=max(activation.get(aid,0.),max(0.,item['score']-.2) if item['eligible'] else 0.)
        for _ in range(2):
            increment = {}
            for link in links:
                for source,target in ((link['source_id'],link['target_id']),(link['target_id'],link['source_id'])):
                    increment[target] = max(increment.get(target,0.),activation.get(source,0.)*link['weight']*.25)
            activation = {i:max(value,increment.get(i,0.)) for i,value in activation.items()}
            for item in candidates.values():
                item['score'] += min(.08,increment.get(item['row']['id'],0.))
        ranked = sorted(candidates.values(),key=lambda v:(-v['score'],-v['row']['id']))
        selected = [r for r in ranked if r['eligible'] and r['score']>.18][:limit]
        # Reserve one alternative/counterexample from a different emotional context.
        if len(selected)>=3:
            seen = {r['row']['id'] for r in selected}
            opposite = next((r for r in ranked if r['row']['id'] not in seen and r['eligible'] and r['score']>.18
                             and (r['row']['payload'].get('emotional_valence',0.)*mood<=0 or r['row']['payload']['salience']<.2)),None)
            if opposite:
                selected[-1] = opposite
        await self.record_retrieval(cid,owner,cycle_key,'candidate',[r['row']['id'] for r in ranked[:32]],at)
        await self.record_retrieval(cid,owner,cycle_key,'archive_checked' if archive else 'recalled',[r['row']['id'] for r in selected],at)
        if not archive:
            await self._reactivate(cid,owner,selected,at,cycle_key)
        return RetrievalResult([dict(artifact_id=r['row']['id'],score=r['score'],**r['recollection']) for r in selected],diagnostics=getattr(traces,'diagnostics',{}))

    async def archive_lookup(self,cid,owner,query,at,cycle_key,limit=8):
        """Explicit source verification can recover details never encoded in gist."""
        from cognition.source_chunks import lexical_windows
        result = []
        async with self.pool.acquire() as conn,conn.transaction():
            context=await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
            if context['rebuilding']:
                raise SuppressedEvidence()
            rows = await conn.fetch("""SELECT e.* FROM cognitive_events e WHERE e.context_id=$1 AND e.owner_id=$2
                AND e.suppressed_at IS NULL AND e.payload IS NOT NULL AND e.origin IN ('user','delivered_action')
                AND NOT EXISTS(WITH RECURSIVE dependencies(id) AS (
                    SELECT e.id UNION SELECT d.source_event_id FROM cognitive_event_dependencies d
                        JOIN dependencies previous ON previous.id=d.event_id WHERE d.context_id=$1)
                    SELECT 1 FROM dependencies d LEFT JOIN cognitive_events source ON source.context_id=$1 AND source.id=d.id
                    WHERE source.id IS NULL OR source.suppressed_at IS NOT NULL OR source.payload IS NULL
                        OR source.owner_id IS DISTINCT FROM e.owner_id)
                AND to_tsvector('russian',arti_memory_search_text(payload->>'text')) @@ websearch_to_tsquery('russian',arti_memory_search_text($3))
                ORDER BY ts_rank(to_tsvector('russian',arti_memory_search_text(payload->>'text')),websearch_to_tsquery('russian',arti_memory_search_text($3))) DESC,id DESC LIMIT $4""",cid,owner,query,min(limit,16))
            for row in rows:
                ev = load_event(row['payload'])
                for start,end,excerpt in await lexical_windows(conn,ev.text,query,width=1400,limit=min(3,max(0,limit-len(result)))):
                    p = dict(source_id=ev.evidence.source_id,gist=excerpt,event_id=row['id'],record_start=start,record_end=end,
                             observed_at=ev.observed_at.isoformat(),occurred_at=ev.occurred_at.isoformat(),source_record_verified=True)
                    aid = await self._put(conn,cid,'archive_record',key('archive',ev.event_id,start),p,owner,[row['id']])
                    result.append(dict(artifact_id=aid,source_id=ev.evidence.source_id,details=[dict(text=excerpt,kind='wording',confidence=.5,verbatim_verified=True)],
                        record_start=start,record_end=end,source_prefix=ev.text[:240] if start else '',
                        author_id='arti' if ev.evidence.origin==Origin.DELIVERED_ACTION else ev.actor_id,audience=asdict(ev.audience),
                        observed_at=ev.observed_at.isoformat(),occurred_at=ev.occurred_at.isoformat(),time_basis='source_event',
                        time_precision='source_record',modality='delivered_action' if ev.evidence.origin==Origin.DELIVERED_ACTION else 'source_utterance',
                        interpretation='',version=1,familiarity=.5,uncertainty=False,score=1.))
                if len(result)>=limit:
                    break
        await self.record_retrieval(cid,owner,cycle_key,'archive_checked',[r['artifact_id'] for r in result],at)
        return RetrievalResult(result,diagnostics=dict(status='complete',lexical_status='complete',semantic_status='not_requested',scope='private_archive'))

    async def record_retrieval(self,cid,owner,cycle,stage,ids,at):
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
            allowed = await conn.fetch('SELECT id FROM cognitive_artifacts WHERE context_id=$1 AND id=ANY($2::bigint[]) AND suppressed_at IS NULL AND (owner_id IS NULL OR owner_id=$3) AND projection_epoch=(SELECT suppression_epoch FROM cognitive_contexts WHERE id=$1)',cid,ids,owner)
            permitted = sorted(r['id'] for r in allowed)
            return await conn.fetchval('''INSERT INTO cognitive_retrievals(context_id,cycle_key,owner_id,stage,artifact_ids,created_at)
                VALUES($1,$2,$3,$4,$5::bigint[],$6) ON CONFLICT(context_id,cycle_key,stage) DO NOTHING RETURNING id''',cid,cycle,owner,stage,permitted,at)

    async def validate_retrieval(self,cid,owner,ids,*,expected_epoch=None,connection=None):
        """Recheck private source closure at prompt inclusion and final send."""
        try:
            async with asyncio.timeout(1.):
                if connection is None:
                    async with self.pool.acquire() as conn,conn.transaction():
                        return await self.validate_retrieval(cid,owner,ids,expected_epoch=expected_epoch,connection=conn)
                conn=connection
                ctx=await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
                if (not ctx or ctx['rebuilding'] or ctx['authority']!='active' or ctx['model_version']!=MODEL_VERSION
                        or (expected_epoch is not None and ctx['suppression_epoch']!=expected_epoch)):
                    raise SuppressedEvidence()
                rows=await conn.fetch('''WITH RECURSIVE selected AS (
                    SELECT a.* FROM cognitive_artifacts a WHERE a.context_id=$1 AND a.id=ANY($2::bigint[])
                      AND a.owner_id IS NOT DISTINCT FROM $3 AND a.model_version=$4
                      AND a.projection_epoch=$5 AND a.suppressed_at IS NULL AND a.payload IS NOT NULL
                      AND a.kind IN ('trace','belief','belief_version','archive_record')
                ), dependencies(artifact_id,event_id) AS (
                    SELECT a.id,p.source_event_id FROM selected a JOIN cognitive_provenance p
                      ON p.context_id=a.context_id AND p.artifact_id=a.id
                    UNION SELECT prior.artifact_id,d.source_event_id FROM dependencies prior
                      JOIN cognitive_event_dependencies d ON d.context_id=$1 AND d.event_id=prior.event_id
                ) SELECT a.id,ARRAY(SELECT d.event_id FROM dependencies d WHERE d.artifact_id=a.id ORDER BY d.event_id) AS sources
                  FROM selected a WHERE EXISTS(SELECT 1 FROM dependencies d WHERE d.artifact_id=a.id)
                  AND NOT EXISTS(SELECT 1 FROM dependencies d LEFT JOIN cognitive_events e
                    ON e.context_id=$1 AND e.id=d.event_id WHERE d.artifact_id=a.id
                    AND (e.id IS NULL OR e.suppressed_at IS NOT NULL OR e.payload IS NULL
                      OR e.owner_id IS DISTINCT FROM $3))''',
                    cid,sorted(set(ids)),owner,MODEL_VERSION,ctx['suppression_epoch'])
                return sorted(row['id'] for row in rows),sorted({source for row in rows for source in row['sources']})
        except (TimeoutError,QueryCanceledError) as exc:
            raise SuppressedEvidence() from exc

    async def _reactivate(self,cid,owner,selected,at,cycle):
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
            grouped={}
            for item in selected:
                grouped.setdefault(item['row']['id'],[]).extend(item['recollection']['details'])
            for aid,details in grouped.items():
                row = await conn.fetchrow('SELECT * FROM cognitive_artifacts WHERE context_id=$1 AND id=$2 AND suppressed_at IS NULL AND owner_id IS NOT DISTINCT FROM $3 AND projection_epoch=(SELECT suppression_epoch FROM cognitive_contexts WHERE id=$1)',cid,aid,owner)
                if not row:
                    continue
                value = object_value(row['payload'])
                if value.get('last_retrieval_cycle')==cycle:
                    continue
                created = datetime.fromisoformat(value['observed_at'])
                visible = {(d['kind'],d['text']) for d in details}
                if not visible:
                    continue
                value['details'] = [reactivate(d,created,at) if (d['kind'],d['text']) in visible else d for d in value['details']]
                value['last_retrieval_cycle'] = cycle
                await conn.execute('UPDATE cognitive_artifacts SET payload=$3::jsonb,revision=revision+1 WHERE context_id=$1 AND id=$2',cid,row['id'],dump(value))

    async def replay(self,cid,eid,at,budget=4,worker_token=None):
        at = utc(at)
        async with self.pool.acquire() as conn,conn.transaction():
            context = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
            if context['rebuilding']:
                raise SuppressedEvidence()
            if worker_token is not None:
                valid = await conn.fetchval("SELECT 1 FROM cognitive_jobs WHERE context_id=$1 AND event_id=$2 AND status='running' AND lease_token=$3 AND lease_until>NOW()",cid,eid,worker_token)
                if not valid or context['worker_token']!=worker_token:
                    raise StaleRevision()
            source = await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1 AND id=$2 AND suppressed_at IS NULL',cid,eid)
            if not source:
                raise SuppressedEvidence()
            done = await conn.fetchval("SELECT 1 FROM cognitive_projection_effects WHERE context_id=$1 AND event_id=$2 AND phase='replay' AND model_version=$3",cid,eid,MODEL_VERSION)
            if done:
                return []
            rows = await conn.fetch("SELECT * FROM cognitive_artifacts WHERE context_id=$1 AND kind='trace' AND owner_id IS NOT DISTINCT FROM $2 AND suppressed_at IS NULL ORDER BY id DESC LIMIT 128",cid,source['owner_id'])
            ranked = []
            for r in rows:
                p = object_value(r['payload'])
                last = datetime.fromisoformat(p['last_replay']) if p['last_replay'] else None
                if p['replay_count']>=3 or last and (at-last).total_seconds()<86400:
                    continue
                ranked.append((p['salience']+.3/(1+p['replay_count']),r,p))
            ranked.sort(key=lambda r:(-r[0],r[1]['id']))
            selected = []
            topics = set()
            for _,row,p in ranked:
                if p['topic'] in topics:
                    continue
                topics.add(p['topic'])
                p['details'] = [reactivate(d,datetime.fromisoformat(p['observed_at']),at,replay=True) for d in p['details']]
                p['replay_count'] += 1
                p['last_replay'] = at.isoformat()
                await conn.execute('UPDATE cognitive_artifacts SET payload=$3::jsonb,revision=revision+1 WHERE context_id=$1 AND id=$2',cid,row['id'],dump(p))
                selected.append(row['id'])
                if len(selected)>=budget:
                    break
            # Offline consolidation reorganizes existing evidence. It never
            # creates another witness, increases truth confidence or social trust.
            for left_index,left in enumerate(selected):
                lp = next(p for _,r,p in ranked if r['id']==left)
                for right in selected[left_index+1:]:
                    rp = next(p for _,r,p in ranked if r['id']==right)
                    similarity = max(0.,cosine(lexical_vector(lp['gist']),lexical_vector(rp['gist'])))
                    if similarity>=.25:
                        await conn.execute("INSERT INTO cognitive_memory_links VALUES($1,$2,$3,'semantic',$4) ON CONFLICT DO NOTHING",cid,left,right,min(.7,similarity))
            if selected:
                cluster = dict(trace_ids=selected,topics=sorted(topics),source_groups=sorted({p['group'] for _,r,p in ranked if r['id'] in selected}),
                               consolidated_at=at.isoformat(),truth_confidence_gain=0.,social_evidence_gain=0.)
                await self._put(conn,cid,'replay_cluster',key('replay_cluster',source['event_key']),cluster,source['owner_id'],[eid],selected)
            await conn.execute("INSERT INTO cognitive_projection_effects(context_id,event_id,phase,model_version) VALUES($1,$2,'replay',$3)",cid,eid,MODEL_VERSION)
            return selected

    async def rebuild(self,cid,perceptions):
        """Discard projections and replay only still-permitted raw observations."""
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
            await conn.execute("UPDATE cognitive_artifacts SET payload=NULL,suppressed_at=NOW() WHERE context_id=$1 AND artifact_key IS NOT NULL",cid)
            await conn.execute('DELETE FROM cognitive_embeddings WHERE context_id=$1',cid)
            await conn.execute('DELETE FROM cognitive_memory_links WHERE context_id=$1',cid)
            await conn.execute('DELETE FROM cognitive_projection_effects WHERE context_id=$1',cid)
            await conn.execute('DELETE FROM cognitive_artifact_parents WHERE context_id=$1',cid)
            # Old flattened dependencies must not contaminate newly rebuilt views.
            await conn.execute('DELETE FROM cognitive_provenance WHERE context_id=$1 AND artifact_id IN (SELECT id FROM cognitive_artifacts WHERE context_id=$1 AND artifact_key IS NOT NULL)',cid)
        for eid,p in perceptions:
            await self.encode(cid,eid,p)
