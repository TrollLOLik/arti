"""Single authoritative event cycle shared by Telegram and isolated simulations."""
import asyncio
import contextvars
import uuid
from dataclasses import dataclass,replace,field
from datetime import datetime,timezone
from weakref import WeakValueDictionary

from cognition.affect import advance,appraise,affect,expression,available_goals
from cognition.interpreter import SelectedModelInterpreter,InterpreterFailure
from cognition.jobs import JobQueue
from cognition.memory_repository import MemoryRepository,key
from cognition.prompting import memory_for_prompt
from cognition.regulation import regulate
from cognition.reappraisal import ReappraisalRepository,explained_perception
from cognition.repositories import CognitiveRepository,ensure_schema,SuppressedEvidence,StaleRevision
from cognition.serialization import load_event,object_value,dump
from cognition.types import (ContextKey,CognitiveEvent,EvidenceRef,Origin,Perception,PERCEPTION_VERSION,DEFAULT_GOALS,MODEL_VERSION,AudienceScope)
from cognition.scope import CURRENT_SCOPE
from cognition.worker import CognitiveWorker

CURRENT_TURN = contextvars.ContextVar('arti_cognitive_turn',default=None)
_runtime = None


async def fence_context(conn,cid):
    """Invalidate in-flight work while preserving permitted projections."""
    await conn.execute('UPDATE cognitive_contexts SET suppression_epoch=suppression_epoch+1,worker_token=NULL,worker_lease_until=NULL WHERE id=$1',cid)
    await conn.execute('UPDATE cognitive_artifacts SET projection_epoch=(SELECT suppression_epoch FROM cognitive_contexts WHERE id=$1) WHERE context_id=$1 AND suppressed_at IS NULL',cid)
    await conn.execute("UPDATE cognitive_jobs SET status='pending',lease_token=NULL,lease_until=NULL,available_at=NOW() WHERE context_id=$1 AND status='running'",cid)
    await conn.execute("UPDATE cognitive_outbox SET status='cancelled',payload=NULL WHERE context_id=$1 AND status='prepared'",cid)


@dataclass
class PreparedTurn:
    runtime: object
    context_id: int
    event_id: int
    event: CognitiveEvent
    expression: object
    memory: str
    epoch: int
    authority: str
    repeated_delivery: bool = False
    send_ordinal: int = 0
    delivery_blocked: bool = False
    preferences: dict = field(default_factory=dict)

    @property
    def active(self):
        return self.authority=='active'

    @property
    def uses_cognition(self):
        return self.active or (self.runtime.mode!='legacy' and self.event.audience.kind in ('group','topic'))

    @property
    def tracks_delivery(self):
        return self.active or (self.authority=='shadow' and self.runtime.mode!='legacy'
                               and self.event.audience.kind in ('group','topic') and self.event.context.topic_id>=0)


