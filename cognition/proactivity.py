"""Cause-based initiatives with bounded public evidence and durable arbitration."""
import asyncio
import hashlib
import uuid
from dataclasses import replace,asdict
from datetime import datetime,timedelta
from types import SimpleNamespace

from cognition.group_policy import PolicyRepository
from cognition.group_context import build_frame,candidate_kind,contextual_candidate,explicit_refusal
from cognition.scope import CURRENT_SCOPE,TransportScope
from cognition.serialization import dump,object_value,load_event
from cognition.types import AudienceScope,Origin


ASSESS_TIMEOUT_SECONDS = 8.
COMPOSE_TIMEOUT_SECONDS = 12.
CONTINUATION_TIMEOUT_SECONDS = 1.
DIRECT_LEASE_TIMEOUT_SECONDS = 1.
MEMBERSHIP_TIMEOUT_SECONDS = 3.
SEMANTIC_RETRIES = 1
MAX_OUTCOME_DEPENDENCIES = 128
MAX_OUTCOME_LINEAGE = 256


class GroupService:
    def __init__(self,runtime,judge=None):
        self.runtime=runtime; self.pool=runtime.pool; self.policies=PolicyRepository(self.pool)
        if judge is None:
            from ai.group_participation import SelectedModelGroupJudge
            judge=SelectedModelGroupJudge()
        self.judge=judge

    async def close(self):
        close=getattr(self.judge,'close',None)
        if close: await close()

    async def context_id(self,scope,mode='default'):
        ctx=await self.runtime.context(scope.chat_id,mode,scope.topic_id)
        async with self.pool.acquire() as conn:
            return await conn.fetchval('SELECT id FROM cognitive_contexts WHERE persona_id=$1 AND chat_id=$2 AND mode=$3 AND scene_id=$4 AND topic_id=$5',*ctx.identity())

    async def frame(self,cid):
        now=self.runtime.clock()
        # A revision must describe exactly the observations given to the model.
        # READ COMMITTED could stamp old rows with a newer observation revision.
        async with self.pool.acquire() as conn,conn.transaction(isolation='repeatable_read',readonly=True):
            ctx=await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1',cid)
            policy,_=await self.policies.get(ctx['chat_id'],ctx['topic_id'],conn)
            rows=await conn.fetch('''SELECT o.*,e.source_id FROM group_observations o JOIN cognitive_events e ON e.id=o.event_id
                WHERE o.context_id=$1 AND o.suppressed_at IS NULL AND e.suppressed_at IS NULL AND o.payload IS NOT NULL
                AND e.context_id=$1 AND e.payload->'audience'->>'kind' IN ('group','topic')
                AND (e.payload->'audience'->>'chat_id')::bigint=$4 AND (e.payload->'audience'->>'topic_id')::bigint=$5
                AND o.event_id>$3 AND o.observed_at>=$2 ORDER BY o.observed_at DESC,o.id DESC LIMIT 64''',
                cid,now-timedelta(days=policy.retention_days),ctx['history_after_event_id'],ctx['chat_id'],ctx['topic_id'])
            state=await conn.fetchrow('SELECT * FROM group_topic_runtime WHERE context_id=$1',cid)
            feedback=await conn.fetch('''SELECT f.*,g.kind FROM group_feedback f
                JOIN cognitive_outbox o ON o.context_id=f.context_id AND o.receipt_id=f.message_id AND o.status='delivered'
                JOIN group_candidates g ON g.outbox_id=o.id WHERE f.context_id=$1 AND f.created_at>$2
                AND NOT EXISTS(SELECT 1 FROM cognitive_events e WHERE e.id=ANY(f.source_ids) AND e.suppressed_at IS NOT NULL)
                ORDER BY f.created_at DESC LIMIT 60''',cid,now-timedelta(days=7))
            contributions=await conn.fetch('''SELECT g.kind,o.receipt_id,o.channel,o.payload,o.status,o.created_at,
                ARRAY(SELECT e.source_id FROM cognitive_events e WHERE e.id=ANY(g.source_ids)) AS public_sources
                FROM group_candidates g JOIN cognitive_outbox o ON o.id=g.outbox_id
                WHERE g.context_id=$1 AND o.context_id=$1 AND o.status IN ('delivered','delivery_unknown') AND o.payload IS NOT NULL
                AND cardinality(g.source_ids)<=$7
                AND o.created_at>=$2 AND cardinality(g.source_ids)=(SELECT count(*) FROM cognitive_events e
                    WHERE e.id=ANY(g.source_ids) AND e.context_id=$1 AND e.id>$3 AND e.suppressed_at IS NULL
                    AND e.observed_at>=$6
                    AND e.payload->'audience'->>'kind' IN ('group','topic')
                    AND (e.payload->'audience'->>'chat_id')::bigint=$4 AND (e.payload->'audience'->>'topic_id')::bigint=$5)
                ORDER BY o.created_at DESC LIMIT 8''',cid,now-timedelta(days=min(7,policy.retention_days)),ctx['history_after_event_id'],ctx['chat_id'],ctx['topic_id'],now-timedelta(days=policy.retention_days),MAX_OUTCOME_DEPENDENCIES)
            outcomes=[]; lineage=set()
            for contribution in contributions:
                # Omit an over-budget derived snippet in its entirety. Retaining
                # text while trimming its hidden support would break erasure.
                support=set(contribution['public_sources'])
                if len(lineage|support)>MAX_OUTCOME_LINEAGE: continue
                lineage.update(support)
                signals=await conn.fetch('''SELECT e.source_id,f.user_id AS owner_id,f.signal FROM group_feedback f
                    JOIN cognitive_events e ON e.id=ANY(f.source_ids) AND e.context_id=f.context_id
                    WHERE f.context_id=$1 AND f.message_id=$2 AND e.suppressed_at IS NULL AND e.id>$3
                    AND e.payload->>'event_kind'='reaction' AND e.payload->'audience'->>'kind' IN ('group','topic')
                    AND (e.payload->'audience'->>'chat_id')::bigint=$4 AND (e.payload->'audience'->>'topic_id')::bigint=$5
                    AND NOT EXISTS(SELECT 1 FROM cognitive_events s WHERE s.id=ANY(f.source_ids) AND s.suppressed_at IS NOT NULL)
                    ORDER BY f.created_at DESC LIMIT 8''',cid,contribution['receipt_id'],ctx['history_after_event_id'],ctx['chat_id'],ctx['topic_id'])
                outcomes.append(dict(message_id=contribution['receipt_id'],source_ids=contribution['public_sources'],kind=contribution['kind'],
                    channel=contribution['channel'],text=object_value(contribution['payload']).get('text','')[:600],
                    delivery_status=contribution['status'],at=contribution['created_at'].isoformat(),feedback=[dict(s) for s in signals]))
        messages=[dict(**object_value(r['payload']),source_id=r['source_id'],event_id=r['event_id'],at=r['observed_at'].isoformat()) for r in reversed(rows)]
        return build_frame(cid,ctx['chat_id'],ctx['topic_id'],messages,state['revision'] if state else 0,state['closed'] if state else False,
                           feedback,outcomes=outcomes,suppression_epoch=ctx['suppression_epoch'])

    async def observe(self,scope,text,mode='default',at=None,message=None,edited=False,is_bot=False):
        if not scope.group or scope.topic_id<0 or scope.sender_kind=='bot' and not is_bot: return None
        audience=AudienceScope('topic' if scope.topic_id>0 else 'group',scope.chat_id,scope.topic_id)
        identity=str(scope.message_id)+(f':edit:{at.isoformat()}' if edited else '')
        cid,eid,event=await self.runtime.ingest(scope.chat_id,scope.user_id,text,identity,mode,occurred_at=at,
                context=await self.runtime.context(scope.chat_id,mode,scope.topic_id),
                origin=Origin.DELIVERED_ACTION if is_bot else Origin.USER if scope.user_id is not None else Origin.SYSTEM,
                audience=audience,addressed_to_arti=scope.addressed,reply_to_id=scope.reply_to_id)
        entities=getattr(message,'entities',None) or getattr(message,'caption_entities',None) or []
        elsewhere=bool(scope.reply_to_id and not scope.addressed) or bool(entities and not scope.addressed and any(getattr(e,'type','') in ('mention','text_mention') for e in entities))
        payload=dict(message_id=scope.message_id,owner_id=scope.user_id,text=str(text)[:2500],reply_to_id=scope.reply_to_id,
                     sender_kind='bot' if is_bot else scope.sender_kind,sender_ref=scope.sender_ref,directed=scope.addressed,is_bot=is_bot,addressed_elsewhere=elsewhere)
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
            old=await conn.fetchrow('SELECT event_id,payload FROM group_observations WHERE context_id=$1 AND message_id=$2',cid,scope.message_id)
            if old and old['event_id']==eid: return cid
            if edited and old:
                await conn.execute("UPDATE group_candidates SET status='cancelled',payload=NULL WHERE context_id=$1 AND $2=ANY(source_ids) AND status IN ('pending','deferred','claimed')",cid,old['event_id'])
            await conn.execute('''INSERT INTO group_observations(context_id,event_id,message_id,owner_id,payload,observed_at)
                VALUES($1,$2,$3,$4,$5::jsonb,$6) ON CONFLICT(context_id,message_id) DO UPDATE
                SET event_id=EXCLUDED.event_id,payload=EXCLUDED.payload,observed_at=EXCLUDED.observed_at,edited_at=EXCLUDED.observed_at''',
                cid,eid,scope.message_id,scope.user_id,dump(payload),event.observed_at)
            await conn.execute('''INSERT INTO group_topic_runtime(context_id,revision) VALUES($1,1)
                ON CONFLICT(context_id) DO UPDATE SET revision=group_topic_runtime.revision+1,updated_at=NOW()''',cid)
        if not is_bot and not scope.addressed:
            await self.propose(cid,eid,{**payload,'edited':edited})
        if not is_bot and scope.reply_to_id:
            import re
            signal=.7 if re.match(r'(?i)^\s*(спасибо|благодарю|thanks)\b',str(text)) else -.7 if re.match(r'(?i)^\s*(не вмешивайся|не надо вмешиваться|stop interrupting)\b',str(text)) else None
            if signal is not None: await self.feedback(scope,scope.reply_to_id,scope.user_id,signal)
        return cid

    async def propose(self,cid,eid,message):
        frame=await self.frame(cid)
        policy,revision=await self.policies.get(frame.chat_id,frame.topic_id)
        if policy.mode=='mentions' or not policy.full_visibility or message['sender_kind']=='bot': return
        if await self.policies.opted_out(frame.chat_id,message['owner_id']): return
        kind=candidate_kind(frame,message,policy)
        if not kind:
            if not contextual_candidate(frame,message,policy): return
            kind='contextual'
        now=self.runtime.clock()
        normalized=' '.join(__import__('re').findall(r'[\w]+',message['text'].casefold()))
        key=hashlib.sha256(f'{cid}:{kind}:{normalized}:{int(now.timestamp())//3600}'.encode()).hexdigest()
        recent=frame.messages[-6:]
        pace=sum((recent[i]['_at']-recent[i-1]['_at']).total_seconds() for i in range(1,len(recent)))/max(1,len(recent)-1)
        wait=max(12,min(45,pace*1.5))
        payload=dict(owner_id=message['owner_id'],message_id=message['message_id'],kind=kind,mode=policy.mode,
                     reactions=policy.reactions,topic_seeds=policy.topic_seeds,branch=next((m['branch'] for m in frame.messages if m['message_id']==message['message_id']),None))
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',frame.chat_id)
            if kind=='contextual':
                # Coalesce assessment opportunities through the rolling public
                # frame, rather than enqueue a provider call for every message.
                recent_contextual=await conn.fetchval('''SELECT 1 FROM group_candidates
                    WHERE context_id=$1 AND kind='contextual' AND status IN ('pending','deferred','claimed')
                    AND expires_at>$2 LIMIT 1''',cid,now)
                if recent_contextual: return
            queued=await conn.fetchrow('''SELECT count(*) AS total,count(*) FILTER (WHERE g.context_id=$2) AS topic
                FROM group_candidates g JOIN cognitive_contexts c ON c.id=g.context_id
                WHERE c.chat_id=$1 AND g.status IN ('pending','deferred','claimed') AND g.expires_at>$3''',frame.chat_id,cid,now)
            if queued['total']>=128 or queued['topic']>=64: return
            await conn.execute('''INSERT INTO group_candidates(context_id,candidate_key,kind,source_ids,payload,created_at,due_at,expires_at,policy_revision)
                VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9) ON CONFLICT(context_id,candidate_key) DO NOTHING''',
                cid,key,kind,[eid],dump(payload),now,now+timedelta(seconds=wait),now+timedelta(minutes=5),revision)

    async def history(self,scope,mode='default'):
        cid=await self.context_id(scope,mode)
        if cid is None: return ''
        frame=await self.frame(cid)
        return '\n'.join(f"[{m['at']}] {'Арти' if m['is_bot'] else 'Участник '+str(m['owner_id'])}: {m['text']}" for m in frame.messages[-24:])

    async def propose_intention(self,row,event):
        policy,revision=await self.policies.get(row['chat_id'],row['topic_id'])
        p=row['payload']; reminder=p['status']=='reminder'
        if policy.reason(self.runtime.clock(),'reminder' if reminder else 'initiative'): return
        now=self.runtime.clock()
        expires=(datetime.fromisoformat(p['deadline'])+timedelta(hours=24) if reminder and p.get('deadline')
                 else now+timedelta(hours=24 if reminder else 2))
        if expires<=now: return
        async with self.pool.acquire() as conn:
            eid=await conn.fetchval('SELECT id FROM cognitive_events WHERE context_id=$1 AND source_id=$2 AND suppressed_at IS NULL',row['context_id'],event.evidence.source_id)
            if not eid: return
            if not reminder and not await conn.fetchval('SELECT 1 FROM cognitive_contexts WHERE id=$1 AND history_after_event_id<$2 AND NOT rebuilding',row['context_id'],eid): return
            artifact=await conn.fetchrow('SELECT revision,projection_epoch,payload,owner_id FROM cognitive_artifacts WHERE id=$1 AND context_id=$2 AND suppressed_at IS NULL',row['id'],row['context_id'])
            if not artifact: return
            current=object_value(artifact['payload'])
            if (current.get('delivery_key')!=p['delivery_key'] or current.get('status')!=p['status'] or current.get('delivered')
                    or current.get('source_id')!=event.evidence.source_id or artifact['owner_id']!=row['owner_id']): return
            # The description and version must come from the same artifact
            # snapshot; discovery can precede a meaning-preserving reappraisal.
            p=current
            expires=(datetime.fromisoformat(p['deadline'])+timedelta(hours=24) if reminder and p.get('deadline')
                     else now+timedelta(hours=24 if reminder else 2))
            if expires<=now: return
            key='intention:'+p['delivery_key']
            payload=dict(owner_id=row['owner_id'],message_id=int(event.event_id.split(':')[2]),kind='reminder' if reminder else 'followup',
                         mode=policy.mode,reactions=False,intention_id=row['id'],description=p['description'],source_id=event.evidence.source_id,
                         intention_revision=artifact['revision'],intention_epoch=artifact['projection_epoch'],intention_key=p['delivery_key'])
            await conn.execute('''INSERT INTO group_candidates(context_id,candidate_key,kind,source_ids,payload,created_at,due_at,expires_at,policy_revision)
                VALUES($1,$2,$3,$4,$5::jsonb,$6,$6,$7,$8) ON CONFLICT(context_id,candidate_key) DO UPDATE
                SET payload=EXCLUDED.payload,source_ids=EXCLUDED.source_ids,status='pending',due_at=EXCLUDED.due_at,
                    expires_at=EXCLUDED.expires_at,policy_revision=EXCLUDED.policy_revision
                WHERE group_candidates.status='cancelled' AND group_candidates.outbox_id IS NULL
                    AND (group_candidates.kind='reminder' OR group_candidates.kind='followup' AND
                        (SELECT reason FROM group_decisions WHERE candidate_id=group_candidates.id ORDER BY id DESC LIMIT 1)='intention_changed')''',row['context_id'],key,payload['kind'],[eid],dump(payload),now,expires,revision)

    async def continuation(self,scope,text,mode='default'):
        if scope.addressed or scope.reply_to_id or scope.sender_kind!='user': return False
        # This runs on ingress before the normal addressed-message path. Never
        # turn a speculative continuation into a provider-sized ingress stall.
        try:
            async with asyncio.timeout(CONTINUATION_TIMEOUT_SECONDS):
                return await self._continuation(scope,text,mode)
        except Exception: return False

    async def _continuation(self,scope,text,mode):
        cid=await self.context_id(scope,mode)
        if cid is None: return False
        policy,revision=await self.policies.get(scope.chat_id,scope.topic_id)
        if policy.disabled or policy.paused_until and datetime.fromisoformat(policy.paused_until)>self.runtime.clock(): return False
        if await self.policies.opted_out(scope.chat_id,scope.user_id): return False
        frame=await self.frame(cid); last=frame.last_bot
        if frame.closed: return False
        if not last or last.get('owner_id')!=scope.user_id or (self.runtime.clock()-last['_at']).total_seconds()>120: return False
        following=[m for m in frame.messages if m['_at']>last['_at']]
        if len(following)>2 or any(m['owner_id']!=scope.user_id for m in following): return False
        packet=dict(source_id=f'telegram:{scope.chat_id}:{scope.message_id}:user',message_id=scope.message_id,owner_id=scope.user_id,
                    sender_kind='user',text=text[:2500],reply_to_id=None,branch=last['branch'],at=self.runtime.clock().isoformat(),directed=False,is_bot=False)
        frame.messages=frame.messages+[packet]
        from cognition.initiative_policy import provider_slot
        async with provider_slot(self.runtime,cid,scope.user_id,'continuation',hourly_limit=6) as admitted:
            if not admitted: return False
            fresh=await self.frame(cid); _,latest=await self.policies.get(scope.chat_id,scope.topic_id)
            if fresh.revision!=frame.revision or fresh.suppression_epoch!=frame.suppression_epoch or latest!=revision: return False
            if await self.policies.opted_out(scope.chat_id,scope.user_id): return False
            judgement=await self.judge.assess(frame,dict(kind='continuation',mode='mentions',reactions=False,message_id=scope.message_id))
        # A classification is tied to the conversation it actually examined.
        fresh=await self.frame(cid); _,latest=await self.policies.get(scope.chat_id,scope.topic_id)
        if fresh.revision!=frame.revision or fresh.suppression_epoch!=frame.suppression_epoch or latest!=revision: return False
        if await self.policies.opted_out(scope.chat_id,scope.user_id): return False
        return judgement.action=='speak' and judgement.reason=='continuation' and judgement.confidence>=.8

    async def decision(self,row,action,reason,score=None,revision=None):
        async with self.pool.acquire() as conn:
            await conn.execute('INSERT INTO group_decisions(context_id,candidate_id,action,reason,score,policy_revision) VALUES($1,$2,$3,$4,$5,$6)',
                               row['context_id'],row['id'],action,reason,score,revision)

    async def cancel(self,row,reason):
        async with self.pool.acquire() as conn:
            changed=await conn.fetchval("UPDATE group_candidates SET status='cancelled',payload=NULL WHERE id=$1 AND (status IN ('pending','deferred') OR status='claimed' AND lease_token=$2) RETURNING id",row['id'],row.get('lease_token'))
        if changed: await self.decision(row,'abstain',reason)

    async def valid(self,row,frame,policy,revision):
        now=self.runtime.clock(); p=object_value(row['payload'])
        if not p or row['expires_at']<=now: return 'expired'
        if p.get('subscription_id'):
            from agents.group_tasks import guard_subscription
            if not await guard_subscription(self.pool,p): return 'workflow_revoked'
        if frame.closed: return 'topic_closed'
        if row['policy_revision']!=revision: return 'policy_changed'
        reason=policy.reason(now,'reminder' if row['kind']=='reminder' else 'initiative')
        if reason: return reason
        if row['kind']!='reminder' and await self.policies.opted_out(frame.chat_id,p.get('owner_id')): return 'personal_opt_out'
        if frame.serious and row['kind'] in ('social_moment','topic_seed'): return 'serious_context'
        if frame.tension>=.6: return 'tense_context'
        if row['kind']=='open_question':
            q=frame.question(p['message_id'])
            if q and q['status'] in ('answered','closed'): return 'question_resolved'
        if row['kind'] not in ('reminder','followup'):
            anchor=next((m for m in frame.messages if m['message_id']==p['message_id'] and m['event_id'] in row['source_ids']),None)
            if not anchor: return 'source_unavailable'
            if row['kind']!='open_question':
                reply=next((m for m in reversed(frame.messages) if not m.get('is_bot') and m['owner_id']==anchor['owner_id']
                            and m.get('reply_to_id')==anchor['message_id']),None)
                if reply and explicit_refusal(reply['text']): return 'explicit_topic_refusal'
        async with self.pool.acquire() as conn:
            if p.get('intention_id') and not await self.intention_valid(row,p,conn): return 'intention_changed'
            if not await conn.fetchval('SELECT enabled FROM response_status WHERE chat_id=$1',frame.chat_id): return 'responses_disabled'
            sources=await conn.fetchval("SELECT count(*) FROM cognitive_events WHERE id=ANY($1::bigint[]) AND context_id=$2 AND suppressed_at IS NULL AND payload->'audience'->>'kind' IN ('group','topic') AND (payload->'audience'->>'chat_id')::bigint=$3 AND (payload->'audience'->>'topic_id')::bigint=$4",row['source_ids'],row['context_id'],frame.chat_id,frame.topic_id)
        if sources!=len(row['source_ids']): return 'source_suppressed'
        return None

    async def intention_valid(self,row,p,conn):
        artifact=await conn.fetchrow('''SELECT a.*,c.suppression_epoch,c.rebuilding,c.history_after_event_id FROM cognitive_artifacts a
            JOIN cognitive_contexts c ON c.id=a.context_id WHERE a.id=$1 AND a.context_id=$2''',p['intention_id'],row['context_id'])
        if not artifact or artifact['suppressed_at'] or artifact['rebuilding']: return False
        if row['kind']!='reminder' and not await conn.fetchval('''SELECT 1 FROM cognitive_events WHERE context_id=$1
            AND source_id=$2 AND id>$3 AND suppressed_at IS NULL''',row['context_id'],p.get('source_id'),artifact['history_after_event_id']): return False
        current=object_value(artifact['payload'])
        return (artifact['revision']==p.get('intention_revision') and artifact['projection_epoch']==p.get('intention_epoch')==artifact['suppression_epoch']
                and artifact['owner_id']==p.get('owner_id') and current.get('delivery_key')==p.get('intention_key')
                and current.get('source_id')==p.get('source_id') and not current.get('delivered')
                and current.get('status')==('reminder' if row['kind']=='reminder' else 'open'))

    async def mark_intention_delivered(self,p,conn):
        await conn.execute("""UPDATE cognitive_artifacts SET payload=jsonb_set(jsonb_set(payload,'{delivered}','true'::jsonb),
            '{delivered_at}',to_jsonb($3::text)),revision=revision+1
            WHERE id=$1 AND suppressed_at IS NULL AND payload->>'delivery_key'=$2""",
            p['intention_id'],p.get('intention_key'),self.runtime.clock().isoformat())

    async def reserve(self,row,frame,policy,revision):
        now=self.runtime.clock(); token=uuid.uuid4().hex
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',frame.chat_id)
            candidate=await conn.fetchrow('SELECT * FROM group_candidates WHERE id=$1 FOR UPDATE',row['id'])
            if not candidate or candidate['status'] not in ('pending','deferred') or candidate['expires_at']<=now: return None
            group_policy,_=await self.policies.get(frame.chat_id,None,conn)
            charged=await conn.fetch('''SELECT coalesce(g.charged_at,g.created_at) AS created_at,g.status,g.kind FROM group_candidates g JOIN cognitive_contexts c ON c.id=g.context_id
                WHERE c.chat_id=$1 AND g.status IN ('claimed','delivered','delivery_unknown','shadow') AND coalesce(g.charged_at,g.created_at)>$2 ORDER BY coalesce(g.charged_at,g.created_at) DESC''',frame.chat_id,now-timedelta(days=1))
            charged=[r for r in charged if r['kind']!='reminder']
            assessed=await conn.fetchval('''SELECT coalesce(sum(g.attempts),0) FROM group_candidates g
                JOIN cognitive_contexts c ON c.id=g.context_id WHERE c.chat_id=$1 AND g.kind!='reminder'
                AND g.charged_at>$2''',frame.chat_id,now-timedelta(hours=1))
            if row['kind']!='reminder' and assessed>=min(policy.assessment_hourly_limit,group_policy.assessment_hourly_limit): return False
            if row['kind']=='contextual':
                used=await conn.fetchval('''SELECT coalesce(sum(g.attempts),0) FROM group_candidates g
                    JOIN cognitive_contexts c ON c.id=g.context_id WHERE c.chat_id=$1 AND g.kind='contextual'
                    AND g.charged_at>$2''',frame.chat_id,now-timedelta(hours=1))
                # Leave at least half the assessment budget for explicit cues.
                if used>=max(1,min(policy.assessment_hourly_limit,group_policy.assessment_hourly_limit)//2): return False
            if row['kind']!='reminder' and (len(charged)>=min(policy.daily_limit,group_policy.daily_limit) or charged and (now-charged[0]['created_at']).total_seconds()<max(policy.spacing_seconds,group_policy.spacing_seconds)):
                return False
            if row['kind']!='reminder' and len(frame.messages)>=10 and sum(m['is_bot'] for m in frame.messages[-30:])/min(30,len(frame.messages))>=policy.max_share:
                return False
            lease=await conn.fetchval('''INSERT INTO group_action_leases(context_id,token,fence,lease_until) VALUES($1,$2,1,$3)
                ON CONFLICT(context_id) DO UPDATE SET token=EXCLUDED.token,fence=group_action_leases.fence+1,lease_until=EXCLUDED.lease_until
                WHERE group_action_leases.lease_until IS NULL OR group_action_leases.lease_until<=$4 RETURNING fence''',frame.context_id,token,now+timedelta(seconds=180),now)
            if lease is None: return 'busy'
            await conn.execute("UPDATE group_candidates SET status='claimed',lease_token=$2,charged_at=$3,attempts=attempts+1 WHERE id=$1",row['id'],token,now)
        return token

    async def release(self,cid,token):
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE group_action_leases SET token=NULL,lease_until=NULL WHERE context_id=$1 AND token=$2',cid,token)

    async def renew(self,cid,token):
        while True:
            await asyncio.sleep(30)
            now=self.runtime.clock()
            async with self.pool.acquire() as conn:
                alive=await conn.fetchval('UPDATE group_action_leases SET lease_until=$3 WHERE context_id=$1 AND token=$2 AND lease_until>$4 RETURNING fence',cid,token,now+timedelta(seconds=180),now)
            if alive is None:
                from cognition.delivery import DeliverySuppressed
                raise DeliverySuppressed('Lease lost')

    async def direct_lease(self,cid):
        token=uuid.uuid4().hex
        try:
            async with asyncio.timeout(DIRECT_LEASE_TIMEOUT_SECONDS):
                while True:
                    now=self.runtime.clock()
                    async with self.pool.acquire() as conn,conn.transaction():
                        chat_id=await conn.fetchval('SELECT chat_id FROM cognitive_contexts WHERE id=$1',cid)
                        # Match the final delivery guard's lock order. An
                        # initiative can be displaced before, never during, its
                        # irreversible transport attempt.
                        await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',chat_id)
                        lease=await conn.fetchrow('SELECT * FROM group_action_leases WHERE context_id=$1 FOR UPDATE',cid)
                        if lease and lease['token']:
                            displaced=await conn.fetchval('''UPDATE group_candidates g SET status='cancelled',payload=NULL
                                WHERE g.context_id=$1 AND g.lease_token=$2 AND g.status='claimed' AND g.kind!='reminder'
                                AND NOT EXISTS(SELECT 1 FROM cognitive_outbox o WHERE o.id=g.outbox_id
                                    AND o.status IN ('sending','delivered','delivery_unknown')) RETURNING g.id''',cid,lease['token'])
                            if displaced:
                                await conn.execute('UPDATE group_action_leases SET token=NULL,lease_until=NULL WHERE context_id=$1',cid)
                        claimed=await conn.fetchval('''INSERT INTO group_action_leases(context_id,token,fence,lease_until) VALUES($1,$2,1,$3)
                            ON CONFLICT(context_id) DO UPDATE SET token=EXCLUDED.token,fence=group_action_leases.fence+1,lease_until=EXCLUDED.lease_until
                            WHERE group_action_leases.lease_until IS NULL OR group_action_leases.lease_until<=$4 RETURNING fence''',cid,token,now+timedelta(seconds=120),now)
                    if claimed is not None: return token
                    await asyncio.sleep(.05)
        except TimeoutError: pass
        from cognition.delivery import DeliverySuppressed
        raise DeliverySuppressed('Conversation is busy')

    async def run_cycle(self,bot):
        now=self.runtime.clock()
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE group_candidates SET status='cancelled',payload=NULL WHERE expires_at<=$1 AND status IN ('pending','deferred')",now)
            recovered=await conn.fetch('''UPDATE group_candidates g SET status=CASE
                WHEN EXISTS(SELECT 1 FROM cognitive_outbox o WHERE o.id=g.outbox_id AND o.status='delivered') THEN 'delivered'
                WHEN EXISTS(SELECT 1 FROM cognitive_outbox o WHERE o.id=g.outbox_id AND o.status IN ('sending','delivery_unknown')) THEN 'delivery_unknown'
                ELSE 'cancelled' END WHERE status='claimed' AND NOT EXISTS(SELECT 1 FROM group_action_leases l
                WHERE l.context_id=g.context_id AND l.token=g.lease_token AND l.lease_until>$1) RETURNING g.*''',now)
            for item in recovered:
                payload=object_value(item['payload']) or {}
                if payload.get('intention_id') and item['status']=='delivered':
                    await self.mark_intention_delivered(payload,conn)
                await conn.execute('UPDATE group_candidates SET payload=NULL WHERE id=$1',item['id'])
            rows=await conn.fetch("SELECT * FROM group_candidates WHERE status IN ('pending','deferred') AND due_at<=$1 ORDER BY due_at,id LIMIT 20",now)
        for row in rows:
            frame=await self.frame(row['context_id']); policy,revision=await self.policies.get(frame.chat_id,frame.topic_id)
            if row['kind']!='reminder' and getattr(self.runtime,'bot_id',None):
                try:
                    async with asyncio.timeout(MEMBERSHIP_TIMEOUT_SECONDS):
                        me=await bot.get_me(); member=await bot.get_chat_member(frame.chat_id,me.id)
                    visible=member.status in ('administrator','creator') or bool(getattr(me,'can_read_all_group_messages',False))
                except Exception: visible=False
                if not visible:
                    await self.policies.set(frame.chat_id,dict(full_visibility=False))
                    await self.cancel(row,'visibility_lost'); continue
            reason=await self.valid(row,frame,policy,revision)
            if reason: await self.cancel(row,reason); continue
            token=await self.reserve(row,frame,policy,revision)
            if token=='busy':
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE group_candidates SET status='deferred',due_at=$2 WHERE id=$1 AND status IN ('pending','deferred')",row['id'],now+timedelta(seconds=5))
                continue
            if token is None: continue
            if token is False:
                await self.cancel(row,'participation_budget'); continue
            row={**dict(row),'lease_token':token}
            renewal=asyncio.create_task(self.renew(frame.context_id,token))
            work=asyncio.create_task(self.execute(row,frame,policy,revision,token,bot))
            try:
                done,_=await asyncio.wait((renewal,work),return_when=asyncio.FIRST_COMPLETED)
                for task in done: task.result()
            except asyncio.CancelledError: raise
            except Exception:
                await self.cancel(row,'provider_or_internal_failure')
            finally:
                renewal.cancel(); work.cancel()
                await asyncio.gather(renewal,work,return_exceptions=True)
                await self.release(frame.context_id,token)

    @staticmethod
    def frame_sources(frame,anchor=None):
        packet=frame.public_packet(anchor)
        sources={m['source_id'] for m in packet['messages']}
        for contribution in packet.get('recent_contributions',()):
            sources.update(contribution.get('source_ids',()))
            sources.update(signal['source_id'] for signal in contribution.get('feedback',()))
        # The model's compact provenance list can truncate IDs. Internal
        # erasure/dependency fences must retain the full lineage of that text.
        exported={(c.get('message_id'),c.get('at'),c.get('delivery_status')) for c in packet.get('recent_contributions',())}
        for contribution in frame.outcomes:
            if (contribution.get('message_id'),contribution.get('at'),contribution.get('delivery_status')) in exported:
                sources.update(contribution.get('source_ids',()))
                sources.update(signal['source_id'] for signal in contribution.get('feedback',()))
        return sources

    async def revalidate(self,row,frame,policy,revision,token):
        reason=await self.valid(row,frame,policy,revision)
        if reason:
            await self.cancel(row,reason); return False
        p=object_value(row['payload'])
        sources=self.frame_sources(frame,p.get('message_id')) if row['kind']!='reminder' else set()
        async with self.pool.acquire() as conn:
            ctx=await conn.fetchrow('SELECT suppression_epoch,rebuilding,history_after_event_id FROM cognitive_contexts WHERE id=$1',frame.context_id)
            permitted=await conn.fetchval("""SELECT count(*) FROM cognitive_events WHERE context_id=$1 AND source_id=ANY($2::text[])
                AND suppressed_at IS NULL AND id>$3 AND payload->'audience'->>'kind' IN ('group','topic')
                AND (payload->'audience'->>'chat_id')::bigint=$4 AND (payload->'audience'->>'topic_id')::bigint=$5""",
                frame.context_id,list(sources),ctx['history_after_event_id'],frame.chat_id,frame.topic_id)
            alive=await conn.fetchval("""SELECT 1 FROM group_candidates g JOIN group_action_leases l ON l.context_id=g.context_id
                WHERE g.id=$1 AND g.status='claimed' AND g.lease_token=$2 AND l.token=$2 AND l.lease_until>$3""",row['id'],token,self.runtime.clock())
        if ctx['rebuilding'] or ctx['suppression_epoch']!=frame.suppression_epoch or permitted!=len(sources):
            await self.cancel(row,'evidence_changed'); return False
        if not alive:
            await self.cancel(row,'lease_lost'); return False
        return True

    async def execute(self,row,frame,policy,revision,token,bot):
        anchor_event_id=row['source_ids'][0]
        p={**object_value(row['payload']),'age_seconds':max(0.,(self.runtime.clock()-row['created_at']).total_seconds())}
        if row['kind'] in ('reminder','followup'):
            try:
                async with asyncio.timeout(MEMBERSHIP_TIMEOUT_SECONDS):
                    member=await bot.get_chat_member(frame.chat_id,p['owner_id'])
                if member.status in ('left','kicked'):
                    await self.cancel(row,'owner_left'); return
            except Exception:
                await self.cancel(row,'membership_unknown'); return
        from cognition.initiative_policy import provider_slot
        from cognition.affect import expression
        from cognition.runtime import PreparedTurn,CURRENT_TURN
        # Both assessment and composition belong to one immutable conversation
        # revision. Changed meaning gets one new assessment, never a fresh stamp
        # on old prose. Continuous chatter safely exhausts this bounded retry.
        for attempt in range(SEMANTIC_RETRIES+1):
            frame=await self.frame(frame.context_id)
            policy,revision=await self.policies.get(frame.chat_id,frame.topic_id)
            if not await self.revalidate(row,frame,policy,revision,token): return
            if row['kind']=='reminder':
                from ai.group_participation import GroupJudgement
                async with self.pool.acquire() as conn: sid=await conn.fetchval('SELECT source_id FROM cognitive_events WHERE id=$1',anchor_event_id)
                judgement=GroupJudgement('speak','shared_task',1.,0.,1.,(sid,))
            else:
                if row['kind']=='followup' and p['source_id'] not in {m['source_id'] for m in frame.messages}:
                    async with self.pool.acquire() as conn: raw=await conn.fetchrow('SELECT * FROM cognitive_events WHERE id=$1 AND suppressed_at IS NULL',anchor_event_id)
                    if not raw or not raw['payload']:
                        await self.cancel(row,'source_unavailable'); return
                    ev=load_event(raw['payload'])
                    frame.messages=[dict(source_id=ev.evidence.source_id,message_id=p['message_id'],owner_id=ev.evidence.owner_id,text=ev.text[:2500],
                        sender_kind='user',reply_to_id=ev.reply_to_id,branch=p['message_id'],at=ev.observed_at.isoformat(),_at=ev.observed_at,event_id=raw['id'],directed=True,is_bot=False)]+frame.messages[-31:]
                async with asyncio.timeout(ASSESS_TIMEOUT_SECONDS):
                    async with provider_slot(self.runtime,frame.context_id,p.get('owner_id'),'group_assess',hourly_limit=policy.assessment_hourly_limit) as admitted:
                        if not admitted:
                            await self.cancel(row,'provider_budget'); return
                        if not await self.revalidate(row,frame,policy,revision,token): return
                        judgement=await self.judge.assess(frame,p)
            fresh=await self.frame(frame.context_id); latest,rev=await self.policies.get(frame.chat_id,frame.topic_id)
            if fresh.suppression_epoch!=frame.suppression_epoch:
                await self.cancel(row,'evidence_changed'); return
            if not await self.revalidate(row,fresh,latest,rev,token): return
            if fresh.revision!=frame.revision:
                if attempt<SEMANTIC_RETRIES: continue
                await self.cancel(row,'conversation_changed'); return
            if judgement.action=='defer' and row['attempts']<2:
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE group_candidates SET status='deferred',due_at=$2,lease_token=NULL WHERE id=$1 AND status='claimed' AND lease_token=$3",row['id'],self.runtime.clock()+timedelta(seconds=judgement.defer_seconds),token)
                await self.decision(row,'defer',judgement.reason,judgement.usefulness,revision); return
            norms=frame.norms.get('by_kind',{}).get(row['kind'],frame.norms)
            threshold=.65+max(0.,-norms['receptivity'])*norms['confidence']*.2
            if judgement.action!='speak' or judgement.confidence<.7 or judgement.usefulness<threshold or judgement.interruption>.35:
                async with self.pool.acquire() as conn: await conn.execute("UPDATE group_candidates SET status='abstained',payload=NULL WHERE id=$1 AND status='claimed' AND lease_token=$2",row['id'],token)
                await self.decision(row,'abstain',judgement.reason,judgement.usefulness,revision); return
            if judgement.channel=='reaction' and not policy.reactions:
                await self.cancel(row,'reaction_not_permitted'); return
            if judgement.reason in ('social_fit','topic_seed') and policy.mode!='social':
                await self.cancel(row,'social_not_permitted'); return
            if judgement.reason=='topic_seed' and not policy.topic_seeds:
                await self.cancel(row,'topic_seed_not_permitted'); return
            async with self.pool.acquire() as conn:
                ctx=await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1',frame.context_id)
                required=set(judgement.evidence_ids)|(self.frame_sources(frame,p.get('message_id')) if row['kind']!='reminder' else set())
                evidence=await conn.fetch('SELECT id FROM cognitive_events WHERE context_id=$1 AND source_id=ANY($2::text[]) AND suppressed_at IS NULL',frame.context_id,list(required))
                source=await conn.fetchrow('SELECT * FROM cognitive_events WHERE id=$1 AND suppressed_at IS NULL',anchor_event_id)
                if len(evidence)!=len(required) or not source or not source['payload']:
                    await self.cancel(row,'source_unavailable'); return
                ids=sorted(set(row['source_ids'])|{r['id'] for r in evidence})
                changed=await conn.fetchval("UPDATE group_candidates SET source_ids=$2 WHERE id=$1 AND status='claimed' AND lease_token=$3 RETURNING id",row['id'],ids,token)
            if not changed: return
            row={**dict(row),'source_ids':ids}
            if policy.execution=='shadow' or ctx['authority']!='active' or self.runtime.mode=='legacy':
                async with self.pool.acquire() as conn: await conn.execute("UPDATE group_candidates SET status='shadow',payload=NULL WHERE id=$1 AND status='claimed' AND lease_token=$2",row['id'],token)
                await self.decision(row,'speak','shadow_selected',judgement.usefulness,revision); return
            event=load_event(source['payload'])
            style=expression(await self.runtime.personal_state(frame.context_id,event.evidence.owner_id),task_serious=frame.serious)
            if p.get('subscription_id'): text=p['workflow_text']
            elif row['kind']=='reminder': text=('Напоминание: '+p['description'])[:600]
            else:
                async with asyncio.timeout(COMPOSE_TIMEOUT_SECONDS):
                    async with provider_slot(self.runtime,frame.context_id,p.get('owner_id'),'group_compose',hourly_limit=policy.assessment_hourly_limit) as admitted:
                        if not admitted:
                            await self.cancel(row,'provider_budget'); return
                        if not await self.revalidate(row,frame,policy,revision,token): return
                        text=await self.judge.compose(frame,p,judgement,replace(style,disclosure=0.).instruction())
            fresh=await self.frame(frame.context_id); latest,rev=await self.policies.get(frame.chat_id,frame.topic_id)
            if fresh.suppression_epoch!=frame.suppression_epoch:
                await self.cancel(row,'evidence_changed'); return
            if not await self.revalidate(row,fresh,latest,rev,token): return
            if fresh.revision!=frame.revision:
                if attempt<SEMANTIC_RETRIES: continue
                await self.cancel(row,'conversation_changed'); return
            if not text: await self.cancel(row,'no_added_value'); return
            break
        if p.get('subscription_id'):
            async with asyncio.timeout(MEMBERSHIP_TIMEOUT_SECONDS):
                member=await bot.get_chat_member(frame.chat_id,p['owner_id'])
            if member.status not in ('member','administrator','creator'): await self.cancel(row,'workflow_member_left'); return
        turn=PreparedTurn(self.runtime,frame.context_id,source['id'],replace(event,event_id='group:'+row['candidate_key']),style,'',frame.suppression_epoch,'active')
        turn.group_candidate_id=row['id']; turn.group_lease_token=token; turn.group_policy_revision=revision; turn.group_frame_revision=frame.revision
        if row['kind']!='reminder':
            turn.initiative=dict(owner_id=p.get('owner_id'),context_daily=policy.daily_limit,spacing_seconds=policy.spacing_seconds)
        scope=TransportScope(frame.chat_id,frame.topic_id,'supergroup',event.evidence.owner_id,p['message_id'],True)
        t=CURRENT_TURN.set(turn); s=CURRENT_SCOPE.set(scope)
        try:
            from cognition.delivery import send_with_receipt,DeliveryUnknown
            from bot.retry_bot import RetryBot
            kwargs=dict(chat_id=frame.chat_id)
            if judgement.channel=='reaction':
                from telegram import ReactionTypeEmoji
                method=bot.set_message_reaction; kwargs.update(message_id=p['message_id'],reaction=[ReactionTypeEmoji(text)])
            else:
                from telegram import ReplyParameters
                method=bot.send_message; kwargs.update(**scope.send_kwargs(),text=text,reply_parameters=ReplyParameters(p['message_id'],allow_sending_without_reply=False),disable_notification=True)
            try:
                if isinstance(bot,RetryBot): await method(**kwargs)
                else: await send_with_receipt(method,(),kwargs,'reaction' if judgement.channel=='reaction' else 'message')
            except DeliveryUnknown:
                async with self.pool.acquire() as conn: await conn.execute("UPDATE group_candidates SET status='delivery_unknown',payload=NULL WHERE id=$1",row['id'])
                if p.get('subscription_id'):
                    from agents.group_tasks import record_group_delivery
                    await record_group_delivery(self.pool,p,'unknown',row['candidate_key'])
                await self.decision(row,'abstain','delivery_unknown'); return
            # A transport return is not a confirmed receipt: reset/erasure can
            # invalidate confirmation while the external request is in flight.
            async with self.pool.acquire() as conn:
                confirmed=await conn.fetchval("""UPDATE group_candidates g SET status='delivered',payload=NULL,charged_at=$2
                    FROM cognitive_outbox o WHERE g.id=$1 AND g.outbox_id=o.id AND o.status='delivered'
                    AND g.lease_token=$3 RETURNING g.id""",row['id'],self.runtime.clock(),token)
            if not confirmed:
                await self.cancel(row,'delivery_unconfirmed'); return
            if p.get('subscription_id'):
                from agents.group_tasks import record_group_delivery
                await record_group_delivery(self.pool,p,'delivered',row['candidate_key'])
            if p.get('intention_id'):
                async with self.pool.acquire() as conn:
                    await self.mark_intention_delivered(p,conn)
            await self.decision(row,'speak',judgement.reason,judgement.usefulness,revision)
        finally: CURRENT_TURN.reset(t); CURRENT_SCOPE.reset(s)

    async def delivery_guard(self,turn,conn):
        row=await conn.fetchrow('SELECT * FROM group_candidates WHERE id=$1 FOR UPDATE',turn.group_candidate_id)
        if not row or row['status']!='claimed' or row['lease_token']!=turn.group_lease_token: return False
        p=object_value(row['payload'])
        if p.get('intention_id') and not await self.intention_valid(row,p,conn): return False
        if row['kind']!='reminder' and await conn.fetchval('SELECT opt_out FROM group_participant_settings WHERE chat_id=$1 AND user_id=$2',turn.event.context.chat_id,p.get('owner_id')): return False
        if p.get('subscription_id'):
            from agents.group_tasks import guard_subscription
            if not await guard_subscription(self.pool,p,conn): return False
        lease=await conn.fetchval('SELECT 1 FROM group_action_leases WHERE context_id=$1 AND token=$2 AND lease_until>$3',turn.context_id,turn.group_lease_token,self.runtime.clock())
        revision=await conn.fetchval('SELECT revision FROM group_topic_runtime WHERE context_id=$1',turn.context_id)
        if not lease or revision!=turn.group_frame_revision: return False
        policy,rev=await self.policies.get(turn.event.context.chat_id,turn.event.context.topic_id,conn)
        if rev!=turn.group_policy_revision or policy.execution!='live' or policy.reason(self.runtime.clock(),'reminder' if row['kind']=='reminder' else 'initiative'): return False
        if not await conn.fetchval('SELECT enabled FROM response_status WHERE chat_id=$1',turn.event.context.chat_id): return False
        sources=await conn.fetchval("SELECT count(*) FROM cognitive_events WHERE id=ANY($1::bigint[]) AND context_id=$2 AND suppressed_at IS NULL AND payload->'audience'->>'kind' IN ('group','topic') AND (payload->'audience'->>'chat_id')::bigint=$3 AND (payload->'audience'->>'topic_id')::bigint=$4",row['source_ids'],row['context_id'],turn.event.context.chat_id,turn.event.context.topic_id)
        return sources==len(row['source_ids'])

    async def feedback(self,scope,message_id,user_id,signal):
        async with self.pool.acquire() as conn:
            receipt=await conn.fetchrow('''SELECT o.context_id,g.source_ids,c.persona_id,c.chat_id,c.topic_id,c.mode,c.scene_id FROM cognitive_outbox o JOIN group_candidates g ON g.outbox_id=o.id
                JOIN cognitive_contexts c ON c.id=o.context_id WHERE c.chat_id=$1 AND o.receipt_id=$2 AND o.status='delivered' ''',scope.chat_id,message_id)
        if not receipt: return False
        from cognition.types import ContextKey
        ctx=ContextKey(receipt['persona_id'],receipt['chat_id'],receipt['mode'],receipt['scene_id'],receipt['topic_id'])
        _,eid,_=await self.runtime.ingest(scope.chat_id,user_id,str(signal),f'reaction:{message_id}:{user_id}:{self.runtime.clock().isoformat()}',
            receipt['mode'],context=ctx,event_kind='reaction',addressed_to_arti=False,
            audience=AudienceScope('topic' if ctx.topic_id>0 else 'group',scope.chat_id,ctx.topic_id))
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',receipt['context_id'])
            await conn.execute('''INSERT INTO group_feedback VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(context_id,message_id,user_id)
                DO UPDATE SET signal=EXCLUDED.signal,created_at=EXCLUDED.created_at,source_ids=EXCLUDED.source_ids''',receipt['context_id'],message_id,user_id,signal,self.runtime.clock(),sorted(set(receipt['source_ids'])|{eid}))
            await conn.execute('''INSERT INTO group_topic_runtime(context_id,revision) VALUES($1,1)
                ON CONFLICT(context_id) DO UPDATE SET revision=group_topic_runtime.revision+1''',receipt['context_id'])
        return True

    async def set_closed(self,scope,closed):
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',scope.chat_id)
            await conn.fetch('SELECT id FROM cognitive_contexts WHERE chat_id=$1 AND topic_id=$2 ORDER BY id FOR UPDATE',scope.chat_id,scope.topic_id)
            await conn.execute('''INSERT INTO group_topic_runtime(context_id,closed,revision)
                SELECT id,$3,1 FROM cognitive_contexts WHERE chat_id=$1 AND topic_id=$2
                ON CONFLICT(context_id) DO UPDATE SET closed=EXCLUDED.closed,revision=group_topic_runtime.revision+1''',scope.chat_id,scope.topic_id,closed)
            if closed:
                await conn.execute("UPDATE group_candidates SET status='cancelled',payload=NULL WHERE context_id IN (SELECT id FROM cognitive_contexts WHERE chat_id=$1 AND topic_id=$2) AND status IN ('pending','deferred','claimed')",scope.chat_id,scope.topic_id)

    async def migrate_chat(self,old_chat,new_chat):
        """Known Telegram migration invalidates actions; it never moves consent/history."""
        for chat_id in sorted({old_chat,new_chat}):
            await self.policies.set(chat_id,dict(mode='mentions',execution='shadow',full_visibility=False))
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute("UPDATE group_candidates SET status='cancelled',payload=NULL WHERE context_id IN (SELECT id FROM cognitive_contexts WHERE chat_id=ANY($1::bigint[])) AND status IN ('pending','deferred','claimed')",[old_chat,new_chat])
            if not getattr(self.runtime,'strict',False):
                await conn.execute("UPDATE cognitive_contexts SET authority='shadow',authority_explicit=TRUE WHERE chat_id=ANY($1::bigint[])",[old_chat,new_chat])
            else:
                from cognition.runtime import fence_context
                contexts = await conn.fetch('SELECT id FROM cognitive_contexts WHERE chat_id=ANY($1::bigint[]) ORDER BY id FOR UPDATE',[old_chat,new_chat])
                for context in contexts:
                    await fence_context(conn,context['id'])
            await conn.execute("UPDATE group_topic_settings SET payload=payload || '{\"mode\":\"mentions\",\"execution\":\"shadow\",\"full_visibility\":false}'::jsonb,revision=revision+1 WHERE chat_id=ANY($1::bigint[])",[old_chat,new_chat])

    async def maintenance(self):
        async with self.pool.acquire() as conn:
            await conn.execute('''UPDATE group_observations o SET payload=NULL,suppressed_at=NOW() FROM cognitive_events e
                WHERE e.id=o.event_id AND e.suppressed_at IS NOT NULL AND o.payload IS NOT NULL''')
            await conn.execute("UPDATE group_candidates g SET status='cancelled',payload=NULL WHERE status IN ('pending','deferred','claimed') AND EXISTS(SELECT 1 FROM cognitive_events e WHERE e.id=ANY(g.source_ids) AND e.suppressed_at IS NOT NULL)")
            await conn.execute("DELETE FROM group_decisions WHERE created_at<NOW()-INTERVAL '30 days'")
            await conn.execute("DELETE FROM group_feedback WHERE created_at<NOW()-INTERVAL '30 days'")
            await conn.execute('''UPDATE group_observations o SET payload=NULL,suppressed_at=NOW()
                FROM cognitive_contexts c LEFT JOIN group_chat_settings g ON g.chat_id=c.chat_id
                LEFT JOIN group_topic_settings t ON t.chat_id=c.chat_id AND t.topic_id=c.topic_id
                WHERE o.context_id=c.id AND o.payload IS NOT NULL AND o.observed_at<NOW()-make_interval(days=>coalesce((t.payload->>'retention_days')::int,(g.payload->>'retention_days')::int,30))''')
            await conn.execute("DELETE FROM group_candidates WHERE created_at<NOW()-INTERVAL '30 days' AND status IN ('shadow','cancelled','abstained','delivered','delivery_unknown') AND NOT EXISTS(SELECT 1 FROM group_decisions d WHERE d.candidate_id=group_candidates.id)")


async def group_scheduler(runtime,bot):
    while True:
        try:
            if runtime.mode=='legacy':
                await asyncio.sleep(5); continue
            await runtime.groups.maintenance()
            await runtime.groups.run_cycle(bot)
        except asyncio.CancelledError: raise
        except Exception: __import__('logging').getLogger(__name__).error('Group cycle failed')
        await asyncio.sleep(5)
