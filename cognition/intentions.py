"""Prospective memory with operational reminder delivery and contextual follow-up."""
import asyncio
from datetime import datetime
from cognition.affect import advance,expression
from cognition.runtime import PreparedTurn,CURRENT_TURN
from cognition.serialization import object_value,load_event,dump


async def due_intentions(runtime):
    if runtime.mode=='legacy':
        return []
    now = runtime.clock()
    async with runtime.pool.acquire() as conn:
        rows = await conn.fetch("""SELECT a.*,c.chat_id,c.mode,c.scene_id,c.topic_id,c.suppression_epoch FROM cognitive_artifacts a
            JOIN cognitive_contexts c ON c.id=a.context_id WHERE a.kind='intention' AND a.suppressed_at IS NULL
              AND a.payload IS NOT NULL AND c.authority='active' AND NOT c.rebuilding AND a.projection_epoch=c.suppression_epoch
              AND coalesce((a.payload->>'delivered')::boolean,false)=false
              AND ((a.payload->>'status'='reminder' AND (a.payload->>'deadline')::timestamptz<=$1)
                OR (a.payload->>'status'='open' AND coalesce(a.payload->>'actor_id','')!='arti'
                    AND (a.payload->>'created_at')::timestamptz BETWEEN $1-INTERVAL '14 days' AND $1-INTERVAL '1 day'))
              AND NOT EXISTS (
                WITH RECURSIVE owned_ancestors(id) AS (
                    SELECT p.source_event_id FROM cognitive_provenance p WHERE p.context_id=a.context_id AND p.artifact_id=a.id
                    UNION SELECT d.source_event_id FROM cognitive_event_dependencies d
                        JOIN owned_ancestors n ON d.event_id=n.id
                        JOIN cognitive_events child ON child.id=d.event_id
                        JOIN cognitive_events parent ON parent.id=d.source_event_id
                        WHERE d.context_id=a.context_id AND (child.origin='delivered_action'
                            OR child.payload->>'reply_to_id'=split_part(parent.source_id,':',3)))
                SELECT 1 FROM owned_ancestors n JOIN cognitive_events e ON e.id=n.id AND e.context_id=a.context_id
                    JOIN arti_organizer_source_routes r ON r.owner_id=e.owner_id AND r.chat_id=c.chat_id AND r.source_key=e.source_id
                    WHERE c.chat_id=e.owner_id AND c.chat_id>0 AND c.topic_id<0)
              ORDER BY a.payload->>'deadline' NULLS LAST,a.id LIMIT 512""",now)
    result = []
    for row in rows:
        p = object_value(row['payload'])
        if p['status'] not in ('reminder','open') or p.get('delivered'):
            continue
        deadline = datetime.fromisoformat(p['deadline']) if p.get('deadline') else None
        model = await runtime.memory.relationship(row['context_id'],row['owner_id'])
        if p['status']=='reminder':
            eligible = deadline is not None and deadline<=now
        else:
            # Spontaneous outcome inquiry needs explicit receptivity, a cue and
            # enough time to plausibly observe the expected outcome.
            created = datetime.fromisoformat(p['created_at'])
            eligible = (model['preferences'].get('proactive') is True and bool(p['cue'])
                        and 86400 <= (now-created).total_seconds() <= 14*86400)
        if eligible and row['mode']=='rp':
            from config import rp_mode_state
            current = await runtime.context(row['chat_id'],'rp',row['topic_id'])
            from cognition.scope import CURRENT_SCOPE,TransportScope
            token=CURRENT_SCOPE.set(TransportScope(row['chat_id'],row['topic_id'],'supergroup' if row['chat_id']<0 else 'private'))
            try: eligible = bool(rp_mode_state.get(row['chat_id'])) and current.scene_id==row['scene_id']
            finally: CURRENT_SCOPE.reset(token)
        if eligible:
            result.append(({**dict(row),'payload':p},model))
    return result


async def run_intention_cycle(runtime,bot):
    for row,relationship in await due_intentions(runtime):
        p = row['payload']
        if row['chat_id']<0 or row['topic_id']>=0:
            async with runtime.pool.acquire() as conn:
                source=await conn.fetchrow('SELECT payload FROM cognitive_events WHERE context_id=$1 AND source_id=$2 AND suppressed_at IS NULL',row['context_id'],p['source_id'])
            if not source: continue
            event=load_event(source['payload'])
            if not event.audience.permits(row['chat_id'],row['topic_id']): continue
            # A preference of one owner never grants group permission. Existing
            # commitments enter the same scoped coordinator as other actions.
            try:
                member=await bot.get_chat_member(row['chat_id'],row['owner_id'])
                if member.status in ('left','kicked'): continue
            except Exception: continue
            await runtime.groups.propose_intention(row,event)
            continue
        async with runtime.pool.acquire() as conn:
            source = await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1 AND source_id=$2 AND suppressed_at IS NULL',row['context_id'],p['source_id'])
            prior_delivery = await conn.fetchval('''SELECT 1 FROM cognitive_outbox WHERE context_id=$1 AND delivery_key LIKE $2
                AND status IN ('delivered','sending','delivery_unknown')''',row['context_id'],p['delivery_key']+':%')
        if not source or prior_delivery:
            continue
        event = load_event(source['payload'])
        state = await runtime.personal_state(row['context_id'],row['owner_id'])
        plan = expression(state,task_serious=True)
        turn = PreparedTurn(runtime,row['context_id'],source['id'],event,plan,'',row['suppression_epoch'],'active')
        # An intention has its own stable delivery slot; input conversation sends
        # cannot collide with a due reminder for the same source.
        turn.event = __import__('dataclasses').replace(event,event_id=p['delivery_key'])
        token = CURRENT_TURN.set(turn)
        try:
            from organizer.ownership import cognitive_delivery_allowed
            async with cognitive_delivery_allowed(runtime.pool,row['owner_id'],row['chat_id'],row['context_id'],row['id']) as allowed:
                if not allowed: continue
                text = ('Напоминание: ' if p['status']=='reminder' else 'Как продвигается: ') + p['description']
                from cognition.delivery import send_with_receipt
                from bot.retry_bot import RetryBot
                if isinstance(bot,RetryBot):
                    await bot.send_message(chat_id=row['chat_id'],text=text)
                else:
                    await send_with_receipt(bot.send_message,(),dict(chat_id=row['chat_id'],text=text),'message')
                async with runtime.pool.acquire() as conn,conn.transaction():
                    await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',row['context_id'])
                    active = await conn.fetchrow('SELECT payload FROM cognitive_artifacts WHERE id=$1 AND suppressed_at IS NULL',row['id'])
                    if active:
                        current = object_value(active['payload'])
                        current['delivered'] = True
                        current['delivered_at'] = runtime.clock().isoformat()
                        await conn.execute('UPDATE cognitive_artifacts SET payload=$2::jsonb,revision=revision+1 WHERE id=$1',row['id'],dump(current))
        finally:
            CURRENT_TURN.reset(token)


async def intention_scheduler(runtime,bot):
    while True:
        try:
            await run_intention_cycle(runtime,bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A durable unknown delivery is inspected by maintenance, never
            # retried blindly at the next tick.
            __import__('logging').getLogger(__name__).error('Intention cycle failed')
        await asyncio.sleep(60)