class CognitiveRuntime:
    def __init__(self,pool,interpreter,mode='active',clock=None,*,strict=False,semantic=None,prepare_budget=3.):
        if mode not in ('shadow','active','legacy'):
            raise ValueError('Invalid cognitive authority')
        self.pool,self.interpreter,self.mode = pool,interpreter,mode
        self.strict = strict
        if strict and mode != 'active':
            raise ValueError('Production cognition is always active')
        self.clock = clock or (lambda:datetime.now(timezone.utc))
        self.repo = CognitiveRepository(pool)
        self.semantic = semantic
        self.prepare_budget = prepare_budget
        self.foreground = set()
        self.memory = MemoryRepository(pool,semantic)
        self.reappraisal = ReappraisalRepository(pool)
        self.jobs = JobQueue(pool)
        self.worker = CognitiveWorker(self.jobs,self.handle_job)
        self.locks = WeakValueDictionary()
        from cognition.proactivity import GroupService
        self.groups = GroupService(self)

    async def initialize(self,start_worker=True):
        await ensure_schema(self.pool)
        # A crash between send and receipt must never trigger an automatic resend.
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE cognitive_outbox SET status='delivery_unknown' WHERE status='sending' AND updated_at<NOW()-INTERVAL '10 minutes'")
        if self.strict:
            await self.activate_current_contexts()
        if start_worker and self.mode!='legacy':
            self.worker.start()
            if self.semantic:
                self.semantic.start()
        return self

    async def activate_current_contexts(self):
        """Promote current contexts, preserving retired/unresolved RP scenes."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''SELECT c.id FROM cognitive_contexts c
                WHERE c.authority!='active' AND (c.mode='default' OR EXISTS(
                    SELECT 1 FROM cognitive_scenes s WHERE s.chat_id=c.chat_id
                    AND s.topic_id=c.topic_id AND s.scene_id=c.scene_id)) ORDER BY c.id''')
        for row in rows:
            await self.set_authority(row['id'], 'active')
        return len(rows)

    async def ensure_context(self, context):
        """An empty diagnostic context is storage, never invented evidence."""
        from cognition.affect import initial_state
        async with self.pool.acquire() as conn,conn.transaction():
            if self.strict and context.mode == 'rp':
                await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',context.chat_id)
                scene = await conn.fetchval('SELECT scene_id FROM cognitive_scenes WHERE chat_id=$1 AND topic_id=$2',context.chat_id,context.topic_id)
                if scene != context.scene_id:
                    raise SuppressedEvidence()
            cid = await conn.fetchval('''INSERT INTO cognitive_contexts
                (persona_id,chat_id,mode,scene_id,topic_id,model_version,state,authority)
                VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8)
                ON CONFLICT(persona_id,chat_id,mode,scene_id,topic_id) DO NOTHING RETURNING id''',
                *context.identity(),MODEL_VERSION,dump(initial_state(context,self.clock())),self.mode)
            if cid is None:
                cid = await conn.fetchval('''SELECT id FROM cognitive_contexts
                    WHERE persona_id=$1 AND chat_id=$2 AND mode=$3 AND scene_id=$4 AND topic_id=$5''',*context.identity())
        if self.strict:
            async with self.pool.acquire() as conn:
                authority = await conn.fetchval('SELECT authority FROM cognitive_contexts WHERE id=$1',cid)
            if authority != 'active':
                await self.set_authority(cid,'active')
        return cid

    async def reset_history(self,chat_id,mode='default'):
        context = await self.context(chat_id,mode)
        cid = await self.ensure_context(context)
        async with self.pool.acquire() as conn,conn.transaction():
            await self._validate_current_scene(conn,cid)
            await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
            await conn.execute('''UPDATE cognitive_contexts SET history_after_event_id=
                COALESCE((SELECT MAX(id) FROM cognitive_events WHERE context_id=$1),0) WHERE id=$1''',cid)
            await fence_context(conn,cid)
            await conn.execute("UPDATE group_candidates SET status='cancelled',payload=NULL WHERE context_id=$1 AND status IN ('pending','deferred','claimed')",cid)
        from cognition.history import invalidate_history
        invalidate_history(chat_id)
        return cid

    async def _validate_current_scene(self,conn,cid):
        if not self.strict:
            return
        context = await conn.fetchrow('SELECT chat_id,mode,scene_id,topic_id FROM cognitive_contexts WHERE id=$1',cid)
        if context is None:
            raise ValueError('Unknown cognitive context')
        if context['mode'] == 'rp':
            await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',context['chat_id'])
            scene = await conn.fetchval('SELECT scene_id FROM cognitive_scenes WHERE chat_id=$1 AND topic_id=$2',context['chat_id'],context['topic_id'])
            if scene != context['scene_id']:
                raise SuppressedEvidence()

    async def close(self):
        for task in self.foreground:
            task.cancel()
        await asyncio.gather(*self.foreground,return_exceptions=True)
        await self.worker.stop()
        if self.semantic:
            await self.semantic.close()
        await self.groups.close()
        close = getattr(self.interpreter,'close',None)
        if close:
            await close()

    async def context(self,chat_id,mode='default',topic_id=None):
        scope = CURRENT_SCOPE.get()
        topic_id = topic_id if topic_id is not None else scope.topic_id if scope and scope.chat_id==chat_id else -1
        scene = ''
        if mode=='rp':
            async with self.pool.acquire() as conn:
                scene = await conn.fetchval('''INSERT INTO cognitive_scenes(chat_id,scene_id,topic_id) VALUES($1,$2,$3)
                    ON CONFLICT(chat_id,topic_id) DO UPDATE SET scene_id=cognitive_scenes.scene_id RETURNING scene_id''',chat_id,uuid.uuid4().hex,topic_id)
        return ContextKey('arti',chat_id,mode,scene,topic_id)

    async def new_scene(self,chat_id):
        topic = (await self.context(chat_id)).topic_id
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',chat_id)
            await conn.execute("UPDATE cognitive_contexts SET suppression_epoch=suppression_epoch+1,authority='shadow' WHERE chat_id=$1 AND topic_id=$2 AND mode='rp'",chat_id,topic)
            await conn.execute("UPDATE cognitive_outbox SET status='cancelled',payload=NULL WHERE context_id IN (SELECT id FROM cognitive_contexts WHERE chat_id=$1 AND topic_id=$2 AND mode='rp') AND status='prepared'",chat_id,topic)
            return await conn.fetchval('''INSERT INTO cognitive_scenes(chat_id,scene_id,topic_id) VALUES($1,$2,$3)
                ON CONFLICT(chat_id,topic_id) DO UPDATE SET scene_id=EXCLUDED.scene_id,updated_at=NOW() RETURNING scene_id''',chat_id,uuid.uuid4().hex,topic)

    async def ingest(self,chat_id,owner,text,message_id,mode='default',occurred_at=None,origin=Origin.USER,context=None,event_kind='utterance',audience=None,addressed_to_arti=None,reply_to_id=None):
        context = context or await self.context(chat_id,mode)
        if self.strict and context != await self.context(chat_id,mode,context.topic_id):
            raise SuppressedEvidence()
        if self.strict:
            await self.ensure_context(context)
        source = f'telegram:{chat_id}:{message_id}:{origin.value}'
        async with self.pool.acquire() as conn:
            existing = await conn.fetchrow('''SELECT e.* FROM cognitive_events e JOIN cognitive_contexts c ON c.id=e.context_id
                WHERE c.persona_id=$1 AND c.chat_id=$2 AND c.mode=$3 AND c.scene_id=$4 AND c.topic_id=$5 AND e.event_key=$6''',*context.identity(),source)
        if existing:
            if existing['suppressed_at'] is not None:
                raise SuppressedEvidence()
            if existing['owner_id']!=owner:
                raise ValueError('Transport source cannot change owner')
            return existing['context_id'],existing['id'],load_event(existing['payload'])
        at = self.clock()
        occurred_at = occurred_at or at
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        occurred_at = min(occurred_at,at)
        scope = CURRENT_SCOPE.get()
        scoped = scope is not None and scope.chat_id==chat_id
        audience = audience or (AudienceScope('topic' if scope.topic_id>0 else 'group' if scope.group and scope.topic_id==0 else 'private' if scope.chat_type=='private' else 'unknown',chat_id,scope.topic_id) if scoped else AudienceScope())
        addressed_to_arti = addressed_to_arti if addressed_to_arti is not None else scope.addressed if scoped else True
        reply_to_id = reply_to_id if reply_to_id is not None else scope.reply_to_id if scoped else None
        evidence = EvidenceRef(source,source,origin,owner)
        event = CognitiveEvent(source,context,evidence,occurred_at,at,str(text or ''),owner if origin==Origin.USER else None,owner if origin==Origin.DELIVERED_ACTION else None,event_kind,audience,addressed_to_arti,reply_to_id)
        cid,eid = await self.repo.observe(event)
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_contexts SET authority=$2 WHERE id=$1 AND revision=0 AND NOT authority_explicit',cid,self.mode)
        await self.jobs.enqueue(cid,eid,'interpret' if origin==Origin.USER else 'encode')
        return cid,eid,event

    async def handle_job(self,job):
        cid,eid = job['context_id'],job['event_id']
        if job['kind']=='rebuild':
            from cognition.forgetting import recover_rebuild
            return await recover_rebuild(self.pool,cid)
        if job['kind']=='replay':
            return await self.memory.replay(cid,eid,self.clock(),worker_token=job['lease_token'])
        return await self.process(cid,eid,worker_token=job['lease_token'])

    async def process(self,cid,eid,worker_token=None):
        if worker_token is None:
            # Foreground and background workers use the same PostgreSQL lease;
            # process-local locks alone cannot serialize two bot processes.
            deadline = asyncio.get_running_loop().time()+35
            while asyncio.get_running_loop().time()<deadline:
                async with self.pool.acquire() as conn:
                    target = await conn.fetchrow('SELECT independent_group,suppressed_at FROM cognitive_events WHERE context_id=$1 AND id=$2',cid,eid)
                if not target or target['suppressed_at'] is not None:
                    raise SuppressedEvidence()
                if await self.repo.applied(cid,target['independent_group']):
                    return await self.repo.state(cid)
                job = await self.jobs.claim(context_id=cid)
                if not job:
                    async with self.pool.acquire() as conn:
                        dead = await conn.fetchval("SELECT 1 FROM cognitive_jobs WHERE context_id=$1 AND event_id=$2 AND kind IN ('interpret','encode') AND status='dead'",cid,eid)
                    if dead:
                        break
                    await asyncio.sleep(.2)
                    continue
                renewal = asyncio.create_task(self.worker._renew(job))
                work = asyncio.create_task(self.handle_job(job))
                try:
                    done,_ = await asyncio.wait((renewal,work),return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                    if work not in done:
                        raise StaleRevision()
                    await self.jobs.finish(job['id'],job['lease_token'])
                except InterpreterFailure as exc:
                    code = exc.code if exc.code in ('timeout','provider_unavailable','invalid_perception','output_truncated') else 'provider_unavailable'
                    await self.jobs.fail(job['id'],job['lease_token'],code)
                    raise
                except BaseException:
                    await self.jobs.fail(job['id'],job['lease_token'],'internal_error')
                    raise
                finally:
                    renewal.cancel()
                    work.cancel()
                    await asyncio.gather(renewal,work,return_exceptions=True)
            return await self.repo.state(cid)
        lock = self.locks.setdefault(cid,asyncio.Lock())
        async with lock:
            async with self.pool.acquire() as conn:
                source = await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1 AND id=$2 AND suppressed_at IS NULL',cid,eid)
                context = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1',cid)
            if not source:
                raise SuppressedEvidence()
            event = load_event(source['payload'])
            before = await self.repo.state(cid)
            if context['rebuilding']:
                raise SuppressedEvidence()
            async with self.pool.acquire() as conn:
                goals = await self.repo.goal_registry(conn,cid,event,before)
            late = event.observed_at<before.last_at
            if source['perception']:
                p = Perception.from_dict(object_value(source['perception']))
            elif event.evidence.origin in (Origin.USER,Origin.DELIVERED_ACTION) and event.event_kind!='historical' and event.addressed_to_arti:
                # Preliminary retrieval does not strengthen traces or supply a new
                # external evidence group. The current event is not yet encoded.
                raw = await self.memory.candidates(cid,event.evidence.owner_id,event.text,limit=32)
                words = __import__('cognition.memory_dynamics',fromlist=['tokens']).tokens(event.text)
                raw.sort(key=lambda r:-(.55*len(words & __import__('cognition.memory_dynamics',fromlist=['tokens']).tokens(r['payload']['gist']))/max(1,len(words))+.45*r.get('semantic_score',0.)))
                memories = [dict(source_id=r['payload']['source_id'],text=r['payload']['gist'][:400],interpretation=r['payload']['interpretation']) for r in raw[:8]]
                pending = await self.memory.open_intentions(cid,event.evidence.owner_id,event.text)
                intentions = [{k:r['payload'].get(k) for k in ('key','description','actor_id','deadline','status','source_id')} for r in pending]
                from cognition.sensory import acoustic_context
                sensory=await acoustic_context(self.pool,event)
                interpreted = await self.interpreter.interpret(event,goals=DEFAULT_GOALS if late else goals,memories=memories,intentions=intentions,rich=True,**({'sensory':sensory} if sensory else {}))
                p = interpreted.perception
                if sensory and event.text.startswith('Материал: '):
                    # A decoder observation supplies no goal outcome or human intent.
                    p=replace(p,appraisals=())
                if event.evidence.origin==Origin.DELIVERED_ACTION:
                    if p.situation:
                        p = replace(p,appraisals=(),situation=replace(p.situation,beliefs=(),preferences={},social_signal='contact',revisions=()))
                    else:
                        p = replace(p,appraisals=())
                # The dependency epoch is checked by the state commit. Store the
                # exact eligible raw-source registry for later invalidation.
                async with self.pool.acquire() as conn,conn.transaction():
                    current = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
                    if current['suppression_epoch']!=context['suppression_epoch']:
                        raise SuppressedEvidence()
                    for memory in raw[:8]:
                        sid = memory['payload']['event_id']
                        await conn.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3) ON CONFLICT DO NOTHING',cid,eid,sid)
                    goal_groups = [g.evidence_group for g in goals if g.evidence_group]
                    if goal_groups:
                        await conn.execute('''INSERT INTO cognitive_event_dependencies(context_id,event_id,source_event_id)
                            SELECT $1,$2,id FROM cognitive_events WHERE context_id=$1 AND independent_group=ANY($3::text[])
                            AND suppressed_at IS NULL ON CONFLICT DO NOTHING''',cid,eid,goal_groups)
                    if pending:
                        await conn.execute('''INSERT INTO cognitive_event_dependencies(context_id,event_id,source_event_id)
                            SELECT $1,$2,p.source_event_id FROM cognitive_provenance p
                            JOIN cognitive_events e ON e.id=p.source_event_id
                            WHERE p.context_id=$1 AND p.artifact_id=ANY($3::bigint[]) AND e.suppressed_at IS NULL
                            ON CONFLICT DO NOTHING''',cid,eid,[r['id'] for r in pending])
            else:
                p = Perception(event.event_id,PERCEPTION_VERSION,())
            if not await self.repo.applied(cid,event.evidence.independent_group):
                if event.observed_at<before.last_at:
                    await self.repo.commit_late(cid,eid,p,worker_token=worker_token,expected_epoch=context['suppression_epoch'])
                else:
                    result = appraise(before,event,p,goals=goals)
                    await self.repo.commit(cid,eid,p,before.revision,result,worker_token=worker_token,expected_epoch=context['suppression_epoch'])
            after = await self.repo.state(cid)
            if late:
                from cognition.forgetting import rebuild_allowed
                await rebuild_allowed(self.pool,cid)
            else:
                await self.memory.encode(cid,eid,p,before,after,worker_token=worker_token,expected_epoch=context['suppression_epoch'])
            if p.situation:
                if event.audience.kind in ('group','topic') and 'proactive' in p.situation.preferences:
                    await self.groups.policies.opt_out(event.context.chat_id,event.evidence.owner_id,p.situation.preferences['proactive'] is False)
                for revision in p.situation.revisions:
                    async with self.pool.acquire() as conn:
                        cause = await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1 AND source_id=$2 AND suppressed_at IS NULL',cid,revision['source_id'])
                    if cause and cause['perception']:
                        original = Perception.from_dict(object_value(cause['perception']))
                        revised = explained_perception(original,p)
                        if cause['origin']=='user':
                            await self.reappraisal.revise(cid,revision['source_id'],eid,revised,expected_epoch=context['suppression_epoch'],worker_token=worker_token)
                        await self.reappraisal.reinterpret_trace(cid,revision['source_id'],eid,revision['interpretation'],revision['confidence'],expected_epoch=context['suppression_epoch'],worker_token=worker_token)
                        if cause['origin']=='delivered_action':
                            async with self.pool.acquire() as conn,conn.transaction():
                                await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
                                await self.memory._put(conn,cid,'own_action_review',key('own_action_review',cause['id'],eid),
                                    dict(action_source=revision['source_id'],explanation=revision['interpretation'],confidence=revision['confidence'],status='reported_review'),
                                    event.evidence.owner_id,[cause['id'],eid])
            # Durable replay is low priority through availability and chronological
            # ordering. One job per source, capped processing per source group.
            if event.evidence.origin==Origin.USER and event.addressed_to_arti:
                jid = await self.jobs.enqueue(cid,eid,'replay')
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE cognitive_jobs SET available_at=GREATEST(available_at,NOW()+INTERVAL '1 day') WHERE id=$1 AND status='pending'",jid)
            return after

    async def prepare(self,chat_id,owner,text,message_id,mode='default',task_serious=False,source_context=None):
        if source_context is not None and source_context!=await self.context(chat_id,mode):
            raise SuppressedEvidence()
        cid,eid,event = await self.ingest(chat_id,owner,text,message_id,mode,context=source_context)
        # The input may have been registered before debounce. Process every
        # earlier observation before preparing the latest response.
        async with self.pool.acquire() as conn:
            rows = [] if self.strict and self.worker.task is not None and not self.worker.task.done() else await conn.fetch('''SELECT e.id FROM cognitive_events e WHERE context_id=$1 AND id<=$2 AND suppressed_at IS NULL AND origin IN ('user','delivered_action')
                AND NOT EXISTS(SELECT 1 FROM cognitive_effects f WHERE f.context_id=e.context_id AND f.event_id=e.id AND f.model_version=$3)
                ORDER BY observed_at,id''',cid,eid,__import__('cognition.types',fromlist=['MODEL_VERSION']).MODEL_VERSION)
        async def catch_up():
            if self.strict and self.worker.task is not None and not self.worker.task.done():
                deadline = asyncio.get_running_loop().time()+self.prepare_budget
                while asyncio.get_running_loop().time()<deadline:
                    async with self.pool.acquire() as conn:
                        ready = await conn.fetchval("SELECT 1 FROM cognitive_projection_effects WHERE event_id=$1 AND phase='encode' AND model_version=$2",eid,MODEL_VERSION)
                    if ready:
                        return
                    await asyncio.sleep(.05)
                return
            # Foreground processing shares the local serialization lock. A
            # production worker will reuse its already committed perception.
            for r in rows:
                try:
                    await self.process(cid,r['id'])
                except InterpreterFailure:
                    # A provider failure supplies no user emotion or legacy delta.
                    break
        if self.strict:
            # The observer remains durable and continues after the reply budget.
            # Shielding avoids cancelling paid interpretation on every slow turn.
            task = asyncio.create_task(catch_up(),name='cognitive-catch-up')
            self.foreground.add(task)
            def finished(t):
                self.foreground.discard(t)
                if not t.cancelled():
                    t.exception()
            task.add_done_callback(finished)
            try:
                await asyncio.wait_for(asyncio.shield(task),self.prepare_budget)
            except asyncio.TimeoutError:
                pass
        else:
            await catch_up()
        async with self.pool.acquire() as conn:
            if await conn.fetchval('SELECT rebuilding FROM cognitive_contexts WHERE id=$1',cid):
                raise SuppressedEvidence()
        state = await self.personal_state(cid,owner)
        relationship = await self.memory.relationship(cid,owner)
        async with self.pool.acquire() as conn:
            source = await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1 AND id=$2',cid,eid)
            ctx = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1',cid)
            delivered = await conn.fetchval("SELECT 1 FROM cognitive_outbox WHERE context_id=$1 AND event_id=$2 AND status IN ('delivered','delivery_unknown','sending')",cid,eid)
        if source['suppressed_at'] is not None:
            raise SuppressedEvidence()
        p = Perception.from_dict(object_value(source['perception'])) if source['perception'] else Perception(event.event_id,PERCEPTION_VERSION,())
        from ai.intents import channel_restrictions
        preferences = {**relationship['preferences'],**channel_restrictions(text)}
        decision,plan = regulate(state,p.situation,preferences,task_serious)
        if not source['perception']:
            # Do not express an earlier mood as a response to an unassessed event.
            plan = replace(plan,sticker_mood=None,playfulness=0.,disclosure=0.,tone='calm and attentive',tts_style='neutral',cause_ids=(),behaviors=(),mixed_affect=False)
        from cognition.relationships import relationship_view
        view = relationship_view(relationship,self.clock())
        cue = p.situation.topic.casefold().strip() if p.situation else ''
        association = relationship.get('associations',{}).get(cue,{})
        implicit = max(-.06,min(.06,.06*association.get('strength',0.)))
        plan = replace(plan,warmth=min(1.,max(0.,plan.warmth+.2*(view['dimensions']['warmth']['value']-.5))),
                       disclosure=min(.35,plan.disclosure*view['dimensions']['openness']['value']*2))
        plan = replace(plan,warmth=min(1.,max(0.,plan.warmth+implicit)),
                       directness=min(1.,max(0.,plan.directness-implicit/2)))
        memories = await self.memory.retrieve(cid,owner,text,self.clock(),event.event_id,mood=affect(state)['mood_valence'])
        beliefs = await self.memory.artifacts(cid,owner,'belief',limit=16)
        memory,ids = memory_for_prompt(memories,beliefs)
        # Inclusion is recorded later by the final prompt assembler.
        async with self.pool.acquire() as conn,conn.transaction():
            await self._validate_current_scene(conn,cid)
            await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
            await self.memory._put(conn,cid,'regulation',key('regulation',event.event_id),
                                   dict(strategy=decision.strategy,expected_outcome=decision.expected_outcome,pending=decision.pending,
                                        implicit_cue=cue,implicit_expression_bias=implicit),owner,[eid])
        turn = PreparedTurn(self,cid,eid,event,plan,memory,ctx['suppression_epoch'],ctx['authority'],bool(delivered))
        turn.preferences = preferences
        if event.audience.kind in ('group','topic'):
            turn.memory = ''
            turn.expression = replace(turn.expression,disclosure=0.)
        CURRENT_TURN.set(turn)
        return turn

    async def personal_state(self,cid,owner):
        # Group members share a transport, not each other's private emotional
        # causes. Slow dynamics are projected from this owner's allowed ledger.
        from cognition.affect import initial_state
        from cognition.serialization import load_state
        async with self.pool.acquire() as conn:
            _,cached = await self.memory._get(conn,cid,key('personal_state',owner))
        if cached:
            state = load_state(cached['state'])
            return advance(state,max(state.last_at,self.clock()))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''SELECT e.*, (SELECT r.perception FROM cognitive_reappraisals r
                JOIN cognitive_events s ON s.id=r.support_event_id WHERE r.context_id=e.context_id AND r.cause_event_id=e.id
                AND r.perception IS NOT NULL AND s.suppressed_at IS NULL ORDER BY r.created_at DESC,r.support_event_id DESC LIMIT 1) AS revised
                FROM cognitive_events e JOIN cognitive_effects f ON f.context_id=e.context_id AND f.event_id=e.id
                WHERE e.context_id=$1 AND e.owner_id IS NOT DISTINCT FROM $2 AND e.suppressed_at IS NULL AND f.model_version=$3
                ORDER BY e.observed_at,e.id''',cid,owner,__import__('cognition.types',fromlist=['MODEL_VERSION']).MODEL_VERSION)
        context = (await self.repo.state(cid)).context
        state = initial_state(context,rows[0]['observed_at'] if rows else self.clock())
        for row in rows:
            state = appraise(state,load_event(row['payload']),Perception.from_dict(object_value(row['revised'] or row['perception'])))
            state = replace(state,applied_groups=frozenset())
        return replace(advance(state,max(state.last_at,self.clock())),applied_groups=frozenset())

    async def mark_included(self,turn,artifact_ids):
        await self.memory.record_retrieval(turn.context_id,turn.event.evidence.owner_id,turn.event.event_id,'included',sorted(artifact_ids),self.clock())

    async def set_authority(self,cid,authority):
        if self.strict and authority != 'active':
            raise ValueError('Production cognition cannot return to legacy/shadow')
        if authority not in ('active','shadow','legacy'):
            raise ValueError('Invalid authority')
        async with self.pool.acquire() as conn,conn.transaction():
            await self._validate_current_scene(conn,cid)
            await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
            changed = await conn.fetchval('SELECT authority!=$2 FROM cognitive_contexts WHERE id=$1',cid,authority)
            await conn.execute('UPDATE cognitive_contexts SET authority=$2,authority_explicit=TRUE WHERE id=$1',cid,authority)
            if changed:
                await fence_context(conn,cid)
            # Re-enable the prior projection by authority alone; no mixing or
            # reverse import of new numerical states into the old core.
            if authority!='active':
                await conn.execute("UPDATE cognitive_outbox SET status='cancelled',payload=NULL WHERE context_id=$1 AND status='prepared'",cid)


async def start_runtime(pool,mode=None,interpreter=None):
    global _runtime
    if mode not in (None,'active'):
        raise ValueError('Production cognition is always active')
    from cognition.semantic import SemanticIndex
    semantic = SemanticIndex(pool) if interpreter is None else None
    interpreter = interpreter or SelectedModelInterpreter()
    _runtime = await CognitiveRuntime(pool,interpreter,'active',strict=True,semantic=semantic).initialize()
    return _runtime


def get_runtime():
    return _runtime


async def stop_runtime():
    global _runtime
    if _runtime:
        await _runtime.close()
        _runtime = None


async def prepare_turn(chat_id,user_id,text,message_id,mode='default',task_serious=False,source_context=None):
    runtime = get_runtime()
    scope=CURRENT_SCOPE.get()
    if runtime and runtime.mode!='legacy' and scope and scope.group and scope.sender_kind=='chat' and message_id is not None:
        cid=await runtime.groups.context_id(scope,mode)
        async with runtime.pool.acquire() as conn:
            source=await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1 AND event_key=$2 AND suppressed_at IS NULL',cid,f'telegram:{chat_id}:{message_id}:system')
            ctx=await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1',cid)
        if source and ctx:
            event=load_event(source['payload'])
            style=replace(expression(await runtime.personal_state(cid,None)),disclosure=0.)
            turn=PreparedTurn(runtime,cid,source['id'],event,style,'',ctx['suppression_epoch'],ctx['authority'])
            CURRENT_TURN.set(turn)
            return turn
    if not runtime or runtime.mode=='legacy' or user_id is None or message_id is None:
        CURRENT_TURN.set(None)
        raise SuppressedEvidence('A cognitive response requires an available runtime and authored source')
    return await runtime.prepare(chat_id,user_id,text,message_id,mode,task_serious,source_context)
