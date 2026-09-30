"""Confirmed receipts and an outbox that preserves ambiguous transport outcomes."""
import hashlib
import asyncio
from datetime import datetime,timezone
from cognition.runtime import CURRENT_TURN
from cognition.serialization import dump,object_value
from cognition.types import Origin,CognitiveEvent,EvidenceRef
from cognition.repositories import SuppressedEvidence


class DeliveryUnknown(Exception):
    pass


class DeliverySuppressed(Exception):
    pass


async def send_with_receipt(method,args,kwargs,channel):
    turn = CURRENT_TURN.get()
    if turn is None or not turn.active:
        return await method(*args,**kwargs)
    if turn.delivery_blocked:
        raise DeliveryUnknown()
    chat_id = kwargs.get('chat_id',args[0] if args else None)
    if chat_id!=turn.event.context.chat_id:
        raise ValueError('Transport destination differs from the cognitive context')
    runtime = turn.runtime
    turn.send_ordinal += 1
    delivery_key = f'{turn.event.event_id}:{channel}:{turn.send_ordinal}'
    text = str(kwargs.get('text') or kwargs.get('caption') or '')
    # Files, API tokens, reply markup and raw provider errors are never serialized.
    payload = dict(text=text,channel=channel)
    async with runtime.pool.acquire() as conn,conn.transaction():
        ctx = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE',turn.context_id)
        permitted = await conn.fetchval('SELECT 1 FROM cognitive_events WHERE context_id=$1 AND id=$2 AND suppressed_at IS NULL',turn.context_id,turn.event_id)
        if not permitted or ctx['rebuilding'] or ctx['suppression_epoch']!=turn.epoch or ctx['authority']!='active':
            raise DeliverySuppressed()
        if channel=='sticker' and await conn.fetchval("""SELECT 1 FROM cognitive_outbox WHERE context_id=$1 AND channel='sticker'
            AND status IN ('sending','delivered','delivery_unknown') AND created_at>NOW()-INTERVAL '2 minutes'""",turn.context_id):
            raise DeliverySuppressed()
        row = await conn.fetchrow('''INSERT INTO cognitive_outbox(context_id,event_id,delivery_key,channel,payload,suppression_epoch)
            VALUES($1,$2,$3,$4,$5::jsonb,$6) ON CONFLICT(context_id,delivery_key) DO NOTHING RETURNING *''',turn.context_id,turn.event_id,delivery_key,channel,dump(payload),turn.epoch)
        if not row:
            row = await conn.fetchrow('SELECT * FROM cognitive_outbox WHERE context_id=$1 AND delivery_key=$2',turn.context_id,delivery_key)
            # Native Telegram cannot recover a sent Message from an idempotency
            # key. The caller skips previously completed/ambiguous turns.
            if row['status'] in ('delivered','sending','delivery_unknown'):
                raise DeliveryUnknown()
            if row['status']=='cancelled':
                raise DeliverySuppressed()
        await conn.execute("UPDATE cognitive_outbox SET status='sending',updated_at=NOW() WHERE id=$1",row['id'])
    try:
        result = await method(*args,**kwargs)
    except BaseException as exc:
        turn.delivery_blocked = True
        async with runtime.pool.acquire() as conn:
            await conn.execute("UPDATE cognitive_outbox SET status='delivery_unknown',updated_at=NOW() WHERE id=$1 AND status='sending'",row['id'])
        if isinstance(exc,asyncio.CancelledError):
            raise
        raise DeliveryUnknown() from None
    messages = result if isinstance(result,(list,tuple)) else [result]
    receipt = getattr(messages[0],'message_id',None) if messages else None
    if receipt is None:
        turn.delivery_blocked = True
        async with runtime.pool.acquire() as conn:
            await conn.execute("UPDATE cognitive_outbox SET status='delivery_unknown',updated_at=NOW() WHERE id=$1 AND status='sending'",row['id'])
        raise DeliveryUnknown()
    try:
        changed = await _confirm_transaction(turn,row,receipt,messages,text,channel)
    except BaseException as exc:
        turn.delivery_blocked = True
        try:
            async with runtime.pool.acquire() as conn:
                await conn.execute("UPDATE cognitive_outbox SET status='delivery_unknown',receipt_id=$2,updated_at=NOW() WHERE id=$1 AND status='sending'",row['id'],receipt)
        except Exception:
            pass
        if isinstance(exc,asyncio.CancelledError):
            raise
        raise DeliveryUnknown() from None
    if changed:
        from cognition.history import invalidate_history
        invalidate_history(chat_id)
    return result


async def _confirm_transaction(turn,row,receipt,messages,text,channel):
    runtime = turn.runtime
    chat_id = turn.event.context.chat_id
    async with runtime.pool.acquire() as conn,conn.transaction():
        context = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE',turn.context_id)
        permitted = await conn.fetchval('SELECT 1 FROM cognitive_events WHERE context_id=$1 AND id=$2 AND suppressed_at IS NULL',turn.context_id,turn.event_id)
        if not permitted or context['rebuilding'] or context['suppression_epoch']!=turn.epoch or context['authority']!='active':
            await conn.execute("UPDATE cognitive_outbox SET status='cancelled',payload=NULL,receipt_id=$2 WHERE id=$1",row['id'],receipt)
            return False
        changed = await conn.fetchval("UPDATE cognitive_outbox SET status='delivered',receipt_id=$2,updated_at=NOW() WHERE id=$1 AND status='sending' RETURNING id",row['id'],receipt)
        if changed:
            cid = turn.context_id
            included = await conn.fetchval("SELECT artifact_ids FROM cognitive_retrievals WHERE context_id=$1 AND cycle_key=$2 AND stage='included'",cid,turn.event.event_id) or []
            candidates = await conn.fetch('SELECT id,payload FROM cognitive_artifacts WHERE context_id=$1 AND id=ANY($2::bigint[]) AND suppressed_at IS NULL AND projection_epoch=$3',cid,included,turn.epoch)
            actual_text = '\n'.join(str(getattr(m,'text',None) or getattr(m,'caption',None) or text) for m in messages)
            expressed = []
            for artifact in candidates:
                p = object_value(artifact['payload'])
                anchors = [d['text'] for d in p.get('details',[])] if 'details' in p else [p.get('value','')]
                if any(len(a)>=8 and a in actual_text for a in anchors):
                    expressed.append(artifact['id'])
            await conn.execute("""INSERT INTO cognitive_retrievals(context_id,cycle_key,owner_id,stage,artifact_ids,created_at)
                VALUES($1,$2,$3,'expressed',$4::bigint[],$5) ON CONFLICT(context_id,cycle_key,stage) DO UPDATE
                SET artifact_ids=ARRAY(SELECT DISTINCT unnest(cognitive_retrievals.artifact_ids || EXCLUDED.artifact_ids))""",cid,turn.event.event_id,turn.event.evidence.owner_id,expressed,runtime.clock())
            for message in messages:
                actual_id = message.message_id
                source = f'telegram:{chat_id}:{actual_id}:delivered_action'
                at = runtime.clock()
                event = CognitiveEvent(source,turn.event.context,EvidenceRef(source,source,Origin.DELIVERED_ACTION,turn.event.evidence.owner_id),
                    at,at,str(getattr(message,'text',None) or getattr(message,'caption',None) or text or f'[{channel}]'),None,turn.event.evidence.owner_id,event_kind='delivery')
                payload = dump(event)
                eid = await conn.fetchval('''INSERT INTO cognitive_events(context_id,event_key,source_id,independent_group,origin,owner_id,occurred_at,observed_at,payload,fingerprint)
                    VALUES($1,$2,$2,$2,'delivered_action',$3,$4,$4,$5::jsonb,$6)
                    ON CONFLICT(context_id,event_key) DO UPDATE SET event_key=EXCLUDED.event_key RETURNING id''',cid,source,turn.event.evidence.owner_id,at,payload,hashlib.sha256(payload.encode()).hexdigest())
                await conn.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3) ON CONFLICT DO NOTHING',cid,eid,turn.event_id)
                await conn.execute('''INSERT INTO cognitive_event_dependencies(context_id,event_id,source_event_id)
                    SELECT $1,$2,source_event_id FROM cognitive_event_dependencies WHERE context_id=$1 AND event_id=$3
                    ON CONFLICT DO NOTHING''',cid,eid,turn.event_id)
                await conn.execute("INSERT INTO cognitive_jobs(context_id,event_id,kind) VALUES($1,$2,'encode') ON CONFLICT DO NOTHING",cid,eid)
                from cognition.history import save_source_history
                await save_source_history(conn,cid,eid,event,'Арти',event.text,actual_id)
    return changed
